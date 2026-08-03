# Solin Virtual Camera on macOS — handover

> **Status:** the Solin-side half is done and tested. What remains is a **Camera
> Extension** in Swift, which cannot be written or verified from a non-macOS
> machine — it needs Xcode, an Apple Developer ID, and notarisation.
>
> **Reading discipline:** everything under "Already done" is implemented and
> covered by tests. Everything under "Your part" is specification, not code that
> exists. Nothing in this document has been run on macOS.

---

## 1. What a macOS virtual camera actually is

Since macOS 12.3 the supported mechanism is a **Camera Extension**
(`CMIOExtension`), replacing the deprecated CoreMediaIO DAL plugin. The
constraints shape everything else:

- It is a **system extension bundled inside the host `.app`** — at
  `Solin.app/Contents/Library/SystemExtensions/`. It cannot be installed
  standalone, and it cannot live outside the app bundle.
- It must be **signed with an Apple Developer ID** and carry the
  `com.apple.developer.system-extension.install` entitlement. macOS refuses to
  load an unsigned camera extension — there is no developer-mode escape hatch
  comparable to Windows, where an unsigned DirectShow filter registers happily.
- It must be **notarised** for distribution outside the App Store.
- The host app requests installation via `OSSystemExtensionRequest`, and the
  **user must then approve it** in System Settings › General › Login Items &
  Extensions › Camera Extensions.
- The extension runs **out of process**, in its own sandbox, hosted by the
  system — not inside Solin.

That last point is the architectural one: like the Windows Media Foundation
route (and unlike the DirectShow filter, which loads inside the consuming app),
frames must cross a process boundary.

---

## 2. Already done — the Solin side

| Piece | File | Notes |
|---|---|---|
| Provisioning facade dispatch | `src/solin/core/media/vcam_provision.py` | `darwin` now delegates instead of returning `UNSUPPORTED` |
| macOS detection + guidance | `src/solin/core/media/vcam_provision_mac.py` | detect / is_loaded / module_installed / find_loopback_device / hints |
| Tests | `tests/unit/test_vcam_provision_mac.py` | fully mocked, run on any OS |
| Graphics-module path fix | `src/solin/core/media/obs_runtime.py` | see §5 — this was a real bug |

`vcam_provision_mac` answers the same contract as Linux and Windows, so the
controller, the Scenes UI and the status reporting need **no macOS-specific
code**:

- `MODULE_MISSING` — the extension is not inside this `.app` (a broken build)
- `NOT_LOADED` — bundled, but the user has not approved it yet (**the normal
  first-run state**)
- `READY` — `systemextensionsctl list` reports it `activated enabled`

`provision()` deliberately returns `False` with a hint. It does **not** attempt
installation, because it cannot: `OSSystemExtensionRequest` is a Swift API that
must be called from the signed app. Returning `True` would make the caller skip
the guidance and leave the operator with no camera and no explanation.

**Identifier contract — the Swift target must match these exactly:**

```python
EXTENSION_ID = "com.solin.Solin.CameraExtension"   # vcam_provision_mac.py
DESIRED_LABEL = "Solin Virtual Camera"             # vcam_provision.py
```

macOS requires the extension's bundle id to be **prefixed by the host app's** id,
so if the app id is not `com.solin.Solin`, change `EXTENSION_ID` to match — the
detection reads `systemextensionsctl list` for that exact string.

---

## 3. Your part — the Camera Extension

### 3.1 Targets

1. A **Camera Extension** target (Xcode: *System Extension* → *Camera
   Extension*), embedded in `Solin.app`.
2. A small amount of host-app code to call `OSSystemExtensionRequest
   .activationRequest(forExtensionWithIdentifier:queue:)` and report the
   delegate's result.

Entitlements: `com.apple.developer.system-extension.install` on the host app;
the extension needs the camera-extension entitlement Xcode adds for the template.

### 3.2 The frame path — the part that needs a decision

