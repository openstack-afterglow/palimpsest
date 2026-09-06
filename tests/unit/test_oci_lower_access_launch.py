"""Fresh exec binds distinct lower export descriptors and their readonly grants."""

import json
import os
import subprocess
import sys

import pytest
import test_oci_boot_access as boot_tests
import test_oci_boot_access_launch as boot_launch
import test_oci_lower_access as lower_tests
from test_oci_lower_access import case as case

from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local import oci_runtime_access as runtime_access
from palimpsest_local.errors import StateError


@pytest.fixture
def granted(case):
    boot_tests.grant(case)
    case.receipt = lower_tests.grant(case)
    runtime_access.grant_oci_runtime_access(case.roots, case.binding, conn=case.conn)
    (case.run_root / "monitor-private").mkdir(mode=0o700)
    return case


def prepare(case):
    return launch.prepare_monitor_launch_authority(case.roots, case.store, case.boot, case.profile, case.binding)


def test_v10_pins_each_distinct_lower_once(granted):
    with prepare(granted) as authority:
        frame = authority.to_dict()
        assert frame["schema"] == "palimpsest.monitor-launch-authority.v10"
        assert frame["lower_exports"] == granted.lower_exports.to_dict()
        assert frame["lower_access"] == granted.receipt.to_dict()
        entries = {key: entry for key, entry in frame["entries"].items() if key.startswith("lower_")}
        assert len(entries) == len(granted.lower_paths)
        assert {entry["path"] for entry in entries.values()} == {str(p) for p in granted.lower_paths.values()}
        for entry in frame["entries"].values():
            entry["fd"] = os.dup(entry["fd"])
        with launch.MonitorLaunchAuthority.from_dict(frame) as child:
            child.validate()
        authority.validate()


@pytest.mark.parametrize("field", ["lower_exports", "lower_access"])
@pytest.mark.parametrize("damage", ["missing", "null"])
def test_missing_managed_frame_authority_is_not_legacy(granted, field, damage):
    with prepare(granted) as authority:
        frame = authority.to_dict()
        if damage == "missing":
            frame.pop(field)
        else:
            frame[field] = None
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


@pytest.mark.parametrize("ledger", [False, True])
def test_complete_lower_frame_omission_cannot_downgrade_to_cas(granted, ledger):
    with prepare(granted) as authority:
        frame = authority.to_dict()
        frame["lower_exports"] = frame["lower_access"] = None
        frame["entries"] = {key: value for key, value in frame["entries"].items() if not key.startswith("lower_")}
        if ledger:
            data = json.loads(granted.state.read_bytes())
            data.pop("oci_lower_exports")
            data.pop("oci_lower_access")
            granted.state.write_text(json.dumps(data))
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)


@pytest.mark.parametrize("damage", ["v9", "inode", "principal", "fd", "revoked"])
def test_changed_frame_or_descriptor_cannot_launch(granted, damage):
    with prepare(granted) as authority:
        frame = authority.to_dict()
        if damage == "v9":
            frame["schema"] = "palimpsest.monitor-launch-authority.v9"
        elif damage == "inode":
            frame["lower_exports"]["targets"][0]["inode"] += 1
        elif damage == "principal":
            frame["lower_exports"]["qemu_uid"] += 1
        elif damage == "fd":
            frame["entries"]["lower_0"]["fd"] = frame["entries"]["kernel"]["fd"]
        else:
            frame["lower_access"]["phase"] = "revoked"
            frame["lower_access"]["cleanup_digest"] = "sha256:" + "f" * 64
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


def test_shared_departure_rejects_still_granted_lowers(granted, monkeypatch, request):
    from palimpsest_local import oci_shared_traversal as shared
    from palimpsest_local.state import locked_existing_run

    lower_tests.runtime_tests._terminal_cleanup(granted, monkeypatch, request)
    boot_tests.revoke(granted)
    with locked_existing_run(granted.roots, granted.name) as mutation:
        with pytest.raises(StateError):
            shared._require_root_released(mutation, granted.binding)
    lower_tests.revoke(granted)
    with locked_existing_run(granted.roots, granted.name) as mutation:
        shared._require_root_released(mutation, granted.binding)


