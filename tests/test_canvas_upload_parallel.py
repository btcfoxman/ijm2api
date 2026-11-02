"""Bounded uploads use fake commands only: no subprocess, network or generation."""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError
from app.canvas_client import CanvasClient, preparation_retryable_failure
from app.canvas_timing import PreparationTiming
from app.canvas_upload_limiter import UploadLimiter
from test_canvas_transport import ACCOUNT, FakeRunner, request


def inputs(count):
    urls = [f'https://fixture.example/{index}.png' for index in range(count)]
    return {**request(), 'prompt': 'Image1 Image2', 'images': urls}, {
        'images': [{'source_url': url} for url in urls], 'videos': [], 'audios': []}


def transient_error():
    return CanvasCLIError('service.1', 'synthetic transient service error', details={'error': {
        'code': 'service.1', 'serviceCode': 1, 'class': 'retryable_failure',
        'retryable': True, 'requiredAction': 'retry', 'reason': 'service_unavailable'}})


class MeterRunner(FakeRunner):
    def __init__(self, folder):
        super().__init__(folder)
        self.persisted.append({})
        self.mutex = threading.Lock()
        self.active = self.maximum = 0
        self.by_account = {}
        self.max_account = {}
        self.upload_hook = None

    def run(self, account, args, **kwargs):
        resource = args[:2] in (['resource', 'upload'], ['resource', 'get'])
        if resource:
            with self.mutex:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                identifier = account['id']
                self.by_account[identifier] = self.by_account.get(identifier, 0) + 1
                self.max_account[identifier] = max(self.max_account.get(identifier, 0), self.by_account[identifier])
        try:
            if args[:2] == ['resource', 'upload'] and self.upload_hook:
                self.upload_hook(args)
            if resource:
                time.sleep(.012)
            assert args[:2] not in (['node', 'confirm'], ['node', 'run'])
            return super().run(account, args, **kwargs)
        finally:
            if resource:
                with self.mutex:
                    self.active -= 1
                    self.by_account[account['id']] -= 1


def prepare(client, count=5, task='parallel-task', **kwargs):
    normalized, assets = inputs(count)
    return client.prepare(normalized, task, assets, deadline=time.monotonic()+3, **kwargs)


def test_real_overlap_keeps_source_order_resource_ids_and_prompt_bindings(tmp_path):
    runner = MeterRunner(tmp_path)
    barrier = threading.Barrier(3)
    counter = [0]

    def hook(args):
        with runner.mutex:
            counter[0] += 1
            first = counter[0] <= 3
        if first:
            barrier.wait(1)
        index = int(args[args.index('--source-url')+1].rsplit('/', 1)[1].split('.')[0])
        time.sleep(.006 * (5-index))

    runner.upload_hook = hook
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=3)
    callbacks = [0, 0]
    saved = []

    def persist(value):
        with runner.mutex:
            callbacks[0] += 1; callbacks[1] = max(callbacks[1], callbacks[0])
        time.sleep(.001)
        saved.append(value)
        with runner.mutex:
            callbacks[0] -= 1

    result = prepare(client, persist=persist)
    assert runner.maximum == 3
    assert result['uploaded_resources'] == result['resources']['images']
    node = next(args for args in runner.calls if args[:2] == ['node', 'create'])
    assert [node[index+1] for index, value in enumerate(node) if value == '--ref'] == [
        'res:'+identifier for identifier in result['resources']['images']]
    prompt = node[node.index('--prompt')+1]
    assert '{{res:'+result['resources']['images'][0]+'}}' in prompt
    assert '{{res:'+result['resources']['images'][1]+'}}' in prompt
    assert [ref['index'] for ref in result['upload_checkpoint']['refs']] == list(range(5))
    assert callbacks[1] == 1
    counts = [len(value['upload_checkpoint']['refs']) for value in saved]
    assert counts == sorted(counts) and counts[0] == 0 and counts[-1] == 5
    assert runner.active == 0


