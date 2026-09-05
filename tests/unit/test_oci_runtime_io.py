"""Trusted parent / untrusted QEMU I/O separation and durable inode binding."""

from __future__ import annotations

import errno
import os
import socket
import stat
from contextlib import contextmanager
from dataclasses import replace

import pytest

from palimpsest_local import oci_runtime_io as runtime_io
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind

PLAN = "sha256:" + "a" * 64


@pytest.fixture
def case(tmp_path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "c"), "XDG_STATE_HOME": str(tmp_path / "s")})
    with state.reserve_new_run(roots, "vm", DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)) as reservation:
        reservation.write_state("creating", {})
    return roots, state.run_paths(roots, "vm")


def _commit(case):
    roots, _ = case
    with state.locked_existing_run(roots, "vm") as mutation:
        with runtime_io.runtime_io_guard(mutation, plan_digest=PLAN, create=True) as guard:
            receipt = guard.receipt
            mutation.write_state("creating", {**mutation.mutable_state(), "oci_runtime_io": receipt.to_dict()})
    return receipt


@contextmanager
def _open(case, **kwargs):
    with state.locked_existing_run(case[0], "vm") as mutation:
        with runtime_io.runtime_io_guard(mutation, plan_digest=kwargs.pop("plan_digest", PLAN), **kwargs) as guard:
            yield guard


