"""Register paired local BuildKit solves through the canonical OCI cache.

This is local evidence binding, not a Git signature or build attestation verifier.
The caller acquires the pinned source and retains the original specs and receipts.
No registry request, Nova job, runtime packing or alternate blob store is involved.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .buildkit import BuildKitSpec, compute_build_key, compute_context_digest, image_arch_for_platform
from .errors import ArtifactValidationError
from .layer_cache import LayerCache, _directory, _read_file, _stable
from .nova_assets import AssetCatalog, _digest
from .oci_changeset import EntryKind
from .oci_converter import stage_layer
from .oci_image import strict_json_object


def _fail(message: str) -> None:
    raise ArtifactValidationError("OCI build assets: " + message)


def _file_identity(path: Path) -> tuple[str, int]:
    """Hash one stable physical file without following any path-component symlink."""
    path = path.expanduser().absolute()
    with _directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, os.geteuid()} or before.st_mode & 0o022:
                _fail("build input/output must be an owner-controlled regular file")
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _stable(before) != _stable(os.fstat(fd)) or _stable(before) != _stable(visible):
                _fail("build input/output changed while hashing")
            return "sha256:" + digest.hexdigest(), before.st_size
        finally:
            os.close(fd)


def _verified_source_commit(context: Path, expected: str) -> None:
    """Require the exact pristine source checkout, not a caller's version string.

    This deliberately rejects untracked and ignored files too: registration does
    not implement a second Dockerignore evaluator or permit uncommitted inputs.
    Git optional index writes and fsmonitor hooks are disabled for these reads.
    """
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }

    def git(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", os.fspath(context), *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            _fail("source context must be an available pristine Git checkout")
        return result.stdout.strip()

    if git("rev-parse", "--show-toplevel") != os.fspath(context):
        _fail("source context must be the exact Git checkout root")
    if git("rev-parse", "HEAD^{commit}") != expected:
        _fail("source context Git commit does not match the pinned identity")
    if git("status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"):
        _fail("source context must contain no tracked, untracked or ignored changes")


@dataclass(frozen=True)
class LocalBuildEvidence:
    """Original request plus exact bytes of BuildKit's successful v2 record.json."""

    spec: BuildKitSpec
    record_path: Path
    record_digest: str


def _verified_build(evidence: LocalBuildEvidence) -> tuple[dict[str, Any], str, str]:
    if not isinstance(evidence, LocalBuildEvidence) or not isinstance(evidence.spec, BuildKitSpec):
        _fail("an original BuildKitSpec and receipt binding are required")
    _digest(evidence.record_digest)
    path = evidence.record_path.expanduser().absolute()
    with _directory(path.parent) as parent:
        payload = _read_file(parent, path.name, mode=0o600, maximum=8 * 1024 * 1024)
    if "sha256:" + hashlib.sha256(payload).hexdigest() != evidence.record_digest:
        _fail("build result digest does not match the original receipt bytes")
    record = strict_json_object(payload, "local BuildKit result")
    spec = evidence.spec
    if (
        type(record.get("schema_version")) is not int
        or record["schema_version"] != 2
        or record.get("engine") != "buildkit"
        or record.get("status") != "success"
        or record.get("platform") != spec.platform
        or record.get("output_path") != os.fspath(spec.output)
        or not isinstance(record.get("builder_fingerprint"), str)
        or not record["builder_fingerprint"]
    ):
        _fail("successful BuildKit v2 receipt does not match its original request")
    if spec.push or spec.push_image or record.get("registry_image_pushed") is not False:
        _fail("registration requires an unpublished local build")
    if record.get("build_key") != compute_build_key(spec, builder_fingerprint=record["builder_fingerprint"]):
        _fail("build context, Dockerfile or original request differs from its receipt")
    _digest(record.get("output_oci_manifest_digest"))
    archive_digest = _digest(record.get("output_oci_archive_digest"))
    actual_digest, actual_size = _file_identity(spec.output)
    sizes = record.get("bytes")
    if (
        actual_digest != archive_digest
        or not isinstance(sizes, dict)
        or type(sizes.get("oci_archive")) is not int
        or sizes["oci_archive"] != actual_size
    ):
        _fail("OCI archive bytes do not match the build result")
    return record, compute_context_digest(spec.context), _file_identity(spec.dockerfile)[0]


