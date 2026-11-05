"""Final-image tracing is exercised with default isolation and no account data."""
import ast
import json
import subprocess
from unittest.mock import Mock

import pytest

from tools import test_canvas_proxy_image as gate


IMAGE = 'ghcr.io/btcfoxman/ijm2api:' + 'a' * 40


def test_smoke_program_is_valid_and_contains_only_local_metadata_commands():
    ast.parse(gate.CODE)
    assert "commands=[['/bin/true'],['/usr/local/bin/dreamina-canvas','--format','json','version']]" in gate.CODE
    assert "--http_proxy','127.0.0.1:9'" in gate.CODE
    assert "'node','run'" not in gate.CODE and "'auth'" not in gate.CODE


def test_non_hosted_environment_stops_before_any_docker_command(monkeypatch):
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'self-hosted')
    run = Mock(); monkeypatch.setattr(gate.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='hosted Linux'):
        gate.verify_image(IMAGE)
    run.assert_not_called()


def test_only_immutable_ijm_tag_allowed(monkeypatch):
    monkeypatch.setattr(gate, 'require_hosted', lambda: None)
    run = Mock(); monkeypatch.setattr(gate.subprocess, 'run', run)
    with pytest.raises(ValueError, match='immutable'):
        gate.verify_image('ghcr.io/btcfoxman/ijm2api:pre')
    run.assert_not_called()


def test_container_is_local_no_network_no_added_permissions_and_cleans_own_uuid(monkeypatch):
    monkeypatch.setattr(gate, 'require_hosted', lambda: None)
    proof = {'passed': 64, 'generation_requests': 0}
    run = Mock(return_value=Mock(returncode=0, stdout=json.dumps(proof), stderr=''))
    monkeypatch.setattr(gate.subprocess, 'run', run)
    assert gate.verify_image(IMAGE) == proof
    command = run.call_args_list[0].args[0]
    assert command[:3] == ['docker', '--host', 'unix:///var/run/docker.sock']
    assert command[command.index('--network') + 1] == 'none'
    assert command[command.index('--entrypoint') + 1] == 'python'
    assert not {'--privileged', '--cap-add', '--security-opt', '--env-file', '-v', '--volume'}.intersection(command)
    name = command[command.index('--name') + 1]
    assert name.startswith('ijm-proxy-offline-')
    assert run.call_args_list[1].args[0] == ['docker', '--host', 'unix:///var/run/docker.sock', 'rm', '-f', name]
    assert run.call_args_list[0].kwargs['input'] == gate.CODE


def test_timeout_still_removes_only_its_synthetic_container(monkeypatch):
    monkeypatch.setattr(gate, 'require_hosted', lambda: None)
    run = Mock(side_effect=[subprocess.TimeoutExpired('synthetic Docker', 120), Mock(returncode=0)])
    monkeypatch.setattr(gate.subprocess, 'run', run)
    with pytest.raises(subprocess.TimeoutExpired):
        gate.verify_image(IMAGE)
    start, cleanup = [call.args[0] for call in run.call_args_list]
    assert cleanup[-3:] == ['rm', '-f', start[start.index('--name') + 1]]
