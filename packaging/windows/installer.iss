; Inno Setup script for Spec Critic (Windows desktop app).
;
; Compiled by .github/workflows/release.yml with:
;   ISCC /DMyAppVersion=3.1.0 packaging\windows\installer.iss
; and expects the PyInstaller one-folder output at dist\SpecCritic\ plus the
; third-party notices the spec writes beside it, dist\THIRD-PARTY-NOTICES.txt.
;
; Produces dist\installer\SpecCriticSetup.exe — a normal double-click
; installer with a License Agreement page the user must accept, a Start-menu
; shortcut, an optional desktop icon, and a clean uninstaller. The app is NOT
; code-signed, so Windows SmartScreen shows a
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
; License Agreement page: shows the repository's LICENSE verbatim (PolyForm
; Noncommercial 1.0.0 and its "Required Notice:" line) before the install
; location page. "I do not accept the agreement" is preselected and Next stays
; disabled until the user selects "I accept the agreement"; the only other way
; off the page is Cancel, which installs nothing. The page is shown on every
; interactive run, updates included — the in-app updater launches this
; installer with no arguments. /SILENT and /VERYSILENT skip every wizard page,
; this one too. The file must stay ASCII or UTF-8 (Inno reads a Unicode .txt
; license only as UTF-8 or UTF-16LE); tests/test_packaging_entry.py pins it.
LicenseFile=..\..\LICENSE
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
; The license terms, installed beside the app. The license's Notices clause
; requires that anyone who gets a copy of the software also gets these terms
; and the Required Notice line, and the About dialog tells the user the terms
; ship with the software. Named .txt so a double-click opens it in Notepad.
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
; The license texts of everything the app bundles (the Python interpreter,
; Tcl/Tk, and every Python package), written by spec-critic.spec through
; third_party_notices.py, which fails the build when a text is missing. A
; bundled binary must carry them (README, License). If the file is absent,
; ISCC stops with "Source file does not exist".
Source: "..\..\dist\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
