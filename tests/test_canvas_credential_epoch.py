"""Credential writer changes are visible between concurrent read groups."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from app.canvas_account_lane import AccountLane
from app.canvas_cli import CanvasCLI


def test_epoch_changes_only_when_a_new_writer_enters():
    lane = AccountLane()
    stopped = threading.Event()
    assert lane.credential_epoch == 0
    lane.acquire(exclusive=False, deadline=None, stopped=stopped)
    lane.acquire(exclusive=False, deadline=None, stopped=stopped)
    assert lane.credential_epoch == 0
    lane.release(exclusive=False)
    lane.release(exclusive=False)
    lane.acquire(exclusive=True, deadline=None, stopped=stopped)
    assert lane.credential_epoch == 1
    lane.acquire(exclusive=True, deadline=None, stopped=stopped)
    lane.acquire(exclusive=False, deadline=None, stopped=stopped)
    assert lane.credential_epoch == 1
    lane.release(exclusive=False)
    lane.release(exclusive=True)
    lane.release(exclusive=True)
    lane.acquire(exclusive=True, deadline=None, stopped=stopped)
    assert lane.credential_epoch == 2
    lane.release(exclusive=True)


def test_a_queued_writer_does_not_change_an_active_reader_epoch():
    lane = AccountLane()
    stopped = threading.Event()
    queued, entered, release = threading.Event(), threading.Event(), threading.Event()
    lane.acquire(exclusive=False, deadline=None, stopped=stopped)

    def writer():
        queued.set()
        lane.acquire(exclusive=True, deadline=time.monotonic() + 2, stopped=stopped)
        try:
            entered.set()
            assert release.wait(2)
        finally:
            lane.release(exclusive=True)

    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(writer)
            assert queued.wait(1)
            assert lane.credential_epoch == 0
            lane.release(exclusive=False)
            assert entered.wait(1)
            assert lane.credential_epoch == 1
            release.set()
            future.result(1)
    finally:
        release.set()


def test_timed_out_or_stopped_writers_do_not_change_epoch():
    lane = AccountLane()
    stopped = threading.Event()
    lane.acquire(exclusive=False, deadline=None, stopped=stopped)
    try:
        with ThreadPoolExecutor(1) as pool:
            expired = pool.submit(lane.acquire, exclusive=True,
                                  deadline=time.monotonic() - 1, stopped=stopped)
            with pytest.raises(TimeoutError):
                expired.result(1)
        assert lane.credential_epoch == 0
    finally:
        lane.release(exclusive=False)
    stopped.set()
    with pytest.raises(InterruptedError):
        lane.acquire(exclusive=True, deadline=None, stopped=stopped)
    assert lane.credential_epoch == 0


def test_cli_epoch_is_account_scoped_and_does_not_create_storage(tmp_path):
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    one, two = {'id': 19}, {'id': 57}
    try:
        assert runner.credential_epoch(one) == runner.credential_epoch(two) == 0
        assert list(tmp_path.iterdir()) == []
        with runner.account_guard(one, exclusive=True):
            assert runner.credential_epoch(one) == 1
            assert runner.credential_epoch(two) == 0
            with runner.account_guard(one, exclusive=True):
                assert runner.credential_epoch(one) == 1
        with runner.account_guard(one):
            assert runner.credential_epoch(one) == 1
        with runner.account_guard(two, exclusive=True):
            assert runner.credential_epoch(two) == 1
        assert list(tmp_path.iterdir()) == []
    finally:
        runner.close()
