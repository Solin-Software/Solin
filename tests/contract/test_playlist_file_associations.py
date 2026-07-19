from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys
from xml.etree import ElementTree

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
MIME_TYPE = "application/vnd.solin.playlist+zip"


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_playlist_file_icons_are_multiresolution_and_reproducible():
    assets = ROOT / "src" / "solin" / "resources" / "assets"

    assert (assets / "playlist.svg").is_file()
    with Image.open(assets / "playlist.ico") as icon:
        assert icon.info["sizes"] == {
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (96, 96),
            (128, 128),
            (256, 256),
        }
    with Image.open(assets / "playlist-512.png") as icon:
        assert icon.size == (512, 512)
        assert icon.mode == "RGBA"
    with Image.open(assets / "playlist-1024.png") as icon:
        assert icon.size == (1024, 1024)
        assert icon.mode == "RGBA"

    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    subprocess.run(
        [sys.executable, "scripts/generate_playlist_file_icons.py", "--check"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_windows_installers_register_native_playlist_without_forcing_user_choice():
    setup = _read("packaging/windows/installer/setup.iss")
    patch = _read("packaging/windows/installer/patch.iss")
    workflow = _read(".github/workflows/build-solin-windows.yml")

    for installer in (setup, patch):
        assert "ChangesAssociations=yes" in installer
        assert '"application/vnd.solin.playlist+zip"' in installer
        assert '"Solin.Playlist"' in installer
        assert "Software\\Classes\\.solinplaylist\\OpenWithProgids" in installer
        assert "Software\\Solin\\Capabilities\\FileAssociations" in installer
        assert "Software\\RegisteredApplications" in installer
        assert "playlist.ico,0" in installer
        assert '"""{app}\\{#MyAppExeName}"" ""%1"""' in installer
        assert "Software\\Classes\\.solinplaylist\"; ValueType: string; ValueName: \"\"" not in installer
        assert ".jwlplaylist" not in installer.lower()

    assert "Root: HKCU" in patch
    assert "Check: IsPatchUserInstall" in patch
    assert "Root: HKLM" in patch
    assert "Check: IsPatchMachineInstall" in patch
    assert '"packaging\\windows\\installer\\setup.iss"' in workflow


def test_linux_appimage_declares_and_packages_native_playlist_mime_type():
    desktop = _read("packaging/linux/appimage/com.solin.Solin.desktop")
    package_script = _read("scripts/package_solin_appimage.sh")
    appstream = _read("packaging/linux/appimage/com.solin.Solin.appdata.xml")
    mime_path = ROOT / "packaging/linux/appimage/application-vnd.solin.playlist+zip.xml"
    workflow = _read(".github/workflows/build-solin-linux.yml")

    assert "Exec=solin %F" in desktop
    assert f"MimeType={MIME_TYPE};" in desktop
    assert f"<mimetype>{MIME_TYPE}</mimetype>" in appstream
    assert "usr/share/mime/packages" in package_script
    assert "usr/share/icons/hicolor/512x512/mimetypes" in package_script
    assert "usr/share/icons/hicolor/scalable/mimetypes" in package_script
    assert "update-mime-database" in package_script
    assert "playlist-512.png" in package_script
    assert "shared-mime-info" in workflow
    assert "scripts/package_solin_appimage.sh" in workflow

    root = ElementTree.parse(mime_path).getroot()
    namespace = {"mime": "http://www.freedesktop.org/standards/shared-mime-info"}
    mime = root.find("mime:mime-type", namespace)
    assert mime is not None
    assert mime.attrib["type"] == MIME_TYPE
    glob = mime.find("mime:glob", namespace)
    assert glob is not None
    assert glob.attrib["pattern"] == "*.solinplaylist"


def test_macos_bundle_exports_native_playlist_uti_with_document_icon():
    workflow = _read(".github/workflows/build-solin-macos.yml")

    assert "Register Solin playlist document type" in workflow
    assert "scripts/configure_macos_playlist_filetype.py" in workflow
    assert 'iconutil --convert icns --output "$RESOURCES/playlist.icns"' in workflow
    assert "plutil -lint" in workflow
    assert workflow.index("Register Solin playlist document type") < workflow.index(
        "Ad-hoc sign app bundle"
    )


def test_macos_bundle_configuration_preserves_other_document_types(tmp_path: Path):
    bundle = tmp_path / "Solin.app"
    contents = bundle / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True)
    (resources / "playlist.icns").write_bytes(b"icns")
    plist_path = contents / "Info.plist"
    with plist_path.open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.solin.Solin",
                "CFBundleDocumentTypes": [
                    {
                        "CFBundleTypeName": "Existing",
                        "LSItemContentTypes": ["public.example"],
                    }
                ],
            },
            stream,
        )

    subprocess.run(
        [
            sys.executable,
            "scripts/configure_macos_playlist_filetype.py",
            "--bundle",
            os.fspath(bundle),
        ],
        cwd=ROOT,
        check=True,
    )

    with plist_path.open("rb") as stream:
        plist = plistlib.load(stream)
    assert plist["CFBundleIdentifier"] == "com.solin.Solin"
    assert plist["CFBundleDocumentTypes"][0]["CFBundleTypeName"] == "Existing"
    playlist_document = plist["CFBundleDocumentTypes"][-1]
    assert playlist_document["LSItemContentTypes"] == ["com.solin.playlist"]
    assert playlist_document["CFBundleTypeExtensions"] == ["solinplaylist"]
    assert playlist_document["CFBundleTypeIconFile"] == "playlist.icns"
    assert playlist_document["LSHandlerRank"] == "Owner"
    exported_type = plist["UTExportedTypeDeclarations"][-1]
    assert exported_type["UTTypeIdentifier"] == "com.solin.playlist"
    assert exported_type["UTTypeConformsTo"] == ["public.zip-archive"]
    assert exported_type["UTTypeTagSpecification"] == {
        "public.filename-extension": ["solinplaylist"],
        "public.mime-type": [MIME_TYPE],
    }
