from __future__ import annotations

import pytest

from solin.core.conference.web_client import (
    CLIENT_FILES,
    OVERRIDE_ENVIRONMENT,
    WebClientUnavailableError,
    client_directory,
)


def _populate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name in CLIENT_FILES:
        (directory / name).write_text("//")
    return directory


def test_lookup_order(tmp_path):
    override = _populate(tmp_path / "override")
    assert client_directory(environ={OVERRIDE_ENVIRONMENT: str(override)}) == override
    app = tmp_path / "app"
    _populate(app / "web" / "livekit-client")
    assert client_directory(environ={}, compiled=True, app_executable=str(app / "Solin.bin")) == (
        app / "web" / "livekit-client"
    )
    repo = tmp_path / "repo"
    _populate(repo / "build" / "livekit-client")
    assert client_directory(environ={}, compiled=False, repo_root=repo) == repo / "build" / "livekit-client"


def test_missing_files_are_reported(tmp_path):
    (tmp_path / "build" / "livekit-client").mkdir(parents=True)
    with pytest.raises(WebClientUnavailableError, match="fetch_livekit_client"):
        client_directory(environ={}, compiled=False, repo_root=tmp_path)
