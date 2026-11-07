"""A lost Jimeng receipt can only query the original durable submission."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

import pytest

from app.browser import BrowserError, BrowserRejected
from app.jimeng_client import JimengPageNotReady, JimengProtocolUnavailable, PreparedJimengSubmission
from app.media import MediaError
from app.upload import UploadError
from app.region_policy import DEFAULT_REGION_GENERATION_POLICY
from app.service import AccountMaintenanceBusy, Service
from app.store import Store


def with_download_receipt(state, *, account_id, submit_id, history_id=''):
    """Match the native client contract without changing each test's scenario."""
    url = state['result_urls'][0]
    preview = 'https://preview.example.test/watermarked.mp4'
    history_id = history_id or state.get('history_id') or '12345678901234'
    state['result_download_receipts'] = [{
        'version': 1, 'kind': 'jimeng_web_video_download', 'account_id': account_id,
        'submit_id': submit_id, 'history_id': history_id, 'item_id': '22345678901234',
        'video_id': 'v03055synthetic', 'source': 'get_local_item_list_download',
        'request_scene': 1, 'is_for_video_download': True, 'watermark_type': 1, 'logo_type': '',
        'source_url_sha256': hashlib.sha256(url.encode()).hexdigest(),
        'preview_url_sha256': hashlib.sha256(preview.encode()).hexdigest(),
        'history_watermark_type': 3, 'history_logo_type': 'synthetic-logo',
        'url_host': 'v3-dreamnia.jimeng.com', 'selection': 'origin_video',
        'width': 1280, 'height': 720, 'size_bytes': 123456,
    }]
    return state


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'jimeng.db'))
    policies = deepcopy(DEFAULT_REGION_GENERATION_POLICY)
    policies['cn']['models'] = {'sd-2-0-mini': {'min_duration': 4, 'max_duration': 15},
                               'gpt-image-2.5-flare': {}}
    store.update_settings({'region_generation_policy': policies, 'task_timeout_seconds': 10})
    service = Service(store)
    monkeypatch.setattr(service._pool, 'submit', Mock())
    monkeypatch.setattr(service.browser_runtime, 'acquire', Mock(side_effect=lambda a, **_: (a, False)))
    monkeypatch.setattr(service.browser_runtime, 'release', Mock())
    account = store.upsert_account({'name': 'mainland', 'execution_mode': 'jimeng_web',
        'proxy_url': '', 'cdp_port': 19008})
    store.update_account(account['id'], {'enabled': True, 'status': 'active', 'balance': 100,
        'login_user_id': 'synthetic-jimeng-user'})
    account = store.account(account['id'], private=True)
    submit_id = str(uuid.uuid4())
    scene = {'modelReqKey': 'dreamina_seedance_40_mini', 'resolution': '720p',
             'videoDuration': 4, 'inputVideoDuration': 0, 'materialTypes': []}
    body = {'submit_id': submit_id, 'extend': {'root_model': 'dreamina_seedance_40_mini',
            'm_video_commerce_info': {'amount': 4}},
            'metrics_extra': json.dumps({'sceneOptions': json.dumps([scene])})}
    prepared = PreparedJimengSubmission(submit_id, 'video', body,
        {'generate_id': 'gen-' + str(uuid.uuid4())}, quoted_credits=24)
    client = Mock(account=account)
    client.preflight_model.return_value = None
    client.login_identity.return_value = {'authenticated': True, 'user_id': 'synthetic-jimeng-user'}
    client.account_state.side_effect = [{'available_balance': 100, 'buckets': {}},
                                      {'available_balance': 100, 'buckets': {}},
                                      {'available_balance': 93, 'buckets': {}}]
    client.upload_inputs.return_value = {'images': [], 'videos': [], 'audios': []}
    client.prepare_submit.return_value = prepared
    client.submit_prepared.return_value = {'run_id': submit_id, 'thread_id': '', 'upstream_task_id': 'upstream-task'}
    client.poll.return_value = with_download_receipt(
        {'status': 'succeeded', 'result_urls': ['https://v3-dreamnia.jimeng.com/original.mp4'],
         'error': '', 'error_code': ''}, account_id=account['id'], submit_id=submit_id)
    monkeypatch.setattr('app.service.JimengWebClient', Mock(return_value=client))
    international = Mock(side_effect=AssertionError('Jimeng cannot use Dreamina transport'))
    image = Mock(side_effect=AssertionError('Jimeng cannot use Dreamina image transport'))
    monkeypatch.setattr('app.service.DreaminaClient', international)
    monkeypatch.setattr('app.service.DreaminaImageClient', image)
    monkeypatch.setattr(service.canvas_cli, 'run', Mock(side_effect=AssertionError('Jimeng cannot invoke CLI')))
    def stored_output(url, task_id, storage, *, index=0, **kwargs):
        return 'https://storage.example.test/original.mp4', {
            'bucket': 'assets', 'key': f'jimeng/{task_id}/original-{index}.mp4',
            'size_bytes': 123814, 'sha256': '1' * 64, 'content_type': 'video/mp4',
            'original_bytes': True, 'upload_id': 'synthetic-upload', 'expires_at': time.time() + 3600,
            'source_url_sha256': hashlib.sha256(url.encode()).hexdigest(),
            'public_download_verified': True, 'public_download_verified_at': time.time(),
        }
    monkeypatch.setattr('app.jimeng_service._transfer_jimeng_original', Mock(side_effect=stored_output))
    yield SimpleNamespace(store=store, service=service, account=account, client=client, prepared=prepared,
                          international=international, image=image, policies=policies)
    service.stop()
    international.assert_not_called()
    image.assert_not_called()
    service.canvas_cli.run.assert_not_called()
    assert not service._credit_reservations
    assert sum(service._active.values()) == 0
    assert service._worker_slots._value == 50


