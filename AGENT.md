# Local Agent and Server Dispatch

Recommended production workflow:

```text
edit locally -> push GitHub -> package/deploy server
server creates task -> local agent polls task -> local crawler runs -> upload data/status back
```

Do not edit `/opt/tmall-dashboard/current/app.py` directly on the server as the long-term source of truth. Server-side release files can be replaced during deployment. Keep durable data such as `sku_cost.xlsx`, realtime snapshots, and task/status JSON under `/opt/tmall-dashboard/data/`.

## Files

- `realtime_agent.py`: polls `/opt/tmall-dashboard/data/tasks/realtime_task.json`, runs scheduled/manual realtime crawls, syncs SKU cost before and after crawling, uploads results, and sends Feishu notifications.
- `run_realtime_agent.bat`: foreground startup for local debugging.
- `install_startup_agent.ps1`: installs and starts the hidden startup agent.

## Commands

```powershell
python realtime_agent.py --once
.\run_realtime_agent.bat
.\install_startup_agent.ps1
```

The agent polls every 30 seconds and runs scheduled crawling every 2 hours by default.

```powershell
$env:TMALL_AGENT_POLL_SECONDS="30"
$env:TMALL_AGENT_SCHEDULE_SECONDS="7200"
```

## Task Contract

Server task file:

```text
/opt/tmall-dashboard/data/tasks/realtime_task.json
```

Example manual task:

```json
{
  "id": "a1b2c3d4e5f6",
  "action": "run_realtime",
  "status": "pending",
  "requested_at": "2026-08-24 18:30:00",
  "requested_by": "dashboard"
}
```

Agent status file:

```text
/opt/tmall-dashboard/data/tasks/realtime_status.json
```

Common statuses:

- `idle`: local agent is online and waiting.
- `checking_login`: checking browser login state.
- `running`: crawler is running.
- `paused`: login/captcha blocked the run.
- `success`: crawl finished and data was uploaded.
- `failed`: a step failed.
