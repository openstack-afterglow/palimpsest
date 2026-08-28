"""Fail-closed existing-run dispatch from durable runtime ledgers."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat as stat_module
import subprocess
import sys
import tempfile
import uuid
from collections import UserDict
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from palimpsest_local import runtime_dispatch, state
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import (
    ALLOWED_RUNTIME_COMBINATIONS,
    DispatchKey,
    ExistingRunRecord,
    ExpectedRunIdentity,
    RunAggregationError,
    RunAggregationResult,
    RunSummary,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RuntimeOperation,
)


def _roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )


def _write_ledger(
    roots: state.StatePaths,
    *,
    name: str = "demo",
    owner: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
    complete_v2_identity: bool = True,
) -> tuple[state.RunPaths, str]:
    rpaths = state.run_paths(roots, name)
    rpaths.root.mkdir(parents=True)
    run_id = str(uuid.uuid4())
    owner_payload = owner if owner is not None else {"schema_version": 1, "run_id": run_id, "name": name}
    state_payload = (
        dict(record)
        if record is not None
        else {"name": name, "run_id": owner_payload.get("run_id", run_id), "status": "stopped"}
    )
    if complete_v2_identity and state_payload.get("schema_version") == 2:
        state_payload.setdefault("name", name)
        state_payload.setdefault("run_id", owner_payload.get("run_id", run_id))
    rpaths.owner.write_text(json.dumps(owner_payload, sort_keys=True) + "\n", encoding="utf-8")
    rpaths.state.write_text(json.dumps(state_payload, sort_keys=True) + "\n", encoding="utf-8")
    return rpaths, run_id


def _snapshot_tree(root: Path) -> dict[str, tuple[str, int, int, bytes | str | None]]:
    result: dict[str, tuple[str, int, int, bytes | str | None]] = {}
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat_module.S_IMODE(metadata.st_mode)
        if stat_module.S_ISREG(metadata.st_mode):
            result[relative] = ("file", mode, metadata.st_mtime_ns, path.read_bytes())
        elif stat_module.S_ISDIR(metadata.st_mode):
            result[relative] = ("directory", mode, metadata.st_mtime_ns, None)
        elif stat_module.S_ISLNK(metadata.st_mode):
            result[relative] = ("symlink", mode, metadata.st_mtime_ns, os.readlink(path))
        else:
            result[relative] = ("other", mode, metadata.st_mtime_ns, None)
    return result


def _replace_ledger_with_oci(roots: state.StatePaths, rpaths: state.RunPaths) -> None:
    replacement_id = str(uuid.uuid4())
    rpaths.owner.write_text(
        json.dumps({"schema_version": 1, "run_id": replacement_id, "name": "demo"}) + "\n",
        encoding="utf-8",
    )
    rpaths.state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_kind": "oci-root",
                "backend": "kvm",
                "name": "demo",
                "run_id": replacement_id,
                "status": "stopped",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _run_dispatch_reader_subprocess(
    roots: state.StatePaths, *, setup_source: str = ""
) -> subprocess.CompletedProcess[str]:
    source = f"""
import os
import sys
import time
from pathlib import Path

from palimpsest_local import state
from palimpsest_local.errors import StateError

roots = state.StatePaths(Path(sys.argv[1]), Path(sys.argv[2]))
{setup_source}
fd_directory = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
before = len(os.listdir(fd_directory))
started = time.monotonic()
for _ in range(32):
    try:
        state.read_run_dispatch_record(roots, "demo")
    except StateError as exc:
        assert str(exc) == "cannot securely read run ledger"
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        raise AssertionError("non-regular ledger was accepted")
after = len(os.listdir(fd_directory))
assert time.monotonic() - started < 1.0
assert after == before, (before, after)
print("ok")
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    return subprocess.run(
        [sys.executable, "-c", source, str(roots.config), str(roots.state)],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
        env=environment,
    )


def test_runtime_type_contract_has_only_the_phase_one_dispatch_combinations() -> None:
    assert ALLOWED_RUNTIME_COMBINATIONS == frozenset(
        {
            (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
            (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
            (RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
        }
    )
    with pytest.raises(ValueError, match="unsupported runtime/backend combination"):
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.LIMA_VZ)
    with pytest.raises(TypeError, match="RuntimeKind and RuntimeBackend"):
        DispatchKey("cloud-image", "kvm")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Demo"},
        {"run_id": "not-a-uuid"},
        {"run_id": "862FFB44-6795-4618-B2D8-C0750439FAC3"},
        {"state_schema_version": True},
        {"state_schema_version": 0},
        {"state_schema_version": 3},
        {"dispatch_key": "cloud-image/kvm"},
    ],
)
def test_existing_run_record_public_constructor_enforces_every_invariant(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "name": "demo",
        "run_id": "862ffb44-6795-4618-b2d8-c0750439fac3",
        "state_schema_version": 2,
        "dispatch_key": DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        **kwargs,
    }
    with pytest.raises((TypeError, ValueError)):
        ExistingRunRecord(**values)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Demo"},
        {"run_id": "not-a-uuid"},
        {"run_id": "862FFB44-6795-4618-B2D8-C0750439FAC3"},
        {"dispatch_key": "cloud-image/kvm"},
    ],
)
def test_expected_run_identity_public_constructor_enforces_every_invariant(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "name": "demo",
        "run_id": "862ffb44-6795-4618-b2d8-c0750439fac3",
        "dispatch_key": DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        **kwargs,
    }
    with pytest.raises((TypeError, ValueError)):
        ExpectedRunIdentity(**values)


def test_missing_run_resolution_does_not_create_state_or_config_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config-home"
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    with pytest.raises(StateError):
        runtime_dispatch.resolve_existing_run("missing")

    assert not config_home.exists()
    assert not state_home.exists()


