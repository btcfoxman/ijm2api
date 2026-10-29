"""Preparation diagnostics are bounded, safe, and distinct from paid attempts."""
import hashlib
import json

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError, command_failure_facts
from app.canvas_timing import PreparationTiming, archived_attempts
from test_canvas_transport import ACCOUNT, patch_process


def test_repeat_attempts_keep_initial_queue_separate_from_prior_preparation():
    timing=PreparationTiming('task',queued_at=100,first_admission_at=100.07,
        wall_time=lambda:301.3,monotonic=lambda:7)
    value=timing.snapshot()
    assert value['queue_wait_ms']==value['first_admission_queue_ms']==70
    assert value['elapsed_before_attempt_ms']==201300


def test_attempt_archive_keeps_last8_and_global_counter():
    context={}
    for index in range(12):
        value=PreparationTiming('task',monotonic=lambda:0,wall_time=lambda:index+100).snapshot()
        context.update(archived_attempts(context,value,index+1,'insufficient_credits'))
    assert context['preparation_attempt_count']==12
    assert [v['ordinal'] for v in context['preparation_attempts']]==list(range(5,13))
    assert len(context['preparation_attempts'])==8


def test_real_process_without_json_keeps_exit_fingerprints_and_fixed_symptom_only(tmp_path,monkeypatch):
    raw='proxy https://user:private-password@proxy.test:20019 connection refused private-prompt private-token'
    patch_process(monkeypatch,f'import sys;sys.stderr.write({raw!r});sys.exit(7)')
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True);timing=PreparationTiming('task')
    with pytest.raises(CanvasCLIError):
        timing.command(runner,ACCOUNT,['resource','upload','--source-url','https://private-material.example/secret'],
            {},1,lambda:runner.run(ACCOUNT,['resource','upload'],timeout=2))
    failure=timing.snapshot()['recent_commands'][0]['failure']
    assert failure['exit_code']==7
    assert failure['transport']['stdout_bytes']==0
    assert failure['transport']['stderr_bytes']==len(raw)
    assert failure['transport']['stderr_sha256']==hashlib.sha256(raw.encode()).hexdigest()
    assert failure['transport']['stderr_symptoms']==['connection_refused']
    serialized=json.dumps(timing.snapshot())
    for secret in ['private-password','proxy.test','private-prompt','private-token','private-material','https://']:
        assert secret not in serialized


def test_structured_transport_failure_keeps_scalar_evidence_but_no_message_or_raw_fields():
    error=CanvasCLIError('cli.transport_failed','private failure URL',exit_code=21,details={'error':{
        'class':'internal_failure','reason':'network_error','retryable':True,'serviceCode':6,
        'requiredAction':'retry','message':'private body','url':'https://private.example'},
        'transport_diagnostic':{'stdout_bytes':5,'stderr_bytes':0,'stdout_sha256':'a'*64,
            'stderr_sha256':'b'*64,'stderr_symptoms':['connection_refused','private-text'],'raw':'private'}})
    facts=command_failure_facts(error)
    assert facts['class']=='internal_failure' and facts['reason']=='network_error'
    assert facts['retryable'] is True and facts['exit_code']==21 and facts['serviceCode']==6
    assert facts['transport']['stderr_symptoms']==['connection_refused']
    assert 'private' not in json.dumps(facts) and 'message' not in facts


def test_malformed_failure_fields_cannot_smuggle_prose_or_unbounded_payloads():
    error=CanvasCLIError('x','secret',details={'error':{'class':'https://secret.example',
        'reason':['secret'],'serviceCode':{'secret':1}},'transport_diagnostic':{'stdout_bytes':True,
        'stderr_bytes':-1,'stdout_sha256':'secret','stderr_symptoms':'secret'}})
    assert command_failure_facts(error)=={}
