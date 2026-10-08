// Shared transactional coordinator for DirectShow registration in the install
// scope selected by the parent installer. The parent must define
// IsCameraMachineInstall before including this file.

const
  CameraFilterClassId = '{08AFA2E5-0293-4E56-9FE1-2A79DAE8E28F}';
  CameraVideoInputCategory = '{860BB310-5D01-11D0-BD3B-00A0C911CE86}';
  CameraFriendlyName = 'Solin Virtual Camera';

var
  GCameraStateCaptured: Boolean;
  GCameraTransactionStarted: Boolean;
  GCameraTransactionCommitted: Boolean;
  GCameraRegistrationAttempted: Boolean;
  GCameraCreatedX64File: Boolean;
  GCameraCreatedX86File: Boolean;
  GCameraPriorX64Exists: Boolean;
  GCameraPriorX86Exists: Boolean;
  GCameraPriorX64Path: String;
  GCameraPriorX86Path: String;
  GCameraPairId: String;

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

function CameraStorageRoot(): String;
begin
  if IsCameraMachineInstall() then
    Result := ExpandConstant('{commonpf64}\Solin\VirtualCamera')
  else
    Result := ExpandConstant('{localappdata}\Solin\VirtualCamera');
end;

function CameraVersionsRoot(): String;
begin
  Result := CameraStorageRoot() + '\versions';
end;

function CameraVersionedFilterDirectory(Architecture: String): String;
begin
  Result := CameraVersionsRoot() + '\' + GCameraPairId + '\' + Architecture;
end;

function CameraVersionedFilterPath(const Architecture: String): String;
begin
  Result := CameraVersionedFilterDirectory(Architecture) +
            '\solin-virtual-camera.dll';
end;

function CameraRegistrationRootKey(IsX64: Boolean): Integer;
begin
  if IsCameraMachineInstall() then
  begin
    if IsX64 then
      Result := HKLM64
    else
      Result := HKLM32;
  end
  else
  begin
    if IsX64 then
      Result := HKCU64
    else
      Result := HKCU32;
  end;
end;

function CameraInstallScopeArgument(): String;
begin
  if IsCameraMachineInstall() then
    Result := 'machine'
  else
    Result := 'user';
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
    CameraRegistrationRootKey(True), CameraClassKey(), '', GCameraPriorX64Path
  ) and FileExists(GCameraPriorX64Path);
  GCameraPriorX86Exists := RegQueryStringValue(
    CameraRegistrationRootKey(False), CameraClassKey(), '', GCameraPriorX86Path
  ) and FileExists(GCameraPriorX86Path);
  GCameraStateCaptured := True;
  Log(
    'Captured ' + CameraInstallScopeArgument() +
    ' DirectShow registration pair: x64=' +
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
    CameraRegistrationRootKey(True), CameraVersionedFilterPath('x64')
  ) then
    RaiseException('The x64 Solin DirectShow registration could not be verified.');
  if not CameraRegistrationViewIsValid(
    CameraRegistrationRootKey(False), CameraVersionedFilterPath('x86')
  ) then
    RaiseException('The x86 Solin DirectShow registration could not be verified.');
  Log(
    'Verified the transactional ' + CameraInstallScopeArgument() +
    ' x64/x86 DirectShow registration pair.'
  );
end;

function RunCameraRegsvrCommand(
  const FilterPath: String; IsX64: Boolean; Unregister: Boolean;
  ScopedInstall: Boolean
): Boolean;
var
  RegsvrPath: String;
  Params: String;
  ExitCode: Integer;
begin
  Result := False;
  ExitCode := -1;
  if not FileExists(FilterPath) then
    Exit;
  if IsX64 then
    RegsvrPath := ExpandConstant('{sys}\regsvr32.exe')
  else
    RegsvrPath := ExpandConstant('{syswow64}\regsvr32.exe');
  Params := '/s ';
  if Unregister then
    Params := Params + '/u ';
  if ScopedInstall then
    Params := Params + '/n /i:' + CameraInstallScopeArgument() + ' ';
  Params := Params + '"' + FilterPath + '"';
  Result := Exec(
    RegsvrPath, Params, ExtractFileDir(FilterPath), SW_HIDE,
    ewWaitUntilTerminated, ExitCode
  ) and (ExitCode = 0);
  if not Result then
    Log(
      'DirectShow registration command failed with exit code ' +
      IntToStr(ExitCode) + ': ' + RegsvrPath + ' ' + Params
    );
end;

function RunCameraRegsvr(
  const FilterPath: String; IsX64: Boolean; Unregister: Boolean;
  AllowLegacy: Boolean
): Boolean;
begin
  Result := RunCameraRegsvrCommand(
    FilterPath, IsX64, Unregister, True
  );
  if not Result and AllowLegacy and not IsCameraMachineInstall() then
  begin
    Log(
      'Retrying legacy per-user DirectShow registration without DllInstall: ' +
      FilterPath
    );
    Result := RunCameraRegsvrCommand(
      FilterPath, IsX64, Unregister, False
    );
  end;
end;

