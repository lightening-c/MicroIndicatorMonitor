"""
微观指标跟踪 — 分析引擎
CSV → analysis_result.json
计算：技术指标（MA/MACD/RSI/布林带）、估值分位、情绪综合指数、行业景气度
"""
import os
import re
import json
import yaml
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from paths import DATA_DIR, CONFIG_FILE

ANALYSIS_OUTPUT = os.path.join(DATA_DIR, "analysis_result.json")

# ═══════════════════════════════════════════════════════════════
#  Date parsing utilities
# ═══════════════════════════════════════════════════════════════
def _json_safe(obj):
    """递归把 NaN/Inf/numpy 标量转成 JSON 可表示的值（浏览器 JSON.parse 不接受 NaN）。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return None if (np.isnan(obj) or np.isinf(obj)) else float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def parse_date(val):
    """Try to parse various date formats"""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return pd.NaT
    s = str(val).strip()
    if not s:
        return pd.NaT

    # Numeric
    clean = s.replace(".", "")
    if re.match(r'^\d{8}$', clean):
        try: return pd.to_datetime(clean, format="%Y%m%d")
        except: pass
    if re.match(r'^\d{6}$', clean):
        try: return pd.to_datetime(clean, format="%Y%m")
        except: pass

    # Standard
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m", "%Y/%m"]:
        try: return pd.to_datetime(s, format=fmt)
        except: continue

    # Pandas fallback
    try:
        r = pd.to_datetime(s, errors="coerce")
        if pd.notna(r): return r
    except: pass
    return pd.NaT


def detect_date_col(df):
    for col in df.columns:
        if any(kw in str(col).lower() for kw in ["日期", "date", "时间"]):
            return col
    return df.columns[0]


def detect_value_col(df, date_col):
    """Find best numeric value column. Prefer '收盘' or 'close' for OHLC data."""
    # If we have OHLC, prefer '收盘'
    for col in df.columns:
        if col == date_col: continue
        if str(col) in ["收盘", "close"]:
            return col
    # Otherwise pick best numeric column
    best, best_n = None, 0
    for col in df.columns:
        if col == date_col: continue
        s = pd.to_numeric(df[col], errors="coerce")
        n = s.notna().sum()
        if n > best_n:
            best_n, best = n, col
    return best


# ═══════════════════════════════════════════════════════════════
#  Technical indicators
# ═══════════════════════════════════════════════════════════════
def compute_ma(values, period):
    """Simple moving average"""
    v = values.dropna().reset_index(drop=True)
    if len(v) < period:
        return float(v.mean())
    return float(v.tail(period).mean())


def compute_ema(values, period):
    """Exponential moving average"""
    v = values.dropna().reset_index(drop=True)
    if len(v) < 2:
        return float(v.iloc[-1]) if len(v) > 0 else None
    alpha = 2 / (period + 1)
    ema = float(v.iloc[0])
    for i in range(1, len(v)):
        ema = alpha * float(v.iloc[i]) + (1 - alpha) * ema
    return ema


def compute_macd(values, fast=12, slow=26, signal=9):
    """MACD: DIF, DEA, MACD柱"""
    v = values.dropna().reset_index(drop=True)
    if len(v) < slow + signal:
        return {"dif": None, "dea": None, "macd": None, "signal": "neutral"}

    ema_fast = compute_ema(v, fast)
    ema_slow = compute_ema(v, slow)
    if ema_fast is None or ema_slow is None:
        return {"dif": None, "dea": None, "macd": None, "signal": "neutral"}

    dif = ema_fast - ema_slow

    # DEA = EMA of DIF
    dea = None
    alpha_s = 2 / (signal + 1)
    dea = 0
    for i in range(len(v)):
        # approximate: DIF series
        pass
    # Simplified: just report latest DIF direction
    # Compute DIF series for last 2 periods for direction
    v1 = v.iloc[:-1]
    v2 = v
    ema_f1 = compute_ema(v1, fast)
    ema_s1 = compute_ema(v1, slow)
    dif_prev = (ema_f1 - ema_s1) if ema_f1 is not None and ema_s1 is not None else None
    dif_now = dif

    macd_bar = None
    macd_signal = "neutral"
    if dif_now is not None and dif_prev is not None:
        macd_bar = dif_now - dif_prev  # approximate bar
        if dif_now > dif_prev:
            macd_signal = "bullish" if dif_now > 0 else "improving"
        elif dif_now < dif_prev:
            macd_signal = "bearish" if dif_now < 0 else "deteriorating"

    return {
        "dif": round(dif_now, 4),
        "dea": round((dif_now + dif_prev) / 2, 4) if dif_prev is not None else None,
        "macd_bar": round(macd_bar, 4) if macd_bar is not None else None,
        "signal": macd_signal,
    }


def compute_rsi(values, period=14):
    """RSI — Relative Strength Index"""
    v = values.dropna().reset_index(drop=True)
    if len(v) < period + 1:
        return None
    changes = v.diff().dropna()
    gains = changes.clip(lower=0)
    losses = (-changes).clip(lower=0)
    avg_gain = float(gains.tail(period).mean())
    avg_loss = float(losses.tail(period).mean())
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    return round(rsi, 2)


def compute_bollinger(values, period=20, std_mult=2):
    """Bollinger Bands: upper, middle, lower"""
    v = values.dropna().reset_index(drop=True)
    if len(v) < period:
        return None
    tail = v.tail(period)
    middle = float(tail.mean())
    std = float(tail.std())
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    latest = float(v.iloc[-1])
    position = (latest - lower) / (upper - lower) if upper != lower else 0.5
    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "position": round(position, 4),  # 0=下轨, 1=上轨
        "status": "oversold" if position < 0.1 else "overbought" if position > 0.9 else "normal",
    }


def compute_roc(values, period=5):
    """Rate of Change"""
    v = values.dropna().reset_index(drop=True)
    if len(v) <= period:
        return None
    prev = float(v.iloc[-(period + 1)])
    curr = float(v.iloc[-1])
    if prev == 0:
        return None
    return round((curr - prev) / abs(prev) * 100, 4)


# ═══════════════════════════════════════════════════════════════
#  Valuation percentile calculation
# ═══════════════════════════════════════════════════════════════
def compute_percentile(series, current_val):
    """Calculate where current sits in historical distribution (0-100)"""
    if current_val is None or series.empty:
        return None
    s = series.dropna()
    if len(s) < 10:
        return None
    rank = (s < current_val).sum()
    pct = round(rank / len(s) * 100, 2)
    return pct


def compute_valuation_level(pct):
    """Map percentile to valuation level"""
    if pct is None:
        return "无数据"
    if pct < 20:
        return "低估"
    elif pct <= 80:
        return "合理"
    else:
        return "高估"


# ═══════════════════════════════════════════════════════════════
#  Sentiment composite index (Fear & Greed)
# ═══════════════════════════════════════════════════════════════
def compute_index_volatility(csv_path, window=20):
    """近 window 个交易日年化波动率（%），数据不足返回 None。"""
    if not os.path.exists(csv_path):
        return None
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    if "close" not in df.columns or len(df) < window + 1:
        return None
    close = pd.to_numeric(df.sort_values("日期")["close"], errors="coerce")
    ret = close.pct_change().dropna()
    if len(ret) < window:
        return None
    return round(float(ret.tail(window).std() * np.sqrt(252) * 100), 2)


# 波动率映射：年化 10% 视为极低波动(100 分)，45% 视为极高波动(0 分)
VOL_CALM = 10.0
VOL_PANIC = 45.0
# 涨跌比因子下限：单日极端普跌不应单独把指数打到 0
UD_RATIO_FLOOR = 10


def compute_fear_greed_index(margin_change=None, up_down_ratio=None,
                              volume_deviation=None, volatility=None):
    """
    自建恐贪指数：0-100 分数，值越大越贪婪
    因子标准化后等权平均；volatility 为年化波动率（%）
    """
    factors = []

    # 因子1：融资余额变化（升=贪婪）
    if margin_change is not None:
        # -10%到+10% 映射到 0-100
        score = min(100, max(0, (margin_change + 10) * 5))
        factors.append(score)

    # 因子2：涨跌比（升=贪婪）
    if up_down_ratio is not None:
        # 0.5到3.0 映射到 0-100；下限避免单日极端值一票定调
        score = min(100, max(0, (up_down_ratio - 0.5) / 2.5 * 100))
        factors.append(max(UD_RATIO_FLOOR, score))

    # 因子3：成交量偏离（放量=贪婪）
    if volume_deviation is not None:
        # -30%到+50% 映射到 0-100
        score = min(100, max(0, (volume_deviation + 30) / 80 * 100))
        factors.append(score)

    # 因子4：波动率（高波动=恐惧）
    if volatility is not None:
        # 年化 10% 到 45% 映射到 100-0
        score = min(100, max(0, (VOL_PANIC - volatility) /
                              (VOL_PANIC - VOL_CALM) * 100))
        factors.append(score)

    if not factors:
        return 50

    index = np.mean(factors)
    return round(index, 1)


# ═══════════════════════════════════════════════════════════════
#  Main analysis
# ═══════════════════════════════════════════════════════════════
def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_csv(ind_id):
    """Load a CSV file, return DataFrame with parsed dates"""
    filepath = os.path.join(DATA_DIR, f"{ind_id}.csv")
    if not os.path.exists(filepath):
        return None, None, None

    try:
        df = pd.read_csv(filepath, encoding="utf-8-sig")
        if df.empty:
            return None, None, None
    except Exception as e:
        print(f"    [ERR] 读取失败 {ind_id}: {e}")
        return None, None, None

    date_col = detect_date_col(df)
    val_col = detect_value_col(df, date_col)

    if val_col is None:
        print(f"    [ERR] 无法检测数值列 {ind_id}: {list(df.columns)}")
        return None, None, None

    df["_date"] = df[date_col].apply(parse_date)
    df = df[df["_date"].notna()].copy()
    df = df.sort_values("_date")
    df["_val"] = pd.to_numeric(df[val_col], errors="coerce")
    df = df[df["_val"].notna()].copy()

    if df.empty:
        return None, None, None

    return df, date_col, val_col


def analyze_index(ind_id, name, cfg, category):
    """Analyze a single index indicator"""
    df, date_col, val_col = load_csv(ind_id)
    if df is None:
        return None

    values = df["_val"]
    dates = df["_date"]

    latest_val = float(values.iloc[-1]) if len(values) > 0 else None
    prev_val = float(values.iloc[-2]) if len(values) > 1 else None
    latest_date = dates.iloc[-1].strftime("%Y-%m-%d") if len(dates) > 0 else None

    # Price change
    change_1d = round((values.iloc[-1] - values.iloc[-2]) / values.iloc[-2] * 100, 4) \
        if len(values) > 1 and values.iloc[-2] != 0 else None
    change_5d = compute_roc(values, 5)
    change_20d = compute_roc(values, 20)

    # MA
    ma5 = compute_ma(values, 5)
    ma20 = compute_ma(values, 20)
    ma250 = compute_ma(values, 250)
    bias250 = round((latest_val / ma250 - 1) * 100, 2) if ma250 else None

    # Trend
    if ma5 is not None and ma20 is not None and ma20 != 0:
        if ma5 > ma20 * 1.005:
            direction = "up"
        elif ma5 < ma20 * 0.995:
            direction = "down"
        else:
            direction = "stable"
    else:
        direction = "stable"

    # MACD
    macd = compute_macd(values)

    # RSI
    rsi = compute_rsi(values, 14)

    # Bollinger
    boll = compute_bollinger(values)

    # Volume (成交额, 亿元) — extract from raw CSV if available
    latest_volume = None
    vol_col = None
    for col in df.columns:
        if col in ["_date", "_val"]: continue
        cl = str(col).lower()
        if "volume" in cl or "成交" in cl or "amount" in cl:
            vol_col = col
            break
    if vol_col:
        vol_vals = pd.to_numeric(df[vol_col], errors="coerce")
        latest_vol_raw = float(vol_vals.iloc[-1]) if len(vol_vals) > 0 and pd.notna(vol_vals.iloc[-1]) else None
        if latest_vol_raw is not None:
            # Convert to 亿元 if > 1e8
            if latest_vol_raw > 1e8:
                latest_volume = round(latest_vol_raw / 1e8, 2)
            else:
                latest_volume = round(latest_vol_raw, 2)

    # Momentum
    momentum = {
        "1d": change_1d,
        "5d": change_5d,
        "20d": change_20d,
    }

    # Time series for frontend (last N days)
    n_disp = 120
    df_tail = df.tail(n_disp)
    ts_data = [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
               for d, v in zip(df_tail["_date"], df_tail["_val"])]

    return {
        "id": ind_id,
        "name": name,
        "category": category,
        "unit": cfg.get("unit", ""),
        "source": cfg.get("source", ""),
        "frequency": cfg.get("frequency", ""),
        "latest_value": latest_val,
        "latest_date": latest_date,
        "latest_volume": latest_volume,
        "change_1d": change_1d,
        "change_5d": change_5d,
        "change_20d": change_20d,
        "ma5": round(ma5, 4) if ma5 else None,
        "ma20": round(ma20, 4) if ma20 else None,
        "ma250": round(ma250, 4) if ma250 else None,
        "bias250": bias250,
        "direction": direction,
        "macd": macd,
        "rsi": rsi,
        "bollinger": boll,
        "momentum": momentum,
        "ts_data": ts_data,
    }


def analyze_industry(ind_id, name):
    """Analyze a SW industry index"""
    df, date_col, val_col = load_csv(ind_id)
    if df is None:
        return None

    values = df["_val"]
    dates = df["_date"]

    latest_val = float(values.iloc[-1]) if len(values) > 0 else None
    latest_date = dates.iloc[-1].strftime("%Y-%m-%d") if len(dates) > 0 else None

    change_1d = round((values.iloc[-1] - values.iloc[-2]) / values.iloc[-2] * 100, 4) \
        if len(values) > 1 and values.iloc[-2] != 0 else None
    change_5d = compute_roc(values, 5)
    change_20d = compute_roc(values, 20)

    ma5 = compute_ma(values, 5)
    ma20 = compute_ma(values, 20)
    direction = "up" if ma5 > ma20 * 1.005 else "down" if ma5 < ma20 * 0.995 else "stable"

    rsi = compute_rsi(values, 14)
    boll = compute_bollinger(values)

    # Volume (成交额, 亿元)
    latest_volume = None
    vol_col = None
    for col in df.columns:
        if col in ["_date", "_val"]: continue
        cl = str(col).lower()
        if "volume" in cl or "成交" in cl or "amount" in cl:
            vol_col = col
            break
    if vol_col:
        vol_vals = pd.to_numeric(df[vol_col], errors="coerce")
        latest_vol_raw = float(vol_vals.iloc[-1]) if len(vol_vals) > 0 and pd.notna(vol_vals.iloc[-1]) else None
        if latest_vol_raw is not None:
            if latest_vol_raw > 1e8:
                latest_volume = round(latest_vol_raw / 1e8, 2)
            else:
                latest_volume = round(latest_vol_raw, 2)

    ts_data = [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
               for d, v in zip(df.tail(120)["_date"], df.tail(120)["_val"])]

    return {
        "id": ind_id,
        "name": name,
        "category": "industry",
        "latest_value": latest_val,
        "latest_date": latest_date,
        "latest_volume": latest_volume,
        "change_1d": change_1d,
        "change_5d": change_5d,
        "change_20d": change_20d,
        "ma5": round(ma5, 4) if ma5 else None,
        "ma20": round(ma20, 4) if ma20 else None,
        "direction": direction,
        "rsi": rsi,
        "bollinger": boll,
        "ts_data": ts_data,
    }


def analyze_valuation(val_id, index_name):
    """Analyze PE/PB valuation. Percentiles are computed from the CSV series
    (5年 / 10年 / 全历史); a window is only used when the series covers it.
    """
    df, date_col, val_col = load_csv(val_id)
    if df is None:
        return None

    latest_dt = df["_date"].iloc[-1]
    latest_date = latest_dt.strftime("%Y-%m-%d")

    # PE_TTM — from CSV column directly
    pe_col = next((c for c in df.columns if c == "PE_TTM"), None)
    latest_pe = float(df[pe_col].iloc[-1]) if pe_col and len(df) > 0 else None

    # PE percentile — computed from series (乐股乐分位列已弃用)
    def percentile(series, latest, window_days=None, min_obs=24):
        s = pd.to_numeric(series, errors="coerce").dropna()
        if latest is None or len(s) == 0:
            return None
        if window_days is not None:
            if s.index.min() > latest_dt - pd.Timedelta(days=window_days):
                return None  # 历史不足该窗口
            s = s[s.index >= latest_dt - pd.Timedelta(days=window_days)]
        if len(s) < min_obs:
            return None
        return round(float((s <= latest).mean() * 100), 2)

    pe_series = df.set_index("_date")[pe_col] if pe_col else None
    pe_pct_all = percentile(pe_series, latest_pe) if pe_col else None
    pe_pct_5y = percentile(pe_series, latest_pe, window_days=1826) if pe_col else None
    pe_pct_10y = percentile(pe_series, latest_pe, window_days=3652) if pe_col else None

    # PB — from CSV
    pb_col = next((c for c in df.columns if c == "PB"), None)
    latest_pb = float(df[pb_col].iloc[-1]) if pb_col and len(df) > 0 else None

    # PB percentile
    pb_pct_all = percentile(df.set_index("_date")[pb_col], latest_pb) if (pb_col and latest_pb is not None) else None

    ts_data = []
    for d, v in zip(df.tail(252)["_date"], df.tail(252)[pe_col] if pe_col else [None]*min(252, len(df))):
        ts_data.append({"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 4) if pd.notna(v) else None})

    return {
        "id": val_id,
        "name": index_name,
        "category": "valuation",
        "latest_date": latest_date,
        "pe_ttm": latest_pe,
        "pb": latest_pb,
        "pe_pct_all": pe_pct_all,    # 乐股乐分位 — 直接使用
        "pe_pct_5y": pe_pct_5y,      # 乐股乐已覆盖5年以上历史，分位即全历史分位
        "pe_pct_10y": pe_pct_10y,     # 同上
        "pb_pct_all": pb_pct_all,
        "pe_level": compute_valuation_level(pe_pct_5y if pe_pct_5y is not None else pe_pct_all),
        "pb_level": compute_valuation_level(pb_pct_all),
        "ts_data": ts_data,
    }


def analyze_market_breadth():
    """Analyze market breadth indicators"""
    result = {
        "limit_stats": None,
        "advance_decline": None,
        "up_down_ratio": None,
    }

    # Limit up/down stats
    df_l, _, _ = load_csv("limit_stats")
    if df_l is not None:
        values_lu = pd.to_numeric(df_l["涨停数量"], errors="coerce").dropna()
        values_ld = pd.to_numeric(df_l["跌停数量"], errors="coerce").dropna()
        result["limit_stats"] = {
            "latest_limit_up": int(values_lu.iloc[-1]) if len(values_lu) > 0 else None,
            "latest_limit_down": int(values_ld.iloc[-1]) if len(values_ld) > 0 else None,
            "limit_up_ma5": round(float(values_lu.tail(5).mean()), 1) if len(values_lu) >= 5 else None,
            "limit_down_ma5": round(float(values_ld.tail(5).mean()), 1) if len(values_ld) >= 5 else None,
        }

    # Advance/decline
    df_ad, _, _ = load_csv("advance_decline")
    if df_ad is not None:
        up_col = "上涨家数" if "上涨家数" in df_ad.columns else [c for c in df_ad.columns if "上涨" in str(c)][0] if any("上涨" in str(c) for c in df_ad.columns) else None
        down_col = "下跌家数" if "下跌家数" in df_ad.columns else [c for c in df_ad.columns if "下跌" in str(c)][0] if any("下跌" in str(c) for c in df_ad.columns) else None

        if up_col and down_col:
            up_vals = pd.to_numeric(df_ad[up_col], errors="coerce").dropna()
            down_vals = pd.to_numeric(df_ad[down_col], errors="coerce").dropna()
            latest_up = int(up_vals.iloc[-1]) if len(up_vals) > 0 else None
            latest_down = int(down_vals.iloc[-1]) if len(down_vals) > 0 else None
            ratio = round(latest_up / max(latest_down, 1), 4) if latest_up is not None and latest_down and latest_down > 0 else None

            result["advance_decline"] = {
                "latest_up": latest_up,
                "latest_down": latest_down,
                "up_down_ratio": ratio,
                "up_ma5": round(float(up_vals.tail(5).mean()), 0) if len(up_vals) >= 5 else None,
                "down_ma5": round(float(down_vals.tail(5).mean()), 0) if len(down_vals) >= 5 else None,
            }
            result["up_down_ratio"] = ratio

    return result


def analyze_margin_cost(keep=250):
    """融资盘平均成本：按融资余额变化对指数收盘价做成本累积。

    买入（余额增加）按当日价计入成本；卖出（余额减少）不改变平均成本。
    结果可与最新指数比较，判断融资盘整体盈亏。
    """
    mb, _, _ = load_csv("margin_balance")
    idx, _, _ = load_csv("sh_index")
    if mb is None or idx is None:
        return None
    m = pd.DataFrame({"date": mb["_date"], "bal": pd.to_numeric(mb["_val"], errors="coerce")})
    s = pd.DataFrame({"date": idx["_date"], "px": pd.to_numeric(idx["_val"], errors="coerce")})
    d = m.merge(s, on="date").dropna().sort_values("date").reset_index(drop=True)
    if len(d) < 30:
        return None

    cost = None
    out = []
    for i in range(len(d)):
        px, bal = float(d.at[i, "px"]), float(d.at[i, "bal"])
        if cost is None or bal <= 0:
            cost = px
        else:
            db = bal - float(d.at[i - 1, "bal"])
            if db > 0:
                cost = (cost * (bal - db) + px * db) / bal
                # 余额减少时平均成本不变
        out.append(cost)
    d["cost"] = out

    last = d.iloc[-1]
    cur_px = float(last["px"])
    spread = (cur_px / float(last["cost"]) - 1) * 100 if last["cost"] else None
    tail = d.tail(keep)
    ts = [{"date": r["date"].strftime("%Y-%m-%d"),
           "cost": round(float(r["cost"]), 1),
           "index": round(float(r["px"]), 1)}
          for _, r in tail.iterrows()]

    return {
        "id": "margin_cost",
        "name": "融资盘成本",
        "latest_cost": round(float(last["cost"]), 1),
        "latest_index": round(cur_px, 1),
        "latest_date": last["date"].strftime("%Y-%m-%d"),
        "spread_pct": round(spread, 2) if spread is not None else None,
        "ts_data": ts,
    }


def analyze_margin():
    """融资余额：合计 + 沪/深拆分（若 CSV 含分市场列）。"""
    path = os.path.join(DATA_DIR, "margin_balance.csv")
    if not os.path.exists(path):
        return None
    try:
        raw = pd.read_csv(path, dtype={"日期": str})
    except Exception:
        return None
    if raw.empty or "沪深融资余额" not in raw.columns:
        return None
    raw["_date"] = pd.to_datetime(raw["日期"], errors="coerce")
    raw = raw.dropna(subset=["_date"]).sort_values("_date").reset_index(drop=True)

    total = pd.to_numeric(raw["沪深融资余额"], errors="coerce")
    has_split = "沪市融资余额" in raw.columns and "深市融资余额" in raw.columns
    sh = pd.to_numeric(raw["沪市融资余额"], errors="coerce") if has_split else None
    sz = pd.to_numeric(raw["深市融资余额"], errors="coerce") if has_split else None

    latest = float(total.iloc[-1]) if len(total) else None
    latest_date = raw["_date"].iloc[-1].strftime("%Y-%m-%d") if len(raw) else None

    ts_data = []
    for i in raw.tail(120).index:
        row = {"date": raw.at[i, "_date"].strftime("%Y-%m-%d"),
               "value": round(float(total.iloc[i]), 2)}
        if has_split and pd.notna(sh.iloc[i]):
            row["sh"] = round(float(sh.iloc[i]), 2)
        if has_split and pd.notna(sz.iloc[i]):
            row["sz"] = round(float(sz.iloc[i]), 2)
        ts_data.append(row)

    out = {
        "id": "margin_balance",
        "name": ("沪深融资余额" if has_split and pd.notna(sz.iloc[-1]) else "沪市融资余额"),
        "category": "sentiment",
        "latest_value": latest, "latest_date": latest_date, "unit": "亿元",
        "change_5d": compute_roc(total, 5), "change_20d": compute_roc(total, 20),
        "ts_data": ts_data,
    }
    if has_split:
        out["latest_sh"] = float(sh.iloc[-1]) if pd.notna(sh.iloc[-1]) else None
        out["latest_sz"] = float(sz.iloc[-1]) if pd.notna(sz.iloc[-1]) else None
    return out


def analyze_volume():
    """Analyze trading volume from market breadth data"""
    # Try to compute volume from index data
    vol_ts = []
    for idx_id in ["sh_index", "sz_index"]:
        df, date_col, val_col = load_csv(idx_id)
        if df is None:
            continue
        # Check if we have volume column
        for col in df.columns:
            if "volume" in str(col).lower() or "成交" in str(col):
                dates = df["_date"]
                vals = pd.to_numeric(df[col], errors="coerce")
                vol_ts.append((dates, vals, idx_id))
                break

    if not vol_ts:
        return None

    # Combine SH + SZ volumes
    result = {}
    for dates, vals, idx_id in vol_ts:
        # Latest
        latest_vol = float(vals.iloc[-1]) if len(vals) > 0 else None
        # Change rates
        ma5 = float(vals.tail(5).mean()) if len(vals) >= 5 else None
        ma20 = float(vals.tail(20).mean()) if len(vals) >= 20 else None
        change_5d = round((ma5 / float(vals.tail(10).mean()) - 1) * 100, 2) if ma5 and len(vals) >= 10 else None
        change_20d = round((ma20 / float(vals.tail(40).mean()) - 1) * 100, 2) if ma20 and len(vals) >= 40 else None

        result[idx_id] = {
            "latest_volume": latest_vol,
            "ma5": ma5,
            "ma20": ma20,
            "change_5d": change_5d,
            "change_20d": change_20d,
        }

    return result


def _analyze_one_nfgj(path, name, keep_days=None):
    """单个指数的 ETF 资金跟踪分析（读取 fetch_nfgj.py 产物）。"""
    if not os.path.exists(path):
        print(f"    [WARN] {os.path.basename(path)} 不存在，跳过")
        return None

    try:
        df = pd.read_csv(path, dtype={"date": str})
    except Exception as e:
        print(f"    [ERR] 读取 nfgj_300etf.csv 失败: {e}")
        return None
    if df.empty:
        return None

    df = df.sort_values("date").reset_index(drop=True)
    for c in ("etf_mv_yi", "pct", "idx_level", "amt_diff", "etf_amt", "float_mv", "shares_yi"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["pct_std"] = pd.to_numeric(df.get("pct_std"), errors="coerce")

    df["d20"] = df["pct"].diff(20)
    df["d60"] = df["pct"].diff(60)
    df["d1"] = df["pct"].diff()

    float_mv = float(df["float_mv"].iloc[-1])
    conv = float_mv / 1e8 / 100.0            # 亿元 / pp
    last = df.iloc[-1]

    def _f(v):
        return None if v is None or pd.isna(v) else round(float(v), 4)

    tail = df if not keep_days else df.tail(keep_days)
    ts = [{
        "date": r["date"],
        "pct": _f(r["pct"]),
        "pct_std": _f(r["pct_std"]),
        "idx_level": _f(r["idx_level"]),
        "amt_diff": _f(r["amt_diff"]),
        "etf_amt": _f(r["etf_amt"]),
        "etf_mv_yi": _f(r["etf_mv_yi"]),
        "d20": _f(r["d20"]),
        "d60": _f(r["d60"]),
    } for _, r in tail.iterrows()]

    # 峰值与极值标注（用于前端提示）
    i_peak = int(df["etf_mv_yi"].idxmax())
    s_amt = df.dropna(subset=["amt_diff"])
    i_amax = int(s_amt["amt_diff"].idxmax()) if len(s_amt) else None
    i_amin = int(s_amt["amt_diff"].idxmin()) if len(s_amt) else None

    return {
        "latest": {
            "date": str(last["date"]),
            "pct": _f(last["pct"]),
            "pct_std": _f(last["pct_std"]),
            "etf_mv_yi": _f(last["etf_mv_yi"]),
            "etf_amt": _f(last["etf_amt"]),
            "amt_diff": _f(last["amt_diff"]),
            "idx_level": _f(last["idx_level"]),
            "d20": _f(last["d20"]),
            "d60": _f(last["d60"]),
            "net20_yi": round(float(last["d20"]) * conv, 1) if pd.notna(last["d20"]) else None,
            "net60_yi": round(float(last["d60"]) * conv, 1) if pd.notna(last["d60"]) else None,
        },
        "float_mv_yi": round(float_mv / 1e8, 0),
        "float_mv_wanyi": round(float_mv / 1e12, 2),
        "conv_yi_per_pp": round(conv, 1),
        "n_etf": int(df["n_etf"].iloc[-1]) if "n_etf" in df.columns else None,
        "peak": {"date": str(df.at[i_peak, "date"]), "etf_mv_yi": _f(df.at[i_peak, "etf_mv_yi"])},
        "max_amt": ({"date": str(df.at[i_amax, "date"]), "amt_diff": _f(df.at[i_amax, "amt_diff"]),
                     "etf_amt": _f(df.at[i_amax, "etf_amt"])} if i_amax is not None else None),
        "min_amt": ({"date": str(df.at[i_amin, "date"]), "amt_diff": _f(df.at[i_amin, "amt_diff"]),
                     "etf_amt": _f(df.at[i_amin, "etf_amt"])} if i_amin is not None else None),
        "ts": ts,
    }


def _nfgj_frame(key):
    """读入单指数序列并预计算 20/60 日斜率与未来收益（在完整历史上算，避免窗口边缘缺值）。"""
    path = os.path.join(DATA_DIR, f"nfgj_{key}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, dtype={"date": str}).sort_values("date").reset_index(drop=True)
    except Exception:
        return None
    if len(df) < 60:
        return None
    df["pct"] = pd.to_numeric(df["pct"], errors="coerce")
    df["idx_level"] = pd.to_numeric(df["idx_level"], errors="coerce")
    df["d20"] = df["pct"].diff(20)
    df["d60"] = df["pct"].diff(60)
    df["f20"] = (df["idx_level"].shift(-20) / df["idx_level"] - 1) * 100
    df["f60"] = (df["idx_level"].shift(-60) / df["idx_level"] - 1) * 100
    return df


def _win(df, window):
    return df if not window else df.tail(window)


def analyze_nfgj_bucket(key="hs300", window=None):
    """20 日净申购分桶 → 未来 20/60 日收益（日频样本 vs 去重叠样本）。

    统计样本限定在所选时间区间内；d20 / 未来收益在完整历史上计算后切片，
    这样窗口起点也能有斜率与后续收益。
    """
    df = _nfgj_frame(key)
    if df is None:
        return None
    w = _win(df, window)
    d20 = w["d20"].dropna()
    if len(d20) < 30:
        return None

    qs = d20.quantile([0.10, 0.35, 0.65, 0.90])
    e1, e2, e3, e4 = [round(float(x), 3) for x in qs]
    bins = [-99, e1, e2, e3, e4, 99]
    labels = [f"<{e1:+.2f}", f"{e1:+.2f}~{e2:+.2f}", f"{e2:+.2f}~{e3:+.2f}",
              f"{e3:+.2f}~{e4:+.2f}", f">{e4:+.2f}"]

    bucket = pd.cut(w["d20"], bins=bins, labels=labels)
    g = pd.DataFrame({"b": bucket, "f20": w["f20"], "f60": w["f60"]})
    g["win"] = (g["f20"] > 0).astype(float)
    daily = g.groupby("b", observed=False).agg(v20=("f20", "mean"), v60=("f60", "mean"),
                                               n=("f20", "count"), win=("win", "mean"))
    sub = g.iloc[::20]
    indep = sub.groupby("b", observed=False).agg(v20=("f20", "mean"), v60=("f60", "mean"), n=("f20", "count"))

    def _r(x):
        return None if x is None or pd.isna(x) else round(float(x), 2)

    return {
        "labels": labels,
        "daily20": [_r(daily["v20"].get(l)) for l in labels],
        "daily60": [_r(daily["v60"].get(l)) for l in labels],
        "indep20": [_r(indep["v20"].get(l)) for l in labels],
        "indep60": [_r(indep["v60"].get(l)) for l in labels],
        "n_daily": [int(daily["n"].get(l) or 0) for l in labels],
        "win20": [(None if pd.isna(daily["win"].get(l)) else round(float(daily["win"].get(l))*100, 1)) for l in labels],
        "edges": [e1, e2, e3, e4],
        "n_indep": [int(indep["n"].get(l) or 0) for l in labels],
        "start": str(w["date"].iloc[0]),
        "end": str(w["date"].iloc[-1]),
        "window": window,
        "key": key,
    }


def analyze_nfgj_episode(key="hs300"):
    """本轮增持：峰值 → 低点 → 至今，按份额变化×价格估算累计申购金额。"""
    df = _nfgj_frame(key)
    if df is None or "shares_yi" not in df.columns:
        return None
    mv = df["etf_mv_yi"]
    sh = df["shares_yi"]
    if mv.isna().all() or sh.isna().all():
        return None
    px = mv / sh

    i_pk = int(mv.idxmax())
    peak, peak_d = float(mv.iloc[i_pk]), df.at[i_pk, "date"]
    tail = df.loc[i_pk:]
    i_tr = int(tail["etf_mv_yi"].idxmin())
    trough, trough_d = float(mv.iloc[i_tr]), df.at[i_tr, "date"]
    now, now_d = float(mv.iloc[-1]), str(df["date"].iloc[-1])

    seg = df.loc[i_tr:]
    d_sh = seg["shares_yi"].diff()
    added = float((d_sh * (seg["etf_mv_yi"] / seg["shares_yi"])).sum())
    idx_chg = (float(df["idx_level"].iloc[-1]) / float(df["idx_level"].iloc[i_tr]) - 1) * 100
    gap = peak - now
    dd = (trough / peak - 1) * 100 if peak else 0.0      # 低点距峰值（回撤%）

    def d(x):
        return f"{x[:4]}-{x[4:6]}-{x[6:]}"

    if added <= max(1.0, 0.01 * trough):
        # 低点即当前、或回升可忽略：仍处赎回阶段
        txt = (f"仍处赎回阶段：自 {d(peak_d)} 的峰值 {peak:,.0f} 亿元回落至当前 {now:,.0f} 亿元，"
               f"累计减少 {abs(now-peak):,.0f} 亿元（较峰值 {dd:+.0f}%），尚未出现份额回升。")
        tone = "down"
    elif added > 0:
        txt = (f"自 {d(trough_d)} 的低点（{trough:,.0f} 亿，较峰值回落 {abs(dd):.0f}%）以来，ETF 份额持续回升，"
               f"按份额变化估算累计增持约 {added:,.0f} 亿元；当前规模 {now:,.0f} 亿元，"
               f"距 {d(peak_d)} 的峰值 {peak:,.0f} 亿元还差 {gap:,.0f} 亿元（缺口 {gap/peak*100:.0f}%）。"
               f"同期指数 {idx_chg:+.1f}%。")
        tone = "up"
    else:
        txt = (f"自 {d(peak_d)} 的峰值（{peak:,.0f} 亿）以来份额仍在收缩，"
               f"当前规模 {now:,.0f} 亿元，累计减少约 {abs(added):,.0f} 亿元，"
               f"距峰值还差 {gap:,.0f} 亿元。同期指数 {idx_chg:+.1f}%。")
        tone = "down"

    return {
        "h": "本轮增持",
        "t": txt,
        "tone": tone,
        "peak_date": peak_d, "peak_yi": round(peak, 1),
        "trough_date": trough_d, "trough_yi": round(trough, 1),
        "now_date": now_d, "now_yi": round(now, 1),
        "added_yi": round(added, 1), "gap_yi": round(gap, 1),
        "drawdown_pct": round(dd, 1), "gap_pct": round(-gap / peak * 100, 1) if peak else None,
    }


def analyze_nfgj_narrative(key="hs300", window=None):
    """基于所选区间的占比序列自动生成文字解读。"""
    df = _nfgj_frame(key)
    if df is None:
        return None
    w = _win(df, window)
    if len(w.dropna(subset=["d20"])) < 20:
        return None

    conv = float(df["float_mv"].iloc[-1]) / 1e8 / 100.0
    last = df.iloc[-1]
    d20 = float(last["d20"]) if pd.notna(last["d20"]) else 0.0
    d60 = float(last["d60"]) if pd.notna(last["d60"]) else 0.0

    def d(x):
        return f"{x[:4]}-{x[4:6]}-{x[6:]}"

    def yi(v):
        return f"{v:+,.0f} 亿"

    q10, q40, q60, q90 = [float(w["d20"].quantile(x)) for x in (0.10, 0.40, 0.60, 0.90)]
    if d20 >= q90:
        state, tone = "强申购（护盘中）", "up"
    elif d20 >= q60:
        state, tone = "温和申购", "up"
    elif d20 > q40:
        state, tone = "中性", "neutral"
    elif d20 > q10:
        state, tone = "温和赎回", "down"
    else:
        state, tone = "强赎回（撤托底）", "down"

    span = f"{d(w['date'].iloc[0])} ~ {d(w['date'].iloc[-1])}"
    items = [{
        "h": "当前状态",
        "t": (f"{state}。最近 20 日占比变化 {d20:+.3f}pp，折算约 {yi(d20*conv)}；"
              f"60 日 {d60:+.3f}pp，约 {yi(d60*conv)}。"
              f"本区间（{span}）的分位阈值为：≥{q90:+.3f}pp 视为强申购、"
              f"{q60:+.3f}~{q90:+.3f} 温和申购、{q40:+.3f}~{q60:+.3f} 中性、"
              f"{q10:+.3f}~{q40:+.3f} 温和赎回、≤{q10:+.3f} 强赎回。"
              f"阈值按所选区间与本指数自身分布计算，不同指数之间不可直接比较。"),
        "tone": tone,
    }]

    s = w.dropna(subset=["d20"]).set_index("date")["d20"]
    rmax = s.rolling(41, center=True, min_periods=1).max()
    rmin = s.rolling(41, center=True, min_periods=1).min()
    peaks = s[(s == rmax) & (s >= q90)].sort_values(ascending=False).head(2)
    troughs = s[(s == rmin) & (s <= q10)].sort_values().head(1)

    def _after(date, n):
        pos = df.index[df["date"] == date]
        if len(pos) == 0 or pos[0] + n >= len(df):
            return None
        return (df["idx_level"].iloc[pos[0] + n] / df["idx_level"].iloc[pos[0]] - 1) * 100

    def _seg(date):
        a = _after(date, 20)
        b = _after(date, 60)
        txt = f"{d(date)} 净申购 {yi(s[date]*conv)}"
        if a is not None and b is not None:
            return txt + f"（之后 20 日 {a:+.1f}%、60 日 {b:+.1f}%）", True
        return txt + "（后续样本尚未走完）", False

    if len(peaks):
        segs, done = zip(*[_seg(x) for x in peaks.index])
        segs, done = list(segs), list(done)
        if all(done):
            tail = "这两次都出现在指数的阶段性低位，之后指数走高，符合“托底”的行为特征。"
        elif any(done):
            tail = "其中一次已可观察到后续走势并走高，另一次样本尚未走完，暂不作方向判断。"
        else:
            tail = "后续样本尚未走完，暂不作方向判断。"
        items.append({"h": "区间内申购时点",
                      "t": "区间内最强的两次 20 日申购为：" + "；".join(segs) + "。" + tail,
                      "tone": "up"})

    if len(troughs):
        tdate = troughs.index[0]
        tval = float(troughs.iloc[0])
        r = df[df["date"] == tdate]
        idx0 = float(r["idx_level"].iloc[0]) if len(r) else None
        a = _after(tdate, 20)
        b = _after(tdate, 60)
        outcome, tone = "", "down"
        if a is not None and b is not None:
            outcome = f"，之后 20 日 {a:+.1f}%、60 日 {b:+.1f}%"
            if a > 0 and b > 0:
                tone = "neutral"
        items.append({"h": "区间内赎回时点",
                      "t": (f"区间内最大的一次 20 日赎回发生在 {d(tdate)}，幅度 {tval:+.3f}pp"
                            f"（约 {yi(tval*conv)}），当日指数 {idx0:,.0f}{outcome}。"
                            f"方向上，买入是“托底”，卖出是“撤掉托底”；"
                            f"但单次时点不构成顶底判断，需结合估值与外部环境。"),
                      "tone": tone})

    weak = w[w["d20"] <= q10]
    if len(weak) > 5:
        m20 = weak["f20"].mean()
        m60 = weak["f60"].mean()
        base20 = w["f20"].mean()
        base60 = w["f60"].mean()
        if (pd.isna(m20) or pd.isna(base20)):
            verdict, vtone = "样本不足以判断方向。", "neutral"
        elif m20 < base20 and (pd.isna(m60) or m60 < base60):
            verdict, vtone = "净赎回明显放大的阶段，后续收益低于全区间平均，方向偏弱。", "down"
        else:
            verdict, vtone = "与全区间平均相比并未走弱，说明该阈值下的方向性证据不稳定。", "neutral"
        items.append({"h": "反向情形",
                      "t": (f"区间内 20 日占比变化 ≤ {q10:+.3f}pp（最弱的 10% 情形）共 {len(weak)} 天，"
                            f"其后 20 日平均 {0 if pd.isna(m20) else m20:+.2f}%、"
                            f"60 日平均 {0 if pd.isna(m60) else m60:+.2f}%"
                            f"（全区间同期为 20 日 {0 if pd.isna(base20) else base20:+.2f}%、"
                            f"60 日 {0 if pd.isna(base60) else base60:+.2f}%）。{verdict}"),
                      "tone": vtone})

    lo, hi = float(w["idx_level"].min()), float(w["idx_level"].max())
    p_now = float(last["pct"])
    rank = int((w["d20"] <= d20).sum() / max(1, w["d20"].notna().sum()) * 100)
    pos = (float(last["idx_level"]) - lo) / (hi - lo) * 100 if hi > lo else 0
    ref = w.iloc[max(0, len(w) - 21)]
    ref_yi = float(ref["etf_mv_yi"]) if pd.notna(ref.get("etf_mv_yi")) else None
    now_yi = float(last["etf_mv_yi"]) if pd.notna(last.get("etf_mv_yi")) else None
    idx_chg = (float(last["idx_level"]) / float(ref["idx_level"]) - 1) * 100
    # 当前 d20 落入哪个桶 → 引用该桶的历史未来收益与胜率
    bk_label = bk_n = bk_v20 = bk_win = None
    try:
        bk = analyze_nfgj_bucket(key, window)
        if bk and bk.get("edges"):
            e = bk["edges"]
            idx_b = min(max(sum(1 for x in e if d20 > x), 0), 4)
            if bk["daily20"][idx_b] is not None:
                bk_label = bk["labels"][idx_b]
                bk_n = bk["n_daily"][idx_b]
                bk_v20 = bk["daily20"][idx_b]
                bk_win = bk["win20"][idx_b]
    except Exception:
        pass

    sents = []
    cur_idx = float(last["idx_level"])
    tpct = float((w["idx_level"] <= cur_idx).mean() * 100)     # 时间分位：多少比例的交易日低于当前
    y1 = df.tail(250)
    lo1, hi1 = float(y1["idx_level"].min()), float(y1["idx_level"].max())
    pos1 = (cur_idx - lo1) / (hi1 - lo1) * 100 if hi1 > lo1 else 0
    edge1 = "区间下沿" if pos1 <= 15 else ("区间上沿" if pos1 >= 85 else f"区间中段（{pos1:.0f}%）")
    _line = (f"指数 {cur_idx:,.0f}，处于本区间 {lo:,.0f}~{hi:,.0f} 的 {pos:.0f}% 位置"
             f"（时间分位 {tpct:.0f}%，约 {tpct:.0f}% 的交易日低于当前）")
    if len(w) > 375:      # 区间长于一年时才补充近一年视角，避免与「本区间」重复
        _line += f"；近一年区间 {lo1:,.0f}~{hi1:,.0f}，当前位于{edge1}"
    sents.append(_line + "。")
    p_rank = float((w["pct"] <= p_now).mean() * 100)
    sents.append(f"ETF 占比 {p_now:.3f}%，处于区间内 {p_rank:.0f}% 分位；"
                 f"20 日斜率处于区间内 {rank}% 分位。")
    if ref_yi and now_yi:
        mv_g = now_yi / ref_yi - 1
        share_g = (1 + mv_g) / (1 + idx_chg / 100.0) - 1 if idx_chg > -100 else 0
        if mv_g > 0.02 and share_g > max(0.02, 0.5 * mv_g):
            attrib = f"增量主要来自份额（估算份额 {share_g*100:+.0f}%），而非价格上涨"
        elif mv_g > 0.02 and share_g < 0.2 * mv_g:
            attrib = f"增量主要来自价格上涨，份额变化有限（估算 {share_g*100:+.0f}%）"
        elif mv_g < -0.02:
            attrib = f"规模下降主要来自份额收缩（估算份额 {share_g*100:+.0f}%）"
        else:
            attrib = f"份额与价格共同作用（估算份额 {share_g*100:+.0f}%）"
        sents.append(f"近一个月 ETF 规模从 {ref_yi:,.0f} 亿增至 {now_yi:,.0f} 亿"
                     f"（{now_yi-ref_yi:+,.0f} 亿），同期指数 {idx_chg:+.1f}%，{attrib}。")
    strong = w[w["d20"] >= q90]["idx_level"]
    if len(strong) >= 3:
        cost = float(strong.mean())
        rel = (float(last["idx_level"]) / cost - 1) * 100
        if rel > 2:
            sents.append(f"当前指数比区间内强申购时点的平均成本 {cost:,.0f} 高约 {rel:.0f}%，"
                         f"主要买入资金大概率处于浮盈状态。")
        elif rel < -2:
            sents.append(f"当前指数比区间内强申购时点的平均成本 {cost:,.0f} 低约 {abs(rel):.0f}%。")
        else:
            sents.append(f"当前指数与区间内强申购时点的平均成本 {cost:,.0f} 基本持平。")
    if bk_v20 is not None:
        tail = f"按本区间分桶，当前斜率落在“{bk_label}”组（{bk_n} 天），其后 20 日平均 {bk_v20:+.1f}%"
        if bk_win is not None:
            tail += f"、胜率 {bk_win:.0f}%"
            if bk_v20 > 0 and bk_win < 50:
                tail += "；均值由少数大涨贡献，方向性并不稳固"
        sents.append(tail + "。")
    sents.append("盘中份额于 T 日收盘后披露，需次日确认。")

    items.append({"h": "当下定位", "t": "".join(sents), "tone": "neutral"})

    return {
        "key": key,
        "state": state,
        "tone": tone,
        "window": window,
        "start": str(w["date"].iloc[0]),
        "end": str(w["date"].iloc[-1]),
        "as_of": str(last["date"]),
        "items": items,
    }


def analyze_nfgj(keep_days=None):
    """宽基 ETF 资金跟踪：按 config.yaml → nfgj.indices 逐个指数分析。"""
    cfg = (load_config().get("nfgj") or {})
    groups = cfg.get("indices") or []
    items = []
    for g in groups:
        path = os.path.join(DATA_DIR, f"nfgj_{g['key']}.csv")
        one = _analyze_one_nfgj(path, g["name"], keep_days)
        if one:
            one["key"] = g["key"]
            one["name"] = g["name"]
            items.append(one)
    if not items:
        return None
    # 按「指数 × 时间区间」预计算解读与分桶，前端按当前区间取用
    ranges = [("1m", 21), ("6m", 121), ("1y", 242), ("3y", 726), ("5y", 1210), ("all", None)]
    buckets, narratives, episodes = {}, {}, {}
    for g in groups:
        buckets[g["key"]] = {}
        narratives[g["key"]] = {}
        epi = analyze_nfgj_episode(g["key"])
        episodes[g["key"]] = epi
        for rname, n in ranges:
            buckets[g["key"]][rname] = analyze_nfgj_bucket(g["key"], n)
            rn = analyze_nfgj_narrative(g["key"], n)
            if rn and epi:
                rn["items"].append({k: epi[k] for k in ("h", "t", "tone")})
            narratives[g["key"]][rname] = rn
    return {
        "items": items,
        "buckets": buckets,
        "narratives": narratives,
        "episodes": episodes,
        "ranges": [r for r, _ in ranges],
        # 兼容旧字段
        "bucket": (buckets.get("hs300") or {}).get("all"),
        "narrative": (narratives.get("hs300") or {}).get("all"),
    }


def run_analysis():
    config = load_config()
    chart_cfg = config.get("chart", {})
    sent_cfg = config.get("sentiment", {})

    results = {
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "major_indices": [],
        "broad_indices": [],
        "style_indices": [],
        "industry_indices": [],
        "market_breadth": {},
        "volume": {},
        "sentiment": {},
        "etf_valuation": [],
        "fear_greed_index": None,
    }

    # ── 1. 大盘指数 ──
    print("  分析: 大盘指数...")
    for ind in config.get("indicators", {}).get("major_indices", []):
        entry = analyze_index(ind["id"], ind["name"], ind, "major_index")
        if entry:
            results["major_indices"].append(entry)
            print(f"    [OK] {ind['name']}: {entry['latest_value']:.2f} ({entry['change_1d']:+.2f}%)")

    # ── 2. 宽基指数 ──
    print("  分析: 宽基指数...")
    for ind in config.get("indicators", {}).get("broad_indices", []):
        entry = analyze_index(ind["id"], ind["name"], ind, "broad_index")
        if entry:
            results["broad_indices"].append(entry)
            print(f"    [OK] {ind['name']}: {entry['latest_value']:.2f} ({entry['change_1d']:+.2f}%)")

    # ── 3. 风格指数 ──
    print("  分析: 风格指数...")
    for ind in config.get("indicators", {}).get("style_indices", []):
        entry = analyze_index(ind["id"], ind["name"], ind, "style_index")
        if entry:
            results["style_indices"].append(entry)
            print(f"    [OK] {ind['name']}: {entry['latest_value']:.2f} ({entry['change_1d']:+.2f}%)")

    # ── 4. 申万行业指数 ──
    print("  分析: 行业指数...")
    from fetch_data import SW_INDUSTRY_CODES, SW_NAMES
    for ind_id, sw_code in SW_INDUSTRY_CODES.items():
        name = SW_NAMES.get(ind_id, ind_id)
        entry = analyze_industry(ind_id, name)
        if entry:
            results["industry_indices"].append(entry)
            print(f"    [OK] {name}: {entry['change_1d']:+.2f}%" if entry['change_1d'] else f"    [OK] {name}")

    # Sort industry by latest change_1d
    results["industry_indices"].sort(key=lambda x: x.get("change_1d") or 0, reverse=True)

    # ── 合并行业PE估值到行业指数 ──
    industry_pe_file = os.path.join(DATA_DIR, "industry_pe.csv")
    if os.path.exists(industry_pe_file):
        try:
            df_pe = pd.read_csv(industry_pe_file, encoding="utf-8-sig")
            for idx_entry in results["industry_indices"]:
                sw_name = idx_entry.get("name", "")
                row = df_pe[df_pe["行业"] == sw_name]
                if not row.empty:
                    idx_entry["pe_industry"] = float(row["PE中位数"].iloc[0]) if pd.notna(row["PE中位数"].iloc[0]) else None
                else:
                    idx_entry["pe_industry"] = None
            print("    [OK] 行业PE数据已合并")
        except Exception as e:
            print(f"    [WARN] 行业PE合并失败: {e}")

    # ── 5. 市场宽度 ──
    print("  分析: 市场宽度...")
    results["market_breadth"] = analyze_market_breadth()

    # ── 6. 成交量 ──
    print("  分析: 成交量...")
    results["volume"] = analyze_volume()

    # ── 7. 融资余额 ──
    print("  分析: 融资余额...")
    margin = analyze_margin()
    if margin:
        results["sentiment"]["margin_balance"] = margin
    mcost = analyze_margin_cost()
    if mcost:
        results["sentiment"]["margin_cost"] = mcost

    # ── 8. 恐贪指数 ──
    print("  计算: 恐贪指数...")
    mb = results["market_breadth"]
    mr = margin

    # Extract factors
    margin_chg = mr.get("change_5d", 0) if mr else None
    up_down = mb.get("up_down_ratio") if mb else None

    # Volume deviation: use sh_index volume if available
    vol_dev = None
    vol_data = results.get("volume", {})
    if vol_data:
        for k in ["sh_index", "sz_index"]:
            if k in vol_data and vol_data[k].get("change_5d"):
                vol_dev = vol_data[k]["change_5d"]
                break

    # Volatility: 中证800 近 20 日年化波动
    volatility = compute_index_volatility(os.path.join(DATA_DIR, "zz800.csv"))
    if volatility is None:
        print("  [WARN] zz800.csv 数据不足，波动率因子跳过")

    fear_greed = compute_fear_greed_index(
        margin_change=margin_chg,
        up_down_ratio=up_down,
        volume_deviation=vol_dev,
        volatility=volatility,
    )
    results["fear_greed_index"] = fear_greed

    # ── 8.5 市场拥挤度 ──
    crowding_file = os.path.join(DATA_DIR, "market_crowding.csv")
    if os.path.exists(crowding_file):
        try:
            df_cr = pd.read_csv(crowding_file, encoding="utf-8-sig")
            if not df_cr.empty:
                df_cr["_v"] = pd.to_numeric(df_cr["拥挤度"], errors="coerce")
                valid = df_cr[df_cr["_v"].notna()]
                if len(valid):
                    # 抓取失败的当日行(拥挤度为空)不参与，取最后一个有效值
                    latest = valid.iloc[-1]
                    cr_ts = [{"date": str(r["日期"]), "value": float(r["_v"])}
                             for _, r in valid.iterrows()]
                    results["market_crowding"] = {
                        "latest_crowding": round(float(latest["_v"]), 2),
                        "latest_date": str(latest["日期"]),
                        "total_amount": float(latest.get("总成交额", 0)),
                        "top5_amount": float(latest.get("前5%成交额", 0)),
                        "ts_data": cr_ts,
                    }
                    print(f"    市场拥挤度: {results['market_crowding']['latest_crowding']}%")
        except Exception as e:
            print(f"    [WARN] 市场拥挤度读取失败: {e}")
    else:
        results["market_crowding"] = {"latest_crowding": None}

    # ── 8.6 小/微盘成交占比（长历史代理）──
    proxy_path = os.path.join(DATA_DIR, "crowding_proxy.csv")
    if os.path.exists(proxy_path):
        try:
            pr = pd.read_csv(proxy_path, dtype={"日期": str})
            for c in ("小盘占比", "小微盘占比", "中证800占比",
                      "沪深300占比", "中证500占比", "北交所占比",
                      "北证50占比", "北交所其他占比", "其他占比",
                      "hs300额", "zz500额", "小盘成交额", "小微盘成交额",
                      "其他成交额", "北证50成交额", "北交所其他成交额"):
                pr[c] = pd.to_numeric(pr.get(c), errors="coerce")
            pr = pr.dropna(subset=["小微盘占比"])
            if len(pr):
                def _v(x):
                    return None if pd.isna(x) else round(float(x), 2)

                ts = [{"date": str(r["日期"]),
                       "small": _v(r.get("小盘占比")),
                       "micro": _v(r.get("小微盘占比")),
                       "csi800": _v(r.get("中证800占比")),
                       "hs300": _v(r.get("沪深300占比")),
                       "zz500": _v(r.get("中证500占比")),
                       "other": _v(r.get("其他占比")),
                       "bj": _v(r.get("北交所占比")),
                       "bj50": _v(r.get("北证50占比")),
                       "bjother": _v(r.get("北交所其他占比")),
                       "hs300_amt": _v(r.get("hs300额")),
                       "zz500_amt": _v(r.get("zz500额")),
                       "small_amt": _v(r.get("小盘成交额")),
                       "micro_amt": _v(r.get("小微盘成交额")),
                       "other_amt": _v(r.get("其他成交额")),
                       "bj50_amt": _v(r.get("北证50成交额")),
                       "bjother_amt": _v(r.get("北交所其他成交额"))}
                      for _, r in pr.iterrows()]
                _tot = []
                for t in ts:
                    bj = (t.get("bj50_amt") or 0) + (t.get("bjother_amt") or 0)
                    t["bj_amt"] = round(bj, 0)
                    hs = sum((t.get(k) or 0) for k in ("hs300_amt", "zz500_amt",
                                                       "small_amt", "micro_amt", "other_amt"))
                    t["total_amt"] = round(hs, 0)
                    _tot.append(hs)
                for i, t in enumerate(ts):
                    t["d_total_amt"] = round(_tot[i] - _tot[i - 1], 0) if i > 0 else None

                last = pr.iloc[-1]
                results["market_crowding"]["proxy"] = {
                    "latest_small": _v(last.get("小盘占比")),
                    "latest_micro": _v(last.get("小微盘占比")),
                    "latest_csi800": _v(last.get("中证800占比")),
                    "latest_date": str(last["日期"]),
                    "ts_data": ts,
                }
                print(f"    小盘/小微盘成交占比: {last.get('小盘占比')}% / {last['小微盘占比']}%")
        except Exception as e:
            print(f"    [WARN] 小/微盘成交占比读取失败: {e}")

    # ── 8.7 市值结构时序 ──
    cap_path = os.path.join(DATA_DIR, "cap_structure.csv")
    if os.path.exists(cap_path):
        try:
            cp = pd.read_csv(cap_path, dtype={"日期": str})
            keys = {"hs300": "沪深300市值占比", "zz500": "中证500市值占比",
                    "small": "中证1000市值占比", "micro": "中证2000市值占比",
                    "other": "其他市值占比", "bj50": "北证50市值占比",
                    "bjother": "北交所其他市值占比"}
            mvcols = {"hs300_mv": "沪深300市值", "zz500_mv": "中证500市值",
                      "small_mv": "中证1000市值", "micro_mv": "中证2000市值",
                      "other_mv": "其他市值", "bj50_mv": "北证50市值",
                      "bjother_mv": "北交所其他市值"}
            for c in list(keys.values()) + list(mvcols.values()):
                cp[c] = pd.to_numeric(cp.get(c), errors="coerce")
            cp = cp.dropna(subset=["沪深300市值占比"])
            if len(cp):
                def _v(x):
                    return None if pd.isna(x) else round(float(x), 2)
                ts = [{"date": str(r["日期"]),
                       **{k: _v(r[col]) for k, col in keys.items()},
                       **{k: _v(r[col]) for k, col in mvcols.items()}}
                      for _, r in cp.iterrows()]
                for i, t in enumerate(ts):
                    bj50 = t.get("bj50_mv") or 0
                    bjo = t.get("bjother_mv") or 0
                    t["bj"] = round((t.get("bj50") or 0) + (t.get("bjother") or 0), 2)
                    t["bj_mv"] = round(bj50 + bjo, 0)
                    t["total_mv"] = round(sum((t.get(k) or 0) for k in
                                              ("hs300_mv", "zz500_mv", "small_mv",
                                               "micro_mv", "other_mv")), 0)
                for i, t in enumerate(ts):
                    t["d_total_mv"] = (round(t["total_mv"] - ts[i - 1]["total_mv"], 0)
                                       if i > 0 else None)

                last = cp.iloc[-1]
                results["market_crowding"]["cap"] = {
                    "latest": {k: _v(last[col]) for k, col in keys.items()},
                    "latest_date": str(last["日期"]),
                    "ts_data": ts,
                }
                print(f"    市值结构: 沪深300 {last['沪深300市值占比']}% "
                      f"中证500 {last['中证500市值占比']}% 1000 {last['中证1000市值占比']}% "
                      f"2000 {last['中证2000市值占比']}%")
        except Exception as e:
            print(f"    [WARN] 市值结构读取失败: {e}")

    # ── 9. ETF 估值 ──
    print("  分析: 宽基估值...")
    # Also look for valuation CSV files
    val_files = [f for f in os.listdir(DATA_DIR) if f.startswith("valuation_") and f.endswith(".csv")]
    name_map = {
        "valuation_000300": "沪深300估值",
        "valuation_000510": "中证A500估值",
        "valuation_000016": "上证50估值",
        "valuation_000905": "中证500估值",
        "valuation_000852": "中证1000估值",
        "valuation_399006": "创业板指估值",
        "valuation_000688": "科创50估值",
        "valuation_000009": "上证380估值",
        "valuation_000906": "中证800估值",
        "valuation_000932": "中证2000估值",
        "valuation_399006": "创业板指估值",
        "valuation_000688": "科创50估值",
        "valuation_399303": "国证2000估值",
    }
    for val_file in sorted(val_files):
        val_id = val_file.replace(".csv", "")
        val_name = name_map.get(val_id, val_id)
        entry = analyze_valuation(val_id, val_name)
        if entry:
            results["etf_valuation"].append(entry)
            print(f"    [OK] {val_name}: PE={entry['pe_ttm']} (分位{entry['pe_pct_5y']}%)")

    # ── 合并宽基PE估值到宽基指数（必须在ETF估值生成后执行）──
    broad_pe_map = {
        "hs300": "valuation_000300", "sz50": "valuation_000016",
        "a500": "valuation_000510",
        "zz800": "valuation_000906",
        "chinext_broad": "valuation_399006",
        "zz2000": "valuation_000932",
        "gz2000": "valuation_399303",
        "microcap": "valuation_399303",
        "zz500": "valuation_000905", "zz1000": "valuation_000852",
        "star50_index": "valuation_000688", "zz2000": "valuation_000932",
    }
    val_lookup = {}
    for v in results.get("etf_valuation", []):
        val_lookup[v["id"]] = v
    for idx_entry in results.get("broad_indices", []):
        val_id = broad_pe_map.get(idx_entry["id"])
        if val_id and val_id in val_lookup:
            v = val_lookup[val_id]
            idx_entry["pe_valuation"] = v.get("pe_ttm")
            idx_entry["pe_pct"] = v.get("pe_pct_all")
        else:
            idx_entry["pe_valuation"] = None
            idx_entry["pe_pct"] = None
    print("    [OK] 宽基PE已合并")

    # ── 10. 宽基 ETF 资金跟踪（ETF 总市值/指数流通市值及斜率）──
    print("  分析: ETF资金跟踪...")
    results["nfgj"] = analyze_nfgj()
    if results["nfgj"]:
        for n in results["nfgj"]["items"]:
            L = n["latest"]
            print(f"    [OK] {n['name']}: 占比 {L['pct']:.2f}%  规模 {L['etf_mv_yi']:.0f} 亿  "
                  f"20日 {L['net20_yi']:+,.0f} 亿  60日 {L['net60_yi']:+,.0f} 亿")

    # ── Save ──
    os.makedirs(DATA_DIR, exist_ok=True)
    results = _json_safe(results)
    with open(ANALYSIS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, allow_nan=False)

    # Generate trimmed version for frontend (smaller file)
    trimmed = json.loads(json.dumps(results, ensure_ascii=False))
    for key in ["major_indices", "broad_indices", "style_indices", "industry_indices"]:
        for ind in trimmed.get(key, []):
            ts = ind.get("ts_data", [])
            if len(ts) > 60:
                ind["ts_data"] = ts[-60:]
    for entry in trimmed.get("etf_valuation", []):
        ts = entry.get("ts_data", [])
        if len(ts) > 120:
            entry["ts_data"] = ts[-120:]

    trimmed_path = os.path.join(DATA_DIR, "analysis_trimmed.json")
    with open(trimmed_path, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False)
    print(f"\n  精简版已保存至: {trimmed_path}")

    js_path = os.path.join(DATA_DIR, "analysis_data.js")
    js_str = json.dumps(trimmed, ensure_ascii=False)
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.__ANALYSIS_DATA__ = ")
        f.write(js_str)
        f.write(";\n")
    print(f"  JS数据已保存至: {js_path}")

    print(f"\n  分析结果已保存至: {ANALYSIS_OUTPUT}")
    print(f"  大盘指数: {len(results['major_indices'])} | 宽基: {len(results['broad_indices'])} | "
          f"风格: {len(results['style_indices'])} | 行业: {len(results['industry_indices'])}")
    print(f"  恐贪指数: {results['fear_greed_index']}")

    return results


if __name__ == "__main__":
    run_analysis()
