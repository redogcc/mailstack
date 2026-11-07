#!/usr/bin/env bash
# =============================================================================
#  mailstack - one-command installer for a small/medium business mail system
#
#  Maddy (SMTP/IMAP) + nginx (TLS) + a self-contained Python web console
#
#  Typical use:
#      sudo ./install.sh                       # interactive wizard
#      sudo ./install.sh -c my.conf -y         # unattended
#      sudo ./install.sh --domain example.com --ip 1.2.3.4 --tls http01 -y
#
#  Everything the installer writes can be regenerated with --reconfigure.
# =============================================================================
set -Eeuo pipefail

VERSION="1.0.0"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"

# ------------------------------------------------------------------ defaults
MAIL_DOMAIN=""
MAIL_HOSTNAME=""
SERVER_IP=""
ADMIN_EMAIL=""
ADMIN_PASSWORD="auto"
TLS_MODE="http01"
ACME_EMAIL=""
DNS_TOKEN=""
DNS_API_BASE=""
DNS_ACCESS_KEY=""
DNS_SECRET_KEY=""
DNS_ZONE_ID=""
DNS_API_HOST=""
DNS_API_REGION=""
DNS_API_SERVICE=""
ENABLE_WEB="yes"
ENABLE_LANDING="yes"
ENABLE_NGINX="yes"
WEB_PORT="8080"
INSTALL_PREFIX="/opt/mailstack"
STATE_DIR="/etc/mailstack"
SECRETS_DIR="/etc/maddy-admin"
DATA_DIR="/var/lib/maddy"
MADDY_CONF="/etc/maddy/maddy.conf"
MADDY_BIN="/usr/local/bin/maddy"
MADDY_VERSION="0.9.7"
MADDY_USER="maddy"
SMTP_PORT="25"
SMTPS_PORT="465"
SUBMISSION_PORT="587"
IMAP_PORT="993"
PROFILE="production"
ASSUME_YES="no"
DRY_RUN="no"
RECONFIGURE="no"
CONF_FILE=""

# ------------------------------------------------------------------- logging
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_CYN=$'\033[36m'
else
  C_RESET=""; C_DIM=""; C_B=""; C_RED=""; C_GRN=""; C_YEL=""; C_CYN=""
fi

log()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$C_CYN" "$C_RESET" "$C_B" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s[warn]%s %s\n' "$C_YEL" "$C_RESET" "$*" >&2; }
die()  { printf '%s[error]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

# ------------------------------------------------------------------ arguments
usage() {
  cat <<'EOF'
mailstack installer

Usage: sudo ./install.sh [options]

  -c, --config FILE      read values from FILE (see conf/install.conf.example)
  -d, --domain DOMAIN    mail domain, e.g. example.com
      --hostname NAME    mail hostname, default mail.<domain>
  -i, --ip ADDR          public IPv4 of this server (auto-detected if omitted)
  -a, --admin EMAIL      administrator mailbox, default admin@<domain>
  -p, --password PASS    admin password ("auto" generates one)
      --tls MODE         http01 | dns-manual | dns-token | dns-aksign | selfsigned
      --acme-email EMAIL contact address for Let's Encrypt
      --web-port PORT    web console port on loopback (default 8080)
      --no-web           do not install the web console
      --no-landing       do not install the public landing page
      --no-nginx         do not configure nginx (console listens directly)
  -y, --yes              never prompt, use the values as given
      --dry-run          render configs into a temporary tree, change nothing
      --reconfigure      re-render configs from the saved install.conf
      --profile sandbox  isolated test instance (own ports/users/dirs)
      --version          print version
  -h, --help             this help

Examples
  sudo ./install.sh
  sudo ./install.sh -c /root/mail.conf -y
  sudo ./install.sh --domain acme.cn --ip 203.0.113.10 --tls dns-aksign -y
EOF
}

need_arg() { [[ $# -ge 2 && -n "${2:-}" ]] || die "option $1 needs a value"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config)      need_arg "$@"; CONF_FILE="$2"; shift 2 ;;
    -d|--domain)      need_arg "$@"; MAIL_DOMAIN="$2"; shift 2 ;;
    --hostname)       need_arg "$@"; MAIL_HOSTNAME="$2"; shift 2 ;;
    -i|--ip)          need_arg "$@"; SERVER_IP="$2"; shift 2 ;;
    -a|--admin)       need_arg "$@"; ADMIN_EMAIL="$2"; shift 2 ;;
    -p|--password)    need_arg "$@"; ADMIN_PASSWORD="$2"; shift 2 ;;
    --tls)            need_arg "$@"; TLS_MODE="$2"; shift 2 ;;
    --acme-email)     need_arg "$@"; ACME_EMAIL="$2"; shift 2 ;;
    --web-port)       need_arg "$@"; WEB_PORT="$2"; shift 2 ;;
    --no-web)         ENABLE_WEB="no"; shift ;;
    --no-landing)     ENABLE_LANDING="no"; shift ;;
    --no-nginx)       ENABLE_NGINX="no"; shift ;;
    -y|--yes)         ASSUME_YES="yes"; shift ;;
    --dry-run)        DRY_RUN="yes"; shift ;;
    --reconfigure)    RECONFIGURE="yes"; shift ;;
    --profile)        need_arg "$@"; PROFILE="$2"; shift 2 ;;
    --version)        echo "mailstack installer $VERSION"; exit 0 ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown option: $1 (try --help)" ;;
  esac
done

[[ $EUID -eq 0 || "$DRY_RUN" == "yes" ]] || die "please run as root: sudo ./install.sh"

