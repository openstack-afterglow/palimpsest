import hashlib
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import oci_host as host
from palimpsest_local._oci_stage1_kvm_proof import _REQUIRED_KERNEL_CONFIG
from palimpsest_local.errors import StateError
from palimpsest_local.state import StatePaths


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def config(tmp_path):
    kernel = tmp_path / "kernel"
    kernel.write_bytes(b"\0" * 0x202 + b"HdrS" + b"kernel")
    cfg = tmp_path / "config"
    cfg.write_text("\n".join(key + "=y" for key in _REQUIRED_KERNEL_CONFIG))
    return host.OCIHostConfig(kernel, digest(kernel.read_bytes()), cfg, digest(cfg.read_bytes()), tmp_path / "packer")


def test_explicit_environment_roundtrip(config):
    env = {
        "PALIMPSEST_OCI_KERNEL": str(config.kernel),
        "PALIMPSEST_OCI_KERNEL_DIGEST": config.kernel_digest,
        "PALIMPSEST_OCI_KERNEL_CONFIG": str(config.kernel_config),
        "PALIMPSEST_OCI_KERNEL_CONFIG_DIGEST": config.kernel_config_digest,
        "PALIMPSEST_OCI_PACKER": str(config.packer),
    }
    assert host.OCIHostConfig.from_environment(env) == config
    for key in env:
        with pytest.raises(StateError, match=key):
            host.OCIHostConfig.from_environment({k: v for k, v in env.items() if k != key})


@pytest.mark.parametrize("value", ["", "sha256:ABC", "x" * 64, True])
def test_invalid_digest(config, value):
    with pytest.raises(StateError):
        host.OCIHostConfig(config.kernel, value, config.kernel_config, config.kernel_config_digest, config.packer)


def simple_ancestors(monkeypatch, target, *, mode=0o711, acl=b"user::rwx\ngroup::--x\nother::--x\n\n"):
    original = os.fstat
    inode = target.stat().st_ino

    def metadata(fd):
        info = original(fd)
        values = {key: getattr(info, key) for key in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_ctime_ns")}
        if info.st_ino != inode:
            values["st_mode"] = stat.S_IFDIR | 0o755
        elif mode is not None:
            values["st_mode"] = stat.S_IFDIR | mode
        return SimpleNamespace(**values)

    monkeypatch.setattr(host.os, "fstat", metadata)
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=acl))


