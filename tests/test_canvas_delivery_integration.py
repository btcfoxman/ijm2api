"""Real Store/Service/CanvasClient delivery, with only native I/O and S3 faked."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlsplit

import pytest

from app.canvas_cli import CanvasCLIError
from app.catalog import normalize_request
from app.result_delivery import result_delivery_projection
from app.service import Service
from app.store import Store
from test_result_transfer_resume import Center, SETTINGS


TASK_ID = 'ijm_delivery_integration'
RESOURCE = '11111111-1111-4111-8111-111111111111'
VIDEO = b'\x00\x00\x00\x20ftypisom' + b'complete-original-video'
BINDINGS = ('backend', 'account_id', 'task_id', 'profile', 'project_id', 'node_id',
            'submit_id', 'expected_user_id', 'kind', 'resource_ids', 'output_count')


class NativeRunner:
    """The allowed native boundary has no prepare/confirm/run or real processes."""
    timeout = 120

    def __init__(self, root, account):
        self.root, self.account = root, account
        self.calls = []
        self.query_error = None

    def account_directory(self, account):
        assert account['id'] == self.account['id']
        return self.root / f'account-{account["id"]}'

    def account_guard(self, account, **kwargs):
        assert account['id'] == self.account['id']
        return nullcontext()

    def profile(self, account):
        assert account['id'] == self.account['id']
        return 'original-profile'

    def close(self):
        pass

    def run(self, account, args, **kwargs):
        assert account['id'] == self.account['id']
        pair = tuple(args[:2])
        assert pair in {('auth', 'account'), ('operation', 'status'),
                        ('resource', 'get'), ('resource', 'download')}, 'No paid commands'
        self.calls.append((list(args), dict(kwargs)))
        if pair == ('auth', 'account'):
            data = {'userId': self.account['login_user_id'], 'credits': {'available': 90}}
        else:
            assert args[args.index('--project-id') + 1] == 'original-project'
            if pair == ('operation', 'status'):
                assert args[2] == 'original-submit'
                if self.query_error:
                    raise self.query_error
                data = {'operationRef': args[2], 'state': 'succeeded',
                        'resources': [{'resourceId': RESOURCE, 'state': 'succeeded'}]}
            elif pair == ('resource', 'get'):
                assert args[2] == RESOURCE
                data = {'resourceId': RESOURCE, 'type': 'video', 'creditsAmount': 76}
            else:
                assert args[2] == RESOURCE
                output = Path(args[args.index('--output') + 1])
                output.write_bytes(VIDEO)
                data = {'resourceId': RESOURCE, 'path': str(output), 'size': len(VIDEO),
                        'sha256': hashlib.sha256(VIDEO).hexdigest()}
        return {'ok': True, 'data': data}

    def count(self, pair):
        return sum(tuple(args[:2]) == pair for args, _ in self.calls)


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Hermetic test forbids real network and subprocesses')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(subprocess, 'Popen', deny)
    monkeypatch.setattr('app.result_transfer.time.sleep', lambda _: None)
    monkeypatch.setattr('test_result_transfer_resume.PAYLOAD', VIDEO)
    center = Center()
    monkeypatch.setattr('app.result_transfer.requests.Session', lambda: center)
    database = tmp_path / 'tasks.db'
    store = Store(str(database))
    store.update_settings({**SETTINGS, 'canvas_cli_enabled': True})
    account = store.upsert_account({'name': 'synthetic-owner', 'execution_mode': 'canvas_cli'})
    account = store.update_account(account['id'], {'status': 'active', 'enabled': True,
        'balance': 100, 'login_user_id': 'original-user', 'cli_user_id': 'original-user',
        'watermark_ready': True, 'agreement_ready': True, 'cli_auth_status': 'ready'})
    request = {'model': 'sd-2-0-mini', 'prompt': 'Synthetic landscape', 'duration': 4}
    store.create_task(TASK_ID, request, normalize_request(request), execution_backend='canvas_cli')
    bound = {'backend': 'canvas_cli', 'account_id': account['id'], 'task_id': TASK_ID,
        'profile': 'original-profile', 'project_id': 'original-project', 'node_id': 'original-node',
        'submit_id': 'original-submit', 'expected_user_id': 'original-user', 'kind': 'video',
        'resource_ids': [RESOURCE], 'output_count': 1, 'phase': 'accepted',
        'submission_phase': 'accepted', 'observation_deadline': time.time() + 3600,
        'submission_evidence': {'version': 1, 'accepted': True, 'output_resources': True,
            'receipt': False, 'identity_conflict': False, 'ambiguous_partial': False},
        'preparation_timing': {'commands': {'node run': {'count': 1, 'failed': 0}}}}
    before = store.update_task(TASK_ID, {'account_id': account['id'], 'status': 'running',
        'progress': 45, 'estimated_cost': 20, 'actual_cost': None, 'balance_before': 100,
        'balance_after': None, 'upstream_context': bound})
    services, runners, http_boundaries = [], [], []

    def restart():
        if services:
            services[-1].stop()
        fresh = Store(str(database))
        service = Service(fresh)
        runner = NativeRunner(tmp_path, fresh.account(account['id'], private=True))
        service.canvas_cli = runner
        services.append(service); runners.append(runner)
        return service, runner

    original_post = center.post
    def observed_post(url, **kwargs):
        # Every control HTTP effect is reached only after the real receipt CAS.
        row = Store(str(database)).task(TASK_ID)
        receipt = row['upstream_context']['download_receipts'][RESOURCE]
        location = tmp_path / f'account-{account["id"]}' / 'results' / TASK_ID / receipt['filename']
        assert location.read_bytes() == VIDEO
        assert receipt['sha256'] == hashlib.sha256(VIDEO).hexdigest()
        checkpoint = row['upstream_context']['upload_checkpoints'][RESOURCE]
        http_boundaries.append((urlsplit(url).path, checkpoint['state'], checkpoint['upload_id']))
        return original_post(url, **kwargs)
    center.post = observed_post
    service, runner = restart()
    try:
        yield {'service': service, 'runner': runner, 'center': center, 'restart': restart,
            'before': before, 'runners': runners, 'boundaries': http_boundaries}
    finally:
        for service in services:
            service.stop()


def observe(service):
    """Advance only the synthetic fixture's due time, then use the real claim path."""
    row = service.store.task(TASK_ID)
    queued = service.store.queue_canvas_result_observation(TASK_ID, row['updated_at'])
    context = deepcopy(queued['upstream_context'])
    context['result_observation']['next_poll_at'] = 0
    queued = service.store.update_task(TASK_ID, {'upstream_context': context})
    claimed = service.store.queue_canvas_result_observation(TASK_ID, queued['updated_at'], claim=True)
    assert claimed
    account = service.store.account(claimed['account_id'], private=True)
    service._run_canvas_result_observer(claimed, account)
    return service.store.task(TASK_ID)


