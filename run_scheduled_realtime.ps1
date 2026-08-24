$ErrorActionPreference = "Stop"

$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $BaseDir "logs\scheduled"
$LockFile = Join-Path $BaseDir "logs\scheduled_realtime.lock"
$Python = "D:\python\python.exe"
$RunScript = Join-Path $BaseDir "qianniu_profit_crawler_v5_5.py"
$UploadScript = Join-Path $BaseDir "upload_realtime_snapshot.py"
$SyncSkuScript = Join-Path $BaseDir "sync_sku_cost.py"
$FeishuScript = Join-Path $BaseDir "notify_feishu.py"
$LoginCheckScript = Join-Path $BaseDir "check_login_status.py"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("realtime_" + (Get-Date -Format "yyyyMMdd") + ".log")

function Write-RunLog {
    param([string]$Message)
    Add-Content -Path $LogFile -Value $Message
    Write-Host $Message
}

function Invoke-PythonStepWithRetry {
    param(
        [string]$Label,
        [string[]]$Arguments,
        [int]$MaxAttempts = 3
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        Write-RunLog "[$Label] attempt $attempt/$MaxAttempts started"
        try {
            & $Python @Arguments 2>&1 | ForEach-Object {
                Add-Content -Path $LogFile -Value $_
                Write-Host $_
            }
            $code = $LASTEXITCODE
        }
        catch {
            $code = 1
            Write-RunLog "[$Label] attempt $attempt/$MaxAttempts exception: $($_.Exception.Message)"
        }

        if ($code -eq 0) {
            if ($attempt -gt 1) {
                Write-RunLog "[$Label] retry succeeded on attempt $attempt/$MaxAttempts"
            }
            return 0
        }

        Write-RunLog "[$Label] attempt $attempt/$MaxAttempts failed, exit code: $code"
        if ($attempt -lt $MaxAttempts) {
            $delaySeconds = 15 * $attempt
            Write-RunLog "[$Label] retrying after $delaySeconds seconds..."
            Start-Sleep -Seconds $delaySeconds
        }
    }

    Write-RunLog "[$Label] failed after $MaxAttempts attempts"
    return 1
}

$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($LockFile, "OpenOrCreate", "ReadWrite", "None")

    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    $env:TMALL_NO_PAUSE = "1"

    Set-Location $BaseDir

    Write-RunLog "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') scheduled run started =========="

    & $Python $LoginCheckScript 2>&1 | ForEach-Object {
        Add-Content -Path $LogFile -Value $_
        Write-Host $_
    }
    $loginCheckCode = $LASTEXITCODE
    if ($loginCheckCode -eq 20) {
        Write-RunLog "login check blocked this run; crawler paused until login/captcha is resolved"
        Write-RunLog "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') scheduled run paused =========="
        exit 0
    }
    if ($loginCheckCode -ne 0) {
        Write-RunLog "login check failed, exit code: $loginCheckCode"
        exit $loginCheckCode
    }

    $syncPullCode = Invoke-PythonStepWithRetry -Label "sync sku cost pull" -Arguments @($SyncSkuScript, "pull")
    if ($syncPullCode -ne 0) {
        Write-RunLog "sync sku cost pull failed after retries"
        exit $syncPullCode
    }

    & $Python $RunScript 2>&1 | Tee-Object -FilePath $LogFile -Append
    $runCode = $LASTEXITCODE
    if ($runCode -ne 0) {
        Write-RunLog "crawler failed, exit code: $runCode"
        exit $runCode
    }

    $syncPushCode = Invoke-PythonStepWithRetry -Label "sync sku cost push" -Arguments @($SyncSkuScript, "push")
    if ($syncPushCode -ne 0) {
        Write-RunLog "sync sku cost push failed after retries"
        exit $syncPushCode
    }

    $uploadCode = Invoke-PythonStepWithRetry -Label "upload realtime snapshot" -Arguments @($UploadScript)
    if ($uploadCode -ne 0) {
        Write-RunLog "upload failed after retries, exit code: $uploadCode"
        exit $uploadCode
    }

    & $Python $FeishuScript 2>&1 | Tee-Object -FilePath $LogFile -Append

    Write-RunLog "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') scheduled run finished =========="
}
catch [System.IO.IOException] {
    Write-RunLog "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') previous run still active, skipped =========="
    exit 0
}
catch {
    Write-RunLog "task error: $($_.Exception.Message)"
    exit 1
}
finally {
    if ($lockStream -ne $null) {
        $lockStream.Close()
    }
}
