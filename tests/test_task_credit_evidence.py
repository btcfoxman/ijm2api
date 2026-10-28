"""Task-scoped billing evidence and temporary reservation queue regressions."""
import time
from unittest.mock import Mock, patch

import pytest

from app.catalog import normalize_request
from app.client import history_task_credits
from app.cost_adapter import build_observations
from app.service import Service
from app.store import Store


def evidence(amount=40, phase='succeeded', submit_id='run'):
    return {'amount': amount, 'field': 'item_list[].extra.credits_consume',
            'submit_id': submit_id, 'phase': phase}


def record(*amounts, submit_id='run'):
    return {'task': {'submit_id': submit_id},
            'item_list': [{'extra': {'credits_consume': value}} for value in amounts]}


@pytest.mark.parametrize('amount', [0, 40, '40.5'])
def test_explicit_nonnegative_task_debit_is_preserved_and_duplicate_views_are_not_summed(amount):
    value = history_task_credits(record(amount, amount), 'run', 'succeeded')
    assert value == evidence(float(amount))


@pytest.mark.parametrize('amount', [False, True, -1, 'NaN', 'Infinity', None, 'unknown'])
def test_invalid_reported_credit_is_unknown(amount):
    assert history_task_credits(record(amount), 'run', 'succeeded') is None


def test_conflicting_tasks_input_items_and_forecast_durations_are_not_cost():
    assert history_task_credits(record(40, 50), 'run', 'succeeded') is None
    assert history_task_credits(record(40, submit_id='other-run'), 'run', 'succeeded') is None
    value = record()
    value.update(origin_item_list=[{'extra': {'credits_consume': 900}}],
                 forecast_generate_cost=400, forecast_queue_cost=800)
    assert history_task_credits(value, 'run', 'running') is None


@pytest.fixture
def context(tmp_path):
    store = Store(str(tmp_path / 'tasks.db'))
    service = Service(store)
    owner = store.upsert_account({'name': 'synthetic', 'proxy_url': 'socks5://proxy.test:20001',
                                  'max_concurrency': 3})
    owner = store.update_account(owner['id'], {'status': 'active', 'balance': 1000})
    yield store, service, owner
    service.stop()


def create(store, identifier='task', **body):
    request = {'model': 'sd-2-0-mini', 'duration': 15, 'prompt': 'Synthetic landscape', **body}
    return store.create_task(identifier, request, normalize_request(request))


@pytest.mark.parametrize('reported', [0, 40])
def test_success_prefers_official_task_credit_to_nonisolated_balance_delta(context, reported):
    store, service, owner = context
    task = create(store)
    store.update_task(task['id'], {'status': 'running', 'account_id': owner['id'],
                                  'run_id': 'run', 'thread_id': 'thread', 'balance_before': 1000})
    client = Mock(account=owner)
    client.poll.return_value = {'status': 'succeeded', 'result_urls': ['https://result.test/video.mp4'],
                                'task_credit_evidence': evidence(reported)}
    client.account_state.return_value = {'available_balance': 100}
    with patch.object(store, 'has_account_overlap', return_value=True):
        service._poll(task['id'], client, time.monotonic() + 3)
    result = store.task(task['id'])
    assert result['actual_cost'] == reported
    assert result['normalized']['_cost_evidence']['balance_isolated'] is False
    observation = build_observations(result)[0]
    assert observation['charged'] == reported and observation['source'] == 'actual'
    client.submit.assert_not_called()


def test_running_official_task_cost_updates_persistent_and_memory_budget(context):
    store, service, owner = context
    task = create(store)
    store.update_task(task['id'], {'status': 'running', 'account_id': owner['id'],
                                  'run_id': 'run', 'thread_id': 'thread', 'balance_before': 1000})
    service._active[owner['id']] = 1
    service._credit_reservations[task['id']] = (owner['id'], 0)
    assert service._reserve_account(10, task_id='other') is None
    client = Mock(account=owner)
    client.poll.side_effect = [
        {'status': 'running', 'result_urls': [], 'task_credit_evidence': evidence(40, 'running')},
        {'status': 'succeeded', 'result_urls': ['https://result.test/video.mp4'],
         'task_credit_evidence': evidence(40)},
    ]
    client.account_state.return_value = {'available_balance': 960}

    def release_probe(_seconds):
        assert store.task(task['id'])['estimated_cost'] == 40
        assert service._credit_reservations[task['id']] == (owner['id'], 40)
        assert service._reserve_account(10, task_id='other')['id'] == owner['id']
        service._release_account(owner['id'], 'other')

    with patch('app.service.time.sleep', side_effect=release_probe):
        service._poll(task['id'], client, time.monotonic() + 3)
    assert store.task(task['id'])['actual_cost'] == 40


