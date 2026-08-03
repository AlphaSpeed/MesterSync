#ifndef MyAppVersion
  #define MyAppVersion "2.0.4"
#endif

#define MyAppName "MesterSync"
#define MyAppPublisher "AlphaSpeed"
#define MyAppExeName "MesterSync.exe"

[Setup]
AppId={{A5054BA4-4899-46F4-BD62-58366EDB9E60}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/AlphaSpeed/MesterSync
AppSupportURL=https://github.com/AlphaSpeed/MesterSync/issues
AppUpdatesURL=https://github.com/AlphaSpeed/MesterSync/releases
DefaultDirName={localappdata}\Programs\MesterSync
DefaultGroupName=MesterSync
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=..\dist\installer
OutputBaseFilename=MesterSync-Setup-{#MyAppVersion}
SetupIconFile=..\app\mestersync_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=no
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\MesterSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\MesterSync"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\MesterSync"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--migrate-from=""{param:MIGRATEFROM|{src}}"""; Description: "Open MesterSync"; Flags: nowait; Check: ShouldLaunchApp

[Code]
function ShouldLaunchApp(): Boolean;
begin
  Result := ExpandConstant('{param:NOLAUNCH|0}') <> '1';
end;
