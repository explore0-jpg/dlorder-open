import time
from datetime import timedelta

from epay import EpayError
from meal import (choose_best, format_goods, has_options, is_sold_out,
                  iter_goods)


def grab_window_dates(d, rules, step_days=8):
    """抢单窗口：返回抢单日及其后 step_days 天（含周末/工作日）。

    grab_on_weekday 为 None/-1 时每天可下，返回 [d]。
    否则返回以「下一个抢单日」为起点的窗口 —— 预览(preview)与抢单(grab)
    共用此函数，保证周五晚预览的内容与周六早抢单逐日对应。
    """
    gw = (rules or {}).get("grab_on_weekday", -1)
    if isinstance(gw, str):
        try:
            gw = int(gw)
        except (TypeError, ValueError):
            gw = -1
    if gw < 0:
        return [d]
    ahead = (gw - d.weekday()) % 7
    start = d + timedelta(days=ahead)
    return [start + timedelta(days=i) for i in range(step_days)]


def has_order_for_day(api, day, meal=None):
    """某用餐日(delivery_date)是否已有订单（未撤销）。有则返回 True 供上层跳过。
    meal 给定时只判定指定餐别（午餐/晚餐）。"""
    r = api.call("app.shop.getUserOrderGoodsList", {
        "startDate": str(day), "endDate": str(day), "page": 1, "limit": 20,
    })
    for it in (r or {}).get("data") or []:
        if str(it.get("delivery_date")) != str(day):
            continue
        if meal and str(it.get("category_title")) != meal:
            continue
        status = str(it.get("goods_status") or "")
        if status not in ("5", "6"):   # 5/6 视为已退/撤销，不阻挡
            return True
    return False


def resolve_option(api, target_gid, option=None):
    """处理带规格商品：若未指定，从菜单 options_content 取第一个有效选项名。"""
    if option:
        return option
    try:
        menu_data = api.menu()
    except EpayError:
        return None
    for gid, goods, cat in iter_goods(menu_data):
        if str(gid) == str(target_gid) and has_options(goods):
            content = goods.get("options_content") or goods.get("options") or ""
            if isinstance(content, dict):
                names = list(content.keys())
            else:
                names = [str(c).split(":")[0].strip()
                         for c in str(content).split(",") if c.strip()]
            if isinstance(content, str) and content.startswith("["):
                import json
                try:
                    names = [i.get("option_name", i.get("name", ""))
                             for i in json.loads(content) if isinstance(i, dict)]
                except Exception:
                    pass
            return names[0] if names else None
    return None


def add_to_cart(api, target, num=1, option=None):
    """target 为 (goods_id, category_id, goods_title, goods_mode) 元组。"""
    gid, cid, title, mode = target
    option = resolve_option(api, gid, option)
    return api.cart_buy(gid, cid, title, num=num, goods_mode=mode, option=option)


def do_order(api, target, order_mode, delivery, pay_offline=True, num=1,
             option=None, dry_run=False):
    """完整下单流程，返回人类可读结果。"""
    if dry_run:
        return "[DRY-RUN] 已跳过实际下单"

    # 1. 清空购物车（保险起见）
    api.cart_clear()

    # 2. 加入购物车
    add_to_cart(api, target, num=num, option=option)

    # 3. 确认订单
    api.cart_confirm(goods_mode=order_mode)

    # 4. 提交订单
    submit = api.order_submit(delivery, goods_mode=order_mode,
                              pay_offline=pay_offline, order_mode=order_mode)
    if not isinstance(submit, dict):
        raise EpayError("A004xxx", f"提交订单返回异常: {submit}")
    order_id = submit.get("order_id") or submit.get("id")
    order_salt = submit.get("order_salt") or submit.get("salt")
    if not order_id or not order_salt:
        raise EpayError("A004xxx", f"提交订单缺少 order_id/order_salt: {submit}")
    print(f"订单已提交: {order_id}")

    if pay_offline:
        # 线下结算在提交时已结算，勿再调 setUserOrderPay（会报 C001001 订单已成功）
        return f"下单成功！订单号: {order_id}（线下结算，提交即完成）"
    pay = api.order_pay(order_id, order_salt, pay_offline=False)
    return (f"下单成功！订单号: {order_id}"
            f"  支付结果: {pay.get('msg', pay) if isinstance(pay, dict) else pay}")


