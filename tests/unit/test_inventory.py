from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from palimpsest_local import inventory, state
from palimpsest_local.errors import ArtifactValidationError, StateError
from palimpsest_local.hub import KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS
from palimpsest_local.oci_layout import ContentStore, build_bundle_tar_bytes
from palimpsest_local.runtime_types import CapabilityCheck
from palimpsest_local.state import TagRecord, init_roots, write_tag_record


@pytest.fixture(autouse=True)
def _stub_operation_capability_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        inventory.runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "test-present", True),
    )
    monkeypatch.setattr(
        inventory.host_inventory,
        "observe",
        lambda: {
            "vms": [],
            "volumes": [],
            "networks": [],
            "warnings": [],
        },
    )


def _setup_roots(tmp_path: Path) -> state.StatePaths:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return init_roots({"XDG_CONFIG_HOME": str(config_dir), "XDG_STATE_HOME": str(state_dir)})


@pytest.mark.parametrize("retention", [None, "delete"])
def test_force_web_run_is_accepted_by_foreground_carrier_policy(tmp_path, retention):
    from palimpsest_local.cli import build_parser
    from palimpsest_local.oci_run_request import LocalOCIRunRequest
    from palimpsest_local.runtime_types import ExperimentalOCIDispatchKey

    roots = _setup_roots(tmp_path)
    captured = []

    class Operations:
        def start(self, _roots, kind, argv, *, resource_name):
            assert kind == "run" and resource_name == "web-carrier"
            captured.append(build_parser().parse_args(argv))
            return {"id": "submitted"}

    body = {
        "image": str(tmp_path / "image.oci"),
        "name": "web-carrier",
        "runtime_kind": "oci-root",
        "backend": "lima-qemu-oci",
        "platform": "linux/amd64",
        "force_architecture": True,
        "vm_image": str(tmp_path / "carrier.qcow2"),
        "vm_image_digest": "sha256:" + "a" * 64,
    }
    if retention is not None:
        body["root_retention"] = retention
    inventory.run_image(roots, Operations(), body)
    args = captured[0]
    request = LocalOCIRunRequest(
        name=args.name,
        source=Path(args.image_or_bundle),
        detached=args.detach,
        backend=args.backend,
        platform=args.platform,
        force_architecture=args.force_architecture,
        root_retention=args.root_retention or "delete",
        vm_image=args.vm_image,
        vm_image_digest=args.vm_image_digest,
    )
    assert isinstance(request.dispatch_key, ExperimentalOCIDispatchKey)
    assert not request.detached and request.root_retention == "delete"


def test_force_web_run_rejects_explicit_retained_root_before_submission(tmp_path):
    class Operations:
        def start(self, *_args, **_kwargs):
            pytest.fail("invalid retained carrier must not be submitted")

    with pytest.raises(StateError, match="force_architecture requires disposable root_retention delete, not retain"):
        inventory.run_image(
            _setup_roots(tmp_path),
            Operations(),
            {"image": "image.oci", "force_architecture": True, "root_retention": "retain"},
        )


@pytest.mark.parametrize("runtime_kind", ["oci-root", "cloud-image"])
@pytest.mark.parametrize("force", [None, False])
def test_native_and_conventional_web_runs_stay_detached(tmp_path, runtime_kind, force):
    from palimpsest_local.cli import build_parser

    captured = []

    class Operations:
        def start(self, _roots, _kind, argv, **_kwargs):
            captured.append(build_parser().parse_args(argv))
            return {"id": "submitted"}

    body = {"image": "local-image", "runtime_kind": runtime_kind, "root_retention": "retain"}
    if force is not None:
        body["force_architecture"] = force
    inventory.run_image(_setup_roots(tmp_path), Operations(), body)
    assert captured[0].detach is True
    assert captured[0].force_architecture is False
    assert captured[0].root_retention == "retain"


def test_storage_report(tmp_path: Path):
    roots = _setup_roots(tmp_path)
    (roots.store / "sample.txt").write_text("hello store", encoding="utf-8")
    report = inventory.storage_report(roots)

    assert report["state_root"] == str(roots.state)
    assert report["source"] in ("env", "config", "default")
    assert report["directories"]["store"] > 0
    assert report["total_state_bytes"] > 0


def test_list_vms_and_get_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = _setup_roots(tmp_path)

    # Synthesize run ledgers
    run1 = roots.runs / "demo-kvm"
    run1.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        run1 / "owner.json", {"schema_version": 1, "run_id": "11111111-1111-1111-1111-111111111111", "name": "demo-kvm"}
    )
    state.atomic_write_json(
        run1 / "state.json",
        {
            "name": "demo-kvm",
            "run_id": "11111111-1111-1111-1111-111111111111",
            "backend": "kvm",
            "status": "running",
            "memory_mib": 2048,
            "vcpus": 2,
            "base": {
                "digest": "sha256:" + "a" * 64,
                "arch": "x86_64",
                "local_path": "/private/SENSITIVE_VALUE/base.qcow2",
            },
            "layers": [
                {
                    "digest": "sha256:" + "b" * 64,
                    "target_dev": "vdb",
                    "local_path": "/private/SENSITIVE_VALUE/layer.squashfs",
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "mount_path": "/srv/Data",
                    "filesystem": "ext4",
                    "read_only": False,
                    "target_dev": "vdc",
                    "host_path": "/private/SENSITIVE_VALUE/data.raw",
                }
            ],
            "ssh": {"host": "127.0.0.1", "port": 2222},
            "created_at": "2026-08-24T00:00:00Z",
            "updated_at": "2026-08-24T00:01:00Z",
        },
    )

    run2 = roots.runs / "demo-lima"
    run2.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        run2 / "owner.json",
        {"schema_version": 1, "run_id": "22222222-2222-2222-2222-222222222222", "name": "demo-lima"},
    )
    state.atomic_write_json(
        run2 / "state.json",
        {
            "name": "demo-lima",
            "run_id": "22222222-2222-2222-2222-222222222222",
            "backend": "lima-vz",
            "status": "stopped",
            "created_at": "2026-08-24T00:00:00Z",
        },
    )

    # Synthesize project ledger
    proj = roots.projects / "myproj"
    proj.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        proj / "state.json",
        {
            "schema_version": 1,
            "project": "myproj",
            "config_digest": "sha256:" + "c" * 64,
            "services": [
                {
                    "service": "web",
                    "run_name": "demo-kvm",
                    "config_digest": "sha256:" + "d" * 64,
                    "run_id": "11111111-1111-1111-1111-111111111111",
                    "backend": "kvm",
                }
            ],
            "order": ["web"],
            "volumes": [],
            "created_at": "2026-08-24T00:00:00Z",
            "updated_at": "2026-08-24T00:00:00Z",
        },
    )

    monkeypatch.setattr(inventory.runtime_dispatch.cloud_runtime, "reconcile_run", lambda *_a, **_k: {})
    monkeypatch.setattr(inventory.runtime_dispatch.lima, "reconcile_run", lambda *_a, **_k: {})

    vms_res = inventory.list_vms(roots)
    vms = vms_res["vms"]
    assert len(vms) == 2

    kvm_vm = next(v for v in vms if v["name"] == "demo-kvm")
    assert kvm_vm["runtime_kind"] == "cloud-image"
    assert kvm_vm["backend"] == "kvm"
    assert kvm_vm["project"] == "myproj"
    assert kvm_vm["base_digest"] == "sha256:" + "a" * 64
    assert kvm_vm["layer_count"] == 1
    assert kvm_vm["ssh"] == {"host": "127.0.0.1", "port": 2222}
    assert "SENSITIVE_VALUE" not in repr(vms_res)
    assert "local_path" not in repr(vms_res)
    assert "host_path" not in repr(vms_res)

    vm_detail = inventory.get_vm(roots, "demo-kvm")
    assert vm_detail["name"] == "demo-kvm"

    with pytest.raises(StateError, match="VM 'unknown' not found"):
        inventory.get_vm(roots, "unknown")


def test_list_vms_stale_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = _setup_roots(tmp_path)
    run_dir = roots.runs / "stale-vm"
    run_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        run_dir / "owner.json",
        {"schema_version": 1, "run_id": "33333333-3333-3333-3333-333333333333", "name": "stale-vm"},
    )
    state.atomic_write_json(run_dir / "state.json", {"name": "stale-vm", "backend": "kvm", "status": "running"})

    monkeypatch.setattr(
        inventory.runtime_dispatch.cloud_runtime,
        "reconcile_run",
        lambda *_a, **_k: (_ for _ in ()).throw(ArtifactValidationError("/dev/kvm is not accessible")),
    )

    res = inventory.list_vms(roots)
    assert len(res["vms"]) == 1
    vm = res["vms"][0]
    assert vm["stale"] is True
    assert any("runtime reconciliation failed" in w for w in res["warnings"])


def test_list_vms_uses_non_reflective_token_for_invalid_entry_name(tmp_path: Path) -> None:
    roots = _setup_roots(tmp_path)
    invalid = roots.runs / "BAD SENSITIVE_VALUE"
    invalid.mkdir()
    (invalid / "owner.json").write_text("SENSITIVE_VALUE", encoding="utf-8")

    result = inventory.list_vms(roots)

    assert result["vms"] == []
    assert len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("entry-")
    assert "invalid run entry" in result["warnings"][0]
    assert "SENSITIVE_VALUE" not in repr(result)


def test_list_artifacts(tmp_path: Path):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)

    img_digest = f"sha256:{store.write_stream([b'image artifact']).name}"
    layer_digest = f"sha256:{store.write_stream([b'layer artifact']).name}"
    unknown_digest = f"sha256:{store.write_stream([b'unknown artifact']).name}"

    # Write store blobs & metadata
    (roots.store / "metadata").mkdir(parents=True, exist_ok=True)

    store.write_metadata(
        img_digest, {"kind": KIND_CLOUD_IMAGE, "disk_format": "qcow2", "arch": "x86_64", "name": "ubuntu.img"}
    )
    store.write_metadata(
        layer_digest, {"kind": "squashfs", "media_type": MEDIA_TYPE_LAYER_SQUASHFS, "base_image_digest": img_digest}
    )
    store.write_metadata(unknown_digest, {"kind": "other"})

    # Write tag record
    write_tag_record(
        roots,
        TagRecord(
            schema_version=1,
            tag="ubuntu-base",
            digest=img_digest,
            media_type="application/octet-stream",
            size_bytes=20,
            parent_digest=None,
            base_image_digest=None,
            source="import",
            created_at="2026-08-24T00:00:00Z",
        ),
    )

    # Write run ledger referencing img_digest and layer_digest
    run_dir = roots.runs / "art-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        run_dir / "owner.json",
        {"schema_version": 1, "run_id": "44444444-4444-4444-4444-444444444444", "name": "art-run"},
    )
    state.atomic_write_json(
        run_dir / "state.json",
        {
            "name": "art-run",
            "backend": "kvm",
            "status": "stopped",
            "base": {"digest": img_digest},
            "layers": [{"digest": layer_digest}],
        },
    )

    res = inventory.list_artifacts(roots)
    assert len(res["images"]) == 1
    assert len(res["layers"]) == 1
    assert len(res["unknown"]) == 1

    img_art = res["images"][0]
    assert img_art["digest"] == img_digest
    assert len(img_art["tags"]) == 1
    assert img_art["tags"][0]["tag"] == "ubuntu-base"
    assert img_art["referenced_by"]["runs"] == ["art-run"]


def _buildkit_export(roots: state.StatePaths, build_id: str, **changes):
    """A deterministic real OCI export and the receipt written by BuildKit."""
    layer = build_bundle_tar_bytes({"hello.txt": b"hello from Dockerfile\n"})
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(layer).hexdigest()]},
            "config": {},
        },
        sort_keys=True,
    ).encode()

    def descriptor(payload, media_type):
        return {
            "mediaType": media_type,
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    config_desc = descriptor(config, "application/vnd.oci.image.config.v1+json")
    layer_desc = descriptor(layer, "application/vnd.oci.image.layer.v1.tar")
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config_desc,
            "layers": [layer_desc],
        },
        sort_keys=True,
    ).encode()
    manifest_desc = descriptor(manifest, "application/vnd.oci.image.manifest.v1+json")
    archive = build_bundle_tar_bytes(
        {
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            "index.json": json.dumps({"schemaVersion": 2, "manifests": [manifest_desc]}).encode(),
            "blobs/sha256/" + config_desc["digest"].split(":")[1]: config,
            "blobs/sha256/" + layer_desc["digest"].split(":")[1]: layer,
            "blobs/sha256/" + manifest_desc["digest"].split(":")[1]: manifest,
        }
    )
    output = roots.builds / "outputs" / f"{build_id}.oci.tar"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(archive)
    record = {
        "schema_version": 2,
        "engine": "buildkit",
        "build_id": build_id,
        "status": "success",
        "output_path": str(output.resolve()),
        "output_oci_manifest_digest": manifest_desc["digest"],
        "output_oci_archive_digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
        "output_tags": ["example/image:dev"],
        "runtime_block_digest": None,
        "bytes": {"oci_archive": len(archive)},
        "platform": "linux/amd64",
        "finished_at": "2026-08-24T11:00:00Z",
        **changes,
    }
    build_dir = roots.builds / build_id
    build_dir.mkdir(exist_ok=True)
    state.atomic_write_json(build_dir / "record.json", record)
    return record


def test_plain_buildkit_exports_are_images_with_typed_identity_and_provenance(tmp_path):
    roots = _setup_roots(tmp_path)
    first = _buildkit_export(roots, "bk-000000000001")
    second = _buildkit_export(roots, "bk-000000000002")
    # Two byte-distinct archives of the same image must still produce one row.
    output = Path(second["output_path"])
    output.write_bytes(output.read_bytes() + b"\0" * 512)
    second["output_oci_archive_digest"] = "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()
    second["bytes"]["oci_archive"] = output.stat().st_size
    state.atomic_write_json(roots.builds / second["build_id"] / "record.json", second)

    result = inventory.list_artifacts(roots)

    assert result["artifacts"] == result["images"]
    assert result["layers"] == result["unknown"] == []
    assert len(result["images"]) == 1
    image = result["images"][0]
    assert image["kind"] == "oci-image"
    assert image["digest_type"] == "oci-manifest"
    assert image["digest"] == first["output_oci_manifest_digest"]
    assert image["output_oci_archive_digest"] == first["output_oci_archive_digest"]
    assert image["size_bytes"] == Path(first["output_path"]).stat().st_size
    assert image["output_oci_archive_size_bytes"] == image["size_bytes"]
    assert image["availability"] == "available"
    assert image["editable"] is False
    assert image["source"] == "build-record"
    assert image["tags"] == []  # Recorded Docker output tags are not local CAS bindings.
    assert image["output_tags"] == ["example/image:dev"]
    assert [build["build_id"] for build in image["builds"]] == [first["build_id"], second["build_id"]]
    assert [build["output_oci_archive_digest"] for build in image["builds"]] == [
        first["output_oci_archive_digest"],
        second["output_oci_archive_digest"],
    ]
    assert all(build["runtime_block_digest"] is None for build in image["builds"])
    with pytest.raises(StateError):
        inventory.tag_artifact(roots, image["digest"], "not-in-cas")
    assert not (roots.tags / "not-in-cas.json").exists()


