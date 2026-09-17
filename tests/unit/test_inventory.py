from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from palimpsest_local import inventory, state
from palimpsest_local.errors import ArtifactValidationError, StateError
from palimpsest_local.hub import KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS
from palimpsest_local.oci_layout import ContentStore
from palimpsest_local.runtime_types import CapabilityCheck
from palimpsest_local.state import TagRecord, init_roots, write_tag_record


@pytest.fixture(autouse=True)
def _stub_operation_capability_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        inventory.runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "test-present", True),
    )


def _setup_roots(tmp_path: Path) -> state.StatePaths:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return init_roots({"XDG_CONFIG_HOME": str(config_dir), "XDG_STATE_HOME": str(state_dir)})


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
    (b2_dir / "console.log").write_text("buildkit log\n", encoding="utf-8")
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

    assert builds[1]["build_id"] == b1_id
    assert builds[1]["engine"] == "palimpsestfile"
    assert builds[1]["duration_ms"] == 42000

    b1_rec = inventory.get_build(roots, b1_id)
    assert b1_rec["output_tags"] == ["myimage:v1"]

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
