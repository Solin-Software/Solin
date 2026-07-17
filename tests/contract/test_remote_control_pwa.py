from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from PIL import Image

from solin.styles.icons import (
    ICON_IMAGE,
    ICON_MUSIC,
    ICON_NAV_MEETINGS,
    ICON_NAV_PLAYLIST,
    ICON_SHIELD,
    ICON_VIDEO,
    ICON_VOLUME_HIGH,
    ICON_VOLUME_LOW,
    ICON_VOLUME_MUTE,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PWA_ROOT = PROJECT_ROOT / "src" / "solin" / "resources" / "remote_control"


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.elements.append((tag, {name: value or "" for name, value in attrs}))


def _document() -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed((PWA_ROOT / "index.html").read_text(encoding="utf-8"))
    return parser


def _svg_shape(svg: str) -> tuple:
    def shape(element: ET.Element) -> tuple:
        return (
            element.tag.rsplit("}", 1)[-1],
            tuple(sorted(element.attrib.items())),
            tuple(shape(child) for child in element),
        )

    return shape(ET.fromstring(svg))


def _sprite_symbol(html: str, name: str) -> str:
    match = re.search(
        rf'<symbol id="icon-{re.escape(name)}"(?P<attrs>[^>]*)>(?P<body>.*?)</symbol>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg"{match.group("attrs")}>{match.group("body")}</svg>'
    )


def test_remote_control_pwa_manifest_and_icons_are_installable() -> None:
    manifest = json.loads((PWA_ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))

    assert manifest["id"] == "/remote/"
    assert manifest["start_url"] == "/remote/"
    assert manifest["scope"] == "/remote/"
    assert manifest["display"] == "standalone"
    assert manifest["name"] and manifest["short_name"]

    icons = {entry["src"]: entry for entry in manifest["icons"]}
    assert set(icons) == {
        "./icons/icon-192.png",
        "./icons/icon-512.png",
        "./icons/icon-maskable-512.png",
    }
    assert icons["./icons/icon-maskable-512.png"]["purpose"] == "maskable"
    for source, expected_size in (
        ("./icons/icon-192.png", (192, 192)),
        ("./icons/icon-512.png", (512, 512)),
        ("./icons/icon-maskable-512.png", (512, 512)),
    ):
        with Image.open(PWA_ROOT / source.removeprefix("./")) as image:
            assert image.size == expected_size
            assert image.format == "PNG"


def test_remote_control_shell_is_self_contained_csp_compatible_and_accessible() -> None:
    document = _document()
    ids: list[str] = []
    local_assets: set[str] = set()
    labels: list[str] = []

    for tag, attributes in document.elements:
        if element_id := attributes.get("id"):
            ids.append(element_id)
        assert "style" not in attributes
        if tag == "script":
            assert attributes.get("src")
        if tag == "label" and attributes.get("for"):
            labels.append(attributes["for"])
        for name in ("href", "src"):
            reference = attributes.get(name, "")
            if reference.startswith("./"):
                local_assets.add(reference.removeprefix("./"))

    assert len(ids) == len(set(ids))
    assert set(labels) <= set(ids)
    assert {
        "login-form",
        "playlists-tab",
        "meetings-tab",
        "library-content",
        "tree-content",
        "player-dock",
        "seek-control",
        "volume-slider",
        "stop-button",
    } <= set(ids)

    dynamic_routes = {"trust-certificate.cer"}
    for relative_path in local_assets - dynamic_routes:
        assert (PWA_ROOT / relative_path).is_file(), relative_path


def test_remote_control_navigation_prioritizes_meetings_and_uses_canonical_icons() -> None:
    html = (PWA_ROOT / "index.html").read_text(encoding="utf-8")
    state = (PWA_ROOT / "scripts" / "state.js").read_text(encoding="utf-8")
    app = (PWA_ROOT / "scripts" / "app.js").read_text(encoding="utf-8")
    renderer = (PWA_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")
    formatter = (PWA_ROOT / "scripts" / "format.js").read_text(encoding="utf-8")

    assert html.index('id="meetings-tab"') < html.index('id="playlists-tab"')
    assert 'activeTab: "meetings"' in state
    assert 'const tabs = ["meetings", "playlists"]' in app
    for symbol in (
        "icon-nav-meetings",
        "icon-nav-playlist",
        "icon-volume-high",
        "icon-volume-low",
        "icon-volume-mute",
        "icon-eye",
        "icon-eye-off",
        "icon-shield",
        "icon-media-video",
        "icon-media-audio",
        "icon-media-image",
        "icon-media-generic",
        "icon-logout",
    ):
        assert f'id="{symbol}"' in html
    assert 'setUiIcon("volume-icon", volumeIcon)' in renderer
    assert "Intl.DateTimeFormat" in formatter
    assert "formatRange" in formatter
    assert "meetingWeekGroups" in renderer
    assert 'text: t("library.currentWeek")' in renderer
    assert "marker-number" not in renderer
    assert "collectionThumbnailUrl(collection)" in renderer
    assert 'classList.add("library-icon--cover")' in renderer

    canonical_icons = {
        "media-audio": ICON_MUSIC,
        "media-image": ICON_IMAGE,
        "media-video": ICON_VIDEO,
        "nav-meetings": ICON_NAV_MEETINGS,
        "nav-playlist": ICON_NAV_PLAYLIST,
        "shield": ICON_SHIELD,
        "volume-high": ICON_VOLUME_HIGH,
        "volume-low": ICON_VOLUME_LOW,
        "volume-mute": ICON_VOLUME_MUTE,
    }
    for name, canonical in canonical_icons.items():
        assert _svg_shape(_sprite_symbol(html, name)) == _svg_shape(canonical)


def test_remote_control_service_worker_caches_only_the_static_shell() -> None:
    service_worker = (PWA_ROOT / "service-worker.js").read_text(encoding="utf-8")
    pwa = (PWA_ROOT / "scripts" / "pwa.js").read_text(encoding="utf-8")
    match = re.search(
        r"const SHELL_RESOURCES = \[(?P<resources>.*?)\];",
        service_worker,
        flags=re.DOTALL,
    )
    assert match is not None
    cached = set(re.findall(r'"\./([^"\n]*)"', match.group("resources")))
    expected = {
        path.relative_to(PWA_ROOT).as_posix()
        for path in PWA_ROOT.rglob("*")
        if path.is_file() and path.name not in {"README.md", "service-worker.js"}
    }

    assert cached - {""} == expected
    assert "trust-certificate.cer" not in cached
    assert "/remote/api/" in service_worker
    assert 'request.mode === "navigate"' in service_worker
    assert 'cache: "reload"' in service_worker
    assert "networkFirstShellResource" in service_worker
    assert 'fetch(request, { cache: "no-cache" })' in service_worker
    assert 'updateViaCache: "none"' in pwa
    assert 'navigator.serviceWorker.addEventListener("controllerchange"' in pwa
    assert "window.location.reload()" in pwa


def test_remote_control_setup_is_a_dedicated_three_step_onboarding_flow() -> None:
    document = _document()
    html = (PWA_ROOT / "index.html").read_text(encoding="utf-8")
    setup = (PWA_ROOT / "scripts" / "setup.js").read_text(encoding="utf-8")
    api = (PWA_ROOT / "scripts" / "api.js").read_text(encoding="utf-8")
    ids = {attributes["id"] for _tag, attributes in document.elements if attributes.get("id")}

    assert {
        "setup-view",
        "setup-steps",
        "setup-certificate-link",
        "setup-verification-code",
        "setup-fingerprint",
        "setup-install-button",
    } <= ids
    assert html.count('class="setup-step"') == 3
    assert 'href="?setup=1"' in html
    assert html.index('id="login-view"') < html.index('id="setup-view"')
    login_html = html[html.index('id="login-view"') : html.index('id="setup-view"')]
    assert "setup-certificate-link" not in login_html
    assert 'get("setup") === "1"' in setup
    assert 'return this.#request("/setup", { csrf: false })' in api
    assert "/localization" not in api
    assert 'data-platform-help="android"' in html
    assert 'data-platform-help="apple"' in html
    assert "setup-installed" not in ids
    assert "setup.installed" not in json.loads(
        (PWA_ROOT / "messages.en.json").read_text(encoding="utf-8")
    )
    assert "navigator.maxTouchPoints > 1" in setup
    assert 'setAttribute("aria-disabled", "true")' in setup
    assert 'toggleAttribute("aria-disabled"' not in setup

    setup_view_rules = re.findall(
        r"\.setup-view\s*\{(?P<body>.*?)\}",
        (PWA_ROOT / "styles" / "layout.css").read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert any(
        "height: 100dvh;" in rule and "overflow-y: auto;" in rule
        for rule in setup_view_rules
    )


def test_remote_control_tree_constrains_long_content_on_narrow_screens() -> None:
    layout = (PWA_ROOT / "styles" / "layout.css").read_text(encoding="utf-8")
    components = (PWA_ROOT / "styles" / "components.css").read_text(encoding="utf-8")

    panel_rule = re.search(
        r"\.library-panel,\s*\.tree-panel\s*\{(?P<body>.*?)\}",
        layout,
        flags=re.DOTALL,
    )
    assert panel_rule is not None
    assert "grid-template-columns: minmax(0, 1fr);" in panel_rule.group("body")

    mobile_title_rules = re.findall(
        r"\.tree-title-wrap h2\s*\{(?P<body>.*?)\}",
        layout,
        flags=re.DOTALL,
    )
    assert any(
        "overflow-wrap: anywhere;" in rule
        and "white-space: normal;" in rule
        and "-webkit-line-clamp: 2;" in rule
        for rule in mobile_title_rules
    )

    list_items = re.search(
        r"\.tree-root > li,\s*\.tree-children > li\s*\{(?P<body>.*?)\}",
        components,
        flags=re.DOTALL,
    )
    assert list_items is not None
    assert "min-width: 0;" in list_items.group("body")
    assert "max-width: 100%;" in list_items.group("body")

    for selector in (".tree-group", ".tree-group-summary", ".media-row", ".marker-row"):
        rules = re.findall(
            rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}",
            components,
            flags=re.DOTALL,
        )
        assert any("min-width: 0;" in rule and "max-width: 100%;" in rule for rule in rules)


def test_remote_control_ui_uses_complete_keyed_localization_catalog() -> None:
    messages = json.loads((PWA_ROOT / "messages.en.json").read_text(encoding="utf-8"))
    html = (PWA_ROOT / "index.html").read_text(encoding="utf-8")
    scripts = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((PWA_ROOT / "scripts").glob("*.js"))
    )

    static_keys = set(re.findall(r'data-i18n(?:-aria-label)?="([A-Za-z0-9.]+)"', html))
    direct_keys = set(re.findall(r'\bt\("([A-Za-z0-9.]+)"', scripts))
    plural_bases = set(re.findall(r'\btp\("([A-Za-z0-9.]+)"', scripts))

    assert static_keys | direct_keys <= set(messages)
    for base in plural_bases:
        assert {f"{base}.one", f"{base}.other"} <= set(messages)
    assert 'lang="en"' in html
    assert "pt-BR" not in html
    assert not re.search(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]", scripts)


def test_remote_control_scripts_never_persist_secrets_or_inject_markup() -> None:
    scripts = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((PWA_ROOT / "scripts").glob("*.js"))
    )

    for forbidden in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "new Function",
    ):
        assert forbidden not in scripts
    assert 'const API_ROOT = "/remote/api"' in scripts
    assert "new WebSocket" in scripts
    assert 'credentials: "same-origin"' in scripts
    assert 'cache: "no-store"' in scripts


