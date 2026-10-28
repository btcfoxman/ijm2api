"""One daily claim follows a short, verified, read-only browser warmup."""
from datetime import datetime
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from app.account_automation import AutomationError
from app.browser import BrowserError, BrowserRejected
from app.catalog import normalize_request
from app.service import AccountMaintenanceBusy, Service
from app.store import Store


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'accounts.db'))
    store.update_settings({'canvas_cli_enabled': True})
    account = store.upsert_account({'name': 'synthetic', 'proxy_url': 'socks5://proxy.test:20001'})
    account = store.update_account(account['id'], {'login_user_id': 'synthetic-uid',
        'status': 'active', 'cli_auth_status': 'ready', 'watermark_ready': True, 'agreement_ready': True,
        'balance': 100, 'max_concurrency': 20})
    account = store.account(account['id'], private=True)
    service = Service(store)
    service.browser_runtime = Mock()
    service.browser_runtime.busy.return_value = False
    service.browser_runtime.acquire.return_value = ({**account, 'cdp_port': 19300 + account['id']}, True)
    ready = {'identity_verified': True, 'identity': {'user_id': 'synthetic-uid'}}
    automation = Mock()
    automation.read_ready.return_value = ready
    monkeypatch.setattr('app.service.AccountAutomation', Mock(return_value=automation))
    client = Mock()
    client.daily_checkin.return_value = {'receive_quota': 10}
    monkeypatch.setattr('app.service.DreaminaClient', Mock(return_value=client))
    yield store, service, account, automation, client, ready
    service.stop()


def test_cold_sdk_reads_retry_before_one_daily_claim_and_release_browser(context):
    store, service, account, automation, client, ready = context
    automation.read_ready.side_effect = [
        AutomationError('login_required', 'Dreamina identity read could not complete'),
        AutomationError('login_required', 'Dreamina identity read could not complete'), ready]
    service._stop.wait = Mock(return_value=False)
    assert service.checkin_account(account['id'])['received'] == 10
    assert automation.read_ready.call_count == 3
    client.daily_checkin.assert_called_once_with()
    assert store.account(account['id'])['last_checkin_date']
    service.browser_runtime.release.assert_called_once()


@pytest.mark.parametrize('error', [
    AutomationError('login_required', 'Dreamina login session has not been verified'),
    AutomationError('login_required', 'Dreamina signed-in identity does not match this account'),
    AutomationError('challenge_required', 'Dreamina login requires a human security challenge'),
    AutomationError('verification_required', 'Dreamina login requires email or security verification'),
    AutomationError('setup_required', 'Dreamina account settings could not be verified'),
    BrowserRejected('Dreamina ret=34010100: param error'),
])
def test_login_challenge_mismatch_and_business_errors_never_retry_or_claim(context, error):
    store, service, account, automation, client, _ = context
    automation.read_ready.side_effect = error
    with pytest.raises(type(error)):
        service.checkin_account(account['id'])
    automation.read_ready.assert_called_once()
    client.daily_checkin.assert_not_called()
    assert not store.account(account['id'])['last_checkin_date']
    service.browser_runtime.release.assert_called_once()


@pytest.mark.parametrize('ready', [
    {'identity_verified': True, 'identity': {'user_id': 'other-uid'}},
    {'identity_verified': False, 'identity': {'user_id': 'synthetic-uid'}},
    {'identity_verified': True, 'identity': {}},
])
def test_readiness_requires_actual_matching_identity(context, ready):
    store, service, account, automation, client, _ = context
    automation.read_ready.return_value = ready
    with pytest.raises(BrowserError, match='identity does not match'):
        service.checkin_account(account['id'])
    client.daily_checkin.assert_not_called()
    assert not store.account(account['id'])['last_checkin_date']


def test_missing_saved_identity_never_claims(context):
    store, service, account, automation, client, _ = context
    current, owned = service.browser_runtime.acquire.return_value
    service.browser_runtime.acquire.return_value = ({**current, 'login_user_id': ''}, owned)
    with pytest.raises(BrowserError, match='verified signed-in account'):
        service.checkin_account(account['id'])
    automation.read_ready.assert_not_called()
    client.daily_checkin.assert_not_called()


def test_one_failed_claim_is_not_replayed_and_not_marked_success(context):
    store, service, account, automation, client, _ = context
    client.daily_checkin.side_effect = BrowserError('Dreamina browser fetch failed')
    with pytest.raises(BrowserError):
        service.checkin_account(account['id'])
    automation.read_ready.assert_called_once()
    client.daily_checkin.assert_called_once()
    assert not store.account(account['id'])['last_checkin_date']
    service.browser_runtime.release.assert_called_once()


