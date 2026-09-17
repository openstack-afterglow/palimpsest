"""Root constructors preserve managed traversal and never repair foreign authority."""

import os
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import pytest
import test_oci_runtime_access as access_tests

from palimpsest_local import oci_runtime_access as access
from palimpsest_local import oci_shared_traversal as shared
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import LinuxFdACLBackend, OCIACLError, baseline_acl

case = access_tests.case


def _roots(tmp_path):
    return state.StatePaths(tmp_path / "config", tmp_path / "state")


@pytest.fixture
def managed(case, monkeypatch):  # noqa: F811
    backend = case.backend
    original_write = backend.write_acl

    def write(fd, acl):
        if acl.named_users and acl.named_users[0][1] == "--x":
            info = os.fstat(fd)
            backend.acls[info.st_dev, info.st_ino] = acl
            backend.writes.append(("traversal", acl))
            os.fchmod(fd, 0o710)
            return backend.read_acl(fd)
        return original_write(fd, acl)

    monkeypatch.setattr(backend, "write_acl", write)
    monkeypatch.setattr(shared, "LinuxFdACLBackend", lambda: backend)
    access.grant_oci_runtime_access(case.roots, case.binding, conn=case.conn)
    member = shared.join_oci_shared_traversal(case.roots, case.binding, conn=case.conn, acl_backend=backend)
    return case, member


def test_unmanaged_initialization_is_repeatable_and_does_not_need_acl_tools(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    monkeypatch.setattr(shared, "LinuxFdACLBackend", lambda: pytest.fail("unmanaged initialization needs no ACL tools"))
    assert state.init_resolved_roots(roots) is roots
    identity = [(path.stat().st_dev, path.stat().st_ino) for path in (roots.state, roots.runs)]
    assert state.init_resolved_roots(roots) is roots
    assert identity == [(path.stat().st_dev, path.stat().st_ino) for path in (roots.state, roots.runs)]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (roots.state, roots.runs, roots.store))


@pytest.mark.parametrize("role", ["state", "runs", "locks"])
@pytest.mark.parametrize("mode", [0o710, 0o750, 0o770])
def test_unmanaged_owner_directory_is_repaired_without_replacement(tmp_path, monkeypatch, role, mode):
    roots = state.init_resolved_roots(_roots(tmp_path))
    path = getattr(roots, role)
    path.chmod(mode)
    before = path.stat(follow_symlinks=False)
    identity = before.st_dev, before.st_ino
    mutations = []
    original_chmod = os.fchmod
    original_fsync = os.fsync

    def chmod(fd, permissions):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            mutations.append(("chmod", permissions))
        return original_chmod(fd, permissions)

    def fsync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            mutations.append(("fsync", None))
        return original_fsync(fd)

    monkeypatch.setattr(os, "fchmod", chmod)
    monkeypatch.setattr(os, "fsync", fsync)
    assert state.init_resolved_roots(roots) is roots
    after = path.stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino, after.st_uid, after.st_gid) == (*identity, before.st_uid, before.st_gid)
    assert stat.S_IMODE(after.st_mode) == 0o700
    assert mutations == [("chmod", 0o700), ("fsync", None)]


@pytest.mark.parametrize("role", ["state", "runs", "locks", "store"])
def test_symlink_root_is_rejected_without_chmod_target(tmp_path, role):
    roots = state.init_resolved_roots(_roots(tmp_path))
    path = getattr(roots, role)
    original = path.with_name(path.name + "-original")
    path.rename(original)
    path.symlink_to(original, target_is_directory=True)
    before = original.stat()
    with pytest.raises(StateError):
        state.init_resolved_roots(roots)
    after = original.stat()
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
    assert path.is_symlink()


def test_managed_constructor_preserves_traversal_and_other_roots_private(managed):
    case, _ = managed
    before = dict(case.backend.acls)
    writes = list(case.backend.writes)
    case.roots.store.chmod(0o750)
    for _ in range(3):
        assert state.init_resolved_roots(case.roots) is case.roots
    assert case.backend.acls == before
    assert case.backend.writes == writes
    assert [stat.S_IMODE(p.stat().st_mode) for p in (case.roots.state, case.roots.runs)] == [0o710, 0o710]
    assert stat.S_IMODE(case.roots.store.stat().st_mode) == 0o700


