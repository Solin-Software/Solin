"""Runtime policy follows the package origin, independently of the working directory."""

from pathlib import Path
import runpy

import pytest

from solin.core.foundation import constants, resources


@pytest.mark.parametrize(
    ("layout", "project_metadata", "compiled", "development"),
    [
        ("src/solin", True, False, True),
        ("src/solin", False, False, False),
        ("site-packages/solin", False, False, False),
        ("site-packages/solin", True, False, False),
        ("src/solin", True, True, False),
        ("Solin.dist/solin", False, True, False),
    ],
)
def test_runtime_flags_and_policy_follow_package_origin(
    tmp_path, monkeypatch, layout, project_metadata, compiled, development,
):
    package = tmp_path / layout
    if project_metadata:
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    unrelated = tmp_path / "unrelated-checkout"
    unrelated.mkdir()
    (unrelated / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(resources, "package_root", lambda: package)

    state = runpy.run_path(
        str(Path(constants.__file__).resolve()),
        init_globals={"__compiled__": object()} if compiled else {},
    )

    assert state["IS_DEV"] is development
    assert state["IS_COMPILED"] is compiled
    identity = "SolinDev" if development else "Solin"
    assert state["QT_ORGANIZATION_NAME"] == state["QT_APPLICATION_NAME"] == identity
    assert state["QSETTINGS_ORG_NAME"] == identity
    assert state["QSETTINGS_PROFILE_ORG_PREFIX"] == f"{identity}_"
    assert state["IPC_SERVER_NAME"] == f"{identity}_IPC_v1"
    assert ("localhost" in state["NOTIFICATION_API_URL"]) is development
