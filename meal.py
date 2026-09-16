from dishes import describe_goods_dishes
from history import history_profile_score


def iter_goods(menu_data, category_title=None):
    """遍历菜单里所有商品，产出 (goods_id, goods)。兼容 category_list 为 dict 或 list。
    category_title 给定时只产出该餐别（如“午餐”“晚餐”）的分类。"""
    categories = menu_data.get("category_list") or {}
    items = categories.values() if isinstance(categories, dict) else categories
    for cat in items or []:
        if not isinstance(cat, dict):
            continue
        if category_title and cat.get("category_title") != category_title:
            continue
        glist = cat.get("goods_list", {})
        gitems = glist.items() if isinstance(glist, dict) else glist
        for gid, goods in gitems:
            if isinstance(goods, dict):
                yield str(gid), goods, cat


def is_sold_out(goods):
    empty = goods.get("goods_empty")
    if empty not in (None, "", "0", 0, False, "false"):
        return True
    stock = goods.get("goods_stock")
    try:
        if stock is not None and int(stock) == 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def has_options(goods):
    return str(goods.get("options_enabled", "0")) == "1"


def score_goods(goods, prefs):
    """对单个商品打分。返回值 = -1 表示进黑名单/超预算，直接排除。"""
    desc = str(goods.get("goods_description", ""))
    title = str(goods.get("goods_title", ""))
    text = title + " " + desc

    blacklist = prefs.get("blacklist", [])
    for item in blacklist:
        if item and item in text:
            return -1

    try:
        price = float(goods.get("goods_price_sale") or goods.get("goods_price") or 0)
    except (TypeError, ValueError):
        price = 0
    max_price = prefs.get("max_price")
    if max_price and price > max_price:
        return -1

    scores = prefs.get("scores", {})
    price_rules = prefs.get("price_score", {})
    priced = set()
    score = 0
    # 价格条件分：价低于 threshold 用 low，否则用 high（例：酱烧鸡翅根 <8元→5分）
    for name, rule in price_rules.items():
        if name and name in text:
            scored = rule.get("low") if price < rule.get("threshold") else rule.get("high")
            score += scored if scored is not None else 0
            priced.add(name)
    for name, pts in scores.items():
        if name and name in text and name not in priced:
            score += pts

    # 高减分项（例：虾类），可带例外词（例：虾丸）不扣
    neg = prefs.get("negative_scores", {})
    excepts = prefs.get("negative_exceptions", [])
    for name, pts in neg.items():
        if name and name in text and not any(e and e in text for e in excepts):
            score += pts

    # 必选词（force_pick）：命中即强制压过其他一切
    for name in prefs.get("force_pick", []):
        if name and name in text:
            score += 999

    if score == 0:
        return 0
    if prefs.get("prefer_cheaper"):
        score = round(score - price / 10, 2)
    return score


def score_goods_with_history(goods, prefs, stats, neg_weight=0.8,
                             history_weight=0.2):
    """黑名单/价格排除 + 手动打分 + 历史喜好加分（默认历史×0.2）。

    返回 (score, reasons)，reasons 为 [(部分, 来源说明, 分值), ...]。
    """
    base = score_goods(goods, prefs)
    if base < 0:
        return -1, []
    hscore, detail = history_profile_score(goods, stats, neg_weight)
    hscore = hscore * history_weight
    reasons = [("手动喜好", f"{base}")]
    for dish, delta, dtype in detail:
        st = (stats.get(dish) or {})
        reasons.append((f"{dtype}:{dish}", f"历:订{st.get('pos', 0)}退{st.get('neg', 0)}",
                        f"{delta:+.2f}"))
    return round(base + hscore, 2), reasons


def rank_menu(menu_data, prefs, stats=None, neg_weight=0.8,
              history_weight=0.2):
    """返回按得分排序的候选列表。

    stats 为 None 时返回 4 元组 (score, gid, goods, category)；
    提供历史统计时返回 5 元组 (score, gid, goods, category, reasons)。
    """
    candidates = []
    for gid, goods, cat in iter_goods(menu_data):
        if stats:
            s, reasons = score_goods_with_history(goods, prefs, stats, neg_weight,
                                                  history_weight)
        else:
            s = score_goods(goods, prefs)
            reasons = []
        if s < 0:
            continue
        c = (s, gid, goods, cat)
        if stats:
            c = (*c, reasons)
        candidates.append(c)
    candidates.sort(key=lambda x: (x[0], float(x[2].get("goods_price_sale", 0))), reverse=True)
    return candidates


def choose_best(menu_data, prefs, min_score=1, stats=None, neg_weight=0.8,
                history_weight=0.2):
    ranked = rank_menu(menu_data, prefs, stats, neg_weight, history_weight)
    ranked = [c for c in ranked if c[0] >= min_score]
    if not ranked:
        return None, ranked
    return ranked[0], ranked


def format_goods(goods):
    opt = ""
    if has_options(goods):
        opt = " [含规格]"
    return (f"{goods.get('goods_title', '?')} ￥{goods.get('goods_price_sale', '?')}"
            f"{opt} | {goods.get('goods_description', '')}")