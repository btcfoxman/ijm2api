"""Durable original-byte S3 recovery, using the real create/session contract."""
from copy import deepcopy
import hashlib
import json
import socket
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

import pytest
import requests

from app.result_transfer import ResultTransferError, store_original


SETTINGS = {'result_s3_center_api_base': 'https://center.example.test',
            'result_s3_center_token': 'private-test-control-token',
            'result_s3_bucket': 'assets', 'result_s3_key_prefix': 'test-results'}
PAYLOAD = b'original-video-bytes-never-transcoded'


class Response:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return deepcopy(self.body)


class Center:
    """Committed create conflicts; GET and complete expose the original session."""
    def __init__(self):
        self.trust_env = True
        self.sessions, self.idempotency, self.calls, self.streams = {}, {}, [], []
        self.complete_response_losses = 0
        self.presign_failures = 0
        self.create_http = None
        self.complete_http = None
        self.get_http = None
        self.get_state = None
        self.put_http = []
        self.before_create = None
        self.grant_number = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, **kwargs):
        assert self.trust_env is False and kwargs['timeout'] == 60
        assert kwargs['allow_redirects'] is False
        assert kwargs['headers']['Authorization'] == 'Bearer ' + SETTINGS['result_s3_center_token']
        path = urlsplit(url).path
        if path == '/v1/uploads':
            body, idem = kwargs['json'], kwargs['headers']['Idempotency-Key']
            self.calls.append(('create', idem))
            if self.before_create:
                self.before_create(idem)
            if self.create_http:
                return Response(self.create_http, {'error': {'message': 'private-token https://secret.test/?signature=hidden'}})
            uid = self.idempotency.get(idem)
            if uid is None:
                uid = 'du_' + f'{len(self.sessions) + 1:032x}'
                self.sessions[uid] = {'id': uid, 'status': 'url_issued',
                    'logical_key': body['bucket'] + '/' + body['key'], 'declared_bytes': body['size']}
                self.idempotency[idem] = uid
            session = self.sessions[uid]
            if session['status'] not in {'reserved', 'url_issued'}:
                return Response(409, {'error': {'code': 'session_conflict'}})
            self.grant_number += 1
            # The actual create response omits logical_key and reissues PUT URLs.
            return Response(201, {'id': uid, 'status': 'url_issued', 'declared_bytes': session['declared_bytes'],
                'upload': {'method': 'PUT',
                    'url': f'https://storage.example.test/{uid}?signature=private-put-{self.grant_number}',
                    'headers': {'Content-Type': ['video/mp4'], 'X-Upload-Id': [uid]}}})
        if path.endswith('/complete'):
            uid = path.split('/')[-2]
            self.calls.append(('complete', uid))
            if self.complete_http:
                return Response(self.complete_http)
            self.sessions[uid]['status'] = 'committed'
            if self.complete_response_losses:
                self.complete_response_losses -= 1
                raise requests.ConnectionError('https://secret.test/?token=private-test-control-token')
            return Response(200, self.sessions[uid])
        assert path == '/v1/objects/presign'
        self.calls.append(('presign', None))
        if self.presign_failures:
            self.presign_failures -= 1
            return Response(503, {'error': {'message': 'private server prose'}})
        body = kwargs['json']
        return Response(200, {'bucket': body['bucket'], 'key': body['key'],
            'size_bytes': len(PAYLOAD), 'request': {'method': 'GET',
                'url': 'https://delivery.example.test/video.mp4?signature=private-get',
                'expires_at': '2099-01-01T00:00:00Z'}})

    def get(self, url, **kwargs):
        assert self.trust_env is False and kwargs['timeout'] == 60
        assert kwargs['allow_redirects'] is False
        uid = urlsplit(url).path.split('/')[-1]
        self.calls.append(('get', uid))
        if self.get_http:
            return Response(self.get_http, {'error': {'message': 'private server prose'}})
        value = deepcopy(self.sessions[uid])
        if self.get_state is not None:
            value['status'] = self.get_state
        return Response(200, value)

    def put(self, url, **kwargs):
        assert self.trust_env is False and kwargs['timeout'] == (30, 300)
        assert kwargs['allow_redirects'] is False
        assert 'Authorization' not in kwargs['headers']
        self.streams.append(kwargs['data'])
        data = kwargs['data'].read()
        assert data == PAYLOAD
        uid = urlsplit(url).path.split('/')[-1]
        self.calls.append(('put', uid))
        return Response(self.put_http.pop(0) if self.put_http else 200)

    def count(self, name):
        return sum(c[0] == name for c in self.calls)


