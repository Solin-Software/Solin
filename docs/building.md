# Building and platform requirements

The repository supports Windows x64, Intel macOS, and Linux x86_64 build
targets. Generated files are written under `build/` or `dist/` and must not be
committed.

Install the Python dependencies before invoking a build:

```text
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

## Windows

The local Windows build uses Nuitka:

```text
scripts\build_solin.bat
```

The script accepts `SOLIN_PYTHON` when a specific interpreter is required,
otherwise it uses the repository environment when available and then the
`python` command. The installer recipe is
`packaging/windows/installer/setup.iss` and requires Inno Setup 6.

The manual `Build Solin Windows` workflow builds the standalone application and
full installer. Upgrade smoke testing additionally requires the URL of the
previously distributed installer.

## macOS

The `Build Solin macOS` workflow is the maintained delivery recipe. It runs on
Intel macOS, builds the application with Nuitka, creates a DMG, and validates
both packaged startup and replacement of a previous application when requested.

The replacement smoke test requires the URL of the previously distributed DMG.

## Linux development

The embedded SideView browser uses GTK 3, WebKitGTK 4.1, libsoup 3, and
`pkg-config`. QtMultimedia may additionally require the distribution's VA-API,
PipeWire, and GStreamer plugin packages. Automatic Zoom sharing from source
requires `xdotool` and an X11/XWayland session.

SideView and its native backend are installed from PyPI through the Python
dependencies. No SideView binary is vendored in this repository.

On a Wayland desktop, run Solin through XWayland when the embedded native view
is needed:

```text
QT_QPA_PLATFORM=xcb python main.py
```

## Linux standalone and AppImage

Linux builds must run on Linux or WSL 2. The standalone build entry point is:

```text
python -m pip install nuitka ordered-set zstandard
bash scripts/build_solin.sh
```

Set `SOLIN_PYTHON` only when the desired interpreter is not discoverable through
the environment or repository-local environment.

The AppImage entry point is:

```text
bash scripts/package_solin_appimage.sh
```

From Windows, the WSL wrapper can run the complete build:

```text
scripts\build_solin_appimage.bat
```

Pass `--skip-standalone` to the wrapper when `build/linux/main.dist` is already
current. The resulting AppImage and SHA-256 file are written to `dist/`.

The AppImage packages the application and its Python/Qt dependencies while
using the host distribution's security-maintained WebKitGTK stack. Its Zoom
automation remains limited to X11/XWayland.

## Build workflows

Platform build workflows are intentionally manual. They produce short-lived
verification artifacts; distributing the application is a separate step. See
[Versioning and releases](releases.md).
