#!/usr/bin/env bash
set -Eeuo pipefail

site="${1:-/etc/nginx/sites-enabled/echolife}"
ip_site="${2:-/etc/nginx/sites-enabled/memoria-ip}"

sudo test -f "$site"
sudo test -f "$ip_site"

assert_count() {
  local expected="$1"
  local needle="$2"
  local file="$3"
  local actual
  actual="$(sudo grep -cF "$needle" "$file" || true)"
  if [[ "$actual" != "$expected" ]]; then
    echo "expected $expected occurrence(s) of '$needle' in $file, found $actual" >&2
    exit 1
  fi
}

assert_count 1 'include /etc/nginx/snippets/memoria-http.conf;' "$site"
assert_count 1 'include /etc/nginx/snippets/memoria-https.conf;' "$site"
assert_count 1 'try_files /memoria-h5/index.html =404;' "$site"
assert_count 1 'include /etc/nginx/snippets/memoria-https.conf;' "$ip_site"

if sudo grep -qE 'pocketsparks|goods-invoice' "$site" "$ip_site"; then
  echo "retired PocketSparks/Goods Invoice routes still exist in an enabled site" >&2
  exit 1
fi

sudo nginx -t
echo "nginx Memoria root-domain validation: PASS (read-only, no reload)"
