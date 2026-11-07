#!/usr/bin/env bash
# Remove an mailstack installation. Mail data is kept unless --purge is given.
set -Eeuo pipefail

STATE_DIR="/etc/mailstack"
PURGE="no"
PROFILE="production"
ASSUME_YES="no"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE="yes"; shift ;;
    --profile) PROFILE="$2"; STATE_DIR="/etc/mailstack-$2"; shift 2 ;;
    -y|--yes) ASSUME_YES="yes"; shift ;;
    -h|--help) echo "usage: sudo ./uninstall.sh [--purge] [-y] [--profile sandbox]"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

CONF="$STATE_DIR/install.conf"
CONFIG_JSON="$STATE_DIR/config.json"
[[ -f "$CONF" ]] || { echo "no installation found at $STATE_DIR" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$CONF"; set +a

json_get() {
  [[ -f "$CONFIG_JSON" ]] || return 0
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get(sys.argv[2],""))' \
    "$CONFIG_JSON" "$1" 2>/dev/null || true
}

# config.json is authoritative: install.conf may predate a field
_v="$(json_get domain)";        [[ -n "$_v" ]] && MAIL_DOMAIN="$_v" || true
_v="$(json_get maddy_service)"; [[ -n "$_v" ]] && MADDY_SERVICE="$_v" || true
_v="$(json_get admin_service)"; [[ -n "$_v" ]] && ADMIN_SERVICE="$_v" || true
_v="$(json_get data_dir)";      [[ -n "$_v" ]] && DATA_DIR="$_v" || true
_v="$(json_get cert_dir)";      [[ -n "$_v" ]] && CERT_DIR="$_v" || true

MADDY_SERVICE="${MADDY_SERVICE:?cannot determine maddy service name - refusing to guess}"
ADMIN_SERVICE="${ADMIN_SERVICE:?cannot determine admin service name - refusing to guess}"
DATA_DIR="${DATA_DIR:?cannot determine data dir - refusing to guess}"

# a sandbox profile must never touch production paths
if [[ "$PROFILE" == "sandbox" ]]; then
  for path in "${INSTALL_PREFIX:-}" "${SECRETS_DIR:-}" "$DATA_DIR" "${MADDY_CONF:-}"; do
    [[ "$path" == *sandbox* ]] || { echo "sandbox profile points at $path - refusing" >&2; exit 1; }
  done
fi

echo "Removing mailstack ($MAIL_DOMAIN)"
echo "  data dir : $DATA_DIR"
echo "  purge    : $PURGE"

if [[ "$ASSUME_YES" != "yes" ]]; then
  printf 'Type the mail domain to confirm: '
  read -r answer || true
  [[ "$answer" == "$MAIL_DOMAIN" ]] || { echo "aborted"; exit 1; }
fi

for svc in "$MADDY_SERVICE" "$ADMIN_SERVICE"; do
  systemctl disable --now "$svc" 2>/dev/null || true
  rm -f "/etc/systemd/system/$svc.service"
done
systemctl daemon-reload

if [[ "${ENABLE_NGINX:-yes}" == "yes" ]]; then
  rm -f /etc/nginx/sites-enabled/mailstack /etc/nginx/sites-available/mailstack
  rm -f /etc/nginx/snippets/mailstack-locations.conf
  nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
fi

rm -rf "${INSTALL_PREFIX:-/opt/mailstack}"
rm -rf "$STATE_DIR"
rm -rf "${SECRETS_DIR:-/etc/maddy-admin}"
if [[ -n "${MADDY_CONF:-}" ]]; then rm -f "$MADDY_CONF"; fi
rm -f /usr/local/bin/mailstack-deploy-cert
rm -f /etc/letsencrypt/renewal-hooks/deploy/mailstack-cert.sh

if [[ "$PURGE" == "yes" ]]; then
  # only remove shared web roots that carry our ownership marker
  if [[ "$(cat /var/www/landing/.mailstack-owner 2>/dev/null)" == "$MAIL_DOMAIN" ]]; then
    rm -rf /var/www/landing
  fi
  if [[ "$(cat /var/www/mta-sts/.mailstack-owner 2>/dev/null)" == "$MAIL_DOMAIN" ]]; then
    rm -rf /var/www/mta-sts
  fi
  rm -rf "$DATA_DIR"
  echo "mail data removed from $DATA_DIR"
  userdel "${MADDY_USER:-maddy}" 2>/dev/null || true
else
  echo "mail data kept in $DATA_DIR (remove with --purge)"
fi

echo "done"