def test_legacy_state_may_omit_both_identity_fields_without_being_rewritten(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _rpaths, run_id = _write_ledger(roots, record={"status": "stopped"})
    before = _snapshot_tree(roots.state)

    record = state.read_run_dispatch_record(roots, "demo")

    assert record.run_id == run_id
    assert record.state_schema_version == 1
    assert record.dispatch_key == DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    ("schema_version", "runtime_kind", "backend", "expected_kind", "expected_backend"),
    [
        (None, None, None, RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        (1, None, None, RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        (1, None, "libvirt-hvf", RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
        (1, "cloud-image", "lima-vz", RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
        (2, "cloud-image", "kvm", RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        (2, "oci-root", "kvm", RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    ],
)
def test_read_run_dispatch_record_normalizes_only_durable_schema_rules(
    tmp_path: Path,
    schema_version: int | None,
    runtime_kind: str | None,
    backend: str | None,
    expected_kind: RuntimeKind,
    expected_backend: RuntimeBackend,
) -> None:
    roots = _roots(tmp_path)
    record: dict[str, Any] = {"name": "demo", "status": "stopped"}
    if schema_version is not None:
        record["schema_version"] = schema_version
    if runtime_kind is not None:
        record["runtime_kind"] = runtime_kind
    if backend is not None:
        record["backend"] = backend
    _rpaths, run_id = _write_ledger(roots, record=record)
    before = _snapshot_tree(roots.state)

    resolved = state.read_run_dispatch_record(roots, "demo")

    assert resolved.name == "demo"
    assert resolved.run_id == run_id
    assert resolved.state_schema_version == (1 if schema_version is None else schema_version)
    assert resolved.dispatch_key == DispatchKey(expected_kind, expected_backend)
    assert runtime_dispatch.resolve_existing_run("demo", roots=roots) == resolved
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    "record",
    [
        {"schema_version": True, "runtime_kind": "cloud-image", "backend": "kvm"},
        {"schema_version": 0, "runtime_kind": "cloud-image", "backend": "kvm"},
        {"schema_version": 3, "runtime_kind": "cloud-image", "backend": "kvm"},
        {"schema_version": 2, "backend": "kvm"},
        {"schema_version": 2, "runtime_kind": "cloud-image"},
        {"schema_version": 2, "runtime_kind": "unknown", "backend": "kvm"},
        {"schema_version": 2, "runtime_kind": "cloud-image", "backend": "unknown"},
        {"schema_version": 2, "runtime_kind": "oci-root", "backend": "lima-vz"},
        {"schema_version": 1, "runtime_kind": "oci-root", "backend": "kvm"},
    ],
)
def test_read_run_dispatch_record_rejects_unknown_or_ambiguous_state_without_rewrite(
    tmp_path: Path,
    record: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _rpaths, _ = _write_ledger(roots, record={"name": "demo", "status": "stopped", **record})
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError):
        state.read_run_dispatch_record(roots, "demo")

    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    "owner_update",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"schema_version": "1"},
        {"run_id": "not-a-uuid"},
        {"run_id": "862FFB44-6795-4618-B2D8-C0750439FAC3"},
        {"run_id": "862ffb4467954618b2d8c0750439fac3"},
        {"name": "other"},
        {"name": "Demo"},
    ],
)
def test_read_run_dispatch_record_rejects_invalid_owner_identity(
    tmp_path: Path,
    owner_update: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    owner = {"schema_version": 1, "run_id": str(uuid.uuid4()), "name": "demo", **owner_update}
    _rpaths, _ = _write_ledger(roots, owner=owner, record={"name": "demo", "status": "stopped"})
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError):
        state.read_run_dispatch_record(roots, "demo")

    assert _snapshot_tree(roots.state) == before


def test_read_run_dispatch_record_rejects_conflicting_state_identity(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _rpaths, _ = _write_ledger(
        roots,
        record={"name": "demo", "run_id": str(uuid.uuid4()), "status": "stopped"},
    )
    before = _snapshot_tree(roots.state)
    with pytest.raises(StateError, match="state run_id mismatch"):
        state.read_run_dispatch_record(roots, "demo")
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    "record",
    [
        {"schema_version": 1, "name": None},
        {"schema_version": 1, "run_id": None},
        {"schema_version": 1, "name": "other"},
        {"schema_version": 1, "run_id": "862ffb44-6795-4618-b2d8-c0750439fac3"},
        {"schema_version": 2, "runtime_kind": "cloud-image", "backend": "kvm"},
        {"schema_version": 2, "runtime_kind": "cloud-image", "backend": "kvm", "name": None},
        {"schema_version": 2, "runtime_kind": "cloud-image", "backend": "kvm", "run_id": None},
    ],
)
def test_state_identity_uses_explicit_legacy_compatibility_and_strict_v2_rules(
    tmp_path: Path,
    record: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _rpaths, _ = _write_ledger(roots, record=record, complete_v2_identity=False)
    before = _snapshot_tree(roots.state)
    with pytest.raises(StateError):
        state.read_run_dispatch_record(roots, "demo")
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize("filename", ["owner.json", "state.json"])
def test_dispatch_reader_rejects_symlinked_ledger_files(tmp_path: Path, filename: str) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    target = rpaths.root / f"real-{filename}"
    (rpaths.root / filename).rename(target)
    (rpaths.root / filename).symlink_to(target.name)
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely read run ledger") as captured:
        state.read_run_dispatch_record(roots, "demo")

    rendered = f"{captured.value!s} {captured.value!r}"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert filename not in rendered
    assert str(tmp_path) not in rendered
    assert "Errno" not in rendered
    assert _snapshot_tree(roots.state) == before


def test_secure_run_presence_returns_false_only_for_stable_enoent_without_writes(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    before = _snapshot_tree(roots.state)

    assert state.run_entry_present_or_ambiguous(roots, "demo") is False
    assert _snapshot_tree(roots.state) == before

    roots.runs.rmdir()
    before_missing_parent = _snapshot_tree(roots.state)
    assert state.run_entry_present_or_ambiguous(roots, "demo") is False
    assert _snapshot_tree(roots.state) == before_missing_parent


@pytest.mark.parametrize("entry_kind", ["directory", "dangling-symlink", "file"])
def test_secure_run_presence_treats_every_name_entry_as_present_or_ambiguous(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    roots = _roots(tmp_path)
    entry = roots.runs / "demo"
    if entry_kind == "directory":
        entry.mkdir()
    elif entry_kind == "dangling-symlink":
        entry.symlink_to("missing-run", target_is_directory=True)
    else:
        entry.write_text("not a directory", encoding="utf-8")
    before = _snapshot_tree(roots.state)

    assert state.run_entry_present_or_ambiguous(roots, "demo") is True
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize("parent_kind", ["symlink", "non-directory"])
def test_secure_run_presence_rejects_ambiguous_runs_parent_without_context_or_writes(
    tmp_path: Path,
    parent_kind: str,
) -> None:
    roots = _roots(tmp_path)
    if parent_kind == "symlink":
        real_runs = roots.state / "real-runs"
        roots.runs.rename(real_runs)
        roots.runs.symlink_to(real_runs.name, target_is_directory=True)
    else:
        roots.runs.rmdir()
        roots.runs.write_text("not a directory", encoding="utf-8")
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely inspect run entry") as captured:
        state.run_entry_present_or_ambiguous(roots, "demo")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _snapshot_tree(roots.state) == before


def test_secure_run_presence_rejects_permission_error_without_reflecting_os_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    original_open = state.os.open

    def denied_open(path: str | os.PathLike[str], flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == roots.runs and kwargs.get("dir_fd") is None:
            raise PermissionError(13, "SENSITIVE_PERMISSION_DETAIL", str(path))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(state.os, "open", denied_open)
    with pytest.raises(StateError, match="cannot securely inspect run entry") as captured:
        state.run_entry_present_or_ambiguous(roots, "demo")

    rendered = f"{captured.value!s} {captured.value!r}"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SENSITIVE_" not in rendered
    assert str(roots.runs) not in rendered


def test_secure_run_presence_rejects_runs_parent_swap_while_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    original_fstat = state._safe_fstat
    calls = 0

    def swapping_fstat(file_fd: int) -> os.stat_result | None:
        nonlocal calls
        result = original_fstat(file_fd)
        calls += 1
        if calls == 1:
            roots.runs.rename(roots.state / "old-runs")
            roots.runs.mkdir()
        return result

    monkeypatch.setattr(state, "_safe_fstat", swapping_fstat)
    with pytest.raises(StateError, match="cannot securely inspect run entry") as captured:
        state.run_entry_present_or_ambiguous(roots, "demo")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("filename", ["owner.json", "state.json"])
@pytest.mark.parametrize("node_kind", ["fifo", "unix-socket"])
def test_dispatch_reader_rejects_nonregular_ledger_without_blocking_writing_or_leaking_fds(
    tmp_path: Path,
    filename: str,
    node_kind: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    ledger_path = rpaths.root / filename
    ledger_path.unlink()
    socket_holder: socket.socket | None = None
    short_socket_root: Path | None = None
    short_run_alias: Path | None = None

    try:
        if node_kind == "fifo":
            os.mkfifo(ledger_path, mode=0o600)
        else:
            short_socket_root = Path(tempfile.mkdtemp(prefix="pali-sock-", dir="/tmp"))
            short_run_alias = short_socket_root / "run"
            short_run_alias.symlink_to(rpaths.root, target_is_directory=True)
            socket_holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            socket_holder.bind(str(short_run_alias / filename))
        before = _snapshot_tree(roots.state)
        result = _run_dispatch_reader_subprocess(roots)
        after = _snapshot_tree(roots.state)
    finally:
        if socket_holder is not None:
            socket_holder.close()
        if short_run_alias is not None:
            short_run_alias.unlink()
        if short_socket_root is not None:
            short_socket_root.rmdir()

    assert (result.returncode, result.stdout, result.stderr) == (0, "ok\n", "")
    assert after == before


@pytest.mark.parametrize("filename", ["owner.json", "state.json"])
def test_dispatch_reader_rejects_regular_to_fifo_swap_without_blocking_or_leaking_fds(
    tmp_path: Path,
    filename: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    untouched_path = rpaths.state if filename == "owner.json" else rpaths.owner
    untouched = untouched_path.read_bytes()
    setup_source = f"""
target = {filename!r}
original_open = state._open_readonly_no_follow
swapped = False

def racing_open(path, *, directory_fd=None, directory=False, nonblocking=False):
    global swapped
    if path == target and directory_fd is not None and not directory and not swapped:
        swapped = True
        os.unlink(path, dir_fd=directory_fd)
        os.mkfifo(path, mode=0o600, dir_fd=directory_fd)
    return original_open(
        path,
        directory_fd=directory_fd,
        directory=directory,
        nonblocking=nonblocking,
    )

state._open_readonly_no_follow = racing_open
"""

    result = _run_dispatch_reader_subprocess(roots, setup_source=setup_source)

    assert (result.returncode, result.stdout, result.stderr) == (0, "ok\n", "")
    assert stat_module.S_ISFIFO((rpaths.root / filename).lstat().st_mode)
    assert untouched_path.read_bytes() == untouched


def test_dispatch_reader_rejects_a_symlinked_run_directory(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    backing = roots.runs / "backing"
    backing.mkdir()
    run_id = str(uuid.uuid4())
    (backing / "owner.json").write_text(
        json.dumps({"schema_version": 1, "run_id": run_id, "name": "demo"}) + "\n",
        encoding="utf-8",
    )
    (backing / "state.json").write_text(json.dumps({"status": "stopped"}) + "\n", encoding="utf-8")
    (roots.runs / "demo").symlink_to(backing.name, target_is_directory=True)
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely read run ledger"):
        state.read_run_dispatch_record(roots, "demo")

    assert _snapshot_tree(roots.state) == before


def test_dispatch_reader_rejects_a_symlinked_runs_parent(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _write_ledger(roots)
    real_runs = roots.state / "real-runs"
    roots.runs.rename(real_runs)
    roots.runs.symlink_to(real_runs.name, target_is_directory=True)
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely read run ledger") as captured:
        state.read_run_dispatch_record(roots, "demo")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _snapshot_tree(roots.state) == before


def test_dispatch_reader_rejects_runs_parent_swap_during_pinned_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(roots)
    original_reader = state._read_pinned_json_object
    calls = 0

    def swapping_reader(directory_fd: int, filename: str) -> dict[str, Any]:
        nonlocal calls
        result = original_reader(directory_fd, filename)
        calls += 1
        if calls == 1:
            roots.runs.rename(roots.state / "old-runs")
            roots.runs.mkdir()
            _write_ledger(roots)
        return result

    monkeypatch.setattr(state, "_read_pinned_json_object", swapping_reader)
    with pytest.raises(StateError, match="run ledger changed during read"):
        state.read_run_dispatch_record(roots, "demo")


@pytest.mark.parametrize("mutation", ["directory", "oversized", "array", "malformed", "unicode", "recursive"])
def test_dispatch_reader_requires_bounded_regular_json_objects(tmp_path: Path, mutation: str) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    rpaths.state.unlink()
    if mutation == "directory":
        rpaths.state.mkdir()
    elif mutation == "oversized":
        rpaths.state.write_bytes(b"{" + b" " * (1024 * 1024) + b"}")
    elif mutation == "array":
        rpaths.state.write_text("[]\n", encoding="utf-8")
    elif mutation == "malformed":
        rpaths.state.write_text('{"value":"SENSITIVE_MALFORMED_JSON"', encoding="utf-8")
    elif mutation == "unicode":
        rpaths.state.write_bytes(b'{"value":"SENSITIVE_UNICODE_\xff"}')
    else:
        rpaths.state.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
    before = _snapshot_tree(roots.state)

    with pytest.raises(StateError, match="cannot securely read run ledger") as captured:
        state.read_run_dispatch_record(roots, "demo")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SENSITIVE_" not in f"{captured.value!s} {captured.value!r}"
    assert _snapshot_tree(roots.state) == before


def test_dispatch_reader_rejects_run_directory_swap_during_pinned_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(roots)
    original_reader = state._read_pinned_json_object
    calls = 0

    def swapping_reader(directory_fd: int, filename: str) -> dict[str, Any]:
        nonlocal calls
        result = original_reader(directory_fd, filename)
        calls += 1
        if calls == 1:
            rpaths.root.rename(roots.runs / "old-demo")
            _write_ledger(roots)
        return result

    monkeypatch.setattr(state, "_read_pinned_json_object", swapping_reader)
    with pytest.raises(StateError, match="run ledger changed during read"):
        state.read_run_dispatch_record(roots, "demo")


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        (
            "state",
            {"schema_version": "SENSITIVE_SCHEMA_VALUE", "name": "demo", "status": "stopped"},
        ),
        (
            "state",
            {
                "schema_version": 2,
                "runtime_kind": "SENSITIVE_RUNTIME_KIND",
                "backend": "kvm",
                "name": "demo",
                "status": "stopped",
            },
        ),
        (
            "owner",
            {"schema_version": 1, "run_id": "SENSITIVE_OWNER_UUID", "name": "demo"},
        ),
    ],
)
def test_dispatch_validation_never_reflects_untrusted_values_or_causes(
    tmp_path: Path,
    target: str,
    payload: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    owner = payload if target == "owner" else None
    record = payload if target == "state" else None
    _write_ledger(roots, owner=owner, record=record, complete_v2_identity=False)

    with pytest.raises(StateError) as captured:
        state.read_run_dispatch_record(roots, "demo")

    error: BaseException | None = captured.value
    rendered: list[str] = []
    while error is not None:
        rendered.append(f"{error!s} {error!r}")
        assert error.__context__ is None
        error = error.__cause__
    assert "SENSITIVE_" not in " ".join(rendered)


_OPERATIONS: tuple[tuple[RuntimeOperation, str, Callable[..., Any], dict[str, Any]], ...] = (
    (RuntimeOperation.START, "start", runtime_dispatch.start, {}),
    (RuntimeOperation.STOP, "stop", runtime_dispatch.stop, {}),
    (RuntimeOperation.RM, "rm", runtime_dispatch.rm, {"volumes": True}),
    (RuntimeOperation.INSPECT, "inspect_run", runtime_dispatch.inspect_run, {}),
    (RuntimeOperation.LOGS, "logs", runtime_dispatch.logs, {"follow": True}),
)

_ADAPTER_ENTRY_OPERATIONS: tuple[tuple[RuntimeOperation, Callable[..., Any], dict[str, Any]], ...] = (
    (RuntimeOperation.START, runtime_dispatch.start, {}),
    (RuntimeOperation.STOP, runtime_dispatch.stop, {}),
    (RuntimeOperation.RM, runtime_dispatch.rm, {"volumes": True}),
    (RuntimeOperation.INSPECT, runtime_dispatch.inspect_run, {}),
    (RuntimeOperation.LOGS, runtime_dispatch.logs, {"follow": False}),
)


@pytest.mark.parametrize(
    ("target_name", "dispatch", "kwargs"),
    [
        ("start", runtime_dispatch.start, {}),
        ("stop", runtime_dispatch.stop, {}),
        ("rm", runtime_dispatch.rm, {"volumes": True}),
        ("logs", runtime_dispatch.logs, {"follow": True}),
    ],
)
@pytest.mark.parametrize("mismatch", ["run-id", "backend", "runtime-kind"])
def test_expected_project_identity_rejects_static_name_reuse_before_adapter_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
    mismatch: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, current_run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
            "opaque": "SENSITIVE_REPLACEMENT_VALUE",
        },
    )
    expected_key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    if mismatch == "backend":
        expected_key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ)
    elif mismatch == "runtime-kind":
        expected_key = DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)
    expected = ExpectedRunIdentity(
        "demo",
        str(uuid.uuid4()) if mismatch == "run-id" else current_run_id,
        expected_key,
    )
    before = (rpaths.owner.read_bytes(), rpaths.state.read_bytes())
    effects: list[str] = []
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        target_name,
        lambda *_args, **_kwargs: effects.append("cloud"),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        target_name,
        lambda *_args, **_kwargs: effects.append("lima"),
    )

    with pytest.raises(StateError, match="run identity changed before lifecycle operation") as captured:
        dispatch("demo", roots=roots, expected_identity=expected, **kwargs)

    assert (current_run_id, DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)) != (
        expected.run_id,
        expected.dispatch_key,
    )
    assert effects == []
    assert (rpaths.owner.read_bytes(), rpaths.state.read_bytes()) == before
    assert "SENSITIVE_REPLACEMENT_VALUE" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def _install_adapter_side_effect_spies(
    monkeypatch: pytest.MonkeyPatch,
    effects: list[str],
) -> None:
    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"adapter side effect reached: {effect}")

    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name == "console.log":
            forbidden("console-read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(state, "locked", lambda *_a, **_k: forbidden("lifecycle-lock"))
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "_get_conn", lambda *_a, **_k: forbidden("libvirt"))
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "_write_state", lambda *_a, **_k: forbidden("cloud-write"))
    monkeypatch.setattr(runtime_dispatch.lima, "_run_command", lambda *_a, **_k: forbidden("limactl"))
    monkeypatch.setattr(runtime_dispatch.lima, "_write_state", lambda *_a, **_k: forbidden("lima-write"))
    monkeypatch.setattr(
        runtime_dispatch.lima,
        "_instance_info_or_none",
        lambda *_a, **_k: forbidden("lima-inspect"),
    )
    monkeypatch.setattr(Path, "read_text", guarded_read_text)


@pytest.mark.parametrize(
    ("runtime_kind", "backend", "adapter_name"),
    [
        ("cloud-image", "kvm", "cloud_runtime"),
        ("cloud-image", "libvirt-hvf", "cloud_runtime"),
        ("cloud-image", "lima-vz", "lima"),
    ],
)
@pytest.mark.parametrize(("_operation", "target_name", "dispatch", "kwargs"), _OPERATIONS)
def test_existing_operations_route_by_the_durable_dispatch_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
    backend: str,
    adapter_name: str,
    _operation: RuntimeOperation,
    target_name: str,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": runtime_kind,
            "backend": backend,
            "name": "demo",
            "status": "stopped",
        },
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def selected(name: str, **call_kwargs: Any) -> object:
        calls.append((adapter_name, name, call_kwargs))
        return iter(("line\n",)) if target_name == "logs" else object()

    monkeypatch.setattr(getattr(runtime_dispatch, adapter_name), target_name, selected)
    result = dispatch("demo", roots=roots, **kwargs)

    assert result is not None
    if target_name == "logs":
        assert calls == []
        assert list(result) == ["line\n"]
    assert len(calls) == 1
    called_adapter, called_name, call_kwargs = calls[0]
    assert (called_adapter, called_name) == (adapter_name, "demo")
    expected_record = call_kwargs.pop("_expected_record")
    assert isinstance(expected_record, ExistingRunRecord)
    assert expected_record.dispatch_key == DispatchKey(RuntimeKind(runtime_kind), RuntimeBackend(backend))
    assert call_kwargs == {"roots": roots, **kwargs}


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
@pytest.mark.parametrize(
    ("_operation", "target_name", "dispatch", "kwargs"),
    [entry for entry in _OPERATIONS if entry[0] is not RuntimeOperation.LOGS],
)
def test_eager_dispatch_rejects_identity_and_kind_swap_between_selection_and_adapter_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    _operation: RuntimeOperation,
    target_name: str,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "stopped",
        },
    )
    calls: list[str] = []
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, target_name, lambda *_a, **_k: calls.append("cloud"))
    monkeypatch.setattr(runtime_dispatch.lima, target_name, lambda *_a, **_k: calls.append("lima"))
    original_reader = state.read_run_dispatch_record
    reads = 0

    def racing_reader(read_roots: state.StatePaths, name: str) -> ExistingRunRecord:
        nonlocal reads
        record = original_reader(read_roots, name)
        reads += 1
        if reads == 1:
            replacement_id = str(uuid.uuid4())
            rpaths.owner.write_text(
                json.dumps({"schema_version": 1, "run_id": replacement_id, "name": "demo"}) + "\n",
                encoding="utf-8",
            )
            rpaths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "runtime_kind": "oci-root",
                        "backend": "kvm",
                        "name": "demo",
                        "run_id": replacement_id,
                        "status": "stopped",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        return record

    monkeypatch.setattr(state, "read_run_dispatch_record", racing_reader)
    with pytest.raises(StateError, match="run ledger changed during dispatch"):
        dispatch("demo", roots=roots, **kwargs)
    assert reads == 2
    assert calls == []


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
def test_log_stream_revalidates_bound_record_before_calling_or_entering_adapter_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "stopped",
        },
    )
    calls: list[str] = []

    def forbidden_logs(*_args: Any, **_kwargs: Any):
        calls.append("called")
        yield "must-not-be-observed"

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "logs", forbidden_logs)
    monkeypatch.setattr(runtime_dispatch.lima, "logs", forbidden_logs)
    stream = runtime_dispatch.logs("demo", roots=roots)
    assert calls == []

    replacement_id = str(uuid.uuid4())
    rpaths.owner.write_text(
        json.dumps({"schema_version": 1, "run_id": replacement_id, "name": "demo"}) + "\n",
        encoding="utf-8",
    )
    rpaths.state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_kind": "oci-root",
                "backend": "kvm",
                "name": "demo",
                "run_id": replacement_id,
                "status": "stopped",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="run ledger changed during dispatch"):
        next(stream)
    assert calls == []


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
@pytest.mark.parametrize(("_operation", "dispatch", "kwargs"), _ADAPTER_ENTRY_OPERATIONS)
def test_adapter_entry_guard_blocks_swap_after_dispatch_revalidation_before_real_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    _operation: RuntimeOperation,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "stopped",
        },
    )
    rpaths.console.write_text("old console\n", encoding="utf-8")
    effects: list[str] = []
    _install_adapter_side_effect_spies(monkeypatch, effects)
    original_reader = state.read_run_dispatch_record
    reads = 0

    def racing_reader(read_roots: state.StatePaths, name: str) -> ExistingRunRecord:
        nonlocal reads
        record = original_reader(read_roots, name)
        reads += 1
        if reads == 2:
            _replace_ledger_with_oci(roots, rpaths)
        return record

    monkeypatch.setattr(state, "read_run_dispatch_record", racing_reader)

    with pytest.raises(StateError, match="run ledger changed during adapter entry"):
        result = dispatch("demo", roots=roots, **kwargs)
        if _operation is RuntimeOperation.LOGS:
            next(result)

    assert reads == 3
    assert effects == []


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
@pytest.mark.parametrize(("_operation", "dispatch", "kwargs"), _ADAPTER_ENTRY_OPERATIONS)
def test_adapter_entry_guard_blocks_run_swap_during_secure_reread_before_real_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    _operation: RuntimeOperation,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "status": "stopped",
        },
    )
    rpaths.console.write_text("old console\n", encoding="utf-8")
    effects: list[str] = []
    _install_adapter_side_effect_spies(monkeypatch, effects)
    original_json_reader = state._read_pinned_json_object
    ledger_reads = 0

    def swapping_json_reader(directory_fd: int, filename: str) -> dict[str, Any]:
        nonlocal ledger_reads
        record = original_json_reader(directory_fd, filename)
        ledger_reads += 1
        if ledger_reads == 5:
            rpaths.root.rename(roots.runs / "old-demo")
            replacement_paths, _ = _write_ledger(
                roots,
                record={
                    "schema_version": 2,
                    "runtime_kind": "oci-root",
                    "backend": "kvm",
                    "status": "stopped",
                },
            )
            replacement_paths.console.write_text("replacement console\n", encoding="utf-8")
        return record

    monkeypatch.setattr(state, "_read_pinned_json_object", swapping_json_reader)

    with pytest.raises(StateError, match="run ledger changed during read"):
        result = dispatch("demo", roots=roots, **kwargs)
        if _operation is RuntimeOperation.LOGS:
            next(result)

    assert ledger_reads == 6
    assert effects == []


