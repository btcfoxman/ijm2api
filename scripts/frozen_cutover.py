"""Freeze, verify, and remove only the original app; never build or submit."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import stat
import sys
import tempfile
import threading
import time
import uuid

COMPANION_OWNER_LABEL = "io.ijm2api.frozen-companion.owner"
COMPANION_ROLE_LABEL = "io.ijm2api.frozen-companion.role"
MIB = 1024 * 1024
SNAPSHOT_HEADROOM = 64 * MIB
SNAPSHOT_MINIMUM = 256 * MIB
SNAPSHOT_MAXIMUM = 1024 * MIB
CGROUP_BIND_ROOT = "/run/ijm-frozen-cgroup"
PROCESS_BIND_ROOT = "/run/ijm-original-process"


def pid_namespace(path):
    value = Path(path).stat()
    return {"device": value.st_dev, "inode": value.st_ino}


def original_namespace_program():
    return """# ORIGINAL_PID_NAMESPACE_WITNESS
import json,os,pathlib
namespace=os.stat('/proc/self/ns/pid')
value=pathlib.Path('/proc/1/stat').read_text();ticks=int(value[value.rfind(')')+2:].split()[19])
print(json.dumps({'pid_namespace':{'device':namespace.st_dev,'inode':namespace.st_ino},
 'startticks':ticks,'cgroup':pathlib.Path('/proc/1/cgroup').read_text().splitlines()}))
