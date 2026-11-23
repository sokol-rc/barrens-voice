; Barrens Voice's installer (Inno Setup 6): `mise run installer` builds it on Windows (tools/installer.py).
; Small by design: uv.exe, bv/install.py and the release archive. What is large comes from the network
; while installing: Python and libraries through uv, plus the models for OmniVoice and the voice bank.
; Layout and updates: docs/update-strategy.md.

#ifndef AppVersion
  #error Pass /DAppVersion=<version> (tools/installer.py does)
#endif
#ifndef ReleaseRepo
  #define ReleaseRepo "rgeraskin/barrens-voice"
#endif

[Setup]
AppId={{6F2E1C55-3B8D-4E57-9C61-0B6E2A7D4F19}
AppName=Barrens Voice
AppVersion={#AppVersion}
AppPublisher=rgeraskin
AppPublisherURL=https://github.com/{#ReleaseRepo}
AppSupportURL=https://github.com/{#ReleaseRepo}/issues
DefaultDirName={localappdata}\BarrensVoice
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; Off: RedirectionGuard (on by default) is inherited by what [Run] starts, runasoriginaluser or not when Setup
; is not elevated, and the install is made of junctions - uv's Python (cpython-3.13 -> 3.13.x) and our
; app\current: traversing them failed with «untrusted mount point» (error 448). The guard protects elevated
; installers from junctions planted by a lesser user; this one runs as that same user in that user's folder.
RedirectionGuard=no
OutputDir=..\dist
; one name for every version: README links releases/latest/download/BarrensVoice-Setup.exe (the version is the release's)
OutputBaseFilename=BarrensVoice-Setup
SetupIconFile=..\companion\bv\launcher\icon.ico
UninstallDisplayIcon={app}\Barrens Voice.exe
UninstallDisplayName=Barrens Voice
Uninstallable=not IsStaging
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
SetupLogging=yes
ExtraDiskSpaceRequired=12884901888
; Setup and Uninstall wait while the launcher or the companion runs (their named mutexes, bv/launcher/app.py
; and bv/__main__.py, "Local\": this session's): installing over a running version fails in uv sync or the switch
AppMutex={code:AppMutexName}

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
ru.SelectDirLabel3=Barrens Voice установится в эту папку вместе с моделями. Нужны NVIDIA с CUDA 12.8, интернет и 12 ГиБ свободного места.

[Tasks]
Name: "desktopicon"; Description: "Значок на рабочем столе"

[Files]
Source: "..\dist\uv.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\companion\bv\install.py"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "..\companion\bv\setup_omni.py"; DestDir: "{app}\setup"; Flags: ignoreversion
; the starter a player finds in the folder, with the app's icon; the shortcuts and the sign-in go through it
Source: "..\dist\Barrens Voice.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\barrens-voice-{#AppVersion}.zip"; DestDir: "{app}\setup"; Flags: ignoreversion

[Run]
Filename: "{app}\app\current\.venv\Scripts\python.exe"; Parameters: "-m bv --install-addons"; WorkingDir: "{app}\app\current\companion"; StatusMsg: "Устанавливаю аддон в найденные папки WoW…"; Flags: waituntilterminated; Check: not IsStaging
Filename: "{app}\Barrens Voice.exe"; WorkingDir: "{app}"; Description: "Запустить Barrens Voice Launcher"; Flags: postinstall nowait skipifsilent

[Icons]
Name: "{userprograms}\Barrens Voice"; Filename: "{app}\Barrens Voice.exe"; WorkingDir: "{app}"; Check: not IsStaging
Name: "{userdesktop}\Barrens Voice"; Filename: "{app}\Barrens Voice.exe"; WorkingDir: "{app}"; Tasks: desktopicon; Check: not IsStaging

[Registry]
; the launcher's «Запускать при входе в Windows» writes this value: gone with the program
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Barrens Voice"; Flags: uninsdeletevalue dontcreatekey

[UninstallRun]
; Uninstall does not start while the launcher or the companion holds its mutex (AppMutex above: it asks to close
; Barrens Voice): this stops only what is left of this install without one - the launcher and the companion of
; this install, not other Python programs. StartsWith, not -like: "[" in a folder name is a wildcard there; the
; folder's "'" doubled for PowerShell's quotes (AppForPowerShell). Inno
; reads "{{" as "{" and leaves "}" as it is (its help, "Constants": "You do not need to double } characters")
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -Command ""$app = '{code:AppForPowerShell}\'; Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -and $_.ExecutablePath.StartsWith($app, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }"""; Flags: runhidden; RunOnceId: "StopBarrensVoice"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\setup"
Type: files; Name: "{app}\barrens-voice.install"

[Code]
function SetEnvironmentVariable(Name, Value: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

procedure CurStepChanged(CurStep: TSetupStep);
var
  App, Params: String;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  App := ExpandConstant('{app}');
  WizardForm.StatusLabel.Caption := 'Скачиваю OmniVoice и готовлю CUDA. Это может занять несколько минут…';
  SetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', App + '\python');
  SetEnvironmentVariable('UV_CACHE_DIR', App + '\cache\uv');
  SetEnvironmentVariable('UV_PYTHON_PREFERENCE', 'only-managed');
  SetEnvironmentVariable('PYTHONUTF8', '1');
  Params := 'run --no-project --python 3.13 "' + App + '\setup\setup_omni.py" --root "' + App +
    '" --archive "' + App + '\setup\barrens-voice-{#AppVersion}.zip"';
  if not ExecAndLogOutput(App + '\uv.exe', Params, App, SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode, nil) or
     (ResultCode <> 0) then
    RaiseException('Не удалось подготовить OmniVoice. Проверьте драйвер NVIDIA, интернет и свободное место.' + #13#10 +
                   'Журнал установки: ' + ExpandConstant('{log}'));
end;

function IsStaging: Boolean;
begin
  Result := ExpandConstant('{param:STAGING|0}') = '1';
end;

function AppMutexName(Param: String): String;
begin
  if IsStaging then
    Result := 'BarrensVoiceStaging'
  else
    Result := 'BarrensVoiceLauncher,BarrensVoiceCompanion';
end;

{ the install folder inside PowerShell's single quotes: a ' in it doubled }
function AppForPowerShell(Param: String): String;
begin
  Result := ExpandConstant('{app}');
  StringChangeEx(Result, '''', '''''', True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if MsgBox('Удалить и настройки, банк голосов и скачанные модели?' + #13#10 +
              'Нет — они останутся и пригодятся, если поставить Barrens Voice снова.',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      DelTree(ExpandConstant('{app}\data'), True, True, True);
      DelTree(ExpandConstant('{app}\cache'), True, True, True);
      RemoveDir(ExpandConstant('{app}'));
    end;
end;