def create(context, *, image=False):
    if image:
        return context.service.create_image({'model': 'gpt-image-2.5-flare', 'prompt': 'Synthetic landscape'})['id']
    return context.service.create({'model': 'sd-2-0-mini', 'prompt': 'Synthetic landscape', 'duration': 4})['id']


def test_original_uuid_and_owner_are_committed_before_the_only_generating_call(context):
    identifier = create(context)

    def submit(prepared):
        durable = context.store.task(identifier)
        assert durable['run_id'] == prepared.submit_id
        assert durable['thread_id'] == '' and durable['status'] == 'submitted'
        assert durable['execution_backend'] == 'jimeng_web'
        assert durable['upstream_context']['submit_id'] == prepared.submit_id
        assert durable['upstream_context']['trace_id'] == prepared.trace_id
        assert prepared.trace_id != prepared.submit_id
        assert durable['upstream_context']['submission_phase'] == 'dispatching'
        assert durable['upstream_context']['account_id'] == context.account['id']
        assert durable['upstream_context']['expected_user_id'] == 'synthetic-jimeng-user'
        assert durable['actual_cost'] is None
        assert durable['estimated_cost'] == 24
        assert context.service._credit_reservations[identifier] == (context.account['id'], 24)
        assert durable['normalized']['_cost_evidence']['jimeng_quote']['field'] == 'getNeedCredits.credits'
        return {'run_id': prepared.submit_id, 'thread_id': ''}

    context.client.submit_prepared.side_effect = submit
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'succeeded' and result['thread_id'] == ''
    assert result['actual_cost'] is None  # A balance change is not a task charge.
    assert result['balance_before'] == 100 and result['balance_after'] == 93
    context.client.submit_prepared.assert_called_once_with(context.prepared)
    context.client.poll.assert_called_once_with(context.prepared.submit_id, '')
    context.service._run(identifier)
    context.client.submit_prepared.assert_called_once()


@pytest.mark.parametrize('failure', ['transport', 'mismatched_receipt'])
def test_lost_or_foreign_receipt_only_queries_the_original_id(context, failure):
    identifier = create(context)
    if failure == 'transport':
        context.client.submit_prepared.side_effect = BrowserError('synthetic lost receipt')
    else:
        context.client.submit_prepared.return_value = {'run_id': str(uuid.uuid4()), 'thread_id': 'foreign-history'}
    context.client.poll.return_value.update(record_found=True,
        upstream_task_id='12345678901234567890', history_id='98765432109876543210')
    with_download_receipt(context.client.poll.return_value,
        account_id=context.account['id'], submit_id=context.prepared.submit_id)
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'succeeded' and result['error_code'] == ''
    assert result['run_id'] == context.prepared.submit_id and result['thread_id'] == '98765432109876543210'
    assert result['upstream_context']['upstream_task_id'] == '12345678901234567890'
    assert result['upstream_context']['automatic_resubmission_blocked'] is True
    assert result['upstream_context']['submit_error']['outcome_unknown'] is True
    context.client.poll.assert_called_once_with(context.prepared.submit_id, '')
    context.client.submit_prepared.assert_called_once()


def test_complete_rejection_preserves_original_id_without_resubmitting(context):
    identifier = create(context)
    context.client.submit_prepared.side_effect = BrowserRejected('synthetic complete rejection')
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'JIMENG_UPSTREAM_REJECTED'
    assert result['run_id'] == context.prepared.submit_id
    assert result['actual_cost'] is None
    context.client.poll.assert_not_called()
    context.service._run(identifier)
    context.client.submit_prepared.assert_called_once()


def test_page_readiness_failure_preserves_login_and_blocks_scheduling_until_recovered(context, monkeypatch):
    identifier = create(context)
    context.store.update_account(context.account['id'], {'login_stage': 'ready', 'setup_checked_at': 12345,
        'balance_details': {'gift_credit': 100}})
    previous = context.store.account(context.account['id'], private=True)
    context.client.login_identity.side_effect = JimengPageNotReady('modules', 'factories-pending')
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))
    checked = context.service.check_account(context.account['id'])
    assert checked['status'] == 'network_error'
    checked = context.store.account(context.account['id'], private=True)
    for key in ('login_user_id', 'balance', 'balance_details_json', 'enabled', 'login_stage', 'setup_checked_at'):
        assert checked[key] == previous[key]
    assert context.service._reserve_account(0, task_id=identifier) is None
    context.client.account_state.assert_not_called()
    context.client.login_identity.assert_called_once()
    context.client.login_identity.side_effect = None
    context.client.browser.project_id.return_value = ''
    recovered = context.service.check_account(context.account['id'])
    assert recovered['status'] == 'active' and recovered['last_error'] == ''
    assert context.store.account(context.account['id'], private=True)['login_user_id'] == previous['login_user_id']
    assert context.service._funded_accounts(0, set(), {}, task=context.store.task(identifier))


