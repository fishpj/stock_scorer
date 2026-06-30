# 修改记录
# 修改内容: _regime_adjust_z3 改用 Z3_ret_20d 数值直传（移除正则解析）；_advice 注释同步 V1.12 后格局
# 修改日期: 2026-06-30
# 作者: fishpj
"""加权打分（方案第三步）：六大依据按权重矩阵合成总分。

权重按市场环境（neutral/bull/bear）切换；当某依据强度跃迁时临时加权。
"""
from __future__ import annotations
from typing import List, Dict
import pandas as pd

import config

Z_KEYS = [
    "Z1_业绩向好性",
    "Z2_同质同价",
    "Z3_形态",
    "Z4_市场关注度",
    "Z5_最大可卖量",
    "Z6_特定情境记忆",
]
Z_LABELS = [
    "业绩向好性",
    "同质同价",
    "形态的大概率",
    "市场关注度",
    "最大可卖量",
    "特定情境记忆",
]


def detect_market_regime(spot_all: pd.DataFrame) -> str:
    """粗判市场环境：取全市场涨跌幅均值。

    返回 'bull' / 'bear' / 'neutral'。
    """
    if spot_all is None or spot_all.empty or "涨跌幅" not in spot_all.columns:
        return "neutral"
    avg_chg = pd.to_numeric(spot_all["涨跌幅"], errors="coerce").mean()
    if avg_chg >= 0.5:
        return "bull"
    if avg_chg <= -0.5:
        return "bear"
    return "neutral"


def leap_boost(signals_list: List[Dict]) -> Dict[str, float]:
    """检测哪些依据出现强度跃迁（≥ 30% 的候选在该依据得满分 2）。"""
    if not signals_list:
        return {}
    n = len(signals_list)
    leap = {}
    for key, label in zip(Z_KEYS, Z_LABELS):
        full = sum(1 for s in signals_list if s.get(key, 0) >= 2)
        if full / n >= 0.30:
            leap[label] = config.WEIGHT_LEAP_BOOST
    return leap


def _regime_adjust_z3(z3_raw: int, ret_20d, note: str, regime: str) -> int:
    """根据 Z3_note（趋势/偏离）+ ret_20d（20日涨幅小数）按市场环境重打分。

    V1.12 改动：超涨分级扣分（所有 regime 一致语义），修复 V1.11 倒挂
    （≥6.5 高分池 5 日 60.6%/5.95% 反不如 5.5-6.5 池 69.6%/8.62%。
    根因是 bull 下"走强+超涨=1"变相奖励超涨，且 bear 下超涨=-1
    被钳制 max(0,...) 抹掉）。新规则：
    - 超涨且 20日 ≥ 50% → -2（重超涨）
    - 超涨且 30% ≤ 20日 < 50% → -1（普通超涨）
    - bull：走强且非超涨=2，超跌=1，其它=0
    - bear：超跌=2（反转），走弱=1（筑底），走强=0，超涨按上表扣
    - neutral：走强=1，超跌=1，超涨按上表扣，其它=0

    ret_20d 由 signals.compute_signals 直传（Z3_ret_20d 字段），
    避免从 Z3_note 字符串反向解析的隐式耦合。
    """
    trend = "走强" if "趋势 走强" in note else ("走弱" if "趋势 走弱" in note else "")
    dev = ""
    for d in ("超涨", "超跌"):
        if f"偏离 {d}" in note:
            dev = d
            break

    # 超涨分级扣分（所有 regime 一致）
    overbought_penalty = 0
    if dev == "超涨":
        if ret_20d is not None and ret_20d >= config.PATTERN["overbought_penalty_heavy"]:
            overbought_penalty = -2
        else:
            overbought_penalty = -1  # 0.30 ≤ ret_20d < 0.50

    if regime == "bull":
        if trend == "走强" and dev != "超涨":
            return 2
        if dev == "超跌":
            return 1
        return overbought_penalty
    if regime == "bear":
        if dev == "超跌":
            return 2
        if trend == "走弱":
            return 1
        if trend == "走强":
            return 0
        return overbought_penalty
    # neutral
    if trend == "走强" and dev != "超涨":
        return 1
    if dev == "超跌":
        return 1
    return overbought_penalty