Solin composites in **libobs** and already publishes NV12 frames. On Windows
that goes through a file-backed mapping (`vcam_transport.py`) because the
consumer runs in another session under another account. macOS is a different
shape again: the extension is a separate sandboxed process owned by the system.

Recommended: **`IOSurface` passed through an XPC service**, which is the
idiomatic macOS answer and what the platform is designed around — the extension
wraps the `IOSurface` in a `CMSampleBuffer` with zero copies. A file-backed
mapping like the Windows one is possible but fights the sandbox and adds a copy.

Whichever you choose, the Python side needs a macOS sibling of
`RawVideoBridge` in `src/solin/core/media/vcam_transport.py`. The existing class
is a good template: it taps the vcam's own `obs_view` via
`video_output_connect`, asks libobs for NV12 at exactly the geometry the camera
advertises, and publishes. **Two hard-won details from the Windows build, both
of which will bite identically here:**

- **Ask libobs to scale.** Pass a `video_scale_info` to `video_output_connect`.
  The canvas is 1920×1080 while the camera advertises 1280×720; without it the
  frames arrive canvas-sized and a naive row copy *crops* rather than scales —
  which looks like a mysterious "logo in the corner" bug.
- **A view's mix only renders while an output is active on it.** A raw callback
  alone yields **zero** frames. Windows keeps a cheap `ffmpeg_output` (rawvideo →
  null muxer) attached purely as a pump. macOS will need the same trick.

### 3.3 The camera's picture

`VirtualCamera` composites on its own `obs_view`: channel 0 is the branded idle
logo, channel 1 mirrors the projector program, channel 2 is the meeting camera.
That is all platform-neutral and already works — you are only replacing the
sink.

---

## 4. Sequence, once the extension exists

1. Solin starts → `vcam_provision.detect()` → `MODULE_MISSING` if not bundled.
2. Bundled but unapproved → `NOT_LOADED` → Solin shows `prerequisite_hint()`.
   `vcam_provision_mac.open_extension_settings()` opens the right pane.
3. Host app issues the `OSSystemExtensionRequest`; user approves.
4. `systemextensionsctl list` flips to `activated enabled` → `READY`.
5. `VirtualCamera.start()` runs unchanged and frames flow.

Step 3 is the only piece with no Python equivalent.

---

## 5. Fixed on the way, worth knowing

`obs_runtime._graphics_module_path()` looked for
`macos/<arch>/libobs-opengl.dylib`, but pylibobs ships the mac bundle .app-style
as **`macos/<arch>/Frameworks/`**. It therefore never matched, and the
absolute-path guarantee silently degraded to libobs' bare default name. Fixed,
with a test.

This is the same class of bug as the Windows one, where the code asked for
`libobs-opengl.dll` on a platform that needs D3D11 and `obs_reset_video` failed
with a bare "not supported". **Verify the engine starts on macOS before
debugging the camera** — `libobs-metal.dylib` also ships in the bundle, and
whether OpenGL or Metal is the right default there is untested.

Also untested on macOS: whether pylibobs' mac wheel loads at all in a Nuitka
build. `pylibobs 0.1.0` is on PyPI with a `macosx_12_0_arm64` wheel, and the
release CI now asserts the mac bundle contains
`Frameworks/libobs.dylib` and its shader effects.

---

## 6. Risks, ranked

1. **Signing and notarisation are not optional.** No Developer ID means no
   camera, and no way to test locally. Budget for this before writing code.
2. **The engine underneath is unverified on macOS.** If libobs does not start,
   the camera is moot — and since Solin dropped its Qt fallback, a libobs
   failure now costs *all* media playback, not just the camera.
3. **Frame transport across the sandbox** is the main design work; get the
   `IOSurface`/XPC shape right before writing much else.
4. **The scale and pump gotchas in §3.2** cost real debugging time on Windows.
   They are properties of libobs, not of Windows, so expect both.
5. **User approval is a UX cliff.** The camera does not exist until someone
   visits System Settings. `NOT_LOADED` plus `prerequisite_hint()` is wired, but
   the wording deserves a native speaker's eye.
