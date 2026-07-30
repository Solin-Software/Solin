from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from solin.core.talk_theme.models import (
    MAX_BACKGROUND_BLUR,
    MAX_BACKGROUND_OVERLAY_OPACITY,
    MAX_TEXT_LAYERS,
    MAX_USER_PRESETS,
    PROMPT_ON_UNSAVED_CHANGES,
    SCHEMA_VERSION,
    Background,
    TalkThemeLibrary,
    TalkThemePreset,
    ThemeDocument,
)
from solin.core.talk_theme.presets import builtin_presets, default_document


def test_builtin_presets_are_complete_immutable_documents() -> None:
    presets = builtin_presets()

    assert [preset.id for preset in presets] == [
        "botanical",
        "classic-blue",
        "soft-photo",
    ]
    assert [[layer.template_key for layer in preset.document.layers] for preset in presets] == [
        ["label", "title"],
        ["title", "speaker", "congregation"],
        ["title", "speaker", "congregation"],
    ]
    assert presets[0].document.layers[0].text == "PUBLIC TALK"
    assert all(layer.text for preset in presets for layer in preset.document.layers)
    assert presets[0].document.layers[1].text == "Imitate Jehovah's mercy"
    assert presets[1].document.layers[1].text == "Name"
    assert presets[1].document.layers[2].text == "Congregation"
    for preset in presets:
        for layer in preset.document.layers:
            UUID(layer.id)

    another = builtin_presets()
    assert another == presets
    assert another is not presets
    assert another[0].document is not presets[0].document


def test_botanical_preset_matches_the_original_reference_composition() -> None:
    botanical = builtin_presets()[0]
    label, title = botanical.document.layers

    assert botanical.document.background.source == "talk_theme_botanical.svg"
    assert (title.x, title.y, title.width, title.font_size) == (
        0.155,
        0.30,
        0.72,
        0.13333333333333333,
    )
    assert (title.font_family, title.font_weight, title.color, title.line_height) == (
        "Arial",
        "bold",
        "#FF3A4E42",
        1.0,
    )
    assert (label.x, label.y, label.width, label.font_size) == (
        0.155,
        0.645,
        0.72,
        0.05,
    )
    assert (label.color, label.letter_spacing, label.line_height) == (
        "#FF5A7A62",
        2.5,
        1.0,
    )

    asset = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "solin"
        / "resources"
        / "assets"
        / "talk_theme_botanical.svg"
    )
    fingerprint = hashlib.sha256(asset.read_text(encoding="utf-8").encode()).hexdigest()
    assert fingerprint == "9acb5bd32be4810d2a3e1573403830b1500f962350209730973aa3abe51dd955"


def test_classic_blue_preset_matches_the_original_reference_composition() -> None:
    classic_blue = builtin_presets()[1]
    title, speaker, congregation = classic_blue.document.layers

    assert classic_blue.document.background.source == "talk_theme_classic_blue.png"
    assert classic_blue.document.background.base_color == "#000000"
    assert (title.x, title.y, title.width, title.font_size) == (
        0.1,
        0.195,
        0.8,
        150 / 1080,
    )
    assert (title.font_family, title.font_weight, title.color, title.line_height) == (
        "Lato",
        "normal",
        "#FFFFFFFF",
        1.2,
    )
    assert (speaker.x, speaker.y, speaker.width, speaker.font_size) == (
        184.80676 / 1920,
        (899.1012 - 80) / 1080,
        0.8,
        80 / 1080,
    )
    assert (speaker.font_family, speaker.font_weight, speaker.line_height) == (
        "Lato",
        "normal",
        1.0,
    )
    assert (
        congregation.x,
        congregation.y,
        congregation.width,
        congregation.font_size,
    ) == (
        184.80676 / 1920,
        0.854,
        0.8,
        52 / 1080,
    )
    assert (
        congregation.font_family,
        congregation.font_weight,
        congregation.color,
        congregation.line_height,
    ) == ("Lato", "normal", "#FFFFFFFF", 1.0)

    asset = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "solin"
        / "resources"
        / "assets"
        / "talk_theme_classic_blue.png"
    )
    assert asset.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == (
        "90e4df423bf16447bfefe00e82d59a1ae91ef0f933af59fd8431b4b6cca16554"
    )


