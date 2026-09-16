#!/usr/bin/env python3
"""VPS 多用户网页控制台：注册/登录/绑卡/个人中心/管理员面板。

依赖：仅 stdlib + sqlite3（VPS 无需外部 pip 包）。

用法:
  python3 app.py                        # 默认 0.0.0.0:8080
  python3 app.py --port 8000 --host 127.0.0.1
  python3 app.py --db /var/lib/webapp/db.sqlite
"""

import argparse
import hashlib
import http.server
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(BASE_DIR, "..")
USERS_CLOUD = os.path.join(REPO_DIR, "users.cloud.json")
DEFAULT_DB = os.path.join(BASE_DIR, "webapp.db")
DEFAULT_PASSCODE = "wlsdezx"
PASSCODE_SALT = "dlorder-salt-2026"
SESSION_EXPIRE_HOURS = 168  # 7 days
BIND_CODE_EXPIRE_SECONDS = 300  # 5 minutes
SMS_RATE_LIMIT_PER_IP = 5  # per hour
SMS_RATE_LIMIT_PER_MOBILE = 3  # per hour
CSRF_COOKIE = "dl_csrf"
SESSION_COOKIE = "dl_session"

BJT = timezone(timedelta(hours=8))

# ---------- password & token ----------

def pbkdf2_hash(password: str, salt: str = PASSCODE_SALT) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()


def verify_password(password: str, stored_hash: str) -> bool:
    return secrets.compare_digest(pbkdf2_hash(password), stored_hash)


