# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/), versions use SemVer.

## [1.1.0] - 2026-11-08

### Added
- **Super-admin mailbox inspection.** Under *System → Accounts* every row's mailbox
  address is now a button that opens a side drawer with that employee's folders,
  searchable message list, message body and attachments.
  - Read-only by design: browsing never issues `\Seen`, so the employee's unread
    state is untouched; the drawer footer says so.
  - Restricted to the console account named by `ADMIN_EMAIL`; the tab, the entry
    point and the drawer are not rendered for regular users.
  - Backend enforcement: the handler overwrites `account` with the session's own
    address for every non-admin request, so a hand-crafted query cannot reach
    another mailbox.
  - Audit trail: each open of somebody else's mailbox appends an `AUDIT_MAILBOX`
    line (actor, target, source IP, timestamp) to `auth.log`. Subjects, bodies and
    attachments are deliberately never logged.
  - Responsive: below 860 px the drawer stacks list over reader instead of
    splitting left/right.
- Keyboard support: `Esc` closes the drawer; the mailbox button is focus-visible
  and carries an explicit `aria-label`.

### Changed
- Account list markup restructured so the address (not the checkbox label) is the
  interactive element; bulk-selection behaviour is unchanged.
- Accounts page copy now states that clicking an address opens the read-only
  mailbox view and that it is a super-admin permission.

### Known limitations
- The drawer logs in on the employee's behalf using the credential the console
  stores. If an employee changed their password outside the console, no credential
  is available and the drawer reports that explicitly rather than failing silently.
- Super-admin inspection is not delegated: there is exactly one super admin
  (`ADMIN_EMAIL`). Additional roles are out of scope for this version.

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

[1.1.0]: https://github.com/redogcc/mailstack/releases/tag/v1.1.0
[1.0.0]: https://github.com/redogcc/mailstack/releases/tag/v1.0.0
