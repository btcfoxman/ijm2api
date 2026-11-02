"""CDP transport boundaries stay bounded without replaying JavaScript."""
import json
import threading
from unittest.mock import Mock

import pytest
import websocket

from app import browser
from app.browser import BrowserError, CDPBrowser, CDPTransportError


TARGET = {'id': 'synthetic-tab', 'type': 'page', 'url': browser.JIMENG_BASE + '/ai-tool/home',
          'webSocketDebuggerUrl': 'ws://127.0.0.1/synthetic-private-debugger'}


class Peer:
    def __init__(self, replies=None):
        self.sent, self.timeouts, self.closed = [], [], False
        self.replies = replies or [json.dumps({'id': 1, 'result': {'result': {'value': True}}})]

    def settimeout(self, value):
        self.timeouts.append(value)

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr(browser, '_site_page', lambda *_, **__: dict(TARGET))
    return CDPBrowser(19357, site='jimeng')


def test_cdp_receive_timeout_is_typed_single_attempt_and_hides_expression(page, monkeypatch):
    peer = Peer([websocket.WebSocketTimeoutException('private debug URL and token')])
    connect = Mock(return_value=peer)
    monkeypatch.setattr(browser.websocket, 'create_connection', connect)
    with pytest.raises(CDPTransportError) as caught:
        page.evaluate("fetch('/generate', {body:'private prompt'})", await_promise=True)
    failure = caught.value
    assert failure.method == 'Runtime.evaluate' and failure.phase == 'receive'
    assert failure.command_sent is True and failure.retryable is True
    assert failure.exception_type == 'WebSocketTimeoutException'
    assert 'private' not in str(failure) and '/generate' not in str(failure)
    assert len(peer.sent) == 1 and peer.closed
    connect.assert_called_once()


def test_cdp_connection_failure_is_wrapped_before_any_command_send(page, monkeypatch):
    connect = Mock(side_effect=websocket.WebSocketTimeoutException('private websocket endpoint'))
    monkeypatch.setattr(browser.websocket, 'create_connection', connect)
    with pytest.raises(CDPTransportError) as caught:
        page.evaluate('private expression')
    assert caught.value.phase == 'connect' and caught.value.command_sent is False
    assert caught.value.retryable is True and 'private' not in str(caught.value)
    connect.assert_called_once()


def test_cdp_partial_send_failure_conservatively_records_send_attempt(page, monkeypatch):
    peer = Peer()
    peer.send = Mock(side_effect=OSError('partial write contained private input'))
    monkeypatch.setattr(browser.websocket, 'create_connection', Mock(return_value=peer))
    with pytest.raises(CDPTransportError) as caught:
        page.evaluate('private expression')
    assert caught.value.phase == 'send' and caught.value.command_sent is True
    assert caught.value.retryable is True and peer.closed
    peer.send.assert_called_once()


def test_cdp_close_error_cannot_replace_original_timeout(page, monkeypatch):
    peer = Peer([websocket.WebSocketTimeoutException()])
    peer.close = Mock(side_effect=OSError('close failed'))
    monkeypatch.setattr(browser.websocket, 'create_connection', Mock(return_value=peer))
    with pytest.raises(CDPTransportError) as caught:
        page.evaluate('true')
    assert caught.value.exception_type == 'WebSocketTimeoutException'


def test_cdp_each_command_uses_its_own_connection_and_id_scope(page, monkeypatch):
    first = Peer([websocket.WebSocketTimeoutException()])
    second = Peer([json.dumps({'method': 'Runtime.consoleAPICalled'}),
                   json.dumps({'id': 98, 'result': {'result': {'value': 'foreign'}}}),
                   json.dumps({'id': 1, 'result': {'result': {'value': 'original'}}})])
    connect = Mock(side_effect=[first, second])
    monkeypatch.setattr(browser.websocket, 'create_connection', connect)
    with pytest.raises(CDPTransportError):
        page.evaluate('first')
    assert page.evaluate('second') == 'original'
    assert connect.call_count == 2 and first.closed and second.closed
    assert first.sent[0]['id'] == second.sent[0]['id'] == 1


