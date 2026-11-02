"""Pending input observations retain bounded retries and zero-send routing proof."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_service import CanvasServiceMixin
from app.errors import BOUNDED_RETRY_FAILURE_MESSAGE, public_error
from test_canvas_execution import context, owner, task, fake_client
from test_canvas_preparation_retry_integration import pending_candidate


FACTS = {'generation_request_sent': False, 'financial_outcome': 'not_charged_this_invocation',
         'automatic_resubmission': False, 'stage': 'preparation'}


def input_state(account, registration='unknown'):
    checkpoint = pending_candidate(account)
    checkpoint['binding']['project_id'] = f"project-{account['id']}"
    checkpoint['pending_refs'][0]['registration'] = registration
    return {'phase': 'uploading', 'project_id': checkpoint['binding']['project_id'],
            'resources': {'images': ['private-uploaded-input'], 'videos': [], 'audios': []},
            'upload_checkpoint': checkpoint}


@pytest.mark.parametrize('registration', ['acknowledged', 'unknown', 'unacknowledged'])
def test_pending_reference_can_recover_on_original_account_before_single_paid_run(context, registration):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000})
    account = owner(store, balance=200)
    task(store)
    client = fake_client(store, service, account, quote=170)
    original = client.prepare.side_effect
    checkpoints = []
    state = input_state(account, registration)
    def prepare(*args, checkpoint=None, **kwargs):
        checkpoints.append(deepcopy(checkpoint))
        ready = original(*args, **kwargs)
        if len(checkpoints) == 1:
            kwargs['persist'](deepcopy(state))
            raise CanvasCLIError('MEDIA_UPLOAD_PENDING', 'Original reference is still pending')
        assert checkpoint == state['upload_checkpoint']
        return ready
    client.prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', return_value=client):
        service._run('task')
    row = store.task('task')
    assert row['status'] == 'succeeded', row['error_message']
    assert len(checkpoints) == 2 and checkpoints[0] is None
    assert client.submit.call_count == 1
    budget = row['upstream_context']['preparation_retry_budget']
    assert [entry['account_id'] for entry in budget['attempts']] == [account['id']] * 2
    assert budget['deadline'] - budget['started_at'] == 180


def test_unresolved_pending_reference_stops_without_account_switch_or_paid_run(context):
    store, service, _ = context
    store.update_settings({'task_timeout_seconds': 3000})
    account = owner(store, 'original', balance=200, priority=2)
    alternate = owner(store, 'other', balance=200)
    task(store)
    client = fake_client(store, service, account, quote=170)
    original = client.prepare.side_effect
    state = input_state(account)
    def prepare(*args, checkpoint=None, **kwargs):
        original(*args, **kwargs)
        kwargs['persist'](deepcopy(state))
        raise CanvasCLIError('MEDIA_UPLOAD_PENDING', 'Reference not ready; retained original ID')
    client.prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', return_value=client) as factory:
        service._run('task')
    row = store.task('task')
    assert row['status'] == 'failed' and row['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
    assert client.prepare.call_count == 2 and client.submit.call_count == 0
    assert all(call.args[0]['id'] == account['id'] for call in factory.call_args_list)
    assert row['account_id'] != alternate['id'] and row['actual_cost'] is None
    assert row['upstream_context']['upload_checkpoint'] == state['upload_checkpoint']
    assert len(row['upstream_context']['preparation_retry_budget']['attempts']) == 2
    public = service.public_task(row)
    assert public['error']['message'] == BOUNDED_RETRY_FAILURE_MESSAGE
    assert public['provider_context']['generation_request_sent'] is False
    assert public['provider_context']['fallback_allowed'] is True
    before = deepcopy(row)
    service._run('task')
    assert store.task('task') == before


@pytest.mark.parametrize('mutation', [
    'foreign_task', 'foreign_account', 'foreign_project', 'unbound_resource', 'wrong_index',
    'bad_registration', 'registration_shape', 'group_shape', 'resources_shape', 'not_observe_only',
    'missing_pending', 'paid_receipt', 'run_started', 'unknown_outcome',
])
def test_pending_reference_retry_rejects_invalid_binding_or_paid_uncertainty(context, mutation):
    store, _, _ = context
    account = owner(store)
    row = task(store)
    context_value = input_state(account)
    row.update(status='preparing', account_id=account['id'], upstream_context=context_value)
    checkpoint = context_value['upload_checkpoint']
    pending = checkpoint['pending_refs'][0]
    error = CanvasCLIError('MEDIA_UPLOAD_PENDING', 'Pending input')
    if mutation == 'foreign_task': checkpoint['binding']['task_id'] = 'other'
    if mutation == 'foreign_account': checkpoint['binding']['account_id'] += 1
    if mutation == 'foreign_project': checkpoint['binding']['project_id'] = 'other'
    if mutation == 'unbound_resource': pending['resource_id'] = 'other'
    if mutation == 'wrong_index': pending['index'] = 1
    if mutation == 'bad_registration': pending['registration'] = 'retry_anything'
    if mutation == 'registration_shape': pending['registration'] = []
    if mutation == 'group_shape': pending['group'] = {}
    if mutation == 'resources_shape': context_value['resources'] = []
    if mutation == 'not_observe_only': pending['observe_only'] = False
    if mutation == 'missing_pending': checkpoint['pending_refs'] = []
    if mutation == 'paid_receipt': context_value['resource_ids'] = ['generated-output']
    if mutation == 'run_started': context_value['preparation_timing'] = {'commands': {'node run': {'count': 1}}}
    if mutation == 'unknown_outcome': error.outcome_unknown = True
    assert not CanvasServiceMixin._canvas_pending_reference_retry(row, error)


def test_legacy_pending_reference_get_is_canonical_and_allows_fallback_without_row_write(context):
    store, service, _ = context
    task(store)
    store.update_task('task', {'status': 'failed', 'execution_backend': 'canvas_cli',
        'error_code': 'MEDIA_UPLOAD_PENDING', 'error_message': 'Retained original reference ID',
        'upstream_context': {'phase': 'uploading', 'pre_dispatch_failure': FACTS}})
    row = store.task('task')
    before = deepcopy(row)
    value = service.public_task(row)
    assert value['error']['message'] == BOUNDED_RETRY_FAILURE_MESSAGE
    assert value['provider_context'] == {
        'generation_request_sent': False, 'financial_outcome': 'not_charged_this_invocation',
        'automatic_resubmission': False, 'fallback_allowed': True}
    assert store.task('task') == before == row


@pytest.mark.parametrize('context_value', [
    {}, {'pre_dispatch_failure': {**FACTS, 'generation_request_sent': True}},
    {'pre_dispatch_failure': FACTS, 'resource_ids': ['generated-output']},
    {'pre_dispatch_failure': FACTS, 'submit_error': {'outcome_unknown': True}},
    {'pre_dispatch_failure': FACTS, 'previous_submissions': [{'submit_id': 'old-submit'}]},
])
def test_pending_reference_public_projection_never_invents_zero_send_proof(context_value):
    row = {'status': 'failed', 'error_code': 'MEDIA_UPLOAD_PENDING', 'upstream_context': context_value}
    assert CanvasServiceMixin._canvas_public_pre_dispatch_facts(row) == {}


def test_pending_reference_message_precedes_raw_diagnostic_policy_or_refund_text():
    assert public_error('MEDIA_UPLOAD_PENDING', 'possible copyright policy or refunded credits') == {
        'code': 'MEDIA_UPLOAD_PENDING', 'message': BOUNDED_RETRY_FAILURE_MESSAGE}


@pytest.mark.parametrize('details', [
    {'dispatch_started': True}, {'phase': 'run_dispatching'},
    {'operation_ref': 'accepted-operation'}, {'resource_ids': ['generated-output']},
    {'error': {'dispatch_started': True}}, {'error': {'operation_ref': 'accepted-operation'}},
])
def test_pending_reference_typed_error_cannot_hide_dispatch_evidence(context, details):
    store, _, _ = context
    account = owner(store)
    row = task(store)
    row.update(status='preparing', account_id=account['id'], upstream_context=input_state(account))
    error = CanvasCLIError('MEDIA_UPLOAD_PENDING', 'Input pending', details=details)
    assert not CanvasServiceMixin._canvas_pending_reference_retry(row, error)
