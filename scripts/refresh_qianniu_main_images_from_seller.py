from __future__ import annotations

import asyncio
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pandas as pd
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from product_image_utils import normalize_image_url


SOURCE_PATH = ROOT / "product_image_sources.json"
DATA_ROOT = ROOT / "data"
SELLER_BASE_URL = "https://myseller.taobao.com/home.htm/SellManage"
SELLER_ROUTES = [
    ("出售中", "on_sale"),
    ("仓库中", "in_stock"),
    ("渠道商品", "subitem"),
]
SHOPS = {
    "易丽洁": 9222,
    "咖时光": 9223,
    "坐拥_宁静": 9224,
}

EXTRACT_PAGE_JS = r"""
() => {
  const out = {};
  for (const img of Array.from(document.querySelectorAll('img'))) {
    const src = img.currentSrc || img.src || img.getAttribute('src') || '';
    const rect = img.getBoundingClientRect();
    if (!src || rect.width < 40 || rect.height < 40) continue;
    if (rect.y < 240 || rect.x < 250 || rect.x > 520) continue;
    let el = img;
    let text = '';
    for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
      text += ' ' + String(el.innerText || el.textContent || '');
    }
    const match = text.match(/(?:ID|商品ID|主商品ID)[:：]?\s*(\d{8,})/);
    if (match && !out[match[1]]) {
      out[match[1]] = src;
    }
  }
  const bodyText = document.body ? String(document.body.innerText || '') : '';
  const totalMatch = bodyText.match(/共\s*(\d+)\s*件商品/);
  return {images: out, total: totalMatch ? Number(totalMatch[1]) : 0};
}
"""


def load_sources() -> dict[str, dict[str, str]]:
    if SOURCE_PATH.exists():
        return json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    return {}


def product_ids_for_store(store: str, sources: dict[str, dict[str, str]]) -> list[str]:
    ids: set[str] = set(str(pid) for pid in (sources.get(store) or {}).keys())
    latest = DATA_ROOT / store / "latest.csv"
    if latest.exists():
        try:
            df = pd.read_csv(latest, encoding="utf-8-sig", dtype=str)
            if "商品ID" in df.columns:
                ids.update(str(value).strip() for value in df["商品ID"].dropna())
        except Exception:
            pass
    return sorted(pid for pid in ids if pid and pid.lower() != "nan" and pid != "__store_adjustment__")


def seller_page_url(route: str, page_no: int) -> str:
    parsed = urlparse(f"{SELLER_BASE_URL}/{route}")
    query = parse_qs(parsed.query)
    query.update({"current": [str(page_no)], "pageSize": ["20"]})
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


async def find_seller_page(context, port: int):
    pages = [page for page in context.pages if "SellManage/" in page.url]
    if pages:
        return pages[0]
    page = await context.new_page()
    await page.goto(seller_page_url("on_sale", 1), wait_until="domcontentloaded", timeout=60000)
    return page


async def refresh_store(playwright, store: str, port: int, sources: dict[str, dict[str, str]], page_limit: int) -> int:
    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await find_seller_page(context, port)
    bucket = sources.setdefault(store, {})
    changed = 0
    seen: set[str] = set()
    for source_name, route in SELLER_ROUTES:
        max_pages = 1
        route_seen: set[str] = set()
        for page_no in range(1, min(page_limit, 29) + 1):
            try:
                await page.goto(seller_page_url(route, page_no), wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3500)
                await page.evaluate("() => window.scrollTo(0, 0)")
                await page.wait_for_timeout(800)
                payload = await page.evaluate(EXTRACT_PAGE_JS)
                if not dict((payload or {}).get("images") or {}):
                    await page.mouse.wheel(0, 450)
                    await page.wait_for_timeout(1200)
                    payload = await page.evaluate(EXTRACT_PAGE_JS)
                if page_no == 1 and int((payload or {}).get("total") or 0) and not dict((payload or {}).get("images") or {}):
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(4500)
                    payload = await page.evaluate(EXTRACT_PAGE_JS)
            except Exception as exc:
                print(f"{store} {source_name} page {page_no}: skipped {type(exc).__name__} {exc}")
                continue
            images = dict((payload or {}).get("images") or {})
            total = int((payload or {}).get("total") or 0)
            if page_no == 1 and total:
                max_pages = max(1, (total + 19) // 20)
            print(f"{store} {source_name} page {page_no}/{max_pages}: images={len(images)} total={total}")
            for product_id, raw_url in images.items():
                image_url = normalize_image_url(raw_url)
                old = bucket.get(product_id)
                if old != image_url:
                    bucket[product_id] = image_url
                    changed += 1
                seen.add(product_id)
                route_seen.add(product_id)
            if page_no >= max_pages:
                break
        print(f"{store} {source_name}: seen={len(route_seen)}")
    await browser.close()
    print(f"{store}: seen={len(seen)} changed={changed}")
    return changed


async def main() -> int:
    global SELLER_ROUTES

    parser = argparse.ArgumentParser(description="Refresh QianNiu product main images from seller product lists.")
    parser.add_argument("--store", action="append", help="Only refresh selected store. Can be used multiple times.")
    parser.add_argument("--route", action="append", choices=[route for _, route in SELLER_ROUTES], help="Only refresh selected route.")
    parser.add_argument("--page-limit", type=int, default=29, help="Max pages to scan per route.")
    args = parser.parse_args()

    if args.route:
        route_set = set(args.route)
        SELLER_ROUTES = [entry for entry in SELLER_ROUTES if entry[1] in route_set]

    sources = load_sources()
    total_changed = 0
    requested_stores = set(args.store or [])
    async with async_playwright() as playwright:
        for store, port in SHOPS.items():
            if requested_stores and store not in requested_stores:
                continue
            total_changed += await refresh_store(playwright, store, port, sources, max(1, args.page_limit))
    SOURCE_PATH.write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"qianniu_main_images_changed={total_changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
