"""Retry only known preparation failures; never infer no-send from empty IDs."""
from copy import deepcopy
import json
import time
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient, preparation_service_unavailable
from app.canvas_service import CanvasServiceMixin
from app.errors import public_error
from app.service import Service
from test_canvas_readonly_transport_retry import ACCOUNT, Runner, clock
from test_canvas_confirmation_boundary import boundary
from test_canvas_execution import context, fake_client, owner, task
from test_canvas_transport import FakeRunner


PUBLIC = '生成提交失败，请稍后重试~'
FACTS = {'generation_request_sent': False,
         'financial_outcome': 'not_charged_this_invocation', 'automatic_resubmission': False}


def unavailable(**overrides):
    metadata = {'code': 'service.1', 'serviceCode': 1, 'class': 'retryable_failure',
                'retryable': True, 'requiredAction': 'retry'}
    metadata.update(overrides)
    return CanvasCLIError('service.1', 'The service is temporarily unavailable. Retry this command.',
                          details={'error': metadata}, exit_code=21, outcome_unknown=False)


@pytest.mark.parametrize('pair', [('auth', 'account'), ('model', 'list'), ('canvas', 'ls'),
    ('resource', 'get'), ('node', 'quote')])
def test_observed_typed_preparation_read_recovers_with_same_args_and_budget(clock, pair):
    runner = Runner([unavailable(), unavailable()])
    args = [*pair, 'original-id', '--project-id', 'original-project']
    assert CanvasClient(runner, ACCOUNT)._call(args, read_only=True, deadline=120) == {'synthetic_response': True}
    assert len(runner.calls) == 3 and clock['sleeps'] == [0.25, 0.5]
    assert all(account == ACCOUNT and arguments == args and kwargs['deadline'] == 120
               for account, arguments, kwargs in runner.calls)


@pytest.mark.parametrize('overrides', [
    {'serviceCode': 20114}, {'serviceCode': True}, {'serviceCode': None},
    {'class': 'invalid_input'}, {'class': 'operation_incomplete'},
    {'retryable': False}, {'retryable': 'true'}, {'requiredAction': 'resume'},
    {'requiredAction': 'human_intervention'}, {'requiredAction': 'login'},
    {'reason': 'identity_mismatch'}, {'reason': 'insufficient_credits'},
    {'resources': [{'resourceId': 'existing-output'}]}, {'accepted': True},
])
def test_typed_preparation_retry_never_ignores_an_explicit_veto(clock, overrides):
    failure = unavailable(**overrides)
    assert not preparation_service_unavailable(failure)
    runner = Runner([failure])
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT)._call(['resource', 'get', 'original-id'], read_only=True, deadline=120)
    assert caught.value is failure and len(runner.calls) == 1 and not clock['sleeps']


@pytest.mark.parametrize('pair', [('node', 'run'), ('node', 'create'), ('canvas', 'create'),
    ('resource', 'upload'), ('auth', 'refresh'), ('auth', 'login'), ('operation', 'status')])
def test_new_retry_branch_never_replays_mutations_or_reconciliation(clock, pair):
    failure = unavailable()
    runner = Runner([failure])
    with pytest.raises(CanvasCLIError):
        CanvasClient(runner, ACCOUNT)._call([*pair, 'original-id'], read_only=True, deadline=120)
    assert len(runner.calls) == 1 and not clock['sleeps']


def test_typed_read_exhaustion_keeps_original_failure_and_three_attempt_cap(clock):
    failure = unavailable()
    runner = Runner([failure] * 4)
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote'], read_only=True, deadline=120)
    assert caught.value is failure and len(runner.calls) == 3
    assert clock['sleeps'] == [0.25, 0.5]


def test_typed_read_requires_read_only_opt_in(clock):
    runner = Runner([unavailable()])
    with pytest.raises(CanvasCLIError):
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote'], deadline=120)
    assert len(runner.calls) == 1 and not clock['sleeps']


def test_typed_backoff_uses_original_deadline_without_an_extra_command(clock):
    runner = Runner([unavailable()] * 3)
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote'], read_only=True, deadline=100.1)
    assert caught.value.code == 'CLI_TASK_DEADLINE_EXCEEDED'
    assert len(runner.calls) == 1 and clock['now'] == pytest.approx(100.1)


def test_upload_keeps_existing_three_writes_not_nine(tmp_path, monkeypatch):
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    file = tmp_path / 'reference.png'
    file.write_bytes(b'image')
    monkeypatch.setattr(client, '_sleep', Mock())
    monkeypatch.setattr(client, '_resource_ready', Mock(side_effect=CanvasCLIError('MEDIA_UPLOAD_PENDING', 'Not registered yet')))
    original = runner.run

    def run(account, args, **kwargs):
        if args[:2] == ['resource', 'upload']:
            runner.calls.append(list(args))
            raise unavailable()
        return original(account, args, **kwargs)

    monkeypatch.setattr(runner, 'run', run)
    with pytest.raises(CanvasCLIError) as caught:
        client._upload_resource(file, 'image', 'same-resource', 'same-project')
    assert caught.value.code == 'service.1'
    assert len(runner.calls) == 3
    assert all(args[-1] == 'same-resource' for args in runner.calls)
    assert client._resource_ready.call_count == 3


