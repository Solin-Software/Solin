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
NATIVE_WEBVIEW="${PROJECT_ROOT}/src/native_webview_widget/libnative_webview_widget.so"

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
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
import sys

if sys.version_info < (3, 13):
    raise SystemExit(
        f"Solin requires Python 3.13 or newer; found {sys.version.split()[0]}. "
        "Set SOLIN_PYTHON to a compatible Linux interpreter."
    )
PY

for command_name in gcc patchelf readelf; do
    command -v "${command_name}" >/dev/null || fail \
        "${command_name} is required. On Ubuntu run: sudo apt install binutils build-essential patchelf"
done

"${PYTHON}" -c "import nuitka, PySide6" >/dev/null 2>&1 || fail \
    "Nuitka and PySide6 are required. Install requirements and run: python -m pip install nuitka ordered-set zstandard"

[[ -f "${NATIVE_WEBVIEW}" ]] || fail "Missing Linux sideview artifact: ${NATIVE_WEBVIEW}"
"${PYTHON}" scripts/validate_native_webview.py --require-linux

if ldd "${NATIVE_WEBVIEW}" | grep -q 'not found'; then
    ldd "${NATIVE_WEBVIEW}" >&2
    fail "The Linux sideview artifact has unresolved shared-library dependencies."
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
    --include-package=native_webview_widget \
    --include-module=websocket \
    --include-module=websocket._core \
    --include-module=websocket._app \
    --include-package=ephem \
    --include-package-data=pyqttoast \
    --include-data-dir=src/solin/resources/assets=solin/resources/assets \
    --include-data-dir=src/solin/resources/translations/locales=solin/resources/translations/locales \
    --include-data-dir="${QML_APP_DIR}=solin/qml/Solin" \
    --include-data-dir="${QML_QT_DIR}=PySide6/qml" \
    "${qml_binary_args[@]}" \
    --include-data-files="src/solin/resources/translations/*.qm=solin/resources/translations/" \
    --enable-plugin=pyside6 \
    --include-qt-plugins=platforms,platformthemes,imageformats,multimedia,position,xcbglintegrations \
    main.py

[[ -x "${DIST_DIR}/Solin.bin" ]] || fail "Nuitka did not produce ${DIST_DIR}/Solin.bin."
[[ -f "${DIST_DIR}/native_webview_widget/libnative_webview_widget.so" ]] || fail \
    "The Linux sideview artifact was not packaged."
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
[[ -f "${DIST_DIR}/PySide6/qml/QtMultimedia/libquickmultimediaplugin.so" ]] || fail \
    "QtMultimedia QML plugin is missing."
[[ -f "${DIST_DIR}/PySide6/qml/QtQuick/Controls/libqtquickcontrols2plugin.so" ]] || fail \
    "QtQuick Controls QML plugin is missing."

find "${QT_LIBRARY_DIR}" -maxdepth 1 -type f -name 'libQt6*.so*' \
    -exec cp -f {} "${DIST_DIR}/" \;

while IFS= read -r plugin; do
    relative_root="$(realpath --relative-to="$(dirname "${plugin}")" "${DIST_DIR}")"
    patchelf --add-rpath "\$ORIGIN/${relative_root}" "${plugin}"
    unresolved="$(ldd "${plugin}" | grep 'not found' || true)"
    if [[ -n "${unresolved}" ]]; then
        printf '%s\n' "${unresolved}" >&2
        fail "Packaged QML plugin has unresolved shared-library dependencies: ${plugin}"
    fi
done < <(find "${DIST_DIR}/PySide6/qml" -type f -name '*.so' | sort)

cat > "${DIST_DIR}/run-solin" <<'LAUNCHER'
#!/bin/sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-xcb}
export WEBKIT_DISABLE_DMABUF_RENDERER=${WEBKIT_DISABLE_DMABUF_RENDERER:-1}
exec "$APP_DIR/Solin.bin" "$@"
LAUNCHER
chmod 755 "${DIST_DIR}/run-solin"

if ldd "${DIST_DIR}/native_webview_widget/libnative_webview_widget.so" | grep -q 'not found'; then
    ldd "${DIST_DIR}/native_webview_widget/libnative_webview_widget.so" >&2
    fail "Packaged sideview artifact has unresolved shared-library dependencies."
fi

if [[ "${WORK_ROOT}" != "${OUTPUT_ROOT}" ]]; then
    rm -rf "${FINAL_DIST_DIR}"
    mkdir -p "${OUTPUT_ROOT}"
    cp -a "${DIST_DIR}" "${FINAL_DIST_DIR}"
else
    FINAL_DIST_DIR="${DIST_DIR}"
fi

printf '\nSolin Linux standalone build completed: %s\n' "${FINAL_DIST_DIR}"
printf 'Run it with: %s/run-solin\n' "${FINAL_DIST_DIR}"
