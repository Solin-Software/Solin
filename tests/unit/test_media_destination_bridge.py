from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect, QSize

from solin.core.media.destinations import (
    MediaDestinationAsset,
    MediaDestinationKind,
    MediaRouteAction,
    MeetingDestinationTarget,
    PlaylistDestinationTarget,
)
from solin.core.media.placement import END_OF_LIST_INDEX
from solin.core.meetings.meeting_weeks import current_monday
from solin.ui.qml.media_destination import (
    MediaDestinationBridge,
    centered_dialog_position,
)


def _bridge(*, can_play: bool = False) -> MediaDestinationBridge:
    return MediaDestinationBridge(
        media_title="Example",
        item_count=2,
        can_play=can_play,
        playlists=(("one", "One"), ("two", "Two")),
    )


def test_destination_asset_requires_one_explicit_payload_shape() -> None:
    with pytest.raises(ValueError):
        MediaDestinationAsset(title="Missing", source_id="missing")
    with pytest.raises(ValueError):
        MediaDestinationAsset(
            title="Ambiguous",
            source_id="source",
            item={"url": "clip.mp4"},
            import_path="document.pdf",
        )


def test_dialog_position_is_centered_on_its_owner() -> None:
    position = centered_dialog_position(
        QRect(100, 100, 1000, 700),
        QSize(470, 500),
        QRect(0, 0, 1920, 1080),
    )

    assert position == QPoint(364, 199)


def test_dialog_position_is_clamped_to_the_owners_screen() -> None:
    position = centered_dialog_position(
        QRect(3000, 800, 400, 300),
        QSize(470, 500),
        QRect(1920, 0, 1280, 1024),
    )

    assert position == QPoint(2730, 524)


def test_play_finishes_without_selecting_a_destination() -> None:
    bridge = _bridge(can_play=True)
    finished = []
    bridge.finished.connect(lambda: finished.append(True))

    bridge.choosePlay()
    bridge.choosePlay()

    assert finished == [True]
    assert bridge.result is not None
    assert bridge.result.action is MediaRouteAction.PLAY
    assert bridge.result.destination is None


def test_add_action_preserves_one_continuous_wizard_history() -> None:
    bridge = _bridge(can_play=True)
    requested = []
    bridge.addRequested.connect(lambda: requested.append(True))

    bridge.requestAdd()
    bridge.showDestinations()
    bridge.showPlaylists()
    bridge.back()

    assert requested == [True]
    assert bridge.step == "destination"
    assert bridge.canGoBack is True


def test_playlist_selection_returns_a_typed_target() -> None:
    bridge = _bridge()

    bridge.choosePlaylist("two", "Two")

    assert bridge.result is not None
    assert bridge.result.destination is MediaDestinationKind.PLAYLIST
    assert bridge.result.target == PlaylistDestinationTarget("two", "Two")


def test_empty_playlist_name_cannot_finish_the_wizard() -> None:
    bridge = _bridge()

    bridge.createPlaylist("   ")

    assert bridge.result is None


def test_meeting_navigation_starts_at_current_week_and_stays_in_shared_range() -> None:
    bridge = _bridge()
    requested: list[tuple[str, bool]] = []
    bridge.weekRequested.connect(lambda monday, force: requested.append((monday, force)))

    bridge.showMeetings()
    assert requested[-1] == (current_monday().isoformat(), False)

    for _ in range(20):
        bridge.previousWeek()
    first = bridge.weekMonday
    assert bridge.canPreviousWeek is False

    for _ in range(20):
        bridge.nextWeek()
    assert bridge.canNextWeek is False
    assert bridge.weekMonday != first


def test_stale_meeting_response_does_not_replace_selected_week() -> None:
    bridge = _bridge()
    bridge.showMeetings()
    stale_week = bridge.weekMonday
    bridge.nextWeek()

    bridge.update_meeting_targets(
        stale_week,
        [{"pub_type": "mwb", "available": True, "status": "ready"}],
    )

    assert bridge.meetingTargets == []


def test_small_meeting_tree_inserts_at_end_without_extra_prompt() -> None:
    bridge = _bridge()
    monday = bridge.weekMonday
    bridge.prepare_meeting_selection(
        monday=monday,
        pub_type="mwb",
        placement_options=[],
    )

    assert bridge.result is not None
    assert bridge.result.target == MeetingDestinationTarget(
        monday=monday,
        pub_type="mwb",
        list_id="root",
        insert_index=END_OF_LIST_INDEX,
    )


def test_meeting_placement_rejects_unknown_choice() -> None:
    bridge = _bridge()
    bridge.prepare_meeting_selection(
        monday=bridge.weekMonday,
        pub_type="wt",
        placement_options=[{"id": "top", "label": "Top", "type": "position", "color": ""}],
    )

    bridge.confirmPlacement("unknown")
    assert bridge.result is None

    bridge.confirmPlacement("top")
    assert bridge.result is not None
    assert bridge.result.target == MeetingDestinationTarget(
        monday=bridge.weekMonday,
        pub_type="wt",
        list_id="root",
        insert_index=0,
    )


def test_cancel_is_idempotent() -> None:
    bridge = _bridge()
    finished = []
    bridge.finished.connect(lambda: finished.append(True))

    bridge.cancel()
    bridge.cancel()

    assert finished == [True]
    assert bridge.result is None


def test_meeting_error_retry_reloads_selected_week() -> None:
    bridge = _bridge()
    requested = []
    bridge.weekRequested.connect(lambda monday, force: requested.append((monday, force)))
    bridge.showMeetings()
    requested.clear()

    bridge.show_meeting_error("Unavailable")
    bridge.retry()

    assert bridge.step == "meeting"
    assert requested == [(bridge.weekMonday, True)]


def test_preparation_retry_does_not_leave_error_page_in_back_history() -> None:
    bridge = _bridge(can_play=True)

    bridge.showPreparing()
    bridge.showError("Offline")
    bridge.retry()
    bridge.showPreparing()
    bridge.showDestinations()
    bridge.back()

    assert bridge.step == "action"


def test_initial_document_preparation_has_no_fake_back_destination() -> None:
    bridge = _bridge(can_play=False)

    bridge.showPreparing()
    bridge.showDestinations()

    assert bridge.step == "destination"
    assert bridge.canGoBack is False
