#!/usr/bin/env bash
# Certbot DNS-01 cleanup hook for the manual flow: records are left in place,
# the operator removes them (they are harmless and reused on renewal).
set -Eeuo pipefail
echo "manual mode: remember to remove the _acme-challenge TXT record later" >&2
exit 0
