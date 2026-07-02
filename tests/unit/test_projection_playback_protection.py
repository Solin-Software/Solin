from __future__ import annotations

from types import SimpleNamespace

from solin.widgets.projection.playlist import ProjectionPlaylistMixin


class _Protection:
    def __init__(self, locked: bool) -> None:
        self.locked = locked
        self.blocked = 0

    def allow_manual_projection_change(self, *, notify=True) -> bool:
        if not self.locked:
            return True
        if notify:
            self.blocked += 1
        return False


class _Signal:
    def __init__(self) -> None:
        self.values: list[int] = []

    def emit(self, value: int) -> None:
        self.values.append(value)


def _playlist_host(*, locked: bool):
    return SimpleNamespace(
        _playback_protection=_Protection(locked),
        _playlist=[{"title": "One"}, {"title": "Two"}],
        _playlist_index=0,
        _played_indices={0},
        _update_nav_buttons=lambda: None,
        playlist_navigate=_Signal(),
    )


def test_manual_next_does_not_mutate_index_while_protected() -> None:
    host = _playlist_host(locked=True)

    ProjectionPlaylistMixin._on_next_clicked(host)

    assert host._playlist_index == 0
    assert host._played_indices == {0}
    assert host.playlist_navigate.values == []
    assert host._playback_protection.blocked == 1


def test_manual_next_remains_available_when_unlocked() -> None:
    host = _playlist_host(locked=False)

    ProjectionPlaylistMixin._on_next_clicked(host)

    assert host._playlist_index == 1
    assert host._played_indices == {0, 1}
    assert host.playlist_navigate.values == [1]
