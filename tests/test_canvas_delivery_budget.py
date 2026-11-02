"""Original output delivery owns a durable budget independent of its query."""
from copy import deepcopy
import hashlib
from pathlib import Path
import time
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLIError
from app.service import Service
from app.store import Store
from test_canvas_delivery_integration import delivery, observe, TASK_ID, RESOURCE, VIDEO


@pytest.fixture
def clock(monkeypatch):
    current = {'wall': time.time(), 'mono': time.monotonic()}
    monkeypatch.setattr(time, 'time', lambda: current['wall'])
    monkeypatch.setattr(time, 'monotonic', lambda: current['mono'])
    def advance(seconds):
        current['wall'] += seconds
        current['mono'] += seconds
    return current, advance


def output_binding(row):
    context = row['upstream_context']
    return {'version': 1, 'source': 'operation_status', 'operation_ref': context['submit_id'],
        **{key: context[key] for key in ('account_id', 'task_id', 'profile', 'project_id', 'node_id',
            'submit_id', 'expected_user_id', 'kind', 'resource_ids')}}


def claim_bound(service, *, duration=900):
    row = service.store.task(TASK_ID)
    claimed = service.store.queue_canvas_result_observation(TASK_ID, row['updated_at'], claim=True,
                                                           delivery_timeout_seconds=duration)
    assert claimed
    binding = output_binding(claimed)
    assert service._canvas_persist_output_binding(TASK_ID, binding)
    return claimed['upstream_context']['result_observation']['claim_id'], binding


@pytest.mark.parametrize('outputs', [1, 2])
def test_full_download_over_95_seconds_and_all_outputs_share_fresh_delivery_budget(delivery, clock, monkeypatch, outputs):
    service, native = delivery['service'], delivery['runner']
    now, advance = clock
    resources = [RESOURCE, '22222222-2222-4222-8222-222222222222'][:outputs]
    before = service.store.task(TASK_ID)
    context = deepcopy(before['upstream_context'])
    context.update(resource_ids=resources, output_count=outputs)
    before = service.store.update_task(TASK_ID, {'upstream_context': context})
    original_run = native.run
    deadlines, s3_deadlines = [], []
    query_started = now['wall']
    def slow_native(account, args, **kwargs):
        pair = tuple(args[:2])
        if pair == ('auth', 'account'):
            advance(8)
            return original_run(account, args, **kwargs)
        native.calls.append((list(args), dict(kwargs)))
        assert pair in {('operation', 'status'), ('resource', 'get'), ('resource', 'download')}
        if pair == ('operation', 'status'):
            advance(8)
            return {'ok': True, 'data': {'operationRef': 'original-submit', 'state': 'succeeded',
                'resources': [{'resourceId': resource, 'state': 'succeeded'} for resource in resources]}}
        if pair == ('resource', 'get'):
            advance(8)
            return {'ok': True, 'data': {'resourceId': args[2], 'type': 'video'}}
        attempt = service.store.task(TASK_ID)['upstream_context']['result_delivery_attempt']
        assert attempt['deadline'] - attempt['started_at'] == 900
        assert kwargs['deadline'] - now['mono'] > 180
        deadlines.append(kwargs['deadline'])
        advance(240)
        path = Path(args[args.index('--output') + 1]); path.write_bytes(VIDEO)
        return {'ok': True, 'data': {'resourceId': args[2], 'path': str(path), 'size': len(VIDEO),
                                    'sha256': hashlib.sha256(VIDEO).hexdigest()}}
    native.run = slow_native
    def storage(path, task_id, settings, **kwargs):
        assert task_id == TASK_ID and path.read_bytes() == VIDEO
        s3_deadlines.append(kwargs['deadline'])
        assert kwargs['deadline'] - now['mono'] > 0
        return 'https://results.example.test/' + path.name, {'key': path.name, 'size_bytes': len(VIDEO)}
    monkeypatch.setattr('app.canvas_service.store_original', storage)
    row = observe(service)
    assert row['status'] == 'succeeded', row['error_message']
    assert len(deadlines) == outputs and len(s3_deadlines) == outputs
    assert len(set(deadlines + s3_deadlines)) == 1
    identities = [kwargs for args, kwargs in native.calls if tuple(args[:2]) == ('auth', 'account')]
    assert len(identities) == 2 and identities[-1]['deadline'] == deadlines[0]
    assert identities[-1]['deadline'] > identities[0]['deadline']
    assert row['balance_after'] == 90 and row['actual_cost'] == 10
    attempt = row['upstream_context']['result_delivery_attempt']
    assert attempt['started_at'] == query_started + 16 + 8 * outputs
    assert attempt['deadline'] == attempt['started_at'] + 900
    assert row['upstream_context']['observation_deadline'] == context['observation_deadline']
    assert row['upstream_context']['preparation_timing'] == context['preparation_timing']
    assert row['request'] == before['request']
    assert all(tuple(args[:2]) != ('node', 'run') for args, _ in native.calls)