class Journal:
    def __init__(self, value=None):
        self.value = deepcopy(value)
        self.snapshots = []

    def save(self, value):
        self.value = deepcopy(value)
        self.snapshots.append(deepcopy(value))


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    attempts = []
    def reject(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('Real network is forbidden in this fixture')
    monkeypatch.setattr(socket, 'create_connection', reject)
    monkeypatch.setattr(socket.socket, 'connect', reject)
    yield
    assert not attempts


@pytest.fixture
def transfer_case(tmp_path):
    source = tmp_path / 'video.mp4'
    source.write_bytes(PAYLOAD)
    center, journal = Center(), Journal()
    with patch('app.result_transfer.requests.Session', return_value=center), \
            patch('app.result_transfer.time.sleep'), \
            patch('app.result_transfer.subprocess.run', side_effect=AssertionError('No media tools or CLI')):
        yield source, center, journal


def publish(case, **kwargs):
    source, _, journal = case
    return store_original(source, 'ijm_original', SETTINGS, checkpoint=journal.value,
                          persist_checkpoint=journal.save, **kwargs)


def test_lost_complete_responses_resume_committed_without_create_or_put(transfer_case, tmp_path):
    source, center, journal = transfer_case
    center.complete_response_losses = 3
    with pytest.raises(ResultTransferError) as caught:
        publish(transfer_case)
    assert (caught.value.stage, caught.value.code, caught.value.retryable) == ('s3_complete', 'S3_TRANSPORT_ERROR', True)
    assert journal.value['state'] == 'uploaded' and center.count('put') == 1
    assert center.sessions[journal.value['upload_id']]['status'] == 'committed'
    restored = Journal(json.loads(json.dumps(journal.value)))
    restarted_file = tmp_path / 'new-process' / 'video.mp4'
    restarted_file.parent.mkdir(); restarted_file.write_bytes(PAYLOAD)
    url, receipt = store_original(restarted_file, 'ijm_original', SETTINGS,
        checkpoint=restored.value, persist_checkpoint=restored.save)
    assert center.count('create') == center.count('put') == 1
    assert center.count('get') == 1 and center.count('complete') == 3
    assert restored.value['state'] == 'committed'
    assert receipt['upload_id'] == journal.value['upload_id']
    assert receipt['sha256'] == hashlib.sha256(PAYLOAD).hexdigest()
    assert receipt['original_bytes'] is True and source.read_bytes() == PAYLOAD
    assert url.startswith('https://delivery.example.test/')


def test_presign_failure_keeps_committed_checkpoint_for_next_instance(transfer_case):
    _, center, journal = transfer_case
    center.presign_failures = 3
    with pytest.raises(ResultTransferError) as caught:
        publish(transfer_case)
    assert caught.value.stage == 's3_presign' and caught.value.http_status == 503
    assert journal.value['state'] == 'committed'
    publish(transfer_case)
    assert center.count('create') == center.count('put') == center.count('complete') == 1
    assert center.count('get') == 1


def test_uploaded_checkpoint_completes_original_without_reupload(transfer_case):
    _, center, journal = transfer_case
    center.complete_http = 403
    with pytest.raises(ResultTransferError):
        publish(transfer_case)
    assert journal.value['state'] == 'uploaded'
    center.complete_http = None
    publish(transfer_case)
    assert center.count('create') == center.count('put') == 1
    assert center.count('complete') == 2 and center.count('get') == 1


@pytest.mark.parametrize('state', ['expired', 'aborted'])
def test_only_explicit_bound_terminal_session_rotates_persisted_ordinal(transfer_case, state):
    _, center, journal = transfer_case
    center.put_http = [403]
    with pytest.raises(ResultTransferError):
        publish(transfer_case)
    previous = deepcopy(journal.value)
    center.sessions[previous['upload_id']]['status'] = state
    def before_create(idem):
        assert journal.value['state'] == 'creating' and journal.value['upload_id'] == ''
        assert journal.value['idempotency_key'] == idem == previous['idempotency_key'] + ':resume:1'
    center.before_create = before_create
    publish(transfer_case)
    assert journal.value['upload_id'] != previous['upload_id']
    assert center.sessions[previous['upload_id']]['status'] == state
    assert center.count('get') == 1 and center.count('create') == 2


@pytest.mark.parametrize('http_status', [401, 403, 404, 409, 410])
def test_resume_http_errors_never_rotate_or_reupload(transfer_case, http_status):
    _, center, journal = transfer_case
    center.put_http = [403]
    with pytest.raises(ResultTransferError):
        publish(transfer_case)
    before = deepcopy(journal.value); counts = len(center.calls)
    center.get_http = http_status
    with pytest.raises(ResultTransferError) as caught:
        publish(transfer_case)
    assert caught.value.stage == 's3_resume' and caught.value.http_status == http_status
    assert journal.value == before and center.calls[counts:] == [('get', before['upload_id'])]


@pytest.mark.parametrize('field,value', [('id', 'du_' + 'f' * 32),
    ('logical_key', 'assets/another-task/original-0.mp4'), ('declared_bytes', 1)])
def test_resume_server_binding_conflict_stops_before_any_write(transfer_case, field, value):
    _, center, journal = transfer_case
    center.put_http = [403]
    with pytest.raises(ResultTransferError):
        publish(transfer_case)
    previous = deepcopy(journal.value)
    center.sessions[previous['upload_id']][field] = value
    center.calls.clear()
    with pytest.raises(ResultTransferError) as caught:
        publish(transfer_case)
    assert caught.value.code == 'S3_SESSION_BINDING_MISMATCH'
    assert center.calls == [('get', previous['upload_id'])] and journal.value == previous


@pytest.mark.parametrize('state', [['committed'], 'unknown', 'failed'])
def test_ambiguous_server_state_never_rotates_or_reuploads(transfer_case, state):
    _, center, journal = transfer_case
    center.put_http = [403]
    with pytest.raises(ResultTransferError):
        publish(transfer_case)
    previous = deepcopy(journal.value)
    center.get_state = state
    center.calls.clear()
    with pytest.raises(ResultTransferError) as caught:
        publish(transfer_case)
    assert caught.value.stage == 's3_resume'
    assert center.calls == [('get', previous['upload_id'])] and journal.value == previous


def test_committed_checkpoint_cannot_regress_to_an_aborted_session(transfer_case):
    _, center, journal = transfer_case
    publish(transfer_case)
    before = deepcopy(journal.value)
    center.get_state = 'aborted'; center.calls.clear()
    with pytest.raises(ResultTransferError) as caught:
        publish(transfer_case)
    assert caught.value.code == 'S3_SESSION_STATE_CONFLICT'
    assert center.calls == [('get', before['upload_id'])] and journal.value == before


@pytest.mark.parametrize('change', [
    {'task_id': 'other'}, {'index': 1}, {'bucket': 'other'}, {'key': 'other'},
    {'size_bytes': 1}, {'sha256': '0' * 64}, {'content_type': 'image/png'},
    {'version': True}, {'state': []}, {'idempotency_key': 'opaque-new-key'},
    {'token': 'private-secret-extra-field'},
])
def test_checkpoint_mismatch_rejected_before_any_http(transfer_case, change):
    source, center, journal = transfer_case
    publish(transfer_case)
    bad = {**journal.value, **change}; center.calls.clear()
    with pytest.raises(ResultTransferError) as caught:
        store_original(source, 'ijm_original', SETTINGS, checkpoint=bad)
    assert caught.value.stage == 's3_checkpoint' and center.calls == []


def test_no_checkpoint_secrets_and_typed_diagnostic_never_exposes_server_prose(transfer_case):
    _, center, journal = transfer_case
    center.create_http = 507
    with pytest.raises(ResultTransferError) as caught:
        publish(transfer_case)
    error = caught.value
    assert (error.stage, error.code, error.http_status, error.retryable) == ('s3_create', 'S3_INSUFFICIENT_CAPACITY', 507, True)
    serialized = json.dumps(journal.snapshots) + str(error) + json.dumps(error.__dict__)
    for secret in ['private-test-control-token', 'https://', 'signature', 'Authorization', 'private server prose']:
        assert secret not in serialized
    original_idem = journal.value['idempotency_key']
    center.create_http = None
    publish(transfer_case)
    assert center.calls[0] == center.calls[1] == ('create', original_idem)


@pytest.mark.parametrize('boundary', ['creating', 'url_issued', 'uploaded', 'committed'])
def test_persistence_rejection_stops_before_next_http_effect(transfer_case, boundary):
    source, center, journal = transfer_case
    def persist(value):
        if value['state'] == boundary:
            return False
        journal.save(value)
    with pytest.raises(ResultTransferError) as caught:
        store_original(source, 'ijm_original', SETTINGS, persist_checkpoint=persist)
    assert caught.value.code == 'S3_CHECKPOINT_PERSIST_FAILED'
    expected = {'creating': (0, 0, 0, 0), 'url_issued': (1, 0, 0, 0),
                'uploaded': (1, 1, 0, 0), 'committed': (1, 1, 1, 0)}[boundary]
    assert tuple(center.count(x) for x in ['create', 'put', 'complete', 'presign']) == expected


def test_put_retries_open_fresh_file_streams_and_phase_stop_does_not_put(transfer_case):
    _, center, journal = transfer_case
    center.put_http = [503, 200]
    phases = []
    publish(transfer_case, on_phase=lambda value: phases.append(value))
    assert len(center.streams) == 2 and center.streams[0] is not center.streams[1]
    assert all(stream.closed for stream in center.streams)
    assert {'s3_create', 's3_put', 's3_complete', 's3_presign', 's3_checkpoint'} <= set(phases)


def test_phase_callback_false_stops_before_upload(transfer_case):
    source, center, journal = transfer_case
    with pytest.raises(ResultTransferError) as caught:
        publish(transfer_case, on_phase=lambda stage: False if stage == 's3_put' else None)
    assert caught.value.stage == 's3_put' and caught.value.code == 'S3_TRANSFER_INTERRUPTED'
    assert journal.value['state'] == 'url_issued' and center.count('put') == 0


def test_callback_exception_is_not_retained_as_secret_diagnostic(transfer_case):
    source, center, _ = transfer_case
    def broken(_):
        raise ResultTransferError('https://secret.test/?token=private-token')
    with pytest.raises(ResultTransferError) as caught:
        store_original(source, 'ijm_original', SETTINGS, persist_checkpoint=broken)
    assert str(caught.value) == 's3_checkpoint: S3_CHECKPOINT_PERSIST_FAILED'
    assert center.calls == []


def test_legacy_exception_shape_remains_compatible():
    error = ResultTransferError('legacy fixed diagnostic')
    assert str(error) == 'legacy fixed diagnostic'
    assert error.args == ('legacy fixed diagnostic',)
    assert error.stage is error.code is error.http_status is error.retryable is None
    assert ResultTransferError().args == ()