# ------------------------------------------------------------------ load conf
load_conf_file() {
  local file="$1"
  [[ -f "$file" ]] || die "config file not found: $file"
  local key value
  while IFS='=' read -r key value; do
    key="$(echo "$key" | tr -d '[:space:]')"
    [[ -z "$key" || "$key" == \#* ]] && continue
    value="$(echo "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    # command line wins over the file
    case "$key" in
      MAIL_DOMAIN|MAIL_HOSTNAME|SERVER_IP|ADMIN_EMAIL|ADMIN_PASSWORD|TLS_MODE|\
      ACME_EMAIL|DNS_TOKEN|DNS_API_BASE|DNS_ACCESS_KEY|DNS_SECRET_KEY|\
      DNS_ZONE_ID|DNS_API_HOST|DNS_API_REGION|DNS_API_SERVICE|\
      ENABLE_WEB|ENABLE_LANDING|ENABLE_NGINX|WEB_PORT|INSTALL_PREFIX|STATE_DIR|\
      SECRETS_DIR|DATA_DIR|MADDY_CONF|MADDY_BIN|MADDY_VERSION|MADDY_USER|\
      SMTP_PORT|SMTPS_PORT|SUBMISSION_PORT|IMAP_PORT)
        # only apply when the variable still holds its compiled default
        case "$key" in
          MAIL_DOMAIN)        [[ -z "$MAIL_DOMAIN" ]] && MAIL_DOMAIN="$value" ;;
          MAIL_HOSTNAME)      [[ -z "$MAIL_HOSTNAME" ]] && MAIL_HOSTNAME="$value" ;;
          SERVER_IP)          [[ -z "$SERVER_IP" ]] && SERVER_IP="$value" ;;
          ADMIN_EMAIL)        [[ -z "$ADMIN_EMAIL" ]] && ADMIN_EMAIL="$value" ;;
          ACME_EMAIL)         [[ -z "$ACME_EMAIL" ]] && ACME_EMAIL="$value" ;;
          ADMIN_PASSWORD)     [[ "$ADMIN_PASSWORD" == "auto" ]] && ADMIN_PASSWORD="$value" ;;
          TLS_MODE)           [[ "$TLS_MODE" == "http01" ]] && TLS_MODE="$value" ;;
          DNS_TOKEN)          [[ -z "$DNS_TOKEN" ]] && DNS_TOKEN="$value" ;;
          DNS_API_BASE)       [[ -z "$DNS_API_BASE" ]] && DNS_API_BASE="$value" ;;
          DNS_ACCESS_KEY)     [[ -z "$DNS_ACCESS_KEY" ]] && DNS_ACCESS_KEY="$value" ;;
          DNS_SECRET_KEY)     [[ -z "$DNS_SECRET_KEY" ]] && DNS_SECRET_KEY="$value" ;;
          DNS_ZONE_ID)        [[ -z "$DNS_ZONE_ID" ]] && DNS_ZONE_ID="$value" ;;
          DNS_API_HOST)       [[ -z "$DNS_API_HOST" ]] && DNS_API_HOST="$value" ;;
          DNS_API_REGION)     [[ -z "$DNS_API_REGION" ]] && DNS_API_REGION="$value" ;;
          DNS_API_SERVICE)    [[ -z "$DNS_API_SERVICE" ]] && DNS_API_SERVICE="$value" ;;
          WEB_PORT)           [[ "$WEB_PORT" == "8080" ]] && WEB_PORT="$value" ;;
          *)                  printf -v "$key" '%s' "$value" ;;
        esac
        ;;
    esac
  done < "$file"
}

if [[ -n "$CONF_FILE" ]]; then
  load_conf_file "$CONF_FILE"
elif [[ "$RECONFIGURE" == "yes" && -f "$STATE_DIR/install.conf" ]]; then
  load_conf_file "$STATE_DIR/install.conf"
elif [[ "$PROFILE" != "sandbox" && -f /etc/mailstack/install.conf && "$ASSUME_YES" == "yes" ]]; then
  # re-running on an installed host: reuse the previous answers
  load_conf_file /etc/mailstack/install.conf
fi

# ------------------------------------------------------------------ profile
if [[ "$PROFILE" == "sandbox" ]]; then
  INSTALL_PREFIX="/opt/mailstack-sandbox"
  STATE_DIR="/etc/mailstack-sandbox"
  SECRETS_DIR="/etc/mailstack-sandbox/secrets"
  DATA_DIR="/var/lib/maddy-sandbox"
  MADDY_CONF="/etc/maddy/maddy-sandbox.conf"
  MADDY_USER="maddy-sandbox"
  MADDY_SERVICE="maddy-sandbox"
  ADMIN_SERVICE="maddy-admin-sandbox"
  SMTP_PORT="2525"; SMTPS_PORT="2465"; SUBMISSION_PORT="2587"; IMAP_PORT="2993"
  WEB_PORT="18080"
  ENABLE_NGINX="no"
  ENABLE_LANDING="no"
  [[ "$TLS_MODE" == "http01" ]] && TLS_MODE="selfsigned"
else
  MADDY_SERVICE="maddy"
  ADMIN_SERVICE="maddy-admin"
fi

# ------------------------------------------------------------------ wizard
is_tty() { [[ -t 0 && -t 1 ]]; }

ask() { # ask VAR "prompt" default
  local __var="$1" __prompt="$2" __default="${3:-}" __answer=""
  if [[ -n "$__default" ]]; then
    printf '%s %s[%s]%s: ' "$__prompt" "$C_DIM" "$__default" "$C_RESET"
  else
    printf '%s: ' "$__prompt"
  fi
  read -r __answer || true
  [[ -z "$__answer" ]] && __answer="$__default"
  printf -v "$__var" '%s' "$__answer"
}

choose_tls() {
  local choice=""
  while true; do
    cat <<EOF

TLS certificate
  1) Let's Encrypt via HTTP-01      - needs port 80 reachable, DNS already pointing here
  2) Let's Encrypt via DNS-01       - add two TXT records by hand (works behind firewalls)
  3) Let's Encrypt via DNS-01 API   - automatic, bearer token (cloud DNS API)
  4) Let's Encrypt via DNS-01 AK/SK - automatic, signed credentials (cloud DNS API)
  5) self-signed                    - testing only, mail clients will warn
