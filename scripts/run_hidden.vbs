' run_hidden.vbs — launch the agent watchdog with NO visible window.
'
' A scheduled task that runs powershell.exe directly in an interactive session
' shows a console window even with -WindowStyle Hidden (that flag only hides
' windows the script itself spawns, not the host console Task Scheduler creates).
' Launching through wscript.exe + WshShell.Run(..., 0, ...) starts PowerShell
' with a hidden window, so nothing flashes on screen at logon.
'
' Scheduled-task action:
'   Execute   : wscript.exe
'   Arguments : "<repo>\scripts\run_hidden.vbs" <target>
'   <target>  : agent | proxy
Option Explicit

Dim sh, fso, scriptDir, root, target, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)   ' ...\scripts
root      = fso.GetParentFolderName(scriptDir)                ' repo root

If WScript.Arguments.Count >= 1 Then
    target = WScript.Arguments(0)
Else
    target = "agent"
End If

' Run the watchdog from the repo root so relative paths resolve as they do
' under start_agent.bat.
sh.CurrentDirectory = root

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ _
    & scriptDir & "\agent_watchdog.ps1"" -Target " & target

' 0 = hidden window; False = don't wait (the watchdog runs on independently).
sh.Run cmd, 0, False
