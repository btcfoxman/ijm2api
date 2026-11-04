"""Runtime reuse is narrowly bound; missing proofs preserve the full build."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'select_verified_runtime.py'
SPEC = importlib.util.spec_from_file_location('select_verified_runtime', SCRIPT)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
NOW = 1793799000


def facts():
    return {'repository': policy.APPROVED_REPOSITORY, 'base_commit': policy.APPROVED_BASE_COMMIT,
            'head_commit': 'a' * 40, 'base_timestamp': NOW - 300, 'ancestor': True, 'changed_inputs': []}


def manifest():
    return {'digest': 'sha256:' + 'b' * 64, 'mediaType': 'application/vnd.oci.image.manifest.v1+json'}


def test_fresh_exact_approved_runtime_is_digest_pinned():
    selected = policy.select_runtime(facts(), manifest(), now=NOW)
    assert selected['mode'] == 'app-layer'
    assert selected['verified_runtime_image'] == 'ghcr.io/btcfoxman/ijm2api@sha256:' + 'b' * 64
    assert policy.APPROVED_BASE_COMMIT not in selected['verified_runtime_image']


@pytest.mark.parametrize('change', [
    {'repository': 'foreign/ijm2api'}, {'base_commit': 'c' * 40}, {'head_commit': 'pre'},
    {'base_timestamp': NOW - 86401}, {'base_timestamp': NOW + 1}, {'base_timestamp': None},
    {'base_timestamp': True}, {'ancestor': False}, {'ancestor': None}, {'changed_inputs': None},
])
def test_incomplete_or_out_of_scope_source_proofs_fall_back(change):
    selected = policy.select_runtime({**facts(), **change}, manifest(), now=NOW)
    assert selected['mode'] == 'full' and selected['dockerfile'] == './Dockerfile'
    assert selected['verified_runtime_image'] == selected['build_args'] == ''


@pytest.mark.parametrize('name', policy.RUNTIME_INPUTS)
def test_each_runtime_input_change_requires_a_full_build(name):
    assert policy.select_runtime({**facts(), 'changed_inputs': [name]}, manifest(), now=NOW)['mode'] == 'full'


@pytest.mark.parametrize('value', [None, {}, {'digest': 'pre'}, {'digest': 'sha256:' + 'f' * 63},
    {'digest': 'sha256:' + 'f' * 64, 'mediaType': 'unrecognized'},
    {**manifest(), 'manifests': [{'platform': {'os': 'linux', 'architecture': 'arm64'}}]}])
def test_failed_registry_resolution_or_incompatible_manifest_falls_back(value):
    assert policy.select_runtime(facts(), value, now=NOW)['mode'] == 'full'


def test_manifest_index_keeps_its_immutable_digest_and_requires_amd64():
    value = {**manifest(), 'mediaType': 'application/vnd.oci.image.index.v1+json',
             'manifests': [{'platform': {'os': 'linux', 'architecture': 'amd64'}}]}
    assert policy.select_runtime(facts(), value, now=NOW)['verified_runtime_image'].endswith('@' + value['digest'])


def test_choice_does_not_mutate_its_source_or_registry_evidence():
    source, image = facts(), manifest()
    before = deepcopy((source, image))
    policy.select_runtime(source, image, now=NOW)
    assert (source, image) == before


def git(directory, *args):
    return subprocess.run(['git', *args], cwd=directory, capture_output=True, text=True, check=True).stdout.strip()


def test_depth_one_checkout_fetches_exact_head_and_proves_real_ancestor(tmp_path, monkeypatch):
    origin, checkout = tmp_path / 'origin', tmp_path / 'checkout'
    origin.mkdir()
    git(origin, 'init', '--initial-branch=pre')
    git(origin, 'config', 'user.name', 'Local fixture')
    git(origin, 'config', 'user.email', 'fixture@example.invalid')
    for name in policy.RUNTIME_INPUTS:
        path = origin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture runtime input\n')
    app = origin / 'app' / 'main.py'
    app.parent.mkdir()
    app.write_text('original = True\n')
    git(origin, 'add', '.')
    git(origin, 'commit', '-m', 'approved runtime fixture')
    base = git(origin, 'rev-parse', 'HEAD')
    for index in range(3):
        app.write_text('version = ' + str(index) + '\n')
        git(origin, 'add', '.')
        git(origin, 'commit', '-m', 'app change ' + str(index))
    head = git(origin, 'rev-parse', 'HEAD')
    git(tmp_path, 'clone', '--quiet', '--depth=1', origin.as_uri(), str(checkout))
    assert git(checkout, 'rev-parse', '--is-shallow-repository') == 'true'
    monkeypatch.setattr(policy, 'APPROVED_BASE_COMMIT', base)
    monkeypatch.setattr(policy, 'FETCH_DEPTH', 2)
    unproven = policy.collect_source_facts(checkout, policy.APPROVED_REPOSITORY, head, base_commit=base)
    assert unproven['ancestor'] is False
    assert policy.select_runtime(unproven, manifest(), now=unproven['base_timestamp'] + 60)['mode'] == 'full'
    monkeypatch.setattr(policy, 'FETCH_DEPTH', 64)
    source = policy.collect_source_facts(checkout, policy.APPROVED_REPOSITORY, head, base_commit=base)
    assert source['ancestor'] is True and source['changed_inputs'] == []
    assert policy.select_runtime(source, manifest(), now=source['base_timestamp'] + 60)['mode'] == 'app-layer'
    # A runtime dependency change must still be detected after shallow recovery.
    (origin / 'requirements.txt').write_text('changed dependency\n')
    git(origin, 'add', '.')
    git(origin, 'commit', '-m', 'runtime dependency changed')
    changed_head = git(origin, 'rev-parse', 'HEAD')
    git(checkout, 'fetch', '--quiet', 'origin', changed_head)
    git(checkout, 'checkout', '--quiet', '--detach', changed_head)
    changed = policy.collect_source_facts(checkout, policy.APPROVED_REPOSITORY, changed_head, base_commit=base)
    assert changed['ancestor'] is True and changed['changed_inputs'] == ['requirements.txt']
    assert policy.select_runtime(changed, manifest(), now=changed['base_timestamp'] + 60)['mode'] == 'full'
