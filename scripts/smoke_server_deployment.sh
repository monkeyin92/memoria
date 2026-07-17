#!/usr/bin/env bash
set -Eeuo pipefail

tag="${1:?usage: smoke_server_deployment.sh <release-tag>}"
release="/opt/memoria/releases/$tag"
image="memoria-control-api:$tag"
container="memoria-control-preflight"
nginx_config="$release/infra/nginx-memoria-loopback-smoke.conf"
data_dir="$(mktemp -d /tmp/memoria-preflight-data.XXXXXX)"
workdir="$(mktemp -d /tmp/memoria-preflight-work.XXXXXX)"
host_header="Host: aginice.cn"

cleanup() {
  if sudo test -f /tmp/memoria-nginx-smoke.pid; then
    sudo nginx -s quit -c "$nginx_config" >/dev/null 2>&1 || true
  fi
  sudo docker rm -f "$container" >/dev/null 2>&1 || true
  sudo rm -rf "$data_dir" "$workdir"
  sudo rm -f /tmp/memoria-nginx-smoke.pid /tmp/memoria-nginx-smoke-error.log
}
trap cleanup EXIT

test -f "$nginx_config"
sudo docker image inspect "$image" >/dev/null
if sudo ss -ltn | grep -qE ':(8791|18891)[[:space:]]'; then
  echo "preflight ports 8791 or 18891 are already in use" >&2
  exit 1
fi

sudo chown 65532:65532 "$data_dir"

start_control() {
  sudo docker run -d --name "$container" \
    --read-only \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    -p 127.0.0.1:8791:8000 \
    -v "$data_dir:/data" \
    -e ENVIRONMENT=production \
    -e MEMORIA_RELEASE_TAG=preflight \
    -e PUBLIC_BASE_URL=https://aginice.cn:8443/memoria-api \
    -e ALLOWED_ORIGINS=https://aginice.cn,https://www.aginice.cn,https://aginice.cn:8443,https://www.aginice.cn:8443 \
    -e LIVEKIT_URL=wss://preflight.livekit.cloud \
    -e LIVEKIT_API_KEY=preflight-key \
    -e LIVEKIT_API_SECRET=preflight-secret \
    -e MEMORIA_AUTH_SECRET=preflight-auth-secret-that-is-longer-than-thirty-two-characters \
    -e MEMORIA_DB_PATH=/data/memoria.sqlite3 \
    -e MEMORIA_TIMEZONE=Asia/Shanghai \
    -e OFFLINE_MOCK=true \
    "$image" >/dev/null
}

wait_control() {
  for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8791/health/live >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  sudo docker logs "$container" >&2 || true
  return 1
}

start_control
wait_control

sudo nginx -t -c "$nginx_config"
sudo nginx -c "$nginx_config"

base=http://127.0.0.1:18891
curl -fsS -H "$host_header" -D "$workdir/h5.headers" \
  "$base/memoria-h5/" -o "$workdir/h5.index"
curl -fsS -H "$host_header" \
  "$base/memoria-h5/arbitrary-spa-route" -o "$workdir/h5.spa"
cmp "$workdir/h5.index" "$workdir/h5.spa"
grep -qi '^Permissions-Policy: microphone=(self)' "$workdir/h5.headers"

curl -fsS -H "$host_header" "$base/memoria-api/health/live" \
  -o "$workdir/live.json"
curl -fsS -H "$host_header" "$base/memoria-api/health/ready" \
  -o "$workdir/ready.json"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "ok"' \
  "$workdir/live.json"
python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); assert body["status"] == "ready" and body["mode"] == "offline_mock"' \
  "$workdir/ready.json"

internal_status="$(curl -sS -o /dev/null -w '%{http_code}' -H "$host_header" \
  -X POST "$base/memoria-api/internal/readiness/smokes")"
test "$internal_status" = "404"
unauthorized_status="$(curl -sS -o /dev/null -w '%{http_code}' -H "$host_header" \
  "$base/memoria-api/v1/memory/days?user_id=unauthorized")"
test "$unauthorized_status" = "401"

curl -fsS -H "$host_header" -H 'Content-Type: application/json' \
  -X POST "$base/memoria-api/v1/auth/anonymous" \
  -o "$workdir/identity.json"
read -r user_id access_token < <(
  python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); print(body["user_id"], body["access_token"])' \
    "$workdir/identity.json"
)
auth_header="Authorization: Bearer $access_token"

curl -fsS -H "$host_header" -H "$auth_header" -H 'Content-Type: application/json' \
  -X PUT "$base/memoria-api/v1/memory/profile/$user_id" \
  -d '{"display_name":"部署预检","bio":"loopback","timezone":"Asia/Shanghai"}' \
  -o "$workdir/profile.put.json"
curl -fsS -H "$host_header" -H "$auth_header" -H 'Content-Type: application/json' \
  -X POST "$base/memoria-api/v1/memory/messages" \
  -d "{\"user_id\":\"$user_id\",\"role\":\"user\",\"text\":\"部署预检消息\",\"emotion\":\"calm\"}" \
  -o "$workdir/message.post.json"

sudo docker rm -f "$container" >/dev/null
start_control
wait_control

curl -fsS -H "$host_header" -H "$auth_header" \
  "$base/memoria-api/v1/memory/profile/$user_id" \
  -o "$workdir/profile.get.json"
curl -fsS -H "$host_header" -H "$auth_header" \
  "$base/memoria-api/v1/memory/days?user_id=$user_id" \
  -o "$workdir/days.get.json"
python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); assert body["display_name"] == "部署预检" and body["timezone"] == "Asia/Shanghai"' \
  "$workdir/profile.get.json"
python3 -c 'import json,sys; body=json.load(open(sys.argv[1])); assert body["items"] and body["items"][0]["message_count"] == 1' \
  "$workdir/days.get.json"

echo "server deployment smoke: PASS (H5, SPA fallback, API proxy, SQLite restart persistence)"
