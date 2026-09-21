# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import os
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook

from douyin_profit_crawler import (
    AFTERSALE_LIST_URL,
    ORDER_LIST_URL,
    ORDER_PAGE_URL,
    SHOP_NAME,
    browser_fetch_json,
    build_profit,
    current_page_tokens,
    day_bounds,
    ensure_sku_cost_workbook,
    fetch_complete_promotion_total,
    fetch_product_promotions,
    fetch_realtime_qianchuan_summary,
    is_successful_refund,
    open_or_navigate_cdp,
    parse_orders,
    parse_refunds,
    reconcile_promotions_with_total,
    text,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_PATH = DATA_DIR / "2026年盲盒抖音日报表.xlsx"
PAGE_SIZE = 50

ORDER_BASE = {
    "order": "desc",
    "order_by": "create_time",
    "pageSize": PAGE_SIZE,
    "source": "shop_order_view_upgrade",
    "tab": "all",
    "order_status": "all",
}

REPORT_COLUMNS = [
    "商品ID",
    "商品名称",
    "货号",
    "SKU规格",
    "价格",
    "数量",
    "货总价",
    "支付金额",
    "成功退款金额",
    "推广花费",
    "订单数",
    "发货费",
    "税额",
    "扣点",
    "单品结余",
    "成本状态",
    "收入口径",
]


def iter_days(start_day: str, end_day: str) -> list[str]:
    current = datetime.strptime(start_day, "%Y-%m-%d").date()
    end = datetime.strptime(end_day, "%Y-%m-%d").date()
    days = []
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def fetch_orders_for_day(page, day: str, max_pages: int = 160) -> list[dict[str, Any]]:
    start_ts, end_ts = day_bounds(day)
    rows: list[dict[str, Any]] = []
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


def navigate_and_wait(page, url: str, wait_seconds: int = 10) -> None:
    page.call("Page.enable")
    page.call("Page.navigate", {"url": url})
    time.sleep(wait_seconds)


def fetch_refunds_for_range(page, start_day: str, end_day: str, max_pages: int = 500) -> list[dict[str, Any]]:
    start_ts = int(datetime.strptime(start_day, "%Y-%m-%d").timestamp())
    end_ts = int((datetime.strptime(end_day, "%Y-%m-%d") + timedelta(days=1)).timestamp()) - 1
    tokens = current_page_tokens(page)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
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


def numeric_sum(df: pd.DataFrame, column: str) -> float:
    if df.empty or column not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[column], errors="coerce").fillna(0).sum())


def allocate_store_ad_for_report(day_profit: pd.DataFrame) -> pd.DataFrame:
    df = day_profit.copy()
    store_ad = float(df.attrs.get("store_ad_cost", 0.0) or 0.0)
    if df.empty:
        if store_ad <= 0:
            return df
        return pd.DataFrame(
            [
                {
                    "店铺": SHOP_NAME,
                    "商品ID": "STOREADJDOUYIN",
                    "商品名称": "店铺级推广消耗",
                    "商家编码": "",
                    "SKU规格": "店铺级推广消耗",
                    "支付金额": 0.0,
                    "SKU订单数": 0.0,
                    "SKU成交件数": 0.0,
                    "退款金额": 0.0,
                    "单件货价": 0.0,
                    "快递费": 0.0,
                    "货品成本": 0.0,
                    "快递成本": 0.0,
                    "平台费用": 0.0,
                    "税费": 0.0,
                    "总推广消耗": store_ad,
                    "实时盈亏": -store_ad,
                    "成本匹配状态": "店铺级调整",
                    "收入取值口径": "",
                }
            ]
        )

    pay = pd.to_numeric(df.get("支付金额", 0), errors="coerce").fillna(0.0)
    if pay.sum() > 0:
        weights = pay / pay.sum()
    else:
        weights = pd.Series(1 / len(df), index=df.index)
    allocation = (weights * store_ad).round(6)
    df["总推广消耗"] = pd.to_numeric(df.get("总推广消耗", 0), errors="coerce").fillna(0.0) + allocation
    df["实时盈亏"] = pd.to_numeric(df.get("实时盈亏", 0), errors="coerce").fillna(0.0) - allocation
    return df


