from __future__ import annotations

import json
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "product_image_sources.json"
OUTPUT_ROOT = ROOT / "static" / "product_images"


def main() -> None:
    sources = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    downloaded = 0
    for store, products in sources.items():
        store_dir = OUTPUT_ROOT / store
        store_dir.mkdir(parents=True, exist_ok=True)
        for product_id, image_url in products.items():
            target = store_dir / f"{product_id}.webp"
            request = urllib.request.Request(
                image_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/*"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                image_bytes = response.read()
                content_type = response.headers.get_content_type()
            if not image_bytes or not content_type.startswith("image/"):
                raise RuntimeError(f"{store} {product_id} 未返回有效图片")
            target.write_bytes(image_bytes)
            downloaded += 1
    print(f"cached_images={downloaded}")


if __name__ == "__main__":
    main()