def changed_stat(info, **changes):
    values = {key: getattr(info, key) for key in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_ctime_ns")}
    values.update(changes)
    return SimpleNamespace(**values)


def test_search_chain_does_not_change_any_permissions(tmp_path, monkeypatch):
    before = tmp_path.stat()
    simple_ancestors(monkeypatch, tmp_path)
    host.verify_runtime_parent(tmp_path)
    assert tmp_path.stat() == before


@pytest.mark.parametrize("mode", [0o700, 0o777, 0o731, 0o701, 0o611])
def test_private_or_writable_ancestors_refused(tmp_path, monkeypatch, mode):
    simple_ancestors(monkeypatch, tmp_path, mode=mode)
    with pytest.raises(StateError):
        host.verify_runtime_parent(tmp_path)


@pytest.mark.parametrize(
    "acl",
    [
        b"user::rwx\nuser:64055:---\ngroup::---\nmask::r-x\nother::--x\n",
        b"user::rwx\ngroup::---\nother::---\n",
        b"user::rwx\ngroup::---\nother::--x\ndefault:user::rwx\n",
        b"\xff",
    ],
)
def test_ambiguous_or_denying_acl_refused(tmp_path, monkeypatch, acl):
    simple_ancestors(monkeypatch, tmp_path, acl=acl)
    with pytest.raises(StateError):
        host.verify_runtime_parent(tmp_path)


def test_symlink_ancestor_refused(tmp_path, monkeypatch):
    simple_ancestors(monkeypatch, tmp_path)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(StateError):
        host.verify_runtime_parent(link)


def test_later_acl_callback_cannot_change_previously_verified_ancestor(tmp_path, monkeypatch):
    simple_ancestors(monkeypatch, tmp_path)
    fake_stat = host.os.fstat
    original_stat = os.stat("/")
    target_inode = tmp_path.stat().st_ino
    changed = False

    def metadata(fd):
        info = fake_stat(fd)
        if changed and info.st_ino == original_stat.st_ino:
            info.st_ctime_ns += 1
        return info

    def callback(*args, **kwargs):
        nonlocal changed
        if fake_stat(kwargs["pass_fds"][0]).st_ino == target_inode:
            changed = True
        return SimpleNamespace(returncode=0, stdout=b"user::rwx\ngroup::--x\nother::--x\n")

    monkeypatch.setattr(host.os, "fstat", metadata)
    monkeypatch.setattr(host.subprocess, "run", callback)
    with pytest.raises(StateError, match="changed"):
        host.verify_runtime_parent(tmp_path)


@pytest.mark.parametrize(
    ("attribute", "name"),
    [
        ("st_dev", "dev"),
        ("st_ino", "ino"),
        ("st_mode", "mode"),
        ("st_uid", "uid"),
        ("st_ctime_ns", "ctime"),
    ],
)
def test_post_acl_change_identifies_depth_and_allowlisted_field(tmp_path, monkeypatch, attribute, name):
    simple_ancestors(monkeypatch, tmp_path)
    verified_fstat = host.os.fstat
    target_inode = tmp_path.stat().st_ino
    calls = 0

    def metadata(fd):
        nonlocal calls
        info = verified_fstat(fd)
        if info.st_ino == target_inode:
            calls += 1
            if calls == 2:
                return changed_stat(info, **{attribute: getattr(info, attribute) + 1})
        return info

    monkeypatch.setattr(host.os, "fstat", metadata)
    depth = len(tmp_path.parts) - 1
    with pytest.raises(StateError) as raised:
        host.verify_runtime_parent(tmp_path)
    assert str(raised.value) == (
        f"OCI runtime ancestor changed during verification: phase=post-acl depth={depth} changed={name}"
    )


def test_real_direct_child_creation_reports_post_acl_ctime_change(tmp_path, monkeypatch):
    target = tmp_path / "owned-ancestor"
    target.mkdir()
    target.chmod(0o711)
    before = target.stat()

    # Cross a full timestamp second so a real mkdir cannot share the baseline
    # ctime even on a filesystem with coarse timestamp resolution.
    deadline = time.monotonic() + 3.0
    while time.time_ns() <= before.st_ctime_ns + 1_000_000_000:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail("timed out establishing an older directory ctime baseline")
        time.sleep(min(0.01, remaining))

    simple_ancestors(monkeypatch, target, mode=None)
    normalized_fstat = host.os.fstat
    created = target / "concurrent-child"
    mutations = 0

    def callback(*args, **kwargs):
        nonlocal mutations
        info = normalized_fstat(kwargs["pass_fds"][0])
        if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino):
            assert mutations == 0
            created.mkdir()
            mutations += 1
        return SimpleNamespace(returncode=0, stdout=b"user::rwx\ngroup::--x\nother::--x\n")

    monkeypatch.setattr(host.subprocess, "run", callback)
    depth = len(target.parts) - 1
    with pytest.raises(StateError) as raised:
        host.verify_runtime_parent(target)
    assert str(raised.value) == (
        f"OCI runtime ancestor changed during verification: phase=post-acl depth={depth} changed=ctime"
    )

    after = target.stat()
    assert mutations == 1
    assert created.is_dir()
    assert after.st_ctime_ns > before.st_ctime_ns
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
    )


