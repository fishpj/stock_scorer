"""回测框架：滚动 N 天 × 每日打分取 top-K × 算 5/10 日前瞻收益。

用法：
    python3 backtest.py                          # 默认 30 天 / top 5 / 持有 5 日
    python3 backtest.py --days 60 --top 10 --hold 10
    python3 backtest.py --regime bear --universe 100

输出：backtest_results.xlsx，含 4 个 sheet
    picks          所有 (date, code, score, K, fwd_5d, fwd_10d)
    stats_by_score 按总分桶统计胜率与平均收益
    stats_by_K     按 K 桶统计
    stats_by_Z3    按形态桶统计
"""
from __future__ import annotations
import argparse
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")

import config
import data
import filters
import signals as sig
import scorer


def parse_args():
    p = argparse.ArgumentParser(description="选股模型回测")
    p.add_argument("--days", type=int, default=30, help="回测交易日数")
    p.add_argument("--top", type=int, default=5, help="每日取前 N 只")
    p.add_argument("--hold", type=int, default=5, help="前瞻持有天数")
    p.add_argument("--universe", type=int, default=50, help="universe 规模（top N 当前活跃股）")
    p.add_argument("--regime", choices=["auto", "neutral", "bull", "bear"], default="auto")
    p.add_argument("--offset", type=int, default=0,
                   help="起点偏移：把回测窗口整体向前推 N 个交易日（用于稳定性测试）")
    p.add_argument("--out", default="backtest_results.xlsx")
    return p.parse_args()


def step(msg):
    print(f"\n==> {msg}", flush=True)


def build_universe(n: int) -> pd.DataFrame:
    spot = data.get_spot_all()
    pool = filters.apply_hard_filters(spot)
    return filters.top_by_amount(pool, n), spot


def kline_as_of(kline: pd.DataFrame, date) -> pd.DataFrame:
    """把 K 线截到 date 当日（含）。"""
    if kline is None or kline.empty:
        return pd.DataFrame()
    k = kline.copy()
    if "日期" not in k.columns:
        return pd.DataFrame()
    k["日期"] = pd.to_datetime(k["日期"], errors="coerce")
    return k[k["日期"] <= pd.Timestamp(date)].reset_index(drop=True)


def spot_row_from_kline(code: str, kline_as_of: pd.DataFrame, name: str = "") -> pd.Series:
    """从 K 线最后一行构造 spot_row，兼容 signals.py 的 sina 模式。"""
    if kline_as_of is None or kline_as_of.empty or len(kline_as_of) < 2:
        return None
    last = kline_as_of.iloc[-1]
    prev = kline_as_of.iloc[-2]
    close = pd.to_numeric(last.get("收盘"), errors="coerce")
    prev_close = pd.to_numeric(prev.get("收盘"), errors="coerce")
    pct = (close / prev_close - 1) * 100 if prev_close > 0 else 0
    return pd.Series({
        "代码": code, "名称": name, "最新价": close, "涨跌幅": pct,
        "成交额": pd.to_numeric(last.get("成交额"), errors="coerce"),
        "成交量": pd.to_numeric(last.get("成交量"), errors="coerce"),
    })


def forward_return(kline: pd.DataFrame, buy_date, hold: int) -> float | None:
    """buy_date 收盘买入，buy_date+hold 收盘卖出，返回百分比收益。"""
    if kline is None or kline.empty:
        return None
    k = kline.copy()
    if "日期" not in k.columns:
        return None
    k["日期"] = pd.to_datetime(k["日期"], errors="coerce")
    k = k.sort_values("日期").reset_index(drop=True)
    # 找 ≥ buy_date 的第一行（买入日）
    future = k[k["日期"] >= pd.Timestamp(buy_date)]
    if future.empty or len(future) <= hold:
        return None
    buy_close = pd.to_numeric(future.iloc[0]["收盘"], errors="coerce")
    sell_close = pd.to_numeric(future.iloc[hold]["收盘"], errors="coerce")
    if pd.isna(buy_close) or pd.isna(sell_close) or buy_close <= 0:
        return None
    return (sell_close / buy_close - 1) * 100


