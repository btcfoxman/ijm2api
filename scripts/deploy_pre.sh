#!/usr/bin/env bash
# Deploy an already-built image, preserving every live compose layer and .env.
set -euo pipefail
umask 077
: "${DEPLOY_PATH:?}" "${DEPLOY_SERVICE:?}" "${IMAGE_REGISTRY:?}" "${IMAGE_NAMESPACE:?}" "${IMAGE_NAME:?}" "${IMAGE_TAG:?}" "${IJM2API_ORIGIN:?}"
[[ "$IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]] || { echo "An immutable Git SHA image tag is required"; exit 1; }
wait_seconds="${DEPLOY_WAIT_SECONDS:-7200}"
[[ "$wait_seconds" =~ ^[0-9]+$ ]] || exit 1
test -d "$DEPLOY_PATH"
release="$DEPLOY_PATH/.ijm-releases/$(date -u +%Y%m%dT%H%M%SZ)-${IMAGE_TAG:0:12}"
mkdir -p "$release"
image="$IMAGE_REGISTRY/$IMAGE_NAMESPACE/$IMAGE_NAME:$IMAGE_TAG"

python3 - "$DEPLOY_SERVICE" "$DEPLOY_PATH" "$release" "$image" <<'PY'
import hashlib,json,pathlib,shutil,subprocess,sys
service,directory,release,image=sys.argv[1:]
root=pathlib.Path(directory).resolve();out=pathlib.Path(release)
value=json.loads(subprocess.check_output(['docker','inspect',service],timeout=10))[0]
labels=value['Config'].get('Labels') or {}
if pathlib.Path(labels.get('com.docker.compose.project.working_dir','')).resolve()!=root:
 raise SystemExit('Live compose working directory differs from DEPLOY_PATH; refusing to guess')
project=labels.get('com.docker.compose.project')
if not project or labels.get('com.docker.compose.service')!=service:
 raise SystemExit('Live compose project/service labels are required')
configs=[str(pathlib.Path(path).resolve()) for path in labels.get('com.docker.compose.project.config_files','').split(',') if path]
if not configs:raise SystemExit('No live compose layers found')
env=root/'.env'
if not env.is_file():raise SystemExit('Existing .env is required; it will not be generated')
checksums={}
for number,path in enumerate([str(env),*configs]):
 source=pathlib.Path(path)
 raw=source.read_bytes();checksums[path]=hashlib.sha256(raw).hexdigest()
 backup=out/('config-'+str(number)+'.private.backup')
 shutil.copyfile(source,backup);backup.chmod(0o600)
context={'directory':str(root),'project':project,'service':service,'configs':configs,
 'checksums':checksums,'previous_image':value['Config']['Image'],'previous_image_id':value['Image'],
 'was_running':bool(value['State']['Running']),
 'data_mounts':[{'source':mount['Source'],'target':mount['Destination']} for mount in value.get('Mounts',[]) if mount['Destination']=='/app/data']}
if not context['data_mounts']:raise SystemExit('Existing persistent /app/data mount is required')
(out/'context.private.json').write_text(json.dumps(context,indent=2)+'\n')
(out/'image.compose.json').write_text(json.dumps({'services':{service:{'image':image}}})+'\n')
(out/'rollback.compose.json').write_text(json.dumps({'services':{service:{'image':context['previous_image']}}})+'\n')
PY

mapfile -d '' -t compose_base < <(python3 - "$release/context.private.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
args=['docker','compose','--project-directory',value['directory'],'--env-file',value['directory']+'/.env','-p',value['project']]
for path in value['configs']:args+=['-f',path]
for item in args:sys.stdout.write(item+'\0')
PY
)
compose=("${compose_base[@]}" -f "$release/image.compose.json")
rollback=("${compose_base[@]}" -f "$release/rollback.compose.json")

pulled=0
for attempt in 1 2 3; do
  if docker pull "$image" >"$release/pull.private.log" 2>&1; then pulled=1; break; fi
  sleep 5
done
test "$pulled" -eq 1 || { echo "Could not pull the published image; existing service was preserved"; exit 1; }

"${compose[@]}" config --format json >"$release/merged-compose.private.json" 2>"$release/config.private.log"
python3 - "$release" "$image" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);context=json.loads((out/'context.private.json').read_text())
service=json.loads((out/'merged-compose.private.json').read_text())['services'][context['service']]
if service['image']!=sys.argv[2]:raise SystemExit('Compose did not select the published SHA image')
mounts={item['target']:item.get('source') for item in service.get('volumes',[]) if isinstance(item,dict)}
if any(mounts.get(item['target'])!=item['source'] for item in context['data_mounts']):
 raise SystemExit('Persistent data mounts would change; refusing to restart')
PY

active_commands() {
  docker exec -i "$DEPLOY_SERVICE" python -B - <<'PY'
import os,sqlite3
database=sqlite3.connect('file:'+os.environ.get('IJM_DATA_DIR','/app/data')+'/ijm2api.db?mode=ro',uri=True,timeout=5)
tasks=database.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','preparing','submitting','submitted','running')").fetchone()[0]
logins=database.execute("SELECT count(*) FROM accounts WHERE status='logging_in'").fetchone()[0]
database.close()
print(tasks+logins)
PY
}

deadline=$((SECONDS + wait_seconds))
while true; do
  active="$(active_commands)"
  echo "IJM2API active tasks or logins: $active"
  if [ "$active" -eq 0 ]; then
    sleep 3
    if [ "$(active_commands)" -eq 0 ]; then break; fi
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "Idle wait expired; refusing to interrupt tasks or login"
    exit 1
  fi
  sleep 10
done

# SQLite's online backup includes WAL contents without stopping the service.
docker exec -i "$DEPLOY_SERVICE" python -B - "$(basename "$release")" <<'PY'
import os,pathlib,sqlite3,sys
root=pathlib.Path(os.environ.get('IJM_DATA_DIR','/app/data'))
target=root/'.deploy-backups'/sys.argv[1]/'ijm2api.db'
target.parent.mkdir(parents=True,exist_ok=False);target.parent.chmod(0o700)
source=sqlite3.connect('file:'+str(root/'ijm2api.db')+'?mode=ro',uri=True,timeout=5)
backup=sqlite3.connect(str(target));source.backup(backup);backup.close();source.close();target.chmod(0o600)
PY

check_preserved_config() {
  python3 - "$release/context.private.json" <<'PY'
import hashlib,json,pathlib,sys
context=json.load(open(sys.argv[1]))
if any(hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()!=digest for path,digest in context['checksums'].items()):
 raise SystemExit('Existing .env or compose configuration changed during deployment')
PY
}

healthy() {
  for attempt in $(seq 1 30); do
    if [ "$(docker inspect -f '{{.State.Health.Status}}' "$DEPLOY_SERVICE" 2>/dev/null || true)" = healthy ] \
        && curl --noproxy '*' -fsS --connect-timeout 2 --max-time 4 "$IJM2API_ORIGIN/health" >/dev/null; then
      return 0
    fi
    sleep 5
  done
  return 1
}

check_preserved_config
test "$(active_commands)" -eq 0 || { echo "New work arrived; service was not restarted. Retry deployment after it completes"; exit 1; }
if "${compose[@]}" up -d --no-deps --no-build --pull never "$DEPLOY_SERVICE" >"$release/restart.private.log" 2>&1 && healthy; then
  actual_image="$(docker inspect -f '{{.Config.Image}}' "$DEPLOY_SERVICE")"
  if [ "$actual_image" = "$image" ] && check_preserved_config; then
    echo "IJM2API published SHA image is healthy; existing configuration and data were preserved"
    exit 0
  fi
fi

echo "Candidate did not become healthy; restoring the previous image with unchanged configuration"
if [ "$(docker inspect -f '{{.State.Running}}' "$DEPLOY_SERVICE" 2>/dev/null || true)" = true ]; then
  rollback_active="$(active_commands)" || { echo "Cannot verify that rollback is idle; refusing another restart"; exit 1; }
  if [ "$rollback_active" -ne 0 ]; then
    echo "New tasks or login are active; refusing to interrupt them for rollback"
    exit 1
  fi
fi
if "${rollback[@]}" up -d --no-deps --no-build --pull never "$DEPLOY_SERVICE" >"$release/rollback.private.log" 2>&1 && healthy; then
  echo "Previous IJM2API image restored; candidate deployment failed"
else
  echo "Rollback requires operator inspection; private diagnostics are retained in the release directory"
fi
exit 1
