# Solin Virtual Camera — Media Foundation source (Windows 11)

The branded `"Solin Virtual Camera"` device for Windows. This is the Phase-2
replacement for the stock "OBS Virtual Camera" name that Phase 1 exposes.

## Why Media Foundation rather than a DirectShow filter

| | DirectShow filter (OBS's approach) | Media Foundation (this) |
|---|---|---|
| Device name | compile-time constant in the DLL | **runtime argument** to `MFCreateVirtualCamera` |
| Registration | `HKLM` → needs admin/UAC | `HKLM` → needs admin/UAC (see below) |
| Chrome / Edge / new Teams | unreliable (they capture via MF) | native |
| Legacy DShow hosts | native | Windows bridges MF → DShow |
| Licence | derived from OBS `win-dshow` ⇒ **GPLv2** | Microsoft API ⇒ no GPL derivation |

## Verified on Windows 11 build 22631

Measured on the metal, not assumed:

- `MFCreateVirtualCamera` is exported from `mfsensorgroup.dll`. ✔
- MSVC + Windows SDK 10.0.26100 compile and link against `mfvirtualcamera.h`. ✔
- **An unsigned DLL is accepted** — `MFCreateVirtualCamera(...) -> S_OK` with no
  code-signing certificate. Signing is still worth doing before shipping, to
  avoid SmartScreen/AV friction, but it does not block development. ✔
- The media source is driven through `IMFActivate`, **not** `IMFMediaSource`
  directly. The capture pipeline `CoCreateInstance`s the CLSID, queries
  `IMFActivate`, and calls `ActivateObject()`. Returning the source itself fails
  `Start()` with `E_NOINTERFACE`. ✔
- The stream descriptor must carry `MF_DEVICESTREAM_STREAM_ID`,
  `MF_DEVICESTREAM_STREAM_CATEGORY` (`PINNAME_VIDEO_CAPTURE`) and
  `MF_DEVICESTREAM_FRAMESERVER_SHARED`, or `Start()` fails with
  `MF_E_ATTRIBUTENOTFOUND`. ✔
- Do **not** set `MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK`
  yourself; `MFCreateVirtualCamera` mints the real one. ✔

### Per-user registration is NOT sufficient — this corrects a common assumption

The frequently-cited advantage that a Media Foundation camera "registers
per-user and skips UAC" **does not hold for the COM server itself**:

```
FrameServer   StartName: NT AUTHORITY\LocalService   svchost.exe -k Camera
```

`IMFVirtualCamera::Start()` hands the source off to that service, which runs as
`LocalService` and therefore cannot see `HKCU\Software\Classes` of the logged-on
user. With the DLL registered only under `HKCU`, `Start()` fails with
`0x80070003 ERROR_PATH_NOT_FOUND` — the class is simply not resolvable from the
service's registry view. Registration must go to **`HKLM`**, which needs a
one-time elevated step exactly like the DirectShow filter.

What Media Foundation still buys over DirectShow is therefore *not* the absence
of UAC, but:

- a **runtime-settable device name** (the whole point — branding without a
  custom-named filter), and
- native enumeration in Chrome / Edge / new Teams, and
- no GPLv2 derivation from OBS's `win-dshow`.

## Status: working end-to-end

The media source is implemented and **verified delivering frames** — activation
object, event queues, NV12 presentation/stream descriptors, and an
`IMFMediaStream` answering `RequestSample` with real `IMFSample`s.

`vcam-check.exe`, against the machine-wide registration:

```
ok    IMFVirtualCamera::Start
      Solin Virtual Camera (Câmera Virtual do Windows)   <-- ours
      activate hr=0x00000000
ok    frame 1: 1382400 bytes, ts=1533331800
ok    frame 2: 1382400 bytes, ts=1558998441
```

1382400 bytes is exactly 1280×720 NV12. Windows appends a localized suffix to
the friendly name, so **match the device by prefix, never by equality**.

It renders a scrolling test pattern, not Solin's composite — that is the one
remaining piece.

### The source is hosted OUT-OF-PROCESS — this drives the transport design

Frames arrive without the in-process `first sample` trace ever firing, and the
frame server's own load leaves no log line (it runs as `LocalService` and is
denied write access to `mfcam.log`). So the source really is instantiated inside
the frame server, in **session 0, under a different account** than Solin.

