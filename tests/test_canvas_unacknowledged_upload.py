"""Local URL-write uncertainty is narrower than the vendor no-replay verdict."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from test_canvas_resource_not_found import not_found
from test_canvas_source_url_fallback import URL, Runner
from test_canvas_preparation_transient import unavailable
from test_canvas_transport import ACCOUNT, FakeRunner, request
from test_canvas_upload_recovery import clock


def transport():
    return CanvasCLIError('cli.transport_failed', 'Synthetic pre-run transport interruption',
        details={'error': {'code': 'cli.transport_failed', 'class': 'retryable_failure',
            'retryable': True, 'requiredAction': 'retry'}}, outcome_unknown=False)


def pending_fixture(tmp_path, clock):
    runner = FakeRunner(tmp_path)
    original = runner.run
    path = tmp_path/'fallback.png';path.write_bytes(b'validated-image')
    callback = Mock(return_value=path)
    assets = {'images': [{'source_url': 'https://media.example.test/already-ready.png'},
                         {'source_url': URL, 'fallback_file': callback}], 'videos': [], 'audios': []}
    control = {'state': 'timeout', 'resource': None, 'file_written': False, 'reads': 0}

    def run(account, args, **kwargs):
        if args[:2] == ['resource', 'upload'] and '--source-url' in args and args[args.index('--source-url')+1] == URL:
            runner.calls.append(args);control['resource'] = args[-1];raise transport()
        if args[:2] == ['resource', 'upload'] and '--file' in args and args[-1] == control['resource']:
            control['file_written'] = True
        if args[:2] == ['resource', 'get'] and args[2] == control['resource'] and not control['file_written']:
            runner.calls.append(args);control['reads'] += 1
            state = control['state']
            if callable(state):return state(args, kwargs)
            if state == 'timeout':raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Synthetic resource read timeout')
            if state == 'absent':raise not_found()
            if isinstance(state, Exception):raise state
            if isinstance(state, dict):return {'ok': True, 'data': state}
            return {'ok': True, 'data': {'resourceId': args[2], 'type': 'image', 'status': state}}
        return original(account, args, **kwargs)

    runner.run = run
    saved = runner.persisted
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
            persist=saved.append, deadline=180)
    assert caught.value.code == 'cli.transport_failed' and callback.call_count == 0
    checkpoint = deepcopy(saved[-1]['upload_checkpoint'])
    assert len(checkpoint['refs']) == 1 and len(checkpoint['pending_refs']) == 1
    assert checkpoint['pending_refs'][0]['registration'] == 'unacknowledged'
    assert not any(args[0] == 'node' for args in runner.calls)
    runner.calls.clear();control['reads'] = 0
    return runner, control, assets, checkpoint, callback


def test_locally_unacknowledged_url_resumes_absence_to_same_id_file_and_reuses_ready_sibling(tmp_path, clock):
    runner, control, assets, checkpoint, callback = pending_fixture(tmp_path, clock)
    control['state'] = 'absent'
    pending = checkpoint['pending_refs'][0]
    saved = []
    result = CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
        checkpoint=checkpoint, persist=saved.append, deadline=180)
    writes = [args for args in runner.calls if args[:2] == ['resource', 'upload']]
    assert len(writes) == 1 and '--file' in writes[0] and '--source-url' not in writes[0]
    assert writes[0][-1] == pending['resource_id'] and callback.call_count == 1
    assert all(args[args.index('--project-id')+1] == checkpoint['binding']['project_id']
               for args in writes)
    assert result['upload_checkpoint']['binding'] == checkpoint['binding']
    assert result['upload_checkpoint']['pending_refs'] == []
    completed = next(ref for ref in result['upload_checkpoint']['refs'] if ref['resource_id'] == pending['resource_id'])
    assert completed['source_kind'] == 'url' and completed['source_sha256'] == pending['source_sha256']
    assert checkpoint['refs'][0] in result['upload_checkpoint']['refs']
    assert not any(args[:2] == ['node', 'run'] for args in runner.calls)
    assert URL not in repr(result) and str(callback.return_value) not in repr(result)


def test_unacknowledged_original_ready_resource_is_reused_without_any_write_or_download(tmp_path, clock):
    runner, control, assets, checkpoint, callback = pending_fixture(tmp_path, clock)
    control['state'] = 'success'
    result = CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
        checkpoint=checkpoint, deadline=180)
    assert callback.call_count == 0 and result['upload_checkpoint']['pending_refs'] == []
    assert not any(args[:2] == ['resource', 'upload'] for args in runner.calls)


def test_positive_processing_registration_latches_observe_only_before_later_not_found(tmp_path, clock):
    runner, control, assets, checkpoint, callback = pending_fixture(tmp_path, clock)
    steps = ['processing', not_found(), 'success']
    saved = []
    def read(args, kwargs):
        state = steps.pop(0)
        if isinstance(state, Exception):raise state
        if state != 'processing':
            assert saved[-1]['upload_checkpoint']['pending_refs'][0]['registration'] == 'acknowledged'
        return {'ok': True, 'data': {'resourceId': args[2], 'type': 'image', 'status': state}}
    control['state'] = read
    result = CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
        checkpoint=checkpoint, persist=saved.append, deadline=180)
    assert callback.call_count == 0 and result['upload_checkpoint']['pending_refs'] == []
    assert not any(args[:2] == ['resource', 'upload'] for args in runner.calls)


@pytest.mark.parametrize('legacy', ['unknown', 'acknowledged'])
def test_legacy_vendor_unknown_and_ack_never_fall_back_even_after_exact_absence(tmp_path, clock, legacy):
    runner, control, assets, checkpoint, callback = pending_fixture(tmp_path, clock)
    checkpoint['pending_refs'][0]['registration'] = legacy
    control['state'] = 'absent'
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
            checkpoint=checkpoint, deadline=180)
    assert caught.value.code == 'MEDIA_UPLOAD_PENDING'
    assert callback.call_count == 0 and not any(args[:2] == ['resource', 'upload'] for args in runner.calls)


@pytest.mark.parametrize('overall_remaining', [4, 180])
def test_failed_original_read_is_bounded_and_never_means_absent(tmp_path, clock, overall_remaining):
    runner, control, assets, checkpoint, callback = pending_fixture(tmp_path, clock)
    start = clock['now'];deadline = start + overall_remaining;seen = []
    def timed_out(args, kwargs):
        seen.append(kwargs)
        clock['now'] += min(6, kwargs['timeout'])
        raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Synthetic read failed')
    control['state'] = timed_out
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
            checkpoint=checkpoint, deadline=deadline)
    assert caught.value.code == ('CLI_TASK_DEADLINE_EXCEEDED' if overall_remaining == 4 else 'MEDIA_UPLOAD_PENDING')
    assert clock['now'] - start <= min(15, overall_remaining)
    assert all(value['deadline'] <= min(start+15, deadline) for value in seen)
    assert len(seen) <= 3 and callback.call_count == 0
    assert not any(args[:2] == ['resource', 'upload'] for args in runner.calls)


@pytest.mark.parametrize('state', [
    {'resourceId': 'different-resource', 'type': 'image', 'status': 'success'},
    {'resourceId': None, 'type': 'audio', 'status': 'success'},
    CanvasCLIError('cli.authentication_required', 'Authentication required'),
    not_found(resourceId='different-resource'),
])
def test_foreign_or_intervention_read_cannot_authorize_file_fallback(tmp_path, clock, state):
    runner, control, assets, checkpoint, callback = pending_fixture(tmp_path, clock)
    if isinstance(state, dict) and state['resourceId'] is None:
        state = {**state, 'resourceId': control['resource']}
    control['state'] = state
    with pytest.raises(CanvasCLIError):
        CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
            checkpoint=checkpoint, deadline=180)
    assert callback.call_count == 0 and not any(args[:2] == ['resource', 'upload'] for args in runner.calls)


@pytest.mark.parametrize('change', ['no_callback', 'resource', 'source_hash', 'account', 'project', 'profile'])
def test_unacknowledged_checkpoint_requires_exact_bound_url_job_and_callback(tmp_path, clock, change):
    runner, control, assets, checkpoint, callback = pending_fixture(tmp_path, clock)
    control['state'] = 'absent'
    if change == 'no_callback':assets['images'][1].pop('fallback_file')
    if change == 'resource':checkpoint['pending_refs'][0]['resource_id'] = 'foreign-resource'
    if change == 'source_hash':checkpoint['pending_refs'][0]['source_sha256'] = '0'*64
    if change == 'account':checkpoint['binding']['account_id'] += 1
    if change == 'project':checkpoint['binding']['project_id'] = 'foreign-project'
    if change == 'profile':checkpoint['binding']['profile_sha256'] = '0'*64
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
            checkpoint=checkpoint, deadline=180)
    assert caught.value.code in {'CLI_ACCOUNT_MISMATCH', 'CLI_UPLOAD_CHECKPOINT_INVALID'}
    assert callback.call_count == 0 and not any(args[0] in {'resource', 'canvas', 'node'} for args in runner.calls)


def test_resume_absence_does_not_upload_after_callback_stop(tmp_path, clock):
    runner, control, assets, checkpoint, callback = pending_fixture(tmp_path, clock)
    control['state'] = 'absent';runner._closed = __import__('threading').Event()
    def stopped():
        runner._closed.set()
        return callback.return_value
    assets['images'][1]['fallback_file'] = stopped
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT).prepare(request(), 'original-task', assets,
            checkpoint=checkpoint, deadline=180)
    assert caught.value.code == 'CLI_STOPPING'
    assert not any(args[:2] == ['resource', 'upload'] for args in runner.calls)


def test_resumed_file_transport_has_two_remaining_original_id_writes_and_one_lazy_download(tmp_path, clock):
    path = tmp_path/'fallback.png';path.write_bytes(b'validated-image')
    callback = Mock(return_value=path)
    runner = Runner([unavailable()], [not_found()])
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT)._resume_unacknowledged_upload('original-resource', 'original-project', 'image',
            deadline=180, fallback_file=callback, on_registered=lambda value: None)
    writes = [args for args, _ in runner.calls if args[:2] == ['resource', 'upload']]
    # The original unacknowledged URL command already consumed one write.
    assert caught.value.code == 'service.1' and len(writes) == 2 and callback.call_count == 1
    assert all('--file' in args and '--source-url' not in args and args[-1] == 'original-resource' for args in writes)
    assert all(value['deadline'] <= 180 for _, value in runner.calls)


def test_unacknowledged_registration_cannot_be_attached_to_a_local_file_job(tmp_path, clock):
    path = tmp_path/'input.png';path.write_bytes(b'image')
    callback = Mock(return_value=path)
    runner = FakeRunner(tmp_path);client = CanvasClient(runner, ACCOUNT)
    assets = {'images': [{'path': path, 'fallback_file': callback}], 'videos': [], 'audios': []}
    context = {'profile': 'original-profile', 'project_id': 'original-project'}
    _, refs, _, _, _ = client._upload_plan(request(), 'original-task', assets, context, None, 180)
    checkpoint = deepcopy(context['upload_checkpoint'])
    checkpoint['pending_refs'] = [{**refs[0], 'observe_only': True, 'registration': 'unacknowledged'}]
    with pytest.raises(CanvasCLIError) as caught:
        client._upload_plan(request(), 'original-task', assets, context, checkpoint, 180)
    assert caught.value.code == 'CLI_UPLOAD_CHECKPOINT_INVALID' and callback.call_count == 0 and not runner.calls


def test_resumed_absence_cannot_upload_after_lazy_download_exhausts_original_deadline(tmp_path, clock):
    path = tmp_path/'fallback.png';path.write_bytes(b'image')
    runner = Runner([{'resourceId': 'original-resource'}], [not_found()])
    def late_download():
        clock['now'] = 181
        return path
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, ACCOUNT)._resume_unacknowledged_upload('original-resource', 'original-project', 'image',
            deadline=180, fallback_file=late_download, on_registered=lambda value: None)
    assert caught.value.code == 'CLI_TASK_DEADLINE_EXCEEDED'
    assert not any(args[:2] == ['resource', 'upload'] for args, _ in runner.calls)
