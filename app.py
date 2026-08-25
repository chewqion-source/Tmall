from __future__ import annotations

import base64
from datetime import datetime
from html import escape
from io import BytesIO
import json
import mimetypes
import os
from pathlib import Path
import uuid

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


st.set_page_config(page_title="店铺数据", page_icon="📊", layout="wide")

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
OLD_ZY_STORE_NAME = "坐拥" + "宁静"
SHOP_NAME_ALIASES = {
    OLD_ZY_STORE_NAME: "坐拥_宁静",
}
DEFAULT_STORE_OPTIONS = ["易丽洁", "咖时光", "坐拥_宁静", "国货严选"]
DATA_DIR = Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))
SKU_COST_PATH = Path(os.environ.get("SKU_COST_FILE", DATA_DIR / "sku_cost.xlsx"))
REALTIME_SNAPSHOT_PATH = Path(
    os.environ.get("TMALL_REALTIME_FILE", DATA_DIR / "realtime" / "latest.json")
)
TASK_DIR = DATA_DIR / "tasks"
REALTIME_TASK_PATH = Path(os.environ.get("TMALL_REALTIME_TASK_FILE", TASK_DIR / "realtime_task.json"))
REALTIME_STATUS_PATH = Path(os.environ.get("TMALL_REALTIME_STATUS_FILE", TASK_DIR / "realtime_status.json"))


