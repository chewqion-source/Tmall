from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from data_loader import (
    LEGACY_SUMMARY_PRODUCT_ID,
    STORE_FILE_PATTERNS,
    build_summary,
    complete_daily_series,
    find_store_workbooks,
    load_store_daily,
    validate_known_sample,
)
from ui_helpers import ai_image_url, koc_url, roi_url, sidebar_link, upload_url


st.set_page_config(page_title="天猫四店日报分析", page_icon="📊", layout="wide")


@st.cache_data(show_spinner="正在读取并聚合四家店铺的 Excel…")
def load_dashboard_data(
    source_signature: tuple[tuple[str, str, int, int], ...], schema_version: str
):
    del schema_version
    workbooks = {store: Path(path) for store, path, _mtime, _size in source_signature}
    daily = load_store_daily(workbooks)
    ylj_daily = daily[daily["store"] == "易丽洁"]
    sample = validate_known_sample(ylj_daily) if not ylj_daily.empty else None
    return daily, sample


def signed(value: float, digits: int = 2) -> str:
    return f"{value:+,.{digits}f}"


def color_profit(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 0:
        return "color: #15803d; background-color: #dcfce7; font-weight: 700"
    if number < 0:
        return "color: #b91c1c; background-color: #fee2e2; font-weight: 700"
    return "color: #475569"


@st.cache_data(show_spinner=False)
def load_product_thumbnails(store: str) -> dict[str, str]:
    image_dir = Path(__file__).resolve().parent / "static" / "product_images" / store
    if not image_dir.exists():
        return {}
    thumbnails: dict[str, str] = {}
    for image_path in image_dir.iterdir():
        if not image_path.is_file():
            continue
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/webp"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        thumbnails[image_path.stem] = f"data:{mime_type};base64,{encoded}"
    return thumbnails


TREND_RANGE_OPTIONS = ("今日", "昨日", "3天", "7天", "15天", "近一个月", "近半年")


def filter_trend_range(data: pd.DataFrame, range_label: str) -> pd.DataFrame:
    latest_date = data["date"].max()
    if range_label == "今日":
        filtered = data[data["date"] == latest_date]
    elif range_label == "昨日":
        filtered = data[data["date"] == latest_date - pd.Timedelta(days=1)]
    else:
        days_by_label = {
            "3天": 3,
            "7天": 7,
            "15天": 15,
            "近一个月": 30,
            "近半年": 183,
        }
        days = days_by_label[range_label]
        start_date = latest_date - pd.Timedelta(days=days - 1)
        filtered = data[data["date"] >= start_date]
    return filtered if not filtered.empty else data.tail(1)


def render_sales_orders_trend(data: pd.DataFrame, height: int = 360) -> None:
    sales_fig = make_subplots(specs=[[{"secondary_y": True}]])
    sales_fig.add_trace(
        go.Scatter(
            x=data["date"],
            y=data["sales_qty"],
            name="销量",
            mode="lines+markers",
            line=dict(color="#2563eb", width=3),
            marker=dict(size=7),
            hovertemplate="销量 %{y:,.0f}<extra></extra>",
        ),
        secondary_y=False,
    )
    sales_fig.add_trace(
        go.Scatter(
            x=data["date"],
            y=data["order_count"],
            name="订单量",
            mode="lines+markers",
            line=dict(color="#f59e0b", width=3, dash="dot"),
            marker=dict(size=7),
            hovertemplate="订单量 %{y:,.0f}<extra></extra>",
        ),
        secondary_y=True,
    )
    sales_fig.update_xaxes(title_text="")
    sales_fig.update_yaxes(title_text="销量", secondary_y=False)
    sales_fig.update_yaxes(title_text="订单", secondary_y=True, rangemode="tozero")
    sales_fig.update_layout(
        height=height,
        margin=dict(l=6, r=6, t=8, b=4),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(size=11)),
    )
    st.plotly_chart(sales_fig, width="stretch")


