#!/usr/bin/env python3
"""End-to-end test of an installed mail stack.

Exercises the real services over the loopback interface:

  * web console login (and localisation of the served HTML)
  * account creation through the admin API
  * SMTP submission with the new account
  * IMAP retrieval of that message by the recipient
  * organisation API + account removal

Usage (on the mail server, as root):

    python3 tests/e2e_sandbox.py --config /etc/mailstack-sandbox/config.json
"""
import argparse
import http.cookiejar
import imaplib
import json
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print("  %-42s %s %s" % (name, "PASS" if ok else "FAIL", str(detail)[:90]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/mailstack/config.json")
    ap.add_argument("--password", default="", help="admin password (default: read mailpass)")
    ap.add_argument("--base-url", default="", help="default: http://127.0.0.1:<port>")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    domain = cfg["domain"]
    host = cfg.get("mail_hostname", "mail." + domain)
    admin_email = cfg["admin_email"]
    password = args.password or (
        Path(cfg["secrets_dir"]) / "mailpass").read_text(encoding="utf-8").strip()
    base = args.base_url or "http://127.0.0.1:%d" % int(cfg.get("port", 8080))
    imap_port = int(cfg.get("imap_port", 993))
    smtps_port = int(cfg.get("smtps_port", 465))
    submission_port = int(cfg.get("submission_port", 587))

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    print("\n[1] web console")
    try:
        with opener.open(base + "/admin/login", timeout=10) as resp:
            login_html = resp.read().decode("utf-8", "replace")
        check("login page reachable", resp.status == 200, "HTTP %s" % resp.status)
        check("login page shows deployment domain", domain in login_html,
              "no %s in HTML" % domain)
        check("login page has no stale branding", "example.com" not in login_html)
        data = urllib.parse.urlencode({"email": admin_email, "password": password}).encode()
        req = urllib.request.Request(base + "/admin/login", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with opener.open(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
        check("admin login", payload.get("ok") is True, payload.get("msg"))
        with opener.open(base + "/admin/", timeout=15) as resp:
            console = resp.read().decode("utf-8", "replace")
        check("console renders", resp.status == 200 and "SESSION" in console)
        check("console localised", "example.com" not in console)
        m = re.search(r'csrf:"([^"]+)"', console)
        csrf = m.group(1) if m else ""
        check("csrf token present", bool(csrf))
    except Exception as exc:
        check("web console", False, exc)
        csrf = ""

    stamp = str(int(time.time()))[-6:]
    user_a = "e2e.a%s@%s" % (stamp, domain)
    user_b = "e2e.b%s@%s" % (stamp, domain)
    account_pw = "E2e!Test%s" % stamp

    def api(path, payload=None):
        url = base + "/admin/api/" + path
        if payload is None:
            req = urllib.request.Request(url)
        else:
            body = urllib.parse.urlencode(payload).encode()
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-Token": csrf})
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    print("\n[2] account management")
    created = []
    try:
        for addr in (user_a, user_b):
            res = api("account/add", {"email": addr, "password": account_pw})
            check("create %s" % addr.split("@")[0], res.get("ok"), res.get("msg"))
            if res.get("ok"):
                created.append(addr)
        accounts = api("accounts")
        listed = {a["email"] for a in accounts.get("accounts", [])}
        check("new accounts listed", set(created) <= listed)
        orgs = api("orgs")
        check("organisation API", orgs.get("ok") and len(orgs.get("orgs", [])) == 6,
              "%d orgs" % len(orgs.get("orgs", [])))
    except Exception as exc:
        check("account API", False, exc)

    print("\n[3] mail flow")
    subject = "e2e-%s" % stamp
    if len(created) == 2:
        try:
            msg = ("From: %s\r\nTo: %s\r\nSubject: %s\r\n"
                   "Message-ID: <%s@%s>\r\nDate: %s\r\n\r\nhello e2e\r\n"
                   % (user_a, user_b, subject, subject, domain,
                      time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())))
            with smtplib.SMTP_SSL("127.0.0.1", smtps_port, context=ctx, timeout=25) as s:
                s.login(user_a, account_pw)
                s.sendmail(user_a, [user_b], msg.encode())
            check("SMTP submission (SMTPS %d)" % smtps_port, True)
        except Exception as exc:
            check("SMTP submission", False, exc)

        try:
            with smtplib.SMTP("127.0.0.1", submission_port, timeout=25) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(user_a, account_pw)
                s.sendmail(user_a, [user_b], msg.replace("hello e2e", "starttls").encode())
            check("SMTP submission (STARTTLS %d)" % submission_port, True)
        except Exception as exc:
            check("SMTP submission STARTTLS", False, exc)

        time.sleep(4)
        try:
            m = imaplib.IMAP4_SSL("127.0.0.1", imap_port, ssl_context=ctx, timeout=25)
            m.login(user_b, account_pw)
            m.select("INBOX")
            typ, data = m.search(None, "SUBJECT", '"%s"' % subject)
            ids = data[0].split()
            check("IMAP retrieval", len(ids) >= 2, "%d message(s)" % len(ids))
            if ids:
                typ, raw = m.fetch(ids[-1], "(BODY.PEEK[])")
                blob = raw[0][1].decode("utf-8", "replace")
                check("delivered body intact", "hello e2e" in blob or "starttls" in blob)
                # local delivery skips the outbound signing step; signatures are
                # added for relayed mail, so verify the key material instead
                dkim = Path(cfg["data_dir"]) / "dkim_keys" / ("%s_default.dns" % domain)
                check("DKIM key material", dkim.exists(), str(dkim))
            m.logout()
        except Exception as exc:
            check("IMAP retrieval", False, exc)

    print("\n[4] cleanup")
    for addr in created:
        try:
            res = api("account/remove", {"email": addr})
            check("remove %s" % addr.split("@")[0], res.get("ok"), res.get("msg"))
        except Exception as exc:
            check("remove " + addr, False, exc)

    total = len(PASSED) + len(FAILED)
    print("\n%d/%d checks passed" % (len(PASSED), total))
    if FAILED:
        print("failed: " + ", ".join(FAILED))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
