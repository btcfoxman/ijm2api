"""Execute only no-network metadata probes in the final image on hosted Docker."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import uuid


CODE = r'''
import concurrent.futures,hashlib,json,os,pathlib,signal,subprocess
root=pathlib.Path('/usr/local/share/doc/graftcp')
receipt=json.loads((root/'build-receipt.json').read_text())
assert receipt['patch_id']=='ijm-ptrace-thread-pinning-v1'
assert receipt['source_commit']=='825cf6d3b9ec043defe8e598eb218eccc1a3eaf3'
binary=pathlib.Path('/usr/local/bin/graftcp')
assert hashlib.sha256(binary.read_bytes()).hexdigest()==receipt['executable_sha256']
commands=[['/bin/true'],['/usr/local/bin/dreamina-canvas','--format','json','version']]
def probe(index):
    command=commands[index%2]
    argv=[str(binary),'--config','/dev/null','--select_proxy_mode','only_http_proxy','--http_proxy','127.0.0.1:9','--',*command]
    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try:
        stdout,stderr=process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.communicate(timeout=2)
        raise AssertionError('FinalImageProxyProbeTimedOut')
    assert process.returncode==0,('FinalImageProxyProbeFailed',process.returncode,stderr[-1000:].decode(errors='replace'))
    assert b'ptrace'not in stderr.lower(),'FinalImageProxyPtraceDiagnostic'
    if index%2:
        data=json.loads(stdout)
        assert data.get('ok')is True and data['data']['version']=='1.2.1'
        assert data['data']['distribution']=='global'and data['data']['edition']=='public'
    return True
with concurrent.futures.ThreadPoolExecutor(max_workers=4)as pool:
    passed=list(pool.map(probe,range(64)))
print(json.dumps({'passed':sum(passed),'generation_requests':0,'account_queries':0,
    'external_network':'disabled','capabilities':'Docker defaults','seccomp':'Docker default',
    'executable_sha256':receipt['executable_sha256'],'patch_id':receipt['patch_id']}))
'''


def require_hosted() -> None:
    if (os.name != 'posix' or os.getenv('GITHUB_ACTIONS') != 'true'
            or os.getenv('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.getenv('RUNNER_OS') != 'Linux'):
        raise RuntimeError('Final Canvas proxy image smoke requires a GitHub hosted Linux runner')


def verify_image(image: str) -> dict:
    require_hosted()
    if not re.fullmatch(r'ghcr\.io/btcfoxman/ijm2api:[a-f0-9]{40}', image):
        raise ValueError('Expected one immutable IJM image tag')
    # Pin the local hosted Docker daemon; ignore any inherited remote context.
    docker = ['docker', '--host', 'unix:///var/run/docker.sock']
    name = 'ijm-proxy-offline-' + uuid.uuid4().hex
    try:
        result = subprocess.run([*docker, 'run', '--rm', '--name', name, '--pull', 'never',
            '--network', 'none', '--init', '--entrypoint', 'python', '-i', image, '-B', '-'],
            input=CODE, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError('FinalImageProxySmokeFailed:' + result.stderr[-3000:])
        proof = json.loads(result.stdout)
        if proof.get('passed') != 64 or proof.get('generation_requests') != 0:
            raise RuntimeError('FinalImageProxySmokeIncomplete')
        return proof
    finally:
        # This UUID container is the sole cleanup target, including on timeout.
        subprocess.run([*docker, 'rm', '-f', name], capture_output=True, timeout=20)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    proof = verify_image(args.image)
    if args.output:
        args.output.write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(proof))
