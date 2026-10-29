"""Account quotes predict admission only; paid generation always re-quotes."""
from copy import deepcopy
import time
from unittest.mock import patch

import pytest

from app.canvas_quote_hints import new_hint, matching_hint, TTL_SECONDS
from app.catalog import normalize_request
from test_canvas_execution import context, owner, task, fake_client


def normalized(**overrides):
    return normalize_request({'model':'sd-2-0-mini','duration':4,'prompt':'fixture',
        'image_urls':['https://fixture.example/one.png'],**overrides})


def epoch(service, account, value='fixture-credential-epoch'):
    path=service.canvas_cli.account_directory(account)/'credential-profile'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)


def quoted(account, amount, **overrides):
    return {'backend':'canvas_cli','account_id':account['id'],'phase':'quoted',
        'model':'seedance_2.0_mini','mode':'m2v','quoted_credits':amount,
        'quote':{'confirmable':True,'total_max_credits':amount},**overrides}


def seed(store,service,account,amount,body=None):
    epoch(service,account)
    hint=new_hint(account,service.canvas_cli.data_dir,body or normalized(),
        quoted(account,amount),account['login_user_id'])
    assert hint is not None;store.save_canvas_quote_hint(hint)
    return hint


def target(store,identifier='target',**overrides):
    return task(store,identifier,image_urls=['https://fixture.example/one.png'],**overrides)


def test_known_account44_does_not_fit42_and_does_not_override_account40(context):
    store,service,_=context
    big=owner(store,'big',balance=44743);small=owner(store,'small',balance=42)
    seed(store,service,big,40);seed(store,service,small,44)
    row=target(store);before=store.task(row['id'])
    selected=service._reserve_account(0,task_id=row['id'])
    assert selected['id']==big['id'] and selected['_scheduling_quote_amount']==40
    assert store.task(row['id'])==before
    assert {h['account_id']:h['amount'] for h in store.canvas_quote_hints()}=={big['id']:40,small['id']:44}
    service._release_account(big['id'],row['id'])


@pytest.mark.parametrize('field,value',[('identity_epoch','bad-epoch'),('observed_at',0),
    ('observed_at',time.time()+86400),('model','unrelated-model'),('mode','t2v'),('amount',0),('source','actual_cost')])
def test_invalid_epoch_time_model_mode_or_nonquote_does_not_match(context,field,value):
    store,service,_=context;account=owner(store);hint=seed(store,service,account,40)
    hint[field]=value
    assert not matching_hint(hint,account,service.canvas_cli.data_dir,normalized())


@pytest.mark.parametrize('change',[{'duration':5},{'resolution':'720p'},
    {'mode':'i2v'},{'aspect_ratio':'9:16'},{'count':2},
    {'images':['a','b']},{'videos':['v'],'_cost_evidence':{'reference_seconds':2}},
    {'model':'sd-2-0-fast'}])
def test_exact_dimensions_do_not_share_quotes(context,change):
    store,service,_=context;account=owner(store);body=normalized();hint=seed(store,service,account,40,body)
    changed={**body,**change}
    # Mini normalizes to480p; use another resolution even if defaults change.
    if change=={'resolution':'720p'}:changed['resolution']='1080p' if body['resolution']=='720p' else '720p'
    assert not matching_hint(hint,account,service.canvas_cli.data_dir,changed)


def test_reset_or_changed_verified_identity_invalidates_old_hint(context):
    store,service,_=context;account=owner(store);hint=seed(store,service,account,40)
    epoch(service,account,'another-epoch')
    assert not matching_hint(hint,account,service.canvas_cli.data_dir,normalized())
    epoch(service,account)
    assert not matching_hint(hint,{**account,'login_user_id':'changed'},service.canvas_cli.data_dir,normalized())
    assert new_hint(account,service.canvas_cli.data_dir,normalized(),quoted(account,40),'wrong-user') is None