def test_acknowledged_upload_retries_only_resource_reads(tmp_path, monkeypatch):
    runner = FakeRunner(tmp_path)
    original = runner.run
    attempts = {'reads': 0}

    def run(account, args, **kwargs):
        if args[:2] == ['resource', 'get']:
            attempts['reads'] += 1
            if attempts['reads'] < 3:
                runner.calls.append(list(args))
                raise unavailable()
        return original(account, args, **kwargs)

    monkeypatch.setattr(runner, 'run', run)
    client = CanvasClient(runner, ACCOUNT)
    monkeypatch.setattr(client, '_sleep', Mock())
    client._upload_resource(None, 'image', 'same-resource', 'same-project',
                            source_url='https://media.example.test/reference.png')
    assert sum(args[:2] == ['resource', 'upload'] for args in runner.calls) == 1
    assert sum(args[:2] == ['resource', 'get'] for args in runner.calls) == 3
    assert not any(args[:2] == ['node', 'run'] for args in runner.calls)


def test_acknowledged_upload_read_exhaustion_never_repeats_the_write(tmp_path, monkeypatch):
    runner = FakeRunner(tmp_path)
    original = runner.run

    def run(account, args, **kwargs):
        if args[:2] == ['resource', 'get']:
            runner.calls.append(list(args))
            raise unavailable()
        return original(account, args, **kwargs)

    monkeypatch.setattr(runner, 'run', run)
    client = CanvasClient(runner, ACCOUNT)
    monkeypatch.setattr(client, '_sleep', Mock())
    with pytest.raises(CanvasCLIError) as caught:
        client._upload_resource(None, 'image', 'same-resource', 'same-project',
                                source_url='https://media.example.test/reference.png')
    assert caught.value.code == 'service.1'
    assert sum(args[:2] == ['resource', 'upload'] for args in runner.calls) == 1
    assert sum(args[:2] == ['resource', 'get'] for args in runner.calls) == 3


def test_confirm_reuses_existing_three_attempt_loop_and_runs_only_once(boundary, monkeypatch):
    runner, client, binding, calls, _ = boundary
    execute = runner._execute
    count = 0

    def transient(account, args, timeout, *, deadline=None):
        nonlocal count
        if args[:2] == ['node', 'confirm']:
            count += 1
            if count < 3:
                calls.append((account['id'], ('node', 'confirm')))
                raise unavailable()
        return execute(account, args, timeout, deadline=deadline)

    monkeypatch.setattr(runner, '_execute', transient)
    monkeypatch.setattr('app.canvas_client.time.sleep', lambda delay: None)
    assert client.submit(binding, 20)['phase'] == 'accepted'
    assert sum(pair == ('node', 'confirm') for _, pair in calls) == 3
    assert sum(pair == ('node', 'run') for _, pair in calls) == 1


