#!/usr/bin/env bash
# Build a patched linux-capture.so for the bundled pylibobs libobs.
#
# Why: the stock OBS xcomposite_input window-capture can only target *top-level*
# managed windows (those in _NET_CLIENT_LIST). Solin's live browser tab is a
# WebKitGTK surface reparented as a native Qt child, so it is not top-level and
# cannot be targeted. xcomposite-raw-window.patch adds a "raw:<xid>" spec that
# captures an exact window id directly, letting us composite (and crossfade) the
# browser as a real libobs scene on channel 0.
#
# The plugin must match the bundled libobs ABI EXACTLY (module API major version
# is baked in at compile time), so we build against the obs-studio source tag
# that matches the bundled libobs — 32.1.2 — not whatever is checked out.
#
# Usage:
#   tools/obs-linux-capture-patch/build.sh            # build + install into the venv pylibobs
#   OBS_TAG=32.1.2 PYLIBOBS_LIBS=/path build.sh       # override
#
# Idempotent: re-run after editing the patch to rebuild + reinstall.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Must match the bundled libobs's obs_get_version EXACTLY (checked below before
# install). Confirm with: python -c "import ctypes,glob;l=ctypes.CDLL(glob.glob('.../libobs.so.0')[0]);l.obs_get_version.restype=ctypes.c_uint32;v=l.obs_get_version();print((v>>24)&255,(v>>16)&255,v&0xffff)"
OBS_TAG="${OBS_TAG:-32.2.1}"
OBS_REPO="${OBS_REPO:-/home/jonata/Projetos/obs-studio}"
WORK="${WORK:-${TMPDIR:-/tmp}/solin-linux-capture-build}"
# Where the bundled libobs + obs-plugins live: ask the venv's pylibobs directly
# (it may be an editable install outside site-packages).
PY="${PY:-$(cd "$HERE/../.." && pwd)/.venv/bin/python}"
PYLIBOBS_LIBS="${PYLIBOBS_LIBS:-$("$PY" -c "import os,pylibobs;print(os.path.join(os.path.dirname(pylibobs.__file__),'_libs','linux','x86_64'))")}"

echo ">> obs tag        : $OBS_TAG"
echo ">> obs repo       : $OBS_REPO"
echo ">> work dir       : $WORK"
echo ">> pylibobs libs  : $PYLIBOBS_LIBS"
[ -d "$PYLIBOBS_LIBS" ] || { echo "!! pylibobs libs dir not found: $PYLIBOBS_LIBS"; exit 1; }

mkdir -p "$WORK"
WT="$WORK/obs-$OBS_TAG"
SIMDE="$WORK/simde"
GEN="$WORK/gen"
OUT="$WORK/out"
mkdir -p "$GEN" "$OUT"

# 1. obs-studio source at the matching tag (worktree off the existing clone).
if [ ! -d "$WT/libobs" ]; then
  git -C "$OBS_REPO" fetch --depth 1 origin "refs/tags/$OBS_TAG:refs/tags/$OBS_TAG" || true
  git -C "$OBS_REPO" worktree add --detach "$WT" "$OBS_TAG"
fi

# 2. SIMDe (header-only; obs fetches it at configure time).
[ -f "$SIMDE/simde/x86/sse2.h" ] || git clone --depth 1 https://github.com/simd-everywhere/simde "$SIMDE"

# 3. Minimal obsconfig.h (normally CMake-generated; the plugin uses none of the
#    path/feature flags — only the version-suffix macros are consumed).
cat > "$GEN/obsconfig.h" <<'EOF'
#pragma once
#define OBS_RELEASE_CANDIDATE 0
#define OBS_BETA 0
EOF

# 4. Apply the raw-window patch (idempotent).
PLUG="$WT/plugins/linux-capture"
if ! grep -q 'Solin patch' "$PLUG/xcomposite-input.c"; then
  git -C "$WT" apply "$HERE/xcomposite-raw-window.patch"
fi
grep -q 'Solin patch' "$PLUG/xcomposite-input.c" || { echo "!! patch not applied"; exit 1; }

# 5. Build the module against the matching headers + bundled libobs.
XCB=$(pkg-config --cflags --libs xcb xcb-composite xcb-xfixes xcb-shm xcb-randr xcb-xinerama x11 x11-xcb)
gcc -shared -fPIC -O2 -o "$OUT/linux-capture.so" \
  "$PLUG/linux-capture.c" "$PLUG/xcomposite-input.c" "$PLUG/xshm-input.c" \
  "$PLUG/xcursor-xcb.c" "$PLUG/xhelpers.c" \
  -I "$GEN" -I "$WT/libobs" -I "$WT/deps/glad/include" -I "$SIMDE" -I "$PLUG" \
  $XCB -L "$PYLIBOBS_LIBS" -Wl,-rpath,'$ORIGIN/..' -lobs

# 6. Sanity: module API version must match the bundled libobs, else it is rejected.
have=$(LD_LIBRARY_PATH="$PYLIBOBS_LIBS" python3 - "$OUT/linux-capture.so" <<'PY'
import ctypes,sys
l=ctypes.CDLL(sys.argv[1]); l.obs_module_ver.restype=ctypes.c_uint32; print(l.obs_module_ver())
PY
)
want=$(LD_LIBRARY_PATH="$PYLIBOBS_LIBS" python3 - "$PYLIBOBS_LIBS/obs-plugins/linux-capture.so" <<'PY'
import ctypes,sys
l=ctypes.CDLL(sys.argv[1]); l.obs_module_ver.restype=ctypes.c_uint32; print(l.obs_module_ver())
PY
)
[ "$have" = "$want" ] || { echo "!! module API mismatch: built=$have bundled=$want"; exit 1; }
echo ">> module API version matches bundled ($have)"

# 7. Install (back up the stock plugin once).
DEST="$PYLIBOBS_LIBS/obs-plugins/linux-capture.so"
[ -f "$DEST.orig" ] || cp "$DEST" "$DEST.orig"
cp "$OUT/linux-capture.so" "$DEST"
echo ">> installed patched linux-capture.so -> $DEST"
echo ">> done."
