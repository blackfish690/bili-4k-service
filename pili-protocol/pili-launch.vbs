' Bili 4K Play - windowless launcher for the pili:// protocol.
' Registered as:  wscript.exe //nologo "pili-launch.vbs" "%1"
' It starts pili-play.ps1 with SW_HIDE so no console window is created,
' then exits immediately. All logic stays in pili-play.ps1.
' Keep this file next to pili-play.ps1. ASCII only: safe under any code page.

Option Explicit

Dim shell, fso, scriptDir, handler, ps, uri, cmd

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count = 0 Then
    LogLine "missing URI argument"
    MsgBox "pili:// launcher: missing URI argument." & vbCrLf & _
           "Log: " & LogPath(), vbExclamation, "Bili 4K Play"
    WScript.Quit 2
End If

uri = WScript.Arguments(0)
If InStr(uri, Chr(34)) > 0 Then
    LogLine "URI contains a double quote, refused: " & uri
    MsgBox "pili:// launcher: URI contains an invalid character." & vbCrLf & _
           "Log: " & LogPath(), vbExclamation, "Bili 4K Play"
    WScript.Quit 2
End If

scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
handler = scriptDir & "pili-play.ps1"

If Not fso.FileExists(handler) Then
    LogLine "pili-play.ps1 not found: " & handler
    MsgBox "pili:// launcher: pili-play.ps1 was not found next to pili-launch.vbs." & vbCrLf & _
           handler & vbCrLf & "Log: " & LogPath(), vbCritical, "Bili 4K Play"
    WScript.Quit 3
End If

ps = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"

cmd = Chr(34) & ps & Chr(34) & " -NoProfile -ExecutionPolicy Bypass -File " & _
      Chr(34) & handler & Chr(34) & " " & Chr(34) & uri & Chr(34)

On Error Resume Next
shell.Run cmd, 0, False
If Err.Number <> 0 Then
    LogLine "failed to start PowerShell: " & Err.Description
    MsgBox "pili:// launcher: failed to start PowerShell." & vbCrLf & _
           Err.Description & vbCrLf & "Log: " & LogPath(), vbCritical, "Bili 4K Play"
    WScript.Quit 4
End If
On Error GoTo 0

WScript.Quit 0

' ---------------------------------------------------------------------------

Function LogPath()
    Dim dir
    dir = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    If dir = "" Or dir = "%LOCALAPPDATA%" Then dir = shell.ExpandEnvironmentStrings("%TEMP%")
    LogPath = dir & "\pili4k\play.log"
End Function

Sub LogLine(msg)
    Dim path, f
    path = LogPath()
    On Error Resume Next
    If Not fso.FolderExists(fso.GetParentFolderName(path)) Then
        fso.CreateFolder fso.GetParentFolderName(path)
    End If
    Set f = fso.OpenTextFile(path, 8, True)
    f.WriteLine "LAUNCHER ERROR: " & msg
    f.Close
    Err.Clear
End Sub
