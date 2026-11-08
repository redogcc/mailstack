# mailstack

[![CI](https://github.com/redogcc/mailstack/actions/workflows/ci.yml/badge.svg)](https://github.com/redogcc/mailstack/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-1.1.0-blue)
![Platform](https://img.shields.io/badge/platform-Ubuntu%2022.04%20%7C%2024.04%20%7C%20Debian%2012-lightgrey)

**A complete business mail server in one command — on your own hardware.**

Maddy (SMTP/IMAP) + nginx (TLS) + a dependency-free web console.
No Docker, no Kubernetes, no pip. Runs comfortably on a 1 vCPU / 1 GB VPS.

[中文说明](README.md)

---

## Why

| | |
|---|---|
| **One command** | The wizard asks for a domain, a public IP and an admin address — nothing else |
| **Genuinely small** | Measured: Maddy 12.5 MB, console 18.5 MB, nginx 3.8 MB |
| **Complete** | SMTP/IMAP, webmail, mailbox + organisation management, bulk provisioning, invite activation |
| **Secure by default** | TLS 1.2+, SPF/DKIM/DMARC, MTA-STS, TLS-RPT, CSRF, session isolation, login throttling |
| **Operable** | `mailctl` for status/self-check/backup/renewal; every config is a template you can regenerate |

## Quick start

On Ubuntu 22.04 / 24.04 or Debian 12, as root:

```bash
git clone https://github.com/redogcc/mailstack.git
cd mailstack
sudo bash install.sh
```

The wizard asks for:

```
Mail domain (the part after @) [example.com]: acme.cn
Mail hostname (A record -> this server) [mail.acme.cn]:
Public IPv4 of this server [auto-detected]:
Administrator mailbox [admin@acme.cn]:
TLS certificate
  1) Let's Encrypt via HTTP-01      - port 80 reachable, DNS already live
  2) Let's Encrypt via DNS-01       - add TXT records by hand
  3) Let's Encrypt via DNS-01 API   - automatic DNS-01 with a cloud API token
  4) Let's Encrypt via DNS-01 AK/SK - automatic DNS-01 with signed credentials
  5) self-signed                    - testing only
Choose 1-5 [1]:
```

It then prints the DNS records you must create, the console URL, the generated admin
password (once), and the result of a full self-check.

## What you get

- **Mail**: SMTP 25, submission 587 (STARTTLS) / 465 (TLS), IMAPS 993, DKIM 2048,
  DMARC enforcement, MTA-STS, TLS-RPT, plus-addressing, `postmaster@` aliases
- **Web console** at `/admin/`: login/registration, three-pane webmail, mailbox CRUD with
  inline password reset, bulk provisioning, organisations, one-time activation invites,
  system health page
- **Super-admin mailbox inspection**: click a mailbox name under Accounts to open a
  read-only side drawer with that employee's folders, message list, body and attachments.
  Only `ADMIN_EMAIL` can use it, it never marks messages as read, and every open is
  written to the audit log as `AUDIT_MAILBOX`
- **Site**: optional landing page with a login button

## Requirements

Ubuntu 22.04/24.04 or Debian 12 (systemd), 1 vCPU / 1 GB RAM, 10 GB disk, public IPv4,
**outbound port 25 unblocked** (most Chinese clouds block it by default — open a ticket),
one A record pointing at the server. PTR is recommended.

## Configuration

```bash
cp conf/install.conf.example my.conf && vim my.conf
sudo bash install.sh -c my.conf -y
```

| Key | Default | Notes |
|---|---|---|
| `MAIL_DOMAIN` | required | Domain after the `@` |
| `MAIL_HOSTNAME` | `mail.<domain>` | Must have an A record to this server |
| `SERVER_IP` | auto-detected | Public IPv4 |
| `ADMIN_EMAIL` | `admin@<domain>` | Also the web login name |
| `ADMIN_PASSWORD` | `auto` | Printed once, also stored 0600 on disk |
| `TLS_MODE` | `http01` | `http01` / `dns-manual` / `dns-token` / `dns-aksign` / `selfsigned` |

## Operations

```bash
/opt/mailstack/bin/mailctl status | check | dns | logs | accounts | backup | cert renew
```

Uninstall keeps mail data unless `--purge` is given:

```bash
sudo bash uninstall.sh [--purge]
```

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — step-by-step install, parameters, multi-instance
- [docs/DNS.md](docs/DNS.md) — every record, provider examples, verification
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — backup, renewal, logs, troubleshooting
- [docs/SECURITY.md](docs/SECURITY.md) — hardening model and checklist

## License

MIT. Bundled/used third-party components: Maddy (GPLv3, invoked as a separate process),
nginx (BSD-2-Clause), Certbot (Apache-2.0).