def test_buildkit_inventory_preserves_cloud_and_runtime_projections(tmp_path):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    cloud_digest = "sha256:" + store.write_stream([b"cloud image"]).name
    runtime_digest = "sha256:" + store.write_stream([b"runtime block"]).name
    store.write_metadata(cloud_digest, {"kind": KIND_CLOUD_IMAGE, "disk_format": "qcow2"})
    store.write_metadata(runtime_digest, {"kind": "squashfs", "media_type": MEDIA_TYPE_LAYER_SQUASHFS})
    inventory.tag_artifact(roots, runtime_digest, "runtime")
    build = _buildkit_export(roots, "bk-000000000001", runtime_block_digest=runtime_digest, runtime_tag="runtime")
    run = roots.runs / "user"
    run.mkdir()
    state.atomic_write_json(run / "state.json", {"base": {"digest": cloud_digest}, "layers": [runtime_digest]})
    project = roots.projects / "project"
    project.mkdir()
    state.atomic_write_json(project / "state.json", {"project": "project", "services": [{"run_name": "user"}]})

    result = inventory.list_artifacts(roots)

    assert len(result["images"]) == 2
    cloud = next(image for image in result["images"] if image["kind"] == KIND_CLOUD_IMAGE)
    image = next(image for image in result["images"] if image["kind"] == "oci-image")
    layer = result["layers"][0]
    assert cloud["referenced_by"] == layer["referenced_by"] == {"runs": ["user"], "projects": ["project"]}
    assert layer["digest"] == layer["runtime_block_digest"] == runtime_digest
    assert image["digest"] == build["output_oci_manifest_digest"] != runtime_digest
    assert layer["tags"][0]["tag"] == "runtime"
    assert layer["builds"][0]["build_id"] == build["build_id"]
    detail = inventory.get_build(roots, build["build_id"])
    assert detail["output_digest"] == detail["runtime_block_digest"] == runtime_digest
    assert detail["output_oci_manifest_digest"] == image["digest"]
    assert detail["output_oci_archive_digest"] == build["output_oci_archive_digest"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "changed",
        "symlink",
        "hardlink",
        "failed",
        "relative",
        "invalid-digest",
        "bad-path",
        "bad-record",
        "record-symlink",
        "directory-symlink",
    ],
)
def test_buildkit_inventory_retains_provenance_and_labels_unavailable_exports(tmp_path, damage):
    roots = _setup_roots(tmp_path)
    rec = _buildkit_export(roots, "bk-000000000001")
    output = Path(rec["output_path"])
    record_path = roots.builds / rec["build_id"] / "record.json"
    if damage == "missing":
        output.unlink()
    elif damage == "changed":
        output.write_bytes(b"no longer the recorded OCI export")
    elif damage in {"symlink", "hardlink"}:
        external = tmp_path / "export.tar"
        output.rename(external)
        if damage == "symlink":
            output.symlink_to(external)
        else:
            os.link(external, output)
    elif damage == "failed":
        rec["status"] = "failed"
    elif damage == "relative":
        rec["output_path"] = "relative.oci.tar"
    elif damage == "invalid-digest":
        rec["output_oci_archive_digest"] = "not a digest"
    elif damage == "bad-path":
        rec["output_path"] = {"path": str(output)}
    elif damage == "bad-record":
        record_path.write_text("[]")
    elif damage == "record-symlink":
        external = tmp_path / "record.json"
        record_path.rename(external)
        record_path.symlink_to(external)
    elif damage == "directory-symlink":
        external = tmp_path / "build"
        record_path.parent.rename(external)
        record_path.parent.symlink_to(external, target_is_directory=True)
    if damage in {"failed", "relative", "invalid-digest", "bad-path"}:
        state.atomic_write_json(record_path, rec)

    images = inventory.list_artifacts(roots)["images"]
    if damage in {"failed", "bad-record", "record-symlink", "directory-symlink"}:
        assert images == []
    else:
        assert len(images) == 1
        image = images[0]
        assert image["digest"] == rec["output_oci_manifest_digest"]
        expected = "missing" if damage == "missing" else "available" if damage == "invalid-digest" else "unavailable"
        assert image["availability"] == expected
        assert image["editable"] is False
        assert image["build_ids"] == [rec["build_id"]]
    if damage in {"bad-record", "record-symlink", "directory-symlink"}:
        with pytest.raises(StateError):
            inventory.get_build(roots, rec["build_id"])


@pytest.mark.parametrize("manifest", [None, "invalid-digest", 42])
def test_buildkit_archive_identity_and_conservative_detail(tmp_path, manifest):
    roots = _setup_roots(tmp_path)
    rec = _buildkit_export(
        roots,
        "bk-000000000001",
        output_oci_manifest_digest=manifest,
        runtime_block_digest={"digest": "invalid"},
        output_tags="not-a-list",
        platform=["linux/amd64"],
        started_at=42,
        timings_ms={"total": float("inf")},
    )
    image = inventory.list_artifacts(roots)["images"][0]
    detail = inventory.get_build(roots, rec["build_id"])
    assert image["digest_type"] == "oci-archive"
    assert image["digest"] == detail["output_digest"] == rec["output_oci_archive_digest"]
    assert detail["output_oci_manifest_digest"] is detail["runtime_block_digest"] is None
    assert detail["output_tags"] == []
    assert detail["platform"] is detail["started_at"] is detail["duration_ms"] is None


def test_image_monitoring_does_not_release_a_blocked_fifo_writer(tmp_path):
    roots = _setup_roots(tmp_path)
    rec = _buildkit_export(roots, "bk-000000000001")
    output = Path(rec["output_path"])
    output.unlink()
    os.mkfifo(output, 0o600)
    starting = threading.Event()
    opened = threading.Event()

    def write_peer():
        starting.set()
        fd = os.open(output, os.O_WRONLY)
        opened.set()
        os.close(fd)

    writer = threading.Thread(target=write_peer)
    writer.start()
    try:
        assert starting.wait(5)
        image = inventory.list_artifacts(roots)["images"][0]
        assert image["availability"] == "unavailable"
        assert not opened.wait(0.2), "read-only monitoring opened the FIFO and released its writer"
    finally:
        reader_fd = os.open(output, os.O_RDONLY | os.O_NONBLOCK)
        try:
            writer.join(5)
            assert not writer.is_alive()
        finally:
            os.close(reader_fd)


def test_local_tag_persists_conflicts_and_idempotence_without_changing_artifact(tmp_path):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = "sha256:" + store.write_stream([b"local cloud image"]).name
    other = "sha256:" + store.write_stream([b"other cloud image"]).name
    for value in (digest, other):
        store.write_metadata(value, {"kind": KIND_CLOUD_IMAGE, "disk_format": "qcow2"})
        payload = state.read_json(store.metadata_path(value))
        payload["size"] = 999  # Editable metadata is not authoritative for CAS bytes.
        state.atomic_write_json(store.metadata_path(value), payload)
    before_blob = store.blob_path(digest).stat()
    before_meta = store.metadata_path(digest).read_bytes()

    binding = inventory.tag_artifact(roots, digest, "base")
    before_tag = (roots.tags / "base.json").read_bytes()
    assert binding["digest"] == digest
    assert binding["size_bytes"] == len(b"local cloud image")
    assert binding["media_type"] == "application/vnd.afterglow.palimpsest.image.qcow2.v1"
    assert state.read_tag_record(roots, "base").digest == digest
    assert inventory.tag_artifact(roots, digest, "base") == binding
    assert (roots.tags / "base.json").read_bytes() == before_tag
    with pytest.raises(StateError, match="different digest"):
        inventory.tag_artifact(roots, other, "base")
    assert (roots.tags / "base.json").read_bytes() == before_tag
    assert store.metadata_path(digest).read_bytes() == before_meta
    after_blob = store.blob_path(digest).stat()
    assert (before_blob.st_ino, before_blob.st_mode, before_blob.st_mtime_ns) == (
        after_blob.st_ino,
        after_blob.st_mode,
        after_blob.st_mtime_ns,
    )
    assert store.blob_path(digest).read_bytes() == b"local cloud image"
    image = next(image for image in inventory.list_artifacts(roots)["images"] if image["digest"] == digest)
    assert image["tags"][0] == binding


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "unsupported",
        "writable",
        "corrupt",
        "symlink",
        "hardlink",
        "missing-metadata",
        "metadata-symlink",
        "wrong-digest",
        "bad-parent",
        "bad-format",
    ],
)
def test_tag_artifact_refuses_unproven_cas_targets(tmp_path, damage):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = "sha256:" + store.write_stream([b"local artifact"]).name
    meta = {"kind": KIND_CLOUD_IMAGE, "disk_format": "qcow2"}
    store.write_metadata(digest, meta)
    blob = store.blob_path(digest)
    metadata = store.metadata_path(digest)
    if damage == "missing":
        blob.unlink()
    elif damage == "unsupported":
        store.write_metadata(digest, {"kind": "other"})
    elif damage == "writable":
        blob.chmod(0o600)
    elif damage == "corrupt":
        blob.chmod(0o600)
        blob.write_bytes(b"changed bytes")
        blob.chmod(0o400)
    elif damage in {"symlink", "hardlink"}:
        external = tmp_path / "blob"
        blob.rename(external)
        if damage == "symlink":
            blob.symlink_to(external)
        else:
            os.link(external, blob)
    elif damage == "missing-metadata":
        metadata.unlink()
    elif damage == "metadata-symlink":
        external = tmp_path / "metadata.json"
        metadata.rename(external)
        metadata.symlink_to(external)
    else:
        payload = state.read_json(metadata)
        if damage == "wrong-digest":
            payload["digest"] = "sha256:" + "a" * 64
        elif damage == "bad-parent":
            payload["parent_digest"] = {"digest": digest}
        elif damage == "bad-format":
            payload["disk_format"] = ["qcow2"]
        state.atomic_write_json(metadata, payload)

    with pytest.raises(StateError):
        inventory.tag_artifact(roots, digest, "refused")
    assert not (roots.tags / "refused.json").exists()


def test_tag_artifact_retains_layer_parent_and_base(tmp_path):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = "sha256:" + store.write_stream([b"layer"]).name
    parent = "sha256:" + "a" * 64
    base = "sha256:" + "b" * 64
    store.write_metadata(
        digest,
        {
            "kind": "squashfs",
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
            "parent_digest": parent,
            "base_image_digest": base,
        },
    )
    binding = inventory.tag_artifact(roots, digest, "layer")
    assert binding["parent_digest"] == parent
    assert binding["base_image_digest"] == base
    assert binding["media_type"] == MEDIA_TYPE_LAYER_SQUASHFS
    with pytest.raises(StateError, match="invalid tag"):
        inventory.tag_artifact(roots, digest, "../escape")


@pytest.mark.parametrize("component", ["tags", "metadata"])
def test_tag_artifact_rejects_symlinked_index_directory(tmp_path, component):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = "sha256:" + store.write_stream([b"artifact"]).name
    store.write_metadata(digest, {"kind": "squashfs"})
    directory = roots.tags if component == "tags" else roots.store / "metadata"
    external = tmp_path / "external"
    directory.rename(external)
    directory.symlink_to(external, target_is_directory=True)
    with pytest.raises(StateError):
        inventory.tag_artifact(roots, digest, "unsafe")
    assert not (roots.tags / "unsafe.json").exists()


@pytest.mark.parametrize(
    "change", ["conflicting-tag", "removed-blob", "unsafe-blob", "tags-alias", "missing-metadata", "changed-metadata"]
)
def test_tag_artifact_rechecks_verified_authority_at_state_commit(tmp_path, monkeypatch, change):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = "sha256:" + store.write_stream([b"target"]).name
    other = "sha256:" + store.write_stream([b"concurrent target"]).name
    store.write_metadata(digest, {"kind": "squashfs"})
    real_writer = state._write_tag_record_locked

    def concurrent_restore(writer_roots, record, **authority):
        if change == "conflicting-tag":
            replacement = TagRecord(
                1,
                record.tag,
                other,
                MEDIA_TYPE_LAYER_SQUASHFS,
                len(b"concurrent target"),
                None,
                None,
                "build",
                "2026-08-24T00:00:00Z",
            )
            state.atomic_write_json(roots.tags / f"{record.tag}.json", dataclasses.asdict(replacement))
        elif change == "removed-blob":
            store.blob_path(digest).unlink()
        elif change == "unsafe-blob":
            store.blob_path(digest).unlink()
            store.blob_path(digest).write_bytes(b"unverified replacement")
        elif change == "tags-alias":
            original = tmp_path / "original-tags"
            roots.tags.rename(original)
            replacement = tmp_path / "replacement-tags"
            replacement.mkdir(mode=0o700)
            roots.tags.symlink_to(replacement, target_is_directory=True)
        elif change == "missing-metadata":
            store.metadata_path(digest).unlink()
        else:
            # Model an out-of-band restore, not a cooperating writer's digest lock.
            state.atomic_write_json(store.metadata_path(digest), {**store.read_metadata(digest), "kind": "other"})
        return real_writer(writer_roots, record, **authority)

    monkeypatch.setattr(state, "_write_tag_record_locked", concurrent_restore)
    with pytest.raises(StateError):
        inventory.tag_artifact(roots, digest, "race")
    if change == "conflicting-tag":
        assert state.read_tag_record(roots, "race").digest == other
    else:
        assert not (roots.tags / "race.json").exists()


