from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import oci_root_volume as volumes
from palimpsest_local import oci_root_volume_inventory as inventory
from palimpsest_local import state
from palimpsest_local.errors import StateError

GRAPH = "sha256:" + "a" * 64


@pytest.fixture
def roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})


def _record(volume_id: str, *, status: str = "retained", generation: int = 3) -> volumes.OCIRootVolumeRecord:
    attached = status != "retained"
    return volumes.OCIRootVolumeRecord(
        volume_id,
        16 * 1024**2,
        GRAPH,
        "retain",
        status,
        str(uuid.uuid4()) if attached else None,
        "run-name" if attached else None,
        generation,
    )


def _publish(roots: state.StatePaths, record: volumes.OCIRootVolumeRecord) -> None:
    stem = record.volume_id.replace("-", "")
    (roots.oci_root_volumes / f"{stem}.raw").write_bytes(b"")
    path = roots.oci_root_volumes / f"{stem}.json"
    path.write_text(json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def test_public_projection_is_sorted_allowlisted_and_not_a_reuse_verdict(roots):
    second = _record("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    first = _record("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", status="attached")
    _publish(roots, second)
    _publish(roots, first)

    report = inventory.root_volumes(roots)

    assert report["schema"] == inventory.LIST_SCHEMA
    assert report["classification"] == "local-metadata-observation"
    assert [item["volume_id"] for item in report["volumes"]] == [first.volume_id, second.volume_id]
    assert report["volumes"][0]["attachment"] == {
        "run_id": first.attached_run_id,
        "name": first.attached_run_name,
    }
    assert report["volumes"][1]["attachment"] is None
    assert set(report["volumes"][0]) == {
        "volume_id",
        "status",
        "retention_policy",
        "size_bytes",
        "lower_graph_digest",
        "generation",
        "attachment",
    }
    assert "does not authorize root reuse or deletion" in report["guidance"]


def test_metadata_inventory_never_opens_raw_data_or_takes_lifecycle_file_lock(roots, monkeypatch):
    record = _record(str(uuid.uuid4()))
    _publish(roots, record)
    opened = []
    real_open = volumes.os.open

    def observe_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(volumes.os, "open", observe_open)
    monkeypatch.setattr(volumes, "file_lock", lambda *_a, **_k: pytest.fail("lifecycle lock entered"))
    assert inventory.root_volumes(roots)["volumes"][0]["volume_id"] == record.volume_id
    assert not any(name.endswith(".raw") for name in opened)


def test_empty_existing_namespace_and_exact_lookup(roots):
    assert inventory.root_volumes(roots)["volumes"] == []
    record = _record(str(uuid.uuid4()))
    _publish(roots, record)
    report = inventory.root_volume(roots, record.volume_id)
    assert report["schema"] == inventory.INSPECT_SCHEMA and report["volume"]["volume_id"] == record.volume_id


def test_lookup_validates_canonical_uuid_before_inventory_io(monkeypatch, roots):
    monkeypatch.setattr(inventory, "list_oci_root_volume_records", lambda *_: pytest.fail("inventory read"))
    for value in ("not-a-uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", None):
        with pytest.raises(inventory.OCIRootVolumeInventoryError, match="canonical UUID"):
            inventory.root_volume(roots, value)  # type: ignore[arg-type]


def test_unknown_and_missing_runtime_are_fixed_path_free_errors(tmp_path):
    missing = state.StatePaths(tmp_path / "config", tmp_path / "state")
    with pytest.raises(inventory.OCIRootVolumeInventoryError) as unavailable:
        inventory.root_volumes(missing)
    with pytest.raises(inventory.OCIRootVolumeInventoryError, match="unknown") as unknown:
        inventory.root_volume(state.init_resolved_roots(missing), str(uuid.uuid4()))
    assert str(tmp_path) not in str(unavailable.value) + str(unknown.value)


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "hardlink", "mode", "oversize", "duplicate"])
def test_strict_record_reader_rejects_unsafe_entries_without_blocking(tmp_path, kind):
    root = tmp_path / "records"
    root.mkdir()
    path = root / "record.json"
    payload = b'{"value":1}\n'
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = root / "target"
        target.write_bytes(payload)
        path.symlink_to(target.name)
    else:
        path.write_bytes(b'{"value":1,"value":2}\n' if kind == "duplicate" else payload)
        path.chmod(0o600)
        if kind == "hardlink":
            os.link(path, root / "other")
        elif kind == "mode":
            path.chmod(0o644)
        elif kind == "oversize":
            path.write_bytes(b"x" * (volumes._RECORD_BYTES + 1))
    if kind == "fifo":
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; from palimpsest_local.oci_root_volume import _strict_json_load; "
                    f"fd=os.open({str(root)!r}, os.O_RDONLY|os.O_DIRECTORY); "
                    "\ntry: _strict_json_load(fd, 'record.json')"
                    "\nexcept Exception: raise SystemExit(0)"
                    "\nfinally: os.close(fd)"
                    "\nraise SystemExit(1)"
                ),
            ],
            timeout=2,
            check=False,
        )
        assert result.returncode == 0
        return

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(StateError):
            volumes._strict_json_load(descriptor, path.name)
    finally:
        os.close(descriptor)


def test_strict_reader_fifo_swap_is_nonblocking_and_closes_descriptors(tmp_path):
    root = tmp_path / "records"
    root.mkdir()
    path = root / "record.json"
    path.write_bytes(b'{"value":1}\n')
    path.chmod(0o600)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import os
from palimpsest_local import oci_root_volume as volumes
from palimpsest_local.errors import StateError

root = {str(root)!r}
path = os.path.join(root, "record.json")
directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
real_open = os.open
opened = []

