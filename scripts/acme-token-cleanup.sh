#!/usr/bin/env bash
# Certbot DNS-01 cleanup hook — bearer-token DNS API.
set -Eeuo pipefail
PREFIX="${MAILSTACK_PREFIX:-/opt/mailstack}"
CONF="${MAILSTACK_STATE:-/etc/mailstack}/install.conf"
set -a
# shellcheck disable=SC1090
[[ -f "$CONF" ]] && . "$CONF"
set +a
export DNS_TOKEN="${DNS_TOKEN:-}" DNS_API_BASE="${DNS_API_BASE:-}"
exec python3 "$PREFIX/scripts/acme_dns.py" cleanup
