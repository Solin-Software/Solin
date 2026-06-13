"""Unit tests for the pure timer domain (app.core.timer)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from PySide6.QtCore import QCoreApplication

from app.core.timer.models import (
    AnalogClockStyle,
    ClockConfig,
    ClockMode,
    Direction,
    MeetingPart,
    MeetingSchedule,
    MeetingType,
    PartKind,
    PartState,
    PartTimerDisplay,
    Section,
    FIXED_TOTAL_SECONDS,
)
from app.core.timer.schedule_factory import (
    adjust_part,
    build_default_schedule,
    configurable_count_for,
    configurable_total_seconds,
    normalize_schedule,
    redistribute_section,
    schedule_has_timing_data,
    set_section_part_count,
)
from app.core.timer.part_titles import (
    BIBLE_READING_TITLE,
    CBS_TITLE,
    CONCLUDING_COMMENTS_TITLE,
    GENERIC_INDEXED_PART_TITLE,
    OPENING_COMMENTS_TITLE,
    PUBLIC_TALK_INDEXED_PART_TITLE,
    PUBLIC_TALK_TITLE,
    SPIRITUAL_GEMS_TITLE,
    TREASURES_INDEXED_PART_TITLE,
    TREASURES_TALK_TITLE,
    WATCHTOWER_STUDY_INDEXED_PART_TITLE,
    WATCHTOWER_STUDY_TITLE,
    PART_TITLE_SOURCES,
)
from app.core.timer.engine import TimerEngine
from app.core.timer.store import TimerStore
from app.core.profiles import settings as _ps


_WEEK = date(2026, 6, 1)


def _app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


# ── Schedule defaults ─────────────────────────────────────────────────────────

def test_midweek_default_section_totals():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    assert [p.section for p in sch.parts] == [
        Section.OPENING_COMMENTS,
        Section.TREASURES,
        Section.TREASURES,
        Section.TREASURES,
        Section.MINISTRY,
        Section.MINISTRY,
        Section.MINISTRY,
        Section.LIVING,
        Section.LIVING,
        Section.LIVING,
        Section.CONCLUDING_COMMENTS,
    ]
    assert sch.parts[0].title == OPENING_COMMENTS_TITLE
    assert sch.parts[0].planned_seconds == 60
    assert sch.parts[-1].title == CONCLUDING_COMMENTS_TITLE
    assert sch.parts[-1].planned_seconds == 3 * 60
    assert sch.section_total_seconds(Section.MINISTRY) == FIXED_TOTAL_SECONDS[Section.MINISTRY]
    assert configurable_total_seconds(sch, Section.LIVING) == FIXED_TOTAL_SECONDS[Section.LIVING]
    assert sch.section_total_seconds(Section.LIVING) == 45 * 60
    assert sch.count_for(Section.TREASURES) == 3
    assert [p.title for p in sch.parts_in(Section.TREASURES)] == [
        TREASURES_TALK_TITLE,
        SPIRITUAL_GEMS_TITLE,
        BIBLE_READING_TITLE,
    ]
    assert [p.title for p in sch.parts_in(Section.MINISTRY)] == [
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
    ]
    living = sch.parts_in(Section.LIVING)
    assert [p.kind for p in living] == [PartKind.STANDARD, PartKind.STANDARD, PartKind.CBS]
    assert [p.title for p in living if p.kind is PartKind.STANDARD] == [
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
    ]
    assert living[-1].title == CBS_TITLE
    assert living[-1].planned_seconds == 30 * 60


def test_weekend_default_parts():
    sch = build_default_schedule(_WEEK, MeetingType.WEEKEND)
    sections = [p.section for p in sch.parts]
    assert sections == [Section.PUBLIC_TALK, Section.WATCHTOWER]
    assert [p.title for p in sch.parts] == [PUBLIC_TALK_TITLE, WATCHTOWER_STUDY_TITLE]
    assert sch.part_by_id(sch.parts[0].id).planned_seconds == 30 * 60
    assert sch.part_by_id(sch.parts[1].id).planned_seconds == 60 * 60


def test_timer_section_palette_uses_meeting_section_hues():
    from app.core.meetings.section_meta import SECTION_META
    from app.core.meetings.colors import section_colors
    from app.widgets.timer_bridge import _section_palette

    section_codes = {
        Section.OPENING_COMMENTS: "opening_comments",
        Section.TREASURES: "tgw",
        Section.MINISTRY: "ayfm",
        Section.LIVING: "lac",
        Section.CONCLUDING_COMMENTS: "concluding_comments",
        Section.PUBLIC_TALK: "public_talk",
        Section.WATCHTOWER: "wt",
    }

    for section, code in section_codes.items():
        colors = section_colors(SECTION_META[code][1])
        palette = _section_palette(section)
        assert palette["accent"] == colors["accent"]
        assert palette["text"] == colors["text"]
        assert palette["badge"] == colors["badge"]
        assert palette["border"] == colors["border"]


def test_timer_parts_model_uses_global_display_numbers():
    _app()
    from app.widgets.timer_bridge import _parts_model_for_schedule

    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    rows = _parts_model_for_schedule(sch)

    assert [row["displayNumber"] for row in rows] == list(range(1, len(rows) + 1))
    assert rows[0]["section"] == Section.OPENING_COMMENTS.value
    assert rows[0]["title"] == QCoreApplication.translate(
        "_TimerPart",
        OPENING_COMMENTS_TITLE,
    )
    assert not rows[0]["showSectionHeader"]
    assert rows[0]["sectionTotalLabel"] == ""
    assert rows[1]["section"] == Section.TREASURES.value
    assert rows[1]["showSectionHeader"]
    assert rows[4]["section"] == Section.MINISTRY.value
    assert rows[4]["title"] == "Part 1"
    assert rows[5]["title"] == "Part 2"
    assert rows[-2]["section"] == Section.LIVING.value
    assert rows[-2]["title"] == QCoreApplication.translate(
        "_TimerPart",
        CBS_TITLE,
    )
    assert rows[-1]["section"] == Section.CONCLUDING_COMMENTS.value
    assert rows[-1]["title"] == QCoreApplication.translate(
        "_TimerPart",
        CONCLUDING_COMMENTS_TITLE,
    )
    assert not rows[-1]["showSectionHeader"]
    assert rows[-1]["displayNumber"] == len(rows)


def test_timer_parts_model_formats_default_timer_part_titles():
    _app()
    from app.widgets.timer_bridge import _parts_model_for_schedule

    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    set_section_part_count(sch, Section.TREASURES, 4)
    sch.parts_in(Section.MINISTRY)[0].title = "Custom Ministry Title"

    rows = _parts_model_for_schedule(sch)

    assert rows[0]["title"] == QCoreApplication.translate("_TimerPart", OPENING_COMMENTS_TITLE)
    assert rows[1]["title"] == QCoreApplication.translate("_TimerPart", TREASURES_TALK_TITLE)
    assert rows[2]["title"] == QCoreApplication.translate("_TimerPart", SPIRITUAL_GEMS_TITLE)
    assert rows[3]["title"] == QCoreApplication.translate("_TimerPart", BIBLE_READING_TITLE)
    assert rows[4]["title"] == "Treasures Part 4"
    assert rows[5]["title"] == "Custom Ministry Title"
    assert rows[6]["title"] == "Part 2"


def test_timer_part_i18n_sources_cover_canonical_titles():
    from app.core.i18n.timer_part_titles import TIMER_PART_TITLE_SOURCES

    assert set(TIMER_PART_TITLE_SOURCES) == set(PART_TITLE_SOURCES)


def test_timer_parts_model_formats_started_time_with_locale_pattern():
    import time as _time
    from app.widgets.timer_bridge import _parts_model_for_schedule

    epoch = _time.mktime((2026, 1, 1, 5, 4, 3, 0, 0, -1))
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    sch.parts[0].first_started_epoch = epoch

    rows = _parts_model_for_schedule(sch, "HH.mm.ss")

    assert rows[0]["startedLabel"] == "05.04.03"


def test_timer_pdf_rows_include_finished_time_from_started_plus_duration():
    import time as _time
    from app.core.rendering.timer_report_pdf import build_timer_pdf_rows

    epoch = _time.mktime((2026, 1, 1, 5, 4, 3, 0, 0, -1))
    sch = build_default_schedule(_WEEK, MeetingType.WEEKEND)
    part = sch.parts[0]
    part.first_started_epoch = epoch
    part.accumulated_seconds = 30 * 60 + 15
    part.state = PartState.STOPPED

    rows = build_timer_pdf_rows(sch, time_format="HH:mm:ss")

    assert rows[0].started_label == "05:04:03"
    assert rows[0].finished_label == "05:34:18"
    assert rows[0].result_label == "30:15"
    assert rows[0].delta_label == "+00:15"
    assert rows[1].status_label == "Not started"


def test_timer_pdf_rows_hide_section_headers_for_standalone_midweek_comments():
    from app.core.rendering.timer_report_pdf import build_timer_pdf_rows

    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)

    rows = build_timer_pdf_rows(sch, time_format="HH:mm:ss")

    assert rows[0].section is Section.OPENING_COMMENTS
    assert not rows[0].show_section_header
    assert rows[1].section is Section.TREASURES
    assert rows[1].show_section_header
    assert rows[-1].section is Section.CONCLUDING_COMMENTS
    assert not rows[-1].show_section_header


def test_timer_pdf_summary_counts_completed_parts_by_time_accuracy():
    from app.core.rendering.timer_report_pdf import build_timer_pdf_summary

    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    on_time = sch.parts[0]
    on_time.state = PartState.STOPPED
    on_time.accumulated_seconds = on_time.planned_seconds

    late = sch.parts[1]
    late.state = PartState.STOPPED
    late.accumulated_seconds = late.planned_seconds + 1

    early = sch.parts[2]
    early.state = PartState.STOPPED
    early.accumulated_seconds = early.planned_seconds - 1

    running = sch.parts[3]
    running.state = PartState.RUNNING
    running.started_at_epoch = 1_000.0

    summary = build_timer_pdf_summary(sch, now_epoch=1_000.0)

    assert summary.within_time_count == 2
    assert summary.over_time_count == 1


def test_format_time_with_seconds_supports_locale_patterns():
    import time as _time
    from app.core.i18n.date import format_datetime, format_time_with_seconds

    epoch = _time.mktime((2026, 1, 1, 5, 4, 3, 0, 0, -1))

    assert format_time_with_seconds(epoch, "HH:mm:ss") == "05:04:03"
    assert format_time_with_seconds(epoch, "h:mm:ss") == "5:04:03"
    assert format_time_with_seconds(epoch, "%H.%M.%S") == "05.04.03"
    assert format_time_with_seconds(epoch, "") == "05:04:03"
    assert format_datetime(epoch, "dd/MM/yyyy HH:mm") == "01/01/2026 05:04"
    assert format_datetime(epoch, "%Y-%m-%d %H:%M") == "2026-01-01 05:04"


def test_time_formatting_falls_back_for_invalid_user_patterns(monkeypatch):
    import time as _time
    from app.core.i18n import date as date_i18n

    epoch = _time.mktime((2026, 1, 1, 5, 4, 3, 0, 0, -1))

    def reject_pattern(_epoch, _pattern):
        raise ValueError("invalid pattern")

    monkeypatch.setattr(date_i18n, "_format_time_java_pattern", reject_pattern)

    assert date_i18n.format_time_with_seconds(epoch, "HH:mm:ss") == "05:04:03"
    assert date_i18n.format_datetime(epoch, "dd/MM/yyyy HH:mm") == "2026-01-01 05:04"


def test_time_formatting_does_not_hide_unexpected_errors(monkeypatch):
    from app.core.i18n import date as date_i18n

    def fail_unexpectedly(_epoch, _pattern):
        raise RuntimeError("programming error")

    monkeypatch.setattr(date_i18n, "_format_time_java_pattern", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="programming error"):
        date_i18n.format_time_with_seconds(0, "HH:mm:ss")


# ── Redistribution ────────────────────────────────────────────────────────────

def test_redistribute_keeps_ministry_total():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    first = sch.parts_in(Section.MINISTRY)[0]
    redistribute_section(sch, first.id, first.planned_seconds + 120)
    assert sch.section_total_seconds(Section.MINISTRY) == FIXED_TOTAL_SECONDS[Section.MINISTRY]
    # The following part absorbed the +120s.
    assert first.planned_seconds == _ministry_first_default() + 120


def test_adjust_last_part_borrows_backward():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    living = [p for p in sch.parts_in(Section.LIVING) if p.kind is PartKind.STANDARD]
    last = living[-1]
    adjust_part(sch, last.id, +180)
    assert configurable_total_seconds(sch, Section.LIVING) == FIXED_TOTAL_SECONDS[Section.LIVING]
    assert last.planned_seconds == _living_last_default() + 180


def test_cbs_is_not_part_of_living_redistribution():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    cbs = sch.parts_in(Section.LIVING)[-1]
    first_living = sch.parts_in(Section.LIVING)[0]

    redistribute_section(sch, first_living.id, first_living.planned_seconds + 120)
    assert cbs.kind is PartKind.CBS
    assert cbs.planned_seconds == 30 * 60
    assert configurable_total_seconds(sch, Section.LIVING) == FIXED_TOTAL_SECONDS[Section.LIVING]

    redistribute_section(sch, cbs.id, 25 * 60)
    assert cbs.planned_seconds == 25 * 60
    assert configurable_total_seconds(sch, Section.LIVING) == FIXED_TOTAL_SECONDS[Section.LIVING]


def test_redistribute_respects_minimum_floor():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    parts = sch.parts_in(Section.MINISTRY)
    # Grow the first part beyond the section total; others clamp at the floor
    # but the section total is still preserved.
    redistribute_section(sch, parts[0].id, FIXED_TOTAL_SECONDS[Section.MINISTRY])
    assert sch.section_total_seconds(Section.MINISTRY) == FIXED_TOTAL_SECONDS[Section.MINISTRY]
    assert all(p.planned_seconds >= 60 for p in parts)


def test_non_fixed_section_sets_directly():
    sch = build_default_schedule(_WEEK, MeetingType.WEEKEND)
    talk = sch.parts_in(Section.PUBLIC_TALK)[0]
    redistribute_section(sch, talk.id, 20 * 60)
    assert talk.planned_seconds == 20 * 60


def test_set_part_count_preserves_fixed_total():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    set_section_part_count(sch, Section.MINISTRY, 5)
    assert sch.count_for(Section.MINISTRY) == 5
    assert sch.section_total_seconds(Section.MINISTRY) == FIXED_TOTAL_SECONDS[Section.MINISTRY]
    assert [p.title for p in sch.parts_in(Section.MINISTRY)] == [
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
    ]
    # Canonical ordering preserved: treasures, then ministry, then living.
    order = [p.section for p in sch.parts]
    assert order.index(Section.TREASURES) < order.index(Section.MINISTRY) < order.index(Section.LIVING)


def test_set_living_part_count_preserves_cbs():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)

    set_section_part_count(sch, Section.LIVING, 4)

    living = sch.parts_in(Section.LIVING)
    assert [p.kind for p in living] == [
        PartKind.STANDARD,
        PartKind.STANDARD,
        PartKind.STANDARD,
        PartKind.STANDARD,
        PartKind.CBS,
    ]
    assert configurable_count_for(sch, Section.LIVING) == 4
    assert configurable_total_seconds(sch, Section.LIVING) == FIXED_TOTAL_SECONDS[Section.LIVING]
    assert [p.title for p in living if p.kind is PartKind.STANDARD] == [
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
        GENERIC_INDEXED_PART_TITLE,
    ]
    assert living[-1].title == CBS_TITLE
    assert living[-1].planned_seconds == 30 * 60


def test_set_treasures_part_count_uses_translatable_indexed_fallback():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)

    set_section_part_count(sch, Section.TREASURES, 4)

    treasures = sch.parts_in(Section.TREASURES)
    assert [p.title for p in treasures[:3]] == [
        TREASURES_TALK_TITLE,
        SPIRITUAL_GEMS_TITLE,
        BIBLE_READING_TITLE,
    ]
    assert treasures[3].title == TREASURES_INDEXED_PART_TITLE


def test_set_weekend_part_count_uses_translatable_indexed_titles():
    sch = build_default_schedule(_WEEK, MeetingType.WEEKEND)

    set_section_part_count(sch, Section.PUBLIC_TALK, 2)
    set_section_part_count(sch, Section.WATCHTOWER, 2)

    assert [p.title for p in sch.parts_in(Section.PUBLIC_TALK)] == [
        PUBLIC_TALK_TITLE,
        PUBLIC_TALK_INDEXED_PART_TITLE,
    ]
    assert [p.title for p in sch.parts_in(Section.WATCHTOWER)] == [
        WATCHTOWER_STUDY_TITLE,
        WATCHTOWER_STUDY_INDEXED_PART_TITLE,
    ]


def _ministry_first_default() -> int:
    return build_default_schedule(_WEEK, MeetingType.MIDWEEK).parts_in(Section.MINISTRY)[0].planned_seconds


def _living_last_default() -> int:
    living = build_default_schedule(_WEEK, MeetingType.MIDWEEK).parts_in(Section.LIVING)
    return [p for p in living if p.kind is PartKind.STANDARD][-1].planned_seconds


# ── Engine lifecycle ──────────────────────────────────────────────────────────

def test_engine_countdown_and_overrun():
    _app()
    engine = TimerEngine()
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    engine.set_schedule(sch)
    part = sch.parts_in(Section.TREASURES)[0]  # 10 min
    engine.start_part(part.id)

    started = part.started_at_epoch
    assert started is not None

    snap = engine.snapshot(now=started + 60)
    assert snap.active
    assert round(snap.elapsed_seconds) == 60
    assert round(snap.remaining_seconds) == part.planned_seconds - 60
    assert not snap.overrun

    over = engine.snapshot(now=started + part.planned_seconds + 30)
    assert over.overrun
    assert round(over.remaining_seconds) == -30  # keeps counting past zero


def test_engine_snapshot_formats_indexed_active_part_title():
    _app()
    engine = TimerEngine()
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    engine.set_schedule(sch)
    part = sch.parts_in(Section.MINISTRY)[1]

    engine.start_part(part.id)

    assert engine.snapshot().active_part_title == "Part 2"


def test_engine_stop_freezes_and_reset_clears():
    _app()
    engine = TimerEngine()
    sch = build_default_schedule(_WEEK, MeetingType.WEEKEND)
    engine.set_schedule(sch)
    part = sch.parts[0]
    engine.start_part(part.id)
    part.started_at_epoch -= 45  # simulate 45s elapsed
    engine.stop_part(part.id)

    assert part.state is PartState.STOPPED
    assert round(part.accumulated_seconds) == 45
    # Active part stays visible (frozen) after stop.
    assert engine.snapshot().active

    engine.reset_part(part.id)
    assert part.state is PartState.IDLE
    assert part.accumulated_seconds == 0.0
    assert not engine.snapshot().active  # back to wall clock


def test_stopped_countdown_snapshot_displays_elapsed_result_during_hold():
    _app()
    engine = TimerEngine()
    sch = build_default_schedule(_WEEK, MeetingType.WEEKEND)
    engine.set_schedule(sch)
    part = sch.parts[0]
    part.planned_seconds = 5
    engine.start_part(part.id)
    started = part.started_at_epoch
    assert started is not None
    part.started_at_epoch = started - 3
    engine.stop_part(part.id)

    snap = engine.snapshot()

    assert snap.state is PartState.STOPPED
    assert round(snap.elapsed_seconds) == 3
    assert round(snap.remaining_seconds) == 2
    assert round(snap.display_seconds()) == 3
    assert round(snap.to_dict()["display_seconds"]) == 3


def test_engine_freeze_then_revert_to_wall_clock():
    _app()
    engine = TimerEngine()
    sch = build_default_schedule(_WEEK, MeetingType.WEEKEND)
    engine.set_schedule(sch)
    part = sch.parts[0]
    engine.start_part(part.id)
    engine.stop_part(part.id)
    # Right after stop the frozen result is still shown.
    assert engine.snapshot().active
    # When the hold window elapses the clock reverts but the part stays STOPPED.
    engine._on_revert_timeout()
    assert not engine.snapshot().active
    assert part.state is PartState.STOPPED


def test_engine_revert_cancelled_when_new_part_starts():
    _app()
    engine = TimerEngine()
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    engine.set_schedule(sch)
    p1, p2 = sch.parts[0], sch.parts[1]
    engine.start_part(p1.id)
    engine.stop_part(p1.id)
    engine.start_part(p2.id)        # supersedes the pending revert
    engine._on_revert_timeout()     # stale fire — must be a no-op
    assert engine.snapshot().active_part_id == p2.id


def test_engine_zero_freeze_reverts_immediately():
    _app()
    engine = TimerEngine()
    engine.set_freeze_seconds(0)
    sch = build_default_schedule(_WEEK, MeetingType.WEEKEND)
    engine.set_schedule(sch)
    part = sch.parts[0]
    engine.start_part(part.id)
    engine.stop_part(part.id)
    assert not engine.snapshot().active   # no hold → straight to wall clock
    assert part.state is PartState.STOPPED


def test_engine_starting_new_part_freezes_previous():
    _app()
    engine = TimerEngine()
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    engine.set_schedule(sch)
    p1, p2 = sch.parts[0], sch.parts[1]
    engine.start_part(p1.id)
    p1.started_at_epoch -= 30
    engine.start_part(p2.id)
    assert p1.state is PartState.STOPPED
    assert round(p1.accumulated_seconds) == 30
    assert p2.state is PartState.RUNNING


def test_engine_restores_running_part_from_schedule():
    _app()
    engine = TimerEngine()
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    running = sch.parts[0]
    running.state = PartState.RUNNING
    running.started_at_epoch = 1_000_000.0
    engine.set_schedule(sch)
    assert engine.snapshot().active_part_id == running.id


# ── Serialization round-trips ─────────────────────────────────────────────────

def test_part_roundtrip_preserves_runstate():
    part = MeetingPart.new(
        Section.LIVING,
        CBS_TITLE,
        30 * 60,
        kind=PartKind.CBS,
    )
    part.state = PartState.RUNNING
    part.started_at_epoch = 123456.0
    part.accumulated_seconds = 12.0
    restored = MeetingPart.from_dict(part.to_dict())
    assert restored == part
    assert restored.kind is PartKind.CBS


def test_part_roundtrip_defaults_missing_kind_to_standard():
    part = MeetingPart.new(Section.LIVING, "Part 1", 300)
    raw = part.to_dict()
    raw.pop("kind")

    restored = MeetingPart.from_dict(raw)

    assert restored.kind is PartKind.STANDARD


def test_clock_config_roundtrip():
    cfg = ClockConfig(
        mode=ClockMode.ANALOG, analog_style=AnalogClockStyle.CLASSIC,
        hour_format_24h=False, show_ampm=True, show_seconds=False,
        direction=Direction.UP, part_timer_display=PartTimerDisplay.CLOCK_TIMER,
        text_scale_pct=75,
    )
    assert ClockConfig.from_dict(cfg.to_dict()) == cfg


def test_clock_config_defaults_to_signature_analog_style():
    assert ClockConfig().analog_style is AnalogClockStyle.SIGNATURE
    assert ClockConfig.from_dict({"mode": "analog"}).analog_style is AnalogClockStyle.SIGNATURE
    assert ClockConfig.from_dict({"mode": "analog"}).part_timer_display is PartTimerDisplay.TIMER


def test_clock_config_clamps_display_size():
    assert ClockConfig(text_scale_pct=5).clamped().text_scale_pct == 10
    assert ClockConfig(text_scale_pct=250).clamped().text_scale_pct == 100


def test_schedule_roundtrip():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    restored = MeetingSchedule.from_dict(sch.to_dict())
    assert restored == sch


def test_normalize_schedule_adds_current_midweek_parts_to_unused_saved_schedule():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    sch.parts = [
        p for p in sch.parts
        if p.section not in {Section.OPENING_COMMENTS, Section.CONCLUDING_COMMENTS}
        and p.kind is not PartKind.CBS
    ]

    changed = normalize_schedule(sch)

    living = sch.parts_in(Section.LIVING)
    assert changed
    assert sch.parts[0].section is Section.OPENING_COMMENTS
    assert sch.parts[0].title == OPENING_COMMENTS_TITLE
    assert sch.parts[0].planned_seconds == 60
    assert [p.kind for p in living] == [PartKind.STANDARD, PartKind.STANDARD, PartKind.CBS]
    assert living[-1].title == CBS_TITLE
    assert living[-1].planned_seconds == 30 * 60
    assert sch.parts[-1].section is Section.CONCLUDING_COMMENTS
    assert sch.parts[-1].title == CONCLUDING_COMMENTS_TITLE
    assert sch.parts[-1].planned_seconds == 3 * 60
    assert not normalize_schedule(sch)


def test_normalize_schedule_does_not_insert_current_midweek_parts_into_used_schedule():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    sch.parts = [
        p for p in sch.parts
        if p.section not in {Section.OPENING_COMMENTS, Section.CONCLUDING_COMMENTS}
        and p.kind is not PartKind.CBS
    ]
    living = sch.parts_in(Section.LIVING)
    living[0].state = PartState.STOPPED
    living[0].first_started_epoch = 1_700_000_000.0
    living[0].accumulated_seconds = 45

    changed = normalize_schedule(sch)

    assert not changed
    assert [p.kind for p in sch.parts_in(Section.LIVING)] == [
        PartKind.STANDARD,
        PartKind.STANDARD,
    ]
    assert all(p.section is not Section.OPENING_COMMENTS for p in sch.parts)
    assert all(p.section is not Section.CONCLUDING_COMMENTS for p in sch.parts)


def test_normalize_schedule_can_fix_existing_cbs_metadata_after_timing():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    cbs = sch.parts_in(Section.LIVING)[-1]
    cbs.kind = PartKind.STANDARD
    cbs.first_started_epoch = 1_700_000_000.0
    cbs.accumulated_seconds = 30
    cbs.state = PartState.STOPPED

    changed = normalize_schedule(sch)

    assert changed
    assert cbs.kind is PartKind.CBS
    assert cbs.state is PartState.STOPPED
    assert cbs.first_started_epoch == 1_700_000_000.0
    assert cbs.accumulated_seconds == 30


def test_schedule_has_timing_data_detects_live_and_historical_state():
    sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    assert not schedule_has_timing_data(sch)

    sch.parts[0].first_started_epoch = 1_700_000_000.0
    assert schedule_has_timing_data(sch)

    sch.parts[0].first_started_epoch = None
    sch.parts[0].accumulated_seconds = 0.5
    assert schedule_has_timing_data(sch)

    sch.parts[0].accumulated_seconds = 0.0
    sch.parts[0].started_at_epoch = 1_700_000_000.0
    assert schedule_has_timing_data(sch)

    sch.parts[0].started_at_epoch = None
    sch.parts[0].state = PartState.RUNNING
    assert schedule_has_timing_data(sch)


def test_snapshot_roundtrip_preserves_canonical_display_fields():
    from app.core.timer.models import TimerSnapshot

    snap = TimerSnapshot(
        active=True,
        wall_clock_epoch=1_700_000_000.25,
        direction=Direction.DOWN,
        active_part_id="part-1",
        active_part_title="Demo",
        active_section=Section.TREASURES,
        planned_seconds=5,
        elapsed_seconds=3,
        remaining_seconds=2,
        state=PartState.STOPPED,
    )

    restored = TimerSnapshot.from_dict(snap.to_dict())

    assert restored == snap
    assert restored.display_seconds() == 3


# ── Store persistence ─────────────────────────────────────────────────────────

def test_render_idle_digital_matches_wall_clock():
    import time as _time
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    epoch = 1_700_000_000.0
    snap = TimerSnapshot(active=False, wall_clock_epoch=epoch, direction=Direction.DOWN)
    cfg = ClockConfig(mode=ClockMode.DIGITAL, hour_format_24h=True, show_seconds=True)
    model = build_render_model(snap, cfg)
    assert model["mode"] == "digital"
    assert model["display_mode"] == "clock"
    assert model["primary_text"] == _time.strftime("%H:%M", _time.localtime(epoch))
    assert model["seconds_text"] == _time.strftime("%S", _time.localtime(epoch))
    assert model["clock"]["primary_text"] == model["primary_text"]
    assert not model["active"]


def test_render_idle_analog_sets_hand_angles():
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    snap = TimerSnapshot(active=False, wall_clock_epoch=1_700_000_000.0, direction=Direction.DOWN)
    cfg = ClockConfig(mode=ClockMode.ANALOG)
    model = build_render_model(snap, cfg)
    assert model["mode"] == "analog"
    assert model["analog_style"] == AnalogClockStyle.SIGNATURE.value
    assert 0.0 <= model["hour_angle"] < 360.0
    assert 0.0 <= model["minute_angle"] < 360.0
    assert 0.0 <= model["second_angle"] < 360.0


def test_render_idle_analog_digital_sets_hands_and_clock_text():
    import time as _time
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    epoch = _time.mktime((2026, 6, 1, 12, 34, 10, 0, 0, -1))
    snap = TimerSnapshot(active=False, wall_clock_epoch=epoch, direction=Direction.DOWN)
    cfg = ClockConfig(mode=ClockMode.ANALOG_DIGITAL, hour_format_24h=True, show_seconds=True)

    model = build_render_model(snap, cfg)

    assert model["mode"] == "analog_digital"
    assert model["primary_text"] == _time.strftime("%H:%M", _time.localtime(epoch))
    assert model["seconds_text"] == _time.strftime("%S", _time.localtime(epoch))
    assert model["secondary_text"] == ""
    assert model["hour_angle"] > 0.0
    assert model["minute_angle"] > 0.0
    assert model["second_angle"] == 60.0


def test_render_idle_analog_second_hand_ticks_once_per_second():
    import time as _time
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    base = _time.mktime((2026, 6, 1, 12, 34, 10, 0, 0, -1))
    cfg = ClockConfig(mode=ClockMode.ANALOG)
    first = build_render_model(
        TimerSnapshot(active=False, wall_clock_epoch=base + 0.10, direction=Direction.DOWN),
        cfg,
    )
    same_second = build_render_model(
        TimerSnapshot(active=False, wall_clock_epoch=base + 0.90, direction=Direction.DOWN),
        cfg,
    )
    next_second = build_render_model(
        TimerSnapshot(active=False, wall_clock_epoch=base + 1.00, direction=Direction.DOWN),
        cfg,
    )

    assert same_second["second_angle"] == first["second_angle"]
    assert same_second["minute_angle"] == first["minute_angle"]
    assert same_second["hour_angle"] == first["hour_angle"]
    assert next_second["second_angle"] == first["second_angle"] + 6.0


def test_render_active_countdown_and_overrun():
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    snap = TimerSnapshot(
        active=True, wall_clock_epoch=0.0, direction=Direction.DOWN,
        active_part_title="Part 1", planned_seconds=300,
        elapsed_seconds=330, remaining_seconds=-30, overrun=True, state=PartState.RUNNING,
    )
    cfg = ClockConfig(direction=Direction.DOWN, analog_style=AnalogClockStyle.CLASSIC)
    model = build_render_model(snap, cfg)
    assert model["active"]
    assert model["display_mode"] == "timer"
    assert model["mode"] == "digital"
    assert model["clock"]["analog_style"] == AnalogClockStyle.CLASSIC.value
    assert model["overrun"]
    assert model["primary_text"] == "-00:30"
    assert model["seconds_text"] == ""
    assert model["secondary_text"] == ""
    assert model["timer"]["primary_text"] == "-00:30"
    assert model["timer"]["overrun"]


def test_render_active_can_show_clock_only_with_configured_clock_face():
    import time as _time
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    epoch = _time.mktime((2026, 6, 1, 12, 34, 10, 0, 0, -1))
    snap = TimerSnapshot(
        active=True, wall_clock_epoch=epoch, direction=Direction.DOWN,
        active_part_title="Part 1", planned_seconds=300,
        elapsed_seconds=120, remaining_seconds=180, overrun=False, state=PartState.RUNNING,
    )
    cfg = ClockConfig(
        mode=ClockMode.ANALOG_DIGITAL,
        hour_format_24h=True,
        show_seconds=True,
        part_timer_display=PartTimerDisplay.CLOCK,
    )

    model = build_render_model(snap, cfg)

    assert model["display_mode"] == "clock"
    assert model["mode"] == "analog_digital"
    assert model["primary_text"] == "12:34"
    assert model["seconds_text"] == "10"
    assert model["clock"]["mode"] == "analog_digital"
    assert model["timer"]["primary_text"] == "03:00"


def test_render_active_can_show_configured_clock_with_timer():
    import time as _time
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    epoch = _time.mktime((2026, 6, 1, 12, 34, 10, 0, 0, -1))
    snap = TimerSnapshot(
        active=True, wall_clock_epoch=epoch, direction=Direction.DOWN,
        active_part_title="Part 1", planned_seconds=300,
        elapsed_seconds=120, remaining_seconds=180, overrun=False, state=PartState.RUNNING,
    )
    cfg = ClockConfig(
        mode=ClockMode.ANALOG,
        part_timer_display=PartTimerDisplay.CLOCK_TIMER,
    )

    model = build_render_model(snap, cfg)

    assert model["display_mode"] == "clock_timer"
    assert model["clock"]["mode"] == "analog"
    assert model["clock"]["hour_angle"] > 0.0
    assert model["timer"]["mode"] == "digital"
    assert model["timer"]["primary_text"] == "03:00"


def test_render_active_analog_clock_sector_marks_remaining_time():
    import time as _time
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    epoch = _time.mktime((2026, 6, 1, 12, 34, 10, 0, 0, -1))
    snap = TimerSnapshot(
        active=True, wall_clock_epoch=epoch, direction=Direction.DOWN,
        active_part_title="Part 1", planned_seconds=300,
        elapsed_seconds=120, remaining_seconds=180, overrun=False, state=PartState.RUNNING,
    )
    cfg = ClockConfig(
        mode=ClockMode.ANALOG,
        part_timer_display=PartTimerDisplay.CLOCK,
    )

    model = build_render_model(snap, cfg)
    sector = model["clock"]["duration_sector"]

    assert sector["visible"]
    assert sector["show_remaining"]
    assert not sector["show_overrun"]
    assert sector["end_angle"] == 223.0
    assert sector["current_angle"] == 205.0


def test_render_active_analog_clock_sector_marks_overrun_trail():
    import time as _time
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    epoch = _time.mktime((2026, 6, 1, 12, 34, 10, 0, 0, -1))
    snap = TimerSnapshot(
        active=True, wall_clock_epoch=epoch, direction=Direction.DOWN,
        active_part_title="Part 1", planned_seconds=300,
        elapsed_seconds=330, remaining_seconds=-30, overrun=True, state=PartState.RUNNING,
    )
    cfg = ClockConfig(
        mode=ClockMode.ANALOG,
        part_timer_display=PartTimerDisplay.CLOCK_TIMER,
    )

    model = build_render_model(snap, cfg)
    sector = model["clock"]["duration_sector"]

    assert sector["visible"]
    assert not sector["show_remaining"]
    assert sector["show_overrun"]
    assert sector["end_angle"] == 202.0
    assert sector["current_angle"] == 205.0


def test_render_active_analog_clock_sector_stays_hidden_when_clock_is_not_visible():
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    snap = TimerSnapshot(
        active=True, wall_clock_epoch=0.0, direction=Direction.DOWN,
        active_part_title="Part 1", planned_seconds=300,
        elapsed_seconds=120, remaining_seconds=180, overrun=False, state=PartState.RUNNING,
    )
    cfg = ClockConfig(
        mode=ClockMode.ANALOG,
        part_timer_display=PartTimerDisplay.TIMER,
    )

    model = build_render_model(snap, cfg)

    assert not model["clock"]["duration_sector"]["visible"]


def test_render_active_duration_splits_seconds_only_when_hours_are_visible():
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    snap = TimerSnapshot(
        active=True, wall_clock_epoch=0.0, direction=Direction.DOWN,
        active_part_title="Long Part", planned_seconds=7200,
        elapsed_seconds=3540, remaining_seconds=3661, overrun=False, state=PartState.RUNNING,
    )

    model = build_render_model(snap, ClockConfig(direction=Direction.DOWN))

    assert model["primary_text"] == "1:01"
    assert model["seconds_text"] == "01"


def test_render_stopped_countdown_shows_elapsed_result():
    from app.core.timer.render import build_render_model
    from app.core.timer.models import TimerSnapshot

    snap = TimerSnapshot(
        active=True, wall_clock_epoch=0.0, direction=Direction.DOWN,
        active_part_title="Short Part", planned_seconds=5,
        elapsed_seconds=3, remaining_seconds=2, overrun=False, state=PartState.STOPPED,
    )
    model = build_render_model(snap, ClockConfig(direction=Direction.DOWN))

    assert model["primary_text"] == "00:03"
    assert model["seconds_text"] == ""


def test_store_roundtrip(tmp_path):
    _app()
    original_org = _ps.current_org()
    _ps.set_org("SolinTest_timer_store")
    try:
        store = TimerStore()
        cfg = ClockConfig(
            mode=ClockMode.ANALOG,
            analog_style=AnalogClockStyle.CLASSIC,
            text_scale_pct=90,
        )
        store.save_clock_config(cfg)
        assert store.load_clock_config() == cfg

        sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
        sch.parts[0].state = PartState.RUNNING
        sch.parts[0].started_at_epoch = 999.0
        store.save_schedule(sch)
        loaded = store.load_schedule(_WEEK, MeetingType.MIDWEEK)
        assert loaded == sch

        store.set_timer_visible(False)
        assert store.get_timer_visible() is False
    finally:
        _ps.prefs("Timer").clear()
        _ps.set_org(original_org)


def test_store_load_normalizes_unused_midweek_missing_current_parts(tmp_path):
    _app()
    original_org = _ps.current_org()
    _ps.set_org("SolinTest_timer_store_migration")
    try:
        store = TimerStore()
        sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
        sch.parts = [
            p for p in sch.parts
            if p.section not in {Section.OPENING_COMMENTS, Section.CONCLUDING_COMMENTS}
            and p.kind is not PartKind.CBS
        ]
        key = f"schedule/{_WEEK.isoformat()}/{MeetingType.MIDWEEK.value}"
        _ps.prefs("Timer").setValue(key, json.dumps(sch.to_dict()))

        loaded = store.load_schedule(_WEEK, MeetingType.MIDWEEK)

        assert loaded is not None
        assert loaded.parts[0].section is Section.OPENING_COMMENTS
        assert loaded.parts_in(Section.LIVING)[-1].kind is PartKind.CBS
        assert loaded.parts[-1].section is Section.CONCLUDING_COMMENTS
        saved = json.loads(_ps.prefs("Timer").value(key, "", str))
        assert saved["parts"][0]["section"] == Section.OPENING_COMMENTS.value
        assert saved["parts"][-2]["kind"] == PartKind.CBS.value
        assert saved["parts"][-1]["section"] == Section.CONCLUDING_COMMENTS.value
    finally:
        _ps.prefs("Timer").clear()
        _ps.set_org(original_org)


def test_store_load_preserves_used_midweek_missing_current_parts(tmp_path):
    _app()
    original_org = _ps.current_org()
    _ps.set_org("SolinTest_timer_store_used_schedule")
    try:
        store = TimerStore()
        sch = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
        sch.parts = [
            p for p in sch.parts
            if p.section not in {Section.OPENING_COMMENTS, Section.CONCLUDING_COMMENTS}
            and p.kind is not PartKind.CBS
        ]
        sch.parts[0].state = PartState.STOPPED
        sch.parts[0].first_started_epoch = 1_700_000_000.0
        sch.parts[0].accumulated_seconds = 60
        key = f"schedule/{_WEEK.isoformat()}/{MeetingType.MIDWEEK.value}"
        _ps.prefs("Timer").setValue(key, json.dumps(sch.to_dict()))

        loaded = store.load_schedule(_WEEK, MeetingType.MIDWEEK)

        assert loaded is not None
        assert all(p.kind is not PartKind.CBS for p in loaded.parts)
        assert all(p.section is not Section.OPENING_COMMENTS for p in loaded.parts)
        assert all(p.section is not Section.CONCLUDING_COMMENTS for p in loaded.parts)
        saved = json.loads(_ps.prefs("Timer").value(key, "", str))
        assert all(p.get("kind") != PartKind.CBS.value for p in saved["parts"])
        assert all(p["section"] != Section.OPENING_COMMENTS.value for p in saved["parts"])
        assert all(p["section"] != Section.CONCLUDING_COMMENTS.value for p in saved["parts"])
    finally:
        _ps.prefs("Timer").clear()
        _ps.set_org(original_org)
