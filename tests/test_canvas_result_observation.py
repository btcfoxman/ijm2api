"""Accepted outputs are automatically observed; paid submissions never repeat."""
from copy import deepcopy
import json
import threading
import time
from unittest.mock import Mock, patch
import uuid

import pytest

from app.canvas_cli import CanvasCLIError
from app.deploy_guard import active_work
from app.service import Service
from app.store import accepted_canvas_result
from test_canvas_execution import context, owner, fake_client, task


def accepted(store, service, identifier='accepted', *, failed=True, account=None):
    account = account or owner(store, identifier)
    task(store, identifier)
    resource = str(uuid.uuid4())
    bound = {'backend': 'canvas_cli', 'account_id': account['id'], 'task_id': identifier,
        'profile': service.canvas_cli.profile(account), 'project_id': 'original-project',
        'node_id': 'original-node-' + identifier, 'submit_id': 'original-submit-' + identifier,
        'expected_user_id': account['login_user_id'], 'kind': 'video', 'output_count': 1,
        'resource_ids': [resource], 'phase': 'accepted', 'submission_phase': 'accepted',
        'observation_deadline': time.time() - 10, 'financial_outcome': 'unconfirmed',
        'reconciliation_closed': failed, 'reconciliation_close_reason': 'observation_deadline_reached',
        'submission_evidence': {'version': 1, 'accepted': True, 'output_resources': True,
            'receipt': False, 'identity_conflict': False, 'ambiguous_partial': True},
        'preparation_timing': {'commands': {'node run': {'count': 1, 'failed': 0}}}}
    row = store.update_task(identifier, {'account_id': account['id'], 'execution_backend': 'canvas_cli',
        'status': 'failed' if failed else 'running', 'error_code': 'CLI_SUBMISSION_UNCONFIRMED' if failed else '',
        'error_message': 'original bounded lookup failure' if failed else '',
        'completed_at': time.time() if failed else None, 'estimated_cost': 20, 'actual_cost': None,
        'balance_before': 100, 'balance_after': None, 'upstream_context': bound})
    return row, account


def test_old_accepted_timeout_CAS_retains_finance_failure_binding_and_original_deadline(context):
    store, service, _ = context
    before, _ = accepted(store, service)
    after = store.queue_canvas_result_observation(before['id'], before['updated_at'])
    assert after['status'] == 'running' and after['completed_at'] is None
    assert after['error_code'] == after['error_message'] == ''
    audit = after['upstream_context']['result_observation']
    assert audit['preserve_finance'] is True and audit['attempts'] == 0
    assert 'deadline' not in audit
    for key in ('request', 'normalized', 'estimated_cost', 'actual_cost', 'balance_before', 'balance_after'):
        assert after[key] == before[key]
    for key in ('resource_ids', 'profile', 'node_id', 'project_id', 'submit_id', 'preparation_timing',
                'observation_deadline', 'financial_outcome', 'submission_evidence'):
        assert after['upstream_context'][key] == before['upstream_context'][key]
    for key in ('status', 'error_code', 'error_message', 'completed_at', 'estimated_cost',
                'actual_cost', 'balance_before', 'balance_after'):
        assert audit['original_failure'][key] == before[key]
    assert store.queue_canvas_result_observation(before['id'], before['updated_at']) == {}
    empty_proc = store.path.parent / 'empty-proc'; empty_proc.mkdir()
    assert active_work(store.path, empty_proc) == 1


@pytest.mark.parametrize('change', [
    {'error_code': 'UPSTREAM_FAILED'}, {'status': 'cancelled'}, {'result_urls': ['already-delivered']},
    {'upstream_context': {'manual_termination': {}}},
    {'upstream_context': {'submit_error': {'code': 'cli.transport_failed'}}},
    {'upstream_context': {'phase': 'rejected'}}, {'upstream_context': {'resource_ids': []}},
    {'upstream_context': {'resource_ids': ['a', 'a']}},
    {'upstream_context': {'expected_user_id': ''}},
    {'upstream_context': {'reconciliation_close_reason': 'operation_submission_absent'}},
    {'upstream_context': {'submission_evidence': {'version': 1, 'accepted': False}}},
    {'upstream_context': {'preparation_timing': {'commands': {'node run': {'count': 2, 'failed': 0}}}}},
    {'upstream_context': {'preparation_timing': {'commands': {'node run': {'count': 1, 'failed': 1}}}}},
    {'upstream_context': {'preparation_timing': []}},
    {'normalized': {'_execution': {'profile': 'another-profile'}}},
])
def test_legacy_recovery_never_revives_unproven_conflicting_or_manual_terminal(context, change):
    store, service, _ = context
    row, _ = accepted(store, service)
    update = deepcopy(change)
    if 'upstream_context' in update:
        update['upstream_context'] = {**row['upstream_context'], **update['upstream_context']}
    if 'normalized' in update:
        # This immutable caller field is deliberately not a Store.update_task input.
        with store._connect() as db:
            db.execute('UPDATE tasks SET normalized_json=? WHERE id=?',
                       (json.dumps({**row['normalized'], **update.pop('normalized')}), row['id']))
    row = store.update_task(row['id'], update)
    assert not accepted_canvas_result(row, allow_closed=True)
    assert store.queue_canvas_result_observation(row['id'], row['updated_at']) == {}


