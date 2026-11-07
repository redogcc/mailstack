#!/usr/bin/env bash
# Certbot DNS-01 auth hook — AK/SK signed DNS API. Credentials come from install.conf.
set -Eeuo pipefail
PREFIX="${MAILSTACK_PREFIX:-/opt/mailstack}"
CONF="${MAILSTACK_STATE:-/etc/mailstack}/install.conf"
[[ -f "$CONF" ]] || { echo "missing $CONF" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$CONF"
set +a
export DNS_ACCESS_KEY="${DNS_ACCESS_KEY:-}" DNS_SECRET_KEY="${DNS_SECRET_KEY:-}" \
       DNS_ZONE_ID="${DNS_ZONE_ID:-}" DNS_API_HOST="${DNS_API_HOST:-}" \
       DNS_API_REGION="${DNS_API_REGION:-}" DNS_API_SERVICE="${DNS_API_SERVICE:-dns}"
exec python3 "$PREFIX/scripts/acme_dns.py" create
