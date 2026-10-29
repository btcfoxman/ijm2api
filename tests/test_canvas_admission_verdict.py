"""Official admission verdicts cannot be replaced by generation-state guesses."""
from copy import deepcopy
import json
import time

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError, admission_verdict
from app.canvas_service import CanvasServiceMixin
from test_canvas_confirmation_boundary import boundary, TOKEN
from test_canvas_execution import context, fake_client, owner, retained_operation
from test_canvas_transport import ACCOUNT, patch_process


@pytest.mark.parametrize('state,expected', [
    ('ACCEPTED', 'accepted'), (' accepted ', 'accepted'), (' RUN_ACCEPTED ', 'accepted'),
    ('run_rejected', 'rejected'), (' REJECTED ', 'rejected'),
    ('running', 'unknown'), ('processing', 'unknown'), ('completed', 'unknown'),
    ('success', 'unknown'), ('unknown', 'unknown'), ('RUN_ ACCEPTED', 'unknown'),
    ('', 'unknown'), (None, 'unknown'), (True, 'unknown'), (42, 'unknown'),
])
def test_official_admission_verdict_is_separate_from_generation_states(state, expected):
    assert admission_verdict(state) == expected


def _run_reply(boundary, state, error=None, resources=None):
    runner, client, context, calls, response = boundary
    execute = runner._execute

    def reply(account, args, timeout, *, deadline=None):
        envelope = execute(account, args, timeout, deadline=deadline)
        if args[:2] == ['node', 'run']:
            item = envelope['data']['items'][0]
            item['state'] = state
            item['resources'] = resources or []
            if error is not None:
                item['error'] = error
        return envelope

    runner._execute = reply
    return runner, client, context, calls


@pytest.mark.parametrize('state', ['accepted', ' ACCEPTED ', ' RUN_ACCEPTED '])
def test_run_acceptance_keeps_original_identity_and_dispatches_once(boundary, state):
    runner, client, context, calls = _run_reply(boundary, state)
    saved = []
    result = client.submit(context, 20, persist=lambda value: saved.append(deepcopy(value)))
    assert result['phase'] == result['submission_phase'] == 'accepted'
    assert result['submit_id'] == 'submit-original' and result['node_id'] == 'node-original'
    assert calls.count((19, ('node', 'run'))) == 1
    assert TOKEN not in json.dumps(saved)


@pytest.mark.parametrize('state', ['running', 'processing', 'completed', 'succeeded', 'success', '', 'RUN_UNKNOWN'])
def test_generation_state_is_not_admission_and_does_not_authorize_paid_replay(boundary, state):
    runner, client, context, calls = _run_reply(boundary, state)
    saved = []
    with pytest.raises(CanvasCLIError) as unknown:
        client.submit(context, 20, persist=lambda value: saved.append(deepcopy(value)))
    assert unknown.value.outcome_unknown
    assert context['submission_phase'] == 'run_dispatching' and context['phase'] != 'accepted'
    with pytest.raises(CanvasCLIError) as second:
        client.submit(context, 20)
    assert second.value.outcome_unknown
    assert calls.count((19, ('node', 'run'))) == calls.count((19, ('node', 'confirm'))) == 1
    assert context['submit_id'] == 'submit-original' and TOKEN not in json.dumps(saved)


@pytest.mark.parametrize('state,resources,unknown', [
    ('RUN_REJECTED', [], False), (' REJECTED ', [], False),
    ('RUN_ACCEPTED', [], True), ('running', [], True),
    ('RUN_REJECTED', [{'resourceId': 'original-resource'}], True),
])
def test_error_receipts_only_release_credit_retry_after_explicit_unpartial_rejection(boundary, state, resources, unknown):
    runner, client, context, calls = _run_reply(boundary, state,
        error={'serviceCode': 1006, 'message': 'insufficient credits'}, resources=resources)
    with pytest.raises(CanvasCLIError) as rejected:
        client.submit(context, 20, persist=lambda value: True)
    assert rejected.value.outcome_unknown is unknown
    assert calls.count((19, ('node', 'run'))) == 1


