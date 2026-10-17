"""Read expected catalog content independently of Qt's compiled representation."""

from functools import cache
import xml.etree.ElementTree as ET

from tests._paths import REPO_ROOT


@cache
def _catalog(locale: str) -> ET.Element:
    return ET.parse(
        REPO_ROOT / "src" / "solin" / "resources" / "translations" / f"solin_{locale}.ts"
    ).getroot()


def catalog_translation(
    locale: str,
    context: str,
    source: str,
    disambiguation: str | None = None,
    *,
    plural_form: int | None = None,
) -> str:
    """Require a finished message and return its current TS text or plural form."""
    matches = [
        message
        for node in _catalog(locale).findall("context")
        if node.findtext("name") == context
        for message in node.findall("message")
        if message.findtext("source") == source
        and (message.findtext("comment") or None) == disambiguation
        and message.find("translation") is not None
        and message.find("translation").get("type") not in {"obsolete", "vanished"}
    ]
    assert len(matches) == 1, (locale, context, source, disambiguation)
    translation = matches[0].find("translation")
    assert translation is not None and translation.get("type") != "unfinished", (locale, context, source)
    if plural_form is not None:
        forms = translation.findall("numerusform")
        assert 0 <= plural_form < len(forms), (locale, context, source, plural_form)
        translation = forms[plural_form]
    text = "".join(translation.itertext())
    assert text.strip(), (locale, context, source)
    return text
