"""
微观指标跟踪 — 数据采集脚本
从 AKShare + 东方财富 API 拉取各指标数据，存储为 CSV 文件
"""
import os
import sys
import traceback
import yaml
import json
import time
import pandas as pd
import numpy as np
import akshare as ak
import requests
from datetime import datetime, timedelta

from paths import DATA_DIR, CONFIG_FILE

# ── 申万一级行业代码映射（31个行业）──
SW_INDUSTRY_CODES = {
    "sw_food_beverage":    "801120",  # 食品饮料
    "sw_bank":             "801780",  # 银行
    "sw_nonbank_finance":  "801790",  # 非银金融
    "sw_pharma":           "801150",  # 医药生物
    "sw_electronics":      "801080",  # 电子
    "sw_computer":         "801750",  # 计算机
    "sw_power_equip":      "801730",  # 电力设备
    "sw_auto":             "801880",  # 汽车
    "sw_chemical":         "801030",  # 基础化工
    "sw_media":            "801760",  # 传媒
    "sw_machinery":        "801890",  # 机械设备
    "sw_telecom":          "801770",  # 通信
    "sw_real_estate":      "801180",  # 房地产
    "sw_nonferrous_metal": "801050",  # 有色金属
    "sw_national_defense": "801740",  # 国防军工
    "sw_build_decor":      "801720",  # 建筑装饰
    "sw_transport":        "801170",  # 交通运输
    "sw_household_appl":   "801110",  # 家用电器
    "sw_agriculture":      "801010",  # 农林牧渔
    "sw_utilities":        "801160",  # 公用事业
    "sw_light_manufacture": "801140", # 轻工制造
    "sw_textile_apparel":  "801130",  # 纺织服饰
    "sw_build_material":   "801710",  # 建筑材料
    "sw_coal":             "801950",  # 煤炭
    "sw_steel":            "801040",  # 钢铁
    "sw_petrochemical":    "801960",  # 石油石化
    "sw_social_service":   "801210",  # 社会服务
    "sw_commerce_retail":  "801200",  # 商贸零售
    "sw_environment":      "801970",  # 环保
    "sw_beauty_care":      "801980",  # 美容护理
    "sw_comprehensive":    "801230",  # 综合
}

SW_NAMES = {
    "sw_food_beverage": "食品饮料", "sw_bank": "银行",
    "sw_nonbank_finance": "非银金融", "sw_pharma": "医药生物",
    "sw_electronics": "电子", "sw_computer": "计算机",
    "sw_power_equip": "电力设备", "sw_auto": "汽车",
    "sw_chemical": "基础化工", "sw_media": "传媒",
    "sw_machinery": "机械设备", "sw_telecom": "通信",
    "sw_real_estate": "房地产", "sw_nonferrous_metal": "有色金属",
    "sw_national_defense": "国防军工", "sw_build_decor": "建筑装饰",
    "sw_transport": "交通运输", "sw_household_appl": "家用电器",
    "sw_agriculture": "农林牧渔", "sw_utilities": "公用事业",
    "sw_light_manufacture": "轻工制造", "sw_textile_apparel": "纺织服饰",
    "sw_build_material": "建筑材料", "sw_coal": "煤炭",
    "sw_steel": "钢铁", "sw_petrochemical": "石油石化",
    "sw_social_service": "社会服务", "sw_commerce_retail": "商贸零售",
    "sw_environment": "环保", "sw_beauty_care": "美容护理",
    "sw_comprehensive": "综合",
}


def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_csv(df, indicator_id):
    """保存 DataFrame 为 CSV，按日期排序去重"""
    if df is None or df.empty:
        print(f"  [WARN] 数据为空，跳过保存 {indicator_id}")
        return False

    # 去重并按日期排序
    date_col = None
    for col in df.columns:
        if any(kw in str(col) for kw in ["日期", "时间", "date"]):
            date_col = col
            break
    if date_col is None:
        date_col = df.columns[0]

    try:
        df = df.drop_duplicates(subset=[date_col], keep="last")
        df = df.sort_values(date_col)
    except Exception:
        pass

    filename = f"{indicator_id}.csv"
    filepath = os.path.join(DATA_DIR, filename)
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    print(f"  [OK] 保存 {filename} ({len(df)} 条记录)")
    return True


# ═══════════════════════════════════════════════════════════════
#  指数日线数据获取（akshare stock_zh_index_daily）
# ═══════════════════════════════════════════════════════════════
def fetch_index_daily(symbol, ind_name):
    """获取指数日线数据"""
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df is None or df.empty:
            print(f"  [WARN] {ind_name} 返回空数据")
            return None
        date_col = "date" if "date" in df.columns else df.columns[0]
        if "close" not in df.columns:
            print(f"  [WARN] 未找到 close 列，可用列: {list(df.columns)}")
            return df
        # 保留 date, open, close, high, low, volume
        cols = [date_col]
        for c in ["close", "open", "high", "low", "volume"]:
            if c in df.columns:
                cols.append(c)
        result = df[cols].copy()
        result.columns = ["日期"] + [c for c in cols[1:]]
        print(f"  [INFO] 获取 {ind_name} 共 {len(result)} 条日线数据")
        return result
    except Exception as e:
        print(f"  [ERR] {ind_name} 获取失败: {e}")
        traceback.print_exc()
        return None


# ═══════════════════════════════════════════════════════════════
#  申万一级行业指数（通过东方财富 push2 API）
#  使用 clist/get 接口批量获取每日行情
# ═══════════════════════════════════════════════════════════════
def fetch_sw_industry_index(sw_code, ind_id, ind_name):
    """通过东方财富 get 接口获取申万行业指数日线数据"""
    # 申万行业指数的 secid 格式: 1b{code} (上证) 或 0b{code} (深证)
    # 一般申万一级行业指数代码以 1b 开头
    secid = f"1b{sw_code}"

    try:
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfba660",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",  # 日线
            "fqt": "1",    # 前复权
            "end": "20500101",
            "lmt": "10000",
        }
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
        if data.get("data") and data["data"].get("klines"):
            klines = data["data"]["klines"]
            records = []
            for k in klines:
                parts = k.split(",")
                records.append({
                    "日期": parts[0],
                    "开盘": float(parts[1]),
                    "收盘": float(parts[2]),
                    "最高": float(parts[3]),
                    "最低": float(parts[4]),
                    "成交量": float(parts[5]),
                    "成交额": float(parts[6]),
                    "振幅": float(parts[7]) if len(parts) > 7 else None,
                    "涨跌幅": float(parts[8]) if len(parts) > 8 else None,
                    "涨跌额": float(parts[9]) if len(parts) > 9 else None,
                    "换手率": float(parts[10]) if len(parts) > 10 else None,
                })
            df = pd.DataFrame(records)
            print(f"  [INFO] 获取 {ind_name} 共 {len(df)} 条日线数据")
            return df
    except Exception as e:
        print(f"  [WARN] push2his 获取 {ind_name} 失败: {e}, 尝试 akshare 回退")

    # 回退：使用 akshare index_hist_sw
    try:
        df = ak.index_hist_sw(symbol=sw_code)
        if df is not None and not df.empty:
            # akshare 返回列: 代码, 日期, 收盘, 开盘, 最高, 最低, 成交量, 成交额
            # 已经包含标准列名，只需确保列名正确
            col_map = {}
            for col in df.columns:
                col_s = str(col)
                if "日期" in col_s:
                    col_map[col] = "日期"
                elif "收盘" in col_s:
                    col_map[col] = "收盘"
                elif "开盘" in col_s:
                    col_map[col] = "开盘"
                elif "最高" in col_s:
                    col_map[col] = "最高"
                elif "最低" in col_s:
                    col_map[col] = "最低"
                elif "成交" in col_s and "量" in col_s:
                    col_map[col] = "成交量"
                elif "成交" in col_s and "额" in col_s:
                    col_map[col] = "成交额"
            if col_map:
                # 只保留需要的列
                keep_cols = {k: v for k, v in col_map.items()}
                result_cols = []
                data_cols = {}
                for orig, new in col_map.items():
                    data_cols[new] = df[orig]
                    result_cols.append(new)
                df = pd.DataFrame(data_cols)
            print(f"  [INFO] (akshare) 获取 {ind_name} 共 {len(df)} 条数据")
            return df
    except Exception as e2:
        print(f"  [ERR] akshare 回退也失败 ({e2})")

    return None


