# -*- coding: utf-8 -*-
"""回补深市融资余额（2018 起），按日抓取并增量落盘 data/margin_szse.csv。"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd

from paths import DATA_DIR as DATA

CACHE = os.path.join(DATA, "margin_szse.csv")
START = "2018-01-01"


def trading_dates():
    p = os.path.join(DATA, "margin_balance.csv")
    d = pd.read_csv(p, dtype={"日期": str})
    return [x for x in d["日期"].tolist() if x >= START]


def main():
    dates = trading_dates()
    cache = {}
    if os.path.exists(CACHE):
        old = pd.read_csv(CACHE, dtype={"日期": str})
        cache = dict(zip(old["日期"], pd.to_numeric(old["深市融资余额"], errors="coerce")))
    todo = [d for d in dates if d not in cache][-240:]
    print(f"总 {len(dates)} 天，已有 {len(cache)}，待补 {len(todo)}", flush=True)
    if not todo:
        return

    def _one(d):
        for _ in range(2):
            try:
                x = ak.stock_margin_szse(date=d.replace("-", ""))
                if x is not None and len(x):
                    v = pd.to_numeric(x.iloc[0]["融资余额"], errors="coerce")
                    if pd.notna(v):
                        return d, float(v)
                return d, None
            except Exception:
                time.sleep(0.5)
        return d, None

    buf = 0
    streak = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_one, d) for d in todo]
        for i, f in enumerate(as_completed(futs), 1):
            d, v = f.result()
            if v is not None:
                cache[d] = v
                buf += 1
                streak = 0
            else:
                streak += 1
            if streak >= 15 or time.time() - t0 > 240:
                print("限流/超时，提前结束", flush=True)
                break
            if i % 50 == 0 or i == len(todo):
                out = pd.DataFrame({"日期": list(cache.keys()), "深市融资余额": list(cache.values())})
                out.sort_values("日期").to_csv(CACHE, index=False, encoding="utf-8-sig")
                print(f"  {i}/{len(todo)} 已存 {len(cache)} 天，{time.time()-t0:.0f}s", flush=True)
    out = pd.DataFrame({"日期": list(cache.keys()), "深市融资余额": list(cache.values())})
    out.sort_values("日期").to_csv(CACHE, index=False, encoding="utf-8-sig")
    print(f"完成：{len(cache)} 天，{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
