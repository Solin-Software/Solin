# Solin

Solin is a PySide6 desktop application for audio, video, projection, meetings,
OBS, Zoom, and local media workflows.

## Development

Runtime dependencies are pinned in `requirements.txt`; development tooling is in
`requirements-dev.txt`. The project can be managed with standard `venv`/`pip` or
with `uv` as the environment and installer frontend.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -e .
```

Equivalent `uv` flow:

```powershell
uv venv
uv pip install -r requirements-dev.txt -e .
```

The importable application package lives under `src/solin`. Use the installed
`solin` GUI entry point after editable installation, or `python main.py` as the
thin local launcher during development.

## Quality Checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe scripts\check_locales.py
.\.venv\Scripts\python.exe scripts\validate_native_webview.py
```

`pyproject.toml` centralizes pytest, Ruff, and Pyright configuration. Ruff
enforces Pyflakes, bugbear checks, silent `try`/`except`/`pass` prevention, and
`BLE001` globally. Broad catches are allowed only at intentional runtime
boundaries and must use a locally justified `# noqa: BLE001 - ...`; the
exception-policy test independently enforces that contract.
Pyright currently covers core foundation, timer, JW, meetings, and playlist
domains; controllers/widgets are the next incremental typing frontier because
they rely heavily on dynamic Qt attributes and mixins.

## Delivery Layout

Versioned delivery automation lives outside generated output directories:
`scripts/` contains repository operations such as QML cache compilation,
translation validation, local Windows builds, and release diffs; `packaging/`
contains installer recipes; `resources/` contains runtime assets and
translations. `build/` and `dist/` are ignored output-only directories.