def test_account_and_total_caps_span_multiple_client_instances(tmp_path):
    runner = MeterRunner(tmp_path)
    clients = [CanvasClient(runner, {**ACCOUNT, 'id': identifier}, upload_concurrency=3,
        account_upload_concurrency=2, total_upload_concurrency=3) for identifier in (5, 5, 6, 7)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda pair: prepare(pair[1], 4, task=f'task-{pair[0]}'), enumerate(clients)))
    assert len(results) == 4
    assert runner.maximum == 3
    assert max(runner.max_account.values()) <= 2
    assert runner.active == 0


def test_busy_account_does_not_reserve_idle_global_slots():
    limiter = UploadLimiter()
    busy = threading.Event()
    other = threading.Event()
    release = threading.Event()

    def holder():
        with limiter.slot('root', 1, account_limit=1, total_limit=2):
            busy.set(); release.wait(1)

    def waiter():
        with limiter.slot('root', 1, account_limit=1, total_limit=2):
            pass

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(holder)
        assert busy.wait(1)
        second = pool.submit(waiter)
        with limiter.slot('root', 2, account_limit=1, total_limit=2, deadline=time.monotonic()+.3):
            other.set()
        assert other.is_set()
        release.set()
        first.result(); second.result()


def test_sibling_success_is_checkpointed_and_reused_after_failure(tmp_path):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=2)
    original = client._upload_resource
    saved = []
    checkpointed = threading.Event()

    def persist(value):
        saved.append(value)
        if value['upload_checkpoint']['refs']:
            checkpointed.set()

    def upload(path, kind, identifier, project, **kwargs):
        if kwargs['source_url'].endswith('/1.png'):
            assert checkpointed.wait(1)
            raise CanvasCLIError('CLI_INVALID_RESPONSE', 'synthetic sibling failure')
        return original(path, kind, identifier, project, **kwargs)

    client._upload_resource = upload
    with pytest.raises(CanvasCLIError):
        prepare(client, 3, persist=persist)
    assert runner.active == 0
    checkpoint = copy.deepcopy(saved[-1]['upload_checkpoint'])
    original_ids = {ref['resource_id'] for ref in checkpoint['refs']}
    assert original_ids
    assert not any(args[0] == 'node' for args in runner.calls)
    runner.calls.clear()
    resumed = prepare(CanvasClient(runner, ACCOUNT, upload_concurrency=2), 3, checkpoint=checkpoint)
    uploads = {args[-1] for args in runner.calls if args[:2] == ['resource', 'upload']}
    assert not uploads.intersection(original_ids)
    observed = {args[2] for args in runner.calls if args[:2] == ['resource', 'get']}
    assert original_ids <= observed
    assert len(resumed['upload_checkpoint']['refs']) == 3


@pytest.mark.parametrize('changed', ['account', 'profile', 'user', 'proxy', 'task'])
def test_identity_checkpoint_mismatch_fails_before_any_writes(tmp_path, changed):
    runner = MeterRunner(tmp_path)
    checkpoint = prepare(CanvasClient(runner, ACCOUNT), 1)['upload_checkpoint']
    account = dict(ACCOUNT)
    task = 'parallel-task'
    if changed == 'account': account['id'] += 1
    if changed == 'user': account['login_user_id'] = 'different-user'
    if changed == 'proxy': account['proxy_url'] = 'socks5://other:9'
    if changed == 'profile': runner.profile = lambda _: 'retired-profile'
    if changed == 'task': task = 'different-task'
    runner.calls.clear()
    with pytest.raises(CanvasCLIError) as caught:
        prepare(CanvasClient(runner, account), 1, task=task, checkpoint=checkpoint)
    assert caught.value.code == 'CLI_ACCOUNT_MISMATCH'
    assert not any(args[0] in {'resource', 'node', 'canvas'} for args in runner.calls)


