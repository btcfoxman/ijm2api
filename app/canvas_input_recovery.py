"""Bounded input-registration tickets; never generation submission identities."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import stat

from .canvas_cli import CanvasCLIError

TICKET_KEY = 'file_unknown_retries'
_FIELDS = {'version', 'resource_id', 'media_type', 'source_sha256', 'binding_sha256',
           'attempt', 'state', 'observation_started_at', 'observation_deadline_at', 'original_error'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def safe_error(error):
    details = getattr(error, 'details', None)
    source = details.get('error') if isinstance(details, dict) else None
    source = source if isinstance(source, dict) else {}
    value = {'code': 'cli.upload_state_unknown'}
    for key in ('class', 'reason', 'requiredAction'):
        item = source.get(key)
        if isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', item):
            value[key] = item
    return value


def validated_tickets(checkpoint, jobs, binding):
    entries = checkpoint.get(TICKET_KEY, [])
    if not isinstance(entries, list):
        raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas input retry tickets are invalid')
    planned = {job['resource_id']: job for job in jobs}
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != _FIELDS:
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas input retry ticket is invalid')
        identifier = item.get('resource_id')
        job = planned.get(identifier) if isinstance(identifier, str) else None
        start, end = item['observation_started_at'], item['observation_deadline_at']
        error = item['original_error']
        registered = any(isinstance(ref, dict) and ref.get('resource_id') == identifier
            and (ref.get('verified') is True or ref.get('observe_only') is True
                 and ref.get('registration') in {'unknown', 'acknowledged'})
            for ref in [*(checkpoint.get('refs') or []), *(checkpoint.get('pending_refs') or [])])
        if (job is None or identifier in seen or job['source_kind'] != 'file'
                or not registered
                or type(item['version']) is not int or item['version'] != 1
                or item['media_type'] != job['media_type'] or item['source_sha256'] != job['source_sha256']
                or item['binding_sha256'] != digest(binding)
                or type(item['attempt']) is not int or item['attempt'] not in {0, 1}
                or item['state'] != ('observing' if item['attempt'] == 0 else 'claimed')
                or type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end) or not 0 < end - start <= 120.000001
                or not isinstance(error, dict) or error.get('code') != 'cli.upload_state_unknown'
                or set(error) - {'code', 'class', 'reason', 'requiredAction'}
                or any(not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value) is None
                       for value in error.values())):
            raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas input retry ticket binding changed')
        seen.add(identifier)
    return copy.deepcopy(entries)


def remaining_deadline(ticket, *, clock, wall_time, deadline=None):
    value = clock() + max(0, ticket['observation_deadline_at'] - wall_time())
    return min(value, deadline) if deadline is not None else value


def file_identity(path, check):
    """Capture path/stat/content together; no locator is persisted in a ticket."""
    try:
        return _file_identity(path, check)
    except (OSError, ValueError):
        raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas recovery input file is unavailable or changed') from None


def _file_identity(path, check):
    check()
    path = Path(path)
    resolved = path.resolve(strict=True)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas recovery requires an unchanged regular input file')
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            check()
            hasher.update(block)
    signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if resolved != path.resolve(strict=True) or signature(before) != signature(path.lstat()):
        raise CanvasCLIError('CLI_UPLOAD_CHECKPOINT_INVALID', 'Canvas recovery input changed while reading')
    check()
    return (str(resolved), *signature(before), hasher.hexdigest())
