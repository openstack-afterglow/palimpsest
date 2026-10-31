"""Behavior at the pinned-input, sandbox, resource and output boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import hub_builder
from palimpsest_local.oci_layout import MEDIA_TYPE_LAYER_SQUASHFS


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _manifest(job: Path, *, layers: int = 0):
    base = job / "base.raw"
    base.write_bytes(b"pinned fixture image bytes")
    digest = _digest(base.read_bytes())
    manifest = {
        "name": "worker-result",
        "recipe": f"FROM {digest}\nRUN echo ready > /opt/ready",
        "base": {"digest": digest, "path": str(base), "arch": platform.machine(), "disk_format": "raw"},
        "layers": [],
        "timeout_seconds": 60,
        "max_output_bytes": 1024,
    }
    previous = None
    lines = [f"FROM {digest}"]
    for index in range(layers):
        path = job / f"parent-{index}.sqsh"
        path.write_bytes(b"hsqs" + bytes([index]))
        layer_digest = _digest(path.read_bytes())
        manifest["layers"].append(
            {
                "digest": layer_digest,
                "path": str(path),
                "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                "parent_digest": previous,
                "base_image_digest": digest,
            }
        )
        lines.append(f"LAYER {layer_digest}")
        previous = layer_digest
    lines.append("RUN echo ready > /opt/ready")
    manifest["recipe"] = "\n".join(lines)
    return manifest


class _FakeExecutor:
    instances = []
    failure = None

    def __init__(self, job_dir, timeout):
        self.job_dir = job_dir
        self.calls = []
        self.closed = False
        self.instances.append(self)

    def command(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "findmnt":
            return b"ext4\n"
        if argv[:2] == ["qemu-img", "info"]:
            return json.dumps({"format": "raw", "virtual-size": 4096}).encode()
        if argv[0] == "bwrap" and self.failure:
            raise self.failure
        if argv[0] == "mksquashfs":
            Path(argv[2]).write_bytes(b"hsqs\x00\x00direct fixture")
        return b""

    def loop(self, path, **kwargs):
        return "/dev/loop7"

    def mount(self, device, target, kind, options):
        target.mkdir()
        self.calls.append((["mount", kind, options, str(target)], {}))
        if kind == "squashfs":
            # 0.3.3-era layers carry a private 0700 root; children must not inherit it.
            target.chmod(0o700)

    def unmount(self, target):
        self.calls.append((["umount", str(target)], {}))

    def close(self):
        self.closed = True


@pytest.fixture
def direct_boundary(tmp_path, monkeypatch):
    _FakeExecutor.instances = []
    _FakeExecutor.failure = None
    monkeypatch.setattr(hub_builder, "_Executor", _FakeExecutor)
    monkeypatch.setattr(hub_builder, "_private_mounts", lambda: None)
    root = tmp_path / "cloud-root"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    monkeypatch.setattr(hub_builder, "_root_partition", lambda executor, device: root)
    if not hasattr(os, "listxattr"):
        monkeypatch.setattr(os, "listxattr", lambda fd: [], raising=False)
    return _FakeExecutor


def test_direct_output_preserves_public_result_and_child_first_lowers(tmp_path, direct_boundary):
    manifest = _manifest(tmp_path, layers=2)
    manifest["layers"][1]["base_image_digest"] = None
    result = hub_builder.run_build(manifest, tmp_path)
    output = (tmp_path / "result.sqsh").read_bytes()
    assert result == {"digest": _digest(output), "size_bytes": len(output)}
    executor = direct_boundary.instances[0]
    assert executor.closed
    mount = next(argv for argv, _ in executor.calls if argv[:2] == ["mount", "overlay"])
    assert f"lowerdir={tmp_path}/layer-1:{tmp_path}/layer-0:{tmp_path}/cloud-root" in mount[2]
    pack, options = next((argv, kwargs) for argv, kwargs in executor.calls if argv[0] == "mksquashfs")
    assert "-all-root" not in pack  # numeric uid/gid are not rewritten
    assert options["file_limit"] == manifest["max_output_bytes"]
    capture = next(argv for argv, _ in executor.calls if argv[:2] == ["mount", "tmpfs"])
    assert f"size={hub_builder.MAX_DISK_BYTES}," in capture[2]
    assert stat.S_IMODE(Path(pack[1]).stat().st_mode) == 0o755
    run_options = next(options for argv, options in executor.calls if argv[0] == "bwrap")
    assert run_options["umask"] == 0o022


@pytest.mark.parametrize("damage", ["bytes", "recipe", "parent", "base", "outside", "symlink"])
def test_invalid_inputs_fail_before_any_mount(tmp_path, monkeypatch, damage):
    job = tmp_path / "job"
    job.mkdir()
    manifest = _manifest(job, layers=1)
    if damage == "bytes":
        Path(manifest["base"]["path"]).write_bytes(b"tampered")
    elif damage == "recipe":
        manifest["recipe"] = "FROM sha256:" + "f" * 64 + "\nRUN true"
    elif damage == "parent":
        manifest["layers"][0]["parent_digest"] = "sha256:" + "a" * 64
    elif damage == "base":
        manifest["layers"][0]["base_image_digest"] = "sha256:" + "a" * 64
    else:
        outside = tmp_path / "outside.raw"
        outside.write_bytes(Path(manifest["base"]["path"]).read_bytes())
        if damage == "outside":
            manifest["base"]["path"] = str(job / ".." / "outside.raw")
        else:
            link = job / "link.raw"
            link.symlink_to(outside)
            manifest["base"]["path"] = str(link)

    def no_mount():
        pytest.fail("input failure reached mount boundary")

    monkeypatch.setattr(hub_builder, "_private_mounts", no_mount)
    with pytest.raises(Exception, match="digest|mismatch|private pinned"):
        hub_builder.run_build(manifest, job)


@pytest.mark.parametrize("failure", [RuntimeError("RUN failed"), TimeoutError("deadline")])
def test_run_failure_and_timeout_always_close_resources(tmp_path, direct_boundary, failure):
    manifest = _manifest(tmp_path)
    direct_boundary.failure = failure
    with pytest.raises(type(failure)):
        hub_builder.run_build(manifest, tmp_path)
    assert direct_boundary.instances[0].closed
    assert not (tmp_path / "result.sqsh").exists()


def test_sandbox_has_no_host_mounts_credentials_or_uid_remapping(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_KEY", "never-in-run")
    instruction = SimpleNamespace(env={"RECIPE_VALUE": "yes"}, workdir="/opt/a b", command="id")
    argv = hub_builder._sandbox_command(tmp_path / "merged", instruction)
    assert "never-in-run" not in argv
    assert "--clearenv" in argv
    assert all(
        flag in argv
        for flag in ("--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts", "--die-with-parent")
    )
    assert "--unshare-user" not in argv
    caps = [argv[index + 1] for index, value in enumerate(argv) if value == "--cap-add"]
    assert set(caps) == {"CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_SETUID", "CAP_SETGID"}
    binds = [argv[index + 1 : index + 3] for index, value in enumerate(argv) if value == "--bind"]
    assert binds == [[str(tmp_path / "merged"), "/"]]
    assert argv[-1] == "umask 022; mkdir -p -- '/opt/a b' && cd -- '/opt/a b' && id"


def test_cleaning_does_not_follow_recipe_symlinks(tmp_path):
    upper = tmp_path / "upper"
    upper.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep"
    protected.write_text("not recipe data")
    (upper / "var").symlink_to(outside, target_is_directory=True)
    (upper / "tmp").mkdir()
    (upper / "tmp" / "escape").symlink_to(outside, target_is_directory=True)
    (upper / "tmp" / ".hidden").write_text("discard")
    (upper / "etc").mkdir()
    (upper / "etc" / "hosts").symlink_to(protected)
    hub_builder._clean_upper(upper)
    assert protected.read_text() == "not recipe data"
    assert list((upper / "tmp").iterdir()) == []
    assert not (upper / "etc" / "hosts").is_symlink()


@pytest.mark.parametrize("readonly", [True, False])
def test_loop_intent_is_durable_before_attachment(tmp_path, monkeypatch, readonly):
    path = tmp_path / "base.raw"
    path.write_bytes(b"base")
    executor = hub_builder._Executor(tmp_path, 60)

    def command(argv, **kwargs):
        if argv == ["losetup", "--find"]:
            return b"/dev/loop7\n"
        record = json.loads((tmp_path / "resources.json").read_text())["loops"][0]
        assert record["path"] == str(path)
        assert record["ino"] == path.stat().st_ino
        assert ("--read-only" in argv) is readonly
        raise RuntimeError("interrupted attach")

    monkeypatch.setattr(executor, "command", command)
    with pytest.raises(RuntimeError, match="interrupted attach"):
        executor.loop(path, readonly=readonly)
    assert (tmp_path / "resources.json").exists()


def test_cleanup_refuses_reassigned_loop_and_retains_journal(tmp_path, monkeypatch):
    path = tmp_path / "base.raw"
    path.write_bytes(b"base")
    record = {"device": "/dev/loop7", "path": str(path), "dev": path.stat().st_dev, "ino": path.stat().st_ino}
    (tmp_path / "resources.json").write_text(json.dumps({"loops": [record]}))
    monkeypatch.setattr(hub_builder, "_loop_identity", lambda device: (record["dev"] + 1, record["ino"]))
    with pytest.raises(RuntimeError, match="refusing detach"):
        hub_builder.cleanup_build(tmp_path)
    assert (tmp_path / "resources.json").exists()


def test_cleanup_refuses_replaced_backing_inode(tmp_path, monkeypatch):
    path = tmp_path / "base.raw"
    path.write_bytes(b"base")
    record = {"device": "/dev/loop7", "path": str(path), "dev": path.stat().st_dev, "ino": path.stat().st_ino}
    path.rename(tmp_path / "original.raw")
    path.write_bytes(b"replacement")
    replacement = path.stat()
    monkeypatch.setattr(hub_builder, "_loop_identity", lambda device: (replacement.st_dev, replacement.st_ino))
    monkeypatch.setattr(hub_builder, "_command", lambda *args, **kwargs: pytest.fail("detached replaced inode"))
    with pytest.raises(RuntimeError, match="refusing detach"):
        hub_builder._detach_owned(record, tmp_path)


def test_restart_detaches_only_exact_owned_loop(tmp_path, monkeypatch):
    path = tmp_path / "base.raw"
    path.write_bytes(b"base")
    record = {"device": "/dev/loop7", "path": str(path), "dev": path.stat().st_dev, "ino": path.stat().st_ino}
    (tmp_path / "resources.json").write_text(json.dumps({"loops": [record]}))
    backing = [(record["dev"], record["ino"])]
    detached = []
    # Identity comes from the kernel, not sysfs path text that changes after namespace death.
    monkeypatch.setattr(hub_builder, "_loop_identity", lambda device: backing[0])

    def command(argv, deadline):
        detached.append(argv)
        backing[0] = None

    monkeypatch.setattr(hub_builder, "_command", command)
    hub_builder.cleanup_build(tmp_path)
    assert detached == [["losetup", "--detach", "/dev/loop7"]]
    assert not (tmp_path / "resources.json").exists()


def test_restart_does_not_signal_reused_pid(tmp_path, monkeypatch):
    identity = {"pid": 34567, "start_ticks": "old", "boot_id": "boot"}
    (tmp_path / "builder.pid").write_text(json.dumps(identity))
    monkeypatch.setattr(hub_builder, "_identity", lambda pid: {**identity, "start_ticks": "new"})
    monkeypatch.setattr(hub_builder, "_group_live", lambda pid: False)
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("signalled reused PID"))
    hub_builder.cleanup_build(tmp_path)
    assert not (tmp_path / "builder.pid").exists()


def test_loop_identity_reads_kernel_backing_device_and_inode(monkeypatch):
    calls = []
    monkeypatch.setattr(hub_builder.os, "open", lambda path, flags: calls.append(path) or 99)
    monkeypatch.setattr(hub_builder.os, "close", lambda fd: calls.append(("close", fd)))

    def ioctl(fd, request, status):
        assert (fd, request, len(status)) == (99, 0x4C05, 232)
        hub_builder.struct.pack_into("=QQ", status, 0, 65025, 809)

    monkeypatch.setattr(hub_builder.fcntl, "ioctl", ioctl)
    assert hub_builder._loop_identity("/dev/loop3") == (65025, 809)

    def detached(fd, request, status):
        raise OSError(hub_builder.errno.ENXIO, "No such device or address")

    monkeypatch.setattr(hub_builder.fcntl, "ioctl", detached)
    assert hub_builder._loop_identity("/dev/loop3") is None
    assert calls == ["/dev/loop3", ("close", 99), "/dev/loop3", ("close", 99)]
    with pytest.raises(RuntimeError, match="invalid owned loop device"):
        hub_builder._loop_identity("/dev/sda")


def test_tool_environment_excludes_parent_credentials(monkeypatch):
    monkeypatch.setenv("WORKER_KEY", "secret")
    monkeypatch.setattr(hub_builder, "_child_limits", lambda *args: None)
    result = hub_builder._command(
        [sys.executable, "-c", "import os;print(os.getenv('WORKER_KEY'))"], time.monotonic() + 5, capture=True
    )
    assert result.strip() == b"None"


def test_tool_metadata_and_runtime_are_bounded(monkeypatch):
    monkeypatch.setattr(hub_builder, "_child_limits", lambda *args: None)
    children = []
    popen = subprocess.Popen

    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="metadata exceeds"):
        hub_builder._command([sys.executable, "-c", "print('a'*100000)"], time.monotonic() + 5, capture=True)
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
        hub_builder._command([sys.executable, "-c", "import time;time.sleep(30)"], time.monotonic() + 0.1)
    assert all(child.poll() is not None for child in children)


def test_preflight_checks_mount_privilege_without_kvm(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(hub_builder.shutil, "which", lambda *args, **kwargs: "/bin/sh")
    monkeypatch.setattr(hub_builder, "_private_mounts", lambda: None)
    calls = []
    monkeypatch.setattr(hub_builder, "_command", lambda argv, deadline: calls.append(argv))
    monkeypatch.setattr(os, "access", lambda *args: pytest.fail("preflight probed a device"))
    hub_builder.preflight_build_host()
    assert calls[0][:3] == ["mount", "-t", "tmpfs"]
    assert calls[1][0] == "umount"
    assert calls[2][0] == "bwrap"
    assert calls[2][-1] == "/usr/bin/true"
    caps = [calls[2][index + 1] for index, item in enumerate(calls[2]) if item == "--cap-add"]
    assert caps == ["CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_SETUID", "CAP_SETGID"]


@pytest.mark.parametrize("root_count", [0, 1, 2])
def test_cloud_root_selection_rejects_ambiguity(tmp_path, monkeypatch, root_count):
    mounted = []
    unmounted = []
    monkeypatch.setattr(
        hub_builder, "_partition_nodes", lambda executor, device: ["/private/loop7p1", "/private/loop7p2"]
    )

    def command(argv, **kwargs):
        return b"ext4\n"

    def mount(device, target, kind, options):
        mounted.append((device, kind, options))
        target.mkdir()
        if len(mounted) <= root_count:
            (target / "etc").mkdir()
            (target / "usr").mkdir()

    executor = SimpleNamespace(job_dir=tmp_path, command=command, mount=mount, unmount=unmounted.append)
    if root_count == 1:
        assert hub_builder._root_partition(executor, "/dev/loop7") == tmp_path / "base-0"
    else:
        with pytest.raises(RuntimeError, match="exactly one"):
            hub_builder._root_partition(executor, "/dev/loop7")
    assert all(kind == "ext4" and options == "ro,noload,nodev,nosuid" for _, kind, options in mounted)


def test_public_cli_keeps_manifest_workerpid_and_small_json(tmp_path, monkeypatch, capsys):
    manifest = _manifest(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    identities = []
    monkeypatch.setattr(sys, "argv", ["hub_builder", str(manifest_path), "12345"])
    monkeypatch.setattr(hub_builder, "_mark_builder", lambda path, pid: identities.append((path, pid)))
    result = {"digest": "sha256:" + "a" * 64, "size_bytes": 42}

    def build(received, path):
        assert received == manifest
        assert path == tmp_path
        return result

    monkeypatch.setattr(hub_builder, "run_build", build)
    hub_builder.main()
    output = capsys.readouterr().out
    assert len(output.encode()) <= 1024
    assert json.loads(output) == result
    assert identities == [(tmp_path, 12345)]


def test_inheritable_secret_descriptor_is_closed_in_child(tmp_path, monkeypatch):
    key = tmp_path / "worker.key"
    key.write_bytes(b"private credential")
    monkeypatch.setattr(hub_builder, "_child_limits", lambda *args: None)
    with key.open("rb") as handle:
        os.set_inheritable(handle.fileno(), True)
        code = f"import os\ntry: os.fstat({handle.fileno()}); print('leaked')\nexcept OSError: print('closed')"
        result = hub_builder._command([sys.executable, "-c", code], time.monotonic() + 5, capture=True)
    assert result.strip() == b"closed"


@pytest.mark.parametrize("result", [0, -1])
def test_builder_seals_same_uid_memory_before_marker(tmp_path, monkeypatch, result):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getsid", lambda pid: os.getpid())
    monkeypatch.setattr(hub_builder, "_fence", lambda pid: None)
    calls = []

    def prctl(*args):
        calls.append(args)
        return result

    monkeypatch.setattr(hub_builder.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(prctl=prctl))
    if result:
        with pytest.raises(OSError, match="seal builder"):
            hub_builder._mark_builder(tmp_path, 12345)
        assert not (tmp_path / "builder.pid").exists()
    else:
        monkeypatch.setattr(hub_builder, "_identity", lambda pid: {"pid": pid})
        hub_builder._mark_builder(tmp_path, 12345)
        assert json.loads((tmp_path / "builder.pid").read_text()) == {"pid": os.getpid()}
    assert calls == [(4, 0, 0, 0, 0)]


def test_root_metadata_keeps_numeric_owner_mode_and_private_outer_tree(tmp_path, monkeypatch):
    source, outer = tmp_path / "root", tmp_path / "job"
    source.mkdir()
    source.chmod(0o755)
    outer.mkdir()
    outer.chmod(0o700)
    upper = outer / "upper"
    upper.mkdir(mode=0o700)
    source_info = source.stat()
    original_fstat = os.fstat

    def image_stat(fd):
        info = original_fstat(fd)
        if info.st_ino == source_info.st_ino:
            return SimpleNamespace(
                st_uid=1000,
                st_gid=1001,
                st_mode=info.st_mode,
                st_atime_ns=info.st_atime_ns,
                st_mtime_ns=info.st_mtime_ns,
            )
        return info

    owners, attrs = [], []
    monkeypatch.setattr(os, "fstat", image_stat)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: owners.append((uid, gid)))
    monkeypatch.setattr(os, "listxattr", lambda fd: ["user.fixture"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda fd, name: b"preserved", raising=False)
    monkeypatch.setattr(os, "setxattr", lambda fd, name, value: attrs.append((name, value)), raising=False)
    hub_builder._copy_root_metadata(source, upper)
    assert owners == [(1000, 1001)]
    assert attrs == [("user.fixture", b"preserved")]
    assert stat.S_IMODE(upper.stat().st_mode) == 0o755
    assert stat.S_IMODE(outer.stat().st_mode) == 0o700


def test_recipe_umask_does_not_relax_staged_files(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_builder, "_child_limits", lambda *args: None)
    for name, options, expected in [("recipe", {"umask": 0o022}, (0o644, 0o755)), ("protocol", {}, (0o600, 0o700))]:
        root = tmp_path / name
        root.mkdir()
        code = f"from pathlib import Path;p=Path({str(root)!r});(p/'file').write_text('x');(p/'dir').mkdir()"
        hub_builder._command([sys.executable, "-c", code], time.monotonic() + 5, **options)
        assert (stat.S_IMODE((root / "file").stat().st_mode), stat.S_IMODE((root / "dir").stat().st_mode)) == expected


def test_cleanup_stops_original_and_recovery_groups_before_detach(tmp_path, monkeypatch):
    original = {"pid": 34567, "start_ticks": "old", "boot_id": "boot"}
    recovery = {"pid": 34568, "start_ticks": "helper", "boot_id": "boot"}
    (tmp_path / "builder.pid").write_text(json.dumps(original))
    (tmp_path / "helper-old.pid").write_text(json.dumps(recovery))
    (tmp_path / "resources.json").write_text(json.dumps({"loops": [{"device": "/dev/loop7"}]}))
    stopped = []
    monkeypatch.setattr(hub_builder, "_stop_recorded", stopped.append)
    monkeypatch.setattr(hub_builder, "_identity", lambda pid: {"pid": pid})

    def detach(record, directory):
        assert stopped == [original, recovery]
        assert record["device"] == "/dev/loop7"

    monkeypatch.setattr(hub_builder, "_detach_owned", detach)
    hub_builder.cleanup_build(tmp_path)
    assert not (tmp_path / "builder.pid").exists()
    assert not (tmp_path / "helper-old.pid").exists()


def test_recovery_group_uncertainty_prevents_loop_reuse(tmp_path, monkeypatch):
    identity = {"pid": 34567, "start_ticks": "old", "boot_id": "boot"}
    (tmp_path / "helper-old.pid").write_text(json.dumps(identity))
    (tmp_path / "resources.json").write_text(json.dumps({"loops": [{"device": "/dev/loop7"}]}))
    monkeypatch.setattr(hub_builder, "_identity", lambda pid: {"pid": pid, "start_ticks": "reused"})
    monkeypatch.setattr(hub_builder, "_group_live", lambda pid: True)
    monkeypatch.setattr(hub_builder, "_detach_owned", lambda *args: pytest.fail("detached with live uncertain helper"))
    with pytest.raises(RuntimeError, match="ownership is uncertain"):
        hub_builder.cleanup_build(tmp_path)
    assert (tmp_path / "resources.json").exists()


@pytest.mark.parametrize("mode", ["cleanup", "preflight"])
def test_helper_cli_records_identity_before_privileged_operations(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(hub_builder, "_fence_helper", lambda pid: None)
    own = {"pid": os.getpid(), "start_ticks": "self", "boot_id": "boot"}
    monkeypatch.setattr(hub_builder, "_identity", lambda pid: own)
    original = b'{"pid":12345}'
    (tmp_path / "builder.pid").write_bytes(original)

    def operations(*args):
        assert (tmp_path / "builder.pid").read_bytes() == original
        helpers = list(tmp_path.glob("helper-*.pid"))
        assert len(helpers) == 1
        assert json.loads(helpers[0].read_text()) == own
        assert stat.S_IMODE(helpers[0].stat().st_mode) == 0o600

    if mode == "cleanup":
        monkeypatch.setattr(hub_builder, "cleanup_build", operations)
        argv = ["hub_builder", mode, str(tmp_path), "12345"]
    else:
        monkeypatch.setattr(hub_builder, "preflight_build_host", operations)
        argv = ["hub_builder", mode, "12345", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)
    hub_builder.main()
    assert (tmp_path / "builder.pid").read_bytes() == original
    assert not list(tmp_path.glob("helper-*.pid"))


def test_restart_reclaims_stale_helper_record_that_reused_current_pid(tmp_path, monkeypatch):
    # After a VM/container restart the new helper can receive the old helper's PID.
    own = {"pid": os.getpid(), "start_ticks": "now", "boot_id": "boot"}
    stale = {"pid": os.getpid(), "start_ticks": "previous-lifetime", "boot_id": "boot"}
    monkeypatch.setattr(hub_builder, "_identity", lambda pid: own)
    monkeypatch.setattr(hub_builder.os, "killpg", lambda *args: pytest.fail("signalled the current helper"))
    (tmp_path / "helper-stale.pid").write_text(json.dumps(stale))
    (tmp_path / "helper-own.pid").write_text(json.dumps(own))
    hub_builder._stop_helpers(tmp_path)
    assert not (tmp_path / "helper-stale.pid").exists()
    assert (tmp_path / "helper-own.pid").exists()
    with pytest.raises(RuntimeError, match="current helper"):
        hub_builder._stop_recorded(own)


def test_worker_pid_one_keeps_parent_death_fence(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getsid", lambda pid: os.getpid())
    monkeypatch.setattr(os, "getppid", lambda: 1)
    calls = []
    monkeypatch.setattr(
        hub_builder.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(prctl=lambda *args: calls.append(args) or 0)
    )
    hub_builder._fence_helper(1)
    assert calls == [(1, signal.SIGKILL, 0, 0, 0)]


@pytest.mark.parametrize("multibyte", [False, True])
def test_manifest_admits_full_utf8_recipe_and_escaped_envelope(tmp_path, multibyte):
    manifest = _manifest(tmp_path)
    prefix = f"FROM {manifest['base']['digest']}\nRUN printf "
    remaining = 1024**2 - len(prefix.encode())
    padding = ("é" * (remaining // 2) + "x" * (remaining % 2)) if multibyte else "\x01" * remaining
    manifest["recipe"] = prefix + padding
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=True))
    loaded = hub_builder._load_manifest(path)
    recipe, _, _ = hub_builder._verify_inputs(loaded, tmp_path)
    assert recipe.runs[0].command.startswith("printf ")
    loaded["recipe"] += "x"
    with pytest.raises(ValueError, match="UTF-8 size"):
        hub_builder._verify_inputs(loaded, tmp_path)


def test_manifest_json_above_envelope_is_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(b" " * (hub_builder.MANIFEST_LIMIT + 1))
    with pytest.raises(ValueError, match="JSON exceeds"):
        hub_builder._load_manifest(path)


def _loop_fixture(tmp_path, monkeypatch, names=("loop7p1",)):
    job = tmp_path / "job"
    job.mkdir()
    backing = job / "base.raw"
    backing.write_bytes(b"raw")
    disk = tmp_path / "sysfs" / "loop7"
    disk.mkdir(parents=True)
    (disk / "dev").write_text("7:7")
    for index, name in enumerate(names, 1):
        entry = disk / name
        entry.mkdir()
        (entry / "partition").write_text(str(index))
        (entry / "dev").write_text(f"259:{index}")
    monkeypatch.setattr(hub_builder, "LOOP_SYSFS", disk.parent)
    monkeypatch.setattr(hub_builder, "_loop_identity", lambda device: (backing.stat().st_dev, backing.stat().st_ino))
    info = backing.stat()
    executor = SimpleNamespace(
        job_dir=job, loops=[{"device": "/dev/loop7", "path": str(backing), "dev": info.st_dev, "ino": info.st_ino}]
    )
    return executor


def test_missing_dev_partition_node_uses_exact_private_sysfs_node(tmp_path, monkeypatch):
    executor = _loop_fixture(tmp_path, monkeypatch)
    created, probed = [], []

    def mknod(path, mode, device):
        created.append((path, mode, device))
        Path(path).write_bytes(b"block-node substitute")

    monkeypatch.setattr(os, "mknod", mknod, raising=False)

    def command(argv, **kwargs):
        assert argv[0] == "blkid"
        probed.append(argv[-1])
        assert Path(argv[-1]).is_relative_to(executor.job_dir / "devices")
        assert Path(argv[-1]).exists()
        return b"ext4\n"

    def mount(device, target, kind, options):
        assert device == probed[-1]
        target.mkdir()
        (target / "etc").mkdir()
        (target / "usr").mkdir()

    executor.command, executor.mount, executor.unmount = command, mount, lambda target: None
    root = hub_builder._root_partition(executor, "/dev/loop7")
    assert root == executor.job_dir / "base-0"
    assert created == [(executor.job_dir / "devices/loop7p1", stat.S_IFBLK | 0o600, os.makedev(259, 1))]


def test_partition_name_ambiguity_fails_before_node_creation(tmp_path, monkeypatch):
    executor = _loop_fixture(tmp_path, monkeypatch, names=("loop8p1",))
    monkeypatch.setattr(os, "mknod", lambda *args: pytest.fail("created unowned node"), raising=False)
    with pytest.raises(RuntimeError, match="partition name"):
        hub_builder._partition_nodes(executor, "/dev/loop7")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel parent-death behavior")
def test_worker_sigkill_during_cleanup_fences_helper_before_restart_detach(tmp_path, monkeypatch):
    (tmp_path / "resources.json").write_text(json.dumps({"loops": [{"device": "/dev/loop7"}]}))
    leaked = tmp_path / "late-detach"
    helper_code = (
        "import os,sys,signal\n"
        f"sys.path.insert(0,{str(Path(hub_builder.__file__).parents[1])!r})\n"
        "from pathlib import Path\nfrom palimpsest_local import hub_builder as b\n"
        "def detach(record,directory):\n"
        " print(os.getpid(),flush=True)\n signal.pause()\n"
        f" Path({str(leaked)!r}).write_text('unsafe old detach')\n"
        "b._detach_owned=detach\n"
        f"sys.argv=['hub_builder','cleanup',{str(tmp_path)!r},str(os.getppid())]\n"
        "b.main()\n"
    )
    worker_code = f"import subprocess,sys;child=subprocess.Popen([sys.executable,'-c',{helper_code!r}],start_new_session=True);child.wait()"
    worker_process = subprocess.Popen(
        [sys.executable, "-c", worker_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    helper_pid = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(worker_process.stdout, selectors.EVENT_READ)
            assert selector.select(5), "cleanup helper did not reach fenced detach boundary"
        helper_pid = int(worker_process.stdout.readline())
        markers = list(tmp_path.glob("helper-*.pid"))
        assert len(markers) == 1
        assert json.loads(markers[0].read_text())["pid"] == helper_pid
        worker_process.kill()
        worker_process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while hub_builder._group_live(helper_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not hub_builder._group_live(helper_pid)
        assert not leaked.exists()

        def restart_detach(record, directory):
            assert not hub_builder._group_live(helper_pid)
            assert not leaked.exists()

        monkeypatch.setattr(hub_builder, "_detach_owned", restart_detach)
        hub_builder.cleanup_build(tmp_path)
        assert not list(tmp_path.glob("helper-*.pid"))
        assert not (tmp_path / "resources.json").exists()
    finally:
        if worker_process.poll() is None:
            worker_process.kill()
            worker_process.wait(timeout=5)
        if helper_pid is not None:
            try:
                os.killpg(helper_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        worker_process.stdout.close()
        worker_process.stderr.close()


def test_child_file_limit_never_raises_inherited_hard_ceiling(monkeypatch):
    calls = []
    monkeypatch.setattr(hub_builder, "_fence", lambda parent: None)
    monkeypatch.setattr(hub_builder.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(prctl=lambda *args: 0))
    monkeypatch.setattr(hub_builder.resource, "getrlimit", lambda kind: (100, 100))
    monkeypatch.setattr(hub_builder.resource, "setrlimit", lambda kind, limits: calls.append((kind, limits)))
    hub_builder._child_limits(1234, 1000)
    assert calls[-1] == (hub_builder.resource.RLIMIT_FSIZE, (100, 100))