def render_profit_trend(data: pd.DataFrame, height: int = 360) -> None:
    profit_colors = ["#16a34a" if value >= 0 else "#dc2626" for value in data["profit"]]
    profit_fig = go.Figure(
        go.Bar(
            x=data["date"],
            y=data["profit"],
            marker_color=profit_colors,
            text=[f"{value:+.2f}" for value in data["profit"]],
            textposition="outside",
            hovertemplate="日期 %{x|%m-%d}<br>盈亏 ¥%{y:,.2f}<extra></extra>",
        )
    )
    profit_fig.add_hline(y=0, line_color="#64748b", line_width=1)
    profit_fig.update_layout(
        height=height,
        xaxis_title="",
        yaxis_title="盈亏",
        margin=dict(l=6, r=6, t=8, b=4),
    )
    st.plotly_chart(profit_fig, width="stretch")


def _product_axis_label(product_id: object) -> str:
    text = str(product_id)
    if len(text) <= 12:
        return text
    return f"{text[:6]}...{text[-4:]}"


def _format_money(value: float) -> str:
    return f"¥{value:,.2f}"


def _render_rank_bars(
    data: pd.DataFrame,
    value_col: str,
    title: str,
    color: str,
    empty_text: str,
) -> None:
    st.caption(title)
    if data.empty:
        st.info(empty_text)
        return

    max_abs = max(abs(float(value)) for value in data[value_col]) or 1
    rows: list[str] = []
    for index, row in data.reset_index(drop=True).iterrows():
        value = float(row[value_col])
        width = max(abs(value) / max_abs * 100, 8)
        product_id = str(row["商品ID"])
        short_id = _product_axis_label(product_id)
        rows.append(
            f"""
            <div class="rank-row">
                <div class="rank-head">
                    <span class="rank-no">{index + 1}</span>
                    <span class="rank-id" title="{product_id}">{short_id}</span>
                    <strong>{_format_money(value)}</strong>
                </div>
                <div class="rank-track">
                    <div class="rank-fill" style="width:{width:.1f}%; background:{color};"></div>
                </div>
                <div class="rank-meta">销量 {float(row["销量"]):,.0f} ｜ 订单 {float(row["订单量"]):,.0f}</div>
            </div>
            """
        )

    st.markdown(
        """
        <style>
        .rank-row { margin: 0 0 14px 0; }
        .rank-head {
            display: grid;
            grid-template-columns: 28px 1fr auto;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            color: #0f172a;
        }
        .rank-no {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 22px;
            height: 22px;
            border-radius: 50%;
            background: #eef2ff;
            color: #3730a3;
            font-weight: 700;
        }
        .rank-id {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: #334155;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        }
        .rank-track {
            height: 8px;
            border-radius: 999px;
            background: #e2e8f0;
            overflow: hidden;
            margin: 6px 0 4px 36px;
        }
        .rank-fill {
            height: 100%;
            border-radius: 999px;
        }
        .rank-meta {
            margin-left: 36px;
            font-size: 12px;
            color: #64748b;
        }
        </style>
        """
        + "\n".join(rows),
        unsafe_allow_html=True,
    )


