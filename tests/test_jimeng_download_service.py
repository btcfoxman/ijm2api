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
from app.result_transfer import ResultTransferError
from app.region_policy import DEFAULT_REGION_GENERATION_POLICY
from app.service import Service
from app.store import Store


DOWNLOAD_URL = 'https://v3-dreamnia.jimeng.com/original-video.mp4?signature=synthetic'
PREVIEW_URL = 'https://v26-artist.vlabvod.com/preview-video.mp4?signature=synthetic'
HISTORY_ID = '12345678901234'
STORED_URL = 'https://storage.example.test/original.mp4?signature=synthetic'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def stored_output(url, task_id, storage, *, index=0, on_phase=None,
                  persist_checkpoint=None, **kwargs):
    assert on_phase('download') is True
    assert persist_checkpoint({'version': 1, 'task_id': task_id, 'index': index}) is True
    return STORED_URL + f'&index={index}', {
        'bucket': 'assets', 'key': f'jimeng/{task_id}/original-{index}.mp4',
        'size_bytes': 123814, 'sha256': '1' * 64, 'content_type': 'video/mp4',
        'original_bytes': True, 'upload_id': 'synthetic-upload', 'expires_at': time.time() + 3600,
        'source_url_sha256': sha(url), 'public_download_verified': True,
        'public_download_verified_at': time.time(), 'width': 720, 'height': 1280, 'duration_seconds': 4.0,
    }


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
    transfer = Mock(side_effect=stored_output)
    monkeypatch.setattr('app.jimeng_service._transfer_jimeng_original', transfer)
    monkeypatch.setattr(service, '_reserve_account', Mock(side_effect=AssertionError('Result reads cannot change accounts')))
    result = SimpleNamespace(service=service, store=store, account=account, task_id=task_id,
                             submit_id=submit_id, client=client, transfer=transfer)
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
    assert task['status'] == 'succeeded' and task['result_urls'] == [STORED_URL + '&index=0']
    assert task['upstream_result_urls'] == [DOWNLOAD_URL]
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


def test_original_cdn_is_private_evidence_and_only_verified_s3_is_published(context):
    context.client.poll.return_value = output_state(context, credits=0)
    task = run_poll(context)
    assert task['status'] == 'succeeded' and task['actual_cost'] == 0
    assert task['result_urls'] == [STORED_URL + '&index=0']
    assert task['upstream_result_urls'] == [DOWNLOAD_URL]
    transfer = task['result_transfer']
    assert transfer['original_bytes'] is True and transfer['source'] == 'jimeng_web_download'
    stored = transfer['items'][0]
    assert stored['public_download_verified'] is True and stored['content_type'] == 'video/mp4'
    # CDN metadata is not an actual-byte promise: use the downloaded size/hash.
    assert stored['size_bytes'] == 123814 and stored['sha256'] == '1' * 64
    assert task['upstream_context']['result_download']['receipts'][0]['size_bytes'] == 123456
    assert task['upstream_context']['result_storage']['phase'] == 'verified'
    assert DOWNLOAD_URL not in json.dumps(task['upstream_context'])
    assert_original_binding(context, task)


def test_storage_failure_has_small_budget_and_never_falls_back_to_cdn(context):
    context.client.poll.return_value = output_state(context, credits=12)
    context.transfer.side_effect = ResultTransferError('secret signed URL ' + DOWNLOAD_URL,
        code='S3_TRANSPORT_FAILED', stage='upload', retryable=True)
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert task['result_urls'] == [] and task['upstream_result_urls'] == [DOWNLOAD_URL]
    assert task['actual_cost'] == 12
    assert task['upstream_context']['result_storage']['attempts'] == 3
    assert task['upstream_context']['result_download']['phase'] == 'verified'
    assert DOWNLOAD_URL not in task['error_message']
    assert DOWNLOAD_URL not in json.dumps(task['upstream_context'])
    assert context.transfer.call_count == 3 and context.client.poll.call_count == 1
    assert [call.args for call in context.service._stop.wait.call_args_list] == [(1,), (2,)]
    assert_original_binding(context, task)


def test_storage_retries_resume_the_original_upload_checkpoint(context):
    context.client.poll.return_value = output_state(context)
    checkpoint = {'version': 1, 'task_id': context.task_id, 'index': 0,
                  'upload_id': 'synthetic-persisted-session'}
    attempts = []
    def interrupted_upload(*args, **kwargs):
        attempts.append(kwargs['checkpoint'])
        if len(attempts) == 1:
            assert kwargs['persist_checkpoint'](checkpoint) is True
            raise ResultTransferError('synthetic disconnect', code='S3_TRANSPORT_FAILED', retryable=True)
        return stored_output(*args, **kwargs)
    context.transfer.side_effect = interrupted_upload
    task = run_poll(context)
    assert task['status'] == 'succeeded'
    assert attempts == [None, checkpoint]
    assert task['upstream_context']['result_storage']['attempts'] == 1
    context.client.poll.assert_called_once()


