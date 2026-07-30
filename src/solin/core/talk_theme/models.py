from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any


SCHEMA_VERSION = 2
MAX_TEXT_LAYERS = 32
MAX_USER_PRESETS = 100
MAX_BACKGROUND_OVERLAY_OPACITY = 0.95
MAX_BACKGROUND_BLUR = 1.0
PROMPT_ON_UNSAVED_CHANGES = False

ALIGNMENTS = frozenset({"left", "center", "right"})
FONT_WEIGHTS = frozenset({"normal", "medium", "semibold", "bold"})
FILL_MODES = frozenset({"cover", "contain"})
BACKGROUND_KINDS = frozenset({"none", "builtin", "asset"})

_IDENTITY_NAMESPACE = uuid.UUID("db411d35-6ae1-4fb2-b042-f3818f130ac3")
_COLOR_PATTERN = re.compile(r"^#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
_LEGACY_LAYER_NAMES = {
    "label": "Public talk",
    "title": "Title",
    "speaker": "Speaker",
    "congregation": "Congregation",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_identity() -> str:
    """Return an opaque identity suitable for a user layer or preset."""

    return str(uuid.uuid4())


def stable_identity(seed: str) -> str:
    """Return a stable UUID for definitions that live in application code."""

    return str(uuid.uuid5(_IDENTITY_NAMESPACE, seed))


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string(value: object, default: str = "", *, limit: int = 500) -> str:
    if not isinstance(value, str):
        return default
    return value[:limit]


def _number(value: object, default: float, low: float, high: float) -> float:
    if not isinstance(value, (int, float, str)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(low, min(high, number))


def _choice(value: object, choices: frozenset[str], default: str) -> str:
    candidate = str(value) if value is not None else ""
    return candidate if candidate in choices else default


def _color(value: object, default: str) -> str:
    candidate = _string(value, default, limit=9)
    return candidate.upper() if _COLOR_PATTERN.fullmatch(candidate) else default


def _opaque_color(value: object, default: str) -> str:
    color = _color(value, default)
    return f"#{color[-6:]}"


def _identity(value: object, fallback_seed: str) -> str:
    candidate = _string(value, limit=80).strip()
    return candidate or stable_identity(fallback_seed)


def _unique_identity(candidate: str, *, seed: str, seen: set[str]) -> str:
    if candidate not in seen:
        return candidate
    suffix = 1
    while True:
        replacement = stable_identity(f"{seed}:{candidate}:{suffix}")
        if replacement not in seen:
            return replacement
        suffix += 1


@dataclass(frozen=True, slots=True)
class TextLayer:
    id: str
    name: str
    text: str
    x: float
    y: float
    width: float
    font_size: float
    template_key: str = ""
    font_family: str = "Arial"
    font_weight: str = "normal"
    color: str = "#FFFFFFFF"
    alignment: str = "left"
    visible: bool = True
    letter_spacing: float = 0.0
    line_height: float = 1.05
    snap_x: str = ""
    snap_y: str = ""

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "text": self.text,
            "template_key": self.template_key,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "font_size": self.font_size,
            "font_family": self.font_family,
            "font_weight": self.font_weight,
            "color": self.color,
            "alignment": self.alignment,
            "visible": self.visible,
            "letter_spacing": self.letter_spacing,
            "line_height": self.line_height,
            "snap_x": self.snap_x,
            "snap_y": self.snap_y,
        }

    @classmethod
    def from_record(
        cls,
        raw: object,
        *,
        fallback_seed: str = "layer",
        legacy_content: object = None,
    ) -> TextLayer:
        data = _mapping(raw)
        legacy_key = _string(data.get("content_key"), limit=40)
        template_key = _string(data.get("template_key"), legacy_key, limit=40)
        legacy_values = _mapping(legacy_content)
        fallback_name = _LEGACY_LAYER_NAMES.get(template_key, "Text")
        fallback_text = _string(legacy_values.get(template_key), limit=4000)
        if template_key == "label" and template_key not in legacy_values:
            fallback_text = "PUBLIC TALK"
        return cls(
            id=_identity(data.get("id"), fallback_seed),
            name=_string(data.get("name"), fallback_name, limit=120).strip() or fallback_name,
            text=_string(data.get("text"), fallback_text, limit=4000),
            template_key=template_key,
            x=_number(data.get("x"), 0.1, 0.0, 1.0),
            y=_number(data.get("y"), 0.1, 0.0, 1.0),
            width=_number(data.get("width"), 0.8, 0.08, 1.0),
            font_size=_number(data.get("font_size"), 0.08, 0.012, 0.30),
            font_family=_string(data.get("font_family"), "Arial", limit=100) or "Arial",
            font_weight=_choice(data.get("font_weight"), FONT_WEIGHTS, "normal"),
            color=_color(data.get("color"), "#FFFFFFFF"),
            alignment=_choice(data.get("alignment"), ALIGNMENTS, "left"),
            visible=bool(data.get("visible", True)),
            letter_spacing=_number(data.get("letter_spacing"), 0.0, -5.0, 30.0),
            line_height=_number(data.get("line_height"), 1.05, 0.75, 2.0),
            snap_x=_string(data.get("snap_x"), limit=80),
            snap_y=_string(data.get("snap_y"), limit=80),
        )


@dataclass(frozen=True, slots=True)
class Background:
    kind: str = "builtin"
    source: str = "talk_theme_botanical.svg"
    base_color: str = "#11182A"
    fill_mode: str = "cover"
    overlay_color: str = "#000000"
    overlay_opacity: float = 0.0
    blur: float = 0.0
    zoom: float = 1.0
    norm_x: float = 0.0
    norm_y: float = 0.0

    def to_record(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source": self.source,
            "base_color": self.base_color,
            "fill_mode": self.fill_mode,
            "overlay_color": self.overlay_color,
            "overlay_opacity": self.overlay_opacity,
            "blur": self.blur,
            "zoom": self.zoom,
            "norm_x": self.norm_x,
            "norm_y": self.norm_y,
        }

    @classmethod
    def from_record(cls, raw: object) -> Background:
        data = _mapping(raw)
        kind = _choice(data.get("kind"), BACKGROUND_KINDS, "builtin")
        source = _string(
            data.get("source"),
            "talk_theme_botanical.svg",
            limit=260,
        )
        if kind == "none":
            source = ""
        elif not source:
            kind = "none"
        return cls(
            kind=kind,
            source=source,
            base_color=_opaque_color(data.get("base_color"), "#11182A"),
            fill_mode=_choice(data.get("fill_mode"), FILL_MODES, "cover"),
            overlay_color=_color(data.get("overlay_color"), "#000000"),
            overlay_opacity=_number(
                data.get("overlay_opacity"),
                0.0,
                0.0,
                MAX_BACKGROUND_OVERLAY_OPACITY,
            ),
            blur=_number(data.get("blur"), 0.0, 0.0, MAX_BACKGROUND_BLUR),
            zoom=_number(data.get("zoom"), 1.0, 0.1, 10.0),
            norm_x=_number(data.get("norm_x"), 0.0, -1.0, 1.0),
            norm_y=_number(data.get("norm_y"), 0.0, -1.0, 1.0),
        )


def _layers_from_records(
    raw: object,
    *,
    seed: str,
    legacy_content: object = None,
) -> tuple[TextLayer, ...]:
    if not isinstance(raw, list):
        return ()
    layers: list[TextLayer] = []
    seen: set[str] = set()
    for index, record in enumerate(raw[:MAX_TEXT_LAYERS]):
        layer = TextLayer.from_record(
            record,
            fallback_seed=f"{seed}:layer:{index}",
            legacy_content=legacy_content,
        )
        unique_id = _unique_identity(layer.id, seed=f"{seed}:layer:{index}", seen=seen)
        if unique_id != layer.id:
            layer = replace(layer, id=unique_id)
        seen.add(layer.id)
        layers.append(layer)
    return tuple(layers)


def _merge_legacy_content(
    layers: tuple[TextLayer, ...],
    raw_content: object,
) -> tuple[TextLayer, ...]:
    content = _mapping(raw_content)
    merged: list[TextLayer] = []
    represented: set[str] = set()
    for layer in layers:
        key = layer.template_key
        represented.add(key)
        if key in content:
            layer = replace(layer, text=_string(content.get(key), limit=4000))
        merged.append(layer)

    for key in ("label", "title", "speaker", "congregation"):
        if key in represented or key not in content or len(merged) >= MAX_TEXT_LAYERS:
            continue
        text = _string(content.get(key), limit=4000)
        if not text and key != "label":
            continue
        merged.append(
            TextLayer.from_record(
                {
                    "id": stable_identity(f"legacy-draft:missing:{key}"),
                    "name": _LEGACY_LAYER_NAMES[key],
                    "text": text,
                    "template_key": key,
                },
                fallback_seed=f"legacy-draft:missing:{key}",
            )
        )
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class ThemeDocument:
    background: Background
    layers: tuple[TextLayer, ...]
    updated_at: str = field(default_factory=utc_now_iso)

    def to_record(self) -> dict[str, object]:
        return {
            "background": self.background.to_record(),
            "layers": [layer.to_record() for layer in self.layers],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(
        cls,
        raw: object,
        *,
        fallback: ThemeDocument | None = None,
        seed: str = "document",
    ) -> ThemeDocument:
        data = _mapping(raw)
        default_background = fallback.background if fallback is not None else Background()
        if isinstance(data.get("background"), dict):
            background = Background.from_record(data.get("background"))
        else:
            background = default_background
        if isinstance(data.get("layers"), list):
            layers = _layers_from_records(data.get("layers"), seed=seed)
        else:
            layers = fallback.layers if fallback is not None else ()
        return cls(
            background=background,
            layers=layers,
            updated_at=_string(data.get("updated_at"), utc_now_iso(), limit=64),
        )

    @classmethod
    def from_legacy_draft(
        cls,
        raw: object,
        *,
        fallback: ThemeDocument,
    ) -> ThemeDocument:
        data = _mapping(raw)
        appearance = _mapping(data.get("appearance"))
        content = _mapping(data.get("content"))
        layers_raw = appearance.get("layers")
        layers = (
            _layers_from_records(
                layers_raw,
                seed="legacy-draft",
                legacy_content=content,
            )
            if isinstance(layers_raw, list)
            else fallback.layers
        )
        layers = _merge_legacy_content(layers, content)
        background = (
            Background.from_record(appearance.get("background"))
            if isinstance(appearance.get("background"), dict)
            else fallback.background
        )
        return cls(
            background=background,
            layers=layers,
            updated_at=_string(data.get("updated_at"), utc_now_iso(), limit=64),
        )

    @classmethod
    def from_legacy_appearance(
        cls,
        raw: object,
        *,
        fallback: ThemeDocument,
        seed: str,
    ) -> ThemeDocument:
        data = _mapping(raw)
        layers_raw = data.get("layers")
        layers = (
            _layers_from_records(layers_raw, seed=seed)
            if isinstance(layers_raw, list)
            else fallback.layers
        )
        background = (
            Background.from_record(data.get("background"))
            if isinstance(data.get("background"), dict)
            else fallback.background
        )
        return cls(
            background=background,
            layers=layers,
        )


@dataclass(frozen=True, slots=True)
class TalkThemePreset:
    id: str
    name: str
    document: ThemeDocument
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "document": self.document.to_record(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(
        cls,
        raw: object,
        *,
        index: int = 0,
        fallback_document: ThemeDocument,
    ) -> TalkThemePreset:
        data = _mapping(raw)
        preset_id = _identity(data.get("id"), f"preset:{index}")
        name = _string(data.get("name"), f"Preset {index + 1}", limit=120).strip()
        return cls(
            id=preset_id,
            name=name or f"Preset {index + 1}",
            document=ThemeDocument.from_record(
                data.get("document"),
                fallback=fallback_document,
                seed=f"preset:{preset_id}",
            ),
            created_at=_string(data.get("created_at"), utc_now_iso(), limit=64),
            updated_at=_string(data.get("updated_at"), utc_now_iso(), limit=64),
        )

    @classmethod
    def from_legacy_record(
        cls,
        raw: object,
        *,
        index: int,
        fallback_document: ThemeDocument,
    ) -> TalkThemePreset:
        data = _mapping(raw)
        preset_id = _identity(data.get("id"), f"legacy-preset:{index}")
        name = _string(data.get("name"), f"Preset {index + 1}", limit=120).strip()
        return cls(
            id=preset_id,
            name=name or f"Preset {index + 1}",
            document=ThemeDocument.from_legacy_appearance(
                data.get("appearance"),
                fallback=fallback_document,
                seed=f"legacy-preset:{preset_id}",
            ),
            created_at=_string(data.get("created_at"), utc_now_iso(), limit=64),
            updated_at=_string(data.get("updated_at"), utc_now_iso(), limit=64),
        )


def _presets_from_records(
    raw: object,
    *,
    fallback_document: ThemeDocument,
    legacy: bool,
) -> tuple[TalkThemePreset, ...]:
    if not isinstance(raw, list):
        return ()
    presets: list[TalkThemePreset] = []
    seen: set[str] = set()
    for index, record in enumerate(raw[:MAX_USER_PRESETS]):
        preset = (
            TalkThemePreset.from_legacy_record(
                record,
                index=index,
                fallback_document=fallback_document,
            )
            if legacy
            else TalkThemePreset.from_record(
                record,
                index=index,
                fallback_document=fallback_document,
            )
        )
        if preset.id in seen:
            continue
        seen.add(preset.id)
        presets.append(preset)
    return tuple(presets)


@dataclass(frozen=True, slots=True)
class TalkThemeLibrary:
    """Editor state plus the user-owned data that may be persisted.

    ``document`` is intentionally omitted from :meth:`to_record`: working edits never
    become durable unless the bridge explicitly creates or updates a user preset.
    """

    document: ThemeDocument
    user_presets: tuple[TalkThemePreset, ...] = ()
    last_saved_preset_id: str = ""

    def to_record(self) -> dict[str, object]:
        valid_ids = {preset.id for preset in self.user_presets[:MAX_USER_PRESETS]}
        last_saved = self.last_saved_preset_id if self.last_saved_preset_id in valid_ids else ""
        return {
            "version": SCHEMA_VERSION,
            "user_presets": [preset.to_record() for preset in self.user_presets[:MAX_USER_PRESETS]],
            "last_saved_preset_id": last_saved,
        }

    @classmethod
    def from_record(
        cls,
        raw: object,
        *,
        fallback_document: ThemeDocument,
    ) -> TalkThemeLibrary:
        data = _mapping(raw)
        try:
            version = int(data.get("version", 1))
        except (TypeError, ValueError):
            version = 1

        if version >= SCHEMA_VERSION:
            presets = _presets_from_records(
                data.get("user_presets"),
                fallback_document=fallback_document,
                legacy=False,
            )
            valid_ids = {preset.id for preset in presets}
            requested_id = _string(data.get("last_saved_preset_id"), limit=80)
            last_saved_id = requested_id if requested_id in valid_ids else ""
            document = next(
                (preset.document for preset in presets if preset.id == last_saved_id),
                fallback_document,
            )
            return cls(
                document=document,
                user_presets=presets,
                last_saved_preset_id=last_saved_id,
            )

        document = ThemeDocument.from_legacy_draft(
            data.get("draft"),
            fallback=fallback_document,
        )
        presets = _presets_from_records(
            data.get("presets"),
            fallback_document=fallback_document,
            legacy=True,
        )
        return cls(document=document, user_presets=presets)
