"""URL fallback keeps the selected account policy and original task cutoff."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_service import CanvasServiceMixin
from app.errors import BOUNDED_RETRY_FAILURE_MESSAGE, public_error
from app.media import MediaError


ACCOUNT = {'id': 19, 'proxy_url': 'socks5://fixture:secret@proxy.example:1234'}
URLS = ['https://caller.example/first.png', 'https://caller.example/second.jpg']


def test_image_fallback_is_lazy_per_input_and_retains_download_policy(tmp_path, monkeypatch):
    monkeypatch.setattr('app.canvas_service.time.monotonic', lambda: 100)
    download = Mock(side_effect=[(b'first-image', 'image/png', 'first.png'),
                                 (b'second-image', 'image/jpeg', 'second.jpg')])
    monkeypatch.setattr('app.canvas_service._download', download)
    assets = CanvasServiceMixin()._canvas_local_assets({'images': URLS}, ACCOUNT, tmp_path, deadline=120)
    assert not download.called and not list(tmp_path.iterdir())
    paths = [item['fallback_file']() for item in assets['images']]
    assert [path.read_bytes() for path in paths] == [b'first-image', b'second-image']
    assert [call.args for call in download.call_args_list] == [
        (URLS[0], 'images', ACCOUNT['proxy_url']), (URLS[1], 'images', ACCOUNT['proxy_url'])]
    assert [call.kwargs for call in download.call_args_list] == [
        {'allow_direct_fallback': False, 'deadline': 120},
        {'allow_direct_fallback': False, 'deadline': 120}]
    assert [Path(path).name for path in paths] == ['images-1.png', 'images-2.jpg']
    assert assets['images'][0]['source_url'] == URLS[0]


def test_fallback_download_has_short_bound_inside_original_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr('app.canvas_service.time.monotonic', lambda: 100)
    download = Mock(return_value=(b'image', 'image/png', 'reference.png'))
    monkeypatch.setattr('app.canvas_service._download', download)
    assets = CanvasServiceMixin()._canvas_local_assets({'images': URLS[:1]}, ACCOUNT, tmp_path, deadline=500)
    assets['images'][0]['fallback_file']()
    assert download.call_args.kwargs['deadline'] == 130


def test_known_public_image_host_tries_url_import_and_preserves_lazy_direct_first_download(tmp_path, monkeypatch):
    monkeypatch.setattr('app.canvas_service.time.monotonic', lambda: 100)
    download = Mock(return_value=(b'image', 'image/png', 'reference.png'))
    monkeypatch.setattr('app.canvas_service._download', download)
    source = 'https://media.juxingdonghua.com/original.png'
    assets = CanvasServiceMixin()._canvas_local_assets({'images': [source]}, ACCOUNT, tmp_path, deadline=120)
    assert len(assets['images']) == 1 and assets['images'][0]['source_url'] == source
    assert not download.called and not list(tmp_path.iterdir())
    assert assets['images'][0]['fallback_file']().read_bytes() == b'image'
    assert download.call_args.args == (source, 'images', ACCOUNT['proxy_url'])
    assert download.call_args.kwargs == {'allow_direct_fallback': True, 'deadline': 120}


def test_fallback_uses_existing_bounded_media_error_diagnostics(tmp_path, monkeypatch):
    download = Mock(side_effect=MediaError('fixture timeout', category='timeout'))
    monkeypatch.setattr('app.canvas_service._download', download)
    assets = CanvasServiceMixin()._canvas_local_assets({'images': URLS}, ACCOUNT, tmp_path, deadline=500)
    with pytest.raises(MediaError) as caught:
        assets['images'][1]['fallback_file']()
    assert caught.value.diagnostic['media_kind'] == 'images'
    assert caught.value.diagnostic['media_index'] == 2
    assert not list(tmp_path.iterdir())


def test_invalid_public_url_never_gets_local_fallback(tmp_path, monkeypatch):
    download = Mock()
    monkeypatch.setattr('app.canvas_service._download', download)
    with pytest.raises(CanvasCLIError):
        CanvasServiceMixin()._canvas_local_assets({'images': ['http://127.0.0.1/private.png']},
            ACCOUNT, tmp_path)
    download.assert_not_called()


@pytest.mark.parametrize('raw', ['Task deadline passed during Canvas preparation; no generation was submitted',
    'CLI command timed out', 'Canvas preparation retry window expired', 'possible copyright material'])
def test_preparation_timeout_uses_submission_failure_message_without_refund_inference(raw):
    assert public_error('CLI_PREPARATION_TIMEOUT', raw) == {
        'code': 'CLI_PREPARATION_TIMEOUT', 'message': BOUNDED_RETRY_FAILURE_MESSAGE}
