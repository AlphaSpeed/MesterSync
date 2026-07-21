Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
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
WshShell.Run "pyw.exe """ & appDir & "\MesterSync.pyw""", 0, False