That rules out the obvious IPC choices:

- `Local\` named shared memory — wrong session; the frame server is in session 0.
- `Global\` named shared memory — creating one needs `SeCreateGlobalPrivilege`,
  which an unelevated Solin does not hold.

The workable option is a **file-backed mapping at a fixed machine-wide path**
(`C:\ProgramData\Solin\vcam-frame.bin`): both processes map it by path, so it
crosses both the session and the account boundary. The path must be machine-wide,
not under `%LOCALAPPDATA%` — that would resolve against the *frame server's*
profile, not the user's. Grant `LocalService` read access explicitly rather than
assuming inheritance; the log-write denial above shows those ACLs bite.

## Camera lifetime — persist it, like OBS

`MFCreateVirtualCamera` takes a lifetime, and the choice is not cosmetic:

- `MFVirtualCameraLifetime_Session` — the device exists only while the creating
  process holds the handle. It disappears the moment Solin closes.
- `MFVirtualCameraLifetime_System` — registered once, stays in every
  application's camera list permanently. This is what OBS's DirectShow filter
  effectively does, and it is the right default.

Session lifetime looks tidier but is wrong in practice, because **most
applications enumerate cameras only at startup**: a device that appears after
Chrome launched typically will not show up until Chrome is restarted.

**A persistent camera must not be sent `Stop()` or `Shutdown()`** — either
unpublishes the device and the registration is lost. Release the handle and
nothing else; that is what makes it survive process exit. `Remove()` is the
deliberate way to unregister.

```bat
python tools\solin-mfcam\register-camera.py            REM elevated: register for good
python tools\solin-mfcam\register-camera.py --remove   REM elevated: unregister
```

Registering needs elevation once, because it is a machine-wide device. After
that Solin just starts and stops *feeding* it, with no further prompts — and the
media source shows its fallback image whenever Solin is not producing, exactly
as OBS shows a placeholder.

## Still to do

1. Add the file-backed frame mapping: writer in Solin, reader replacing
   `fill_test_pattern`. Feed it from libobs' raw output binding rather than OBS's
   `virtualcam_output` queue, to avoid depending on OBS's internal format.
2. Solin-side wiring: call `MFCreateVirtualCamera` with
   `friendlyName="Solin Virtual Camera"`, hold the `IMFVirtualCamera` for the
   session, and point `vcam_provision_win.detect()` at this CLSID instead of the
   DirectShow filter.
3. Ship the DLL to a machine-wide location and register it from the installer
   (one elevated step), rather than from a working tree.

## Build

```bat
tools\solin-mfcam\build.bat
```

Needs Visual Studio Build Tools 2022 (C++ workload) and a Windows 10/11 SDK.
No OBS headers are involved — this component talks to Media Foundation only, so
it is not subject to libobs' ABI lock-step.

## Register / unregister

Must be **machine-wide (`HKLM`)**, from an elevated prompt — see the frame-server
finding above. `DllRegisterServer` writes `HKCU` when unelevated, which builds
and registers fine but fails at `Start()`; run it elevated so the write lands in
`HKLM` where `LocalService` can see it.

```bat
regsvr32 /s C:\ProgramData\Solin\solin-mfcam.dll
regsvr32 /s /u C:\ProgramData\Solin\solin-mfcam.dll
```

Install the DLL somewhere readable by `LocalService` (`C:\ProgramData\Solin` or
Program Files) — **not** under `C:\Users\<name>\`, which that account cannot
traverse.

`regsvr32` is a GUI-subsystem binary and returns immediately; wait for it
(`Start-Process regsvr32 -ArgumentList ... -Wait`) before checking the registry,
or you will read a stale value.

Registration alone creates no camera: a device only exists while some process
holds an `IMFVirtualCamera` from `MFCreateVirtualCamera`. With
`MFVirtualCameraLifetime_Session` it disappears when that process exits.

## CLSID

`{6F9C1B24-6E5A-4F4E-9C1D-2C7A5E3B8D41}` — minted for Solin. Never reuse OBS's
`{A3FCE0F5-3493-419F-958A-ABA1250EC20B}`: that CLSID belongs to OBS's own
registration, and writing to it would hijack a co-installed OBS Studio.
