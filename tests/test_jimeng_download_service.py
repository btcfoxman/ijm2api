"""A completed generation requires its own verified, plain download receipt."""

from copy import deepcopy
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from app.browser import BrowserError
from app.jimeng_client import JimengResultDownloadError
from app.region_policy import DEFAULT_REGION_GENERATION_POLICY
from app.service import Service
from app.store import Store


DOWNLOAD_URL = 'https://v3-dreamnia.jimeng.com/original-video.mp4?signature=synthetic'
PREVIEW_URL = 'https://v26-artist.vlabvod.com/preview-video.mp4?signature=synthetic'
HISTORY_ID = '12345678901234'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def output_state(context, *, credits=None):
    state = {'status': 'succeeded', 'state': 50, 'record_found': True,
        'upstream_task_id': HISTORY_ID, 'history_id': HISTORY_ID, 'result_urls': [DOWNLOAD_URL],
        'error': '', 'error_code': '', 'result_download_receipts': [{
            'version': 1, 'kind': 'jimeng_web_video_download', 'account_id': context.account['id'],
            'submit_id': context.submit_id, 'history_id': HISTORY_ID, 'item_id': '22345678901234',
            'video_id': 'v03055synthetic', 'source': 'get_local_item_list_download',
            'request_scene': 1, 'is_for_video_download': True, 'watermark_type': 1, 'logo_type': '',
            'source_url_sha256': sha(DOWNLOAD_URL), 'preview_url_sha256': sha(PREVIEW_URL),
            'history_watermark_type': 3, 'history_logo_type': 'synthetic-logo',
            'url_host': 'v3-dreamnia.jimeng.com', 'selection': 'origin_video',
            'width': 720, 'height': 1280, 'size_bytes': 123456, 'size_source': 'upstream_metadata',
        }]}
    if credits is not None:
        state['task_credit_evidence'] = {'amount': credits, 'submit_id': context.submit_id,
            'phase': 'succeeded', 'field': 'item_list[].extra.credits_consume'}
    return state


def download_error(context, *, retryable=True, credits=None):
    exc = JimengResultDownloadError('JIMENG_DOWNLOAD_UNAVAILABLE', retryable=retryable)
    state = output_state(context, credits=credits)
    exc.generation_state = {key: state[key] for key in ('status', 'state', 'record_found',
        'upstream_task_id', 'history_id', 'task_credit_evidence') if key in state}
    return exc


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'jimeng-download.db'))
    policy = deepcopy(DEFAULT_REGION_GENERATION_POLICY)
    policy['cn']['models'] = {'sd-2-0-mini': {'min_duration': 4, 'max_duration': 15}}
    store.update_settings({'region_generation_policy': policy})
    service = Service(store)
    monkeypatch.setattr(service._pool, 'submit', Mock())
    monkeypatch.setattr(service._stop, 'wait', Mock(return_value=False))
    account = store.upsert_account({'name': 'mainland-download', 'execution_mode': 'jimeng_web',
        'proxy_url': '', 'cdp_port': 19057})
    store.update_account(account['id'], {'enabled': True, 'status': 'active', 'balance': 100})
    account = store.account(account['id'], private=True)
    task_id = service.create({'model': 'sd-2-0-mini', 'prompt': 'Synthetic landscape', 'duration': 4})['id']
    submit_id = str(uuid.uuid4())
    context = {'backend': 'jimeng_web', 'site': 'jimeng', 'account_id': account['id'],
        'submit_id': submit_id, 'task_id': task_id, 'expected_user_id': 'synthetic-user',
        'submission_phase': 'submitted', 'upstream_task_id': HISTORY_ID,
        'quote_source': 'native_get_need_credits', 'quoted_credits': 12}
    store.update_task(task_id, {'account_id': account['id'], 'execution_backend': 'jimeng_web',
        'status': 'submitted', 'run_id': submit_id, 'thread_id': HISTORY_ID,
        'estimated_cost': 12, 'upstream_context': context,
        'cost_evidence': {'jimeng_quote': {'amount': 12, 'submit_id': submit_id}}})
    client = Mock(account=account)
    client.account_state.return_value = {'available_balance': 88}
    client.submit_prepared.side_effect = AssertionError('Result reads must not generate')
    monkeypatch.setattr(service, '_reserve_account', Mock(side_effect=AssertionError('Result reads cannot change accounts')))
    result = SimpleNamespace(service=service, store=store, account=account, task_id=task_id,
                             submit_id=submit_id, client=client)
    yield result
    client.submit_prepared.assert_not_called()
    service._reserve_account.assert_not_called()
    service.stop()


def run_poll(context):
    context.service._poll_jimeng(context.task_id, context.client, time.monotonic() + 60)
    return context.store.task(context.task_id)


