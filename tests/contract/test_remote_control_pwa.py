from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

from solin.core.remote_control.web_assets import RemoteControlWebAssets
from solin.styles.icons import (
    ICON_HOME,
    ICON_IMAGE,
    ICON_MUSIC,
    ICON_NAV_MEETINGS,
    ICON_NAV_PLAYLIST,
    ICON_SEC_LIVING,
    ICON_SEC_MINISTRY,
    ICON_SEC_PUBLIC_TALK,
    ICON_SEC_TREASURES,
    ICON_SEC_WATCHTOWER,
    ICON_SHIELD,
    ICON_VIDEO,
    ICON_VOLUME_HIGH,
    ICON_VOLUME_LOW,
    ICON_VOLUME_MUTE,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PWA_ROOT = PROJECT_ROOT / "src" / "solin" / "resources" / "remote_control"
SHARED_ROOT = PWA_ROOT / "shared"
MEDIA_ROOT = PWA_ROOT / "media"
TIMER_ROOT = PWA_ROOT / "timer"


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


def _document(app_root: Path) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed((app_root / "index.html").read_text(encoding="utf-8"))
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


def _javascript_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    if source[max(0, start - 6) : start] == "async ":
        start -= 6
    open_paren = source.index("(", start)
    paren_depth = 0
    close_paren = open_paren
    for index in range(open_paren, len(source)):
        if source[index] == "(":
            paren_depth += 1
        elif source[index] == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close_paren = index
                break
    opening = source.index("{", close_paren)
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(source)):
        character = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'", "`"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Unclosed JavaScript function: {name}")


@pytest.mark.parametrize(
    ("app_root", "expected_scope"),
    [
        (MEDIA_ROOT, "/remote/media/"),
        (TIMER_ROOT, "/remote/timer/"),
    ],
)
def test_remote_control_pwa_manifests_and_icons_are_installable(
    app_root: Path,
    expected_scope: str,
) -> None:
    manifest = json.loads((app_root / "manifest.webmanifest").read_text(encoding="utf-8"))

    assert manifest["id"] == expected_scope
    assert manifest["start_url"] == expected_scope
    assert manifest["scope"] == expected_scope
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
        with Image.open(app_root / source.removeprefix("./")) as image:
            assert image.size == expected_size
            assert image.format == "PNG"


def test_media_and_timer_pwa_identities_and_icons_are_distinct() -> None:
    media_manifest = json.loads((MEDIA_ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))
    timer_manifest = json.loads((TIMER_ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))

    assert media_manifest["id"] != timer_manifest["id"]
    assert media_manifest["name"] == media_manifest["short_name"] == "Solin Media"
    assert timer_manifest["name"] == timer_manifest["short_name"] == "Solin Timer"

    for icon_name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png"):
        assert (MEDIA_ROOT / "icons" / icon_name).read_bytes() != (
            TIMER_ROOT / "icons" / icon_name
        ).read_bytes()


@pytest.mark.parametrize("app_root", [MEDIA_ROOT, TIMER_ROOT])
def test_remote_control_shells_are_self_contained_csp_compatible_and_accessible(
    app_root: Path,
) -> None:
    document = _document(app_root)
    ids: list[str] = []
    app_assets: set[str] = set()
    shared_assets: set[str] = set()
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
                app_assets.add(reference.removeprefix("./"))
            elif reference.startswith("../shared/"):
                shared_assets.add(reference.removeprefix("../shared/"))

    assert len(ids) == len(set(ids))
    assert set(labels) <= set(ids)

    dynamic_routes = {"trust-certificate.cer", "/remote/trust-certificate.cer"}
    for relative_path in app_assets - dynamic_routes:
        assert (app_root / relative_path).is_file(), relative_path
    for relative_path in shared_assets:
        assert (SHARED_ROOT / relative_path).is_file(), relative_path


def test_media_shell_has_required_controls_and_canonical_icons() -> None:
    document = _document(MEDIA_ROOT)
    ids = {attributes["id"] for _tag, attributes in document.elements if attributes.get("id")}
    html = (MEDIA_ROOT / "index.html").read_text(encoding="utf-8")
    state = (MEDIA_ROOT / "scripts" / "state.js").read_text(encoding="utf-8")
    app = (MEDIA_ROOT / "scripts" / "app.js").read_text(encoding="utf-8")
    renderer = (MEDIA_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")
    formatter = (MEDIA_ROOT / "scripts" / "format.js").read_text(encoding="utf-8")

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
    } <= ids

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


