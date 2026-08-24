// Shared transactional coordinator for the per-user DirectShow registration.
// Included from inside [Code], after ExecAndWaitResponsive is declared.

const
  CameraFilterClassId = '{08AFA2E5-0293-4E56-9FE1-2A79DAE8E28F}';
  CameraVideoInputCategory = '{860BB310-5D01-11D0-BD3B-00A0C911CE86}';
  CameraFriendlyName = 'Solin Virtual Camera';

var
  GCameraStateCaptured: Boolean;
  GCameraTransactionStarted: Boolean;
  GCameraTransactionCommitted: Boolean;
  GCameraPriorX64Exists: Boolean;
  GCameraPriorX86Exists: Boolean;
  GCameraPriorX64Path: String;
  GCameraPriorX86Path: String;

function CameraClassKey(): String;
begin
  Result := 'Software\Classes\CLSID\' + CameraFilterClassId +
            '\InprocServer32';
end;

function CameraCategoryKey(): String;
begin
  Result := 'Software\Classes\CLSID\' + CameraVideoInputCategory +
            '\Instance\' + CameraFilterClassId;
end;

function CameraVersionedFilterPath(const Architecture: String): String;
begin
  Result := ExpandConstant(
    '{localappdata}\Solin\VirtualCamera\versions\{#MyVirtualCameraVersion}\' +
    Architecture + '\solin-virtual-camera.dll'
  );
end;

function CameraBooleanText(Value: Boolean): String;
begin
  if Value then
    Result := 'true'
  else
    Result := 'false';
end;

procedure CaptureCameraRegistrationState();
begin
  if GCameraStateCaptured then
    Exit;
  GCameraPriorX64Path := '';
  GCameraPriorX86Path := '';
  GCameraPriorX64Exists := RegQueryStringValue(
    HKCU64, CameraClassKey(), '', GCameraPriorX64Path
  ) and FileExists(GCameraPriorX64Path);
  GCameraPriorX86Exists := RegQueryStringValue(
    HKCU32, CameraClassKey(), '', GCameraPriorX86Path
  ) and FileExists(GCameraPriorX86Path);
  GCameraStateCaptured := True;
  Log(
    'Captured DirectShow registration pair: x64=' +
    CameraBooleanText(GCameraPriorX64Exists) + '; x86=' +
    CameraBooleanText(GCameraPriorX86Exists)
  );
end;

procedure BeginCameraRegistrationTransaction();
begin
  CaptureCameraRegistrationState();
  GCameraTransactionStarted := True;
  GCameraTransactionCommitted := False;
end;

function CameraRegistrationViewIsValid(
  RootKey: Integer; const ExpectedPath: String
): Boolean;
var
  RegisteredPath: String;
  ThreadingModel: String;
  RegisteredClassId: String;
  FriendlyName: String;
begin
  RegisteredPath := '';
  ThreadingModel := '';
  RegisteredClassId := '';
  FriendlyName := '';
  Result :=
    FileExists(ExpectedPath) and
    RegQueryStringValue(RootKey, CameraClassKey(), '', RegisteredPath) and
    (CompareText(RegisteredPath, ExpectedPath) = 0) and
    RegQueryStringValue(
      RootKey, CameraClassKey(), 'ThreadingModel', ThreadingModel
    ) and
    (CompareText(ThreadingModel, 'Both') = 0) and
    RegQueryStringValue(
      RootKey, CameraCategoryKey(), 'CLSID', RegisteredClassId
    ) and
    (CompareText(RegisteredClassId, CameraFilterClassId) = 0) and
    RegQueryStringValue(
      RootKey, CameraCategoryKey(), 'FriendlyName', FriendlyName
    ) and
    (FriendlyName = CameraFriendlyName) and
    RegValueExists(RootKey, CameraCategoryKey(), 'FilterData');
end;

