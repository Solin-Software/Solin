"""The native video surface must announce geometry changes.

The engine renders into this window through an obs_display sized when the window
target was last sent. Nothing re-sends that target on its own, so a surface that
resizes without announcing leaves the display at the old dimensions. The inset
contract is covered in test_projection_app_fullscreen; this covers the signal that
makes the re-send happen at all.
"""

from __future__ import annotations

import pytest

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from solin.widgets.projection.native_surface import NativeVideoSurface


_APP = QApplication.instance() or QApplication([])
# Comfortably longer than the surface's coalescing interval.
_SETTLE_MS = 200


@pytest.fixture
def shown_surface(request) -> tuple[QWidget, NativeVideoSurface]:
    host = QWidget()

    def delete_host() -> None:
        host.deleteLater()
        QCoreApplication.sendPostedEvents(host, QEvent.Type.DeferredDelete)

    # The host owns the surface, input overlay and settle timer.
    request.addfinalizer(delete_host)
    request.addfinalizer(host.close)
    host.resize(400, 300)
    surface = NativeVideoSurface(host)
    surface.show()
    host.show()
    QTest.qWait(_SETTLE_MS)  # drain the announcement showEvent schedules
    return host, surface


def _announcements(surface: NativeVideoSurface) -> list[int]:
    """Collect geometry announcements emitted by the surface's settle timer."""
    seen: list[int] = []
    surface.geometry_changed.connect(lambda: seen.append(1))
    return seen


def test_resizing_the_surface_announces_new_geometry(shown_surface) -> None:
    _host, surface = shown_surface
    seen = _announcements(surface)

    surface.setGeometry(0, 0, 320, 180)
    QTest.qWait(_SETTLE_MS)

    assert seen, "a resize must re-announce the handle"
    assert (surface.width(), surface.height()) == (320, 180)


def test_moving_the_surface_announces_new_geometry(shown_surface) -> None:
    """The target carries a global origin, and a move can change the DPR."""
    _host, surface = shown_surface
    surface.setGeometry(0, 0, 320, 180)
    QTest.qWait(_SETTLE_MS)
    seen = _announcements(surface)

    surface.move(24, 32)
    QTest.qWait(_SETTLE_MS)

    assert seen, "a move must re-announce the handle"


def test_a_burst_of_resizes_announces_once(shown_surface) -> None:
    """Each announcement costs an IPC round trip, so a drag must not flood it."""
    _host, surface = shown_surface
    seen = _announcements(surface)

    for width in range(200, 260, 10):
        surface.setGeometry(0, 0, width, int(width * 9 / 16))
    QTest.qWait(_SETTLE_MS)

    assert len(seen) == 1, f"expected one coalesced announcement, got {len(seen)}"


def test_the_input_overlay_still_tracks_the_surface(shown_surface) -> None:
    """The announcement must not displace the overlay resizing already did."""
    host, surface = shown_surface
    surface.set_input_target(host)

    surface.setGeometry(0, 0, 256, 144)
    QTest.qWait(_SETTLE_MS)

    overlay = surface.input_overlay
    assert overlay is not None
    assert overlay.size() == surface.size()
