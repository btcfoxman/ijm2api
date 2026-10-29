"""Preparation failure facts retain only output hashes, never raw CLI text."""
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError
from app.canvas_service import CanvasServiceMixin
from test_canvas_execution import context, fake_client, owner, task


def captured_error(tmp_path, stdout, stderr=b'', *, pair=('node', 'create'), exit_code=1):
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    process = SimpleNamespace(stdout=io.BytesIO(stdout), stderr=io.BytesIO(stderr),
                              poll=lambda: exit_code, returncode=exit_code)
    with patch.object(runner, 'environment', return_value={}), \
            patch.object(runner, 'profile', return_value='synthetic-profile'), \
            patch('app.canvas_cli.subprocess.Popen', return_value=process) as spawn:
        with pytest.raises(CanvasCLIError) as caught:
            runner.run({'id': 1}, [*pair, 'private-argument'], timeout=5)
    spawn.assert_called_once()
    return caught.value


def test_noisy_json_remains_invalid_and_keeps_only_captured_byte_hashes(tmp_path):
    stdout=b'private prompt token=private-token https://private.test/material\n{"ok":true,"data":{}}'
    stderr=b'proxy password=private-password /private/local/path'
    error=captured_error(tmp_path, stdout, stderr)
    assert error.code == 'CLI_INVALID_RESPONSE' and error.exit_code == 1
    assert error.outcome_unknown is False
    diagnostic=error.details['transport_diagnostic']
    assert diagnostic == {'command_pair':['node','create'], 'stdout_bytes':len(stdout),
        'stderr_bytes':len(stderr), 'stdout_sha256':hashlib.sha256(stdout).hexdigest(),
        'stderr_sha256':hashlib.sha256(stderr).hexdigest()}
    serialized=json.dumps(error.details)
    assert all(secret not in serialized for secret in ('private prompt','private-token','private-password',
        'private.test','/private/local/path','private-argument'))


def test_invalid_paid_receipt_stays_unknown_without_replay(tmp_path):
    error=captured_error(tmp_path, b'not-json private-token', pair=('node','run'))
    assert error.code == 'CLI_INVALID_RESPONSE' and error.outcome_unknown is True
    assert error.details['transport_diagnostic']['command_pair'] == ['node','run']


def test_structured_error_and_partial_acceptance_evidence_are_unchanged(tmp_path):
    envelope={'ok':False,'error':{'code':'cli.transport_failed','message':'network failure'},
              'partialData':{'items':[{'state':'accepted','resources':[{'resourceId':'original-resource'}]}]}}
    error=captured_error(tmp_path,json.dumps(envelope).encode(),pair=('node','run'))
    assert error.code == 'cli.transport_failed' and error.outcome_unknown is True
    assert error.details['error'] == envelope['error']
    assert error.details['partialData'] == envelope['partialData']


def test_diagnostic_metadata_uses_exact_white_list():
    error=CanvasCLIError('CLI_INVALID_RESPONSE','safe message',exit_code=7,details={
        'transport_diagnostic':{'command_pair':['node','create'],'stdout_bytes':42,
            'stdout_sha256':'a'*64,'stderr_bytes':False,'stderr_sha256':'token=secret',
            'stdout':'private prompt','args':['private-path'],'proxy':'private-proxy','token':'secret'}})
    diagnostic=CanvasServiceMixin._canvas_diagnostic(error,'saving')
    assert diagnostic['exit_code'] == 7
    assert diagnostic['transport_diagnostic'] == {'command_pair':['node','create'],
        'stdout_bytes':42,'stdout_sha256':'a'*64}
    assert 'private' not in json.dumps(diagnostic) and 'secret' not in json.dumps(diagnostic)


def test_unrecoverable_preparation_error_is_saved_after_original_context(context):
    store,service,_=context
    account=owner(store);task(store)
    client=fake_client(store,service,account)
    def failed_prepare(normalized,identifier,assets,*,persist,deadline):
        persist({'phase':'saving','submit_id':'original-submit','node_id':'original-node'})
        raise CanvasCLIError('CLI_INVALID_RESPONSE','Canvas CLI returned no valid JSON result',exit_code=1,
            details={'transport_diagnostic':{'command_pair':['node','create'],
                'stdout_bytes':5,'stdout_sha256':'b'*64,'stderr_bytes':0,'stderr_sha256':hashlib.sha256(b'').hexdigest()}})
    client.prepare.side_effect=failed_prepare
    with patch.object(service,'canvas_client',return_value=client):service._run('task')
    row=store.task('task');saved=row['upstream_context']['preparation_error']
    assert row['status']=='failed' and row['error_code']=='CLI_INVALID_RESPONSE'
    assert saved['exit_code']==1 and saved['phase']=='saving'
    assert saved['transport_diagnostic']['command_pair']==['node','create']
    assert row['upstream_context']['submit_id']=='original-submit'
    assert row['actual_cost'] is None and not service._credit_reservations
    client.submit.assert_not_called()


@pytest.mark.parametrize('failure', ['identity','model','credits'])
def test_safe_account_reselection_does_not_gain_terminal_preparation_diagnostic(context,failure):
    store,service,_=context
    first=owner(store,'first',priority=9);second=owner(store,'second',priority=8);task(store)
    clients={a['id']:fake_client(store,service,a) for a in (first,second)}
    client=clients[first['id']]
    if failure=='identity':client.identity.side_effect=CanvasCLIError('CLI_COMMAND_TIMEOUT','identity timeout')
    elif failure=='model':client.preflight_model.side_effect=CanvasCLIError('CLI_CAPABILITY_UNAVAILABLE',
        'account lacks model',details={'error':{'scope':'account','reason':'model_not_in_catalog',
            'requested_model':'sd-2-0-mini','kind':'video'}})
    else:client.submit.side_effect=CanvasCLIError('INSUFFICIENT_CREDITS','not enough credits left')
    with patch.object(service,'canvas_client',side_effect=lambda a:clients[a['id']]):service._run('task')
    row=store.task('task')
    assert row['status']=='succeeded' and row['account_id']==second['id']
    assert 'preparation_error' not in row['upstream_context']
