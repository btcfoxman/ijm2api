"""Daily scheduling and quota snapshots; every provider/browser is mocked."""
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.browser import BrowserError, BrowserRejected
from app.canvas_cli import CanvasCLIError
from app.daily_checkin import (AccountMaintenanceBusy, claim_day, cli_credit_details,
    identity_binding, read_state, receipt_quota)
from test_daily_checkin import context


def fresh_quota(gift=10, vip=20):
    return {'available_balance': gift+vip, 'buckets': {
        'gift_credit': gift, 'vip_credit': vip, 'purchase_credit': 0}}


def test_confirmed_receipt_immediately_refreshes_authoritative_web_quota(context):
    store, service, account, automation, client, _ = context
    client.account_state.return_value = fresh_quota()
    result = service.checkin_account(account['id'])
    row = store.account(account['id'])
    assert result['received'] == 10 and result['quota_refreshed'] is True
    assert row['last_checkin_date'] == claim_day() and row['checkin']['confirmed_today']
    assert row['balance'] == 30 and row['balance_details']['gift_credit'] == 10
    assert row['balance_details']['_meta']['source'] == 'dreamina_web'
    client.daily_checkin.assert_called_once()
    client.account_state.assert_called_once_with(max_attempts=2, timeout_seconds=6)
    assert automation.read_identity.call_count == 4  # Before/after SDK readiness, receipt, quota.
    assert 'checkin_state_json' not in row


@pytest.mark.parametrize('receipt', [{}, {'receive_quota': None}, {'receive_quota': True},
    {'receive_quota': -1}, {'receive_quota': 'nan'}, {'receive_quota': 'invalid'}])
def test_invalid_receipt_never_marks_the_day_or_changes_credits(context, receipt):
    store, service, account, _, client, _ = context
    client.daily_checkin.return_value = receipt
    with pytest.raises(BrowserError):
        service.checkin_account(account['id'])
    row = store.account(account['id'])
    assert row['last_checkin_date'] == '' and row['balance'] == 100
    assert row['checkin']['state'] == 'retry'
    client.account_state.assert_not_called()


def test_zero_is_only_accepted_from_an_explicit_successful_typed_receipt(context):
    store, service, account, _, client, _ = context
    client.daily_checkin.return_value = {'receive_quota': '0'}
    client.account_state.return_value = fresh_quota(gift=0)
    assert service.checkin_account(account['id'])['received'] == 0
    assert store.account(account['id'])['last_checkin_date'] == claim_day()
    assert store.account(account['id'])['balance_details']['gift_credit'] == 0


def test_refresh_failure_preserves_confirmation_and_balance_and_never_reclaims(context):
    store, service, account, _, client, _ = context
    client.account_state.return_value = {'available_balance': 0, 'buckets': {}}
    assert service.checkin_account(account['id'])['quota_refreshed'] is False
    row = store.account(account['id'])
    assert row['last_checkin_date'] == claim_day() and row['balance'] == 100
    assert row['checkin']['confirmed_today'] and row['balance_details']['_meta']['stale']
    client.account_state.return_value = fresh_quota(gift=0)
    service.checkin_account(account['id'], _refresh_only=True)
    assert store.account(account['id'])['balance'] == 20
    client.daily_checkin.assert_called_once()


def test_gift_spent_or_missing_details_cannot_reset_a_confirmed_day(context):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    store.update_account(account['id'], {'last_checkin_date': claim_day(),
        'balance_details': {'gift_credit': 0, 'vip_credit': 20, 'purchase_credit': 0}})
    client.account_state.return_value = fresh_quota(gift=0)
    service.checkin_account(account['id'], _automatic=True, _refresh_only=True)
    client.daily_checkin.assert_not_called()
    assert store.account(account['id'])['last_checkin_date'] == claim_day()


