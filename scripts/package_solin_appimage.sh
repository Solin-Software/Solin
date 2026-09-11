#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
BUILD_ROOT="$(realpath -m -- "${SOLIN_BUILD_DIR:-${PROJECT_ROOT}/build/linux}")"
OUTPUT_ROOT="$(realpath -m -- "${SOLIN_APPIMAGE_OUTPUT_DIR:-${PROJECT_ROOT}/dist}")"
TEMPLATE_ROOT="${PROJECT_ROOT}/packaging/linux/appimage"

APPIMAGETOOL_VERSION="1.9.1"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-x86_64.AppImage"
TYPE2_RUNTIME_COMMIT="75849dce7cc37e4319b633df1f116ca895c71a12"
TYPE2_RUNTIME_SHA256="1cc49bcf1e2ccd593c379adb17c9f85a36d619088296504de95b1d06215aebbf"
TYPE2_RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64"

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
    command -v python3 || fail "Python was not found. Set SOLIN_PYTHON."
}

download_verified() {
    local url="$1"
    local expected_sha256="$2"
    local destination="$3"
    local temporary="${destination}.download"

    if [[ -f "${destination}" ]] && printf '%s  %s\n' \
        "${expected_sha256}" "${destination}" | sha256sum --check --status; then
        return
    fi

    rm -f -- "${temporary}"
    curl --fail --location --retry 3 --retry-delay 2 \
        --output "${temporary}" "${url}"
    printf '%s  %s\n' "${expected_sha256}" "${temporary}" \
        | sha256sum --check --status \
        || fail "Checksum verification failed for ${url}"
    mv -f -- "${temporary}" "${destination}"
    chmod 755 "${destination}"
}

[[ "$(uname -s)" == "Linux" ]] || fail "AppImage packaging must run on Linux or WSL 2."
[[ "$(uname -m)" == "x86_64" ]] || fail "Only x86_64 AppImages are currently supported."

for command_name in awk cp curl file ldd realpath sha256sum update-mime-database; do
    command -v "${command_name}" >/dev/null || fail "Missing required command: ${command_name}"
done

PYTHON="$(resolve_python)"
cd "${PROJECT_ROOT}"

APP_VERSION="$("${PYTHON}" - <<'PY'
from solin.version import VERSION
from solin.core.releases.version import ReleaseVersion
version = ReleaseVersion.parse(VERSION)
if version is None:
    raise SystemExit("Invalid release version")
print(version.display_version)
PY
)"

if [[ -n "${SOURCE_DATE_EPOCH:-}" ]]; then
    RELEASE_EPOCH="${SOURCE_DATE_EPOCH}"
elif command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    RELEASE_EPOCH="$(git log -1 --format=%ct)"
else
    RELEASE_EPOCH="$(date +%s)"
fi
[[ "${RELEASE_EPOCH}" =~ ^[0-9]+$ ]] || fail "Invalid release epoch: ${RELEASE_EPOCH}"
RELEASE_DATE="$(date --utc --date="@${RELEASE_EPOCH}" +%F)"

STANDALONE_DIR="${SOLIN_STANDALONE_DIR:-${BUILD_ROOT}/main.dist}"
if [[ "${SOLIN_APPIMAGE_SKIP_BUILD:-0}" != "1" ]]; then
    if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
        STANDALONE_WORK_ROOT="${SOLIN_WORK_DIR:-${HOME}/.cache/solin/build-linux}"
        SOLIN_WORK_DIR="${STANDALONE_WORK_ROOT}" \
            SOLIN_BUILD_DIR="${BUILD_ROOT}" \
            SOLIN_PYTHON="${PYTHON}" \
            bash "${SCRIPT_DIR}/build_solin.sh"
        STANDALONE_DIR="${STANDALONE_WORK_ROOT}/main.dist"
    else
        SOLIN_BUILD_DIR="${BUILD_ROOT}" SOLIN_PYTHON="${PYTHON}" \
            bash "${SCRIPT_DIR}/build_solin.sh"
    fi
fi

STANDALONE_DIR="$(realpath -m -- "${STANDALONE_DIR}")"
[[ -x "${STANDALONE_DIR}/Solin.bin" ]] \
    || fail "Standalone build not found at ${STANDALONE_DIR}"

if [[ -n "${SOLIN_APPIMAGE_WORK_DIR:-}" ]]; then
    WORK_ROOT="$(realpath -m -- "${SOLIN_APPIMAGE_WORK_DIR}")"
elif grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
    WORK_ROOT="${HOME}/.cache/solin/appimage"
else
    WORK_ROOT="${BUILD_ROOT}/appimage"
fi
[[ -n "${WORK_ROOT}" && "${WORK_ROOT}" != "/" ]] || fail "Unsafe AppImage work path."

APPDIR="${WORK_ROOT}/Solin.AppDir"
WORK_OUTPUT="${WORK_ROOT}/Solin-${APP_VERSION}-linux-x86_64.AppImage"
FINAL_OUTPUT="${OUTPUT_ROOT}/Solin-${APP_VERSION}-linux-x86_64.AppImage"
TOOL_CACHE="${HOME}/.cache/solin/tools"
APPIMAGETOOL="${SOLIN_APPIMAGETOOL:-${TOOL_CACHE}/appimagetool-${APPIMAGETOOL_VERSION}-x86_64.AppImage}"
TYPE2_RUNTIME="${SOLIN_APPIMAGE_RUNTIME:-${TOOL_CACHE}/runtime-x86_64-${TYPE2_RUNTIME_COMMIT}}"

