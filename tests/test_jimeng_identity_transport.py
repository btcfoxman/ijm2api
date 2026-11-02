"""Temporary CDP failures only retry a complete live identity verification."""
import json
from unittest.mock import Mock

import pytest

from app import jimeng_client
from app.browser import BrowserError, BrowserRejected, CDPTransportError
from app.jimeng_client import JimengReceiptMismatch, JimengWebClient


CONFIG = {'sec_uid': 'synthetic-sec-uid'}
PROFILE = {'uid': 'synthetic-user', 'sec_uid': CONFIG['sec_uid'], 'status': 0}


def timeout(*, retryable=True):
    return CDPTransportError('Runtime.evaluate', 'receive', True, 'WebSocketTimeoutException',
                             retryable=retryable)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(jimeng_client.time, 'sleep', Mock())
    return JimengWebClient({'id': 57, 'cdp_port': 19357, 'execution_mode': 'jimeng_web'})


def test_identity_transport_retry_restarts_complete_live_read_without_business_calls(client, monkeypatch):
    post = Mock(side_effect=[timeout(), CONFIG, PROFILE])
    live = Mock(return_value={'user_id': PROFILE['uid']})
    monkeypatch.setattr(client, '_post', post)
    monkeypatch.setattr(client.browser, 'evaluate', live)
    assert client.login_identity()['user_id'] == PROFILE['uid']
    assert [call.args[0] for call in post.call_args_list] == [
        '/mweb/v1/get_common_config', '/mweb/v1/get_common_config', '/mweb/v1/get_user_info']
    assert client.account['login_user_id'] == PROFILE['uid']
    jimeng_client.time.sleep.assert_called_once_with(.5)
    live.assert_called_once()
    assert client.browser.timeout == 90 and not hasattr(client.browser._command_local, 'budget')


def test_partial_identity_round_is_not_reused_after_transport_failure(client, monkeypatch):
    fresh = {'sec_uid': 'synthetic-fresh-sec'}
    profile = {**PROFILE, 'sec_uid': fresh['sec_uid']}
    post = Mock(side_effect=[CONFIG, timeout(), fresh, profile])
    monkeypatch.setattr(client, '_post', post)
    monkeypatch.setattr(client.browser, 'evaluate', Mock(return_value={'user_id': PROFILE['uid']}))
    assert client.login_identity()['user_id'] == PROFILE['uid']
    assert post.call_args_list[1].args[1] == {'sec_uid': CONFIG['sec_uid']}
    assert post.call_args_list[3].args[1] == {'sec_uid': fresh['sec_uid']}
    assert post.call_count == 4


def test_identity_transport_exhaustion_never_uses_stored_uid_as_live_proof(client, monkeypatch):
    client.account['login_user_id'] = PROFILE['uid']
    original, last = timeout(), timeout()
    post = Mock(side_effect=[original, last])
    live = Mock()
    monkeypatch.setattr(client, '_post', post)
    monkeypatch.setattr(client.browser, 'evaluate', live)
    with pytest.raises(CDPTransportError) as caught:
        client.login_identity()
    assert caught.value is last and post.call_count == 2
    live.assert_not_called()
    assert client.account['login_user_id'] == PROFILE['uid']


@pytest.mark.parametrize('failure', [BrowserRejected('Jimeng HTTP 401'),
    JimengReceiptMismatch('account changed'), BrowserError('invalid profile response'), timeout(retryable=False)])
def test_identity_auth_rejection_mismatch_and_nontransient_failures_do_not_retry(client, monkeypatch, failure):
    post = Mock(side_effect=failure)
    monkeypatch.setattr(client, '_post', post)
    with pytest.raises(type(failure)):
        client.login_identity()
    post.assert_called_once()
    jimeng_client.time.sleep.assert_not_called()
    assert 'login_user_id' not in client.account


def test_identity_final_live_uid_mismatch_cannot_bind_or_retry(client, monkeypatch):
    post = Mock(side_effect=[CONFIG, PROFILE])
    monkeypatch.setattr(client, '_post', post)
    monkeypatch.setattr(client.browser, 'evaluate', Mock(return_value={'user_id': 'foreign'}))
    with pytest.raises(JimengReceiptMismatch):
        client.login_identity()
    assert post.call_count == 2 and 'login_user_id' not in client.account
    jimeng_client.time.sleep.assert_not_called()


def test_identity_attempts_share_total_deadline_and_thread_local_per_command_cap(client, monkeypatch):
    clock, observed = [0.0], []
    monkeypatch.setattr(jimeng_client.time, 'monotonic', lambda: clock[0])
    def post(path, body):
        observed.append((path, client.browser._command_local.budget, client.browser.timeout))
        if len(observed) == 1:
            clock[0] = 10
            raise timeout()
        return CONFIG if path.endswith('get_common_config') else PROFILE
    def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(jimeng_client.time, 'sleep', Mock(side_effect=sleep))
    monkeypatch.setattr(client, '_post', post)
    monkeypatch.setattr(client.browser, 'evaluate', Mock(return_value={'user_id': PROFILE['uid']}))
    assert client.login_identity()['user_id'] == PROFILE['uid']
    assert len(observed) == 3 and all(budget == (30, 10) and default == 90
                                      for _, budget, default in observed)
    assert clock[0] == 10.5


def test_identity_cannot_start_another_attempt_when_total_budget_is_exhausted(client, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(jimeng_client.time, 'monotonic', lambda: clock[0])
    def fail(*_):
        clock[0] = 29.8
        raise timeout()
    post = Mock(side_effect=fail)
    monkeypatch.setattr(client, '_post', post)
    with pytest.raises(CDPTransportError):
        client.login_identity()
    post.assert_called_once()
    jimeng_client.time.sleep.assert_not_called()


def test_generation_evaluate_timeout_is_never_replayed_by_identity_read_policy(client, monkeypatch):
    evaluate = Mock(side_effect=timeout())
    monkeypatch.setattr(client.browser, 'evaluate', evaluate)
    ready = Mock(side_effect=AssertionError('single-shot write must not enter a retrying readiness gate'))
    monkeypatch.setattr(client, 'wait_until_ready', ready)
    with pytest.raises(CDPTransportError):
        client._post('/mweb/v1/aigc_draft/generate', {'submit_id': 'synthetic'}, one_shot=True)
    evaluate.assert_called_once()
    ready.assert_not_called()
    jimeng_client.time.sleep.assert_not_called()
    assert 'reconcileBySubmitId' in evaluate.call_args.args[0]


def test_readiness_budget_does_not_mutate_default_timeout(client, monkeypatch):
    clock, observed = [0.0], []
    monkeypatch.setattr(jimeng_client.time, 'monotonic', lambda: clock[0])
    def evaluate(_):
        observed.append((client.browser.timeout, client.browser._command_local.budget))
        if len(observed) == 1:
            return json.dumps({'transport_error': 'native-not-ready', 'stage': 'profile',
                'reason': 'profile-pending', 'did_dispatch': False})
        return json.dumps({'ready': True})
    monkeypatch.setattr(client.browser, 'evaluate', evaluate)
    monkeypatch.setattr(jimeng_client.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    client.wait_until_ready(timeout_seconds=1)
    assert observed == [(90, (1, 90)), (90, (1, 90))]
    assert client.browser.timeout == 90 and not hasattr(client.browser._command_local, 'budget')
