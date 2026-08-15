from __future__ import annotations

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
from product_images import fetch_product_image, product_image_url


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


with st.sidebar:
    st.header("店铺与数据")
    if st.button("重新读取四家店铺", width="stretch"):
        st.cache_data.clear()
    st.link_button(
        "财务上传报表",
        os.environ.get("TMALL_UPLOAD_URL", "http://150.158.133.102:8080/upload/"),
        width="stretch",
    )
    st.link_button(
        "保本 ROI 计算器",
        os.environ.get("TMALL_ROI_URL", "http://150.158.133.102/roi/"),
        width="stretch",
    )
    st.link_button(
        "达人管理",
        os.environ.get("TMALL_KOC_URL", "http://150.158.133.102/koc/"),
        width="stretch",
    )

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

with st.sidebar:
    selected_store = st.selectbox("当前店铺", list(sources), index=0)
    store_daily = all_daily[all_daily["store"] == selected_store].copy()
    complete = complete_daily_series(store_daily)
    summary = build_summary(store_daily, complete)
    products = summary["product_id"].tolist()
    selected_product = st.selectbox("商品 ID", products, index=0)
    trend_range = st.selectbox("趋势日期范围", TREND_RANGE_OPTIONS, index=5)

st.title("天猫四店日报分析")
st.info(f"当前查看：**{selected_store}**　｜　数据截至 {store_daily['date'].max():%Y-%m-%d}")
st.caption("四家店铺分别读取、分别聚合；相同商品 ID 不会跨店合并。")

if selected_product == LEGACY_SUMMARY_PRODUCT_ID:
    st.warning(
        "该时段使用早期日报模板，没有商品 ID，也没有按商品拆分的订单量和盈亏。"
        "这里展示的是当日全店销量、快递单量和结余，未错误归入某个单品。"
    )

selected = complete[complete["product_id"] == selected_product].copy().sort_values("date")
trend_selected = filter_trend_range(selected, trend_range)
selected_summary = summary[summary["product_id"] == selected_product].iloc[0]
latest = selected.iloc[-1]

image_col, metric_area = st.columns([1, 4])
with image_col:
    image_url = product_image_url(selected_product)
    if image_url:
        st.image(image_url, caption=f"商品 {selected_product}", width=160)
    else:
        st.caption("暂无商品图")
        if st.button("抓取当前商品图", width="stretch"):
            try:
                fetch_product_image(selected_product)
            except Exception as exc:
                st.error(f"抓取失败：{exc}")
            else:
                st.rerun()

with metric_area:
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

with st.expander("商品缩略图批量抓取", expanded=False):
    st.caption("自动按商品 ID 从天猫/淘宝公开详情页抓取缩略图，并缓存到服务器。抓取失败不会影响看板数据。")
    max_fetch = st.number_input("本次最多抓取缺失商品数", min_value=1, max_value=100, value=20, step=1)
    if st.button("抓取当前店铺缺失商品图", width="stretch"):
        missing_products = [product for product in products if not product_image_url(product)]
        results = []
        for product in missing_products[: int(max_fetch)]:
            try:
                fetch_product_image(product)
                results.append((product, "成功"))
            except Exception as exc:
                results.append((product, f"失败：{exc}"))
        st.dataframe(pd.DataFrame(results, columns=["商品ID", "结果"]), width="stretch", hide_index=True)
        if any(status == "成功" for _product, status in results):
            st.rerun()

left, right = st.columns(2)
with left:
    st.subheader(f"销量 / 订单量趋势（{trend_range}）")
    sales_fig = make_subplots(specs=[[{"secondary_y": True}]])
    sales_fig.add_trace(
        go.Scatter(
            x=trend_selected["date"],
            y=trend_selected["sales_qty"],
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
            x=trend_selected["date"],
            y=trend_selected["order_count"],
            name="订单量",
            mode="lines+markers",
            line=dict(color="#f59e0b", width=3, dash="dot"),
            marker=dict(size=7),
            hovertemplate="订单量 %{y:,.0f}<extra></extra>",
        ),
        secondary_y=True,
    )
    sales_fig.update_xaxes(title_text="日期")
    sales_fig.update_yaxes(title_text="销量", secondary_y=False)
    sales_fig.update_yaxes(title_text="订单量", secondary_y=True, rangemode="tozero")
    sales_fig.update_layout(
        margin=dict(l=10, r=10, t=20, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    st.plotly_chart(sales_fig, width="stretch")

with right:
    st.subheader(f"盈亏趋势（{trend_range}）")
    profit_colors = ["#16a34a" if value >= 0 else "#dc2626" for value in trend_selected["profit"]]
    profit_fig = go.Figure(
        go.Bar(
            x=trend_selected["date"],
            y=trend_selected["profit"],
            marker_color=profit_colors,
            text=[f"{value:+.2f}" for value in trend_selected["profit"]],
            textposition="outside",
            hovertemplate="日期 %{x|%m-%d}<br>盈亏 ¥%{y:,.2f}<extra></extra>",
        )
    )
    profit_fig.add_hline(y=0, line_color="#64748b", line_width=1)
    profit_fig.update_layout(
        xaxis_title="日期", yaxis_title="盈亏（元）", margin=dict(l=10, r=10, t=20, b=10)
    )
    st.plotly_chart(profit_fig, width="stretch")

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
st.caption("绿色为盈利，红色为亏损；可点击列标题排序。")
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
st.dataframe(styled_summary, width="stretch", hide_index=True, height=520)

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

with st.expander("数据口径与来源"):
    source_lines = "\n".join(f"- {store}：`{path}`" for store, path in sources.items())
    st.markdown(
        f"""
{source_lines}
- 店铺是第一层分组，同一商品 ID 在不同店铺中保持独立。
- 销量取“数量”，订单量取“订单数”，盈亏取“单品结余”。
- 商品 ID 合并区域覆盖的每个明细行都会归入该商品。
- 替换桌面同名日报后，文件修改时间与大小会使数据缓存自动更新；也可点击“重新读取四家店铺”。
- Excel聚合结果同时写入服务器磁盘缓存；文件内容不变时无需重复解析，文件变化后自动重建。
- 缺失日期补 0 后计算日增减；前一日为 0 时，百分比环比显示为“—”。
"""
    )