def test_post_acl_comparison_intentionally_excludes_gid(tmp_path, monkeypatch):
    simple_ancestors(monkeypatch, tmp_path)
    verified_fstat = host.os.fstat
    target_inode = tmp_path.stat().st_ino
    calls = 0

    def metadata(fd):
        nonlocal calls
        info = verified_fstat(fd)
        if info.st_ino == target_inode:
            calls += 1
            if calls == 2:
                return changed_stat(info, st_gid=info.st_gid + 1)
        return info

    monkeypatch.setattr(host.os, "fstat", metadata)
    host.verify_runtime_parent(tmp_path)


def test_ancestor_depth_starts_at_zero_for_root(monkeypatch):
    root = Path("/")
    simple_ancestors(monkeypatch, root)
    verified_fstat = host.os.fstat
    calls = 0

    def metadata(fd):
        nonlocal calls
        info = verified_fstat(fd)
        calls += 1
        if calls == 2:
            return changed_stat(info, st_ctime_ns=info.st_ctime_ns + 1)
        return info

    monkeypatch.setattr(host.os, "fstat", metadata)
    with pytest.raises(StateError) as raised:
        host.verify_runtime_parent(root)
    assert str(raised.value) == (
        "OCI runtime ancestor changed during verification: phase=post-acl depth=0 changed=ctime"
    )


def test_final_visible_identity_change_is_distinct_and_path_free(tmp_path, monkeypatch):
    target = tmp_path / "private-runtime-name"
    target.mkdir()
    simple_ancestors(monkeypatch, target)
    original_lstat = Path.lstat
    visible = target.stat()

    def lstat(path):
        info = original_lstat(path)
        if path == target:
            return changed_stat(info, st_dev=visible.st_dev + 11, st_ino=visible.st_ino + 13)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    depth = len(target.parts) - 1
    with pytest.raises(StateError) as raised:
        host.verify_runtime_parent(target)
    assert str(raised.value) == (
        f"OCI runtime ancestor changed during verification: phase=final-identity depth={depth} changed=dev,ino"
    )
    assert str(target) not in str(raised.value)
    assert target.name not in str(raised.value)


def test_final_held_stamp_change_includes_gid_and_orders_fields(tmp_path, monkeypatch):
    simple_ancestors(monkeypatch, tmp_path)
    verified_fstat = host.os.fstat
    target_inode = tmp_path.stat().st_ino
    calls = 0

    def metadata(fd):
        nonlocal calls
        info = verified_fstat(fd)
        if info.st_ino == target_inode:
            calls += 1
            if calls == 3:
                return changed_stat(info, st_mode=info.st_mode + 1, st_gid=info.st_gid + 1)
        return info

    monkeypatch.setattr(host.os, "fstat", metadata)
    depth = len(tmp_path.parts) - 1
    with pytest.raises(StateError) as raised:
        host.verify_runtime_parent(tmp_path)
    assert str(raised.value) == (
        f"OCI runtime ancestor changed during verification: phase=final-stamp depth={depth} changed=mode,gid"
    )


def test_change_diagnostic_is_bounded_and_never_contains_metadata_values_or_unknown_labels(tmp_path, monkeypatch):
    target = tmp_path / "secret-component"
    target.mkdir()
    simple_ancestors(monkeypatch, target)
    verified_fstat = host.os.fstat
    target_inode = target.stat().st_ino
    private_value = 10**200
    calls = 0

    def metadata(fd):
        nonlocal calls
        info = verified_fstat(fd)
        if info.st_ino == target_inode:
            calls += 1
            if calls == 2:
                return changed_stat(info, st_ctime_ns=private_value)
        return info

    monkeypatch.setattr(host.os, "fstat", metadata)
    with pytest.raises(StateError) as raised:
        host.verify_runtime_parent(target)
    message = str(raised.value)
    assert len(message.encode("ascii")) <= host._RUNTIME_ANCESTOR_MAX_DIAGNOSTIC_BYTES
    assert str(target) not in message
    assert target.name not in message
    assert str(private_value) not in message
    assert "changed=ctime" in message

    bounded = str(
        host._runtime_ancestor_changed(
            phase="final-stamp",
            depth=private_value,
            changed=("dev", "ino", "mode", "uid", "gid", "ctime"),
        )
    )
    assert "depth=9999+" in bounded
    assert str(private_value) not in bounded
    assert len(bounded.encode("ascii")) <= host._RUNTIME_ANCESTOR_MAX_DIAGNOSTIC_BYTES

    fallback = str(host._runtime_ancestor_changed(phase="private-phase", depth=private_value, changed=("secret",)))
    assert fallback == "OCI runtime ancestor changed during verification"


