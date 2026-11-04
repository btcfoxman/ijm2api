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
from typing import Any, Callable

from .canvas_cli import CanvasCLIError


SOURCE = 'node_show_original_submission'
_UUID = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
_NODE = re.compile(r'node_[0123456789abcdefghjkmnpqrstvwxyz]{10}\Z')
_SUCCESS = frozenset({'success', 'succeeded', 'completed'})
_FAILED = frozenset({'failed', 'rejected', 'error', 'canceled', 'cancelled'})
_RUNNING = frozenset({'pending', 'queued', 'running', 'in_progress', 'processing'})
_DIAGNOSTIC_REASONS = frozenset({'ineligible_error', 'not_dispatched', 'blocked', 'invalid_deadline',
    'binding_mismatch', 'node_read_failed', 'view_not_object', 'nodes_missing', 'node_count_invalid',
    'node_rejected', 'node_missing', 'outputs_missing', 'outputs_empty', 'output_count_invalid',
    'submit_id_missing', 'output_state_unknown', 'resource_read_failed', 'resource_not_object',
    'resource_state_unknown', 'recovered'})
_DIAGNOSTIC_BOOLEANS = frozenset({'project_binding_matches', 'node_binding_matches',
    'submit_binding_matches', 'node_id_matches', 'node_type_matches', 'submit_ids_present',
    'submit_ids_match', 'output_ids_valid', 'resource_id_matches', 'resource_type_matches'})


