"""Preparation window expiry preserves the cause and proven routing facts."""
from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_retry_budget import new_retry_budget, claim_attempt, budget_token
from app.errors import BOUNDED_RETRY_FAILURE_MESSAGE
from test_canvas_execution import context, owner, task, fake_client


FACTS = {'generation_request_sent': False,
         'financial_outcome': 'not_charged_this_invocation',
         'automatic_resubmission': False, 'stage': 'preparation'}


@pytest.mark.parametrize('retry_window', [False, True])
def test_real_preparation_expiry_preserves_upload_timeout_and_original_window(context, retry_window):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000})
    account = owner(store)
    task(store)
    clock = {'wall': time.time(), 'mono': time.monotonic()}
    ledger = None
    original = {'backend': 'canvas_cli', 'phase': 'uploading',
                'preparation_admission_at': clock['wall'] - 70,
                'pre_dispatch_failure': {**FACTS, 'original_proof': 'retained'}}
    if retry_window:
        ledger = new_retry_budget('task', admitted_at=clock['wall'] - 70,
                                  retry_started_at=clock['wall'] - 10,
                                  task_deadline=clock['wall'] + 2900)
        proof = {'stage': 'preparation', 'node_run_count': 0,
                 'generation_request_sent': False, 'dispatch_started': False,
                 'outcome_unknown': False, 'prior_dispatch_evidence': False}
        ledger = claim_attempt(ledger, task_id='task', account_id=account['id'],
                               now=clock['wall'] - 10,
                               expected_token=budget_token(ledger, task_id='task'), proof=proof)
        original['preparation_retry_budget'] = ledger
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing',
                              'execution_backend': 'canvas_cli', 'upstream_context': original})
    client = fake_client(store, service, account)
    prepare = client.prepare.side_effect

    def timeout(*args, **kwargs):
        prepare(*args, **kwargs)
        kwargs['persist']({'phase': 'uploading'})
        advance = kwargs['deadline'] - clock['mono'] + .25
        clock['wall'] += advance
        clock['mono'] += advance
        raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Original bounded upload failure',
                             exit_code=-15, outcome_unknown=False)

    client.prepare.side_effect = timeout
    with patch.object(service, 'canvas_client', return_value=client), patch(
            'app.canvas_service.time', SimpleNamespace(
                time=lambda: clock['wall'], monotonic=lambda: clock['mono'])):
        service._execute_canvas('task', account, clock['mono'] + (2900 if retry_window else 5))
    row = store.task('task')
    detail = row['upstream_context']
    assert row['status'] == 'failed'
    assert row['error_code'] == ('CLI_PREPARATION_RETRY_EXHAUSTED' if retry_window else 'CLI_PREPARATION_TIMEOUT')
    assert detail['preparation_expiry']['scope'] == ('retry_window' if retry_window else 'task_window')
    assert detail['preparation_expiry']['source_error_code'] == 'CLI_COMMAND_TIMEOUT'
    assert detail['preparation_error']['code'] == 'CLI_COMMAND_TIMEOUT'
    assert detail['preparation_error']['phase'] == 'uploading'
    assert 'CLI_COMMAND_TIMEOUT' in row['error_message']
    assert detail['pre_dispatch_failure'] == {**FACTS, 'original_proof': 'retained', 'fallback_allowed': True}
    if ledger:
        current = detail['preparation_retry_budget']
        assert current['started_at'] == ledger['started_at']
        assert current['deadline'] == ledger['deadline']
        assert current['task_deadline'] == ledger['task_deadline']
        assert [entry['account_id'] for entry in current['attempts']] == [account['id']] * 2
        assert current['deadline'] - current['started_at'] == 180
    else:
        assert 'preparation_retry_budget' not in detail
    public = service.public_task(row)
    assert public['error']['message'] == BOUNDED_RETRY_FAILURE_MESSAGE
    assert public['provider_context'] == {key: value for key, value in
        {**FACTS, 'fallback_allowed': True}.items() if key != 'stage'}
    assert row['actual_cost'] is None
    client.prepare.assert_called_once()
    client.submit.assert_not_called()
    client.poll.assert_not_called()
    before = deepcopy(row)
    service._run('task')  # A terminal task never gets a fresh budget on recovery.
    assert store.task('task') == before


def test_legacy_timeout_with_existing_positive_proof_gains_readonly_fallback_projection(context):
    store, service, _ = context
    task(store)
    store.update_task('task', {'status': 'failed', 'error_code': 'CLI_PREPARATION_TIMEOUT',
        'error_message': 'Task deadline passed during Canvas preparation; no generation was submitted',
        'execution_backend': 'canvas_cli', 'upstream_context': {
            'phase': 'uploading', 'resource_ids': [], 'pre_dispatch_failure': FACTS,
            'preparation_error': {'code': 'CLI_COMMAND_TIMEOUT', 'phase': 'uploading'}}})
    row = store.task('task')
    before = deepcopy(row)
    public = service.public_task(row)
    assert public['error']['message'] == BOUNDED_RETRY_FAILURE_MESSAGE
    assert public['provider_context'] == {
        'generation_request_sent': False, 'financial_outcome': 'not_charged_this_invocation',
        'automatic_resubmission': False, 'fallback_allowed': True}
    assert 'preparation_expiry' not in row['upstream_context']
    assert row == before == store.task('task')


@pytest.mark.parametrize('marker,unknown,prior', [
    ({'resource_ids': ['accepted-output']}, False, False),
    ({'operation_ref': 'original-operation'}, False, False),
    ({'submit_error': {'outcome_unknown': True}}, False, False),
    ({'previous_submissions': [{'submit_id': 'old-operation'}]}, False, False),
    ({'preparation_timing': {'commands': {'node run': {'count': 1}}}}, False, False),
    ({'_failure_details': {'dispatch_started': True}}, False, False),
    ({'_failure_details': {'error': {'operationRef': 'existing-operation'}}}, False, False),
    ({}, True, False),
    ({}, False, True),
])
def test_expiry_never_promotes_stale_proof_over_dispatch_or_unknown_outcome(context, marker, unknown, prior):
    store, service, _ = context
    task(store)
    store.update_task('task', {'status': 'preparing', 'actual_cost': 13,
        'execution_backend': 'canvas_cli', 'upstream_context': {
            'phase': 'uploading', 'pre_dispatch_failure': FACTS,
            **{key: value for key, value in marker.items() if key != '_failure_details'}}})
    failure = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Unknown synthetic failure', outcome_unknown=unknown,
                             details=marker.get('_failure_details'))
    service._canvas_expire_preparation('task', time.monotonic() - 1, cause=failure,
                                      prior_dispatch_evidence=prior)
    row = store.task('task')
    assert row['upstream_context']['preparation_expiry']['pre_dispatch_proven'] is False
    assert service._canvas_public_pre_dispatch_facts(row) == {}
    assert 'no generation was submitted' not in row['error_message']
    assert row['actual_cost'] == 13
    assert row['upstream_context']['pre_dispatch_failure'] == FACTS


def test_legacy_timeout_with_only_empty_output_ids_does_not_fabricate_routing_proof(context):
    store, service, _ = context
    task(store)
    store.update_task('task', {'status': 'failed', 'error_code': 'CLI_PREPARATION_TIMEOUT',
        'execution_backend': 'canvas_cli', 'upstream_context': {'phase': 'uploading', 'resource_ids': []}})
    row = store.task('task')
    before = deepcopy(row)
    assert 'provider_context' not in service.public_task(row)
    assert row == before == store.task('task')
