from pathlib import Path


def test_screen_picker_uses_primary_screen_and_absolute_position_signal():
    source = Path("src/solin/widgets/screen_picker_overlay.py").read_text(
        encoding="utf-8"
    )

    assert "position_picked = Signal(int, int)" in source
    assert "QGuiApplication.primaryScreen()" in source
    assert "showFullScreen()" in source
    assert "find_zoom_share_dialog_bounds_at_point" not in source
    assert "target_picked = Signal(float, float)" not in source