def test_official_history_is_usable_without_isolated_balance_and_covers_shorter_references(context):
    store, service, _ = context
    task = create(store, 'historical', video_urls=['https://source.test/video.mp4'])
    store.update_task(task['id'], {'status': 'succeeded', 'run_id': 'run', 'actual_cost': 216,
        'balance_before': 1000, 'balance_after': 100, 'completed_at': time.time(),
        'cost_evidence': {'reference_seconds': 12, 'balance_isolated': False,
                          'upstream_task_credits': evidence(216)}})
    target = normalize_request({'model': 'sd-2-0-mini', 'duration': 15, 'prompt': 'Synthetic'})
    cost, source = service._cost_estimate(target, 0, store.scheduling_rows())
    assert cost == 216 and source['source'] == 'recent_actual_covering_dimensions'
    assert source['sample_ids'] == ['historical']
    target['videos'] = ['https://source.test/longer.mp4']
    assert service._cost_estimate(target, 0, store.scheduling_rows())[0] == 0
    target['_cost_evidence'] = {'reference_seconds': 13}
    assert service._cost_estimate(target, 0, store.scheduling_rows())[0] == 0
    store.update_task(task['id'], {'run_id': 'different-run'})
    target['videos'] = []
    assert service._cost_estimate(target, 0, store.scheduling_rows())[0] == 0


def test_temporary_hold_waits_then_submits_once_without_replaying_the_held_task(context):
    store, service, owner = context
    task = create(store)
    store.update_task(task['id'], {'estimated_cost': 10})
    client = Mock(account=owner)
    client.account_state.return_value = {'available_balance': 1000}
    client.upload_inputs.return_value = {'images': [], 'videos': [], 'audios': []}
    client.submit.return_value = {'run_id': 'new-run', 'thread_id': 'new-thread'}
    original_funded = service._submission_funded
    calls = []

    def funded(*args):
        if not calls:
            calls.append(True)
            held = create(store, 'held')
            store.update_task(held['id'], {'account_id': owner['id'], 'status': 'running',
                                          'run_id': 'held-run', 'thread_id': 'held-thread'})
            return False
        return original_funded(*args)

    def finish_hold(_seconds):
        store.update_task('held', {'status': 'succeeded'})
        return False

    with patch.object(service.browser_runtime, 'acquire', side_effect=lambda value: (value, False)), \
         patch.object(service.browser_runtime, 'release'), \
         patch('app.service.DreaminaClient', return_value=client), \
         patch.object(service, '_submission_funded', side_effect=funded), \
         patch.object(service._stop, 'wait', side_effect=finish_hold), \
         patch.object(service, '_poll'):
        service._run(task['id'])
    assert store.task(task['id'])['run_id'] == 'new-run'
    assert store.task('held')['run_id'] == 'held-run'
    client.submit.assert_called_once()


def test_permanently_excluded_account_does_not_hide_another_temporarily_held_account(context):
    store, service, owner = context
    other = store.upsert_account({'name': 'other', 'proxy_url': 'socks5://proxy.test:20002'})
    store.update_account(other['id'], {'status': 'active', 'balance': 1000})
    task = create(store, 'held')
    store.update_task(task['id'], {'status': 'failed', 'account_id': owner['id'],
                                  'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'})
    assert not service._has_eligible_account(10, {other['id']}, 'waiting')
    assert service._has_waiting_credit_account(10, {other['id']}, 'waiting')
    assert store.task(task['id'])['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
