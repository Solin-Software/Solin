# Building and platform requirements

The repository has Windows x64, Intel macOS, and Linux x86_64 build recipes.
Generated files are written under `build/` or `dist/` and must not be committed.

The published `pylibobs` dependency excludes Intel macOS. The Intel macOS recipe
does not yet provide the default media backend.

Playback and scenes use the supervised libobs sidecar. Source runs launch its
Python module; standalone builds launch the Solin executable with
`--scene-engine-sidecar`, before importing the GUI or opening a profile.
Each build includes the `pylibobs` Python modules and stages the installed native
runtime under `pylibobs/_libs/<platform>/<architecture>`, including its plugins,
shader resources, dependencies and redistribution notices. Builds also stage
the OBS mux helper beside Solin so recording works in read-only installations;
Windows additionally stages the native encoder probe helpers.

Builds run `scripts/package_libobs_runtime.py` before producing release artifacts.
It validates the runtime files, adjusts private library paths on Linux/macOS and
requires a real packaged IPC handshake, decoded image pixels through preview
egress, media playback progress, heartbeat and clean shutdown. Runtime startup
or missing-plugin failures fail the
build. Linux qualification needs a graphics session; CI uses Xvfb. macOS
qualification runs in the macOS workflow and cannot be established by Windows
tests alone.

Install the `ffprobe` and `ffmpeg` command-line tools on PATH for titles,
durations, embedded cover art, and video thumbnails. Python dependencies alone
do not install those executables.

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

Build and test the native scene engine, the DirectShow filters for x64 and x86
consumers, and the pinned GStreamer development runtime with one command. Use
Release for camera installation and performance qualification:

```text
python scripts/build_native_engine.py --configuration Release
```

The C++ source and DirectShow filters remain in this repository; generated
executables and DLLs do not. Building the C++ engine is required for the optional
Windows backend selected with `SOLIN_SCENE_ENGINE=native`, and for the DirectShow
filters used by the Windows virtual camera. The default libobs scene engine
does not depend on that C++ executable. The launcher discovers the optional
native configuration under `build/native/`. Use the same Python environment
that runs Solin, for example:

```text
.venv\Scripts\python scripts\build_native_engine.py --configuration Release
.venv\Scripts\python main.py
```

Both filter DLLs are packaged with every Windows build. A current-user install
keeps the immutable filters under the user's local application data and
registers the 64-bit and 32-bit COM views in HKCU without elevation. An
all-users install keeps them under the protected 64-bit Program Files root and
registers both views in HKLM while Setup is elevated. Windows x86 itself is not
a supported host; the x86 DLL exists for 32-bit camera consumers on Windows x64.

Setup stages and verifies the immutable filter pair before installing application
files. A registration failure restores the preceding pair in the same scope.
Uninstall removes the registration immediately; files still loaded by a camera
consumer are retained until a later install can clean them up. Setup does not
schedule their deletion on reboot, so a reinstall cannot lose its filters to an
older uninstall's pending deletion.

For local virtual-camera testing, install the compiled filters from an ordinary
terminal. Registration and removal are explicit so ordinary builds never
modify the developer machine:

```text
.venv\Scripts\python scripts\manage_virtual_camera_directshow.py install
.venv\Scripts\python scripts\manage_virtual_camera_directshow.py status
.venv\Scripts\python scripts\manage_virtual_camera_directshow.py uninstall
```

The install command validates both PE architectures, copies the pair to one
content-addressed immutable version under the user's local application data,
registers x64 and x86 transactionally, and verifies each registry view and
DirectShow enumeration. A failed second architecture restores the previous
pair. Open consumers may retain an older loaded DLL; unreferenced version
directories are removed when Windows releases them. The camera remains
discoverable across Solin restarts and Windows reboots until the explicit
uninstall command removes both registrations.

The Release test suite gates the 8 ms P95 budget for the three important filter
paths (1080p NV12 copy, 720p-to-1080p scale, and 1080p YUY2 conversion). It can
also be run directly:

```text
cmake --build build/native/media-engine-gstreamer --config Release --target solin-virtual-camera-frame-adapter-benchmark
build\native\media-engine-gstreamer\Release\solin-virtual-camera-frame-adapter-benchmark.exe
```

CTest fails the Release build if any measured P95 exceeds the budget.

For the historical QtMultimedia-to-native media route measurements, including
fixture generation, explicit one-second CPU buckets, and provenance requirements, see
[Native media performance measurements](native-media-performance.md).

The manual `Build Solin Windows` workflow builds the standalone application and
full installer. Upgrade smoke testing additionally requires the URL of the
previously distributed installer.

The workflow signs by default. Diagnostic runs may explicitly disable signing;
their files and uploaded artifacts receive an `unsigned-diagnostic` suffix. A
build intended for distribution must keep `sign_windows_artifacts` enabled and
provide the repository secrets
`SOLIN_SIGNING_CERTIFICATE_BASE64` and
`SOLIN_SIGNING_CERTIFICATE_PASSWORD`. The workflow signs and verifies the app,
native sidecar, both DirectShow filter DLLs, and full installer. Inno Setup
signs the embedded uninstaller through the same required signing command before
the workflow produces checksums.

## macOS

The `Build Solin macOS` workflow is the maintained delivery recipe. It runs on
Intel macOS, builds the application with Nuitka, creates a DMG, and validates
both packaged startup and replacement of a previous application when requested.

The replacement smoke test requires the URL of the previously distributed DMG.

## Linux development

The embedded SideView browser uses GTK 3, WebKitGTK 4.1, libsoup 3, and
`pkg-config`. The libobs runtime needs the host's graphics and audio support.
Linux virtual-camera output additionally needs a `v4l2loopback` device and the
libobs `virtualcam_output` plugin. Automatic Zoom sharing from source requires
`xdotool` and an X11/XWayland session.

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
