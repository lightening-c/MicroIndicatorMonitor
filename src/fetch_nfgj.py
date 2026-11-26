# -*- coding: utf-8 -*-
"""宽基 ETF 资金跟踪：ETF 总市值 / 指数流通市值，及其斜率。

覆盖指数（config.yaml → nfgj.indices）
    沪深300 / 中证500 / 中证1000 / 科创50 / 创业板指

数据源
------
- ETF 清单      IQUANT 项目的 ETF基础信息列表.csv（按「跟踪指数代码」筛），
                落到 data/nfgj_etf_list_{key}.csv（源文件缺失时用本地副本）
- ETF 份额      上交所 https://query.sse.com.cn/commonQuery.do
                  sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L，SEC_CODE + TOT_VOL（万份）
                深交所 akshare fund_scale_daily_szse(symbol='ETF')
                缓存 data/nfgj_shares/{sse,szse}_YYYY.csv：date, code, shares（份）
- ETF 收盘/成交额 akshare fund_etf_hist_em（不复权），缓存 data/nfgj_px.csv：date, code, close, amount
- 指数点位       中证指数 https://www.csindex.com.cn/csindex-home/perf/index-perf
                创业板指（中证不覆盖）用东财 push2his 日K
- 流通市值       东财 push2 指数快照 f117(流通市值) / f43(点位×100)，标定 K（元/点）
                float_mv = 点位 × K

计算
----
  etf_mv     = Σ(份额 × 收盘价)          （元，含增强型）
  etf_mv_std = 同上，仅非增强型          （元）
  etf_amt    = Σ ETF 成交额              （亿元）
  amt_diff   = etf_amt.diff()            （亿元 → 每日放量/缩量）
  pct        = etf_mv / float_mv × 100   （%）
  d20 / d60  = pct.diff(20/60)           （pp）
  折算净申购  = d × float_mv/1e8/100     （亿元）

输出 data/nfgj_{key}.csv
"""
import json
import os
import random
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd
import requests
import yaml

from paths import ROOT_DIR, DATA_DIR, CONFIG_FILE

SHARE_DIR = os.path.join(DATA_DIR, "nfgj_shares")
PX_FILE = os.path.join(DATA_DIR, "nfgj_px.csv")
K_CACHE = os.path.join(DATA_DIR, "nfgj_k.json")
SHARES_LATEST = os.path.join(DATA_DIR, "nfgj_shares_latest.csv")
PX_META = os.path.join(DATA_DIR, "nfgj_px_meta.json")
EM_DOWN = {"v": False}   # 东财 push2his 不可用时置位，本轮直接走新浪

HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_SQL = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
RETRY = 4
WORKERS = 4


# ═══════════════════════════════════════════════════════════════
#  ETF 清单
# ═══════════════════════════════════════════════════════════════
def load_etf_list(group, source_csv):
    """返回 columns=[code,name,增强]；优先从源表重建，否则用本地副本。"""
    local = os.path.join(DATA_DIR, f"nfgj_etf_list_{group['key']}.csv")
    track = group["etf_track"]
    if source_csv and os.path.exists(source_csv):
        try:
            info = pd.read_csv(source_csv)
            info["跟踪指数代码"] = info["跟踪指数代码"].astype(str)
            c = info[info["跟踪指数代码"] == track].copy()
            if len(c) and "ETF简称" in c.columns:
                c["增强"] = c["ETF简称"].astype(str).str.contains("增强")
                out = c[["ETF代码", "ETF简称", "增强"]].rename(
                    columns={"ETF代码": "code", "ETF简称": "name"})
                out["code"] = out["code"].astype(str)
                out.to_csv(local, index=False, encoding="utf-8-sig")
                return out
        except Exception as e:
            print(f"  [WARN] 读取 ETF 源表失败: {type(e).__name__} {str(e)[:60]}")
    if os.path.exists(local):
        d = pd.read_csv(local, dtype={"code": str})
        if len(d):
            return d
    raise RuntimeError(f"缺少 {group['name']} 的 ETF 清单")


