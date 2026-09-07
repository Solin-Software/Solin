import os
from pathlib import Path
from types import SimpleNamespace

from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.media.destinations import MediaDestinationRequest
from solin.core.projection.idle_media import IdleMediaRequest
from solin.ui.qml.media_tree.state import MediaAvailability, MediaPresentationState
from solin.widgets.playlist import widget as playlist_widget
from solin.widgets.playlist.edit_actions import PlaylistEditActionsMixin


def _attach_presentation_state(
    view,
    *,
    thumbnail_source: str = "",
    local_path: str = "C:/media/clip.mp4",
    source_signature: str = "",
    persisted: list | None = None,
    refreshed: list | None = None,
) -> None:
    state = MediaPresentationState(
        availability=MediaAvailability.AVAILABLE,
        local_path=local_path,
        thumbnail_source=thumbnail_source,
        source_signature=source_signature,
    )
    persisted = persisted if persisted is not None else []
    refreshed = refreshed if refreshed is not None else []
    view._tree_session = getattr(
        view,
        "_tree_session",
        SimpleNamespace(
            owner_id="playlist:saved",
            playlist=getattr(view, "_pl", None),
            refresh=lambda **_kwargs: refreshed.append(True),
        ),
    )
    view._media_tree_runtime = SimpleNamespace(
        registry=SimpleNamespace(
            state=lambda _owner, _node: state,
            patch=lambda *_args, **_kwargs: None,
        ),
        thumbnails=SimpleNamespace(
            save_image=lambda **values: persisted.append(values),
        ),
    )


def test_playlist_edit_view_uses_actions_mixin():
    assert playlist_widget.PlaylistEditActionsMixin is PlaylistEditActionsMixin
    assert issubclass(playlist_widget.PlaylistEditView, PlaylistEditActionsMixin)
    assert playlist_widget.PlaylistEditView._add_files is PlaylistEditActionsMixin._add_files
    assert playlist_widget.PlaylistEditView.load_temp_playlist is (
        PlaylistEditActionsMixin.load_temp_playlist
    )


def test_add_to_destination_emits_the_selected_playlist_item():
    requests = []
    source = {
        "id": "media-1",
        "title": "Talk",
        "url": "C:/media/talk.mp4",
        "type": "video",
        "section_id": "section-1",
        "start_trim_ticks": 10_000,
    }
    view = SimpleNamespace(
        _pl={"items": [source]},
        _flush_image_framing_save=lambda: None,
        _tree_session=SimpleNamespace(owner_id="playlist:saved"),
        _media_tree_runtime=SimpleNamespace(
            registry=SimpleNamespace(
                state=lambda _owner, _node: MediaPresentationState(
                    availability=MediaAvailability.AVAILABLE,
                )
            )
        ),
        media_destination_requested=SimpleNamespace(emit=requests.append),
    )

    PlaylistEditActionsMixin._add_to_destination(view, "media-1")

    assert len(requests) == 1
    request = requests[0]
    assert isinstance(request, MediaDestinationRequest)
    assert request.title == "Talk"
    assert request.can_play is False
    assert len(request.assets) == 1
    assert request.assets[0].source_id == "C:/media/talk.mp4"
    assert request.assets[0].item["id"] != "media-1"
    assert request.assets[0].item["start_trim_ticks"] == 10_000
    assert "section_id" not in request.assets[0].item


def test_add_to_destination_ignores_unavailable_playlist_item():
    requests = []
    view = SimpleNamespace(
        _pl={
            "items": [
                {
                    "id": "media-1",
                    "title": "Missing",
                    "url": "C:/media/missing.mp4",
                    "type": "video",
                }
            ]
        },
        _flush_image_framing_save=lambda: None,
        _tree_session=SimpleNamespace(owner_id="playlist:saved"),
        _media_tree_runtime=SimpleNamespace(
            registry=SimpleNamespace(
                state=lambda _owner, _node: MediaPresentationState(
                    availability=MediaAvailability.MISSING,
                )
            )
        ),
        media_destination_requested=SimpleNamespace(emit=requests.append),
    )

    PlaylistEditActionsMixin._add_to_destination(view, "media-1")

    assert requests == []


def test_set_as_idle_emits_an_existing_local_playlist_item(tmp_path):
    path = tmp_path / "idle.mp4"
    thumbnail = tmp_path / "idle.jpg"
    path.write_bytes(b"video")
    thumbnail.write_bytes(b"thumbnail")
    requests = []
    view = SimpleNamespace(
        _pl={
            "items": [
                {
                    "id": "media-1",
                    "title": "Idle video",
                    "url": str(path),
                    "type": "video",
                }
            ]
        },
        _flush_image_framing_save=lambda: None,
        _playlist_thumbnail_store=SimpleNamespace(
            path=lambda _storage_id: thumbnail,
        ),
        _tree_session=SimpleNamespace(owner_id="playlist:saved"),
        _media_tree_runtime=SimpleNamespace(
            registry=SimpleNamespace(
                state=lambda _owner, _node: MediaPresentationState(
                    availability=MediaAvailability.AVAILABLE,
                    local_path=str(path),
                )
            )
        ),
        set_as_idle_requested=SimpleNamespace(emit=requests.append),
    )

    PlaylistEditActionsMixin._set_as_idle(view, "media-1")

    assert requests == [
        IdleMediaRequest(
            title="Idle video",
            path=str(path),
            media_type="video",
            thumbnail_path=str(thumbnail),
        )
    ]


def test_set_as_idle_ignores_remote_playlist_item():
    requests = []
    view = SimpleNamespace(
        _pl={
            "items": [
                {
                    "id": "media-1",
                    "title": "Remote",
                    "url": "https://example.test/video.mp4",
                    "type": "video",
                }
            ]
        },
        _flush_image_framing_save=lambda: None,
        _tree_session=SimpleNamespace(owner_id="playlist:saved"),
        _media_tree_runtime=SimpleNamespace(
            registry=SimpleNamespace(
                state=lambda _owner, _node: MediaPresentationState(
                    availability=MediaAvailability.AVAILABLE,
                )
            )
        ),
        set_as_idle_requested=SimpleNamespace(emit=requests.append),
    )

    PlaylistEditActionsMixin._set_as_idle(view, "media-1")

    assert requests == []


