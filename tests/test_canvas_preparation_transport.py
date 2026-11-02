"""Retry typed zero-run transport failures without replaying a paid dispatch."""
from copy import deepcopy
import time
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient, preparation_retryable_failure, preparation_transport_failure
from test_canvas_readonly_transport_retry import ACCOUNT, Runner, clock
from test_canvas_transport import prepare


def transport(*, unknown=False, error_changes=None, envelope_changes=None):
    error = {'code': 'cli.transport_failed', 'class': 'retryable_failure',
             'retryable': True, 'requiredAction': 'retry'}
    error.update(error_changes or {})
    details = {'schemaVersion': '1', 'ok': False, 'error': error}
    details.update(envelope_changes or {})
    return CanvasCLIError('cli.transport_failed',
        'The service could not be reached. Check network and proxy access, then retry safely.',
        details=details, exit_code=21, outcome_unknown=unknown)


@pytest.mark.parametrize('reason', [None, '', 'network_error', 'network_timeout',
                                    'transport_error', 'dns_resolution_failed', 'service_unavailable'])
def test_exact_typed_preparation_transport_contract_is_retryable_without_mutating_diagnostic(reason):
    failure = transport(error_changes={'reason': reason} if reason is not None else {})
    before = deepcopy(failure.details)
    assert preparation_transport_failure(failure)
    assert preparation_retryable_failure(failure)
    assert failure.details == before and failure.exit_code == 21 and not failure.outcome_unknown


@pytest.mark.parametrize('changes', [
    {'code': 'cli.authentication_required'}, {'code': None}, {'class': 'authentication_failure'},
    {'class': 'invalid_input'}, {'class': 'operation_incomplete'}, {'class': ''},
    {'retryable': False}, {'retryable': 1}, {'retryable': 'true'},
    {'requiredAction': 'resume'}, {'requiredAction': 'login'}, {'requiredAction': 'contact_support'},
    {'requiredAction': None}, {'serviceCode': 20114}, {'serviceCode': '1'},
    {'reason': 'content_policy_violation'}, {'reason': 'identity_mismatch'}, {'reason': 'unknown'},
    {'dispatch_started': True}, {'outcome_unknown': True}, {'phase': 'run_dispatching'},
    {'phase': 'accepted'}, {'phase': 'unknown'}, {'accepted': True},
    {'resources': [{'resourceId': 'original-output'}]}, {'operationRef': 'original-operation'},
])
def test_explicit_hard_rejection_or_dispatch_evidence_vetoes_transport_retry(changes):
    assert not preparation_transport_failure(transport(error_changes=changes))
    assert not preparation_retryable_failure(transport(error_changes=changes))


@pytest.mark.parametrize('changes', [
    {'partialData': {'items': [{'state': 'RUN_ACCEPTED'}]}},
    {'data': {'items': [{'state': 'accepted', 'resources': []}]}},
    {'data': {'operationRef': 'original-operation'}},
    {'dispatch_started': True}, {'outcome_unknown': True}, {'phase': 'run_dispatching'},
    {'receipt': {'accepted': True}}, {'submission_evidence': {'accepted': True}},
    {'resource_ids': ['original-output']}, {'localStorage': {'submissionPhase': 'after_submit'}},
    {'localStorage': {'submissionPhase': 'before_submit', 'operationRef': 'original-operation'}},
    {'localStorage': {'submissionPhase': 'before_submit', 'resources': ['original-output']}},
])
def test_partial_receipt_or_unknown_operation_in_envelope_prevents_preparation_retry(changes):
    assert not preparation_transport_failure(transport(envelope_changes=changes))
    assert not preparation_retryable_failure(transport(envelope_changes=changes))


def test_paid_outcome_unknown_vetoes_even_a_retry_recommendation():
    assert not preparation_retryable_failure(transport(unknown=True))
    assert preparation_retryable_failure(transport(envelope_changes={'localStorage': {'submissionPhase': 'before_submit'}}))


@pytest.mark.parametrize('arguments', [
    ['canvas', 'create', 'title', '--project-id', 'same-project'],
    ['node', 'create', 'video', '--project-id', 'same-project', '--node-id', 'same-node', '--update-id', 'same-update'],
])
def test_retryable_draft_writes_keep_original_ids_account_and_three_attempt_budget(clock, arguments):
    runner = Runner([transport(), transport()])
    result = CanvasClient(runner, ACCOUNT)._preparation_write(arguments, deadline=120)
    assert result == {'synthetic_response': True}
    assert len(runner.calls) == 3 and clock['sleeps'] == [0.25, 0.5]
    assert all(owner == ACCOUNT and args == arguments and options['deadline'] == 120
               for owner, args, options in runner.calls)
    assert not any(args[:2] == ['node', 'run'] for _, args, _ in runner.calls)