def inject_dashboard_styles() -> None:
    st.markdown(
        """
<style>
.st-key-refresh_data_icon {
    display: flex;
    justify-content: flex-end;
}
.st-key-refresh_data_icon button {
    width: 44px;
    height: 44px;
    min-height: 44px;
    padding: 0;
    border-radius: 8px;
    font-size: 22px;
    line-height: 1;
}
.st-key-profit_advice_fab {
    position: fixed;
    right: 24px;
    bottom: 24px;
    z-index: 9998;
}
.st-key-profit_advice_fab button {
    width: 58px;
    height: 58px;
    min-height: 58px;
    padding: 0;
    border-radius: 999px;
    font-size: 24px;
    box-shadow: 0 14px 34px rgba(15, 23, 42, .24);
}
.st-key-profit_advice_panel {
    position: fixed;
    right: 24px;
    bottom: 92px;
    width: min(390px, calc(100vw - 32px));
    max-height: min(560px, calc(100vh - 132px));
    overflow: auto;
    z-index: 9997;
    background: #ffffff;
    border: 1px solid #dbe3ef;
    border-radius: 12px;
    box-shadow: 0 22px 54px rgba(15, 23, 42, .22);
    padding: 16px 16px 12px;
}
.st-key-profit_advice_panel h3 {
    font-size: 18px;
    margin: 0 0 8px;
}
.st-key-rank_store_all button,
.st-key-rank_store_0 button,
.st-key-rank_store_1 button,
.st-key-rank_store_2 button,
.st-key-rank_store_3 button,
.st-key-rank_store_4 button {
    justify-content: flex-start;
}
.st-key-store_select_0 button,
.st-key-store_select_1 button,
.st-key-store_select_2 button,
.st-key-store_select_3 button,
.st-key-store_select_4 button {
    min-height: 38px;
}
.metric-card {
    min-height: 94px;
    border: 1px solid #dbe3ef;
    border-radius: 10px;
    padding: 14px 16px;
    background: #f3f4f6;
    box-shadow: 0 8px 22px rgba(15, 23, 42, .06);
}
.metric-card.store {
    background: linear-gradient(180deg, #eff6ff 0%, #ffffff 100%);
    border-color: #bfdbfe;
}
.metric-card.good {
    background: linear-gradient(180deg, #ecfdf5 0%, #ffffff 100%);
    border-color: #bbf7d0;
}
.metric-card.bad {
    background: linear-gradient(180deg, #fef2f2 0%, #ffffff 100%);
    border-color: #fecaca;
}
.metric-card.neutral {
    background: linear-gradient(180deg, #f8fafc 0%, #ffffff 100%);
}
.metric-label {
    color: #334155;
    font-size: 13px;
    line-height: 1.25;
    margin-bottom: 10px;
    white-space: nowrap;
}
.metric-value {
    color: #0f172a;
    font-size: 23px;
    line-height: 1.15;
    font-weight: 760;
    letter-spacing: 0;
    overflow-wrap: anywhere;
}
.metric-card.store .metric-value {
    font-size: 20px;
}
.metric-delta {
    display: inline-flex;
    align-items: center;
    margin-top: 10px;
    padding: 3px 8px;
    border-radius: 999px;
    background: transparent;
    color: #475569;
    font-size: 12px;
    font-weight: 650;
}
.metric-delta.good {
    background: #dcfce7;
    color: #15803d;
}
.metric-delta.bad {
    background: #fee2e2;
    color: #b91c1c;
}
.metric-section-gap {
    height: 10px;
}
.section-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin: 24px 0 10px;
}
.section-title-row h2 {
    margin: 0;
    font-size: 30px;
    line-height: 1.15;
}
.section-title-row .section-subtitle {
    color: #64748b;
    font-size: 13px;
    margin-top: 6px;
}
.inline-action button {
    min-height: 38px;
    border-radius: 8px;
}
.rank-panel {
    background: #f3f4f6;
    border-radius: 8px;
    padding: 18px 16px;
    min-height: 230px;
}
.rank-title {
    font-weight: 760;
    font-size: 16px;
    margin-bottom: 14px;
}
.rank-row {
    display: grid;
    grid-template-columns: minmax(130px, 210px) 1fr 76px;
    align-items: center;
    gap: 10px;
    margin: 8px 0;
    font-size: 12px;
}
.rank-name {
    color: #334155;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.rank-track {
    height: 16px;
    background: #e5e7eb;
    border-radius: 4px;
    overflow: hidden;
}
.rank-fill {
    height: 100%;
    min-width: 2px;
}
.rank-value {
    text-align: right;
    color: #0f172a;
    font-variant-numeric: tabular-nums;
}
.svg-chart-card {
    background: #f3f4f6;
    border-radius: 8px;
    padding: 18px 16px 12px;
    min-height: 300px;
}
.svg-chart-title {
    font-weight: 760;
    font-size: 16px;
    margin-bottom: 10px;
}
.svg-chart-card svg {
    width: 100%;
    height: 244px;
    display: block;
}
.svg-chart-label {
    fill: #64748b;
    font-size: 11px;
}
.svg-chart-legend {
    display: flex;
    justify-content: flex-end;
    gap: 14px;
    color: #475569;
    font-size: 12px;
    margin-bottom: 4px;
}
.legend-dot {
    display: inline-block;
    width: 9px;
    height: 9px;
    border-radius: 999px;
    margin-right: 5px;
}
</style>
""",
        unsafe_allow_html=True,
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


def metric_card(label: str, value: str, delta: str | None = None, tone: str = "neutral") -> None:
    delta_html = ""
    if delta:
        delta_tone = "good" if delta.strip().startswith("+") else "bad" if delta.strip().startswith("-") else ""
        delta_html = f'<div class="metric-delta {delta_tone}">{escape(delta)}</div>'
    st.markdown(
        f"""
<div class="metric-card {escape(tone)}">
    <div class="metric-label">{escape(label)}</div>
    <div class="metric-value">{escape(value)}</div>
    {delta_html}
</div>
""",
        unsafe_allow_html=True,
    )


def section_heading(title: str, subtitle: str | None = None, right_html: str = "") -> None:
    subtitle_html = f'<div class="section-subtitle">{escape(subtitle)}</div>' if subtitle else ""
    st.markdown(
        f"""
<div class="section-title-row">
    <div>
        <h2>{escape(title)}</h2>
        {subtitle_html}
    </div>
    <div>{right_html}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def profit_tone(value: float) -> str:
    if value > 0:
        return "good"
    if value < 0:
        return "bad"
    return "neutral"


def render_store_button_filter(stores: list[str], key_prefix: str = "store_select") -> str:
    if not stores:
        return ""
    current_store = st.session_state.get("selected_store", stores[0])
    if current_store not in stores:
        current_store = stores[0]
        st.session_state["selected_store"] = current_store

    columns = st.columns(len(stores))
    for index, store in enumerate(stores):
        with columns[index]:
            if st.button(
                store,
                key=f"{key_prefix}_{index}",
                type="primary" if store == current_store else "secondary",
                width="stretch",
            ):
                st.session_state["selected_store"] = store
                st.rerun()
    return current_store


def _range_days(range_label: str) -> int:
    return {
        "今日": 1,
        "昨日": 1,
        "3天": 3,
        "7天": 7,
        "15天": 15,
        "近一个月": 30,
        "近半年": 183,
    }.get(range_label, 30)


def _period_window(data: pd.DataFrame, range_label: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    if data.empty:
        today = pd.Timestamp.today().normalize()
        return today, today
    latest_date = data["date"].max()
    days = _range_days(range_label)
    if range_label == "昨日":
        end_date = latest_date - pd.Timedelta(days=1)
        start_date = end_date
    else:
        end_date = latest_date
        start_date = latest_date - pd.Timedelta(days=days - 1)
    return start_date, end_date


def _metric_delta(current: float, previous: float) -> str:
    if previous == 0:
        if current == 0:
            return "0.0%"
        return "+100.0%"
    return f"{(current - previous) / abs(previous) * 100:+.1f}%"


def _aggregate_period(data: pd.DataFrame, start_date: pd.Timestamp, end_date: pd.Timestamp) -> dict[str, float]:
    if data.empty:
        return {"sales_qty": 0.0, "order_count": 0.0, "profit": 0.0, "products": 0.0}
    rows = data[(data["date"] >= start_date) & (data["date"] <= end_date)]
    return {
        "sales_qty": float(rows["sales_qty"].sum()),
        "order_count": float(rows["order_count"].sum()),
        "profit": float(rows["profit"].sum()),
        "products": float(rows["product_id"].nunique()),
    }


def _period_metrics(data: pd.DataFrame, range_label: str) -> tuple[dict[str, float], dict[str, str]]:
    start_date, end_date = _period_window(data, range_label)
    days = max((end_date - start_date).days + 1, 1)
    current = _aggregate_period(data, start_date, end_date)
    prev_end = start_date - pd.Timedelta(days=1)
    prev_start = prev_end - pd.Timedelta(days=days - 1)
    previous = _aggregate_period(data, prev_start, prev_end)
    deltas = {
        key: _metric_delta(current[key], previous[key])
        for key in ["sales_qty", "order_count", "profit", "products"]
    }
    return current, deltas


def render_overview_metrics(
    selected_store: str,
    selected_product: str,
    store_daily: pd.DataFrame,
    selected_summary: pd.Series,
    latest: pd.Series,
    realtime_daily: pd.DataFrame,
) -> None:
    source = realtime_daily if not realtime_daily.empty else store_daily
    store_rows = source[source["store"] == selected_store].copy()
    if not store_rows.empty:
        latest_date = store_rows["date"].max()
        latest_rows = store_rows[store_rows["date"] == latest_date]
        store_sales = float(latest_rows["sales_qty"].sum())
        store_orders = float(latest_rows["order_count"].sum())
        store_profit = float(latest_rows["profit"].sum())
        product_count = int(latest_rows["product_id"].nunique())
    else:
        latest_date = store_daily["date"].max()
        latest_rows = store_daily[store_daily["date"] == latest_date]
        store_sales = float(latest_rows["sales_qty"].sum())
        store_orders = float(latest_rows["order_count"].sum())
        store_profit = float(latest_rows["profit"].sum())
        product_count = int(latest_rows["product_id"].nunique())

    st.subheader("经营概览")
    metric_cols = st.columns(6)
    with metric_cols[0]:
        metric_card("当前店铺", selected_store, tone="store")
    with metric_cols[1]:
        metric_card("最新销量", f"{store_sales:,.0f}")
    with metric_cols[2]:
        metric_card("最新订单", f"{store_orders:,.0f}")
    with metric_cols[3]:
        metric_card("最新盈亏", f"¥{store_profit:,.2f}", signed(store_profit), profit_tone(store_profit))
    with metric_cols[4]:
        metric_card("商品数", f"{product_count:,.0f}")
    with metric_cols[5]:
        metric_card("数据日期", f"{latest_date:%m-%d}")

    st.markdown('<div class="metric-section-gap"></div>', unsafe_allow_html=True)
    product_cols = st.columns(4)
    total_profit = float(selected_summary["total_profit"])
    latest_profit = float(latest["profit"])
    with product_cols[0]:
        metric_card("单品累计销量", f"{selected_summary['total_sales']:,.0f}")
    with product_cols[1]:
        metric_card("单品累计盈亏", f"¥{total_profit:,.2f}", signed(total_profit), profit_tone(total_profit))
    with product_cols[2]:
        sales_delta = signed(latest["sales_change"], 0) if pd.notna(latest["sales_change"]) else None
        metric_card(f"{latest['sheet']} 单品销量", f"{latest['sales_qty']:,.0f}", sales_delta)
    with product_cols[3]:
        profit_delta = signed(latest["profit_change"]) if pd.notna(latest["profit_change"]) else None
        metric_card(f"{latest['sheet']} 单品盈亏", f"¥{latest_profit:,.2f}", profit_delta, profit_tone(latest_profit))


def _empty_sku_cost_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=SKU_COST_HEADERS)


def normalize_store_name(value: object) -> str:
    store = str(value or "").strip()
    return SHOP_NAME_ALIASES.get(store, store)


def normalize_store_column(data: pd.DataFrame, column: str = "店铺") -> pd.DataFrame:
    if column not in data.columns:
        return data
    normalized = data.copy()
    normalized[column] = normalized[column].map(normalize_store_name)
    return normalized


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
    return normalize_store_column(data)


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
    cleaned = normalize_store_column(cleaned)
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


def read_json_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json_file(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def render_realtime_agent_panel() -> None:
    status = read_json_file(REALTIME_STATUS_PATH)
    task = read_json_file(REALTIME_TASK_PATH)
    status_text = str(status.get("status") or "unknown")
    status_labels = {
        "idle": "空闲",
        "checking_login": "检查登录",
        "running": "运行中",
        "success": "完成",
        "failed": "失败",
        "paused": "暂停",
        "skipped": "跳过",
        "error": "异常",
        "stopped": "已停止",
        "unknown": "未连接",
    }
    status_label = status_labels.get(status_text, status_text)
    pending_task = task.get("action") == "run_realtime" and task.get("status") in {"pending", "paused", "running"}
    is_busy = status_text in {"checking_login", "running"} or task.get("status") == "running"

    panel_cols = st.columns([1.2, 1.2, 2.4, 1.2], vertical_alignment="center")
    panel_cols[0].metric("本地守护进程", status_label)
    panel_cols[1].metric("最近更新", str(status.get("updated_at") or "—"))
    panel_cols[2].caption(str(status.get("message") or status.get("step") or "等待本地电脑接收任务"))
    if pending_task and task.get("status") != "success":
        panel_cols[2].caption(f"当前任务：{task.get('id')} / {task.get('status')}")

    with panel_cols[3]:
        if st.button("立即抓取实时数据", type="primary", width="stretch", disabled=is_busy):
            task_payload = {
                "id": uuid.uuid4().hex[:12],
                "action": "run_realtime",
                "status": "pending",
                "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "requested_by": "dashboard",
            }
            write_json_file(REALTIME_TASK_PATH, task_payload)
            st.success("已派发抓取任务，本地守护进程会在约 30 秒内接收。")
            st.rerun()


def load_realtime_snapshot(path: Path = REALTIME_SNAPSHOT_PATH) -> tuple[pd.DataFrame, str | None]:
    if not path.exists():
        return pd.DataFrame(), None
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    if not records:
        return pd.DataFrame(), payload.get("generated_at")
    data = pd.DataFrame(records)
    required = {"store", "date", "product_id", "sales_qty", "order_count", "profit"}
    if not required.issubset(data.columns):
        return pd.DataFrame(), payload.get("generated_at")
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    numeric_columns = [
        "sales_qty",
        "order_count",
        "profit",
        "pay_amount",
        "ad_cost",
        "refund_amount",
        "sku_count",
    ]
    for column in numeric_columns:
        if column not in data.columns:
            data[column] = 0
        data[column] = pd.to_numeric(data[column], errors="coerce").fillna(0)
    data["product_id"] = data["product_id"].astype(str)
    if "product_name" not in data.columns:
        data["product_name"] = ""
    if "sku_count" not in data.columns:
        data["sku_count"] = 0
    return data.dropna(subset=["date"]), payload.get("generated_at")


def build_profit_advice(product_id: str, realtime_daily: pd.DataFrame, all_daily: pd.DataFrame) -> tuple[str, bool]:
    query = str(product_id or "").strip()
    if not query:
        return "请输入商品 ID，我会按最新销量、订单量和盈亏给出调整建议。", False

    candidates = pd.DataFrame()
    source_name = "实时数据"
    if not realtime_daily.empty:
        candidates = realtime_daily[realtime_daily["product_id"].astype(str) == query].copy()

    if candidates.empty:
        source_name = "财务日报"
        candidates = all_daily[all_daily["product_id"].astype(str) == query].copy()
        if not candidates.empty:
            latest_date = candidates["date"].max()
            candidates = candidates[candidates["date"] == latest_date].copy()

    if candidates.empty:
        return f"没有找到商品 ID「{query}」。请确认 ID 是否完整，或先上传/刷新该商品的数据。", False

    latest_date = candidates["date"].max()
    view = candidates[candidates["date"] == latest_date].copy()
    total_sales = float(view["sales_qty"].sum())
    total_orders = float(view["order_count"].sum())
    total_profit = float(view["profit"].sum())
    stores = "、".join(sorted(view["store"].dropna().astype(str).unique()))
    product_name = ""
    if "product_name" in view.columns:
        names = [str(value).strip() for value in view["product_name"].dropna() if str(value).strip()]
        product_name = names[0] if names else ""

    per_order_gap = abs(total_profit) / total_orders if total_orders else abs(total_profit)
    per_sale_gap = abs(total_profit) / total_sales if total_sales else abs(total_profit)
    lines = [
        f"商品 ID：{query}",
        f"店铺：{stores or '未识别'}",
        f"日期：{latest_date:%Y-%m-%d}（{source_name}）",
    ]
    if product_name:
        lines.append(f"商品：{product_name}")
    lines.extend(
        [
            f"销量：{total_sales:,.0f}，订单量：{total_orders:,.0f}",
            f"盈亏：¥{total_profit:,.2f}",
            "",
        ]
    )

    if total_profit < 0:
        lines.extend(
            [
                "调整建议：",
                f"1. 先把单均亏损压回来：当前约每单亏 ¥{per_order_gap:,.2f}，或每件亏 ¥{per_sale_gap:,.2f}。",
                "2. 检查 SKU 成本、快递费和退款金额，优先处理高销量但亏损的规格。",
                "3. 推广先收紧低转化计划，保留能带来成交的关键词/人群。",
                "4. 如果价格有空间，建议按单均亏损上调售价或减少优惠，先把该品拉到不亏。",
            ]
        )
    elif total_profit > 0:
        lines.extend(
            [
                "调整建议：",
                "1. 这是盈利商品，可以保持库存和发货稳定，避免断货影响利润。",
                "2. 推广可小幅加预算，建议每次增加 10%-20%，观察盈亏是否同步提升。",
                "3. 复盘高利润 SKU，把主图、标题和活动资源优先给这类规格。",
                "4. 如果销量偏低，可以尝试轻微优惠换量，但要守住当前利润率。",
            ]
        )
    else:
        lines.extend(
            [
                "调整建议：",
                "1. 当前接近盈亏平衡，先不要大幅加推广。",
                "2. 优先核对成本、快递费和优惠金额，确认没有漏算。",
                "3. 可以做小幅价格或优惠测试，看销量是否能带动利润转正。",
            ]
        )

    return "\n".join(lines), True


def render_profit_advice_floating(realtime_daily: pd.DataFrame, all_daily: pd.DataFrame) -> None:
    if "profit_advice_open" not in st.session_state:
        st.session_state["profit_advice_open"] = False

    if st.button("询", key="profit_advice_fab", help="单品盈亏咨询"):
        st.session_state["profit_advice_open"] = not st.session_state["profit_advice_open"]

    if not st.session_state["profit_advice_open"]:
        return

    try:
        panel = st.container(key="profit_advice_panel")
    except TypeError:
        panel = st.container()

    with panel:
        st.markdown("### 单品盈亏咨询")
        product_id = st.text_input(
            "输入商品 ID",
            key="profit_advice_product_id",
            placeholder="例如 653372334339",
        )
        advice, _found = build_profit_advice(product_id, realtime_daily, all_daily)
        with st.chat_message("assistant"):
            st.markdown(advice.replace("\n", "  \n"))


def render_changes_table(selected: pd.DataFrame) -> None:
    st.subheader("商品订单数 / 盈亏日环比变化")
    changes = selected[
        [
            "sheet",
            "order_count",
            "orders_change",
            "orders_change_pct",
            "sales_qty",
            "sales_change",
            "sales_change_pct",
            "profit",
            "profit_change",
            "profit_change_pct",
        ]
    ].rename(
        columns={
            "sheet": "日期",
            "order_count": "订单数",
            "orders_change": "订单数日增减",
            "orders_change_pct": "订单数日环比",
            "sales_qty": "件数",
            "sales_change": "件数日增减",
            "sales_change_pct": "件数日环比",
            "profit": "盈亏",
            "profit_change": "盈亏日增减",
            "profit_change_pct": "盈亏变化率",
        }
    )
    styled_changes = changes.style.map(color_profit, subset=["盈亏", "盈亏日增减"]).format(
        {
            "订单数": "{:,.0f}",
            "订单数日增减": lambda value: "—" if pd.isna(value) else f"{value:+,.0f}",
            "订单数日环比": lambda value: "—" if pd.isna(value) else f"{value:+.1%}",
            "件数": "{:,.0f}",
            "件数日增减": lambda value: "—" if pd.isna(value) else f"{value:+,.0f}",
            "件数日环比": lambda value: "—" if pd.isna(value) else f"{value:+.1%}",
            "盈亏": "{:+,.2f}",
            "盈亏日增减": lambda value: "—" if pd.isna(value) else f"{value:+,.2f}",
            "盈亏变化率": lambda value: "—" if pd.isna(value) else f"{value:+.1%}",
        }
    )
    st.dataframe(styled_changes, width="stretch", hide_index=True)


def render_product_summary_table(summary: pd.DataFrame, selected_store: str) -> None:
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


def render_store_overview_table(all_daily: pd.DataFrame) -> None:
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
    stores = sorted(
        {
            *DEFAULT_STORE_OPTIONS,
            *[normalize_store_name(store) for store in data["店铺"].dropna().unique() if str(store).strip()],
        }
    )
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
    view = view.reset_index(drop=True)

    st.caption("可直接修改单件货价、快递费，也可以在最后新增行。保存后会写回线上 sku_cost.xlsx。")

    batch_cols = st.columns([1, 1, 1, 1.2])
    with batch_cols[0]:
        batch_column = st.selectbox("\u6279\u91cf\u5b57\u6bb5", ["\u5feb\u9012\u8d39", "\u5355\u4ef6\u8d27\u4ef7"])
    with batch_cols[1]:
        batch_value = st.number_input("\u6279\u91cf\u91d1\u989d", min_value=0.0, value=3.7, step=0.1, format="%.2f")
    with batch_cols[2]:
        batch_scope = st.selectbox("\u586b\u5199\u8303\u56f4", ["\u53ea\u586b\u7a7a\u503c", "\u8986\u76d6\u5f53\u524d\u7b5b\u9009"])
    with batch_cols[3]:
        st.write("")
        st.write("")
        if st.button("\u6279\u91cf\u586b\u5199\u5f53\u524d\u7b5b\u9009", type="secondary", width="stretch"):
            target_ids = view["_row_id"].dropna().astype(int).tolist()
            if not target_ids:
                st.warning("\u5f53\u524d\u7b5b\u9009\u6ca1\u6709\u53ef\u586b\u5199\u7684 SKU\u3002")
            else:
                save_data = data.copy()
                target_mask = save_data.index.isin(target_ids)
                if batch_scope == "\u53ea\u586b\u7a7a\u503c":
                    target_mask = target_mask & save_data[batch_column].isna()
                changed = int(target_mask.sum())
                if changed == 0:
                    st.info("\u5f53\u524d\u7b5b\u9009\u91cc\u6ca1\u6709\u9700\u8981\u586b\u5199\u7684\u7a7a\u503c\u3002")
                else:
                    save_data.loc[target_mask, batch_column] = round(float(batch_value), 2)
                    backup_path = save_sku_cost_frame(save_data)
                    st.success(
                        f"\u5df2\u6279\u91cf\u586b\u5199 {changed} \u884c {batch_column}\u3002"
                        + (f" \u65e7\u6587\u4ef6\u5907\u4efd\uff1a{backup_path.name}" if backup_path else "")
                    )
                    st.rerun()

    edited = st.data_editor(
        view,
        hide_index=True,
        num_rows="dynamic",
        width="stretch",
        height=560,
        column_config={
            "店铺": st.column_config.SelectboxColumn("店铺", options=stores),
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
CHART_CARD_MARGIN = dict(l=18, r=18, t=46, b=24)


def style_chart_card(fig: go.Figure, title: str, height: int) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color="#0f172a"), x=0.02, xanchor="left"),
        height=height,
        paper_bgcolor="#f8fbff",
        plot_bgcolor="#ffffff",
        margin=CHART_CARD_MARGIN,
        font=dict(family="Arial, Microsoft YaHei, sans-serif", size=11, color="#475569"),
        hoverlabel=dict(bgcolor="#ffffff", bordercolor="#dbe3ef", font=dict(color="#0f172a")),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.05,
            xanchor="right",
            x=1,
            font=dict(size=11, color="#475569"),
        ),
        shapes=[
            dict(
                type="rect",
                xref="paper",
                yref="paper",
                x0=0,
                y0=0,
                x1=1,
                y1=1,
                line=dict(color="#dbe3ef", width=1),
                fillcolor="rgba(0,0,0,0)",
                layer="below",
            )
        ],
    )
    fig.update_xaxes(showgrid=True, gridcolor="#edf2f7", zeroline=False, linecolor="#dbe3ef")
    fig.update_yaxes(showgrid=True, gridcolor="#edf2f7", zeroline=False, linecolor="#dbe3ef")
    return fig


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


def _line_points(values: list[float], width: int, height: int, pad: int) -> str:
    if not values:
        return ""
    low = min(values)
    high = max(values)
    span = high - low if high != low else max(abs(high), 1.0)
    step = (width - pad * 2) / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = pad + index * step
        y = pad + (high - value) / span * (height - pad * 2)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _chart_ticks(data: pd.DataFrame) -> list[tuple[float, str]]:
    if data.empty:
        return []
    dates = data["date"].tolist()
    indexes = sorted({0, len(dates) // 2, len(dates) - 1})
    width = 640
    pad = 44
    step = (width - pad * 2) / max(len(dates) - 1, 1)
    return [(pad + index * step, pd.Timestamp(dates[index]).strftime("%m-%d")) for index in indexes]


def render_sales_orders_trend(data: pd.DataFrame, title: str, height: int = 360) -> None:
    del height
    if data.empty:
        st.markdown(
            f'<div class="svg-chart-card"><div class="svg-chart-title">{escape(title)}</div>暂无数据</div>',
            unsafe_allow_html=True,
        )
        return
    data = data.sort_values("date").tail(60)
    width, chart_height, pad = 640, 244, 44
    sales_values = [float(value) for value in data["sales_qty"]]
    order_values = [float(value) for value in data["order_count"]]
    sales_points = _line_points(sales_values, width, chart_height, pad)
    order_points = _line_points(order_values, width, chart_height, pad)
    tick_html = "".join(
        f'<text x="{x:.1f}" y="232" text-anchor="middle" class="svg-chart-label">{escape(label)}</text>'
        for x, label in _chart_ticks(data)
    )
    max_sales = max(sales_values) if sales_values else 0
    max_orders = max(order_values) if order_values else 0
    st.markdown(
        f"""
<div class="svg-chart-card">
  <div class="svg-chart-title">{escape(title)}</div>
  <div class="svg-chart-legend">
    <span><i class="legend-dot" style="background:#2563eb"></i>件数</span>
    <span><i class="legend-dot" style="background:#f59e0b"></i>订单数</span>
  </div>
  <svg viewBox="0 0 {width} {chart_height}" preserveAspectRatio="none">
    <line x1="{pad}" y1="202" x2="610" y2="202" stroke="#cbd5e1" stroke-width="1" />
    <line x1="{pad}" y1="36" x2="610" y2="36" stroke="#e2e8f0" stroke-width="1" />
    <line x1="{pad}" y1="119" x2="610" y2="119" stroke="#e2e8f0" stroke-width="1" />
    <polyline fill="none" stroke="#2563eb" stroke-width="3" points="{sales_points}" />
    <polyline fill="none" stroke="#f59e0b" stroke-width="3" stroke-dasharray="6 5" points="{order_points}" />
    <text x="{pad}" y="28" class="svg-chart-label">件数最高 {max_sales:,.0f}</text>
    <text x="610" y="28" text-anchor="end" class="svg-chart-label">订单最高 {max_orders:,.0f}</text>
    {tick_html}
  </svg>
</div>
""",
        unsafe_allow_html=True,
    )


def render_profit_trend(data: pd.DataFrame, title: str, height: int = 360) -> None:
    del height
    if data.empty:
        st.markdown(
            f'<div class="svg-chart-card"><div class="svg-chart-title">{escape(title)}</div>暂无数据</div>',
            unsafe_allow_html=True,
        )
        return
    data = data.sort_values("date").tail(60)
    values = [float(value) for value in data["profit"]]
    width, chart_height, pad = 640, 244, 44
    max_abs = max(max(abs(value) for value in values), 1.0)
    baseline = 119
    step = (width - pad * 2) / max(len(values), 1)
    bar_width = max(min(step * 0.68, 18), 4)
    bars = []
    for index, value in enumerate(values):
        x = pad + index * step + (step - bar_width) / 2
        bar_height = abs(value) / max_abs * 78
        y = baseline - bar_height if value >= 0 else baseline
        color = "#16a34a" if value >= 0 else "#dc2626"
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}" rx="2" />')
    tick_html = "".join(
        f'<text x="{x:.1f}" y="232" text-anchor="middle" class="svg-chart-label">{escape(label)}</text>'
        for x, label in _chart_ticks(data)
    )
    st.markdown(
        f"""
<div class="svg-chart-card">
  <div class="svg-chart-title">{escape(title)}</div>
  <svg viewBox="0 0 {width} {chart_height}" preserveAspectRatio="none">
    <line x1="{pad}" y1="{baseline}" x2="610" y2="{baseline}" stroke="#64748b" stroke-width="1" />
    <line x1="{pad}" y1="36" x2="610" y2="36" stroke="#e2e8f0" stroke-width="1" />
    <line x1="{pad}" y1="202" x2="610" y2="202" stroke="#e2e8f0" stroke-width="1" />
    {''.join(bars)}
    <text x="{pad}" y="28" class="svg-chart-label">最大波动 {_format_money(max_abs)}</text>
    {tick_html}
  </svg>
</div>
""",
        unsafe_allow_html=True,
    )


def _product_axis_label(product_id: object) -> str:
    text = str(product_id)
    if len(text) <= 12:
        return text
    return f"{text[:6]}...{text[-4:]}"


def _format_money(value: float) -> str:
    return f"¥{value:,.2f}"


def _render_rank_bar_list(data: pd.DataFrame, title: str, color: str, empty_text: str) -> None:
    if data.empty:
        st.markdown(
            f"""
<div class="rank-panel">
    <div class="rank-title">{escape(title)}</div>
    <div class="rank-name">{escape(empty_text)}</div>
</div>
""",
            unsafe_allow_html=True,
        )
        return

    max_value = max(float(data["实时盈亏"].abs().max()), 1.0)
    rows_html = []
    for _, row in data.iterrows():
        value = float(row["实时盈亏"])
        width = max(abs(value) / max_value * 100, 1.5)
        label = f"{row['店铺']} #{int(row['排名'])} {row['商品ID']}"
        rows_html.append(
            f"""
<div class="rank-row">
    <div class="rank-name" title="{escape(label)}">{escape(label)}</div>
    <div class="rank-track"><div class="rank-fill" style="width:{width:.1f}%; background:{color};"></div></div>
    <div class="rank-value">{escape(_format_money(value))}</div>
</div>
"""
        )
    st.markdown(
        f"""
<div class="rank-panel">
    <div class="rank-title">{escape(title)}</div>
    {''.join(rows_html)}
</div>
""",
        unsafe_allow_html=True,
    )


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
    style_chart_card(fig, title, min(450, 112 + 30 * len(chart_data)))
    fig.update_layout(xaxis_title="", yaxis_title="", showlegend=False)
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
    style_chart_card(fig, "店铺实时盈亏", 240)
    fig.update_layout(xaxis_title="", yaxis_title="", showlegend=False)
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

    st.subheader("店铺商品盈亏排行")
    st.caption(f"数据日期：{latest_date:%Y-%m-%d}")
    compact_rank = _build_product_rank_rows(product_daily)
    if compact_rank.empty:
        st.info("暂无商品盈利 / 亏损数据")
        return

    stores = ["全部"] + sorted(compact_rank["店铺"].dropna().astype(str).unique())
    current_store = st.session_state.get("rank_store_filter", "全部")
    if current_store not in stores:
        current_store = "全部"
        st.session_state["rank_store_filter"] = current_store

    filter_col, chart_col = st.columns([0.85, 4.15], vertical_alignment="top")
    with filter_col:
        st.markdown("**筛选店铺**")
        for index, store in enumerate(stores):
            key = "rank_store_all" if store == "全部" else f"rank_store_{index}"
            if st.button(
                store,
                key=key,
                type="primary" if store == current_store else "secondary",
                width="stretch",
            ):
                st.session_state["rank_store_filter"] = store
                st.rerun()

    filtered_rank = compact_rank if current_store == "全部" else compact_rank[compact_rank["店铺"] == current_store]
    with chart_col:
        profit_col, loss_col = st.columns(2)
        with profit_col:
            _render_product_rank_chart(
                filtered_rank[filtered_rank["类型"] == "盈利"],
                "实时盈亏",
                "盈利产品 TOP5",
                "暂无盈利产品",
                "#16a34a",
            )
        with loss_col:
            _render_product_rank_chart(
                filtered_rank[filtered_rank["类型"] == "亏损"],
                "实时盈亏",
                "亏损产品 TOP5",
                "暂无亏损产品",
                "#dc2626",
            )


def render_realtime_data_section(realtime_daily: pd.DataFrame, all_daily: pd.DataFrame, generated_at: str | None) -> None:
    st.markdown("## 实时数据")
    st.caption(f"更新时间 {generated_at}" if generated_at else "暂无实时快照")
    source = realtime_daily if not realtime_daily.empty else all_daily
    latest_date = source["date"].max()
    latest_rows = source[source["date"] == latest_date].copy()

    pay_total = float(latest_rows["pay_amount"].sum()) if "pay_amount" in latest_rows.columns else 0.0
    refund_total = float(latest_rows["refund_amount"].sum()) if "refund_amount" in latest_rows.columns else 0.0
    order_total = float(latest_rows["order_count"].sum())
    sales_total = float(latest_rows["sales_qty"].sum())
    profit_total = float(latest_rows["profit"].sum())
    refund_rate = refund_total / pay_total if pay_total else 0.0

    metric_cols = st.columns(6)
    cards = [
        ("支付金额", _format_money(pay_total), "小时环比 --", "neutral"),
        ("订单数", f"{order_total:,.0f}", "小时环比 --", "neutral"),
        ("件数", f"{sales_total:,.0f}", "小时环比 --", "neutral"),
        ("盈亏", _format_money(profit_total), "小时环比 --", profit_tone(profit_total)),
        ("退款金额", _format_money(refund_total), "小时环比 --", "neutral"),
        ("退款率", f"{refund_rate:.1%}", "小时环比 --", "neutral"),
    ]
    for column, (label, value, delta, tone) in zip(metric_cols, cards):
        with column:
            metric_card(label, value, delta, tone)

    product_daily = (
        latest_rows.groupby(["store", "product_id"], as_index=False)
        .agg(
            销量=("sales_qty", "sum"),
            订单量=("order_count", "sum"),
            实时盈亏=("profit", "sum"),
        )
    )
    profit_rank = (
        product_daily[product_daily["实时盈亏"] > 0]
        .sort_values("实时盈亏", ascending=False)
        .head(5)
        .rename(columns={"store": "店铺", "product_id": "商品ID"})
        .copy()
    )
    profit_rank.insert(2, "排名", range(1, len(profit_rank) + 1))
    loss_rank = (
        product_daily[product_daily["实时盈亏"] < 0]
        .sort_values("实时盈亏", ascending=True)
        .head(5)
        .rename(columns={"store": "店铺", "product_id": "商品ID"})
        .copy()
    )
    loss_rank.insert(2, "排名", range(1, len(loss_rank) + 1))
    profit_col, loss_col = st.columns(2)
    with profit_col:
        _render_rank_bar_list(
            profit_rank,
            "盈利产品 TOP5",
            "#16a34a",
            "暂无盈利产品",
        )
    with loss_col:
        _render_rank_bar_list(
            loss_rank,
            "亏损产品 TOP5",
            "#dc2626",
            "暂无亏损产品",
        )


def render_store_overview_section(
    selected_store: str,
    store_daily: pd.DataFrame,
    trend_range: str,
    trend_store: pd.DataFrame,
) -> None:
    current, deltas = _period_metrics(store_daily, trend_range)
    metric_cols = st.columns(6)
    cards = [
        ("订单数", f"{current['order_count']:,.0f}", deltas["order_count"], "neutral"),
        ("件数", f"{current['sales_qty']:,.0f}", deltas["sales_qty"], "neutral"),
        ("盈亏", _format_money(current["profit"]), deltas["profit"], profit_tone(current["profit"])),
        ("商品数", f"{current['products']:,.0f}", deltas["products"], "neutral"),
        ("日均订单数", f"{current['order_count'] / max(_range_days(trend_range), 1):,.1f}", deltas["order_count"], "neutral"),
        ("日均盈亏", _format_money(current["profit"] / max(_range_days(trend_range), 1)), deltas["profit"], profit_tone(current["profit"])),
    ]
    for column, (label, value, delta, tone) in zip(metric_cols, cards):
        with column:
            metric_card(label, value, delta, tone)

    chart_cols = st.columns(2)
    with chart_cols[0]:
        render_sales_orders_trend(trend_store, "订单数与件数折线图", height=300)
    with chart_cols[1]:
        render_profit_trend(trend_store, "盈亏柱状趋势图", height=300)


def render_product_overview_section(
    selected: pd.DataFrame,
    selected_summary: pd.Series,
    latest: pd.Series,
    trend_range: str,
    trend_selected: pd.DataFrame,
) -> None:
    current, deltas = _period_metrics(selected, trend_range)
    total_profit = float(selected_summary["total_profit"])
    latest_profit = float(latest["profit"])
    latest_sales_delta = signed(float(latest["sales_change"]), 0) if pd.notna(latest["sales_change"]) else None
    latest_profit_delta = signed(float(latest["profit_change"])) if pd.notna(latest["profit_change"]) else None

    metric_cols = st.columns(6)
    cards = [
        ("订单数", f"{current['order_count']:,.0f}", deltas["order_count"], "neutral"),
        ("件数", f"{current['sales_qty']:,.0f}", deltas["sales_qty"], "neutral"),
        ("盈亏", _format_money(current["profit"]), deltas["profit"], profit_tone(current["profit"])),
        ("累计盈亏", _format_money(total_profit), signed(total_profit), profit_tone(total_profit)),
        ("最新件数", f"{float(latest['sales_qty']):,.0f}", latest_sales_delta, "neutral"),
        ("最新盈亏", _format_money(latest_profit), latest_profit_delta, profit_tone(latest_profit)),
    ]
    for column, (label, value, delta, tone) in zip(metric_cols, cards):
        with column:
            metric_card(label, value, delta, tone)

    chart_cols = st.columns(2)
    with chart_cols[0]:
        render_sales_orders_trend(trend_selected, "单品订单数与件数折线图", height=300)
    with chart_cols[1]:
        render_profit_trend(trend_selected, "单品盈亏柱状趋势图", height=300)


with st.sidebar:
    st.header("店铺与数据")
    page_param = str(st.query_params.get("page", "dashboard"))
    page_mode = "SKU成本维护" if page_param == "sku-cost" else "日报看板"
    if st.button(
        "店铺数据",
        type="primary" if page_mode == "日报看板" else "secondary",
        width=168,
    ):
        st.query_params["page"] = "dashboard"
        st.rerun()
    if st.button(
        "SKU成本维护",
        type="primary" if page_mode == "SKU成本维护" else "secondary",
        width=168,
    ):
        st.query_params["page"] = "sku-cost"
        st.rerun()
    sidebar_link("财务上传报表", upload_url())
    sidebar_link("投产计算器", roi_url())
    sidebar_link("达人管理", koc_url())
    sidebar_link("AI 生图", ai_image_url())

inject_dashboard_styles()

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
    st.title("店铺数据")
with refresh_col:
    if st.button("↻", type="primary", key="refresh_data_icon", help="刷新数据"):
        st.cache_data.clear()
        st.rerun()

realtime_daily, realtime_generated_at = load_realtime_snapshot()
data_note = (
    f"实时模块更新时间：{realtime_generated_at}；趋势和明细以财务导入日报为准。"
    if realtime_generated_at and not realtime_daily.empty
    else "暂无实时抓取快照；当前以财务日报最新日期展示。"
)
st.caption(data_note)
render_realtime_agent_panel()

rank_source = realtime_daily if not realtime_daily.empty else all_daily
render_realtime_data_section(realtime_daily, all_daily, realtime_generated_at)

st.markdown("## 店铺概览")
store_filter_cols = st.columns([1.2, 1.2, 2.6])
with store_filter_cols[0]:
    selected_store = st.selectbox("店铺名称", list(sources), index=0)
with store_filter_cols[1]:
    trend_range = st.selectbox("选择时间", TREND_RANGE_OPTIONS, index=5, key="store_trend_range")
store_daily = all_daily[all_daily["store"] == selected_store].copy()
complete = complete_daily_series(store_daily)
summary = build_summary(store_daily, complete)
products = summary["product_id"].tolist()

store_trend = (
    store_daily.groupby(["date", "sheet"], as_index=False)
    .agg(sales_qty=("sales_qty", "sum"), order_count=("order_count", "sum"), profit=("profit", "sum"))
    .sort_values("date", ignore_index=True)
)
trend_store = filter_trend_range(store_trend, trend_range)
render_store_overview_section(selected_store, store_daily, trend_range, trend_store)

product_heading_cols = st.columns([4, 1], vertical_alignment="center")
with product_heading_cols[0]:
    st.markdown("## 商品概览")
with product_heading_cols[1]:
    if st.button("SKU成本维护", type="secondary", width="stretch"):
        st.query_params["page"] = "sku-cost"
        st.rerun()

product_filter_cols = st.columns([1.8, 1.2, 2])
with product_filter_cols[0]:
    selected_product = st.selectbox("商品 ID", products, index=0)
with product_filter_cols[1]:
    product_trend_range = st.selectbox("选择时间", TREND_RANGE_OPTIONS, index=5, key="product_trend_range")

if selected_product == LEGACY_SUMMARY_PRODUCT_ID:
    st.warning(
        "该时段使用早期日报模板，没有商品 ID，也没有按商品拆分的订单量和盈亏。"
        "这里展示的是当日全店销量、快递单量和结余，未错误归入某个单品。"
    )

selected = complete[complete["product_id"] == selected_product].copy().sort_values("date")
trend_selected = filter_trend_range(selected, product_trend_range)
selected_summary = summary[summary["product_id"] == selected_product].iloc[0]
latest = selected.iloc[-1]
render_product_overview_section(selected, selected_summary, latest, product_trend_range, trend_selected)

render_changes_table(selected)
render_product_summary_table(summary, selected_store)

render_profit_advice_floating(realtime_daily, all_daily)
