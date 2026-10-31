"""Partial CLI metadata is distinct from an affirmative, bound run receipt."""
from copy import deepcopy
import json
import time

import pytest

from app.canvas_cli import CanvasCLIError, _has_accepted
from app.canvas_client import _remember_submission_evidence
from app.canvas_service import CanvasServiceMixin
from test_canvas_execution import context, fake_client, owner, retained_operation
from test_canvas_retryable_submission import FACTS, eligible
from test_canvas_transport import prepare
from test_retryable_submission_public import closed_task, service


def test_943d_legacy_partial_marker_projects_retryable_without_rewriting_original_task():
    # Sanitized shape of 943d: four input images, no output, one failed run,
    # three exact absences. Its original partial envelope was not retained.
    task = closed_task()
    ctx = task['upstream_context']
    ctx.update(submission_receipt_evidence=True,
               resources={'images': ['input-1', 'input-2', 'input-3', 'input-4'], 'videos': [], 'audios': []})
    task['normalized']['_execution'] = deepcopy(ctx)
    before = deepcopy(task)
    instance = service()
    result = instance.public_task(task)
    assert result['status'] == 'failed' and result['error']['code'] == 'CLI_SUBMISSION_RETRYABLE'
    assert result['provider_context'] == FACTS
    assert 'generation_request_sent' not in result['provider_context']
    assert task == before and ctx['submission_receipt_evidence'] is True
    assert not instance.store.mock_calls


@pytest.mark.parametrize('partial', [
    {'unrecognized_partial_shape': {'private': 'token=never-save-this'}},
    {'operationRef': 'original-submit', 'state': 'unknown', 'resources': [],
     'submission': {'state': 'unknown', 'resubmittable': True, 'adviceCode': 'resume'}},
    {'items': [{'nodeId': 'original-node', 'submitId': 'original-submit', 'state': 'RUN_UNKNOWN', 'resources': []}]},
    {'submissionState': 'unknown', 'resourceIds': None, 'projectId': 'original-project',
     'nodeId': 'original-node', 'operationRef': 'original-submit',
     'spec': {'resources': ['input-image'], 'params': {'accepted': True}}},
])
def test_weak_partial_metadata_never_becomes_an_affirmative_receipt(partial):
    task = eligible()
    ctx = task['upstream_context']
    _remember_submission_evidence(ctx, {'partialData': partial})
    assert ctx['submission_receipt_evidence'] is True
    assert ctx['submission_evidence'] == {
        'version': 1, 'accepted': False, 'output_resources': False,
        'receipt': False, 'identity_conflict': False, 'ambiguous_partial': True}
    assert CanvasServiceMixin._canvas_retryable_submission_failure(task) == FACTS
    assert 'never-save-this' not in json.dumps(ctx)


@pytest.mark.parametrize('strong', [
    {'items': [{'state': 'RUN_ACCEPTED', 'resources': []}]},
    {'submission': {'status': 'accepted'}},
    {'items': [{'state': 'unknown', 'resources': [{'resourceId': 'private-output-id'}]}]},
    {'state': 'unknown', 'resourceIds': ['private-output-id']},
    {'resourceId': 'private-output-id'},
    {'operation': {'state': 'running', 'resources': []}},
    {'run_receipt': {'opaque': True}},
    {'generationId': 'private-generation-id'},
    {'projectId': 'another-project'}, {'nodeId': 'another-node'},
    {'operationRef': 'another-submit'},
    {'items': [{'nodeId': 'original-node', 'submitId': 'another-submit', 'state': 'unknown', 'resources': []}]},
])
def test_strong_or_conflicting_partial_evidence_always_blocks_fallback(strong):
    task = eligible()
    ctx = task['upstream_context']
    _remember_submission_evidence(ctx, {'partialData': strong})
    before = deepcopy(ctx['submission_evidence'])
    _remember_submission_evidence(ctx, {'partialData': {'state': 'unknown', 'resources': []}})
    assert all(not value or ctx['submission_evidence'][key] for key, value in before.items())
    assert CanvasServiceMixin._canvas_retryable_submission_failure(task) == {}
    assert 'private-output-id' not in json.dumps(ctx)
    assert 'another-submit' not in json.dumps(ctx)


def test_strong_nested_error_evidence_wins_over_a_weak_partial():
    task = eligible()
    _remember_submission_evidence(task['upstream_context'], {
        'partialData': {'state': 'unknown', 'resources': []},
        'error': {'accepted': True, 'resources': [{'resourceId': 'never-persist'}]},
    })
    assert CanvasServiceMixin._canvas_retryable_submission_failure(task) == {}


