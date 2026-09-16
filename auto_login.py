#!/usr/bin/env python3
"""H5 自动登录/刷新：换新 client_uuid + token，绑定成员身份（短信验证码）。

用法:
  python3 auto_login.py                          # 完整流程，交互输入短信验证码
  python3 auto_login.py --code 1234              # 直接给验证码（免交互）
  python3 auto_login.py --dry-run                # 只演示，不写 config
  python3 auto_login.py --gh                     # 成功后同步 GitHub Secrets

只允许给尚未绑定成员的 client_uuid 使用（每次都会走短信）。已有 token 未失效时不要重复跑：
同一 client_uuid 再签发会顶掉旧 token。
"""

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE_DIR, "config.json")
STATE = os.path.join(BASE_DIR, ".login_state.json")
DEFAULT_URI = "https://www.epay100.cn/901/6126/"


def gen_uuid():
    h = hashlib.md5(("%d-%d" % (time.time() * 1000, random.random())).encode()).hexdigest()
    return "finger-{}-{}{}".format(
        h,
        time.strftime("%y%m%d"),
        "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(10)),
    )


def workspace_suffix(cfg_user):
    match = re.search(r"uni_id\s*[:=]\s*['\"]?(\d+)", "0")
    return ""


SUFFIXES = ["", "-2", "-3", "-4"]


def read_creds(path):
    d = json.load(open(path))
    if isinstance(d, dict):
        return [d]
    out = []
    for u in d:
        c = json.load(open(u.get("config_path", CONFIG))) if u.get("config_path") else None
        out.append((u, c))
    return out


def api(cfg, client_uuid, method, params=None, token=None):
    ts = int(time.time())
    r8 = "%08d" % random.randint(0, 10 ** 8)
    unif = str(cfg.get("uni_id", "6126"))
    url = "https://www.epay100.cn/901/{}/server/index.php?mh901_method={}&mh901_client_uuid={}&mh901_rand={}-{}".format(
        unif, method, client_uuid, ts, r8
    )
    body = {"method": method, "uni_id": unif, "client_uuid": client_uuid, "attach": "5.6.2607315"}
    if params:
        for k, v in params.items():
            body["data[%s]" % k] = v
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Mobile",
    }
    if token:
        headers["Authorization"] = token
    r = requests.post(url, data=body, headers=headers, timeout=25)
    return r.json()


def save_state(uuid, token, open_uuid, user_id, user_salt):
    json.dump({"client_uuid": uuid, "access_token": token, "open_uuid": open_uuid,
               "user_id": user_id, "user_salt": user_salt},
              open(STATE, "w"), ensure_ascii=False, indent=2)


