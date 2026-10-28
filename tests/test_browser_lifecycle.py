"""Managed timing sessions persist beyond login without changing account state."""
import json
from unittest.mock import Mock

import pytest

from app import browser
from app.browser import BrowserError
from app.browser_runtime import BrowserRuntime


def page(identity, *, url=browser.BASE + '/ai-tool/home'):
    return {'id': identity, 'type': 'page', 'url': url,
            'webSocketDebuggerUrl': 'ws://127.0.0.1/devtools/page/' + identity}


class TimingPeer:
    def __init__(self, target):
        self.target = target
        self.closed = False
        self.calls = []
        self.pending = []
        self.registered = False
        self.fail_refresh = False
        self.navigated_with_script = False

    def settimeout(self, _value):
        pass

    def send(self, raw):
        item = json.loads(raw)
        self.calls.append(item)
        method = item['method']
        if method == 'Page.addScriptToEvaluateOnNewDocument':
            self.registered = True
            result = {'identifier': 'registered-' + self.target['id']}
        elif method == 'Page.removeScriptToEvaluateOnNewDocument':
            self.registered = False
            result = {}
        elif method == 'Runtime.evaluate':
            if self.fail_refresh:
                raise OSError('synthetic disconnected telemetry session')
            result = {'result': {'value': self.target['url'].startswith(browser.BASE)}}
        elif method == 'Page.navigate':
            assert self.target['url'] == 'about:blank', 'Shared website tabs must not be navigated'
            self.navigated_with_script = self.registered
            self.target['url'] = item['params']['url']
            result = {'frameId': 'synthetic-frame'}
        else:
            result = {}
        self.pending.append({'id': item['id'], 'result': result})

    def recv(self):
        return json.dumps(self.pending.pop(0))

    def close(self):
        self.closed = True


@pytest.fixture
def timing(monkeypatch):
    monkeypatch.setattr(browser, '_TIMING_CAPTURES', {})
    monkeypatch.setattr(browser, '_TIMING_LOCKS', {})
    monkeypatch.setattr(browser, '_PROCESSES', {})
    targets, peers = {}, []
    monkeypatch.setattr(browser, '_page', lambda port: targets[port])

    def connect(endpoint, **_):
        target = next(target for target in targets.values() if target['webSocketDebuggerUrl'] == endpoint)
        peer = TimingPeer(target)
        peers.append(peer)
        return peer

    monkeypatch.setattr(browser.websocket, 'create_connection', connect)
    yield targets, peers
    for port in list(browser._TIMING_CAPTURES):
        browser.close_resource_timing_capture(port)


def test_existing_managed_browser_attaches_once_without_launching_or_navigating(timing, monkeypatch, tmp_path):
    targets, peers = timing
    targets[19316] = page('account-16')
    monkeypatch.setattr(browser, 'browser_alive', lambda _port: True)
    spawn = Mock(side_effect=AssertionError('a live managed page must not be relaunched'))
    monkeypatch.setattr(browser.subprocess, 'Popen', spawn)
    account = {'id': 16, 'proxy_url': 'socks5://proxy.invalid:10000'}
    for _ in range(2):
        assert browser.start_managed_browser(account, chrome='synthetic-chrome', profiles=tmp_path) == 19316
    assert len(peers) == 1 and not peers[0].closed and peers[0].registered
    methods = [item['method'] for item in peers[0].calls]
    assert methods.count('Page.addScriptToEvaluateOnNewDocument') == 1
    assert methods.count('Runtime.evaluate') == 2
    assert 'Page.navigate' not in methods and 'Page.reload' not in methods
    spawn.assert_not_called()


def test_lifecycle_session_survives_new_documents_until_explicit_close(timing):
    targets, peers = timing
    targets[19316] = page('account-16')
    browser.ensure_resource_timing_capture(19316)
    # Navigation retains the target ID. Its attached init script remains live
    # outside the short AccountAutomation.run context.
    targets[19316]['url'] = browser.BASE + '/ai-tool/home?projectId=synthetic'
    browser.ensure_resource_timing_capture(19316)
    assert len(peers) == 1 and peers[0].registered and not peers[0].closed
    browser.close_resource_timing_capture(19316)
    assert peers[0].closed and not peers[0].registered
    assert 19316 not in browser._TIMING_CAPTURES


