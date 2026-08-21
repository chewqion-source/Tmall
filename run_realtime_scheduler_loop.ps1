$ErrorActionPreference = "Stop"

$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner = Join-Path $BaseDir "run_scheduled_realtime.ps1"
$LogDir = Join-Path $BaseDir "logs\scheduled"
$StateFile = Join-Path $LogDir "scheduler_state.json"
$LoopLog = Join-Path $LogDir "scheduler_loop.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-LoopLog($Message) {
    ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) |
        Tee-Object -FilePath $LoopLog -Append | Out-Null
}

function Get-LastSlot {
    if (-not (Test-Path $StateFile)) {
        return ""
    }
    try {
        $state = Get-Content -Path $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        return [string]$state.last_slot
    }
    catch {
        return ""
    }
}

function Set-LastSlot($Slot) {
    @{ last_slot = $Slot } |
        ConvertTo-Json |
        Set-Content -Path $StateFile -Encoding UTF8
}

Write-LoopLog "scheduler loop started"

while ($true) {
    try {
        $now = Get-Date
        $inWindow = (
            ($now.Hour -ge 9) -and
            (
                ($now.Hour -lt 23) -or
                (($now.Hour -eq 23) -and ($now.Minute -le 59))
            )
        )

        $isTwoHourSlot = (
            ($now.Minute -eq 0) -and
            ((($now.Hour - 9) % 2) -eq 0)
        )

        if ($inWindow -and $isTwoHourSlot) {
            $slot = $now.ToString("yyyyMMddHHmm")
            $lastSlot = Get-LastSlot

            if ($slot -ne $lastSlot) {
                Write-LoopLog "run started: $slot"
                Set-LastSlot $slot
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runner
                Write-LoopLog "run finished: $slot, exit code $LASTEXITCODE"
            }
        }
    }
    catch {
        Write-LoopLog "scheduler error: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds 60
}
