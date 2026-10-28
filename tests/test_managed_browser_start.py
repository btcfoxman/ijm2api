"""Only a browser spawned by this failed start may be reclaimed."""
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import app.browser as browser
import app.browser_runtime as browser_runtime


class FakeProcess:
    def __init__(self, *, returncode=None, terminate_hangs=False, kill_hangs=False, terminate_error=None):
        self.returncode = returncode
        self.terminate_hangs = terminate_hangs
        self.kill_hangs = kill_hangs
        self.terminate_error = terminate_error
        self.actions = []
        self.waits = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.actions.append('terminate')
        if self.terminate_error:
            raise self.terminate_error
        if not self.terminate_hangs:
            self.returncode = -15

    def kill(self):
        self.actions.append('kill')
        if not self.kill_hangs:
            self.returncode = -9

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired('synthetic-chrome', timeout)
        return self.returncode


@pytest.fixture
def context(tmp_path, monkeypatch):
    # Cleanup must remain safe if this becomes a non-reentrant lock.
    monkeypatch.setattr(browser, '_PROCESS_LOCK', threading.Lock())
    monkeypatch.setattr(browser, '_PROCESSES', {})
    monkeypatch.setattr(browser, '_TIMING_CAPTURES', {})
    monkeypatch.setattr(browser, 'browser_alive', Mock(return_value=False))
    monkeypatch.setattr(browser, 'stop_managed_browser', Mock(side_effect=AssertionError('Must not recursively stop by account/port')))
    clock = {'now': 0.0}
    monkeypatch.setattr(browser, 'time', SimpleNamespace(monotonic=lambda: clock['now'],
        sleep=lambda delay: clock.update(now=clock['now'] + delay)))
    fresh = {'id': 'owned-blank', 'type': 'page', 'url': 'about:blank'}
    calls = {'targets': 0}
    def targets(_):
        calls['targets'] += 1
        return [] if calls['targets'] == 1 else [fresh]
    monkeypatch.setattr(browser, '_targets', targets)
    process = FakeProcess()
    popen = Mock(return_value=process)
    monkeypatch.setattr(browser.subprocess, 'Popen', popen)
    timing = MagicMock()
    def ensure(port, **kwargs):
        browser._TIMING_CAPTURES[port] = ('owned-blank', timing, Mock())
    ensure = Mock(side_effect=ensure)
    monkeypatch.setattr(browser, 'ensure_resource_timing_capture', ensure)
    account = {'id': 14, 'proxy_url': 'socks5://private-user:private-password@proxy.test:20001'}
    profile = tmp_path / 'profiles' / '14'
    profile.mkdir(parents=True)
    (profile / 'Cookies').write_bytes(b'preserve-existing-profile')
    return SimpleNamespace(account=account, profiles=tmp_path / 'profiles', profile=profile,
        process=process, popen=popen, timing=timing, ensure=ensure, fresh=fresh, clock=clock)


def start(ctx):
    return browser.start_managed_browser(ctx.account, chrome='synthetic-chrome', profiles=ctx.profiles)


@pytest.mark.parametrize('failure', [
    browser.BrowserError('Dreamina login resource timing page was not ready'),
    browser.BrowserError('Dreamina initial page navigation failed'),
    RuntimeError('private error via https://private-user:private-password@proxy.test/token'),
])
def test_failed_new_target_or_telemetry_cleans_only_owned_child(context, failure, caplog):
    ctx = context
    def fail(port, **kwargs):
        browser._TIMING_CAPTURES[port] = ('owned-blank', ctx.timing, Mock())
        raise failure
    ctx.ensure.side_effect = fail
    peer = FakeProcess()
    browser._PROCESSES[15] = peer
    with pytest.raises(browser.BrowserError) as raised:
        start(ctx)
    if isinstance(failure, browser.BrowserError):
        assert raised.value is failure
    else:
        assert str(raised.value) == 'managed Dreamina Chrome startup failed'
    assert ctx.process.actions == ['terminate'] and ctx.process.waits == [3]
    assert 14 not in browser._PROCESSES and 19314 not in browser._TIMING_CAPTURES
    ctx.timing.__exit__.assert_called_once_with(None, None, None)
    assert browser._PROCESSES[15] is peer and not peer.actions
    browser.stop_managed_browser.assert_not_called()
    assert (ctx.profile / 'Cookies').read_bytes() == b'preserve-existing-profile'
    assert 'account_id=14 stage=telemetry error_type=' in caplog.text
    assert 'private-user' not in caplog.text and 'private-password' not in caplog.text