def safe_recovery_diagnostic(value: Any) -> dict[str, Any]:
    """Only a fixed schema, counts and comparison results may reach task storage."""
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or not isinstance(value.get('reason'), str) or value['reason'] not in _DIAGNOSTIC_REASONS):
        return {}
    result = {'version': 1, 'reason': value['reason']}
    for key in ('node_count', 'output_count', 'verified_resource_count'):
        if type(value.get(key)) is int and 0 <= value[key] <= 10000:
            result[key] = value[key]
    result.update({key: value[key] for key in _DIAGNOSTIC_BOOLEANS if type(value.get(key)) is bool})
    return result


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
                             *, deadline: float | None,
                             on_diagnostic: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any] | None:
    """Return a provenance-labelled original-submission observation or no proof.

    Any explicit identity conflict or read error propagates. No missing/unknown
    association is turned into acceptance, a refund, or permission to resubmit.
    The caller must preserve ``source`` and ``recovery_provenance`` when storing
    an output binding; operationRef is derived from direct node submit evidence,
    not a successful operation-status response.
    """
    diagnostic = {'version': 1}

    def record(reason: str, **values) -> None:
        diagnostic.update(values, reason=reason)
        if on_diagnostic is not None:
            on_diagnostic(safe_recovery_diagnostic(diagnostic))

    def mismatch() -> None:
        record('binding_mismatch')
        raise _mismatch()

    def bindings(body: dict[str, Any]) -> None:
        for field, expected, flag in (('projectId', 'project_id', 'project_binding_matches'),
                ('nodeId', 'node_id', 'node_binding_matches'), ('submitId', 'submit_id', 'submit_binding_matches')):
            if body.get(field) in (None, ''):
                continue  # Optional absence is not a positive association fact.
            matches = body[field] == context[expected]
            diagnostic[flag] = diagnostic.get(flag, True) and matches
            if not matches:
                mismatch()

    if not _eligible_error(original_error):
        record('ineligible_error')
        return None
    if not isinstance(context, dict) or not _paid_dispatch(context):
        record('not_dispatched')
        return None
    evidence = context.get('submission_evidence')
    if ('manual_termination' in context or evidence is not None and
            (not isinstance(evidence, dict) or evidence.get('identity_conflict'))):
        record('blocked')
        return None
    context = copy.deepcopy(context)
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        record('invalid_deadline')
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
        mismatch()
    count = context.get('output_count')
    expected = context.get('resource_ids')
    if (type(count) is not int or not 1 <= count <= 4 or not isinstance(expected, list)
            or any(not isinstance(item, str) or not _UUID.fullmatch(item) for item in expected)
            or len(set(expected)) != len(expected) or expected and len(expected) != count):
        mismatch()
    try:
        view = client._call(['node', 'show', '--project-id', context['project_id'], '--node-id', context['node_id']],
                            timeout=client._remaining(deadline, 10), read_only=True, deadline=deadline)
    except CanvasCLIError:
        record('node_read_failed')
        raise
    client._remaining(deadline)
    if not isinstance(view, dict):
        record('view_not_object')
        return None
    bindings(view)
    nodes = view.get('nodes')
    if not isinstance(nodes, list):
        record('nodes_missing')
        return None
    diagnostic['node_count'] = len(nodes)
    if len(nodes) != 1 or not isinstance(nodes[0], dict):
        record('node_count_invalid')
        return None
    item = nodes[0]
    bindings(item)
    if item.get('errorCode') not in (None, '', 0) or item.get('errmsg'):
        record('node_rejected')
        return None
    node = item.get('node')
    if not isinstance(node, dict):
        record('node_missing')
        return None
    bindings(node)
    diagnostic.update(node_id_matches=node.get('nodeId') == context['node_id'],
                      node_type_matches=node.get('type') == context['kind'])
    if node.get('nodeId') != context['node_id'] or node.get('type') != context['kind']:
        mismatch()
    outputs = node.get('resources')
    if not isinstance(outputs, list):
        record('outputs_missing')
        return None
    diagnostic['output_count'] = len(outputs)
    if not outputs:
        record('outputs_empty')
        return None
    if len(outputs) != count or any(not isinstance(output, dict) for output in outputs):
        record('output_count_invalid')
        raise _mismatch()
    input_ids = _input_resources(context)
    ids = []
    for output in outputs:
        bindings(output)
        resource_id = output.get('resourceId')
        if (not isinstance(resource_id, str) or not _UUID.fullmatch(resource_id)
                or output.get('type') != context['kind'] or resource_id in input_ids
                or output.get('source') in {'local_upload', 'external_generated'}):
            diagnostic['output_ids_valid'] = False
            mismatch()
        diagnostic['output_ids_valid'] = True
        if output.get('submitId') in (None, ''):
            record('submit_id_missing', submit_ids_present=False)
            return None
        diagnostic['submit_ids_present'] = True
        # Do not filter a mixed historical list into an apparently complete
        # result. The exposed schema does not label this list as history.
        if output['submitId'] != context['submit_id']:
            diagnostic['submit_ids_match'] = False
            mismatch()
        diagnostic['submit_ids_match'] = True
        if _state(output.get('status')) is None:
            record('output_state_unknown')
            return None
        ids.append(resource_id)
    if len(set(ids)) != len(ids) or expected and set(ids) != set(expected):
        diagnostic['output_ids_valid'] = False
        mismatch()
    if expected:
        order = {resource_id: index for index, resource_id in enumerate(expected)}
        outputs = sorted(outputs, key=lambda output: order[output['resourceId']])
    resources, proofs = [], []
    for output in outputs:
        client._remaining(deadline)
        client._check_context(context, deadline=deadline)
        resource_id = output['resourceId']
        try:
            facts = client._call(['resource', 'get', resource_id, '--project-id', context['project_id']],
                                 timeout=client._remaining(deadline, 10), read_only=True, deadline=deadline)
        except CanvasCLIError:
            record('resource_read_failed', verified_resource_count=len(resources))
            raise
        client._remaining(deadline)
        if not isinstance(facts, dict):
            record('resource_not_object', verified_resource_count=len(resources))
            return None
        bindings(facts)
        diagnostic.update(resource_id_matches=facts.get('resourceId') == resource_id,
                          resource_type_matches=facts.get('type') == context['kind'])
        if (facts.get('resourceId') != resource_id or facts.get('type') != context['kind']
                or facts.get('source') in {'local_upload', 'external_generated'}):
            mismatch()
        status = _state(facts.get('status'))
        if status is None:
            record('resource_state_unknown', verified_resource_count=len(resources))
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
    record('recovered', verified_resource_count=len(resources))
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