@pytest.mark.parametrize('changed', ['request', 'url', 'file'])
def test_changed_media_or_request_gets_new_bound_ids(tmp_path, changed):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    normalized, assets = inputs(1)
    if changed == 'file':
        path = tmp_path/'source.png'; path.write_bytes(b'first-image')
        assets['images'] = [path]
    old = client.prepare(normalized, 'same-task', assets)
    if changed == 'request': normalized['prompt'] = 'changed prompt'
    if changed == 'url': assets['images'][0]['source_url'] += '?changed=1'
    if changed == 'file': path.write_bytes(b'second-image')
    fresh = client.prepare(normalized, 'same-task', assets, checkpoint=old['upload_checkpoint'])
    assert old['resources']['images'] != fresh['resources']['images']
    serialized = json.dumps(fresh['upload_checkpoint'])
    assert 'fixture.example' not in serialized and ACCOUNT['proxy_url'] not in serialized
    assert normalized['prompt'] not in serialized and str(tmp_path) not in serialized


def test_cached_wrong_resource_and_prior_dispatch_never_reach_draft_or_quote(tmp_path):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    checkpoint = prepare(client, 1)['upload_checkpoint']
    original = runner.run

    def wrong(account, args, **kwargs):
        if args[:2] == ['resource', 'get']:
            return {'ok': True, 'data': {'resourceId': 'wrong-resource', 'type': 'image', 'status': 'success'}}
        return original(account, args, **kwargs)

    runner.run = wrong
    runner.calls.clear()
    with pytest.raises(CanvasCLIError) as caught:
        prepare(client, 1, checkpoint=checkpoint)
    assert caught.value.code == 'CLI_RESOURCE_MISMATCH'
    assert not any(args[0] in {'resource', 'node'} for args in runner.calls)
    with pytest.raises(CanvasCLIError) as caught:
        prepare(client, 1, checkpoint={**checkpoint, 'submission_phase': 'unknown'})
    assert caught.value.outcome_unknown


def test_local_checkpoint_uses_bytes_not_temporary_path(tmp_path):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    normalized, assets = inputs(1)
    first, second = tmp_path/'first.png', tmp_path/'second.png'
    first.write_bytes(b'same-content'); second.write_bytes(b'same-content')
    assets['images'] = [first]
    original = client.prepare(normalized, 'same-task', assets)
    runner.calls.clear()
    assets['images'] = [second]
    reused = client.prepare(normalized, 'same-task', assets, checkpoint=original['upload_checkpoint'])
    assert reused['resources'] == original['resources']
    assert not any(args[:2] == ['resource', 'upload'] for args in runner.calls)
    assert any(args[:2] == ['resource', 'get'] for args in runner.calls)


def test_transient_sibling_cannot_replay_an_unresolved_unknown_upload_on_next_prepare(tmp_path):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=2)
    first_error = transient_error()
    entered, first_recorded = threading.Event(), threading.Event()
    saved = []
    original_upload = client._upload_resource
    original_run = runner.run
    uploads = [0]

    def persist(value):
        saved.append(value)
        if value['upload_checkpoint'].get('first_failure', {}).get('code') == 'service.1':
            first_recorded.set()

    def upload(path, kind, identifier, project, **kwargs):
        if kwargs['source_url'].endswith('/0.png'):
            assert entered.wait(1)
            raise first_error
        entered.set()
        assert first_recorded.wait(1)
        return original_upload(path, kind, identifier, project, **kwargs)

    def run(account, args, **kwargs):
        if args[:2] == ['resource', 'upload'] and args[args.index('--source-url')+1].endswith('/1.png'):
            uploads[0] += 1
            runner.uploaded_types[args[-1]] = 'image'
            raise CanvasCLIError('cli.upload_state_unknown', 'observe original resource only')
        return original_run(account, args, **kwargs)

    client._upload_resource = upload
    client._observe_unknown_upload = Mock(side_effect=CanvasCLIError('MEDIA_UPLOAD_PENDING', 'still pending'))
    runner.run = run
    with pytest.raises(CanvasCLIError) as caught:
        prepare(client, 2, persist=persist)
    assert caught.value is first_error and preparation_retryable_failure(caught.value)
    checkpoint = saved[-1]['upload_checkpoint']
    pending = checkpoint['pending_refs']
    assert len(pending) == 1 and pending[0]['registration'] == 'unknown'
    assert pending[0]['observe_only'] is True and pending[0]['index'] == 1
    resource_id = pending[0]['resource_id']
    assert not any(args[0] == 'node' for args in runner.calls)
    runner.calls.clear()
    resumed = prepare(CanvasClient(runner, ACCOUNT, upload_concurrency=2), 2, checkpoint=checkpoint)
    assert uploads[0] == 1
    assert any(args[:3] == ['resource', 'get', resource_id] for args in runner.calls)
    assert resumed['upload_checkpoint']['pending_refs'] == []
    assert len(resumed['upload_checkpoint']['refs']) == 2


