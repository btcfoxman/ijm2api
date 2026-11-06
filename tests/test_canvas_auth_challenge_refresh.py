"""An expired device flow permits one fresh ordinary login, never a reset."""
import logging
from unittest.mock import call

import pytest

from app.canvas_cli import CanvasCLIError
from test_canvas_auth import context


def response(code, *, expires=None, uri=None):
    challenge = {'deviceCode': code, 'userCode': 'PRIVATE_USER_CODE',
        'verificationUri': 'https://dreamina.capcut.com/ai-tool/cli-auth-os',
        'verificationUriComplete': uri or 'https://dreamina.capcut.com/ai-tool/cli-auth-os?code=' + code}
    if expires is not None:
        challenge['expiresAt'] = expires
    return {'status': 'authorization_required', 'challenge': challenge}


@pytest.fixture
def flow(context, monkeypatch):
    store, service, client, browser, manager, identifier = context
    clock = [0.0]
    monkeypatch.setattr('app.canvas_auth.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.canvas_auth.time.time', lambda: 1700000000.0 + clock[0])
    client.identity.side_effect = [CanvasCLIError('cli.authentication_required', 'PRIVATE'),
                                   {'id': 'account-123', 'credits': 20}]
    client.auth_start.side_effect = [response('PRIVATE_OLD'), response('PRIVATE_NEW')]
    client.auth_wait.side_effect = [CanvasCLIError('cli.login_expired', 'PRIVATE'), {'status': 'logged_in'}]
    manager.start(identifier)
    yield context, clock
    client.logout.assert_not_called()
    client.submit.assert_not_called()


def run(flow):
    context, _ = flow
    *_, manager, identifier = context
    manager._run(identifier)
    return context


def test_refresh_is_ordinary_fresh_and_only_after_old_lease_closes(flow, caplog):
    context, clock = flow
    store, service, client, browser, manager, identifier = context
    starts = []

    def start():
        starts.append(service.browser_runtime.closed)
        assert service.browser_runtime.open == 0
        assert not manager._jobs[identifier].get('assist')
        assert not manager._jobs[identifier].get('browser_active')
        if len(starts) == 2:
            assert not manager.status(identifier).get('verification_uri_complete')
        return response('PRIVATE_OLD' if len(starts) == 1 else 'PRIVATE_NEW', expires=1700000060)

    def wait(code, timeout):
        assert service.browser_runtime.open == 1
        if code == 'PRIVATE_OLD':
            clock[0] = 4
            raise CanvasCLIError('cli.login_expired', 'PRIVATE')
        return {'status': 'logged_in'}

    client.auth_start.side_effect = start
    client.auth_wait.side_effect = wait
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    run(flow)
    assert starts == [0, 1]
    assert client.auth_start.call_args_list == [call(), call()]
    assert client.auth_wait.call_args_list == [call('PRIVATE_OLD', timeout=5), call('PRIVATE_NEW', timeout=5)]
    assert manager.status(identifier)['state'] == 'ready' and store.account(identifier)['enabled']
    assert service.browser_runtime.closed == 2 and service.browser_runtime.open == 0
    for event in ('challenge_created', 'consent_dispatched', 'first_poll', 'challenge_expired', 'refresh_requested'):
        assert 'event=' + event in caplog.text
    assert 'attempt=2 elapsed_s=4 remaining_s=11 ttl_s=56 expired=False' in caplog.text
    assert 'PRIVATE' not in caplog.text and 'https://' not in caplog.text and 'account-123' not in caplog.text


@pytest.mark.parametrize('replacement', [response('PRIVATE_OLD'),
    response('PRIVATE_NEW', uri='https://attacker.invalid/?code=PRIVATE'),
    response('PRIVATE_NEW', expires=1699999999), response('PRIVATE_NEW', expires='PRIVATE_INVALID'),
    response('PRIVATE_NEW', expires=float('inf'))])