def order_day_meals(api, menu_data, day, meals, prefs, delivery, order_mode,
pay_offline=True, min_score=1, stats=None, dry_run=False,
                     label="点餐", option=None, num=1, skip_meals=None,
                     prefer=None):
    """按餐别（午餐/晚餐）分别选最优并下单，各自受 min_score / 已点防重约束。

    skip_meals: 集合 {(day, meal)}，命中则视为用户主动退餐，绝不重下。
    prefer: 字典 {(day, meal): goods_id}，命中且未售罄时优先下单该菜
            （预选缓存出的优选），售罄则自动回退普通评分。
    返回 (rc, lines)。rc=0 表示所有餐别都已处理（含合理跳过）。
    """
    lines = []
    rc = 0
    cats = menu_data.get("category_list") or {}
    for meal in (meals or ["午餐", "晚餐"]):
        if skip_meals and (str(day), meal) in skip_meals:
            lines.append(f"[{label}] {day} {meal} 已在退餐名单，不重下（除非手动解除）")
            continue
        sub = {"category_list": {
            cid: c for cid, c in cats.items()
            if (c or {}).get("category_title") == meal}}
        if not sub["category_list"]:
            lines.append(f"[{label}] {day} 无{meal}菜单，跳过")
            continue
        _best, ranked = choose_best(sub, prefs, min_score=min_score, stats=stats)
        ranked = [c for c in ranked if not is_sold_out(c[2])]
        if not ranked:
            lines.append(f"[{label}] {day} {meal} 分数低于 min_score={min_score}"
                         f"{'（或已售罄）' if _best else ''}，不值得点，跳过")
            continue
        prefer_hit = None
        if prefer:
            _want = prefer.get((str(day), meal))
            if _want is not None:
                for c in ranked:
                    g = c[2]
                    gidv = str(g.get("goods_id") or g.get("id") or c[1])
                    if gidv == str(_want):
                        prefer_hit = c
                        break
        if prefer_hit:
            ranked = [prefer_hit] + [c for c in ranked if c is not prefer_hit]
            lines.append(f"[{label}] {day} {meal} 缓存优选（售罄将自动换菜） {format_goods(prefer_hit[2])}  得分 {prefer_hit[0]}")
        best = ranked[0]
        gid, goods, cat = best[1], best[2], best[3]
        if dry_run:
            lines.append(f"[{label}] {meal} [DRY] {format_goods(goods)}  得分 {best[0]}")
            continue
        meal_day = goods.get("goods_belong_day") or str(day)
        try:
            if has_order_for_day(api, meal_day, meal):
                lines.append(f"[{label}] {meal_day} {meal} 已存在订单，跳过")
                continue
        except EpayError as e:
            print(f"[{label}] [警告] 查询已有订单失败（{e}），继续下单以保底")
        dlv = {k: (delivery or {}).get(k, "") for k in
               ("name", "mobile", "date", "time", "time_name", "area1", "area2")}
        dlv["date"] = meal_day
        if not dlv.get("time"):
            dlv["time"] = "00:00 - 23:59"
        lines.append(f"[{label}] {meal} 选取 {format_goods(goods)}  得分 {best[0]}")
        ordered = False
        for cand in ranked:
            cg = cand[2]
            target = (int(cg.get("id") or cg.get("goods_id")),
                      int(cg.get("category_id") or 0),
                      str(cg.get("goods_title", "")),
                      str(order_mode))
            if cand is not ranked[0]:
                lines.append(f"[{label}] {meal} 换菜尝试 {format_goods(cg)}  得分 {cand[0]}")
            try:
                result = do_order(api, target, order_mode, dlv,
                                  pay_offline=pay_offline, num=num, option=option)
                lines.append(f"[{label}] {result}")
                ordered = True
                break
            except EpayError as e:
                msg = str(e)
                if "A004911" in msg or "售罄" in msg:
                    lines.append(f"[{label}] {meal} {format_goods(cg)} 售罄，换下一档")
                    continue
                lines.append(f"[{label}] {meal} 下单失败: {e}")
                rc = 1
                break
        if not ordered and rc == 0:
            lines.append(f"[{label}] {meal} 无可售候选，跳过")
    return rc, lines


def wait_until(target_dt, advance_seconds=5, on_tick=None):
    """等待至目标时间（提前 advance_seconds 就绪，最后 1ms 精度对齐）。"""
    import time as _t
    from datetime import datetime

    now = datetime.now()
    wait = (target_dt - now).total_seconds()
    if wait > 0:
        _t.sleep(max(0, wait - advance_seconds))
        if on_tick:
            on_tick()
        while datetime.now() < target_dt:
            _t.sleep(0.001)
    return datetime.now()