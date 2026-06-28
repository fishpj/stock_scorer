# 修改记录
# 修改内容: 加权打分，六大依据按权重矩阵合成总分，支持市场环境切换与强度跃迁临时加权
# 修改日期: 2026-06-28
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


def _regime_adjust_z3(z3_raw: int, note: str, regime: str) -> int:
    """根据 Z3_note（含趋势/偏离）按市场环境重打分。

    note 形如："趋势 走强, 偏离 超涨, 20日 89.1%"
    桶验证显示 bull 下超涨样本占 70% 但 5 日仅 6.73%，原规则
    "走强+超涨=2" 把分堆到追涨顶端。收紧为：
    - bull：走强且非超涨=2，走强+超涨=1，超涨单独=0
    - bear：超跌=2（反转），走弱=1（筑底），走强=0，超涨=-1
    - neutral：走强=1，超跌=1，超涨=0，其它=0
    """
    trend = "走强" if "趋势 走强" in note else ("走弱" if "趋势 走弱" in note else "")
    dev = ""
    for d in ("超涨", "超跌"):
        if f"偏离 {d}" in note:
            dev = d
            break

    if regime == "bull":
        if trend == "走强" and dev != "超涨":
            return 2
        if trend == "走强" and dev == "超涨":
            return 1
        if dev == "超跌":
            return 1
        return 0
    if regime == "bear":
        if dev == "超跌":
            return 2
        if trend == "走弱":
            return 1
        if trend == "走强":
            return 0
        if dev == "超涨":
            return -1
        return 0
    # neutral
    if trend == "走强":
        return 1
    if dev == "超跌":
        return 1
    return 0


def score_all(signals_list: List[Dict], regime: str) -> pd.DataFrame:
    """对每个候选按权重打分，输出 DataFrame（已按总分降序）。"""
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
        z3 = _regime_adjust_z3(z3_raw, str(s.get("Z3_note", "")), regime)
        z3 = max(0, min(2, z3))  # 钳到 [0, 2]
        z4 = s.get(Z_KEYS[3], 0)
        z5 = s.get(Z_KEYS[4], 0)
        z6 = s.get(Z_KEYS[5], 0)
        sub = [z1, z2, z3, z4, z5, z6]
        total = sum(sub[i] * weights[Z_LABELS[i]] for i in range(6)) * 5  # 0~10

        # K 甜点奖励：[0.5, 1.5] +0.5；<0.3 扣 1（超涨末端风险）
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
        # 2) 机构净买入金额（cap 10亿 → +0~0.03）—— 移除 Z4 加分后，
        #    回测显示机构净买入 picks 5日胜率 75.8%（高于无机构 60.1%），
        #    重新作为正向 tiebreaker
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
    # 桶验证显示 5.5-6.0 系统性最优，门槛 5.5；≥6.5 反而追涨风险，标注警惕
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