EOF
    ask choice "Choose 1-5" "1"
    case "$choice" in
      1) TLS_MODE="http01"; return ;;
      2) TLS_MODE="dns-manual"; return ;;
      3) TLS_MODE="dns-token"; return ;;
      4) TLS_MODE="dns-aksign"; return ;;
      5) TLS_MODE="selfsigned"; return ;;
      *) warn "please answer 1-5" ;;
    esac
  done
}

wizard() {
  step "mailstack $VERSION - interactive setup"
  printf '%s\n' "This will install a complete mail server on this machine."
  ask MAIL_DOMAIN "Mail domain (the part after @)" "example.com"
  [[ -n "$MAIL_HOSTNAME" ]] || MAIL_HOSTNAME="mail.$MAIL_DOMAIN"
  ask MAIL_HOSTNAME "Mail hostname (A record -> this server)" "$MAIL_HOSTNAME"
  local detected; detected="$(detect_public_ip || true)"
  ask SERVER_IP "Public IPv4 of this server" "${SERVER_IP:-$detected}"
  [[ -n "$ADMIN_EMAIL" ]] || ADMIN_EMAIL="admin@$MAIL_DOMAIN"
  ask ADMIN_EMAIL "Administrator mailbox" "$ADMIN_EMAIL"
  [[ -z "$ACME_EMAIL" ]] && ACME_EMAIL="$ADMIN_EMAIL"
  choose_tls
  case "$TLS_MODE" in
    dns-token)
      ask DNS_API_BASE "DNS API base URL (见云厂商控制台)" "${DNS_API_BASE:-}"
      ask DNS_TOKEN "DNS API token (zone read + record write)" "${DNS_TOKEN:-}" ;;
    dns-aksign)
      ask DNS_API_HOST "DNS API host (见云厂商控制台)" "${DNS_API_HOST:-}"
      ask DNS_API_REGION "DNS API region" "${DNS_API_REGION:-default}"
      ask DNS_ACCESS_KEY "Access Key ID" "${DNS_ACCESS_KEY:-}"
      ask DNS_SECRET_KEY "Secret Access Key" "${DNS_SECRET_KEY:-}"
      ask DNS_ZONE_ID "DNS Zone ID (optional)" "${DNS_ZONE_ID:-}" ;;
  esac
  if [[ "$TLS_MODE" != "selfsigned" ]]; then
    ask ACME_EMAIL "Let's Encrypt contact email" "$ACME_EMAIL"
  fi
  if [[ "$ENABLE_WEB" == "yes" ]]; then
    ask WEB_PORT "Web console port (loopback)" "$WEB_PORT"
  fi
}

# ------------------------------------------------------------------ helpers
detect_public_ip() {
  local ip=""
  ip="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
  [[ -z "$ip" ]] && ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')"
  [[ -z "$ip" ]] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "$ip"
}