@pytest.mark.parametrize(("operation", "_target_name", "dispatch", "kwargs"), _OPERATIONS)
def test_oci_root_dispatch_returns_typed_capability_error_before_adapter_or_file_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: RuntimeOperation,
    _target_name: str,
    dispatch: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _rpaths, _ = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "oci-root",
            "backend": "kvm",
            "name": "demo",
            "status": "stopped",
        },
    )
    before = _snapshot_tree(roots.state)
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        operation.value if operation is not RuntimeOperation.INSPECT else "inspect_run",
        lambda *_a, **_k: pytest.fail("cloud adapter called"),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        operation.value if operation is not RuntimeOperation.INSPECT else "inspect_run",
        lambda *_a, **_k: pytest.fail("Lima adapter called"),
    )

    with pytest.raises(RuntimeCapabilityError) as captured:
        dispatch("demo", roots=roots, **kwargs)

    error = captured.value
    assert error.code == "runtime-operation-unavailable"
    assert error.operation is operation
    assert error.dispatch_key == DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)
    assert _snapshot_tree(roots.state) == before


def test_ps_securely_projects_mixed_durable_runs_without_adapters_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    common = {
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
                "host_path": "/private/SENSITIVE_VALUE/volume.raw",
                "backend_name": "internal-volume-name",
            }
        ],
        "ports": [{"host_ip": "127.0.0.1", "host_port": 8080, "guest_port": 80, "protocol": "tcp"}],
        "memory_mib": 2048,
        "vcpus": 2,
        "network": "default",
        "ssh": {"host": "127.0.0.1", "port": 2222},
        "guest_ip": "192.0.2.10",
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T00:01:00Z",
        "ssh_config_file": "/private/SENSITIVE_VALUE/ssh.config",
    }
    for name, backend in (("a-kvm", "kvm"), ("b-hvf", "libvirt-hvf"), ("c-lima", "lima-vz")):
        _write_ledger(
            roots,
            name=name,
            record={
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": backend,
                "status": "stopped",
                **common,
            },
        )
    _write_ledger(
        roots,
        name="d-oci",
        record={
            "schema_version": 2,
            "runtime_kind": "oci-root",
            "backend": "kvm",
            "status": "root-mounted",
            "created_at": "2026-08-28T00:00:00Z",
        },
    )
    broken = roots.runs / "broken"
    broken.mkdir()
    (broken / "owner.json").write_text("{}\n", encoding="utf-8")
    (broken / "state.json").write_text("{SENSITIVE_VALUE\n", encoding="utf-8")
    invalid_name = "BAD NAME"
    invalid = roots.runs / invalid_name
    invalid.mkdir()
    (invalid / "owner.json").write_text("SENSITIVE_VALUE", encoding="utf-8")

    before = _snapshot_tree(roots.state)
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "reconcile_run", lambda *_a, **_k: pytest.fail("adapter"))
    monkeypatch.setattr(runtime_dispatch.lima, "reconcile_run", lambda *_a, **_k: pytest.fail("adapter"))

    result = runtime_dispatch.ps(roots=roots)

    assert [summary.name for summary in result.summaries] == ["a-kvm", "b-hvf", "c-lima", "d-oci"]
    assert all(summary.stale is True for summary in result.summaries)
    assert {summary.status for summary in result.summaries} == {"stopped", "root-mounted"}
    expected_detail_keys = {
        "base_digest",
        "base_arch",
        "layers",
        "memory_mib",
        "vcpus",
        "network",
        "ports",
        "volumes",
        "ssh",
        "guest_ip",
        "created_at",
        "updated_at",
    }
    assert all(set(summary.details) == expected_detail_keys for summary in result.summaries)
    rendered = repr(result)
    assert "SENSITIVE_VALUE" not in rendered
    assert "local_path" not in rendered
    assert "host_path" not in rendered
    assert "backend_name" not in rendered
    expected_token = "entry-" + hashlib.sha256(invalid_name.encode()).hexdigest()[:12]
    assert [(error.name, error.entry_token, error.code) for error in result.errors] == [
        ("broken", None, "invalid-ledger"),
        (None, expected_token, "invalid-entry"),
    ]
    assert all(error.operation is RuntimeOperation.PS for error in result.errors)
    assert _snapshot_tree(roots.state) == before


