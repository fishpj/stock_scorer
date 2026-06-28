# 修改记录
# 修改内容: 股票池硬剔除，兼容东财/新浪两套列名，缺失字段自动跳过
# 修改日期: 2026-06-28
# 作者: fishpj
"""股票池硬剔除（方案第一步）。

兼容东财与新浪两套列名：
  - 东财 stock_zh_a_spot_em：代码、名称、流通市值、成交额、涨跌幅、PE、PB、换手率、量比…
  - 新浪 stock_zh_a_spot：代码、名称、最新价、涨跌额、涨跌幅、买入、卖出、昨收、今开、最高、最低、成交量、成交额、时间戳
新浪没有 流通市值/PE/PB/换手率——这些过滤项会自动跳过，由后续 K 线阶段补算。
"""
from __future__ import annotations
import pandas as pd

import config


def apply_hard_filters(spot: pd.DataFrame) -> pd.DataFrame:
    if spot is None or spot.empty:
        return pd.DataFrame()
    df = spot.copy()

    # ST / *ST / 退市
    if config.HARD_FILTERS["exclude_st"] and "名称" in df.columns:
        df = df[~df["名称"].astype(str).str.contains(r"ST|\*ST|退", na=False, regex=True)]

    # 流通市值（东财列名）
    if "流通市值" in df.columns:
        circ_yi = pd.to_numeric(df["流通市值"], errors="coerce") / 1e8
        df = df[
            (circ_yi >= config.HARD_FILTERS["circ_min_billion"])
            & (circ_yi <= config.HARD_FILTERS["circ_max_billion"])
        ]

    # 成交额（两源均有；东财元、新浪元）
    if "成交额" in df.columns:
        amt_w = pd.to_numeric(df["成交额"], errors="coerce") / 1e4
        df = df[amt_w >= config.HARD_FILTERS["amount_min_million"]]

    # 北交所（代码 8/4 开头）、B 股（200/900）
    if "代码" in df.columns:
        def _keep(code: str) -> bool:
            code = str(code).zfill(6)
            if code.startswith(("8", "4")):
                return False
            if code.startswith(("200", "900")):
                return False
            return True
        df = df[df["代码"].apply(_keep)]

    return df.reset_index(drop=True)


def top_by_amount(spot_filtered: pd.DataFrame, n: int) -> pd.DataFrame:
    """活跃度排序选候选。

    东财源：换手率 × 成交额（综合活跃度，小盘高换手也能进圈，Z5 才有意义）
    新浪源：fallback 到纯成交额（无换手率列）
    """
    if spot_filtered is None or spot_filtered.empty:
        return spot_filtered
    df = spot_filtered.copy()
    if "换手率" in df.columns and "成交额" in df.columns:
        df["_amt"] = pd.to_numeric(df["成交额"], errors="coerce")
        df["_turn"] = pd.to_numeric(df["换手率"], errors="coerce").fillna(0)
        # 综合活跃度：成交额 × log(换手率+1)；避免单边大成交额占绝对优势
        import numpy as np
        df["_activity"] = df["_amt"] * np.log1p(df["_turn"])
        df = df.sort_values("_activity", ascending=False)
        return df.head(n).drop(columns=["_amt", "_turn", "_activity"])
    if "成交额" in df.columns:
        df["_amt"] = pd.to_numeric(df["成交额"], errors="coerce")
        return df.sort_values("_amt", ascending=False).head(n).drop(columns="_amt")
    return df.head(n)


def enrich_turnover_from_kline(spot_filtered: pd.DataFrame, kline_turn_map: dict) -> pd.DataFrame:
    """sina 模式：用 K 线近 5 日平均换手率补 spot 的换手率列。

    kline_turn_map: {code6: avg_turnover_pct}
    """
    if spot_filtered is None or spot_filtered.empty:
        return spot_filtered
    df = spot_filtered.copy()
    if "换手率" in df.columns:
        return df
    df["换手率"] = df["代码"].astype(str).str.zfill(6).map(kline_turn_map).fillna(0)
    return df
