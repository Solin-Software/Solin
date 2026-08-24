@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  build_solin.bat — Compila o projeto Solin com Nuitka
::  Modo: standalone (pasta distribuivel, sem onefile)
::  Script em scripts\. Resolve a raiz do projeto automaticamente.
:: ============================================================

for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI\"
set MAIN_SCRIPT=%PROJECT_ROOT%main.py
set OUTPUT_DIR=%PROJECT_ROOT%build
set APP_NAME=Solin
set ICON=%PROJECT_ROOT%src\solin\resources\assets\icon.ico
set "PYTHON="
if defined SOLIN_PYTHON set "PYTHON=%SOLIN_PYTHON%"
if not defined PYTHON if exist "%PROJECT_ROOT%.venv\Scripts\python.exe" (
    set "PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
)
if not defined PYTHON (
    where python.exe >nul 2>nul
    if errorlevel 1 (
        echo  [ERRO] Python 3.13 ou superior nao foi encontrado.
        echo  Defina SOLIN_PYTHON ou disponibilize python no PATH.
        exit /b 1
    )
    set "PYTHON=python"
)
set QML_SOURCE_DIR=%PROJECT_ROOT%src\solin\qml
set QML_MODULE_ROOT=%PROJECT_ROOT%build\qmlcache\solin\qml
set QML_CACHE_DIR=%QML_MODULE_ROOT%\Solin
set QT_QML_CACHE_DIR=%PROJECT_ROOT%build\qmlcache\PySide6\qml
set QT_LIBRARY_CACHE_DIR=%PROJECT_ROOT%build\qmlcache\qt-libs

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>nul
if errorlevel 1 (
    echo  [ERRO] Solin requer Python 3.13 ou superior.
    exit /b 1
)

for /f "delims=" %%I in ('call "%PYTHON%" -c "from pathlib import Path; import sideview; print(Path(sideview.__file__).resolve().parent)"') do set "SIDEVIEW_PACKAGE_DIR=%%I"
if not defined SIDEVIEW_PACKAGE_DIR (
    echo  [ERRO] O pacote SideView nao esta instalado no ambiente Python.
    pause
    exit /b 1
)
set "SIDEVIEW_NATIVE_DLL=%SIDEVIEW_PACKAGE_DIR%\sideview_native.dll"
if not exist "%SIDEVIEW_NATIVE_DLL%" (
    echo  [ERRO] O backend nativo do SideView nao foi encontrado em %SIDEVIEW_NATIVE_DLL%.
    pause
    exit /b 1
)

rmdir /s /q "%PROJECT_ROOT%.venv\Lib\site-packages\win32com\gen_py" 2>nul
rmdir /s /q "%LOCALAPPDATA%\Temp\gen_py" 2>nul
rmdir /s /q "%LOCALAPPDATA%\comtypes\Cache" 2>nul

:: "%PYTHON%" -m nuitka --clean-cache=all

:: ── Metadados do executavel ───────────────────────────────
set "VERSION_FILE=%PROJECT_ROOT%src\solin\version.py"
for /f "delims=" %%V in ('call "%PYTHON%" -c "import pathlib; ns={}; exec(pathlib.Path(r'%VERSION_FILE%').read_text(encoding='utf-8'), ns); print(ns['__version__'])"') do set "VERSION=%%V"
if not defined VERSION (
    echo  [ERRO] Nao foi possivel ler a versao em src\solin\version.py.
    pause
    exit /b 1
)
set PRODUCT_NAME=Solin
set COMPANY_NAME=Solin Software
set DESCRIPTION=Solin - Audio and Video for Kingdom Hall meetings
set COPYRIGHT=Copyright (c) 2026 Alexsander. All rights reserved.

echo.
echo  =========================================
echo   Compilando %APP_NAME% com Nuitka...
echo  =========================================
echo.

echo  =========================================
echo   Compilando e testando o motor nativo...
echo  =========================================
echo.

"%PYTHON%" "%PROJECT_ROOT%scripts\build_native_engine.py" --configuration Release
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERRO] Compilacao do motor nativo falhou com codigo %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)

rmdir /s /q "%QML_MODULE_ROOT%" 2>nul