def assert_original_binding(case, row):
    before = case['before']
    # Store projects normal cost evidence into normalized; it is audit, not input.
    assert row['request'] == before['request']
    assert {key: value for key, value in row['normalized'].items() if key != '_cost_evidence'} == {
        key: value for key, value in before['normalized'].items() if key != '_cost_evidence'}
    assert row['account_id'] == before['account_id']
    assert {key: row['upstream_context'][key] for key in BINDINGS} == {
        key: before['upstream_context'][key] for key in BINDINGS}
    assert row['upstream_context']['preparation_timing']['commands']['node run'] == {'count': 1, 'failed': 0}
    assert all(tuple(args[:2]) != ('node', 'run') for runner in case['runners'] for args, _ in runner.calls)


@pytest.mark.parametrize('boundary', ['url_issued', 'uploaded', 'committed'])
def test_new_service_resumes_original_download_and_s3_checkpoint_without_repeating_effects(delivery, boundary):
    service, center = delivery['service'], delivery['center']
    if boundary == 'url_issued':
        center.put_http = [403]
    elif boundary == 'uploaded':
        center.complete_http = 403
    else:
        center.presign_failures = 3
    failed = observe(service)
    assert failed['status'] == 'running' and not failed['result_urls']
    old = failed['upstream_context']['upload_checkpoints'][RESOURCE]
    assert old['state'] == boundary
    assert delivery['runner'].count(('resource', 'download')) == 1
    counts = {name: center.count(name) for name in ('create', 'put', 'complete')}
    center.complete_http = None
    restarted, native = delivery['restart']()
    result = observe(restarted)
    assert result['status'] == 'succeeded', result['upstream_context'].get('last_result_query_error')
    assert native.count(('resource', 'download')) == 0
    checkpoint = result['upstream_context']['upload_checkpoints'][RESOURCE]
    assert checkpoint['upload_id'] == old['upload_id'] and checkpoint['state'] == 'committed'
    assert checkpoint['idempotency_key'] == old['idempotency_key']
    assert center.count('get') == 1
    if boundary in {'uploaded', 'committed'}:
        assert center.count('create') == counts['create'] and center.count('put') == counts['put']
    if boundary == 'committed':
        assert center.count('complete') == counts['complete']
    assert_original_binding(delivery, result)
    assert result['result_transfer']['items'][0]['upload_id'] == old['upload_id']
    assert result['result_transfer']['source_files'][0]['sha256'] == hashlib.sha256(VIDEO).hexdigest()
    assert delivery['boundaries'][0][1] == 'creating'


