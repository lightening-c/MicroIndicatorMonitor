# -*- coding: utf-8 -*-
"""申万行业分类树：一/二/三级名称、成分股统计与涨跌幅。

输出 data/sw_tree.json：
  {"updated","trade_date","totals",
   "l1":[{code,name,chg,chg_stock,n_children,n_stocks,limit_up,limit_down}],
   "l2":[{...,parent}], "l3":[{...,parent}]}

成分股统计口径：申万指数成分股 + 东财全市场快照；
chg_stock 为成分股按总市值加权的涨跌幅；涨跌停按主板 10%、创业/科创 20%、
北交所 30%、ST 5% 判定。
用法：python src/fetch_sw_tree.py
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import akshare as ak
import pandas as pd
from paths import DATA_DIR

OUT = os.path.join(DATA_DIR, "sw_tree.json")
WORKERS = 8


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _conv(df, need_parent):
    out = []
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        code = str(r["行业代码"]).split(".")[0]
        item = {"code": code, "name": str(r["行业名称"])}
        if need_parent:
            item["parent"] = str(r["上级行业"])
        out.append(item)
    return out


def _hierarchy():
    """返回 (l1, l2, l3)；接口异常时沿用上一次成功保存的层级。"""
    l1 = l2 = l3 = None
    try:
        l1 = _conv(ak.sw_index_first_info(), False)
        l2 = _conv(ak.sw_index_second_info(), True)
        l3 = _conv(ak.sw_index_third_info(), True)
        _log(f"层级接口: 一级 {len(l1)} / 二级 {len(l2)} / 三级 {len(l3)}")
    except Exception as e:
        _log(f"[WARN] 层级接口失败 {type(e).__name__}，尝试沿用旧文件")

    if not l1:
        if os.path.exists(OUT):
            old = json.load(open(OUT, encoding="utf-8"))
            l1 = [{"code": x["code"], "name": x["name"]} for x in old.get("l1", [])]
            l2 = [{"code": x["code"], "name": x["name"], "parent": x.get("parent")}
                  for x in old.get("l2", [])]
            l3 = [{"code": x["code"], "name": x["name"], "parent": x.get("parent")}
                  for x in old.get("l3", [])]
            _log(f"沿用旧层级: 一级 {len(l1)} / 二级 {len(l2)} / 三级 {len(l3)}")
        else:
            raise RuntimeError("申万层级接口不可用，且无旧文件可沿用")
    return l1, l2, l3


def _realtime(level):
    try:
        df = ak.index_realtime_sw(symbol=level)
    except Exception:
        return {}
    out = {}
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        try:
            prev = float(r["昨收盘"])
            cur = float(r["最新价"])
            chg = (cur - prev) / prev * 100 if prev else None
        except Exception:
            chg = None
        out[str(r["指数代码"])] = round(chg, 2) if chg is not None else None
    return out


def _constituents(code):
    try:
        d = ak.index_component_sw(symbol=code)
        codes = [str(x).split(".")[0].zfill(6) for x in d["证券代码"]]
        return code, codes
    except Exception:
        return code, None


def _spot():
    """东财全市场快照：{6位代码: (名称, 涨跌幅, 总市值)}。"""
    try:
        d = ak.stock_zh_a_spot_em()
    except Exception as e:
        _log(f"[WARN] 东财快照失败 {type(e).__name__}，成分股统计跳过")
        return {}
    out = {}
    for _, r in d.iterrows():
        code = str(r["代码"]).split(".")[0].zfill(6)
        try:
            chg = float(r["涨跌幅"])
        except Exception:
            chg = None
        try:
            mv = float(r["总市值"])
        except Exception:
            mv = None
        out[code] = (str(r["名称"]), chg, mv)
    return out


def _limit_threshold(code, name):
    if "ST" in name.upper():
        return 4.8
    if code.startswith(("300", "301", "688")):
        return 19.8
    if code.startswith(("8", "4", "92")):
        return 29.8
    return 9.8


def _stats(codes, spot):
    if codes is None:
        return {"n_stocks": None, "limit_up": None, "limit_down": None, "chg_stock": None}
    up = down = 0
    wsum = chg_wsum = chg_sum = 0.0
    n_valid = 0
    for c in codes:
        info = spot.get(c)
        if not info:
            continue
        name, chg, mv = info
        if chg is None:
            continue
        n_valid += 1
        lim = _limit_threshold(c, name)
        if chg >= lim - 0.15:
            up += 1
        elif chg <= -(lim - 0.15):
            down += 1
        chg_sum += chg
        if mv and mv > 0:
            wsum += mv
            chg_wsum += chg * mv
    if not n_valid:
        return {"n_stocks": len(codes), "limit_up": None, "limit_down": None, "chg_stock": None}
    chg_stock = chg_wsum / wsum if wsum else chg_sum / n_valid
    return {"n_stocks": len(codes), "limit_up": up, "limit_down": down,
            "chg_stock": round(chg_stock, 2)}


def _trade_date():
    try:
        d = pd.read_csv(os.path.join(DATA_DIR, "sh_index.csv"), usecols=["日期"], dtype={"日期": str})
        if len(d):
            return str(d["日期"].max())
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def main():
    l1, l2, l3 = _hierarchy()
    l1_rt = _realtime("一级行业")
    l2_rt = _realtime("二级行业")

    nodes = l1 + l2 + l3
    _log(f"成分股: 共 {len(nodes)} 个行业节点，开始拉取")
    cons = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_constituents, x["code"]) for x in nodes]
        done = 0
        for f in as_completed(futs):
            code, codes = f.result()
            cons[code] = codes
            done += 1
            if done % 80 == 0:
                _log(f"  成分股进度 {done}/{len(nodes)}")
    failed = sum(1 for v in cons.values() if v is None)
    _log(f"成分股完成: 失败 {failed} 个")

    spot = _spot()
    _log(f"全市场快照: {len(spot)} 只")

    l2_children = {}
    for x in l2:
        l2_children[x.get("parent")] = l2_children.get(x.get("parent"), 0) + 1
    l3_children = {}
    for x in l3:
        l3_children[x.get("parent")] = l3_children.get(x.get("parent"), 0) + 1

    for x in l1:
        x["chg"] = l1_rt.get(x["code"])
        x.update(_stats(cons.get(x["code"]), spot))
        x["n_children"] = l2_children.get(x["name"], 0)
    for x in l2:
        x["chg"] = l2_rt.get(x["code"])
        x.update(_stats(cons.get(x["code"]), spot))
        x["n_children"] = l3_children.get(x["name"], 0)
    for x in l3:
        x["chg"] = None
        x.update(_stats(cons.get(x["code"]), spot))
        x["n_children"] = 0

    total_stocks = sum(x["n_stocks"] or 0 for x in l3)
    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": _trade_date(),
        "totals": {"l1": len(l1), "l2": len(l2), "l3": len(l3), "stocks": total_stocks},
        "l1": l1, "l2": l2, "l3": l3,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    _log(f"已保存 {OUT}（一级 {len(l1)} / 二级 {len(l2)} / 三级 {len(l3)}，成分股 {total_stocks} 家）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