# ═══════════════════════════════════════════════════════════════
#  指数点位
# ═══════════════════════════════════════════════════════════════
def fetch_index_level_csindex(code, start, end):
    r = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                     params={"indexCode": code, "startDate": start, "endDate": end},
                     headers=HDR, timeout=40)
    rows = (r.json() or {}).get("data") or []
    if not rows:
        raise RuntimeError(f"中证指数 {code} 返回空")
    d = pd.DataFrame(rows)
    d["date"] = pd.to_datetime(d["tradeDate"]).dt.strftime("%Y%m%d")
    d["idx_level"] = pd.to_numeric(d["close"], errors="coerce")
    return d.dropna(subset=["idx_level"])[["date", "idx_level"]].drop_duplicates("date")


def _index_level_sina(secid, start, end):
    """新浪指数日线回退（东财不可用时）。"""
    market = "sh" if secid.startswith("1.") else "sz"
    code = secid.split(".")[1]
    d = ak.stock_zh_index_daily(symbol=market + code)
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y%m%d")
    d["idx_level"] = pd.to_numeric(d["close"], errors="coerce")
    d = d[(d["date"] >= start) & (d["date"] <= end)]
    return d.dropna(subset=["idx_level"])[["date", "idx_level"]].drop_duplicates("date")


def fetch_index_level_em(secid, start, end):
    try:
        return _index_level_em_raw(secid, start, end)
    except Exception as e:
        print(f"  [WARN] 东财K线 {secid} 失败({type(e).__name__})，改用新浪")
        return _index_level_sina(secid, start, end)


def _index_level_em_raw(secid, start, end):
    r = requests.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                     params={"secid": secid, "ut": "fa5fd1943c7b386f172d6893dbfba660",
                             "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f53",
                             "klt": "101", "fqt": "0", "beg": start, "end": end, "lmt": "10000"},
                     headers={**HDR, "Referer": "https://quote.eastmoney.com/"}, timeout=40)
    k = ((r.json() or {}).get("data") or {}).get("klines") or []
    if not k:
        raise RuntimeError(f"东财K线 {secid} 返回空")
    rows = [x.split(",")[:2] for x in k]
    d = pd.DataFrame(rows, columns=["date", "idx_level"])
    d["date"] = d["date"].str.replace("-", "")
    d["idx_level"] = pd.to_numeric(d["idx_level"], errors="coerce")
    return d.dropna(subset=["idx_level"]).drop_duplicates("date")


def fetch_index_level(group, start):
    end = pd.Timestamp.now().strftime("%Y%m%d")
    code = group.get("csindex_code") or ""
    if code:
        try:
            d = fetch_index_level_csindex(code, start, end)
            src = "中证指数"
        except Exception as e:
            print(f"  [WARN] 中证 {code} 失败({type(e).__name__})，改用东财K线")
            d = fetch_index_level_em(group["em_secid"], start, end)
            src = "东财K线"
    else:
        d = fetch_index_level_em(group["em_secid"], start, end)
        src = "东财K线"
    d = d.sort_values("date").reset_index(drop=True)
    print(f"  [INFO] {group['name']} 点位 {len(d)} 天（{src}），最新 "
          f"{d.iloc[-1]['date']} {d.iloc[-1]['idx_level']:.2f}")
    return d


