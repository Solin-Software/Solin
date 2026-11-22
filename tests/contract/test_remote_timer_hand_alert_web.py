from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest

from solin.core.remote_control.web_assets import WEB_ASSET_CONTENT_TYPES, RemoteControlWebAssets


playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect

REMOTE_ROOT = Path(__file__).resolve().parents[2] / "src/solin/resources/remote_control"
TIMER_PATH = "/remote/timer/"
TIMER_URL = f"http://127.0.0.1:8877{TIMER_PATH}"


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch()
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Run playwright install chromium for timer stage browser tests")
            raise
        with browser:
            yield browser


@pytest.fixture
def open_stage(browser):
    assets = RemoteControlWebAssets(
        REMOTE_ROOT / "timer",
        shared_root=REMOTE_ROOT / "shared",
        installation_id="web-test",
        url_prefix=TIMER_PATH,
    )
    contexts = []

    def open_stage(*, width=844, height=390, authenticated=False, reduced_motion="reduce"):
        context = browser.new_context(
            viewport={"width": width, "height": height},
            reduced_motion=reduced_motion,
            service_workers="block",
        )
        contexts.append(context)
        page = context.new_page()
        page.set_default_timeout(4_000)
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(error))
        session = {
            "authenticated": authenticated,
            "controlAvailable": True,
            "serverInstanceId": "test-timer",
            "profile": {"locale": "en"},
        }
        sequence = 0
        sockets = {}

        def live(revision, alert=None, **fields):
            snapshot = {"liveRevision": revision, "serverEpochMs": 1_700_000_000_000, **fields}
            if alert is not None:
                snapshot["handAlert"] = alert
            return snapshot

        def payload():
            return {**session, "eventSequence": sequence, "live": live(sequence)}

        def route_http(route):
            path = urlsplit(route.request.url).path.removeprefix(TIMER_PATH)
            if not path:
                route.fulfill(body=assets.index_html, content_type="text/html")
            elif path in ("api/public", "api/bootstrap"):
                route.fulfill(json=payload())
            elif asset := assets.resolve_versioned(path):
                route.fulfill(path=asset, content_type=WEB_ASSET_CONTENT_TYPES[asset.suffix])
            else:
                route.fulfill(status=404)

        def send(event_type, body, *, auth=False):
            nonlocal sequence
            sequence += 1
            sockets[auth].send(
                json.dumps(
                    {
                        "type": event_type,
                        "sequence": sequence,
                        "serverInstanceId": session["serverInstanceId"],
                        "payload": body,
                    }
                )
            )

        def route_socket(socket):
            auth = not socket.url.endswith("/public")
            sockets[auth] = socket
            send("session.snapshot" if auth else "public.snapshot", payload(), auth=auth)

        def push(alert=None, *, auth=False, **fields):
            send("timer.live", live(sequence + 1, alert, **fields), auth=auth)

        page.route(f"{TIMER_URL}**", route_http)
        page.route_web_socket(f"ws://127.0.0.1:8877{TIMER_PATH}api/ws**", route_socket)
        page.goto(TIMER_URL)
        expect(page.locator("#stage-connection-chip")).to_have_attribute("data-state", "online")
        return page, push, send, page_errors

    yield open_stage
    for context in contexts:
        context.close()


