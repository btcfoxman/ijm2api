"""Lazy transport fallback reuses the original URL-bound input identity only."""
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.media import MediaError
from test_canvas_resource_not_found import not_found
from test_canvas_preparation_transient import unavailable
from test_canvas_transport import ACCOUNT, FakeRunner, request
from test_canvas_upload_recovery import clock, ready, upload_unknown


URL = 'https://media.example.test/reference.png?signature=private-signature'


class Runner:
    timeout = 120

    def __init__(self, writes, reads):
        self.writes, self.reads, self.calls = list(writes), list(reads), []
        self._closed = Event()

    def run(self, account, args, **kwargs):
        self.calls.append((list(args), kwargs))
        assert args[:2] in (['resource', 'upload'], ['resource', 'get'])
        assert '--project-id' in args and args[args.index('--project-id') + 1] == 'original-project'
        if args[:2] == ['resource', 'upload']:
            assert args[-1] == 'original-resource'
            values = self.writes
        else:
            assert args[2] == 'original-resource'
            values = self.reads
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return {'ok': True, 'data': value}


def invoke(runner, callback, *, deadline=180, registered=None):
    return CanvasClient(runner, ACCOUNT)._upload_resource(None, 'image', 'original-resource',
        'original-project', source_url=URL, fallback_file=callback, deadline=deadline,
        on_registered=registered)


def test_exact_absent_registration_permits_one_lazy_file_download_same_original_ids(tmp_path, clock):
    file = tmp_path / 'reference.png';file.write_bytes(b'validated-image')
    callback = Mock(return_value=file)
    runner = Runner([unavailable(), {'resourceId': 'original-resource'}], [not_found(), ready()])
    registered = []
    invoke(runner, callback, registered=registered.append)
    writes = [args for args, _ in runner.calls if args[:2] == ['resource', 'upload']]
    assert len(writes) == 2 and '--source-url' in writes[0] and '--file' not in writes[0]
    assert '--file' in writes[1] and '--source-url' not in writes[1]
    assert writes[0][-1] == writes[1][-1] == 'original-resource'
    assert writes[1][writes[1].index('--file') + 1] == str(file.resolve())
    assert callback.call_count == 1 and registered == ['acknowledged']
    assert all(kwargs['deadline'] <= 180 for _, kwargs in runner.calls)


def test_url_and_file_share_three_write_cap_and_only_one_lazy_download(tmp_path, clock, monkeypatch):
    file = tmp_path / 'reference.png';file.write_bytes(b'validated-image')
    callback = Mock(return_value=file)
    runner = Runner([unavailable()], [not_found()])
    client = CanvasClient(runner, ACCOUNT)
    monkeypatch.setattr(client, '_resource_ready', Mock(side_effect=CanvasCLIError('MEDIA_UPLOAD_PENDING', 'not yet ready')))
    with pytest.raises(CanvasCLIError) as caught:
        client._upload_resource(None, 'image', 'original-resource', 'original-project',
            source_url=URL, fallback_file=callback, deadline=180)
    assert caught.value.code == 'service.1'
    writes = [args for args, _ in runner.calls if args[:2] == ['resource', 'upload']]
    assert len(writes) == 3 and ['--source-url' in args for args in writes] == [True, False, False]
    assert callback.call_count == 1 and all(args[-1] == 'original-resource' for args in writes)


@pytest.mark.parametrize('failure', [
    CanvasCLIError('cli.authentication_required', 'auth'),
    CanvasCLIError('service.20115', 'content rejection', details={'error': {'serviceCode': 20115, 'retryable': True}}),
    unavailable(**{'class': 'invalid_input'}),
    unavailable(requiredAction='fix_input'),
    unavailable(reason='content_policy'),
    CanvasCLIError('CLI_COMMAND_TIMEOUT', 'explicit no retry', details={'error': {'retryable': False}}),
    CanvasCLIError('CLI_COMMAND_TIMEOUT', 'business', details={'error': {'serviceCode': 1006}}),
    CanvasCLIError('CLI_COMMAND_TIMEOUT', 'partial', details={'partialData': {'resourceId': 'possibly-registered'}}),
])
def test_auth_policy_business_and_ambiguous_failures_never_fall_back(clock, failure):
    callback = Mock()
    runner = Runner([failure], [not_found()])
    with pytest.raises(CanvasCLIError) as caught:
        invoke(runner, callback)
    assert caught.value is failure and callback.call_count == 0
    assert len(runner.calls) == 1


@pytest.mark.parametrize('failure', [upload_unknown(), CanvasCLIError('CLI_COMMAND_TIMEOUT', 'unknown receipt', outcome_unknown=True)])
def test_unknown_url_import_only_observes_without_file_or_repeat_write(clock, failure):
    callback = Mock()
    runner = Runner([failure], [ready()])
    registered = []
    invoke(runner, callback, registered=registered.append)
    assert callback.call_count == 0 and registered == ['unknown']
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


