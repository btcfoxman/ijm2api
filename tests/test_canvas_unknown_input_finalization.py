"""Official unknown input registrations end safely after their bounded reads."""
from copy import deepcopy
import time
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_service import CanvasServiceMixin
from app.errors import BOUNDED_RETRY_FAILURE_MESSAGE, public_error
from test_canvas_execution import context, owner, task
from test_canvas_transport import FakeRunner


@pytest.fixture
def observed_unknown(context, tmp_path):
    store, service, _ = context
    account = owner(store)
    store.update_account(account['id'], {'login_user_id': '42', 'cli_user_id': '42'})
    account = store.account(account['id'], private=True)
    task(store, image_urls=['https://fixture.example/reference.png'])
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing',
                              'execution_backend': 'canvas_cli'})
    file = tmp_path / 'reference.png'
    file.write_bytes(b'local synthetic image; never uploaded')

    class UnknownRunner(FakeRunner):
        def run(self, account, args, **kwargs):
            if args[:2] == ['resource', 'upload']:
                self.calls.append(list(args))
                raise CanvasCLIError('cli.upload_state_unknown', 'Original registration cannot be confirmed',
                    exit_code=22, outcome_unknown=False, details={'error': {
                        'code': 'cli.upload_state_unknown', 'class': 'human_intervention',
                        'requiredAction': 'human_intervention', 'retryable': False}})
            if args[:2] == ['resource', 'get']:
                self.calls.append(list(args))
                return {'ok': True, 'data': {'resourceId': args[2], 'type': 'image', 'status': 'processing'}}
            return super().run(account, args, **kwargs)

    runner = UnknownRunner(tmp_path)
    runner.persisted.append({})
    client = CanvasClient(runner, account)

    def observed_pending(resource_id, project_id, **kwargs):
        client._call(['resource', 'get', resource_id, '--project-id', project_id],
                     read_only=True, deadline=kwargs['deadline'])
        raise CanvasCLIError('MEDIA_UPLOAD_PENDING', 'Existing bounded observation expired')

    observation = Mock(side_effect=observed_pending)
    with patch.object(client, '_observe_unknown_upload', observation), \
            patch.object(service, 'canvas_client', return_value=client), \
            patch.object(service, '_canvas_local_assets', return_value={
                'images': [{'path': str(file)}], 'videos': [], 'audios': []}):
        service._execute_canvas('task', account, time.monotonic() + 3000)
    row = store.task('task')
    return store, service, row, runner, observation


def test_real_input_pipeline_stops_after_existing_observation_without_reupload_or_new_window(observed_unknown):
    store, service, row, runner, observation = observed_unknown
    assert row['status'] == 'failed' and row['error_code'] == 'cli.upload_state_unknown'
    context = row['upstream_context']
    pending = context['upload_checkpoint']['pending_refs']
    assert len(pending) == 1 and pending[0]['registration'] == 'unknown' and pending[0]['observe_only'] is True
    assert context['preparation_error']['code'] == 'cli.upload_state_unknown'
    assert context['preparation_error']['class'] == 'human_intervention'
    assert context['pre_dispatch_failure'] == {
        'generation_request_sent': False, 'financial_outcome': 'not_charged_this_invocation',
        'automatic_resubmission': False, 'fallback_allowed': True, 'stage': 'preparation'}
    assert 'preparation_retry_budget' not in context
    assert context['preparation_attempt_count'] == 1
    observation.assert_called_once()
    assert sum(args[:2] == ['resource', 'upload'] for args in runner.calls) == 1
    assert sum(args[:2] == ['resource', 'get'] for args in runner.calls) == 1
    assert not any(args[:2] in (['node', 'run'], ['node', 'confirm'], ['node', 'create']) for args in runner.calls)
    assert row['actual_cost'] is None
    before = deepcopy(row)
    public = service.public_task(row)
    assert public['error'] == {'code': 'CLI_PREPARATION_TRANSIENT_FAILED', 'message': BOUNDED_RETRY_FAILURE_MESSAGE}
    assert public['provider_context']['fallback_allowed'] is True
    service._run('task')
    assert row == before == store.task('task')


def test_legacy_unknown_input_get_derives_only_complete_original_task_proof_without_mutation(observed_unknown):
    store, service, row, _, _ = observed_unknown
    context = deepcopy(row['upstream_context'])
    context.pop('pre_dispatch_failure')
    row = store.update_task('task', {'upstream_context': context})
    before = deepcopy(row)
    public = service.public_task(row)
    assert public['provider_context'] == {
        'generation_request_sent': False, 'financial_outcome': 'not_charged_this_invocation',
        'automatic_resubmission': False, 'fallback_allowed': True}
    assert public['error']['code'] == 'CLI_PREPARATION_TRANSIENT_FAILED'
    assert row == before == store.task('task')


