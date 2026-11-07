#!/usr/bin/env python3
"""Lightweight web admin for the Maddy mail server (Python stdlib only).

Listens on 127.0.0.1 only; nginx terminates TLS and reverse-proxies /admin/.

v4: full three-pane mail client (folders, search, threaded reading,
attachments, compose/reply/forward, drafts) + original admin views.
v5: form login/register, server-side sessions, per-account data isolation.
v6: organisations, mailbox ownership, bulk reassignment, account page reorder.
v7: portable build - every deployment value is read from MAILSTACK_CONFIG.
"""
import base64
import datetime
import email.header
import email.message
import email.parser
import email.policy
import email.utils
import hmac
import html
import imaplib
import json
import os
import re
import secrets
import shutil
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# =============================================================================
# Deployment configuration
# -----------------------------------------------------------------------------
# Everything site-specific lives in one JSON file so this same file can be
# dropped on any server. install.sh renders it from install.conf; the built-in
# defaults below keep the program runnable stand-alone for tests.
# =============================================================================
CONFIG_FILE = Path(os.environ.get("MAILSTACK_CONFIG", "/etc/mailstack/config.json"))

_DEFAULTS = {
    "domain": "example.com",
    "mail_hostname": "mail.example.com",
    "server_ip": "",
    "admin_email": "admin@example.com",
    "data_dir": "/var/lib/maddy",
    "state_dir": "/var/lib/maddy/admin",
    "secrets_dir": "/etc/maddy-admin",
    "maddy_conf": "/etc/maddy/maddy.conf",
    "cert_dir": "/etc/maddy/certs/mail.example.com",
    "ip_cert_dir": "",
    "mta_sts_file": "/var/www/mta-sts/.well-known/mta-sts.txt",
    "listen": "127.0.0.1",
    "port": 8080,
    "public_url": "",
    "imap_port": 993,
    "smtps_port": 465,
    "smtp_port": 25,
    "submission_port": 587,
    "maddy_service": "maddy",
    "nginx_service": "nginx",
    "admin_service": "maddy-admin",
    "certbot_timer": "certbot.timer",
    "brand": "",
    "cookie_secure": True,
}


def _load_config(path):
    cfg = dict(_DEFAULTS)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except Exception as exc:                      # malformed file must not brick the panel
        sys.stderr.write("config %s unreadable: %s\n" % (path, exc))
        return cfg
    if isinstance(data, dict):
        for key, value in data.items():
            if value is not None and key in cfg:
                cfg[key] = value
    return cfg


CFG = _load_config(CONFIG_FILE)
DOMAIN = str(CFG["domain"]).strip().lower()
MAIL_HOSTNAME = str(CFG["mail_hostname"] or ("mail." + DOMAIN)).strip().lower()
SERVER_IP = str(CFG["server_ip"]).strip()
ADMIN_MAIL = str(CFG["admin_email"]).strip().lower()
ADMIN_USER = ADMIN_MAIL.split("@")[0] if "@" in ADMIN_MAIL else "admin"
BRAND = str(CFG.get("brand") or DOMAIN)
DATA_DIR = Path(CFG["data_dir"])
STATE_DIR = Path(CFG["state_dir"])
SECRETS_DIR = Path(CFG["secrets_dir"])
CONF = str(CFG["maddy_conf"])
CERT = str(Path(CFG["cert_dir"]) / "fullchain.pem")
IP_CERT = (str(Path(CFG["ip_cert_dir"]) / "fullchain.pem") if CFG["ip_cert_dir"] else "")
MTA_STS_FILE = str(CFG["mta_sts_file"])
COOKIE_SECURE = str(CFG.get("cookie_secure", True)).lower() not in ("false", "0", "no")
COOKIE_FLAGS = "; HttpOnly; Secure; SameSite=Lax" if COOKIE_SECURE else "; HttpOnly; SameSite=Lax"
AUTH_FILE = SECRETS_DIR / "auth.env"
MAILPASS_FILE = SECRETS_DIR / "mailpass"
REGISTER_FILE = SECRETS_DIR / "register.env"
LISTEN = str(CFG["listen"])
PORT = int(CFG["port"])
IMAP_PORT = int(CFG["imap_port"])
SMTPS_PORT = int(CFG["smtps_port"])
SMTP_PORT = int(CFG["smtp_port"])
SUBMISSION_PORT = int(CFG["submission_port"])
MADDY_SERVICE = str(CFG["maddy_service"])
NGINX_SERVICE = str(CFG["nginx_service"])
ADMIN_SERVICE = str(CFG["admin_service"])
CERTBOT_TIMER = str(CFG["certbot_timer"])

BASE = "/admin"
MADDY = os.environ.get("MADDY_BIN", "/usr/local/bin/maddy")
PUBLIC_URL = str(CFG.get("public_url") or "").rstrip("/")
if not PUBLIC_URL:
    PUBLIC_URL = "https://" + (MAIL_HOSTNAME if DOMAIN else SERVER_IP)
IP_ACCESS = PUBLIC_URL + "/admin/"
EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._%+-]{0,63}@" + re.escape(DOMAIN) + r"$")
LOCK = threading.Lock()
FAILURES = {}          # ip -> [count, locked_until]
MAX_FAILS = 5
LOCK_SECONDS = 600

# ---- batch provisioning / activation -------------------------------------
BATCH_DIR = STATE_DIR
BATCH_FILE = BATCH_DIR / "batches.json"
BATCH_LOCK = threading.RLock()
ACTIVATE_BASE = PUBLIC_URL + "/admin/activate?token="
TOKEN_TTL = 72 * 3600

# ---- legacy -> configured value rewriting ---------------------------------
# The HTML/JS below was originally written for one deployment. Instead of
# scattering format calls through ~3.6k lines we rewrite the handful of literal
# values on the way out (HTML and JSON only, never binaries or mail bodies).
_LEGACY = tuple(
    (old, new) for old, new in (
        ("admin@example.com", ADMIN_MAIL),
        ("mail.example.com", MAIL_HOSTNAME),
        ("mta-sts.example.com", "mta-sts." + DOMAIN),
        ("203.0.113.10", SERVER_IP),
        ("/var/lib/maddy", str(DATA_DIR)),
        ("example.com", DOMAIN),
    ) if new
)


def localize(text):
    """Rewrite build-time example values to this deployment's values."""
    if not text:
        return text
    for old, new in _LEGACY:
        if old in text and old != new:
            text = text.replace(old, new)
    return text
WORDS = (
    "cedar lumen harbor willow quartz meadow falcon ember drift summit "
    "pebble cobalt juniper orchard raven amber brook maple ridge fern "
    "coral slate basalt pollen zephyr thistle bramble marlin birch cinder "
    "dune flint grove hazel indigo kestrel lark moss nimbus opal pine "
    "rowan sable tundra velvet wren yarrow zinnia"
).split()


def read_secret(path, key=None):
    raw = path.read_text(encoding="utf-8")
    if key:
        for line in raw.splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
        raise RuntimeError("missing key in " + str(path))
    return raw.strip()


AUTH_PASS = read_secret(AUTH_FILE, "ADMIN_PASSWORD")
MAILPASS = read_secret(MAILPASS_FILE)

# ================================================================ v5 auth / sessions
# Form login + server-side sessions. Passwords are verified against Maddy's real
# IMAP credentials; a user session is strictly scoped to its own mailbox.
AUTH_COOKIE = "mailadmin_sid"
SESSION_IDLE = 2 * 3600          # 2h idle timeout
SESSION_ABS = 12 * 3600          # 12h absolute lifetime
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 600
REGISTER_DB = BATCH_DIR / "registered.json"
AUTH_LOG = BATCH_DIR / "auth.log"
REGISTER_LIMIT = 5               # successful registrations per IP per hour
REGISTER_ATTEMPT_LIMIT = 20      # all attempts per IP per hour

SESSION_LOCK = threading.RLock()
SESSIONS = {}                    # sid -> session dict (in memory only)
LOGIN_FAILS = {}                 # key -> [count, locked_until]
REGISTER_HITS = {}               # ip -> [count, window_start]
CTX = threading.local()          # per-request session, read by account_password()

RESERVED_LOCALS = {
    "admin", "postmaster", "abuse", "root", "webmaster", "hostmaster",
    "noreply", "no-reply", "mail", "mta-sts", "smtp", "imap", "pop", "info",
    "support", "security", "test", "www", "mailstack", "sysadmin", "administrator",
    "help", "sales", "contact", "news", "bounce", "dmarc", "spf", "dkim",
}
LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9._]{1,30}[a-z0-9]$")


def auth_cfg():
    cfg = {"code": "", "open": False}
    try:
        for line in REGISTER_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("REGISTER_CODE="):
                cfg["code"] = line.split("=", 1)[1].strip()
            elif line.startswith("REGISTER_OPEN="):
                cfg["open"] = line.split("=", 1)[1].strip().lower() == "true"
    except Exception:
        pass
    return cfg


def audit(event, account="", ip="", detail=""):
    """Append-only auth audit trail. Never log passwords."""
    try:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        line = "%s  %-13s %-30s %-16s %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), event, account, ip, detail)
        with AUTH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
        try:
            os.chmod(AUTH_LOG, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def session_create(account, password, ip):
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with SESSION_LOCK:
        # Hard cap on concurrent sessions to avoid unbounded memory growth.
        if len(SESSIONS) > 2000:
            for k in sorted(SESSIONS, key=lambda x: SESSIONS[x]["last"])[:200]:
                SESSIONS.pop(k, None)
        SESSIONS[sid] = {"account": account, "password": password,
                         "csrf": secrets.token_urlsafe(24),
                         "created": now, "last": now, "ip": ip}
    return sid


def session_get(sid):
    if not sid:
        return None
    with SESSION_LOCK:
        rec = SESSIONS.get(sid)
        if not rec:
            return None
        now = time.time()
        if now - rec["last"] > SESSION_IDLE or now - rec["created"] > SESSION_ABS:
            SESSIONS.pop(sid, None)
            return None
        rec["last"] = now
        return dict(rec)


def session_destroy(sid):
    with SESSION_LOCK:
        SESSIONS.pop(sid, None)


def cookie_sid(headers):
    for part in (headers.get("Cookie", "") or "").split(";"):
        key, _, val = part.strip().partition("=")
        if key == AUTH_COOKIE:
            return val
    return ""


def sid_cookie(sid, max_age=SESSION_ABS):
    return ("%s=%s; Path=/admin; Max-Age=%d" % (AUTH_COOKIE, sid, max_age)) + COOKIE_FLAGS


def clear_cookie():
    return ("%s=; Path=/admin; Max-Age=0" % AUTH_COOKIE) + COOKIE_FLAGS


def locked(key):
    with LOCK:
        rec = LOGIN_FAILS.get(key, [0, 0])
        return rec[1] > time.time()


def note_fail(*keys):
    is_locked = False
    with LOCK:
        for key in keys:
            rec = LOGIN_FAILS.get(key, [0, 0])
            rec[0] += 1
            if rec[0] >= LOGIN_MAX_FAILS:
                rec[1] = time.time() + LOGIN_LOCK_SECONDS
                rec[0] = 0
                is_locked = True
            LOGIN_FAILS[key] = rec
    return is_locked


def clear_fail(*keys):
    with LOCK:
        for key in keys:
            LOGIN_FAILS.pop(key, None)


def verify_mail_password(addr, pw):
    """Real credential check: only a valid Maddy IMAP login succeeds."""
    try:
        m = imap_session(addr, pw)
        try:
            m.logout()
        except Exception:
            pass
        return True
    except Exception:
        return False


def password_problem(pw, addr):
    if len(pw) < 10 or len(pw) > 128:
        return "密码长度需为 10–128 位"
    if "\n" in pw or "\r" in pw:
        return "密码不能包含换行"
    classes = sum((bool(re.search(r"[a-z]", pw)), bool(re.search(r"[A-Z]", pw)),
                   bool(re.search(r"[0-9]", pw)), bool(re.search(r"[^A-Za-z0-9]", pw))))
    if classes < 3:
        return "密码需包含大写字母、小写字母、数字、符号中的至少 3 类"
    local = addr.split("@", 1)[0]
    if local and local.lower() in pw.lower():
        return "密码不能包含邮箱名"
    if len(set(pw)) < 4:
        return "密码过于简单"
    return None


def register_hit(ip, success):
    """Return True when the IP is allowed to attempt a registration."""
    with LOCK:
        now = time.time()
        rec = REGISTER_HITS.get(ip, [0, now])
        if now - rec[1] > 3600:
            rec = [0, now]
        rec[0] += 1
        REGISTER_HITS[ip] = rec
        return rec[0] <= REGISTER_ATTEMPT_LIMIT, rec[0]


def load_registered():
    try:
        return json.loads(REGISTER_DB.read_text(encoding="utf-8"))
    except Exception:
        return {"accounts": []}


def save_registered(data):
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTER_DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, REGISTER_DB)


def do_register(form, ip):
    cfg = auth_cfg()
    local = (form.get("local", "") or "").strip().lower()
    name = (form.get("name", "") or "").strip()[:40]
    pw = form.get("password", "") or ""
    pw2 = form.get("password2", "") or ""
    code = (form.get("code", "") or "").strip()

    allowed, _ = register_hit(ip, False)
    if not allowed:
        return {"ok": False, "msg": "注册尝试过于频繁，请 1 小时后再试"}
    if not cfg["open"]:
        if not cfg["code"] or not hmac.compare_digest(code, cfg["code"]):
            audit("REG_BADCODE", local, ip)
            return {"ok": False, "msg": "注册码不正确"}
    if not LOCAL_RE.match(local) or len(local) < 3 or ".." in local:
        return {"ok": False, "msg": "邮箱名需为 3–32 位小写字母/数字，可用 . 或 _ 分隔"}
    if local in RESERVED_LOCALS:
        return {"ok": False, "msg": "该邮箱名为系统保留，请换一个"}
    addr = local + "@" + DOMAIN
    if not EMAIL_RE.match(addr):
        return {"ok": False, "msg": "邮箱格式不合法"}
    if pw != pw2:
        return {"ok": False, "msg": "两次输入的密码不一致"}
    bad = password_problem(pw, addr)
    if bad:
        return {"ok": False, "msg": bad}
    if addr in existing_accounts():
        return {"ok": False, "msg": "该邮箱已被注册"}

    reg = load_registered()
    if any(a.get("email") == addr for a in reg["accounts"]):
        return {"ok": False, "msg": "该邮箱已被注册"}
    recent = sum(1 for a in reg["accounts"]
                 if a.get("ip") == ip and time.time() - float(a.get("ts", 0)) < 3600)
    if recent >= REGISTER_LIMIT:
        return {"ok": False, "msg": "同一网络 1 小时内注册数量已达上限"}

    out1, rc1 = maddy_cli(["creds", "create", addr], stdin=pw + "\n")
    if rc1 != 0:
        return {"ok": False, "msg": "创建认证失败: " + out1[:120]}
    out2, rc2 = maddy_cli(["imap-acct", "create", addr])
    if rc2 != 0:
        maddy_cli(["creds", "remove", "-y", addr])
        return {"ok": False, "msg": "创建邮箱存储失败，已回滚: " + out2[:120]}
    if not verify_mail_password(addr, pw):
        maddy_cli(["creds", "remove", "-y", addr])
        maddy_cli(["imap-acct", "remove", "-y", addr])
        return {"ok": False, "msg": "账号自检未通过，已回滚，请稍后重试"}
    reg["accounts"].append({"email": addr, "name": name,
                            "created_at": now_str(), "ts": time.time(), "ip": ip})
    save_registered(reg)
    # Deliberately NOT store_credential(): a self-registered password is never
    # written to disk in clear text. It only lives in the user's live session.
    audit("REGISTER_OK", addr, ip, "name=%s" % (name or "-"))
    return {"ok": True, "msg": "邮箱 %s 注册成功，请用刚设置的密码登录" % addr,
            "email": addr}


AUTH_CSS = """
:root{--bg:#f4f1ea;--panel:#fffdf9;--ink:#26221d;--ink2:#4a433a;--muted:#8a8175;
--line:#e6dfd3;--line2:#f0ebe1;--accent:#b4533a;--accent2:#f5e9e3;--bad:#a63d3d;--ok:#3f7d58}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);
 font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
 display:flex;align-items:center;justify-content:center;padding:28px 18px;
 background-image:radial-gradient(circle at 12% 8%,#faf6ee 0,transparent 42%),radial-gradient(circle at 88% 92%,#efe7dc 0,transparent 40%)}
.wrap{width:100%;max-width:420px}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:26px;justify-content:center}
.logo{width:40px;height:40px;border-radius:11px;background:var(--accent);color:#fff;
 display:flex;align-items:center;justify-content:center;font:24px Georgia,serif}
.brand h1{font:20px/1.2 Georgia,"Songti SC",serif;margin:0;letter-spacing:.2px}
.brand .tiny{font-size:11px;color:var(--muted);letter-spacing:1.6px;text-transform:uppercase}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
 padding:30px 30px 26px;box-shadow:0 1px 2px rgba(38,34,29,.04),0 10px 34px rgba(38,34,29,.07)}
h2{font:22px/1.3 Georgia,"Songti SC",serif;margin:0 0 6px}
.sub{color:var(--muted);font-size:12.5px;margin:0 0 22px}
label{display:block;font-size:12px;font-weight:600;color:var(--ink2);margin:15px 0 6px}
input{width:100%;height:42px;border:1px solid var(--line);background:#fff;border-radius:8px;
 padding:0 12px;font-size:14px;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent2)}
.row{display:flex;align-items:center;gap:8px}
.row input{flex:1}
.suffix{color:var(--muted);font-size:13px;white-space:nowrap;font-family:ui-monospace,Menlo,monospace}
button{width:100%;height:43px;margin-top:22px;border:0;border-radius:8px;background:var(--accent);
 color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.9}button:disabled{opacity:.55;cursor:wait}
.err{display:none;margin-top:16px;padding:10px 12px;border-radius:7px;background:#fbefec;
 border-left:3px solid var(--bad);color:#8a3232;font-size:12.5px}
.okbox{display:none;margin-top:16px;padding:12px;border-radius:7px;background:#edf5ef;
 border-left:3px solid var(--ok);color:#2f6144;font-size:13px}
.links{margin-top:20px;text-align:center;font-size:12.5px;color:var(--muted)}
a{color:var(--accent);text-decoration:none;font-weight:600}
.note{margin-top:22px;padding-top:16px;border-top:1px solid var(--line2);
 font-size:11.5px;line-height:1.8;color:var(--muted)}
.note b{color:var(--ink2);font-weight:600}
.hint{font-size:11px;color:var(--muted);margin-top:6px}
"""


def auth_page(mode):
    """mode = 'login' | 'register'. __REG_HINT__ is filled by the caller."""
    is_login = mode == "login"
    title = "登录企业邮箱" if is_login else "注册企业邮箱"
    sub = ("请使用您的 @example.com 邮箱和登录密码" if is_login
           else "创建一个属于您自己的 @example.com 邮箱")
    if is_login:
        fields = (
            '<label>邮箱地址</label>'
            '<input id="f-email" type="email" placeholder="name@example.com" '
            'autocomplete="username" required autofocus>'
            '<label>登录密码</label>'
            '<input id="f-pw" type="password" autocomplete="current-password" required>'
        )
        links = ('还没有账号？ <a href="/admin/register">注册新邮箱</a>'
                 if auth_cfg()["open"] or auth_cfg()["code"] else "")
        action_js = """
  const r=await fetch('/admin/login',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({email:$('#f-email').value.trim(),password:$('#f-pw').value})});"""
    else:
        fields = (
            '<label>邮箱名</label>'
            '<div class="row"><input id="f-local" placeholder="例如 zhangsan" '
            'autocomplete="off" required autofocus><span class="suffix">@example.com</span></div>'
            '<label>姓名（可选）</label>'
            '<input id="f-name" autocomplete="name" maxlength="40">'
            '<label>设置密码</label>'
            '<input id="f-pw" type="password" autocomplete="new-password" required>'
            '<div class="hint">至少 10 位，需含大写字母、小写字母、数字、符号中的至少 3 类</div>'
            '<label>确认密码</label>'
            '<input id="f-pw2" type="password" autocomplete="new-password" required>'
            '<label>注册码</label>'
            '<input id="f-code" placeholder="请向管理员索取" autocomplete="off" required>'
        )
        links = '已有账号？ <a href="/admin/login">返回登录</a>'
        action_js = """
  const r=await fetch('/admin/register',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({local:$('#f-local').value.trim(),name:$('#f-name').value.trim(),
      password:$('#f-pw').value,password2:$('#f-pw2').value,code:$('#f-code').value.trim()})});"""
    note = (
        '<div class="note"><b>安全说明</b><br>'
        '· 密码与邮件系统凭据一致，错误密码将被拒绝并记录<br>'
        '· 连续 5 次失败将临时锁定 10 分钟<br>'
        '· 会话 2 小时无操作自动退出，最长 12 小时<br>'
        '· 登录后只能访问<b>本人</b>邮箱，无法查看他人邮件</div>')
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s · example.com</title><style>%s</style></head><body>'
            '<div class="wrap"><div class="brand"><div class="logo">@</div>'
            '<div><h1 class="serif">example.com</h1><div class="tiny">Secure Mail</div></div></div>'
            '<div class="card" id="card"><h2>%s</h2><p class="sub">%s</p>'
            '<form id="form">%s'
            '<div class="err" id="err"></div><div class="okbox" id="ok"></div>'
            '<button id="btn" type="submit">%s</button></form>'
            '<div class="links" id="links">%s</div>%s</div></div>'
            '<script>'
            'const $=(s)=>document.querySelector(s);'
            'function err(t){const e=$("#err");e.textContent=t;e.style.display="block";'
            '$("#btn").disabled=false;}'
            "$('#form').onsubmit=async(e)=>{e.preventDefault();"
            "$('#err').style.display='none';$('#btn').disabled=true;"
            "try{%s"
            "const d=await r.json();"
            "if(!d.ok){err(d.msg||'操作失败');return;}"
            "if(%s){$('#card').innerHTML='<h2>注册成功</h2><p class=\"sub\">'+"
            "d.msg+'</p><a href=\"/admin/login\">前往登录 →</a>'+"
            "'<div class=\"note\">请妥善保管密码，系统不会明文存储或显示。</div>';return;}"
            "location.href='/admin/';"
            "}catch(x){err('网络错误，请稍后重试');}};"
            '</script></body></html>'
            % (title, AUTH_CSS, title, sub, fields,
               "登录" if is_login else "创建邮箱", links, note, action_js,
               "false" if is_login else "true"))



