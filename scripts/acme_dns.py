#!/usr/bin/env python3
"""ACM(E) DNS-01 helper for Let's Encrypt certificates.

Two generic DNS-API styles are supported, with no third-party dependency:

  aksign   cloud DNS API with AK/SK request signing
           env: DNS_API_HOST / DNS_API_REGION / DNS_API_SERVICE
                DNS_ACCESS_KEY / DNS_SECRET_KEY / DNS_ZONE_ID(optional)

  token    cloud DNS API with a bearer token
           env: DNS_API_BASE / DNS_TOKEN

The endpoint is never hard-coded: point DNS_API_HOST or DNS_API_BASE at the
DNS API published by your cloud provider.

Certbot calls this script as

  acme_dns.py create        (CERTBOT_DOMAIN, CERTBOT_VALIDATION in env)
  acme_dns.py cleanup       (same env; record id is read back from the state file)

It writes the _acme-challenge TXT record, then waits until at least three
public resolvers see it, which avoids the classic DNS-01 negative-cache race.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

STATE_FILE = os.environ.get("ACME_DNS_STATE", "/run/mailstack/acme-dns.json")
RESOLVERS = ["8.8.8.8", "1.1.1.1", "9.9.9.9"]


# --------------------------------------------------------------------------- utils
def log(msg):
    sys.stderr.write("[acme-dns] %s\n" % msg)
    sys.stderr.flush()


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def challenge_name(domain):
    return "_acme-challenge." + domain if not domain.startswith("_acme-challenge") else domain


def wait_visible(fqdn, value, attempts=30, need=3, delay=5):
    for attempt in range(attempts):
        seen = 0
        for resolver in RESOLVERS:
            try:
                out = subprocess.run(
                    ["dig", "@" + resolver, "+time=3", "+tries=1", "+short", "TXT", fqdn],
                    capture_output=True, text=True, timeout=8).stdout
            except Exception:
                continue
            if value in out.replace('"', "").replace("\n", ""):
                seen += 1
        if seen >= need:
            log("challenge visible on %d/%d resolvers (attempt %d)" % (seen, len(RESOLVERS), attempt))
            return True
        time.sleep(delay)
    return False


# ------------------------------------------------------------------ aksign API
def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _hmac(key, data):
    if isinstance(data, str):
        data = data.encode()
    return hmac.new(key, data, hashlib.sha256).digest()


def _api_host():
    host = os.environ.get("DNS_API_HOST", "").strip()
    if not host:
        raise SystemExit("DNS_API_HOST is required for aksign mode")
    return host.replace("https://", "").rstrip("/")


def _api_region():
    return os.environ.get("DNS_API_REGION", "default").strip() or "default"


def _api_service():
    return os.environ.get("DNS_API_SERVICE", "dns").strip() or "dns"


def aksign_request(action, query=None, body=None, method="GET", version="2018-08-01"):
    host = _api_host()
    region = _api_region()
    service = _api_service()
    ak = os.environ["DNS_ACCESS_KEY"]
    sk = os.environ["DNS_SECRET_KEY"]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date = now[:8]
    full_query = {"Action": action, "Version": version}
    full_query.update(query or {})
    canonical_query = urllib.parse.urlencode(sorted(full_query.items()))
    payload = b"" if body is None else json.dumps(
        body, ensure_ascii=False, separators=(",", ":")).encode()
    payload_hash = _sha256_hex(payload)
    canonical_headers = "host:%s\nx-content-sha256:%s\nx-date:%s\n" % (
        host, payload_hash, now)
    signed_headers = "host;x-content-sha256;x-date"
    canonical_request = "\n".join([method, "/", canonical_query,
                                   canonical_headers, signed_headers, payload_hash])
    scope = "%s/%s/%s/request" % (date, region, service)
    string_to_sign = "\n".join(["HMAC-SHA256", now, scope,
                               _sha256_hex(canonical_request.encode())])
    key = _hmac(_hmac(_hmac(_hmac(sk.encode(), date), region), service), "request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Host": host,
        "X-Date": now,
        "X-Content-Sha256": payload_hash,
        "Authorization": "HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                         % (ak, scope, signed_headers, signature),
    }
    if body is not None:
        headers["Content-Type"] = "application/json; charset=UTF-8"
    req = urllib.request.Request("https://%s/?%s" % (host, canonical_query),
                                 data=payload or None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def aksign_zone_id(domain):
    zid = os.environ.get("DNS_ZONE_ID")
    if zid:
        return int(zid)
    for candidate in (domain, ".".join(domain.split(".")[-2:])):
        status, text = aksign_request("ListZones", {"PageNumber": 1, "PageSize": 100})
        if status != 200:
            raise SystemExit("ListZones failed: HTTP %s %s" % (status, text[:200]))
        zones = json.loads(text).get("Result", {}).get("Zones", [])
        for zone in zones:
            if zone.get("ZoneName") == candidate:
                return int(zone["ZID"])
    raise SystemExit("cannot locate DNS zone for %s (set DNS_ZONE_ID)" % domain)


def aksign_create(domain, value):
    zid = aksign_zone_id(domain)
    status, text = aksign_request("ListRecords", {"ZID": zid, "PageNumber": 1, "PageSize": 100})
    if status != 200:
        raise SystemExit("ListRecords failed: HTTP %s %s" % (status, text[:200]))
    zone_name = None
    for zone in json.loads(aksign_request("ListZones", {"PageNumber": 1, "PageSize": 100})[1]
                           ).get("Result", {}).get("Zones", []):
        if int(zone.get("ZID", 0)) == zid:
            zone_name = zone.get("ZoneName")
    if not zone_name:
        raise SystemExit("zone %s not found" % zid)
    host = "_acme-challenge" + ("" if domain == zone_name
                                else "." + domain[: -len("." + zone_name)])
    existing = [r for r in json.loads(text)["Result"]["Records"]
                if r["Host"] == host and r["Type"] == "TXT"]
    if existing:
        body = {"RecordID": existing[0]["RecordID"], "Host": host, "Line": "default",
                "Value": value, "TTL": 600}
        status, text = aksign_request("UpdateRecord", {}, body, "POST")
        rid = existing[0]["RecordID"]
    else:
        body = {"ZID": zid, "Host": host, "Type": "TXT", "Value": value,
                "Line": "default", "TTL": 600, "Weight": 1}
        status, text = aksign_request("CreateRecord", {}, body, "POST")
        rid = json.loads(text).get("Result", {}).get("RecordID") if status == 200 else None
    if status != 200:
        raise SystemExit("write challenge failed: HTTP %s %s" % (status, text[:300]))
    log("TXT written for %s (aksign)" % domain)
    return {"provider": "aksign", "zid": zid, "host": host, "record_id": rid}


def aksign_cleanup(entry, domain):
    zid = entry.get("zid")
    rid = entry.get("record_id")
    if not (zid and rid):
        return
    status, text = aksign_request("DeleteRecord", {}, {"RecordID": rid}, "POST")
    log("TXT cleanup: HTTP %s %s" % (status, text[:120]))


# ------------------------------------------------------------------- token API
def _api_base():
    base = os.environ.get("DNS_API_BASE", "").strip().rstrip("/")
    if not base:
        raise SystemExit("DNS_API_BASE is required for token mode")
    if not base.startswith("https://"):
        base = "https://" + base
    return base


def token_request(path, method="GET", body=None):
    token = os.environ["DNS_TOKEN"]
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(_api_base() + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def token_zone_id(domain):
    for candidate in (domain, ".".join(domain.split(".")[-2:])):
        status, payload = token_request("/zones?name=" + urllib.parse.quote(candidate))
        if status == 200 and payload.get("result"):
            return payload["result"][0]["id"], candidate
    raise SystemExit("cannot locate DNS zone for " + domain)


def token_create(domain, value):
    zone, zone_name = token_zone_id(domain)
    name = challenge_name(domain)
    status, payload = token_request("/zones/%s/dns_records" % zone, "POST",
                                    {"type": "TXT", "name": name, "content": value,
                                     "ttl": 120, "comment": "acme-dns-01"})
    if status not in (200, 201):
        raise SystemExit("DNS write failed: %s" % json.dumps(payload)[:300])
    log("TXT written for %s (token)" % name)
    return {"provider": "token", "zone": zone, "record_id": payload["result"]["id"]}


def token_cleanup(entry):
    zone, rid = entry.get("zone"), entry.get("record_id")
    if not (zone and rid):
        return
    status, payload = token_request("/zones/%s/dns_records/%s" % (zone, rid), "DELETE")
    log("TXT cleanup: HTTP %s" % status)


# --------------------------------------------------------------------------- main
def provider():
    if os.environ.get("DNS_ACCESS_KEY"):
        return "aksign"
    if os.environ.get("DNS_TOKEN"):
        return "token"
    raise SystemExit("no DNS credentials found: set DNS_ACCESS_KEY/SECRET or DNS_TOKEN")


def main(argv):
    if len(argv) < 2 or argv[1] not in ("create", "cleanup"):
        raise SystemExit("usage: acme_dns.py create|cleanup")
    action = argv[1]
    domain = os.environ.get("CERTBOT_DOMAIN", "").strip().lstrip("*.")
    if not domain:
        raise SystemExit("CERTBOT_DOMAIN is not set")
    fqdn = challenge_name(domain)
    state = load_state()

    if action == "create":
        value = os.environ.get("CERTBOT_VALIDATION", "").strip()
        if not value:
            raise SystemExit("CERTBOT_VALIDATION is not set")
        prov = provider()
        entry = aksign_create(domain, value) if prov == "aksign" else token_create(domain, value)
        state[fqdn] = entry
        save_state(state)
        if not wait_visible(fqdn, value):
            raise SystemExit("TXT record %s did not propagate to public resolvers" % fqdn)
        return 0

    entry = state.get(fqdn) or state.pop(fqdn, None)
    if not entry:
        log("nothing to clean for " + fqdn)
        return 0
    if entry.get("provider") == "aksign":
        aksign_cleanup(entry, domain)
    else:
        token_cleanup(entry)
    state.pop(fqdn, None)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