def render_latest_store_snapshot(all_daily: pd.DataFrame) -> None:
    latest_date = all_daily["date"].max()
    latest_rows = all_daily[all_daily["date"] == latest_date]
    latest_overview = (
        latest_rows.groupby("store", as_index=False)
        .agg(
            销量=("sales_qty", "sum"),
            订单量=("order_count", "sum"),
            实时盈亏=("profit", "sum"),
            商品数=("product_id", "nunique"),
        )
        .rename(columns={"store": "店铺"})
        .sort_values("实时盈亏", ascending=False, ignore_index=True)
    )

    st.subheader(f"{latest_date:%Y-%m-%d} 店铺实时盈亏汇总")
    metric_cols = st.columns(4)
    metric_cols[0].metric("店铺数", f"{len(latest_rows['store'].unique()):,.0f}")
    metric_cols[1].metric("总销量", f"{latest_overview['销量'].sum():,.0f}")
    metric_cols[2].metric("总订单量", f"{latest_overview['订单量'].sum():,.0f}")
    metric_cols[3].metric("实时盈亏", f"¥{latest_overview['实时盈亏'].sum():,.2f}")

    max_profit = max(abs(float(value)) for value in latest_overview["实时盈亏"]) or 1
    cards = []
    for _, row in latest_overview.iterrows():
        profit = float(row["实时盈亏"])
        width = max(abs(profit) / max_profit * 100, 8)
        color = "#16a34a" if profit >= 0 else "#dc2626"
        cards.append(
            f"""
            <div class="store-card">
                <div class="store-title">{row["店铺"]}</div>
                <div class="store-profit" style="color:{color};">{_format_money(profit)}</div>
                <div class="store-track"><div class="store-fill" style="width:{width:.1f}%; background:{color};"></div></div>
                <div class="store-meta">
                    <span>销量 {float(row["销量"]):,.0f}</span>
                    <span>订单 {float(row["订单量"]):,.0f}</span>
                    <span>商品 {float(row["商品数"]):,.0f}</span>
                </div>
            </div>
            """
        )

    st.markdown(
        """
        <style>
        .store-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 12px;
            margin: 4px 0 18px 0;
        }
        .store-card {
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 14px 16px;
            background: #ffffff;
        }
        .store-title {
            font-size: 14px;
            color: #475569;
            font-weight: 700;
            margin-bottom: 8px;
        }
        .store-profit {
            font-size: 26px;
            line-height: 1.1;
            font-weight: 800;
            margin-bottom: 12px;
        }
        .store-track {
            height: 9px;
            border-radius: 999px;
            background: #e2e8f0;
            overflow: hidden;
            margin-bottom: 10px;
        }
        .store-fill {
            height: 100%;
            border-radius: 999px;
        }
        .store-meta {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            font-size: 12px;
            color: #64748b;
        }
        </style>
        <div class="store-grid">
        """
        + "\n".join(cards)
        + "</div>",
        unsafe_allow_html=True,
    )


def render_latest_product_extremes(all_daily: pd.DataFrame) -> None:
    latest_date = all_daily["date"].max()
    latest_rows = all_daily[all_daily["date"] == latest_date].copy()
    if latest_rows.empty:
        return

    product_daily = (
        latest_rows.groupby(["store", "product_id"], as_index=False)
        .agg(
            销量=("sales_qty", "sum"),
            订单量=("order_count", "sum"),
            实时盈亏=("profit", "sum"),
        )
    )

    st.subheader(f"{latest_date:%Y-%m-%d} 店铺商品盈利 / 亏损 TOP5")
    stores = sorted(product_daily["store"].unique())
    tabs = st.tabs(stores)

    for tab, store in zip(tabs, stores):
        store_products = product_daily[product_daily["store"] == store].copy()
        if store_products.empty:
            continue

        profit_top = (
            store_products[store_products["实时盈亏"] > 0]
            .sort_values("实时盈亏", ascending=False)
            .head(5)
            .rename(columns={"product_id": "商品ID"})
        )
        loss_top = (
            store_products[store_products["实时盈亏"] < 0]
            .sort_values("实时盈亏", ascending=True)
            .head(5)
            .rename(columns={"product_id": "商品ID"})
        )

        with tab:
            left_col, right_col = st.columns(2)
            with left_col:
                _render_rank_bars(
                    profit_top,
                    "实时盈亏",
                    "实时 TOP5 盈利产品",
                    "#16a34a",
                    "暂无盈利产品",
                )
            with right_col:
                _render_rank_bars(
                    loss_top,
                    "实时盈亏",
                    "实时 TOP5 亏损产品",
                    "#dc2626",
                    "暂无亏损产品",
                )


