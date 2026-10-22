"""Download the pinned LiveKit server release and verify its checksum.

    python scripts/fetch_livekit_server.py                 # this machine's platform
    python scripts/fetch_livekit_server.py --platform windows-amd64 --dest dist/bin

Default destination: build/livekit/<platform>/ (where development checkouts look).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from solin.core.conference.server_binary import (  # noqa: E402
    LIVEKIT_SERVER_VERSION,
    executable_name,
    platform_key,
)

# From the release's checksums.txt (https://github.com/livekit/livekit/releases).
SHA256 = {
    "linux-amd64": "c25a3e45fb922e9a93c6443d10c0ed041ba64342097e38632331736630283165",
    "linux-arm64": "ff60607e1931ff078576882fa11c087aa5821d635ac0c47f56ff77e203e32006",
    "windows-amd64": "fd4b7c2c3c1e4c71adaa4f6a046ad1c0abaaaf1e09fae904b35bf231ffae11d2",
    "windows-arm64": "887587684dae37cd21db8a16c1bae0647a48868bfaf49f6dc4e5972de4d850f1",
}
BASE_URL = "https://github.com/livekit/livekit/releases/download"


def asset_name(target: str) -> str:
    system, arch = target.split("-")
    suffix = "zip" if system == "windows" else "tar.gz"
    return f"livekit_{LIVEKIT_SERVER_VERSION}_{system}_{arch}.{suffix}"


def fetch(target: str, dest: Path, license_path: Path | None = None) -> Path:
    if target not in SHA256:
        raise SystemExit(f"No pinned LiveKit {LIVEKIT_SERVER_VERSION} build for {target}")
    url = f"{BASE_URL}/v{LIVEKIT_SERVER_VERSION}/{asset_name(target)}"
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - pinned https URL
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SHA256[target]:
        raise SystemExit(f"Checksum mismatch for {url}: {digest}")
    name = executable_name("win32" if target.startswith("windows") else "linux")
    if target.startswith("windows"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            data = archive.read(name)
            notice = archive.read("LICENSE")
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = archive.extractfile(name)
            license_member = archive.extractfile("LICENSE")
            if member is None or license_member is None:
                raise SystemExit(f"{name} or LICENSE missing from {url}")
            data = member.read()
            notice = license_member.read()
    if license_path is not None:
        license_path.parent.mkdir(parents=True, exist_ok=True)
        license_path.write_bytes(notice)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--platform", default="", help="e.g. linux-amd64, windows-amd64 (default: this machine)"
    )
    parser.add_argument("--dest", type=Path, default=None)
    parser.add_argument(
        "--license", type=Path, default=None, help="also write the Apache-2.0 LICENSE here"
    )
    args = parser.parse_args()
    target = args.platform or platform_key()
    dest = args.dest or REPO_ROOT / "build" / "livekit" / target
    path = fetch(target, dest, args.license)
    print(f"LiveKit server {LIVEKIT_SERVER_VERSION} ({target}) -> {path}")


if __name__ == "__main__":
    main()