def test_actual_page_identity_mismatch_remains_a_login_failure(context, monkeypatch):
    context.client.login_identity.return_value = {'authenticated': True, 'user_id': 'different-account'}
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))
    monkeypatch.setattr('app.service.time.sleep', Mock())
    checked = context.service.check_account(context.account['id'])
    assert checked['status'] == 'login_required'
    assert context.store.account(context.account['id'], private=True)['login_user_id'] == context.account['login_user_id']
    assert checked['balance'] == context.account['balance']
    context.client.account_state.assert_not_called()


def test_account_check_cannot_use_an_unmanaged_profile_during_online_login(context):
    context.store.update_account(context.account['id'], {'status': 'logging_in', 'managed_profile': False})
    with pytest.raises(AccountMaintenanceBusy):
        context.service.check_account(context.account['id'])
    assert context.store.account(context.account['id'])['status'] == 'logging_in'
    context.service.browser_runtime.acquire.assert_not_called()
    context.client.login_identity.assert_not_called()
    context.client.account_state.assert_not_called()


def test_late_page_readiness_failure_cannot_overwrite_online_login(context, monkeypatch):
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))

    def login_started():
        context.store.update_account(context.account['id'], {'status': 'logging_in', 'enabled': False})
        raise JimengPageNotReady('profile', 'profile-pending')

    context.client.login_identity.side_effect = login_started
    with pytest.raises(AccountMaintenanceBusy):
        context.service.check_account(context.account['id'])
    account = context.store.account(context.account['id'], private=True)
    assert account['status'] == 'logging_in' and not account['enabled']
    assert account['login_user_id'] == context.account['login_user_id']
    context.client.account_state.assert_not_called()


@pytest.mark.parametrize('result', ['ready', 'not_ready'])
def test_old_account_check_cannot_overwrite_a_completed_new_login(context, monkeypatch, result):
    monkeypatch.setattr('app.service.browser_alive', Mock(return_value=True))
    context.client.browser.project_id.return_value = ''

    def login_completed():
        context.store.update_account(context.account['id'], {'status': 'active', 'login_stage': 'ready',
            'setup_checked_at': 54321, 'login_user_id': 'newly-verified-account', 'balance': 456,
            'cdp_port': 19300 + context.account['id']})
        if result == 'not_ready':
            raise JimengPageNotReady('profile', 'profile-pending')
        return {'authenticated': True, 'user_id': context.account['login_user_id']}

    context.client.login_identity.side_effect = login_completed
    with pytest.raises(AccountMaintenanceBusy, match='binding changed'):
        context.service.check_account(context.account['id'])
    account = context.store.account(context.account['id'], private=True)
    assert account['login_user_id'] == 'newly-verified-account' and account['balance'] == 456
    assert account['status'] == 'active' and account['setup_checked_at'] == 54321


@pytest.mark.parametrize('method', ['login_identity', 'account_state', 'prepare_submit', 'submit_prepared'])
def test_proven_page_not_ready_is_unsubmitted_even_after_dispatch_intent_was_persisted(context, method):
    identifier = create(context)
    calls = getattr(context.client, method)

    def not_ready(*args, **kwargs):
        durable = context.store.task(identifier)
        if method == 'submit_prepared':
            assert durable['run_id'] == context.prepared.submit_id
            assert durable['upstream_context']['submission_phase'] == 'dispatching'
        raise JimengPageNotReady('profile', 'profile-pending')

    calls.side_effect = not_ready
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'JIMENG_PROTOCOL_UNAVAILABLE'
    assert result['run_id'] == '' and result['thread_id'] == '' and result['actual_cost'] is None
    assert result['upstream_context']['submission_phase'] == 'not_dispatched'
    assert result['upstream_context']['pre_dispatch_failure']['generation_request_sent'] is False
    public = context.service.public_task(result)
    assert public['provider_context'] == {'generation_request_sent': False,
        'financial_outcome': 'not_charged_this_invocation', 'automatic_resubmission': False}
    assert public['error']['message'] == context.service.public_task({**result,
        'upstream_context': {}})['error']['message']
    assert context.store.account(context.account['id'])['status'] == 'network_error'
    context.client.poll.assert_not_called()
    if method != 'submit_prepared':
        context.client.submit_prepared.assert_not_called()
    else:
        context.client.submit_prepared.assert_called_once()
    context.service._run(identifier)
    context.service._resume_poll(identifier)
    context.client.poll.assert_not_called()


def test_page_not_ready_proof_cannot_clear_later_acceptance_evidence(context):
    identifier = create(context)

    def accepted_then_pending(prepared):
        context.service._jimeng_update(identifier, {'thread_id': 'own-history'},
            {'submission_phase': 'accepted', 'upstream_task_id': 'own-provider-task'})
        with_download_receipt(context.client.poll.return_value,
            account_id=context.account['id'], submit_id=context.prepared.submit_id, history_id='own-history')
        raise JimengPageNotReady('modules', 'factories-pending')

    context.client.submit_prepared.side_effect = accepted_then_pending
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'succeeded'
    assert result['run_id'] == context.prepared.submit_id and result['thread_id'] == 'own-history'
    assert result['upstream_context']['submit_error']['outcome_unknown'] is True
    assert 'provider_context' not in context.service.public_task(result)
    context.client.poll.assert_called_once_with(context.prepared.submit_id, 'own-history')
    context.client.submit_prepared.assert_called_once()