@pytest.mark.parametrize(
    "malicious_fields",
    [
        {"base": {"digest": "SENSITIVE_VALUE", "arch": "x86_64"}},
        {"base": {"digest": "sha256:" + "a" * 64, "arch": "SENSITIVE_VALUE"}},
        {"layers": [{"digest": "SENSITIVE_VALUE"}]},
        {"layers": [{"digest": "sha256:" + "b" * 64, "target_dev": "SENSITIVE_VALUE"}]},
        {"network": "SENSITIVE_VALUE"},
        {"ports": [{"host_ip": "SENSITIVE_VALUE", "host_port": 8080, "guest_port": 80, "protocol": "tcp"}]},
        {"ports": [{"host_ip": "127.0.0.1", "host_port": 8080, "guest_port": 80, "protocol": "SENSITIVE_VALUE"}]},
        {"volumes": [{"name": "SENSITIVE_VALUE"}]},
        {"volumes": [{"name": "data", "mount_path": "SENSITIVE_VALUE"}]},
        {"volumes": [{"name": "data", "filesystem": "SENSITIVE_VALUE"}]},
        {"ssh": {"host": "SENSITIVE_VALUE", "port": 22}},
        {"guest_ip": "SENSITIVE_VALUE"},
        {"created_at": "SENSITIVE_VALUE"},
        {"updated_at": "SENSITIVE_VALUE"},
    ],
)
def test_ps_never_reflects_unvalidated_public_string_fields(
    tmp_path: Path,
    malicious_fields: dict[str, Any],
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
            **malicious_fields,
        },
    )

    result = runtime_dispatch.ps(roots=roots)

    assert result.summaries == ()
    assert len(result.errors) == 1
    assert result.errors[0].code == "invalid-ledger"
    assert "SENSITIVE_VALUE" not in repr(result)


