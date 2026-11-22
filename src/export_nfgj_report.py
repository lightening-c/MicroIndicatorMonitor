# -*- coding: utf-8 -*-
"""导出《国家队主流宽基 ETF 资金跟踪分析》Word 报告。

数据来源：data/nfgj_{key}.csv、data/analysis_result.json
输出：导出/国家队主流宽基ETF资金跟踪分析_{日期}.docx
"""
import json
import os
from datetime import datetime

import pandas as pd
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from paths import DATA_DIR as DATA, EXPORT_DIR as OUT

os.makedirs(OUT, exist_ok=True)

GROUPS = [
    ("sz50", "上证50"), ("hs300", "沪深300"), ("zz500", "中证500"),
    ("zz1000", "中证1000"), ("zz2000", "中证2000"),
    ("star50", "科创50"), ("chinext", "创业板指"),
    ("broker", "证券ETF"),
]


def load(key):
    p = os.path.join(DATA, f"nfgj_{key}.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, dtype={"date": str}).sort_values("date").reset_index(drop=True)
    for c in ("pct", "idx_level", "etf_mv_yi", "float_mv", "etf_amt", "amt_diff"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["d20"] = df["pct"].diff(20)
    df["d60"] = df["pct"].diff(60)
    df["conv"] = df["float_mv"] / 1e8 / 100.0      # 亿元/pp
    return df


def set_font(run, size=10.5, bold=False, color=None):
    run.font.name = "微软雅黑"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    run.font.size = Pt(size)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)


def para(doc, text, size=10.5, bold=False, space_after=4, color=None, align=None):
    p = doc.add_paragraph()
    r = p.add_run(text)
    set_font(r, size, bold, color)
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = 1.45
    if align:
        p.alignment = align
    return p


def table(doc, header, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(header):
        cell = t.rows[0].cells[i]
        cell.text = ""
        r = cell.paragraphs[0].add_run(str(h))
        set_font(r, 9, True)
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            r = cells[i].paragraphs[0].add_run(str(v))
            set_font(r, 9)
            cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    return t


def main():
    res = json.load(open(os.path.join(DATA, "analysis_result.json"), encoding="utf-8"))
    nfgj = res.get("nfgj") or {}
    narratives = nfgj.get("narratives") or {}
    buckets = nfgj.get("buckets") or {}
    episodes = nfgj.get("episodes") or {}

    rows, asof, sum20, sum60 = [], None, 0.0, 0.0
    for key, name in GROUPS:
        df = load(key)
        if df is None or df.empty:
            continue
        last = df.iloc[-1]
        asof = last["date"]
        d20 = float(last["d20"]) if pd.notna(last["d20"]) else 0.0
        d60 = float(last["d60"]) if pd.notna(last["d60"]) else 0.0
        conv = float(last["conv"])
        n20, n60 = d20 * conv, d60 * conv
        if key != "broker":          # 证券ETF 为行业指数，不计入宽基合计
            sum20 += n20
            sum60 += n60
        state = ((narratives.get(key) or {}).get("all") or {}).get("state", "-")
        p_rank = float((df["pct"] <= float(last["pct"])).mean() * 100)
        rows.append([name, int(last.get("n_etf") or 0), f"{float(last['etf_mv_yi']):,.0f}",
                     f"{float(last['pct']):.3f}", f"{p_rank:.0f}%",
                     f"{d20:+.3f}", f"{n20:+,.0f}",
                     f"{d60:+.3f}", f"{n60:+,.0f}", state])

    d = f"{asof[:4]}-{asof[4:6]}-{asof[6:]}" if asof else datetime.now().strftime("%Y-%m-%d")
    doc = Document()
    doc.styles["Normal"].font.name = "微软雅黑"
    doc.styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    doc.styles["Normal"].font.size = Pt(10.5)

    para(doc, "国家队主流宽基 ETF 资金跟踪分析", 18, True, 2, (0x1F, 0x3A, 0x5F), WD_ALIGN_PARAGRAPH.CENTER)
    para(doc, f"数据截至 {d}　|　口径：ETF 总市值 ÷ 指数流通市值，斜率为 20/60 日占比变化",
         9, False, 14, (0x66, 0x66, 0x66), WD_ALIGN_PARAGRAPH.CENTER)

    # ── 摘要 ──
    para(doc, "一、结论摘要", 13, True, 6, (0x1F, 0x3A, 0x5F))
    broad_rows = [r for r, (k, _) in zip(rows, GROUPS) if k != "broker"]
    up_cnt = sum(1 for r in broad_rows if r[5].startswith("+"))
    para(doc, f"截至 {d}，{len(broad_rows)} 个主流宽基（不含证券ETF）合计 20 日折算净申购约 "
              f"{sum20:+,.0f} 亿元、60 日约 {sum60:+,.0f} 亿元；其中 {up_cnt} 个为净流入，"
              f"{len(broad_rows)-up_cnt} 个为净流出。资金端整体处于净增持状态，规模集中在沪深300 与科创50。")

    # 本轮增持口径（自各自低点以来，按份额变化估算，已剔除价格影响）
    epis = [(k, n, episodes[k]) for k, n in GROUPS if episodes.get(k) and k != "broker"]
    if epis:
        s_add = sum(e["added_yi"] for _, _, e in epis)
        s_gap = sum(e["gap_yi"] for _, _, e in epis)
        s_peak = sum(e["peak_yi"] for _, _, e in epis)
        s_now = sum(e["now_yi"] for _, _, e in epis)
        t_dates = sorted(e["trough_date"] for _, _, e in epis)
        tr0, tr1 = t_dates[0], t_dates[-1]
        fd = lambda x: f"{x[:4]}-{x[4:6]}-{x[6:]}"
        span = fd(tr0) if tr0 == tr1 else f"{fd(tr0)}~{fd(tr1)}"
        para(doc, f"分轮次看：自各自本轮低点（{span}）以来，{len(epis)} 个宽基合计增持约 {s_add:,.0f} 亿元；"
                  f"当前合计规模 {s_now:,.0f} 亿元，距各自历史峰值合计 {s_peak:,.0f} 亿元"
                  f"还差 {s_gap:,.0f} 亿元（合计较峰值 {(-s_gap/s_peak*100 if s_peak else 0):.0f}%）。")
        hs_e = episodes.get("hs300")
        if hs_e:
            para(doc, f"结构上以沪深300 为主：自 {fd(hs_e['trough_date'])} 的低点"
                      f"（{hs_e['trough_yi']:,.0f} 亿，较峰值回落 {abs(hs_e.get('drawdown_pct', 0)):.0f}%）以来"
                      f"累计增持约 {hs_e['added_yi']:,.0f} 亿元；当前 {hs_e['now_yi']:,.0f} 亿元，"
                      f"距 {fd(hs_e['peak_date'])} 的峰值 {hs_e['peak_yi']:,.0f} 亿元还差 "
                      f"{hs_e['gap_yi']:,.0f} 亿元（{hs_e.get('gap_pct', 0):.0f}%）。")
    para(doc, "需说明：本口径的「折算净申购」= 占比变化 × 流通市值，是把资金流按指数体量还原成金额，"
              "属于估算值；ETF 份额按日披露，盘中只能用「最新份额 × 实时价」近似，最终以交易所份额数据为准。")
    para(doc, "上述合计为各指数 ETF 净申购的简单加总，反映的是「买 ETF 的钱」，"
              "不代表个股层面的净买入——不同宽基的成分股存在大量重叠（如上证50 与沪深300）。")

    # ── 表1 ──
    para(doc, "二、最新预计增持规模（按指数）", 13, True, 6, (0x1F, 0x3A, 0x5F))
    table(doc, ["指数", "ETF数", "ETF规模(亿)", "占比(%)", "占比分位", "20日Δ(pp)", "20日折算(亿)",
                "60日Δ(pp)", "60日折算(亿)", "状态"], rows)
    para(doc, "注：20/60 日折算金额按最新流通市值换算；状态阈值取该指数自身历史分位（90/60/40/10 分位），"
              "不同指数之间不可直接比较。", 9, False, 10, (0x66, 0x66, 0x66))

    # ── 本轮增持 ──
    episodes = nfgj.get("episodes") or {}
    if episodes:
        para(doc, "三、本轮增持：自低点以来累计买入与距峰值缺口", 13, True, 6, (0x1F, 0x3A, 0x5F))
        erows = []
        for key, name in GROUPS:
            e = episodes.get(key)
            if not e:
                continue
            fd = lambda x: f"{x[:4]}-{x[4:6]}-{x[6:]}"
            erows.append([name, f"{e['peak_yi']:,.0f}（{fd(e['peak_date'])}）",
                          f"{e['trough_yi']:,.0f}（{fd(e['trough_date'])}）",
                          f"{e.get('drawdown_pct', 0):.0f}%",
                          f"{e['now_yi']:,.0f}",
                          f"{e['added_yi']:+,.0f}" if e["added_yi"] > 1 else "尚未回升",
                          f"{e['gap_yi']:,.0f}",
                          f"{e.get('gap_pct', 0):.0f}%"])
        table(doc, ["指数", "历史峰值(亿)", "本轮低点(亿)", "低点较峰值",
                    "当前规模(亿)", "本轮累计增持(亿)", "距峰值(亿)", "当前较峰值"], erows)
        hs_e = episodes.get("hs300")
        if hs_e and hs_e["added_yi"] > 1:
            para(doc, f"以沪深300 为例：自 {hs_e['trough_date'][:4]}-{hs_e['trough_date'][4:6]}-"
                      f"{hs_e['trough_date'][6:]} 的低点以来累计增持约 {hs_e['added_yi']:,.0f} 亿元，"
                      f"但当前 {hs_e['now_yi']:,.0f} 亿元仍较 {hs_e['peak_date'][:4]}-{hs_e['peak_date'][4:6]}-"
                      f"{hs_e['peak_date'][6:]} 的峰值 {hs_e['peak_yi']:,.0f} 亿元少 "
                      f"{hs_e['gap_yi']:,.0f} 亿元。", 10)
        para(doc, "注：累计增持按「每日份额变化 × 当日均价」逐年累加，已剔除价格上涨的影响；"
                  "缺口为历史峰值与当前规模的差额，不代表后续的目标规模。", 9, False, 10, (0x66, 0x66, 0x66))

    # ── 表2 分位阈值 ──
    para(doc, "四、各指数当前所处位置", 13, True, 6, (0x1F, 0x3A, 0x5F))
    pos_rows = []
    for key, name in GROUPS:
        df = load(key)
        if df is None or df.empty:
            continue
        last = df.iloc[-1]
        q90 = float(df["d20"].quantile(0.90))
        q10 = float(df["d20"].quantile(0.10))
        d20 = float(last["d20"]) if pd.notna(last["d20"]) else 0.0
        rank = int((df["d20"] <= d20).sum() / max(1, df["d20"].notna().sum()) * 100)
        y1 = df.tail(250)
        lo, hi = float(y1["idx_level"].min()), float(y1["idx_level"].max())
        pos = (float(last["idx_level"]) - lo) / (hi - lo) * 100 if hi > lo else 0
        pos_rows.append([name, f"{q10:+.3f}", f"{q90:+.3f}", f"{d20:+.3f}",
                         f"{rank}%", f"{float(last['idx_level']):,.0f}", f"{pos:.0f}%"])
    table(doc, ["指数", "10分位(pp)", "90分位(pp)", "当前20日Δ", "斜率分位", "当前点位", "近一年位置"], pos_rows)

    # ── 沪深300 拆分 ──
    hs = load("hs300")
    if hs is not None and len(hs) > 40:
        last = hs.iloc[-1]
        ref = hs.iloc[-30]
        ref_yi, now_yi = float(ref["etf_mv_yi"]), float(last["etf_mv_yi"])
        idx_chg = (float(last["idx_level"]) / float(ref["idx_level"]) - 1) * 100
        strong = hs[hs["d20"] >= hs["d20"].quantile(0.90)]["idx_level"]
        cost = float(strong.mean()) if len(strong) >= 3 else None
        para(doc, "五、沪深300：规模增长来自份额，而非价格上涨", 13, True, 6, (0x1F, 0x3A, 0x5F))
        para(doc, f"ETF 合计规模从区间起点附近的 {ref_yi:,.0f} 亿元增至 {now_yi:,.0f} 亿元"
                  f"（{now_yi-ref_yi:+,.0f} 亿元），而同期指数为 {idx_chg:+.1f}%。"
                  f"价格下跌不可能放大规模，因此增量主要来自份额（净申购），"
                  f"这是判断「国家队借道 ETF 进场」的核心依据。")
        if cost:
            rel = (float(last["idx_level"]) / cost - 1) * 100
            para(doc, f"以历史强申购时点的平均成本 {cost:,.0f} 作为参考，当前指数 "
                      f"{float(last['idx_level']):,.0f} 较其{'高' if rel >= 0 else '低'}约 {abs(rel):.0f}%，"
                      f"主要买入资金{'大概率处于浮盈状态' if rel > 2 else '盈亏基本平衡' if rel > -2 else '处于浮亏'}。")

    # ── 关键时点 ──
    nar = ((narratives.get('hs300') or {}).get('all') or {})
    items = nar.get("items") or []
    if items:
        para(doc, "六、关键时点与历史印证", 13, True, 6, (0x1F, 0x3A, 0x5F))
        for it in items:
            if it.get("h") in ("当前状态",):
                continue
            para(doc, f"· {it.get('h')}：{it.get('t')}", 10)

    # ── 分桶 ──
    bk = ((buckets.get("hs300") or {}).get("all") or {})
    if bk:
        para(doc, "七、20 日净申购分桶 → 未来收益（沪深300）", 13, True, 6, (0x1F, 0x3A, 0x5F))
        rows_b = []
        for i, lab in enumerate(bk["labels"]):
            rows_b.append([lab, bk["n_daily"][i], bk["n_indep"][i],
                           f"{bk['daily20'][i]:+.1f}" if bk["daily20"][i] is not None else "-",
                           f"{bk['daily60'][i]:+.1f}" if bk["daily60"][i] is not None else "-",
                           f"{bk['indep20'][i]:+.1f}" if bk["indep20"][i] is not None else "-",
                           f"{bk['indep60'][i]:+.1f}" if bk["indep60"][i] is not None else "-"])
        table(doc, ["占比变化区间(pp)", "日频样本", "去重叠样本", "日频20日", "日频60日",
                    "去重叠20日", "去重叠60日"], rows_b)
        para(doc, "注：单位为 %；去重叠样本为每 20 个交易日取一点，避免重叠区间造成伪显著，"
                  "其样本量很小（常见 1~4 个），仅作参考。", 9, False, 10, (0x66, 0x66, 0x66))

    # ── 风险提示 ──
    para(doc, "八、口径与风险提示", 13, True, 6, (0x1F, 0x3A, 0x5F))
    for t in [
        "ETF 份额由交易所按日披露，当日数据通常晚间才上线；盘中占比为估算值。",
        "流通市值以指数点位 × 标定系数近似，系数按 30 天周期更新，跨年比较存在偏差。",
        "折算净申购是「占比变化 × 流通市值」的重建结果，不等于基金公司的精确申赎金额。",
        "历史统计（分桶、时点回看）样本有限，且存在重叠与幸存者偏差，不构成收益预测。",
        "本文仅作数据跟踪与研究记录，不构成任何投资建议。",
    ]:
        para(doc, f"· {t}", 9.5, False, 2)

    stamp = asof if asof else datetime.now().strftime("%Y%m%d")
    out = os.path.join(OUT, f"国家队主流宽基ETF资金跟踪分析_{stamp}.docx")
    try:
        doc.save(out)
    except PermissionError:
        # 文件被 Word 占用时另存一份，避免覆盖失败
        base, ext = os.path.splitext(out)
        out = f"{base}_{datetime.now():%H%M}{ext}"
        doc.save(out)
    print("saved", out)


if __name__ == "__main__":
    main()
