$ErrorActionPreference = "Stop"

$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $BaseDir "logs\scheduled"
$LockFile = Join-Path $BaseDir "logs\scheduled_realtime.lock"
$Python = "D:\python\python.exe"
$RunScript = Join-Path $BaseDir "qianniu_profit_crawler_v5_5.py"
$UploadScript = Join-Path $BaseDir "upload_realtime_snapshot.py"
$SyncSkuScript = Join-Path $BaseDir "sync_sku_cost.py"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("realtime_" + (Get-Date -Format "yyyyMMdd") + ".log")

$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($LockFile, "OpenOrCreate", "ReadWrite", "None")

    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    $env:TMALL_NO_PAUSE = "1"

    Set-Location $BaseDir

    "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') scheduled run started ==========" | Tee-Object -FilePath $LogFile -Append

    & $Python $SyncSkuScript pull 2>&1 | Tee-Object -FilePath $LogFile -Append

    & $Python $RunScript 2>&1 | Tee-Object -FilePath $LogFile -Append
    $runCode = $LASTEXITCODE
    if ($runCode -ne 0) {
        "crawler failed, exit code: $runCode" | Tee-Object -FilePath $LogFile -Append
        exit $runCode
    }

    & $Python $SyncSkuScript push 2>&1 | Tee-Object -FilePath $LogFile -Append

    & $Python $UploadScript 2>&1 | Tee-Object -FilePath $LogFile -Append
    $uploadCode = $LASTEXITCODE
    if ($uploadCode -ne 0) {
        "upload failed, exit code: $uploadCode" | Tee-Object -FilePath $LogFile -Append
        exit $uploadCode
    }

    "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') scheduled run finished ==========" | Tee-Object -FilePath $LogFile -Append
}
catch [System.IO.IOException] {
    "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') previous run still active, skipped ==========" | Tee-Object -FilePath $LogFile -Append
    exit 0
}
catch {
    "task error: $($_.Exception.Message)" | Tee-Object -FilePath $LogFile -Append
    exit 1
}
finally {
    if ($lockStream -ne $null) {
        $lockStream.Close()
    }
}
