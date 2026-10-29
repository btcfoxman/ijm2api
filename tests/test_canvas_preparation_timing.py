"""Timing diagnostics cannot change admission, paid identity or lock boundaries."""
import ast
import json
from pathlib import Path
import threading
import time
from typing import Any
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_timing import PreparationTiming
from test_canvas_execution import context, owner, task, fake_client
from test_canvas_confirmation_boundary import boundary, TOKEN


def test_command_timings_are_bounded_and_never_capture_args_or_account_secrets():
    clock = [0.0]
    timing = PreparationTiming('task-a', monotonic=lambda:clock[0], wall_time=lambda:1700000000+clock[0])
    account = {'id':19, 'proxy_url':'socks5://private-user:private-password@proxy:9'}
    def command():
        clock[0] += .002
        return {'unchanged':'value'}
    for index in range(100):
        result = timing.command(object(), account, ['node','quote','--prompt','private-prompt',
            '--source-url','https://private.example/signed?token=private-token'], {}, index%3+1, command)
        assert result == {'unchanged':'value'}
    timing.finish()
    value = timing.snapshot()
    assert value['command_count'] == 100 and len(value['recent_commands']) == 64
    assert value['dropped_command_samples'] == 36
    assert value['commands']['node quote']['elapsed_ms'] == 200
    assert value['commands']['node quote']['unknown_lock_wait_count'] == 100
    assert all(row['lock_wait_ms'] is None for row in value['recent_commands'])
    assert not any(word in json.dumps(value) for word in ['private-user','private-password','private-prompt',
        'private.example','private-token','--source-url','--prompt'])
    timing.command(object(), account, ['node','show'], {}, 1, command)
    assert timing.snapshot()['command_count'] == 100


def test_failure_and_unknown_fields_remain_measured_not_invented():
    clock = [0.0]
    timing = PreparationTiming('task-b', monotonic=lambda:clock[0], wall_time=lambda:1700000000)
    error = CanvasCLIError('cli.transport_failed','private upstream prose')
    def failure():
        clock[0] += 1.25
        raise error
    with pytest.raises(CanvasCLIError) as caught:
        with timing.phase('identity'):
            timing.command(object(), {'id':1}, ['auth','account'], {}, 2, failure)
    assert caught.value is error
    timing.finish()
    value = timing.snapshot();row=value['recent_commands'][0]
    assert row['elapsed_ms'] == row['execution_ms'] == 1250 and row['lock_wait_ms'] is None
    assert row['attempt'] == 2 and row['error_code'] == 'cli.transport_failed'
    assert value['phases']['identity']['failed'] == 1
    assert 'private upstream prose' not in json.dumps(value)


def test_real_account_lock_wait_is_separate_and_keeps_original_deadline(tmp_path, monkeypatch):
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    executed = Mock(return_value={'ok':True,'data':{'unchanged':True}})
    monkeypatch.setattr(runner,'_execute',executed)
    timing = PreparationTiming('task-lock')
    client = CanvasClient(runner, {'id':19});client.preparation_timing=timing
    locked,release=threading.Event(),threading.Event()
    def holder():
        with runner._lock(19):
            locked.set();release.wait(2)
    thread=threading.Thread(target=holder);thread.start();assert locked.wait(1)
    timer=threading.Timer(.12,release.set);timer.start()
    deadline=time.monotonic()+1
    try:
        assert client._call(['auth','account'], deadline=deadline) == {'unchanged':True}
        row=timing.snapshot()['recent_commands'][0]
        assert row['lock_wait_ms'] >= 50 and row['elapsed_ms'] >= row['lock_wait_ms']
        assert row['execution_ms'] is not None
        assert executed.call_args.kwargs['deadline'] == deadline
    finally:
        release.set();timer.cancel();thread.join(1);runner.close()


def test_waiting_past_original_deadline_does_not_start_a_command(tmp_path, monkeypatch):
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    executed = Mock(side_effect=AssertionError('Expired command started'))
    monkeypatch.setattr(runner,'_execute',executed)
    timing=PreparationTiming('task-expired');client=CanvasClient(runner,{'id':19});client.preparation_timing=timing
    locked,release=threading.Event(),threading.Event()
    def holder():
        with runner._lock(19):locked.set();release.wait(1)
    thread=threading.Thread(target=holder);thread.start();assert locked.wait(1)
    try:
        with pytest.raises(CanvasCLIError) as caught:
            client._call(['auth','account'], deadline=time.monotonic()+.02)
        assert caught.value.code=='CLI_TASK_DEADLINE_EXCEEDED'
        row=timing.snapshot()['recent_commands'][0]
        assert row['outcome']=='failed' and row['execution_ms'] is None and row['lock_wait_ms'] is None
        executed.assert_not_called()
    finally:
        release.set();thread.join(1);runner.close()


