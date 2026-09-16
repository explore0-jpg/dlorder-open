"""菜品拆解与荤素分类。"""

import re

MEAT_HINTS = [
    "猪", "肉", "五花", "里脊", "排骨", "仔排", "牛", "羊", "鸡", "鸭",
    "排", "腿", "翅", "香肠", "肠", "鱼", "虾", "蟹", "蛤蜊", "鲍鱼",
    "鱿鱼", "鳕鱼", "带鱼", "培根", "火腿", "蛋", "肉丝", "肉片", "肉末",
    "丸子", "牛腩", "牛蛙", "鸡块", "鸭胗", "鸡胗", "猪蹄", "大排",
]

TITLE_STRIP = re.compile(r"^\s*\d{4,8}")          # 去掉前缀日期 2026/0907
SET_STRIP = re.compile(r"套餐[A-Za-z0-9号]*-?")
BIG_STRIP = re.compile(r"等\d+个?[件个].*$")        # 去掉 '等8件商品' 尾部
QTY_STRIP = re.compile(r"(?<=[\u4e00-\u9fff])\d+[只个份例]?$")  # 去掉数量后缀 卤蛋6/薯饼3
PUNCT_EDGE = "（）()[]｛｝{}··—一-—:：。.、，,；;"


def _clean(p):
    p = p.strip(PUNCT_EDGE).strip()
    p = re.sub(r"\s+", "", p)
    p = BIG_STRIP.sub("", p).strip(PUNCT_EDGE).strip()
    p = QTY_STRIP.sub("", p).strip(PUNCT_EDGE).strip()
    p = re.sub(r"[（(][^）)]*$", "", p)  # 去掉未闭合的中文括号残渣 '（小份'
    p = p.strip(PUNCT_EDGE).strip()
    return p


def parse_goods_dishes(title):
    """把 '0907套餐B-黄焖鸡+葱烧豆腐+香菇炒青菜等8件商品' 拆成 ['黄焖鸡', '葱烧豆腐', '香菇炒青菜']。"""
    s = str(title or "")
    s = TITLE_STRIP.sub("", s, count=1)           # 去日期前缀
    s = SET_STRIP.sub("", s, count=1).strip()     # 去 '套餐B-' 前缀
    parts = re.split(r"[+＋,，、]", s)
    dishes = []
    for p in parts:
        p = _clean(p)
        if not p or len(p) < 2 or re.fullmatch(r"[()（）,，、.{}.+\s\\-]+", p):
            continue
        dishes.append(p)
    if not dishes:
        p = _clean(s)
        if p:
            dishes.append(p)
    return dishes


def classify_dish(dish):
    """荤/素粗判：命中荤词 → 'meat'，否则 'veg'。"""
    return "meat" if any(k in dish for k in MEAT_HINTS) else "veg"


def dish_type_title(t):
    return "荤菜" if t == "meat" else "素菜"


def describe_goods_dishes(title):
    return [(d, classify_dish(d)) for d in parse_goods_dishes(title)]