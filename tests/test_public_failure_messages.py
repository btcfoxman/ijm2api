"""Reason-specific public failures must not expose prose or imply resubmission."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.errors import public_error
from app.service import Service


PRIVATE = ('private-token private-prompt private-upstream-prose '
           'https://private.example/material?signature=private-signature '
           'socks5://private-user:private-password@private-proxy:20009')
UNKNOWN = '提交结果未确认，请核对上游任务，暂勿重复提交~'


@pytest.mark.parametrize('code,expected', [
    ('MEDIA_UPLOAD_FAILED', '素材上传失败，请检查素材后重试~'),
    ('MEDIA_UPLOAD_PENDING', '素材仍在处理中，请稍后重试~'),
    ('RESULT_TRANSFER_FAILED', '生成结果保存暂未完成，请稍后查询~'),
    ('ACCOUNT_POOL_UNAVAILABLE', '当前可用生成资源不足，请稍后重试~'),
    ('ACCOUNT_MISSING', '任务执行资源不可用，请联系管理员核查~'),
    ('ACCOUNT_VERIFICATION_FAILED', '生成服务校验失败，请稍后重试~'),
    ('BROWSERERROR', '生成连接异常，请稍后查询或联系管理员~'),
    ('CLI_CAPABILITY_UNAVAILABLE', '所选模型不支持该参数或素材组合，请调整后重试~'),
    ('CLI_AUTH_REQUIRED', '生成服务授权暂不可用，请稍后重试~'),
    ('CLI_AUTH_EXPIRED', '生成服务授权暂不可用，请稍后重试~'),
    ('cli.authentication_required', '生成服务授权暂不可用，请稍后重试~'),
    ('CLI_COMMAND_TIMEOUT', '生成请求处理超时，请稍后查询~'),
    ('CLI_TASK_DEADLINE_EXCEEDED', '生成请求处理超时，请稍后查询~'),
    ('CLI_INVALID_RESPONSE', '生成服务响应异常，请稍后重试~'),
    ('cli.invalid_service_response', '生成服务响应异常，请稍后重试~'),
    ('SERVICE.1', '生成服务暂时不可用，请稍后重试~'),
    ('CLI_PREPARATION_TRANSIENT_FAILED', '生成提交失败，请稍后重试~'),
    ('CLI_SUBMISSION_RETRYABLE', '生成提交失败，请稍后重试~'),
    ('CLI_UNRECOGNIZED_FAILURE', '生成请求处理失败，请稍后查询或联系管理员~'),
    ('cli.unrecognized_failure', '生成请求处理失败，请稍后查询或联系管理员~'),
])
def test_failure_categories_use_fixed_reasons_without_maintenance_or_financial_claims(code, expected):
    result = public_error(code, PRIVATE)
    assert result == {'code': code.upper(), 'message': expected}
    assert '维护' not in result['message']
    assert not any(word in result['message'] for word in ['已返还', '已退还', '退款'])
    assert not any(secret in str(result) for secret in ['private-', 'https://', 'socks5://', 'signature='])


@pytest.mark.parametrize('code', [
    'CLI_SUBMISSION_UNCONFIRMED', 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'UPSTREAM_TIMEOUT',
])
@pytest.mark.parametrize('raw', [PRIVATE, 'Canvas generation failed (20115)',
    'Canvas submission could not be confirmed within bounded lookup window; task failed without resubmission'])
def test_uncertain_submission_keeps_code_and_never_encourages_a_new_submission(code, raw):
    result = public_error(code, raw)
    assert result == {'code': code, 'message': UNKNOWN}
    assert '暂勿重复提交' in result['message']
    assert not any(word in result['message'] for word in ['维护', '请重试', '重新提交', '已返还', '退款'])
    assert 'private-' not in str(result)


def test_legacy_browser_upload_failure_is_sanitized_without_exposing_original_prose():
    result = public_error('UPSTREAM_FAILED', 'web upload failed: ' + PRIVATE)
    assert result == {'code': 'MEDIA_UPLOAD_FAILED', 'message': '素材上传失败，请检查素材后重试~'}
    assert not any(word in str(result) for word in ['维护', 'private-', '已返还'])


def test_f758_historical_public_projection_keeps_unknown_code_and_all_private_records_unchanged():
    task = {'id': 'ijm_f7581102dcc44d30', 'created_at': 1793331294.9979024,
        'status': 'failed', 'progress': 100, 'model': 'sd-2-0', 'normalized': {'kind': 'video'},
        'account_id': 19, 'result_urls': [], 'error_code': 'CLI_SUBMISSION_UNCONFIRMED',
        'error_message': 'Canvas submission could not be confirmed within bounded lookup window; '
            'task failed without resubmission; original=cli.invalid_service_response; ' + PRIVATE,
        'estimated_cost': 192, 'reserved_cost': 192, 'actual_cost': None,
        'balance_before': 1000, 'balance_after': None, 'financial_outcome': 'unconfirmed',
        'upstream_context': {'phase': 'quoted', 'submission_phase': 'run_dispatching', 'resource_ids': [],
            'submit_error': {'code': 'cli.invalid_service_response', 'outcome_unknown': True},
            'financial_outcome': 'unconfirmed', 'private_detail': PRIVATE}}
    before = deepcopy(task)
    service = object.__new__(Service)
    service.store = Mock()
    service._refresh_canvas_result_urls = Mock(side_effect=lambda row: row)
    result = service.public_task(task)
    assert result['id'] == task['id'] and result['status'] == 'failed'
    assert result['error'] == {'code': 'CLI_SUBMISSION_UNCONFIRMED', 'message': UNKNOWN}
    assert not any(word in str(result) for word in ['维护', 'private-', '已返还', '退款'])
    assert task == before
    assert not service.store.mock_calls
    service._refresh_canvas_result_urls.assert_called_once_with(task)
