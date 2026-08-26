<#
.SYNOPSIS
    Register OpenPaper as a Windows Scheduled Task that starts at logon.

.USAGE
    powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
#>

$ErrorActionPreference = 'Stop'

$TaskName = 'OpenPaperServer'
$LegacyTaskName = 'PaperWaatchdog'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path $ScriptDir -Parent
$VbsPath = Join-Path $ScriptDir 'start_server.vbs'

function Test-PythonExecutable {
    param([string]$Path)

    if (-not $Path) {
        return $false
    }

    try {
        & $Path -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (-not (Test-Path $VbsPath)) {
    $VbsPath = Join-Path $ScriptDir 'start_watchdog.vbs'
}
if (-not (Test-Path $VbsPath)) {
    throw "Launcher script not found: $VbsPath"
}

$PythonPath = $null
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
if (Test-PythonExecutable $VenvPython) {
    $PythonPath = $VenvPython
} else {
    foreach ($Command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if (Test-PythonExecutable $Command.Source) {
            $PythonPath = $Command.Source
            break
        }
    }
}

if (-not $PythonPath) {
    $PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        $LauncherPython = & $PyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and (Test-PythonExecutable $LauncherPython)) {
            $PythonPath = $LauncherPython
        }
    }
}

if (-not $PythonPath) {
    throw "No working Python 3.8+ interpreter was found. Create .venv or repair the Python installation first."
}

& $PythonPath -c "import watchdog, pymupdf, pptx, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Python is missing OpenPaper dependencies. Run: `"$PythonPath`" -m pip install -r `"$(Join-Path $ProjectDir 'requirements.txt')`""
}

$ActionArguments = "`"$VbsPath`" `"$PythonPath`""
$Action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $ActionArguments
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$FullUser = "$env:USERDOMAIN\$env:USERNAME"

foreach ($Name in @($TaskName, $LegacyTaskName)) {
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed existing task: $Name"
    }
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User $FullUser `
    -RunLevel Limited `
    -Description 'Auto-start OpenPaper (PDF watcher + HTTP server) at user logon.' | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "  Trigger: user logon"
Write-Host "  Launcher: $VbsPath"
Write-Host "  Python: $PythonPath"
Write-Host ""
Write-Host "Starting the task once now so you can verify http://127.0.0.1:8000"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

$LogPath = Join-Path $ProjectDir 'watchdog.log'
$Listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($Listening) {
    Write-Host "Service is listening on http://127.0.0.1:8000"
} else {
    Write-Warning "Port 8000 is not listening yet. Check log: $LogPath"
}