@pytest.mark.parametrize('buckets', [{}, {'details': []}, {'details': None}])
def test_cli_aggregate_is_fresh_but_missing_breakdown_preserves_marked_history(context, buckets):
    store, service, account, _, _, _ = context
    store.update_account(account['id'], {'balance_details': {
        'gift_credit': 10, 'vip_credit': 20, 'purchase_credit': 0,
        '_meta': {'source': 'dreamina_web', 'observed_at': 1, 'stale': False}}})
    account = store.account(account['id'], private=True)
    reader = Mock()
    reader.identity.return_value = {'user_id': account['login_user_id'], 'available_balance': 7,
                                   'buckets': buckets}
    service.canvas_client = Mock(return_value=reader)
    row = service._check_cli_account(account)
    assert row['balance'] == 7
    assert row['balance_details']['gift_credit'] == 10
    assert row['balance_details']['_meta']['stale'] is True
    assert row['balance_details']['_meta']['source'] == 'dreamina_web'


def test_cli_details_presence_is_metadata_not_a_daily_claim_verdict(context):
    store, _, account, _, _, _ = context
    item = {'available': 99, 'expiresAt': '2099-01-01T00:00:00Z'}
    result = cli_credit_details(account, {'details': [item]})
    assert result['details'] == [item] and result['_meta']['stale'] is False
    assert 'gift_credit' not in result
    assert store.account(account['id'])['last_checkin_date'] == ''


def test_late_cli_read_cannot_overwrite_a_newer_web_credit_snapshot(context):
    store, service, account, _, _, _ = context
    reader = Mock()

    def old_snapshot():
        store.update_account(account['id'], {'balance': 130, 'balance_details': {
            'gift_credit': 30, 'vip_credit': 100, 'purchase_credit': 0,
            '_meta': {'source': 'dreamina_web', 'observed_at': 10**12, 'stale': False}}})
        return {'user_id': account['login_user_id'], 'available_balance': 100, 'buckets': {}}

    reader.identity.side_effect = old_snapshot
    service.canvas_client = Mock(return_value=reader)
    assert service._check_cli_account(account)['balance'] == 130
    assert store.account(account['id'])['balance_details']['gift_credit'] == 30


def test_old_cli_zero_cannot_disable_account_after_new_web_balance(context):
    store, service, account, _, _, _ = context
    store.update_settings({'low_balance_disable_threshold': 10})
    reader = Mock()

    def obsolete_zero():
        store.update_account(account['id'], {'balance': 50, 'balance_details': {
            'gift_credit': 50, 'vip_credit': 0, 'purchase_credit': 0,
            '_meta': {'source': 'dreamina_web', 'observed_at': 10**12, 'stale': False}}})
        return {'user_id': account['login_user_id'], 'available_balance': 0, 'buckets': {}}

    reader.identity.side_effect = obsolete_zero
    service.canvas_client = Mock(return_value=reader)
    result = service._check_cli_account(account)
    assert result['enabled'] and result['status'] == 'active' and result['balance'] == 50


def test_old_cli_error_cannot_overwrite_a_new_login_epoch(context):
    store, service, account, _, _, _ = context
    reader = Mock()

    def changed():
        store.update_account(account['id'], {'status': 'logging_in', 'enabled': False, 'setup_checked_at': 99})
        raise CanvasCLIError('CLI_AUTH_FAILED', 'old authentication error')

    reader.identity.side_effect = changed
    service.canvas_client = Mock(return_value=reader)
    with pytest.raises(AccountMaintenanceBusy):
        service._check_cli_account(account)
    result = store.account(account['id'])
    assert result['status'] == 'logging_in' and not result['enabled']


def test_reject_34070104_has_bounded_independent_retry_and_no_false_confirmation(context, monkeypatch):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    clock = [1000]
    monkeypatch.setattr('app.daily_checkin.time', SimpleNamespace(time=lambda: clock[0]))
    client.daily_checkin.side_effect = BrowserRejected('Dreamina ret=34070104: shark action check reject')
    client.account_state.return_value = fresh_quota()
    with pytest.raises(BrowserRejected):
        service.checkin_account(account['id'], _automatic=True)
    state = json.loads(store.account(account['id'], private=True)['checkin_state_json'])
    assert state['retry_class'] == 'rejected' and state['next_retry_at'] == clock[0] + 3600
    assert state['last_error_code'] == '34070104' and state['last_http_status'] is None
    with pytest.raises(AccountMaintenanceBusy):
        service.checkin_account(account['id'], _automatic=True)
    assert client.daily_checkin.call_count == 1
    assert store.account(account['id'])['last_checkin_date'] == ''
    assert not service._maintenance_failures


