"""Admin-only identifiers, keeping local Canvas submissions separate from outputs."""
from __future__ import annotations

from typing import Any


def _identifier(value: Any) -> str:
    if not isinstance(value, str):
        return ''
    value = value.strip()
    return value if value and len(value) <= 200 and all(c.isalnum() or c in '._:-' for c in value) else ''


def upstream_identifiers(task: dict[str, Any]) -> dict[str, Any]:
    """Never label the locally allocated Canvas submit UUID as a provider job."""
    if task.get('execution_backend') != 'canvas_cli':
        return {'task_id': _identifier(task.get('run_id')), 'submission_id': '', 'resource_ids': []}
    context = task.get('upstream_context')
    context = context if isinstance(context, dict) else {}
    values = context.get('resource_ids')
    resources = list(dict.fromkeys(filter(None, map(_identifier, values)))) if isinstance(values, list) else []
    return {'task_id': '', 'submission_id': _identifier(context.get('submit_id')), 'resource_ids': resources}