def test_accepted_main_worker_deadline_defers_automatically_without_submission_unknown(context):
    store, service, _ = context
    row, account = accepted(store, service, failed=False)
    client = fake_client(store, service, account)
    service._poll_canvas(row['id'], client, time.monotonic() - 1)
    result = store.task(row['id'])
    assert result['status'] == 'running' and result['error_code'] == '' and result['completed_at'] is None
    assert result['upstream_context']['result_observation']['preserve_finance'] is False
    client.poll.assert_not_called(); client.prepare.assert_not_called(); client.submit.assert_not_called()


def test_observer_delivers_original_result_once_and_does_not_settle_historical_finance(context):
    store, service, stored = context
    row, account = accepted(store, service)
    client = fake_client(store, service, account)
    client.poll.return_value['result_files'][0]['resource_id'] = row['upstream_context']['resource_ids'][0]
    service.canvas_client = Mock(return_value=client)
    service._canvas_result_pool.submit = Mock()
    service._schedule_canvas_results()
    service._schedule_canvas_results()
    assert service._canvas_result_pool.submit.call_count == 1
    function, claimed, owned = service._canvas_result_pool.submit.call_args.args
    before = store.task(row['id'])
    assert before['upstream_context']['result_observation']['attempts'] == 1
    assert not service._active and service._worker_slots._value == 50
    function(claimed, owned)
    result = store.task(row['id'])
    assert result['status'] == 'succeeded' and len(result['result_urls']) == 1
    assert service.public_task(result)['status'] == 'succeeded'
    assert result['result_transfer']['original_bytes'] is True
    source, = result['result_transfer']['source_files']
    assert source['resource_id'] == before['upstream_context']['resource_ids'][0]
    assert source['size_bytes'] == len(b'synthetic-original-video-bytes')
    assert len(source['sha256']) == 64 and source['mime'] == 'video/mp4'
    stored.assert_called_once(); client.poll.assert_called_once(); client.identity.assert_called_once()
    client.prepare.assert_not_called(); client.submit.assert_not_called()
    assert not service._canvas_result_inflight
    for key in ('estimated_cost', 'actual_cost', 'balance_before', 'balance_after', 'request', 'normalized'):
        assert result[key] == before[key]


def test_pending_observer_is_one_bounded_read_with_durable_backoff_and_restart_dedup(context):
    store, service, _ = context
    row, account = accepted(store, service)
    client = fake_client(store, service, account)
    client.poll.return_value = {'status': 'running', 'progress': 50}
    service.canvas_client = Mock(return_value=client)
    service._canvas_result_pool.submit = Mock()
    service._schedule_canvas_results()
    function, claimed, owned = service._canvas_result_pool.submit.call_args.args
    function(claimed, owned)
    current = store.task(row['id']); record = current['upstream_context']['result_observation']
    assert current['status'] == 'running' and current['error_code'] == ''
    assert record['attempts'] == 1 and 29 <= record['next_poll_at'] - time.time() <= 31
    assert client.poll.call_count == 1 and not service._active and service._worker_slots._value == 50
    restarted = Service(store)
    try:
        restarted._canvas_result_pool.submit = Mock()
        restarted._schedule_canvas_results()
        restarted._canvas_result_pool.submit.assert_not_called()
        assert store.task(row['id'])['upstream_context']['result_observation'] == record
    finally:
        restarted.stop()


def test_query_identity_conflict_blocks_only_result_observer_without_paid_replay(context):
    store, service, _ = context
    row, account = accepted(store, service)
    client = fake_client(store, service, account)
    client.identity.side_effect = None
    client.identity.return_value = {'user_id': 'another-account'}
    service.canvas_client = Mock(return_value=client); service._canvas_result_pool.submit = Mock()
    service._schedule_canvas_results()
    function, claimed, owned = service._canvas_result_pool.submit.call_args.args
    function(claimed, owned)
    current = store.task(row['id'])
    assert current['status'] == 'running' and current['error_code'] == ''
    assert current['upstream_context']['result_observation']['blocked_reason'] == 'CLI_ACCOUNT_MISMATCH'
    client.poll.assert_not_called(); client.prepare.assert_not_called(); client.submit.assert_not_called()
    service._schedule_canvas_results()
    assert service._canvas_result_pool.submit.call_count == 1


