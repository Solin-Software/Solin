// Shared uninstall lifecycle for full and patch installers. Inno runs the
// Pascal script from the most recent installation, so both must include it.
// Registry entries owned by [Registry] are removed by the scoped uninstall log.

procedure DeleteTempFiles(const Dir: String);
var
  FindRec: TFindRec;
begin
  if FindFirst(Dir + '\is-*.tmp', FindRec) then
  try
    repeat
      DeleteFile(Dir + '\' + FindRec.Name);
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure DeleteProfileQSettingsKeys();
var
  Names: TArrayOfString;
  I: Integer;
  KeyName: String;
begin
  if RegGetSubkeyNames(HKCU, 'Software', Names) then
  begin
    for I := 0 to GetArrayLength(Names) - 1 do
    begin
      KeyName := Names[I];
      if Copy(KeyName, 1, 6) = 'Solin_' then
        RegDeleteKeyIncludingSubkeys(HKCU, 'Software\' + KeyName);
    end;
  end;
end;

procedure DeleteQSettingsKeys();
begin
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Solin\ProjectionPrefs');
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Solin\App');
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Solin\GlobalApp');
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Solin\MainWindowGeometry');
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Solin\Timer');
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Solin\Monitors');
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Solin\Notifications');
  DeleteProfileQSettingsKeys();
  RegDeleteKeyIfEmpty(HKCU, 'Software\Solin');
end;

function OtherScopeInstallationExists(): Boolean;
var
  OtherRootKey: Integer;
begin
  if IsAdminInstallMode() then
    OtherRootKey := HKCU64
  else
    OtherRootKey := HKLM64;
  // Preserve shared preferences even if the other installation needs repair.
  Result := RegValueExists(OtherRootKey, '{#MyRegSubkey}', 'InstallPath');
end;

procedure DeleteCameraStorage();
begin
  // DelTree does not follow reparse points. Never schedule deletion by path:
  // reinstalling the same immutable version before reboot must remain safe.
  if not DelTree(CameraStorageRoot(), True, True, True) then
    Log('Deferred loaded virtual-camera files for cleanup by a later install.');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: String;
  CameraFilterX64Path: String;
  CameraFilterX86Path: String;
begin
  AppDir := ExpandConstant('{app}');

  case CurUninstallStep of

    usAppMutexCheck:
    begin
      if IsAppRunning() then
      begin
        if MsgBox(
          'Solin is currently running.' + #13#10 +
          'It must be closed before uninstalling.' + #13#10#13#10 +
          'Close it and continue?',
          mbConfirmation, MB_YESNO or MB_DEFBUTTON1
        ) = IDYES then
        begin
          ForceCloseApp();
          if IsAppRunning() then
          begin
            MsgBox(
              'Could not close Solin. Please close it manually and try again.',
              mbError, MB_OK
            );
            Abort();
          end;
        end
        else
          Abort();
      end;
    end;

    usUninstall:
    begin
      // Unregister both DirectShow registry views before deleting the immutable
      // version.
      CameraFilterX64Path := '';
      if not RegQueryStringValue(
        CameraRegistrationRootKey(True), CameraClassKey(), '', CameraFilterX64Path
      ) then
        CameraFilterX64Path := CameraVersionedFilterPath('x64');
      if FileExists(CameraFilterX64Path) and
         not RunCameraRegsvr(CameraFilterX64Path, True, True, True) then
        Log('x64 virtual-camera cleanup failed during uninstall.');
      RemoveCameraRegistrationView(CameraRegistrationRootKey(True));

      CameraFilterX86Path := '';
      if not RegQueryStringValue(
        CameraRegistrationRootKey(False), CameraClassKey(), '', CameraFilterX86Path
      ) then
        CameraFilterX86Path := CameraVersionedFilterPath('x86');
      if FileExists(CameraFilterX86Path) and
         not RunCameraRegsvr(CameraFilterX86Path, False, True, True) then
        Log('x86 virtual-camera cleanup failed during uninstall.');
      RemoveCameraRegistrationView(CameraRegistrationRootKey(False));

      // Preferences belong to the current account and are shared by both scopes.
      if OtherScopeInstallationExists() then
        Log('Preserving shared QSettings for the other Solin installation scope.')
      else
        DeleteQSettingsKeys();
    end;

    usPostUninstall:
    begin
      DeleteCameraStorage();
      // Inno Setup installer temp files left behind from interrupted installs
      DeleteTempFiles(AppDir);

      // Remove the install directory itself ONLY if it is now empty
      RemoveDir(AppDir);
    end;

  end;
end;
