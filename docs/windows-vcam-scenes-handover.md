# Solin on Windows: Virtual Camera + Scenes — Handover

> **Repo:** `/home/jonata/Projetos/Solin` (branch `scenes_and_virtual_camera`). Sibling native package: `pylibobs` (checked out at `/home/jonata/Projetos/pylibobs` on the reference Linux box). All paths below are repo-relative unless absolute.
> **Reading discipline:** items tagged **VERIFY:** were "recalled" by the research, not confirmed against a running Windows machine — confirm them on the metal before you rely on them.

---

## 1. TL;DR / mission

Make Solin's **Virtual Camera** and **Scenes** features work on Windows 11. Today they work only on Linux. This is a **real three-layer port, not a config flip**:

- **Layer A** — get the libobs media engine (`SOLIN_MEDIA_ENGINE=obs`) actually running on Windows: it needs a bundled Windows libobs (`obs.dll` + `libobs-d3d11.dll` + plugins + `data/` + helper EXEs), none of which ship today.
- **Layer B** — replace the Linux vcam sink (v4l2loopback kernel module) with the Windows equivalent: the `win-dshow` plugin's `virtualcam_output` feeding a **DirectShow filter DLL that must be COM-registered (`regsvr32`, admin/UAC as OBS ships it)** — the analog of `modprobe v4l2loopback`.
- **Layer C** — the Scenes UI + rule-engine "brain": **already platform-agnostic** and ports for free; it only needs A+B underneath it to have live effect.

The honest scope: Layer C is basically free, Layer A is packaging/toolchain work, and Layer B contains the one genuinely hard/expensive item — a branded "Solin Virtual Camera" device requires building a **custom C++/COM camera filter**. De-risk by getting the stock "OBS Virtual Camera" pipeline working end-to-end first, then do the branded filter.

**GPLv2 is not a Phase-2-only concern.** Solin *already* bundles `obs.dll` (libobs) and loads it in-process via pylibobs/ctypes, and on Windows will additionally bundle/load `win-dshow.dll`; libobs and every OBS plugin are GPLv2. Distributing Solin combined with a dynamically-loaded GPLv2 libobs already raises the combined-work question **today, on Linux too** — see §5/§9. The custom filter is an *additional* GPL item, not the first one.

**Modern-consumer caveat (read before investing in a custom DirectShow filter):** a DirectShow-only virtual camera is *not* reliably enumerable by Media-Foundation consumers — Chrome, Edge, the new (WebView2/Edge-based) Teams, and the Windows Camera app capture through Media Foundation, not DirectShow. For Phase 2, evaluate the Windows 11 Media Foundation virtual-camera API (`MFCreateVirtualCamera`) as an alternative to a DShow filter (see §5).

---

## 2. Why it doesn't work on Windows today

### 2a. Engine selection is one env var, defaulting to the Qt engine on every OS

Engine choice is a single switch in `MediaComposition.create_playback` (`src/solin/bootstrap/media.py:66-83`):

```python
engine = os.environ.get("SOLIN_MEDIA_ENGINE", "").strip().lower()
if engine == "obs":
    from solin.core.media.obs_playback import ObsMediaController
    ...
    return ObsMediaController(...)
return MediaController(...)   # Qt engine (default, every OS)
```

Mirrored by `obs_media_engine_active()` (`src/solin/projection/window.py:72-74`) and by the Qt/GL coexistence tweaks in `src/solin/bootstrap/application.py:39-44`. **There is no `if sys.platform == "win32"` forcing Qt** — Windows falls back simply because obs mode is opt-in and off by default.

### 2b. Even if you set `SOLIN_MEDIA_ENGINE=obs` on Windows, the runtime can't start

`ObsRuntime.ensure_started()` (`src/solin/core/media/obs_runtime.py:264-322`) guards on `libobs_available()`, and `context.startup()` ultimately calls pylibobs `find_libobs()`. pylibobs's bundle ships **only Linux binaries**:

```
pylibobs/_libs/
  linux/x86_64/{libobs.so,.0,.30, libobs-opengl.so(.30), data/, obs-plugins/}
  # NO windows/  NO macos/
```

With no `_libs/windows/x86_64/obs.dll`, `find_libobs()` falls to a system OBS install else raises `ImportError`, which `ensure_started` converts to `ObsRuntimeError`. So obs mode never comes up on Windows.

### 2c. The Linux vcam sink is v4l2loopback — no Windows analog

On Linux the `virtualcam_output` kind (`obs_virtual_camera.py:31`, `_VCAM_OUTPUT_KIND = "virtualcam_output"`) is registered by the **`linux-v4l2.so`** plugin and writes into a `v4l2loopback` kernel device. Confirmed by string-scan: `virtualcam_output` appears in **only** `linux-v4l2.so` among bundled plugins. The whole provisioning module `src/solin/core/media/vcam_provision.py` is a v4l2loopback + `pkexec modprobe` state machine with zero Windows path (it returns `UNSUPPORTED` off Linux at `:112`). On Windows the same output id is registered by **`win-dshow.dll`** and targets a **DirectShow COM filter** — a completely different sink that must be registered with the OS.

---

## 3. Architecture recap — what exists and what's reusable

Solin's vcam stack is deliberately layered so the platform-specific surface is small. Three tiers:

### Platform-AGNOSTIC — ports to Windows unchanged (zero edits)

