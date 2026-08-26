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

sPy = ""
If WScript.Arguments.Count > 0 Then
    sPy = WScript.Arguments(0)
End If
If Not oFso.FileExists(sPy) Then
    sPy = sProjectDir & "\.venv\Scripts\python.exe"
End If
If Not oFso.FileExists(sPy) Then
    sPy = "python.exe"
End If

sLog = sProjectDir & "\watchdog.log"

sCmd = "cmd /c cd /d """ & sProjectDir & """ && " & _
       "set ""PYTHONUTF8=1"" && set ""PYTHONIOENCODING=utf-8"" && " & _
       Q(sPy) & " -m backend >> " & Q(sLog) & " 2>&1"

oShell.Run sCmd, 0, False