@pytest.mark.parametrize('state,resources,unknown', [
    ('RUN_ACCEPTED', [], True), ('running', [], True), ('processing', [], True),
    ('pending', [], True), ('', [], True), ('unknown', [], True),
    ('RUN_REJECTED', [], False), ('RUN_REJECTED', [{'resourceId': 'existing-resource'}], True),
])
def test_partial_error_envelope_cannot_become_safe_balance_retry(tmp_path, monkeypatch, state, resources, unknown):
    envelope = {'ok': False, 'error': {'code': 'cli.service_rejected', 'reason': 'insufficient_credits'},
                'partialData': {'items': [{'state': state, 'resources': resources}]}}
    raw = json.dumps(envelope)
    patch_process(monkeypatch, f'import sys;sys.stderr.write({raw!r});sys.exit(22)')
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    try:
        with pytest.raises(CanvasCLIError) as rejected:
            runner.run(ACCOUNT, ['node', 'run'])
        assert rejected.value.outcome_unknown is unknown
    finally:
        runner.close()


@pytest.mark.parametrize('submission,expected', [
    (' RUN_ACCEPTED ', 'running'), ('RUN_UNKNOWN', 'unknown'), ('processing', 'running'),
])
def test_original_operation_keeps_generation_and_admission_contracts_separate(boundary, submission, expected):
    runner, client, context, calls, _ = boundary
    execute = runner._execute

    def reply(account, args, timeout, *, deadline=None):
        if args[:2] == ['operation', 'status']:
            calls.append((account['id'], tuple(args[:2])))
            assert args[2] == context['submit_id']
            return {'ok': True, 'data': {'operationRef': context['submit_id'], 'state': 'running',
                'submission': {'state': submission}, 'resources': []}}
        return execute(account, args, timeout, deadline=deadline)

    runner._execute = reply
    assert client.poll(context)['status'] == expected
    assert calls == [(19, ('operation', 'status'))]


def test_three_absent_queries_close_with_original_safe_error_without_replay_or_financial_changes(context):
    store, service, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 60)
    original = {'code': 'cli.invalid_service_response', 'phase': 'run_dispatching', 'exit_code': 1,
        'class': 'internal_failure', 'requiredAction': 'contact_support', 'retryable': False,
        'dispatch_started': True, 'outcome_unknown': True,
        'message': 'private prompt https://private.example/secret?token=private-token',
        'traceId': 'private-upstream-id', 'node_id': 'private-node', 'rawHTTP': 'private-body'}
    task = store.task('task')
    before_context = {**task['upstream_context'], 'submit_error': original}
    store.update_task('task', {'upstream_context': before_context, 'estimated_cost': 40, 'actual_cost': None})
    client = fake_client(store, service, account)
    client.poll.side_effect = [CanvasCLIError('cli.operation_submission_absent', 'not found') for _ in range(3)]
    service._poll_canvas('task', client, time.monotonic() + 60)
    result = store.task('task')
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert client.poll.call_count == 3
    client.submit.assert_not_called()
    client.prepare.assert_not_called()
    assert result['upstream_context']['submit_error'] == original
    assert result['upstream_context']['submit_id'] == before_context['submit_id']
    assert result['upstream_context']['project_id'] == before_context['project_id']
    assert result['upstream_context']['node_id'] == before_context['node_id']
    assert result['estimated_cost'] == 40 and result['actual_cost'] is None
    assert result['upstream_context']['financial_outcome'] == 'unconfirmed'
    diagnostic = result['upstream_context']['closed_submission_diagnostic']
    assert diagnostic['code'] == 'cli.invalid_service_response' and diagnostic['phase'] == 'run_dispatching'
    assert diagnostic['exit_code'] == 1 and diagnostic['retryable'] is False
    assert all(word in result['error_message'] for word in ('cli.invalid_service_response', 'phase=run_dispatching', 'exit=1'))
    assert 'private' not in result['error_message'] and 'private' not in json.dumps(diagnostic)


def test_closed_diagnostic_ignores_prose_ids_and_malformed_enum_fields():
    value = CanvasServiceMixin._canvas_closed_diagnostic({'submit_error': {
        'code': 'https://private.invalid', 'phase': ['private'], 'class': {'private': True},
        'reason': 'a' * 81, 'exit_code': True, 'traceId': 'private', 'message': 'private',
        'retryable': 'false', 'rawHTTP': 'private'}})
    assert value == {}
