from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MAX_IMAGE_BYTES = 6 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class ProductImageResult:
    product_id: str
    image_path: Path
    source_url: str


def get_product_image_dir() -> Path:
    data_dir = Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))
    return Path(os.environ.get("TMALL_PRODUCT_IMAGE_DIR", data_dir / "product_images"))


def product_image_url(product_id: str) -> str | None:
    image_dir = get_product_image_dir()
    for suffix in IMAGE_EXTENSIONS:
        path = image_dir / f"{product_id}{suffix}"
        if path.exists():
            return f"/product-images/{path.name}"
    return None


def _request(url: str, timeout: int = 18) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.tmall.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片文件过大")
    return data


def _normalize_image_url(value: str) -> str | None:
    value = html.unescape(value).replace("\\/", "/").strip()
    value = value.split("?")[0] if "alicdn.com" in value else value
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith(("http://", "https://")):
        return None
    lowered = value.lower()
    if not any(ext in lowered for ext in IMAGE_EXTENSIONS):
        return None
    if not any(host in lowered for host in ("alicdn.com", "tbcdn.cn", "taobaocdn.com")):
        return None
    return value


def _extract_image_urls(page_html: str) -> list[str]:
    patterns = [
        r"https?:\\?/\\?/[^\"'<> ]+?\.(?:jpg|jpeg|png|webp)",
        r"https?://[^\"'<> ]+?\.(?:jpg|jpeg|png|webp)",
        r"//[^\"'<> ]+?\.(?:jpg|jpeg|png|webp)",
        r"(?:picUrl|pic_url|image|img|src)[\"']?\s*[:=]\s*[\"']([^\"']+)",
    ]
    urls: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for raw in re.findall(pattern, page_html, flags=re.IGNORECASE):
            normalized = _normalize_image_url(raw)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)
    return urls


def _image_suffix(content_type: str | None, source_url: str) -> str:
    lowered_type = (content_type or "").lower()
    lowered_url = urllib.parse.urlparse(source_url).path.lower()
    if "png" in lowered_type or lowered_url.endswith(".png"):
        return ".png"
    if "webp" in lowered_type or lowered_url.endswith(".webp"):
        return ".webp"
    return ".jpg"


def _download_image(source_url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(
        source_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": "https://detail.tmall.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=18) as response:
        content_type = response.headers.get("content-type")
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片文件过大")
    if not data.startswith((b"\xff\xd8", b"\x89PNG", b"RIFF")):
        raise ValueError("未下载到有效图片")
    return data, _image_suffix(content_type, source_url)


def fetch_product_image(product_id: str, image_dir: Path | None = None, force: bool = False) -> ProductImageResult:
    product_id = str(product_id).strip()
    if not product_id or not product_id.isdigit():
        raise ValueError("商品 ID 无效")
    image_dir = image_dir or get_product_image_dir()
    image_dir.mkdir(parents=True, exist_ok=True)
    if not force:
        existing = product_image_url(product_id)
        if existing:
            existing_path = image_dir / Path(existing).name
            return ProductImageResult(product_id, existing_path, "cache")

    detail_urls = [
        f"https://detail.tmall.com/item.htm?id={product_id}",
        f"https://item.taobao.com/item.htm?id={product_id}",
    ]
    errors: list[str] = []
    for detail_url in detail_urls:
        try:
            page = _request(detail_url).decode("utf-8", errors="ignore")
            image_urls = _extract_image_urls(page)
            for source_url in image_urls[:12]:
                try:
                    image_data, suffix = _download_image(source_url)
                except Exception:
                    continue
                target = image_dir / f"{product_id}{suffix}"
                for old_suffix in IMAGE_EXTENSIONS:
                    old_path = image_dir / f"{product_id}{old_suffix}"
                    if old_path != target:
                        old_path.unlink(missing_ok=True)
                target.write_bytes(image_data)
                if os.name != "nt":
                    target.chmod(0o644)
                return ProductImageResult(product_id, target, source_url)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            errors.append(f"{detail_url}: {exc}")
    raise ValueError("未能抓取到商品图；可能被平台拦截或商品页没有公开图片。")
