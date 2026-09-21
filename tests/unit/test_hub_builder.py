"""Pinned Hub inputs and successful output handoff across the VM boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from palimpsest_local import hub_builder
from palimpsest_local.oci_layout import ContentStore


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_host_build_helper_checks_recipe_and_source_bytes_before_guest(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    base_bytes = b"pinned fixture image bytes"
    base_path = tmp_path / "base.raw"
    base_path.write_bytes(base_bytes)
    base_digest = _digest(base_bytes)
    output = b"hsqs\x00\x00guest output fixture"
    output_digest = _digest(output)
    manifest = {
        "name": "worker-result",
        "recipe": f"FROM {base_digest}\nRUN echo ready > /opt/ready",
        "base": {"digest": base_digest, "path": str(base_path), "arch": "x86_64", "disk_format": "raw"},
        "layers": [],
    }
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    def guest_boundary(spec, *, roots):
        assert spec.network == "none"
        assert spec.base.digest == base_digest
        assert spec.base.local_path.read_bytes() == base_bytes
        result_path = ContentStore(roots.store).blob_path(output_digest)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_bytes(output)
        return {"output_digest": output_digest}

    monkeypatch.setattr(hub_builder, "build_layer", guest_boundary)
    result = hub_builder.run_build(manifest, job_dir)
    assert result == {"digest": output_digest, "size_bytes": len(output)}
    assert (job_dir / "result.sqsh").read_bytes() == output

    base_path.write_bytes(b"tampered base bytes")
    with pytest.raises(Exception, match="digest|mismatch"):
        hub_builder.run_build(manifest, job_dir)

    base_path.write_bytes(base_bytes)
    wrong_recipe = {**manifest, "recipe": f"FROM {'sha256:' + 'f' * 64}\nRUN echo ready"}
    with pytest.raises(Exception, match="base digest mismatch"):
        hub_builder.run_build(wrong_recipe, job_dir)
