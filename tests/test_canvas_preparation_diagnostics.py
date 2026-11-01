"""Preparation failure facts retain only output hashes, never raw CLI text."""
import hashlib
import json
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError
from app.canvas_service import CanvasServiceMixin
from test_canvas_execution import context, fake_client, owner, task


def captured_error(tmp_path, stdout, stderr=b'', *, pair=('node', 'create'), exit_code=1):
    runner = CanvasCLI(tmp_path, allow_unwrapped_for_tests=True)
    actual = subprocess.Popen
    program = f'import os,sys;os.write(1,{stdout!r});os.write(2,{stderr!r});sys.exit({exit_code!r})'
    def toy_process(command, **kwargs):
        return actual([sys.executable, '-c', program], **kwargs)
    with patch('app.canvas_cli.subprocess.Popen', side_effect=toy_process) as spawn:
        with pytest.raises(CanvasCLIError) as caught:
            runner.run({'id': 1, 'proxy_url': 'socks5://proxy.example.test:20001'},
                       [*pair, 'private-argument'], timeout=5)
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


def test_retryable_preparation_error_keeps_original_context_and_raw_diagnostic(context):
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
    row=store.task('task');history=row['upstream_context']['preparation_failures'];saved=history[-1]['error']
    assert row['status']=='failed' and row['error_code']=='CLI_PREPARATION_RETRY_EXHAUSTED'
    assert len(history)==2 and all(item['account_id']==account['id'] for item in history)
    assert saved['code']=='CLI_INVALID_RESPONSE'
    assert saved['exit_code']==1 and saved['phase']=='saving'
    assert saved['transport_diagnostic']['command_pair']==['node','create']
    assert history[-1]['submit_id']=='original-submit' and history[-1]['node_id']=='original-node'
    assert 'submit_id' not in row['upstream_context'] and 'node_id' not in row['upstream_context']
    assert row['actual_cost'] is None and not service._credit_reservations
    client.submit.assert_not_called()


def test_preparation_failure_history_is_bounded_and_keeps_only_safe_diagnostics(context):
    store,service,_=context
    account=owner(store);task(store)
    store.update_task('task', {'upstream_context': {'preparation_failures': [
        {'account_id':account['id'], 'error':{'code':f'old-{index}'}} for index in range(18)]}})
    client=fake_client(store,service,account)
    client.prepare.side_effect=CanvasCLIError('CLI_CAPABILITY_UNAVAILABLE',
        'unsupported fixture https://private.test/material?token=private-token')
    with patch.object(service,'canvas_client',return_value=client):service._run('task')
    row=store.task('task');history=row['upstream_context']['preparation_failures']
    assert len(history)==16 and history[0]['error']['code']=='old-3'
    assert history[-1]['account_id']==account['id']
    assert history[-1]['error']['code']=='CLI_CAPABILITY_UNAVAILABLE'
    assert 'private-token' not in json.dumps(history) and '?token=private-token' not in json.dumps(history)
    assert all(key not in history[-1]['error'] for key in ('stdout','stderr','rawBody','rawHTTP','partialData'))
    client.submit.assert_not_called()


def test_preparation_failure_audit_survives_account_switch_without_changing_new_active_binding(context):
    store,service,_=context
    first=owner(store,'transient',priority=2);second=owner(store,'ready',priority=1);task(store)
    clients={a['id']:fake_client(store,service,a) for a in (first,second)}
    original=clients[first['id']].prepare.side_effect
    def rejected_prepare(*args,**kwargs):
        original(*args,**kwargs)
        raise CanvasCLIError('CLI_INVALID_RESPONSE','Canvas CLI returned no valid JSON result',exit_code=1)
    clients[first['id']].prepare.side_effect=rejected_prepare
    with patch.object(service,'canvas_client',side_effect=lambda a:clients[a['id']]):service._run('task')
    row=store.task('task');bound=row['upstream_context'];history=bound['preparation_failures']
    assert row['status']=='succeeded' and row['account_id']==second['id']
    assert len(history)==2 and all(value['account_id']==first['id'] for value in history)
    assert all(value['submit_id']==f"submit-task-{first['id']}" for value in history)
    assert bound['submit_id']==f"submit-task-{second['id']}" and bound['project_id']==f"project-{second['id']}"
    assert 'preparation_error' not in bound
    clients[first['id']].submit.assert_not_called()
    clients[second['id']].submit.assert_called_once()
    assert not service._credit_reservations


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
