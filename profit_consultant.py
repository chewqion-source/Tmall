from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def _format_money(value: float) -> str:
    return f"¥{value:,.2f}"


def _format_roi(value: object) -> str:
    if pd.isna(value):
        return "-"
    return f"{float(value):.2f}"


def _safe_float(value: object) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _advice(row: pd.Series) -> list[str]:
    pay_amount = _safe_float(row.get("pay_amount"))
    ad_cost = _safe_float(row.get("ad_cost"))
    profit = _safe_float(row.get("profit"))
    gross_profit = _safe_float(row.get("gross_profit"))
    refund_amount = _safe_float(row.get("refund_amount"))
    promotion_roi = row.get("promotion_roi")
    promotion_roi_value = None if pd.isna(promotion_roi) else _safe_float(promotion_roi)

    margin_before_ad = gross_profit - refund_amount
    overall_breakeven_roi = pay_amount / margin_before_ad if margin_before_ad > 0 else None
    non_ad_cost = (
        _safe_float(row.get("merch_cost"))
        + _safe_float(row.get("freight_cost"))
        + _safe_float(row.get("platform_fee"))
        + _safe_float(row.get("tax_fee"))
        + refund_amount
    )
    ad_only_margin_rate = (pay_amount - non_ad_cost) / pay_amount if pay_amount > 0 else 0
    ad_only_breakeven_roi = 1 / ad_only_margin_rate if ad_only_margin_rate > 0 else None

    advice: list[str] = []
    if profit < 0:
        advice.append(f"当前亏损 {_format_money(abs(profit))}，先不要放量，优先压推广消耗或提高客单价。")
    elif profit < pay_amount * 0.03:
        advice.append("当前接近保本，投放可以继续测，但不适合快速放大。")
    else:
        advice.append("当前整体盈利，可以保留投放观察，但仍要看推广本身是否保本。")

    if ad_cost > 0 and promotion_roi_value is not None:
        if ad_only_breakeven_roi and promotion_roi_value < ad_only_breakeven_roi:
            advice.append(
                f"仅看投放，当前ROI {_format_roi(promotion_roi_value)} 低于投放保本ROI "
                f"{_format_roi(ad_only_breakeven_roi)}，建议降价出价或缩预算。"
            )
        elif ad_only_breakeven_roi:
            advice.append(
                f"仅看投放，当前ROI {_format_roi(promotion_roi_value)} 高于投放保本ROI "
                f"{_format_roi(ad_only_breakeven_roi)}，可以小步加预算。"
            )
    elif pay_amount > 0:
        advice.append("当前没有推广消耗，适合作为自然成交参考，开投前先按投放保本ROI设目标。")

    freight_cost = _safe_float(row.get("freight_cost"))
    merch_cost = _safe_float(row.get("merch_cost"))
    if pay_amount > 0 and freight_cost / pay_amount > 0.25:
        advice.append("快递费占比较高，优先检查是否有低客单、多件包邮或快递费录入偏高的问题。")
    if pay_amount > 0 and merch_cost / pay_amount > 0.45:
        advice.append("货品成本占比较高，投放目标ROI要更保守，低价SKU不建议重投。")
    if refund_amount > 0:
        advice.append(f"当前退款 {_format_money(refund_amount)} 已计入盈亏，放量前建议看退款原因。")
    if _safe_float(row.get("unmatched_sku_rows")) > 0:
        advice.append("该商品存在未匹配SKU，成本可能不完整，先补齐SKU成本再做投放判断。")
    if overall_breakeven_roi:
        advice.append(
            f"整体保本ROI参考 {_format_roi(overall_breakeven_roi)}；"
            f"投放单独保本ROI参考 {_format_roi(ad_only_breakeven_roi) if ad_only_breakeven_roi else '-'}。"
        )
    return advice


