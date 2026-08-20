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

Build and test the native scene engine, the DirectShow filters for x64 and x86
consumers, and the pinned GStreamer development runtime with one command. Use
Release for camera installation and performance qualification:

```text
python scripts/build_native_engine.py --configuration Release
```

The native source belongs in this repository so Python and C++ contracts change
atomically; generated executables and DLLs do not. A source checkout therefore
reports the scene engine as unavailable until this command succeeds. The local
launcher discovers the resulting configuration under `build/native/` without a
manual copy. Use the same Python environment that runs Solin, for example:

```text
.venv\Scripts\python scripts\build_native_engine.py --configuration Release
.venv\Scripts\python main.py
```

Both filter DLLs are packaged with every Windows build. The installer registers
them only for the installing account, in the corresponding 64-bit and 32-bit
per-user COM views. Registration does not require elevation, including when the
application itself is installed for all users. Windows x86 itself is not a
supported host; the x86 DLL exists for 32-bit camera consumers on Windows x64.

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

For process-level measurements of the complete current media route, including reproducible
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