def append_refund_only_rows(
    day_profit: pd.DataFrame,
    orders_df: pd.DataFrame,
    refunds_df: pd.DataFrame,
) -> pd.DataFrame:
    if refunds_df.empty or "退款金额" not in refunds_df.columns:
        return day_profit

    keys = ["商品ID", "商家编码", "SKU规格"]
    existing: set[tuple[str, str, str]] = set()
    source = day_profit if not day_profit.empty else orders_df
    for _, row in source.iterrows():
        existing.add(tuple(text(row.get(key)) for key in keys))

    grouped = (
        refunds_df.groupby(keys, dropna=False, as_index=False)
        .agg(
            商品名称=("商品名称", "last"),
            退款金额=("退款金额", "sum"),
        )
    )

    rows = []
    for _, row in grouped.iterrows():
        key = tuple(text(row.get(col)) for col in keys)
        refund_amount = float(row.get("退款金额") or 0)
        if refund_amount <= 0 or key in existing:
            continue
        rows.append(
            {
                "店铺": SHOP_NAME,
                "商品ID": key[0],
                "商家编码": key[1],
                "SKU规格": key[2],
                "商品名称": text(row.get("商品名称")),
                "支付金额": 0.0,
                "用户实付金额": 0.0,
                "平台补贴金额": 0.0,
                "收入取值口径": "refund_only",
                "SKU订单数": 0.0,
                "SKU成交件数": 0.0,
                "退款金额": refund_amount,
                "单件货价": 0.0,
                "快递费": 0.0,
                "货品成本": 0.0,
                "快递成本": 0.0,
                "推商品推广消耗": 0.0,
                "店铺被投推广消耗": 0.0,
                "推广数据日期": "",
                "总推广消耗": 0.0,
                "平台扣点": 0.0,
                "税点": 0.0,
                "平台费用": 0.0,
                "税费": 0.0,
                "实时盈亏": -refund_amount,
                "利润率": 0.0,
                "成本匹配状态": "退款无当日订单",
            }
        )

    if not rows:
        return day_profit
    attrs = dict(day_profit.attrs)
    combined = pd.concat([day_profit, pd.DataFrame(rows)], ignore_index=True, sort=False)
    combined.attrs.update(attrs)
    return combined


def report_rows(day_profit: pd.DataFrame) -> list[list[Any]]:
    rows = []
    for _, row in day_profit.iterrows():
        rows.append(
            [
                text(row.get("商品ID")),
                text(row.get("商品名称")),
                text(row.get("商家编码")),
                text(row.get("SKU规格")),
                round(float(row.get("单件货价") or 0), 2),
                round(float(row.get("SKU成交件数") or 0), 4),
                round(float(row.get("货品成本") or 0), 2),
                round(float(row.get("支付金额") or 0), 2),
                round(float(row.get("退款金额") or 0), 2),
                round(float(row.get("总推广消耗") or 0), 2),
                round(float(row.get("SKU订单数") or 0), 4),
                round(float(row.get("快递成本") or 0), 2),
                round(float(row.get("税费") or 0), 2),
                round(float(row.get("平台费用") or 0), 2),
                round(float(row.get("实时盈亏") or 0), 2),
                text(row.get("成本匹配状态")),
                text(row.get("收入取值口径")),
            ]
        )
    return rows


def write_workbook(day_frames: dict[str, pd.DataFrame], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)

    for day in sorted(day_frames):
        dt = datetime.strptime(day, "%Y-%m-%d")
        sheet = workbook.create_sheet(f"{dt.month}.{dt.day}")
        sheet.append(REPORT_COLUMNS)
        for row in report_rows(day_frames[day]):
            sheet.append(row)
        for column_cells in sheet.columns:
            max_length = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 10), 32)

    temporary = output_path.with_name(f".{output_path.name}.tmp.xlsx")
    workbook.save(temporary)
    workbook.close()
    temporary.replace(output_path)