def swap_before_open(name, flags, *args, **kwargs):
    if name == "record.json" and kwargs.get("dir_fd") == directory_fd:
        os.unlink(path)
        os.mkfifo(path)
    descriptor = real_open(name, flags, *args, **kwargs)
    if name == "record.json":
        opened.append(descriptor)
    return descriptor

volumes.os.open = swap_before_open
try:
    volumes._strict_json_load(directory_fd, "record.json")
except StateError:
    pass
else:
    raise SystemExit(2)
finally:
    os.close(directory_fd)
if not opened:
    raise SystemExit(3)
try:
    os.fstat(opened[0])
except OSError:
    raise SystemExit(0)
raise SystemExit(4)
""",
        ],
        timeout=2,
        check=False,
    )
    assert result.returncode == 0


def test_strict_reader_rejects_deep_json_and_observed_replacement(tmp_path, monkeypatch):
    root = tmp_path / "records"
    root.mkdir()
    path = root / "record.json"
    path.write_bytes(b'{"a":' * 2000 + b"0\n")
    path.chmod(0o600)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(StateError):
            volumes._strict_json_load(descriptor, path.name)
        path.write_bytes(b'{"value":1}\n')
        replacement = root / "replacement"
        replacement.write_bytes(b'{"value":1}\n')
        replacement.chmod(0o600)
        real_read = volumes.os.read

        def replace_after_read(fd, count):
            payload = real_read(fd, count)
            os.replace(replacement, path)
            return payload

        monkeypatch.setattr(volumes.os, "read", replace_after_read)
        with pytest.raises(StateError, match="changed"):
            volumes._strict_json_load(descriptor, path.name)
    finally:
        os.close(descriptor)


def test_strict_reader_accepts_canonical_record_at_exact_size_limit(tmp_path):
    root = tmp_path / "records"
    root.mkdir()
    path = root / "record.json"
    prefix, suffix = b'{"value":"', b'"}\n'
    path.write_bytes(prefix + b"a" * (volumes._RECORD_BYTES - len(prefix) - len(suffix)) + suffix)
    path.chmod(0o600)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert len(path.read_bytes()) == volumes._RECORD_BYTES
        assert volumes._strict_json_load(descriptor, path.name)["value"]
    finally:
        os.close(descriptor)


def test_public_wrapper_sanitizes_deep_parser_failure(monkeypatch, roots):
    monkeypatch.setattr(inventory, "list_oci_root_volume_records", lambda *_: (_ for _ in ()).throw(RecursionError()))
    with pytest.raises(inventory.OCIRootVolumeInventoryError, match="unavailable or inconsistent"):
        inventory.root_volumes(roots)


def test_public_wrapper_preserves_cancellation(monkeypatch, roots):
    monkeypatch.setattr(
        inventory, "list_oci_root_volume_records", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        inventory.root_volumes(roots)


def test_namespace_caps_count_records_and_ignored_dot_entries(roots, monkeypatch):
    monkeypatch.setattr(volumes, "_MAX_NAMESPACE_ENTRIES", 2)
    for name in (".one", ".two"):
        (roots.oci_root_volumes / name).touch()
    assert volumes.list_oci_root_volume_records(roots) == ()
    (roots.oci_root_volumes / ".three").touch()
    with pytest.raises(StateError, match="entry limit"):
        volumes.list_oci_root_volume_records(roots)

    for path in roots.oci_root_volumes.iterdir():
        path.unlink()
    monkeypatch.setattr(volumes, "_MAX_NAMESPACE_ENTRIES", 10)
    monkeypatch.setattr(volumes, "_MAX_VOLUME_RECORDS", 1)
    _publish(roots, _record("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert len(volumes.list_oci_root_volume_records(roots)) == 1
    _publish(roots, _record("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))
    with pytest.raises(StateError, match="record limit"):
        volumes.list_oci_root_volume_records(roots)


def test_namespace_rejects_duplicate_enumerated_names(roots, monkeypatch):
    class DuplicateEntries:
        def __enter__(self):
            return iter((SimpleNamespace(name=".same"), SimpleNamespace(name=".same")))

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(volumes.os, "scandir", lambda _descriptor: DuplicateEntries())
    with pytest.raises(StateError, match="duplicate entry"):
        volumes.list_oci_root_volume_records(roots)


def test_namespace_rejects_observed_changes_even_to_ignored_entries(roots, monkeypatch):
    record = _record(str(uuid.uuid4()))
    _publish(roots, record)
    real_load = volumes._strict_json_load

    def mutate_namespace(directory_fd, name):
        result = real_load(directory_fd, name)
        before = roots.oci_root_volumes.stat()
        (roots.oci_root_volumes / ".appeared-during-read").touch()
        os.utime(
            roots.oci_root_volumes,
            ns=(before.st_atime_ns, max(before.st_mtime_ns + 1, roots.oci_root_volumes.stat().st_mtime_ns)),
        )
        return result

    monkeypatch.setattr(volumes, "_strict_json_load", mutate_namespace)
    with pytest.raises(StateError, match="changed during enumeration"):
        volumes.list_oci_root_volume_records(roots)


def test_namespace_rejects_unknown_entries_and_raw_record_mismatch(roots):
    (roots.oci_root_volumes / "unexpected").touch()
    with pytest.raises(StateError, match="invalid entry"):
        volumes.list_oci_root_volume_records(roots)
    (roots.oci_root_volumes / "unexpected").unlink()
    (roots.oci_root_volumes / ("a" * 32 + ".raw")).touch()
    with pytest.raises(StateError, match="inconsistent"):
        volumes.list_oci_root_volume_records(roots)