def sh(args, timeout=12):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (r.stdout.strip() or r.stderr.strip(), r.returncode)
    except Exception as exc:
        return ("ERR: %s" % exc, 1)


def maddy_cli(args, stdin=None, timeout=20):
    cmd = [MADDY, "--config", CONF] + list(args)
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout, cwd=str(DATA_DIR))
        text = (r.stdout.strip() + "\n" + r.stderr.strip()).strip()
        rc = r.returncode
        # Maddy's CLI prints fatal errors to stderr but still exits with 0.
        if rc == 0 and ("app.Run failed" in text or "level=error" in text.lower()):
            rc = 1
        return (text, rc)
    except Exception as exc:
        return ("ERR: %s" % exc, 1)


def dig(qtype, name):
    for args in (["dig", "+time=3", "+tries=2", "+short", qtype, name],
                 ["dig", "@8.8.8.8", "+time=3", "+tries=2", "+short", qtype, name]):
        out, rc = sh(args, timeout=14)
        out = out.replace('" "', "").replace('"', "").replace("\n", " ").strip()
        if out and rc == 0:
            return out
    return ""


def local_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def imap_session(user=ADMIN_MAIL, password=None):
    m = imaplib.IMAP4_SSL("127.0.0.1", IMAP_PORT, ssl_context=local_ssl_context(), timeout=20)
    m.login(user, password if password is not None else MAILPASS)
    return m


def decode_mime(value):
    try:
        return str(email.header.make_header(email.header.decode_header(value or "")))
    except Exception:
        return value or ""


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- batch state

def load_state():
    with BATCH_LOCK:
        try:
            return json.loads(BATCH_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"batches": []}


def save_state(st):
    with BATCH_LOCK:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = BATCH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, BATCH_FILE)


def find_batch(st, batch_id=None):
    if batch_id:
        for b in st.get("batches", []):
            if b.get("id") == batch_id:
                return b
        return None
    return st["batches"][-1] if st.get("batches") else None


def find_token(st, token):
    if not token:
        return None, None
    for b in st.get("batches", []):
        for a in b.get("accounts", []):
            if hmac.compare_digest(str(a.get("token", "")), token):
                return b, a
    return None, None


def public_account(a):
    return {k: a.get(k) for k in (
        "email", "status", "token", "activate_url", "created_at", "invited_at",
        "activated_at", "activated_by", "final_password",
        "cn_name", "pinyin", "category", "category_name", "rule", "index")}


def build_invite(a):
    msg = email.message.EmailMessage()
    msg["From"] = ADMIN_MAIL
    msg["To"] = a["email"]
    msg["Subject"] = localize("[example.com] 邮箱账号激活邀请 - %s" % a["email"])
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=DOMAIN)
    body = (
        "您好，\n\n"
        "管理员已为您创建 example.com 企业邮箱账号：\n\n"
        "    邮箱地址：%s\n\n"
        "请在 72 小时内打开以下链接，设置登录密码并完成激活：\n\n"
        "    %s\n\n"
        "（若链接未自动变为可点击，请完整复制到浏览器地址栏打开。）\n\n"
        "激活后可使用以下参数收发邮件：\n"
        "    IMAP   mail.example.com:993   SSL/TLS\n"
        "    SMTP   mail.example.com:465   SSL/TLS\n"
        "    SMTP   mail.example.com:587   STARTTLS\n"
        "    网页管理台  %s\n\n"
        "出于安全考虑，此链接仅可使用一次；激活前账号无法凭任何密码登录。\n"
        "如非本人操作，请直接忽略本邮件。\n\n"
        "-- example.com 邮件系统管理员\n" % (a["email"], a["activate_url"], IP_ACCESS)
    )
    msg.set_content(localize(body))
    return msg


def activate_one(token, password, source="link"):
    """Validate a one-time token, set the real password and mark activated."""
    with BATCH_LOCK:
        st = load_state()
        batch, acct = find_token(st, token)
        if not acct:
            return False, "激活链接无效（令牌不存在）", None
        if acct.get("status") == "activated":
            return False, "该账号已激活，此链接不可重复使用", acct
        if time.time() - float(acct.get("created_ts", 0)) > TOKEN_TTL:
            return False, "激活链接已过期（超过 72 小时）", acct
        if not password or len(password) < 10 or "\n" in password or "\r" in password:
            return False, "密码至少 10 位且不能包含换行", acct
        out, rc = maddy_cli(["creds", "password", acct["email"]],
                            stdin=password + "\n")
        if rc != 0:
            return False, "写入登录密码失败: " + out[:200], acct
        acct["status"] = "activated"
        acct["activated_at"] = now_str()
        acct["activated_by"] = source
        acct["final_password"] = password
        store_credential(acct["email"], password)
        save_state(st)
        return True, "激活成功，登录密码已生效", acct


# ================================================================ v6 organizations
# Organization directory + mailbox ownership. Stored as one small JSON file next
# to the other admin state; no database and no extra runtime dependency.
ORG_FILE = BATCH_DIR / "organizations.json"
ORG_LOCK = threading.RLock()
ORG_PALETTE = ["#b4533a", "#c08a3e", "#3f7d58", "#4a6fa5",
               "#8a63a8", "#c26a6a", "#6b6257", "#2f7f7a"]
DEFAULT_ORGS = [
    ("sales", "销售", "#b4533a"),
    ("admin", "行政", "#c08a3e"),
    ("finance", "财务", "#3f7d58"),
    ("tech", "技术", "#4a6fa5"),
    ("support", "售后", "#8a63a8"),
    ("telesales", "电销", "#c26a6a"),
]
ORG_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ORG_BAD_NAME_RE = re.compile(r"[<>\x00-\x1f\"'\\]")


def org_admin_guard():
    """Organisation data is admin-only (the router whitelist already enforces this)."""
    sess = getattr(CTX, "session", None)
    if not sess or sess.get("account") != ADMIN_MAIL:
        return {"ok": False, "msg": "仅管理员可管理组织"}
    return None


def org_state():
    with ORG_LOCK:
        try:
            d = json.loads(ORG_FILE.read_text(encoding="utf-8"))
            if isinstance(d.get("orgs"), list) and isinstance(d.get("assignments"), dict):
                return d
        except Exception:
            pass
        return None


def save_org_state(d):
    with ORG_LOCK:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ORG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, ORG_FILE)


def seed_orgs():
    """First run: create the six default orgs and give every existing mailbox an owner."""
    orgs = [{"id": oid, "name": name, "color": color, "created_at": now_str(),
             "builtin": True} for oid, name, color in DEFAULT_ORGS]
    assignments = {}
    try:
        mailboxes = sorted(existing_accounts())
        staff = [x for x in mailboxes if x != ADMIN_MAIL]
        for i, addr in enumerate(staff):
            assignments[addr] = orgs[i % len(orgs)]["id"]
        if ADMIN_MAIL in mailboxes:
            # The system administrator belongs to 行政, so no mailbox is left out.
            assignments[ADMIN_MAIL] = orgs[1]["id"]
    except Exception:
        pass
    d = {"orgs": orgs, "assignments": assignments,
         "created_at": now_str(), "updated_at": now_str()}
    save_org_state(d)
    audit("ORG_SEED", detail="%d orgs / %d mailboxes" % (len(orgs), len(assignments)))
    return d


def load_orgs():
    st = org_state()
    return st if st is not None else seed_orgs()


def org_name_ok(name, st, exclude_id=None):
    name = (name or "").strip()
    if not (1 <= len(name) <= 20):
        return None, "组织名称需 1–20 个字符"
    if ORG_BAD_NAME_RE.search(name):
        return None, "组织名称不能包含 < > 引号或控制字符"
    if any(o.get("name", "").strip().lower() == name.lower()
           and o.get("id") != exclude_id for o in st["orgs"]):
        return None, "组织名称已存在"
    return name, None


def org_by_id(st, oid):
    return next((o for o in st["orgs"] if o.get("id") == oid), None)


def api_orgs(q=None):
    """GET /api/orgs (q unused; kept optional so the plain-route table can call it)."""
    denied = org_admin_guard()
    if denied:
        return denied
    st = load_orgs()
    live = set(existing_accounts())
    counts = {}
    for addr, oid in st["assignments"].items():
        if addr in live and oid:
            counts[oid] = counts.get(oid, 0) + 1
    orgs = [{"id": o["id"], "name": o.get("name", ""),
             "color": o.get("color", "#b4533a"),
             "builtin": bool(o.get("builtin")),
             "created_at": o.get("created_at", ""),
             "count": counts.get(o["id"], 0)} for o in st["orgs"]]
    assigned = sum(1 for addr, oid in st["assignments"].items() if addr in live and oid)
    return {"ok": True, "orgs": orgs, "assignments": st["assignments"],
            "total_accounts": len(live), "unassigned": max(0, len(live) - assigned),
            "updated_at": st.get("updated_at", "")}


def api_org_add(p):
    denied = org_admin_guard()
    if denied:
        return denied
    st = load_orgs()
    name, err = org_name_ok(p.get("name", ""), st)
    if err:
        return {"ok": False, "msg": err}
    color = str(p.get("color", "") or "").strip()
    if not ORG_COLOR_RE.match(color):
        color = ORG_PALETTE[len(st["orgs"]) % len(ORG_PALETTE)]
    oid = "org_" + secrets.token_hex(4)
    while org_by_id(st, oid):
        oid = "org_" + secrets.token_hex(4)
    st["orgs"].append({"id": oid, "name": name, "color": color,
                       "created_at": now_str(), "builtin": False})
    st["updated_at"] = now_str()
    save_org_state(st)
    audit("ORG_ADD", detail="%s / %s" % (oid, name))
    return {"ok": True, "msg": "组织「%s」已创建" % name, "id": oid}


def api_org_rename(p):
    denied = org_admin_guard()
    if denied:
        return denied
    st = load_orgs()
    oid = str(p.get("id", "") or "").strip()
    org = org_by_id(st, oid)
    if not org:
        return {"ok": False, "msg": "组织不存在"}
    before = org.get("name", "")
    name, err = org_name_ok(p.get("name", ""), st, exclude_id=oid)
    if err:
        return {"ok": False, "msg": err}
    org["name"] = name
    if "color" in p and ORG_COLOR_RE.match(str(p.get("color") or "")):
        org["color"] = str(p["color"])
    st["updated_at"] = now_str()
    save_org_state(st)
    audit("ORG_RENAME", detail="%s: %s -> %s" % (oid, before, name))
    return {"ok": True, "msg": "已更名为「%s」" % name}


def api_org_remove(p):
    denied = org_admin_guard()
    if denied:
        return denied
    st = load_orgs()
    oid = str(p.get("id", "") or "").strip()
    org = org_by_id(st, oid)
    if not org:
        return {"ok": False, "msg": "组织不存在"}
    if len(st["orgs"]) <= 1:
        return {"ok": False, "msg": "至少保留一个组织"}
    moved = 0
    for addr, val in list(st["assignments"].items()):
        if val == oid:
            del st["assignments"][addr]
            moved += 1
    st["orgs"] = [o for o in st["orgs"] if o.get("id") != oid]
    st["updated_at"] = now_str()
    save_org_state(st)
    audit("ORG_REMOVE", detail="%s / %s / %d mailboxes unassigned"
          % (oid, org.get("name", ""), moved))
    return {"ok": True, "msg": "组织「%s」已删除，%d 个邮箱变为未分配"
            % (org.get("name", ""), moved)}


def parse_bulk_emails(raw):
    if isinstance(raw, list):
        items = [str(x).strip().lower() for x in raw]
    else:
        text = str(raw or "")
        if text.strip().startswith("["):
            try:
                items = [str(x).strip().lower() for x in json.loads(text)]
            except Exception:
                items = []
        else:
            items = [x.strip().lower() for x in re.split(r"[\s,;]+", text) if x.strip()]
    return [x for x in dict.fromkeys(items) if EMAIL_RE.match(x)][:500]


def api_account_org(p):
    denied = org_admin_guard()
    if denied:
        return denied
    addr = str(p.get("email", "") or "").strip().lower()
    if not EMAIL_RE.match(addr):
        return {"ok": False, "msg": "邮箱地址不合法"}
    if addr not in existing_accounts():
        return {"ok": False, "msg": "账号不存在"}
    st = load_orgs()
    oid = str(p.get("org_id", "") or "").strip()
    if oid and not org_by_id(st, oid):
        return {"ok": False, "msg": "目标组织不存在"}
    if oid:
        st["assignments"][addr] = oid
    else:
        st["assignments"].pop(addr, None)
    st["updated_at"] = now_str()
    save_org_state(st)
    audit("ORG_ASSIGN", addr, detail="org=%s" % (oid or "unassigned"))
    name = org_by_id(st, oid)["name"] if oid else "未分配"
    return {"ok": True, "msg": "%s 已归入「%s」" % (addr, name),
            "org_id": oid, "org_name": name if oid else ""}


def api_account_org_bulk(p):
    denied = org_admin_guard()
    if denied:
        return denied
    st = load_orgs()
    oid = str(p.get("org_id", "") or "").strip()
    if oid and not org_by_id(st, oid):
        return {"ok": False, "msg": "目标组织不存在"}
    emails = parse_bulk_emails(p.get("emails", ""))
    if not emails:
        return {"ok": False, "msg": "没有可调整的账号"}
    live = set(existing_accounts())
    done, skipped = [], []
    for addr in emails:
        if addr not in live:
            skipped.append(addr)
            continue
        if oid:
            st["assignments"][addr] = oid
        else:
            st["assignments"].pop(addr, None)
        done.append(addr)
    if not done:
        return {"ok": False, "msg": "没有匹配的现存账号（%d 个地址不存在）" % len(skipped)}
    st["updated_at"] = now_str()
    save_org_state(st)
    audit("ORG_BULK", detail="org=%s / %d accounts" % (oid or "unassigned", len(done)))
    name = org_by_id(st, oid)["name"] if oid else "未分配"
    return {"ok": True, "msg": "已将 %d 个账号调整到「%s」%s"
            % (len(done), name, "（跳过 %d 个不存在的账号）" % len(skipped) if skipped else ""),
            "count": len(done), "skipped": skipped, "org_id": oid}


# ---------------------------------------------------------------- data APIs

def api_overview():
    services = {}
    for svc in (MADDY_SERVICE, NGINX_SERVICE, CERTBOT_TIMER, ADMIN_SERVICE):
        services[svc], _ = sh(["systemctl", "is-active", svc])
    mem, _ = sh(["systemctl", "show", MADDY_SERVICE, "-p", "MemoryCurrent", "--value"])
    try:
        mem_mb = round(int(mem) / 1048576, 1)
    except Exception:
        mem_mb = None
    uptime, _ = sh(["systemctl", "show", MADDY_SERVICE,
                    "-p", "ActiveEnterTimestamp", "--value"])
    version, _ = sh([MADDY, "version"])
    cert_end, _ = sh(["openssl", "x509", "-in", CERT, "-noout", "-enddate"])
    cert_subject, _ = sh(["openssl", "x509", "-in", CERT, "-noout", "-subject"])
    ip_cert_end = ""
    ip_days = None
    if IP_CERT and os.path.exists(IP_CERT):
        ip_cert_end, _ = sh(["openssl", "x509", "-in", IP_CERT, "-noout", "-enddate"])
        try:
            ip_exp = email.utils.parsedate_to_datetime(
                ip_cert_end.replace("notAfter=", "").strip())
            ip_days = (ip_exp - datetime.datetime.now(datetime.timezone.utc)).days
        except Exception:
            ip_days = None
    du = shutil.disk_usage("/")
    sts, _ = sh(["cat", MTA_STS_FILE])
    return {
        "services": services,
        "memory_mb": mem_mb,
        "uptime": uptime,
        "version": version.splitlines()[0] if version else "",
        "cert_end": cert_end.replace("notAfter=", ""),
        "cert_subject": cert_subject.replace("subject=", ""),
        "ip_cert_end": ip_cert_end.replace("notAfter=", ""),
        "ip_days": ip_days,
        "ip_access": IP_ACCESS,
        "disk": {"total_gb": round(du.total / 2**30, 1),
                 "free_gb": round(du.free / 2**30, 1)},
        "mta_sts": sts,
        "dns": {
            "A mail": dig("A", "mail." + DOMAIN),
            "A mta-sts": dig("A", "mta-sts." + DOMAIN),
            "MX": dig("MX", DOMAIN),
            "SPF": dig("TXT", DOMAIN),
            "DMARC": dig("TXT", "_dmarc." + DOMAIN),
            "MTA-STS": dig("TXT", "_mta-sts." + DOMAIN),
            "TLS-RPT": dig("TXT", "_smtp._tls." + DOMAIN),
            "DKIM": (dig("TXT", "default._domainkey." + DOMAIN)[:80] + "…"),
        },
        "ports": [str(SMTP_PORT), "80", "443", str(SMTPS_PORT),
                  str(SUBMISSION_PORT), str(IMAP_PORT)],
    }


def api_accounts():
    creds, rc1 = maddy_cli(["creds", "list"])
    accts, rc2 = maddy_cli(["imap-acct", "list"])
    c = {x.strip() for x in creds.splitlines() if "@" in x}
    a = {x.strip() for x in accts.splitlines() if "@" in x}
    ost = load_orgs()
    onames = {o["id"]: o.get("name", "") for o in ost["orgs"]}
    accounts = []
    for x in sorted(c | a):
        oid = ost["assignments"].get(x, "")
        accounts.append({"email": x, "creds": x in c, "imap": x in a,
                         "org_id": oid if oid in onames else "",
                         "org_name": onames.get(oid, "")})
    return {"ok": rc1 == 0 and rc2 == 0,
            "accounts": accounts,
            "orgs": [{"id": o["id"], "name": o.get("name", ""),
                      "color": o.get("color", "#b4533a")}
                     for o in ost["orgs"]],
            "raw": {"creds": creds, "imap": accts}}


def api_account_add(p):
    addr, pw = p.get("email", "").strip().lower(), p.get("password", "")
    if not EMAIL_RE.match(addr):
        return {"ok": False, "msg": "邮箱格式不合法，仅允许 xxx@example.com"}
    if len(pw) < 10 or "\n" in pw or "\r" in pw:
        return {"ok": False, "msg": "密码至少 10 位且不能含换行"}
    exists, _ = maddy_cli(["creds", "list"])
    if addr in [x.strip() for x in exists.splitlines()]:
        return {"ok": False, "msg": "账号已存在"}
    out1, rc1 = maddy_cli(["creds", "create", addr], stdin=pw + "\n")
    if rc1 != 0:
        return {"ok": False, "msg": "创建认证失败: " + out1}
    out2, rc2 = maddy_cli(["imap-acct", "create", addr])
    if rc2 != 0:
        maddy_cli(["creds", "remove", "-y", addr])
        return {"ok": False, "msg": "创建邮箱存储失败，已回滚: " + out2}
    store_credential(addr, pw)
    org_id = str(p.get("org_id", "") or "").strip()
    org_note = ""
    if org_id:
        st = load_orgs()
        org = org_by_id(st, org_id)
        if not org:
            return {"ok": False, "msg": "所选组织不存在"}
        st["assignments"][addr] = org_id
        st["updated_at"] = now_str()
        save_org_state(st)
        org_note = "，归属「%s」" % org["name"]
    return {"ok": True, "msg": "账号 %s 创建完成%s" % (addr, org_note)}


def _atomic_secret(path, text):
    """Atomically replace a 600-permission secret file owned by the service user."""
    import tempfile
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".secret-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def api_account_password(p):
    """Change one mailbox password and keep every consumer of it in sync."""
    global MAILPASS, AUTH_PASS
    addr, pw = p.get("email", "").strip().lower(), p.get("password", "")
    if not EMAIL_RE.match(addr):
        return {"ok": False, "msg": "邮箱地址不合法"}
    problem = password_problem(pw, addr)
    if problem:
        return {"ok": False, "msg": problem}
    if addr not in existing_accounts():
        return {"ok": False, "msg": "账号不存在"}
    out, rc = maddy_cli(["creds", "password", addr], stdin=pw + "\n")
    if rc != 0:
        return {"ok": False, "msg": "密码更新失败: " + (out or "").strip()[:120]}
    store_credential(addr, pw)
    # Keep batch provisioning records (and exported account lists) consistent.
    with BATCH_LOCK:
        st = load_state()
        touched = False
        for b in st.get("batches", []):
            for a in b.get("accounts", []):
                if a.get("email") == addr and a.get("final_password"):
                    a["final_password"] = pw
                    touched = True
        if touched:
            save_state(st)
    # The live session may belong to the account being changed: keep its
    # in-memory secret valid so the console does not break immediately.
    sess = getattr(CTX, "session", None)
    if sess and sess.get("account") == addr:
        sess["password"] = pw
    if addr == ADMIN_MAIL:
        # Internal automation (self-check, invitation mail) authenticates via
        # MAILPASS; refresh both secret files so a restart stays consistent.
        try:
            _atomic_secret(MAILPASS_FILE, pw + "\n")
            MAILPASS = pw
            lines = []
            try:
                lines = AUTH_FILE.read_text(encoding="utf-8").splitlines()
            except Exception:
                pass
            replaced = False
            for i, line in enumerate(lines):
                if line.startswith("ADMIN_PASSWORD="):
                    lines[i] = "ADMIN_PASSWORD=" + pw
                    replaced = True
            if not replaced:
                lines.append("ADMIN_PASSWORD=" + pw)
            _atomic_secret(AUTH_FILE, "\n".join(lines) + "\n")
            AUTH_PASS = pw
        except Exception as exc:
            return {"ok": False, "msg": "Maddy 密码已改，但同步系统凭据失败：%s（请立即联系运维）" % exc}
    audit("PASSWORD_CHANGE", addr, detail="admin-console")
    return {"ok": True, "msg": "密码已更新"}


def api_account_remove(p):
    addr = p.get("email", "").strip().lower()
    if not EMAIL_RE.match(addr):
        return {"ok": False, "msg": "参数不合法"}
    if addr == ADMIN_MAIL:
        return {"ok": False, "msg": "禁止删除主管理员账号"}
    o1, r1 = maddy_cli(["creds", "remove", "-y", addr])
    o2, r2 = maddy_cli(["imap-acct", "remove", "-y", addr])
    if r1 == 0 or r2 == 0:
        try:
            st = load_orgs()
            if addr in st["assignments"]:
                del st["assignments"][addr]
                st["updated_at"] = now_str()
                save_org_state(st)
        except Exception:
            pass
    return {"ok": r1 == 0 or r2 == 0,
            "msg": "已删除 %s（认证: %s / 存储: %s）" % (addr, o1.strip(), o2.strip())}


