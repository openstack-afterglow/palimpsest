from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError, DigestMismatchError
from palimpsest_local.refs import BuildSpec, ImageRef, LayerRef, RunSpec, StackRef


def _artifact(tmp_path: Path, name: str, content: bytes = b"artifact") -> tuple[Path, str]:
    path = tmp_path / name
    path.write_bytes(content)
    return path, f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_references_canonicalize_and_verify_artifacts(tmp_path: Path):
    image_path, image_digest = _artifact(tmp_path, "base.qcow2")
    layer_path, layer_digest = _artifact(tmp_path, "layer.squashfs")
    image = ImageRef(image_digest.upper().replace("SHA256", "sha256"), "qcow2", "x86_64", None, image_path)
    layer = LayerRef(layer_digest, "application/vnd.afterglow.palimpsest.layer.squashfs.v1", layer_path)
    stack = StackRef(image, (layer,))
    assert image.digest == image_digest
    assert RunSpec("demo", stack).memory_mib == 4096


def test_references_reject_unverified_and_invalid_shapes(tmp_path: Path):
    path, digest = _artifact(tmp_path, "base")
    with pytest.raises(DigestMismatchError):
        ImageRef("sha256:" + "0" * 64, "qcow2", "x86_64", None, path)
    with pytest.raises(ArtifactValidationError):
        ImageRef(digest, "vmdk", "x86_64", None, path)
    with pytest.raises(ArtifactValidationError):
        ImageRef(digest, "raw", "ppc64", None, path)
    with pytest.raises(ArtifactValidationError):
        BuildSpec(ImageRef(digest, "raw", "x86_64", None, path), (), tmp_path / "missing")


def test_stack_and_run_contract_limits(tmp_path: Path):
    base_path, base_digest = _artifact(tmp_path, "base")
    layer_path, layer_digest = _artifact(tmp_path, "layer")
    base = ImageRef(base_digest, "raw", "x86_64", None, base_path)
    layer = LayerRef(layer_digest, "application/test", layer_path)
    with pytest.raises(ArtifactValidationError, match="duplicate"):
        StackRef(base, (layer, layer))
    with pytest.raises(ArtifactValidationError, match="run names"):
        RunSpec("Bad Name", StackRef(base, ()))
