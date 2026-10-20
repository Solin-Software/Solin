# Building and platform requirements

The repository supports Windows x86-64, macOS 13+ on Intel and Apple Silicon,
and Linux x86-64 with glibc 2.38 or newer. Generated files are written under
`build/` or `dist/` and must not be committed.

Build recipes generate Qt `.qm` catalogs from versioned `.ts` sources before
packaging; no precompiled catalog is required in the checkout. Wheels use the
same compiler through setuptools, with a pinned PySide6 build dependency for
isolated builds. See [Translations](translations.md#development-and-builds) for
development preparation and the source/distribution resource contract.

Playback and scenes use the supervised libobs sidecar. Source runs and wheel
installations launch its Python module; standalone builds launch the Solin
executable with
`--scene-engine-sidecar`, before importing the GUI or opening a profile.
Each build includes the `pylibobs` Python modules and stages the installed native
runtime under `pylibobs/_libs/<platform>/<architecture>`, including its plugins,
shader resources, dependencies and redistribution notices. Builds copy Solin's
`LICENSE` and `NOTICE` under `licenses/solin`. They also stage
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

The compiled executable also accepts the isolated
`--verify-media-runtime <fixture-directory>` role. Packaged smokes pass
`tests/fixtures/media_extract` and require ordered metadata, frame, video-cover,
audio-cover and absent-cover checks through the delivered Python client and its
production helper locator. The role emits `solin.media-runtime.v1` JSON and exits
after closing its owned extraction processes, before importing Qt or creating
application state. The E2E harness clears PATH, graphics variables and injected
loader paths; the AppImage launcher uses shell builtins for payload discovery.

Titles, durations, embedded cover art, and video thumbnails use Solin's small
`solin-media-extract` executable and the FFmpeg libraries already supplied with
the private libobs runtime. No external `ffprobe` or `ffmpeg` executable is
required. Extraction does not initialize OBS, a graphics session, or a render
context.

Install the Python dependencies before invoking a build. On Intel macOS, use the
[macOS binding bootstrap](#macos) below. On other platforms:

```text
python -m pip install -r requirements-dev.txt
python -m pip install -r requirements-build.txt
python -m pip install -e .
```

Before running from source or executing the Python suites, install CMake 3.25+
and a C++17 toolchain (MSVC on Windows, Clang on macOS, the Ubuntu build tools on
Linux), then run with the application's Python interpreter:

```text
python scripts/build_media_extract.py
```

The build uses `native/media_extract`, writes compiler output under
`build/native/media-extract`, and installs the qualified helper next to the
installed `pylibobs/_libs/<platform>/<architecture>` runtime. It replaces only
Solin's own helper. Linux fills missing dependency files from the same installed
Ubuntu packages without overwriting existing wheel libraries; macOS adds relative
FFmpeg library aliases when necessary. Release recipes run this bootstrap
automatically. Python dependency installation alone does not compile the helper.

Windows x64 and both macOS architectures use the official `obs-deps 2025-08-23`
SDK, verified against fixed SHA-256 values. Its FFmpeg 7.1.1 public headers match
libavcodec/libavformat 61, libavutil 59 and libswscale 8. SDK binaries are build
inputs only and are never substituted for the installed runtime. Ubuntu 24.04
uses the same archive's `libavcodec-dev`, `libavformat-dev`, `libavutil-dev` and
`libswscale-dev`, matching runtime majors 60/60/58/7. The helper checks header
and loaded runtime versions and exports their actual library paths through
`--version`; packaging rejects a mismatched ABI or a library outside the private
runtime.

Successful staging includes `media-extract-manifest.json` beside the helper. The
receipt records protocol, target architecture, compiled/loaded library versions,
relative library paths, SHA-256 hashes, and completed qualification checks. It
contains no build-machine paths. Qualification removes native runtime overrides
so it can only validate the staged dependency graph.

The packaging script qualifies metadata, Unicode titles, covers and decoded BGRA
pixels before publishing the runtime. It uses generated WAV/PNG data and the
small repository fixtures under `tests/fixtures/media_extract`, with external
CLI and graphics environment lookup disabled. Use `--qualification-fixtures`
to supply that fixture directory explicitly to `package_libobs_runtime.py`.
FFmpeg executables available on CI are optional fixture-development tools, not
application runtime dependencies.

## Windows

The local Windows build uses Nuitka:

```text
scripts\build_solin.bat
```

The script accepts `SOLIN_PYTHON` when a specific interpreter is required,
otherwise it uses the repository environment when available and then the
`python` command. The installer recipe is
`packaging/windows/installer/setup.iss` and requires Inno Setup 6.

Build and test the DirectShow filters and camera transport for x64 and x86
consumers with CMake and the MSVC C++ tools. Use Release for camera installation
and performance qualification:

```text
python scripts/build_virtual_camera.py --configuration Release
```

The C++ camera source lives in [`native/virtual_camera`](../native/virtual_camera).
Windows builds produce `build/native/virtual-camera-x64/Release/solin-virtual-camera.dll`
and `build/native/virtual-camera-x86/Release/solin-virtual-camera.dll`. The camera
transport is independent of the libobs scene engine; the libobs sidecar publishes
frames for the DirectShow filters. Generated DLLs are not committed.

The local build and Windows workflow stage the pair with
`scripts/package_virtual_camera_windows.py --application-dir build/main.dist`.
The defaults use the Release paths above. Custom builds pass
`--virtual-camera-filter-x64` and `--virtual-camera-filter-x86` explicitly.
The payload contains `native/virtual-camera/{x64,x86}/solin-virtual-camera.dll`,
third-party notices under `native/virtual-camera/share/licenses/solin-virtual-camera`,
and a `runtime-manifest.json` recording architectures, system imports and SHA-256
hashes. Architecture and import audits always run. Verification checks the staged
manifest and file inventory before transactional replacement of an existing pair;
`--skip-verification` skips that final check.

Portable transport tests can also run on Linux or macOS:

```text
cmake -S native/virtual_camera -B build/native/virtual-camera-ci -DCMAKE_BUILD_TYPE=Release -DSOLIN_VIRTUAL_CAMERA_BUILD_TESTS=ON
cmake --build build/native/virtual-camera-ci --config Release --parallel
ctest --test-dir build/native/virtual-camera-ci -C Release --output-on-failure
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
cmake --build build/native/virtual-camera-x64 --config Release --target solin-virtual-camera-frame-adapter-benchmark
build\native\virtual-camera-x64\Release\solin-virtual-camera-frame-adapter-benchmark.exe
```

CTest fails the Release build if any measured P95 exceeds the budget.

DirectShow graph checks cover standby/live/stale-producer transitions with
normal and deliberately slow consumers. They disable the renderer clock to
verify the filter's own pacing, preserve exact sample timestamps and reject
overproduction. Separate tests use controlled time to verify 30 fps deadlines,
lateness boundaries and recovery without bursts; the graph watchdog bounds a
stalled test rather than requiring a fixed frame count from a busy runner.

The `Build Solin Windows` workflow first creates a Nuitka staging tree and then
the single full installer used for clean installs and upgrades. The staging tree
is an installer input, not a public portable distribution. This is required
because Inno Setup performs the virtual-camera registration and transactional
rollback that a copied directory cannot provide. Release runs resolve and
verify their predecessor automatically; a manual diagnostic run may disable
upgrade smoke testing.

Upgrades remove the installer-owned `backports` runtime directory and retired
`native/media-engine` payload before copying the new files. This removes the old
engine executable, Qt bridge and private runtime. Optional extensions left by
an older local build must not remain importable when their compiled Python
package is absent from the new executable. Cleanup leaves `native/virtual-camera`,
other native files and the independently managed camera versions intact.
The upgrade smoke seeds an obsolete extension and retired engine files and
verifies their removal, the current packaged and managed camera pair, HTTP
operation and preservation of unrelated files and user state.

The DirectShow filters are staged as an immutable x64/x86 pair identified by
their binary hashes, independently of the application version. Reinstalling
the same pair reuses its files; rebuilding the same application version stages
a different pair without overwriting DLLs loaded by camera consumers. Uninstall
removes both scoped camera registrations immediately. Windows keeps a loaded
DLL until its consumer closes; later installation or uninstallation cleans
obsolete unlocked files. Reboot deletion is not scheduled because the same
pair may be reinstalled before reboot. The installer smoke verifies repair,
uninstall and immediate reinstall while an x64 consumer keeps the DLL loaded.
Existing-directory confirmation uses Inno Setup's localized, silent-aware
behavior, so files preserved by uninstall do not block an unattended reinstall.
The Windows smoke waits for the whole installer/uninstaller process tree;
Inno's original uninstaller process can exit before its cleanup phase finishes.
Failures include the Inno log rather than relying only on the launcher exit code.

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
`xdotool` and an X11 session; the automation rejects Wayland sessions.

SideView and its native backend are installed from PyPI through the Python
dependencies. No SideView binary is vendored in this repository.

Install the native dependencies from the Ubuntu 24.04 archive before installing
the Python requirements. The build script supplies the same explicit libobs
package list used by both CI workflows; apt resolves its transitive dependencies:

```bash
mapfile -t libobs_packages < <(bash scripts/build_solin.sh --print-native-packages)
sudo apt-get update
sudo apt-get install --no-install-recommends "${libobs_packages[@]}" \
    binutils build-essential cmake curl dpkg patchelf pkg-config xdotool \
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
automation requires X11 and rejects Wayland sessions. AppImage packaging does
not lower the native glibc requirements or establish support for Ubuntu 22.04.
Native Linux rendering, decoding and packaging qualification are gated by the
Ubuntu 24.04 workflow; Windows tests do not establish those results.

## Integration capabilities

Scenes use the default libobs engine on all supported platforms. Native
devices, capture sources, and output plugins determine which features are
available on a particular machine.

- Virtual-camera output on Windows requires registered Solin DirectShow
  filters; the installer supplies them. Linux requires a configured
  `v4l2loopback` device and the libobs `virtualcam_output` plugin. The macOS
  runtime includes `mac-virtualcam`; validate camera-extension availability,
  permissions, and camera output on the target Mac before distributing a build.
- Zoom meeting audio/video automation requires Windows and `comtypes`. A
  supervised, persistent helper uses native UI Automation with bounded requests.
  `scripts/prepare_zoom_bindings.py` generates `comtypes.gen.UIAutomationClient`
  and its type-library module from Windows' `UIAutomationCore.dll` before Nuitka
  bundles them. Installed executables use the bundled interfaces.
  Background-song linking is available whenever background playback is enabled,
  including manual playback without scheduled days. An application controller
  stops playback on manual meeting start, respecting the configured fade and
  suppressing automatic restart for that occurrence. Scheduled song stops
  prepare Zoom without restarting the fade; successful before/after-meeting
  actions start playback.
- Automatic screen sharing on macOS requires Accessibility
  permission; on Linux it requires `xdotool` and an X11 session. The Linux
  automation rejects Wayland sessions.
- Recording microphone capture uses WASAPI on Windows and PulseAudio or ALSA
  on Linux. System-audio capture uses WASAPI on Windows and PulseAudio on Linux.
  The selector does not support macOS microphone or system-audio capture types.
- Office documents and presentations require LibreOffice and are converted
  through PDF to static images. Animations and embedded-media playback are not
  preserved.

For application setup and operation, see the
[user guide](https://solinav.vercel.app/guide/).

## Build workflows

Platform workflows support manual diagnostics and reusable release calls. Only
the tag-driven release orchestrator can gather all four required targets and
publish them. See [Versioning and releases](releases.md).