def render_product_profit_consultant(realtime_rows: pd.DataFrame) -> None:
    if realtime_rows.empty:
        return

    st.subheader("单品盈亏咨询")
    search_col, hint_col = st.columns([0.42, 0.58], vertical_alignment="bottom")
    with search_col:
        query = st.text_input(
            "输入商品ID",
            placeholder="例如 695622997368",
            key="profit_consult_product_id",
        ).strip()
    with hint_col:
        st.caption("输入商品ID后，自动按最新实时抓取数据拆解盈亏，并给出投放调整建议。")

    if not query:
        return

    rows = realtime_rows[
        realtime_rows["product_id"].astype(str).str.contains(query, case=False, na=False)
    ].copy()
    if rows.empty:
        st.warning("没有在最新实时数据里找到这个商品ID，请确认是否输入完整，或等下一轮抓取后再查。")
        return

    if len(rows) > 1:
        options = [
            f"{row.store} | {row.product_id} | {str(getattr(row, 'product_name', ''))[:28]}"
            for row in rows.itertuples(index=False)
        ]
        selected_option = st.selectbox("匹配到多个商品，请选择", options, key="profit_consult_match")
        row = rows.iloc[options.index(selected_option)]
    else:
        row = rows.iloc[0]

    pay_amount = _safe_float(row.get("pay_amount"))
    merch_cost = _safe_float(row.get("merch_cost"))
    freight_cost = _safe_float(row.get("freight_cost"))
    platform_fee = _safe_float(row.get("platform_fee"))
    tax_fee = _safe_float(row.get("tax_fee"))
    refund_amount = _safe_float(row.get("refund_amount"))
    ad_cost = _safe_float(row.get("ad_cost"))
    profit = _safe_float(row.get("profit"))
    gross_profit = _safe_float(row.get("gross_profit"))

    margin_before_ad = gross_profit - refund_amount
    overall_breakeven_roi = pay_amount / margin_before_ad if margin_before_ad > 0 else None
    non_ad_cost = merch_cost + freight_cost + platform_fee + tax_fee + refund_amount
    ad_only_margin_rate = (pay_amount - non_ad_cost) / pay_amount if pay_amount > 0 else 0
    ad_only_breakeven_roi = 1 / ad_only_margin_rate if ad_only_margin_rate > 0 else None

    st.markdown(f"**{row.get('store', '')} ｜ {row.get('product_id', '')}**")
    st.caption(str(row.get("product_name", "")))

    metric_cols = st.columns(5)
    metric_cols[0].metric("支付金额", _format_money(pay_amount))
    metric_cols[1].metric("成交件数", f"{_safe_float(row.get('sales_qty')):,.0f}")
    metric_cols[2].metric("实时盈亏", _format_money(profit))
    metric_cols[3].metric("推广ROI", _format_roi(row.get("promotion_roi")))
    metric_cols[4].metric("投放保本ROI", _format_roi(ad_only_breakeven_roi) if ad_only_breakeven_roi else "-")

    chart_col, advice_col = st.columns([0.58, 0.42], gap="large")
    with chart_col:
        fig = go.Figure(
            go.Waterfall(
                orientation="v",
                measure=["relative", "relative", "relative", "relative", "relative", "relative", "relative", "total"],
                x=["支付金额", "货品成本", "快递成本", "平台费", "税费", "退款", "推广费", "实时盈亏"],
                y=[pay_amount, -merch_cost, -freight_cost, -platform_fee, -tax_fee, -refund_amount, -ad_cost, profit],
                connector={"line": {"color": "#cbd5e1"}},
                increasing={"marker": {"color": "#16a34a"}},
                decreasing={"marker": {"color": "#dc2626"}},
                totals={"marker": {"color": "#2563eb" if profit >= 0 else "#ef4444"}},
                text=[
                    _format_money(pay_amount),
                    _format_money(-merch_cost),
                    _format_money(-freight_cost),
                    _format_money(-platform_fee),
                    _format_money(-tax_fee),
                    _format_money(-refund_amount),
                    _format_money(-ad_cost),
                    _format_money(profit),
                ],
                textposition="outside",
                hovertemplate="%{x}<br>%{y:,.2f}<extra></extra>",
            )
        )
        fig.update_layout(
            height=320,
            margin=dict(l=8, r=8, t=12, b=8),
            yaxis_title="",
            showlegend=False,
            font=dict(size=12),
        )
        fig.add_hline(y=0, line_color="#94a3b8", line_width=1)
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    with advice_col:
        st.markdown("**调整建议**")
        for item in _advice(row):
            st.markdown(f"- {item}")
        st.markdown("**关键口径**")
        st.caption(
            f"整体保本ROI：{_format_roi(overall_breakeven_roi) if overall_breakeven_roi else '-'}；"
            f"投放保本ROI：{_format_roi(ad_only_breakeven_roi) if ad_only_breakeven_roi else '-'}。"
        )