@pytest.mark.parametrize(("width", "height"), [(844, 390), (390, 844)])
def test_named_hand_alert_expands_the_same_card_and_preserves_the_clock(open_stage, width, height):
    page, push, _, page_errors = open_stage(width=width, height=height)
    stage = page.locator("#stage-view")
    stage.focus()
    page.keyboard.press("Enter")
    card = page.locator("#stage-hand-alert-card")
    show_all = card.get_by_role("button", name="Show all")
    canvas = stage.locator("canvas")
    face_bounds = canvas.bounding_box()
    names = ["<img src=x onerror=alert(1)>"] + [f"Person {n}" for n in range(2, 21)]
    alert = {"kind": "automatic", "count": len(names), "names": names}
    push(alert, active=True, state="running", plannedSeconds=300)

    rows = card.get_by_role("listitem")
    expect(show_all).to_be_visible()
    preview_count = rows.count()
    expect(rows).to_have_text(names[:preview_count])
    expect(card.get_by_text(f"+{len(names) - preview_count}", exact=True)).to_be_visible()
    expect(card.get_by_text("20", exact=True)).to_be_visible()
    assert card.locator("img").count() == 0
    preview = card.bounding_box()
    assert preview["y"] >= page.locator("#stage-hud").bounding_box()["height"]
    assert preview["height"] <= height * 0.55
    if width > height:
        assert preview["x"] > width / 2
        assert preview_count == 4
    else:
        bottom = preview["y"] + preview["height"]
        assert height * 0.39 < bottom < height * 0.47
        assert 4 <= preview_count < 8

    first_row = rows.first.element_handle()
    push(alert, active=True, state="stopped", plannedSeconds=300, elapsedSeconds=250)
    readout = card.locator("#stage-hand-alert-time")
    expect(readout.get_by_text("04:10", exact=True)).to_be_visible()
    assert rows.first.evaluate("(row, before) => row === before", first_row)

    show_all.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Raised hands 20")
    close = dialog.get_by_role("button", name="Close raised hands list")
    expect(close).to_be_focused()
    expect(readout.get_by_text("04:10", exact=True)).to_be_visible()
    expect(rows).to_have_text(names)
    assert rows.first.evaluate("(row, before) => row === before", first_row)
    assert canvas.bounding_box() == face_bounds
    bounds = dialog.bounding_box()
    assert abs(bounds["x"] + bounds["width"] / 2 - width / 2) < 2
    assert abs(bounds["y"] + bounds["height"] / 2 - height / 2) < 2
    assert bounds["height"] > preview["height"]
    if width > height:
        assert bounds["width"] > preview["width"]

    page.keyboard.press("Tab")
    queue = dialog.get_by_role("list")
    expect(queue).to_be_focused()
    page.keyboard.press("PageDown")
    expect(queue).not_to_have_js_property("scrollTop", 0)
    page.keyboard.press("Tab")
    expect(close).to_be_focused()
    page.keyboard.press("Escape")
    expect(show_all).to_be_focused()
    expect(rows).to_have_text(names[:preview_count])
    expect(dialog).to_have_count(0)
    expect(readout.get_by_text("04:10", exact=True)).to_be_visible()
    assert canvas.bounding_box() == face_bounds
    assert not page_errors


def test_live_alert_modes_clear_on_missing_alert_offline_and_revoked_session(open_stage):
    page, push, send, page_errors = open_stage(authenticated=True, reduced_motion="no-preference")
    card = page.locator("#stage-hand-alert-card")
    manual = page.get_by_role("img", name="Hand raised")
    canvas = page.locator("#stage-canvas")
    face_bounds = canvas.bounding_box()
    expect(card).to_be_hidden()
    expect(manual).to_be_hidden()

    push({"kind": "manual"}, auth=True)
    expect(manual).to_be_visible()
    expect(card).to_be_hidden()
    assert manual.evaluate("(hand) => getComputedStyle(hand).animationName") != "none"
    page.emulate_media(reduced_motion="reduce")
    assert manual.evaluate("(hand) => getComputedStyle(hand).animationName") == "none"

    push({"kind": "automatic", "count": 7}, auth=True)
    expect(card.get_by_text("7", exact=True)).to_be_visible()
    expect(manual).to_be_hidden()
    expect(card.get_by_role("list")).to_have_count(0)
    expect(card.get_by_role("button")).to_have_count(0)
    expect(card.locator("#stage-hand-alert-time")).to_be_hidden()
    expect(page.locator("#stage-hand-alert-scrim")).to_be_hidden()
    assert card.bounding_box()["width"] < face_bounds["width"] / 4

    push(auth=True)
    expect(card).to_be_hidden()
    push({"kind": "automatic", "count": 2, "names": ["Ada", "Lin"]}, auth=True)
    expect(card.get_by_text("Ada", exact=True)).to_be_visible()
    page.context.set_offline(True)
    expect(card).to_be_hidden()
    assert card.get_by_text("Ada", exact=True).count() == 0
    page.context.set_offline(False)
    expect(page.locator("#stage-connection-chip")).to_have_attribute("data-state", "online")

    names = ["Sam", "Ada", "Lin", "Chris", "Pat"]
    push({"kind": "automatic", "count": len(names), "names": names}, auth=True)
    card.get_by_role("button", name="Show all").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    send("session.revoked", {"reason": "revoked_device"}, auth=True)
    expect(card).to_be_hidden()
    expect(page.locator("#stage-view")).to_be_focused()
    expect(page.locator("#stage-hand-alert-scrim")).to_be_hidden()
    assert card.get_by_text("Sam", exact=True).count() == 0
    assert canvas.bounding_box() == face_bounds
    assert not page_errors