"""


def process_startticks(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return int(value[value.rfind(")") + 2:].split()[19])


def cgroup_events(value):
    pairs = dict(line.split() for line in value.splitlines())
    if any(pairs.get(key) not in {"0", "1"} for key in ("populated", "frozen")):
        raise RuntimeError("CgroupEventsInvalid")
    return {key: int(pairs[key]) for key in ("populated", "frozen")}


def diagnostic_error(error):
    name = type(error).__name__
    number = getattr(error, "errno", None)
    return {"error_class": name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "Exception",
        "errno": number if type(number) is int else None}


def kill_command_diagnostic(output=None, error=None):
    value = {"returned": error is None, "returncode": 0 if error is None else None,
        "timed_out": isinstance(error, subprocess.TimeoutExpired), "stages_observed": []}
    if error is None:
        try:
            response = json.loads(output)
            value["guest_write_returned"] = response.get("kill_write_returned") is True
        except (TypeError, ValueError, AttributeError):
            value["guest_write_returned"] = False
        return value
    value.update(diagnostic_error(error))
    code = getattr(error, "returncode", None)
    value["returncode"] = code if type(code) is int else None
    tail = getattr(error, "command_stderr", getattr(error, "stderr", None))
    if isinstance(tail, bytes):
        tail = tail[-4096:].decode("utf-8", errors="replace")
    elif isinstance(tail, str):
        tail = tail[-4096:]
    else:
        tail = ""
    value["stderr_captured_characters"] = len(tail)
    value["stages_observed"] = re.findall(r"(?m)^CGROUP_KILL_STAGE=(start|prewrite_checks_passed|write_returned)$", tail)
    value["stderr_error_classes"] = sorted(set(re.findall(r"(?m)^(RuntimeError|PermissionError|FileNotFoundError|ProcessLookupError|OSError):", tail)))
    value["stderr_errno"] = [int(number) for number in re.findall(r"(?m)^(?:PermissionError|FileNotFoundError|ProcessLookupError|OSError): \[Errno (-?[0-9]{1,4})\]", tail)]
    reasons = ("CompanionRootIdentityRequired", "NamespaceInitIdentityChanged", "NamespaceInitCgroupChanged",
        "CgroupBindIdentityChanged", "CgroupDomainRequired", "CgroupWriteDescriptorChanged",
        "CgroupNoLongerFrozen", "CgroupNotFullyFrozen", "CgroupKillShortWrite", "KillerPidNamespaceNotIsolated")
    value["guest_error_reasons"] = [reason for reason in reasons if "RuntimeError: " + reason in tail]
    return value


def old_container_diagnostic(current, old_id):
    if not isinstance(current, dict) or current.get("Id") != old_id:
        return {"exact_old_id_matched": False}
    state = current.get("State") or {}
    policy = (current.get("HostConfig") or {}).get("RestartPolicy") or {}
    return {"exact_old_id_matched": True,
        "state": {key: state.get(key) for key in ("Running", "Paused", "Restarting", "Dead", "Pid", "ExitCode", "OOMKilled")
            if type(state.get(key)) in (bool, int)},
        "restart_policy": {"Name": policy.get("Name") if policy.get("Name") in {"no", "always", "unless-stopped", "on-failure"} else "unknown",
            "MaximumRetryCount": policy.get("MaximumRetryCount") if type(policy.get("MaximumRetryCount")) is int else None},
        "restart_count": current.get("RestartCount") if type(current.get("RestartCount")) is int else None}


def restart_suppression_diagnostic(original, current, identity, cgroup):
    """Compare only the already captured snapshots; never authorize a cutover."""
    def fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    def changed_keys(before, after):
        return sorted(key for key in set(before) | set(after)
            if key not in before or key not in after or before[key] != after[key])
    stable_original = container_stable_facts(original)
    stable_current = container_stable_facts(current)
    value = {"predicates": {
        "stable_facts_equal": stable_current == stable_original,
        "restart_policy_is_no": current["HostConfig"].get("RestartPolicy") == {"Name": "no", "MaximumRetryCount": 0},
        "restart_count_equal": current.get("RestartCount") == original.get("RestartCount"),
        "pid_matches_captured_identity": current["State"].get("Pid") == identity["pid"],
        "running_truthy": bool(current["State"].get("Running")),
        "paused_falsy": not current["State"].get("Paused")},
        "stable_field_equalities": {key: stable_original[key] == stable_current[key] for key in stable_original},
        "original": old_container_diagnostic(original, identity["container_id"]),
        "current": old_container_diagnostic(current, identity["container_id"]),
        "captured_identity_matches": {
            "original_id": original.get("Id") == identity["container_id"],
            "current_id": current.get("Id") == identity["container_id"],
            "original_pid": original["State"].get("Pid") == identity["pid"],
            "current_pid": current["State"].get("Pid") == identity["pid"]}}
    for name in ("Config", "HostConfig"):
        before, after = original.get(name) or {}, current.get(name) or {}
        value[name] = {"original_sha256": fingerprint(before), "current_sha256": fingerprint(after),
            "changed_top_level_keys": changed_keys(before, after)}
    before, after = original.get("Mounts") or [], current.get("Mounts") or []
    original_order, current_order = fingerprint(before), fingerprint(after)
    original_sorted = fingerprint(sorted(before, key=lambda row: row.get("Destination", "")))
    current_sorted = fingerprint(sorted(after, key=lambda row: row.get("Destination", "")))
    value["Mounts"] = {"original_order_sensitive_sha256": original_order, "current_order_sensitive_sha256": current_order,
        "order_sensitive_hashes_equal": original_order == current_order,
        "original_sorted_destination_sha256": original_sorted, "current_sorted_destination_sha256": current_sorted,
        "sorted_destination_hashes_equal": original_sorted == current_sorted,
        "original_destination_order": [row.get("Destination") for row in before],
        "current_destination_order": [row.get("Destination") for row in after]}
    try:
        value["live_cgroup_identity_matches"] = True
        value["live_cgroup_events"] = cgroup.validate(frozen=False)
    except Exception as error:
        value["live_cgroup_identity_matches"] = False
        value["live_cgroup_error"] = diagnostic_error(error)
    return value


class HostCgroup:
    """Host metadata only; the single privileged write occurs in the companion."""

    def __init__(self):
        self.fds = {}
        self.stop_watch = threading.Event()
        self.watcher = None
        self.empty_observation = None
        self.last_events = None
        self.last_events_read_bytes = None
        self.last_events_error = None
        self.last_watch_error = None
        self.watch_error_count = 0
        self.killer_identity = None

    def capture(self, original):
        if sys.platform != "linux" or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise RuntimeError("CgroupV2HostRequired")
        old_id, pid = original["Id"], original["State"].get("Pid")
        if not re.fullmatch(r"[0-9a-f]{64}", old_id) or type(pid) is not int or pid <= 1:
            raise RuntimeError("OriginalCgroupIdentityInvalid")
        membership = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if len(membership) != 1 or not membership[0].startswith("0::/"):
            raise RuntimeError("OriginalUnifiedCgroupRequired")
        relative = membership[0][3:]
        path = Path("/sys/fs/cgroup") / relative.lstrip("/")
        if (not re.fullmatch(r"/[A-Za-z0-9_./:-]+", relative) or ".." in path.parts
                or path.name not in {old_id, f"docker-{old_id}.scope"}
                or path.resolve(strict=True) != path):
            raise RuntimeError("OriginalCgroupPathRejected")
        # Reject symlinks in every component, including ancestors.
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise RuntimeError("OriginalCgroupSymlinkRejected")
        self.pid, self.path, self.relative = pid, path, relative
        self.ticks = process_startticks(pid)
        if not self.ticks:
            raise RuntimeError("OriginalProcessIdentityMissing")
        self.namespaces = {"original": None, "controller": pid_namespace("/proc/self/ns/pid")}
        if original["HostConfig"].get("PidMode") != "":
            raise RuntimeError("OriginalPrivatePidNamespaceRequired")
        self.fds["directory"] = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.identities = {}
        for name in ("directory", "cgroup.kill", "cgroup.events", "cgroup.freeze", "cgroup.type"):
            item = path if name == "directory" else path / name
            value = item.lstat()
            if value.st_uid != 0 or stat.S_ISLNK(value.st_mode):
                raise RuntimeError("CgroupFileOwnershipInvalid")
            if name != "directory" and not stat.S_ISREG(value.st_mode):
                raise RuntimeError("CgroupFileTypeInvalid")
            self.identities[name] = {"device": value.st_dev, "inode": value.st_ino}
        self.fds["events"] = os.open(path / "cgroup.events", os.O_RDONLY | os.O_NOFOLLOW)
        if (path / "cgroup.type").read_text().strip() != "domain":
            raise RuntimeError("CgroupDomainRequired")
        self.identity = {"container_id": old_id, "pid": pid, "startticks": self.ticks,
            "cgroup_path": relative, "host_cgroup_path": str(path), "files": self.identities,
            "pid_namespaces": self.namespaces}
        self.validate(frozen=False)
        return self.identity

    def bind_original_namespace(self, witness):
        namespace = witness.get("pid_namespace") if isinstance(witness, dict) else None
        if (not isinstance(namespace, dict) or set(namespace) != {"device", "inode"}
                or any(type(value) is not int or value <= 0 for value in namespace.values())
                or namespace == self.namespaces["controller"] or witness.get("startticks") != self.ticks
                or witness.get("cgroup") != ["0::" + self.relative] or self.namespaces["original"] is not None):
            raise RuntimeError("OriginalNamespaceWitnessRejected")
        self.validate(frozen=False)
        self.namespaces["original"] = namespace
        return {"shared_guard_witness_verified": True, "startticks_match": True, "cgroup_match": True,
            "original_pid_namespace": namespace, "controller_pid_namespace": self.namespaces["controller"]}

    def mount_args(self):
        if self.namespaces["original"] is None:
            raise RuntimeError("OriginalNamespaceWitnessRequired")
        arguments = ["--cgroupns", "host"]
        for name in ("cgroup.kill", "cgroup.events", "cgroup.freeze", "cgroup.type"):
            value = f"type=bind,src={self.path / name},dst={CGROUP_BIND_ROOT}/{name}"
            if name != "cgroup.kill":
                value += ",readonly"
            arguments += ["--mount", value]
        for name in ("stat", "cgroup"):
            arguments += ["--mount", f"type=bind,src=/proc/{self.pid}/{name},dst={PROCESS_BIND_ROOT}/{name},readonly"]
        return arguments

    def validate_killer(self, current, access):
        if self.namespaces["original"] is None:
            raise RuntimeError("OriginalNamespaceWitnessRequired")
        pid, cid = (current.get("State") or {}).get("Pid"), current.get("Id")
        if (not re.fullmatch(r"[0-9a-f]{64}", cid or "") or type(pid) is not int or pid <= 1 or pid == self.pid
                or (current.get("HostConfig") or {}).get("PidMode") != ""
                or not current["State"].get("Running") or current["State"].get("Paused")):
            raise RuntimeError("KillerPrivatePidNamespaceRequired")
        ticks = process_startticks(pid)
        namespace = access.get("writer_pid_namespace")
        membership = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if (not ticks or not isinstance(namespace, dict) or set(namespace) != {"device", "inode"}
                or any(type(value) is not int or value <= 0 for value in namespace.values())
                or namespace in self.namespaces.values()
                or len(membership) != 1 or not membership[0].startswith("0::/")
                or Path(membership[0][3:]).name not in {cid, f"docker-{cid}.scope"}
                or process_startticks(pid) != ticks):
            raise RuntimeError("KillerLocalNamespaceIdentityRejected")
        proof = {"container_id": cid, "pid": pid, "startticks": ticks, "pid_namespace": namespace,
            "private_sibling_verified": True, "local_cgroup_matches_container": True}
        if self.killer_identity is not None and self.killer_identity != proof:
            raise RuntimeError("KillerNamespaceIdentityChanged")
        self.killer_identity = proof
        return proof

    def validate(self, frozen=None):
        if (process_startticks(self.pid) != self.ticks
                or Path(f"/proc/{self.pid}/cgroup").read_text().splitlines() != ["0::" + self.relative]
                or pid_namespace("/proc/self/ns/pid") != self.namespaces["controller"]):
            raise RuntimeError("OriginalProcessOrCgroupChanged")
        for name, identity in self.identities.items():
            path = self.path if name == "directory" else self.path / name
            value = path.lstat()
            if stat.S_ISLNK(value.st_mode) or (value.st_dev, value.st_ino) != (identity["device"], identity["inode"]):
                raise RuntimeError("CgroupInodeChanged")
        os.lseek(self.fds["events"], 0, os.SEEK_SET)
        events = cgroup_events(os.read(self.fds["events"], 4096).decode())
        if events["populated"] != 1 or (frozen is not None and events["frozen"] != int(frozen)):
            raise RuntimeError("OriginalCgroupFreezeStateInvalid")
        return events

    def _empty(self):
        directory = os.fstat(self.fds["directory"])
        identity = self.identities["directory"]
        if (directory.st_dev, directory.st_ino) != (identity["device"], identity["inode"]):
            raise RuntimeError("HeldCgroupDirectoryChanged")
        try:
            os.lseek(self.fds["events"], 0, os.SEEK_SET)
            raw = os.read(self.fds["events"], 4096)
            self.last_events_read_bytes = len(raw)
            events = cgroup_events(raw.decode())
            self.last_events = events
            if events["populated"] == 0:
                return {"empty_proof": "held_events_populated_zero", "populated": 0}
        except (OSError, RuntimeError, ValueError) as error:
            self.last_events_error = diagnostic_error(error)
        # Linux cgroup rmdir rejects populated groups. Path absence alone could
        # be a rename; require the held original directory to be unlinked too.
        link = os.readlink(f"/proc/self/fd/{self.fds['directory']}")
        try:
            current = self.path.lstat()
            path_replaced = (current.st_dev, current.st_ino) != (identity["device"], identity["inode"])
        except FileNotFoundError:
            path_replaced = True
        if link == str(self.path) + " (deleted)" and path_replaced:
            return {"empty_proof": "held_original_cgroup_deleted", "original_directory_unlinked": True}
        return None

    def arm(self):
        self.validate(frozen=True)
        def watch():
            while not self.stop_watch.is_set():
                try:
                    value = self._empty()
                    if value:
                        self.empty_observation = value
                        return
                except (OSError, RuntimeError, ValueError) as error:
                    self.last_watch_error = diagnostic_error(error)
                    self.watch_error_count += 1
                self.stop_watch.wait(0.01)
        self.watcher = threading.Thread(target=watch, name="frozen-cgroup-events", daemon=True)
        self.watcher.start()

    def wait_empty(self, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.empty_observation and process_startticks(self.pid) != self.ticks:
                return dict(self.empty_observation, original_pid_gone=True)
            time.sleep(0.02)
        raise RuntimeError("CgroupDeathNotProven")

    def diagnostic(self):
        """Best-effort bounded observations only; never used as death proof."""
        value = {"last_events": self.last_events, "last_events_read_bytes": self.last_events_read_bytes, "last_events_error": self.last_events_error,
            "last_watch_error": self.last_watch_error, "watch_error_count": self.watch_error_count,
            "empty_observation": self.empty_observation, "watcher_alive": bool(self.watcher and self.watcher.is_alive())}
        try:
            with Path(f"/proc/{self.pid}/stat").open("rb") as reader:
                raw = reader.read(4096).decode("utf-8", errors="replace")
            fields = raw[raw.rfind(")") + 2:].split()
            ticks = int(fields[19])
            value["original_pid"] = {"present": True, "state": fields[0] if re.fullmatch(r"[A-Za-z]", fields[0]) else "unknown",
                "startticks": ticks, "expected_startticks": self.ticks, "original_startticks_match": ticks == self.ticks}
        except (OSError, ValueError, IndexError) as error:
            value["original_pid"] = dict(diagnostic_error(error), expected_startticks=self.ticks)
            if isinstance(error, (FileNotFoundError, ProcessLookupError)):
                value["original_pid"]["present"] = False
        try:
            raw = os.pread(self.fds["events"], 4096, 0)
            value["current_held_events_read_bytes"] = len(raw)
            value["current_held_events"] = cgroup_events(raw.decode())
        except (OSError, RuntimeError, ValueError, AttributeError) as error:
            value["current_held_events"] = diagnostic_error(error)
        try:
            value["held_directory_fd_link"] = os.readlink(f"/proc/self/fd/{self.fds['directory']}")[:1024]
            current = os.fstat(self.fds["directory"])
            expected = self.identities["directory"]
            value["held_directory_identity_match"] = (current.st_dev, current.st_ino) == (expected["device"], expected["inode"])
        except (OSError, KeyError) as error:
            value["held_directory"] = diagnostic_error(error)
        return value

    def close(self):
        self.stop_watch.set()
        if self.watcher:
            self.watcher.join(timeout=1)
        for descriptor in self.fds.values():
            os.close(descriptor)
        self.fds.clear()


def cgroup_companion_program(identity, *, kill=False):
    # The writer is in a separate default-private sibling PID namespace. The
    # original shared namespace companion only performs read-only guards.
    program = "# CGROUP_SINGLE_FILE_" + ("KILL" if kill else "ACCESS") + "\nimport json,os,pathlib,stat\n"
    if kill:
        program += "import sys\ndef observe(stage):\n try:print('CGROUP_KILL_STAGE='+stage,file=sys.stderr,flush=True)\n except Exception:pass\nobserve('start')\n"
    program += "expected=" + repr(identity) + "\nroot=pathlib.Path(" + repr(CGROUP_BIND_ROOT) + ")\noriginal=pathlib.Path(" + repr(PROCESS_BIND_ROOT) + ")\n"
    program += """if os.geteuid()!=0:raise RuntimeError('CompanionRootIdentityRequired')
