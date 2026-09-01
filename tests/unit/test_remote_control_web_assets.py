from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from solin.core.remote_control.web_assets import RemoteControlWebAssets


def _asset_tree(root: Path, *, app_source: str = "window.shell = 'first';") -> Path:
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "icons").mkdir()
    (root / "index.html").write_text(
        "<!doctype html>\n"
        '<link rel="manifest" href="./manifest.webmanifest">\n'
        '<link rel="icon" href="./icons/icon.png">\n'
        '<script type="module" src="./scripts/app.js"></script>\n'
        '<a href="./trust-certificate.cer">Certificate</a>\n',
        encoding="utf-8",
    )
    (root / "service-worker.js").write_text(
        'const revision = "__SOLIN_REMOTE_SHELL_REVISION__";\n'
        "const resources = __SOLIN_REMOTE_SHELL_RESOURCES__;\n",
        encoding="utf-8",
    )
    (root / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    (root / "scripts" / "app.js").write_text(app_source, encoding="utf-8")
    (root / "icons" / "icon.png").write_bytes(b"png")
    (root / "README.md").write_text("not part of the shell", encoding="utf-8")
    return root


def _resources(worker: str) -> list[str]:
    match = re.search(r"const resources = (?P<resources>\[.*?\]);", worker, re.DOTALL)
    assert match is not None
    return json.loads(match.group("resources"))


def test_shell_revision_is_content_and_installation_scoped(tmp_path: Path) -> None:
    first_root = _asset_tree(tmp_path / "first")
    identical_root = _asset_tree(tmp_path / "identical")
    changed_root = _asset_tree(tmp_path / "changed", app_source="window.shell = 'second';")

    first = RemoteControlWebAssets(
        first_root,
        installation_id="installation-a",
        url_prefix="/remote",
    )
    identical = RemoteControlWebAssets(
        identical_root,
        installation_id="installation-a",
        url_prefix="/remote",
    )
    another_installation = RemoteControlWebAssets(
        identical_root,
        installation_id="installation-b",
        url_prefix="/remote",
    )
    changed = RemoteControlWebAssets(
        changed_root,
        installation_id="installation-a",
        url_prefix="/remote",
    )

    assert re.fullmatch(r"[a-f0-9]{32}", first.revision)
    assert identical.revision == first.revision
    assert another_installation.revision != first.revision
    assert changed.revision != first.revision


def test_rendered_shell_uses_versioned_assets_and_keeps_dynamic_routes_stable(
    tmp_path: Path,
) -> None:
    assets = RemoteControlWebAssets(
        _asset_tree(tmp_path / "assets"),
        installation_id="installation-a",
        url_prefix="/remote",
    )

    index = assets.index_html.decode("utf-8")
    worker = assets.service_worker.decode("utf-8")
    versioned_prefix = f"/remote/_assets/{assets.revision}"

    assert f"{versioned_prefix}/scripts/app.js" in index
    assert f"{versioned_prefix}/manifest.webmanifest" in index
    assert f"{versioned_prefix}/icons/icon.png" in index
    assert 'href="./trust-certificate.cer"' in index
    assert "__SOLIN_REMOTE_" not in index
    assert "__SOLIN_REMOTE_" not in worker
    assert _resources(worker) == [
        "./",
        "./index.html",
        f"./_assets/{assets.revision}/icons/icon.png",
        f"./_assets/{assets.revision}/manifest.webmanifest",
        f"./_assets/{assets.revision}/scripts/app.js",
    ]

    versioned = assets.resolve_versioned(f"_assets/{assets.revision}/scripts/app.js")
    assert versioned == (tmp_path / "assets" / "scripts" / "app.js").resolve()
    assert assets.resolve_versioned("_assets/outdated/scripts/app.js") is None
    assert assets.resolve_versioned(f"_assets/{assets.revision}/README.md") is None
    assert assets.resolve_versioned(f"_assets/{assets.revision}/../index.html") is None


def test_shell_templates_fail_closed_when_required_placeholders_are_missing(
    tmp_path: Path,
) -> None:
    root = _asset_tree(tmp_path / "assets")
    (root / "service-worker.js").write_text("self.addEventListener('fetch', () => {});", "utf-8")

    with pytest.raises(ValueError, match="must contain exactly one"):
        RemoteControlWebAssets(
            root,
            installation_id="installation-a",
            url_prefix="/remote",
        )


def test_index_fails_closed_for_unknown_local_assets(tmp_path: Path) -> None:
    root = _asset_tree(tmp_path / "assets")
    with (root / "index.html").open("a", encoding="utf-8") as stream:
        stream.write('<script src="./missing.js"></script>')

    with pytest.raises(ValueError, match="unknown asset: missing.js"):
        RemoteControlWebAssets(
            root,
            installation_id="installation-a",
            url_prefix="/remote",
        )
