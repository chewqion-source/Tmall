from __future__ import annotations

from typing import Any


IMAGE_KEYS = (
    "mainImage",
    "mainImageUrl",
    "mainPic",
    "mainPicUrl",
    "itemPic",
    "itemPicUrl",
    "picUrl",
    "pictUrl",
    "imgUrl",
    "imageUrl",
    "image",
    "cover",
    "coverUrl",
    "productImg",
    "productImgUrl",
    "productImage",
    "productImageUrl",
    "thumbUrl",
)


def normalize_image_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    if raw.startswith("http://"):
        return "https://" + raw[len("http://") :]
    if raw.startswith("https://"):
        return raw
    if raw.startswith(("i0/", "i1/", "i2/", "i3/", "i4/")):
        return f"https://img.alicdn.com/imgextra/{raw}"
    if raw.startswith(("img/", "bao/uploaded/")):
        return f"https://img.alicdn.com/{raw}"
    return raw


def first_image_from_value(value: Any) -> str:
    if isinstance(value, str):
        return normalize_image_url(value)
    if isinstance(value, list):
        for item in value:
            url = first_image_from_value(item)
            if url:
                return url
    if isinstance(value, dict):
        return first_image_from_obj(value)
    return ""


def first_image_from_obj(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    for key in IMAGE_KEYS:
        if key in obj:
            url = first_image_from_value(obj.get(key))
            if url:
                return url
    for key, value in obj.items():
        lowered = str(key).lower()
        if any(token in lowered for token in ("image", "img", "pic", "cover", "thumb")):
            url = first_image_from_value(value)
            if url:
                return url
    return ""
