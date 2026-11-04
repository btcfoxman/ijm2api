"""Unreliable generation lanes must not remain the best funded future account."""
from copy import deepcopy
import hashlib
import json
import time
from unittest.mock import Mock
import uuid

import pytest

from test_canvas_execution import context, owner, task


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':')).encode()).hexdigest()


@pytest.fixture
def unreliable(context):
    store, service, _ = context
    bad = owner(store, 'unreliable', balance=400, priority=9)
    good = owner(store, 'healthy', balance=100)
    profiles = {a['id']: service.canvas_cli.profile(a) for a in (bad, good)}
    old = task(store, 'old-failed-run', model='sd-2-5-480p-preview', backend='canvas_cli')
    now = time.time()
    project = str(uuid.uuid4())
    bound = {'backend': 'canvas_cli', 'task_id': old['id'], 'account_id': bad['id'],
        'profile': profiles[bad['id']], 'expected_user_id': bad['login_user_id'],
        'project_id': project, 'node_id': 'node_0123456789', 'submit_id': str(uuid.uuid4()),
        'phase': 'quoted', 'submission_phase': 'run_dispatching', 'output_count': 1, 'resource_ids': [],
        'submission_receipt_evidence': True,
        'submission_evidence': {'version': 1, 'accepted': False, 'output_resources': False,
            'receipt': False, 'identity_conflict': False, 'ambiguous_partial': True},
        'upload_checkpoint': {'binding': {'task_id': old['id'], 'account_id': bad['id'],
            'project_id': project, 'profile_sha256': digest(profiles[bad['id']]),
            'user_id_sha256': digest(bad['login_user_id']), 'proxy_sha256': digest(bad['proxy_url'])}},
        'submit_error': {'code': 'cli.invalid_service_response', 'class': 'internal_failure',
            'requiredAction': 'contact_support', 'retryable': False, 'phase': 'run_dispatching',
            'dispatch_started': True, 'outcome_unknown': True},
        'reconciliation_closed': True, 'reconciliation_close_reason': 'missing_output_resource_budget_exhausted',
        'reconciliation_closed_at': now - 10, 'financial_outcome': 'unconfirmed',
        'preparation_timing': {'commands': {'node run': {'count': 1, 'failed': 1}}},
        'missing_resource_recovery': {'version': 1, 'started_at': now - 70, 'deadline': now - 10,
                                     'attempts': 3, 'max_attempts': 3}}
    old = store.update_task(old['id'], {'status': 'failed', 'account_id': bad['id'],
        'error_code': 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED', 'completed_at': now - 10,
        'estimated_cost': 76, 'actual_cost': None, 'upstream_context': bound})
    future = task(store, 'new-request', model='sd-2-5-480p-preview', backend='canvas_cli')
    return store, service, bad, good, old, future


def test_future_admission_and_availability_agree_and_preserve_old_unknown_finance(unreliable):
    store, service, bad, good, old, future = unreliable
    before = deepcopy(old)
    profiles = {a['id']: (service.canvas_cli.account_directory(a) / 'credential-profile').read_bytes()
                for a in (bad, good)}
    service.canvas_cli.profile = Mock(side_effect=AssertionError('Admission cannot create a credential profile'))
    assert service._has_eligible_account(76, {good['id']}, future['id']) is False
    selected = service._reserve_account(76, task_id=future['id'])
    assert selected['id'] == good['id']
    service._release_account(good['id'], future['id'])
    assert store.task(old['id']) == before
    assert store.account(bad['id'], private=True)['enabled'] == 1
    assert store.account(bad['id'], private=True)['status'] == 'active'
    for account in (bad, good):
        assert (service.canvas_cli.account_directory(account) / 'credential-profile').read_bytes() == profiles[account['id']]


def test_auth_balance_success_does_not_clear_generation_cooldown(unreliable):
    store, service, bad, good, old, future = unreliable
    store.update_account(bad['id'], {'status': 'active', 'cli_auth_status': 'ready', 'balance': 999})
    assert service._has_eligible_account(76, {good['id']}, future['id']) is False


def test_default_window_survives_deployment_and_expires_without_observation_renewal(unreliable, monkeypatch):
    store, service, bad, good, old, future = unreliable
    assert service.settings()['canvas_account_failure_cooldown_seconds'] == 3600
    closed = old['completed_at']
    monkeypatch.setattr('app.service.time.time', lambda: closed + 2400)
    assert service._has_eligible_account(76, {good['id']}, future['id']) is False
    monkeypatch.setattr('app.service.time.time', lambda: closed + 3601)
    assert service._has_eligible_account(76, {good['id']}, future['id']) is True


def test_same_account_other_model_and_explicit_disable_setting_remain_available(unreliable):
    store, service, bad, good, old, future = unreliable
    other = task(store, 'other-model', model='sd-2-0-mini', backend='canvas_cli')
    selected = service._reserve_account(10, task_id=other['id'])
    assert selected['id'] == bad['id']
    service._release_account(bad['id'], other['id'])
    service.update_settings({'canvas_account_failure_cooldown_seconds': 0})
    selected = service._reserve_account(76, task_id=future['id'])
    assert selected['id'] == bad['id']
    service._release_account(bad['id'], future['id'])


def test_existing_original_upload_owner_is_not_redirected_by_future_lane_health(unreliable):
    store, service, bad, good, old, future = unreliable
    bound = deepcopy(old['upstream_context'])
    for key in ('submit_error', 'reconciliation_closed', 'reconciliation_close_reason',
                'reconciliation_closed_at', 'financial_outcome', 'missing_resource_recovery'):
        bound.pop(key, None)
    bound.update(task_id=future['id'], phase='uploading', submission_phase='')
    bound['upload_checkpoint']['binding']['task_id'] = future['id']
    store.update_task(future['id'], {'status': 'preparing', 'account_id': bad['id'], 'upstream_context': bound})
    selected = service._reserve_account(76, exclude_ids={good['id']}, task_id=future['id'])
    assert selected['id'] == bad['id']
    service._release_account(bad['id'], future['id'])


def test_optional_history_read_failure_does_not_break_existing_admission(unreliable, monkeypatch):
    store, service, bad, good, old, future = unreliable
    monkeypatch.setattr(store, 'canvas_account_health_rows', Mock(side_effect=RuntimeError('synthetic read failure')))
    selected = service._reserve_account(76, task_id=future['id'])
    assert selected['id'] == bad['id']
    service._release_account(bad['id'], future['id'])


def test_one_damaged_profile_does_not_clear_other_accounts_failure_evidence(unreliable):
    store, service, bad, good, old, future = unreliable
    (service.canvas_cli.account_directory(good) / 'credential-profile').write_bytes(b'\xff\xfe')
    assert service._has_eligible_account(76, {good['id']}, future['id']) is False


@pytest.mark.parametrize('value', [-1, 86401, True, '1800', 1.5])
def test_cooldown_setting_is_an_explicit_bounded_integer(context, value):
    store, service, _ = context
    with pytest.raises(ValueError, match='canvas_account_failure_cooldown_seconds'):
        service.update_settings({'canvas_account_failure_cooldown_seconds': value})
