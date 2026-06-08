from pathlib import Path

from app.widgets.meetings.visuals import _SEC_ICONS


def test_meeting_section_icons_stay_embedded_in_python():
    assert set(_SEC_ICONS) == {"tgw", "ayfm", "lac"}
    assert all(svg.startswith("<svg") for svg, _size in _SEC_ICONS.values())


def test_meetings_qml_pointer_bridge_remains_wired():
    source = Path("app/widgets/meetings/widget.py").read_text(encoding="utf-8")
    detail_model_source = Path("app/widgets/meetings/detail_model.py").read_text(
        encoding="utf-8"
    )

    assert source.count(
        "controller.pointerEntered.connect(self.begin_qml_pointer_cursor)"
    ) == 2
    assert source.count(
        "controller.pointerExited.connect(self.end_qml_pointer_cursor)"
    ) == 2
    assert "begin_qml_pointer_cursor(self.qml_widget)" in source
    assert "end_qml_pointer_cursor(self.qml_widget)" in source
    assert "parent.begin_qml_pointer_cursor()" in detail_model_source
    assert "parent.end_qml_pointer_cursor()" in detail_model_source