@pytest.mark.parametrize('field,value', [
    ('public_download_verified', False), ('content_type', 'text/html'),
    ('sha256', ''), ('source_url_sha256', '0' * 64), ('original_bytes', False),
    ('public_download_verified_at', None), ('size_bytes', 0),
])
def test_storage_requires_actual_original_file_and_public_download_evidence(context, field, value):
    context.client.poll.return_value = output_state(context)
    def invalid(*args, **kwargs):
        url, metadata = stored_output(*args, **kwargs)
        metadata[field] = value
        return url, metadata
    context.transfer.side_effect = invalid
    task = run_poll(context)
    assert task['status'] == 'failed' and task['result_urls'] == []
    assert task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert task['upstream_context']['result_storage']['reason_code'] == 'JIMENG_STORAGE_RECEIPT_INVALID'
    context.transfer.assert_called_once()


def test_partial_storage_resumes_without_reupload_and_retains_source_binding(context):
    first = output_state(context, credits=12)
    second_url = DOWNLOAD_URL.replace('original-video', 'second-video')
    second = deepcopy(first['result_download_receipts'][0])
    second.update(item_id='22345678901235', video_id='v03055synthetic2', source_url_sha256=sha(second_url))
    first['result_urls'].append(second_url)
    first['result_download_receipts'].append(second)
    context.client.poll.return_value = first
    calls = []
    def stop_second(*args, **kwargs):
        calls.append(kwargs['index'])
        if kwargs['index'] == 1:
            context.service._stop.set()
            raise ResultTransferError('stopped', code='S3_INTERRUPTED', retryable=False)
        return stored_output(*args, **kwargs)
    context.transfer.side_effect = stop_second
    pending = run_poll(context)
    assert pending['status'] == 'running' and pending['result_urls'] == []
    assert pending['actual_cost'] == 12
    assert list(pending['upstream_context']['result_storage']['completed']) == ['22345678901234']
    context.service._stop.clear()
    refreshed = deepcopy(first)
    refreshed['result_urls'][0] = DOWNLOAD_URL + '&renewed=1'
    refreshed['result_download_receipts'][0]['source_url_sha256'] = sha(refreshed['result_urls'][0])
    context.client.poll.return_value = refreshed
    context.transfer.side_effect = stored_output
    task = run_poll(context)
    assert task['status'] == 'succeeded'
    assert task['result_urls'] == [STORED_URL + '&index=0', STORED_URL + '&index=1']
    assert task['upstream_result_urls'] == [DOWNLOAD_URL, second_url]
    assert task['upstream_context']['result_download']['receipts'][0]['source_url_sha256'] == sha(DOWNLOAD_URL)
    assert [call.kwargs['index'] for call in context.transfer.call_args_list] == [0, 1, 1]
    assert task['actual_cost'] == 12
    assert_original_binding(context, task)


def test_storage_cancellation_between_chunks_prevents_late_success(context):
    context.client.poll.return_value = output_state(context, credits=12)
    def cancel(*args, **kwargs):
        assert kwargs['on_phase']('download') is True
        current = context.store.task(context.task_id)
        context.store.update_task(context.task_id, {'status': 'failed', 'error_code': 'MANUAL_TASK_TERMINATED',
            'upstream_context': {**current['upstream_context'], 'manual_termination': {'operator': True}}})
        assert kwargs['on_phase']('download') is False
        assert kwargs['persist_checkpoint']({'upload_id': 'must-not-save'}) is False
        raise ResultTransferError('interrupted', retryable=False)
    context.transfer.side_effect = cancel
    task = run_poll(context)
    assert task['status'] == 'failed' and task['error_code'] == 'MANUAL_TASK_TERMINATED'
    assert task['result_urls'] == [] and task['actual_cost'] == 12
    assert task['upstream_context']['result_storage']['checkpoints'] == {}
    context.transfer.assert_called_once()


def test_storage_retry_budget_survives_restart(context):
    context.client.poll.return_value = output_state(context)
    context.service._jimeng_update(context.task_id, context={'result_storage': {
        'phase': 'retrying', 'attempts': 2, 'generation_completed': True}})
    context.transfer.side_effect = ResultTransferError('synthetic outage', code='S3_TRANSPORT_FAILED', retryable=True)
    task = run_poll(context)
    assert task['status'] == 'failed' and task['upstream_context']['result_storage']['attempts'] == 3
    context.transfer.assert_called_once()
    context.service._stop.wait.assert_not_called()


