"""Bounded preparation timings with no request text, locators or credentials."""
from __future__ import annotations

import copy
import math
import re
import time
from collections import deque
from contextlib import contextmanager, nullcontext

from .canvas_cli import CanvasCLI, TRANSPORT_DIAGNOSTIC_COMMANDS


_PHASES = frozenset({'identity', 'model_preflight', 'material_prepare', 'project',
    'reference_upload', 'draft_save', 'quote', 'credit_wait', 'confirmation_submit'})
_SAFE_CODE = re.compile(r'[A-Za-z0-9_.-]{1,80}\Z')


class PreparationTiming:
    """One task/attempt owns bounded aggregates and samples.

    Phases may contain commands or nested phases. Their durations overlap and
    must not be added to infer total preparation time; elapsed_ms is the total.
    """

    def __init__(self, task_id, *, queued_at=None, on_change=None, monotonic=None, wall_time=None):
        self._mono = monotonic or time.monotonic
        self._wall = wall_time or time.time
        self.task_id = task_id if isinstance(task_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,120}', task_id) else 'unknown-task'
        self.started_at = self._wall()
        self._started = self._mono()
        self.queued_at = queued_at
        self._ended = None
        self._listener = on_change
        self._deferred = 0
        self._active = []
        self._phase_started = {}
        self._command = None
        self._recent = deque(maxlen=64)
        self._phases = {}
        self._commands = {}
        self._count = 0

    def _ms(self, start):
        return round(max(0, self._mono() - start) * 1000, 3)

    def snapshot(self):
        total = self._ms(self._started) if self._ended is None else self._ended
        queued = self.queued_at
        return copy.deepcopy({'task_id': self.task_id, 'started_at': self.started_at,
            'finished': self._ended is not None, 'elapsed_ms': total,
            'queue_wait_ms': round(max(0, self.started_at - queued) * 1000, 3)
                if isinstance(queued, (int, float)) and not isinstance(queued, bool) and math.isfinite(queued) else None,
            'phases': self._phases, 'commands': self._commands,
            'current_phase': self._active[-1] if self._active else None,
            'current_command': self._command, 'command_count': self._count,
            'dropped_command_samples': max(0, self._count - len(self._recent)),
            'recent_commands': list(self._recent)})

    def _changed(self):
        if self._listener is not None and not self._deferred:
            try:
                self._listener(self.snapshot())
            except Exception:
                # Telemetry must never erase an accepted receipt or alter retry policy.
                pass

    @contextmanager
    def defer_notifications(self):
        """Piggyback on existing submit persistence, outside its critical section."""
        self._deferred += 1
        try:
            yield
        finally:
            self._deferred -= 1
            if not self._deferred:
                self._changed()

    def begin_phase(self, name, *, index=None, media_type=None):
        if name not in _PHASES:
            raise ValueError('Unknown preparation timing phase')
        marker = {'name': name, 'started_at': self._wall()}
        if isinstance(index, int) and not isinstance(index, bool) and 1 <= index <= 10000:
            marker['index'] = index
        if media_type in {'image', 'video', 'audio'}:
            marker['media_type'] = media_type
        self._active.append(marker)
        self._phase_started[id(marker)] = self._mono()
        self._changed()
        return marker

    def end_phase(self, marker, succeeded=True):
        started = self._phase_started.pop(id(marker), None)
        if started is None:
            return
        name = marker['name']
        entry = self._phases.setdefault(name, {'count': 0, 'elapsed_ms': 0.0, 'failed': 0})
        entry['count'] += 1
        entry['elapsed_ms'] = round(entry['elapsed_ms'] + self._ms(started), 3)
        entry['failed'] += int(not succeeded)
        entry['last_completed_at'] = self._wall()
        self._active = [item for item in self._active if item is not marker]
        self._changed()

    @contextmanager
    def phase(self, name, *, index=None, media_type=None):
        marker = self.begin_phase(name, index=index, media_type=media_type)
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            self.end_phase(marker, succeeded)

    def command(self, runner, account, args, kwargs, attempt, invoke):
        if self._ended is not None:
            return invoke()
        pair = tuple(args[:2])
        safe_pair = list(pair) if pair in TRANSPORT_DIAGNOSTIC_COMMANDS else ['unknown']
        started = self._mono()
        entry = {'command_pair': safe_pair, 'attempt': attempt,
                 'started_at': self._wall(), 'lock_wait_ms': None, 'execution_ms': None}
        if self._active:
            entry['phase'] = self._active[-1]['name']
            for field in ('index', 'media_type'):
                if field in self._active[-1]:
                    entry[field] = self._active[-1][field]
        self._command = entry
        self._changed()
        # Custom/test runners may expose a guard without actually taking that
        # lock in run(). Their lock wait is unknown, rather than an invented 0.
        guard = runner.account_guard if isinstance(runner, CanvasCLI) else None
        execution_started = None
        try:
            manager = guard(account, deadline=kwargs.get('deadline')) if callable(guard) else nullcontext()
            with manager:
                if callable(guard):
                    entry['lock_wait_ms'] = self._ms(started)
                execution_started = self._mono()
                result = invoke()
            entry['outcome'] = 'success'
            return result
        except BaseException as error:
            entry['outcome'] = 'failed'
            code = str(getattr(error, 'code', type(error).__name__))
            entry['error_code'] = code if _SAFE_CODE.fullmatch(code) else 'Error'
            raise
        finally:
            if execution_started is not None:
                entry['execution_ms'] = self._ms(execution_started)
            entry['elapsed_ms'] = self._ms(started)
            name = ' '.join(safe_pair)
            aggregate = self._commands.setdefault(name, {'count': 0, 'elapsed_ms': 0.0,
                'known_lock_wait_ms': 0.0, 'unknown_lock_wait_count': 0, 'failed': 0})
            aggregate['count'] += 1
            aggregate['elapsed_ms'] = round(aggregate['elapsed_ms'] + entry['elapsed_ms'], 3)
            if entry['lock_wait_ms'] is None:
                aggregate['unknown_lock_wait_count'] += 1
            else:
                aggregate['known_lock_wait_ms'] = round(aggregate['known_lock_wait_ms'] + entry['lock_wait_ms'], 3)
            aggregate['failed'] += int(entry['outcome'] != 'success')
            self._count += 1
            self._recent.append(entry)
            self._command = None
            self._changed()

    def finish(self):
        if self._ended is None:
            while self._active:
                self.end_phase(self._active[-1], False)
            self._ended = self._ms(self._started)
            self._changed()
