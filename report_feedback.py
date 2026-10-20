from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from data_loader import load_product_daily


BASE_DIR = Path(__file__).resolve().parent
FEEDBACK_WEBHOOK_CONFIG = BASE_DIR / "config" / "report_feedback_feishu_webhook.json"


@dataclass(frozen=True)
class ReportFeedback:
    store: str
    date: str
    previous_date: str | None
    has_previous: bool
    summary: pd.DataFrame
    products: pd.DataFrame
    suggestions: list[str]
    message: str = ""


METRIC_LABELS = {
    "pay_amount": "支付金额",
    "order_count": "订单数",
    "sales_qty": "件数",
    "profit": "盈亏",
    "refund_amount": "退款金额",
    "ad_cost": "推广费",
    "客单价": "客单价",
    "退款率": "退款率",
    "推广占比": "推广占比",
    "利润率": "利润率",
}


def _sum(frame: pd.DataFrame, column: str) -> float:
    if column not in frame.columns:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0).sum())


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _day_totals(frame: pd.DataFrame) -> dict[str, float]:
    pay = _sum(frame, "pay_amount")
    orders = _sum(frame, "order_count")
    profit = _sum(frame, "profit")
    refund = _sum(frame, "refund_amount")
    ad = _sum(frame, "ad_cost")
    return {
        "pay_amount": pay,
        "order_count": orders,
        "sales_qty": _sum(frame, "sales_qty"),
        "profit": profit,
        "refund_amount": refund,
        "ad_cost": ad,
        "客单价": _ratio(pay, orders),
        "退款率": _ratio(refund, pay),
        "推广占比": _ratio(ad, pay),
        "利润率": _ratio(profit, pay),
    }


def _summary_frame(current: pd.DataFrame, previous: pd.DataFrame) -> pd.DataFrame:
    cur = _day_totals(current)
    prev = _day_totals(previous)
    rows = []
    for key in [
        "pay_amount",
        "order_count",
        "sales_qty",
        "profit",
        "refund_amount",
        "ad_cost",
        "客单价",
        "退款率",
        "推广占比",
        "利润率",
    ]:
        current_value = cur[key]
        previous_value = prev[key]
        delta = current_value - previous_value
        change_rate = _ratio(delta, abs(previous_value))
        rows.append(
            {
                "指标": METRIC_LABELS[key],
                "今日": current_value,
                "昨日": previous_value,
                "变化": delta,
                "变化率": change_rate,
            }
        )
    return pd.DataFrame(rows)


def _product_frame(current: pd.DataFrame, previous: pd.DataFrame) -> pd.DataFrame:
    product_columns = [
        "pay_amount",
        "order_count",
        "sales_qty",
        "profit",
        "refund_amount",
        "ad_cost",
    ]
    current_product = (
        current.groupby("product_id", as_index=False)
        .agg(
            pay_amount=("pay_amount", "sum"),
            order_count=("order_count", "sum"),
            sales_qty=("sales_qty", "sum"),
            profit=("profit", "sum"),
            refund_amount=("refund_amount", "sum"),
            ad_cost=("ad_cost", "sum"),
            skus=("skus", lambda values: "；".join(dict.fromkeys(str(v) for v in values if str(v).strip()))),
        )
        .rename(columns={column: f"{column}_today" for column in product_columns})
    )
    previous_product = (
        previous.groupby("product_id", as_index=False)
        .agg(
            pay_amount=("pay_amount", "sum"),
            order_count=("order_count", "sum"),
            sales_qty=("sales_qty", "sum"),
            profit=("profit", "sum"),
            refund_amount=("refund_amount", "sum"),
            ad_cost=("ad_cost", "sum"),
        )
        .rename(columns={column: f"{column}_yesterday" for column in product_columns})
    )
    merged = current_product.merge(previous_product, on="product_id", how="outer")
    for column in product_columns:
        merged[f"{column}_today"] = pd.to_numeric(merged[f"{column}_today"], errors="coerce").fillna(0)
        merged[f"{column}_yesterday"] = pd.to_numeric(merged[f"{column}_yesterday"], errors="coerce").fillna(0)
        merged[f"{column}_delta"] = merged[f"{column}_today"] - merged[f"{column}_yesterday"]
    if "skus" not in merged.columns:
        merged["skus"] = ""
    merged["波动分"] = (
        merged["profit_delta"].abs() * 1.0
        + merged["pay_amount_delta"].abs() * 0.25
        + merged["refund_amount_delta"].abs() * 0.6
        + merged["ad_cost_delta"].abs() * 0.6
        + merged["sales_qty_delta"].abs() * 2.0
    )
    merged = merged[
        ~merged["product_id"].astype(str).str.upper().str.startswith(("STOREADJ", "LEGACY"))
    ].copy()
    merged = merged.sort_values("波动分", ascending=False, ignore_index=True)
    return merged


