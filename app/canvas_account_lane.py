"""Account credential exclusion without serializing independent task commands."""
from __future__ import annotations

import threading
import time


class AccountLane:
    """Reentrant readers, exclusive credential writers, cancellable bounded wait.

    A task's confirm/run sequence holds a reader across both commands, so an
    authorization change cannot enter their gap. Different tasks remain readers
    and use their own explicit project/node/submit/token bindings.
    """

    def __init__(self, clock=None):
        self._condition = threading.Condition()
        self._clock = clock or time.monotonic
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._writer_depth = 0
        self._waiting_writers = 0

    def acquire(self, *, exclusive: bool, deadline: float | None, stopped) -> None:
        owner = threading.get_ident()
        with self._condition:
            if exclusive and self._writer != owner and self._readers.get(owner):
                raise RuntimeError('Credential exclusion cannot upgrade an active task sequence')
            if exclusive:
                self._waiting_writers += 1
            try:
                while True:
                    if stopped.is_set():
                        raise InterruptedError('Account transport is stopping')
                    remaining = None if deadline is None else deadline - self._clock()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError('Account lane deadline reached')
                    if exclusive:
                        ready = self._writer == owner or self._writer is None and not self._readers
                    else:
                        ready = self._writer in {None, owner} and (
                            not self._waiting_writers or owner in self._readers or self._writer == owner)
                    if ready:
                        if exclusive:
                            self._writer = owner
                            self._writer_depth += 1
                        else:
                            self._readers[owner] = self._readers.get(owner, 0) + 1
                        return
                    self._condition.wait(min(0.05, remaining) if remaining is not None else 0.05)
            finally:
                if exclusive:
                    self._waiting_writers -= 1
                    self._condition.notify_all()

    def release(self, *, exclusive: bool) -> None:
        owner = threading.get_ident()
        with self._condition:
            if exclusive:
                if self._writer != owner or self._writer_depth <= 0:
                    raise RuntimeError('Credential lane released by another thread')
                self._writer_depth -= 1
                if not self._writer_depth:
                    self._writer = None
            else:
                count = self._readers.get(owner, 0)
                if not count:
                    raise RuntimeError('Task lane released by another thread')
                if count == 1:
                    del self._readers[owner]
                else:
                    self._readers[owner] = count - 1
            self._condition.notify_all()
