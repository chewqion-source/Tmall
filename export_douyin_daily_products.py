# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import json
import time

import pandas as pd

from douyin_profit_crawler import (
    AFTERSALE_LIST_URL,
    ORDER_LIST_URL,
    ORDER_PAGE_URL,
    browser_fetch_json,
    current_page_tokens,
    day_bounds,
    is_successful_refund,
    open_or_navigate_cdp,
    parse_orders,
    parse_refunds,
)


PORT = 9226
START_DAY = "2026-07-01"
END_DAY = "2026-09-16"
OUTPUT_DIR = Path("outputs") / "douyin_daily_product_export"
OUTPUT_FILE = OUTPUT_DIR / f"盲盒抖店_商品日数据_{START_DAY}_至_{END_DAY}.xlsx"
PAGE_SIZE = 50

ORDER_BASE = {
    "order": "desc",
    "order_by": "create_time",
    "pageSize": PAGE_SIZE,
    "source": "shop_order_view_upgrade",
    "tab": "all",
    "order_status": "all",
}


def iter_days(start_day: str, end_day: str):
    cur = datetime.strptime(start_day, "%Y-%m-%d").date()
    end = datetime.strptime(end_day, "%Y-%m-%d").date()
    while cur <= end:
        yield cur.isoformat()
        cur += timedelta(days=1)


def fetch_orders_for_day(page, day: str, max_pages: int = 120) -> list[dict]:
    start_ts, end_ts = day_bounds(day)
    rows: list[dict] = []
    for page_no in range(max_pages):
        params = {
            **ORDER_BASE,
            "page": page_no,
            "create_time_start": start_ts,
            "create_time_end": end_ts,
        }
        payload = browser_fetch_json(page, ORDER_LIST_URL, params)
        if not isinstance(payload, dict):
            break
        batch = payload.get("data") or []
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(0.05)
    return rows


def fetch_refunds_range(page, start_day: str, end_day: str, max_pages: int = 300) -> list[dict]:
    start_ts = int(datetime.strptime(start_day, "%Y-%m-%d").timestamp())
    end_ts = int((datetime.strptime(end_day, "%Y-%m-%d") + timedelta(days=1)).timestamp()) - 1
    tokens = current_page_tokens(page)
    seen: set[str] = set()
    rows: list[dict] = []
    older_streak = 0

    for page_no in range(1, max_pages + 1):
        params = {
            "appid": 1,
            "_bid": "ffa_aftersale",
            "aid": "4272",
            "aftersale_platform_source": "fxg",
            "page": page_no,
            "pageSize": PAGE_SIZE,
            "__token": tokens.get("csrf", ""),
            "verifyFp": tokens.get("verifyFp", ""),
            "fp": tokens.get("fp", ""),
        }
        payload = browser_fetch_json(page, AFTERSALE_LIST_URL, params, method="POST")
        if not isinstance(payload, dict):
            break
        items = ((payload.get("data") or {}).get("items")) or []
        if not isinstance(items, list) or not items:
            break

        page_times: list[int] = []
        for item in items:
            info = item.get("after_sale_info") or {}
            raw_ts = int(info.get("update_time") or info.get("create_time") or 0)
            if raw_ts > 10_000_000_000:
                raw_ts //= 1000
            if raw_ts:
                page_times.append(raw_ts)
            if not (start_ts <= raw_ts <= end_ts):
                continue
            refund_day = datetime.fromtimestamp(raw_ts).strftime("%Y-%m-%d")
            if not is_successful_refund(item, refund_day):
                continue
            key = str(
                info.get("after_sale_id")
                or info.get("related_id")
                or json.dumps(item, ensure_ascii=False, sort_keys=True)[:500]
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)

        if page_times and max(page_times) < start_ts:
            older_streak += 1
        else:
            older_streak = 0
        if older_streak >= 2 or len(items) < PAGE_SIZE:
            break
        time.sleep(0.05)
    return rows


