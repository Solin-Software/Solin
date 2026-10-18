import pytest
from PySide6.QtCore import QPoint, QRect, QSize

from solin.ui.dialogs.positioning import centered_dialog_position


@pytest.mark.parametrize(
    ("anchor", "size", "available", "expected"),
    [
        ((100, 100, 1000, 700), (470, 500), (0, 0, 1920, 1080), (365, 200)),
        ((3000, 800, 400, 300), (470, 500), (1920, 0, 1280, 1024), (2730, 524)),
        ((-1850, 100, 1000, 700), (470, 500), (-1920, -200, 1920, 1080), (-1585, 200)),
        ((100, -1000, 1000, 700), (470, 500), (0, -1080, 1920, 1080), (365, -900)),
        ((-200, 100, 400, 500), (470, 500), (0, 0, 1280, 1024), (0, 100)),
        ((300, -200, 600, 400), (470, 500), (0, 0, 1280, 1024), (365, 0)),
        ((100, 100, 600, 400), (1500, 1200), (0, 0, 1280, 1024), (0, 0)),
    ],
    ids=["centered", "right-bottom", "left-monitor", "upper-monitor", "left", "top", "oversized"],
)
def test_dialog_frame_is_centered_and_constrained_to_its_screen(anchor, size, available, expected):
    assert centered_dialog_position(QRect(*anchor), QSize(*size), QRect(*available)) == QPoint(
        *expected
    )