def test_confirmation_timings_flush_only_after_existing_atomic_guard_exits(boundary):
    runner,client,context,_,_=boundary
    observations=[]
    timing=PreparationTiming('task-submit',on_change=lambda snapshot:observations.append(
        runner._lock(19)._is_owned()))
    client.preparation_timing=timing
    with timing.defer_notifications():
        result=client.submit(context,20,persist=lambda value:None,deadline=time.monotonic()+1)
    assert result['phase']=='accepted' and observations and not any(observations)
    assert TOKEN not in json.dumps(timing.snapshot())


def test_telemetry_failure_cannot_erase_an_accepted_receipt(boundary):
    runner,client,context,_,_=boundary
    client.preparation_timing=PreparationTiming('task-telemetry-failed',
        on_change=Mock(side_effect=RuntimeError('private storage failure')))
    with client.preparation_timing.defer_notifications():
        result=client.submit(context,20,persist=lambda value:None,deadline=time.monotonic()+1)
    assert result['phase']=='accepted' and context['submission_phase']=='accepted'


def test_per_task_objects_do_not_share_command_events():
    first,second=PreparationTiming('task-one'),PreparationTiming('task-two')
    first.command(object(),{'id':19},['node','quote'],{},1,lambda:{})
    assert first.snapshot()['command_count']==1 and second.snapshot()['command_count']==0
    assert first.snapshot()['task_id']=='task-one' and second.snapshot()['task_id']=='task-two'


def test_service_persists_timings_without_changing_cost_or_public_task_shape(context):
    store,service,_=context;account=owner(store);task(store)
    client=fake_client(store,service,account)
    with patch.object(service,'canvas_client',return_value=client):service._run('task')
    row=store.task('task');timing=row['upstream_context']['preparation_timing']
    assert row['status']=='succeeded' and timing['finished'] is True and timing['task_id']=='task'
    assert {'identity','model_preflight','material_prepare','credit_wait','confirmation_submit'} <= timing['phases'].keys()
    assert not any('timing' in key for key in row['normalized'].get('_cost_evidence',{}))
    assert 'preparation_timing' not in json.dumps(service.public_task(row))


@pytest.mark.parametrize('failure',['identity','model','credits'])
def test_admission_reselection_keeps_priority_and_does_not_gain_a_top_level_timing_key(context,failure):
    store,service,_=context
    first=owner(store,'first',priority=9);second=owner(store,'second',priority=8);task(store)
    clients={a['id']:fake_client(store,service,a) for a in (first,second)}
    first_client=clients[first['id']]
    if failure=='identity':first_client.identity.side_effect=CanvasCLIError('CLI_COMMAND_TIMEOUT','identity timeout')
    elif failure=='model':first_client.preflight_model.side_effect=CanvasCLIError('CLI_CAPABILITY_UNAVAILABLE',
        'missing model',details={'error':{'scope':'account','reason':'model_not_in_catalog',
            'requested_model':'sd-2-0-mini','kind':'video'}})
    else:first_client.submit.side_effect=CanvasCLIError('INSUFFICIENT_CREDITS','not enough credits left')
    with patch.object(service,'canvas_client',side_effect=lambda account:clients[account['id']]):service._run('task')
    row=store.task('task')
    assert row['status']=='succeeded' and row['account_id']==second['id']
    assert 'preparation_error' not in row['upstream_context']
    assert row['upstream_context']['preparation_timing']['finished'] is True
    assert row['actual_cost']==10


def test_admin_details_expose_timing_but_strip_the_raw_context():
    path=Path(__file__).parents[1]/'app/main.py';tree=ast.parse(path.read_text(encoding='utf-8'))
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_with_cost_observations')
    namespace={'__package__':'app','Any':Any,'build_observations':lambda task:[]}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),namespace)
    row={'id':'task','execution_backend':'canvas_cli','upstream_context':{'private_field':'not exposed',
        'preparation_timing':PreparationTiming('task').snapshot()}}
    result=namespace['_with_cost_observations'](row)
    assert 'upstream_context' not in result
    assert result['execution_diagnostics']['preparation_timing']['task_id']=='task'
    assert 'private_field' not in repr(result)
