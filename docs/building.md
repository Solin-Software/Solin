# Building and platform requirements

The repository supports Windows x86-64, macOS 13+ on Intel and Apple Silicon,
and Linux x86-64 with glibc 2.38 or newer. Generated files are written under
`build/` or `dist/` and must not be committed.

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

Packaged smoke tests also launch the delivered executable in its headless
`--verify-http-runtime` role. A local HTTP server verifies gzip decoding,
streamed responses and HTTP error handling through the production adapter,
without opening the GUI, changing preferences or contacting external services.

Install the `ffprobe` and `ffmpeg` command-line tools on PATH for titles,
durations, embedded cover art, and video thumbnails. Python dependencies alone
do not install those executables.

Install the Python dependencies before invoking a build. On Intel macOS, use the
[macOS binding bootstrap](#macos) below. On other platforms:

```text
python -m pip install -r requirements-dev.txt
python -m pip install -r requirements-build.txt
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

DirectShow graph checks cover standby/live/stale-producer transitions with
normal and deliberately slow consumers. They disable the renderer clock to
verify the filter's own pacing, preserve exact sample timestamps and reject
overproduction. Separate tests use controlled time to verify 30 fps deadlines,
lateness boundaries and recovery without bursts; the graph watchdog bounds a
stalled test rather than requiring a fixed frame count from a busy runner.

For the historical QtMultimedia-to-native media route measurements, including
fixture generation, explicit one-second CPU buckets, and provenance requirements, see
[Native media performance measurements](native-media-performance.md).

The `Build Solin Windows` workflow first creates a Nuitka staging tree and then
the single full installer used for clean installs and upgrades. The staging tree
is an installer input, not a public portable distribution. This is required
because Inno Setup performs the virtual-camera registration and transactional
rollback that a copied directory cannot provide. Release runs resolve and
verify their predecessor automatically; a manual diagnostic run may disable
upgrade smoke testing.

Upgrades replace the installer-owned `backports` runtime directory before
copying the new payload. Optional extensions left by an older local build must
not remain importable when their compiled Python package is absent from the
new executable. The upgrade smoke seeds an obsolete extension and verifies
its removal, HTTP operation and preservation of unrelated files and user state.

Windows artifacts are currently unsigned, matching the existing distribution
model. Release integrity is enforced by the exact asset inventory, immutable
GitHub release, recorded size, and SHA-256 verification before installation.
Windows may consequently identify the publisher as unknown when the installer
requests elevation.

## macOS

The `Build Solin macOS` workflow is the maintained delivery recipe. Its native
Intel and Apple Silicon jobs build with Nuitka, apply ad-hoc signatures, create
architecture-specific DMGs, verify the bundle architecture, and validate
packaged startup and application replacement.

Published `pylibobs` wheels provide the runtime on Windows and Linux. On both
macOS architectures, bootstrap a local `pylibobs==0.1.2` wheel against the official
OBS 32.1.2 runtime before installing requirements. The helper preserves the
`libobs.framework` identity required for Cocoa resource lookup and relocates its
native dependencies before signing and validating them:

```text
python -m pip install setuptools==80.10.2 wheel==0.46.3 cffi==2.0.0
python scripts/build_pylibobs_macos.py --output-dir .pip-wheels
python -m pip install --no-index --find-links .pip-wheels --no-deps pylibobs
python -m pip install -r requirements-dev.txt
python -m pip install --no-deps -e .
```

The helper verifies the downloads, target architecture and native initialization.
It leaves other hosts unchanged. The workflow uses the local wheel during its
binary dependency download and runs the same packaged runtime qualification.
Release replacement tests resolve the previous DMG for the same architecture.
The first Apple Silicon release runs clean-install and startup checks because no
native predecessor exists. Ad-hoc signing does not provide a Developer ID or
notarization.

## Linux development

Ubuntu 24.04 x86_64 is the reference Linux platform for development, CI and
AppImage builds. Ubuntu 22.04 is unsupported by this delivery recipe. The pinned
`pylibobs==0.1.2` Linux wheel is tagged `manylinux_2_31_x86_64`, but its ELF
version requirements reach `GLIBC_2.34`. Its native dependency set from Ubuntu
24.04 raises the minimum further: `libsrt.so.1.5`, `librist.so.4`,
`libx264.so.164` and `libvpl.so.2` require `GLIBC_2.38`. Ubuntu 24.04 provides
glibc 2.39. The wheel tag alone does not establish distribution compatibility.

The embedded SideView browser uses GTK 3, WebKitGTK 4.1, libsoup 3, and
`pkg-config`. The libobs runtime needs the host's graphics and audio support.
Linux virtual-camera output additionally needs a `v4l2loopback` device and the
libobs `virtualcam_output` plugin. Automatic Zoom sharing from source requires
`xdotool` and an X11/XWayland session.

SideView and its native backend are installed from PyPI through the Python
dependencies. No SideView binary is vendored in this repository.

Install the native dependencies from the Ubuntu 24.04 archive before installing
the Python requirements. The build script supplies the same explicit libobs
package list used by both CI workflows; apt resolves its transitive dependencies:

```bash
mapfile -t libobs_packages < <(bash scripts/build_solin.sh --print-native-packages)
sudo apt-get update
sudo apt-get install --no-install-recommends "${libobs_packages[@]}" \
    binutils build-essential curl dpkg patchelf pkg-config xdotool ffmpeg \
    libgtk-3-dev libwebkit2gtk-4.1-dev libxcb-cursor0 libxkbcommon-x11-0 xauth xvfb
```

On a Wayland desktop, run Solin through XWayland when the embedded native view
is needed:

```text
QT_QPA_PLATFORM=xcb python main.py
```

## Linux standalone and AppImage

Linux builds must run on Ubuntu 24.04 x86_64 or its WSL 2 distribution, with
Python 3.13 or newer and the native dependencies above. The standalone build
entry point is:

```text
python -m pip install -r requirements-build.txt
xvfb-run --auto-servernum bash scripts/build_solin.sh
```

Set `SOLIN_PYTHON` only when the desired interpreter is not discoverable through
the environment or repository-local environment.

The script downloads the official `OBS-Studio-32.1.2-Ubuntu-24.04-x86_64.deb`,
verifies SHA-256
`a3bb1b0176604dad9e22710e057f0fdd76e8afb600e0e1914c30464ae49908e8`,
and reuses only a verified archive in the build's dependency cache. It extracts
the missing `usr/local/bin/obs-ffmpeg-mux` with `dpkg-deb` and passes it to runtime
staging. This supplies the wheel's matching recording helper without installing
the OBS application or using a third-party apt repository. The temporary
extraction is removed when the build exits.

The AppImage entry point is:

```text
xvfb-run --auto-servernum bash scripts/package_solin_appimage.sh
```

From Windows, the WSL wrapper can run the complete build:

```text
scripts\build_solin_appimage.bat
```

The wrapper defaults to `Ubuntu-24.04` and runs full builds under Xvfb. Install
that WSL distribution and its dependencies first; `SOLIN_WSL_DISTRO` or
`-Distribution` selects another compatible distribution explicitly.

Pass `--skip-standalone` to the wrapper when `build/linux/main.dist` is already
current. The resulting AppImage and SHA-256 file are written to `dist/`.

The AppImage packages the application and its Python/Qt dependencies while
using the host distribution's security-maintained WebKitGTK stack. Its Zoom
automation remains limited to X11/XWayland. AppImage packaging does not lower
the native glibc requirements or establish support for Ubuntu 22.04. Native
Linux rendering, decoding and packaging qualification are gated by the Ubuntu
24.04 workflow; Windows tests do not establish those results.

## Build workflows

Platform workflows support manual diagnostics and reusable release calls. Only
the tag-driven release orchestrator can gather all four required targets and
publish them. See [Versioning and releases](releases.md).