def test_remove_artifact_refusal_and_success(tmp_path: Path):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)

    ref_digest = store.write_stream([b"referenced blob"]).name
    free_digest = store.write_stream([b"free blob"]).name
    ref_digest = f"sha256:{ref_digest}"
    free_digest = f"sha256:{free_digest}"

    (roots.store / "metadata").mkdir(parents=True, exist_ok=True)

    store.write_metadata(ref_digest, {"kind": KIND_CLOUD_IMAGE, "disk_format": "qcow2", "arch": "x86_64"})
    store.write_metadata(free_digest, {"kind": "squashfs", "media_type": MEDIA_TYPE_LAYER_SQUASHFS})

    write_tag_record(
        roots,
        TagRecord(
            schema_version=1,
            tag="free-tag",
            digest=free_digest,
            media_type=MEDIA_TYPE_LAYER_SQUASHFS,
            size_bytes=10,
            parent_digest=None,
            base_image_digest=None,
            source="build",
            created_at="2026-08-24T00:00:00Z",
        ),
    )

    run_dir = roots.runs / "ref-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        run_dir / "state.json",
        {"name": "ref-run", "base": {"digest": ref_digest}},
    )

    with pytest.raises(StateError, match="is still used by: ref-run"):
        inventory.remove_artifact(roots, ref_digest)

    with pytest.raises(StateError, match="is still used by: ref-run"):
        inventory.remove_artifact(roots, ref_digest, force=True)

    rem_res = inventory.remove_artifact(roots, free_digest)
    assert rem_res["digest"] == free_digest
    assert rem_res["removed_tags"] == ["free-tag"]
    assert not store.exists(free_digest)
    assert not (roots.tags / "free-tag.json").exists()


def test_remove_artifact_rejects_malformed_run_layer_shape(tmp_path: Path) -> None:
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'guarded layer']).name}"
    run_dir = roots.runs / "malformed-run"
    run_dir.mkdir()
    state.atomic_write_json(run_dir / "state.json", {"layers": {"digest": digest}})

    with pytest.raises(StateError, match="run ledger is invalid"):
        inventory.remove_artifact(roots, digest)

    assert store.exists(digest)


def test_remove_artifact_accepts_symlinked_state_root(tmp_path: Path) -> None:
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'symlinked state root']).name}"
    store.write_metadata(digest, {"kind": "other"})
    alias = tmp_path / "state-alias"
    try:
        alias.symlink_to(roots.state, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    result = inventory.remove_artifact(state.StatePaths(config=roots.config, state=alias), digest)

    assert result["digest"] == digest
    assert not store.exists(digest)
    assert not store.metadata_path(digest).exists()


def test_remove_artifact_rejects_tag_payload_path_traversal(tmp_path: Path) -> None:
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'tag traversal target']).name}"
    victim = roots.state.parent / "victim.json"
    victim.write_text("preserve me", encoding="utf-8")
    state.atomic_write_json(
        roots.tags / "alias.json",
        {
            "schema_version": 1,
            "tag": "../../victim",
            "digest": digest,
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
            "size_bytes": 20,
            "parent_digest": None,
            "base_image_digest": None,
            "source": "test",
            "created_at": "2026-08-31T00:00:00Z",
        },
    )

    with pytest.raises(StateError, match="tag record is invalid"):
        inventory.remove_artifact(roots, digest)

    assert victim.read_text(encoding="utf-8") == "preserve me"
    assert store.exists(digest)


@pytest.mark.parametrize("component", ["tags", "metadata"])
def test_remove_artifact_rejects_symlinked_index_directory(tmp_path: Path, component: str) -> None:
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'external index target']).name}"
    external = tmp_path / f"external-{component}"
    external.mkdir(mode=0o700)
    if component == "tags":
        roots.tags.rmdir()
        attacked = roots.tags
        external_entry = external / "outside.json"
    else:
        store.write_metadata(digest, {"kind": "other"})
        metadata = roots.store / "metadata"
        for entry in metadata.iterdir():
            entry.unlink()
        metadata.rmdir()
        attacked = metadata
        external_entry = external / f"{digest.split(':', 1)[1]}.json"
    external_entry.write_text("preserve me", encoding="utf-8")
    try:
        attacked.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(StateError, match="state directory authority"):
        inventory.remove_artifact(roots, digest)

    assert external_entry.read_text(encoding="utf-8") == "preserve me"
    assert store.exists(digest)


@pytest.mark.parametrize("mutation", ["replace", "in-place"])
def test_remove_artifact_rejects_mutated_tag_snapshot(tmp_path: Path, monkeypatch, mutation: str) -> None:
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'original tag target']).name}"
    replacement_digest = f"sha256:{store.write_stream([b'replacement tag target']).name}"
    write_tag_record(
        roots,
        TagRecord(
            schema_version=1,
            tag="race-tag",
            digest=digest,
            media_type=MEDIA_TYPE_LAYER_SQUASHFS,
            size_bytes=19,
            parent_digest=None,
            base_image_digest=None,
            source="test",
            created_at="2026-08-31T00:00:00Z",
        ),
    )
    delete_entered = threading.Event()
    allow_delete = threading.Event()
    failures: list[str] = []
    real_delete = inventory.ArtifactStore.delete_blob

    def paused_delete(self, *args, **kwargs):
        delete_entered.set()
        assert allow_delete.wait(2)
        return real_delete(self, *args, **kwargs)

    monkeypatch.setattr(inventory.ArtifactStore, "delete_blob", paused_delete)

    def remove() -> None:
        try:
            inventory.remove_artifact(roots, digest)
        except StateError as exc:
            failures.append(str(exc))

    remove_thread = threading.Thread(target=remove)
    remove_thread.start()
    assert delete_entered.wait(2)
    replacement = {
        "schema_version": 1,
        "tag": "race-tag",
        "digest": replacement_digest,
        "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
        "size_bytes": 23,
        "parent_digest": None,
        "base_image_digest": None,
        "source": "test",
        "created_at": "2026-08-31T00:00:01Z",
    }
    if mutation == "replace":
        state.atomic_write_json(roots.tags / "race-tag.json", replacement)
    else:
        (roots.tags / "race-tag.json").write_text(
            json.dumps(replacement, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    allow_delete.set()
    remove_thread.join(2)

    assert not remove_thread.is_alive()
    assert failures == ["tag record changed before removal"]
    assert store.exists(digest)
    assert state.read_tag_record(roots, "race-tag").digest == replacement_digest


@pytest.mark.parametrize("replacement_target", ["other", "same"])
def test_remove_artifact_reconciles_tag_replacement_during_finalize(
    tmp_path: Path, monkeypatch, replacement_target: str
) -> None:
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'multi-tag target']).name}"
    other_digest = f"sha256:{store.write_stream([b'late replacement']).name}"
    replacement_digest = other_digest if replacement_target == "other" else digest
    for tag in ("a-tag", "b-tag"):
        write_tag_record(
            roots,
            TagRecord(
                schema_version=1,
                tag=tag,
                digest=digest,
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                size_bytes=16,
                parent_digest=None,
                base_image_digest=None,
                source="test",
                created_at="2026-08-31T00:00:00Z",
            ),
        )
    real_unlink = inventory._unlink_index_entry

    def replace_later_tag(filename: str, *, directory_fd: int) -> None:
        real_unlink(filename, directory_fd=directory_fd)
        if filename == "a-tag.json":
            state.atomic_write_json(
                roots.tags / "b-tag.json",
                {
                    "schema_version": 1,
                    "tag": "b-tag",
                    "digest": replacement_digest,
                    "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                    "size_bytes": 16,
                    "parent_digest": None,
                    "base_image_digest": None,
                    "source": "test",
                    "created_at": "2026-08-31T00:00:01Z",
                },
            )

    monkeypatch.setattr(inventory, "_unlink_index_entry", replace_later_tag)

    result = inventory.remove_artifact(roots, digest)

    assert not store.exists(digest)
    if replacement_target == "other":
        assert result["removed_tags"] == ["a-tag"]
        assert state.read_tag_record(roots, "b-tag").digest == other_digest
    else:
        assert result["removed_tags"] == ["a-tag", "b-tag"]
        assert not state.tag_path(roots, "b-tag").exists()


def test_remove_artifact_serializes_late_run_reference_commit(tmp_path: Path, monkeypatch) -> None:
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'reference race']).name}"
    store.write_metadata(digest, {"kind": "other"})
    rpaths = state.run_paths(roots, "late-run")
    rpaths.root.mkdir()
    entered_delete = threading.Event()
    allow_delete = threading.Event()
    writer_started = threading.Event()
    outcomes: list[str] = []
    real_delete = inventory.ArtifactStore.delete_blob

    def paused_delete(self, *args, **kwargs):
        entered_delete.set()
        assert allow_delete.wait(2)
        return real_delete(self, *args, **kwargs)

    monkeypatch.setattr(inventory.ArtifactStore, "delete_blob", paused_delete)

    def remove() -> None:
        inventory.remove_artifact(roots, digest)

    def write_reference() -> None:
        writer_started.set()
        try:
            state.write_run_state(rpaths, status="running", data={"base_digest": digest})
        except StateError as exc:
            outcomes.append(str(exc))

    remove_thread = threading.Thread(target=remove)
    writer_thread = threading.Thread(target=write_reference)
    remove_thread.start()
    assert entered_delete.wait(2)
    writer_thread.start()
    assert writer_started.wait(2)
    allow_delete.set()
    remove_thread.join(2)
    writer_thread.join(2)

    assert not remove_thread.is_alive() and not writer_thread.is_alive()
    assert outcomes == ["run ledger references a missing artifact"]
    assert not rpaths.state.exists() and not store.exists(digest)


def test_remove_cleanup_is_serialized_with_same_digest_republish(tmp_path: Path, monkeypatch) -> None:
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    payload = b"same digest republish"
    digest = f"sha256:{store.write_stream([payload]).name}"
    store.write_metadata(digest, {"kind": "old"})
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    publish_started = threading.Event()
    real_fsync_index_directory = inventory._fsync_index_directory

    def paused_fsync(directory_fd: int) -> None:
        if not cleanup_entered.is_set():
            cleanup_entered.set()
            assert allow_cleanup.wait(2)
        real_fsync_index_directory(directory_fd)

    monkeypatch.setattr(inventory, "_fsync_index_directory", paused_fsync)

    def remove() -> None:
        inventory.remove_artifact(roots, digest)

    def republish() -> None:
        publish_started.set()
        store.write_stream([payload], expected_digest=digest)
        store.write_metadata(digest, {"kind": "new"})

    remove_thread = threading.Thread(target=remove)
    publish_thread = threading.Thread(target=republish)
    remove_thread.start()
    assert cleanup_entered.wait(2)
    publish_thread.start()
    assert publish_started.wait(2)
    allow_cleanup.set()
    remove_thread.join(2)
    publish_thread.join(2)

    assert not remove_thread.is_alive() and not publish_thread.is_alive()
    assert store.verify_blob(digest) == len(payload)
    assert store.read_metadata(digest)["kind"] == "new"


def test_list_builds_and_get_build_and_log(tmp_path: Path):
    roots = _setup_roots(tmp_path)

    # Build 1: Schema 1 (palimpsestfile)
    b1_id = "b-000000000001"
    b1_dir = roots.builds / b1_id
    b1_dir.mkdir(parents=True, exist_ok=True)
    (b1_dir / "console.log").write_text("line 1\nline 2\nline 3\nline 4\n", encoding="utf-8")
    state.atomic_write_json(
        b1_dir / "record.json",
        {
            "schema_version": 1,
            "build_id": b1_id,
            "status": "success",
            "created_at": "2026-08-24T10:00:00Z",
            "finished_at": "2026-08-24T10:00:42Z",
            "output_tag": "myimage:v1",
            "base_digest": "sha256:" + "a" * 64,
        },
    )

    # Build 2: Schema 2 (buildkit)
    b2_id = "bk-000000000002"
    b2_dir = roots.builds / b2_id
    b2_dir.mkdir(parents=True, exist_ok=True)
    (b2_dir / "buildkit.log").write_text("buildkit step 1\nbuildkit done\n", encoding="utf-8")
    (b2_dir / "console.log").write_text("not the BuildKit engine log\n", encoding="utf-8")
    state.atomic_write_json(
        b2_dir / "record.json",
        {
            "schema_version": 2,
            "engine": "buildkit",
            "build_id": b2_id,
            "status": "success",
            "finished_at": "2026-08-24T11:00:00Z",
            "output_tags": ["mybk:v1"],
            "timings_ms": {"total": 17250},
        },
    )

    builds = inventory.list_builds(roots)
    assert len(builds) == 2
    assert builds[0]["build_id"] == b2_id
    assert builds[0]["engine"] == "buildkit"
    assert builds[0]["duration_ms"] == 17250
    assert builds[0]["log_available"] is True
    assert inventory.build_log(roots, b2_id) == "buildkit step 1\nbuildkit done\n"
    assert inventory.build_log(roots, b2_id, tail=1) == "buildkit done\n"

    assert builds[1]["build_id"] == b1_id
    assert builds[1]["engine"] == "palimpsestfile"
    assert builds[1]["duration_ms"] == 42000

    b1_rec = inventory.get_build(roots, b1_id)
    assert b1_rec["output_tags"] == ["myimage:v1"]
    for build in (b1_rec, builds[0]):
        assert build["output_oci_manifest_digest"] is None
        assert build["output_oci_archive_digest"] is None
        assert build["runtime_block_digest"] is None

    with pytest.raises(StateError, match="invalid build id"):
        inventory.get_build(roots, "invalid")

    with pytest.raises(StateError, match="invalid build id"):
        inventory.get_build(roots, "../b-000000000001")

    with pytest.raises(StateError, match="invalid build id"):
        inventory.build_log(roots, "../b-000000000001")

    log_tail = inventory.build_log(roots, b1_id, tail=2)
    assert log_tail == "line 3\nline 4\n"


def test_import_cloud_image(tmp_path: Path):
    roots = _setup_roots(tmp_path)
    img_file = tmp_path / "test.qcow2"
    img_file.write_bytes(b"qcow2 image header and payload")

    res = inventory.import_cloud_image(roots, img_file, disk_format="qcow2", arch="aarch64", os_variant="ubuntu-24.04")
    assert res["digest"].startswith("sha256:")
    assert res["metadata"]["arch"] == "aarch64"

    store = ContentStore(roots.store)
    assert store.exists(res["digest"])

    missing_file = tmp_path / "missing.img"
    with pytest.raises(ArtifactValidationError, match="image path not found"):
        inventory.import_cloud_image(roots, missing_file, disk_format="qcow2", arch="x86_64")