def test_task_credit_evidence_is_the_only_actual_spend_source(context):
    identifier = create(context)
    context.client.poll.return_value['task_credit_evidence'] = {
        'amount': 8, 'field': 'item_list[].extra.credits_consume',
        'submit_id': context.prepared.submit_id, 'phase': 'succeeded'}
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'succeeded' and result['actual_cost'] == 8


@pytest.mark.parametrize('credit', [
    {'amount': 8, 'field': 'resource.creditsAmount', 'phase': 'succeeded'},
    {'amount': 8, 'field': 'item_list[].extra.credits_consume', 'phase': 'running'},
    {'amount': 8, 'field': 'item_list[].extra.credits_consume', 'phase': 'succeeded', 'submit_id': 'foreign'},
])
def test_unsettled_or_foreign_credit_fields_never_become_actual_cost(context, credit):
    identifier = create(context)
    credit.setdefault('submit_id', context.prepared.submit_id)
    context.client.poll.return_value['task_credit_evidence'] = credit
    context.service._run(identifier)
    assert context.store.task(identifier)['actual_cost'] is None


@pytest.mark.parametrize('image,error_type', [(False, JimengProtocolUnavailable), (False, ValueError),
                                             (True, JimengProtocolUnavailable)])
def test_missing_generation_contract_fails_before_startup_identity_or_upload(context, image, error_type):
    if image:
        with pytest.raises(ValueError, match='Jimeng'):
            create(context, image=True)
        context.service.browser_runtime.acquire.assert_not_called()
        context.client.login_identity.assert_not_called()
        context.client.upload_inputs.assert_not_called()
        context.client.submit_prepared.assert_not_called()
        return
    identifier = create(context, image=image)
    context.client.preflight_model.side_effect = error_type('synthetic contract unavailable')
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'failed' and result['error_code'] == 'JIMENG_PROTOCOL_UNAVAILABLE'
    assert result['run_id'] == '' and result['attempted_account_ids'] == []
    context.service.browser_runtime.acquire.assert_not_called()
    context.client.login_identity.assert_not_called()
    context.client.upload_inputs.assert_not_called()
    context.client.submit_prepared.assert_not_called()


def test_policy_revoked_during_upload_blocks_preparation_and_paid_dispatch(context):
    identifier = create(context)

    def upload(_):
        policies = deepcopy(context.policies)
        policies['cn']['models'] = {}
        context.store.update_settings({'region_generation_policy': policies})
        return {'images': [], 'videos': [], 'audios': []}

    context.client.upload_inputs.side_effect = upload
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['error_code'] == 'REGION_GENERATION_DENIED'
    assert result['run_id'] == '' and result['attempted_account_ids'] == []
    context.client.prepare_submit.assert_not_called()
    context.client.submit_prepared.assert_not_called()


def test_policy_changes_after_dispatch_never_block_original_task_queries(context):
    identifier = create(context)

    def submit(prepared):
        policies = deepcopy(context.policies)
        policies['cn']['models'] = {}
        context.store.update_settings({'region_generation_policy': policies})
        return {'run_id': prepared.submit_id, 'thread_id': ''}

    context.client.submit_prepared.side_effect = submit
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'succeeded', (result['error_code'], result['error_message'])
    context.client.poll.assert_called_once_with(context.prepared.submit_id, '')


def dispatched(context, identifier):
    return context.store.update_task(identifier, {'account_id': context.account['id'],
        'execution_backend': 'jimeng_web', 'status': 'submitted', 'run_id': context.prepared.submit_id,
        'thread_id': '', 'upstream_context': {
            **context.prepared.receipt_context(context.account['id'], phase='dispatching'),
            'task_id': identifier, 'expected_user_id': 'synthetic-jimeng-user',
            'observation_deadline': time.time() + 30}})


def test_restart_resumes_uuid_without_history_or_region_permission_and_never_prepares(context):
    identifier = create(context)
    dispatched(context, identifier)
    policies = deepcopy(context.policies)
    policies['cn']['models'] = {}
    context.store.update_settings({'region_generation_policy': policies})
    context.store.update_account(context.account['id'], {'enabled': False, 'status': 'disabled'})
    context.service._resume_poll(identifier)
    assert context.store.task(identifier)['status'] == 'succeeded'
    context.client.preflight_model.assert_not_called()
    context.client.upload_inputs.assert_not_called()
    context.client.prepare_submit.assert_not_called()
    context.client.submit_prepared.assert_not_called()
    context.client.poll.assert_called_once_with(context.prepared.submit_id, '')


def test_service_start_recovers_jimeng_dispatch_with_empty_history(context):
    identifier = create(context)
    dispatched(context, identifier)
    context.service._pool.submit.reset_mock()
    with patch.object(context.service.canvas_cli, 'verify_runtime', return_value={'available': False}), \
            patch('app.service.threading.Thread'):
        context.service.start()
    calls = context.service._pool.submit.call_args_list
    assert any(call.args == (context.service._resume_jimeng_poll, identifier) for call in calls)
    context.client.submit_prepared.assert_not_called()


