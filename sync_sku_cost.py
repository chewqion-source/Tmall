# -*- coding: utf-8 -*-
from datetime import datetime
from pathlib import Path
import logging
import sys
import time

import pandas as pd
import paramiko

from sku_cost_utils import merge_duplicate_sku_cost_rows


logging.getLogger("paramiko").setLevel(logging.CRITICAL)


BASE_DIR = Path(__file__).resolve().parent
LOCAL_SKU_COST = BASE_DIR / "config" / "sku_cost.xlsx"
SSH_KEY_FILE = BASE_DIR / ".ssh_tmp" / "tmall_codex_temp_ed25519"

REMOTE_HOST = "150.158.133.102"
REMOTE_USER = "ubuntu"
REMOTE_SKU_COST = "/opt/tmall-dashboard/data/sku_cost.xlsx"
OLD_ZY_STORE_NAME = "坐拥" + "宁静"
SHOP_NAME_ALIASES = {
    OLD_ZY_STORE_NAME: "坐拥_宁静",
}
SKU_COLUMNS = [
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


def _normalize_shop_names(data: pd.DataFrame) -> pd.DataFrame:
    if "店铺" not in data.columns:
        return data
    normalized = data.copy()
    normalized["店铺"] = (
        normalized["店铺"]
        .fillna("")
        .astype(str)
        .str.strip()
        .replace(SHOP_NAME_ALIASES)
    )
    return normalized


def _read_sku_cost(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=SKU_COLUMNS)

    data = pd.read_excel(
        path,
        dtype={
            "店铺": str,
            "商品ID": str,
            "商家编码": str,
            "SKU规格": str,
        },
    )
    for column in SKU_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    data = data[SKU_COLUMNS].copy()
    for column in ["店铺", "商品ID", "商家编码", "SKU规格", "备注", "首次发现日期", "最近成交日期"]:
        data[column] = data[column].fillna("").astype(str).str.strip()
    for column in ["单件货价", "快递费"]:
        data[column] = pd.to_numeric(data[column], errors="coerce").round(2)
    return _normalize_shop_names(data)


def _save_sku_cost(data: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = data.copy()
    for column in SKU_COLUMNS:
        if column not in cleaned.columns:
            cleaned[column] = ""
    cleaned = cleaned[SKU_COLUMNS]
    cleaned = _normalize_shop_names(cleaned)
    has_key = (
        cleaned["店铺"].fillna("").astype(str).str.strip().ne("")
        | cleaned["商品ID"].fillna("").astype(str).str.strip().ne("")
        | cleaned["商家编码"].fillna("").astype(str).str.strip().ne("")
        | cleaned["SKU规格"].fillna("").astype(str).str.strip().ne("")
    )
    cleaned = merge_duplicate_sku_cost_rows(cleaned[has_key])
    cleaned.to_excel(path, index=False, sheet_name="SKU成本配置")


def _connect():
    key = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_FILE))
    last_error = None
    for attempt in range(1, 6):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            print(f"SSH connect attempt {attempt}/5...")
            client.connect(
                hostname=REMOTE_HOST,
                username=REMOTE_USER,
                pkey=key,
                timeout=30,
                banner_timeout=60,
                auth_timeout=60,
                look_for_keys=False,
                allow_agent=False,
            )
            return client
        except Exception as exc:
            last_error = exc
            client.close()
            print(f"SSH connect attempt {attempt}/5 failed: {exc}")
            if attempt < 5:
                time.sleep(min(10 * attempt, 60))
    raise last_error


def merge_remote_to_local() -> int:
    client = _connect()
    temp_remote = BASE_DIR / "config" / "_remote_sku_cost.xlsx"
    sftp = client.open_sftp()
    try:
        sftp.get(REMOTE_SKU_COST, str(temp_remote))
    finally:
        sftp.close()
        client.close()

    local = _read_sku_cost(LOCAL_SKU_COST)
    remote = _read_sku_cost(temp_remote)
    temp_remote.unlink(missing_ok=True)

    if LOCAL_SKU_COST.exists():
        backup = (
            BASE_DIR
            / "config"
            / "backups"
            / f"sku_cost_before_remote_merge_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(LOCAL_SKU_COST.read_bytes())

    merged = pd.concat([local, remote], ignore_index=True, sort=False)
    _save_sku_cost(merged, LOCAL_SKU_COST)
    print(f"成本表已合并线上版本：本地 {len(local)} 行，线上 {len(remote)} 行，合并后 {len(_read_sku_cost(LOCAL_SKU_COST))} 行")
    return 0


def upload_local_to_remote() -> int:
    if not LOCAL_SKU_COST.exists():
        print(f"本地成本表不存在：{LOCAL_SKU_COST}")
        return 1

    client = _connect()
    client.exec_command("mkdir -p /opt/tmall-dashboard/data")[1].channel.recv_exit_status()
    sftp = client.open_sftp()
    try:
        sftp.put(str(LOCAL_SKU_COST), REMOTE_SKU_COST)
    finally:
        sftp.close()
        client.close()

    rows = len(_read_sku_cost(LOCAL_SKU_COST))
    print(f"成本表已同步到网站：{rows} 行")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "push"
    if mode == "pull":
        return merge_remote_to_local()
    if mode == "push":
        return upload_local_to_remote()
    print("用法：python sync_sku_cost.py pull|push")
    return 2


if __name__ == "__main__":
    sys.exit(main())