def test_set_state_root(tmp_path: Path):
    roots = _setup_roots(tmp_path)
    target_dir = tmp_path / "new_state"
    target_dir.mkdir(parents=True, exist_ok=True)

    rel_path = Path("relative/path")
    with pytest.raises(StateError, match="must be an absolute path"):
        inventory.set_state_root(roots, rel_path)

    non_existent = tmp_path / "does_not_exist"
    with pytest.raises(StateError, match="does not exist"):
        inventory.set_state_root(roots, non_existent)

    non_empty = tmp_path / "non_empty"
    non_empty.mkdir()
    (non_empty / "file.txt").write_text("data")
    with pytest.raises(StateError, match="is not empty and lacks a store directory"):
        inventory.set_state_root(roots, non_empty)

    res = inventory.set_state_root(roots, target_dir)
    assert res["new_root"] == str(target_dir.resolve())


def test_move_state_root_preconditions_and_success(tmp_path: Path):
    roots = _setup_roots(tmp_path)

    rel_dest = Path("rel_dest")
    with pytest.raises(StateError, match="must be an absolute path"):
        inventory.move_state_root(roots, rel_dest)

    # Active run blocks move
    run_dir = roots.runs / "active-vm"
    run_dir.mkdir(parents=True, exist_ok=True)

    dest_dir = tmp_path / "target_state"
    with pytest.raises(
        StateError, match="relocating the state root requires no runs and no projects; remove them first: active-vm"
    ):
        inventory.move_state_root(roots, dest_dir)

    # Remove run and proceed
    run_dir.rmdir()

    res = inventory.move_state_root(roots, dest_dir, keep_source=False)
    assert res["new_root"] == str(dest_dir.resolve())
    assert dest_dir.is_dir()
    assert not roots.state.exists()


def test_move_state_root_failure_cleans_incoming_without_deleting_committed_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    roots = _setup_roots(tmp_path)
    dest_dir = tmp_path / "target_state_fail"

    def fake_copytree(src, dst, **kwargs):
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / "partial.txt").write_text("partial")
        raise RuntimeError("simulated copy failure")

    monkeypatch.setattr("shutil.copytree", fake_copytree)

    incoming = dest_dir.parent / f"{dest_dir.name}.incoming-{os.getpid()}"

    with pytest.raises(RuntimeError, match="simulated copy failure"):
        inventory.move_state_root(roots, dest_dir)

    assert not incoming.exists()
    assert roots.state.exists()


def test_set_and_move_state_root_rejected_when_env_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = _setup_roots(tmp_path)
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "env_override_state"))

    dest_set = tmp_path / "set_target"
    dest_set.mkdir()

    with pytest.raises(StateError) as exc_info_set:
        inventory.set_state_root(roots, dest_set)
    assert "PALIMPSEST_STATE_HOME" in str(exc_info_set.value)
    assert "unset" in str(exc_info_set.value)

    # Verify no state directories were initialized in dest_set
    assert not (dest_set / "store").exists()

    dest_move = tmp_path / "move_target"
    with pytest.raises(StateError) as exc_info_move:
        inventory.move_state_root(roots, dest_move)
    assert "PALIMPSEST_STATE_HOME" in str(exc_info_move.value)
    assert "unset" in str(exc_info_move.value)

    # Verify move target was not created and source state exists
    assert not dest_move.exists()
    assert roots.state.exists()


def test_list_vms_reconciles_kvm_and_hvf_separately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = _setup_roots(tmp_path)

    kvm_dir = roots.runs / "kvm-vm"
    kvm_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        kvm_dir / "owner.json",
        {"schema_version": 1, "run_id": "11111111-1111-1111-1111-111111111111", "name": "kvm-vm"},
    )
    state.atomic_write_json(
        kvm_dir / "state.json",
        {
            "name": "kvm-vm",
            "run_id": "11111111-1111-1111-1111-111111111111",
            "backend": "kvm",
            "status": "running",
            "base": {"arch": "x86_64"},
            "created_at": "2026-08-24T00:00:00Z",
        },
    )

    hvf_dir = roots.runs / "hvf-vm"
    hvf_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        hvf_dir / "owner.json",
        {"schema_version": 1, "run_id": "22222222-2222-2222-2222-222222222222", "name": "hvf-vm"},
    )
    state.atomic_write_json(
        hvf_dir / "state.json",
        {
            "name": "hvf-vm",
            "run_id": "22222222-2222-2222-2222-222222222222",
            "backend": "libvirt-hvf",
            "status": "running",
            "base": {"arch": "aarch64"},
            "created_at": "2026-08-24T00:00:00Z",
        },
    )

    captured_backends: list[str] = []

    def mock_reconcile_run(name, *, _expected_record, **_kwargs):
        captured_backends.append(_expected_record.dispatch_key.backend.value)
        return {"state": state.read_run_state(state.run_paths(roots, name)), "warnings": []}

    monkeypatch.setattr(inventory.runtime_dispatch.cloud_runtime, "reconcile_run", mock_reconcile_run)

    res = inventory.list_vms(roots)
    vms = {v["name"]: v for v in res["vms"]}

    assert captured_backends == ["libvirt-hvf", "kvm"]
    assert vms["kvm-vm"]["status"] == "running"
    assert vms["hvf-vm"]["status"] == "running"
    assert vms["kvm-vm"]["stale"] is False
    assert vms["hvf-vm"]["stale"] is False


def test_list_vms_reconcile_fallbacks_and_failure_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = _setup_roots(tmp_path)

    # KVM ledger with no base arch
    kvm_dir = roots.runs / "legacy-kvm"
    kvm_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        kvm_dir / "owner.json",
        {"schema_version": 1, "run_id": "11111111-1111-1111-1111-111111111111", "name": "legacy-kvm"},
    )
    state.atomic_write_json(
        kvm_dir / "state.json",
        {
            "name": "legacy-kvm",
            "run_id": "11111111-1111-1111-1111-111111111111",
            "backend": "kvm",
            "status": "running",
        },
    )

    # HVF ledger with no base arch
    hvf_dir = roots.runs / "legacy-hvf"
    hvf_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        hvf_dir / "owner.json",
        {"schema_version": 1, "run_id": "22222222-2222-2222-2222-222222222222", "name": "legacy-hvf"},
    )
    state.atomic_write_json(
        hvf_dir / "state.json",
        {
            "name": "legacy-hvf",
            "run_id": "22222222-2222-2222-2222-222222222222",
            "backend": "libvirt-hvf",
            "status": "running",
        },
    )

    captured_backends: list[str] = []

    def mock_reconcile_run(name, *, _expected_record, **_kwargs):
        backend = _expected_record.dispatch_key.backend.value
        captured_backends.append(backend)
        if backend == "libvirt-hvf":
            raise RuntimeError("sensitive backend failure")
        return {"state": state.read_run_state(state.run_paths(roots, name)), "warnings": []}

    monkeypatch.setattr(inventory.runtime_dispatch.cloud_runtime, "reconcile_run", mock_reconcile_run)

    res = inventory.list_vms(roots)

    assert captured_backends == ["libvirt-hvf", "kvm"]
    assert any("runtime reconciliation failed" in w for w in res["warnings"])
    assert all("sensitive" not in warning for warning in res["warnings"])
    vms = {v["name"]: v for v in res["vms"]}
    assert vms["legacy-hvf"]["stale"] is True
    assert vms["legacy-kvm"]["stale"] is False


def _resource_run(roots, name, *, backend="lima-vz", runtime_kind="cloud-image", **fields):
    import uuid

    run_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "inventory-test:" + name))
    run = roots.runs / name
    run.mkdir(mode=0o700)
    state.atomic_write_json(run / "owner.json", {"schema_version": 1, "name": name, "run_id": run_id})
    state.atomic_write_json(
        run / "state.json",
        {
            "schema_version": 2,
            "runtime_kind": runtime_kind,
            "backend": backend,
            "name": name,
            "run_id": run_id,
            "status": "stopped",
            **fields,
        },
    )
    return run_id


def _resource_project(roots, name, *, service=None, run_id=None, backend="lima-vz"):
    from palimpsest_local.project import deterministic_service_name

    services = (
        []
        if service is None
        else [
            {
                "service": service,
                "run_name": deterministic_service_name(name, service),
                "run_id": run_id,
                "backend": backend,
                "config_digest": "sha256:" + "a" * 64,
            }
        ]
    )
    project = roots.projects / name
    project.mkdir(mode=0o700)
    state.atomic_write_json(
        project / "state.json",
        {
            "schema_version": 2,
            "project": name,
            "config_digest": "sha256:" + "a" * 64,
            "services": services,
            "order": [] if service is None else [service],
            "volumes": [{"name": "data", "backend": backend, "size_bytes": 32 * 1024**2}],
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
        },
    )
    return project / "state.json"


def test_resource_volumes_keep_project_and_standalone_identity_boundaries(tmp_path):
    from palimpsest_local.project import deterministic_service_name

    roots = _setup_roots(tmp_path)
    mount = {
        "name": "data",
        "mount_path": "/srv/data",
        "read_only": False,
        "host_path": "/private/SENSITIVE_VOLUME_PATH/data.raw",
    }
    for project in ("alpha", "beta"):
        name = deterministic_service_name(project, "web")
        run_id = _resource_run(roots, name, volumes=[mount])
        _resource_project(roots, project, service="web", run_id=run_id)
    _resource_project(roots, "preserved")
    first = _resource_run(roots, "standalone-one", volumes=[mount])
    second = _resource_run(roots, "standalone-two", volumes=[mount])
    supplied = inventory.list_vms(roots, live=False)
    before = json.dumps(supplied, sort_keys=True)

    result = inventory.list_volumes(roots, vms_result=supplied)

    rows = {row["id"]: row for row in result["volumes"] if row["kind"] == "project"}
    assert list(rows) == sorted(rows)
    assert set(rows) == {
        "project:alpha:lima-vz:data",
        "project:beta:lima-vz:data",
        "project:preserved:lima-vz:data",
        f"run-volume:{first}:data",
        f"run-volume:{second}:data",
    }
    for project in ("alpha", "beta"):
        row = rows[f"project:{project}:lima-vz:data"]
        assert row["project"] == project and row["size_bytes"] == 32 * 1024**2
        assert row["source"] == "project-ledger" and row["status"] == "declared-attached"
        assert [item["name"] for item in row["attachments"]] == [deterministic_service_name(project, "web")]
    preserved = rows["project:preserved:lima-vz:data"]
    assert preserved["attachments"] == [] and preserved["status"] == "declared"
    for run_id in (first, second):
        row = rows[f"run-volume:{run_id}:data"]
        assert row["project"] is None and row["size_bytes"] is None
        assert row["attachments"][0]["run_id"] == run_id
    assert "SENSITIVE_VOLUME_PATH" not in repr(result)
    assert json.dumps(supplied, sort_keys=True) == before


def test_resource_volumes_do_not_attach_replaced_project_run(tmp_path):
    from palimpsest_local.project import deterministic_service_name

    roots = _setup_roots(tmp_path)
    name = deterministic_service_name("alpha", "web")
    run_id = _resource_run(roots, name, volumes=[{"name": "data", "mount_path": "/data", "read_only": True}])
    _resource_project(roots, "alpha", service="web", run_id="11111111-1111-4111-8111-111111111111")

    result = inventory.list_volumes(roots)

    rows = {row["id"]: row for row in result["volumes"]}
    assert rows["project:alpha:lima-vz:data"]["attachments"] == []
    assert rows[f"run-volume:{run_id}:data"]["project"] is None
    assert rows[f"run-volume:{run_id}:data"]["attachments"][0]["read_only"] is True


