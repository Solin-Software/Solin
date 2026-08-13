; =============================================================================
;  Solin — Update Installer (Patch)
;  patch.iss  |  Inno Setup 6.3+
;
;  PURPOSE:
;    Distributes a new version without requiring the user to reinstall from
;    scratch. Reads the previous install path from the registry, closes the
;    app if running, replaces changed files, and updates the version key.
;    Does not recreate or modify shortcuts.
;
;  FLOW:
;    1. Reads HKCU\Software\Solin\Solin → InstallPath (per-user install)
;          or HKLM\Software\Solin\Solin → InstallPath (machine-wide install)
;    2. Aborts with a clear message if Solin is not installed.
;    3. Aborts if the installed version is already >= patch target version.
;    4. Closes the running app gracefully (WM_CLOSE + TerminateProcess fallback).
;    5. Ensures Microsoft Edge WebView2 Runtime is available when needed.
;    6. Copies updated files to the existing install directory.
;    7. Updates the "Version" registry key and Add/Remove Programs entry.
;
;  HOW TO BUILD A PATCH:
;    - Place the full main.dist output in MyDistDir (or list only changed files
;      using Option B in [Files] for a smaller download).
;    - Compile with ISCC.exe /DMyPatchVersion=<version> patch.iss.
;    - Compile this .iss → distribute the generated .exe.
;
;  IMPORTANT:
;    - AppId MUST match setup.iss so Windows recognises this as the same
;      product in Add/Remove Programs and the uninstall log is appended to
;      (UninstallLogMode=append, the Inno default).
;    - CreateUninstallRegKey=no → the original uninstaller is preserved.
; =============================================================================

#define MyAppName       "Solin"
#define MyAppPublisher  "Solin Software"
#define MyAppURL        "https://solinav.vercel.app"
#define MyAppExeName    "Solin.exe"
#define MyAppMutex      "Solin_SingleInstance_Mutex"
#define MyRegSubkey     "Software\Solin\Solin"
#define MyPlaylistProgId "Solin.Playlist"
#define MyPlaylistMime  "application/vnd.solin.playlist+zip"
#define MyPatchFromVer  "1.0.0.0"   ; minimum installed version this patch accepts
#ifndef MyDistDir
  #define MyDistDir     "..\..\..\build\diff" ; release diff directory
#endif

#ifndef MyPatchVersion
  #define MyPatchVersion GetFileVersion(MyDistDir + "\" + MyAppExeName)
#endif

#if MyPatchVersion == ""
  #error MyPatchVersion was not supplied and could not be read from build\main.dist\Solin.exe.
#endif

#ifndef MyPatchVersion
  #error MyPatchVersion must be supplied by the build pipeline.
#endif

#ifndef MyArtifactSuffix
  #define MyArtifactSuffix ""
#endif

#define MyVirtualCameraVersion MyPatchVersion