def test_run_summary_public_constructor_rejects_semantically_untrusted_details() -> None:
    record = ExistingRunRecord(
        "demo",
        "862ffb44-6795-4618-b2d8-c0750439fac3",
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    details = MappingProxyType(
        {
            "base_digest": "SENSITIVE_VALUE",
            "base_arch": "x86_64",
            "layers": (),
            "memory_mib": None,
            "vcpus": None,
            "network": None,
            "ports": (),
            "volumes": (),
            "ssh": MappingProxyType({"host": None, "port": 22}),
            "guest_ip": None,
            "created_at": None,
            "updated_at": None,
        }
    )
    with pytest.raises(ValueError, match="invalid base digest"):
        RunSummary(record, "stopped", details, stale=True)
    with pytest.raises(TypeError, match="immutable detail mapping"):
        RunSummary(record, "stopped", UserDict(details), stale=True)
    nested_mutable = dict(details)
    nested_mutable["base_digest"] = "sha256:" + "a" * 64
    nested_mutable["ssh"] = UserDict({"host": None, "port": 22})
    with pytest.raises(TypeError, match="deeply immutable"):
        RunSummary(record, "stopped", MappingProxyType(nested_mutable), stale=True)


def test_reconcile_routes_each_exact_record_and_keeps_oci_durable_summary_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    for name, kind, backend, status in (
        ("a-kvm", "cloud-image", "kvm", "stopped"),
        ("b-hvf", "cloud-image", "libvirt-hvf", "stopped"),
        ("c-lima", "cloud-image", "lima-vz", "stopped"),
        ("d-oci", "oci-root", "kvm", "fetching"),
    ):
        _write_ledger(
            roots,
            name=name,
            record={
                "schema_version": 2,
                "runtime_kind": kind,
                "backend": backend,
                "status": status,
                "created_at": "2026-08-28T00:00:00Z",
            },
        )
    calls: list[tuple[str, str]] = []

    def cloud_adapter(name, *, _expected_record, **_kwargs):
        calls.append((name, _expected_record.dispatch_key.backend.value))
        return {
            "state": {"status": "running", "guest_ip": "SENSITIVE_VALUE"},
            "warnings": ["SENSITIVE_VALUE"] if name == "a-kvm" else [],
        }

    def lima_adapter(name, *, _expected_record, **_kwargs):
        calls.append((name, _expected_record.dispatch_key.backend.value))
        return {"state": {"status": "running", "guest_ip": "SENSITIVE_VALUE"}}

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "reconcile_run", cloud_adapter)
    monkeypatch.setattr(runtime_dispatch.lima, "reconcile_run", lima_adapter)

    result = runtime_dispatch.reconcile(roots=roots)

    assert calls == [("a-kvm", "kvm"), ("b-hvf", "libvirt-hvf"), ("c-lima", "lima-vz")]
    assert [summary.name for summary in result.summaries] == ["a-kvm", "b-hvf", "c-lima", "d-oci"]
    assert [summary.stale for summary in result.summaries] == [False, False, False, True]
    assert [summary.status for summary in result.summaries] == ["stopped", "stopped", "stopped", "fetching"]
    assert [(error.name, error.code, error.operation) for error in result.errors] == [
        ("a-kvm", "runtime-warning", RuntimeOperation.RECONCILE),
        ("d-oci", "runtime-capability", RuntimeOperation.RECONCILE),
    ]
    assert "SENSITIVE_VALUE" not in repr(result)


