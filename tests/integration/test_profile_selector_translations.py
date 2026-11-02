"""Profile selection and transition messages survive Qt catalog compilation."""

from pathlib import Path

import pytest
from PySide6.QtCore import QTranslator

from tests._paths import REPO_ROOT
from tests._translation_catalog import catalog_translation


_LOCALES = sorted(
    path.stem
    for path in (REPO_ROOT / "src" / "solin" / "resources" / "translations" / "locales").glob(
        "*.json"
    )
)


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize(
    ("context", "source"),
    [
        ("ProfileSelector", "Sign out of profile"),
        ("ProfileSelector", "Current profile"),
        ("MainWindow", "Could not switch profiles. Please try again."),
    ],
)
def test_profile_messages_resolve_from_finished_catalogs(
    compiled_translation_catalogs: Path,
    locale: str,
    context: str,
    source: str,
) -> None:
    expected = catalog_translation(locale, context, source)
    translator = QTranslator()
    assert translator.load(str(compiled_translation_catalogs / f"solin_{locale}.qm"))
    assert translator.translate(context, source) == expected
