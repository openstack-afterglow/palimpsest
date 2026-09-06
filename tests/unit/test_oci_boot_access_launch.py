"""Private exec binds both sealed exports and their explicit read-only grant."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager

import pytest
import test_oci_boot_access as boot_tests
from test_oci_boot_access import case as case

from palimpsest_local import oci_boot_access as access
from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local import oci_runtime_access as runtime_access
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import readonly_grant_acl


@pytest.fixture
def granted(case):
    case.receipt = boot_tests.grant(case)
    runtime_access.grant_oci_runtime_access(case.roots, case.binding, conn=case.conn)
    (case.run_root / "monitor-private").mkdir(mode=0o700)
    return case


def prepare(case):
    return launch.prepare_monitor_launch_authority(case.roots, case.store, case.boot, case.profile, case.binding)


def test_v9_reconstructs_exact_sealed_pair(granted):
    before = boot_tests.snapshot(granted)
    with prepare(granted) as authority:
        frame = authority.to_dict()
        assert frame["schema"] == "palimpsest.monitor-launch-authority.v9"
        assert frame["boot_exports"] == granted.export_receipt.to_dict()
        assert frame["boot_access"] == granted.receipt.to_dict()
        for role, path in granted.export_paths.items():
            assert frame["entries"][role]["path"] == str(path)
            assert frame["entries"][role]["inode"] == path.stat().st_ino
        for entry in frame["entries"].values():
            entry["fd"] = os.dup(entry["fd"])
        try:
            child = launch.MonitorLaunchAuthority.from_dict(frame)
        except BaseException:
            for entry in frame["entries"].values():
                os.close(entry["fd"])
            raise
        with child:
            child.validate(binding=granted.binding)
            assert child.to_dict() == frame
        authority.validate()
    assert boot_tests.snapshot(granted) == before


def test_launch_selects_exports_even_when_original_source_paths_are_gone(granted):
    for role in granted.export_paths:
        getattr(granted.source_boot, role).path.unlink()
    granted.boot = granted.source_boot
    with prepare(granted) as authority:
        for role, path in granted.export_paths.items():
            assert authority.to_dict()["entries"][role]["path"] == str(path)


def test_pair_launch_validation_does_not_reacquire_run_lock(granted, monkeypatch):
    original = state.locked_existing_run
    depth = 0

    @contextmanager
    def locked(*args, **kwargs):
        nonlocal depth
        assert depth == 0, "boot pair verification reacquired its caller's run lock"
        depth += 1
        try:
            with original(*args, **kwargs) as mutation:
                yield mutation
        finally:
            depth -= 1

    for module in (state, launch, access, runtime_access):
        monkeypatch.setattr(module, "locked_existing_run", locked)
    with prepare(granted) as authority:
        with locked(granted.roots, granted.binding.record.name):
            authority.validate()
            authority.to_dict()


@pytest.mark.parametrize("field", ["boot_exports", "boot_access"])
@pytest.mark.parametrize("damage", ["missing", "null"])
def test_frame_cannot_omit_managed_pair_authority(granted, field, damage):
    with prepare(granted) as authority:
        frame = authority.to_dict()
        if damage == "missing":
            frame.pop(field)
        else:
            frame[field] = None
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


@pytest.mark.parametrize("damage", ["v8", "attempt", "principal", "export-id", "inode"])
def test_frame_cannot_rebind_pair_identity(granted, damage):
    with prepare(granted) as authority:
        frame = authority.to_dict()
        if damage == "v8":
            frame["schema"] = "palimpsest.monitor-launch-authority.v8"
        elif damage == "attempt":
            frame["boot_access"]["binding"]["boot_attempt_id"] = "00000000-0000-4000-8000-000000000004"
        elif damage == "principal":
            frame["boot_exports"]["qemu_uid"] += 1
        elif damage == "export-id":
            frame["boot_exports"]["export_id"] = "00000000-0000-4000-8000-000000000004"
        else:
            frame["boot_exports"]["kernel"]["inode"] += 1
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


@pytest.mark.parametrize("phase", ["intent", "revoking", "revoked"])
def test_only_granted_pair_phase_authorizes_launch(granted, phase):
    with prepare(granted) as authority:
        frame = authority.to_dict()
        frame["boot_access"]["phase"] = phase
        frame["boot_access"]["cleanup_digest"] = None if phase == "intent" else "sha256:" + "f" * 64
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


@pytest.mark.parametrize("damage", ["missing", "null", "both-missing"])
def test_reserved_exports_never_downgrade_to_generic_boot_paths(granted, damage):
    with prepare(granted) as authority:
        data = json.loads(granted.state.read_bytes())
        if damage == "null":
            data["oci_boot_access"] = None
        else:
            data.pop("oci_boot_access")
        if damage == "both-missing":
            data.pop("oci_boot_exports")
        granted.state.write_text(json.dumps(data))
        with pytest.raises(StateError):
            authority.validate()
        with pytest.raises(StateError):
            prepare(granted)


def test_ready_exports_require_explicit_grant_before_launch(case):
    runtime_access.grant_oci_runtime_access(case.roots, case.binding, conn=case.conn)
    (case.run_root / "monitor-private").mkdir(mode=0o700)
    with pytest.raises(StateError):
        prepare(case)


@pytest.mark.parametrize("role", ["kernel", "initramfs"])
@pytest.mark.parametrize("writable", [False, True])
def test_pair_descriptor_identity_and_readonly_rights(granted, tmp_path, role, writable):
    path = granted.export_paths[role] if writable else tmp_path / "identical-copy"
    if writable:
        path.chmod(0o640)
    else:
        path.write_bytes(granted.export_paths[role].read_bytes())
        path.chmod(0o440)
    fd = os.open(path, os.O_RDWR if writable else os.O_RDONLY)
    if writable:
        path.chmod(0o440)
    info = os.fstat(fd)
    granted.backend.acls[info.st_dev, info.st_ino] = readonly_grant_acl(granted.export_receipt.qemu_uid)
    try:
        with prepare(granted) as authority:
            frame = authority.to_dict()
            frame["entries"][role]["fd"] = fd
            with pytest.raises(StateError):
                launch.MonitorLaunchAuthority.from_dict(frame)
            os.fstat(fd)
            authority.validate()
    finally:
        os.close(fd)


@pytest.mark.parametrize("damage", ["bytes", "principal"])
def test_later_runtime_acl_callback_cannot_modify_pair(granted, monkeypatch, damage):
    with prepare(granted) as authority:
        original = granted.backend.read_acl
        console = granted.paths.console_log.stat()
        fired = False

        def read(fd):
            nonlocal fired
            result = original(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (console.st_dev, console.st_ino) and not fired:
                fired = True
                path = granted.export_paths["kernel"]
                before = path.stat()
                if damage == "principal":
                    granted.backend.acls[before.st_dev, before.st_ino] = readonly_grant_acl(
                        granted.export_receipt.qemu_uid + 1
                    )
                else:
                    path.chmod(0o600)
                    with path.open("r+b") as stream:
                        stream.write(b"x")
                path.chmod(0o440)
                assert path.stat().st_ctime_ns != before.st_ctime_ns
            return result

        monkeypatch.setattr(granted.backend, "read_acl", read)
        with pytest.raises(StateError):
            authority.validate()
        assert fired


_FRESH_EXEC = """
import json, os, sys
from palimpsest_local import oci_runtime_access, oci_boot_access
from palimpsest_local.oci_acl import ACLStructure
from palimpsest_local.oci_monitor_launch import MonitorLaunchAuthority
from palimpsest_local.errors import StateError
payload = json.load(sys.stdin)
acls = {(dev,ino): ACLStructure.from_dict(acl) for dev,ino,acl in payload['acls']}
class Backend:
    count = 0
    trigger = None
    def read_acl(self, fd):
        info = os.fstat(fd)
        result = acls[info.st_dev, info.st_ino]
        if (info.st_dev, info.st_ino) == tuple(payload['target']):
            self.count += 1
            if self.trigger == self.count:
                path = payload['frame']['entries']['kernel']['path']
                os.chmod(path, 0o600)
                with open(path, 'r+b') as stream:
                    stream.write(b'x')
                os.chmod(path, 0o440)
        return result
