"""CalVer identity and deterministic native package version projections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from packaging.version import InvalidVersion, Version

_PATTERN = re.compile(r"^(\d{1,2})\.(\d+)\.(\d+)(?:b[1-9]\d*)?$")
_TAG_PATTERN = re.compile(r"^(\d{2}\.\d+\.\d+)(?:-beta\.([1-9]\d*))?$")
FINAL_BUILD = 65535


@total_ordering
@dataclass(frozen=True, slots=True)
class ReleaseVersion:
    value: Version

    @classmethod
    def parse(cls, raw: object) -> ReleaseVersion | None:
        if not isinstance(raw, str) or not _PATTERN.fullmatch(raw):
            return None
        try:
            version = Version(raw)
        except InvalidVersion:
            return None
        if any(part > 65535 for part in version.release):
            return None
        if version.pre is not None and not 1 <= version.pre[1] < FINAL_BUILD:
            return None
        return cls(version)

    @classmethod
    def from_tag(cls, tag: str) -> ReleaseVersion | None:
        match = _TAG_PATTERN.fullmatch(tag)
        if match is None:
            return None
        result = cls.parse(match[1] + (f"b{match[2]}" if match[2] else ""))
        return result if result and result.tag == tag else None

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReleaseVersion):
            return NotImplemented
        return self.value < other.value

    @property
    def canonical(self) -> str:
        return ".".join(map(str, self.value.release)) + (
            f"b{self.value.pre[1]}" if self.is_beta else ""
        )

    def __str__(self) -> str:
        return self.canonical

    @property
    def is_beta(self) -> bool:
        return self.value.pre is not None

    @property
    def channel(self) -> str:
        return "beta" if self.is_beta else "stable"

    @property
    def display_version(self) -> str:
        return self.canonical.replace("b", "-beta.")

    @property
    def tag(self) -> str:
        return self.display_version

    @property
    def base(self) -> tuple[int, int, int]:
        return self.value.release[:3]

    def _require_distributable(self) -> None:
        if self.base[0] < 10:
            raise ValueError("New releases require canonical YY.RELEASE.PATCH[bN]")

    @property
    def build(self) -> int:
        self._require_distributable()
        return self.value.pre[1] if self.value.pre else FINAL_BUILD

    @property
    def windows_version(self) -> str:
        return ".".join(map(str, (*self.base, self.build)))

    @property
    def macos_version(self) -> str:
        self._require_distributable()
        return ".".join(map(str, self.base))

    @property
    def macos_build(self) -> str:
        year, release, patch = self.base
        return f"{year}.{release}.{patch * 65536 + self.build}"

    @property
    def notification_version(self) -> str:
        """Preserve the numerical version contract of the notification API."""
        notification_build = self.value.pre[1] if self.is_beta else 0
        return ".".join(map(str, (*self.base, notification_build)))