def test_observer_rejects_success_without_exact_original_resource_receipt(context):
    store, service, stored = context
    row, account = accepted(store, service)
    client = fake_client(store, service, account)
    client.poll.return_value = {'status': 'succeeded', 'result_urls': ['https://unbound.test/result.mp4']}
    service.canvas_client = Mock(return_value=client); service._canvas_result_pool.submit = Mock()
    service._schedule_canvas_results()
    function, claimed, owned = service._canvas_result_pool.submit.call_args.args
    function(claimed, owned)
    result = store.task(row['id'])
    assert result['status'] == 'running' and not result['result_urls']
    assert result['upstream_context']['result_observation']['blocked_reason'] == 'CLI_RESULT_INVALID'
    stored.assert_not_called(); client.submit.assert_not_called()


def test_stop_and_manual_terminal_win_over_inflight_observer(context):
    store, service, stored = context
    row, account = accepted(store, service)
    client = fake_client(store, service, account)
    returned = deepcopy(client.poll.return_value)
    def stopped(*args, **kwargs):
        value = store.task(row['id'])
        store.update_task(row['id'], {'status': 'failed', 'error_code': 'OPERATOR_STOP',
            'upstream_context': {**value['upstream_context'], 'manual_termination': {}}})
        return returned
    client.poll.side_effect = stopped
    service.canvas_client = Mock(return_value=client); service._canvas_result_pool.submit = Mock()
    service._schedule_canvas_results()
    function, claimed, owned = service._canvas_result_pool.submit.call_args.args
    function(claimed, owned)
    assert store.task(row['id'])['error_code'] == 'OPERATOR_STOP'
    stored.assert_not_called(); assert not service._canvas_result_inflight
    service._stop.set(); service._schedule_canvas_results()
    assert service._canvas_result_pool.submit.call_count == 1


def test_result_dispatcher_has_two_physical_workers_and_no_generation_slot_ownership(context):
    store, service, _ = context
    entered, release = threading.Event(), threading.Event()
    count, lock = [], threading.Lock()
    clients = {}
    for index in range(3):
        row, account = accepted(store, service, 'task-' + str(index))
        client = fake_client(store, service, account)
        def poll(*args, **kwargs):
            with lock:
                count.append(1)
                if len(count) == 2:
                    entered.set()
            assert release.wait(3)
            return {'status': 'running', 'progress': 50}
        client.poll.side_effect = poll; clients[account['id']] = client
    service.canvas_client = lambda account: clients[account['id']]
    try:
        service._schedule_canvas_results()
        assert entered.wait(3)
        service._schedule_canvas_results()
        assert len(service._canvas_result_inflight) == len(count) == 2
        assert not service._active and service._worker_slots._value == 50
        assert store.task('task-2')['status'] == 'failed'
    finally:
        release.set()
    limit = time.monotonic() + 3
    while service._canvas_result_inflight and time.monotonic() < limit:
        time.sleep(.01)
    assert not service._canvas_result_inflight


def test_start_routes_legacy_result_to_query_dispatcher_and_never_generation_pool(context):
    store, service, _ = context
    identifiers = ['legacy-' + str(index) for index in range(5)]
    for identifier in identifiers:
        accepted(store, service, identifier)
    with patch.object(service.canvas_cli, 'verify_runtime', return_value={'available': True}), \
            patch('app.service.threading.Thread') as thread:
        service.start()
    service._pool.submit.assert_not_called()
    assert thread.call_args.kwargs['target'] == service._canvas_result_loop
    assert thread.return_value.start.call_count == 2
    assert all(store.task(identifier)['status'] == 'running' and
        store.task(identifier)['upstream_context']['result_observation']['attempts'] == 0
        for identifier in identifiers)
    service._canvas_result_pool.submit = Mock()
    service._schedule_canvas_results()
    assert service._canvas_result_pool.submit.call_count == 2
    assert len(service._canvas_result_inflight) == 2


def test_identity_and_observation_share_original_120_second_deadline(context):
    store, service, _ = context
    row, account = accepted(store, service)
    row = store.queue_canvas_result_observation(row['id'], row['updated_at'])
    client = fake_client(store, service, account)
    client.poll.return_value = {'status': 'running', 'progress': 50}
    deadline = time.monotonic() + 11
    service._poll_canvas(row['id'], client, deadline, one_shot=True, result_observer=True)
    assert client.poll.call_args.kwargs['deadline'] <= deadline

