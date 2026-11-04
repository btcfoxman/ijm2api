"""URL-bound inputs retain one durable FILE recovery, never a generation retry."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_input_recovery import TICKET_KEY
from test_canvas_file_unknown_recovery import missing
from test_canvas_transport import ACCOUNT, FakeRunner, request


URL = 'https://fixture.example/original.png?signature=never-persist'


def unavailable():
    return CanvasCLIError('service.1', 'Temporary service failure', exit_code=21, details={'error': {
        'code': 'service.1', 'serviceCode': 1, 'class': 'retryable_failure',
        'retryable': True, 'requiredAction': 'retry'}})


def unknown(**changes):
    # Production's exact typed failure has no reason field. The safety gate is
    # the registration code and fresh binding/absence proof, not message text.
    facts = {'code': 'cli.upload_state_unknown', 'class': 'human_intervention',
             'requiredAction': 'human_intervention', 'retryable': False, **changes}
    return CanvasCLIError('cli.upload_state_unknown', 'Original registration uncertain',
                          exit_code=22, details={'error': facts})


@pytest.fixture
def prepared_url(tmp_path, monkeypatch):
    clock = {'mono': 0.0, 'wall': 1000.0}
    def sleep(seconds):
        clock['mono'] += seconds
        clock['wall'] += seconds
    monkeypatch.setattr('app.canvas_client.time', SimpleNamespace(
        monotonic=lambda: clock['mono'], time=lambda: clock['wall'], sleep=sleep))
    path = tmp_path / 'fallback.png'
    path.write_bytes(b'original-fallback-bytes')

    class Runner(FakeRunner):
        def __init__(self):
            super().__init__(tmp_path)
            self.persisted.append({})
            self.saved = []
            self.epoch = 0
            self.profile_name = 'original-profile'
            self.url_writes = 0
            self.file_writes = 0
            self.downloads = 0
            self.mode = 'recover'
            self.first_error = unknown
            self.probe_error = missing
            self.on_save = lambda value: None
            self.on_probe = lambda: None
            self.on_identity = lambda: None
            self.on_retry = lambda: None

        def credential_epoch(self, account): return self.epoch
        def profile(self, account): return self.profile_name
        def persist(self, value):
            self.on_save(value)
            self.saved.append(deepcopy(value))
            return True
        def fallback(self):
            self.downloads += 1
            return path
        def run(self, account, args, **kwargs):
            assert args[:2] not in (['node', 'run'], ['node', 'confirm'])
            if args[:2] == ['auth', 'account'] and self.file_writes:
                self.on_identity()
            if args[:2] == ['resource', 'upload']:
                self.calls.append(list(args))
                if '--source-url' in args:
                    self.url_writes += 1
                    if self.mode == 'url_unknown': raise unknown()
                    raise unavailable()
                self.file_writes += 1
                if self.mode == 'ack': return {'ok': True, 'data': {'resourceId': args[-1]}}
                if self.mode == 'file_transient': raise unavailable()
                if self.file_writes == 1: raise self.first_error()
                ticket = self.saved[-1]['upload_checkpoint'][TICKET_KEY][0]
                assert ticket['version'] == 2 and ticket['attempt'] == 1 and ticket['state'] == 'claimed'
                assert ticket['fallback_file_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
                self.on_retry()
                if self.mode == 'twice_unknown': raise unknown()
                if self.mode == 'crash_retry': raise RuntimeError('Synthetic crash after durable claim')
                return {'ok': True, 'data': {'resourceId': args[-1]}}
            if args[:2] == ['resource', 'get']:
                self.calls.append(list(args))
                if self.mode == 'file_transient': raise missing(args[2])
                if not self.file_writes:
                    if self.mode == 'unacknowledged': raise unavailable()
                    raise missing(args[2])
                if self.file_writes == 1:
                    self.on_probe()
                    if self.mode == 'read_auth': raise CanvasCLIError('CLI_AUTH_REQUIRED', 'Explicit auth refusal')
                    if self.mode == 'read_network': raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Read timeout')
                    if self.mode in {'processing', 'ack', 'ready', 'foreign_project'}:
                        value = {'resourceId': args[2], 'type': 'image', 'status':
                            'processing' if self.mode == 'processing' and clock['mono'] < 3 else 'success'}
                        if self.mode == 'foreign_project': value['projectId'] = 'foreign-project'
                        return {'ok': True, 'data': value}
                    raise self.probe_error(args[2])
                if self.mode == 'twice_unknown': raise missing(args[2])
                return {'ok': True, 'data': {'resourceId': args[2], 'type': 'image', 'status': 'success'}}
            return super().run(account, args, **kwargs)

    runner = Runner()
    client = CanvasClient(runner, deepcopy(ACCOUNT))
    body = {**request(), 'images': [URL]}
    assets = {'images': [{'source_url': URL, 'fallback_file': runner.fallback}], 'videos': [], 'audios': []}
    def prepare(*, checkpoint=None, persist=True, selected_client=None):
        return (selected_client or client).prepare(body, 'original-url-task', assets,
            persist=runner.persist if persist else None, checkpoint=checkpoint, deadline=500)
    return runner, client, path, clock, prepare


def test_url_file_unknown_recovers_once_same_original_id_and_retains_both_input_hashes(prepared_url):
    runner, _, path, _, prepare = prepared_url
    result = prepare()
    writes = [args for args in runner.calls if args[:2] == ['resource', 'upload']]
    assert len(writes) == 3 and runner.url_writes == 1 and runner.file_writes == 2 and runner.downloads == 1
    assert '--source-url' in writes[0] and '--file' in writes[1] and writes[1] == writes[2]
    assert len({args[-1] for args in writes}) == 1
    ticket = result['upload_checkpoint'][TICKET_KEY][0]
    assert ticket['version'] == 2 and ticket['attempt'] == 1
    assert ticket['source_sha256'] == hashlib.sha256(URL.encode()).hexdigest()
    assert ticket['fallback_file_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert ticket['original_error'] == {'code': 'cli.upload_state_unknown', 'class': 'human_intervention',
                                       'requiredAction': 'human_intervention'}
    ref = result['upload_checkpoint']['refs'][0]
    assert ref['source_kind'] == 'url' and ref['source_sha256'] == ticket['source_sha256']
    assert URL not in repr(result) and str(path) not in repr(result)
    assert result['upload_checkpoint']['pending_refs'] == []
    saved_tickets = [row['upload_checkpoint'][TICKET_KEY][0] for row in runner.saved
                     if row.get('upload_checkpoint', {}).get(TICKET_KEY)]
    assert saved_tickets[0]['attempt'] == 0
    assert len({item['observation_deadline_at'] for item in saved_tickets}) == 1


def test_resumed_unacknowledged_url_has_same_bound_once_file_recovery(prepared_url):
    runner, _, _, _, prepare = prepared_url
    runner.mode = 'unacknowledged'
    with pytest.raises(CanvasCLIError): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    assert checkpoint['pending_refs'][0]['registration'] == 'unacknowledged'
    assert runner.file_writes == 0 and runner.downloads == 0
    runner.mode = 'recover'
    result = prepare(checkpoint=checkpoint)
    assert runner.url_writes == 1 and runner.file_writes == 2 and runner.downloads == 1
    assert result['upload_checkpoint'][TICKET_KEY][0]['version'] == 2
    assert result['upload_checkpoint']['refs'][0]['resource_id'] == checkpoint['pending_refs'][0]['resource_id']


def test_resumed_url_retains_three_total_writes_including_its_original_url_write(prepared_url):
    runner, _, _, _, prepare = prepared_url
    runner.mode = 'unacknowledged'
    with pytest.raises(CanvasCLIError): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    runner.mode = 'file_transient'
    with pytest.raises(CanvasCLIError): prepare(checkpoint=checkpoint)
    assert runner.url_writes == 1 and runner.file_writes == 2 and runner.downloads == 1
    assert TICKET_KEY not in runner.saved[-1]['upload_checkpoint']


@pytest.mark.parametrize('mode', ['processing', 'ready', 'read_auth', 'read_network', 'foreign_project'])
def test_positive_or_failed_absence_proof_never_permits_a_third_write(prepared_url, mode):
    runner, _, _, _, prepare = prepared_url
    runner.mode = mode
    if mode in {'processing', 'ready'}: prepare()
    else:
        with pytest.raises(CanvasCLIError): prepare()
    assert runner.url_writes == 1 and runner.file_writes == 1
    assert runner.saved[-1]['upload_checkpoint'][TICKET_KEY][0]['attempt'] == 0


@pytest.mark.parametrize('change', [
    {'class': 'business_failure'}, {'class': 'invalid_input'}, {'class': 'human_intervention', 'reason': 'content_policy'},
    {'requiredAction': 'login'}, {'requiredAction': 'fix_input'}, {'serviceCode': 20115},
    {'reason': 'unsupported_model'}, {'accepted': True}, {'resources': ['foreign-output']},
])
def test_exact_upload_unknown_does_not_override_policy_auth_business_or_presence(prepared_url, change):
    runner, _, _, _, prepare = prepared_url
    runner.first_error = lambda: unknown(**change)
    with pytest.raises(CanvasCLIError): prepare()
    assert runner.url_writes == 1 and runner.file_writes == 1
    first_file = next(i for i, args in enumerate(runner.calls)
                      if args[:2] == ['resource', 'upload'] and '--file' in args)
    assert not any(args[:2] == ['auth', 'account'] for args in runner.calls[first_file + 1:])


@pytest.mark.parametrize('change', ['bytes', 'profile', 'epoch', 'proxy', 'uid', 'claim_bytes', 'claim_epoch'])
def test_bound_fallback_bytes_identity_and_durable_claim_are_rechecked(prepared_url, change):
    runner, client, path, _, prepare = prepared_url
    def mutate():
        if change == 'bytes': path.write_bytes(b'changed-fallback')
        if change == 'profile': runner.profile_name = 'retired-profile'
        if change == 'epoch': runner.epoch += 1
        if change == 'proxy': client.account['proxy_url'] = 'socks5://foreign.invalid:1'
        if change == 'uid': client.account['login_user_id'] = 'foreign-user'
    runner.on_probe = mutate
    if change.startswith('claim_'):
        def after_claim(value):
            entries = value.get('upload_checkpoint', {}).get(TICKET_KEY, [])
            if entries and entries[0]['attempt'] == 1:
                if change == 'claim_bytes': path.write_bytes(b'changed-after-claim')
                else: runner.epoch += 1
        runner.on_save = after_claim
    with pytest.raises(CanvasCLIError): prepare()
    assert runner.file_writes == 1


@pytest.mark.parametrize('claimed', [False, True])
def test_restart_only_observes_original_ticket_and_never_renews_its_deadline(prepared_url, claimed):
    runner, _, _, clock, prepare = prepared_url
    runner.mode = 'crash_retry' if claimed else 'read_auth'
    with pytest.raises((CanvasCLIError, RuntimeError)): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    before_writes, before_downloads = runner.file_writes, runner.downloads
    ticket = checkpoint[TICKET_KEY][0]
    assert ticket['attempt'] == int(claimed)
    clock.update(mono=50, wall=1050)
    runner.mode = 'twice_unknown'
    with pytest.raises(CanvasCLIError):
        prepare(checkpoint=checkpoint, selected_client=CanvasClient(runner, deepcopy(ACCOUNT)))
    assert runner.file_writes == before_writes and runner.downloads == before_downloads
    assert runner.saved[-1]['upload_checkpoint'][TICKET_KEY] == checkpoint[TICKET_KEY]
    assert clock['wall'] == ticket['observation_deadline_at']


@pytest.mark.parametrize('stage', ['observing', 'claimed'])
def test_url_fallback_requires_durable_ticket_before_recovery_write(prepared_url, stage):
    runner, _, _, _, prepare = prepared_url
    def reject(value):
        entries = value.get('upload_checkpoint', {}).get(TICKET_KEY, [])
        if entries and entries[0]['state'] == stage:
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_FAILED', 'Synthetic persistence failure')
    runner.on_save = reject
    with pytest.raises(CanvasCLIError): prepare()
    assert runner.file_writes == 1


@pytest.mark.parametrize('change', ['file_hash_missing', 'file_hash_format', 'version', 'url_hash', 'binding', 'resource', 'dangling'])
def test_invalid_url_fallback_ticket_never_authorizes_any_new_write(prepared_url, change):
    runner, _, _, _, prepare = prepared_url
    runner.mode = 'read_auth'
    with pytest.raises(CanvasCLIError): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    ticket = checkpoint[TICKET_KEY][0]
    if change == 'file_hash_missing': ticket.pop('fallback_file_sha256')
    if change == 'file_hash_format': ticket['fallback_file_sha256'] = 'not-a-hash'
    if change == 'version': ticket['version'] = 1
    if change == 'url_hash': ticket['source_sha256'] = '0' * 64
    if change == 'binding': ticket['binding_sha256'] = '0' * 64
    if change == 'resource': ticket['resource_id'] = 'foreign-resource'
    if change == 'dangling': checkpoint['pending_refs'] = []
    with pytest.raises(CanvasCLIError) as caught: prepare(checkpoint=checkpoint)
    assert caught.value.code == 'CLI_UPLOAD_CHECKPOINT_INVALID' and runner.file_writes == 1


def test_slow_second_unknown_retains_first_absolute_120_second_window(prepared_url):
    runner, _, _, clock, prepare = prepared_url
    runner.mode = 'twice_unknown'
    def slow_retry():
        clock['mono'] += 75
        clock['wall'] += 75
    runner.on_retry = slow_retry
    with pytest.raises(CanvasCLIError) as caught: prepare()
    assert caught.value.code == 'cli.upload_state_unknown' and runner.file_writes == 2
    ticket = runner.saved[-1]['upload_checkpoint'][TICKET_KEY][0]
    assert clock['wall'] == ticket['observation_deadline_at']


@pytest.mark.parametrize('mode', ['ack', 'url_unknown'])
def test_acknowledged_file_or_unknown_original_url_has_no_extra_recovery_write(prepared_url, mode):
    runner, _, _, _, prepare = prepared_url
    runner.mode = mode
    if mode == 'ack': prepare()
    else:
        with pytest.raises(CanvasCLIError): prepare()
    assert runner.url_writes == 1 and runner.file_writes == (1 if mode == 'ack' else 0)
    assert TICKET_KEY not in runner.saved[-1]['upload_checkpoint']


def test_no_durable_persist_never_retries_unknown_fallback_file(prepared_url):
    runner, _, _, _, prepare = prepared_url
    with pytest.raises(CanvasCLIError): prepare(persist=False)
    assert runner.url_writes == 1 and runner.file_writes == 1 and runner.saved == []


@pytest.mark.parametrize('change', ['flat_business', 'partial_receipt', 'outcome_unknown', 'other_human_code'])
def test_unknown_recovery_is_exact_typed_registration_only(prepared_url, change):
    runner, _, _, _, prepare = prepared_url
    def error():
        value = unknown()
        if change == 'flat_business': value.details = {'class': 'business_failure'}
        if change == 'partial_receipt': value.details['partialData'] = {'accepted': True}
        if change == 'outcome_unknown': value.outcome_unknown = True
        if change == 'other_human_code': value.code = 'cli.human_intervention'
        return value
    runner.first_error = error
    with pytest.raises(CanvasCLIError): prepare()
    assert runner.file_writes == 1


def test_partial_data_on_exact_absence_is_not_permission_to_write_again(prepared_url):
    runner, _, _, _, prepare = prepared_url
    def partial(identifier):
        error = missing(identifier)
        error.details['partialData'] = {'resources': [identifier]}
        return error
    runner.probe_error = partial
    with pytest.raises(CanvasCLIError): prepare()
    assert runner.file_writes == 1


@pytest.mark.parametrize('stage', ['identity', 'probe', 'claim', 'retry_write'])
def test_expired_fallback_observation_preserves_original_error_without_new_window(prepared_url, stage):
    runner, _, _, clock, prepare = prepared_url
    def expire():
        ticket = runner.saved[-1]['upload_checkpoint'][TICKET_KEY][0]
        clock.update(mono=ticket['observation_deadline_at'] - 1000,
                     wall=ticket['observation_deadline_at'])
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
    assert runner.file_writes == (2 if stage == 'retry_write' else 1)


def test_legacy_unknown_url_without_byte_ticket_remains_read_only(prepared_url):
    runner, _, _, _, prepare = prepared_url
    runner.mode = 'read_auth'
    with pytest.raises(CanvasCLIError): prepare()
    checkpoint = deepcopy(runner.saved[-1]['upload_checkpoint'])
    checkpoint.pop(TICKET_KEY)
    downloads = runner.downloads
    runner.mode = 'twice_unknown'
    with pytest.raises(CanvasCLIError): prepare(checkpoint=checkpoint)
    assert runner.file_writes == 1 and runner.downloads == downloads
    assert TICKET_KEY not in runner.saved[-1]['upload_checkpoint']
