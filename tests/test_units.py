#!/usr/bin/env python3
"""Offline unit tests: templates, config plumbing, DNS helper, admin localisation.

Run with:  python3 tests/test_units.py
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def render(template, values):
    text = (ROOT / "templates" / template).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("@@%s@@" % key, str(value))
    return text


SAMPLE = {
    "DOMAIN": "acme.example", "HOSTNAME": "mail.acme.example", "SERVER_IP": "203.0.113.7",
    "ADMIN_EMAIL": "admin@acme.example", "ADMIN_LOCAL": "admin", "DATA_DIR": "/srv/mail",
    "RUNTIME_DIR": "/run/mail", "RUNTIME_NAME": "mail", "STATE_NAME": "mail",
    "CERT_DIR": "/etc/maddy/certs/mail.acme.example", "SECRETS_DIR": "/etc/mail-secrets",
    "MADDY_CONF": "/etc/maddy/maddy.conf", "MADDY_BIN": "/usr/local/bin/maddy",
    "MADDY_USER": "maddy", "MADDY_SERVICE": "maddy", "ADMIN_SERVICE": "maddy-admin",
    "CERTBOT_TIMER": "certbot.timer", "ADMIN_PY": "/opt/mailstack/admin/mailadmin.py",
    "CONFIG_JSON": "/etc/mailstack/config.json", "ADMIN_LISTEN": "127.0.0.1",
    "ADMIN_PORT": 8080, "PUBLIC_URL": "https://mail.acme.example",
    "SMTP_PORT": 25, "SMTPS_PORT": 465, "SUBMISSION_PORT": 587, "IMAP_PORT": 993,
    "BRAND": "acme.example", "DOMAIN_INITIAL": "a", "RENDERED_AT": "2026-01-01T00:00:00Z",
    "COOKIE_SECURE": "true",
}


class TemplateTests(unittest.TestCase):
    def test_no_unreplaced_tokens(self):
        for tpl in (ROOT / "templates").iterdir():
            if not tpl.name.endswith(".tmpl"):
                continue
            text = render(tpl.name, SAMPLE)
            leftover = re.findall(r"@@[A-Z_]+@@", text)
            self.assertEqual(leftover, [], "%s still has %s" % (tpl.name, leftover))

    def test_maddy_conf_essentials(self):
        text = render("maddy.conf.tmpl", SAMPLE)
        self.assertIn("state_dir /srv/mail", text)
        self.assertIn("$(primary_domain) = acme.example", text)
        self.assertIn("tcp://0.0.0.0:25", text)
        self.assertIn("tls://0.0.0.0:465", text)
        self.assertIn("imap tls://0.0.0.0:993", text)
        self.assertIn("dmarc yes", text)
        self.assertIn("dkim $(primary_domain)", text)

    def test_nginx_config_ports(self):
        text = render("nginx-site.conf.tmpl", SAMPLE)
        self.assertIn("listen 443 ssl http2;", text)
        self.assertIn("server_name mail.acme.example acme.example mta-sts.acme.example;", text)
        self.assertIn("/etc/maddy/certs/mail.acme.example/fullchain.pem", text)

    def test_config_json_valid(self):
        text = render("config.json.tmpl", SAMPLE)
        cfg = json.loads(text)
        self.assertEqual(cfg["domain"], "acme.example")
        self.assertEqual(cfg["port"], 8080)
        self.assertIs(cfg["cookie_secure"], True)

    def test_systemd_units(self):
        maddy = render("maddy.service.tmpl", SAMPLE)
        self.assertIn("User=maddy", maddy)
        self.assertIn("ExecStart=/usr/local/bin/maddy --config /etc/maddy/maddy.conf run", maddy)
        admin = render("maddy-admin.service.tmpl", SAMPLE)
        self.assertIn("MAILSTACK_CONFIG=/etc/mailstack/config.json", admin)
        self.assertIn("ReadWritePaths=/srv/mail /run/mail /etc/mail-secrets", admin)

    def test_landing_page(self):
        text = render("landing.html.tmpl", SAMPLE)
        self.assertIn("acme.example", text)
        self.assertIn("/admin/login", text)
        self.assertNotIn("example.com", text)


class DnsHelperTests(unittest.TestCase):
    def test_records(self):
        import importlib
        mod = importlib.import_module("show-dns-records".replace("-", "_")) \
            if False else None  # module name has a dash; exec instead
        path = ROOT / "scripts" / "show-dns-records.py"
        spec = importlib.util.spec_from_file_location("dnsrec", path)
        dnsrec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dnsrec)

        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "install.conf"
            conf.write_text("MAIL_DOMAIN=acme.example\nMAIL_HOSTNAME=mail.acme.example\n"
                            "SERVER_IP=203.0.113.7\n", encoding="utf-8")
            domain, host, records = dnsrec.build(dnsrec.read_conf(str(conf)), "")
            self.assertEqual(domain, "acme.example")
            self.assertEqual(host, "mail.acme.example")
            types = [r[0] for r in records]
            self.assertEqual(types.count("TXT"), 5)
            self.assertIn("MX", types)
            spf = [r for r in records if r[3].startswith("SPF")][0]
            self.assertEqual(spf[2], "v=spf1 mx -all")


class AdminConfigTests(unittest.TestCase):
    def _load(self, **overrides):
        tmp = tempfile.mkdtemp()
        secrets = Path(tmp) / "secrets"
        secrets.mkdir()
        (secrets / "auth.env").write_text("ADMIN_PASSWORD=TestPass!123\n", encoding="utf-8")
        (secrets / "mailpass").write_text("TestPass!123\n", encoding="utf-8")
        (secrets / "register.env").write_text("REGISTER_CODE=ABC\n", encoding="utf-8")
        cfg = {
            "domain": "acme.example", "mail_hostname": "mail.acme.example",
            "server_ip": "203.0.113.7", "admin_email": "postmaster@acme.example",
            "data_dir": str(Path(tmp) / "data"), "state_dir": str(Path(tmp) / "data" / "admin"),
            "secrets_dir": str(secrets), "maddy_conf": str(Path(tmp) / "maddy.conf"),
            "cert_dir": str(Path(tmp) / "certs"), "mta_sts_file": str(Path(tmp) / "sts.txt"),
            "listen": "127.0.0.1", "port": 18099, "imap_port": 1993, "smtps_port": 1465,
            "smtp_port": 125, "submission_port": 1587, "maddy_service": "maddy-test",
            "admin_service": "maddy-admin-test", "cookie_secure": False,
        }
        cfg.update(overrides)
        conf = Path(tmp) / "config.json"
        conf.write_text(json.dumps(cfg), encoding="utf-8")
        os.environ["MAILSTACK_CONFIG"] = str(conf)
        spec = importlib.util.spec_from_file_location(
            "mailadmin_under_test", ROOT / "admin" / "mailadmin.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_values_wired_through(self):
        mod = self._load()
        self.assertEqual(mod.DOMAIN, "acme.example")
        self.assertEqual(mod.MAIL_HOSTNAME, "mail.acme.example")
        self.assertEqual(mod.ADMIN_MAIL, "postmaster@acme.example")
        self.assertEqual(mod.PORT, 18099)
        self.assertEqual(mod.IMAP_PORT, 1993)
        self.assertEqual(mod.MADDY_SERVICE, "maddy-test")
        self.assertFalse(mod.COOKIE_SECURE)
        self.assertIn("HttpOnly", mod.COOKIE_FLAGS)
        self.assertNotIn("Secure", mod.COOKIE_FLAGS)

    def test_email_regex_scoped_to_domain(self):
        mod = self._load()
        self.assertTrue(mod.EMAIL_RE.match("alice@acme.example"))
        self.assertFalse(mod.EMAIL_RE.match("alice@other.example"))
        self.assertFalse(mod.EMAIL_RE.match("alice@acme.example.evil"))

    def test_page_localised(self):
        mod = self._load()
        page = mod.localize(mod.PAGE)
        self.assertNotIn("example.com", page)
        self.assertNotIn("203.0.113.10", page)
        self.assertIn("acme.example", page)
        self.assertIn("postmaster@acme.example", page)

    def test_activation_base(self):
        mod = self._load(public_url="https://mail.acme.example")
        self.assertTrue(mod.ACTIVATE_BASE.startswith("https://mail.acme.example/admin/activate"))
        self.assertIn("acme.example", mod.IP_ACCESS)


class SelfcheckTests(unittest.TestCase):
    def test_missing_config_is_safe(self):
        path = ROOT / "scripts" / "selfcheck.py"
        spec = importlib.util.spec_from_file_location("selfcheck", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.load_cfg("/nonexistent/config.json"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
