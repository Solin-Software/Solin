from __future__ import annotations

import ast
from pathlib import Path


def test_app_layers_do_not_silently_swallow_exception_pass_blocks():
    roots = [
        Path("app/core"),
        Path("app/controllers"),
        Path("app/widgets"),
        Path("app/projection"),
    ]
    offenders: list[str] = []

    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                exception_type = ast.unparse(node.type) if node.type else ""
                if (
                    exception_type == "Exception"
                    and len(node.body) == 1
                    and isinstance(node.body[0], ast.Pass)
                ):
                    offenders.append(f"{path}:{node.lineno}")

    assert offenders == []
