# Solin

Solin is a PySide6 desktop application for audio, video, projection, meetings,
OBS, Zoom, and local media workflows.

## Development

Runtime dependencies are pinned in `requirements.txt`; development tooling is in
`requirements-dev.txt`. The project can be managed with standard `venv`/`pip` or
with `uv` as the environment and installer frontend.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -e .
```

Equivalent `uv` flow:

```powershell
uv venv
uv pip install -r requirements-dev.txt -e .
```

The importable application package lives under `src/solin`. Use the installed
`solin` GUI entry point after editable installation, or `python main.py` as the
thin local launcher during development.

## Local Remote Control

Solin can expose a profile-scoped, installable remote-control web app on one
selected private network interface. The service is disabled by default and uses
the fixed address `https://<selected-ip>:8765/remote/`; it never binds to every
interface and should not be forwarded through a router or exposed to the public
Internet.

To enable it, open Solin settings, select the private LAN interface, save a
username and a password with at least 12 characters, then turn on **Remote
control**. Playlists, linked folders, stored meeting trees, now-playing state,
transport controls, seek, volume and stop remain synchronized with the desktop.
The remote catalog is read-only: filesystem paths, media URLs and editing or
reordering operations are never exposed.

The web interface follows the active Solin interface language. Language changes
are published live to connected browsers, and canonical generated meeting
section titles are resolved from their semantic section codes. Titles edited by
the user are always preserved verbatim.

The first device setup requires trusting Solin's local certificate authority so
the browser can treat the PWA as a secure, installable app. Open the remote URL,
expand **Prepare installation on this device**, download the certificate, and
verify its SHA-256 fingerprint against the value shown in Solin settings before
trusting it. On iPhone or iPad, enable the installed root under **Settings ›
General › About › Certificate Trust Settings**. On Android, install it as a CA
certificate from the device security settings. Reopen the URL afterward and use
the browser's install or Add to Home Screen action.

The generated authority is private to this Solin installation and constrained
to the selected IPv4 address. Its private key never leaves the desktop. Changing
the selected IP rotates the authority and requires trusting the new certificate.
Authentication uses profile-local scrypt password hashes, bounded login rate
limits, server-side sessions, `Secure`/`HttpOnly`/`SameSite=Strict` cookies,
per-session CSRF tokens, exact Host/Origin checks, and an HTTPS WebSocket with
revisioned recovery and idempotent commands.

### Linux native webview

Solin's embedded browser uses WebKitGTK 4.1 with GTK 3 and libsoup 3 on Linux.
Place the sideview artifact at:

```text
src/native_webview_widget/libnative_webview_widget.so
```

After copying it, validate the vendored artifact and ABI with:

```bash
python scripts/validate_native_webview.py --require-linux
```

The native view is embedded through X11/XCB. On a Wayland desktop, start Solin
through XWayland:

```bash
QT_QPA_PLATFORM=xcb python main.py
```

For an Ubuntu development machine, install the native runtime/build dependencies
with `sudo apt install libgtk-3-dev libwebkit2gtk-4.1-dev pkg-config`.
For QtMultimedia hardware acceleration and PipeWire integration in minimal Linux
installations, also install `libva2`, `libva-drm2`, `libva-x11-2`, and
`pipewire-bin`. WebVTT subtitles and some QtMultimedia video sinks require
`gstreamer1.0-plugins-bad`. When running from source, automatic Zoom sharing on
Linux/X11 additionally requires the system executable `xdotool`
(`sudo apt install xdotool`); it is not a Python package and cannot be installed
with `pip`.

### Linux standalone build

The Linux build uses Nuitka and must run inside Linux. From WSL 2, use the
existing Linux Python 3.13 environment:

```bash
cd /path/to/Solin
SOLIN_PYTHON="$HOME/.venvs/solin/bin/python" bash scripts/build_solin.sh
```

The build requires `build-essential` and `patchelf`, plus the WebKitGTK runtime
described above. On WSL, compilation intermediates are automatically kept in
the faster Linux filesystem and the finished distribution is synchronized back
to `build/linux/main.dist`. Start it through the generated `run-solin` launcher,
which selects Qt's XCB backend automatically:

```bash
build/linux/main.dist/run-solin
```