def promotion_worker(port: int, day: str) -> int:
    promo_page = open_or_navigate_cdp(
        port,
        "compass.jinritemai.com/shop/commodity/product-list",
        "https://compass.jinritemai.com/shop/commodity/product-list",
        3,
    )
    fallback_reason = ""
    error = ""
    promo_df = pd.DataFrame()
    try:
        try:
            promo_df = fetch_product_promotions(promo_page, day)
            if numeric_sum(promo_df, "推广消耗合计") <= 0:
                fallback_reason = "罗盘商品推广为0"
        except Exception as exc:
            fallback_reason = f"罗盘商品推广失败：{exc}"
            promo_df = pd.DataFrame()

        if fallback_reason:
            try:
                fallback_df = fetch_realtime_qianchuan_summary(promo_page, day)
                if numeric_sum(fallback_df, "推广消耗合计") > 0 or promo_df.empty:
                    promo_df = fallback_df
            except Exception as exc:
                error = f"fallback failed={fallback_reason}; {exc}"
        try:
            complete_total = fetch_complete_promotion_total(promo_page, day)
            promo_df = reconcile_promotions_with_total(promo_df, day, complete_total)
        except Exception as exc:
            if error:
                error = f"{error}; complete_total failed={exc}"
            else:
                error = f"complete_total failed={exc}"
    except Exception as exc:
        error = str(exc)
    finally:
        promo_page.close()

    payload = {
        "records": promo_df.to_dict("records") if not promo_df.empty else [],
        "attrs": {
            "ad_balance": promo_df.attrs.get("ad_balance") if hasattr(promo_df, "attrs") else None,
            "fallback_reason": fallback_reason,
            "error": error,
        },
    }
    print("__PROMO_JSON__" + json.dumps(payload, ensure_ascii=False, default=str))
    return 0


