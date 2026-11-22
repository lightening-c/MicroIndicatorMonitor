# -*- coding: utf-8 -*-
"""导出宽基 ETF 资金跟踪四面板图（与原 matplotlib 版同格式）。

读取 data/nfgj_{key}.csv，输出 exports/{name}_ETF资金跟踪.png
用法：python src/export_nfgj_charts.py [key ...]
"""
import os
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

from paths import DATA_DIR as DATA, EXPORT_DIR as OUT

os.makedirs(OUT, exist_ok=True)

GROUPS = [
    ("sz50", "上证50"), ("hs300", "沪深300"), ("zz500", "中证500"), ("zz1000", "中证1000"),
    ("zz2000", "中证2000"),
    ("star50", "科创50"), ("chinext", "创业板指"), ("broker", "证券ETF"),
]
EVENTS = [
    ("2024-01-22", "国家队大额申购"), ("2024-02-06", "汇金公告扩大增持"),
    ("2024-09-23", "政策组合拳前夜大额申购"), ("2025-04-07", "关税冲击后护盘"),
    ("2026-01-15", "转为大额赎回"), ("2026-10-09", "重新净申购"),
]
RED, GRN = "#d62728", "#2ca02c"


def plot_one(key, name):
    path = os.path.join(DATA, f"nfgj_{key}.csv")
    if not os.path.exists(path):
        print(f"skip {key}: no csv")
        return None
    df = pd.read_csv(path, dtype={"date": str}).sort_values("date").reset_index(drop=True)
    if df.empty:
        return None
    df["dt"] = pd.to_datetime(df["date"])
    for c in ("pct", "idx_level", "etf_amt", "amt_diff", "etf_mv_yi", "float_mv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["d20"] = df["pct"].diff(20)
    df["d60"] = df["pct"].diff(60)

    conv = float(df["float_mv"].iloc[-1]) / 1e8 / 100.0      # 亿元/pp
    fig, axes = plt.subplots(4, 1, figsize=(15.5, 13.8), sharex=True,
                             gridspec_kw={"height_ratios": [2.4, 1.15, 1.15, 0.95],
                                          "hspace": 0.16})

    # ── 面板1 ──
    ax = axes[0]
    ax2 = ax.twinx()
    ax2.plot(df["dt"], df["idx_level"], color="#c0c0c0", lw=1.3, label=f"{name} 指数(右轴)")
    ax.plot(df["dt"], df["pct"], color="#1f5fbf", lw=2.0, label="ETF总市值 / 指数流通市值(左轴)")
    ax3 = ax.twinx()
    ax3.spines["right"].set_position(("outward", 62))
    ax3.bar(df["dt"], df["amt_diff"], width=1.0, color="#e8850c", alpha=0.45,
            label="每日放量/缩量")
    ax3.axhline(0, color="#e8850c", lw=0.6, alpha=0.35)
    ax3.set_ylabel("成交额日变化 (亿元)", color="#e8850c", fontsize=9)
    ax3.tick_params(axis="y", colors="#e8850c", labelsize=8.5)
    lo, hi = ax3.get_ylim()
    ax3.set_ylim(lo * 1.28, hi * 1.28)
    ax3.set_zorder(0.2); ax3.patch.set_visible(False)
    ax2.set_zorder(0.5); ax2.patch.set_visible(False)
    ax.set_zorder(1); ax.patch.set_visible(False)

    s = df.dropna(subset=["amt_diff"])
    if len(s):
        i_max, i_min, i_last = s["amt_diff"].idxmax(), s["amt_diff"].idxmin(), s.index[-1]
        ymax = float(df["pct"].max()) * 1.12
        ax.set_ylim(top=ymax)
        ax.text(0.60, 0.955,
                f"最大:成交 {df.at[i_max,'etf_amt']:.0f} 亿({df.at[i_max,'amt_diff']:+.0f} 亿)\n"
                f"规模 {df.at[i_max,'etf_mv_yi']:.0f} 亿 · {df.at[i_max,'dt']:%Y-%m-%d}",
                transform=ax.transAxes, ha="right", va="top", fontsize=8, color="#a85400",
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="#e8850c", lw=0.6, alpha=0.95))
        ax3.annotate(f"最小:成交 {df.at[i_min,'etf_amt']:.0f} 亿({df.at[i_min,'amt_diff']:+.0f} 亿)\n"
                     f"规模 {df.at[i_min,'etf_mv_yi']:.0f} 亿 · {df.at[i_min,'dt']:%Y-%m-%d}",
                     xy=(df.at[i_min, "dt"], df.at[i_min, "amt_diff"]),
                     xytext=(0, -16), textcoords="offset points", ha="center", va="top",
                     fontsize=8, color="#a85400",
                     bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="#e8850c", lw=0.6, alpha=0.92))
        ax.text(0.988, 0.955,
                f"最新:成交 {df.at[i_last,'etf_amt']:.0f} 亿({df.at[i_last,'amt_diff']:+.0f} 亿)\n"
                f"规模 {df.at[i_last,'etf_mv_yi']:.0f} 亿 · {df.at[i_last,'dt']:%Y-%m-%d}",
                transform=ax.transAxes, ha="right", va="top", fontsize=9, color="#a85400",
                bbox=dict(boxstyle="round,pad=0.34", fc="white", ec="#e8850c", lw=1.0))

    ax.set_ylabel("占比 (%)", color="#1f5fbf", fontsize=11)
    ax2.set_ylabel(f"{name} 点位", color="#999", fontsize=10)
    ax.tick_params(axis="y", colors="#1f5fbf")
    ax2.tick_params(axis="y", colors="#999")
    ax.grid(alpha=0.22)
    ax.set_title(f"{name} ETF 资金跟踪：总市值 / 流通市值，及其斜率",
                 fontsize=15, fontweight="bold", pad=28)

    if key == "hs300":
        for d, lab, yf in [(e[0], e[1], 0.97 if i % 2 == 0 else 0.84) for i, e in enumerate(EVENTS)]:
            x = pd.Timestamp(d)
            if df["dt"].min() <= x <= df["dt"].max():
                ax.axvline(x, color="#d62728", ls="--", lw=0.9, alpha=0.55)
                ax.text(x, yf, lab, transform=ax.get_xaxis_transform(), fontsize=9,
                        color="#b22222", ha="left", va="top",
                        bbox=dict(boxstyle="round,pad=0.22", fc="white", ec="none", alpha=0.85))

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    h3, l3 = ax3.get_legend_handles_labels()
    ax.legend(h1 + h2 + h3, l1 + l2 + l3, loc="upper left", fontsize=9.5, framealpha=0.9)

    # ── 面板2/3 ──
    for axx, w in zip(axes[1:3], (20, 60)):
        c = np.where(df[f"d{w}"] >= 0, RED, GRN)
        axx.bar(df["dt"], df[f"d{w}"], width=1.0, color=c, alpha=0.8)
        axx.axhline(0, color="k", lw=0.8)
        axx.set_ylabel(f"{w}日变化 (pp)", fontsize=10)
        axx.grid(alpha=0.22)
        axx.set_title(f"斜率：{w} 个交易日的比值变化 → 折算净申购金额", fontsize=10.5, loc="left")
        axr = axx.twinx()
        lo2, hi2 = axx.get_ylim()
        axr.set_ylim(lo2 * conv, hi2 * conv)
        axr.set_ylabel(f"折算净申购 (亿元)\n按 {float(df['float_mv'].iloc[-1])/1e12:.1f} 万亿流通市值",
                       fontsize=9, color="#666")
        axr.tick_params(axis="y", colors="#666", labelsize=9)

    # ── 面板4 ──
    ax4 = axes[3]
    ax4.fill_between(df["dt"], df["etf_mv_yi"], color="#9dbde0", alpha=0.65, linewidth=0)
    ax4.plot(df["dt"], df["etf_mv_yi"], color="#1f5fbf", lw=1.2)
    ax4.set_ylabel("ETF 总规模\n(亿元)", fontsize=9.5)
    ax4.grid(alpha=0.22)
    ax4.set_title(f"{int(df['n_etf'].iloc[-1]) if 'n_etf' in df else ''} 只{name} ETF 合计规模（份额 × 收盘价）".strip(),
                  fontsize=10.5, loc="left")
    i_pk = df["etf_mv_yi"].idxmax()
    ax4.annotate(f"峰值 {df.at[i_pk,'etf_mv_yi']:.0f} 亿 · {df.at[i_pk,'dt']:%Y-%m-%d}",
                 xy=(df.at[i_pk, "dt"], df.at[i_pk, "etf_mv_yi"]),
                 xytext=(0, 6), textcoords="offset points", ha="center", va="bottom",
                 fontsize=8.5, color="#1a4a8f")
    ax4.annotate(f"最新 {df['etf_mv_yi'].iloc[-1]:.0f} 亿",
                 xy=(df["dt"].iloc[-1], df["etf_mv_yi"].iloc[-1]),
                 xytext=(-6, 8), textcoords="offset points", ha="right", va="bottom",
                 fontsize=8.5, color="#1a4a8f")
    ax4.set_ylim(0, df["etf_mv_yi"].max() * 1.18)
    ax4.xaxis.set_major_locator(mdates.YearLocator())
    ax4.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    out = os.path.join(OUT, f"{name}_ETF资金跟踪.png")
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("saved", out)
    return out


def main():
    keys = sys.argv[1:] or [k for k, _ in GROUPS]
    made = []
    for k, n in GROUPS:
        if k in keys:
            p = plot_one(k, n)
            if p:
                made.append(p)
    print(f"共生成 {len(made)} 张")


if __name__ == "__main__":
    main()
