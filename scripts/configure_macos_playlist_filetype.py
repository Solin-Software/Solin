"""Configure the native Solin playlist UTI in a built macOS app bundle."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import stat
import tempfile
from typing import Any


PLAYLIST_UTI = "com.solin.playlist"
PLAYLIST_MIME = "application/vnd.solin.playlist+zip"
PLAYLIST_EXTENSION = "solinplaylist"


def configure_bundle(bundle: Path, *, icon_name: str = "playlist.icns") -> None:
    """Atomically add or replace Solin's document declaration in Info.plist."""
    plist_path = bundle / "Contents" / "Info.plist"
    icon_path = bundle / "Contents" / "Resources" / icon_name
    if not plist_path.is_file():
        raise FileNotFoundError(f"App bundle Info.plist not found: {plist_path}")
    if not icon_path.is_file() or icon_path.stat().st_size <= 0:
        raise FileNotFoundError(f"Playlist document icon not found: {icon_path}")

    with plist_path.open("rb") as stream:
        plist = plistlib.load(stream)
    if not isinstance(plist, dict):
        raise ValueError("App bundle Info.plist root must be a dictionary")

    document_type = {
        "CFBundleTypeName": "Solin Playlist",
        "CFBundleTypeRole": "Editor",
        "CFBundleTypeIconFile": icon_name,
        "CFBundleTypeExtensions": [PLAYLIST_EXTENSION],
        "LSHandlerRank": "Owner",
        "LSItemContentTypes": [PLAYLIST_UTI],
    }
    exported_type = {
        "UTTypeIdentifier": PLAYLIST_UTI,
        "UTTypeDescription": "Solin Playlist",
        "UTTypeConformsTo": ["public.zip-archive"],
        "UTTypeIconFile": icon_name,
        "UTTypeTagSpecification": {
            "public.filename-extension": [PLAYLIST_EXTENSION],
            "public.mime-type": [PLAYLIST_MIME],
        },
    }
    plist["CFBundleDocumentTypes"] = _replace_document_type(
        plist.get("CFBundleDocumentTypes"), document_type
    )
    plist["UTExportedTypeDeclarations"] = _replace_exported_type(
        plist.get("UTExportedTypeDeclarations"), exported_type
    )
    _write_plist_atomically(plist_path, plist)


def _replace_document_type(value: object, replacement: dict[str, Any]) -> list[object]:
    entries = list(value) if isinstance(value, list) else []
    retained = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and (
                PLAYLIST_UTI in entry.get("LSItemContentTypes", [])
                or PLAYLIST_EXTENSION in entry.get("CFBundleTypeExtensions", [])
            )
        )
    ]
    return [*retained, replacement]


def _replace_exported_type(value: object, replacement: dict[str, Any]) -> list[object]:
    entries = list(value) if isinstance(value, list) else []
    retained = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and entry.get("UTTypeIdentifier") == PLAYLIST_UTI
        )
    ]
    return [*retained, replacement]


def _write_plist_atomically(path: Path, value: dict[str, Any]) -> None:
    original_mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, original_mode)
        os.replace(temp_path, path)
    except Exception:  # noqa: BLE001 - atomic build-artifact cleanup boundary
        temp_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register .solinplaylist in a built macOS app bundle."
    )
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--icon-name", default="playlist.icns")
    args = parser.parse_args()
    configure_bundle(args.bundle, icon_name=args.icon_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