def test_resource_volumes_include_retained_unattached_oci_roots(tmp_path):
    from palimpsest_local import oci_root_volume

    roots = _setup_roots(tmp_path)
    for volume_id, status, run_id, run_name in (
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "retained", None, None),
        ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "attached", "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "absent-run"),
    ):
        record = oci_root_volume.OCIRootVolumeRecord(
            volume_id,
            16 * 1024**2,
            "sha256:" + "a" * 64,
            "retain",
            status,
            run_id,
            run_name,
            3,
        )
        stem = volume_id.replace("-", "")
        (roots.oci_root_volumes / f"{stem}.raw").touch(mode=0o600)
        state.atomic_write_json(roots.oci_root_volumes / f"{stem}.json", record.to_dict())

    result = inventory.list_volumes(roots)

    retained, attached = result["volumes"]
    assert retained["id"] == "oci-root:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert retained["status"] == "retained" and retained["attachments"] == []
    assert retained["size_bytes"] == 16 * 1024**2 and retained["retention_policy"] == "retain"
    assert attached["status"] == "attached" and attached["source"] == "oci-root-volume-ledger"
    assert attached["attachments"] == [
        {"name": "absent-run", "run_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "mount_path": "/", "read_only": False}
    ]
    assert result["warnings"] == []


@pytest.mark.parametrize("damage", ["corrupt", "symlink-file", "symlink-directory", "owner-mismatch"])
def test_resource_project_metadata_refusals_are_fixed_and_path_free(tmp_path, damage):
    roots = _setup_roots(tmp_path)
    ledger = _resource_project(roots, "invalid")
    _resource_project(roots, "valid")
    sensitive = tmp_path / "SENSITIVE_VALUE"
    if damage == "corrupt":
        ledger.write_text('{"host_path":"/private/SENSITIVE_VALUE",', encoding="utf-8")
    elif damage == "owner-mismatch":
        payload = json.loads(ledger.read_text())
        payload["project"] = "wrong-project"
        state.atomic_write_json(ledger, payload)
    elif damage == "symlink-file":
        ledger.rename(sensitive)
        ledger.symlink_to(sensitive)
    else:
        ledger.parent.rename(sensitive)
        ledger.parent.symlink_to(sensitive, target_is_directory=True)

    result = inventory.list_volumes(roots, vms_result={"vms": [], "warnings": []})

    assert [row["id"] for row in result["volumes"]] == ["project:valid:lima-vz:data"]
    assert result["warnings"] == ["Project volume metadata is unavailable or inconsistent"]
    assert "SENSITIVE_VALUE" not in repr(result) and str(tmp_path) not in repr(result)


@pytest.mark.parametrize("damage", ["corrupt", "symlink"])
def test_resource_oci_metadata_failure_is_not_a_clean_empty_inventory(tmp_path, damage):
    roots = _setup_roots(tmp_path)
    ledger = roots.oci_root_volumes / ("a" * 32 + ".json")
    if damage == "corrupt":
        ledger.write_text('{"private_path":"/private/SENSITIVE_VALUE",', encoding="utf-8")
    else:
        sensitive = tmp_path / "SENSITIVE_VALUE"
        sensitive.write_text("{}", encoding="utf-8")
        ledger.symlink_to(sensitive)

    result = inventory.list_volumes(roots)

    assert result["volumes"] == []
    assert result["warnings"] == ["OCI root-volume metadata is unavailable or inconsistent"]
    assert "SENSITIVE_VALUE" not in repr(result) and str(tmp_path) not in repr(result)


def test_resource_vm_disk_observation_is_not_virtual_capacity(tmp_path):
    roots = _setup_roots(tmp_path)
    observed_id = _resource_run(roots, "observed", backend="kvm")
    rejected_id = _resource_run(roots, "rejected", backend="kvm")
    external = tmp_path / "SENSITIVE_DISK"
    external.write_bytes(b"outside-authority")
    (roots.runs / "observed" / "overlay.qcow2").write_bytes(b"fixed-managed-file")
    (roots.runs / "rejected" / "overlay.qcow2").symlink_to(external)

    result = inventory.list_volumes(roots)

    rows = {row["id"]: row for row in result["volumes"]}
    observed = rows[f"vm-disk:{observed_id}"]
    assert observed["status"] == "observed-file" and observed["source"] == "managed-overlay-stat"
    assert observed["size_bytes"] is None and observed["file_size_bytes"] == len(b"fixed-managed-file")
    rejected = rows[f"vm-disk:{rejected_id}"]
    assert rejected["status"] == "unavailable" and rejected["size_bytes"] is None
    assert rejected["file_size_bytes"] is None
    assert result["warnings"] == ["Some managed VM disk observations are unavailable or inconsistent"]
    assert "SENSITIVE_DISK" not in repr(result)


def test_resource_vm_disk_written_during_observation_stays_observed(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    run_id = _resource_run(roots, "busy", backend="kvm")
    overlay = roots.runs / "busy" / "overlay.qcow2"
    overlay.write_bytes(b"guest-disk")
    read_payloads = inventory.state._read_pinned_run_payloads

    def guest_writes_between_stats(*args, **kwargs):
        with overlay.open("ab") as stream:
            stream.write(b"-more-guest-data")
        return read_payloads(*args, **kwargs)

    monkeypatch.setattr(inventory.state, "_read_pinned_run_payloads", guest_writes_between_stats)

    result = inventory.list_volumes(roots)

    row = {row["id"]: row for row in result["volumes"]}[f"vm-disk:{run_id}"]
    assert row["status"] == "observed-file"
    assert row["file_size_bytes"] == overlay.stat().st_size
    assert result["warnings"] == []


def test_resource_networks_share_only_conventional_networks_and_keep_unknown_rows(tmp_path):
    roots = _setup_roots(tmp_path)
    port = {"host_ip": "127.0.0.1", "host_port": 18080, "guest_port": 80, "protocol": "tcp"}
    _resource_run(roots, "lima-one", network="default", ports=[port], guest_ip="192.168.5.2")
    _resource_run(roots, "lima-two", network="vzNAT", ports=[], guest_ip="192.168.5.3")
    _resource_run(roots, "kvm-one", backend="kvm", network="default")
    _resource_run(roots, "kvm-two", backend="kvm", network="default")
    no_nic = _resource_run(roots, "no-nic", backend="kvm", network="none")
    missing = _resource_run(roots, "missing", backend="kvm")
    malformed = _resource_run(roots, "malformed", backend="kvm", network="/private/SENSITIVE_VALUE")
    supplied = inventory.list_vms(roots, live=False)
    before = json.dumps(supplied, sort_keys=True)

    result = inventory.list_networks(roots, vms_result=supplied)

    rows = {row["id"]: row for row in result["networks"]}
    assert list(rows) == sorted(rows)
    lima = rows["network:lima-vz:vzNAT"]
    assert lima["kind"] == "lima" and lima["status"] == "configured"
    assert lima["subnet"] is None and lima["gateway"] is None and lima["external"] is None
    assert [attachment["name"] for attachment in lima["attachments"]] == ["lima-one", "lima-two"]
    assert lima["attachments"][0]["ports"] == [port]
    # Lima records the first global guest address (user-mode eth0), which is not an address on vzNAT.
    assert [attachment["guest_ip"] for attachment in lima["attachments"]] == [None, None]
    assert [attachment["name"] for attachment in rows["network:kvm:default"]["attachments"]] == ["kvm-one", "kvm-two"]
    assert rows[f"network:kvm:{no_nic}"]["status"] == "isolated"
    for run_id in (missing, malformed):
        row = rows[f"network-unavailable:{run_id}"]
        assert row["status"] == "unavailable" and row["attachments"] == [] and row["external"] is None
    assert result["warnings"] == [
        "Some VM resource metadata is unavailable or inconsistent",
        "Some configured network metadata is unavailable or inconsistent",
    ]
    assert "SENSITIVE_VALUE" not in repr(result)
    assert json.dumps(supplied, sort_keys=True) == before


@pytest.mark.parametrize(
    "plan", [None, {"digest": "sha256:" + "a" * 64, "plan": {"host_path": "/private/SENSITIVE_VALUE"}}]
)
def test_resource_oci_network_plan_refusal_never_becomes_empty_exposure(tmp_path, plan):
    roots = _setup_roots(tmp_path)
    fields = {} if plan is None else {"oci_root_domain": plan}
    run_id = _resource_run(roots, "oci-invalid", backend="kvm", runtime_kind="oci-root", **fields)

    result = inventory.list_networks(roots)

    assert result["networks"] == [
        {
            "id": f"oci-network:{run_id}",
            "name": "oci-invalid",
            "kind": "oci-root",
            "backend": "kvm",
            "mode": None,
            "subnet": None,
            "gateway": None,
            "status": "unavailable",
            "source": "committed-domain-plan",
            "external": None,
            "attachments": [],
        }
    ]
    assert (
        "OCI network status is unavailable; the exact run has no committed domain plan or its plan is invalid"
        in result["warnings"]
    )
    assert "SENSITIVE_VALUE" not in repr(result) and str(tmp_path) not in repr(result)


def test_resource_oci_networks_keep_equal_subnets_per_vm_and_exact_exposure(tmp_path):
    import test_oci_store as oci_fixtures

    from palimpsest_local.oci_network import OCINetworkConfig

    roots, store, tools, boot, profile, _prepared, first_plan = oci_fixtures._committed_oci_domain(
        tmp_path,
        "oci-first",
        network=OCINetworkConfig.resolve("nat", ["127.0.0.1:18080:80"]),
    )
    plans = [first_plan]
    for name, network in (
        ("oci-second", OCINetworkConfig.resolve("nat", ["0.0.0.0:18443:443/udp"])),
        ("oci-none", OCINetworkConfig.resolve("none")),
    ):
        with state.reserve_new_run(roots, name, oci_fixtures._oci_dispatch()) as reservation:
            prepared = oci_fixtures.prepare_oci_root_run(
                reservation,
                oci_fixtures._image_materialization(store),
                store,
                root_volume_size_bytes=oci_fixtures._ROOT_VOLUME_SIZE,
                runner=tools,
            )
        preview = oci_fixtures.build_oci_root_domain_plan(
            roots,
            prepared,
            store,
            boot,
            profile,
            network=network,
            runner=tools,
        )
        plans.append(oci_fixtures.commit_oci_root_domain_plan(roots, preview, store, runner=tools))
    ledgers_before = {plan.run_name: (roots.runs / plan.run_name / "state.json").read_bytes() for plan in plans}

    result = inventory.list_networks(roots)

    rows = {row["name"]: row for row in result["networks"]}
    assert set(rows) == {"oci-first", "oci-second", "oci-none"}
    assert rows["oci-first"]["id"] != rows["oci-second"]["id"]
    assert rows["oci-first"]["subnet"] == rows["oci-second"]["subnet"] == "10.0.2.0/24"
    assert rows["oci-first"]["gateway"] == "10.0.2.2"
    assert rows["oci-first"]["external"] is False and rows["oci-second"]["external"] is True
    assert rows["oci-first"]["attachments"][0]["ports"] == [
        {"host_ip": "127.0.0.1", "host_port": 18080, "guest_port": 80, "protocol": "tcp"}
    ]
    assert rows["oci-second"]["attachments"][0]["ports"] == [
        {"host_ip": "0.0.0.0", "host_port": 18443, "guest_port": 443, "protocol": "udp"}
    ]
    assert rows["oci-none"]["mode"] == "none" and rows["oci-none"]["status"] == "isolated"
    assert rows["oci-none"]["attachments"][0]["guest_ip"] is None
    assert rows["oci-none"]["attachments"][0]["ports"] == []
    assert result["warnings"] == []
    for plan in plans:
        assert rows[plan.run_name]["attachments"][0]["run_id"] == plan.run_id
        assert (roots.runs / plan.run_name / "state.json").read_bytes() == ledgers_before[plan.run_name]


@pytest.mark.parametrize("damage", ["corrupt", "symlink"])
def test_resource_run_metadata_refusals_are_visible_and_path_free(tmp_path, damage):
    roots = _setup_roots(tmp_path)
    _resource_run(roots, "invalid", network="default", volumes=[{"name": "data"}])
    ledger = roots.runs / "invalid" / "state.json"
    if damage == "corrupt":
        ledger.write_text('{"private_path":"/private/SENSITIVE_VALUE",', encoding="utf-8")
    else:
        sensitive = tmp_path / "SENSITIVE_VALUE"
        ledger.rename(sensitive)
        ledger.symlink_to(sensitive)

    volumes = inventory.list_volumes(roots)
    networks = inventory.list_networks(roots)

    assert volumes["volumes"] == [] and networks["networks"] == []
    assert volumes["warnings"] == ["Some VM resource metadata is unavailable or inconsistent"]
    assert networks["warnings"] == ["Some VM resource metadata is unavailable or inconsistent"]
    assert "SENSITIVE_VALUE" not in repr((volumes, networks))
    assert str(tmp_path) not in repr((volumes, networks))


def test_resource_hvf_user_networks_are_not_shared_by_conventional_name(tmp_path):
    roots = _setup_roots(tmp_path)
    ssh = {"host": "127.0.0.1", "port": 60022}
    first = _resource_run(roots, "hvf-first", backend="libvirt-hvf", network="default", ssh=ssh)
    second = _resource_run(roots, "hvf-second", backend="libvirt-hvf", network="default", ssh=ssh)

    result = inventory.list_networks(roots)

    rows = {row["id"]: row for row in result["networks"]}
    assert set(rows) == {f"network:libvirt-hvf:{first}", f"network:libvirt-hvf:{second}"}
    assert rows[f"network:libvirt-hvf:{first}"]["kind"] == "user-hostfwd"
    assert rows[f"network:libvirt-hvf:{first}"]["attachments"][0]["name"] == "hvf-first"
    assert rows[f"network:libvirt-hvf:{second}"]["attachments"][0]["name"] == "hvf-second"
    # The host-loopback SSH forward is not a guest address on the user-mode network.
    assert [row["attachments"][0]["guest_ip"] for row in rows.values()] == [None, None]
    assert [vm["guest_ip"] for vm in inventory.list_vms(roots, live=False)["vms"]] == [None, None]
    assert result["warnings"] == []


def _observed_vm(name, run_id=None, *, status="running", backend="lima-vz"):
    return {
        "id": f"host:{backend}:{name}",
        "name": name,
        "run_id": run_id,
        "backend": backend,
        "runtime_kind": "cloud-image" if run_id else "external",
        "status": status,
        "managed": False,
        "source": "host-backend",
        "memory_mib": 2048,
        "vcpus": 2,
        "ssh": {"host": "127.0.0.1", "port": 2222},
        "guest_ip": None,
        "ports": [],
        "layers": [],
        "layer_count": 0,
        "volumes": [],
    }


def _live_ledger_projection(*, roots):
    result = inventory.runtime_dispatch.ps(roots=roots)
    return dataclasses.replace(
        result, summaries=tuple(dataclasses.replace(row, stale=False) for row in result.summaries)
    )


def test_host_vms_are_visible_without_adopting_external_state(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    snapshot = {"vms": [_observed_vm("external")], "volumes": [], "networks": [], "warnings": []}
    monkeypatch.setattr(inventory.host_inventory, "observe", lambda: snapshot)
    result = inventory.list_vms(roots)
    assert [(row["name"], row["status"], row["managed"]) for row in result["vms"]] == [("external", "running", False)]
    assert list(roots.runs.iterdir()) == []
    assert inventory.get_vm(roots, "external")["id"] == "host:lima-vz:external"
    monkeypatch.setattr(inventory.host_inventory, "observe", lambda: pytest.fail("ledger-only query touched host"))
    assert inventory.list_vms(roots, live=False)["vms"] == []


def test_host_same_name_different_run_id_is_not_managed(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    _resource_run(roots, "collision")
    external = _observed_vm("collision", "33333333-3333-3333-3333-333333333333")
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", inventory.runtime_dispatch.ps)
    monkeypatch.setattr(
        inventory.host_inventory, "observe", lambda: {"vms": [external], "volumes": [], "networks": [], "warnings": []}
    )
    result = inventory.list_vms(roots)["vms"]
    assert len(result) == 2
    assert {row["id"] for row in result} == {"collision", "host:lima-vz:collision"}
    assert all(row["managed"] is False for row in result)
    with pytest.raises(StateError, match="ambiguous"):
        inventory.get_vm(roots, "collision")
    assert inventory.get_vm(roots, "host:lima-vz:collision")["run_id"] == external["run_id"]
    external["removal_identity"] = "native-identity"
    fresh = {row["id"]: row for row in inventory.list_vms(roots)["vms"]}
    assert fresh[external["id"]]["lifecycle"]["remove"] is True
    assert fresh["collision"]["lifecycle"]["remove"] is False
    assert all(row["access"]["reason"] == "identity" for row in result)
    assert inventory.vm_metrics(roots, external["id"], expected_run_id=external["run_id"])["reason"] == "identity"
    assert inventory.vm_logs(roots, external["id"], expected_run_id=external["run_id"])["reason"] == "identity"


@pytest.mark.parametrize("backend", ["lima-vz", "lima-krunkit"])
def test_host_exact_identity_updates_runtime_and_disk_without_duplicate(tmp_path, monkeypatch, backend):
    roots = _setup_roots(tmp_path)
    run_id = _resource_run(roots, "owned", backend=backend, network="default" if backend == "lima-krunkit" else "vzNAT")
    vm = _observed_vm("owned", run_id, backend=backend)
    attachment = {"name": "owned", "run_id": run_id, "mount_path": "/", "read_only": False}
    disk = {
        "id": f"host-disk:{backend}:owned",
        "name": "owned",
        "kind": "vm-disk",
        "backend": backend,
        "status": "observed-file",
        "source": "host-backend",
        "size_bytes": 30 * 1024**3,
        "file_size_bytes": 12345,
        "allocated_bytes": 4096,
        "attachments": [attachment],
    }
    network = {
        "id": f"network:lima-krunkit:{run_id}:owned" if backend == "lima-krunkit" else "host-network:lima-vz:vzNAT",
        "name": "default" if backend == "lima-krunkit" else "vzNAT",
        "kind": "user-mode" if backend == "lima-krunkit" else "lima",
        "backend": backend,
        "mode": "gvisor" if backend == "lima-krunkit" else "vzNAT",
        "subnet": None,
        "gateway": None,
        "status": "configured",
        "source": "host-backend",
        "attachments": [{"name": "owned", "run_id": run_id, "guest_ip": None, "ports": []}],
    }
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", _live_ledger_projection)
    monkeypatch.setattr(
        inventory.host_inventory,
        "observe",
        lambda: {"vms": [vm], "volumes": [disk], "networks": [network], "warnings": []},
    )
    vms = inventory.list_vms(roots)["vms"]
    assert [(row["id"], row["status"], row["managed"]) for row in vms] == [("owned", "running", True)]
    assert vms[0]["run_id"] == run_id
    assert vms[0]["access"]["transport"] == "lima"
    assert vms[0]["lifecycle"]["stop"] is True
    volumes = inventory.list_volumes(roots)["volumes"]
    assert len(volumes) == 1
    assert volumes[0]["id"] == f"vm-disk:{run_id}"
    assert (volumes[0]["size_bytes"], volumes[0]["file_size_bytes"], volumes[0]["allocated_bytes"]) == (
        30 * 1024**3,
        12345,
        4096,
    )
    networks = inventory.list_networks(roots)["networks"]
    assert len(networks) == 1
    assert networks[0]["attachments"] == network["attachments"]


def test_libvirt_host_refresh_preserves_recorded_published_endpoints(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    port = {"host_ip": "127.0.0.1", "host_port": 18080, "guest_port": 8080, "protocol": "tcp"}
    run_id = _resource_run(
        roots,
        "native",
        backend="kvm",
        network="default",
        ports=[port],
        ssh={"host": "192.168.122.10", "port": 22},
        guest_ip="192.168.122.10",
    )
    host_vm = _observed_vm("native", run_id, backend="kvm")
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", _live_ledger_projection)
    monkeypatch.setattr(
        inventory.host_inventory, "observe", lambda: {"vms": [host_vm], "volumes": [], "networks": [], "warnings": []}
    )
    vm = inventory.list_vms(roots)["vms"][0]
    assert vm["status"] == "running"
    assert vm["ports"] == [port]
    assert vm["ssh"] == {"host": "192.168.122.10", "port": 22}
    assert vm["guest_ip"] == "192.168.122.10"


def test_failed_live_ownership_reconcile_disables_managed_controls(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    _resource_run(roots, "unverified")
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", inventory.runtime_dispatch.ps)
    vm = inventory.list_vms(roots)["vms"][0]
    assert vm["name"] == "unverified" and vm["stale"] is True and vm["managed"] is False
    assert inventory.list_vms(roots, live=False)["vms"][0]["managed"] is True


def test_host_network_named_none_does_not_merge_isolated_vm(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    isolated_id = _resource_run(roots, "isolated", backend="kvm", network="none")
    network = {
        "id": "network:kvm:none",
        "name": "none",
        "backend": "kvm",
        "kind": "libvirt",
        "status": "active",
        "mode": "nat",
        "source": "host-backend",
        "subnet": None,
        "gateway": None,
        "attachments": [{"name": "external", "run_id": None, "guest_ip": None, "ports": []}],
    }
    monkeypatch.setattr(
        inventory.host_inventory, "observe", lambda: {"vms": [], "volumes": [], "networks": [network], "warnings": []}
    )
    rows = {row["id"]: row for row in inventory.list_networks(roots)["networks"]}
    assert rows[f"network:kvm:{isolated_id}"]["status"] == "isolated"
    assert [item["name"] for item in rows[f"network:kvm:{isolated_id}"]["attachments"]] == ["isolated"]
    assert rows["network:kvm:none"]["mode"] == "nat"
    assert [item["name"] for item in rows["network:kvm:none"]["attachments"]] == ["external"]


_LINUX_METRICS = b"""@@stat
cpu  10 2 3 40 5 1 2 0 0 0
@@loadavg
0.25 0.50 0.75 1/30 42
@@meminfo
MemTotal: 1024 kB
MemAvailable: 256 kB
@@uptime
100.5 80.0
@@sys/kernel/random/boot_id
11111111-1111-4111-8111-111111111111
@@net/dev
Inter-| Receive | Transmit
 face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed
  eth0: 123 1 0 0 0 0 0 0 456 2 0 0 0 0 0 0
@@diskstats
   8 0 vda 1 0 2 3 4 0 5 6 0 7 8
@@df
Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/vda 1000 400 600 40% /
@@end
"""


def _vm_access_fixture(
    tmp_path, monkeypatch, *, status="running", backend="lima-vz", run_id="11111111-1111-4111-8111-111111111111"
):
    from palimpsest_local import vm_access

    row = _observed_vm("redis", run_id, status=status, backend=backend)
    row["access"] = vm_access.capabilities(row)
    roots = _setup_roots(tmp_path)
    monkeypatch.setattr(inventory, "get_vm", lambda roots, identifier: row)
    directory = tmp_path / "redis"
    directory.mkdir()
    (tmp_path / "guest-environment").write_text(f"PALIMPSEST_RUN_ID={run_id}\n")
    instance = {
        "name": "redis",
        "status": "Running" if status == "running" else "Stopped",
        "dir": str(directory),
        "vmType": "krunkit" if backend == "lima-krunkit" else "vz",
        "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
    }
    monkeypatch.setattr(vm_access, "_instance", lambda name: instance)
    return roots, row, directory, instance


def _spawn_lima_guest(tmp_path, argv, code, **kw):
    import shlex
    import sys

    from palimpsest_local.process_session import LocalProcessSession

    gate = argv[6].replace("/etc/environment", shlex.quote(str(tmp_path / "guest-environment")))
    return LocalProcessSession(("sh", "-c", gate, "sh", sys.executable, "-c", code), tty=kw["tty"], stdin=True)


@pytest.mark.parametrize("backend", ["lima-vz", "lima-krunkit"])
def test_guest_measurements_cross_real_process_consumer_boundary(tmp_path, monkeypatch, backend):
    from palimpsest_local import vm_access

    roots, row, _, _ = _vm_access_fixture(tmp_path, monkeypatch, backend=backend)

    def spawn(argv, **kw):
        return _spawn_lima_guest(tmp_path, argv, f"import os; os.write(1, {_LINUX_METRICS!r})", **kw)

    monkeypatch.setattr(vm_access, "spawn_process_session", spawn)
    result = inventory.vm_metrics(roots, row["id"], expected_run_id=row["run_id"])
    assert result["status"] == "available"
    assert result["cpu"]["total_ticks"] == 63
    assert result["cpu"]["idle_ticks"] == 45
    assert result["load"] == {"one": 0.25, "five": 0.5, "fifteen": 0.75}
    assert result["memory"] == {"total_bytes": 1048576, "available_bytes": 262144, "used_bytes": 786432}
    assert result["filesystems"][0]["free_bytes"] == 614400
    assert result["interfaces"] == [{"name": "eth0", "rx_bytes": 123, "tx_bytes": 456}]
    assert result["block_devices"][0]["read_bytes"] == 1024
    assert result["block_devices"][0]["write_bytes"] == 2560
    assert result["uptime_seconds"] == 100.5
    assert result["measured_at"]
    assert list(roots.runs.iterdir()) == []


@pytest.mark.parametrize(
    "source,bad",
    [
        ("stat", "cpu -1 2 3 4 5 6 7 8"),
        ("loadavg", "nan 2 3"),
        ("loadavg", "1"),
        ("meminfo", "MemTotal: 2 kB"),
        ("df", "UNAVAILABLE"),
        ("net/dev", "bad header\nbad header\neth0: 1"),
        ("diskstats", "8 0 vda -1"),
        ("uptime", "inf"),
        ("sys/kernel/random/boot_id", "bad"),
    ],
)
def test_guest_measurement_unavailable_never_becomes_zero(source, bad):
    import re

    from palimpsest_local import vm_access

    damaged = re.sub(
        rb"@@" + source.encode() + rb"\n.*?(?=@@)",
        b"@@" + source.encode() + b"\n" + bad.encode() + b"\n",
        _LINUX_METRICS,
        flags=re.S,
    )
    parsed = vm_access.parse_metrics(damaged)
    assert source in parsed["unavailable"]
    key = {
        "stat": "cpu",
        "loadavg": "load",
        "meminfo": "memory",
        "df": "filesystems",
        "net/dev": "interfaces",
        "diskstats": "block_devices",
        "uptime": "uptime_seconds",
        "sys/kernel/random/boot_id": "boot_id",
    }[source]
    assert parsed[key] is None


@pytest.mark.parametrize("backend", ["lima-vz", "lima-krunkit"])
def test_stopped_lima_logs_are_native_identity_bound_console(tmp_path, monkeypatch, backend):
    roots, row, directory, _ = _vm_access_fixture(tmp_path, monkeypatch, status="stopped", backend=backend)
    (directory / "serial.log").write_text("old\nredis ready\nshutdown\n")
    result = inventory.vm_logs(roots, row["id"], tail=2, expected_run_id=row["run_id"])
    assert result == {
        "status": "available",
        "source": "lima-serial-console",
        "log": "redis ready\nshutdown",
        "truncated": True,
    }
    assert inventory.vm_metrics(roots, row["id"], expected_run_id=row["run_id"])["reason"] == "stopped"
    (directory / "serial.log").unlink()
    (directory / "serial.log").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("hidden credential")
    denied = inventory.vm_logs(roots, row["id"], expected_run_id=row["run_id"])
    assert denied["status"] == "unavailable"
    assert "hidden credential" not in repr(denied)


def test_running_guest_journal_failure_is_explicit(tmp_path, monkeypatch):
    from palimpsest_local import vm_access

    roots, row, _, _ = _vm_access_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        vm_access,
        "spawn_process_session",
        lambda argv, **kw: _spawn_lima_guest(
            tmp_path, argv, "import os; os.write(2, b'private credential'); raise SystemExit(1)", **kw
        ),
    )
    result = inventory.vm_logs(roots, row["id"], expected_run_id=row["run_id"])
    assert result["status"] == "unavailable"
    assert result["reason"] == "probe"
    assert "private credential" not in repr(result)


def test_external_libvirt_credentials_are_not_inferred_from_endpoint(tmp_path, monkeypatch):
    roots, row, _, _ = _vm_access_fixture(tmp_path, monkeypatch, backend="hvf")
    assert row["access"]["shell"] is False
    assert row["access"]["reason"] == "credentials"
    assert inventory.vm_metrics(roots, row["id"], expected_run_id=row["run_id"])["reason"] == "credentials"


def test_resource_capabilities_are_list_projection_without_guest_probes(tmp_path, monkeypatch):
    from palimpsest_local import vm_access

    roots = _setup_roots(tmp_path)
    row = _observed_vm("redis", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(
        inventory.host_inventory, "observe", lambda: {"vms": [row], "volumes": [], "networks": [], "warnings": []}
    )
    monkeypatch.setattr(vm_access, "spawn_process_session", lambda *a, **kw: pytest.fail("list must not probe SSH"))
    listed = inventory.list_vms(roots)["vms"][0]
    assert listed["access"]["shell"] is True
    assert listed["access"]["metrics"] is True
    assert list(roots.runs.iterdir()) == []
    _resource_run(roots, "oci", backend="kvm", runtime_kind="oci-root", status="running")
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", _live_ledger_projection)
    oci = next(vm for vm in inventory.list_vms(roots)["vms"] if vm["name"] == "oci")
    assert oci["access"]["transport"] == "oci-monitor"
    assert oci["access"]["shell"] is False
    assert oci["access"]["exec"] is True
    assert oci["access"]["metrics"] is True


def test_guest_probe_time_and_output_bounds_reap_real_processes():
    import sys

    from palimpsest_local import vm_access
    from palimpsest_local.process_session import LocalProcessSession

    too_large = LocalProcessSession(
        (sys.executable, "-c", "import os; os.write(1, b'x' * 65536)"), tty=False, stdin=False
    )
    with pytest.raises(vm_access.VMAccessError) as large:
        vm_access._collect(too_large, limit=1024)
    assert large.value.category == "output-limit"
    assert too_large._process.poll() is not None
    blocked = LocalProcessSession((sys.executable, "-c", "import time; time.sleep(60)"), tty=False, stdin=False)
    with pytest.raises(vm_access.VMAccessError) as timeout:
        vm_access._collect(blocked, timeout=0.05)
    assert timeout.value.category == "timeout"
    assert blocked._process.poll() is not None


def test_selected_owned_vm_missing_ssh_trust_is_explicit(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    run_id = _resource_run(roots, "owned", backend="kvm", status="running")
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", _live_ledger_projection)
    result = inventory.vm_metrics(roots, "owned", expected_run_id=run_id)
    assert result["status"] == "unavailable"
    assert result["reason"] == "credentials"
    assert result["memory"] is None


def test_detail_identity_changed_during_measurement_discards_old_counters(tmp_path, monkeypatch):
    from palimpsest_local import vm_access

    roots, row, _, instance = _vm_access_fixture(tmp_path, monkeypatch)

    def collect(session, **kw):
        iterator = session.events()
        output = bytearray(next(iterator).data)
        instance["config"]["env"]["PALIMPSEST_RUN_ID"] = "22222222-2222-4222-8222-222222222222"
        try:
            for event in iterator:
                if hasattr(event, "data"):
                    output.extend(event.data)
            return bytes(output)
        finally:
            session.close()

    monkeypatch.setattr(vm_access, "_collect", collect)
    monkeypatch.setattr(
        vm_access,
        "spawn_process_session",
        lambda argv, **kw: _spawn_lima_guest(
            tmp_path, argv, f"import os, time; os.write(1, {_LINUX_METRICS!r}); time.sleep(.1)", **kw
        ),
    )
    result = inventory.vm_metrics(roots, row["id"], expected_run_id=row["run_id"])
    assert result["status"] == "unavailable"
    assert result["reason"] == "identity"
    assert result["interfaces"] is None


def test_stopped_owned_hvf_keeps_bounded_console_without_ssh_credentials(tmp_path, monkeypatch):
    from palimpsest_local import vm_access

    roots = _setup_roots(tmp_path)
    run_id = _resource_run(roots, "owned-hvf", backend="libvirt-hvf")
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", _live_ledger_projection)
    console = roots.runs / "owned-hvf" / "console.log"
    console.write_bytes(b"older boot output\n" + b"x" * vm_access.MAX_OUTPUT_BYTES + b"\nREADY\npoweroff\n")
    console.chmod(0o600)

    result = inventory.vm_logs(roots, "owned-hvf", expected_run_id=run_id, tail=2)
    assert result == {
        "status": "available",
        "source": "retained-console",
        "log": "READY\npoweroff",
        "truncated": True,
    }

    secret = tmp_path / "foreign-console"
    secret.write_text("hidden credential")
    console.unlink()
    console.symlink_to(secret)
    replaced = inventory.vm_logs(roots, "owned-hvf", expected_run_id=run_id, tail=2)
    assert replaced["status"] == "unavailable"
    assert replaced["log"] == ""


@pytest.mark.parametrize("damage", ["oversized", "missing", "read-failure"])
def test_guest_source_overflow_and_read_failure_preserve_other_measurements(tmp_path, damage):
    import re

    from palimpsest_local import vm_access
    from palimpsest_local.process_session import LocalProcessSession

    proc = tmp_path / "proc"
    chunks = re.split(rb"(?m)^@@([^\n]+)\n", _LINUX_METRICS)
    for source, content in zip(chunks[1::2], chunks[2::2], strict=True):
        if source in {b"df", b"end"}:
            continue
        target = proc / source.decode()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    diskstats = proc / "diskstats"
    if damage == "oversized":
        # Exactly 64 KiB of a syntactically valid device row hides a later
        # device with the old prefix-only reader. Padding itself is legal.
        row = diskstats.read_bytes()
        diskstats.write_bytes(b" " * (65536 - len(row)) + row + b"8 1 vdb 1 0 2 3 4 0 5 6 0 7 8\n")
    else:
        diskstats.unlink()
        if damage == "read-failure":
            diskstats.mkdir()
    probe = vm_access.METRICS_PROBE.replace("/proc/", str(proc) + "/")
    session = LocalProcessSession(("sh", "-c", probe), tty=False, stdin=False)
    parsed = vm_access.parse_metrics(vm_access._collect(session))
    assert parsed["block_devices"] is None
    assert "diskstats" in parsed["unavailable"]
    assert parsed["cpu"]["total_ticks"] == 63
    assert parsed["interfaces"] == [{"name": "eth0", "rx_bytes": 123, "tx_bytes": 456}]


def _lifecycle_run(tmp_path, monkeypatch, *, status="running", **fields):
    roots = _setup_roots(tmp_path)
    run_id = _resource_run(roots, "selected", status=status, **fields)
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", _live_ledger_projection)
    monkeypatch.setattr(
        inventory.runtime_dispatch.lima,
        "_run_command",
        lambda *a, **kw: pytest.fail("must not invoke a real VM backend"),
    )
    return roots, run_id


def _replace_lifecycle_run(roots):
    replacement_id = "22222222-2222-4222-8222-222222222222"
    run = roots.runs / "selected"
    ledger = state.read_json(run / "state.json")
    state.atomic_write_json(
        run / "owner.json",
        {
            "schema_version": 1,
            "name": "selected",
            "run_id": replacement_id,
        },
    )
    state.atomic_write_json(run / "state.json", {**ledger, "run_id": replacement_id})
    return replacement_id


@pytest.mark.parametrize("boundary", ["selected", "inventory", "dispatch", "adapter-lock"])
def test_remove_vm_replacement_fences_original_uuid_before_backend(tmp_path, monkeypatch, boundary):
    roots, run_id = _lifecycle_run(tmp_path, monkeypatch)
    adapter = inventory.runtime_dispatch.lima
    monkeypatch.setattr(
        adapter, "_instance_info_or_none", lambda *a, **kw: pytest.fail("replacement reached backend inspection")
    )
    if boundary == "selected":
        _replace_lifecycle_run(roots)
    elif boundary == "inventory":
        original = inventory.get_vm

        def replace_after_inventory(*args, **kwargs):
            row = original(*args, **kwargs)
            _replace_lifecycle_run(roots)
            return row

        monkeypatch.setattr(inventory, "get_vm", replace_after_inventory)
    elif boundary == "dispatch":
        original = inventory.runtime_dispatch.rm

        def replace_before_dispatch(*args, **kwargs):
            assert kwargs["expected_identity"].run_id == run_id
            _replace_lifecycle_run(roots)
            return original(*args, **kwargs)

        monkeypatch.setattr(inventory.runtime_dispatch, "rm", replace_before_dispatch)
    else:
        original = adapter.rm

        def replace_before_adapter_lock(*args, **kwargs):
            assert kwargs["_expected_record"].run_id == run_id
            assert kwargs["_expected_snapshot"].record.run_id == run_id
            _replace_lifecycle_run(roots)
            return original(*args, **kwargs)

        monkeypatch.setattr(adapter, "rm", replace_before_adapter_lock)
    with pytest.raises(StateError, match="identity changed|ledger changed"):
        inventory.remove_vm(roots, "selected", expected_run_id=run_id, confirmation="selected")
    replacement = state.read_run_ledger_snapshot(roots, "selected")
    assert replacement.record.run_id == "22222222-2222-4222-8222-222222222222"
    assert replacement.state["status"] == "running"


@pytest.mark.parametrize("backend", ["lima-vz", "lima-krunkit"])
@pytest.mark.parametrize("native_id", [False, True])
def test_remove_vm_external_identity_is_forbidden_without_adoption(tmp_path, monkeypatch, backend, native_id):
    identifier = f"host:{backend}:external" if native_id else "external"
    roots = _setup_roots(tmp_path)
    run_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(
        inventory.host_inventory,
        "observe",
        lambda: {
            "vms": [_observed_vm("external", run_id, backend=backend)],
            "volumes": [],
            "networks": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        inventory.runtime_dispatch, "rm", lambda *a, **kw: pytest.fail("external VM reached lifecycle dispatch")
    )
    with pytest.raises(StateError, match="forbidden"):
        inventory.remove_vm(roots, identifier, expected_run_id=run_id, confirmation="external")
    assert list(roots.runs.iterdir()) == []
    assert not any(inventory.list_vms(roots)["vms"][0]["lifecycle"].values())


@pytest.mark.parametrize("confirmation", ["Selected", "selected ", " selected", "", None, True])
def test_remove_vm_requires_exact_typed_name(tmp_path, monkeypatch, confirmation):
    roots, run_id = _lifecycle_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        inventory.runtime_dispatch, "rm", lambda *a, **kw: pytest.fail("unconfirmed VM reached lifecycle dispatch")
    )
    with pytest.raises(StateError, match="confirmation must exactly match"):
        inventory.remove_vm(roots, "selected", expected_run_id=run_id, confirmation=confirmation)
    assert state.read_run_ledger_snapshot(roots, "selected").state["status"] == "running"


@pytest.mark.parametrize("volumes", [None, 0, 1, "false", "true", [], {}])
def test_remove_vm_requires_boolean_volume_flag(tmp_path, monkeypatch, volumes):
    roots, run_id = _lifecycle_run(tmp_path, monkeypatch)
    monkeypatch.setattr(inventory, "get_vm", lambda *a, **kw: pytest.fail("invalid flag read inventory"))
    with pytest.raises(StateError, match="volumes must be a boolean"):
        inventory.remove_vm(roots, "selected", expected_run_id=run_id, confirmation="selected", volumes=volumes)


@pytest.mark.parametrize("condition", ["stale", "ambiguous", "removed", "oci-root"])
def test_remove_vm_ineligible_selected_states_fail_closed(tmp_path, monkeypatch, condition):
    roots, run_id = _lifecycle_run(
        tmp_path,
        monkeypatch,
        status="removed" if condition == "removed" else "running",
        runtime_kind="oci-root" if condition == "oci-root" else "cloud-image",
        backend="kvm" if condition == "oci-root" else "lima-vz",
    )
    if condition == "stale":
        monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", inventory.runtime_dispatch.ps)
    elif condition == "ambiguous":
        monkeypatch.setattr(
            inventory.host_inventory,
            "observe",
            lambda: {
                "vms": [_observed_vm("selected", "33333333-3333-4333-8333-333333333333", backend="kvm")],
                "volumes": [],
                "networks": [],
                "warnings": [],
            },
        )
    monkeypatch.setattr(
        inventory.runtime_dispatch, "rm", lambda *a, **kw: pytest.fail("ineligible VM reached lifecycle dispatch")
    )
    rows = inventory.list_vms(roots)["vms"]
    assert not any(row["lifecycle"]["remove"] for row in rows)
    with pytest.raises(StateError, match="unavailable|forbidden|ambiguous"):
        inventory.remove_vm(roots, "selected", expected_run_id=run_id, confirmation="selected")


@pytest.mark.parametrize("volumes", [False, True])
def test_remove_vm_canonical_disk_retention_never_deletes_project_or_arbitrary_files(tmp_path, monkeypatch, volumes):
    import subprocess

    roots, run_id = _lifecycle_run(
        tmp_path,
        monkeypatch,
        volumes=[
            {
                "name": "project-data",
                "host_path": str(tmp_path / "unrelated.raw"),
            }
        ],
    )
    run = roots.runs / "selected"
    (run / "disk.raw").write_bytes(b"owned run disk")
    (run / "console.log").write_text("retained log")
    outside = tmp_path / "unrelated.raw"
    outside.write_bytes(b"foreign disk")
    (run / "outside-link").symlink_to(outside)
    project = roots.volumes / "project-data"
    project.mkdir()
    (project / "data.raw").write_bytes(b"named project volume")
    sibling_id = _resource_run(roots, "sibling")
    home = (tmp_path / "lima").resolve()
    native = home / "selected"
    native.mkdir(parents=True, mode=0o700)
    (native / "diffdisk").write_bytes(b"native root sentinel")
    (native / "lima.yaml").write_text("vmType: vz\n")
    monkeypatch.setenv("LIMA_HOME", str(home))
    instance = {
        "name": "selected",
        "dir": str(native),
        "vmType": "vz",
        "status": "Running",
        "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
    }
    calls = []

    def delete_backend(argv, **kwargs):
        nonlocal instance
        calls.append(argv)
        if argv[1] == "stop":
            instance["status"] = "Stopped"
        else:
            import shutil

            shutil.rmtree(native)
            instance = None
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(inventory.runtime_dispatch.lima, "_instance_info_or_none", lambda name: instance)
    monkeypatch.setattr(inventory.runtime_dispatch.lima, "_run_command", delete_backend)

    kwargs = {"volumes": True} if volumes else {}  # Exercise default retention, not explicit False.
    result = inventory.remove_vm(roots, "selected", expected_run_id=run_id, confirmation="selected", **kwargs)

    assert calls[-1] == ["limactl", "delete", "--force", "--", "selected"]
    assert result["status"] == "removed" and result["run_id"] == run_id
    archives = inventory.retained_instances.list_retained_instances(roots)
    if volumes:
        assert archives == []
    else:
        archive = archives[0]
        saved = roots.state / "retained-instances" / archive["id"].split(":", 1)[1]
        assert (saved / "diffdisk").read_bytes() == b"native root sentinel"
        assert archive["topology"]["volumes"][0]["name"] == "project-data"
    assert str(tmp_path) not in repr(result)
    if volumes:
        assert not run.exists()
    else:
        assert (run / "disk.raw").read_bytes() == b"owned run disk"
        assert (run / "console.log").read_text() == "retained log"
        assert (run / "outside-link").is_symlink()
        assert state.read_run_ledger_snapshot(roots, "selected").state["status"] == "removed"
    assert outside.read_bytes() == b"foreign disk"
    assert (project / "data.raw").read_bytes() == b"named project volume"
    assert state.read_run_ledger_snapshot(roots, "sibling").record.run_id == sibling_id


@pytest.mark.parametrize("boundary", ["dispatch", "adapter-lock"])
@pytest.mark.parametrize("action,status", [("start", "stopped"), ("stop", "running")])
def test_vm_lifecycle_start_stop_carry_original_identity_into_dispatch(tmp_path, monkeypatch, action, status, boundary):
    roots, run_id = _lifecycle_run(tmp_path, monkeypatch, status=status)
    target = inventory.runtime_dispatch if boundary == "dispatch" else inventory.runtime_dispatch.lima
    original = getattr(target, action)

    def replace_before_operation(*args, **kwargs):
        identity = kwargs["expected_identity"] if boundary == "dispatch" else kwargs["_expected_record"]
        assert identity.name == "selected" and identity.run_id == run_id
        _replace_lifecycle_run(roots)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, action, replace_before_operation)
    with pytest.raises(StateError, match="identity changed|ledger changed"):
        inventory.vm_lifecycle(roots, "selected", action, expected_run_id=run_id)
    assert state.read_run_ledger_snapshot(roots, "selected").state["status"] == status


def test_vm_lifecycle_capabilities_require_live_unique_managed_selection(tmp_path, monkeypatch):
    roots, _ = _lifecycle_run(tmp_path, monkeypatch, status="stopped")
    row = inventory.list_vms(roots)["vms"][0]
    assert row["lifecycle"] == {"start": True, "stop": False, "remove": True, "remove_volumes": True}
    assert not any(inventory.list_vms(roots, live=False)["vms"][0]["lifecycle"].values())


@pytest.mark.parametrize("expected_run_id", [None, "", True, "not-a-uuid"])
def test_vm_lifecycle_missing_or_invalid_expected_uuid_fails_closed(tmp_path, monkeypatch, expected_run_id):
    roots, _ = _lifecycle_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        inventory.runtime_dispatch, "rm", lambda *a, **kw: pytest.fail("invalid UUID reached lifecycle dispatch")
    )
    with pytest.raises(StateError, match="expected_run_id|identity changed"):
        inventory.remove_vm(roots, "selected", expected_run_id=expected_run_id, confirmation="selected")


def test_vm_lifecycle_stop_returns_durable_safe_receipt(tmp_path, monkeypatch):
    roots, run_id = _lifecycle_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        inventory.runtime_dispatch.lima,
        "_instance_info_or_none",
        lambda name: {
            "name": "selected",
            "status": "Stopped",
            "vmType": "vz",
            "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
        },
    )
    result = inventory.vm_lifecycle(roots, "selected", "stop", expected_run_id=run_id)
    assert result["run_id"] == run_id
    assert result["operation"] == "stop"
    assert result["previous_status"] == "running"
    assert result["status"] == "stopped"
    assert result["lifecycle_revision"] == 1
    assert str(tmp_path) not in repr(result)
    snapshot = state.read_run_ledger_snapshot(roots, "selected")
    assert snapshot.state["status"] == "stopped"
    assert state.lifecycle_revision(snapshot) == result["lifecycle_revision"]


@pytest.mark.parametrize("action", ["rm", "remove", "restart", None, True, []])
def test_vm_lifecycle_rejects_other_actions_before_observation(tmp_path, monkeypatch, action):
    roots = _setup_roots(tmp_path)
    monkeypatch.setattr(inventory, "get_vm", lambda *a, **kw: pytest.fail("invalid action read inventory"))
    with pytest.raises(StateError, match="must be start or stop"):
        inventory.vm_lifecycle(roots, "selected", action, expected_run_id="11111111-1111-4111-8111-111111111111")


def test_remove_vm_missing_selected_run_fails_closed(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    monkeypatch.setattr(
        inventory.runtime_dispatch, "rm", lambda *a, **kw: pytest.fail("absent VM reached lifecycle dispatch")
    )
    with pytest.raises(StateError, match="not found"):
        inventory.remove_vm(
            roots, "selected", expected_run_id="11111111-1111-4111-8111-111111111111", confirmation="selected"
        )


@pytest.mark.parametrize("force", [False, True])
def test_retained_instance_prevents_layer_deletion_until_archive_purge(tmp_path, monkeypatch, force):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'retained shared layer']).name}"
    store.write_metadata(digest, {"kind": "squashfs"})
    native = tmp_path / "native"
    native.mkdir(mode=0o700)
    (native / "diffdisk").write_bytes(b"private root sentinel")
    instance = {"name": "saved", "dir": str(native), "vmType": "vz", "config": {}}
    archive = inventory.retained_instances.retain_lima(
        roots, instance, identity="native-saved", ledger={"layers": [{"digest": digest}]}
    )
    inventory.retained_instances.mark_native_removed(roots, archive)
    row = inventory.list_artifacts(roots)["artifacts"][0]
    assert row["delete_supported"] is False
    assert row["deletion_blockers"]["volumes"]
    with pytest.raises(StateError, match="still used by.*retained-instance"):
        inventory.remove_artifact(roots, digest, force=force)
    assert store.exists(digest)
    monkeypatch.setattr(inventory.retained_instances, "_assert_detached", lambda *_a: None)
    inventory.delete_volume(roots, archive, confirmation="saved")
    inventory.remove_artifact(roots, digest, force=force)
    assert not store.exists(digest)


def test_corrupt_retained_metadata_blocks_layer_deletion_and_inventory_action(tmp_path):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'protected while uncertain']).name}"
    store.write_metadata(digest, {"kind": "squashfs"})
    directory = roots.state / "retained-instances" / ("a" * 64)
    directory.mkdir(parents=True, mode=0o700)
    (directory / "record.json").write_text("{")
    with pytest.raises(StateError, match="retained instance metadata is invalid"):
        inventory.remove_artifact(roots, digest, force=True)
    assert store.exists(digest)
    assert inventory.list_artifacts(roots)["artifacts"][0]["delete_supported"] is False


