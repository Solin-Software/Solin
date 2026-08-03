@echo off
REM Build the Solin Virtual Camera media source (Windows 11, MSVC x64).
REM
REM Usage:  build.bat            -> builds solin-mfcam.dll next to this script
REM
REM Requires Visual Studio Build Tools 2022 with the C++ workload and a
REM Windows 10/11 SDK. No OBS headers are needed: this component talks to
REM Media Foundation, not libobs.

setlocal
set VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe
if not exist "%VSWHERE%" (echo Visual Studio Build Tools not found & exit /b 1)

for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -property installationPath`) do set VSPATH=%%i
if not defined VSPATH (echo No Visual Studio installation found & exit /b 1)

call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (echo vcvars64 failed & exit /b 1)

cd /d "%~dp0"
cl /nologo /LD /EHsc /W3 /O2 /DUNICODE /D_UNICODE ^
   solin-mfcam.cpp ^
   /Fe:solin-mfcam.dll ^
   /link /DEF:solin-mfcam.def mfplat.lib mfuuid.lib ole32.lib advapi32.lib
if errorlevel 1 (echo BUILD FAILED & exit /b 1)

echo.
echo Built: %~dp0solin-mfcam.dll

REM "build.bat check" also builds the end-to-end verification harness.
if /i "%~1"=="check" (
    cl /nologo /EHsc /W3 /O2 /DUNICODE /D_UNICODE vcam-check.cpp /Fe:vcam-check.exe
    if errorlevel 1 (echo CHECK BUILD FAILED & exit /b 1)
    echo Built: %~dp0vcam-check.exe
)
endlocal