namespace=os.stat('/proc/self/ns/pid');writer_namespace={'device':namespace.st_dev,'inode':namespace.st_ino}
namespaces=expected['pid_namespaces']
if not isinstance(namespaces['original'],dict) or namespaces['original']==namespaces['controller'] or writer_namespace in namespaces.values():raise RuntimeError('KillerPidNamespaceNotIsolated')
value=(original/'stat').read_text();ticks=int(value[value.rfind(')')+2:].split()[19])
if ticks!=expected['startticks']:raise RuntimeError('NamespaceInitIdentityChanged')
if (original/'cgroup').read_text().splitlines()!=['0::'+expected['cgroup_path']]:raise RuntimeError('NamespaceInitCgroupChanged')
for name in ('cgroup.kill','cgroup.events','cgroup.freeze','cgroup.type'):
 value=(root/name).lstat();wanted=expected['files'][name]
 if stat.S_ISLNK(value.st_mode) or (value.st_dev,value.st_ino)!=(wanted['device'],wanted['inode']):raise RuntimeError('CgroupBindIdentityChanged')
if (root/'cgroup.type').read_text().strip()!='domain':raise RuntimeError('CgroupDomainRequired')
fd=os.open(root/'cgroup.kill',os.O_WRONLY|os.O_NOFOLLOW)
try:
 value=os.fstat(fd);wanted=expected['files']['cgroup.kill']
 if (value.st_dev,value.st_ino)!=(wanted['device'],wanted['inode']):raise RuntimeError('CgroupWriteDescriptorChanged')