def ack_pending_checkpoint(tmp_path):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT)
    saved = []
    error = transient_error()

    def unavailable(*args, **kwargs):
        assert saved[-1]['upload_checkpoint']['pending_refs'][0]['registration'] == 'acknowledged'
        raise error

    client._resource_ready = unavailable
    with pytest.raises(CanvasCLIError) as caught:
        prepare(client, 1, persist=saved.append)
    assert caught.value is error
    assert not any(args[0] == 'node' for args in runner.calls)
    return runner, saved[-1]['upload_checkpoint']


def test_acknowledged_upload_is_persisted_before_read_failure_and_next_prepare_only_reads(tmp_path):
    runner, checkpoint = ack_pending_checkpoint(tmp_path)
    resource_id = checkpoint['pending_refs'][0]['resource_id']
    assert len([args for args in runner.calls if args[:2] == ['resource', 'upload']]) == 1
    resumed = prepare(CanvasClient(runner, ACCOUNT), 1, checkpoint=checkpoint)
    assert len([args for args in runner.calls if args[:2] == ['resource', 'upload']]) == 1
    assert any(args[:3] == ['resource', 'get', resource_id] for args in runner.calls)
    assert resumed['upload_checkpoint']['pending_refs'] == []
    assert resumed['uploaded_resources'] == [resource_id]


@pytest.mark.parametrize('change', ['resource', 'hash', 'observe', 'registration', 'binding', 'shape'])
def test_invalid_pending_checkpoint_cannot_inject_or_replace_an_unresolved_resource(tmp_path, change):
    runner, original = ack_pending_checkpoint(tmp_path)
    checkpoint = copy.deepcopy(original)
    if change == 'resource': checkpoint['pending_refs'][0]['resource_id'] = 'foreign-resource'
    if change == 'hash': checkpoint['pending_refs'][0]['source_sha256'] = '0'*64
    if change == 'observe': checkpoint['pending_refs'][0]['observe_only'] = False
    if change == 'registration': checkpoint['pending_refs'][0]['registration'] = 'reupload'
    if change == 'binding': checkpoint['binding']['media_sha256'] = '0'*64
    if change == 'shape': checkpoint['pending_refs'] = None
    runner.calls.clear()
    with pytest.raises(CanvasCLIError) as caught:
        prepare(CanvasClient(runner, ACCOUNT), 1, checkpoint=checkpoint)
    assert caught.value.code == 'CLI_UPLOAD_CHECKPOINT_INVALID'
    assert not any(args[0] in {'canvas', 'resource', 'node'} for args in runner.calls)