def gen_token() -> str:
    return secrets.token_hex(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- database ----------

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pass_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            realname TEXT,
            percode TEXT,
            mobile TEXT,
            area1 TEXT DEFAULT '温岭第二中学',
            area2 TEXT DEFAULT '食堂',
            use_history INTEGER DEFAULT 0,
            min_score REAL DEFAULT 0,
            grab_on_weekday INTEGER DEFAULT 5,
            require_both_meals_weekday TEXT DEFAULT '[6]',
            cancelled TEXT DEFAULT '[]',
            scores TEXT DEFAULT '{}',
            blacklist TEXT DEFAULT '[]',
            negative_scores TEXT DEFAULT '{}',
            negative_exceptions TEXT DEFAULT '[]',
            prefer_overrides TEXT DEFAULT '{}',
            force_pick TEXT DEFAULT '[]',
            price_score TEXT DEFAULT '{}',
            max_price TEXT,
            prefer_cheaper INTEGER DEFAULT 1,
            order_mode TEXT DEFAULT '1',
            meals TEXT DEFAULT '["午餐","晚餐"]',
            uuid_env TEXT,
            token_env TEXT,
            bound INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            uid INTEGER NOT NULL,
            csrf_token TEXT,
            created_at TEXT,
            expires_at TEXT,
            FOREIGN KEY(uid) REFERENCES users(uid)
        );
        CREATE TABLE IF NOT EXISTS bind_flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL,
            mobile TEXT,
            uuid TEXT,
            token TEXT,
            open_uuid TEXT,
            user_id TEXT,
            user_salt TEXT,
            phase TEXT DEFAULT 'sms_sent',
            created_at TEXT,
            expires_at TEXT,
            attempts INTEGER DEFAULT 0,
            ip TEXT,
            FOREIGN KEY(uid) REFERENCES users(uid)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER,
            action TEXT,
            detail TEXT,
            ip TEXT,
            created_at TEXT
        );
    """)
    # default passcode
    cur = conn.execute("SELECT value FROM settings WHERE key='passcode'")
    if not cur.fetchone():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('passcode',?)",
                     (pbkdf2_hash(DEFAULT_PASSCODE),))
        conn.commit()
    return conn


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    conn.commit()


# ---------- auth helpers ----------

def get_current_user(conn: sqlite3.Connection, handler) -> dict:
    cookie = SimpleCookie()
    cookie.load(handler.headers.get("Cookie", ""))
    session_token = cookie.get(SESSION_COOKIE)
    if not session_token:
        return None
    token_h = hash_token(session_token.value)
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute("""
        SELECT u.uid, u.username, u.role, u.realname, u.percode, u.mobile,
               u.bound, u.area1, u.area2, u.use_history, u.min_score,
               u.grab_on_weekday, u.require_both_meals_weekday, u.cancelled,
               u.scores, u.blacklist, u.negative_scores, u.negative_exceptions,
               u.prefer_overrides, u.force_pick, u.price_score, u.max_price,
               u.prefer_cheaper, u.order_mode, u.meals, u.uuid_env, u.token_env
        FROM sessions s JOIN users u ON s.uid=u.uid
        WHERE s.token_hash=? AND s.expires_at>?
    """, (token_h, now))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    # parse JSON fields
    for field in ("scores", "blacklist", "negative_scores", "negative_exceptions",
                  "prefer_overrides", "force_pick", "price_score", "meals",
                  "require_both_meals_weekday", "cancelled"):
        try:
            d[field] = json.loads(d[field]) if d[field] else ([] if "list" in field else {})
        except (json.JSONDecodeError, TypeError):
            d[field] = [] if field in ("blacklist", "negative_exceptions", "force_pick",
                                       "meals", "require_both_meals_weekday", "cancelled") else {}
    d["min_score"] = float(d["min_score"]) if d["min_score"] is not None else 0
    d["prefer_cheaper"] = bool(d["prefer_cheaper"])
    return d


def generate_csrf_token() -> str:
    return secrets.token_hex(16)


# ---------- email-like JSON response ----------

def json_response(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False, indent=2).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler, filepath):
    if not os.path.exists(filepath):
        handler.send_error(404)
        return
    ext = os.path.splitext(filepath)[1]
    content_types = {".html": "text/html", ".css": "text/css",
                     ".js": "application/javascript", ".json": "application/json"}
    ct = content_types.get(ext, "application/octet-stream")
    with open(filepath, "rb") as f:
        body = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", f"{ct}; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ---------- SMS rate limiting ----------

def sms_rate_ok(conn: sqlite3.Connection, ip: str, mobile: str) -> bool:
    hour_ago = (datetime.now(BJT) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "SELECT COUNT(*) as cnt FROM bind_flows WHERE ip=? AND created_at>?",
        (ip, hour_ago))
    ip_count = cur.fetchone()["cnt"]
    cur = conn.execute(
        "SELECT COUNT(*) as cnt FROM bind_flows WHERE mobile=? AND created_at>?",
        (mobile, hour_ago))
    mob_count = cur.fetchone()["cnt"]
    return ip_count < SMS_RATE_LIMIT_PER_IP and mob_count < SMS_RATE_LIMIT_PER_MOBILE


def audit(conn: sqlite3.Connection, uid, action: str, detail: str, ip: str = ""):
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO audit_log(uid,action,detail,ip,created_at) VALUES(?,?,?,?,?)",
        (uid, action, detail, ip, now))
    conn.commit()


# ---------- git push helper ----------

def git_commit_and_push(repo_dir: str, commit_msg: str) -> dict:
    try:
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "webpanel"
        env["GIT_AUTHOR_EMAIL"] = "webpanel@dlorder.local"
        env["GIT_COMMITTER_NAME"] = "webpanel"
        env["GIT_COMMITTER_EMAIL"] = "webpanel@dlorder.local"

        def run(cmd):
            return subprocess.run(
                cmd, cwd=repo_dir, capture_output=True, text=True, env=env, timeout=30)

        run(["git", "add", "-A"])
        diff = run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            return {"ok": True, "msg": "无变更，跳过提交"}

        run(["git", "commit", "-m", commit_msg])
        for i in range(3):
            p = run(["git", "push"])
            if p.returncode == 0:
                return {"ok": True, "msg": "推送成功"}
            time.sleep(5)
        return {"ok": False, "msg": f"推送失败: {p.stderr[:200]}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


# ---------- auto-login integration (SMS flow) ----------

def send_binding_sms(cfg: dict, percode: str, realname: str, mobile: str) -> dict:
    """Phase 1: 生成新 uuid, 发送验证码到 mobile, 返回 session 信息。"""
    sys.path.insert(0, REPO_DIR)
    from auto_login import gen_uuid, api, DEFAULT_URI, save_state

    uuid = gen_uuid()
    r1 = api(cfg, uuid, "app.portal.getOAuthUrl",
             {"redirect_uri": DEFAULT_URI, "t_uri": "", "uuid_serial": ""})
    m = re.search(r"[?&]mh_code=([0-9a-f]+)", (r1.get("data") or {}).get("url") or "")
    if not m:
        return {"ok": False, "error": "getOAuthUrl 未返回 mh_code"}

    r2 = api(cfg, uuid, "app.portal.getOAuthUuid",
             {"mh_code": m.group(1), "uuid_serial": ""})
    d = r2.get("data") or {}
    token = d.get("mh901_access_token_h5")
    if not token:
        return {"ok": False, "error": "getOAuthUuid 未返回 token"}

    open_uuid = (d.get("user") or {}).get("open_uuid", "")
    r3 = api(cfg, uuid, "app.portal.setMemberRegiest", {
        "percode": percode, "password": "", "mh_tag": "",
        "realname": realname, "mobile": mobile, "verify_mob_code": "",
    }, token=token)
    reg = r3.get("data") or {}

    if reg.get("is_mobile_verify"):
        api(cfg, uuid, "app.common.sendSms", {
            "mobile": mobile, "user_id": reg.get("user_id"),
            "user_salt": reg.get("user_salt"), "is_user_mobile_check": 1,
        }, token=token)
        return {"ok": True, "uuid": uuid, "token": token, "open_uuid": open_uuid,
                "user_id": reg.get("user_id"), "user_salt": reg.get("user_salt")}
    else:
        return {"ok": False, "error": reg.get("msg", "未要求手机验证")}


def complete_binding_sms(cfg: dict, mobile: str, code: str, uuid: str, token: str,
                         user_id: str, user_salt: str) -> dict:
    """Phase 2: 用验证码完成绑定, 返回最终 uuid/token。"""
    sys.path.insert(0, REPO_DIR)
    from auto_login import api

    r4 = api(cfg, uuid, "app.portal.setMemberMobileCheck", {
        "mobile": mobile, "code": code,
        "user_id": user_id, "user_salt": user_salt,
    }, token=token)
    if (r4.get("code") or "") not in ("200", "SUC0000"):
        return {"ok": False, "error": f"验证码校验失败: {r4.get('msg', '')}"}

    check = api(cfg, uuid, "app.portal.getMember", token=token)
    if (check.get("code") or "") != "200":
        return {"ok": False, "error": f"绑定失败: {check.get('msg', '')}"}

    return {"ok": True, "uuid": uuid, "token": token}


# ---------- main handler ----------

class DlorderHandler(http.server.BaseHTTPRequestHandler):

    db: sqlite3.Connection = None
    repo_dir: str = REPO_DIR

    def log_message(self, format, *args):
        pass  # silence default logging

    def _parse_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        ct = self.headers.get("Content-Type", "")
        if "json" in ct:
            return json.loads(body)
        if "form" in ct:
            return {k: v[0] if len(v) == 1 else v
                    for k, v in parse_qs(body.decode()).items()}
        return {}

    def _client_ip(self) -> str:
        return self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0]

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # static: index.html
        if path == "/" or path == "":
            return html_response(self, os.path.join(BASE_DIR, "web", "index.html"))

        # api routes
        if path == "/api/ping":
            return json_response(self, {"ok": True, "time": datetime.now(BJT).isoformat()})
        if path == "/api/me":
            return self._api_me()
        if path == "/api/prefs":
            return self._api_get_prefs()
        if path == "/api/admin/users":
            return self._api_admin_users()
        if path == "/api/admin/audit":
            return self._api_admin_audit()

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        body = self._parse_body()
        ip = self._client_ip()

        if path == "/api/register":
            return self._api_register(body, ip)
        if path == "/api/login":
            return self._api_login(body, ip)
        if path == "/api/logout":
            return self._api_logout()
        if path == "/api/bind/start":
            return self._api_bind_start(body, ip)
        if path == "/api/bind/submit":
            return self._api_bind_submit(body, ip)
        if path == "/api/prefs":
            return self._api_set_prefs(body, ip)
        if path == "/api/apply":
            return self._api_apply(body, ip)
        if path == "/api/admin/ban":
            return self._api_admin_ban(body, ip)
        if path == "/api/admin/unban":
            return self._api_admin_unban(body, ip)
        if path == "/api/admin/reset-password":
            return self._api_admin_reset_password(body, ip)
        if path == "/api/admin/set-passcode":
            return self._api_admin_set_passcode(body, ip)

        self.send_error(404)

    # ---- auth routes ----

    def _api_register(self, body, ip):
        username = (body.get("username") or "").strip()
        password = (body.get("password") or "").strip()
        passcode = (body.get("passcode") or "").strip()
        if not username or not password or not passcode:
            return json_response(self, {"ok": False, "error": "缺少字段"}, 400)
        if len(username) < 2 or len(username) > 20:
            return json_response(self, {"ok": False, "error": "用户名 2-20 位"}, 400)
        if len(password) < 4:
            return json_response(self, {"ok": False, "error": "密码至少 4 位"}, 400)

        db = self.db
        stored = get_setting(db, "passcode")
        if not stored or not verify_password(passcode, stored):
            audit(db, None, "register_fail", f"口令错误 user={username}", ip)
            return json_response(self, {"ok": False, "error": "口令错误"}, 403)

        try:
            now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "INSERT INTO users(username,pass_hash,role,created_at,updated_at) VALUES(?,?,?,?,?)",
                (username, pbkdf2_hash(password), "admin" if db.execute(
                    "SELECT COUNT(*) as c FROM users").fetchone()["c"] == 0 else "user", now, now))
            db.commit()
            uid = db.execute("SELECT uid FROM users WHERE username=?", (username,)).fetchone()["uid"]
            audit(db, uid, "register", f"新用户注册", ip)
            return json_response(self, {"ok": True, "uid": uid})
        except sqlite3.IntegrityError:
            return json_response(self, {"ok": False, "error": "用户名已存在"}, 409)

    def _api_login(self, body, ip):
        username = (body.get("username") or "").strip()
        password = (body.get("password") or "").strip()
        db = self.db
        cur = db.execute("SELECT uid,pass_hash,role,bound FROM users WHERE username=?", (username,))
        user = cur.fetchone()
        if not user or not verify_password(password, user["pass_hash"]):
            audit(db, user["uid"] if user else None, "login_fail", f"user={username}", ip)
            return json_response(self, {"ok": False, "error": "用户名或密码错误"}, 401)

        token = gen_token()
        token_h = hash_token(token)
        now = datetime.now(BJT)
        expires = (now + timedelta(hours=SESSION_EXPIRE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        csrf = generate_csrf_token()
        db.execute("INSERT INTO sessions(token_hash,uid,csrf_token,created_at,expires_at) VALUES(?,?,?,?,?)",
                   (token_h, user["uid"], csrf, now.strftime("%Y-%m-%d %H:%M:%S"), expires))
        db.commit()
        audit(db, user["uid"], "login", "", ip)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        sc = SimpleCookie()
        sc[SESSION_COOKIE] = token
        sc[SESSION_COOKIE]["path"] = "/"
        sc[SESSION_COOKIE]["httponly"] = True
        sc[SESSION_COOKIE]["samesite"] = "Lax"
        sc[SESSION_COOKIE]["max-age"] = SESSION_EXPIRE_HOURS * 3600
        self.send_header("Set-Cookie", sc[SESSION_COOKIE].OutputString())
        sc2 = SimpleCookie()
        sc2[CSRF_COOKIE] = csrf
        sc2[CSRF_COOKIE]["path"] = "/"
        sc2[CSRF_COOKIE]["samesite"] = "Lax"
        sc2[CSRF_COOKIE]["max-age"] = SESSION_EXPIRE_HOURS * 3600
        self.send_header("Set-Cookie", sc2[CSRF_COOKIE].OutputString())
        body = json.dumps({"ok": True, "role": user["role"], "bound": bool(user["bound"])},
                          ensure_ascii=False).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_logout(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        st = cookie.get(SESSION_COOKIE)
        if st:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(st.value),))
            self.db.commit()
        self.send_response(204)
        self.end_headers()

    def _api_me(self):
        user = get_current_user(self.db, self)
        if not user:
            return json_response(self, {"ok": False, "error": "未登录"}, 401)
        return json_response(self, {"ok": True, "user": {
            "username": user["username"], "role": user["role"],
            "realname": user["realname"], "percode": user["percode"],
            "mobile": user["mobile"], "bound": bool(user["bound"]),
            "area1": user["area1"], "area2": user["area2"],
        }})

    # ---- bind routes ----

    def _api_bind_start(self, body, ip):
        user = get_current_user(self.db, self)
        if not user:
            return json_response(self, {"ok": False, "error": "未登录"}, 401)
        realname = (body.get("realname") or "").strip()
        percode = (body.get("percode") or "").strip()
        mobile = (body.get("mobile") or "").strip()
        if not realname or not percode or not mobile:
            return json_response(self, {"ok": False, "error": "姓名/饭卡号/手机号必填"}, 400)

        db = self.db
        if not sms_rate_ok(db, ip, mobile):
            audit(db, user["uid"], "bind_rate_limit", f"ip={ip} mobile={mobile}", ip)
            return json_response(self, {"ok": False, "error": "验证码发送过于频繁，请稍后再试"}, 429)

        # 清理旧 flow
        db.execute("DELETE FROM bind_flows WHERE uid=? AND phase='sms_sent'", (user["uid"],))
        db.commit()

        cfg = {"mer_id": "61266001", "mer_salt": "pq1z400jlg",
               "base_url": "https://www.epay100.cn/901/6126/server/index.php",
               "uni_id": "6126", "attach": "5.6.2607315"}

        result = send_binding_sms(cfg, percode, realname, mobile)
        if not result.get("ok"):
            audit(db, user["uid"], "bind_sms_fail", result.get("error", ""), ip)
            return json_response(self, {"ok": False, "error": result.get("error", "发送失败")}, 400)

        now = datetime.now(BJT)
        expires = (now + timedelta(seconds=BIND_CODE_EXPIRE_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO bind_flows(uid,mobile,uuid,token,open_uuid,user_id,user_salt,phase,created_at,expires_at,ip) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user["uid"], mobile, result["uuid"], result["token"], result["open_uuid"],
             result.get("user_id"), result.get("user_salt"), "sms_sent",
             now.strftime("%Y-%m-%d %H:%M:%S"), expires, ip))
        db.commit()
        audit(db, user["uid"], "bind_sms_sent", f"mobile={mobile}", ip)
        return json_response(self, {"ok": True, "msg": "验证码已发送"})

    def _api_bind_submit(self, body, ip):
        user = get_current_user(self.db, self)
        if not user:
            return json_response(self, {"ok": False, "error": "未登录"}, 401)
        code = (body.get("code") or "").strip()
        if not code or not code.isdigit() or len(code) != 4:
            return json_response(self, {"ok": False, "error": "请输入 4 位验证码"}, 400)

        db = self.db
        now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            "SELECT * FROM bind_flows WHERE uid=? AND phase='sms_sent' AND expires_at>? ORDER BY id DESC LIMIT 1",
            (user["uid"], now_str))
        flow = cur.fetchone()
        if not flow:
            return json_response(self, {"ok": False, "error": "验证码已过期，请重新发送"}, 410)

        if flow["attempts"] >= 5:
            db.execute("DELETE FROM bind_flows WHERE id=?", (flow["id"],))
            db.commit()
            return json_response(self, {"ok": False, "error": "尝试次数过多，请重新发送"}, 429)

        db.execute("UPDATE bind_flows SET attempts=attempts+1 WHERE id=?", (flow["id"],))
        db.commit()

        cfg = {"mer_id": "61266001", "mer_salt": "pq1z400jlg",
               "base_url": "https://www.epay100.cn/901/6126/server/index.php",
               "uni_id": "6126", "attach": "5.6.2607315"}

        result = complete_binding_sms(
            cfg, flow["mobile"], code, flow["uuid"], flow["token"],
            flow["user_id"], flow["user_salt"])

        if not result.get("ok"):
            audit(db, user["uid"], "bind_code_fail", result.get("error", ""), ip)
            return json_response(self, {"ok": False, "error": result.get("error", "绑定失败")}, 400)

        # 成功：更新 users 表
        now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        uuid_env = f"CLOUD_UUID_{user['username'].upper()}"
        token_env = f"CLOUD_TOKEN_{user['username'].upper()}"
        db.execute("""
            UPDATE users SET bound=1, realname=?, percode=?, mobile=?,
                   uuid_env=?, token_env=?, updated_at=?
            WHERE uid=?
        """, (user.get("realname") or "", user.get("percode") or "", flow["mobile"],
              uuid_env, token_env, now, user["uid"]))
        db.execute("DELETE FROM bind_flows WHERE uid=?", (user["uid"],))
        db.commit()
        audit(db, user["uid"], "bind_success", f"mobile={flow['mobile']}", ip)

        return json_response(self, {"ok": True, "msg": "绑定成功",
                                    "uuid_env": uuid_env, "token_env": token_env})

    # ---- prefs routes ----

    def _api_get_prefs(self):
        user = get_current_user(self.db, self)
        if not user:
            return json_response(self, {"ok": False, "error": "未登录"}, 401)
        return json_response(self, {"ok": True, "prefs": {
            "scores": user["scores"], "blacklist": user["blacklist"],
            "negative_scores": user["negative_scores"],
            "negative_exceptions": user["negative_exceptions"],
            "prefer_overrides": user["prefer_overrides"],
            "force_pick": user["force_pick"], "price_score": user["price_score"],
            "max_price": user["max_price"], "prefer_cheaper": user["prefer_cheaper"],
            "min_score": user["min_score"], "use_history": bool(user["use_history"]),
            "grab_on_weekday": user["grab_on_weekday"],
            "require_both_meals_weekday": user["require_both_meals_weekday"],
            "cancelled": user["cancelled"], "order_mode": user["order_mode"],
            "meals": user["meals"],
        }})

    def _api_set_prefs(self, body, ip):
        user = get_current_user(self.db, self)
        if not user:
            return json_response(self, {"ok": False, "error": "未登录"}, 401)
        db = self.db
        allowed = ("scores", "blacklist", "negative_scores", "negative_exceptions",
                   "prefer_overrides", "force_pick", "price_score",
                   "max_price", "prefer_cheaper", "min_score",
                   "grab_on_weekday", "require_both_meals_weekday", "cancelled",
                   "order_mode", "meals")
        updates = {}
        for k in allowed:
            if k in body:
                v = body[k]
                if isinstance(v, (dict, list)):
                    updates[k] = json.dumps(v, ensure_ascii=False)
                else:
                    updates[k] = v
        if not updates:
            return json_response(self, {"ok": False, "error": "无修改"}, 400)

        updates["updated_at"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [user["uid"]]
        db.execute(f"UPDATE users SET {set_clause} WHERE uid=?", vals)
        db.commit()
        audit(db, user["uid"], "prefs_update", json.dumps(list(updates.keys())), ip)
        return json_response(self, {"ok": True, "updated": list(updates.keys())})

    # ---- apply (git push) ----

    def _api_apply(self, body, ip):
        user = get_current_user(self.db, self)
        if not user:
            return json_response(self, {"ok": False, "error": "未登录"}, 401)
        if user["role"] != "admin":
            return json_response(self, {"ok": False, "error": "仅管理员可推送"}, 403)

        # 生成 users.cloud.json
        result = self._generate_users_cloud()
        if not result.get("ok"):
            return json_response(self, {"ok": False, "error": result.get("error")}, 500)

        push = git_commit_and_push(self.repo_dir, f"webpanel: {user['username']} 更新配置 @{datetime.now(BJT).strftime('%Y-%m-%d %H:%M')}")
        audit(user["uid"], "apply_push", push.get("msg", ""), ip)
        return json_response(self, push)

    def _generate_users_cloud(self) -> dict:
        """从 DB 读取所有用户, 写入 users.cloud.json。"""
        db = self.db
        try:
            cur = db.execute("""
                SELECT username, realname, percode, mobile, area1, area2,
                       use_history, min_score, grab_on_weekday, require_both_meals_weekday,
                       cancelled, scores, blacklist, negative_scores, negative_exceptions,
                       prefer_overrides, force_pick, price_score, max_price, prefer_cheaper,
                       order_mode, meals, uuid_env, token_env, bound
                FROM users WHERE bound=1
            """)
            users = []
            for row in cur:
                d = dict(row)
                users.append({
                    "name": d["username"],
                    "mer_id": "61266001", "mer_salt": "pq1z400jlg",
                    "uuid_env": d["uuid_env"], "token_env": d["token_env"],
                    "percode": d["percode"], "realname": d["realname"],
                    "mobile": d["mobile"],
                    "use_history": bool(d["use_history"]),
                    "scores": json.loads(d["scores"] or "{}"),
                    "blacklist": json.loads(d["blacklist"] or "[]"),
                    "negative_scores": json.loads(d["negative_scores"] or "{}"),
                    "negative_exceptions": json.loads(d["negative_exceptions"] or "[]"),
                    "prefer_overrides": json.loads(d["prefer_overrides"] or "{}"),
                    "force_pick": json.loads(d["force_pick"] or "[]"),
                    "price_score": json.loads(d["price_score"] or "{}"),
                    "max_price": d["max_price"],
                    "prefer_cheaper": bool(d["prefer_cheaper"]),
                    "order_rules": {
                        "require_both_meals_weekday": json.loads(d["require_both_meals_weekday"] or "[6]"),
                        "grab_on_weekday": d["grab_on_weekday"],
                        "cancelled": json.loads(d["cancelled"] or "[]"),
                    },
                    "delivery": {
                        "name": d["realname"], "mobile": d["mobile"],
                        "area1": d["area1"], "area2": d["area2"],
                        "date": "", "time": "",
                    },
                    "order_mode": d["order_mode"],
                    "min_score": float(d["min_score"]),
                    "meals": json.loads(d["meals"] or '["午餐","晚餐"]'),
                })
            with open(USERS_CLOUD, "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=2)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ---- admin routes ----

    def _api_admin_users(self):
        user = get_current_user(self.db, self)
        if not user or user["role"] != "admin":
            return json_response(self, {"ok": False, "error": "无权限"}, 403)
        cur = self.db.execute("""
            SELECT uid, username, role, realname, percode, mobile, bound, created_at
            FROM users ORDER BY uid
        """)
        users = [dict(r) for r in cur]
        return json_response(self, {"ok": True, "users": users})

    def _api_admin_audit(self):
        user = get_current_user(self.db, self)
        if not user or user["role"] != "admin":
            return json_response(self, {"ok": False, "error": "无权限"}, 403)
        cur = self.db.execute("""
            SELECT a.id, a.uid, u.username, a.action, a.detail, a.ip, a.created_at
            FROM audit_log a LEFT JOIN users u ON a.uid=u.uid
            ORDER BY a.id DESC LIMIT 100
        """)
        return json_response(self, {"ok": True, "logs": [dict(r) for r in cur]})

    def _api_admin_ban(self, body, ip):
        user = get_current_user(self.db, self)
        if not user or user["role"] != "admin":
            return json_response(self, {"ok": False, "error": "无权限"}, 403)
        target_uid = body.get("uid")
        if not target_uid:
            return json_response(self, {"ok": False, "error": "缺少 uid"}, 400)
        self.db.execute("UPDATE users SET role='banned' WHERE uid=?", (target_uid,))
        self.db.commit()
        audit(self.db, user["uid"], "ban_user", f"target={target_uid}", ip)
        return json_response(self, {"ok": True})

    def _api_admin_unban(self, body, ip):
        user = get_current_user(self.db, self)
        if not user or user["role"] != "admin":
            return json_response(self, {"ok": False, "error": "无权限"}, 403)
        target_uid = body.get("uid")
        self.db.execute("UPDATE users SET role='user' WHERE uid=?", (target_uid,))
        self.db.commit()
        audit(self.db, user["uid"], "unban_user", f"target={target_uid}", ip)
        return json_response(self, {"ok": True})

    def _api_admin_reset_password(self, body, ip):
        user = get_current_user(self.db, self)
        if not user or user["role"] != "admin":
            return json_response(self, {"ok": False, "error": "无权限"}, 403)
        target_uid = body.get("uid")
        new_pw = (body.get("password") or "").strip()
        if not target_uid or not new_pw or len(new_pw) < 4:
            return json_response(self, {"ok": False, "error": "uid + 密码(≥4位) 必填"}, 400)
        self.db.execute("UPDATE users SET pass_hash=?, updated_at=? WHERE uid=?",
                        (pbkdf2_hash(new_pw), datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S"), target_uid))
        self.db.commit()
        audit(self.db, user["uid"], "reset_password", f"target={target_uid}", ip)
        return json_response(self, {"ok": True})

    def _api_admin_set_passcode(self, body, ip):
        user = get_current_user(self.db, self)
        if not user or user["role"] != "admin":
            return json_response(self, {"ok": False, "error": "无权限"}, 403)
        new_pc = (body.get("passcode") or "").strip()
        if not new_pc or len(new_pc) < 4:
            return json_response(self, {"ok": False, "error": "口令至少 4 位"}, 400)
        set_setting(self.db, "passcode", pbkdf2_hash(new_pc))
        audit(self.db, user["uid"], "set_passcode", "", ip)
        return json_response(self, {"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--repo", default=REPO_DIR)
    args = ap.parse_args()

    conn = init_db(args.db)
    DlorderHandler.db = conn
    DlorderHandler.repo_dir = args.repo

    server = http.server.ThreadingHTTPServer((args.host, args.port), DlorderHandler)
    print(f"webpanel 启动: http://{args.host}:{args.port}")
    print(f"  数据库: {args.db}")
    print(f"  仓库:   {args.repo}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()
