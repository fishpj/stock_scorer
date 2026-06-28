"""数据层：封装 akshare，所有调用走文件缓存。

策略：先试东财（_em 系列，字段全），失败时降级到新浪（字段少但稳定）。
缓存命中后会跳过网络请求。
"""
from __future__ import annotations
import hashlib
import json
import os
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config

warnings.filterwarnings("ignore")

CACHE_DIR = Path(__file__).parent / config.CACHE_DIR
CACHE_DIR.mkdir(exist_ok=True)
_CACHE_EXT = "pkl"

ak = None


def _ak():
    global ak
    if ak is None:
        import akshare as _aks
        ak = _aks
    return ak


def _cache_key(name: str, *args) -> str:
    blob = json.dumps({"name": name, "args": [str(a) for a in args]}, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


def _read_cache(key: str, ttl_hours: float):
    path = CACHE_DIR / f"{key}.{_CACHE_EXT}"
    if not path.exists():
        return None
    age = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
    if age > ttl_hours:
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def _write_cache(key: str, df: pd.DataFrame):
    df.to_pickle(CACHE_DIR / f"{key}.{_CACHE_EXT}")
    (CACHE_DIR / f"{key}.meta").write_text(datetime.now().isoformat())


def _cached(name: str, ttl_hours: float, fetcher):
    key = _cache_key(name)
    df = _read_cache(key, ttl_hours)
    if df is not None:
        return df
    df = fetcher()
    if df is not None and not df.empty:
        _write_cache(key, df)
    return df


# ---------------------------------------------------------------------------
# 1. 全市场实时行情：东财失败 → 新浪
# ---------------------------------------------------------------------------
def _spot_em():
    return _ak().stock_zh_a_spot_em()


def _spot_sina():
    """新浪分支：列较少（无 PE/PB/流通市值/换手率），但稳定。"""
    df = _ak().stock_zh_a_spot()
    # 统一规范化
    if df is None or df.empty:
        return pd.DataFrame()
    # 代码去掉前缀（sh/sz/bj）保留 6 位
    df = df.copy()
    df["代码"] = df["代码"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
    df["成交量"] = pd.to_numeric(df["成交量"], errors="coerce")
    df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce")
    return df


def get_spot_all():
    """优先东财；失败降级新浪。"""
    try:
        df = _cached("spot_em", 0.3, _spot_em)
        if df is not None and not df.empty:
            df["_source"] = "em"
            return df
    except Exception as e:
        print(f"  [warn] 东财行情失败：{type(e).__name__}，降级到新浪")
    df = _cached("spot_sina", 0.3, _spot_sina)
    if df is not None:
        df["_source"] = "sina"
    return df.copy() if df is not None else pd.DataFrame()


# ---------------------------------------------------------------------------
# 2. 个股日 K
# ---------------------------------------------------------------------------
def _kline_em(code: str, start: str, end: str):
    return _ak().stock_zh_a_hist(symbol=code, period="daily",
                                 start_date=start, end_date=end, adjust="qfq")


def _kline_sina(code: str):
    """新浪分支。symbol 需 sh/sz/bj 前缀。"""
    code6 = str(code).zfill(6)
    if code6.startswith("6"):
        sym = f"sh{code6}"
    elif code6.startswith(("0", "3")):
        sym = f"sz{code6}"
    elif code6.startswith(("8", "4")):
        sym = f"bj{code6}"
    else:
        sym = f"sz{code6}"
    df = _ak().stock_zh_a_daily(symbol=sym, adjust="qfq")
    if df is None or df.empty:
        return df
    # 翻译为 signals.py 假设的中文列名
    df = df.copy()
    df = df.rename(columns={
        "date": "日期", "open": "开盘", "high": "最高",
        "low": "最低", "close": "收盘", "volume": "成交量",
        "amount": "成交额", "turnover": "换手率",
        "outstanding_share": "流通股本",
    })
    return df


def get_daily_kline(code: str, days: int = 120):
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    key = f"kline_{code}_{days}d"
    cached = _read_cache(key, 24)
    if cached is not None:
        return cached
    # 先东财，失败再新浪
    try:
        df = _kline_em(code, start, end)
        if df is None or df.empty:
            raise RuntimeError("empty")
        df["_source"] = "em"
    except Exception as e:
        try:
            df = _kline_sina(code)
            if df is not None and not df.empty:
                # 仅取最近 days 日
                df = df.tail(days * 2).reset_index(drop=True)
                df["_source"] = "sina"
        except Exception as e2:
            print(f"  [warn] {code} K线 双源失败：{e} / {e2}")
            return pd.DataFrame()
    if df is not None and not df.empty:
        _write_cache(key, df)
    return df


# ---------------------------------------------------------------------------
# 3. 财务摘要（东财）—— 失败降级到同花顺（带 YoY + BPS）
# ---------------------------------------------------------------------------
def get_financials_em(code: str):
    try:
        return _ak().stock_financial_abstract(symbol=code)
    except Exception:
        return pd.DataFrame()


def get_fundamentals(code: str):
    """同花顺财务摘要，给出 营收同比、净利同比、每股净资产、ROE 等。

    返回最新一期的 dict，调用方直接取字段：
      {"报告期", "净利润同比增长率", "营业总收入同比增长率",
       "每股净资产", "净资产收益率", "基本每股收益"}
    """
    df = _cached(
        f"fund_{code}", 24 * 7,
        lambda: _ak().stock_financial_abstract_ths(symbol=code, indicator="按报告期"),
    )
    if df is None or df.empty:
        return {}
    # 取最新一期（按报告期降序）
    df = df.copy()
    df["报告期"] = pd.to_datetime(df["报告期"], errors="coerce")
    df = df.sort_values("报告期", ascending=False)
    row = df.iloc[0]
    out = {}
    for k in ["净利润同比增长率", "营业总收入同比增长率",
              "每股净资产", "净资产收益率", "基本每股收益",
              "扣非净利润同比增长率", "销售毛利率"]:
        v = row.get(k)
        out[k] = v if pd.notna(v) else None
    # 把字符串百分数转 float（如 "52.36%" → 0.5236）
    for k in ["净利润同比增长率", "营业总收入同比增长率",
              "净资产收益率", "销售毛利率"]:
        if isinstance(out.get(k), str) and "%" in out[k]:
            try:
                out[k] = float(out[k].replace("%", "").strip()) / 100.0
            except Exception:
                out[k] = None
    return out


# ---------------------------------------------------------------------------
# 4. 龙虎榜：基础汇总 + 机构席位统计
# ---------------------------------------------------------------------------
def get_recent_lhb(days: int = 5):
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        df = _cached(f"lhb_{start}_{end}", 12,
                     lambda: _ak().stock_lhb_detail_em(start_date=start, end_date=end))
        return df.copy() if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def get_recent_lhb_inst(days: int = 5):
    """机构席位买卖统计。返回 机构买入净额 > 0 的代码集合（机构净买）。"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    return get_lhb_inst_range(start, end)


def get_lhb_range(start: str, end: str):
    """按日期区间拉龙虎榜明细。start/end 为 'YYYYMMDD'。"""
    try:
        df = _cached(f"lhb_{start}_{end}", 24,
                     lambda: _ak().stock_lhb_detail_em(start_date=start, end_date=end))
        return df.copy() if df is not None and not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def get_lhb_inst_range(start: str, end: str):
    """按日期区间拉龙虎榜机构席位统计。"""
    try:
        df = _cached(
            f"lhb_inst_{start}_{end}", 24,
            lambda: _ak().stock_lhb_jgmmtj_em(start_date=start, end_date=end),
        )
        if df is None or df.empty:
            return pd.DataFrame()
        return df.copy()
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# 5. 解禁（仅东财提供）
# ---------------------------------------------------------------------------
def get_recent_lockups(days_back: int = 5, days_forward: int = 30):
    today = datetime.now().date()
    start = (today - timedelta(days=days_back)).strftime("%Y%m%d")
    end = (today + timedelta(days=days_forward)).strftime("%Y%m%d")
    try:
        df = _cached(
            f"lockup_{start}_{end}", 24,
            lambda: _ak().stock_restricted_release_summary_em(
                symbol="全部A股", start_date=start, end_date=end),
        )
        return df.copy() if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()
