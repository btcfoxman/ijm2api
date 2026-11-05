"""Hosted-runner-only comparison of wrappers using no-network CLI metadata or true.

Never supplies credentials, account profiles, nodes or generation arguments.
The dead loopback HTTP proxy deliberately prevents accidental external routing.
"""
import argparse,collections,concurrent.futures,hashlib,json,os,pathlib,signal,subprocess,tempfile,time

def one(binary,command,job,home,threads,timeout):
    env=dict(os.environ)
    for key in list(env):
        if any(s in key.upper()for s in ('TOKEN','SECRET','PASSWORD','COOKIE')):
            env.pop(key,None)
    env.update(HOME=home,XDG_CONFIG_HOME=home,XDG_CACHE_HOME=home,GOMAXPROCS=str(threads),
               HTTP_PROXY='http://127.0.0.1:9',HTTPS_PROXY='http://127.0.0.1:9',ALL_PROXY='http://127.0.0.1:9',NO_PROXY='')
    cmd=[binary,'--config','/dev/null','--select_proxy_mode','only_http_proxy','--http_proxy','127.0.0.1:9','--',*command]
    started=time.monotonic()
    p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       env=env,cwd=home,start_new_session=True)
    timed_out=False;pre_cleanup_rc=None
    try:
        out,err=p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out=True;pre_cleanup_rc=p.poll()
        # This process group was created exclusively for this one test attempt.
        try:
            os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        out,err=p.communicate(timeout=2)
    version_ok=p.returncode==0
    if command!=['/bin/true']:
        try:version_ok=version_ok and json.loads(out).get('ok')is True
        except (ValueError,AttributeError):version_ok=False
    return {'job':job,'returncode':p.returncode,'timeout':timed_out,'returncode_before_timeout_cleanup':pre_cleanup_rc,
            'elapsed_ms':round((time.monotonic()-started)*1000,3),'stdout_bytes':len(out),'stderr_bytes':len(err),
            'stdout_sha256':hashlib.sha256(out).hexdigest(),'stderr_sha256':hashlib.sha256(err).hexdigest(),
            'ptrace_diagnostic':b'ptrace'in err.lower(),'permission_diagnostic':b'not permitted'in err.lower(),
            'stderr_text':err[:4096].decode('utf-8',errors='replace')if err else '',
            'version_ok':version_ok}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--original',required=True);ap.add_argument('--patched',required=True)
    ap.add_argument('--cli');ap.add_argument('--release');ap.add_argument('--count',type=int,default=500);ap.add_argument('--workers',type=int,default=8)
    ap.add_argument('--timeout',type=float,default=5);ap.add_argument('--output',required=True)
    a=ap.parse_args()
    if not(os.name=='posix'and 1<=a.workers<=8 and 1<=a.count<=4096 and 1<=a.timeout<=15):
        raise ValueError('Canvas proxy stress bounds are invalid')
    if os.getenv('GITHUB_ACTIONS')!='true'or os.getenv('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('Canvas proxy stress validation requires a GitHub hosted runner')
    results={'generation_requests':0,'account_queries':0,'configuration_changes':0,'external_network_required':False,'rounds':[]}
    commands=[('true',['/bin/true'])]
    if a.cli:commands.append(('native-version',[str(pathlib.Path(a.cli).resolve()),'--format','json','version']))
    wrappers=[('rebuilt-unpatched',a.original),('thread-pinned',a.patched)]
    if a.release:wrappers.insert(0,('official-release',a.release))
    for threads in (2,8):
        for label,binary in wrappers:
            binary=str(pathlib.Path(binary).resolve())
            for test,command in commands:
                with tempfile.TemporaryDirectory(prefix='graftcp-offline-')as home:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers)as pool:
                        rows=list(pool.map(lambda j:one(binary,command,j,home,threads,a.timeout),range(a.count)))
                rc=collections.Counter(str(r['returncode'])for r in rows)
                failures=[r for r in rows if r['returncode']!=0 or r['timeout']or not r['version_ok']or r['ptrace_diagnostic']]
                results['rounds'].append({'wrapper':label,'binary_sha256':hashlib.sha256(pathlib.Path(binary).read_bytes()).hexdigest(),
                    'test':test,'gomaxprocs':threads,'count':a.count,'workers':a.workers,'returncodes':dict(rc),
                    'failure_count':len(failures),'ptrace_failure_count':sum(r['ptrace_diagnostic']for r in failures),
                    'ptrace_diagnostic_count':sum(r['ptrace_diagnostic']for r in rows),
                    'timeout_count':sum(r['timeout']for r in rows),'failure_samples':failures[:12],
                    'maximum_ms':max(r['elapsed_ms']for r in rows)})
                pathlib.Path(a.output).write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
                print(json.dumps({k:v for k,v in results['rounds'][-1].items()if k!='failure_samples'}),flush=True)
    assert not any(r['failure_count']for r in results['rounds']if r['wrapper']=='thread-pinned'),'PatchedWrapperOfflineValidationFailed'
if __name__=='__main__':main()
