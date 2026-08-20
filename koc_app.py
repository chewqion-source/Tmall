from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


st.set_page_config(page_title="达人管理", page_icon="🤝", layout="wide")

st.markdown(
    """
    <style>
    .koc-chip-row {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin: 2px 0 12px;
    }
    .koc-chip {
        display: inline-flex;
        align-items: center;
        min-height: 28px;
        padding: 4px 10px;
        border: 1px solid currentColor;
        border-radius: 999px;
        background: #fff;
        font-size: 13px;
        font-weight: 650;
        line-height: 1;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

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

TEXT_COLUMNS = [
    "联系状态",
    "达人名",
    "ID",
    "账号主页",
    "微信号",
    "邮箱",
    "渠道",
    "推广方式",
    "发布链接",
    "素材",
    "备注",
]

STATUS_CONFIG = {
    "未标记": "#64748b",
    "待联系": "#2563eb",
    "已联系": "#7c3aed",
    "合作中": "#16a34a",
    "已完结": "#f97316",
    "已拒绝": "#dc2626",
}
STATUS_ALIASES = {"已合作": "合作中", "已同合作": "合作中"}

CHANNEL_CONFIG = {
    "小红书": "#dc2626",
    "抖音": "#111827",
    "快手": "#f97316",
    "得物": "#2563eb",
}

PROMOTION_METHODS = ["置换", "蒲公英", "非报备"]


def get_koc_path() -> Path:
    data_dir = Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))
    return Path(os.environ.get("TMALL_KOC_FILE", data_dir / "koc_management.xlsx"))


def empty_koc_data() -> pd.DataFrame:
    return pd.DataFrame(columns=REQUIRED_COLUMNS)


def normalize_homepage_url(value: object) -> str:
    text = "" if pd.isna(value) else str(value).strip()
    if not text:
        return ""
    if "://" in text or text.startswith(("mailto:", "tel:")):
        return text
    return f"https://{text}"


def calculate_settlement(price: pd.Series, rebate: pd.Series) -> pd.Series:
    rebate_percent = pd.to_numeric(rebate, errors="coerce").fillna(0)
    return (
        pd.to_numeric(price, errors="coerce").fillna(0)
        - pd.to_numeric(price, errors="coerce").fillna(0) * rebate_percent / 100
    )


def normalize_rebate_percent(value: pd.Series) -> pd.Series:
    rebate = pd.to_numeric(value, errors="coerce")
    legacy_decimal = rebate.notna() & rebate.gt(0) & rebate.le(1)
    rebate = rebate.mask(legacy_decimal, rebate * 100)
    return rebate


def normalize_koc_data(df: pd.DataFrame) -> pd.DataFrame:
    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA
    df = df[REQUIRED_COLUMNS].copy()
    for column in TEXT_COLUMNS:
        df[column] = df[column].fillna("").astype(str).str.strip()
    df["达人名"] = df["达人名"].fillna("").astype(str).str.strip()
    df["ID"] = df["ID"].fillna("").astype(str).str.strip()
    df["联系状态"] = df["联系状态"].replace(STATUS_ALIASES)
    df["联系状态"] = df["联系状态"].replace("", "未标记")
    df["渠道"] = df["渠道"].replace("", "未标记")
    df["推广方式"] = df["推广方式"].replace("", PROMOTION_METHODS[0])
    df["账号主页"] = df["账号主页"].map(normalize_homepage_url)
    df["报价"] = pd.to_numeric(df["报价"], errors="coerce")
    df["返点"] = normalize_rebate_percent(df["返点"])
    df["结算价"] = calculate_settlement(df["报价"], df["返点"])
    df["发布时间（最早）"] = pd.to_datetime(df["发布时间（最早）"], errors="coerce")
    return df


def save_koc_data(df: pd.DataFrame, target_path: Path) -> None:
    normalized = normalize_koc_data(df)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    incoming_dir = target_path.parent / ".incoming"
    incoming_dir.mkdir(exist_ok=True)
    archive_dir = target_path.parent / "archive" / "达人管理"
    archive_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=incoming_dir,
        prefix="koc-management-",
        suffix=".xlsx",
        delete=False,
    ) as temporary_file:
        staged_path = Path(temporary_file.name)

    try:
        normalized.to_excel(staged_path, index=False)
        if target_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive_name = f"{timestamp}-{uuid.uuid4().hex[:8]}-{target_path.name}"
            os.replace(target_path, archive_dir / archive_name)
        os.replace(staged_path, target_path)
        if os.name != "nt":
            target_path.chmod(0o640)
    finally:
        staged_path.unlink(missing_ok=True)


def add_koc_record(current: pd.DataFrame, record: dict[str, object], target_path: Path) -> int:
    name = str(record.get("达人名", "")).strip()
    if not name:
        raise ValueError("请填写达人名")
    next_df = pd.concat([current, pd.DataFrame([record])], ignore_index=True)
    save_koc_data(next_df, target_path)
    return len(next_df)


@st.cache_data(show_spinner="正在读取达人管理表...")
def load_koc_data(path_text: str, mtime_ns: int, size: int) -> pd.DataFrame:
    del mtime_ns, size
    return normalize_koc_data(pd.read_excel(path_text, sheet_name=0))


def option_values(series: pd.Series) -> list[str]:
    return sorted(value for value in series.dropna().astype(str).unique() if value)


def merge_options(config_values: list[str], existing_values: list[str]) -> list[str]:
    options = list(config_values)
    for value in existing_values:
        if value not in options:
            options.append(value)
    return options


def render_chip_legend(config: dict[str, str]) -> None:
    chips = "".join(
        f'<span class="koc-chip" style="border-color:{color}; color:{color};">{label}</span>'
        for label, color in config.items()
    )
    st.markdown(f'<div class="koc-chip-row">{chips}</div>', unsafe_allow_html=True)


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


def editor_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    snapshot = df[["_row_id", *REQUIRED_COLUMNS]].copy()
    for column in TEXT_COLUMNS:
        snapshot[column] = snapshot[column].fillna("").astype(str).str.strip()
    for column in ["报价", "返点", "结算价"]:
        snapshot[column] = pd.to_numeric(snapshot[column], errors="coerce")
    snapshot["发布时间（最早）"] = (
        pd.to_datetime(snapshot["发布时间（最早）"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    )
    return snapshot.reset_index(drop=True)


def apply_editor_changes(current: pd.DataFrame, edited: pd.DataFrame) -> pd.DataFrame:
    updated = current.copy()
    for _, edited_row in edited.iterrows():
        row_id = int(edited_row["_row_id"])
        for column in REQUIRED_COLUMNS:
            if column == "结算价":
                continue
            updated.at[row_id, column] = edited_row[column]
        updated.at[row_id, "结算价"] = calculate_settlement(
            pd.Series([updated.at[row_id, "报价"]]),
            pd.Series([updated.at[row_id, "返点"]]),
        ).iat[0]
    return updated


koc_path = get_koc_path()

with st.sidebar:
    st.header("达人管理")
    st.link_button(
        "返回主看板",
        os.environ.get("TMALL_DASHBOARD_URL", "http://150.158.133.102/"),
        width="content",
    )
    st.link_button(
        "投产计算器",
        os.environ.get("TMALL_ROI_URL", "http://150.158.133.102/roi/"),
        width="content",
    )
    if st.button("重新读取达人表", width="content"):
        st.cache_data.clear()

title_col, create_col = st.columns([5, 1], vertical_alignment="center")
with title_col:
    st.title("达人管理")
with create_col:
    if st.button("新增达人", type="primary", width="stretch"):
        st.session_state["show_create_koc"] = not st.session_state.get("show_create_koc", False)

if koc_path.exists():
    df = load_koc_data(str(koc_path), koc_path.stat().st_mtime_ns, koc_path.stat().st_size)
else:
    df = empty_koc_data()

status_options = merge_options(list(STATUS_CONFIG), option_values(df["联系状态"]))
channel_options = merge_options(list(CHANNEL_CONFIG), option_values(df["渠道"]))
method_options = merge_options(PROMOTION_METHODS, option_values(df["推广方式"]))

if st.session_state.get("show_create_koc") or not koc_path.exists():
    with st.form("create_koc"):
        st.subheader("新增达人")
        col1, col2, col3 = st.columns(3)
        with col1:
            new_status = st.selectbox("联系状态", status_options, index=status_options.index("待联系"))
            new_name = st.text_input("达人名")
            new_id = st.text_input("ID")
            new_channel = st.selectbox("渠道", channel_options, index=channel_options.index("小红书"))
            new_method = st.selectbox("推广方式", method_options)
        with col2:
            new_homepage = st.text_input("账号主页")
            new_wechat = st.text_input("微信号")
            new_email = st.text_input("邮箱")
            new_publish_date = st.date_input("发布时间（最早）", value=None, format="YYYY-MM-DD")
            new_post_link = st.text_input("发布链接")
        with col3:
            new_price = st.number_input("报价", min_value=0.0, step=1.0, value=None)
            new_rebate = st.number_input("返点（%）", min_value=0.0, max_value=100.0, step=1.0, value=None)
            preview_price = 0 if new_price is None else new_price
            preview_rebate = 0 if new_rebate is None else new_rebate
            st.metric("结算价", f"¥{preview_price - preview_price * preview_rebate / 100:,.2f}")
            new_material = st.text_input("素材")
            new_note = st.text_area("备注", height=92)

        submitted = st.form_submit_button("保存达人", type="primary", width="stretch")

    if submitted:
        publish_date = pd.to_datetime(new_publish_date, errors="coerce") if new_publish_date else pd.NaT
        price_value = pd.NA if new_price is None else new_price
        rebate_value = pd.NA if new_rebate is None else new_rebate
        record = {
            "联系状态": new_status,
            "达人名": new_name,
            "ID": new_id,
            "账号主页": new_homepage,
            "微信号": new_wechat,
            "邮箱": new_email,
            "渠道": new_channel,
            "推广方式": new_method,
            "报价": price_value,
            "返点": rebate_value,
            "结算价": 0,
            "发布时间（最早）": publish_date,
            "发布链接": new_post_link,
            "素材": new_material,
            "备注": new_note,
        }
        try:
            total_rows = add_koc_record(df, record, koc_path)
        except Exception as exc:
            st.error(f"保存失败：{exc}")
        else:
            st.cache_data.clear()
            st.session_state["show_create_koc"] = False
            st.success(f"已新增达人：{new_name}。当前共 {total_rows} 位达人。")
            st.rerun()

st.subheader("达人明细")
legend_left, legend_right = st.columns(2)
with legend_left:
    render_chip_legend(STATUS_CONFIG)
with legend_right:
    render_chip_legend(CHANNEL_CONFIG)

filter_cols = st.columns([2, 1, 1, 1, 1.4])
with filter_cols[0]:
    keyword = st.text_input("搜索达人 / ID / 联系方式 / 备注")
with filter_cols[1]:
    selected_status = st.multiselect("联系状态", status_options)
with filter_cols[2]:
    selected_channels = st.multiselect("渠道", channel_options)
with filter_cols[3]:
    selected_methods = st.multiselect("推广方式", method_options)
with filter_cols[4]:
    valid_dates = df["发布时间（最早）"].dropna()
    if valid_dates.empty:
        date_range = None
        st.date_input("发布时间范围", value=None, disabled=True)
    else:
        date_range = st.date_input(
            "发布时间范围",
            value=(valid_dates.min().date(), valid_dates.max().date()),
            format="YYYY-MM-DD",
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
metric_cols[1].metric("合作中", f"{(filtered['联系状态'] == '合作中').sum():,}")
metric_cols[2].metric("已拒绝", f"{(filtered['联系状态'] == '已拒绝').sum():,}")
metric_cols[3].metric("结算价合计", f"¥{filtered['结算价'].sum():,.2f}")

chart_left, chart_right = st.columns(2)
with chart_left:
    status_counts = filtered["联系状态"].value_counts().reset_index()
    status_counts.columns = ["联系状态", "数量"]
    st.plotly_chart(
        px.bar(
            status_counts,
            x="联系状态",
            y="数量",
            text_auto=True,
            color="联系状态",
            color_discrete_map=STATUS_CONFIG,
        ),
        width="stretch",
    )

with chart_right:
    channel_counts = filtered["渠道"].value_counts().reset_index()
    channel_counts.columns = ["渠道", "数量"]
    st.plotly_chart(
        px.pie(
            channel_counts,
            names="渠道",
            values="数量",
            hole=0.45,
            color="渠道",
            color_discrete_map=CHANNEL_CONFIG,
        ),
        width="stretch",
    )

table = filtered.copy()
table["_row_id"] = table.index
original_snapshot = editor_snapshot(table)
edited_table = st.data_editor(
    table[["_row_id", *REQUIRED_COLUMNS]],
    width="stretch",
    hide_index=True,
    num_rows="fixed",
    key="koc_detail_editor",
    column_config={
        "_row_id": None,
        "联系状态": st.column_config.SelectboxColumn(
            "联系状态",
            options=status_options,
        ),
        "渠道": st.column_config.SelectboxColumn("渠道", options=channel_options),
        "推广方式": st.column_config.SelectboxColumn("推广方式", options=method_options),
        "账号主页": st.column_config.LinkColumn("账号主页", display_text="跳转主页"),
        "发布链接": st.column_config.TextColumn("发布链接"),
        "报价": st.column_config.NumberColumn("报价", format="¥%.2f", min_value=0),
        "返点": st.column_config.NumberColumn("返点（%）", format="%.2f%%", min_value=0, max_value=100),
        "结算价": st.column_config.NumberColumn("结算价", format="¥%.2f", disabled=True),
        "发布时间（最早）": st.column_config.DateColumn("发布时间（最早）", format="YYYY-MM-DD"),
    },
)

edited_snapshot = editor_snapshot(edited_table)
if not original_snapshot.equals(edited_snapshot):
    updated_df = apply_editor_changes(df, edited_table)
    try:
        save_koc_data(updated_df, koc_path)
    except Exception as exc:
        st.error(f"保存失败：{exc}")
    else:
        st.cache_data.clear()
        st.toast("达人表格修改已自动保存。")
        st.rerun()
