#!/usr/bin/env python3
"""Fail when a template uses a placeholder install.sh does not render."""
import pathlib
import re
import sys

KNOWN = {
    "DOMAIN", "HOSTNAME", "SERVER_IP", "ADMIN_EMAIL", "ADMIN_LOCAL", "DATA_DIR",
    "RUNTIME_DIR", "RUNTIME_NAME", "STATE_NAME", "CERT_DIR", "SECRETS_DIR",
    "MADDY_CONF", "MADDY_BIN", "MADDY_USER", "MADDY_SERVICE", "ADMIN_SERVICE",
    "CERTBOT_TIMER", "ADMIN_PY", "CONFIG_JSON", "ADMIN_LISTEN", "ADMIN_PORT",
    "PUBLIC_URL", "SMTP_PORT", "SMTPS_PORT", "SUBMISSION_PORT", "IMAP_PORT",
    "BRAND", "DOMAIN_INITIAL", "RENDERED_AT", "COOKIE_SECURE",
}

root = pathlib.Path(__file__).resolve().parents[1] / "templates"
unknown = []
for path in sorted(root.glob("*.tmpl")):
    for token in re.findall(r"@@([A-Z_]+)@@", path.read_text(encoding="utf-8")):
        if token not in KNOWN:
            unknown.append("%s: %s" % (path.name, token))

if unknown:
    print("  FAIL unknown template tokens: " + ", ".join(sorted(set(unknown))))
    sys.exit(1)
print("  ok  every template token is rendered by install.sh")