def api_inbox(q):
    try:
        limit = max(1, min(int(q.get("limit", ["20"])[0]), 50))
    except Exception:
        limit = 20
    m = imap_session()
    try:
        m.select("INBOX", readonly=True)
        typ, data = m.search(None, "ALL")
        ids = data[0].split()
        result = []
        for num in reversed(ids[-limit:]):
            typ, raw = m.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if not raw or not raw[0]:
                continue
            msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw[0][1])
            result.append({"id": num.decode(),
                           "from": decode_mime(msg.get("From", "")),
                           "subject": decode_mime(msg.get("Subject", "")) or "(无主题)",
                           "date": msg.get("Date", "")})
        return {"ok": True, "count": len(ids), "messages": result}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_message(q):
    num = q.get("id", [""])[0]
    if not num.isdigit():
        return {"ok": False, "body": "非法邮件编号"}
    m = imap_session()
    try:
        m.select("INBOX", readonly=True)
        typ, raw = m.fetch(num.encode(), "(RFC822)")
        if not raw or not raw[0]:
            return {"ok": False, "body": "邮件不存在"}
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw[0][1])
        part = msg.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part else "(无正文)"
        if part and part.get_content_type() == "text/html":
            body = re.sub(r"<[^>]+>", " ", body)
        return {"ok": True, "from": decode_mime(msg.get("From", "")),
                "subject": decode_mime(msg.get("Subject", "")),
                "date": msg.get("Date", ""), "body": html.unescape(body)[:20000]}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_send(p):
    to = p.get("to", "").strip()
    subject = p.get("subject", "").strip() or "(无主题)"
    body = p.get("body", "").strip()
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", to) or not body:
        return {"ok": False, "msg": "收件人或正文不合法"}
    msg = email.message.EmailMessage()
    msg["From"] = ADMIN_MAIL
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("127.0.0.1", SMTPS_PORT, context=local_ssl_context(), timeout=25) as s:
            s.login(ADMIN_MAIL, MAILPASS)
            s.send_message(msg)
        return {"ok": True, "msg": "已提交投递至 %s" % to}
    except Exception as exc:
        return {"ok": False, "msg": "发信失败: %s" % exc}


# ---------------------------------------------------------------- mail client (v4)

CRED_FILE = BATCH_DIR / "credentials.json"
CRED_LOCK = threading.RLock()
FOLDER_RE = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")
KNOWN_FOLDERS = ("INBOX", "Sent", "Drafts", "Junk", "Trash", "Archive")
FOLDER_LABELS = {
    "INBOX": "收件箱", "Sent": "已发送", "Drafts": "草稿箱",
    "Junk": "垃圾邮件", "Trash": "已删除", "Archive": "归档",
}
FOLDER_ICONS = {
    "INBOX": "inbox", "Sent": "send", "Drafts": "draft",
    "Junk": "junk", "Trash": "trash", "Archive": "archive",
}


def load_creds():
    with CRED_LOCK:
        try:
            return json.loads(CRED_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


def store_credential(addr, pw):
    """Remember a mailbox password so the admin console can open that mailbox."""
    if not addr or not pw:
        return
    with CRED_LOCK:
        data = load_creds()
        data[addr] = pw
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CRED_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CRED_FILE)


def account_password(addr):
    # v5: a logged-in user's password exists only in the in-memory session.
    sess = getattr(CTX, "session", None)
    if sess and sess.get("account") == addr and sess.get("password"):
        return sess["password"]
    if addr == ADMIN_MAIL:
        return MAILPASS
    pw = load_creds().get(addr)
    if pw:
        return pw
    for b in load_state().get("batches", []):
        for a in b.get("accounts", []):
            if a.get("email") == addr and a.get("final_password"):
                return a["final_password"]
    return None


def existing_accounts():
    out, _ = maddy_cli(["creds", "list"])
    return sorted({x.strip().lower() for x in out.splitlines()
                   if "@" in x and EMAIL_RE.match(x.strip().lower())})


def qget(src, key, default=""):
    """Normalise parse_qs (list values) and read_form (string values)."""
    val = src.get(key, default)
    if isinstance(val, (list, tuple)):
        return val[0] if val else default
    return val if val is not None else default


def safe_account(q, default=ADMIN_MAIL):
    addr = str(qget(q, "account", default) or default).strip().lower()
    if addr not in existing_accounts():
        raise ValueError("账号不存在")
    return addr


def safe_folder(q, default="INBOX"):
    folder = str(qget(q, "folder", default) or default).strip()
    if not FOLDER_RE.match(folder) or ".." in folder:
        raise ValueError("文件夹名称不合法")
    return folder


def mail_login(addr):
    pw = account_password(addr)
    if not pw:
        return None, "该账号没有可用的登录凭据（管理员无法代开邮箱）"
    try:
        return imap_session(addr, pw), None
    except Exception as exc:
        return None, "IMAP 登录失败: %s" % exc


def parse_address(value):
    try:
        pairs = email.utils.getaddresses([value or ""])
    except Exception:
        pairs = []
    out = []
    for name, addr in pairs:
        if not addr:
            continue
        out.append({"name": decode_mime(name) or addr.split("@")[0], "addr": addr})
    return out


def first_address(value):
    lst = parse_address(value)
    return lst[0] if lst else {"name": value or "(未知发件人)", "addr": ""}


def imap_date(value):
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp()) if dt else 0
    except Exception:
        return 0


def folder_counts(m, folder):
    """(total, unseen) with a STATUS, falling back to a read-only SELECT."""
    try:
        typ, data = m.status('"%s"' % folder, "(MESSAGES UNSEEN)")
        if typ == "OK" and data and isinstance(data[0], bytes):
            text = data[0].decode("utf-8", "replace")
            total = re.search(r"MESSAGES (\d+)", text)
            unseen = re.search(r"UNSEEN (\d+)", text)
            return (int(total.group(1)) if total else 0,
                    int(unseen.group(1)) if unseen else 0)
    except Exception:
        pass
    try:
        typ, data = m.select('"%s"' % folder, readonly=True)
        total = int(data[0]) if data and data[0] else 0
        typ, res = m.search(None, "UNSEEN")
        unseen = len(res[0].split()) if res and res[0] else 0
        return total, unseen
    except Exception:
        return 0, 0


def fetch_headers(m, seqs):
    """Batch-fetch flags + headers for a set of IMAP sequence numbers."""
    if not seqs:
        return {}
    seqset = ",".join(str(s) for s in seqs)
    typ, data = m.fetch(seqset, "(FLAGS INTERNALDATE RFC822.SIZE "
                                "BODY.PEEK[HEADER.FIELDS "
                                "(FROM TO CC SUBJECT DATE MESSAGE-ID)])")
    if typ != "OK":
        return {}
    out = {}
    for item in data or []:
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        meta, payload = item
        head = re.match(rb"^(\d+)\s", meta)
        if not head:
            continue
        seq = int(head.group(1))
        flags = re.search(rb"FLAGS \(([^)]*)\)", meta)
        flagtxt = (flags.group(1).decode("utf-8", "replace") if flags else "")
        idate = re.search(rb'INTERNALDATE "([^"]*)"', meta)
        size = re.search(rb"RFC822.SIZE (\d+)", meta)
        try:
            hdr = email.parser.BytesHeaderParser(
                policy=email.policy.default).parsebytes(payload)
        except Exception:
            continue
        out[seq] = {
            "id": str(seq),
            "from": first_address(hdr.get("From", "")),
            "to": parse_address(hdr.get("To", "")),
            "cc": parse_address(hdr.get("Cc", "")),
            "subject": decode_mime(hdr.get("Subject", "")) or "(无主题)",
            "date": decode_mime(hdr.get("Date", "")),
            "ts": imap_date(hdr.get("Date", "")) or imap_date(
                idate.group(1).decode() if idate else ""),
            "message_id": (hdr.get("Message-ID", "") or "").strip(),
            "size": int(size.group(1)) if size else 0,
            "seen": "\\Seen" in flagtxt,
            "flagged": "\\Flagged" in flagtxt,
            "flags": flagtxt,
        }
    return out


def fetch_snippet(m, seq):
    for spec in ("(BODY.PEEK[TEXT]<0.400>)", "(BODY.PEEK[TEXT])"):
        try:
            typ, data = m.fetch(str(seq), spec)
            if typ != "OK" or not data:
                continue
            raw = b""
            for item in data:
                if isinstance(item, tuple) and len(item) == 2:
                    raw = item[1]
                    break
            if not raw:
                continue
            text = raw.decode("utf-8", "replace")
            text = re.sub(r"(?is)<[^>]+>", " ", text)
            text = re.sub(r"(?m)^\s*(Content-|MIME-|boundary=|--).*$", " ", text)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:120]
        except Exception:
            continue
    return ""


def fetch_extras(m, seq, size=0):
    """Return (snippet, has_attachment) by parsing the decoded MIME body."""
    if size > 2 * 1024 * 1024:
        return "(大邮件，点击阅读正文)", True
    raw = fetch_raw(m, seq)
    if raw is None:
        return "", False
    try:
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
        text, has_att = "", False
        for part in msg.walk():
            if part.is_multipart():
                continue
            if (part.get_content_disposition() or "") == "attachment" \
                    or part.get_filename():
                has_att = True
                continue
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html") or text:
                continue
            try:
                text = part.get_content()
            except Exception:
                try:
                    text = (part.get_payload(decode=True) or b"").decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    text = ""
            if ctype == "text/html":
                text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        return text[:120], has_att
    except Exception:
        return "", False


def fetch_raw(m, seq):
    typ, data = m.fetch(str(seq), "(BODY.PEEK[])")
    if typ != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            return item[1]
    return None


