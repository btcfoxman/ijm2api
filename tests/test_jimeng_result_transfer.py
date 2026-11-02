"""Anonymous S3 delivery must preserve original Jimeng media bytes."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from requests.structures import CaseInsensitiveDict

import app.result_transfer as transfer


SOURCE = 'https://v3-dreamnia.jimeng.com/video.mp4?signature=source-secret'
PUBLIC = 'https://assets.example.test/video.mp4?signature=public-secret'
TASK = 'ijm_original_delivery'
BODY = b'\x00\x00\x00\x20ftypisom\x00\x00\x00\x00' + bytes(range(256)) * 80
SETTINGS = {'result_s3_backend': 's3_center_direct',
            'result_s3_center_api_base': 'https://s3-center.example.test',
            'result_s3_center_token': 'synthetic-credential',
            'result_s3_bucket': 'assets', 'result_s3_key_prefix': 'ijm-results'}


class Response:
    def __init__(self, body=BODY, *, status=200, headers=None, chunks=None):
        self.body = body
        self.status_code = status
        self.headers = CaseInsensitiveDict({'Content-Length': str(len(body)), 'Content-Type': 'video/mp4'})
        self.headers.update(headers or {})
        self.chunks = chunks
        self.delivered = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.closed = True

    def iter_content(self, size):
        for item in self.chunks if self.chunks is not None else [self.body]:
            if isinstance(item, Exception):
                raise item
            self.delivered += 1
            yield item


def public_response(*, body=None, status=206, headers=None):
    prefix = BODY[:4096] if body is None else body
    values = {'Content-Range': f'bytes 0-{len(prefix) - 1}/{len(BODY)}'}
    values.update(headers or {})
    return Response(prefix if status == 206 else BODY, status=status, headers=values)


@pytest.fixture
def context(monkeypatch):
    queued, calls, sessions, stored = [], [], [], []

    class Session:
        def __init__(self):
            self.trust_env = True
            self.cookies = requests.cookies.RequestsCookieJar()
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url, **options):
            calls.append((url, options))
            assert not self.trust_env
            assert 'Cookie' not in options['headers'] and 'Referer' not in options['headers']
            assert options['allow_redirects'] is False
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    def original(path, task_id, storage, **options):
        raw = path.read_bytes()
        stored.append((raw, task_id, storage, options))
        assert raw == BODY and options['content_type'] == 'video/mp4'
        callback = options.get('on_phase')
        if callback:
            callback('s3_create')
            callback('s3_put')
        return PUBLIC, {'bucket': 'assets', 'key': f'ijm-results/{task_id}/original-{options["index"]}.mp4',
            'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'content_type': 'video/mp4', 'original_bytes': True,
            'upload_id': 'du_' + 'a' * 32, 'expires_at': '2099-01-01T00:00:00Z'}

    store = Mock(side_effect=original)
    monkeypatch.setattr(transfer.requests, 'Session', Session)
    monkeypatch.setattr(transfer, 'store_original', store)
    probe = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({
        'streams': [{'width': 720, 'height': 1280}], 'format': {'duration': '30.08'}})))
    monkeypatch.setattr(transfer.subprocess, 'run', probe)
    monkeypatch.setattr(transfer.time, 'sleep', Mock())
    return SimpleNamespace(queued=queued, calls=calls, sessions=sessions, stored=stored,
                           store=store, probe=probe)


def execute(context, **options):
    return transfer.transfer_jimeng_original(SOURCE, TASK, SETTINGS, **options)


def test_original_bytes_use_existing_storage_and_anonymous_range_verification(context):
    context.queued += [Response(), public_response()]
    checkpoint, persist, phases = {'synthetic': 'checkpoint'}, Mock(), []
    url, metadata = execute(context, index=2, checkpoint=checkpoint,
                            persist_checkpoint=persist, on_phase=phases.append)
    assert url == PUBLIC and url != SOURCE
    assert len(context.stored) == 1 and context.stored[0][0] == BODY
    assert context.stored[0][3]['checkpoint'] is checkpoint
    assert context.stored[0][3]['persist_checkpoint'] is persist
    assert metadata['size_bytes'] == len(BODY)
    assert metadata['sha256'] == hashlib.sha256(BODY).hexdigest()
    assert metadata['source_url_sha256'] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert metadata['original_bytes'] and metadata['public_download_verified']
    assert metadata['width'] == 720 and metadata['height'] == 1280
    assert metadata['duration_seconds'] == 30.08 and metadata['public_download_verified_at'] > 0
    assert metadata['public_verification'] == {'status': 206, 'range_bytes': 4096,
        'verified_size_bytes': len(BODY), 'mode': 'range'}
    assert 'public_url_sha256' not in metadata
    assert context.calls[0][1]['proxies'] == context.calls[1][1]['proxies'] == {}
    assert context.calls[1][1]['headers'] == {'Accept-Encoding': 'identity', 'Range': 'bytes=0-4095'}
    assert context.probe.call_args.args[0][0] == 'ffprobe'
    assert 's3_public_verify' in phases and 's3_put' in phases


def test_server_ignoring_range_requires_full_original_hash_and_size(context):
    context.queued += [Response(), public_response(status=200)]
    _, metadata = execute(context)
    assert metadata['public_verification']['mode'] == 'full_get'


def test_interrupted_download_restarts_instead_of_splicing_and_can_use_account_proxy(context):
    interrupted = Response(chunks=[BODY[:1000],
        requests.exceptions.ChunkedEncodingError('stream failed at ' + SOURCE)])
    context.queued += [interrupted, Response(), public_response()]
    _, metadata = execute(context, proxy_url='http://user:private-password@proxy.example.test:1080')
    assert len(context.calls) == 3 and context.stored[0][0] == BODY
    assert context.calls[0][1]['proxies'] == {}
    assert context.calls[1][1]['proxies']['https'].startswith('http://user:')
    assert metadata['source_route'] == 'account_proxy'
    assert interrupted.closed


def test_short_clean_response_is_retried_with_a_fresh_file(context):
    context.queued += [Response(BODY[:100], headers={'Content-Length': str(len(BODY))}),
                      Response(), public_response()]
    execute(context)
    assert context.stored[0][0] == BODY
    assert len(context.calls) == 3


def test_transport_retry_budget_is_small_and_diagnostics_do_not_expose_addresses(context):
    context.queued += [requests.ConnectionError(SOURCE) for _ in range(3)]
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context)
    assert raised.value.code == 'JIMENG_MEDIA_TRANSPORT_ERROR'
    assert SOURCE not in str(raised.value) and 'source-secret' not in str(raised.value)
    assert len(context.calls) == 3
    context.store.assert_not_called()


@pytest.mark.parametrize('headers,code', [
    ({'Content-Length': 'unknown'}, 'JIMENG_MEDIA_LENGTH_INVALID'),
    ({'Content-Length': '0'}, 'JIMENG_MEDIA_LENGTH_INVALID'),
    ({'Content-Length': str(transfer.MAX_SINGLE_UPLOAD_BYTES + 1)}, 'JIMENG_MEDIA_LENGTH_INVALID'),
    ({'Content-Encoding': 'gzip'}, 'JIMENG_MEDIA_ENCODING_INVALID'),
    ({'Content-Type': 'text/html'}, 'JIMENG_MEDIA_TYPE_INVALID'),
])
def test_invalid_length_encoding_or_type_never_reaches_storage(context, headers, code):
    context.queued += [Response(headers=headers)]
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context)
    assert raised.value.code == code
    context.store.assert_not_called()


@pytest.mark.parametrize('url', [
    'http://v3-dreamnia.jimeng.com/file.mp4',
    'https://v3-dreamnia.jimeng.com.evil.test/file.mp4',
    'https://user:password@v3-dreamnia.jimeng.com/file.mp4',
    'https://v3-dreamnia.jimeng.com:8443/file.mp4',
    'https://v3-dreamnia.jimeng.com/file.mp4#fragment',
    'https://v3-dreamnia.jimeng.com/file.mp4\n',
    'https://v3-dreamnia.jimeng.com\\@evil.test/file.mp4',
    'https://127.0.0.1/file.mp4',
])
def test_source_address_is_strict_before_any_network(context, url):
    with pytest.raises(transfer.ResultTransferError):
        transfer.transfer_jimeng_original(url, TASK, SETTINGS)
    assert context.calls == []
    context.store.assert_not_called()


def test_source_redirect_cannot_escape_official_download_hosts(context):
    context.queued += [Response(status=302, headers={'Location': 'https://evil.example.test/file.mp4'})]
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context)
    assert raised.value.code == 'JIMENG_DOWNLOAD_URL_INVALID'
    assert len(context.calls) == 1


def test_source_redirect_within_official_hosts_remains_anonymous(context):
    context.queued += [Response(status=302, headers={'Location': 'https://v9-artist.vlabvod.com/file.mp4'}),
                      Response(), public_response()]
    execute(context)
    assert len(context.calls) == 3


def test_mp4_magic_and_probe_are_both_required(context):
    context.queued += [Response(b'not an mp4' * 100)]
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context)
    assert raised.value.code == 'JIMENG_MP4_INVALID'
    context.store.assert_not_called()


@pytest.mark.parametrize('probe', [
    SimpleNamespace(returncode=1, stdout=''),
    SimpleNamespace(returncode=0, stdout='not json'),
    SimpleNamespace(returncode=0, stdout='{"streams":[],"format":{"duration":"30"}}'),
    SimpleNamespace(returncode=0, stdout='{"streams":[{"width":720,"height":1280}],"format":{"duration":"NaN"}}'),
])
def test_probe_failures_are_safe_and_do_not_upload(context, probe):
    context.queued += [Response()]
    context.probe.return_value = probe
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context)
    assert raised.value.code == 'JIMENG_MP4_INVALID'
    context.store.assert_not_called()


def test_chunk_callback_can_stop_before_any_storage_effect(context):
    response = Response(chunks=[BODY[:1000], BODY[1000:]])
    context.queued += [response]
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context, on_phase=lambda phase: not (phase == 'jimeng_download' and response.delivered >= 2))
    assert raised.value.code == 'JIMENG_TRANSFER_INTERRUPTED'
    context.store.assert_not_called()
    assert response.closed


def test_deadline_prevents_network_and_storage(context):
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context, deadline=0)
    assert raised.value.code == 'JIMENG_TRANSFER_DEADLINE_EXCEEDED'
    assert context.calls == []
    context.store.assert_not_called()


def test_storage_failure_never_publishes_the_upstream_cdn(context):
    context.queued += [Response()]
    context.store.side_effect = transfer.ResultTransferError('safe synthetic storage error', code='S3_AUTH_FAILED')
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context)
    assert raised.value.code == 'S3_AUTH_FAILED'
    assert len(context.calls) == 1


@pytest.mark.parametrize('response,code', [
    (public_response(headers={'Content-Type': 'text/html'}), 'S3_PUBLIC_MEDIA_TYPE_INVALID'),
    (public_response(headers={'Content-Type': 'application/json'}), 'S3_PUBLIC_MEDIA_TYPE_INVALID'),
    (public_response(headers={'Content-Range': f'bytes 1-4096/{len(BODY)}'}), 'S3_PUBLIC_RANGE_MISMATCH'),
    (public_response(headers={'Content-Range': f'bytes 0-4095/{len(BODY) + 1}'}), 'S3_PUBLIC_RANGE_MISMATCH'),
    (public_response(body=b'not-a-video' + b'x' * 4085), 'S3_PUBLIC_BYTES_MISMATCH'),
    (Response(BODY[:-1] + b'x', headers={'Content-Range': ''}), 'S3_PUBLIC_BYTES_MISMATCH'),
    (public_response(status=403), 'S3_PUBLIC_HTTP_ERROR'),
])
def test_anonymous_object_must_match_original_content_before_success(context, response, code):
    context.queued += [Response(), response]
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context)
    assert raised.value.code == code
    assert SOURCE not in str(raised.value) and PUBLIC not in str(raised.value)


def test_public_transport_can_retry_without_redownloading_or_uploading_again(context):
    context.queued += [Response(), requests.ConnectionError(PUBLIC), public_response()]
    _, metadata = execute(context)
    assert metadata['public_download_verified'] is True
    context.store.assert_called_once()
    assert len(context.calls) == 3


def test_public_response_that_only_repeats_the_cdn_address_is_not_a_storage_result(context):
    context.queued += [Response()]
    original = context.store.side_effect
    def bad(*args, **kwargs):
        _, metadata = original(*args, **kwargs)
        return SOURCE, metadata
    context.store.side_effect = bad
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context)
    assert raised.value.code == 'S3_PUBLIC_URL_INVALID'


def test_cancel_during_public_verification_cannot_publish_uploaded_object(context):
    context.queued += [Response(), public_response()]
    with pytest.raises(transfer.ResultTransferError) as raised:
        execute(context, on_phase=lambda phase: phase != 's3_public_verify')
    assert raised.value.code == 'JIMENG_TRANSFER_INTERRUPTED'
    context.store.assert_called_once()
    assert len(context.calls) == 1