def _empty_managed_epoch(case, member):
    ns = shared._Namespace(case.roots, locked=True)
    try:
        registry = dict(ns.registry)
        registry["members"] = {shared._key(member): replace(member, phase="left").to_dict()}
        for role in ("state", "runs", "root_volumes"):
            case.backend.write_acl(ns.fds[role], baseline_acl(directory=True))
        ns.write(registry)
    finally:
        ns.close()


def test_empty_managed_epoch_still_checks_full_acl(managed):
    case, member = managed
    _empty_managed_epoch(case, member)
    state.init_resolved_roots(case.roots)
    info = case.roots.runs.stat()
    case.backend.acls[info.st_dev, info.st_ino] = member.runs.granted
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    assert stat.S_IMODE(case.roots.runs.stat().st_mode) == 0o700


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("role", ["state", "runs", "locks"])
def test_managed_mode_drift_is_not_repaired(managed, empty, role):
    case, member = managed
    if empty:
        _empty_managed_epoch(case, member)
    path = getattr(case.roots, role)
    mode = 0o750 if empty or role == "locks" else 0o700
    path.chmod(mode)
    before = path.stat(follow_symlinks=False)
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    after = path.stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino, after.st_mode) == (before.st_dev, before.st_ino, before.st_mode)


@pytest.mark.parametrize("role", ["state", "runs"])
def test_managed_acl_drift_is_not_repaired(managed, role):
    case, _ = managed
    info = getattr(case.roots, role).stat()
    identity = info.st_dev, info.st_ino
    case.backend.acls[identity] = baseline_acl(directory=True)
    writes = list(case.backend.writes)
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    assert case.backend.writes == writes
    assert stat.S_IMODE(getattr(case.roots, role).stat().st_mode) == 0o710


def test_private_config_alias_cannot_chmod_managed_state(managed):
    case, _ = managed
    roots = state.StatePaths(case.roots.state, case.roots.state)
    with pytest.raises(StateError, match="private root"):
        state.init_resolved_roots(roots)
    assert stat.S_IMODE(roots.state.stat().st_mode) == 0o710


def test_private_initialization_occurs_inside_guard_cleanup_outside(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    active = False
    real_guard = shared.shared_traversal_initialization
    real_private = state._initialize_private_root

    @contextmanager
    def guard(roots):
        nonlocal active
        with real_guard(roots):
            active = True
            try:
                yield
            finally:
                active = False

    def private(path, protected):
        assert active
        real_private(path, protected)

    def cleanup(roots):
        assert not active

    monkeypatch.setattr(shared, "shared_traversal_initialization", guard)
    monkeypatch.setattr(state, "_initialize_private_root", private)
    monkeypatch.setattr(state, "_retry_run_deletion_quarantines", cleanup)
    state.init_resolved_roots(roots)


def test_managed_constructor_waits_for_namespace_mutation(managed, monkeypatch):
    case, _ = managed
    attempting = threading.Event()
    initialized = threading.Event()
    original_private = state._initialize_private_root
    original_flock = shared.fcntl.flock

    def flock(fd, operation):
        attempting.set()
        return original_flock(fd, operation)

    def private(path, protected):
        initialized.set()
        return original_private(path, protected)

    ns = shared._Namespace(case.roots, locked=True)
    try:
        monkeypatch.setattr(shared.fcntl, "flock", flock)
        monkeypatch.setattr(state, "_initialize_private_root", private)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(state.init_resolved_roots, case.roots)
            try:
                assert attempting.wait(5)
                assert not initialized.is_set()
            finally:
                ns.close()
            assert future.result(timeout=5) is case.roots
    finally:
        ns.close()
    assert initialized.is_set()


@pytest.mark.parametrize("role", ["state", "runs"])
@pytest.mark.parametrize("acl_problem", ["extra", "default"])
def test_unmanaged_mode_only_init_does_not_authorize_first_shared_join(case, monkeypatch, role, acl_problem):  # noqa: F811
    # An opaque ACL may retain mode700. Generic portable init is not a grant:
    # Linux's full ACL reader must still reject it at the first shared join.
    path = getattr(case.roots, role)
    info = path.stat()
    identity = info.st_dev, info.st_ino
    original_read = case.backend.read_acl

    def read(fd):
        observed = os.fstat(fd)
        if (observed.st_dev, observed.st_ino) == identity:
            if acl_problem == "default":
                raise OCIACLError("unsupported ACL")
            return object()
        return original_read(fd)

    monkeypatch.setattr(case.backend, "read_acl", read)
    state.init_resolved_roots(case.roots)
    access.grant_oci_runtime_access(case.roots, case.binding, conn=case.conn)
    writes = list(case.backend.writes)
    with pytest.raises(StateError):
        shared.join_oci_shared_traversal(case.roots, case.binding, conn=case.conn, acl_backend=case.backend)
    assert case.backend.writes == writes
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert not (case.roots.locks / shared._REGISTRY).exists()


def test_private_directory_swap_before_open_is_not_chmodded(tmp_path, monkeypatch):
    roots = state.init_resolved_roots(_roots(tmp_path))
    roots.store.chmod(0o750)
    original_open = os.open
    saved = roots.store.with_name("store-original")

    def open_swapped(path, flags, *args, **kwargs):
        if path == roots.store:
            roots.store.rename(saved)
            roots.store.mkdir(mode=0o750)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_swapped)
    with pytest.raises(StateError, match="changed"):
        state.init_resolved_roots(roots)
    assert stat.S_IMODE(saved.stat().st_mode) == 0o750
    assert stat.S_IMODE(roots.store.stat().st_mode) == 0o750


