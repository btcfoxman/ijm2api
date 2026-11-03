"""Upload admission metrics describe actual waits without changing limits."""
import json
import threading
import time

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_timing import PreparationTiming
from app.canvas_upload_limiter import UploadLimiter


def test_wait_is_measured_before_entry_not_during_processing():
    limiter = UploadLimiter()
    timing = PreparationTiming('local-test')
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with limiter.slot('fixture', 1, account_limit=1, total_limit=1):
            acquired.set()
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(1)
    timer = threading.Timer(.1, release.set)
    timer.start()
    try:
        with limiter.slot('fixture', 2, account_limit=1, total_limit=1,
                deadline=time.monotonic() + 1, on_wait=lambda seconds, admitted:
                timing.record_upload_admission(seconds, admitted, index=2, media_type='image')):
            first = timing.snapshot()['upload_admission']
            time.sleep(.03)
            assert timing.snapshot()['upload_admission'] == first
        assert first['count'] == 1 and first['failed'] == 0
        assert first['max_wait_ms'] >= 30
        assert first['recent'][0]['index'] == 2
        assert limiter._totals == limiter._accounts == {}
    finally:
        release.set()
        timer.cancel()
        thread.join(1)


@pytest.mark.parametrize('cancelled', [False, True])
def test_failed_admission_preserves_original_error_and_never_acquires(cancelled):
    limiter = UploadLimiter()
    observed = []
    original = 'CLI_TASK_TERMINATED' if cancelled else 'CLI_TASK_DEADLINE_EXCEEDED'
    with pytest.raises(CanvasCLIError) as error:
        with limiter.slot('fixture', 1, account_limit=1, total_limit=1,
                deadline=5, clock=lambda: 6, stopped=lambda: cancelled,
                on_wait=lambda seconds, admitted: observed.append((seconds, admitted))):
            pytest.fail('Rejected upload entered the slot')
    assert error.value.code == original and observed == [(0, False)]
    assert limiter._totals == limiter._accounts == {}


def test_broken_observer_does_not_leak_slot_or_change_success():
    limiter = UploadLimiter()

    def observer(*unused):
        raise RuntimeError('fixture listener failure')

    for _ in range(2):
        with limiter.slot('fixture', 1, account_limit=1, total_limit=1, on_wait=observer):
            assert limiter._totals == {'fixture': 1}
    assert limiter._totals == limiter._accounts == {}


def test_observer_can_read_timing_and_access_limiter_without_lock_inversion():
    limiter = UploadLimiter()
    finished = threading.Event()

    def observer(seconds, admitted):
        def other_account():
            with limiter.slot('fixture', 2, account_limit=1, total_limit=2):
                finished.set()
        thread = threading.Thread(target=other_account)
        thread.start()
        assert finished.wait(1), 'Observer was called while holding limiter condition'
        thread.join(1)

    with limiter.slot('fixture', 1, account_limit=1, total_limit=2, on_wait=observer):
        assert finished.is_set()


def test_metrics_are_bounded_sanitized_and_do_not_change_complete_command_audit():
    timing = PreparationTiming('task')
    for index in range(100):
        timing.record_upload_admission(.125, index % 2 == 0, index=index + 1,
            media_type='private-user:private-password@private-media-url')
    snapshot = timing.snapshot()
    metrics = snapshot['upload_admission']
    assert metrics['count'] == 100 and metrics['failed'] == 50
    assert len(metrics['recent']) == 32 and metrics['dropped_samples'] == 68
    assert metrics['total_wait_ms'] == 12500 and metrics['max_wait_ms'] == 125
    assert snapshot['command_count'] == snapshot['dropped_command_samples'] == 0
    assert 'private-' not in json.dumps(snapshot)
    metrics['recent'].clear()
    assert len(timing.snapshot()['upload_admission']['recent']) == 32
    timing.finish()
    before = timing.snapshot()
    timing.record_upload_admission(1, True)
    assert timing.snapshot() == before


@pytest.mark.parametrize('seconds,admitted', [(True, True), (float('nan'), True),
    (float('inf'), True), (-1, True), (86401, True), (.1, 1), (None, False)])
def test_invalid_metrics_are_not_persisted(seconds, admitted):
    timing = PreparationTiming('task')
    timing.record_upload_admission(seconds, admitted)
    assert 'upload_admission' not in timing.snapshot()
