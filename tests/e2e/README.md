# End-to-End Tests

This suite contains artifact-gated packaged application checks. Local test runs
skip them unless the required release artifacts are provided explicitly.

Run the startup smoke after producing a standalone build:

```powershell
$env:SOLIN_PACKAGED_EXE = "C:\path\to\main.dist\Solin.exe"
.\.venv\Scripts\python.exe -m pytest tests\e2e -q
```

The smoke launches the packaged executable with isolated `APPDATA` and
`LOCALAPPDATA`, verifies that it stays alive through the startup window, then
terminates the process.

Run the full-installer upgrade smoke only on a disposable Windows test machine
or CI runner with no existing Solin installation:

```powershell
$env:SOLIN_E2E_ALLOW_INSTALLER_MUTATION = "1"
$env:SOLIN_OLD_INSTALLER = "C:\path\to\old\Solin_Setup_1.0.0.exe"
$env:SOLIN_NEW_INSTALLER = "C:\path\to\new\Solin_Setup_1.1.0.exe"
.\.venv\Scripts\python.exe -m pytest tests\e2e\test_installer_upgrade_smoke.py -q
```

The upgrade smoke installs into a temporary directory, seeds profile,
settings, playlist, and meeting-tree sentinels, installs the new full
installer over it, verifies that user state survived, starts the upgraded app,
then runs the generated uninstaller and removes its test registry keys.
