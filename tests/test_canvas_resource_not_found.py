"""Observe a planned upload ID through exact service.6 registration absence."""
from pathlib import Path

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from test_canvas_upload_recovery import ACCOUNT, UPLOAD_MESSAGE, UploadRunner, clock, ready, upload


MISSING = 'The requested resource was not found.'


def not_found(service_code=6, **metadata):
    details={'code':'service.6','class':'invalid_input','serviceCode':service_code,'message':MISSING}
    details.update(metadata)
    return CanvasCLIError('service.6',details['message'],details={'error':details},exit_code=2)


def assert_original_ids_and_no_replay(runner):
    uploads=[args for args,_,_ in runner.calls if args[:2]==['resource','upload']]
    reads=[args for args,_,_ in runner.calls if args[:2]==['resource','get']]
    assert len(uploads)==1
    assert uploads[0][uploads[0].index('--resource-id')+1]=='original-resource'
    assert all(args[2:]==['original-resource','--project-id','original-project'] for args in reads)
    assert all(args[:2] in (['resource','upload'],['resource','get']) for args,_,_ in runner.calls)


@pytest.mark.parametrize('ack', [False,True])
@pytest.mark.parametrize('service_code', [6,'6'])
@pytest.mark.parametrize('metadata', [{},{'requiredAction':'fix_input','retryable':False}])
def test_ack_and_unknown_uploads_observe_missing_original_resource_until_ready(clock,ack,service_code,metadata):
    runner=UploadRunner(clock,[not_found(service_code,**metadata),not_found(service_code,**metadata),ready()],upload_succeeds=ack)
    upload(CanvasClient(runner,ACCOUNT))
    assert clock['now']==2
    assert len(runner.calls)==4
    assert_original_ids_and_no_replay(runner)


@pytest.mark.parametrize('ack', [False,True])
@pytest.mark.parametrize('deadline', [3,300])
def test_permanent_missing_is_bounded_pending_or_original_unknown_without_reupload(clock,ack,deadline):
    runner=UploadRunner(clock,[not_found()],upload_succeeds=ack)
    with pytest.raises(CanvasCLIError) as caught:
        upload(CanvasClient(runner,ACCOUNT),deadline=deadline)
    assert caught.value.code==('MEDIA_UPLOAD_PENDING' if ack else 'cli.upload_state_unknown')
    if not ack:assert caught.value.message==UPLOAD_MESSAGE
    assert clock['now']==min(120,deadline)
    assert len(runner.calls)<=1+min(120,deadline)
    assert all(passed is None or passed<=deadline for _,_,passed in runner.calls)
    assert_original_ids_and_no_replay(runner)


@pytest.mark.parametrize('ack', [False,True])
@pytest.mark.parametrize('wrong', ['id','type'])
def test_missing_then_wrong_resource_or_type_is_not_accepted(clock,ack,wrong):
    invalid=ready('another-resource') if wrong=='id' else {**ready(),'type':'video'}
    runner=UploadRunner(clock,[not_found(),invalid],upload_succeeds=ack)
    with pytest.raises(CanvasCLIError) as caught:
        upload(CanvasClient(runner,ACCOUNT))
    assert caught.value.code=='CLI_RESOURCE_MISMATCH'
    assert len(runner.calls)==3
    assert_original_ids_and_no_replay(runner)


@pytest.mark.parametrize('ack', [False,True])
@pytest.mark.parametrize('error', [
    not_found(message='Invalid resource request parameters.'),
    not_found(service_code=7),
    not_found(**{'class':'authentication_failure'}),
    not_found(reason='identity_mismatch'),
    not_found(resourceId='another-resource'),
    not_found(requiredAction='human_intervention'),
    not_found(requiredAction='login'),
    not_found(requiredAction='resume'),
    CanvasCLIError('cli.authentication_required','Authentication required'),
    CanvasCLIError('CLI_ACCOUNT_MISMATCH','Wrong account'),
    CanvasCLIError('cli.manual_action_required','Needs human action',details={'error':{'requiredAction':'human_intervention'}}),
])
def test_other_invalid_auth_mismatch_and_human_errors_stop_immediately(clock,ack,error):
    runner=UploadRunner(clock,[error,ready()],upload_succeeds=ack)
    with pytest.raises(CanvasCLIError) as caught:
        upload(CanvasClient(runner,ACCOUNT))
    assert caught.value is error
    assert len(runner.calls)==2 and clock['now']==0
    assert_original_ids_and_no_replay(runner)


