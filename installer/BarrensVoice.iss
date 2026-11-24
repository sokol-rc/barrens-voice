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
ArchitecturesAllowed=x64os
MinVersion=10.0
DisableStartupPrompt=yes
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
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
; Setup and Uninstall wait while the launcher or the companion runs (their named mutexes, bv/launcher/app.py
; and bv/__main__.py, "Local\": this session's): installing over a running version fails in uv sync or the switch
AppMutex={code:AppMutexName}

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
ru.SelectDirLabel3=Barrens Voice установится в эту папку вместе с моделями. Нужны NVIDIA с CUDA 12.8, интернет и 15 ГиБ свободного места.

[Files]
Source: "..\dist\uv.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\companion\bv\install.py"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "..\companion\bv\setup_omni.py"; DestDir: "{app}\setup"; Flags: ignoreversion
; the starter a player finds in the folder, with the app's icon; the shortcuts and the sign-in go through it
Source: "..\dist\Barrens Voice.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\barrens-voice-{#AppVersion}.zip"; DestDir: "{app}\setup"; Flags: ignoreversion

[Run]
Filename: "{app}\app\current\.venv\Scripts\python.exe"; Parameters: "-m bv --install-addons"; WorkingDir: "{app}\app\current\companion"; StatusMsg: "Устанавливаю аддон в найденные папки WoW…"; Flags: waituntilterminated; Check: not IsStaging and SetupSucceeded
Filename: "{app}\Barrens Voice.exe"; WorkingDir: "{app}"; Flags: nowait skipifsilent; Check: not IsStaging and SetupSucceeded

[Icons]
Name: "{userprograms}\Barrens Voice"; Filename: "{app}\Barrens Voice.exe"; WorkingDir: "{app}"; Check: not IsStaging
Name: "{userdesktop}\Barrens Voice"; Filename: "{app}\Barrens Voice.exe"; WorkingDir: "{app}"; Check: not IsStaging

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
var
  SetupError: String;
  SetupPhase: String;

function SetEnvironmentVariable(Name, Value: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function SetupSucceeded: Boolean;
begin
  Result := SetupError = '';
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := 0;
  if not SetupSucceeded then
    Result := 20;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectDir then
    WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  App, Probe, Report: String;
  DriverLog: AnsiString;
  Free, Total: Int64;
  ResultCode: Integer;
begin
  App := WizardDirValue;
  if not GetSpaceOnDisk64(ExtractFileDrive(App) + '\', Free, Total) then
  begin
    Result := 'Не удалось проверить свободное место. Выбери локальный диск.';
    Exit;
  end;
  if (Free < 16106127360) and not FileExists(App + '\cache\.setup-started') and
     not FileExists(App + '\barrens-voice.install') then
  begin
    Result := 'Для первой установки нужно 15 ГиБ свободного места. Нажми «Назад» и выбери другой диск.';
    Exit;
  end;
  ExtractTemporaryFile('Barrens Voice.exe');
  Probe := ExpandConstant('{tmp}\Barrens Voice.exe');
  Report := ExpandConstant('{tmp}\bv-cuda.txt');
  if not Exec(Probe, '--check-cuda "' + Report + '"', ExpandConstant('{tmp}'), SW_HIDE,
              ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    Result := 'Не найден рабочий драйвер NVIDIA с поддержкой CUDA 12. Обнови драйвер и запусти установщик снова.';
    if not WizardSilent and (SuppressibleMsgBox(Result + #13#10 + 'Открыть страницу драйверов NVIDIA?',
       mbError, MB_YESNO, IDNO) = IDYES) then
      ShellExec('', 'https://www.nvidia.com/Download/index.aspx', '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
    Exit;
  end;
  if LoadStringFromFile(Report, DriverLog) then
    Log(String(DriverLog));
  Result := '';
end;

procedure SetupOutput(const S: String; const Error, FirstLine: Boolean);
begin
  Log(S);
  if Error then
    Exit;
  if Pos('BV_SETUP: core', S) = 1 then
    SetupPhase := 'Загружаю Python и компоненты программы…'
  else if Pos('BV_SETUP: cuda-error', S) = 1 then
    SetupPhase := 'Проверка CUDA FP16 не прошла. Проверь драйвер NVIDIA и доступную видеопамять.'
  else if Pos('BV_SETUP: cuda', S) = 1 then
    SetupPhase := 'Загружаю CUDA-библиотеки и проверяю видеокарту…'
  else if Pos('BV_SETUP: model', S) = 1 then
    SetupPhase := 'Загружаю модель OmniVoice…'
  else if Pos('BV_SETUP: voices', S) = 1 then
    SetupPhase := 'Загружаю модели для голосов из WoW и ударений…';
  WizardForm.StatusLabel.Caption := SetupPhase;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  App, Params: String;
  ResultCode: Integer;
  Retry: Boolean;
begin
  if CurStep <> ssPostInstall then
    Exit;
  App := ExpandConstant('{app}');
  ForceDirectories(App + '\cache');
  SaveStringToFile(App + '\cache\.setup-started', '', False);
  SetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', App + '\python');
  SetEnvironmentVariable('UV_CACHE_DIR', App + '\cache\uv');
  SetEnvironmentVariable('UV_PYTHON_PREFERENCE', 'only-managed');
  SetEnvironmentVariable('PYTHONUTF8', '1');
  Params := 'run --no-project --python 3.13 "' + App + '\setup\setup_omni.py" --root "' + App +
    '" --archive "' + App + '\setup\barrens-voice-{#AppVersion}.zip"';
  repeat
    SetupPhase := 'Готовлю загрузку…';
    WizardForm.StatusLabel.Caption := SetupPhase;
    SetupError := 'Установка прервалась. Журнал установки: ' + ExpandConstant('{log}');
    if ExecAndLogOutput(App + '\uv.exe', Params, App, SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode, @SetupOutput)
       and (ResultCode = 0) then
      SetupError := '';
    Retry := False;
    if not SetupSucceeded then
    begin
      SetupError := SetupPhase + #13#10 + 'Этап не завершился. При повторе готовые файлы будут использованы снова.' + #13#10 +
                    'Журнал установки: ' + ExpandConstant('{log}');
      Log(SetupError);
      Retry := SuppressibleMsgBox(SetupError, mbError, MB_RETRYCANCEL, IDCANCEL) = IDRETRY;
    end;
  until not Retry;
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
