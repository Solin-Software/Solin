from __future__ import annotations

from .models import (
    Background,
    TalkThemePreset,
    TextLayer,
    ThemeDocument,
    stable_identity,
)

_BUILTIN_TIMESTAMP = "1970-01-01T00:00:00+00:00"
DEFAULT_TALK_TITLE = "Imitate Jehovah's mercy"
DEFAULT_SPEAKER_TEXT = "Name"
DEFAULT_CONGREGATION_TEXT = "Congregation"


def _layer(
    preset_id: str,
    template_key: str,
    name: str,
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    font_size: float,
    family: str,
    weight: str = "normal",
    color: str = "#FFFFFFFF",
    alignment: str = "left",
    letter_spacing: float = 0.0,
    line_height: float = 1.05,
    snap_x: str = "",
    snap_y: str = "",
) -> TextLayer:
    return TextLayer(
        id=stable_identity(f"builtin:{preset_id}:{template_key}"),
        name=name,
        text=text,
        template_key=template_key,
        x=x,
        y=y,
        width=width,
        font_size=font_size,
        font_family=family,
        font_weight=weight,
        color=color,
        alignment=alignment,
        letter_spacing=letter_spacing,
        line_height=line_height,
        snap_x=snap_x,
        snap_y=snap_y,
    )


def _botanical() -> TalkThemePreset:
    preset_id = "botanical"
    document = ThemeDocument(
        background=Background(source="talk_theme_botanical.svg"),
        layers=(
            _layer(
                preset_id,
                "label",
                "Public talk",
                "PUBLIC TALK",
                x=0.155,
                y=0.645,
                width=0.72,
                font_size=0.05,
                family="Arial",
                color="#FF5A7A62",
                letter_spacing=2.5,
                line_height=1.0,
            ),
            _layer(
                preset_id,
                "title",
                "Title",
                DEFAULT_TALK_TITLE,
                x=0.155,
                y=0.30,
                width=0.72,
                font_size=0.13333333333333333,
                family="Arial",
                weight="bold",
                color="#FF3A4E42",
                line_height=1.0,
            ),
        ),
        updated_at=_BUILTIN_TIMESTAMP,
    )
    return TalkThemePreset(
        id=preset_id,
        name="Botanical",
        document=document,
        created_at=_BUILTIN_TIMESTAMP,
        updated_at=_BUILTIN_TIMESTAMP,
    )


def _classic_blue() -> TalkThemePreset:
    preset_id = "classic-blue"
    document = ThemeDocument(
        background=Background(
            source="talk_theme_classic_blue.png",
            base_color="#000000",
        ),
        layers=(
            _layer(
                preset_id,
                "title",
                "Title",
                DEFAULT_TALK_TITLE,
                x=0.1,
                y=0.195,
                width=0.8,
                font_size=150 / 1080,
                family="Lato",
                line_height=1.2,
            ),
            _layer(
                preset_id,
                "speaker",
                "Speaker",
                DEFAULT_SPEAKER_TEXT,
                x=184.80676 / 1920,
                y=(899.1012 - 80) / 1080,
                width=0.8,
                font_size=80 / 1080,
                family="Lato",
                line_height=1.0,
            ),
            _layer(
                preset_id,
                "congregation",
                "Congregation",
                DEFAULT_CONGREGATION_TEXT,
                x=184.80676 / 1920,
                y=0.854,
                width=0.8,
                font_size=52 / 1080,
                family="Lato",
                line_height=1.0,
            ),
        ),
        updated_at=_BUILTIN_TIMESTAMP,
    )
    return TalkThemePreset(
        id=preset_id,
        name="Classic blue",
        document=document,
        created_at=_BUILTIN_TIMESTAMP,
        updated_at=_BUILTIN_TIMESTAMP,
    )


def _soft_photo() -> TalkThemePreset:
    preset_id = "soft-photo"
    document = ThemeDocument(
        background=Background(
            source="talk_theme_soft_photo.jpg",
            base_color="#0B1733",
            overlay_color="#0B1733",
            overlay_opacity=0.54,
        ),
        layers=(
            _layer(
                preset_id,
                "title",
                "Title",
                DEFAULT_TALK_TITLE,
                x=0.10,
                y=0.245,
                width=0.76,
                font_size=132 / 1080,
                family="Lato",
                weight="bold",
                color="#D2FFE6C9",
                line_height=1.03,
            ),
            _layer(
                preset_id,
                "speaker",
                "Speaker",
                DEFAULT_SPEAKER_TEXT,
                x=0.10,
                y=0.735,
                width=0.65,
                font_size=68 / 1080,
                family="Lato",
                weight="semibold",
                color="#D2FFE6C9",
                line_height=1.0,
            ),
            _layer(
                preset_id,
                "congregation",
                "Congregation",
                DEFAULT_CONGREGATION_TEXT,
                x=0.10,
                y=0.820,
                width=0.65,
                font_size=48 / 1080,
                family="Lato",
                color="#D2FFE6C9",
                line_height=1.0,
            ),
        ),
        updated_at=_BUILTIN_TIMESTAMP,
    )
    return TalkThemePreset(
        id=preset_id,
        name="Soft photographic",
        document=document,
        created_at=_BUILTIN_TIMESTAMP,
        updated_at=_BUILTIN_TIMESTAMP,
    )


def builtin_presets() -> tuple[TalkThemePreset, ...]:
    return (_botanical(), _classic_blue(), _soft_photo())


def default_document() -> ThemeDocument:
    return _botanical().document
