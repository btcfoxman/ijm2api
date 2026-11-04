"""A slow resource read cannot lose the original accepted operation's IDs."""
from copy import deepcopy
import time

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_resource_recovery import operation_receipt, accepted_output_context
from test_canvas_execution import context
from test_canvas_operation_node_recovery import PROJECT, SUBMIT, NODE
from test_canvas_output_binding_persistence import (
    RID, OTHER, durable, no_live_commands_or_network)


@pytest.fixture
def uncertain(durable):
    store, service, account, runner, row = durable
    bound = deepcopy(row['upstream_context'])
    bound.update(project_id=PROJECT, node_id=NODE, submit_id=SUBMIT,
        phase='quoted', submission_phase='run_dispatching',
        submit_error={'code': 'service.1', 'class': 'retryable_failure',
                      'outcome_unknown': True, 'requiredAction': 'retry'})
    bound['submission_evidence'].update(accepted=False, output_resources=False,
        receipt=False, ambiguous_partial=True)
    row = store.update_task(row['id'], {'upstream_context': bound})
    runner.bound = bound
    runner.facts[RID] = {'status': 'success'}
    receipt = operation_receipt(bound, {'operationRef': SUBMIT, 'state': 'succeeded',
        'submission': {'state': 'completed', 'resubmittable': False,
                       'adviceCode': 'submission_accepted'},
        'resources': [{'resourceId': RID, 'state': 'succeeded'}]})
    assert receipt
    return store, service, account, runner, row, receipt


def test_accepted_ids_survive_failed_resource_read_and_restart_without_paid_replay(uncertain):
    store, service, account, runner, row, _ = uncertain
    original_run = runner.run
    first = True

    def run(owner, args, **kwargs):
        if args[:2] == ['resource', 'get'] and first:
            saved = store.task(row['id'])
            assert saved['upstream_context']['resource_ids'] == [RID]
            assert saved['upstream_context']['operation_receipt']['durable'] is True
            assert saved['upstream_context']['submit_error'] == row['upstream_context']['submit_error']
            assert saved['actual_cost'] is None
            raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Original resource read interrupted')
        return original_run(owner, args, **kwargs)

    runner.run = run
    client = CanvasClient(runner, account)
    with pytest.raises(CanvasCLIError):
        client.poll(deepcopy(row['upstream_context']), deadline=time.monotonic() + 120,
            persist_operation_receipt=lambda receipt: service._canvas_persist_operation_receipt(
                row['id'], receipt, account))
    first = False
    saved = store.task(row['id'])
    assert accepted_output_context(saved['upstream_context'])
    assert saved['status'] == 'running' and saved['actual_cost'] is None
    assert 'output_binding' not in saved['upstream_context']
    operation_reads = runner.count(('operation', 'status'))
    # A new client represents a resumed worker, using only the durable receipt.
    result = CanvasClient(runner, account).poll(deepcopy(saved['upstream_context']),
        deadline=time.monotonic() + 120,
        persist_output_binding=lambda value: service._canvas_persist_output_binding(row['id'], value))
    assert result['status'] == 'succeeded'
    assert runner.count(('operation', 'status')) == operation_reads == 1
    assert runner.count(('resource', 'download')) == 1
    assert store.task(row['id'])['actual_cost'] is None


