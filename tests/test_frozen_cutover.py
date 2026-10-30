"""Hermetic deployment-controller tests; no Docker, application or CLI starts."""
from __future__ import annotations

import hashlib
import ast
from copy import deepcopy
import importlib.util
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts/frozen_cutover.py"
SPEC = importlib.util.spec_from_file_location("isolated_frozen_cutover", MODULE)
cutover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutover)

QUERY_IDS = "ijm_c1b4967e7b674cd2,ijm_4705618586294e2e,ijm_5f4677122fea4a08,ijm_5a0b6e0a43904cc8"
BINDING = "a" * 64


class FakeCgroup:
    """No /proc or cgroup access; model only the controller's trusted seam."""
    def __init__(self, docker):
        self.docker = docker
        self.closed = False
        self.armed = False
        self.capture_failure = False
        self.validation_failure = False
        self.death_proof = 'held_events_populated_zero'
        self.namespace_attested = False
        self.identity = {'pid': 4321, 'startticks': 123456, 'container_id': docker.old_id,
            'pid_namespaces': {'original': {'device': 4, 'inode': 11}, 'controller': {'device': 4, 'inode': 22}},
            'cgroup_path': '/system.slice/docker-' + docker.old_id + '.scope',
            'host_cgroup_path': '/sys/fs/cgroup/system.slice/docker-' + docker.old_id + '.scope',
            'files': {name: {'device': 1, 'inode': number} for number, name in enumerate(
                ('cgroup.kill', 'cgroup.events', 'cgroup.freeze', 'cgroup.type'), 1)}}

    def capture(self, original):
        self.docker.calls.append(['cgroup_capture'])
        if self.capture_failure:
            raise RuntimeError('SyntheticCgroupUnavailable')
        assert original['Id'] == self.identity['container_id'] and original['State']['Pid'] == self.identity['pid']
        return deepcopy(self.identity)

    def bind_original_namespace(self, witness):
        assert witness == {'pid_namespace': self.identity['pid_namespaces']['original'],
            'startticks': self.identity['startticks'], 'cgroup': ['0::' + self.identity['cgroup_path']]}
        assert not self.namespace_attested
        self.namespace_attested = True
        return {'shared_guard_witness_verified': True}

    def mount_args(self):
        assert self.namespace_attested
        args = ['--cgroupns', 'host']
        for name in ('cgroup.kill', 'cgroup.events', 'cgroup.freeze', 'cgroup.type'):
            mount = 'type=bind,src=' + self.identity['host_cgroup_path'] + '/' + name + ',dst=' + cutover.CGROUP_BIND_ROOT + '/' + name
            args += ['--mount', mount + ('' if name == 'cgroup.kill' else ',readonly')]
        for name in ('stat', 'cgroup'):
            args += ['--mount', f'type=bind,src=/proc/4321/{name},dst={cutover.PROCESS_BIND_ROOT}/{name},readonly']
        return args

    def validate_killer(self, current, access):
        assert self.namespace_attested
        self.docker.calls.append(['cgroup_validate_killer'])
        if (current['HostConfig']['PidMode'] != '' or access.get('writer_pid_namespace') != {'device': 4, 'inode': 33}):
            raise RuntimeError('KillerLocalNamespaceIdentityRejected')
        return {'container_id': current['Id'], 'pid': 7777, 'startticks': 654321,
            'pid_namespace': access['writer_pid_namespace'], 'private_sibling_verified': True, 'local_cgroup_matches_container': True}

    def validate(self, frozen=None):
        self.docker.calls.append(['cgroup_validate', frozen])
        if self.validation_failure:
            raise RuntimeError('SyntheticCgroupIdentityChanged')
        if frozen is not None:
            assert self.docker.paused is frozen
        return {'populated': 1, 'frozen': int(bool(self.docker.paused))}

    def arm(self):
        self.armed = True
        self.docker.calls.append(['cgroup_arm'])

    def wait_empty(self, timeout=20):
        self.docker.calls.append(['cgroup_wait_empty'])
        if not self.docker.old_stopped:
            raise RuntimeError('CgroupDeathNotProven')
        return {'empty_proof': self.death_proof, 'populated': 0, 'original_pid_gone': True}

    def diagnostic(self):
        return {'last_events': {'populated': 1, 'frozen': 1},
            'original_pid': {'present': True, 'state': 'Z', 'startticks': 123456,
                'expected_startticks': 123456, 'original_startticks_match': True},
            'last_watch_error': {'error_class': 'PermissionError', 'errno': 13}}

    def close(self):
        self.closed = True


