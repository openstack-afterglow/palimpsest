"""Unit tests for palimpsest_local.state."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from pathlib import Path

import pytest

import palimpsest_local.state as state
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind


def _cloud_key(backend: RuntimeBackend = RuntimeBackend.KVM) -> DispatchKey:
    return DispatchKey(RuntimeKind.CLOUD_IMAGE, backend)


def test_xdg_roots_and_permissions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {
            "XDG_CONFIG_HOME": str(tmppath / "cfg"),
            "XDG_STATE_HOME": str(tmppath / "st"),
        }
        roots = state.init_roots(env)
        assert roots.config == tmppath / "cfg" / "palimpsest"
        assert roots.state == tmppath / "st" / "palimpsest"

        assert state.permission_bits(roots.config) == 0o700
        assert state.permission_bits(roots.state) == 0o700
        assert state.permission_bits(roots.runs) == 0o700
        assert state.permission_bits(roots.locks) == 0o700
        assert state.permission_bits(roots.transfers) == 0o700
        assert state.permission_bits(roots.tags) == 0o700
        assert state.permission_bits(roots.builds) == 0o700
        assert state.permission_bits(roots.build_cache) == 0o700
        assert state.permission_bits(roots.runtime_packs) == 0o700
        assert state.permission_bits(roots.projects) == 0o700
        assert state.permission_bits(roots.volumes) == 0o700


def test_run_paths_and_owner_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)

        rpaths = state.run_paths(roots, "my-run")
        assert rpaths.root.resolve() == (roots.runs / "my-run").resolve()

        owner = state.write_owner_record(rpaths)
        assert owner.name == "my-run"
        assert owner.schema_version == 1
        assert owner.run_id != ""
        assert state.permission_bits(rpaths.owner) == 0o600

        # Owner record is immutable
        with pytest.raises(StateError, match="immutable"):
            state.write_owner_record(rpaths)

        read_owner = state.read_owner_record(rpaths)
        assert read_owner == owner


def test_project_paths_are_scoped_and_traversal_safe(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    project = state.project_paths(roots, "shop")

    assert project.root == roots.projects / "shop"
    assert project.volumes == roots.volumes / "shop"
    assert project.lock == roots.locks / "project-shop.lock"
    with pytest.raises(StateError, match="invalid project name"):
        state.project_paths(roots, "../escape")


def test_run_state_and_locks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)
        rpaths = state.run_paths(roots, "my-run")
        state.write_owner_record(rpaths)

        with state.locked(rpaths):
            st_data = state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.10"})
            assert st_data["status"] == "running"
            assert st_data["guest_ip"] == "192.168.122.10"

        read_st = state.read_run_state(rpaths)
        assert read_st["status"] == "running"
        assert read_st["guest_ip"] == "192.168.122.10"


def test_new_run_reservation_writes_exact_v2_identity_and_rejects_smuggling(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    with state.reserve_new_run(roots, "fresh", _cloud_key(RuntimeBackend.LIBVIRT_HVF)) as reservation:
        with pytest.raises(AttributeError, match="immutable"):
            reservation.dispatch_key = _cloud_key()  # type: ignore[misc]
        with pytest.raises(AttributeError, match="immutable"):
            reservation._last_status = "failed"  # type: ignore[misc]
        with pytest.raises(StateError, match="reserved identity"):
            reservation.write_state("creating", {"backend": "kvm"})
        with pytest.raises(StateError, match="invalid status"):
            reservation.write_state("root-mounted", {})
        written = reservation.write_state("creating", {"guest_ip": None})

    assert state.read_json(state.run_paths(roots, "fresh").owner) == {
        "schema_version": 1,
        "run_id": reservation.record.run_id,
        "name": "fresh",
    }
    assert written == state.read_run_state(state.run_paths(roots, "fresh"))
    assert {key: written[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "libvirt-hvf",
        "name": "fresh",
        "run_id": reservation.record.run_id,
        "status": "creating",
    }
    assert state.permission_bits(state.run_paths(roots, "fresh").owner) == 0o600
    assert state.permission_bits(state.run_paths(roots, "fresh").state) == 0o600


def test_new_run_reservation_uses_canonical_public_path_spelling(tmp_path: Path) -> None:
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(real_state, target_is_directory=True)
    roots = state.init_roots(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "PALIMPSEST_STATE_HOME": str(linked_state),
        }
    )

    with state.reserve_new_run(roots, "canonical", _cloud_key()) as reservation:
        reservation.write_state("failed", {})
        assert reservation.paths.root == roots.runs.resolve() / "canonical"
        assert reservation.paths.root == state.run_paths(roots, "canonical").root


def test_new_run_owner_postcheck_rejects_same_json_with_different_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    real_read = state._read_exact_private_file

    def rewrite_owner(directory_fd: int, filename: str) -> bytes:
        original = real_read(directory_fd, filename)
        if filename == "owner.json":
            rewritten = json.dumps(json.loads(original), indent=2, sort_keys=True).encode() + b"\n"
            file_fd = os.open(filename, os.O_WRONLY | os.O_TRUNC, dir_fd=directory_fd)
            try:
                os.write(file_fd, rewritten)
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
        return real_read(directory_fd, filename)

    monkeypatch.setattr(state, "_read_exact_private_file", rewrite_owner)

    with pytest.raises(StateError, match="owner record changed"):
        with state.reserve_new_run(roots, "owner-bytes", _cloud_key()):
            pytest.fail("byte-mutated owner was accepted")

    assert not (roots.runs / "owner-bytes").exists()


def test_new_run_state_postcheck_rejects_same_json_with_different_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    real_read = state._read_exact_private_file
    mutated = False

    def rewrite_first_state(directory_fd: int, filename: str) -> bytes:
        nonlocal mutated
        original = real_read(directory_fd, filename)
        if filename == "state.json" and not mutated:
            mutated = True
            rewritten = json.dumps(json.loads(original), indent=2, sort_keys=True).encode() + b"\n"
            file_fd = os.open(filename, os.O_WRONLY | os.O_TRUNC, dir_fd=directory_fd)
            try:
                os.write(file_fd, rewritten)
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
        return real_read(directory_fd, filename)

    monkeypatch.setattr(state, "_read_exact_private_file", rewrite_first_state)

    with pytest.raises(StateError, match="state changed"):
        with state.reserve_new_run(roots, "state-bytes", _cloud_key()) as reservation:
            reservation.write_state("creating", {})

    assert state.read_run_state(state.run_paths(roots, "state-bytes"))["status"] == "failed"


def test_new_run_reservation_serializes_same_name_race(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    first_reserved = threading.Event()
    release_first = threading.Event()
    outcomes: list[str] = []
    backend_side_effects: list[str] = []

    def reserve(marker: str, backend: RuntimeBackend) -> None:
        try:
            with state.reserve_new_run(roots, "race", _cloud_key(backend)) as reservation:
                outcomes.append(f"reserved-{marker}")
                first_reserved.set()
                assert release_first.wait(5)
                backend_side_effects.append(backend.value)
                reservation.write_state("running", {"marker": marker})
        except StateError:
            outcomes.append(f"rejected-{marker}")

    first = threading.Thread(target=reserve, args=("a", RuntimeBackend.KVM))
    second = threading.Thread(target=reserve, args=("b", RuntimeBackend.LIMA_VZ))
    first.start()
    assert first_reserved.wait(5)
    second.start()
    release_first.set()
    first.join(5)
    second.join(5)

    assert len([item for item in outcomes if item.startswith("reserved-")]) == 1
    assert len([item for item in outcomes if item.startswith("rejected-")]) == 1
    assert len(backend_side_effects) == 1
    written = state.read_run_state(state.run_paths(roots, "race"))
    assert written["status"] == "running"
    assert written["backend"] == backend_side_effects[0]


@pytest.mark.parametrize("entry_kind", ["directory", "file", "symlink"])
def test_new_run_reservation_preserves_preexisting_entry_bytes(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    entry = roots.runs / "occupied"
    outside = tmp_path / "outside"
    if entry_kind == "directory":
        entry.mkdir()
        (entry / "marker").write_bytes(b"directory-marker")
    elif entry_kind == "file":
        entry.write_bytes(b"file-marker")
    else:
        outside.mkdir()
        (outside / "marker").write_bytes(b"outside-marker")
        entry.symlink_to(outside, target_is_directory=True)
    before_entry = entry.lstat()
    before_marker = (entry / "marker").read_bytes() if entry_kind == "directory" else None
    before_outside = (outside / "marker").read_bytes() if entry_kind == "symlink" else None

    with pytest.raises(StateError, match="already exists"):
        with state.reserve_new_run(roots, "occupied", _cloud_key()):
            pytest.fail("pre-existing entry was reserved")

    after_entry = entry.lstat()
    assert (after_entry.st_dev, after_entry.st_ino, after_entry.st_size, after_entry.st_mtime_ns) == (
        before_entry.st_dev,
        before_entry.st_ino,
        before_entry.st_size,
        before_entry.st_mtime_ns,
    )
    if before_marker is not None:
        assert (entry / "marker").read_bytes() == before_marker
    if before_outside is not None:
        assert (outside / "marker").read_bytes() == before_outside


def test_new_run_reservation_rejects_lock_symlink_without_run_entry(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"do-not-touch")
    (roots.locks / "locked.lock").symlink_to(outside)

    with pytest.raises(StateError, match="securely lock"):
        with state.reserve_new_run(roots, "locked", _cloud_key()):
            pytest.fail("symlink lock was followed")

    assert outside.read_bytes() == b"do-not-touch"
    assert not (roots.runs / "locked").exists()


def test_new_run_reservation_rejects_lock_hardlink_before_chmod(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"do-not-touch")
    outside.chmod(0o640)
    os.link(outside, roots.locks / "hardlocked.lock")

    with pytest.raises(StateError, match="securely lock"):
        with state.reserve_new_run(roots, "hardlocked", _cloud_key()):
            pytest.fail("hardlinked lock was accepted")

    assert outside.read_bytes() == b"do-not-touch"
    assert state.permission_bits(outside) == 0o640
    assert not (roots.runs / "hardlocked").exists()


def test_new_run_reservation_detects_entry_swap_and_never_deletes_replacement(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    displaced = roots.runs / "displaced"
    replacement = roots.runs / "swapped"
    with pytest.raises(StateError, match="reservation changed"):
        with state.reserve_new_run(roots, "swapped", _cloud_key()) as reservation:
            reservation.write_state("creating", {})
            os.rename(replacement, displaced)
            replacement.mkdir()
            (replacement / "marker").write_bytes(b"replacement")
            reservation.write_state("running", {})

    assert (replacement / "marker").read_bytes() == b"replacement"
    assert state.read_run_state(state.run_paths(roots, "displaced"))["status"] == "failed"


def test_reserved_run_file_write_never_touches_replacement_directory(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    displaced = roots.runs / "file-displaced"
    replacement = roots.runs / "file-swap"

    with pytest.raises(StateError, match="reservation changed"):
        with state.reserve_new_run(roots, "file-swap", _cloud_key()) as reservation:
            reservation.write_state("creating", {})
            os.rename(replacement, displaced)
            replacement.mkdir()
            (replacement / "marker").write_bytes(b"replacement")
            reservation.write_file("lima.yaml", b"vmType: vz\n")

    assert (replacement / "marker").read_bytes() == b"replacement"
    assert not (replacement / "lima.yaml").exists()
    assert state.read_run_state(state.run_paths(roots, "file-displaced"))["status"] == "failed"


def test_reserved_run_file_preserves_preexisting_large_artifact_support(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    content = b"x" * (2 * 1024 * 1024)

    with state.reserve_new_run(roots, "large-config", _cloud_key(RuntimeBackend.LIMA_VZ)) as reservation:
        reservation.write_state("creating", {})
        target = reservation.write_file("lima.yaml", content)
        reservation.write_state("failed", {})

    assert target.read_bytes() == content


def test_staged_artifact_publish_never_touches_replacement_directory(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    (staging / "overlay.qcow2").write_bytes(b"overlay")
    displaced = roots.runs / "stage-displaced"
    replacement = roots.runs / "stage-swap"

    with pytest.raises(StateError, match="reservation changed"):
        with state.reserve_new_run(roots, "stage-swap", _cloud_key()) as reservation:
            reservation.write_state("creating", {})
            os.rename(replacement, displaced)
            replacement.mkdir()
            (replacement / "marker").write_bytes(b"replacement")
            reservation.publish_staging(staging)

    assert (replacement / "marker").read_bytes() == b"replacement"
    assert not (replacement / "overlay.qcow2").exists()
    assert (staging / "overlay.qcow2").read_bytes() == b"overlay"
    assert state.read_run_state(state.run_paths(roots, "stage-displaced"))["status"] == "failed"


def test_new_run_state_publish_replaces_symlink_without_following_it(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    outside = tmp_path / "outside-state"
    outside.write_bytes(b"do-not-touch")

    with state.reserve_new_run(roots, "state-link", _cloud_key()) as reservation:
        reservation.paths.state.symlink_to(outside)
        written = reservation.write_state("creating", {})

    assert outside.read_bytes() == b"do-not-touch"
    assert not reservation.paths.state.is_symlink()
    assert state.read_run_state(reservation.paths) == written


def test_new_run_state_replace_failure_leaves_held_failed_ledger_without_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    real_replace = state.os.replace
    replace_calls = 0

    def fail_first_replace(*args: object, **kwargs: object) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise OSError("injected replace failure")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(state.os, "replace", fail_first_replace)

    with pytest.raises(StateError, match="durably write"):
        with state.reserve_new_run(roots, "replace-failed", _cloud_key()) as reservation:
            reservation.write_state("creating", {})

    rpaths = state.run_paths(roots, "replace-failed")
    owner = state.read_owner_record(rpaths)
    failed = state.read_run_state(rpaths)
    assert failed["schema_version"] == 2
    assert failed["run_id"] == owner.run_id
    assert failed["status"] == "failed"
    assert list(rpaths.root.glob(".state-tmp-*")) == []


def test_new_run_reservation_persists_failed_v2_ledger_after_reservation(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    with pytest.raises(RuntimeError, match="backend failed"):
        with state.reserve_new_run(roots, "failed-create", _cloud_key(RuntimeBackend.LIMA_VZ)) as reservation:
            reservation.write_state("creating", {})
            raise RuntimeError("backend failed")

    failed = state.read_run_state(state.run_paths(roots, "failed-create"))
    assert failed["schema_version"] == 2
    assert failed["runtime_kind"] == "cloud-image"
    assert failed["backend"] == "lima-vz"
    assert failed["name"] == "failed-create"
    assert failed["run_id"] == reservation.record.run_id
    assert failed["status"] == "failed"


def test_new_run_reservation_persists_failed_v2_on_base_exception(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})

    with pytest.raises(KeyboardInterrupt):
        with state.reserve_new_run(roots, "interrupted", _cloud_key()) as reservation:
            reservation.write_state("creating", {})
            raise KeyboardInterrupt

    failed = state.read_run_state(state.run_paths(roots, "interrupted"))
    assert failed["schema_version"] == 2
    assert failed["runtime_kind"] == "cloud-image"
    assert failed["backend"] == "kvm"
    assert failed["status"] == "failed"


def test_new_run_reservation_does_not_fail_after_last_success_boundary(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    displaced_lock = roots.locks / "after-success.displaced"

    with state.reserve_new_run(roots, "after-success", _cloud_key()) as reservation:
        reservation.write_state("running", {})
        os.rename(reservation.paths.lock, displaced_lock)
        reservation.paths.lock.write_bytes(b"replacement")

    assert state.read_run_state(state.run_paths(roots, "after-success"))["status"] == "running"


def test_existing_run_mutation_promotes_legacy_state_once_with_authoritative_identity(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "legacy-mutation")
    owner = state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="stopped", data={"backend": "libvirt-hvf", "guest_ip": None})
    owner_before = rpaths.owner.read_bytes()

    with state.locked_existing_run(roots, "legacy-mutation") as mutation:
        assert mutation.is_legacy is True
        current = mutation.mutable_state()
        written = mutation.write_state("running", {**current, "guest_ip": "192.0.2.8"})
        assert mutation.is_legacy is False

    assert rpaths.owner.read_bytes() == owner_before
    assert state.read_run_state(rpaths) == written
    assert {key: written[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "libvirt-hvf",
        "name": "legacy-mutation",
        "run_id": owner.run_id,
        "status": "running",
    }


def test_existing_run_mutation_exception_preserves_legacy_bytes(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "legacy-failure")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="stopped", data={"backend": "kvm"})
    before = rpaths.state.read_bytes()

    with pytest.raises(KeyboardInterrupt):
        with state.locked_existing_run(roots, "legacy-failure") as mutation:
            mutation.verify_binding()
            raise KeyboardInterrupt

    assert rpaths.state.read_bytes() == before


def test_existing_run_mutation_rejects_identity_smuggling(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "legacy-smuggle")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="stopped", data={"backend": "kvm"})
    before = rpaths.state.read_bytes()

    with state.locked_existing_run(roots, "legacy-smuggle") as mutation:
        with pytest.raises(StateError, match="durable identity"):
            mutation.write_state("running", {**mutation.mutable_state(), "backend": "lima-vz"})

    assert rpaths.state.read_bytes() == before


def test_existing_run_mutation_rejects_stale_canonical_public_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots_a = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg-a"), "XDG_STATE_HOME": str(tmp_path / "st-a")})
    roots_b = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg-b"), "XDG_STATE_HOME": str(tmp_path / "st-b")})
    stale_paths = state.run_paths(roots_a, "parent-swap")
    current_paths = state.run_paths(roots_b, "parent-swap")
    state.write_owner_record(current_paths)
    state.write_run_state(current_paths, status="stopped", data={"backend": "kvm", "marker": "b"})
    monkeypatch.setattr(state, "_new_run_paths", lambda _roots, _name: stale_paths)

    with pytest.raises(StateError, match="changed during lifecycle mutation"):
        with state.locked_existing_run(roots_b, "parent-swap"):
            pytest.fail("stale public paths were accepted")

    assert not stale_paths.root.exists()
    assert state.read_run_state(current_paths)["marker"] == "b"


def test_new_run_reservation_rejects_stale_canonical_public_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots_a = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg-a"), "XDG_STATE_HOME": str(tmp_path / "st-a")})
    roots_b = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg-b"), "XDG_STATE_HOME": str(tmp_path / "st-b")})
    stale_paths = state.run_paths(roots_a, "new-parent-swap")
    monkeypatch.setattr(state, "_new_run_paths", lambda _roots, _name: stale_paths)

    with pytest.raises(StateError, match="changed during create"):
        with state.reserve_new_run(roots_b, "new-parent-swap", _cloud_key()):
            pytest.fail("stale public paths were accepted")

    assert not stale_paths.root.exists()


def test_existing_run_delete_rejects_visible_replacement_without_touching_either_tree(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "delete-swap")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="removed", data={"backend": "kvm"})
    original_state = rpaths.state.read_bytes()
    displaced = roots.runs / "delete-swap-original"

    with pytest.raises(StateError, match="changed during lifecycle"):
        with state.locked_existing_run(roots, "delete-swap") as mutation:
            os.rename(rpaths.root, displaced)
            rpaths.root.mkdir()
            (rpaths.root / "marker").write_bytes(b"replacement")
            mutation.delete_run_tree()

    assert (rpaths.root / "marker").read_bytes() == b"replacement"
    assert (displaced / "state.json").read_bytes() == original_state


def test_existing_run_delete_removes_only_pinned_run_tree(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "delete-exact")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="removed", data={"backend": "kvm"})
    (rpaths.root / "nested").mkdir()
    (rpaths.root / "nested" / "artifact").write_bytes(b"data")

    with state.locked_existing_run(roots, "delete-exact") as mutation:
        mutation.delete_run_tree()

    assert not rpaths.root.exists()


def test_existing_run_delete_supports_symlink_configured_state_root(tmp_path: Path) -> None:
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(real_state, target_is_directory=True)
    roots = state.init_roots(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "PALIMPSEST_STATE_HOME": str(linked_state),
        }
    )
    rpaths = state.run_paths(roots, "delete-linked-root")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="removed", data={"backend": "kvm"})

    with state.locked_existing_run(roots, "delete-linked-root") as mutation:
        mutation.delete_run_tree()

    assert not rpaths.root.exists()
    assert list(roots.run_deletions.resolve().iterdir()) == []


def test_existing_run_append_rejects_hardlink_without_changing_external_inode(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "append-hardlink")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"backend": "lima-vz"})
    external = tmp_path / "external.log"
    external.write_bytes(b"external")
    external.chmod(0o640)
    os.link(external, rpaths.console)

    with state.locked_existing_run(roots, "append-hardlink") as mutation:
        with pytest.raises(StateError, match="securely append"):
            mutation.append_file("console.log", b"new")

    assert external.read_bytes() == b"external"
    assert external.stat().st_mode & 0o777 == 0o640


def test_existing_run_append_rejects_fifo_without_opening_it(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "append-fifo")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"backend": "lima-vz"})
    os.mkfifo(rpaths.console, 0o600)

    with state.locked_existing_run(roots, "append-fifo") as mutation:
        with pytest.raises(StateError, match="securely append"):
            mutation.append_file("console.log", b"new")

    assert stat.S_ISFIFO(rpaths.console.lstat().st_mode)


def test_existing_run_delete_preflights_entire_tree_before_removing_files(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "delete-preflight")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="removed", data={"backend": "kvm"})
    ordinary = rpaths.root / "aaa-artifact"
    ordinary.write_bytes(b"keep")
    os.mkfifo(rpaths.root / "zzz-fifo", 0o600)

    with state.locked_existing_run(roots, "delete-preflight") as mutation:
        with pytest.raises(StateError, match="unsupported filesystem entry"):
            mutation.delete_run_tree()

    assert ordinary.read_bytes() == b"keep"
    assert rpaths.owner.exists()
    assert rpaths.state.exists()


def test_existing_run_delete_restores_replacement_instead_of_unlinking_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "delete-entry-swap")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="removed", data={"backend": "kvm"})
    victim = rpaths.root / ".aaa-victim"
    victim.write_bytes(b"original")
    displaced = rpaths.root / ".aaa-original"
    real_rename = state.os.rename
    swapped = False

    def swap_rename(src, dst, *args, **kwargs):
        nonlocal swapped
        if src == ".aaa-victim" and not swapped:
            swapped = True
            real_rename(src, ".aaa-original", src_dir_fd=kwargs["src_dir_fd"], dst_dir_fd=kwargs["dst_dir_fd"])
            replacement_fd = os.open(
                ".aaa-victim",
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
                dir_fd=kwargs["src_dir_fd"],
            )
            try:
                os.write(replacement_fd, b"replacement")
            finally:
                os.close(replacement_fd)
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(state.os, "rename", swap_rename)

    with state.locked_existing_run(roots, "delete-entry-swap") as mutation:
        with pytest.raises(StateError, match="changed during deletion"):
            mutation.delete_run_tree()

    assert not rpaths.root.exists()
    quarantines = list(roots.run_deletions.glob("delete-entry-swap-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / victim.name).read_bytes() == b"replacement"
    assert (quarantines[0] / displaced.name).read_bytes() == b"original"
    assert (quarantines[0] / "owner.json").exists()


def test_existing_run_delete_failure_never_leaves_partial_public_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")}
    roots = state.init_roots(environment)
    rpaths = state.run_paths(roots, "delete-unlink-failure")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="removed", data={"backend": "kvm"})
    (rpaths.root / "artifact-a").write_bytes(b"a")
    (rpaths.root / "artifact-b").write_bytes(b"b")
    real_unlink = state.os.unlink
    unlink_calls = 0

    def fail_second_unlink(path, *args, **kwargs):
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 2:
            raise OSError("injected unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(state.os, "unlink", fail_second_unlink)

    with state.locked_existing_run(roots, "delete-unlink-failure") as mutation:
        with pytest.raises(StateError, match="securely delete"):
            mutation.delete_run_tree()

    assert not rpaths.root.exists()
    quarantines = list(roots.run_deletions.glob("delete-unlink-failure-*"))
    assert len(quarantines) == 1
    assert quarantines[0].is_dir()

    state.init_roots(environment)

    assert list(roots.run_deletions.iterdir()) == []


def test_init_roots_rejects_run_deletion_symlink_without_chmodding_target(tmp_path: Path) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")}
    roots = state.init_roots(environment)
    roots.run_deletions.rmdir()
    external = tmp_path / "external-deletions"
    external.mkdir(mode=0o755)
    roots.run_deletions.symlink_to(external, target_is_directory=True)

    state.init_roots(environment)

    assert roots.run_deletions.is_symlink()
    assert external.stat().st_mode & 0o777 == 0o755


def test_run_deletion_retry_honors_original_name_lock(tmp_path: Path) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")}
    roots = state.init_roots(environment)
    quarantine = roots.run_deletions / ("locked-retry-" + "a" * 32)
    quarantine.mkdir(mode=0o700)
    (quarantine / "artifact").write_bytes(b"pending")
    started = threading.Event()
    finished = threading.Event()

    def retry() -> None:
        started.set()
        state.init_roots(environment)
        finished.set()

    with state._new_run_name_lock(roots, "locked-retry"):
        worker = threading.Thread(target=retry)
        worker.start()
        assert started.wait(timeout=1)
        worker.join(timeout=0.1)
        assert worker.is_alive()
        assert quarantine.exists()

    worker.join(timeout=2)
    assert finished.is_set()
    assert not quarantine.exists()


def test_existing_run_state_write_restores_legacy_bytes_after_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    rpaths = state.run_paths(roots, "state-fsync-failure")
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="stopped", data={"backend": "kvm"})
    before = rpaths.state.read_bytes()
    real_fsync = state.os.fsync

    with state.locked_existing_run(roots, "state-fsync-failure") as mutation:

        def fail_directory_fsync(file_fd: int) -> None:
            if file_fd == mutation._run_fd:
                raise OSError("injected directory fsync failure")
            real_fsync(file_fd)

        monkeypatch.setattr(state.os, "fsync", fail_directory_fsync)
        with pytest.raises(StateError, match="durably write existing run ledger"):
            mutation.write_state("running", mutation.mutable_state())

    assert rpaths.state.read_bytes() == before


def test_atomic_write_json_syncs_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state" / "record.json"
    synced: list[Path] = []
    monkeypatch.setattr(state, "fsync_directory", lambda path: synced.append(path))

    state.atomic_write_json(target, {"status": "complete"})

    assert synced == [target.parent]
    assert state.read_json(target) == {"status": "complete"}


def test_secret_scanning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)
        rpaths = state.run_paths(roots, "my-run")
        state.write_owner_record(rpaths)

        with pytest.raises(StateError, match="secret-shaped field"):
            state.write_run_state(rpaths, status="running", data={"auth_token": "secret123"})

        with pytest.raises(StateError, match="key-material-shaped string"):
            state.atomic_write_json(rpaths.root / "key.json", {"key": "-----BEGIN PRIVATE KEY-----\nabc\n"})


def test_traversal_and_name_rejection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)

        with pytest.raises(StateError, match="invalid run name"):
            state.run_paths(roots, "../escape")

        with pytest.raises(StateError, match="invalid tag name"):
            state.tag_path(roots, "../../etc/passwd")


def test_tag_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)
        digest = "sha256:" + "a" * 64

        tag_rec = state.TagRecord(
            schema_version=1,
            tag="v1.0",
            digest=digest,
            media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            size_bytes=1024,
            parent_digest=None,
            base_image_digest=None,
            source="pack",
            created_at=state.utc_now_iso(),
        )

        state.write_tag_record(roots, tag_rec)
        read_tag = state.read_tag_record(roots, "v1.0")
        assert read_tag == tag_rec

        # Idempotent write with same identity succeeds
        state.write_tag_record(roots, tag_rec)

        # Conflicting write fails
        conflicting = state.TagRecord(
            schema_version=1,
            tag="v1.0",
            digest="sha256:" + "b" * 64,
            media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            size_bytes=1024,
            parent_digest=None,
            base_image_digest=None,
            source="pack",
            created_at=state.utc_now_iso(),
        )
        with pytest.raises(StateError, match="already maps to a different digest"):
            state.write_tag_record(roots, conflicting)


def test_conflicting_tag_writers_are_serialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    first_entered_write = threading.Event()
    release_first_write = threading.Event()
    original_atomic_write = state.atomic_write_json

    def record(digest_character: str) -> state.TagRecord:
        return state.TagRecord(
            schema_version=1,
            tag="concurrent",
            digest="sha256:" + digest_character * 64,
            media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            size_bytes=1024,
            parent_digest=None,
            base_image_digest=None,
            source="test",
            created_at=state.utc_now_iso(),
        )

    def delayed_atomic_write(path: Path, value: dict[str, object]) -> None:
        if path == state.tag_path(roots, "concurrent") and value.get("digest") == "sha256:" + "a" * 64:
            first_entered_write.set()
            assert release_first_write.wait(5)
        original_atomic_write(path, value)

    monkeypatch.setattr(state, "atomic_write_json", delayed_atomic_write)
    outcomes: list[str] = []

    def write(item: state.TagRecord) -> None:
        try:
            state.write_tag_record(roots, item)
            outcomes.append("success")
        except StateError:
            outcomes.append("conflict")

    first = threading.Thread(target=write, args=(record("a"),))
    second = threading.Thread(target=write, args=(record("b"),))
    first.start()
    assert first_entered_write.wait(5)
    second.start()
    release_first_write.set()
    first.join(5)
    second.join(5)

    assert outcomes.count("success") == 1
    assert outcomes.count("conflict") == 1
    assert state.read_tag_record(roots, "concurrent").digest in {
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    }


def test_transfer_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)
        digest = "sha256:" + "c" * 64

        t_rec = state.TransferRecord(
            schema_version=1,
            digest=digest,
            path_fingerprint="fp123",
            session_id="sess456",
            acknowledged_offset=512,
            updated_at=state.utc_now_iso(),
        )

        state.write_transfer_record(roots, t_rec)
        read_t = state.read_transfer_record(roots, digest)
        assert read_t == t_rec

        records = state.list_transfer_records(roots)
        assert len(records) == 1
        assert records[0] == t_rec

        state.delete_transfer_record(roots, digest)
        with pytest.raises(StateError, match="state file not found"):
            state.read_transfer_record(roots, digest)


def test_state_root_precedence_and_source_reporting(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    default_st = tmp_path / "default_st"
    config_st = tmp_path / "config_st"
    env_st = tmp_path / "env_st"

    env = {
        "XDG_CONFIG_HOME": str(cfg_dir),
        "XDG_STATE_HOME": str(default_st),
    }

    # 1. Default precedence
    assert state.state_root_source(env) == "default"
    roots = state.init_roots(env)
    assert roots.state == default_st / "palimpsest"

    # 2. Config precedence
    state.write_state_root(roots, config_st)
    assert state.state_root_source(env) == "config"
    roots_cfg = state.init_roots(env)
    assert roots_cfg.state == config_st

    # 3. Env precedence over config and default
    env_with_var = dict(env)
    env_with_var["PALIMPSEST_STATE_HOME"] = str(env_st)
    assert state.state_root_source(env_with_var) == "env"
    roots_env = state.init_roots(env_with_var)
    assert roots_env.state == env_st


def test_invalid_relative_palimpsest_state_home(tmp_path: Path) -> None:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_STATE_HOME": str(tmp_path / "st"),
        "PALIMPSEST_STATE_HOME": "relative/path/to/state",
    }
    with pytest.raises(StateError, match="PALIMPSEST_STATE_HOME must be an absolute path"):
        state.init_roots(env)


def test_invalid_relative_config_state_root(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg" / "palimpsest"
    cfg_dir.mkdir(parents=True)
    config_file = cfg_dir / "config.toml"
    config_file.write_text("[storage]\nstate_root = 'relative/path'\n", encoding="utf-8")

    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    with pytest.raises(StateError, match=r"invalid storage\.state_root in"):
        state.init_roots(env)


def test_invalid_toml_config_state_root(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg" / "palimpsest"
    cfg_dir.mkdir(parents=True)
    config_file = cfg_dir / "config.toml"
    config_file.write_text("[storage\nstate_root = ", encoding="utf-8")

    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    with pytest.raises(StateError, match=r"invalid storage\.state_root in"):
        state.init_roots(env)


def test_write_state_root_url_preservation(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    config_file = cfg_dir / "palimpsest" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[hub]\nurl = 'https://hub.example.com'\n", encoding="utf-8")

    env = {"XDG_CONFIG_HOME": str(cfg_dir)}
    roots = state.init_roots(env)

    new_st = tmp_path / "new_state"
    state.write_state_root(roots, new_st)

    content = config_file.read_text(encoding="utf-8")
    assert "[hub]" in content
    assert 'url = "https://hub.example.com"' in content
    assert "[storage]" in content
    assert f'state_root = "{new_st}"' in content

    reloaded_roots = state.init_roots(env)
    assert reloaded_roots.state == new_st


def test_write_state_root_requires_absolute_path(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    with pytest.raises(StateError, match="state root destination must be an absolute path"):
        state.write_state_root(roots, Path("relative/dest"))