def test_cdp_discovery_connect_send_and_receive_share_one_absolute_budget(page, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(browser.time, 'monotonic', lambda: clock[0])
    def target(*_, **kwargs):
        assert kwargs['deadline'] == 10
        clock[0] += 3
        return dict(TARGET)
    monkeypatch.setattr(browser, '_site_page', target)
    peer = Peer()
    original_send = peer.send
    def send(raw):
        original_send(raw)
        clock[0] += 2
    peer.send = send
    def connect(*_, **kwargs):
        assert kwargs['timeout'] == 7
        clock[0] += 2
        return peer
    monkeypatch.setattr(browser.websocket, 'create_connection', connect)
    with page.command_budget(30, timeout=10):
        assert page.evaluate('true') is True
    assert peer.timeouts == [5, 3]
    assert page.timeout == 90


def test_cdp_page_discovery_http_timeout_uses_the_same_remaining_budget(monkeypatch):
    monkeypatch.setattr(browser.time, 'monotonic', lambda: 0.0)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps([TARGET]).encode()
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(browser.urllib.request, 'build_opener', Mock(return_value=opener))
    peer = Peer()
    monkeypatch.setattr(browser.websocket, 'create_connection', Mock(return_value=peer))
    page = CDPBrowser(19357, site='jimeng')
    with page.command_budget(1.25, timeout=10):
        assert page.evaluate('true') is True
    assert opener.open.call_args.kwargs['timeout'] == 1.25
    assert all(0 < value <= 1.25 for value in peer.timeouts)


def test_cdp_noise_frames_cannot_reset_the_absolute_command_deadline(page, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(browser.time, 'monotonic', lambda: clock[0])
    peer = Peer()
    def recv():
        clock[0] += .4
        return json.dumps({'method': 'Runtime.event'})
    peer.recv = recv
    monkeypatch.setattr(browser.websocket, 'create_connection', Mock(return_value=peer))
    with page.command_budget(1, timeout=10), pytest.raises(CDPTransportError) as caught:
        page.evaluate('true')
    assert caught.value.phase == 'receive' and peer.closed
    assert len(peer.timeouts) == 4 and clock[0] < 1.3


def test_cdp_thread_local_nested_budgets_restore_without_mutating_browser_timeout(page, monkeypatch):
    monkeypatch.setattr(browser.time, 'monotonic', lambda: 0.0)
    barrier = threading.Barrier(2)
    observed, errors = {}, []
    def run(name, deadline, cap):
        try:
            with page.command_budget(deadline, timeout=cap):
                barrier.wait(timeout=2)
                observed[name] = page._command_local.budget
                with page.command_budget(deadline + 10, timeout=cap + 1):
                    assert page._command_local.budget == (deadline, cap)
            assert not hasattr(page._command_local, 'budget')
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run, args=('a', 10, 3)),
               threading.Thread(target=run, args=('b', 20, 7))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert not errors
    assert observed == {'a': (10, 3), 'b': (20, 7)} and page.timeout == 90


def test_cdp_wrong_site_and_protocol_errors_are_not_transport_retries(page, monkeypatch):
    monkeypatch.setattr(browser, '_site_page', Mock(side_effect=BrowserError('open original Jimeng page')))
    connect = Mock()
    monkeypatch.setattr(browser.websocket, 'create_connection', connect)
    with pytest.raises(BrowserError) as caught:
        page.evaluate('true')
    assert not isinstance(caught.value, CDPTransportError)
    connect.assert_not_called()


@pytest.mark.parametrize('targets', [[], [dict(TARGET, url='https://other.example.test/')],
                                   [dict(TARGET, type='service_worker')]])
def test_missing_original_page_is_a_transport_failure_without_sending_javascript(monkeypatch, targets):
    monkeypatch.setattr(browser, '_targets', lambda *_, **__: list(targets))
    connect = Mock()
    monkeypatch.setattr(browser.websocket, 'create_connection', connect)
    original = CDPBrowser(19357, site='jimeng')
    with pytest.raises(CDPTransportError) as caught:
        original.evaluate('private identity expression')
    assert caught.value.phase == 'target_lookup' and caught.value.command_sent is False
    assert caught.value.exception_type == 'TargetUnavailable' and caught.value.retryable is True
    assert 'private' not in str(caught.value) and 'other.example.test' not in str(caught.value)
    connect.assert_not_called()