class FakeDocker:
    def __init__(self):
        self.old_id = "a" * 64
        self.companion_id = "c" * 64
        self.killer_id = 'e' * 64
        self.killer_name = None
        self.killer_image = None
        self.killer_exists = False
        self.killer_mounts = []
        self.killer_start_timeout_after_effect = False
        self.foreign_killer_field = None
        self.killer_replaced_at_cleanup = False
        self.companion_name = None
        self.companion_image = None
        self.companion_exists = False
        self.start_timeout_after_effect = False
        self.start_failure_before_creation = False
        self.start_error = TimeoutError("companion startup response lost")
        self.cleanup_inspect_failure = False
        self.foreign_companion_field = None
        self.empty_start_receipt = False
        self.paused = False
        self.old_removed = False
        self.old_stopped = False
        self.kill_count = 0
        self.kill_failure_before_effect = False
        self.kill_error_before_effect = None
        self.kill_error_after_effect = None
        self.access_allowed = True
        self.restart_policy = {'Name': 'always', 'MaximumRetryCount': 0}
        self.original_restart_policy = deepcopy(self.restart_policy)
        self.restart_count = 0
        self.restart_update_timeout_after_effect = False
        self.restart_update_ignored = False
        self.restart_restore_failure = False
        self.external_policy_on_first_guard = None
        self.release = None
        self.inspect_count = 0
        self.guard_count = 0
        self.calls = []
        self.inputs = []
        self.backup = b"mock sqlite online backup bytes"
        self.first_failure = False
        self.first_binding = BINDING
        self.final_binding = BINDING
        self.first_source_manifest = {
            "ijm2api.db": {"sha256": "d" * 64, "size_bytes": 123},
            "ijm2api.db-wal": None,
        }
        self.final_source_manifest = json.loads(json.dumps(self.first_source_manifest))
        self.final_failure = False
        self.bad_backup = False
        self.changed_before_pause = False
        self.removal_timeout_after_effect = False
        self.pause_timeout_after_effect = False
        self.export_failure = False
        self.source_sizes = {'ijm2api.db': 69746688, 'ijm2api.db-wal': 0}
        self.mounts = []
        self.restart_read_mount_order_drift_once = False
        self.restart_read_mount_field_drift_once = None
        self.snapshot_capacity_failure = False
        self.diagnostic_inspect_failure = False
        self.cgroup = FakeCgroup(self)

    def export_backup(self, companion_id, destination):
        assert companion_id == self.companion_id and self.paused and self.guard_count == 1
        self.calls.append(["binary_export", companion_id, str(destination)])
        self.inputs.append(None)
        if self.export_failure:
            Path(destination).write_bytes(b"incomplete stream")
            raise RuntimeError("SyntheticStreamExportFailed")
        Path(destination).write_bytes(b"corrupted backup" if self.bad_backup else self.backup)

    def __call__(self, args, *, input=None, timeout=30):
        self.calls.append(list(args))
        self.inputs.append(input)
        if args[:2] == ["docker", "inspect"]:
            if timeout == 5 and self.diagnostic_inspect_failure:
                raise PermissionError(13, 'private diagnostic detail must not leak')
            if args[-1] in (self.companion_name, self.companion_id, self.killer_name, self.killer_id):
                killer = args[-1] in (self.killer_name, self.killer_id)
                if not (self.killer_exists if killer else self.companion_exists) or (self.cleanup_inspect_failure and not killer):
                    raise RuntimeError("CompanionInspectionUnavailable")
                value = {"Id": self.killer_id if killer else self.companion_id,
                    "Name": "/" + (self.killer_name if killer else self.companion_name),
                    "Config": {"Image": self.killer_image if killer else self.companion_image,
                        'Entrypoint': ['python'], 'Cmd': ['-I', '-S', '-B', '-c', 'import time; time.sleep(600)'] if killer else ['-B', '-c', 'import time; time.sleep(600)'],
                        "Labels": {cutover.COMPANION_OWNER_LABEL: self.old_id, cutover.COMPANION_ROLE_LABEL: 'killer' if killer else 'guard'}},
                    "State": {'Pid': 7777 if killer else 8888, 'Running': True, 'Paused': False},
                    "Mounts": deepcopy(self.killer_mounts) if killer else [],
                    "HostConfig": {"PidMode": '' if killer else "container:" + self.old_id,
                        'CgroupnsMode': 'host', 'Binds': None, 'Mounts': None,
                        "ReadonlyRootfs": True, "NetworkMode": "none", 'Privileged': False,
                        'CapDrop': ['ALL'], 'CapAdd': None if killer else ['DAC_READ_SEARCH'], 'SecurityOpt': ['no-new-privileges']}}
                value['HostConfig']['VolumesFrom'] = None if killer else [self.old_id + ':ro']
                field = self.foreign_killer_field if killer else self.foreign_companion_field
                if killer and args[-1] == self.killer_name and self.killer_replaced_at_cleanup:
                    value['Id'] = 'f' * 64
                if field == "name": value["Name"] = "/foreign-container"
                elif field == "owner": value["Config"]["Labels"][cutover.COMPANION_OWNER_LABEL] = "foreign-owner"
                elif field == "image": value["Config"]["Image"] = "foreign-image"
                elif field == "pid": value["HostConfig"]["PidMode"] = "host"
                elif field == "readonly": value["HostConfig"]["ReadonlyRootfs"] = False
                elif field == "network": value["HostConfig"]["NetworkMode"] = "host"
                elif field == "original-id": value["Id"] = self.old_id
                elif field == 'role': value['Config']['Labels'][cutover.COMPANION_ROLE_LABEL] = 'foreign-role'
                elif field == 'capadd': value['HostConfig']['CapAdd'] = ['SYS_ADMIN']
                elif field == 'mount-rw': value['Mounts'][-1]['RW'] = True
                elif field == 'cmd': value['Config']['Cmd'] = ['app.main']
                elif field == 'replacement-id': value['Id'] = 'f' * 64
                return json.dumps([value])
            self.inspect_count += 1
            value = {"Id": self.old_id, 'Name': '/ijm2api', 'Image': 'sha256:' + 'b' * 64,
                'Config': {'Image': 'original:immutable'}, 'Mounts': deepcopy(self.mounts), 'RestartCount': self.restart_count,
                'HostConfig': {'AutoRemove': False, 'RestartPolicy': deepcopy(self.restart_policy), 'PidMode': ''},
                "State": {"Running": not self.old_stopped and not self.old_removed, "Paused": self.paused,
                          'Pid': 0 if self.old_stopped else 4321, 'Restarting': False}}
            if self.restart_read_mount_order_drift_once and self.restart_policy['Name'] == 'no' and not self.companion_exists:
                value['Mounts'].reverse()
                self.restart_read_mount_order_drift_once = False
            if self.restart_read_mount_field_drift_once and self.restart_policy['Name'] == 'no' and not self.companion_exists:
                key, changed = self.restart_read_mount_field_drift_once
                value['Mounts'][0][key] = changed
                self.restart_read_mount_field_drift_once = None
            if self.changed_before_pause and self.companion_exists and not self.paused and args[-1] == 'ijm2api':
                value["Id"] = "concurrent-replacement-id"
            return json.dumps([value])
        if args[:2] == ['docker', 'update']:
            assert args[-1] == self.old_id
            desired = args[2].removeprefix('--restart=')
            if desired != 'no' and self.restart_restore_failure:
                raise RuntimeError('SyntheticRestartRestoreFailed')
            if not self.restart_update_ignored:
                name, _, count = desired.partition(':')
                self.restart_policy = {'Name': name, 'MaximumRetryCount': int(count or 0)}
            if self.restart_update_timeout_after_effect:
                self.restart_update_timeout_after_effect = False
                raise TimeoutError('restart policy update acknowledged late')
            return self.old_id
        if args[:2] == ["docker", "run"]:
            assert timeout == 90
            labels = [args[index + 1] for index, value in enumerate(args) if value == '--label']
            assert len(labels) == 2 and labels[0] == cutover.COMPANION_OWNER_LABEL + '=' + self.old_id
            killer = labels[1] == cutover.COMPANION_ROLE_LABEL + '=killer'
            assert labels[1] == cutover.COMPANION_ROLE_LABEL + ('=killer' if killer else '=guard')
            if killer:
                self.killer_name = args[args.index('--name') + 1]
                self.killer_image = args[args.index('--entrypoint') + 2]
            else:
                self.companion_name = args[args.index("--name") + 1]
                self.companion_image = args[args.index("--entrypoint") + 2]
            for flag in ["--rm", "--read-only", "--cap-drop", "--pids-limit"]:
                assert flag in args
            # Read an existing private host-owned data directory without
            # granting file writes, tracing, host namespaces or admin powers.
            assert args.count("--cap-drop") == 1
            assert args[args.index("--cap-drop") + 1] == "ALL"
            assert "--privileged" not in args
            assert not any(cap in args for cap in ["DAC_OVERRIDE", "SYS_ADMIN", "SYS_PTRACE"])
            assert args[args.index("--security-opt") + 1] == "no-new-privileges"
            assert args[args.index("--network") + 1] == "none"
            assert args[args.index("--pull") + 1] == "never"
            mounts = [args[i + 1] for i, value in enumerate(args) if value == '--mount']
            if killer:
                assert '--cap-add' not in args and '--pid' not in args and '--volumes-from' not in args
                assert args[args.index('--cgroupns') + 1] == 'host'
                assert args[-5:-2] == ['-I', '-S', '-B']
                assert len(mounts) == 6 and sum(not value.endswith(',readonly') for value in mounts) == 1
                self.killer_mounts = []
                for mount in mounts:
                    pieces = dict(part.split('=', 1) for part in mount.split(',') if '=' in part)
                    self.killer_mounts.append({'Type': 'bind', 'Source': pieces['src'], 'Destination': pieces['dst'], 'RW': not mount.endswith(',readonly')})
                self.killer_exists = True
                if self.killer_start_timeout_after_effect:
                    raise self.start_error
                return self.killer_id
            assert args.count('--cap-add') == 1 and args[args.index('--cap-add') + 1] == 'DAC_READ_SEARCH'
            assert args[args.index('--volumes-from') + 1] == self.old_id + ':ro'
            assert args[args.index('--pid') + 1] == 'container:' + self.old_id
            assert not mounts and args[args.index('--cgroupns') + 1] == 'host'
            budget = cutover.snapshot_budget(self.source_sizes)
            assert args[args.index('--memory') + 1] == str(budget['memory_bytes'])
            assert args[args.index('--tmpfs') + 1] == '/tmp:rw,noexec,nosuid,size=' + str(budget['tmpfs_bytes']) + ',mode=0700'
            if self.start_failure_before_creation:
                raise self.start_error
            self.companion_exists = True
            if self.start_timeout_after_effect:
                raise self.start_error
            if self.empty_start_receipt:
                return ""
            return self.companion_id
        if args[:2] == ["docker", "pause"]:
            assert args[-1] == self.old_id and not self.paused
            self.paused = True
            if self.pause_timeout_after_effect:
                raise TimeoutError("pause response lost after freezing original")
            return ""
        if args[:2] == ["docker", "exec"]:
            if input and input.startswith('# ORIGINAL_PID_NAMESPACE_WITNESS\n'):
                assert args[3] == self.companion_id and not self.paused and not self.killer_exists
                assert args[-5:] == ['python', '-I', '-S', '-B', '-']
                return json.dumps({'pid_namespace': self.cgroup.identity['pid_namespaces']['original'],
                    'startticks': self.cgroup.identity['startticks'], 'cgroup': ['0::' + self.cgroup.identity['cgroup_path']]})
            if args[3] == self.old_id:
                assert not self.paused and self.guard_count == 0
                assert input == cutover.snapshot_sizing_program() and 'sqlite' not in input
                return json.dumps(self.source_sizes)
            if 'CGROUP_SINGLE_FILE_ACCESS' in (input or ''):
                assert args[3] == self.killer_id and not self.paused
                assert "os.write(fd,b'1')" not in input
                return json.dumps({'access_verified': self.access_allowed, 'writer_pid_namespace': {'device': 4, 'inode': 33}})
            if 'CGROUP_SINGLE_FILE_KILL' in (input or ''):
                assert args[3] == self.killer_id and self.paused and self.guard_count == 2
                assert args[-5:] == ['python', '-I', '-S', '-B', '-']
                assert self.restart_policy == {'Name': 'no', 'MaximumRetryCount': 0}
                assert self.cgroup.armed and self.kill_count == 0
                manifest = self.release / 'frozen-prekill.safe.json'
                attempt = json.loads((self.release / 'frozen-kill-attempt.safe.json').read_text())
                assert attempt['kill_attempted'] is True and attempt['automatic_kill_replay_allowed'] is False
                assert attempt['prekill_manifest_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()
                prekill = json.loads(manifest.read_text())
                assert prekill['frozen_backup_sha256'] == hashlib.sha256(self.backup).hexdigest()
                self.kill_count += 1
                if self.kill_error_before_effect:
                    raise self.kill_error_before_effect
                if self.kill_failure_before_effect:
                    raise TimeoutError('kill command did not give a result')
                self.old_stopped = True
                self.paused = False
                if self.kill_error_after_effect:
                    raise self.kill_error_after_effect
                return json.dumps({'access_verified': True, 'kill_write_returned': True})
            assert args[3] == self.companion_id and self.paused
            assert isinstance(input, str) and "FrozenGuardRejected" in input
            self.guard_count += 1
            if self.guard_count == 1 and self.external_policy_on_first_guard is not None:
                self.restart_policy = deepcopy(self.external_policy_on_first_guard)
            if self.snapshot_capacity_failure:
                raise RuntimeError('FrozenSnapshotCapacityInsufficient')
            if self.guard_count == 1 and self.first_failure:
                raise RuntimeError("FrozenGuardRejected: new active task")
            if self.guard_count == 2 and self.final_failure:
                raise RuntimeError("FrozenGuardRejected: active work arrived")
            value = {"passed": True, "mode": "query_only", "active_count": 4,
                     "binding_sha256": self.first_binding if self.guard_count == 1 else self.final_binding,
                     "source_manifest": self.first_source_manifest if self.guard_count == 1 else self.final_source_manifest}
            if self.guard_count == 1:
                assert "source.backup(dest)" in input
                value["frozen_backup_sha256"] = hashlib.sha256(self.backup).hexdigest()
            else:
                assert "source.backup(dest)" not in input
            return json.dumps(value)
        if args[:2] == ["docker", "unpause"]:
            assert args[-1] == self.old_id and self.paused and not self.old_removed
            self.paused = False
            return ""
        if args[:3] == ["docker", "rm", "--force"]:
            assert args[-1] in (self.companion_id, self.killer_id)
            if args[-1] == self.companion_id: self.companion_exists = False
            else: self.killer_exists = False
            return ""
        if args[:2] == ['docker', 'rm']:
            assert args == ['docker', 'rm', self.old_id] and self.old_stopped and self.kill_count == 1
            assert self.guard_count == 2 and (self.release / 'frozen-death.safe.json').is_file()
            self.old_removed = True
            if self.removal_timeout_after_effect:
                raise TimeoutError('remove response lost after stopping original')
            return ''
        if args[:2] == ["docker", "ps"]:
            assert args == ["docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"]
            assert self.old_removed
            return ""
        raise AssertionError("Unexpected fake Docker command")


class FrozenCutoverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.release = Path(directory.name)
        (self.release / "query-resume-baseline.safe.json").write_text(json.dumps({"binding_sha256": BINDING}))
        self.docker = FakeDocker()

    def invoke(self):
        self.docker.release = self.release
        return cutover.freeze_remove("ijm2api", "ghcr.io/test/ijm2api:immutable", self.release,
                                     QUERY_IDS, "b" * 64, run=self.docker, exporter=self.docker.export_backup,
                                     cgroup_ops=self.docker.cgroup)

    def receipt(self):
        return json.loads((self.release / "frozen-cutover.safe.json").read_text())

    def test_actual_large_database_budget_covers_clone_backup_and_heap(self):
        budget = cutover.snapshot_budget(self.docker.source_sizes)
        self.assertEqual(budget['tmpfs_bytes'], 256 * cutover.MIB)
        self.assertEqual(budget['memory_bytes'], 384 * cutover.MIB)
        self.assertGreater(budget['tmpfs_bytes'], 2 * 69746688 + cutover.SNAPSHOT_HEADROOM)
        self.assertTrue(self.invoke()['passed'])
        self.assertEqual(self.receipt()['snapshot_capacity'], budget)

    def test_wal_growth_changes_budget_and_rounding_remains_bounded(self):
        budget = cutover.snapshot_budget({'ijm2api.db': 100 * cutover.MIB, 'ijm2api.db-wal': 100 * cutover.MIB})
        self.assertEqual(budget['tmpfs_bytes'], 512 * cutover.MIB)
        self.assertEqual(budget['memory_bytes'], 640 * cutover.MIB)
        maximum = cutover.snapshot_budget({'ijm2api.db': 480 * cutover.MIB, 'ijm2api.db-wal': 0})
        self.assertEqual(maximum['tmpfs_bytes'], cutover.SNAPSHOT_MAXIMUM)

    def test_unknown_or_over_limit_size_never_starts_or_pauses_any_container(self):
        for sizes in ({}, {'ijm2api.db': 0, 'ijm2api.db-wal': 0},
                      {'ijm2api.db': True, 'ijm2api.db-wal': 0},
                      {'ijm2api.db': 1, 'ijm2api.db-wal': -1},
                      {'ijm2api.db': 481 * cutover.MIB, 'ijm2api.db-wal': 0}):
            with self.subTest(sizes=sizes):
                self.docker = FakeDocker()
                self.docker.source_sizes = sizes
                with self.assertRaisesRegex(RuntimeError, 'SnapshotSizingInvalid|SnapshotCapacityLimitExceeded'):
                    self.invoke()
                self.assertFalse(any(call[:2] in (['docker', 'run'], ['docker', 'pause'], ['docker', 'unpause'])
                                     or call[:3] == ['docker', 'rm', '--force'] for call in self.docker.calls))
                self.assertFalse(self.receipt()['original_removed_while_frozen'])

    def test_frozen_capacity_failure_unpauses_without_export_or_removal(self):
        self.docker.snapshot_capacity_failure = True
        with self.assertRaisesRegex(RuntimeError, 'FrozenSnapshotCapacityInsufficient'):
            self.invoke()
        self.assertTrue(self.receipt()['original_unpaused_after_abort'])
        self.assertFalse(self.docker.old_removed)
        self.assertFalse(any(call[0] == 'binary_export' for call in self.docker.calls))

    def test_generated_guard_checks_real_free_space_before_cloning_or_sqlite(self):
        (self.release / 'ijm2api.db').write_bytes(b'synthetic database')
        program = cutover.companion_program('', 'b' * 64, backup=True)
        with patch.dict('os.environ', {'IJM_DATA_DIR': str(self.release)}), \
                patch('os.statvfs', create=True, return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)), \
                patch('tempfile.TemporaryDirectory', side_effect=AssertionError('Clone must not start')):
            with self.assertRaisesRegex(RuntimeError, 'FrozenSnapshotCapacityInsufficient'):
                exec(compile(program, 'synthetic_capacity_guard', 'exec'), {})

    def assert_aborted_safely(self):
        self.assertFalse(self.docker.old_removed)
        self.assertFalse(self.docker.paused)
        self.assertTrue(self.receipt()["original_unpaused_after_abort"])
        self.assertFalse(self.receipt()["passed"])
        self.assertIn(["docker", "rm", "--force", self.docker.companion_id], self.docker.calls)
        self.assertNotIn(["docker", "rm", "--force", self.docker.old_id], self.docker.calls)
        self.assertNotIn(['docker', 'rm', self.docker.old_id], self.docker.calls)
        self.assertFalse(self.docker.old_stopped)
        self.assertEqual(self.docker.kill_count, 0)
        self.assertEqual(self.docker.restart_policy, self.docker.original_restart_policy)
        unpause = self.docker.calls.index(["docker", "unpause", self.docker.old_id])
        self.assertEqual(self.docker.calls[unpause - 1], ['cgroup_validate', True])
        self.assertEqual(self.docker.calls[unpause - 2], ["docker", "inspect", self.docker.old_id])

    def test_success_checks_twice_and_removes_original_without_unpausing(self):
        receipt = self.invoke()
        self.assertTrue(receipt["passed"] and receipt["original_removed_while_frozen"])
        self.assertFalse(receipt["original_unpaused_after_abort"])
        actions = [args[1] if args[0] == 'docker' else args[0] for args in self.docker.calls]
        sizing = next(i for i, args in enumerate(self.docker.calls) if args[:4] == ['docker', 'exec', '-i', self.docker.old_id])
        guard = next(i for i, args in enumerate(self.docker.calls) if i > actions.index('pause')
                     and args[:4] == ['docker', 'exec', '-i', self.docker.companion_id])
        self.assertLess(sizing, actions.index('run'))
        self.assertLess(actions.index("pause"), guard)
        self.assertLess(guard, actions.index("binary_export"))
        self.assertLess(actions.index("binary_export"), len(actions) - 3)
        self.assertFalse(any(args[:2] == ["docker", "cp"] for args in self.docker.calls))
        self.assertEqual(self.docker.guard_count, 2)
        self.assertTrue(self.docker.old_removed)
        self.assertTrue(self.docker.old_stopped and self.docker.cgroup.closed)
        self.assertEqual(self.docker.kill_count, 1)
        self.assertIn(['docker', 'rm', self.docker.old_id], self.docker.calls)
        self.assertNotIn(['docker', 'rm', '--force', self.docker.old_id], self.docker.calls)
        self.assertNotIn("unpause", actions)
        self.assertFalse(any(word in args for args in self.docker.calls for word in ["build", "compose", "pull"]))
        self.assertEqual((self.release / "frozen.ijm2api.db").read_bytes(), self.docker.backup)
        self.assertEqual(receipt["server_builds"], 0)
        self.assertEqual(receipt["new_generations"], 0)

    def test_new_active_task_after_pause_aborts_and_unpauses_same_original(self):
        self.docker.first_failure = True
        with self.assertRaisesRegex(RuntimeError, "FrozenGuardRejected"):
            self.invoke()
        self.assert_aborted_safely()
        self.assertFalse(any(args[0] == "binary_export" for args in self.docker.calls))

    def test_original_query_binding_drift_aborts_before_copy_or_removal(self):
        self.docker.first_binding = "c" * 64
        with self.assertRaisesRegex(RuntimeError, "FrozenOriginalBindingChanged"):
            self.invoke()
        self.assert_aborted_safely()
        self.assertFalse(any(args[0] == "binary_export" for args in self.docker.calls))

    def test_bad_backup_digest_aborts_without_second_guard_or_removal(self):
        self.docker.bad_backup = True
        with self.assertRaisesRegex(RuntimeError, "FrozenBackupChecksumMismatch"):
            self.invoke()
        self.assert_aborted_safely()
        self.assertEqual(self.docker.guard_count, 1)

    def test_second_guard_new_work_or_binding_change_aborts(self):
        for failure in ["new-work", "binding"]:
            with self.subTest(failure=failure):
                self.docker = FakeDocker()
                (self.release / "frozen.ijm2api.db").unlink(missing_ok=True)
                if failure == "new-work":
                    self.docker.final_failure = True
                else:
                    self.docker.final_binding = "d" * 64
                with self.assertRaises(RuntimeError):
                    self.invoke()
                self.assert_aborted_safely()

    def test_source_database_or_optional_wal_fingerprint_drift_aborts(self):
        for change in ["database-content", "database-size", "wal-created", "wal-removed"]:
            with self.subTest(change=change):
                self.docker = FakeDocker()
                (self.release / "frozen.ijm2api.db").unlink(missing_ok=True)
                final = self.docker.final_source_manifest
                if change == "database-content":
                    final["ijm2api.db"]["sha256"] = "e" * 64
                elif change == "database-size":
                    final["ijm2api.db"]["size_bytes"] += 1
                elif change == "wal-created":
                    final["ijm2api.db-wal"] = {"sha256": "e" * 64, "size_bytes": 99}
                else:
                    self.docker.first_source_manifest["ijm2api.db-wal"] = {"sha256": "e" * 64, "size_bytes": 99}
                with self.assertRaises(RuntimeError):
                    self.invoke()
                self.assert_aborted_safely()
                self.assertEqual(self.docker.guard_count, 2)
                self.assertEqual(sum(args[0] == "binary_export" for args in self.docker.calls), 1)

    def test_concurrent_container_replacement_is_not_paused_or_removed(self):
        self.docker.changed_before_pause = True
        with self.assertRaisesRegex(RuntimeError, "OriginalAppChangedBeforeFreeze"):
            self.invoke()
        self.assertFalse(self.docker.paused or self.docker.old_removed)
        self.assertFalse(any(args[0] == "binary_export" or args[0] == 'docker' and args[1] in ["pause", "unpause", "cp"] for args in self.docker.calls))
        self.assertIn(["docker", "rm", "--force", self.docker.companion_id], self.docker.calls)

    def test_removal_timeout_is_reconciled_as_gone_without_unpausing(self):
        self.docker.removal_timeout_after_effect = True
        receipt = self.invoke()
        self.assertTrue(receipt["passed"] and self.docker.old_removed)
        self.assertEqual(receipt["removal_timeout_resolved"], "original_absent")
        self.assertFalse(receipt["original_unpaused_after_abort"])
        self.assertFalse(any(args[:2] == ['docker', 'unpause'] for args in self.docker.calls))
        self.assertTrue(any(args[:2] == ['docker', 'ps'] for args in self.docker.calls))

    def test_receipt_write_failure_cannot_replace_successful_cutover_return(self):
        messages = io.StringIO()
        with patch.object(Path, "write_text", side_effect=OSError("private receipt disk failure")), redirect_stderr(messages):
            receipt = self.invoke()
        self.assertTrue(receipt["passed"] and self.docker.old_removed)
        self.assertFalse(any(args[:2] == ['docker', 'unpause'] for args in self.docker.calls))
        self.assertIn("Frozen cutover receipt could not be written", messages.getvalue())
        self.assertNotIn("private receipt disk failure", messages.getvalue())
        self.assertIn(["docker", "rm", "--force", self.docker.companion_id], self.docker.calls)

    def test_pause_timeout_after_effect_still_unpauses_same_original(self):
        self.docker.pause_timeout_after_effect = True
        with self.assertRaises(TimeoutError):
            self.invoke()
        self.assert_aborted_safely()
        self.assertEqual(self.docker.guard_count, 0)

    def test_binary_stream_export_failure_unpauses_without_removal_or_paid_action(self):
        self.docker.export_failure = True
        with self.assertRaisesRegex(RuntimeError, "SyntheticStreamExportFailed"):
            self.invoke()
        self.assert_aborted_safely()
        self.assertEqual(self.docker.guard_count, 1)
        self.assertEqual(sum(args[0] == "binary_export" for args in self.docker.calls), 1)
        self.assertFalse(any(args[:2] == ["docker", "cp"] for args in self.docker.calls))

    def assert_start_failure_preserves_original(self):
        self.assertFalse(self.docker.paused or self.docker.old_removed)
        self.assertEqual(self.docker.guard_count, 0)
        self.assertEqual(sum(args[:2] == ["docker", "run"] for args in self.docker.calls), 1)
        self.assertFalse(any(args[:2] in [["docker", "pause"], ["docker", "unpause"]] for args in self.docker.calls))
        self.assertNotIn(["docker", "rm", "--force", self.docker.old_id], self.docker.calls)
        self.assertFalse(self.receipt()["passed"])

    def test_start_timeout_after_creation_cleans_owned_companion_without_pausing_original(self):
        self.docker.start_timeout_after_effect = True
        with self.assertRaises(TimeoutError) as found:
            self.invoke()
        self.assertIs(found.exception, self.docker.start_error)
        self.assert_start_failure_preserves_original()
        self.assertFalse(self.docker.companion_exists)
        self.assertTrue(self.receipt()["owned_companion_cleanup_completed"])
        self.assertIn(["docker", "rm", "--force", self.docker.companion_id], self.docker.calls)

    def test_start_failure_without_creation_never_removes_any_container(self):
        self.docker.start_failure_before_creation = True
        with self.assertRaises(TimeoutError) as found:
            self.invoke()
        self.assertIs(found.exception, self.docker.start_error)
        self.assert_start_failure_preserves_original()
        self.assertFalse(any(args[:3] == ["docker", "rm", "--force"] for args in self.docker.calls))
        self.assertTrue(self.receipt()["companion_cleanup_requires_operator_review"])

    def test_uncertain_start_never_removes_foreign_or_uninspectable_companion(self):
        for field in ["name", "owner", "image", "pid", "readonly", "network", "original-id", "inspect-failed"]:
            with self.subTest(field=field):
                self.docker = FakeDocker()
                self.docker.start_timeout_after_effect = True
                self.docker.foreign_companion_field = field
                self.docker.cleanup_inspect_failure = field == "inspect-failed"
                with self.assertRaises(TimeoutError) as found:
                    self.invoke()
                self.assertIs(found.exception, self.docker.start_error)
                self.assert_start_failure_preserves_original()
                self.assertFalse(any(args[:3] == ["docker", "rm", "--force"] for args in self.docker.calls))
                self.assertTrue(self.receipt()["companion_cleanup_requires_operator_review"])

    def test_empty_start_receipt_is_rejected_and_owned_companion_is_cleaned_before_pause(self):
        self.docker.empty_start_receipt = True
        with self.assertRaisesRegex(RuntimeError, "CompanionStartReceiptInvalid"):
            self.invoke()
        self.assert_start_failure_preserves_original()
        self.assertFalse(self.docker.companion_exists)

    def test_kill_missing_ack_after_effect_uses_host_proof_without_replay_or_restore(self):
        for failure in (TimeoutError('exec response lost'), RuntimeError('exit137 namespace died')):
            with self.subTest(error=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                self.release = Path(directory)
                (self.release / 'query-resume-baseline.safe.json').write_text(json.dumps({'binding_sha256': BINDING}))
                self.docker = FakeDocker()
                self.docker.kill_error_after_effect = failure
                receipt = self.invoke()
                self.assertTrue(receipt['passed'] and receipt['kill_command_acknowledgement_missing'])
                self.assertEqual(self.docker.kill_count, 1)
                self.assertEqual(receipt['kill_command_invocations'], 1)
                self.assertTrue(self.docker.old_removed)
                self.assertEqual(self.docker.restart_policy['Name'], 'no')
                self.assertNotIn(['docker', 'unpause', self.docker.old_id], self.docker.calls)
                updates = [call for call in self.docker.calls if call[:2] == ['docker', 'update']]
                self.assertEqual(updates, [['docker', 'update', '--restart=no', self.docker.old_id]])

    def test_kill_without_effect_is_irreversible_and_never_unpaused_or_replayed(self):
        self.docker.kill_failure_before_effect = True
        with self.assertRaisesRegex(RuntimeError, 'CgroupDeathNotProven'):
            self.invoke()
        self.assertEqual(self.docker.kill_count, 1)
        self.assertTrue(self.docker.paused)
        self.assertFalse(self.docker.old_stopped or self.docker.old_removed)
        self.assertEqual(self.docker.restart_policy['Name'], 'no')
        self.assertTrue(self.receipt()['postkill_outcome_requires_operator_review'])
        self.assertFalse(self.receipt()['passed'])
        self.assertNotIn(['docker', 'unpause', self.docker.old_id], self.docker.calls)
        self.assertNotIn(['docker', 'rm', self.docker.old_id], self.docker.calls)
        self.assertTrue((self.release / 'frozen-kill-attempt.safe.json').is_file())
        with self.assertRaisesRegex(RuntimeError, 'OriginalAppNotRunningUnpaused|CutoverAttemptAlreadyRecorded'):
            self.invoke()
        self.assertEqual(self.docker.kill_count, 1)

    def test_docker_stopped_without_complete_cgroup_death_proof_cannot_remove(self):
        for proof in ({'empty_proof': 'unknown', 'original_pid_gone': True},
                      {'empty_proof': 'held_events_populated_zero', 'original_pid_gone': False}):
            with self.subTest(proof=proof), tempfile.TemporaryDirectory() as directory:
                self.release = Path(directory)
                (self.release / 'query-resume-baseline.safe.json').write_text(json.dumps({'binding_sha256': BINDING}))
                self.docker = FakeDocker()
                with patch.object(self.docker.cgroup, 'wait_empty', return_value=proof):
                    with self.assertRaisesRegex(RuntimeError, 'CgroupDeathNotProven'):
                        self.invoke()
                self.assertTrue(self.docker.old_stopped)
                self.assertFalse(self.docker.old_removed)
                self.assertEqual(self.docker.restart_policy['Name'], 'no')
                self.assertNotIn(['docker', 'unpause', self.docker.old_id], self.docker.calls)
                self.assertNotIn(['docker', 'rm', self.docker.old_id], self.docker.calls)

    def test_prewrite_failure_records_fixed_stages_returncode_and_zombie_without_leaking_stderr(self):
        private = 'PRIVATE_TOKEN=must-never-appear-in-receipt https://private.invalid/credential'
        failure = subprocess.CalledProcessError(1, ['synthetic-exec'], stderr=(
            'CGROUP_KILL_STAGE=start\nRuntimeError: CgroupBindIdentityChanged\n' + private + '\n'))
        self.docker.kill_error_before_effect = failure
        with self.assertRaisesRegex(RuntimeError, 'CgroupDeathNotProven'):
            self.invoke()
        receipt = self.receipt()
        diagnostic = receipt['kill_command_diagnostic']
        self.assertFalse(diagnostic['returned'])
        self.assertEqual(diagnostic['returncode'], 1)
        self.assertEqual(diagnostic['stages_observed'], ['start'])
        self.assertEqual(diagnostic['guest_error_reasons'], ['CgroupBindIdentityChanged'])
        self.assertEqual(receipt['cgroup_wait_diagnostic']['original_pid']['state'], 'Z')
        self.assertTrue(receipt['cgroup_wait_diagnostic']['original_pid']['original_startticks_match'])
        docker_state = receipt['old_container_wait_diagnostic']
        self.assertTrue(docker_state['exact_old_id_matched'])
        self.assertEqual(docker_state['state'], {'Running': True, 'Paused': True, 'Restarting': False, 'Pid': 4321})
        self.assertEqual(docker_state['restart_policy'], {'Name': 'no', 'MaximumRetryCount': 0})
        encoded = json.dumps(receipt)
        self.assertNotIn(private, encoded)
        self.assertNotIn('PRIVATE_TOKEN', encoded)
        self.assertEqual(self.docker.kill_count, 1)
        self.assertTrue(receipt['postkill_outcome_requires_operator_review'])
        self.assertNotIn(['docker', 'unpause', self.docker.old_id], self.docker.calls)
        self.assertFalse(self.docker.old_removed)

    def test_missing_ack_keeps_fixed_write_stage_but_still_requires_independent_death(self):
        self.docker.kill_error_after_effect = subprocess.CalledProcessError(137, ['synthetic-exec'], stderr=(
            'CGROUP_KILL_STAGE=start\nCGROUP_KILL_STAGE=prewrite_checks_passed\n'
            'CGROUP_KILL_STAGE=write_returned\nPRIVATE_TOKEN=omit\n'))
        receipt = self.invoke()
        self.assertTrue(receipt['passed'] and receipt['kill_command_acknowledgement_missing'])
        self.assertEqual(receipt['kill_command_diagnostic']['returncode'], 137)
        self.assertEqual(receipt['kill_command_diagnostic']['stages_observed'], ['start', 'prewrite_checks_passed', 'write_returned'])
        self.assertTrue(receipt['cgroup_death']['original_pid_gone'])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(receipt))
        self.assertEqual(self.docker.kill_count, 1)

    def test_success_stdout_stays_one_json_and_diagnostic_does_not_store_raw_output(self):
        program = cutover.cgroup_companion_program(self.docker.cgroup.identity, kill=True)
        self.assertIn("file=sys.stderr,flush=True", program)
        self.assertEqual(program.count("os.write(fd,b'1')"), 1)
        self.assertIn("'kill_write_returned':True", program)
        raw = json.dumps({'access_verified': True, 'kill_write_returned': True, 'raw_secret': 'PRIVATE_TOKEN=omit'})
        self.assertEqual(cutover.kill_command_diagnostic(output=raw), {
            'returned': True, 'returncode': 0, 'timed_out': False, 'stages_observed': [], 'guest_write_returned': True})
        receipt = self.invoke()
        self.assertTrue(receipt['kill_command_diagnostic']['guest_write_returned'])
        self.assertEqual(receipt['old_container_death_diagnostic']['state']['Pid'], 0)
        self.assertTrue(receipt['passed'])

    def test_diagnostic_failures_never_replace_original_death_error_or_authorize_recovery(self):
        self.docker.kill_failure_before_effect = True
        self.docker.diagnostic_inspect_failure = True
        with patch.object(self.docker.cgroup, 'diagnostic', side_effect=RuntimeError('PRIVATE_TOKEN=diagnostic-only')):
            with self.assertRaisesRegex(RuntimeError, '^CgroupDeathNotProven$'):
                self.invoke()
        receipt = self.receipt()
        self.assertEqual(receipt['cgroup_wait_diagnostic'], {'error_class': 'RuntimeError', 'errno': None})
        self.assertEqual(receipt['old_container_wait_diagnostic'], {'error_class': 'PermissionError', 'errno': 13})
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(receipt))
        self.assertFalse(receipt['passed'] or receipt['original_unpaused_after_abort'])
        self.assertEqual(self.docker.kill_count, 1)
        self.assertEqual(self.docker.restart_policy['Name'], 'no')

    def test_host_diagnostic_captures_state_ticks_empty_events_and_watcher_errors_only(self):
        group = cutover.HostCgroup()
        group.pid, group.ticks = 4321, 123456
        group.path = Path('/sys/fs/cgroup/synthetic-original')
        group.fds = {'directory': 7101, 'events': 7102}
        group.identities = {'directory': {'device': 7, 'inode': 11}}
        group.last_events = {'populated': 1, 'frozen': 1}
        group.last_events_read_bytes = 0
        group.last_watch_error = {'error_class': 'PermissionError', 'errno': 13}
        group.watch_error_count = 2
        stat_bytes = ('4321 (synthetic comm) ' + ' '.join(['Z'] + ['0'] * 18 + ['123456', '0'])).encode()
        with patch.object(Path, 'open', return_value=io.BytesIO(stat_bytes)), \
                patch.object(cutover.os, 'pread', create=True, return_value=b'') as read_events, \
                patch.object(cutover.os, 'readlink', return_value=str(group.path) + ' (deleted)'), \
                patch.object(cutover.os, 'fstat', return_value=SimpleNamespace(st_dev=7, st_ino=11)):
            value = group.diagnostic()
        self.assertEqual(value['original_pid'], {'present': True, 'state': 'Z', 'startticks': 123456, 'expected_startticks': 123456, 'original_startticks_match': True})
        self.assertEqual(value['current_held_events_read_bytes'], 0)
        self.assertEqual(value['current_held_events']['error_class'], 'RuntimeError')
        self.assertEqual(value['held_directory_fd_link'], str(group.path) + ' (deleted)')
        self.assertTrue(value['held_directory_identity_match'])
        self.assertEqual(value['last_watch_error'], {'error_class': 'PermissionError', 'errno': 13})
        read_events.assert_called_once_with(7102, 4096, 0)

    def test_real_execute_and_smoke_transport_keep_returncode_without_publishing_private_stderr(self):
        stderr = 'CGROUP_KILL_STAGE=start\nPermissionError: [Errno 13] fixed-path\nPRIVATE_TOKEN=omit\n'
        failed = SimpleNamespace(returncode=1, stdout='', stderr=stderr)
        program = cutover.cgroup_companion_program(self.docker.cgroup.identity, kill=True)
        with patch.object(cutover.subprocess, 'run', return_value=failed) as child:
            with self.assertRaises(RuntimeError) as error:
                cutover.execute(['docker', 'exec', '-i', 'synthetic', 'python', '-B', '-'], input=program)
        diagnostic = cutover.kill_command_diagnostic(error=error.exception)
        self.assertEqual(diagnostic['returncode'], 1)
        self.assertEqual(diagnostic['stderr_errno'], [13])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(diagnostic))
        child.assert_called_once()
        path = MODULE.parents[1] / 'tools/test_deploy_freeze_runtime.py'
        spec = importlib.util.spec_from_file_location('synthetic_smoke_transport', path)
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        with patch.object(smoke.subprocess, 'run', return_value=failed) as child:
            with self.assertRaises(RuntimeError) as error:
                smoke.docker(['exec', '-i', 'synthetic', 'python', '-B', '-'], input=program)
        self.assertEqual(cutover.kill_command_diagnostic(error=error.exception), diagnostic)
        child.assert_called_once()

    def test_restart_update_result_is_read_back_and_original_retry_count_is_restored(self):
        self.docker.restart_update_timeout_after_effect = True
        self.assertTrue(self.invoke()['passed'])
        self.assertTrue(self.receipt()['restart_update_client_result_unknown'])
        self.assertEqual(sum(call[:2] == ['docker', 'update'] for call in self.docker.calls), 1)
        with tempfile.TemporaryDirectory() as directory:
            self.release = Path(directory)
            (self.release / 'query-resume-baseline.safe.json').write_text(json.dumps({'binding_sha256': BINDING}))
            self.docker = FakeDocker()
            self.docker.restart_policy = {'Name': 'on-failure', 'MaximumRetryCount': 3}
            self.docker.original_restart_policy = deepcopy(self.docker.restart_policy)
            self.docker.first_failure = True
            with self.assertRaisesRegex(RuntimeError, 'FrozenGuardRejected'):
                self.invoke()
            self.assert_aborted_safely()
            self.assertIn(['docker', 'update', '--restart=on-failure:3', self.docker.old_id], self.docker.calls)

    def test_mount_order_only_drift_preserves_effective_bindings_and_passes_canonical_guard(self):
        self.docker.mounts = [
            {'Destination': '/app/data', 'Source': '/PRIVATE_SOURCE_SECRET/data', 'Type': 'bind', 'RW': True},
            {'Destination': '/app/profiles', 'Source': '/PRIVATE_SOURCE_SECRET/profile', 'Type': 'bind', 'RW': False}]
        self.docker.restart_read_mount_order_drift_once = True
        original = json.loads(self.docker(['docker', 'inspect', 'ijm2api']))[0]
        current = deepcopy(original)
        current['Mounts'].reverse()
        current['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
        value = cutover.restart_suppression_diagnostic(original, current, self.docker.cgroup.identity, self.docker.cgroup)
        self.assertTrue(all(value['predicates'].values()))
        self.assertTrue(all(value['stable_field_equalities'].values()))
        self.assertFalse(value['Mounts']['order_sensitive_hashes_equal'])
        self.assertTrue(value['Mounts']['sorted_destination_hashes_equal'])
        self.assertEqual(value['Mounts']['original_destination_order'], ['/app/data', '/app/profiles'])
        self.assertEqual(value['Mounts']['current_destination_order'], ['/app/profiles', '/app/data'])
        self.assertNotIn('PRIVATE_SOURCE_SECRET', json.dumps(value))
        before = deepcopy(self.docker.mounts)
        receipt = self.invoke()
        self.assertTrue(receipt['passed'] and self.docker.old_removed)
        self.assertEqual(self.docker.kill_count, 1)
        self.assertEqual(self.docker.mounts, before)

    def test_mount_source_rw_type_and_propagation_drift_each_still_reject_before_pause(self):
        for key, changed in (('Source', '/different'), ('RW', False), ('Type', 'volume'), ('Propagation', 'rshared')):
            with self.subTest(field=key):
                self.docker = FakeDocker()
                self.docker.mounts = [{'Destination': '/app/data', 'Source': '/original', 'RW': True,
                    'Type': 'bind', 'Propagation': 'rprivate', 'Mode': 'z', 'Driver': 'local'}]
                self.docker.restart_read_mount_field_drift_once = (key, changed)
                with self.assertRaisesRegex(RuntimeError, '^OriginalRestartSuppressionNotVerified$'):
                    self.invoke()
                value = self.receipt()['restart_suppression_diagnostic']
                self.assertFalse(value['predicates']['stable_facts_equal'])
                self.assertFalse(value['Mounts']['sorted_destination_hashes_equal'])
                self.assertEqual(self.docker.kill_count, 0)
                self.assertFalse(self.docker.paused or self.docker.old_removed)
                self.assertTrue(self.receipt()['original_restart_policy_restored_after_abort'])

    def test_duplicate_or_invalid_mount_destinations_reject_before_any_container_mutation(self):
        bad_mounts = [None, {}, 'not-a-list', [{}], [{'Destination': '/data'}, {'Destination': '/data'}]]
        bad_mounts += [[{'Destination': value}] for value in ('', 'relative', '/app/../data', '/app//data', '/app/\x00data', None, 7)]
        for mounts in bad_mounts:
            with self.subTest(mounts=mounts):
                self.docker = FakeDocker()
                self.docker.mounts = mounts
                with self.assertRaisesRegex(RuntimeError, '^ContainerMount(?:ListInvalid|DestinationInvalid|DestinationDuplicate)$'):
                    self.invoke()
                self.assertEqual(self.docker.kill_count, 0)
                self.assertFalse(self.docker.paused or self.docker.old_removed)
                self.assertFalse(any(call[:2] in (['docker', 'update'], ['docker', 'run'], ['docker', 'pause']) for call in self.docker.calls))
                self.assertNotIn(['cgroup_capture'], self.docker.calls)

    def test_canonical_mounts_preserve_all_fields_and_original_input_without_filtering(self):
        mounts = [{'Destination': '/z', 'Source': '/private-z', 'Type': 'bind', 'RW': False,
            'Propagation': 'rslave', 'Mode': 'ro,z', 'opaque_future_field': {'a': [1, 2]}},
            {'Destination': '/', 'Source': '/private-root', 'Type': 'volume', 'RW': True, 'Name': 'volume-name'}]
        before = deepcopy(mounts)
        result = cutover.canonical_mounts(mounts)
        self.assertEqual([mount['Destination'] for mount in result], ['/', '/z'])
        self.assertEqual(result, [before[1], before[0]])
        self.assertEqual(mounts, before)
        self.assertIsNot(result, mounts)
        self.assertIs(result[1], mounts[0])

    def test_suppression_diagnostic_keeps_only_config_hashes_changed_keys_and_state(self):
        original = json.loads(self.docker(['docker', 'inspect', 'ijm2api']))[0]
        original['Config']['Env'] = ['PRIVATE_ENV=original-secret']
        original['HostConfig']['LogConfig'] = {'private-auth': 'original-secret'}
        original['Mounts'] = [{'Destination': '/app/data', 'Source': '/PRIVATE_SOURCE_SECRET', 'RW': True}]
        current = deepcopy(original)
        current['Config']['Env'] = ['PRIVATE_ENV=current-secret']
        current['HostConfig']['LogConfig'] = {'private-auth': 'current-secret'}
        current['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
        current['RestartCount'] = 1
        current['State'].update(Pid=99, Running=False, Paused=True)
        value = cutover.restart_suppression_diagnostic(original, current, self.docker.cgroup.identity, self.docker.cgroup)
        self.assertEqual(value['Config']['changed_top_level_keys'], ['Env'])
        self.assertEqual(value['HostConfig']['changed_top_level_keys'], ['LogConfig', 'RestartPolicy'])
        self.assertNotEqual(value['Config']['original_sha256'], value['Config']['current_sha256'])
        self.assertFalse(value['predicates']['restart_count_equal'])
        self.assertFalse(value['predicates']['pid_matches_captured_identity'])
        self.assertFalse(value['predicates']['running_truthy'])
        self.assertFalse(value['predicates']['paused_falsy'])
        self.assertTrue(value['predicates']['restart_policy_is_no'])
        encoded = json.dumps(value)
        for private in ('PRIVATE_ENV', 'original-secret', 'current-secret', 'PRIVATE_SOURCE_SECRET', 'private-auth'):
            self.assertNotIn(private, encoded)
        self.assertEqual(value['current']['state']['Pid'], 99)
        self.assertEqual(value['current']['restart_count'], 1)

    def test_suppression_observation_failure_never_changes_original_rejection_or_cleanup(self):
        self.docker.restart_update_ignored = True
        with patch.object(cutover, 'restart_suppression_diagnostic', side_effect=ValueError('PRIVATE_CONFIG_SECRET')):
            with self.assertRaisesRegex(RuntimeError, '^OriginalRestartSuppressionNotVerified$'):
                self.invoke()
        value = self.receipt()['restart_suppression_diagnostic']
        self.assertEqual(value, {'error_class': 'ValueError', 'errno': None})
        self.assertNotIn('PRIVATE_CONFIG_SECRET', json.dumps(self.receipt()))
        self.assertEqual(self.docker.kill_count, 0)
        self.assertFalse(self.docker.paused or self.docker.old_removed)
        self.assertTrue(self.receipt()['original_restart_policy_restored_after_abort'])

    def test_capture_access_or_restart_suppression_failure_never_pauses_or_kills(self):
        for failure in ('capture', 'access', 'restart'):
            with self.subTest(failure=failure):
                self.docker = FakeDocker()
                if failure == 'capture': self.docker.cgroup.capture_failure = True
                if failure == 'access': self.docker.access_allowed = False
                if failure == 'restart': self.docker.restart_update_ignored = True
                with self.assertRaisesRegex(RuntimeError, 'SyntheticCgroupUnavailable|SingleFileCgroupAccessNotVerified|OriginalRestartSuppressionNotVerified'):
                    self.invoke()
                self.assertTrue(self.docker.cgroup.closed)
                self.assertFalse(self.docker.paused or self.docker.old_stopped or self.docker.old_removed)
                self.assertEqual(self.docker.kill_count, 0)
                self.assertEqual(self.docker.restart_policy, self.docker.original_restart_policy)
                self.assertFalse(any(call[:2] in (['docker', 'pause'], ['docker', 'unpause']) for call in self.docker.calls))

    def test_partial_kill_journal_blocks_recovery_but_no_marker_allows_safe_abort(self):
        real_durable = cutover.durable_json
        for partial in (False, True):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as directory:
                self.release = Path(directory)
                (self.release / 'query-resume-baseline.safe.json').write_text(json.dumps({'binding_sha256': BINDING}))
                self.docker = FakeDocker()
                def failing_journal(path, value):
                    if Path(path).name == 'frozen-kill-attempt.safe.json':
                        if partial: Path(path).write_bytes(b'partial-attempt-marker')
                        raise OSError('synthetic journal commit failure')
                    return real_durable(path, value)
                with patch.object(cutover, 'durable_json', side_effect=failing_journal):
                    with self.assertRaisesRegex(OSError, 'synthetic journal commit failure'):
                        self.invoke()
                self.assertEqual(self.docker.kill_count, 0)
                self.assertEqual(self.receipt()['kill_command_invocations'], 0)
                self.assertFalse(self.docker.old_stopped or self.docker.old_removed)
                if partial:
                    self.assertTrue(self.docker.paused)
                    self.assertTrue(self.receipt()['kill_journal_commit_uncertain'])
                    self.assertEqual(self.docker.restart_policy['Name'], 'no')
                    self.assertNotIn(['docker', 'unpause', self.docker.old_id], self.docker.calls)
                    self.assertEqual((self.release / 'frozen-kill-attempt.safe.json').read_bytes(), b'partial-attempt-marker')
                else:
                    self.assert_aborted_safely()

    def test_abort_cas_never_overwrites_external_policy_or_unpauses_on_restore_failure(self):
        for failure in ('external', 'restore'):
            with self.subTest(failure=failure):
                self.docker = FakeDocker()
                self.docker.first_failure = True
                if failure == 'external':
                    self.docker.external_policy_on_first_guard = {'Name': 'on-failure', 'MaximumRetryCount': 2}
                else:
                    self.docker.restart_restore_failure = True
                with self.assertRaisesRegex(RuntimeError, 'FrozenGuardRejected'):
                    self.invoke()
                self.assertTrue(self.docker.paused)
                self.assertTrue(self.receipt()['prekill_abort_recovery_requires_operator_review'])
                self.assertEqual(self.docker.kill_count, 0)
                self.assertNotIn(['docker', 'unpause', self.docker.old_id], self.docker.calls)
                if failure == 'external':
                    self.assertEqual(self.docker.restart_policy, self.docker.external_policy_on_first_guard)
                    self.assertEqual(sum(call[:2] == ['docker', 'update'] for call in self.docker.calls), 1)

    def test_host_deletion_proof_requires_held_identity_deleted_link_and_absent_original(self):
        path = Path('/sys/fs/cgroup/synthetic-original')
        for case in ('events-zero', 'missing-only', 'renamed', 'deleted', 'replaced', 'still-original', 'held-changed'):
            with self.subTest(case=case):
                group = cutover.HostCgroup()
                group.path = path
                group.fds = {'directory': 7101, 'events': 7102}
                group.identities = {'directory': {'device': 7, 'inode': 11}}
                held = SimpleNamespace(st_dev=7, st_ino=12 if case == 'held-changed' else 11)
                current = SimpleNamespace(st_dev=7, st_ino=99 if case == 'replaced' else 11)
                link = str(path) + (' (deleted)' if case in {'deleted', 'replaced', 'still-original'} else '')
                if case == 'renamed': link = str(path) + '-renamed'
                event_bytes = b'populated 0\nfrozen 1\n' if case == 'events-zero' else b'populated 1\nfrozen 1\n'
                effect = FileNotFoundError() if case in {'missing-only', 'renamed', 'deleted'} else None
                with patch.object(cutover.os, 'fstat', return_value=held), \
                        patch.object(cutover.os, 'lseek', return_value=0), \
                        patch.object(cutover.os, 'read', return_value=event_bytes), \
                        patch.object(cutover.os, 'readlink', return_value=link), \
                        patch.object(Path, 'lstat', side_effect=effect, return_value=current):
                    if case == 'held-changed':
                        with self.assertRaisesRegex(RuntimeError, 'HeldCgroupDirectoryChanged'): group._empty()
                    elif case in {'events-zero', 'deleted', 'replaced'}:
                        proof = group._empty()
                        self.assertEqual(proof['empty_proof'], 'held_events_populated_zero' if case == 'events-zero' else 'held_original_cgroup_deleted')
                    else:
                        self.assertIsNone(group._empty())

    def test_companion_program_has_one_write_only_in_kill_and_shell_blocks_postkill_recovery(self):
        access = cutover.cgroup_companion_program(self.docker.cgroup.identity)
        kill = cutover.cgroup_companion_program(self.docker.cgroup.identity, kill=True)
        compile(access, 'synthetic_access', 'exec')
        compile(kill, 'synthetic_kill', 'exec')
        self.assertNotIn('os.write(', access)
        self.assertEqual(kill.count("os.write(fd,b'1')"), 1)
        self.assertIn("events.get('frozen')!='1'", kill)
        shell = (MODULE.parent / 'deploy_pre.sh').read_text()
        branch = shell[shell.index('>"$release/frozen-cutover.private.log" 2>&1; then'):]
        self.assertLess(branch.index('frozen-kill-attempt.safe.json'), branch.index('recovery="'))
        self.assertIn('refusing automatic restart or rollback', branch)
        self.assertIn('exit 1', branch[:branch.index('recovery="')])
        marker_program = branch.split("<<'FROZEN_KILL_MARKER'\n", 1)[1].split('\nFROZEN_KILL_MARKER', 1)[0]
        for name, failure, metadata, expected in (
                ('missing', FileNotFoundError(), None, 0),
                ('exists', None, SimpleNamespace(st_mode=0o100600, st_size=100), 1),
                ('permission', PermissionError(), None, 1),
                ('io', OSError(5, 'synthetic I/O error'), None, 1),
                ('dangling-symlink', None, SimpleNamespace(st_mode=0o120777, st_size=20), 1),
                ('partial-marker', None, SimpleNamespace(st_mode=0o100600, st_size=7), 1)):
            with self.subTest(marker=name), \
                    patch.object(cutover.os, 'lstat', side_effect=failure, return_value=metadata), \
                    patch.object(cutover.sys, 'argv', ['shell-marker-guard', '/synthetic/attempt']):
                with self.assertRaises(SystemExit) as result:
                    exec(compile(marker_program, 'actual_shell_marker_guard', 'exec'), {})
                self.assertEqual(result.exception.code, expected)

    def test_deleted_cgroup_evidence_still_requires_pid_death_then_nonforce_remove(self):
        self.docker.cgroup.death_proof = 'held_original_cgroup_deleted'
        receipt = self.invoke()
        self.assertTrue(receipt['passed'])
        self.assertEqual(receipt['cgroup_death']['empty_proof'], 'held_original_cgroup_deleted')
        self.assertEqual(self.docker.kill_count, 1)
        self.assertNotIn(['docker', 'rm', '--force', self.docker.old_id], self.docker.calls)

    def test_two_roles_keep_guard_read_only_and_send_single_write_only_to_private_killer(self):
        receipt = self.invoke()
        runs = [call for call in self.docker.calls if call[:2] == ['docker', 'run']]
        self.assertEqual(len(runs), 2)
        guard, killer = runs
        self.assertIn(cutover.COMPANION_ROLE_LABEL + '=guard', guard)
        self.assertIn(cutover.COMPANION_ROLE_LABEL + '=killer', killer)
        self.assertIn('--pid', guard)
        self.assertNotIn('--mount', guard)
        self.assertNotIn('--pid', killer)
        self.assertNotIn('--volumes-from', killer)
        self.assertNotIn('--cap-add', killer)
        writes = [body for body in self.docker.inputs if body and body.startswith('# CGROUP_SINGLE_FILE_KILL\n')]
        self.assertEqual(len(writes), 1)
        self.assertIn(['docker', 'exec', '-i', self.docker.killer_id, 'python', '-I', '-S', '-B', '-'], self.docker.calls)
        self.assertEqual(self.docker.guard_count, 2)
        self.assertTrue(receipt['killer_identity']['private_sibling_verified'])
        self.assertTrue(receipt['original_namespace_witness']['shared_guard_witness_verified'])
        self.assertFalse(self.docker.killer_exists or self.docker.companion_exists)

    def test_lost_killer_start_receipt_cleans_both_exact_roles_before_pause(self):
        self.docker.killer_start_timeout_after_effect = True
        with self.assertRaises(TimeoutError) as error:
            self.invoke()
        self.assertIs(error.exception, self.docker.start_error)
        self.assertFalse(self.docker.killer_exists or self.docker.companion_exists)
        self.assertEqual(self.docker.kill_count, 0)
        self.assertFalse(any(call[:2] == ['docker', 'pause'] for call in self.docker.calls))
        self.assertEqual(self.docker.restart_policy, self.docker.original_restart_policy)

    def test_killer_foreign_role_command_privilege_or_bind_is_never_removed_as_ours(self):
        for field in ('role', 'cmd', 'capadd', 'mount-rw', 'pid', 'owner', 'image', 'network'):
            with self.subTest(field=field):
                self.docker = FakeDocker()
                self.docker.foreign_killer_field = field
                with self.assertRaisesRegex(RuntimeError, 'KillerPrivateSpecificationRejected'):
                    self.invoke()
                self.assertTrue(self.docker.killer_exists)
                self.assertEqual(self.docker.kill_count, 0)
                self.assertNotIn(['docker', 'rm', '--force', self.docker.killer_id], self.docker.calls)
                self.assertFalse(any(call[:2] == ['docker', 'pause'] for call in self.docker.calls))

    def test_known_killer_id_never_cleans_a_same_name_replacement(self):
        self.docker.killer_replaced_at_cleanup = True
        receipt = self.invoke()
        self.assertTrue(receipt['passed'] and receipt['killer_cleanup_requires_operator_review'])
        self.assertTrue(self.docker.killer_exists)
        self.assertNotIn(['docker', 'rm', '--force', 'f' * 64], self.docker.calls)
        self.assertNotIn(['docker', 'rm', '--force', self.docker.killer_id], self.docker.calls)

    def test_namespace_witness_must_bind_original_ticks_cgroup_and_different_host_namespace(self):
        for changed in ('valid', 'same-as-host', 'ticks', 'cgroup', 'invalid'):
            with self.subTest(changed=changed):
                group = cutover.HostCgroup()
                group.pid, group.ticks, group.relative = 4321, 123456, '/synthetic-old'
                group.path = Path('/sys/fs/cgroup/synthetic-old')
                group.namespaces = {'original': None, 'controller': {'device': 4, 'inode': 22}}
                witness = {'pid_namespace': {'device': 4, 'inode': 11}, 'startticks': 123456, 'cgroup': ['0::/synthetic-old']}
                with self.assertRaisesRegex(RuntimeError, 'OriginalNamespaceWitnessRequired'):
                    group.mount_args()
                if changed == 'same-as-host': witness['pid_namespace'] = {'device': 4, 'inode': 22}
                elif changed == 'ticks': witness['startticks'] = 999
                elif changed == 'cgroup': witness['cgroup'] = ['0::/foreign']
                elif changed == 'invalid': witness['pid_namespace']['inode'] = True
                with patch.object(group, 'validate') as validate:
                    if changed == 'valid':
                        self.assertTrue(group.bind_original_namespace(witness)['shared_guard_witness_verified'])
                        validate.assert_called_once_with(frozen=False)
                        mounts = group.mount_args()
                        self.assertEqual(mounts.count('--mount'), 6)
                        self.assertEqual(sum(not value.endswith(',readonly') for value in mounts if value.startswith('type=bind')), 1)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'OriginalNamespaceWitnessRejected'):
                            group.bind_original_namespace(witness)
                        validate.assert_not_called()

    def test_killer_namespace_requires_private_local_pid_and_never_reads_root_namespace_magiclink(self):
        cid = self.docker.killer_id
        for mode, namespace, membership, accepted in (
                ('', {'device': 4, 'inode': 33}, '0::/system.slice/docker-' + cid + '.scope', True),
                ('host', {'device': 4, 'inode': 33}, '0::/x', False),
                ('container:' + self.docker.old_id, {'device': 4, 'inode': 33}, '0::/x', False),
                ('', {'device': 4, 'inode': 11}, '0::/x', False),
                ('', {'device': 4, 'inode': 22}, '0::/x', False),
                ('', {'device': 4, 'inode': 33}, '0::/foreign', False)):
            with self.subTest(mode=mode, namespace=namespace, membership=membership):
                group = cutover.HostCgroup()
                group.pid = 4321
                group.namespaces = deepcopy(self.docker.cgroup.identity['pid_namespaces'])
                current = {'Id': cid, 'State': {'Pid': 7777, 'Running': True, 'Paused': False}, 'HostConfig': {'PidMode': mode}}
                with patch.object(cutover, 'process_startticks', return_value=654321), \
                        patch.object(Path, 'read_text', return_value=membership), \
                        patch.object(Path, 'stat', side_effect=AssertionError('Host must not follow target namespace magiclink')) as stat:
                    if accepted:
                        self.assertTrue(group.validate_killer(current, {'writer_pid_namespace': namespace})['private_sibling_verified'])
                        with patch.object(cutover, 'process_startticks', return_value=654322):
                            with self.assertRaisesRegex(RuntimeError, 'KillerNamespaceIdentityChanged'):
                                group.validate_killer(current, {'writer_pid_namespace': namespace})
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'KillerPrivatePidNamespaceRequired|KillerLocalNamespaceIdentityRejected'):
                            group.validate_killer(current, {'writer_pid_namespace': namespace})
                    stat.assert_not_called()
        module = ast.parse(MODULE.read_text())
        calls = [node for node in ast.walk(module) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'pid_namespace']
        self.assertTrue(calls)
        self.assertTrue(all(isinstance(node.args[0], ast.Constant) and node.args[0].value == '/proc/self/ns/pid' for node in calls))

    def test_private_guest_rejects_old_or_host_pid_namespace_before_any_write(self):
        identity = deepcopy(self.docker.cgroup.identity)
        program = cutover.cgroup_companion_program(identity, kill=True)
        for inode in (11, 22):
            with self.subTest(inode=inode), patch.object(cutover.os, 'geteuid', create=True, return_value=0), \
                    patch.object(cutover.os, 'stat', return_value=SimpleNamespace(st_dev=4, st_ino=inode)), \
                    patch.object(cutover.os, 'open') as opened, patch.object(cutover.os, 'write') as written, \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'KillerPidNamespaceNotIsolated'):
                    exec(compile(program, 'isolated-killer-guest', 'exec'), {})
                opened.assert_not_called()
                written.assert_not_called()

    def test_private_guest_validates_bound_original_process_and_writes_only_once(self):
        identity = deepcopy(self.docker.cgroup.identity)
        process_stat = '4321 (synthetic) ' + ' '.join(['S'] + ['0'] * 18 + ['123456', '0'])
        texts = {cutover.PROCESS_BIND_ROOT + '/stat': process_stat,
            cutover.PROCESS_BIND_ROOT + '/cgroup': '0::' + identity['cgroup_path'],
            cutover.CGROUP_BIND_ROOT + '/cgroup.type': 'domain',
            cutover.CGROUP_BIND_ROOT + '/cgroup.freeze': '1',
            cutover.CGROUP_BIND_ROOT + '/cgroup.events': 'populated 1\nfrozen 1\n'}
        def read_text(path, *args, **kwargs):
            return texts[str(path).replace('\\', '/')]
        def lstat(path, *args, **kwargs):
            expected = identity['files'][path.name]
            return SimpleNamespace(st_dev=expected['device'], st_ino=expected['inode'], st_mode=0o100600)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cutover.os, 'geteuid', create=True, return_value=0), \
                patch.object(cutover.os, 'O_NOFOLLOW', create=True, new=0), \
                patch.object(cutover.os, 'stat', return_value=SimpleNamespace(st_dev=4, st_ino=33)), \
                patch.object(Path, 'read_text', autospec=True, side_effect=read_text), \
                patch.object(Path, 'lstat', autospec=True, side_effect=lstat), \
                patch.object(cutover.os, 'open', return_value=9911), \
                patch.object(cutover.os, 'fstat', return_value=SimpleNamespace(st_dev=1, st_ino=1)), \
                patch.object(cutover.os, 'close'), patch.object(cutover.os, 'write', return_value=1) as write, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            exec(compile(cutover.cgroup_companion_program(identity, kill=True), 'isolated-killer-valid', 'exec'), {})
        write.assert_called_once_with(9911, b'1')
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        response = json.loads(stdout.getvalue())
        self.assertTrue(response['kill_write_returned'])
        self.assertEqual(response['writer_pid_namespace'], {'device': 4, 'inode': 33})
        self.assertEqual(stderr.getvalue().splitlines(), ['CGROUP_KILL_STAGE=start', 'CGROUP_KILL_STAGE=prewrite_checks_passed', 'CGROUP_KILL_STAGE=write_returned'])

    def test_missing_original_namespace_attestation_never_grants_killer_identity(self):
        group = cutover.HostCgroup()
        group.namespaces = {'original': None, 'controller': {'device': 4, 'inode': 22}}
        with self.assertRaisesRegex(RuntimeError, 'OriginalNamespaceWitnessRequired'):
            group.validate_killer({}, {'writer_pid_namespace': {'device': 4, 'inode': 33}})

    def test_killer_effective_mount_reordering_passes_and_full_field_changes_reject(self):
        self.invoke()
        self.docker.killer_exists = True
        candidate = json.loads(self.docker(['docker', 'inspect', self.docker.killer_id]))[0]
        for mount in candidate['Mounts']:
            mount.update(Mode='', Propagation='rprivate', future_field={'preserve': True})
        baseline = deepcopy(candidate['Mounts'])
        candidate['Mounts'].reverse()
        arguments = (self.docker.killer_name, self.docker.killer_image, self.docker.old_id, 'killer', self.docker.cgroup.identity)
        self.assertTrue(cutover.owned_helper(candidate, *arguments, expected_mounts=baseline))
        for field, value in (('Source', '/foreign'), ('RW', True), ('Type', 'volume'), ('Propagation', 'rshared'),
                             ('Mode', 'shared'), ('future_field', {'preserve': False})):
            with self.subTest(field=field):
                changed = deepcopy(candidate)
                changed['Mounts'][0][field] = value
                self.assertFalse(cutover.owned_helper(changed, *arguments, expected_mounts=baseline))


if __name__ == "__main__":
    unittest.main()