def test_timer_shell_is_fullscreen_first_with_modal_login_and_canonical_section_icons() -> None:
    document = _document(TIMER_ROOT)
    elements_by_id = {
        attributes["id"]: (tag, attributes)
        for tag, attributes in document.elements
        if attributes.get("id")
    }
    ids = set(elements_by_id)
    html = (TIMER_ROOT / "index.html").read_text(encoding="utf-8")
    state = (TIMER_ROOT / "scripts" / "state.js").read_text(encoding="utf-8")
    renderer = (TIMER_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")
    clock_face = (TIMER_ROOT / "scripts" / "clock-face.js").read_text(encoding="utf-8")

    assert {
        "stage-view",
        "stage-canvas",
        "stage-hud",
        "stage-signin-button",
        "stage-control-button",
        "stage-fullscreen-button",
        "login-modal",
        "login-form",
        "control-view",
        "sticky-timer-bar",
        "sticky-scroll-top",
        "sticky-expand-button",
        "hero-expand-button",
        "hero-side-time",
        "hero-ampm-time",
        "hero-suffix-time",
        "hero-minus-second",
        "hero-plus-second",
        "hero-stop-button",
        "week-prev",
        "week-next",
        "week-current",
        "week-not-current",
        "schedule-list",
        "setup-view",
    } <= ids
    assert "control-stage-button" not in ids
    assert "hero-fullscreen-button" not in ids

    assert html.index('id="stage-view"') < html.index('id="control-view"')
    assert 'view: "boot"' in state
    assert "loginModalOpen: false" in state

    # Home button stays in layout (disabled on current week rather than hidden),
    # while the non-current-week subtitle appears explicitly below the week label.
    assert "hidden" not in elements_by_id["week-current"][1]
    assert "disabled" in elements_by_id["week-current"][1]
    assert "hidden" in elements_by_id["week-not-current"][1]
    assert 'byId("week-not-current").hidden = Boolean(schedule.isCurrentWeek)' in renderer
    assert "homeBtn.hidden = !caps.navigateWeeks" in renderer
    assert "Boolean(schedule.isCurrentWeek)" in renderer

    # Small trailing seconds must never carry a leading colon.
    assert "`:${stageSecs}`" not in renderer
    assert "`:${wallSecs}`" not in renderer
    assert "`:${seconds}`" not in clock_face
    assert "suffixTime.textContent = secsText" in renderer

    canonical_icons = {
        "home": ICON_HOME,
        "shield": ICON_SHIELD,
        "sec-treasures": ICON_SEC_TREASURES,
        "sec-ministry": ICON_SEC_MINISTRY,
        "sec-living": ICON_SEC_LIVING,
        "sec-public_talk": ICON_SEC_PUBLIC_TALK,
        "sec-watchtower": ICON_SEC_WATCHTOWER,
    }
    for name, canonical in canonical_icons.items():
        assert _svg_shape(_sprite_symbol(html, name)) == _svg_shape(canonical)


@pytest.mark.parametrize(
    ("app_root", "url_prefix"),
    [
        (MEDIA_ROOT, "/remote/media"),
        (TIMER_ROOT, "/remote/timer"),
    ],
)
def test_remote_control_service_workers_cache_only_the_static_shell(
    app_root: Path,
    url_prefix: str,
) -> None:
    web_assets = RemoteControlWebAssets(
        app_root,
        shared_root=SHARED_ROOT,
        installation_id="contract-installation",
        url_prefix=url_prefix,
    )
    service_worker = web_assets.service_worker.decode("utf-8")
    pwa = (SHARED_ROOT / "scripts" / "pwa.js").read_text(encoding="utf-8")
    match = re.search(
        r"const SHELL_RESOURCES = \[(?P<resources>.*?)\];",
        service_worker,
        flags=re.DOTALL,
    )
    assert match is not None
    cached = set(re.findall(r'"\./([^"\n]*)"', match.group("resources")))

    app_versioned = {
        f"app/{path.relative_to(app_root).as_posix()}"
        for path in app_root.rglob("*")
        if path.is_file() and path.name not in {"README.md", "index.html", "service-worker.js"}
    }
    shared_versioned = {
        f"shared/{path.relative_to(SHARED_ROOT).as_posix()}"
        for path in SHARED_ROOT.rglob("*")
        if path.is_file() and path.name != "README.md"
    }
    expected = {
        "index.html",
        *(f"_assets/{web_assets.revision}/{path}" for path in (app_versioned | shared_versioned)),
    }

    assert cached - {""} == expected
    assert "trust-certificate.cer" not in cached
    assert f"{url_prefix}/api/" in service_worker
    assert 'request.mode === "navigate"' in service_worker
    assert 'cache: "reload"' in service_worker
    assert "cacheFirstShellResource" in service_worker
    assert 'fetch(request, { cache: "reload" })' in service_worker
    assert "name.startsWith(CACHE_PREFIX)" in service_worker
    assert 'updateViaCache: "none"' in pwa
    assert "await registration.update()" in pwa
    assert 'navigator.serviceWorker.addEventListener("controllerchange"' in pwa
    assert "window.location.reload()" in pwa


@pytest.mark.parametrize(
    ("app_root", "url_prefix"),
    [
        (MEDIA_ROOT, "/remote/media"),
        (TIMER_ROOT, "/remote/timer"),
    ],
)
def test_rendered_remote_control_service_workers_are_valid_javascript(
    app_root: Path,
    url_prefix: str,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to validate the rendered Service Worker")
    service_worker = RemoteControlWebAssets(
        app_root,
        shared_root=SHARED_ROOT,
        installation_id="contract-installation",
        url_prefix=url_prefix,
    ).service_worker.decode("utf-8")

    result = subprocess.run(
        [node, "--check", "-"],
        input=service_worker,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_remote_control_media_cards_only_play_from_the_explicit_action() -> None:
    renderer = (MEDIA_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")
    components = (MEDIA_ROOT / "styles" / "components.css").read_text(encoding="utf-8")
    media_row = re.search(
        r"  #mediaRow\(.*?\n(?P<body>.*?)\n  #renderPlayer\(",
        renderer,
        flags=re.DOTALL,
    )
    assert media_row is not None
    body = media_row.group("body")
    card, play_action = body.split('const action = element("button",', maxsplit=1)

    assert 'const row = element("div", {' in card
    assert 'className: "media-row"' in card
    assert 'action: "play-media"' not in card
    assert 'className: "media-action"' in play_action
    assert 'disabled: disabled ? "" : null' in play_action
    assert '"aria-label": current' in play_action
    assert 'action: "play-media"' in play_action
    assert "row.append(thumbnail, copy, action)" in play_action

    action_rules = re.findall(
        r"\.media-action\s*\{(?P<body>.*?)\}",
        components,
        flags=re.DOTALL,
    )
    assert any(
        "width: var(--touch-target);" in rule and "height: var(--touch-target);" in rule
        for rule in action_rules
    )


def test_remote_control_connection_uses_an_atomic_session_snapshot() -> None:
    media_app = (MEDIA_ROOT / "scripts" / "app.js").read_text(encoding="utf-8")
    timer_app = (TIMER_ROOT / "scripts" / "app.js").read_text(encoding="utf-8")
    api = (SHARED_ROOT / "scripts" / "api.js").read_text(encoding="utf-8")

    assert 'message.type === "session.snapshot"' in media_app
    assert "applyWebsocketSessionSnapshot(message)" in media_app
    assert 'connection: "online"' in media_app
    assert 'event.type === "public.snapshot"' in timer_app
    assert 'event.type === "session.snapshot"' in timer_app
    assert 'message?.type !== "heartbeat"' not in api
    assert "this.#onEvent(message)" in api or "this.onEvent(message)" in api


def test_stale_play_recovery_is_bounded_and_revalidates_the_catalog_origin() -> None:
    app = (MEDIA_ROOT / "scripts" / "app.js").read_text(encoding="utf-8")
    service_worker = RemoteControlWebAssets(
        MEDIA_ROOT,
        shared_root=SHARED_ROOT,
        installation_id="contract-installation",
        url_prefix="/remote/media",
    ).service_worker.decode("utf-8")

    recovery = re.search(
        r"async function executeCommandWithStateRecovery\(.*?\n\}",
        app,
        flags=re.DOTALL,
    )
    assert recovery is not None
    body = recovery.group(0)
    assert "let retriedPlay = false" in body
    assert 'code === "catalog_stale" && !retriedPlay && recovered && refreshed' in body
    assert "retriedPlay = true" in body
    assert "continue" in body
    assert body.count("newCommandId()") == 1
    assert "function refreshedPlayCommand(fields)" in app
    assert "candidate.id === collectionId && candidate.kind === source" in app
    assert 'node.kind !== "media" || !node.available' in app
    assert re.search(r'const SHELL_REVISION = "[a-f0-9]{32}";', service_worker)


def test_stale_play_recovery_behavior() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the PWA recovery behavior test")
    app = (MEDIA_ROOT / "scripts" / "app.js").read_text(encoding="utf-8")
    functions = "\n\n".join(
        _javascript_function(app, name)
        for name in (
            "executeCommandWithStateRecovery",
            "refreshedPlayCommand",
            "findCatalogNode",
            "postCommandWithAuthenticationRecovery",
        )
    )
    script = textwrap.dedent(
        """
        class ApiError extends Error {
          constructor(message, options = {}) {
            super(message);
            this.code = options.code ?? "failed";
            this.status = options.status ?? 0;
            this.retryable = Boolean(options.retryable);
          }
        }

        let nextId = 0;
        let scenario;
        let apiCalls;
        let recoverCalls;
        let toasts;
        const store = { state: {} };
        const t = (key) => key;
        const newCommandId = () => `command-${++nextId}`;
        const isUnauthorized = (error) => error instanceof ApiError && error.status === 401;
        const showToast = (message, level) => toasts.push({ message, level });
        const api = {
          async command(command) {
            apiCalls.push(JSON.parse(JSON.stringify(command)));
            const response = scenario.responses.shift() ?? "success";
            if (response === "403") {
              throw new ApiError("Forbidden", { code: "forbidden", status: 403 });
            }
            if (response === "success") return { ok: true };
            return { ok: false, error: { code: response, message: response } };
          },
        };

        async function recover() {
          recoverCalls += 1;
          const catalog = scenario.catalogs.shift();
          if (catalog) store.state.catalog = catalog;
          return scenario.recoveries.shift() ?? true;
        }

        __FUNCTIONS__

        const availableCatalog = (revision = 2) => ({
          catalogRevision: revision,
          collections: [{
            id: "playlist-1",
            kind: "playlist",
            nodes: [{ id: "media-1", kind: "media", available: true, children: [] }],
          }],
        });
        const missingCatalog = (revision = 2) => ({ catalogRevision: revision, collections: [] });
        const play = () => ({
          type: "play",
          catalogRevision: 1,
          origin: { source: "playlist", collectionId: "playlist-1", nodeId: "media-1" },
        });
        const assert = (condition, message) => {
          if (!condition) throw new Error(message);
        };

        async function run(responses, catalogs = [], recoveries = []) {
          scenario = { responses: [...responses], catalogs: [...catalogs], recoveries: [...recoveries] };
          apiCalls = [];
          recoverCalls = 0;
          toasts = [];
          store.state = { view: "app", catalog: availableCatalog(1) };
          await executeCommandWithStateRecovery(play());
          return { apiCalls, recoverCalls, toasts };
        }

        const healed = await run(["catalog_stale", "success"], [availableCatalog(2)]);
        assert(healed.apiCalls.length === 2, "stale play must retry exactly once");
        assert(healed.apiCalls[0].commandId !== healed.apiCalls[1].commandId, "retry needs a new id");
        assert(healed.apiCalls[1].catalogRevision === 2, "retry needs the recovered revision");
        assert(healed.recoverCalls === 1 && healed.toasts.length === 0, "healed retry stays silent");

        const removed = await run(["catalog_stale"], [missingCatalog(2)]);
        assert(removed.apiCalls.length === 1, "removed media must not retry");
        assert(removed.recoverCalls === 1 && removed.toasts.length === 1, "removed media reports change");

        const bounded = await run(
          ["catalog_stale", "catalog_stale", "success"],
          [availableCatalog(2), availableCatalog(3)],
        );
        assert(bounded.apiCalls.length === 2, "a second stale response must stop retrying");
        assert(bounded.toasts.length === 1, "a second stale response reports the state change");

        const playbackStale = await run(["playback_stale"], [availableCatalog(2)]);
        assert(playbackStale.apiCalls.length === 1, "playback stale must never replay a command");

        const authenticated = await run(["403", "success"], [availableCatalog(1)]);
        assert(authenticated.apiCalls.length === 2, "403 recovery must repeat the HTTP request");
        assert(
          authenticated.apiCalls[0].commandId === authenticated.apiCalls[1].commandId,
          "403 recovery must preserve command idempotency",
        );
        """
    ).replace("__FUNCTIONS__", functions)

    result = subprocess.run(  # noqa: S603 - fixed local Node.js test harness
        [node, "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("app_root", [MEDIA_ROOT, TIMER_ROOT])
def test_remote_control_setup_is_a_dedicated_three_step_onboarding_flow(
    app_root: Path,
) -> None:
    document = _document(app_root)
    html = (app_root / "index.html").read_text(encoding="utf-8")
    setup = (SHARED_ROOT / "scripts" / "setup.js").read_text(encoding="utf-8")
    api = (SHARED_ROOT / "scripts" / "api.js").read_text(encoding="utf-8")
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
    assert 'get("setup") === "1"' in setup
    assert 'return this.#request("/setup", { csrf: false })' in api
    assert "/localization" not in api
    assert 'data-platform-help="android"' in html
    assert 'data-platform-help="apple"' in html
    assert "setup-installed" not in ids
    assert "setup.installed" not in json.loads(
        (app_root / "messages.en.json").read_text(encoding="utf-8")
    )
    assert "navigator.maxTouchPoints > 1" in setup
    assert 'setAttribute("aria-disabled", "true")' in setup
    assert 'toggleAttribute("aria-disabled"' not in setup

    setup_view_rules = re.findall(
        r"\.setup-view\s*\{(?P<body>.*?)\}",
        (app_root / "styles" / "layout.css").read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert any(
        "height: 100dvh;" in rule and "overflow-y: auto;" in rule for rule in setup_view_rules
    )


def test_remote_control_tree_constrains_long_content_on_narrow_screens() -> None:
    layout = (MEDIA_ROOT / "styles" / "layout.css").read_text(encoding="utf-8")
    components = (MEDIA_ROOT / "styles" / "components.css").read_text(encoding="utf-8")

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


def test_timer_workspace_constrains_long_content_on_narrow_screens() -> None:
    layout = (TIMER_ROOT / "styles" / "layout.css").read_text(encoding="utf-8")
    components = (TIMER_ROOT / "styles" / "components.css").read_text(encoding="utf-8")

    for selector in (
        r"\.boot-view,\s*\.setup-view",
        r"\.modal-backdrop",
        r"\.control-shell",
        r"\.control-container",
        r"\.brand-copy",
        r"\.stage-brand-copy",
    ):
        rules = re.findall(rf"{selector}\s*\{{(?P<body>.*?)\}}", layout, flags=re.DOTALL)
        assert any("grid-template-columns: minmax(0, 1fr);" in rule for rule in rules), selector

    for selector in (
        r"\.hero-card",
        r"\.schedule-list",
        r"\.section-parts",
        r"\.part-copy",
        r"\.login-form,\s*\.duration-form",
        r"\.field",
        r"\.setup-steps",
        r"\.verification-card",
    ):
        rules = re.findall(rf"{selector}\s*\{{(?P<body>.*?)\}}", components, flags=re.DOTALL)
        assert any("grid-template-columns: minmax(0, 1fr);" in rule for rule in rules), selector

    for selector, source in (
        (".topbar", layout),
        (".reconnect-banner", layout),
        (".control-workspace", layout),
        (".sticky-timer-shell", layout),
        (".control-container", layout),
        (".stage-hud-bar", layout),
        (".stage-actions", layout),
        (".hero-card", components),
        (".sticky-timer-dock", components),
        (".meeting-bar", components),
        (".week-nav", components),
        (".section-card", components),
        (".section-header", components),
        (".part-row", components),
        (".part-info", components),
        (".part-controls", components),
        (".duration-cluster", components),
    ):
        rules = re.findall(
            rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}",
            source,
            flags=re.DOTALL,
        )
        assert any("min-width: 0;" in rule and "max-width: 100%;" in rule for rule in rules), (
            selector
        )

    for selector in (".hero-title", ".section-title", ".part-title", ".duration-modal-title"):
        rules = re.findall(
            rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}",
            components,
            flags=re.DOTALL,
        )
        assert any("overflow-wrap: anywhere;" in rule for rule in rules), selector

    segmented_rules = re.findall(
        r"\.segmented-switch\s*\{(?P<body>.*?)\}",
        components,
        flags=re.DOTALL,
    )
    assert any(
        "grid-template-columns: repeat(2, minmax(0, 1fr));" in rule for rule in segmented_rules
    )


def test_timer_part_rows_hide_minute_steppers_when_uneditable_and_use_single_slot_actions() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the timer part-row behavior test")
    renderer = (TIMER_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")
    components = (TIMER_ROOT / "styles" / "components.css").read_text(encoding="utf-8")
    part_row_fn = _javascript_function(renderer, "buildPartRow")

    script = textwrap.dedent(
        """
        function element(tag, options = {}) {
          return {
            tag,
            className: options.className ?? "",
            text: options.text ?? "",
            attrs: { ...(options.attrs ?? {}) },
            dataset: { ...(options.dataset ?? {}) },
            disabled: false,
            children: [],
            append(...items) {
              this.children.push(...items);
            },
          };
        }
        const cssIcon = (name) => element("span", { className: `css-icon css-icon--${name}` });
        const uiIcon = (name) => element("svg", { className: "ui-icon", dataset: { icon: name } });
        const t = (key) => key;

        __BUILD_PART_ROW__

        function walk(node, out = []) {
          out.push(node);
          for (const child of node.children) walk(child, out);
          return out;
        }
        const actionsIn = (row) => walk(row).filter((n) => n.dataset.action).map((n) => n.dataset.action);
        const hasClass = (row, cls) => walk(row).some((n) => n.className.split(/\\s+/).includes(cls));
        const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

        const caps = {
          adjustPlannedMinutes: true,
          setPlannedSeconds: true,
          controlParts: true,
        };
        const basePart = {
          id: "p1",
          displayNumber: 1,
          title: "Part 1",
          plannedSeconds: 300,
          plannedLabel: "05:00",
          state: "idle",
          durationEditable: true,
          startedLabel: "",
          resultLabel: "",
        };

        // 1. Editable idle part: +/-1 min, duration editor, and Start action
        const idleEditable = buildPartRow(basePart, { isInteractive: true, caps });
        assert(
          JSON.stringify(actionsIn(idleEditable)) ===
            JSON.stringify(["adjust-part", "edit-duration", "adjust-part", "start-part"]),
          "editable idle part must expose minute steppers, duration editor, and start-part",
        );

        // 2. Locked idle part (e.g., 1-part fixed-total section): no steppers or editor, read-only badge + Start
        const idleLocked = buildPartRow(
          { ...basePart, durationEditable: false },
          { isInteractive: true, caps },
        );
        assert(
          JSON.stringify(actionsIn(idleLocked)) === JSON.stringify(["start-part"]),
          "locked idle part must omit minute steppers and duration editor",
        );
        assert(hasClass(idleLocked, "part-duration-badge"), "locked idle part shows read-only badge");

        // 3. Running part: live badge + Stop only (no disabled minute steppers)
        const runningRow = buildPartRow(
          { ...basePart, state: "running", durationEditable: false, startedLabel: "19:30" },
          { isInteractive: true, caps },
        );
        assert(
          JSON.stringify(actionsIn(runningRow)) === JSON.stringify(["stop-part"]),
          "running part must omit minute steppers and expose only stop-part",
        );
        assert(hasClass(runningRow, "part-live-badge"), "running part shows live badge");
        assert(
          walk(runningRow).some((n) => n.dataset.livePartId === "p1"),
          "running part binds livePartId for live countdown ticks",
        );

        // 4. Stopped part: read-only badge + Reset in the primary action slot (never Start + Reset)
        const stoppedRow = buildPartRow(
          {
            ...basePart,
            state: "stopped",
            durationEditable: false,
            startedLabel: "19:30",
            resultLabel: "05:12",
          },
          { isInteractive: true, caps },
        );
        assert(
          JSON.stringify(actionsIn(stoppedRow)) === JSON.stringify(["reset-part"]),
          "stopped part must expose reset-part in place of start-part and omit minute steppers",
        );
        const resetBtn = walk(stoppedRow).find((n) => n.dataset.action === "reset-part");
        assert(
          resetBtn.className.includes("part-action-btn") && resetBtn.className.includes("button--reset"),
          "reset button must occupy the primary part-action-btn slot",
        );
        assert(hasClass(stoppedRow, "part-duration-badge"), "stopped part shows read-only badge");
        """
    ).replace("__BUILD_PART_ROW__", part_row_fn)

    result = subprocess.run(  # noqa: S603 - fixed local Node.js test harness
        [node, "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr

    for selector in (".button--reset", ".part-live-badge", ".part-duration-badge"):
        assert re.search(rf"{re.escape(selector)}\s*\{{", components) is not None, selector


@pytest.mark.parametrize("app_root", [MEDIA_ROOT, TIMER_ROOT])
def test_remote_control_ui_uses_complete_keyed_localization_catalog(app_root: Path) -> None:
    messages = json.loads((app_root / "messages.en.json").read_text(encoding="utf-8"))
    html = (app_root / "index.html").read_text(encoding="utf-8")
    shared_scripts = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((SHARED_ROOT / "scripts").glob("*.js"))
    )
    app_scripts = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((app_root / "scripts").glob("*.js"))
    )
    scripts = f"{shared_scripts}\n{app_scripts}"

    static_keys = set(re.findall(r'data-i18n(?:-aria-label|-title)?="([A-Za-z0-9.]+)"', html))
    direct_keys = set(re.findall(r'\bt\("([A-Za-z0-9.]+)"', scripts))
    plural_bases = set(re.findall(r'\btp\("([A-Za-z0-9.]+)"', scripts))

    assert static_keys | direct_keys <= set(messages)
    for base in plural_bases:
        assert {f"{base}.one", f"{base}.other"} <= set(messages)
    assert 'lang="en"' in html
    assert "pt-BR" not in html
    assert not re.search(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]", scripts)


def test_remote_control_scripts_never_persist_secrets_inject_markup_or_mutate_inline_styles() -> (
    None
):
    script_paths = [
        *sorted((SHARED_ROOT / "scripts").glob("*.js")),
        *sorted((MEDIA_ROOT / "scripts").glob("*.js")),
        *sorted((TIMER_ROOT / "scripts").glob("*.js")),
    ]
    scripts = "\n".join(path.read_text(encoding="utf-8") for path in script_paths)

    for forbidden in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "new Function",
        ".style.",
        '.setAttribute("style"',
        ".setAttribute('style'",
    ):
        assert forbidden not in scripts, forbidden

    media_app = (MEDIA_ROOT / "scripts" / "app.js").read_text(encoding="utf-8")
    timer_app = (TIMER_ROOT / "scripts" / "app.js").read_text(encoding="utf-8")
    assert 'new RemoteApi("/remote/media/api")' in media_app
    assert 'new RemoteApi("/remote/timer/api")' in timer_app
    assert "new WebSocket" in scripts
    assert 'credentials: "same-origin"' in scripts
    assert 'cache: "no-store"' in scripts


def test_remote_control_player_is_compact_capability_driven_and_keeps_catalog_visible() -> None:
    document = _document(MEDIA_ROOT)
    elements_by_id = {
        attributes["id"]: (tag, attributes)
        for tag, attributes in document.elements
        if attributes.get("id")
    }
    layout = (MEDIA_ROOT / "styles" / "layout.css").read_text(encoding="utf-8")
    renderer = (MEDIA_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")

    assert elements_by_id["app-shell"][1]["data-player-mode"] == "idle"
    assert elements_by_id["player-dock"][1]["data-mode"] == "idle"
    for control_group in ("transport", "dock-actions", "volume-control"):
        assert "hidden" in elements_by_id[control_group][1]

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
    renderer = (MEDIA_ROOT / "scripts" / "renderer.js").read_text(encoding="utf-8")

    assert "const isOpen = locallyExpanded ?? node.collapsed !== true" in renderer
    assert 'attrs: { open: isOpen ? "" : null }' in renderer
    assert 'details.addEventListener("toggle"' in renderer
    assert "if (catalogChanged) this.#groupExpansion.clear()" in renderer