@pytest.mark.parametrize('code', ['CLI_ACCOUNT_MISMATCH', 'CLI_RESOURCE_MISMATCH', 'MEDIA_UPLOAD_FAILED'])
def test_fatal_sibling_blocks_retry_while_preserving_the_original_transient_code(tmp_path, code):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=2)
    entered, first_recorded = threading.Event(), threading.Event()
    first_error, fatal = transient_error(), CanvasCLIError(code, 'synthetic fatal error')
    saved = []

    def persist(value):
        saved.append(value)
        if value['upload_checkpoint'].get('first_failure'):
            first_recorded.set()

    def upload(*args, **kwargs):
        if kwargs['source_url'].endswith('/0.png'):
            assert entered.wait(1)
            raise first_error
        entered.set()
        assert first_recorded.wait(1)
        raise fatal

    client._upload_resource = upload
    with pytest.raises(CanvasCLIError) as caught:
        prepare(client, 2, persist=persist)
    assert caught.value is fatal and not preparation_retryable_failure(caught.value)
    assert fatal.details['parallel_first_failure']['code'] == 'service.1'
    assert saved[-1]['upload_checkpoint']['first_failure']['code'] == 'service.1'
    assert not any(args[0] == 'node' for args in runner.calls)


@pytest.mark.parametrize('failure', ['unacknowledged', 'exception'])
def test_registration_checkpoint_failure_cannot_be_hidden_by_transient_sibling(tmp_path, failure):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=2)
    entered, first_recorded = threading.Event(), threading.Event()
    original_upload = client._upload_resource
    first = transient_error()

    def persist(value):
        if value['upload_checkpoint']['pending_refs']:
            if failure == 'exception':
                raise RuntimeError('synthetic private store failure')
            return False
        if value['upload_checkpoint'].get('first_failure'):
            first_recorded.set()

    def upload(path, kind, identifier, project, **kwargs):
        if kwargs['source_url'].endswith('/0.png'):
            assert entered.wait(1)
            raise first
        entered.set()
        assert first_recorded.wait(1)
        return original_upload(path, kind, identifier, project, **kwargs)

    client._upload_resource = upload
    with pytest.raises((CanvasCLIError, RuntimeError)) as caught:
        prepare(client, 2, persist=persist)
    assert caught.value is not first
    if isinstance(caught.value, CanvasCLIError):
        assert not preparation_retryable_failure(caught.value)
        assert caught.value.details['parallel_first_failure']['code'] == 'service.1'
    assert len([args for args in runner.calls if args[:2] == ['resource', 'upload']]) == 1
    assert not any(args[:2] == ['resource', 'get'] or args[0] == 'node' for args in runner.calls)


@pytest.mark.parametrize('ready', [True, False])
def test_unknown_upload_remains_observe_only_with_original_resource_id(tmp_path, ready):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=3)
    original = runner.run
    count = [0]

    def run(account, args, **kwargs):
        if args[:2] == ['resource', 'upload']:
            count[0] += 1
            runner.uploaded_types[args[-1]] = 'image'
            raise CanvasCLIError('cli.upload_state_unknown', 'observe original upload only')
        return original(account, args, **kwargs)

    runner.run = run
    if not ready:
        client._observe_unknown_upload = Mock(side_effect=CanvasCLIError('MEDIA_UPLOAD_PENDING', 'pending'))
    if ready:
        client._upload_resource(None, 'image', 'original-resource', 'project', source_url='https://fixture.example/image.png')
        assert next(args for args in runner.calls if args[:2] == ['resource', 'get'])[2] == 'original-resource'
    else:
        with pytest.raises(CanvasCLIError) as caught:
            client._upload_resource(None, 'image', 'original-resource', 'project', source_url='https://fixture.example/image.png')
        assert caught.value.code == 'cli.upload_state_unknown'
    assert count[0] == 1


