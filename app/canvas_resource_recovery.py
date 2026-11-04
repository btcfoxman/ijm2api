"""Observe concrete outputs already returned by the original accepted run.

The operation summary is not the only source of output state. Every read here
uses an original receipt resource ID, account/profile and project. This module
never uploads, creates, confirms, runs, downloads or changes task data.
"""
from __future__ import annotations

import copy
import math
import re
import time
from typing import Any

from .canvas_cli import CanvasCLIError


SOURCE = 'accepted_receipt_resource_get'
_UUID = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
_NODE = re.compile(r'node_[0123456789abcdefghjkmnpqrstvwxyz]{10}\Z')
_FLAGS = frozenset({'accepted', 'output_resources', 'receipt', 'identity_conflict', 'ambiguous_partial'})
_SUCCESS = frozenset({'success', 'succeeded', 'completed'})
_FAILED = frozenset({'failed', 'rejected', 'error', 'canceled', 'cancelled'})
_RUNNING = frozenset({'pending', 'queued', 'accepted', 'running', 'in_progress', 'processing'})
_READ_FAILURES = frozenset({'CLI_COMMAND_TIMEOUT', 'CLI_INVALID_RESPONSE', 'cli.transport_failed',
    'cli.invalid_service_response', 'cli.operation_submission_absent', 'cli.resource_unavailable',
    'service.1', 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'})


def _mismatch() -> CanvasCLIError:
    return CanvasCLIError('CLI_RESOURCE_MISMATCH',
        'Canvas accepted-resource observation changed its original task binding', outcome_unknown=True)


def accepted_output_context(context: Any) -> bool:
    """Positive run facts, not the presence of a UUID or a generic partial bit.

    Older service versions stored ``submitted`` after a successful ``accepted``
    reply. ``ambiguous_partial`` was also set on ordinary accepted replies by
    their receipt projector. Neither legacy detail erases explicit acceptance
    plus a complete output set; neither can grant acceptance by itself.
    """
    if (not isinstance(context, dict) or context.get('backend') != 'canvas_cli'
            or context.get('phase') != 'accepted'
            or context.get('submission_phase') not in {'accepted', 'submitted'}
            or context.get('submit_error') or 'manual_termination' in context
            or context.get('previous_submissions')
            or type(context.get('account_id')) is not int or context['account_id'] < 1
            or not all(isinstance(context.get(key), str) and context[key]
                       for key in ('task_id', 'profile', 'expected_user_id'))
            or context.get('kind') not in {'image', 'video'}
            or any(not isinstance(context.get(key), str) or not _UUID.fullmatch(context[key])
                   for key in ('project_id', 'submit_id'))
            or not isinstance(context.get('node_id'), str) or not _NODE.fullmatch(context['node_id'])):
        return False
    evidence = context.get('submission_evidence')
    if (not isinstance(evidence, dict) or set(evidence) != {'version', *_FLAGS}
            or type(evidence.get('version')) is not int or evidence['version'] != 1
            or any(type(evidence.get(key)) is not bool for key in _FLAGS)
            or not evidence['accepted'] or not evidence['output_resources'] or evidence['identity_conflict']
            or context.get('submission_receipt_evidence') is not True):
        return False
    count, ids = context.get('output_count'), context.get('resource_ids')
    return (type(count) is int and 1 <= count <= 4 and isinstance(ids, list)
        and len(ids) == count
        and all(isinstance(value, str) and _UUID.fullmatch(value) for value in ids)
        and len(set(ids)) == count)


def _eligible_error(error: Any) -> bool:
    if not isinstance(error, CanvasCLIError) or error.code not in _READ_FAILURES:
        return False
    details = error.details if isinstance(error.details, dict) else {}
    facts = details.get('error') if isinstance(details.get('error'), dict) else {}
    if facts.get('requiredAction') not in (None, '', 'none', 'retry', 'resume', 'contact_support'):
        return False
    if facts.get('class') in {'invalid_input', 'permanent_failure', 'business_failure', 'authorization_failure'}:
        return False
    # A transport wrapper must not hide an explicit auth/identity/policy veto.
    for value in (details, facts):
        for key in ('code', 'reason', 'class'):
            item = str(value.get(key) or '').lower()
            if any(word in item for word in ('auth', 'identity', 'mismatch', 'permission', 'forbidden',
                    'policy', 'moderation', 'copyright', 'insufficient', 'invalid_input', 'unsupported')):
                return False
    return True


def resource_observation_retryable(error: Any) -> bool:
    """Classify a read failure only; never authorizes generation replay."""
    return (isinstance(error, CanvasCLIError) and error.code == 'CLI_TASK_DEADLINE_EXCEEDED'
            or _eligible_error(error))


def _optional_bindings(value: dict, context: dict) -> None:
    for field, key in (('projectId', 'project_id'), ('nodeId', 'node_id'), ('submitId', 'submit_id'),
                       ('operationRef', 'submit_id')):
        if value.get(field) not in (None, '', context[key]):
            raise _mismatch()
    if value.get('userId') not in (None, '', context['expected_user_id']):
        raise _mismatch()


def observe_bound_resources(client, context: dict, *, deadline: float | None,
                            operation: dict | None = None, error: CanvasCLIError | None = None) -> dict | None:
    """Read an accepted run's original output set within the caller's deadline.

    A caller can request this directly, after a transient operation read error,
    or while an original operation summary still says running. Explicit failed
    summaries and identity/authentication conflicts are never overridden.
    """
    if not accepted_output_context(context):
        return None
    if error is not None and not _eligible_error(error):
        return None
    context = copy.deepcopy(context)
    if error is not None:
        details = error.details if isinstance(error.details, dict) else {}
        facts = details.get('error') if isinstance(details.get('error'), dict) else {}
        for value in (details, facts):
            _optional_bindings(value, context)
            if value.get('identity_conflict') is True:
                return None
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        return None
    client._remaining(deadline)
    # One to four independent original outputs share one bounded observation.
    # A fixed 15s batch cap could starve the fourth output forever when each
    # normal resource read takes several seconds. The original task deadline
    # remains authoritative; no per-resource or per-retry budget is renewed.
    query_deadline = min(deadline, time.monotonic() + 15 * context['output_count'])
    client._check_context(context, deadline=query_deadline)
    account = client.account
    uids = [str(account.get(key) or '') for key in ('login_user_id', 'cli_user_id')]
    if (account.get('id') != context['account_id'] or not any(uids)
            or any(uid and uid != context['expected_user_id'] for uid in uids)):
        raise _mismatch()
    if operation is not None:
        if not isinstance(operation, dict):
            return None
        _optional_bindings(operation, context)
        state = operation.get('state')
        if not isinstance(state, str) or state.lower().strip() not in _RUNNING:
            return None
        outputs = operation.get('resources')
        if not isinstance(outputs, list):
            return None
        for output in outputs:
            if not isinstance(output, dict):
                return None
            _optional_bindings(output, context)
            if output.get('resourceId') not in context['resource_ids']:
                raise _mismatch()
            if output.get('type') not in (None, '', context['kind']):
                raise _mismatch()
        if operation.get('submissionError'):
            return None
    input_ids = set()
    inputs = context.get('resources')
    if isinstance(inputs, dict):
        for group in ('images', 'videos', 'audios'):
            if isinstance(inputs.get(group), list):
                input_ids.update(value for value in inputs[group] if isinstance(value, str))
    resources, observed = [], {}
    for identifier in context['resource_ids']:
        if identifier in input_ids:
            raise _mismatch()
        client._remaining(query_deadline)
        client._check_context(context, deadline=query_deadline)
        facts = client._call(['resource', 'get', identifier, '--project-id', context['project_id']],
            timeout=client._remaining(query_deadline, 10), read_only=True, deadline=query_deadline)
        client._remaining(query_deadline)
        if not isinstance(facts, dict):
            return None
        _optional_bindings(facts, context)
        if (facts.get('resourceId') != identifier or facts.get('type') != context['kind']
                or facts.get('source') in {'local_upload', 'external_generated'}):
            raise _mismatch()
        state = facts.get('status')
        state = state.strip().lower() if isinstance(state, str) else ''
        if state not in _SUCCESS | _FAILED | _RUNNING:
            return None
        output = {'resourceId': identifier, 'type': context['kind'], 'state': state,
                  'submitId': context['submit_id']}
        if type(facts.get('errorCode')) in (int, str):
            output['errorCode'] = facts['errorCode']
        resources.append(output)
        observed[identifier] = copy.deepcopy(facts)
    client._remaining(query_deadline)
    state = ('failed' if any(value['state'] in _FAILED for value in resources)
             else 'completed' if all(value['state'] in _SUCCESS for value in resources) else 'running')
    return {'operationRef': context['submit_id'], 'projectId': context['project_id'],
        'nodeId': context['node_id'], 'submitId': context['submit_id'], 'state': state,
        'resources': resources, 'source': SOURCE,
        # Ephemeral local-call facts, never a durable receipt. The client may
        # reuse these only when its local recovery flag proves their origin;
        # similarly named fields in an operation reply must be ignored.
        '_observed_resource_facts': observed,
        'resource_recovery': {'version': 1, 'project_id': context['project_id'],
            'node_id': context['node_id'], 'submit_id': context['submit_id'],
            'resource_ids': list(context['resource_ids']),
            'receipt_evidence': copy.deepcopy(context['submission_evidence'])}}