def test_existing_thumbnail_does_not_skip_missing_duration_hydration():
    class _Thumbnail:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    requests = []
    view = SimpleNamespace(
        _id_to_thumb={"media-1": _Thumbnail()},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: True),
        _request_thumbnail=lambda *args, **kwargs: requests.append((args, kwargs)),
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    _attach_presentation_state(view, thumbnail_source="image://playlistthumbs/media-1")
    item = {
        "id": "media-1",
        "url": "clip.mp4",
        "type": "video",
    }

    playlist_widget.PlaylistEditView._request_missing_thumbnail_for_item(view, item)

    assert requests == [
        (
            ("media-1", "clip.mp4", "video"),
            {
                "require_thumbnail": False,
                "require_title": False,
                "require_duration": True,
            },
        )
    ]


def test_local_auto_title_requests_embedded_metadata_even_with_existing_thumbnail():
    class _Thumbnail:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    requests = []
    view = SimpleNamespace(
        _id_to_thumb={"media-1": _Thumbnail()},
        _request_thumbnail=lambda *args, **kwargs: requests.append((args, kwargs)),
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    _attach_presentation_state(
        view,
        thumbnail_source="image://playlistthumbs/media-1",
    )
    item = {
        "id": "media-1",
        "url": "C:/media/gnj_T_02_r720P.mp4",
        "type": "video",
        "title": "gnj_T_02_r720P",
        "auto_title": True,
        "base_duration_ticks": 10_000,
    }

    playlist_widget.PlaylistEditView._request_missing_thumbnail_for_item(view, item)

    assert requests == [
        (
            ("media-1", "C:/media/gnj_T_02_r720P.mp4", "video"),
            {
                "require_thumbnail": False,
                "require_title": True,
                "require_duration": False,
            },
        )
    ]


def test_probe_completion_starts_one_complete_presentation_request():
    item = {
        "id": "media-1",
        "url": "C:/media/gnj_T_02_r720P.mp4",
        "type": "video",
        "title": "gnj_T_02_r720P",
        "auto_title": True,
    }
    state = MediaPresentationState(availability=MediaAvailability.CHECKING)
    requests = []
    view = SimpleNamespace(
        _pl={"items": [item]},
        _id_to_thumb={},
        _tree_session=SimpleNamespace(owner_id="playlist:saved"),
        _media_tree_runtime=SimpleNamespace(
            registry=SimpleNamespace(
                state=lambda _owner, _node: state,
            )
        ),
        _request_thumbnail=lambda *args, **kwargs: requests.append((args, kwargs)),
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    view._request_missing_thumbnail_for_item = lambda current: (
        playlist_widget.PlaylistEditView._request_missing_thumbnail_for_item(
            view,
            current,
        )
    )

    view._request_missing_thumbnail_for_item(item)
    assert requests == []

    state = MediaPresentationState(
        availability=MediaAvailability.AVAILABLE,
        local_path=item["url"],
        source_signature="100:200",
    )
    playlist_widget.PlaylistEditView._on_presentation_state_changed(
        view,
        "playlist:saved",
        "media-1",
    )

    assert requests == [
        (
            ("media-1", item["url"], "video"),
            {
                "require_thumbnail": True,
                "require_title": True,
                "require_duration": True,
            },
        )
    ]


def test_source_change_failure_reprobes_before_requesting_metadata_again():
    retired = []
    requested = []
    view = SimpleNamespace(
        _thumb_idx_to_id={12: "media-1"},
        _retire_thumbnail_request=retired.append,
        _tree_session=SimpleNamespace(request_nodes=requested.append),
    )

    playlist_widget.PlaylistEditView._on_thumbnail_failed(
        view,
        12,
        SimpleNamespace(code="source-changed"),
    )

    assert retired == [12]
    assert requested == [{"media-1"}]


def test_rebuild_keeps_matching_thumbnail_request_and_applies_late_result(
    monkeypatch,
):
    class _Pixmap:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    invalidated = []
    updated = []
    changed = []
    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "type": "video",
        "base_duration_ticks": 10_000,
    }
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(True, False, False)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        model=SimpleNamespace(
            update_thumb=updated.append,
            update_title=lambda *_args: None,
        ),
        bridge=SimpleNamespace(emit_media_changed=changed.append),
        _save=lambda: None,
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    view._thumbnail_result_is_current = lambda item_id, source, signature: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
            signature,
        )
    )
    persisted = []
    refreshed = []
    _attach_presentation_state(view, persisted=persisted, refreshed=refreshed)

    pixmap = _Pixmap()
    pixmap.toImage = lambda: SimpleNamespace()
    playlist_widget.PlaylistEditView._reconcile_thumbnail_requests(
        view,
        [dict(item)],
    )
    playlist_widget.PlaylistEditView._on_info(view, 12, pixmap, "")

    assert invalidated == [12]
    assert view._thumb_idx_to_id == {}
    assert view._thumb_idx_to_source == {}
    assert view._thumb_idx_to_intent == {}
    assert view._thumb_pending_item_ids == set()
    assert view._id_to_thumb == {"media-1": pixmap}
    assert persisted and persisted[0]["node_id"] == "media-1"
    assert refreshed == [True]


