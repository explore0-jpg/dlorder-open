#!/usr/bin/env python3
"""拉取某用户最近 N 天订单，按菜单词频统计（荤/素）生成 Excel 评分表供本人打分。

用法:
  python3 dish_score_table.py                       # 用 env 凭证（CLOUD_UUID_*/CLOUD_TOKEN_*）
  python3 dish_score_table.py --user friend --days 180 --out /sdcard/Download/...
  python3 dish_score_table.py --uuid xxx --token yyy
依赖导出 utils.goods_list 解析（dishes）。
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover
    sys.exit("缺少 openpyxl，请先 pip install openpyxl")

from dishes import classify_dish, parse_goods_dishes
from epay import EpayAPI
from history import (COMPLETED, REFUND, fetch_history, fetch_order_info,
                     good_record, _fetch_all, fetch_order_list,
                     fetch_refund_list)


def build_api(uuid, token, cfg=None):
    return EpayAPI(client_uuid=uuid, access_token=token,
                   mer_id=(cfg or {}).get("mer_id", "61266001"),
                   mer_salt=(cfg or {}).get("mer_salt", "pq1z400jlg"),
                   base_url=(cfg or {}).get("base_url",
                                            "https://www.epay100.cn/901/6126/server/index.php"),
                   uni_id=(cfg or {}).get("uni_id", "6126"),
                   attach=(cfg or {}).get("attach", "5.6.2607315"))


def count_dishes(infos):
    """按订单明细行统计每道菜被点的次数（同一行内去重）。"""
    cnt = Counter()
    typ = {}
    for info in infos:
        status_last = info.get("order_status_last", "")
        for g in info.get("goods_list") or []:
            rec = good_record(g)
            title = rec["goods_title"]
            if not title:
                continue
            for d in set(parse_goods_dishes(title)):
                cnt[d] += 1
                typ[d] = classify_dish(d)
    return cnt, typ


def sheet_for(ws, rows, start=2):
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 40
    ws.column_dimensions["E"].width = 46
    ws.freeze_panes = "A3"
    ws.append(["菜品", "次数", "评分(7=超爱 5-6=喜欢 3-4=一般 1-2=凑合 0=绝对不吃)"])
    for dish, count in rows:
        ws.append([dish, count, ""])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="friend", help="用户节点名")
    ap.add_argument("--uuid", default=os.environ.get("CLOUD_UUID_FRIEND", ""))
    ap.add_argument("--token", default=os.environ.get("CLOUD_TOKEN_FRIEND", ""))
    ap.add_argument("--users-file", default="users.cloud.json")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--out", default=None, help="输出 xlsx 路径")
    ap.add_argument("--min-count", type=int, default=1, help="过滤少于该次数的菜")
    args = ap.parse_args()

    uuid = args.uuid.strip()
    token = args.token.strip()
    users = json.load(open(args.users_file))
    node = next((u for u in users if u.get("name") == args.user), {})
    if not uuid or not token:
        print("缺少凭证：请设置 env CLOUD_UUID_%s/CLOUD_TOKEN_%s 或 --uuid/--token"
              % (str(args.user).upper(), str(args.user).upper()))
        return 1
    print("开始拉取 %s 最近 %d 天订单..." % (args.user, args.days))
    api = build_api(uuid, token, node)
    infos, raw = fetch_history(api, days=args.days,
                               progress=lambda m: print("  ", m))
    print("订单明细", len(infos), "条")
    cnt, typ = count_dishes(infos)
    if not cnt:
        print("没有可统计的菜品，请检查凭证/日期范围")
        return 1

    meat_rows = sorted([(d, n) for d, n in cnt.items() if typ.get(d) == "meat" and n >= args.min_count],
                       key=lambda r: r[1], reverse=True)
    veg_rows = sorted([(d, n) for d, n in cnt.items() if typ.get(d) == "veg" and n >= args.min_count],
                      key=lambda r: r[1], reverse=True)

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "菜单评分表_%s_%d天.xlsx" % (args.user, args.days))
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "荤菜"
    sheet_for(ws1, meat_rows)
    ws2 = wb.create_sheet("素菜")
    sheet_for(ws2, veg_rows)
    wb.save(out)

    print("荤菜 %d 道、素菜 %d 道（>=%d 次）" % (len(meat_rows), len(veg_rows), args.min_count))
    print("已生成:", out)


if __name__ == "__main__":
    sys.exit(main())