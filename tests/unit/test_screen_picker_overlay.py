from PySide6.QtCore import QRect

from solin.widgets.screen_picker_overlay import _united_screen_geometry


def test_united_screen_geometry_covers_negative_and_positive_monitor_origins():
    geometry = _united_screen_geometry(
        [
            QRect(-1920, 0, 1920, 1080),
            QRect(0, 0, 2560, 1440),
            QRect(2560, -900, 1600, 900),
        ]
    )

    assert geometry == QRect(-1920, -900, 6080, 2340)


def test_united_screen_geometry_handles_no_screens():
    assert _united_screen_geometry([]).isNull()
