#!/usr/bin/env python3
"""Post-install / on-demand health check for the mail stack.

    python3 selfcheck.py [--json] [--no-dns]

Exit code 0 when every non-optional check passes.
"""
import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import imaplib
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = os.environ.get("MAILSTACK_CONFIG", "/etc/mailstack/config.json")


def load_cfg(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def run(args, timeout=12):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (res.stdout + res.stderr).strip(), res.returncode
    except Exception as exc:
        return "ERR: %s" % exc, 1


def tcp(host, port, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def cert_info(path):
    out, rc = run(["openssl", "x509", "-in", path, "-noout", "-enddate", "-subject"])
    if rc != 0:
        return None, None
    end = ""
    for line in out.splitlines():
        if line.startswith("notAfter="):
            end = line.split("=", 1)[1].strip()
    try:
        exp = datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (exp - datetime.now(timezone.utc)).days
    except Exception:
        days = None
    return end, days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-dns", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    home = cfg.get("mail_hostname", "localhost")
    domain = cfg.get("domain", "")
    data_dir = Path(cfg.get("data_dir", "/var/lib/maddy"))
    secrets = Path(cfg.get("secrets_dir", "/etc/maddy-admin"))
    cert = Path(cfg.get("cert_dir", "/etc/maddy/certs/%s" % home)) / "fullchain.pem"
    admin_email = cfg.get("admin_email", "")
    checks = []

    def add(name, ok, detail="", optional=False):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200],
                       "optional": optional})

    for service in (cfg.get("maddy_service", "maddy"), cfg.get("nginx_service", "nginx"),
                    cfg.get("admin_service", "maddy-admin")):
        if not service:
            continue
        if service == "nginx" and not Path("/etc/nginx").exists():
            continue
        out, _ = run(["systemctl", "is-active", service])
        add("service %s" % service, out.strip() == "active", out.strip())

    for label, port in (("smtp", cfg.get("smtp_port", 25)),
                        ("submission", cfg.get("submission_port", 587)),
                        ("smtps", cfg.get("smtps_port", 465)),
                        ("imap", cfg.get("imap_port", 993))):
        add("listen %s/%s" % (label, port), tcp("127.0.0.1", int(port)), "tcp connect")

    end, days = cert_info(str(cert)) if cert.exists() else (None, None)
    add("tls certificate", bool(end), end or "missing: %s" % cert)
    if days is not None:
        add("certificate validity", days >= 7 if "letsencrypt" in str(cert) else days >= 0,
            "%s day(s) left" % days, optional=True)

    dkim = data_dir / "dkim_keys" / ("%s_default.dns" % domain)
    add("dkim key", dkim.exists(), str(dkim))

    pw_file = secrets / "mailpass"
    if admin_email and pw_file.exists():
        password = pw_file.read_text(encoding="utf-8").strip()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            m = imaplib.IMAP4_SSL("127.0.0.1", int(cfg.get("imap_port", 993)),
                                  ssl_context=ctx, timeout=10)
            m.login(admin_email, password)
            m.logout()
            add("admin imap login", True, admin_email)
        except Exception as exc:
            add("admin imap login", False, "%s: %s" % (admin_email, exc))

    port = int(cfg.get("port", 8080))
    try:
        import urllib.request
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request("http://127.0.0.1:%d/admin/login" % port)
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
        add("web console", resp.status == 200 and "<form" in body.lower(),
            "HTTP %s" % resp.status)
    except Exception as exc:
        add("web console", False, str(exc))

    if not args.no_dns and domain:
        out, _ = run(["dig", "+short", "+time=3", "MX", domain])
        add("dns MX", home in out, out.replace("\n", " "), optional=True)
        out, _ = run(["dig", "+short", "+time=3", "TXT", domain])
        add("dns SPF", "v=spf1" in out, out.replace("\n", " ")[:90], optional=True)

    failed = [c for c in checks if not c["ok"] and not c["optional"]]
    if args.json:
        print(json.dumps({"ok": not failed, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print("\n%-26s %-5s %s" % ("CHECK", "OK", "DETAIL"))
        print("-" * 74)
        for c in checks:
            mark = "yes" if c["ok"] else ("warn" if c["optional"] else "NO")
            print("%-26s %-5s %s" % (c["name"], mark, c["detail"]))
        print()
        print("PASS" if not failed else "FAILED: %d check(s)" % len(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