def test_visual_rebuild_does_not_restart_thumbnail_work():
    invalidated = []
    rebuilt = []
    replacements = []
    playlist = {"items": [{"id": "media-1", "url": "C:/media/new.mp4"}]}
    view = SimpleNamespace(
        _pl=playlist,
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: "C:/media/old.mp4"},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(True, False, False)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        model=SimpleNamespace(rebuild=rebuilt.append),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
        _request_thumbnail=lambda *args, **kwargs: replacements.append((args, kwargs)),
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )

    view._reconcile_thumbnail_requests = lambda items: (
        playlist_widget.PlaylistEditView._reconcile_thumbnail_requests(view, items)
    )
    refreshed = []
    view._tree_session = SimpleNamespace(
        playlist=playlist,
        refresh=lambda **_kwargs: refreshed.append(True),
    )

    playlist_widget.PlaylistEditView._publish_tree_snapshot(view)

    assert invalidated == []
    assert view._thumb_idx_to_id == {12: "media-1"}
    assert view._thumb_idx_to_source == {12: "C:/media/old.mp4"}
    assert view._thumb_idx_to_intent == {
        12: playlist_widget._ThumbnailRequestIntent(True, False, False)
    }
    assert view._thumb_pending_item_ids == {"media-1"}
    assert refreshed == [True]
    assert replacements == []


def test_rebuild_promotes_request_when_duration_becomes_required():
    invalidated = []
    replacements = []
    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "type": "video",
    }
    view = SimpleNamespace(
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(True, False, False)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
        _request_thumbnail=lambda *args, **kwargs: replacements.append((args, kwargs)),
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    _attach_presentation_state(view)

    playlist_widget.PlaylistEditView._reconcile_thumbnail_requests(view, [item])

    assert invalidated == [12]
    assert replacements == [
        (
            ("media-1", item["url"], "video"),
            {
                "require_thumbnail": True,
                "require_title": False,
                "require_duration": True,
            },
        )
    ]


def test_active_metadata_request_is_promoted_when_probe_enables_thumbnail():
    invalidated = []
    requests = []
    view = SimpleNamespace(
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: "C:/media/clip.mp4"},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(False, True, True)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_request_token=12,
        _thumb_queue=SimpleNamespace(
            invalidate=invalidated.append,
            request=lambda *args, **kwargs: requests.append((args, kwargs)),
        ),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    _attach_presentation_state(view)

    playlist_widget.PlaylistEditView._request_thumbnail(
        view,
        "media-1",
        "C:/media/clip.mp4",
        "video",
        require_thumbnail=True,
        require_title=True,
        require_duration=False,
    )

    assert invalidated == [12]
    assert view._thumb_idx_to_id == {13: "media-1"}
    assert view._thumb_idx_to_intent == {
        13: playlist_widget._ThumbnailRequestIntent(True, True, True)
    }
    assert requests == [
        (
            (13, "C:/media/clip.mp4", "video"),
            {
                "require_thumbnail": True,
                "require_title": True,
                "require_duration": True,
                "restart_on_source_change": False,
            },
        )
    ]


def test_active_request_is_not_restarted_when_it_already_covers_intent():
    invalidated = []
    requests = []
    view = SimpleNamespace(
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: "C:/media/clip.mp4"},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(True, True, True)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_request_token=12,
        _thumb_queue=SimpleNamespace(
            invalidate=invalidated.append,
            request=lambda *args, **kwargs: requests.append((args, kwargs)),
        ),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    _attach_presentation_state(view)

    playlist_widget.PlaylistEditView._request_thumbnail(
        view,
        "media-1",
        "C:/media/clip.mp4",
        "video",
        require_thumbnail=True,
    )

    assert invalidated == []
    assert requests == []
    assert view._thumb_idx_to_id == {12: "media-1"}


def test_rebuild_retires_request_when_snapshot_satisfies_all_intents():
    class _Thumbnail:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    invalidated = []
    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "title": "Resolved title",
        "auto_title": False,
        "base_duration_ticks": 10_000,
    }
    view = SimpleNamespace(
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(True, True, True)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
        _id_to_thumb={"media-1": _Thumbnail()},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    _attach_presentation_state(view, thumbnail_source="image://playlistthumbs/media-1")

    playlist_widget.PlaylistEditView._reconcile_thumbnail_requests(view, [item])

    assert invalidated == [12]
    assert view._thumb_idx_to_id == {}
    assert view._thumb_idx_to_source == {}
    assert view._thumb_idx_to_intent == {}
    assert view._thumb_pending_item_ids == set()


def test_duration_only_result_does_not_rewrite_thumbnail_or_title():
    class _Thumbnail:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "title": "Original",
    }
    thumbnail_updates = []
    title_updates = []
    saves = []
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(False, False, True)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=lambda _token: None),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        model=SimpleNamespace(
            update_thumb=thumbnail_updates.append,
            update_title=lambda *args: title_updates.append(args),
        ),
        bridge=SimpleNamespace(emit_media_changed=lambda _item_id: None),
        _save=lambda: saves.append(True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_result_is_current = lambda item_id, source, signature: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
            signature,
        )
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    _attach_presentation_state(view)

    playlist_widget.PlaylistEditView._on_info(
        view,
        12,
        _Thumbnail(),
        "Unexpected title",
    )

    assert view._id_to_thumb == {}
    assert item["title"] == "Original"
    assert thumbnail_updates == []
    assert title_updates == []
    assert saves == []


def test_equal_auto_title_is_marked_resolved_without_visual_patch():
    item = {
        "id": "media-1",
        "url": "https://example.test/clip.mp4",
        "title": "Resolved title",
        "auto_title": True,
    }
    title_updates = []
    saves = []
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(False, True, False)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=lambda _token: None),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        model=SimpleNamespace(
            update_thumb=lambda _item_id: None,
            update_title=lambda *args: title_updates.append(args),
        ),
        bridge=SimpleNamespace(emit_media_changed=lambda _item_id: None),
        _save=lambda: saves.append(True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_result_is_current = lambda item_id, source, signature: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
            signature,
        )
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    refreshed = []
    _attach_presentation_state(view, refreshed=refreshed)

    playlist_widget.PlaylistEditView._on_info(
        view,
        12,
        None,
        "Resolved title",
    )

    assert item["auto_title"] is False
    assert title_updates == []
    assert saves == [True]


def test_empty_metadata_title_does_not_resolve_auto_title():
    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "title": "clip",
        "auto_title": True,
    }
    saves = []
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(False, True, False)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=lambda _token: None),
        _id_to_thumb={},
        _save=lambda: saves.append(True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_result_is_current = lambda item_id, source, signature: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
            signature,
        )
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    _attach_presentation_state(view)

    playlist_widget.PlaylistEditView._on_info(view, 12, None, "")

    assert item["auto_title"] is True
    assert saves == []