@pytest.mark.parametrize('mutation', [
    'missing_checkpoint', 'foreign_task', 'foreign_account', 'profile_hash', 'request_hash',
    'expected_user_hash', 'foreign_project', 'bad_resource_id', 'bad_pending_index',
    'duplicate_pending', 'changed_to_unacknowledged', 'missing_identity_read', 'unfinished_timing',
    'dropped_samples', 'missing_command_sample', 'confirm_was_called', 'run_was_called',
    'missing_archived_attempt', 'accepted_receipt', 'outcome_unknown', 'foreign_timing_task',
])
def test_incomplete_or_conflicting_legacy_audit_never_fabricates_zero_fee_facts(observed_unknown, mutation):
    _, service, source, _, _ = observed_unknown
    row = deepcopy(source)
    context = row['upstream_context']
    context.pop('pre_dispatch_failure', None)
    checkpoint = context['upload_checkpoint']
    timing = context['preparation_timing']
    if mutation == 'missing_checkpoint': context.pop('upload_checkpoint')
    if mutation == 'foreign_task': checkpoint['binding']['task_id'] = 'other'
    if mutation == 'foreign_account': checkpoint['binding']['account_id'] += 1
    if mutation == 'profile_hash': checkpoint['binding']['profile_sha256'] = '0' * 64
    if mutation == 'request_hash': checkpoint['binding']['request_sha256'] = '0' * 64
    if mutation == 'expected_user_hash': context['expected_user_id'] = 'wrong-user'
    if mutation == 'foreign_project': context['project_id'] = 'another-project'
    if mutation == 'bad_resource_id': checkpoint['pending_refs'][0]['resource_id'] = 'another-resource'
    if mutation == 'bad_pending_index': checkpoint['pending_refs'][0]['index'] = 99
    if mutation == 'duplicate_pending': checkpoint['pending_refs'].append(deepcopy(checkpoint['pending_refs'][0]))
    if mutation == 'changed_to_unacknowledged': checkpoint['pending_refs'][0]['registration'] = 'unacknowledged'
    if mutation == 'missing_identity_read':
        for sample in timing['recent_commands']:
            if sample['command_pair'] == ['auth', 'account']: sample['outcome'] = 'failed'
    if mutation == 'unfinished_timing': timing['finished'] = False
    if mutation == 'dropped_samples': timing['dropped_command_samples'] = 1
    if mutation == 'missing_command_sample': timing['recent_commands'].pop()
    if mutation == 'confirm_was_called': timing['commands']['node confirm'] = {'count': 1}
    if mutation == 'run_was_called': timing['commands']['node run'] = {'count': 1}
    if mutation == 'missing_archived_attempt': context.pop('preparation_attempts')
    if mutation == 'accepted_receipt': context['receipt'] = {'accepted': True}
    if mutation == 'outcome_unknown': context['preparation_error']['outcome_unknown'] = True
    if mutation == 'foreign_timing_task': timing['task_id'] = 'other'
    before = deepcopy(row)
    public = service.public_task(row)
    assert public['error'] == {'code': 'CLI_PREPARATION_TRANSIENT_FAILED', 'message': BOUNDED_RETRY_FAILURE_MESSAGE}
    assert 'provider_context' not in public
    assert row == before


@pytest.mark.parametrize('message', ['human intervention: query original operation', 'possible policy failure or credits refunded'])
def test_unknown_input_public_code_and_message_are_neutral_even_without_certifiable_audit(message):
    assert public_error('cli.upload_state_unknown', message) == {
        'code': 'CLI_PREPARATION_TRANSIENT_FAILED', 'message': BOUNDED_RETRY_FAILURE_MESSAGE}


def test_captured_expected_user_can_be_checked_without_reading_current_account_auth(observed_unknown):
    _, _, source, _, _ = observed_unknown
    row = deepcopy(source)
    row['upstream_context']['expected_user_id'] = '42'
    assert CanvasServiceMixin._canvas_input_only_failure_facts(row)['generation_request_sent'] is False


