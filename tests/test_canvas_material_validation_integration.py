"""An authoritative material quote rejection must terminate before any paid run."""
from copy import deepcopy
import time
from unittest.mock import patch

import pytest

from app.canvas_client import CanvasClient
from app.canvas_validation import MATERIAL_REQUIREMENTS_MESSAGE
from test_canvas_execution import context, owner, task
from test_canvas_transport import FakeRunner


@pytest.fixture(params=[False, True], ids=['normal-deadline', 'saved-validation-crosses-deadline'])
def rejected_quote(context, tmp_path, request):
    store, service, _ = context
    account = owner(store)
    store.update_account(account['id'], {'login_user_id': '42', 'cli_user_id': '42'})
    account = store.account(account['id'], private=True)
    task(store, image_urls=['https://fixture.example/original.png'])
    store.update_task('task', {'account_id': account['id'], 'status': 'preparing',
                              'execution_backend': 'canvas_cli'})
    class Runner(FakeRunner):
        def run(self, selected, args, **kwargs):
            if args[:2] == ['node', 'quote']:
                self.calls.append(list(args))
                return {'ok': True, 'data': {'confirmable': False, 'confirmationRequired': False,
                    'totalMaxCredits': 0, 'items': [{'nodeId': args[args.index('--node-id') + 1],
                        'error': {'serviceCode': 2, 'message': 'Generic invalid request.', 'validation': {
                            'reasonCode': 'MATERIAL_RULE_VIOLATED', 'fieldPath': 'materials[0].width_px',
                            'detail': 'The reference image at materials[0] is 170 x 217 px; its width must be at least 300 px.'}}}]}}
            return super().run(selected, args, **kwargs)
    runner = Runner(tmp_path)
    runner.persisted.append({})
    client = CanvasClient(runner, account)
    monotonic = time.monotonic
    began = monotonic()
    crossed = {'value': False}
    diagnostic = service._canvas_diagnostic
    def record(error, phase):
        value = diagnostic(error, phase)
        if request.param and error.code == 'MEDIA_REQUIREMENTS_INVALID':
            crossed['value'] = True
        return value
    with patch.object(service, 'canvas_client', return_value=client), \
            patch.object(service, '_canvas_diagnostic', side_effect=record), \
            patch('app.canvas_service.time.monotonic', side_effect=lambda: began + 4000 if crossed['value'] else monotonic()):
        service._execute_canvas('task', account, began + 3000)
    return store, service, store.task('task'), runner


def test_typed_quote_validation_preserves_details_and_stops_before_confirmation(rejected_quote):
    store, service, row, runner = rejected_quote
    assert row['status'] == 'failed' and row['error_code'] == 'MEDIA_REQUIREMENTS_INVALID'
    bound = row['upstream_context']
    assert bound['material_validation']['material_index'] == 0
    assert bound['material_validation']['actual_width_px'] == 170
    assert bound['material_validation']['minimum_width_px'] == 300
    assert bound['preparation_error']['code'] == 'MEDIA_REQUIREMENTS_INVALID'
    assert bound['preparation_attempt_count'] == 1
    assert 'preparation_retry_budget' not in bound
    assert sum(args[:2] == ['node', 'quote'] for args in runner.calls) == 1
    assert not any(args[:2] in (['node', 'confirm'], ['node', 'run']) for args in runner.calls)
    assert row['actual_cost'] is None


def test_public_material_failure_carries_no_dispatch_and_forbids_route_retry(rejected_quote):
    store, service, row, runner = rejected_quote
    original = deepcopy(row)
    public = service.public_task(row)
    assert public['error'] == {'code': 'MEDIA_REQUIREMENTS_INVALID', 'message': MATERIAL_REQUIREMENTS_MESSAGE}
    assert public['provider_context'] == {'generation_request_sent': False,
        'financial_outcome': 'not_charged_this_invocation', 'automatic_resubmission': False,
        'fallback_allowed': False}
    service._run(row['id'])
    assert store.task(row['id']) == original


def test_incomplete_audit_cannot_fabricate_no_charge_claim(rejected_quote):
    store, service, row, runner = rejected_quote
    changed = deepcopy(row)
    changed['upstream_context']['preparation_timing']['dropped_command_samples'] = 1
    public = service.public_task(changed)
    assert public['error']['message'] == MATERIAL_REQUIREMENTS_MESSAGE
    assert not public.get('provider_context')