"%PYTHON%" "%PROJECT_ROOT%scripts\compile_qml_cache.py" ^
    --source-dir "%QML_SOURCE_DIR%" ^
    --output-dir "%QML_CACHE_DIR%" ^
    --qt-qml-output-dir "%QT_QML_CACHE_DIR%" ^
    --qt-library-output-dir "%QT_LIBRARY_CACHE_DIR%" ^
    --qt-qml-module "QtQuick/Controls/impl" ^
    --qt-qml-module "QtQuick/Controls/Basic/impl"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERRO] Geracao do cache compilado de QML falhou com codigo %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)

"%PYTHON%" -m nuitka ^
    --standalone ^
    --output-dir="%OUTPUT_DIR%" ^
    --output-filename="%APP_NAME%" ^
    --windows-icon-from-ico="%ICON%" ^
    --windows-console-mode=attach ^
    --assume-yes-for-downloads ^
    --windows-file-version=%VERSION% ^
    --windows-product-version=%VERSION% ^
    --windows-product-name="%PRODUCT_NAME%" ^
    --windows-company-name="%COMPANY_NAME%" ^
    --windows-file-description="%DESCRIPTION%" ^
    --copyright="%COPYRIGHT%" ^
    --follow-import-to=solin ^
    --follow-import-to=solin.core ^
    --follow-import-to=solin.styles ^
    --follow-import-to=solin.widgets ^
    --nofollow-import-to=PySide6.QtTranslations ^
	--nofollow-import-to=win32com.gen_py ^
	--nofollow-import-to=comtypes.gen ^
	--noinclude-setuptools-mode=nofollow ^
    --include-package=solin ^
    --include-package=solin.core ^
    --include-package=solin.styles ^
    --include-package=solin.widgets ^
    --include-package=sideview ^
	--include-module=websocket ^
	--include-module=websocket._core ^
	--include-module=websocket._app ^
	--include-package=ephem ^
    --include-package=keyring ^
    --include-package=pywinauto ^
    --include-package=comtypes ^
    --include-package-data=pyqttoast ^
    --include-data-dir="%PROJECT_ROOT%src\solin\resources\assets=solin/resources/assets" ^
    --include-data-dir="%PROJECT_ROOT%src\solin\resources\remote_control=solin/resources/remote_control" ^
    --include-data-dir="%PROJECT_ROOT%src\solin\resources\translations\locales=solin/resources/translations/locales" ^
    --include-data-dir="%QML_CACHE_DIR%=solin/qml/Solin" ^
    --include-data-dir="%QT_QML_CACHE_DIR%=PySide6/qml" ^
    --include-data-files="%QT_QML_CACHE_DIR%=PySide6/qml/=**/*.dll" ^
    --include-data-files="%QT_LIBRARY_CACHE_DIR%\*.dll=./" ^
    --include-data-files="%PROJECT_ROOT%src\solin\resources\translations\*.qm=solin/resources/translations/" ^
    --include-data-files="%SIDEVIEW_NATIVE_DLL%=sideview/sideview_native.dll" ^
    --enable-plugin=pyside6 ^
    --include-qt-plugins=platforms,styles,imageformats,multimedia,position ^
    "%MAIN_SCRIPT%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERRO] Compilacao falhou com codigo %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)

:: ── Limpeza pos-build ─────────────────────────────────────
echo.
echo  Removendo arquivos desnecessarios...
 
set DIST=%OUTPUT_DIR%\main.dist

if not exist "%DIST%\sideview\sideview_native.dll" (
    echo  [ERRO] O backend nativo do SideView nao foi empacotado.
    pause
    exit /b 1
)
 
:: _avif nao usado
if exist "%DIST%\PIL\_avif.pyd" (
    del /f /q "%DIST%\PIL\_avif.pyd"
    echo  [OK] _avif.pyd removido
)
 
:: DevTools debug pak — so necessario para depuracao do Chromium
if exist "%DIST%\qtwebengine_devtools_resources.debug.pak" (
    del /f /q "%DIST%\qtwebengine_devtools_resources.debug.pak"
    echo  [OK] qtwebengine_devtools_resources.debug.pak removido
)

del /f /q "%DIST%\solin\qml\*.qmlc" 2>nul
del /f /q "%DIST%\solin\qml\*.qml" 2>nul
del /f /q "%DIST%\solin\qml\qmldir" 2>nul

if not exist "%DIST%\solin\qml\Solin\*.qmlc" (
    echo  [ERRO] Nenhum cache QML .qmlc foi empacotado em %DIST%\solin\qml\Solin.
    pause
    exit /b 1
)

