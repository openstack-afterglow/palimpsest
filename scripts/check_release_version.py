#!/usr/bin/env python3
"""Reject malformed or mismatched immutable release tags before publication."""

from __future__ import annotations

import ast
import os
import re
import tomllib
from pathlib import Path


def main() -> None:
    tag = os.environ.get("RELEASE_TAG", "")
    match = re.fullmatch(r"v?((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))", tag)
    if match is None:
        raise SystemExit("release tag must be X.Y.Z (historical vX.Y.Z also accepted)")
    version = match.group(1)
    for file in ("pyproject.toml", "hub/pyproject.toml"):
        if tomllib.loads(Path(file).read_text())["project"]["version"] != version:
            raise SystemExit("release tag and distribution versions differ")

    client_module = ast.parse(Path("src/palimpsest_local/__init__.py").read_text(encoding="utf-8"))
    client_version = next(
        (
            ast.literal_eval(node.value)
            for node in client_module.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
        ),
        None,
    )
    if client_version != version:
        raise SystemExit("release tag and client CLI versions differ")
    print(version)


if __name__ == "__main__":
    main()
