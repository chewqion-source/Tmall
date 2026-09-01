# -*- coding: utf-8 -*-
"""
Xiaohongshu/Qianfan SKU-level realtime profit crawler.

Rules for 盲盒千帆:
- Platform fee: 5%
- Tax fee: 5%
- Refunds: same-day successful refunds only
- Promotion spend: realtime Qianfan product promotion, with unmatched remainder kept as store-level spend
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import websocket

from sku_cost_utils import merge_duplicate_sku_cost_rows, normalize_sku_spec


BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
SKU_COST_FILE = CONFIG_DIR / "sku_cost.xlsx"
SHOP_NAME = "盲盒千帆"
SHOP_DIR = DATA_ROOT / SHOP_NAME
DEFAULT_PORT = 9227
PLATFORM_RATE = 0.05
TAX_RATE = 0.05

ARK_HOME_URL = "https://ark.xiaohongshu.com/app-system/home"
ARK_ORDER_URL = "https://ark.xiaohongshu.com/app-order/order/query"
ARK_ORDER_PAGE_URL = "https://ark.xiaohongshu.com/api/edith/fulfillment/order/page"
ARK_AFTERSALE_URL = "https://ark.xiaohongshu.com/api/edith/after-sales/returns/v3"
ARK_REALTIME_ITEM_URL = "https://ark.xiaohongshu.com/api/edith/business_data/realtime_item_v2"
QIANFAN_PROMOTION_URL = "https://chengfeng.xiaohongshu.com/cf/ad/manage?type=increment"
QIANFAN_BALANCE_URL = "https://chengfeng.xiaohongshu.com/api/wind/advertiser/balance"
QIANFAN_CAMPAIGN_URL = "https://chengfeng.xiaohongshu.com/api/wind/campaign/list"


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        x = float(str(value).replace(",", ""))
        if math.isfinite(x):
            return x
    except Exception:
        pass
    return default


def cents(value: Any) -> float:
    return num(value) / 100.0


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def day_ms_bounds(day: str) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d")
    end = start.replace(hour=23, minute=59, second=59)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def ms_to_day(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


@dataclass
class CdpPage:
    ws: websocket.WebSocket
    next_id: int = 1

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        msg_id = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result") or {}

    def eval_json(self, expression: str, timeout: int = 30) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout * 1000,
            },
        )
        value = (result.get("result") or {}).get("value")
        if isinstance(value, str):
            return json.loads(value)
        return value

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def connect_cdp(port: int, domain: str | None = None) -> CdpPage:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        tabs = json.loads(resp.read().decode("utf-8"))

    candidates = [
        tab
        for tab in tabs
        if tab.get("type") == "page"
        and "devtools" not in text(tab.get("url")).lower()
        and (not domain or domain in text(tab.get("url")))
    ]
    if not candidates:
        raise RuntimeError(f"端口 {port} 没有找到小红书/千帆页面")

    ws_url = candidates[0].get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError(f"端口 {port} 找到页面但没有调试地址")

    ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
    page = CdpPage(ws)
    page.call("Runtime.enable")
    page.call("Page.enable")
    return page


def open_or_navigate(port: int, domain: str, url: str, wait_seconds: int = 6) -> CdpPage:
    try:
        page = connect_cdp(port, domain)
    except RuntimeError:
        page = connect_cdp(port)
    page.call("Page.navigate", {"url": url})
    time.sleep(wait_seconds)
    return page


def browser_fetch_json(
    page: CdpPage,
    url: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    method: str | None = None,
) -> Any:
    full_url = url
    if params:
        full_url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    request_method = method or ("POST" if body is not None else "GET")
    expr = f"""
    (async () => {{
      const res = await fetch({json.dumps(full_url)}, {{
        method: {json.dumps(request_method)},
        credentials: 'include',
        headers: {{
          'accept': 'application/json, text/plain, */*',
          'content-type': 'application/json'
        }},
        body: {json.dumps(json.dumps(body, ensure_ascii=False)) if body is not None else "undefined"}
      }});
      const txt = await res.text();
      return JSON.stringify({{ ok: res.ok, status: res.status, url: res.url, text: txt }});
    }})()
    """
    payload = page.eval_json(expr, timeout=45)
    if not payload or not payload.get("ok"):
        raise RuntimeError(f"小红书接口失败 {full_url}：{(payload or {}).get('text', '')[:300]}")
    try:
        return json.loads(payload.get("text") or "{}")
    except Exception as exc:
        raise RuntimeError(f"小红书接口返回不是 JSON：{(payload or {}).get('text', '')[:300]}") from exc


def fetch_realtime_items(page: CdpPage, page_size: int = 50, max_pages: int = 20) -> pd.DataFrame:
    rows = []
    total = None
    for page_no in range(1, max_pages + 1):
        data = browser_fetch_json(
            page,
            ARK_REALTIME_ITEM_URL,
            params={
                "displayViewType": 2,
                "orderField": "payGmv",
                "sortType": "desc",
                "pageNo": page_no,
                "pageSize": page_size,
                "total": total or 0,
                "filterValue": "",
            },
        )
        payload = data.get("data") or {}
        items = payload.get("items") or []
        total = payload.get("total") or total
        for item in items:
            info = item.get("itemInfo") or {}
            metrics = item.get("data") or {}
            rows.append(
                {
                    "商品ID": text(info.get("itemId") or metrics.get("id")),
                    "商品名称": text(info.get("name")),
                    "支付金额": num(metrics.get("payGmv")),
                    "净支付金额": num(metrics.get("payNetAmt")),
                    "SKU成交件数": num(metrics.get("payGoodsCnt")),
                    "SKU订单数": num(metrics.get("payPkgNum")),
                    "退款金额": num(metrics.get("payRefundAmt") or metrics.get("refundAmt")),
                    "支付退款率": num(metrics.get("payRefundRate")),
                }
            )
        if not items or len(items) < page_size:
            break
        if total and len(rows) >= int(total):
            break
    return pd.DataFrame(rows)


def fetch_orders(page: CdpPage, day: str, page_size: int = 50, max_pages: int = 30) -> list[dict[str, Any]]:
    start_ms, end_ms = day_ms_bounds(day)
    rows: list[dict[str, Any]] = []
    for page_no in range(1, max_pages + 1):
        data = browser_fetch_json(
            page,
            ARK_ORDER_PAGE_URL,
            body={
                "page_no": page_no,
                "page_size": page_size,
                "time_range_list": [
                    {
                        "time_type": 2,
                        "start_time": start_ms,
                        "end_time": end_ms,
                    }
                ],
                "order_by": "paid_at",
                "order": "desc",
            },
        )
        payload = data.get("data") or {}
        items = payload.get("packages") or []
        rows.extend(items)
        total = int(payload.get("total") or payload.get("total_count") or 0)
        if not items or len(items) < page_size:
            break
        if total and len(rows) >= total:
            break
    return rows


def fetch_success_refunds(page: CdpPage, day: str, page_size: int = 50, max_pages: int = 20) -> list[dict[str, Any]]:
    start_ms, end_ms = day_ms_bounds(day)
    rows: list[dict[str, Any]] = []
    for page_no in range(1, max_pages + 1):
        data = browser_fetch_json(
            page,
            ARK_AFTERSALE_URL,
            params={
                "page": page_no,
                "number": page_no,
                "pageSize": page_size,
                "size": page_size,
                "status_in": "12",
            },
        )
        payload = data.get("data") or {}
        items = payload.get("after_sales") or []
        for item in items:
            refund_at = int(item.get("refund_ok_time") or item.get("updated_at") or item.get("time") or 0)
            if start_ms <= refund_at <= end_ms and (
                "成功" in text(item.get("status_name"))
                or str(item.get("status")) == "12"
            ):
                rows.append(item)
        if not items or len(items) < page_size:
            break
    return rows


def fetch_promotions(page: CdpPage, day: str, page_size: int = 50, max_pages: int = 10) -> tuple[pd.DataFrame, float, float]:
    balance_data = browser_fetch_json(page, QIANFAN_BALANCE_URL)
    balance = balance_data.get("data") or {}
    account_spend = cents(balance.get("todaySpend") or balance.get("dayToSpendingTotal"))
    account_balance = cents(balance.get("availableBalance"))

    columns = [
        "campaignFilterState",
        "combineAuditStatus",
        "newCustomerFlag",
        "diagnosisResult",
        "constraintValue",
        "campaignDayBudget",
        "campaignCreateTime",
        "fee",
        "allDealOrderNum1d",
        "allDealOrderGmv1d",
        "allRoi1d",
        "allDealOrderGmv7d",
        "allDealOrderNum7d",
        "allRoi7d",
        "impression",
        "click",
        "ctr",
    ]
    rows = []
    for page_no in range(1, max_pages + 1):
        data = browser_fetch_json(
            page,
            QIANFAN_CAMPAIGN_URL,
            body={
                "page": {"pageIndex": page_no, "pageSize": page_size},
                "columns": columns,
                "marketingTargetList": [3],
                "startTime": day,
                "endTime": day,
                "creationType": 3,
            },
        )
        payload = data.get("data") or {}
        for item in payload.get("dataList") or []:
            metrics_raw = item.get("dataValueJson") or "{}"
            try:
                metrics = json.loads(metrics_raw) if isinstance(metrics_raw, str) else metrics_raw
            except Exception:
                metrics = {}
            spend = num(metrics.get("fee"))
            if spend <= 0:
                continue
            rows.append(
                {
                    "推广数据日期": day,
                    "商品ID": text(item.get("ecomBizSpuId")),
                    "商品名称": text(item.get("ecomBizSpuName") or item.get("campaignName")),
                    "罗盘支付金额": num(metrics.get("allDealOrderGmv1d")),
                    "店铺被投推广消耗": 0.0,
                    "推商品推广消耗": spend,
                    "推广消耗合计": spend,
                    "推广数据口径": "小红书千帆全域商品推广实时",
                    "推广更新时间": datetime.now().strftime("%m-%d %H:%M"),
                    "推广后台ROI": num(metrics.get("allRoi7d") or metrics.get("allRoi1d")),
                }
            )
        total_page = int(payload.get("totalPage") or 0)
        if total_page and page_no >= total_page:
            break
        if not payload.get("dataList"):
            break

    df = pd.DataFrame(rows)
    product_spend = float(df["推商品推广消耗"].sum()) if not df.empty else 0.0
    remainder = max(account_spend - product_spend, 0.0)
    if remainder > 0.01:
        df = pd.concat(
            [
                df,
                pd.DataFrame(
                    [
                        {
                            "推广数据日期": day,
                            "商品ID": "",
                            "商品名称": "小红书千帆未归属推广消耗",
                            "罗盘支付金额": 0.0,
                            "店铺被投推广消耗": remainder,
                            "推商品推广消耗": 0.0,
                            "推广消耗合计": remainder,
                            "推广数据口径": "小红书千帆账户实时余额差额",
                            "推广更新时间": datetime.now().strftime("%m-%d %H:%M"),
                            "推广后台ROI": 0.0,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
    return df, account_spend, account_balance


def parse_orders(orders: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for package in orders:
        package_id = text(package.get("packageId"))
        order_id = text(package.get("orderId") or package.get("order_id") or package_id)
        paid_at = text(package.get("paidAt") or package.get("orderedAt") or package.get("createdAt"))
        package_seller_receive = num(package.get("actualSellerReceiveAmount"))
        package_skus = package.get("skus") or []
        line_basis_values: list[float] = []

        for sku in package_skus:
            scskus = sku.get("scskus") or []
            if not scskus:
                line_basis_values.append(
                    num(sku.get("skuTotalPaidAmount") or sku.get("paidAmount"))
                    + num(sku.get("skuTotalRedDiscount") or sku.get("redDiscountAmount"))
                )
                continue

            for sc in scskus:
                line_basis_values.append(
                    (
                        num(sc.get("paidAmount"))
                        + num(sc.get("redDiscount"))
                        + num(sc.get("allowance"))
                    )
                    * num(sc.get("quantity"), 1.0)
                )

        package_basis_sum = sum(line_basis_values)
        line_index = 0

        for sku in package_skus:
            product_id = text(sku.get("itemId"))
            product_name = text(sku.get("displayName") or sku.get("skuName"))
            sku_spec = text(sku.get("skuSpecification"))
            sku_quantity = num(sku.get("skuQuantity"), 1.0)
            sku_paid = num(sku.get("skuTotalPaidAmount") or sku.get("paidAmount"))
            scskus = sku.get("scskus") or []
            if not scskus:
                line_basis = line_basis_values[line_index] if line_index < len(line_basis_values) else sku_paid
                line_index += 1
                paid = (
                    package_seller_receive * line_basis / package_basis_sum
                    if package_seller_receive > 0 and package_basis_sum > 0
                    else sku_paid
                )
                rows.append(
                    {
                        "店铺": SHOP_NAME,
                        "订单号": order_id,
                        "SKU订单号": text(sku.get("skuId") or order_id),
                        "付款日期": paid_at[:10],
                        "付款时间": paid_at,
                        "商品ID": product_id,
                        "商品名称": product_name,
                        "商家编码": text(sku.get("scskuCode") or sku.get("skuId")),
                        "SKU规格": sku_spec,
                        "支付金额": paid,
                        "SKU订单数": 1,
                        "SKU成交件数": sku_quantity,
                        "单价": num(sku.get("skuSoldPrice") or sku.get("skuRawPrice")),
                    }
                )
                continue

            sc_paid_sum = sum(
                num(sc.get("paidAmount")) * num(sc.get("quantity"), 1.0)
                for sc in scskus
            ) or sku_paid
            sc_qty_sum = sum(num(sc.get("quantity")) for sc in scskus) or sku_quantity
            for sc in scskus:
                qty = num(sc.get("quantity"), 1.0)
                line_basis = line_basis_values[line_index] if line_index < len(line_basis_values) else 0.0
                line_index += 1
                paid = (
                    package_seller_receive * line_basis / package_basis_sum
                    if package_seller_receive > 0 and package_basis_sum > 0
                    else num(sc.get("paidAmount")) * qty
                )
                if paid <= 0 and sc_paid_sum:
                    paid = sku_paid * qty / sc_qty_sum
                rows.append(
                    {
                        "店铺": SHOP_NAME,
                        "订单号": order_id,
                        "SKU订单号": text(sc.get("skuId") or sku.get("skuId") or order_id),
                        "付款日期": paid_at[:10],
                        "付款时间": paid_at,
                        "商品ID": product_id,
                        "商品名称": product_name or text(sc.get("name")),
                        "商家编码": text(sc.get("scskuCode") or sku.get("scskuCode")),
                        "SKU规格": sku_spec or text(sc.get("specification") or sc.get("skuName") or sc.get("name")),
                        "支付金额": paid,
                        "SKU订单数": 1,
                        "SKU成交件数": qty,
                        "单价": num(sc.get("soldPrice") or sku.get("skuSoldPrice")),
                    }
                )
    return pd.DataFrame(rows)


def parse_refunds(refunds: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in refunds:
        refund_amount_total = num(item.get("refund_fee") or item.get("refunded") or item.get("expected_refund_amount"))
        refund_time = item.get("refund_ok_time") or item.get("time")
        skus = item.get("skus") or []
        if not skus:
            rows.append(
                {
                    "店铺": SHOP_NAME,
                    "售后单号": text(item.get("returns_id")),
                    "订单号": text(item.get("package_id")),
                    "SKU订单号": "",
                    "退款成功日期": ms_to_day(refund_time),
                    "商品ID": "",
                    "商品名称": "",
                    "商家编码": "",
                    "SKU规格": "",
                    "退款金额": refund_amount_total,
                    "售后状态": text(item.get("status_name")),
                }
            )
            continue
        paid_sum = sum(num(sku.get("paid_and_deposit_amount") or sku.get("pay_amount")) for sku in skus)
        for sku in skus:
            product_id = text(sku.get("item_id") or sku.get("itemId"))
            paid = num(sku.get("paid_and_deposit_amount") or sku.get("pay_amount"))
            refund_amount = refund_amount_total * paid / paid_sum if paid_sum else refund_amount_total / len(skus)
            rows.append(
                {
                    "店铺": SHOP_NAME,
                    "售后单号": text(item.get("returns_id")),
                    "订单号": text(item.get("package_id")),
                    "SKU订单号": text(sku.get("sku_id") or sku.get("skuId")),
                    "退款成功日期": ms_to_day(refund_time),
                    "商品ID": product_id,
                    "商品名称": text(sku.get("display_name") or sku.get("name")),
                    "商家编码": text(sku.get("scsku_code") or sku.get("scskuCode")),
                    "SKU规格": text(sku.get("sku_specification") or sku.get("skuSpecification")),
                    "退款金额": refund_amount,
                    "售后状态": text(item.get("status_name")),
                }
            )
    return pd.DataFrame(rows)


def load_cost_table() -> pd.DataFrame:
    candidates = [SKU_COST_FILE, DATA_ROOT / "sku_cost.xlsx"]
    for path in candidates:
        if path.exists():
            return pd.read_excel(path, dtype={"店铺": str, "商品ID": str, "商家编码": str, "SKU规格": str})
    return pd.DataFrame()


def ensure_sku_cost_workbook(orders_df: pd.DataFrame, day: str) -> tuple[int, int, int]:
    if orders_df.empty:
        return 0, 0, 0

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    columns = ["店铺", "商品ID", "商家编码", "SKU规格", "单件货价", "快递费", "备注", "首次发现日期", "最近成交日期"]
    data = pd.read_excel(SKU_COST_FILE, dtype=str) if SKU_COST_FILE.exists() else pd.DataFrame(columns=columns)
    for col in columns:
        if col not in data.columns:
            data[col] = ""
    data = data[columns].copy()
    for col in ["店铺", "商品ID", "商家编码", "SKU规格", "备注", "首次发现日期", "最近成交日期"]:
        data[col] = data[col].fillna("").astype(str).map(text)
    for col in ["单件货价", "快递费"]:
        data[col] = pd.to_numeric(data[col], errors="coerce").round(2)

    def key(row: pd.Series | dict[str, Any]) -> tuple[str, str, str, str]:
        store = text(row.get("店铺") or SHOP_NAME)
        product_id = text(row.get("商品ID"))
        sku_spec = normalize_sku_spec(row.get("SKU规格"))
        merchant = text(row.get("商家编码"))
        if sku_spec:
            return store, product_id, "", sku_spec
        return store, product_id, merchant, ""

    existing = {key(row): idx for idx, row in data.iterrows()}
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for _, row in orders_df.iterrows():
        product_id = text(row.get("商品ID"))
        if not product_id:
            continue
        candidate = {
            "店铺": SHOP_NAME,
            "商品ID": product_id,
            "商家编码": text(row.get("商家编码")),
            "SKU规格": text(row.get("SKU规格")),
            "单件货价": None,
            "快递费": None,
            "备注": "",
            "首次发现日期": day,
            "最近成交日期": day,
        }
        unique[key(candidate)] = candidate

    added = 0
    updated = 0
    rows_to_add = []
    for item_key, row in unique.items():
        if item_key in existing:
            idx = existing[item_key]
            data.at[idx, "最近成交日期"] = day
            if not text(data.at[idx, "商家编码"]) and text(row["商家编码"]):
                data.at[idx, "商家编码"] = row["商家编码"]
            updated += 1
        else:
            rows_to_add.append(row)
            added += 1
    if rows_to_add:
        data = pd.concat([data, pd.DataFrame(rows_to_add)], ignore_index=True, sort=False)

    cleaned = merge_duplicate_sku_cost_rows(data)
    backup = CONFIG_DIR / "backups" / f"sku_cost_before_xhs_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    if SKU_COST_FILE.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(SKU_COST_FILE.read_bytes())
    cleaned.to_excel(SKU_COST_FILE, index=False, sheet_name="SKU成本配置")
    return added, updated, len(unique)


def apply_costs(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["单件货价"] = 0.0
    df["快递费"] = 0.0
    costs = load_cost_table()
    if costs.empty:
        df["成本匹配状态"] = "未找到成本表"
        df["货品成本"] = 0.0
        df["快递成本"] = 0.0
        return df

    for col in ["店铺", "商家编码", "商品ID", "SKU规格"]:
        if col not in costs.columns:
            costs[col] = ""
        costs[col] = costs[col].fillna("").astype(str).map(text)
    for col in ["单件货价", "快递费"]:
        if col not in costs.columns:
            costs[col] = 0.0
        costs[col] = pd.to_numeric(costs[col], errors="coerce").fillna(0.0)

    scoped = costs[costs["店铺"].isin(["", SHOP_NAME])].copy()
    scoped["_sku_key"] = scoped["店铺"] + "|" + scoped["商品ID"] + "|" + scoped["SKU规格"].map(normalize_sku_spec)
    scoped["_code_key"] = scoped["店铺"] + "|" + scoped["商品ID"] + "|" + scoped["商家编码"]
    sku_map = scoped[scoped["SKU规格"].ne("")].drop_duplicates("_sku_key", keep="last").set_index("_sku_key")[["单件货价", "快递费"]].to_dict("index")
    code_map = scoped[scoped["商家编码"].ne("")].drop_duplicates("_code_key", keep="last").set_index("_code_key")[["单件货价", "快递费"]].to_dict("index")

    prices = []
    freights = []
    statuses = []
    for _, row in df.iterrows():
        store = text(row.get("店铺"))
        product_id = text(row.get("商品ID"))
        sku_key = store + "|" + product_id + "|" + normalize_sku_spec(row.get("SKU规格"))
        code_key = store + "|" + product_id + "|" + text(row.get("商家编码"))
        cost = sku_map.get(sku_key) or code_map.get(code_key)
        if cost is None:
            cost = sku_map.get("|" + product_id + "|" + normalize_sku_spec(row.get("SKU规格"))) or code_map.get("|" + product_id + "|" + text(row.get("商家编码")))
        price = num(cost.get("单件货价")) if cost else 0.0
        freight = num(cost.get("快递费")) if cost else 0.0
        prices.append(price)
        freights.append(freight)
        statuses.append("已匹配" if price > 0 else "待补成本")

    df["单件货价"] = prices
    df["快递费"] = freights
    df["成本匹配状态"] = statuses
    df["货品成本"] = df["单件货价"] * df["SKU成交件数"]
    if "订单号" not in df.columns:
        df["快递成本"] = df["快递费"] * df["SKU订单数"]
        return df

    df["快递成本"] = 0.0
    for _order_id, order_rows in df.groupby("订单号", dropna=False):
        idx = order_rows.index
        shipping_values = order_rows["快递费"][order_rows["快递费"] > 0]
        if shipping_values.empty:
            continue

        order_shipping = float(shipping_values.max())
        merchandise_total = float(order_rows["货品成本"].sum())
        quantity_total = float(order_rows["SKU成交件数"].sum())
        if merchandise_total > 0:
            weights = order_rows["货品成本"] / merchandise_total
        elif quantity_total > 0:
            weights = order_rows["SKU成交件数"] / quantity_total
        else:
            weights = pd.Series(1 / len(order_rows), index=idx)

        df.loc[idx, "快递成本"] = (weights * order_shipping).round(4)
    return df


def build_profit(
    orders_df: pd.DataFrame,
    refunds_df: pd.DataFrame,
    promotions_df: pd.DataFrame,
    realtime_items_df: pd.DataFrame,
    account_ad_spend: float,
    ad_balance: float,
) -> pd.DataFrame:
    keys = ["商品ID", "商家编码", "SKU规格"]
    if orders_df.empty:
        grouped = pd.DataFrame(
            columns=["店铺", *keys, "商品名称", "支付金额", "SKU订单数", "SKU成交件数"]
        )
    else:
        grouped = (
            apply_costs(orders_df).groupby(["店铺", *keys, "商品名称"], as_index=False)
            .agg(
                {
                    "支付金额": "sum",
                    "SKU订单数": "sum",
                    "SKU成交件数": "sum",
                    "单件货价": "last",
                    "快递费": "max",
                    "货品成本": "sum",
                    "快递成本": "sum",
                    "成本匹配状态": "last",
                }
            )
        )

    if not refunds_df.empty and not grouped.empty:
        refund_grouped = refunds_df.groupby(keys, as_index=False).agg({"退款金额": "sum"})
        grouped = grouped.merge(refund_grouped, on=keys, how="left")
    elif "退款金额" not in grouped.columns:
        grouped["退款金额"] = 0.0

    if grouped.empty and not realtime_items_df.empty:
        grouped = realtime_items_df.copy()
        grouped["店铺"] = SHOP_NAME
        grouped["商家编码"] = ""
        grouped["SKU规格"] = ""
        grouped["单件货价"] = 0.0
        grouped["快递费"] = 0.0
        grouped["货品成本"] = 0.0
        grouped["快递成本"] = 0.0
        grouped["成本匹配状态"] = "无订单成本"

    grouped["退款金额"] = pd.to_numeric(grouped.get("退款金额", 0), errors="coerce").fillna(0.0)
    for col in ["店铺被投推广消耗", "推商品推广消耗", "推广后台ROI"]:
        grouped[col] = 0.0
    grouped["推广数据日期"] = ""
    if not promotions_df.empty:
        promotions = promotions_df.copy()
        for col in ["店铺被投推广消耗", "推商品推广消耗", "推广后台ROI"]:
            if col in promotions.columns:
                promotions[col] = pd.to_numeric(promotions[col], errors="coerce").fillna(0.0)
        store_ad_total = float(promotions["店铺被投推广消耗"].sum())
        product_ad_map = promotions.groupby("商品ID")["推商品推广消耗"].sum().to_dict()
        product_roi_map = promotions.drop_duplicates("商品ID").set_index("商品ID")["推广后台ROI"].to_dict()
        promo_day_map = promotions.drop_duplicates("商品ID").set_index("商品ID")["推广数据日期"].to_dict()
        product_pay_sum = grouped.groupby("商品ID")["支付金额"].transform("sum").replace(0, pd.NA)
        grouped["推商品推广消耗"] = (
            grouped["商品ID"].map(product_ad_map).fillna(0.0) * grouped["支付金额"] / product_pay_sum
        ).fillna(0.0)
        grouped["推广后台ROI"] = grouped["商品ID"].map(product_roi_map).fillna(0.0)
        grouped["推广数据日期"] = grouped["商品ID"].map(promo_day_map).fillna("")
    else:
        store_ad_total = account_ad_spend

    grouped["店铺被投推广消耗"] = 0.0
    grouped["总推广消耗"] = grouped["推商品推广消耗"]
    grouped["平台扣点"] = PLATFORM_RATE
    grouped["税点"] = TAX_RATE
    grouped["平台费用"] = grouped["支付金额"] * PLATFORM_RATE
    grouped["税费"] = grouped["支付金额"] * TAX_RATE
    grouped["实时盈亏"] = (
        grouped["支付金额"]
        - grouped["退款金额"]
        - grouped["货品成本"]
        - grouped["快递成本"]
        - grouped["总推广消耗"]
        - grouped["平台费用"]
        - grouped["税费"]
    )
    grouped["利润率"] = grouped.apply(lambda row: row["实时盈亏"] / row["支付金额"] if row["支付金额"] else 0.0, axis=1)
    grouped["实际净投产"] = grouped.apply(lambda row: row["支付金额"] / row["总推广消耗"] if row["总推广消耗"] else 0.0, axis=1)
    grouped.attrs["store_ad_cost"] = store_ad_total
    grouped.attrs["product_ad_cost"] = float(grouped["推商品推广消耗"].sum()) if not grouped.empty else 0.0
    grouped.attrs["ad_balance"] = ad_balance
    grouped.attrs["overall_profit"] = float(grouped["实时盈亏"].sum()) - store_ad_total if not grouped.empty else -store_ad_total
    return grouped.sort_values("实时盈亏", ascending=False)


def save_outputs(
    df: pd.DataFrame,
    refunds_df: pd.DataFrame,
    promotions_df: pd.DataFrame,
    realtime_items_df: pd.DataFrame,
    day: str,
) -> None:
    SHOP_DIR.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    money_cols = [
        "支付金额",
        "退款金额",
        "单件货价",
        "快递费",
        "货品成本",
        "快递成本",
        "平台费用",
        "税费",
        "总推广消耗",
        "推商品推广消耗",
        "店铺被投推广消耗",
        "实时盈亏",
    ]
    for col in money_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).round(2)
    for col in ["利润率", "实际净投产", "推广后台ROI"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).round(4)

    df.to_csv(SHOP_DIR / "latest.csv", index=False, encoding="utf-8-sig")
    df.to_csv(SHOP_DIR / f"latest_{day.replace('-', '')}.csv", index=False, encoding="utf-8-sig")
    refunds_df.to_csv(SHOP_DIR / f"xhs_refund_success_{day.replace('-', '')}.csv", index=False, encoding="utf-8-sig")
    promotions_df.to_csv(SHOP_DIR / f"xhs_promotion_{day.replace('-', '')}.csv", index=False, encoding="utf-8-sig")
    realtime_items_df.to_csv(SHOP_DIR / f"xhs_realtime_items_{day.replace('-', '')}.csv", index=False, encoding="utf-8-sig")

    summary = {
        "store": SHOP_NAME,
        "order_day": day,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(df)),
        "pay_amount": round(float(df["支付金额"].sum()) if not df.empty else 0.0, 2),
        "refund_amount": round(float(df["退款金额"].sum()) if not df.empty else 0.0, 2),
        "product_ad_cost": round(float(df.attrs.get("product_ad_cost", 0.0)), 2),
        "store_ad_cost": round(float(df.attrs.get("store_ad_cost", 0.0)), 2),
        "ad_balance": round(float(df.attrs.get("ad_balance", 0.0)), 2),
        "row_profit": round(float(df["实时盈亏"].sum()) if not df.empty else 0.0, 2),
        "overall_profit": round(float(df.attrs.get("overall_profit", 0.0)), 2),
        "promotion_rows": int(len(promotions_df)),
        "promotion_day": day,
    }
    (SHOP_DIR / "latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def run(port: int = DEFAULT_PORT, day: str | None = None) -> pd.DataFrame:
    day = day or datetime.now().strftime("%Y-%m-%d")
    print(f"[{SHOP_NAME}] 连接小红书订单页：127.0.0.1:{port}", flush=True)
    ark_page = open_or_navigate(port, "ark.xiaohongshu.com", ARK_ORDER_URL, wait_seconds=8)
    try:
        print(f"[{SHOP_NAME}] 抓取当日订单SKU...", flush=True)
        orders = fetch_orders(ark_page, day)
        print(f"[{SHOP_NAME}] 订单包裹：{len(orders)}", flush=True)
        print(f"[{SHOP_NAME}] 抓取当日成功退款...", flush=True)
        refunds = fetch_success_refunds(ark_page, day)
        print(f"[{SHOP_NAME}] 成功退款单：{len(refunds)}", flush=True)
        ark_page.call("Page.navigate", {"url": ARK_HOME_URL})
        time.sleep(4)
        print(f"[{SHOP_NAME}] 抓取实时商品概览...", flush=True)
        realtime_items_df = fetch_realtime_items(ark_page)
        print(f"[{SHOP_NAME}] 实时商品：{len(realtime_items_df)}", flush=True)
    finally:
        ark_page.close()

    print(f"[{SHOP_NAME}] 连接千帆推广页：127.0.0.1:{port}", flush=True)
    promotion_page = open_or_navigate(port, "chengfeng.xiaohongshu.com", QIANFAN_PROMOTION_URL, wait_seconds=8)
    try:
        print(f"[{SHOP_NAME}] 抓取千帆实时推广消耗和余额...", flush=True)
        promotions_df, account_ad_spend, ad_balance = fetch_promotions(promotion_page, day)
        print(
            f"[{SHOP_NAME}] 推广记录：{len(promotions_df)}，账户消耗：{account_ad_spend:.2f}元，余额：{ad_balance:.2f}元",
            flush=True,
        )
    finally:
        promotion_page.close()

    print(f"[{SHOP_NAME}] 写入SKU成本维护表并计算盈亏...", flush=True)
    orders_df = parse_orders(orders)
    added, updated, unique_count = ensure_sku_cost_workbook(orders_df, day)
    refunds_df = parse_refunds(refunds)
    result = build_profit(orders_df, refunds_df, promotions_df, realtime_items_df, account_ad_spend, ad_balance)
    result.attrs["sku_cost_added"] = added
    result.attrs["sku_cost_updated"] = updated
    result.attrs["sku_cost_unique"] = unique_count
    save_outputs(result, refunds_df, promotions_df, realtime_items_df, day)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    args = parser.parse_args()

    result = run(port=args.port, day=args.day)
    total_profit = result["实时盈亏"].sum() if not result.empty else 0.0
    total_pay = result["支付金额"].sum() if not result.empty else 0.0
    total_refund = result["退款金额"].sum() if not result.empty else 0.0
    product_ad = float(result.attrs.get("product_ad_cost", 0.0))
    store_ad = float(result.attrs.get("store_ad_cost", 0.0))
    overall_profit = float(result.attrs.get("overall_profit", total_profit))
    print(f"{SHOP_NAME} 小红书 SKU 实时盈亏完成")
    print(f"商品/SKU行数：{len(result)}")
    print(f"成本表新增SKU：{result.attrs.get('sku_cost_added', 0)}")
    print(f"成本表已有SKU：{result.attrs.get('sku_cost_updated', 0)}")
    print(f"支付金额：{total_pay:.2f}元")
    print(f"退款金额：{total_refund:.2f}元")
    print(f"推商品推广：{product_ad:.2f}元")
    print(f"店铺级推广：{store_ad:.2f}元")
    print(f"账户推广余额：{result.attrs.get('ad_balance', 0):.2f}元")
    print(f"SKU行盈亏：{total_profit:.2f}元")
    print(f"店铺整体盈亏：{overall_profit:.2f}元")
    print(f"结果：{SHOP_DIR / 'latest.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
