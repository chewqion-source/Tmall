from __future__ import annotations

import re
from datetime import datetime

import pandas as pd


SKU_COST_COLUMNS = [
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


def clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").replace("\t", " ").split()).strip()


def normalize_sku_spec(value: object) -> str:
    text = clean_text(value)
    if not text:
        return ""

    parts = re.split(r"\s*[|｜;；]\s*", text)
    normalized_parts: list[str] = []
    for part in parts:
        part = clean_text(part)
        if not part:
            continue
        part = re.sub(r"^(颜色分类|颜色|规格|尺码|尺寸|套餐|款式)\s*[:：=]\s*", "", part)
        part = re.sub(r"\s+", "", part)
        normalized_parts.append(part)

    return "|".join(normalized_parts) if normalized_parts else re.sub(r"\s+", "", text)


def sku_cost_merge_key(row: pd.Series | dict[str, object]) -> tuple[str, str, str, str]:
    store = clean_text(row.get("店铺", ""))
    item_id = clean_text(row.get("商品ID", ""))
    merchant_code = clean_text(row.get("商家编码", ""))
    sku_spec = normalize_sku_spec(row.get("SKU规格", ""))
    if sku_spec:
        return store, item_id, "", sku_spec
    return store, item_id, merchant_code, ""


def _first_non_empty(values: pd.Series) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def _last_non_empty(values: pd.Series) -> str:
    for value in reversed(values.tolist()):
        text = clean_text(value)
        if text:
            return text
    return ""


def _first_number(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return None
    return round(float(numeric.iloc[-1]), 2)


def merge_duplicate_sku_cost_rows(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame(columns=SKU_COST_COLUMNS)

    cleaned = data.copy()
    for column in SKU_COST_COLUMNS:
        if column not in cleaned.columns:
            cleaned[column] = ""
    cleaned = cleaned[SKU_COST_COLUMNS].copy()

    for column in ["店铺", "商品ID", "商家编码", "SKU规格", "备注", "首次发现日期", "最近成交日期"]:
        cleaned[column] = cleaned[column].fillna("").astype(str).map(clean_text)
    for column in ["单件货价", "快递费"]:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce").round(2)

    has_key = (
        cleaned["店铺"].ne("")
        | cleaned["商品ID"].ne("")
        | cleaned["商家编码"].ne("")
        | cleaned["SKU规格"].ne("")
    )
    cleaned = cleaned[has_key].copy()
    if cleaned.empty:
        return pd.DataFrame(columns=SKU_COST_COLUMNS)

    cleaned["_merge_key"] = cleaned.apply(sku_cost_merge_key, axis=1)
    merged = (
        cleaned.groupby("_merge_key", sort=False, dropna=False)
        .agg(
            店铺=("店铺", _last_non_empty),
            商品ID=("商品ID", _last_non_empty),
            商家编码=("商家编码", _last_non_empty),
            SKU规格=("SKU规格", _last_non_empty),
            单件货价=("单件货价", _first_number),
            快递费=("快递费", _first_number),
            备注=("备注", _last_non_empty),
            首次发现日期=("首次发现日期", _first_non_empty),
            最近成交日期=("最近成交日期", _last_non_empty),
        )
        .reset_index(drop=True)
    )

    today = datetime.now().strftime("%Y-%m-%d")
    merged["首次发现日期"] = merged["首次发现日期"].replace("", today)
    merged["最近成交日期"] = merged["最近成交日期"].replace("", today)
    return merged[SKU_COST_COLUMNS]
