"""Bounded preparation recovery and explicit caller failover, never paid replay."""
from copy import deepcopy
import json
import time
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient, preparation_retryable_failure
from app.canvas_service import CanvasServiceMixin
from test_canvas_execution import context, fake_client, owner, retained_operation, task
from test_canvas_preparation_transient import unavailable
from test_canvas_readonly_transport_retry import ACCOUNT, Runner, clock
from test_canvas_transport import FakeRunner, prepare, request


FACTS = {'may_have_been_accepted': True, 'financial_outcome': 'unconfirmed',
         'automatic_resubmission': False, 'fallback_allowed': True}


def invalid(**metadata):
    error = {'code': 'cli.invalid_service_response', 'class': 'internal_failure',
             'requiredAction': 'contact_support', 'retryable': False}
    error.update(metadata)
    return CanvasCLIError('cli.invalid_service_response', 'private diagnostic token=do-not-publish',
                          details={'error': error}, exit_code=1)


@pytest.mark.parametrize('failure', [invalid(), unavailable()])
@pytest.mark.parametrize('args', [
    ['canvas', 'create', 'original-title', '--project-id', 'original-project'],
    ['node', 'create', 'video', '--project-id', 'original-project', '--node-id', 'original-node', '--update-id', 'original-update'],
])
def test_preparation_writes_retry_same_durable_ids_only_three_times(clock, failure, args):
    runner = Runner([failure, failure])
    assert CanvasClient(runner, ACCOUNT)._preparation_write(args, deadline=120) == {'synthetic_response': True}
    assert len(runner.calls) == 3 and clock['sleeps'] == [0.25, 0.5]
    assert all(account == ACCOUNT and command == args and kwargs['deadline'] == 120
               for account, command, kwargs in runner.calls)


@pytest.mark.parametrize('args', [
    ['node', 'run', '--project-id', 'p', '--node-id', 'n'],
    ['node', 'create', 'video', '--project-id', 'p', '--node-id', 'n'],
    ['node', 'create', 'video', '--project-id', 'p', '--node-id', 'n', '--update-id', 'u', '--run'],
    ['node', 'create', 'video', '--project-id', 'p', '--node-id', 'n', '--update-id', 'u', '--run=true'],
    ['canvas', 'create', 'title'],
])
def test_preparation_retry_seam_cannot_generate_or_allocate_unbound_ids(clock, args):
    runner = Runner([])
    with pytest.raises(CanvasCLIError, match='original draft IDs'):
        CanvasClient(runner, ACCOUNT)._preparation_write(args, deadline=120)
    assert not runner.calls and not clock['sleeps']


@pytest.mark.parametrize('metadata', [
    {'class': 'authentication_failure'}, {'requiredAction': 'resume'},
    {'requiredAction': 'login'}, {'serviceCode': 20115},
    {'resources': [{'resourceId': 'already-accepted'}]}, {'accepted': True},
])
def test_invalid_response_does_not_override_auth_policy_or_receipt(clock, metadata):
    failure = invalid(**metadata)
    assert not preparation_retryable_failure(failure)
    runner = Runner([failure])
    with pytest.raises(CanvasCLIError):
        CanvasClient(runner, ACCOUNT)._preparation_write(['canvas', 'create', 'title', '--project-id', 'original'], deadline=120)
    assert len(runner.calls) == 1 and not clock['sleeps']


def test_preparation_write_backoff_never_renews_absolute_deadline(clock):
    runner = Runner([invalid()] * 3)
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT)._preparation_write(['canvas', 'create', 'title', '--project-id', 'original'], deadline=100.1)
    assert caught.value.code == 'CLI_TASK_DEADLINE_EXCEEDED'
    assert len(runner.calls) == 1 and clock['now'] == pytest.approx(100.1)


