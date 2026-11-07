"""A failed CDP read is neither an expired login nor a generation receipt."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.browser import BrowserRejected, CDPTransportError
from app.errors import RETRYABLE_GENERATION_MESSAGE
from app.service import AccountMaintenanceBusy
from test_jimeng_execution import context, create


def transport():
    return CDPTransportError('Runtime.evaluate', 'receive', True, 'WebSocketTimeoutException')


def unchanged_identity_and_balance(context, before):
    after = context.store.account(context.account['id'], private=True)
    for key in ('enabled', 'login_user_id', 'login_stage', 'setup_checked_at', 'balance', 'balance_details_json'):
        assert after[key] == before[key]


@pytest.mark.parametrize('method', ['login_identity', 'account_state', 'project_id'])
def test_maintenance_transport_exhaustion_is_one_network_failure_not_three_login_checks(context, monkeypatch, method):
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))
    before = context.store.account(context.account['id'], private=True)
    call = getattr(context.client.browser if method == 'project_id' else context.client, method)
    call.side_effect = transport()
    result = context.service.check_account(context.account['id'])
    assert result['status'] == 'network_error'
    call.assert_called_once()
    unchanged_identity_and_balance(context, before)
    context.service.browser_runtime.release.assert_called_once()


def test_transport_during_browser_acquisition_does_not_require_login_or_leak_a_lease(context):
    before = context.store.account(context.account['id'], private=True)
    context.service.browser_runtime.acquire.side_effect = transport()
    checked = context.service.check_account(context.account['id'])
    assert checked['status'] == 'network_error'
    unchanged_identity_and_balance(context, before)
    context.client.login_identity.assert_not_called()
    context.service.browser_runtime.release.assert_not_called()


def test_boolean_discovery_failure_cannot_misclassify_the_live_jimeng_transport(context, monkeypatch):
    probe = Mock(return_value=False)
    monkeypatch.setattr('app.service.browser_alive', probe)
    context.client.login_identity.side_effect = CDPTransportError(
        'Runtime.evaluate', 'target_lookup', False, 'TimeoutError')
    checked = context.service.check_account(context.account['id'])
    assert checked['status'] == 'network_error'
    probe.assert_not_called()


@pytest.mark.parametrize('failure', ['http401', 'missing_identity', 'foreign_identity'])
def test_real_rejection_or_missing_or_foreign_identity_still_blocks_login(context, monkeypatch, failure):
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))
    monkeypatch.setattr('app.service.time.sleep', Mock())
    if failure == 'http401':
        context.client.login_identity.side_effect = BrowserRejected('HTTP 401')
    elif failure == 'missing_identity':
        context.client.login_identity.return_value = {'authenticated': False}
    else:
        context.client.login_identity.return_value = {'authenticated': True, 'user_id': 'another-user'}
    checked = context.service.check_account(context.account['id'])
    assert checked['status'] == 'login_required'
    assert context.store.account(context.account['id'], private=True)['login_user_id'] == context.account['login_user_id']
    context.client.account_state.assert_not_called()
    context.client.submit_prepared.assert_not_called()


@pytest.mark.parametrize('late', [False, True])
def test_transport_before_dispatch_preserves_truthful_facts_and_never_replays(context, late):
    identifier = create(context)
    context.client.login_identity.side_effect = (
        [{'authenticated': True, 'user_id': context.account['login_user_id']}, transport()] if late else transport())
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'JIMENG_PROTOCOL_UNAVAILABLE'
    assert not result['run_id'] and not result['thread_id'] and result['actual_cost'] is None
    facts = result['upstream_context']['pre_dispatch_failure']
    assert facts['generation_request_sent'] is False and facts['automatic_resubmission'] is False
    assert facts['financial_outcome'] == 'not_charged_this_invocation'
    diagnostic = result['upstream_context']['cdp_transport_failure']
    assert diagnostic == {'code': 'CDP_TRANSPORT_FAILED', 'method': 'Runtime.evaluate', 'phase': 'receive',
                          'command_sent': True, 'exception_type': 'WebSocketTimeoutException', 'retryable': True}
    public = context.service.public_task(result)
    assert public['error']['message'] == RETRYABLE_GENERATION_MESSAGE
    assert public['provider_context']['generation_request_sent'] is False
    assert context.store.account(context.account['id'])['status'] == 'network_error'
    if not late:
        context.client.upload_inputs.assert_not_called()
    context.client.submit_prepared.assert_not_called()
    context.client.poll.assert_not_called()
    calls = context.client.login_identity.call_count
    context.service._run(identifier)
    context.service._resume_poll(identifier)
    assert context.client.login_identity.call_count == calls
    context.client.submit_prepared.assert_not_called()


@pytest.mark.parametrize('evidence', [
    {'run_id': 'existing-run'}, {'thread_id': 'existing-history'}, {'actual_cost': 0},
    {'upstream_context': {'submission_phase': 'accepted'}},
    {'upstream_context': {'upstream_task_id': 'existing-upstream'}},
    {'status': 'failed', 'error_code': 'MANUALLY_TERMINATED'},
    {'status': 'failed', 'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'},
])
def test_existing_acceptance_charge_or_terminal_state_cannot_be_reclassified_as_unsubmitted(context, evidence):
    identifier = create(context)
    context.store.update_task(identifier, {'account_id': context.account['id'], 'execution_backend': 'jimeng_web',
                                         'status': 'preparing', **evidence})
    before = deepcopy(context.store.task(identifier))
    assert context.service._jimeng_cdp_pre_dispatch_failure(identifier, context.account, transport()) is False
    assert context.store.task(identifier) == before


def test_late_transport_failure_cannot_overwrite_a_new_account_login(context, monkeypatch):
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))

    def changed_login():
        context.store.update_account(context.account['id'], {'login_user_id': 'new-user',
            'setup_checked_at': 999, 'status': 'active', 'balance': 456})
        raise transport()

    context.client.login_identity.side_effect = changed_login
    with pytest.raises(AccountMaintenanceBusy, match='binding changed'):
        context.service.check_account(context.account['id'])
    current = context.store.account(context.account['id'], private=True)
    assert current['login_user_id'] == 'new-user' and current['balance'] == 456 and current['status'] == 'active'


@pytest.mark.parametrize('transition', ['accepted', 'terminated'])
def test_late_transport_failure_preserves_a_concurrent_task_transition(context, transition):
    identifier = create(context)
    saved = []

    def concurrent_change():
        values = ({'status': 'submitted', 'run_id': context.prepared.submit_id,
                   'upstream_context': {'submission_phase': 'accepted', 'upstream_task_id': 'original-task'}}
                  if transition == 'accepted' else
                  {'status': 'failed', 'error_code': 'MANUALLY_TERMINATED',
                   'upstream_context': {'manual_termination': True}})
        saved.append(context.store.update_task(identifier, values))
        raise transport()

    context.client.login_identity.side_effect = concurrent_change
    context.service._run(identifier)
    assert context.store.task(identifier) == saved[0]
    context.client.submit_prepared.assert_not_called()


def test_transport_after_dispatch_keeps_the_original_query_and_cannot_claim_no_charge(context):
    identifier = create(context)
    context.client.submit_prepared.side_effect = transport()
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'succeeded' and result['run_id'] == context.prepared.submit_id
    assert 'pre_dispatch_failure' not in result['upstream_context']
    assert 'provider_context' not in context.service.public_task(result)
    context.client.submit_prepared.assert_called_once()
    context.client.poll.assert_called_once_with(context.prepared.submit_id, '')


def test_before_and_after_preparation_share_one_thirty_second_read_budget(context, monkeypatch):
    clock = [100.0]
    deadlines = []

    class Browser:
        @contextmanager
        def command_budget(self, deadline, *, timeout):
            deadlines.append((deadline, timeout))
            yield

    context.client.browser = Browser()
    monkeypatch.setattr('app.jimeng_service.time', SimpleNamespace(monotonic=lambda: clock[0]))
    budget = [30.0]
    with context.service._jimeng_verification_reads(context.client, budget):
        clock[0] += 23
    clock[0] += 400  # Media preparation must not consume the remaining verification reads.
    with context.service._jimeng_verification_reads(context.client, budget):
        clock[0] += 7
    assert deadlines == [(130.0, 10), (530.0, 10)]
    with pytest.raises(CDPTransportError):
        with context.service._jimeng_verification_reads(context.client, budget):
            pytest.fail('Spent verification budget was renewed')