procedure VerifyCameraRegistrationTransaction();
begin
  if not CameraRegistrationViewIsValid(
    HKCU64, CameraVersionedFilterPath('x64')
  ) then
    RaiseException('The x64 Solin DirectShow registration could not be verified.');
  if not CameraRegistrationViewIsValid(
    HKCU32, CameraVersionedFilterPath('x86')
  ) then
    RaiseException('The x86 Solin DirectShow registration could not be verified.');
  Log('Verified the transactional x64/x86 DirectShow registration pair.');
end;

function RunCameraRegsvr(
  const FilterPath: String; IsX64: Boolean; Unregister: Boolean
): Boolean;
var
  RegsvrPath: String;
  Params: String;
  ExitCode: Integer;
begin
  Result := False;
  if not FileExists(FilterPath) then
    Exit;
  if IsX64 then
    RegsvrPath := ExpandConstant('{sys}\regsvr32.exe')
  else
    RegsvrPath := ExpandConstant('{syswow64}\regsvr32.exe');
  Params := '/s ';
  if Unregister then
    Params := Params + '/u ';
  Params := Params + '"' + FilterPath + '"';
  Result := ExecAndWaitResponsive(
    RegsvrPath, Params, ExtractFileDir(FilterPath), SW_HIDE, ExitCode
  ) and (ExitCode = 0);
end;

procedure RemoveCameraRegistrationView(RootKey: Integer);
begin
  RegDeleteKeyIncludingSubkeys(RootKey, CameraCategoryKey());
  RegDeleteKeyIncludingSubkeys(
    RootKey, 'Software\Classes\CLSID\' + CameraFilterClassId
  );
end;

procedure RestoreCameraRegistrationView(
  RootKey: Integer; IsX64: Boolean; PriorExists: Boolean;
  const PriorPath: String
);
begin
  RemoveCameraRegistrationView(RootKey);
  if PriorExists then
  begin
    if RunCameraRegsvr(PriorPath, IsX64, False) then
      Log('Restored previous DirectShow registration: ' + PriorPath)
    else
      Log('Failed to restore previous DirectShow registration: ' + PriorPath);
  end;
end;

procedure RollbackCameraRegistrationTransaction();
begin
  Log('Rolling back the DirectShow x64/x86 registration pair.');
  if not RunCameraRegsvr(
    CameraVersionedFilterPath('x86'), False, True
  ) then
    RemoveCameraRegistrationView(HKCU32);
  if not RunCameraRegsvr(
    CameraVersionedFilterPath('x64'), True, True
  ) then
    RemoveCameraRegistrationView(HKCU64);
  RestoreCameraRegistrationView(
    HKCU64, True, GCameraPriorX64Exists, GCameraPriorX64Path
  );
  RestoreCameraRegistrationView(
    HKCU32, False, GCameraPriorX86Exists, GCameraPriorX86Path
  );
end;

procedure CommitCameraRegistrationTransaction();
begin
  GCameraTransactionCommitted := True;
end;

procedure CleanupObsoleteCameraVersions();
var
  RootDir: String;
  Candidate: String;
  FindRec: TFindRec;
begin
  RootDir := ExpandConstant('{localappdata}\Solin\VirtualCamera\versions');
  if not DirExists(RootDir) then
    Exit;
  if FindFirst(RootDir + '\*', FindRec) then
  try
    repeat
      if ((FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and
         (FindRec.Name <> '.') and (FindRec.Name <> '..') and
         (CompareText(FindRec.Name, '{#MyVirtualCameraVersion}') <> 0) then
      begin
        Candidate := RootDir + '\' + FindRec.Name;
        if DelTree(Candidate, True, True, True) then
          Log('Removed obsolete virtual-camera version: ' + Candidate)
        else
          Log('Deferred loaded virtual-camera version cleanup: ' + Candidate);
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure MaybeInjectVirtualCameraX86RegistrationFailure();
begin
#ifdef MyVirtualCameraForceX86RegistrationFailure
  RaiseException('Injected x86 DirectShow registration failure.');
#endif
end;

procedure DeinitializeSetup();
begin
  if GCameraTransactionStarted and not GCameraTransactionCommitted then
    RollbackCameraRegistrationTransaction();
end;