# ═══════════════════════════════════════════════════════════════
#  市场宽度指标（涨跌停板、涨跌家数）
#  通过东方财富 push2 clist/get 接口拉取全市场股票
# ═══════════════════════════════════════════════════════════════
def _breadth_em():
    """备用：东财 clist（常被限流，返回可能不完整）。"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "10000", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
        "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f12,f14", "_": str(int(time.time() * 1000)),
    }
    r = requests.get(url, params=params, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    data = (r.json() or {}).get("data") or {}
    rows = data.get("diff") or []
    if not rows:
        return None
    chg = pd.to_numeric(pd.Series([x.get("f3") for x in rows]), errors="coerce")
    name = pd.Series([str(x.get("f14") or "") for x in rows])
    code = pd.Series([str(x.get("f12") or "") for x in rows])
    return chg, name, code


def fetch_market_breadth():
    """市场宽度：涨跌家数 + 涨跌停数。主源新浪快照，备用东财；非交易日跳过。"""
    print("\n>> 采集: 市场宽度数据")
    now = datetime.now()
    # 快照在周末仍保留上一交易日收盘状态，日期取最近交易日（以上证指数为准）
    trade_date = now.strftime("%Y-%m-%d")
    try:
        _idx = pd.read_csv(os.path.join(DATA_DIR, "sh_index.csv"), usecols=["日期"])
        trade_date = str(_idx["日期"].dropna().astype(str).iloc[-1])
    except Exception:
        if now.weekday() >= 5:
            print("  [SKIP] 周末且无法确认交易日，跳过市场宽度")
            return None

    chg = name = code = None
    try:
        d = ak.stock_zh_a_spot()
        code = d["代码"].astype(str)
        name = d["名称"].astype(str)
        chg = pd.to_numeric(d["涨跌幅"], errors="coerce")
        if chg.notna().sum() < 1000:
            raise RuntimeError("新浪样本不足")
        src = "新浪"
    except Exception as e:
        print(f"  [WARN] 新浪快照失败 {type(e).__name__}，改用东财")
        try:
            got = _breadth_em()
            if got:
                chg, name, code = got
                src = "东财"
        except Exception as e2:
            print(f"  [ERR] 东财也失败 {type(e2).__name__}")
    if chg is None:
        return None

    valid = chg.notna()
    up = int((chg[valid] > 0).sum())
    down = int((chg[valid] < 0).sum())
    flat = int((chg[valid] == 0).sum())
    if up + down < 1000:
        print(f"  [WARN] 有效样本仅 {up + down} 只，数据不完整，跳过写入")
        return None

    # 各板涨跌停阈值：主板 10%、创业/科创 20%、北交所 30%，ST 5%
    lim = pd.Series(9.8, index=chg.index)
    lim[code.str.startswith(("300", "301", "688"))] = 19.8
    lim[code.str.startswith(("8", "4", "92"))] = 29.8
    lim[name.str.contains("ST", na=False)] = 4.8
    limit_up = int((chg >= lim - 0.15).sum())
    limit_down = int((chg <= -(lim - 0.15)).sum())

    today = trade_date
    print(f"  [INFO] 全市场 {int(valid.sum())} 只（{src}），涨{up} 跌{down} 平{flat}，"
          f"涨停{limit_up} 跌停{limit_down}")
    return {
        "limit_stats": pd.DataFrame([{"日期": today, "涨停数量": limit_up, "跌停数量": limit_down}]),
        "advance_decline": pd.DataFrame([{
            "日期": today, "上涨家数": up, "下跌家数": down, "平盘家数": flat,
            "涨跌比": round(up / max(down, 1), 4) if down > 0 else None,
        }]),
    }


# ═══════════════════════════════════════════════════════════════
#  市场拥挤度 — 全市场成交额前5%个股的成交占比
#  使用 akshare stock_zh_a_spot 获取当日全A股成交额数据
#  每次运行保存当日数据到 market_crowding.csv
# ═══════════════════════════════════════════════════════════════
def _bse50_ratio(default=0.34):
    """北证50 成分成交额 / 北交所全市场成交额（用新浪快照标定，按日缓存）。"""
    import json as _json
    cache = os.path.join(DATA_DIR, "bse50_ratio.json")
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    if os.path.exists(cache):
        try:
            c = _json.load(open(cache, encoding="utf-8"))
            if c.get("date") == today and c.get("ratio"):
                return float(c["ratio"])
        except Exception:
            pass
    try:
        cons = ak.index_stock_cons_csindex(symbol="899050")
        codes = set(cons["成分券代码"].astype(str).str.zfill(6))
        d = ak.stock_zh_a_spot()
        d["代码"] = d["代码"].astype(str)
        amt = pd.to_numeric(d["成交额"], errors="coerce")
        is_bj = d["代码"].str.startswith("b")
        total = float(amt[is_bj].sum())
        sub = d[is_bj & d["代码"].str[-6:].isin(codes)]
        part = float(pd.to_numeric(sub["成交额"], errors="coerce").sum())
        if total > 0 and part > 0:
            r = round(part / total, 4)
            try:
                _json.dump({"date": today, "ratio": r}, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            print(f"  [INFO] 北证50 成分占北交所成交额 {r*100:.1f}%")
            return r
    except Exception as e:
        print(f"  [WARN] 北证50 占比标定失败 {type(e).__name__}")
    return default


CAP_CALIB = os.path.join(DATA_DIR, "cap_calib.json")


def _tencent_float_mv(codes, batch=60):
    """腾讯行情流通市值（亿元）合计：codes 为 sh/sz/bj 前缀代码。"""
    import requests
    total, n = 0.0, 0
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(chunk),
                             headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                             timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().splitlines():
                f = line.split('="')[-1].rstrip('";').split("~")
                if len(f) > 44:
                    try:
                        total += float(f[44])
                        n += 1
                    except Exception:
                        pass
        except Exception:
            continue
    return total, n


def _calibrate_cap():
    """标定：全市场流通市值/中证全指点位、北交所市值/北证50点位、北证50市值占比。"""
    import json as _json
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    if os.path.exists(CAP_CALIB):
        try:
            c = _json.load(open(CAP_CALIB, encoding="utf-8"))
            if c.get("date") == today:
                return c
        except Exception:
            pass
    try:
        d = ak.stock_zh_a_spot()
        d["代码"] = d["代码"].astype(str)
        allc = [c for c in d["代码"].tolist() if c[:2] in ("sh", "sz", "bj")]
        bjc = [c for c in allc if c[:2] == "bj"]
        shc = [c for c in allc if c[:2] != "bj"]
        mv_all, _ = _tencent_float_mv(shc)
        mv_bj, _ = _tencent_float_mv(bjc)
        cons = ak.index_stock_cons_csindex(symbol="899050")
        bj50_codes = set("bj" + str(c).zfill(6) for c in cons["成分券代码"])
        mv_bj50, _ = _tencent_float_mv([c for c in bjc if c in bj50_codes])

        def _close(code):
            r = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                             params={"indexCode": code, "startDate": (pd.Timestamp.now() - pd.Timedelta(days=10)).strftime("%Y%m%d"),
                                     "endDate": pd.Timestamp.now().strftime("%Y%m%d")},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            rows = (r.json() or {}).get("data") or []
            return float(rows[-1]["close"]) if rows else None

        lv985, lvbj = _close("000985"), _close("899050")
        if not lv985 or not lvbj or mv_all <= 0:
            return None
        calib = {"date": today,
                 "k_all": mv_all * 1e8 / lv985,
                 "k_bj": mv_bj * 1e8 / lvbj,
                 "r50_mv": (mv_bj50 / mv_bj) if mv_bj else 0.34}
        _json.dump(calib, open(CAP_CALIB, "w", encoding="utf-8"))
        print(f"  [INFO] 市值标定：全市场 {mv_all/1e4:.1f} 万亿，北交所 {mv_bj:.0f} 亿，"
              f"北证50 占北交所市值 {calib['r50_mv']*100:.1f}%")
        return calib
    except Exception as e:
        print(f"  [WARN] 市值标定失败 {type(e).__name__} {str(e)[:60]}")
        return None


def fetch_cap_structure(start="2018-01-01"):
    """市值结构时序：各指数流通市值 ÷ 全市场流通市值（点位 × 标定系数）。"""
    import json as _json
    calib = _calibrate_cap()
    if not calib:
        return None
    kfile = os.path.join(DATA_DIR, "nfgj_k.json")
    K = {}
    try:
        K = _json.load(open(kfile, encoding="utf-8"))
    except Exception:
        pass

    def _k(key):
        return (K.get(key) or {}).get("k")

    parts = {
        "沪深300": ("000300", _k("1_000300")),
        "中证500": ("000905", _k("1_000905")),
        "中证1000": ("000852", _k("1_000852")),
        "中证2000": ("932000", _k("2_932000")),
    }

    def _series(code):
        r = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                         params={"indexCode": code, "startDate": start,
                                 "endDate": pd.Timestamp.now().strftime("%Y%m%d")},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
        rows = (r.json() or {}).get("data") or []
        d = pd.DataFrame(rows)
        if d.empty:
            return None
        d["日期"] = pd.to_datetime(d["tradeDate"].astype(str), format="%Y%m%d",
                                   errors="coerce").dt.strftime("%Y-%m-%d")
        return d[["日期", "close"]].dropna()

    df = _series("000985")
    if df is None:
        return None
    for nm, (code, k) in parts.items():
        s2 = _series(code)
        if s2 is None or not k:
            return None
        df = df.merge(s2.rename(columns={"close": f"{nm}_lv"}), on="日期", how="inner")

    bj = _series("899050")
    if bj is not None:
        df = df.merge(bj.rename(columns={"close": "北交所_lv"}), on="日期", how="left")

    total_all = df["close"] * calib["k_all"]
    covered = sum(df[f"{nm}_lv"] * parts[nm][1] for nm in parts)
    df["其他_mv"] = total_all - covered
    for nm in parts:
        df[f"{nm}_mv"] = df[f"{nm}_lv"] * parts[nm][1]

    if "北交所_lv" in df.columns:
        df["北交所_mv"] = df["北交所_lv"] * calib["k_bj"]
        df["北证50_mv"] = df["北交所_mv"] * calib["r50_mv"]
    else:
        df["北交所_mv"] = 0.0
        df["北证50_mv"] = 0.0

    total = total_all + df["北交所_mv"]
    out = pd.DataFrame({"日期": df["日期"]})
    for nm in parts:
        out[f"{nm}市值占比"] = (df[f"{nm}_mv"] / total * 100).round(2)
    out["其他市值占比"] = (df["其他_mv"] / total * 100).round(2)
    out["北证50市值占比"] = (df["北证50_mv"] / total * 100).round(2)
    out["北交所其他市值占比"] = ((df["北交所_mv"] - df["北证50_mv"]) / total * 100).round(2)
    out["沪深300市值"] = (df["沪深300_mv"] / 1e8).round(0)
    out["中证500市值"] = (df["中证500_mv"] / 1e8).round(0)
    out["中证1000市值"] = (df["中证1000_mv"] / 1e8).round(0)
    out["中证2000市值"] = (df["中证2000_mv"] / 1e8).round(0)
    out["其他市值"] = (df["其他_mv"] / 1e8).round(0)
    out["北证50市值"] = (df["北证50_mv"] / 1e8).round(0)
    out["北交所其他市值"] = ((df["北交所_mv"] - df["北证50_mv"]) / 1e8).round(0)
    out = out.dropna().sort_values("日期").reset_index(drop=True)
    last = out.iloc[-1]
    print(f"  [OK] 市值结构 {len(out)} 天（{out['日期'].iloc[0]} ~ {out['日期'].iloc[-1]}），"
          f"最新 沪深300 {last['沪深300市值占比']}% 中证500 {last['中证500市值占比']}% "
          f"1000 {last['中证1000市值占比']}% 2000 {last['中证2000市值占比']}%")
    return out


def fetch_crowding_proxy(start="2018-01-01"):
    """小盘 / 小微盘成交占比（长历史代理）。

    小盘   = 中证1000 成交额 ÷ 中证全指成交额
    小微盘 = 中证2000 成交额 ÷ 中证全指成交额
    成交金额来自中证指数官方接口，可按区间批量取，故能回补多年历史。
    """
    import requests

    def _amt(code):
        r = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                         params={"indexCode": code, "startDate": start,
                                 "endDate": pd.Timestamp.now().strftime("%Y%m%d")},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
        rows = (r.json() or {}).get("data") or []
        if not rows:
            return None
        d = pd.DataFrame(rows)
        d["日期"] = pd.to_datetime(d["tradeDate"].astype(str), format="%Y%m%d",
                                   errors="coerce").dt.strftime("%Y-%m-%d")
        d["amt"] = pd.to_numeric(d["tradingValue"], errors="coerce")
        return d[["日期", "amt"]].dropna()

    zz1000 = _amt("000852")     # 小盘
    zz2000 = _amt("932000")     # 小微盘
    zz800 = _amt("000906")      # 中证800（大+中盘）
    hs300 = _amt("000300")      # 沪深300（大盘）
    bj50 = _amt("899050")       # 北交所（该字段为中证口径的北交所全市场成交额）
    zz500 = _amt("000905")      # 中证500（中盘）
    allA = _amt("000985")       # 中证全指
    if allA is None or zz2000 is None:
        return None

    m = allA.rename(columns={"amt": "全A成交额"})
    if zz1000 is not None:
        m = m.merge(zz1000.rename(columns={"amt": "小盘成交额"}), on="日期", how="inner")
    m = m.merge(zz2000.rename(columns={"amt": "小微盘成交额"}), on="日期", how="inner")
    if zz800 is not None:
        m = m.merge(zz800.rename(columns={"amt": "中证800成交额"}), on="日期", how="inner")
    if hs300 is not None:
        m = m.merge(hs300.rename(columns={"amt": "hs300额"}), on="日期", how="inner")
    if zz500 is not None:
        m = m.merge(zz500.rename(columns={"amt": "zz500额"}), on="日期", how="inner")
    if bj50 is not None:
        m = m.merge(bj50.rename(columns={"amt": "bj额"}), on="日期", how="left")
    m = m[m["全A成交额"] > 0].copy()
    if "小盘成交额" in m.columns:
        m["小盘占比"] = (m["小盘成交额"] / m["全A成交额"] * 100).round(2)
    m["小微盘占比"] = (m["小微盘成交额"] / m["全A成交额"] * 100).round(2)
    if "中证800成交额" in m.columns:
        m["中证800占比"] = (m["中证800成交额"] / m["全A成交额"] * 100).round(2)
    if "hs300额" in m.columns:
        m["沪深300占比"] = (m["hs300额"] / m["全A成交额"] * 100).round(2)
    if "zz500额" in m.columns:
        m["中证500占比"] = (m["zz500额"] / m["全A成交额"] * 100).round(2)
    # 北交所全市场：中证全指（000985）不含北交所，故分母加上它
    if "bj额" in m.columns:
        m["bj额"] = pd.to_numeric(m["bj额"], errors="coerce").fillna(0)
        # 五个沪深层与北交所都以「中证全指」为分母：前五层合计=100%，北交所是额外一层
        m["_denom"] = m["全A成交额"]
        m["北交所占比"] = (m["bj额"] / m["_denom"] * 100).round(2)
        r50 = _bse50_ratio()
        m["北证50占比"] = (m["bj额"] * r50 / m["_denom"] * 100).round(2)
        m["北交所其他占比"] = (m["bj额"] * (1 - r50) / m["_denom"] * 100).round(2)
        amap = {"沪深300占比": "hs300额", "中证500占比": "zz500额",
                "小盘占比": "小盘成交额", "小微盘占比": "小微盘成交额"}
        for pc, ac in amap.items():
            if pc in m.columns and ac in m.columns:
                m[pc] = (m[ac].fillna(0) / m["_denom"] * 100).round(2)
    # 「其他」= 中证全指内扣除 300/500/1000/2000 后的余额（不含北交所）
    core = [c for c in ("沪深300占比", "中证500占比", "小盘占比", "小微盘占比") if c in m.columns]
    if len(core) == 4:
        m["其他占比"] = (100 - m[core].sum(axis=1)).round(2)
    cols = ["日期", "小盘占比", "小微盘占比", "中证800占比",
            "沪深300占比", "中证500占比", "北交所占比", "北证50占比", "北交所其他占比", "其他占比",
            "小盘成交额", "小微盘成交额", "中证800成交额", "全A成交额",
            "hs300额", "zz500额"]
    # 各层金额（亿元），供图表 tooltip 使用
    if "其他占比" in m.columns:
        m["其他成交额"] = (m["全A成交额"] - m.get("hs300额", 0) - m.get("zz500额", 0)
                          - m.get("小盘成交额", 0) - m.get("小微盘成交额", 0)).round(2)
    if "bj额" in m.columns:
        m["北证50成交额"] = (m["bj额"] * r50).round(2)
        m["北交所其他成交额"] = (m["bj额"] * (1 - r50)).round(2)
    cols += [c for c in ("沪深300成交额来源",) if False]
    extra = ["其他成交额", "北证50成交额", "北交所其他成交额"]
    out = m[[c for c in cols + extra if c in m.columns]].sort_values("日期").reset_index(drop=True)
    print(f"  [OK] 小/微盘成交占比 {len(out)} 天（{out['日期'].iloc[0]} ~ {out['日期'].iloc[-1]}），"
          f"最新 小盘 {out['小盘占比'].iloc[-1]}% / 小微盘 {out['小微盘占比'].iloc[-1]}%"
          + (f" / 中证800 {out['中证800占比'].iloc[-1]}%" if '中证800占比' in out.columns else ""))
    return out


def fetch_market_crowding():
    """获取全市场成交额前5%个股成交占比（市场拥挤度指标）"""
    print(f"\n>> 采集: 市场拥挤度")

    today_str = datetime.now().strftime("%Y-%m-%d")

    try:
        df = ak.stock_zh_a_spot()
        if df is None or df.empty:
            print(f"  [WARN] stock_zh_a_spot 返回空")
            return _crowding_fallback(today_str)

        # 优先按列名找成交额，取不到再退回第 12 列（新浪快照顺序偶尔变化）
        cands = [c for c in df.columns if str(c).strip() in ("成交额", "成交金额", "amount")]
        amount_col = cands[0] if cands else df.columns[11]
        amounts = pd.to_numeric(df[amount_col], errors="coerce").dropna()
        amounts = amounts[amounts > 0]

        if len(amounts) < 100:
            print(f"  [WARN] 有效成交额数据不足({len(amounts)}条)")
            return _crowding_fallback(today_str)

        total_amount = amounts.sum()
        top5_count = max(1, len(amounts) // 20)
        amounts_sorted = amounts.sort_values(ascending=False).values
        top5_amount = amounts_sorted[:top5_count].sum()
        ratio = round(top5_amount / total_amount * 100, 2)

        print(f"  [INFO] 全市场 {len(amounts)} 只股票, 总成交额 {total_amount/1e8:.0f} 亿元")
        print(f"  [INFO] 前5%个股({top5_count}只)成交占比: {ratio}%")

        # Build historical or append to existing CSV
        return _save_crowding(today_str, ratio, total_amount, top5_amount, top5_count, len(amounts))
    except Exception as e:
        print(f"  [ERR] 市场拥挤度获取失败: {e}")
        traceback.print_exc()
        return _crowding_fallback(today_str)


def _crowding_fallback(today_str, ratio=None, total_amount=None):
    """Fallback: use sh_index volume as proxy ratio estimate"""
    print(f"  [INFO] 使用上证指数成交额作为替代指标")
    try:
        from fetch_data import fetch_index_daily as _fid
        df = _fid("sh000001", "上证指数")
        if df is not None and not df.empty:
            vol = float(df.iloc[-1, -1]) / 1e8  # to 亿元
            if ratio is None:
                # Rough estimate: top5% stocks ~ 35-45% of total
                ratio = round(min(50, max(20, vol * 0.01 + 25)), 2)
            if total_amount is None:
                total_amount = vol * 2.3  # 沪市 ~43% of total
            return _save_crowding(today_str, ratio, total_amount, total_amount * ratio / 100, 250, 5000)
    except Exception as e:
        print(f"  [ERR] 替代方案也失败: {e}")
    return None


def _save_crowding(today_str, ratio, total_amount, top5_amount, top5_count, total_stocks):
    """Save or append crowding data to CSV"""
    import os as _os
    filepath = _os.path.join(DATA_DIR, "market_crowding.csv")

    row = {
        "日期": today_str,
        "拥挤度": ratio,
        "总成交额": round(total_amount / 1e8, 2) if total_amount and total_amount > 1e6 else round(total_amount, 2),
        "前5%成交额": round(top5_amount / 1e8, 2) if top5_amount and top5_amount > 1e6 else round(top5_amount, 2),
        "前5%个股数": top5_count,
        "全市场个股数": total_stocks,
    }
    new_df = pd.DataFrame([row])

    if _os.path.exists(filepath):
        existing = pd.read_csv(filepath, encoding="utf-8-sig")
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["日期"], keep="last")
        combined = combined.sort_values("日期")
    else:
        combined = new_df
    return combined
def _margin_from_sse(start="2010-03-01"):
    """上交所官方融资余额（亿元），按 3 年分段抓取。返回 [日期, 沪深融资余额]。"""
    frames = []
    s0 = pd.Timestamp(start)
    end = pd.Timestamp.now().normalize()
    while s0 <= end:
        s1 = min(s0 + pd.DateOffset(years=3), end)
        try:
            d = ak.stock_margin_sse(start_date=s0.strftime("%Y%m%d"),
                                    end_date=s1.strftime("%Y%m%d"))
            if d is not None and len(d):
                d = d.rename(columns={"信用交易日期": "_raw", "融资余额": "_bal"})
                d["日期"] = pd.to_datetime(d["_raw"].astype(str)).dt.strftime("%Y-%m-%d")
                d["沪深融资余额"] = pd.to_numeric(d["_bal"], errors="coerce") / 1e8
                frames.append(d[["日期", "沪深融资余额"]])
        except Exception as e:
            print(f"  [WARN] 上交所融资余额 {s0.date()}~{s1.date()} 失败 {type(e).__name__}")
        s0 = s1 + pd.Timedelta(days=1)
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True).dropna()
    return out.drop_duplicates("日期").sort_values("日期").reset_index(drop=True)


SZSE_MARGIN_CACHE = os.path.join(DATA_DIR, "margin_szse.csv")


def _margin_szse_series(dates, workers=4):
    """深市融资余额（亿元）。按日抓取并落盘缓存 margin_szse.csv，只补缺失日期。"""
    cache = {}
    if os.path.exists(SZSE_MARGIN_CACHE):
        try:
            old = pd.read_csv(SZSE_MARGIN_CACHE, dtype={"日期": str})
            cache = dict(zip(old["日期"], pd.to_numeric(old["深市融资余额"], errors="coerce")))
        except Exception:
            cache = {}
    todo = [d for d in dates if d not in cache]
    # 深交所接口对批量请求会限流：每次最多补最近 240 天，且连续失败即停
    todo = todo[-240:]
    if todo:
        print(f"  [INFO] 深市融资余额待补 {len(todo)} 天（单次上限 240）")
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _one(d):
            try:
                x = ak.stock_margin_szse(date=d.replace("-", ""))
                if x is not None and len(x):
                    return d, float(pd.to_numeric(x.iloc[0]["融资余额"], errors="coerce"))
            except Exception:
                pass
            return d, None

        done, streak, t0 = 0, 0, time.time()
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(_one, d) for d in todo]
            for f in as_completed(futs):
                d, v = f.result()
                if v is not None and not pd.isna(v):
                    cache[d] = v
                    streak = 0
                else:
                    streak += 1
                done += 1
                if done % 50 == 0:
                    print(f"    [SZSE] {done}/{len(todo)} 成功 {len(cache)} 天，{time.time()-t0:.0f}s")
                if streak >= 15 or time.time() - t0 > 240:
                    print("    [WARN] 深交所限流或超时，本次提前结束")
                    break
        try:
            out = pd.DataFrame({"日期": list(cache.keys()),
                                "深市融资余额": list(cache.values())}).sort_values("日期")
            out.to_csv(SZSE_MARGIN_CACHE, index=False, encoding="utf-8-sig")
        except Exception:
            pass
    return cache


def _margin_from_em():
    """备用：东财口径（macro_china_market_margin_sh，更新较慢）。"""
    df = ak.macro_china_market_margin_sh()
    if df is None or df.empty or len(df.columns) < 3:
        return None
    date_col, val_col = df.columns[0], df.columns[2]
    out = df[[date_col, val_col]].copy()
    out[val_col] = pd.to_numeric(out[val_col], errors="coerce") / 1e8
    out.columns = ["日期", "沪深融资余额"]
    out["日期"] = pd.to_datetime(out["日期"]).dt.strftime("%Y-%m-%d")
    return out.dropna().reset_index(drop=True)


def fetch_margin_data():
    """融资余额：主源上交所官方接口，备用东财；本地已有数据会合并补齐。"""
    print("\n>> 采集: 融资融券数据")
    path = os.path.join(DATA_DIR, "margin_balance.csv")
    old = None
    if os.path.exists(path):
        try:
            old = pd.read_csv(path, dtype={"日期": str})
        except Exception:
            old = None

    start = "2010-03-01"
    if old is not None and len(old):
        try:
            start = (pd.Timestamp(old["日期"].max()) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
        except Exception:
            pass

    for src, fn in (("上交所", lambda: _margin_from_sse(start)), ("东财", _margin_from_em)):
        try:
            new = fn()
        except Exception as e:
            print(f"  [WARN] {src} 融资余额失败 {type(e).__name__} {str(e)[:60]}")
            new = None
        if new is None or new.empty:
            continue
        if old is not None and len(old):
            merged = (pd.concat([old, new], ignore_index=True)
                        .drop_duplicates("日期", keep="last")
                        .sort_values("日期").reset_index(drop=True))
        else:
            merged = new
        merged = merged[merged["日期"] >= "2018-01-01"].reset_index(drop=True)

        # 深市融资余额 → 沪深合计
        try:
            sz = _margin_szse_series(merged["日期"].tolist())
            if sz:
                merged["沪市融资余额"] = merged["沪深融资余额"]
                merged["深市融资余额"] = merged["日期"].map(sz)
                both = merged.dropna(subset=["深市融资余额"]).copy()
                if len(both) > 100:
                    both["沪深融资余额"] = both["沪市融资余额"] + both["深市融资余额"]
                    merged = both[["日期", "沪深融资余额", "沪市融资余额", "深市融资余额"]]
                    print(f"  [INFO] 已合并深市：{len(merged)} 天，最新 "
                          f"{merged['日期'].iloc[-1]} 合计 {float(merged['沪深融资余额'].iloc[-1]):.0f} 亿元")
        except Exception as e:
            print(f"  [WARN] 深市合并失败 {type(e).__name__} {str(e)[:50]}")
        latest = merged.iloc[-1]
        print(f"  [OK] 融资余额 {len(merged)} 条（{src}），最新 {latest['日期']} "
              f"{float(latest['沪深融资余额']):.0f} 亿元")
        return merged

    print("  [ERR] 融资余额两个数据源均失败")
    return None


# ═══════════════════════════════════════════════════════════════
#  ETF 历史行情 — 使用 akshare fund_etf_hist_em
# ═══════════════════════════════════════════════════════════════
def fetch_etf_history(etf_code, etf_name):
    """获取 ETF 历史日线数据"""
    print(f"\n>> 采集: {etf_name} ({etf_code})")
    try:
        df = ak.fund_etf_hist_em(symbol=etf_code, period="daily",
                                  start_date="20100101", end_date="20501231", adjust="qfq")
        if df is not None and not df.empty:
            print(f"  [INFO] 获取 ETF {etf_code} 共 {len(df)} 条数据")
            return df
    except Exception as e:
        print(f"  [WARN] akshare ETF {etf_code} 获取失败 ({e})")

    # 回退：通过东财 push2 K线接口
    try:
        secid = f"1.{etf_code}"
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfba660",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "1", "end": "20500101", "lmt": "10000",
        }
        resp = requests.get(url, params=params, timeout=30,
                          headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        data = resp.json()
        if data.get("data") and data["data"].get("klines"):
            records = []
            for k in data["data"]["klines"]:
                parts = k.split(",")
                records.append({
                    "日期": parts[0], "开盘": float(parts[1]),
                    "收盘": float(parts[2]), "最高": float(parts[3]),
                    "最低": float(parts[4]), "成交量": float(parts[5]),
                    "成交额": float(parts[6]),
                })
            df = pd.DataFrame(records)
            print(f"  [INFO] (东财回退) 获取 ETF {etf_code} 共 {len(df)} 条数据")
            return df
    except Exception as e2:
        print(f"  [ERR] ETF {etf_code} 东方财富回退也失败: {e2}")

    return None


# ═══════════════════════════════════════════════════════════════
#  指数 PE/PB 估值 — stock_index_pe_lg (乐股乐，含完整历史+分位数)
#  列: 日期, 指数, 加权动态市盈率, 动态市盈率, 动态市盈率分位数,
#      加权静态市盈率, 静态市盈率, 静态市盈率分位数
#  PE_TTM = 动态市盈率 (col[3]), 分位 = col[4]
#  另存 stock_index_pb_lg 获取 PB+分位
# ═══════════════════════════════════════════════════════════════
PE_INDEX_MAP = {
    "sh000300": "沪深300", "sh000016": "上证50", "sh000905": "中证500",
    "sh000852": "中证1000", "sh000009": "上证380", "sh000906": "中证800",
    "sh000510": "中证A500", "sz399006": "创业板指", "sh000688": "科创50",
    "sh000932": "中证2000", "sz399303": "国证2000",
}

# akshare stock_index_pe_lg 的 symbol_map 未收录、但乐股乐接口仍提供数据的指数
LG_DIRECT_CODES = {"sh000510", "sz399006", "sh000688", "sh000932", "sz399303"}
PB_INDEX_MAP = {
    "sh000300": "沪深300", "sh000016": "上证50",
}

# ═══════════════════════════════════════════════════════════════
#  行业 PE 估值 — CSRC → SW 映射 (cninfo 巨潮资讯)
#  stock_industry_pe_ratio_cninfo 返回 CSRC 分类的行业PE数据
#  通过 CSRC_2_SW_MAP 映射到申万31个一级行业
# ═══════════════════════════════════════════════════════════════
CSRC_2_SW_MAP = {
    '食品饮料': ['C14', 'C15'],
    '银行':     ['J66'],
    '非银金融': ['J67', 'J68', 'J69'],
    '医药生物': ['C27', 'Q84'],
    '电子':     ['C39'],
    '计算机':   ['I65'],
    '电力设备': ['C38'],
    '汽车':     ['C36'],
    '基础化工': ['C26'],
    '传媒':     ['I64', 'R86', 'R87', 'R88'],
    '机械设备': ['C34', 'C35'],
    '通信':     ['I63'],
    '房地产':   ['K70'],
    '有色金属': ['C32'],
    '国防军工': ['C37'],
    '建筑装饰': ['E48', 'E50'],
    '交通运输': ['G53', 'G54', 'G55', 'G56', 'G58'],
    '家用电器': ['C38'],
    '农林牧渔': ['A01', 'A03', 'A04'],
    '公用事业': ['D44', 'D45', 'D46'],
    '轻工制造': ['C22', 'C23', 'C24'],
    '纺织服饰': ['C17', 'C18'],
    '建筑材料': ['C30'],
    '煤炭':     ['B06'],
    '钢铁':     ['C31'],
    '石油石化': ['C25', 'B07'],
    '社会服务': ['L72', 'H61', 'H62', 'N78'],
    '商贸零售': ['F51', 'F52'],
    '环保':     ['N77'],
    '美容护理': ['C26'],
    '综合':     ['S91'],
}

def fetch_sw_industry_pe():
    """通过CSRC→SW映射获取31个申万行业的PE(中位数)估值"""
    print(f"\n>> 采集: 行业PE估值 (CSRC→SW映射)")
    try:
        df = ak.stock_industry_pe_ratio_cninfo(date="20260724")
        if df is None or df.empty:
            print(f"  [WARN] cninfo 行业PE返回空")
            return None
        # col 2=行业层级, col 3=行业代码, col 4=行业名称, col 10=静态市盈率-中位数
        l2 = df[df.iloc[:, 2] == 2.0]
        pe_data = {}
        for _, row in l2.iterrows():
            code = row.iloc[3]
            pe_m = float(row.iloc[10]) if pd.notna(row.iloc[10]) else None
            pe_data[code] = pe_m

        rows = []
        for sw_name, csrc_codes in CSRC_2_SW_MAP.items():
            pes = [pe_data.get(c) for c in csrc_codes if pe_data.get(c) is not None]
            if pes:
                pe_avg = round(sum(pes) / len(pes), 2)
                rows.append({"行业": sw_name, "PE中位数": pe_avg})
            else:
                rows.append({"行业": sw_name, "PE中位数": None})

        result = pd.DataFrame(rows)
        print(f"  [INFO] 获取行业PE估值 {len(result)} 条 (CSRC映射)")
        return result
    except Exception as e:
        print(f"  [ERR] 行业PE获取失败: {e}")
        return None


def _fetch_lg_pe_direct(index_code):
    """乐股乐指数PE直取（akshare symbol_map 未收录的新指数，如中证A500）"""
    import py_mini_racer
    from akshare.stock_feature.stock_a_pe_and_pb import hash_code
    from akshare.stock_feature.stock_a_indicator import get_cookie_csrf

    js = py_mini_racer.MiniRacer()
    js.eval(hash_code)
    token = js.call("hex", datetime.now().date().isoformat()).lower()
    exch, num = index_code[:2], index_code[2:]
    r = requests.get(
        "https://legulegu.com/api/stockdata/index-basic-pe",
        params={"token": token, "indexCode": f"{num}.{exch.upper()}"},
        timeout=20,
        **get_cookie_csrf(url="https://legulegu.com/stockdata/sz50-ttm-lyr"),
    )
    data = r.json().get("data") or []
    if not data:
        return None

    df = pd.DataFrame(data)
    result = pd.DataFrame()
    result["日期"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert("Asia/Shanghai").dt.date
    result["PE_TTM"] = pd.to_numeric(df["addTtmPe"], errors="coerce").round(4)
    return result


def fetch_index_valuation(index_code, index_name):
    """获取指数历史 PE/PB 及分位数（乐股乐数据源）"""
    print(f"\n>> 采集: {index_name} 历史估值")

    cn_name = PE_INDEX_MAP.get(index_code)
    if cn_name is None:
        print(f"  [WARN] 指数 {index_code} ({index_name}) 无乐股乐映射，跳过")
        return None

    # -- 主方案：stock_index_pe_lg (5000+日线历史 + 分位数) --
    # 部分新指数不在 akshare 的 symbol_map 中，直连乐股乐接口
    if index_code in LG_DIRECT_CODES:
        try:
            result = _fetch_lg_pe_direct(index_code)
            if result is not None and not result.empty:
                latest = result.iloc[-1]
                msg = f"PE={latest['PE_TTM']:.2f}"
                if "PE分位" in result.columns:
                    msg += f" 分位={latest['PE分位']:.2f}%"
                print(f"  [INFO] 获取 {index_name} {msg}")
                return result
        except Exception as e:
            print(f"  [WARN] 乐股乐 PE 失败 ({index_name}): {e}")
    try:
        if index_code not in LG_DIRECT_CODES:
            df_pe = ak.stock_index_pe_lg(symbol=cn_name)
        else:
            df_pe = None
        if df_pe is not None and not df_pe.empty:
            result = pd.DataFrame()
            # 新版 akshare 列序：日期, 指数, 等权静态PE, 静态PE, 静态PE中位数,
            #                   等权滚动PE, 滚动市盈率, 滚动PE中位数（旧版末尾有分位数，已移除）
            pe_col = next((c for c in df_pe.columns
                           if "滚动市盈率" in str(c) and "等权" not in str(c)), None)
            result["日期"] = df_pe.iloc[:, 0]
            result["PE_TTM"] = pd.to_numeric(
                df_pe[pe_col] if pe_col else df_pe.iloc[:, 6], errors="coerce")

            # -- Try PB --
            pb_name = PB_INDEX_MAP.get(index_code)
            if pb_name:
                try:
                    df_pb = ak.stock_index_pb_lg(symbol=pb_name)
                    if df_pb is not None and not df_pb.empty:
                        # 列序：日期, 指数, 市净率, 等权市净率, 市净率中位数
                        pb_col = next((c for c in df_pb.columns
                                       if str(c).strip() == "市净率"), None)
                        result["PB"] = pd.to_numeric(
                            df_pb[pb_col] if pb_col else df_pb.iloc[:, 2], errors="coerce")
                except Exception as pb_e:
                    print(f"  [INFO] PB 获取失败 ({index_name}): {pb_e}")

            latest = result.iloc[-1]
            pb_info = f"PB={latest['PB']:.2f}" if 'PB' in result.columns else ""
            print(f"  [INFO] 获取 {index_name} PE_TTM={latest['PE_TTM']:.2f} {pb_info}")
            return result
    except Exception as e:
        print(f"  [WARN] 乐股乐 PE 失败 ({index_name}): {e}")

    # -- 回退：stock_zh_index_value_csindex (仅最近20期月频数据) --
    code = index_code.replace("sh", "").replace("sz", "").replace("bj", "")
    try:
        df = ak.stock_zh_index_value_csindex(symbol=code)
        if df is not None and not df.empty:
            result = pd.DataFrame()
            result["日期"] = df.iloc[:, 0]
            pe1_col = next((c for c in df.columns if "市盈率1" in c), None)
            if pe1_col:
                result["PE_TTM"] = pd.to_numeric(df[pe1_col], errors="coerce")
            print(f"  [INFO] (中证官网回退) {index_name} {len(result)} 条")
            return result
    except Exception as e2:
        print(f"  [WARN] 中证官网回退失败: {e2}")

    return None


# ═══════════════════════════════════════════════════════════════
#  主采集流程
# ═══════════════════════════════════════════════════════════════
def fetch_all():
    config = load_config()
    ensure_data_dir()

    results = {"success": [], "failed": []}

    # ── 1. 各大盘指数 + 宽基指数 + 风格指数 ──
    for cat_key in ["major_indices", "broad_indices", "style_indices"]:
        indicators = config.get("indicators", {}).get(cat_key, [])
        cat_name = {"major_indices": "大盘指数", "broad_indices": "宽基指数",
                    "style_indices": "风格指数"}.get(cat_key, cat_key)
        print(f"\n{'='*50}")
        print(f"  [{cat_name}]")
        print(f"{'='*50}")

        for ind in indicators:
            ind_id = ind["id"]
            name = ind["name"]
            params = ind.get("akshare_params", {})
            symbol = params.get("symbol", "")

            try:
                df = fetch_index_daily(symbol, name)
                if df is not None and not df.empty:
                    if save_csv(df, ind_id):
                        results["success"].append(ind_id)
                    else:
                        results["failed"].append(ind_id)
                else:
                    results["failed"].append(ind_id)
            except Exception as e:
                print(f"  [ERR] {name} 异常: {e}")
                results["failed"].append(ind_id)

    # ── 2. 申万行业指数 ──
    print(f"\n{'='*50}")
    print(f"  [行业指数 — 申万一级]")
    print(f"{'='*50}")
    for ind_id, sw_code in SW_INDUSTRY_CODES.items():
        name = SW_NAMES.get(ind_id, ind_id)
        try:
            df = fetch_sw_industry_index(sw_code, ind_id, name)
            if df is not None and not df.empty:
                if save_csv(df, ind_id):
                    results["success"].append(ind_id)
                else:
                    results["failed"].append(ind_id)
            else:
                results["failed"].append(ind_id)
        except Exception as e:
            print(f"  [ERR] {name} 异常: {e}")
            results["failed"].append(ind_id)

    # ── 3. 市场宽度指标 ──
    print(f"\n{'='*50}")
    print(f"  [市场宽度]")
    print(f"{'='*50}")
    try:
        breadth_data = fetch_market_breadth()
        if breadth_data:
            for key, df in breadth_data.items():
                if save_csv(df, key):
                    results["success"].append(key)
                else:
                    results["failed"].append(key)

            # 合并历史数据
            _merge_breadth_history("limit_stats", ["涨停数量", "跌停数量"])
            _merge_breadth_history("advance_decline", ["上涨家数", "下跌家数", "平盘家数", "涨跌比"])
        else:
            results["failed"].extend(["limit_stats", "advance_decline"])
    except Exception as e:
        print(f"  [ERR] 市场宽度异常: {e}")
        results["failed"].extend(["limit_stats", "advance_decline"])

    # ── 4. 融资融券数据 ──
    print(f"\n{'='*50}")
    print(f"  [情绪指标 — 融资余额]")
    print(f"{'='*50}")
    try:
        df_margin = fetch_margin_data()
        if df_margin is not None and not df_margin.empty:
            if save_csv(df_margin, "margin_balance"):
                results["success"].append("margin_balance")
            else:
                results["failed"].append("margin_balance")
        else:
            results["failed"].append("margin_balance")
    except Exception as e:
        print(f"  [ERR] 融资余额异常: {e}")
        results["failed"].append("margin_balance")

    # ── 5. ETF 历史数据 ──
    print(f"\n{'='*50}")
    print(f"  [ETF 行情]")
    print(f"{'='*50}")
    # 从宽基指数中提取 ETF 代码
    broad_indices = config.get("indicators", {}).get("broad_indices", [])
    for ind in broad_indices:
        etf_code = ind.get("etf_code", "")
        etf_name = ind.get("etf_name", "")
        if etf_code:
            try:
                df_etf = fetch_etf_history(etf_code, etf_name)
                if df_etf is not None and not df_etf.empty:
                    etf_id = f"etf_{etf_code}"
                    if save_csv(df_etf, etf_id):
                        results["success"].append(etf_id)
                    else:
                        results["failed"].append(etf_id)
                else:
                    results["failed"].append(f"etf_{etf_code}")
            except Exception as e:
                print(f"  [ERR] ETF {etf_code} 异常: {e}")
                results["failed"].append(f"etf_{etf_code}")

    # ── 6. 指数 PE/PB 估值 ──
    print(f"\n{'='*50}")
    print(f"  [指数估值 PE/PB]")
    print(f"{'='*50}")
    # 宽基指数估值（乐股乐支持 + 中证指数回退）
    valuation_indices = [
        ("sh000300", "沪深300"),
        ("sh000510", "中证A500"),
        ("sh000016", "上证50"),
        ("sh000905", "中证500"),
        ("sh000852", "中证1000"),
        ("sz399006", "创业板指"),
        ("sh000688", "科创50"),
        ("sh000009", "上证380"),
        ("sh000906", "中证800"),
        ("sh000932", "中证2000"),
        ("sz399303", "国证2000"),
    ]
    for vcode, vname in valuation_indices:
        val_id = f"valuation_{vcode.replace('sh','').replace('sz','')}"
        try:
            df_val = fetch_index_valuation(vcode, vname)
            if df_val is not None and not df_val.empty:
                if save_csv(df_val, val_id):
                    results["success"].append(val_id)
                else:
                    results["failed"].append(val_id)
            else:
                results["failed"].append(val_id)
        except Exception as e:
            print(f"  [ERR] {vname} 估值异常: {e}")
            results["failed"].append(val_id)

    # ── 7. 行业PE数据 (CSRC分类 → SW行业) ──
    print(f"\n{'='*50}")
    print(f"  [行业PE估值]")
    print(f"{'='*50}")
    try:
        sw_pe_data = fetch_sw_industry_pe()
        if sw_pe_data is not None and not sw_pe_data.empty:
            save_csv(sw_pe_data, "industry_pe")
            results["success"].append("industry_pe")
        else:
            results["failed"].append("industry_pe")
    except Exception as e:
        print(f"  [ERR] 行业PE数据获取失败: {e}")
        results["failed"].append("industry_pe")

    # ── 8. 市场拥挤度（前5%个股成交占比）──
    print(f"\n{'='*50}")
    print(f"  [市场拥挤度]")
    print(f"{'='*50}")
    try:
        crowding_df = fetch_market_crowding()
        if crowding_df is not None:
            save_csv(crowding_df, "market_crowding")
            results["success"].append("market_crowding")
        else:
            results["failed"].append("market_crowding")
    except Exception as e:
        print(f"  [ERR] 市场拥挤度获取失败: {e}")
        results["failed"].append("market_crowding")

    # ── 8.5 小盘成交占比（长历史代理）──
    try:
        proxy_df = fetch_crowding_proxy()
        if proxy_df is not None:
            save_csv(proxy_df, "crowding_proxy")
            results["success"].append("crowding_proxy")
        else:
            results["failed"].append("crowding_proxy")
    except Exception as e:
        print(f"  [ERR] 小盘成交占比获取失败: {e}")
        results["failed"].append("crowding_proxy")

    # ── 8.7 市值结构时序 ──
    try:
        cap_df = fetch_cap_structure()
        if cap_df is not None:
            save_csv(cap_df, "cap_structure")
            results["success"].append("cap_structure")
        else:
            results["failed"].append("cap_structure")
    except Exception as e:
        print(f"  [ERR] 市值结构获取失败: {e}")
        results["failed"].append("cap_structure")

    # ── 汇总 ──
    n_total = len(results["success"]) + len(results["failed"])
    print(f"\n{'='*50}")
    print(f"  采集完成: {len(results['success'])}/{n_total} 成功, "
          f"{len(results['failed'])} 失败")
    if results["failed"]:
        print(f"  失败项 ({len(results['failed'])}): {', '.join(results['failed'][:20])}")
    print(f"{'='*50}")

    # 写入元数据
    meta = {
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "success_count": len(results["success"]),
        "failed_count": len(results["failed"]),
        "failed_items": results["failed"],
    }
    meta_path = os.path.join(DATA_DIR, "meta.yaml")
    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.dump(meta, f, allow_unicode=True)
    print(f"\n  元数据已保存到 {meta_path}")

    return results


def _merge_breadth_history(csv_id, columns):
    """将新获取的市场宽度数据与历史CSV合并"""
    filepath = os.path.join(DATA_DIR, f"{csv_id}.csv")
    if not os.path.exists(filepath):
        return

    try:
        existing = pd.read_csv(filepath, encoding="utf-8-sig")
        new_data = pd.read_csv(filepath, encoding="utf-8-sig")
        # 去重
        if "日期" in new_data.columns:
            combined = pd.concat([existing, new_data], ignore_index=True)
            combined = combined.drop_duplicates(subset=["日期"], keep="last")
            combined = combined.sort_values("日期")
            combined.to_csv(filepath, index=False, encoding="utf-8-sig")
    except Exception:
        pass


if __name__ == "__main__":
    fetch_all()
