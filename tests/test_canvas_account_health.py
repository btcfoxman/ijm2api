"""A soft future-admission gate must not rewrite original work or finance."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.canvas_account_health import account_cooldowns


NOW = 200000.0
PROFILE = 'ijm-test-account-20'
MODEL = 'dreamina_seedance_45_pro_draft'
PROJECT = '11111111-1111-4111-8111-111111111111'
RESOURCE = '22222222-2222-4222-8222-222222222222'
ACCOUNT = {'id': 20, 'execution_mode': 'canvas_cli', 'login_user_id': 'test-user',
    'cli_user_id': 'test-user', 'proxy_url': 'socks5://test.proxy:20020'}
REQUEST = {'kind': 'video', 'model': 'sd-2-5-480p-preview', 'upstream_model': MODEL}
digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
    separators=(',', ':')).encode()).hexdigest()


def failure(identifier='ijm_failed', *, ago=20, severe=False):
    stamp = NOW - ago
    binding = {'task_id': identifier, 'account_id': 20, 'project_id': PROJECT,
        'profile_sha256': digest(PROFILE), 'user_id_sha256': digest('test-user'),
        'proxy_sha256': digest(ACCOUNT['proxy_url'])}
    error = {'code': 'cli.invalid_service_response', 'class': 'internal_failure',
        'requiredAction': 'contact_support', 'retryable': False, 'outcome_unknown': False,
        'phase': 'uploading', 'at': stamp}
    context = {'backend': 'canvas_cli', 'task_id': identifier, 'account_id': 20,
        'profile': PROFILE, 'project_id': PROJECT, 'phase': 'uploading',
        'upload_checkpoint': {'binding': binding}, 'preparation_error': error,
        'preparation_timing': {'commands': {'resource upload': {'count': 3, 'failed': 1}}}}
    row = {'id': identifier, 'account_id': 20, 'execution_backend': 'canvas_cli',
        'model': REQUEST['model'], 'normalized': deepcopy(REQUEST), 'status': 'failed',
        'created_at': stamp - 60, 'completed_at': stamp, 'updated_at': stamp,
        'error_code': 'CLI_PREPARATION_RETRY_EXHAUSTED', 'upstream_context': context}
    if severe:
        context.pop('preparation_error')
        error.update(phase='run_dispatching', dispatch_started=True, outcome_unknown=True)
        context.update(phase='quoted', submission_phase='run_dispatching', submit_error=error,
            expected_user_id='test-user', resource_ids=[],
            reconciliation_closed=True, reconciliation_closed_at=stamp,
            reconciliation_close_reason='missing_output_resource_budget_exhausted', financial_outcome='unconfirmed',
            missing_resource_recovery={'version': 1, 'started_at': stamp-50, 'deadline': stamp+10,
                'attempts': 3, 'max_attempts': 3},
            submission_evidence={'version': 1, 'accepted': False, 'output_resources': False,
                'receipt': False, 'identity_conflict': False, 'ambiguous_partial': False},
            preparation_timing={'commands': {'node confirm': {'count': 1, 'failed': 0},
                                              'node run': {'count': 1, 'failed': 1}}})
        row['error_code'] = 'CLI_OUTPUT_RESOURCE_RECOVERY_EXHAUSTED'
    return row


def success(identifier='ijm_ok', *, ago=5, delivered=False, preserve_error=False):
    row = failure(identifier, ago=ago, severe=True)
    context = row['upstream_context']
    context.update(phase='accepted', submission_phase='accepted', resource_ids=[RESOURCE], output_count=1,
        dispatch_started_at=NOW-ago, submission_receipt_evidence=True)
    context['submission_evidence'].update(accepted=True, output_resources=True, receipt=delivered)
    if not preserve_error:
        context.pop('submit_error')
    row.update(status='succeeded' if delivered else 'running', completed_at=NOW-ago if delivered else None)
    return row


def evaluate(rows, *, account=None, normalized=None, profiles=None, **kwargs):
    return account_cooldowns(rows, [deepcopy(account or ACCOUNT)], deepcopy(normalized or REQUEST),
        profiles=profiles or {20: PROFILE}, now=NOW, **kwargs)


def test_two_distinct_preparation_failures_temporarily_avoid_only_this_model_and_account():
    rows = [failure('one'), failure('two', ago=60)]; before = deepcopy(rows)
    result = evaluate(rows)
    assert result == {20: {'blocked_until': NOW-20+1800,
        'reason': 'repeated_preparation_system_failure', 'tail_failure_ids': ['one', 'two'],
        'model': MODEL, 'kind': 'video', 'backend': 'canvas_cli'}}
    assert rows == before
    assert not evaluate(rows[:1])
    assert not evaluate([rows[0], rows[0]])


def test_one_closed_original_dispatch_system_failure_avoids_new_paid_attempts_without_refund_claim():
    row = failure(severe=True); before = deepcopy(row)
    result = evaluate([row])
    assert result[20]['reason'] == 'original_dispatch_system_failure'
    assert result[20]['tail_failure_ids'] == [row['id']]
    assert 'financial_outcome' not in result[20]
    assert row == before and row['upstream_context']['financial_outcome'] == 'unconfirmed'


@pytest.mark.parametrize('change', [
    {'code': 'MEDIA_REQUIREMENTS_INVALID'}, {'code': 'CONTENT_MODERATION_FAILED'},
    {'reason': 'copyright'}, {'reason': 'insufficient_credits'}, {'reason': 'model_not_in_catalog'},
    {'class': 'invalid_input'}, {'class': 'business_failure'}, {'serviceCode': 20115},
    {'requiredAction': 'login'}, {'class': 'authentication_failure'}, {'requiredAction': 'human_intervention'}])
def test_material_policy_credit_capability_and_authorization_errors_do_not_cool_generation_accounts(change):
    rows = [failure('one'), failure('two')]
    for row in rows:
        row['upstream_context']['preparation_error'].update(change)
    assert evaluate(rows) == {}


@pytest.mark.parametrize('action', ['human', 'human_intervention'])
def test_exact_vod_data_timeout_can_count_but_other_human_intervention_cannot(action):
    rows = [failure('one'), failure('two')]
    for row in rows:
        row['upstream_context']['preparation_error'].update(code='cli.upload_state_unknown',
            reason='vod.data_timeout', **{'class': 'human_intervention', 'requiredAction': action})
    assert evaluate(rows)[20]['reason'] == 'repeated_preparation_system_failure'
    rows[0]['upstream_context']['preparation_error']['reason'] = 'requires_manual_review'
    assert not evaluate(rows)


@pytest.mark.parametrize('change', [
    {'resource_ids': [RESOURCE]}, {'phase': 'accepted'}, {'reconciliation_closed': False},
    {'reconciliation_close_reason': 'manual_verification_unresolved'},
    {'financial_outcome': 'not_charged_this_invocation'},
    {'manual_termination': {}},
    {'missing_resource_recovery': {'version': 9, 'deadline': NOW-1}},
    {'preparation_timing': {'commands': {'node run': {'count': 2, 'failed': 1}}}},
])
def test_unknown_or_already_accepted_or_unbounded_dispatch_is_not_account_failure_proof(change):
    row = failure(severe=True); row['upstream_context'].update(change)
    assert not evaluate([row])


def test_legacy_partial_summary_is_not_positive_acceptance_under_complete_typed_failed_run_proof():
    row = failure(severe=True)
    row['upstream_context']['submission_receipt_evidence'] = True
    row['upstream_context']['submission_evidence']['ambiguous_partial'] = True
    before = deepcopy(row)
    assert evaluate([row])[20]['reason'] == 'original_dispatch_system_failure'
    assert row == before and row['upstream_context']['financial_outcome'] == 'unconfirmed'


@pytest.mark.parametrize('flag', ['accepted', 'output_resources', 'receipt', 'identity_conflict'])
def test_a_legacy_partial_bit_never_overrides_any_typed_positive_or_conflicting_receipt(flag):
    row = failure(severe=True)
    row['upstream_context']['submission_receipt_evidence'] = True
    row['upstream_context']['submission_evidence'].update(ambiguous_partial=True, **{flag: True})
    assert not evaluate([row])


@pytest.mark.parametrize('evidence', [None, {}, {'ambiguous_partial': True}])
def test_sparse_legacy_partial_without_complete_typed_negative_flags_stays_excluded(evidence):
    row = failure(severe=True)
    row['upstream_context']['submission_receipt_evidence'] = True
    row['upstream_context']['submission_evidence'] = evidence
    assert not evaluate([row])


def test_legacy_partial_marker_cannot_be_reclassified_as_ordinary_pre_dispatch_failure():
    rows = [failure('one'), failure('two')]
    for row in rows:
        row['upstream_context'].update(submission_receipt_evidence=True,
            submission_evidence={'version': 1, 'accepted': False, 'output_resources': False,
                'receipt': False, 'identity_conflict': False, 'ambiguous_partial': True})
    assert not evaluate(rows)


@pytest.mark.parametrize('change', [
    {'login_user_id': 'new-user', 'cli_user_id': 'new-user'},
    {'proxy_url': 'socks5://changed.proxy:20020'}, {'cli_user_id': 'foreign-user'},
    {'execution_mode': 'jimeng_web'}])
def test_old_identity_and_proxy_epochs_cannot_block_current_account(change):
    assert not evaluate([failure(severe=True)], account={**ACCOUNT, **change})


def test_rotated_credential_profile_clears_historical_health_without_mutating_tasks():
    assert not evaluate([failure(severe=True)], profiles={20: 'fresh-profile'})


def test_prepare_error_does_not_require_expected_uid_before_paid_submission():
    rows = [failure('one'), failure('two')]
    assert all('expected_user_id' not in row['upstream_context'] for row in rows)
    assert evaluate(rows)
    rows[0]['upstream_context']['upload_checkpoint']['binding']['user_id_sha256'] = digest('foreign')
    assert not evaluate(rows)


@pytest.mark.parametrize('delivered,preserve_error', [(False, False), (True, False), (True, True)])
def test_subsequent_functional_acceptance_or_delivered_recovery_clears_failures(delivered, preserve_error):
    rows = [failure(severe=True), success(delivered=delivered, preserve_error=preserve_error)]
    assert not evaluate(rows)


def test_older_concurrent_run_finishing_after_failure_does_not_clear_new_submission_failure():
    old = success(ago=100, delivered=True, preserve_error=True)
    old['completed_at'] = NOW-2
    old['updated_at'] = NOW
    assert evaluate([failure(severe=True, ago=20), old])


def test_newer_successful_dispatch_can_clear_failure_even_when_original_error_audit_is_retained():
    newer = success(ago=5, delivered=True, preserve_error=True)
    newer['completed_at'] = NOW-2
    assert not evaluate([failure(severe=True, ago=20), newer])


def test_a_new_whoami_or_query_updated_at_is_not_a_functional_recovery():
    row = failure(severe=True)
    row['updated_at'] = NOW
    assert evaluate([row])[20]['blocked_until'] == NOW-20+1800
    earlier_acceptance = success(ago=100)
    earlier_acceptance['updated_at'] = NOW
    assert evaluate([row, earlier_acceptance])


def test_same_timestamp_success_does_not_clear_failure_by_task_id_sort_order():
    assert evaluate([failure('aaa', severe=True, ago=20), success('zzz', ago=20)])


def test_expiry_is_not_renewed_by_restart_duplicate_history_or_status_queries():
    row = failure(severe=True, ago=1800)
    row['updated_at'] = NOW
    assert not evaluate([row, deepcopy(row)])
    assert not evaluate([failure(severe=True)], cooldown_seconds=0)


def test_other_models_backends_and_account_scopes_are_unaffected():
    row = failure(severe=True)
    assert not evaluate([row], normalized={**REQUEST, 'upstream_model': 'other_model'})
    row['execution_backend'] = 'jimeng_web'
    assert not evaluate([row])


def test_aliases_with_the_same_explicit_canonical_model_share_the_health_scope():
    assert evaluate([failure(severe=True)], normalized={**REQUEST, 'model': 'another-public-alias'})


def test_effective_image_mapping_uses_its_actual_target_without_polluting_original_pro_model():
    original = {'kind': 'image', 'model': 'seedream-5.0-pro', 'upstream_model': 'seedream_5.0_pro'}
    target = {**original, 'model': 'gpt-image-2.5-flare', 'upstream_model': 'gpt_image_25_flare'}
    row = failure(severe=True); row['normalized'] = original
    row['upstream_context']['image_model_route'] = {'account_id': 20,
        'requested_model': original['model'], 'requested_upstream_model': original['upstream_model'],
        'effective_model': target['model'], 'effective_upstream_model': target['upstream_model']}
    assert not evaluate([row], normalized=original)
    result = evaluate([row], normalized=original, effective_requests={20: target})
    assert result[20]['model'] == target['upstream_model'] and result[20]['kind'] == 'image'


@pytest.mark.parametrize('value', [True, -1, 86401, '1800', 1.2])
def test_cooldown_configuration_is_finite_and_bounded(value):
    with pytest.raises(ValueError):
        evaluate([], cooldown_seconds=value)


@pytest.mark.parametrize('change', [
    {'submit_error': []}, {'upload_checkpoint': []}, {'submission_evidence': []},
    {'preparation_timing': []}, {'previous_submissions': [{}]},
])
def test_malformed_or_foreign_legacy_audit_is_ignored(change):
    row = failure(severe=True); row['upstream_context'].update(change)
    assert evaluate([row]) == {}