mkdir -p -- "${WORK_ROOT}" "${OUTPUT_ROOT}" "${TOOL_CACHE}"
case "${APPDIR}" in
    "${WORK_ROOT}"/*) ;;
    *) fail "AppDir escaped the configured work directory." ;;
esac
rm -rf -- "${APPDIR}"
rm -f -- "${WORK_OUTPUT}"

mkdir -p \
    "${APPDIR}/usr/bin" \
    "${APPDIR}/usr/lib/solin" \
    "${APPDIR}/usr/share/applications" \
    "${APPDIR}/usr/share/icons/hicolor/512x512/apps" \
    "${APPDIR}/usr/share/icons/hicolor/512x512/mimetypes" \
    "${APPDIR}/usr/share/icons/hicolor/scalable/mimetypes" \
    "${APPDIR}/usr/share/mime/packages" \
    "${APPDIR}/usr/share/metainfo"
cp -a "${STANDALONE_DIR}/." "${APPDIR}/usr/lib/solin/"
install -m 755 "${TEMPLATE_ROOT}/AppRun" "${APPDIR}/AppRun"
install -m 755 "${TEMPLATE_ROOT}/solin" "${APPDIR}/usr/bin/solin"
install -m 644 "${TEMPLATE_ROOT}/com.solin.Solin.desktop" \
    "${APPDIR}/usr/share/applications/com.solin.Solin.desktop"
install -m 644 "${TEMPLATE_ROOT}/application-vnd.solin.playlist+zip.xml" \
    "${APPDIR}/usr/share/mime/packages/application-vnd.solin.playlist+zip.xml"
install -m 644 "${PROJECT_ROOT}/src/solin/resources/assets/playlist-512.png" \
    "${APPDIR}/usr/share/icons/hicolor/512x512/mimetypes/application-vnd.solin.playlist+zip.png"
install -m 644 "${PROJECT_ROOT}/src/solin/resources/assets/playlist.svg" \
    "${APPDIR}/usr/share/icons/hicolor/scalable/mimetypes/application-vnd.solin.playlist+zip.svg"
update-mime-database "${APPDIR}/usr/share/mime"
sed -e "s/@VERSION@/${APP_VERSION}/g" \
    -e "s/@RELEASE_DATE@/${RELEASE_DATE}/g" \
    "${TEMPLATE_ROOT}/com.solin.Solin.appdata.xml" \
    > "${APPDIR}/usr/share/metainfo/com.solin.Solin.appdata.xml"

"${PYTHON}" - "${PROJECT_ROOT}/src/solin/resources/assets/icon.icns" \
    "${APPDIR}/usr/share/icons/hicolor/512x512/apps/com.solin.Solin.png" <<'PY'
from pathlib import Path
import sys

from PIL import Image

source = Image.open(Path(sys.argv[1]))
image = source.convert("RGBA")
image.thumbnail((512, 512), Image.Resampling.LANCZOS)
canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
canvas.alpha_composite(image, ((512 - image.width) // 2, (512 - image.height) // 2))
canvas.save(Path(sys.argv[2]), format="PNG", optimize=True)
PY

ln -s usr/share/applications/com.solin.Solin.desktop \
    "${APPDIR}/com.solin.Solin.desktop"
ln -s usr/share/icons/hicolor/512x512/apps/com.solin.Solin.png \
    "${APPDIR}/com.solin.Solin.png"
ln -s com.solin.Solin.png "${APPDIR}/.DirIcon"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate \
        "${APPDIR}/usr/share/applications/com.solin.Solin.desktop"
fi
if command -v appstreamcli >/dev/null 2>&1; then
    appstreamcli validate --no-net \
        "${APPDIR}/usr/share/metainfo/com.solin.Solin.appdata.xml"
fi

if [[ -z "${SOLIN_APPIMAGETOOL:-}" ]]; then
    download_verified "${APPIMAGETOOL_URL}" "${APPIMAGETOOL_SHA256}" "${APPIMAGETOOL}"
fi
if [[ -z "${SOLIN_APPIMAGE_RUNTIME:-}" ]]; then
    download_verified "${TYPE2_RUNTIME_URL}" "${TYPE2_RUNTIME_SHA256}" "${TYPE2_RUNTIME}"
fi
[[ -x "${APPIMAGETOOL}" ]] || fail "appimagetool is not executable: ${APPIMAGETOOL}"
[[ -f "${TYPE2_RUNTIME}" ]] || fail "AppImage runtime not found: ${TYPE2_RUNTIME}"

appimagetool_args=(--runtime-file "${TYPE2_RUNTIME}")
if [[ -n "${SOLIN_APPIMAGE_UPDATE_INFORMATION:-}" ]]; then
    appimagetool_args+=(
        --updateinformation "${SOLIN_APPIMAGE_UPDATE_INFORMATION}"
    )
fi
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "${APPIMAGETOOL}" \
    "${appimagetool_args[@]}" "${APPDIR}" "${WORK_OUTPUT}"

[[ -x "${WORK_OUTPUT}" ]] || fail "appimagetool did not create ${WORK_OUTPUT}"
file "${WORK_OUTPUT}" | grep -q 'ELF 64-bit' || fail "Output is not a 64-bit AppImage."
cp -f -- "${WORK_OUTPUT}" "${FINAL_OUTPUT}"
chmod 755 "${FINAL_OUTPUT}"
sha256sum "${FINAL_OUTPUT}" > "${FINAL_OUTPUT}.sha256"

printf '\nSolin AppImage completed: %s\n' "${FINAL_OUTPUT}"
printf 'SHA-256: %s.sha256\n' "${FINAL_OUTPUT}"