"""
    if kill:
        program += " if (root/'cgroup.freeze').read_text().strip()!='1':raise RuntimeError('CgroupNoLongerFrozen')\n"
        program += " events=dict(line.split() for line in (root/'cgroup.events').read_text().splitlines())\n"
        program += " if events.get('frozen')!='1' or events.get('populated')!='1':raise RuntimeError('CgroupNotFullyFrozen')\n"
        program += " observe('prewrite_checks_passed')\n"
        program += " if os.write(fd,b'1')!=1:raise RuntimeError('CgroupKillShortWrite')\n"
        program += " observe('write_returned')\n"
    program += "finally:os.close(fd)\n"
    program += "print(json.dumps({'access_verified':True,'kill_write_returned':True,'writer_pid_namespace':writer_namespace}))\n" if kill else "print(json.dumps({'access_verified':True,'writer_pid_namespace':writer_namespace}))\n"
    return program


def owned_helper(candidate, name, image, old_id, role, identity, expected_mounts=None, diagnostic=None):
    config = candidate.get("Config") if isinstance(candidate.get("Config"), dict) else {}
    host = candidate.get("HostConfig") if isinstance(candidate.get("HostConfig"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    cid = candidate.get("Id")
    checks = {"valid_distinct_container_id": bool(isinstance(cid, str) and re.fullmatch(r"[0-9a-f]{64}", cid) and cid != old_id),
        "exact_name": candidate.get("Name") == "/" + name, "exact_image": config.get("Image") == image,
        "owner_label": labels.get(COMPANION_OWNER_LABEL) == old_id, "role_label": labels.get(COMPANION_ROLE_LABEL) == role,
        "python_entrypoint": config.get("Entrypoint") == ["python"], "readonly_rootfs": host.get("ReadonlyRootfs") is True,
        "offline_network": host.get("NetworkMode") == "none", "not_privileged": host.get("Privileged") is False,
        "drop_all_capabilities": host.get("CapDrop") == ["ALL"], "no_new_privileges": host.get("SecurityOpt") == ["no-new-privileges"]}
    try:
        mounts = canonical_mounts(candidate.get("Mounts"))
        checks["valid_mount_destinations"] = True
        checks["complete_mount_baseline"] = expected_mounts is None or mounts == canonical_mounts(expected_mounts)
    except RuntimeError:
        mounts = []
        checks.update(valid_mount_destinations=False, complete_mount_baseline=False)
    if role == "guard":
        checks.update(shared_original_pid_namespace=host.get("PidMode") == "container:" + old_id,
            exact_read_only_dac_capability=host.get("CapAdd") in (["DAC_READ_SEARCH"], ["CAP_DAC_READ_SEARCH"]),
            host_cgroup_namespace=host.get("CgroupnsMode") == "host", original_volumes_readonly=host.get("VolumesFrom") == [old_id + ":ro"],
            no_extra_binds=host.get("Binds") in (None, []) and host.get("Mounts") in (None, []),
            fixed_sleep_command=config.get("Cmd") == ["-B", "-c", "import time; time.sleep(600)"],
            persistent_mounts_readonly=all(not mount.get("RW") or (mount.get("Destination") == "/tmp" and mount.get("Type") == "tmpfs") for mount in mounts),
            no_cgroup_or_original_proc_mounts=not any(str(mount.get("Destination", "")).startswith((CGROUP_BIND_ROOT, PROCESS_BIND_ROOT)) for mount in mounts))
    elif role == "killer":
        expected = {CGROUP_BIND_ROOT + "/" + key: (identity["host_cgroup_path"] + "/" + key, key == "cgroup.kill")
            for key in ("cgroup.kill", "cgroup.events", "cgroup.freeze", "cgroup.type")}
        expected.update({PROCESS_BIND_ROOT + "/" + key: (f"/proc/{identity['pid']}/{key}", False) for key in ("stat", "cgroup")})
        checks.update(private_pid_namespace=host.get("PidMode") == "", host_cgroup_namespace=host.get("CgroupnsMode") == "host",
            no_added_capabilities=host.get("CapAdd") in (None, []), no_data_volumes=host.get("VolumesFrom") in (None, []),
            fixed_isolated_sleep_command=config.get("Cmd") == ["-I", "-S", "-B", "-c", "import time; time.sleep(600)"],
            exactly_six_bindings=len(mounts) == len(expected),
            private_bind_mode=all(mount.get("Mode", "") == "" and mount.get("Propagation", "rprivate") == "rprivate" for mount in mounts),
            exact_original_file_bindings={mount.get("Destination"): (mount.get("Source"), mount.get("RW")) for mount in mounts if mount.get("Type") == "bind"} == expected)
    else:
        checks["known_role"] = False
    if isinstance(diagnostic, dict):
        try:
            def safe_list(value, allowed):
                return {"count": len(value), "values": [entry if isinstance(entry, str) and entry in allowed else "other" for entry in value[:16]]} if isinstance(value, list) else {"count": None, "values": [], "list": False}
            diagnostic["checks"] = checks
            diagnostic["rejected_predicates"] = [key for key, passed in checks.items() if not passed]
            diagnostic["observed"] = {"cap_add": safe_list(host.get("CapAdd"), {"DAC_READ_SEARCH", "CAP_DAC_READ_SEARCH", "CAP_SYS_ADMIN", "SYS_ADMIN", "ALL"}),
                "cap_drop": safe_list(host.get("CapDrop"), {"ALL"}),
                "security_options": safe_list(host.get("SecurityOpt"), {"no-new-privileges", "no-new-privileges:true"}),
                "pid_mode": "private" if host.get("PidMode") == "" else "shared_original" if host.get("PidMode") == "container:" + old_id else "other",
                "cgroup_namespace": host.get("CgroupnsMode") if host.get("CgroupnsMode") in {"host", "private", ""} else "other",
                "entrypoint_length": len(config["Entrypoint"]) if isinstance(config.get("Entrypoint"), list) else None,
                "command_length": len(config["Cmd"]) if isinstance(config.get("Cmd"), list) else None,
                "mount_count": len(mounts), "destination_order": [mount["Destination"] for mount in mounts],
                "writable_destinations": [mount["Destination"] for mount in mounts if mount.get("RW")],
                "canonical_mounts_sha256": hashlib.sha256(json.dumps(mounts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        except Exception as error:
            diagnostic["observation_error"] = diagnostic_error(error)
    return all(checks.values())


def durable_json(path, value):
    data = json.dumps(value, sort_keys=True, indent=2).encode()
    with Path(path).open("xb") as output:
        if hasattr(os, "fchmod"):
            os.fchmod(output.fileno(), 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    # Directory fsync is required on Linux; tests on Windows have no such API.
    if sys.platform == "linux":
        descriptor = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if Path(path).read_bytes() != data:
        raise RuntimeError("DurableCutoverManifestReadbackMismatch")
    return hashlib.sha256(data).hexdigest()


def canonical_mounts(mounts):
    if not isinstance(mounts, list):
        raise RuntimeError("ContainerMountListInvalid")
    destinations = set()
    for mount in mounts:
        destination = mount.get("Destination") if isinstance(mount, dict) else None
        if (not isinstance(destination, str) or not destination.startswith("/") or "\x00" in destination
                or destination.startswith("//") or PurePosixPath(destination).as_posix() != destination
                or ".." in PurePosixPath(destination).parts):
            raise RuntimeError("ContainerMountDestinationInvalid")
        if destination in destinations:
            raise RuntimeError("ContainerMountDestinationDuplicate")
        destinations.add(destination)
    # Preserve every inspected mount field and only normalize map iteration
    # order; any Source/RW/Type/Propagation or other field drift still differs.
    return sorted(mounts, key=lambda mount: mount["Destination"])


def container_stable_facts(value):
    host = dict(value.get("HostConfig") or {})
    host.pop("RestartPolicy", None)
    return {"Id": value.get("Id"), "Name": value.get("Name"), "Image": value.get("Image"),
        "Config": value.get("Config"), "HostConfig": host, "Mounts": canonical_mounts(value.get("Mounts"))}


def restart_argument(policy):
    if (not isinstance(policy, dict) or set(policy) != {"Name", "MaximumRetryCount"}
            or policy["Name"] not in {"no", "always", "unless-stopped", "on-failure"}
            or type(policy["MaximumRetryCount"]) is not int or policy["MaximumRetryCount"] < 0
            or (policy["Name"] != "on-failure" and policy["MaximumRetryCount"] != 0)):
        raise RuntimeError("OriginalRestartPolicyInvalid")
    return policy["Name"] + (":" + str(policy["MaximumRetryCount"]) if policy["Name"] == "on-failure" and policy["MaximumRetryCount"] else "")


def snapshot_sizing_program():
    """Stat only, before pause: never open the original database with SQLite."""
    return """import json,os,pathlib