| Component | Files | Notes |
|---|---|---|
| **Rule model** (enums, `VcamComposition`, rule tables, `VcamSceneConfig`) | `src/solin/core/media/vcam_model.py` | Pure stdlib (`dataclasses`, `enum`). No `os`, no `sys.platform`, no libobs, no Qt. "Leaf module, no libobs" by design. Ports verbatim. |
| **Director** (follow-the-projector polling engine) | `src/solin/core/media/vcam_director.py` | Only third-party import is `PySide6.QtCore.QTimer` (cross-platform). Touches hardware only through two injected methods `apply_composition()` / `set_pip_placement()` and one polled attribute `current_key`. Ports unchanged. |
| **Settings persistence** | `src/solin/core/media/vcam_settings.py` | `VcamSettingsStore` reads/writes one JSON blob under `SettingsKey.VCAM_SCENE_CONFIG`. Platform-neutral. |
| **Scenes UI** (nav page + quick-toolbar override) | `src/solin/widgets/scenes_widget.py`, nav registration in `src/solin/controllers/main_window_ui_controller.py`, `src/solin/widgets/quick_access_toolbar.py`, `src/solin/ui/qml/quick_toolbar.py`, `src/solin/qml/QuickAccessToolbar.qml` | Pure Qt over `VcamSceneConfig`. Nav item is registered **unconditionally** at page index 10; no platform/engine gate on the nav button. Edits always persist. |

### The vcam RENDERER — mostly platform-neutral, but its sink is the port surface