def score_all(signals_list: List[Dict], regime: str) -> pd.DataFrame:
    """对每个候选按权重打分，输出 DataFrame（已按总分降序）。"""
    if config.USE_EQUAL_WEIGHTS:
        weights = {label: 1.0 / len(Z_LABELS) for label in Z_LABELS}
    else:
        weights = dict(config.SCORE_WEIGHTS[regime])
    leap = leap_boost(signals_list)
    # 跃迁加权：从其它依据中按比例收回
    for k in leap:
        weights[k] = weights.get(k, 0) + leap[k]
    total_w = sum(weights.values())
    weights = {k: v / total_w for k, v in weights.items()}  # 归一

    rows = []
    for s in signals_list:
        z1 = s.get(Z_KEYS[0], 0)
        z2 = s.get(Z_KEYS[1], 0)
        z3_raw = s.get(Z_KEYS[2], 0)
        z3 = _regime_adjust_z3(z3_raw, s.get("Z3_ret_20d"), str(s.get("Z3_note", "")), regime)
        z3 = max(-2, min(2, z3))  # V1.12: 钳到 [-2, 2]，让超涨负分穿透到加权求和
        z4 = s.get(Z_KEYS[3], 0)
        z5 = s.get(Z_KEYS[4], 0)
        z6 = s.get(Z_KEYS[5], 0)
        sub = [z1, z2, z3, z4, z5, z6]
        total = sum(sub[i] * weights[Z_LABELS[i]] for i in range(6)) * 5  # 0~10

        # K 甜点奖励 + 超涨末端惩罚
        # 桶验证（V1.1 5 窗口）：0.8-1.0 70.3% / 1.0-1.2 71.2% / 1.2-1.5 90.4% 是最优子区间，
        # 0.5-0.8 仅 57-59% 较差。但 A/B 验证（V1.3）显示收窄到 [0.8,1.5] 反而整体降
        # 2.6pp——甜点宽是为了保护排序，把 0.5-0.8 次优股推到 top-K 替代更差的 K<0.4 股票。
        # V1.8 A/B 重验（V1.5 修复框架）：保留 [0.5,1.5] 59.7% > 收窄 [0.8,1.5] 57.4%，
        # A 独有 63.9% > B 独有 49.6%，结论仍成立。桶级相关 ≠ 因果，保留 [0.5,1.5]。
        # <0.3 扣 1 在 picks 里 n=0 是因惩罚已生效把股票推出 top-5，非死代码，保留。
        k_ratio = s.get("K_盈亏比")
        if pd.notna(k_ratio) and isinstance(k_ratio, (int, float)):
            if 0.5 <= k_ratio <= 1.5:
                total += 0.5
            elif k_ratio < 0.3:
                total -= 1.0
        total = max(0, min(10, total))

        # Tiebreaker：用小数级（< 0.05）打破并列，不改变整数总分档位
        # 1) G 量比 momentum（cap 3.0 → +0~0.03）—— 顺势量能，正向
        g_ratio = s.get("G_量比")
        tiebreak = 0.0
        if pd.notna(g_ratio) and isinstance(g_ratio, (int, float)):
            tiebreak += min(max(g_ratio, 0), 3.0) * 0.01
        # 2) 机构净买入金额 tiebreaker（受 config.TIEBREAKER_INST_ENABLED 开关控制）
        #    V1.2 A/B 实测（5 窗口）：保留 tiebreaker 整体 5 日 67.8% / 7.57%，
        #    移除降到 66.4% / 7.32%。tiebreaker 把 G 量比单独推上的差 picks
        #    （B 独有 42.9% / -0.02%）替换为机构净买入 picks（A 独有 63.3% / 3.66%）。
        #    机构净买入桶整体平均低是右侧尾薄所致，非 tiebreaker 反向。默认开。
        if config.TIEBREAKER_INST_ENABLED:
            inst_amt_yi = s.get("机构净买入_亿")
            if pd.notna(inst_amt_yi) and isinstance(inst_amt_yi, (int, float)):
                tiebreak += min(max(inst_amt_yi, 0), 10.0) / 10.0 * 0.03

        row = dict(s)
        row["Z3_形态"] = z3  # 用调整后的值
        row["总分"] = round(total, 2)
        row["_tiebreak"] = tiebreak  # 仅用于排序，不导出
        row["市场环境"] = regime
        row["权重跃迁"] = " / ".join(leap.keys()) if leap else "—"
        row["建议"] = _advice(total, s)
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["总分", "_tiebreak"], ascending=[False, False]).reset_index(drop=True)
        df = df.drop(columns=["_tiebreak"])
    return df, weights


def _advice(total: float, s: Dict) -> str:
    # V1.12 修复倒挂后 ≥6.5 池 5 日 61.2% > 5.5-6.5 池 55.0%（单调性恢复）。
    # 门槛 5.5 落在 V1.1 桶验证最优区间下界；≥6.5 保留"警惕追涨"标签作风控约束
    # （高分池样本量大、绝对收益仍高于门槛，但单票波动大）。
    if total >= config.SCORE_THRESHOLD and s.get("timing_ok"):
        tag = "（高分警惕追涨）" if total >= 6.5 else ""
        return f"可建仓（择时已满足）{tag}".strip()
    if total >= config.SCORE_THRESHOLD:
        tag = "（高分警惕追涨）" if total >= 6.5 else ""
        return f"进入候选（等待择时）{tag}".strip()
    return "暂不关注"


def weight_table(weights: Dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [(k, round(v * 100, 1)) for k, v in weights.items()],
        columns=["依据", "权重%"],
    )