@pytest.mark.parametrize(
    ("selected_backend", "native_backend"),
    [
        ("lima-vz", "lima-krunkit"),
        ("lima-krunkit", "lima-vz"),
    ],
)
def test_same_native_lima_name_replaced_with_other_backend_refuses_access_and_control(
    tmp_path, monkeypatch, selected_backend, native_backend
):
    roots = _setup_roots(tmp_path)
    run_id = _resource_run(roots, "replaced", backend=selected_backend)
    observed = _observed_vm("replaced", run_id, backend=native_backend)
    observed["removal_identity"] = "native-backend-selection"
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", _live_ledger_projection)
    monkeypatch.setattr(
        inventory.host_inventory,
        "observe",
        lambda: {
            "vms": [observed],
            "volumes": [],
            "networks": [],
            "warnings": [],
        },
    )
    rows = inventory.list_vms(roots)["vms"]
    assert len(rows) == 2
    assert all(not row["managed"] and row["access"]["reason"] == "identity" for row in rows)
    assert all(not any(row["lifecycle"].values()) for row in rows)
    with pytest.raises(StateError, match="ambiguous"):
        inventory.get_vm(roots, "replaced")


def test_krunkit_default_networks_do_not_merge_or_claim_vznat(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    for name in ("one", "two"):
        _resource_run(roots, name, backend="lima-krunkit", network="default")
    result = inventory.list_networks(roots)
    assert result["warnings"] == []
    assert len(result["networks"]) == 2
    assert all(row["mode"] == "gvisor" and row["kind"] == "user-mode" for row in result["networks"])
    assert {tuple(item["name"] for item in row["attachments"]) for row in result["networks"]} == {("one",), ("two",)}


@pytest.mark.parametrize("observed_run_matches", [False, True])
@pytest.mark.parametrize("native_name", ["project-data", "data"])
@pytest.mark.parametrize("selected_identity_matches", [False, True])
def test_lima_named_disk_joins_by_native_name_and_run_not_display_name(
    tmp_path, monkeypatch, observed_run_matches, native_name, selected_identity_matches
):
    roots = _setup_roots(tmp_path)
    run_id = _resource_run(
        roots,
        "krun",
        backend="lima-krunkit",
        volumes=[
            {
                "name": "data",
                "backend_name": "project-data",
                "mount_path": "/srv/data",
                "read_only": False,
            }
        ],
    )
    attachment = {
        "name": "krun",
        "run_id": run_id if observed_run_matches else "33333333-3333-4333-8333-333333333333",
        "mount_path": "",
        "read_only": False,
    }
    native = {
        "id": f"volume:lima:{native_name}",
        "name": native_name,
        "backend": "lima",
        "kind": "volume",
        "source": "host-backend",
        "status": "attached",
        "size_bytes": 4096,
        "allocated_bytes": 512,
        "attachments": [
            attachment,
            {
                "name": "external-vz",
                "run_id": "44444444-4444-4444-8444-444444444444",
                "mount_path": "",
                "read_only": False,
            },
        ],
    }
    monkeypatch.setattr(
        inventory.host_inventory,
        "observe",
        lambda: {
            "vms": [],
            "volumes": [native],
            "networks": [],
            "warnings": [],
        },
    )
    supplied = inventory.list_vms(roots, live=False)
    assert "backend_name" not in repr(supplied) and "project-data" not in repr(supplied)
    if not selected_identity_matches:
        path = roots.runs / "krun" / "state.json"
        payload = state.read_json(path)
        payload["backend"] = "lima-vz"
        state.atomic_write_json(path, payload)
    rows = inventory.list_volumes(roots, vms_result=supplied)["volumes"]
    declared = next(row for row in rows if row["id"] == f"run-volume:{run_id}:data")
    assert next(item for item in declared["attachments"] if item["name"] == "krun")["mount_path"] == "/srv/data"
    assert sum(row.get("allocated_bytes") or 0 for row in rows) == 512
    joined = observed_run_matches and native_name == "project-data" and selected_identity_matches
    assert (native["id"] in {row["id"] for row in rows}) is not joined
    assert declared["size_bytes"] == (4096 if joined else None)
    physical = declared if joined else next(row for row in rows if row["id"] == native["id"])
    assert {item["name"] for item in physical["attachments"]} == {"krun", "external-vz"}
    assert physical["backend"] == "lima"
    if joined:
        assert next(item for item in physical["attachments"] if item["name"] == "krun")["mount_path"] == "/srv/data"


def test_krunkit_same_name_replacement_keeps_default_network_identity_separate(tmp_path, monkeypatch):
    roots = _setup_roots(tmp_path)
    original = _resource_run(roots, "krun", backend="lima-krunkit", network="default")
    replacement = "33333333-3333-4333-8333-333333333333"
    native = {
        "id": f"network:lima-krunkit:{replacement}:krun",
        "name": "default",
        "kind": "user-mode",
        "backend": "lima-krunkit",
        "mode": "gvisor",
        "status": "configured",
        "source": "host-backend",
        "attachments": [{"name": "krun", "run_id": replacement, "ports": [], "guest_ip": None}],
    }
    monkeypatch.setattr(
        inventory.host_inventory,
        "observe",
        lambda: {
            "vms": [],
            "volumes": [],
            "networks": [native],
            "warnings": [],
        },
    )
    networks = inventory.list_networks(roots)["networks"]
    assert len(networks) == 2
    assert {tuple(item["run_id"] for item in row["attachments"]) for row in networks} == {(original,), (replacement,)}


def test_named_lima_network_is_one_native_network_across_backends(tmp_path):
    roots = _setup_roots(tmp_path)
    _resource_run(roots, "vz", backend="lima-vz", network="lima:shared")
    _resource_run(roots, "krun", backend="lima-krunkit", network="lima:shared")
    result = inventory.list_networks(roots)
    assert result["warnings"] == []
    assert len(result["networks"]) == 1
    network = result["networks"][0]
    assert network["id"] == "network:lima:shared" and network["backend"] == "lima"
    assert {item["name"] for item in network["attachments"]} == {"vz", "krun"}


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        (None, "compatible"),
        ("unknown-parent", "unknown"),
        ("missing-root-binding", "unknown"),
        ("foreign-parent", "incompatible"),
        ("missing-parent", "unknown"),
        ("cycle", "incompatible"),
        ("foreign-base", "incompatible"),
        ("missing-base", "unknown"),
        ("missing-bytes", "unknown"),
    ],
)
def test_artifact_platform_check_requires_complete_matching_chain(tmp_path, monkeypatch, damage, expected):
    from palimpsest_local import platforms

    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)

    def publish(payload, metadata):
        digest = "sha256:" + store.write_stream([payload]).name
        store.write_metadata(digest, metadata)
        return digest

    base = publish(
        b"base",
        {"kind": "cloud-image", "arch": "x86_64" if damage == "foreign-base" else "aarch64", "disk_format": "raw"},
    )
    parent = publish(
        b"parent",
        {
            "kind": "squashfs",
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
            "arch": None if damage == "unknown-parent" else "x86_64" if damage == "foreign-parent" else "aarch64",
            "base_image_digest": base,
        },
    )
    leaf = publish(
        b"leaf",
        {
            "kind": "squashfs",
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
            "arch": "aarch64",
            "base_image_digest": base,
            "parent_digest": parent,
        },
    )
    if damage == "missing-root-binding":
        metadata = store.read_metadata(parent)
        metadata.pop("base_image_digest")
        store.write_metadata(parent, metadata)
    if damage == "missing-parent":
        store.metadata_path(parent).unlink()
    elif damage == "missing-base":
        store.metadata_path(base).unlink()
    elif damage == "missing-bytes":
        store.blob_path(base).unlink()
    elif damage == "cycle":
        store.write_metadata(parent, {**store.read_metadata(parent), "parent_digest": leaf})
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    monkeypatch.setattr(
        platforms, "_check_capability", lambda *a, **kw: pytest.fail("metadata inspection probed a runtime")
    )
    checked = inventory.check_local_artifact(roots, leaf)
    assert checked["status"] == expected
    filtered = inventory.list_artifacts(roots, compatible=True)["layers"]
    assert (leaf in {item["digest"] for item in filtered}) == (expected == "compatible")
    assert store.read_metadata(leaf)["arch"] == "aarch64"


