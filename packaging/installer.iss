; Inno Setup script: wraps dist\Kairos into a normal Windows installer (Start Menu entry, optional desktop icon, uninstaller).
;   iscc /DAppVersion=1.0.0 packaging\installer.iss     ->  dist\Kairos-Setup-1.0.0.exe
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1B7C1E-5B7D-4E5B-9C2B-4B0A7A6C1C11}
AppName=Kairos
AppVersion={#AppVersion}
AppPublisher=Kairos
DefaultDirName={autopf}\Kairos
DefaultGroupName=Kairos
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Kairos-Setup-{#AppVersion}
SetupIconFile=kairos.ico
UninstallDisplayIcon={app}\Kairos.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\Kairos\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Kairos"; Filename: "{app}\Kairos.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Kairos"; Filename: "{app}\Kairos.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Kairos.exe"; Description: "Start Kairos"; Flags: nowait postinstall skipifsilent
