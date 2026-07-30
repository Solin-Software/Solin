# Patched `linux-capture` — raw-window-id capture

Solin projects the **live browser tab** on the second monitor. To make it a real
libobs scene (so it crossfades like every other content and can be picked up by a
future virtual-camera / recording), we capture the browser window with OBS's X11
window-capture source (`xcomposite_input`).

**Problem:** stock `xcomposite_input` only targets *top-level* managed windows
(those the window manager lists in `_NET_CLIENT_LIST`). Solin's browser is a
`sideview.NativeWebView` (WebKitGTK) that Qt **reparents as a native child**, so
it is not top-level and cannot be selected.

**Fix:** [`xcomposite-raw-window.patch`](./xcomposite-raw-window.patch) makes two
small changes to `xcomposite-input.c`:

1. **`raw:<xid>` window spec** in `xcomp_find_window` — resolves an exact X window
   id directly, skipping the top-level `_NET_CLIENT_LIST` search. The compositing
   path (composite-redirect + `name_window_pixmap` → GL texture) is
   window-agnostic, so a child id captures fine. No behavior change for normal
   top-level captures.
2. **`XCB_REPARENT_NOTIFY` handling** in `watcher_process` — Solin parks the
   captured window into an off-screen keep-alive host to survive an operator
   panel switch (so the projection doesn't go black). A reparent frees the
   redirected pixmap, but stock code has no case for it, so recovery waited up to
   `FIND_WINDOW_INTERVAL` (2s) → a visible black flash. Treating the reparent as a
   change rebuilds the pixmap on the next frame (near-instant).

Solin reads the browser's foreign X id (`tab.view._foreign_window.winId()`),
creates the source with `capture_window = "raw:<xid>"`, and parks the window
(keeping it mapped) when its page is hidden.

## Build + install

```bash
tools/obs-linux-capture-patch/build.sh
```

This checks out obs-studio at the tag matching the **bundled** libobs
(`32.2.1` — the module API major version is compiled in, so a mismatched build is
rejected at load), fetches SIMDe, applies the patch, builds `linux-capture.so`
against the bundled libobs, verifies `obs_module_ver` matches, and installs it
into the venv's pylibobs (`_libs/.../obs-plugins/linux-capture.so`, backing up the
stock one as `.orig`).

Re-run after editing the patch. Requires: `gcc`, `cmake`/`pkg-config`, and the
X11/XCB dev headers (`libxcb*-dev`, `libx11*-dev`) — already present on this box.

## Runtime preconditions

- X11 only. `xcomposite_input` is registered by libobs **only when the nix
  platform is `X11_EGL`** (set before `load_modules`); Solin's `obs_runtime`
  already does this in the real Qt app.
- WebKitGTK must paint into the captured (redirected) window under accelerated
  compositing. If capture is black, try `WEBKIT_DISABLE_COMPOSITING_MODE=1` for
  the browser process, or fall back to `xshm_input` region capture.

## Provenance / license

`linux-capture` is part of OBS Studio (GPL-2.0-or-later). The patch is a derived
work under the same license. Source tag: `32.2.1`.
