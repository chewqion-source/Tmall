# -*- coding: utf-8 -*-
"""
国货严选实时抓取适配器。

抓取内容：
- 淘工厂商品实时数据
- 投流托管实时费用
- 今日订单 SKU、商家编码，用于复用统一 sku_cost.xlsx 成本表
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import sys
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
from playwright.async_api import async_playwright


BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / "data"
SHOP_NAME = "国货严选"
SAFE_SHOP_NAME = SHOP_NAME
CDP_PORT = 9225
SUPPLIER_ID = 1000000306959207
PLATFORM_RATE = 0.08
TAX_RATE = 0.05
MARKETING_ESTIMATE_RATE = 0.05
MARKETING_EXEMPT_PRODUCT_IDS = {
    "952900248402",
    "949587977970",
    "954859088828",
    "992853929359",
    "1058126529708",
    "991021966779",
    "977855300916",
}
PAGE_TIMEOUT = 60000

PRODUCT_URL = "https://tgc.tmall.com/ds/page/supplier/product-data?from=menu"
HOSTING_URL = "https://tgc.tmall.com/ds/page/supplier/commercial-hosting-home"
ORDER_URL = "https://tgc.tmall.com/ds/page/supplier/order-manage"


def _num(value, default=0.0):
    if value is None:
        return default
    text = str(value).strip().replace(",", "").replace("¥", "")
    if text.endswith("%"):
        text = text[:-1]
        try:
            return float(text) / 100
        except Exception:
            return default
    try:
        out = float(text)
        if math.isfinite(out):
            return out
    except Exception:
        pass
    return default


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def _shop_dir() -> Path:
    path = DATA_ROOT / SAFE_SHOP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _fetch_json(page, url: str, body: dict | None = None, method: str = "POST"):
    script = """
    async ({url, body, method}) => {
      const options = {
        method,
        credentials: 'include',
        headers: {'content-type': 'application/json'}
      };
      if (body !== null) options.body = JSON.stringify(body);
      const resp = await fetch(url, options);
      const text = await resp.text();
      let data = null;
      try { data = JSON.parse(text); } catch (e) {}
      return {ok: resp.ok, status: resp.status, data, text};
    }
    """
    result = await page.evaluate(script, {"url": url, "body": body, "method": method})
    if not result.get("ok"):
        raise RuntimeError(f"接口失败 {url}: HTTP {result.get('status')}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"接口返回不是 JSON：{url}")
    if data.get("success") is False:
        raise RuntimeError(f"接口返回失败 {url}: {data.get('errorMessage') or data.get('errorCode')}")
    return data


async def fetch_products(page) -> pd.DataFrame:
    await page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(1500)

    body = {
        "supplierIdSec": SUPPLIER_ID,
        "pageIndex": 1,
        "pageSize": 100,
        "dimension": "real_time",
        "inBlackTagPeriod": False,
        "trend": True,
        "tagCodePath": ["ALL_ITEMS"],
    }
    payload = await _fetch_json(page, "/ds/api/v1/product-data/queryItemFigure", body)
    data = payload.get("data") or {}
    rows = []

    for item in data.get("itemFigureModelList") or []:
        values = item.get("figureShowValMap") or {}

        def val(key):
            return _num((values.get(key) or {}).get("figureVal"))

        rows.append({
            "商品ID": _text(item.get("itemId")),
            "商品名称": _text(item.get("title") or item.get("itemTitle") or item.get("itemName")),
            "商品货号": "",
            "支付件数": val("pay_qty_cnt"),
            "支付金额": val("pay_ord_amt"),
            "支付买家数": val("pay_byr_cnt"),
            "商品访客": val("iuv"),
            "支付转化率": val("ipv_cvr"),
            "加购件数": 0,
        })

    return pd.DataFrame(rows)


async def fetch_hosting(page) -> pd.DataFrame:
    await page.goto(HOSTING_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(1500)

    today = datetime.now().strftime("%Y%m%d")
    body = {
        "fLevel": "all",
        "startDate": today,
        "endDate": today,
        "pageNo": 1,
        "pageSize": 100,
        "itemTagCodeList": [],
        "itemOnline": True,
    }
    payload = await _fetch_json(page, "/ds/api/v1/hosting/queryEnrolledItemList", body)
    rows = []

    for item in payload.get("data") or []:
        ad_cost = _num(item.get("payOrdCost"))
        gmv = _num(item.get("custodyGmv"))
        roi = _num(item.get("roi"))
        if roi <= 0 and ad_cost > 0:
            roi = gmv / ad_cost
        rows.append({
            "商品ID": _text(item.get("itemId")),
            "智能托管消耗": 0.0,
            "全站推广消耗": ad_cost,
            "全站推广成交金额": gmv,
            "全站推广点击": _num(item.get("clickCnt")),
            "全站推广ROI": roi,
            "推广后台ROI": roi,
            "投流托管佣金费用": _num(item.get("trCost")),
            "投流托管加码费用": _num(item.get("trChargeCost")),
        })

    return pd.DataFrame(rows)


def _order_time_ms(order: dict) -> int:
    return int(_num(order.get("sourceTradeGmtCreate"), 0))


def _sku_text(sub_order: dict) -> str:
    parts = []
    for attr in sub_order.get("orderSkuAttrVOs") or []:
        name = _text(attr.get("attrType"))
        value = _text(attr.get("attrValue"))
        if name and value:
            parts.append(f"{name}: {value}")
        elif value:
            parts.append(value)
    return "；".join(parts)


async def fetch_today_order_skus(page) -> list[dict]:
    await page.goto(ORDER_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(1500)

    today_start = int(datetime.combine(datetime.now().date(), time.min).timestamp() * 1000)
    tomorrow_start = int(datetime.combine(datetime.now().date(), time.max).timestamp() * 1000) + 1
    page_no = 1
    page_size = 100
    rows = []

    while page_no <= 30:
        url = f"/ds/api/v2/orderManagement/orderList?status=ALL&pageSize={page_size}&pageNo={page_no}"
        payload = await _fetch_json(page, url, None, method="GET")
        orders = payload.get("data") or []
        if not orders:
            break

        min_seen = None
        for order in orders:
            created_at = _order_time_ms(order)
            if created_at:
                min_seen = created_at if min_seen is None else min(min_seen, created_at)

            if not (today_start <= created_at < tomorrow_start):
                continue

            order_id = _text(order.get("sourceTradeId"))
            for sub in order.get("subOrderModelList") or []:
                attrs = sub.get("attributes") or {}
                item_id = _text(sub.get("auctionId"))
                if not item_id:
                    continue
                rows.append({
                    "order_id": order_id,
                    "item_id": item_id,
                    "quantity": int(_num(sub.get("buyAmount"), 0)),
                    "sku_text": _sku_text(sub),
                    "merchant_code": _text(attrs.get("outerIdSKU")),
                    "trade_snap": "",
                    "title": _text(sub.get("auctionTitle")),
                    "actual_fee": _num(sub.get("actualTotalFee")),
                    "sku_id": _text(attrs.get("skuId")),
                })

        if min_seen is not None and min_seen < today_start:
            break
        page_no += 1

    return rows


def _load_order_cost_module():
    path = BASE_DIR / "order_sku_crawler_v2_5_4.py"
    spec = importlib.util.spec_from_file_location("_guohuo_order_cost", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法载入成本模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_guohuo_order_cost"] = module
    spec.loader.exec_module(module)
    module.SHOP_NAME = SHOP_NAME
    module.DATA_DIR = _shop_dir()
    return module


def write_order_cost_outputs(order_rows: list[dict]) -> tuple[int, int, int]:
    today = datetime.now().strftime("%Y%m%d")
    shop_dir = _shop_dir()
    cost_module = _load_order_cost_module()

    added, updated, unique_sku = cost_module.ensure_sku_cost_workbook(order_rows)
    detail_rows, unmatched_rows, _ = cost_module.calculate_order_sku_costs(order_rows)
    summary_rows = cost_module.build_product_cost_summary(detail_rows)

    cost_module.save_order_cost_detail(shop_dir / f"order_sku_cost_{today}.csv", detail_rows)
    cost_module.save_unmatched_cost_rows(shop_dir / f"SKU成本未匹配_{today}.csv", unmatched_rows)
    cost_module.save_product_cost_summary(shop_dir / f"product_cost_summary_{today}.csv", summary_rows)

    raw = pd.DataFrame(order_rows)
    raw.to_csv(shop_dir / f"order_sku_{today}.csv", index=False, encoding="utf-8-sig")

    return added, updated, unique_sku


def build_latest(products: pd.DataFrame, hosting: pd.DataFrame) -> pd.DataFrame:
    if products.empty:
        raise RuntimeError("国货严选商品实时数据为空")

    result = products.merge(hosting, on="商品ID", how="left")
    money_cols = [
        "智能托管消耗", "全站推广消耗", "全站推广成交金额", "全站推广点击",
        "全站推广ROI", "推广后台ROI", "投流托管佣金费用", "投流托管加码费用",
    ]
    for col in money_cols:
        if col not in result.columns:
            result[col] = 0
        result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0)

    result["关键词推广消耗"] = 0.0
    result["关键词推广成交金额"] = 0.0
    result["关键词推广点击"] = 0.0
    result["关键词推广ROI"] = 0.0
    result["普通全站推广消耗"] = 0.0
    result["总推广消耗"] = result["全站推广消耗"]
    result["是否有全站推广"] = np.where(result["总推广消耗"] > 0, "是", "否")
    result["是否有关键词推广"] = "否"

    result["商品成本"] = 0.0
    result["单件快递费"] = 0.0
    result["平台扣点"] = PLATFORM_RATE
    result["税点"] = TAX_RATE
    result["其他成本"] = 0.0
    result["成本配置状态"] = "待SKU成本整合"

    result["货品成本"] = 0.0
    result["快递成本"] = 0.0
    result["平台费用"] = result["支付金额"] * PLATFORM_RATE
    result["税费"] = result["支付金额"] * TAX_RATE
    exempt_mask = result["商品ID"].astype(str).str.strip().isin(MARKETING_EXEMPT_PRODUCT_IDS)
    result["预估营销托管费用"] = np.where(
        exempt_mask,
        0.0,
        result["支付金额"] * MARKETING_ESTIMATE_RATE,
    )
    result["销售毛利"] = result["支付金额"] - result["平台费用"] - result["税费"]
    result["实时盈亏"] = (
        result["销售毛利"]
        - result["总推广消耗"]
        - result["预估营销托管费用"]
    )
    result["利润率"] = np.where(result["支付金额"] > 0, result["实时盈亏"] / result["支付金额"], 0)
    result["盈亏状态"] = np.where(result["实时盈亏"] > 0, "盈利", np.where(result["实时盈亏"] < 0, "亏损", "持平"))
    result["实际净投产"] = np.where(result["总推广消耗"] > 0, result["支付金额"] / result["总推广消耗"], 0)

    result.insert(0, "店铺", SHOP_NAME)
    result.insert(1, "抓取时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    for col in [
        "支付件数", "支付金额", "支付买家数", "商品访客", "支付转化率", "加购件数",
        "总推广消耗", "商品成本", "单件快递费", "平台费用", "税费",
        "预估营销托管费用", "销售毛利", "实时盈亏", "利润率", "实际净投产",
    ]:
        if col in result.columns:
            result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0).round(4)

    return result


def save_latest(df: pd.DataFrame) -> None:
    shop_dir = _shop_dir()
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest = shop_dir / "latest.csv"
    history = shop_dir / f"商品实时数据_{now}.csv"
    df.to_csv(latest, index=False, encoding="utf-8-sig")
    df.to_csv(history, index=False, encoding="utf-8-sig")


async def async_main() -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        context = browser.contexts[0]
        page = await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            products = await fetch_products(page)
            hosting = await fetch_hosting(page)
            order_rows = await fetch_today_order_skus(page)
        finally:
            await page.close()

    added, updated, unique_sku = write_order_cost_outputs(order_rows)
    latest = build_latest(products, hosting)
    save_latest(latest)

    total_sales = float(latest["支付金额"].sum())
    total_ad = float(latest["总推广消耗"].sum())
    total_est = float(latest["预估营销托管费用"].sum())
    print()
    print("=" * 76)
    print("国货严选抓取完成")
    print("=" * 76)
    print(f"实时商品：{len(latest)} 个")
    print(f"今日订单SKU：{len(order_rows)} 行 / 唯一SKU {unique_sku}")
    print(f"成本表新增SKU：{added}，已有SKU：{updated}")
    print(f"支付金额：RMB {total_sales:.2f}")
    print(f"投流托管费用：RMB {total_ad:.2f}")
    print(f"预估营销托管费用(5%)：RMB {total_est:.2f}")

    return {
        "success": True,
        "shop": SHOP_NAME,
        "products": len(latest),
        "order_sku_rows": len(order_rows),
        "unique_sku": unique_sku,
        "added_sku": added,
        "updated_sku": updated,
    }


def main() -> dict:
    return asyncio.run(async_main())


if __name__ == "__main__":
    main()
