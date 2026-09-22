# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
import textwrap

import paramiko

from encoding_guard import enable_utf8_stdio


enable_utf8_stdio()


BASE_DIR = Path(__file__).resolve().parent
SSH_KEY_FILE = BASE_DIR / ".ssh_tmp" / "tmall_codex_temp_ed25519"
REMOTE_HOST = "150.158.133.102"
REMOTE_USER = "ubuntu"
REMOTE_SCRIPT = "/opt/tmall-dashboard/bin/request_xhs_review_images.py"
REMOTE_TASK_FILE = "/opt/tmall-dashboard/data/tasks/realtime_task.json"


def main() -> int:
    if not SSH_KEY_FILE.exists():
        print(f"SSH Key 不存在：{SSH_KEY_FILE}")
        return 1

    script = f"""\
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime
import json
from pathlib import Path
import uuid

task_file = Path({REMOTE_TASK_FILE!r})
task_file.parent.mkdir(parents=True, exist_ok=True)
now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
task = {{
    "id": "xhs-review-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6],
    "action": "run_xhs_review_images",
    "status": "pending",
    "requested_at": now,
    "requested_by": "server_weekly_cron",
    "port": 9227,
    "max_images": 20,
}}
task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(task, ensure_ascii=False))
"""

    cron_line = f"30 9 * * 1 /usr/bin/python3 {REMOTE_SCRIPT} >> /opt/tmall-dashboard/data/tasks/xhs_review_cron.log 2>&1"
    install_cmd = f"""
set -e
mkdir -p /opt/tmall-dashboard/bin /opt/tmall-dashboard/data/tasks
cat > {REMOTE_SCRIPT} <<'PY'
{script.rstrip()}
PY
chmod +x {REMOTE_SCRIPT}
(crontab -l 2>/dev/null | grep -v '{REMOTE_SCRIPT}' || true; echo '{cron_line}') | crontab -
"""

    key = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_FILE))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
    try:
        stdin, stdout, stderr = client.exec_command(install_cmd)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="ignore").strip()
        err = stderr.read().decode("utf-8", errors="ignore").strip()
        if out:
            print(out)
        if err:
            print(err)
        if rc != 0:
            return rc
    finally:
        client.close()

    print("云服务器每周一 09:30 下发小红书评价素材抓取任务已安装。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