def test_reconcile_marks_original_durable_snapshot_stale_when_post_adapter_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    rpaths, original_run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "stopped",
        },
    )

    def replace_after_adapter(*_args, **_kwargs):
        replacement_run_id = str(uuid.uuid4())
        rpaths.owner.write_text(
            json.dumps({"schema_version": 1, "run_id": replacement_run_id, "name": "demo"}) + "\n",
            encoding="utf-8",
        )
        rpaths.state.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "runtime_kind": "cloud-image",
                    "backend": "kvm",
                    "name": "demo",
                    "run_id": replacement_run_id,
                    "status": "running",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {"state": {"status": "running"}}

    monkeypatch.setattr(runtime_dispatch.cloud_runtime, "reconcile_run", replace_after_adapter)

    result = runtime_dispatch.reconcile(roots=roots)

    assert len(result.summaries) == 1
    assert result.summaries[0].run_id == original_run_id
    assert result.summaries[0].status == "stopped"
    assert result.summaries[0].stale is True
    assert [(error.name, error.code) for error in result.errors] == [("demo", "runtime-failure")]


def test_reconcile_emits_stable_warning_after_missing_domain_status_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "status": "running",
        },
    )

    class MissingDomainConnection:
        def lookupByName(self, _name: str):
            raise KeyError("SENSITIVE_VALUE")

    monkeypatch.setattr(runtime_dispatch.cloud_runtime.kvm, "connect", lambda _uri: MissingDomainConnection())

    result = runtime_dispatch.reconcile(roots=roots)

    assert len(result.summaries) == 1
    assert result.summaries[0].status == "stopped"
    assert result.summaries[0].stale is False
    assert [(error.name, error.code, error.message) for error in result.errors] == [
        ("demo", "runtime-warning", "runtime status changed during reconciliation")
    ]
    assert "SENSITIVE_VALUE" not in repr(result)