def test_acknowledged_url_import_read_failure_never_downloads_or_rewrites(clock):
    callback = Mock()
    runner = Runner([{'resourceId': 'original-resource'}], [unavailable()])
    with pytest.raises(CanvasCLIError) as caught:
        invoke(runner, callback)
    assert caught.value.code == 'service.1' and callback.call_count == 0
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


def test_pending_original_registration_is_observe_only_not_a_file_fallback(clock):
    callback = Mock()
    runner = Runner([unavailable()], [dict(ready(), status='processing'), ready()])
    registered = []
    invoke(runner, callback, registered=registered.append)
    assert callback.call_count == 0 and registered == ['acknowledged']
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


@pytest.mark.parametrize('response', [
    not_found(resourceId='foreign-resource'),
    not_found(message='Other invalid input'),
    CanvasCLIError('cli.authentication_required', 'auth'),
    ready('foreign-resource'),
    dict(ready(), type='audio'),
    dict(ready(), status='failed'),
])
def test_only_exact_not_found_not_wrong_identity_or_failure_allows_file(clock, response):
    callback = Mock()
    runner = Runner([unavailable()], [response])
    with pytest.raises(CanvasCLIError):
        invoke(runner, callback)
    assert callback.call_count == 0
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


def test_failed_read_is_not_absence_and_durably_restricts_to_observation(clock):
    failure = unavailable()
    callback = Mock()
    runner = Runner([failure], [unavailable()])
    registered = []
    with pytest.raises(CanvasCLIError) as caught:
        invoke(runner, callback, registered=registered.append)
    assert caught.value is failure and callback.call_count == 0 and registered == ['unknown']
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


@pytest.mark.parametrize('cancel', ['deadline', 'stop'])
def test_callback_cannot_start_file_upload_after_deadline_or_stop(tmp_path, clock, cancel):
    file = tmp_path / 'reference.png';file.write_bytes(b'image')
    runner = Runner([unavailable()], [not_found()])
    def download():
        if cancel == 'deadline':clock['now'] = 181
        else:runner._closed.set()
        return file
    with pytest.raises(CanvasCLIError) as caught:
        invoke(runner, download)
    assert caught.value.code == ('CLI_TASK_DEADLINE_EXCEEDED' if cancel == 'deadline' else 'CLI_STOPPING')
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


def test_structured_media_download_failure_is_preserved_for_service_normalization(clock):
    failure = MediaError('safe download failure', category='timeout', attempt_count=2)
    callback = Mock(side_effect=failure)
    runner = Runner([unavailable()], [not_found()])
    with pytest.raises(MediaError) as caught:
        invoke(runner, callback)
    assert caught.value is failure and callback.call_count == 1
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


def test_fallback_keeps_url_checkpoint_hash_and_ready_siblings_reusable(tmp_path):
    runner = FakeRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    body = request()
    file = tmp_path / 'download.png';file.write_bytes(b'downloaded-on-demand')
    lazy = Mock(return_value=file)
    calls = runner.run
    waiting = set()
    def run(account, args, **kwargs):
        if args[:2] == ['resource', 'upload'] and '--source-url' in args and args[args.index('--source-url')+1] == URL:
            runner.calls.append(args)
            waiting.add(args[-1])
            raise unavailable()
        if args[:2] == ['resource', 'get'] and args[2] in waiting:
            runner.calls.append(args);waiting.remove(args[2]);raise not_found()
        return calls(account, args, **kwargs)
    runner.run = run
    assets = {'images': [{'source_url': URL, 'fallback_file': lazy}, {'source_url': 'https://media.example.test/ready.png'}], 'videos': [], 'audios': []}
    saved = client.prepare(body, 'ijm_fallback_binding', assets, persist=runner.persisted.append)
    writes = [args for args in runner.calls if args[:2] == ['resource', 'upload']]
    fallback_writes = [args for args in writes if '--file' in args]
    assert len(writes) == 3 and len(fallback_writes) == 1 and lazy.call_count == 1
    original_ids = saved['resources']['images']
    assert fallback_writes[0][-1] == original_ids[0]
    assert all(ref['source_kind'] == 'url' for ref in saved['upload_checkpoint']['refs'])
    assert URL not in repr(saved) and str(file) not in repr(saved) and 'fallback_file' not in repr(saved)
    runner.calls.clear()
    resumed = CanvasClient(runner, ACCOUNT).prepare(body, 'ijm_fallback_binding', assets,
        checkpoint=saved['upload_checkpoint'], persist=runner.persisted.append)
    assert resumed['resources']['images'] == original_ids and lazy.call_count == 1
    assert not any(args[:2] == ['resource', 'upload'] for args in runner.calls)