def test_identical_missing_untrusted_or_expired_renewal_never_opens_or_polls(flow, replacement):
    context, _ = flow
    store, service, client, browser, manager, identifier = context
    client.auth_start.side_effect = [response('PRIVATE_OLD'), replacement]
    run(flow)
    assert client.auth_start.call_count == 2 and client.auth_wait.call_count == 1
    assert browser.open.call_count == 1 and service.browser_runtime.closed == 1
    assert client.identity.call_count == 1 and not store.account(identifier)['enabled']
    assert manager.status(identifier)['state'] in {'failed', 'expired'}


def test_second_expiration_is_terminal_without_a_third_login(flow):
    context, _ = flow
    store, service, client, browser, manager, identifier = context
    client.auth_wait.side_effect = CanvasCLIError('cli.login_expired', 'PRIVATE')
    run(flow)
    assert client.auth_start.call_count == client.auth_wait.call_count == 2
    assert manager.status(identifier)['state'] == 'failed' and not store.account(identifier)['enabled']
    assert service.browser_runtime.closed == 2


@pytest.mark.parametrize('code', ['cli.transport_failed', 'cli.service_unavailable',
    'cli.authorization_expired', 'CLI_LOGIN_EXPIRED', 'cli.login_denied'])
def test_other_errors_do_not_refresh(flow, code):
    context, _ = flow
    store, service, client, browser, manager, identifier = context
    client.auth_wait.side_effect = CanvasCLIError(code, 'PRIVATE')
    run(flow)
    assert client.auth_start.call_count == client.auth_wait.call_count == 1
    assert manager.status(identifier)['state'] == 'failed' and not store.account(identifier)['enabled']


def test_identity_error_with_same_code_does_not_refresh(flow):
    context, _ = flow
    store, service, client, browser, manager, identifier = context
    client.auth_wait.side_effect = None
    client.auth_wait.return_value = {'status': 'logged_in'}
    client.identity.side_effect = [CanvasCLIError('cli.authentication_required', 'PRIVATE'),
                                  CanvasCLIError('cli.login_expired', 'PRIVATE')]
    run(flow)
    assert client.auth_start.call_count == 1 and client.identity.call_count == 2
    assert manager.status(identifier)['state'] == 'failed' and not store.account(identifier)['enabled']


@pytest.mark.parametrize('action', ['cancel', 'close', 'budget'])
def test_no_renewal_after_cancel_shutdown_or_original_budget(flow, action):
    context, clock = flow
    store, service, client, browser, manager, identifier = context

    def expired(*args, **kwargs):
        if action == 'cancel':
            manager.cancel(identifier)
        elif action == 'close':
            manager._closed = True
        else:
            clock[0] = 15
        raise CanvasCLIError('cli.login_expired', 'PRIVATE')

    client.auth_wait.side_effect = expired
    run(flow)
    assert client.auth_start.call_count == client.auth_wait.call_count == 1
    assert service.browser_runtime.closed == 1 and not store.account(identifier)['enabled']
    assert manager.status(identifier)['state'] == ('expired' if action == 'budget' else 'cancelled')


def test_refresh_does_not_restart_overall_budget_or_accept_late_wait(flow):
    context, clock = flow
    store, service, client, browser, manager, identifier = context

    def wait(code, timeout):
        if code == 'PRIVATE_OLD':
            clock[0] = 12
            raise CanvasCLIError('cli.login_expired', 'PRIVATE')
        clock[0] = 15
        return {'status': 'logged_in'}

    client.auth_wait.side_effect = wait
    run(flow)
    assert client.auth_start.call_count == 2 and client.auth_wait.call_count == 2
    assert manager.status(identifier)['state'] == 'expired' and not store.account(identifier)['enabled']
    assert client.identity.call_count == 1 and service.browser_runtime.closed == 2


