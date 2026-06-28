# 修改记录
# 修改内容: CLI 入口，编排抓数→过滤→打分→输出 ranked_candidates.xlsx 全流程
# 修改日期: 2026-06-28
# 作者: fishpj
"""CLI 入口：编排全部流程，输出 ranked_candidates.xlsx。

用法：
    python3 run.py                         # 默认流程
    python3 run.py --top 100 --regime bull # 手动指定候选数与市场环境
    python3 run.py --init-memory           # 写出示例记忆库模板
"""
from __future__ import annotations
import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
import data
import filters
import signals as sig
import scorer
import memory


def parse_args():
    p = argparse.ArgumentParser(description="基于涨跌理论的 A 股选股打分")
    p.add_argument("--top", type=int, default=config.TOP_N_DETAIL,
                   help=f"进入详细打分的候选数（默认 {config.TOP_N_DETAIL}）")
    p.add_argument("--regime", choices=["auto", "neutral", "bull", "bear"],
                   default="auto", help="市场环境")
    p.add_argument("--out", default="ranked_candidates.xlsx",
                   help="输出 Excel 文件名")
    p.add_argument("--init-memory", action="store_true",
                   help="写出示例记忆库模板并退出")
    p.add_argument("--no-cache", action="store_true",
                   help="忽略缓存（强制刷新数据）")
    return p.parse_args()


def step(msg):
    print(f"\n==> {msg}", flush=True)


