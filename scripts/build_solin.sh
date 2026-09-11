#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_ROOT="${SOLIN_BUILD_DIR:-${PROJECT_ROOT}/build/linux}"
if [[ -n "${SOLIN_WORK_DIR:-}" ]]; then
    WORK_ROOT="${SOLIN_WORK_DIR}"
elif grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
    WORK_ROOT="${HOME}/.cache/solin/build-linux"
else
    WORK_ROOT="${OUTPUT_ROOT}"
fi
QML_CACHE_ROOT="${WORK_ROOT}/qmlcache"
QML_APP_DIR="${QML_CACHE_ROOT}/solin/qml/Solin"
QML_QT_DIR="${QML_CACHE_ROOT}/PySide6/qml"
QT_LIBRARY_DIR="${QML_CACHE_ROOT}/qt-libs"
DIST_DIR="${WORK_ROOT}/main.dist"
FINAL_DIST_DIR="${OUTPUT_ROOT}/main.dist"

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# Direct DT_NEEDED providers for the pinned pylibobs Linux wheel and OBS mux.
# Apt supplies their transitive dependencies from the same Ubuntu archive.
libobs_runtime_packages=(
    libasound2t64
    libavcodec60
    libavdevice60
    libavformat60
    libavutil58
    libc6
    libcurl4t64
    libdrm2
    libegl1
    libfontconfig1
    libfreetype6
    libgcc-s1
    libglib2.0-0t64
    libjansson4
    libmbedcrypto7t64
    libmbedtls14t64
    libmbedx509-1t64
    libpci3
    libpipewire-0.3-0t64
    libpulse0
    librist4
    libspeexdsp1
    libsrt1.5-openssl
    libstdc++6
    libswresample4
    libswscale7
    libudev1
    libuuid1
    libv4l-0t64
    libva-drm2
    libva2
    libvpl2
    libwayland-client0
    libwayland-egl1
    libx11-6
    libx11-xcb1
    libx264-164
    libxcb-composite0
    libxcb-randr0
    libxcb-shm0
    libxcb-xfixes0
    libxcb-xinerama0
    libxcb1
    libxkbcommon0
    zlib1g
)
if [[ "${1:-}" == "--print-native-packages" && "$#" == 1 ]]; then
    printf '%s\n' "${libobs_runtime_packages[@]}"
    exit 0
fi
[[ "$#" == 0 ]] || fail "Usage: $0 [--print-native-packages]"

resolve_linked_library() {
    local binary="$1"
    local library_name="$2"

    ldd "${binary}" \
        | awk -v library_name="${library_name}" \
            '$1 == library_name && $2 == "=>" { print $3; exit }'
}

stage_linked_library() {
    local binary="$1"
    local library_name="$2"
    local library_path

    library_path="$(resolve_linked_library "${binary}" "${library_name}")"
    [[ -n "${library_path}" && -f "${library_path}" ]] \
        || fail "Linux runtime dependency was not found: ${library_name}"
    cp -Lf "${library_path}" "${DIST_DIR}/${library_name}"
}