def test_soft_photo_preset_prioritizes_projection_readability() -> None:
    soft_photo = builtin_presets()[2]
    title, speaker, congregation = soft_photo.document.layers
    background = soft_photo.document.background

    assert background.source == "talk_theme_soft_photo.jpg"
    assert (
        background.base_color,
        background.overlay_color,
        background.overlay_opacity,
        background.blur,
    ) == (
        "#0B1733",
        "#0B1733",
        0.54,
        0.0,
    )
    assert (title.x, title.y, title.width, title.font_size) == (
        0.10,
        0.245,
        0.76,
        132 / 1080,
    )
    assert (
        title.font_family,
        title.font_weight,
        title.color,
        title.alignment,
        title.line_height,
    ) == ("Lato", "bold", "#D2FFE6C9", "left", 1.03)
    assert (speaker.x, speaker.y, speaker.width, speaker.font_size) == (
        0.10,
        0.735,
        0.65,
        68 / 1080,
    )
    assert (speaker.font_family, speaker.font_weight, speaker.color) == (
        "Lato",
        "semibold",
        "#D2FFE6C9",
    )
    assert (
        congregation.x,
        congregation.y,
        congregation.width,
        congregation.font_size,
    ) == (0.10, 0.820, 0.65, 48 / 1080)
    assert (congregation.font_family, congregation.color) == ("Lato", "#D2FFE6C9")


def test_library_round_trip_persists_only_user_presets_and_last_saved_id() -> None:
    document = replace(
        default_document(),
        background=Background(
            source="custom.jpg",
            kind="asset",
            base_color="#29435C",
        ),
    )
    preset = TalkThemePreset(id="mine", name="My preset", document=document)
    library = TalkThemeLibrary(
        document=document,
        user_presets=(preset,),
        last_saved_preset_id="mine",
    )

    record = library.to_record()
    restored = TalkThemeLibrary.from_record(
        record,
        fallback_document=default_document(),
    )

    assert record["version"] == SCHEMA_VERSION
    assert "document" not in record
    assert "draft" not in record
    assert "presets" not in record
    assert restored == library
    assert all("height" not in layer.to_record() for layer in document.layers)


def test_unsaved_document_is_not_part_of_the_persisted_library() -> None:
    unsaved = replace(default_document(), background=Background(source="unsaved.jpg"))
    record = TalkThemeLibrary(document=unsaved).to_record()

    restored = TalkThemeLibrary.from_record(
        record,
        fallback_document=default_document(),
    )

    assert restored.document == default_document()
    assert restored.user_presets == ()
    assert restored.last_saved_preset_id == ""


def test_invalid_values_are_clamped_and_identities_are_deduplicated() -> None:
    raw_layers = [
        {
            "id": "same",
            "name": "",
            "text": "First",
            "x": 4,
            "font_size": -2,
            "color": "invalid",
        },
        {
            "id": "same",
            "text": "Second",
            "width": 0,
            "max_lines": 1,
        },
    ]
    raw_layers.extend({"id": f"layer-{index}"} for index in range(40))
    raw_presets = [
        {
            "id": f"preset-{index}",
            "document": {"layers": raw_layers},
        }
        for index in range(MAX_USER_PRESETS + 10)
    ]
    raw_presets.insert(1, raw_presets[0])

    restored = TalkThemeLibrary.from_record(
        {
            "version": SCHEMA_VERSION,
            "user_presets": raw_presets,
            "last_saved_preset_id": "missing",
        },
        fallback_document=default_document(),
    )

    assert len(restored.user_presets) == MAX_USER_PRESETS - 1
    assert restored.last_saved_preset_id == ""
    first_document = restored.user_presets[0].document
    assert len(first_document.layers) == MAX_TEXT_LAYERS
    assert len({layer.id for layer in first_document.layers}) == MAX_TEXT_LAYERS
    first, second = first_document.layers[:2]
    assert first.name == "Text"
    assert first.x == 1.0
    assert first.font_size == 0.012
    assert first.color == "#FFFFFFFF"
    assert second.id != "same"
    assert second.width == 0.08
    assert "max_lines" not in second.to_record()

    background = Background.from_record({"overlay_opacity": 2})
    assert background.overlay_opacity == MAX_BACKGROUND_OVERLAY_OPACITY
    blurred = Background.from_record({"blur": 2})
    assert blurred.blur == MAX_BACKGROUND_BLUR
    assert Background.from_record(blurred.to_record()) == blurred
    assert Background.from_record({}).blur == 0.0


