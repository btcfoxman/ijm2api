"""Build the pinned thread-affine account proxy on a GitHub hosted runner only.

The original upstream executable remains a diagnostic baseline. Runtime images
receive an already built executable plus exact source, patch, license and receipt.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request

VERSION = '0.8.3'
SOURCE_COMMIT = '825cf6d3b9ec043defe8e598eb218eccc1a3eaf3'
SOURCE_URL = f'https://codeload.github.com/hmgle/graftcp/tar.gz/{SOURCE_COMMIT}'
SOURCE_SHA256 = '06d664a7b6b4d5da8da70dabdea9ae1bb4ca5739ab7f03a2a37a71f020f02645'
PATCH_ID = 'ijm-ptrace-thread-pinning-v1'
PATCH_SHA256 = '10a2927fae80bed6aa3a8f3788339dc6ea8eb91426ea7625d90d10efcb52ee52'
MAIN_PATH = 'local/cmd/mgraftcp/main.go'
MAIN_BEFORE_SHA256 = '8ea51130bd78a50d765a0f01967fc26c90b4ae1b686e5f347d06c20caa1c56a9'
MAIN_AFTER_SHA256 = 'ac999c50617abaa2408574adefefeca18fb34d57242293cf2852ee0c145a3112'
PATCH_PATH = Path(__file__).parent / 'patches/graftcp-0.8.3-thread-pinning.patch'
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_source_archive() -> bytes:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(SOURCE_URL, timeout=45) as response:
                data = response.read(MAX_ARCHIVE_BYTES + 1)
            if len(data) > MAX_ARCHIVE_BYTES:
                raise ValueError('graftcp source archive exceeds size limit')
            if digest(data) != SOURCE_SHA256:
                raise ValueError('graftcp source archive SHA256 mismatch')
            return data
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError('unreachable')


def extract_source(data: bytes, directory: Path) -> None:
    """Reject aliases, links and traversal; never use unrestricted extractall."""
    if digest(data) != SOURCE_SHA256:
        raise ValueError('graftcp source archive SHA256 mismatch')
    seen, total = set(), 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for item in archive.getmembers():
            path = PurePosixPath(item.name)
            if (not path.parts or path.parts[0] != 'graftcp-' + SOURCE_COMMIT
                    or path.is_absolute() or '..' in path.parts or '\\' in item.name
                    or path.as_posix() != item.name.rstrip('/')):
                raise ValueError('graftcp source archive contains an unsafe path')
            if item.isdir():
                continue
            if not item.isfile() or len(path.parts) < 2 or item.name in seen:
                raise ValueError('graftcp source archive contains an unsafe member')
            seen.add(item.name)
            total += item.size
            if item.size < 0 or total > MAX_EXPANDED_BYTES:
                raise ValueError('graftcp source archive exceeds expanded size limit')
            target = directory.joinpath(*path.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(item) as stream:
                target.write_bytes(stream.read())
            target.chmod(0o755 if item.mode & 0o111 else 0o644)


def thread_pin_source(directory: Path, patch_path: Path = PATCH_PATH) -> bytes:
    patch = patch_path.read_bytes()
    if digest(patch) != PATCH_SHA256:
        raise ValueError('graftcp patch SHA256 mismatch')
    main = directory / MAIN_PATH
    before = main.read_bytes()
    if digest(before) != MAIN_BEFORE_SHA256:
        raise ValueError('graftcp upstream main SHA256 mismatch')
    original = before.decode('utf-8')
    updated = original.replace('"path/filepath"', '"path/filepath"\n\t"runtime"')
    marker = '\tretCode = clientPrepare(cfg.clientArgs(faddr.Port, dnsPort, udpPort, args))'
    updated = updated.replace(marker,
        '\t// PTRACE_TRACEME binds the child to the thread performing the fork.\n'
        '\t// Keep preparation and the entire trace loop on that same OS thread.\n'
        '\truntime.LockOSThread()\n\tdefer runtime.UnlockOSThread()\n\n' + marker)
    expected_patch = ''.join(difflib.unified_diff(original.splitlines(keepends=True),
        updated.splitlines(keepends=True), fromfile='a/' + MAIN_PATH,
        tofile='b/' + MAIN_PATH)).encode()
    after = updated.encode()
    if patch != expected_patch or digest(after) != MAIN_AFTER_SHA256:
        raise ValueError('graftcp exact patch application mismatch')
    main.write_bytes(after)
    return patch


def verify_elf(binary: bytes) -> None:
    if (len(binary) < 20 or binary[:6] != b'\x7fELF\x02\x01'
            or int.from_bytes(binary[18:20], 'little') != 62):
        raise ValueError('graftcp build is not Linux amd64 ELF')


def verify_runtime(directory: Path) -> dict:
    receipt = json.loads((directory / 'docs/build-receipt.json').read_text(encoding='utf-8'))
    expected = {'upstream_version': VERSION, 'source_commit': SOURCE_COMMIT,
        'source_archive_sha256': SOURCE_SHA256, 'patch_id': PATCH_ID,
        'patch_sha256': PATCH_SHA256, 'main_before_sha256': MAIN_BEFORE_SHA256,
        'main_after_sha256': MAIN_AFTER_SHA256}
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError('graftcp provenance mismatch')
    binary = (directory / 'graftcp').read_bytes()
    verify_elf(binary)
    if receipt.get('executable_sha256') != digest(binary):
        raise ValueError('graftcp executable SHA256 mismatch')
    if digest((directory / 'docs/source.tar.gz').read_bytes()) != SOURCE_SHA256:
        raise ValueError('graftcp archived source SHA256 mismatch')
    if digest((directory / 'docs/thread-pinning.patch').read_bytes()) != PATCH_SHA256:
        raise ValueError('graftcp archived patch SHA256 mismatch')
    if not (directory / 'docs/COPYING').is_file():
        raise ValueError('graftcp license missing')
    return receipt


def build(destination: Path, comparison_directory: Path) -> dict:
    if (os.getenv('GITHUB_ACTIONS') != 'true' or os.getenv('RUNNER_ENVIRONMENT') != 'github-hosted'
            or platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'amd64'}):
        raise RuntimeError('Canvas proxy compilation requires a Linux amd64 GitHub hosted runner')
    destination, comparison_directory = destination.resolve(), comparison_directory.resolve()
    if destination.exists() or comparison_directory.exists():
        raise ValueError('Canvas proxy build destinations must be new')
    archive = read_source_archive()
    with tempfile.TemporaryDirectory(prefix='canvas-proxy-source-') as temporary:
        upstream, patched = Path(temporary) / 'upstream', Path(temporary) / 'thread-pinned'
        extract_source(archive, upstream)
        shutil.copytree(upstream, patched)
        patch = thread_pin_source(patched)
        go = subprocess.check_output(['go', 'version'], text=True, timeout=10).strip()
        gcc = subprocess.check_output(['gcc', '--version'], text=True, timeout=10).splitlines()[0]
        env = dict(os.environ, GOTOOLCHAIN='local', CGO_ENABLED='1')
        for source in (upstream, patched):
            subprocess.run(['make', '-j2', 'local/graftcp', 'VERSION=v' + VERSION],
                cwd=source, env=env, check=True, timeout=600)
        subprocess.run(['make', 'test'], cwd=patched, env=env, check=True, timeout=600)
        binary, original = (patched / 'local/graftcp').read_bytes(), (upstream / 'local/graftcp').read_bytes()
        verify_elf(binary); verify_elf(original)
        destination.mkdir(parents=True)
        comparison_directory.mkdir(parents=True)
        (destination / 'graftcp').write_bytes(binary)
        (destination / 'graftcp').chmod(0o755)
        (comparison_directory / 'graftcp-unpatched').write_bytes(original)
        (comparison_directory / 'graftcp-unpatched').chmod(0o755)
        docs = destination / 'docs'; docs.mkdir()
        (docs / 'COPYING').write_bytes((patched / 'COPYING').read_bytes())
        (docs / 'source.tar.gz').write_bytes(archive)
        (docs / 'thread-pinning.patch').write_bytes(patch)
        receipt = {'upstream_version': VERSION, 'source_commit': SOURCE_COMMIT,
            'source_archive_sha256': SOURCE_SHA256, 'patch_id': PATCH_ID,
            'patch_sha256': PATCH_SHA256, 'main_before_sha256': MAIN_BEFORE_SHA256,
            'main_after_sha256': MAIN_AFTER_SHA256, 'executable_sha256': digest(binary),
            'unpatched_executable_sha256': digest(original), 'go_version': go,
            'c_compiler': gcc, 'build_environment': 'github-hosted-linux-amd64',
            'source_url': SOURCE_URL, 'account_proxy_bypass': False,
            'paid_retry_logic_changed': False}
        (docs / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        (docs / 'SOURCE').write_text('graftcp v' + VERSION + '\n' + SOURCE_URL + '\n'
            'Exact source archive and local patch accompany this GPL-3.0 executable.\n'
            'Patch: ' + PATCH_ID + '\n', encoding='utf-8')
    return verify_runtime(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=Path('.canvas-proxy-build'))
    parser.add_argument('--comparison-directory', type=Path, default=Path('.canvas-proxy-comparison'))
    parser.add_argument('--verify-only', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_runtime(args.verify_only) if args.verify_only else
        build(args.destination, args.comparison_directory)))