def test_exclusive_creation_persists_exact_receipt_and_preserves_parent(case):
    _, paths = case
    parent = paths.root.stat()
    receipt = _commit(case)
    io = runtime_io.runtime_io_paths(paths.root)
    assert io.root.name == "io"
    assert io.lifecycle_socket == io.root / "lifecycle.sock"
    assert io.console_log == io.root / "console.log"
    assert stat.S_IMODE(io.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(io.console_log.stat().st_mode) == 0o600
    assert io.console_log.read_bytes() == b""
    assert not io.lifecycle_socket.exists()
    assert (paths.root.stat().st_ino, paths.root.stat().st_mode) == (parent.st_ino, parent.st_mode)
    assert runtime_io.RuntimeIOReceipt.from_dict(receipt.to_dict()) == receipt
    assert all(not isinstance(value, str) or "/" not in value for value in receipt.to_dict().values())
    with _open(case, require_socket_absent=True) as guard:
        assert guard.receipt == receipt
        assert os.fstat(guard.directory_fd).st_ino == receipt.directory_inode
    with pytest.raises(StateError):
        guard.verify()
    with pytest.raises(StateError):
        _ = guard.directory_fd


def test_console_append_does_not_invalidate_guard(case):
    _commit(case)
    with _open(case) as guard:
        with guard.paths.console_log.open("ab") as output:
            output.write(b"untrusted QEMU output\n")
        guard.verify(require_socket_absent=True)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_creation_never_adopts_or_modifies_existing_child(case, tmp_path, kind):
    io = runtime_io.runtime_io_paths(case[1].root)
    if kind == "directory":
        io.root.mkdir(mode=0o700)
        (io.root / "foreign").write_bytes(b"keep")
    elif kind == "file":
        io.root.write_bytes(b"keep")
    else:
        io.root.symlink_to(tmp_path, target_is_directory=True)
    before = io.root.lstat()
    with pytest.raises(StateError):
        _commit(case)
    assert io.root.lstat() == before
    if kind == "directory":
        assert (io.root / "foreign").read_bytes() == b"keep"
        assert not io.console_log.exists()


def test_failed_creation_is_preserved_and_not_adopted_on_retry(case, monkeypatch):
    original = os.fsync
    calls = 0

    def fail(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("durability failure")
        original(fd)

    # Enter the already acquired run lock before injecting the I/O fsync fault.
    with state.locked_existing_run(case[0], "vm") as mutation:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail)
            with pytest.raises(StateError):
                with runtime_io.runtime_io_guard(mutation, plan_digest=PLAN, create=True):
                    pytest.fail("undurable receipt yielded")
    io = runtime_io.runtime_io_paths(case[1].root)
    before = io.console_log.stat()
    assert "oci_runtime_io" not in state.read_run_state(case[1])
    with pytest.raises(StateError):
        _commit(case)
    assert io.console_log.stat() == before


def test_creation_fsync_order_precedes_receipt_publication(case, monkeypatch):
    original = os.fsync
    seen = []
    with state.locked_existing_run(case[0], "vm") as mutation:
        with monkeypatch.context() as patch:

            def observe(fd):
                seen.append(os.fstat(fd).st_ino)
                original(fd)

            patch.setattr(os, "fsync", observe)
            with runtime_io.runtime_io_guard(mutation, plan_digest=PLAN, create=True) as guard:
                assert seen == [
                    guard.receipt.console_inode,
                    guard.receipt.directory_inode,
                    os.fstat(mutation._run_fd).st_ino,
                ]
                assert "oci_runtime_io" not in mutation.snapshot.state


def test_racing_console_symlink_is_not_followed_or_removed(case, monkeypatch, tmp_path):
    target = tmp_path / "foreign"
    target.write_bytes(b"do not truncate")
    original = os.open

    def race(path, flags, *args, **kwargs):
        fd = original(path, flags, *args, **kwargs)
        if path == runtime_io.OCI_RUNTIME_DIRECTORY:
            os.symlink(target, runtime_io.OCI_RUNTIME_CONSOLE_FILENAME, dir_fd=fd)
        return fd

    with state.locked_existing_run(case[0], "vm") as mutation:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", race)
            with pytest.raises(StateError):
                with runtime_io.runtime_io_guard(mutation, plan_digest=PLAN, create=True):
                    pass
    assert target.read_bytes() == b"do not truncate"
    assert runtime_io.runtime_io_paths(case[1].root).console_log.is_symlink()


def test_invalid_created_directory_is_rejected_before_console_write(case, monkeypatch):
    mkdir = os.mkdir

    def race(path, mode=0o777, *, dir_fd=None):
        mkdir(path, mode, dir_fd=dir_fd)
        if path == runtime_io.OCI_RUNTIME_DIRECTORY:
            os.chmod(path, 0o730, dir_fd=dir_fd)

    with state.locked_existing_run(case[0], "vm") as mutation:
        with monkeypatch.context() as patch:
            patch.setattr(os, "mkdir", race)
            with pytest.raises(StateError):
                with runtime_io.runtime_io_guard(mutation, plan_digest=PLAN, create=True):
                    pass
    io = runtime_io.runtime_io_paths(case[1].root)
    assert io.root.exists() and not io.console_log.exists()


def test_directory_swap_at_open_does_not_adopt_or_write_replacement(case, monkeypatch):
    io = runtime_io.runtime_io_paths(case[1].root)
    original = os.open

    def replace_directory(path, flags, *args, **kwargs):
        if path == runtime_io.OCI_RUNTIME_DIRECTORY:
            io.root.rename(io.root.with_name("original-io"))
            io.root.mkdir(mode=0o700)
            (io.root / "foreign").write_bytes(b"keep")
        return original(path, flags, *args, **kwargs)

    with state.locked_existing_run(case[0], "vm") as mutation:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", replace_directory)
            with pytest.raises(StateError):
                with runtime_io.runtime_io_guard(mutation, plan_digest=PLAN, create=True):
                    pass
    assert (io.root / "foreign").read_bytes() == b"keep"
    assert not io.console_log.exists()
    assert not (io.root.with_name("original-io") / "console.log").exists()


@pytest.mark.parametrize("receipt", [None, {}, {"schema": "foreign"}])
def test_invalid_receipt_is_rejected_before_endpoint_open(case, monkeypatch, receipt):
    _commit(case)
    with state.locked_existing_run(case[0], "vm") as mutation:
        current = mutation.mutable_state()
        current["oci_runtime_io"] = receipt
        mutation.write_state("creating", current)
        original = os.open

        def forbidden(path, *args, **kwargs):
            assert path not in {runtime_io.OCI_RUNTIME_DIRECTORY, runtime_io.OCI_RUNTIME_CONSOLE_FILENAME}
            return original(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", forbidden)
            with pytest.raises(StateError):
                with runtime_io.runtime_io_guard(mutation, plan_digest=PLAN):
                    pass


def test_replaced_console_is_rejected_before_open(case, monkeypatch):
    _commit(case)
    console = runtime_io.runtime_io_paths(case[1].root).console_log
    console.rename(console.with_name("original-console"))
    console.touch(mode=0o600)
    original = os.open

    def forbidden(path, *args, **kwargs):
        assert path != runtime_io.OCI_RUNTIME_CONSOLE_FILENAME
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", forbidden)
        with pytest.raises(StateError):
            with _open(case):
                pass


def test_exception_closes_guard_without_removing_evidence(case):
    _commit(case)
    with pytest.raises(RuntimeError, match="launch failed"):
        with _open(case) as guard:
            held_fd = guard.directory_fd
            raise RuntimeError("launch failed")
    with pytest.raises(OSError):
        os.fstat(held_fd)
    assert guard.paths.root.exists() and guard.paths.console_log.exists()


def test_forked_guard_refuses_use_and_cleanup_preserves_reused_descriptor(case):
    if not hasattr(os, "fork"):
        pytest.skip("requires fork")
    _commit(case)
    with state.locked_existing_run(case[0], "vm") as mutation:
        context = runtime_io.runtime_io_guard(mutation, plan_digest=PLAN)
        guard = context.__enter__()
        held_fd = guard.directory_fd
        child = os.fork()
        if child == 0:
            try:
                assert guard._descriptors.directory == guard._descriptors.console == -1
                for operation in (guard.verify, lambda: guard.directory_fd):
                    try:
                        operation()
                    except StateError:
                        pass
                    else:
                        raise AssertionError("inherited authority remained usable")
                descriptor = os.open(os.devnull, os.O_RDONLY)
                if descriptor != held_fd:
                    os.dup2(descriptor, held_fd)
                    os.close(descriptor)
                try:
                    context.__exit__(None, None, None)
                except StateError:
                    pass
                else:
                    raise AssertionError("child context exit validated inherited authority")
                os.fstat(held_fd)
                os.close(held_fd)
                os._exit(0)
            except BaseException:
                os._exit(1)
        try:
            _, status = os.waitpid(child, 0)
            assert os.waitstatus_to_exitcode(status) == 0
            guard.verify()
        finally:
            context.__exit__(None, None, None)


def test_descriptor_cleanup_does_not_close_replacement_from_earlier_child_hook(case):
    _commit(case)
    with state.locked_existing_run(case[0], "vm") as mutation:
        context = runtime_io.runtime_io_guard(mutation, plan_digest=PLAN)
        guard = context.__enter__()
        held_fd = guard.directory_fd
        os.close(held_fd)
        replacement = os.open(os.devnull, os.O_RDONLY)
        if replacement != held_fd:
            os.dup2(replacement, held_fd)
            os.close(replacement)
        try:
            # Simulate another child hook closing/reusing an original FD before
            # this module's child hook runs; only the original inode may close.
            runtime_io._FORK_LOCK.acquire()
            runtime_io._close_inherited_descriptors()
            os.fstat(held_fd)
            with pytest.raises(StateError):
                context.__exit__(None, None, None)
            os.fstat(held_fd)
        finally:
            os.close(held_fd)


@pytest.mark.parametrize("target", ["directory", "console"])
def test_replaced_inode_is_rejected_across_invocations(case, target):
    _commit(case)
    io = runtime_io.runtime_io_paths(case[1].root)
    path = io.root if target == "directory" else io.console_log
    path.rename(path.with_name(path.name + "-original"))
    if target == "directory":
        path.mkdir(mode=0o700)
        io.console_log.touch(mode=0o600)
    else:
        path.touch(mode=0o600)
    before = path.stat()
    with pytest.raises(StateError):
        with _open(case):
            pass
    assert path.stat() == before


@pytest.mark.parametrize("target", ["directory", "console"])
def test_held_guard_rejects_rebound_entries(case, target):
    _commit(case)
    with pytest.raises(StateError):
        with _open(case) as guard:
            path = guard.paths.root if target == "directory" else guard.paths.console_log
            path.rename(path.with_name(path.name + "-original"))
            if target == "directory":
                path.mkdir(mode=0o700)
            else:
                path.touch(mode=0o600)
            guard.verify()


@pytest.mark.parametrize("target", ["directory", "console"])
def test_symlink_replacements_are_never_followed(case, target):
    _commit(case)
    io = runtime_io.runtime_io_paths(case[1].root)
    path = io.root if target == "directory" else io.console_log
    original = path.with_name(path.name + "-original")
    path.rename(original)
    path.symlink_to(original, target_is_directory=target == "directory")
    with pytest.raises(StateError):
        with _open(case):
            pass
    assert path.is_symlink()


@pytest.mark.parametrize(
    "target,mode", [("directory", 0o730), ("directory", 0o755), ("console", 0o660), ("console", 0o644)]
)
def test_qemu_acl_mask_or_other_mode_is_not_implicitly_authorized(case, target, mode):
    _commit(case)
    io = runtime_io.runtime_io_paths(case[1].root)
    (io.root if target == "directory" else io.console_log).chmod(mode)
    with pytest.raises(StateError):
        with _open(case):
            pass


@pytest.mark.parametrize("which", ["run", "runs"])
def test_trusted_parent_write_permissions_are_rejected(case, which):
    path = case[1].root if which == "run" else case[0].runs
    path.chmod(0o730)
    with pytest.raises(StateError):
        _commit(case)
    assert not runtime_io.runtime_io_paths(case[1].root).root.exists()


def test_console_hardlink_is_rejected(case):
    _commit(case)
    io = runtime_io.runtime_io_paths(case[1].root)
    os.link(io.console_log, io.root / "extra")
    with pytest.raises(StateError):
        with _open(case):
            pass
    assert io.console_log.stat().st_nlink == 2


def test_console_fifo_is_rejected_without_blocking(case):
    _commit(case)
    path = runtime_io.runtime_io_paths(case[1].root).console_log
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(StateError):
        with _open(case):
            pass
    assert stat.S_ISFIFO(path.lstat().st_mode)


@pytest.mark.parametrize("kind", ["file", "symlink", "socket"])
def test_any_existing_lifecycle_endpoint_is_rejected_prelaunch(case, kind):
    _commit(case)
    io = runtime_io.runtime_io_paths(case[1].root)
    sock = None
    if kind == "file":
        io.lifecycle_socket.touch()
    elif kind == "symlink":
        io.lifecycle_socket.symlink_to("missing-target")
    else:
        # AF_UNIX's pathname limit is unrelated to directory authority here.
        sock = socket.socket(socket.AF_UNIX)
        fd = os.open(io.root, os.O_RDONLY | os.O_DIRECTORY)
        old = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fchdir(fd)
            try:
                sock.bind("lifecycle.sock")
            except OSError as exc:
                sock.close()
                if exc.errno in {errno.EPERM, errno.EACCES}:
                    pytest.skip("sandbox does not allow AF_UNIX bind")
                raise
        finally:
            os.fchdir(old)
            os.close(old)
            os.close(fd)
    try:
        with pytest.raises(StateError, match="reserved"):
            with _open(case, require_socket_absent=True):
                pass
        with _open(case):
            pass  # Endpoint existence is expected once QEMU has started.
        assert io.lifecycle_socket.lstat()
    finally:
        if sock is not None:
            sock.close()


@pytest.mark.parametrize(
    "field,value", [("plan_digest", "sha256:" + "b" * 64), ("run_name", "other"), ("directory_inode", True)]
)
def test_receipt_binding_drift_is_rejected(case, field, value):
    _commit(case)
    with state.locked_existing_run(case[0], "vm") as mutation:
        current = mutation.mutable_state()
        current["oci_runtime_io"][field] = value
        mutation.write_state("creating", current)
    with pytest.raises(StateError):
        with _open(case):
            pass


def test_missing_receipt_cannot_adopt_existing_io(case):
    _commit(case)
    with state.locked_existing_run(case[0], "vm") as mutation:
        current = mutation.mutable_state()
        del current["oci_runtime_io"]
        mutation.write_state("creating", current)
    with pytest.raises(StateError):
        with _open(case):
            pass


def test_foreign_owner_metadata_is_rejected(case):
    receipt = _commit(case)
    io = runtime_io.runtime_io_paths(case[1].root)
    directory, console = io.root.stat(), io.console_log.stat()
    for which in ("directory", "console"):
        changed = list(directory if which == "directory" else console)
        changed[4] += 1
        with pytest.raises(StateError):
            runtime_io._validate_runtime_io_metadata(
                os.stat_result(changed) if which == "directory" else directory,
                os.stat_result(changed) if which == "console" else console,
                receipt,
            )


def test_invalid_plan_is_rejected_before_creation(case):
    with pytest.raises(StateError):
        with _open(case, create=True, plan_digest="not-a-digest"):
            pass
    assert not runtime_io.runtime_io_paths(case[1].root).root.exists()


def test_receipt_rejects_extra_fields_and_noncanonical_uuid(case):
    receipt = _commit(case)
    with pytest.raises(StateError):
        runtime_io.RuntimeIOReceipt.from_dict({**receipt.to_dict(), "path": "/untrusted"})
    with pytest.raises(StateError):
        replace(receipt, run_id=receipt.run_id.upper())
