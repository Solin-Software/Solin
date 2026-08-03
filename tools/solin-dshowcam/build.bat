@echo off
REM Build the Solin Virtual Camera DirectShow filter.
REM
REM   build.bat          -> x64 filter
REM   build.bat x86      -> 32-bit filter (for 32-bit hosts)
REM
REM Requires Visual Studio Build Tools 2022 (C++ workload) and a Windows SDK.
REM The DirectShow BaseClasses are vendored under baseclasses/ (MIT licensed,
REM see baseclasses/LICENSE.MIT) because the SDK ships strmbase.lib without the
REM headers needed to derive from CSource/CSourceStream.

setlocal
set ARCH=%~1
if "%ARCH%"=="" set ARCH=x64
if /i "%ARCH%"=="check" set ARCH=x64

set VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe
if not exist "%VSWHERE%" (echo Visual Studio Build Tools not found & exit /b 1)
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -property installationPath`) do set VSPATH=%%i
if not defined VSPATH (echo No Visual Studio installation found & exit /b 1)

if /i "%ARCH%"=="x86" (
    call "%VSPATH%\VC\Auxiliary\Build\vcvars32.bat" >nul 2>&1
) else (
    call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
)
if errorlevel 1 (echo vcvars failed & exit /b 1)

cd /d "%~dp0"
if not exist obj\%ARCH% mkdir obj\%ARCH%
if not exist obj\%ARCH%\filter mkdir obj\%ARCH%\filter

REM /MT (static CRT) is mandatory: this DLL is loaded into Chrome, Zoom and
REM Teams, and a /MD build would add a VCRUNTIME140.dll dependency the host may
REM not have. The failure mode is silent — the device enumerates but never loads.
set CFLAGS=/nologo /EHsc /MT /O2 /W3 /DUNICODE /D_UNICODE /DWIN32 /D_WINDOWS /DNDEBUG /D_CRT_SECURE_NO_WARNINGS /I baseclasses

echo Building BaseClasses (%ARCH%)...
cl %CFLAGS% /c /Fo:obj\%ARCH%\ ^
   baseclasses\source.cpp    baseclasses\amfilter.cpp   baseclasses\combase.cpp ^
   baseclasses\mtype.cpp     baseclasses\wxutil.cpp     baseclasses\wxlist.cpp ^
   baseclasses\wxdebug.cpp   baseclasses\dllentry.cpp   baseclasses\dllsetup.cpp ^
   baseclasses\amvideo.cpp   baseclasses\outputq.cpp    baseclasses\ctlutil.cpp ^
   baseclasses\refclock.cpp  baseclasses\sysclock.cpp   baseclasses\amextra.cpp ^
   baseclasses\arithutil.cpp baseclasses\winutil.cpp    baseclasses\videoctl.cpp ^
   baseclasses\ddmm.cpp > obj\%ARCH%\baseclasses.log 2>&1
if errorlevel 1 (echo BASECLASSES BUILD FAILED - see obj\%ARCH%\baseclasses.log & exit /b 1)
lib /nologo /OUT:obj\%ARCH%\strmbase_local.lib obj\%ARCH%\*.obj >nul
if errorlevel 1 (echo LIB FAILED & exit /b 1)

echo Building filter (%ARCH%)...
if /i "%ARCH%"=="x86" (set OUT=solin-dshowcam-x86.dll) else (set OUT=solin-dshowcam-x64.dll)
cl %CFLAGS% solin-dshowcam.cpp /Fo:obj\%ARCH%\filter\ /Fe:%OUT% ^
   /link /DEF:solin-dshowcam.def ^
   obj\%ARCH%\strmbase_local.lib strmiids.lib ole32.lib oleaut32.lib uuid.lib ^
   advapi32.lib winmm.lib user32.lib gdi32.lib
if errorlevel 1 (echo FILTER BUILD FAILED & exit /b 1)

echo.
echo Built: %~dp0%OUT%

if /i "%~1"=="check" (
    cl %CFLAGS% dshow-check.cpp /Fo:obj\%ARCH%\chk\ /Fe:dshow-check.exe
    if errorlevel 1 (echo CHECK BUILD FAILED & exit /b 1)
    echo Built: %~dp0dshow-check.exe
)
endlocal
