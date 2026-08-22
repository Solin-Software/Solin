from __future__ import annotations

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QPushButton, QWidget

from solin.widgets.common.flow_container import FlowContainer


def test_flow_container_reflows_resized_items_without_overlap() -> None:
    host = QWidget()
    host.resize(220, 120)
    flow = FlowContainer(horizontal_spacing=8, vertical_spacing=6, parent=host)
    flow.setGeometry(0, 0, 180, 100)
    first = QPushButton("First")
    second = QPushButton("Second")
    first.setFixedSize(80, 32)
    second.setFixedSize(80, 32)
    flow.add_widget(first)
    flow.add_widget(second)
    host.show()
    QCoreApplication.processEvents()

    flow.relayout()
    assert second.x() - first.geometry().right() - 1 == 8

    first.setFixedWidth(120)
    flow.relayout()

    assert not first.geometry().intersects(second.geometry())
    assert second.y() - first.geometry().bottom() - 1 == 6
    host.close()
    host.deleteLater()
    QCoreApplication.processEvents()


def test_flow_container_keeps_reused_items_visible() -> None:
    host = QWidget()
    flow = FlowContainer(parent=host)
    button = QPushButton("Scene")
    flow.add_widget(button)
    host.show()
    QCoreApplication.processEvents()
    assert button.isVisible()

    assert flow.clear_items(delete=False) == (button,)
    flow.add_widget(button)
    QCoreApplication.processEvents()

    assert button.isVisible()
    host.close()
    host.deleteLater()
    QCoreApplication.processEvents()
