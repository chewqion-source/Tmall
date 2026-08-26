$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$moduleFile = Join-Path $PSScriptRoot "ai_image_app.py"
$keyFile = Join-Path $repoRoot ".ssh_tmp\tmall_codex_temp_ed25519"

if (-not (Test-Path $moduleFile)) {
    throw "Missing module file: $moduleFile"
}
if (-not (Test-Path $keyFile)) {
    throw "Missing SSH key: $keyFile"
}

Set-Location $repoRoot

python -m py_compile $moduleFile

$pendingModuleChanges = git status --porcelain -- ai_image_module .gitignore
if ($pendingModuleChanges) {
    throw "Please commit AI image module changes before deploying.`n$pendingModuleChanges"
}

$env:PYTHONIOENCODING = "utf-8"
@"
import paramiko
from pathlib import Path

host = "150.158.133.102"
user = "ubuntu"
local_file = Path(r"$moduleFile")
key_file = Path(r"$keyFile")
remote_file = "/opt/tmall-dashboard/current/ai_image_app.py"

key = paramiko.Ed25519Key.from_private_key_file(str(key_file))
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    hostname=host,
    username=user,
    pkey=key,
    timeout=30,
    banner_timeout=60,
    auth_timeout=60,
    look_for_keys=False,
    allow_agent=False,
)

stdin, stdout, stderr = client.exec_command(
    f"cp {remote_file} {remote_file}.bak.`$(date +%Y%m%d_%H%M%S)"
)
rc = stdout.channel.recv_exit_status()
if rc != 0:
    err = stderr.read().decode("utf-8", errors="replace")
    client.close()
    raise SystemExit(err)

sftp = client.open_sftp()
sftp.put(str(local_file), remote_file)
sftp.close()

cmd = (
    "cd /opt/tmall-dashboard/current "
    "&& /opt/tmall-dashboard/.venv/bin/python -m py_compile ai_image_app.py "
    "&& sudo systemctl restart tmall-ai-image.service "
    "&& sleep 3 "
    "&& systemctl is-active tmall-ai-image.service"
)
stdin, stdout, stderr = client.exec_command(cmd)
out = stdout.read().decode("utf-8", errors="replace").strip()
err = stderr.read().decode("utf-8", errors="replace").strip()
rc = stdout.channel.recv_exit_status()
client.close()

print(out)
if err:
    print(err)
raise SystemExit(rc)
"@ | python -