def test_capacity_failure_history_and_safe_retry_projection_survive_successful_restart(delivery):
    service, center = delivery['service'], delivery['center']
    center.create_http = 507
    failed = observe(service)
    stage = failed['upstream_context']['result_delivery']
    assert result_delivery_projection(failed)['result_stage'] == 'transferring_retry'
    assert stage['last_error']['code'] == 'S3_INSUFFICIENT_CAPACITY'
    assert stage['last_error']['stage'] == 'create' and stage['last_error']['http_status'] == 507
    assert stage['errors'] == [stage['last_error']] and len(stage['errors']) <= 16
    assert set(stage['last_error']) == {'code', 'stage', 'http_status', 'retryable', 'at'}
    original_error = deepcopy(stage['last_error'])
    center.create_http = None
    restarted, native = delivery['restart']()
    result = observe(restarted)
    assert result_delivery_projection(result)['result_stage'] == 'completed'
    final = result['upstream_context']['result_delivery']
    assert final['phase'] == 'completed' and final['retrying'] is False and 'last_error' not in final
    assert final['errors'] == [original_error]
    assert native.count(('resource', 'download')) == 0
    assert result['actual_cost'] == 10 and result['balance_after'] == 90
    assert result['estimated_cost'] == 20 and result['balance_before'] == 100
    assert result['actual_cost'] != 76  # Resource precharge is not settled consumption.
    assert_original_binding(delivery, result)


@pytest.mark.parametrize('durable_state', ['uploaded', 'committed'])
def test_stale_urlissued_writer_cannot_regress_durable_state_across_new_service(delivery, durable_state):
    service, center = delivery['service'], delivery['center']
    if durable_state == 'uploaded':
        center.complete_http = 403
    else:
        center.presign_failures = 3
    pending = observe(service)
    checkpoint = deepcopy(pending['upstream_context']['upload_checkpoints'][RESOURCE])
    assert checkpoint['state'] == durable_state
    stale = {**checkpoint, 'state': 'url_issued'}
    restarted, native = delivery['restart']()
    before = restarted.store.task(TASK_ID)
    calls = deepcopy(center.calls)
    assert restarted._canvas_delivery_progress(TASK_ID, resource_id=RESOURCE, upload=stale) is False
    assert restarted.store.task(TASK_ID) == before
    assert center.calls == calls and native.calls == []
    center.complete_http = None
    result = observe(restarted)
    assert result['status'] == 'succeeded'
    assert result['upstream_context']['upload_checkpoints'][RESOURCE]['upload_id'] == checkpoint['upload_id']
    assert native.count(('resource', 'download')) == 0
    assert center.count('put') == 1 and center.count('create') == 1
    assert_original_binding(delivery, result)


