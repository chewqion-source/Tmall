from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
SOURCE_PATH = ROOT / "product_image_sources.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from product_image_utils import normalize_image_url
OLD_ZY = "坐拥" + "宁静"
STORE_ALIASES = {OLD_ZY: "坐拥_宁静"}


def load_sources() -> dict[str, dict[str, str]]:
    if not SOURCE_PATH.exists():
        return {}
    return json.loads(SOURCE_PATH.read_text(encoding="utf-8"))


def normalize_store(value: object) -> str:
    store = str(value or "").strip()
    return STORE_ALIASES.get(store, store)


def collect_latest_images() -> dict[str, dict[str, str]]:
    collected: dict[str, dict[str, str]] = {}
    for path in DATA_ROOT.glob("*/latest.csv"):
        try:
            data = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        except Exception:
            continue
        if data.empty or "商品ID" not in data.columns or "商品主图" not in data.columns:
            continue
        store = normalize_store(data["店铺"].dropna().iloc[0] if "店铺" in data.columns and data["店铺"].notna().any() else path.parent.name)
        bucket = collected.setdefault(store, {})
        for _, row in data.iterrows():
            product_id = str(row.get("商品ID") or "").strip()
            image_url = normalize_image_url(row.get("商品主图"))
            if product_id and image_url:
                bucket[product_id] = image_url
    return collected


def main() -> int:
    sources = load_sources()
    normalized: dict[str, dict[str, str]] = {}
    for store, products in sources.items():
        normalized_store = normalize_store(store)
        normalized.setdefault(normalized_store, {}).update({str(k): str(v) for k, v in (products or {}).items() if str(v).strip()})

    latest = collect_latest_images()
    changed = 0
    for store, products in latest.items():
        bucket = normalized.setdefault(store, {})
        for product_id, image_url in products.items():
            if bucket.get(product_id) != image_url:
                bucket[product_id] = image_url
                changed += 1

    SOURCE_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(len(products) for products in normalized.values())
    print(f"image_sources={total} changed={changed} stores={len(normalized)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
