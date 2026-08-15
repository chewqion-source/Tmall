from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


st.set_page_config(page_title="达人管理", page_icon="🤝", layout="wide")

REQUIRED_COLUMNS = [
    "联系状态",
    "达人名",
    "ID",
    "账号主页",
    "微信号",
    "邮箱",
    "渠道",
    "推广方式",
    "报价",
    "返点",
    "结算价",
    "发布时间（最早）",
    "发布链接",
    "素材",
    "备注",
]


def get_koc_path() -> Path:
    data_dir = Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))
    return Path(os.environ.get("TMALL_KOC_FILE", data_dir / "koc_management.xlsx"))


@st.cache_data(show_spinner="正在读取达人管理表...")
def load_koc_data(path_text: str, mtime_ns: int, size: int) -> pd.DataFrame:
    del mtime_ns, size
    df = pd.read_excel(path_text, sheet_name=0)
    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA
    df = df[REQUIRED_COLUMNS].copy()
    df["达人名"] = df["达人名"].fillna("").astype(str).str.strip()
    df["ID"] = df["ID"].fillna("").astype(str).str.strip()
    df["联系状态"] = df["联系状态"].fillna("未标记").astype(str).str.strip()
    df["渠道"] = df["渠道"].fillna("未标记").astype(str).str.strip()
    df["推广方式"] = df["推广方式"].fillna("未标记").astype(str).str.strip()
    df["报价"] = pd.to_numeric(df["报价"], errors="coerce")
    df["返点"] = pd.to_numeric(df["返点"], errors="coerce")
    df["结算价"] = pd.to_numeric(df["结算价"], errors="coerce").fillna(0)
    df["发布时间（最早）"] = pd.to_datetime(df["发布时间（最早）"], errors="coerce")
    return df


def option_values(series: pd.Series) -> list[str]:
    return sorted(value for value in series.dropna().astype(str).unique() if value)


def search_rows(df: pd.DataFrame, keyword: str) -> pd.DataFrame:
    keyword = keyword.strip().lower()
    if not keyword:
        return df
    search_columns = ["达人名", "ID", "微信号", "邮箱", "备注", "发布链接", "账号主页"]
    combined = (
        df[search_columns]
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
        .str.lower()
    )
    return df[combined.str.contains(keyword, regex=False)]


koc_path = get_koc_path()

with st.sidebar:
    st.header("达人管理")
    st.link_button(
        "返回主看板",
        os.environ.get("TMALL_DASHBOARD_URL", "http://150.158.133.102/"),
        width="stretch",
    )
    st.link_button(
        "保本 ROI 计算器",
        os.environ.get("TMALL_ROI_URL", "http://150.158.133.102/roi/"),
        width="stretch",
    )
    if st.button("重新读取达人表", width="stretch"):
        st.cache_data.clear()

st.title("达人管理")

if not koc_path.exists():
    st.warning(f"尚未找到达人管理表：{koc_path}")
    st.stop()

df = load_koc_data(str(koc_path), koc_path.stat().st_mtime_ns, koc_path.stat().st_size)

with st.sidebar:
    keyword = st.text_input("搜索达人 / ID / 联系方式 / 备注")
    selected_status = st.multiselect("联系状态", option_values(df["联系状态"]))
    selected_channels = st.multiselect("渠道", option_values(df["渠道"]))
    selected_methods = st.multiselect("推广方式", option_values(df["推广方式"]))

    valid_dates = df["发布时间（最早）"].dropna()
    if valid_dates.empty:
        date_range = None
    else:
        date_range = st.date_input(
            "发布时间范围",
            value=(valid_dates.min().date(), valid_dates.max().date()),
        )

filtered = search_rows(df, keyword)
if selected_status:
    filtered = filtered[filtered["联系状态"].isin(selected_status)]
if selected_channels:
    filtered = filtered[filtered["渠道"].isin(selected_channels)]
if selected_methods:
    filtered = filtered[filtered["推广方式"].isin(selected_methods)]
if date_range and len(date_range) == 2:
    start_date, end_date = pd.to_datetime(date_range[0]), pd.to_datetime(date_range[1])
    date_series = filtered["发布时间（最早）"]
    filtered = filtered[date_series.isna() | date_series.between(start_date, end_date)]

metric_cols = st.columns(4)
metric_cols[0].metric("达人数量", f"{len(filtered):,}")
metric_cols[1].metric("已完结", f"{(filtered['联系状态'] == '已完结').sum():,}")
metric_cols[2].metric("已拒绝", f"{(filtered['联系状态'] == '已拒绝').sum():,}")
metric_cols[3].metric("结算价合计", f"¥{filtered['结算价'].sum():,.2f}")

chart_left, chart_right = st.columns(2)
with chart_left:
    status_counts = filtered["联系状态"].value_counts().reset_index()
    status_counts.columns = ["联系状态", "数量"]
    st.plotly_chart(
        px.bar(status_counts, x="联系状态", y="数量", text_auto=True),
        width="stretch",
    )

with chart_right:
    channel_counts = filtered["渠道"].value_counts().reset_index()
    channel_counts.columns = ["渠道", "数量"]
    st.plotly_chart(
        px.pie(channel_counts, names="渠道", values="数量", hole=0.45),
        width="stretch",
    )

st.subheader("达人明细")
table = filtered.copy()
table["发布时间（最早）"] = table["发布时间（最早）"].dt.strftime("%Y-%m-%d").fillna("")
st.dataframe(
    table,
    width="stretch",
    hide_index=True,
    column_config={
        "账号主页": st.column_config.LinkColumn("账号主页"),
        "发布链接": st.column_config.LinkColumn("发布链接"),
        "报价": st.column_config.NumberColumn("报价", format="¥%.2f"),
        "返点": st.column_config.NumberColumn("返点", format="%.2f"),
        "结算价": st.column_config.NumberColumn("结算价", format="¥%.2f"),
    },
)
