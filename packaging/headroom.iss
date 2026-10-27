; Inno Setup script for Headroom.
; Compile on Windows after packaging\headroom.spec has produced dist\headroom.exe:
;   iscc packaging\headroom.iss
; The installer copies headroom.exe. It does not start a cleanup and it does not
; register a logon task. Run `headroom install` yourself if you want that.

#define MyAppName "Headroom"
#define MyAppVersion "0.1.1"
#define MyAppPublisher "Jasdish Singh"
#define MyAppExeName "headroom.exe"

[Setup]
AppId={{A7B1E5C2-4D18-4F6A-9C31-0E5B7A2D4F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Headroom
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=Headroom-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "Open a Headroom command prompt"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Run]
; Leave the box unchecked so setup does not launch a command the user did not ask for.
Filename: "{app}\{#MyAppExeName}"; Parameters: "status"; Description: "Show RAM and disk status"; Flags: postinstall nowait skipifsilent unchecked
