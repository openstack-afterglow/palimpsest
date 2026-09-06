"""The monitor may execute only against explicitly inherited launch authority."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local.errors import StateError
from palimpsest_local.oci_monitor_ipc import MonitorPreActivationBinding
from palimpsest_local.oci_root_kvm import verify_host_boot_artifacts
from palimpsest_local.oci_runtime_io import runtime_io_guard
from palimpsest_local.oci_store import OCIStore
from palimpsest_local.platforms import DomainProfile
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind
from palimpsest_local.state import StatePaths, locked_existing_run, read_run_ledger_snapshot, reserve_new_run


@pytest.fixture
def inputs(tmp_path: Path):
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    for path in (roots.config, roots.state, roots.runs, roots.locks):
        path.mkdir(mode=0o700)
    with reserve_new_run(roots, "demo", DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)) as reservation:
        reservation.write_state("defined", {})
    (roots.runs / "demo" / "monitor-private").mkdir(mode=0o700)
    roots.runtime_packs.mkdir(mode=0o700)
    store = OCIStore(roots)
    kernel = tmp_path / "kernel"
    kernel.write_bytes(b"\0" * 0x202 + b"HdrS")
    initramfs = tmp_path / "initramfs"
    initramfs.write_bytes(b"070701" + b"\0" * 20)
    kernel.chmod(0o400)
    initramfs.chmod(0o400)
    boot = verify_host_boot_artifacts(kernel, initramfs)
    profile = DomainProfile(
        "kvm",
        "kvm",
        "x86_64",
        "q35",
        Path("/usr/bin/qemu-system-x86_64"),
        "qemu:///system",
        None,
        False,
        "libvirt-network",
        "cloud-localds",
        "sata",
    )
    binding = MonitorPreActivationBinding(
        read_run_ledger_snapshot(roots, "demo").record,
        os.geteuid(),
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
        "sha256:" + "c" * 64,
        "21849d77-fdd8-4f65-92a0-bbc75ea80767",
        "31849d77-fdd8-4f65-92a0-bbc75ea80767",
        "qemu:///system",
    )
    with locked_existing_run(roots, "demo") as mutation:
        with runtime_io_guard(mutation, plan_digest=binding.plan_digest, create=True) as runtime_io:
            data = mutation.mutable_state()
            data["oci_runtime_io"] = runtime_io.receipt.to_dict()
            mutation.write_state("defined", data)
    return roots, store, boot, profile, binding


def test_authority_pins_complete_explicit_roots_and_readonly_boot(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        frame = authority.to_dict()
        assert len(authority.pass_fds) == len(set(authority.pass_fds)) == 21
        assert frame["binding"] == inputs[-1].to_dict()
        assert frame["store_identity"] == inputs[1].identity
        assert frame["boot"] == inputs[2].to_dict()
        assert all(not os.get_inheritable(fd) for fd in authority.pass_fds)
        frame["schema"] = "changed"
        assert authority.to_dict()["schema"] != "changed"
        descriptors = authority.pass_fds
    authority.close()
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_child_reconstructs_only_explicit_duplicated_descriptors(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        frame = authority.to_dict()
        for entry in frame["entries"].values():
            entry["fd"] = os.dup(entry["fd"])
        with launch.MonitorLaunchAuthority.from_dict(frame) as child:
            assert child.to_dict() == frame
            assert set(authority.pass_fds).isdisjoint(child.pass_fds)
        authority.validate()


@pytest.mark.parametrize("terminal_timeout", [None, 0.1, 3600])
def test_explicit_service_lifetime_roundtrips_without_relaxing_boot_deadline(inputs, terminal_timeout):
    with launch.prepare_monitor_launch_authority(*inputs, terminal_timeout_seconds=terminal_timeout) as authority:
        frame = authority.to_dict()
        assert frame["terminal_timeout_seconds"] == terminal_timeout
        assert frame["timeout_seconds"] == 45.0
        for entry in frame["entries"].values():
            entry["fd"] = os.dup(entry["fd"])
        with launch.MonitorLaunchAuthority.from_dict(frame) as child:
            assert child.to_dict()["terminal_timeout_seconds"] == terminal_timeout
            assert child.to_dict()["timeout_seconds"] == 45.0


@pytest.mark.parametrize("value", [True, False, 0, -1, 3601, "none", float("nan"), float("inf")])
def test_service_lifetime_rejects_invalid_non_null_values(inputs, value):
    with pytest.raises(StateError):
        launch.prepare_monitor_launch_authority(*inputs, terminal_timeout_seconds=value)
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        frame = authority.to_dict()
        frame["terminal_timeout_seconds"] = value
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


def test_unbounded_boot_is_still_rejected_for_services(inputs):
    with pytest.raises(StateError):
        launch.prepare_monitor_launch_authority(*inputs, timeout_seconds=None, terminal_timeout_seconds=None)


@pytest.mark.parametrize(
    "change",
    [
        lambda f: f.update(extra="callback"),
        lambda f: f.update(schema="v2"),
        lambda f: f.update(timeout_seconds=True),
        lambda f: f.update(terminal_timeout_seconds=float("inf")),
        lambda f: f["profile"].update(firmware="arbitrary"),
        lambda f: f["profile"].update(autoselect_firmware=0),
        lambda f: f["profile"].update(emulator="relative/qemu"),
        lambda f: f["entries"]["state"].update(fd=2),
        lambda f: f["entries"]["state"].update(fd=-1),
        lambda f: f["entries"]["state"].update(fd=True),
        lambda f: f["entries"]["state"].update(fd=f["entries"]["config"]["fd"]),
        lambda f: f["entries"]["state"].update(inode=True),
        lambda f: f["entries"]["run"].update(path=f["entries"]["state"]["path"]),
        lambda f: f["entries"]["kernel"].update(path="/tmp/../kernel"),
        lambda f: f["boot"]["kernel"].update(size_bytes=True),
        lambda f: f["boot"].update(architecture="aarch64"),
        lambda f: f.update(store_identity="oci-store-v1:" + "0" * 64),
        lambda f: f["boot"]["initramfs"].update(digest="sha256:" + "0" * 64),
    ],
)
def test_bootstrap_rejects_mutation_without_closing_callers_descriptors(inputs, change):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        frame = authority.to_dict()
        change(frame)
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


def test_bootstrap_rejects_collision_with_control_descriptors(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(authority.to_dict(), excluded_fds=authority.pass_fds[:1])
        authority.validate()


@pytest.mark.parametrize(
    "name",
    [
        "config",
        "state",
        "runs",
        "run",
        "monitor",
        "runtime_io",
        "runtime_console",
        "store",
        "derived",
        "derived_records",
        "blobs",
        "kernel",
        "initramfs",
    ],
)
def test_visible_path_replacement_invalidates_held_authority(inputs, name):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        path = Path(authority.to_dict()["entries"][name]["path"])
        moved = path.with_name(path.name + "-moved")
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=moved.is_dir())
        with pytest.raises(StateError):
            authority.validate()


@pytest.mark.parametrize("name", ["config", "state", "runs", "run", "monitor", "store", "kernel", "initramfs"])
def test_mode_change_invalidates_authority(inputs, name):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        path = Path(authority.to_dict()["entries"][name]["path"])
        path.chmod(0o777)
        with pytest.raises(StateError):
            authority.validate()


def test_boot_bytes_change_invalidates_authority(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        inputs[2].kernel.path.chmod(0o600)
        inputs[2].kernel.path.write_bytes(b"\0" * 0x202 + b"HdrS" + b"changed")
        inputs[2].kernel.path.chmod(0o400)
        with pytest.raises(StateError):
            authority.validate()


def test_console_output_is_mutable_but_its_inode_remains_pinned(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        console = Path(authority.to_dict()["entries"]["runtime_console"]["path"])
        console.write_bytes(b"untrusted guest console output\n")
        authority.validate()
        replacement = console.with_name("replacement")
        replacement.write_bytes(console.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(console)
        with pytest.raises(StateError):
            authority.validate()


def test_bootstrap_v1_is_not_reinterpreted_as_runtime_io_authority(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        frame = authority.to_dict()
        frame["schema"] = "palimpsest.monitor-launch-authority.v1"
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)


@pytest.mark.parametrize("key", ["runtime_io", "runtime_console"])
def test_bootstrap_never_accepts_generic_qemu_group_write_modes(inputs, key):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        path = Path(authority.to_dict()["entries"][key]["path"])
        path.chmod(0o730 if key == "runtime_io" else 0o660)
        with pytest.raises(StateError):
            authority.validate()


def test_factory_rejects_symlinked_ancestor(inputs, tmp_path):
    roots, store, boot, profile, binding = inputs
    alias = tmp_path / "alias"
    alias.symlink_to(roots.state, target_is_directory=True)
    with pytest.raises(StateError):
        launch.prepare_monitor_launch_authority(StatePaths(roots.config, alias), store, boot, profile, binding)


def test_directory_and_binding_must_match_selected_authority(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        with pytest.raises(StateError):
            authority.validate(authority.to_dict()["entries"]["state"]["fd"], inputs[-1])
        with pytest.raises(StateError):
            authority.validate(binding=replace(inputs[-1], plan_digest="sha256:" + "d" * 64))


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("terminal_timeout", [45, None])
def test_worker_connects_only_after_validation_and_closes_connection_and_fds(
    inputs, monkeypatch, fail, controlled, terminal_timeout
):
    from palimpsest_local import oci_root_runtime as runtime

    events = []
    stop_control = launch.MonitorStopControl() if controlled else None

    class Connection:
        def close(self):
            events.append("closed")

    connection = Connection()

    def connect(uri):
        assert uri == inputs[-1].libvirt_uri
        events.append("connect")
        return connection

    def run(roots, name, store, boot, profile, **kwargs):
        events.append("launch")
        assert roots == inputs[0] and name == "demo" and store.identity == inputs[1].identity
        assert boot == inputs[2] and profile == inputs[3]
        assert kwargs["monitor_binding"] == inputs[-1]
        assert kwargs["monitor_lease"] == "lease"
        assert kwargs["conn"] is connection
        assert kwargs["terminal_timeout_seconds"] == terminal_timeout
        assert kwargs["timeout_seconds"] == 45.0
        if controlled:
            assert kwargs["stop_control"] is stop_control
        else:
            assert "stop_control" not in kwargs
        kwargs["authority_guard"]()
        if fail:
            raise StateError("failure")
        return "terminal"

    monkeypatch.setattr(runtime, "connect_oci_root_libvirt", connect)
    monkeypatch.setattr(runtime, "launch_defined_oci_root_domain", run)
    authority = launch.prepare_monitor_launch_authority(*inputs, terminal_timeout_seconds=terminal_timeout)
    frame = authority.to_dict()
    descriptors = authority.pass_fds
    if fail:
        with pytest.raises(StateError, match="failure"):
            authority.run(frame["entries"]["monitor"]["fd"], inputs[-1], "lease", stop_control=stop_control)
    else:
        assert (
            authority.run(frame["entries"]["monitor"]["fd"], inputs[-1], "lease", stop_control=stop_control)
            == "terminal"
        )
    assert events == ["connect", "launch", "closed"]
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("stop_control", [object(), {}, True])
def test_untyped_stop_control_is_rejected_before_worker_connect(inputs, monkeypatch, stop_control):
    from palimpsest_local import oci_root_runtime as runtime

    authority = launch.prepare_monitor_launch_authority(*inputs)
    frame = authority.to_dict()
    descriptors = authority.pass_fds
    monkeypatch.setattr(runtime, "connect_oci_root_libvirt", lambda _: pytest.fail("must not connect"))
    with pytest.raises(StateError):
        authority.run(frame["entries"]["monitor"]["fd"], inputs[-1], "lease", stop_control=stop_control)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_invalid_authority_never_connects(inputs, monkeypatch):
    from palimpsest_local import oci_root_runtime as runtime

    authority = launch.prepare_monitor_launch_authority(*inputs)
    frame = authority.to_dict()
    monkeypatch.setattr(runtime, "connect_oci_root_libvirt", lambda _: pytest.fail("connected"))
    inputs[0].config.chmod(0o777)
    with pytest.raises(StateError):
        authority.run(frame["entries"]["monitor"]["fd"], inputs[-1], "lease")
    with pytest.raises(StateError):
        authority.to_dict()


def test_same_content_boot_replacement_is_not_the_selected_inode(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        kernel = inputs[2].kernel.path
        replacement = kernel.with_name("replacement")
        replacement.write_bytes(kernel.read_bytes())
        replacement.chmod(0o400)
        replacement.replace(kernel)
        with pytest.raises(StateError):
            authority.validate()


def test_factory_rejects_forged_supplied_boot_inode(inputs):
    roots, store, boot, profile, binding = inputs
    forged = replace(boot, kernel=replace(boot.kernel, inode=boot.kernel.inode + 1_000_000))
    with pytest.raises(StateError):
        launch.prepare_monitor_launch_authority(roots, store, forged, profile, binding)


def test_frame_rejects_writable_boot_descriptor(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        inputs[2].kernel.path.chmod(0o600)
        fd = os.open(inputs[2].kernel.path, os.O_RDWR)
        try:
            frame = authority._frame.copy()
            frame["entries"] = dict(frame["entries"])
            frame["entries"]["kernel"] = launch._entry(inputs[2].kernel.path, fd)
            with pytest.raises(StateError):
                launch.MonitorLaunchAuthority.from_dict(frame)
            os.fstat(fd)
        finally:
            os.close(fd)


def test_closed_boot_descriptor_is_rejected(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        frame = authority.to_dict()
        # The temporary duplicate is explicitly ours, not one the authority owns.
        fd = os.dup(frame["entries"]["kernel"]["fd"])
        os.close(fd)
        frame["entries"]["kernel"]["fd"] = fd
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()


def test_private_store_mode_rejected_before_rebuild_can_repair_it(inputs):
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        frame = authority.to_dict()
        path = inputs[0].store
        path.chmod(0o755)
        entry = frame["entries"]["store"]
        entry.update(launch._entry(path, entry["fd"]))
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        assert path.stat().st_mode & 0o777 == 0o755


def test_forged_equal_value_binding_rejected_before_connection(inputs, monkeypatch):
    from palimpsest_local import oci_root_runtime as runtime

    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        frame = authority.to_dict()
        forged = replace(inputs[-1])
        object.__setattr__(forged, "owner_uid", float(os.geteuid()))
        monkeypatch.setattr(runtime, "connect_oci_root_libvirt", lambda _: pytest.fail("connected"))
        with pytest.raises(StateError):
            authority.run(frame["entries"]["monitor"]["fd"], forged, "lease")