@pytest.mark.parametrize('error,code,http', [
    (BrowserRejected('Dreamina ret=34010100: param error'), '34010100', None),
    (BrowserRejected('Dreamina HTTP 503'), '', 503),
])
def test_known_temporary_rejections_use_a_bounded_burst_without_faking_confirmation(context, monkeypatch, caplog, error, code, http):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    clock = [1000]
    monkeypatch.setattr('app.daily_checkin.time', SimpleNamespace(time=lambda: clock[0]))
    client.daily_checkin.side_effect = error
    client.account_state.return_value = fresh_quota()
    for delay in (60, 300, 3600):
        service._daily_claims._run_one(account['id'])
        state = json.loads(store.account(account['id'], private=True)['checkin_state_json'])
        assert state['retry_class'] == 'upstream_transient'
        assert state['last_error_code'] == code and state['last_http_status'] == http
        assert state['next_retry_at'] == clock[0] + delay
        assert store.account(account['id'])['last_checkin_date'] == ''
        clock[0] = state['next_retry_at'] + 1
    assert client.daily_checkin.call_count == 3
    assert f'error_code={code}' in caplog.text and f'http_status={http}' in caplog.text
    client.daily_checkin.side_effect = None
    client.daily_checkin.return_value = {'receive_quota': 10}
    service.checkin_account(account['id'], _automatic=True)
    assert store.account(account['id'])['checkin']['confirmed_today']
    assert client.daily_checkin.call_count == 4


def test_sdk_timeout_never_claims_and_does_not_reclassify_existing_long_backoff(context, monkeypatch):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    client.browser.wait_daily_credit_sdk.side_effect = BrowserError('SDK readiness timed out before request')
    with pytest.raises(BrowserError):
        service.checkin_account(account['id'], _automatic=True)
    client.daily_checkin.assert_not_called()
    state = json.loads(store.account(account['id'], private=True)['checkin_state_json'])
    assert state['retry_class'] == 'transport'
    assert state['attempts'] == 1 and state['last_error_code'] == ''
    before = store.account(account['id'], private=True)['checkin_state_json']
    service._daily_claims.dispatch()
    assert store.account(account['id'], private=True)['checkin_state_json'] == before


@pytest.mark.parametrize('change', ['uid', 'global', 'disabled', 'binding'])
def test_account_changes_while_waiting_for_sdk_prevent_any_claim(context, change):
    store, service, account, automation, client, ready = context
    store.update_settings({'daily_checkin_enabled': True})
    def changed():
        if change == 'uid':automation.read_identity.return_value = {'user_id':'different-user'}
        elif change == 'global':store.update_settings({'daily_checkin_enabled':False})
        elif change == 'disabled':store.update_account(account['id'], {'enabled':False})
        else:store.update_account(account['id'], {'login_user_id':'different-user'})
    client.browser.wait_daily_credit_sdk.side_effect = changed
    with pytest.raises(BrowserError):
        service.checkin_account(account['id'], _automatic=True)
    client.daily_checkin.assert_not_called()
    assert store.account(account['id'])['last_checkin_date'] == ''


def test_transport_burst_cools_down_then_recovers_on_the_same_day(context, monkeypatch):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    clock = [1000]
    monkeypatch.setattr('app.daily_checkin.time', SimpleNamespace(time=lambda: clock[0]))
    client.daily_checkin.side_effect = BrowserError('synthetic temporary network failure')
    client.account_state.return_value = fresh_quota()
    for index in range(3):
        with pytest.raises(BrowserError):
            service.checkin_account(account['id'], _automatic=True)
        state = json.loads(store.account(account['id'], private=True)['checkin_state_json'])
        if index < 2:
            clock[0] = state['next_retry_at'] + 1
    assert state['attempts'] == 3 and state['next_retry_at'] == clock[0] + 3600
    with pytest.raises(AccountMaintenanceBusy):
        service.checkin_account(account['id'], _automatic=True)
    assert client.daily_checkin.call_count == 3
    clock[0] = state['next_retry_at'] + 1
    client.daily_checkin.side_effect = None
    client.daily_checkin.return_value = {'receive_quota': 10}
    service.checkin_account(account['id'], _automatic=True)
    state = json.loads(store.account(account['id'], private=True)['checkin_state_json'])
    assert state['attempts'] == 1 and state['total_attempts'] == 4
    assert state['state'] == 'confirmed' and client.daily_checkin.call_count == 4
    service.checkin_account(account['id'], _automatic=True)
    assert client.daily_checkin.call_count == 4


