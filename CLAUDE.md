# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A 股选股打分模型：把"股票涨跌理论"的 G（关注度）/ Z（确定性）/ K（盈亏比）/ S（最大可卖量）四变量与六大判断依据，翻译成 akshare 可抓取的信号，加权打分后输出 Excel 报告。无任何 token 依赖，数据全部来自 akshare 公开接口。

## 常用命令

```bash
# 安装依赖
pip3 install -r requirements.txt

# 首次：生成示例记忆库模板（memory_seed.xlsx）
python3 run.py --init-memory

# 每日盘后运行（默认 top 50 候选）
python3 run.py

# 强制清空缓存重跑
python3 run.py --no-cache

# 指定市场环境与候选数
python3 run.py --regime bull --top 100

# 回测（默认 30 天 / top 5 / 持有 5 日）
python3 backtest.py --days 60 --top 10 --hold 10
python3 backtest.py --regime bear --universe 100 --offset 10

# Streamlit 看板（可视化 ranked_candidates.xlsx）
streamlit run app.py
```

看板额外依赖：`pip install streamlit plotly`（不在 requirements.txt 中）。

## 架构

七步流程在 `run.py` 串起来，每个模块对应方案的一步：

1. **`data.py`** — akshare 数据层。所有调用走文件缓存（`cache/` 目录，pkl 格式）。**双源策略**：东财（`_em` 系列，字段全）优先，失败降级到新浪（字段少但稳定）。新浪源没有 `流通市值`/`PE`/`PB`/`换手率`，会在 `signals.py` 用 K 线末行补算。spot 行情会打上 `_source` 列（`em` 或 `sina`）供下游分支判断。
2. **`filters.py`** — 硬剔除（ST/退市、流通市值区间、成交额下限、北交所/B 股）。`top_by_amount` 用 `成交额 × log(换手率+1)` 综合活跃度排序，新浪 fallback 到纯成交额。`enrich_turnover_from_kline` 在 sina 模式下补换手率。
3. **`signals.py`** — `compute_signals` 对单只股票产出扁平 dict：六大依据子分（0~2）+ K 盈亏比 + G 量比 + timing_ok。注意 Z4 只看换手率，龙虎榜/机构净买入仅写 note 不加分（回测显示 LHB 上榜后反而回调）。
4. **`scorer.py`** — 加权合成 0~10 总分。权重矩阵按市场环境（neutral/bull/bear）切换；`leap_boost` 检测 ≥30% 候选在某依据得满分时临时加权并归一。`_regime_adjust_z3` 会按环境重打 Z3 分（bull 顺势、bear 超跌反转）。K 甜点 [0.5,1.5] +0.5，<0.3 扣 1。Tiebreaker 用 G 量比与机构净买入金额做小数级排序（不改整数档位）。
5. **`memory.py`** — 特定情境记忆库。`memory_seed.xlsx` schema：题材/P1~P3 日期与涨幅/关联代码。衰减公式 `贡献 = Σ(涨幅 × P权重 × 0.5^(距今天数/60))`，P1=50%/P2=30%/P3=15%/P4=3%。
6. **`config.py`** — 所有阈值与权重的唯一来源。`HARD_FILTERS`/`SCORE_WEIGHTS`/`PERF`/`PEER`/`PATTERN`/`ATTENTION`/`SELLABLE`/`RISK_REWARD`/`RISK`/`TIMING`/`CACHE_*`。改参数只动这里。
7. **`app.py`** — Streamlit 看板，读 `ranked_candidates.xlsx` 四个 sheet（排名/权重/风控/元数据）做可视化。可通过页面按钮触发 `run.py` 重跑。
8. **`backtest.py`** — 滚动 N 天 × 每日打分取 top-K × 5/10 日前瞻收益。`--offset` 把回测窗口整体向前推 N 个交易日做稳定性测试。输出 `backtest_results.xlsx` 含 picks/stats_by_score/stats_by_K/stats_by_Z3 四 sheet。

## 输出文件

- `ranked_candidates.xlsx` — 主输出，四个 sheet：排名（总分降序+六大子分+各 note+K/PE/流通市值）、权重（归一后权重矩阵含跃迁）、风控（`config.RISK` 拷贝）、元数据（生成时间/环境/候选数/门槛分/跃迁数）。
- `backtest_results.xlsx` — 回测输出。
- `memory_seed.xlsx` — 记忆库，人工维护。
- `cache/` — akshare 数据缓存（pkl + meta），TTL 见 `config.CACHE_TTL_HOURS`（行情 12h，财务 7 天，K 线 24h）。

## 关键约束

- 所有 akshare 调用必须经 `data.py` 的 `_cached` 包装，不要直连。
- 新浪源缺字段是常态，`signals.py` 与 `filters.py` 都已兼容缺失列自动跳过，改的时候保持这个特性。
- `config.py` 是参数唯一来源，不要在其它文件硬编码阈值。
- 每个源文件顶部有"修改记录"注释头（修改内容/日期/作者 fishpj），编辑时保留这个约定。
- 输出 Excel 的列顺序在 `run.py` 的 `cols` 列表里维护，新增列要同步加到这里才会导出。