def _product_reason(row: pd.Series) -> str:
    reasons = []
    if row["profit_delta"] < 0:
        if row["ad_cost_delta"] > 0:
            reasons.append(f"推广费增加 {row['ad_cost_delta']:.2f}")
        if row["refund_amount_delta"] > 0:
            reasons.append(f"退款增加 {row['refund_amount_delta']:.2f}")
        if row["pay_amount_delta"] < 0:
            reasons.append(f"支付下降 {abs(row['pay_amount_delta']):.2f}")
        if row["sales_qty_delta"] < 0:
            reasons.append(f"件数下降 {abs(row['sales_qty_delta']):.0f}")
    else:
        if row["pay_amount_delta"] > 0:
            reasons.append(f"支付增加 {row['pay_amount_delta']:.2f}")
        if row["ad_cost_delta"] < 0:
            reasons.append(f"推广下降 {abs(row['ad_cost_delta']):.2f}")
        if row["refund_amount_delta"] < 0:
            reasons.append(f"退款减少 {abs(row['refund_amount_delta']):.2f}")
    return "，".join(reasons[:3]) or "综合变化"


def _suggestions(summary: pd.DataFrame, products: pd.DataFrame) -> list[str]:
    values = {row["指标"]: row for _, row in summary.iterrows()}
    profit_delta = float(values["盈亏"]["变化"])
    pay_delta = float(values["支付金额"]["变化"])
    ad_delta = float(values["推广费"]["变化"])
    refund_delta = float(values["退款金额"]["变化"])
    order_delta = float(values["订单数"]["变化"])
    suggestions: list[str] = []

    if profit_delta < 0:
        if ad_delta > 0:
            suggestions.append("盈亏下降且推广费上升，优先检查推广计划 ROI 和高消耗低转化商品。")
        if refund_delta > 0:
            suggestions.append("退款金额上升，建议排查退款集中的商品、规格和售后原因。")
        if pay_delta < 0 or order_delta < 0:
            suggestions.append("成交规模下降，建议看流量入口、主推商品库存和价格活动是否变化。")
    else:
        suggestions.append("整体盈亏较昨日改善，建议保留贡献最大的商品和投放结构。")

    loss_products = products[products["profit_delta"] < 0].head(3)
    if not loss_products.empty:
        ids = "、".join(loss_products["product_id"].astype(str).tolist())
        suggestions.append(f"重点复盘波动商品：{ids}，它们是本次盈亏变化的主要来源。")

    gain_products = products[products["profit_delta"] > 0].head(3)
    if not gain_products.empty:
        ids = "、".join(gain_products["product_id"].astype(str).tolist())
        suggestions.append(f"可关注正向贡献商品：{ids}，如果库存和毛利稳定，可考虑承接更多流量。")

    return suggestions[:5]


def _format_products(products: pd.DataFrame) -> pd.DataFrame:
    rows = products.head(12).copy()
    rows["主要原因"] = rows.apply(_product_reason, axis=1)
    rows["商品/规格线索"] = rows["skus"].fillna("").astype(str).str.slice(0, 42)
    return rows[
        [
            "product_id",
            "商品/规格线索",
            "profit_today",
            "profit_yesterday",
            "profit_delta",
            "pay_amount_delta",
            "ad_cost_delta",
            "refund_amount_delta",
            "sales_qty_delta",
            "主要原因",
        ]
    ].rename(
        columns={
            "product_id": "商品ID",
            "profit_today": "今日盈亏",
            "profit_yesterday": "昨日盈亏",
            "profit_delta": "盈亏变化",
            "pay_amount_delta": "支付变化",
            "ad_cost_delta": "推广变化",
            "refund_amount_delta": "退款变化",
            "sales_qty_delta": "件数变化",
        }
    )