def fetch_promotion_with_timeout(port: int, day: str, timeout: int) -> tuple[pd.DataFrame, str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--promotion-worker",
        "--port",
        str(port),
        "--day",
        day,
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return pd.DataFrame(), f"promotion timeout after {timeout}s"

    marker = "__PROMO_JSON__"
    for line in reversed((completed.stdout or "").splitlines()):
        if line.startswith(marker):
            payload = json.loads(line[len(marker) :])
            df = pd.DataFrame(payload.get("records") or [])
            attrs = payload.get("attrs") or {}
            if attrs.get("ad_balance") is not None:
                df.attrs["ad_balance"] = attrs.get("ad_balance")
            message = attrs.get("error") or attrs.get("fallback_reason") or ""
            return df, message

    message = (completed.stderr or completed.stdout or "").strip()
    return pd.DataFrame(), message[-300:] if message else f"promotion worker exit {completed.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9226)
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-09-19")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--day", default="")
    parser.add_argument("--promotion-timeout", type=int, default=75)
    parser.add_argument("--promotion-worker", action="store_true")
    args = parser.parse_args()

    if args.promotion_worker:
        if not args.day:
            raise SystemExit("--day is required for --promotion-worker")
        return promotion_worker(args.port, args.day)

    days = iter_days(args.start, args.end)
    order_page = open_or_navigate_cdp(args.port, "fxg.jinritemai.com", ORDER_PAGE_URL, 3)
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
                f"[订单 {index:02d}/{len(days)}] {day} "
                f"orders={len(orders)} sku_rows={len(orders_df)} "
                f"pay={numeric_sum(orders_df, '支付金额'):.2f}"
            )
    finally:
        order_page.close()

    all_orders = pd.concat(all_order_frames, ignore_index=True) if all_order_frames else pd.DataFrame()
    if not all_orders.empty:
        ensure_sku_cost_workbook(all_orders, args.end)

    refund_page = open_or_navigate_cdp(args.port, "fxg.jinritemai.com", ORDER_PAGE_URL, 3)
    try:
        navigate_and_wait(refund_page, ORDER_PAGE_URL, 8)
        refunds = fetch_refunds_for_range(refund_page, args.start, args.end)
        refunds_df = parse_refunds(refunds)
    finally:
        refund_page.close()
    print(f"[退款] rows={len(refunds_df)} amount={numeric_sum(refunds_df, '退款金额'):.2f}")

    promo_page = open_or_navigate_cdp(
        args.port,
        "compass.jinritemai.com/shop/commodity/product-list",
        "https://compass.jinritemai.com/shop/commodity/product-list",
        3,
    )
    promotion_frames: dict[str, pd.DataFrame] = {}
    try:
        for index, day in enumerate(days, 1):
            fallback_reason = ""
            try:
                promo_df = fetch_product_promotions(promo_page, day)
                if numeric_sum(promo_df, "推广消耗合计") <= 0:
                    fallback_reason = "罗盘商品推广为0"
            except Exception as exc:
                fallback_reason = f"罗盘商品推广失败：{exc}"
                promo_df = pd.DataFrame()

            if fallback_reason:
                try:
                    fallback_df = fetch_realtime_qianchuan_summary(promo_page, day)
                    if numeric_sum(fallback_df, "推广消耗合计") > 0 or promo_df.empty:
                        promo_df = fallback_df
                    print(
                        f"[推广 {index:02d}/{len(days)}] {day} "
                        f"fallback={fallback_reason}"
                    )
                except Exception as exc:
                    print(
                        f"[推广 {index:02d}/{len(days)}] {day} "
                        f"fallback failed={fallback_reason}; {exc}"
                    )
            try:
                complete_total = fetch_complete_promotion_total(promo_page, day)
                before_total = numeric_sum(promo_df, "推广消耗合计")
                promo_df = reconcile_promotions_with_total(promo_df, day, complete_total)
                after_total = numeric_sum(promo_df, "推广消耗合计")
                if after_total > before_total + 0.01:
                    print(
                        f"[推广 {index:02d}/{len(days)}] {day} "
                        f"完整总额={complete_total:.2f} 补差={after_total - before_total:.2f}"
                    )
            except Exception as exc:
                print(
                    f"[推广 {index:02d}/{len(days)}] {day} "
                    f"完整总额失败：{exc}"
                )
            promotion_frames[day] = promo_df
            print(
                f"[推广 {index:02d}/{len(days)}] {day} "
                f"rows={len(promo_df)} total={numeric_sum(promo_df, '推广消耗合计'):.2f}"
            )
            time.sleep(0.05)
    finally:
        promo_page.close()

    day_frames: dict[str, pd.DataFrame] = {}
    checks = []
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
                "待补成本行": int((profit_df.get("成本匹配状态", pd.Series(dtype=str)).astype(str) != "已匹配").sum()) if not profit_df.empty else 0,
            }
        )

    output_path = Path(args.output)
    write_workbook(day_frames, output_path)
    checks_df = pd.DataFrame(checks)
    checks_path = output_path.with_name(output_path.stem + "_回填校验.csv")
    checks_df.to_csv(checks_path, index=False, encoding="utf-8-sig")
    print(f"OUTPUT={output_path.resolve()}")
    print(f"CHECKS={checks_path.resolve()}")
    print(
        "SUMMARY",
        f"days={len(days)}",
        f"pay={checks_df['支付金额'].sum():.2f}",
        f"refund={checks_df['退款金额'].sum():.2f}",
        f"ad={checks_df['推广花费'].sum():.2f}",
        f"profit={checks_df['单品结余'].sum():.2f}",
        f"missing_cost_rows={int(checks_df['待补成本行'].sum())}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
