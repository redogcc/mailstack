# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/), versions use SemVer.

## [1.0.0] - 2026-11-08

First public release, extracted from a production deployment serving 200+ mailboxes.

### Added
- One-command installer for Debian 12 / Ubuntu 22.04+ with interactive wizard and
  non-interactive mode (`-c FILE`, `--domain/--ip/...`, `-y`)
- Maddy 0.9.7 provisioning: binary download with mirror fallback, templated
  `maddy.conf`, systemd unit with sandboxing, admin mailbox bootstrap
- TLS automation: HTTP-01 (standalone/webroot), DNS-01 manual, cloud DNS API
  (bearer token or AK/SK signing), self-signed; deploy hook syncs Maddy + nginx
- nginx site + snippet templates, MTA-STS policy, optional landing page
- Web console (`/admin/`): form login, registration with invite code, server-side
  sessions, scoped mail client, mailbox CRUD with inline password reset, bulk
  provisioning, activation invites, organisations, health page
- `mailctl` operations helper: status/check/dns/logs/accounts/add/passwd/backup/cert
- `scripts/show-dns-records.py` and `scripts/selfcheck.py`
- Sandbox profile for isolated end-to-end testing (own ports, users, directories)
- Test suite: 12 offline unit tests, 18-check server-side E2E, token coverage check
- Documentation: README (zh/en), INSTALL, DNS, OPERATIONS, SECURITY

### Security
- Non-open relay, SPF/DKIM/DMARC/MTA-STS/TLS-RPT on by default
- Console never listens on a public interface; CSRF + origin checks; login lockout;
  per-account backend authorisation; sanitised HTML mail in sandboxed iframe
- Secrets 0600, data directories 0750, systemd hardening (`ProtectSystem=strict`)
- Uninstall refuses to guess service names and never removes paths it does not own

[1.0.0]: https://github.com/redogcc/mailstack/releases/tag/v1.0.0
