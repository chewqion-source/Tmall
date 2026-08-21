from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
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

SKU_COST_HEADERS = [
    "店铺",
    "商品ID",
    "商家编码",
    "SKU规格",
    "单件货价",
    "快递费",
    "备注",
    "首次发现日期",
    "最近成交日期",
]
SKU_COST_PATH = Path(
    os.environ.get(
        "SKU_COST_FILE",
        Path(__file__).resolve().parent / "data" / "sku_cost.xlsx",
    )
)


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


def _empty_sku_cost_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=SKU_COST_HEADERS)


def load_sku_cost_frame(path: Path = SKU_COST_PATH) -> pd.DataFrame:
    if not path.exists():
        return _empty_sku_cost_frame()

    data = pd.read_excel(path, dtype={"店铺": str, "商品ID": str, "商家编码": str, "SKU规格": str})
    for column in SKU_COST_HEADERS:
        if column not in data.columns:
            data[column] = ""
    data = data[SKU_COST_HEADERS].copy()
    for column in ["单件货价", "快递费"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    for column in ["店铺", "商品ID", "商家编码", "SKU规格", "备注", "首次发现日期", "最近成交日期"]:
        data[column] = data[column].fillna("").astype(str)
    return data


def save_sku_cost_frame(data: pd.DataFrame, path: Path = SKU_COST_PATH) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if path.exists():
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"sku_cost_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        backup_path.write_bytes(path.read_bytes())

    cleaned = data.copy()
    for column in SKU_COST_HEADERS:
        if column not in cleaned.columns:
            cleaned[column] = ""
    cleaned = cleaned[SKU_COST_HEADERS]
    for column in ["店铺", "商品ID", "商家编码", "SKU规格", "备注", "首次发现日期", "最近成交日期"]:
        cleaned[column] = cleaned[column].fillna("").astype(str).str.strip()
    for column in ["单件货价", "快递费"]:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce").round(2)

    has_key = (
        cleaned["店铺"].ne("")
        | cleaned["商品ID"].ne("")
        | cleaned["商家编码"].ne("")
        | cleaned["SKU规格"].ne("")
    )
    cleaned = cleaned[has_key].drop_duplicates(
        subset=["店铺", "商品ID", "商家编码", "SKU规格"],
        keep="last",
    )
    cleaned.to_excel(path, index=False, sheet_name="SKU成本配置")
    return backup_path


def sku_cost_download_bytes(data: pd.DataFrame) -> bytes:
    output = BytesIO()
    data.to_excel(output, index=False, sheet_name="SKU成本配置")
    return output.getvalue()


def render_sku_cost_manager() -> None:
    st.title("SKU 成本维护")
    st.caption(f"当前文件：{SKU_COST_PATH}")

    uploaded = st.file_uploader("导入现有 sku_cost.xlsx", type=["xlsx"])
    if uploaded is not None:
        imported = pd.read_excel(uploaded, dtype={"店铺": str, "商品ID": str, "商家编码": str, "SKU规格": str})
        backup_path = save_sku_cost_frame(imported)
        st.success(
            "已导入并保存。"
            + (f" 旧文件备份：{backup_path.name}" if backup_path else "")
        )
        st.rerun()

    data = load_sku_cost_frame()
    missing_cost = data["单件货价"].isna() | data["快递费"].isna()
    metric_cols = st.columns(4)
    metric_cols[0].metric("SKU 行数", f"{len(data):,.0f}")
    metric_cols[1].metric("已填成本", f"{len(data) - int(missing_cost.sum()):,.0f}")
    metric_cols[2].metric("待补成本", f"{int(missing_cost.sum()):,.0f}")
    metric_cols[3].metric("涉及店铺", f"{data['店铺'].replace('', pd.NA).dropna().nunique():,.0f}")

    filter_cols = st.columns([1, 1.2, 1])
    stores = sorted(store for store in data["店铺"].dropna().unique() if str(store).strip())
    with filter_cols[0]:
        selected_store = st.selectbox("店铺筛选", ["全部"] + stores)
    with filter_cols[1]:
        keyword = st.text_input("搜索商品ID / 商家编码 / SKU规格")
    with filter_cols[2]:
        only_missing = st.toggle("只看待补成本", value=False)

    view = data.copy()
    view["_row_id"] = view.index
    if selected_store != "全部":
        view = view[view["店铺"] == selected_store]
    if keyword:
        query = keyword.strip()
        view = view[
            view["商品ID"].str.contains(query, case=False, na=False)
            | view["商家编码"].str.contains(query, case=False, na=False)
            | view["SKU规格"].str.contains(query, case=False, na=False)
        ]
    if only_missing:
        view = view[view["单件货价"].isna() | view["快递费"].isna()]

    st.caption("可直接修改单件货价、快递费，也可以在最后新增行。保存后会写回线上 sku_cost.xlsx。")
    edited = st.data_editor(
        view,
        hide_index=True,
        num_rows="dynamic",
        width="stretch",
        height=560,
        column_config={
            "店铺": st.column_config.SelectboxColumn("店铺", options=["易丽洁", "咖时光", "坐拥_宁静", "坐拥宁静"]),
            "商品ID": st.column_config.TextColumn("商品ID"),
            "商家编码": st.column_config.TextColumn("商家编码"),
            "SKU规格": st.column_config.TextColumn("SKU规格"),
            "单件货价": st.column_config.NumberColumn("单件货价", min_value=0, step=0.01, format="¥%.2f"),
            "快递费": st.column_config.NumberColumn("快递费", min_value=0, step=0.01, format="¥%.2f"),
            "备注": st.column_config.TextColumn("备注"),
            "首次发现日期": st.column_config.TextColumn("首次发现日期"),
            "最近成交日期": st.column_config.TextColumn("最近成交日期"),
            "_row_id": None,
        },
        key="sku_cost_editor",
    )

    action_cols = st.columns([1, 1, 4])
    with action_cols[0]:
        if st.button("保存成本表", type="primary", width="stretch"):
            edited_existing = edited[pd.to_numeric(edited["_row_id"], errors="coerce").notna()].copy()
            edited_new = edited[pd.to_numeric(edited["_row_id"], errors="coerce").isna()].copy()
            save_data = data.copy()
            for _, row in edited_existing.iterrows():
                row_id = int(row["_row_id"])
                if row_id in save_data.index:
                    save_data.loc[row_id, SKU_COST_HEADERS] = row[SKU_COST_HEADERS].to_list()
            if not edited_new.empty:
                save_data = pd.concat(
                    [save_data, edited_new[SKU_COST_HEADERS]],
                    ignore_index=True,
                )
            backup_path = save_sku_cost_frame(save_data)
            st.success(
                "保存成功。"
                + (f" 已备份旧文件：{backup_path.name}" if backup_path else "")
            )
            st.rerun()
    with action_cols[1]:
        st.download_button(
            "下载成本表",
            data=sku_cost_download_bytes(data),
            file_name="sku_cost.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )


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


def _render_product_rank_chart(
    data: pd.DataFrame,
    value_col: str,
    title: str,
    empty_text: str,
    color: str,
) -> None:
    if data.empty:
        st.caption(empty_text)
        return

    chart_data = data.iloc[::-1].copy()
    chart_data["短商品ID"] = chart_data["商品ID"].map(_product_axis_label)
    chart_data["标签"] = (
        chart_data["店铺"].astype(str)
        + " #"
        + chart_data["排名"].astype(str)
        + "  "
        + chart_data["短商品ID"]
    )
    fig = go.Figure(
        go.Bar(
            x=chart_data[value_col],
            y=chart_data["标签"],
            orientation="h",
            marker_color=color,
            text=[_format_money(float(value)) for value in chart_data[value_col]],
            textposition="outside",
            customdata=chart_data[["商品ID", "销量", "订单量"]],
            hovertemplate=(
                "商品 %{customdata[0]}<br>"
                "销量 %{customdata[1]:,.0f}<br>"
                "订单 %{customdata[2]:,.0f}<br>"
                "实时盈亏 %{x:,.2f}<extra></extra>"
            ),
        )
    )
    fig.add_vline(x=0, line_color="#94a3b8", line_width=1)
    fig.update_layout(
        title=dict(text=title, font=dict(size=14)),
        height=min(450, 96 + 28 * len(chart_data)),
        margin=dict(l=6, r=36, t=34, b=8),
        xaxis_title="",
        yaxis_title="",
        showlegend=False,
        font=dict(size=11),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e5e7eb", zeroline=False)
    fig.update_yaxes(tickfont=dict(size=10))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def _render_store_profit_chart(latest_overview: pd.DataFrame) -> None:
    chart_data = latest_overview.sort_values("实时盈亏", ascending=True).copy()
    colors = ["#16a34a" if value >= 0 else "#dc2626" for value in chart_data["实时盈亏"]]
    fig = go.Figure(
        go.Bar(
            x=chart_data["实时盈亏"],
            y=chart_data["店铺"],
            orientation="h",
            marker_color=colors,
            text=[_format_money(float(value)) for value in chart_data["实时盈亏"]],
            textposition="outside",
            customdata=chart_data[["销量", "订单量", "商品数"]],
            hovertemplate=(
                "销量 %{customdata[0]:,.0f}<br>"
                "订单 %{customdata[1]:,.0f}<br>"
                "商品 %{customdata[2]:,.0f}<br>"
                "实时盈亏 %{x:,.2f}<extra></extra>"
            ),
        )
    )
    fig.add_vline(x=0, line_color="#94a3b8", line_width=1)
    fig.update_layout(
        height=220,
        margin=dict(l=6, r=42, t=10, b=8),
        xaxis_title="",
        yaxis_title="",
        showlegend=False,
        font=dict(size=12),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e5e7eb", zeroline=False)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def _build_product_rank_rows(product_daily: pd.DataFrame) -> pd.DataFrame:
    rank_rows: list[pd.DataFrame] = []
    for store in sorted(product_daily["store"].unique()):
        store_products = product_daily[product_daily["store"] == store].copy()
        if store_products.empty:
            continue

        profit_top = (
            store_products[store_products["实时盈亏"] > 0]
            .sort_values("实时盈亏", ascending=False)
            .head(5)
            .rename(columns={"product_id": "商品ID"})
        )
        if not profit_top.empty:
            profit_top.insert(0, "类型", "盈利")
            profit_top.insert(0, "店铺", store)
            profit_top.insert(2, "排名", range(1, len(profit_top) + 1))
            rank_rows.append(profit_top)

        loss_top = (
            store_products[store_products["实时盈亏"] < 0]
            .sort_values("实时盈亏", ascending=True)
            .head(5)
            .rename(columns={"product_id": "商品ID"})
        )
        if not loss_top.empty:
            loss_top.insert(0, "类型", "亏损")
            loss_top.insert(0, "店铺", store)
            loss_top.insert(2, "排名", range(1, len(loss_top) + 1))
            rank_rows.append(loss_top)

    if not rank_rows:
        return pd.DataFrame(
            columns=["店铺", "类型", "排名", "商品ID", "销量", "订单量", "实时盈亏"]
        )
    return pd.concat(rank_rows, ignore_index=True)[
        ["店铺", "类型", "排名", "商品ID", "销量", "订单量", "实时盈亏"]
    ]


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

    _render_store_profit_chart(latest_overview)


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
    compact_rank = _build_product_rank_rows(product_daily)
    if compact_rank.empty:
        st.info("暂无商品盈利 / 亏损数据")
        return

    profit_col, loss_col = st.columns(2)
    with profit_col:
        _render_product_rank_chart(
            compact_rank[compact_rank["类型"] == "盈利"],
            "实时盈亏",
            "盈利产品 TOP5",
            "暂无盈利产品",
            "#16a34a",
        )
    with loss_col:
        _render_product_rank_chart(
            compact_rank[compact_rank["类型"] == "亏损"],
            "实时盈亏",
            "亏损产品 TOP5",
            "暂无亏损产品",
            "#dc2626",
        )


with st.sidebar:
    st.header("店铺与数据")
    page_mode = st.radio("页面", ["日报看板", "SKU成本维护"])
    sidebar_link("财务上传报表", upload_url())
    sidebar_link("投产计算器", roi_url())
    sidebar_link("达人管理", koc_url())
    sidebar_link("AI 生图", ai_image_url())

if page_mode == "SKU成本维护":
    render_sku_cost_manager()
    st.stop()

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
