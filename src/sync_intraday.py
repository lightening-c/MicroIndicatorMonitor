# -*- coding: utf-8 -*-
"""盘中轻量同步：只采集指数日线、市场宽度、拥挤度，然后重跑分析。

份额、两融、估值等 T 日盘后才有的数据不在此更新（沿用上一日）。
用法：python src/sync_intraday.py
"""
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_data as fd
from paths import DATA_DIR, CONFIG_FILE
import yaml

MARK_FILE = os.path.join(DATA_DIR, "intraday.json")


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def main():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    inds = cfg.get("indicators", {})
    groups = inds.get("major_indices", []) + inds.get("broad_indices", []) + inds.get("style_indices", [])

    ok, fail = 0, 0
    for ind in groups:
        try:
            sym = (ind.get("akshare_params") or {}).get("symbol")
            if not sym:
                continue
            df = fd.fetch_index_daily(sym, ind["name"])
            if df is not None and len(df):
                fd.save_csv(df, ind["id"])
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            _log(f"[WARN] {ind.get('name')} 失败 {type(e).__name__}")
    _log(f"指数 {ok} 成功 / {fail} 失败")

    # 市场宽度
    try:
        b = fd.fetch_market_breadth()
        if b:
            for k, v in b.items():
                fd.save_csv(v, k)
    except Exception as e:
        _log(f"[WARN] 宽度失败 {type(e).__name__}")

    # 市场拥挤度
    try:
        c = fd.fetch_market_crowding()
        if c is not None:
            fd.save_csv(c, "market_crowding")
    except Exception as e:
        _log(f"[WARN] 拥挤度失败 {type(e).__name__}")

    # 市值结构（用于综述里的流通市值）
    try:
        cap = fd.fetch_cap_structure()
        if cap is not None and len(cap):
            fd.save_csv(cap, "cap_structure")
    except Exception as e:
        _log(f"[WARN] 市值结构失败 {type(e).__name__}")

    # 两市成交额（交易所综指口径）
    try:
        t = fd.fetch_turnover_total()
        if t is not None and len(t):
            fd.save_csv(t, "turnover_all")
    except Exception as e:
        _log(f"[WARN] 成交额失败 {type(e).__name__}")

    # 盘中标记
    now = datetime.now()
    try:
        with open(MARK_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M")}, f)
    except Exception:
        pass

    # 重算并重建页面
    for script in ("analyze.py", "build_dashboard.py"):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
        r = subprocess.run([sys.executable, p], cwd=os.path.dirname(p))
        if r.returncode != 0:
            _log(f"[ERR] {script} 退出码 {r.returncode}")
            return 1
    _log("盘中同步完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