def test_local_embedded_title_replaces_filename_and_is_persisted():
    item = {
        "id": "media-1",
        "url": "C:/media/gnj_T_02_r720P.mp4",
        "title": "gnj_T_02_r720P",
        "auto_title": True,
        "base_duration_ticks": 10_000,
    }
    saves = []
    refreshed = []
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={12: playlist_widget._ThumbnailRequestIntent(False, True, False)},
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=lambda _token: None),
        _id_to_thumb={},
        _save=lambda: saves.append(True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_result_is_current = lambda item_id, source, signature: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
            signature,
        )
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    _attach_presentation_state(view, refreshed=refreshed)

    playlist_widget.PlaylistEditView._on_info(
        view,
        12,
        None,
        'Episódio 2: "Este é meu Filho"',
    )

    assert item["title"] == 'Episódio 2: "Este é meu Filho"'
    assert item["auto_title"] is False
    assert saves == [True]
    assert refreshed == [True]


def test_result_from_replaced_local_content_cannot_patch_playlist():
    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "title": "clip",
        "auto_title": True,
    }
    saves = []
    persisted = []
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={
            12: playlist_widget._ThumbnailRequestIntent(
                True,
                True,
                False,
                "100:200",
            )
        },
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=lambda _token: None),
        _id_to_thumb={},
        _save=lambda: saves.append(True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_result_is_current = lambda item_id, source, signature: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
            signature,
        )
    )
    _attach_presentation_state(
        view,
        source_signature="101:300",
        persisted=persisted,
    )
    pixmap = SimpleNamespace(
        isNull=lambda: False,
        toImage=lambda: SimpleNamespace(),
    )

    playlist_widget.PlaylistEditView._on_info(
        view,
        12,
        pixmap,
        "Stale title",
    )

    assert item["title"] == "clip"
    assert item["auto_title"] is True
    assert view._id_to_thumb == {}
    assert persisted == []
    assert saves == []


def test_playlist_widget_records_source_duration_through_public_boundary():
    recorded = []
    widget = SimpleNamespace(
        _edit_view=SimpleNamespace(
            notify_duration=lambda item_id, duration: recorded.append((item_id, duration))
        )
    )

    playlist_widget.PlaylistWidget.record_source_duration(widget, "media-1", 12_345)
    playlist_widget.PlaylistWidget.record_source_duration(widget, "", 12_345)
    playlist_widget.PlaylistWidget.record_source_duration(widget, "media-1", 0)

    assert recorded == [("media-1", 12_345)]


def test_temp_playlist_sessions_receive_unique_ids():
    loaded = []
    view = SimpleNamespace(
        lang=None,
        tr=lambda text: text,
        load_playlist=loaded.append,
    )

    first = PlaylistEditActionsMixin.load_temp_playlist(view, [])
    second = PlaylistEditActionsMixin.load_temp_playlist(view, [])

    assert first != second
    assert loaded[0]["id"] == first
    assert loaded[1]["id"] == second


def test_append_temp_playlist_items_rejects_stale_session():
    rebuilds = []
    edit_view = SimpleNamespace(
        _is_temp=True,
        _pl={"id": "current", "items": []},
        _reconcile_playlist=lambda: rebuilds.append(True),
    )
    widget = SimpleNamespace(_edit_view=edit_view)

    appended = playlist_widget.PlaylistWidget.append_temp_playlist_items(
        widget,
        "stale",
        [{"title": "Late result"}],
    )

    assert appended is False
    assert edit_view._pl["items"] == []
    assert rebuilds == []


def test_append_temp_playlist_items_updates_matching_session():
    rebuilds = []
    edit_view = SimpleNamespace(
        _is_temp=True,
        _pl={"id": "current", "items": []},
        _reconcile_playlist=lambda: rebuilds.append(True),
    )
    widget = SimpleNamespace(_edit_view=edit_view)
    item = {"title": "Imported"}

    appended = playlist_widget.PlaylistWidget.append_temp_playlist_items(
        widget,
        "current",
        [item],
    )
    item["title"] = "Mutated externally"

    assert appended is True
    assert edit_view._pl["items"] == [{"title": "Imported"}]
    assert rebuilds == [True]


def test_jw_duplicate_is_rejected_before_thumbnail_or_playlist_mutation(tmp_path):
    thumbnail = tmp_path / "thumb.jpg"
    thumbnail.write_bytes(b"thumb")
    thumbnail_copies = []
    existing = {
        "id": "existing",
        "title": "Song",
        "url": "https://akamd1.jw-cdn.org/x/sjjm_T_002_r480P.mp4",
        "key_symbol": "sjjm",
        "track": 2,
        "meps_language": 5,
    }
    view = SimpleNamespace(
        _pl={"id": "playlist", "items": [existing]},
        _playlist_thumbnail_store=SimpleNamespace(
            copy_from=lambda *args: thumbnail_copies.append(args)
        ),
    )

    result = PlaylistEditActionsMixin._on_jw_media_confirmed(
        view,
        {
            "title": "2. Song",
            "download_url": "https://akamd1.jw-cdn.org/y/sjjm_T_002_r720P.mp4",
            "media_type": "video",
            "pub": "sjjm",
            "track": 2,
            "language": "T",
            "meps_language": 5,
            "thumbnail_path": str(thumbnail),
        },
        "root",
        0,
    )

    assert result.added_count == 0
    assert result.duplicate_count == 1
    assert view._pl["items"] == [existing]
    assert thumbnail_copies == []