def analyze_uploaded_report_dates(
    store: str,
    workbook_path: str | Path,
    changed_dates: list[str] | tuple[str, ...],
) -> list[ReportFeedback]:
    if not changed_dates:
        return []

    daily = load_product_daily(workbook_path)
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    daily = daily.dropna(subset=["date"])
    results: list[ReportFeedback] = []

    for raw_date in sorted(set(changed_dates)):
        current_date = pd.Timestamp(raw_date)
        previous_date = current_date - pd.Timedelta(days=1)
        current = daily[daily["date"] == current_date].copy()
        previous = daily[daily["date"] == previous_date].copy()
        if current.empty:
            results.append(
                ReportFeedback(
                    store=store,
                    date=current_date.strftime("%Y-%m-%d"),
                    previous_date=previous_date.strftime("%Y-%m-%d"),
                    has_previous=False,
                    summary=pd.DataFrame(),
                    products=pd.DataFrame(),
                    suggestions=[],
                    message="本次上传日期在解析结果中没有明细，暂时无法分析。",
                )
            )
            continue
        if previous.empty:
            results.append(
                ReportFeedback(
                    store=store,
                    date=current_date.strftime("%Y-%m-%d"),
                    previous_date=previous_date.strftime("%Y-%m-%d"),
                    has_previous=False,
                    summary=pd.DataFrame(),
                    products=pd.DataFrame(),
                    suggestions=[],
                    message="没有找到前一天日报，无法做昨日对比。",
                )
            )
            continue

        summary = _summary_frame(current, previous)
        products = _product_frame(current, previous)
        results.append(
            ReportFeedback(
                store=store,
                date=current_date.strftime("%Y-%m-%d"),
                previous_date=previous_date.strftime("%Y-%m-%d"),
                has_previous=True,
                summary=summary,
                products=_format_products(products),
                suggestions=_suggestions(summary, products),
            )
        )
    return results


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}¥{abs(value):,.2f}"


def _delta(value: float) -> str:
    if abs(value) < 0.005:
        return "持平"
    return f"{'增加' if value > 0 else '下降'}{_money(abs(value))}"


def _summary_value(summary: pd.DataFrame, metric: str, column: str) -> float:
    if summary.empty:
        return 0.0
    rows = summary[summary["指标"] == metric]
    if rows.empty or column not in rows.columns:
        return 0.0
    return float(pd.to_numeric(rows.iloc[0][column], errors="coerce") or 0.0)


def _load_feedback_webhook() -> str:
    webhook = os.environ.get("REPORT_FEEDBACK_FEISHU_WEBHOOK", "").strip()
    if webhook:
        return webhook
    if not FEEDBACK_WEBHOOK_CONFIG.exists():
        return ""
    payload = json.loads(FEEDBACK_WEBHOOK_CONFIG.read_text(encoding="utf-8"))
    return str(payload.get("webhook", "")).strip()


def _short_feedback_text(item: ReportFeedback) -> str:
    if not item.has_previous:
        return f"财务已导入【{item.store}】{item.date}日报，但{item.message}"

    profit_delta = _summary_value(item.summary, "盈亏", "变化")
    pay_delta = _summary_value(item.summary, "支付金额", "变化")
    ad_delta = _summary_value(item.summary, "推广费", "变化")
    refund_delta = _summary_value(item.summary, "退款金额", "变化")

    lines = [
        f"财务已导入【{item.store}】{item.date}日报。",
        f"我看了下：盈亏{_delta(profit_delta)}，支付{_delta(pay_delta)}，推广{_delta(ad_delta)}，退款{_delta(refund_delta)}。",
    ]

    if not item.products.empty:
        row = item.products.iloc[0]
        product_id = str(row.get("商品ID", "")).strip()
        clue = str(row.get("商品/规格线索", "")).strip()
        reason = str(row.get("主要原因", "")).strip()
        product_label = f"{clue}｜{product_id}" if clue else product_id
        product_line = (
            f"影响最大的是 {product_label}："
            f"盈亏{_delta(float(row.get('盈亏变化', 0) or 0))}，"
            f"支付{_delta(float(row.get('支付变化', 0) or 0))}。"
        )
        if reason:
            product_line += f"主要是{reason}。"
        lines.append(product_line)

    if item.suggestions:
        lines.append(item.suggestions[0].rstrip("。") + "，要不要看下？")

    return "\n".join(lines)


