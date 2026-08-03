Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

Sub TryLaunch(command, ByRef launched)
    On Error Resume Next
    Err.Clear
    WshShell.Run command, 0, False
    launched = (Err.Number = 0)
    Err.Clear
    On Error GoTo 0
End Sub

scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
appDir = scriptDir
installDir = FSO.GetParentFolderName(appDir)
shortcutPath = installDir & "\MesterSync.lnk"
Set Shortcut = WshShell.CreateShortcut(shortcutPath)
Shortcut.TargetPath = WScript.ScriptFullName
Shortcut.WorkingDirectory = appDir
Shortcut.IconLocation = appDir & "\mestersync_icon.ico,0"
Shortcut.Save
WshShell.CurrentDirectory = appDir
scriptPath = """" & appDir & "\MesterSync.pyw"""
launched = False
TryLaunch "pyw.exe " & scriptPath, launched
If Not launched Then TryLaunch "pythonw.exe " & scriptPath, launched
If Not launched Then TryLaunch "py.exe -3 " & scriptPath, launched
If Not launched Then TryLaunch "python.exe " & scriptPath, launched
If Not launched Then
    MsgBox "Python 3 could not be found. Install Python 3.10 or newer, then run MesterSync.vbs again.", 16, "MesterSync"
End If
