"""Cold identity reads and optional credit reads share one bounded window."""
from types import SimpleNamespace

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from test_canvas_transport import ACCOUNT


IDENTITY = {'userId': 42, 'availableCredits': 100}


@pytest.fixture
def clock(monkeypatch):
    value = {'now': 0.0}
    monkeypatch.setattr('app.canvas_client.time', SimpleNamespace(
        monotonic=lambda: value['now'], sleep=lambda seconds: value.update(now=value['now'] + seconds)))
    return value


class TimedRunner:
    def __init__(self, clock, responses):
        self.clock, self.responses, self.calls = clock, list(responses), []

    def run(self, account, args, *, timeout=None, deadline=None):
        assert args == ['auth', 'account']  # No submission, login or material writes.
        self.calls.append({'args': list(args), 'timeout': timeout, 'deadline': deadline})
        elapsed, response = self.responses.pop(0)
        if elapsed > timeout:
            self.clock['now'] += timeout
            raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Synthetic process timeout')
        self.clock['now'] += elapsed
        if isinstance(response, Exception): raise response
        return {'ok': True, 'data': response}


def test_cold_read_over_ten_seconds_completes_in_one_call_within_thirty_seconds(clock):
    runner = TimedRunner(clock, [(18, IDENTITY)])
    value = CanvasClient(runner, ACCOUNT).identity()
    assert value['user_id'] == '42' and value['available_balance'] == 100
    assert clock['now'] == 18 and len(runner.calls) == 1
    assert runner.calls[0]['timeout'] == runner.calls[0]['deadline'] == 30


def test_one_slow_timeout_ends_at_thirty_seconds_without_three_full_windows(clock):
    runner = TimedRunner(clock, [(100, IDENTITY)])
    with pytest.raises(CanvasCLIError) as found: CanvasClient(runner, ACCOUNT).identity(deadline=3000)
    assert found.value.code == 'CLI_COMMAND_TIMEOUT'
    assert clock['now'] == 30 and len(runner.calls) == 1


def test_quick_transport_retries_consume_the_same_remaining_identity_window(clock):
    timeout = CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Synthetic quick transient')
    runner = TimedRunner(clock, [(1, timeout), (1, timeout), (100, IDENTITY)])
    with pytest.raises(CanvasCLIError) as found: CanvasClient(runner, ACCOUNT).identity(deadline=3000)
    assert found.value.code == 'CLI_COMMAND_TIMEOUT' and clock['now'] == 30
    assert [call['timeout'] for call in runner.calls] == [30, 28.75, 27.25]
    assert {call['deadline'] for call in runner.calls} == {30}


@pytest.mark.parametrize('deadline', [0, 5, 30])
def test_real_task_deadline_is_final_and_is_never_changed_to_account_timeout(clock, deadline):
    runner = TimedRunner(clock, [(100, IDENTITY)])
    with pytest.raises(CanvasCLIError) as found: CanvasClient(runner, ACCOUNT).identity(deadline=deadline)
    assert found.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and clock['now'] == deadline
    assert len(runner.calls) == int(deadline > 0)
    assert all(call['deadline'] == deadline and call['timeout'] <= deadline for call in runner.calls)


def test_optional_credit_read_uses_remaining_window_and_keeps_this_calls_verified_identity(clock):
    runner = TimedRunner(clock, [(20, {'userId': 42, 'isVip': True}), (100, IDENTITY)])
    value = CanvasClient(runner, ACCOUNT).identity(deadline=3000)
    assert value['user_id'] == '42' and value['available_balance'] is None
    assert value['membership'] == {'isVip': True} and clock['now'] == 30
    assert [call['timeout'] for call in runner.calls] == [30, 9.75]
    assert {call['deadline'] for call in runner.calls} == {30}


def test_optional_backoff_cannot_start_a_fresh_credit_window(clock):
    runner = TimedRunner(clock, [(29.9, {'userId': 42})])
    value = CanvasClient(runner, ACCOUNT).identity(deadline=3000)
    assert value['user_id'] == '42' and value['available_balance'] is None
    assert clock['now'] == 30 and len(runner.calls) == 1


def test_optional_credit_read_cannot_hide_real_task_deadline(clock):
    runner = TimedRunner(clock, [(2, {'userId': 42}), (100, IDENTITY)])
    with pytest.raises(CanvasCLIError) as found: CanvasClient(runner, ACCOUNT).identity(deadline=5)
    assert found.value.code == 'CLI_TASK_DEADLINE_EXCEEDED' and clock['now'] == 5
    assert {call['deadline'] for call in runner.calls} == {5}


@pytest.mark.parametrize('error', [
    CanvasCLIError('CLI_AUTH_EXPIRED', 'Expired authorization'),
    CanvasCLIError('cli.service_rejected', 'Business refusal', details={'error': {'serviceCode': 20114}}),
    CanvasCLIError('cli.upload_state_unknown', 'Different human-intervention state',
                   details={'error': {'class': 'human_intervention'}}),
])
@pytest.mark.parametrize('optional', [False, True])
def test_identity_window_does_not_hide_or_retry_explicit_auth_business_or_intervention(clock, error, optional):
    responses = [(1, {'userId': 42})] if optional else []
    runner = TimedRunner(clock, [*responses, (1, error)])
    with pytest.raises(CanvasCLIError) as found: CanvasClient(runner, ACCOUNT).identity(deadline=3000)
    assert found.value is error and len(runner.calls) == 1 + int(optional)


def test_early_deadline_error_not_caused_by_identity_window_is_not_reclassified(clock):
    original = CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Different explicit deadline')
    runner = TimedRunner(clock, [(1, original)])
    with pytest.raises(CanvasCLIError) as found: CanvasClient(runner, ACCOUNT).identity(deadline=3000)
    assert found.value is original and clock['now'] == 1
