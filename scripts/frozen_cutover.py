"""Freeze, verify, and remove only the original app; never build or submit."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

COMPANION_OWNER_LABEL = "io.ijm2api.frozen-companion.owner"


def execute(args, *, input=None, timeout=30):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        if args[:3] == ["docker", "exec", "-i"] and input is not None:
            # Only the trusted read-only guard runs through this stdin path.
            # deploy_pre.sh redirects this exception to its private 0600 log.
            raise RuntimeError("DockerCutoverOperationFailed phase=guard_exec exit="
                + str(result.returncode) + " " + result.stderr[-4000:].strip())
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
                  exporter=export_frozen_backup):
    release = Path(release)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service) or not release.is_dir():
        raise RuntimeError("CutoverArgumentsInvalid")
    original = json.loads(run(["docker", "inspect", service], timeout=10))[0]
    old_id = original["Id"]
    if not original["State"]["Running"] or original["State"].get("Paused"):
        raise RuntimeError("OriginalAppNotRunningUnpaused")
    name = "ijm-frozen-guard-" + uuid.uuid4().hex[:16]
    companion_id = None
    paused = False
    removed = False
    receipt = {"passed": False, "old_container_sha256": hashlib.sha256(old_id.encode()).hexdigest(),
        "server_builds": 0, "new_generations": 0, "database_writes": 0,
        "original_unpaused_after_abort": False, "original_removed_while_frozen": False}
    try:
        # The companion joins while the original is running; only its separate
        # cgroup stays runnable after pause. It never starts the application.
        companion_id = run(["docker", "run", "--detach", "--rm", "--name", name,
            "--label", COMPANION_OWNER_LABEL + "=" + old_id,
            "--pull", "never", "--network", "none", "--read-only", "--user", "0:0",
            "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH", "--security-opt", "no-new-privileges", "--memory", "256m", "--cpus", "0.5", "--pids-limit", "64",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m,mode=0700", "--volumes-from", old_id + ":ro",
            "--pid", "container:" + old_id, "--entrypoint", "python", image,
            "-B", "-c", "import time; time.sleep(600)"], timeout=90)
        if not isinstance(companion_id, str) or not re.fullmatch(r"[0-9a-f]{64}", companion_id) or companion_id == old_id:
            raise RuntimeError("CompanionStartReceiptInvalid")
        current = json.loads(run(["docker", "inspect", service], timeout=10))[0]
        if current["Id"] != old_id or not current["State"]["Running"] or current["State"].get("Paused"):
            raise RuntimeError("OriginalAppChangedBeforeFreeze")
        # A client timeout does not prove the daemon failed to pause it.
        paused = True
        run(["docker", "pause", old_id], timeout=15)
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
        if hashlib.sha256(destination.read_bytes()).hexdigest() != first.get("frozen_backup_sha256"):
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
        receipt.update(frozen_guard=final, frozen_backup_sha256=first["frozen_backup_sha256"])
        # Never unpause an approved old listener: a newly queued request must
        # not cross the final check. Hosted Docker smoke verifies this path.
        try:
            run(["docker", "rm", "--force", old_id], timeout=30)
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
        if paused and not removed:
            try:
                current = json.loads(run(["docker", "inspect", old_id], timeout=10))[0]
                if current["Id"] == old_id and current["State"]["Running"] and current["State"].get("Paused"):
                    run(["docker", "unpause", old_id], timeout=15)
                    receipt["original_unpaused_after_abort"] = True
            except Exception:
                receipt["unpause_requires_operator_review"] = True
        try:
            # A startup timeout can lose stdout after the daemon created it.
            # Resolve only this operation's random name; never replay creation
            # or remove an unchecked name, the original app, or a foreign ID.
            candidates = json.loads(run(["docker", "inspect", name], timeout=10))
            candidate = candidates[0] if isinstance(candidates, list) and len(candidates) == 1 else {}
            config, host = candidate.get("Config") or {}, candidate.get("HostConfig") or {}
            cleanup_id = candidate.get("Id")
            owned = (isinstance(cleanup_id, str) and re.fullmatch(r"[0-9a-f]{64}", cleanup_id)
                and cleanup_id != old_id and candidate.get("Name") == "/" + name
                and (config.get("Labels") or {}).get(COMPANION_OWNER_LABEL) == old_id
                and config.get("Image") == image and host.get("PidMode") == "container:" + old_id
                and host.get("ReadonlyRootfs") is True and host.get("NetworkMode") == "none")
            if owned:
                run(["docker", "rm", "--force", cleanup_id], timeout=15)
                receipt["owned_companion_cleanup_completed"] = True
            else:
                receipt["companion_cleanup_requires_operator_review"] = True
        except Exception:
            # The --rm companion can already be gone. Failed observation or
            # cleanup must preserve the original outcome, never guess/remove.
            receipt["companion_cleanup_requires_operator_review"] = True
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
