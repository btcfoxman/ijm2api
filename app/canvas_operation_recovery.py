"""Read only an original node when its paid submission lookup is absent.

The caller retains its account guard and original observation deadline. This
component never creates, confirms, runs, uploads or downloads anything. A node
snapshot alone is insufficient: each direct output must name the original
submit ID and must have fresh resource facts. Missing association stays absent.
"""
from __future__ import annotations

import copy
import math
import re
from typing import Any

from .canvas_cli import CanvasCLIError


SOURCE = 'node_show_original_submission'
_UUID = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
_NODE = re.compile(r'node_[0123456789abcdefghjkmnpqrstvwxyz]{10}\Z')
_SUCCESS = frozenset({'success', 'succeeded', 'completed'})
_FAILED = frozenset({'failed', 'rejected', 'error', 'canceled', 'cancelled'})
_RUNNING = frozenset({'pending', 'queued', 'running', 'in_progress', 'processing'})


def _mismatch() -> CanvasCLIError:
    return CanvasCLIError('CLI_RESOURCE_MISMATCH',
        'Canvas node recovery did not match the original task binding', outcome_unknown=True)


def _eligible_error(error: Any) -> bool:
    if not isinstance(error, CanvasCLIError) or error.code != 'cli.operation_submission_absent' or error.exit_code != 20:
        return False
    details = error.details if isinstance(error.details, dict) else {}
    facts = details.get('error')
    if (not isinstance(facts, dict) or facts.get('class') != 'operation_incomplete'
            or facts.get('requiredAction') != 'resume'
            or facts.get('code') not in (None, '', error.code)):
        return False
    # Missing raw phase is permitted: the exact read error and durable paid
    # boundary below establish its scope. An explicit conflicting phase is not.
    return all(value in (None, '', 'query', 'querying', 'operation_status')
               for value in (details.get('phase'), facts.get('phase')))


def _paid_dispatch(context: dict[str, Any]) -> bool:
    phase = context.get('submission_phase')
    error = context.get('submit_error')
    error = error if isinstance(error, dict) else {}
    if error.get('dispatch_started') is False:
        return False
    if context.get('phase') == 'accepted' and phase in {'accepted', 'submitted'}:
        return True
    return (phase in {'run_dispatching', 'submitted', 'accepted', 'unknown'}
            and error.get('dispatch_started') is True and error.get('outcome_unknown') is True
            and error.get('phase') == 'run_dispatching')


def _optional_bindings(body: dict[str, Any], context: dict[str, Any]) -> None:
    for field, expected in (('projectId', 'project_id'), ('nodeId', 'node_id'), ('submitId', 'submit_id')):
        if body.get(field) not in (None, '', context[expected]):
            raise _mismatch()


