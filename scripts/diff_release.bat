@echo off
color 0b
echo Carregando...
title Diff Patch
chcp 1258>nul
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "BUILD_DIR=%PROJECT_ROOT%\build"
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
"%PYTHON%" "%~dp0diff_release.py" --old "%BUILD_DIR%\main.dist-old" --new "%BUILD_DIR%\main.dist" --out "%BUILD_DIR%\diffnew"
timeout /t 2 >nul
pause