def main():
    args = parse_args()

    if args.init_memory:
        path = memory.write_seed_template()
        print(f"已写出示例记忆库：{path}")
        return 0

    if args.no_cache:
        # 清空 cache 目录
        for f in Path(__file__).parent.joinpath(config.CACHE_DIR).glob("*"):
            f.unlink()

    # ---- 1. 全市场行情 ----
    step("拉取全市场实时行情…")
    spot = data.get_spot_all()
    if spot.empty:
        print("无法获取行情数据。")
        return 1
    print(f"  获取到 {len(spot)} 只股票")

    # ---- 2. 硬剔除 ----
    step("应用硬剔除条件…")
    pool = filters.apply_hard_filters(spot)
    print(f"  通过剔除后剩余 {len(pool)} 只")

    # ---- 3. 取成交额最大前 N 进入详细打分 ----
    sina_mode = (spot.get("_source") == "sina").any() if "_source" in spot.columns else False
    if sina_mode and "换手率" not in pool.columns:
        step("sina 模式：补 K 线换手率后重新排序候选…")
        # 先用纯成交额取 top N×3 做粗筛，再抓 K 线补换手率重排
        rough = filters.top_by_amount(pool, args.top * 3)
        kline_turn = {}
        for idx, row in rough.reset_index(drop=True).iterrows():
            code = str(row["代码"]).zfill(6)
            try:
                k = data.get_daily_kline(code)
                if k is not None and not k.empty and "换手率" in k.columns:
                    avg = pd.to_numeric(k["换手率"], errors="coerce").tail(5).mean()
                    if pd.notna(avg):
                        kline_turn[code] = float(avg) * 100  # sina 小数 → 百分数
            except Exception:
                pass
            if (idx + 1) % 30 == 0:
                print(f"  补换手率 {idx+1}/{len(rough)}")
        pool = filters.enrich_turnover_from_kline(pool, kline_turn)
        print(f"  补到换手率 {len(kline_turn)} / {len(rough)} 只")

    candidates = filters.top_by_amount(pool, args.top)
    print(f"  选取成交额最大的前 {len(candidates)} 只进入打分")

    # ---- 4. 拉公共数据 ----
    step("拉取近 5 日龙虎榜…")
    lhb = data.get_recent_lhb(config.TIMING["lhb_lookback_days"])
    lhb_codes = set()
    if not lhb.empty:
        for col in ["代码", "股票代码"]:
            if col in lhb.columns:
                lhb_codes = set(lhb[col].astype(str).str.zfill(6).tolist())
                break
    print(f"  龙虎榜上榜 {len(lhb_codes)} 个代码")

    step("拉取龙虎榜机构席位…")
    lhb_inst_df = data.get_recent_lhb_inst(config.TIMING["lhb_lookback_days"])
    lhb_inst_codes = set()
    lhb_inst_top = pd.DataFrame()  # top 20 机构净买入明细，用于并入候选
    lhb_inst_amount = {}  # code -> 机构净买入金额（元）
    if not lhb_inst_df.empty and "代码" in lhb_inst_df.columns:
        amt_col = "机构买入净额" if "机构买入净额" in lhb_inst_df.columns else None
        if amt_col:
            lhb_inst_df = lhb_inst_df.copy()
            lhb_inst_df["_net"] = pd.to_numeric(
                lhb_inst_df[amt_col], errors="coerce").fillna(0)
            lhb_inst_codes = set(
                lhb_inst_df.loc[lhb_inst_df["_net"] > 0, "代码"]
                .astype(str).str.zfill(6))
            lhb_inst_top = (lhb_inst_df[lhb_inst_df["_net"] > 0]
                            .sort_values("_net", ascending=False)
                            .head(20))
            # 金额 dict：用于打分 tiebreaker
            lhb_inst_amount = dict(zip(
                lhb_inst_top["代码"].astype(str).str.zfill(6),
                lhb_inst_top["_net"],
            ))
    print(f"  机构净买入 {len(lhb_inst_codes)} 个代码（top 20 并入候选）")

    # 把机构净买入 top 20 并入候选池（去重，从 spot 中取对应行）
    if not lhb_inst_top.empty and "代码" in pool.columns:
        inst_codes = lhb_inst_top["代码"].astype(str).str.zfill(6).tolist()
        pool_codes = pool["代码"].astype(str).str.zfill(6)
        existing = set(candidates["代码"].astype(str).str.zfill(6)) if not candidates.empty else set()
        new_codes = [c for c in inst_codes if c not in existing]
        if new_codes:
            extra = pool[pool_codes.isin(new_codes)].copy()
            candidates = pd.concat([candidates, extra], ignore_index=True)
            print(f"  并入 {len(extra)} 只机构净买入股（候选总数 → {len(candidates)}）")

    step("拉取解禁明细…")
    lockup = data.get_recent_lockups()
    lockup_codes = set()
    if not lockup.empty:
        for col in ["股票代码", "代码"]:
            if col in lockup.columns:
                lockup_codes = set(lockup[col].astype(str).str.zfill(6).tolist())
                break
    print(f"  近 30 日有解禁的 {len(lockup_codes)} 个代码")

    step("加载记忆库…")
    mem = memory.load()
    if mem.empty:
        print("  记忆库为空（如需初始化：python3 run.py --init-memory）")

    # ---- 5. 逐股打分 ----
    # sina 源无流通市值，需要从 K 线补算后再做二次过滤
    sina_mode = (spot.get("_source") == "sina").any() if "_source" in spot.columns else False
    if sina_mode:
        step(f"对 {len(candidates)} 只候选股抓 K 线 + 二次流通市值过滤（{config.HARD_FILTERS['circ_min_billion']}~{config.HARD_FILTERS['circ_max_billion']}亿）…")
    else:
        step(f"对 {len(candidates)} 只候选股打分…")
    signals_list = []
    filtered_out = 0
    for idx, row in candidates.reset_index(drop=True).iterrows():
        code = str(row["代码"]).zfill(6)
        try:
            kline = data.get_daily_kline(code)
            # sina 源：用 close × outstanding_share 补算流通市值并过滤
            if sina_mode and kline is not None and not kline.empty:
                last = kline.iloc[-1]
                share = pd.to_numeric(last.get("流通股本"), errors="coerce")
                price = pd.to_numeric(last.get("收盘"), errors="coerce")
                if pd.notna(share) and pd.notna(price):
                    circ_yi = share * price / 1e8
                    if not (config.HARD_FILTERS["circ_min_billion"] <= circ_yi <= config.HARD_FILTERS["circ_max_billion"]):
                        filtered_out += 1
                        continue
            fin = data.get_fundamentals(code)
            mem_hit = memory.match(code, mem)
            s = sig.compute_signals(
                code=code, spot_row=row, kline=kline, fund=fin,
                lhb_codes=lhb_codes, lhb_inst_codes=lhb_inst_codes,
                lockup_codes=lockup_codes, memory_hit=mem_hit,
                lhb_inst_amount=lhb_inst_amount.get(code),
            )
            signals_list.append(s)
        except Exception as e:
            print(f"  ! {code} 异常：{e}")
        if (idx + 1) % 10 == 0:
            print(f"  已处理 {idx+1}/{len(candidates)} | 留下 {len(signals_list)} | 剔除 {filtered_out}")

    # ---- 6. 加权打分 ----
    regime = args.regime
    if regime == "auto":
        regime = scorer.detect_market_regime(spot)
    print(f"\n市场环境判定：{regime}")

    df, weights = scorer.score_all(signals_list, regime)
    if df.empty:
        print("没有可输出的结果。")
        return 1

    # ---- 7. 输出 Excel ----
    out_path = Path(__file__).parent / args.out
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        # 主表
        cols = ["代码", "名称", "最新价", "涨跌幅", "总分", "市场环境", "建议",
                "Z1_业绩向好性", "Z2_同质同价", "Z3_形态",
                "Z4_市场关注度", "Z5_最大可卖量", "Z6_特定情境记忆",
                "K_盈亏比", "G_量比", "机构净买入_亿", "PE", "流通市值_亿",
                "Z1_note", "Z2_note", "Z3_note", "Z4_note",
                "Z5_note", "Z6_note", "K_note"]
        cols = [c for c in cols if c in df.columns]
        df[cols].to_excel(w, sheet_name="排名", index=False)
        # 权重表
        scorer.weight_table(weights).to_excel(
            w, sheet_name="权重", index=False)
        # 风控
        pd.DataFrame(
            [(k, v) for k, v in config.RISK.items()],
            columns=["规则", "值"],
        ).to_excel(w, sheet_name="风控", index=False)
        # 元数据
        pd.DataFrame({
            "项": ["生成时间", "市场环境", "候选总数", "门槛分", "权重跃迁数"],
            "值": [datetime.now().strftime("%Y-%m-%d %H:%M"), regime,
                   len(df), config.SCORE_THRESHOLD,
                   (df["权重跃迁"] != "—").sum()],
        }).to_excel(w, sheet_name="元数据", index=False)

    print(f"\n✓ 已输出：{out_path}")
    print(f"  ≥ 门槛 {config.SCORE_THRESHOLD} 分共 "
          f"{(df['总分'] >= config.SCORE_THRESHOLD).sum()} 只")
    print("\nTop 10：")
    show = df.head(10)[["代码", "名称", "最新价", "总分", "建议"]]
    print(show.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
