from solin.ui.qml.media_tree.state import (
    MediaAvailability,
    MediaPresentationState,
    MediaProbeResult,
    MediaStateRegistry,
)
import pytest


def test_registry_rejects_a_probe_for_an_obsolete_source_generation() -> None:
    registry = MediaStateRegistry()
    first = registry.begin_probe("playlist:1", "media-1", "old.mp4")
    second = registry.begin_probe("playlist:1", "media-1", "new.mp4")

    stale = registry.accept(
        MediaProbeResult(
            first,
            MediaPresentationState(availability=MediaAvailability.MISSING),
        )
    )
    current = registry.accept(
        MediaProbeResult(
            second,
            MediaPresentationState(
                availability=MediaAvailability.AVAILABLE,
                local_path="new.mp4",
                thumbnail_source="image://playlistthumbs/media-1/1",
            ),
        )
    )

    assert stale is False
    assert current is True
    assert registry.state("playlist:1", "media-1").local_path == "new.mp4"


def test_presentation_state_rejects_fractional_negative_progress() -> None:
    with pytest.raises(ValueError, match="must be -1 or between 0 and 1"):
        MediaPresentationState(cloud_progress=-0.5)


def test_registry_rejects_a_completion_from_before_owner_clear() -> None:
    registry = MediaStateRegistry()
    stale = registry.begin_probe("playlist:1", "media-1", "video.mp4")
    registry.clear_owner("playlist:1")
    current = registry.begin_probe("playlist:1", "media-1", "video.mp4")

    assert stale != current
    assert not registry.accept(
        MediaProbeResult(
            stale,
            MediaPresentationState(availability=MediaAvailability.MISSING),
        )
    )


def test_registry_preserves_last_known_visuals_while_rechecking() -> None:
    registry = MediaStateRegistry()
    key = registry.begin_probe("meeting:1", "media-1", "video.mp4")
    registry.accept(
        MediaProbeResult(
            key,
            MediaPresentationState(
                availability=MediaAvailability.AVAILABLE,
                local_path="video.mp4",
                thumbnail_source="image://playlistthumbs/media-1/3",
                duration_ticks=10_000_000,
            ),
        )
    )

    registry.begin_probe("meeting:1", "media-1", "video.mp4")
    checking = registry.state("meeting:1", "media-1")

    assert checking.availability == MediaAvailability.CHECKING
    assert checking.thumbnail_source == "image://playlistthumbs/media-1/3"
    assert checking.duration_ticks == 10_000_000


def test_registry_invalidates_visuals_when_node_source_changes() -> None:
    registry = MediaStateRegistry()
    key = registry.begin_probe("meeting:1", "media-1", "old.mp4")
    registry.accept(
        MediaProbeResult(
            key,
            MediaPresentationState(
                availability=MediaAvailability.AVAILABLE,
                local_path="old.mp4",
                thumbnail_source="image://playlistthumbs/media-1/3",
                duration_ticks=10_000_000,
                image_aspect_ratio=16 / 9,
            ),
        )
    )

    registry.begin_probe("meeting:1", "media-1", "new.mp4")
    checking = registry.state("meeting:1", "media-1")

    assert checking.local_path == ""
    assert checking.thumbnail_source == ""
    assert checking.duration_ticks == 0
    assert checking.image_aspect_ratio == 0


def test_registry_distinguishes_temporary_unavailability_from_missing() -> None:
    registry = MediaStateRegistry()
    key = registry.begin_probe("playlist:1", "media-1", "cloud.mp4")

    assert registry.accept(
        MediaProbeResult(
            key,
            MediaPresentationState(
                availability=MediaAvailability.TEMPORARILY_UNAVAILABLE,
                error="Cloud provider is hydrating the file",
            ),
        )
    )
    assert (
        registry.state("playlist:1", "media-1").availability
        == MediaAvailability.TEMPORARILY_UNAVAILABLE
    )
