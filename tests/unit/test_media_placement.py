from solin.core.i18n.media_placement import (
    MEDIA_PLACEMENT_TRANSLATION_SOURCES,
)
from solin.core.media.placement import (
    END_OF_LIST_INDEX,
    MEDIA_PLACEMENT_SOURCES,
    build_media_placement_options,
    resolve_media_placement,
)


def test_all_placement_labels_have_lupdate_visible_sources() -> None:
    assert MEDIA_PLACEMENT_TRANSLATION_SOURCES == MEDIA_PLACEMENT_SOURCES


def test_small_single_section_tree_skips_placement_prompt() -> None:
    tree = {
        "items": [{} for _ in range(13)],
        "sections": [{"id": "one", "name": "One"}],
    }

    assert build_media_placement_options(tree, translate=str) == []


def test_large_tree_offers_top_end_and_only_main_sections() -> None:
    tree = {
        "items": [{} for _ in range(14)],
        "sections": [
            {"id": "one", "name": "One", "color_hue": 20},
            {"id": "nested", "name": "Nested", "parent_id": "one"},
        ],
    }

    options = build_media_placement_options(tree, translate=str)

    assert [option["id"] for option in options] == [
        "top",
        "bottom",
        "section:one",
    ]


def test_two_main_sections_trigger_prompt_even_for_short_tree() -> None:
    tree = {
        "items": [],
        "sections": [
            {"id": "one", "name": "One"},
            {"id": "two", "name": "Two"},
        ],
    }

    assert len(build_media_placement_options(tree, translate=str)) == 4


def test_placement_resolution_uses_controller_list_contract() -> None:
    assert resolve_media_placement("top") == ("root", 0)
    assert resolve_media_placement("section:one") == ("section:one", 0)
    assert resolve_media_placement("bottom") == ("root", END_OF_LIST_INDEX)
    assert resolve_media_placement("invalid") == ("root", END_OF_LIST_INDEX)