for %%F in ("%DIST%\solin\qml\Solin\*.qml") do (
    if exist "%%~fF" if /I "%%~xF"==".qml" (
        del /f /q "%%~fF"
        echo  [OK] QML fonte removido: %%~nxF
    )
)

if not exist "%DIST%\solin\qml\Solin\qmldir" (
    echo  [ERRO] Metadados qmldir dos QML compilados nao foram empacotados.
    pause
    exit /b 1
)

set RAW_QML_FOUND=
for %%F in ("%DIST%\solin\qml\Solin\*.qml") do (
    if exist "%%~fF" if /I "%%~xF"==".qml" set RAW_QML_FOUND=1
)

if defined RAW_QML_FOUND (
    echo  [ERRO] Arquivos QML fonte permaneceram em %DIST%\solin\qml\Solin.
    pause
    exit /b 1
)

if not exist "%DIST%\PySide6\qml\QtQuick\qmldir" (
    echo  [ERRO] Runtime QML QtQuick nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\PySide6\qml\QtQml\qmldir" (
    echo  [ERRO] Runtime QML QtQml nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\PySide6\qml\QtMultimedia\quickmultimediaplugin.dll" (
    echo  [ERRO] Plugin QML QtMultimedia nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\PySide6\qml\QtQuick\Layouts\qquicklayoutsplugin.dll" (
    echo  [ERRO] Plugin QML qquicklayoutsplugin.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\PySide6\qml\QtQuick\Controls\qtquickcontrols2plugin.dll" (
    echo  [ERRO] Plugin QML qtquickcontrols2plugin.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\PySide6\qml\QtQuick\Controls\impl\qtquickcontrols2implplugin.dll" (
    echo  [ERRO] Plugin QML qtquickcontrols2implplugin.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\PySide6\qml\QtQuick\Controls\Basic\impl\qtquickcontrols2basicstyleimplplugin.dll" (
    echo  [ERRO] Plugin QML qtquickcontrols2basicstyleimplplugin.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\PySide6\qml\QtQuick\Templates\qtquicktemplates2plugin.dll" (
    echo  [ERRO] Plugin QML qtquicktemplates2plugin.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\Qt6QuickLayouts.dll" (
    echo  [ERRO] Qt6QuickLayouts.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\opengl32sw.dll" (
    echo  [ERRO] Fallback OpenGL por software do Qt nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\Qt6QuickControls2.dll" (
    echo  [ERRO] Qt6QuickControls2.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\Qt6QuickControls2Impl.dll" (
    echo  [ERRO] Qt6QuickControls2Impl.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\Qt6QuickControls2BasicStyleImpl.dll" (
    echo  [ERRO] Qt6QuickControls2BasicStyleImpl.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\Qt6QuickTemplates2.dll" (
    echo  [ERRO] Qt6QuickTemplates2.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\Qt6Multimedia.dll" (
    echo  [ERRO] Qt6Multimedia.dll nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\Qt6MultimediaQuick.dll" (
    echo  [ERRO] Qt6MultimediaQuick.dll nao foi empacotado.
    pause
    exit /b 1
)

echo  [OK] QML do app empacotado somente como cache .qmlc

"%PYTHON%" "%PROJECT_ROOT%scripts\package_native_engine_windows.py" ^
    --gstreamer-root "%PROJECT_ROOT%build\dependencies\gstreamer\msvc_x86_64" ^
    --gstreamer-licenses "%PROJECT_ROOT%build\dependencies\gstreamer\msvc_x86_64\share\licenses" ^
    --application-dir "%DIST%"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERRO] Empacotamento do motor nativo falhou com codigo %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)

if not exist "%DIST%\native\media-engine\solin-media-engine.exe" (
    echo  [ERRO] O motor nativo nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\native\media-engine\virtual-camera\x64\solin-virtual-camera.dll" (
    echo  [ERRO] O filtro DirectShow x64 nao foi empacotado.
    pause
    exit /b 1
)

if not exist "%DIST%\native\media-engine\virtual-camera\x86\solin-virtual-camera.dll" (
    echo  [ERRO] O filtro DirectShow x86 nao foi empacotado.
    pause
    exit /b 1
)

echo.
echo  [OK] Compilacao concluida. Saida em: %OUTPUT_DIR%
echo.
pause
