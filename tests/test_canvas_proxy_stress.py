"""Stress probes must stay local and clean up only their own process groups."""
import subprocess
from unittest.mock import Mock

import pytest

from tools import stress_canvas_proxy as stress


def process(monkeypatch, output=b'', code=0):
    child = Mock(pid=918273, returncode=code)
    child.communicate.return_value = (output, b'')
    popen = Mock(return_value=child)
    monkeypatch.setattr(stress.subprocess, 'Popen', popen)
    return child, popen


def test_true_probe_uses_own_session_and_dead_proxy_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-not-forwarded')
    monkeypatch.setenv('EXAMPLE_PASSWORD', 'synthetic-not-forwarded')
    child, popen = process(monkeypatch)
    result = stress.one('/candidate/graftcp', ['/bin/true'], 1, str(tmp_path), 8, 5)
    assert result['returncode'] == 0 and result['version_ok'] is True
    command = popen.call_args.args[0]
    assert command == ['/candidate/graftcp', '--config', '/dev/null',
        '--select_proxy_mode', 'only_http_proxy', '--http_proxy', '127.0.0.1:9', '--', '/bin/true']
    options = popen.call_args.kwargs
    assert options['start_new_session'] is True
    assert options['stdin'] == subprocess.DEVNULL
    assert options['env']['HOME'] == options['cwd'] == str(tmp_path)
    assert options['env']['GOMAXPROCS'] == '8'
    assert 'GITHUB_TOKEN' not in options['env'] and 'EXAMPLE_PASSWORD' not in options['env']
    assert '127.0.0.1:9' in options['env']['HTTPS_PROXY']


@pytest.mark.parametrize('output,valid', [(b'{"ok":true,"data":{"version":"1.2.1"}}', True),
    (b'', False), (b'not-json', False), (b'{"ok":false}', False)])
def test_native_metadata_needs_a_valid_json_success(tmp_path, monkeypatch, output, valid):
    process(monkeypatch, output)
    result = stress.one('/candidate/graftcp', ['/candidate/dreamina-canvas', '--format', 'json', 'version'],
        2, str(tmp_path), 2, 5)
    assert result['version_ok'] is valid


def test_timeout_kills_just_its_owned_group_and_retains_original_exit(tmp_path, monkeypatch):
    child, _ = process(monkeypatch, code=22)
    child.communicate.side_effect = [subprocess.TimeoutExpired('owned-test', 5), (b'', b'ptrace: No such process\n')]
    child.poll.return_value = 22
    killpg = Mock(); monkeypatch.setattr(stress.os, 'killpg', killpg, raising=False)
    monkeypatch.setattr(stress.signal, 'SIGKILL', 9, raising=False)
    result = stress.one('/candidate/graftcp', ['/bin/true'], 1, str(tmp_path), 8, 5)
    killpg.assert_called_once_with(child.pid, 9)
    assert result['timeout'] is True
    assert result['returncode_before_timeout_cleanup'] == 22
    assert result['ptrace_diagnostic'] is True
    assert child.communicate.call_count == 2


def test_group_disappearing_during_timeout_cleanup_is_safe(tmp_path, monkeypatch):
    child, _ = process(monkeypatch)
    child.communicate.side_effect = [subprocess.TimeoutExpired('owned-test', 5), (b'', b'')]
    killpg = Mock(side_effect=ProcessLookupError())
    monkeypatch.setattr(stress.os, 'killpg', killpg, raising=False)
    monkeypatch.setattr(stress.signal, 'SIGKILL', 9, raising=False)
    assert stress.one('/candidate/graftcp', ['/bin/true'], 1, str(tmp_path), 8, 5)['timeout'] is True
