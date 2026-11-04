"""Confirmed generation timeout keeps receipt ownership and honest finance."""
from copy import deepcopy
import time
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLIError
from test_canvas_execution import context, owner, task
from test_canvas_resource_recovery import context as original_context


def accepted(context, *, expired=True):
    store, service, _ = context
    account = owner(store)
    task(store, 'ijm_original')
    bound = original_context()
    bound.update(account_id=account['id'], expected_user_id=account['login_user_id'],
        submission_phase='accepted',
        observation_deadline=time.time() + (-1 if expired else 60))
    row = store.update_task('ijm_original', {'account_id': account['id'], 'execution_backend': 'canvas_cli',
        'status': 'running', 'estimated_cost': 37, 'balance_before': 100,
        'upstream_context': bound})
    return store, service, row


def test_confirmed_original_receipt_deadline_keeps_bounded_original_result_observer(context):
    store, service, before = accepted(context)
    result = service._finish_canvas_observation(before['id'], reason='observation_deadline_reached')
    assert result['status'] == 'running' and result['error_code'] == result['error_message'] == ''
    audit = result['upstream_context']
    assert audit['result_observation']['attempts'] == 0
    assert audit['result_observation']['preserve_finance'] is False
    assert 'deadline' not in audit['result_observation']
    assert audit.get('financial_outcome') != 'not_charged_this_invocation'
    assert not audit.get('reconciliation_closed') and not audit.get('submit_error')
    assert not audit.get('pre_dispatch_failure')
    for key in ('request', 'normalized', 'actual_cost', 'estimated_cost', 'balance_before', 'balance_after'):
        assert result[key] == before[key]
    for key in ('project_id', 'node_id', 'submit_id', 'resource_ids', 'submission_evidence', 'observation_deadline'):
        assert audit[key] == before['upstream_context'][key]


def test_main_observation_deadline_queues_original_result_without_more_cli_work(context):
    store, service, row = accepted(context)
    client = Mock()
    service._poll_canvas(row['id'], client, time.monotonic() - 1)
    result = store.task(row['id'])
    assert result['error_code'] == '' and result['status'] == 'running'
    assert result['upstream_context']['result_observation']['attempts'] == 0
    client.poll.assert_not_called()
    client.prepare.assert_not_called()
    client.submit.assert_not_called()


def test_deadline_exception_at_the_original_boundary_preserves_the_confirmed_verdict(context, monkeypatch):
    store, service, row = accepted(context, expired=False)
    wall = time.time(); mono = time.monotonic(); clock = {'elapsed': 0}
    monkeypatch.setattr('app.canvas_service.time.time', lambda: wall + clock['elapsed'])
    monkeypatch.setattr('app.canvas_service.time.monotonic', lambda: mono + clock['elapsed'])
    client = Mock()
    def expired(*args, **kwargs):
        clock['elapsed'] = 61
        raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'read reached original deadline')
    client.poll.side_effect = expired
    service._poll_canvas(row['id'], client, mono + 60)
    result = store.task(row['id'])
    assert result['error_code'] == '' and result['status'] == 'running'
    assert result['upstream_context']['result_observation']['attempts'] == 0
    assert client.poll.call_count == 1
    assert result['actual_cost'] is None


def test_manual_termination_or_a_closed_failure_wins_over_observation_timeout(context):
    store, service, row = accepted(context)
    bound = {**row['upstream_context'], 'manual_termination': {'at': time.time()}}
    closed = store.update_task(row['id'], {'status': 'failed', 'error_code': 'TASK_TERMINATED',
        'error_message': 'closed by operator', 'upstream_context': bound})
    assert service._finish_canvas_observation(row['id'], reason='observation_deadline_reached') == closed
    assert store.task(row['id']) == closed


def test_ambiguous_partial_without_explicit_acceptance_stays_on_existing_unconfirmed_path(context):
    store, service, row = accepted(context)
    bound = deepcopy(row['upstream_context'])
    bound['submission_evidence']['accepted'] = False
    store.update_task(row['id'], {'upstream_context': bound})
    result = service._finish_canvas_observation(row['id'], reason='observation_deadline_reached')
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert 'accepted_observation_timeout' not in result['upstream_context']


def test_stored_known_financial_outcome_is_not_rewritten(context):
    store, service, row = accepted(context)
    bound = {**row['upstream_context'], 'financial_outcome': 'charged'}
    store.update_task(row['id'], {'upstream_context': bound, 'actual_cost': 37, 'balance_after': 63})
    result = service._finish_canvas_observation(row['id'], reason='observation_deadline_reached')
    assert result['upstream_context']['financial_outcome'] == 'charged'
    assert result['actual_cost'] == 37 and result['balance_after'] == 63


@pytest.mark.parametrize('code', ['cli.transport_failed', 'CLI_TASK_DEADLINE_EXCEEDED'])
def test_transient_query_does_not_exit_the_original_generation_window(context, monkeypatch, code):
    store, service, row = accepted(context, expired=False)
    wall = time.time(); mono = time.monotonic(); clock = {'elapsed': 0}
    monkeypatch.setattr('app.canvas_service.time.time', lambda: wall + clock['elapsed'])
    monkeypatch.setattr('app.canvas_service.time.monotonic', lambda: mono + clock['elapsed'])
    client = Mock(); queries = []
    def poll(bound, **kwargs):
        assert bound['submit_id'] == row['upstream_context']['submit_id']
        assert bound['resource_ids'] == row['upstream_context']['resource_ids']
        assert kwargs['deadline'] <= mono + 60
        queries.append(bound)
        if len(queries) == 1:
            clock['elapsed'] = 1
            raise CanvasCLIError(code, 'temporary query failure', details={'error': {
                'class': 'retryable_failure', 'requiredAction': 'retry'}})
        current = store.task(row['id'])
        assert current['status'] == 'running' and current['error_code'] == ''
        assert 'result_observation' not in current['upstream_context']
        clock['elapsed'] = 61
        return {'status': 'running', 'operation_state': 'running', 'progress': 50}
    client.poll.side_effect = poll
    service._poll_canvas(row['id'], client, mono + 60)
    result = store.task(row['id'])
    assert len(queries) == 2 and result['status'] == 'running' and result['error_code'] == ''
    assert result['upstream_context']['result_observation']['attempts'] == 0
    assert result['actual_cost'] is None
    client.submit.assert_not_called(); client.prepare.assert_not_called()


def test_authentication_rejection_is_not_hidden_by_query_retry(context):
    store, service, row = accepted(context, expired=False)
    client = Mock()
    client.poll.side_effect = CanvasCLIError('cli.authentication_required', 'login required',
        details={'error': {'requiredAction': 'login'}})
    service._poll_canvas(row['id'], client, time.monotonic() + 60)
    result = store.task(row['id'])
    assert result['status'] == 'failed' and result['error_code'] == 'cli.authentication_required'
    assert client.poll.call_count == 1
    assert 'result_observation' not in result['upstream_context']
    assert result['actual_cost'] is None