def test_exact_missing_is_not_a_global_call_retry_rule(clock):
    runner=UploadRunner(clock,[not_found(),ready()],upload_succeeds=True)
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner,ACCOUNT)._call(['resource','get','original-resource','--project-id','original-project'],
                                         read_only=True,deadline=150)
    assert caught.value.code=='service.6' and len(runner.calls)==1 and clock['now']==0


@pytest.mark.parametrize('ack', [False,True])
@pytest.mark.parametrize('wrong', ['id','type'])
def test_ready_identity_and_present_type_are_checked_without_prior_not_found(clock,ack,wrong):
    invalid=ready('another-resource') if wrong=='id' else {**ready(),'type':'audio'}
    runner=UploadRunner(clock,[invalid],upload_succeeds=ack)
    with pytest.raises(CanvasCLIError) as caught:
        upload(CanvasClient(runner,ACCOUNT))
    assert caught.value.code=='CLI_RESOURCE_MISMATCH' and len(runner.calls)==2
    assert_original_ids_and_no_replay(runner)


def test_optional_absent_type_does_not_invent_a_required_sdk_field(clock):
    runner=UploadRunner(clock,[not_found(),{'resourceId':'original-resource','status':'success'}],upload_succeeds=True)
    upload(CanvasClient(runner,ACCOUNT))
    assert_original_ids_and_no_replay(runner)


def test_acknowledged_processing_timeout_never_reuploads_even_without_not_found(clock):
    runner=UploadRunner(clock,[{'resourceId':'original-resource','status':'running'}],upload_succeeds=True)
    with pytest.raises(CanvasCLIError) as caught:
        upload(CanvasClient(runner,ACCOUNT),deadline=300)
    assert caught.value.code=='MEDIA_UPLOAD_PENDING' and clock['now']==120
    assert_original_ids_and_no_replay(runner)


def test_existing_retryable_upload_itself_lost_receipt_keeps_same_id_retry_policy(clock):
    class RetryUpload(UploadRunner):
        def __init__(self):
            super().__init__(clock,[ready()])
            self.upload_calls=0

        def run(self,account,args,timeout=None,deadline=None):
            if args[:2]==['resource','upload']:
                self.upload_calls+=1
                if self.upload_calls==1:
                    self.calls.append((list(args),timeout,deadline))
                    raise CanvasCLIError('CLI_COMMAND_TIMEOUT','Synthetic lost upload response')
                self.upload_succeeds=True
            elif not self.upload_succeeds:
                self.calls.append((list(args),timeout,deadline))
                raise not_found()
            return super().run(account,args,timeout=timeout,deadline=deadline)

    runner=RetryUpload()
    upload(CanvasClient(runner,ACCOUNT))
    writes=[args for args,_,_ in runner.calls if args[:2]==['resource','upload']]
    assert len(writes)==2 and all(args[-1]=='original-resource' for args in writes)
    assert all(args[2:]==['original-resource','--project-id','original-project']
               for args,_,_ in runner.calls if args[:2]==['resource','get'])
    assert clock['now']==5.5


@pytest.mark.parametrize('probe,code', [
    (ready('another-resource'),'CLI_RESOURCE_MISMATCH'),
    ({**ready(),'type':'video'},'CLI_RESOURCE_MISMATCH'),
    (CanvasCLIError('cli.manual_action_required','Manual action',
        details={'error':{'requiredAction':'human_intervention'}}),'cli.manual_action_required'),
    (CanvasCLIError('cli.authentication_required','Authentication required'),'cli.authentication_required'),
    (CanvasCLIError('CLI_ACCOUNT_MISMATCH','Wrong account'),'CLI_ACCOUNT_MISMATCH'),
])
def test_lost_upload_receipt_does_not_swallow_invalid_or_intervention_probe(clock,probe,code):
    class LostResponse(UploadRunner):
        def run(self,account,args,timeout=None,deadline=None):
            if args[:2]==['resource','upload']:
                self.calls.append((list(args),timeout,deadline))
                raise CanvasCLIError('CLI_COMMAND_TIMEOUT','Synthetic lost upload response')
            return super().run(account,args,timeout=timeout,deadline=deadline)

    runner=LostResponse(clock,[probe,ready()])
    with pytest.raises(CanvasCLIError) as caught:
        upload(CanvasClient(runner,ACCOUNT))
    assert caught.value.code==code and len(runner.calls)==2 and clock['now']==0
    assert_original_ids_and_no_replay(runner)