def test_shared_departure_rechecks_lower_after_later_boot_acl_callback(granted, monkeypatch, request):
    from palimpsest_local import oci_shared_traversal as shared
    from palimpsest_local.state import locked_existing_run

    lower_tests.runtime_tests._terminal_cleanup(granted, monkeypatch, request)
    boot_tests.revoke(granted)
    lower_tests.revoke(granted)
    path = next(iter(granted.lower_paths.values()))
    boot_path = granted.export_paths["kernel"]
    boot_identity = boot_path.stat().st_dev, boot_path.stat().st_ino
    original = granted.backend.read_acl

    def mutate(fd):
        result = original(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == boot_identity:
            path.chmod(0o600)
            with path.open("r+b") as stream:
                stream.write(b"x")
            path.chmod(0o400)
        return result

    monkeypatch.setattr(granted.backend, "read_acl", mutate)
    with locked_existing_run(granted.roots, granted.name) as mutation:
        with pytest.raises(StateError):
            shared._require_root_released(mutation, granted.binding)


def test_v10_config_retains_explicit_encoded_size_cap(granted):
    from test_oci_monitor_ipc import _config_value

    from palimpsest_local import oci_monitor_ipc as ipc

    with prepare(granted) as authority:
        frame = authority.to_dict()
        value = _config_value(granted.binding)
        value["launch_authority"] = frame
        assert ipc._encode_config_frame(value)
        # At most 24 distinct lowers plus 24 fixed descriptors. Very long escaped
        # paths need not fit: the existing CONFIG preflight refuses before spawn.
        entry = frame["entries"]["lower_0"]
        frame["entries"] = {str(index): {**entry, "path": "/" + "\x01" * 4095} for index in range(48)}
        with pytest.raises(ipc.MonitorIPCError):
            ipc._encode_config_frame(value)
        assert ipc._MAX_CONFIG_FRAME_BYTES == 1024 * 1024
        assert ipc._MAX_FRAME_BYTES == 16 * 1024


_FRESH_EXEC = (
    boot_launch._FRESH_EXEC.replace(
        "from palimpsest_local import oci_runtime_access, oci_boot_access",
        "from palimpsest_local import oci_runtime_access, oci_boot_access, oci_lower_access",
    )
    .replace(
        "oci_boot_access.LinuxFdACLBackend = lambda: backend",
        "oci_boot_access.LinuxFdACLBackend = lambda: backend\noci_lower_access.LinuxFdACLBackend = lambda: backend",
    )
    .replace("['entries']['kernel']['path']", "['entries']['lower_0']['path']")
)


@pytest.mark.parametrize("damage", ["valid", "null", "bytes", "late-callback"])
def test_actual_fresh_exec_lower_authority(granted, damage):
    with prepare(granted) as authority:
        frame = authority.to_dict()
        if damage == "null":
            frame["lower_access"] = None
        elif damage == "bytes":
            path = next(iter(granted.lower_paths.values()))
            path.chmod(0o600)
            with path.open("r+b") as stream:
                stream.write(b"x")
            path.chmod(0o440)
        info = granted.paths.console_log.stat()
        payload = {
            "frame": frame,
            "acls": [[dev, inode, acl.to_dict()] for (dev, inode), acl in granted.backend.acls.items()],
            "target": [info.st_dev, info.st_ino],
            "late": damage == "late-callback",
        }
        result = subprocess.run(
            [sys.executable, "-c", _FRESH_EXEC],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
            pass_fds=authority.pass_fds,
        )
        assert not result.stderr
        assert result.stdout.strip() == (
            "accepted" if damage == "valid" else "armed\nrefused" if damage == "late-callback" else "refused"
        )


def test_later_runtime_callback_cannot_change_lower_acl_same_mode(granted, monkeypatch):
    from palimpsest_local.oci_acl import readonly_grant_acl

    path = next(iter(granted.lower_paths.values()))
    identity = path.stat().st_dev, path.stat().st_ino
    console_identity = granted.paths.console_log.stat().st_dev, granted.paths.console_log.stat().st_ino
    original = granted.backend.read_acl
    with prepare(granted) as authority:

        def mutate(fd):
            info = os.fstat(fd)
            result = original(fd)
            if (info.st_dev, info.st_ino) == console_identity:
                granted.backend.acls[identity] = readonly_grant_acl(12347)
                path.chmod(0o440)
            return result

        monkeypatch.setattr(granted.backend, "read_acl", mutate)
        with pytest.raises(StateError):
            authority.validate()