def test_expired_workers_are_drained_and_queued_work_cannot_start(tmp_path):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=2)
    entered = threading.Barrier(3)
    release = threading.Event()
    calls = []

    def upload(*args, **kwargs):
        calls.append(args[2]); entered.wait(1); release.wait(1)

    client._upload_resource = upload
    normalized, assets = inputs(8)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.prepare, normalized, 'deadline-task', assets, deadline=time.monotonic()+.08)
        entered.wait(1)
        time.sleep(.1)
        assert not future.done(), 'prepare returned while upload workers were still alive'
        release.set()
        with pytest.raises(CanvasCLIError) as caught:
            future.result(timeout=1)
    assert caught.value.code == 'CLI_TASK_DEADLINE_EXCEEDED'
    assert len(calls) == 2
    assert not any(args[0] == 'node' for args in runner.calls)


def test_cancelled_persistence_drains_workers_without_saving_a_draft(tmp_path):
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=3)
    with pytest.raises(CanvasCLIError) as caught:
        prepare(client, 8, persist=lambda value: value['phase'] != 'uploading')
    assert caught.value.code == 'CLI_TASK_TERMINATED'
    assert runner.active == 0
    count = len(runner.calls)
    time.sleep(.03)
    assert len(runner.calls) == count
    assert not any(args[0] == 'node' for args in runner.calls)


def test_queued_auth_writer_cannot_deadlock_workers_or_mix_credentials(tmp_path, monkeypatch):
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    fake = MeterRunner(tmp_path)
    entered = threading.Barrier(3)
    release = threading.Event()
    writer_entered = threading.Event()

    def execute(account, args, timeout=None, **kwargs):
        if args[:2] == ['resource', 'upload']:
            entered.wait(1); release.wait(1)
        return fake.run(account, args, timeout=timeout, **kwargs)

    monkeypatch.setattr(runner, '_execute', execute)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=2)

    def writer():
        with runner.account_guard(ACCOUNT, exclusive=True, deadline=time.monotonic()+2):
            writer_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(prepare, client, 5)
        entered.wait(1)
        mutation = pool.submit(writer)
        time.sleep(.03)
        release.set()
        mutation.result(timeout=1)
        with pytest.raises(CanvasCLIError) as caught:
            pending.result(timeout=1)
    assert writer_entered.is_set() and caught.value.code == 'CLI_ACCOUNT_MISMATCH'
    assert not any(args[0] == 'node' for args in fake.calls)
    runner.close()


def test_first_upstream_error_survives_a_faster_cancelled_sibling(tmp_path, monkeypatch):
    paused = threading.Event()
    release = threading.Event()
    sibling_started = threading.Event()
    sibling_cancelled = threading.Event()

    class PausingEvent(threading.Event):
        def __init__(self):
            super().__init__()
            self.once = threading.Lock()
            self.first = True

        def set(self):
            super().set()
            with self.once:
                first, self.first = self.first, False
            if first:
                paused.set(); release.wait(1)

    monkeypatch.setattr('app.canvas_client.Event', PausingEvent)
    class RacingLimiter:
        def __init__(self):
            self.lock = threading.Lock()
            self.count = 0

        @contextmanager
        def slot(self, *args, **kwargs):
            with self.lock:
                self.count += 1
                ordinal = self.count
            if ordinal == 2:
                sibling_started.set()
                assert paused.wait(1)
                sibling_cancelled.set()
                raise CanvasCLIError('CLI_TASK_TERMINATED', 'cancelled before admission')
            yield

    monkeypatch.setattr('app.canvas_client.UPLOAD_LIMITER', RacingLimiter())
    runner = MeterRunner(tmp_path)
    client = CanvasClient(runner, ACCOUNT, upload_concurrency=2)
    original = transient_error()

    def upload(*args, **kwargs):
        assert sibling_started.wait(1)
        raise original

    client._upload_resource = upload
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(prepare, client, 2)
        assert paused.wait(1) and sibling_cancelled.wait(1)
        release.set()
        with pytest.raises(CanvasCLIError) as caught:
            pending.result(timeout=1)
    assert caught.value is original


