"""特定情境记忆库（方案第七步接口）。

schema：
    题材/股性, 最近一次表现日期(P1), P1涨幅, P2日期, P2涨幅, P3日期, P3涨幅,
    衰减后预期强度, 下次触发预估日期, 关联代码(逗号分隔)

就近强度原则：P1 占 50%, P2 占 30%, P3 占 15%, P4 占 3% (理论原文值)。
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
import pandas as pd

WEIGHTS = {"P1": 0.50, "P2": 0.30, "P3": 0.15, "P4": 0.03}

DEFAULT_PATH = Path(__file__).parent / "memory_seed.xlsx"


def load(path: Path = None) -> pd.DataFrame:
    path = path or DEFAULT_PATH
    if not path.exists():
        return pd.DataFrame(columns=[
            "题材", "P1日期", "P1涨幅", "P2日期", "P2涨幅",
            "P3日期", "P3涨幅", "关联代码",
        ])
    return pd.read_excel(path)


def decayed_expected_return(row: pd.Series, today: datetime = None) -> float:
    """按就近强度原则 + 时间衰减估算当前预期涨幅。

    time decay: 距今每过 60 日，权重衰减一半（粗略）。
    """
    today = today or datetime.now()
    contrib = 0.0
    for p, w in WEIGHTS.items():
        d = row.get(f"{p}日期")
        r = row.get(f"{p}涨幅")
        if pd.isna(d) or pd.isna(r):
            continue
        try:
            d_ts = pd.Timestamp(d)
            days = max((today - d_ts.to_pydatetime()).days, 0)
        except Exception:
            continue
        decay = 0.5 ** (days / 60)
        contrib += float(r) * w * decay
    return contrib


def match(code: str, mem: pd.DataFrame, today: datetime = None) -> Dict | None:
    """查询 code 是否在记忆库的关联代码里；命中则返回打分与 note。"""
    if mem.empty:
        return None
    today = today or datetime.now()
    hits = mem[mem["关联代码"].fillna("").str.contains(code, na=False)]
    if hits.empty:
        return None
    row = hits.iloc[0]
    exp = decayed_expected_return(row, today)
    # 期望涨幅 ≥ 10% 给 1 分，≥ 20% 给 2 分
    score = 2 if exp >= 0.20 else (1 if exp >= 0.10 else 0)
    return {
        "score": score,
        "note": f"匹配题材【{row['题材']}】 衰减后预期涨幅 {exp*100:.1f}%",
    }


def write_seed_template(path: Path = None):
    """写入一个示例 seed 文件，作为记忆库的起点。"""
    path = path or DEFAULT_PATH
    df = pd.DataFrame([
        {
            "题材": "光模块(算力)",
            "P1日期": "2026-05-15", "P1涨幅": 0.40,
            "P2日期": "2025-11-20", "P2涨幅": 0.25,
            "P3日期": "2025-06-10", "P3涨幅": 0.60,
            "关联代码": "002281,300308,300502",
        },
        {
            "题材": "固态电池",
            "P1日期": "2026-04-10", "P1涨幅": 0.30,
            "P2日期": None, "P2涨幅": None,
            "P3日期": None, "P3涨幅": None,
            "关联代码": "300073,002074",
        },
        {
            "题材": "老妖股反抽",
            "P1日期": "2026-03-22", "P1涨幅": 0.20,
            "P2日期": "2025-12-15", "P2涨幅": 0.15,
            "P3日期": "2025-09-08", "P3涨幅": 0.18,
            "关联代码": "002762,300624",
        },
    ])
    df.to_excel(path, index=False)
    return path