def test_verification_failure_closes_every_opened_descriptor(tmp_path, monkeypatch):
    simple_ancestors(monkeypatch, tmp_path)
    verified_fstat = host.os.fstat
    original_open = host.os.open
    original_close = host.os.close
    target_inode = tmp_path.stat().st_ino
    opened = []
    closed = []
    calls = 0

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracked_close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    def metadata(fd):
        nonlocal calls
        info = verified_fstat(fd)
        if info.st_ino == target_inode:
            calls += 1
            if calls == 2:
                return changed_stat(info, st_ctime_ns=info.st_ctime_ns + 1)
        return info

    monkeypatch.setattr(host.os, "open", tracked_open)
    monkeypatch.setattr(host.os, "close", tracked_close)
    monkeypatch.setattr(host.os, "fstat", metadata)
    with pytest.raises(StateError, match="phase=post-acl"):
        host.verify_runtime_parent(tmp_path)
    assert opened
    assert closed == list(reversed(opened))


def test_successful_pin_duplicates_only_final_descriptor_and_keeps_it_open(tmp_path, monkeypatch):
    simple_ancestors(monkeypatch, tmp_path)
    original_dup = host.os.dup
    duplicated = []

    def tracked_dup(descriptor):
        pinned = original_dup(descriptor)
        duplicated.append((descriptor, pinned))
        return pinned

    monkeypatch.setattr(host.os, "dup", tracked_dup)
    pinned = host.verify_runtime_parent(tmp_path, pin=True)
    try:
        assert isinstance(pinned, int)
        assert len(duplicated) == 1
        assert pinned == duplicated[0][1]
        assert host.os.fstat(pinned).st_ino == tmp_path.stat().st_ino
    finally:
        os.close(pinned)


def test_successful_verification_keeps_existing_stat_acl_and_visible_check_sequence(tmp_path, monkeypatch):
    simple_ancestors(monkeypatch, tmp_path)
    verified_fstat = host.os.fstat
    original_open = host.os.open
    original_lstat = Path.lstat
    original_dup = host.os.dup
    opened = []
    fstat_calls = []
    acl_calls = []
    lstat_calls = []
    dup_calls = []

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracked_fstat(descriptor):
        fstat_calls.append(descriptor)
        return verified_fstat(descriptor)

    def tracked_acl(*args, **kwargs):
        acl_calls.append(kwargs["pass_fds"][0])
        return SimpleNamespace(returncode=0, stdout=b"user::rwx\ngroup::--x\nother::--x\n")

    def tracked_lstat(path):
        lstat_calls.append(path)
        return original_lstat(path)

    def tracked_dup(descriptor):
        dup_calls.append(descriptor)
        return original_dup(descriptor)

    monkeypatch.setattr(host.os, "open", tracked_open)
    monkeypatch.setattr(host.os, "fstat", tracked_fstat)
    monkeypatch.setattr(host.subprocess, "run", tracked_acl)
    monkeypatch.setattr(Path, "lstat", tracked_lstat)
    monkeypatch.setattr(host.os, "dup", tracked_dup)
    pinned = host.verify_runtime_parent(tmp_path, pin=True)
    try:
        assert len(opened) == len(tmp_path.parts)
        assert acl_calls == opened
        assert fstat_calls == [descriptor for descriptor in opened for _ in range(2)] + list(reversed(opened))
        expected_visible = []
        visible = tmp_path
        while True:
            expected_visible.append(visible)
            if visible == visible.parent:
                break
            visible = visible.parent
        assert lstat_calls == expected_visible
        assert dup_calls == [opened[-1]]
    finally:
        os.close(pinned)