def test_readiness_deadline_never_enters_claim(context, monkeypatch):
    store, service, account, automation, client, _ = context
    clock = {'now': 0.0}
    monkeypatch.setattr('app.service.time', SimpleNamespace(monotonic=lambda: clock['now']))
    service._stop.wait = lambda delay: clock.update(now=clock['now'] + delay)
    automation.read_ready.side_effect = AutomationError('login_required', 'Dreamina identity read could not complete')
    with pytest.raises(BrowserError, match='readiness timed out'):
        service.checkin_account(account['id'])
    assert clock['now'] == 30 and automation.read_ready.call_count == 60
    client.daily_checkin.assert_not_called()
    assert not store.account(account['id'])['last_checkin_date']
    service.browser_runtime.release.assert_called_once()


def test_stop_arriving_during_readiness_does_not_claim(context):
    store, service, account, automation, client, ready = context
    def stop():
        service._stop.set()
        return ready
    automation.read_ready.side_effect = stop
    with pytest.raises(BrowserError, match='cancelled'):
        service.checkin_account(account['id'])
    client.daily_checkin.assert_not_called()
    assert not store.account(account['id'])['last_checkin_date']


def test_already_checked_in_does_not_open_browser(context, monkeypatch):
    store, service, account, automation, client, _ = context
    monkeypatch.setenv('IJM_TIME_ZONE', 'Asia/Taipei')
    today = datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
    store.update_account(account['id'], {'last_checkin_date': today})
    assert service.checkin_account(account['id']) == {'already_checked_in': True, 'date': today}
    service.browser_runtime.acquire.assert_not_called()
    automation.read_ready.assert_not_called()
    client.daily_checkin.assert_not_called()


def test_maintenance_failure_reports_only_safe_stage_and_error_type(context, caplog):
    store, service, account, automation, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    service.check_account = Mock(return_value={'status': 'active'})
    service.checkin_account = Mock(side_effect=BrowserError('private cookie=secret-value prompt=private-text'))
    def finish(_):
        service._stop.set()
        return True
    service._stop.wait = finish
    service._maintain_accounts()
    record = next(r for r in caplog.records if r.message.startswith('IJM account maintenance failed'))
    assert record.account_id == account['id'] and record.stage == 'daily_checkin'
    assert record.error_type == 'BrowserError'
    assert 'secret-value' not in caplog.text and 'private-text' not in caplog.text
    assert account['id'] in service._maintenance_failures


def queued_task(store, identifier='waiting'):
    request = {'model': 'sd-2-0-mini', 'prompt': 'Synthetic landscape', 'duration': 4}
    return store.create_task(identifier, request, normalize_request(request))


@pytest.mark.parametrize('source', ['active', 'preparing', 'submitted', 'running', 'held_unknown'])
def test_generation_occupancy_defers_checkin_without_claim_or_reservation_changes(context, source):
    store, service, account, automation, client, _ = context
    if source == 'active':
        service._active[account['id']] = 1
    else:
        queued_task(store, 'existing')
        store.update_task('existing', {'account_id': account['id'],
            'status': 'failed' if source == 'held_unknown' else source,
            'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN' if source == 'held_unknown' else ''})
    service._credit_reservations['existing'] = (account['id'], 10)
    before = dict(service._credit_reservations)
    with pytest.raises(AccountMaintenanceBusy, match='deferred'):
        service.checkin_account(account['id'])
    service.browser_runtime.acquire.assert_not_called()
    automation.read_ready.assert_not_called()
    client.daily_checkin.assert_not_called()
    assert not store.account(account['id'])['last_checkin_date']
    assert service._credit_reservations == before and not service._aux_account_owners


@pytest.mark.parametrize('source', ['logging_in', 'browser_lease'])
def test_existing_login_or_browser_lease_defers_checkin(context, source):
    store, service, account, automation, client, _ = context
    if source == 'logging_in':
        store.update_account(account['id'], {'status': 'logging_in'})
    else:
        def check_browser(_):
            available = threading.Event()
            def probe_global_lock():
                with service._lock:
                    available.set()
            helper = threading.Thread(target=probe_global_lock)
            helper.start()
            helper.join(1)
            assert available.is_set(), 'Browser lease inspection must not hold the global admission lock'
            return True
        service.browser_runtime.busy.side_effect = check_browser
    with pytest.raises(AccountMaintenanceBusy, match='deferred'):
        service.checkin_account(account['id'])
    service.browser_runtime.acquire.assert_not_called()
    automation.read_ready.assert_not_called()
    client.daily_checkin.assert_not_called()
    assert not store.account(account['id'])['last_checkin_date']
    assert not service._aux_account_owners


