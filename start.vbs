Option Explicit

Dim shell, fso, root, command, exitCode, logPath, productDir
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & root & "\launcher.ps1"""
exitCode = shell.Run(command, 0, True)

If exitCode <> 0 Then
    productDir = ChrW(&H70ED) & ChrW(&H70B9) & ChrW(&H56FE) & ChrW(&H6587) & ChrW(&H5DE5) & ChrW(&H4F5C) & ChrW(&H53F0)
    logPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\" & productDir & "\logs\launcher.log"
    MsgBox "Workbench failed to start. Check the launcher log:" & vbCrLf & logPath, 16, "Hotspot Article Agent"
End If
