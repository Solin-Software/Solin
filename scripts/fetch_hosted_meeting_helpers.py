"""Fetch the third-party hosted-meeting helpers: Galene (built from source) and frpc.

* **Galene** (MIT) is a pure-Go program: it is built from the pinned tag with
  ``CGO_ENABLED=0``, so any platform can be cross-compiled from any host.
* **frpc** (Apache-2.0) comes from the pinned frp release on GitHub; the archive is
  verified against the release's SHA-256 checksum list.

Both land in ``build/hosted-meeting/<platform>/`` (``linux-amd64``, ``windows-amd64``,
``darwin-arm64``, …) with their licence texts under ``licenses/``, where
``solin.core.hosted_meeting.binaries`` and the packaging scripts look for them.
``solin-galene-bridge`` is built separately by ``scripts/build_galene_bridge.py`` (it
needs cgo and libopus for the target platform).

Usage::

    python scripts/fetch_hosted_meeting_helpers.py [--platform linux-amd64] [--skip-galene] [--skip-frpc]

Requirements: Go and git on PATH for Galene; network access.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform as host_platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GALENE_REPOSITORY = "https://github.com/jech/galene.git"
GALENE_TAG = "galene-1.2.1"
FRP_VERSION = "0.71.1"
FRP_BASE = f"https://github.com/fatedier/frp/releases/download/v{FRP_VERSION}"


def host_platform_key() -> str:
    system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = host_platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "solin-build"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed HTTPS URLs
        return response.read()


def build_galene(platform_key: str, output: Path) -> None:
    goos, goarch = platform_key.split("-", 1)
    exe = "galene.exe" if goos == "windows" else "galene"
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "galene"
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", GALENE_TAG, GALENE_REPOSITORY,
                        str(source)], check=True)
        env = {**os.environ, "CGO_ENABLED": "0", "GOOS": goos, "GOARCH": goarch}
        subprocess.run(["go", "build", "-trimpath", "-ldflags", "-s -w", "-o", str(output / exe), "."],
                       cwd=source, env=env, check=True)
        licenses = output / "licenses"
        licenses.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "LICENCE", licenses / "galene-LICENCE.txt")
    print(f"galene {GALENE_TAG} -> {output / exe}")


def fetch_frpc(platform_key: str, output: Path) -> None:
    goos, goarch = platform_key.split("-", 1)
    stem = f"frp_{FRP_VERSION}_{goos}_{goarch}"
    archive_name = f"{stem}.zip" if goos == "windows" else f"{stem}.tar.gz"
    checksums = _download(f"{FRP_BASE}/frp_sha256_checksums.txt").decode("utf-8")
    expected = next((line.split()[0] for line in checksums.splitlines() if line.strip().endswith(archive_name)), "")
    if not expected:
        raise SystemExit(f"no checksum for {archive_name}")
    data = _download(f"{FRP_BASE}/{archive_name}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit(f"checksum mismatch for {archive_name}: {actual} != {expected}")
    exe = "frpc.exe" if goos == "windows" else "frpc"
    licenses = output / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    if archive_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            (output / exe).write_bytes(archive.read(f"{stem}/{exe}"))
            (licenses / "frp-LICENSE.txt").write_bytes(archive.read(f"{stem}/LICENSE"))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member, target in ((f"{stem}/{exe}", output / exe), (f"{stem}/LICENSE", licenses / "frp-LICENSE.txt")):
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise SystemExit(f"{member} missing from {archive_name}")
                target.write_bytes(extracted.read())
    (output / exe).chmod(0o755)
    print(f"frpc {FRP_VERSION} -> {output / exe}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", default=host_platform_key())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--skip-galene", action="store_true")
    parser.add_argument("--skip-frpc", action="store_true")
    args = parser.parse_args()
    output = args.output_dir or REPOSITORY_ROOT / "build" / "hosted-meeting" / args.platform
    output.mkdir(parents=True, exist_ok=True)
    if not args.skip_galene:
        build_galene(args.platform, output)
    if not args.skip_frpc:
        fetch_frpc(args.platform, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
