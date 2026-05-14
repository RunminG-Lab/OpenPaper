' Legacy launcher kept for backward compatibility with older task names.
Option Explicit

Dim oShell, oFso, sScriptDir, sProjectDir, sPy, sLog, sCmd
Set oShell = CreateObject("WScript.Shell")
Set oFso = CreateObject("Scripting.FileSystemObject")

Function Q(ByVal s)
    Q = Chr(34) & s & Chr(34)
End Function

sScriptDir = oFso.GetParentFolderName(WScript.ScriptFullName)
sProjectDir = oFso.GetParentFolderName(sScriptDir)

sPy = sProjectDir & "\.venv\Scripts\python.exe"
If Not oFso.FileExists(sPy) Then
    sPy = "python.exe"
End If

sLog = sProjectDir & "\waatchdog.log"

sCmd = "cmd /c cd /d """ & sProjectDir & """ && " & _
       "set ""PYTHONUTF8=1"" && set ""PYTHONIOENCODING=utf-8"" && " & _
       Q(sPy) & " -m backend.server >> " & Q(sLog) & " 2>&1"

oShell.Run sCmd, 0, False
