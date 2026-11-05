"""Select one approved same-day runtime base, or the unchanged full Dockerfile.

This tool never builds, deploys or mutates a running container. Registry reads
only resolve an immutable digest after exact source/age/ancestry checks pass.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

APPROVED_REPOSITORY = 'btcfoxman/ijm2api'
APPROVED_BASE_COMMIT = '5daa0df2b37e309b4016e933cb9f312d11397b7b'
RUNTIME_INPUTS = ('Dockerfile', '.dockerignore', 'requirements.txt',
                  'tools/install_canvas_cli.py', 'tools/install_canvas_proxy.py',
                  'tools/build_canvas_proxy.py', 'tools/stress_canvas_proxy.py',
                  'tools/test_canvas_proxy_image.py',
                  'tools/patches/graftcp-0.8.3-thread-pinning.patch')
MAX_AGE_SECONDS = 24 * 60 * 60
FETCH_DEPTH = 64
_SHA = re.compile(r'[a-f0-9]{40}\Z')
_DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')


def full_build(reason):
    return {'mode': 'full', 'reason': reason, 'dockerfile': './Dockerfile',
            'verified_runtime_image': '', 'build_args': ''}


def source_rejection(facts, *, now=None):
    now = time.time() if now is None else now
    if facts.get('repository') != APPROVED_REPOSITORY:
        return 'different_repository'
    if facts.get('base_commit') != APPROVED_BASE_COMMIT or not _SHA.fullmatch(str(facts.get('head_commit') or '')):
        return 'unapproved_commit'
    stamp = facts.get('base_timestamp')
    if type(stamp) not in (int, float) or not 0 <= now - stamp <= MAX_AGE_SECONDS:
        return 'runtime_base_outside_24_hours'
    if facts.get('ancestor') is not True:
        return 'ancestry_unproven'
    if facts.get('changed_inputs') != []:
        return 'runtime_inputs_changed_or_unknown'
    return None


def select_runtime(facts, manifest, *, now=None):
    """Pure policy: an arbitrary tag, stale base or partial proof never wins."""
    reason = source_rejection(facts, now=now)
    if reason:
        return full_build(reason)
    if not isinstance(manifest, dict) or not _DIGEST.fullmatch(str(manifest.get('digest') or '')):
        return full_build('runtime_digest_unavailable')
    if manifest.get('mediaType') not in {
            'application/vnd.oci.image.manifest.v1+json', 'application/vnd.oci.image.index.v1+json',
            'application/vnd.docker.distribution.manifest.v2+json',
            'application/vnd.docker.distribution.manifest.list.v2+json'}:
        return full_build('runtime_manifest_unsupported')
    if 'manifests' in manifest and not any(
            isinstance(item, dict) and item.get('platform') == {'os': 'linux', 'architecture': 'amd64'}
            for item in manifest.get('manifests') or []):
        return full_build('runtime_amd64_manifest_unavailable')
    image = 'ghcr.io/' + APPROVED_REPOSITORY + '@' + manifest['digest']
    return {'mode': 'app-layer', 'reason': 'approved_same_day_runtime',
            'dockerfile': './Dockerfile.app-layer', 'verified_runtime_image': image,
            'build_args': 'VERIFIED_RUNTIME_IMAGE=' + image,
            'base_commit': APPROVED_BASE_COMMIT, 'base_timestamp': facts['base_timestamp']}


def collect_source_facts(directory, repository, head_commit, *, base_commit=APPROVED_BASE_COMMIT):
    facts = {'repository': repository, 'head_commit': head_commit, 'base_commit': base_commit,
             'base_timestamp': None, 'ancestor': False, 'changed_inputs': None}
    if repository != APPROVED_REPOSITORY or not _SHA.fullmatch(head_commit) or base_commit != APPROVED_BASE_COMMIT:
        return facts

    def git(*args, check=True):
        return subprocess.run(['git', *args], cwd=directory, capture_output=True, text=True,
                              timeout=45, check=check)
    try:
        if git('rev-parse', 'HEAD').stdout.strip() != head_commit:
            return facts
        # checkout defaults to depth one. Fetch the precise HEAD and approved
        # base with a bounded history; insufficient ancestry safely uses full.
        git('fetch', '--quiet', '--no-tags', '--depth=' + str(FETCH_DEPTH), 'origin', head_commit, base_commit)
        facts['base_timestamp'] = int(git('show', '-s', '--format=%ct', base_commit).stdout.strip())
        facts['ancestor'] = git('merge-base', '--is-ancestor', base_commit, head_commit, check=False).returncode == 0
        facts['changed_inputs'] = git('diff', '--name-only', base_commit, head_commit, '--', *RUNTIME_INPUTS).stdout.splitlines()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return facts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default=os.getenv('GITHUB_REPOSITORY', ''))
    parser.add_argument('--head-commit', default=os.getenv('GITHUB_SHA', ''))
    args = parser.parse_args()
    facts = collect_source_facts(Path.cwd(), args.repository, args.head_commit)
    manifest = None
    if source_rejection(facts) is None:
        try:
            image = 'ghcr.io/' + APPROVED_REPOSITORY + ':' + APPROVED_BASE_COMMIT
            inspected = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', image,
                '--format', '{{json .Manifest}}'], capture_output=True, text=True, timeout=45, check=True)
            manifest = json.loads(inspected.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    choice = select_runtime(facts, manifest)
    output = os.getenv('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as stream:
            for key in ('dockerfile', 'verified_runtime_image', 'build_args'):
                stream.write(key + '=' + choice[key] + '\n')
    print(json.dumps(choice))


if __name__ == '__main__':
    main()