def detect_regime_as_of(klines: dict, date) -> str:
    """用 universe 在 date 当日涨跌幅均值粗判市场环境。

    ≥ +0.5% → bull；≤ -0.5% → bear；其它 → neutral
    """
    chgs = []
    for code, k in klines.items():
        if k is None or k.empty or "日期" not in k.columns:
            continue
        kk = k.copy()
        kk["日期"] = pd.to_datetime(kk["日期"], errors="coerce")
        kk = kk.sort_values("日期")
        future = kk[kk["日期"] <= pd.Timestamp(date)]
        if len(future) < 2:
            continue
        try:
            c0 = pd.to_numeric(future.iloc[-1]["收盘"], errors="coerce")
            c1 = pd.to_numeric(future.iloc[-2]["收盘"], errors="coerce")
            if pd.notna(c0) and pd.notna(c1) and c1 > 0:
                chgs.append((c0 / c1 - 1) * 100)
        except Exception:
            continue
    if not chgs:
        return "neutral"
    avg = sum(chgs) / len(chgs)
    if avg >= 0.5:
        return "bull"
    if avg <= -0.5:
        return "bear"
    return "neutral"


def score_as_of(code, name, kline_full, fund, date,
                lhb_codes: set | None = None,
                lhb_inst_codes: set | None = None,
                lhb_inst_amount: dict | None = None) -> dict | None:
    k_as_of = kline_as_of(kline_full, date)
    if k_as_of is None or k_as_of.empty or len(k_as_of) < 60:
        return None
    spot_row = spot_row_from_kline(code, k_as_of, name)
    if spot_row is None:
        return None
    # sina 模式：流通市值二次过滤（保持与 run.py 一致）
    share = pd.to_numeric(k_as_of.iloc[-1].get("流通股本"), errors="coerce")
    price = pd.to_numeric(k_as_of.iloc[-1].get("收盘"), errors="coerce")
    if pd.notna(share) and pd.notna(price):
        circ_yi = share * price / 1e8
        if not (config.HARD_FILTERS["circ_min_billion"] <= circ_yi <= config.HARD_FILTERS["circ_max_billion"]):
            return None
    try:
        return sig.compute_signals(
            code=code, spot_row=spot_row, kline=k_as_of, fund=fund,
            lhb_codes=lhb_codes or set(),
            lhb_inst_codes=lhb_inst_codes or set(),
            lockup_codes=set(), memory_hit=None,
            lhb_inst_amount=(lhb_inst_amount or {}).get(code),
        )
    except Exception:
        return None


def bucket_score(s):
    if s >= 6.5: return "≥6.5（候选门槛）"
    if s >= 5.5: return "5.5-6.5（观察池）"
    if s >= 4.0: return "4.0-5.5"
    return "<4.0"


def bucket_K(k):
    if pd.isna(k): return "N/A"
    if k >= 1.0: return "≥1.0（健康）"
    if k >= 0.5: return "0.5-1.0"
    if k > 0: return "0-0.5（超涨警告）"
    return "≤0（已破位）"


def bucket_Z3(note):
    s = str(note)
    if "超涨" in s: return "超涨"
    if "超跌" in s: return "超跌"
    if "走强" in s: return "走强"
    if "走弱" in s: return "走弱"
    return "中性"


def aggregate(picks: pd.DataFrame, col: str, bucket_fn) -> pd.DataFrame:
    """按 col 桶聚合：样本数、5日胜率、5日平均、10日胜率、10日平均。"""
    df = picks.copy()
    df["_bucket"] = df[col].apply(bucket_fn)
    rows = []
    for b, g in df.groupby("_bucket"):
        rows.append({
            "桶": b,
            "样本": len(g),
            "5日胜率%": round((g["fwd_5d"] > 0).mean() * 100, 1) if g["fwd_5d"].notna().any() else None,
            "5日平均%": round(g["fwd_5d"].mean(), 2) if g["fwd_5d"].notna().any() else None,
            "10日胜率%": round((g["fwd_10d"] > 0).mean() * 100, 1) if g["fwd_10d"].notna().any() else None,
            "10日平均%": round(g["fwd_10d"].mean(), 2) if g["fwd_10d"].notna().any() else None,
        })
    # 排序
    order_map = {}
    return pd.DataFrame(rows).sort_values("5日平均%", ascending=False).reset_index(drop=True)