root=pathlib.Path(os.environ.get('IJM_DATA_DIR','/app/data'));sizes={}
for name in ('ijm2api.db','ijm2api.db-wal'):
 path=root/name
 if path.is_symlink():raise RuntimeError('SnapshotSizingSymlinkRejected')
 if not path.exists():
  if name=='ijm2api.db':raise RuntimeError('SnapshotSizingDatabaseMissing')
  sizes[name]=0;continue
 if not path.is_file():raise RuntimeError('SnapshotSizingNotRegularFile')
 sizes[name]=path.stat().st_size
print(json.dumps(sizes))
"""


def snapshot_budget(sizes):
    if (not isinstance(sizes, dict) or set(sizes) != {'ijm2api.db', 'ijm2api.db-wal'}
            or any(type(value) is not int or value < 0 for value in sizes.values())
            or sizes['ijm2api.db'] == 0):
        raise RuntimeError('SnapshotSizingInvalid')
    required = 2 * sum(sizes.values()) + SNAPSHOT_HEADROOM
    capacity = max(SNAPSHOT_MINIMUM, (required + SNAPSHOT_HEADROOM - 1) // SNAPSHOT_HEADROOM * SNAPSHOT_HEADROOM)
    if capacity > SNAPSHOT_MAXIMUM:
        raise RuntimeError('SnapshotCapacityLimitExceeded')
    return {'source_db_bytes': sizes['ijm2api.db'], 'source_wal_bytes': sizes['ijm2api.db-wal'],
            'tmpfs_bytes': capacity, 'memory_bytes': capacity + 128 * MIB}


def execute(args, *, input=None, timeout=30):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        if args[:3] == ["docker", "exec", "-i"] and input is not None:
            # Only the trusted read-only guard runs through this stdin path.
            # deploy_pre.sh redirects this exception to its private 0600 log.
            error = RuntimeError("DockerCutoverOperationFailed phase=guard_exec exit="
                + str(result.returncode) + " " + result.stderr[-4000:].strip())
            if input.startswith("# CGROUP_SINGLE_FILE_KILL\n"):
                error.returncode, error.command_stderr = result.returncode, result.stderr[-4096:]
            raise error
        raise RuntimeError("DockerCutoverOperationFailed")
    return result.stdout.strip()


def companion_program(query_ids, startup_sha, *, backup=False):
    here = Path(__file__).resolve().parent
    query = (here / "query_resume_guard.py").read_text(encoding="utf-8")
    idle = (here.parent / "app/deploy_guard.py").read_text(encoding="utf-8")
    program = "import hashlib,json,os,pathlib,sqlite3,tempfile\n"
    program += "query={\"__name__\":\"frozen_query_guard\"};idle={\"__name__\":\"frozen_idle_guard\"}\n"
    program += "exec(compile(" + repr(query) + ",'query_guard','exec'),query)\n"
    program += "exec(compile(" + repr(idle) + ",'idle_guard','exec'),idle)\n"
    program += "root=pathlib.Path(os.environ.get('IJM_DATA_DIR','/app/data'));expected=" + repr(query_ids) + "\n"
    program += """# Recheck actual frozen sizes/free space, not the pre-pause estimate.
snapshot_bytes=0
for name in ('ijm2api.db','ijm2api.db-wal'):
 path=root/name
 if path.is_symlink():raise RuntimeError('FrozenSourceSymlinkRejected')
 if not path.exists():
  if name=='ijm2api.db':raise RuntimeError('FrozenSourceDatabaseMissing')
  continue
 if not path.is_file():raise RuntimeError('FrozenSourceNotRegularFile')
 snapshot_bytes+=path.stat().st_size
space=os.statvfs('/tmp');available_bytes=space.f_bavail*space.f_frsize
"""
    program += "required_bytes=snapshot_bytes*" + ("2" if backup else "1") + "+" + str(SNAPSHOT_HEADROOM) + "\n"
    program += "if available_bytes<required_bytes:raise RuntimeError('FrozenSnapshotCapacityInsufficient')\n"
    # A WAL-mode source may have no sidecars when its last writer closed.
    # Never ask SQLite to initialize them on the original read-only mount.
    program += """with tempfile.TemporaryDirectory(prefix='ijm-frozen-clone-',dir='/tmp') as temporary:
 clone_root=pathlib.Path(temporary);source_manifest={}
 for name in ('ijm2api.db','ijm2api.db-wal'):
  original=root/name
  if original.is_symlink():raise RuntimeError('FrozenSourceSymlinkRejected')
  if not original.exists():
   if name=='ijm2api.db':raise RuntimeError('FrozenSourceDatabaseMissing')
   source_manifest[name]=None;continue
  if not original.is_file():raise RuntimeError('FrozenSourceNotRegularFile')
  checksum=hashlib.sha256()
  with original.open('rb') as reader,(clone_root/name).open('xb') as writer:
   before=os.fstat(reader.fileno())
   for chunk in iter(lambda:reader.read(1024*1024),b''):
    writer.write(chunk);checksum.update(chunk)
   after=os.fstat(reader.fileno())
  if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise RuntimeError('FrozenSourceChangedDuringRead')
  (clone_root/name).chmod(0o600)
  source_manifest[name]={'sha256':checksum.hexdigest(),'size_bytes':after.st_size}
 database=clone_root/'ijm2api.db'
