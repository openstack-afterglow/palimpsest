"""Fail-closed existing-run dispatch from durable runtime ledgers."""

from __future__ import annotations

import json
import os
import socket
import stat as stat_module
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from palimpsest_local import runtime_dispatch, state
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import (
    ALLOWED_RUNTIME_COMBINATIONS,
    DispatchKey,
    ExistingRunRecord,
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