def assert_original_binding(context, task):
    assert task['account_id'] == context.account['id']
    assert task['run_id'] == context.submit_id and task['thread_id'] == HISTORY_ID
    assert task['upstream_context']['submit_id'] == context.submit_id
    assert task['upstream_context']['upstream_task_id'] == HISTORY_ID
    assert all(call.args == (context.submit_id, HISTORY_ID) for call in context.client.poll.call_args_list)


def test_preview_without_download_receipts_cannot_become_success(context):
    state = output_state(context)
    state['result_urls'] = [PREVIEW_URL]
    state.pop('result_download_receipts')
    context.client.poll.return_value = state
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert task['result_urls'] == [] and task['upstream_result_urls'] == []
    assert task['upstream_context']['result_download']['phase'] == 'failed'
    assert task['upstream_context']['result_download']['attempts'] == 1
    context.client.poll.assert_called_once()
    context.client.account_state.assert_not_called()
    assert context.service.public_task(task)['error'] == {'code': 'RESULT_TRANSFER_FAILED',
        'message': '生成结果保存暂未完成，请稍后查询~'}
    assert_original_binding(context, task)


def test_transient_download_error_retries_only_original_poll_and_persists_receipt(context):
    context.client.poll.side_effect = [download_error(context), output_state(context, credits=12)]
    def during_retry(_):
        pending = context.store.task(context.task_id)
        assert pending['status'] == 'running' and pending['progress'] == 95
        assert pending['error_code'] == '' and pending['error_message'] == ''
        assert context.service.public_task(pending).get('error') is None
        return False
    context.service._stop.wait.side_effect = during_retry
    task = run_poll(context)
    assert task['status'] == 'succeeded' and task['result_urls'] == [DOWNLOAD_URL]
    assert context.client.poll.call_count == 2
    context.service._stop.wait.assert_called_once_with(1)
    download = task['upstream_context']['result_download']
    assert download['phase'] == 'verified' and download['attempts'] == 1
    assert download['receipts'][0]['watermark_type'] == 1
    assert download['receipts'][0]['source_url_sha256'] == sha(DOWNLOAD_URL)
    assert download['generation_completed'] is True and download['automatic_resubmission'] is False
    assert task['actual_cost'] == 12 and task['estimated_cost'] == 12
    assert task['normalized']['_cost_evidence']['jimeng_quote']['amount'] == 12
    assert task['normalized']['_cost_evidence']['upstream_task_credits']['amount'] == 12
    assert task['upstream_context']['quoted_credits'] == 12
    assert_original_binding(context, task)


@pytest.mark.parametrize('amount', [0, 12])
def test_download_failure_credit_evidence_survives_a_later_receipt_without_credit_data(context, amount):
    context.client.poll.side_effect = [download_error(context, credits=amount), output_state(context)]
    task = run_poll(context)
    assert task['status'] == 'succeeded' and task['actual_cost'] == amount
    assert task['normalized']['_cost_evidence']['upstream_task_credits']['amount'] == amount
    assert task['normalized']['_cost_evidence']['jimeng_quote']['amount'] == 12


def test_permanent_download_failure_ends_immediately_without_exposing_raw_diagnostics(context):
    exc = download_error(context, retryable=False, credits=12)
    exc.raw_response = {'url': PREVIEW_URL}
    context.client.poll.side_effect = exc
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert task['actual_cost'] == 12
    context.client.poll.assert_called_once()
    context.service._stop.wait.assert_not_called()
    assert task['upstream_context']['result_download']['retryable'] is False
    assert PREVIEW_URL not in json.dumps(task) and DOWNLOAD_URL not in json.dumps(task)
    assert_original_binding(context, task)


def test_transient_download_failure_has_a_small_persisted_budget(context):
    context.client.poll.side_effect = [download_error(context) for _ in range(3)]
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert context.client.poll.call_count == 3
    assert [call.args for call in context.service._stop.wait.call_args_list] == [(1,), (2,)]
    download = task['upstream_context']['result_download']
    assert download['attempts'] == 3 and download['phase'] == 'failed'
    assert task['result_urls'] == []
    assert_original_binding(context, task)


def test_resumed_download_does_not_reset_an_existing_retry_budget(context):
    context.service._jimeng_update(context.task_id, context={'result_download': {
        'attempts': 2, 'phase': 'retrying', 'generation_completed': True}})
    context.client.poll.side_effect = download_error(context)
    task = run_poll(context)
    assert task['status'] == 'failed' and context.client.poll.call_count == 1
    assert task['upstream_context']['result_download']['attempts'] == 3
    context.service._stop.wait.assert_not_called()
    assert_original_binding(context, task)