@pytest.mark.parametrize('strong', ['none', 'accepted', 'output'])
def test_local_post_edit_validation_success_is_not_generation_acceptance(strong):
    task = eligible()
    envelope = {'partialData': {'state': 'unknown', 'resources': []},
                'error': {'validation': {'postEdit': {'status': 'success', 'sourceResourceId': 'input-image'}}}}
    if strong == 'accepted':
        envelope['data'] = {'items': [{'state': 'RUN_ACCEPTED', 'resources': []}]}
    elif strong == 'output':
        envelope['data'] = {'resourceId': 'actual-output'}
    _remember_submission_evidence(task['upstream_context'], envelope)
    evidence = task['upstream_context']['submission_evidence']
    assert evidence['accepted'] is (strong == 'accepted')
    assert evidence['output_resources'] is (strong == 'output')
    assert CanvasServiceMixin._canvas_retryable_submission_failure(task) == (FACTS if strong == 'none' else {})


@pytest.mark.parametrize('mutation', ['not_closed', 'two_absences', 'another_error', 'already_accepted',
                                     'normalized_output', 'normalized_strong', 'another_profile', 'two_runs', 'bad_evidence_format'])
def test_legacy_weak_bit_does_not_bypass_any_other_reconciliation_guard(mutation):
    task = eligible()
    ctx = task['upstream_context']
    ctx['submission_receipt_evidence'] = True
    if mutation == 'not_closed':
        ctx['reconciliation_closed'] = False
    elif mutation == 'two_absences':
        ctx['consecutive_submission_absent'] = 2
    elif mutation == 'another_error':
        ctx['submit_error']['code'] = 'cli.authentication_required'
    elif mutation == 'already_accepted':
        ctx['phase'] = 'accepted'
    elif mutation == 'normalized_output':
        task['normalized'] = {'_execution': {'resource_ids': ['actual-output']}}
    elif mutation == 'normalized_strong':
        previous = {}
        _remember_submission_evidence(previous, {'partialData': {'state': 'RUN_ACCEPTED'}})
        task['normalized'] = {'_execution': previous}
    elif mutation == 'another_profile':
        task['normalized'] = {'_execution': {'profile': 'different-profile'}}
    elif mutation == 'two_runs':
        ctx['preparation_timing']['commands']['node run']['count'] = 2
    else:
        ctx['submission_evidence'] = {'version': 1, 'accepted': 'false'}
    assert CanvasServiceMixin._canvas_retryable_submission_failure(task) == {}


@pytest.mark.parametrize('strong', [False, True])
def test_real_submit_redaction_then_three_absences_preserves_receipt_strength_and_run_once(context, tmp_path, strong):
    store, instance, _ = context
    account = owner(store)
    retained_operation(store, account, paused=False, deadline=time.time() + 60)
    runner, client, bound = prepare(tmp_path)
    partial = {'items': [{'nodeId': bound['node_id'], 'submitId': bound['submit_id'],
                          'state': 'RUN_ACCEPTED' if strong else 'RUN_UNKNOWN', 'resources': []}]}
    runner.run_failure = CanvasCLIError('cli.invalid_service_response', 'private response token=never-save-this',
        details={'error': {'code': 'cli.invalid_service_response', 'class': 'internal_failure',
                          'requiredAction': 'contact_support', 'retryable': False}, 'partialData': partial},
        outcome_unknown=True)
    with pytest.raises(CanvasCLIError) as caught:
        client.submit(bound, 20)
    assert caught.value.outcome_unknown is True
    assert bound['submission_evidence']['accepted'] is strong
    assert 'partialData' not in bound['submit_error']
    assert sum(args[:2] == ['node', 'run'] for args in runner.calls) == 1
    bound.update(task_id='task', account_id=account['id'], expected_user_id=account['login_user_id'],
                 preparation_timing={'commands': {'node run': {'count': 1}}})
    store.update_task('task', {'upstream_context': bound})
    original = store.task('task')
    poller = fake_client(store, instance, account)
    poller.poll.side_effect = [CanvasCLIError('cli.operation_submission_absent', 'not found') for _ in range(3)]
    instance._poll_canvas('task', poller, time.monotonic() + 60)
    result = store.task('task')
    assert result['error_code'] == ('CLI_SUBMISSION_UNCONFIRMED' if strong else 'CLI_SUBMISSION_RETRYABLE')
    assert result['upstream_context']['submission_receipt_evidence'] is True
    assert result['upstream_context']['financial_outcome'] == 'unconfirmed'
    for field in ('actual_cost', 'estimated_cost', 'balance_before', 'balance_after', 'normalized'):
        assert result[field] == original[field]
    for field in ('profile', 'project_id', 'node_id', 'submit_id'):
        assert result['upstream_context'][field] == original['upstream_context'][field]
    poller.submit.assert_not_called()
    poller.prepare.assert_not_called()
    assert poller.poll.call_count == 3


def test_global_credit_rejection_guard_keeps_its_conservative_unknown_semantics():
    assert _has_accepted({'partialData': {'items': [{'state': 'RUN_UNKNOWN', 'resources': []}]}}) is True