def _state(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip().lower()
    return result if result in _SUCCESS | _FAILED | _RUNNING else None


def _input_resources(context: dict[str, Any]) -> set[str]:
    found = set()
    resources = context.get('resources')
    if isinstance(resources, dict):
        for group in ('images', 'videos', 'audios'):
            if isinstance(resources.get(group), list):
                found.update(item for item in resources[group] if isinstance(item, str))
    uploaded = context.get('uploaded_resources')
    if isinstance(uploaded, list):
        for item in uploaded:
            if isinstance(item, dict):
                found.update(value for key in ('resource_id', 'resourceId')
                             if isinstance(value := item.get(key), str))
    return found


def recover_absent_operation(client, context: dict[str, Any], original_error: CanvasCLIError,
                             *, deadline: float | None) -> dict[str, Any] | None:
    """Return a provenance-labelled original-submission observation or no proof.

    Any explicit identity conflict or read error propagates. No missing/unknown
    association is turned into acceptance, a refund, or permission to resubmit.
    The caller must preserve ``source`` and ``recovery_provenance`` when storing
    an output binding; operationRef is derived from direct node submit evidence,
    not a successful operation-status response.
    """
    if not _eligible_error(original_error) or not isinstance(context, dict) or not _paid_dispatch(context):
        return None
    evidence = context.get('submission_evidence')
    if ('manual_termination' in context or evidence is not None and
            (not isinstance(evidence, dict) or evidence.get('identity_conflict'))):
        return None
    context = copy.deepcopy(context)
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        return None
    client._remaining(deadline)
    client._check_context(context, deadline=deadline)
    account = client.account
    uid = context.get('expected_user_id')
    account_uids = [str(account.get(key) or '') for key in ('login_user_id', 'cli_user_id')]
    if (type(context.get('account_id')) is not int or context['account_id'] != account.get('id')
            or context.get('backend') != 'canvas_cli'
            or not isinstance(context.get('task_id'), str) or not context['task_id']
            or not isinstance(context.get('profile'), str) or not context['profile']
            or not isinstance(uid, str) or not uid or not any(account_uids)
            or any(value and value != uid for value in account_uids)
            or context.get('kind') not in {'image', 'video'}
            or any(not isinstance(context.get(key), str) or not _UUID.fullmatch(context[key])
                   for key in ('project_id', 'submit_id'))
            or not isinstance(context.get('node_id'), str) or not _NODE.fullmatch(context['node_id'])):
        raise _mismatch()
    count = context.get('output_count')
    expected = context.get('resource_ids')
    if (type(count) is not int or not 1 <= count <= 4 or not isinstance(expected, list)
            or any(not isinstance(item, str) or not _UUID.fullmatch(item) for item in expected)
            or len(set(expected)) != len(expected) or expected and len(expected) != count):
        raise _mismatch()
    view = client._call(['node', 'show', '--project-id', context['project_id'], '--node-id', context['node_id']],
                        timeout=client._remaining(deadline, 10), read_only=True, deadline=deadline)
    client._remaining(deadline)
    if not isinstance(view, dict):
        return None
    _optional_bindings(view, context)
    nodes = view.get('nodes')
    if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
        return None
    item = nodes[0]
    _optional_bindings(item, context)
    if item.get('errorCode') not in (None, '', 0) or item.get('errmsg'):
        return None
    node = item.get('node')
    if not isinstance(node, dict):
        return None
    _optional_bindings(node, context)
    if node.get('nodeId') != context['node_id'] or node.get('type') != context['kind']:
        raise _mismatch()
    outputs = node.get('resources')
    if not isinstance(outputs, list) or not outputs:
        return None
    if len(outputs) != count or any(not isinstance(output, dict) for output in outputs):
        raise _mismatch()
    input_ids = _input_resources(context)
    ids = []
    for output in outputs:
        _optional_bindings(output, context)
        resource_id = output.get('resourceId')
        if (not isinstance(resource_id, str) or not _UUID.fullmatch(resource_id)
                or output.get('type') != context['kind'] or resource_id in input_ids
                or output.get('source') in {'local_upload', 'external_generated'}):
            raise _mismatch()
        if output.get('submitId') in (None, ''):
            return None
        # Do not filter a mixed historical list into an apparently complete
        # result. The exposed schema does not label this list as history.
        if output['submitId'] != context['submit_id']:
            raise _mismatch()
        if _state(output.get('status')) is None:
            return None
        ids.append(resource_id)
    if len(set(ids)) != len(ids) or expected and set(ids) != set(expected):
        raise _mismatch()
    if expected:
        order = {resource_id: index for index, resource_id in enumerate(expected)}
        outputs = sorted(outputs, key=lambda output: order[output['resourceId']])
    resources, proofs = [], []
    for output in outputs:
        client._remaining(deadline)
        client._check_context(context, deadline=deadline)
        resource_id = output['resourceId']
        facts = client._call(['resource', 'get', resource_id, '--project-id', context['project_id']],
                             timeout=client._remaining(deadline, 10), read_only=True, deadline=deadline)
        client._remaining(deadline)
        if not isinstance(facts, dict):
            return None
        _optional_bindings(facts, context)
        if (facts.get('resourceId') != resource_id or facts.get('type') != context['kind']
                or facts.get('source') in {'local_upload', 'external_generated'}):
            raise _mismatch()
        status = _state(facts.get('status'))
        if status is None:
            return None
        resource = {'resourceId': resource_id, 'type': context['kind'], 'state': status,
                    'submitId': output['submitId']}
        if type(facts.get('errorCode')) is int:
            resource['errorCode'] = facts['errorCode']
        resources.append(resource)
        proofs.append({'resource_id': resource_id, 'node_submit_id': output['submitId'],
                       'node_type': output['type'], 'node_status': _state(output['status']),
                       'observed_type': facts['type'], 'resource_status': status,
                       'explicit_resource_bindings': {key: facts[key] for key in ('projectId', 'nodeId', 'submitId')
                                                      if facts.get(key) not in (None, '')}})
    state = ('failed' if any(item['state'] in _FAILED for item in resources)
             else 'completed' if all(item['state'] in _SUCCESS for item in resources) else 'running')
    client._remaining(deadline)
    return {'operationRef': context['submit_id'], 'projectId': context['project_id'],
            'nodeId': context['node_id'], 'submitId': context['submit_id'], 'state': state,
            'resources': resources, 'source': SOURCE, 'recovery_source': SOURCE,
            'node_recovery': {'version': 1, 'project_id': context['project_id'],
                'node_id': context['node_id'], 'submit_id': context['submit_id'],
                'resource_ids': [item['resourceId'] for item in resources],
                'resource_submit_ids': {item['resourceId']: item['submitId'] for item in resources}},
            'recovery_provenance': {'version': 1, 'source': SOURCE,
                'operation_ref_origin': 'node.resources[].submitId',
                'project_id': context['project_id'], 'node_id': context['node_id'], 'submit_id': context['submit_id'],
                'resource_ids': [item['resourceId'] for item in resources], 'resource_bindings': proofs}}
