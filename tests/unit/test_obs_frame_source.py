"""Unit tests for the libobs frame-injection source bridge (no real libobs)."""

from __future__ import annotations

from solin.core.media.obs_frame_source import ObsFrameSource, create_frame_source


class _RaisingSourceFactory:
    def create(self, _kind, _name, _settings):
        raise RuntimeError("solin_frame_source plugin not registered")


class _FakeRuntime:
    ob = type("_Ob", (), {"Source": _RaisingSourceFactory()})()


def test_create_frame_source_returns_none_when_plugin_missing():
    # Graceful: the caller falls back to the Qt frame path instead of crashing.
    assert create_frame_source(_FakeRuntime()) is None


def test_push_is_safe_with_no_source():
    ObsFrameSource(None).push(None)  # no source, no image → no-op, no crash


def test_release_is_idempotent():
    fs = ObsFrameSource(None)
    fs.release()
    fs.release()
    assert fs.source is None