def test_retry_cooldown_survives_restart_and_setting_toggle(context, monkeypatch):
    from app.service import Service
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    clock = [1000]
    monkeypatch.setattr('app.daily_checkin.time', SimpleNamespace(time=lambda: clock[0]))
    client.daily_checkin.side_effect = BrowserError('temporary transport error')
    with pytest.raises(BrowserError):
        service.checkin_account(account['id'], _automatic=True)
    before = store.account(account['id'], private=True)['checkin_state_json']
    service.update_settings({'daily_checkin_enabled': False})
    service.update_settings({'daily_checkin_enabled': True})
    restarted = Service(store)
    restarted.browser_runtime = service.browser_runtime
    try:
        with pytest.raises(AccountMaintenanceBusy):
            restarted.checkin_account(account['id'], _automatic=True)
        assert store.account(account['id'], private=True)['checkin_state_json'] == before
        assert client.daily_checkin.call_count == 1
    finally:
        restarted.stop()


def test_legacy_three_attempt_state_does_not_permanently_lock_the_same_day(context):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    state = read_state(account)
    state.update(state='retry', attempts=3, next_retry_at=0)
    assert store.save_checkin_state(account['id'], state, binding=identity_binding(account))
    client.account_state.return_value = fresh_quota()
    service.checkin_account(account['id'], _automatic=True)
    assert store.account(account['id'])['checkin']['confirmed_today']


def test_dispatch_reserves_at_most_two_accounts_and_busy_release_wakes_pending(context):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    for index in range(4):
        row = store.upsert_account({'name': f'extra-{index}', 'proxy_url': f'socks5://proxy.test:{21000+index}'})
        store.update_account(row['id'], {'status': 'active', 'login_user_id': f'user-{index}',
            'watermark_ready': True, 'agreement_ready': True, 'cli_auth_status': 'ready', 'balance': 100})
    service._daily_claims._pool.shutdown(wait=True)
    service._daily_claims._pool = Mock()
    service._active[account['id']] = 1
    service._daily_claims.dispatch()
    assert service._daily_claims._pool.submit.call_count == 2
    assert len(service._daily_claims._inflight) == 2
    assert not service._aux_account_owners  # No global exclusion of all due accounts.
    for row in store.accounts(private=True):
        assert read_state(row)['state'] == 'pending'
    service._daily_claims._wake.clear()
    service._release_account(account['id'])
    assert service._daily_claims._wake.is_set()
    client.daily_checkin.assert_not_called()


def test_global_flag_or_manual_disable_blocks_automatic_but_manual_api_keeps_its_behavior(context):
    store, service, account, _, client, _ = context
    client.account_state.return_value = fresh_quota()
    with pytest.raises(AccountMaintenanceBusy):
        service.checkin_account(account['id'], _automatic=True)
    store.update_settings({'daily_checkin_enabled': True})
    store.update_account(account['id'], {'enabled': False})
    with pytest.raises(AccountMaintenanceBusy):
        service.checkin_account(account['id'], _automatic=True)
    assert service.checkin_account(account['id'])['received'] == 10
    assert not store.account(account['id'])['enabled']


def test_disabling_global_flag_during_readiness_prevents_the_claim(context):
    store, service, account, automation, client, ready = context
    store.update_settings({'daily_checkin_enabled': True})

    def disabled():
        store.update_settings({'daily_checkin_enabled': False})
        return ready

    automation.read_identity.side_effect = disabled
    with pytest.raises(AccountMaintenanceBusy):
        service.checkin_account(account['id'], _automatic=True)
    client.daily_checkin.assert_not_called()