@pytest.mark.parametrize('mode', ['cold_timeout', 'redirect', 'malformed_target'])
def test_target_discovery_failures_reclaim_new_child(context, monkeypatch, mode):
    ctx = context
    calls = {'n': 0}
    def targets(_):
        calls['n'] += 1
        if calls['n'] == 1 or mode == 'cold_timeout':
            return []
        if mode == 'redirect':
            return [{'id': 'redirect', 'type': 'page', 'url': 'https://unrelated.test/'}]
        return [None]
    monkeypatch.setattr(browser, '_targets', targets)
    with pytest.raises(browser.BrowserError) as raised:
        start(ctx)
    if mode == 'redirect':
        assert 'configure an account proxy' in str(raised.value)
    assert ctx.process.actions == ['terminate'] and 14 not in browser._PROCESSES
    ctx.ensure.assert_not_called()


def test_normal_start_preserves_child_profile_proxy_and_timing_until_release(context):
    ctx = context
    assert start(ctx) == 19314
    assert browser._PROCESSES[14] is ctx.process and not ctx.process.actions
    assert 19314 in browser._TIMING_CAPTURES
    ctx.timing.__exit__.assert_not_called()
    args = ctx.popen.call_args.args[0]
    assert '--user-data-dir=' + str(ctx.profile) in args
    assert '--proxy-server=' + ctx.account['proxy_url'] in args
    assert args[-1] == 'about:blank'


@pytest.mark.parametrize('registered', [False, True])
def test_existing_healthy_browser_timing_failure_never_closes_it(context, registered):
    ctx = context
    browser.browser_alive.return_value = True
    if registered:
        browser._PROCESSES[14] = ctx.process
    ctx.ensure.side_effect = browser.BrowserError('synthetic telemetry failure')
    with pytest.raises(browser.BrowserError):
        start(ctx)
    ctx.popen.assert_not_called()
    assert not ctx.process.actions
    if registered:
        assert browser._PROCESSES[14] is ctx.process
    browser.stop_managed_browser.assert_not_called()


def test_spawn_oserror_does_not_touch_existing_ports_or_peer_process(context):
    ctx = context
    peer = FakeProcess()
    browser._PROCESSES[15] = peer
    ctx.popen.side_effect = OSError('synthetic launch failure private-password')
    with pytest.raises(browser.BrowserError, match='startup failed'):
        start(ctx)
    assert browser._PROCESSES[15] is peer and not peer.actions
    assert not ctx.process.actions and 14 not in browser._PROCESSES
    browser.stop_managed_browser.assert_not_called()


def test_invalid_proxy_never_spawns_or_closes_any_process(context):
    ctx = context
    ctx.account['proxy_url'] = 'invalid'
    browser._PROCESSES[14] = ctx.process
    with pytest.raises(browser.BrowserError, match='account proxy'):
        start(ctx)
    ctx.popen.assert_not_called()
    assert not ctx.process.actions and browser._PROCESSES[14] is ctx.process


@pytest.mark.parametrize('failure', ['slow_terminate', 'terminate_error', 'already_exited', 'listener_close_error'])
def test_cleanup_escalates_with_bounded_wait_and_preserves_original_error(context, failure):
    ctx = context
    original = browser.BrowserError('original startup failure')
    if failure == 'slow_terminate':
        ctx.process.terminate_hangs = True
    elif failure == 'terminate_error':
        ctx.process.terminate_error = OSError('synthetic terminate failure')
    elif failure == 'already_exited':
        ctx.process.returncode = 1
    else:
        ctx.timing.__exit__.side_effect = RuntimeError('synthetic listener close failure')
    def fail(port, **kwargs):
        browser._TIMING_CAPTURES[port] = ('owned-blank', ctx.timing, Mock())
        raise original
    ctx.ensure.side_effect = fail
    with pytest.raises(browser.BrowserError) as raised:
        start(ctx)
    assert raised.value is original and 14 not in browser._PROCESSES
    assert all(timeout == 3 for timeout in ctx.process.waits)
    assert len(ctx.process.waits) <= 2
    if failure in {'slow_terminate', 'terminate_error'}:
        assert ctx.process.actions == ['terminate', 'kill']
    if failure == 'already_exited':
        assert not ctx.process.actions
    assert 19314 not in browser._TIMING_CAPTURES


