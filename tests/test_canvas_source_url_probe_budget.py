"""A fresh original-ID absence lookup owns one clipped 15 second window."""
from unittest.mock import Mock

import pytest

from app.canvas_cli import CanvasCLIError
from test_canvas_preparation_transient import unavailable
from test_canvas_resource_not_found import not_found
from test_canvas_source_url_fallback import Runner, invoke
from test_canvas_upload_recovery import clock, ready


def slow_probe(runner, clock, *, seconds, every=False):
    original = runner.run
    calls = []
    def run(account, args, **kwargs):
        if args[:2] == ['resource', 'get'] and (every or not calls):
            calls.append(dict(kwargs))
            allowed = kwargs['timeout']
            clock['now'] += min(seconds, allowed)
            if seconds > allowed:
                runner.calls.append((list(args), kwargs))
                raise CanvasCLIError('CLI_COMMAND_TIMEOUT', 'Synthetic bounded original lookup')
        return original(account, args, **kwargs)
    runner.run = run
    return calls


@pytest.mark.parametrize('seconds', [6, 8])
def test_slow_exact_absence_can_enable_same_id_file_fallback(tmp_path, clock, seconds):
    path = tmp_path / 'original.png'; path.write_bytes(b'original-input')
    callback = Mock(return_value=path)
    runner = Runner([unavailable(), {'resourceId': 'original-resource'}], [not_found(), ready()])
    probes = slow_probe(runner, clock, seconds=seconds)
    invoke(runner, callback)
    writes = [args for args, _ in runner.calls if args[:2] == ['resource', 'upload']]
    assert len(writes) == 2 and '--source-url' in writes[0] and '--file' in writes[1]
    assert writes[0][-1] == writes[1][-1] == 'original-resource' and callback.call_count == 1
    assert len(probes) == 1 and probes[0]['deadline'] == probes[0]['timeout'] == 15
    assert clock['now'] == seconds + 0.5


@pytest.mark.parametrize('deadline,cutoff', [(180, 15), (4, 4)])
def test_probe_window_or_real_preparation_deadline_expiry_cannot_enable_file_write(clock, deadline, cutoff):
    callback = Mock()
    runner = Runner([unavailable()], [not_found()])
    probes = slow_probe(runner, clock, seconds=100)
    dispositions = []
    with pytest.raises(CanvasCLIError): invoke(runner, callback, deadline=deadline, registered=dispositions.append)
    assert clock['now'] == cutoff and probes[0]['deadline'] == probes[0]['timeout'] == cutoff
    assert not callback.called and dispositions == ['unacknowledged']
    writes = [args for args, _ in runner.calls if args[:2] == ['resource', 'upload']]
    assert len(writes) == 1 and '--source-url' in writes[0]


def test_slow_positive_processing_registration_only_observes_without_file_or_repeat_write(clock):
    callback = Mock()
    runner = Runner([unavailable()], [{**ready(), 'status': 'processing'}, ready()])
    slow_probe(runner, clock, seconds=8)
    dispositions = []
    invoke(runner, callback, registered=dispositions.append)
    assert not callback.called and dispositions == ['acknowledged']
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


def test_transient_failed_reads_share_one_probe_deadline_and_do_not_imply_absence(clock):
    callback = Mock()
    runner = Runner([unavailable()], [unavailable()])
    probes = slow_probe(runner, clock, seconds=6, every=True)
    dispositions = []
    with pytest.raises(CanvasCLIError): invoke(runner, callback, registered=dispositions.append)
    assert clock['now'] == 15 and len(probes) == 3
    assert {item['deadline'] for item in probes} == {15}
    assert [item['timeout'] for item in probes] == [15, 8.75, 2.25]
    assert not callback.called and dispositions == ['unacknowledged']
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1


def test_slow_foreign_registration_is_not_adopted_or_replaced(clock):
    callback = Mock()
    runner = Runner([unavailable()], [ready('foreign-resource')])
    slow_probe(runner, clock, seconds=8)
    with pytest.raises(CanvasCLIError) as found: invoke(runner, callback)
    assert found.value.code == 'CLI_RESOURCE_MISMATCH' and not callback.called
    assert sum(args[:2] == ['resource', 'upload'] for args, _ in runner.calls) == 1