@pytest.fixture
def unavailable_quote(context, tmp_path):
    store, service, _ = context
    account = owner(store)
    store.update_account(account['id'], {'login_user_id': '42', 'cli_user_id': '42'})
    account = store.account(account['id'], private=True)
    task(store, image_urls=['https://fixture.example/reference.png'])
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing',
                              'execution_backend': 'canvas_cli'})
    file = tmp_path / 'quote-reference.png'
    file.write_bytes(b'synthetic fixture; no network')

    class QuoteRunner(FakeRunner):
        def run(self, account, args, **kwargs):
            if args[:2] == ['node', 'quote']:
                self.calls.append(list(args))
                return {'ok': True, 'data': {'items': [{'nodeId': args[-1]}], 'confirmable': False}}
            return super().run(account, args, **kwargs)

    runner = QuoteRunner(tmp_path)
    runner.persisted.append({})
    client = CanvasClient(runner, account)
    with patch.object(service, 'canvas_client', return_value=client), \
            patch.object(service, '_canvas_local_assets', return_value={
                'images': [{'path': str(file)}], 'videos': [], 'audios': []}):
        service._execute_canvas('task', account, time.monotonic() + 3000)
    return store, service, store.task('task'), runner


def test_unusable_quote_stops_without_guessing_price_input_error_or_repeating_paid_calls(unavailable_quote):
    store, service, row, runner = unavailable_quote
    assert row['status'] == 'failed' and row['error_code'] == 'CLI_QUOTE_UNAVAILABLE'
    assert row['upstream_context']['phase'] == 'quoting'
    assert row['upstream_context']['pre_dispatch_failure']['generation_request_sent'] is False
    assert row['actual_cost'] is None and 'quoted_credits' not in row['upstream_context']
    assert sum(args[:2] == ['node', 'create'] for args in runner.calls) == 1
    assert sum(args[:2] == ['node', 'quote'] for args in runner.calls) == 1
    assert not any(args[:2] in (['node', 'confirm'], ['node', 'run']) for args in runner.calls)
    assert 'preparation_retry_budget' not in row['upstream_context']
    before = deepcopy(row)
    public = service.public_task(row)
    assert public['error'] == {'code': 'CLI_PREPARATION_TRANSIENT_FAILED', 'message': BOUNDED_RETRY_FAILURE_MESSAGE}
    assert public['provider_context']['fallback_allowed'] is True
    assert row == before == store.task('task')


@pytest.mark.parametrize('mutation', [None, 'quote_not_recorded', 'foreign_draft',
                                   'unverified_reference', 'incomplete_audit', 'actual_cost'])
def test_quote_legacy_projection_requires_complete_original_draft_and_no_paid_audit(unavailable_quote, mutation):
    _, service, source, _ = unavailable_quote
    row = deepcopy(source)
    context = row['upstream_context']
    context.pop('pre_dispatch_failure', None)
    if mutation == 'quote_not_recorded':
        for audit in [context['preparation_timing'], *[a['timing'] for a in context['preparation_attempts']]]:
            audit['recent_commands'] = [sample for sample in audit['recent_commands']
                if sample['command_pair'] != ['node', 'quote']]
            audit['command_count'] = len(audit['recent_commands'])
            audit['commands'].pop('node quote')
    if mutation == 'foreign_draft': context['node_id'] = 'node_other'
    if mutation == 'unverified_reference': context['upload_checkpoint']['refs'].clear()
    if mutation == 'incomplete_audit': context['preparation_timing']['finished'] = False
    if mutation == 'actual_cost': row['actual_cost'] = 1
    before = deepcopy(row)
    public = service.public_task(row)
    assert public['error'] == {'code': 'CLI_PREPARATION_TRANSIENT_FAILED', 'message': BOUNDED_RETRY_FAILURE_MESSAGE}
    assert bool(public.get('provider_context')) is (mutation is None)
    assert row == before


@pytest.mark.parametrize('code', ['CLI_PREPARATION_TRANSIENT_FAILED', 'CLI_PREPARATION_RETRY_EXHAUSTED',
                                'CLI_PREPARATION_TIMEOUT', 'MEDIA_UPLOAD_PENDING', 'INSUFFICIENT_CREDITS'])
def test_legacy_bounded_preparation_proof_explicitly_allows_fallback_only_after_strict_validation(observed_unknown, code):
    _, service, source, _, _ = observed_unknown
    row = deepcopy(source)
    row['error_code'] = code
    row['upstream_context']['pre_dispatch_failure'].pop('fallback_allowed')
    before = deepcopy(row)
    assert service.public_task(row)['provider_context']['fallback_allowed'] is True
    assert row == before
    row['upstream_context']['receipt'] = {'accepted': True}
    assert 'provider_context' not in service.public_task(row)
