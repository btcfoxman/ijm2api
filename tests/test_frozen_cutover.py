"""Hermetic deployment-controller tests; no Docker, application or CLI starts."""
from __future__ import annotations

import hashlib
import importlib.util
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts/frozen_cutover.py"
SPEC = importlib.util.spec_from_file_location("isolated_frozen_cutover", MODULE)
cutover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutover)

QUERY_IDS = "ijm_c1b4967e7b674cd2,ijm_4705618586294e2e,ijm_5f4677122fea4a08,ijm_5a0b6e0a43904cc8"
BINDING = "a" * 64


class FakeDocker:
    def __init__(self):
        self.old_id = "original-container-id"
        self.companion_id = "c" * 64
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
            if args[-1] == self.companion_name:
                if not self.companion_exists or self.cleanup_inspect_failure:
                    raise RuntimeError("CompanionInspectionUnavailable")
                value = {"Id": self.companion_id, "Name": "/" + self.companion_name,
                    "Config": {"Image": self.companion_image,
                        "Labels": {cutover.COMPANION_OWNER_LABEL: self.old_id}},
                    "HostConfig": {"PidMode": "container:" + self.old_id,
                        "ReadonlyRootfs": True, "NetworkMode": "none"}}
                field = self.foreign_companion_field
                if field == "name": value["Name"] = "/foreign-container"
                elif field == "owner": value["Config"]["Labels"][cutover.COMPANION_OWNER_LABEL] = "foreign-owner"
                elif field == "image": value["Config"]["Image"] = "foreign-image"
                elif field == "pid": value["HostConfig"]["PidMode"] = "host"
                elif field == "readonly": value["HostConfig"]["ReadonlyRootfs"] = False
                elif field == "network": value["HostConfig"]["NetworkMode"] = "host"
                elif field == "original-id": value["Id"] = self.old_id
                return json.dumps([value])
            self.inspect_count += 1
            value = {"Id": self.old_id, "State": {"Running": not self.old_removed, "Paused": self.paused}}
            if self.changed_before_pause and self.inspect_count == 2:
                value["Id"] = "concurrent-replacement-id"
            return json.dumps([value])
        if args[:2] == ["docker", "run"]:
            assert timeout == 90
            assert args.count("--label") == 1
            assert args[args.index("--label") + 1] == cutover.COMPANION_OWNER_LABEL + "=" + self.old_id
            self.companion_name = args[args.index("--name") + 1]
            self.companion_image = args[args.index("--entrypoint") + 2]
            for flag in ["--rm", "--read-only", "--cap-drop", "--pids-limit", "--tmpfs"]:
                assert flag in args
            # Read an existing private host-owned data directory without
            # granting file writes, tracing, host namespaces or admin powers.
            assert args.count("--cap-drop") == 1
            assert args[args.index("--cap-drop") + 1] == "ALL"
            assert args.count("--cap-add") == 1
            assert args[args.index("--cap-add") + 1] == "DAC_READ_SEARCH"
            assert "--privileged" not in args
            assert not any(cap in args for cap in ["DAC_OVERRIDE", "SYS_ADMIN", "SYS_PTRACE"])
            assert args[args.index("--security-opt") + 1] == "no-new-privileges"
            assert args[args.index("--network") + 1] == "none"
            assert args[args.index("--pull") + 1] == "never"
            assert args[args.index("--volumes-from") + 1] == self.old_id + ":ro"
            assert args[args.index("--pid") + 1] == "container:" + self.old_id
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
            assert args[3] == self.companion_id and self.paused
            assert isinstance(input, str) and "FrozenGuardRejected" in input
            self.guard_count += 1
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
            if args[-1] == self.old_id:
                assert self.paused and self.guard_count == 2
                self.old_removed = True
                if self.removal_timeout_after_effect:
                    raise TimeoutError("remove response lost after removing original")
            else:
                assert args[-1] == self.companion_id
                self.companion_exists = False
            return ""
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
        return cutover.freeze_remove("ijm2api", "ghcr.io/test/ijm2api:immutable", self.release,
                                     QUERY_IDS, "b" * 64, run=self.docker, exporter=self.docker.export_backup)

    def receipt(self):
        return json.loads((self.release / "frozen-cutover.safe.json").read_text())

    def assert_aborted_safely(self):
        self.assertFalse(self.docker.old_removed)
        self.assertFalse(self.docker.paused)
        self.assertTrue(self.receipt()["original_unpaused_after_abort"])
        self.assertFalse(self.receipt()["passed"])
        self.assertIn(["docker", "rm", "--force", self.docker.companion_id], self.docker.calls)
        self.assertNotIn(["docker", "rm", "--force", self.docker.old_id], self.docker.calls)
        unpause = self.docker.calls.index(["docker", "unpause", self.docker.old_id])
        self.assertEqual(self.docker.calls[unpause - 1], ["docker", "inspect", self.docker.old_id])

    def test_success_checks_twice_and_removes_original_without_unpausing(self):
        receipt = self.invoke()
        self.assertTrue(receipt["passed"] and receipt["original_removed_while_frozen"])
        self.assertFalse(receipt["original_unpaused_after_abort"])
        actions = [args[1] if args[0] == 'docker' else args[0] for args in self.docker.calls]
        self.assertLess(actions.index("pause"), actions.index("exec"))
        self.assertLess(actions.index("exec"), actions.index("binary_export"))
        self.assertLess(actions.index("binary_export"), len(actions) - 3)
        self.assertFalse(any(args[:2] == ["docker", "cp"] for args in self.docker.calls))
        self.assertEqual(self.docker.guard_count, 2)
        self.assertTrue(self.docker.old_removed)
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
        self.assertFalse(any(args[0] == "binary_export" or args[1] in ["pause", "unpause", "cp"] for args in self.docker.calls))
        self.assertIn(["docker", "rm", "--force", self.docker.companion_id], self.docker.calls)

    def test_removal_timeout_is_reconciled_as_gone_without_unpausing(self):
        self.docker.removal_timeout_after_effect = True
        receipt = self.invoke()
        self.assertTrue(receipt["passed"] and self.docker.old_removed)
        self.assertEqual(receipt["removal_timeout_resolved"], "original_absent")
        self.assertFalse(receipt["original_unpaused_after_abort"])
        self.assertFalse(any(args[1] == "unpause" for args in self.docker.calls))
        self.assertTrue(any(args[1] == "ps" for args in self.docker.calls))

    def test_receipt_write_failure_cannot_replace_successful_cutover_return(self):
        messages = io.StringIO()
        with patch.object(Path, "write_text", side_effect=OSError("private receipt disk failure")), redirect_stderr(messages):
            receipt = self.invoke()
        self.assertTrue(receipt["passed"] and self.docker.old_removed)
        self.assertNotIn("unpause", [args[1] for args in self.docker.calls])
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


if __name__ == "__main__":
    unittest.main()