def test_serial_and_parallel_prepares_release_parent_readers_before_global_wait(tmp_path, monkeypatch):
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    fake = MeterRunner(tmp_path)
    entered, release, serial_waiting, writer_done = [threading.Event() for _ in range(4)]
    serial_reader_count = []
    first_upload = [True]
    counter_lock = threading.Lock()

    class RecordingLimiter(UploadLimiter):
        @contextmanager
        def slot(self, *args, **kwargs):
            if threading.current_thread().name == 'serial-prepare':
                lane = runner._lane(ACCOUNT['id'])
                with lane._condition:
                    serial_reader_count.append(lane._readers.get(threading.get_ident(), 0))
                serial_waiting.set()
            with super().slot(*args, **kwargs):
                yield

    monkeypatch.setattr('app.canvas_client.UPLOAD_LIMITER', RecordingLimiter())

    def execute(account, args, timeout=None, **kwargs):
        if args[:2] == ['resource', 'upload']:
            with counter_lock:
                first, first_upload[0] = first_upload[0], False
            if first:
                entered.set(); release.wait(1)
        return fake.run(account, args, timeout=timeout, **kwargs)

    monkeypatch.setattr(runner, '_execute', execute)
    parallel = CanvasClient(runner, ACCOUNT, upload_concurrency=3, total_upload_concurrency=1)
    serial = CanvasClient(runner, ACCOUNT, upload_concurrency=1, total_upload_concurrency=1)
    errors = []

    def run_serial():
        try:
            prepare(serial, 1, task='serial-task')
        except CanvasCLIError as error:
            errors.append(error)

    def writer():
        with runner.account_guard(ACCOUNT, exclusive=True, deadline=time.monotonic()+2):
            writer_done.set()

    thread = threading.Thread(target=run_serial, name='serial-prepare')
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(prepare, parallel, 5)
        assert entered.wait(1)
        thread.start()
        assert serial_waiting.wait(1)
        mutation = pool.submit(writer)
        time.sleep(.025)
        release.set()
        mutation.result(timeout=1)
        with pytest.raises(CanvasCLIError) as caught:
            pending.result(timeout=1)
    thread.join(1)
    assert not thread.is_alive() and writer_done.is_set()
    assert serial_reader_count == [0]
    assert errors and errors[0].code == caught.value.code == 'CLI_ACCOUNT_MISMATCH'
    assert not any(args[0] == 'node' for args in fake.calls)
    runner.close()


def test_timing_uses_per_thread_phases_and_serial_persistence_callbacks():
    lock = threading.Lock()
    active = maximum = 0

    def listener(snapshot):
        nonlocal active, maximum
        with lock:
            active += 1; maximum = max(maximum, active)
        time.sleep(.001)
        with lock: active -= 1

    timing = PreparationTiming('parallel-timing', on_change=listener)
    barrier = threading.Barrier(3)

    def work(index):
        with timing.phase('reference_upload', index=index, media_type='image'):
            barrier.wait(1)
            timing.command(object(), ACCOUNT, ['resource', 'get', f'private-{index}'], {}, index,
                lambda: {'resourceId': f'private-{index}', 'status': 'success', 'type': 'image'})

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(work, (1, 2, 3)))
    timing.finish()
    snapshot = timing.snapshot()
    assert {row['index'] for row in snapshot['recent_commands']} == {1, 2, 3}
    assert all(row['index'] == row['attempt'] for row in snapshot['recent_commands'])
    assert snapshot['command_count'] == 3 and snapshot['phases']['reference_upload']['count'] == 3
    assert maximum == 1 and snapshot['current_command'] is None and snapshot['current_phase'] is None


@pytest.mark.parametrize('kwargs', [{'upload_concurrency': 0}, {'upload_concurrency': True},
    {'upload_concurrency': 9}, {'account_upload_concurrency': 17}, {'total_upload_concurrency': 33}])
def test_invalid_limits_reject_before_commands(tmp_path, kwargs):
    runner = MeterRunner(tmp_path)
    with pytest.raises(ValueError):
        CanvasClient(runner, ACCOUNT, **kwargs)
    assert runner.calls == []
