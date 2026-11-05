"""The runtime proxy must be built from exact reviewed source, never on pre."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
from unittest.mock import Mock

import pytest

from tools import build_canvas_proxy as builder
from tools import select_verified_runtime as runtime_policy


def source_archive(entries):
    contents = io.BytesIO()
    with tarfile.open(fileobj=contents, mode='w:gz') as archive:
        for name, data, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            if kind == tarfile.REGTYPE:
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
            else:
                item.linkname = '../../outside'
                archive.addfile(item)
    return contents.getvalue()


def exact_hash(monkeypatch, field, data):
    monkeypatch.setattr(builder, field, hashlib.sha256(data).hexdigest())


def test_source_extraction_keeps_exact_files_and_modes(tmp_path, monkeypatch):
    name = 'graftcp-' + builder.SOURCE_COMMIT + '/local/main.go'
    data = source_archive([(name, b'package main\n', tarfile.REGTYPE)])
    exact_hash(monkeypatch, 'SOURCE_SHA256', data)
    builder.extract_source(data, tmp_path)
    assert (tmp_path / 'local/main.go').read_bytes() == b'package main\n'


@pytest.mark.parametrize('suffix,kind', [('../outside', tarfile.REGTYPE),
    ('/absolute', tarfile.REGTYPE), ('file', tarfile.SYMTYPE), ('file', tarfile.LNKTYPE),
    ('file', tarfile.FIFOTYPE), ('local\\outside', tarfile.REGTYPE)])
def test_unsafe_source_paths_and_non_files_never_extract(tmp_path, monkeypatch, suffix, kind):
    name = 'graftcp-' + builder.SOURCE_COMMIT + '/' + suffix
    data = source_archive([(name, b'not safe', kind)])
    exact_hash(monkeypatch, 'SOURCE_SHA256', data)
    with pytest.raises(ValueError, match='unsafe'):
        builder.extract_source(data, tmp_path)
    assert not list(tmp_path.rglob('*'))


def test_archive_digest_checked_before_any_writes(tmp_path):
    with pytest.raises(ValueError, match='archive SHA256'):
        builder.extract_source(b'untrusted archive', tmp_path)
    assert not list(tmp_path.iterdir())


def test_duplicate_source_member_is_rejected(tmp_path, monkeypatch):
    name = 'graftcp-' + builder.SOURCE_COMMIT + '/file'
    data = source_archive([(name, b'one', tarfile.REGTYPE), (name, b'two', tarfile.REGTYPE)])
    exact_hash(monkeypatch, 'SOURCE_SHA256', data)
    with pytest.raises(ValueError, match='unsafe member'):
        builder.extract_source(data, tmp_path)


def test_expansion_limit_rejects_oversized_member(tmp_path, monkeypatch):
    name = 'graftcp-' + builder.SOURCE_COMMIT + '/file'
    data = source_archive([(name, b'too large', tarfile.REGTYPE)])
    exact_hash(monkeypatch, 'SOURCE_SHA256', data)
    monkeypatch.setattr(builder, 'MAX_EXPANDED_BYTES', 2)
    with pytest.raises(ValueError, match='expanded size'):
        builder.extract_source(data, tmp_path)
    assert not list(tmp_path.rglob('*'))


def test_digest_mismatch_download_is_not_retried(monkeypatch):
    request = Mock(return_value=io.BytesIO(b'untrusted source'))
    monkeypatch.setattr(builder.urllib.request, 'urlopen', request)
    with pytest.raises(ValueError, match='archive SHA256'):
        builder.read_source_archive()
    request.assert_called_once()


def test_network_download_retry_still_checks_exact_archive(monkeypatch):
    payload = b'synthetic exact archive'
    exact_hash(monkeypatch, 'SOURCE_SHA256', payload)
    request = Mock(side_effect=[OSError('temporary'), io.BytesIO(payload)])
    monkeypatch.setattr(builder.urllib.request, 'urlopen', request)
    sleep = Mock(); monkeypatch.setattr(builder.time, 'sleep', sleep)
    assert builder.read_source_archive() == payload
    assert request.call_count == 2
    sleep.assert_called_once_with(1)


def test_patch_source_is_pinned_and_lock_covers_both_cgo_calls():
    patch = builder.PATCH_PATH.read_bytes()
    assert hashlib.sha256(patch).hexdigest() == builder.PATCH_SHA256
    text = patch.decode()
    assert '+\t"runtime"' in text
    assert text.index('+\truntime.LockOSThread()') < text.index('retCode = clientPrepare(')
    assert '+\tdefer runtime.UnlockOSThread()' in text
    assert 'clientTrace' not in '\n'.join(line for line in text.splitlines() if line.startswith('-'))
    assert builder.SOURCE_COMMIT == '825cf6d3b9ec043defe8e598eb218eccc1a3eaf3'


def test_altered_patch_or_source_is_rejected_without_mutating_main(tmp_path):
    main = tmp_path / builder.MAIN_PATH
    main.parent.mkdir(parents=True); main.write_bytes(b'untrusted main')
    with pytest.raises(ValueError, match='upstream main SHA256'):
        builder.thread_pin_source(tmp_path)
    patch = tmp_path / 'patch'; patch.write_bytes(b'changed patch')
    with pytest.raises(ValueError, match='patch SHA256'):
        builder.thread_pin_source(tmp_path, patch)
    assert main.read_bytes() == b'untrusted main'


def test_build_refuses_non_hosted_environment_before_download_or_compilation(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'self-hosted')
    download = Mock(); process = Mock()
    monkeypatch.setattr(builder, 'read_source_archive', download)
    monkeypatch.setattr(builder.subprocess, 'run', process)
    with pytest.raises(RuntimeError, match='GitHub hosted runner'):
        builder.build(tmp_path / 'runtime', tmp_path / 'comparison')
    download.assert_not_called(); process.assert_not_called()
    assert not list(tmp_path.iterdir())


def receipt_fixture(tmp_path, monkeypatch):
    binary = bytearray(64); binary[:6] = b'\x7fELF\x02\x01'; binary[18:20] = (62).to_bytes(2, 'little')
    docs = tmp_path / 'docs'; docs.mkdir()
    source, patch = b'original exact source', b'original exact patch'
    exact_hash(monkeypatch, 'SOURCE_SHA256', source); exact_hash(monkeypatch, 'PATCH_SHA256', patch)
    (tmp_path / 'graftcp').write_bytes(binary)
    (docs / 'source.tar.gz').write_bytes(source); (docs / 'thread-pinning.patch').write_bytes(patch)
    (docs / 'COPYING').write_text('GPL-3.0')
    receipt = {'upstream_version': builder.VERSION, 'source_commit': builder.SOURCE_COMMIT,
        'source_archive_sha256': builder.SOURCE_SHA256, 'patch_id': builder.PATCH_ID,
        'patch_sha256': builder.PATCH_SHA256, 'main_before_sha256': builder.MAIN_BEFORE_SHA256,
        'main_after_sha256': builder.MAIN_AFTER_SHA256, 'executable_sha256': builder.digest(binary)}
    (docs / 'build-receipt.json').write_text(json.dumps(receipt))
    return receipt


@pytest.mark.parametrize('file', ['graftcp', 'docs/source.tar.gz', 'docs/thread-pinning.patch'])
def test_runtime_proof_detects_binary_or_provenance_tampering(tmp_path, monkeypatch, file):
    receipt = receipt_fixture(tmp_path, monkeypatch)
    assert builder.verify_runtime(tmp_path) == receipt
    path = tmp_path / file
    path.write_bytes(path.read_bytes() + b'modified')
    with pytest.raises(ValueError, match='SHA256'):
        builder.verify_runtime(tmp_path)


def test_runtime_proof_rejects_wrong_patch_identity(tmp_path, monkeypatch):
    receipt = receipt_fixture(tmp_path, monkeypatch)
    receipt['patch_id'] = 'unreviewed-patch'
    (tmp_path / 'docs/build-receipt.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='provenance'):
        builder.verify_runtime(tmp_path)


def test_wrapper_inputs_invalidate_runtime_reuse_and_production_never_compiles():
    root = Path(__file__).parents[1]
    workflow = (root / '.github/workflows/deploy-test.yml').read_text(encoding='utf-8')
    build_job, deploy_job = workflow.split('\n  deploy:', 1)
    assert 'runs-on: ubuntu-latest' in build_job
    assert 'go-version: \'1.25.x\'' in build_job
    for name in ('tools/build_canvas_proxy.py', 'tools/stress_canvas_proxy.py',
                 'tools/test_canvas_proxy_image.py',
                 'tools/patches/graftcp-0.8.3-thread-pinning.patch'):
        assert name in runtime_policy.RUNTIME_INPUTS
        assert name in next(line for line in workflow.splitlines() if 'git diff --quiet' in line)
    assert 'tools/build_canvas_proxy.py' not in deploy_job
    assert 'tools/stress_canvas_proxy.py' not in deploy_job
    dockerfile = (root / 'Dockerfile').read_text(encoding='utf-8')
    assert 'COPY .canvas-proxy-build /tmp/canvas-proxy-runtime' in dockerfile
    assert '--verify-only /tmp/canvas-proxy-runtime' in dockerfile
    assert 'python /tmp/install_canvas_proxy.py' not in dockerfile
    assert 'make ' not in dockerfile and 'go build' not in dockerfile