def test_expired_session_rotates_durably_before_create_and_rejects_old_ordinal_writer(delivery):
    service, center = delivery['service'], delivery['center']
    center.put_http = [403]
    pending = observe(service)
    old = deepcopy(pending['upstream_context']['upload_checkpoints'][RESOURCE])
    assert old['state'] == 'url_issued'
    center.sessions[old['upload_id']]['status'] = 'expired'
    restarted, native = delivery['restart']()
    rejected = []
    def before_create(idempotency_key):
        before = restarted.store.task(TASK_ID)
        rotated = before['upstream_context']['upload_checkpoints'][RESOURCE]
        assert rotated['state'] == 'creating' and rotated['upload_id'] == ''
        assert rotated['idempotency_key'] == idempotency_key == old['idempotency_key'] + ':resume:1'
        assert restarted._canvas_delivery_progress(TASK_ID, resource_id=RESOURCE, upload=old) is False
        assert restarted.store.task(TASK_ID) == before
        rejected.append(True)
    center.before_create = before_create
    result = observe(restarted)
    assert rejected == [True] and result['status'] == 'succeeded'
    final = result['upstream_context']['upload_checkpoints'][RESOURCE]
    assert final['state'] == 'committed' and final['upload_id'] != old['upload_id']
    assert final['idempotency_key'] == old['idempotency_key'] + ':resume:1'
    assert center.sessions[old['upload_id']]['status'] == 'expired'
    assert center.count('create') == center.count('put') == 2 and center.count('get') == 1
    assert native.count(('resource', 'download')) == 0
    assert_original_binding(delivery, result)


@pytest.mark.parametrize('boundary', ['download_receipt', 'url_issued', 'uploaded'])
def test_manual_stop_during_real_persistence_callback_blocks_all_successor_effects(delivery, monkeypatch, boundary):
    service, center = delivery['service'], delivery['center']
    original = service._canvas_delivery_progress
    stopped = []
    def guarded(task_id, **kwargs):
        upload = kwargs.get('upload') or {}
        if ((boundary == 'download_receipt' and kwargs.get('download') is not None)
                or boundary == upload.get('state')):
            row = service.store.task(task_id)
            service.store.update_task(task_id, {'status': 'failed', 'progress': 100,
                'error_code': 'OPERATOR_STOP', 'error_message': 'Synthetic operator stop',
                'upstream_context': {**row['upstream_context'], 'manual_termination': {'source': 'test'}}})
            stopped.append(True)
        return original(task_id, **kwargs)
    monkeypatch.setattr(service, '_canvas_delivery_progress', guarded)
    result = observe(service)
    assert stopped == [True] and result['status'] == 'failed' and result['error_code'] == 'OPERATOR_STOP'
    assert result['result_urls'] == [] and not result['result_transfer']
    assert center.count('presign') == center.count('complete') == 0
    assert center.count('create') == (0 if boundary == 'download_receipt' else 1)
    assert center.count('put') == (1 if boundary == 'uploaded' else 0)
    if boundary == 'download_receipt':
        assert 'download_receipts' not in result['upstream_context']
    assert result['actual_cost'] is None and result['balance_after'] is None
    assert_original_binding(delivery, result)


@pytest.mark.parametrize('path', ['main', 'observer'])
def test_each_query_failure_finishes_its_own_timing_instead_of_reusing_old_attempt(delivery, path):
    service, runner = delivery['service'], delivery['runner']
    runner.query_error = CanvasCLIError('CLI_QUERY_OFFLINE', 'Synthetic query outage')
    row = service.store.task(TASK_ID)
    service.store.update_task(TASK_ID, {'upstream_context': {**row['upstream_context'],
        'last_result_query_timing': {'finished': False, 'command_count': 999, 'observation_attempt': -1}}})
    client = service.canvas_client(runner.account)
    seen = []
    for attempt in (1, 2):
        if path == 'observer':
            current = observe(service)
        else:
            service._poll_canvas(TASK_ID, client, time.monotonic() + 30, one_shot=True)
            current = service.store.task(TASK_ID)
        timing = current['upstream_context']['last_result_query_timing']
        assert timing['finished'] is True and timing['current_command'] is None
        assert timing['command_count'] == (2 if path == 'observer' else 1)
        assert timing['commands']['operation status']['count'] == 1
        assert timing['commands']['operation status']['failed'] == 1
        assert timing['observation_attempt'] == (attempt if path == 'observer' else (None if attempt == 1 else 0))
        seen.append(deepcopy(timing))
    assert seen[1]['started_at'] >= seen[0]['started_at']
    assert runner.count(('resource', 'download')) == 0 and not delivery['center'].calls
    assert_original_binding(delivery, service.store.task(TASK_ID))
