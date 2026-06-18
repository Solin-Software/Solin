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
terminates the process. Upgrade-install data survival still requires the full
installer artifacts and remains a release checklist item.