def test_jw_insert_persists_canonical_duration_and_official_thumbnail(
    monkeypatch,
) -> None:
    refreshes = []
    saves = []
    view = SimpleNamespace(
        _pl={"id": "playlist", "items": []},
        _tree_session=SimpleNamespace(
            owner_id="playlist:test",
            refresh=lambda: refreshes.append(True),
        ),
        _save=lambda: saves.append(True),
        _sync_playlist_chrome=lambda **_kwargs: None,
        _request_missing_thumbnails=lambda: None,
    )
    monkeypatch.setattr(playlist_widget.QTimer, "singleShot", lambda *_args: None)

    result = PlaylistEditActionsMixin._on_jw_media_confirmed(
        view,
        {
            "title": "Video",
            "download_url": "https://cdn.example/video.mp4",
            "media_type": "video",
            "duration_ticks": 120_000_000,
            "thumbnail_url": "https://cdn.example/video.jpg",
            "pub": "mwb",
            "track": 1,
            "language": "T",
        },
        "",
        -1,
    )

    assert result.added_count == 1
    [persisted] = view._pl["items"]
    assert persisted["base_duration_ticks"] == 120_000_000
    assert "duration_seconds" not in persisted
    assert persisted["thumbnail_url"] == "https://cdn.example/video.jpg"
    assert persisted["thumbnail_binding"] == "jw_artwork"
    assert refreshes == [True]
    assert saves == [True]


def test_linked_folder_rejects_a_previously_copied_source_as_duplicate(tmp_path):
    source = tmp_path / "source" / "S-337-26v_T_02_r720P.mp4"
    destination = tmp_path / "linked" / source.name
    queued = []
    warnings = []
    view = SimpleNamespace(
        _pl={
            "items": [
                {
                    "id": "existing",
                    "url": str(destination),
                    "source_url": str(source),
                    "type": "video",
                }
            ]
        },
        _is_watched=True,
        _watched_path=str(destination.parent),
        _tree_session=SimpleNamespace(pending_items=lambda: ()),
        _queue_watched_media_copy=lambda *args, **kwargs: queued.append((args, kwargs)),
        _list_id_for_section=lambda _section_id: "root",
        _notifications=SimpleNamespace(
            warning=warnings.append,
            success=lambda _message: None,
        ),
        tr=lambda message, _disambiguation=None, count=-1: message.replace(
            "%n", str(count)
        ),
    )

    PlaylistEditActionsMixin._add_files(view, [str(source)])

    assert queued == []
    assert warnings == ["1 file(s) already in playlist"]


def test_linked_folder_accepts_distinct_local_jw_filename_variants(tmp_path):
    first = tmp_path / "sjjm_E_02_r720P.mp4"
    second = tmp_path / "sjjm_E_02_r720P (1).mp4"
    first.write_bytes(b"first-recording")
    second.write_bytes(b"different-recording")
    queued = []
    view = SimpleNamespace(
        _pl={"items": []},
        _is_watched=True,
        _watched_path=str(tmp_path / "linked"),
        _tree_session=SimpleNamespace(
            pending_items=lambda: (),
            refresh=lambda: None,
        ),
        _queue_watched_media_copy=lambda item, **options: queued.append((item, options)),
        _list_id_for_section=lambda _section_id: "root",
        _sync_playlist_chrome=lambda **_kwargs: None,
        _notifications=SimpleNamespace(
            warning=lambda _message: None,
            success=lambda _message: None,
        ),
        tr=lambda message: message,
    )

    PlaylistEditActionsMixin._add_files(view, [str(first), str(second)])

    assert [Path(item["url"]).name for item, _options in queued] == [
        first.name,
        second.name,
    ]


def test_linked_folder_commit_deduplicates_legacy_destination(tmp_path):
    source_folder = tmp_path / "source"
    source_folder.mkdir()
    source = source_folder / "S-337-26v_T_02_r720P.mp4"
    source.write_bytes(b"same-video")
    linked = tmp_path / "linked"
    linked.mkdir()
    destination = linked / source.name
    destination.write_bytes(source.read_bytes())
    playlist = {
        "items": [
            {
                "id": "existing",
                "url": str(destination),
                "type": "video",
            }
        ]
    }
    removed = []
    refreshed = []
    warnings = []
    saves = []

    def submit(spec):
        result = spec.runner(
            lambda *_args: None,
            SimpleNamespace(is_set=lambda: False),
        )
        spec.commit(result)
        return True

    tree_session = SimpleNamespace(
        owner_id="playlist:linked",
        generation=1,
        playlist=playlist,
        add_pending=lambda *_args, **_kwargs: None,
        remove_pending=removed.append,
        refresh=lambda: refreshed.append(True),
    )
    item = {
        "id": "candidate",
        "title": source.stem,
        "url": str(source),
        "type": "video",
        "auto_title": True,
    }
    view = SimpleNamespace(
        _pl=playlist,
        _watched_path=str(linked),
        _tree_session=tree_session,
        _watched_folder_file_store=WatchedFolderFileStore(),
        _media_tree_runtime=SimpleNamespace(
            operations=SimpleNamespace(submit=submit),
            schedule_artifact_cleanup=lambda *_args, **_kwargs: None,
        ),
        _schedule_manifest_save=lambda *_args: saves.append(True),
        _sync_playlist_chrome=lambda **_kwargs: None,
        _request_missing_thumbnails=lambda: None,
        _notifications=SimpleNamespace(
            warning=warnings.append,
            success=lambda _message: None,
        ),
        tr=lambda message: message,
    )

    PlaylistEditActionsMixin._queue_watched_media_copy(
        view,
        item,
        source_path=str(source),
        target_list_id="root",
        target_list_index=-1,
    )

    assert [current["id"] for current in playlist["items"]] == ["existing"]
    assert [path.name for path in linked.iterdir()] == [source.name]
    assert removed == ["candidate"]
    assert refreshed == [True]
    assert warnings == ["File already in playlist"]
    assert saves == []


