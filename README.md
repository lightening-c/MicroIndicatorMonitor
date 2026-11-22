# 微观指标跟踪 MicroIndicatorMonitor

A 股市场微观指标与「国家队」宽基 ETF 资金跟踪工具。项目从 AKShare、东方财富、上交所/深交所等公开数据源采集行情与 ETF 份额，计算技术指标、估值分位、情绪指数和 ETF 资金占比，最终渲染为单页 HTML 看板，并可导出图表与 Word 报告。

> 仅用于数据研究与学习，不构成任何投资建议。

## 功能概览

- **指数行情**：大盘、宽基、风格、行业指数的日线数据与技术指标（MA / MACD / RSI / 布林带）
- **估值与情绪**：指数 PE/PB 分位、融资余额、涨跌比、成交量偏离、波动率合成的情绪指数
- **国家队 ETF 资金跟踪**：宽基 ETF 总市值 / 指数流通市值及其 20/60 日变化，覆盖上证50、沪深300、中证500/1000/2000、科创50、创业板指、证券ETF
- **盘中实时快照**：交易时段用「最新份额 × 实时价」估算 ETF 规模占比
- **看板与导出**：本地 Web 看板、四面板 PNG 图表、Word 分析报告

## 目录结构

```
.
├── dashboard.html              # 单页看板（ECharts）
├── config.yaml                 # 指标、ETF 跟踪与阈值配置
├── server.py                   # 本地服务器：静态页面 + 更新 API + 定时任务
├── run_all.py                  # 命令行一键跑完整流程
├── start.bat                   # Windows 启动脚本
├── requirements.txt
├── src/
│   ├── paths.py                # 统一路径常量
│   ├── fetch_data.py           # 采集指数、估值、融资、情绪等数据
│   ├── fetch_nfgj.py           # 采集 ETF 份额/价格，计算资金占比
│   ├── fetch_rt.py             # 盘中实时快照
│   ├── analyze.py              # 分析引擎，生成 analysis_result.json
│   ├── build_dashboard.py      # 生成看板所需的精简 JSON / JS
│   ├── backfill_margin_szse.py # 回补深市融资余额
│   ├── export_nfgj_charts.py   # 导出四面板 PNG
│   └── export_nfgj_report.py   # 导出 Word 报告
├── assets/                     # 静态素材
├── data/                       # 运行数据（默认不入库）
└── exports/                    # 导出产物（默认不入库）
```

## 环境准备

需要 Python 3.10+。

```powershell
conda create -n micro-tracker python=3.11 -y
conda activate micro-tracker
pip install -r requirements.txt
```

或直接使用已有的 `micro-tracker` conda 环境。`start.bat` 会依次尝试 `MICRO_PYTHON` 环境变量、`%USERPROFILE%\.conda\envs\micro-tracker\python.exe`、PATH 中的 `python`。

## 使用

### 1. 首次采集并生成看板

```powershell
python run_all.py
```

依次执行 `fetch_data.py → fetch_nfgj.py → analyze.py → build_dashboard.py`。首次运行需要下载较长历史的数据，耗时取决于网络。

### 2. 启动本地服务器

```powershell
.\start.bat
```

浏览器打开 <http://localhost:4323>。页面右上角的更新按钮会调用 `POST /api/update` 重新跑完整流程；服务器还会在交易时段每 5 分钟采集实时快照，并在晚间份额披露后自动补采。

也可以不加服务器，直接双击打开 `dashboard.html`（需先通过 `run_all.py` 生成 `data/analysis_trimmed.json`）。

### 3. 导出图表与报告

```powershell
python src/export_nfgj_charts.py
python src/export_nfgj_report.py
```

产物写入 `exports/`。图表导出依赖 `matplotlib` 与系统中文字体（微软雅黑 / 黑体）。

## 配置说明

- `config.yaml → indicators`：指数清单、数据源与 AKShare 调用参数
- `config.yaml → nfgj.indices`：ETF 资金跟踪覆盖的指数
- `config.yaml → nfgj.etf_list_source`：ETF 基础信息表路径，默认 `data/etf_list_source.csv`（相对项目根目录）。该表需包含 `ETF代码`、`ETF简称`、`跟踪指数代码` 三列；文件缺失时回退到仓库内的 `data/nfgj_etf_list_*.csv` 缓存
- `config.yaml → etf_valuation`：低估/高估分位阈值
- `config.yaml → sentiment`：情绪指数各分项权重

## 数据与产物

- `data/` 下全部为运行时生成或缓存数据，默认不纳入版本控制，仅保留 ETF 清单样本
- `exports/` 下的图片与文档同样不入库
- 删除 `data/` 后重新执行 `python run_all.py` 即可重建

## 数据源

- [AKShare](https://akshare.akfamily.xyz/)：指数日线、融资余额、行业 PE 等
- 中证指数公司、上交所、深交所：指数点位与 ETF 份额
- 东方财富、新浪财经：行情与实时报价

公开接口可能限流或调整，脚本内置了重试与本地缓存。

## 免责声明

本项目所有数据来自公开渠道，可能存在延迟、缺失或口径差异；计算结果仅供研究参考，不构成投资建议。据此操作，风险自负。