def test_draft_transport_retries_cannot_renew_or_cross_original_deadline(clock):
    runner = Runner([transport()] * 3)
    with pytest.raises(CanvasCLIError) as found:
        CanvasClient(runner, ACCOUNT)._preparation_write(
            ['canvas', 'create', 'title', '--project-id', 'same-project'], deadline=100.1)
    assert found.value.code == 'CLI_TASK_DEADLINE_EXCEEDED'
    assert len(runner.calls) == 1 and clock['now'] == pytest.approx(100.1)


@pytest.mark.parametrize('changes', [
    {'requiredAction': 'login'}, {'requiredAction': 'resume'}, {'class': 'authentication_failure'},
    {'class': 'business_rejection'}, {'serviceCode': 20114}, {'retryable': False},
])
def test_read_quote_transport_veto_cannot_be_bypassed_by_generic_network_reason(clock, changes):
    # Before this change, retryable=true + network_error bypassed the exact
    # transport classifier even when requiredAction/class explicitly vetoed it.
    failure = transport(error_changes={'reason': 'network_error', **changes})
    runner = Runner([failure])
    with pytest.raises(CanvasCLIError) as found:
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote', '--node-id', 'original-node'],
                                           read_only=True, deadline=120)
    assert found.value is failure and len(runner.calls) == 1 and not clock['sleeps']


def test_read_quote_typed_transport_retry_retains_original_command(clock):
    args = ['node', 'quote', '--project-id', 'same-project', '--node-id', 'same-node']
    runner = Runner([transport(), transport()])
    assert CanvasClient(runner, ACCOUNT)._call(args, read_only=True, deadline=120) == {'synthetic_response': True}
    assert len(runner.calls) == 3 and all(args == call[1] for call in runner.calls)


@pytest.mark.parametrize('failures', [1, 2, 3])
def test_confirm_transport_retries_before_a_single_paid_run_or_fails_zero_run(tmp_path, monkeypatch, failures):
    runner, client, context = prepare(tmp_path)
    execute = runner.run
    runner.calls.clear()
    remaining = failures

    def run(account, args, **kwargs):
        nonlocal remaining
        if args[:2] == ['node', 'confirm'] and remaining:
            remaining -= 1
            runner.calls.append(list(args))
            raise transport()
        return execute(account, args, **kwargs)

    monkeypatch.setattr(runner, 'run', run)
    monkeypatch.setattr('app.canvas_client.time.sleep', Mock())
    if failures == 3:
        with pytest.raises(CanvasCLIError) as found:
            client.submit(context, 20, persist=runner.persisted.append, deadline=time.monotonic() + 30)
        assert not found.value.outcome_unknown
        assert found.value.details['dispatch_started'] is False
        assert found.value.details['phase'] == 'confirming'
        assert sum(args[:2] == ['node', 'run'] for args in runner.calls) == 0
    else:
        assert client.submit(context, 20, persist=runner.persisted.append, deadline=time.monotonic() + 30)['phase'] == 'accepted'
        assert sum(args[:2] == ['node', 'run'] for args in runner.calls) == 1
    assert sum(args[:2] == ['node', 'confirm'] for args in runner.calls) == min(failures + 1, 3)
    assert 'never-save-this-token' not in str(runner.persisted)


def test_paid_run_retry_recommendation_does_not_reconfirm_reprepare_or_replay(tmp_path, monkeypatch):
    runner, client, context = prepare(tmp_path)
    runner.calls.clear();runner.run_failure = transport(unknown=True)
    monkeypatch.setattr('app.canvas_client.time.sleep', Mock())
    with pytest.raises(CanvasCLIError) as found:
        client.submit(context, 20, persist=runner.persisted.append)
    assert found.value.outcome_unknown and found.value.details['dispatch_started'] is True
    assert found.value.details['phase'] == 'run_dispatching'
    assert sum(args[:2] == ['node', 'run'] for args in runner.calls) == 1
    assert sum(args[:2] == ['node', 'confirm'] for args in runner.calls) == 1
    assert not any(args[:2] in (['canvas', 'create'], ['node', 'create'], ['resource', 'upload']) for args in runner.calls)
    assert not preparation_retryable_failure(found.value)


def test_existing_accepted_context_never_starts_another_confirmation(tmp_path):
    runner, client, context = prepare(tmp_path)
    client.submit(context, 20, persist=runner.persisted.append)
    runner.calls.clear()
    with pytest.raises(CanvasCLIError) as found:
        client.submit(context, 20)
    assert found.value.outcome_unknown and not runner.calls
