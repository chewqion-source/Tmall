# -*- coding: utf-8 -*-
"""
Douyin SKU-level realtime profit crawler.

Rules for 盲盒抖店:
- Platform fee: 5%
- Tax fee: 5%
- Refunds: only same-day successful refunds are deducted
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import websocket

from sku_cost_utils import merge_duplicate_sku_cost_rows, normalize_sku_spec


BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
SKU_COST_FILE = CONFIG_DIR / "sku_cost.xlsx"
SHOP_NAME = "盲盒抖店"
SHOP_DIR = DATA_ROOT / SHOP_NAME
DEFAULT_PORT = 9226
PLATFORM_RATE = 0.05
TAX_RATE = 0.05

ORDER_LIST_URL = "https://fxg.jinritemai.com/api/order/searchlist"
AFTERSALE_LIST_URL = "https://fxg.jinritemai.com/after_sale/pc/list"
ORDER_PAGE_URL = "https://fxg.jinritemai.com/ffa/morder/order/list"
PRODUCT_PROMOTION_URL = "https://compass.jinritemai.com/compass_api/shop/product/product/product_list"
QIANCHUAN_AAVID = "1841119329577479"
QIANCHUAN_REALTIME_URL = "https://qianchuan.jinritemai.com/uni-prom/overall"
SETTLEMENT_ANALYSIS_URL = (
    "https://compass.jinritemai.com/shop/settlement-analysis"
)
SETTLEMENT_INDEX_CARD_URL = (
    "https://compass.jinritemai.com/compass_api/shop/common/trade/"
    "income_expense/overview/index_card"
)
PRODUCT_PROMOTION_INDEXES = (
    "receive_amt,pay_amt_exclude_refund,trans_amt,pay_amt,pay_cnt,"
    "ad_costed_amt,ad_cost_ratio,qc_ad_cost,pay_refund_success_amt,"
    "product_show_ucnt,product_click_ucnt,pay_ucnt,net_trans_amt,"
    "product_show_pay_converse_uv_rate,pay_combo_cnt,refund_order_cnt"
)


def cents(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value) / 100.0
    except Exception:
        return 0.0


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isfinite(x):
            return x
    except Exception:
        pass
    return default


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def day_bounds(day: str) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d")
    end = start.replace(hour=23, minute=59, second=59)
    return int(start.timestamp()), int(end.timestamp())


def ts_to_day(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def sku_spec_text(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = text(item.get("name"))
        value = text(item.get("value"))
        if name and value:
            parts.append(f"{name}:{value}")
        elif value:
            parts.append(value)
    return "；".join(parts)


def first_positive_cents(data: dict[str, Any], keys: list[str]) -> tuple[float, str]:
    for key in keys:
        value = cents(data.get(key))
        if value > 0:
            return value, key
    return 0.0, ""


def merchant_income_from_item(item: dict[str, Any], qty: float) -> tuple[float, float, float, str]:
    user_pay = cents(item.get("pay_amount"))
    direct_income, source = first_positive_cents(
        item,
        [
            "merchant_receive_amount",
            "merchant_income_amount",
            "settle_amount",
            "settlement_amount",
            "shop_receive_amount",
            "shop_income_amount",
            "seller_receive_amount",
            "seller_income_amount",
            "estimated_income_amount",
            "confirm_receipt_amount",
            "combo_amount",
        ],
    )

    combo_amount = cents(item.get("combo_amount"))
    if combo_amount > 0:
        combo_total = combo_amount
        if qty > 1 and combo_amount <= user_pay + 0.01:
            combo_total = combo_amount * qty
        if combo_total > direct_income + 0.01:
            direct_income = combo_total
            source = "combo_amount"

    income = direct_income if direct_income > 0 else user_pay
    platform_subsidy = max(income - user_pay, 0.0)
    return income, user_pay, platform_subsidy, source or "pay_amount"


def is_successful_refund(item: dict[str, Any], day: str) -> bool:
    info = item.get("after_sale_info") or {}
    text_part = item.get("text_part") or {}
    status_tag = info.get("after_sale_status_tag") or {}
    status_text = " ".join(
        [
            text(status_tag.get("text")),
            text(text_part.get("after_sale_status_text")),
            text(text_part.get("after_sale_refund_type_text")),
        ]
    )
    status_code = info.get("after_sale_status")
    update_day = ts_to_day(info.get("update_time") or info.get("create_time"))

    if update_day != day:
        return False

    if "退款成功" in status_text:
        return True

    # 抖店样例里 12 对应“同意退款，退款成功”。
    return str(status_code) == "12"


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


def connect_cdp(port: int, url_contains: str | None = None) -> CdpPage:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        tabs = json.loads(resp.read().decode("utf-8"))

    candidates = [
        tab
        for tab in tabs
        if tab.get("type") == "page"
        and "jinritemai.com" in text(tab.get("url"))
        and "devtools" not in text(tab.get("url")).lower()
        and (not url_contains or url_contains in text(tab.get("url")))
    ]
    if not candidates:
        raise RuntimeError(f"端口 {port} 没有找到已登录的抖店页面")

    target = candidates[0]
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError(f"端口 {port} 找到页面但没有调试地址")

    ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
    page = CdpPage(ws)
    page.call("Runtime.enable")
    return page


def open_or_navigate_cdp(
    port: int,
    url_contains: str,
    target_url: str,
    wait_seconds: int = 8,
) -> CdpPage:
    try:
        return connect_cdp(port, url_contains)
    except RuntimeError:
        page = connect_cdp(port, None)
        page.call("Page.enable")
        page.call(
            "Page.navigate",
            {
                "url": target_url,
            },
        )
        time.sleep(wait_seconds)
        return page


def browser_fetch_json(
    page: CdpPage,
    url: str,
    params: dict[str, Any] | None = None,
    method: str = "GET",
) -> Any:
    full_url = url
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        full_url = f"{url}?{query}"

    expr = f"""
    (async () => {{
      const res = await fetch({json.dumps(full_url)}, {{
        method: {json.dumps(method)},
        credentials: 'include',
        headers: {{ 'accept': 'application/json, text/plain, */*' }}
      }});
      const text = await res.text();
      return JSON.stringify({{ ok: res.ok, status: res.status, url: res.url, text }});
    }})()
    """
    last_payload = None
    last_body = ""
    for attempt in range(1, 4):
        payload = page.eval_json(expr)
        last_payload = payload
        if payload and payload.get("ok"):
            body = payload.get("text") or ""
            last_body = body
            try:
                data = json.loads(body)
            except Exception as exc:
                if attempt >= 3:
                    raise RuntimeError(f"抖店接口返回不是 JSON：{body[:300]}") from exc
            else:
                if data or attempt >= 3:
                    return data
        time.sleep(3 * attempt)
    raise RuntimeError(f"抖店接口请求失败：{last_payload or last_body}")


def current_page_tokens(page: CdpPage) -> dict[str, str]:
    expr = """
    (() => {
      const pairs = Object.fromEntries(
        document.cookie
          .split(';')
          .map(x => x.trim())
          .filter(Boolean)
          .map(x => {
            const i = x.indexOf('=');
            return i >= 0 ? [x.slice(0, i), decodeURIComponent(x.slice(i + 1))] : [x, ''];
          })
      );
      return JSON.stringify({
        csrf: pairs.csrf_session_id || '',
        verifyFp: pairs.s_v_web_id || '',
        fp: pairs.s_v_web_id || ''
      });
    })()
    """
    value = page.eval_json(expr)
    return {k: text(v) for k, v in (value or {}).items()}


def fetch_orders(page: CdpPage, day: str, page_size: int = 50, max_pages: int = 30) -> list[dict[str, Any]]:
    start_ts, end_ts = day_bounds(day)
    rows: list[dict[str, Any]] = []

    for page_no in range(max_pages):
        params = {
            "order": "desc",
            "order_by": "create_time",
            "page": page_no,
            "pageSize": page_size,
            "source": "shop_order_view_upgrade",
            "tab": "all",
            "order_status": "all",
            "compact_time[select]": "create_time_start,create_time_end",
        }
        data = browser_fetch_json(page, ORDER_LIST_URL, params)
        items = data.get("data") or []
        if not items:
            break

        stop = False
        for order in items:
            pay_ts = int(order.get("pay_time") or order.get("create_time") or 0)
            if pay_ts > end_ts:
                continue
            if pay_ts < start_ts:
                stop = True
                continue
            rows.append(order)

        if stop:
            break
        if len(items) < page_size:
            break

    return rows


def fetch_success_refunds(page: CdpPage, day: str, page_size: int = 50, max_pages: int = 30) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tokens = current_page_tokens(page)
    for page_no in range(1, max_pages + 1):
        params = {
            "appid": 1,
            "_bid": "ffa_aftersale",
            "aid": 4272,
            "aftersale_platform_source": "fxg",
            "__token": tokens.get("csrf", ""),
            "verifyFp": tokens.get("verifyFp", ""),
            "fp": tokens.get("fp", ""),
            "page": page_no,
            "pageSize": page_size,
        }
        data = browser_fetch_json(page, AFTERSALE_LIST_URL, params, method="POST")
        items = ((data.get("data") or {}).get("items")) or []
        if not items:
            break
        for item in items:
            if is_successful_refund(item, day):
                rows.append(item)
        if len(items) < page_size:
            break
    return rows


def metric_value(cell_info: dict[str, Any], metric: str) -> float:
    node = cell_info.get(metric) or {}
    node = node.get(f"{metric}_index_values") or {}
    index_values = node.get("index_values") or {}
    value = index_values.get("value") or {}
    return cents(value.get("value"))


def product_info_value(cell_info: dict[str, Any], key: str) -> str:
    info = cell_info.get("product_info") or {}
    node = info.get(key) or {}
    value = node.get("value") or {}
    return text(value.get("value_str"))


def parse_money_text(value: str) -> float:
    value = text(value)
    if not value or value in {"-", "--"}:
        return 0.0
    value = (
        value.replace("￥", "")
        .replace("¥", "")
        .replace(",", "")
        .replace("元", "")
        .strip()
    )
    try:
        return float(value)
    except Exception:
        return 0.0


def build_qianchuan_realtime_url(day: str) -> str:
    day_compact = datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d")
    query = {
        "aavid": QIANCHUAN_AAVID,
        "ct": "1",
        "dr": f"{day_compact},{day_compact}",
        "utm_source": "qianchuan-origin-entrance",
        "utm_medium": "doudian-pc",
        "utm_campaign": "top-navigation-qianchuan",
        "utm_term": "tuiguangguanli",
        "umg": "2",
        "uniVideoTab": "2",
        "usbt": "0",
        "dut": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "OverallProm": json.dumps(
            {
                "uniTab": "ad",
                "autoOpenAdDetailRoiEditor": "",
                "showStarTask": "",
                "sAdId": "",
                "sAwemeShowId": "",
                "sPId": "",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    fragment = urllib.parse.urlencode(
        {
            "shop": "",
            "adr": json.dumps({"dateRange": []}, separators=(",", ":")),
            "OverallProm": json.dumps(
                {
                    "ad": json.dumps(
                        {
                            "p": "1",
                            "ps": "10",
                            "sf": "create_time",
                            "st": "desc",
                            "skw": "",
                            "sd": json.dumps({"option": "1"}, separators=(",", ":")),
                            "cost_items": json.dumps(
                                {"option": "1", "value": ""},
                                separators=(",", ":"),
                            ),
                            "value_added_services": json.dumps(
                                {"option": "enabled", "value": ""},
                                separators=(",", ":"),
                            ),
                            "act": "",
                            "asft": "0",
                            "cos": "-1",
                            "cft": "-1",
                        },
                        separators=(",", ":"),
                    ),
                    "bp": json.dumps(
                        {"p": "1", "ps": "10", "sf": "", "st": "desc", "sk": ""},
                        separators=(",", ":"),
                    ),
                    "upAdId": "",
                    "bsbt": "",
                },
                separators=(",", ":"),
            ),
        }
    )
    return f"{QIANCHUAN_REALTIME_URL}?{urllib.parse.urlencode(query)}#{fragment}"


def parse_qianchuan_overall_text(body_text: str) -> tuple[float, str]:
    lines = [line.strip() for line in text(body_text).splitlines() if line.strip()]
    updated_at = ""
    for line in lines:
        match = re.search(r"\d{2}-\d{2}\s+\d{2}:\d{2}更新", line)
        if match:
            updated_at = match.group(0).replace("更新", "")
            break

    summary_line = ""
    for line in lines:
        if re.search(r"共\s*\d+\s*条计划", line) and "\t" in line:
            summary_line = line
            break
    if not summary_line:
        return 0.0, updated_at

    parts = [part.strip() for part in summary_line.split("\t") if part.strip()]
    values = []
    for part in parts:
        if "%" in part:
            values.append(part)
            continue
        candidate = part.replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", candidate):
            values.append(candidate)
    # Summary columns after ROI are fixed on the qianchuan overall page.
    # Index 11 is "整体消耗(元)"; index 10 is "综合成本(元)".
    if len(values) >= 12:
        return num(values[11]), updated_at
    if len(values) >= 11:
        return num(values[10]), updated_at
    return 0.0, updated_at


def parse_qianchuan_product_ad_rows(
    body_text: str,
    day: str,
    updated_at: str,
) -> list[dict[str, Any]]:
    rows = []
    seen: set[tuple[str, float]] = set()
    for match in re.finditer(r"商品ID[:：]?\s*(\d{8,})", text(body_text)):
        product_id = match.group(1)
        block = text(body_text)[match.start() : match.start() + 1200]
        spend = 0.0
        spend_match = re.search(r"整体消耗(?:\(元\))?\s*[\n\t ]+(-?\d+(?:,\d{3})*(?:\.\d+)?)", block)
        if spend_match:
            spend = parse_money_text(spend_match.group(1))
        if spend <= 0:
            numeric_values = re.findall(r"(?<![\d.])-?\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])", block)
            if len(numeric_values) >= 2:
                spend = parse_money_text(numeric_values[-2])
        key = (product_id, spend)
        if spend <= 0 or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "推广数据日期": day,
                "商品ID": product_id,
                "商品名称": "千川单品实时推广消耗",
                "罗盘支付金额": 0.0,
                "店铺被投推广消耗": 0.0,
                "推商品推广消耗": spend,
                "推广消耗合计": spend,
                "推广数据口径": "千川单品实时",
                "推广更新时间": updated_at or datetime.now().strftime("%m-%d %H:%M"),
            }
        )
    return rows


def fetch_realtime_qianchuan_summary(page: CdpPage, day: str) -> pd.DataFrame:
    page.call("Page.enable")
    page.call(
        "Page.navigate",
        {
            "url": build_qianchuan_realtime_url(day),
        },
    )
    time.sleep(20)
    payload = page.eval_json(
        r"""
        JSON.stringify((() => {
          return {
            url: location.href,
            title: document.title,
            body_text: document.body.innerText || ''
          };
        })())
        """
    )
    body_text = payload.get("body_text", "")
    overall_ad, updated_at = parse_qianchuan_overall_text(body_text)
    product_rows = parse_qianchuan_product_ad_rows(body_text, day, updated_at)
    product_ad_total = sum(num(row.get("推商品推广消耗")) for row in product_rows)
    store_ad = max(overall_ad - product_ad_total, 0.0)
    rows = list(product_rows)
    rows.append(
        {
            "推广数据日期": day,
            "商品ID": "",
            "商品名称": "千川全域投放实时整体消耗",
            "罗盘支付金额": 0.0,
            "店铺被投推广消耗": store_ad,
            "推商品推广消耗": 0.0,
            "推广消耗合计": store_ad,
            "推广数据口径": "千川全域投放实时",
            "推广更新时间": updated_at or datetime.now().strftime("%m-%d %H:%M"),
        }
    )
    return pd.DataFrame(
        rows
    )


def settlement_metric_cents(data: dict[str, Any], metric: str) -> float:
    try:
        card = (
            data.get("data", {})
            .get("module_data", {})
            .get("homepage_core_index", {})
            .get("compass_general_multi_index_card_value", {})
            .get("data", [])
        )
        if not card:
            return 0.0
        node = card[0].get(metric, {})
        value = node.get("index_value", {}).get("value", {}).get("value")
        if value is None:
            value = node.get("value", {}).get("value")
        return cents(value)
    except Exception:
        return 0.0


def fetch_realtime_settlement_summary(page: CdpPage, day: str) -> pd.DataFrame:
    page.call("Page.enable")
    page.call(
        "Page.navigate",
        {
            "url": SETTLEMENT_ANALYSIS_URL,
        },
    )
    time.sleep(10)
    day_text = datetime.strptime(day, "%Y-%m-%d").strftime("%Y/%m/%d")
    params = {
        "date_type": "1",
        "end_date": f"{day_text} 00:00:00",
        "begin_date": f"{day_text} 00:00:00",
        "activity_id": "",
        "is_activity": "false",
        "index_selected": (
            "settlement_amt_pay_time,ad_costed_amt,"
            "ad_costed_expense_ratio_with_refund,"
            "overall_ad_costed_expense_ratio_without_refund,"
            "shop_targeted_efficiency_ratio_without_refund,"
            "ad_costed_efficiency_ratio_without_refund"
        ),
        "operate_type": "0",
        "content_type": "0",
        "traffic_channel": "1",
    }
    data = browser_fetch_json(page, SETTLEMENT_INDEX_CARD_URL, params)
    if data.get("st") not in (0, "0", None):
        raise RuntimeError(f"抖店收支投放实时接口失败：{data.get('msg') or data.get('st')}")
    store_ad = settlement_metric_cents(data, "ad_costed_amt")
    return pd.DataFrame(
        [
            {
                "推广数据日期": day,
                "商品ID": "",
                "商品名称": "收支投放分析实时店铺被投消耗",
                "罗盘支付金额": 0.0,
                "店铺被投推广消耗": store_ad,
                "推商品推广消耗": 0.0,
                "推广消耗合计": store_ad,
                "推广数据口径": "收支投放分析实时",
                "推广更新时间": datetime.now().strftime("%m-%d %H:%M"),
            }
        ]
    )


def fetch_product_promotions(
    page: CdpPage,
    promotion_day: str,
    page_size: int = 10,
    max_pages: int = 20,
) -> pd.DataFrame:
    rows = []
    today = datetime.now().strftime("%Y-%m-%d")
    date_type = "1" if promotion_day == today else "20"
    day_text = datetime.strptime(promotion_day, "%Y-%m-%d").strftime("%Y/%m/%d")
    for page_no in range(1, max_pages + 1):
        params = {
            "date_type": date_type,
            "begin_date": f"{day_text} 00:00:00",
            "end_date": f"{day_text} 00:00:00",
            "is_activity": "false",
            "activity_id": "",
            "key_word": "",
            "index_selected": PRODUCT_PROMOTION_INDEXES,
            "sale_type": "1",
            "content_type": "1",
            "cate_ids": "",
            "cate_ids_original": "0",
            "product_tab": "0",
            "only_abnormal": "false",
            "only_drop_gmv": "false",
            "only_drop_product_show": "false",
            "use_customize_gmv": "false",
            "use_customize_product_show": "false",
            "abnormal_threshold_gmv": "0",
            "abnormal_threshold_product_show": "0",
            "new_version": "true",
            "page_no": page_no,
            "page_size": page_size,
        }
        data = browser_fetch_json(page, PRODUCT_PROMOTION_URL, params)
        if data.get("st") not in (0, "0", None):
            raise RuntimeError(f"抖店罗盘推广接口失败：{data.get('msg') or data.get('st')}")
        items = data.get("data") or []
        for item in items:
            cell_info = item.get("cell_info") or {}
            store_ad = metric_value(cell_info, "ad_costed_amt")
            product_ad = metric_value(cell_info, "qc_ad_cost")
            rows.append(
                {
                    "推广数据日期": promotion_day,
                    "商品ID": product_info_value(cell_info, "product_id_value"),
                    "商品名称": product_info_value(cell_info, "product_name_value"),
                    "罗盘支付金额": metric_value(cell_info, "pay_amt"),
                    "店铺被投推广消耗": store_ad,
                    "推商品推广消耗": product_ad,
                    "推广消耗合计": store_ad + product_ad,
                    "推广数据口径": "实时" if date_type == "1" else "历史",
                }
            )
        page_result = data.get("page_result") or {}
        total = int(page_result.get("total") or 0)
        if len(rows) >= total or len(items) < page_size:
            break
    return pd.DataFrame(rows)


def parse_orders(orders: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for order in orders:
        shop_order_id = text(order.get("shop_order_id"))
        pay_time = order.get("pay_time") or order.get("create_time")
        for item in order.get("product_item") or []:
            qty = num(item.get("combo_num"))
            merchant_income, user_pay, platform_subsidy, income_source = (
                merchant_income_from_item(item, qty)
            )
            rows.append(
                {
                    "店铺": SHOP_NAME,
                    "订单号": shop_order_id,
                    "SKU订单号": text(item.get("item_order_id") or item.get("sku_order_id") or shop_order_id),
                    "付款日期": ts_to_day(pay_time),
                    "付款时间": datetime.fromtimestamp(int(pay_time)).strftime("%Y-%m-%d %H:%M:%S") if pay_time else "",
                    "商品ID": text(item.get("product_id")),
                    "商品名称": text(item.get("product_name")),
                    "商家编码": text(item.get("merchant_sku_code")),
                    "SKU规格": sku_spec_text(item.get("sku_spec")),
                    "支付金额": merchant_income,
                    "用户实付金额": user_pay,
                    "平台补贴金额": platform_subsidy,
                    "收入取值口径": income_source,
                    "SKU订单数": 1,
                    "SKU成交件数": qty,
                    "单价": cents(item.get("combo_amount")),
                }
            )
    return pd.DataFrame(rows)


def parse_refunds(refunds: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in refunds:
        info = item.get("after_sale_info") or {}
        order_info = item.get("order_info") or {}
        text_part = item.get("text_part") or {}
        related = order_info.get("related_order_info") or []
        if not related:
            related = [{}]
        for related_item in related:
            refund_amount = cents(related_item.get("refund_amount"))
            if refund_amount <= 0:
                refund_amount = cents(info.get("refund_amount"))
            rows.append(
                {
                    "店铺": SHOP_NAME,
                    "售后单号": text(info.get("after_sale_id")),
                    "订单号": text(order_info.get("shop_order_id") or info.get("related_id")),
                    "SKU订单号": text(related_item.get("sku_order_id")),
                    "退款成功日期": ts_to_day(info.get("update_time") or info.get("create_time")),
                    "商品ID": text(related_item.get("product_id")),
                    "商品名称": text(related_item.get("product_name")),
                    "商家编码": text(related_item.get("shop_spec_code")),
                    "SKU规格": sku_spec_text(related_item.get("sku_spec")),
                    "退款金额": refund_amount,
                    "退运费": cents(related_item.get("refund_post_amount")),
                    "售后状态": text(text_part.get("after_sale_status_text"))
                    or text((info.get("after_sale_status_tag") or {}).get("text")),
                }
            )
    return pd.DataFrame(rows)


def load_cost_table() -> pd.DataFrame:
    candidates = [
        SKU_COST_FILE,
        DATA_ROOT / "sku_cost.xlsx",
    ]
    for path in candidates:
        if not path.exists():
            continue
        if path.suffix.lower() == ".xlsx":
            return pd.read_excel(path)
        return pd.read_csv(path, encoding="utf-8-sig")
    return pd.DataFrame()


def ensure_sku_cost_workbook(orders_df: pd.DataFrame, day: str) -> tuple[int, int, int]:
    if orders_df.empty:
        return 0, 0, 0

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    columns = [
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

    if SKU_COST_FILE.exists():
        data = pd.read_excel(
            SKU_COST_FILE,
            dtype={
                "店铺": str,
                "商品ID": str,
                "商家编码": str,
                "SKU规格": str,
            },
        )
    else:
        data = pd.DataFrame(columns=columns)

    for col in columns:
        if col not in data.columns:
            data[col] = ""
    data = data[columns].copy()

    for col in ["店铺", "商品ID", "商家编码", "SKU规格", "备注", "首次发现日期", "最近成交日期"]:
        data[col] = data[col].fillna("").astype(str).map(text)
    for col in ["单件货价", "快递费"]:
        data[col] = pd.to_numeric(data[col], errors="coerce").round(2)

    def key_from_row(row: pd.Series | dict[str, Any]) -> tuple[str, str, str, str]:
        store = text(row.get("店铺") or SHOP_NAME)
        product_id = text(row.get("商品ID"))
        sku = normalize_sku_spec(row.get("SKU规格"))
        merchant = text(row.get("商家编码"))
        if sku:
            return store, product_id, "", sku
        return store, product_id, merchant, ""

    existing = {key_from_row(row): idx for idx, row in data.iterrows()}
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
        unique[key_from_row(candidate)] = candidate

    added = 0
    updated = 0
    rows_to_add = []

    for key, row in unique.items():
        if key in existing:
            idx = existing[key]
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
    backup = CONFIG_DIR / "backups" / f"sku_cost_before_douyin_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
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
    else:
        costs = costs.copy()
        for col in ["店铺", "商家编码", "商品ID", "SKU规格"]:
            if col not in costs.columns:
                costs[col] = ""
            costs[col] = costs[col].map(text)
        for col in ["单件货价", "快递费"]:
            if col not in costs.columns:
                costs[col] = 0.0
            costs[col] = pd.to_numeric(costs[col], errors="coerce").fillna(0.0)

        scoped = costs[costs["店铺"].isin(["", SHOP_NAME])].copy()
        scoped["_sku_key"] = scoped["店铺"] + "|" + scoped["商品ID"] + "|" + scoped["SKU规格"].map(normalize_sku_spec)
        scoped["_code_key"] = scoped["店铺"] + "|" + scoped["商品ID"] + "|" + scoped["商家编码"]
        sku_map = (
            scoped[scoped["SKU规格"].ne("")]
            .drop_duplicates(subset=["_sku_key"], keep="last")
            .set_index("_sku_key")[["单件货价", "快递费"]]
            .to_dict("index")
        )
        code_map = (
            scoped[scoped["商家编码"].ne("")]
            .drop_duplicates(subset=["_code_key"], keep="last")
            .set_index("_code_key")[["单件货价", "快递费"]]
            .to_dict("index")
        )

        prices = []
        freights = []
        statuses = []
        for _, row in df.iterrows():
            store = text(row.get("店铺"))
            product_id = text(row.get("商品ID"))
            sku_key = store + "|" + product_id + "|" + normalize_sku_spec(row.get("SKU规格"))
            code_key = store + "|" + product_id + "|" + text(row.get("商家编码"))
            cost = sku_map.get(sku_key) or code_map.get(code_key)
            if cost is None and store:
                blank_sku_key = "|" + product_id + "|" + normalize_sku_spec(row.get("SKU规格"))
                blank_code_key = "|" + product_id + "|" + text(row.get("商家编码"))
                cost = sku_map.get(blank_sku_key) or code_map.get(blank_code_key)

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


def allocate_product_promotions(grouped: pd.DataFrame, promotions_df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    grouped = grouped.copy()
    grouped["推商品推广消耗"] = 0.0
    grouped["店铺被投推广消耗"] = 0.0
    grouped["推广数据日期"] = ""

    if promotions_df.empty:
        grouped["总推广消耗"] = 0.0
        return grouped, 0.0

    promotions = promotions_df.copy()
    for col in ["店铺被投推广消耗", "推商品推广消耗"]:
        promotions[col] = pd.to_numeric(promotions[col], errors="coerce").fillna(0.0)

    store_ad_total = float(promotions["店铺被投推广消耗"].sum())
    product_ad_map = promotions.groupby("商品ID", as_index=True)["推商品推广消耗"].sum().to_dict()
    promo_day_map = promotions.drop_duplicates("商品ID").set_index("商品ID")["推广数据日期"].to_dict()
    product_pay_sum = grouped.groupby("商品ID")["支付金额"].transform("sum").replace(0, pd.NA)
    product_ad_total = grouped["商品ID"].map(product_ad_map).fillna(0.0)
    grouped["推商品推广消耗"] = (
        product_ad_total
        * grouped["支付金额"]
        / product_pay_sum
    ).fillna(0.0)
    grouped["推广数据日期"] = grouped["商品ID"].map(promo_day_map).fillna("")
    grouped["总推广消耗"] = grouped["推商品推广消耗"]
    return grouped, store_ad_total


def build_profit(
    orders_df: pd.DataFrame,
    refunds_df: pd.DataFrame,
    promotions_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if orders_df.empty:
        columns = [
            "店铺",
            "商品ID",
            "商家编码",
            "SKU规格",
            "商品名称",
            "支付金额",
            "用户实付金额",
            "平台补贴金额",
            "收入取值口径",
            "SKU订单数",
            "SKU成交件数",
            "退款金额",
            "单件货价",
            "快递费",
            "货品成本",
            "快递成本",
            "推商品推广消耗",
            "店铺被投推广消耗",
            "推广数据日期",
            "总推广消耗",
            "平台扣点",
            "税点",
            "平台费用",
            "税费",
            "实时盈亏",
            "利润率",
        ]
        empty = pd.DataFrame(columns=columns)
        store_ad_total = 0.0
        product_ad_total = 0.0
        if promotions_df is not None and not promotions_df.empty:
            promotions = promotions_df.copy()
            for col in ["店铺被投推广消耗", "推商品推广消耗"]:
                if col in promotions.columns:
                    promotions[col] = pd.to_numeric(promotions[col], errors="coerce").fillna(0.0)
            store_ad_total = float(promotions.get("店铺被投推广消耗", pd.Series(dtype=float)).sum())
            product_ad_total = float(promotions.get("推商品推广消耗", pd.Series(dtype=float)).sum())
        empty.attrs["store_ad_cost"] = store_ad_total
        empty.attrs["overall_profit"] = -store_ad_total
        empty.attrs["product_ad_cost"] = product_ad_total
        return empty

    keys = ["商品ID", "商家编码", "SKU规格"]
    grouped = (
        apply_costs(orders_df).groupby(["店铺", *keys, "商品名称"], as_index=False)
        .agg(
            {
                "支付金额": "sum",
                "用户实付金额": "sum",
                "平台补贴金额": "sum",
                "收入取值口径": "last",
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
    if not refunds_df.empty:
        refund_grouped = refunds_df.groupby(keys, as_index=False).agg({"退款金额": "sum"})
        grouped = grouped.merge(refund_grouped, on=keys, how="left")
    else:
        grouped["退款金额"] = 0.0

    grouped["退款金额"] = pd.to_numeric(grouped["退款金额"], errors="coerce").fillna(0.0)
    grouped, store_ad_total = allocate_product_promotions(
        grouped,
        promotions_df if promotions_df is not None else pd.DataFrame(),
    )
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
    grouped["利润率"] = grouped.apply(
        lambda row: row["实时盈亏"] / row["支付金额"] if row["支付金额"] else 0.0,
        axis=1,
    )
    grouped.attrs["store_ad_cost"] = store_ad_total
    grouped.attrs["overall_profit"] = float(grouped["实时盈亏"].sum()) - store_ad_total
    grouped.attrs["product_ad_cost"] = float(grouped["推商品推广消耗"].sum())
    return grouped.sort_values("实时盈亏", ascending=False)


def save_outputs(df: pd.DataFrame, refunds_df: pd.DataFrame, promotions_df: pd.DataFrame, day: str) -> None:
    SHOP_DIR.mkdir(parents=True, exist_ok=True)
    money_cols = [
        "支付金额",
        "用户实付金额",
        "平台补贴金额",
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
    df = df.copy()
    for col in money_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).round(2)
    if "利润率" in df.columns:
        df["利润率"] = pd.to_numeric(df["利润率"], errors="coerce").fillna(0.0).round(4)
    refunds_df = refunds_df.copy()
    for col in ["退款金额", "退运费"]:
        if col in refunds_df.columns:
            refunds_df[col] = pd.to_numeric(refunds_df[col], errors="coerce").fillna(0.0).round(2)
    df.to_csv(SHOP_DIR / "latest.csv", index=False, encoding="utf-8-sig")
    df.to_csv(SHOP_DIR / f"latest_{day.replace('-', '')}.csv", index=False, encoding="utf-8-sig")
    refunds_df.to_csv(
        SHOP_DIR / f"douyin_refund_success_{day.replace('-', '')}.csv",
        index=False,
        encoding="utf-8-sig",
    )
    promotions_df.to_csv(
        SHOP_DIR / f"douyin_promotion_{day.replace('-', '')}.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary = {
        "store": SHOP_NAME,
        "order_day": day,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(df)),
        "pay_amount": round(float(df["支付金额"].sum()) if not df.empty else 0.0, 2),
        "refund_amount": round(float(df["退款金额"].sum()) if not df.empty else 0.0, 2),
        "product_ad_cost": round(float(df.attrs.get("product_ad_cost", 0.0)), 2),
        "store_ad_cost": round(float(df.attrs.get("store_ad_cost", 0.0)), 2),
        "row_profit": round(float(df["实时盈亏"].sum()) if not df.empty else 0.0, 2),
        "overall_profit": round(float(df.attrs.get("overall_profit", 0.0)), 2),
        "promotion_rows": int(len(promotions_df)),
        "promotion_day": text(promotions_df["推广数据日期"].iloc[0]) if not promotions_df.empty else "",
    }
    (SHOP_DIR / "latest_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run(port: int = DEFAULT_PORT, day: str | None = None, promotion_day: str | None = None) -> pd.DataFrame:
    day = day or datetime.now().strftime("%Y-%m-%d")
    page = connect_cdp(port, "fxg.jinritemai.com")
    try:
        page.call("Page.enable")
        page.call("Page.navigate", {"url": ORDER_PAGE_URL})
        time.sleep(12)
        orders = fetch_orders(page, day)
        refunds = fetch_success_refunds(page, day)
    finally:
        page.close()

    promotion_day = promotion_day or day
    if promotion_day == day:
        promo_page = open_or_navigate_cdp(
            port,
            "qianchuan.jinritemai.com/uni-prom/overall",
            build_qianchuan_realtime_url(promotion_day),
        )
    else:
        promo_page = open_or_navigate_cdp(
            port,
            "compass.jinritemai.com/shop/commodity/product-list",
            "https://compass.jinritemai.com/shop/commodity/product-list",
        )
    try:
        if promotion_day == day:
            promotions_df = fetch_realtime_qianchuan_summary(
                promo_page,
                promotion_day,
            )
        else:
            promotions_df = fetch_product_promotions(promo_page, promotion_day)
    finally:
        promo_page.close()

    orders_df = parse_orders(orders)
    added, updated, unique_count = ensure_sku_cost_workbook(orders_df, day)
    refunds_df = parse_refunds(refunds)
    result = build_profit(orders_df, refunds_df, promotions_df)
    result.attrs["sku_cost_added"] = added
    result.attrs["sku_cost_updated"] = updated
    result.attrs["sku_cost_unique"] = unique_count
    save_outputs(result, refunds_df, promotions_df, day)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--promotion-day", default=None)
    args = parser.parse_args()

    result = run(port=args.port, day=args.day, promotion_day=args.promotion_day)
    total_profit = result["实时盈亏"].sum() if not result.empty else 0.0
    total_pay = result["支付金额"].sum() if not result.empty else 0.0
    total_refund = result["退款金额"].sum() if not result.empty else 0.0
    product_ad = float(result.attrs.get("product_ad_cost", 0.0))
    store_ad = float(result.attrs.get("store_ad_cost", 0.0))
    overall_profit = float(result.attrs.get("overall_profit", total_profit))
    print(f"{SHOP_NAME} 抖店 SKU 实时盈亏完成")
    print(f"商品/SKU行数：{len(result)}")
    print(f"成本表新增SKU：{result.attrs.get('sku_cost_added', 0)}")
    print(f"成本表已有SKU：{result.attrs.get('sku_cost_updated', 0)}")
    print(f"支付金额：¥{total_pay:.2f}")
    print(f"当日退款成功：¥{total_refund:.2f}")
    print(f"推商品推广消耗：¥{product_ad:.2f}")
    print(f"店铺被投推广消耗：¥{store_ad:.2f}")
    print(f"商品行盈亏：¥{total_profit:.2f}")
    print(f"店铺整体盈亏：¥{overall_profit:.2f}")
    print(f"文件：{SHOP_DIR / 'latest.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
