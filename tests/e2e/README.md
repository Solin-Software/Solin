# End-to-End Tests

This suite contains artifact-gated packaged application checks. Local test runs
skip them unless the required release artifacts are provided explicitly.

Run the startup smoke after producing a packaged artifact:

```powershell
$env:SOLIN_PACKAGED_EXE = "C:\path\to\main.dist\Solin.exe"
.\.venv\Scripts\python.exe -m pytest tests\e2e -q
```

For macOS, point `SOLIN_PACKAGED_EXE` at the executable inside the signed app
bundle, for example `build/Solin.app/Contents/MacOS/main`.

The smoke launches the packaged executable with isolated user data directories,
verifies that it stays alive through the startup window, then terminates the
process.

Run the full-installer upgrade smoke only on a disposable Windows test machine
or CI runner with no existing Solin installation:

```powershell
$env:SOLIN_E2E_ALLOW_INSTALLER_MUTATION = "1"
$env:SOLIN_OLD_INSTALLER = "C:\path\to\old\Solin_Setup_1.0.0.exe"
$env:SOLIN_NEW_INSTALLER = "C:\path\to\new\Solin_Setup_1.1.0.exe"
$env:SOLIN_ROLLBACK_INSTALLER = "C:\path\to\Solin_Setup_1.1.0-rollback-injection.exe"
$env:SOLIN_DIRECTSHOW_HARNESS_X64 = "C:\path\to\x64\solin-virtual-camera-filter-tests.exe"
$env:SOLIN_DIRECTSHOW_HARNESS_X86 = "C:\path\to\x86\solin-virtual-camera-filter-tests.exe"
.\.venv\Scripts\python.exe -m pytest tests\e2e\test_installer_upgrade_smoke.py -q
```

The upgrade smoke installs into a temporary directory, seeds profile,
settings, playlist, and meeting-tree sentinels, installs the new full
installer over it, verifies that user state survived, starts the upgraded app,
then runs the generated uninstaller and removes its test registry keys. It is
also wired into the Windows release workflow after the Inno Setup build by
downloading the previous production installer configured in the workflow input.
The smoke temporarily registers the per-user DirectShow camera in both WOW64
registry views, loads each installed DLL through its architecture-matched
harness, connects a real graph, and validates standby samples. It then runs an
installer whose x86 registration deliberately fails and verifies that the prior
x64/x86 pair was restored before uninstall. Both registrations must be absent
after uninstall. The test refuses to run when the Solin camera CLSID already
exists, so use a disposable Windows account or runner. The Windows release
workflow builds the failure-injection artifact automatically; it is never
published.

Run the macOS app replacement smoke after extracting the previous release DMG
and building the new app bundle:

```bash
export SOLIN_OLD_MACOS_APP="/path/to/old/Solin.app"
export SOLIN_NEW_MACOS_APP="/path/to/build/Solin.app"
python -m pytest tests/e2e/test_macos_app_upgrade_smoke.py -q
```

The macOS release workflow downloads the previous production DMG configured in
the workflow input, mounts it, copies out the old app bundle, replaces it with
the newly built bundle in a temporary install directory, verifies that user
state survived, and starts the upgraded app.