WebKitGTK's accelerated DMABuf renderer remains enabled by default. If a virtual
GPU or incompatible graphics driver produces a blank browser, use the explicit
compatibility workaround only for that machine:

```bash
WEBKIT_DISABLE_DMABUF_RENDERER=1 build/linux/main.dist/run-solin
```

### Linux AppImage

The AppImage packager reuses the standalone build and the same script in local
WSL and GitHub Actions. From Windows, run the complete build with:

```bat
scripts\build_solin_appimage.bat
```

To repackage an already current `build/linux/main.dist` without recompiling
Nuitka, use:

```bat
scripts\build_solin_appimage.bat --skip-standalone
```

The equivalent command inside Ubuntu/WSL is:

```bash
cd /path/to/Solin
SOLIN_PYTHON="$HOME/.venvs/solin/bin/python" bash scripts/package_solin_appimage.sh
```

The output is `dist/Solin-<version>-x86_64.AppImage` plus its SHA-256 file.
`appimagetool` 1.9.1 and the Type 2 runtime are downloaded to the WSL cache and
verified against pinned SHA-256 hashes. On WSL, the AppDir and compression work
remain in the Linux filesystem for better I/O performance.

The AppImage bundles Solin, Python, PySide6 and the required Qt runtime. It
intentionally uses the distribution's security-maintained WebKitGTK 4.1, GTK 3
and libsoup 3. Qt's auxiliary XCB libraries are bundled, so users do not need to
install the usual `libxcb-cursor0`, `libxcb-icccm4`, `libxcb-image0`,
`libxcb-keysyms1`, `libxcb-render-util0`, `libxkbcommon-x11-0`, and related
helper packages separately. On Ubuntu, install the required WebKit runtime with:

```bash
# Ubuntu 24.04
sudo apt install libwebkit2gtk-4.1-0 libgtk-3-0t64 libsoup-3.0-0

# Ubuntu 22.04
sudo apt install libwebkit2gtk-4.1-0 libgtk-3-0 libsoup-3.0-0
```

Automatic Zoom sharing also works without a separate `xdotool` installation:
the standalone distribution and AppImage carry a private copy with its required
non-core X11 libraries and redistribution notices. This feature remains limited
to X11/Xorg (including applications running through XWayland); `xdotool` cannot
control native Wayland windows.

Because Zoom does not expose its screen-share shortcut as global on Linux,
Solin resolves visible windows from exact Zoom process IDs, activates and
verifies the selected X11 window before dispatching the shortcut, then restores
the previously focused window. The largest Zoom window is preferred so a small
floating meeting toolbar is not selected while the main meeting window exists.

After downloading the AppImage, make it executable and run it:

```bash
chmod +x Solin-*-x86_64.AppImage
./Solin-*-x86_64.AppImage
```

The manual `Build Solin Linux` GitHub Actions workflow runs the same script on
Ubuntu 22.04 for an older compatible glibc baseline, validates the
desktop/AppStream metadata, performs an AppImage startup smoke test under Xvfb,
and uploads the versioned AppImage with its SHA-256 checksum.

## Quality Checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe scripts\check_locales.py
.\.venv\Scripts\python.exe scripts\validate_native_webview.py
```

`pyproject.toml` centralizes pytest, Ruff, and Pyright configuration. Ruff
enforces Pyflakes, bugbear checks, silent `try`/`except`/`pass` prevention, and
`BLE001` globally. Broad catches are allowed only at intentional runtime
boundaries and must use a locally justified `# noqa: BLE001 - ...`; the
exception-policy test independently enforces that contract.
Pyright currently covers core foundation, timer, JW, meetings, and playlist
domains; controllers/widgets are the next incremental typing frontier because
they rely heavily on dynamic Qt attributes and mixins.

Tests are organized by suite under `tests/unit`, `tests/integration`,
`tests/contract`, and `tests/e2e`. Run a focused suite with `pytest -m unit`,
`pytest -m integration`, or `pytest -m contract`.

## Delivery Layout

Versioned delivery automation lives outside generated output directories:
`scripts/` contains repository operations such as QML cache compilation,
translation validation, local Windows builds, and release diffs; `packaging/`
contains installer recipes; `resources/` contains runtime assets and
translations. `build/` and `dist/` are ignored output-only directories.
