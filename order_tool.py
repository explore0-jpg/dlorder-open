#!/usr/bin/env python3
"""温岭二中自动点餐工具

用法:
  python3 order_tool.py test            测试 API 连通性
  python3 order_tool.py menu [DATE]     打印菜单（DATE=YYYY-MM-DD/YYYYMMDD，默认今天）+ 智能评分
  python3 order_tool.py add <goods_id> [num]   加入购物车
  python3 order_tool.py cart            查看购物车
  python3 order_tool.py clear           清空购物车
  python3 order_tool.py order [goods_id] [--num N] [--option 规格] [--dry-run]
                        自动下单（不指定 goods_id 则智能选最优）
  python3 order_tool.py grab [--now]    等待到配置时间后自动下单
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

from config import Config, ConfigError, load
from dishes import dish_type_title
from epay import AuthError, EpayAPI, EpayError
from history import (build_stats, fetch_history, summarize)
from meal import (choose_best, format_goods, has_options, is_sold_out,
                  iter_goods, rank_menu)
from order import (do_order, grab_window_dates, has_order_for_day,
                   wait_until)


def build_api(cfg):
    return EpayAPI(
        client_uuid=cfg.client_uuid,
        access_token=cfg.access_token,
        mer_id=cfg.mer_id,
        mer_salt=cfg.mer_salt,
        base_url=cfg.base_url,
        uni_id=cfg.uni_id,
        attach=cfg.attach,
    )


def goods_target(goods, order_mode):
    return (int(goods.get("id") or goods.get("goods_id")),
            int(goods.get("category_id") or 0),
            str(goods.get("goods_title", "")),
            order_mode)


def print_menu(api, cfg, day=None, stats=None, only_top=None):
    menu_data = api.menu(day=day)
    ranked = rank_menu(menu_data, cfg.prefer(), stats=stats)
    print(f"===== 菜单评分（{day or '今天'}）=====")
    if stats:
        print(f"{'排名':<4}{'ID':<14}{'价格':<8}{'总分':<6}菜品（含历史偏好）")
    else:
        print(f"{'排名':<4}{'ID':<14}{'价格':<8}{'评分':<6}菜品")
    for i, row in enumerate(ranked, 1):
        if stats:
            score, gid, goods, _cat, reasons = row
        else:
            score, gid, goods, _cat = row
            reasons = []
        mark = " [售罄]" if goods.get("goods_stock") == 0 else ""
        print(f"{i:<5}{gid:<15}{goods.get('goods_price_sale', '?'):<9}"
              f"{score:<6}{format_goods(goods)}{mark}")
        if stats and reasons:
            parts = " | ".join(f"{r[0]} {r[2]}" for r in reasons if r[0] != "手动喜好")
            if parts:
                print("          -> " + parts[:130])
    best, _ = choose_best(menu_data, cfg.prefer(), stats=stats)
    if best:
        print()
        print(f"=> 推荐: {format_goods(best[2])}  得分 {best[0]}")
    return menu_data


def cmd_analyze(cfg, days, confirm):
    """拉取历史订单 → 正/反向喜好统计 → 荤素分开 → 提示写入 config.json。"""
    api = build_api(cfg)
    print(f"正在拉取最近 {days} 天订单...")
    infos, raw = fetch_history(api, days=days)
    stats = build_stats(infos)
    if not stats:
        print("没有可用的历史订单数据（已完成/退款商品为空）")
        return 0
    meat, veg = summarize(stats)

    def dump(title, rows):
        print(f"\n===== {title}（正向=已完成 +，负向=退款 -） =====")
        print(f"{'菜品':<26}{'类型':<4}{'订次':>5}{'退次':>5}{'净修正':>9}")
        for r in rows:
            print(f"{r['dish']:<26}{dish_type_title(r['type']):<4}"
                  f"{r['pos']:>5}{r['neg']:>5}{r['delta']:>+9.2f}")
    dump("荤菜", meat[:30])
    dump("素菜", veg[:30])

    print("\n预览：按当前统计对菜单今日菜品重新评分")
    try:
        menu_data = api.menu()
    except EpayError as e:
        print(f"（菜单获取失败：{e}，仅保存统计）")
        menu_data = None
    if menu_data:
        ranked = rank_menu(menu_data, cfg.prefer(), stats)
        print(f"\n{'排名':<4}{'ID':<14}{'价格':<8}{'总分':<6}菜品（各加分项）")
        for i, (score, gid, goods, _cat, reasons) in enumerate(ranked[:10], 1):
            base = reasons[0][1] if reasons else "?"
            extra = "".join(f" {r[0]}({r[1]}){r[2]}" for r in reasons[1:])
            print(f"{i:<5}{gid:<15}{goods.get('goods_price_sale', '?'):<9}"
                  f"{score:<6}{goods.get('goods_title', '?')}")
            if extra:
                print(f"      ->{extra[:120]}")

    if not confirm:
        print("\n统计结果已展示（未保存）。加 --confirm 写入 config.json。")
        return 0
    ans = input("\n确认把该统计写入 config.json 的 preferences.history_stats 词表？ [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("已取消，未写入")
        return 0
    path = cfg.path if hasattr(cfg, "path") else None
    write_history_stats(cfg, stats, infos)
    print("已写入偏好词表。以后 order/grab 会自动带上历史加分，可用 order --dry-run 查看效果。")
    return 0


def write_history_stats(cfg, stats, infos=None):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw.setdefault("preferences", {})["history_stats"] = {
        d: {"pos": s["pos"], "neg": s["neg"], "type": s["type"]}
        for d, s in stats.items()
    }
    if infos:
        info = _pick_delivery_info(infos)
        if info:
            dlv = raw.setdefault("delivery", {})
            changed = []
            for cfg_key, src_key, label in (
                ("name", "delivery_name", "姓名"),
                ("mobile", "delivery_mobile", "手机号"),
                ("area1", "delivery_area_L1", "配送地点1"),
                ("area2", "delivery_area_L2", "配送地点2"),
            ):
                if not dlv.get(cfg_key) and info.get(src_key):
                    dlv[cfg_key] = info[src_key]
                    changed.append(f"{label}={info[src_key]}")
            if changed:
                print("[自动填充 delivery] " + "、".join(changed))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def _pick_delivery_info(infos):
    """从最近的已完成/未退订单选一条，取 delivery_name/mobile/area。"""
    for info in infos:
        if (info.get("delivery_name") and info.get("delivery_mobile")
                and not (info.get("order_status_last") in ("退款", "已退款", "退款中"))):
            return info
    return None


def cmd_menu(cfg, day, show_history=False):
    api = build_api(cfg)
    hs = (cfg.prefer() or {}).get("history_stats") or {}
    stats = {d: dict(s) for d, s in hs.items()} if show_history and hs else None
    menu_data = print_menu(api, cfg, day, stats=stats)
    return 0


def cmd_test(cfg):
    api = build_api(cfg)
    info = api.store_info()
    print("连通性 OK，店铺:", info.get("store_title"))
    return 0


def cmd_add(cfg, gid, num, option):
    api = build_api(cfg)
    for _gid, goods, _cat in iter_goods(api.menu()):
        if str(_gid) == str(gid):
            from order import add_to_cart
            add_to_cart(api, goods_target(goods, cfg.order_mode), num, option)
            print(f"已加入购物车: {gid} x{num}")
            return 0
    print(f"未找到商品 {gid}，请先查看今日菜单")
    return 1


def cmd_cart(cfg, goods_mode):
    api = build_api(cfg)
    info = api.cart_info(goods_mode=goods_mode)
    cart = info.get("goods_cart")
    if not cart:
        print("购物车为空")
        return 0
    print("购物车:", json_str(cart))
    return 0


def json_str(o):
    import json
    return json.dumps(o, ensure_ascii=False, indent=2)


def cmd_clear(cfg):
    api = build_api(cfg)
    api.cart_clear()
    print("已清空购物车")
    return 0


def _stats_from_cfg(cfg):
    hs = (cfg.prefer() or {}).get("history_stats") or {}
    if not hs:
        return None
    return {d: dict(s) for d, s in hs.items()}


def cmd_order(cfg, gid, num, option, dry_run, use_history=False):
    api = build_api(cfg)
    stats = _stats_from_cfg(cfg)
    if use_history and not stats:
        print("config.json 中无 preferences.history_stats，先运行 analyze --confirm")
    if gid:
        target = None
        for _gid, goods, _cat in iter_goods(api.menu()):
            if str(_gid) == str(gid):
                target = goods_target(goods, cfg.order_mode)
                break
        if not target:
            print(f"未找到商品 {gid}")
            return 1
    else:
        menu_data = api.menu()
        best, _ranked = choose_best(menu_data, cfg.prefer(), stats=stats)
        if not best:
            print("没有符合偏好的菜品")
            return 1
        target = goods_target(best[2], cfg.order_mode)
        if stats:
            print(f"智能选择: {format_goods(best[2])}  得分 {best[0]}"
                  f"（含历史偏好）")
        else:
            print(f"智能选择: {format_goods(best[2])}  得分 {best[0]}")
    dlv = dict(cfg.delivery)
    dlv["date"] = best[2].get("goods_belong_day") or ""
    if not dlv.get("time"):
        dlv["time"] = "00:00 - 23:59"
    result = do_order(api, target, cfg.order_mode, dlv,
                      pay_offline=cfg.pay_offline, num=num, option=option,
                      dry_run=dry_run)
    print(result)
    return 0


def meal_titles_present(menu_data):
    """返回当日实际供应的餐别集合（如 {'午餐','晚餐'}）。"""
    out = set()
    for _cid, cat in ((menu_data.get("category_list") or {}) or {}).items():
        if isinstance(cat, dict) and cat.get("category_title"):
            out.add(cat["category_title"])
    return out


def check_order_rules(cfg, menu_data, day=None):
    """营业规则：需双餐齐备才下单的日子若缺少任一餐，返回原因字符串；否则 None。"""
    rules = getattr(cfg, "order_rules", None) or {}
    weekdays = rules.get("require_both_meals_weekday") or []
    if not weekdays:
        return None
    day = day or datetime.now()
    if day.weekday() not in weekdays:
        return None
    meals = meal_titles_present(menu_data)
    if "午餐" in meals and "晚餐" in meals:
        return None
    return f"规则：周{ '一二三四五六日'[day.weekday()] }需午餐+晚餐均供应才点，当前仅提供：{','.join(meals) or '无'}"


def cmd_preview(cfg):
    """预演：菜单周五晚发布后运行，算好下一抢单窗口的优选并存缓存，
    用于周六早 6 点抢单直接命中缓存，同时输出预览给用户提前改评分。"""
    api = build_api(cfg)
    prefs = cfg.prefer()
    min_score = prefs.get("min_score", 1)
    stats = _stats_from_cfg(cfg)
    rules = getattr(cfg, "order_rules", None) or {}
    days = grab_window_dates(datetime.now(), rules)
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              ".re", "preview_cache.json")
    cache = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "window_start": days[0].strftime("%Y%m%d"), "users": {"self": {}}}
    plan = cache["users"]["self"]
    print(f"=== 预演：下个抢单窗口 {days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d}（{len(days)} 天） ===")
    for dd in days:
        key = dd.strftime("%Y%m%d")
        try:
            menu = api.menu(day=key)
        except EpayError as e:
            print(f"[{key}] 拉取菜单失败: {e}")
            continue
        cats = menu.get("category_list") or {}
        if not cats:
            print(f"[{key}] 无菜单")
            continue
        line = f"[{key}]"
        for meal in (cfg.meals or ["午餐", "晚餐"]):
            sub = {"category_list": {cid: c for cid, c in cats.items()
                                     if (c or {}).get("category_title") == meal}}
            if not sub["category_list"]:
                line += f"  {meal}:无"
                continue
            best, ranked = choose_best(sub, prefs, min_score=min_score, stats=stats)
            ranked = [c for c in ranked if not is_sold_out(c[2])]
            if not ranked:
                line += f"  {meal}:不点(过低/售罄)"
                plan.setdefault(key, {})[meal] = None
            else:
                pick = ranked[0]
                ordered = ""
                try:
                    if has_order_for_day(api, key, meal):
                        ordered = "(已订)"
                except EpayError:
                    pass
                plan.setdefault(key, {})[meal] = {
                    "gid": str(pick[2].get("goods_id") or pick[2].get("id") or pick[1]),
                    "title": pick[2].get("goods_title", ""),
                    "score": round(pick[0], 2),
                    "price": str(pick[2].get("goods_price_sale") or pick[2].get("goods_price") or 0),
                }
                line += f"  {meal}:{format_goods(pick[2])} 分{pick[0]:.2f}{ordered}"
        print(line)
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"\n已写入缓存: {cache_path}")
    except OSError as e:
        print(f"[警告] 缓存写入失败: {e}")
    return 0


def cmd_grab_week(cfg, api, now_mode):
    """抢单日（如周六）整周扫描：按预演缓存优先，缺的午餐/晚餐补齐。"""
    rules = getattr(cfg, "order_rules", None) or {}
    prefs = cfg.prefer()
    min_score = prefs.get("min_score", 1)
    stats = _stats_from_cfg(cfg)
    days = grab_window_dates(datetime.now(), rules)

    skip_meals = set()
    for c in rules.get("cancelled", []):
        if c.get("day") and c.get("meal"):
            skip_meals.add((str(c["day"]), str(c["meal"])))
    prefer_map = {}
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              ".re", "preview_cache.json")
    try:
        pc = json.load(open(cache_path, encoding="utf-8"))
        for day, meals in (pc.get("users") or {}).get("self", {}).items():
            for meal, v in (meals or {}).items():
                gid = (v or {}).get("gid")
                if gid:
                    prefer_map[(str(day), meal)] = str(gid)
    except (OSError, ValueError, AttributeError):
        pass

    if not now_mode and cfg.delivery.get("name") and cfg.delivery.get("mobile"):
        hh, mm, *ss = (int(x) for x in str(getattr(cfg, "order_time", "06:00:00")).split(":"))
        target_dt = datetime.now().replace(
            hour=hh, minute=mm, second=ss[0] if ss else 0, microsecond=0)
        if (target_dt - datetime.now()).total_seconds() <= 0:
            target_dt += timedelta(days=1)
        print(f"目标时间: {target_dt:%Y-%m-%d %H:%M:%S}"
              f"  还差 {(target_dt - datetime.now()).total_seconds():.0f} 秒")
        wait_until(target_dt, advance_seconds=getattr(cfg, "advance_seconds", 5))

    print(f"=== 抢单窗口 {days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d}（{len(days)} 天）===")
    rc = 0
    for dd in days:
        day = dd.strftime("%Y%m%d")
        try:
            menu = api.menu(day=day)
        except EpayError as e:
            print(f"[{day}] 拉取菜单失败: {e}")
            rc |= 1
            continue
        cats = menu.get("category_list") or {}
        if not cats:
            print(f"[{day}] 无菜单，跳过")
            continue
        r, lines = order_day_meals(
            api, menu, day, meals=cfg.meals or ["午餐", "晚餐"], prefs=prefs,
            delivery=cfg.delivery, order_mode=getattr(cfg, "order_mode", "1"),
            pay_offline=cfg.pay_offline, min_score=min_score, stats=stats,
            label="本地", skip_meals=skip_meals, prefer=prefer_map)
        rc |= r
        for line in lines:
            print(line)
    return rc


def cmd_grab(cfg, now_mode):
    api = build_api(cfg)
    rules = getattr(cfg, "order_rules", None) or {}
    grab_wd = rules.get("grab_on_weekday", -1)
    if isinstance(grab_wd, str):
        try:
            grab_wd = int(grab_wd)
        except (TypeError, ValueError):
            grab_wd = -1
    if grab_wd >= 0 and datetime.now().weekday() != grab_wd:
        print(f"非抢单日（仅周{'一二三四五六日'[grab_wd]}下单），只校验不补单，跳过")
        return 0
    if grab_wd >= 0:
        return cmd_grab_week(cfg, api, now_mode)
    prefer = cfg.prefer()
    min_score = prefer.get("min_score", 1)
    stats = _stats_from_cfg(cfg)

    if not cfg.delivery.get("name") or not cfg.delivery.get("mobile"):
        print("config.json 缺少 delivery.name / delivery.mobile，无法下单")
        return 1

    try:
        menu_data = api.menu()
        best, ranked = choose_best(menu_data, prefer, min_score=min_score, stats=stats)
    except EpayError as e:
        print(f"[警告] 提前获取菜单失败: {e}（凭证失效？用 Via 书签重新抓 token）")
        menu_data = None
        best = ranked = None

    blocked = check_order_rules(cfg, menu_data) if menu_data else None
    if blocked:
        print(f"[跳过] {blocked}，本天不下单")
        return 0

    if not best:
        print("当前没有符合偏好的在售餐品，尝试等待后重查...")
        refresh_later = True
    else:
        print(f"目标餐品: {format_goods(best[2])}  得分 {best[0]}")
        refresh_later = False

    if now_mode:
        target_dt = datetime.now()
    else:
        order_time = cfg.order_time
        weekday = cfg.order_weekday
        hh, mm, *ss = (int(x) for x in order_time.split(":"))
        if weekday is not None and weekday >= 0:
            days_ahead = (weekday - datetime.now().weekday()) % 7
            target_dt = (datetime.now() + timedelta(days=days_ahead)).replace(
                hour=hh, minute=mm, second=ss[0] if ss else 0, microsecond=0)
            if (target_dt - datetime.now()).total_seconds() <= 0:
                target_dt += timedelta(days=7)
        else:
            target_dt = datetime.now().replace(
                hour=hh, minute=mm, second=ss[0] if ss else 0, microsecond=0)
            if (target_dt - datetime.now()).total_seconds() <= 0:
                target_dt += timedelta(days=1)
        print(f"目标时间: {target_dt:%Y-%m-%d %H:%M:%S}"
              f"  还差 {(target_dt - datetime.now()).total_seconds():.0f} 秒")

    wait_until(target_dt, advance_seconds=cfg.advance_seconds,
               on_tick=lambda: print("准备就绪，等待触发..."))

    if refresh_later:
        pass
    try:
        menu_data = api.menu()
    except EpayError as e:
        print(f"[失败] 到点重取菜单错误: {e}")
        print("（很可能是凭证失效：用 Via 书签重新抓 token 更新 config.json 后重试）")
        return 1
    blocked = check_order_rules(cfg, menu_data)
    if blocked:
        print(f"[跳过] {blocked}，本天不下单")
        return 0
    best, _ranked = choose_best(menu_data, prefer, min_score=min_score, stats=stats)
    if not best:
        print(f"到点了分数仍低于 min_score={min_score}，不值得点，本天下单失败")
        return 1

    target = goods_target(best[2], cfg.order_mode)
    print(f"到点选取: {format_goods(best[2])}  得分 {best[0]}")

    day = best[2].get("goods_belong_day") or ""
    if day:
        meal = (best[3] or {}).get("category_title", "")
        cancelled = set()
        for c in (getattr(cfg, "order_rules", None) or {}).get("cancelled", []) or []:
            if c.get("day") and c.get("meal"):
                cancelled.add((str(c["day"]), str(c["meal"])))
        if (str(day), meal) in cancelled:
            print(f"[跳过] {day} {meal} 在退餐名单，不重下（除非手动解除）")
            return 0
        try:
            if has_order_for_day(api, day, meal or None):
                print(f"[跳过] {day} 已存在订单，避免重复下单")
                return 0
        except EpayError as e:
            print(f"[警告] 查询已有订单失败（{e}），继续下单以保底")

    dlv = dict(cfg.delivery)
    dlv["date"] = day
    if not dlv.get("time"):
        dlv["time"] = "00:00 - 23:59"

    print(f"{datetime.now():%H:%M:%S} 开始下单! 目标商品 {target[0]}")
    try:
        result = do_order(api, target, cfg.order_mode, dlv,
                          pay_offline=cfg.pay_offline)
    except EpayError as e:
        print(f"[失败] 下单接口错误: {e}")
        print("（若是凭证失效：用 Via 书签重新抓 token 更新 config.json 后重试）")
        return 1
    print(result)
    return 0


def main():
    parser = argparse.ArgumentParser(prog="order_tool.py", description="温岭二中自动点餐工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("test", help="测试 API 连通性")
    p = sub.add_parser("menu", help="打印菜单 + 评分")
    p.add_argument("date", nargs="?", default=None,
                   help="YYYY-MM-DD 或 YYYYMMDD，默认今天")
    p.add_argument("--history", action="store_true",
                   help="叠加历史点餐偏好（自动使用 config 中 preferences.history_stats）")
    p = sub.add_parser("add", help="加入购物车")
    p.add_argument("goods_id")
    p.add_argument("num", nargs="?", default=1, type=int)
    p.add_argument("--option", default=None)
    sub.add_parser("cart")
    sub.add_parser("clear")
    p = sub.add_parser("order", help="自动下单")
    p.add_argument("goods_id", nargs="?")
    p.add_argument("--num", default=1, type=int)
    p.add_argument("--option", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--history", action="store_true",
                   help="选餐时叠加历史点餐偏好加分")
    p = sub.add_parser("grab", help="等待到配置时间后自动下单")
    p.add_argument("--now", action="store_true", help="立即下单（跳过等待）")
    p = sub.add_parser("preview", help="预演：算好下个抢单窗口优选并写缓存")
    p = sub.add_parser("analyze", help="拉取历史订单生成喜好统计并入库")
    p.add_argument("--days", type=int, default=180, help="统计最近 N 天（默认 180）")
    p.add_argument("--confirm", action="store_true",
                   help="确认后写入 config.json（不加仅预览）")

    args = parser.parse_args()

    try:
        cfg = load()
    except ConfigError as e:
        print(e)
        return 1

    fns = {
        "test": lambda: cmd_test(cfg),
        "menu": lambda: [cmd_menu(cfg, args.date, args.history), 0][1],
        "add": lambda: cmd_add(cfg, args.goods_id, args.num, args.option),
        "cart": lambda: cmd_cart(cfg, getattr(cfg, "order_mode", "1")),
        "clear": lambda: cmd_clear(cfg),
        "order": lambda: cmd_order(cfg, args.goods_id, args.num, args.option,
                                   args.dry_run, args.history),
        "grab": lambda: cmd_grab(cfg, args.now),
        "preview": lambda: cmd_preview(cfg),
        "analyze": lambda: cmd_analyze(cfg, args.days, args.confirm),
    }
    try:
        return fns[args.cmd]()
    except AuthError as e:
        print(f"认证失败: {e}\n请检查 config.json 中的 client_uuid / access_token（README 第五节）。")
        return 1
    except EpayError as e:
        print(f"接口错误: {e}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())