@pytest.mark.parametrize('failure', [invalid(), unavailable()])
def test_draft_retry_reuses_uploaded_materials_and_never_runs(tmp_path, monkeypatch, failure):
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    execute = runner.run
    remaining = 2

    def run(account, args, **kwargs):
        nonlocal remaining
        if args[:2] == ['node', 'create'] and remaining:
            remaining -= 1
            runner.calls.append(list(args))
            raise failure
        return execute(account, args, **kwargs)

    monkeypatch.setattr(runner, 'run', run)
    monkeypatch.setattr(client, '_sleep', Mock())
    image = tmp_path / 'reference.png'
    image.write_bytes(b'original-image')
    result = client.prepare(request(), 'same-task', {'images': [image], 'videos': [], 'audios': []}, persist=runner.persisted.append)
    drafts = [args for args in runner.calls if args[:2] == ['node', 'create']]
    assert len(drafts) == 3 and drafts[0] == drafts[1] == drafts[2]
    assert len([args for args in runner.calls if args[:2] == ['resource', 'upload']]) == 1
    assert result['phase'] == 'quoted'
    assert not any(args[:2] == ['node', 'run'] for args in runner.calls)


@pytest.mark.parametrize('failure', [invalid(), unavailable()])
def test_upload_exhaustion_remains_three_writes_not_nine(tmp_path, monkeypatch, failure):
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    monkeypatch.setattr(client, '_sleep', Mock())
    monkeypatch.setattr(client, '_resource_ready', Mock(side_effect=CanvasCLIError('MEDIA_UPLOAD_PENDING', 'pending')))
    calls = []

    def run(account, args, **kwargs):
        calls.append((account, deepcopy(args), kwargs))
        raise failure

    monkeypatch.setattr(runner, 'run', run)
    with pytest.raises(CanvasCLIError) as caught:
        client._upload_resource(tmp_path / 'original.png', 'image', 'same-resource', 'same-project')
    assert caught.value is failure and len(calls) == 3
    assert all(value[1] == calls[0][1] for value in calls)


def eligible():
    return {'id': 'original-task', 'status': 'failed', 'execution_backend': 'canvas_cli', 'account_id': 19,
        'error_code': 'CLI_SUBMISSION_UNCONFIRMED', 'actual_cost': None, 'estimated_cost': 40,
        'upstream_context': {'account_id': 19, 'profile': 'original-profile', 'project_id': 'original-project',
            'node_id': 'original-node', 'submit_id': 'original-submit', 'phase': 'quoted',
            'submission_phase': 'run_dispatching', 'financial_outcome': 'unconfirmed',
            'reconciliation_closed': True, 'reconciliation_close_reason': 'operation_submission_absent',
            'consecutive_submission_absent': 3, 'resource_ids': [],
            'last_query_error': {'code': 'cli.operation_submission_absent'},
            'submit_error': {'code': 'cli.invalid_service_response', 'class': 'internal_failure',
                'requiredAction': 'contact_support', 'retryable': False, 'phase': 'run_dispatching',
                'dispatch_started': True, 'outcome_unknown': True, 'message': 'private-body'},
            'preparation_timing': {'commands': {'node run': {'count': 1}}}}}


def test_historical_projection_is_pure_and_does_not_claim_no_send_or_refund():
    value = eligible()
    before = deepcopy(value)
    assert CanvasServiceMixin._canvas_retryable_submission_failure(value) == FACTS
    assert value == before
    assert not {'generation_request_sent', 'refunded', 'actual_cost'} & FACTS.keys()
    assert 'private' not in json.dumps(FACTS)


@pytest.mark.parametrize('change', [
    {'status': 'running'}, {'execution_backend': 'browser'}, {'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'},
    {'result_urls': ['existing-result']}, {'run_id': 'known-upstream'},
    {'upstream_context': {'resource_ids': ['existing-output']}},
    {'upstream_context': {'phase': 'accepted'}}, {'upstream_context': {'submission_phase': 'accepted'}},
    {'upstream_context': {'consecutive_submission_absent': 2}},
    {'upstream_context': {'consecutive_submission_absent': True}},
    {'upstream_context': {'last_query_error': {'code': 'CLI_COMMAND_TIMEOUT'}}},
    {'upstream_context': {'reconciliation_close_reason': 'deadline'}},
    {'upstream_context': {'financial_outcome': 'not_charged_this_invocation'}},
    {'upstream_context': {'submit_error': {'code': 'cli.authentication_required'}}},
    {'upstream_context': {'submit_error': {'code': 'service.20115'}}},
    {'upstream_context': {'submit_error': {'code': 'unknown-code', 'message': 'The service is temporarily unavailable. Retry this command.'}}},
    {'upstream_context': {'submit_error': {'dispatch_started': False}}},
    {'upstream_context': {'submit_error': {'outcome_unknown': False}}},
    {'upstream_context': {'previous_submissions': [{'submit_id': 'older-operation'}]}},
    {'upstream_context': {'run_receipt': {'state': 'ACCEPTED'}}},
    {'upstream_context': {'submission_evidence': {'version': 99}}},
    {'upstream_context': {'preparation_attempts': [{'reason': 'accepted'}]}},
    {'upstream_context': {'preparation_timing': {'commands': {'node run': {'count': 2}}}}},
    {'normalized': {'_execution': {'profile': 'another-profile'}}},
])
def test_retryable_terminal_contract_never_overrides_receipts_unknown_families_or_conflicts(change):
    value = eligible()
    for key, replacement in deepcopy(change).items():
        if key == 'upstream_context':
            for name, content in replacement.items():
                if name == 'submit_error':
                    value[key][name].update(content)
                else:
                    value[key][name] = content
        else:
            value[key] = replacement
    assert CanvasServiceMixin._canvas_retryable_submission_failure(value) == {}


