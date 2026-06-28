# 修改记录
# 修改内容: 信号提取，G/Z/K/S 四变量与六大依据子项打分（0~2），供 scorer 合成总分
# 修改日期: 2026-06-28
# 作者: fishpj
"""信号提取（方案第二步）：G/Z/K/S 四变量 + 六大判断依据打分。

每个子项打分 0~2，最终在 scorer.py 按权重合成 0~10 总分。
"""
from __future__ import annotations
from typing import Dict, Optional
import pandas as pd
import numpy as np

import config


# ---------------------------------------------------------------------------
# 工具：从 fundamentals dict 取同比
# ---------------------------------------------------------------------------
def _yoy_from_fund(fund: dict) -> dict:
    """fund 来自 data.get_fundamentals；返回归一后的同比 dict。"""
    return {
        "revenue_yoy": fund.get("营业总收入同比增长率"),
        "profit_yoy": fund.get("净利润同比增长率"),
    }


# ---------------------------------------------------------------------------
# Z3 形态的大概率
# ---------------------------------------------------------------------------
def _pattern_score(kline: pd.DataFrame) -> Dict:
    """返回 {score: 0~2, trend: str, deviation: str, ma20: float}。"""
    out = {"score": 0, "trend": "中性", "deviation": "中性", "ma20": None,
           "ret_20d": None}
    if kline is None or kline.empty or len(kline) < 60:
        return out
    close = pd.to_numeric(kline["收盘"], errors="coerce").dropna()
    if len(close) < 60:
        return out
    ma20 = close.rolling(20).mean().iloc[-1]
    ma5 = close.rolling(5).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    last = close.iloc[-1]
    ret_20d = (last / close.iloc[-21] - 1) if len(close) >= 21 else 0.0

    trend = "走强" if ma5 > ma20 > ma60 else ("走弱" if ma5 < ma20 < ma60 else "中性")
    dev = "超涨" if ret_20d > config.PATTERN["overbought_20d"] else ("超跌" if ret_20d < config.PATTERN["oversold_20d"] else "中性")

    score = 0
    if trend == "走强":
        score += 1
    if dev == "超跌":  # 超跌反弹机会
        score += 1
    # 最多 2
    out.update({"score": min(score, 2), "trend": trend, "deviation": dev,
                "ma20": float(ma20), "ret_20d": float(ret_20d)})
    return out