def test_remote_control_player_is_compact_capability_driven_and_keeps_catalog_visible() -> None:
    document = _document()
    elements_by_id = {
        attributes["id"]: (tag, attributes)
        for tag, attributes in document.elements
        if attributes.get("id")
    }
    layout = (PWA_ROOT / "styles" / "layout.css").read_text(encoding="utf-8")
    renderer = (PWA_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")

    assert elements_by_id["app-shell"][1]["data-player-mode"] == "idle"
    assert elements_by_id["player-dock"][1]["data-mode"] == "idle"
    for control_group in ("transport", "dock-actions", "volume-control"):
        assert "hidden" in elements_by_id[control_group][1]

    # Explicit grid placement prevents a hidden reconnect banner from shifting
    # the player into the flexible, scrollable catalog row.
    for selector, row in (
        (".topbar", 1),
        (".reconnect-banner", 2),
        (".primary-tabs", 3),
        (".workspace", 4),
        (".player-dock", 5),
    ):
        rule = re.search(
            rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}",
            layout,
            flags=re.DOTALL,
        )
        assert rule is not None
        assert f"grid-row: {row};" in rule.group("body")
    assert "minmax(0, 1fr)" in layout
    assert "--dock-height: 224px" not in layout
    assert "--dock-height: 228px" not in layout
    scroll_rule = re.search(
        r"\.library-content,\s*\.tree-content\s*\{(?P<body>.*?)\}",
        layout,
        flags=re.DOTALL,
    )
    assert scroll_rule is not None
    assert "min-height: 0;" in scroll_rule.group("body")
    assert "overflow-y: auto;" in scroll_rule.group("body")

    # mediaKind is the canonical contract field. Only audio/video may expose
    # timed controls, and every action is still gated by capabilities.
    assert 'new Set(["audio", "video"])' in renderer
    assert "TIMED_MEDIA_KINDS.has(playback.mediaKind)" in renderer
    assert "const showTimeline = timed && duration > 0" in renderer
    assert "showTimeline && capabilities.seek === true" in renderer
    assert "seek.disabled = !seekEnabled" in renderer
    assert "timeline.hidden = !showTimeline" in renderer
    assert "const showQueueNavigation = active && (playback.queue?.length ?? 0) > 1" in renderer
    assert "capabilities.previous === true && online" in renderer
    assert "capabilities.next === true && online" in renderer
    assert "volumeControl.hidden = !showVolume" in renderer
    assert "capabilities.stop === true" in renderer
    assert "queueItem?.thumbnailId" in renderer
    assert "mediaThumbnailUrl(" in renderer
    assert "this.appShell.dataset.playerMode = layoutMode" in renderer


def test_remote_tree_uses_authoritative_collapsed_state_without_layout_resets() -> None:
    renderer = (PWA_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")

    assert "const isOpen = locallyExpanded ?? node.collapsed !== true" in renderer
    assert 'attrs: { open: isOpen ? "" : null }' in renderer
    assert 'details.addEventListener("toggle"' in renderer
    assert "if (catalogChanged) this.#groupExpansion.clear()" in renderer
