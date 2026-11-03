"""Process-wide admission for logical uploads, shared by runner data directory.

The service uses one process with threaded workers. Admission tests both limits
under one condition, so a busy account never reserves otherwise usable global
slots. A slot covers upload and all of its resource-read reconciliation.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from .canvas_cli import CanvasCLIError


class UploadLimiter:
    def __init__(self):
        self._condition = threading.Condition()
        self._totals = {}
        self._accounts = {}

    @contextmanager
    def slot(self, namespace, account_id, *, account_limit, total_limit,
             deadline=None, stopped=lambda: False, clock=None, on_wait=None):
        clock = clock or time.monotonic
        key = (namespace, account_id)
        started = clock() if callable(on_wait) else None

        def observed(admitted):
            if started is not None:
                try:
                    on_wait(max(0, clock() - started), admitted)
                except Exception:
                    pass  # Diagnostics cannot change admission or its original error.

        try:
            with self._condition:
                while True:
                    if stopped():
                        raise CanvasCLIError('CLI_TASK_TERMINATED', 'Canvas reference preparation stopped')
                    remaining = None if deadline is None else deadline - clock()
                    if remaining is not None and remaining <= 0:
                        raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas reference upload deadline reached')
                    if self._totals.get(namespace, 0) < total_limit and self._accounts.get(key, 0) < account_limit:
                        self._totals[namespace] = self._totals.get(namespace, 0) + 1
                        self._accounts[key] = self._accounts.get(key, 0) + 1
                        break
                    self._condition.wait(min(0.05, remaining) if remaining is not None else 0.05)
        except BaseException:
            observed(False)  # Outside the limiter condition: callbacks may persist a snapshot.
            raise
        try:
            observed(True)
            yield
        finally:
            with self._condition:
                self._totals[namespace] -= 1
                self._accounts[key] -= 1
                if not self._totals[namespace]:
                    del self._totals[namespace]
                if not self._accounts[key]:
                    del self._accounts[key]
                self._condition.notify_all()


UPLOAD_LIMITER = UploadLimiter()


def runner_namespace(runner):
    directory = getattr(runner, 'data_dir', None)
    if directory is not None:
        from pathlib import Path
        return ('directory', str(Path(directory).resolve()))
    return ('runner', id(runner))