def test_portrait_preview_shows_clock_at_three_names_with_split_seconds(open_stage):
    page, push, _, page_errors = open_stage(width=390, height=844)
    card = page.locator("#stage-hand-alert-card")
    readout = card.locator("#stage-hand-alert-time")
    primary = readout.locator("#stage-hand-alert-primary")
    seconds = readout.locator("#stage-hand-alert-seconds")
    suffix = readout.locator("#stage-hand-alert-suffix")
    names = ["Ana", "Bia", "João"]
    for count in (1, 2):
        push({"kind": "automatic", "count": count, "names": names[:count]})
        expect(card.get_by_role("listitem")).to_have_count(count)
        expect(readout).to_be_hidden()

    alert = {"kind": "automatic", "count": len(names), "names": names}
    push(
        alert,
        clockConfig={"mode": "analog", "hourFormat24h": False, "showAmpm": True},
        utcOffsetMinutes=0,
    )
    expect(readout).to_be_visible()
    expect(primary).to_have_text("10:13")
    expect(seconds).to_have_text(re.compile(r"^[0-5]\d$"))
    expect(suffix).to_have_text("PM")
    assert seconds.evaluate("(node) => parseFloat(getComputedStyle(node).fontSize)") < (
        primary.evaluate("(node) => parseFloat(getComputedStyle(node).fontSize)")
    )

    push(alert, active=True, state="stopped", plannedSeconds=5400, elapsedSeconds=3789)
    expect(primary).to_have_text("1:03")
    expect(seconds).to_have_text("09")
    expect(suffix).to_be_hidden()

    push({"kind": "automatic", "count": len(names)})
    expect(card.get_by_role("listitem")).to_have_count(0)
    expect(readout).to_be_hidden()
    push({"kind": "automatic", "count": 2, "names": names[:2]})
    expect(card.get_by_role("listitem")).to_have_count(2)
    expect(readout).to_be_hidden()
    assert not page_errors


def test_landscape_preview_shows_clock_when_its_bottom_nears_the_center(open_stage):
    page, push, _, page_errors = open_stage(width=844, height=520)
    stage = page.locator("#stage-view")
    if stage.get_attribute("data-hud-visible") == "true":
        page.mouse.click(10, 260)
    expect(stage).to_have_attribute("data-hud-visible", "false")
    card = page.locator("#stage-hand-alert-card")
    readout = card.locator("#stage-hand-alert-time")
    names = ["Ana", "Bia", "João", "Lin", "Pat"]
    push(
        {"kind": "automatic", "count": len(names), "names": names},
        clockConfig={"mode": "analog", "showSeconds": False},
        utcOffsetMinutes=0,
    )
    rows = card.get_by_role("listitem")
    expect(rows).to_have_text(names[:4])
    expect(readout).to_be_hidden()
    bounds = card.bounding_box()
    bottom = bounds["y"] + bounds["height"]
    assert bottom < 520 * 0.46

    page.set_viewport_size({"width": 844, "height": 460})
    expect(readout).to_be_visible()
    expect(readout).to_have_text("22:13")
    expect(rows).to_have_text(names[:4])
    assert 460 * 0.46 <= bottom < 460 / 2
    assert card.bounding_box() == bounds

    page.set_viewport_size({"width": 844, "height": 520})
    expect(readout).to_be_hidden()
    assert card.bounding_box() == bounds
    assert not page_errors