def send_report_feedback_messages(items: list[ReportFeedback]) -> int:
    webhook = _load_feedback_webhook()
    if not webhook:
        return 0
    text = build_report_feedback_batch_text(items)
    if not text:
        return 0
    body = json.dumps(
        {"msg_type": "text", "content": {"text": text}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        payload = response.read().decode("utf-8", errors="ignore")
        if response.status >= 400:
            raise RuntimeError(payload)
        try:
            result = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"飞书返回异常：{payload}") from exc
        if int(result.get("code", result.get("StatusCode", -1)) or 0) != 0:
            raise RuntimeError(result.get("msg", payload))
    return 1


def build_report_feedback_batch_text(items: list[ReportFeedback]) -> str:
    if not items:
        return ""
    valid_items = [item for item in items if item.has_previous]
    first = items[0]
    stores = "、".join(dict.fromkeys(item.store for item in items))
    dates = [item.date for item in items]
    date_text = dates[0] if len(set(dates)) == 1 else f"{min(dates)}~{max(dates)}"
    if not valid_items:
        return f"财务已导入【{stores}】{date_text}日报，但没有找到可对比的昨日数据。"

    summary_rows = []
    focus_item: ReportFeedback | None = None
    focus_score = -1.0
    focus_row: pd.Series | None = None
    for item in valid_items:
        profit_delta = _summary_value(item.summary, "盈亏", "变化")
        pay_delta = _summary_value(item.summary, "支付金额", "变化")
        ad_delta = _summary_value(item.summary, "推广费", "变化")
        refund_delta = _summary_value(item.summary, "退款金额", "变化")
        summary_rows.append(
            {
                "store": item.store,
                "date": item.date,
                "profit_delta": profit_delta,
                "pay_delta": pay_delta,
                "ad_delta": ad_delta,
                "refund_delta": refund_delta,
            }
        )
        if not item.products.empty:
            row = item.products.iloc[0]
            score = abs(float(row.get("盈亏变化", 0) or 0)) + abs(float(row.get("支付变化", 0) or 0)) * 0.2
            if score > focus_score:
                focus_score = score
                focus_item = item
                focus_row = row

    total_profit_delta = sum(row["profit_delta"] for row in summary_rows)
    total_pay_delta = sum(row["pay_delta"] for row in summary_rows)
    total_ad_delta = sum(row["ad_delta"] for row in summary_rows)
    total_refund_delta = sum(row["refund_delta"] for row in summary_rows)

    lines = [
        f"财务已导入【{stores}】{date_text}日报。",
        f"我看了下：盈亏{_delta(total_profit_delta)}，支付{_delta(total_pay_delta)}，推广{_delta(total_ad_delta)}，退款{_delta(total_refund_delta)}。",
    ]

    if focus_item is not None and focus_row is not None:
        product_id = str(focus_row.get("商品ID", "")).strip()
        clue = str(focus_row.get("商品/规格线索", "")).strip()
        reason = str(focus_row.get("主要原因", "")).strip()
        product_label = f"{clue}｜{product_id}" if clue else product_id
        line = (
            f"波动最大的是【{focus_item.store}】{product_label}："
            f"盈亏{_delta(float(focus_row.get('盈亏变化', 0) or 0))}，"
            f"支付{_delta(float(focus_row.get('支付变化', 0) or 0))}。"
        )
        if reason:
            line += f"主要是{reason}。"
        lines.append(line)

    if total_profit_delta < 0:
        if total_refund_delta > 0:
            lines.append("建议先看退款集中的商品和规格，要不要我继续拆一下？")
        elif total_ad_delta > 0:
            lines.append("建议先看推广消耗上涨的商品，要不要我继续拆一下？")
        else:
            lines.append("建议先看支付下滑和件数下滑的商品，要不要我继续拆一下？")
    else:
        lines.append("整体比昨日改善，可以先保留贡献最大的商品和投放结构。")

    return "\n".join(lines[:4])
