# -*- coding: utf-8 -*-
"""盘中实时快照（ETF 资金跟踪的「当日」视图）。

口径
----
份额按日披露、盘中不变，因此盘中只能用「最新份额 × 实时价」估算 ETF 市值：
    pct_est = Σ(最新份额 × 实时价) / (指数实时点位 × K) × 100
K 来自 data/nfgj_k.json（fetch_nfgj.py 标定并缓存 30 天）。
这是估算值，不是真实申赎（申赎最早 T 日收盘后可见）。

数据源
------
- 新浪实时行情 https://hq.sinajs.cn/list=...（批量，需 Referer）
    个股/ETF：名称,今开,昨收,现价,最高,最低,买一,卖一,成交量(股),成交额(元),...
    指数 s_xx：名称,点位,涨跌,涨跌幅,成交量(手),成交额(万元)
- data/nfgj_shares_latest.csv（每只 ETF 最新一日份额）
- data/nfgj_k.json（流通市值标定 K）

输出
----
- data/rt_snapshot.json  最新一次快照（页面读取）
- data/rt_intraday.csv    采样追加：time,key,name,pct,idx_level,etf_mv_yi,amt_yi
"""
import json
import os
from datetime import datetime

import pandas as pd
import requests
import yaml

from paths import DATA_DIR, CONFIG_FILE

SHARES_LATEST = os.path.join(DATA_DIR, "nfgj_shares_latest.csv")
K_CACHE = os.path.join(DATA_DIR, "nfgj_k.json")
SNAP_FILE = os.path.join(DATA_DIR, "rt_snapshot.json")
INTRA_FILE = os.path.join(DATA_DIR, "rt_intraday.csv")
PX_FILE = os.path.join(DATA_DIR, "nfgj_px.csv")

# 异动阈值
VOL_RATIO = 1.5      # 当日成交额 / 近5日日均（按已交易时长折算）≥ 1.5 倍
VOL_MIN_YI = 0.5     # 成交额低于 0.5 亿不报（小基金噪声）
PREM_DIFF = 1.0      # ETF 涨跌幅与指数涨跌幅偏离 ≥ 1.0pp
FLOW_PP = 0.03       # 指数占比相对昨收变化 ≥ 0.03pp

HDR = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}
SINA = "https://hq.sinajs.cn/list="
INDEX_SINA = {
    "hs300": "s_sh000300",
    "zz500": "s_sh000905",
    "zz1000": "s_sh000852",
    "star50": "s_sh000688",
    "chinext": "s_sz399006",
}
CHUNK = 40


def sina_symbol(code):
    sym, ex = code.split(".")
    return ("sh" if ex.upper() == "SH" else "sz") + sym


def _sina(symbols):
    """批量取新浪行情，返回 {symbol: [字段...]}。"""
    out = {}
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i:i + CHUNK]
        for attempt in range(3):
            try:
                r = requests.get(SINA + ",".join(chunk), headers=HDR, timeout=12)
                r.encoding = "gbk"
                for line in r.text.strip().split("\n"):
                    if '="' not in line:
                        continue
                    head, body = line.split('="', 1)
                    sym = head.split("hq_str_")[-1]
                    out[sym] = body.rstrip('";').split(",")
                break
            except Exception:
                if attempt == 2:
                    raise
    return out


