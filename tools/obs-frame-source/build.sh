#!/usr/bin/env bash
# Build the solin-framesrc libobs plugin (a tiny async video source that displays
# frames pushed from Python via obs_source_output_video). See solin-framesrc.c.
#
# Like the linux-capture patch, the module's API version is compiled in, so it
# MUST be built against the obs-studio source tag matching the bundled libobs
# (checked below before install). Reuses the shared obs worktree + SIMDe that the
# capture build sets up (or creates them).
#
# Usage: tools/obs-frame-source/build.sh    # build + install into the venv pylibobs
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBS_TAG="${OBS_TAG:-32.2.1}"
OBS_REPO="${OBS_REPO:-/home/jonata/Projetos/obs-studio}"
WORK="${WORK:-${TMPDIR:-/tmp}/solin-linux-capture-build}"  # share the capture build's dir
PY="${PY:-$(cd "$HERE/../.." && pwd)/.venv/bin/python}"
PYLIBOBS_LIBS="${PYLIBOBS_LIBS:-$("$PY" -c "import os,pylibobs;print(os.path.join(os.path.dirname(pylibobs.__file__),'_libs','linux','x86_64'))")}"

echo ">> obs tag       : $OBS_TAG"
echo ">> pylibobs libs : $PYLIBOBS_LIBS"
[ -d "$PYLIBOBS_LIBS" ] || { echo "!! pylibobs libs dir not found: $PYLIBOBS_LIBS"; exit 1; }

WT="$WORK/obs-$OBS_TAG"
SIMDE="$WORK/simde"
GEN="$WORK/gen"
OUT="$WORK/out"
mkdir -p "$GEN" "$OUT"

# obs source at the matching tag (shared worktree).
if [ ! -d "$WT/libobs" ]; then
  git -C "$OBS_REPO" fetch --depth 1 origin "refs/tags/$OBS_TAG:refs/tags/$OBS_TAG" || true
  git -C "$OBS_REPO" worktree add --detach "$WT" "$OBS_TAG"
fi
# SIMDe (header-only).
[ -f "$SIMDE/simde/x86/sse2.h" ] || git clone --depth 1 https://github.com/simd-everywhere/simde "$SIMDE"
# Minimal obsconfig.h (CMake-generated normally; only version macros are consumed).
[ -f "$GEN/obsconfig.h" ] || printf '#pragma once\n#define OBS_RELEASE_CANDIDATE 0\n#define OBS_BETA 0\n' > "$GEN/obsconfig.h"

# Build (only libobs is needed; no X11/XCB).
gcc -shared -fPIC -O2 -o "$OUT/solin-framesrc.so" "$HERE/solin-framesrc.c" \
  -I "$GEN" -I "$WT/libobs" -I "$SIMDE" \
  -L "$PYLIBOBS_LIBS" -Wl,-rpath,'$ORIGIN/..' -lobs

# Sanity: module API version must match the bundled libobs.
have=$(LD_LIBRARY_PATH="$PYLIBOBS_LIBS" "$PY" - "$OUT/solin-framesrc.so" <<'PY'
import ctypes,sys
l=ctypes.CDLL(sys.argv[1]); l.obs_module_ver.restype=ctypes.c_uint32; print(l.obs_module_ver())
PY
)
want=$(LD_LIBRARY_PATH="$PYLIBOBS_LIBS" "$PY" - "$PYLIBOBS_LIBS/libobs.so.0" <<'PY'
import ctypes,sys
l=ctypes.CDLL(sys.argv[1]); l.obs_get_version.restype=ctypes.c_uint32; print(l.obs_get_version())
PY
)
[ "$have" = "$want" ] || { echo "!! module API mismatch: built=$have libobs=$want"; exit 1; }
echo ">> module API version matches bundled libobs ($have)"

cp "$OUT/solin-framesrc.so" "$PYLIBOBS_LIBS/obs-plugins/solin-framesrc.so"
echo ">> installed -> $PYLIBOBS_LIBS/obs-plugins/solin-framesrc.so"
echo ">> done."
