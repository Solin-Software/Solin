# Solin

Solin is a PySide6 desktop application for audio, video, projection, meetings,
OBS, Zoom, and local media workflows.

## Development

Runtime dependencies are pinned in `requirements.txt`; development tooling is in
`requirements-dev.txt`. The project can be managed with standard `venv`/`pip` or
with `uv` as the environment and installer frontend.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Equivalent `uv` flow:

```powershell
uv venv
uv pip install -r requirements-dev.txt
```

## Quality Checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe
```

`pyproject.toml` centralizes pytest, Ruff, and Pyright configuration. Ruff
enforces Pyflakes, bugbear checks, and silent `try`/`except`/`pass` prevention.
Pyright currently covers core foundation, timer, JW, meetings, and playlist
domains; controllers/widgets are the next incremental typing frontier because
they rely heavily on dynamic Qt attributes and mixins.
