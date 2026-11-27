# -*- coding: utf-8 -*-
"""融资余额单独同步：抓两融（沪官方 + 深交所直连/金十兜底），再重算页面。

交易所通常在 T 日 21:00 后才披露当日两融，故供晚间定时任务调用。
"""
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_data as fd


def main():
    t0 = datetime.now()
    print(f"[{t0:%H:%M:%S}] 融资余额同步开始", flush=True)
    df = fd.fetch_margin_data()
    if df is None or df.empty:
        print("[ERR] 两融数据为空", flush=True)
        return 1
    df.to_csv(os.path.join(fd.DATA_DIR, "margin_balance.csv"), index=False, encoding="utf-8-sig")
    print(f"[{datetime.now():%H:%M:%S}] 融资余额 {len(df)} 行，最新 {df.iloc[-1]['日期']} "
          f"{float(df.iloc[-1]['沪深融资余额']):.0f} 亿元", flush=True)

    here = os.path.dirname(os.path.abspath(__file__))
    for script in ("analyze.py", "build_dashboard.py"):
        r = subprocess.run([sys.executable, os.path.join(here, script)], cwd=here)
        if r.returncode != 0:
            print(f"[ERR] {script} 退出码 {r.returncode}", flush=True)
            return 1
    print(f"[{datetime.now():%H:%M:%S}] 融资余额同步完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
