# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import json
import logging
import os
import subprocess
import sys
import time
import uuid

import paramiko


logging.getLogger("paramiko").setLevel(logging.CRITICAL)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs" / "agent"
STATE_FILE = LOG_DIR / "agent_state.json"
LOCK_FILE = BASE_DIR / "logs" / "scheduled_realtime.lock"
SSH_KEY_FILE = BASE_DIR / ".ssh_tmp" / "tmall_codex_temp_ed25519"

PYTHON = os.environ.get("TMALL_PYTHON", r"D:\python\python.exe")
REMOTE_HOST = os.environ.get("TMALL_REMOTE_HOST", "150.158.133.102")
REMOTE_USER = os.environ.get("TMALL_REMOTE_USER", "ubuntu")
REMOTE_TASK_FILE = os.environ.get(
    "TMALL_REMOTE_TASK_FILE",
    "/opt/tmall-dashboard/data/tasks/realtime_task.json",
)
REMOTE_STATUS_FILE = os.environ.get(
    "TMALL_REMOTE_STATUS_FILE",
    "/opt/tmall-dashboard/data/tasks/realtime_status.json",
)

POLL_SECONDS = int(os.environ.get("TMALL_AGENT_POLL_SECONDS", "30"))
SCHEDULE_SECONDS = int(os.environ.get("TMALL_AGENT_SCHEDULE_SECONDS", str(2 * 60 * 60)))

LOGIN_CHECK_SCRIPT = BASE_DIR / "check_login_status.py"
SYNC_SKU_SCRIPT = BASE_DIR / "sync_sku_cost.py"
CRAWLER_SCRIPT = BASE_DIR / "qianniu_profit_crawler_v5_5.py"
UPLOAD_SCRIPT = BASE_DIR / "upload_realtime_snapshot.py"
FEISHU_SCRIPT = BASE_DIR / "notify_feishu.py"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_log_file() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"agent_{datetime.now():%Y%m%d}.log"


def write_log(message: str) -> None:
    line = f"{now_text()} {message}"
    print(line, flush=True)
    with today_log_file().open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_local_state() -> dict[str, object]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_local_state(state: dict[str, object]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def connect_sftp(max_attempts: int = 3):
    key = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_FILE))
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=REMOTE_HOST,
                username=REMOTE_USER,
                pkey=key,
                timeout=20,
                banner_timeout=40,
                auth_timeout=40,
                look_for_keys=False,
                allow_agent=False,
            )
            return client, client.open_sftp()
        except Exception as exc:
            last_error = exc
            client.close()
            write_log(f"remote connect attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"remote connect failed: {last_error}")


def ensure_remote_dir(sftp, remote_dir: str) -> None:
    parts = [part for part in remote_dir.split("/") if part]
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def read_remote_json(remote_path: str) -> dict[str, object] | None:
    client = sftp = None
    temp = LOG_DIR / f"remote_{uuid.uuid4().hex}.json"
    try:
        client, sftp = connect_sftp()
        try:
            sftp.get(remote_path, str(temp))
        except FileNotFoundError:
            return None
        return json.loads(temp.read_text(encoding="utf-8"))
    finally:
        temp.unlink(missing_ok=True)
        if sftp:
            sftp.close()
        if client:
            client.close()


def write_remote_json(remote_path: str, payload: dict[str, object]) -> None:
    client = sftp = None
    local_temp = LOG_DIR / f"upload_{uuid.uuid4().hex}.json"
    remote_temp = f"{remote_path}.tmp.{uuid.uuid4().hex}"
    try:
        local_temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        client, sftp = connect_sftp()
        ensure_remote_dir(sftp, str(Path(remote_path).parent).replace("\\", "/"))
        sftp.put(str(local_temp), remote_temp)
        try:
            sftp.remove(remote_path)
        except FileNotFoundError:
            pass
        sftp.rename(remote_temp, remote_path)
    finally:
        local_temp.unlink(missing_ok=True)
        if sftp:
            sftp.close()
        if client:
            client.close()


def update_status(status: str, **extra: object) -> None:
    payload: dict[str, object] = {
        "status": status,
        "updated_at": now_text(),
        "agent_id": os.environ.get("TMALL_AGENT_ID", os.environ.get("COMPUTERNAME", "local-agent")),
        **extra,
    }
    try:
        write_remote_json(REMOTE_STATUS_FILE, payload)
    except Exception as exc:
        write_log(f"failed to upload status {status}: {exc}")
    save_local_state({**load_local_state(), "last_status": payload})


def update_task(task: dict[str, object], status: str, **extra: object) -> None:
    payload = {**task, "status": status, "updated_at": now_text(), **extra}
    try:
        write_remote_json(REMOTE_TASK_FILE, payload)
    except Exception as exc:
        write_log(f"failed to update task {task.get('id')} to {status}: {exc}")


def run_command(label: str, args: list[str], log_file: Path, max_attempts: int = 1) -> int:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["TMALL_NO_PAUSE"] = "1"

    for attempt in range(1, max_attempts + 1):
        write_log(f"{label} attempt {attempt}/{max_attempts} started")
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{now_text()}] {label} attempt {attempt}/{max_attempts}\n")
            process = subprocess.Popen(
                args,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="")
                handle.write(line)
            code = process.wait()

        if code == 0:
            return 0
        write_log(f"{label} failed with exit code {code}")
        if attempt < max_attempts:
            time.sleep(15 * attempt)
    return code


