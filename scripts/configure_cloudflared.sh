#!/usr/bin/env bash
set -euo pipefail

: "${DEPLOY_PATH:?}"
: "${IJM2API_HOSTNAME:?}"
: "${IJM2API_ORIGIN:?}"

umask 077
config_dir="$DEPLOY_PATH/.cloudflared"
mkdir -p "$config_dir" "$HOME/.config/systemd/user"
id_file="$config_dir/tunnel-id"

if [ ! -s "$id_file" ]; then
  output="$(cloudflared tunnel create ijm2api-pre 2>&1)"
  tunnel_id="$(printf '%s\n' "$output" | grep -oE '[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}' | tail -n 1)"
  test -n "$tunnel_id"
  printf '%s\n' "$tunnel_id" > "$id_file"
fi

tunnel_id="$(cat "$id_file")"
credentials="$HOME/.cloudflared/${tunnel_id}.json"
test -f "$credentials"
route_config="$config_dir/config.yml"
cat > "$route_config" <<EOF
tunnel: $tunnel_id
credentials-file: $credentials
ingress:
  - hostname: $IJM2API_HOSTNAME
    service: $IJM2API_ORIGIN
  - service: http_status:404
EOF

cloudflared tunnel --config "$route_config" ingress validate
if curl -fsS --max-time 10 "https://${IJM2API_HOSTNAME}/health" | grep -q '"service":"ijm2api"'; then
  echo "IJM2API public route is already healthy"
else
  route_ready=false
  for attempt in 1 2 3; do
    if cloudflared tunnel route dns --overwrite-dns "$tunnel_id" "$IJM2API_HOSTNAME"; then
      route_ready=true
      break
    fi
    sleep "$attempt"
  done
  if [ "$route_ready" != true ]; then
    echo "Could not configure IJM2API public DNS route" >&2
    exit 1
  fi
fi

cat > "$HOME/.config/systemd/user/ijm2api-cloudflared.service" <<EOF
[Unit]
Description=IJM2API Cloudflare Tunnel connector
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared --config $route_config tunnel run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable ijm2api-cloudflared.service
systemctl --user restart ijm2api-cloudflared.service
systemctl --user is-active --quiet ijm2api-cloudflared.service
