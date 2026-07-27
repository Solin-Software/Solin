# Development tools

Utilities under `tools/` are opt-in development aids. They are not imported by
the Solin runtime and must not be required for normal application startup.

## Translation editor

`tools/translation_editor/main.py` manages the Qt translation catalogs. See
[Translations](translations.md) for its dependencies and workflow.

## Development data manager

`tools/dev_data_manager.py` manages the isolated `SolinDev` state used during
development and migration testing:

```text
python tools/dev_data_manager.py
```

The tool can:

- delete the current user's `SolinDev` QSettings registry trees;
- delete the corresponding development data and cache directories;
- replace that state with a synthetic legacy fixture for migration testing.

These actions are destructive by design. Close every Solin development process,
inspect the paths displayed by the tool, and read the confirmation dialog before
continuing. It targets development namespaces only and must never be adapted to
delete real application data.

## Repository scripts

Repeatable validation, build, and packaging operations belong in `scripts/`.
Prefer a script over undocumented manual steps when an operation must produce
the same result for another contributor or a build runner.