def test_same_day_manual_and_automatic_calls_have_one_exclusive_claim(context):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    entered, release = threading.Event(), threading.Event()
    errors = []

    def claim():
        entered.set(); release.wait(1)
        return {'receive_quota': 10}

    client.daily_checkin.side_effect = claim
    client.account_state.return_value = fresh_quota()

    def automatic():
        try: service.checkin_account(account['id'], _automatic=True)
        except Exception as error: errors.append(error)

    thread = threading.Thread(target=automatic)
    thread.start(); assert entered.wait(1)
    with pytest.raises(AccountMaintenanceBusy):
        service.checkin_account(account['id'])
    release.set(); thread.join(1)
    assert not thread.is_alive() and not errors
    client.daily_checkin.assert_called_once()


@pytest.mark.parametrize('field,value', [('login_user_id', 'different-user'), ('setup_checked_at', 999)])
def test_late_receipt_cannot_write_to_a_changed_identity_epoch(context, field, value):
    store, service, account, _, client, _ = context

    def changed():
        store.update_account(account['id'], {field: value})
        return {'receive_quota': 10}

    client.daily_checkin.side_effect = changed
    with pytest.raises(AccountMaintenanceBusy):
        service.checkin_account(account['id'])
    row = store.account(account['id'])
    assert row['last_checkin_date'] == '' and row['balance'] == 100
    client.account_state.assert_not_called()


def test_unexpected_account_error_does_not_kill_the_maintenance_pass(context, caplog):
    store, service, account, _, _, _ = context
    second = store.upsert_account({'name': 'second', 'proxy_url': 'socks5://proxy.test:20002'})
    store.update_account(second['id'], {'status': 'active', 'cli_auth_status': 'ready'})
    service.check_account = Mock(side_effect=[TypeError('private upstream shape'), {'status': 'active'}])
    service._stop.wait = lambda _: service._stop.set()
    service._maintain_accounts()
    assert service.check_account.call_count == 2
    assert account['id'] in service._maintenance_failures
    assert 'private upstream shape' not in caplog.text


def test_identity_or_profile_change_clears_daily_binding_and_public_api_hides_it(context):
    store, _, account, _, _, _ = context
    state = read_state(account)
    assert store.save_checkin_state(account['id'], state, binding=identity_binding(account),
                                    values={'last_checkin_date': claim_day()})
    row = store.update_account(account['id'], {'setup_checked_at': 123456})
    assert row['last_checkin_date'] == '' and 'checkin_state_json' not in row
    assert not store.save_checkin_state(account['id'], state, binding=identity_binding(account),
                                       values={'balance': 0})
    assert store.account(account['id'])['balance'] == 100


def test_slow_browser_runtime_probe_cannot_hold_global_generation_admission(context):
    store, service, account, _, _, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    second = store.upsert_account({'name': 'other-ready', 'proxy_url': 'socks5://proxy.test:21111'})
    store.update_account(second['id'], {'status': 'active', 'login_user_id': 'other-user',
        'cli_auth_status': 'ready', 'watermark_ready': True, 'agreement_ready': True, 'balance': 100})
    service._daily_claims._pool.shutdown(wait=True)
    service._daily_claims._pool = Mock()
    entered, release, admitted = threading.Event(), threading.Event(), threading.Event()

    def slow_busy(identifier):
        if identifier == account['id']:
            entered.set(); release.wait(1)
            return True
        return False

    service.browser_runtime.busy.side_effect = slow_busy
    dispatch = threading.Thread(target=service._daily_claims.dispatch)
    selected = []

    def reserve():
        selected.append(service._reserve_account(1, exclude_ids={account['id']}, task_id='other-job'))
        admitted.set()

    dispatch.start(); assert entered.wait(1)
    worker = threading.Thread(target=reserve)
    worker.start()
    try:
        assert admitted.wait(.3), 'A slow Chrome runtime blocked another account admission'
        assert selected[0]['id'] == second['id']
    finally:
        release.set(); dispatch.join(1); worker.join(1)
        service._release_account(second['id'], 'other-job')


