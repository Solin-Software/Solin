from dataclasses import FrozenInstanceError

import pytest

from solin.core.media.playback_request import (
    MIN_PLAYBACK_INTERVAL_MS,
    TICKS_PER_MILLISECOND,
    MediaPlaybackRequest,
    MediaTrim,
    PlaybackCachePolicy,
    ResolvedPlaybackRange,
)


def test_media_trim_preserves_exact_ticks_and_is_immutable():
    trim = MediaTrim(
        start_trim_ticks=10_000_001,
        end_trim_ticks=20_000_009,
        base_duration_ticks=100_000_019,
    )

    assert trim.start_trim_ticks == 10_000_001
    assert trim.end_trim_ticks == 20_000_009
    assert trim.base_duration_ticks == 100_000_019
    assert trim.custom is True
    with pytest.raises(FrozenInstanceError):
        trim.start_trim_ticks = 0  # type: ignore[misc]


@pytest.mark.parametrize(
    "field_name,value,error_type",
    [
        ("start_trim_ticks", -1, ValueError),
        ("end_trim_ticks", -1, ValueError),
        ("base_duration_ticks", -1, ValueError),
        ("start_trim_ticks", 1.5, TypeError),
        ("end_trim_ticks", True, TypeError),
    ],
)
def test_media_trim_rejects_invalid_tick_values(field_name, value, error_type):
    values = {
        "start_trim_ticks": 0,
        "end_trim_ticks": 0,
        "base_duration_ticks": 0,
    }
    values[field_name] = value

    with pytest.raises(error_type):
        MediaTrim(**values)


def test_media_trim_rejects_values_outside_sqlite_integer_range():
    with pytest.raises(ValueError, match="persisted integer range"):
        MediaTrim(start_trim_ticks=1 << 63)


def test_media_trim_requires_minimum_interval_when_base_duration_is_known():
    with pytest.raises(ValueError, match="at least 100 ms"):
        MediaTrim(
            start_trim_ticks=400 * TICKS_PER_MILLISECOND,
            end_trim_ticks=501 * TICKS_PER_MILLISECOND,
            base_duration_ticks=1_000 * TICKS_PER_MILLISECOND,
        )

    trim = MediaTrim(
        start_trim_ticks=400 * TICKS_PER_MILLISECOND,
        end_trim_ticks=500 * TICKS_PER_MILLISECOND,
        base_duration_ticks=1_000 * TICKS_PER_MILLISECOND,
    )
    assert trim.custom is True


def test_media_trim_accepts_exact_minimum_interval_boundary():
    trim = MediaTrim(
        start_trim_ticks=400 * TICKS_PER_MILLISECOND,
        end_trim_ticks=500 * TICKS_PER_MILLISECOND,
        base_duration_ticks=1_000 * TICKS_PER_MILLISECOND,
    )

    assert trim.resolve(1_000).duration_ms == MIN_PLAYBACK_INTERVAL_MS


def test_media_trim_without_offsets_is_not_custom():
    assert MediaTrim(base_duration_ticks=30_000_000).custom is False


def test_media_trim_builds_jw_offsets_from_editor_bounds():
    trim = MediaTrim.from_millisecond_bounds(1_250.25, 8_500.75, 10_000.0)

    assert trim.start_trim_ticks == 12_502_500
    assert trim.end_trim_ticks == 14_992_500
    assert trim.base_duration_ticks == 100_000_000


@pytest.mark.parametrize(
    "bounds,error_type",
    [
        ((0, 0, 10_000), ValueError),
        ((-1, 5_000, 10_000), ValueError),
        ((0, 10_001, 10_000), ValueError),
        ((0, 50, 10_000), ValueError),
        ((0, float("nan"), 10_000), TypeError),
        ((False, 5_000, 10_000), TypeError),
    ],
)
def test_media_trim_rejects_invalid_editor_bounds(bounds, error_type):
    with pytest.raises(error_type):
        MediaTrim.from_millisecond_bounds(*bounds)


def test_resolve_uses_actual_duration_and_rounds_both_bounds_inward():
    trim = MediaTrim(
        start_trim_ticks=10 * TICKS_PER_MILLISECOND + 1,
        end_trim_ticks=20 * TICKS_PER_MILLISECOND + 1,
    )

    resolved = trim.resolve(1_000)

    assert resolved == ResolvedPlaybackRange(
        start_ms=11,
        end_ms=979,
        source_duration_ms=1_000,
    )
    assert resolved.duration_ms == 968
    assert resolved.custom is True


