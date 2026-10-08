# End-to-End Tests

This suite contains artifact-gated packaged application checks. Local test runs
skip them unless the required release artifacts are provided explicitly.

Run the startup smoke after producing a packaged artifact:

```powershell
$env:SOLIN_PACKAGED_EXE = "C:\path\to\main.dist\Solin.exe"
.\.venv\Scripts\python.exe -m pytest tests\e2e -q
```

For macOS, point `SOLIN_PACKAGED_EXE` at the executable inside the sealed app
bundle, for example `build/Solin.app/Contents/MacOS/main`.

The smoke launches the packaged executable with isolated user data directories,
verifies that it stays alive through the startup window, then terminates the
process.

Each smoke owns private user-data, temporary-directory, and IPC namespaces. On
POSIX, it also owns a separate process group and cleans up launcher descendants,
including the child spawned by AppImage extract-and-run. Upgrade pairs reuse the
same environment so replacement cannot conceal a surviving old instance.

Run the full-installer upgrade smoke only on a disposable Windows test machine
or CI runner with no existing Solin installation:

```powershell
$env:SOLIN_E2E_ALLOW_INSTALLER_MUTATION = "1"
$env:SOLIN_OLD_INSTALLER = "C:\artifacts\Solin-26.31.1-windows-x86_64.exe"
$env:SOLIN_NEW_INSTALLER = "C:\artifacts\Solin-26.32.0-windows-x86_64.exe"
$env:SOLIN_ROLLBACK_INSTALLER = "C:\artifacts\Solin-26.32.0-windows-x86_64-rollback-injection.exe"
$env:SOLIN_DIRECTSHOW_HARNESS_X64 = "C:\path\to\x64\solin-virtual-camera-filter-tests.exe"
$env:SOLIN_DIRECTSHOW_HARNESS_X86 = "C:\path\to\x86\solin-virtual-camera-filter-tests.exe"
.\.venv\Scripts\python.exe -m pytest tests\e2e\test_installer_upgrade_smoke.py -q
```

The upgrade smoke installs into a temporary directory, seeds profile,
settings, playlist, and meeting-tree sentinels, installs the new full
installer over it, verifies that user state survived, starts the upgraded app,
then runs the generated uninstaller and removes its test registry keys. It is
also wired into the Windows release workflow after the Inno Setup build. The
workflow resolves the previous setup and verifies its SHA-256 before execution.
The smoke validates current-user and all-users DirectShow registration in both
WOW64 registry views, loads each installed DLL through its architecture-matched
harness, connects a real graph, and validates standby samples. It then runs an
installer whose x86 registration deliberately fails and verifies that the prior
x64/x86 pair was restored before uninstall. The machine-scope scenario runs
only from an elevated process. Registrations created by the smoke must be absent
after uninstall, while a pre-existing registration in the other scope must stay
unchanged. Use a disposable Windows account or runner. The Windows release
workflow builds the failure-injection artifact automatically; it is never
published.

Run the macOS app replacement smoke after extracting the previous release DMG
and building the new app bundle:

```bash
export SOLIN_OLD_MACOS_APP="/path/to/old/Solin.app"
export SOLIN_NEW_MACOS_APP="/path/to/build/Solin.app"
python -m pytest tests/e2e/test_macos_app_upgrade_smoke.py -q
```

The macOS release workflow resolves the previous DMG for the same architecture,
validates its SHA-256, mounts it, copies out the old bundle, replaces it with the
new bundle in a temporary install directory, verifies that user state survived,
and starts the upgraded app. The first Apple Silicon build has no predecessor
and must pass its clean-install and native startup checks.

The Linux upgrade smoke receives the previous verified AppImage and the newly
built AppImage, runs each under Xvfb with native XCB, and verifies that the same
isolated user state survives. Release jobs set `SOLIN_E2E_STRICT=1`; under that
mode, a missing prerequisite is a failure rather than a successful skip.
