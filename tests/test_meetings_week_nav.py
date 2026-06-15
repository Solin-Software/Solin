from pathlib import Path

from solin.widgets.meetings.visuals import MEETING_SECTION_ICONS


def test_meeting_section_icons_stay_embedded_in_python():
    assert set(MEETING_SECTION_ICONS) == {"tgw", "ayfm", "lac"}
    assert all(svg.startswith("<svg") for svg, _size in MEETING_SECTION_ICONS.values())


def test_meetings_qml_pointer_bridge_remains_wired():
    source = Path("src/solin/widgets/meetings/widget.py").read_text(encoding="utf-8")
    controller_source = Path(
        "src/solin/widgets/meetings/tree_controller.py"
    ).read_text(
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
    assert "self.pointerEntered.emit()" in controller_source
    assert "self.pointerExited.emit()" in controller_source
    assert not Path("src/solin/widgets/meetings/detail_model.py").exists()
