' 静默启动 waatchdog.py（不弹出黑色控制台窗口）
' 用法：双击或被任务计划程序调用
Option Explicit

Dim oShell, oFso, sScriptDir, sProjectDir, sPy, sLog, sCmd
Set oShell = CreateObject("WScript.Shell")
Set oFso = CreateObject("Scripting.FileSystemObject")

' 当前 vbs 所在目录
sScriptDir = oFso.GetParentFolderName(WScript.ScriptFullName)
' 项目根目录（vbs 在 scripts/ 下，向上一级）
sProjectDir = oFso.GetParentFolderName(sScriptDir)

' python.exe 的位置：默认走 PATH，找不到时也可手动改成绝对路径
' 不用 pythonw.exe 是因为它没有 stdout/stderr，print 会报错；
' 这里用 python.exe，并由下面 oShell.Run 的第二个参数 0 隐藏窗口。
sPy = "python.exe"

' 日志文件
sLog = sProjectDir & "\waatchdog.log"

' 切到项目目录后启动 waatchdog.py，stdout/stderr 重定向到日志
' 用 cmd /c 包一层是为了支持 > 重定向
' 设 PYTHONUTF8=1 / PYTHONIOENCODING=utf-8 避免 emoji 在 GBK 下崩溃
sCmd = "cmd /c cd /d """ & sProjectDir & """ && " & _
       "set ""PYTHONUTF8=1"" && set ""PYTHONIOENCODING=utf-8"" && " & _
       sPy & " waatchdog.py >> """ & sLog & """ 2>&1"

' 第二个参数 0 = 隐藏窗口，第三个参数 False = 不等待
oShell.Run sCmd, 0, False

' 第二个参数 0 = 隐藏窗口，第三个参数 False = 不等待
Set objShell = CreateObject("WScript.Shell")
objShell.Run "cmd /c python backend/server.py", 0, False