@pytest.mark.parametrize("failure_mode", ["query", "foreign", "write"])
def test_lima_reconcile_failure_is_typed_stale_and_preserves_ledger_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    roots = _roots(tmp_path)
    rpaths, run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "status": "stopped",
        },
    )
    before = (rpaths.owner.read_bytes(), rpaths.state.read_bytes())
    backend_calls: list[str] = []
    writes: list[str] = []

    def query(_name: str) -> dict[str, Any]:
        backend_calls.append("query")
        if failure_mode == "query":
            raise StateError("SENSITIVE_VALUE")
        marker = "00000000-0000-0000-0000-000000000000" if failure_mode == "foreign" else run_id
        return {
            "name": "demo",
            "status": "Running",
            "config": {"env": {"PALIMPSEST_RUN_ID": marker}},
        }

    def write(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        writes.append("write")
        if failure_mode == "write":
            raise OSError("SENSITIVE_VALUE")
        pytest.fail("unexpected Lima ledger write")

    monkeypatch.setattr(runtime_dispatch.lima, "_instance_info_or_none", query)
    monkeypatch.setattr(runtime_dispatch.lima, "_write_state", write)

    result = runtime_dispatch.reconcile(roots=roots)

    assert backend_calls == (["query", "query"] if failure_mode == "write" else ["query"])
    assert writes == (["write"] if failure_mode == "write" else [])
    assert (rpaths.owner.read_bytes(), rpaths.state.read_bytes()) == before
    assert [(summary.name, summary.status, summary.stale) for summary in result.summaries] == [
        ("demo", "stopped", True)
    ]
    assert [(error.name, error.code, error.message) for error in result.errors] == [
        ("demo", "runtime-failure", "runtime reconciliation failed")
    ]
    assert "SENSITIVE_VALUE" not in repr(result)


def test_lima_reconcile_detects_cooperative_swap_during_external_query_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    rpaths, old_run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "status": "stopped",
        },
    )
    replacement_run_id = str(uuid.uuid4())
    replacement_owner = (
        json.dumps({"schema_version": 1, "run_id": replacement_run_id, "name": "demo"}, sort_keys=True) + "\n"
    ).encode()
    replacement_state = (
        json.dumps(
            {
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": "lima-vz",
                "name": "demo",
                "run_id": replacement_run_id,
                "status": "running",
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    backend_calls: list[str] = []

    def swapping_query(_name: str) -> dict[str, Any]:
        backend_calls.append("query")
        rpaths.owner.write_bytes(replacement_owner)
        rpaths.state.write_bytes(replacement_state)
        return {
            "name": "demo",
            "status": "Running",
            "config": {"env": {"PALIMPSEST_RUN_ID": old_run_id}},
        }

    monkeypatch.setattr(runtime_dispatch.lima, "_instance_info_or_none", swapping_query)
    monkeypatch.setattr(
        runtime_dispatch.lima,
        "_write_state",
        lambda *_a, **_k: pytest.fail("replacement ledger was overwritten"),
    )

    result = runtime_dispatch.reconcile(roots=roots)

    assert backend_calls == ["query"]
    assert rpaths.owner.read_bytes() == replacement_owner
    assert rpaths.state.read_bytes() == replacement_state
    assert [(summary.run_id, summary.status, summary.stale) for summary in result.summaries] == [
        (old_run_id, "stopped", True)
    ]
    assert [(error.name, error.code) for error in result.errors] == [("demo", "runtime-failure")]


def test_lima_reconcile_writes_only_the_fresh_live_status_observed_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    rpaths, run_id = _write_ledger(
        roots,
        record={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "lima-vz",
            "status": "stopped",
        },
    )
    statuses = iter(("Stopped", "Running"))
    backend_calls: list[str] = []

    def changing_query(_name: str) -> dict[str, Any]:
        backend_calls.append("query")
        return {
            "name": "demo",
            "status": next(statuses),
            "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
        }

    monkeypatch.setattr(runtime_dispatch.lima, "_instance_info_or_none", changing_query)

    result = runtime_dispatch.reconcile(roots=roots)

    assert backend_calls == ["query", "query"]
    assert [(summary.name, summary.status, summary.stale) for summary in result.summaries] == [
        ("demo", "running", False)
    ]
    assert result.errors == ()
    assert state.read_run_state(rpaths)["status"] == "running"


def test_ps_missing_runs_root_is_empty_and_does_not_create_directories(tmp_path: Path) -> None:
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")

    result = runtime_dispatch.ps(roots=roots)

    assert result == RunAggregationResult((), ())
    assert not roots.config.exists()
    assert not roots.state.exists()


def test_ps_rejects_ambiguous_runs_parent_and_reports_child_symlink_without_following(
    tmp_path: Path,
) -> None:
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")
    roots.state.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "owner.json").write_text("SENSITIVE_VALUE", encoding="utf-8")
    roots.runs.symlink_to(external, target_is_directory=True)

    with pytest.raises(StateError, match="cannot securely enumerate run ledgers") as captured:
        runtime_dispatch.ps(roots=roots)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SENSITIVE_VALUE" not in str(captured.value)

    roots.runs.unlink()
    roots.runs.mkdir()
    (roots.runs / "linked-run").symlink_to(external, target_is_directory=True)
    result = runtime_dispatch.ps(roots=roots)
    assert result.summaries == ()
    assert [(error.name, error.code) for error in result.errors] == [("linked-run", "invalid-entry")]
    assert "SENSITIVE_VALUE" not in repr(result)


def test_ps_detects_runs_parent_swap_while_enumerating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(roots, record={"status": "stopped"})
    original_reader = state._read_pinned_run_payloads
    swapped = False

    def swapping_reader(
        runs_fd: int,
        name: str,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal swapped
        payloads = original_reader(runs_fd, name, **kwargs)
        if not swapped:
            swapped = True
            roots.runs.rename(roots.state / "old-runs")
            roots.runs.mkdir()
        return payloads

    monkeypatch.setattr(state, "_read_pinned_run_payloads", swapping_reader)

    with pytest.raises(StateError, match="run ledger changed during read") as captured:
        runtime_dispatch.ps(roots=roots)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_ps_detects_runs_parent_swap_between_stat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _write_ledger(roots, record={"status": "stopped"})
    original_open = state._open_readonly_no_follow
    swapped = False

    def swapping_open(path, **kwargs):
        nonlocal swapped
        if not swapped and Path(path) == roots.runs and kwargs.get("directory"):
            swapped = True
            roots.runs.rename(roots.state / "old-runs")
            roots.runs.mkdir()
        return original_open(path, **kwargs)

    monkeypatch.setattr(state, "_open_readonly_no_follow", swapping_open)

    with pytest.raises(StateError, match="cannot securely enumerate run ledgers"):
        runtime_dispatch.ps(roots=roots)


def test_run_aggregation_result_rejects_unsorted_or_duplicate_errors() -> None:
    one = RunAggregationError(
        "z-run",
        None,
        RuntimeOperation.PS,
        None,
        "invalid-ledger",
        "invalid run ledger",
    )
    two = RunAggregationError(
        "a-run",
        None,
        RuntimeOperation.PS,
        None,
        "invalid-ledger",
        "invalid run ledger",
    )
    with pytest.raises(ValueError, match="deterministically sorted"):
        RunAggregationResult((), (one, two))
    with pytest.raises(ValueError, match="unique"):
        RunAggregationResult((), (one, one))