def test_missing_history_after_confirmed_completion_stays_in_the_download_budget(context):
    missing = {'status': 'running', 'record_found': False, 'result_urls': [],
               'error': '', 'error_code': ''}
    context.client.poll.side_effect = [download_error(context, credits=12), missing, missing]
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert context.client.poll.call_count == 3 and task['result_urls'] == []
    download = task['upstream_context']['result_download']
    assert download['attempts'] == 3 and download['phase'] == 'failed'
    assert download['generation_completed'] is True
    assert task['actual_cost'] == 12
    assert task['normalized']['_cost_evidence']['upstream_task_credits']['amount'] == 12
    assert_original_binding(context, task)


@pytest.mark.parametrize('generation_evidence', [True, False])
def test_later_history_transport_errors_use_the_small_download_budget(context, generation_evidence):
    first = download_error(context)
    if not generation_evidence:
        first.generation_state = {}
    context.client.poll.side_effect = [first,
        BrowserError('A transport failed at ' + PREVIEW_URL), download_error(context)]
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert context.client.poll.call_count == 3
    assert task['upstream_context']['result_download']['attempts'] == 3
    assert PREVIEW_URL not in json.dumps(task)
    assert_original_binding(context, task)


@pytest.mark.parametrize('field,value', [('account_id', 99999), ('submit_id', str(uuid.uuid4())),
                                       ('history_id', '99999999999999'), ('source_url_sha256', '0' * 64)])
def test_foreign_or_unbound_download_receipt_cannot_publish_a_video(context, field, value):
    state = output_state(context)
    state['result_download_receipts'][0][field] = value
    context.client.poll.return_value = state
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert task['result_urls'] == []
    context.client.poll.assert_called_once()
    assert_original_binding(context, task)


def test_receipt_persistence_contains_only_safe_fields_and_preserves_costs(context):
    state = output_state(context, credits=0)
    state['result_download_receipts'][0]['raw_url'] = PREVIEW_URL
    context.client.poll.return_value = state
    task = run_poll(context)
    assert task['status'] == 'succeeded' and task['actual_cost'] == 0
    receipt = task['upstream_context']['result_download']['receipts'][0]
    assert 'raw_url' not in receipt and PREVIEW_URL not in json.dumps(task['upstream_context'])
    assert receipt['history_watermark_type'] == 3 and receipt['history_logo_type'] == 'synthetic-logo'
    assert receipt['watermark_type'] == 1 and receipt['logo_type'] == ''
    assert receipt['size_source'] == 'upstream_metadata'


def test_operator_termination_during_download_query_wins_over_late_success(context):
    def poll(*_):
        current = context.store.task(context.task_id)
        context.store.update_task(context.task_id, {'status': 'failed', 'progress': 100,
            'error_code': 'MANUAL_TASK_TERMINATED', 'error_message': 'manually ended',
            'upstream_context': {**current['upstream_context'], 'manual_termination': {'operator': True}}})
        return output_state(context, credits=12)
    context.client.poll.side_effect = poll
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'MANUAL_TASK_TERMINATED'
    assert task['result_urls'] == [] and task['actual_cost'] is None
    assert 'result_download' not in task['upstream_context']
    context.client.account_state.assert_not_called()


def test_operator_termination_during_balance_read_cannot_be_overwritten(context):
    context.client.poll.return_value = output_state(context, credits=12)
    def read_balance(**_):
        current = context.store.task(context.task_id)
        context.store.update_task(context.task_id, {'status': 'failed', 'error_code': 'MANUAL_TASK_TERMINATED',
            'upstream_context': {**current['upstream_context'], 'manual_termination': {'operator': True}}})
        return {'available_balance': 88}
    context.client.account_state.side_effect = read_balance
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'MANUAL_TASK_TERMINATED'
    assert task['result_urls'] == []
    assert 'result_download' not in task['upstream_context']


def test_service_stop_preserves_original_operation_for_recovery(context):
    def stopped(*_):
        context.service._stop.set()
        raise download_error(context)
    context.client.poll.side_effect = stopped
    task = run_poll(context)
    assert task['status'] == 'submitted' and task['error_code'] == ''
    assert task['result_urls'] == []
    assert_original_binding(context, task)


def test_download_retry_reaching_deadline_remains_a_result_error(context, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('app.jimeng_service.time.monotonic', lambda: clock[0])
    context.client.poll.side_effect = download_error(context)
    def wait(seconds):
        clock[0] += seconds
        return False
    context.service._stop.wait.side_effect = wait
    context.service._poll_jimeng(context.task_id, context.client, 100.5)
    task = context.store.task(context.task_id)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    context.client.poll.assert_called_once()
    download = task['upstream_context']['result_download']
    assert download['phase'] == 'failed' and download['deadline_exhausted'] is True
    assert_original_binding(context, task)