backend = Backend()
oci_runtime_access.LinuxFdACLBackend = lambda: backend
oci_boot_access.LinuxFdACLBackend = lambda: backend
try:
    with MonitorLaunchAuthority.from_dict(payload['frame']) as authority:
        if payload['late']:
            backend.count = 0
            authority.validate()
            assert backend.count > 0
            backend.trigger, backend.count = backend.count, 0
            print('armed')
        authority.validate()
    print('accepted')
except StateError:
    print('refused')
"""


@pytest.mark.parametrize("damage", ["valid", "missing", "null", "revoked", "descriptor", "bytes", "late-callback"])
def test_actual_fresh_exec_pair_authority(granted, monkeypatch, request, tmp_path, damage):
    extra_fd = None
    with prepare(granted) as authority:
        frame = authority.to_dict()
        if damage == "missing":
            frame.pop("boot_access")
        elif damage == "null":
            frame["boot_access"] = None
        elif damage == "revoked":
            boot_tests.runtime_tests._terminal_cleanup(granted, monkeypatch, request)
            boot_tests.revoke(granted)
        elif damage == "descriptor":
            path = tmp_path / "replacement-kernel"
            path.write_bytes(granted.export_paths["kernel"].read_bytes())
            path.chmod(0o440)
            extra_fd = os.open(path, os.O_RDONLY)
            info = os.fstat(extra_fd)
            granted.backend.acls[info.st_dev, info.st_ino] = readonly_grant_acl(granted.export_receipt.qemu_uid)
            frame["entries"]["kernel"]["fd"] = extra_fd
        elif damage == "bytes":
            path = granted.export_paths["kernel"]
            path.chmod(0o600)
            with path.open("r+b") as stream:
                stream.write(b"x")
            path.chmod(0o440)
        payload = {
            "frame": frame,
            "acls": [[dev, inode, acl.to_dict()] for (dev, inode), acl in granted.backend.acls.items()],
            "target": [granted.paths.console_log.stat().st_dev, granted.paths.console_log.stat().st_ino],
            "late": damage == "late-callback",
        }
        try:
            result = subprocess.run(
                [sys.executable, "-c", _FRESH_EXEC],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=15,
                check=True,
                pass_fds=(*authority.pass_fds, *((extra_fd,) if extra_fd is not None else ())),
            )
        finally:
            if extra_fd is not None:
                os.close(extra_fd)
        assert not result.stderr
        assert result.stdout.strip() == (
            "accepted" if damage == "valid" else "armed\nrefused" if damage == "late-callback" else "refused"
        )