@pytest.mark.parametrize(
    ("kind", "arch", "platform", "system", "machine", "backend", "expected"),
    [
        ("cloud-image", "aarch64", None, "Darwin", "aarch64", "lima-krunkit", "compatible"),
        ("cloud-image", "x86_64", None, "Darwin", "aarch64", "lima-vz", "incompatible"),
        ("cloud-image", "aarch64", None, "Linux", "aarch64", "auto", "compatible"),
        ("cloud-image", "aarch64", None, "Windows", "aarch64", "auto", "incompatible"),
        ("oci-image", "amd64", "linux/amd64", "Linux", "x86_64", "kvm", "compatible"),
        ("oci-image", "arm64", "linux/arm64", "Darwin", "aarch64", "auto", "incompatible"),
        ("cloud-image", None, None, "Darwin", "aarch64", "auto", "unknown"),
    ],
)
def test_artifact_platform_check_retains_runtime_boundaries(kind, arch, platform, system, machine, backend, expected):
    from palimpsest_local.artifact_compatibility import check_artifact
    from palimpsest_local.platforms import HostPlatform

    result = check_artifact(
        {"kind": kind, "arch": arch, "platform": platform, "disk_format": "raw"},
        host=HostPlatform(system, machine),
        requested_backend=backend,
    )
    assert result["status"] == expected
    assert result["compatible"] == (expected == "compatible")


