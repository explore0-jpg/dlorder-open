#!/usr/bin/env python3
"""批量绑卡 + 自动生成评分：读 CSV → 逐个绑卡 → 拉180天历史 → 自动评分 → 写入 users.cloud.json + Secrets。

用法:
  python3 batch_bind.py --csv 同学名单.csv
  python3 batch_bind.py --csv 同学名单.csv --days 180 --dry-run

CSV 格式（首行表头）:
  姓名,饭卡号,手机号
  金依萱,20241442,13858612873
  张三,20241500,13900001111

流程（每个同学）:
  1. 发送验证码到手机
  2. 管理员输入收到的 4 位码
  3. 完成绑定 → 写入 users.cloud.json + GitHub Secrets
  4. 拉取 180 天订单历史 → 自动生成评分
  5. 评分写回 users.cloud.json
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_CLOUD = os.path.join(BASE_DIR, "users.cloud.json")

from auto_login import (api as epay_api, gen_uuid, DEFAULT_URI,
                        save_state, resume_flow, save_profile, push_gh)
from history import fetch_history
from dishes import classify_dish, parse_goods_dishes
from epay import EpayAPI, AuthError, EpayError


# ---------- auto-score from history ----------

def auto_score_from_history(api, days=180):
    """拉取 N 天订单，按频次自动生成评分。返回 {dish: score}。"""
    infos, _ = fetch_history(api, days=days, progress=lambda m: None)
    cnt = Counter()
    for info in infos:
        for g in info.get("goods_list") or []:
            title = (g.get("goods_title") or "").strip()
            if not title:
                continue
            for d in set(parse_goods_dishes(title)):
                cnt[d] += 1

    scores = {}
    for dish, count in cnt.items():
        if count >= 5:
            scores[dish] = 6
        elif count >= 3:
            scores[dish] = 4
        elif count >= 1:
            scores[dish] = 2
    return scores, len(infos), len(cnt)


# ---------- CSV loading ----------

def load_csv(csv_path):
    """读取 CSV，返回 [{name, percode, mobile}, ...]。"""
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("姓名") or row.get("name") or "").strip()
            percode = (row.get("饭卡号") or row.get("percode") or row.get("卡号") or "").strip()
            mobile = (row.get("手机号") or row.get("mobile") or row.get("手机") or "").strip()
            if name and percode and mobile:
                rows.append({"name": name, "percode": percode, "mobile": mobile})
            else:
                print(f"  跳过无效行: {row}")
    return rows


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="批量绑卡 + 自动生成评分")
    ap.add_argument("--csv", required=True, help="同学名单 CSV 文件路径")
    ap.add_argument("--days", type=int, default=180, help="拉取历史天数（默认180）")
    ap.add_argument("--dry-run", action="store_true", help="只读 CSV 不绑卡")
    ap.add_argument("--skip-existing", action="store_true", default=True,
                    help="跳过已存在的用户（默认True）")
    ap.add_argument("--users-file", default=USERS_CLOUD)
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"CSV 文件不存在: {args.csv}")
        return 1

    rows = load_csv(args.csv)
    if not rows:
        print("CSV 中无有效数据")
        return 1

    print(f"读取到 {len(rows)} 位同学")
    for i, r in enumerate(rows):
        print(f"  {i+1}. {r['name']} | {r['percode']} | {r['mobile']}")

    if args.dry_run:
        print("\n[DRY-RUN] 不执行绑卡")
        return 0

    # load existing users
    existing = []
    if os.path.exists(args.users_file):
        with open(args.users_file, encoding="utf-8") as f:
            existing = json.load(f)
    existing_names = {u.get("name") for u in existing}

    cfg = {"mer_id": "61266001", "mer_salt": "pq1z400jlg",
           "base_url": "https://www.epay100.cn/901/6126/server/index.php",
           "uni_id": "6126", "attach": "5.6.2607315"}

    results = {"success": [], "skipped": [], "failed": []}

    for i, row in enumerate(rows):
        name = row["name"]
        percode = row["percode"]
        mobile = row["mobile"]
        username = "user_" + mobile[-4:]

        print(f"\n{'='*40}")
        print(f"[{i+1}/{len(rows)}] {name} ({percode} / {mobile})")

        # skip existing
        if args.skip_existing and name in existing_names:
            print(f"  → 已存在，跳过")
            results["skipped"].append(name)
            continue

        # --- phase 1: send SMS ---
        try:
            uuid = gen_uuid()
            r1 = epay_api(cfg, uuid, "app.portal.getOAuthUrl",
                          {"redirect_uri": DEFAULT_URI, "t_uri": "", "uuid_serial": ""})
            import re
            m = re.search(r"[?&]mh_code=([0-9a-f]+)", (r1.get("data") or {}).get("url") or "")
            if not m:
                print(f"  ✗ getOAuthUrl 失败")
                results["failed"].append(name)
                continue

            r2 = epay_api(cfg, uuid, "app.portal.getOAuthUuid",
                          {"mh_code": m.group(1), "uuid_serial": ""})
            d = r2.get("data") or {}
            token = d.get("mh901_access_token_h5")
            if not token:
                print(f"  ✗ getOAuthUuid 失败")
                results["failed"].append(name)
                continue

            r3 = epay_api(cfg, uuid, "app.portal.setMemberRegiest", {
                "percode": percode, "password": "", "mh_tag": "",
                "realname": name, "mobile": mobile, "verify_mob_code": "",
            }, token=token)
            reg = r3.get("data") or {}

            if not reg.get("is_mobile_verify"):
                msg = reg.get("msg", "未要求手机验证")
                print(f"  ✗ 注册失败: {msg}")
                results["failed"].append(name)
                continue

            epay_api(cfg, uuid, "app.common.sendSms", {
                "mobile": mobile, "user_id": reg.get("user_id"),
                "user_salt": reg.get("user_salt"), "is_user_mobile_check": 1,
            }, token=token)
            save_state(uuid, token, d.get("user", {}).get("open_uuid", ""),
                       reg.get("user_id"), reg.get("user_salt"))
            print(f"  验证码已发到 {mobile}")

            # --- phase 2: wait for code ---
            code = input(f"  请输入 {name} 的验证码: ").strip()
            if not code or len(code) != 4:
                print(f"  ✗ 验证码无效，跳过")
                results["failed"].append(name)
                continue

            res = resume_flow(cfg, mobile, code)
            print(f"  ✓ 绑定成功: uuid={res['client_uuid'][:20]}...")

            # --- phase 3: save profile + push secrets ---
            uuid_env = f"CLOUD_UUID_{username.upper()}"
            token_env = f"CLOUD_TOKEN_{username.upper()}"

            # build minimal args for save_profile
            class Args:
                user = username
                users_file = args.users_file
                percode = percode
                realname = name
                mobile = mobile
                dry_run = False
            save_profile(res, Args())

            push_gh(res, uuid_env, token_env)
            print(f"  ✓ Secrets 已同步: {uuid_env}")

            # --- phase 4: auto-score from history ---
            print(f"  拉取 {args.days} 天订单历史...")
            try:
                api = EpayAPI(client_uuid=res["client_uuid"],
                              access_token=res["access_token"],
                              mer_id="61266001", mer_salt="pq1z400jlg")
                scores, order_count, dish_count = auto_score_from_history(api, days=args.days)
                print(f"  历史订单 {order_count} 条，菜品 {dish_count} 道，生成评分 {len(scores)} 条")

                # update users.cloud.json with scores
                with open(args.users_file, encoding="utf-8") as f:
                    users = json.load(f)
                for u in users:
                    if u.get("name") == username:
                        u["scores"] = scores
                        break
                with open(args.users_file, "w", encoding="utf-8") as f:
                    json.dump(users, f, ensure_ascii=False, indent=2)
                print(f"  ✓ 评分已写入 {args.users_file}")

            except Exception as e:
                print(f"  ⚠ 历史评分生成失败（绑定已成功）: {e}")

            results["success"].append(name)
            existing_names.add(name)

        except Exception as e:
            print(f"  ✗ 异常: {e}")
            results["failed"].append(name)

    # --- summary ---
    print(f"\n{'='*40}")
    print(f"批量绑卡完成")
    print(f"  成功: {len(results['success'])} 人 → {', '.join(results['success']) or '无'}")
    print(f"  跳过: {len(results['skipped'])} 人 → {', '.join(results['skipped']) or '无'}")
    print(f"  失败: {len(results['failed'])} 人 → {', '.join(results['failed']) or '无'}")

    # commit users.cloud.json
    if results["success"]:
        import subprocess
        try:
            subprocess.run(["git", "add", args.users_file], cwd=BASE_DIR, check=True)
            subprocess.run(["git", "commit", "-m",
                            f"batch_bind: {len(results['success'])}人绑卡+自动评分"],
                           cwd=BASE_DIR, check=True)
            for _ in range(3):
                p = subprocess.run(["git", "push"], cwd=BASE_DIR, capture_output=True)
                if p.returncode == 0:
                    print("  ✓ 已推送到 GitHub")
                    break
        except Exception as e:
            print(f"  ⚠ 推送失败: {e}")

    return 0 if not results["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
