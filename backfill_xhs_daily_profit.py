# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from backfill_douyin_daily_profit import iter_days, numeric_sum, write_workbook
from xiaohongshu_profit_crawler import (
    ARK_HOME_URL,
    ARK_AFTERSALE_URL,
    ARK_ORDER_URL,
    QIANFAN_PROMOTION_URL,
    SHOP_DIR,
    SHOP_NAME,
    browser_fetch_json,
    build_profit,
    day_ms_bounds,
    ensure_sku_cost_workbook,
    fetch_orders,
    fetch_promotions,
    fetch_realtime_items,
    normalize_epoch_ms,
    open_or_navigate,
    parse_orders,
    parse_refunds,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_PATH = DATA_DIR / "2026年盲盒千帆日报表.xlsx"


def _product_only_promotions(promotions_df: pd.DataFrame) -> pd.DataFrame:
    """Keep historical product-level promotion spend and drop today's balance remainder."""
    if promotions_df.empty:
        return promotions_df
    df = promotions_df.copy()
    if "商品ID" in df.columns:
        df = df[df["商品ID"].fillna("").astype(str).str.strip().ne("")].copy()
    if "推商品推广消耗" in df.columns:
        df["推商品推广消耗"] = pd.to_numeric(df["推商品推广消耗"], errors="coerce").fillna(0.0)
        df = df[df["推商品推广消耗"] > 0].copy()
    if "店铺被投推广消耗" in df.columns:
        df["店铺被投推广消耗"] = 0.0
    if "推广消耗合计" in df.columns and "推商品推广消耗" in df.columns:
        df["推广消耗合计"] = df["推商品推广消耗"]
    return df


def retry_call(label: str, func, *args, attempts: int = 4, delay: float = 3.0, **kwargs):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            print(f"[重试] {label} 第{attempt}/{attempts}次失败：{exc}", flush=True)
            time.sleep(delay * attempt)
    raise RuntimeError(f"{label} 连续失败：{last_error}") from last_error


COL_PRODUCT_ID = "\u5546\u54c1ID"
COL_SKU_ORDER_ID = "SKU\u8ba2\u5355\u53f7"
COL_MERCHANT_CODE = "\u5546\u5bb6\u7f16\u7801"
COL_SKU_SPEC = "SKU\u89c4\u683c"


def _clean_key(value) -> str:
    if value is None:
        return ""
    if pd.isna(value):
        return ""
    return str(value).strip()


def build_refund_product_maps(merged_orders: pd.DataFrame) -> tuple[dict[str, str], dict[tuple[str, str], str], dict[str, str]]:
    sku_id_map: dict[str, str] = {}
    code_spec_map: dict[tuple[str, str], str] = {}
    code_map: dict[str, str] = {}
    if merged_orders.empty:
        return sku_id_map, code_spec_map, code_map
    needed = [COL_PRODUCT_ID, COL_SKU_ORDER_ID, COL_MERCHANT_CODE, COL_SKU_SPEC]
    for col in needed:
        if col not in merged_orders.columns:
            merged_orders[col] = ""
    for _, row in merged_orders.iterrows():
        product_id = _clean_key(row.get(COL_PRODUCT_ID))
        if not product_id:
            continue
        sku_id = _clean_key(row.get(COL_SKU_ORDER_ID))
        code = _clean_key(row.get(COL_MERCHANT_CODE))
        spec = _clean_key(row.get(COL_SKU_SPEC))
        if sku_id:
            sku_id_map.setdefault(sku_id, product_id)
        if code and spec:
            code_spec_map.setdefault((code, spec), product_id)
        if code:
            code_map.setdefault(code, product_id)
    return sku_id_map, code_spec_map, code_map


def enrich_refund_product_ids(
    refunds_df: pd.DataFrame,
    sku_id_map: dict[str, str],
    code_spec_map: dict[tuple[str, str], str],
    code_map: dict[str, str],
) -> pd.DataFrame:
    if refunds_df.empty or COL_PRODUCT_ID not in refunds_df.columns:
        return refunds_df
    df = refunds_df.copy()
    for col in [COL_PRODUCT_ID, COL_SKU_ORDER_ID, COL_MERCHANT_CODE, COL_SKU_SPEC]:
        if col not in df.columns:
            df[col] = ""
    for idx, row in df.iterrows():
        if _clean_key(row.get(COL_PRODUCT_ID)):
            continue
        sku_id = _clean_key(row.get(COL_SKU_ORDER_ID))
        code = _clean_key(row.get(COL_MERCHANT_CODE))
        spec = _clean_key(row.get(COL_SKU_SPEC))
        product_id = (
            sku_id_map.get(sku_id)
            or code_spec_map.get((code, spec))
            or code_map.get(code)
            or ""
        )
        if product_id:
            df.at[idx, COL_PRODUCT_ID] = product_id
    return df


def fetch_success_refunds_range(
    page,
    start_day: str,
    end_day: str,
    page_size: int = 200,
    max_pages: int = 60,
) -> list[dict]:
    start_ms, _ = day_ms_bounds(start_day)
    _, end_ms = day_ms_bounds(end_day)
    rows: list[dict] = []
    for page_no in range(1, max_pages + 1):
        data = browser_fetch_json(
            page,
            ARK_AFTERSALE_URL,
            params={
                "page": page_no,
                "number": page_size,
                "pageSize": page_size,
                "size": page_size,
                "status_in": "4",
            },
        )
        payload = data.get("data") or {}
        items = payload.get("after_sales") or []
        crossed_start = False
        for item in items:
            refund_at = normalize_epoch_ms(
                item.get("refund_ok_time")
                or item.get("refund_time")
                or item.get("time")
                or item.get("update_at")
                or item.get("updated_at")
            )
            if refund_at < start_ms:
                crossed_start = True
                continue
            if refund_at <= end_ms and str(item.get("status")) == "4":
                rows.append(item)
        if crossed_start or not items or len(items) < page_size:
            break
    return rows


def append_refund_only_rows(profit_df: pd.DataFrame, refunds_df: pd.DataFrame) -> pd.DataFrame:
    if refunds_df.empty:
        return profit_df
    if "商品ID" not in refunds_df.columns or "退款金额" not in refunds_df.columns:
        return profit_df

    existing_keys: set[tuple[str, str, str]] = set()
    if not profit_df.empty:
        for _, row in profit_df.iterrows():
            existing_keys.add(
                (
                    str(row.get("商品ID") or ""),
                    str(row.get("商家编码") or ""),
                    str(row.get("SKU规格") or ""),
                )
            )

    extras = []
    for _, row in refunds_df.iterrows():
        key = (
            str(row.get("商品ID") or ""),
            str(row.get("商家编码") or ""),
            str(row.get("SKU规格") or ""),
        )
        refund_amount = float(pd.to_numeric(pd.Series([row.get("退款金额")]), errors="coerce").fillna(0).iloc[0])
        if refund_amount <= 0 or key in existing_keys:
            continue
        extras.append(
            {
                "店铺": SHOP_NAME,
                "商品ID": key[0],
                "商家编码": key[1],
                "SKU规格": key[2],
                "商品名称": row.get("商品名称") or "仅退款商品",
                "支付金额": 0.0,
                "SKU订单数": 0.0,
                "SKU成交件数": 0.0,
                "单件货价": 0.0,
                "快递费": 0.0,
                "货品成本": 0.0,
                "快递成本": 0.0,
                "成本匹配状态": "仅退款",
                "退款金额": refund_amount,
                "店铺被投推广消耗": 0.0,
                "推商品推广消耗": 0.0,
                "推广后台ROI": 0.0,
                "推广数据日期": row.get("退款成功日期") or "",
                "总推广消耗": 0.0,
                "平台扣点": 0.05,
                "税点": 0.05,
                "平台费用": 0.0,
                "税费": 0.0,
                "实时盈亏": -refund_amount,
                "利润率": 0.0,
                "实际净投产": 0.0,
            }
        )
    if not extras:
        return profit_df
    return pd.concat([profit_df, pd.DataFrame(extras)], ignore_index=True, sort=False)


def save_day_outputs(day: str, profit_df: pd.DataFrame, refunds_df: pd.DataFrame, promotions_df: pd.DataFrame) -> None:
    SHOP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = day.replace("-", "")
    profit_df.to_csv(SHOP_DIR / f"daily_backfill_{stamp}.csv", index=False, encoding="utf-8-sig")
    refunds_df.to_csv(SHOP_DIR / f"xhs_refund_success_{stamp}.csv", index=False, encoding="utf-8-sig")
    promotions_df.to_csv(SHOP_DIR / f"xhs_promotion_{stamp}.csv", index=False, encoding="utf-8-sig")


def run(port: int, start_day: str, end_day: str, output: str) -> int:
    days = iter_days(start_day, end_day)
    order_frames: dict[str, pd.DataFrame] = {}
    refund_frames: dict[str, pd.DataFrame] = {}
    all_orders: list[pd.DataFrame] = []

    ark_page = open_or_navigate(port, "ark.xiaohongshu.com", ARK_ORDER_URL, wait_seconds=8)
    try:
        for index, day in enumerate(days, 1):
            orders = retry_call(f"{day} 订单", fetch_orders, ark_page, day, max_pages=80)
            orders_df = parse_orders(orders)
            order_frames[day] = orders_df
            if not orders_df.empty:
                all_orders.append(orders_df)
            print(
                f"[订单 {index:02d}/{len(days)}] {day} "
                f"orders={len(orders)} sku_rows={len(orders_df)} "
                f"pay={numeric_sum(orders_df, '支付金额'):.2f}",
                flush=True,
            )
            time.sleep(0.05)
        refunds = retry_call(
            f"{start_day}~{end_day} 退款",
            fetch_success_refunds_range,
            ark_page,
            start_day,
            end_day,
            attempts=4,
            delay=5.0,
        )
        all_refunds_df = parse_refunds(refunds)
        for day in days:
            if all_refunds_df.empty or "退款成功日期" not in all_refunds_df.columns:
                refund_frames[day] = pd.DataFrame()
            else:
                refund_frames[day] = all_refunds_df[
                    all_refunds_df["退款成功日期"].astype(str).eq(day)
                ].copy()
        print(
            f"[退款] rows={len(all_refunds_df)} "
            f"refund={numeric_sum(all_refunds_df, '退款金额'):.2f}",
            flush=True,
        )
        try:
            ark_page.call("Page.navigate", {"url": ARK_HOME_URL})
            time.sleep(2)
        except Exception:
            pass
    finally:
        ark_page.close()

    merged_orders = pd.concat(all_orders, ignore_index=True) if all_orders else pd.DataFrame()
    sku_id_map, code_spec_map, code_map = build_refund_product_maps(merged_orders)
    for day in days:
        refund_frames[day] = enrich_refund_product_ids(
            refund_frames.get(day, pd.DataFrame()),
            sku_id_map,
            code_spec_map,
            code_map,
        )
    if not merged_orders.empty:
        added, updated, unique_count = ensure_sku_cost_workbook(merged_orders, end_day)
        print(f"[SKU成本表] 新增={added} 已有/更新={updated} 本轮唯一SKU={unique_count}", flush=True)

    promotion_frames: dict[str, pd.DataFrame] = {}
    promo_page = open_or_navigate(port, "chengfeng.xiaohongshu.com", QIANFAN_PROMOTION_URL, wait_seconds=8)
    try:
        for index, day in enumerate(days, 1):
            try:
                promotions_df, _account_ad_spend, _ad_balance = retry_call(
                    f"{day} 推广",
                    fetch_promotions,
                    promo_page,
                    day,
                    max_pages=30,
                    attempts=3,
                    delay=4.0,
                )
                promotions_df = _product_only_promotions(promotions_df)
            except Exception as exc:
                print(f"[推广 {index:02d}/{len(days)}] {day} failed={exc}", flush=True)
                promotions_df = pd.DataFrame()
            promotion_frames[day] = promotions_df
            print(
                f"[推广 {index:02d}/{len(days)}] {day} "
                f"rows={len(promotions_df)} ad={numeric_sum(promotions_df, '推广消耗合计'):.2f}",
                flush=True,
            )
            time.sleep(0.05)
    finally:
        promo_page.close()

    day_frames: dict[str, pd.DataFrame] = {}
    checks = []
    for day in days:
        orders_df = order_frames.get(day, pd.DataFrame())
        refunds_df = refund_frames.get(day, pd.DataFrame())
        promotions_df = promotion_frames.get(day, pd.DataFrame())
        profit_df = build_profit(
            orders_df,
            refunds_df,
            promotions_df,
            pd.DataFrame(),
            0.0,
            0.0,
        )
        profit_df = append_refund_only_rows(profit_df, refunds_df)
        day_frames[day] = profit_df
        save_day_outputs(day, profit_df, refunds_df, promotions_df)
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
    parser.add_argument("--port", type=int, default=9227)
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-09-19")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    args = parser.parse_args()
    return run(args.port, args.start, args.end, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
