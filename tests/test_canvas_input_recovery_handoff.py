"""Resume input-only failures on another owner without changing paid authority."""
from copy import deepcopy
import time

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_input_handoff import HANDOFF_KEY, propose_input_handoff
from test_canvas_execution import context
from test_canvas_input_handoff import prepared, transport


def recovered_case(context, tmp_path, mode):
    values = list(prepared(context, tmp_path))
    store, _service, old, _new, row, _profile, _assets, diagnostic = values
    ctx = deepcopy(row['upstream_context'])
    if mode == 'project_read':
        ctx['phase'] = 'preparing'
        diagnostic = {**diagnostic, 'phase': 'preparing'}
        last = ctx['preparation_attempts'][-1]['timing']
        last.update(recent_commands=[
            {'command_pair': ['auth', 'account'], 'outcome': 'success'},
            {'command_pair': ['model', 'list'], 'outcome': 'success'},
            {'command_pair': ['canvas', 'ls'], 'outcome': 'failed', 'error_code': 'cli.transport_failed',
             'failure': {'class': 'retryable_failure', 'requiredAction': 'retry', 'retryable': True}}],
            commands={'auth account': {'count': 1, 'failed': 0}, 'model list': {'count': 1, 'failed': 0},
                      'canvas ls': {'count': 1, 'failed': 1}}, command_count=3)
        ctx['preparation_timing'] = deepcopy(last)
    else:
        diagnostic = {'code': 'MEDIA_UPLOAD_PENDING', 'phase': 'uploading', 'outcome_unknown': False}
    ctx['preparation_error'] = diagnostic
    values[4] = store.update_task(row['id'], {'upstream_context': ctx})
    values[7] = diagnostic
    return values


def propose(values, *, workers_joined=True):
    _store, _service, old, _new, row, profile, _assets, diagnostic = values
    return propose_input_handoff(row, old, profile, diagnostic, now=time.time(), workers_joined=workers_joined)


@pytest.mark.parametrize('mode', ['project_read', 'pending_input'])
def test_recovered_inputs_use_one_existing_handoff_with_original_budget_and_phase(context, tmp_path, mode):
    values = recovered_case(context, tmp_path, mode)
    store, service, old, new, row, _profile, _assets, _diagnostic = values
    before = deepcopy(row['upstream_context'])
    error = transport() if mode == 'project_read' else CanvasCLIError('MEDIA_UPLOAD_PENDING', 'input still pending')
    assert propose(values) is not None
    assert service._canvas_handoff_preparation(row['id'], old, error) is True
    queued = store.task(row['id']); ctx = queued['upstream_context']
    record, = ctx[HANDOFF_KEY]
    assert record['diagnostic']['phase'] == before['phase']
    assert record['checkpoint'] == before['upload_checkpoint']
    assert ctx['preparation_retry_budget'] == before['preparation_retry_budget']
    assert queued['status'] == 'queued' and queued['account_id'] is None
    assert queued['attempted_account_ids'] == [old['id']] and queued['actual_cost'] is None
    assert record['proof']['node_run_count'] == record['proof']['node_confirm_count'] == 0
    assert not any(key in ctx for key in ('upload_checkpoint', 'profile', 'resources', 'submit_id'))
    selected = service._reserve_account(20, task_id=row['id'])
    assert selected['id'] == new['id']
    service._release_account(new['id'], row['id'])


@pytest.mark.parametrize('mode', ['project_read', 'pending_input'])
@pytest.mark.parametrize('veto', ['workers_active', 'identity', 'output_pending', 'policy', 'material', 'paid', 'bad_uid'])
def test_recovery_variants_keep_all_input_only_vetoes(context, tmp_path, mode, veto):
    values = recovered_case(context, tmp_path, mode)
    row, diagnostic = values[4], values[7]; ctx = row['upstream_context']
    if veto == 'workers_active':
        assert propose(values, workers_joined=False) is None
        return
    if veto == 'identity': diagnostic.update(code='cli.authentication_required')
    elif veto == 'output_pending':
        ctx['resource_ids'] = ['generated-output']; diagnostic['code'] = 'CLI_RESULT_PENDING'
    elif veto == 'policy': diagnostic.update(code='service.20115', **{'class': 'business_failure'})
    elif veto == 'material': diagnostic.update(code='MEDIA_REQUIREMENTS_INVALID', **{'class': 'invalid_input'})
    elif veto == 'paid': ctx['submission_phase'] = 'run_dispatching'
    elif veto == 'bad_uid': ctx['upload_checkpoint']['binding']['user_id_sha256'] = '0' * 64
    assert propose(values) is None


@pytest.mark.parametrize('extra', ['canvas create', 'resource get', 'no_prior_upload'])
def test_project_recovery_requires_current_only_reads_and_actual_prior_input_uploads(context, tmp_path, extra):
    values = recovered_case(context, tmp_path, 'project_read'); ctx = values[4]['upstream_context']
    if extra == 'no_prior_upload':
        first = ctx['preparation_attempts'][0]['timing']
        first.update(recent_commands=[{'command_pair': ['auth', 'account'], 'outcome': 'success'}],
            commands={'auth account': {'count': 1, 'failed': 0}}, command_count=1)
    else:
        last = ctx['preparation_attempts'][-1]['timing']
        last['recent_commands'].insert(1, {'command_pair': extra.split(), 'outcome': 'success'})
        last['commands'][extra] = {'count': 1, 'failed': 0};last['command_count'] += 1
        ctx['preparation_timing'] = deepcopy(last)
    assert propose(values) is None


def test_pending_input_does_not_accept_upstream_auth_actions_under_local_pending_code(context, tmp_path):
    values = recovered_case(context, tmp_path, 'pending_input')
    values[7]['requiredAction'] = 'authenticate'
    assert propose(values) is None


@pytest.mark.parametrize('mode', ['project_read', 'pending_input'])
def test_no_funded_alternate_does_not_change_owner_or_budget(context, tmp_path, mode):
    values = recovered_case(context, tmp_path, mode)
    store, service, old, new, row, *_ = values
    store.update_account(new['id'], {'enabled': False})
    error = transport() if mode == 'project_read' else CanvasCLIError('MEDIA_UPLOAD_PENDING', 'input still pending')
    assert service._canvas_handoff_preparation(row['id'], old, error) is False
    assert store.task(row['id']) == row


def test_project_read_cas_progress_conflict_closes_owned_work_instead_of_hanging(context, tmp_path, monkeypatch):
    values = recovered_case(context, tmp_path, 'project_read')
    store, service, old, _new, row, *_ = values
    def conflict(*args, **kwargs):
        store.update_task(row['id'], {'progress': 5})
        return False
    monkeypatch.setattr(store, 'migrate_canvas_input_preparation', conflict)
    assert service._canvas_handoff_preparation(row['id'], old, transport()) == 'stopped'
    assert store.task(row['id'])['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
    assert store.task(row['id'])['actual_cost'] is None
