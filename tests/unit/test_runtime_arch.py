from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.refs import ImageRef, RunSpec, StackRef
from palimpsest_local.runtime import run
from palimpsest_local.state import init_roots


def test_runtime_rejects_non_x86_64_before_run_creation(tmp_path: Path):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    base = roots.store / "base.raw"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(b"base")
    digest = f"sha256:{hashlib.sha256(base.read_bytes()).hexdigest()}"
    spec = RunSpec("arm-run", StackRef(ImageRef(digest, "raw", "aarch64", None, base), ()))
    with pytest.raises(ArtifactValidationError, match="x86_64"):
        run(spec, roots=roots)
    assert not (roots.runs / "arm-run").exists()
