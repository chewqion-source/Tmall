from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from data_loader import STORE_FILE_PATTERNS, SUPPORTED_EXTENSIONS, load_product_daily


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
STORE_CANONICAL_BASENAMES = {
    "易丽洁": "2026年天猫日报表-易丽洁",
    "坐拥宁静": "2026年坐拥宁静日报表",
    "国货严选": "2026年国货严选日报表",
    "咖时光": "2026年咖时光日报表-天猫",
}


@dataclass(frozen=True)
class UploadResult:
    store: str
    original_name: str
    saved_name: str
    sha256: str
    bytes: int
    start_date: str
    end_date: str
    date_sheets: int
    products: int
    sales: float
    orders: float
    profit: float


def _validate_upload(store: str, original_name: str, content: bytes) -> str:
    if store not in STORE_CANONICAL_BASENAMES:
        raise ValueError(f"未知店铺：{store}")
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"{store}：只支持 {', '.join(SUPPORTED_EXTENSIONS)}")
    if not content:
        raise ValueError(f"{store}：上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{store}：文件超过 25MB")
    return suffix


def _existing_store_files(data_dir: Path, store: str) -> list[Path]:
    return sorted(
        {
            path
            for pattern in STORE_FILE_PATTERNS[store]
            for path in data_dir.glob(pattern)
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        }
    )


def install_uploaded_workbooks(
    uploads: Mapping[str, tuple[str, bytes]], data_dir: str | Path
) -> list[UploadResult]:
    """Validate every workbook, then archive old files and install atomically.

    The caller supplies an explicit store for each file, so finance users do not
    have to preserve a particular filename. File contents are parsed before any
    current workbook is changed.
    """
    if not uploads:
        raise ValueError("请至少选择一家店铺的日报")

    target_dir = Path(data_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    incoming_dir = target_dir / ".incoming"
    incoming_dir.mkdir(exist_ok=True)
    prepared: list[tuple[str, str, Path, Path, UploadResult]] = []
    staged_paths: list[Path] = []

    try:
        for store, (original_name, content) in uploads.items():
            suffix = _validate_upload(store, original_name, content)
            canonical_name = f"{STORE_CANONICAL_BASENAMES[store]}{suffix}"
            target_path = target_dir / canonical_name
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=incoming_dir,
                prefix=f"{STORE_CANONICAL_BASENAMES[store]}-",
                suffix=suffix,
                delete=False,
            ) as temporary_file:
                temporary_file.write(content)
                staged_path = Path(temporary_file.name)
                staged_paths.append(staged_path)

            daily = load_product_daily(staged_path)
            result = UploadResult(
                store=store,
                original_name=Path(original_name).name,
                saved_name=canonical_name,
                sha256=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
                start_date=daily["date"].min().strftime("%Y-%m-%d"),
                end_date=daily["date"].max().strftime("%Y-%m-%d"),
                date_sheets=int(daily["date"].nunique()),
                products=int(daily["product_id"].nunique()),
                sales=float(daily["sales_qty"].sum()),
                orders=float(daily["order_count"].sum()),
                profit=float(daily["profit"].sum()),
            )
            prepared.append((store, original_name, staged_path, target_path, result))

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        results: list[UploadResult] = []
        for store, _original_name, staged_path, target_path, result in prepared:
            archive_dir = target_dir / "archive" / store
            archive_dir.mkdir(parents=True, exist_ok=True)
            for existing in _existing_store_files(target_dir, store):
                archive_name = f"{timestamp}-{uuid.uuid4().hex[:8]}-{existing.name}"
                os.replace(existing, archive_dir / archive_name)
            os.replace(staged_path, target_path)
            if os.name != "nt":
                target_path.chmod(0o640)
            results.append(result)

        history_path = target_dir / "upload-history.jsonl"
        history_record = {
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "files": [asdict(result) for result in results],
        }
        with history_path.open("a", encoding="utf-8") as history_file:
            history_file.write(json.dumps(history_record, ensure_ascii=False) + "\n")
        return results
    finally:
        for staged_path in staged_paths:
            staged_path.unlink(missing_ok=True)
