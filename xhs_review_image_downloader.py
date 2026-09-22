# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import websocket

from encoding_guard import enable_utf8_stdio


enable_utf8_stdio()


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 9227
REVIEW_MANAGER_URL = "https://ark.xiaohongshu.com/api/edith/review/v2/seller/review_manager"
REVIEW_DETAIL_URL = "https://ark.xiaohongshu.com/api/edith/review/seller/get_review_info_by_review_id"


def safe_name(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:80] or fallback


class CdpPage:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.next_id = 1
        self.call("Runtime.enable")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        msg_id = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            data = json.loads(self.ws.recv())
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result") or {}

    def eval_json(self, expression: str, timeout: int = 60) -> Any:
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


def connect_review_page(port: int) -> CdpPage:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        tabs = json.loads(resp.read().decode("utf-8"))
    review_candidates = [
        tab
        for tab in tabs
        if tab.get("type") == "page"
        and "app-item/comment/analysis" in str(tab.get("url") or "")
        and tab.get("webSocketDebuggerUrl")
    ]
    candidates = review_candidates or [
        tab
        for tab in tabs
        if tab.get("type") == "page"
        and "ark.xiaohongshu.com/" in str(tab.get("url") or "")
        and tab.get("webSocketDebuggerUrl")
    ]
    if not candidates:
        raise RuntimeError(f"端口 {port} 没有找到小红书评价管理页面")
    return CdpPage(candidates[0]["webSocketDebuggerUrl"])


def browser_fetch_json(
    page: CdpPage,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    full_url = url
    if params:
        full_url += "?" + urllib.parse.urlencode(params, doseq=True)
    expr = f"""
    (async () => {{
      const res = await fetch({json.dumps(full_url)}, {{
        method: {json.dumps(method)},
        credentials: 'include',
        headers: {{
          'accept': 'application/json, text/plain, */*',
          'content-type': 'application/json'
        }},
        body: {json.dumps(json.dumps(body, ensure_ascii=False)) if body is not None else "undefined"}
      }});
      const text = await res.text();
      return JSON.stringify({{status: res.status, ok: res.ok, text}});
    }})()
    """
    payload = page.eval_json(expr)
    try:
        data = json.loads(payload.get("text") or "{}")
    except Exception as exc:
        raise RuntimeError(f"接口返回不是 JSON：{payload.get('text', '')[:300]}") from exc
    if not payload.get("ok") or not data.get("success"):
        raise RuntimeError(f"接口失败 {full_url}：{data.get('msg') or payload.get('text', '')[:300]}")
    return data.get("data") or {}


def fetch_review_rows(page: CdpPage, page_size: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total = None
    page_no = 1
    while True:
        body = {
            "page_size": page_size,
            "page": page_no,
            "source": 0,
            "sku_score_list": [5],
            "content_type_list": [1],
        }
        data = browser_fetch_json(page, REVIEW_MANAGER_URL, method="POST", body=body)
        items = data.get("review_info_list") or []
        total = int(data.get("total") or total or 0)
        rows.extend(items)
        print(f"已读取第 {page_no} 页：累计 {len(rows)}/{total or '?'} 条")
        if not items or len(items) < page_size or (total and len(rows) >= total):
            break
        page_no += 1
    return rows[:total] if total else rows


def fetch_detail(page: CdpPage, review_id: str, review_type: Any) -> dict[str, Any]:
    params = {"review_id": review_id, "review_type": review_type or 4}
    return browser_fetch_json(page, REVIEW_DETAIL_URL, method="GET", params=params)


def download_file(url: str, dest: Path) -> Path:
    if url.startswith("//"):
        url = "https:" + url
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://ark.xiaohongshu.com/app-item/comment/analysis",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        content_type = resp.headers.get("Content-Type", "").lower()
        suffix = ".jpg"
        if "png" in content_type:
            suffix = ".png"
        elif "webp" in content_type:
            suffix = ".webp"
        final = dest.with_suffix(suffix)
        final.write_bytes(resp.read())
        return final


def main() -> int:
    parser = argparse.ArgumentParser(description="下载小红书评价管理里的好评带图图片")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--out", default="")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else BASE_DIR / "data" / "xhs_review_images" / stamp
    if out_dir.exists() and not args.resume:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    page = connect_review_page(args.port)
    manifest: list[dict[str, Any]] = []
    try:
        rows = fetch_review_rows(page)
        for idx, row in enumerate(rows, 1):
            review = row.get("review_data") or {}
            sku = row.get("sku_info") or {}
            review_id = str(review.get("review_id") or "")
            images = ((review.get("content") or {}).get("images") or [])
            if not review_id or not images:
                continue
            detail = fetch_detail(page, review_id, review.get("review_type"))
            user_info = detail.get("user_info") or {}
            nickname = user_info.get("name") or ("匿名用户" if review.get("anonymous") else "unknown")
            nick_dir = out_dir / safe_name(nickname, "unknown")
            nick_dir.mkdir(parents=True, exist_ok=True)
            for img_no, img in enumerate(images, 1):
                link = img.get("link") if isinstance(img, dict) else str(img)
                if not link:
                    continue
                base = f"{idx:04d}_{safe_name(sku.get('name'), '商品')}_{review_id}_{img_no}"
                dest = nick_dir / base
                existing = list(nick_dir.glob(f"*_{review_id}_{img_no}.*"))
                try:
                    if existing and existing[0].stat().st_size > 0:
                        final_path = existing[0]
                        status = "ok"
                    else:
                        final_path = download_file(link, dest)
                        status = "ok"
                except Exception as exc:
                    final_path = ""
                    status = f"failed: {exc}"
                manifest.append(
                    {
                        "nickname": nickname,
                        "review_id": review_id,
                        "image_no": img_no,
                        "status": status,
                        "file": str(final_path),
                        "image_url": link,
                        "product_name": sku.get("name") or "",
                        "item_id": sku.get("item_id") or "",
                        "sku_id": sku.get("sku_id") or "",
                        "order_id": sku.get("order_id") or "",
                        "review_text": (review.get("content") or {}).get("text") or "",
                        "create_time": review.get("create_time") or "",
                    }
            )
            print(f"已处理 {idx}/{len(rows)}：{nickname} / {review_id}", flush=True)
            time.sleep(0.05)
    finally:
        page.close()

    csv_path = out_dir / "下载清单.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "nickname",
                "review_id",
                "image_no",
                "status",
                "file",
                "image_url",
                "product_name",
                "item_id",
                "sku_id",
                "order_id",
                "review_text",
                "create_time",
            ],
        )
        writer.writeheader()
        writer.writerows(manifest)

    zip_path = out_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in out_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(out_dir.parent))

    ok_count = sum(1 for x in manifest if x["status"] == "ok")
    fail_count = len(manifest) - ok_count
    print(json.dumps({"out_dir": str(out_dir), "zip": str(zip_path), "images_ok": ok_count, "images_failed": fail_count}, ensure_ascii=False))
    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
