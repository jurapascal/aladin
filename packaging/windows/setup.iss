; Aladin — instalačka pro Windows (Inno Setup 6).
;
;   iscc /DAppVersion=2.30.0 /DStage=C:\cesta\k\balíku /DOutDir=dist packaging\windows\setup.iss
;
; Balík připraví packaging/stage.py (Python + zdroj hubu + spouštěč). Instaluje
; se jen pro přihlášeného uživatele, bez práv správce, do
; %LOCALAPPDATA%\ClaudeCodeHub (cesta bez mezer — míří na ni hooky Claude Code).
; Na konci se spustí spouštěč a zbytek instalace proběhne v okně hubu.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Stage
  #define Stage "..\..\dist\stage-windows"
#endif
#ifndef OutDir
  #define OutDir "..\..\dist"
#endif

[Setup]
AppId={{6E0F3C9B-5B1D-4E0B-9C7A-C1A0DE0C0DE5}
AppName=Aladin
AppVersion={#AppVersion}
AppPublisher=Aladin
AppPublisherURL=https://github.com/jurapascal/aladin
DefaultDirName={localappdata}\ClaudeCodeHub
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=Claude-Code-Hub-Setup
SetupIconFile=..\..\assets\claude-code.ico
UninstallDisplayIcon={app}\claude-code.ico
UninstallDisplayName=Aladin
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Aktualizace přes nový Setup.exe: běžící hub se zavře a po instalaci pustí.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "cs"; MessagesFile: "compiler:Languages\Czech.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\assets\claude-code.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Instalace v okně (install.ps1 -App) tenhle zástupce přepíše na nainstalovanou
; appku; do té doby vede na spouštěč, který instalaci dokončí.
Name: "{userprograms}\Aladin"; Filename: "{app}\python\pythonw.exe"; \
  Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\claude-code.ico"
Name: "{userdesktop}\Aladin"; Filename: "{app}\python\pythonw.exe"; \
  Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\claude-code.ico"

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; \
  WorkingDir: "{app}"; Description: "Spustit Aladin"; \
  Flags: nowait postinstall

[UninstallDelete]
; Po odinstalování nemá zůstat nic, co by vedlo do prázdna. Data (projekty,
; paměť, ~/.claude) zůstávají — patří uživateli, ne appce.
Type: filesandordirs; Name: "{app}"