@pytest.mark.parametrize('stage', ['readiness', 'readback'])
def test_midnight_rollover_uses_the_actual_claim_day_not_chrome_start_day(context, monkeypatch, stage):
    store, service, account, automation, client, ready = context
    day = ['2026-11-02']
    monkeypatch.setattr('app.daily_checkin.claim_day', lambda: day[0])
    client.account_state.return_value = fresh_quota()
    if stage == 'readiness':
        def crossing():
            day[0] = '2026-11-03'
            return ready
        automation.read_identity.side_effect = crossing
    else:
        state = read_state(account, day[0])
        state.update(attempts=1, state='retry')
        store.save_checkin_state(account['id'], state, binding=identity_binding(account))
        def crossing(*args, **kwargs):
            day[0] = '2026-11-03'
            return fresh_quota()
        client.account_state.side_effect = crossing
    result = service.checkin_account(account['id'])
    assert result['date'] == '2026-11-03'
    assert store.account(account['id'])['last_checkin_date'] == '2026-11-03'
    assert json.loads(store.account(account['id'], private=True)['checkin_state_json'])['day'] == '2026-11-03'
    client.daily_checkin.assert_called_once()


def test_external_profile_port_change_clears_and_rejects_the_old_receipt_binding(context):
    store, _, account, _, _, _ = context
    store.update_account(account['id'], {'cdp_port': 19008, 'managed_profile': False})
    account = store.account(account['id'], private=True)
    state = read_state(account)
    state['state'] = 'confirmed'
    assert store.save_checkin_state(account['id'], state, binding=identity_binding(account),
                                    values={'last_checkin_date': claim_day()})
    changed = store.update_account(account['id'], {'cdp_port': 19009})
    assert changed['last_checkin_date'] == ''
    assert not store.save_checkin_state(account['id'], state, binding=identity_binding(account),
                                       values={'last_checkin_date': claim_day()})


def test_implicit_managed_startup_port_does_not_invalidate_the_same_profile(context):
    store, _, account, _, _, _ = context
    state = read_state(account)
    assert store.save_checkin_state(account['id'], state, binding=identity_binding(account),
                                    values={'last_checkin_date': claim_day()})
    changed = store.update_account(account['id'], {'cdp_port': 19300+account['id']})
    assert changed['last_checkin_date'] == claim_day()


def test_post_receipt_page_identity_mismatch_never_confirms_the_day(context):
    store, service, account, automation, client, ready = context
    automation.read_identity.side_effect = [ready, ready, {'user_id': 'other-page-user'}]
    with pytest.raises(BrowserError, match='identity changed'):
        service.checkin_account(account['id'])
    assert store.account(account['id'])['last_checkin_date'] == ''
    assert store.account(account['id'])['balance'] == 100
    client.daily_checkin.assert_called_once()
    client.account_state.assert_not_called()


def test_post_quota_page_switch_preserves_receipt_but_does_not_apply_foreign_balance(context):
    store, service, account, automation, client, ready = context
    automation.read_identity.side_effect = [ready, ready, ready, {'user_id': 'other-page-user'}]
    client.account_state.return_value = fresh_quota()
    result = service.checkin_account(account['id'])
    assert result['quota_refreshed'] is False
    row = store.account(account['id'])
    assert row['last_checkin_date'] == claim_day() and row['checkin']['confirmed_today']
    assert row['balance'] == 100


def test_confirmed_refresh_readiness_failure_retains_receipt_and_is_rate_limited(context):
    store, service, account, automation, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    store.update_account(account['id'], {'last_checkin_date': claim_day()})
    automation.read_identity.side_effect = BrowserError('synthetic read failure')
    with pytest.raises(BrowserError):
        service.checkin_account(account['id'], _automatic=True, _refresh_only=True)
    row = store.account(account['id'])
    assert row['checkin']['confirmed_today'] and row['checkin']['quota_status'] == 'failed'
    service._daily_claims._pool.shutdown(wait=True)
    service._daily_claims._pool = Mock()
    service._daily_claims.dispatch()
    service._daily_claims._pool.submit.assert_not_called()
    client.daily_checkin.assert_not_called()