procedure RegisterCameraPair();
begin
  GCameraRegistrationAttempted := True;
  if not RunCameraRegsvr(
    CameraVersionedFilterPath('x64'), True, False, False
  ) then
    RaiseException('The x64 Solin DirectShow registration failed.');
  MaybeInjectVirtualCameraX86RegistrationFailure();
  if not RunCameraRegsvr(
    CameraVersionedFilterPath('x86'), False, False, False
  ) then
    RaiseException('The x86 Solin DirectShow registration failed.');
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
    if RunCameraRegsvr(PriorPath, IsX64, False, True) then
      Log('Restored previous DirectShow registration: ' + PriorPath)
    else
      Log('Failed to restore previous DirectShow registration: ' + PriorPath);
  end;
end;

procedure RollbackCameraRegistrationTransaction();
begin
  Log(
    'Rolling back the ' + CameraInstallScopeArgument() +
    ' DirectShow x64/x86 registration pair.'
  );
  if GCameraRegistrationAttempted then
  begin
    RemoveCameraRegistrationView(CameraRegistrationRootKey(False));
    RemoveCameraRegistrationView(CameraRegistrationRootKey(True));
    RestoreCameraRegistrationView(
      CameraRegistrationRootKey(True), True,
      GCameraPriorX64Exists, GCameraPriorX64Path
    );
    RestoreCameraRegistrationView(
      CameraRegistrationRootKey(False), False,
      GCameraPriorX86Exists, GCameraPriorX86Path
    );
  end;
  // Extraction can fail before the pair identity exists. Do not resolve an
  // incomplete identity to unrelated directories beneath the versions root.
  if GCameraPairId <> '' then
  begin
    if GCameraCreatedX64File then
      DeleteFile(CameraVersionedFilterPath('x64'));
    if GCameraCreatedX86File then
      DeleteFile(CameraVersionedFilterPath('x86'));
    RemoveDir(CameraVersionedFilterDirectory('x64'));
    RemoveDir(CameraVersionedFilterDirectory('x86'));
    RemoveDir(ExtractFileDir(CameraVersionedFilterDirectory('x64')));
  end;
end;

procedure PrepareCameraFilterPair();
var
  X64Hash: String;
  X86Hash: String;
begin
  ExtractTemporaryFile('solin-virtual-camera-x64.dll');
  ExtractTemporaryFile('solin-virtual-camera-x86.dll');
  X64Hash := GetSHA256OfFile(ExpandConstant('{tmp}\solin-virtual-camera-x64.dll'));
  X86Hash := GetSHA256OfFile(ExpandConstant('{tmp}\solin-virtual-camera-x86.dll'));
  // Match the development registrar's immutable pair identity. App versions do
  // not identify binaries: a diagnostic rebuild can keep the same app version.
  // StageCameraFilter verifies the complete digest before reusing either file.
  GCameraPairId := Copy(GetSHA256OfString(X64Hash + ':' + X86Hash), 1, 20);
end;

procedure StageCameraFilter(const Architecture: String; var Created: Boolean);
var
  SourceName: String;
  SourcePath: String;
  TargetPath: String;
begin
  SourceName := 'solin-virtual-camera-' + Architecture + '.dll';
  SourcePath := ExpandConstant('{tmp}\') + SourceName;
  TargetPath := CameraVersionedFilterPath(Architecture);
  if FileExists(TargetPath) then
  begin
    // Reuse identical binaries even when a consumer holds the DLL. Never
    // overwrite an existing content identity, including a hash-prefix collision.
    if GetSHA256OfFile(SourcePath) <> GetSHA256OfFile(TargetPath) then
      RaiseException('The managed virtual-camera binary has inconsistent contents: ' + TargetPath);
    Exit;
  end;
  if not ForceDirectories(ExtractFileDir(TargetPath)) then
    RaiseException('Could not create the virtual-camera directory: ' + ExtractFileDir(TargetPath));
  Created := True;
  if not FileCopy(SourcePath, TargetPath, True) then
    RaiseException('Could not install the virtual-camera binary: ' + TargetPath);
end;

procedure InstallCameraRegistrationTransaction();
begin
  // ssInstall is the last event in which an exception aborts Setup. Own the
  // immutable filter files and registration together, before application writes.
  BeginCameraRegistrationTransaction();
  PrepareCameraFilterPair();
  StageCameraFilter('x64', GCameraCreatedX64File);
  StageCameraFilter('x86', GCameraCreatedX86File);
  RegisterCameraPair();
  VerifyCameraRegistrationTransaction();
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
  RootDir := CameraVersionsRoot();
  if not DirExists(RootDir) then
    Exit;
  if FindFirst(RootDir + '\*', FindRec) then
  try
    repeat
      if ((FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and
         (FindRec.Name <> '.') and (FindRec.Name <> '..') and
         (CompareText(FindRec.Name, GCameraPairId) <> 0) then
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

procedure DeinitializeSetup();
begin
  if GCameraTransactionStarted and not GCameraTransactionCommitted then
    RollbackCameraRegistrationTransaction();
end;
