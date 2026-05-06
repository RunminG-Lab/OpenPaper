<#
.SYNOPSIS
  卸载 OpenPaper 自启动任务，并尝试结束正在监听 8000 端口的 Python 进程。
#>

$ErrorActionPreference = 'SilentlyContinue'
$TaskNames = @('OpenPaperServer', 'PaperWaatchdog')

foreach ($TaskName in $TaskNames) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "✅ 已移除任务：$TaskName"
    }
}

$conns = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    try {
        $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($p -and ($p.ProcessName -match 'python')) {
            Stop-Process -Id $p.Id -Force
            Write-Host "🛑 已结束进程 $($p.ProcessName) (PID=$($p.Id))"
        }
    } catch {}
}
