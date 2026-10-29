"""Prevent gaps in safe reads without broadening mutation replay behavior."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient


ACCOUNT = {'id': 41, 'login_user_id': 'synthetic-user', 'proxy_url': 'socks5://proxy.test:20001'}
READS = [('auth', 'account'), ('model', 'list'), ('canvas', 'ls'), ('node', 'quote'),
         ('node', 'show'), ('resource', 'get'), ('operation', 'status')]
WRITES = [('canvas', 'create'), ('node', 'create'), ('node', 'confirm'), ('node', 'run'),
          ('resource', 'upload'), ('resource', 'download'), ('auth', 'login'),
          ('auth', 'wait'), ('auth', 'refresh')]


class Runner:
    timeout = 120

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = []

    def run(self, account, args, **kwargs):
        self.calls.append((deepcopy(account), list(args), dict(kwargs)))
        if self.errors:
            raise self.errors.pop(0)
        return {'ok': True, 'data': {'synthetic_response': True}}


@pytest.fixture
def clock(monkeypatch):
    value = {'now': 100.0, 'sleeps': []}

    def sleep(delay):
        value['sleeps'].append(delay)
        value['now'] += delay

    monkeypatch.setattr('app.canvas_client.time', SimpleNamespace(monotonic=lambda: value['now'], sleep=sleep))
    return value


def transport_error(metadata=None, *, code='cli.transport_failed', unknown=False):
    return CanvasCLIError(code, 'Synthetic service transport interruption',
        details={'error': metadata or {}}, outcome_unknown=unknown)


@pytest.mark.parametrize('pair', READS)
def test_exact_read_retries_same_account_and_arguments_with_original_deadline(clock, pair):
    error = transport_error()
    runner = Runner([error, error])
    arguments = [*pair, '--project-id', 'original-project', '--node-id', 'original-node', 'original-reference']
    result = CanvasClient(runner, ACCOUNT)._call(arguments, read_only=True, timeout=10, deadline=120)
    assert result == {'synthetic_response': True}
    assert len(runner.calls) == 3 and clock['sleeps'] == [0.25, 0.5]
    assert all(account == ACCOUNT and args == arguments and options['deadline'] == 120
               for account, args, options in runner.calls)
    assert all(options['timeout'] == 10 for _, _, options in runner.calls)


@pytest.mark.parametrize('pair', WRITES)
@pytest.mark.parametrize('read_only', [False, True])
def test_transport_branch_never_replays_writes_even_if_caller_marks_read_only(clock, pair, read_only):
    runner = Runner([transport_error()])
    with pytest.raises(CanvasCLIError, match='Synthetic service transport interruption'):
        CanvasClient(runner, ACCOUNT)._call([*pair, 'original-id'], read_only=read_only, deadline=120)
    assert len(runner.calls) == 1 and not clock['sleeps']


@pytest.mark.parametrize('metadata', [
    {'retryable': False},
    {'retryable': False, 'class': 'transport_failure', 'reason': 'network_error'},
    {'requiredAction': 'human_intervention'},
    {'requiredAction': 'resume'},
    {'requiredAction': 'login'},
    {'class': 'human_intervention'},
    {'class': 'authentication_failure'},
    {'class': 'business_rejection'},
    {'class': 'operation_incomplete'},
    {'code': 'cli.authentication_required'},
    {'reason': 'identity_mismatch'},
    {'reason': 'insufficient_credits'},
    {'serviceCode': '20114'},
    {'serviceCode': 'upstream_business_failure'},
])
def test_explicit_metadata_rejection_never_enters_new_transport_retry(clock, metadata):
    runner = Runner([transport_error(metadata)])
    with pytest.raises(CanvasCLIError):
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote', '--node-id', 'original-node'],
                                           read_only=True, deadline=120)
    assert len(runner.calls) == 1 and not clock['sleeps']


@pytest.mark.parametrize('pair', READS)
def test_read_without_read_only_opt_in_does_not_retry(clock, pair):
    runner = Runner([transport_error()])
    with pytest.raises(CanvasCLIError):
        CanvasClient(runner, ACCOUNT)._call(list(pair), deadline=120)
    assert len(runner.calls) == 1 and not clock['sleeps']


@pytest.mark.parametrize('code', ['cli.upload_state_unknown', 'cli.authentication_required',
    'CLI_ACCOUNT_MISMATCH', 'cli.operation_submission_absent', 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'])
def test_transport_code_whitelist_does_not_expand_to_other_failures(clock, code):
    runner = Runner([transport_error(code=code)])
    with pytest.raises(CanvasCLIError) as error:
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote'], read_only=True, deadline=120)
    assert error.value.code == code and len(runner.calls) == 1 and not clock['sleeps']


def test_repeated_transport_errors_stop_at_existing_three_attempt_limit(clock):
    original = transport_error()
    runner = Runner([original] * 4)
    with pytest.raises(CanvasCLIError) as error:
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote'], read_only=True, deadline=120)
    assert error.value is original and len(runner.calls) == 3
    assert clock['sleeps'] == [0.25, 0.5]


def test_read_retries_share_absolute_deadline_and_shrink_command_timeout(clock):
    class SlowRunner(Runner):
        def run(self, account, args, **kwargs):
            clock['now'] += min(2, kwargs['timeout'])
            return super().run(account, args, **kwargs)

    runner = SlowRunner([transport_error()] * 5)
    with pytest.raises(CanvasCLIError) as error:
        CanvasClient(runner, ACCOUNT)._call(['resource', 'get', 'original-resource'],
                                           read_only=True, deadline=105)
    # Preserve the original third-attempt failure; the elapsed budget still
    # prevents any fourth command or renewed command timeout.
    assert error.value.code == 'cli.transport_failed'
    assert clock['now'] == 105
    assert [options['timeout'] for _, _, options in runner.calls] == [5, 2.75, 0.25]
    assert all(options['deadline'] == 105 for _, _, options in runner.calls)


def test_expired_deadline_never_starts_cli_or_sleeps(clock):
    runner = Runner([])
    with pytest.raises(CanvasCLIError) as error:
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote'], read_only=True, deadline=100)
    assert error.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and not runner.calls and not clock['sleeps']


def test_backoff_cannot_cross_deadline_and_start_another_read(clock):
    runner = Runner([transport_error()] * 3)
    with pytest.raises(CanvasCLIError) as error:
        CanvasClient(runner, ACCOUNT)._call(['node', 'quote'], read_only=True, deadline=100.1)
    assert error.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and len(runner.calls) == 1
    assert clock['now'] == pytest.approx(100.1) and clock['sleeps'] == pytest.approx([0.1])


def test_paid_unknown_remains_unreplayed_with_original_identity(clock):
    runner = Runner([transport_error(unknown=True)])
    arguments = ['node', 'run', '--project-id', 'original-project', '--node-id', 'original-node',
                 '--submit-id', 'original-submit']
    with pytest.raises(CanvasCLIError) as error:
        CanvasClient(runner, ACCOUNT)._call(arguments, read_only=True, deadline=120)
    assert error.value.outcome_unknown is True
    assert len(runner.calls) == 1 and runner.calls[0][1] == arguments and not clock['sleeps']


def test_retryable_transport_without_reason_can_recover_exact_read(clock):
    runner = Runner([transport_error({'retryable': True, 'class': 'transport_failure'})])
    assert CanvasClient(runner, ACCOUNT)._call(['node', 'quote', '--node-id', 'original-node'],
        read_only=True, deadline=120) == {'synthetic_response': True}
    assert len(runner.calls) == 2 and clock['sleeps'] == [0.25]
