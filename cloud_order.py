#!/usr/bin/env python3
"""云端无头抢餐器：读取 users.json，为每个用户自动下单。

用法:
  python3 cloud_order.py                     # 今天，真实下单
  python3 cloud_order.py --date 20260914     # 指定日期
  python3 cloud_order.py --dry-run           # 只选餐不下单
  python3 cloud_order.py --users users.cloud.json

凭证通过环境变量注入（对应 GitHub Actions Secrets），不写入仓库。
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from epay import AuthError, EpayAPI, EpayError
from meal import choose_best, format_goods, score_goods_with_history
from order import (do_order, grab_window_dates, has_order_for_day,
                   order_day_meals)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PREVIEW_CACHE = os.path.join(BASE_DIR, ".re", "preview_cache.json")
PREVIEW_MD = os.path.join(BASE_DIR, ".re", "preview.md")
PREVIEW_TXT = os.path.join(BASE_DIR, ".re", "preview.txt")
STATUS_JSON = os.path.join(BASE_DIR, ".re", "status.json")
DOWS = "一二三四五六日"


def user_prefer_overrides(u):
    """面板手动预选：{day: {meal: gid}}，权重高于预演缓存。"""
    out = {}
    for day, meals in (u.get("prefer_overrides") or {}).items():
        for meal, gid in (meals or {}).items():
            if gid:
                out[(str(day), str(meal))] = str(gid)
    return out


def prefer_for(user_name):
    """预演缓存 -> {(day, meal): goods_id}，读取失败不影响。"""
    prefer = {}
    try:
        with open(PREVIEW_CACHE, encoding="utf-8") as f:
            pc = json.load(f)
        for day, meals in (pc.get("users") or {}).get(user_name, {}).items():
            for meal, v in (meals or {}).items():
                gid = (v or {}).get("gid")
                if gid:
                    prefer[(str(day), meal)] = str(gid)
    except (OSError, ValueError, AttributeError):
        pass
    return prefer


def load_prefer_map(u):
    """回退链：面板手动预选 > 预演缓存。"""
    prefer = prefer_for(u.get("name", "?"))
    prefer.update(user_prefer_overrides(u))
    return prefer


def build_api(user, order_rules):
    api = EpayAPI(
        client_uuid=os.environ.get(user["uuid_env"], "").strip(),
        access_token=os.environ.get(user["token_env"], "").strip(),
        mer_id=user.get("mer_id", "61266001"),
        mer_salt=user.get("mer_salt", "pq1z400jlg"),
        base_url=user.get("base_url", "https://www.epay100.cn/901/6126/server/index.php"),
        uni_id=user.get("uni_id", "6126"),
        attach=user.get("attach", "5.6.2607315"),
    )
    return api


def user_prefs(u):
    return {
        "scores": u.get("scores", {}),
        "blacklist": u.get("blacklist", []),
        "negative_scores": u.get("negative_scores", {}),
        "negative_exceptions": u.get("negative_exceptions", []),
        "max_price": u.get("max_price"),
        "prefer_cheaper": u.get("prefer_cheaper", True),
        "min_score": u.get("min_score", 1),
        "force_pick": u.get("force_pick", []),
        "price_score": u.get("price_score", {}),
    }


def meal_titles_present(menu_data):
    out = set()
    for _cid, cat in ((menu_data.get("category_list") or {}) or {}).items():
        if isinstance(cat, dict) and cat.get("category_title"):
            out.add(cat["category_title"])
    return out


def blocked_by_rules(u, menu_data, d):
    rules = u.get("order_rules", {})
    for wd in rules.get("require_both_meals_weekday", []):
        if d.weekday() == wd:
            meals = meal_titles_present(menu_data)
            if not {"午餐", "晚餐"} <= meals:
                return f"需午晚齐备才点，当前仅提供 {','.join(meals) or '无'}"
    return None


def run_user(u, d, dry_run):
    name = u.get("name", "?")
    uuid = os.environ.get(u.get("uuid_env", ""), "").strip()
    token = os.environ.get(u.get("token_env", ""), "").strip()
    if not uuid or not token:
        print(f"[{name}] 缺少凭证（env {u.get('uuid_env')}/{u.get('token_env')} 未设置），跳过")
        return 0

    rules = u.get("order_rules") or {}
    grab_wd = rules.get("grab_on_weekday", -1)
    if isinstance(grab_wd, str):
        try:
            grab_wd = int(grab_wd)
        except (TypeError, ValueError):
            grab_wd = -1
    if grab_wd >= 0 and d.weekday() != grab_wd:
        print(f"[{name}] 非抢单日（仅周{'一二三四五六日'[grab_wd]}下单），只校验不补单，跳过")
        return 0

    target_days = grab_window_dates(d, rules)
    skip_meals = set()
    for c in rules.get("cancelled", []):
        if c.get("day") and c.get("meal"):
            skip_meals.add((str(c["day"]), str(c["meal"])))
    prefer_map = load_prefer_map(u)

    try:
        api = build_api(u, None)
        prefs = user_prefs(u)
        rdlv = u.get("delivery") or {}
        stats = u.get("history_stats") if u.get("use_history", True) else None
        for dd in target_days:
            menu = api.menu(day=dd.strftime("%Y%m%d"))
            cats = menu.get("category_list") or {}
            if not cats:
                print(f"[{name}] {dd:%Y-%m-%d} 无菜单，跳过")
                continue
            blocked = blocked_by_rules(u, menu, dd)
            if blocked:
                print(f"[{name}] {blocked}，跳过")
                continue
            _, lines = order_day_meals(
                api, menu, dd.strftime("%Y%m%d"),
                meals=u.get("meals", ["午餐", "晚餐"]),
                prefs=prefs,
                delivery=rdlv,
                order_mode=u.get("order_mode", "1"),
                pay_offline=u.get("pay_offline", True),
                min_score=prefs.get("min_score", 1),
                stats=stats,
                dry_run=dry_run,
                label=name,
                skip_meals=skip_meals,
                prefer=prefer_map,
            )
            for line in lines:
                print(f"[{name}] {line}")
        return 0
    except AuthError as e:
        print(f"[{name}] 认证失败: {e}")
        return 1
    except EpayError as e:
        print(f"[{name}] 下单失败: {e}")
        return 1


def run_preview(u, d):
    """周五晚预演：算好下个抢单窗口每人每天/每餐优选，不产生订单。

    返回 (rc, plan, text)。plan 结构 users.{name}.{day}.{meal}.{gid,title,score,price}
    供周六抢单 prefer 缓存；text 为人类可读 markdown。
    """
    from meal import choose_best, is_sold_out

    name = u.get("name", "?")
    uuid = os.environ.get(u.get("uuid_env", ""), "").strip()
    token = os.environ.get(u.get("token_env", ""), "").strip()
    if not uuid or not token:
        print(f"[{name}] 缺少凭证（env {u.get('uuid_env')}/{u.get('token_env')} 未设置），跳过")
        return 0, {}, ""

    rules = u.get("order_rules") or {}
    skip_meals = set()
    for c in rules.get("cancelled", []):
        if c.get("day") and c.get("meal"):
            skip_meals.add((str(c["day"]), str(c["meal"])))

    prefs = user_prefs(u)
    min_score = prefs.get("min_score", 1)
    stats = u.get("history_stats") if u.get("use_history", True) else None

    plan = {}
    text = []
    now_dt = datetime.now(timezone(timedelta(hours=8)))
    window = grab_window_dates(d, rules)
    text.append(f"# 下周菜单预演（{window[0]:%m-%d} ~ {window[-1]:%m-%d}）")
    text.append(f"- 生成: {now_dt:%Y-%m-%d %H:%M}  用户: {name}")
    text.append(f"- 周六 06:00 将按此缓存优先下单，售罄自动回退评分")
    text.append("")

    api = build_api(u, None)
    got_menu = False
    over = user_prefer_overrides(u)
    for dd in window:
        day = dd.strftime("%Y%m%d")
        plan[day] = {}
        try:
            menu = api.menu(day=day)
        except AuthError as e:
            raise
        except EpayError as e:
            text.append(f"## {day}（{DOWS[dd.weekday()]}） 拉取菜单失败: {e}")
            plan[day] = None
            continue
        cats = menu.get("category_list") or {}
        if not cats:
            text.append(f"## {day}（{DOWS[dd.weekday()]}） 无菜单（下周五发布后可见）")
            continue
        got_menu = True
        blocked = blocked_by_rules(u, menu, dd)
        if blocked:
            text.append(f"## {day}（周{DOWS[dd.weekday()]}） ⚠ {blocked}")
            continue
        for meal in u.get("meals", ["午餐", "晚餐"]):
            tag = f"{day}（周{DOWS[dd.weekday()]}） {meal}"
            if skip_meals and (day, meal) in skip_meals:
                text.append(f"- {tag}: 已退餐，不重下")
                plan[day][meal] = None
                continue
            sub = {"category_list": {
                cid: c for cid, c in cats.items()
                if (c or {}).get("category_title") == meal}}
            if not sub["category_list"]:
                text.append(f"- {tag}: 无菜单")
                plan[day][meal] = None
                continue
            _best, ranked = choose_best(sub, prefs, min_score=min_score, stats=stats)
            ranked = [c for c in ranked if not is_sold_out(c[2])]
            if not ranked:
                reason = "低于最低分" if _best else "无合适菜品"
                text.append(f"- {tag}: 🚫 {reason}（min_score={min_score}）")
                plan[day][meal] = None
                continue
            pick = ranked[0]
            manual = ""
            ov = over.get((day, meal))
            if ov:
                target = next((c for c in ranked if str(c[1]) == ov), None)
                if target:
                    pick = target
                    manual = " ✓手动指定"
                else:
                    manual = " ⚠手动指定无效(菜单无该套餐)"
            ordered = ""
            try:
                if has_order_for_day(api, day, meal):
                    ordered = " ✅已订"
            except EpayError:
                pass
            plan[day][meal] = {
                "gid": str(pick[2].get("goods_id") or pick[2].get("id") or pick[1]),
                "title": pick[2].get("goods_title", ""),
                "score": round(pick[0], 2),
                "price": str(pick[2].get("goods_price_sale")
                             or pick[2].get("goods_price") or 0),
            }
            stars = "★" if pick[0] >= 5 else "☆"
            text.append(f"- {tag}: {format_goods(pick[2])}  {stars} 评分 {pick[0]:.2f}{manual}{ordered}")
    text.append("")
    text.append("_自动生成，仅供参考。可随时在 config.json 评分里调整后重跑 preview。_")
    return (0 if got_menu else 1), plan, "\n".join(text)


def meal_menu_items(cats, meal):
    """某餐分类下的菜品清单：[{gid,title,price}]。"""
    items = []
    for _cid, c in (cats or {}).items():
        if (c or {}).get("category_title") != meal:
            continue
        for g in c.get("goods_list") or []:
            items.append({
                "gid": str(g.get("goods_id") or g.get("id") or ""),
                "title": g.get("goods_title", ""),
                "price": str(g.get("goods_price_sale") or g.get("goods_price") or 0),
            })
    return items


def run_status(u, d):
    """监测模式：校验 token、抓窗口菜单与已订状态，绝不产生订单。

    返回 (rc, user_status)。任何异常都不置 rc=1，监测型不把工作流弄红。
    """
    name = u.get("name", "?")
    st = {"name": name, "token_ok": False, "auth_error": None,
          "api_error": None, "meals": u.get("meals", ["午餐", "晚餐"])}
    uuid = os.environ.get(u.get("uuid_env", ""), "").strip()
    token = os.environ.get(u.get("token_env", ""), "").strip()
    if not uuid or not token:
        st["auth_error"] = f"缺少凭证（env {u.get('uuid_env')}/{u.get('token_env')} 未设置）"
        print(f"[{name}] 缺凭证，跳过")
        return 0, st
    api = build_api(u, None)
    rules = u.get("order_rules") or {}
    window = grab_window_dates(d, rules)
    for dd in window:
        day = dd.strftime("%Y%m%d")
        try:
            menu = api.menu(day=day)
        except AuthError as e:
            st["auth_error"] = f"凭证失效（{e}），请重新绑定"
            st["token_ok"] = False
            print(f"[{name}] {day} 验证失败: {e}")
            return 0, st
        except EpayError as e:
            st["api_error"] = str(e)
            st["token_ok"] = True
            continue
        cats = menu.get("category_list") or {}
        st.setdefault("menus", {})[day] = {}
        st.setdefault("orders", {})[day] = {}
        for meal in st["meals"]:
            items = meal_menu_items(cats, meal)
            if not items:
                st["menus"][day][meal] = []
                st["orders"][day][meal] = {"ordered": False, "title": None}
                continue
            st["menus"][day][meal] = items
            try:
                has = has_order_for_day(api, day, meal)
            except EpayError as e:
                has = None
            st["orders"][day][meal] = {"ordered": bool(has), "title": None}
    st["token_ok"] = True
    return 0, st


def main():
    ap = argparse.ArgumentParser(prog="cloud_order.py")
    ap.add_argument("--date", default=None, help="YYYYMMDD，默认今天")
    ap.add_argument("--users", default=os.path.join(BASE_DIR, "users.cloud.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preview", action="store_true",
                    help="周五预演：只算不订，生成 preview_cache.json / preview.md")
    ap.add_argument("--status", action="store_true",
                    help="监测：校验 token / 窗口菜单 / 已订状态，写入 .re/status.json")
    args = ap.parse_args()

    if not os.path.exists(args.users):
        print(f"找不到用户文件: {args.users}")
        return 2
    with open(args.users, "r", encoding="utf-8") as f:
        users = json.load(f)

    d = datetime.now(timezone(timedelta(hours=8))).date()
    if args.date:
        ds = str(args.date).replace("-", "")
        if len(ds) == 8:
            d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))
        else:
            print(f"日期格式需为 YYYYMMDD，得到: {args.date}")
            return 2

    if args.preview:
        rc = 0
        plans = {}
        texts = []
        print(f"=== 云端预演（下个抢单窗口，以 {d:%Y-%m-%d} 为基准）用户数={len(users)} ===")
        for u in users:
            try:
                r, plan, text = run_preview(u, d)
            except Exception as e:  # noqa: BLE001
                print(f"[{u.get('name', '?')}] 预演异常: {e!r}")
                r = 1
                plan = None
                text = ""
            rc |= r
            if plan is not None:
                plans[u.get("name", "?")] = plan
            if text:
                texts.append(text)
        os.makedirs(os.path.join(BASE_DIR, ".re"), exist_ok=True)
        merged = {"users": {}}
        for name, plan in (plans or {}).items():
            merged["users"][name] = plan or {}
        win_start = None
        for _name, plan in (merged["users"] or {}).items():
            if plan:
                win_start = sorted(plan.keys())[0]
                break
        with open(PREVIEW_CACHE, "w", encoding="utf-8") as f:
            json.dump({"generated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                       "window_start": win_start,
                       "users": merged["users"]}, f, ensure_ascii=False, indent=1)
        md = "\n\n".join(texts) or "（无可用用户/凭证，未生成预演）"
        with open(PREVIEW_MD, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print("=" * 20)
        print(md)
        print("已写入:", PREVIEW_CACHE, "/", PREVIEW_MD)
        sys.exit(rc)

    if args.status:
        print(f"=== 云端监测（窗口自 {d:%Y-%m-%d} 起）用户数={len(users)} ===")
        out = {
            "generated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
            "window_start": d.strftime("%Y%m%d"),
            "users": {},
            "orders": {},
            "menus": {},
        }
        rc = 0
        for u in users:
            r, st = run_status(u, d)
            rc |= r
            name = st.pop("name")
            out["users"][name] = st
            for day, meals in (st.get("orders") or {}).items():
                out["orders"].setdefault(day, {})[name] = meals
            for day, meals in (st.get("menus") or {}).items():
                for meal, items in (meals or {}).items():
                    if items:
                        out["menus"].setdefault(day, {}).setdefault(meal, items)
            print(f"[{name}] token_ok={st['token_ok']}"
                  + (f" auth_error={st['auth_error']}" if st.get("auth_error") else ""))
        os.makedirs(os.path.join(BASE_DIR, ".re"), exist_ok=True)
        with open(STATUS_JSON, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("已写入:", STATUS_JSON)
        sys.exit(0 if os.path.exists(STATUS_JSON) else 1)

    print(f"=== 云端抢餐 {d:%Y-%m-%d} (dry_run={args.dry_run}) 用户数={len(users)} ===")
    rc = 0
    for u in users:
        try:
            rc |= run_user(u, d, args.dry_run)
        except Exception as e:  # noqa: BLE001
            print(f"[{u.get('name', '?')}] 异常: {e!r}")
            rc |= 1
    sys.exit(rc)


if __name__ == "__main__":
    main()