"""
    program += " if expected:\n  value=query['check'](database,query['parse_expected'](expected),root,pathlib.Path('/app/app/service.py')," + repr(startup_sha) + ")\n"
    program += " else:\n  count=idle['active_work'](database);value={'passed':count==0,'mode':'idle','active_count':count,'binding_sha256':None}\n"
    program += " if value.get('passed') is not True:raise RuntimeError('FrozenGuardRejected')\n"
    program += " value['source_manifest']=source_manifest\n"
    program += " value['snapshot_space']={'source_bytes':snapshot_bytes,'required_free_bytes':required_bytes,'available_free_bytes':available_bytes}\n"
    if backup:
        program += " source=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=5);source.execute('PRAGMA query_only=ON')\n"
        program += " target=pathlib.Path('/tmp/frozen.ijm2api.db');dest=sqlite3.connect(str(target));source.backup(dest);dest.close();source.close();target.chmod(0o600)\n"
        program += " with target.open('rb') as reader:value['frozen_backup_sha256']=hashlib.file_digest(reader,'sha256').hexdigest()\n"
    program += "print(json.dumps(value))\n"
    return program


def export_frozen_backup(companion_id, destination, timeout=30):
    """Stream the existing tmpfs backup; Docker's archive API cannot copy it."""
    command = ["docker", "exec", companion_id, "python", "-B", "-c",
        "import pathlib,shutil,sys\nwith pathlib.Path('/tmp/frozen.ijm2api.db').open('rb') as source:"
        "shutil.copyfileobj(source,sys.stdout.buffer,1024*1024)"]
    with Path(destination).open("xb") as output, tempfile.TemporaryFile() as errors:
        os.fchmod(output.fileno(), 0o600)
        try:
            result = subprocess.run(command, stdout=output, stderr=errors, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("FrozenBackupExportTimedOut") from None
        if result.returncode:
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 2000))
            detail = errors.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError("FrozenBackupExportFailed:" + detail)


