"""A delivery budget covers S3 effects and streaming without renewing on retry."""
from unittest.mock import Mock, patch

import pytest

import requests

from app.result_transfer import ResultTransferError, _DeadlineReader, _post, store_original
from test_canvas_result_storage import SETTINGS


def test_deadline_stream_retains_real_http_content_length_and_stops_late_reads(tmp_path):
    source = tmp_path / 'original.mp4'
    payload = b'original-bytes-for-http'
    source.write_bytes(payload)
    clock = {'now': 100}
    with source.open('rb') as stream, \
            patch('app.result_transfer.time.monotonic', side_effect=lambda: clock['now']):
        body = _DeadlineReader(stream, 110, 's3_put')
        request = requests.Request('PUT', 'https://storage.example.test/put', data=body).prepare()
        assert request.headers['Content-Length'] == str(len(payload))
        assert request.body.read() == payload
        clock['now'] = 111
        with pytest.raises(ResultTransferError) as found:
            request.body.read()
        assert found.value.code == 'S3_TRANSFER_DEADLINE_EXCEEDED'


@pytest.mark.parametrize('deadline', [100, True, float('nan')])
def test_invalid_or_expired_delivery_budget_never_opens_an_upload(tmp_path, deadline):
    with patch('app.result_transfer.time.monotonic', return_value=100), \
            patch('app.result_transfer.requests.Session') as factory, \
            pytest.raises(ResultTransferError) as found:
        store_original(tmp_path / 'missing.mp4', 'ijm_budget', SETTINGS, deadline=deadline)
    assert found.value.code in {'S3_TRANSFER_DEADLINE_EXCEEDED', 'S3_DEADLINE_INVALID'}
    factory.assert_not_called()


def test_retry_backoff_uses_the_original_budget_without_another_http_effect():
    clock = {'now': 100.0}
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        clock['now'] += delay
    http = Mock()
    http.post.return_value = Mock(status_code=503)
    with patch('app.result_transfer.time.monotonic', side_effect=lambda: clock['now']), \
            patch('app.result_transfer.time.sleep', side_effect=sleep), \
            pytest.raises(ResultTransferError) as found:
        _post(http, 'https://storage.example.test/create', 'test-token', {}, 'storage',
              stage='s3_create', deadline=100.25)
    assert found.value.code == 'S3_TRANSFER_DEADLINE_EXCEEDED'
    assert http.post.call_count == 1 and sleeps == [0.25]
    assert http.post.call_args.kwargs['timeout'] <= 0.25


@pytest.mark.parametrize('expire_during_stream', [False, True])
def test_expired_put_never_commits_or_presigns_and_keeps_a_resumable_bound_checkpoint(tmp_path, expire_during_stream):
    payload = b'original-media-bytes'
    source = tmp_path / 'original.mp4'
    source.write_bytes(payload)
    clock = {'now': 100.0}
    created = {'id': 'du_' + 'a' * 32, 'status': 'url_issued',
        'declared_bytes': len(payload),
        'upload': {'method': 'PUT', 'url': 'https://storage.example.test/put', 'headers': {}}}
    checkpoints = []
    session = Mock()
    def put(url, **kwargs):
        assert all(value <= 5 for value in kwargs['timeout'])
        if expire_during_stream:
            assert kwargs['data'].read(2) == payload[:2]
            clock['now'] = 111
            kwargs['data'].read(2)  # The stream cannot send another chunk.
            raise AssertionError('Expired stream continued')
        assert kwargs['data'].read() == payload
        clock['now'] = 111  # The PUT may have succeeded, but do not commit late.
        return Mock(status_code=200)
    session.put.side_effect = put
    with patch('app.result_transfer.time.monotonic', side_effect=lambda: clock['now']), \
            patch('app.result_transfer.requests.Session') as factory, \
            patch('app.result_transfer._post', return_value=created) as post, \
            patch('app.result_transfer.presign') as presign, \
            pytest.raises(ResultTransferError) as found:
        factory.return_value.__enter__.return_value = session
        store_original(source, 'ijm_budget', SETTINGS, deadline=110,
                       persist_checkpoint=lambda value: checkpoints.append(value))
    assert found.value.code == 'S3_TRANSFER_DEADLINE_EXCEEDED'
    assert post.call_count == 1 and session.put.call_count == 1
    assert checkpoints[-1]['state'] == 'url_issued'
    assert checkpoints[-1]['task_id'] == 'ijm_budget'
    assert checkpoints[-1]['size_bytes'] == len(payload)
    presign.assert_not_called()
    assert source.read_bytes() == payload
