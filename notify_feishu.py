# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_FILE = BASE_DIR / "data" / "realtime_snapshot" / "latest.json"
SKU_COST_FILE = BASE_DIR / "config" / "sku_cost.xlsx"
CONFIG_FILE = BASE_DIR / "config" / "feishu_webhook.json"
DASHBOARD_URL = "http://150.158.133.102:8080/"


def _load_config() -> tuple[str, str]:
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    secret = os.environ.get("FEISHU_SECRET", "").strip()

    if CONFIG_FILE.exists():
        payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        webhook = webhook or str(payload.get("webhook", "")).strip()
        secret = secret or str(payload.get("secret", "")).strip()

    return webhook, secret


def _sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _money(value: float) -> str:
    return f"¥{value:,.2f}"


def _money_optional(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(number):
        return "-"
    return _money(number)


def _snapshot_summary() -> dict[str, object]:
    if not SNAPSHOT_FILE.exists():
        raise FileNotFoundError(f"实时快照不存在：{SNAPSHOT_FILE}")

    payload = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    if not records:
        raise RuntimeError("实时快照没有 records")

    data = pd.DataFrame(records)
    if "ad_balance_source" not in data.columns:
        data["ad_balance_source"] = ""
    numeric_columns = [
        "profit",
        "pay_amount",
        "normal_site_ad_cost",
        "smart_ad_cost",
        "site_ad_cost",
        "keyword_ad_cost",
        "ad_cost",
        "ad_balance",
        "sales_qty",
        "order_count",
    ]

    for column in numeric_columns:
        if column not in data.columns:
            data[column] = None if column == "ad_balance" else 0
        data[column] = pd.to_numeric(data[column], errors="coerce")
        if column != "ad_balance":
            data[column] = data[column].fillna(0)
    data.loc[data["ad_balance_source"].astype(str) != "promotion_balance_api", "ad_balance"] = pd.NA

    site_keyword_ad_cost = (
        data["site_ad_cost"]
        +
        data["keyword_ad_cost"]
    )
    has_site_keyword_ad_cost = (
        data[["site_ad_cost", "keyword_ad_cost"]]
        .abs()
        .sum(axis=1)
        >
        0
    )
    data.loc[has_site_keyword_ad_cost, "ad_cost"] = site_keyword_ad_cost[has_site_keyword_ad_cost]

    legacy_component_ad_cost = (
        data["normal_site_ad_cost"]
        +
        data["smart_ad_cost"]
        +
        data["keyword_ad_cost"]
    )
    has_legacy_component_ad_cost = (
        data[["normal_site_ad_cost", "smart_ad_cost", "keyword_ad_cost"]]
        .abs()
        .sum(axis=1)
        >
        0
    )
    legacy_only = (
        ~has_site_keyword_ad_cost
        &
        has_legacy_component_ad_cost
    )
    data.loc[legacy_only, "ad_cost"] = legacy_component_ad_cost[legacy_only]

    store_summary = (
        data.groupby("store", as_index=False)
        .agg(
            profit=("profit", "sum"),
            pay_amount=("pay_amount", "sum"),
            normal_site_ad_cost=("normal_site_ad_cost", "sum"),
            smart_ad_cost=("smart_ad_cost", "sum"),
            site_ad_cost=("site_ad_cost", "sum"),
            keyword_ad_cost=("keyword_ad_cost", "sum"),
            ad_cost=("ad_cost", "sum"),
            ad_balance=("ad_balance", "max"),
            sales_qty=("sales_qty", "sum"),
            order_count=("order_count", "sum"),
        )
        .sort_values("profit", ascending=False)
    )

    return {
        "generated_at": payload.get("generated_at", ""),
        "record_count": len(data),
        "total_profit": float(data["profit"].sum()),
        "total_pay": float(data["pay_amount"].sum()),
        "total_ad": float(data["ad_cost"].sum()),
        "total_normal_site_ad": float(data["normal_site_ad_cost"].sum()),
        "total_smart_ad": float(data["smart_ad_cost"].sum()),
        "total_keyword_ad": float(data["keyword_ad_cost"].sum()),
        "stores": store_summary,
    }


def _sku_cost_summary() -> dict[str, int]:
    if not SKU_COST_FILE.exists():
        return {"rows": 0, "missing_rows": 0, "missing_products": 0}

    data = pd.read_excel(SKU_COST_FILE, dtype=str)
    if data.empty or len(data.columns) < 5:
        return {"rows": len(data), "missing_rows": 0, "missing_products": 0}

    store_col = data.columns[0]
    product_col = data.columns[1]
    price_col = data.columns[4]
    missing = data[data[price_col].isna() | data[price_col].astype(str).str.strip().eq("")]
    return {
        "rows": len(data),
        "missing_rows": len(missing),
        "missing_products": missing[[store_col, product_col]].drop_duplicates().shape[0],
    }


def _build_message() -> dict[str, object]:
    snapshot = _snapshot_summary()
    sku = _sku_cost_summary()

    stores = snapshot["stores"]
    store_count = len(stores)
    store_scope = f"{store_count}店" if store_count else "多店"
    store_lines = []
    for _, row in stores.iterrows():
        store_lines.append(
            f"【{row['store']}】："
            f"盈亏{_money(float(row['profit']))}｜"
            f"支付 {_money(float(row['pay_amount']))}｜"
            f"推广 {_money(float(row['ad_cost']))}｜"
            f"余额 {_money_optional(row.get('ad_balance'))}"
        )

    text = "\n".join(
        [
            "天猫实时盈亏抓取完成",
            f"更新时间：{snapshot['generated_at'] or datetime.now():}",
            f"商品记录：{snapshot['record_count']}",
            f"{store_scope}实时盈亏：{_money(float(snapshot['total_profit']))}",
            f"支付金额：{_money(float(snapshot['total_pay']))}",
            f"推广消耗：{_money(float(snapshot['total_ad']))}",
            "",
            "分店结果：",
            *store_lines,
            "",
            f"SKU成本表：{sku['rows']} 行",
            f"待补单件货价：{sku['missing_rows']} 行 / {sku['missing_products']} 个商品",
            "",
            f"网站：{DASHBOARD_URL}",
        ]
    )

    return {
        "msg_type": "text",
        "content": {
            "text": text
        },
    }


def send_message(webhook: str, secret: str, message: dict[str, object]) -> None:
    if secret:
        timestamp = str(int(time.time()))
        message["timestamp"] = timestamp
        message["sign"] = _sign(timestamp, secret)

    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    request = Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        response_body = response.read().decode("utf-8", errors="ignore")
        if response.status >= 400:
            raise RuntimeError(response_body)
        print(response_body)


def main() -> int:
    webhook, secret = _load_config()
    if not webhook:
        print("未配置飞书 webhook，跳过通知。")
        return 0

    try:
        send_message(webhook, secret, _build_message())
        print("飞书通知已发送。")
        return 0
    except (HTTPError, URLError, TimeoutError, RuntimeError, FileNotFoundError) as exc:
        print(f"飞书通知失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
