"""Exact media credential outages stay inside existing input recovery budgets."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import (preparation_retryable_failure, preparation_upload_credential_failure,
                               identity_network_error, _source_url_fallback_allowed)
from app.canvas_input_recovery import TICKET_KEY
from test_canvas_file_unknown_recovery import missing
from test_canvas_url_file_unknown_recovery import prepared_url
from test_canvas_execution import context, owner, task, fake_client


MESSAGE = 'The media-upload credential service is unavailable. Follow retryable and requiredAction in this error.'


def failure(**changes):
    return CanvasCLIError('cli.upload_credential_unavailable', MESSAGE, exit_code=21,
        outcome_unknown=False, details={'error': {'code': 'cli.upload_credential_unavailable',
            'class': 'retryable_failure', 'retryable': True, 'requiredAction': 'retry', **changes}})


def test_observed_full_typed_media_credential_outage_is_a_preparation_transient():
    original = failure()
    assert preparation_upload_credential_failure(original) and preparation_retryable_failure(original)
    assert not identity_network_error(original)  # No login/account-auth interpretation.
    assert not _source_url_fallback_allowed(original)  # Existing credential transport veto remains.
    original.message = 'Different localized diagnostic text'
    assert preparation_upload_credential_failure(original)  # Structured facts control it.


@pytest.mark.parametrize('change', [
    {'code': 'cli.authentication_required'}, {'class': 'authentication_failure'},
    {'class': 'business_failure'}, {'class': 'internal_failure'}, {'retryable': False},
    {'retryable': 'true'}, {'requiredAction': 'login'}, {'requiredAction': 'human_intervention'},
    {'requiredAction': 'fix_input'}, {'serviceCode': 1006}, {'serviceCode': 20115},
    {'reason': 'identity_mismatch'}, {'reason': 'content_policy'}, {'reason': 'unsupported_media'},
    {'accepted': True}, {'resources': ['original-output']}, {'receipt': True},
    {'dispatch_started': True}, {'outcome_unknown': True}, {'phase': 'run_dispatching'},
    {'localStorage': {'submissionPhase': 'after_submit'}}, {'localStorage': []},
])
def test_precise_outage_classifier_never_broadens_auth_policy_or_dispatch_errors(change):
    original = failure(**change)
    assert not preparation_upload_credential_failure(original)


@pytest.mark.parametrize('change', ['unknown', 'partial', 'different_code', 'wrong_envelope', 'dispatch'])
def test_top_level_conflicting_evidence_does_not_authorize_preparation_retry(change):
    original = failure()
    if change == 'unknown': original.outcome_unknown = True
    if change == 'partial': original.details['partialData'] = {'resources': ['original-output']}
    if change == 'different_code': original.code = 'cli.credential_unavailable'
    if change == 'wrong_envelope': original.details['error'] = []
    if change == 'dispatch': original.details['dispatch_started'] = True
    assert not preparation_upload_credential_failure(original)


def test_explicit_before_submit_metadata_still_cannot_override_prior_dispatch_evidence():
    original = failure(localStorage={'submissionPhase': 'before_submit'})
    assert preparation_upload_credential_failure(original)
    original.details['localStorage'] = {'submissionPhase': 'before_submit', 'receipt': True}
    assert not preparation_upload_credential_failure(original)


def inject_retry_outage(runner, clock):
    def retry():
        # The observed second FILE invocation spent 16.58 seconds obtaining
        # credentials; those seconds consume the original unknown ticket.
        clock['mono'] += 16.58
        clock['wall'] += 16.58
        raise failure()
    runner.on_retry = retry


def test_credential_outage_after_claim_only_observes_original_uuid_until_original_cutoff(prepared_url):
    runner, _, _, clock, prepare = prepared_url
    runner.mode = 'twice_unknown'
    inject_retry_outage(runner, clock)
    with pytest.raises(CanvasCLIError) as found: prepare()
    assert found.value.code == 'cli.upload_state_unknown'
    assert runner.url_writes == 1 and runner.file_writes == 2  # No fourth upload.
    ticket = runner.saved[-1]['upload_checkpoint'][TICKET_KEY][0]
    assert ticket['attempt'] == 1 and ticket['state'] == 'claimed'
    assert ticket['original_error']['code'] == 'cli.upload_state_unknown'
    assert clock['wall'] == ticket['observation_deadline_at']
    assert clock['mono'] == pytest.approx(120.5, rel=0, abs=1e-9)
    writes = [args for args in runner.calls if args[:2] == ['resource', 'upload']]
    assert len({args[-1] for args in writes}) == 1 and writes[1] == writes[2]
    last_write = max(index for index, args in enumerate(runner.calls) if args[:2] == ['resource', 'upload'])
    assert all(args[:2] == ['resource', 'get'] for args in runner.calls[last_write + 1:])


def test_late_original_registration_can_complete_after_retry_credential_outage_without_new_write(prepared_url):
    runner, _, _, clock, prepare = prepared_url
    runner.mode = 'twice_unknown'
    inject_retry_outage(runner, clock)
    original_run = runner.run
    def delayed_registration(account, args, **kwargs):
        if args[:2] == ['resource', 'get'] and runner.file_writes == 2:
            if clock['mono'] < 25:
                runner.calls.append(list(args)); raise missing(args[2])
            runner.mode = 'recover'
        return original_run(account, args, **kwargs)
    runner.run = delayed_registration
    result = prepare()
    assert result['phase'] == 'quoted' and runner.url_writes == 1 and runner.file_writes == 2
    assert result['upload_checkpoint']['pending_refs'] == []
    assert len(result['upload_checkpoint']['refs']) == 1
    ticket = result['upload_checkpoint'][TICKET_KEY][0]
    assert ticket['attempt'] == 1 and ticket['observation_deadline_at'] == 1120.5
    assert clock['wall'] < ticket['observation_deadline_at']
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm']) for args in runner.calls)


def test_auth_intervention_after_claim_is_not_hidden_by_the_media_outage_rule(prepared_url):
    runner, _, _, _, prepare = prepared_url
    rejected = failure(requiredAction='login')
    def retry(): raise rejected
    runner.on_retry = retry
    with pytest.raises(CanvasCLIError) as found: prepare()
    assert found.value is rejected and runner.url_writes == 1 and runner.file_writes == 2


def test_restart_after_outage_cannot_claim_another_upload_or_renew_the_unknown_window(prepared_url):
    runner, _, _, clock, prepare = prepared_url
    runner.mode = 'twice_unknown'
    inject_retry_outage(runner, clock)
    with pytest.raises(CanvasCLIError): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    previous_writes, previous_downloads = runner.file_writes, runner.downloads
    before = deepcopy(checkpoint[TICKET_KEY])
    with pytest.raises(CanvasCLIError): prepare(checkpoint=checkpoint)
    assert runner.file_writes == previous_writes and runner.downloads == previous_downloads
    assert runner.saved[-1]['upload_checkpoint'][TICKET_KEY] == before
    assert clock['wall'] == before[0]['observation_deadline_at']


def test_prepaid_credential_outage_can_reprepare_same_owner_then_generate_exactly_once(context):
    store, service, _ = context
    account = owner(store, balance=200)
    row = task(store)
    client = fake_client(store, service, account, quote=170)
    original, deadlines = client.prepare.side_effect, []
    def prepare(*args, **kwargs):
        deadlines.append(kwargs['deadline'])
        value = original(*args, **kwargs)
        if len(deadlines) == 1: raise failure()
        return value
    client.prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', return_value=client): service._run(row['id'])
    saved = store.task(row['id'])
    budget = saved['upstream_context']['preparation_retry_budget']
    assert saved['status'] == 'succeeded' and client.prepare.call_count == 2
    assert client.submit.call_count == 1 and deadlines[1] <= deadlines[0]
    assert [item['account_id'] for item in budget['attempts']] == [account['id'], account['id']]
    assert budget['deadline'] - budget['started_at'] <= 180


def test_repeated_prepaid_credential_outages_cannot_expand_three_preparations_two_owners(context):
    store, service, _ = context
    accounts = [owner(store, name, balance=200, priority=priority)
                for name, priority in [('first', 3), ('second', 2), ('third', 1)]]
    row = task(store)
    clients = {account['id']: fake_client(store, service, account, quote=170) for account in accounts}
    for client in clients.values():
        original = client.prepare.side_effect
        def prepare(*args, _original=original, **kwargs):
            _original(*args, **kwargs); raise failure()
        client.prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run(row['id'])
    saved = store.task(row['id'])
    budget = saved['upstream_context']['preparation_retry_budget']
    assert saved['status'] == 'failed' and saved['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
    assert [item['account_id'] for item in budget['attempts']] == [accounts[0]['id'], accounts[0]['id'], accounts[1]['id']]
    assert sum(client.prepare.call_count for client in clients.values()) == 3
    assert clients[accounts[2]['id']].prepare.call_count == 0
    assert all(client.submit.call_count == 0 for client in clients.values()) and saved['actual_cost'] is None
