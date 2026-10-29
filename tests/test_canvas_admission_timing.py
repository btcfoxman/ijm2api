"""Queue time begins at shared admission, not at a later execution backend."""
import time
from unittest.mock import patch

import pytest

from app.browser import BrowserRejected
from app.canvas_timing import PreparationTiming
from test_canvas_execution import context, fake_client, owner, task
from test_mixed_execution_admission import mixed


@pytest.mark.parametrize('kind',['video','image'])
def test_browser_preparation_time_is_not_reported_as_cli_queue(mixed,monkeypatch,kind):
    clock={'now':1000.0}
    monkeypatch.setattr(time,'time',lambda:clock['now'])
    browser=mixed.add('browser-first',mode='browser',priority=9,kind=kind)
    cli=mixed.add('cli-second',mode='canvas_cli',priority=8,kind=kind)
    identifier=mixed.create(kind)
    def prepare(*args,**kwargs):
        assert mixed.store.task(identifier)['upstream_context']['preparation_admission_at']==1000
        clock['now']+=120
        return {'images':[],'videos':[],'audios':[]}
    mixed.browser[browser['id']].upload_inputs.side_effect=prepare
    mixed.browser[browser['id']].submit.side_effect=BrowserRejected('not enough credits left')
    mixed.service._run(identifier)
    row=mixed.store.task(identifier)
    assert row['status']=='succeeded' and row['account_id']==cli['id']
    timing=row['upstream_context']['preparation_timing']
    assert timing['first_admission_queue_ms']==timing['queue_wait_ms']==0
    assert timing['elapsed_before_attempt_ms']==120000
    assert row['upstream_context']['preparation_admission_at']==1000


@pytest.mark.parametrize('kind',['video','image'])
def test_legacy_prior_attempt_without_marker_has_unknown_queue_not_a_fake_epoch(context,monkeypatch,kind):
    store,service,_=context;clock={'now':1000.0};monkeypatch.setattr(time,'time',lambda:clock['now'])
    account=owner(store)
    row=(service.create_image({'model':'gpt-image-2.5-flare','prompt':'fixture'}) if kind=='image' else task(store))
    identifier=row['id'];store.update_task(identifier,{'attempted_account_ids':[99]})
    client=fake_client(store,service,account,kind=kind)
    clock['now']=1120
    with patch.object(service,'canvas_client',return_value=client):service._run(identifier)
    final=store.task(identifier)
    assert final['status']=='succeeded'
    timing=final['upstream_context']['preparation_timing']
    assert timing['queue_wait_ms'] is None and timing['first_admission_queue_ms'] is None
    assert timing['elapsed_before_attempt_ms']==120000
    assert final['upstream_context']['preparation_admission_at'] is None
    assert client.submit.call_count==1


def test_timing_none_is_explicitly_unknown_while_omitted_marker_is_first_attempt():
    options={'queued_at':100,'wall_time':lambda:200,'monotonic':lambda:0}
    assert PreparationTiming('task',first_admission_at=None,**options).snapshot()['queue_wait_ms'] is None
    assert PreparationTiming('task',**options).snapshot()['queue_wait_ms']==100000