def test_local_artifact_architecture_filter_does_not_assume_legacy_target(tmp_path, monkeypatch):
    from palimpsest_local import platforms

    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    for arch in ("aarch64", "x86_64", None):
        digest = "sha256:" + store.write_stream([str(arch).encode()]).name
        store.write_metadata(digest, {"kind": "squashfs", "arch": arch, "media_type": MEDIA_TYPE_LAYER_SQUASHFS})
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    assert [item["arch"] for item in inventory.list_artifacts(roots, arch="aarch64")["layers"]] == ["aarch64"]
    assert inventory.list_artifacts(roots, compatible=True)["layers"] == []
    legacy = next(item for item in inventory.list_artifacts(roots)["layers"] if item.get("arch") is None)
    assert legacy["compatibility"]["status"] == "unknown"


def test_runtime_pack_platform_variant_keeps_conventional_and_oci_boundaries():
    from palimpsest_local.artifact_compatibility import check_artifact
    from palimpsest_local.platforms import HostPlatform

    base = "sha256:" + "a" * 64
    layer = {"kind": "squashfs", "arch": "aarch64", "platform": "linux/arm64/v8", "base_image_digest": base}
    metadata = {base: {"digest": base, "kind": "cloud-image", "arch": "aarch64", "disk_format": "raw"}}
    host = HostPlatform("Darwin", "aarch64")
    assert check_artifact(layer, lookup=metadata.__getitem__, host=host)["compatible"] is True
    assert check_artifact({"kind": "oci-image", "platform": "linux/arm64/v8"}, host=host)["status"] == "incompatible"