def test_linked_folder_commit_targets_rebound_snapshot_of_same_playlist(tmp_path):
    source = tmp_path / "source" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    linked = tmp_path / "linked"
    linked.mkdir()
    original_playlist = {"id": "linked", "items": []}
    rebound_playlist = {"id": "linked", "items": []}
    submitted = []
    saves = []
    tree_session = SimpleNamespace(
        owner_id="playlist:linked",
        generation=1,
        playlist=original_playlist,
        add_pending=lambda *_args, **_kwargs: None,
        remove_pending=lambda *_args: None,
        refresh=lambda: None,
    )
    view = SimpleNamespace(
        _pl=original_playlist,
        _watched_path=str(linked),
        _tree_session=tree_session,
        _watched_folder_file_store=WatchedFolderFileStore(),
        _media_tree_runtime=SimpleNamespace(
            operations=SimpleNamespace(
                submit=lambda spec: submitted.append(spec) or True,
            ),
            schedule_artifact_cleanup=lambda *_args, **_kwargs: None,
        ),
        _schedule_manifest_save=lambda _folder, playlist: saves.append(playlist),
        _sync_playlist_chrome=lambda **_kwargs: None,
        _request_missing_thumbnails=lambda: None,
        _notifications=SimpleNamespace(
            warning=lambda _message: None,
            success=lambda _message: None,
        ),
        tr=lambda message: message,
    )
    item = {
        "id": "candidate",
        "title": source.stem,
        "url": str(source),
        "type": "video",
    }

    PlaylistEditActionsMixin._queue_watched_media_copy(
        view,
        item,
        source_path=str(source),
        target_list_id="root",
        target_list_index=-1,
    )
    tree_session.playlist = rebound_playlist
    result = submitted[0].runner(
        lambda *_args: None,
        SimpleNamespace(is_set=lambda: False),
    )
    submitted[0].commit(result)

    assert original_playlist["items"] == []
    assert [current["id"] for current in rebound_playlist["items"]] == ["candidate"]
    assert saves == [rebound_playlist]


def test_linked_folder_commit_replaces_watcher_auto_adoption_at_requested_target(
    tmp_path,
):
    source = tmp_path / "source" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    linked = tmp_path / "linked"
    linked.mkdir()
    playlist = {
        "id": "linked",
        "items": [],
        "sections": [{"id": "section-1", "name": "Section", "position": 0}],
    }
    submitted = []
    tree_session = SimpleNamespace(
        owner_id="playlist:linked",
        generation=1,
        playlist=playlist,
        add_pending=lambda *_args, **_kwargs: None,
        remove_pending=lambda *_args: None,
        refresh=lambda: None,
    )
    view = SimpleNamespace(
        _pl=playlist,
        _watched_path=str(linked),
        _tree_session=tree_session,
        _watched_folder_file_store=WatchedFolderFileStore(),
        _media_tree_runtime=SimpleNamespace(
            operations=SimpleNamespace(
                submit=lambda spec: submitted.append(spec) or True,
            ),
            schedule_artifact_cleanup=lambda *_args, **_kwargs: None,
        ),
        _schedule_manifest_save=lambda *_args: None,
        _sync_playlist_chrome=lambda **_kwargs: None,
        _request_missing_thumbnails=lambda: None,
        _notifications=SimpleNamespace(
            warning=lambda _message: None,
            success=lambda _message: None,
        ),
        tr=lambda message: message,
    )
    item = {
        "id": "candidate",
        "title": source.stem,
        "url": str(source),
        "type": "video",
    }

    PlaylistEditActionsMixin._queue_watched_media_copy(
        view,
        item,
        source_path=str(source),
        target_list_id="section:section-1",
        target_list_index=-1,
    )
    result = submitted[0].runner(
        lambda *_args: None,
        SimpleNamespace(is_set=lambda: False),
    )
    playlist["items"] = [
        {
            "id": "scanner-auto-adopted",
            "title": result.destination.stem,
            "url": str(result.destination),
            "type": "video",
        }
    ]
    submitted[0].commit(result)

    assert [current["id"] for current in playlist["items"]] == ["candidate"]
    assert playlist["items"][0]["section_id"] == "section-1"


def test_linked_folder_commit_does_not_fall_back_to_root_when_target_disappears(
    tmp_path,
):
    source = tmp_path / "source" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    linked = tmp_path / "linked"
    linked.mkdir()
    playlist = {"id": "linked", "items": [], "sections": []}
    submitted = []
    cleanups = []
    warnings = []
    tree_session = SimpleNamespace(
        owner_id="playlist:linked",
        generation=1,
        playlist=playlist,
        add_pending=lambda *_args, **_kwargs: None,
        remove_pending=lambda *_args: None,
        refresh=lambda: None,
    )
    view = SimpleNamespace(
        _pl=playlist,
        _watched_path=str(linked),
        _tree_session=tree_session,
        _watched_folder_file_store=WatchedFolderFileStore(),
        _media_tree_runtime=SimpleNamespace(
            operations=SimpleNamespace(
                submit=lambda spec: submitted.append(spec) or True,
            ),
            schedule_artifact_cleanup=lambda paths, **_kwargs: cleanups.extend(paths),
        ),
        _schedule_manifest_save=lambda *_args: None,
        _sync_playlist_chrome=lambda **_kwargs: None,
        _request_missing_thumbnails=lambda: None,
        _notifications=SimpleNamespace(
            warning=warnings.append,
            success=lambda _message: None,
        ),
        tr=lambda message: message,
    )
    item = {
        "id": "candidate",
        "title": source.stem,
        "url": str(source),
        "type": "video",
    }

    PlaylistEditActionsMixin._queue_watched_media_copy(
        view,
        item,
        source_path=str(source),
        target_list_id="section:removed",
        target_list_index=-1,
    )
    result = submitted[0].runner(
        lambda *_args: None,
        SimpleNamespace(is_set=lambda: False),
    )
    submitted[0].commit(result)

    assert playlist["items"] == []
    assert cleanups == [result.destination]
    assert warnings == ["Could not update the linked folder."]


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)
        return callback

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class _SyncThread:
    def __init__(self):
        self.sync_complete = _Signal()
        self.sync_failed = _Signal()
        self.finished = _Signal()
        self.started = False

    def start(self):
        self.started = True

    def deleteLater(self):
        pass


class _WatchedFolderStore:
    def __init__(self, thread):
        self.thread = thread

    def pending_files(self, _path):
        return ["slides.pdf"]

    def create_sync_thread(self, *_args, **_kwargs):
        return self.thread


