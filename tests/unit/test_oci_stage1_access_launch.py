"""Private exec frames bind the sealed stage-1 grant, never generic read access."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager

import pytest
import test_oci_stage1_access as stage_tests
from test_oci_stage1_access import case as case

from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local import oci_runtime_access as runtime_access
from palimpsest_local import oci_stage1_access as access
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import readonly_grant_acl


@pytest.fixture
def granted(case):
    case.receipt = stage_tests.grant(case)
    runtime_access.grant_oci_runtime_access(case.roots, case.binding, conn=case.conn)
    (case.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    return case


def _prepare(case):
    return launch.prepare_monitor_launch_authority(case.roots, case.store, case.boot, case.profile, case.binding)


def test_v8_frame_reconstructs_exact_readonly_stage1_descriptors(granted):
    before = stage_tests._snapshot(granted)
    with _prepare(granted) as authority:
        frame = authority.to_dict()
        assert frame["schema"] == "palimpsest.monitor-launch-authority.v10"
        assert frame["stage1_access"] == granted.receipt.to_dict()
        assert frame["entries"]["stage1_transport"]["inode"] == granted.receipt.target.inode
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
    assert stage_tests._snapshot(granted) == before


@pytest.mark.parametrize("damage", ["missing", "null", "v7", "attempt", "digest", "inode"])
def test_managed_frame_cannot_drop_or_rebind_stage1_authority(granted, damage):
    with _prepare(granted) as authority:
        frame = authority.to_dict()
        if damage == "missing":
            frame.pop("stage1_access")
        elif damage == "null":
            frame["stage1_access"] = None
            frame["entries"].pop("stage1_transport")
        elif damage == "v7":
            frame["schema"] = "palimpsest.monitor-launch-authority.v7"
        elif damage == "attempt":
            frame["stage1_access"]["binding"]["boot_attempt_id"] = "00000000-0000-4000-8000-000000000004"
        elif damage == "digest":
            frame["stage1_access"]["transport"]["artifact_digest"] = "sha256:" + "f" * 64
        else:
            frame["stage1_access"]["target"]["inode"] += 1
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


@pytest.mark.parametrize("phase", ["intent", "revoking", "revoked"])
def test_only_granted_stage1_phase_authorizes_launch(granted, phase):
    with _prepare(granted) as authority:
        frame = authority.to_dict()
        frame["stage1_access"]["phase"] = phase
        frame["stage1_access"]["cleanup_digest"] = None if phase == "intent" else "sha256:" + "f" * 64
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


@pytest.mark.parametrize("damage", ["missing", "null", "attempt"])
def test_actual_current_member_is_rechecked_by_existing_frame(granted, damage):
    with _prepare(granted) as authority:
        data = json.loads(granted.state.read_bytes())
        if damage == "missing":
            data.pop("oci_stage1_access")
        elif damage == "null":
            data["oci_stage1_access"] = None
        else:
            data["oci_stage1_access"]["binding"]["boot_attempt_id"] = "00000000-0000-4000-8000-000000000004"
        granted.state.write_text(json.dumps(data))
        with pytest.raises(StateError):
            authority.validate()
        with pytest.raises(StateError):
            _prepare(granted)


@pytest.mark.parametrize("writable", [False, True])
def test_stage1_descriptor_replacement_or_write_rights_are_rejected(granted, tmp_path, writable):
    path = granted.transport_path if writable else tmp_path / "same-bytes.raw"
    if not writable:
        path.write_bytes(granted.transport_path.read_bytes())
        path.chmod(0o440)
    else:
        path.chmod(0o640)
    fd = os.open(path, os.O_RDWR if writable else os.O_RDONLY)
    if writable:
        path.chmod(0o440)
    info = os.fstat(fd)
    granted.backend.acls[info.st_dev, info.st_ino] = granted.receipt.target.granted
    try:
        with _prepare(granted) as authority:
            frame = authority.to_dict()
            frame["entries"]["stage1_transport"]["fd"] = fd
            with pytest.raises(StateError):
                launch.MonitorLaunchAuthority.from_dict(frame)
            os.fstat(fd)
            authority.validate()
    finally:
        os.close(fd)


def test_unchanged_readonly_mode_does_not_hide_acl_principal_drift(granted):
    with _prepare(granted) as authority:
        target = granted.receipt.target
        granted.backend.acls[target.device, target.inode] = readonly_grant_acl(granted.receipt.qemu_uid + 1)
        with pytest.raises(StateError):
            authority.validate()


def test_later_runtime_acl_callback_cannot_change_same_mode_stage1_principal(granted, monkeypatch):
    with _prepare(granted) as authority:
        original = granted.backend.read_acl
        console = granted.paths.console_log.stat()
        fired = False

        def read(fd):
            nonlocal fired
            result = original(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (console.st_dev, console.st_ino) and not fired:
                fired = True
                target = granted.receipt.target
                before = granted.transport_path.stat()
                granted.backend.acls[target.device, target.inode] = readonly_grant_acl(granted.receipt.qemu_uid + 1)
                os.chmod(granted.transport_path, 0o440)
                assert granted.transport_path.stat().st_ctime_ns != before.st_ctime_ns
            return result

        monkeypatch.setattr(granted.backend, "read_acl", read)
        with pytest.raises(StateError):
            authority.validate()
        assert fired


def test_stage1_launch_validation_does_not_reacquire_held_run_lock(granted, monkeypatch):
    original = state.locked_existing_run
    depth = 0

    @contextmanager
    def locked(*args, **kwargs):
        nonlocal depth
        assert depth == 0, "stage1 launch validation reacquired its caller's run lock"
        depth += 1
        try:
            with original(*args, **kwargs) as mutation:
                yield mutation
        finally:
            depth -= 1

    for module in (state, launch, access, runtime_access):
        monkeypatch.setattr(module, "locked_existing_run", locked)
    with _prepare(granted) as authority:
        with locked(granted.roots, granted.binding.record.name):
            authority.validate()
            authority.to_dict()


@pytest.mark.parametrize("damage", ["member", "payload"])
def test_launch_last_acl_callback_cannot_change_stage1_authority(granted, monkeypatch, damage):
    with _prepare(granted) as authority:
        original = granted.backend.read_acl
        count = 0

        def read(fd):
            nonlocal count
            result = original(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (granted.receipt.target.device, granted.receipt.target.inode):
                count += 1
            return result

        monkeypatch.setattr(granted.backend, "read_acl", read)
        authority.validate()
        last = count
        assert last > 0
        count = 0
        fired = False

        def mutate(fd):
            nonlocal fired
            result = read(fd)
            if count == last and not fired:
                fired = True
                if damage == "member":
                    data = json.loads(granted.state.read_bytes())
                    data.pop("oci_stage1_access")
                    granted.state.write_text(json.dumps(data))
                else:
                    granted.transport_path.chmod(0o600)
                    with granted.transport_path.open("r+b") as stream:
                        stream.seek(-1, os.SEEK_END)
                        stream.write(b"x")
                    granted.transport_path.chmod(0o440)
            return result

        monkeypatch.setattr(granted.backend, "read_acl", mutate)
        with pytest.raises(StateError):
            authority.validate()
        assert fired


_FRESH_EXEC = """
import json, os, stat, sys
from palimpsest_local import oci_runtime_access, oci_stage1_access
from palimpsest_local.oci_acl import ACLStructure
from palimpsest_local.oci_monitor_launch import MonitorLaunchAuthority
from palimpsest_local.errors import StateError
payload = json.load(sys.stdin)
frame = payload['frame']
acls = {(dev,ino): ACLStructure.from_dict(acl) for dev,ino,acl in payload['acls']}
target = payload['target']
class Backend:
    count = 0
    trigger = None
    def read_acl(self, fd):
        info = os.fstat(fd)
        result = acls[info.st_dev, info.st_ino]
        if (info.st_dev, info.st_ino) == tuple(target):
            self.count += 1
            if self.trigger == self.count:
                path = frame['entries']['stage1_transport']['path']
                mode = stat.S_IMODE(os.stat(path).st_mode)
                os.chmod(path, 0o600)
                with open(path, 'r+b') as stream:
                    stream.seek(-1, os.SEEK_END)
                    stream.write(b'x')
                os.chmod(path, mode)
        return result
