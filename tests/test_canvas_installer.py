"""A build may use only the pinned bytes from verified official sources."""
import hashlib
import importlib.util
import io
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from app import deploy_runtime_guard
from app.canvas_cli import PINNED_GLOBAL_LINUX_SHA256, PINNED_VERSION

SPEC = importlib.util.spec_from_file_location('canvas_installer', Path(__file__).parents[1] / 'tools/install_canvas_cli.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_installer_runtime_and_standalone_deploy_guard_share_exact_release():
    assert installer.VERSION == PINNED_VERSION == '1.2.1'
    assert installer.ARTIFACTS['linux-amd64'][1] == PINNED_GLOBAL_LINUX_SHA256
    assert installer.BASE.endswith('/dreamina_canvas_cli_oversea/1_0_0_29/')
    assert installer.FALLBACK_BASE.endswith('/1_0_0_29/')
    runtime = {'available': True, 'version': PINNED_VERSION, 'distribution': 'global',
               'schema_version': '1', 'proxy_transport': 'graftcp', 'proxy_version': '0.8.3'}
    assert deploy_runtime_guard.runtime_ready({'canvas_cli_runtime': runtime})
    for version, distribution in [('1.2.0', 'global'), (PINNED_VERSION, 'cn')]:
        assert not deploy_runtime_guard.runtime_ready({'canvas_cli_runtime': {
            **runtime, 'version': version, 'distribution': distribution}})


def test_cdn_download_is_checked_before_installing(tmp_path):
    payload = b'synthetic-official-elf'
    artifact = ('dreamina-canvas_linux_amd64', hashlib.sha256(payload).hexdigest())
    destination = tmp_path / 'bin/dreamina-canvas'
    with patch.dict(installer.ARTIFACTS, {'linux-amd64': artifact}), \
         patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as download:
        installer.install(destination)
    assert destination.read_bytes() == payload
    assert download.call_args.args[0] == installer.BASE + artifact[0]
    assert 'capcutstatic.com/' in download.call_args.args[0]


def test_front_door_fallback_keeps_same_hash(tmp_path):
    payload = b'synthetic-official-elf'
    artifact = ('dreamina-canvas_linux_amd64', hashlib.sha256(payload).hexdigest())
    with patch.dict(installer.ARTIFACTS, {'linux-amd64': artifact}), \
         patch.object(installer.time, 'sleep'), \
         patch.object(installer.urllib.request, 'urlopen', side_effect=[HTTPError(installer.BASE, 404, 'Not Found', {}, None), io.BytesIO(payload)]) as download:
        installer.install(tmp_path / 'dreamina-canvas')
    assert [call.args[0] for call in download.call_args_list] == [installer.BASE + artifact[0], installer.FALLBACK_BASE + artifact[0]]


def test_hash_mismatch_preserves_existing_binary_and_never_falls_back(tmp_path):
    destination = tmp_path / 'dreamina-canvas'
    destination.write_bytes(b'previous-verified-binary')
    with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong-download')) as download, \
         pytest.raises(ValueError, match='SHA256 mismatch'):
        installer.install(destination)
    assert destination.read_bytes() == b'previous-verified-binary'
    assert download.call_count == 1
    assert list(tmp_path.iterdir()) == [destination]
