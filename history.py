"""历史订单拉取 + 喜好统计（正向=已完成餐品，负向=退款餐品）。"""

import math
from datetime import date, timedelta

from dishes import classify_dish, describe_goods_dishes, parse_goods_dishes

COMPLETED = {"商家已完成", "已完成", "交易完成", "完成"}
REFUND = {"退款", "已退款", "退款中"}


WINDOW_DAYS = 30


def _fmt(d):
    return d.strftime("%Y%m%d") if isinstance(d, date) else str(d)


def _list_items(resp):
    """接口返回 data 为 {current_page, data:[...], last_page}，取出订单数组与末页。"""
    if not isinstance(resp, dict):
        return [], 1
    page = resp.get("data") or {}
    if isinstance(page, dict):
        items = page.get("data") or []
        last = page.get("last_page") or page.get("current_page") or 1
    else:
        items = page or []
        last = resp.get("last_page") or resp.get("current_page") or 1
    return items, last


def fetch_order_list(api, start, end, page=1, limit=50):
    resp = api.call("app.shop.getUserOrderList", {
        "startDate": _fmt(start), "endDate": _fmt(end),
        "page": page, "limit": limit,
    })
    return _list_items(resp)


def fetch_refund_list(api, start, end, page=1, limit=50):
    resp = api.call("app.shop.getUserOrderRefundList", {
        "startDate": _fmt(start), "endDate": _fmt(end),
        "page": page, "limit": limit,
    })
    return _list_items(resp)


def fetch_order_info(api, order):
    return api.call("app.shop.getUserOrderInfo", {
        "mer_id": api.mer_id,
        "order_id": order["id"],
        "order_salt": order["salt"],
    })


def _fetch_all(api, fn_fetch, start, end, progress=None, label=""):
    """按 ≤30 天窗口 + 分页拉取全部订单，去重返回 {oid: order}。"""
    acc = {}
    cur = start
    n = 0
    while cur <= end:
        win_end = min(end, cur + timedelta(days=WINDOW_DAYS))
        page = 1
        while True:
            items, last = fn_fetch(api, cur, win_end, page=page)
            for it in items:
                if not isinstance(it, dict) or "id" not in it:
                    continue
                acc.setdefault(it["id"], it)
            if page >= last or not items:
                break
            page += 1
            if page > 50:
                break
        n += 1
        if progress:
            progress(f"{label}{_fmt(cur)}~{_fmt(win_end)} 累计 {len(acc)} 单")
        cur = win_end + timedelta(days=1)
    return acc


def fetch_history(api, days=180, progress=print, order_hint="completed"):
    """拉取日内订单；返回 (orders_info, orders_raw)。orders_info 含每单 goods_list。"""
    end = date.today()
    start = end - timedelta(days=days)

    # 主订单列表（含已完成/待核销等）
    seen = _fetch_all(api, fetch_order_list, start, end, progress, "订单 ")
    # 退款订单列表并入
    refund_seen = _fetch_all(api, fetch_refund_list, start, end, progress, "退款 ")
    seen.update(refund_seen)
    raw = list(seen.values())

    if progress:
        progress(f"共 {len(raw)} 单，开始拉取每单明细...")
    infos = []
    for i, o in enumerate(raw, 1):
        if progress and i % 10 == 0:
            progress(f"明细 {i}/{len(raw)}")
        try:
            info = fetch_order_info(api, o)
        except Exception:
            continue
        info["_order"] = o
        infos.append(info)
    return infos, raw


def good_record(g):
    """单个 goods_list 条目 → (delivery_date, category_title, goods_title, count, is_refund, status)。"""
    return {
        "delivery_date": g.get("delivery_date"),
        "category_title": g.get("category_title", ""),
        "goods_title": g.get("goods_title", ""),
        "goods_id": g.get("goods_id"),
        "goods_count": g.get("goods_count", 1),
        "goods_price": g.get("goods_price", "0"),
        "is_refund": bool(g.get("is_refund")),
        "goods_status_name": g.get("goods_status_name", ""),
    }


def build_stats(infos):
    """从订单明细构建菜品统计 {dish: {pos, neg, type}}。"""
    stats = {}
    for info in infos:
        status_last = info.get("order_status_last", "")
        for g in info.get("goods_list") or []:
            rec = good_record(g)
            dishes = parse_goods_dishes(rec["goods_title"])
            neg = rec["is_refund"] or status_last in REFUND
            pos = (not neg) and status_last in COMPLETED
            if not rec["goods_title"] or not (pos or neg):
                continue
            for d in set(dishes):
                st = stats.setdefault(d, {"pos": 0, "neg": 0, "type": classify_dish(d)})
                st["pos" if pos else "neg"] += 1
    return stats


def dish_delta(stats, dish, neg_weight=0.8, cap=None):
    """一个菜品词给出的历史加分（对数稀释，负向折算）。"""
    st = stats.get(dish)
    if not st:
        return 0.0
    delta = math.log1p(st["pos"]) - neg_weight * math.log1p(st["neg"])
    if cap is not None:
        delta = max(-cap, min(cap, delta))
    return delta


def score_title(title, stats, neg_weight=0.8):
    """对一个商品标题累计历史得分，返回 (score, [(dish, delta, type), ...])。"""
    score = 0.0
    detail = []
    seen = set()
    for dish, dtype in describe_goods_dishes(title):
        if dish in seen:
            continue
        seen.add(dish)
        delta = dish_delta(stats, dish, neg_weight)
        if delta:
            detail.append((dish, delta, dtype))
        score += delta
    return score, detail


def history_profile_score(goods, stats, neg_weight=0.8):
    """评分入口：对菜单商品叠加历史喜好分。"""
    if not stats:
        return 0.0, []
    return score_title(str(goods.get("goods_title", "")), stats, neg_weight)


def summarize(stats):
    """返回 (meat, veg) 两个 [[type, dish, pos, neg, delta]...] 排序列表，供审查展示。"""
    rows = []
    for dish, st in stats.items():
        delta = dish_delta(stats, dish)
        rows.append({
            "type": st["type"], "dish": dish,
            "pos": st["pos"], "neg": st["neg"], "delta": round(delta, 2),
        })
    meat = sorted([r for r in rows if r["type"] == "meat"],
                  key=lambda r: r["delta"], reverse=True)
    veg = sorted([r for r in rows if r["type"] == "veg"],
                 key=lambda r: r["delta"], reverse=True)
    return meat, veg