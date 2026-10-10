; =============================================================================
;  Solin — Full Installer
;  setup.iss  |  Inno Setup 6.3+
;
;  REGISTRY STRUCTURE (detected from source):
;    Global QSettings:
;      HKCU\Software\Solin\ProjectionPrefs
;      HKCU\Software\Solin\App
;      HKCU\Software\Solin\GlobalApp
;      HKCU\Software\Solin\MainWindowGeometry
;      HKCU\Software\Solin\Timer
;      HKCU\Software\Solin\Monitors
;      HKCU\Software\Solin\Notifications
;    Profile-scoped QSettings:
;      HKCU\Software\Solin_<profile_id>\...
;
;  INSTALLER KEYS (read by the updater):
;    HKA = HKLM if admin / HKCU if per-user
;    HKA\Software\Solin\Solin
;      InstallPath   REG_SZ  — install directory
;      Version       REG_SZ  — installed version  (e.g. "1.0.0.0")
;      InstallScope  REG_SZ  — "machine" | "user"
;
;  BUILD INSTRUCTIONS:
;    1. Compile with Nuitka:
;         nuitka --standalone --enable-plugin=pyside6 --windows-icon-from-ico=src\solin\resources\assets\icon.ico
;                --output-dir=build --output-filename=Solin main.py
;       (output directory must be named "main.dist" — or adjust MyDistDir below)
;    2. The executable must be "Solin.exe" inside MyDistDir.
;    3. Compile with ISCC.exe /DMyAppVersion=<display-version> /DMyWindowsVersion=<numeric-version> setup.iss.
; =============================================================================

#define MyAppName        "Solin"
#define MyAppPublisher   "Solin Software"
#define MyAppURL         "https://solinav.vercel.app"
#define MyAppExeName     "Solin.exe"
#define MyAppMutex       "Solin_SingleInstance_Mutex"
#define MyRegSubkey      "Software\Solin\Solin"
#define MyPlaylistProgId "Solin.Playlist"
#define MyPlaylistMime   "application/vnd.solin.playlist+zip"
#ifndef MyDistDir
  #define MyDistDir      "..\..\..\build\main.dist"
#endif

#ifndef MyAppVersion
  #error MyAppVersion must be supplied by the build pipeline.
#endif

#ifndef MyArtifactSuffix
  #define MyArtifactSuffix ""
#endif

#ifndef MyWindowsVersion
  #error MyWindowsVersion must be supplied by the build pipeline.
#endif

