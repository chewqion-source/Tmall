# -*- coding: utf-8 -*-
from encoding_guard import enable_utf8_stdio

enable_utf8_stdio()

from pathlib import Path
from datetime import datetime
import json
import logging
import sys
import time

import paramiko


logging.getLogger("paramiko").setLevel(logging.CRITICAL)


BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_FILE = BASE_DIR / "data" / "realtime_snapshot" / "latest.json"
SSH_KEY_FILE = BASE_DIR / ".ssh_tmp" / "tmall_codex_temp_ed25519"

REMOTE_HOST = "150.158.133.102"
REMOTE_USER = "ubuntu"
REMOTE_FILE = "/opt/tmall-dashboard/data/realtime/latest.json"


def validate_today_snapshot(payload: dict) -> tuple[bool, str]:
    generated_at = str(payload.get("generated_at", "")).strip()
    today = datetime.now().strftime("%Y-%m-%d")
    if not generated_at:
        return False, "实时快照缺少 generated_at，停止上传。"
    if not generated_at.startswith(today):
        return False, f"实时快照不是今天的数据：generated_at={generated_at}，today={today}，停止上传。"
    return True, ""


def connect_with_retry(max_attempts: int = 5):
    key = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_FILE))
    last_error = None
    for attempt in range(1, max_attempts + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            print(f"SSH connect attempt {attempt}/{max_attempts}...")
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
            print(f"SSH connect attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(min(10 * attempt, 60))
    raise last_error


def main() -> int:
    if not SNAPSHOT_FILE.exists():
        print(f"实时快照不存在：{SNAPSHOT_FILE}")
        return 1

    payload = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    generated_at = payload.get("generated_at", "")
    ok, reason = validate_today_snapshot(payload)
    if not ok:
        print(reason)
        return 1
    if not records:
        print("实时快照没有 records，停止上传。")
        return 1

    if not SSH_KEY_FILE.exists():
        print(f"SSH Key 不存在：{SSH_KEY_FILE}")
        return 1

    client = connect_with_retry()

    stdin, stdout, stderr = client.exec_command("mkdir -p /opt/tmall-dashboard/data/realtime")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        print(stderr.read().decode("utf-8", errors="ignore"))
        client.close()
        return rc

    sftp = client.open_sftp()
    sftp.put(str(SNAPSHOT_FILE), REMOTE_FILE)
    sftp.close()

    verify_cmd = (
        "/opt/tmall-dashboard/.venv/bin/python - <<'PY'\n"
        "import json\n"
        f"p='{REMOTE_FILE}'\n"
        "payload=json.load(open(p, encoding='utf-8'))\n"
        "print(payload.get('generated_at', ''), len(payload.get('records', [])))\n"
        "PY"
    )
    stdin, stdout, stderr = client.exec_command(verify_cmd)
    verify_out = stdout.read().decode("utf-8", errors="ignore").strip()
    verify_err = stderr.read().decode("utf-8", errors="ignore").strip()
    rc = stdout.channel.recv_exit_status()
    client.close()

    if rc != 0:
        print(verify_err)
        return rc

    print(f"上传完成：{generated_at}，本地记录 {len(records)}，服务器确认 {verify_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
