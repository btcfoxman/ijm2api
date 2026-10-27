"""Credit budgets use recent isolated successes and never replay a generation."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import Mock, patch

import pytest

from app.browser import BrowserError
from app.catalog import normalize_request
from app.service import Service
from app.store import Store


@pytest.fixture
def context(tmp_path):
    store = Store(str(tmp_path / 'tasks.db'))
    service = Service(store)
    yield store, service
    service.stop()


def request(**values):
    return {'model': 'sd-2-5-480p', 'duration': 4, 'prompt': 'A synthetic landscape', **values}


def account(store, name, balance, priority=0, concurrency=3):
    value = store.upsert_account({'name': name, 'proxy_url': 'socks5://proxy.test:20002',
                                 'priority': priority, 'max_concurrency': concurrency})
    return store.update_account(value['id'], {'status': 'active', 'balance': balance})


def history(store, identifier, cost, *, body=None, age=0, isolated=True, status='succeeded', after=None):
    body = body or request()
    normalized = normalize_request(body)
    reference = body.get('_reference_seconds')
    normalized['_cost_evidence'] = {'balance_isolated': isolated, 'reference_seconds': reference or 0}
    store.create_task(identifier, body, normalized)
    return store.update_task(identifier, {'status': status, 'actual_cost': cost, 'balance_before': 1000,
        'balance_after': 1000 - cost if after is None else after, 'completed_at': time.time() - age})


def estimate(store, service, normalized=None, fallback=13):
    return service._cost_estimate(normalized or normalize_request(request()), fallback, store.scheduling_rows())


def test_recent_five_samples_take_upper_bound_and_old_rates_expire(context):
    store, service = context
    for i, cost in enumerate((68, 76, 70, 72, 74, 200)):
        history(store, f'history-{i}', cost, age=i + 1)
    history(store, 'expired-high-cost', 900, age=8 * 86400)
    cost, source = estimate(store, service)
    assert cost == 76 and source == {'source': 'recent_actual_exact', 'sample_count': 5, 'window_days': 7}


@pytest.mark.parametrize('kind', ['failed', 'unisolated', 'zero', 'mismatched_balance', 'estimate_only', 'bad_json', 'bad_evidence'])
def test_untrusted_costs_never_become_an_actual_history_budget(context, kind):
    store, service = context
    row = history(store, 'untrusted', 20, isolated=kind != 'unisolated',
                  status='failed' if kind == 'failed' else 'succeeded', after=999 if kind == 'mismatched_balance' else None)
    if kind == 'zero':
        store.update_task(row['id'], {'actual_cost': 0, 'balance_after': 1000})
    if kind == 'estimate_only':
        store.update_task(row['id'], {'actual_cost': None, 'estimated_cost': 500})
    if kind in {'bad_json', 'bad_evidence'}:
        normalized = [] if kind == 'bad_json' else {**normalize_request(request()), '_cost_evidence': []}
        with store._connect() as database:
            import json
            database.execute('UPDATE tasks SET normalized_json=? WHERE id=?', (json.dumps(normalized), row['id']))
    cost, source = estimate(store, service)
    assert cost == 13 and source['source'] == 'legacy_estimate'


def test_material_count_fallback_keeps_reference_seconds_independent(context):
    store, service = context
    history(store, 'one-image', 76, body=request(image_urls=['https://source.test/one.png']))
    target = normalize_request(request(image_urls=['https://source.test/image.png'] * 9,
                                       audio_urls=['https://source.test/audio.mp3'] * 3))
    assert estimate(store, service, target)[0:1] == (76,)
    assert estimate(store, service, target)[1]['source'] == 'recent_actual_dimensions'
    reference = request(video_urls=['https://source.test/video.mp4'], _reference_seconds=4.2)
    history(store, 'measured-reference', 120, body=reference)
    target = normalize_request(reference)
    assert estimate(store, service, target)[0] == 13  # No request-side duration guess.
    target['_cost_evidence'] = {'reference_seconds': 4.2}
    assert estimate(store, service, target)[0] == 120
    target['_cost_evidence']['reference_seconds'] = 8.4
    assert estimate(store, service, target)[0] == 13


def test_best_fit_preserves_priority_and_puts_unknown_balance_last(context):
    store, service = context
    small = account(store, 'small', 20, 2)
    account(store, 'larger', 80, 2)
    account(store, 'unknown', None, 2)
    account(store, 'lower-priority', 15, 1)
    chosen = service._reserve_account(15, task_id='first')
    assert chosen['id'] == small['id']
    assert service._reserve_account(15, task_id='second')['name'] == 'larger'


def test_unknown_cost_probes_larger_balance_instead_of_treating_zero_as_free(context):
    store, service = context
    account(store, 'almost-empty', 1)
    large = account(store, 'large', 100)
    account(store, 'empty', 0)
    account(store, 'unknown', None)
    assert service._reserve_account(0)['id'] == large['id']
    assert estimate(store, service, fallback=0) == (0, {'source': 'unknown', 'sample_count': 0})


def test_best_fit_uses_balance_after_existing_holds(context):
    store, service = context
    owner = account(store, 'partly-reserved', 100)
    service._credit_reservations['old-reservation'] = (owner['id'], 70)
    account(store, 'unreserved', 40)
    assert service._reserve_account(20, task_id='new')['id'] == owner['id']


def test_parallel_reservations_cannot_overbook_the_same_credits(context):
    store, service = context
    account(store, 'owner', 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda task_id: service._reserve_account(60, task_id=task_id), ('one', 'two')))
    assert sum(value is not None for value in results) == 1
    assert sum(amount for _, amount in service._credit_reservations.values()) == 60


def test_unknown_balance_has_only_one_read_only_probe_until_refreshed(context):
    store, service = context
    owner = account(store, 'unknown', None)
    assert service._reserve_account(50, task_id='probe') is not None
    assert service._reserve_account(40, task_id='parallel-probe') is None
    store.update_account(owner['id'], {'balance': 100})
    assert service._reserve_account(40, task_id='funded-after-refresh') is not None


def test_inflight_and_memory_reservations_are_merged_once_and_released(context):
    store, service = context
    owner = account(store, 'owner', 100)
    assert service._reserve_account(70, task_id='in-flight')['id'] == owner['id']
    body = request()
    store.create_task('in-flight', body, normalize_request(body))
    store.update_task('in-flight', {'account_id': owner['id'], 'status': 'running', 'estimated_cost': 70})
    assert service._reserve_account(40, task_id='too-much') is None
    assert service._reserve_account(30, task_id='fits-exactly')['id'] == owner['id']
    service._release_account(owner['id'], 'fits-exactly')
    service._release_account(owner['id'], 'in-flight')
    assert service._reserve_account(40, task_id='still-persisted') is None
    store.update_task('in-flight', {'status': 'succeeded'})
    assert service._reserve_account(100, task_id='released')['id'] == owner['id']


def test_restart_and_unknown_submit_keep_persistent_budget_without_replaying(context):
    store, service = context
    owner = account(store, 'owner', 100)
    body = request()
    store.create_task('unknown', body, normalize_request(body))
    store.update_task('unknown', {'account_id': owner['id'], 'status': 'failed', 'estimated_cost': 70,
                                 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'})
    assert service._reserve_account(40) is None
    assert not service._has_eligible_account(40, set())
    assert store.task('unknown')['status'] == 'failed'


def test_unknown_live_and_cached_balance_never_reaches_generation(context):
    store, service = context
    owner = account(store, 'unknown-balance', None)
    body = request()
    store.create_task('new-task', body, normalize_request(body))
    client = Mock()
    client.account_state.side_effect = BrowserError('synthetic credit read failed')
    with patch.object(service.browser_runtime, 'acquire', side_effect=lambda value: (value, False)), \
         patch.object(service.browser_runtime, 'release'), patch('app.service.DreaminaClient', return_value=client):
        service._run('new-task')
    client.submit.assert_not_called()
    client.upload_inputs.assert_not_called()
    assert store.account(owner['id'])['enabled']
    assert store.task('new-task')['status'] == 'failed'
    assert not service._credit_reservations


def test_uploaded_reference_reestimation_switches_before_any_paid_submit(context):
    store, service = context
    body = request(video_urls=['https://source.test/video.mp4'])
    history(store, 'reference-cost', 40, body={**body, '_reference_seconds': 4})
    first, second = account(store, 'first', 30), account(store, 'second', 60)
    task = store.create_task('new-task', body, normalize_request(body))
    store.update_task(task['id'], {'estimated_cost': 13})
    original = deepcopy(task['request'])
    clients = {}
    for owner in (first, second):
        client = Mock(account=owner)
        client.account_state.return_value = {'available_balance': owner['balance']}
        client.upload_inputs.return_value = {'images': [], 'videos': [{'duration': 4}], 'audios': []}
        client.submit.return_value = {'run_id': 'accepted-run', 'thread_id': 'accepted-history'}
        client.poll.return_value = {'status': 'succeeded', 'result_urls': ['https://result.test/video.mp4']}
        clients[owner['id']] = client
    with patch.object(service.browser_runtime, 'acquire', side_effect=lambda value: (value, False)), \
         patch.object(service.browser_runtime, 'release'), \
         patch('app.service.DreaminaClient', side_effect=lambda value, **kwargs: clients[value['id']]):
        service._run(task['id'])
    result = store.task(task['id'])
    assert result['status'] == 'succeeded' and result['estimated_cost'] == 40
    assert result['account_id'] == second['id'] and result['attempted_account_ids'] == [second['id']]
    clients[first['id']].submit.assert_not_called()
    clients[second['id']].submit.assert_called_once()
    assert result['request'] == original and not service._credit_reservations


def test_estimation_failure_releases_worker_slot_and_records_failure(context):
    store, service = context
    body = request()
    store.create_task('new-task', body, normalize_request(body))
    before = service._worker_slots._value
    with patch.object(service, '_update_cost_estimate', side_effect=RuntimeError('synthetic history failure')):
        service._run('new-task')
    assert service._worker_slots._value == before
    assert store.task('new-task')['status'] == 'failed'
