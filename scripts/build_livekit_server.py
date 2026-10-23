"""Build the pinned LiveKit server from source (macOS has no official release binary).

    python scripts/build_livekit_server.py                          # this machine
    python scripts/build_livekit_server.py --goos darwin --goarch arm64 --dest dist/bin

Clones the release tag, refuses to build unless it resolves to the pinned commit, and
compiles ``./cmd/server``. Linux and Windows builds are CGO-free like LiveKit's releases;
macOS needs cgo (its CPU statistics come from the system frameworks), so build macOS on
macOS. Requires Go (see the tag's go.mod) and git on PATH.
Default destination: build/livekit/<platform>/ (where development checkouts look).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from solin.core.conference.server_binary import LIVEKIT_SERVER_VERSION  # noqa: E402

SOURCE = "https://github.com/livekit/livekit.git"
# The commit the v1.13.10 tag points to (a moved tag must not change what we ship).
COMMIT = "e5655ac20267fa5005dccaf3f0e3bfc3ea688c07"
_GOARCH = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


def _run(*command: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed:\n{result.stderr[-4000:]}")
    return result.stdout.strip()


def build(dest: Path, goos: str, goarch: str, license_path: Path | None = None) -> Path:
    if goos == "darwin" and sys.platform != "darwin":
        raise SystemExit("The macOS server needs cgo: build it on macOS")
    with tempfile.TemporaryDirectory(prefix="livekit-src-") as work:
        source = Path(work) / "livekit"
        _run("git", "clone", "--quiet", "--depth", "1", "--branch",
             f"v{LIVEKIT_SERVER_VERSION}", SOURCE, str(source))
        head = _run("git", "rev-parse", "HEAD", cwd=source)
        if head != COMMIT:
            raise SystemExit(f"v{LIVEKIT_SERVER_VERSION} resolves to {head}, expected {COMMIT}")
        environment = {**os.environ, "CGO_ENABLED": "1" if goos == "darwin" else "0",
                       "GOOS": goos, "GOARCH": goarch}
        name = "livekit-server.exe" if goos == "windows" else "livekit-server"
        dest.mkdir(parents=True, exist_ok=True)
        output = dest / name
        _run("go", "build", "-trimpath", "-ldflags", "-s -w", "-o", str(output), "./cmd/server",
             cwd=source, env=environment)
        output.chmod(0o755)
        if license_path is not None:
            license_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / "LICENSE", license_path)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--goos", default={"darwin": "darwin", "win32": "windows"}.get(sys.platform, "linux"))
    parser.add_argument("--goarch", default=_GOARCH.get(platform.machine().casefold(), "amd64"))
    parser.add_argument("--dest", type=Path, default=None)
    parser.add_argument("--license", type=Path, default=None)
    args = parser.parse_args()
    dest = args.dest or REPO_ROOT / "build" / "livekit" / f"{args.goos}-{args.goarch}"
    path = build(dest, args.goos, args.goarch, args.license)
    print(f"livekit-server {LIVEKIT_SERVER_VERSION} ({args.goos}/{args.goarch}) -> {path}")


if __name__ == "__main__":
    main()