resolve_python() {
    local candidate
    for candidate in \
        "${SOLIN_PYTHON:-}" \
        "${PROJECT_ROOT}/.venv/bin/python" \
        "${HOME}/.venvs/solin/bin/python"; do
        if [[ -n "${candidate}" && -x "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return
        fi
    done

    command -v python3 || fail \
        "Python 3.13 was not found. Set SOLIN_PYTHON to the Linux environment's interpreter."
}

[[ "$(uname -s)" == "Linux" ]] || fail "This script must run on Linux or WSL 2."

PYTHON="$(resolve_python)"
cd "${PROJECT_ROOT}"

"${PYTHON}" - <<'PY'
import platform
import sys

if sys.version_info < (3, 13):
    raise SystemExit(
        f"Solin requires Python 3.13 or newer; found {sys.version.split()[0]}. "
        "Set SOLIN_PYTHON to a compatible Linux interpreter."
    )
if platform.machine() != "x86_64":
    raise SystemExit("Solin Linux builds require x86_64.")
libc, version = platform.libc_ver()
if libc != "glibc" or tuple(map(int, version.split("."))) < (2, 38):
    raise SystemExit(
        "The Ubuntu 24.04 libobs dependency runtime requires glibc 2.38 or newer. "
        "Use Ubuntu 24.04 x86_64 (including WSL 2). Ubuntu 22.04 is unsupported."
    )
PY

for command_name in curl dpkg-deb gcc patchelf readelf sha256sum; do
    command -v "${command_name}" >/dev/null || fail \
        "${command_name} is required. On Ubuntu run: sudo apt install binutils build-essential curl dpkg patchelf"
done

# The published Linux wheel omits usr/local/bin/obs-ffmpeg-mux. Extract the
# matching official release without installing OBS or changing apt sources.
OBS_RUNTIME_VERSION="32.1.2"
OBS_RUNTIME_SHA256="a3bb1b0176604dad9e22710e057f0fdd76e8afb600e0e1914c30464ae49908e8"
OBS_RUNTIME_NAME="OBS-Studio-${OBS_RUNTIME_VERSION}-Ubuntu-24.04-x86_64.deb"
OBS_RUNTIME_URL="https://github.com/obsproject/obs-studio/releases/download/${OBS_RUNTIME_VERSION}/${OBS_RUNTIME_NAME}"
OBS_CACHE_ROOT="${WORK_ROOT}/dependencies/libobs"
OBS_RUNTIME_ARCHIVE="${OBS_CACHE_ROOT}/${OBS_RUNTIME_NAME}"
mkdir -p "${OBS_CACHE_ROOT}"
if [[ ! -f "${OBS_RUNTIME_ARCHIVE}" ]] || ! printf '%s  %s\n' \
    "${OBS_RUNTIME_SHA256}" "${OBS_RUNTIME_ARCHIVE}" | sha256sum --check --status; then
    OBS_DOWNLOAD="$(mktemp "${OBS_CACHE_ROOT}/.obs-download.XXXXXX")"
    if ! curl --fail --location --retry 3 --retry-delay 2 \
        --output "${OBS_DOWNLOAD}" "${OBS_RUNTIME_URL}"; then
        rm -f -- "${OBS_DOWNLOAD}"
        fail "Failed to download the pinned OBS runtime."
    fi
    if ! printf '%s  %s\n' "${OBS_RUNTIME_SHA256}" "${OBS_DOWNLOAD}" \
        | sha256sum --check --status; then
        rm -f -- "${OBS_DOWNLOAD}"
        fail "Checksum verification failed for ${OBS_RUNTIME_URL}"
    fi
    mv -f -- "${OBS_DOWNLOAD}" "${OBS_RUNTIME_ARCHIVE}"
fi
OBS_EXTRACT_ROOT="$(mktemp -d "${OBS_CACHE_ROOT}/.obs-extract.XXXXXX")"
trap 'rm -rf -- "${OBS_EXTRACT_ROOT}"' EXIT
dpkg-deb --extract "${OBS_RUNTIME_ARCHIVE}" "${OBS_EXTRACT_ROOT}"
LINUX_MUX_HELPER="${OBS_EXTRACT_ROOT}/usr/local/bin/obs-ffmpeg-mux"
[[ -s "${LINUX_MUX_HELPER}" && -x "${LINUX_MUX_HELPER}" ]] || fail \
    "The pinned OBS runtime is missing its executable mux helper."

XDOTOOL_SOURCE="$(command -v xdotool || true)"
[[ -n "${XDOTOOL_SOURCE}" ]] || fail \
    "xdotool is required for packaging. On Ubuntu run: sudo apt install xdotool"

"${PYTHON}" -c "import nuitka, PySide6, sideview" >/dev/null 2>&1 || fail \
    "Nuitka, PySide6 and SideView are required. Install requirements.txt and requirements-build.txt."

SIDEVIEW_PACKAGE_DIR="$(
    "${PYTHON}" -c \
        'from pathlib import Path; import sideview; print(Path(sideview.__file__).resolve().parent)'
)"
SIDEVIEW_NATIVE="${SIDEVIEW_PACKAGE_DIR}/libsideview_native.so"
[[ -f "${SIDEVIEW_NATIVE}" ]] || fail \
    "The installed SideView package is missing its Linux native backend: ${SIDEVIEW_NATIVE}"
"${PYTHON}" -c "from sideview._backend import NativeBackend; NativeBackend()"

if ldd "${SIDEVIEW_NATIVE}" | grep -q 'not found'; then
    ldd "${SIDEVIEW_NATIVE}" >&2
    fail "The installed SideView backend has unresolved shared-library dependencies."
fi

rm -rf "${QML_CACHE_ROOT}" "${DIST_DIR}" "${WORK_ROOT}/main.build"

"${PYTHON}" scripts/compile_qml_cache.py \
    --source-dir src/solin/qml \
    --output-dir "${QML_APP_DIR}" \
    --qt-qml-output-dir "${QML_QT_DIR}" \
    --qt-library-output-dir "${QT_LIBRARY_DIR}" \
    --qt-qml-module QtQuick/Controls/impl \
    --qt-qml-module QtQuick/Controls/Basic/impl

qml_binary_args=()
if find "${QML_QT_DIR}" -name '*.so' -print -quit | grep -q .; then
    qml_binary_args+=(
        "--include-data-files=${QML_QT_DIR}=PySide6/qml/=**/*.so"
    )
fi

"${PYTHON}" -m nuitka \
    --standalone \
    --output-dir="${WORK_ROOT}" \
    --output-filename=Solin.bin \
    --assume-yes-for-downloads \
    --follow-imports \
    --follow-import-to=solin \
    --follow-import-to=solin.core \
    --follow-import-to=solin.styles \
    --follow-import-to=solin.widgets \
    --nofollow-import-to=PySide6.QtTranslations \
    --noinclude-setuptools-mode=nofollow \
    --include-package=solin \
    --include-package=solin.core \
    --include-package=solin.styles \
    --include-package=solin.widgets \
    --include-package=sideview \
    --include-package=pylibobs \
    --include-module=websocket \
    --include-module=websocket._core \
    --include-module=websocket._app \
    --include-package=ephem \
    --include-package=keyring \
    --include-package-data=pyqttoast \
    --include-data-dir=src/solin/resources/assets=solin/resources/assets \
    --include-data-dir=src/solin/resources/remote_control=solin/resources/remote_control \
    --include-data-dir=src/solin/resources/translations/locales=solin/resources/translations/locales \
    --include-data-dir="${QML_APP_DIR}=solin/qml/Solin" \
    --include-data-dir="${QML_QT_DIR}=PySide6/qml" \
    "${qml_binary_args[@]}" \
    --include-data-files="src/solin/resources/translations/*.qm=solin/resources/translations/" \
    --enable-plugin=pyside6 \
    --include-qt-plugins=platforms,platformthemes,imageformats,position,xcbglintegrations \
    main.py

[[ -x "${DIST_DIR}/Solin.bin" ]] || fail "Nuitka did not produce ${DIST_DIR}/Solin.bin."
[[ -f "${DIST_DIR}/sideview/libsideview_native.so" ]] || fail \
    "The SideView Linux backend was not packaged."
[[ -f "${DIST_DIR}/solin/qml/Solin/qmldir" ]] || fail \
    "Compiled app QML metadata was not packaged."
find "${DIST_DIR}/solin/qml/Solin" -maxdepth 1 -name '*.qmlc' -print -quit | grep -q . || fail \
    "No compiled app QML cache was packaged."

find "${DIST_DIR}/solin/qml" -maxdepth 1 -type f \
    \( -name '*.qml' -o -name '*.qmlc' -o -name qmldir \) -delete
find "${DIST_DIR}/solin/qml/Solin" -maxdepth 1 -type f -name '*.qml' -delete

# PySide6 6.10 currently ships a TIFF image plugin linked against libtiff.so.5.
# Ubuntu 24.04 provides libtiff.so.6; omitting the optional plugin avoids an
# invalid ABI shim and keeps startup free of a misleading loader warning.
rm -f "${DIST_DIR}/PySide6/qt-plugins/imageformats/libqtiff.so"

find "${DIST_DIR}/solin/qml/Solin" -maxdepth 1 -name '*.qml' -print -quit | grep -q . && fail \
    "Raw app QML source remained in the standalone distribution."
[[ -f "${DIST_DIR}/PySide6/qml/QtQuick/qmldir" ]] || fail "QtQuick QML runtime is missing."
[[ -f "${DIST_DIR}/PySide6/qml/QtQml/qmldir" ]] || fail "QtQml QML runtime is missing."
[[ -f "${DIST_DIR}/PySide6/qml/QtQuick/Controls/libqtquickcontrols2plugin.so" ]] || fail \
    "QtQuick Controls QML plugin is missing."

find "${QT_LIBRARY_DIR}" -maxdepth 1 -type f -name 'libQt6*.so*' \
    -exec cp -f {} "${DIST_DIR}/" \;

# Qt's XCB platform plugin depends on a small set of helper libraries that are
# absent from some minimal desktop installations. Bundle only those stable XCB
# helpers; core X11, OpenGL, libc and graphics-driver libraries remain supplied
# by the host system, as required for cross-distribution compatibility.
QXCB_PLUGIN="${DIST_DIR}/PySide6/qt-plugins/platforms/libqxcb.so"
xcb_helper_libraries=(
    libxkbcommon-x11.so.0
    libxcb-cursor.so.0
    libxcb-icccm.so.4
    libxcb-util.so.1
    libxcb-image.so.0
    libxcb-keysyms.so.1
    libxcb-randr.so.0
    libxcb-render-util.so.0
    libxcb-xfixes.so.0
    libxcb-shape.so.0
    libxcb-xkb.so.1
)
for library_name in "${xcb_helper_libraries[@]}"; do
    stage_linked_library "${QXCB_PLUGIN}" "${library_name}"
done

# Zoom screen-share automation calls xdotool as an external process. Keep the
# executable in the private payload and bundle only its non-core X11 libraries;
# the launcher's private PATH makes it discoverable without modifying the host.
mkdir -p "${DIST_DIR}/bin" "${DIST_DIR}/licenses"
cp -Lf "${XDOTOOL_SOURCE}" "${DIST_DIR}/bin/xdotool"
chmod 755 "${DIST_DIR}/bin/xdotool"

xdotool_libraries=(
    libxdo.so.3
    libXtst.so.6
    libXinerama.so.1
)
for library_name in "${xdotool_libraries[@]}"; do
    stage_linked_library "${XDOTOOL_SOURCE}" "${library_name}"
done

patchelf --set-rpath "\$ORIGIN/.." "${DIST_DIR}/bin/xdotool"
patchelf --set-rpath "\$ORIGIN" "${DIST_DIR}/libxdo.so.3"

license_sources=(
    /usr/share/doc/xdotool/copyright
    /usr/share/doc/libxtst6/copyright
    /usr/share/doc/libxinerama1/copyright
)
for license_source in "${license_sources[@]}"; do
    [[ -f "${license_source}" ]] \
        || fail "Required redistribution notice was not found: ${license_source}"
    cp -f "${license_source}" \
        "${DIST_DIR}/licenses/$(basename "$(dirname "${license_source}")").txt"
done

while IFS= read -r plugin; do
    relative_root="$(realpath --relative-to="$(dirname "${plugin}")" "${DIST_DIR}")"
    patchelf --add-rpath "\$ORIGIN/${relative_root}" "${plugin}"
    unresolved="$(ldd "${plugin}" | grep 'not found' || true)"
    if [[ -n "${unresolved}" ]]; then
        printf '%s\n' "${unresolved}" >&2
        fail "Packaged QML plugin has unresolved shared-library dependencies: ${plugin}"
    fi
done < <(find "${DIST_DIR}/PySide6/qml" -type f -name '*.so' | sort)

for library_name in "${xcb_helper_libraries[@]}"; do
    [[ -f "${DIST_DIR}/${library_name}" ]] \
        || fail "Qt XCB helper was not packaged: ${library_name}"
done

for library_name in "${xdotool_libraries[@]}"; do
    packaged_library="$(resolve_linked_library "${DIST_DIR}/bin/xdotool" "${library_name}")"
    [[ -n "${packaged_library}" ]] \
        || fail "Bundled xdotool dependency was not resolved: ${library_name}"
    [[ "$(realpath "${packaged_library}")" == "$(realpath "${DIST_DIR}/${library_name}")" ]] \
        || fail "Bundled xdotool resolved ${library_name} outside the private payload."
done

cat > "${DIST_DIR}/run-solin" <<'LAUNCHER'
#!/bin/sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$APP_DIR/bin${PATH:+:$PATH}"
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-xcb}
exec "$APP_DIR/Solin.bin" "$@"
LAUNCHER
chmod 755 "${DIST_DIR}/run-solin"

if ldd "${DIST_DIR}/sideview/libsideview_native.so" | grep -q 'not found'; then
    ldd "${DIST_DIR}/sideview/libsideview_native.so" >&2
    fail "The packaged SideView backend has unresolved shared-library dependencies."
fi

"${PYTHON}" scripts/package_libobs_runtime.py \
    --application-dir "${DIST_DIR}" \
    --executable "${DIST_DIR}/Solin.bin" \
    --linux-mux-helper "${LINUX_MUX_HELPER}"

if [[ "${WORK_ROOT}" != "${OUTPUT_ROOT}" ]]; then
    rm -rf "${FINAL_DIST_DIR}"
    mkdir -p "${OUTPUT_ROOT}"
    cp -a "${DIST_DIR}" "${FINAL_DIST_DIR}"
else
    FINAL_DIST_DIR="${DIST_DIR}"
fi

printf '\nSolin Linux standalone build completed: %s\n' "${FINAL_DIST_DIR}"
printf 'Run it with: %s/run-solin\n' "${FINAL_DIST_DIR}"