@pytest.mark.parametrize("role", ["state", "runs"])
def test_managed_inode_replacement_is_not_adopted(managed, role):
    case, _ = managed
    path = getattr(case.roots, role)
    saved = path.with_name(path.name + "-original")
    path.rename(saved)
    path.mkdir(mode=0o700)
    if role == "state":
        # Keep the old trusted registry visible. Replacing the entire namespace
        # and its registry outside an operation is not a same-UID boundary.
        (saved / "locks").rename(path / "locks")
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    assert stat.S_IMODE(saved.stat().st_mode) == 0o710
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_held_managed_directory_swap_during_initialization_is_rejected(managed, monkeypatch):
    case, _ = managed
    original_private = state._initialize_private_root
    saved = case.roots.runs.with_name("runs-original")
    swapped = False

    def private(path, protected):
        nonlocal swapped
        original_private(path, protected)
        if not swapped:
            swapped = True
            case.roots.runs.rename(saved)
            case.roots.runs.mkdir(mode=0o700)

    monkeypatch.setattr(state, "_initialize_private_root", private)
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    assert stat.S_IMODE(saved.stat().st_mode) == 0o710
    assert stat.S_IMODE(case.roots.runs.stat().st_mode) == 0o700


@pytest.mark.skipif(
    sys.platform != "linux" or not all(os.access(path, os.X_OK) for path in ("/usr/bin/getfacl", "/usr/bin/setfacl")),
    reason="native Linux ACL tools required",
)
def test_native_managed_initialization_preserves_exact_acl(managed, monkeypatch):
    case, member = managed
    backend = LinuxFdACLBackend()
    paths = (case.roots.state, case.roots.runs, case.roots.oci_root_volumes)
    targets = (member.state, member.runs, member.root_volumes)
    fds = [os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) for path in paths]
    try:
        for fd, target in zip(fds, targets, strict=True):
            os.fchmod(fd, 0o700)
            backend.write_acl(fd, target.granted)
        monkeypatch.setattr(shared, "LinuxFdACLBackend", lambda: backend)
        state.init_resolved_roots(case.roots)
        assert [backend.read_acl(fd) for fd in fds] == [target.granted for target in targets]
    finally:
        for fd in fds:
            try:
                backend.write_acl(fd, baseline_acl(directory=True))
            finally:
                os.close(fd)