; =============================================================================
[Setup]
AppId={{B7E4D2A1-9C3F-4E8B-A5D6-E7F8G9H0I1J2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
AppCopyright=Copyright (c) 2026 Alexsander and contributors.

; Set the display name in Control Panel.
UninstallDisplayName={#MyAppName}

; ── Mutex: blocks installation while app is running ──────────────────────────
AppMutex={#MyAppMutex}

; Directory resolved at runtime.
; IsAdminInstallMode() → {autopf}\Solin   (install for all users)
;                     → {localappdata}\Solin  (install for current user only)
DefaultDirName={code:GetDefaultInstallDir}
; Use Inno's localized, silent-aware existing-directory confirmation. Residue
; preserved by uninstall does not mean this is a foreign application directory.
DirExistsWarning=auto
DefaultGroupName={#MyAppName}
AllowNoIcons=yes

; ── Privilege selection ───────────────────────────────────────────────────────
; "lowest" = installs without elevation by default (per-user, %LocalAppData%)
; User can optionally install for all via "dialog" which adds an extra page.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog

; ── Close running app automatically ──────────────────────────────────────────
CloseApplications=no
CloseApplicationsFilter=*{#MyAppExeName}*
RestartApplications=no

; ── Output ────────────────────────────────────────────────────────────────────
OutputDir=..\..\..\build\installer_output
OutputBaseFilename=Solin-{#MyAppVersion}-windows-x86_64{#MyArtifactSuffix}
SetupIconFile=..\..\..\src\solin\resources\assets\icon.ico
WizardStyle=modern

; Set the icon in Control Panel (Add/Remove Programs).
UninstallDisplayIcon={app}\{#MyAppExeName}

; Uncomment if you have wizard artwork BMPs:
; WizardSmallImageFile=resources\assets\wizard_small.bmp
; WizardImageFile=resources\assets\wizard_side.bmp

; ── Compression ───────────────────────────────────────────────────────────────
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; ── Language ──────────────────────────────────────────────────────────────────
ShowLanguageDialog=auto

; ── Versioning ────────────────────────────────────────────────────────────────
VersionInfoVersion={#MyWindowsVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyWindowsVersion}
VersionInfoProductTextVersion={#MyAppVersion}

RestartIfNeededByRun=no
ChangesAssociations=yes
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763

; =============================================================================
[Languages]
Name: "english";    MessagesFile: "compiler:Default.isl"
Name: "portuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "spanish";    MessagesFile: "compiler:Languages\Spanish.isl"
Name: "french";     MessagesFile: "compiler:Languages\French.isl"
Name: "italian";    MessagesFile: "compiler:Languages\Italian.isl"

; =============================================================================
[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}";
Name: "startupicon"; Description: "Start with Windows";     GroupDescription: "Options:"; Flags: unchecked

; =============================================================================
[InstallDelete]
; Replace this installer-owned Python namespace before copying the new payload.
; Older local builds included optional backports extensions. Leaving an extension
; behind when its compiled package disappears creates an importable, empty
; namespace that breaks urllib3's optional compression detection.
Type: filesandordirs; Name: "{app}\backports"

; =============================================================================
[Files]
; Stage the immutable filter pair before application files are installed.
; These files and their COM registration have one shared lifecycle owner.
Source: "{#MyDistDir}\native\media-engine\virtual-camera\x64\solin-virtual-camera.dll"; DestName: "solin-virtual-camera-x64.dll"; Flags: dontcopy
Source: "{#MyDistDir}\native\media-engine\virtual-camera\x86\solin-virtual-camera.dll"; DestName: "solin-virtual-camera-x86.dll"; Flags: dontcopy
; ── 1. Copy build output (DRY: one line copies the entire app) ────────────────
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; ── 2. App icon (only if not already inside main.dist) ───────────────────────
Source: "..\..\..\src\solin\resources\assets\icon.ico"; DestDir: "{app}\resources\assets"; Flags: ignoreversion
Source: "..\..\..\src\solin\resources\assets\playlist.ico"; DestDir: "{app}\resources\assets"; Flags: ignoreversion

; NOTE: Translations/lang folders are handled automatically via recursesubdirs
;       above, as long as they reside inside main.dist/.

; =============================================================================
[Dirs]
; Create writable data directories for the current user.
; Essential for machine-wide installs where Program Files is read-only.
; Name: "{app}\data";        Permissions: users-modify
; Name: "{app}\cache";       Permissions: users-modify
; Name: "{app}\cache\media"; Permissions: users-modify

; =============================================================================
[Icons]
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\resources\assets\icon.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\resources\assets\icon.ico"; Tasks: desktopicon
Name: "{autostartup}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

; =============================================================================
[Registry]
; ── HKA = HKLM if admin, HKCU if per-user ────────────────────────────────────
; These keys identify the installation directory and scope.
Root: HKA; Subkey: "{#MyRegSubkey}"; ValueType: string; ValueName: "InstallPath";  ValueData: "{app}";                   Flags: uninsdeletekey
Root: HKA; Subkey: "{#MyRegSubkey}"; ValueType: string; ValueName: "Version";      ValueData: "{#MyAppVersion}";         Flags: uninsdeletevalue
Root: HKA; Subkey: "{#MyRegSubkey}"; ValueType: string; ValueName: "InstallScope"; ValueData: "{code:GetInstallScope}";  Flags: uninsdeletevalue

; 1. Define Solin image, video, and audio file types.
; Imagem
Root: HKA; Subkey: "Software\Classes\Solin.Image"; ValueType: string; ValueName: ""; ValueData: "Arquivo de Imagem do Solin"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Solin.Image\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"
Root: HKA; Subkey: "Software\Classes\Solin.Image\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

; Video
Root: HKA; Subkey: "Software\Classes\Solin.Video"; ValueType: string; ValueName: ""; ValueData: "Arquivo de Vídeo do Solin"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Solin.Video\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"
Root: HKA; Subkey: "Software\Classes\Solin.Video\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

; Audio
Root: HKA; Subkey: "Software\Classes\Solin.Audio"; ValueType: string; ValueName: ""; ValueData: "Arquivo de Áudio do Solin"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Solin.Audio\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"
Root: HKA; Subkey: "Software\Classes\Solin.Audio\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

; 2. Add Solin to the "Open with..." list without changing default associations.
; Imagens
Root: HKA; Subkey: "Software\Classes\.png\OpenWithProgids"; ValueType: string; ValueName: "Solin.Image"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.jpg\OpenWithProgids"; ValueType: string; ValueName: "Solin.Image"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.jpeg\OpenWithProgids"; ValueType: string; ValueName: "Solin.Image"; ValueData: ""; Flags: uninsdeletevalue

; Videos
Root: HKA; Subkey: "Software\Classes\.mp4\OpenWithProgids"; ValueType: string; ValueName: "Solin.Video"; ValueData: ""; Flags: uninsdeletevalue

; Audio files
Root: HKA; Subkey: "Software\Classes\.mp3\OpenWithProgids"; ValueType: string; ValueName: "Solin.Audio"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.wav\OpenWithProgids"; ValueType: string; ValueName: "Solin.Audio"; ValueData: ""; Flags: uninsdeletevalue

; ── Native Solin playlist format ─────────────────────────────────────────────
; Do not write the extension's default value or Windows UserChoice. Registering
; through OpenWithProgids and Capabilities keeps an existing user-selected app
; intact while making Solin available in Open With and Default Apps.
Root: HKA; Subkey: "Software\Classes\.solinplaylist\OpenWithProgids"; ValueType: none; ValueName: "{#MyPlaylistProgId}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: ""; ValueData: "Solin Playlist"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "Solin Playlist"
Root: HKA; Subkey: "Software\Classes\{#MyPlaylistProgId}"; ValueType: string; ValueName: "Content Type"; ValueData: "{#MyPlaylistMime}"
Root: HKA; Subkey: "Software\Classes\{#MyPlaylistProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\playlist.ico,0"
Root: HKA; Subkey: "Software\Classes\{#MyPlaylistProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\resources\assets\icon.ico,0"
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: none; ValueName: ".solinplaylist"
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
Root: HKA; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Open native Solin playlists"
Root: HKA; Subkey: "Software\Solin\Capabilities"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: "{app}\resources\assets\icon.ico,0"
Root: HKA; Subkey: "Software\Solin\Capabilities\FileAssociations"; ValueType: string; ValueName: ".solinplaylist"; ValueData: "{#MyPlaylistProgId}"
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: "Software\Solin\Capabilities"; Flags: uninsdeletevalue


; =============================================================================
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent runasoriginaluser; Check: not IsUpdateMode

; =============================================================================
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; Check: IsUpdateMode

[Code]
(*
  ============================================================================
  Pascal Script — Full Installer
  ============================================================================
  Covers:
    1. Dynamic DefaultDirName (admin → Program Files / user → LocalAppData)
    2. InstallScope → "machine" | "user" written to registry
    3. Detect app running before install → graceful close without terminating the process
    4. Ensure Microsoft Edge WebView2 Runtime is available when needed
    5. Uninstaller: close app, clean runtime files, registry, temp files
  ============================================================================
*)

function FindWindowEx(hWndParent: HWND; hWndChild: HWND; lpszClass: String; lpszWindow: String): HWND;
  external 'FindWindowExW@user32.dll stdcall';

function PostMessage(hWnd: HWND; Msg: Cardinal; wParam: LongInt; lParam: LongInt): BOOL;
  external 'PostMessageW@user32.dll stdcall';

function GetWindowThreadProcessId(hWnd: HWND; var lpdwProcessId: DWORD): DWORD;
  external 'GetWindowThreadProcessId@user32.dll stdcall';

function OpenProcess(dwDesiredAccess: DWORD; bInheritHandle: BOOL; dwProcessId: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall';

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
  WM_CLOSE = $0010;
  SYNCHRONIZE = $00100000;
  EVENT_MODIFY_STATE = $0002;
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

function IsCameraMachineInstall(): Boolean;
begin
  Result := IsAdminInstallMode();
end;

procedure MaybeInjectVirtualCameraX86RegistrationFailure();
begin
#ifdef MyVirtualCameraForceX86RegistrationFailure
  RaiseException('Injected x86 DirectShow registration failure.');
#endif
end;

#include "virtual_camera_registration.iss"

// ── Install scope ─────────────────────────────────────────────────────────────

function GetInstallScope(Param: String): String;
begin
  if IsAdminInstallMode() then
    Result := 'machine'
  else
    Result := 'user';
end;

// ── Default install directory based on elevation mode ────────────────────────

function GetDefaultInstallDir(Param: String): String;
begin
  if IsAdminInstallMode() then
    Result := ExpandConstant('{autopf}\{#MyAppName}')
  else
    Result := ExpandConstant('{localappdata}\Programs\{#MyAppName}');
end;

// ── Running process detection ─────────────────────────────────────────────────

function IsAppRunning(): Boolean;
begin
  Result := (FindWindowEx(0, 0, '', '{#MyAppName}') <> 0) or
            CheckForMutexes('{#MyAppMutex}');
end;

function OpenEvent(dwDesiredAccess: DWORD; bInheritHandle: BOOL; lpName: String): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(hEvent: THandle): BOOL;
  external 'SetEvent@kernel32.dll stdcall';

function IsUpdateMode(): Boolean;
begin
  Result := ExpandConstant('{param:SOLINUPDATE|}') <> '';
end;

procedure RequestCloseApp();
var
  Window: HWND;
  PID: DWORD;
  ProcessHandle: THandle;
begin
  Window := FindWindowEx(0, 0, '', '{#MyAppName}');
  if Window = 0 then Exit;
  PID := 0;
  GetWindowThreadProcessId(Window, PID);
  ProcessHandle := OpenProcess(SYNCHRONIZE, False, PID);
  if ProcessHandle = 0 then Exit;
  try
    PostMessage(Window, WM_CLOSE, 0, 0);
    WaitForSingleObject(ProcessHandle, 30000);
  finally
    CloseHandle(ProcessHandle);
  end;
end;

function ConfirmCloseRunningSolin(): Boolean;
begin
  Result := True;
  if not IsAppRunning() then Exit;
  if MsgBox(
    'Solin is currently running.' + #13#10 +
    'It must be closed before continuing.' + #13#10#13#10 +
    'Close it and continue?',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON1
  ) <> IDYES then
  begin
    Result := False;
    Exit;
  end;
  RequestCloseApp();
  if IsAppRunning() then
  begin
    MsgBox(
      'Could not close Solin. Please close it manually and try again.',
      mbError, MB_OK
    );
    Result := False;
  end;
end;

function InitializeSetup(): Boolean;
var
  Token, InstallPath, Scope: String;
  RootKey, PID, I, Elapsed: Integer;
  ReadyEvent, CancelEvent, ParentProcess: THandle;
  WaitResult: DWORD;
begin
  Result := False;
  if IsUpdateMode() then
  begin
    Token := ExpandConstant('{param:SOLINUPDATE|}');
    if Length(Token) <> 32 then Exit;
    for I := 1 to Length(Token) do
      if Pos(Token[I], '0123456789abcdef') = 0 then Exit;
    PID := StrToIntDef(ExpandConstant('{param:SOLINPID|0}'), 0);
    if PID <= 0 then Exit;
    if IsAdminInstallMode() then begin RootKey := HKLM64; Scope := 'machine'; end
    else begin RootKey := HKCU64; Scope := 'user'; end;
    InstallPath := '';
    if not RegQueryStringValue(RootKey, '{#MyRegSubkey}', 'InstallPath', InstallPath) then Exit;
    if CompareText(RemoveBackslashUnlessRoot(InstallPath),
       RemoveBackslashUnlessRoot(ExpandConstant('{param:DIR|}'))) <> 0 then Exit;
    if not FileExists(InstallPath + '\{#MyAppExeName}') then Exit;
    InstallPath := '';
    if not RegQueryStringValue(RootKey, '{#MyRegSubkey}', 'InstallScope', InstallPath) then Exit;
    if InstallPath <> Scope then Exit;
    ReadyEvent := OpenEvent(EVENT_MODIFY_STATE, False, 'Local\SolinUpdateReady-' + Token);
    CancelEvent := OpenEvent(SYNCHRONIZE, False, 'Local\SolinUpdateCancel-' + Token);
    ParentProcess := OpenProcess(SYNCHRONIZE, False, PID);
    try
      if (ReadyEvent = 0) or (CancelEvent = 0) or (ParentProcess = 0) then Exit;
      if not SetEvent(ReadyEvent) then Exit;
      Elapsed := 0;
      repeat
        if WaitForSingleObject(CancelEvent, 0) = 0 then Exit;
        WaitResult := WaitForSingleObject(ParentProcess, 100);
        Elapsed := Elapsed + 100;
      until (WaitResult <> WAIT_TIMEOUT) or (Elapsed >= 120000);
      Result := WaitResult = 0;
    finally
      if ReadyEvent <> 0 then CloseHandle(ReadyEvent);
      if CancelEvent <> 0 then CloseHandle(CancelEvent);
      if ParentProcess <> 0 then CloseHandle(ParentProcess);
    end;
    Exit;
  end;
  Result := ConfirmCloseRunningSolin();
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
      'Solin requires WebView2 Runtime, but the installer could not download it.' + #13#10#13#10 +
      'Please check your internet connection and run the installer again.' + #13#10#13#10 +
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
        'Solin requires Microsoft Edge WebView2 Runtime, but the installer could not start the WebView2 bootstrapper.',
        mbError, MB_OK
      );
      Abort();
    end;
  finally
    WizardForm.ProgressGauge.Style := OriginalProgressStyle;
    WizardForm.StatusLabel.Caption := OriginalStatus;
    WizardForm.Refresh();
  end;

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

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    EnsureWebView2Runtime();
    InstallCameraRegistrationTransaction();
  end;
  if CurStep = ssPostInstall then
  begin
    CommitCameraRegistrationTransaction();
  end;
  if CurStep = ssDone then
  begin
    CleanupObsoleteCameraVersions();
  end;
end;

#include "uninstall.iss"
