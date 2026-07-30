"""Unit tests for the projection bar's gated preview-frame output wiring.

The bar tells the media engine to emit preview frames only while an operator is
actually watching (preview expanded or fullscreen active, and the media is
video). On the Qt engine (no set_frame_output_enabled) it must be a silent no-op.
"""

from __future__ import annotations

from solin.widgets.projection.bar import ProjectionBar


class _ObsMedia:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_frame_output_enabled(self, enabled: bool) -> None:
        self.calls.append(enabled)


class _Overlay:
    def __init__(self, active: bool) -> None:
        self._active = active

    def is_active(self) -> bool:
        return self._active


def _bar(media, *, expanded=False, mode="video", is_audio=False, fullscreen=False):
    bar = ProjectionBar.__new__(ProjectionBar)  # bypass heavy Qt construction
    bar.media = media
    bar._expanded = expanded
    bar._mode = mode
    bar._is_audio = is_audio
    bar._fullscreen_overlay = _Overlay(True) if fullscreen else None
    return bar


def test_collapsed_video_disables_frame_output():
    media = _ObsMedia()
    _bar(media, expanded=False, mode="video")._sync_frame_output()
    assert media.calls == [False]


def test_expanded_video_enables_frame_output():
    media = _ObsMedia()
    _bar(media, expanded=True, mode="video")._sync_frame_output()
    assert media.calls == [True]


def test_fullscreen_video_enables_frame_output():
    media = _ObsMedia()
    _bar(media, expanded=False, mode="video", fullscreen=True)._sync_frame_output()
    assert media.calls == [True]


def test_expanded_audio_disables_frame_output():
    media = _ObsMedia()
    _bar(media, expanded=True, mode="video", is_audio=True)._sync_frame_output()
    assert media.calls == [False]  # audio has no video frames


def test_expanded_image_disables_frame_output():
    media = _ObsMedia()
    _bar(media, expanded=True, mode="image")._sync_frame_output()
    assert media.calls == [False]


def test_qt_engine_without_setter_is_noop():
    class _QtMedia:  # no set_frame_output_enabled
        pass

    # Must not raise even though the engine lacks the method.
    _bar(_QtMedia(), expanded=True, mode="video")._sync_frame_output()