class _Notifications:
    def __init__(self):
        self.infos = []
        self.successes = []
        self.errors = []

    def information(self, message):
        self.infos.append(message)

    def success(self, message):
        self.successes.append(message)

    def error(self, message):
        self.errors.append(message)


def test_watched_folder_sync_is_quiet_unless_it_fails():
    thread = _SyncThread()
    notifications = _Notifications()
    refreshes = []
    view = SimpleNamespace(
        _watched_path="folder",
        _watched_folder_playlist_store=_WatchedFolderStore(thread),
        _media_tree_runtime=SimpleNamespace(resource_lanes=ResourceLaneRegistry()),
        _notifications=notifications,
        _wf_refresh_pending=False,
        _wf_sync_thread=None,
        _current_media_context=lambda: SimpleNamespace(api_code="E", fallback_code="T"),
        tr=lambda text, *_args: text,
        refresh_watched_folder=lambda: refreshes.append(True),
    )

    playlist_widget.PlaylistEditView._start_wf_sync(view, ("pending",))
    thread.sync_complete.emit()
    thread.sync_failed.emit("disk full")

    assert thread.started is True
    assert refreshes == [True]
    assert notifications.infos == []
    assert notifications.successes == []
    assert notifications.errors == ["disk full"]


def test_watcher_coalesces_events_and_defers_hidden_list_refresh():
    starts = []
    list_refreshes = []
    edit_refreshes = []
    superseded = []
    notifications = []
    folder_path = "C:/linked/one"
    widget = SimpleNamespace(
        _wf_root_refresh_pending=False,
        _wf_pending_sub_path="",
        _wf_refresh_debounce=SimpleNamespace(start=lambda: starts.append(True)),
        _list_view=SimpleNamespace(refresh_watched=lambda: list_refreshes.append(True)),
        _stack=SimpleNamespace(currentIndex=lambda: 1),
        _edit_view=SimpleNamespace(
            _is_watched=True,
            _watched_path=folder_path,
            refresh_watched_folder=lambda: edit_refreshes.append(True),
            supersede_watched_folder_refresh=lambda: superseded.append(True),
        ),
        _watched_folder_playlist_store=SimpleNamespace(
            notify_external_change=lambda: notifications.append(True)
        ),
    )

    playlist_widget.PlaylistWidget._on_folder_changed(widget)
    playlist_widget.PlaylistWidget._on_subfolder_changed(widget, folder_path)
    playlist_widget.PlaylistWidget._flush_watched_folder_refresh(widget)

    assert starts == [True, True]
    assert superseded == [True, True]
    assert list_refreshes == []
    assert edit_refreshes == [True]
    assert notifications == [True]


def test_watched_folder_refresh_schedules_disk_snapshot_off_qt_thread():
    submitted = []

    class _Future:
        def add_done_callback(self, callback):
            self.callback = callback

    class _Executor:
        def submit(self, callback, *args):
            submitted.append((callback, args))
            return _Future()

    folder_path = "C:/linked/one"
    view = SimpleNamespace(
        _flush_image_framing_save=lambda: None,
        _reset_watched_folder_refresh_retry=lambda: None,
        _wf_refresh_shutdown=False,
        _is_watched=True,
        _watched_path=folder_path,
        _pending_manifest_saves={},
        _wf_sync_thread=None,
        _wf_refresh_inflight=None,
        _wf_refresh_pending=False,
        _wf_refresh_generation=0,
        _manifest_state_generation=0,
        _wf_refresh_future=None,
        _watched_folder_refresh_executor=_Executor(),
        _read_watched_folder_snapshot=lambda path: path,
        _emit_watched_folder_refresh_completed=lambda *_args: None,
    )

    playlist_widget.PlaylistEditView.refresh_watched_folder(view)

    assert len(submitted) == 1
    callback, args = submitted[0]
    assert callback is view._read_watched_folder_snapshot
    assert args == (folder_path,)
    assert view._wf_refresh_inflight is not None
    assert view._wf_refresh_future is not None


def test_initial_watched_folder_open_defers_all_disk_reads():
    rebuilt = []
    states = []
    refreshes = []
    folder_path = "C:/linked/one"
    view = SimpleNamespace(
        _flush_image_framing_save=lambda: None,
        _reset_watched_folder_refresh_retry=lambda: None,
        _wf_refresh_pending=False,
        _is_watched=False,
        _watched_path="",
        _is_temp=False,
        _pending_manifest_saves={},
        _thumb_queue=SimpleNamespace(clear=lambda: None),
        _thumb_scan_timer=SimpleNamespace(stop=lambda: None),
        _thumb_scan_items=[],
        _id_to_thumb={},
        _thumb_idx_to_id={},
        _thumb_idx_to_source={},
        _thumb_idx_to_intent={},
        _thumb_pending_item_ids=set(),
        _wf_file_availability=(),
        _watched_folder_playlist_store=SimpleNamespace(
            load_playlist=lambda _path: (_ for _ in ()).throw(
                AssertionError("disk read ran on Qt thread")
            )
        ),
        catalog_bridge=SimpleNamespace(set_playlist_ref=lambda value: rebuilt.append(value)),
        _tree_session=SimpleNamespace(
            activate=lambda value: rebuilt.append(value),
        ),
        bridge=SimpleNamespace(set_state=lambda **values: states.append(values)),
        tr=lambda value: value,
        refresh_watched_folder=lambda: refreshes.append(True),
    )

    playlist_widget.PlaylistEditView.load_watched_folder(view, folder_path)

    assert view._pl is None
    assert refreshes == [True]
    assert states == [
        {
            "name": "one",
            "is_watched": True,
            "is_loading": True,
            "item_count": 0,
        }
    ]
    assert rebuilt[0] == rebuilt[1]


