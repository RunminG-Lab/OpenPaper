<#
.SYNOPSIS
    Register backend/server.py as a Windows Scheduled Task that starts at logon.

.USAGE
    powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
#>

$ErrorActionPreference = 'Stop'

$TaskName = 'OpenPaperServer'
$LegacyTaskName = 'PaperWaatchdog'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path $ScriptDir -Parent
$VbsPath = Join-Path $ScriptDir 'start_server.vbs'

if (-not (Test-Path $VbsPath)) {
    $VbsPath = Join-Path $ScriptDir 'start_waatchdog.vbs'
}
if (-not (Test-Path $VbsPath)) {
    throw "Launcher script not found: $VbsPath"
}

$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$Python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not (Test-Path $VenvPython) -and -not $Python) {
    Write-Warning "No project .venv or PATH python.exe was found."
    Write-Warning "Create the virtual environment first, or install Python and add it to PATH."
}

$Action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$VbsPath`""
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
    -Description 'Auto-start backend/server.py (PDF watcher + HTTP server) at user logon.' | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "  Trigger: user logon"
Write-Host "  Launcher: $VbsPath"
Write-Host ""
Write-Host "Starting the task once now so you can verify http://127.0.0.1:8000"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

$LogPath = Join-Path $ProjectDir 'waatchdog.log'
$Listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($Listening) {
    Write-Host "Service is listening on http://127.0.0.1:8000"
} else {
    Write-Warning "Port 8000 is not listening yet. Check log: $LogPath"
}
