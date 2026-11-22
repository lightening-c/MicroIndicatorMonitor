"""
微观指标跟踪 — 页面数据注入器
生成 trimmed JSON 和 JS 文件（不修改 HTML）
"""
import os, json

from paths import DATA_DIR

ANALYSIS_FILE = os.path.join(DATA_DIR, "analysis_result.json")
TRIM_FILE = os.path.join(DATA_DIR, "analysis_trimmed.json")
JS_FILE = os.path.join(DATA_DIR, "analysis_data.js")


def build():
    if not os.path.exists(ANALYSIS_FILE):
        print(f"[WARN] analysis_result.json 不存在，跳过构建")
        return

    with open(ANALYSIS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Trim time series
    for key in ["major_indices", "broad_indices", "style_indices", "industry_indices"]:
        for ind in data.get(key, []):
            ts = ind.get("ts_data", [])
            if len(ts) > 60:
                ind["ts_data"] = ts[-60:]
    for entry in data.get("etf_valuation", []):
        ts = entry.get("ts_data", [])
        if len(ts) > 120:
            entry["ts_data"] = ts[-120:]
    if "sentiment" in data and "margin_balance" in data["sentiment"]:
        ts = data["sentiment"]["margin_balance"].get("ts_data", [])
        if len(ts) > 120:
            data["sentiment"]["margin_balance"]["ts_data"] = ts[-120:]

    with open(TRIM_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"[OK] 精简版已保存: {TRIM_FILE} ({os.path.getsize(TRIM_FILE)} bytes)")

    js_str = json.dumps(data, ensure_ascii=False)
    with open(JS_FILE, "w", encoding="utf-8") as f:
        f.write("window.__ANALYSIS_DATA__ = ")
        f.write(js_str)
        f.write(";\n")
    print(f"[OK] JS数据已保存: {JS_FILE}")


if __name__ == "__main__":
    build()