; =============================================================================
[Setup]
; ── MUST be identical to setup.iss ───────────────────────────────────────────
AppId={{B7E4D2A1-9C3F-4E8B-A5D6-E7F8G9H0I1J2}
AppName={#MyAppName}
AppVersion={#MyPatchVersion}
AppVerName={#MyAppName} {#MyPatchVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}

; ── Install path comes from registry, not from user input ─────────────────────
DefaultDirName={code:GetInstallPath}

; ── Patch shows only the Ready and Finished pages ────────────────────────────
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
DisableFinishedPage=no

; ── Keep original uninstaller — patch entries are appended to unins*.dat ──────
CreateUninstallRegKey=no

; ── Define o nome limpo no Painel de Controle ────────────────────────────────
UninstallDisplayName={#MyAppName}
UninstallDisplaySize=441450496

; ── Mutex: blocks simultaneous installs ───────────────────────────────────────
AppMutex={#MyAppMutex}

; ── Close running process automatically ──────────────────────────────────────
CloseApplications=yes
CloseApplicationsFilter=*{#MyAppExeName}*
RestartApplications=no

; ── Output ────────────────────────────────────────────────────────────────────
#define MyPatchFileVersion \
    Copy(MyPatchVersion, 1, RPos(".", MyPatchVersion) - 1)
    
OutputDir=..\..\..\build\installer_output
OutputBaseFilename=Solin_Patch_{#MyPatchFileVersion}{#MyArtifactSuffix}
SetupIconFile=..\..\..\src\solin\resources\assets\icon.ico

#ifdef MySignToolName
SignTool={#MySignToolName}
SignedUninstaller=yes
#endif

; ── Define o ícone no Painel de Controle (Adicionar/Remover Programas) ──
UninstallDisplayIcon={app}\{#MyAppExeName}

; ── Compression ───────────────────────────────────────────────────────────────
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
ChangesAssociations=yes

; ── Privilege handling ────────────────────────────────────────────────────────
; Mirrors setup.iss: per-user by default, elevation optional via dialog.
; The code reads from HKCU first (per-user install), then HKLM (machine install).
; If the original install was machine-wide the user will be prompted to elevate.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763

; ── Versioning ────────────────────────────────────────────────────────────────
VersionInfoVersion={#MyPatchVersion}
VersionInfoDescription={#MyAppName} Update {#MyPatchVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyPatchVersion}

; =============================================================================
[Languages]
Name: "english";    MessagesFile: "compiler:Default.isl"
Name: "portuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "spanish";    MessagesFile: "compiler:Languages\Spanish.isl"
Name: "french";     MessagesFile: "compiler:Languages\French.isl"
Name: "italian";    MessagesFile: "compiler:Languages\Italian.isl"

; =============================================================================
[Files]
; ── Option A — Copy the full dist (simpler, ensures consistency) ──────────────
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\..\src\solin\resources\assets\playlist.ico"; DestDir: "{app}\resources\assets"; Flags: ignoreversion
; Register the patch's immutable per-user DirectShow filters in both registry
; views. Existing consumers may continue using the preceding version until exit.
Source: "{#MyDistDir}\native\media-engine\virtual-camera\x64\solin-virtual-camera.dll"; DestDir: "{localappdata}\Solin\VirtualCamera\versions\{#MyPatchVersion}\x64"; Flags: ignoreversion regserver 64bit uninsrestartdelete
Source: "{#MyDistDir}\native\media-engine\virtual-camera\x86\solin-virtual-camera.dll"; DestDir: "{localappdata}\Solin\VirtualCamera\versions\{#MyPatchVersion}\x86"; Flags: ignoreversion regserver 32bit uninsrestartdelete; BeforeInstall: MaybeInjectVirtualCameraX86RegistrationFailure

; =============================================================================
[Registry]
; ── 1. Define o que é uma Imagem, Vídeo e Áudio para o Solin ──
; Imagem
Root: HKA; Subkey: "Software\Classes\Solin.Image"; ValueType: string; ValueName: ""; ValueData: "Arquivo de Imagem do Solin"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Solin.Image\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"
Root: HKA; Subkey: "Software\Classes\Solin.Image\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

; Vídeo
Root: HKA; Subkey: "Software\Classes\Solin.Video"; ValueType: string; ValueName: ""; ValueData: "Arquivo de Vídeo do Solin"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Solin.Video\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"
Root: HKA; Subkey: "Software\Classes\Solin.Video\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

; Áudio
Root: HKA; Subkey: "Software\Classes\Solin.Audio"; ValueType: string; ValueName: ""; ValueData: "Arquivo de Áudio do Solin"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Solin.Audio\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"
Root: HKA; Subkey: "Software\Classes\Solin.Audio\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

; ── 2. Adiciona o Solin na lista de "Abrir com..." educadamente ──
; Imagens
Root: HKA; Subkey: "Software\Classes\.png\OpenWithProgids"; ValueType: string; ValueName: "Solin.Image"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.jpg\OpenWithProgids"; ValueType: string; ValueName: "Solin.Image"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.jpeg\OpenWithProgids"; ValueType: string; ValueName: "Solin.Image"; ValueData: ""; Flags: uninsdeletevalue

; Vídeos
Root: HKA; Subkey: "Software\Classes\.mp4\OpenWithProgids"; ValueType: string; ValueName: "Solin.Video"; ValueData: ""; Flags: uninsdeletevalue

; Áudios
Root: HKA; Subkey: "Software\Classes\.mp3\OpenWithProgids"; ValueType: string; ValueName: "Solin.Audio"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.wav\OpenWithProgids"; ValueType: string; ValueName: "Solin.Audio"; ValueData: ""; Flags: uninsdeletevalue

; ── Native Solin playlist format ─────────────────────────────────────────────
; These entries are duplicated deliberately: the Check functions select the
; hive discovered from the original installation rather than the patch
; process's current HKA mode.
Root: HKCU; Subkey: "Software\Classes\.solinplaylist\OpenWithProgids"; ValueType: none; ValueName: "{#MyPlaylistProgId}"; Flags: uninsdeletevalue; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: ""; ValueData: "Solin Playlist"; Flags: uninsdeletekey; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "Solin Playlist"; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: "Content Type"; ValueData: "{#MyPlaylistMime}"; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\{#MyPlaylistProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\playlist.ico,0"; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\{#MyPlaylistProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: none; ValueName: ".solinplaylist"; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Open native Solin playlists"; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: "{app}\resources\assets\icon.ico,0"; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\Solin\Capabilities\FileAssociations"; ValueType: string; ValueName: ".solinplaylist"; ValueData: "{#MyPlaylistProgId}"; Check: IsPatchUserInstall
Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: "Software\Solin\Capabilities"; Flags: uninsdeletevalue; Check: IsPatchUserInstall

Root: HKLM; Subkey: "Software\Classes\.solinplaylist\OpenWithProgids"; ValueType: none; ValueName: "{#MyPlaylistProgId}"; Flags: uninsdeletevalue; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: ""; ValueData: "Solin Playlist"; Flags: uninsdeletekey; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "Solin Playlist"; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: "Content Type"; ValueData: "{#MyPlaylistMime}"; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\{#MyPlaylistProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\playlist.ico,0"; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\{#MyPlaylistProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\Applications\{#MyAppExeName}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: none; ValueName: ".solinplaylist"; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Open native Solin playlists"; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: "{app}\resources\assets\icon.ico,0"; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\Solin\Capabilities\FileAssociations"; ValueType: string; ValueName: ".solinplaylist"; ValueData: "{#MyPlaylistProgId}"; Check: IsPatchMachineInstall
Root: HKLM; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: "Software\Solin\Capabilities"; Flags: uninsdeletevalue; Check: IsPatchMachineInstall

; ── Option B — List only changed files (smaller download) ────────────────────
; Comment the line above and uncomment/adjust lines below.
; Source: "{#MyDistDir}\Solin.exe";                  DestDir: "{app}"; Flags: ignoreversion
; Source: "{#MyDistDir}\some_updated.dll";            DestDir: "{app}"; Flags: ignoreversion
; Source: "..\..\..\src\solin\resources\translations\*";                  DestDir: "{app}\resources\translations"; Flags: ignoreversion
; Source: "..\..\..\src\solin\resources\translations\locales\*.json";     DestDir: "{app}\resources\translations\locales"; Flags: ignoreversion


; =============================================================================
[Run]
Filename: "{app}\{#MyAppExeName}"; Flags: nowait

; =============================================================================
[Code]
(*
  ============================================================================
  Pascal Script — Patch Installer
  ============================================================================
  Responsibilities:
    1. Read install path from registry (HKCU or HKLM) → DefaultDirName.
    2. Verify installed version is within the accepted range for this patch.
    3. Detect and close the app if running.
    4. Abort with a clear message if Solin is not installed.
    5. Ensure Microsoft Edge WebView2 Runtime is available when needed.
    6. Update Version key and Add/Remove Programs DisplayVersion after install.
  ============================================================================
*)

// ── Win32 API ─────────────────────────────────────────────────────────────────
function FindWindowEx(hWndParent: HWND; hWndChild: HWND; lpszClass: String; lpszWindow: String): HWND;
  external 'FindWindowExW@user32.dll stdcall';

function PostMessage(hWnd: HWND; Msg: Cardinal; wParam: LongInt; lParam: LongInt): BOOL;
  external 'PostMessageW@user32.dll stdcall';

function GetWindowThreadProcessId(hWnd: HWND; var lpdwProcessId: DWORD): DWORD;
  external 'GetWindowThreadProcessId@user32.dll stdcall';

function OpenProcess(dwDesiredAccess: DWORD; bInheritHandle: BOOL; dwProcessId: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall';

function TerminateProcess(hProcess: THandle; uExitCode: UINT): BOOL;
  external 'TerminateProcess@kernel32.dll stdcall';

function CloseHandle(hObject: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

type
  TMsg = record
    hwnd: HWND;
    message: Cardinal;
    wParam: LongInt;
    lParam: LongInt;
    time: DWORD;
    pt_x: LongInt;
    pt_y: LongInt;
  end;

  TShellExecuteInfo = record
    cbSize: DWORD;
    fMask: Cardinal;
    Wnd: HWND;
    lpVerb: String;
    lpFile: String;
    lpParameters: String;
    lpDirectory: String;
    nShow: Integer;
    hInstApp: THandle;
    lpIDList: LongInt;
    lpClass: String;
    hkeyClass: THandle;
    dwHotKey: DWORD;
    hMonitor: THandle;
    hProcess: THandle;
  end;

function ShellExecuteEx(var lpExecInfo: TShellExecuteInfo): BOOL;
  external 'ShellExecuteExW@shell32.dll stdcall';

function WaitForSingleObject(hHandle: THandle; dwMilliseconds: DWORD): DWORD;
  external 'WaitForSingleObject@kernel32.dll stdcall';

function GetExitCodeProcess(hProcess: THandle; var lpExitCode: DWORD): BOOL;
  external 'GetExitCodeProcess@kernel32.dll stdcall';

function PeekMessage(var lpMsg: TMsg; hWnd: HWND; wMsgFilterMin: Cardinal; wMsgFilterMax: Cardinal; wRemoveMsg: Cardinal): BOOL;
  external 'PeekMessageW@user32.dll stdcall';

function TranslateMessage(const lpMsg: TMsg): BOOL;
  external 'TranslateMessage@user32.dll stdcall';

function DispatchMessage(const lpMsg: TMsg): LongInt;
  external 'DispatchMessageW@user32.dll stdcall';

const
  WM_CLOSE          = $0010;
  PROCESS_TERMINATE = $0001;
  SEE_MASK_NOCLOSEPROCESS = $00000040;
  WAIT_TIMEOUT = $00000102;
  WAIT_FAILED = $FFFFFFFF;
  PM_REMOVE = $0001;
  WEBVIEW2_CLIENT_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_BOOTSTRAPPER_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';
  WEBVIEW2_BOOTSTRAPPER_EXE = 'MicrosoftEdgeWebView2Setup.exe';

procedure PumpMessages();
var
  Msg: TMsg;
begin
  while PeekMessage(Msg, 0, 0, 0, PM_REMOVE) do
  begin
    TranslateMessage(Msg);
    DispatchMessage(Msg);
  end;
end;

function ExecAndWaitResponsive(const Filename, Params, WorkingDir: String; ShowCmd: Integer; var ResultCode: Integer): Boolean;
var
  ExecInfo: TShellExecuteInfo;
  WaitResult: DWORD;
  ExitCode: DWORD;
begin
  Result := False;
  ResultCode := -1;

  ExecInfo.cbSize := SizeOf(ExecInfo);
  ExecInfo.fMask := SEE_MASK_NOCLOSEPROCESS;
  ExecInfo.Wnd := WizardForm.Handle;
  ExecInfo.lpVerb := 'open';
  ExecInfo.lpFile := Filename;
  ExecInfo.lpParameters := Params;
  ExecInfo.lpDirectory := WorkingDir;
  ExecInfo.nShow := ShowCmd;
  ExecInfo.hInstApp := 0;
  ExecInfo.lpIDList := 0;
  ExecInfo.lpClass := '';
  ExecInfo.hkeyClass := 0;
  ExecInfo.dwHotKey := 0;
  ExecInfo.hMonitor := 0;
  ExecInfo.hProcess := 0;

  if not ShellExecuteEx(ExecInfo) then
    Exit;

  if ExecInfo.hProcess = 0 then
  begin
    ResultCode := 0;
    Result := True;
    Exit;
  end;

  repeat
    WaitResult := WaitForSingleObject(ExecInfo.hProcess, 100);
    PumpMessages();
    WizardForm.Refresh();
  until WaitResult <> WAIT_TIMEOUT;

  if WaitResult <> WAIT_FAILED then
  begin
    ExitCode := 0;
    if GetExitCodeProcess(ExecInfo.hProcess, ExitCode) then
      ResultCode := Integer(ExitCode)
    else
      ResultCode := 0;
    Result := True;
  end;

  CloseHandle(ExecInfo.hProcess);
end;

#include "virtual_camera_registration.iss"

// ── Cached install info populated in InitializeSetup ─────────────────────────
var
  GInstallPath:  String;  // full path to the existing install directory
  GInstallHive:  Integer; // HKLM or HKCU — where the installer keys were written

function IsPatchUserInstall(): Boolean;
begin
  Result := GInstallHive = HKCU;
end;

function IsPatchMachineInstall(): Boolean;
begin
  Result := GInstallHive = HKLM;
end;

// ── Locate the existing install: HKCU first (per-user), then HKLM (machine) ──

function FindInstallPath(out OutPath: String; out OutHive: Integer): Boolean;
var
  Path: String;
begin
  Result := False;
  OutPath := '';
  OutHive := HKCU;

  if RegQueryStringValue(HKCU, '{#MyRegSubkey}', 'InstallPath', Path) and (Path <> '') then
  begin
    OutPath := Path;
    OutHive := HKCU;
    Result := True;
    Exit;
  end;

  if RegQueryStringValue(HKLM, '{#MyRegSubkey}', 'InstallPath', Path) and (Path <> '') then
  begin
    OutPath := Path;
    OutHive := HKLM;
    Result := True;
  end;
end;

// ── Callback used by DefaultDirName={code:GetInstallPath} ────────────────────

function GetInstallPath(Param: String): String;
begin
  if GInstallPath <> '' then
    Result := GInstallPath
  else
    Result := ExpandConstant('{localappdata}\Programs\{#MyAppName}'); // safe fallback (should never hit)
end;

// ── Retrieve currently installed version from the detected hive ───────────────

function GetInstalledVersion(): String;
var
  Ver: String;
begin
  if RegQueryStringValue(GInstallHive, '{#MyRegSubkey}', 'Version', Ver) then
    Result := Ver
  else
    Result := '';
end;

// ── Semantic version comparison: returns -1 / 0 / +1 ─────────────────────────
// Supports both "major.minor.patch" and "major.minor.patch.build" formats.

// ── Função auxiliar movida para fora ─────────────────────────────────────────
function NextPart(var S: String): Integer;
var
  P: Integer;
begin
  P := Pos('.', S);
  if P > 0 then
  begin
    Result := StrToIntDef(Copy(S, 1, P - 1), 0);
    S := Copy(S, P + 1, Length(S));
  end
  else
  begin
    Result := StrToIntDef(S, 0);
    S := '';
  end;
end;

// ── Semantic version comparison: returns -1 / 0 / +1 ─────────────────────────
// Supports both "major.minor.patch" and "major.minor.patch.build" formats.
function CompareVersions(A, B: String): Integer;
var
  i, av, bv: Integer;
begin
  Result := 0;
  for i := 1 to 4 do
  begin
    av := NextPart(A);
    bv := NextPart(B);
    if av <> bv then
    begin
      if av < bv then Result := -1 else Result := 1;
      Exit;
    end;
  end;
end;

// ── Running process detection ─────────────────────────────────────────────────

function IsAppRunning(): Boolean;
begin
  Result := (FindWindowEx(0, 0, '', '{#MyAppName}') <> 0) or
            CheckForMutexes('{#MyAppMutex}');
end;

procedure ForceCloseApp();
var
  hWnd:    HWND;
  Elapsed: Integer;
  PID:     DWORD;
  hProc:   THandle;
begin
  hWnd := FindWindowEx(0, 0, '', '{#MyAppName}');
  if hWnd <> 0 then
  begin
    PostMessage(hWnd, WM_CLOSE, 0, 0);
    Elapsed := 0;
    while (Elapsed < 5000) and IsAppRunning() do
    begin
      Sleep(300);
      Elapsed := Elapsed + 300;
    end;

    if IsAppRunning() then
    begin
      hWnd := FindWindowEx(0, 0, '', '{#MyAppName}');
      if hWnd <> 0 then
      begin
        PID := 0;
        GetWindowThreadProcessId(hWnd, PID);
        if PID <> 0 then
        begin
          hProc := OpenProcess(PROCESS_TERMINATE, False, PID);
          if hProc <> 0 then
          begin
            TerminateProcess(hProc, 0);
            CloseHandle(hProc);
          end;
        end;
      end;
      Sleep(1000);
    end;
  end;
end;

// ── InitializeSetup: all validations before any UI is shown ───────────────────

function InitializeSetup(): Boolean;
var
  InstalledVer: String;
begin
  CaptureCameraRegistrationState();
  Result := True;

  // 1. Locate the existing installation (populates GInstallPath / GInstallHive)
  if not FindInstallPath(GInstallPath, GInstallHive) or
     not FileExists(GInstallPath + '\{#MyAppExeName}') then
  begin
    MsgBox(
      'Solin was not found on this computer.' + #13#10 +
      'This file is an update and requires Solin to already be installed.' + #13#10#13#10 +
      'Download the full installer at: {#MyAppURL}',
      mbError, MB_OK
    );
    Result := False;
    Exit;
  end;

  // 2. Check version compatibility
  InstalledVer := GetInstalledVersion();
  if InstalledVer = '' then
    InstalledVer := '{#MyPatchFromVer}'; // assume minimum if key is missing

  if CompareVersions(InstalledVer, '{#MyPatchFromVer}') < 0 then
  begin
    MsgBox(
      'This patch requires Solin {#MyPatchFromVer} or later.' + #13#10 +
      'Installed version: ' + InstalledVer + #13#10#13#10 +
      'Please install the latest full version before applying this patch.',
      mbError, MB_OK
    );
    Result := False;
    Exit;
  end;

  if CompareVersions(InstalledVer, '{#MyPatchVersion}') >= 0 then
  begin
    MsgBox(
      'Solin is already at version ' + InstalledVer + ' or later.' + #13#10 +
      'This patch targets version {#MyPatchVersion}.' + #13#10#13#10 +
      'No action needed.',
      mbInformation, MB_OK
    );
    Result := False;
    Exit;
  end;

  // 3. Close the app if running
  if IsAppRunning() then
  begin
    if MsgBox(
      'Solin is currently running.' + #13#10 +
      'The update needs to close it to replace files.' + #13#10#13#10 +
      'Close Solin and continue?',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON1
    ) = IDYES then
    begin
      ForceCloseApp();
      if IsAppRunning() then
      begin
        MsgBox(
          'Could not close Solin.' + #13#10 +
          'Please close it manually and run this update again.',
          mbError, MB_OK
        );
        Result := False;
      end;
    end
    else
      Result := False;
  end;
end;

// ── Microsoft Edge WebView2 Runtime ──────────────────────────────────────────

function IsValidWebView2Version(const Version: String): Boolean;
begin
  Result := (Version <> '') and (Version <> '0.0.0.0');
end;

function HasWebView2VersionAt(RootKey: Integer; const Subkey: String): Boolean;
var
  Version: String;
begin
  Version := '';
  Result := RegQueryStringValue(RootKey, Subkey, 'pv', Version) and
            IsValidWebView2Version(Version);

  if Result then
    Log('Detected Microsoft Edge WebView2 Runtime ' + Version + ' at ' + Subkey);
end;

function IsWebView2RuntimeInstalled(): Boolean;
begin
  // Official Evergreen Runtime detection: check pv under HKLM/HKCU EdgeUpdate.
  // HKLM\WOW6432Node is the documented location on 64-bit Windows.
  if HasWebView2VersionAt(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_CLIENT_GUID) then
  begin
    Result := True;
    Exit;
  end;

  // Keep the non-WOW6432Node HKLM path as a safety net for 32-bit Windows.
  if HasWebView2VersionAt(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_CLIENT_GUID) then
  begin
    Result := True;
    Exit;
  end;

  if HasWebView2VersionAt(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_CLIENT_GUID) then
  begin
    Result := True;
    Exit;
  end;

  Log('Microsoft Edge WebView2 Runtime was not detected.');
  Result := False;
end;

procedure EnsureWebView2Runtime();
var
  BootstrapperPath: String;
  ResultCode: Integer;
  OriginalProgressStyle: TNewProgressBarStyle;
  OriginalStatus: String;
begin
  if IsWebView2RuntimeInstalled() then
    Exit;

  WizardForm.StatusLabel.Caption := 'Installing WebView2 Runtime...';
  Log('Downloading Microsoft Edge WebView2 Evergreen Bootstrapper...');

  try
    DownloadTemporaryFile(
      WEBVIEW2_BOOTSTRAPPER_URL,
      WEBVIEW2_BOOTSTRAPPER_EXE,
      '',
      nil
    );
  except
    MsgBox(
      'Solin requires WebView2 Runtime, but the update installer could not download it.' + #13#10#13#10 +
      'Please check your internet connection and run this update again.' + #13#10#13#10 +
      'Details: ' + GetExceptionMessage,
      mbError, MB_OK
    );
    Abort();
  end;

  BootstrapperPath := ExpandConstant('{tmp}\') + WEBVIEW2_BOOTSTRAPPER_EXE;
  Log('Running Microsoft Edge WebView2 Evergreen Bootstrapper: ' + BootstrapperPath);

  OriginalProgressStyle := WizardForm.ProgressGauge.Style;
  OriginalStatus := WizardForm.StatusLabel.Caption;
  WizardForm.StatusLabel.Caption := 'Installing WebView2 Runtime...';
  WizardForm.ProgressGauge.Style := npbstMarquee;
  WizardForm.Refresh();
  PumpMessages();

  try
    if not ExecAndWaitResponsive(BootstrapperPath, '/silent /install', '', SW_HIDE, ResultCode) then
    begin
      MsgBox(
        'Solin requires Microsoft Edge WebView2 Runtime, but the update installer could not start the WebView2 bootstrapper.',
        mbError, MB_OK
      );
      Abort();
    end;
  finally
    WizardForm.ProgressGauge.Style := OriginalProgressStyle;
    WizardForm.StatusLabel.Caption := OriginalStatus;
    WizardForm.Refresh();
  end;

  Sleep(1000);

  if not IsWebView2RuntimeInstalled() then
  begin
    MsgBox(
      'Solin requires Microsoft Edge WebView2 Runtime, but it was not detected after installation.' + #13#10#13#10 +
      'WebView2 bootstrapper exit code: ' + IntToStr(ResultCode),
      mbError, MB_OK
    );
    Abort();
  end;

  Log('Microsoft Edge WebView2 Runtime is available.');
end;

// ── Post-install: update Version key and Add/Remove Programs entry ─────────────

procedure CurStepChanged(CurStep: TSetupStep);
var
  UninstKey: String;
begin
  if CurStep = ssInstall then
  begin
    EnsureWebView2Runtime();
    BeginCameraRegistrationTransaction();
  end;

  if CurStep = ssPostInstall then
  begin
    VerifyCameraRegistrationTransaction();
    // Update our own Version key in whichever hive the original install used
    RegWriteStringValue(GInstallHive, '{#MyRegSubkey}', 'Version', '{#MyPatchVersion}');

    // Update DisplayVersion in Add/Remove Programs
    // The uninstall key is under HKLM for machine installs, HKCU for per-user.
    UninstKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
                 '{B7E4D2A1-9C3F-4E8B-A5D6-E7F8G9H0I1J2}_is1';
    RegWriteStringValue(GInstallHive, UninstKey, 'DisplayVersion', '{#MyPatchVersion}');

    Log('Patch {#MyPatchVersion} applied successfully to: ' + GInstallPath);
  end;
  if CurStep = ssDone then
  begin
    CleanupObsoleteCameraVersions();
    CommitCameraRegistrationTransaction();
  end;
end;
