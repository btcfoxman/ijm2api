"""One durable same-UUID FILE recovery; fake commands only, never node.run."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_input_recovery import TICKET_KEY
from test_canvas_transport import ACCOUNT, FakeRunner, request


def unknown():
    return CanvasCLIError('cli.upload_state_unknown', 'Original upload registration uncertain',
        exit_code=22, details={'error': {'code': 'cli.upload_state_unknown', 'class': 'human_intervention',
            'requiredAction': 'human_intervention', 'retryable': False, 'reason': 'vod.data_timeout'}})


def missing(identifier):
    return CanvasCLIError('service.6', 'The requested resource was not found.', details={'error': {
        'code': 'service.6', 'serviceCode': 6, 'class': 'invalid_input', 'requiredAction': 'none',
        'message': 'The requested resource was not found.', 'resourceId': identifier}})


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    clock = {'mono': 0.0, 'wall': 1000.0}
    def sleep(seconds):
        clock['mono'] += seconds
        clock['wall'] += seconds
    monkeypatch.setattr('app.canvas_client.time', SimpleNamespace(
        monotonic=lambda: clock['mono'], time=lambda: clock['wall'], sleep=sleep))
    path = tmp_path / 'original.png'
    path.write_bytes(b'unchanged-original-input')

    class Runner(FakeRunner):
        def __init__(self):
            super().__init__(tmp_path)
            self.persisted.append({})
            self.saved = []
            self.uploads = 0
            self.epoch = 0
            self.profile_name = 'original-profile'
            self.mode = 'recover'
            self.on_probe = lambda: None
            self.on_retry = lambda: None
            self.on_identity = lambda: None
            self.first_error = unknown
            self.probe_error = missing
            self.on_save = lambda value: None
        def credential_epoch(self, account): return self.epoch
        def profile(self, account): return self.profile_name
        def persist(self, value):
            self.on_save(value)
            self.saved.append(deepcopy(value))
            return True
        def run(self, account, args, **kwargs):
            assert args[:2] not in (['node', 'run'], ['node', 'confirm'])
            if args[:2] == ['auth', 'account'] and self.uploads:
                self.on_identity()
            if args[:2] == ['resource', 'upload']:
                self.calls.append(list(args)); self.uploads += 1
                if self.mode == 'ack':
                    return {'ok': True, 'data': {'resourceId': args[-1], 'mediaType': 'image'}}
                if self.uploads == 1: raise self.first_error()
                ticket = self.saved[-1]['upload_checkpoint'][TICKET_KEY][0]
                assert ticket['attempt'] == 1 and ticket['state'] == 'claimed'
                assert self.saved[-1]['upload_checkpoint']['pending_refs'][0]['registration'] == 'unknown'
                self.on_retry()
                if self.mode == 'twice_unknown': raise unknown()
                if self.mode == 'crash_retry': raise RuntimeError('Synthetic interruption after durable claim')
                return {'ok': True, 'data': {'resourceId': args[-1], 'mediaType': 'image'}}
            if args[:2] == ['resource', 'get']:
                self.calls.append(list(args))
                if self.uploads == 1:
                    self.on_probe()
                    if self.mode in {'processing', 'ack', 'foreign_project'}:
                        current = {'resourceId': args[2], 'type': 'image',
                            'status': 'processing' if clock['mono'] < 3 else 'success'}
                        if self.mode == 'foreign_project': current['projectId'] = 'foreign-project'
                        return {'ok': True, 'data': current}
                    if self.mode == 'read_auth': raise CanvasCLIError('CLI_AUTH_REQUIRED', 'Explicit auth refusal')
                    if self.mode == 'read_network': raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Read timeout')
                    raise self.probe_error(args[2])
                if self.mode == 'twice_unknown': raise missing(args[2])
                return {'ok': True, 'data': {'resourceId': args[2], 'type': 'image', 'status': 'success'}}
            return super().run(account, args, **kwargs)

    runner = Runner()
    client = CanvasClient(runner, deepcopy(ACCOUNT))
    body = {**request(), 'images': ['https://fixture.example/input.png']}
    assets = {'images': [{'path': str(path)}], 'videos': [], 'audios': []}
    def prepare(*, checkpoint=None, persist=True, selected_client=None):
        return (selected_client or client).prepare(body, 'original-task', assets,
            persist=runner.persist if persist else None, checkpoint=checkpoint, deadline=500)
    return runner, client, path, clock, prepare


def test_exact_absence_and_durable_claim_allow_one_same_id_same_file_retry(prepared):
    runner, _, _, _, prepare = prepared
    result = prepare()
    writes = [args for args in runner.calls if args[:2] == ['resource', 'upload']]
    assert len(writes) == 2 and writes[0] == writes[1]
    assert '--force' not in writes[1] and '--source-url' not in writes[1]
    snapshots = [row['upload_checkpoint'][TICKET_KEY][0] for row in runner.saved
                 if row.get('upload_checkpoint', {}).get(TICKET_KEY)]
    assert snapshots[0]['attempt'] == 0 and snapshots[0]['state'] == 'observing'
    assert snapshots[-1]['attempt'] == 1 and snapshots[-1]['original_error']['reason'] == 'vod.data_timeout'
    assert {item['observation_deadline_at'] for item in snapshots} == {1120}
    assert result['upload_checkpoint']['pending_refs'] == []


@pytest.mark.parametrize('mode', ['processing', 'read_auth', 'read_network'])
def test_non_absence_fresh_read_never_authorizes_another_write(prepared, mode):
    runner, _, _, _, prepare = prepared
    runner.mode = mode
    if mode == 'processing': prepare()
    else:
        with pytest.raises(CanvasCLIError): prepare()
    assert runner.uploads == 1
    assert runner.saved[-1]['upload_checkpoint'][TICKET_KEY][0]['attempt'] == 0


def test_acknowledged_upload_only_observes_processing_and_never_creates_a_retry_ticket(prepared):
    runner, _, _, clock, prepare = prepared
    runner.mode = 'ack'
    result = prepare()
    assert runner.uploads == 1 and clock['mono'] == 3
    assert TICKET_KEY not in result['upload_checkpoint']


def test_existing_original_resource_from_another_explicit_project_is_not_adopted_or_rewritten(prepared):
    runner, _, _, _, prepare = prepared
    runner.mode = 'foreign_project'
    with pytest.raises(CanvasCLIError) as caught: prepare()
    assert caught.value.code == 'CLI_RESOURCE_MISMATCH' and runner.uploads == 1


@pytest.mark.parametrize('conflict', ['policy', 'partial_absence'])
def test_explicit_policy_or_partial_presence_never_authorizes_the_same_id_write(prepared, conflict):
    runner, _, _, _, prepare = prepared
    if conflict == 'policy':
        def policy():
            error = unknown(); error.details['error']['reason'] = 'content_policy_violation'; return error
        runner.first_error = policy
    else:
        def partial(identifier):
            error = missing(identifier); error.details['partialData'] = {'resources': [identifier]}; return error
        runner.probe_error = partial
    with pytest.raises(CanvasCLIError): prepare()
    assert runner.uploads == 1
    assert runner.saved[-1]['upload_checkpoint'][TICKET_KEY][0]['attempt'] == 0


@pytest.mark.parametrize('stage', ['observing', 'claimed'])
def test_failed_ticket_persistence_never_retries_the_file(prepared, stage):
    runner, _, _, _, prepare = prepared
    def reject(value):
        entries = value.get('upload_checkpoint', {}).get(TICKET_KEY, [])
        if entries and entries[0]['state'] == stage:
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_FAILED', 'Synthetic persistence failure')
    runner.on_save = reject
    with pytest.raises(CanvasCLIError): prepare()
    assert runner.uploads == 1
    if stage == 'observing':
        assert not any(args[:2] == ['resource', 'get'] for args in runner.calls)


@pytest.mark.parametrize('mutation', ['bytes', 'profile', 'epoch', 'proxy', 'uid', 'cli_uid', 'claim_bytes', 'claim_epoch'])
def test_changed_original_binding_or_file_burns_no_second_upload(prepared, mutation):
    runner, client, path, _, prepare = prepared
    def change():
        if mutation == 'bytes': path.write_bytes(b'different-content')
        if mutation == 'profile': runner.profile_name = 'retired-profile'
        if mutation == 'epoch': runner.epoch += 1
        if mutation == 'proxy': client.account['proxy_url'] = 'socks5://other.invalid:1'
        if mutation == 'uid': client.account['login_user_id'] = 'another-user'
        if mutation == 'cli_uid': client.account['cli_user_id'] = 'another-user'
    runner.on_probe = change
    if mutation in {'claim_bytes', 'claim_epoch'}:
        def change_after_claim(value):
            entries = value.get('upload_checkpoint', {}).get(TICKET_KEY, [])
            if entries and entries[0]['attempt'] == 1:
                if mutation == 'claim_bytes': path.write_bytes(b'changed-after-durable-claim')
                else: runner.epoch += 1
        runner.on_save = change_after_claim
    with pytest.raises(CanvasCLIError): prepare()
    assert runner.uploads == 1


@pytest.mark.parametrize('claimed', [False, True])
def test_restart_reuses_observation_deadline_and_never_obtains_another_write_ticket(prepared, claimed):
    runner, _, _, clock, prepare = prepared
    runner.mode = 'crash_retry' if claimed else 'read_auth'
    with pytest.raises((CanvasCLIError, RuntimeError)): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    assert checkpoint[TICKET_KEY][0]['attempt'] == int(claimed)
    original_writes = runner.uploads
    clock.update(mono=50, wall=1050)
    runner.mode = 'twice_unknown'
    with pytest.raises(CanvasCLIError):
        prepare(checkpoint=checkpoint, selected_client=CanvasClient(runner, deepcopy(ACCOUNT)))
    assert clock['mono'] == 120 and runner.uploads == original_writes
    assert runner.saved[-1]['upload_checkpoint'][TICKET_KEY] == checkpoint[TICKET_KEY]


def test_slow_second_unknown_shares_first_observation_deadline_instead_of_starting_another_120_seconds(prepared):
    runner, _, _, clock, prepare = prepared
    runner.mode = 'twice_unknown'
    def slow_retry():
        clock['mono'] += 75
        clock['wall'] += 75
    runner.on_retry = slow_retry
    with pytest.raises(CanvasCLIError) as caught: prepare()
    assert caught.value.code == 'cli.upload_state_unknown'
    assert runner.uploads == 2 and clock['mono'] == 120
    assert runner.saved[-1]['upload_checkpoint'][TICKET_KEY][0]['observation_deadline_at'] == 1120


@pytest.mark.parametrize('stage', ['identity', 'probe', 'claim', 'retry_write'])
def test_recovery_window_expiry_retains_original_sdk_unknown_when_task_deadline_is_still_open(prepared, stage):
    runner, _, _, clock, prepare = prepared
    def expire():
        clock.update(mono=120, wall=1120)
    if stage == 'identity': runner.on_identity = expire
    if stage == 'probe': runner.on_probe = expire
    if stage == 'claim':
        def expire_claim(value):
            entries = value.get('upload_checkpoint', {}).get(TICKET_KEY, [])
            if entries and entries[0]['attempt'] == 1: expire()
        runner.on_save = expire_claim
    if stage == 'retry_write': runner.on_retry = expire
    with pytest.raises(CanvasCLIError) as caught: prepare()
    assert caught.value.code == 'cli.upload_state_unknown'
    assert runner.uploads == (2 if stage == 'retry_write' else 1)
    assert runner.saved[-1]['upload_checkpoint'][TICKET_KEY][0]['original_error']['reason'] == 'vod.data_timeout'


def test_real_task_deadline_expiry_keeps_the_existing_task_timeout_contract(prepared):
    runner, _, _, clock, prepare = prepared
    runner.on_probe = lambda: clock.update(mono=500, wall=1500)
    with pytest.raises(CanvasCLIError) as caught: prepare()
    assert caught.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and runner.uploads == 1


def test_legacy_pending_unknown_without_ticket_is_observed_without_a_recovery_write(prepared):
    runner, _, _, clock, prepare = prepared
    runner.mode = 'read_auth'
    with pytest.raises(CanvasCLIError): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    checkpoint.pop(TICKET_KEY)
    runner.mode = 'twice_unknown'
    with pytest.raises(CanvasCLIError): prepare(checkpoint=checkpoint)
    assert runner.uploads == 1 and clock['mono'] == 120
    assert TICKET_KEY not in runner.saved[-1]['upload_checkpoint']


@pytest.mark.parametrize('mutation', ['duplicate', 'foreign_id', 'source', 'binding', 'attempt'])
def test_tampered_ticket_is_rejected_before_any_additional_file_write(prepared, mutation):
    runner, client, _, _, prepare = prepared
    runner.mode = 'read_auth'
    with pytest.raises(CanvasCLIError): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    item = checkpoint[TICKET_KEY][0]
    if mutation == 'duplicate': checkpoint[TICKET_KEY].append(deepcopy(item))
    if mutation == 'foreign_id': item['resource_id'] = 'other-resource'
    if mutation == 'source': item['source_sha256'] = '0' * 64
    if mutation == 'binding': item['binding_sha256'] = '0' * 64
    if mutation == 'attempt': item['attempt'] = 2
    with pytest.raises(CanvasCLIError) as caught: prepare(checkpoint=checkpoint)
    assert caught.value.code == 'CLI_UPLOAD_CHECKPOINT_INVALID' and runner.uploads == 1


@pytest.mark.parametrize('claimed', [False, True])
def test_dangling_observing_or_claimed_ticket_cannot_be_treated_as_a_fresh_upload(prepared, claimed):
    runner, _, _, _, prepare = prepared
    runner.mode = 'crash_retry' if claimed else 'read_auth'
    with pytest.raises((CanvasCLIError, RuntimeError)): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    checkpoint['pending_refs'] = []
    checkpoint['refs'] = []
    previous_writes = runner.uploads
    with pytest.raises(CanvasCLIError) as caught: prepare(checkpoint=checkpoint)
    assert caught.value.code == 'CLI_UPLOAD_CHECKPOINT_INVALID' and runner.uploads == previous_writes


def test_without_durable_persistence_only_original_id_observation_is_allowed(prepared):
    runner, _, _, _, prepare = prepared
    runner.mode = 'processing'
    prepare(persist=False)
    assert runner.uploads == 1 and runner.saved == []