@pytest.mark.parametrize('status', ['failed', 'expired', 'cancelled', 'succeeded'])
def test_closed_tasks_cannot_be_reopened_by_late_acceptance(uncertain, status):
    store, service, account, runner, row, receipt = uncertain
    closed = store.update_task(row['id'], {'status': status, 'completed_at': time.time(),
        'error_code': 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED'})
    assert not service._canvas_persist_operation_receipt(row['id'], receipt, account)
    assert store.task(row['id']) == closed


@pytest.mark.parametrize('change', [
    {'proxy_url': 'socks5://different.test:20002'}, {'login_user_id': 'different-user'},
    {'cli_user_id': 'different-user'}, {'execution_mode': 'browser'},
    {'project_id': 'different-project'}, {'region': 'cn'},
])
def test_account_rotation_invalidates_receipt_before_commit(uncertain, change):
    store, service, account, runner, row, receipt = uncertain
    store.update_account(account['id'], change)
    before = store.task(row['id'])
    assert not service._canvas_persist_operation_receipt(row['id'], receipt, account)
    assert store.task(row['id']) == before


@pytest.mark.parametrize('change', [
    {'project_id': SUBMIT}, {'node_id': 'node_0000000000'}, {'submit_id': PROJECT},
    {'task_id': 'ijm_foreign'}, {'account_id': 999}, {'profile': 'foreign-profile'},
    {'expected_user_id': 'foreign-user'}, {'resource_ids': [RID, OTHER]},
    {'operation_state': 'failed'},
])
def test_foreign_or_incomplete_receipts_have_no_task_or_finance_effect(uncertain, change):
    store, service, account, runner, row, receipt = uncertain
    assert not service._canvas_persist_operation_receipt(row['id'], {**receipt, **change}, account)
    assert store.task(row['id']) == row


def test_manual_stop_or_concurrent_task_write_wins_transaction_CAS(uncertain):
    store, service, account, runner, row, receipt = uncertain
    current = store.update_task(row['id'], {'progress': 46, 'upstream_context': {
        **row['upstream_context'], 'last_query_error': {'code': 'CLI_COMMAND_TIMEOUT'}}})
    assert not store.remember_canvas_operation_receipt(row['id'], receipt,
        expected_updated_at=row['updated_at'], expected_context=row['upstream_context'],
        expected_account=account)
    assert store.task(row['id']) == current
    stopped = store.update_task(row['id'], {'upstream_context': {
        **current['upstream_context'], 'manual_termination': {'reason': 'operator'}}})
    assert not service._canvas_persist_operation_receipt(row['id'], receipt, account)
    assert store.task(row['id']) == stopped


def test_same_clock_progress_update_is_preserved_when_acceptance_is_saved(uncertain, monkeypatch):
    store, service, account, runner, row, receipt = uncertain
    monkeypatch.setattr('app.store.time.time', lambda: row['updated_at'])
    current = store.update_task(row['id'], {'progress': 46})
    assert current['updated_at'] == row['updated_at']
    assert store.remember_canvas_operation_receipt(row['id'], receipt,
        expected_updated_at=row['updated_at'], expected_context=row['upstream_context'],
        expected_account=account)
    saved = store.task(row['id'])
    assert saved['progress'] == 46 and saved['actual_cost'] is None
    assert saved['upstream_context']['operation_receipt']['durable'] is True


@pytest.mark.parametrize('status', ['queued', 'preparing'])
def test_unsubmitted_tasks_cannot_acquire_generation_receipts(uncertain, status):
    store, service, account, runner, row, receipt = uncertain
    before = store.update_task(row['id'], {'status': status})
    assert not service._canvas_persist_operation_receipt(row['id'], receipt, account)
    assert store.task(row['id']) == before


def test_profile_marker_rotation_blocks_late_receipt(uncertain):
    store, service, account, runner, row, receipt = uncertain
    runner.profile = lambda account: 'rotated-profile'
    assert not service._canvas_persist_operation_receipt(row['id'], receipt, account)
    assert store.task(row['id']) == row


def test_refused_persistence_stops_before_resource_get_or_download(uncertain):
    store, service, account, runner, row, _ = uncertain
    with pytest.raises(CanvasCLIError) as error:
        CanvasClient(runner, account).poll(deepcopy(row['upstream_context']),
            deadline=time.monotonic() + 120, persist_operation_receipt=lambda receipt: False)
    assert error.value.code == 'CLI_STOPPING'
    assert runner.count(('resource', 'get')) == runner.count(('resource', 'download')) == 0
    assert store.task(row['id']) == row
