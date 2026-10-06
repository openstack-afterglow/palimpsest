from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError, DigestMismatchError
from palimpsest_local.refs import (
    BuildSpec,
    HostDirectoryShare,
    ImageRef,
    LayerRef,
    PortForward,
    RunSpec,
    StackRef,
    VolumeAttachment,
)


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


def test_project_runtime_resources_are_strict_and_collision_free(tmp_path: Path):
    base_path, base_digest = _artifact(tmp_path, "base")
    volume_path, _ = _artifact(tmp_path, "data.raw")
    stack = StackRef(ImageRef(base_digest, "raw", "x86_64", None, base_path), ())
    volume = VolumeAttachment("data", "/var/lib/data", host_path=volume_path)
    port = PortForward("127.0.0.1", 8080, 80)

    spec = RunSpec("demo", stack, ports=(port,), volumes=(volume,), environment=(("APP_ENV", "prod"),))
    assert spec.ports == (port,)
    assert spec.volumes == (volume,)

    with pytest.raises(ArtifactValidationError, match="duplicate host port"):
        RunSpec("demo", stack, ports=(port, port))
    with pytest.raises(ArtifactValidationError, match="single NUL-free line"):
        RunSpec("demo", stack, environment=(("BAD", "one\ntwo"),))
    with pytest.raises(ArtifactValidationError, match="shadow"):
        VolumeAttachment("bad", "/proc/data", host_path=volume_path)


def test_host_share_policy_is_structural_and_live_validation_is_explicit(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "absent-project"
    share = HostDirectoryShare(root, "absent-source", "/srv/shared", True)
    assert share.host_path == root / "absent-source"
    assert share.guest_tag.startswith("ps-")
    assert not root.exists()
    with pytest.raises(ArtifactValidationError, match="missing"):
        share.validate_source()
    assert not root.exists()


@pytest.mark.parametrize(
    "source",
    ["", ".", "./data", "data/", "data//child", "../data", "/data", "data\\child", "data\nchild", "data\x00child"],
)
def test_host_share_policy_rejects_malformed_source_without_filesystem_access(
    tmp_path: Path,
    source: str,
) -> None:
    with pytest.raises(ArtifactValidationError, match="project-relative"):
        HostDirectoryShare(tmp_path.resolve(), source, "/srv/shared")


@pytest.mark.parametrize(
    "root", [Path("relative"), Path("/project/../other"), Path("//project"), Path("/project\nroot")]
)
def test_host_share_policy_rejects_ambiguous_root(root: Path) -> None:
    with pytest.raises(ArtifactValidationError, match="normalized absolute"):
        HostDirectoryShare(root, "data", "/srv/shared")


@pytest.mark.parametrize("target", [None, "/srv/shared\nother", "/srv/shared\x00other"])
def test_host_share_policy_rejects_malformed_target(tmp_path: Path, target: str | None) -> None:
    with pytest.raises(ArtifactValidationError, match="single-line"):
        HostDirectoryShare(tmp_path.resolve(), "data", target)


def test_host_share_policy_rejects_unencodable_paths(tmp_path: Path) -> None:
    with pytest.raises(ArtifactValidationError, match="UTF-8"):
        HostDirectoryShare(tmp_path.resolve(), "data\ud800", "/srv/shared")


@pytest.mark.parametrize("target", ["relative", "//srv/shared", "/srv//shared", "/srv/../shared", "/"])
def test_host_share_policy_normalizes_guest_path_errors(tmp_path: Path, target: str) -> None:
    with pytest.raises(ArtifactValidationError, match="host share target"):
        HostDirectoryShare(tmp_path.resolve(), "data", target)