def test_background_supports_a_canonical_solid_color_without_an_image() -> None:
    solid = Background.from_record(
        {
            "kind": "asset",
            "source": "",
            "base_color": "#a24b62",
            "overlay_opacity": 0.7,
        }
    )

    assert solid.kind == "none"
    assert solid.source == ""
    assert solid.base_color == "#A24B62"
    assert Background.from_record(solid.to_record()) == solid

    explicitly_empty = Background.from_record(
        {
            "kind": "none",
            "source": "ignored.jpg",
            "base_color": "invalid",
        }
    )
    assert explicitly_empty.kind == "none"
    assert explicitly_empty.source == ""
    assert explicitly_empty.base_color == "#11182A"
    assert Background.from_record({"base_color": "#80ABCDEF"}).base_color == "#ABCDEF"


def test_empty_document_is_valid() -> None:
    restored = ThemeDocument.from_record(
        {"layers": []},
        fallback=default_document(),
    )

    assert restored.layers == ()


def test_v1_library_migration_preserves_active_content_and_layout() -> None:
    legacy = {
        "version": 1,
        "draft": {
            "content": {
                "label": "ASSEMBLY TALK",
                "title": "A meaningful title",
                "speaker": "Daniel Almeida",
                "congregation": "Central",
            },
            "appearance": {
                "background": {"kind": "asset", "source": "legacy.jpg"},
                "layers": [
                    {
                        "id": "old-label",
                        "content_key": "label",
                        "x": 0.15,
                        "snap_x": "safe-left",
                    },
                    {
                        "id": "old-title",
                        "content_key": "title",
                        "width": 0.61,
                    },
                    {"id": "old-speaker", "content_key": "speaker"},
                    {"id": "old-congregation", "content_key": "congregation"},
                ],
            },
            "follow_output_aspect": False,
            "selected_layer_id": "old-title",
        },
        "presets": [
            {
                "id": "legacy-style",
                "name": "Legacy style",
                "appearance": {
                    "background": {"source": "style.svg"},
                    "layers": [
                        {"id": "label", "content_key": "label", "x": 0.2},
                        {"id": "title", "content_key": "title", "width": 0.7},
                        {"id": "speaker", "content_key": "speaker"},
                    ],
                },
            }
        ],
    }

    migrated = TalkThemeLibrary.from_record(
        legacy,
        fallback_document=default_document(),
    )

    assert migrated.document.background.source == "legacy.jpg"
    assert "follow_output_aspect" not in migrated.document.to_record()
    assert [layer.text for layer in migrated.document.layers] == [
        "ASSEMBLY TALK",
        "A meaningful title",
        "Daniel Almeida",
        "Central",
    ]
    assert migrated.document.layers[0].x == 0.15
    assert migrated.document.layers[1].width == 0.61
    assert migrated.user_presets[0].document.background.source == "style.svg"
    assert [layer.text for layer in migrated.user_presets[0].document.layers] == [
        "PUBLIC TALK",
        "",
        "",
    ]
    assert migrated.last_saved_preset_id == ""
    persisted = migrated.to_record()
    assert persisted["version"] == SCHEMA_VERSION
    assert "draft" not in persisted
    assert "selected_layer_id" not in str(persisted)
    assert "content_key" not in str(persisted)


def test_v1_migration_preserves_content_when_appearance_is_missing() -> None:
    migrated = TalkThemeLibrary.from_record(
        {
            "draft": {
                "content": {
                    "title": "Recovered title",
                    "speaker": "Recovered speaker",
                    "congregation": "Recovered congregation",
                }
            }
        },
        fallback_document=default_document(),
    )

    content = {layer.template_key: layer.text for layer in migrated.document.layers}
    assert content == {
        "label": "PUBLIC TALK",
        "title": "Recovered title",
        "speaker": "Recovered speaker",
        "congregation": "Recovered congregation",
    }


def test_prompt_for_unsaved_changes_is_disabled_by_default() -> None:
    assert PROMPT_ON_UNSAVED_CHANGES is False