def freeze_remove(service, image, release, query_ids, startup_sha, run=execute,
                  exporter=export_frozen_backup, cgroup_ops=None):
    release = Path(release)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service) or not release.is_dir():
        raise RuntimeError("CutoverArgumentsInvalid")
    original = json.loads(run(["docker", "inspect", service], timeout=10))[0]
    old_id = original["Id"]
    if not original["State"]["Running"] or original["State"].get("Paused"):
        raise RuntimeError("OriginalAppNotRunningUnpaused")
    if original["HostConfig"].get("AutoRemove") is not False or original["State"].get("Restarting"):
        raise RuntimeError("OriginalAppLifecycleUnsupported")
    policy = original["HostConfig"]["RestartPolicy"]
    original_restart = restart_argument(policy)
    stable_facts = container_stable_facts(original)
    original_restart_count = original.get("RestartCount")
    if type(original_restart_count) is not int or original_restart_count < 0:
        raise RuntimeError("OriginalRestartCountMissing")
    if any((release / name).exists() for name in ("frozen-kill-attempt.safe.json", "frozen-prekill.safe.json")):
        raise RuntimeError("CutoverAttemptAlreadyRecorded")
    cgroup = cgroup_ops if cgroup_ops is not None else HostCgroup()
    name = "ijm-frozen-guard-" + uuid.uuid4().hex[:16]
    killer_name = "ijm-frozen-killer-" + uuid.uuid4().hex[:16]
    companion_id = None
    killer_id = None
    companion_start_attempted = False
    killer_start_attempted = False
    guard_mounts = None
    killer_mounts = None
    paused = False
    removed = False
    restart_update_attempted = False
    kill_attempted = False
    receipt = {"passed": False, "old_container_sha256": hashlib.sha256(old_id.encode()).hexdigest(),
        "server_builds": 0, "new_generations": 0, "database_writes": 0,
        "stop_primitive": "single_file_cgroup_kill", "kill_command_invocations": 0,
        "original_unpaused_after_abort": False, "original_removed_while_frozen": False}
    try:
        identity = cgroup.capture(original)
        receipt["cgroup_identity"] = identity
        # A client timeout can occur after the daemon applied this update.
        restart_update_attempted = True
        try:
            run(["docker", "update", "--restart=no", old_id], timeout=15)
        except Exception:
            receipt["restart_update_client_result_unknown"] = True
        current = json.loads(run(["docker", "inspect", old_id], timeout=10))[0]
        if (container_stable_facts(current) != stable_facts
                or current["HostConfig"].get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
                or current.get("RestartCount") != original_restart_count
                or current["State"].get("Pid") != identity["pid"]
                or not current["State"].get("Running") or current["State"].get("Paused")):
            try:
                receipt["restart_suppression_diagnostic"] = restart_suppression_diagnostic(original, current, identity, cgroup)
            except Exception as error:
                receipt["restart_suppression_diagnostic"] = diagnostic_error(error)
            raise RuntimeError("OriginalRestartSuppressionNotVerified")
        cgroup.validate(frozen=False)
        receipt["original_restart_policy"] = policy
        receipt["restart_policy_suppressed"] = True
        sizes = json.loads(run(['docker', 'exec', '-i', old_id, 'python', '-B', '-'],
            input=snapshot_sizing_program(), timeout=10))
        capacity = snapshot_budget(sizes)
        receipt['snapshot_capacity'] = capacity
        # The companion joins while the original is running; only its separate
        # cgroup stays runnable after pause. It never starts the application.
        companion_start_attempted = True
        companion_id = run(["docker", "run", "--detach", "--rm", "--name", name,
            "--label", COMPANION_OWNER_LABEL + "=" + old_id,
            "--label", COMPANION_ROLE_LABEL + "=guard",
            "--pull", "never", "--network", "none", "--read-only", "--user", "0:0",
            "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH", "--security-opt", "no-new-privileges", "--memory", str(capacity['memory_bytes']), "--cpus", "0.5", "--pids-limit", "64",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=" + str(capacity['tmpfs_bytes']) + ",mode=0700", "--volumes-from", old_id + ":ro",
            "--pid", "container:" + old_id, "--cgroupns", "host", "--entrypoint", "python", image,
            "-B", "-c", "import time; time.sleep(600)"], timeout=90)
        if not isinstance(companion_id, str) or not re.fullmatch(r"[0-9a-f]{64}", companion_id) or companion_id == old_id:
            raise RuntimeError("CompanionStartReceiptInvalid")
        current_guard = json.loads(run(["docker", "inspect", companion_id], timeout=10))[0]
        guard_diagnostic = {}
        guard_owned = owned_helper(current_guard, name, image, old_id, "guard", identity, diagnostic=guard_diagnostic)
        guard_diagnostic["checks"]["startup_cid_matches"] = current_guard.get("Id") == companion_id
        if current_guard.get("Id") != companion_id:
            guard_diagnostic["rejected_predicates"].append("startup_cid_matches")
        if current_guard.get("Id") != companion_id or not guard_owned:
            receipt["guard_spec_diagnostic"] = guard_diagnostic
            raise RuntimeError("GuardReadOnlySpecificationRejected")
        guard_mounts = canonical_mounts(current_guard["Mounts"])
        witness = json.loads(run(["docker", "exec", "-i", companion_id, "python", "-I", "-S", "-B", "-"],
            input=original_namespace_program(), timeout=10))
        receipt["original_namespace_witness"] = cgroup.bind_original_namespace(witness)
        killer_start_attempted = True
        killer_id = run(["docker", "run", "--detach", "--rm", "--name", killer_name,
            "--label", COMPANION_OWNER_LABEL + "=" + old_id,
            "--label", COMPANION_ROLE_LABEL + "=killer",
            "--pull", "never", "--network", "none", "--read-only", "--user", "0:0",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", str(128 * MIB),
            "--cpus", "0.5", "--pids-limit", "16", *cgroup.mount_args(), "--entrypoint", "python", image,
            "-I", "-S", "-B", "-c", "import time; time.sleep(600)"], timeout=90)
        if not isinstance(killer_id, str) or not re.fullmatch(r"[0-9a-f]{64}", killer_id) or killer_id in {old_id, companion_id}:
            raise RuntimeError("KillerStartReceiptInvalid")
        current_killer = json.loads(run(["docker", "inspect", killer_id], timeout=10))[0]
        killer_diagnostic = {}
        killer_owned = owned_helper(current_killer, killer_name, image, old_id, "killer", identity, diagnostic=killer_diagnostic)
        killer_diagnostic["checks"]["startup_cid_matches"] = current_killer.get("Id") == killer_id
        if current_killer.get("Id") != killer_id:
            killer_diagnostic["rejected_predicates"].append("startup_cid_matches")
        if current_killer.get("Id") != killer_id or not killer_owned:
            receipt["killer_spec_diagnostic"] = killer_diagnostic
            raise RuntimeError("KillerPrivateSpecificationRejected")
        killer_mounts = canonical_mounts(current_killer["Mounts"])
        access = json.loads(run(["docker", "exec", "-i", killer_id, "python", "-I", "-S", "-B", "-"],
            input=cgroup_companion_program(identity), timeout=10))
        if access.get("access_verified") is not True:
            raise RuntimeError("SingleFileCgroupAccessNotVerified")
        receipt["killer_identity"] = cgroup.validate_killer(current_killer, access)
        current = json.loads(run(["docker", "inspect", service], timeout=10))[0]
        if current["Id"] != old_id or not current["State"]["Running"] or current["State"].get("Paused"):
            raise RuntimeError("OriginalAppChangedBeforeFreeze")
        # A client timeout does not prove the daemon failed to pause it.
        paused = True
        run(["docker", "pause", old_id], timeout=15)
        cgroup.validate(frozen=True)
        first = json.loads(run(["docker", "exec", "-i", companion_id, "python", "-B", "-"],
            input=companion_program(query_ids, startup_sha, backup=True), timeout=30))
        if not isinstance(first.get("source_manifest"), dict):
            raise RuntimeError("FrozenSourceManifestMissing")
        baseline_path = release / "query-resume-baseline.safe.json"
        baseline = json.loads(baseline_path.read_text()) if baseline_path.is_file() else None
        if (query_ids and first.get("mode") != "idle"
                and (not baseline or first.get("binding_sha256") != baseline.get("binding_sha256"))):
            raise RuntimeError("FrozenOriginalBindingChanged")
        destination = release / "frozen.ijm2api.db"
        if destination.exists():
            raise RuntimeError("FrozenBackupAlreadyExists")
        exporter(companion_id, destination)
        destination.chmod(0o600)
        with destination.open('rb+') as reader:
            backup_sha256 = hashlib.file_digest(reader, 'sha256').hexdigest()
            os.fsync(reader.fileno())
        if backup_sha256 != first.get("frozen_backup_sha256"):
            raise RuntimeError("FrozenBackupChecksumMismatch")
        final = json.loads(run(["docker", "exec", "-i", companion_id, "python", "-B", "-"],
            input=companion_program(query_ids, startup_sha), timeout=20))
        if (final.get("passed") is not True or final.get("mode") != first.get("mode")
                or final.get("binding_sha256") != first.get("binding_sha256")
                or final.get("active_count") != first.get("active_count")):
            raise RuntimeError("FrozenStateChangedDuringBackup")
        if final.get("source_manifest") != first["source_manifest"]:
            raise RuntimeError("FrozenSourceChangedDuringBackup")
        current = json.loads(run(["docker", "inspect", service], timeout=10))[0]
        if current["Id"] != old_id or not current["State"]["Running"] or not current["State"].get("Paused"):
            raise RuntimeError("OriginalAppDidNotRemainFrozen")
        if (container_stable_facts(current) != stable_facts
                or current["HostConfig"].get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
                or current.get("RestartCount") != original_restart_count):
            raise RuntimeError("OriginalConfigOrRestartPolicyChanged")
        cgroup.validate(frozen=True)
        current_killer = json.loads(run(["docker", "inspect", killer_id], timeout=10))[0]
        killer_diagnostic = {}
        killer_owned = owned_helper(current_killer, killer_name, image, old_id, "killer", identity, killer_mounts, killer_diagnostic)
        killer_diagnostic["checks"]["startup_cid_matches"] = current_killer.get("Id") == killer_id
        if current_killer.get("Id") != killer_id:
            killer_diagnostic["rejected_predicates"].append("startup_cid_matches")
        if current_killer.get("Id") != killer_id or not killer_owned:
            receipt["prekill_killer_spec_diagnostic"] = killer_diagnostic
            raise RuntimeError("KillerPrivateSpecificationChanged")
        killer_proof = cgroup.validate_killer(current_killer, access)
        receipt.update(frozen_guard=final, frozen_backup_sha256=first["frozen_backup_sha256"])
        # PID1 death kills only the shared read-only guard. The isolated killer
        # survives, but all frozen evidence is still durable before its write.
        manifest_sha = durable_json(release / "frozen-prekill.safe.json", {
            "cgroup_identity": identity, "original_restart_policy": policy,
            "restart_count": original_restart_count, "frozen_guard": final,
            "killer_identity": killer_proof,
            "frozen_backup_sha256": backup_sha256, "frozen_backup_bytes": destination.stat().st_size,
            "snapshot_capacity": capacity})
        receipt["durable_prekill_manifest_sha256"] = manifest_sha
        cgroup.arm()
        attempt_path = release / "frozen-kill-attempt.safe.json"
        try:
            durable_json(attempt_path, {
                "kill_attempted": True, "container_id": old_id,
                "prekill_manifest_sha256": manifest_sha, "automatic_kill_replay_allowed": False})
        except BaseException:
            # A partial marker, failed directory fsync or failed readback must
            # also block automatic recovery. No kill exec has been invoked.
            try:
                attempt_path.lstat()
                kill_attempted = True
            except FileNotFoundError:
                pass
            except OSError:
                kill_attempted = True
            receipt["kill_journal_commit_uncertain"] = kill_attempted
            receipt["kill_attempted"] = kill_attempted
            raise
        # Set immediately after the durable boundary, before any exec. Neither
        # exit 137 nor missing stdout proves that the write did not happen.
        kill_attempted = True
        receipt["kill_attempted"] = True
        receipt["kill_command_invocations"] = 1
        try:
            output = run(["docker", "exec", "-i", killer_id, "python", "-I", "-S", "-B", "-"],
                input=cgroup_companion_program(identity, kill=True), timeout=20)
            try:
                receipt["kill_command_diagnostic"] = kill_command_diagnostic(output=output)
            except Exception as error:
                receipt["kill_command_diagnostic"] = diagnostic_error(error)
        except Exception as error:
            receipt["kill_command_acknowledgement_missing"] = True
            try:
                receipt["kill_command_diagnostic"] = kill_command_diagnostic(error=error)
            except Exception as observation_error:
                receipt["kill_command_diagnostic"] = diagnostic_error(observation_error)
        try:
            death = cgroup.wait_empty(timeout=20)
            if death.get("original_pid_gone") is not True or death.get("empty_proof") not in {
                    "held_events_populated_zero", "held_original_cgroup_deleted"}:
                raise RuntimeError("CgroupDeathNotProven")
        except Exception:
            # Observation failures never replace the original failed death
            # proof, authorize recovery, or cause another cgroup write.
            try:
                observer = getattr(cgroup, "diagnostic", None)
                receipt["cgroup_wait_diagnostic"] = observer() if callable(observer) else {"available": False}
            except Exception as error:
                receipt["cgroup_wait_diagnostic"] = diagnostic_error(error)
            try:
                observed = json.loads(run(["docker", "inspect", old_id], timeout=5))[0]
                receipt["old_container_wait_diagnostic"] = old_container_diagnostic(observed, old_id)
            except Exception as error:
                receipt["old_container_wait_diagnostic"] = diagnostic_error(error)
            raise
        current = json.loads(run(["docker", "inspect", old_id], timeout=10))[0]
        try:
            receipt["old_container_death_diagnostic"] = old_container_diagnostic(current, old_id)
        except Exception as error:
            receipt["old_container_death_diagnostic"] = diagnostic_error(error)
        if (container_stable_facts(current) != stable_facts
                or current["State"].get("Running") is not False or current["State"].get("Restarting")
                or current["State"].get("Pid") != 0 or current.get("RestartCount") != original_restart_count
                or current["HostConfig"].get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}):
            raise RuntimeError("OriginalContainerDeathNotVerified")
        receipt["cgroup_death"] = death
        durable_json(release / "frozen-death.safe.json", {
            "cgroup_death": death, "container_id": old_id, "original_process_stopped": True,
            "restart_count": original_restart_count, "restart_policy": "no", "kill_attempts": 1})
        # This is now only metadata cleanup of a proven stopped container.
        # No force removal, Docker kill, unpause-to-kill, or fallback is allowed.
        try:
            run(["docker", "rm", old_id], timeout=30)
            removed = True
            receipt["original_removed_while_frozen"] = True
        except Exception:
            # A timeout is not evidence that removal failed. Query the daemon
            # before deciding whether to recreate or preserve the old service.
            rows = [json.loads(line) for line in run(["docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"], timeout=10).splitlines() if line]
            old = [row for row in rows if row.get("ID") == old_id]
            named = [row for row in rows if row.get("Names") == service]
            if not old and not named:
                removed = True
                receipt.update(original_removed_while_frozen=True, removal_timeout_resolved="original_absent")
            elif len(old) == len(named) == 1 and old[0].get("ID") == named[0].get("ID") and old[0].get("State") in {"exited", "dead", "created"}:
                removed = True  # No old app process exists; compose will replace the stopped container.
                receipt.update(original_stopped_while_frozen=True, removal_timeout_resolved="original_stopped")
            else:
                raise RuntimeError("RemovalOutcomeNotSafeForRecreate") from None
        receipt.update(passed=True)
        return receipt
    finally:
        if restart_update_attempted and not kill_attempted:
            try:
                current = json.loads(run(["docker", "inspect", old_id], timeout=10))[0]
                if (container_stable_facts(current) != stable_facts or not current["State"].get("Running")
                        or current.get("RestartCount") != original_restart_count):
                    raise RuntimeError("OriginalAbortIdentityChanged")
                current_policy = current["HostConfig"].get("RestartPolicy")
                if current_policy != {"Name": "no", "MaximumRetryCount": 0} and current_policy != policy:
                    raise RuntimeError("OriginalAbortRestartPolicyChangedExternally")
                cgroup.validate(frozen=bool(current["State"].get("Paused")))
                try:
                    run(["docker", "update", "--restart=" + original_restart, old_id], timeout=15)
                except Exception:
                    receipt["restart_restore_client_result_unknown"] = True
                restored = json.loads(run(["docker", "inspect", old_id], timeout=10))[0]
                if (container_stable_facts(restored) != stable_facts
                        or restored["HostConfig"].get("RestartPolicy") != policy
                        or restored.get("RestartCount") != original_restart_count
                        or restored["State"].get("Pid") != identity["pid"]
                        or restored["State"].get("Running") is not True
                        or bool(restored["State"].get("Paused")) != bool(current["State"].get("Paused"))):
                    raise RuntimeError("OriginalRestartRestorationNotVerified")
                receipt["original_restart_policy_restored_after_abort"] = True
                if paused and restored["State"]["Running"] and restored["State"].get("Paused"):
                    cgroup.validate(frozen=True)
                    run(["docker", "unpause", old_id], timeout=15)
                    receipt["original_unpaused_after_abort"] = True
            except Exception:
                receipt["prekill_abort_recovery_requires_operator_review"] = True
        elif kill_attempted and not removed:
            receipt["postkill_outcome_requires_operator_review"] = True
        for role, attempted, helper_name, known_id, mounts in (("killer", killer_start_attempted, killer_name, killer_id, killer_mounts), ("guard", companion_start_attempted, name, companion_id, guard_mounts)):
            if not attempted:
                continue
            try:
                # A lost startup receipt never permits another creation. Only
                # this role's exact owned random name/spec may be cleaned.
                candidates = json.loads(run(["docker", "inspect", helper_name], timeout=10))
                candidate = candidates[0] if isinstance(candidates, list) and len(candidates) == 1 else {}
                known = isinstance(known_id, str) and re.fullmatch(r"[0-9a-f]{64}", known_id) and known_id != old_id
                if (not known or candidate.get("Id") == known_id) and owned_helper(candidate, helper_name, image, old_id, role, identity, mounts):
                    run(["docker", "rm", "--force", candidate["Id"]], timeout=15)
                    receipt["owned_" + role + "_cleanup_completed"] = True
                    if role == "guard":
                        receipt["owned_companion_cleanup_completed"] = True
                else:
                    receipt[role + "_cleanup_requires_operator_review"] = True
            except Exception:
                # A guard may already have died with old PID1; uncertainty
                # never changes the business result or permits a blind rm.
                receipt[role + "_cleanup_requires_operator_review"] = True
            if role == "guard" and receipt.get("guard_cleanup_requires_operator_review"):
                receipt["companion_cleanup_requires_operator_review"] = True
        try:
            cgroup.close()
        except Exception:
            receipt["cgroup_observer_close_failed"] = True
        try:
            (release / "frozen-cutover.safe.json").write_text(json.dumps(receipt, indent=2))
        except OSError:
            # Audit IO must never mask the successful removal return and keep
            # deploy_pre.sh from immediately starting the already-built image.
            print("Frozen cutover receipt could not be written; continue service recovery", file=__import__('sys').stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--release", type=Path, required=True)
    args = parser.parse_args()
    value = freeze_remove(args.service, args.image, args.release, os.environ.get("IJM_DEPLOY_QUERY_ONLY_TASK_IDS", ""),
        os.environ.get("IJM_DEPLOY_STARTUP_SHA256", ""))
    print(json.dumps(value))


if __name__ == "__main__":
    main()