def numeric_sum(df: pd.DataFrame, col: str) -> float:
    if df.empty or col not in df.columns:
        return 0.0
    return round(float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum()), 2)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    page = open_or_navigate_cdp(PORT, "fxg.jinritemai.com", ORDER_PAGE_URL, 3)
    all_order_frames: list[pd.DataFrame] = []
    checks: list[dict] = []

    try:
        days = list(iter_days(START_DAY, END_DAY))
        for index, day in enumerate(days, 1):
            orders = fetch_orders_for_day(page, day)
            order_df = parse_orders(orders)
            if not order_df.empty:
                all_order_frames.append(order_df)
            checks.append(
                {
                    "日期": day,
                    "订单原始行数": len(orders),
                    "SKU明细行数": len(order_df),
                    "支付金额": numeric_sum(order_df, "支付金额"),
                    "支付件数": numeric_sum(order_df, "SKU成交件数"),
                    "支付订单数": numeric_sum(order_df, "SKU订单数"),
                }
            )
            print(f"[{index:02d}/{len(days)}] {day} orders={len(orders)} sku_rows={len(order_df)}")
            time.sleep(0.08)

        refund_items = fetch_refunds_range(page, START_DAY, END_DAY)
        refunds_df = parse_refunds(refund_items)
    finally:
        page.close()

    orders_df = pd.concat(all_order_frames, ignore_index=True) if all_order_frames else pd.DataFrame()
    if orders_df.empty:
        raise RuntimeError("没有抓到订单数据，请确认 9226 端口浏览器已登录抖店。")

    order_detail = orders_df.rename(
        columns={
            "付款日期": "日期",
            "SKU成交件数": "支付件数",
            "SKU订单数": "支付订单数",
        }
    )
    keys = ["日期", "商品ID", "商家编码", "SKU规格"]
    order_summary = (
        order_detail.groupby(keys, dropna=False, as_index=False)
        .agg(
            商品名称=("商品名称", "last"),
            支付金额=("支付金额", "sum"),
            支付件数=("支付件数", "sum"),
            支付订单数=("支付订单数", "sum"),
        )
    )

    if refunds_df.empty:
        refund_summary = pd.DataFrame(columns=[*keys, "退款金额"])
    else:
        refund_detail = refunds_df.rename(columns={"退款成功日期": "日期"})
        refund_summary = (
            refund_detail.groupby(keys, dropna=False, as_index=False)
            .agg(退款金额=("退款金额", "sum"))
        )

    result = order_summary.merge(refund_summary, on=keys, how="outer")
    result["商品名称"] = result["商品名称"].fillna("")
    for col in ["支付金额", "支付件数", "支付订单数", "退款金额"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0)
    result = result[
        ["日期", "商品ID", "商品名称", "商家编码", "SKU规格", "支付金额", "支付件数", "支付订单数", "退款金额"]
    ].sort_values(["日期", "商品ID", "商家编码", "SKU规格"], ignore_index=True)

    daily_summary = (
        result.groupby("日期", as_index=False)
        .agg(支付金额=("支付金额", "sum"), 支付件数=("支付件数", "sum"), 支付订单数=("支付订单数", "sum"), 退款金额=("退款金额", "sum"))
    )
    checks_df = pd.DataFrame(checks)

    for frame in [result, daily_summary, checks_df]:
        for col in ["支付金额", "退款金额"]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).round(2)
        for col in ["支付件数", "支付订单数"]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).round(4)

    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        result.to_excel(writer, sheet_name="商品SKU日汇总", index=False)
        daily_summary.to_excel(writer, sheet_name="每日汇总", index=False)
        checks_df.to_excel(writer, sheet_name="抓取检查", index=False)
        order_detail.to_excel(writer, sheet_name="订单明细", index=False)
        refunds_df.to_excel(writer, sheet_name="退款明细", index=False)

    print(f"OUTPUT={OUTPUT_FILE.resolve()}")
    print(
        "SUMMARY",
        f"sku_rows={len(result)}",
        f"order_detail_rows={len(order_detail)}",
        f"refund_rows={len(refunds_df)}",
        f"pay={numeric_sum(result, '支付金额')}",
        f"qty={numeric_sum(result, '支付件数')}",
        f"orders={numeric_sum(result, '支付订单数')}",
        f"refund={numeric_sum(result, '退款金额')}",
    )


if __name__ == "__main__":
    main()