with st.sidebar:
    st.header("店铺与数据")
    sidebar_link("财务上传报表", upload_url())
    sidebar_link("投产计算器", roi_url())
    sidebar_link("达人管理", koc_url())
    sidebar_link("AI 生图", ai_image_url())

try:
    sources = find_store_workbooks()
    source_signature = tuple(
        (
            store,
            str(path),
            path.stat().st_mtime_ns,
            path.stat().st_size,
        )
        for store, path in sources.items()
    )
    all_daily, sample = load_dashboard_data(source_signature, "four-stores-v4")
except Exception as exc:
    st.error(f"读取失败：{exc}")
    st.stop()

title_col, refresh_col = st.columns([5, 1], vertical_alignment="center")
with title_col:
    st.title("天猫四店日报分析")
with refresh_col:
    if st.button("刷新数据", type="primary", width="stretch"):
        st.cache_data.clear()
        st.rerun()

render_latest_store_snapshot(all_daily)
render_latest_product_extremes(all_daily)

filter_cols = st.columns([1.2, 1.4, 1])
with filter_cols[0]:
    selected_store = st.selectbox("当前店铺", list(sources), index=0)
store_daily = all_daily[all_daily["store"] == selected_store].copy()
complete = complete_daily_series(store_daily)
summary = build_summary(store_daily, complete)
products = summary["product_id"].tolist()
with filter_cols[1]:
    selected_product = st.selectbox("商品 ID", products, index=0)
with filter_cols[2]:
    trend_range = st.selectbox("趋势日期范围", TREND_RANGE_OPTIONS, index=5)

st.info(f"当前查看：**{selected_store}**　｜　数据截至 {store_daily['date'].max():%Y-%m-%d}")

if selected_product == LEGACY_SUMMARY_PRODUCT_ID:
    st.warning(
        "该时段使用早期日报模板，没有商品 ID，也没有按商品拆分的订单量和盈亏。"
        "这里展示的是当日全店销量、快递单量和结余，未错误归入某个单品。"
    )

selected = complete[complete["product_id"] == selected_product].copy().sort_values("date")
trend_selected = filter_trend_range(selected, trend_range)
selected_summary = summary[summary["product_id"] == selected_product].iloc[0]
latest = selected.iloc[-1]
store_trend = (
    store_daily.groupby(["date", "sheet"], as_index=False)
    .agg(sales_qty=("sales_qty", "sum"), order_count=("order_count", "sum"), profit=("profit", "sum"))
    .sort_values("date", ignore_index=True)
)
trend_store = filter_trend_range(store_trend, trend_range)

metric_cols = st.columns(4)
metric_cols[0].metric("累计销量", f"{selected_summary['total_sales']:,.0f}")
metric_cols[1].metric(
    "累计盈亏",
    f"¥{selected_summary['total_profit']:,.2f}",
    delta=signed(selected_summary["total_profit"]),
    delta_color="normal",
)
metric_cols[2].metric(
    f"{latest['sheet']} 销量",
    f"{latest['sales_qty']:,.0f}",
    delta=signed(latest["sales_change"], 0) if pd.notna(latest["sales_change"]) else None,
)
metric_cols[3].metric(
    f"{latest['sheet']} 盈亏",
    f"¥{latest['profit']:,.2f}",
    delta=signed(latest["profit_change"]) if pd.notna(latest["profit_change"]) else None,
)

st.subheader(f"核心趋势（{trend_range}）")
chart_cols = st.columns(4)
with chart_cols[0]:
    st.markdown("**累计销量趋势**")
    render_sales_orders_trend(trend_store, height=300)
with chart_cols[1]:
    st.markdown("**累计盈亏趋势**")
    render_profit_trend(trend_store, height=300)
with chart_cols[2]:
    st.markdown("**单品最新销量趋势**")
    render_sales_orders_trend(trend_selected, height=300)
with chart_cols[3]:
    st.markdown("**单品最新盈亏趋势**")
    render_profit_trend(trend_selected, height=300)