def login_flow(cfg, percode, realname, mobile, code=None, dry_run=False):
    uuid = gen_uuid()
    r1 = api(cfg, uuid, "app.portal.getOAuthUrl",
             {"redirect_uri": DEFAULT_URI, "t_uri": "", "uuid_serial": ""})
    url = (r1.get("data") or {}).get("url") or ""
    m = re.search(r"[?&]mh_code=([0-9a-f]+)", url)
    if not m:
        raise RuntimeError("getOAuthUrl 未返回 mh_code: %s" % json.dumps(r1, ensure_ascii=False)[:200])
    r2 = api(cfg, uuid, "app.portal.getOAuthUuid", {"mh_code": m.group(1), "uuid_serial": ""})
    d = r2.get("data") or {}
    token = d.get("mh901_access_token_h5")
    if not token:
        raise RuntimeError("getOAuthUuid 未返回 token: %s" % json.dumps(r2, ensure_ascii=False)[:200])
    open_uuid = (d.get("user") or {}).get("open_uuid", "")
    r3 = api(cfg, uuid, "app.portal.setMemberRegiest", {
        "percode": percode, "password": "", "mh_tag": "",
        "realname": realname, "mobile": mobile, "verify_mob_code": "",
    }, token=token)
    reg = r3.get("data") or {}
    if reg.get("is_mobile_verify") and not code:
        api(cfg, uuid, "app.common.sendSms", {
            "mobile": mobile, "user_id": reg.get("user_id"),
            "user_salt": reg.get("user_salt"), "is_user_mobile_check": 1,
        }, token=token)
        save_state(uuid, token, open_uuid, reg.get("user_id"), reg.get("user_salt"))
        print("短信已发往 %s；本次会话状态已保存，之后请运行: python3 auto_login.py --code <码> --gh" % mobile)
        return None
    if reg.get("is_mobile_verify"):
        r4 = api(cfg, uuid, "app.portal.setMemberMobileCheck", {
            "mobile": mobile, "code": code,
            "user_id": reg.get("user_id"), "user_salt": reg.get("user_salt"),
        }, token=token)
        if (r4.get("code") or "") not in ("200", "SUC0000"):
            raise RuntimeError("短信校验失败: %s" % json.dumps(r4, ensure_ascii=False)[:200])
    check = api(cfg, uuid, "app.portal.getMember", token=token)
    if (check.get("code") or "") != "200":
        raise RuntimeError("登录后 getMember 未通过: %s" % json.dumps(check, ensure_ascii=False)[:200])
    return {"client_uuid": uuid, "access_token": token, "open_uuid": open_uuid,
            "user_id": reg.get("user_id"), "user_salt": reg.get("user_salt")}


def resume_flow(cfg, mobile, code):
    """用已保存的会话状态 + 短信验证码完成登录（同一 uuid，不重发短信）。"""
    st = json.load(open(STATE))
    uuid = st["client_uuid"]
    token = st["access_token"]
    r4 = api(cfg, uuid, "app.portal.setMemberMobileCheck", {
        "mobile": mobile, "code": code,
        "user_id": st.get("user_id"), "user_salt": st.get("user_salt"),
    }, token=token)
    if (r4.get("code") or "") not in ("200", "SUC0000"):
        raise RuntimeError("短信校验失败: %s" % json.dumps(r4, ensure_ascii=False)[:200])
    check = api(cfg, uuid, "app.portal.getMember", token=token)
    if (check.get("code") or "") != "200":
        raise RuntimeError("登录后 getMember 未通过: %s" % json.dumps(check, ensure_ascii=False)[:200])
    return {"client_uuid": uuid, "access_token": token, "open_uuid": st.get("open_uuid", ""),
            "user_id": st.get("user_id"), "user_salt": st.get("user_salt")}


def save_config(res, dry_run=False):
    cfg = json.load(open(CONFIG))
    cfg["client_uuid"] = res["client_uuid"]
    cfg["access_token"] = res["access_token"]
    cfg["open_uuid"] = res.get("open_uuid", "")
    if dry_run:
        print("[DRY] 不写入 config.json")
        return cfg
    json.dump(cfg, open(CONFIG, "w"), ensure_ascii=False, indent=2)
    return cfg


def push_gh(res, uuid_env="CLOUD_UUID_SELF", token_env="CLOUD_TOKEN_SELF"):
    try:
        import subprocess
        for name, val in ((uuid_env, res["client_uuid"]),
                          (token_env, res["access_token"])):
            p = subprocess.run(["gh", "secret", "set", name, "--repo", "explore0-jpg/dlorder-cloud"],
                               input=val.encode(), capture_output=True)
            if p.returncode:
                raise RuntimeError("gh secret %s 失败: %s" % (name, p.stderr.decode()))
        print("[gh] Secrets 已同步:", uuid_env, "/", token_env)
    except Exception as e:
        print("[gh] 同步失败（可忽略并手动更新 Secrets）:", e)