def test_removed_playlist_item_cancels_all_owned_thumbnail_requests():
    invalidated = []
    view = SimpleNamespace(
        _thumb_idx_to_id={3: "removed", 4: "kept", 5: "removed"},
        _thumb_idx_to_source={3: "a", 4: "b", 5: "c"},
        _thumb_idx_to_intent={3: object(), 4: object(), 5: object()},
        _thumb_pending_item_ids={"removed", "kept"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )

    playlist_widget.PlaylistEditView._cancel_thumbnail_requests_for_item(
        view,
        "removed",
    )

    assert invalidated == [3, 5]
    assert view._thumb_idx_to_id == {4: "kept"}
    assert view._thumb_pending_item_ids == {"kept"}


def test_watched_folder_refresh_applies_only_current_snapshot():
    folder_path = "C:/linked/one"
    key = os.path.normcase(os.path.abspath(folder_path))
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist={"items": []},
        availability=(),
        pending_files=(),
    )
    applied = []
    view = SimpleNamespace(
        _wf_refresh_inflight=(7, key),
        _wf_refresh_future=object(),
        _wf_refresh_superseded=False,
        _wf_refresh_manifest_generation=0,
        _manifest_state_generation=0,
        _watched_path=folder_path,
        _is_watched=True,
        _pending_manifest_saves={},
        _wf_sync_thread=None,
        _wf_refresh_pending=False,
        _apply_watched_folder_snapshot=applied.append,
        _reset_watched_folder_refresh_retry=lambda: None,
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        key,
        snapshot,
        None,
    )

    assert applied == [snapshot]
    assert view._wf_refresh_inflight is None
    assert view._wf_refresh_future is None


def test_initial_watched_folder_failure_schedules_cloud_retry():
    folder_path = "C:/linked/one"
    key = os.path.normcase(os.path.abspath(folder_path))
    retries = []
    error = OSError("temporarily locked")
    view = SimpleNamespace(
        _wf_refresh_inflight=(7, key),
        _wf_refresh_future=object(),
        _wf_refresh_superseded=False,
        _wf_refresh_pending=False,
        _wf_refresh_manifest_generation=0,
        _manifest_state_generation=0,
        _watched_path=folder_path,
        _is_watched=True,
        _pending_manifest_saves={},
        _wf_sync_thread=None,
        _schedule_watched_folder_refresh_retry=lambda: retries.append(True),
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        key,
        None,
        error,
    )

    assert retries == [True]
    assert view._wf_refresh_inflight is None


def test_availability_refresh_patches_media_without_full_qml_reset(
    monkeypatch,
):
    path = os.path.abspath("C:/linked/one/video.mp4")
    key = os.path.normcase(os.path.normpath(path))
    playlist = {
        "items": [
            {
                "id": "media-1",
                "url": path,
                "type": "video",
            }
        ]
    }
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist=playlist,
        availability=((key, (True, 2_048, 20)),),
        pending_files=(),
    )
    chrome_updates = []
    reconciled = []
    requested_nodes = []
    scheduled = []
    view = SimpleNamespace(
        _pl=playlist,
        _wf_file_availability=((key, (False, 0, 0, 0, 0, 0)),),
        _watched_playlist_equivalent=lambda _playlist: True,
        _publish_tree_snapshot=lambda: reconciled.append(True),
        _tree_session=SimpleNamespace(request_nodes=requested_nodes.append),
        _sync_playlist_chrome=lambda **kwargs: chrome_updates.append(kwargs),
        _cancel_thumbnail_requests_for_item=lambda _item_id: None,
        _request_missing_thumbnails=lambda: None,
        _start_wf_sync=lambda _pending: None,
    )
    view._availability_changed_item_ids = lambda current, availability: (
        playlist_widget.PlaylistEditView._availability_changed_item_ids(
            view,
            current,
            availability,
        )
    )
    monkeypatch.setattr(
        playlist_widget,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: scheduled.append((delay, callback))),
    )

    playlist_widget.PlaylistEditView._apply_watched_folder_snapshot(
        view,
        snapshot,
    )

    assert chrome_updates == [{"emit_data_changed": False}]
    assert reconciled == [True]
    assert requested_nodes == [{"media-1"}]
    assert len(scheduled) == 1


def test_watched_folder_refresh_ignores_superseded_snapshot():
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist={"items": []},
        availability=(),
        pending_files=(),
    )
    applied = []
    view = SimpleNamespace(
        _wf_refresh_inflight=(8, "current"),
        _apply_watched_folder_snapshot=applied.append,
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        "stale",
        snapshot,
        None,
    )

    assert applied == []
    assert view._wf_refresh_inflight == (8, "current")


def test_watched_folder_refresh_discards_immediately_superseded_snapshot():
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist={"items": []},
        availability=(),
        pending_files=(),
    )
    applied = []
    view = SimpleNamespace(
        _wf_refresh_inflight=(7, "current"),
        _wf_refresh_future=object(),
        _wf_refresh_superseded=True,
        _wf_refresh_pending=False,
        _wf_refresh_manifest_generation=0,
        _manifest_state_generation=0,
        _apply_watched_folder_snapshot=applied.append,
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        "current",
        snapshot,
        None,
    )

    assert applied == []
    assert view._wf_refresh_superseded is False
    assert view._wf_refresh_inflight is None


def test_watched_folder_refresh_discards_snapshot_when_new_event_is_pending(
    monkeypatch,
):
    scheduled = []
    monkeypatch.setattr(
        playlist_widget,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: scheduled.append((delay, callback))),
    )
    folder_path = "C:/linked/one"
    key = os.path.normcase(os.path.abspath(folder_path))
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist={"items": []},
        availability=(),
        pending_files=(),
    )
    applied = []
    refresh = lambda: None
    view = SimpleNamespace(
        _wf_refresh_inflight=(7, key),
        _wf_refresh_future=object(),
        _wf_refresh_pending=True,
        _wf_refresh_superseded=False,
        _wf_refresh_manifest_generation=0,
        _manifest_state_generation=0,
        refresh_watched_folder=refresh,
        _apply_watched_folder_snapshot=applied.append,
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        key,
        snapshot,
        None,
    )

    assert applied == []
    assert view._wf_refresh_pending is False
    assert scheduled == [(0, refresh)]
