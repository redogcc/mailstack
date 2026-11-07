#!/usr/bin/env bash
# Certbot DNS-01 auth hook — bearer-token DNS API.
set -Eeuo pipefail
PREFIX="${MAILSTACK_PREFIX:-/opt/mailstack}"
CONF="${MAILSTACK_STATE:-/etc/mailstack}/install.conf"
[[ -f "$CONF" ]] || { echo "missing $CONF" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$CONF"
set +a
export DNS_TOKEN="${DNS_TOKEN:-}" DNS_API_BASE="${DNS_API_BASE:-}"
exec python3 "$PREFIX/scripts/acme_dns.py" create
