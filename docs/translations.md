# Translations

English source strings are embedded in the application code. Qt translation
catalogs and locale metadata live under:

```text
src/solin/resources/translations/
```

Each supported locale has a Qt `.ts` source catalog, a compiled `.qm` catalog,
and locale metadata under `locales/`.

## Validation

Run the locale contract check after changing translations or locale metadata:

```text
python scripts/check_locales.py
```

The normal test suite also validates packaged translation paths and the
supported-locale contracts.

## Translation editor

Start the optional editor with:

```text
python tools/translation_editor/main.py
```

The editor can extract source strings with Qt `lupdate`, edit `.ts` catalogs,
and compile `.qm` catalogs with `lrelease`.

Automatic completion uses the Google Gen AI SDK and is not required for normal
development. Install that optional integration with:

```text
python -m pip install -e ".[translation]"
```

Provide `GEMINI_API_KEY` through the process environment using the normal
mechanism for the platform or environment manager.

Never commit an API key or a local environment file. Review generated
translations before saving them; automated output is input to the translation
workflow, not an authoritative application translation.

## Changing source strings

When a user-visible source string changes:

1. run `lupdate` through the editor;
2. update every supported translation;
3. compile the `.qm` catalogs;
4. run `python scripts/check_locales.py`;
5. include source and compiled catalogs in the same pull request.

Reuse the existing application vocabulary instead of introducing near-duplicate
translations for the same concept.
