#!/usr/bin/env bash
# Certbot DNS-01 cleanup hook — AK/SK signed DNS API.
set -Eeuo pipefail
PREFIX="${MAILSTACK_PREFIX:-/opt/mailstack}"
CONF="${MAILSTACK_STATE:-/etc/mailstack}/install.conf"
set -a
# shellcheck disable=SC1090
[[ -f "$CONF" ]] && . "$CONF"
set +a
export DNS_ACCESS_KEY="${DNS_ACCESS_KEY:-}" DNS_SECRET_KEY="${DNS_SECRET_KEY:-}" \
       DNS_ZONE_ID="${DNS_ZONE_ID:-}" DNS_API_HOST="${DNS_API_HOST:-}" \
       DNS_API_REGION="${DNS_API_REGION:-}" DNS_API_SERVICE="${DNS_API_SERVICE:-dns}"
exec python3 "$PREFIX/scripts/acme_dns.py" cleanup
