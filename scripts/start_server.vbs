' 静默启动 backend/server.py（不弹出控制台窗口）
' 用法：双击执行，或由任务计划调用。
Option Explicit

Dim oShell, oFso, sScriptDir, sProjectDir, sPy, sServerScript, sLog, sCmd
Set oShell = CreateObject("WScript.Shell")
Set oFso = CreateObject("Scripting.FileSystemObject")

Function Q(ByVal s)
    Q = Chr(34) & s & Chr(34)
End Function

' 当前 vbs 所在目录
sScriptDir = oFso.GetParentFolderName(WScript.ScriptFullName)
' 项目根目录（vbs 在 scripts/ 下，向上一级）
sProjectDir = oFso.GetParentFolderName(sScriptDir)
sServerScript = sProjectDir & "\backend\server.py"

' 优先使用项目内虚拟环境；不存在时退回 PATH 中的 python.exe
' 不用 pythonw.exe 是因为它没有 stdout/stderr，print 会报错；
' 这里用 python.exe，并由下面 oShell.Run 的第二个参数 0 隐藏窗口。
sPy = sProjectDir & "\.venv\Scripts\python.exe"
If Not oFso.FileExists(sPy) Then
    sPy = "python.exe"
End If

' 日志文件
sLog = sProjectDir & "\waatchdog.log"

' 切到项目目录后启动 backend/server.py，stdout/stderr 重定向到日志
' 用 cmd /c 包一层是为了支持 > 重定向
' 设置 PYTHONUTF8=1 / PYTHONIOENCODING=utf-8，避免中文日志在 GBK 下崩溃
sCmd = "cmd /c cd /d """ & sProjectDir & """ && " & _
       "set ""PYTHONUTF8=1"" && set ""PYTHONIOENCODING=utf-8"" && " & _
    Q(sPy) & " " & Q(sServerScript) & " >> " & Q(sLog) & " 2>&1"

' 第二个参数 0 = 隐藏窗口，第三个参数 False = 不等待
oShell.Run sCmd, 0, False

