"""Input-only account handoffs preserve budgets, sources, and paid boundaries."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import time
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient, _node_id, _stable
from app.canvas_input_handoff import (HANDOFF_KEY, digest, handoff_input_guard,
                                     propose_input_handoff, verify_input_guard)
from app.canvas_retry_budget import budget_token, claim_attempt, new_retry_budget
from test_canvas_execution import context, owner, task, fake_client
from test_canvas_transport import FakeRunner, model


def transport():
    return CanvasCLIError('cli.transport_failed', 'synthetic transport failure', details={'error': {
        'code': 'cli.transport_failed', 'class': 'retryable_failure', 'requiredAction': 'retry', 'retryable': True}},
        exit_code=21, outcome_unknown=False)


def prepared(context, tmp_path, *, unknown=False):
    store, service, _ = context
    old = owner(store, 'input-owner', balance=200, priority=9)
    new = owner(store, 'replacement-owner', balance=200, priority=1)
    profile = service.canvas_cli.profile(old)
    row = task(store, 'input-handoff', image_urls=['https://fixture.example/one.png',
        'https://fixture.example/two.png'], audio_urls=['https://fixture.example/voice.mp3'])
    normalized = row['normalized']
    audio = tmp_path / 'original.mp3';audio.write_bytes(b'ID3-original-voice')
    assets = {'images': [{'source_url': value} for value in normalized['images']], 'videos': [], 'audios': [audio]}
    descriptors = [{'group': 'images', 'index': i, 'source_kind': 'url',
                    'source_sha256': hashlib.sha256(value.encode()).hexdigest()} for i, value in enumerate(normalized['images'])]
    descriptors.append({'group': 'audios', 'index': 0, 'source_kind': 'file',
                        'source_sha256': hashlib.sha256(audio.read_bytes()).hexdigest()})
    project = _stable('account', old['id'], old['login_user_id'])
    binding = {'task_id': row['id'], 'account_id': old['id'], 'project_id': project,
        'profile_sha256': digest(profile), 'user_id_sha256': digest(old['login_user_id']),
        'proxy_sha256': digest(old['proxy_url']),
        'request_sha256': digest({k: v for k, v in normalized.items() if not k.startswith('_')}),
        'media_sha256': digest(descriptors)}
    refs = [{**item, 'resource_id': _stable(old['id'], row['id'], item['group'], item['index'], digest(binding))}
            for item in descriptors]
    now = time.time()
    proof = {'stage': 'preparation', 'node_run_count': 0, 'generation_request_sent': False,
             'dispatch_started': False, 'outcome_unknown': False, 'prior_dispatch_evidence': False}
    budget = new_retry_budget(row['id'], admitted_at=now - 20, retry_started_at=now - 10, task_deadline=now + 3000)
    attempts = []
    diagnostic = {'code': 'cli.upload_state_unknown' if unknown else 'cli.transport_failed',
        'class': 'human_intervention' if unknown else 'retryable_failure', 'phase': 'uploading',
        'requiredAction': 'human_intervention' if unknown else 'retry', 'retryable': not unknown,
        'outcome_unknown': False, 'exit_code': 22 if unknown else 21}
    for index in range(1 if unknown else 2):
        budget = claim_attempt(budget, task_id=row['id'], account_id=old['id'], now=now - 10 + index,
            expected_token=budget_token(budget, task_id=row['id']), proof=proof)
        recent = [
            {'command_pair': ['auth', 'account'], 'outcome': 'success', 'started_at': now - 8 + index},
            {'command_pair': ['resource', 'upload'], 'outcome': 'failed', 'error_code': diagnostic['code'],
             'started_at': now - 6 + index, 'elapsed_ms': 500, 'index': 2, 'media_type': 'image',
             'failure': {'exit_code': diagnostic['exit_code'], 'class': diagnostic['class']}},
            {'command_pair': ['resource', 'get'], 'outcome': 'failed', 'error_code': 'service.6',
             'started_at': now - 5 + index, 'index': 2, 'media_type': 'image', 'failure': {
                 'class': 'invalid_input', 'serviceCode': 6, 'requiredAction': 'none', 'retryable': False}},
        ]
        timing = {'task_id': row['id'], 'finished': True, 'current_command': None, 'current_phase': None,
            'dropped_command_samples': 0, 'command_count': len(recent), 'recent_commands': recent,
            'commands': {'auth account': {'count': 1, 'failed': 0}, 'resource upload': {'count': 1, 'failed': 1},
                         'resource get': {'count': 1, 'failed': 1}}}
        attempts.append({'ordinal': index + 1, 'account_id': old['id'], 'reason': 'command_failed', 'timing': timing})
    cp = {'version': 1, 'backend': 'canvas_cli', 'binding': binding,
          'refs': [{**refs[0], 'verified': True}],
          'pending_refs': [{**refs[1], 'observe_only': True,
                            'registration': 'unknown' if unknown else 'unacknowledged'}]}
    ctx = {'backend': 'canvas_cli', 'account_id': old['id'], 'task_id': row['id'], 'profile': profile,
        'project_id': project, 'node_id': _node_id(f"{old['id']}:{row['id']}"),
        'update_id': _stable(old['id'], row['id'], 'update'), 'submit_id': _stable(old['id'], row['id'], 'submit'),
        'kind': 'video', 'phase': 'uploading', 'resource_ids': [],
        'resources': {group: [item['resource_id'] for item in refs if item['group'] == group] for group in assets},
        'uploaded_resources': [refs[0]['resource_id']], 'upload_checkpoint': cp,
        'preparation_admission_at': now - 20, 'preparation_attempts': attempts,
        'preparation_attempt_count': len(attempts), 'preparation_timing': deepcopy(attempts[-1]['timing']),
        'preparation_retry_budget': budget, 'preparation_error': diagnostic}
    ctx['pre_dispatch_failure'] = {'generation_request_sent': False,
        'financial_outcome': 'not_charged_this_invocation', 'automatic_resubmission': False,
        'fallback_allowed': True, 'stage': 'preparation'}
    row = store.update_task(row['id'], {'status': 'preparing', 'execution_backend': 'canvas_cli',
        'account_id': old['id'], 'estimated_cost': 20, 'upstream_context': ctx})
    return store, service, old, new, row, profile, assets, diagnostic


def proposal(values):
    _, _, old, _, row, profile, _, diagnostic = values
    return propose_input_handoff(row, old, profile, diagnostic, now=time.time(), workers_joined=True)


def test_live_input_handoff_preserves_deadline_and_moves_no_old_input_to_new_owner(context, tmp_path):
    values = prepared(context, tmp_path)
    store, service, old, new, row, _, _, _ = values
    budget = deepcopy(row['upstream_context']['preparation_retry_budget'])
    assert service._canvas_handoff_preparation(row['id'], old, transport()) is True
    queued = store.task(row['id'])
    assert queued['status'] == 'queued' and queued['account_id'] is None and queued['execution_backend'] == 'canvas_cli'
    assert queued['attempted_account_ids'] == [old['id']]
    assert queued['upstream_context']['preparation_retry_budget'] == budget
    assert not any(k in queued['upstream_context'] for k in ('upload_checkpoint', 'resources', 'profile', 'node_id', 'submit_id'))
    assert queued['upstream_context'][HANDOFF_KEY][0]['checkpoint'] == row['upstream_context']['upload_checkpoint']
    picked = service._reserve_account(20, task_id=row['id'])
    assert picked['id'] == new['id']
    service._release_account(new['id'], row['id'])
    assert queued['actual_cost'] is None and store.account(old['id'])['balance'] == 200


@pytest.mark.parametrize('change', ['failed', 'unknown_generation', 'manual_empty', 'receipt_empty', 'confirm',
    'run', 'audit_trimmed', 'audit_missing', 'audit_unfinished', 'workers_active', 'foreign_uid',
    'foreign_proxy', 'foreign_profile', 'expected_uid', 'image_kind', 'material_error', 'auth_error',
    'expired', 'three_tickets', 'existing_handoff', 'source_url_changed', 'ambiguous_partial',
    'submit_error', 'prior_failure_dispatch', 'prior_financial_unknown', 'prior_nested_receipt',
    'camel_resource_ids', 'prior_invalid_media', 'prior_identity', 'unknown_command'])
def test_any_incomplete_or_non_input_only_evidence_vetoes_handoff(context, tmp_path, change):
    values = prepared(context, tmp_path)
    _, _, old, _, original, profile, _, diagnostic = values
    row = deepcopy(original);ctx = row['upstream_context'];diagnostic = deepcopy(diagnostic)
    at = time.time();joined = True
    if change == 'failed': row['status'] = 'failed'
    elif change == 'unknown_generation': ctx['submission_phase'] = 'run_dispatching'
    elif change == 'manual_empty': ctx['manual_termination'] = {}
    elif change == 'receipt_empty': ctx['operation_receipt'] = {}
    elif change in {'run', 'confirm'}:
        timing = ctx['preparation_attempts'][-1]['timing']
        timing['recent_commands'].append({'command_pair': ['node', change], 'outcome': 'failed'})
        timing['command_count'] += 1;timing['commands']['node ' + change] = {'count': 1, 'failed': 1}
        ctx['preparation_timing'] = deepcopy(timing)
    elif change == 'audit_trimmed': ctx['preparation_timing']['dropped_command_samples'] = 1
    elif change == 'audit_missing': ctx['preparation_attempts'][0]['timing']['recent_commands'].pop()
    elif change == 'audit_unfinished': ctx['preparation_timing']['finished'] = False
    elif change == 'workers_active': joined = False
    elif change == 'foreign_uid': old = {**old, 'login_user_id': 'other', 'cli_user_id': 'other'}
    elif change == 'foreign_proxy': old = {**old, 'proxy_url': 'socks5://different:9000'}
    elif change == 'foreign_profile': profile = 'different-epoch'
    elif change == 'expected_uid': ctx['expected_user_id'] = 'other'
    elif change == 'image_kind': row['normalized']['kind'] = 'image'
    elif change == 'material_error': diagnostic.update(code='MEDIA_REQUIREMENTS_INVALID', **{'class': 'invalid_input'})
    elif change == 'auth_error': diagnostic.update(code='cli.authentication_required')
    elif change == 'expired': at = ctx['preparation_retry_budget']['deadline']
    elif change == 'three_tickets':
        state = ctx['preparation_retry_budget'];other = old['id'] + 1
        ctx['preparation_retry_budget'] = claim_attempt(state, task_id=row['id'], account_id=other,
            now=at, expected_token=budget_token(state, task_id=row['id']), proof=state['attempts'][0]['proof'])
    elif change == 'existing_handoff': ctx[HANDOFF_KEY] = []
    elif change == 'source_url_changed': row['normalized']['images'][0] += '?changed=1'
    elif change == 'ambiguous_partial': ctx['submission_evidence'] = {'version': 1, 'ambiguous_partial': True}
    elif change == 'submit_error': ctx['submit_error'] = {'phase': 'run_dispatching', 'dispatch_started': True, 'outcome_unknown': True}
    elif change == 'prior_failure_dispatch': ctx['preparation_failures'] = [{'account_id': old['id'], 'error': {'phase': 'run_dispatching', 'dispatch_started': True, 'outcome_unknown': True}}]
    elif change == 'prior_financial_unknown': ctx['preparation_failures'] = [{'account_id': old['id'], 'error': {'financialOutcome': 'unconfirmed'}}]
    elif change == 'prior_nested_receipt': ctx['preparation_failures'] = [{'account_id': old['id'], 'error': {
        'submission': {'state': 'accepted', 'resources': [{'resourceId': 'generated-output'}]}}}]
    elif change == 'camel_resource_ids': ctx['resourceIds'] = ['generated-output']
    elif change in {'prior_invalid_media', 'prior_identity', 'unknown_command'}:
        timing = ctx['preparation_attempts'][0]['timing'];command = timing['recent_commands'][1]
        if change == 'unknown_command':
            command['command_pair'] = ['future', 'generate']
            timing['commands']['future generate'] = timing['commands'].pop('resource upload')
        elif change == 'prior_invalid_media':
            command['error_code'] = 'service.2';command['failure'] = {'class': 'invalid_input', 'requiredAction': 'fix_input'}
        else:
            command['failure'] = {'class': 'identity_failure', 'reason': 'identity_mismatch', 'requiredAction': 'authenticate'}
    assert propose_input_handoff(row, old, profile, diagnostic, now=at, workers_joined=joined) is None


@pytest.mark.parametrize('race', ['same_clock', 'manual', 'receipt', 'closed', 'proxy', 'profile', 'expired', 'tampered_proposal'])
def test_store_handoff_cas_rechecks_all_persistent_boundaries(context, tmp_path, race):
    values = prepared(context, tmp_path)
    store, service, old, _, row, _, _, _ = values
    proposed = proposal(values);assert proposed is not None
    def commit():
        return store.migrate_canvas_input_preparation(row['id'], proposed,
            expected_updated_at=row['updated_at'], expected_context=row['upstream_context'], expected_account=old)
    if race == 'same_clock':
        with patch('app.store.time.time', return_value=row['updated_at']):
            with ThreadPoolExecutor(max_workers=5) as pool:
                outcomes = list(pool.map(lambda _: commit(), range(10)))
        assert outcomes.count(True) == 1
        return
    if race in {'manual', 'receipt'}:
        ctx = deepcopy(row['upstream_context']);ctx['manual_termination' if race == 'manual' else 'operation_receipt'] = {}
        store.update_task(row['id'], {'upstream_context': ctx})
    elif race == 'closed': store.update_task(row['id'], {'status': 'failed'})
    elif race == 'proxy': store.update_account(old['id'], {'proxy_url': 'socks5://changed:9'})
    elif race == 'profile': (service.canvas_cli.account_directory(old) / 'credential-profile').write_text('retired')
    elif race == 'tampered_proposal': proposed['context']['preparation_retry_budget']['deadline'] += 1
    before = deepcopy(store.task(row['id']))
    if race == 'expired':
        with patch('app.store.time.time', return_value=row['upstream_context']['preparation_retry_budget']['deadline']):
            assert commit() is False
    else: assert commit() is False
    assert store.task(row['id']) == before


@pytest.mark.parametrize('evidence', ['ambiguous_partial', 'submit_error', 'prior_failure_dispatch'])
def test_store_independently_rejects_uncertain_generation_even_with_fresh_cas_arguments(context, tmp_path, evidence):
    values = prepared(context, tmp_path);store, _, old, _, row, _, _, _ = values
    proposed = proposal(values);ctx = deepcopy(row['upstream_context'])
    if evidence == 'ambiguous_partial': ctx['submission_evidence'] = {'version': 1, 'ambiguous_partial': True}
    elif evidence == 'submit_error': ctx['submit_error'] = {'phase': 'run_dispatching', 'dispatch_started': True, 'outcome_unknown': True}
    else: ctx['preparation_failures'] = [{'account_id': old['id'], 'error': {'phase': 'run_dispatching', 'outcome_unknown': True}}]
    changed = store.update_task(row['id'], {'upstream_context': ctx})
    assert store.migrate_canvas_input_preparation(row['id'], proposed,
        expected_updated_at=changed['updated_at'], expected_context=changed['upstream_context'], expected_account=old) is False
    assert store.task(row['id']) == changed


def test_ordinary_progress_cas_conflict_finishes_instead_of_leaving_preparing_unowned(context, tmp_path):
    values = prepared(context, tmp_path);store, service, old, _, row, _, _, _ = values
    with patch.object(store, 'migrate_canvas_input_preparation', return_value=False):
        assert service._canvas_handoff_preparation(row['id'], old, transport()) == 'stopped'
    result = store.task(row['id'])
    assert result['status'] == 'failed' and result['error_code'] == 'CLI_PREPARATION_RETRY_EXHAUSTED'
    assert service._canvas_public_pre_dispatch_facts(result)['fallback_allowed'] is True


def test_receipt_winning_handoff_race_is_never_overwritten_by_conflict_close(context, tmp_path):
    values = prepared(context, tmp_path);store, service, old, _, row, _, _, _ = values
    def race(*args, **kwargs):
        ctx = deepcopy(store.task(row['id'])['upstream_context'])
        ctx.update(phase='accepted', submission_phase='accepted', resource_ids=['original-accepted-output'])
        store.update_task(row['id'], {'status': 'running', 'upstream_context': ctx})
        return False
    with patch.object(store, 'migrate_canvas_input_preparation', side_effect=race):
        assert service._canvas_handoff_preparation(row['id'], old, transport()) == 'stopped'
    result = store.task(row['id'])
    assert result['status'] == 'running' and result['upstream_context']['resource_ids'] == ['original-accepted-output']


def test_conflict_close_cannot_overwrite_result_transfer_evidence(context, tmp_path):
    values = prepared(context, tmp_path);store, _, old, _, row, _, _, _ = values
    current = store.update_task(row['id'], {'result_transfer': {'phase': 'uploading'}})
    assert store.close_canvas_input_handoff_conflict(row['id'], old['id']) is False
    assert store.task(row['id']) == current


@pytest.mark.parametrize('damage', ['count', 'ordinal', 'budget'])
def test_conflict_close_requires_the_complete_current_audit(context, tmp_path, damage):
    values = prepared(context, tmp_path);store, _, old, _, row, _, _, _ = values
    ctx = deepcopy(row['upstream_context'])
    if damage == 'count': ctx['preparation_attempt_count'] += 1
    elif damage == 'ordinal': ctx['preparation_attempts'][0]['ordinal'] = 2
    else: ctx['preparation_retry_budget']['attempts'].pop()
    current = store.update_task(row['id'], {'upstream_context': ctx})
    assert store.close_canvas_input_handoff_conflict(row['id'], old['id']) is False
    assert store.task(row['id']) == current


def test_unknown_input_gets_one_handoff_without_renewing_budget_or_second_handoff(context, tmp_path):
    values = prepared(context, tmp_path, unknown=True)
    store, _, old, new, row, _, _, _ = values
    proposed = proposal(values);assert proposed is not None
    assert len(proposed['context']['preparation_retry_budget']['attempts']) == 1
    assert store.migrate_canvas_input_preparation(row['id'], proposed,
        expected_updated_at=row['updated_at'], expected_context=row['upstream_context'], expected_account=old)
    queued = store.task(row['id']);guard = handoff_input_guard(queued)
    assert guard['source_account_id'] == old['id']
    state = queued['upstream_context']['preparation_retry_budget']
    for _ in range(2):
        state = claim_attempt(state, task_id=row['id'], account_id=new['id'], now=time.time(),
            expected_token=budget_token(state, task_id=row['id']), proof=state['attempts'][0]['proof'])
    queued['upstream_context']['preparation_retry_budget'] = state
    assert handoff_input_guard(queued) == guard
    assert state['deadline'] == proposed['context']['preparation_retry_budget']['deadline']
    assert propose_input_handoff(queued, new, 'other', values[-1], now=time.time(), workers_joined=True) is None


@pytest.mark.parametrize('change', ['request', 'file', 'url', 'source_kind'])
def test_handoff_source_guard_rejects_changes_before_any_project_or_material_write(context, tmp_path, change):
    values = prepared(context, tmp_path)
    _, _, _, new, row, _, assets, _ = values
    proposed = proposal(values)
    future = {**row, 'status': 'queued', 'account_id': None, 'upstream_context': proposed['context']}
    guard = handoff_input_guard(future)
    normalized = deepcopy(row['normalized']);assets = deepcopy(assets)
    if change == 'request': normalized['prompt'] = 'Changed prompt'
    elif change == 'file': assets['audios'][0].write_bytes(b'ID3-changed-voice')
    elif change == 'url': assets['images'][0]['source_url'] += '?different=1'
    else:
        path = tmp_path / 'replacement.png';path.write_bytes(b'\x89PNG\r\n\x1a\n')
        assets['images'][0] = path
    runner = FakeRunner(tmp_path);runner.persisted.append({})
    client = CanvasClient(runner, new, capabilities={'video': [model()]})
    with pytest.raises(CanvasCLIError, match='input content changed'):
        client.prepare(normalized, row['id'], assets, input_binding_guard=guard, persist=runner.persisted.append)
    assert not any(args[0] in {'canvas', 'resource', 'node'} for args in runner.calls)


def test_damaged_handoff_ledger_never_degrades_into_unrestricted_prepare(context, tmp_path):
    values = prepared(context, tmp_path)
    row = values[4];proposed = proposal(values)
    future = {**row, 'status': 'queued', 'account_id': None, 'upstream_context': proposed['context']}
    future['upstream_context'][HANDOFF_KEY][0]['checkpoint']['binding']['media_sha256'] = '0' * 64
    with pytest.raises(ValueError): handoff_input_guard(future)


def test_unchanged_sources_prepare_only_new_account_ids_and_requote(context, tmp_path):
    values = prepared(context, tmp_path)
    _, _, _, new, row, _, assets, _ = values
    proposed = proposal(values)
    future = {**row, 'status': 'queued', 'account_id': None, 'upstream_context': proposed['context']}
    guard = handoff_input_guard(future)
    runner = FakeRunner(tmp_path);runner.persisted.append({})
    client = CanvasClient(runner, new, capabilities={'video': [model()]})
    prepared_new = client.prepare(row['normalized'], row['id'], assets,
        input_binding_guard=guard, persist=runner.persisted.append)
    old_ids = {value for ids in row['upstream_context']['resources'].values() for value in ids}
    new_ids = {value for ids in prepared_new['resources'].values() for value in ids}
    assert old_ids.isdisjoint(new_ids)
    assert {args[-1] for args in runner.calls if args[:2] == ['resource', 'upload']} == new_ids
    assert {args[2] for args in runner.calls if args[:2] == ['resource', 'get']} == new_ids
    assert sum(args[:2] == ['node', 'quote'] for args in runner.calls) == 1
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm']) for args in runner.calls)


def test_normal_budget_claim_runs_replacement_once_and_receives_source_guard(context, tmp_path):
    values = prepared(context, tmp_path)
    store, service, old, new, row, _, assets, _ = values
    assert service._canvas_handoff_preparation(row['id'], old, transport()) is True
    client = fake_client(store, service, new, quote=20)
    original = client.prepare.side_effect
    def prepare(normalized, task_id, prepared_assets, **kwargs):
        guard = kwargs.pop('input_binding_guard')
        assert guard['source_account_id'] == old['id']
        verify_input_guard(guard, {'task_id': task_id, 'account_id': new['id'],
            'request_sha256': guard['request_sha256'], 'media_sha256': guard['media_sha256']})
        return original(normalized, task_id, prepared_assets, **kwargs)
    client.prepare.side_effect = prepare
    with patch.object(service, 'canvas_client', return_value=client), patch.object(service, '_canvas_local_assets', return_value=assets):
        service._run(row['id'])
    result = store.task(row['id'])
    assert result['status'] == 'succeeded' and client.submit.call_count == 1
    budget = result['upstream_context']['preparation_retry_budget']
    assert [entry['account_id'] for entry in budget['attempts']] == [old['id'], old['id'], new['id']]
    assert result['actual_cost'] == 20 and store.account(old['id'])['balance'] == 200


def test_positive_unknown_terminal_branch_seeds_once_then_hands_off_without_paid_command(context, tmp_path):
    values = prepared(context, tmp_path);store, service, old, _, row, profile, assets, _ = values
    store.update_task(row['id'], {'upstream_context': {}, 'error_code': '', 'error_message': ''})
    runner = FakeRunner(tmp_path);runner.persisted.append({});runner.profile = lambda account: profile
    original_run = runner.run
    unknown = CanvasCLIError('cli.upload_state_unknown', 'synthetic completed bounded observation',
        details={'error': {'code': 'cli.upload_state_unknown', 'class': 'human_intervention',
                          'requiredAction': 'human_intervention'}}, exit_code=22)
    unresolved = set()
    def run(account, args, **kwargs):
        if args[:2] == ['auth', 'account']:
            runner.calls.append(args)
            return {'ok': True, 'data': {'userId': old['login_user_id'], 'credits': {'available': 200}}}
        if args[:2] == ['resource', 'upload'] and args[args.index('--source-url') + 1].endswith('two.png'):
            runner.calls.append(args);unresolved.add(args[-1]);raise unknown
        if args[:2] == ['resource', 'get'] and args[2] in unresolved:
            runner.calls.append(args)
            raise CanvasCLIError('service.6', 'The requested resource was not found.', details={'error': {
                'code': 'service.6', 'serviceCode': 6, 'class': 'invalid_input',
                'requiredAction': 'none', 'retryable': False}}, exit_code=2)
        return original_run(account, args, **kwargs)
    runner.run = run
    client = CanvasClient(runner, old, capabilities={'video': [model()]})
    original_upload = client._upload_resource
    def upload(path, kind, identifier, project, **kwargs):
        if str(kwargs.get('source_url')).endswith('two.png'):
            try:
                client._call(['resource', 'upload', '--source-url', kwargs['source_url'], '--type', kind,
                    '--project-id', project, '--resource-id', identifier], deadline=kwargs['deadline'])
            except CanvasCLIError:
                kwargs['on_registered']('unknown')
                try: client._call(['resource', 'get', identifier, '--project-id', project], deadline=kwargs['deadline'])
                except CanvasCLIError: pass
                raise unknown
        return original_upload(path, kind, identifier, project, **kwargs)
    client._upload_resource = upload
    with patch.object(service, 'canvas_client', return_value=client), patch.object(service, '_canvas_local_assets', return_value=assets):
        result = service._execute_canvas(row['id'], old, time.monotonic() + 60)
    current = store.task(row['id'])
    assert result == 'retry_preparation' and current['status'] == 'queued'
    assert len(current['upstream_context']['preparation_retry_budget']['attempts']) == 1
    assert len(current['upstream_context'][HANDOFF_KEY]) == 1
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm']) for args in runner.calls)
