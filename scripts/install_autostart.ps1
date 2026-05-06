<#
.SYNOPSIS
    把 backend/server.py 注册为 Windows 任务计划，在当前用户登录时自动后台启动。

.USAGE
  powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
#>

$ErrorActionPreference = 'Stop'

$TaskName = 'OpenPaperServer'
$LegacyTaskName = 'PaperWaatchdog'
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path $ScriptDir -Parent
$VbsPath    = Join-Path $ScriptDir 'start_server.vbs'

if (-not (Test-Path $VbsPath)) {
    $VbsPath = Join-Path $ScriptDir 'start_waatchdog.vbs'
}
if (-not (Test-Path $VbsPath)) {
    throw "找不到启动器：$VbsPath"
}

$venvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not (Test-Path $venvPython) -and -not $python) {
    Write-Warning "未找到项目 .venv 或 PATH 中的 python.exe。"
    Write-Warning "请先创建虚拟环境，或确认 Python 已安装并加入 PATH。"
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

foreach ($name in @($TaskName, $LegacyTaskName)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "已移除旧任务：$name"
    }
}

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $Action `
    -Trigger     $Trigger `
    -Settings    $Settings `
    -User        $FullUser `
    -RunLevel    Limited `
    -Description 'Auto-start backend/server.py (PDF watcher + HTTP server) at user logon.' | Out-Null

Write-Host "✅ 已注册任务计划：$TaskName"
Write-Host "   触发：当前用户登录时"
Write-Host "   启动器：$VbsPath"
Write-Host ""
Write-Host "现在立即启动一次，方便你马上访问 http://127.0.0.1:8000"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "✅ 服务已监听 127.0.0.1:8000，浏览器打开 http://127.0.0.1:8000 即可。"
} else {
    Write-Warning "未检测到 8000 端口监听。请查看日志：$(Join-Path (Split-Path $ScriptDir -Parent) 'waatchdog.log')"
}