@pytest.mark.parametrize('corrupt', ['owner', 'submit_id', 'identity'])
def test_query_recovery_never_switches_to_a_different_submission_or_signed_in_user(context, corrupt):
    identifier = create(context)
    original = dispatched(context, identifier)
    if corrupt == 'identity':
        context.client.login_identity.return_value = {'authenticated': True, 'user_id': 'foreign-user'}
    else:
        update = dict(original['upstream_context'])
        update['account_id' if corrupt == 'owner' else 'submit_id'] = 999 if corrupt == 'owner' else str(uuid.uuid4())
        context.store.update_task(identifier, {'upstream_context': update})
    context.service._resume_poll(identifier)
    result = context.store.task(identifier)
    assert result['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
    assert result['run_id'] == context.prepared.submit_id
    context.client.poll.assert_not_called()
    context.client.submit_prepared.assert_not_called()


def test_manual_termination_before_dispatch_wins_over_prepared_body(context):
    identifier = create(context)

    def prepare(*_):
        row = context.store.task(identifier)
        context.store.update_task(identifier, {'status': 'failed', 'error_code': 'MANUAL_TERMINATION',
            'upstream_context': {**row['upstream_context'], 'manual_termination': {'source': 'synthetic-test'}}})
        return context.prepared

    context.client.prepare_submit.side_effect = prepare
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['error_code'] == 'MANUAL_TERMINATION' and result['run_id'] == ''
    context.client.submit_prepared.assert_not_called()


def test_an_existing_durable_submission_cannot_be_overwritten_during_preparation(context):
    identifier = create(context)
    original_id = str(uuid.uuid4())

    def prepare(*_):
        row = dispatched(context, identifier)
        context.store.update_task(identifier, {'run_id': original_id,
            'upstream_context': {**row['upstream_context'], 'submit_id': original_id}})
        return context.prepared

    context.client.prepare_submit.side_effect = prepare
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['run_id'] == original_id and original_id != context.prepared.submit_id
    assert result['upstream_context']['submission_phase'] == 'dispatching'
    context.client.submit_prepared.assert_not_called()


def test_reentering_fresh_dispatch_never_revalidates_or_replaces_an_existing_uuid(context):
    identifier = create(context)
    dispatched(context, identifier)
    policies = deepcopy(context.policies)
    policies['cn']['models'] = {}
    context.store.update_settings({'region_generation_policy': policies})
    assert context.service._execute_jimeng(identifier, context.account, time.monotonic() + 10) is True
    context.client.preflight_model.assert_not_called()
    context.client.submit_prepared.assert_not_called()
    context.service.browser_runtime.acquire.assert_not_called()


def test_query_failures_are_bounded_by_original_deadline_and_never_enable_resubmission(context, monkeypatch):
    identifier = create(context)
    row = dispatched(context, identifier)
    clock = {'wall': time.time(), 'steady': time.monotonic()}
    monkeypatch.setattr('app.jimeng_service.time', SimpleNamespace(
        time=lambda: clock['wall'], monotonic=lambda: clock['steady']))

    def advance(seconds):
        clock['wall'] += seconds
        clock['steady'] += seconds
        return False

    monkeypatch.setattr(context.service._stop, 'wait', Mock(side_effect=advance))
    context.client.poll.side_effect = BrowserError('synthetic private response detail')
    context.service._resume_poll(identifier)
    result = context.store.task(identifier)
    assert result['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
    assert result['run_id'] == context.prepared.submit_id and result['actual_cost'] is None
    assert result['upstream_context']['observation_deadline'] == row['upstream_context']['observation_deadline']
    assert 1 <= context.client.poll.call_count <= 8
    context.client.submit_prepared.assert_not_called()
    with pytest.raises(ValueError, match='not safe to retry'):
        context.store.requeue_unsubmitted_task(identifier)
    previous = context.client.poll.call_count
    context.service._resume_poll(identifier)
    assert context.client.poll.call_count == previous


def test_any_interruption_after_dispatch_keeps_query_recovery_available(context, monkeypatch):
    identifier = create(context)
    monkeypatch.setattr(context.service, '_poll_jimeng', Mock(side_effect=RuntimeError('synthetic interruption')))
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['run_id'] == context.prepared.submit_id
    assert result['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
    assert result['upstream_context']['submission_phase'] == 'submitted'
    context.client.submit_prepared.assert_called_once()


def prepared_with_quote_context(context, change):
    value = context.prepared.receipt_context(context.account['id'])
    change(value)
    return SimpleNamespace(kind=context.prepared.kind, submit_id=context.prepared.submit_id,
        body=context.prepared.body, receipt_context=lambda *_, phase='prepared': {**value, 'submission_phase': phase})


@pytest.mark.parametrize('value', [None, True, -1, float('nan'), float('inf'), '0'])
def test_invalid_native_quote_never_defaults_to_free_or_dispatches(context, value):
    identifier = create(context)
    context.client.prepare_submit.return_value = prepared_with_quote_context(context,
        lambda receipt: receipt.update(quoted_credits=value))
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['error_code'] == 'JIMENG_PROTOCOL_UNAVAILABLE'
    assert result['run_id'] == '' and result['actual_cost'] is None
    context.client.submit_prepared.assert_not_called()
    assert context.client.account_state.call_count == 1


@pytest.mark.parametrize('change', [
    lambda receipt: receipt.pop('quoted_credits'),
    lambda receipt: receipt.update(quoted_credits=0, quote_source='commerce.amount'),
    lambda receipt: receipt.pop('quote_bound'),
    lambda receipt: receipt['quote_bound'].update(model_req_key='foreign-model'),
    lambda receipt: receipt['quote_bound'].update(resolution='1080p'),
    lambda receipt: receipt['quote_bound'].update(duration_seconds=8),
    lambda receipt: receipt['quote_bound'].update(material_types=['video']),
])
def test_missing_unofficial_or_foreign_request_quote_is_rejected_before_dispatch(context, change):
    identifier = create(context)
    context.client.prepare_submit.return_value = prepared_with_quote_context(context, change)
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['error_code'] == 'JIMENG_PROTOCOL_UNAVAILABLE'
    assert result['run_id'] == '' and result['attempted_account_ids'] == []
    context.client.submit_prepared.assert_not_called()


def test_a_verified_native_zero_quote_can_finish_with_zero_final_credit_balance(context):
    identifier = create(context)
    context.client.prepare_submit.return_value = replace(context.prepared, quoted_credits=0)
    context.client.account_state.side_effect = [{'available_balance': 100}, {'available_balance': 0},
                                               {'available_balance': 0}]
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['status'] == 'succeeded' and result['estimated_cost'] == 0
    assert result['upstream_context']['quoted_credits'] == 0
    assert result['upstream_context']['quote_source'] == 'native_get_need_credits'
    assert result['actual_cost'] is None
    context.client.submit_prepared.assert_called_once()


def test_a_dynamic_quote_replaces_the_own_estimate_and_excludes_the_own_reservation(context):
    identifier = create(context)
    context.store.update_task(identifier, {'estimated_cost': 999})

    def prepare(*_):
        assert context.service._credit_reservations[identifier] == (context.account['id'], 100)
        context.service._credit_reservations[identifier] = (context.account['id'], 999)
        return context.prepared

    def submit(prepared):
        assert context.service._credit_reservations[identifier] == (context.account['id'], 24)
        assert context.store.task(identifier)['estimated_cost'] == 24
        return {'run_id': prepared.submit_id, 'thread_id': ''}

    context.client.prepare_submit.side_effect = prepare
    context.client.submit_prepared.side_effect = submit
    context.service._run(identifier)
    assert context.store.task(identifier)['status'] == 'succeeded'
    context.client.submit_prepared.assert_called_once()


def test_current_quote_uses_credits_instead_of_commerce_amount_and_reads_balance_after_prepare(context):
    identifier = create(context)
    context.client.account_state.side_effect = [{'available_balance': 100}, {'available_balance': 20}]
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert context.prepared.body['extend']['m_video_commerce_info']['amount'] == 4
    assert result['estimated_cost'] == 24 and result['error_code'] == 'JIMENG_CREDITS_INSUFFICIENT'
    assert result['account_id'] == context.account['id'] and result['run_id'] == ''
    assert result['actual_cost'] is None and result['attempted_account_ids'] == []
    context.client.submit_prepared.assert_not_called()


@pytest.mark.parametrize('pending_source,amount', [('memory', 90), ('database', 90), ('memory', 0)])
def test_current_quote_cannot_spend_another_tasks_reservation(context, pending_source, amount):
    identifier = create(context)

    def prepare(*_):
        if pending_source == 'memory':
            context.service._credit_reservations['other-task'] = (context.account['id'], amount)
        else:
            row = context.store.task(identifier)
            context.store.create_task('other-task', row['request'], row['normalized'])
            context.store.update_task('other-task', {'account_id': context.account['id'],
                'status': 'running', 'estimated_cost': amount})
        return context.prepared

    context.client.prepare_submit.side_effect = prepare
    try:
        context.service._run(identifier)
        result = context.store.task(identifier)
        assert result['error_code'] == 'JIMENG_CREDITS_INSUFFICIENT' and result['estimated_cost'] == 24
        assert result['actual_cost'] is None and result['run_id'] == ''
        assert result['account_id'] == context.account['id']
        context.client.submit_prepared.assert_not_called()
        if pending_source == 'memory':
            assert context.service._credit_reservations['other-task'] == (context.account['id'], amount)
    finally:
        context.service._credit_reservations.pop('other-task', None)


def test_a_newer_lower_cached_balance_wins_over_the_final_read_race(context):
    identifier = create(context)
    reads = []

    def credits(**_):
        reads.append(True)
        if len(reads) == 2:
            context.store.update_account(context.account['id'], {'balance': 10})
        return {'available_balance': 100}

    context.client.account_state.side_effect = credits
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['error_code'] == 'JIMENG_CREDITS_INSUFFICIENT'
    assert context.store.account(context.account['id'])['balance'] == 10
    context.client.submit_prepared.assert_not_called()


def test_a_failed_final_credit_read_cannot_authorize_the_prepared_quote(context):
    identifier = create(context)
    context.client.account_state.side_effect = [{'available_balance': 100}, BrowserError('synthetic read failure')]
    context.service._run(identifier)
    result = context.store.task(identifier)
    assert result['error_code'] == 'ACCOUNT_VERIFICATION_FAILED'
    assert result['run_id'] == '' and result['actual_cost'] is None
    context.client.submit_prepared.assert_not_called()


def test_jimeng_quote_rejection_uses_the_approved_neutral_submission_message():
    from app.errors import public_error, BOUNDED_RETRY_FAILURE_MESSAGE
    result = public_error('JIMENG_CREDITS_INSUFFICIENT', 'native quote exceeds reserved budget')
    assert result == {'code': 'JIMENG_CREDITS_INSUFFICIENT', 'message': BOUNDED_RETRY_FAILURE_MESSAGE}


@pytest.mark.parametrize('quote', [0, 24])
def test_zero_balance_jimeng_account_reaches_native_quote_and_only_verified_free_work_dispatches(context, quote):
    context.store.update_account(context.account['id'], {'balance': 0})
    context.client.account_state.side_effect = [{'available_balance': 0}, {'available_balance': 0},
                                               {'available_balance': 0}]
    context.client.prepare_submit.return_value = replace(context.prepared, quoted_credits=quote)
    identifier = create(context)
    context.service._run(identifier)
    result = context.store.task(identifier)
    context.client.prepare_submit.assert_called_once()
    assert result['account_id'] == context.account['id'] and result['actual_cost'] is None
    if quote == 0:
        assert result['status'] == 'succeeded' and result['estimated_cost'] == 0
        assert result['upstream_context']['quote_source'] == 'native_get_need_credits'
        context.client.submit_prepared.assert_called_once()
    else:
        assert result['error_code'] == 'JIMENG_CREDITS_INSUFFICIENT'
        assert result['run_id'] == '' and result['attempted_account_ids'] == []
        context.client.submit_prepared.assert_not_called()


def test_zero_balance_international_account_is_still_excluded_even_with_mainland_region(context):
    context.store.update_account(context.account['id'], {'execution_mode': 'browser',
        'region': 'cn', 'proxy_url': 'socks5://proxy.example.test:20001', 'balance': 0})
    identifier = create(context)
    assert context.service._reserve_account(task_id=identifier) is None
    context.service.browser_runtime.acquire.assert_not_called()
    context.client.prepare_submit.assert_not_called()


@pytest.mark.parametrize('case', ['negative', 'nan', 'infinite', 'missing', 'manual_disabled',
                                 'admin_threshold', 'unknown_hold', 'negative_available'])
def test_zero_balance_admission_never_relaxes_invalid_balance_or_existing_account_controls(context, case):
    values = {'balance': {'negative': -1, 'nan': float('nan'), 'infinite': float('inf'),
                          'missing': None}.get(case, 0)}
    if case == 'manual_disabled':
        values.update(enabled=False, status='disabled')
    context.store.update_account(context.account['id'], values)
    if case == 'admin_threshold':
        context.store.update_settings({'low_balance_disable_threshold': 1})
    if case in {'unknown_hold', 'negative_available'}:
        context.service._credit_reservations['other-task'] = (context.account['id'],
            0 if case == 'unknown_hold' else 1)
    try:
        identifier = create(context)
        assert context.service._reserve_account(task_id=identifier) is None
        context.service.browser_runtime.acquire.assert_not_called()
        context.client.login_identity.assert_not_called()
        context.client.prepare_submit.assert_not_called()
        context.client.submit_prepared.assert_not_called()
        if case == 'manual_disabled':
            assert context.store.account(context.account['id'])['enabled'] is False
    finally:
        context.service._credit_reservations.pop('other-task', None)


@pytest.mark.parametrize('code', ['REGION_GENERATION_DENIED', 'JIMENG_PROTOCOL_UNAVAILABLE',
                                 'JIMENG_CREDITS_INSUFFICIENT', 'JIMENG_UPSTREAM_FAILED',
                                 'JIMENG_UPSTREAM_REJECTED'])
def test_typed_jimeng_rejections_preserve_routing_codes_and_only_use_approved_caller_messages(code):
    from app.errors import (public_error, BOUNDED_RETRY_FAILURE_MESSAGE, PARAMETER_VALIDATION_MESSAGE,
                            RETRYABLE_GENERATION_MESSAGE)
    raw = 'raw admin diagnostic: video may contain third-party content; credits returned; region cn'
    expected = (PARAMETER_VALIDATION_MESSAGE if code == 'JIMENG_UPSTREAM_REJECTED' else
                RETRYABLE_GENERATION_MESSAGE if code in {'REGION_GENERATION_DENIED', 'JIMENG_PROTOCOL_UNAVAILABLE'}
                else BOUNDED_RETRY_FAILURE_MESSAGE)
    assert public_error(code, raw) == {'code': code, 'message': expected}


@pytest.mark.parametrize('code', ['JIMENG_QUERY_FAILED', 'JIMENG_RESULT_MISSING'])
def test_a_paid_task_with_unconfirmed_query_or_result_never_claims_no_generation_or_refund(code):
    from app.errors import public_error
    original = public_error('UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', '')
    result = public_error(code, 'video may contain third-party content; private raw diagnostic')
    assert result == {'code': code, 'message': original['message']}


@pytest.mark.parametrize('state,upstream_code,expected', [(10, '', 'CONTENT_MODERATION_FAILED'),
    (40, '', 'CONTENT_MODERATION_FAILED'), (30, '', 'JIMENG_UPSTREAM_FAILED'),
    (30, '2059', 'CONTENT_MODERATION_FAILED')])
def test_terminal_native_moderation_status_is_distinguished_from_a_generic_generation_failure(context, state, upstream_code, expected):
    from app.errors import CONTENT_MODERATION_MESSAGE, BOUNDED_RETRY_FAILURE_MESSAGE
    identifier = create(context)
    dispatched(context, identifier)
    diagnostic = 'video may contain third-party content; preserve admin diagnostic only'
    context.client.poll.return_value = {'status': 'failed', 'state': state, 'result_urls': [],
        'error_code': upstream_code, 'error': diagnostic}
    context.service._resume_poll(identifier)
    row = context.store.task(identifier)
    assert row['error_code'] == expected and row['error_message'] == diagnostic
    assert row['actual_cost'] is None
    expected_message = CONTENT_MODERATION_MESSAGE if expected == 'CONTENT_MODERATION_FAILED' else BOUNDED_RETRY_FAILURE_MESSAGE
    assert context.service.public_task(row)['error']['message'] == expected_message


@pytest.mark.parametrize('category', ['transport', 'deadline'])
def test_media_download_failures_retain_sanitized_admin_diagnostics_without_dispatch(context, category):
    identifier = create(context)
    diagnostic = 'sanitized reference transport diagnostic: ' + category
    context.client.upload_inputs.side_effect = MediaError(diagnostic, category=category)
    context.service._run(identifier)
    row = context.store.task(identifier)
    assert row['error_code'] == 'MEDIA_DOWNLOAD_FAILED' and row['error_message'] == diagnostic
    assert row['run_id'] == '' and row['attempted_account_ids'] == [] and row['actual_cost'] is None
    assert context.service.public_task(row)['error']['message'] == '素材下载失败，请检查素材链接后重试~'
    context.client.prepare_submit.assert_not_called()
    context.client.submit_prepared.assert_not_called()


@pytest.mark.parametrize('category,code,message', [
    ('content_type', 'MEDIA_FORMAT_UNSUPPORTED', '素材格式不支持，请修改后再试~'),
    ('too_large', 'MEDIA_LIMIT_EXCEEDED', '素材超限，请修改后再试~'),
])
def test_media_format_and_size_categories_preserve_raw_diagnostic_and_approved_caller_message(context, category, code, message):
    identifier = create(context)
    diagnostic = 'sanitized detailed media diagnostic'
    context.client.upload_inputs.side_effect = MediaError(diagnostic, category=category)
    context.service._run(identifier)
    row = context.store.task(identifier)
    assert row['error_code'] == code and row['error_message'] == diagnostic
    assert row['run_id'] == '' and row['actual_cost'] is None
    assert context.service.public_task(row)['error'] == {'code': code, 'message': message}
    context.client.prepare_submit.assert_not_called()
    context.client.submit_prepared.assert_not_called()


def test_upload_error_retains_the_specific_sanitized_reason_and_uses_approved_jimeng_failure_message(context):
    from app.errors import RETRYABLE_GENERATION_MESSAGE
    identifier = create(context)
    diagnostic = 'Jimeng upload blob host is unsupported'
    context.client.upload_inputs.side_effect = UploadError(diagnostic)
    context.service._run(identifier)
    row = context.store.task(identifier)
    assert row['error_code'] == 'MEDIA_UPLOAD_FAILED' and row['error_message'] == diagnostic
    assert row['run_id'] == '' and row['attempted_account_ids'] == [] and row['actual_cost'] is None
    assert context.service.public_task(row)['error'] == {'code': 'MEDIA_UPLOAD_FAILED',
        'message': RETRYABLE_GENERATION_MESSAGE}
    context.client.prepare_submit.assert_not_called()
    context.client.submit_prepared.assert_not_called()


def test_a_media_exception_after_dispatch_keeps_the_original_unknown_submission_category(context, monkeypatch):
    identifier = create(context)
    diagnostic = 'sanitized post-dispatch media interruption'
    monkeypatch.setattr(context.service, '_poll_jimeng', Mock(
        side_effect=MediaError(diagnostic, category='content_type')))
    context.service._run(identifier)
    row = context.store.task(identifier)
    assert row['run_id'] == context.prepared.submit_id
    assert row['error_code'] == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN' and row['error_message'] == diagnostic
    assert row['actual_cost'] is None
    context.client.submit_prepared.assert_called_once()


@pytest.mark.parametrize('first_record_found', [True, False])
def test_only_verified_history_receipts_bind_actual_ids_and_later_queries_reuse_bound_history(context, monkeypatch, first_record_found):
    identifier = create(context)
    context.client.submit_prepared.side_effect = BrowserError('synthetic lost receipt')
    history, upstream = '99887766554433221100', '11223344556677889900'
    first = {'status': 'running', 'record_found': first_record_found,
        'history_id': history if first_record_found else 'unverified-history',
        'upstream_task_id': upstream if first_record_found else 'unverified-task',
        'result_urls': [], 'error': '', 'error_code': ''}
    done = {'status': 'succeeded', 'record_found': True, 'history_id': history,
        'upstream_task_id': upstream, 'result_urls': ['https://v3-dreamnia.jimeng.com/original.mp4'],
        'error': '', 'error_code': ''}
    with_download_receipt(done, account_id=context.account['id'], submit_id=context.prepared.submit_id)
    context.client.poll.side_effect = [first, done]
    monkeypatch.setattr(context.service._stop, 'wait', Mock(return_value=False))
    context.service._run(identifier)
    row = context.store.task(identifier)
    assert row['status'] == 'succeeded' and row['thread_id'] == history
    assert row['upstream_context']['upstream_task_id'] == upstream
    assert [call.args for call in context.client.poll.call_args_list] == [
        (context.prepared.submit_id, ''),
        (context.prepared.submit_id, history if first_record_found else '')]
    context.client.submit_prepared.assert_called_once()
