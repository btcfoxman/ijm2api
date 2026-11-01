"""Bound abnormal original-operation reads, not a normally pending video."""
from copy import deepcopy
import time
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.errors import public_error
from app.catalog import normalize_request
from app.image_client import resolve_image_model
from app.image_request import normalize_image_request
from test_canvas_execution import context, owner, task, fake_client
from test_canvas_admin_api import context as api_context


PUBLIC = '视频生成提交失败，请稍后重试~'


def started(context):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000, 'canvas_missing_resource_query_attempts': 3,
                           'canvas_missing_resource_timeout_seconds': 60})
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    client.submit.side_effect = CanvasCLIError('cli.transport_failed', 'original run reply unavailable',
        details={'dispatch_started': True, 'phase': 'run_dispatching'}, outcome_unknown=True)
    return store, service, client


@pytest.mark.parametrize('shape', ['exception', 'short_command_deadline', 'unknown', 'terminal_without_outputs'])
def test_original_dispatched_task_has_three_abnormal_reads_then_terminal_without_run_replay(context, shape):
    store, service, client = started(context)
    if shape in {'exception', 'short_command_deadline'}:
        code = 'CLI_TASK_DEADLINE_EXCEEDED' if shape == 'short_command_deadline' else 'cli.transport_failed'
        client.poll.side_effect = CanvasCLIError(code, 'query interrupted', outcome_unknown=True)
    elif shape == 'unknown':
        client.poll.return_value = {'status': 'unknown', 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'}
    else:
        client.poll.return_value = {'status': 'running', 'operation_state': 'completed',
            'output_resources_missing': True, 'resource_recovery_required': True}
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    row = store.task('task'); ctx = row['upstream_context']
    assert row['status'] == 'failed'
    assert row['error_code'] == 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED'
    assert public_error(row['error_code'], row['error_message'])['message'] == PUBLIC
    assert client.submit.call_count == 1 and client.poll.call_count == 3
    assert ctx['missing_resource_recovery']['attempts'] == 3
    assert ctx['missing_resource_recovery']['deadline'] - ctx['missing_resource_recovery']['started_at'] <= 60
    assert ctx['reconciliation_close_reason'] == 'missing_output_resource_budget_exhausted'
    assert ctx['financial_outcome'] == 'unconfirmed' and row['actual_cost'] is None
    assert not ctx.get('pre_dispatch_failure') and ctx['submit_id'] == 'submit-task-1'
    assert not ctx.get('preparation_retry_budget')
    public = service.public_task(row)
    assert public['error'] == {'code': row['error_code'], 'message': PUBLIC}
    assert public['provider_context'] == {'may_have_been_accepted': True,
        'financial_outcome': 'unconfirmed', 'automatic_resubmission': False, 'fallback_allowed': True}
    assert all(call.kwargs['deadline'] <= time.monotonic() + 60 for call in client.poll.call_args_list)


@pytest.mark.parametrize('state', ['pending', 'queued', 'accepted', 'running', 'in_progress', 'processing'])
def test_positive_original_operation_can_have_empty_outputs_for_many_polls(context, state):
    store, service, client = started(context)
    complete = deepcopy(client.poll.return_value)
    client.poll.side_effect = ([{'status': 'running', 'operation_state': state,
        'output_resources_missing': True, 'resource_recovery_required': False}] * 7 + [complete])
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    row = store.task('task')
    assert row['status'] == 'succeeded'
    assert client.poll.call_count == 8 and client.submit.call_count == 1
    assert 'missing_resource_recovery' not in row['upstream_context']


def test_restart_preserves_exhausted_anomaly_budget_without_another_cli_read(context):
    store, service, client = started(context)
    now = time.time()
    store.update_task('task', {'status': 'submitted', 'account_id': 1, 'execution_backend': 'canvas_cli',
        'upstream_context': {'submit_id': 'original-submit', 'project_id': 'original-project', 'resource_ids': [],
            'observation_deadline': now + 3000, 'submission_phase': 'run_dispatching',
            'submit_error': {'dispatch_started': True, 'outcome_unknown': True},
            'missing_resource_recovery': {'version': 1, 'started_at': now - 61, 'deadline': now - 1,
                'attempts': 1, 'max_attempts': 3, 'active': True}}})
    service._poll_canvas('task', client, time.monotonic() + 3000)
    row = store.task('task')
    assert row['error_code'] == 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED'
    client.poll.assert_not_called(); client.submit.assert_not_called()
    assert row['upstream_context']['missing_resource_recovery']['deadline'] == now - 1


def test_each_recovery_read_gets_remaining_absolute_budget(context, monkeypatch):
    store, service, client = started(context)
    clock = {'wall': time.time(), 'mono': time.monotonic()}
    monkeypatch.setattr('app.canvas_service.time.time', lambda: clock['wall'])
    monkeypatch.setattr('app.canvas_service.time.monotonic', lambda: clock['mono'])
    deadlines = []
    def interrupted(_context, *, deadline):
        deadlines.append(deadline)
        clock['wall'] += 31; clock['mono'] += 31
        raise CanvasCLIError('cli.transport_failed', 'query interrupted', outcome_unknown=True)
    client.poll.side_effect = interrupted
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    row = store.task('task')
    assert row['error_code'] == 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED'
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert row['upstream_context']['missing_resource_recovery']['attempts'] == 2


@pytest.mark.parametrize('code', ['CLI_PREPARATION_RETRY_EXHAUSTED', 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED'])
@pytest.mark.parametrize('raw', ['积分已返还', 'Canvas generation failed (20115)', '上游维护中，请稍后再试~'])
def test_budget_public_failure_is_authoritative_and_never_fabricates_refund(code, raw):
    assert public_error(code, raw) == {'code': code, 'message': PUBLIC}


@pytest.mark.parametrize('key,value', [('canvas_missing_resource_query_attempts', True),
    ('canvas_missing_resource_query_attempts', 0), ('canvas_missing_resource_query_attempts', 11),
    ('canvas_missing_resource_timeout_seconds', -1), ('canvas_missing_resource_timeout_seconds', 301),
    ('canvas_missing_resource_timeout_seconds', '60')])
def test_recovery_configuration_rejects_invalid_bounds(context, key, value):
    with pytest.raises(ValueError):
        context[1].update_settings({key: value})


@pytest.mark.parametrize('kind', ['video', 'image'])
def test_all_existing_caller_projections_keep_typed_terminal_and_honest_financial_context(api_context, kind):
    main, api, _headers, account_id = api_context
    body = {'model': 'seedream-5.0-pro', 'prompt': 'local fixture'} if kind == 'image' else {
        'model': 'sd-2-0-mini', 'prompt': 'local fixture', 'duration': 4}
    normalized = (normalize_image_request(body, resolve_image_model(body['model'])) if kind == 'image'
                  else normalize_request(body))
    row = main.store.create_task('ijm_budget_api', body, normalized, execution_backend='canvas_cli')
    now = time.time()
    ctx = {'backend': 'canvas_cli', 'account_id': account_id, 'task_id': row['id'],
        'profile': 'original-profile', 'project_id': 'original-project', 'node_id': 'original-node',
        'submit_id': 'original-submit', 'resource_ids': [], 'kind': kind,
        'submission_phase': 'run_dispatching', 'financial_outcome': 'unconfirmed',
        'submit_error': {'code': 'cli.transport_failed', 'dispatch_started': True, 'outcome_unknown': True},
        'reconciliation_closed': True, 'reconciliation_close_reason': 'missing_output_resource_budget_exhausted',
        'missing_resource_recovery': {'version': 1, 'started_at': now - 10, 'deadline': now + 50,
                                      'attempts': 3, 'max_attempts': 3, 'active': True}}
    row = main.store.update_task(row['id'], {'account_id': account_id, 'status': 'failed',
        'completed_at': now, 'upstream_context': ctx, 'error_code': 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED',
        'error_message': 'original CLI raw diagnostics may contain maintenance or refund prose'})
    before = deepcopy(row)
    paths = [f"/v1/responses/{row['id']}"] if kind == 'image' else [
        f"/v1/videos/{row['id']}", f"/api/videos/{row['id']}",
        f"/api/v3/contents/generations/tasks/{row['id']}"]
    for path in paths:
        response = api.get(path, headers={'X-API-Key': 'synthetic-canvas-api'})
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'failed'
        assert data['error'] == {'code': 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED', 'message': PUBLIC}
        assert data['provider_context'] == {'may_have_been_accepted': True,
            'financial_outcome': 'unconfirmed', 'automatic_resubmission': False, 'fallback_allowed': True}
        assert 'original-submit' not in response.text and 'refund prose' not in response.text
    assert main.store.task(row['id']) == before