st.subheader("销量 / 盈亏日环比变化")
changes = selected[
    ["sheet", "sales_qty", "sales_change", "sales_change_pct", "profit", "profit_change", "profit_change_pct"]
].rename(
    columns={
        "sheet": "日期",
        "sales_qty": "销量",
        "sales_change": "销量日增减",
        "sales_change_pct": "销量日环比",
        "profit": "盈亏",
        "profit_change": "盈亏日增减",
        "profit_change_pct": "盈亏变化率",
    }
)
styled_changes = changes.style.map(color_profit, subset=["盈亏", "盈亏日增减"]).format(
    {
        "销量": "{:,.0f}",
        "销量日增减": lambda value: "—" if pd.isna(value) else f"{value:+,.0f}",
        "销量日环比": lambda value: "—" if pd.isna(value) else f"{value:+.1%}",
        "盈亏": "{:+,.2f}",
        "盈亏日增减": lambda value: "—" if pd.isna(value) else f"{value:+,.2f}",
        "盈亏变化率": lambda value: "—" if pd.isna(value) else f"{value:+.1%}",
    }
)
st.dataframe(styled_changes, width="stretch", hide_index=True)

st.subheader(f"{selected_store}商品汇总表")
summary_view = summary.rename(
    columns={
        "product_id": "商品ID",
        "total_sales": "累计销量",
        "total_orders": "累计订单量",
        "total_profit": "累计盈亏",
        "active_days": "有销量日期数",
        "sku_count": "SKU数",
        "latest_sales": "最新销量",
        "latest_orders": "最新订单量",
        "latest_profit": "最新盈亏",
        "latest_sales_change": "最新销量增减",
        "latest_profit_change": "最新盈亏增减",
        "avg_daily_sales": "日均销量",
        "avg_daily_orders": "日均订单量",
        "avg_daily_profit": "日均盈亏",
    }
)
product_thumbnails = load_product_thumbnails(selected_store)
summary_view.insert(0, "商品图", summary_view["商品ID"].map(product_thumbnails).fillna(""))
profit_columns = ["累计盈亏", "最新盈亏", "最新盈亏增减", "日均盈亏"]
styled_summary = summary_view.style.map(color_profit, subset=profit_columns).format(
    {
        "累计销量": "{:,.0f}",
        "累计订单量": "{:,.0f}",
        "累计盈亏": "{:+,.2f}",
        "最新销量": "{:,.0f}",
        "最新订单量": "{:,.0f}",
        "最新盈亏": "{:+,.2f}",
        "最新销量增减": "{:+,.0f}",
        "最新盈亏增减": "{:+,.2f}",
        "日均销量": "{:,.1f}",
        "日均订单量": "{:,.1f}",
        "日均盈亏": "{:+,.2f}",
    }
)
st.dataframe(
    styled_summary,
    width="stretch",
    hide_index=True,
    height=520,
    row_height=64,
    column_config={"商品图": st.column_config.ImageColumn("商品图", width="small")},
)

st.subheader("四家店铺汇总对比")
store_overview = (
    all_daily.groupby("store", as_index=False)
    .agg(
        起始日期=("date", "min"),
        截止日期=("date", "max"),
        日期数=("date", "nunique"),
        商品数=("product_id", "nunique"),
        总销量=("sales_qty", "sum"),
        总订单量=("order_count", "sum"),
        总盈亏=("profit", "sum"),
    )
    .rename(columns={"store": "店铺"})
)
store_overview["起始日期"] = store_overview["起始日期"].dt.strftime("%Y-%m-%d")
store_overview["截止日期"] = store_overview["截止日期"].dt.strftime("%Y-%m-%d")
styled_overview = store_overview.style.map(color_profit, subset=["总盈亏"]).format(
    {"总销量": "{:,.0f}", "总订单量": "{:,.0f}", "总盈亏": "{:+,.2f}"}
)
st.dataframe(styled_overview, width="stretch", hide_index=True)