def test_same_attempt_reentry_restart_and_changed_config_do_not_renew_deadline(delivery, clock):
    service = delivery['service']; now, advance = clock
    claim, binding = claim_bound(service)
    deadline = service._canvas_result_delivery_deadline(TASK_ID, claim, binding)
    first = service.store.task(TASK_ID)['upstream_context']['result_delivery_attempt']
    advance(300)
    service.store.update_settings({'canvas_result_delivery_timeout_seconds': 3600})
    restarted, _ = delivery['restart']()
    assert restarted._canvas_result_delivery_deadline(TASK_ID, claim, binding) == deadline
    assert restarted.store.task(TASK_ID)['upstream_context']['result_delivery_attempt'] == first
    advance(601)
    with pytest.raises(CanvasCLIError, match='delivery attempt deadline passed'):
        restarted._canvas_result_delivery_deadline(TASK_ID, claim, binding)
    assert restarted.store.task(TASK_ID)['upstream_context']['result_delivery_attempt'] == first


def test_observer_lease_prevents_restart_or_second_store_from_reclaiming_during_delivery(delivery, clock):
    service = delivery['service']; now, advance = clock
    claim, binding = claim_bound(service)
    assert service._canvas_result_delivery_deadline(TASK_ID, claim, binding) > now['mono']
    first = service.store.task(TASK_ID)
    recovery = first['upstream_context']['result_observation']
    assert recovery['next_poll_at'] >= recovery['last_started_at'] + 120 + 900
    advance(500)
    second = Store(str(service.store.path))
    current = second.task(TASK_ID)
    assert second.queue_canvas_result_observation(TASK_ID, current['updated_at'], claim=True) == {}
    restarted, _ = delivery['restart']()
    restarted._canvas_result_pool.submit = Mock()
    restarted._schedule_canvas_results()
    restarted._canvas_result_pool.submit.assert_not_called()
    assert restarted.store.task(TASK_ID)['upstream_context']['result_observation'] == recovery


def test_queued_observer_uses_only_remainder_of_claimed_query_window(delivery, clock):
    service, native = delivery['service'], delivery['runner']
    now, advance = clock
    row = service.store.task(TASK_ID)
    query_deadline = now['mono'] + 120
    claimed = service.store.queue_canvas_result_observation(TASK_ID, row['updated_at'], claim=True)
    account = service.store.account(row['account_id'], private=True)
    advance(60)
    service._run_canvas_result_observer(claimed, account)
    assert service.store.task(TASK_ID)['status'] == 'succeeded'
    first_download = next(index for index, (args, _) in enumerate(native.calls)
                          if tuple(args[:2]) == ('resource', 'download'))
    queries = [kwargs for _, kwargs in native.calls[:first_download]]
    assert queries and all(kwargs['deadline'] <= query_deadline for kwargs in queries)
    download, = [kwargs for args, kwargs in native.calls if tuple(args[:2]) == ('resource', 'download')]
    assert download['deadline'] == now['mono'] + 900
    settlement_args, settlement = native.calls[-1]
    assert tuple(settlement_args[:2]) == ('auth', 'account')
    assert settlement['deadline'] == download['deadline']


def test_only_a_new_observer_CAS_claim_can_start_the_next_delivery_attempt(delivery, clock):
    service = delivery['service']; now, advance = clock
    original = service.store.task(TASK_ID)
    claim, binding = claim_bound(service)
    service._canvas_result_delivery_deadline(TASK_ID, claim, binding)
    first = deepcopy(service.store.task(TASK_ID)['upstream_context']['result_delivery_attempt'])
    advance(1051)
    current = service.store.task(TASK_ID)
    fresh = service.store.queue_canvas_result_observation(TASK_ID, current['updated_at'], claim=True)
    assert fresh and fresh['upstream_context']['result_observation']['claim_id'] != claim
    new_claim = fresh['upstream_context']['result_observation']['claim_id']
    before = service.store.task(TASK_ID)
    with pytest.raises(CanvasCLIError, match='claim changed before download'):
        service._canvas_result_delivery_deadline(TASK_ID, claim, binding)
    assert service.store.task(TASK_ID) == before
    service._canvas_result_delivery_deadline(TASK_ID, new_claim, binding)
    after = service.store.task(TASK_ID)
    attempt = after['upstream_context']['result_delivery_attempt']
    assert attempt['ordinal'] == first['ordinal'] + 1 and attempt['started_at'] == now['wall']
    assert attempt['deadline'] == now['wall'] + 900
    assert after['upstream_context']['observation_deadline'] == original['upstream_context']['observation_deadline']