def test_actual_zero_or_unknown_without_valid_quote_is_never_a_price(context):
    store,service,_=context;account=owner(store);epoch(service,account)
    for amount in (0,None):
        assert new_hint(account,service.canvas_cli.data_dir,normalized(),
            quoted(account,amount,actual_cost=40),account['login_user_id']) is None
    assert new_hint(account,service.canvas_cli.data_dir,normalized(),
        quoted(account,40,quote={},actual_cost=0),account['login_user_id']) is None
    hint=new_hint(account,service.canvas_cli.data_dir,normalized(),
        quoted(account,40,actual_cost=0),account['login_user_id'])
    assert hint['amount']==40 and hint['source']=='node.quote'


def test_hints_keep_priority_and_smallest_funded_balance(context):
    store,service,_=context
    high=owner(store,'high',balance=200,priority=9);small=owner(store,'small',balance=42,priority=8)
    seed(store,service,high,40);seed(store,service,small,40);row=target(store)
    assert service._reserve_account(0,task_id=row['id'])['id']==high['id']
    service._release_account(high['id'],row['id']);store.update_account(high['id'],{'priority':8})
    assert service._reserve_account(0,task_id=row['id'])['id']==small['id']
    service._release_account(small['id'],row['id'])


def test_all_unfunded_predictions_fall_back_to_original_live_quote_path(context):
    store,service,_=context
    big=owner(store,'big',balance=42);small=owner(store,'small',balance=41)
    seed(store,service,big,100);seed(store,service,small,100);row=target(store)
    selected=service._reserve_account(0,task_id=row['id'])
    assert selected['id']==big['id'] and '_scheduling_quote_amount' not in selected
    service._release_account(big['id'],row['id'])


def test_busy_hint_funded_account_does_not_starve_idle_legacy_candidate(context):
    store,service,_=context
    busy=owner(store,'busy',balance=200);idle=owner(store,'idle',balance=42)
    store.update_account(busy['id'],{'max_concurrency':1});seed(store,service,busy,40);seed(store,service,idle,100)
    service._active[busy['id']]=1;service._credit_reservations['running']=(busy['id'],40);row=target(store)
    selected=service._reserve_account(0,task_id=row['id'])
    assert selected['id']==idle['id'] and '_scheduling_quote_amount' not in selected
    service._release_account(idle['id'],row['id']);service._active.clear();service._credit_reservations.clear()


def test_hints_never_bypass_higher_priority_unverified_account(context):
    store,service,_=context
    high=owner(store,'high',balance=42,priority=9);low=owner(store,'low',balance=200,priority=8)
    seed(store,service,high,100);seed(store,service,low,40);row=target(store)
    assert service._reserve_account(0,task_id=row['id'])['id']==high['id']
    service._release_account(high['id'],row['id'])


def test_lower_priority_hint_does_not_change_unknown_higher_priority_balance_order(context):
    store,service,_=context
    high_big=owner(store,'high-big',balance=200,priority=9)
    owner(store,'high-small',balance=42,priority=9)
    low=owner(store,'low',balance=100,priority=8);seed(store,service,low,40);row=target(store)
    assert service._reserve_account(0,task_id=row['id'])['id']==high_big['id']
    service._release_account(high_big['id'],row['id'])


def test_browser_and_unknown_quote_selection_remain_unchanged(context):
    store,service,_=context
    big=owner(store,'browser-big',balance=200,mode='browser');owner(store,'browser-small',balance=42,mode='browser')
    row=target(store,backend='browser')
    selected=service._reserve_account(0,task_id=row['id']);assert selected['id']==big['id']
    service._release_account(big['id'],row['id'])


def test_failed_unpaid_preparation_learns_valid_quote_before_funding(context):
    store,service,_=context;account=owner(store,balance=42);epoch(service,account);row=target(store)
    client=fake_client(store,service,account,quote=44);original=client.prepare.side_effect
    def prepare(*args,**kwargs):
        value=original(*args,**kwargs);value.update(quoted(account,44));kwargs['persist'](value);return value
    client.prepare.side_effect=prepare
    with patch.object(service,'canvas_client',return_value=client):service._run(row['id'])
    failed=store.task(row['id']);assert failed['status']=='failed' and failed['actual_cost'] is None
    client.submit.assert_not_called()
    assert store.canvas_quote_hints()[0]['amount']==44


