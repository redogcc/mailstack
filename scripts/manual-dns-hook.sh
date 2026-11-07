#!/usr/bin/env bash
# Certbot DNS-01 auth hook for the fully manual flow.
# Prints the TXT record and waits for the operator to confirm.
set -Eeuo pipefail
domain="${CERTBOT_DOMAIN:?CERTBOT_DOMAIN missing}"
value="${CERTBOT_VALIDATION:?CERTBOT_VALIDATION missing}"
name="_acme-challenge.${domain}"

cat >&2 <<EOF

────────────────────────────────────────────────────────────────
 Add this DNS record at your provider, then press Enter:

   Type : TXT
   Name : ${name}
   Value: ${value}
   TTL  : 60 (or the smallest value allowed)

 Verify with:
   dig +short TXT ${name} @8.8.8.8
────────────────────────────────────────────────────────────────

EOF
read -r -p "Press Enter once the record is visible... " _ < /dev/tty || true
sleep 5