def test_expired_s3_link_refreshes_without_repolling_or_regenerating(context, monkeypatch):
    context.client.poll.return_value = output_state(context)
    task = run_poll(context)
    metadata = deepcopy(task['result_transfer'])
    metadata['items'][0]['expires_at'] = time.time() - 1
    context.store.update_task(context.task_id, {'result_transfer': metadata})
    renewed = STORED_URL + '&renewed=1'
    presign = Mock(return_value=(renewed, {'expires_at': time.time() + 3600}))
    monkeypatch.setattr('app.result_transfer.presign', presign)
    public = context.service.public_task(context.store.task(context.task_id))
    assert public['data'] == [{'url': renewed}]
    assert context.store.task(context.task_id)['upstream_result_urls'] == [DOWNLOAD_URL]
    context.client.poll.assert_called_once()
    context.transfer.assert_called_once()


def test_deadline_exhausted_during_balance_read_is_a_delivery_failure(context, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('app.jimeng_service.time.monotonic', lambda: clock[0])
    context.client.poll.return_value = output_state(context, credits=12)
    def balance(**_):
        clock[0] = 102.0
        return {'available_balance': 88}
    context.client.account_state.side_effect = balance
    context.service._poll_jimeng(context.task_id, context.client, 101.0)
    task = context.store.task(context.task_id)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert task['result_urls'] == [] and task['actual_cost'] == 12
    assert task['upstream_context']['result_storage']['deadline_exhausted'] is True
    context.transfer.assert_not_called()


@pytest.mark.parametrize('url', ['http://storage.example.test/file.mp4',
                               'https://user:password@storage.example.test/file.mp4'])
def test_storage_cannot_publish_unsafe_external_url(context, url):
    context.client.poll.return_value = output_state(context)
    def invalid(*args, **kwargs):
        _, metadata = stored_output(*args, **kwargs)
        return url, metadata
    context.transfer.side_effect = invalid
    task = run_poll(context)
    assert task['status'] == 'failed' and task['result_urls'] == []
    assert task['error_code'] == 'RESULT_TRANSFER_FAILED'
    context.transfer.assert_called_once()


def test_persisted_storage_binding_cannot_change_to_another_video(context):
    state = output_state(context)
    context.client.poll.return_value = state
    receipt = state['result_download_receipts'][0]
    binding = {key: receipt[key] for key in ('account_id', 'submit_id', 'history_id', 'item_id', 'video_id')}
    binding.update(index=0, video_id='v03055foreign')
    context.service._jimeng_update(context.task_id, context={'result_storage': {
        'bindings': {receipt['item_id']: binding}, 'phase': 'transferring', 'attempts': 0}})
    task = run_poll(context)
    assert task['status'] == 'failed' and task['result_urls'] == []
    assert task['upstream_context']['result_storage']['reason_code'] == 'JIMENG_STORAGE_BINDING_INVALID'
    context.transfer.assert_not_called()


def test_download_chunks_check_cancellation_without_repeating_sqlite_writes(context, monkeypatch):
    context.client.poll.return_value = output_state(context)
    updates = Mock(wraps=context.store.update_task)
    monkeypatch.setattr(context.store, 'update_task', updates)
    def chunks(*args, **kwargs):
        assert kwargs['on_phase']('jimeng_download') is True
        first = updates.call_count
        for _ in range(50):
            assert kwargs['on_phase']('jimeng_download') is True
        assert updates.call_count == first
        assert kwargs['on_phase']('s3_put') is True
        assert updates.call_count == first + 1
        return stored_output(*args, **kwargs)
    context.transfer.side_effect = chunks
    task = run_poll(context)
    assert task['status'] == 'succeeded'


def test_expired_resumed_storage_stays_a_result_failure_without_new_query(context):
    context.service._jimeng_update(context.task_id, {'status': 'running', 'actual_cost': 12},
        {'result_download': {'phase': 'verified', 'generation_completed': True},
         'result_storage': {'phase': 'transferring', 'generation_completed': True, 'attempts': 1}})
    context.service._poll_jimeng(context.task_id, context.client, time.monotonic() - 1)
    task = context.store.task(context.task_id)
    assert task['status'] == 'failed' and task['error_code'] == 'RESULT_TRANSFER_FAILED'
    assert task['actual_cost'] == 12 and task['result_urls'] == []
    assert task['upstream_context']['result_storage']['deadline_exhausted'] is True
    context.client.poll.assert_not_called()
    context.transfer.assert_not_called()
