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
SHOP_NAME = "盲盒抖店"
SHOP_DIR = DATA_ROOT / SHOP_NAME
DEFAULT_PORT = 9226
PLATFORM_RATE = 0.05
TAX_RATE = 0.05

ORDER_LIST_URL = "https://fxg.jinritemai.com/api/order/searchlist"
AFTERSALE_LIST_URL = "https://fxg.jinritemai.com/after_sale/pc/list"


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


def connect_cdp(port: int) -> CdpPage:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        tabs = json.loads(resp.read().decode("utf-8"))

    candidates = [
        tab
        for tab in tabs
        if tab.get("type") == "page"
        and "jinritemai.com" in text(tab.get("url"))
        and "devtools" not in text(tab.get("url")).lower()
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
    payload = page.eval_json(expr)
    if not payload or not payload.get("ok"):
        raise RuntimeError(f"抖店接口请求失败：{payload}")
    body = payload.get("text") or ""
    try:
        return json.loads(body)
    except Exception as exc:
        raise RuntimeError(f"抖店接口返回不是 JSON：{body[:300]}") from exc


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


def parse_orders(orders: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for order in orders:
        shop_order_id = text(order.get("shop_order_id"))
        pay_time = order.get("pay_time") or order.get("create_time")
        for item in order.get("product_item") or []:
            qty = num(item.get("combo_num"))
            pay = cents(item.get("pay_amount"))
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
                    "支付金额": pay,
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
    df["快递成本"] = df["快递费"] * df["SKU订单数"]
    return df


def build_profit(orders_df: pd.DataFrame, refunds_df: pd.DataFrame) -> pd.DataFrame:
    if orders_df.empty:
        return pd.DataFrame()

    keys = ["商品ID", "商家编码", "SKU规格"]
    grouped = (
        orders_df.groupby(["店铺", *keys, "商品名称"], as_index=False)
        .agg({"支付金额": "sum", "SKU订单数": "sum", "SKU成交件数": "sum"})
    )
    if not refunds_df.empty:
        refund_grouped = refunds_df.groupby(keys, as_index=False).agg({"退款金额": "sum"})
        grouped = grouped.merge(refund_grouped, on=keys, how="left")
    else:
        grouped["退款金额"] = 0.0

    grouped["退款金额"] = pd.to_numeric(grouped["退款金额"], errors="coerce").fillna(0.0)
    grouped = apply_costs(grouped)
    grouped["平台扣点"] = PLATFORM_RATE
    grouped["税点"] = TAX_RATE
    grouped["平台费用"] = grouped["支付金额"] * PLATFORM_RATE
    grouped["税费"] = grouped["支付金额"] * TAX_RATE
    grouped["总推广消耗"] = 0.0
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
    return grouped.sort_values("实时盈亏", ascending=False)


def save_outputs(df: pd.DataFrame, refunds_df: pd.DataFrame, day: str) -> None:
    SHOP_DIR.mkdir(parents=True, exist_ok=True)
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


def run(port: int = DEFAULT_PORT, day: str | None = None) -> pd.DataFrame:
    day = day or datetime.now().strftime("%Y-%m-%d")
    page = connect_cdp(port)
    try:
        orders = fetch_orders(page, day)
        refunds = fetch_success_refunds(page, day)
    finally:
        page.close()

    orders_df = parse_orders(orders)
    added, updated, unique_count = ensure_sku_cost_workbook(orders_df, day)
    refunds_df = parse_refunds(refunds)
    result = build_profit(orders_df, refunds_df)
    result.attrs["sku_cost_added"] = added
    result.attrs["sku_cost_updated"] = updated
    result.attrs["sku_cost_unique"] = unique_count
    save_outputs(result, refunds_df, day)
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
    print(f"{SHOP_NAME} 抖店 SKU 实时盈亏完成")
    print(f"商品/SKU行数：{len(result)}")
    print(f"成本表新增SKU：{result.attrs.get('sku_cost_added', 0)}")
    print(f"成本表已有SKU：{result.attrs.get('sku_cost_updated', 0)}")
    print(f"支付金额：¥{total_pay:.2f}")
    print(f"当日退款成功：¥{total_refund:.2f}")
    print(f"实时盈亏：¥{total_profit:.2f}")
    print(f"文件：{SHOP_DIR / 'latest.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
