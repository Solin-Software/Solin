"""Rule model mapping projector content → virtual-camera composition.

The virtual camera *follows* the projector: for each meeting mode, the content
currently on the projector (its program ``current_key``) selects a
:class:`VcamComposition` — whether to mirror the projector and where to place the
camera. Curated defaults live here; the operator can override the live scene from
the floating menu, and (later) edit the tables per mode.

Leaf module: only enums/dataclasses/tables, no libobs — so both the vcam service
and its director can import it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MeetingMode(Enum):
    """Which follow-the-projector rule table is in effect."""

    REGULAR = "regular"
    #: Sign-language meeting — the camera (interpreter) stays visible even over
    #: media, because deaf viewers need the signer at all times.
    SIGN_LANGUAGE = "sign_language"


class CameraLayout(Enum):
    """Where the camera sits on the virtual-camera canvas."""

    OFF = "off"  # camera hidden
    PIP = "pip"  # small, corner, over the mirrored projector (corner: PipCorner)
    FULL = "full"  # fills the canvas (covers the mirror / idle logo)


class PipCorner(Enum):
    """Which corner the picture-in-picture camera occupies."""

    BOTTOM_RIGHT = "bottom_right"
    BOTTOM_LEFT = "bottom_left"
    TOP_RIGHT = "top_right"
    TOP_LEFT = "top_left"


@dataclass(frozen=True, slots=True)
class VcamComposition:
    """A vcam scene recipe: mirror the projector? + where is the camera?

    ``program_visible`` shows the projector's program (media / image / stream /
    projected camera) as the base layer; when False the branded idle logo shows
    through instead. ``camera`` overlays the camera on top per :class:`CameraLayout`.
    """

    program_visible: bool
    camera: CameraLayout

    def to_dict(self) -> dict:
        return {"program_visible": self.program_visible, "camera": self.camera.value}

    @classmethod
    def from_dict(cls, data: dict) -> "VcamComposition":
        return cls(bool(data["program_visible"]), CameraLayout(data["camera"]))


class ContentGroup(Enum):
    """A projector-content category the operator configures a vcam scene for.

    Groups the many raw projector keys into the handful of rows the Scenes panel
    presents, so the operator picks one composition per meaningful situation.
    """

    IDLE = "idle"  # yeartext, timer, black, custom idle background
    PICTURE = "picture"  # a still image
    VIDEO = "video"  # playing media
    PROJECTED_CAMERA = "camera"  # the projector itself is showing the camera
    STREAM = "stream"  # a browser tab or NDI / OBS program stream


# ── projector content keys (mirror obs_program / program_driver) ──────────────
# The program's current_key values; kept as literals here (not imported) to keep
# this a leaf module — covered by a drift test.
_IDLE = "idle"
_IMAGE = "image"
_TIMER = "timer"
_BROWSER = "browser"
_CAMERA = "camera"
_NDI = "ndi"
_IDLE_VIDEO = "idle_video"
_IDLE_IMAGE = "idle_image"
_MEDIA = "media"
_BLACK = "__black__"

_KEY_TO_GROUP: dict[str, ContentGroup] = {
    _IDLE: ContentGroup.IDLE,
    _TIMER: ContentGroup.IDLE,
    _BLACK: ContentGroup.IDLE,
    _IDLE_VIDEO: ContentGroup.IDLE,
    _IDLE_IMAGE: ContentGroup.IDLE,
    _IMAGE: ContentGroup.PICTURE,
    _MEDIA: ContentGroup.VIDEO,
    _CAMERA: ContentGroup.PROJECTED_CAMERA,
    _BROWSER: ContentGroup.STREAM,
    _NDI: ContentGroup.STREAM,
}


def content_group_for(projector_key: str | None) -> ContentGroup:
    """Map a projector ``current_key`` to the content group the rules switch on.

    Unknown / ``None`` keys fall back to STREAM (mirror the projector).
    """
    return _KEY_TO_GROUP.get(projector_key or _BLACK, ContentGroup.STREAM)


# ── the curated compositions the Scenes panel offers per row ──────────────────
_CAMERA_FULL = VcamComposition(program_visible=False, camera=CameraLayout.FULL)
_MEDIA_FULL = VcamComposition(program_visible=True, camera=CameraLayout.OFF)
_MEDIA_WITH_CAMERA = VcamComposition(program_visible=True, camera=CameraLayout.PIP)
_LOGO_ONLY = VcamComposition(program_visible=False, camera=CameraLayout.OFF)

#: Ordered presets for the panel dropdowns — (stable id, composition). The id is
#: for UI/i18n labelling; compositions are still persisted structurally.
SCENE_PRESETS: tuple[tuple[str, VcamComposition], ...] = (
    ("camera_full", _CAMERA_FULL),
    ("media_full", _MEDIA_FULL),
    ("media_with_camera", _MEDIA_WITH_CAMERA),
    ("logo_only", _LOGO_ONLY),
)
_PRESET_BY_ID: dict[str, VcamComposition] = dict(SCENE_PRESETS)


def preset_for_id(preset_id: str) -> VcamComposition:
    """The composition for a preset id (raises KeyError for an unknown id)."""
    return _PRESET_BY_ID[preset_id]


def preset_id_for(comp: VcamComposition) -> str | None:
    """The preset id whose composition equals ``comp`` (None if it's not a preset)."""
    for pid, preset in SCENE_PRESETS:
        if preset == comp:
            return pid
    return None

# ── curated per-mode defaults, by content group ───────────────────────────────
_GROUP_DEFAULTS: dict[MeetingMode, dict[ContentGroup, VcamComposition]] = {
    MeetingMode.REGULAR: {
        ContentGroup.IDLE: _CAMERA_FULL,
        ContentGroup.PICTURE: _MEDIA_WITH_CAMERA,
        ContentGroup.VIDEO: _MEDIA_FULL,
        ContentGroup.PROJECTED_CAMERA: _MEDIA_FULL,
        ContentGroup.STREAM: _MEDIA_FULL,
    },
    MeetingMode.SIGN_LANGUAGE: {
        ContentGroup.IDLE: _CAMERA_FULL,
        ContentGroup.PICTURE: _MEDIA_WITH_CAMERA,
        ContentGroup.VIDEO: _MEDIA_WITH_CAMERA,  # media + interpreter PiP
        ContentGroup.PROJECTED_CAMERA: _MEDIA_FULL,
        ContentGroup.STREAM: _MEDIA_WITH_CAMERA,
    },
}


def default_composition(mode: MeetingMode, group: ContentGroup) -> VcamComposition:
    """The curated (un-overridden) composition for a mode + content group."""
    return _GROUP_DEFAULTS[mode][group]


def composition_for(mode: MeetingMode, projector_key: str | None) -> VcamComposition:
    """The *default* vcam composition for ``projector_key`` under ``mode``.

    Operator overrides are layered on by :class:`VcamSceneConfig`; this is the
    curated baseline (idle-like → camera full; per-mode rules otherwise).
    """
    return _GROUP_DEFAULTS[mode][content_group_for(projector_key)]


@dataclass
class VcamSceneConfig:
    """Operator-editable virtual-camera scene configuration (persistable).

    The active meeting mode, per-(mode, group) composition OVERRIDES layered on
    the curated defaults, and the PiP corner/size. The :class:`VcamDirector`
    resolves the live composition through :meth:`composition_for`.
    """

    mode: MeetingMode = MeetingMode.REGULAR
    overrides: dict[tuple[MeetingMode, ContentGroup], VcamComposition] = field(
        default_factory=dict
    )
    pip_corner: PipCorner = PipCorner.BOTTOM_RIGHT
    pip_fraction: float = 0.25

    def rule(self, mode: MeetingMode, group: ContentGroup) -> VcamComposition:
        """The effective composition for a mode + group (override or default)."""
        return self.overrides.get((mode, group)) or _GROUP_DEFAULTS[mode][group]

    def set_rule(
        self, mode: MeetingMode, group: ContentGroup, comp: VcamComposition
    ) -> None:
        """Override (or, if it equals the default, clear the override for) a rule."""
        if comp == _GROUP_DEFAULTS[mode][group]:
            self.overrides.pop((mode, group), None)
        else:
            self.overrides[(mode, group)] = comp

    def composition_for(self, projector_key: str | None) -> VcamComposition:
        """The effective composition for live projector content under the mode."""
        return self.rule(self.mode, content_group_for(projector_key))

    def copy(self) -> "VcamSceneConfig":
        """An independent copy (so owners don't alias each other's mutations)."""
        return VcamSceneConfig(
            mode=self.mode,
            overrides=dict(self.overrides),  # keys/values are immutable
            pip_corner=self.pip_corner,
            pip_fraction=self.pip_fraction,
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "pip_corner": self.pip_corner.value,
            "pip_fraction": self.pip_fraction,
            "overrides": [
                {"mode": m.value, "group": g.value, "composition": c.to_dict()}
                for (m, g), c in self.overrides.items()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VcamSceneConfig":
        overrides: dict[tuple[MeetingMode, ContentGroup], VcamComposition] = {}
        for entry in data.get("overrides", []):
            try:
                key = (MeetingMode(entry["mode"]), ContentGroup(entry["group"]))
                overrides[key] = VcamComposition.from_dict(entry["composition"])
            except (KeyError, ValueError, TypeError):
                continue  # skip stale/corrupt entries
        try:
            mode = MeetingMode(data.get("mode", MeetingMode.REGULAR.value))
        except ValueError:
            mode = MeetingMode.REGULAR
        try:
            corner = PipCorner(data.get("pip_corner", PipCorner.BOTTOM_RIGHT.value))
        except ValueError:
            corner = PipCorner.BOTTOM_RIGHT
        try:
            fraction = max(0.1, min(0.5, float(data.get("pip_fraction", 0.25))))
        except (TypeError, ValueError):
            fraction = 0.25
        return cls(mode=mode, overrides=overrides, pip_corner=corner, pip_fraction=fraction)


__all__ = [
    "SCENE_PRESETS",
    "CameraLayout",
    "ContentGroup",
    "MeetingMode",
    "PipCorner",
    "VcamComposition",
    "VcamSceneConfig",
    "composition_for",
    "content_group_for",
    "default_composition",
    "preset_for_id",
    "preset_id_for",
]