def test_hand_overlays_fade_on_entry_and_exit(open_stage):
    page, push, _, page_errors = open_stage(reduced_motion="no-preference")
    card = page.locator("#stage-hand-alert-card")
    manual = page.locator("#stage-hand-alert-manual")
    page.evaluate(
        """() => {
          window.presenceTransitions = [];
          for (const id of ['stage-hand-alert-card', 'stage-hand-alert-manual']) {
            const overlay = document.getElementById(id);
            new MutationObserver(() => {
              const fade = overlay.getAnimations().find(animation =>
                animation.effect.getKeyframes().every(frame => 'opacity' in frame)
              );
              if (fade) {
                window.presenceTransitions.push([id, overlay.hasAttribute('inert')]);
              }
            }).observe(overlay, {attributes: true, attributeFilter: ['hidden', 'inert']});
          }
        }"""
    )

    push({"kind": "automatic", "count": 1, "names": ["Ana"]})
    expect(card.get_by_text("Ana", exact=True)).to_be_visible()
    push({"kind": "manual"})
    expect(manual).to_be_visible()
    expect(card).to_be_hidden()
    expect(card.locator("li")).to_have_count(0)
    push()
    expect(manual).to_be_hidden()

    transitions = page.evaluate("window.presenceTransitions")
    for overlay in (card, manual):
        identifier = overlay.get_attribute("id")
        assert [identifier, False] in transitions
        assert [identifier, True] in transitions
    assert not page_errors


def test_named_alert_fits_short_landscape_and_animates_expansion(open_stage):
    page, push, _, page_errors = open_stage(reduced_motion="no-preference")
    names = [f"Person {n}" for n in range(1, 13)]
    push(
        {"kind": "automatic", "count": len(names), "names": names},
        clockConfig={"mode": "analog", "showSeconds": False},
        utcOffsetMinutes=0,
    )
    card = page.locator("#stage-hand-alert-card")
    expect(card.get_by_text("22:13", exact=True)).to_be_visible()
    rows = card.get_by_role("listitem")
    expect(rows).to_have_text(names[:4])
    page.set_viewport_size({"width": 650, "height": 230})
    expect(rows).to_have_text(names[:2])
    footer = card.locator("footer")
    last_row = rows.last.bounding_box()
    assert last_row["y"] + last_row["height"] <= footer.bounding_box()["y"]

    show_all = footer.get_by_role("button")
    assert show_all.evaluate(
        """(button) => {
          button.click();
          return button.closest('section').getAnimations().length > 0;
        }"""
    )
    dialog = page.get_by_role("dialog")
    expect(dialog).to_have_css("transform", "none")
    expect(rows).to_have_text(names)
    page.mouse.click(2, 115)
    expect(dialog).to_have_count(0)
    expect(show_all).to_be_focused()
    expect(rows).to_have_text(names[:2])
    assert not page_errors


def test_delayed_auth_bootstrap_cannot_restore_revoked_names(open_stage):
    page, _, send, page_errors = open_stage(authenticated=True)
    bootstrap_url = f"{TIMER_URL}api/bootstrap"
    pending = []
    page.route(bootstrap_url, lambda route: pending.append(route))
    with page.expect_request(bootstrap_url):
        page.evaluate("window.dispatchEvent(new Event('online'))")

    def released_bootstrap(route):
        route.fulfill(
            json={
                "authenticated": True,
                "controlAvailable": True,
                "serverInstanceId": "test-timer",
                "live": {
                    "liveRevision": 3,
                    "serverEpochMs": 1_700_000_000_000,
                    "handAlert": {"kind": "automatic", "count": 1, "names": ["Private Name"]},
                },
            }
        )

    send("session.revoked", {"reason": "revoked_device"}, auth=True)
    expect(page.locator("#stage-signin-button")).to_be_visible()
    expect(page.locator("#stage-connection-chip")).to_have_attribute("data-state", "online")
    page.evaluate(
        """() => {
          window.sawRevokedName = false;
          new MutationObserver((mutations) => {
            for (const {addedNodes} of mutations) {
              for (const node of addedNodes) {
                window.sawRevokedName ||= node.textContent.includes("Private Name");
              }
            }
          }).observe(document.getElementById('stage-hand-alert-list'), {childList: true});
        }"""
    )
    with page.expect_response(bootstrap_url) as response:
        released_bootstrap(pending.pop())
    response.value.finished()
    page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    assert not page.evaluate("window.sawRevokedName")
    expect(page.locator("#stage-signin-button")).to_be_visible()
    assert not page_errors
