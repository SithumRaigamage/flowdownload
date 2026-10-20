; Inno Setup script for the FlowDownload Windows installer.
;
; Build (from the project root, after `pyinstaller FlowDownload.spec`):
;   iscc /DAppVersion=1.2.0 packaging\windows\FlowDownload.iss
; Output: dist\FlowDownload-<version>-windows-x64-setup.exe
;
; Installs per-user (no admin prompt). The app registers its Chrome native
; messaging host itself on every launch (app/chrome_setup.py); the
; [Registry] entry below just makes sure the uninstaller removes that key.

#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=X.Y.Z FlowDownload.iss
#endif

[Setup]
AppId={{D5CCD019-AFA0-44B9-8C43-25A32DDDCC17}
AppName=FlowDownload
AppVersion={#AppVersion}
AppVerName=FlowDownload {#AppVersion}
AppPublisher=Sithum Raigamage
AppPublisherURL=https://github.com/SithumRaigamage/flowdownload
AppSupportURL=https://github.com/SithumRaigamage/flowdownload/issues
AppUpdatesURL=https://github.com/SithumRaigamage/flowdownload/releases/latest
DefaultDirName={localappdata}\Programs\FlowDownload
DefaultGroupName=FlowDownload
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=FlowDownload-{#AppVersion}-windows-x64-setup
SetupIconFile=..\..\app\AppIcon.ico
UninstallDisplayIcon={app}\FlowDownload.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Upgrades: close a running FlowDownload first so its files can be replaced.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\FlowDownload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\FlowDownload"; Filename: "{app}\FlowDownload.exe"
Name: "{autodesktop}\FlowDownload"; Filename: "{app}\FlowDownload.exe"; Tasks: desktopicon

[Registry]
; Created empty here, filled in by the app; removed on uninstall.
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.flowdownload.host"; Flags: uninsdeletekey

[Run]
Filename: "{app}\FlowDownload.exe"; Description: "{cm:LaunchProgram,FlowDownload}"; Flags: nowait postinstall skipifsilent