# ---------------------------------------------------------------------------
# 主信号函数
# ---------------------------------------------------------------------------
def compute_signals(
    code: str,
    spot_row: pd.Series,
    kline: Optional[pd.DataFrame],
    fund: Optional[dict],
    lhb_codes: set,
    lhb_inst_codes: set,
    lockup_codes: set,
    memory_hit: Optional[Dict] = None,
    lhb_inst_amount: Optional[float] = None,
) -> Dict:
    """对单只股票计算 G/Z/K/S 与六大判断依据子分。返回扁平 dict。"""
    name = str(spot_row.get("名称", ""))
    price = pd.to_numeric(spot_row.get("最新价"), errors="coerce")
    circ_mv_yi = None
    if "流通市值" in spot_row:
        circ_mv_yi = pd.to_numeric(spot_row.get("流通市值"), errors="coerce") / 1e8
    pe = pd.to_numeric(spot_row.get("市盈率-动态"), errors="coerce")
    pb = pd.to_numeric(spot_row.get("市净率"), errors="coerce")
    turnover = pd.to_numeric(spot_row.get("换手率"), errors="coerce")
    amount = pd.to_numeric(spot_row.get("成交额"), errors="coerce")
    pct_change = pd.to_numeric(spot_row.get("涨跌幅"), errors="coerce")

    # 若新浪源没有流通市值/换手率，则从 K 线末行补算
    if (circ_mv_yi is None or pd.isna(circ_mv_yi)) and kline is not None and not kline.empty:
        last_row = kline.iloc[-1]
        share = pd.to_numeric(last_row.get("流通股本"), errors="coerce")
        if pd.notna(share) and pd.notna(price):
            circ_mv_yi = share * price / 1e8  # 假设流通股本单位为股
    if (turnover is None or pd.isna(turnover)) and "换手率" in (kline.columns if kline is not None else []):
        turnover = pd.to_numeric(kline.iloc[-1].get("换手率"), errors="coerce")
        # sina 的 turnover 是小数（0.0023 = 0.23%），转成百分数读法
        if pd.notna(turnover) and turnover < 1:
            turnover = turnover * 100

    # ---------- Z1 业绩向好性 ----------
    yoy = _yoy_from_fund(fund or {})
    z1 = 0
    if yoy["revenue_yoy"] is not None and yoy["revenue_yoy"] >= config.PERF["revenue_yoy_min"]:
        z1 += 1
    if yoy["profit_yoy"] is not None and yoy["profit_yoy"] >= config.PERF["profit_yoy_min"]:
        z1 += 1
    z1 = min(z1, 2)

    # ---------- Z2 同质同价（用 price/EPS 推 PE，price/BPS 推 PB） ----------
    z2 = 0
    peer_note = "无可比基准"
    bps = pd.to_numeric((fund or {}).get("每股净资产"), errors="coerce")
    eps = pd.to_numeric((fund or {}).get("基本每股收益"), errors="coerce")
    pe_derived = (price / eps) if (pd.notna(price) and pd.notna(eps) and eps > 0) else None
    pb_derived = (price / bps) if (pd.notna(price) and pd.notna(bps) and bps > 0) else None
    if pe is None or pd.isna(pe):
        pe = pe_derived
    if pb is None or pd.isna(pb):
        pb = pb_derived
    if pd.notna(pe) and pe and pe > 0:
        if pe < 15:
            z2 = 2; peer_note = f"PE={pe:.1f} 偏低"
        elif pe < 25:
            z2 = 1; peer_note = f"PE={pe:.1f} 中等"
        elif pe > 60:
            z2 = 0; peer_note = f"PE={pe:.1f} 偏高"

    # ---------- Z3 形态的大概率 ----------
    pat = _pattern_score(kline)
    z3 = pat["score"]

    # ---------- Z4 市场关注度（G） ----------
    # 回测显示 LHB 上榜后股票反而回调（机构追高），故 Z4 只看换手率，
    # LHB/机构净买入仍记录在 note 但不再加分。
    z4 = 0
    g_signals = []
    if pd.notna(turnover) and turnover > 0:
        g_signals.append(f"换手率 {turnover:.1f}%")
    if pd.notna(turnover) and turnover > 5:
        z4 = 2
        g_signals.append("换手率 >5%（高关注）")
    elif pd.notna(turnover) and turnover > 2:
        z4 = 1
        g_signals.append("换手率 >2%")
    if code in lhb_inst_codes:
        g_signals.append("龙虎榜-机构净买入（仅记录）")
    elif code in lhb_codes:
        g_signals.append("龙虎榜上榜（仅记录）")

    # ---------- Z5 最大可卖量（S） ----------
    z5 = 0
    s_note = ""
    if pd.notna(circ_mv_yi):
        if circ_mv_yi < config.SELLABLE["circ_marketcap_micro_billion"]:
            z5 += 2
            s_note = f"流通市值 {circ_mv_yi:.0f}亿 次新小盘"
        elif circ_mv_yi < config.SELLABLE["circ_marketcap_small_billion"]:
            z5 += 1
            s_note = f"流通市值 {circ_mv_yi:.0f}亿 较小"
        if code in lockup_codes:
            z5 -= 1
            s_note += " | 30日内有解禁"
        z5 = max(0, min(z5, 2))

    # ---------- Z6 特定情境记忆 ----------
    z6 = 0
    mem_note = "无匹配"
    if memory_hit:
        z6 = memory_hit.get("score", 0)
        mem_note = memory_hit.get("note", "")

    # ---------- K 盈亏比 ----------
    # 支撑用 MA20 与 5 日低点的较高者；阻力按形态自适应：
    #   超涨：阻力 = 当前价 × 1.10（涨势末端，目标保守）
    #   突破走强：阻力 = 前期 60 日高点（向上看空间，但已被吞没则用 ×1.15）
    #   盘整/超跌：阻力 = MA20 × 1.30（突破后看 30% 空间）
    k_ratio = None
    k_note = ""
    if kline is not None and not kline.empty:
        close = pd.to_numeric(kline["收盘"], errors="coerce").dropna()
        if len(close) >= 20:
            last = close.iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1]
            low_5d = close.tail(5).min()
            support = max(ma20, low_5d)
            lookback = min(len(close), config.RISK_REWARD["resistance_lookback_days"])
            high_prev = close.tail(lookback).max()

            if pat["deviation"] == "超涨":
                resistance = last * 1.10
                res_label = "+10%目标（超涨保守）"
            elif pat["trend"] == "走强" and high_prev > last:
                resistance = high_prev
                res_label = "前高"
            elif pat["deviation"] == "超跌":
                resistance = ma20 * 1.30
                res_label = "MA20×1.30（反弹目标）"
            else:
                resistance = ma20 * 1.30
                res_label = "MA20×1.30（盘整突破）"

            up = (resistance - last) / last if last > 0 else 0
            down = max((last - support) / last, 0.001) if last > 0 else 0.001
            k_ratio = up / down
            k_note = (
                f"支撑 {support:.2f} | 阻力 {res_label} {resistance:.2f} | "
                f"上 {(up*100):.1f}% 下 {(down*100):.1f}%"
            )

    # ---------- 择时 R 值方向信号 ----------
    amount_ratio = None
    if kline is not None and not kline.empty:
        recent_vol = pd.to_numeric(kline["成交量"], errors="coerce").dropna()
        if len(recent_vol) >= 6:
            today_vol = recent_vol.iloc[-1]
            avg5 = recent_vol.iloc[-6:-1].mean()
            if avg5 > 0:
                amount_ratio = today_vol / avg5

    return {
        "代码": code,
        "名称": name,
        "最新价": price,
        "涨跌幅": pct_change,
        "PE": pe,
        "PB": pb,
        "流通市值_亿": circ_mv_yi,
        "Z1_业绩向好性": z1,
        "Z1_note": f"营收同比 {yoy['revenue_yoy']}, 净利同比 {yoy['profit_yoy']}",
        "Z2_同质同价": z2,
        "Z2_note": peer_note + (f" (PE={pe:.1f} PB={pb:.1f})" if pd.notna(pe) and pd.notna(pb) else ""),
        "Z3_形态": z3,
        "Z3_note": f"趋势 {pat['trend']}, 偏离 {pat['deviation']}, 20日 {pat['ret_20d']*100 if pat['ret_20d'] else 0:.1f}%",
        "Z4_市场关注度": z4,
        "Z4_note": " / ".join(g_signals) if g_signals else "—",
        "Z5_最大可卖量": z5,
        "Z5_note": s_note or "—",
        "Z6_特定情境记忆": z6,
        "Z6_note": mem_note,
        "K_盈亏比": round(k_ratio, 2) if k_ratio else None,
        "K_note": k_note,
        "G_量比": round(amount_ratio, 2) if amount_ratio else None,
        "机构净买入_亿": round(lhb_inst_amount / 1e8, 2) if lhb_inst_amount else None,
        "timing_ok": (amount_ratio is not None and amount_ratio >= config.TIMING["amount_ratio_threshold"]),
    }
