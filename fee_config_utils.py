# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


FEE_CONFIG_HEADERS = [
    "店铺",
    "平台扣点",
    "税点",
    "营销托管比例",
    "备注",
]

DEFAULT_FEE_CONFIG = [
    {"店铺": "易丽洁", "平台扣点": 5.0, "税点": 5.0, "营销托管比例": 0.0, "备注": ""},
    {"店铺": "咖时光", "平台扣点": 5.0, "税点": 5.0, "营销托管比例": 0.0, "备注": ""},
    {"店铺": "坐拥_宁静", "平台扣点": 0.6, "税点": 5.0, "营销托管比例": 0.0, "备注": ""},
    {"店铺": "国货严选", "平台扣点": 8.0, "税点": 5.0, "营销托管比例": 5.0, "备注": ""},
    {"店铺": "盲盒抖店", "平台扣点": 5.0, "税点": 5.0, "营销托管比例": 0.0, "备注": ""},
    {"店铺": "盲盒千帆", "平台扣点": 5.0, "税点": 5.0, "营销托管比例": 0.0, "备注": ""},
]

OLD_ZY_STORE_NAME = "坐拥" + "宁静"
SHOP_NAME_ALIASES = {
    OLD_ZY_STORE_NAME: "坐拥_宁静",
}


def normalize_store_name(value: object) -> str:
    store = str(value or "").strip()
    return SHOP_NAME_ALIASES.get(store, store)


def default_fee_config_frame(stores: Iterable[str] | None = None) -> pd.DataFrame:
    data = pd.DataFrame(DEFAULT_FEE_CONFIG)
    if stores:
        existing = set(data["店铺"].astype(str))
        extra = [
            {"店铺": normalize_store_name(store), "平台扣点": 5.0, "税点": 5.0, "营销托管比例": 0.0, "备注": ""}
            for store in stores
            if normalize_store_name(store) and normalize_store_name(store) not in existing
        ]
        if extra:
            data = pd.concat([data, pd.DataFrame(extra)], ignore_index=True)
    return clean_fee_config_frame(data)


def clean_fee_config_frame(data: pd.DataFrame) -> pd.DataFrame:
    cleaned = data.copy()
    for column in FEE_CONFIG_HEADERS:
        if column not in cleaned.columns:
            cleaned[column] = ""
    cleaned = cleaned[FEE_CONFIG_HEADERS].copy()
    cleaned["店铺"] = cleaned["店铺"].map(normalize_store_name)
    cleaned["备注"] = cleaned["备注"].fillna("").astype(str).str.strip()
    for column in ["平台扣点", "税点", "营销托管比例"]:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce").fillna(0.0)
        cleaned[column] = cleaned[column].clip(lower=0).round(4)
    cleaned = cleaned[cleaned["店铺"].astype(str).str.strip().ne("")]
    cleaned = cleaned.drop_duplicates(subset=["店铺"], keep="last")
    defaults = default_fee_config_frame_without_clean()
    missing = defaults[~defaults["店铺"].isin(cleaned["店铺"])]
    if not missing.empty:
        cleaned = pd.concat([cleaned, missing], ignore_index=True, sort=False)
    return cleaned[FEE_CONFIG_HEADERS].reset_index(drop=True)


def default_fee_config_frame_without_clean() -> pd.DataFrame:
    return pd.DataFrame(DEFAULT_FEE_CONFIG, columns=FEE_CONFIG_HEADERS)


def load_fee_config_frame(path: Path, stores: Iterable[str] | None = None) -> pd.DataFrame:
    if path.exists():
        data = pd.read_excel(path, dtype={"店铺": str})
    else:
        data = default_fee_config_frame(stores)
    if stores:
        data = pd.concat([data, default_fee_config_frame(stores)], ignore_index=True, sort=False)
    return clean_fee_config_frame(data)


def save_fee_config_frame(data: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean_fee_config_frame(data).to_excel(path, index=False, sheet_name="费用比例配置")


def percent_value_to_rate(value: object) -> float:
    try:
        number = float(value)
    except Exception:
        return 0.0
    if number < 0:
        return 0.0
    return number / 100


def fee_rates_for_store(path: Path, store: str) -> dict[str, float]:
    config = load_fee_config_frame(path)
    store_name = normalize_store_name(store)
    row = config[config["店铺"].astype(str).eq(store_name)]
    if row.empty:
        defaults = default_fee_config_frame()
        row = defaults[defaults["店铺"].astype(str).eq(store_name)]
    if row.empty:
        return {
            "platform_rate": 0.05,
            "tax_rate": 0.05,
            "marketing_rate": 0.0,
        }
    item = row.iloc[0]
    return {
        "platform_rate": percent_value_to_rate(item.get("平台扣点")),
        "tax_rate": percent_value_to_rate(item.get("税点")),
        "marketing_rate": percent_value_to_rate(item.get("营销托管比例")),
    }