def test_resolve_revalidates_minimum_interval_against_actual_duration():
    trim = MediaTrim(
        start_trim_ticks=400 * TICKS_PER_MILLISECOND,
        end_trim_ticks=400 * TICKS_PER_MILLISECOND,
        base_duration_ticks=2_000 * TICKS_PER_MILLISECOND,
    )

    with pytest.raises(ValueError, match="at least 100 ms"):
        trim.resolve(850)


def test_resolve_uses_larger_actual_duration_without_scaling_exact_offsets():
    trim = MediaTrim(
        start_trim_ticks=100 * TICKS_PER_MILLISECOND,
        end_trim_ticks=200 * TICKS_PER_MILLISECOND,
        base_duration_ticks=1_000 * TICKS_PER_MILLISECOND,
    )

    resolved = trim.resolve(1_500)

    assert resolved.start_ms == 100
    assert resolved.end_ms == 1_300
    assert resolved.source_duration_ms == 1_500


def test_resolve_rejects_trim_that_exceeds_actual_duration():
    trim = MediaTrim(end_trim_ticks=1_001 * TICKS_PER_MILLISECOND)

    with pytest.raises(ValueError):
        trim.resolve(1_000)


def test_resolve_rejects_exact_tick_range_that_is_too_short_at_ms_precision():
    trim = MediaTrim(
        start_trim_ticks=1,
        end_trim_ticks=899 * TICKS_PER_MILLISECOND + 1,
        base_duration_ticks=1_000 * TICKS_PER_MILLISECOND + 2,
    )

    with pytest.raises(ValueError, match="at least 100 ms"):
        trim.resolve(1_000)


@pytest.mark.parametrize("duration", [0, -1, 1.5, True])
def test_resolve_rejects_invalid_actual_duration(duration):
    error_type = TypeError if isinstance(duration, (float, bool)) else ValueError
    with pytest.raises(error_type):
        MediaTrim().resolve(duration)


def test_resolved_full_range_is_not_custom():
    resolved = MediaTrim().resolve(5_000)

    assert resolved.start_ms == 0
    assert resolved.end_ms == 5_000
    assert resolved.duration_ms == 5_000
    assert resolved.custom is False


def test_media_playback_request_has_safe_defaults_and_is_immutable():
    request = MediaPlaybackRequest("https://cdn.example/media.mp4")

    assert request.trim is None
    assert request.autoplay is True
    assert request.cache_policy is PlaybackCachePolicy.PROFILE_DEFAULT
    with pytest.raises(FrozenInstanceError):
        request.autoplay = False  # type: ignore[misc]


def test_media_playback_request_accepts_explicit_session_policy():
    trim = MediaTrim(start_trim_ticks=1_000_000, base_duration_ticks=10_000_000)
    request = MediaPlaybackRequest(
        source="clip.mp4",
        trim=trim,
        autoplay=False,
        cache_policy=PlaybackCachePolicy.TEMPORARY,
    )

    assert request.source == "clip.mp4"
    assert request.trim is trim
    assert request.autoplay is False
    assert request.cache_policy.value == "temporary"


def test_playback_cache_policy_values_are_stable_wire_values():
    assert [policy.value for policy in PlaybackCachePolicy] == [
        "profile_default",
        "persistent",
        "temporary",
    ]


@pytest.mark.parametrize(
    "kwargs,error_type",
    [
        ({"source": ""}, ValueError),
        ({"source": "   "}, ValueError),
        ({"source": 42}, TypeError),
        ({"source": "clip.mp4", "trim": object()}, TypeError),
        ({"source": "clip.mp4", "autoplay": 1}, TypeError),
        ({"source": "clip.mp4", "cache_policy": "temporary"}, TypeError),
    ],
)
def test_media_playback_request_rejects_invalid_contract_values(kwargs, error_type):
    with pytest.raises(error_type):
        MediaPlaybackRequest(**kwargs)


def test_public_minimum_interval_is_100_milliseconds():
    assert MIN_PLAYBACK_INTERVAL_MS == 100
