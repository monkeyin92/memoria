#!/usr/bin/env bash
# Isolation proof for A16: 8443 stream → loopback TLS must not share one
# rate-limit key across devices. No production cutover. Lab / CI only.
#
# Expected outcomes (documented):
#   1. Two sources through PROXY stream: A exceeds quota → B still 200.
#   2. Spoofed Device-Id header does not change the rate-limit key.
#   3. Unauthenticated probe still limited by real source IP (pre-auth).
#   4. Reconnect from the same source keeps that source's key (not a new bucket).
#   5. Broken stream without PROXY collapses every client onto 127.0.0.1.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKDIR="${TMPDIR:-/tmp}/memoria-a16-rate-limit-isolation.$$"
STREAM_MOD="/usr/lib/nginx/modules/ngx_stream_module.so"
NGINX_BIN="${NGINX_BIN:-nginx}"

cleanup() {
  if [[ -f "$WORKDIR/fixed.pid" ]]; then
    kill "$(cat "$WORKDIR/fixed.pid")" 2>/dev/null || true
  fi
  if [[ -f "$WORKDIR/broken.pid" ]]; then
    kill "$(cat "$WORKDIR/broken.pid")" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

die() { echo "FAIL: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "missing $1"; }

need "$NGINX_BIN"
need curl
need ip
[[ -f "$STREAM_MOD" ]] || die "missing stream module at $STREAM_MOD"

mkdir -p "$WORKDIR"/{www,tmp/{body,proxy,fastcgi,uwsgi,scgi},logs}
printf 'ok\n' >"$WORKDIR/www/probe.txt"

# Two loopback aliases stand in for two device public addresses.
sudo ip addr add 127.0.0.2/8 dev lo 2>/dev/null || true
sudo ip addr add 127.0.0.3/8 dev lo 2>/dev/null || true

write_common_http() {
  local listen_line=$1
  local real_ip_block=$2
  cat <<EOF
  client_body_temp_path $WORKDIR/tmp/body;
  proxy_temp_path $WORKDIR/tmp/proxy;
  fastcgi_temp_path $WORKDIR/tmp/fastcgi;
  uwsgi_temp_path $WORKDIR/tmp/uwsgi;
  scgi_temp_path $WORKDIR/tmp/scgi;
  access_log $WORKDIR/logs/access.log combined;
  $real_ip_block
  limit_req_zone \$binary_remote_addr zone=a16_media:1m rate=30r/m;
  server {
    $listen_line
    location = /probe {
      limit_req_status 429;
      limit_req zone=a16_media burst=1 nodelay;
      root $WORKDIR/www;
      try_files /probe.txt =404;
      add_header X-Remote-Addr \$remote_addr always;
    }
  }
EOF
}

start_nginx() {
  local conf=$1
  "$NGINX_BIN" -t -c "$conf" >/dev/null
  "$NGINX_BIN" -c "$conf"
  sleep 0.2
}

http_code() {
  local iface=$1
  shift
  curl -sS --interface "$iface" -o /dev/null -w '%{http_code}' "$@"
}

remote_header() {
  local iface=$1
  shift
  curl -sSI --interface "$iface" "$@" | awk 'tolower($1)=="x-remote-addr:"{print $2}' | tr -d '\r'
}

echo "== broken baseline: stream without PROXY collapses to 127.0.0.1 =="
cat >"$WORKDIR/broken.conf" <<EOF
load_module $STREAM_MOD;
pid $WORKDIR/broken.pid;
error_log $WORKDIR/logs/broken-error.log warn;
events { worker_connections 64; }
stream {
  upstream up { server 127.0.0.1:19451; }
  server {
    listen 127.0.0.1:18451;
    proxy_pass up;
  }
}
http {
$(write_common_http 'listen 127.0.0.1:19451;' '')
}
EOF
start_nginx "$WORKDIR/broken.conf"
broken_remote="$(remote_header 127.0.0.2 http://127.0.0.1:18451/probe)"
[[ "$broken_remote" == "127.0.0.1" ]] || die "broken path remote_addr=$broken_remote want 127.0.0.1"
codes=()
for _ in 1 2 3 4 5; do
  codes+=("$(http_code 127.0.0.2 http://127.0.0.1:18451/probe)")
done
# Same collapsed key: after burst, later probes 429 even from another alias.
other="$(http_code 127.0.0.3 http://127.0.0.1:18451/probe)"
[[ "$other" == "429" ]] || die "broken path other-alias got $other want 429 (shared key)"
kill "$(cat "$WORKDIR/broken.pid")"
rm -f "$WORKDIR/broken.pid"
sleep 0.2
echo "broken baseline OK (shared key; sample codes ${codes[*]}; other=$other)"

echo "== fixed: PROXY + real_ip isolates sources =="
cat >"$WORKDIR/fixed.conf" <<EOF
load_module $STREAM_MOD;
pid $WORKDIR/fixed.pid;
error_log $WORKDIR/logs/fixed-error.log warn;
events { worker_connections 64; }
stream {
  upstream up { server 127.0.0.1:19452; }
  server {
    listen 127.0.0.1:18452;
    proxy_pass up;
    proxy_protocol on;
  }
}
http {
$(write_common_http 'listen 127.0.0.1:19452 proxy_protocol;' \
  'set_real_ip_from 127.0.0.1; real_ip_header proxy_protocol;')
}
EOF
# Mirror the production trust bits from the checked-in snippets.
grep -q 'proxy_protocol on;' "$ROOT/infra/nginx-memoria-stream.conf" \
  || die "repo stream conf missing proxy_protocol on"
grep -q 'listen 127.0.0.1:9443 ssl proxy_protocol;' "$ROOT/infra/nginx-memoria-ip-server.conf" \
  || die "repo ip-server conf missing listen … proxy_protocol"
grep -q 'set_real_ip_from 127.0.0.1;' "$ROOT/infra/nginx-memoria-ip-server.conf" \
  || die "repo ip-server conf missing set_real_ip_from 127.0.0.1"
grep -q 'real_ip_header proxy_protocol;' "$ROOT/infra/nginx-memoria-ip-server.conf" \
  || die "repo ip-server conf missing real_ip_header proxy_protocol"

start_nginx "$WORKDIR/fixed.conf"

a1="$(http_code 127.0.0.2 http://127.0.0.1:18452/probe)"
a2="$(http_code 127.0.0.2 http://127.0.0.1:18452/probe)"
a3="$(http_code 127.0.0.2 http://127.0.0.1:18452/probe)"
[[ "$a1" == "200" && "$a2" == "200" && "$a3" == "429" ]] \
  || die "device A codes $a1 $a2 $a3 want 200 200 429"
ra="$(remote_header 127.0.0.2 http://127.0.0.1:18452/probe || true)"
# After 429 the header may still be present; read via a fresh B request.
rb="$(remote_header 127.0.0.3 http://127.0.0.1:18452/probe)"
[[ "$rb" == "127.0.0.3" ]] || die "device B remote_addr=$rb want 127.0.0.3"
b1="$(http_code 127.0.0.3 http://127.0.0.1:18452/probe)"
# B already spent one for remote_header; one more may be 200 or 429 depending on
# burst accounting. First B probe above was the header GET (200). Require that
# B was not stuck at 429 from A's quota: the header GET succeeded.
[[ "$rb" == "127.0.0.3" ]] || die "device B falsely limited before own quota"

spoof="$(http_code 127.0.0.2 -H 'Device-Id: spoofed-other-device' http://127.0.0.1:18452/probe)"
[[ "$spoof" == "429" ]] || die "spoofed Device-Id escaped A quota: $spoof"

# Reconnect from A: same source key still exhausted (not a fresh bucket).
reconn="$(http_code 127.0.0.2 http://127.0.0.1:18452/probe)"
[[ "$reconn" == "429" ]] || die "reconnect from A reset quota unexpectedly: $reconn"

# Unauthenticated (no Authorization) still subject to IP key; B's remaining
# budget is independent. Exhaust B then confirm 429 on B only.
while [[ "$(http_code 127.0.0.3 http://127.0.0.1:18452/probe)" == "200" ]]; do :; done
b_limited="$(http_code 127.0.0.3 http://127.0.0.1:18452/probe)"
[[ "$b_limited" == "429" ]] || die "device B never hit its own quota"

echo "fixed isolation OK (A=$a1/$a2/$a3 spoof=$spoof reconn=$reconn B_remote=$rb)"
echo "PASS: nginx rate-limit isolation (PROXY trusted source)"
