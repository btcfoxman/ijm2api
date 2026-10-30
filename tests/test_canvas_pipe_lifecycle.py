"""Real toy subprocesses reproduce inherited pipes without a provider request."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid
from unittest.mock import Mock

import pytest


def _kill_owned_toy(marker: Path, nonce: str) -> None:
    if os.name != 'posix' or not marker.exists():
        return
    facts = json.loads(marker.read_text())
    for pid in [facts.get('child_pid'), facts.get('wrapper_pid')]:
        if not isinstance(pid, int):
            continue
        try:
            command = (Path('/proc') / str(pid) / 'cmdline').read_bytes()
            if nonce.encode() in command:
                os.kill(pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass


def toy_command(tmp_path, *, paid=False, stopped=False, hold_pipe=True, mode='valid', capture_failure=False):
    nonce = uuid.uuid4().hex
    marker = tmp_path / ('toy-' + nonce + '.json')
    child = (f'import os,signal,time; nonce={nonce!r}; '
             + ('os.kill(os.getpid(),signal.SIGSTOP); ' if stopped else '')
             + 'time.sleep(2)')
    data = ({'items': [{'nodeId': 'node-original', 'submitId': 'submit-original',
                       'state': 'ACCEPTED', 'resources': []}]} if paid else
            {'resourceId': 'original-resource', 'status': 'success', 'type': 'image'})
    envelope = {'ok': True, 'data': data}
    output = json.dumps(envelope)
    if mode == 'truncated':
        output = '{"ok": true,'
    if mode == 'trailing':
        output += ' ' * 70000 + 'not-json'
    if mode == 'overflow':
        output = None
    output_expression = "'x' * 100000" if mode == 'overflow' else repr(output)
    if mode == 'trailing':
        output_expression = repr(json.dumps(envelope)) + " + ' ' * 70000 + 'not-json'"
    wrapper = f'''
import json,os,subprocess,sys,time
nonce={nonce!r}
child=subprocess.Popen([sys.executable,'-c',{child!r}]) if {hold_pipe!r} else None
open({str(marker)!r},'w').write(json.dumps({{'wrapper_pid':os.getpid(),'child_pid':child.pid if child else None}}))
time.sleep(.1)
sys.stdout.write({output_expression});sys.stdout.flush()
{'time.sleep(2)' if mode == 'sleep' else ''}
'''
    script = f'''
import json,subprocess,sys,time
from app.canvas_cli import CanvasCLI,CanvasCLIError
import app.canvas_cli as module
actual=subprocess.Popen
signals=[]
if hasattr(module.os,'killpg'):
 actual_killpg=module.os.killpg
 def observed_signal(group,kind):
  signals.append([group,int(kind)])
  return actual_killpg(group,kind)
 module.os.killpg=observed_signal
def spawn(command,**kwargs):
 if '--profile' in command and '--format' in command:
  return actual([sys.executable,'-c',{wrapper!r}],**kwargs)
 return actual(command,**kwargs)
module.subprocess.Popen=spawn
runner=CanvasCLI({str(tmp_path / 'runner')!r},allow_unwrapped_for_tests=True,max_output_bytes=1024 if {mode=='overflow'!r} else 1024*1024)
if {capture_failure!r}:runner._capture_process_group=lambda process:None
start=time.monotonic()
try:
 value=runner.run({{'id':5,'proxy_url':'socks5://proxy.example.test:20001'}},
  {['node','run'] if paid else ['resource','get','original-resource']!r},timeout=.5,deadline=start+.5)
 result={{'ok':True,'value':value}}
except CanvasCLIError as error:
 result={{'ok':False,'code':error.code,'unknown':error.outcome_unknown,'exit_code':error.exit_code}}
finally:
 runner.close()
result['elapsed']=time.monotonic()-start
result['signals']=signals
if sys.platform.startswith('linux'):
 child=json.load(open({str(marker)!r})).get('child_pid')
 try:
  state=open('/proc/'+str(child)+'/stat').read().rsplit(')',1)[1].split()[0] if child else None
 except FileNotFoundError:state=None
 result['child_still_running']=state not in {{None,'Z'}}
print(json.dumps(result))
'''
    try:
        run = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                             timeout=5, cwd=Path(__file__).resolve().parents[1])
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout), marker, nonce
    finally:
        _kill_owned_toy(marker, nonce)


def test_wrapper_exit_with_inherited_pipe_cannot_hide_original_deadline(tmp_path):
    result, _, _ = toy_command(tmp_path)
    assert result['ok'] is True
    assert result['value']['data']['resourceId'] == 'original-resource'
    assert result['elapsed'] < 1.75


@pytest.mark.skipif(os.name != 'posix', reason='SIGSTOP process-group cleanup is POSIX only')
def test_stopped_child_is_cleaned_without_continuing_it_or_touching_foreign_session(tmp_path):
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(10)'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        result, _, _ = toy_command(tmp_path, stopped=True, mode='truncated')
        assert result['ok'] is False and result['unknown'] is False
        assert result['elapsed'] < 1.75
        assert result['child_still_running'] is False
        assert all(kind in {signal.SIGTERM, signal.SIGKILL} for _, kind in result['signals'])
        assert all(group != sentinel.pid for group, _ in result['signals'])
        assert sentinel.poll() is None
    finally:
        sentinel.kill()
        sentinel.wait(timeout=2)


def test_paid_run_with_unknown_inherited_pipe_is_not_reported_as_safe_to_replay(tmp_path):
    result, _, _ = toy_command(tmp_path, paid=True, mode='truncated')
    assert result['ok'] is False and result['unknown'] is True
    assert result['elapsed'] < 1.75


def test_complete_paid_receipt_is_returned_without_changing_admission(tmp_path):
    result, _, _ = toy_command(tmp_path, paid=True, hold_pipe=False)
    assert result['ok'] is True
    assert result['value']['data']['items'][0]['state'] == 'ACCEPTED'
    assert result['elapsed'] < 1.75


def test_complete_paid_receipt_survives_inherited_tail_pipe(tmp_path):
    result, _, _ = toy_command(tmp_path, paid=True)
    assert result['ok'] is True and result['elapsed'] < 1.75
    assert result['value']['data']['items'][0]['state'] == 'ACCEPTED'


def test_truncated_resource_reply_with_tail_pipe_is_bounded_not_success(tmp_path):
    result, _, _ = toy_command(tmp_path, mode='truncated')
    assert result['ok'] is False and result['unknown'] is False
    assert result['code'] in {'CLI_OUTPUT_DRAIN_TIMEOUT', 'CLI_COMMAND_TIMEOUT'}
    assert result['elapsed'] < 1.75


def test_ready_trailing_output_is_not_ignored_after_a_valid_json_prefix(tmp_path):
    result, _, _ = toy_command(tmp_path, paid=True, hold_pipe=False, mode='trailing')
    assert result['ok'] is False and result['code'] == 'CLI_INVALID_RESPONSE'
    assert result['unknown'] is True


def test_truncated_paid_output_remains_unknown(tmp_path):
    result, _, _ = toy_command(tmp_path, paid=True, hold_pipe=False, mode='truncated')
    assert result['ok'] is False and result['code'] == 'CLI_INVALID_RESPONSE'
    assert result['unknown'] is True and result['elapsed'] < 1.75


def test_output_limit_remains_bounded_and_paid_outcome_unknown(tmp_path):
    result, _, _ = toy_command(tmp_path, paid=True, hold_pipe=False, mode='overflow')
    assert result['ok'] is False and result['code'] == 'CLI_OUTPUT_LIMIT'
    assert result['unknown'] is True and result['elapsed'] < 1.75


@pytest.mark.parametrize('paid', [False, True])
def test_running_wrapper_timeout_is_bounded_and_preserves_dispatch_category(tmp_path, paid):
    result, _, _ = toy_command(tmp_path, paid=paid, hold_pipe=False, mode='sleep')
    assert result['ok'] is False and result['code'] == 'CLI_COMMAND_TIMEOUT'
    assert result['unknown'] is paid and result['elapsed'] < 1.75


def test_reused_leader_pid_refuses_all_group_cleanup(monkeypatch):
    from app.canvas_cli import CanvasCLI
    process = SimpleNamespace(pid=70000, _canvas_owned_group=(70000, 100))
    monkeypatch.setattr(CanvasCLI, '_linux_identity', lambda pid: (70000, 70000, 200, 'S'))
    assert CanvasCLI._owned_group_members(process) == []


def test_foreign_session_with_matching_numeric_group_is_not_owned(monkeypatch):
    from app.canvas_cli import CanvasCLI
    process = SimpleNamespace(pid=70000, _canvas_owned_group=(70000, 100))
    monkeypatch.setattr(CanvasCLI, '_linux_identity', lambda pid: (70000, 69999, 100, 'S'))
    assert CanvasCLI._owned_group_members(process) == []


def test_unproven_process_group_is_never_a_cleanup_target():
    from app.canvas_cli import CanvasCLI
    assert CanvasCLI._owned_group_members(SimpleNamespace(pid=70000)) == []


def test_vanished_leader_without_a_known_live_member_cannot_claim_a_reused_session(monkeypatch):
    from app.canvas_cli import CanvasCLI
    process = SimpleNamespace(pid=70000, _canvas_owned_group=(70000, 100),
                              _canvas_known_members={70000: 100, 70001: 101})
    identities = {70000: None, 70001: (70000, 70000, 999, 'S')}
    monkeypatch.setattr(CanvasCLI, '_linux_identity', identities.get)
    assert CanvasCLI._owned_group_members(process) == []


def test_group_token_must_belong_to_this_exact_popen_pid(monkeypatch):
    from app.canvas_cli import CanvasCLI
    process = SimpleNamespace(pid=69999, _canvas_owned_group=(70000, 100))
    monkeypatch.setattr(CanvasCLI, '_linux_identity', lambda pid: (70000, 70000, 100, 'S'))
    assert CanvasCLI._owned_group_members(process) == []


def test_missing_group_capture_still_terminates_the_live_direct_child(tmp_path):
    result, _, _ = toy_command(tmp_path, hold_pipe=False, mode='sleep', capture_failure=True)
    assert result['ok'] is False and result['code'] == 'CLI_COMMAND_TIMEOUT'
    assert result['exit_code'] is not None
    assert result['elapsed'] < 1.75


@pytest.mark.skipif(os.name != 'posix', reason='POSIX group signal race')
def test_disappearing_owned_group_does_not_replace_command_result(monkeypatch):
    from app.canvas_cli import CanvasCLI
    process = SimpleNamespace(pid=70000, poll=lambda: 0, wait=lambda **kwargs: 0)
    monkeypatch.setattr(CanvasCLI, '_owned_group_members', lambda *args, **kwargs: [70001])

    def vanished(*args):
        raise ProcessLookupError('Synthetic group disappeared during bounded cleanup')

    monkeypatch.setattr('app.canvas_cli.os.killpg', vanished)
    CanvasCLI._terminate(process)


def test_complete_normal_receipt_on_last_read_is_preserved_at_exact_deadline(tmp_path, monkeypatch):
    from app.canvas_cli import CanvasCLI
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    envelope = {'ok': True, 'data': {'items': [{'state': 'ACCEPTED'}]}}
    os.write(stdout_write, json.dumps(envelope).encode())
    os.close(stdout_write)
    os.close(stderr_write)
    process = SimpleNamespace(pid=70000, stdout=os.fdopen(stdout_read, 'rb', buffering=0),
                              stderr=os.fdopen(stderr_read, 'rb', buffering=0), poll=lambda: 0)
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    moments = iter([0.0, 1.0])
    monkeypatch.setattr('app.canvas_cli.time', SimpleNamespace(
        monotonic=lambda: next(moments, 1.0), sleep=lambda delay: None))
    try:
        buffers, failure = runner._collect_output(process, 1.0)
        assert failure == ''
        assert json.loads(buffers[0]) == envelope
    finally:
        process.stdout.close()
        process.stderr.close()


def test_running_commands_do_not_scan_proc_and_exit_is_observed_once_before_reap(monkeypatch):
    from app.canvas_cli import CanvasCLI
    process = SimpleNamespace(pid=70000, returncode=None, _canvas_owned_group=(70000, 100))
    observations = {'exited': False}
    scan = Mock(return_value=[70001])
    monkeypatch.setattr(CanvasCLI, '_owned_group_members', scan)
    monkeypatch.setattr('app.canvas_cli.os.waitid', lambda *args: object() if observations['exited'] else None,
                        raising=False)
    for name, value in [('P_PID', 1), ('WEXITED', 4), ('WNOHANG', 1), ('WNOWAIT', 0x1000000)]:
        monkeypatch.setattr('app.canvas_cli.os.' + name, value, raising=False)

    def reap():
        assert scan.call_count == 1
        process.returncode = 0
        return 0

    process.poll = Mock(side_effect=reap)
    for _ in range(100):
        assert CanvasCLI._poll_with_group_observation(process) is None
    scan.assert_not_called()
    process.poll.assert_not_called()
    observations['exited'] = True
    assert CanvasCLI._poll_with_group_observation(process) == 0
    assert CanvasCLI._poll_with_group_observation(process) == 0
    scan.assert_called_once_with(process, observe=True)


def test_member_observation_during_identity_read_cannot_break_cleanup_iteration(monkeypatch):
    from app.canvas_cli import CanvasCLI
    known = {70000: 100, 70001: 101, 70002: 102}
    process = SimpleNamespace(pid=70000, _canvas_owned_group=(70000, 100),
                              _canvas_known_members=known)

    def identity(pid):
        if pid == 70001:
            known[70003] = 103  # Another observation while the file read releases the GIL.
        if pid == 70002:
            return 70000, 70000, 102, 'S'
        return None

    monkeypatch.setattr(CanvasCLI, '_linux_identity', identity)
    monkeypatch.setattr('app.canvas_cli.Path', lambda path: SimpleNamespace(glob=lambda pattern: []))
    assert CanvasCLI._owned_group_members(process) == []
    assert known[70003] == 103
