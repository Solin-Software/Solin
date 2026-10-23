"""Stage the hosted-meeting helpers into a packaged Solin application.

Copies ``galene``, ``frpc`` and ``solin-galene-bridge`` (plus their licence texts) from
``build/hosted-meeting/<platform>/`` into ``<application-dir>/bin`` and
``<application-dir>/licenses/hosted-meeting``, where
``solin.core.hosted_meeting.binaries`` finds them in compiled builds. Missing helpers are
produced first with ``scripts/fetch_hosted_meeting_helpers.py`` (Galene, frpc) and
``scripts/build_galene_bridge.py`` (the bridge; needs cgo + libopus for the platform).

Usage::

    python scripts/stage_hosted_meeting_helpers.py --application-dir dist/main.dist [--platform linux-amd64]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from fetch_hosted_meeting_helpers import host_platform_key  # noqa: E402

HELPERS = ("galene", "frpc", "solin-galene-bridge")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--platform", default=host_platform_key())
    parser.add_argument("--no-build", action="store_true", help="fail instead of building missing helpers")
    args = parser.parse_args()

    suffix = ".exe" if args.platform.startswith("windows") else ""
    source = REPOSITORY_ROOT / "build" / "hosted-meeting" / args.platform
    names = {helper: f"{helper}{suffix}" for helper in HELPERS}
    if not args.no_build:
        if not (source / names["galene"]).is_file() or not (source / names["frpc"]).is_file():
            subprocess.run([sys.executable, str(REPOSITORY_ROOT / "scripts" / "fetch_hosted_meeting_helpers.py"),
                            "--platform", args.platform, "--output-dir", str(source)], check=True)
        if not (source / names["solin-galene-bridge"]).is_file():
            subprocess.run([sys.executable, str(REPOSITORY_ROOT / "scripts" / "build_galene_bridge.py"),
                            "--output-dir", str(source)], check=True)
    missing = [name for name in names.values() if not (source / name).is_file()]
    if missing:
        raise SystemExit(f"hosted-meeting helpers missing in {source}: {', '.join(missing)}")

    bin_dir = args.application_dir / "bin"
    licenses = args.application_dir / "licenses" / "hosted-meeting"
    bin_dir.mkdir(parents=True, exist_ok=True)
    licenses.mkdir(parents=True, exist_ok=True)
    for name in names.values():
        target = bin_dir / name
        shutil.copy2(source / name, target)
        target.chmod(0o755)
        print(f"staged {target}")
    for licence in sorted((source / "licenses").glob("*")):
        shutil.copy2(licence, licenses / licence.name)
    bridge_licence = REPOSITORY_ROOT / "tools" / "solin-galene-bridge" / "THIRD_PARTY.md"
    if bridge_licence.is_file():
        shutil.copy2(bridge_licence, licenses / "solin-galene-bridge-THIRD_PARTY.md")
    web_licence = REPOSITORY_ROOT / "src" / "solin" / "resources" / "hosted_meeting_web" / "protocol.LICENSE.txt"
    if web_licence.is_file():
        shutil.copy2(web_licence, licenses / "galene-protocol-js-LICENCE.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