def test_future_preparation_transient_has_positive_no_send_facts_without_financial_rewrite(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing'})
    client = fake_client(store, service, account)
    prepare = client.prepare.side_effect

    def fail_prepare(*args, **kwargs):
        prepare(*args, **kwargs)
        raise unavailable()

    client.prepare.side_effect = fail_prepare
    with patch.object(service, 'canvas_client', return_value=client):
        service._execute_canvas('task', account, time.monotonic() + 30)
    row = store.task('task')
    before = deepcopy(row)
    assert row['error_code'] == 'CLI_PREPARATION_TRANSIENT_FAILED' and row['status'] == 'failed'
    assert row['actual_cost'] is None
    assert row['upstream_context']['preparation_error']['code'] == 'service.1'
    result = service.public_task(row)
    assert result['error'] == {'code': 'CLI_PREPARATION_TRANSIENT_FAILED', 'message': PUBLIC}
    assert result['provider_context'] == FACTS
    assert row == before
    client.submit.assert_not_called()
    client.poll.assert_not_called()


def test_confirmation_failure_before_run_can_emit_same_positive_stage_proof(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing'})
    client = fake_client(store, service, account)
    failure = unavailable()
    failure.details.update(dispatch_started=False, phase='confirming')
    client.submit.side_effect = failure
    with patch.object(service, 'canvas_client', return_value=client):
        service._execute_canvas('task', account, time.monotonic() + 30)
    row = store.task('task')
    assert row['error_code'] == 'CLI_PREPARATION_TRANSIENT_FAILED'
    assert service.public_task(row)['provider_context'] == FACTS
    assert row['upstream_context']['pre_dispatch_failure']['stage'] == 'confirming'
    client.poll.assert_not_called()


def test_original_run_evidence_veto_survives_new_attempt_timing_replacement(context):
    store, service, _ = context
    account = owner(store)
    task(store)
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing',
        'upstream_context': {'backend': 'canvas_cli', 'phase': 'preparing',
            'preparation_timing': {'commands': {'node run': {'count': 1}}}}})
    client = fake_client(store, service, account)
    prepare = client.prepare.side_effect

    def fail_prepare(*args, **kwargs):
        prepare(*args, **kwargs)
        raise unavailable()

    client.prepare.side_effect = fail_prepare
    with patch.object(service, 'canvas_client', return_value=client):
        service._execute_canvas('task', account, time.monotonic() + 30)
    row = store.task('task')
    assert 'pre_dispatch_failure' not in row['upstream_context']
    assert row['error_code'] == 'service.1' and row['actual_cost'] is None
    assert 'provider_context' not in service.public_task(row)
    client.submit.assert_not_called()
    client.poll.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'submission_phase': 'run_dispatching'}, {'phase': 'accepted'}, {'resource_ids': ['output']},
    {'dispatch_started_at': 123}, {'financial_outcome': 'unconfirmed'},
    {'previous_submissions': [{'submit_id': 'prior'}]},
    {'submit_error': {'outcome_unknown': True}},
    {'dispatch_started': True}, {'submit_error': {'dispatch_started': True}},
    {'phase': 'submission_unconfirmed'}, {'accepted': True}, {'operation_ref': 'existing'},
    {'reconciliation_closed': True}, {'automatic_resubmission_blocked': True},
    {'preparation_timing': {'commands': {'node run': {'count': 1}}}},
    {'preparation_timing': {'current_command': {'command_pair': ['node', 'run']}}},
    {'preparation_attempts': [{'timing': {'commands': {'node run': {'count': 1}}}}]},
])
def test_no_send_proof_is_not_derived_from_zero_resources_or_conflicting_evidence(changes):
    row = {'status': 'preparing', 'upstream_context': dict(changes)}
    assert CanvasServiceMixin._canvas_pre_dispatch_facts(row, unavailable(), False) == {}
    row.update(status='failed', error_code='CLI_PREPARATION_TRANSIENT_FAILED')
    row['upstream_context']['pre_dispatch_failure'] = {**FACTS, 'stage': 'preparation'}
    assert CanvasServiceMixin._canvas_public_pre_dispatch_facts(row) == {}


@pytest.mark.parametrize('status', ['submitted', 'running', 'submission_unknown', 'succeeded', 'failed'])
def test_preparing_failure_cannot_certify_an_already_dispatched_lifecycle(status):
    assert CanvasServiceMixin._canvas_pre_dispatch_facts({'status': status}, unavailable(), False) == {}


def test_historical_service_failure_is_neutral_but_does_not_gain_no_send_evidence():
    row = {'id': 'historical', 'model': 'sd-2-0', 'normalized': {'kind': 'video'},
           'created_at': 123, 'progress': 100, 'status': 'failed', 'result_urls': [],
           'error_code': 'service.1', 'error_message': 'private-token private-prompt',
           'actual_cost': None, 'upstream_context': {'resource_ids': []}}
    before = deepcopy(row)
    service = object.__new__(Service)
    service._refresh_canvas_result_urls = lambda value: value
    result = service.public_task(row)
    assert result['error'] == {'code': 'SERVICE.1', 'message': '生成服务暂时不可用，请稍后重试~'}
    assert 'provider_context' not in result and row == before
    assert 'private-' not in json.dumps(result)


def test_fdc490_paid_unknown_keeps_guard_and_no_false_no_send_projection():
    row = {'id': 'ijm_fdc4908673e446d5', 'model': 'sd-2-0', 'normalized': {'kind': 'video'},
           'created_at': 123, 'progress': 100, 'status': 'failed', 'result_urls': [],
           'error_code': 'CLI_SUBMISSION_UNCONFIRMED', 'error_message': 'private-original-response',
           'actual_cost': None, 'upstream_context': {'phase': 'quoted',
               'submission_phase': 'run_dispatching', 'financial_outcome': 'unconfirmed',
               'resource_ids': [], 'consecutive_submission_absent': 3,
               'submit_error': {'code': 'cli.invalid_service_response', 'outcome_unknown': True}}}
    before = deepcopy(row)
    service = object.__new__(Service)
    service._refresh_canvas_result_urls = lambda value: value
    result = service.public_task(row)
    assert result['error'] == public_error('CLI_SUBMISSION_UNCONFIRMED', '')
    assert '暂勿重复提交' in result['error']['message']
    assert 'provider_context' not in result and row == before
