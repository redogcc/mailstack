#!/usr/bin/env python3
"""Print every DNS record this deployment needs, in copy-paste or JSON form.

    ./scripts/show-dns-records.py                     # human readable
    ./scripts/show-dns-records.py --json              # machine readable
    ./scripts/show-dns-records.py --zone-file z.txt   # BIND zone snippet
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path


def read_conf(path):
    conf = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            conf[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return conf


def dkim_value(data_dir, domain):
    for name in ("%s_default.dns" % domain, "default.dns"):
        path = Path(data_dir) / "dkim_keys" / name
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


def build(conf, config_json):
    cfg = {}
    if config_json and Path(config_json).exists():
        cfg = json.loads(Path(config_json).read_text(encoding="utf-8"))
    domain = conf.get("MAIL_DOMAIN") or cfg.get("domain") or "example.com"
    host = conf.get("MAIL_HOSTNAME") or cfg.get("mail_hostname") or "mail." + domain
    ip = conf.get("SERVER_IP") or cfg.get("server_ip") or "<SERVER_IP>"
    data_dir = conf.get("DATA_DIR") or cfg.get("data_dir") or "/var/lib/maddy"
    admin = conf.get("ADMIN_EMAIL") or cfg.get("admin_email") or "admin@" + domain
    sp = conf.get("SUBMISSION_PORT") or str(cfg.get("submission_port") or 587)
    imaps = conf.get("IMAP_PORT") or str(cfg.get("imap_port") or 993)
    dkim = dkim_value(data_dir, domain) or "v=DKIM1; k=rsa; p=<run maddy once to generate>"
    sts_id = re.sub(r"\D", "", conf.get("MTA_STS_ID", "")) or "1"

    return domain, host, [
        ("A", host, ip, "邮件服务器地址"),
        ("MX", "@", "10 " + host, "收信路由"),
        ("TXT", "@", "v=spf1 mx -all", "SPF：只允许本机发信"),
        ("TXT", "default._domainkey", dkim, "DKIM 公钥（maddy 生成）"),
        ("TXT", "_dmarc", "v=DMARC1; p=quarantine; rua=mailto:postmaster@%s; adkim=s; aspf=s" % domain,
         "DMARC 隔离策略"),
        ("TXT", "_mta-sts", "v=STSv1; id=%s" % sts_id, "MTA-STS 策略版本"),
        ("TXT", "_smtp._tls", "v=TLSRPTv1; rua=mailto:postmaster@%s" % domain, "TLS 报告"),
        ("CNAME", "mta-sts", host, "MTA-STS 策略主机"),
        ("SRV", "_submission._tcp", "0 1 %s %s" % (sp, host), "客户端自动发现（提交）"),
        ("SRV", "_imaps._tcp", "0 1 %s %s" % (imaps, host), "客户端自动发现（IMAP）"),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", default=os.environ.get("MAILSTACK_INSTALL_CONF",
                                                    "/etc/mailstack/install.conf"))
    ap.add_argument("--config-json", default=os.environ.get("MAILSTACK_CONFIG",
                                                           "/etc/mailstack/config.json"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--zone-file")
    args = ap.parse_args()

    conf = read_conf(args.conf)
    domain, host, records = build(conf, args.config_json)

    if args.json:
        print(json.dumps({"domain": domain, "records": [
            {"type": t, "name": (n if n != "@" else domain),
             "value": v, "note": note} for t, n, v, note in records]},
            ensure_ascii=False, indent=2))
        return 0

    if args.zone_file:
        lines = ["; mailstack DNS records for %s" % domain,
                 "$TTL 600"]
        for rtype, name, value, _ in records:
            owner = "@" if name == "@" else name
            if rtype == "MX":
                lines.append("%-28s MX    %s" % (owner, value))
            elif rtype == "SRV":
                lines.append("%-28s SRV   %s" % (owner, value))
            elif rtype == "CNAME":
                lines.append("%-28s CNAME %s" % (owner, value))
            elif rtype == "TXT":
                lines.append('%-28s TXT   "%s"' % (owner, value.replace('"', '\\"')))
            else:
                lines.append("%-28s %-6s%s" % (owner, rtype, value))
        Path(args.zone_file).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("written " + args.zone_file)
        return 0

    width = max(len(r[1]) for r in records) + len(domain) + 2
    print("\nDNS records for %s  (add these at your DNS provider)\n" % domain)
    print("%-6s %-*s %s" % ("TYPE", width, "NAME", "VALUE"))
    print("-" * (width + 30))
    for rtype, name, value, note in records:
        fqdn = domain if name == "@" else "%s.%s" % (name, domain)
        print("%-6s %-*s %s" % (rtype, width, fqdn, value))
        print("       %s%s" % (" " * width, "└ " + note))
    print("\nPropagation usually takes 1-10 minutes. Verify with:")
    print("  dig +short MX %s        dig +short TXT default._domainkey.%s" % (domain, domain))
    return 0


if __name__ == "__main__":
    sys.exit(main())
