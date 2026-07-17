; Inno Setup script for Spec Critic (Windows desktop app).
;
; Compiled by .github/workflows/release.yml with:
;   ISCC /DMyAppVersion=3.1.0 packaging\windows\installer.iss
; and expects the PyInstaller one-folder output at dist\SpecCritic\.
;
; Produces dist\installer\SpecCriticSetup.exe — a normal double-click
; installer with a Start-menu shortcut, an optional desktop icon, and a clean
; uninstaller. The app is NOT code-signed, so Windows SmartScreen shows a
; "Windows protected your PC" notice on first run (More info -> Run anyway);
; that is expected and documented in docs/RELEASE_WINDOWS.md and the README.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "Spec Critic"
#define MyAppPublisher "Abraham Borg"
#define MyAppExeName "SpecCritic.exe"
#define MyAppURL "https://github.com/Abe-Borg/Claude-Spec-Critic"

[Setup]
; A stable AppId ties every version together so an install upgrades in place
; instead of stacking side-by-side. Unique to Spec Critic (NOT shared with any
; sibling app). Do NOT change this GUID across releases.
AppId={{8C2FB872-0DDD-49B8-919E-10FF1C12FF22}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases/latest
DefaultDirName={autopf}\Spec Critic
DefaultGroupName=Spec Critic
DisableProgramGroupPage=yes
; Per-user install: no admin/UAC prompt and no install-mode dialog, which keeps
; the unsigned experience as smooth as possible (the user only sees the one
; SmartScreen notice). "commandline" (vs "dialog") means an interactive
; double-click install is unconditionally per-user; power users can still pass
; /ALLUSERS on the command line for a machine-wide install.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=..\..\dist\installer
OutputBaseFilename=SpecCriticSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; Let an in-place update replace the running app: Inno detects a running
; instance and offers to close it. Pairs with the in-app updater, which exits
; the app before launching this installer.
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The entire PyInstaller one-folder output.
Source: "..\..\dist\SpecCritic\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
