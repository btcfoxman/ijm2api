"""Caller-visible retry classification must preserve the original outcome facts."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.service import Service
from app.errors import public_error


@pytest.mark.parametrize('code', ['CLI_SUBMISSION_RETRYABLE', 'CLI_PREPARATION_TRANSIENT_FAILED'])
@pytest.mark.parametrize('raw', ['Canvas generation failed (20115)',
    'video may contain third-party content', 'image you uploaded community guidelines'])
def test_typed_service_retry_failure_cannot_be_reclassified_by_raw_policy_words(code, raw):
    assert public_error(code, raw) == {'code': code, 'message': '生成提交失败，请稍后重试~'}


def closed_task(kind='video', code='CLI_SUBMISSION_UNCONFIRMED'):
    return {
        'id': 'ijm_empty_original_operation', 'account_id': 19,
        'created_at': 1793374828, 'completed_at': 1793375149,
        'status': 'failed', 'progress': 100, 'model': 'sd-2-0',
        'execution_backend': 'canvas_cli', 'normalized': {'kind': kind},
        'result_urls': [], 'upstream_result_urls': [], 'run_id': '', 'thread_id': '',
        'error_code': code,
        'error_message': 'Canvas submission could not be confirmed within the bounded lookup window; '
                         'task failed without resubmission (original CLI: code=cli.invalid_service_response; '
                         'phase=run_dispatching; exit=1; class=internal_failure; action=contact_support)',
        'estimated_cost': 510, 'actual_cost': None, 'balance_before': 41682, 'balance_after': None,
        'upstream_context': {
            'backend': 'canvas_cli', 'task_id': 'ijm_empty_original_operation', 'account_id': 19,
            'profile': 'account-19', 'project_id': 'project-original', 'node_id': 'node-original',
            'update_id': 'update-original', 'submit_id': 'submit-original',
            'phase': 'quoted', 'submission_phase': 'run_dispatching', 'resource_ids': [],
            'dispatch_started_at': 1793374900,
            'reconciliation_closed': True, 'reconciliation_close_reason': 'operation_submission_absent',
            'consecutive_submission_absent': 3, 'financial_outcome': 'unconfirmed',
            'submit_error': {'code': 'cli.invalid_service_response', 'class': 'internal_failure',
                             'requiredAction': 'contact_support', 'retryable': False, 'exit_code': 1,
                             'phase': 'run_dispatching', 'dispatch_started': True, 'outcome_unknown': True},
            'last_query_error': {'code': 'cli.operation_submission_absent', 'phase': 'query',
                                 'outcome_unknown': False},
            'preparation_timing': {'commands': {'node run': {'count': 1, 'failed': 1}}},
        },
    }


def service():
    instance = object.__new__(Service)
    instance.store = Mock()
    instance._refresh_canvas_result_urls = Mock(side_effect=lambda task: task)
    return instance


@pytest.mark.parametrize('kind,entrypoint', [
    ('video', 'public_task'), ('image', 'public_task'), ('image', 'image_response'),
])
@pytest.mark.parametrize('code', ['CLI_SUBMISSION_UNCONFIRMED', 'CLI_SUBMISSION_RETRYABLE'])
def test_bounded_empty_service_failure_all_public_routes_allow_next_channel_without_rewriting(kind, entrypoint, code):
    task = closed_task(kind, code)
    before = deepcopy(task)
    instance = service()
    result = getattr(instance, entrypoint)(task)
    assert result['id'] == task['id'] and result['status'] == 'failed'
    assert result['error'] == {'code': 'CLI_SUBMISSION_RETRYABLE', 'message': '生成提交失败，请稍后重试~'}
    assert result['provider_context'] == {
        'may_have_been_accepted': True, 'financial_outcome': 'unconfirmed',
        'automatic_resubmission': False, 'fallback_allowed': True,
    }
    assert task == before and not instance.store.mock_calls
    assert 'generation_request_sent' not in result['provider_context']
    assert not any(word in result['error']['message'] for word in ['已返还', '退款', '核对上游'])


@pytest.mark.parametrize('evidence', ['accepted', 'resource', 'incomplete_lookup', 'auth_error'])
@pytest.mark.parametrize('code', ['CLI_SUBMISSION_UNCONFIRMED', 'CLI_SUBMISSION_RETRYABLE'])
def test_same_english_message_cannot_override_receipt_or_missing_retry_evidence(evidence, code):
    task = closed_task(code=code)
    context = task['upstream_context']
    if evidence == 'accepted':
        context['submission_phase'] = 'accepted'
    elif evidence == 'resource':
        context['resource_ids'] = ['original-output-resource']
    elif evidence == 'incomplete_lookup':
        context['consecutive_submission_absent'] = 2
    else:
        context['submit_error']['code'] = 'cli.authentication_required'
    before = deepcopy(task)
    instance = service()
    result = instance.public_task(task)
    assert result['error']['code'] == 'CLI_SUBMISSION_UNCONFIRMED'
    assert 'provider_context' not in result
    assert task == before and not instance.store.mock_calls
