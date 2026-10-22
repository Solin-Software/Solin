"""Download the pinned LiveKit browser client (npm ``livekit-client``) and verify it.

    python scripts/fetch_livekit_client.py                       # build/livekit-client/
    python scripts/fetch_livekit_client.py --dest dist/web/livekit-client --license dist/licenses/livekit-client.txt

Only the UMD bundle and the end-to-end encryption worker are extracted.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from solin.core.conference.web_client import CLIENT_FILES, LIVEKIT_CLIENT_VERSION  # noqa: E402

# sha256 of the registry tarball (its npm integrity is
# sha512-j9Kg86nJjBjNFUD+ukp2jwMCGa0k+YD5gU//RzAM+ZY3pbG631QCte7fQjwqWMmtBzhDJL9nKYuOLMv/9IbAxg==).
SHA256 = "993eafb83afb4c566bb388108ad6469bf499d76200014c96c35fe3b39fee859d"
URL = (
    f"https://registry.npmjs.org/livekit-client/-/livekit-client-{LIVEKIT_CLIENT_VERSION}.tgz"
)


def fetch(dest: Path, license_path: Path | None = None) -> None:
    with urllib.request.urlopen(URL, timeout=120) as response:  # noqa: S310 - pinned https URL
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"Checksum mismatch for {URL}: {digest}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for name in CLIENT_FILES:
            member = archive.extractfile(f"package/dist/{name}")
            if member is None:
                raise SystemExit(f"{name} missing from {URL}")
            (dest / name).write_bytes(member.read())
        if license_path is not None:
            notice = archive.extractfile("package/LICENSE")
            if notice is None:
                raise SystemExit(f"LICENSE missing from {URL}")
            license_path.parent.mkdir(parents=True, exist_ok=True)
            license_path.write_bytes(notice.read())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dest", type=Path, default=REPO_ROOT / "build" / "livekit-client")
    parser.add_argument("--license", type=Path, default=None)
    args = parser.parse_args()
    fetch(args.dest, args.license)
    print(f"livekit-client {LIVEKIT_CLIENT_VERSION} -> {args.dest}")


if __name__ == "__main__":
    main()