def test_unknown_holds_are_not_bypassed_by_positive_hints(context):
    store,service,_=context;account=owner(store);seed(store,service,account,40);row=target(store)
    service._credit_reservations['other-task']=(account['id'],0)
    assert service._reserve_account(0,task_id=row['id']) is None
    service._credit_reservations.clear()


def test_quote_table_is_time_and_size_bounded_without_changing_task_or_balance(context,monkeypatch):
    store,service,_=context;account=owner(store);row=target(store);before=deepcopy(store.task(row['id']))
    monkeypatch.setattr('app.canvas_quote_hints.MAX_HINTS',3)
    for duration in range(4,9):seed(store,service,account,40,normalized(duration=duration))
    assert len(store.canvas_quote_hints())==3
    expired=seed(store,service,account,40);expired['observed_at']=time.time()-TTL_SECONDS-1
    store.save_canvas_quote_hint(expired)
    assert all(h['observed_at']>=time.time()-TTL_SECONDS for h in store.canvas_quote_hints())
    assert store.task(row['id'])==before and store.account(account['id'],private=True)['balance']==account['balance']


def test_prediction_does_not_replace_fresh_quote_or_balance_check(context):
    store,service,_=context;account=owner(store,balance=42);seed(store,service,account,40);row=target(store)
    client=fake_client(store,service,account,quote=44);prepare=client.prepare.side_effect
    def fresh(*args,**kwargs):
        value=prepare(*args,**kwargs);value.update(quoted(account,44));kwargs['persist'](value);return value
    client.prepare.side_effect=fresh
    with patch.object(service,'canvas_client',return_value=client):service._run(row['id'])
    assert client.prepare.call_count==1;client.submit.assert_not_called()
    assert store.canvas_quote_hints()[0]['amount']==44


def test_three_preparation_rounds_keep_timings_and_initial_queue(context):
    store,service,_=context
    big=owner(store,'big',balance=44743);small=owner(store,'small',balance=42);row=target(store)
    clients={a['id']:fake_client(store,service,a,quote=q) for a,q in [(big,40),(small,44)]}
    with patch.object(service,'canvas_client',side_effect=lambda account:clients[account['id']]):service._run(row['id'])
    final=store.task(row['id']);history=final['upstream_context']['preparation_attempts']
    assert [h['account_id'] for h in history]==[big['id'],small['id'],big['id']]
    assert [h['reason'] for h in history]==['best_fit_reselect','insufficient_credits','accepted']
    assert [h['ordinal'] for h in history]==[1,2,3] and all(h['timing']['finished'] for h in history)
    assert len({h['timing']['first_admission_queue_ms'] for h in history})==1
    assert history[-1]['timing']['elapsed_before_attempt_ms']>history[0]['timing']['queue_wait_ms']
    assert clients[big['id']].submit.call_count==1 and clients[small['id']].submit.call_count==0


@pytest.mark.parametrize('status',['running','submitted','submission_unknown'])
def test_quote_cache_never_grants_resubmit_permission(context,status):
    store,service,_=context;account=owner(store);seed(store,service,account,40);row=target(store)
    store.update_task(row['id'],{'status':status,'upstream_context':{'phase':'accepted'}})
    with patch.object(service,'canvas_client') as factory:service._run(row['id'])
    factory.assert_not_called()


@pytest.mark.parametrize('which',['initial','archive','both'])
def test_optional_audit_storage_failure_cannot_replace_accepted_receipt_or_resubmit(context,which):
    store,service,_=context;account=owner(store);row=target(store)
    client=fake_client(store,service,account);original=service._canvas_context
    def audit_write(task_id,changes=None,remove=()):
        changes=changes or {}
        if (which in {'initial','both'} and set(changes)=={'preparation_admission_at'}
                or which in {'archive','both'} and 'preparation_attempts' in changes):
            raise OSError('fixture optional audit storage unavailable')
        return original(task_id,changes,remove)
    with patch.object(service,'canvas_client',return_value=client),patch.object(service,'_canvas_context',side_effect=audit_write):
        service._run(row['id'])
    final=store.task(row['id'])
    assert final['status']=='succeeded' and final['actual_cost']==10
    assert final['upstream_context']['submission_phase']=='accepted'
    assert client.submit.call_count==1 and final['attempted_account_ids']==[account['id']]
