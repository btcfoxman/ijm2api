"""Original media publication must not transcode or repeat committed uploads."""
import hashlib
from unittest.mock import Mock, patch

import pytest

from app.result_transfer import ResultTransferError, store_original
from app.errors import public_error


SETTINGS = {'result_s3_center_api_base': 'http://s3-center:9000',
            'result_s3_center_token': 'test-only-token', 'result_s3_bucket': 'assets',
            'result_s3_key_prefix': 'test-ijm'}


def test_original_jpeg_bytes_and_mime_are_preserved(tmp_path):
    payload = b'\xff\xd8\xff\xe0synthetic-jpeg-original'
    source = tmp_path / 'cli-output.png'
    source.write_bytes(payload)
    upload_id = 'du_' + 'a' * 32
    key = 'test-ijm/ijm_test/original-0.jpg'
    created = {'id': upload_id, 'status': 'url_issued', 'declared_bytes': len(payload),
               'upload': {'method': 'PUT', 'url': 'https://storage.example.test/upload', 'headers': {}}}
    committed = {'id': upload_id, 'status': 'committed', 'declared_bytes': len(payload),
                 'logical_key': 'assets/' + key}
    session = Mock()
    captured = []
    def put(url, **kwargs):
        captured.append(kwargs['data'].read())
        return Mock(status_code=200)
    session.put.side_effect = put
    with patch('app.result_transfer.requests.Session') as factory, \
         patch('app.result_transfer._post', side_effect=[created, committed]) as post, \
         patch('app.result_transfer.presign', return_value=('https://results.example.test/image.jpg', {'bucket': 'assets', 'key': key})), \
         patch('app.result_transfer.subprocess.run') as subprocess_run:
        factory.return_value.__enter__.return_value = session
        url, metadata = store_original(source, 'ijm_test', SETTINGS, content_type='image/jpeg')
    assert captured == [payload]
    assert metadata['sha256'] == hashlib.sha256(payload).hexdigest()
    assert metadata['content_type'] == 'image/jpeg'
    assert metadata['original_bytes'] is True
    assert post.call_args_list[0].args[3]['key'] == key
    assert post.call_args_list[0].args[3]['content_type'] == 'image/jpeg'
    assert source.read_bytes() == payload
    subprocess_run.assert_not_called()


def test_already_committed_original_upload_only_renews_url(tmp_path):
    source = tmp_path / 'original.mp4'
    source.write_bytes(b'synthetic-video')
    key = 'test-ijm/ijm_test/original-0.mp4'
    committed = {'id': 'du_' + 'b' * 32, 'status': 'committed',
        'declared_bytes': source.stat().st_size, 'logical_key': 'assets/' + key}
    with patch('app.result_transfer.requests.Session') as factory, \
         patch('app.result_transfer._post', return_value=committed) as post, \
         patch('app.result_transfer.presign', return_value=('https://results.example.test/video.mp4', {'bucket': 'assets', 'key': key})):
        session = factory.return_value.__enter__.return_value
        url, metadata = store_original(source, 'ijm_test', SETTINGS)
    assert url.endswith('video.mp4')
    session.put.assert_not_called()
    assert post.call_count == 1


@pytest.mark.parametrize('payload,suffix', [(b'', '.mp4'), (b'not media', '.txt')])
def test_empty_or_unknown_result_is_not_published(tmp_path, payload, suffix):
    source = tmp_path / ('result' + suffix)
    source.write_bytes(payload)
    with patch('app.result_transfer.requests.Session') as session, pytest.raises(ResultTransferError):
        store_original(source, 'ijm_test', SETTINGS, content_type='application/octet-stream')
    session.assert_not_called()


@pytest.mark.parametrize('code,message,expected', [
    ('CLI_AUTH_REQUIRED', 'expired session', '上游维护中，请稍后再试~'),
    ('CLI_CAPABILITY_UNAVAILABLE', 'model does not support duration=4', '素材时长不支持，请修改后再试~'),
    ('12009', 'Canvas generation failed', '检测到内容可能违规，请修改提示词或参考素材后重试~'),
    ('UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'connection closed', '提交结果未确认，请核对上游任务，暂勿重复提交~'),
])
def test_canvas_errors_use_existing_caller_vocabulary(code, message, expected):
    assert public_error(code, message)['message'] == expected