# ═══════════════════════════════════════════════════════════════
#  流通市值标定 K（元/点）
# ═══════════════════════════════════════════════════════════════
def _save_k_cache(cache):
    try:
        with open(K_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _estimate_float_mv_by_constituents(index_code, level, batch=60):
    """东财无该指数流通市值时的回退：用成分股流通市值合计 / 点位估算 K。

    腾讯行情字段 44 = 流通市值（亿元）；中证指数按自由流通市值加权，
    与「流通市值」略有差异，属近似口径，仅在东财不可用时使用。
    """
    try:
        cons = ak.index_stock_cons_csindex(symbol=index_code)
        codes = [str(c).zfill(6) for c in cons["成分券代码"].astype(str)]
    except Exception as e:
        print(f"  [WARN] 取成分股失败 {type(e).__name__}")
        return None
    syms = [("sh" if c[0] in "569" else "sz") + c for c in codes]
    total = 0.0
    for i in range(0, len(syms), batch):
        chunk = syms[i:i + batch]
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(chunk),
                             headers={**HDR, "Referer": "https://gu.qq.com/"}, timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().splitlines():
                f = line.split('="')[-1].rstrip('";').split("~")
                if len(f) > 44:
                    try:
                        total += float(f[44])
                    except Exception:
                        pass
        except Exception:
            continue
    if total <= 0 or not level:
        return None
    return total * 1e8 / float(level)


def fetch_float_mv_factor(secid, name, fallback=None, level=None, csindex_code=None):
    """标定 K（元/点）。缓存 30 天：盘中快照的 f117 会随行情漂移，
    每次都重取会让历史占比整体缩放、污染斜率，故按周期复用。"""
    key = secid.replace(".", "_")
    cache = {}
    if os.path.exists(K_CACHE):
        try:
            with open(K_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    hit = cache.get(key) or {}
    if hit.get("k") and hit.get("date"):
        try:
            age = (pd.Timestamp.now().normalize() - pd.Timestamp(str(hit["date"]))).days
            if 0 <= age <= 30:
                print(f"  [INFO] {name} 流通市值标定 K={hit['k']:,.0f} 元/点（{hit['date']} 标定，{age} 天前）")
                return float(hit["k"])
        except Exception:
            pass

    try:
        r = requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                         params={"secid": secid, "ut": "fa5fd1943c7b386f172d6893dbfba660",
                                 "fields": "f43,f57,f58,f117"},
                         headers={**HDR, "Referer": "https://quote.eastmoney.com/"}, timeout=20)
        d = (r.json() or {}).get("data") or {}
        level = float(d.get("f43") or 0) / 100.0
        float_mv = float(d.get("f117") or 0)
        if level > 0 and float_mv > 0:
            k = float_mv / level
            cache[key] = {"k": k, "date": pd.Timestamp.now().strftime("%Y%m%d")}
            try:
                with open(K_CACHE, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
            print(f"  [INFO] {name} 流通市值标定 K={k:,.0f} 元/点（{float_mv/1e12:.2f} 万亿）")
            return k
    except Exception as e:
        print(f"  [WARN] {name} 流通市值标定失败 {type(e).__name__} {str(e)[:60]}")
    if hit.get("k"):
        print(f"  [INFO] {name} 使用历史标定 K={float(hit['k']):,.0f}")
        return float(hit["k"])
    if csindex_code and level:
        k = _estimate_float_mv_by_constituents(csindex_code, level)
        if k:
            cache[key] = {"k": k, "date": pd.Timestamp.now().strftime("%Y%m%d"),
                          "note": "按成分股流通市值估算"}
            _save_k_cache(cache)
            print(f"  [INFO] {name} 按成分股估算 K={k:,.0f} 元/点（腾讯流通市值合计）")
            return k
    if fallback:
        print(f"  [INFO] {name} 使用配置 K={float(fallback):,.0f}")
        return float(fallback)
    raise RuntimeError(f"{name} 无法获得流通市值标定")


# ═══════════════════════════════════════════════════════════════
#  ETF 份额（增量，按年缓存）
# ═══════════════════════════════════════════════════════════════
def _cached_dates(prefix):
    have = set()
    if not os.path.isdir(SHARE_DIR):
        return have
    for fp in os.listdir(SHARE_DIR):
        if fp.startswith(prefix + "_") and fp.endswith(".csv"):
            try:
                d = pd.read_csv(os.path.join(SHARE_DIR, fp), usecols=["date"], dtype={"date": str})
                have |= set(d["date"].unique())
            except Exception:
                pass
    return have


def _append_shares(prefix, df):
    if df is None or df.empty:
        return
    df = df.copy()
    df["year"] = df["date"].astype(str).str[:4]
    for y, g in df.groupby("year"):
        fp = os.path.join(SHARE_DIR, f"{prefix}_{y}.csv")
        old = (pd.read_csv(fp, dtype={"date": str, "code": str})
               if os.path.exists(fp) else pd.DataFrame(columns=["date", "code", "shares"]))
        m = pd.concat([old, g[["date", "code", "shares"]]], ignore_index=True)
        m = m.drop_duplicates(subset=["date", "code"], keep="last").sort_values(["date", "code"])
        m.to_csv(fp, index=False, encoding="utf-8-sig")


def _sse_day(ds):
    params = {
        "isPagination": "true", "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
        "sqlId": SSE_SQL, "STAT_DATE": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}",
    }
    for k in range(RETRY):
        try:
            r = requests.get(SSE_URL, params=params,
                             headers={**HDR, "Referer": "https://www.sse.com.cn/"}, timeout=25)
            rows = (r.json() or {}).get("result") or []
            if not rows:
                return pd.DataFrame(columns=["date", "code", "shares"])
            d = pd.DataFrame(rows)
            return pd.DataFrame({
                "date": ds,
                "code": d["SEC_CODE"].astype(str).str.zfill(6) + ".SH",
                "shares": pd.to_numeric(d["TOT_VOL"], errors="coerce") * 10000.0,
            }).dropna(subset=["shares"])
        except Exception:
            time.sleep(1.2 * (k + 1) + random.random())
    return None


def fetch_sse_shares(dates):
    have = _cached_dates("sse")
    todo = [d for d in dates if d not in have]
    if not todo:
        print("  [INFO] 上交所份额已是最新")
        return []
    print(f"  [INFO] 上交所份额待抓 {len(todo)} 天")
    fails, buf, done = [], [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_sse_day, d): d for d in todo}
        for fut in as_completed(futs):
            try:
                df = fut.result()
            except Exception:
                df = None
            if df is None:
                fails.append(futs[fut])
            else:
                buf.append(df)
            done += 1
            if done % 60 == 0 or done == len(todo):
                _append_shares("sse", pd.concat(buf, ignore_index=True) if buf else None)
                buf = []
    return fails