backend = Backend()
oci_runtime_access.LinuxFdACLBackend = lambda: backend
oci_stage1_access.LinuxFdACLBackend = lambda: backend
try:
    with MonitorLaunchAuthority.from_dict(frame) as authority:
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
def test_fresh_exec_stage1_authority(granted, monkeypatch, request, tmp_path, damage):
    extra_fd = None
    with _prepare(granted) as authority:
        frame = authority.to_dict()
        if damage == "missing":
            frame.pop("stage1_access")
        elif damage == "null":
            frame["stage1_access"] = None
            frame["entries"].pop("stage1_transport")
        elif damage == "revoked":
            stage_tests.runtime_tests._terminal_cleanup(granted, monkeypatch, request)
            stage_tests.revoke(granted)
        elif damage == "descriptor":
            replacement = tmp_path / "other-stage1.raw"
            replacement.write_bytes(granted.transport_path.read_bytes())
            replacement.chmod(0o440)
            extra_fd = os.open(replacement, os.O_RDONLY)
            info = os.fstat(extra_fd)
            granted.backend.acls[info.st_dev, info.st_ino] = granted.receipt.target.granted
            frame["entries"]["stage1_transport"]["fd"] = extra_fd
        elif damage == "bytes":
            granted.transport_path.chmod(0o600)
            with granted.transport_path.open("r+b") as stream:
                stream.seek(-1, os.SEEK_END)
                stream.write(b"x")
            granted.transport_path.chmod(0o440)
        payload = {
            "frame": frame,
            "acls": [[dev, inode, acl.to_dict()] for (dev, inode), acl in granted.backend.acls.items()],
            "target": [granted.receipt.target.device, granted.receipt.target.inode],
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