valid_domain() { [[ "$1" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; }
valid_ip()     { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
valid_email()  { [[ "$1" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; }

random_password() {
  local pw
  pw="$(openssl rand -base64 24 | tr -d '\n/+=' | cut -c1-18)"
  printf 'Aa9!%s' "$pw"
}

port_free() { ! ss -H -tln "sport = :$1" 2>/dev/null | grep -q .; }

backup_path() {
  local path="$1"
  [[ -e "$path" ]] || return 0
  local dir="$STATE_DIR/backups/$STAMP"
  mkdir -p "$dir"
  cp -a "$path" "$dir/$(basename "$path")"
  info "backed up $path -> $dir/"
}

render() { # render TEMPLATE DEST [mode]
  local tpl="$1" dest="$2" mode="${3:-644}" tmp
  [[ -f "$tpl" ]] || die "template missing: $tpl"
  tmp="$(mktemp)"
  sed \
    -e "s|@@DOMAIN@@|${MAIL_DOMAIN}|g" \
    -e "s|@@HOSTNAME@@|${MAIL_HOSTNAME}|g" \
    -e "s|@@SERVER_IP@@|${SERVER_IP}|g" \
    -e "s|@@ADMIN_EMAIL@@|${ADMIN_EMAIL}|g" \
    -e "s|@@ADMIN_LOCAL@@|${ADMIN_EMAIL%%@*}|g" \
    -e "s|@@DATA_DIR@@|${DATA_DIR}|g" \
    -e "s|@@RUNTIME_DIR@@|${RUNTIME_DIR}|g" \
    -e "s|@@RUNTIME_NAME@@|${RUNTIME_NAME}|g" \
    -e "s|@@STATE_NAME@@|${STATE_NAME}|g" \
    -e "s|@@CERT_DIR@@|${CERT_DIR}|g" \
    -e "s|@@SECRETS_DIR@@|${SECRETS_DIR}|g" \
    -e "s|@@MADDY_CONF@@|${MADDY_CONF}|g" \
    -e "s|@@MADDY_BIN@@|${MADDY_BIN}|g" \
    -e "s|@@MADDY_USER@@|${MADDY_USER}|g" \
    -e "s|@@MADDY_SERVICE@@|${MADDY_SERVICE}|g" \
    -e "s|@@ADMIN_SERVICE@@|${ADMIN_SERVICE}|g" \
    -e "s|@@CERTBOT_TIMER@@|${CERTBOT_TIMER}|g" \
    -e "s|@@ADMIN_PY@@|${INSTALL_PREFIX}/admin/mailadmin.py|g" \
    -e "s|@@CONFIG_JSON@@|${STATE_DIR}/config.json|g" \
    -e "s|@@ADMIN_LISTEN@@|127.0.0.1|g" \
    -e "s|@@ADMIN_PORT@@|${WEB_PORT}|g" \
    -e "s|@@PUBLIC_URL@@|${PUBLIC_URL}|g" \
    -e "s|@@COOKIE_SECURE@@|${COOKIE_SECURE}|g" \
    -e "s|@@SMTP_PORT@@|${SMTP_PORT}|g" \
    -e "s|@@SMTPS_PORT@@|${SMTPS_PORT}|g" \
    -e "s|@@SUBMISSION_PORT@@|${SUBMISSION_PORT}|g" \
    -e "s|@@IMAP_PORT@@|${IMAP_PORT}|g" \
    -e "s|@@BRAND@@|${MAIL_DOMAIN}|g" \
    -e "s|@@DOMAIN_INITIAL@@|${MAIL_DOMAIN:0:1}|g" \
    -e "s|@@RENDERED_AT@@|$(date -u +%Y-%m-%dT%H:%M:%SZ)|g" \
    "$tpl" > "$tmp"
  if [[ "$DRY_RUN" == "yes" ]]; then
    info "would render $dest"; rm -f "$tmp"; return 0
  fi
  mkdir -p "$(dirname "$dest")"
  install -m "$mode" "$tmp" "$dest"
  rm -f "$tmp"
}

# ------------------------------------------------------------------ derive
derive() {
  [[ -n "$MAIL_DOMAIN" ]] || die "mail domain is required (--domain)"
  MAIL_DOMAIN="$(echo "$MAIL_DOMAIN" | tr 'A-Z' 'a-z')"
  valid_domain "$MAIL_DOMAIN" || die "invalid domain: $MAIL_DOMAIN"
  [[ -n "$MAIL_HOSTNAME" ]] || MAIL_HOSTNAME="mail.$MAIL_DOMAIN"
  MAIL_HOSTNAME="$(echo "$MAIL_HOSTNAME" | tr 'A-Z' 'a-z')"
  valid_domain "$MAIL_HOSTNAME" || die "invalid hostname: $MAIL_HOSTNAME"
  [[ -n "$SERVER_IP" ]] || SERVER_IP="$(detect_public_ip || true)"
  valid_ip "$SERVER_IP" || die "invalid IPv4 address: ${SERVER_IP:-<empty>}"
  [[ -n "$ADMIN_EMAIL" ]] || ADMIN_EMAIL="admin@$MAIL_DOMAIN"
  ADMIN_EMAIL="$(echo "$ADMIN_EMAIL" | tr 'A-Z' 'a-z')"
  valid_email "$ADMIN_EMAIL" || die "invalid admin email: $ADMIN_EMAIL"
  [[ "${ADMIN_EMAIL##*@}" == "$MAIL_DOMAIN" ]] || die "admin email must belong to $MAIL_DOMAIN"
  if [[ -n "$ACME_EMAIL" ]]; then
    valid_email "$ACME_EMAIL" || die "invalid ACME email: $ACME_EMAIL"
  fi
  ACME_EMAIL="${ACME_EMAIL:-$ADMIN_EMAIL}"
  [[ "$TLS_MODE" =~ ^(http01|dns-manual|dns-token|dns-aksign|selfsigned)$ ]] \
    || die "invalid TLS mode: $TLS_MODE"

  RUNTIME_NAME="$(basename "$DATA_DIR")"
  STATE_NAME="$(basename "$DATA_DIR")"
  RUNTIME_DIR="/run/$RUNTIME_NAME"
  CERT_DIR="/etc/maddy/certs/$MAIL_HOSTNAME"
  [[ "$PROFILE" == "sandbox" ]] && CERT_DIR="$STATE_DIR/certs"
  if [[ "$ENABLE_NGINX" == "yes" ]]; then
    PUBLIC_URL="https://$MAIL_HOSTNAME"
  else
    PUBLIC_URL="https://$MAIL_HOSTNAME:$WEB_PORT"
  fi
  [[ "$PROFILE" == "sandbox" ]] && PUBLIC_URL="https://$MAIL_HOSTNAME:$WEB_PORT"
  # HTTPS terminated by nginx -> secure cookie; direct HTTP console -> plain cookie
  if [[ "$ENABLE_NGINX" == "yes" ]]; then COOKIE_SECURE="true"; else COOKIE_SECURE="false"; fi
  CERTBOT_TIMER="certbot.timer"
}

# ------------------------------------------------------------------ preflight
preflight() {
  step "Pre-flight checks"
  [[ -f /etc/os-release ]] || die "this installer targets Debian/Ubuntu (systemd)"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) info "OS: ${PRETTY_NAME:-$ID}" ;;
    *) warn "untested distribution: ${PRETTY_NAME:-$ID}; continuing" ;;
  esac
  command -v systemctl >/dev/null || die "systemd is required"
  command -v apt-get >/dev/null || die "apt-get is required"

  info "domain      : $MAIL_DOMAIN"
  info "hostname    : $MAIL_HOSTNAME"
  info "public IP   : $SERVER_IP"
  info "admin       : $ADMIN_EMAIL"
  info "TLS mode    : $TLS_MODE"
  info "ports       : smtp $SMTP_PORT, submission $SUBMISSION_PORT, smtps $SMTPS_PORT, imap $IMAP_PORT"
  info "data dir    : $DATA_DIR"
  info "secrets dir : $SECRETS_DIR"

  if [[ -f "$MADDY_CONF" && "$RECONFIGURE" != "yes" && "$DRY_RUN" != "yes" ]]; then
    warn "existing config found at $MADDY_CONF"
    if [[ "$ASSUME_YES" != "yes" ]]; then
      local answer; ask answer "Overwrite it? (configs are backed up first) [y/N]" "N"
      [[ "$answer" =~ ^[Yy] ]] || die "aborted by user"
    fi
  fi

  local busy=()
  for port in "$SMTP_PORT" "$SMTPS_PORT" "$SUBMISSION_PORT" "$IMAP_PORT"; do
    if ! port_free "$port"; then
      # our own previous instance may be running - that is fine
      if systemctl is-active --quiet "$MADDY_SERVICE" 2>/dev/null; then :; else busy+=("$port"); fi
    fi
  done
  ((${#busy[@]} == 0)) || warn "ports already in use: ${busy[*]} (stop the other service or choose different ports)"
  [[ "$DRY_RUN" == "yes" ]] || ((${#busy[@]} == 0)) || die "refusing to continue with busy ports"
}

# ------------------------------------------------------------------ packages
ensure_packages() {
  step "Installing system packages"
  local pkgs=(ca-certificates curl openssl dnsutils sqlite3 zstd python3)
  [[ "$ENABLE_NGINX" == "yes" ]] && pkgs+=(nginx)
  [[ "$TLS_MODE" =~ ^(http01|dns-manual|dns-token|dns-aksign)$ ]] && pkgs+=(certbot)
  [[ "$DRY_RUN" == "yes" ]] && { info "would install: ${pkgs[*]}"; return 0; }
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq "${pkgs[@]}" >/dev/null
  info "packages ready: ${pkgs[*]}"
}

# ------------------------------------------------------------------ maddy
ensure_maddy() {
  step "Installing Maddy $MADDY_VERSION"
  if [[ -x "$MADDY_BIN" ]]; then
    info "reusing $MADDY_BIN ($("$MADDY_BIN" version 2>/dev/null | head -1))"
    return 0
  fi
  [[ "$DRY_RUN" == "yes" ]] && { info "would download maddy $MADDY_VERSION"; return 0; }
  local arch="x86_64"
  case "$(uname -m)" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *) die "unsupported architecture: $(uname -m)" ;;
  esac
  local name="maddy-${MADDY_VERSION}-${arch}-linux-musl.tar.zst"
  local base="https://github.com/foxcpp/maddy/releases/download/v${MADDY_VERSION}"
  local tmp; tmp="$(mktemp -d)"
  local url
  for url in \
      "$base/$name" \
      "https://ghproxy.net/$base/$name" \
      "https://mirror.ghproxy.com/$base/$name"; do
    info "downloading $url"
    if curl -fL --retry 2 --connect-timeout 15 --max-time 300 -o "$tmp/maddy.tar.zst" "$url"; then
      break
    fi
    rm -f "$tmp/maddy.tar.zst"
  done
  [[ -f "$tmp/maddy.tar.zst" ]] || die "could not download Maddy; install it manually as $MADDY_BIN"
  tar --use-compress-program=unzstd -xf "$tmp/maddy.tar.zst" -C "$tmp"
  local bin; bin="$(find "$tmp" -type f -name maddy -perm -u+x | head -1)"
  [[ -n "$bin" ]] || die "maddy binary not found in archive"
  install -m 0755 "$bin" "$MADDY_BIN"
  rm -rf "$tmp"
  info "installed $("$MADDY_BIN" version | head -1)"
}

# ------------------------------------------------------------------ user
ensure_user() {
  step "Preparing directories and service account"
  if ! id "$MADDY_USER" >/dev/null 2>&1; then
    [[ "$DRY_RUN" == "yes" ]] && { info "would create user $MADDY_USER"; return 0; }
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$MADDY_USER"
    info "created system user $MADDY_USER"
  else
    info "user $MADDY_USER already exists"
  fi
  [[ "$DRY_RUN" == "yes" ]] && return 0
  install -d -m 0750 -o "$MADDY_USER" -g "$MADDY_USER" "$DATA_DIR" "$DATA_DIR/admin" \
    "$DATA_DIR/dkim_keys" "$RUNTIME_DIR"
  install -d -m 0750 "$CERT_DIR" "$INSTALL_PREFIX" "$INSTALL_PREFIX/admin" "$SECRETS_DIR"
  install -d -m 0755 /var/www/certbot /var/www/landing /var/www/mta-sts/.well-known
  chown -R "$MADDY_USER:$MADDY_USER" "$DATA_DIR" "$SECRETS_DIR" "$INSTALL_PREFIX"
  chmod 0600 "$SECRETS_DIR" 2>/dev/null || true
  info "directories ready"
}

# ------------------------------------------------------------------ secrets
write_secrets() {
  step "Writing administrator credentials"
  if [[ "$ADMIN_PASSWORD" == "auto" ]]; then
    ADMIN_PASSWORD="$(random_password)"
    GENERATED_PASSWORD="yes"
  fi
  [[ ${#ADMIN_PASSWORD} -ge 10 ]] || die "admin password must be at least 10 characters"
  [[ "$DRY_RUN" == "yes" ]] && { info "would write secrets"; return 0; }
  umask 077
  printf 'ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD" > "$SECRETS_DIR/auth.env"
  printf '%s\n' "$ADMIN_PASSWORD" > "$SECRETS_DIR/mailpass"
  if [[ ! -f "$SECRETS_DIR/register.env" ]]; then
    printf 'REGISTER_CODE=%s\nREGISTER_OPEN=false\n' \
      "$(openssl rand -hex 6 | tr 'a-f' 'A-F')" > "$SECRETS_DIR/register.env"
  fi
  chmod 0600 "$SECRETS_DIR"/*.env "$SECRETS_DIR/mailpass"
  chown "$MADDY_USER:$MADDY_USER" "$SECRETS_DIR" "$SECRETS_DIR"/* 2>/dev/null || true
  chmod 0750 "$SECRETS_DIR"
  umask 022
  info "credentials stored in $SECRETS_DIR (0600)"
}

# ------------------------------------------------------------------ certificate
cert_selfsigned() {
  step "Generating self-signed certificate"
  [[ "$DRY_RUN" == "yes" ]] && { info "would generate certificate for $MAIL_HOSTNAME"; return 0; }
  mkdir -p "$CERT_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" \
    -subj "/CN=$MAIL_HOSTNAME" \
    -addext "subjectAltName=DNS:$MAIL_HOSTNAME,DNS:$MAIL_DOMAIN,DNS:mta-sts.$MAIL_DOMAIN" \
    >/dev/null 2>&1
  chmod 600 "$CERT_DIR/privkey.pem"
  chmod 644 "$CERT_DIR/fullchain.pem"
  chown -R "$MADDY_USER:$MADDY_USER" "$CERT_DIR"
  info "self-signed certificate created (clients will warn)"
}

install_deploy_hook() {
  [[ "$DRY_RUN" == "yes" ]] && return 0
  cat > /usr/local/bin/mailstack-deploy-cert <<EOF
#!/bin/bash
# Copy a renewed Let's Encrypt lineage into the mail stack and reload services.
set -euo pipefail
SRC="\${RENEWED_LINEAGE:-/etc/letsencrypt/live/$MAIL_HOSTNAME}"
install -m 644 "\$SRC/fullchain.pem" "$CERT_DIR/fullchain.pem"
install -m 600 "\$SRC/privkey.pem" "$CERT_DIR/privkey.pem"
chown -R $MADDY_USER:$MADDY_USER "$CERT_DIR" 2>/dev/null || true
systemctl reload-or-restart $MADDY_SERVICE 2>/dev/null || true
[[ -d /etc/nginx ]] && nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
echo "mailstack: certificate deployed from \$SRC"
EOF
  chmod 0755 /usr/local/bin/mailstack-deploy-cert
  install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
  ln -sf /usr/local/bin/mailstack-deploy-cert \
    /etc/letsencrypt/renewal-hooks/deploy/mailstack-cert.sh
}

cert_letsencrypt() {
  step "Obtaining Let's Encrypt certificate ($TLS_MODE)"
  local domains=(-d "$MAIL_HOSTNAME" -d "$MAIL_DOMAIN")
  [[ "$ENABLE_NGINX" == "yes" ]] && domains+=(-d "mta-sts.$MAIL_DOMAIN")
  [[ "$DRY_RUN" == "yes" ]] && { info "would request: ${domains[*]}"; return 0; }
  install_deploy_hook
  mkdir -p /run/mailstack

  local -a args=(certonly --cert-name "$MAIL_HOSTNAME" "${domains[@]}"
                 --email "$ACME_EMAIL" --agree-tos --non-interactive --expand
                 --deploy-hook /usr/local/bin/mailstack-deploy-cert)

  case "$TLS_MODE" in
    http01)
      if port_free 80; then
        args+=(--standalone --preferred-challenges http)
        info "port 80 is free: using standalone HTTP-01"
      else
        info "port 80 busy: using webroot /var/www/certbot"
        args+=(--webroot -w /var/www/certbot --preferred-challenges http)
      fi
      ;;
    dns-manual)
      args+=(--manual --preferred-challenges dns
             --manual-auth-hook "$INSTALL_PREFIX/scripts/manual-dns-hook.sh"
             --manual-cleanup-hook "$INSTALL_PREFIX/scripts/manual-dns-cleanup.sh")
      info "you will be asked to add TXT records; the hook prints exact values"
      ;;
    dns-token)
      [[ -n "$DNS_TOKEN" && -n "$DNS_API_BASE" ]] \
        || die "DNS_TOKEN and DNS_API_BASE are required for dns-token mode"
      args+=(--manual --preferred-challenges dns
             --manual-auth-hook "$INSTALL_PREFIX/scripts/acme-token-create.sh"
             --manual-cleanup-hook "$INSTALL_PREFIX/scripts/acme-token-cleanup.sh")
      ;;
    dns-aksign)
      [[ -n "$DNS_ACCESS_KEY" && -n "$DNS_SECRET_KEY" && -n "$DNS_API_HOST" ]] \
        || die "DNS_ACCESS_KEY/DNS_SECRET_KEY/DNS_API_HOST are required for dns-aksign mode"
      args+=(--manual --preferred-challenges dns
             --manual-auth-hook "$INSTALL_PREFIX/scripts/acme-aksign-create.sh"
             --manual-cleanup-hook "$INSTALL_PREFIX/scripts/acme-aksign-cleanup.sh")
      ;;
  esac

  certbot "${args[@]}"
  # belt and braces: copy the lineage even when the hook did not run
  if [[ -d "/etc/letsencrypt/live/$MAIL_HOSTNAME" ]]; then
    install -m 644 "/etc/letsencrypt/live/$MAIL_HOSTNAME/fullchain.pem" "$CERT_DIR/fullchain.pem"
    install -m 600 "/etc/letsencrypt/live/$MAIL_HOSTNAME/privkey.pem" "$CERT_DIR/privkey.pem"
    chown -R "$MADDY_USER:$MADDY_USER" "$CERT_DIR"
  fi
  info "certificate installed in $CERT_DIR"
}

obtain_certificate() {
  case "$TLS_MODE" in
    selfsigned) cert_selfsigned ;;
    *) cert_letsencrypt ;;
  esac
}

# ------------------------------------------------------------------ maddy config
install_maddy() {
  step "Rendering Maddy configuration"
  backup_path "$MADDY_CONF"
  render "$ROOT/templates/maddy.conf.tmpl" "$MADDY_CONF" 644
  [[ "$DRY_RUN" == "yes" ]] && return 0
  if ! "$MADDY_BIN" --config "$MADDY_CONF" verify-config >/tmp/maddy-verify.log 2>&1; then
    tail -20 /tmp/maddy-verify.log >&2
    die "maddy configuration is invalid"
  fi
  info "maddy config verified"

  render "$ROOT/templates/maddy.service.tmpl" "/etc/systemd/system/$MADDY_SERVICE.service" 644
  systemctl daemon-reload
  systemctl enable "$MADDY_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$MADDY_SERVICE"
  local tries=0
  until systemctl is-active --quiet "$MADDY_SERVICE"; do
    ((tries++)) || true
    [[ $tries -gt 20 ]] && { journalctl -u "$MADDY_SERVICE" -n 30 --no-pager >&2; die "$MADDY_SERVICE failed to start"; }
    sleep 1
  done
  info "$MADDY_SERVICE is active"
}

create_admin_mailbox() {
  step "Creating administrator mailbox $ADMIN_EMAIL"
  [[ "$DRY_RUN" == "yes" ]] && { info "would create $ADMIN_EMAIL"; return 0; }
  local out
  if "$MADDY_BIN" --config "$MADDY_CONF" creds list 2>/dev/null | grep -qx "$ADMIN_EMAIL"; then
    out="$("$MADDY_BIN" --config "$MADDY_CONF" creds password "$ADMIN_EMAIL" <<<"$ADMIN_PASSWORD" 2>&1 || true)"
    info "existing mailbox, password synchronised"
  else
    out="$(printf '%s\n' "$ADMIN_PASSWORD" | "$MADDY_BIN" --config "$MADDY_CONF" creds create "$ADMIN_EMAIL" 2>&1 || true)"
    info "credentials created"
  fi
  if ! "$MADDY_BIN" --config "$MADDY_CONF" imap-acct list 2>/dev/null | grep -qx "$ADMIN_EMAIL"; then
    "$MADDY_BIN" --config "$MADDY_CONF" imap-acct create "$ADMIN_EMAIL" >/dev/null 2>&1 || true
    info "mailbox storage created"
  fi
  printf '%s' "$out" | grep -qiE "error|fail" && warn "maddy said: $(printf '%s' "$out" | head -2 | tr '\n' ' ')"
  # maddy CLI runs as root here and creates sqlite/dkim files root-owned
  chown -R "$MADDY_USER:$MADDY_USER" "$DATA_DIR"
  return 0
}

generate_dkim() {
  step "Waiting for DKIM key"
  [[ "$DRY_RUN" == "yes" ]] && return 0
  local key="$DATA_DIR/dkim_keys/${MAIL_DOMAIN}_default.dns"
  for _ in $(seq 1 20); do
    [[ -s "$key" ]] && { info "DKIM key present: $key"; return 0; }
    sleep 1
  done
  warn "DKIM key not generated yet - it appears after the first outgoing message"
}

# ------------------------------------------------------------------ web
install_web() {
  [[ "$ENABLE_WEB" == "yes" ]] || { info "web console disabled"; return 0; }
  step "Installing web console"
  [[ "$DRY_RUN" == "yes" ]] && { info "would install console on 127.0.0.1:$WEB_PORT"; return 0; }
  install -d -m 0755 "$INSTALL_PREFIX/admin" "$INSTALL_PREFIX/scripts"
  install -m 0755 "$ROOT/admin/mailadmin.py" "$INSTALL_PREFIX/admin/mailadmin.py"
  install -m 0755 "$ROOT/scripts/"*.py "$INSTALL_PREFIX/scripts/" 2>/dev/null || true
  install -m 0755 "$ROOT/scripts/"*.sh "$INSTALL_PREFIX/scripts/" 2>/dev/null || true

  render "$ROOT/templates/config.json.tmpl" "$STATE_DIR/config.json" 640
  chown "$MADDY_USER:$MADDY_USER" "$STATE_DIR/config.json"
  render "$ROOT/templates/maddy-admin.service.tmpl" \
    "/etc/systemd/system/$ADMIN_SERVICE.service" 644

  systemctl daemon-reload
  systemctl enable "$ADMIN_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$ADMIN_SERVICE"
  local tries=0
  until systemctl is-active --quiet "$ADMIN_SERVICE"; do
    ((tries++)) || true
    [[ $tries -gt 20 ]] && { journalctl -u "$ADMIN_SERVICE" -n 30 --no-pager >&2; die "$ADMIN_SERVICE failed to start"; }
    sleep 1
  done
  info "$ADMIN_SERVICE is active on 127.0.0.1:$WEB_PORT"
}

install_nginx() {
  [[ "$ENABLE_NGINX" == "yes" ]] || { info "nginx disabled"; return 0; }
  step "Configuring nginx"
  [[ "$DRY_RUN" == "yes" ]] && { info "would configure nginx"; return 0; }
  render "$ROOT/templates/nginx-snippet.conf.tmpl" \
    /etc/nginx/snippets/mailstack-locations.conf 644
  backup_path /etc/nginx/sites-available/mailstack
  render "$ROOT/templates/nginx-site.conf.tmpl" /etc/nginx/sites-available/mailstack 644
  ln -sf /etc/nginx/sites-available/mailstack /etc/nginx/sites-enabled/mailstack
  rm -f /etc/nginx/sites-enabled/default

  if ! nginx -t >/tmp/nginx-test.log 2>&1; then
    tail -20 /tmp/nginx-test.log >&2
    die "nginx configuration is invalid"
  fi
  systemctl enable nginx >/dev/null 2>&1 || true
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
  info "nginx reloaded"
}

install_mta_sts() {
  [[ "$ENABLE_NGINX" == "yes" ]] || return 0
  step "Publishing MTA-STS policy"
  [[ "$DRY_RUN" == "yes" ]] && return 0
  install -d -m 0755 /var/www/mta-sts/.well-known
  cat > /var/www/mta-sts/.well-known/mta-sts.txt <<EOF
version: STSv1
mode: enforce
mx: $MAIL_HOSTNAME
max_age: 604800
EOF
  chmod 644 /var/www/mta-sts/.well-known/mta-sts.txt
  info "policy written to /var/www/mta-sts/.well-known/mta-sts.txt"
}

install_landing() {
  [[ "$ENABLE_LANDING" == "yes" && "$ENABLE_NGINX" == "yes" ]] || return 0
  step "Publishing landing page"
  [[ "$DRY_RUN" == "yes" ]] && return 0
  render "$ROOT/templates/landing.html.tmpl" /var/www/landing/index.html 644
  printf '%s\n' "$MAIL_DOMAIN" > /var/www/landing/.mailstack-owner
  printf '%s\n' "$MAIL_DOMAIN" > /var/www/mta-sts/.mailstack-owner
  info "landing page installed at $PUBLIC_URL/"
}

# ------------------------------------------------------------------ reporting
print_dns() {
  step "DNS records you must create"
  [[ "$DRY_RUN" == "yes" ]] && { info "would print DNS records"; return 0; }
  MAILSTACK_INSTALL_CONF="$STATE_DIR/install.conf" \
  MAILSTACK_CONFIG="$STATE_DIR/config.json" \
    python3 "$ROOT/scripts/show-dns-records.py" || true
}

run_selfcheck() {
  step "Self-check"
  [[ "$DRY_RUN" == "yes" ]] && return 0
  MAILSTACK_CONFIG="$STATE_DIR/config.json" MADDY_BIN="$MADDY_BIN" \
    python3 "$ROOT/scripts/selfcheck.py" || warn "self-check reported problems (see above)"
}

save_install_conf() {
  [[ "$DRY_RUN" == "yes" ]] && return 0
  umask 077
  cat > "$STATE_DIR/install.conf" <<EOF
# generated by mailstack installer $VERSION on $(date -u +%Y-%m-%dT%H:%M:%SZ)
MAIL_DOMAIN=$MAIL_DOMAIN
MAIL_HOSTNAME=$MAIL_HOSTNAME
SERVER_IP=$SERVER_IP
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=auto
TLS_MODE=$TLS_MODE
ACME_EMAIL=$ACME_EMAIL
DNS_TOKEN=$DNS_TOKEN
DNS_API_BASE=$DNS_API_BASE
DNS_ACCESS_KEY=$DNS_ACCESS_KEY
DNS_SECRET_KEY=$DNS_SECRET_KEY
DNS_ZONE_ID=$DNS_ZONE_ID
DNS_API_HOST=$DNS_API_HOST
DNS_API_REGION=$DNS_API_REGION
DNS_API_SERVICE=$DNS_API_SERVICE
ENABLE_WEB=$ENABLE_WEB
ENABLE_LANDING=$ENABLE_LANDING
WEB_PORT=$WEB_PORT
INSTALL_PREFIX=$INSTALL_PREFIX
STATE_DIR=$STATE_DIR
SECRETS_DIR=$SECRETS_DIR
DATA_DIR=$DATA_DIR
MADDY_CONF=$MADDY_CONF
MADDY_BIN=$MADDY_BIN
MADDY_VERSION=$MADDY_VERSION
MADDY_USER=$MADDY_USER
MADDY_SERVICE=$MADDY_SERVICE
ADMIN_SERVICE=$ADMIN_SERVICE
ENABLE_NGINX=$ENABLE_NGINX
PROFILE=$PROFILE
CERT_DIR=$CERT_DIR
RUNTIME_DIR=$RUNTIME_DIR
PUBLIC_URL=$PUBLIC_URL
SMTP_PORT=$SMTP_PORT
SMTPS_PORT=$SMTPS_PORT
SUBMISSION_PORT=$SUBMISSION_PORT
IMAP_PORT=$IMAP_PORT
EOF
  chmod 600 "$STATE_DIR/install.conf"
  umask 022
}

summary() {
  local web_url="https://$MAIL_HOSTNAME/admin/login"
  [[ "$ENABLE_NGINX" != "yes" ]] && web_url="http://127.0.0.1:$WEB_PORT/admin/login"
  cat <<EOF

${C_B}Installation complete${C_RESET}

  Mail domain    : $MAIL_DOMAIN
  Mail hostname  : $MAIL_HOSTNAME  (A record -> $SERVER_IP)
  SMTP           : $SMTP_PORT / $SUBMISSION_PORT (STARTTLS) / $SMTPS_PORT (TLS)
  IMAP           : $IMAP_PORT (TLS)
  Web console    : $web_url
  Admin account  : $ADMIN_EMAIL
EOF
  if [[ "${GENERATED_PASSWORD:-no}" == "yes" ]]; then
    cat <<EOF
  Admin password : ${C_B}$ADMIN_PASSWORD${C_RESET}
                   (shown once - store it in your password manager)
EOF
  fi
  cat <<EOF

  Next steps
    1. Create the DNS records printed above, then wait for propagation.
    2. Verify:  sudo $INSTALL_PREFIX/bin/mailctl check
    3. Open the web console, create mailboxes under "Accounts".
    4. Point your mail client at $MAIL_HOSTNAME with your full address as login.

  Operations
    mailctl status | check | dns | logs | backup | cert renew
    Re-run this installer with --reconfigure to change settings.
EOF
  [[ "$TLS_MODE" == "selfsigned" ]] && warn "self-signed certificate in use - mail clients will show a warning"
  [[ "$TLS_MODE" == "dns-manual" ]] && warn "DNS-01 manual mode: renewals need 'mailctl cert renew' every 90 days"
  return 0
}

# ------------------------------------------------------------------ main
main() {
  log "${C_B}mailstack installer $VERSION${C_RESET}  ${C_DIM}(profile: $PROFILE)${C_RESET}"
  if [[ "$RECONFIGURE" != "yes" && "$ASSUME_YES" != "yes" && -z "$MAIL_DOMAIN" ]] && is_tty; then
    wizard
  fi
  derive
  if [[ "$RECONFIGURE" != "yes" && "$ASSUME_YES" != "yes" ]] && is_tty; then
    local ok; ask ok "Proceed with these settings? [Y/n]" "Y"
    [[ "$ok" =~ ^[Nn] ]] && die "aborted by user"
  fi
  preflight
  [[ "$DRY_RUN" == "yes" ]] || { install -d -m 0755 "$STATE_DIR"; save_install_conf; }

  ensure_packages
  ensure_maddy
  ensure_user
  write_secrets
  obtain_certificate
  install_maddy
  create_admin_mailbox
  generate_dkim
  install_web
  install_nginx
  install_mta_sts
  install_landing
  if [[ "$DRY_RUN" != "yes" ]]; then
    install -d -m 0755 "$INSTALL_PREFIX/bin"
    install -m 0755 "$ROOT/bin/mailctl" "$INSTALL_PREFIX/bin/mailctl" 2>/dev/null || true
  fi
  print_dns
  run_selfcheck
  summary
}

main "$@"
