from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest

from solin.core.remote_control.server import _CSP
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

    def open_stage(
        *,
        width=844,
        height=390,
        authenticated=False,
        has_touch=False,
        reduced_motion="reduce",
        schedule=None,
        catalog=(),
        on_command=None,
    ):
        context = browser.new_context(
            viewport={"width": width, "height": height},
            reduced_motion=reduced_motion,
            has_touch=has_touch,
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

        def payload(*, private=False):
            result = {**session, "eventSequence": sequence, "live": live(sequence)}
            if private and authenticated and schedule is not None:
                result["schedule"] = schedule
            return result

        def route_http(route):
            path = urlsplit(route.request.url).path.removeprefix(TIMER_PATH)
            if not path:
                route.fulfill(body=assets.index_html, content_type="text/html", headers={"Content-Security-Policy": _CSP})
            elif path in ("api/public", "api/bootstrap"):
                route.fulfill(json=payload(private=path == "api/bootstrap"))
            elif path == "api/schedule" and authenticated:
                route.fulfill(json=schedule)
            elif path == "api/custom-schedules" and authenticated:
                route.fulfill(json={"schedules": catalog})
            elif path == "api/commands" and authenticated and on_command:
                route.fulfill(json=on_command(route.request.post_data_json))
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
            send("session.snapshot" if auth else "public.snapshot", payload(private=auth), auth=auth)

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


def _part(part_id, title, number=1):
    return {"id": part_id, "title": title, "displayNumber": number, "plannedSeconds": 300}


def _schedule(**fields):
    return {
        "scheduleRevision": 17,
        "weekMonday": "2023-11-13",
        "weekLabel": "13–19 November",
        "capabilities": {"manageCustomSchedules": True, "managePacing": False},
        **fields,
    }


def test_pacing_window_is_shown_in_control_workspace_and_absent_from_expanded_clock(open_stage):
    schedule = _schedule(
        pacingMode="monitor",
        pacingAvailability="ready",
        pacing={
            "start_epoch": 1_700_000_000,
            "end_epoch": 1_700_000_300,
            "projection_anchor_epoch": 1_700_000_000,
            "projection_tail_seconds": 360,
            "tolerance_seconds": 10,
            "complete": False,
        },
    )
    public, _, _, public_errors = open_stage(schedule=schedule)
    expect(public.locator("#hero-pacing")).to_be_hidden()

    page, _, send, page_errors = open_stage(authenticated=True, schedule=schedule)
    pacing = page.locator("#hero-pacing")
    expect(pacing).to_be_hidden()
    page.locator("#stage-control-button").click()
    expect(pacing).to_be_visible()
    expect(pacing).to_have_attribute("data-tone", "behind")
    expect(page.locator("#pacing-window-time")).to_have_text(re.compile(r"\d{1,2}:\d{2}.*–.*\d{1,2}:\d{2}"))
    expect(page.locator("#pacing-mode")).to_be_hidden()
    send("timer.schedule", schedule | {"scheduleRevision": 18, "pacingMode": "off"}, auth=True)
    expect(pacing).to_be_hidden()
    send("timer.schedule", schedule | {"scheduleRevision": 19}, auth=True)
    expect(pacing).to_be_visible()
    page.locator("#control-stage-button").click()
    expect(pacing).to_be_hidden()
    expect(page.locator("#pacing-window-time")).to_be_empty()
    send("session.revoked", {"reason": "revoked_device"}, auth=True)
    expect(page.locator("#stage-signin-button")).to_be_visible()
    assert not public_errors
    assert not page_errors


@pytest.mark.parametrize(("width", "height"), [(320, 700), (1800, 820)])
def test_timer_workspace_selects_then_controls_a_part_without_layout_overflow(
    open_stage, width, height
):
    commands = []

    def on_command(command):
        commands.append(command)
        return {"ok": True, "scheduleRevision": 17}

    parts = [
        _part("p1", "Opening comments") | {"state": "stopped", "resultLabel": "04:30"},
        _part("p2", "Schedule discussion", 2) | {"titleOverridden": True, "canonicalTitle": "Discussion"},
        *[_part(f"p{n}", f"Part {n}", n) for n in range(3, 19)],
    ]
    page, push, _, page_errors = open_stage(
        width=width,
        height=height,
        authenticated=True,
        schedule=_schedule(parts=parts),
        on_command=on_command,
    )
    page.locator("#stage-control-button").click()
    expect(page.get_by_role("button", name="Reset results")).to_have_count(0)
    target = page.locator('[data-action="select-part"][data-part-id="p2"]')
    title = page.locator("#hero-title")
    target.click()
    expect(target).to_have_attribute("aria-pressed", "true")
    expect(title).to_have_text(parts[1]["title"])
    assert not commands
    assert page.evaluate("document.documentElement.scrollWidth === innerWidth")

    if width > 900:
        bounds = page.locator("#control-container").bounding_box()
        assert bounds["width"] > width - 100
        hero = page.locator("#hero-card")
        initial_y = hero.bounding_box()["y"]
        assert abs(initial_y - page.locator("#meeting-bar").bounding_box()["y"]) < 1
        page.locator("#main-content").evaluate("(main) => main.scrollTop = 200")
        expect(hero).to_be_in_viewport()
        main_y = page.locator("#main-content").bounding_box()["y"]
        assert main_y <= hero.bounding_box()["y"] < initial_y
        page.locator("#main-content").evaluate("(main) => main.scrollTop = 0")

    expect(page.locator("#hero-adjust-button")).to_be_hidden()
    page.locator("#hero-edit-button").click()
    edit = page.get_by_role("dialog", name="Edit part")
    edit.get_by_role("button", name="Restore default title").click()
    edit.get_by_role("textbox", name=re.compile("Duration")).fill("06:00")
    with page.expect_response(f"{TIMER_URL}api/commands"):
        edit.get_by_role("button", name="Save").click()
    assert {key: commands[-1][key] for key in ("type", "partId", "title", "plannedSeconds")} == {
        "type": "edit_part", "partId": "p2", "title": None, "plannedSeconds": 360,
    }
    with page.expect_response(f"{TIMER_URL}api/commands"):
        page.locator("#hero-start-button").click()
    assert {key: commands[-1][key] for key in ("type", "partId", "scheduleRevision")} == {
        "type": "start_part",
        "partId": "p2",
        "scheduleRevision": 17,
    }
    push(auth=True, active=True, state="running", activePartId="p2", plannedSeconds=300)
    expect(page.locator("#hero-stop-button")).to_be_enabled()
    expect(page.locator("#hero-stop-button")).to_be_focused()
    expect(page.locator("#tab-schedules")).to_be_disabled()
    page.locator("#hero-adjust-button").click()
    expect(page.locator("#hero-nudge-group")).to_be_visible()
    expect(page.locator("#hero-plus-second")).to_be_enabled()
    with page.expect_response(f"{TIMER_URL}api/commands"):
        page.get_by_role("button", name="Start next part").click()
    assert commands[-1]["type"] == "start_part"
    assert commands[-1]["partId"] == "p3"
    if width < 500:
        page.locator('[data-action="select-part"][data-part-id="p18"]').scroll_into_view_if_needed()
        expect(page.locator(".sticky-timer-shell")).to_have_attribute("data-stuck", "true")
        expect(page.locator("#sticky-stop-button")).to_be_in_viewport()
        with page.expect_response(f"{TIMER_URL}api/commands"):
            page.locator("#sticky-stop-button").click()
        assert commands[-1]["type"] == "stop_part"

    push(auth=True, active=True, state="stopped", activePartId="p2", plannedSeconds=300)
    expect(page.locator("#hero-nudge-group")).to_be_hidden()
    expect(page.locator("#tab-schedules")).to_be_enabled()
    assert not page_errors


@pytest.mark.parametrize("still_exists", [True, False], ids=["restore", "deleted"])
def test_returning_to_custom_schedules_uses_the_latest_catalog(open_stage, still_exists):
    first_part = _part("opening", "Opening")
    second_part = _part("practice", "Practice")
    first = {"id": "first", "title": "First schedule", "revision": 2,
             "entries": [first_part | {"type": "part"}]}
    second = {"id": "second", "title": "Second schedule", "revision": 4,
              "entries": [second_part | {"type": "part"}]}
    catalog = [first, second]
    schedule = _schedule(customSchedule=first, parts=[first_part])
    commands = []

    def on_command(command):
        commands.append(command)
        revision = schedule["scheduleRevision"] + 1
        if command["type"] == "select_meeting_schedule":
            schedule.update(scheduleRevision=revision, customSchedule=None, parts=[])
        else:
            assert command["type"] == "select_custom_schedule"
            custom = next(item for item in catalog if item["id"] == command["scheduleId"])
            assert command["customRevision"] == custom["revision"]
            schedule.update(scheduleRevision=revision, customSchedule=custom, parts=custom["entries"])
        return {"ok": True, "scheduleRevision": revision}

    page, _, _, page_errors = open_stage(
        height=780, authenticated=True, schedule=schedule, catalog=catalog, on_command=on_command,
    )
    page.locator("#stage-control-button").click()
    selector = page.locator("#custom-selector")
    selector.select_option(second["id"])
    expect(selector).to_have_value(second["id"])
    page.locator("#tab-meetings").click()
    expect(page.locator("#meeting-bar")).to_be_visible()

    second["revision"] += 1
    if not still_exists:
        catalog.remove(second)
    expected = second if still_exists else first
    page.locator("#tab-schedules").click()
    expect(selector).to_have_value(expected["id"])
    expect(page.locator("#hero-title")).to_have_text(expected["entries"][0]["title"])
    assert [command["type"] for command in commands] == [
        "select_custom_schedule", "select_meeting_schedule", "select_custom_schedule",
    ]
    assert not page_errors


@pytest.mark.parametrize("has_touch", [False, True], ids=["mouse", "touch"])
def test_custom_workspace_creates_and_submits_an_atomic_revision_bound_draft(open_stage, has_touch):
    planned = _part("part-1", "Warm-up") | {"plannedSeconds": 45}
    section = {
        "id": "section-1", "type": "section", "title": "Practice",
        "icon": "book", "color": "#38b27b", "parts": [],
    }
    custom = {
        "id": "workshop", "title": "Workshop", "revision": 2,
        "entries": [planned | {"type": "part"}, section],
    }
    schedule = _schedule()
    catalog = []
    commands = []

    def on_command(command):
        commands.append(command)
        if command["type"] == "create_custom_schedule":
            catalog.append(custom)
            schedule["scheduleRevision"] = 18
            return {"ok": True, "scheduleRevision": 18, "createdScheduleId": custom["id"]}
        if command["type"] == "select_custom_schedule":
            schedule.update(scheduleRevision=19, customSchedule=custom, parts=[planned])
            return {"ok": True, "scheduleRevision": 19}
        return {"ok": False, "error": {"code": "schedule_stale"}}

    page, _, _, page_errors = open_stage(
        width=390,
        height=844,
        authenticated=True,
        has_touch=has_touch,
        reduced_motion="no-preference",
        schedule=schedule,
        catalog=catalog,
        on_command=on_command,
    )
    page.locator("#stage-control-button").click()
    page.locator("#tab-schedules").click()
    page.locator("#catalog-new").click()
    create = page.locator("#workspace-dialog")
    create.locator('[name="title"]').fill(custom["title"])
    create.get_by_role("checkbox", name="Add part").check()
    create.locator('[name="partTitle"]').fill(planned["title"])
    create.locator('[name="partDuration"]').fill("00:45")
    create.locator("#workspace-submit").click()

    expect(page.locator("#custom-selector")).to_have_value(custom["id"])
    expect(page.locator("#meeting-bar")).to_be_hidden()
    assert commands[0]["firstPart"] == {"title": planned["title"], "plannedSeconds": 45}
    assert commands[1]["scheduleRevision"] == 18
    assert commands[1]["customRevision"] == custom["revision"]
    badge = page.locator('[data-section="section-1"] .section-icon-badge')
    expect(badge.locator("use")).to_have_attribute("href", "#icon-sec-book")
    expect(badge).to_be_visible()

    page.locator("#custom-organize").click()
    expect(page.locator("#hero-card")).to_be_hidden()
    page.get_by_role("button", name="Edit section").click()
    section_editor = page.get_by_role("dialog", name="Edit section")
    expect(section_editor.locator(".section-icon-badge")).to_have_css(
        "background-color", badge.evaluate("(node) => getComputedStyle(node).backgroundColor")
    )
    section_editor.get_by_title("Crimson").click()
    assert section_editor.locator(".section-icon-badge").evaluate(
        "(node) => getComputedStyle(node).backgroundColor"
    ) != badge.evaluate("(node) => getComputedStyle(node).backgroundColor")
    expect(section_editor.get_by_role("radio", name="Crimson")).to_be_checked()
    section_editor.get_by_title("Microphone").click()
    expect(section_editor.locator(".section-icon-badge use")).to_have_attribute("href", "#icon-sec-microphone")
    hex_color = section_editor.get_by_role("textbox", name="Custom hex")
    hex_color.fill("#abc")
    section_editor.get_by_role("button", name="Save").click()
    expect(section_editor.get_by_role("alert")).to_be_visible()
    hex_color.fill("#4a6b8c")
    section_editor.get_by_role("button", name="Save").click()
    expect(badge.locator("use")).to_have_attribute("href", "#icon-sec-microphone")
    item = page.locator('[data-drag-id="part-1"]')
    handle = item.locator("[data-drag-handle]" if has_touch else ".part-title").bounding_box()
    zone = page.locator('[data-drag-container="section-1"]')
    empty = zone.locator(".section-empty")
    expect(empty).to_have_css("border-top-style", "dashed")
    start_x, start_y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2

    def assert_drag_visual():
        ghost = page.locator(".schedule-drag-ghost")
        expect(ghost).to_be_visible()
        expect(item).to_be_hidden()
        for _ in range(3):
            page.evaluate("() => new Promise(requestAnimationFrame)")
            bounds = ghost.bounding_box()
            assert bounds["x"] >= 0
            assert bounds["y"] <= end_y <= bounds["y"] + bounds["height"]

    if has_touch:
        session = page.context.new_cdp_session(page)
        session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": start_x, "y": start_y}]})
        session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": start_x, "y": start_y + 18}]})
        expect(item).to_be_hidden()
        drop = zone.bounding_box()
        end_x, end_y = drop["x"] + drop["width"] / 2, drop["y"] + drop["height"] / 2
        session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": end_x, "y": end_y}]})
        assert_drag_visual()
        session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        session.detach()
    else:
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.move(start_x, start_y + 18)
        expect(item).to_be_hidden()
        drop = zone.bounding_box()
        end_x, end_y = drop["x"] + drop["width"] / 2, drop["y"] + drop["height"] / 2
        page.mouse.move(end_x, end_y, steps=8)
        assert_drag_visual()
        page.mouse.up()
    moved = zone.locator('[data-drag-id="part-1"]')
    expect(moved).to_be_visible()
    moved.get_by_role("button", name="Edit part").click()
    editor = page.get_by_role("dialog", name="Edit part")
    editor.get_by_role("textbox", name="Name").fill("Briefing")
    editor.get_by_role("button", name="Save").click()
    expect(moved.get_by_text("Briefing")).to_be_visible()
    assert len(commands) == 2
    page.locator("#organizer-save").click()
    expect(page.locator("#organizer-error")).to_be_visible()
    expect(page.locator("#organizer-save")).to_be_disabled()
    expect(moved.get_by_text("Briefing")).to_be_visible()
    assert len(commands) == 3
    draft = commands[-1]
    assert draft["type"] == "replace_custom_schedule_entries"
    assert draft["scheduleRevision"] == 19
    assert draft["customRevision"] == custom["revision"]
    assert draft["entries"] == [
        section | {"icon": "microphone", "color": "#4a6b8c",
                   "parts": [{"id": "part-1", "type": "part", "title": "Briefing", "plannedSeconds": 45}]}
    ]
    assert not page_errors


def test_missing_window_shortcut_saves_times_for_the_selected_schedule(open_stage):
    part = _part("intro", "Introduction")
    review = _part("review", "Review", 2) | {"adjustmentWeight": 25}
    short = _part("closing", "Closing", 3) | {"plannedSeconds": 45}
    parts = [part, review, short]
    custom = {"id": "workshop", "title": "Workshop", "revision": 2,
              "entries": [entry | {"type": "part"} for entry in parts]}
    caps = {"manageCustomSchedules": True, "managePacing": True}
    schedule = _schedule(
        parts=parts, customSchedule=custom, capabilities=caps,
        pacingMode="monitor", pacingAvailability="missing_window",
    )
    commands = []

    def on_command(command):
        commands.append(command)
        schedule.update(scheduleRevision=18, pacingAvailability="no_projection")
        return {"ok": True, "scheduleRevision": 18}

    page, _, _, page_errors = open_stage(
        width=320, height=780, authenticated=True, schedule=schedule, catalog=[custom],
        on_command=on_command,
    )
    page.locator("#stage-control-button").click()
    shortcut = page.locator("#hero-pacing").get_by_role("button", name="Set schedule time")
    shortcut.click()
    timing = page.get_by_role("dialog", name="Schedule timing and pacing")
    intro = timing.get_by_role("checkbox", name=re.compile("Introduction"))
    recap = timing.get_by_role("checkbox", name=re.compile("Review"))
    expect(timing.get_by_role("checkbox", name=re.compile("Closing"))).to_be_disabled()
    expect(recap).to_be_checked()
    weight = timing.get_by_role("group", name="Introduction").get_by_label(re.compile("Adjustment weight"))
    expect(weight).to_be_hidden()
    intro.check()
    timing.get_by_text("Advanced options", exact=True).click()
    expect(weight).to_have_value("1")
    weight.fill("0")
    expect(intro).not_to_be_checked()
    weight.fill("35")
    expect(intro).to_be_checked()
    recap.uncheck()
    timing.get_by_label("Start time").fill("19:30")
    timing.get_by_label("End time").fill("20:30")
    assert timing.evaluate("dialog => dialog.scrollWidth <= dialog.clientWidth")
    timing.get_by_role("button", name="Save").click()
    expect(timing).to_be_hidden()
    expect(shortcut).to_be_hidden()
    assert len(commands) == 1
    assert (commands[0]["type"], commands[0]["scheduleId"], commands[0]["customRevision"]) == (
        "replace_custom_timing_settings", custom["id"], custom["revision"],
    )
    assert (commands[0]["startTime"], commands[0]["endTime"]) == ("19:30", "20:30")
    assert commands[0]["weights"] == {"intro": 35, "review": 0}
    assert not page_errors


def test_pacing_is_read_only_and_adjustment_is_shown_in_the_running_clock(open_stage):
    part = _part("adjustable", "Adjustable")
    fixed = _part("fixed", "Fixed", 2)
    custom = {"id": "workshop", "title": "Workshop", "revision": 2,
              "entries": [entry | {"type": "part"} for entry in (part, fixed)]}
    schedule = _schedule(parts=[part, fixed], customSchedule=custom,
                         pacingMode="automatic", pacingAvailability="missing_window")
    page, push, send, page_errors = open_stage(
        height=780, authenticated=True, schedule=schedule, catalog=[custom],
    )
    page.locator("#stage-control-button").click()
    expect(page.locator("#hero-pacing")).to_contain_text("in the Solin app")
    expect(page.locator("#pacing-set-time")).to_be_hidden()
    expect(page.locator("#pacing-mode")).to_be_hidden()
    send("timer.schedule", schedule | {
        "scheduleRevision": 18,
        "parts": [part | {"state": "running", "effectiveSeconds": 240}, fixed],
        "pacingAvailability": "ready",
        "pacing": {
            "start_epoch": 1_700_000_000,
            "end_epoch": 1_700_000_600,
            "projection_anchor_epoch": 1_700_000_240,
            "projection_tail_seconds": 300,
            "tolerance_seconds": 30,
            "complete": False,
        },
    }, auth=True)
    push(auth=True, active=True, state="running", activePartId=part["id"], plannedSeconds=240)
    expect(page.locator("#hero-target-label")).to_have_text(re.compile(r"04:00.*05:00"))
    expect(page.locator("#hero-pacing")).to_have_attribute("data-tone", "ahead")
    expect(page.locator("#schedule-list").get_by_text(re.compile(r"^Adjusted\b"))).to_have_count(0)
    assert not page_errors


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