BLANK_GIF = ("data:image/gif;base64,"
             "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def sanitize_html(src):
    """Strip active content and remote images from an HTML mail body."""
    s = src or ""
    s = re.sub(r"(?is)<!--.*?-->", "", s)
    s = re.sub(r"(?is)<(script|style|iframe|object|embed|form|template|"
               r"svg|math|audio|video|source|base|meta|link)\b[^>]*>.*?</\1\s*>", "", s)
    s = re.sub(r"(?is)<(script|iframe|object|embed|form|template|svg|math|"
               r"audio|video|source|base|meta|link|input|button|textarea|select)\b[^>]*/?>", "", s)
    # Remove event handlers and dangerous URL schemes.
    s = re.sub(r"(?is)\son[a-z0-9_:-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", s)
    s = re.sub(r"(?is)\s(href|src|action|formaction|xlink:href|srcdoc|data)\s*=\s*"
               r"(?:\"\s*(?:javascript|vbscript|data:text/html)[^\"]*\"|"
               r"'\s*(?:javascript|vbscript|data:text/html)[^']*'|"
               r"(?:javascript|vbscript)[^\s>]+)", "", s)
    # Neutralise risky inline styles but keep harmless formatting.
    def _style(match):
        val = match.group(0)
        if re.search(r"(?i)expression\s*\(|javascript:|behavior\s*:|@import|"
                     r"url\(\s*['\"]?\s*(?:javascript|data:text)", val):
            return ""
        return val
    s = re.sub(r'(?is)\sstyle\s*=\s*"[^"]*"', _style, s)

    def _img(match):
        tag = match.group(0)
        if re.search(r"(?i)\ssrc\s*=\s*['\"]?\s*(?:https?:)?//", tag):
            tag = re.sub(r"(?is)\ssrc\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
                         ' src="%s" data-remote="1"' % BLANK_GIF, tag)
        return tag
    s = re.sub(r"(?is)<img\b[^>]*>", _img, s)

    def _anchor(match):
        tag = match.group(0)
        tag = re.sub(r"(?is)\starget\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", tag)
        return tag.replace("<a", '<a target="_blank" rel="noopener noreferrer"', 1)
    s = re.sub(r"(?is)<a\b[^>]*>", _anchor, s)

    css = ("<style>body{margin:0;padding:18px 20px;color:#26221d;background:#fffdf9;"
           "font:14px/1.75 -apple-system,BlinkMacSystemFont,'PingFang SC',"
           "'Microsoft YaHei',sans-serif;word-break:break-word}"
           "img{max-width:100%;height:auto}a{color:#b4533a}"
           "blockquote{border-left:2px solid #e5ded2;margin:12px 0;padding:2px 14px;color:#7a7268}"
           "table{max-width:100%}pre{white-space:pre-wrap}</style>")
    return css + s


def parse_full_message(raw):
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    headers = {k: decode_mime(v) for k, v in msg.items()}
    text, html_body, attachments = "", "", []
    for idx, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition() or ""
        filename = decode_mime(part.get_filename() or "")
        if disp == "attachment" or filename:
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            attachments.append({
                "part": idx, "filename": filename or "attachment-%d.bin" % idx,
                "content_type": ctype, "size": len(payload),
                "inline": disp == "inline",
            })
        elif ctype == "text/plain" and not text:
            try:
                text = part.get_content()
            except Exception:
                try:
                    text = (part.get_payload(decode=True) or b"").decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    text = ""
        elif ctype == "text/html" and not html_body:
            try:
                html_body = part.get_content()
            except Exception:
                try:
                    html_body = (part.get_payload(decode=True) or b"").decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    html_body = ""
    if not text and html_body:
        text = re.sub(r"\s+", " ", html.unescape(
            re.sub(r"(?is)<[^>]+>", " ", html_body))).strip()
    body_plain = html.escape(text[:200000])
    return {
        "headers": headers,
        "from": first_address(msg.get("From", "")),
        "to": parse_address(msg.get("To", "")),
        "cc": parse_address(msg.get("Cc", "")),
        "reply_to": parse_address(msg.get("Reply-To", "")),
        "subject": decode_mime(msg.get("Subject", "")) or "(无主题)",
        "date": decode_mime(msg.get("Date", "")),
        "ts": imap_date(msg.get("Date", "")),
        "message_id": (msg.get("Message-ID", "") or "").strip(),
        "in_reply_to": (msg.get("In-Reply-To", "") or "").strip(),
        "references": (msg.get("References", "") or "").strip(),
        "text": body_plain,
        "html": sanitize_html(html_body[:300000]) if html_body else "",
        "attachments": attachments,
        "raw_headers": "\n".join("%s: %s" % (k, v) for k, v in msg.items())[:20000],
    }


def api_mail_identities(q):
    return {"ok": True, "admin": ADMIN_MAIL,
            "accounts": [{"email": a, "available": bool(account_password(a)),
                          "is_admin": a == ADMIN_MAIL} for a in existing_accounts()]}


def api_mail_folders(q):
    addr = safe_account(q)
    m, err = mail_login(addr)
    if err:
        return {"ok": False, "msg": err}
    try:
        typ, boxes = m.list()
        names = set()
        for line in boxes or []:
            try:
                text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                match = re.search(r'\s"([^"]+)"\s*$', text)
                name = match.group(1) if match else text.split()[-1].strip('"')
                if name and FOLDER_RE.match(name):
                    names.add(name)
            except Exception:
                continue
        for f in KNOWN_FOLDERS:
            names.add(f)
        ordered = [f for f in KNOWN_FOLDERS if f in names] + sorted(
            names - set(KNOWN_FOLDERS))
        folders = []
        for f in ordered:
            total, unseen = folder_counts(m, f)
            folders.append({"name": f, "label": FOLDER_LABELS.get(f, f),
                            "icon": FOLDER_ICONS.get(f, "folder"),
                            "total": total, "unseen": unseen})
        return {"ok": True, "account": addr, "folders": folders,
                "total_unseen": sum(x["unseen"] for x in folders)}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_mail_list(q):
    addr = safe_account(q)
    folder = safe_folder(q)
    try:
        page = max(1, min(int(qget(q, "page", "1")), 100000))
        size = max(5, min(int(qget(q, "size", "25")), 100))
    except Exception:
        page, size = 1, 25
    needle = str(qget(q, "q", "") or "").strip().lower()
    mode = str(qget(q, "filter", "all") or "all").strip()
    m, err = mail_login(addr)
    if err:
        return {"ok": False, "msg": err}
    try:
        typ, data = m.select('"%s"' % folder, readonly=True)
        if typ != "OK":
            return {"ok": False, "msg": "无法打开文件夹 %s" % folder}
        typ, res = m.search(None, "ALL")
        ids = [int(x) for x in (res[0].split() if res and res[0] else [])]
        if not ids:
            return {"ok": True, "account": addr, "folder": folder,
                    "total": 0, "page": 1, "pages": 1, "size": size,
                    "messages": [], "q": needle, "filter": mode}
        # Fetch headers in chunks to keep IMAP commands reasonably sized.
        headers = {}
        for start in range(0, len(ids), 400):
            headers.update(fetch_headers(m, ids[start:start + 400]))
        rows = [headers[i] for i in sorted(headers, reverse=True)]
        if mode == "unseen":
            rows = [r for r in rows if not r["seen"]]
        elif mode == "seen":
            rows = [r for r in rows if r["seen"]]
        elif mode == "flagged":
            rows = [r for r in rows if r["flagged"]]
        if needle:
            rows = [r for r in rows if needle in " ".join([
                r["subject"], r["from"]["name"], r["from"]["addr"],
                " ".join(x["addr"] for x in r["to"]), r["date"]]).lower()]
        total = len(rows)
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        chunk = rows[(page - 1) * size:page * size]
        for r in chunk:
            r["snippet"], r["has_attachment"] = fetch_extras(m, r["id"], r["size"])
        return {"ok": True, "account": addr, "folder": folder,
                "total": total, "page": page, "pages": pages, "size": size,
                "messages": chunk, "q": needle, "filter": mode}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_mail_read(q):
    addr = safe_account(q)
    folder = safe_folder(q)
    seq = str(qget(q, "id", "") or "")
    if not seq.isdigit():
        return {"ok": False, "msg": "非法邮件编号"}
    m, err = mail_login(addr)
    if err:
        return {"ok": False, "msg": err}
    try:
        typ, _ = m.select('"%s"' % folder, readonly=True)
        if typ != "OK":
            return {"ok": False, "msg": "无法打开文件夹"}
        raw = fetch_raw(m, seq)
        if raw is None:
            return {"ok": False, "msg": "邮件不存在"}
        detail = parse_full_message(raw)
        typ, fres = m.fetch(seq, "(FLAGS)")
        flagtxt = fres[0].decode("utf-8", "replace") if fres and fres[0] else ""
        detail["seen"] = "\\Seen" in flagtxt
        detail["flagged"] = "\\Flagged" in flagtxt
        detail["id"] = seq
        detail["folder"] = folder
        detail["account"] = addr
        return {"ok": True, "message": detail}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_mail_attachment(q):
    """Return (bytes, filename, ctype) for a MIME part or the raw .eml."""
    addr = safe_account(q)
    folder = safe_folder(q)
    seq = str(qget(q, "id", "") or "")
    if not seq.isdigit():
        raise ValueError("非法邮件编号")
    m, err = mail_login(addr)
    if err:
        raise ValueError(err)
    try:
        typ, _ = m.select('"%s"' % folder, readonly=True)
        if typ != "OK":
            raise ValueError("无法打开文件夹")
        raw = fetch_raw(m, seq)
        if raw is None:
            raise ValueError("邮件不存在")
        if qget(q, "mode", "") == "raw":
            return raw, "message-%s.eml" % seq, "message/rfc822"
        part_no = int(qget(q, "part", "0"))
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
        parts = list(msg.walk())
        if part_no < 0 or part_no >= len(parts):
            raise ValueError("附件编号不存在")
        part = parts[part_no]
        data = part.get_payload(decode=True) or b""
        name = decode_mime(part.get_filename() or "") or "attachment.bin"
        return data, name, part.get_content_type() or "application/octet-stream"
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_mail_action(p):
    addr = safe_account(p)
    folder = safe_folder(p)
    action = (p.get("action", "") or "").strip()
    ids = [x for x in re.split(r"[,\s]+", p.get("ids", "")) if x.isdigit()]
    if not ids or action not in ("seen", "unseen", "flag", "unflag",
                                 "trash", "delete"):
        return {"ok": False, "msg": "参数不合法"}
    dest = (p.get("dest", "") or "").strip()
    m, err = mail_login(addr)
    if err:
        return {"ok": False, "msg": err}
    try:
        typ, _ = m.select('"%s"' % folder, readonly=False)
        if typ != "OK":
            return {"ok": False, "msg": "无法打开文件夹"}
        seqset = ",".join(ids)
        if action == "seen":
            m.store(seqset, "+FLAGS", "(\\Seen)")
        elif action == "unseen":
            m.store(seqset, "-FLAGS", "(\\Seen)")
        elif action == "flag":
            m.store(seqset, "+FLAGS", "(\\Flagged)")
        elif action == "unflag":
            m.store(seqset, "-FLAGS", "(\\Flagged)")
        elif action == "trash":
            if folder != "Trash" and dest in KNOWN_FOLDERS:
                m.copy(seqset, '"%s"' % dest)
            m.store(seqset, "+FLAGS", "(\\Deleted)")
            m.expunge()
        elif action == "delete":
            m.store(seqset, "+FLAGS", "(\\Deleted)")
            m.expunge()
        # Do not CLOSE: CLOSE expunges *all* \Deleted messages, including ones
        # flagged by other IMAP clients. LOGOUT leaves the mailbox untouched.
        return {"ok": True, "msg": "已处理 %d 封邮件" % len(ids),
                "action": action, "ids": ids}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def build_outgoing(p, addr):
    def addr_list(key):
        return [x["addr"] for x in parse_address(p.get(key, ""))
                if x["addr"] and re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", x["addr"])]
    to = addr_list("to")
    cc = addr_list("cc")
    bcc = addr_list("bcc")
    subject = (p.get("subject", "") or "").strip() or "(无主题)"
    body = p.get("body", "") or ""
    msg = email.message.EmailMessage()
    msg["From"] = addr
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=DOMAIN)
    if p.get("in_reply_to"):
        msg["In-Reply-To"] = p["in_reply_to"].strip()
        refs = (p.get("references", "") or "").strip()
        msg["References"] = (refs + " " + msg["In-Reply-To"]).strip()
    msg.set_content(body or "(空正文)")
    try:
        attachments = json.loads(p.get("attachments", "[]") or "[]")
    except Exception:
        attachments = []
    for att in attachments[:10]:
        try:
            raw = base64.b64decode(att.get("data", ""), validate=True)
            name = re.sub(r"[\r\n/\\]", "_", str(att.get("name", "file.bin")))[:120]
            ctype = str(att.get("type", "")) or "application/octet-stream"
            if "/" in ctype:
                main, sub = ctype.split("/", 1)
                msg.add_attachment(raw, maintype=main, subtype=sub, filename=name)
            else:
                msg.add_attachment(raw, maintype="application",
                                   subtype="octet-stream", filename=name)
        except Exception:
            continue
    return msg, to + cc + bcc, list(attachments)


def api_mail_compose(p):
    addr = safe_account(p)
    action = (p.get("action", "send") or "send").strip()
    if action not in ("send", "draft"):
        return {"ok": False, "msg": "未知操作"}
    pw = account_password(addr)
    if not pw:
        return {"ok": False, "msg": "该账号没有可用的登录凭据"}
    msg, recipients, attachments = build_outgoing(p, addr)
    if action == "send" and not recipients:
        return {"ok": False, "msg": "请至少填写一个有效收件人"}
    if action == "draft":
        m, err = mail_login(addr)
        if err:
            return {"ok": False, "msg": err}
        try:
            typ, _ = m.append('"Drafts"', "(\\Seen \\Draft)", None, msg.as_bytes())
            if typ != "OK":
                return {"ok": False, "msg": "草稿保存失败（IMAP APPEND 被拒绝）"}
            return {"ok": True, "msg": "草稿已保存到 Drafts",
                    "attachments": len(attachments)}
        finally:
            try:
                m.logout()
            except Exception:
                pass
    try:
        with smtplib.SMTP_SSL("127.0.0.1", 465,
                              context=local_ssl_context(), timeout=30) as s:
            s.login(addr, pw)
            s.send_message(msg, to_addrs=recipients)
    except Exception as exc:
        return {"ok": False, "msg": "发信失败: %s" % exc}
    draft_id = p.get("draft_id", "")
    if draft_id.isdigit():
        try:
            m, err = mail_login(addr)
            if not err:
                m.select('"Drafts"', readonly=False)
                m.store(draft_id, "+FLAGS", "(\\Deleted)")
                m.expunge()
                m.logout()
        except Exception:
            pass
    return {"ok": True, "msg": "邮件已提交投递（%d 位收件人，%d 个附件）"
            % (len(recipients), len(attachments)),
            "recipients": recipients}


# ---------------------------------------------------------------- legacy data APIs

def api_logs(q):
    try:
        n = max(10, min(int(q.get("lines", ["80"])[0]), 300))
    except Exception:
        n = 80
    out, _ = sh(["journalctl", "-u", MADDY_SERVICE, "-n", str(n), "--no-pager",
                 "--output=short-iso"], timeout=15)
    return {"ok": True, "lines": out.splitlines()}


def api_check():
    """Operator-facing health check; every expectation comes from config."""
    checks = []

    def add(name, value, expect=None, ok=None):
        if ok is None:
            ok = bool(expect) and expect in str(value)
        checks.append({"name": name, "value": str(value)[:180], "ok": bool(ok)})

    add("A %s" % MAIL_HOSTNAME, dig("A", MAIL_HOSTNAME), SERVER_IP)
    add("MX %s" % DOMAIN, dig("MX", DOMAIN), MAIL_HOSTNAME)
    add("SPF", dig("TXT", DOMAIN), "v=spf1")
    dkim = dig("TXT", "default._domainkey." + DOMAIN)
    add("DKIM", ("长度 %d 字符" % len(dkim)) if dkim else "缺失", ok=len(dkim) > 300)
    add("DMARC", dig("TXT", "_dmarc." + DOMAIN), "v=DMARC1")
    add("MTA-STS TXT", dig("TXT", "_mta-sts." + DOMAIN), "v=STSv1")
    add("TLS-RPT", dig("TXT", "_smtp._tls." + DOMAIN), "v=TLSRPTv1")
    add("SRV submission", dig("SRV", "_submission._tcp." + DOMAIN), str(SUBMISSION_PORT))
    add("SRV imaps", dig("SRV", "_imaps._tcp." + DOMAIN), str(IMAP_PORT))
    if os.path.exists(MTA_STS_FILE):
        sts, _ = sh(["curl", "-sS", "-m", "6",
                     "https://mta-sts.%s/.well-known/mta-sts.txt" % DOMAIN])
        add("MTA-STS 策略", sts.replace("\n", " ")[:120], "mode: enforce")
    if os.path.exists(CERT):
        cert, _ = sh(["openssl", "x509", "-in", CERT, "-noout", "-issuer"])
        end, _ = sh(["openssl", "x509", "-in", CERT, "-noout", "-enddate"])
        add("TLS 证书", "%s / %s" % (cert.replace("issuer=", ""),
                                     end.replace("notAfter=", "")), ok=bool(cert))
    if IP_CERT and os.path.exists(IP_CERT):
        ip_end, _ = sh(["openssl", "x509", "-in", IP_CERT, "-noout", "-enddate"])
        try:
            ip_exp = email.utils.parsedate_to_datetime(
                ip_end.replace("notAfter=", "").strip())
            ip_left = (ip_exp - datetime.datetime.now(datetime.timezone.utc)).days
        except Exception:
            ip_left = -1
        add("IP 证书(%s)" % SERVER_IP,
            "%s，剩余 %s 天" % (ip_end.replace("notAfter=", ""), ip_left),
            ok=ip_left >= 2)
    tls_results = []
    for port in (SMTPS_PORT, IMAP_PORT):
        tls_ok = False
        for _ in range(2):
            out, _ = sh(["timeout", "8", "openssl", "s_client", "-connect",
                         "127.0.0.1:%d" % port, "-servername", MAIL_HOSTNAME],
                        timeout=12)
            if "issuer=" in out:
                tls_ok = True
                break
        tls_results.append("%s:%s" % (port, "OK" if tls_ok else "FAIL"))
    add("本地 TLS 端点", "  ".join(tls_results), ok=all("OK" in x for x in tls_results))
    ptr, _ = sh(["dig", "+time=3", "+tries=1", "+short", "-x", SERVER_IP])
    add("PTR 反向解析", ptr or "未设置（多为云厂商付费项，不影响基本收发）", ok=bool(ptr))
    probe_ok = False
    for _ in range(2):
        _, prc = sh(["timeout", "7", "bash", "-c",
                     "exec 3<>/dev/tcp/163mx01.mxmail.netease.com/25"], timeout=10)
        if prc == 0:
            probe_ok = True
            break
    add("出站 25 端口", "可达" if probe_ok else "不可达 / 被云厂商封禁", ok=probe_ok)
    return {"ok": all(c["ok"] for c in checks if "PTR" not in c["name"]),
            "checks": checks}


# ---------------------------------------------------------------- batch APIs

def api_batch_status(q):
    st = load_state()
    batch = find_batch(st, q.get("batch_id", [""])[0] or None)
    if not batch:
        return {"ok": True, "batch": None, "batches": [b["id"] for b in st["batches"]]}
    accts = batch["accounts"]
    return {"ok": True,
            "batch": {"id": batch["id"], "created_at": batch["created_at"],
                      "accounts": [public_account(a) for a in accts]},
            "summary": {"total": len(accts),
                        "pending": sum(1 for a in accts if a["status"] == "pending"),
                        "activated": sum(1 for a in accts if a["status"] == "activated"),
                        "invited": sum(1 for a in accts if a.get("invited_at"))},
            "batches": [b["id"] for b in st["batches"]]}


def api_batch_create(p):
    try:
        count = int(p.get("count", "10"))
    except Exception:
        count = 10
    count = max(1, min(count, 500))
    with BATCH_LOCK:
        st = load_state()
        existing = set()
        creds, _ = maddy_cli(["creds", "list"])
        existing |= {x.strip() for x in creds.splitlines() if "@" in x}
        for b in st["batches"]:
            existing |= {a["email"] for a in b["accounts"]}
        accounts, errors = [], []
        for _ in range(count):
            email = None
            for _try in range(60):
                cand = "%s%d@%s" % (secrets.choice(WORDS),
                                    secrets.randbelow(9000) + 1000, DOMAIN)
                if cand not in existing:
                    email = cand
                    break
            if not email:
                errors.append("无法生成唯一邮箱名，已跳过")
                continue
            existing.add(email)
            temp_pw = "Tmp-" + secrets.token_urlsafe(18)
            res = api_account_add({"email": email, "password": temp_pw})
            if not res["ok"]:
                errors.append("%s: %s" % (email, res["msg"]))
                continue
            token = secrets.token_urlsafe(24)
            accounts.append({
                "email": email,
                "temp_password": temp_pw,
                "token": token,
                "activate_url": ACTIVATE_BASE + token,
                "status": "pending",
                "created_at": now_str(),
                "created_ts": time.time(),
                "invited_at": None,
                "activated_at": None,
                "activated_by": None,
                "final_password": None,
            })
        if not accounts:
            return {"ok": False, "msg": "没有成功创建任何账号", "errors": errors}
        batch = {"id": datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                       + "-" + secrets.token_hex(2),
                 "created_at": now_str(), "accounts": accounts}
        st["batches"].append(batch)
        save_state(st)
    return {"ok": True, "batch_id": batch["id"],
            "count": len(accounts), "errors": errors,
            "accounts": [public_account(a) for a in accounts],
            "msg": "批量创建 %d 个账号完成" % len(accounts)}


def api_batch_create_custom(p):
    """按自定义名单批量创建。名单可用换行/逗号/分号/空白分隔，
    可写完整邮箱或仅 local part（自动补 @example.com）。
    meta 为可选 JSON：{email: {cn_name, pinyin, category, rule, index}}。
    """
    raw = p.get("emails", "") or ""
    items = [x.strip().lower() for x in re.split(r"[\s,;]+", raw) if x.strip()]
    emails = [x if "@" in x else "%s@%s" % (x, DOMAIN) for x in items]
    if not emails:
        return {"ok": False, "msg": "名单为空"}
    if len(emails) > 500:
        return {"ok": False, "msg": "单批最多 500 个邮箱"}
    if len(set(emails)) != len(emails):
        return {"ok": False, "msg": "名单中存在重复邮箱"}
    try:
        meta = json.loads(p.get("meta") or "{}")
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta_keys = ("cn_name", "pinyin", "category", "category_name", "rule", "index")
    with BATCH_LOCK:
        st = load_state()
        existing = set()
        creds, _ = maddy_cli(["creds", "list"])
        existing |= {x.strip() for x in creds.splitlines() if "@" in x}
        for b in st["batches"]:
            existing |= {a["email"] for a in b["accounts"]}
        accounts, errors = [], []
        for e in emails:
            if e == ADMIN_MAIL:
                errors.append("%s: 禁止操作主管理员账号" % e)
                continue
            if not EMAIL_RE.match(e):
                errors.append("%s: 邮箱格式不合法" % e)
                continue
            if e in existing:
                errors.append("%s: 已存在，跳过" % e)
                continue
            temp_pw = "Tmp-" + secrets.token_urlsafe(18)
            res = api_account_add({"email": e, "password": temp_pw})
            if not res["ok"]:
                errors.append("%s: %s" % (e, res["msg"]))
                continue
            existing.add(e)
            token = secrets.token_urlsafe(24)
            rec = {
                "email": e,
                "temp_password": temp_pw,
                "token": token,
                "activate_url": ACTIVATE_BASE + token,
                "status": "pending",
                "created_at": now_str(),
                "created_ts": time.time(),
                "invited_at": None,
                "activated_at": None,
                "activated_by": None,
                "final_password": None,
            }
            m = meta.get(e, {})
            if isinstance(m, dict):
                for k in meta_keys:
                    if k in m:
                        rec[k] = m[k]
            accounts.append(rec)
        if not accounts:
            return {"ok": False, "msg": "没有成功创建任何账号", "errors": errors}
        batch = {"id": datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                       + "-" + secrets.token_hex(2),
                 "created_at": now_str(), "accounts": accounts}
        st["batches"].append(batch)
        save_state(st)
    return {"ok": True, "batch_id": batch["id"],
            "count": len(accounts), "errors": errors,
            "accounts": [public_account(a) for a in accounts],
            "msg": "按自定义名单批量创建 %d 个账号完成" % len(accounts)}


def api_batch_invite(p):
    with BATCH_LOCK:
        st = load_state()
        batch = find_batch(st, p.get("batch_id") or None)
        if not batch:
            return {"ok": False, "msg": "没有找到批次，请先批量创建账号"}
        targets = [a for a in batch["accounts"] if a["status"] == "pending"]
        if not targets:
            return {"ok": False, "msg": "该批次没有待激活账号"}
        sent, failed = [], []
        try:
            with smtplib.SMTP_SSL("127.0.0.1", 465,
                                  context=local_ssl_context(), timeout=30) as s:
                s.login(ADMIN_MAIL, MAILPASS)
                for a in targets:
                    try:
                        s.send_message(build_invite(a))
                        a["invited_at"] = now_str()
                        sent.append(a["email"])
                    except Exception as exc:
                        failed.append("%s: %s" % (a["email"], exc))
        except Exception as exc:
            return {"ok": False, "msg": "连接 SMTP 失败: %s" % exc}
        save_state(st)
    return {"ok": len(sent) > 0, "batch_id": batch["id"],
            "sent": sent, "failed": failed,
            "msg": "已从 %s 提交 %d 封激活邀请" % (ADMIN_MAIL, len(sent))}


def api_batch_activate(p):
    with BATCH_LOCK:
        st = load_state()
        batch = find_batch(st, p.get("batch_id") or None)
        if not batch:
            return {"ok": False, "msg": "没有找到批次"}
        common_pw = p.get("password", "").strip()
        results, ok_count = [], 0
        for a in batch["accounts"]:
            if a["status"] == "activated":
                results.append({"email": a["email"], "ok": True, "skipped": True,
                                "msg": "已激活，跳过"})
                ok_count += 1
                continue
            pw = common_pw or ("Act-" + secrets.token_urlsafe(12))
            ok, msg, _ = activate_one(a["token"], pw, "batch-api")
            results.append({"email": a["email"], "ok": ok,
                            "skipped": False, "msg": msg,
                            "password": pw if ok else None})
            ok_count += 1 if ok else 0
    return {"ok": ok_count == len(results), "batch_id": batch["id"],
            "activated": ok_count, "total": len(results), "results": results,
            "msg": "批量激活完成：%d/%d" % (ok_count, len(results))}


def api_batch_remove(p):
    with BATCH_LOCK:
        st = load_state()
        batch = find_batch(st, p.get("batch_id") or None)
        if not batch:
            return {"ok": False, "msg": "没有找到批次"}
        removed = []
        for a in batch["accounts"]:
            r = api_account_remove({"email": a["email"]})
            if r["ok"]:
                removed.append(a["email"])
        st["batches"] = [b for b in st["batches"] if b["id"] != batch["id"]]
        save_state(st)
    return {"ok": True, "removed": removed,
            "msg": "已清理 %d 个测试账号" % len(removed)}


# ---------------------------------------------------------------- web layer

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>example.com 邮件管理系统</title>
<style>
:root{
 --bg:#f4f1ea;--panel:#fffdf9;--white:#fff;--ink:#26221d;--ink2:#4a433a;
 --muted:#8a8175;--line:#e6dfd3;--line2:#f0ebe1;--accent:#b4533a;
 --accent2:#f5e9e3;--ok:#3f7d58;--bad:#a63d3d;--amber:#c08a3e;
 --shadow:0 1px 2px rgba(38,34,29,.04),0 6px 20px rgba(38,34,29,.05)
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
button,input,textarea,select{font:inherit;color:inherit}
a{color:var(--accent);text-decoration:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.serif{font-family:Georgia,"Songti SC","Noto Serif CJK SC",serif}
.hide{display:none!important}
.app{height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ---------- top bar ---------- */
.top{height:62px;flex:0 0 62px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:20px;padding:0 22px;z-index:20}
.brand{display:flex;align-items:center;gap:11px;min-width:212px}
.logo{width:32px;height:32px;border-radius:7px;background:var(--accent);color:#fff;display:grid;place-items:center;font:400 19px/1 Georgia,serif;letter-spacing:-1px}
.brand h1{margin:0;font:400 17px/1.2 Georgia,"Songti SC",serif;letter-spacing:.3px}
.brand .tiny{font-size:10px;color:var(--muted);letter-spacing:.16em;text-transform:uppercase;line-height:1.3}
.gsearch{position:relative;flex:1;max-width:520px}
.gsearch svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted)}
.gsearch input{width:100%;height:36px;padding:0 14px 0 36px;border:1px solid var(--line);border-radius:18px;background:#fbf9f4;outline:none;font-size:13px}
.gsearch input:focus{border-color:var(--accent);background:#fff;box-shadow:0 0 0 3px var(--accent2)}
.top-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.idpick{display:flex;align-items:center;gap:7px;background:#fbf9f4;border:1px solid var(--line);border-radius:18px;padding:3px 6px 3px 11px}
.idpick span{font-size:11px;color:var(--muted)}
.idpick select{border:0;background:transparent;outline:none;font-size:12.5px;max-width:210px;padding:4px 3px;cursor:pointer}
.ibtn{width:34px;height:34px;border:1px solid var(--line);background:#fff;border-radius:9px;display:grid;place-items:center;cursor:pointer;color:var(--ink2);transition:.15s}
.ibtn:hover{border-color:var(--accent);color:var(--accent)}
.btn{background:var(--accent);color:#fff;border:0;border-radius:9px;padding:9px 16px;cursor:pointer;font-size:13px;display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.btn:hover{background:#9e4632}
.btn.ghost{background:#fff;color:var(--ink2);border:1px solid var(--line)}
.btn.ghost:hover{border-color:var(--accent);color:var(--accent);background:#fff}
.btn.sm{padding:6px 11px;font-size:12px;border-radius:7px}
.btn:disabled{opacity:.45;cursor:default}
.btn:disabled:hover{background:var(--accent)}
.clock{font-size:11px;color:var(--muted);white-space:nowrap}

/* ---------- shell ---------- */
.shell{flex:1;display:flex;min-height:0}
.side{width:232px;flex:0 0 232px;background:#faf7f1;border-right:1px solid var(--line);display:flex;flex-direction:column;overflow-y:auto;padding:18px 12px}
.side .compose{width:100%;justify-content:center;margin-bottom:20px;padding:11px}
.glabel{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);padding:0 12px;margin:14px 0 8px;font-weight:600}
.fitem{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;cursor:pointer;color:var(--ink2);font-size:13.5px;border-left:2px solid transparent;margin-bottom:2px;user-select:none}
.fitem:hover{background:#f1ece2}
.fitem.on{background:var(--accent2);color:var(--accent);border-left-color:var(--accent);font-weight:600}
.fitem svg{opacity:.8;flex:0 0 16px}
.fitem .fname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{background:#ece6da;color:var(--ink2);border-radius:10px;padding:1px 7px;font-size:11px;font-variant-numeric:tabular-nums}
.fitem.on .badge{background:#e9d3c9;color:var(--accent)}
.badge.dot{background:var(--accent);color:#fff;padding:1px 6px}
.side-foot{margin-top:auto;border-top:1px solid var(--line);padding:14px 12px 4px;font-size:11px;color:var(--muted);line-height:1.8}
.side-foot b{color:var(--ok);font-weight:600}

/* ---------- mail view ---------- */
.mailview{flex:1;display:flex;min-width:0;background:var(--panel)}
.listpane{width:392px;flex:0 0 392px;border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;background:var(--panel)}
.lhead{padding:16px 18px 11px;border-bottom:1px solid var(--line)}
.ltitle{display:flex;align-items:baseline;gap:9px}
.ltitle h2{margin:0;font:400 20px/1.2 Georgia,"Songti SC",serif}
.ltitle .cnt{font-size:12px;color:var(--muted)}
.lsub{font-size:11.5px;color:var(--muted);margin-top:3px}
.ltools{padding:8px 12px 9px;border-bottom:1px solid var(--line);background:#fdfbf7}
.lt1,.lt2{display:flex;align-items:center;gap:5px;white-space:nowrap;overflow:hidden}
.lt2{margin-top:7px;padding-top:7px;border-top:1px dashed var(--line2)}
.ltools .sep{width:1px;height:17px;background:var(--line);margin:0 3px;flex:0 0 1px}
.flabel{font-size:10.5px;color:var(--muted);letter-spacing:.06em;margin-right:2px}
.chk{width:15px;height:15px;accent-color:var(--accent);cursor:pointer}
.mini{border:0;background:transparent;cursor:pointer;color:var(--muted);padding:4px 7px;border-radius:6px;font-size:12px;display:inline-flex;align-items:center;gap:4px}
.mini:hover{background:#f1ece2;color:var(--accent)}
.mini:disabled{opacity:.35;cursor:default}
.mini.on{color:var(--accent);background:var(--accent2)}
select.sel{border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 6px;font-size:11.5px;outline:none}
.mlist{flex:1;overflow-y:auto;min-height:0}
.mrow{display:flex;gap:7px;padding:9px 12px 9px 10px;border-bottom:1px solid var(--line2);cursor:pointer;position:relative;transition:background .12s}
.mrow:hover{background:#faf6ef}
.mrow.active{background:var(--accent2);box-shadow:inset 2px 0 0 var(--accent)}
.mrow.unread{background:#fffaf5}
.mrow.unread.active{background:var(--accent2)}
.mrow .pick{margin-top:2px;flex:0 0 15px}
.mrow .mbody{flex:1;min-width:0}
.ml1{display:flex;align-items:baseline;gap:8px}
.mfrom{flex:1;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink2)}
.mrow.unread .mfrom{font-weight:700;color:var(--ink)}
.mdate{font-size:11px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.msubj{font-size:12.5px;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink2)}
.mrow.unread .msubj{font-weight:600;color:var(--ink)}
.msnip{font-size:11px;color:var(--muted);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mtag{display:inline-block;font-size:10px;border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:6px;color:var(--muted);vertical-align:1px}
.star{background:none;border:0;cursor:pointer;color:#c9bfae;font-size:14px;padding:0 1px;line-height:1;align-self:flex-start;margin-top:2px;flex:0 0 15px}
.star.on{color:var(--amber)}
.dotmark{position:absolute;left:5px;top:19px;width:5px;height:5px;border-radius:50%;background:var(--accent)}
.mrow:not(.unread) .dotmark{display:none}
.empty{padding:70px 24px;text-align:center;color:var(--muted)}
.empty .big{font:400 40px/1 Georgia,serif;color:#ddd3c3;margin-bottom:12px}
.pager{display:flex;align-items:center;gap:8px;padding:9px 14px;border-top:1px solid var(--line2);font-size:11.5px;color:var(--muted);background:#fdfbf7}
.pager .sp{flex:1}

/* ---------- read pane ---------- */
.readpane{flex:1;min-width:0;display:flex;flex-direction:column;background:var(--white)}
.rhead{padding:20px 26px 15px;border-bottom:1px solid var(--line)}
.rsubj{font:400 22px/1.35 Georgia,"Songti SC",serif;margin:0 0 12px;word-break:break-word}
.ractions{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
.rmeta{display:flex;gap:12px;align-items:flex-start}
.avatar{width:38px;height:38px;flex:0 0 38px;border-radius:50%;background:var(--accent2);color:var(--accent);display:grid;place-items:center;font:400 16px/1 Georgia,serif}
.rmeta .who{flex:1;min-width:0;font-size:12.5px;line-height:1.6}
.rmeta .who b{font-size:13.5px;font-weight:600}
.rmeta .who .addr{color:var(--muted)}
.rmeta .who .tolist{color:var(--muted);font-size:11.5px}
.rdate{font-size:11.5px;color:var(--muted);white-space:nowrap;padding-top:3px}
.rtabs{display:flex;gap:2px;padding:0 26px;border-bottom:1px solid var(--line);background:#fdfbf7}
.rtab{border:0;background:none;padding:10px 14px;font-size:12.5px;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent}
.rtab.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.rcontent{flex:1;overflow:auto;padding:0;background:#fff}
.rcontent iframe{width:100%;height:100%;min-height:420px;border:0;background:#fffdf9;display:block}
.rtext{padding:24px 28px;white-space:pre-wrap;word-break:break-word;font-size:13.5px;line-height:1.8;margin:0}
.rraw{padding:20px 26px;white-space:pre-wrap;word-break:break-all;font:11.5px/1.7 ui-monospace,Menlo,monospace;color:var(--ink2);margin:0}
.rattach{padding:12px 26px;border-top:1px solid var(--line);background:#fdfbf7}
.rattach .lbl{font-size:11px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px}
.aitems{display:flex;gap:8px;flex-wrap:wrap}
.aitem{display:flex;align-items:center;gap:8px;border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 11px;font-size:12px}
.aitem .an{max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.aitem .as{color:var(--muted);font-size:11px}
.aitem button{border:0;background:none;color:var(--accent);cursor:pointer;font-size:12px}
.rnotice{font-size:11px;color:var(--muted);padding:8px 26px;background:#fffdf9;border-bottom:1px solid var(--line2)}

/* ---------- system views ---------- */
.sysview{flex:1;overflow-y:auto;padding:28px 32px 60px;background:var(--bg)}
.syshead{margin-bottom:22px}
.syshead h2{margin:0 0 5px;font:400 25px/1.25 Georgia,"Songti SC",serif}
.syshead p{margin:0;color:var(--muted);font-size:12.5px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:16px;box-shadow:var(--shadow)}
.card h3{font:400 16px/1.3 Georgia,"Songti SC",serif;margin:0 0 14px}
.card .note{color:var(--muted);font-size:12px;margin-top:12px;line-height:1.7}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:11px}
.kv{border:1px solid var(--line);border-radius:8px;padding:11px 13px;background:#fff}
.kv .k{font-size:10.5px;color:var(--muted);letter-spacing:.07em;text-transform:uppercase}
.kv .v{font-size:15px;margin-top:4px;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line2);vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:11.5px}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:2px 9px;border:1px solid var(--line);border-radius:11px;font-size:11px}
.pill.ok{color:var(--ok);border-color:#bcd8c6;background:#f2f8f4}
.pill.bad{color:var(--bad);border-color:#e6c4c4;background:#fcf3f3}
.field{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:7px;background:#fff;outline:none;font-size:13px}
.field:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent2)}
textarea.field{resize:vertical}
label.lb{display:block;font-size:11.5px;color:var(--muted);margin:0 0 5px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:12px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.msg{margin:12px 0 0;padding:9px 12px;border-left:2px solid var(--accent);background:#fbf4ef;font-size:12.5px;white-space:pre-wrap}
/* ---- inline password change (account management) ---- */
.cnt{font:400 12px/1.4 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:var(--muted);margin-left:9px}
.acbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.acbar .field{max-width:290px}
.acbar .note{margin:0;color:var(--muted);font-size:12px}
.acbar .note b{color:var(--ink2)}
tr.pwrow>td{background:#fbf8f2;padding:0;border-bottom:1px solid var(--line)}
.pwbox{border-left:3px solid var(--accent);padding:15px 18px 16px}
.pwhead{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:13px}
.pwhead .lb{display:block;margin-bottom:3px}
.pwaddr{font:600 15px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink)}
.pwin{position:relative}
.pwin .field{padding-right:70px}
.pwin .eye{position:absolute;right:5px;top:50%;transform:translateY(-50%);font-size:11.5px}
.pwrules{display:flex;flex-wrap:wrap;gap:4px 18px;margin:2px 0 12px;font-size:12px}
.pwrules span{color:var(--muted)}
.pwrules span.ok{color:var(--ok)}
.pwrules span.bad{color:var(--bad)}
.pwres{font-size:12.5px;line-height:1.6;flex:1;min-width:180px}
.pwres.ok{color:var(--ok)}
.pwres.bad{color:var(--bad)}
.pwwarn{font-size:12px;color:#a05a2c;background:#fdf1e6;border-left:2px solid #c98652;padding:8px 11px;margin:0 0 12px;line-height:1.6}
pre.log{background:#f7f3ec;border:1px solid var(--line);padding:14px;overflow:auto;max-height:440px;font:11.5px/1.6 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;margin:0;border-radius:7px}

/* ---------- composer ---------- */
.mask{position:fixed;inset:0;background:rgba(38,34,29,.42);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(2px)}
.modal{width:min(760px,100%);max-height:92vh;background:var(--panel);border-radius:14px;box-shadow:0 24px 70px rgba(38,34,29,.28);display:flex;flex-direction:column;overflow:hidden}
.mhead{padding:15px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px}
.mhead h3{margin:0;font:400 17px/1.3 Georgia,"Songti SC",serif;flex:1}
.mbody{padding:16px 22px;overflow-y:auto;display:flex;flex-direction:column;gap:11px}
.mline{display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--line2);padding-bottom:9px}
.mline label{width:62px;flex:0 0 62px;font-size:12px;color:var(--muted)}
.mline input,.mline select{border:0;background:transparent;outline:none;flex:1;font-size:13px;padding:4px 0}
.mbody textarea{width:100%;min-height:210px;border:0;outline:none;resize:vertical;background:transparent;font-size:13.5px;line-height:1.75}
.mfoot{padding:13px 22px;border-top:1px solid var(--line);display:flex;align-items:center;gap:9px;background:#fdfbf7}
.mfoot .hint{font-size:11px;color:var(--muted);flex:1}
.filelist{display:flex;gap:6px;flex-wrap:wrap}
.filechip{border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 8px;font-size:11px;color:var(--ink2)}
.filechip b{color:var(--accent);cursor:pointer;margin-left:5px}
.toast{position:fixed;bottom:26px;left:50%;transform:translate(-50%,20px);background:#26221d;color:#fff;padding:11px 20px;border-radius:9px;font-size:13px;opacity:0;pointer-events:none;transition:.24s;z-index:200;box-shadow:0 8px 28px rgba(0,0,0,.2)}
.toast.on{opacity:1;transform:translate(-50%,0)}
.toast.bad{background:#a63d3d}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
@media(max-width:1180px){
 .listpane{width:330px;flex-basis:330px}.brand{min-width:auto}.clock{display:none}
 .gsearch{max-width:280px}
}
@media(max-width:900px){
 .side{width:64px;flex-basis:64px;padding:14px 8px}
 .side .compose span,.fitem .fname,.fitem .badge,.glabel,.side-foot{display:none}
 .fitem{justify-content:center;padding:11px 0}.side .compose{padding:11px 0}
 .listpane{width:300px;flex-basis:300px}
}
.who{display:flex;align-items:center;gap:7px;padding:5px 11px;background:var(--accent2);
 border-radius:20px;font-size:12px;color:var(--ink2);max-width:250px}
.who b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.who em{font-style:normal;font-size:11px;color:var(--muted);white-space:nowrap}
.wdot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:0 0 7px}

/* ---- organisations (v6) ---- */
.orgsel{width:auto;max-width:200px;padding:7px 9px;font-size:12.5px;border:1px solid var(--line);border-radius:7px;background:#fff;outline:none}
select.orgsel.inline{display:block;max-width:160px;margin-top:6px;padding:4px 6px;font-size:11.5px;border-radius:6px}
.orgtag{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border:1px solid var(--line);border-radius:11px;background:#fff;font-size:11px;white-space:nowrap;color:var(--ink2)}
.orgtag.none{color:var(--muted);background:#faf7f1}
.orgtag .dotc,.osname .dotc,.orgdot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:0 0 8px}
.pickline{display:inline-flex;align-items:center;gap:8px;cursor:pointer}
.bulkbar{border-top:1px dashed var(--line2);padding-top:11px;margin-top:2px}
.bulkbar .orgsel{max-width:175px}
.bulkbar .note{font-size:11.5px}
.orgstats{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:11px 28px}
.orgstat{display:flex;align-items:center;gap:11px;font-size:12px}
.osname{width:96px;flex:0 0 96px;display:flex;align-items:center;gap:7px;color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.osbar{flex:1;height:8px;border-radius:5px;background:#f0ebe1;overflow:hidden;min-width:60px}
.osbar i{display:block;height:100%;border-radius:5px}
.osnum{width:54px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.orgsum{font-size:12px;color:var(--muted);margin:0 0 14px}
.orgedit{display:flex;gap:6px;align-items:center}
.orgedit .field{max-width:170px;padding:6px 9px;font-size:12px}
</style></head><body>
<div class="app">
  <header class="top">
    <div class="brand"><div class="logo">@</div><div><h1 class="serif">example.com</h1><div class="tiny">Mail System</div></div></div>
    <div class="gsearch">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="gq" placeholder="搜索当前文件夹的邮件（主题 / 发件人 / 收件人）" autocomplete="off">
    </div>
    <div class="top-right">
      <div class="who"><span class="wdot"></span><b>__SESSION_ACCOUNT__</b><em id="rolelab"></em></div>
      <div class="idpick"><span>正在查看</span><select id="idpick"></select></div>
      <button class="ibtn" id="refresh" title="刷新">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/></svg></button>
      <button class="btn" id="newmail"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>写邮件</button>
      <button class="ibtn" id="logout" title="退出登录"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/></svg></button>
      <span class="clock" id="clock"></span>
    </div>
  </header>

  <div class="shell">
    <aside class="side">
      <button class="btn compose" id="compose2"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16v16H4z"/><path d="m4 6 8 7 8-7"/></svg><span>写新邮件</span></button>
      <div class="glabel">邮箱文件夹</div>
      <div id="folders"></div>
      <div class="glabel" id="syslabel">系统管理</div>
      <div id="sysnav"></div>
      <div class="side-foot">
        <div>服务状态 <b id="svc-dot">检测中</b></div>
        <div id="svc-line">Maddy · nginx · 管理台</div>
        <div>HTTPS + 会话登录保护</div>
      </div>
    </aside>

    <main class="mailview" id="view-mail">
      <section class="listpane">
        <div class="lhead">
          <div class="ltitle"><h2 id="ftitle">收件箱</h2><span class="cnt" id="fcnt"></span></div>
          <div class="lsub" id="fsub">加载中…</div>
        </div>
        <div class="ltools">
          <div class="lt1">
            <input type="checkbox" class="chk" id="pickall" title="全选本页">
            <button class="mini" id="a-seen" title="标为已读" disabled>标已读</button>
            <button class="mini" id="a-unseen" title="标为未读" disabled>标未读</button>
            <span class="sep"></span>
            <button class="mini" id="a-trash" title="删除（移入已删除）" disabled>删除</button>
            <select class="sel" id="a-move" disabled><option value="">移动到…</option></select>
          </div>
          <div class="lt2">
            <span class="flabel">筛选</span>
            <button class="mini on" data-fl="all">全部</button>
            <button class="mini" data-fl="unseen">未读</button>
            <button class="mini" data-fl="seen">已读</button>
            <button class="mini" data-fl="flagged">星标</button>
          </div>
        </div>
        <div class="mlist" id="mlist"><div class="empty">加载中…</div></div>
        <div class="pager">
          <button class="mini" id="prev">‹ 上一页</button>
          <span id="pinfo">—</span>
          <button class="mini" id="next">下一页 ›</button>
          <span class="sp"></span>
          <select class="sel" id="psize"><option>25</option><option>50</option><option>100</option></select>
          <span>封/页</span>
        </div>
      </section>
      <section class="readpane" id="readpane">
        <div class="empty" style="margin:auto"><div class="big">✉</div>
          <div>从左侧选择一封邮件开始阅读</div>
          <div style="font-size:11.5px;margin-top:8px">支持 HTML 安全渲染 · 附件下载 · 线程化回复</div></div>
      </section>
    </main>

    <main class="sysview hide" id="view-sys">
      <div id="sys-overview">
        <div class="syshead"><h2>系统概览</h2><p>邮件服务运行状态、域名解析与连接参数</p></div>
        <div class="card" style="border-left:3px solid var(--accent)"><h3>公网访问地址</h3>
          <div style="font:400 19px/1.4 Georgia,serif"><a id="pub-url" href="#">加载中…</a></div>
          <div class="note">纯 IP 直连，使用 Let's Encrypt 签发的 IP 地址证书（6 天自动续期），不依赖域名解析。</div></div>
        <div class="card"><h3>服务状态</h3><div id="ov" class="grid">加载中…</div></div>
        <div class="card"><h3>域名解析</h3><table id="dns"><tbody><tr><td>加载中…</td></tr></tbody></table></div>
        <div class="card"><h3>客户端连接参数</h3><pre class="log">IMAP   mail.example.com:993   SSL/TLS
SMTP   mail.example.com:465   SSL/TLS
SMTP   mail.example.com:587   STARTTLS
Web    https://203.0.113.10/admin/  （HTTPS + 密码保护）
激活   https://203.0.113.10/admin/activate?token=...</pre>
          <div class="note">Maddy 单进程邮件服务，本页为服务器本机自建的轻量管理系统，无额外数据库或运行时依赖。</div></div>
      </div>

      <div id="sys-accounts" class="hide">
        <div class="syshead"><h2>账号管理</h2><p>创建账号、按组织筛选与调整归属、修改密码、删除邮箱；主管理员账号受保护</p></div>
        <div class="card" id="card-add-account"><h3>新增账号</h3>
          <div class="row"><div><label class="lb">邮箱地址</label><input class="field" id="a-email" placeholder="name@example.com"></div>
            <div><label class="lb">初始密码（≥10 位）</label><input class="field" id="a-pw" placeholder="留空则自动生成"></div>
            <div><label class="lb">所属组织</label><select class="field orgsel" id="a-org"></select></div></div>
          <button class="btn" id="a-add">创建账号</button><div class="msg hide" id="a-msg"></div></div>
        <div class="card" id="card-account-list"><h3>账号列表<span class="cnt" id="acct-count"></span></h3>
          <div class="acbar">
            <input class="field" id="acct-q" placeholder="搜索邮箱地址…" autocomplete="off">
            <select class="field orgsel" id="acct-org-filter"></select>
            <span class="note">共 <b id="acct-total">0</b> 个账号 · 可直接在本行调整组织或修改密码</span>
          </div>
          <div class="acbar bulkbar">
            <label class="pickline"><input type="checkbox" class="chk" id="acct-pickall"> 全选当前结果</label>
            <span class="note">已选 <b id="acct-seln">0</b> 个</span>
            <select class="field orgsel" id="acct-bulk-org"></select>
            <button class="btn ghost sm" id="acct-bulk-apply">应用到选中账号</button>
            <button class="btn ghost sm" id="acct-bulk-filtered">应用到当前筛选结果</button>
          </div>
          <div class="msg hide" id="acct-bulk-msg"></div>
          <div id="acct">加载中…</div>
          <div class="msg hide" id="acct-msg"></div>
        </div>
        <div class="card"><h3>关于修改密码</h3>
          <div class="note" style="margin-top:0">· 在上方列表点击某账号的「修改密码」，邮箱地址自动带入，无需手输，避免改错账号。<br>· 新密码需 10–128 位、含大写／小写／数字／符号中至少 3 类、且不含邮箱名；可一键生成强密码并复制。<br>· 修改后该账号的 IMAP／SMTP／网页登录立即使用新密码，旧密码失效。<br>· 修改主管理员账号会同步服务器凭据文件，两个旧密码同时失效，请务必保存好新密码。<br>· 账号由「认证凭据」与「IMAP 存储」两部分组成，面板会同时创建／删除，避免残留。</div></div>
      </div>

      <div id="sys-orgs" class="hide">
        <div class="syshead"><h2>组织管理</h2><p>自定义组织架构、查看成员分布，把员工邮箱归属到销售／行政／财务／技术／售后／电销等组织</p></div>
        <div class="card"><h3>组织成员分布</h3>
          <div class="orgsum" id="org-sum">加载中…</div>
          <div class="orgstats" id="org-stats">加载中…</div>
          <div class="note">组织归属只用于管理台分组与筛选，不影响邮件收发；删除组织不会删除任何邮箱账号。</div></div>
        <div class="card"><h3>新增组织</h3>
          <div class="row">
            <div><label class="lb">组织名称（1–20 字，不可重名）</label><input class="field" id="org-new-name" placeholder="例如：市场部 / 华东大区"></div>
            <div><label class="lb">标识色</label><select class="field orgsel" id="org-new-color">
              <option value="#b4533a">赤陶</option><option value="#c08a3e">琥珀</option>
              <option value="#3f7d58">松绿</option><option value="#4a6fa5">靛蓝</option>
              <option value="#8a63a8">紫藤</option><option value="#c26a6a">玫瑰</option>
              <option value="#6b6257">石墨</option><option value="#2f7f7a">青碧</option>
            </select></div></div>
          <button class="btn" id="org-add">创建组织</button><div class="msg hide" id="org-msg"></div></div>
        <div class="card"><h3>组织列表</h3>
          <div id="org-list">加载中…</div></div>
        <div class="card"><h3>组织成员整体迁移</h3>
          <div class="bar">
            <label class="lb" style="margin:0">来源</label><select class="field orgsel" id="org-move-from"></select>
            <span class="note" style="margin:0">→</span>
            <label class="lb" style="margin:0">目标组织</label><select class="field orgsel" id="org-move-to"></select>
            <button class="btn ghost sm" id="org-move-go">迁移成员</button></div>
          <div class="msg hide" id="org-move-msg"></div>
          <div class="note">把来源组织的全部成员一次性调整到目标组织；目标选「不分配 / 未分配」可清空归属。单个账号请在「账号管理」里直接调整。</div></div>
        <div class="card"><h3>使用说明</h3>
          <div class="note" style="margin-top:0">· 系统预置销售、行政、财务、技术、售后、电销六个组织，可改名、删除，也可新增自定义组织。<br>· 首次启用时已把现有员工邮箱按顺序分配到六个组织，可随时在「账号管理」逐行调整或勾选后批量调整。<br>· 组织数据保存在 /var/lib/maddy/admin/organizations.json，服务重启后不会丢失。<br>· 仅管理员可见组织管理；普通用户登录后只能看到自己的邮箱。</div></div>
      </div>

      <div id="sys-batch" class="hide">
        <div class="syshead"><h2>批量账号与激活</h2><p>批量创建、发送激活邀请、批量激活与批次清理</p></div>
        <div class="card" style="border-left:3px solid var(--accent)"><h3>批量创建与激活</h3>
          <div class="bar"><label class="lb" style="margin:0">创建数量</label>
            <input class="field" id="b-count" type="number" min="1" max="500" value="10" style="width:100px">
            <label class="lb" style="margin:0">所属组织</label>
            <select class="field orgsel" id="b-org"></select>
            <button class="btn" id="b-create">批量创建账号</button>
            <button class="btn ghost sm" id="b-invite">发送激活邀请</button>
            <button class="btn ghost sm" id="b-activate">批量激活</button>
            <button class="btn ghost sm" id="b-refresh">刷新</button>
            <button class="btn ghost sm" id="b-remove">清理本批次</button></div>
          <div class="msg hide" id="b-msg"></div>
          <div class="note">命名规范：随机名称 + 4 位随机数字 @example.com。创建后为「待激活」，凭一次性令牌设置密码后启用。</div></div>
        <div class="card"><h3>按自定义名单批量创建</h3>
          <div style="margin-bottom:10px"><label class="lb">邮箱名单（每行一个，也可用逗号/空格分隔；完整地址或仅前缀均可）</label>
            <textarea class="field" id="b-custom" rows="4" placeholder="opc_training.00&#10;opc_training.01&#10;zhaowei.00"></textarea></div>
          <button class="btn" id="b-custom-go">按名单创建</button><div class="msg hide" id="b-custom-msg"></div>
          <div class="note">名单统一归入 @example.com，允许下划线与点。单批最多 500 个；含 admin@example.com 会自动拒绝；创建后按上方「所属组织」自动归属。</div></div>
        <div class="card"><h3>批次概览</h3><div class="bar"><span class="note" id="b-meta" style="margin:0"></span></div>
          <div id="b-wrap" style="max-height:440px;overflow:auto"><table id="b-list"><tbody><tr><td>加载中…</td></tr></tbody></table></div></div>
      </div>

      <div id="sys-check" class="hide">
        <div class="syshead"><h2>部署自检</h2><p>覆盖 DNS、证书、TLS、MTA-STS；PTR 为付费项单独标注</p></div>
        <div class="card"><div class="bar"><button class="btn" id="k-run">重新检测</button></div>
          <table id="k-list"><tbody><tr><td>点击「重新检测」开始</td></tr></tbody></table></div>
      </div>

      <div id="sys-logs" class="hide">
        <div class="syshead"><h2>运行日志</h2><p>Maddy 服务最近日志</p></div>
        <div class="card"><div class="bar"><button class="btn ghost sm" id="l-refresh">刷新</button>
          <select class="sel" id="l-n"><option>50</option><option selected>80</option><option>150</option><option>300</option></select></div>
          <pre class="log" id="l-out">加载中…</pre></div>
      </div>
    </main>
  </div>
</div>

<div class="mask hide" id="composer"><div class="modal">
  <div class="mhead"><h3 id="cmp-title">写新邮件</h3>
    <button class="ibtn" id="cmp-close" title="关闭">✕</button></div>
  <div class="mbody">
    <div class="mline"><label>发件人</label><select id="cmp-from"></select></div>
    <div class="mline"><label>收件人</label><input id="cmp-to" placeholder="name@example.com, 多个用逗号分隔"></div>
    <div class="mline"><label>抄送</label><input id="cmp-cc" placeholder="可选"></div>
    <div class="mline"><label>主题</label><input id="cmp-sub" placeholder="邮件主题"></div>
    <textarea id="cmp-body" placeholder="输入邮件正文…"></textarea>
    <div><label class="lb">附件（单个 ≤ 2 MB，合计 ≤ 4 MB）</label>
      <input type="file" id="cmp-file" multiple style="font-size:12px">
      <div class="filelist" id="cmp-files" style="margin-top:7px"></div></div>
  </div>
  <div class="mfoot"><button class="btn" id="cmp-send">发送邮件</button>
    <button class="btn ghost" id="cmp-draft">存草稿</button>
    <span class="hint" id="cmp-hint"></span>
    <button class="btn ghost sm" id="cmp-cancel">取消</button></div>
</div></div>
<div class="toast" id="toast"></div>

<script>
const SESSION={account:"__SESSION_ACCOUNT__",is_admin:__IS_ADMIN__,csrf:"__CSRF_TOKEN__"};
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => { const d=document.createElement('div'); d.textContent=(s==null?'':String(s)); return d.innerHTML; };
const ICONS = {
 inbox:'<path d="M4 4h16v11H4z"/><path d="m4 15 3 5h10l3-5"/>',
 send:'<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
 draft:'<path d="M5 3h9l5 5v13H5z"/><path d="M14 3v5h5"/>',
 junk:'<path d="M4 7h16l-1 13H5z"/><path d="M9 7V4h6v3"/>',
 trash:'<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 14h10l1-14"/>',
 archive:'<path d="M3 4h18v5H3z"/><path d="M5 9v11h14V9"/><path d="M10 13h4"/>',
 folder:'<path d="M3 6h7l2 2h9v11H3z"/>',
 users:'<path d="M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 20v-2a4 4 0 0 0-3-3.9"/>',
 gauge:'<path d="M12 20a8 8 0 1 0-8-8"/><path d="m12 12 4-4"/><circle cx="12" cy="12" r="2"/>',
 list:'<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/>'
};
const icon = (n,c) => '<svg class="'+esc(c||'')+'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+ICONS[n]+'</svg>';
const S = { account:SESSION.account, folder:'INBOX', page:1, size:25, q:'', filter:'all',
            selected:new Set(), current:null, folders:[], view:'mail', sysTab:'overview',
            messages:[], compose:{mode:'new', draft_id:'', refs:'', inreply:'', files:[]} };

function toast(txt,ok){ const t=$('#toast'); t.textContent=txt; t.className='toast on'+(ok===false?' bad':''); clearTimeout(t._tm); t._tm=setTimeout(()=>t.className='toast',2600); }
async function api(path,opts){ opts=opts||{}; opts.headers=Object.assign({'X-CSRF-Token':SESSION.csrf},opts.headers||{}); opts.credentials='same-origin'; const r=await fetch('/admin/api/'+path,opts); if(r.status===401){ location.href='/admin/login'; throw new Error('会话已过期'); } return r.json(); }
async function post(path,obj){ return api(path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(obj)}); }
function qs(obj){ return Object.entries(obj).filter(([k,v])=>v!==''&&v!=null).map(([k,v])=>encodeURIComponent(k)+'='+encodeURIComponent(v)).join('&'); }
function fmtSize(n){ n=n||0; return n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB'; }
function fmtDate(ts,raw){
  if(!ts) return esc((raw||'').slice(0,25))||'—';
  const d=new Date(ts*1000),now=new Date();
  const hm=String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
  if(d.toDateString()===now.toDateString()) return hm;
  if(d.getFullYear()===now.getFullYear()) return (d.getMonth()+1)+'/'+d.getDate()+' '+hm;
  return d.getFullYear()+'/'+(d.getMonth()+1)+'/'+d.getDate();
}
function fmtFull(ts,raw){ if(!ts) return esc(raw||'—'); const d=new Date(ts*1000);
  return d.toLocaleString('zh-CN',{hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
function showMsg(id,txt,ok){ const e=$(id); e.className='msg'; e.textContent=txt; e.style.borderLeftColor=ok?'#3f7d58':'#a63d3d'; }
function paint(el,rows){ el.innerHTML=rows.map(r=>'<tr>'+r+'</tr>').join(''); }

/* ---------------- boot / identities / folders ---------------- */
async function boot(){
  const d=await api('mail/identities');
  $('#idpick').innerHTML=d.accounts.map(a=>'<option value="'+esc(a.email)+'"'+(!a.available?' disabled':'')+'>'+esc(a.email)+(a.is_admin?' · 管理员':'')+(a.available?'':' · 无凭据')+'</option>').join('');
  $('#cmp-from').innerHTML=d.accounts.filter(a=>a.available).map(a=>'<option value="'+esc(a.email)+'">'+esc(a.email)+'</option>').join('');
  $('#idpick').value=S.account; $('#cmp-from').value=S.account;
  $('#rolelab').textContent=SESSION.is_admin?'管理员':'用户';
  if(!SESSION.is_admin){
    document.querySelector('.idpick').classList.add('hide');
    $('#syslabel').classList.add('hide'); $('#sysnav').classList.add('hide');
    $('#view-sys').classList.add('hide');
  }
  await loadFolders();
  await loadList();
  previewFirst();
  if(SESSION.is_admin) overview();
  setInterval(()=>{ const d=new Date(); $('#clock').textContent=d.toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); },1000);
}
async function loadFolders(){
  const d=await api('mail/folders?'+qs({account:S.account}));
  if(!d.ok){ toast(d.msg||'文件夹加载失败',false); return; }
  S.folders=d.folders;
  $('#folders').innerHTML=d.folders.map(f=>
    '<div class="fitem'+(S.view==='mail'&&f.name===S.folder?' on':'')+'" data-folder="'+esc(f.name)+'">'+
    icon(f.icon||'folder')+'<span class="fname">'+esc(f.label)+'</span>'+
    (f.unseen?'<span class="badge dot">'+f.unseen+'</span>':f.total?'<span class="badge">'+f.total+'</span>':'')+'</div>').join('');
  $$('[data-folder]').forEach(el=>el.onclick=()=>switchFolder(el.dataset.folder));
  $('#refresh').onclick=refreshAll;
  const sys=[['overview','概览','gauge'],['accounts','账号管理','users'],['orgs','组织管理','users'],['batch','批量账号','list'],['check','部署自检','gauge'],['logs','运行日志','list']];
  $('#sysnav').innerHTML=sys.map(([k,n,i])=>'<div class="fitem'+(S.view==='sys'&&S.sysTab===k?' on':'')+'" data-sys="'+k+'">'+icon(i)+'<span class="fname">'+n+'</span></div>').join('');
  $$('[data-sys]').forEach(el=>el.onclick=()=>switchSys(el.dataset.sys));
  const allok=d.folders.length>=6;
  $('#svc-dot').textContent=allok?'正常':'需检查';
  $('#svc-line').textContent='共 '+d.folders.length+' 个文件夹 · 未读 '+d.total_unseen;
}
function markFolderNav(){
  $$('[data-folder]').forEach(el=>el.classList.toggle('on',S.view==='mail'&&el.dataset.folder===S.folder));
  $$('[data-sys]').forEach(el=>el.classList.toggle('on',S.view==='sys'&&el.dataset.sys===S.sysTab));
}
function switchFolder(name){
  S.folder=name; S.page=1; S.q=''; S.filter='all'; S.selected.clear(); S.current=null;
  $('#gq').value=''; $('#view-mail').classList.remove('hide'); $('#view-sys').classList.add('hide'); S.view='mail';
  $$('[data-fl]').forEach(b=>b.classList.toggle('on',b.dataset.fl==='all'));
  markFolderNav(); resetRead(); loadList().then(previewFirst); loadFolders();
}
function switchSys(tab){
  S.view='sys'; S.sysTab=tab; markFolderNav();
  $('#view-mail').classList.add('hide'); $('#view-sys').classList.remove('hide');
  ['overview','accounts','orgs','batch','check','logs'].forEach(k=>$('#sys-'+k).classList.toggle('hide',k!==tab));
  if(tab==='overview') overview(); if(tab==='accounts') accounts(); if(tab==='orgs') orgs();
  if(tab==='batch') batch(); if(tab==='check') check(); if(tab==='logs') logs();
}
async function refreshAll(){ await loadFolders(); if(S.view==='mail') await loadList(); markFolderNav(); toast('已刷新'); }

/* ---------------- message list ---------------- */
async function loadList(){
  const box=$('#mlist'); box.innerHTML='<div class="empty"><span class="spin"></span> 正在读取邮件…</div>';
  const d=await api('mail/list?'+qs({account:S.account,folder:S.folder,page:S.page,size:S.size,q:S.q,filter:S.filter}));
  if(!d.ok){ box.innerHTML='<div class="empty">'+esc(d.msg||'读取失败')+'</div>'; return; }
  S.messages=d.messages; S.page=d.page;
  const f=S.folders.find(x=>x.name===S.folder);
  $('#ftitle').textContent=f?f.label:S.folder;
  $('#fcnt').textContent=d.total+' 封'+(d.q?' · 搜索结果':'');
  const unread=S.folders.reduce((a,x)=>a+x.unseen,0);
  $('#fsub').textContent=(SESSION.is_admin?'管理员视图':'我的邮箱')+' · '+S.account+' · 全部文件夹未读 '+unread+' 封';
  $('#pinfo').textContent=d.total?((d.page-1)*d.size+1)+'–'+Math.min(d.page*d.size,d.total)+' / 共 '+d.total+' 封':'无邮件';
  $('#prev').disabled=d.page<=1; $('#next').disabled=d.page>=d.pages;
  $('#pickall').checked=false; syncTools();
  if(!d.messages.length){
    box.innerHTML='<div class="empty"><div class="big">✉</div><div>'+(d.q?'没有匹配的邮件':'这个文件夹是空的')+'</div></div>'; return;
  }
  box.innerHTML=d.messages.map(m=>{
    const selected=S.selected.has(m.id);
    return '<div class="mrow'+(m.seen?'':' unread')+(S.current===m.id?' active':'')+'" data-id="'+esc(m.id)+'">'+
      '<span class="dotmark"></span>'+
      '<input type="checkbox" class="chk pick" data-pick="'+esc(m.id)+'"'+(selected?' checked':'')+'>'+
      '<button class="star'+(m.flagged?' on':'')+'" data-star="'+esc(m.id)+'" title="星标">'+(m.flagged?'★':'☆')+'</button>'+
      '<div class="mbody"><div class="ml1"><span class="mfrom">'+esc(m.from.name||m.from.addr||'(未知)')+'</span>'+
      '<span class="mdate">'+fmtDate(m.ts,m.date)+'</span></div>'+
      '<div class="msubj">'+(m.has_attachment?'<span title="含附件">📎 </span>':'')+esc(m.subject)+(m.size>102400?'<span class="mtag">'+fmtSize(m.size)+'</span>':'')+'</div>'+
      '<div class="msnip">'+esc(m.snippet||'')+'</div></div></div>';
  }).join('');
  $$('.mrow').forEach(el=>el.onclick=e=>{
    if(e.target.dataset.pick!==undefined||e.target.dataset.star!==undefined) return;
    openMessage(el.dataset.id);
  });
  $$('[data-pick]').forEach(el=>el.onchange=()=>{ el.checked?S.selected.add(el.dataset.pick):S.selected.delete(el.dataset.pick); syncTools(); });
  $$('[data-star]').forEach(el=>el.onclick=async e=>{ e.stopPropagation();
    const on=el.classList.toggle('on'); el.textContent=on?'★':'☆';
    await post('mail/action',{account:S.account,folder:S.folder,ids:el.dataset.star,action:on?'flag':'unflag'});
  });
}
function syncTools(){
  const n=S.selected.size;
  ['a-seen','a-unseen','a-trash','a-move'].forEach(id=>$('#'+id).disabled=!n);
  $('#a-move').value='';
}
async function bulk(action,dest){
  const ids=Array.from(S.selected).join(',');
  if(!ids){ toast('请先勾选邮件',false); return; }
  if(action==='delete'&&!confirm('确认永久删除勾选的邮件？此操作不可撤销。')) return;
  const r=await post('mail/action',{account:S.account,folder:S.folder,ids:ids,action:action,dest:dest||''});
  toast(r.msg||'操作完成',r.ok); S.selected.clear();
  if(S.current&&ids.split(',').indexOf(String(S.current))>=0){ S.current=null; resetRead(); }
  await loadList(); await loadFolders(); markFolderNav();
}
function resetRead(){
  $('#readpane').innerHTML='<div class="empty" style="margin:auto"><div class="big">✉</div><div>从左侧选择一封邮件开始阅读</div>'+
    '<div style="font-size:11.5px;margin-top:8px">支持 HTML 安全渲染 · 附件下载 · 线程化回复</div></div>';
}

/* ---------------- reading ---------------- */
function previewFirst(){ if(S.messages.length) return openMessage(S.messages[0].id,false); }
async function openMessage(id,markSeen){
  const d=await api('mail/read?'+qs({account:S.account,folder:S.folder,id:id}));
  if(!d.ok){ toast(d.msg||'读取失败',false); return; }
  const m=d.message;
  if(S.folder==='Drafts'){ compose('draft',m); return; }
  S.current=id;
  renderMessage(m);
  $$('.mrow').forEach(el=>el.classList.toggle('active',el.dataset.id===id));
  if(markSeen!==false && !m.seen){
    const r=await post('mail/action',{account:S.account,folder:S.folder,ids:id,action:'seen'});
    if(r.ok){ const row=S.messages.find(x=>x.id===id); if(row) row.seen=true;
      const el=document.querySelector('.mrow[data-id="'+id+'"]'); if(el) el.classList.remove('unread');
      loadFolders(); }
  }
}
function renderMessage(m){
  const who=m.from||{name:'(未知)',addr:''};
  const initial=(who.name||who.addr||'?').trim().charAt(0).toUpperCase();
  const toList=(m.to||[]).map(x=>x.name||x.addr).join(', ')||'—';
  const ccList=(m.cc||[]).map(x=>x.name||x.addr).join(', ');
  const htmlBody=!!m.html;
  const atts=(m.attachments||[]).map(a=>'<div class="aitem">'+icon('draft')+
    '<span class="an" title="'+esc(a.filename)+'">'+esc(a.filename)+'</span>'+
    '<span class="as">'+fmtSize(a.size)+'</span>'+
    '<button data-dl="'+qs({account:S.account,folder:S.folder,id:m.id,part:a.part})+'" data-name="'+esc(a.filename)+'">下载</button></div>').join('');
  const rawUrl='/admin/api/mail/attachment?'+qs({account:S.account,folder:S.folder,id:m.id,mode:'raw'});
  $('#readpane').innerHTML=
   '<div class="rhead"><h2 class="rsubj">'+esc(m.subject)+'</h2>'+
   '<div class="ractions">'+
     '<button class="btn sm" id="r-reply">↩ 回复</button>'+
     '<button class="btn ghost sm" id="r-replyall">回复全部</button>'+
     '<button class="btn ghost sm" id="r-forward">转发</button>'+
     '<span style="flex:1"></span>'+
     '<button class="btn ghost sm" id="r-unread">标为未读</button>'+
     '<button class="btn ghost sm" id="r-trash">'+(S.folder==='Trash'?'永久删除':'删除')+'</button>'+
     '<button class="btn ghost sm" id="r-eml">下载 .eml</button>'+
   '</div>'+
   '<div class="rmeta"><div class="avatar">'+esc(initial)+'</div><div class="who">'+
     '<div><b>'+esc(who.name)+'</b> <span class="addr mono">&lt;'+esc(who.addr)+'&gt;</span></div>'+
     '<div class="tolist">收件人：'+esc(toList)+(ccList?' · 抄送：'+esc(ccList):'')+'</div>'+
     '</div><div class="rdate">'+fmtFull(m.ts,m.date)+'</div></div></div>'+
   '<div class="rtabs"><button class="rtab on" data-rt="html">'+(htmlBody?'邮件正文':'纯文本')+'</button>'+
     (htmlBody?'<button class="rtab" data-rt="text">纯文本</button>':'')+
     '<button class="rtab" data-rt="raw">原始邮件头</button></div>'+
   (htmlBody?'<div class="rnotice">HTML 邮件已在隔离沙箱中渲染，脚本与外部追踪图片已被拦截。</div>':'')+
   '<div class="rcontent" id="rcontent"></div>'+
   (atts?'<div class="rattach"><div class="lbl">附件 · '+m.attachments.length+' 个</div><div class="aitems">'+atts+'</div></div>':'');
  const pane=$('#rcontent');
  if(htmlBody){
    const f=document.createElement('iframe');
    f.setAttribute('sandbox','allow-popups allow-popups-to-escape-sandbox');
    f.setAttribute('title','邮件正文（安全沙箱）');
    f.srcdoc=m.html;
    pane.appendChild(f);
  } else {
    const pre=document.createElement('pre'); pre.className='rtext'; pre.textContent=unescapeText(m.text); pane.appendChild(pre);
  }
  $$('[data-rt]').forEach(b=>b.onclick=()=>{
    $$('[data-rt]').forEach(x=>x.classList.toggle('on',x===b));
    const pane=$('#rcontent'); pane.innerHTML='';
    if(b.dataset.rt==='raw'){ const p=document.createElement('pre'); p.className='rraw'; p.textContent=m.raw_headers||''; pane.appendChild(p); }
    else if(b.dataset.rt==='html'&&htmlBody){ const f=document.createElement('iframe'); f.setAttribute('sandbox','allow-popups allow-popups-to-escape-sandbox'); f.srcdoc=m.html; pane.appendChild(f); }
    else { const p=document.createElement('pre'); p.className='rtext'; p.textContent=unescapeText(m.text); pane.appendChild(p); }
  });
  $$('[data-dl]').forEach(b=>b.onclick=()=>{
    const a=document.createElement('a'); a.href='/admin/api/mail/attachment?'+b.dataset.dl;
    a.download=b.dataset.name||'attachment'; document.body.appendChild(a); a.click(); a.remove();
  });
  $('#r-reply').onclick=()=>compose('reply',m);
  $('#r-replyall').onclick=()=>compose('replyall',m);
  $('#r-forward').onclick=()=>compose('forward',m);
  $('#r-eml').onclick=()=>{ const a=document.createElement('a'); a.href=rawUrl; a.download='message.eml'; document.body.appendChild(a); a.click(); a.remove(); };
  $('#r-unread').onclick=async()=>{ const r=await post('mail/action',{account:S.account,folder:S.folder,ids:m.id,action:'unseen'});
    toast(r.msg,r.ok); S.current=null; resetRead(); loadList(); loadFolders(); };
  $('#r-trash').onclick=async()=>{ const perm=S.folder==='Trash';
    if(perm&&!confirm('确认永久删除这封邮件？'))return;
    const r=await post('mail/action',{account:S.account,folder:S.folder,ids:m.id,action:perm?'delete':'trash',dest:'Trash'});
    toast(r.msg,r.ok); S.current=null; resetRead(); loadList(); loadFolders(); };
}
function unescapeText(t){ const d=document.createElement('div'); d.innerHTML=t||''; return d.textContent||''; }

/* ---------------- compose ---------------- */
function compose(mode,m){
  m=m||{}; S.compose={mode:mode,draft_id:m.id||'',refs:m.references||'',inreply:m.message_id||'',files:[]};
  const titles={new:'写新邮件',reply:'回复邮件',replyall:'回复全部',forward:'转发邮件',draft:'继续编辑草稿'};
  $('#cmp-title').textContent=titles[mode]||'写邮件';
  $('#cmp-from').value=m.account||S.account;
  const f=m.from||{name:'',addr:''};
  const selfs=[];
  if(mode==='reply'||mode==='replyall'){
    const rp=(m.reply_to&&m.reply_to.length?m.reply_to[0].addr:f.addr);
    $('#cmp-to').value=rp;
    const others=(m.to||[]).filter(x=>x.addr&&x.addr.toLowerCase()!==S.account.toLowerCase()).map(x=>x.addr);
    $('#cmp-cc').value=mode==='replyall'?others.concat((m.cc||[]).map(x=>x.addr)).join(', '):'';
    $('#cmp-sub').value=/^re:/i.test(m.subject||'')?m.subject:'Re: '+(m.subject||'');
  } else if(mode==='forward'){
    $('#cmp-to').value=''; $('#cmp-cc').value='';
    $('#cmp-sub').value=/^fwd:/i.test(m.subject||'')?m.subject:'Fwd: '+(m.subject||'');
  } else if(mode==='draft'){
    $('#cmp-to').value=(m.to||[]).map(x=>x.addr).join(', ');
    $('#cmp-cc').value=(m.cc||[]).map(x=>x.addr).join(', ');
    $('#cmp-sub').value=m.subject||'';
  } else { $('#cmp-to').value=''; $('#cmp-cc').value=''; $('#cmp-sub').value=''; }
  let quote='';
  if(m.text&&(mode==='reply'||mode==='replyall'||mode==='forward')){
    const body=unescapeText(m.text);
    quote='\n\n\n---------- '+(mode==='forward'?'转发邮件':'原始邮件')+' ----------\n'+
      '发件人: '+f.name+' <'+f.addr+'>\n日期: '+(m.date||'')+'\n主题: '+(m.subject||'')+'\n\n'+body;
  }
  $('#cmp-body').value=(mode==='draft'?unescapeText(m.text):'')+quote;
  $('#cmp-file').value=''; $('#cmp-files').innerHTML=''; $('#cmp-hint').textContent='';
  $('#composer').classList.remove('hide');
  setTimeout(()=>$('#cmp-to').focus(),80);
}
function closeCompose(){ $('#composer').classList.add('hide'); }
async function submitCompose(action){
  const btn=$('#cmp-send'); btn.disabled=true; btn.textContent='处理中…';
  const obj={action:action,account:$('#cmp-from').value,to:$('#cmp-to').value,
    cc:$('#cmp-cc').value,subject:$('#cmp-sub').value,body:$('#cmp-body').value,
    in_reply_to:(S.compose.mode==='reply'||S.compose.mode==='replyall')?S.compose.inreply:'',
    references:(S.compose.mode==='reply'||S.compose.mode==='replyall')?S.compose.refs:'',
    draft_id:S.compose.mode==='draft'?S.compose.draft_id:'',
    attachments:JSON.stringify(S.compose.files)};
  try{
    const r=await post('mail/compose',obj);
    toast(r.msg||'操作完成',r.ok);
    if(r.ok){ closeCompose(); loadList(); loadFolders(); markFolderNav(); }
    else $('#cmp-hint').textContent=r.msg||'';
  }catch(e){ toast('请求失败：'+e.message,false); }
  btn.disabled=false; btn.textContent='发送邮件';
}

/* ---------------- system views ---------------- */
async function overview(){
  const d=await api('overview');
  const svc=Object.entries(d.services).map(([k,v])=>'<div class="kv"><div class="k">'+esc(k)+'</div><div class="v"><span class="pill '+(v==='active'?'ok':'bad')+'">'+esc(v)+'</span></div></div>').join('');
  const cells=[['Maddy 版本',d.version],['运行内存',d.memory_mb+' MB'],['启动时间',d.uptime],
    ['域名证书至',d.cert_end],['IP 证书','剩余 '+d.ip_days+' 天（自动续期）'],
    ['磁盘可用',d.disk.free_gb+' / '+d.disk.total_gb+' GB'],['MTA-STS',d.mta_sts]].map(([k,v])=>
    '<div class="kv"><div class="k">'+esc(k)+'</div><div class="v mono" style="font-size:12px">'+esc(String(v||'—').replace(/\n/g,' '))+'</div></div>').join('');
  $('#ov').innerHTML=svc+cells;
  $('#pub-url').href=d.ip_access; $('#pub-url').textContent=d.ip_access;
  paint($('#dns tbody'),Object.entries(d.dns).filter(([k])=>k!=='PTR').map(([k,v])=>'<th style="width:150px">'+esc(k)+'</th><td class="mono">'+esc(v||'—')+'</td>'));
}
/* ---------------- account management: inline password change ---------------- */
let ACCT_CACHE=[], ACCT_OPEN='';
function pwRules(pw,pw2,addr){
  const local=(addr||'').split('@')[0], blank=!pw;
  const cls=[/[a-z]/,/[A-Z]/,/[0-9]/,/[^A-Za-z0-9]/].filter(r=>r.test(pw)).length;
  return [['长度 10–128 位',!blank&&pw.length>=10&&pw.length<=128],
          ['含大写／小写／数字／符号中至少 3 类',!blank&&cls>=3],
          ['不包含邮箱名',!blank&&(!local||!pw.toLowerCase().includes(local.toLowerCase()))],
          ['两次输入一致',!blank&&pw===pw2]];
}
function strongPw(){
  const cs='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  const a=new Uint32Array(18); crypto.getRandomValues(a);
  return 'Pw-'+Array.from(a,x=>cs[x%cs.length]).join('')+'!';
}
function pwPanel(addr){
  const main=addr==='admin@example.com';
  return '<tr class="pwrow"><td colspan="5"><div class="pwbox" data-pwbox="'+esc(addr)+'">'+
   '<div class="pwhead"><div><span class="lb">正在修改密码</span><div class="pwaddr">'+esc(addr)+'</div></div>'+
   '<button class="mini" data-pw="close" type="button">✕ 取消</button></div>'+
   (main?'<div class="pwwarn">这是<b>主管理员账号</b>。修改会同步服务器凭据文件，两个旧密码立即失效；当前会话仍有效，请务必保存新密码后再关闭本页。</div>':'')+
   '<div class="row"><div><label class="lb">新密码</label><div class="pwin">'+
   '<input class="field" type="password" data-pw="new" autocomplete="new-password" placeholder="10–128 位">'+
   '<button class="mini eye" data-pw="eye" type="button">显示</button></div></div>'+
   '<div><label class="lb">确认新密码</label><input class="field" type="password" data-pw="confirm" autocomplete="new-password" placeholder="再输入一次"></div></div>'+
   '<div class="pwrules" data-pw="rules"></div>'+
   '<div class="bar" style="margin-bottom:0">'+
   '<button class="btn sm" data-pw="save" disabled>保存新密码</button>'+
   '<button class="btn ghost sm" data-pw="gen" type="button">生成强密码</button>'+
   '<button class="btn ghost sm" data-pw="copy" type="button">复制密码</button>'+
   '<span class="pwres" data-pw="res"></span></div></div></td></tr>';
}
/* v6: acctRows()/accounts() live in the organisations block. */
function pwState(){
  const box=$('[data-pwbox]'); if(!box) return null;
  return {box:box,addr:box.dataset.pwbox,
          p1:box.querySelector('[data-pw="new"]'),p2:box.querySelector('[data-pw="confirm"]')};
}
function pwRefresh(){
  const s=pwState(); if(!s) return;
  const blank=!s.p1.value;
  const rules=pwRules(s.p1.value,s.p2.value,s.addr);
  s.box.querySelector('[data-pw="rules"]').innerHTML=rules.map(([t,ok])=>
    '<span class="'+(blank?'':(ok?'ok':'bad'))+'">'+(ok&&!blank?'✓ ':'○ ')+t+'</span>').join('');
  s.box.querySelector('[data-pw="save"]').disabled=!rules.every(r=>r[1]);
}
function pwMsg(txt,ok){
  const s=pwState(); if(!s) return;
  const e=s.box.querySelector('[data-pw="res"]');
  e.className='pwres '+(ok?'ok':'bad'); e.textContent=txt;
}
function copyText(t){
  if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(t).catch(()=>{}); return; }
  const ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta);
  ta.select(); try{ document.execCommand('copy'); }catch(err){} ta.remove();
}
$('#acct-q').addEventListener('input',()=>{ ACCT_OPEN=''; acctRows(); });
$('#acct').addEventListener('input',e=>{
  if(e.target.matches('[data-pw="new"],[data-pw="confirm"]')) pwRefresh();
});
$('#acct').addEventListener('click',async e=>{
  const b=e.target.closest('button'); if(!b) return;
  if(b.dataset.del){
    const em=b.dataset.del;
    if(!confirm('确认删除账号 '+em+'？该邮箱的所有邮件将一并移除。')) return;
    const r=await post('account/remove',{email:em});
    showMsg('#acct-msg',r.msg,r.ok);
    if(ACCT_OPEN===em) ACCT_OPEN='';
    accounts(); return;
  }
  const act=b.dataset.pw;
  if(act==='open'){
    const em=b.dataset.email; ACCT_OPEN=(ACCT_OPEN===em?'':em); acctRows();
    if(ACCT_OPEN){ pwRefresh(); const inp=$('[data-pw="new"]'); if(inp) inp.focus(); }
    return;
  }
  const s=pwState(); if(!s) return;
  if(act==='close'){ ACCT_OPEN=''; acctRows(); return; }
  if(act==='eye'){
    const t=s.p1.type==='password'?'text':'password';
    s.p1.type=t; s.p2.type=t; b.textContent=(t==='text'?'隐藏':'显示'); return;
  }
  if(act==='gen'){
    const pw=strongPw(); s.p1.value=pw; s.p2.value=pw;
    s.p1.type='text'; s.p2.type='text';
    s.box.querySelector('[data-pw="eye"]').textContent='隐藏';
    pwRefresh(); pwMsg('已生成强密码，请复制保存后再提交',true); return;
  }
  if(act==='copy'){
    if(!s.p1.value){ pwMsg('请先输入或生成新密码',false); return; }
    copyText(s.p1.value); pwMsg('已复制到剪贴板',true); return;
  }
  if(act==='save'){
    const rules=pwRules(s.p1.value,s.p2.value,s.addr);
    if(!rules.every(r=>r[1])){ pwMsg('请先满足上方所有密码要求',false); return; }
    b.disabled=true; b.textContent='保存中…'; pwMsg('正在提交…',true);
    let r;
    try{ r=await post('account/password',{email:s.addr,password:s.p1.value}); }
    catch(err){ r={ok:false,msg:'请求失败：'+err.message}; }
    b.disabled=false; b.textContent='保存新密码';
    if(r.ok){
      pwMsg('✓ 密码已更新，旧密码立即失效。需要留存请点「复制密码」。',true);
      showMsg('#acct-msg','已为 '+s.addr+' 更新密码',true);
    }else{
      pwMsg('✕ '+(r.msg||'更新失败'),false);
      showMsg('#acct-msg',r.msg||'更新失败',false);
    }
    return;
  }
});
async function batch(){
  const d=await api('batch/status');
  if(!d.batch){ $('#b-meta').textContent='暂无批次'; paint($('#b-list tbody'),['<td class="note">请先在上方点击「批量创建账号」</td>']); return; }
  const s=d.summary;
  $('#b-meta').textContent='批次 '+d.batch.id+' · 共 '+s.total+' 个 · 待激活 '+s.pending+' · 已激活 '+s.activated+' · 已发邀请 '+s.invited;
  paint($('#b-list tbody'),d.batch.accounts.map(a=>
    '<td class="mono">'+esc(a.email)+(a.cn_name?' <span class="note">('+esc(a.cn_name)+')</span>':'')+'</td>'+
    '<td><span class="pill '+(a.status==='activated'?'ok':'bad')+'">'+(a.status==='activated'?'已激活':'待激活')+'</span></td>'+
    '<td class="mono" style="font-size:11px">'+esc(a.invited_at||'—')+'</td>'+
    '<td class="mono" style="font-size:11px">'+esc(a.activated_at||'—')+'</td>'+
    '<td><a href="'+esc(a.activate_url)+'" target="_blank" rel="noopener">打开激活页</a></td>'));
}
async function check(){
  const d=await api('check');
  paint($('#k-list tbody'),d.checks.map(c=>'<td><span class="pill '+(c.ok?'ok':'bad')+'">'+(c.ok?'通过':'注意')+'</span></td><th style="width:190px">'+esc(c.name)+'</th><td class="mono">'+esc(c.value)+'</td>'));
}
async function logs(){
  const d=await api('logs?lines='+$('#l-n').value); $('#l-out').textContent=d.lines.join('\n')||'(无日志)';
}

/* ---------------- organisations (v6) ---------------- */
let ORG_CACHE=[], ORG_ASSIGN={}, ORG_DATA=null, ORG_EDIT='';
let ACCT_SEL=new Set();
function orgColor(id){ const o=ORG_CACHE.find(x=>x.id===id); return o?(o.color||'#6b6257'):'#8a8175'; }
function orgName(id){ const o=ORG_CACHE.find(x=>x.id===id); return o?o.name:''; }
function orgOpts(mode,cur){
  let opts=[];
  if(mode==='filter') opts.push(['__all__','全部组织'],['__none__','未分配']);
  else if(mode==='from') opts.push(['__none__','未分配成员']);
  else opts.push(['','不分配 / 未分配']);
  ORG_CACHE.forEach(o=>opts.push([o.id,o.name]));
  return opts.map(([v,n])=>'<option value="'+esc(v)+'"'+(String(cur||'')===String(v)?' selected':'')+'>'+esc(n)+'</option>').join('');
}
function fillOrgSelects(){
  const defs=[['#a-org','assign'],['#acct-org-filter','filter'],['#acct-bulk-org','assign'],
              ['#b-org','assign'],['#org-move-from','from'],['#org-move-to','to']];
  defs.forEach(([sel,mode])=>{
    const e=$(sel); if(!e) return;
    const old=e.value;
    e.innerHTML=orgOpts(mode,old);
    if(Array.prototype.some.call(e.options,o=>o.value===old)) e.value=old;
  });
}
function filteredAccounts(){
  const q=($('#acct-q').value||'').trim().toLowerCase();
  const f=$('#acct-org-filter')?$('#acct-org-filter').value:'__all__';
  return ACCT_CACHE.filter(a=>{
    if(q && !a.email.toLowerCase().includes(q)) return false;
    if(f==='__none__' && a.org_id) return false;
    if(f!=='__all__' && f!=='__none__' && a.org_id!==f) return false;
    return true;
  });
}
function orgTag(a){
  if(!a.org_id) return '<span class="orgtag none">未分配</span>';
  return '<span class="orgtag"><span class="dotc" style="background:'+esc(orgColor(a.org_id))+'"></span>'+esc(a.org_name||orgName(a.org_id)||'已删除组织')+'</span>';
}
function acctRows(){
  const list=filteredAccounts();
  const q=($('#acct-q').value||'').trim();
  const f=$('#acct-org-filter')?$('#acct-org-filter').value:'__all__';
  $('#acct-total').textContent=ACCT_CACHE.length;
  $('#acct-count').textContent=(q||f!=='__all__')?'· 匹配 '+list.length+' 个':'';
  $('#acct-seln').textContent=ACCT_SEL.size;
  const pk=$('#acct-pickall');
  if(pk) pk.checked=list.length>0&&list.every(a=>ACCT_SEL.has(a.email));
  if(!list.length){ $('#acct').innerHTML='<div class="note">没有匹配的账号</div>'; return; }
  $('#acct').innerHTML='<table><thead><tr><th style="width:30%">邮箱</th><th style="width:19%">组织</th>'+
   '<th>认证</th><th>IMAP 存储</th><th style="width:190px">操作</th></tr></thead><tbody>'+list.map(a=>{
    const open=ACCT_OPEN===a.email;
    return '<tr><td class="mono"><label class="pickline"><input type="checkbox" class="chk" data-pick="'+esc(a.email)+'"'+
     (ACCT_SEL.has(a.email)?' checked':'')+'>'+esc(a.email)+'</label></td>'+
     '<td>'+orgTag(a)+'<select class="orgsel inline" data-orgset="'+esc(a.email)+'">'+orgOpts('row',a.org_id||'')+'</select></td>'+
     '<td><span class="pill '+(a.creds?'ok':'bad')+'">'+(a.creds?'正常':'缺失')+'</span></td>'+
     '<td><span class="pill '+(a.imap?'ok':'bad')+'">'+(a.imap?'正常':'缺失')+'</span></td>'+
     '<td><button class="btn sm" data-pw="open" data-email="'+esc(a.email)+'">'+(open?'收起':'修改密码')+'</button> '+
     '<button class="btn ghost sm" data-del="'+esc(a.email)+'">删除</button></td></tr>'+
     (open?pwPanel(a.email):'');
   }).join('')+'</tbody></table>';
}
async function accounts(){
  const d=await api('accounts');
  ACCT_CACHE=d.accounts||[];
  if(d.orgs) ORG_CACHE=d.orgs.map(o=>({id:o.id,name:o.name,color:o.color,count:0,builtin:false}));
  fillOrgSelects();
  acctRows();
}
async function orgs(){
  const d=await api('orgs');
  ORG_CACHE=d.orgs||[]; ORG_ASSIGN=d.assignments||{}; ORG_DATA=d;
  fillOrgSelects();
  renderOrgStats(d);
  renderOrgList();
}
function renderOrgStats(d){
  const total=Math.max(1,d.total_accounts||0);
  const bar=(name,color,n)=>'<div class="orgstat"><div class="osname">'+
    '<span class="dotc" style="background:'+esc(color)+'"></span>'+esc(name)+'</div>'+
    '<div class="osbar"><i style="width:'+Math.max(n?3:0,Math.round(n/total*100))+'%;background:'+esc(color)+'"></i></div>'+
    '<div class="osnum">'+n+' 人</div></div>';
  $('#org-sum').textContent='共 '+(d.total_accounts||0)+' 个邮箱 · '+(ORG_CACHE.length||0)+' 个组织 · '+(d.unassigned||0)+' 个未分配 · 更新于 '+esc(d.updated_at||'—');
  $('#org-stats').innerHTML=(ORG_CACHE.map(o=>bar(o.name,o.color,o.count||0)).join('')+
    bar('未分配','#c9bfae',d.unassigned||0))||'<div class="note">暂无组织</div>';
}
function renderOrgList(){
  if(!ORG_CACHE.length){ $('#org-list').innerHTML='<div class="note">暂无组织，请在上方新增</div>'; return; }
  $('#org-list').innerHTML='<table><thead><tr><th style="width:34%">组织</th><th style="width:16%">成员数</th>'+
    '<th style="width:14%">类型</th><th>操作</th></tr></thead><tbody>'+ORG_CACHE.map(o=>{
    const editing=ORG_EDIT===o.id;
    return '<tr><td>'+(editing?
      '<div class="orgedit"><input class="field" id="org-edit-name" value="'+esc(o.name)+'" maxlength="20">'+
      '<button class="btn sm" data-orgsave="'+esc(o.id)+'">保存</button>'+
      '<button class="btn ghost sm" data-orgcancel="1">取消</button></div>':
      '<span class="orgtag"><span class="dotc" style="background:'+esc(o.color)+'"></span>'+esc(o.name)+'</span>')+'</td>'+
     '<td><span class="pill">'+ (o.count||0) +' 人</span></td>'+
     '<td><span class="note">'+(o.builtin?'系统预置':'自定义')+'</span></td>'+
     '<td>'+(editing?'<span class="note">正在重命名…</span>':
       '<button class="btn ghost sm" data-orgedit="'+esc(o.id)+'">重命名</button> '+
       '<button class="btn ghost sm" data-orgdel="'+esc(o.id)+'">删除</button>')+'</td></tr>';
  }).join('')+'</tbody></table>';
}
async function applyBulkOrg(useFilter){
  const oid=$('#acct-bulk-org').value||'';
  const list=useFilter?filteredAccounts():ACCT_CACHE.filter(a=>ACCT_SEL.has(a.email));
  if(!list.length){ showMsg('#acct-bulk-msg','请先勾选账号，或使用「应用到当前筛选结果」',false); return; }
  if(!confirm('确认将 '+list.length+' 个邮箱调整到「'+(oid?orgName(oid):'未分配')+'」？')) return;
  const r=await post('account/org_bulk',{emails:list.map(a=>a.email).join(','),org_id:oid});
  showMsg('#acct-bulk-msg',r.msg,r.ok);
  if(r.ok){ ACCT_SEL.clear(); await accounts(); }
}
async function bindBatchOrg(r){
  const oid=$('#b-org')?$('#b-org').value:'';
  if(!r.ok || !oid || !r.accounts || !r.accounts.length) return '';
  try{
    const x=await post('account/org_bulk',{emails:r.accounts.map(a=>a.email).join(','),org_id:oid});
    return x.ok?('；已归入「'+orgName(oid)+'」'):('；组织归属失败：'+x.msg);
  }catch(err){ return '；组织归属失败：'+err.message; }
}
$('#acct-org-filter').addEventListener('change',()=>{ ACCT_SEL.clear(); acctRows(); });
$('#acct-pickall').addEventListener('change',e=>{
  const list=filteredAccounts();
  if(e.target.checked) list.forEach(a=>ACCT_SEL.add(a.email));
  else list.forEach(a=>ACCT_SEL.delete(a.email));
  acctRows();
});
$('#acct').addEventListener('change',async e=>{
  const t=e.target;
  if(t.matches('[data-pick]')){
    if(t.checked) ACCT_SEL.add(t.dataset.pick); else ACCT_SEL.delete(t.dataset.pick);
    /* Do not repaint the table while ticking: it would reset an open password
       panel and replace the very checkboxes the user is clicking. */
    $('#acct-seln').textContent=ACCT_SEL.size;
    const list=filteredAccounts(), pk=$('#acct-pickall');
    if(pk) pk.checked=list.length>0&&list.every(a=>ACCT_SEL.has(a.email));
    return;
  }
  if(t.matches('[data-orgset]')){
    const em=t.dataset.orgset, oid=t.value;
    const r=await post('account/org',{email:em,org_id:oid});
    showMsg('#acct-msg',r.msg,r.ok);
    const a=ACCT_CACHE.find(x=>x.email===em);
    if(r.ok&&a){ a.org_id=r.org_id||''; a.org_name=r.org_name||''; }
    acctRows();
    if(S.view==='sys'&&S.sysTab==='orgs') orgs();
  }
});
$('#acct-bulk-apply').onclick=()=>applyBulkOrg(false);
$('#acct-bulk-filtered').onclick=()=>applyBulkOrg(true);
$('#org-add').onclick=async()=>{
  const name=$('#org-new-name').value.trim(), color=$('#org-new-color').value;
  const r=await post('org/add',{name:name,color:color});
  showMsg('#org-msg',r.msg,r.ok);
  if(r.ok) $('#org-new-name').value='';
  await orgs(); await accounts();
};
$('#org-list').addEventListener('click',async e=>{
  const b=e.target.closest('button'); if(!b) return;
  if(b.dataset.orgedit){ ORG_EDIT=b.dataset.orgedit; renderOrgList(); const i=$('#org-edit-name'); if(i) i.focus(); return; }
  if(b.dataset.orgcancel){ ORG_EDIT=''; renderOrgList(); return; }
  if(b.dataset.orgsave){
    const name=($('#org-edit-name')?$('#org-edit-name').value:'').trim();
    const r=await post('org/rename',{id:b.dataset.orgsave,name:name});
    showMsg('#org-msg',r.msg,r.ok);
    if(r.ok) ORG_EDIT='';
    await orgs(); await accounts(); return;
  }
  if(b.dataset.orgdel){
    const o=ORG_CACHE.find(x=>x.id===b.dataset.orgdel);
    if(!confirm('确认删除组织「'+(o?o.name:'')+'」？其成员将变为未分配，邮箱账号不会被删除。')) return;
    const r=await post('org/remove',{id:b.dataset.orgdel});
    showMsg('#org-msg',r.msg,r.ok);
    await orgs(); await accounts();
  }
});
$('#org-move-go').onclick=async()=>{
  const from=$('#org-move-from').value, to=$('#org-move-to').value||'';
  if(from===(to||'__none__')){ showMsg('#org-move-msg','来源与目标相同，无需迁移',false); return; }
  const ad=await api('accounts');
  const emails=(ad.accounts||[]).filter(a=>(a.org_id||'__none__')===from).map(a=>a.email);
  if(!emails.length){ showMsg('#org-move-msg','来源没有成员',false); return; }
  if(!confirm('确认将 '+emails.length+' 个邮箱整体迁移到「'+(to?orgName(to):'未分配')+'」？')) return;
  const r=await post('account/org_bulk',{emails:emails.join(','),org_id:to});
  showMsg('#org-move-msg',r.msg,r.ok);
  await orgs(); await accounts();
};

/* ---------------- events ---------------- */
let qt=null;
$('#gq').oninput=e=>{ clearTimeout(qt); const v=e.target.value.trim(); qt=setTimeout(()=>{ S.q=v; S.page=1; loadList(); },320); };
$('#idpick').onchange=e=>{ S.account=e.target.value; S.page=1; S.q=''; S.filter='all'; S.selected.clear(); S.current=null;
  $('#gq').value=''; resetRead(); loadFolders(); loadList().then(previewFirst); toast('已切换到 '+S.account); };
$('#psize').onchange=e=>{ S.size=parseInt(e.target.value,10); S.page=1; loadList(); };
$('#prev').onclick=()=>{ if(S.page>1){ S.page--; loadList(); } };
$('#next').onclick=()=>{ S.page++; loadList(); };
$('#pickall').onchange=e=>{ S.selected.clear(); if(e.target.checked) S.messages.forEach(m=>S.selected.add(m.id));
  $$('[data-pick]').forEach(c=>c.checked=e.target.checked); syncTools(); };
$('#a-seen').onclick=()=>bulk('seen'); $('#a-unseen').onclick=()=>bulk('unseen');
$('#a-trash').onclick=()=>bulk(S.folder==='Trash'?'delete':'trash','Trash');
$('#a-move').onchange=e=>{ if(e.target.value) bulk('trash',e.target.value); };
$$('[data-fl]').forEach(b=>b.onclick=()=>{ S.filter=b.dataset.fl; S.page=1; S.selected.clear();
  $$('[data-fl]').forEach(x=>x.classList.toggle('on',x===b)); loadList(); });
$('#newmail').onclick=()=>compose('new'); $('#compose2').onclick=()=>compose('new');
$('#refresh').onclick=refreshAll;
$('#logout').onclick=()=>{ location.href='/admin/logout'; };
$('#cmp-close').onclick=closeCompose; $('#cmp-cancel').onclick=closeCompose;
$('#cmp-send').onclick=()=>submitCompose('send');
$('#cmp-draft').onclick=()=>submitCompose('draft');
$('#a-move').innerHTML='<option value="">移动到…</option>'+['Sent','Archive','Junk','Trash','INBOX'].map(f=>'<option value="'+f+'">'+
  ({Sent:'已发送',Archive:'归档',Junk:'垃圾邮件',Trash:'已删除',INBOX:'收件箱'}[f])+'</option>').join('');
$('#cmp-file').onchange=e=>{
  const files=Array.from(e.target.files||[]); let total=S.compose.files.reduce((a,x)=>a+x.data.length*0.75,0);
  S.compose.files=S.compose.files.slice(0,10);
  for(const f of files){
    if(f.size>2*1024*1024){ toast(f.name+' 超过 2 MB，已跳过',false); continue; }
    if(total+f.size>4*1024*1024){ toast('附件总大小超过 4 MB',false); break; }
    const reader=new FileReader();
    reader.onload=()=>{ S.compose.files.push({name:f.name,type:f.type||'application/octet-stream',data:String(reader.result).split(',')[1]});
      total+=f.size; renderFiles(); };
    reader.readAsDataURL(f);
  }
};
function renderFiles(){ $('#cmp-files').innerHTML=S.compose.files.map((f,i)=>'<span class="filechip">'+esc(f.name)+' <b data-rm="'+i+'">✕</b></span>').join('');
  $$('[data-rm]').forEach(b=>b.onclick=()=>{ S.compose.files.splice(parseInt(b.dataset.rm,10),1); renderFiles(); }); }
$('#a-add').onclick=async()=>{
  const em=$('#a-email').value.trim();
  let pw=$('#a-pw').value.trim(); if(!pw){ pw='Mail-'+Math.random().toString(36).slice(2,12)+Math.random().toString(36).slice(2,12); $('#a-pw').value=pw; }
  const org=$('#a-org')?$('#a-org').value:'';
  const r=await post('account/add',{email:em,password:pw,org_id:org});
  showMsg('#a-msg',(r.ok?'创建成功，初始密码：'+pw+'\n':'')+r.msg,r.ok);
  if(r.ok){ $('#a-email').value=''; $('#a-pw').value=''; }
  accounts(); boot();
};
$('#b-create').onclick=async()=>{
  const r=await post('batch/create',{count:$('#b-count').value});
  const extra=await bindBatchOrg(r);
  showMsg('#b-msg',r.msg+extra+(r.errors&&r.errors.length?'\n'+r.errors.join('\n'):''),r.ok); batch();
};
$('#b-custom-go').onclick=async()=>{ const list=$('#b-custom').value.trim();
  if(!list){ showMsg('#b-custom-msg','请先粘贴邮箱名单',false); return; }
  if(!confirm('确认按名单批量创建账号？')) return;
  const r=await post('batch/create_custom',{emails:list});
  const extra=await bindBatchOrg(r);
  showMsg('#b-custom-msg',r.msg+extra+(r.errors&&r.errors.length?'\n'+r.errors.slice(0,10).join('\n'):''),r.ok); batch();
};
$('#b-invite').onclick=async()=>{ const r=await post('batch/invite',{}); showMsg('#b-msg',r.msg+'（成功 '+r.sent.length+' 封）',r.ok); batch(); };
$('#b-activate').onclick=async()=>{ if(!confirm('确认批量激活本批次全部待激活账号？'))return; const r=await post('batch/activate',{}); showMsg('#b-msg',r.msg,r.ok); batch(); };
$('#b-refresh').onclick=batch;
$('#b-remove').onclick=async()=>{ if(!confirm('确认删除本批次全部测试账号及其邮件？此操作不可撤销。'))return; const r=await post('batch/remove',{}); showMsg('#b-msg',r.msg,r.ok); batch(); boot(); };
$('#k-run').onclick=check; $('#l-refresh').onclick=logs;
document.addEventListener('keydown',e=>{ if(e.key==='Escape'){ closeCompose(); } });
boot().catch(e=>{ toast('初始化失败：'+e.message,false); console.error(e); });
</script></body></html>
"""

ACT_CSS = (
    "body{margin:0;background:#faf8f3;color:#26221d;font:15px/1.7 -apple-system,"
    "BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}"
    ".box{max-width:620px;margin:0 auto;padding:60px 22px}"
    "h1{font:400 27px/1.3 Georgia,'Songti SC',serif;margin:0 0 8px}"
    ".sub{color:#7a7268;font-size:13px;margin-bottom:28px}"
    ".card{background:#fffdf9;border:1px solid #e5ded2;border-left:3px solid #b4533a;"
    "border-radius:3px;padding:26px 28px}"
    "label{display:block;font-size:12px;color:#7a7268;margin:16px 0 6px}"
    "input{width:100%;padding:10px 12px;border:1px solid #e5ded2;border-radius:2px;"
    "background:#fff;font:inherit;color:#26221d}"
    "button{margin-top:20px;background:#b4533a;color:#fff;border:0;border-radius:2px;"
    "padding:10px 22px;cursor:pointer;font:inherit}"
    ".addr{font-family:ui-monospace,Menlo,monospace;font-size:16px;color:#b4533a}"
    "pre{background:#f6f2ea;border:1px solid #e5ded2;padding:14px;font:12px/1.7 "
    "ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all}"
    ".err{border-left-color:#a63d3d}.ok{border-left-color:#3f7d58}"
    ".note{color:#7a7268;font-size:12px;margin-top:20px}a{color:#b4533a}"
)


def act_page(title, inner, tone="neutral"):
    cls = {"neutral": "", "ok": " ok", "bad": " err"}.get(tone, "")
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s - example.com 邮箱激活</title><style>%s</style></head>"
            "<body><div class=\"box\"><h1>%s</h1>"
            "<div class=\"sub\">example.com 企业邮箱 · 账号激活</div>"
            "<div class=\"card%s\">%s</div>"
            "<div class=\"note\">本页由邮件服务器本机提供，通过一次性令牌校验，无需管理台密码。"
            "令牌仅可使用一次，有效期 72 小时。</div></div></body></html>"
            % (html.escape(title), ACT_CSS, html.escape(title), cls, inner))


class Handler(BaseHTTPRequestHandler):
    server_version = "MaddyAdmin/4.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        if os.environ.get("ADMIN_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def client_ip(self):
        return self.headers.get("X-Real-IP", self.client_address[0])

    def send_body(self, status, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = localize(body).encode("utf-8")
        elif ctype.startswith("text/html") or ctype.startswith("application/json"):
            body = localize(body.decode("utf-8", "replace")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        ip = self.client_ip()
        with LOCK:
            rec = FAILURES.get(ip, [0, 0])
            if rec[1] > time.time():
                return False, True
        hdr = self.headers.get("Authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:], validate=True).decode("utf-8")
                user, pw = raw.split(":", 1)
                ok = (hmac.compare_digest(user, AUTH_USER)
                      and hmac.compare_digest(pw, AUTH_PASS))
            except Exception:
                ok = False
        with LOCK:
            if ok:
                FAILURES.pop(ip, None)
            else:
                rec = FAILURES.get(ip, [0, 0])
                rec[0] += 1
                if rec[0] >= MAX_FAILS:
                    rec[1] = time.time() + LOCK_SECONDS
                    rec[0] = 0
                FAILURES[ip] = rec
        return ok, False

    def origin_ok(self):
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True
        try:
            host = urllib.parse.urlsplit(origin).netloc.lower()
            allowed = {h for h in (MAIL_HOSTNAME, "mta-sts." + DOMAIN, DOMAIN,
                                   SERVER_IP) if h}
            return host in allowed or host == self.headers.get("Host", "").lower()
        except Exception:
            return False

    def read_form(self):
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 8 * 1024 * 1024)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        except Exception:
            return {}

    # ---- public, token-authenticated activation page (no Basic Auth) --------

    def handle_activate(self, method, query):
        if method == "GET":
            token = query.get("token", [""])[0]
            if not token:
                return self.send_body(200, act_page(
                    "链接不完整",
                    "<p>请从邀请邮件中复制<strong>完整</strong>链接后打开。</p>", "bad"))
            batch, acct = find_token(load_state(), token)
            if not acct:
                return self.send_body(200, act_page(
                    "激活链接无效",
                    "<p>未找到该令牌对应的账号，可能链接被截断或已失效。</p>", "bad"))
            if acct.get("status") == "activated":
                return self.send_body(200, act_page(
                    "该账号已激活",
                    "<p><span class=\"addr\">%s</span> 已于 %s 完成激活，"
                    "此链接不可重复使用。</p>"
                    "<p>可直接用已设置的密码登录 IMAP / SMTP。</p>"
                    % (html.escape(acct["email"]),
                       html.escape(str(acct.get("activated_at") or "—"))), "ok"))
            if time.time() - float(acct.get("created_ts", 0)) > TOKEN_TTL:
                return self.send_body(200, act_page(
                    "激活链接已过期",
                    "<p>令牌有效期为 72 小时，请联系管理员重新发送邀请。</p>", "bad"))
            inner = (
                "<p>请为邮箱 <span class=\"addr\">%s</span> 设置登录密码，提交后账号立即启用。</p>"
                "<form method=\"post\" action=\"/admin/activate\">"
                "<input type=\"hidden\" name=\"token\" value=\"%s\">"
                "<label>登录密码（至少 10 位）</label>"
                "<input type=\"password\" name=\"password\" minlength=\"10\" required autocomplete=\"new-password\">"
                "<label>再次输入密码</label>"
                "<input type=\"password\" name=\"password2\" minlength=\"10\" required autocomplete=\"new-password\">"
                "<button type=\"submit\">设置密码并激活</button></form>"
                "<div class=\"note\">激活成功后可用该密码登录 IMAP(S) / SMTP(S)；"
                "如需修改，请联系管理员在管理台重置。</div>"
                % (html.escape(acct["email"]), html.escape(token))
            )
            return self.send_body(200, act_page("激活您的企业邮箱", inner))

        if method == "POST":
            form = self.read_form()
            token = form.get("token", "")
            pw, pw2 = form.get("password", ""), form.get("password2", "")
            if pw != pw2:
                return self.send_body(200, act_page(
                    "两次输入的密码不一致",
                    "<p>请 <a href=\"/admin/activate?token=%s\">返回上一页</a> 重新填写。</p>"
                    % html.escape(urllib.parse.quote(token)), "bad"))
            ok, msg, acct = activate_one(token, pw, "link")
            if not ok:
                return self.send_body(200, act_page(
                    "激活未完成", "<p>%s</p>" % html.escape(msg), "bad"))
            inner = (
                "<p>邮箱 <span class=\"addr\">%s</span> 已成功激活，登录密码已生效。</p>"
                "<h3 style=\"font-size:13px;color:#7a7268\">客户端连接参数</h3>"
                "<pre>IMAP   mail.example.com:993   SSL/TLS\n"
                "SMTP   mail.example.com:465   SSL/TLS\n"
                "SMTP   mail.example.com:587   STARTTLS\n"
                "用户名 完整邮箱地址\n"
                "密码   您刚刚设置的密码</pre>"
                "<div class=\"note\">此激活链接现已失效，无法再次使用。</div>"
                % html.escape(acct["email"])
            )
            return self.send_body(200, act_page("激活成功", inner, "ok"))

        return self.send_body(405, "Method Not Allowed", "text/plain; charset=utf-8")

    def handle_one(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        # Public activation endpoint: authenticated by the one-time token only.
        if path.rstrip("/") == BASE + "/activate":
            return self.handle_activate(method, query)
        ok, locked = self.authorized()
        if not ok:
            self.send_body(401, "认证失败" + ("（尝试过多，请 10 分钟后重试）" if locked else ""),
                           "text/plain; charset=utf-8",
                           {"WWW-Authenticate": 'Basic realm="Maddy Admin"'})
            return
        if not self.origin_ok():
            self.send_body(403, "来源校验失败", "text/plain; charset=utf-8")
            return
        if path in (BASE, BASE + "/") and method == "GET":
            self.send_body(200, PAGE)
            return
        if not path.startswith(BASE + "/api/"):
            self.send_body(404, "Not Found", "text/plain; charset=utf-8")
            return
        name = path[len(BASE + "/api/"):]
        payload = {}
        if method == "POST":
            payload = self.read_form()
        # Binary endpoints (attachment downloads / .eml) are served directly.
        if method == "GET" and name in ("mail/attachment",):
            try:
                blob, filename, ctype = api_mail_attachment(query)
            except Exception as exc:
                self.send_body(400, "下载失败: %s" % exc, "text/plain; charset=utf-8")
                return
            ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)[:80] or "file.bin"
            disp = ("attachment; filename=\"%s\"; filename*=UTF-8''%s"
                    % (ascii_name, urllib.parse.quote(filename)))
            self.send_body(200, blob, ctype, {"Content-Disposition": disp})
            return
        try:
            if method == "GET":
                get_routes = {"overview": api_overview, "accounts": api_accounts,
                              "inbox": lambda: api_inbox(query),
                              "message": lambda: api_message(query),
                              "logs": lambda: api_logs(query), "check": api_check,
                              "batch/status": lambda: api_batch_status(query),
                              "mail/identities": lambda: api_mail_identities(query),
                              "mail/folders": lambda: api_mail_folders(query),
                              "mail/list": lambda: api_mail_list(query),
                              "mail/read": lambda: api_mail_read(query)}
                get_routes.update({"orgs": api_orgs})
                result = get_routes[name]()

            else:
                post_routes = {"account/add": api_account_add,
                               "account/password": api_account_password,
                               "account/remove": api_account_remove,
                               "send": api_send,
                               "batch/create": api_batch_create,
                               "batch/create_custom": api_batch_create_custom,
                               "batch/invite": api_batch_invite,
                               "batch/activate": api_batch_activate,
                               "batch/remove": api_batch_remove,
                               "mail/action": api_mail_action,
                               "mail/compose": api_mail_compose}
                post_routes.update({"org/add": api_org_add, "org/rename": api_org_rename, "org/remove": api_org_remove, "account/org": api_account_org, "account/org_bulk": api_account_org_bulk})
                result = post_routes[name](payload)

        except KeyError:
            self.send_body(404, "Unknown API", "text/plain; charset=utf-8")
            return
        except Exception as exc:
            result = {"ok": False, "msg": "执行异常: %s" % exc}
        self.send_body(200, json.dumps(result, ensure_ascii=False),
                       "application/json; charset=utf-8")

    def do_GET(self):
        self.handle_one("GET")

    def do_POST(self):
        self.handle_one("POST")


# ================================================================ v5 request handler
# Subclasses the v4 handler so the activation flow and all response helpers are
# reused verbatim; only authentication / routing / scope enforcement changes.
class V5Handler(Handler):
    server_version = "MaddyAdmin/6.0"
    sys_version = ""

    USER_GET_APIS = {"mail/identities", "mail/folders", "mail/list",
                     "mail/read", "mail/attachment"}
    USER_POST_APIS = {"mail/action", "mail/compose"}

    def current_session(self):
        return session_get(cookie_sid(self.headers))

    def json_reply(self, status, obj, extra=None):
        self.send_body(status, json.dumps(obj, ensure_ascii=False),
                       "application/json; charset=utf-8", extra or {})

    def redirect(self, location, cookie=None):
        extra = {"Location": location}
        if cookie:
            extra["Set-Cookie"] = cookie
        self.send_body(302, "", "text/plain; charset=utf-8", extra)

    # ------------------------------------------------------------ public endpoints
    def handle_login(self, method):
        if method == "GET":
            return self.send_body(200, auth_page("login"))
        form = self.read_form()
        ip = self.client_ip()
        addr = (form.get("email") or "").strip().lower()
        pw = form.get("password") or ""
        k_ip, k_acct = "ip:" + ip, "acct:" + addr
        if locked(k_ip) or locked(k_acct):
            audit("LOGIN_LOCKED", addr, ip)
            return self.json_reply(429, {"ok": False,
                                         "msg": "尝试次数过多，请 10 分钟后再试"})
        session_pw = None
        ok = False
        if EMAIL_RE.match(addr) and pw and addr in existing_accounts():
            ok = verify_mail_password(addr, pw)
            if ok:
                session_pw = pw
            elif addr == ADMIN_MAIL and hmac.compare_digest(pw, AUTH_PASS):
                # Legacy admin console password stays valid for the admin
                # account only; the session still holds the real Maddy secret.
                ok, session_pw = True, MAILPASS
        if not ok:
            note_fail(k_ip, k_acct)
            audit("LOGIN_FAIL", addr, ip)
            return self.json_reply(401, {"ok": False, "msg": "邮箱地址或密码错误"})
        clear_fail(k_ip, k_acct)
        sid = session_create(addr, session_pw, ip)
        audit("LOGIN_OK", addr, ip)
        return self.json_reply(200, {"ok": True, "msg": "登录成功"},
                               {"Set-Cookie": sid_cookie(sid)})

    def handle_register(self, method):
        if method == "GET":
            return self.send_body(200, auth_page("register"))
        return self.json_reply(200, do_register(self.read_form(), self.client_ip()))

    def handle_logout(self):
        sid = cookie_sid(self.headers)
        sess = session_get(sid)
        if sess:
            audit("LOGOUT", sess["account"], self.client_ip())
        session_destroy(sid)
        return self.redirect("/admin/login", clear_cookie())

    # ------------------------------------------------------------ main dispatch
    def handle_one(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        CTX.session = None
        try:
            clean = path.rstrip("/")
            # Token-authenticated activation link: deliberately no Origin check
            # (it is opened from an external mail client) and no session needed.
            if clean == BASE + "/activate":
                return self.handle_activate(method, query)
            if clean == BASE + "/login":
                return self.handle_login(method)
            if clean == BASE + "/register":
                return self.handle_register(method)
            if clean == BASE + "/logout":
                return self.handle_logout()
            if not self.origin_ok():
                return self.send_body(403, "来源校验失败", "text/plain; charset=utf-8")

            sess = self.current_session()
            if not sess:
                if path in (BASE, BASE + "/") and method == "GET":
                    return self.redirect("/admin/login")
                if path.startswith(BASE + "/api/"):
                    return self.json_reply(401, {"ok": False,
                                                 "msg": "未登录或会话已过期，请重新登录"})
                return self.send_body(401, "请先登录", "text/plain; charset=utf-8")

            is_admin = sess["account"] == ADMIN_MAIL
            CTX.session = sess

            if path in (BASE, BASE + "/") and method == "GET":
                page = (PAGE.replace("__SESSION_ACCOUNT__", html.escape(sess["account"]))
                            .replace("__CSRF_TOKEN__", html.escape(sess["csrf"]))
                            .replace("__IS_ADMIN__", "true" if is_admin else "false"))
                return self.send_body(200, page)
            if not path.startswith(BASE + "/api/"):
                return self.send_body(404, "Not Found", "text/plain; charset=utf-8")

            name = path[len(BASE + "/api/"):]
            allowed = self.USER_GET_APIS if method == "GET" else self.USER_POST_APIS
            if not is_admin and name not in allowed:
                audit("DENY_SCOPE", sess["account"], self.client_ip(), name)
                return self.json_reply(403, {"ok": False, "msg": "无权访问该功能"})
            if not is_admin:
                # Back-end enforcement: never trust an "account" sent by the client.
                query["account"] = [sess["account"]]

            payload = {}
            if method == "POST":
                payload = self.read_form()
                if not is_admin:
                    payload["account"] = sess["account"]
                given = self.headers.get("X-CSRF-Token") or payload.get("csrf", "")
                if not hmac.compare_digest(str(given), sess["csrf"]):
                    return self.json_reply(403, {"ok": False,
                                                 "msg": "CSRF 校验失败，请刷新页面"})

            if name == "mail/identities" and not is_admin:
                return self.json_reply(200, {
                    "ok": True, "admin": ADMIN_MAIL,
                    "accounts": [{"email": sess["account"], "available": True,
                                  "is_admin": False}]})

            if method == "GET" and name == "mail/attachment":
                try:
                    blob, filename, ctype = api_mail_attachment(query)
                except Exception as exc:
                    return self.send_body(400, "下载失败: %s" % exc,
                                          "text/plain; charset=utf-8")
                ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)[:80] or "file.bin"
                disp = ("attachment; filename=\"%s\"; filename*=UTF-8''%s"
                        % (ascii_name, urllib.parse.quote(filename)))
                return self.send_body(200, blob, ctype, {"Content-Disposition": disp})

            try:
                if method == "GET":
                    get_routes = {"overview": api_overview, "accounts": api_accounts,
                                  "inbox": lambda: api_inbox(query),
                                  "message": lambda: api_message(query),
                                  "logs": lambda: api_logs(query), "check": api_check,
                                  "batch/status": lambda: api_batch_status(query),
                                  "mail/identities": lambda: api_mail_identities(query),
                                  "mail/folders": lambda: api_mail_folders(query),
                                  "mail/list": lambda: api_mail_list(query),
                                  "mail/read": lambda: api_mail_read(query)}
                    get_routes.update({"orgs": api_orgs})
                    result = get_routes[name]()

                else:
                    post_routes = {"account/add": api_account_add,
                                   "account/password": api_account_password,
                                   "account/remove": api_account_remove,
                                   "send": api_send,
                                   "batch/create": api_batch_create,
                                   "batch/create_custom": api_batch_create_custom,
                                   "batch/invite": api_batch_invite,
                                   "batch/activate": api_batch_activate,
                                   "batch/remove": api_batch_remove,
                                   "mail/action": api_mail_action,
                                   "mail/compose": api_mail_compose}
                    post_routes.update({"org/add": api_org_add, "org/rename": api_org_rename, "org/remove": api_org_remove, "account/org": api_account_org, "account/org_bulk": api_account_org_bulk})
                    result = post_routes[name](payload)

            except KeyError:
                return self.send_body(404, "Unknown API", "text/plain; charset=utf-8")
            except Exception as exc:
                result = {"ok": False, "msg": "执行异常: %s" % exc}
            return self.json_reply(200, result)
        finally:
            CTX.session = None


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((LISTEN, PORT), V5Handler)
    print("mailstack admin listening on %s:%d (domain %s)" % (LISTEN, PORT, DOMAIN),
          flush=True)
    srv.serve_forever()