def run_pipeline(reason: str, task: dict[str, object] | None = None) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_id = str(task.get("id")) if task else f"scheduled-{datetime.now():%Y%m%d%H%M%S}"
    log_file = LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}_{run_id}.log"

    try:
        lock_stream = LOCK_FILE.open("a+")
    except OSError as exc:
        update_status("failed", reason=reason, run_id=run_id, message=f"无法打开本地锁：{exc}")
        return 1

    try:
        try:
            import msvcrt

            msvcrt.locking(lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            update_status("skipped", reason=reason, run_id=run_id, message="上一轮抓取仍在运行，已跳过")
            return 0

        update_status("checking_login", reason=reason, run_id=run_id, log_file=str(log_file))
        login_code = run_command("login check", [PYTHON, str(LOGIN_CHECK_SCRIPT)], log_file)
        if login_code == 20:
            message = "检测到登录失效或验证码，暂停抓取，等待登录恢复"
            update_status("paused", reason=reason, run_id=run_id, message=message, log_file=str(log_file))
            if task:
                update_task(task, "paused", message=message)
            return 20
        if login_code != 0:
            message = f"登录检测失败，退出码 {login_code}"
            update_status("failed", reason=reason, run_id=run_id, message=message, log_file=str(log_file))
            if task:
                update_task(task, "failed", message=message)
            return login_code

        steps = [
            ("sync sku cost pull", [PYTHON, str(SYNC_SKU_SCRIPT), "pull"], 3),
            ("crawler", [PYTHON, str(CRAWLER_SCRIPT)], 1),
            ("sync sku cost push", [PYTHON, str(SYNC_SKU_SCRIPT), "push"], 3),
            ("upload realtime snapshot", [PYTHON, str(UPLOAD_SCRIPT)], 3),
            ("feishu notify", [PYTHON, str(FEISHU_SCRIPT)], 1),
        ]
        for label, args, attempts in steps:
            update_status("running", reason=reason, run_id=run_id, step=label, log_file=str(log_file))
            code = run_command(label, args, log_file, max_attempts=attempts)
            if code != 0:
                message = f"{label} 失败，退出码 {code}"
                update_status("failed", reason=reason, run_id=run_id, step=label, message=message, log_file=str(log_file))
                if task:
                    update_task(task, "failed", message=message)
                return code

        update_status("success", reason=reason, run_id=run_id, message="实时抓取完成并已同步网站/飞书", log_file=str(log_file))
        if task:
            update_task(task, "success", message="实时抓取完成并已同步网站/飞书")
        state = load_local_state()
        state["last_success_at"] = now_text()
        if reason == "scheduled":
            state["last_scheduled_at"] = now_text()
        if task:
            state["last_task_id"] = task.get("id")
        save_local_state(state)
        return 0
    finally:
        try:
            lock_stream.close()
        except Exception:
            pass


def parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def should_run_scheduled(state: dict[str, object]) -> bool:
    last = parse_dt(state.get("last_scheduled_at"))
    if last is None:
        return True
    return datetime.now() - last >= timedelta(seconds=SCHEDULE_SECONDS)


def poll_once() -> None:
    state = load_local_state()
    try:
        task = read_remote_json(REMOTE_TASK_FILE)
    except Exception as exc:
        write_log(f"task poll failed: {exc}")
        task = None

    if task and task.get("action") == "run_realtime":
        task_status = str(task.get("status") or "pending")
        task_id = str(task.get("id") or "")
        if task_status in {"pending", "paused"} and task_id and state.get("last_task_id") != task_id:
            write_log(f"manual task accepted: {task_id}")
            update_task(task, "running", accepted_at=now_text())
            run_pipeline("manual", task)
            return

    if should_run_scheduled(state):
        if "last_scheduled_at" not in state:
            state["last_scheduled_at"] = now_text()
            save_local_state(state)
            update_status("idle", message="本地守护进程在线，等待任务")
            return
        write_log("scheduled task due")
        run_pipeline("scheduled")
        return

    update_status("idle", message="本地守护进程在线，等待任务")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        poll_once()
        return 0

    write_log(f"agent started, poll={POLL_SECONDS}s, schedule={SCHEDULE_SECONDS}s")
    update_status("idle", message="本地守护进程已启动")
    while True:
        try:
            poll_once()
        except KeyboardInterrupt:
            write_log("agent stopped by keyboard")
            update_status("stopped", message="本地守护进程已停止")
            return 0
        except Exception as exc:
            write_log(f"agent loop error: {exc}")
            update_status("error", message=str(exc))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