def test_exact_absence_closes_known_failure_but_never_replays_or_rewrites_finance(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 60)
    original = eligible()['upstream_context']
    current = store.task('task')
    original.update(account_id=account['id'], expected_user_id=account['login_user_id'])
    original.pop('reconciliation_closed')
    original.pop('reconciliation_close_reason')
    original['consecutive_submission_absent'] = 0
    store.update_task('task', {'upstream_context': original, 'estimated_cost': 40, 'actual_cost': None})
    before = store.task('task')
    client = fake_client(store, service, account)
    client.poll.side_effect = [CanvasCLIError('cli.operation_submission_absent', 'not found') for _ in range(3)]
    service._poll_canvas('task', client, time.monotonic() + 60)
    result = store.task('task')
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_SUBMISSION_RETRYABLE'
    assert client.poll.call_count == 3
    client.submit.assert_not_called()
    client.prepare.assert_not_called()
    assert result['upstream_context']['submit_error'] == original['submit_error']
    for key in ('project_id', 'node_id', 'submit_id', 'profile'):
        assert result['upstream_context'][key] == before['upstream_context'][key]
    for key in ('estimated_cost', 'actual_cost', 'balance_before', 'balance_after', 'normalized'):
        assert result[key] == before[key]
    assert service._canvas_retryable_submission_failure(result) == FACTS


def test_invalid_response_before_dispatch_has_positive_stage_facts(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    client = fake_client(store, service, account)
    client.prepare.side_effect = invalid()
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    result = store.task('task')
    assert result['error_code'] == 'CLI_PREPARATION_TRANSIENT_FAILED'
    assert result['upstream_context']['pre_dispatch_failure']['generation_request_sent'] is False
    client.submit.assert_not_called()
    assert result['actual_cost'] is None


@pytest.mark.parametrize('partial', [
    {'resources': [{'resourceId': 'private-accepted-resource'}]},
    {'items': [{'state': 'RUN_ACCEPTED', 'resources': []}]},
])
def test_real_submit_redaction_retains_partial_receipt_veto_after_three_absent_queries(context, tmp_path, partial):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 60)
    runner, client, bound = prepare(tmp_path)
    runner.run_failure = CanvasCLIError('cli.invalid_service_response', 'private response',
        details={'error': {'code': 'cli.invalid_service_response', 'class': 'internal_failure',
                          'requiredAction': 'contact_support', 'retryable': False}, 'partialData': partial},
        outcome_unknown=True)
    with pytest.raises(CanvasCLIError):
        client.submit(bound, 20)
    assert bound['submission_receipt_evidence'] is True
    assert 'partialData' not in bound['submit_error']
    assert 'private-accepted-resource' not in json.dumps(bound)
    bound.update(account_id=account['id'], expected_user_id=account['login_user_id'])
    store.update_task('task', {'upstream_context': bound})
    poller = fake_client(store, service, account)
    poller.poll.side_effect = [CanvasCLIError('cli.operation_submission_absent', 'not found') for _ in range(3)]
    service._poll_canvas('task', poller, time.monotonic() + 60)
    result = store.task('task')
    assert result['error_code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert result['upstream_context']['submission_receipt_evidence'] is True
    assert service._canvas_retryable_submission_failure(result) == {}
    assert sum(args[:2] == ['node', 'run'] for args in runner.calls) == 1
    poller.submit.assert_not_called()
