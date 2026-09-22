# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from backfill_douyin_daily_profit import (
    OUTPUT_PATH,
    allocate_store_ad_for_report,
    append_refund_only_rows,
    fetch_orders_for_day,
    fetch_promotion_with_timeout,
    fetch_refunds_for_range,
    iter_days,
    navigate_and_wait,
    numeric_sum,
    write_workbook,
)
from douyin_profit_crawler import (
    ORDER_PAGE_URL,
    build_profit,
    ensure_sku_cost_workbook,
    open_or_navigate_cdp,
    parse_orders,
    parse_refunds,
)


def run(port: int, start_day: str, end_day: str, output: str, promotion_timeout: int) -> int:
    days = iter_days(start_day, end_day)

    order_page = open_or_navigate_cdp(port, "fxg.jinritemai.com", ORDER_PAGE_URL, 3)
    order_frames: dict[str, pd.DataFrame] = {}
    all_order_frames: list[pd.DataFrame] = []
    try:
        navigate_and_wait(order_page, ORDER_PAGE_URL, 12)
        for index, day in enumerate(days, 1):
            orders = fetch_orders_for_day(order_page, day)
            orders_df = parse_orders(orders)
            order_frames[day] = orders_df
            if not orders_df.empty:
                all_order_frames.append(orders_df)
            print(
                f"[orders {index:02d}/{len(days)}] {day} "
                f"orders={len(orders)} sku_rows={len(orders_df)} "
                f"pay={numeric_sum(orders_df, '支付金额'):.2f}",
                flush=True,
            )
    finally:
        order_page.close()

    all_orders = pd.concat(all_order_frames, ignore_index=True) if all_order_frames else pd.DataFrame()
    if not all_orders.empty:
        ensure_sku_cost_workbook(all_orders, end_day)

    refund_page = open_or_navigate_cdp(port, "fxg.jinritemai.com", ORDER_PAGE_URL, 3)
    try:
        navigate_and_wait(refund_page, ORDER_PAGE_URL, 8)
        refunds = fetch_refunds_for_range(refund_page, start_day, end_day)
        refunds_df = parse_refunds(refunds)
    finally:
        refund_page.close()
    print(
        f"[refunds] rows={len(refunds_df)} amount={numeric_sum(refunds_df, '退款金额'):.2f}",
        flush=True,
    )

    promotion_frames: dict[str, pd.DataFrame] = {}
    for index, day in enumerate(days, 1):
        promo_df, promo_message = fetch_promotion_with_timeout(port, day, promotion_timeout)
        promotion_frames[day] = promo_df
        if promo_message:
            print(f"[promo {index:02d}/{len(days)}] {day} note={promo_message}", flush=True)
        print(
            f"[promo {index:02d}/{len(days)}] {day} "
            f"rows={len(promo_df)} total={numeric_sum(promo_df, '推广消耗合计'):.2f}",
            flush=True,
        )
        time.sleep(0.05)

    day_frames: dict[str, pd.DataFrame] = {}
    checks: list[dict[str, object]] = []
    for day in days:
        orders_df = order_frames.get(day, pd.DataFrame())
        day_refunds = refunds_df
        if not refunds_df.empty and "退款成功日期" in refunds_df.columns:
            day_refunds = refunds_df[refunds_df["退款成功日期"].astype(str).eq(day)].copy()
        profit_df = build_profit(orders_df, day_refunds, promotion_frames.get(day, pd.DataFrame()))
        profit_df = append_refund_only_rows(profit_df, orders_df, day_refunds)
        profit_df = allocate_store_ad_for_report(profit_df)
        day_frames[day] = profit_df
        checks.append(
            {
                "日期": day,
                "商品行": len(profit_df),
                "支付金额": round(numeric_sum(profit_df, "支付金额"), 2),
                "订单数": round(numeric_sum(profit_df, "SKU订单数"), 4),
                "件数": round(numeric_sum(profit_df, "SKU成交件数"), 4),
                "退款金额": round(numeric_sum(profit_df, "退款金额"), 2),
                "推广花费": round(numeric_sum(profit_df, "总推广消耗"), 2),
                "单品结余": round(numeric_sum(profit_df, "实时盈亏"), 2),
                "待补成本行": int((profit_df.get("成本匹配状态", pd.Series(dtype=str)).astype(str) != "已匹配").sum())
                if not profit_df.empty
                else 0,
            }
        )

    output_path = Path(output)
    write_workbook(day_frames, output_path)
    checks_df = pd.DataFrame(checks)
    checks_path = output_path.with_name(output_path.stem + "_回填校验.csv")
    checks_df.to_csv(checks_path, index=False, encoding="utf-8-sig")
    print(f"OUTPUT={output_path.resolve()}", flush=True)
    print(f"CHECKS={checks_path.resolve()}", flush=True)
    print(
        "SUMMARY",
        f"days={len(days)}",
        f"pay={checks_df['支付金额'].sum():.2f}",
        f"refund={checks_df['退款金额'].sum():.2f}",
        f"ad={checks_df['推广花费'].sum():.2f}",
        f"profit={checks_df['单品结余'].sum():.2f}",
        f"missing_cost_rows={int(checks_df['待补成本行'].sum())}",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9226)
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-09-19")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--promotion-timeout", type=int, default=75)
    args = parser.parse_args()
    return run(args.port, args.start, args.end, args.output, args.promotion_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
