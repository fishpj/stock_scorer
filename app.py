# 修改记录
# 修改内容: Streamlit 看板，可视化 ranked_candidates.xlsx；适配 pandas 3.0（applymap→map），流通市值上限放宽到 5000 亿
# 修改日期: 2026-06-28
# 作者: fishpj
"""Streamlit Web 看板：可视化 ranked_candidates.xlsx。

启动：
    streamlit run app.py

依赖（除已有）：
    pip install streamlit plotly
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

import config

ROOT = Path(__file__).parent
OUT = ROOT / "ranked_candidates.xlsx"


@st.cache_data(ttl=10)
def load_data(path: Path):
    if not path.exists():
        return None, None, None, None
    rank = pd.read_excel(path, sheet_name="排名")
    weights = pd.read_excel(path, sheet_name="权重")
    risk = pd.read_excel(path, sheet_name="风控")
    meta_df = pd.read_excel(path, sheet_name="元数据")
    # 转字典方便按字段名查
    meta = dict(zip(meta_df["项"].astype(str), meta_df["值"]))
    return rank, weights, risk, meta


def main():
    st.set_page_config(page_title="A 股选股打分看板", layout="wide", page_icon="📊")
    st.title("📊 A 股选股打分看板")
    st.caption("基于涨跌理论的 G/Z/K/S + 六大依据打分模型")

    rank, weights, risk, meta = load_data(OUT)

    # ----- 顶部控制栏 -----
    col1, col2, col3, col4 = st.columns([2, 2, 1, 1])
    with col1:
        if st.button("🔄 重新跑打分 (run.py)", help="调用 CLI 重抓数据并打分"):
            with st.status("执行中…", expanded=True) as status:
                proc = subprocess.run(
                    [sys.executable, "run.py", "--top", "50"],
                    cwd=str(ROOT),
                    capture_output=True, text=True,
                )
                st.code(proc.stdout[-3000:] or proc.stderr[-3000:])
                status.update(label="完成", state="complete")
            st.cache_data.clear()
            st.rerun()
    with col2:
        if meta is not None:
            threshold = meta.get("门槛分", "—")
            regime = meta.get("市场环境", "—")
            leap = meta.get("权重跃迁数", 0)
            leap_tag = f" ｜ 权重跃迁 {leap} 项" if leap and str(leap) != "0" else ""
            st.info(
                f"生成 {meta.get('生成时间', '—')} ｜ 环境 **{regime}** ｜ "
                f"候选 {meta.get('候选总数', '—')} 只 ｜ 门槛 **{threshold}**{leap_tag}"
            )

    if rank is None:
        st.warning("ranked_candidates.xlsx 不存在。先点上面的「🔄 重新跑打分」。")
        return

    # ----- 侧边栏筛选 -----
    st.sidebar.header("筛选")
    score_min = st.sidebar.slider("最低总分", 0.0, 10.0, float(config.SCORE_THRESHOLD), 0.1)
    z6_only = st.sidebar.checkbox("仅显示有题材命中（Z6 > 0）", value=False)
    inst_only = st.sidebar.checkbox("仅显示机构净买入", value=False)
    cap_max = st.sidebar.slider("流通市值上限（亿）", 100, 5000, 5000, 50)
    show_count = st.sidebar.slider("显示前 N 名", 5, 100, 30)

    df = rank.copy()
    # V3 标记：K 甜点 / 警告
    # 甜点区间 [0.5,1.5] 对齐 scorer.py；K 计算强制 down≥0.001，<0.3 无样本，
    # 警告区间用 <0.4（0.2-0.4 桶 5 日胜率 42.1% 最差）
    if "K_盈亏比" in df.columns:
        def _k_tag(k):
            if pd.isna(k): return ""
            if 0.5 <= k <= 1.5: return "✓甜点"
            if k < 0.4: return "⚠低盈亏比"
            return ""
        df["K状态"] = df["K_盈亏比"].apply(_k_tag)
    df = df[df["总分"] >= score_min]
    if "流通市值_亿" in df.columns:
        df = df[df["流通市值_亿"].fillna(0) <= cap_max]
    if z6_only and "Z6_特定情境记忆" in df.columns:
        df = df[df["Z6_特定情境记忆"] > 0]
    if inst_only and "机构净买入_亿" in df.columns:
        df = df[df["机构净买入_亿"].fillna(0) > 0]
    df = df.head(show_count)

    # ----- Top N 排名条 -----
    st.subheader(f"Top {len(df)} 排名")
    base_cols = ["代码", "名称", "最新价", "涨跌幅", "总分", "建议", "K状态"]
    score_cols = [
        "Z1_业绩向好性", "Z2_同质同价", "Z3_形态",
        "Z4_市场关注度", "Z5_最大可卖量", "Z6_特定情境记忆",
        "K_盈亏比", "G_量比", "机构净买入_亿", "PE", "流通市值_亿",
    ]
    show_cols = [c for c in base_cols + score_cols if c in df.columns]
    # 涨跌幅格式化：+ / - / 着色
    fmt_config = {}
    if "涨跌幅" in df.columns:
        fmt_config["涨跌幅"] = "{:+.2f}%"
    st.dataframe(
        df[show_cols].style.background_gradient(
            subset=["总分"] if "总分" in df.columns else None,
            cmap="RdYlGn", vmin=0, vmax=10)
        .format(fmt_config, na_rep="—")
        .map(
            lambda v: "color: red" if isinstance(v, (int, float)) and v < 0
            else ("color: green" if isinstance(v, (int, float)) and v > 0 else ""),
            subset=["涨跌幅"] if "涨跌幅" in df.columns else None,
        ),
        use_container_width=True, hide_index=True,
    )

    # ----- 图表区 -----
    left, right = st.columns([3, 2])
    with left:
        st.subheader("总分与变量分布")
        chart_df = df.head(20).set_index("名称")[
            [c for c in score_cols if c.startswith("Z") or c == "总分"
             or c in ("K_盈亏比",) and c in df.columns]
        ].copy()
        try:
            import plotly.express as px
            fig = px.bar(
                df.head(20).melt(
                    id_vars=["名称"],
                    value_vars=[c for c in score_cols if c.startswith("Z")],
                    var_name="依据", value_name="得分"),
                x="名称", y="得分", color="依据", barmode="group",
                title="六大依据子分对比（Top 20）",
            )
            fig.update_layout(height=400, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            st.bar_chart(chart_df.drop(columns=["总分"], errors="ignore"))

    with right:
        if weights is not None:
            st.subheader("当前权重矩阵")
            st.dataframe(weights, hide_index=True, use_container_width=True)
        if risk is not None:
            with st.expander("风控规则"):
                st.dataframe(risk, hide_index=True, use_container_width=True)

    # ----- 下钻明细 -----
    st.subheader("🔍 个股下钻")
    if not df.empty:
        sel = st.selectbox("选择股票查看详细 note", df["名称"].tolist())
        row = df[df["名称"] == sel].iloc[0]
        cols = st.columns(3)
        fields = [
            ("最新价", "最新价"),
            ("今日涨跌幅", "涨跌幅"),
            ("Z1 业绩向好性", "Z1_note"),
            ("Z2 同质同价", "Z2_note"),
            ("Z3 形态", "Z3_note"),
            ("Z4 市场关注度", "Z4_note"),
            ("Z5 最大可卖量", "Z5_note"),
            ("Z6 记忆库", "Z6_note"),
            ("K 盈亏比", "K_note"),
            ("机构净买入(亿)", "机构净买入_亿"),
        ]
        for i, (label, col) in enumerate(fields):
            if col in df.columns:
                val = row[col]
                with cols[i % 3]:
                    if col == "今日涨跌幅" and pd.notna(val):
                        delta_color = "inverse" if val < 0 else "normal"
                        st.metric(label, f"{val:+.2f}%", delta_color=delta_color)
                    elif col == "机构净买入_亿" and pd.notna(val):
                        st.metric(label, f"{val:.2f}")
                    else:
                        st.metric(label, f"{val}")


if __name__ == "__main__":
    main()
