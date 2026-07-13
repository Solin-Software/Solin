@echo off
setlocal

set "POWERSHELL_ARGS="
if /I "%~1"=="--skip-standalone" (
    set "POWERSHELL_ARGS=-SkipStandalone"
)

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
    -File "%~dp0build_solin_appimage.ps1" %POWERSHELL_ARGS%
exit /b %ERRORLEVEL%
