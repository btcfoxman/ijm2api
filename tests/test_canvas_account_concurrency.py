"""Hermetic account-lane tests; subprocess bodies are local Python only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import copy
import hashlib
import json
import subprocess
import sys
import threading
import time

import pytest

from app.canvas_cli import CanvasCLI, CanvasCLIError
from app.canvas_client import CanvasClient
from app.canvas_timing import PreparationTiming


ACCOUNT={'id':19,'proxy_url':'http://127.0.0.1:9','login_user_id':'fake-user'}


def wait_until(predicate, timeout=2):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.005)
    assert predicate(), 'Local fixture did not reach the expected boundary'


def test_same_account_network_commands_overlap_with_timing(tmp_path,monkeypatch):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True)
    barrier=threading.Barrier(2)
    def execute(account,args,timeout,**kwargs):
        barrier.wait(1)
        return {'ok':True,'data':{'operationRef':args[-1]}}
    monkeypatch.setattr(runner,'_execute',execute)
    def task(name):
        client=CanvasClient(runner,ACCOUNT);client.preparation_timing=PreparationTiming(name)
        result=client._call(['operation','status',name],deadline=time.monotonic()+1.5)
        return result,client.preparation_timing.snapshot()
    try:
        with ThreadPoolExecutor(2) as pool:
            rows=list(pool.map(task,['one','two']))
        assert [item[0]['operationRef'] for item in rows]==['one','two']
        assert all(item[1]['recent_commands'][0]['outcome']=='success' for item in rows)
    finally:runner.close()


@pytest.mark.parametrize('auth_command',[['auth','refresh'],['auth','login'],['auth','wait'],['auth','logout'],['auth','import'],['auth','future-command']])
def test_auth_writer_excludes_readers_but_not_another_account(tmp_path,monkeypatch,auth_command):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True)
    entered,release=threading.Event(),threading.Event();calls=[]
    def execute(account,args,timeout,**kwargs):
        calls.append((account['id'],tuple(args)))
        if args==auth_command:
            entered.set();assert release.wait(2)
        return {'ok':True,'data':{}}
    monkeypatch.setattr(runner,'_execute',execute)
    try:
        with ThreadPoolExecutor(3) as pool:
            auth=pool.submit(runner.run,ACCOUNT,auth_command)
            assert entered.wait(1)
            read=pool.submit(runner.run,ACCOUNT,['resource','get','one'],deadline=time.monotonic()+1)
            other=pool.submit(runner.run,{**ACCOUNT,'id':20},['resource','get','other'])
            assert other.result(1)=={'ok':True,'data':{}}
            assert not read.done()
            release.set();auth.result(1);read.result(1)
        assert calls.index((19,tuple(auth_command)))<calls.index((19,('resource','get','one')))
    finally:release.set();runner.close()


def test_queued_auth_cannot_deadlock_nested_confirm_run_reader(tmp_path):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True)
    requested,writer_entered=threading.Event(),threading.Event()
    def writer():
        requested.set()
        with runner.account_guard(ACCOUNT,exclusive=True,deadline=time.monotonic()+1):writer_entered.set()
    try:
        with runner.account_guard(ACCOUNT,deadline=time.monotonic()+1):
            thread=threading.Thread(target=writer);thread.start();assert requested.wait(1)
            wait_until(lambda:runner._lane(19)._waiting_writers==1)
            with runner.command_guard(ACCOUNT,['node','run','--project-id','original-project'],deadline=time.monotonic()+.5):
                assert not writer_entered.is_set()
        thread.join(1);assert writer_entered.is_set() and not thread.is_alive()
    finally:runner.close()


@pytest.mark.parametrize('unknown_node',[None,'one'])
def test_two_confirmation_tokens_and_single_runs_never_cross_tasks(tmp_path,monkeypatch,unknown_node):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True)
    profile=runner.profile(ACCOUNT);confirmed=threading.Barrier(2);calls=[];persisted={}
    def execute(account,args,timeout,**kwargs):
        node=args[args.index('--node-id')+1]
        if args[:2]==['node','confirm']:
            confirmed.wait(1)
            return {'ok':True,'data':{'creditConfirmationToken':'token-'+node,'expiresAt':'2099-01-01T00:00:00Z'}}
        assert args[:2]==['node','run']
        assert args[args.index('--credit-token')+1]=='token-'+node
        submit=args[args.index('--submit-id')+1];assert submit=='submit-'+node
        calls.append((node,submit))
        if node==unknown_node:
            raise CanvasCLIError('cli.invalid_service_response','synthetic uncertain service reply',outcome_unknown=True)
        return {'ok':True,'data':{'items':[{'nodeId':node,'submitId':submit,'state':'RUN_ACCEPTED','resources':[{'resourceId':'resource-'+node}]}]}}
    monkeypatch.setattr(runner,'_execute',execute)
    def task(node):
        context={'account_id':19,'profile':profile,'task_id':node,'project_id':'project-'+node,'node_id':node,'submit_id':'submit-'+node,'quoted_credits':10,'phase':'quoted'}
        persisted[node]=[]
        try:
            return CanvasClient(runner,ACCOUNT).submit(context,10,persist=lambda value:persisted[node].append(copy.deepcopy(value)),deadline=time.monotonic()+2)
        except CanvasCLIError as error:return error
    try:
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(task,['one','two']))
        assert sorted(calls)==[('one','submit-one'),('two','submit-two')]
        if unknown_node:
            assert isinstance(results[0],CanvasCLIError) and results[0].outcome_unknown
            assert persisted['one'][-1]['submit_id']=='submit-one'
            assert persisted['one'][-1]['submission_phase']=='run_dispatching'
            assert persisted['one'][-1]['submit_error']['outcome_unknown'] is True
            assert persisted['one'][-1]['submit_error']['dispatch_started'] is True
            assert not persisted['one'][-1].get('resource_ids')
        else:assert results[0]['resource_ids']==['resource-one']
        assert results[1]['resource_ids']==['resource-two']
        assert all('token-' not in json.dumps(items) for items in persisted.values())
    finally:runner.close()


def test_profile_invalidation_waits_for_active_sequence_and_retires_only_its_account(tmp_path):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True)
    original=runner.profile(ACCOUNT);other={**ACCOUNT,'id':20};other_profile=runner.profile(other)
    requested=threading.Event()
    def invalidate():requested.set();runner.invalidate(19)
    try:
        with ThreadPoolExecutor(1) as pool:
            with runner.account_guard(ACCOUNT,deadline=time.monotonic()+1):
                pending=pool.submit(invalidate);assert requested.wait(1)
                wait_until(lambda:runner._lane(19)._waiting_writers==1)
                assert not pending.done() and runner.profile(ACCOUNT)==original
                assert runner.profile(other)==other_profile
            pending.result(1)
        assert runner.profile(ACCOUNT)!=original and runner.profile(other)==other_profile
    finally:runner.close()


def test_same_project_confirm_run_is_short_exclusive_but_upload_reads_stay_parallel(tmp_path,monkeypatch):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True);profile=runner.profile(ACCOUNT)
    entered,release=threading.Event(),threading.Event();calls=[]
    def execute(account,args,timeout,**kwargs):
        if args[:2]==['resource','get']:return {'ok':True,'data':{'resourceId':'independent-input'}}
        node=args[args.index('--node-id')+1]
        if args[:2]==['node','confirm']:
            calls.append('confirm-'+node)
            if node=='one':entered.set();assert release.wait(2)
            return {'ok':True,'data':{'creditConfirmationToken':'token-'+node,'expiresAt':'2099-01-01T00:00:00Z'}}
        assert args[args.index('--credit-token')+1]=='token-'+node
        calls.append('run-'+node)
        return {'ok':True,'data':{'items':[{'nodeId':node,'submitId':'submit-'+node,'state':'accepted','resources':[]}]}}
    monkeypatch.setattr(runner,'_execute',execute)
    def submit(node):
        context={'account_id':19,'profile':profile,'project_id':'same-project','node_id':node,'submit_id':'submit-'+node,'quoted_credits':10,'phase':'quoted'}
        return CanvasClient(runner,ACCOUNT).submit(context,10,deadline=time.monotonic()+2)
    try:
        with ThreadPoolExecutor(3) as pool:
            first=pool.submit(submit,'one');assert entered.wait(1)
            second=pool.submit(submit,'two')
            independent=pool.submit(runner.run,ACCOUNT,['resource','get','independent-input'])
            assert independent.result(1)['data']['resourceId']=='independent-input'
            assert not second.done() and calls==['confirm-one']
            release.set();first.result(1);second.result(1)
        assert calls==['confirm-one','run-one','confirm-two','run-two']
    finally:release.set();runner.close()


def test_draft_save_quote_pair_excludes_only_same_project_writers(tmp_path,monkeypatch):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True);entered,release=threading.Event(),threading.Event();calls=[]
    mode={'name':'t2v','flags':[{'flag':'--ratio','values':['16:9']},{'flag':'--resolution','values':['720p']},{'flag':'--duration','min':4,'max':15,'step':1}],'references':{'min':0,'max':15,'types':[]}}
    selected={'model':'seedance_2.0_mini','modes':[mode]}
    node_names={}
    def execute(account,args,timeout,**kwargs):
        if args[:2]==['resource','get']:return {'ok':True,'data':{}}
        node=args[args.index('--node-id')+1];name=node_names[node]
        if args[:2]==['node','create']:
            calls.append('save-'+name)
            if name=='one':entered.set();assert release.wait(2)
            return {'ok':True,'data':{'node':{'nodeId':node}}}
        assert args[:2]==['node','quote'];calls.append('quote-'+name)
        return {'ok':True,'data':{'items':[{'nodeId':node,'maxCredits':10}],'totalMaxCredits':10,'confirmable':True}}
    monkeypatch.setattr(runner,'_execute',execute)
    def prepare(name):
        client=CanvasClient(runner,ACCOUNT)
        monkeypatch.setattr(client,'preflight_model',lambda *args,**kwargs:selected)
        monkeypatch.setattr(client,'_ensure_project',lambda *args,**kwargs:None)
        def persist(value):node_names[value['node_id']]=name
        return client.prepare({'model':'sd-2-0-mini','prompt':'local fixture','duration':4,'resolution':'720p','aspect_ratio':'16:9'},name,{},persist=persist,deadline=time.monotonic()+2)
    try:
        with ThreadPoolExecutor(3) as pool:
            first=pool.submit(prepare,'one');assert entered.wait(1)
            second=pool.submit(prepare,'two')
            assert pool.submit(runner.run,ACCOUNT,['resource','get','independent']).result(1)=={'ok':True,'data':{}}
            assert calls==['save-one'];release.set()
            contexts=[first.result(1),second.result(1)]
        assert contexts[0]['project_id']==contexts[1]['project_id']
        assert calls==['save-one','quote-one','save-two','quote-two']
    finally:release.set();runner.close()


def runtime_schema():
    commands={
        'auth account':[], 'auth status':[], 'auth login':['force'], 'auth wait':['device-code','timeout'],'auth refresh':[],
        'model list':['type'],'canvas ls':['limit','cursor'],'canvas create':['project-id'],
        'resource upload':['file','type','project-id','resource-id'],'resource get':['project-id'],'resource download':['project-id','output'],
        'node quote':['project-id','node-id'],'node confirm':['project-id','node-id','credit-ceiling'],
        'node run':['project-id','node-id','submit-id','credit-token'],'node show':['project-id','node-id'],
        'operation status':['project-id'],
    }
    common=['project-id','node-id','update-id','title','mode','model','prompt','ratio','resolution','ref']
    commands['node create image']=common+['count'];commands['node create video']=common+['duration']
    root={}
    for path,flags in commands.items():
        children=root
        for name in path.split():
            node=children.setdefault(name,{'name':name,'flags':[],'children':{}});children=node['children']
        node['flags']=[{'name':flag} for flag in flags]
    def nodes(children):return [{'name':node['name'],'flags':node['flags'],'subcommands':nodes(node['children'])} for node in children.values()]
    return {'distribution':'global','schemaVersion':'1','globalFlags':[{'name':name} for name in ['format','non-interactive','profile']],'subcommands':nodes(root)}


@pytest.mark.parametrize('pin_matches,distribution,parallel',[(True,'global',True),(False,'global',False),(True,'cn',False)])
def test_parallel_requires_both_pinned_artifact_and_verified_global_public(tmp_path,monkeypatch,pin_matches,distribution,parallel):
    binary=tmp_path/'binary';binary.write_bytes(b'not executed fixture')
    digest=hashlib.sha256(binary.read_bytes()).hexdigest()
    monkeypatch.setattr('app.canvas_cli.shutil.which',lambda name:str(binary))
    monkeypatch.setattr('app.canvas_cli.PINNED_GLOBAL_LINUX_SHA256',digest if pin_matches else '0'*64)
    runner=CanvasCLI(tmp_path/'state')
    assert not runner._parallel_verified
    monkeypatch.setattr(runner,'run',lambda account,args,**kwargs:{'data':{'version':'1.2.0','distribution':distribution,'edition':'public'} if args==['version'] else runtime_schema()})
    try:
        if distribution!='global':
            with pytest.raises(CanvasCLIError):runner.verify_runtime()
        else:assert runner.verify_runtime()['parallel_account_commands'] is parallel
        assert runner._parallel_verified is parallel
    finally:runner.close()


def test_unknown_binary_keeps_same_account_business_serial(tmp_path,monkeypatch):
    monkeypatch.setattr('app.canvas_cli.shutil.which',lambda name:None)
    runner=CanvasCLI(tmp_path);entered,release=threading.Event(),threading.Event();calls=[]
    def execute(account,args,timeout,**kwargs):
        calls.append(args[-1])
        if args[-1]=='one':entered.set();assert release.wait(2)
        return {'ok':True,'data':{}}
    monkeypatch.setattr(runner,'_execute',execute)
    try:
        with ThreadPoolExecutor(2) as pool:
            first=pool.submit(runner.run,ACCOUNT,['operation','status','one']);assert entered.wait(1)
            second=pool.submit(runner.run,ACCOUNT,['operation','status','two'])
            wait_until(lambda:runner._lane(19)._waiting_writers==1)
            assert calls==['one'] and not second.done()
            release.set();first.result(1);second.result(1)
        assert calls==['one','two']
    finally:release.set();runner.close()


@pytest.mark.parametrize('args',[['node','create'],['node','run','--project-id=one','--project-id=two']])
def test_unbound_or_ambiguous_node_write_cannot_bypass_account_exclusion(tmp_path,monkeypatch,args):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True);entered,release=threading.Event(),threading.Event();calls=[]
    monkeypatch.setattr(runner,'_execute',lambda *args,**kwargs:calls.append(args))
    def reader():
        with runner.account_guard(ACCOUNT):entered.set();release.wait(1)
    thread=threading.Thread(target=reader);thread.start();assert entered.wait(1)
    try:
        with pytest.raises(CanvasCLIError) as blocked:
            runner.run(ACCOUNT,args,deadline=time.monotonic()+.02)
        assert blocked.value.code=='CLI_TASK_DEADLINE_EXCEEDED' and not calls
    finally:release.set();thread.join(1);runner.close()


def test_close_cancels_all_registered_accounts(tmp_path,monkeypatch):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True)
    first,second,other=object(),object(),object()
    runner._processes={19:{first,second},20:{other}}
    stopped=[];monkeypatch.setattr(runner,'_terminate',stopped.append)
    runner.close()
    assert set(stopped)=={first,second,other} and runner._closed.is_set()


def test_waiter_deadline_and_close_never_start_a_command(tmp_path,monkeypatch):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True);calls=[]
    monkeypatch.setattr(runner,'_execute',lambda *args,**kwargs:calls.append(args))
    locked,release=threading.Event(),threading.Event()
    def writer():
        with runner.account_guard(ACCOUNT,exclusive=True):locked.set();release.wait(2)
    thread=threading.Thread(target=writer);thread.start();assert locked.wait(1)
    try:
        with pytest.raises(CanvasCLIError) as deadline:
            runner.run(ACCOUNT,['resource','get','one'],deadline=time.monotonic()+.02)
        assert deadline.value.code=='CLI_TASK_DEADLINE_EXCEEDED'
        with ThreadPoolExecutor(1) as pool:
            waiter=pool.submit(runner.run,ACCOUNT,['resource','get','two'])
            runner.close()
            with pytest.raises(CanvasCLIError) as stopped:waiter.result(1)
        assert stopped.value.code=='CLI_STOPPING' and calls==[]
    finally:release.set();thread.join(1);runner.close()


def toy_spawner(monkeypatch,tmp_path,runner):
    real=subprocess.Popen;children=[]
    code="import json,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.with_suffix('.ready').write_text('ready'); end=time.monotonic()+5\nwhile not p.exists() and time.monotonic()<end:time.sleep(.005)\nprint(json.dumps({'ok':True,'data':{'tag':p.name}}),flush=True)"
    def spawn(command,**kwargs):
        if command[0]!=runner.binary:return real(command,**kwargs)
        tag=command[-1]
        child=real([sys.executable,'-c',code,str(tmp_path/tag)],**kwargs);children.append(child)
        return child
    monkeypatch.setattr('app.canvas_cli.subprocess.Popen',spawn)
    return children


def test_cancel_tracks_all_same_account_processes_and_keeps_other_account(tmp_path,monkeypatch):
    runner=CanvasCLI(tmp_path,timeout=3,allow_unwrapped_for_tests=True)
    children=toy_spawner(monkeypatch,tmp_path,runner)
    def command(account,tag):
        try:return runner.run(account,['operation','status',tag])
        except CanvasCLIError as error:return error
    try:
        with ThreadPoolExecutor(3) as pool:
            first=pool.submit(command,ACCOUNT,'one');second=pool.submit(command,ACCOUNT,'two')
            third=pool.submit(command,{**ACCOUNT,'id':20},'other')
            wait_until(lambda:len(runner._processes.get(19,()))==2 and len(runner._processes.get(20,()))==1)
            runner.cancel(19)
            assert isinstance(first.result(2),CanvasCLIError) and isinstance(second.result(2),CanvasCLIError)
            assert not third.done() and len(runner._processes.get(20,()))==1
            (tmp_path/'other').touch();assert third.result(2)['data']['tag']=='other'
        assert not runner._processes
    finally:
        (tmp_path/'other').touch();runner.close()
        for child in children:
            if child.poll() is None:child.kill()
            child.wait(timeout=2)


def test_close_during_spawn_does_not_leave_an_unregistered_paid_process(tmp_path,monkeypatch):
    runner=CanvasCLI(tmp_path,allow_unwrapped_for_tests=True)
    real=subprocess.Popen;children=[]
    def spawn(command,**kwargs):
        if command[0]!=runner.binary:return real(command,**kwargs)
        child=real([sys.executable,'-c','import time;time.sleep(5)'],**kwargs);children.append(child)
        runner.close()  # No process has reached registration yet.
        return child
    monkeypatch.setattr('app.canvas_cli.subprocess.Popen',spawn)
    try:
        with pytest.raises(CanvasCLIError) as stopped:runner.run(ACCOUNT,['node','run'])
        assert stopped.value.code=='CLI_STOPPING' and stopped.value.outcome_unknown is True
        assert len(children)==1 and children[0].poll() is not None and not runner._processes
    finally:
        runner.close()
        for child in children:
            if child.poll() is None:child.kill()
            child.wait(timeout=2)
