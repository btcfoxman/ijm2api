"""Verified checkpoint reuse saves reads, with no generation or grace window."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from test_canvas_transport import ACCOUNT, FakeRunner, model, request


class EpochRunner(FakeRunner):
    def __init__(self, folder):
        super().__init__(folder)
        self.persisted.append({})
        self.epoch = 7
        self.auth_uid = '42'
        self.auth_error = None
        self.auth_hook = None
        self.quote_error = None
        self.logical_clock = None

    def credential_epoch(self, account):
        return self.epoch

    def run(self, account, args, **kwargs):
        if self.logical_clock is not None and args[:2] in (['auth', 'account'], ['resource', 'get']):
            self.logical_clock['now'] += 3
        if args[:2] == ['auth', 'account']:
            self.calls.append(args)
            if self.auth_error:
                raise self.auth_error
            if self.auth_hook:
                self.auth_hook()
            return {'ok': True, 'data': {'userId': self.auth_uid}}
        if args[:2] == ['node', 'quote'] and self.quote_error:
            self.calls.append(args)
            raise self.quote_error
        return super().run(account, args, **kwargs)


def capabilities():
    result = model()
    for mode in result['modes']:
        mode['references']['max'] = 30
        for item in mode['references']['types']:
            item['max'] = 30
    return {'video': [result]}


def fixture(folder):
    urls = [f'https://fixture.example/image-{i}.png' for i in range(15)]
    audios = []
    for i in range(3):
        path = folder / f'voice-{i}.mp3'
        path.write_bytes(b'ID3-original-audio-' + bytes([i]))
        audios.append(path)
    body = {**request(), 'images': urls, 'videos': [],
            'audios': [f'https://fixture.example/voice-{i}.mp3' for i in range(3)]}
    assets = {'images': [{'source_url': value} for value in urls], 'videos': [], 'audios': audios}
    runner = EpochRunner(folder)
    client = CanvasClient(runner, ACCOUNT, capabilities=capabilities())
    original = client.prepare(body, 'saved-inputs', assets, persist=runner.persisted.append)
    checkpoint = deepcopy(original['upload_checkpoint'])
    last = checkpoint['refs'].pop()
    checkpoint['pending_refs'] = [{**{key: value for key, value in last.items() if key != 'verified'},
        'observe_only': True, 'registration': 'acknowledged'}]
    runner.calls.clear()
    return runner, body, assets, original, checkpoint


def resume(runner, body, assets, checkpoint, *, persist=True, deadline=None):
    client = CanvasClient(runner, ACCOUNT, capabilities=capabilities())
    return client, client.prepare(body, 'saved-inputs', assets, checkpoint=checkpoint,
        persist=runner.persisted.append if persist else None, deadline=deadline)


def test_15_images_3_audio_resume_only_observes_pending_audio_and_still_requotes(tmp_path):
    runner, body, assets, original, checkpoint = fixture(tmp_path)
    _, result = resume(runner, body, assets, checkpoint)
    assert len(checkpoint['refs']) == 17 and len(result['upload_checkpoint']['refs']) == 18
    assert result['resources'] == original['resources']
    assert [args[2] for args in runner.calls if args[:2] == ['resource', 'get']] == [
        checkpoint['pending_refs'][0]['resource_id']]
    assert sum(args[:2] == ['auth', 'account'] for args in runner.calls) == 1
    assert not any(args[:2] == ['resource', 'upload'] for args in runner.calls)
    assert sum(args[:2] == ['node', 'quote'] for args in runner.calls) == 1
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm']) for args in runner.calls)


def test_saved_reads_do_not_consume_the_remaining_original_preparation_budget(tmp_path, monkeypatch):
    runner, body, assets, _, checkpoint = fixture(tmp_path)
    clock = {'now': 1000.0}
    monkeypatch.setattr('app.canvas_client.time', SimpleNamespace(
        monotonic=lambda: clock['now'], sleep=lambda delay: clock.update(now=clock['now'] + delay)))
    runner.logical_clock = clock
    _, result = resume(runner, body, assets, checkpoint, deadline=1020.0)
    assert result['phase'] == 'quoted' and clock['now'] == 1006.0
    assert len(result['upload_checkpoint']['refs']) == 18
    # The previous 18 resource reads would require54s. No deadline was extended.
    assert 18 * 3 > 20


@pytest.mark.parametrize('missing', ['durable_persist', 'epoch_getter', 'invalid_epoch'])
def test_insufficient_reuse_evidence_keeps_original_per_resource_validation(tmp_path, missing):
    runner, body, assets, _, checkpoint = fixture(tmp_path)
    if missing == 'epoch_getter':
        runner.credential_epoch = None
    if missing == 'invalid_epoch':
        runner.epoch = True
    _, result = resume(runner, body, assets, checkpoint, persist=missing != 'durable_persist')
    assert len(result['upload_checkpoint']['refs']) == 18
    assert sum(args[:2] == ['resource', 'get'] for args in runner.calls) == 18
    assert not any(args[:2] == ['auth', 'account'] for args in runner.calls)


@pytest.mark.parametrize('failure', ['foreign_uid', 'missing_uid', 'transport', 'epoch_change', 'profile_change'])
def test_fresh_identity_failure_never_skips_into_draft_quote_or_paid_submission(tmp_path, failure):
    runner, body, assets, _, checkpoint = fixture(tmp_path)
    if failure == 'foreign_uid':
        runner.auth_uid = 'foreign-account'
    elif failure == 'missing_uid':
        runner.auth_uid = ''
    elif failure == 'transport':
        runner.auth_error = CanvasCLIError('cli.transport_failed', 'synthetic account read failure')
    elif failure == 'epoch_change':
        runner.auth_hook = lambda: setattr(runner, 'epoch', runner.epoch + 1)
    elif failure == 'profile_change':
        runner.auth_hook = lambda: setattr(runner, 'profile', lambda account: 'retired-profile')
    with pytest.raises(CanvasCLIError) as caught:
        resume(runner, body, assets, checkpoint)
    assert caught.value.code == {'missing_uid': 'CLI_INVALID_RESPONSE', 'transport': 'cli.transport_failed'}.get(
        failure, 'CLI_ACCOUNT_MISMATCH')
    assert not any(args[0] in {'resource', 'node'} for args in runner.calls)
    assert sum(args[:2] == ['auth', 'account'] for args in runner.calls) == 1


def test_proxy_change_after_fresh_identity_fails_before_reusing_first_saved_resource(tmp_path):
    runner, body, assets, _, checkpoint = fixture(tmp_path)
    client = CanvasClient(runner, ACCOUNT, capabilities=capabilities())
    runner.auth_hook = lambda: client.account.update(proxy_url='socks5://different.example:9000')
    with pytest.raises(CanvasCLIError, match='binding changed'):
        client.prepare(body, 'saved-inputs', assets, checkpoint=checkpoint, persist=runner.persisted.append)
    assert not any(args[0] in {'resource', 'node'} for args in runner.calls)


def test_cli_uid_change_after_fresh_identity_cannot_hide_behind_unchanged_login_uid(tmp_path):
    runner, body, assets, _, checkpoint = fixture(tmp_path)
    client = CanvasClient(runner, {**ACCOUNT, 'cli_user_id': '42'}, capabilities=capabilities())
    runner.auth_hook = lambda: client.account.update(cli_user_id='foreign-account')
    with pytest.raises(CanvasCLIError, match='binding changed'):
        client.prepare(body, 'saved-inputs', assets, checkpoint=checkpoint, persist=runner.persisted.append)
    assert not any(args[0] in {'resource', 'node'} for args in runner.calls)


@pytest.mark.parametrize('change', ['request', 'url', 'file'])
def test_changed_request_or_input_bytes_cannot_reuse_old_verified_resources(tmp_path, change):
    runner, body, assets, original, checkpoint = fixture(tmp_path)
    # With no unresolved registration a changed request may build a new plan;
    # it must obtain readiness for every newly bound resource.
    checkpoint = deepcopy(original['upload_checkpoint'])
    if change == 'request':
        body['prompt'] = 'Different input request'
    elif change == 'url':
        assets['images'][0]['source_url'] += '?different=1'
    else:
        assets['audios'][0].write_bytes(b'ID3-new-audio-input')
    _, result = resume(runner, body, assets, checkpoint)
    assert result['resources'] != original['resources']
    assert sum(args[:2] == ['resource', 'upload'] for args in runner.calls) == 18
    assert sum(args[:2] == ['resource', 'get'] for args in runner.calls) == 18
    assert not any(args[:2] == ['auth', 'account'] for args in runner.calls)


def test_native_quote_rejection_still_stops_after_verified_input_reuse(tmp_path):
    runner, body, assets, _, checkpoint = fixture(tmp_path)
    runner.quote_error = CanvasCLIError('service.6', 'synthetic native material rejection')
    with pytest.raises(CanvasCLIError) as caught:
        resume(runner, body, assets, checkpoint)
    assert caught.value is runner.quote_error
    assert sum(args[:2] == ['node', 'quote'] for args in runner.calls) == 1
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm']) for args in runner.calls)