def test_busy_due_accounts_keep_accepting_generation_without_daily_reservations(context):
    store, service, first, _, _, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    accounts = [first]
    for index in range(2):
        row = store.upsert_account({'name': f'drain-{index}', 'proxy_url': f'socks5://proxy.test:{23000+index}'})
        store.update_account(row['id'], {'status': 'active', 'login_user_id': f'drain-user-{index}',
            'cli_auth_status': 'ready', 'watermark_ready': True, 'agreement_ready': True,
            'balance': 100, 'max_concurrency': 20})
        accounts.append(store.account(row['id'], private=True))
    for row in accounts:
        service._active[row['id']] = 1
    service._daily_claims._pool.shutdown(wait=True)
    service._daily_claims._pool = Mock()
    service._daily_claims.dispatch()
    assert service._daily_claims._inflight == set()
    service._daily_claims._pool.submit.assert_not_called()
    selected = service._reserve_account(1, task_id='new-work')
    assert selected['id'] in {row['id'] for row in accounts}
    service._release_account(selected['id'], 'new-work')
    service.update_settings({'daily_checkin_enabled': False})
    service._daily_claims.dispatch()
    assert service._daily_claims._inflight == set()
    assert service._active[accounts[0]['id']] == service._active[accounts[1]['id']] == 1


def test_queued_daily_worker_does_not_reserve_generation_before_actual_claim(context):
    store, service, account, _, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    service._daily_claims._pool.shutdown(wait=True)
    service._daily_claims._pool = Mock()
    service._daily_claims.dispatch()
    assert service._daily_claims.reserved(account['id'])
    assert not service._aux_account_owners
    assert not service._has_waiting_credit_account(1, set(), 'queued-work')
    selected = service._reserve_account(1, task_id='queued-work')
    assert selected['id'] == account['id']
    # A queued claim loses the race to new work without opening a browser.
    service._daily_claims._run_one(account['id'])
    service.browser_runtime.acquire.assert_not_called()
    client.daily_checkin.assert_not_called()
    assert not service._aux_account_owners
    service._release_account(account['id'], 'queued-work')


def test_uncertain_finance_and_idle_viewer_do_not_take_daily_drain_slots(context):
    from test_daily_checkin import queued_task
    store, service, account, _, _, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    queued_task(store, 'uncertain')
    store.update_task('uncertain', {'account_id': account['id'], 'status': 'failed',
        'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'})
    service._daily_claims.dispatch()
    assert not service._daily_claims._inflight
    store.update_task('uncertain', {'error_code': 'GENERIC_FAILURE'})
    service.browser_runtime.busy.return_value = True
    service._daily_claims.dispatch()
    assert not service._daily_claims._inflight


def test_browser_health_late_snapshot_cannot_overwrite_daily_receipt_or_fresh_quota(context, monkeypatch):
    store, service, account, _, _, _ = context
    store.update_account(account['id'], {'execution_mode': 'browser'})
    account = store.account(account['id'], private=True)
    service._web_client = Mock()
    reader = service._web_client.return_value
    reader.browser.project_id.return_value = ''
    store.update_settings({'low_balance_disable_threshold': 10})
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))

    def obsolete():
        state = read_state(account)
        state.update(state='confirmed', confirmed_at=10**12)
        assert store.save_checkin_state(account['id'], state, binding=identity_binding(account), values={
            'last_checkin_date': claim_day(), 'balance': 130, 'balance_details': {
                'vip_credit': 100, 'gift_credit': 30, 'purchase_credit': 0,
                '_meta': {'source': 'dreamina_web', 'observed_at': 10**12, 'stale': False}}})
        return {'available_balance': 0, 'buckets': {'vip_credit': 0, 'gift_credit': 0, 'purchase_credit': 0}}

    reader.account_state.side_effect = obsolete
    row = service._check_account_live(account)
    assert row['balance'] == 130 and row['enabled'] and row['status'] == 'active'
    assert row['balance_details']['gift_credit'] == 30
    assert row['last_checkin_date'] == claim_day()


def test_browser_health_is_deferred_while_daily_account_is_owned(context):
    store, service, account, _, _, _ = context
    store.update_account(account['id'], {'execution_mode': 'browser'})
    with service._exclusive_aux_account(account['id']):
        with pytest.raises(AccountMaintenanceBusy):
            service.check_account(account['id'])
    service.browser_runtime.acquire.assert_not_called()