def main():
    args = parse_args()

    step(f"构造 universe（top {args.universe} 当前活跃股）…")
    uni, spot_full = build_universe(args.universe)
    print(f"  universe {len(uni)} 只")

    step("拉 universe 全部 K 线与财务（一次性缓存）…")
    codes_names = list(zip(uni["代码"].astype(str).str.zfill(6), uni["名称"].astype(str)))
    klines = {}
    funds = {}
    for i, (code, name) in enumerate(codes_names):
        klines[code] = data.get_daily_kline(code, days=200)
        funds[code] = data.get_fundamentals(code)
        if (i + 1) % 10 == 0:
            print(f"  已抓 {i+1}/{len(codes_names)}")
    print(f"  K 线 {sum(1 for v in klines.values() if v is not None and not v.empty)}/{len(codes_names)}")

    step(f"回测 {args.days} 个交易日（offset={args.offset}）…")
    end = datetime.now().date()
    # 用 periods 把起点推前 offset 个交易日，但总是取最早的 days 个做测试
    all_bdates = pd.bdate_range(end=end, periods=args.days + args.hold + args.offset).date
    test_dates = all_bdates[:args.days]
    print(f"  测试 {len(test_dates)} 个交易日（{test_dates[0]} → {test_dates[-1]}）")
    print(f"  前 {args.hold} 个交易日（{all_bdates[args.days]} → {all_bdates[-1]}）留作前瞻")

    # ---- 预拉整个测试窗口的 LHB 数据（含机构席位）----
    step("拉测试窗口的龙虎榜数据…")
    lhb_start = (test_dates[0] - timedelta(days=14)).strftime("%Y%m%d")
    lhb_end = test_dates[-1].strftime("%Y%m%d")
    lhb_detail = data.get_lhb_range(lhb_start, lhb_end)
    lhb_inst = data.get_lhb_inst_range(lhb_start, lhb_end)
    print(f"  LHB 明细 {len(lhb_detail)} 行 | 机构明细 {len(lhb_inst)} 行")
    # 把日期列标准化
    if not lhb_detail.empty and "上榜日" in lhb_detail.columns:
        lhb_detail["_date"] = pd.to_datetime(lhb_detail["上榜日"], errors="coerce").dt.date
    if not lhb_inst.empty and "上榜日期" in lhb_inst.columns:
        lhb_inst["_date"] = pd.to_datetime(lhb_inst["上榜日期"], errors="coerce").dt.date
        if "机构买入净额" in lhb_inst.columns:
            lhb_inst["_net"] = pd.to_numeric(lhb_inst["机构买入净额"], errors="coerce")

    def lhb_sets_as_of(d):
        """返回 (lhb_codes, lhb_inst_codes, lhb_inst_amount) — d 当日往前 7 日内 LHB 上榜的代码。"""
        window_start = d - timedelta(days=7)
        lhb_codes = set()
        if not lhb_detail.empty:
            mask = (lhb_detail["_date"] >= window_start) & (lhb_detail["_date"] <= d)
            lhb_codes = set(lhb_detail.loc[mask, "代码"].astype(str).str.zfill(6))
        lhb_inst_codes = set()
        lhb_inst_amount = {}
        if not lhb_inst.empty and "_net" in lhb_inst.columns:
            mask = (lhb_inst["_date"] >= window_start) & (lhb_inst["_date"] <= d)
            sub = lhb_inst[mask]
            pos = sub[sub["_net"] > 0]
            lhb_inst_codes = set(pos["代码"].astype(str).str.zfill(6))
            for _, r in pos.iterrows():
                code = str(r["代码"]).zfill(6)
                amt = float(r["_net"])
                if amt > lhb_inst_amount.get(code, 0):
                    lhb_inst_amount[code] = amt
        return lhb_codes, lhb_inst_codes, lhb_inst_amount

    lhb_coverage = sum(1 for d in test_dates if lhb_sets_as_of(d)[0])
    print(f"  {lhb_coverage}/{len(test_dates)} 个测试日有 LHB 数据")

    # 每日按当日市场环境切换权重
    regime_arg = args.regime
    use_daily_regime = (regime_arg == "auto")

    picks = []
    regime_counter = {}
    for di, d in enumerate(test_dates):
        if use_daily_regime:
            regime = detect_regime_as_of(klines, d)
        else:
            regime = regime_arg
        regime_counter[regime] = regime_counter.get(regime, 0) + 1

        signals_list = []
        lhb_codes, lhb_inst_codes, lhb_inst_amount = lhb_sets_as_of(d)
        for code, name in codes_names:
            s = score_as_of(code, name, klines.get(code), funds.get(code, {}), d,
                            lhb_codes=lhb_codes, lhb_inst_codes=lhb_inst_codes,
                            lhb_inst_amount=lhb_inst_amount)
            if s:
                signals_list.append(s)
        if not signals_list:
            continue
        df, _ = scorer.score_all(signals_list, regime)
        top = df.head(args.top)
        for _, r in top.iterrows():
            picks.append({
                "date": d,
                "regime": regime,
                "代码": r["代码"], "名称": r["名称"],
                "总分": r["总分"], "K_盈亏比": r.get("K_盈亏比"),
                "Z3_note": r.get("Z3_note", ""),
                "Z4_note": r.get("Z4_note", ""),
                "机构净买入_亿": r.get("机构净买入_亿"),
                "建议": r.get("建议", ""),
                "fwd_5d": forward_return(klines.get(r["代码"]), d, 5),
                "fwd_10d": forward_return(klines.get(r["代码"]), d, 10),
            })
        if (di + 1) % 10 == 0:
            done = [p for p in picks if p["date"] <= test_dates[di]]
            avg5 = pd.Series([p["fwd_5d"] for p in done if p["fwd_5d"] is not None]).mean()
            print(f"  [{di+1}/{len(test_dates)}] {d} | regime={regime} | picks {len(picks)} | 平均5日 {avg5:.2f}%")

    print(f"\n  市场环境分布：{regime_counter}")

    picks_df = pd.DataFrame(picks)
    if picks_df.empty:
        print("没有可统计的 picks。")
        return 1

    step("聚合统计…")
    stats_score = aggregate(picks_df, "总分", bucket_score)
    stats_K = aggregate(picks_df, "K_盈亏比", bucket_K)
    stats_Z3 = aggregate(picks_df, "Z3_note", bucket_Z3)
    stats_regime = aggregate(picks_df, "regime", lambda r: r)
    stats_lhb = aggregate(picks_df, "Z4_note",
                          lambda r: "有LHB" if "龙虎榜" in str(r) else "无LHB")
    stats_inst = aggregate(picks_df, "机构净买入_亿",
                           lambda r: "机构净买入" if pd.notna(r) and r > 0 else "无机构")

    # 每日组合：top-K 等权 5 日收益
    daily = picks_df.dropna(subset=["fwd_5d"]).groupby("date").agg(
        n=("代码", "count"),
        regime=("regime", "first"),
        avg_5d=("fwd_5d", "mean"),
        win_rate=("fwd_5d", lambda s: (s > 0).mean() * 100),
    ).reset_index()
    daily["cum_avg"] = daily["avg_5d"].cumsum()

    out = Path(__file__).parent / args.out
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        picks_df.to_excel(w, sheet_name="picks", index=False)
        stats_score.to_excel(w, sheet_name="stats_by_score", index=False)
        stats_K.to_excel(w, sheet_name="stats_by_K", index=False)
        stats_Z3.to_excel(w, sheet_name="stats_by_Z3", index=False)
        stats_regime.to_excel(w, sheet_name="stats_by_regime", index=False)
        stats_lhb.to_excel(w, sheet_name="stats_by_LHB", index=False)
        stats_inst.to_excel(w, sheet_name="stats_by_inst", index=False)
        daily.to_excel(w, sheet_name="daily", index=False)

    # 终端汇总
    valid5 = picks_df["fwd_5d"].dropna()
    valid10 = picks_df["fwd_10d"].dropna()
    print()
    print("=" * 60)
    print(f"总 picks: {len(picks_df)} | 有效 5日 {len(valid5)} | 10日 {len(valid10)}")
    print(f"整体 5日胜率: {(valid5>0).mean()*100:.1f}% | 平均 {valid5.mean():.2f}%")
    print(f"整体 10日胜率: {(valid10>0).mean()*100:.1f}% | 平均 {valid10.mean():.2f}%")
    print()
    print("--- 按总分桶 ---")
    print(stats_score.to_string(index=False))
    print()
    print("--- 按 K 桶 ---")
    print(stats_K.to_string(index=False))
    print()
    print("--- 按形态桶 ---")
    print(stats_Z3.to_string(index=False))
    print()
    print("--- 按龙虎榜命中 ---")
    print(stats_lhb.to_string(index=False))
    print()
    print("--- 按机构净买入 ---")
    print(stats_inst.to_string(index=False))
    print()
    print(f"✓ 已输出：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