def _fetch_szse_via_curl(start_date, end_date):
    """深交所官网 HTTPS 对 Python 的 SSL 握手经常失败（UNEXPECTED_EOF），
    但 curl（走系统 SSL 库）能正常访问。用 curl 子进程下载绕开该问题，
    返回与 akshare fund_scale_daily_szse 兼容的 DataFrame（日期/基金代码/基金份额）。"""
    import subprocess, tempfile
    # 深交所接口要求 YYYY-MM-DD 格式（不带横杠会返回空表）
    s = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    e = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
    url = ("https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=scsj_fund_jjgm"
           f"&TABKEY=tab1&txtStart={s}&txtEnd={e}&jjlb=ETF&random={random.random()}")
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        curl = shutil.which("curl") or shutil.which("curl.exe") or "curl"
        cmd = [curl, "-sS", "-m", "30", "-o", path,
               "-A", HDR["User-Agent"],
               "-H", "Referer: https://www.szse.cn/market/fund/volume/etf/index.html",
               url]
        subprocess.run(cmd, check=False, capture_output=True, timeout=45)
        if not os.path.exists(path) or os.path.getsize(path) < 500:
            return None
        df = pd.read_excel(path, engine="openpyxl")
        if df is None or df.empty:
            return None
        # 兼容列名：原始列为 日期/基金代码/基金简称/基金规模(亿份)
        for c in df.columns:
            if "规模" in str(c) or "份额" in str(c):
                df = df.rename(columns={c: "基金份额"})
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        date_col = next((c for c in df.columns if "日期" in str(c)), None)
        if code_col is None or date_col is None or "基金份额" not in df.columns:
            return None
        df["基金份额"] = pd.to_numeric(
            df["基金份额"].astype(str).str.replace(",", "", regex=False), errors="coerce")
        df["_code"] = pd.to_numeric(df[code_col], errors="coerce")
        df["_date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
        df = df.dropna(subset=["_code", "_date"]).copy()
        out = pd.DataFrame({
            "日期": df["_date"],
            "基金代码": df["_code"].astype(int).astype(str).str.zfill(6),
            "基金份额": df["基金份额"],
        })
        return out.dropna(subset=["基金份额"])
    except Exception as e:
        print(f"  [WARN] curl 下载深交所份额失败: {type(e).__name__}: {e}")
        return None
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


def fetch_szse_shares(dates):
    have = _cached_dates("szse")
    todo = [d for d in dates if d not in have]
    if not todo:
        print("  [INFO] 深交所份额已是最新")
        return []
    ts = pd.to_datetime(pd.Series(todo), format="%Y%m%d").sort_values()
    chunks = []
    for _, g in ts.groupby(ts.dt.to_period("Q")):
        s, e = g.iloc[0], g.iloc[-1]
        while s <= e:
            ee = min(s + pd.DateOffset(months=5), e)
            chunks.append((s, ee))
            s = ee + pd.Timedelta(days=1)
    print(f"  [INFO] 深交所份额待抓 {len(todo)} 天 / {len(chunks)} 块")
    fails = []
    for i, (s, e) in enumerate(chunks, 1):
        for k in range(3):
            df = None
            try:
                df = ak.fund_scale_daily_szse(start_date=s.strftime("%Y%m%d"),
                                              end_date=e.strftime("%Y%m%d"), symbol="ETF")
            except Exception:
                # 深交所 HTTPS 对 Python SSL 常失败，fallback 到 curl
                df = _fetch_szse_via_curl(s.strftime("%Y%m%d"), e.strftime("%Y%m%d"))
            if df is not None and len(df):
                _append_shares("szse", pd.DataFrame({
                    "date": pd.to_datetime(df["日期"]).dt.strftime("%Y%m%d"),
                    "code": df["基金代码"].astype(str).str.zfill(6) + ".SZ",
                    "shares": pd.to_numeric(df["基金份额"], errors="coerce"),
                }).dropna(subset=["shares"]))
                break
            time.sleep(2 * (k + 1))
        else:
            fails.append((str(s.date()), str(e.date())))
        time.sleep(0.3)
    return fails


def load_shares(codes):
    frames = []
    if os.path.isdir(SHARE_DIR):
        for fp in sorted(os.listdir(SHARE_DIR)):
            if not fp.endswith(".csv"):
                continue
            try:
                d = pd.read_csv(os.path.join(SHARE_DIR, fp), dtype={"date": str, "code": str})
                if len(d):
                    frames.append(d[d["code"].isin(codes)])
            except Exception:
                pass
    if not frames:
        return pd.DataFrame(columns=["date", "code", "shares"])
    sh = pd.concat(frames, ignore_index=True)
    sh["shares"] = pd.to_numeric(sh["shares"], errors="coerce")
    return sh.drop_duplicates(["date", "code"]).dropna(subset=["shares"])


# ═══════════════════════════════════════════════════════════════
#  ETF 价格（akshare，增量）
# ═══════════════════════════════════════════════════════════════
def _load_px_meta():
    if os.path.exists(PX_META):
        try:
            with open(PX_META, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _save_px_meta(meta):
    try:
        with open(PX_META, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def fetch_prices(codes, start_default="20180101"):
    """增量取价：新代码取全段；已有缓存补尾部缺口；头部回补只尝试一次。

    上市晚于 start_default 的代码，回补必然为空且耗时（代理不稳时更糟），
    因此把「最早可取日期」记进 data/nfgj_px_meta.json，之后不再重复尝试。
    """
    end = pd.Timestamp.now().strftime("%Y%m%d")
    old = (pd.read_csv(PX_FILE, dtype={"date": str, "code": str})
           if os.path.exists(PX_FILE) else pd.DataFrame(columns=["date", "code", "close", "amount"]))
    meta = _load_px_meta()

    def _fetch_sina(sym, ex, s_, e_):
        try:
            d = ak.fund_etf_hist_sina(symbol=("sh" if ex.upper() == "SH" else "sz") + sym)
            d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y%m%d")
            d = d[(d["date"] >= s_) & (d["date"] <= e_)]
            if not len(d):
                return None
            return d.rename(columns={"close": "close"})[["date", "close", "amount"]]
        except Exception:
            return None

    def _fetch(sym, s_, e_, retries=3, ex="SH"):
        # 东财一旦失败即全量切换到新浪，避免每只 ETF 都空耗重试
        if not EM_DOWN["v"]:
            try:
                d = ak.fund_etf_hist_em(symbol=sym, period="daily", start_date=s_,
                                        end_date=e_, adjust="")
                if d is not None and len(d):
                    d = d.rename(columns={"日期": "date", "收盘": "close", "成交额": "amount"})
                    return d[["date", "close", "amount"]].assign(
                        date=lambda x: pd.to_datetime(x["date"]).dt.strftime("%Y%m%d"))
                return None
            except Exception as exc:
                EM_DOWN["v"] = True
                print(f"  [WARN] 东财取价不可用（{type(exc).__name__}），本轮回退新浪")
        alt = _fetch_sina(sym, ex, s_, e_)
        if alt is None:
            print(f"  [WARN] {sym} {s_}-{e_} 价格缺失（东财/新浪均失败）")
        return alt

    new_rows, n_new, meta_changed = [], 0, False
    for i, code in enumerate(sorted(codes), 1):
        sym = code.split(".")[0]
        ex = code.split(".")[1] if "." in code else "SH"
        sub = old[old["code"] == code]
        if sub.empty:
            d = _fetch(sym, start_default, end, ex=ex)
            if d is not None and len(d):
                d["code"] = code
                new_rows.append(d)
                n_new += 1
        else:
            lo, hi = sub["date"].min(), sub["date"].max()
            nxt = (pd.to_datetime(hi) + pd.Timedelta(days=1)).strftime("%Y%m%d")
            if nxt <= end:
                d = _fetch(sym, nxt, end, ex=ex)
                if d is not None and len(d):
                    d["code"] = code
                    new_rows.append(d)
                    n_new += 1
            known = (meta.get(code) or {}).get("earliest")
            if lo > start_default and not (known and known <= lo):
                prev = (pd.to_datetime(lo) - pd.Timedelta(days=1)).strftime("%Y%m%d")
                d = _fetch(sym, start_default, prev, retries=1, ex=ex)
                if d is not None and len(d):
                    d["code"] = code
                    new_rows.append(d)
                    n_new += 1
                else:
                    meta[code] = {"earliest": lo}
                    meta_changed = True
        time.sleep(0.12)
        if n_new and n_new % 20 == 0:
            print(f"  [PX] 已更新 {n_new} 段（{i}/{len(codes)}）")

    if meta_changed:
        _save_px_meta(meta)
    if new_rows:
        px = pd.concat([old] + new_rows, ignore_index=True)
        px = px.drop_duplicates(subset=["date", "code"], keep="last").sort_values(["date", "code"])
        px.to_csv(PX_FILE, index=False, encoding="utf-8-sig")
        print(f"  [INFO] 价格缓存更新：{len(px)} 行（新增 {n_new} 段）")
        return px
    print(f"  [INFO] 价格已是最新：{len(old)} 行")
    return old


# ═══════════════════════════════════════════════════════════════
#  合成
# ═══════════════════════════════════════════════════════════════
def build_ratio(name, idx, px, sh, etf_list, K):
    etf_list = etf_list.copy()
    etf_list["code"] = etf_list["code"].astype(str)
    codes = set(etf_list["code"])
    enh_flag = etf_list["增强"].astype(str).str.lower()
    enh = set(etf_list.loc[enh_flag.isin(["true", "1", "是"]), "code"])

    p = px[px["code"].isin(codes)].copy()
    p["close"] = pd.to_numeric(p["close"], errors="coerce")
    p["amount"] = pd.to_numeric(p["amount"], errors="coerce")

    m = sh[sh["code"].isin(codes)].merge(p, on=["date", "code"], how="left")
    m["mv"] = m["shares"] * m["close"]

    tot = m.groupby("date").agg(etf_mv=("mv", "sum")).reset_index()
    tot_std = m[~m["code"].isin(enh)].groupby("date").agg(etf_mv_std=("mv", "sum")).reset_index()
    amt = p.groupby("date").agg(etf_amt=("amount", "sum")).reset_index()
    amt["etf_amt"] = amt["etf_amt"] / 1e8

    df = (tot.merge(tot_std, on="date", how="outer")
             .merge(amt, on="date", how="outer")
             .merge(idx, on="date", how="inner")
             .sort_values("date").reset_index(drop=True))
    df["amt_diff"] = df["etf_amt"].diff()
    df["float_mv"] = df["idx_level"] * K
    df["pct"] = df["etf_mv"] / df["float_mv"] * 100
    df["pct_std"] = df["etf_mv_std"] / df["float_mv"] * 100
    df["etf_mv_yi"] = df["etf_mv"] / 1e8
    # 合计份额（亿份），用于事后拆分「份额申购」与「价格上涨」
    sh_tot = m.groupby("date")["shares"].sum() / 1e8
    df = df.merge(sh_tot.rename("shares_yi").reset_index(), on="date", how="left")
    df["index_name"] = name
    df["n_etf"] = len(codes)
    return df.dropna(subset=["pct"])


def fetch_all():
    os.makedirs(SHARE_DIR, exist_ok=True)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = (yaml.safe_load(f) or {}).get("nfgj", {}) or {}
    groups = cfg.get("indices") or []
    start = str(cfg.get("start_date", "20201215"))
    src = cfg.get("etf_list_source")
    if src and not os.path.isabs(src):
        src = os.path.join(ROOT_DIR, src)

    print("=" * 50)
    print("  [宽基 ETF 资金跟踪]")
    print("=" * 50)

    lists = {g["key"]: load_etf_list(g, src) for g in groups}
    all_codes = set()
    for d in lists.values():
        all_codes |= set(d["code"].astype(str))
    print(f"  [INFO] 共 {len(groups)} 个指数，{len(all_codes)} 只 ETF")

    # 交易日历用沪深300点位（覆盖面全）
    base = groups[0]
    idx0 = fetch_index_level(base, start)
    dates = [str(d) for d in idx0["date"].tolist()]

    sse_fail = fetch_sse_shares(dates)
    szse_fail = fetch_szse_shares(dates)
    if sse_fail:
        print(f"  [WARN] 上交所缺失 {len(sse_fail)} 天（缓存补齐）")
    if szse_fail:
        print(f"  [WARN] 深交所缺失 {len(szse_fail)} 块（缓存补齐）")

    sh = load_shares(all_codes)
    print(f"  [INFO] 份额缓存 {len(sh)} 行，{sh['code'].nunique()} 只")

    # 每只 ETF 最新一日份额，供盘中实时快照估算规模用
    latest_sh = (sh.sort_values("date").groupby("code").tail(1)
                   [["code", "shares", "date"]].sort_values("code"))
    latest_sh.to_csv(SHARES_LATEST, index=False, encoding="utf-8-sig")
    print(f"  [INFO] 最新份额索引 {len(latest_sh)} 只（截至 {latest_sh['date'].max()}）")

    px = fetch_prices(all_codes)
    # 价格只到最新交易日
    px = px[px["date"] <= dates[-1]]

    summaries = []
    for g in groups:
        print(f"\n  --- {g['name']} ---")
        idx = idx0 if g["key"] == base["key"] else fetch_index_level(g, start)
        K = fetch_float_mv_factor(g["em_secid"], g["name"],
                                  level=float(idx["idx_level"].iloc[-1]),
                                  csindex_code=g.get("csindex_code") or None)
        df = build_ratio(g["name"], idx, px, sh, lists[g["key"]], K)
        if df.empty:
            print(f"  [WARN] {g['name']} 结果为空，跳过")
            continue
        df["date"] = df["date"].astype(str)
        out = os.path.join(DATA_DIR, f"nfgj_{g['key']}.csv")
        df.to_csv(out, index=False, encoding="utf-8-sig")

        last = df.iloc[-1]
        conv = last["float_mv"] / 1e8 / 100
        d20 = df["pct"].diff(20).iloc[-1]
        d60 = df["pct"].diff(60).iloc[-1]
        summaries.append((g["name"], last, d20, d60, conv))
        print(f"  [OK] nfgj_{g['key']}.csv  {len(df)} 天 | {last['date']} 占比 {last['pct']:.3f}% "
              f"规模 {last['etf_mv_yi']:.0f} 亿 | d20 {d20:+.3f}pp d60 {d60:+.3f}pp")

    print("\n" + "=" * 50)
    for name, last, d20, d60, conv in summaries:
        print(f"  {name:8s} 占比 {last['pct']:.2f}%  规模 {last['etf_mv_yi']:>6.0f} 亿  "
              f"20日 {d20*conv:>+7.0f} 亿  60日 {d60*conv:>+7.0f} 亿")
    print("=" * 50)
    return len(summaries)


if __name__ == "__main__":
    fetch_all()