def test_process_replaced_before_failure_is_not_signalled_or_listeners_closed(context):
    ctx = context
    replacement = FakeProcess()
    replacement_timing = MagicMock()
    def fail(port, **kwargs):
        browser._PROCESSES[14] = replacement
        browser._TIMING_CAPTURES[port] = ('replacement', replacement_timing, Mock())
        raise browser.BrowserError('synthetic owner changed')
    ctx.ensure.side_effect = fail
    with pytest.raises(browser.BrowserError):
        start(ctx)
    assert browser._PROCESSES[14] is replacement
    assert not ctx.process.actions and not replacement.actions
    replacement_timing.__exit__.assert_not_called()


def test_unconfirmed_kill_stays_registered_without_masking_startup_failure(context):
    ctx = context
    ctx.process.terminate_hangs = ctx.process.kill_hangs = True
    original = browser.BrowserError('original startup failure')
    ctx.ensure.side_effect = original
    with pytest.raises(browser.BrowserError) as raised:
        start(ctx)
    assert raised.value is original
    assert ctx.process.actions == ['terminate', 'kill'] and ctx.process.waits == [3, 3]
    assert browser._PROCESSES[14] is ctx.process


def test_failed_acquire_has_no_lease_and_no_leaked_new_process(context):
    ctx = context
    account = {**ctx.account, 'status': 'active', 'managed_profile': True, 'cdp_port': 19314}
    store = Mock()
    store.account.return_value = account
    runtime = browser_runtime.BrowserRuntime(store, lambda: {
        'chrome_executable': 'synthetic-chrome', 'chrome_user_data_root': str(ctx.profiles)})
    ctx.ensure.side_effect = browser.BrowserError('synthetic telemetry setup failure')
    with pytest.raises(browser.BrowserError):
        runtime.acquire(account)
    assert runtime._leases == {} and not runtime.busy(14)
    assert 14 not in browser._PROCESSES and ctx.process.actions == ['terminate']


@pytest.mark.parametrize('managed', [False, True])
def test_custom_existing_browser_timing_failure_never_starts_or_stops_it(context, monkeypatch, managed):
    ctx = context
    account = {**ctx.account, 'status': 'active', 'managed_profile': managed, 'cdp_port': 29999}
    store = Mock()
    store.account.return_value = account
    runtime = browser_runtime.BrowserRuntime(store, lambda: {})
    monkeypatch.setattr(browser_runtime, 'browser_alive', Mock(return_value=True))
    ensure = Mock(side_effect=browser.BrowserError('synthetic borrowed telemetry failure'))
    monkeypatch.setattr(browser_runtime, 'ensure_resource_timing_capture', ensure)
    start_mock, stop_mock, close_mock = Mock(), Mock(), Mock()
    monkeypatch.setattr(browser_runtime, 'start_managed_browser', start_mock)
    monkeypatch.setattr(browser_runtime, 'stop_managed_browser', stop_mock)
    monkeypatch.setattr(browser_runtime, 'close_browser_port', close_mock)
    with pytest.raises(browser.BrowserError):
        runtime.acquire(account)
    start_mock.assert_not_called()
    stop_mock.assert_not_called()
    close_mock.assert_not_called()
    assert runtime._leases == {}


def test_old_target_at_same_port_is_never_selected_or_closed_on_new_start_failure(context, monkeypatch):
    ctx = context
    old = {'id': 'pre-existing-target', 'type': 'page', 'url': 'about:blank'}
    calls = {'n': 0}
    def targets(_):
        calls['n'] += 1
        return [old] if calls['n'] == 1 else [old, ctx.fresh]
    monkeypatch.setattr(browser, '_targets', targets)
    close = Mock(side_effect=AssertionError('Do not close a shared port'))
    monkeypatch.setattr(browser, 'close_browser_port', close)
    def fail(port, **kwargs):
        assert kwargs['_target']['id'] == ctx.fresh['id']
        raise browser.BrowserError('synthetic new target initialization failure')
    ctx.ensure.side_effect = fail
    with pytest.raises(browser.BrowserError):
        start(ctx)
    close.assert_not_called()
    assert ctx.process.actions == ['terminate']


def test_interrupted_start_still_cleans_only_new_owned_child(context):
    ctx = context
    ctx.ensure.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        start(ctx)
    assert ctx.process.actions == ['terminate'] and 14 not in browser._PROCESSES
