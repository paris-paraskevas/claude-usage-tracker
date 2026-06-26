; Inno Setup script for Claude Usage Tracker.
; Built in CI against the PyInstaller output in dist\ClaudeUsageTracker\.
; Version is passed in: ISCC.exe /DMyAppVersion=0.1.7 installer.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "Claude Usage Tracker"
#define MyAppPublisher "paris-paraskevas"
#define MyAppURL "https://github.com/paris-paraskevas/claude-usage-tracker"
#define MyAppExeName "ClaudeUsageTracker.exe"

[Setup]
AppId={{B7E5B2A0-1C3D-4E5F-8A9B-0C1D2E3F4A5B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\ClaudeUsageTracker
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=dist
OutputBaseFilename=ClaudeUsageTracker-Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startmenuicon"; Description: "Add a &Start Menu shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce
Name: "startup"; Description: "Start automatically when I sign in"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\ClaudeUsageTracker\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startmenuicon
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"; Tasks: startmenuicon
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[Messages]
FinishedLabel=Setup is done. To pin it to the taskbar, launch the app, then right-click its taskbar icon and choose "Pin to taskbar" (Windows doesn't allow apps to do this automatically).