def test_delayed_old_observer_cannot_borrow_new_claim_or_shorten_its_lease(delivery, clock):
    service = delivery['service']; _, advance = clock
    claim, binding = claim_bound(service)
    delayed = service.store.task(TASK_ID)
    account = service.store.account(delayed['account_id'], private=True)
    advance(1051)
    current = service.store.task(TASK_ID)
    fresh = service.store.queue_canvas_result_observation(TASK_ID, current['updated_at'], claim=True)
    assert fresh['upstream_context']['result_observation']['claim_id'] != claim
    service.canvas_client = Mock(side_effect=AssertionError('stale observer queried under a new owner'))
    service._run_canvas_result_observer(delayed, account)
    assert service.store.task(TASK_ID) == fresh
    service.canvas_client.assert_not_called()


def test_finished_attempt_cannot_reenter_after_its_lease_was_released_to_backoff(delivery, clock):
    service = delivery['service']; now, advance = clock
    claim, binding = claim_bound(service)
    service._canvas_result_delivery_deadline(TASK_ID, claim, binding)
    row = service.store.task(TASK_ID)
    context = deepcopy(row['upstream_context'])
    context['result_observation'].update(last_finished_at=now['wall'], next_poll_at=now['wall'] + 30)
    row = service.store.update_task(TASK_ID, {'upstream_context': context})
    with pytest.raises(CanvasCLIError, match='claim changed before download'):
        service._canvas_result_delivery_deadline(TASK_ID, claim, binding)
    assert service.store.task(TASK_ID) == row
    advance(31)
    fresh = service.store.queue_canvas_result_observation(TASK_ID, row['updated_at'], claim=True)
    assert fresh and 'last_finished_at' not in fresh['upstream_context']['result_observation']
    new_claim = fresh['upstream_context']['result_observation']['claim_id']
    service._canvas_result_delivery_deadline(TASK_ID, new_claim, binding)
    assert service.store.task(TASK_ID)['upstream_context']['result_delivery_attempt']['ordinal'] == 2


def test_old_closed_accepted_result_gets_delivery_budget_without_rewriting_original_finance_or_window(delivery, clock):
    service = delivery['service']; now, _ = clock
    row = service.store.task(TASK_ID)
    context = deepcopy(row['upstream_context'])
    context.update(observation_deadline=now['wall'] - 10000, reconciliation_closed=True,
        reconciliation_close_reason='observation_deadline_reached')
    row = service.store.update_task(TASK_ID, {'status': 'failed', 'error_code': 'CLI_SUBMISSION_UNCONFIRMED',
        'error_message': 'old bounded lookup closed', 'actual_cost': 17, 'balance_after': 83,
        'completed_at': now['wall'] - 1000, 'upstream_context': context})
    claim, binding = claim_bound(service)
    assert service._canvas_result_delivery_deadline(TASK_ID, claim, binding) == now['mono'] + 900
    after = service.store.task(TASK_ID)
    assert after['upstream_context']['result_observation']['preserve_finance'] is True
    assert after['upstream_context']['result_observation']['original_failure']['actual_cost'] == 17
    assert after['upstream_context']['observation_deadline'] == context['observation_deadline']
    for key in ('estimated_cost', 'actual_cost', 'balance_before', 'balance_after', 'created_at', 'request', 'normalized'):
        assert after[key] == row[key]


@pytest.mark.parametrize('change', ['stale_row', 'foreign_binding', 'identity_changed', 'manual_stop', 'unpersisted_binding'])
def test_delivery_claim_CAS_and_original_binding_win_before_any_download(delivery, clock, change):
    service = delivery['service']; _, advance = clock
    claim, binding = claim_bound(service)
    before = service.store.task(TASK_ID)
    expected = before['updated_at']
    if change == 'stale_row':
        advance(1)
        service.store.update_task(TASK_ID, {'progress': 61})
    elif change == 'foreign_binding':
        binding = {**binding, 'submit_id': 'foreign-submit'}
    elif change == 'identity_changed':
        service.store.update_account(before['account_id'], {'cli_user_id': 'foreign-user'})
    elif change == 'unpersisted_binding':
        context = deepcopy(before['upstream_context']); context.pop('output_binding')
        service.store.update_task(TASK_ID, {'upstream_context': context})
    else:
        context = deepcopy(before['upstream_context']); context['manual_termination'] = {}
        service.store.update_task(TASK_ID, {'status': 'failed', 'error_code': 'OPERATOR_STOP', 'upstream_context': context})
    current = service.store.task(TASK_ID)
    assert service.store.claim_canvas_result_delivery(TASK_ID, expected,
        expected_binding=binding, observer_claim_id=claim) == {}
    assert service.store.task(TASK_ID) == current
    assert 'result_delivery_attempt' not in current['upstream_context']


@pytest.mark.parametrize('value', [0, 3601, True, 900.5])
def test_delivery_budget_configuration_is_bounded(delivery, value):
    service = delivery['service']
    with pytest.raises(ValueError, match='canvas_result_delivery_timeout_seconds'):
        service.update_settings({'canvas_result_delivery_timeout_seconds': value})
    assert service.settings()['canvas_result_delivery_timeout_seconds'] == 900
