from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from palimpsest_local import platforms
from palimpsest_local.cloud_runtime import run
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.refs import ImageRef, RunSpec, StackRef
from palimpsest_local.state import init_roots


def test_runtime_rejects_unsupported_host_image_before_run_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    base = roots.store / "base.raw"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(b"base")
    digest = f"sha256:{hashlib.sha256(base.read_bytes()).hexdigest()}"
    spec = RunSpec("foreign-run", StackRef(ImageRef(digest, "raw", "x86_64", None, base), ()))
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    with pytest.raises(ArtifactValidationError, match="no local runtime can boot a x86_64 image on Darwin/aarch64"):
        run(spec, roots=roots)
    assert not (roots.runs / "foreign-run").exists()