def test_target_replacement_releases_old_session_and_keeps_accounts_isolated(timing):
    targets, peers = timing
    targets[19316], targets[19318] = page('old-16'), page('account-18')
    browser.ensure_resource_timing_capture(19316)
    browser.ensure_resource_timing_capture(19318)
    targets[19316] = page('replacement-16')
    browser.ensure_resource_timing_capture(19316)
    assert peers[0].closed and not peers[0].registered
    assert not peers[1].closed and peers[1].registered
    assert not peers[2].closed and peers[2].registered
    assert browser._TIMING_CAPTURES[19316][0] == 'replacement-16'
    browser.close_resource_timing_capture(19316)
    assert not peers[1].closed and 19318 in browser._TIMING_CAPTURES
    assert not any(item['method'] == 'Page.navigate' for peer in peers for item in peer.calls)


def test_dead_session_is_replaced_without_reloading_shared_tab(timing):
    targets, peers = timing
    targets[19316] = page('account-16')
    browser.ensure_resource_timing_capture(19316)
    peers[0].fail_refresh = True
    browser.ensure_resource_timing_capture(19316)
    assert len(peers) == 2 and peers[0].closed and peers[1].registered
    assert not any(item['method'] in {'Page.reload', 'Page.navigate'} for peer in peers for item in peer.calls)


def test_new_browser_owned_blank_registers_before_first_website_navigation(timing, monkeypatch, tmp_path):
    targets, peers = timing
    old_blank = page('preexisting-blank', url='about:blank')
    fresh_blank = page('new-owned-blank', url='about:blank')
    targets[19316] = fresh_blank
    monkeypatch.setattr(browser, 'browser_alive', lambda _port: False)
    listings = iter([[old_blank], [old_blank, fresh_blank]])
    monkeypatch.setattr(browser, '_targets', lambda _port: next(listings))
    spawn = Mock(return_value=Mock())
    monkeypatch.setattr(browser.subprocess, 'Popen', spawn)
    assert browser.start_managed_browser({'id': 16, 'proxy_url': 'socks5://proxy.invalid:10000'},
                                        chrome='synthetic-chrome', profiles=tmp_path) == 19316
    assert spawn.call_args.args[0][-1] == 'about:blank'
    assert len(peers) == 1 and peers[0].target['id'] == 'new-owned-blank'
    assert peers[0].navigated_with_script
    methods = [item['method'] for item in peers[0].calls]
    assert methods.index('Page.addScriptToEvaluateOnNewDocument') < methods.index('Page.navigate') < methods.index('Runtime.evaluate')
    assert peers[0].target['url'] == browser.BASE + '/ai-tool/generate'
    assert old_blank['url'] == 'about:blank'


@pytest.mark.parametrize('target,initial', [
    (page('foreign', url='https://unrelated.example.invalid'), False),
    (page('blank', url='about:blank'), False),
    (page('shared'), True),
])
def test_unowned_or_shared_tabs_cannot_receive_initial_navigation(timing, target, initial):
    _targets, peers = timing
    with pytest.raises(BrowserError):
        browser.ensure_resource_timing_capture(19316, _target=target, _initial_navigation=initial)
    assert peers == [] and browser._TIMING_CAPTURES == {}


def test_stop_cleans_timing_registration_before_closing_owned_browser(timing, monkeypatch):
    targets, peers = timing
    targets[19316] = page('account-16')
    browser.ensure_resource_timing_capture(19316)
    closed = []
    response = Mock()
    response.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{"webSocketDebuggerUrl":"ws://browser-close"}')))
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(browser.urllib.request, 'build_opener', lambda *_: opener)

    def connect(_endpoint, **_):
        assert peers[0].closed and not peers[0].registered
        peer = Mock()
        peer.send.side_effect = lambda raw: closed.append(json.loads(raw)['method'])
        return peer

    monkeypatch.setattr(browser.websocket, 'create_connection', connect)
    browser.stop_managed_browser(16)
    assert closed == ['Browser.close']
    assert browser._TIMING_CAPTURES == {}


@pytest.mark.parametrize('managed,owned', [(False, False), (True, True)])
def test_runtime_existing_compatible_and_external_ports_attach_only_telemetry(monkeypatch, managed, owned):
    account = {'id': 16, 'cdp_port': 9234, 'managed_profile': managed, 'status': 'active'}
    store = Mock()
    store.account.return_value = account.copy()
    runtime = BrowserRuntime(store, lambda: {})
    capture, start = Mock(), Mock(side_effect=AssertionError('external existing profiles must not be relaunched'))
    monkeypatch.setattr('app.browser_runtime.browser_alive', lambda _port: True)
    monkeypatch.setattr('app.browser_runtime.ensure_resource_timing_capture', capture)
    monkeypatch.setattr('app.browser_runtime.start_managed_browser', start)
    selected, selected_owned = runtime.acquire(account)
    assert selected == account and selected_owned is owned
    capture.assert_called_once_with(9234)
    store.update_account.assert_not_called()
    start.assert_not_called()
