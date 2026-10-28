"""Device authorization never generates, crosses identities, or leaves Chrome open."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from app.canvas_auth import CanvasAuthManager, DeviceAuthorizationBrowser
from app.store import Store


class FakeRuntime:
    def __init__(self):
        self.open = 0
        self.closed = 0

    def assert_idle(self, account_id):
        pass

    @contextmanager
    def lease(self, account):
        self.open += 1
        try:
            yield {**account, 'cdp_port': 19300 + account['id']}
        finally:
            self.open -= 1
            self.closed += 1


@pytest.fixture
def context(tmp_path):
    store = Store(str(tmp_path / 'accounts.db'))
    item = store.upsert_account({'name': 'one', 'proxy_url': 'socks5://proxy.test:20001'})
    store.update_account(item['id'], {'login_user_id': 'account-123', 'watermark_ready': True,
        'agreement_ready': True, 'status': 'active', 'enabled': True})
    service = SimpleNamespace(_lock=threading.RLock(), _active={}, browser_runtime=FakeRuntime(),
        settings=lambda: {'low_balance_disable_threshold': 0})
    client = Mock()
    browser = Mock()
    browser.step.return_value = 'confirmed'
    manager = CanvasAuthManager(store, service, client_factory=lambda account: client,
        browser_factory=lambda account: browser, timeout=15)
    manager._pool.shutdown()
    manager._pool = Mock()
    yield store, service, client, browser, manager, item['id']
    manager.close()


def execute(context):
    *_, manager, identifier = context
    manager.start(identifier)
    manager._run(identifier)


def challenge(client):
    client.auth_start.return_value = {'status': 'pending', 'challenge': {
        'deviceCode': 'private-device-code', 'userCode': 'ABCD-1234',
        'verificationUri': 'https://dreamina.capcut.com/cli/authorize',
        'verificationUriComplete': 'https://dreamina.capcut.com/cli/authorize?code=ABCD-1234'}}
    client.auth_wait.return_value = {'status': 'authenticated'}


def test_existing_matching_identity_needs_no_browser_or_auth(context):
    store, service, client, browser, manager, identifier = context
    client.identity.return_value = {'id': 'account-123', 'credits': 98}
    execute(context)
    assert manager.status(identifier)['state'] == 'ready'
    assert not manager.busy(identifier)
    assert store.account(identifier)['enabled']
    assert store.account(identifier)['balance'] == 98
    assert store.account(identifier, private=True)['cli_user_id'] == 'account-123'
    assert 'cli_user_id' not in store.account(identifier)
    client.auth_start.assert_not_called()
    browser.open.assert_not_called()
    assert service.browser_runtime.open == service.browser_runtime.closed == 0
    client.submit.assert_not_called()


def test_wrong_existing_identity_never_authorizes_account(context):
    store, service, client, browser, manager, identifier = context
    client.identity.return_value = {'id': 'someone-else', 'credits': 999}
    execute(context)
    assert manager.status(identifier)['state'] == 'failed'
    assert not store.account(identifier)['enabled']
    client.auth_start.assert_not_called()
    client.logout.assert_not_called()


def test_normal_device_confirmation_keeps_browser_until_cli_proves_authorized(context):
    store, service, client, browser, manager, identifier = context
    client.identity.side_effect = [RuntimeError('no authorization'), {'id': 'account-123', 'credits': 20}]
    challenge(client)

    def wait(code, timeout):
        assert code == 'private-device-code' and timeout == 5
        assert service.browser_runtime.open == 1
        assert service.browser_runtime.closed == 0
        return {'status': 'authenticated'}

    client.auth_wait.side_effect = wait
    execute(context)
    assert manager.status(identifier)['state'] == 'ready'
    assert service.browser_runtime.closed == 1
    assert 'private-device-code' not in str(manager.status(identifier))
    assert 'private-device-code' not in str(store.account(identifier))
    client.submit.assert_not_called()


def test_slow_page_after_twenty_seconds_is_rechecked_and_click_is_not_repeated(context, monkeypatch):
    store, service, client, browser, manager, identifier = context
    challenge(client)
    clock = [0.0]
    monkeypatch.setattr('app.canvas_auth.time.monotonic', lambda: clock[0])
    manager.timeout = 60
    browser.step.side_effect = ['waiting', 'confirmed']
    client.identity.side_effect = [RuntimeError('not authorized'), {'id': 'account-123', 'credits': 20}]
    manager.start(identifier)
    monkeypatch.setattr(manager._jobs[identifier]['cancel'], 'wait', Mock(return_value=False))
    observations = []

    def wait(code, timeout):
        observations.append((clock[0], service.browser_runtime.open, service.browser_runtime.closed))
        assert service.browser_runtime.open == 1 and service.browser_runtime.closed == 0
        if len(observations) == 1:
            clock[0] = 25.0  # Initial page content was slower than the old cutoff.
            return {'status': 'pending'}
        if len(observations) == 2:
            clock[0] = 35.0  # Click dispatched, but consent HTTP is still pending.
            return {'status': 'authorization_pending'}
        return {'status': 'authenticated'}

    client.auth_wait.side_effect = wait
    manager._run(identifier)
    assert manager.status(identifier)['state'] == 'ready'
    assert browser.step.call_count == 2
    assert len(observations) == 3
    assert service.browser_runtime.open == 0 and service.browser_runtime.closed == 1


def test_manual_assist_reuses_pending_browser_and_its_existing_auxiliary_slot(context):
    store, service, client, browser, manager, identifier = context
    challenge(client)
    browser.step.return_value = 'manual_required'
    client.identity.side_effect = [RuntimeError('not authorized'), {'id': 'account-123', 'credits': 20}]
    service.open_browser_view = Mock(return_value={**store.account(identifier, private=True), 'cdp_port': 19301})
    service.close_browser_view = Mock()
    # The other account occupies one slot; this worker occupies the last one.
    assert manager._browser_slots.acquire(blocking=False)

    def wait(code, timeout):
        assert service.browser_runtime.open == 1
        assert manager.status(identifier)['state'] == 'manual_required'
        assert manager.open_assist(identifier)['state'] == 'manual_required'
        assert not manager._jobs[identifier]['assist_slot']
        assert browser.open.call_count == 1  # No reload aborts a pending consent.
        return {'status': 'authenticated'}

    client.auth_wait.side_effect = wait
    try:
        execute(context)
    finally:
        manager._browser_slots.release()
    assert manager.status(identifier)['state'] == 'ready'
    service.close_browser_view.assert_called_once_with(identifier)
    assert service.browser_runtime.open == 0 and service.browser_runtime.closed == 1


def test_pending_consent_keeps_browser_until_deadline_then_releases_it(context, monkeypatch):
    store, service, client, browser, manager, identifier = context
    challenge(client)
    clock = [0.0]
    monkeypatch.setattr('app.canvas_auth.time.monotonic', lambda: clock[0])
    client.identity.side_effect = RuntimeError('not authorized')
    manager.start(identifier)
    monkeypatch.setattr(manager._jobs[identifier]['cancel'], 'wait', Mock(return_value=False))

    def wait(code, timeout):
        assert service.browser_runtime.open == 1 and service.browser_runtime.closed == 0
        clock[0] += 10
        return {'status': 'pending'}

    client.auth_wait.side_effect = wait
    manager._run(identifier)
    assert manager.status(identifier)['state'] == 'expired'
    assert service.browser_runtime.open == 0 and service.browser_runtime.closed == 1
    assert browser.step.call_count == 1


def test_captcha_is_not_clicked_and_browser_is_released(context):
    store, service, client, browser, manager, identifier = context
    client.identity.side_effect = [RuntimeError('not authorized'), {'id': 'account-123', 'credits': 20}]
    challenge(client)
    browser.step.return_value = 'manual_required'
    client.auth_wait.side_effect = lambda *args, **kwargs: manager.cancel(identifier)
    execute(context)
    assert manager.status(identifier)['state'] == 'cancelled'
    assert service.browser_runtime.open == 0 and service.browser_runtime.closed == 1
    assert not store.account(identifier)['enabled']
    client.logout.assert_not_called()


def test_untrusted_device_uri_never_opens_browser(context):
    store, service, client, browser, manager, identifier = context
    client.identity.side_effect = RuntimeError('not authorized')
    challenge(client)
    client.auth_start.return_value['challenge']['verificationUriComplete'] = 'https://attacker.invalid/oauth'
    execute(context)
    assert manager.status(identifier)['state'] == 'failed'
    browser.open.assert_not_called()
    client.auth_wait.assert_not_called()


def test_cancel_during_identity_read_cannot_enable(context):
    store, service, client, browser, manager, identifier = context

    def identity():
        manager.cancel(identifier)
        return {'id': 'account-123', 'credits': 100}

    client.identity.side_effect = identity
    execute(context)
    assert manager.status(identifier)['state'] == 'cancelled'
    assert not store.account(identifier)['enabled']
    client.logout.assert_not_called()


def test_wrong_identity_after_confirmation_is_rejected(context):
    store, service, client, browser, manager, identifier = context
    challenge(client)
    client.identity.side_effect = [RuntimeError('not authorized'), {'id': 'other-id', 'credits': 999}]
    execute(context)
    assert manager.status(identifier)['state'] == 'failed'
    assert not store.account(identifier)['enabled']
    assert service.browser_runtime.open == 0


def test_explicit_disabled_intent_is_preserved_on_authorization(context):
    store, service, client, browser, manager, identifier = context
    store.update_account(identifier, {'enabled': False, 'status': 'disabled', 'login_enable_after': False})
    client.identity.return_value = {'id': 'account-123', 'credits': 98}
    execute(context)
    assert manager.status(identifier)['state'] == 'ready'
    assert store.account(identifier)['status'] == 'disabled'
    assert not store.account(identifier)['enabled']


def test_auth_start_is_deduplicated_and_protects_active_tasks(context):
    store, service, client, browser, manager, identifier = context
    first = manager.start(identifier)
    assert first == manager.start(identifier)
    assert manager._pool.submit.call_count == 1
    manager.cancel(identifier)
    manager._run(identifier)
    service._active[identifier] = 1
    with pytest.raises(ValueError, match='active tasks'):
        manager.start(identifier)


def test_interrupted_auth_is_recoverable_without_browser_launch(context):
    store, service, client, browser, manager, identifier = context
    store.update_account(identifier, {'cli_auth_status': 'authorizing', 'enabled': True})
    manager.recover_interrupted()
    saved = store.account(identifier)
    assert saved['cli_auth_status'] == 'required'
    assert not saved['enabled']
    browser.open.assert_not_called()


@pytest.mark.parametrize('uri', ['http://dreamina.capcut.com/auth',
    'https://dreamina.capcut.com.attacker.invalid/auth', 'https://user:pass@dreamina.capcut.com/auth'])
def test_browser_rejects_nonofficial_or_credential_urls(uri):
    browser = DeviceAuthorizationBrowser({'cdp_port': 1})
    with pytest.raises(Exception, match='official'):
        browser.open(uri)


def test_device_expiry_keeps_utc_offset():
    assert CanvasAuthManager._expiry('2026-10-29T08:00:00+08:00') == CanvasAuthManager._expiry('2026-10-29T00:00:00Z')


def test_assist_uses_server_account_browser_and_closes_it_on_cancellation(context):
    store, service, client, browser, manager, identifier = context
    service.open_browser_view = Mock(return_value={**store.account(identifier, private=True), 'cdp_port': 19301})
    service.browser_view = Mock()
    service.close_browser_view = Mock()
    manager.start(identifier)
    manager._publish(identifier, 'manual_required', verification_uri='https://dreamina.capcut.com/cli/authorize')
    manager.open_assist(identifier)
    manager.open_assist(identifier)
    service.open_browser_view.assert_called_once_with(identifier)
    browser.open.assert_called_once_with('https://dreamina.capcut.com/cli/authorize')
    manager.cancel(identifier)
    manager._run(identifier)
    service.close_browser_view.assert_called_once_with(identifier)
    assert manager.status(identifier)['state'] == 'cancelled'
    assert not manager._jobs[identifier]['assist_slot']


def test_assist_can_reopen_after_user_closed_the_view(context):
    from app.browser import BrowserError
    store, service, client, browser, manager, identifier = context
    service.open_browser_view = Mock(return_value={**store.account(identifier, private=True), 'cdp_port': 19301})
    service.browser_view = Mock(side_effect=BrowserError('account browser view is closed'))
    service.close_browser_view = Mock()
    manager.start(identifier)
    manager._publish(identifier, 'manual_required', verification_uri='https://dreamina.capcut.com/cli/authorize')
    manager.open_assist(identifier)
    manager.open_assist(identifier)
    assert service.open_browser_view.call_count == 2
    manager.cancel(identifier)
    manager._run(identifier)


def test_explicit_force_reauthorization_starts_new_challenge_without_logout(context):
    store, service, client, browser, manager, identifier = context
    challenge(client)
    client.identity.return_value = {'id': 'account-123', 'credits': 20}
    manager.start(identifier, force=True)
    manager._run(identifier)
    client.auth_start.assert_called_once_with(force=True)
    client.identity.assert_called_once_with()
    client.logout.assert_not_called()
    assert manager.status(identifier)['state'] == 'ready'


@pytest.mark.parametrize('enabled', [True, False])
def test_legacy_profile_is_verified_before_cli_identity_and_preserves_enabled_intent(context, monkeypatch, enabled):
    store, service, client, browser, manager, identifier = context
    store.update_account(identifier, {'login_user_id': '', 'watermark_ready': False,
        'agreement_ready': False, 'enabled': enabled, 'status': 'active' if enabled else 'disabled'})
    automation = Mock()
    automation.return_value.ensure_ready.return_value = {'identity_verified': True,
        'watermark_ready': True, 'agreement_ready': True,
        'identity': {'user_id': 'legacy-verified-id'}}
    monkeypatch.setattr('app.canvas_auth.AccountAutomation', automation)

    def identity():
        assert service.browser_runtime.open == 0
        assert service.browser_runtime.closed == 1
        saved = store.account(identifier, private=True)
        assert saved['login_user_id'] == 'legacy-verified-id'
        assert saved['watermark_ready'] and saved['agreement_ready']
        assert not saved['enabled']
        return {'id': 'legacy-verified-id', 'credits': 12}

    client.identity.side_effect = identity
    factory = Mock(return_value=client)
    manager.client_factory = factory
    execute(context)
    selected = factory.call_args.args[0]
    assert selected['login_user_id'] == 'legacy-verified-id'
    assert selected['proxy_url'] == 'socks5://proxy.test:20001'
    assert automation.call_args.kwargs['allow_initial_identity'] is True
    assert automation.call_args.args[0]['id'] == identifier
    assert manager.status(identifier)['state'] == 'ready'
    saved = store.account(identifier, private=True)
    assert bool(saved['enabled']) is enabled
    assert saved['status'] == ('active' if enabled else 'disabled')
    assert saved['login_enable_after'] == int(enabled)
    assert saved['setup_checked_at'] > 0
    client.auth_start.assert_not_called()
    client.submit.assert_not_called()
    automation.return_value.run.assert_not_called()


@pytest.mark.parametrize('stage', ['challenge_required', 'verification_required'])
def test_legacy_security_challenge_fails_without_cli_or_paid_submission(context, monkeypatch, stage):
    from app.account_automation import AutomationError
    store, service, client, browser, manager, identifier = context
    store.update_account(identifier, {'login_user_id': '', 'watermark_ready': False, 'agreement_ready': False})
    automation = Mock()
    automation.return_value.ensure_ready.side_effect = AutomationError(stage, 'private-cookie=secret')
    monkeypatch.setattr('app.canvas_auth.AccountAutomation', automation)
    execute(context)
    saved = store.account(identifier, private=True)
    assert manager.status(identifier)['state'] == 'failed'
    assert not saved['enabled'] and saved['login_user_id'] == ''
    assert not saved['watermark_ready'] and not saved['agreement_ready']
    assert 'secret' not in str(manager.status(identifier))
    assert service.browser_runtime.open == 0 and service.browser_runtime.closed == 1
    client.identity.assert_not_called()
    client.auth_start.assert_not_called()
    client.submit.assert_not_called()


@pytest.mark.parametrize('user_id', ['0', 0, None, '', True])
def test_legacy_zero_or_invalid_identity_never_fakes_verified_setup(context, monkeypatch, user_id):
    store, service, client, browser, manager, identifier = context
    store.update_account(identifier, {'login_user_id': '', 'watermark_ready': False, 'agreement_ready': False})
    automation = Mock()
    automation.return_value.ensure_ready.return_value = {'identity_verified': True,
        'watermark_ready': True, 'agreement_ready': True, 'identity': {'user_id': user_id}}
    monkeypatch.setattr('app.canvas_auth.AccountAutomation', automation)
    execute(context)
    saved = store.account(identifier, private=True)
    assert manager.status(identifier)['state'] == 'failed'
    assert not saved['enabled'] and saved['login_user_id'] == ''
    assert not saved['watermark_ready'] and not saved['agreement_ready']
    assert service.browser_runtime.open == 0 and service.browser_runtime.closed == 1
    client.identity.assert_not_called()
    client.auth_start.assert_not_called()
    client.submit.assert_not_called()


@pytest.mark.parametrize('missing', ['identity_verified', 'watermark_ready', 'agreement_ready'])
def test_legacy_missing_evidence_does_not_publish_ready_flags(context, monkeypatch, missing):
    store, service, client, browser, manager, identifier = context
    store.update_account(identifier, {'login_user_id': '', 'watermark_ready': False, 'agreement_ready': False})
    evidence = {'identity_verified': True, 'watermark_ready': True, 'agreement_ready': True,
        'identity': {'user_id': 'legacy-id'}, missing: False}
    automation = Mock()
    automation.return_value.ensure_ready.return_value = evidence
    monkeypatch.setattr('app.canvas_auth.AccountAutomation', automation)
    execute(context)
    saved = store.account(identifier, private=True)
    assert manager.status(identifier)['state'] == 'failed'
    assert not saved['enabled'] and saved['login_user_id'] == ''
    assert not saved['watermark_ready'] and not saved['agreement_ready']
    client.identity.assert_not_called()


def test_legacy_migration_never_borrows_an_account_in_manual_login(context, monkeypatch):
    store, service, client, browser, manager, identifier = context
    store.update_account(identifier, {'login_user_id': '', 'watermark_ready': False,
        'agreement_ready': False, 'status': 'logging_in', 'enabled': False})
    automation = Mock()
    monkeypatch.setattr('app.canvas_auth.AccountAutomation', automation)
    with pytest.raises(ValueError, match='browser login'):
        manager.start(identifier)
    automation.assert_not_called()
    client.identity.assert_not_called()
    assert service.browser_runtime.open == service.browser_runtime.closed == 0