def market_open(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (915 <= hm <= 1135) or (1255 <= hm <= 1505)


def _load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("nfgj", {}) or {}


def _load_k():
    if not os.path.exists(K_CACHE):
        return {}
    try:
        with open(K_CACHE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _load_latest_shares():
    if not os.path.exists(SHARES_LATEST):
        return {}
    d = pd.read_csv(SHARES_LATEST, dtype={"code": str})
    return dict(zip(d["code"], pd.to_numeric(d["shares"], errors="coerce")))


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def session_fraction(now=None):
    """当日累计成交额的典型占比（0~1）。

    成交量在日内呈 U 形分布，线性折算会在早盘系统性高估放量倍数，
    这里用经验分布：开盘半小时约完成全天 22%，午间 52%，尾盘 100%。
    """
    now = now or datetime.now()
    hm = now.hour * 60 + now.minute
    curve = [
        (9 * 60 + 30, 0.00), (10 * 60, 0.22), (10 * 60 + 30, 0.33),
        (11 * 60, 0.42), (11 * 60 + 30, 0.52), (13 * 60, 0.52),
        (13 * 60 + 30, 0.63), (14 * 60, 0.74), (14 * 60 + 30, 0.86),
        (15 * 60, 1.00),
    ]
    if hm <= curve[0][0]:
        return 0.0
    if hm >= curve[-1][0]:
        return 1.0
    for (h0, v0), (h1, v1) in zip(curve, curve[1:]):
        if h0 <= hm <= h1:
            return round(v0 + (v1 - v0) * (hm - h0) / (h1 - h0), 4)
    return 1.0


def load_avg_amounts(n=5):
    """每只 ETF 近 n 个交易日的日均成交额（亿元），用于判断盘中放量。"""
    if not os.path.exists(PX_FILE):
        return {}
    try:
        d = pd.read_csv(PX_FILE, dtype={"date": str, "code": str})
        d["amount"] = pd.to_numeric(d["amount"], errors="coerce")
        days = sorted(d["date"].unique())[-n:]
        sub = d[d["date"].isin(days)]
        return (sub.groupby("code")["amount"].mean() / 1e8).to_dict()
    except Exception:
        return {}


def load_prev_pct(key):
    """昨日收盘占比（%）与日期。"""
    p = os.path.join(DATA_DIR, f"nfgj_{key}.csv")
    if not os.path.exists(p):
        return None, None
    try:
        d = pd.read_csv(p, dtype={"date": str})
        r = d.iloc[-1]
        return float(r["pct"]), str(r["date"])
    except Exception:
        return None, None


def snapshot():
    """取一次盘中快照，写 rt_snapshot.json 与 rt_intraday.csv。"""
    cfg = _load_config()
    groups = cfg.get("indices") or []
    shares = _load_latest_shares()
    kcache = _load_k()

    lists = {}
    all_codes = []
    for g in groups:
        p = os.path.join(DATA_DIR, f"nfgj_etf_list_{g['key']}.csv")
        if not os.path.exists(p):
            lists[g["key"]] = []
            continue
        d = pd.read_csv(p, dtype={"code": str})
        d = d[d["code"].isin(shares.keys())]
        lists[g["key"]] = d.to_dict("records")
        all_codes += d["code"].tolist()

    etf_syms = [sina_symbol(c) for c in dict.fromkeys(all_codes)]
    idx_syms = [INDEX_SINA[g["key"]] for g in groups if g["key"] in INDEX_SINA]
    q = _sina(list(dict.fromkeys(etf_syms + idx_syms)))

    def etf_quote(code):
        v = q.get(sina_symbol(code))
        if not v or len(v) < 10:
            return None
        price = _num(v[3]) or 0.0
        prev = _num(v[2]) or 0.0
        if price <= 0:
            price = prev
        return {
            "price": price,
            "chg": round((price / prev - 1) * 100, 2) if prev else None,
            "amount_yi": round((_num(v[9]) or 0) / 1e8, 2),
        }

    snap = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_open": market_open(), "indices": [], "etfs": {}, "alerts": []}
    rows = []
    frac = session_fraction()
    avg_amt = load_avg_amounts()

    for g in groups:
        key = g["key"]
        iv = q.get(INDEX_SINA.get(key, "")) or []
        idx_level = _num(iv[1]) if len(iv) > 1 else None
        idx_chg = _num(iv[3]) if len(iv) > 3 else None
        idx_amt_yi = round((_num(iv[5]) or 0) / 1e4, 0) if len(iv) > 5 else None

        kk = (g.get("em_secid") or "").replace(".", "_")
        k = (kcache.get(kk) or {}).get("k")
        float_mv = idx_level * k if (idx_level and k) else None

        mv_sum = 0.0
        amt_sum = 0.0
        detail = []
        for rec in lists.get(key, []):
            code = rec["code"]
            sh = shares.get(code) or 0
            qt = etf_quote(code)
            if not qt:
                continue
            mv = sh * qt["price"] / 1e8 if sh else 0.0
            mv_sum += mv
            amt_sum += qt["amount_yi"]

            flags, vr, dev = [], None, None
            base = avg_amt.get(code)
            if base and base > 0 and frac > 0 and qt["amount_yi"] >= VOL_MIN_YI:
                vr = round(qt["amount_yi"] / (base * frac), 2)
                if vr >= VOL_RATIO:
                    flags.append(f"放量 {vr:.1f}x")
            if idx_chg is not None and qt["chg"] is not None:
                dev = round(qt["chg"] - idx_chg, 2)
                if abs(dev) >= PREM_DIFF:
                    flags.append(f"偏离指数 {dev:+.1f}pp")

            detail.append({
                "code": code, "name": rec.get("name") or code,
                "price": round(qt["price"], 3), "chg": qt["chg"],
                "amount_yi": qt["amount_yi"], "mv_yi": round(mv, 1),
                "vol_ratio": vr, "dev": dev, "flags": flags,
            })
        detail.sort(key=lambda x: x["mv_yi"], reverse=True)

        pct = (mv_sum / (float_mv / 1e8) * 100) if float_mv else None
        prev_pct, prev_date = load_prev_pct(key)
        dpp = round(pct - prev_pct, 3) if (pct is not None and prev_pct is not None) else None
        item = {
            "key": key, "name": g["name"],
            "idx_level": round(idx_level, 2) if idx_level else None,
            "idx_chg": idx_chg,
            "idx_amt_yi": idx_amt_yi,
            "float_mv_wanyi": round(float_mv / 1e12, 2) if float_mv else None,
            "etf_mv_yi": round(mv_sum, 0),
            "pct": round(pct, 3) if pct else None,
            "prev_pct": round(prev_pct, 3) if prev_pct is not None else None,
            "prev_date": prev_date,
            "dpp": dpp,
            "amt_yi": round(amt_sum, 0),
            "n_etf": len(detail),
            "k_date": (kcache.get(kk) or {}).get("date"),
        }
        snap["indices"].append(item)
        snap["etfs"][key] = detail

        if snap["market_open"] and dpp is not None and abs(dpp) >= FLOW_PP:
            snap["alerts"].append({
                "kind": "flow", "key": key, "name": g["name"],
                "code": "", "msg": f"占比 {dpp:+.3f}pp（昨收 {prev_pct:.3f}%）",
                "value": dpp, "mv_yi": item["etf_mv_yi"],
            })
        for d in detail:
            if d["flags"]:
                snap["alerts"].append({
                    "kind": "etf", "key": key, "name": d["name"],
                    "code": d["code"], "msg": " · ".join(d["flags"]),
                    "value": d["vol_ratio"] or d["dev"], "mv_yi": d["mv_yi"],
                })
        if pct is not None and snap["market_open"]:
            rows.append({
                "time": snap["time"], "key": key, "name": g["name"],
                "pct": item["pct"], "idx_level": item["idx_level"],
                "etf_mv_yi": item["etf_mv_yi"], "amt_yi": item["amt_yi"],
            })

    with open(SNAP_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)

    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(INTRA_FILE, mode="a", header=not os.path.exists(INTRA_FILE),
                  index=False, encoding="utf-8-sig")
    return snap


def load_snapshot(max_age=60):
    """读缓存快照；过期且盘中则重新取。"""
    snap = None
    if os.path.exists(SNAP_FILE):
        try:
            with open(SNAP_FILE, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            snap = None
    if snap and snap.get("time"):
        try:
            age = (datetime.now() - datetime.strptime(snap["time"], "%Y-%m-%d %H:%M:%S")).total_seconds()
            if age < max_age or not market_open():
                return snap
        except Exception:
            pass
    return snapshot()


if __name__ == "__main__":
    s = snapshot()
    print(f"[{s['time']}] market_open={s['market_open']}")
    for it in s["indices"]:
        print(f"  {it['name']:8s} 点位 {it['idx_level']}  占比 {it['pct']}%  "
              f"规模 {it['etf_mv_yi']:.0f} 亿  成交 {it['amt_yi']:.0f} 亿  ({it['n_etf']} 只)")