`src/solin/core/media/obs_virtual_camera.py` composites on its **own independent `obs_view`** (a second mix, NOT the projector's global channel 0), so the vcam can differ from what's projected. Per-view channel stack (back→front):

| Channel | Constant (`:34-41`) | Content | Built by |
|---|---|---|---|
| 0 floor | `_CH_IDLE = 0` | Branded idle logo (`image_source` in a scene) | `_ensure_view()` / `_build_idle_scene()` (`:271-284`, `:373-384`) |
| 1 middle | `_CH_MIRROR = 1` | Projector program's transition source, or `None` | `_set_mirror(visible)` (`:287-311`) |
| 2 top | `_CH_CAMERA = 2` | Meeting camera (FULL/PiP) in `vcam-camera-scene`, or `None` | `_set_camera(layout)` (`:313-355`) |

The **create/bind/start path is platform-neutral and should be reusable as-is** once `virtualcam_output` is confirmed to drive the Windows vcam (`start()`, `:110-146`): create `Output.create("virtualcam_output", "Solin Virtual Camera", {})`, bind via `video = self._view.add()`, `self._output.set_media(video, self._audio())`, `self._output.start()`. Lifecycle: `stop()` (`:148-155`), `shutdown()` full teardown (`:213-244`), driven from `obs_runtime.shutdown()` (`:340-345`).

### Linux-BOUND — must be replaced/abstracted for Windows

- `platform_prerequisite_ok()` (`obs_virtual_camera.py:69-79`) — Linux returns `is_loaded()`; **Windows falls through to unconditional `True`** (a stub, no real check).
- `prerequisite_hint()` (`:81-90`) — Linux `modprobe` text; `""` elsewhere.
- `_log_started()` (`:248-260`) — uses Linux sysfs probe `find_loopback_device()`; returns `None` on Windows, so the operator gets only the generic line.
- The entire provisioning module `src/solin/core/media/vcam_provision.py` — v4l2loopback + polkit.
- The bundled native libs (Layer A) — Linux-only in pylibobs.

---

## 4. The three layers of the port

### Layer A — Bring the libobs media engine to Windows (prerequisite for everything)

Nothing works until `SOLIN_MEDIA_ENGINE=obs` on Windows gets past `ObsRuntime.ensure_started()`. The good news: **pylibobs's Windows *code* is already complete and non-stubbed**; only the *binaries* and a couple of fetch bugs are missing.

**What pylibobs already does correctly on Windows** (no changes needed):
- `_lib.py:19-29` maps `Windows → obs.dll`, subdir `windows`, flat layout `_libs/windows/<arch>/obs.dll`.
- `prepare_dll_search_path()` (`_lib.py:180-206`) calls `os.add_dll_directory(lib_dir)` + prepends PATH so co-located dependency DLLs resolve. Runs before `dlopen` (`_ffi.py:30-40`). Correct for Py3.8+.
- Helper-EXE staging: `_stage_helpers_next_to_host()` (`_lib.py:124-177`) copies `obs-ffmpeg-mux.exe`, `obs-amf-test.exe`, `obs-nvenc-test.exe`, `obs-qsv-test.exe` next to `python.exe` (libobs spawns them via `GetModuleFileNameW(NULL)`, which returns the host EXE dir, not `obs.dll`'s dir).
- Default graphics module `Windows → libobs-d3d11` (`context.py:86-90`).
- Data + module resolution for the flat bundle (`_lib.py:209-338`); explicit per-module load in `context.load_modules()` (`context.py:309-341`) that skips `obs_load_all_modules` so a co-installed OBS Qt frontend can't abort the headless process.

**What Layer A actually needs — the native artifact checklist** (`_libs/windows/x86_64/`):

| # | Artifact | Source | Status |
|---|---|---|---|
| A0 | `obs.dll` + dependency DLLs (avcodec/avformat/avutil/swscale/swresample, `libx264-*`, zlib, `w32-pthreads`, libcurl, mbedtls…) | `fetch_libs.py` extracts OBS Windows zip `bin/64bit/` → root | fetch handles it |
| A1 | `libobs-d3d11.dll` (default renderer) | `bin/64bit/` | present, not trimmed |
| A2 | **`d3dcompiler_47.dll`** (D3D11 runtime HLSL `.effect` compilation) | `bin/64bit/` | ❌ **trimmed** by `scripts/fetch_libs.py:467` glob `"**/d3dcompiler_*"` — **must un-trim**. VERIFY: without it `obs_reset_video()` likely fails. Single most likely reason a fresh Windows bundle won't start. |
| A3 | `_libs/windows/x86_64/data/` (libobs shader effects + per-plugin data) | fetch `data/` → `data/` | fetch handles it |
| A4 | Helper EXEs (`obs-ffmpeg-mux.exe`, encoder probes) | `bin/64bit/` → root, staged by pylibobs | handled |
| A5 | Plugins: `obs-ffmpeg.dll` (`ffmpeg_source`), `image-source.dll` (`image_source`+`color_source_v3`), `obs-transitions.dll` (`fade_transition` — the program crossfade; `obs_program.py:156` throws without it), **`win-dshow.dll`** (`dshow_input` camera + `virtualcam_output`) | fetch `obs-plugins/64bit/` → `obs-plugins/` | fetched, **not verified** (CI verify asserts nothing — see A5 note below) |
| A6 | **Custom `solin-framesrc.dll`** — the `solin_frame_source` async frame-injection source | — | ❌ **does not exist for Windows**; no `.c`/CMake/build step in pylibobs. See below. |

**About `solin_frame_source`:** it is NOT required for the vcam or for media playback. It backs only the **live browser-tab / NDI frame-injection path** (`obs_frame_source.py:61-128`, consumed by `src/solin/projection/program_driver.py:255-311` — the two `create_frame_source` consumers are `show_browser_frame` at ~272 and `show_ndi_frame` at ~302). If absent those two methods return `False` and callers fall back to the Qt per-frame path (`window.py:1087-1130`). So **you can complete the vcam+Scenes port without it** — treat it as a later parity item.
- Its C source lives in the *Solin* repo: `tools/obs-frame-source/solin-framesrc.c` (63 lines, `<obs-module.h>` only, portable) with a shell build `tools/obs-frame-source/build.sh`. A Windows build = compile to `solin-framesrc.dll` against a Windows libobs import lib (`obs.lib`) of matching ABI, drop into `_libs/windows/x86_64/obs-plugins/`. **The ABI guard is automatic:** `solin-framesrc.c:21` uses `OBS_DECLARE_MODULE()`, which auto-generates `obs_module_ver()` returning the header's compiled-in `LIBOBS_API_VER`; libobs itself performs the version comparison at module load. There is no enforcement code to add — the only requirement is to compile against the OBS headers + `obs.lib` of the exact bundled `obs.dll` tag.

**ABI lock-step (A-wide invariant):** every native module (bundled OBS plugins + `solin-framesrc`) must be built against the **exact OBS tag matching the bundled `obs.dll`**; libobs enforces this via the `obs_module_ver` vs `obs_get_version` check at module load. VERIFY: docs drift — `obs_runtime.py:13` says "libobs 32.1.2", bundled Linux SONAME is `libobs.so.30`; the runtime version check is the real guard. **The Windows bundle is currently NOT pinned:** `fetch_libs.py`'s `--version` defaults to `None` (`scripts/fetch_libs.py:597`), and `get_release(None)` fetches the GitHub `/latest` OBS release (`fetch_libs.py:111-112`) — `32.2.1` appears only as example text in help/comments (lines 11, 598), there is **no `OBS_TAG` variable anywhere** in the repo. The CI job (`.github/workflows/release.yml:56-58`) invokes fetch with only `--platform`/`--arch`, so the Windows bundle tracks whatever OBS "latest" is at build time — a moving target. **Recommendation:** pin `--version <tag>` in the CI before-build / `release.yml` so the shipped `obs.dll` ABI is deterministic, then build every custom module (`solin-framesrc`, any DShow filter) against that same pinned tag's headers/import lib. Otherwise a custom module built against a stale SDK can hit a load-time `obs_module_ver` mismatch against a newer "latest" `obs.dll`.

**How to flip Solin onto the obs engine on Windows and verify:**
1. Produce a Windows pylibobs wheel with `_libs/windows/x86_64/` populated (run `python scripts/fetch_libs.py --platform windows`, after fixing the `d3dcompiler_47` trim; add `--version <tag>` to pin). CI already wires this: `pyproject.toml:91-92` (`before-build`), `.github/workflows/release.yml` on `windows-latest`.
2. Install that wheel into the Windows Solin env.
3. Set `SOLIN_MEDIA_ENGINE=obs` in the environment.
4. Confirm `obs_media_engine_active()` returns `True` and `ObsRuntime.ensure_started()` completes without `ObsRuntimeError`. Then confirm `"virtualcam_output" in rt.ob.enum_output_types()`.

**System prerequisites (VERIFY on target):** MS Visual C++ 2015–2022 x64 redistributable (`VCRUNTIME140.dll` / `_1.dll` / `MSVCP140.dll`); UCRT (present on Win10/11 by default); a D3D11-capable GPU/driver (else force `libobs-opengl.dll`, which needs `libEGL.dll`/`libGLESv2.dll` — also currently trimmed at `fetch_libs.py:466`). No Qt needed for headless libobs.

**Not ported (Linux/X11-only, leave off on Windows):** `_configure_nix_platform()` (`obs_runtime.py:364-387`) and the `QT_XCB_GL_INTEGRATION=xcb_egl` / `QT_QUICK_BACKEND=software` tweaks (`application.py:41-44`). VERIFY: Windows binds `obs_display_t` to the HWND via `winId()` (`window.py:109-138`) — that D3D11/Win32 display path needs its own validation but is integration work above Layer A, not a prerequisite file.

---

### Layer B — The Windows virtual-camera sink (replace v4l2loopback with DirectShow)

**The Windows topology** (verified from OBS `plugins/win-dshow/` source):
- `win-dshow.dll` registers `struct obs_output_info virtualcam_info` with id **`"virtualcam_output"`** (`virtualcam.c`, compiled when `VIRTUALCAM_AVAILABLE`). Same id Solin already requests.
- On start, the output creates a **shared-memory ring buffer** (`video_queue_create`, NV12) — a page-file-backed `CreateFileMapping` (INVALID_HANDLE_VALUE), **not a named pipe** — and writes frames via `video_queue_write` (`shared-memory-queue.c/.h`).
- The **reader** is a DirectShow **push-source COM filter** (`VCamFilter` in `virtualcam-filter.cpp`) loaded **in-process by the consuming app** — but only by apps that capture via **DirectShow**. It reads via `video_queue_read` and delivers on its output pin.

> **⚠️ Media Foundation consumers (major):** the "register the filter → every app sees it in-process" model is **not universal on modern Windows**. Chrome, Edge, the new WebView2/Edge-based Teams, and the Windows Camera app capture through **Media Foundation (MF)**, not DirectShow. MF has **no native discovery for legacy DirectShow virtual cameras**; a DShow-only OBS-style camera is visible to MF consumers only through an OS bridge that is documented as flaky (widely reported "OBS Virtual Camera not showing in Chrome/Edge/Teams", often needing "disable hardware acceleration" or a full browser restart). Do not assume a custom DShow filter will Just Work in MF-based hosts — see the Phase-2 `MFCreateVirtualCamera` alternative in §5.

- **Runtime "Start Virtual Camera" touches NO registry and needs NO elevation** — it only creates the OBS-side output + queue. If the filter was never registered, Start still runs but no app sees the camera.

**The filter DLLs & registration** (the v4l2loopback analog):
- `obs-virtualcam-module32.dll` and `obs-virtualcam-module64.dll`, shipped under `data\obs-plugins\win-dshow\`. Standard COM in-proc servers (`DllRegisterServer`/`DllUnregisterServer`, `virtualcam-module.cpp`).
- OBS registers them via **elevated `regsvr32`** — its `DllRegisterServer` writes `HKLM\SOFTWARE\Classes\CLSID\{GUID}\InprocServer32` (via `HKEY_CLASSES_ROOT`) + `IFilterMapper2::RegisterFilter` into `CLSID_VideoInputDeviceCategory` with `MERIT_DO_NOT_USE`, and its install `.bat` gates on `net session` ⇒ **admin/UAC required as OBS ships it**. Note this is a *choice of that implementation*, not a DShow requirement — a custom filter can register per-user under `HKCU\Software\Classes` and skip elevation (see §5). Commands (from `virtualcam-install.bat.in`):
  - `regsvr32.exe /i /s "…\obs-virtualcam-module64.dll"`
  - `regsvr32.exe /i /s "…\obs-virtualcam-module32.dll"`
  - Presence check: `reg query HKLM\SOFTWARE\Classes\CLSID\{GUID}` (64) / `…\WOW6432Node\CLSID\{GUID}` (32).
- Friendly name is the compile-time constant `"OBS Virtual Camera"` (the `Description` in `RegisterFilter`); CLSID is `CLSID_OBS_VirtualVideo`, injected at build via `-DVIRTUALCAM_GUID`. VERIFY: OBS's shipped value is recalled as `{A3FCE0F5-3493-419F-958A-ABA1250EC20B}` — confirm via `reg query` on a machine if needed; you should mint your own regardless (see §5).
- **32- vs 64-bit (do not skip):** the filter is loaded in-process, so a 32-bit host can only load the 32-bit DLL. The mainstream 2026 hosts (Zoom, new Teams, Chrome, Edge, Firefox, OBS) are all 64-bit on 64-bit Windows, but some **legacy / embedded / 32-bit Electron** hosts persist — OBS ships both bitnesses for exactly that reason. **Build and register BOTH bitnesses to be safe**, or those 32-bit apps won't see the camera at all.

**pylibobs status for B:** `win-dshow.dll` + its `data/obs-plugins/win-dshow/` (incl. the module DLLs) are pulled by `fetch_libs.py`'s `obs-plugins/64bit/` + `data/` extraction — so the output *id* becomes available. But **bundling files does NOT register the COM filter**; nothing in pylibobs does the `regsvr32`. That registration is the biggest functional gap for the Windows vcam.

#### B — interface spec: a Windows sibling of `vcam_provision.py`

Callers couple to the module at exactly two sites, so keep `vcam_provision` as a **platform-dispatching facade** (Linux branch as today, Windows branch delegating to a `_win` implementation) — zero caller edits:
- `src/solin/controllers/live_integration_controller.py:237` — `from ..core.media import vcam_provision as prov` (uses `prov.detect()`, `prov.VcamProvisionState.*`, `prov.install_hint()`, `prov.provision()`).
- `src/solin/core/media/obs_virtual_camera.py:27` — `from .vcam_provision import DESIRED_LABEL, find_loopback_device, is_loaded`.

Required symbols and their Windows contract (reuse the **same** `VcamProvisionState` enum — callers compare against `MODULE_MISSING`/`NOT_LOADED`/`WRONG_LABEL` at `live_integration_controller.py:241,249,250`):

| Symbol | MODULE_MISSING | NOT_LOADED | WRONG_LABEL | READY |
|---|---|---|---|---|
| `detect()` → state | filter DLL absent from install | DLL present, **not registered** | registered under a **different** friendly name | registered under `DESIRED_LABEL`, enumerable |
| `module_installed()` | `False` | `True` | `True` | `True` |
| `is_loaded()` (registered & available?) | `False` | `False` | `True` | `True` |
| `find_loopback_device()` → `(id, name)\|None` | `None` | `None` | `(moniker, other_name)` | `(moniker, "Solin Virtual Camera")` |
| controller action | warn `install_hint()`, stop | provision → start | provision → start | start now |
| `provision()` goal | n/a (no auto-install) | register under label | re-register under label | idempotent no-op → `True` |

- `DESIRED_LABEL` MUST stay `"Solin Virtual Camera"` (`vcam_provision.py:33`). It is compared in `detect()` (`:124-127`) and is the fallback in `_log_started()` (`obs_virtual_camera.py:259`).
- `find_loopback_device()[1]` MUST be the registered friendly name (drives `READY` vs `WRONG_LABEL`); element 0 is any stable identifier (device moniker/path). `_log_started()` unpacks `path, name = device` (`:255-259`) and tolerates empty name.
- `detect()` MUST **never raise** and be GUI-thread-safe (called at `live_integration_controller.py:239`). On Windows it must return one of the four states above, never `UNSUPPORTED`.
- `provision(runner=subprocess.run, timeout=120) -> bool` MUST: perform the **`regsvr32` registration under `DESIRED_LABEL`** (elevated for a Phase-1 HKLM registration; potentially non-elevated per-user for a Phase-2 HKCU filter — see §5), trigger the **UAC prompt if elevation is required** (analog of polkit), be safe to call from the `QThreadPool` worker and **never touch Qt**, return `True` on success, return `False` (logged, **non-fatal**) on elevation-unavailable / **user cancels UAC** / timeout / failure, be **idempotent** when already `READY`, and honor the `runner` seam for tests. VERIFY: the controller starts the camera regardless of `ok` (`_on_vcam_provisioned`, `live_integration_controller.py:272-280`) — preserve that degrade-gracefully behavior.
- `provision_argv()` SHOULD exist (tests assert its shape, `tests/unit/test_vcam_provision.py:60-70`): on Windows return the argv (e.g. a UAC `ShellExecute "runas"` of `regsvr32 /i /s "<abs>\obs-virtualcam-module64.dll"`, and a second for the 32-bit DLL). Use `/i /s` to match OBS's shipped `virtualcam-install.bat.in` exactly (`/i` invokes `DllInstall`; do not rely on `/s` alone). Keep the safety invariants: absolute interpreter/tool path, no unsanitized interpolation, keep `_SAFE_LABEL` (`vcam_provision.py:44`) guarding anything ever written.
- `install_hint()` MUST return a non-empty human string for `MODULE_MISSING` (shown verbatim at `live_integration_controller.py:243`), e.g. "reinstall Solin / the virtual-camera component is missing".
- `pkexec_available()` may stay Linux-only (callers never call it directly); the Windows `provision()` does its own elevation-availability gate.

#### B — edits to `obs_virtual_camera.py`

- `platform_prerequisite_ok()` (`:69-79`): add a Windows branch that **defers to `is_loaded()`** instead of returning `True` unconditionally. Keep Linux `is_loaded()`; keep macOS `True` for now.
- `prerequisite_hint()` (`:81-90`): add Windows text (e.g. "the Solin Virtual Camera filter isn't registered — run the registration step / reinstall").
- `_log_started()` (`:248-260`): with the facade, `find_loopback_device()` now returns a real Windows tuple, so the existing unpack works — no structural change needed beyond confirming the Windows `find_loopback_device()` returns `(moniker, name)`.
- Output binding path (`start()`, `:110-146`) is expected to work unchanged once `virtualcam_output` is registered on Windows — **VERIFY** on the metal that `Output.create("virtualcam_output", …)` + `set_media(view.add(), audio())` + `start()` actually pushes frames into the DShow queue. The vcam is video-only on Windows too; don't assume audio reaches the meeting app via this output.

---

### Layer C — Scenes UI + wiring (mostly "just works")

The UI + brain are platform-agnostic (see §3). Once A+B land, **no UI edits are required**. Confirm these gates:

| Gate | Definition | Effect |
|---|---|---|
| `obs_media_engine_active()` | env `SOLIN_MEDIA_ENGINE=obs` (`window.py:72-74`) | Gates `on_vcam_scene_override` (`live_integration_controller.py:207`), `start_virtual_camera` (`:233`), `_sync_vcam_camera` (`:311`). |
| `virtual_camera().active` | set in `start()` (`obs_virtual_camera.py:141`), cleared in `stop()` (`:155`) | Gates the **live** apply of Scenes edits in `_push_vcam_scene_config` (`main_window_ui_controller.py:632`) — persistence still happens regardless. |
| `platform_prerequisite_ok()` | see §4 Layer B edit | Gates `is_available()` (`:94`) and `start()` (`:115`). |
| `set_scene_override_available(True)` | reached only on successful start (`live_integration_controller.py:291-293`) | Reveals the quick-toolbar clapperboard override button. |

**Does the nav item auto-activate?** There is nothing to "light up" — the **Scenes nav item is always on, on every platform, today** (registered unconditionally at page index 10; `main_window_ui_controller.py:232,248,609`). Editing and persisting Scenes config already works on Windows *now* (writes via `VcamSettingsStore.save_scene_config`). What auto-activates once A+B land is (a) the **quick-toolbar override button** (via `set_scene_override_available(True)`) and (b) the **live application** of edits (once `virtual_camera().active` is `True`, edits push through `vcam_director().set_config(...)`).

**One backend caveat, not a UI change:** `start_virtual_camera` calls `prov.detect()` unconditionally (`live_integration_controller.py:239`). Today Windows `detect()` returns `UNSUPPORTED` → falls through to `_start_virtual_camera_now`. Once the Windows facade exists (§4 Layer B), `detect()` returns the real state and routes through provisioning correctly. **UX nuance to consider:** the always-on nav page shows no "virtual camera unavailable" hint on a platform where the sink can't run — candidate polish, not a bug.

---

## 5. The "Solin Virtual Camera" naming decision (the crux)

The Linux product decision is a **branded device named `"Solin Virtual Camera"`** (that's what `card_label=` gives on Linux and what `DESIRED_LABEL` encodes). On Windows the device name is **not mutable at runtime** — it's a compile-time `Description` string plus a fixed CLSID baked into the filter's registry entry. The options:

**Option (a) — ship/register OBS's stock DirectShow filter.**
- Register OBS's `obs-virtualcam-module{32,64}.dll` via `regsvr32 /i /s`. Fast; no C++ work.
- **Downside:** the device enumerates as **"OBS Virtual Camera"**, not "Solin Virtual Camera" — wrong brand — and it collides with / depends on OBS's own registration and CLSID (`CLSID_OBS_VirtualVideo`). If real OBS is also installed, only one owner of that CLSID.
- **Requires admin/UAC as shipped** (OBS's `DllRegisterServer` writes to HKLM via HKCR; its install `.bat` gates on `net session`).
- Good for **de-risking the whole pipeline end-to-end** before investing in C++.

**Option (b) — build a CUSTOM virtual-camera filter with a distinct CLSID + friendly name "Solin Virtual Camera".** Two sub-architectures worth weighing:

- **(b1) Custom DirectShow filter, derived from OBS `win-dshow`.**
  - Mint a **new unique GUID** (`uuidgen`) — never reuse OBS's CLSID. Set `RegisterFilter` `Description` (and pin/filter name strings) to `"Solin Virtual Camera"`. Rebuild **both** 32- and 64-bit DLLs, register each from your own installer under your own CLSID. Coexists with OBS's device without touching it.
  - Keep the writer's shared-memory mapping name (`video_queue_*`) in sync with your filter's reader, or your filter only ever shows its placeholder.
  - **Can register per-user under `HKCU\Software\Classes` to skip UAC entirely** — non-elevated HKCR writes redirect there, and `ICreateDevEnum` enumerates the merged HKLM+HKCU view for that user's apps. Flag this as an option to **validate against the target consumer apps** rather than assuming elevation is required.
  - **GPLv2 obligation:** `plugins/win-dshow` (incl. `virtualcam-module` + `shared-memory-queue`) is GPLv2. A filter derived from it is a derivative work — distributing the DLL means licensing that filter GPLv2 and offering complete corresponding source. **Safest posture:** keep the DShow filter a **standalone GPLv2 component** (own repo/source offer, own DLL/process boundary) so copyleft scope is contained to the filter. But see §9 — libobs bundling already puts Solin's whole distribution in GPLv2 scope.
  - **Modern-consumer risk:** a DShow-only camera is not reliably enumerable by Media-Foundation hosts (Chrome/Edge/new Teams/Windows Camera) — see the Layer B warning. This can undercut the investment.

- **(b2) Windows 11 Media Foundation virtual camera (`MFCreateVirtualCamera` / `MFVirtualCamera`).**
  - **MF-native**, so it enumerates cleanly in the modern MF consumers that a DShow filter struggles with, and Windows bridges it *down* to DirectShow for legacy hosts — better all-around modern compatibility.
  - **Can register per-user** and is a **Microsoft-supported** API rather than a derivative of OBS's GPLv2 code (see Microsoft's `VCamSample`), which **sidesteps the DShow-filter GPL derivation** (the libobs-bundling GPL question in §9 still stands separately).
  - Requires Windows 11 and its own C++/COM work + code-signing. Recommended architecture to evaluate for the branded Phase 2.

- **Clean-room DShow alternative:** own `CSource`/`CSourceStream` from the Windows SDK baseclasses + your own IPC sidesteps the win-dshow GPL derivation but is more work than (b1). **VERIFY licensing with counsel** for any of these.

**Recommendation — phased:**
1. **Phase 1 (Option a):** get stock "OBS Virtual Camera" appearing in Zoom/Teams/Chrome driven by Solin's `virtualcam_output`. This proves Layers A+B+C wire together. The provisioning facade's `DESIRED_LABEL` comparison will report `WRONG_LABEL` (name ≠ "Solin Virtual Camera") — acceptable for this phase; the controller still starts. Expect MF-consumer flakiness even here (it's OBS's DShow filter).
2. **Phase 2 (Option b):** build the branded custom camera, register it, and `find_loopback_device()` now reports `"Solin Virtual Camera"` → `READY`. **Evaluate (b2) `MFCreateVirtualCamera` before committing to a custom DShow filter (b1)** — MF is the Microsoft-supported modern path, avoids the DShow-filter GPL derivation, and can register per-user without UAC. Either way this is the C++/COM + code-signing chunk of work.

---

## 6. File-by-file work list

| File / artifact | Change | Contract to preserve |
|---|---|---|
| `pylibobs/scripts/fetch_libs.py:467` | Un-trim `"**/d3dcompiler_*"` (scope it so `d3dcompiler_47.dll` survives); reconsider `libEGL*`/`libGLESv2*` at `:466` if OpenGL fallback wanted | Windows `fetch --platform windows` must land `d3dcompiler_47.dll` next to `obs.dll` |
| `pylibobs/scripts/fetch_libs.py` `--version` / CI invocation | Pin the OBS release: pass `--version <tag>` (default is `None` → `get_release(None)` = GitHub `latest`, a moving target). There is no `OBS_TAG` var to set. | Deterministic `obs.dll` ABI for every build; custom modules built against the same tag |
| `pylibobs/_libs/windows/x86_64/` (built by CI, not committed) | Populate: `obs.dll` + deps, `libobs-d3d11.dll`, `data/`, helper EXEs, `obs-plugins/*.dll` (incl. `win-dshow.dll`), `data/obs-plugins/win-dshow/obs-virtualcam-module{32,64}.dll` | Flat layout `_lib.py` expects; ABI-matched to bundled `obs.dll` |
| `pylibobs/.github/workflows/release.yml` verify step (`:61-73`) | **Add real assertions.** Today it only checks `base.exists()` and prints a file count / total MB — it asserts nothing, so even an empty/incomplete bundle passes. Assert `obs.dll`, `libobs-d3d11.dll`, `d3dcompiler_47.dll`, `obs-plugins/win-dshow.dll`, helper EXEs (and any `solin-framesrc.dll`) exist | Prevent shipping a broken Windows bundle |
| `tools/obs-frame-source/` (Solin) | Add a Windows build (MSVC/clang, link `obs.lib`) producing `solin-framesrc.dll`; drop into `_libs/windows/x86_64/obs-plugins/`. No enforcement code to add — the `obs_module_ver` guard is automatic via `OBS_DECLARE_MODULE()`; just compile against the matching-tag OBS headers + `obs.lib` | Optional — only for browser/NDI compositing parity; **not** needed for vcam/playback |
| **NEW** `src/solin/core/media/vcam_provision_win.py` | Windows implementation: `detect`, `is_loaded`, `module_installed`, `find_loopback_device`, `install_hint`, `provision`, `provision_argv` per §4 spec | Must satisfy the same caller contract; reuse `VcamProvisionState`; `provision()` off-Qt, UAC when elevation needed, non-fatal cancel, idempotent |
| `src/solin/core/media/vcam_provision.py` | Convert to a `sys.platform`-dispatching **facade** (Linux branch as today; Windows delegates to `_win`). Keep `DESIRED_LABEL="Solin Virtual Camera"`, `VcamProvisionState`, `_SAFE_LABEL` | Zero changes at the two import sites (`live_integration_controller.py:237`, `obs_virtual_camera.py:27`) |
| `src/solin/core/media/obs_virtual_camera.py:69-79` | `platform_prerequisite_ok()`: Windows branch → `return is_loaded()` (not unconditional `True`) | Keep Linux behavior; keep exception-swallowing in `is_available()` |
| `src/solin/core/media/obs_virtual_camera.py:81-90` | `prerequisite_hint()`: add Windows registration guidance | Non-empty string on Windows |
| `src/solin/core/media/obs_virtual_camera.py:110-146` | No structural change expected — **VERIFY** `virtualcam_output` create/bind/start drives the DShow queue on Windows | Keep the platform-neutral path intact |
| **NEW (Phase 2)** standalone branded camera filter project | Custom `Solin Virtual Camera` filter (evaluate MF `MFCreateVirtualCamera` vs a GPLv2-derived DShow filter — see §5), new CLSID, both bitnesses if DShow, own installer | Friendly name exactly `DESIRED_LABEL`; if DShow, queue mapping name synced with `win-dshow` writer |
| Engine selection (optional) | Decide whether Windows should default `SOLIN_MEDIA_ENGINE=obs` once A+B are proven. Today it's env-only, no platform default (`bootstrap/media.py:66`) | Don't force obs mode until the Windows bundle is guaranteed present, or Qt fallback breaks |
| `src/solin/bootstrap/application.py:39-44` | No change — the Linux GL tweaks are already gated `linux` only | — |
| Tests | Extend `tests/unit/test_vcam_provision.py` with Windows-path cases using the `runner` seam; the brain (`vcam_model.py`, `vcam_director.py`) tests stay as-is (platform-agnostic) | Honor the memory note: write automated tests proactively |

---

## 7. Testing & verification plan

**Layer A — engine boots (headless smoke):**
- With the Windows pylibobs wheel installed and `SOLIN_MEDIA_ENGINE=obs`: assert `obs_media_engine_active()` is `True`, `ObsRuntime.ensure_started()` completes, `obs_reset_video` succeeds (this is where a missing `d3dcompiler_47.dll` bites), and `enum_output_types()` contains `"virtualcam_output"`. VERIFY the HWND display bind path (`window.py:109-138`) renders on D3D11.

**Layer B — device registration + start:**
- Confirm the filter is registered: `reg query HKLM\SOFTWARE\Classes\CLSID\{GUID}` (64) and `…\WOW6432Node\CLSID\{GUID}` (32). (For a Phase-2 per-user HKCU registration, query `HKCU\Software\Classes\CLSID\{GUID}` instead.)
- `virtual_camera().start()` returns `True` and `virtual_camera().active` is `True`.
- **Enumeration timing:** Solin (the frame producer) must be **running before** the consumer app opens the camera — the DShow filter reads from Solin's shared-memory queue; if Solin isn't pushing, the consumer sees the placeholder. Test: start Solin's vcam, then open the app.
- **Consumer apps — cover BOTH capture stacks:** confirm the device appears and shows live frames in:
  - a **DirectShow** host (e.g. classic desktop Zoom) — the case a DShow filter handles natively;
  - a **Media Foundation** host — **Chrome/Edge `getUserMedia` AND the new (WebView2/Edge-based) Teams** — this is where a DShow-only camera is known to fail or be flaky. **Explicitly test and record the result**; if it doesn't enumerate, that is the MF-bridge risk from §4/§5, not a Solin bug.
  - at least one **32-bit** host, to prove the 32-bit DLL registration works (64-bit-only registration is invisible to 32-bit hosts).
- Phase 1 shows "OBS Virtual Camera"; Phase 2 shows "Solin Virtual Camera".

**Layer C — Scenes drives it:**
- With the vcam active, edit a rule in the Scenes nav page → confirm `_push_vcam_scene_config` pushes to `vcam_director().set_config` and the composite changes live.
- Confirm the quick-toolbar clapperboard button appears (via `set_scene_override_available(True)`) and its menu (Automatic + `SCENE_PRESETS`) drives `on_vcam_scene_override`.
- Confirm a config saved while the vcam was off is applied on next start (`_load_vcam_scene_config`, `live_integration_controller.py:295-305`).

**Provisioning UX:**
- `provision()` triggers a UAC prompt off the GUI thread (when elevation is required); **user cancel** returns `False` and Solin still starts (degraded/unbranded), never blocks.

---

## 8. Milestones (do them in order)

1. **[ ] pylibobs Windows bundle builds & the engine boots.** Fix the `d3dcompiler_47` trim, pin the OBS `--version`, run `fetch_libs.py --platform windows`, install the wheel, set `SOLIN_MEDIA_ENGINE=obs`, verify `ObsRuntime.ensure_started()` + `virtualcam_output` present.
2. **[ ] Camera driven end-to-end under the STOCK name (Option a).** `regsvr32 /i /s` OBS's `obs-virtualcam-module{32,64}.dll` (manually/elevated), `virtual_camera().start()`, confirm "OBS Virtual Camera" shows live Solin frames in a DirectShow host + a Media-Foundation host (Chrome/new Teams) + a 32-bit host. **Record MF-host behavior** — flaky/absent enumeration here is the known DShow↔MF gap, and directly informs the Phase-2 architecture choice.
3. **[ ] Windows provisioning facade.** Implement `vcam_provision_win.py` + convert `vcam_provision.py` to a dispatcher; wire `platform_prerequisite_ok()`/`prerequisite_hint()`. `detect()` returns real states; `provision()` does `regsvr32 /i /s` via UAC, non-fatal cancel. Tests via the `runner` seam.
4. **[ ] Scenes drives the vcam live.** Confirm nav edits + toolbar override change the composite in real time; persisted-while-off config applies on next start.
5. **[ ] Branded "Solin Virtual Camera" (Option b).** Build the custom filter — **evaluate MF `MFCreateVirtualCamera` (b2) vs a GPLv2 DShow filter (b1)** first — new CLSID, both bitnesses if DShow, code-sign, ship an installer that registers it (per-user HKCU if feasible to skip UAC); `find_loopback_device()` reports `READY`.
6. **[ ] (Parity) `solin-framesrc.dll` for Windows** — only if live browser/NDI compositing inside libobs is wanted; otherwise the Qt fallback covers it.

---

## 9. Risks / open questions / verify-on-the-metal

- **VERIFY (highest-impact):** the `d3dcompiler_47.dll` trim is the single most likely reason a freshly-fetched Windows bundle fails at `obs_reset_video`. Confirm on the metal before assuming the engine "just boots."
- **Media Foundation vs DirectShow (major):** a DirectShow-only virtual camera is **not reliably enumerable** by MF consumers (Chrome, Edge, new WebView2-based Teams, Windows Camera). Test both stacks in Phase 1 before committing to a custom DShow filter; the MF-native `MFCreateVirtualCamera` API (§5 b2) is the Microsoft-supported modern alternative and can register per-user without UAC.
- **VERIFY:** `virtualcam_output` create/bind/start on Windows actually pushes frames into the DShow shared-memory queue and reaches consumer apps. The Solin code path is platform-neutral but untested off Linux.
- **VERIFY:** the concrete OBS CLSID `{A3FCE0F5-3493-419F-958A-ABA1250EC20B}` (recalled, not confirmed). Only matters if you touch OBS's own registration; mint your own GUID for the branded filter regardless.
- **OBS tag / ABI is currently UNPINNED (major):** the Windows bundle tracks OBS `latest` via `get_release(None)` — there is no `OBS_TAG` variable. `obs_runtime.py:13` docstring says "32.1.2", Linux SONAME is `.30`; the `obs_module_ver == obs_get_version` load-time check is the real guard. Pin `--version <tag>` in CI and build every custom module against that same tag's headers/import lib, or a stale-SDK module will fail to load against a newer `latest` `obs.dll`.
- **GPLv2 exposure is not Phase-2-only (major):** bundling and in-process-loading libobs (`obs.dll`) and the OBS plugins (incl. `win-dshow.dll`) is **itself GPLv2** and already attaches to Solin's distribution **now — Linux included**, independent of any custom filter. Shipping the Phase-1 "stock OBS filter" bundle does **not** dodge GPL. The custom DShow filter (§5 b1) is an *additional* GPL item, not the first one. **Direct counsel to review the whole libobs-bundling distribution model**, not just the Phase-2 filter. (An MF `MFCreateVirtualCamera` filter avoids the *filter* GPL derivation but not the separate libobs-bundling question.)
- **Code-signing:** an unsigned COM camera DLL registered under the registry will trip SmartScreen/AV and may be blocked by managed environments. Plan Authenticode signing for the DLL(s) and the installer.
- **Admin/UAC UX:** OBS's shipped filter registration is a one-time elevated system change (write to `HKLM\SOFTWARE\Classes`). If you keep Option (a), do it in an **installer step the user consents to**, not silently at runtime — mirror the Linux "prompt once, persist" model. Elevation is **not intrinsic** to a virtual camera, though: a Phase-2 custom filter (DShow or MF) can register **per-user under `HKCU\Software\Classes`** and skip UAC — validate that against your target consumer apps. Runtime "Start" needs no elevation either way.
- **32-bit filter DLL:** if you ship a DShow filter, produce and register a 32-bit build too — the mainstream hosts are 64-bit, but legacy/embedded/32-bit-Electron hosts persist, and a 64-bit-only registration is invisible to them.
- **`solin-framesrc` Windows toolchain:** no Windows build exists (no `.dll`, and pylibobs has no `.c`/CMake for it — the C source lives in the Solin repo at `tools/obs-frame-source/solin-framesrc.c`). Needs an MSVC/clang build + a Windows `obs.lib` import lib of matching ABI. The `obs_module_ver` guard is automatic via `OBS_DECLARE_MODULE()`; no enforcement code to write. Not on the vcam critical path.
- **CI verify weakness:** `pylibobs/.github/workflows/release.yml:61-73` only checks the target directory **exists** and prints a file count — it **asserts nothing**, so even an empty/incomplete bundle passes. Add per-file assertions before relying on released wheels.
- **Enumeration timing / exclusive-caps analog:** on Linux `exclusive_caps=1` makes the loopback advertise capture-only so apps accept it; on Windows the equivalent is simply that the registered DShow filter sits in `CLSID_VideoInputDeviceCategory` (or the MF virtual-camera registration). The practical constraint is producer-before-consumer (Solin must be pushing frames before the app opens the device).
