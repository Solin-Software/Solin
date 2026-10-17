# Translations

English source strings are embedded in the application code. Qt translation
catalogs and locale metadata live under:

```text
src/solin/resources/translations/
```

Each supported locale has a versioned Qt `.ts` source catalog and locale
metadata under `locales/`. Compiled `.qm` catalogs are generated artifacts and
must not be committed. Qt loads `.qm` binaries, not `.ts` sources.

## Development and builds

Starting with `python main.py` or an editable installation's `solin` command
automatically prepares catalogs in `build/translations/`. Preparation uses the
active Python environment's PySide6 `lrelease`, rebuilding missing or changed
catalogs based on source content, compiler identity, and options. A compilation
failure stops startup with a diagnostic. Restart after editing translations;
the application does not reload them while running.

To prepare catalogs explicitly:

```text
python scripts/compile_translations.py --output-dir build/translations
```

Standalone build recipes compile catalogs into their own build directories
before Nuitka packages them. Wheel builds preserve their compilation cache in
`build/translations/wheel/` and copy only `.qm` files into the wheel's resource
tree, including isolated builds from an sdist. Build isolation installs the
pinned Qt compiler declared in `pyproject.toml`; editable installation itself
does not compile catalogs.

Wheels and executables contain `.qm` files and locale metadata, without `.ts`
sources or compilation cache files. Installed applications load those resources
without compiling or writing to the installation. Source distributions include
the `.ts` catalogs and build adapters needed to generate a wheel.

## Validation

Run the locale contract check after changing translations or locale metadata:

```text
python scripts/check_locales.py
```

The normal test suite also validates packaged translation paths and the
supported-locale contracts. Tests exercising real Qt translation behavior
request a session fixture that compiles current sources into a temporary
directory. Pure logic tests do not require catalogs. Each test owns and removes
its installed translators so test ordering cannot change the active language.

## Translation editor

Start the optional editor with:

```text
python tools/translation_editor/main.py
```

The editor can extract source strings with Qt `lupdate`, edit `.ts` catalogs,
and save changes. Restart the development application to compile saved catalogs;
standalone and wheel builds compile their own copies automatically.
Extraction and translation review remain explicit; builds never rewrite sources.

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
3. restart the application or explicitly prepare catalogs to verify the result;
4. run `python scripts/check_locales.py`;
5. include the `.ts` catalogs and any locale metadata changes in the pull request.

Reuse the existing application vocabulary instead of introducing near-duplicate
translations for the same concept.