def save_profile(res, args):
    """把用户资料写入 users.cloud.json 的用户节点（凭证不进 json，只进 Secrets）。"""
    if not args.user:
        save_config(res, dry_run=args.dry_run)
        return
    users_file = args.users_file or os.path.join(BASE_DIR, "users.cloud.json")
    with open(users_file, encoding="utf-8") as f:
        users = json.load(f)
    node = next((u for u in users if u.get("name") == args.user), None)
    if node is None:
        node = {"name": args.user, "mer_id": "61266001", "mer_salt": "pq1z400jlg",
                "uuid_env": "CLOUD_UUID_%s" % str(args.user).upper(),
                "token_env": "CLOUD_TOKEN_%s" % str(args.user).upper(),
                "scores": {}, "blacklist": [], "negative_scores": {},
                "negative_exceptions": [],
                "order_rules": {"require_both_meals_weekday": [6],
                                "grab_on_weekday": -1, "cancelled": []},
                "delivery": {"name": "", "mobile": "", "area1": "",
                             "area2": "", "date": "", "time": ""},
                "order_mode": "1", "min_score": 1, "meals": ["午餐", "晚餐"]}
        users.append(node)
    node["percode"] = args.percode
    node["realname"] = args.realname
    node["mobile"] = args.mobile
    node["delivery"] = dict(dict(node.get("delivery") or {}), **{
        "name": args.realname, "mobile": args.mobile,
        "area1": node["delivery"].get("area1") or "温岭第二中学",
        "area2": node["delivery"].get("area2") or "食堂"})
    json.dump(users, open(users_file, "w"), ensure_ascii=False, indent=2)
    print("[profile] 已写入 %s 的 %s 节点（凭证在 Secrets，未写入文件）" % (users_file, args.user))


def main():
    ap = argparse.ArgumentParser(prog="auto_login.py")
    ap.add_argument("--code", default=None, help="短信验证码（免交互）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gh", action="store_true", help="同步 GitHub Secrets")
    ap.add_argument("--forget-state", action="store_true", help="丢弃已保存的会话状态")
    ap.add_argument("--user", default=None, help="用户节点名（如 self/friend），写 users.cloud.json")
    ap.add_argument("--users-file", default=None, help="users.cloud.json 路径")
    ap.add_argument("--percode", default=None, help="饭卡号")
    ap.add_argument("--realname", default=None, help="姓名")
    ap.add_argument("--mobile", default=None, help="手机号")
    args = ap.parse_args()

    if args.forget_state:
        os.path.exists(STATE) and os.remove(STATE)
        print("会话状态已丢弃")
        return 0

    cfg = json.load(open(CONFIG))
    login = cfg.get("login") or {}
    uuid_env = "CLOUD_UUID_%s" % str(args.user).upper() if args.user else "CLOUD_UUID_SELF"
    token_env = "CLOUD_TOKEN_%s" % str(args.user).upper() if args.user else "CLOUD_TOKEN_SELF"
    percode = args.percode or cfg.get("percode") or login.get("percode")
    realname = args.realname or cfg.get("realname") or login.get("realname")
    mobile = args.mobile or cfg.get("mobile") or login.get("mobile")
    if not (percode and realname and mobile):
        print("缺少 login 凭证（--percode/--realname/--mobile 或 config.json）")
        return 1

    if args.code:
        if os.path.exists(STATE):
            print("发现已保存的会话状态，直接用该会话完成（不重发短信）...")
            res = resume_flow(cfg, mobile, args.code)
        else:
            res = login_flow(cfg, percode, realname, mobile, code=args.code)
    else:
        res = login_flow(cfg, percode, realname, mobile, code=None)
        if res is None:
            # 已完成发短信 + 状态保存（返回 None），等用户拿到码后 --code 完成
            return 0
    print("新凭证已就绪:")
    print("  client_uuid :", res["client_uuid"])
    print("  open_uuid   :", res["open_uuid"])
    print("  access_token (仅预览前段):", res["access_token"][:24] + "…")
    save_profile(res, args)
    if args.gh:
        push_gh(res, uuid_env, token_env)
    return 0


if __name__ == "__main__":
    sys.exit(main())