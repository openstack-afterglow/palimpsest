import hashlib
import os
import stat
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
        values["st_mode"] = stat.S_IFDIR | (mode if info.st_ino == inode else 0o755)
        return SimpleNamespace(**values)

    monkeypatch.setattr(host.os, "fstat", metadata)
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=acl))


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
