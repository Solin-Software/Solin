"""Qt events reported by embedded native content that receives its own input."""

from PySide6.QtCore import QEvent

NATIVE_POINTER_PRESS = QEvent.Type(QEvent.registerEventType())
