"""Native quotes estimate exact-shape admission; each account's own quote wins."""
import time

import pytest

from test_canvas_execution import context, owner, task
from test_canvas_quote_hints import seed


def multi(store, identifier='multi', **overrides):
    return task(store, identifier, image_urls=['https://fixture.test/1.png', 'https://fixture.test/2.png'],
                audio_urls=['https://fixture.test/1.mp3'], **overrides)


def choose(service, row, estimate=0):
    selected = service._reserve_account(estimate, task_id=row['id'])
    if selected:
        service._release_account(selected['id'], row['id'])
    return selected


def test_unknown_multi_reference_quote_prefers_larger_balance_despite_positive_history(context):
    store, service, _ = context
    owner(store, 'small', balance=120)
    big = owner(store, 'big', balance=1000)
    row = multi(store)
    assert choose(service, row, 100)['id'] == big['id']
    assert not store.canvas_quote_hints()


def test_exact_native_request_quote_filters_unknown_account_before_preparation(context):
    store, service, _ = context
    known = owner(store, 'known-underfunded', balance=100)
    unknown = owner(store, 'unknown-plan', balance=40)
    row = multi(store)
    seed(store, service, known, 200, row['normalized'])
    # No task quote exists yet. The current eligible account's exact request
    # quote supplies a conservative estimate, so neither known balance fits.
    assert choose(service, row) is None
    assert not service._credit_reservations and not service._active
    # Unknown pricing is not an account disable. Once it has enough available
    # credits, it can prepare and obtain its own authoritative native quote.
    store.update_account(unknown['id'], {'balance': 250})
    selected = choose(service, row)
    assert selected['id'] == unknown['id']
    assert selected['_scheduling_quote_amount'] == 200
    assert selected['_credit_reservation_amount'] == 200
    assert store.account(known['id'])['enabled']


def test_correct_positive_hints_keep_smallest_funded_balance_for_material_mix(context):
    store, service, _ = context
    large = owner(store, 'large', balance=200)
    small = owner(store, 'small', balance=42)
    row = multi(store)
    for account in (large, small):
        seed(store, service, account, 40, row['normalized'])
    selected = choose(service, row, 100)
    assert selected['id'] == small['id'] and selected['_scheduling_quote_amount'] == 40


def test_funded_exact_quote_is_used_before_unknown_same_priority_probe(context):
    store, service, _ = context
    known = owner(store, 'quoted', balance=42)
    owner(store, 'unknown-larger', balance=200)
    row = multi(store)
    seed(store, service, known, 40, row['normalized'])
    assert choose(service, row)['id'] == known['id']


def test_unknown_lower_priority_never_bypasses_funded_higher_priority(context):
    store, service, _ = context
    high = owner(store, 'high', balance=42, priority=9)
    owner(store, 'low', balance=1000, priority=8)
    row = multi(store)
    seed(store, service, high, 40, row['normalized'])
    assert choose(service, row)['id'] == high['id']


def test_all_current_insufficient_quotes_do_not_fall_back_to_live_upload_probes(context):
    store, service, _ = context
    large = owner(store, 'large', balance=42)
    small = owner(store, 'small', balance=41)
    row = multi(store)
    for account in (large, small):
        seed(store, service, account, 100, row['normalized'])
    before = store.task(row['id'])
    assert choose(service, row, 150) is None
    assert not service._credit_reservations and not service._active
    assert store.task(row['id']) == before
    assert all(store.account(account['id'])['enabled'] for account in (large, small))


@pytest.mark.parametrize('change', ['counts', 'duration', 'epoch', 'expired', 'video_duration'])
def test_stale_or_different_material_dimensions_do_not_supply_a_price(context, change):
    store, service, _ = context
    small = owner(store, 'small', balance=42)
    large = owner(store, 'large', balance=200)
    row = multi(store, **({'video_urls': ['https://fixture.test/1.mp4']} if change == 'video_duration' else {}))
    if change == 'video_duration':
        row = store.update_task(row['id'], {'normalized': {**row['normalized'],
            '_cost_evidence': {'reference_seconds': 2.0}}})
    body = dict(row['normalized'])
    if change == 'counts':
        body['images'] = body['images'][:1]
    if change == 'duration':
        body['duration'] += 1
    if change == 'video_duration':
        body['_cost_evidence'] = {'reference_seconds': 3.0}
    hint = seed(store, service, small, 40, body)
    if change == 'epoch':
        from test_canvas_quote_hints import epoch
        epoch(service, small, 'changed-profile')
    if change == 'expired':
        hint['observed_at'] = time.time() - 7200
        store.save_canvas_quote_hint(hint)
    assert choose(service, row, 10)['id'] == large['id']


def test_unknown_holds_busy_accounts_and_missing_proxy_remain_ineligible(context):
    store, service, _ = context
    held = owner(store, 'held', balance=1000)
    no_proxy = owner(store, 'no-proxy', balance=2000)
    busy = owner(store, 'busy', balance=3000)
    available = owner(store, 'available', balance=40)
    store.update_account(no_proxy['id'], {'proxy_url': ''})
    service._credit_reservations['unknown'] = (held['id'], 0)
    service._active[busy['id']] = 3
    row = multi(store)
    assert choose(service, row)['id'] == available['id']
    service._credit_reservations.clear()
    service._active.clear()


@pytest.mark.parametrize('mode', ['canvas_cli', 'browser', 'jimeng_web'])
def test_single_reference_estimate_and_non_canvas_keep_balance_order(context, mode):
    store, service, _ = context
    small = owner(store, 'small', balance=42, mode=mode)
    owner(store, 'large', balance=200, mode=mode)
    if mode == 'canvas_cli':
        row = task(store, 'single', image_urls=['https://fixture.test/1.png'])
    else:
        row = multi(store)
    if mode == 'jimeng_web':
        # Site capability/region admission is independently tested; this case
        # isolates ordering after those gates have accepted the request.
        service._jimeng_capability_denial = lambda *_: None
        service._region_denial = lambda *_: None
    selected = choose(service, row, 10)
    assert selected['id'] == small['id']
    if mode == 'canvas_cli':
        assert selected['_scheduling_quote_unknown'] is False
        assert selected['_scheduling_quote_amount'] == selected['_credit_reservation_amount'] == 10
    else:
        assert '_scheduling_quote_unknown' not in selected


@pytest.mark.parametrize('key,default,maximum', [
    ('canvas_upload_concurrency', 3, 8),
    ('canvas_account_upload_concurrency', 4, 16),
    ('canvas_total_upload_concurrency', 8, 32),
])
def test_upload_concurrency_settings_defaults_bounds_and_atomic_rejection(context, key, default, maximum):
    store, service, _ = context
    assert service.public_settings()[key] == default
    for valid in (1, maximum):
        assert service.update_settings({key: valid})[key] == valid
    for invalid in (0, maximum + 1, True, 1.0, '2', None, -1):
        before = store.get_settings()
        with pytest.raises(ValueError, match=key):
            service.update_settings({key: invalid, 'task_workers': 7})
        assert store.get_settings() == before