def test_create_only_new_parent_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        host, "verify_runtime_parent", lambda p, pin=False: os.open(p, os.O_RDONLY | os.O_DIRECTORY) if pin else None
    )
    path = tmp_path / "runtime"
    assert host.create_runtime_parent(path) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o711
    (path / "keep").write_bytes(b"existing")
    before = path.stat()
    with pytest.raises(StateError):
        host.create_runtime_parent(path)
    assert path.stat() == before and (path / "keep").read_bytes() == b"existing"


def test_first_party_boot_pins_kernel_and_cleans_only_scratch(config, tmp_path):
    roots = StatePaths(tmp_path / "c", tmp_path / "s")
    roots.state.mkdir()
    before = config.kernel.stat()
    with host.first_party_boot(config, roots) as verified:
        assert verified.kernel.digest == config.kernel_digest
        path = verified.initramfs.path
        assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o400
    assert not path.exists()
    assert config.kernel.stat() == before


def test_create_refuses_parent_replacement_after_verification(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    old = tmp_path / "old"

    def replace(path, *, pin=False):
        assert pin
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        parent.rename(old)
        parent.mkdir()
        return descriptor

    monkeypatch.setattr(host, "verify_runtime_parent", replace)
    with pytest.raises(StateError, match="changed before creation"):
        host.create_runtime_parent(parent / "runtime")
    assert not (parent / "runtime").exists()
    assert not (old / "runtime").exists()


def test_preflight_is_read_only_and_requires_sanitized_libvirt(config, tmp_path, monkeypatch):
    roots = StatePaths(tmp_path / "c", tmp_path / "s")
    monkeypatch.setattr(host, "verify_kvm_api", lambda: 12)
    monkeypatch.setattr(host, "verify_runtime_parent", lambda p: None)
    monkeypatch.setattr(host.shutil, "which", lambda *a, **k: "/usr/bin/tool")
    calls = []

    def probe(argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(host.subprocess, "run", probe)
    # Short runtime path avoids making pathname admission the tested failure.
    roots = StatePaths(roots.config, Path("/tmp/oci-host-unit/s"))
    with pytest.raises(StateError, match="import libvirt"):
        host.preflight_oci_host(config, roots, "vm")
    assert "PYTHONPATH" not in calls[0]["env"]
    assert not (tmp_path / "s").exists()


def test_config_digest_change_refused(config, monkeypatch):
    monkeypatch.setattr(host, "verify_kvm_api", lambda: 12)
    monkeypatch.setattr(host, "verify_runtime_parent", lambda p: None)
    monkeypatch.setattr(host.shutil, "which", lambda *a, **k: "/usr/bin/tool")
    config.kernel_config.write_text(config.kernel_config.read_text() + "\n# changed")
    with pytest.raises(StateError, match="config digest"):
        host.preflight_oci_host(config, StatePaths(Path("/tmp/c"), Path("/tmp/s")), "vm")


def test_preflight_admits_system_sbin_tools_without_ambient_path(config, monkeypatch):
    monkeypatch.setattr(host, "verify_kvm_api", lambda: 12)
    monkeypatch.setattr(host, "verify_runtime_parent", lambda p: None)
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    seen = []

    def which(executable, *, path):
        assert path == "/usr/sbin:/usr/bin:/sbin:/bin"
        seen.append(executable)
        return "/usr/sbin/" + executable

    monkeypatch.setattr(host.shutil, "which", which)
    host.preflight_oci_host(config, StatePaths(Path("/tmp/c"), Path("/tmp/s")), "vm")
    assert "mkfs.ext4" in seen
