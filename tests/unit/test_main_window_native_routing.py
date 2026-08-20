from solin.main_window import _use_native_media_presentation


def test_every_media_surface_uses_native_presentation_for_raw_video_or_program() -> None:
    assert _use_native_media_presentation(
        native_window_routing_ready=True,
        mirror_enabled=False,
        raw_video=True,
    )
    assert _use_native_media_presentation(
        native_window_routing_ready=True,
        mirror_enabled=True,
        raw_video=False,
    )
    assert not _use_native_media_presentation(
        native_window_routing_ready=True,
        mirror_enabled=False,
        raw_video=False,
    )
    assert not _use_native_media_presentation(
        native_window_routing_ready=False,
        mirror_enabled=True,
        raw_video=True,
    )