def _source_handoff(cache: LayerCache, evidence: LocalBuildEvidence, record: dict, relative: Path) -> tuple[str, int]:
    """Resolve one regular artifact through verified normalized OCI changesets.

    No archive member is extracted to a host path. Canonical intake retains its
    existing decompression, member-count, byte, compression-ratio and time limits.
    """
    imported = cache.import_source(evidence.spec.output, manifest_digest=record["output_oci_manifest_digest"])
    if imported["root_digest"] != record["output_oci_manifest_digest"]:
        _fail("source OCI output contradicts its original build receipt")
    target = relative.as_posix()
    ancestors = [parent.as_posix() for parent in relative.parents]
    found = None
    with cache._locked() as (fd, _fresh):
        snapshot = cache._verified(cache._load(fd), imported["root_digest"])
        for ordinal in range(len(snapshot.layers)):
            with cache._source.lease_layer(snapshot, ordinal) as lease, stage_layer(lease) as staged:
                entries = staged.changeset.by_path()
                for ancestor in ancestors:
                    entry = entries.get(ancestor)
                    if entry is not None and (entry.kind is not EntryKind.DIRECTORY or entry.opaque):
                        found = None
                entry = entries.get(target)
                if entry is not None and entry.kind is EntryKind.HARDLINK:
                    entry = entries.get(entry.link_target)
                if entry is not None:
                    if entry.kind is not EntryKind.FILE or entry.payload is None:
                        found = None
                    else:
                        digest = hashlib.sha256()
                        with staged.lease_regular_payload(entry.payload) as payload:
                            for chunk in payload.chunks():
                                digest.update(chunk)
                        found = ("sha256:" + digest.hexdigest(), entry.size)
    if found is None:
        _fail("source OCI output does not contain the regular handoff artifact")
    return found


