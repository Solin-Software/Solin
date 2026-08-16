"""Pure camera option model and persisted-backend compatibility policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CameraBackend(str, Enum):
    QT = "qt"


_BACKEND_ALIASES: dict[str, str] = {
    "cv2": "qt",
    "cv2_dshow": "qt",
    "dshow": "qt",
}


@dataclass(frozen=True)
class CameraOption:
    name: str
    label: str
    backend: CameraBackend
    cv_index: int = 0
    device_path: str = field(default="", compare=False, hash=False)

    @property
    def key(self) -> str:
        return f"{self.backend.value}:{self.cv_index}"

    @property
    def is_virtual(self) -> bool:
        text = self.name.casefold()
        return "virtual" in text or "obs" in text


def normalize_camera_backend(backend: str) -> str:
    value = (backend or "").strip()
    return _BACKEND_ALIASES.get(value, value)