@pytest.mark.parametrize('attempt', [1, 2])
def test_auth_start_exhausting_budget_never_opens_that_browser(flow, attempt):
    context, clock = flow
    store, service, client, browser, manager, identifier = context
    calls = []

    def start():
        calls.append(1)
        if len(calls) == attempt:
            clock[0] = 15
        return response('PRIVATE_OLD' if len(calls) == 1 else 'PRIVATE_NEW')

    client.auth_start.side_effect = start
    run(flow)
    assert browser.open.call_count == client.auth_wait.call_count == attempt - 1
    assert client.identity.call_count == 1 and not store.account(identifier)['enabled']
    assert manager.status(identifier)['state'] == 'expired'


def test_browser_startup_consuming_challenge_ttl_does_not_click_or_poll(flow):
    context, clock = flow
    store, service, client, browser, manager, identifier = context
    original_lease = service.browser_runtime.lease

    def lease(account):
        clock[0] = 6
        return original_lease(account)

    service.browser_runtime.lease = lease
    client.auth_start.side_effect = [response('PRIVATE_OLD', expires=1700000005)]
    run(flow)
    browser.open.assert_not_called()
    browser.step.assert_not_called()
    client.auth_wait.assert_not_called()
    assert service.browser_runtime.closed == 1 and service.browser_runtime.open == 0
    assert manager.status(identifier)['state'] == 'expired' and not store.account(identifier)['enabled']


def test_initial_force_intent_is_not_repeated_by_renewal(flow):
    context, _ = flow
    store, service, client, browser, manager, identifier = context
    manager._jobs[identifier]['force'] = True
    client.identity.side_effect = None
    client.identity.return_value = {'id': 'account-123', 'credits': 20}
    run(flow)
    assert client.auth_start.call_args_list == [call(force=True), call()]
    assert manager.status(identifier)['state'] == 'ready'


def test_fresh_renewal_still_rejects_wrong_identity(flow):
    context, _ = flow
    store, service, client, browser, manager, identifier = context
    client.identity.side_effect = [CanvasCLIError('cli.authentication_required', 'PRIVATE'),
                                  {'id': 'other-identity', 'credits': 20}]
    run(flow)
    assert client.auth_start.call_count == 2
    assert manager.status(identifier)['state'] == 'failed' and not store.account(identifier)['enabled']


@pytest.mark.parametrize('identity', ['matching', 'wrong', 'late', 'cancel'])
def test_renewal_without_challenge_still_requires_timely_verified_identity(flow, identity):
    context, clock = flow
    store, service, client, browser, manager, identifier = context
    client.auth_start.side_effect = [response('PRIVATE_OLD'), {'status': 'reused'}]

    def identify():
        if client.identity.call_count == 1:
            raise CanvasCLIError('cli.authentication_required', 'PRIVATE')
        if identity == 'late':
            clock[0] = 15
        elif identity == 'cancel':
            manager.cancel(identifier)
        return {'id': 'other' if identity == 'wrong' else 'account-123', 'credits': 20}

    client.identity.side_effect = identify
    run(flow)
    assert client.auth_start.call_count == 2 and client.auth_wait.call_count == 1
    assert client.identity.call_count == 2 and browser.open.call_count == 1
    assert bool(store.account(identifier)['enabled']) is (identity == 'matching')
    assert manager.status(identifier)['state'] == {
        'matching': 'ready', 'wrong': 'failed', 'late': 'expired', 'cancel': 'cancelled'}[identity]


@pytest.mark.parametrize('expiry', [1700000060, 1700000060000, '2023-11-14T22:14:20Z'])
def test_challenge_expiry_formats_share_same_safe_ttl(flow, caplog, expiry):
    context, _ = flow
    store, service, client, browser, manager, identifier = context
    client.auth_start.side_effect = [response('PRIVATE_OLD', expires=expiry), response('PRIVATE_NEW', expires=expiry)]
    caplog.set_level(logging.WARNING, logger='app.canvas_auth')
    run(flow)
    assert manager.status(identifier)['state'] == 'ready'
    assert 'ttl_s=60 expired=False' in caplog.text and 'PRIVATE' not in caplog.text
