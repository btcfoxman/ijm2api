"""Public image imports avoid local fetch/upload without changing upload safety."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient, public_image_source_url
from app.canvas_service import CanvasServiceMixin
from test_canvas_execution import context, owner
from test_canvas_transport import ACCOUNT, FakeRunner, request
from test_canvas_upload_recovery import UploadRunner, clock, ready, UPLOAD_MESSAGE
from test_canvas_resource_not_found import not_found


URL = 'https://media.example/image.png?signature=a%2Fb%2Bc&expires=123'


@pytest.mark.parametrize('url', [URL, 'http://media.example/image.jpg', 'https://8.8.8.8/image.webp'])
def test_public_source_url_is_preserved_exactly(url):
    assert public_image_source_url(url) == url


@pytest.mark.parametrize('url', ['https://localhost/a.png', 'http://127.0.0.1/a.png',
    'http://192.168.3.5/a.png', 'http://169.254.169.254/a.png', 'http://[::1]/a.png',
    'http://[::ffff:127.0.0.1]/a.png', 'http://127.1/a.png', 'http://2130706433/a.png',
    'http://0x7f000001/a.png', 'http://metadata/a.png', 'https://example.local/a.png',
    'https://user:password@media.example/a.png', 'https://media.example:0/a.png',
    'https://media.example:99999/a.png', 'https://media.example/a\\b.png',
    'https://media.example/a b.png', 'file:///private/a.png', 'data:image/png;base64,AA=='])
def test_non_public_or_ambiguous_source_url_never_reaches_cli(url):
    with pytest.raises(CanvasCLIError, match='public HTTP'):
        public_image_source_url(url)


def test_service_skips_image_download_without_changing_video_audio_or_data_paths(context, tmp_path):
    store, service, _ = context
    account = owner(store)
    data = 'data:image/png;base64,aW1hZ2U='
    video, audio = 'https://media.example/v.mp4', 'https://media.example/a.mp3'
    original = {'images': [URL, data], 'videos': [video], 'audios': [audio]}
    deadline = 12345
    def download(source, kind, proxy, **kwargs):
        assert source != URL
        assert proxy == account['proxy_url'] and kwargs['deadline'] == deadline
        suffix = {'images':'.png', 'videos':'.mp4', 'audios':'.mp3'}[kind]
        return b'unchanged-bytes', 'application/octet-stream', kind + suffix
    with patch('app.canvas_service._download', side_effect=download) as fetched, \
         patch('app.canvas_service._probe_media', return_value={'duration':6.0}) as probe:
        assets = service._canvas_local_assets(original, account, tmp_path, deadline=deadline)
    assert assets['images'][0] == {'source_url': URL}
    assert [call.args[0] for call in fetched.call_args_list] == [data, video, audio]
    assert probe.call_count == 2
    for kind in ('videos', 'audios'):
        assert Path(assets[kind][0]['path']).read_bytes() == b'unchanged-bytes'
        assert assets[kind][0]['duration'] == 6.0
    assert CanvasServiceMixin._canvas_direct_media_allowed(URL) is False


def test_prepare_imports_source_url_with_original_ids_and_no_file(tmp_path):
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    body = request()
    result = client.prepare(body, 'ijm_source_url', {'images':[{'source_url':URL}], 'videos':[], 'audios':[]},
        persist=lambda value:runner.persisted.append(value))
    writes = [args for args in runner.calls if args[:2] == ['resource','upload']]
    assert len(writes) == 1 and '--source-url' in writes[0] and '--file' not in writes[0]
    assert writes[0][writes[0].index('--source-url')+1] == URL
    assert writes[0][-1] == result['resources']['images'][0]
    assert result['uploaded_resources'] == result['resources']['images']
    assert not any(args[:2] == ['node','run'] for args in runner.calls)
    assert URL not in repr(result)


@pytest.mark.parametrize('media_type', ['video','audio'])
def test_source_url_is_image_only_before_any_command(tmp_path, media_type):
    runner = FakeRunner(tmp_path)
    with pytest.raises(CanvasCLIError):
        CanvasClient(runner, ACCOUNT)._upload_resource(None, media_type, 'original-resource',
            'original-project', source_url=URL)
    assert runner.calls == []


@pytest.mark.parametrize('ready_eventually', [False, True])
def test_unknown_remote_import_only_observes_original_id_never_falls_back_to_file(clock, ready_eventually):
    responses = [not_found(requiredAction='none', retryable=False)]
    if ready_eventually:
        responses.append(ready())
    runner = UploadRunner(clock, responses)
    invoke = lambda:CanvasClient(runner, ACCOUNT)._upload_resource(None, 'image', 'original-resource',
        'original-project', deadline=150, source_url=URL)
    if ready_eventually:
        invoke()
    else:
        with pytest.raises(CanvasCLIError) as caught:
            invoke()
        assert caught.value.code == 'cli.upload_state_unknown' and caught.value.message == UPLOAD_MESSAGE
        assert clock['now'] == 120
    writes = [args for args, _, _ in runner.calls if args[:2] == ['resource','upload']]
    assert len(writes) == 1 and '--source-url' in writes[0] and '--file' not in writes[0]
    assert writes[0][-1] == 'original-resource'
    assert all(args[2:] == ['original-resource','--project-id','original-project']
               for args, _, _ in runner.calls if args[:2] == ['resource','get'])


def test_data_and_local_images_still_use_the_file_transport(tmp_path):
    runner = FakeRunner(tmp_path)
    path = tmp_path/'reference.png';path.write_bytes(b'local-data-image')
    CanvasClient(runner, ACCOUNT).prepare(request(), 'ijm_data_image', {'images':[{'path':str(path)}],
        'videos':[], 'audios':[]}, persist=lambda value:runner.persisted.append(value))
    upload = next(args for args in runner.calls if args[:2] == ['resource','upload'])
    assert '--file' in upload and '--source-url' not in upload
