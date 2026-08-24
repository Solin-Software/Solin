from __future__ import annotations

import pytest

from solin.core.scenes.editor_geometry import crop_geometry, resize_rect, snap_move_rect
from solin.core.scenes.model import Crop, NormalizedRect


def test_editor_resize_preserves_aspect_unless_free_resize_is_requested() -> None:
    original = NormalizedRect(x=0.2, y=0.2, width=0.4, height=0.3)

    preserved = resize_rect(original, "bottom_right", 0.2, 0.04, preserve_aspect=True)
    free = resize_rect(original, "bottom_right", 0.2, 0.04, preserve_aspect=False)

    assert preserved.width / preserved.height == pytest.approx(
        original.width / original.height
    )
    assert free.width == pytest.approx(0.6)
    assert free.height == pytest.approx(0.34)


def test_editor_crop_changes_visible_bounds_without_rescaling_the_source() -> None:
    rect, crop = crop_geometry(
        NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.8),
        Crop(),
        "left",
        0.2,
        0.0,
    )

    assert rect.x == pytest.approx(0.3)
    assert rect.width == pytest.approx(0.6)
    assert crop.left == pytest.approx(0.25)


def test_editor_safe_margin_is_five_percent_of_the_physical_short_edge() -> None:
    snapped, guides = snap_move_rect(
        NormalizedRect(x=0.034, y=0.655, width=0.4, height=0.3),
        viewport_width=1000,
        viewport_height=600,
    )

    assert snapped.x == pytest.approx(0.03)
    assert snapped.y == pytest.approx(0.65)
    assert set(guides) == {("x", 0.03), ("y", 0.95)}