def register_local_oci_build(
    cache: LayerCache,
    assets: AssetCatalog,
    *,
    source_build: LocalBuildEvidence,
    runtime_build: LocalBuildEvidence,
    source_commit: str,
    handoff_path: str,
    name: str | None = None,
    packer_path: Path = Path("/usr/bin/mksquashfs"),
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Verify source→package handoff, import/materialize, then register real bytes.

    ``handoff_path`` is the same relative regular file in source_build's local
    rootfs_output directory and runtime_build's context. Both contexts must still
    match their recorded build keys. The runtime Dockerfile must be unchanged
    from the pristine Git source checkout. HEAD and the original COMMIT argument
    must match source_commit; untracked/ignored source inputs are refused.
    The current hard OCI materializer supports linux/amd64 only.
    """
    if not isinstance(cache, LayerCache) or not isinstance(assets, AssetCatalog):
        _fail("existing LayerCache and project AssetCatalog are required")
    if not isinstance(source_commit, str) or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        _fail("source commit must be a full lowercase Git commit identity")
    if not isinstance(handoff_path, str):
        _fail("handoff path must be a canonical relative path")
    relative = Path(handoff_path)
    if (
        not handoff_path
        or relative.is_absolute()
        or any(part in {".", ".."} for part in handoff_path.split("/"))
        or relative.as_posix() != handoff_path
    ):
        _fail("handoff path must be a canonical relative path")
    source_record, source_context, source_dockerfile = _verified_build(source_build)
    record, context, dockerfile = _verified_build(runtime_build)
    source_spec, runtime_spec = source_build.spec, runtime_build.spec
    _verified_source_commit(source_spec.context, source_commit)
    if source_spec.platform != runtime_spec.platform or runtime_spec.platform != "linux/amd64":
        _fail("paired builds require the supported linux/amd64 target")
    commits = [item.partition("=")[2] for item in source_spec.build_args if item.partition("=")[0] == "COMMIT"]
    if commits != [source_commit]:
        _fail("source commit contradicts the original source build argument")
    if source_spec.rootfs_output is None:
        _fail("source build must retain its real local artifact export")
    try:
        package_path = runtime_spec.dockerfile.relative_to(runtime_spec.context)
    except ValueError:
        _fail("runtime Dockerfile must be inside its assembly context")
    if _file_identity(source_spec.context / package_path)[0] != dockerfile:
        _fail("runtime Dockerfile differs from the original source package Dockerfile")
    handoff = _file_identity(source_spec.rootfs_output / relative)
    if handoff != _file_identity(runtime_spec.context / relative):
        _fail("source artifact and runtime context handoff bytes differ")
    if handoff != _source_handoff(cache, source_build, source_record, relative):
        _fail("handoff bytes are not the artifact in the successful source OCI output")

    imported = cache.import_source(runtime_spec.output, manifest_digest=record["output_oci_manifest_digest"])
    root = imported["root_digest"]
    image = imported["image"]
    if root != record["output_oci_manifest_digest"] or (image["platform"]["os"], image["platform"]["architecture"]) != (
        "linux",
        "amd64",
    ):
        _fail("verified OCI root or platform contradicts the runtime build")
    converted = cache.materialize(root, packer_path=packer_path, timeout_seconds=timeout_seconds)
    receipt = converted["receipt"]
    if (
        converted["image"] != image
        or receipt["root_descriptor"] != image["root_descriptor"]
        or receipt["manifest_digest"] != image["manifest_descriptor"]["digest"]
        or receipt["config_descriptor"] != image["config_descriptor"]
        or receipt["platform"] != {key: image["platform"][key] for key in ("os", "architecture")}
        or len(receipt["results"]) != len(image["layers"])
        or len(converted["blob_paths"]) != len(image["layers"])
    ):
        _fail("materialized graph contradicts the verified OCI source")
    layers = []
    paths = []
    for ordinal, (layer, source_layer, result, blob_path) in enumerate(
        zip(image["layers"], receipt["layers"], receipt["results"], converted["blob_paths"], strict=True)
    ):
        derived = result["receipt"]
        recipe = converted["derived"].get(derived["key_digest"])
        expected_path = cache.roots.store / "blobs" / "sha256" / _digest(derived["image_digest"]).split(":")[1]
        if (
            source_layer != {**layer, "ordinal": ordinal}
            or derived["ordinal"] != ordinal
            or derived["source_image_digest"] != receipt["source_image_digest"]
            or derived["source_snapshot_binding_digest"] != receipt["source_snapshot_binding_digest"]
            or recipe is None
            or recipe["compressed_digest"] != layer["compressed"]["digest"]
            or recipe["diff_id"] != layer["diff_id"]
            or recipe["image_digest"] != derived["image_digest"]
            or recipe["image_size"] != derived["image_size"]
            or Path(blob_path) != expected_path
        ):
            _fail("materialized occurrence or canonical physical binding is inconsistent")
        layers.append(
            {
                "digest": derived["image_digest"],
                "size": derived["image_size"],
                "compressed_digest": layer["compressed"]["digest"],
                "diff_id": layer["diff_id"],
                "recipe_digest": derived["key_digest"],
            }
        )
        paths.append(expected_path)
    # Refuse context/output/receipt replacement during the potentially long conversion.
    if _verified_build(source_build) != (source_record, source_context, source_dockerfile):
        _fail("source build evidence changed during materialization")
    if _verified_build(runtime_build) != (record, context, dockerfile):
        _fail("runtime build evidence changed during materialization")
    _verified_source_commit(source_spec.context, source_commit)
    if handoff != _file_identity(source_spec.rootfs_output / relative):
        _fail("source handoff changed during materialization")
    source = {
        "kind": "oci-build",
        "root_digest": root,
        "manifest_digest": image["manifest_descriptor"]["digest"],
        "config_digest": image["config_descriptor"]["digest"],
        "source_commit": source_commit,
        "source_context_digest": source_context,
        "source_dockerfile_digest": source_dockerfile,
        "source_build_result_digest": source_build.record_digest,
        "context_digest": context,
        "dockerfile_digest": dockerfile,
        "build_result_digest": runtime_build.record_digest,
        "handoff_digest": handoff[0],
    }
    registered = assets.register(
        arch=image_arch_for_platform(runtime_spec.platform), source=source, layers=layers, blob_paths=paths, name=name
    )
    return {**registered, "materialization": receipt}
