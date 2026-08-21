# -*- coding: utf-8 -*-
from pathlib import Path
import json
import sys

import paramiko


BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_FILE = BASE_DIR / "data" / "realtime_snapshot" / "latest.json"
SSH_KEY_FILE = BASE_DIR / ".ssh_tmp" / "tmall_codex_temp_ed25519"

REMOTE_HOST = "150.158.133.102"
REMOTE_USER = "ubuntu"
REMOTE_FILE = "/opt/tmall-dashboard/data/realtime/latest.json"


def main() -> int:
    if not SNAPSHOT_FILE.exists():
        print(f"实时快照不存在：{SNAPSHOT_FILE}")
        return 1

    payload = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    generated_at = payload.get("generated_at", "")
    if not records:
        print("实时快照没有 records，停止上传。")
        return 1

    if not SSH_KEY_FILE.exists():
        print(f"SSH Key 不存在：{SSH_KEY_FILE}")
        return 1

    key = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_FILE))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=REMOTE_HOST, username=REMOTE_USER, pkey=key, timeout=20)

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
