# Solin

Solin is a PySide6 desktop application for meeting media, projection, local
playback, and live integrations.

It brings the operational parts of a meeting into one desktop workflow:

- meeting preparation and reusable playlists;
- audio, video, image, subtitle, and presentation playback;
- projection windows and operator controls;
- OBS and Zoom integrations;
- an optional, private-LAN remote control;
- Windows, macOS, and Linux delivery targets.

## Development

Solin requires Python 3.13 or newer. Use an isolated Python environment when
possible, with whichever environment manager is appropriate for the platform.

Install the locked development environment and the application in editable
mode:

```text
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

Start the application with the installed `solin` entry point or the thin local
launcher:

```text
python main.py
```

Linux needs additional native libraries for QtMultimedia and the embedded
SideView browser. See [Building and platform requirements](docs/building.md).

## Quality checks

Run the relevant checks locally before opening a pull request:

```text
python -m pytest
python -m ruff check .
python scripts/typecheck.py
python scripts/check_locales.py
```

Tests are grouped under `tests/unit`, `tests/integration`, `tests/contract`, and
`tests/e2e`. A focused suite can be selected with `pytest -m unit`,
`pytest -m integration`, or `pytest -m contract`.

## Documentation

- [Contributing](CONTRIBUTING.md)
- [Building and platform requirements](docs/building.md)
- [Local remote control](docs/remote-control.md)
- [Linked-folder collaboration and migration](docs/linked-folder-sync.md)
- [Translations](docs/translations.md)
- [Development tools](docs/development-tools.md)
- [Versioning and releases](docs/releases.md)
- [Architecture decision records](docs/adr/README.md)

The remote-control implementation contract lives beside its static resources in
[`src/solin/resources/remote_control/README.md`](src/solin/resources/remote_control/README.md).

## Repository layout

```text
src/solin/   importable application and packaged resources
tests/       unit, integration, contract, and packaged-app tests
scripts/     validation, build, and packaging entry points
packaging/   platform-specific installer and application metadata
tools/       opt-in developer utilities
```

`build/` and `dist/` contain generated output and are not versioned.
