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
    Add-Content -Path $LogFile -Value $Message -Encoding UTF8
    Write-Host $Message
}

function Quote-ProcessArgument {
    param([string]$Value)
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Invoke-PythonStepWithRetry {
    param(
        [string]$Label,
        [string[]]$Arguments,
        [int]$MaxAttempts = 3,
        [int]$TimeoutSeconds = 0
    )

    $finalCode = 1
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        Write-RunLog "[$Label] attempt $attempt/$MaxAttempts started"
        $code = 1
        $safeLabel = ($Label -replace '[^a-zA-Z0-9_-]', '_')
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $stdoutFile = Join-Path $LogDir ("{0}_{1}_{2}.out.log" -f $safeLabel, $stamp, $attempt)
        $stderrFile = Join-Path $LogDir ("{0}_{1}_{2}.err.log" -f $safeLabel, $stamp, $attempt)
        try {
            $process = Start-Process `
                -FilePath $Python `
                -ArgumentList $Arguments `
                -WorkingDirectory $BaseDir `
                -RedirectStandardOutput $stdoutFile `
                -RedirectStandardError $stderrFile `
                -NoNewWindow `
                -PassThru

            if ($TimeoutSeconds -gt 0) {
                $finished = $process.WaitForExit($TimeoutSeconds * 1000)
                if (-not $finished) {
                    Write-RunLog "[$Label] timeout after $TimeoutSeconds seconds; killing process"
                    try {
                        Stop-Process -Id $process.Id -Force
                    }
                    catch {
                        try { $process.Kill() } catch {}
                    }
                    try { $process.WaitForExit() } catch {}
                    $code = 124
                }
                else {
                    $process.Refresh()
                    $code = $process.ExitCode
                }
            }
            else {
                $process.WaitForExit()
                $process.Refresh()
                $code = $process.ExitCode
            }

            if ($null -eq $code) {
                $code = 1
                Write-RunLog "[$Label] process exited but no exit code was reported; treating as failure"
            }
        }
        catch {
            $code = 1
            Write-RunLog "[$Label] attempt $attempt/$MaxAttempts exception: $($_.Exception.Message)"
        }

        foreach ($stepLog in @($stdoutFile, $stderrFile)) {
            if (Test-Path $stepLog) {
                Get-Content -Path $stepLog -Encoding UTF8 | ForEach-Object {
                    Write-RunLog $_
                }
            }
        }

        if ($code -eq 0) {
            if ($attempt -gt 1) {
                Write-RunLog "[$Label] retry succeeded on attempt $attempt/$MaxAttempts"
            }
            return 0
        }

        $finalCode = $code
        Write-RunLog "[$Label] attempt $attempt/$MaxAttempts failed, exit code: $code"
        if ($attempt -lt $MaxAttempts) {
            $delaySeconds = 15 * $attempt
            Write-RunLog "[$Label] retrying after $delaySeconds seconds..."
            Start-Sleep -Seconds $delaySeconds
        }
    }

    Write-RunLog "[$Label] failed after $MaxAttempts attempts"
    return $finalCode
}

$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($LockFile, "OpenOrCreate", "ReadWrite", "None")

    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    $env:TMALL_NO_PAUSE = "1"

    Set-Location $BaseDir

    Write-RunLog "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') scheduled run started =========="

    $loginCheckCode = Invoke-PythonStepWithRetry -Label "login check" -Arguments @($LoginCheckScript) -MaxAttempts 1 -TimeoutSeconds 120
    if ($loginCheckCode -eq 20) {
        Write-RunLog "login check blocked this run; crawler paused until login/captcha is resolved"
        Write-RunLog "========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') scheduled run paused =========="
        exit 0
    }
    if ($loginCheckCode -ne 0) {
        Write-RunLog "login check failed, exit code: $loginCheckCode"
        exit $loginCheckCode
    }

    $syncPullCode = Invoke-PythonStepWithRetry -Label "sync sku cost pull" -Arguments @($SyncSkuScript, "pull") -TimeoutSeconds 180
    if ($syncPullCode -ne 0) {
        Write-RunLog "sync sku cost pull failed after retries"
        exit $syncPullCode
    }

    $runCode = Invoke-PythonStepWithRetry -Label "crawler" -Arguments @($RunScript) -MaxAttempts 2 -TimeoutSeconds 1800
    if ($runCode -ne 0) {
        Write-RunLog "crawler failed, exit code: $runCode"
        exit $runCode
    }

    $syncPushCode = Invoke-PythonStepWithRetry -Label "sync sku cost push" -Arguments @($SyncSkuScript, "push") -TimeoutSeconds 180
    if ($syncPushCode -ne 0) {
        Write-RunLog "sync sku cost push failed after retries"
        exit $syncPushCode
    }

    $uploadCode = Invoke-PythonStepWithRetry -Label "upload realtime snapshot" -Arguments @($UploadScript) -TimeoutSeconds 180
    if ($uploadCode -ne 0) {
        Write-RunLog "upload failed after retries, exit code: $uploadCode"
        exit $uploadCode
    }

    $feishuCode = Invoke-PythonStepWithRetry -Label "feishu notify" -Arguments @($FeishuScript) -MaxAttempts 1 -TimeoutSeconds 120
    if ($feishuCode -ne 0) {
        Write-RunLog "feishu notify failed, exit code: $feishuCode"
        exit $feishuCode
    }

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
