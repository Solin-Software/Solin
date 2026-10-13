"""Keep fixture HTTP traffic on loopback without implicit host discovery."""

from __future__ import annotations

import ast

from tests._paths import REPO_ROOT


def test_http_fixtures_use_the_loopback_server_adapter():
    root = REPO_ROOT / "tests"
    adapter = root / "_http.py"
    violations = []
    for path in root.rglob("*.py"):
        if path == adapter:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "http.server"
                and any(alias.name in {"HTTPServer", "ThreadingHTTPServer", "*"}
                        for alias in node.names)
            ):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import) and any(
                alias.name == "http.server" for alias in node.names
            ):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert violations == [], (
        "HTTP fixtures must use tests._http to avoid implicit hostname discovery: "
        + ", ".join(violations)
    )