def test_automatic_busy_checkin_is_deferred_without_failure_backoff(context, caplog):
    store, service, account, automation, client, _ = context
    store.update_settings({'daily_checkin_enabled': True})
    service._active[account['id']] = 1
    service.check_account = Mock(return_value={'status': 'active'})
    service._maintenance_failures[account['id']] = (2, 0)
    def finish(_):
        service._stop.set()
        return True
    service._stop.wait = finish
    service._maintain_accounts()
    assert service._maintenance_failures[account['id']] == (2, 0)
    assert not caplog.records
    client.daily_checkin.assert_not_called()
    service.browser_runtime.acquire.assert_not_called()
    assert not store.account(account['id'])['last_checkin_date']


def test_checkin_occupancy_is_exclusive_even_with_twenty_generation_slots(context):
    store, service, account, automation, client, _ = context
    other = store.upsert_account({'name': 'other', 'proxy_url': 'socks5://proxy.test:20002', 'max_concurrency': 20})
    store.update_account(other['id'], {'status': 'active', 'cli_auth_status': 'ready',
        'balance': 100, 'login_user_id': 'other-uid', 'watermark_ready': True, 'agreement_ready': True})
    def claim():
        assert account['id'] in service._aux_account_owners
        assert service._reserve_account(1, exclude_ids={other['id']}, task_id='blocked') is None
        assert service._has_eligible_account(1, {other['id']}, 'blocked')
        assert service._has_waiting_credit_account(1, {other['id']}, 'blocked')
        assert all(a['id'] != account['id'] for a in service._funded_accounts(1, set(), {}))
        selected = service._reserve_account(1, task_id='other-task')
        assert selected['id'] == other['id']
        service._release_account(other['id'], 'other-task')
        lock_available = threading.Event()
        def acquire_lock():
            with service._lock:
                lock_available.set()
        helper = threading.Thread(target=acquire_lock)
        helper.start()
        helper.join(1)
        assert lock_available.is_set(), 'The global admission lock must not be held during a network claim'
        return {'receive_quota': 10}
    client.daily_checkin.side_effect = claim
    assert service.checkin_account(account['id'])['received'] == 10
    assert not service._aux_account_owners and not service._credit_reservations


@pytest.mark.parametrize('failure', ['claim', 'readiness', 'cancel'])
def test_aux_owner_cleanup_never_releases_generation_credit_reservations(context, failure):
    store, service, account, automation, client, _ = context
    service._credit_reservations['unrelated-audit'] = (account['id'], 7)
    before = dict(service._credit_reservations)
    if failure == 'claim':
        client.daily_checkin.side_effect = BrowserError('synthetic claim error')
    elif failure == 'readiness':
        automation.read_ready.side_effect = AutomationError('challenge_required', 'human challenge')
    else:
        def cancel():
            service._stop.set()
            raise BrowserError('synthetic cancellation')
        automation.read_ready.side_effect = cancel
    with pytest.raises(BrowserError):
        service.checkin_account(account['id'])
    assert service._credit_reservations == before
    assert not service._aux_account_owners
    assert not store.account(account['id'])['last_checkin_date']
    service.browser_runtime.release.assert_called_once()


def test_gate_cleanup_preserves_another_owners_marker(context):
    _, service, account, _, _, _ = context
    replacement = object()
    with service._exclusive_aux_account(account['id']):
        service._aux_account_owners[account['id']] = replacement
    assert service._aux_account_owners[account['id']] is replacement
    service._aux_account_owners.clear()


def test_only_funded_account_in_checkin_is_waited_for_instead_of_failed(context):
    store, service, account, _, _, _ = context
    queued_task(store)
    service._execute_canvas = Mock(return_value=True)
    done = threading.Event()
    def run():
        try:
            service._run('waiting')
        finally:
            done.set()
    worker = threading.Thread(target=run)
    try:
        with service._exclusive_aux_account(account['id']):
            worker.start()
            assert not done.wait(.15)
            result = store.task('waiting')
            assert result['status'] == 'queued' and result['account_id'] is None
            service._execute_canvas.assert_not_called()
        worker.join(2)
        assert done.is_set()
        service._execute_canvas.assert_called_once()
        assert store.task('waiting')['status'] == 'preparing'
        assert not service._aux_account_owners and not service._credit_reservations
    finally:
        service._stop.set()
        if worker.is_alive():
            worker.join(2)
