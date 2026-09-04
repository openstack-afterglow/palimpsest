"""Behavioral contract for the standalone KVM primitives."""

from __future__ import annotations

import inspect
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from palimpsest_local import platforms
from palimpsest_local.errors import StateError
from palimpsest_local.kvm import (
    DOMAIN_MARKER_NAMESPACE,
    DOMAIN_MARKER_VERSION,
    LIBVIRT_UNIX_SOCKET_PATH_MAX_BYTES,
    MAX_LAYER_DISKS,
    DomainSpec,
    KvmError,
    KvmUnavailable,
    LayerDisk,
    OCIRootDomainSpec,
    Stage1TransportDisk,
    VolumeDisk,
    build_domain_xml,
    build_hdiutil_seed_command,
    build_layer_activation_script,
    build_layer_disks,
    build_oci_root_domain_xml,
    build_seed_iso_command,
    connect,
    destroy_and_undefine,
    get_domain_run_id,
    layer_blob_path,
    run_hdiutil_seed_iso,
    run_seed_iso,
    validate_domain_name_available,
    validate_network,
)
from palimpsest_local.oci_control_protocol import OCI_CONTROL_CHANNEL_NAME
from palimpsest_local.oci_control_protocol_v2 import OCI_CONTROL_PROTOCOL_V2
from palimpsest_local.oci_root_kvm import verify_host_boot_artifacts

_ROOT = Path("/var/lib/palimpsest/layers")
_DIGESTS = [f"sha256:{chr(ord('a') + index) * 64}" for index in range(3)]


_X86_PROFILE = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64")
_AARCH64_KVM_PROFILE = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "aarch64")
_HVF_PROFILE = platforms.DomainProfile(
    backend=platforms.BACKEND_HVF,
    domain_type="hvf",
    arch="aarch64",
    machine="virt",
    emulator=Path("/opt/homebrew/bin/qemu-system-aarch64"),
    uri="qemu:///session",
    firmware=platforms.Firmware(
        loader=Path("/opt/homebrew/share/qemu/edk2-aarch64-code.fd"),
        nvram_template=Path("/opt/homebrew/share/qemu/edk2-arm-vars.fd"),
    ),
    autoselect_firmware=False,
    network_mode="user-hostfwd",
    seed_tool="hdiutil",
    seed_bus="scsi",
)


def _spec(layers=None) -> DomainSpec:
    return DomainSpec(
        name="palimpsest-demo",
        memory_mib=4096,
        vcpus=2,
        root_disk=Path("/var/lib/palimpsest/domains/demo.qcow2"),
        seed_iso=Path("/var/lib/palimpsest/domains/demo-seed.iso"),
        layers=layers if layers is not None else build_layer_disks(_ROOT, _DIGESTS),
    )


def _oci_root_spec() -> OCIRootDomainSpec:
    return OCIRootDomainSpec(
        name="oci-demo",
        memory_mib=1024,
        vcpus=1,
        kernel=Path("/var/lib/palimpsest/boot/vmlinuz"),
        initramfs=Path("/var/lib/palimpsest/boot/initramfs"),
        kernel_cmdline=(
            "console=ttyS0,115200n8 panic=1 rdinit=/init "
            "palimpsest.root=virtio-11111111111111111111 "
            "palimpsest.lowers=virtio-22222222222222222222,virtio-33333333333333333333"
        ),
        root_disk=Path("/var/lib/palimpsest/roots/demo.raw"),
        root_serial="11111111111111111111",
        layers=(
            LayerDisk(_DIGESTS[0], Path("/var/lib/palimpsest/store/a"), "vdc", "22222222222222222222"),
            LayerDisk(_DIGESTS[0], Path("/var/lib/palimpsest/store/a"), "vdd", "33333333333333333333"),
        ),
        stage1_transport=Stage1TransportDisk(
            "sha256:" + "5" * 64,
            Path("/var/lib/palimpsest/runs/oci-demo/stage1-plan.raw"),
            "vdb",
            "44444444444444444444",
        ),
        run_id="f6f546e2-e734-4920-9eff-1762b348a249",
        boot_contract_digest="sha256:" + "4" * 64,
        lifecycle_socket=Path("/var/lib/palimpsest/runs/oci-demo/lifecycle.sock"),
    )


def test_oci_root_domain_xml_is_direct_kernel_raw_root_without_cloud_seed():
    xml = ET.fromstring(build_oci_root_domain_xml(_oci_root_spec(), _X86_PROFILE))

    assert xml.findtext("./os/kernel") == "/var/lib/palimpsest/boot/vmlinuz"
    assert xml.findtext("./os/initrd") == "/var/lib/palimpsest/boot/initramfs"
    assert xml.find("./os/boot") is None
    disks = xml.findall("./devices/disk")
    assert len(disks) == 4
    assert all(disk.get("device") == "disk" for disk in disks)
    root = disks[0]
    assert root.find("target").attrib == {"dev": "vda", "bus": "virtio"}
    assert [disk.find("target").get("dev") for disk in disks] == ["vda", "vdb", "vdc", "vdd"]
    assert root.find("driver").get("type") == "raw"
    assert root.find("readonly") is None
    assert [disk.findtext("serial") for disk in disks] == [
        "11111111111111111111",
        "44444444444444444444",
        "22222222222222222222",
        "33333333333333333333",
    ]
    assert all(disk.find("readonly") is not None for disk in disks[1:])
    assert disks[1].find("shareable") is None
    assert all(disk.find("shareable") is not None for disk in disks[2:])
    assert [source.attrib for source in xml.findall("./devices/disk/source")] == [
        {"file": os.fspath(path)}
        for path in (
            _oci_root_spec().root_disk,
            _oci_root_spec().stage1_transport.host_path,
            *[layer.host_path for layer in _oci_root_spec().layers],
        )
    ]
    assert [label.attrib for label in xml.findall("./devices/disk/source/seclabel")] == [
        {"model": "dac", "relabel": "no"}
    ] * len(disks)
    marker = xml.find(f"./metadata/{{{DOMAIN_MARKER_NAMESPACE}}}run")
    assert marker is not None and marker.get("contract") == "sha256:" + "4" * 64
    lifecycle = xml.find(f"./metadata/{{{DOMAIN_MARKER_NAMESPACE}}}lifecycle")
    assert lifecycle is not None
    assert lifecycle.attrib == {"channel": OCI_CONTROL_CHANNEL_NAME, "protocol": OCI_CONTROL_PROTOCOL_V2}
    assert [controller.attrib for controller in xml.findall("./devices/controller")] == [
        {"type": "virtio-serial", "index": "0"}
    ]
    channels = xml.findall("./devices/channel")
    assert len(channels) == 1
    assert channels[0].attrib == {"type": "unix"}
    assert channels[0].find("source").attrib == {
        "mode": "bind",
        "path": "/var/lib/palimpsest/runs/oci-demo/lifecycle.sock",
    }
    assert channels[0].find("target").attrib == {"type": "virtio", "name": OCI_CONTROL_CHANNEL_NAME}


def test_oci_root_domain_xml_rejects_wrong_platform_and_layer_order():
    with pytest.raises(KvmError, match="x86_64 KVM"):
        build_oci_root_domain_xml(_oci_root_spec(), _AARCH64_KVM_PROFILE)
    spec = _oci_root_spec()
    reordered = (spec.layers[1], spec.layers[0])
    with pytest.raises(KvmError, match="order or identity"):
        build_oci_root_domain_xml(OCIRootDomainSpec(**{**spec.__dict__, "layers": reordered}), _X86_PROFILE)
    wrong_transport = Stage1TransportDisk(
        spec.stage1_transport.artifact_digest,
        spec.stage1_transport.host_path,
        "vdc",
        spec.stage1_transport.serial,
    )
    with pytest.raises(KvmError, match="transport identity"):
        build_oci_root_domain_xml(
            OCIRootDomainSpec(**{**spec.__dict__, "stage1_transport": wrong_transport}),
            _X86_PROFILE,
        )
    with pytest.raises(KvmError, match="lifecycle channel contract"):
        build_oci_root_domain_xml(
            OCIRootDomainSpec(**{**spec.__dict__, "lifecycle_channel_name": "user.controlled"}),
            _X86_PROFILE,
        )
    with pytest.raises(KvmError, match="lifecycle socket path"):
        build_oci_root_domain_xml(
            OCIRootDomainSpec(**{**spec.__dict__, "lifecycle_socket": Path("relative/lifecycle.sock")}),
            _X86_PROFILE,
        )


@pytest.mark.parametrize(
    ("size", "accepted"),
    [
        (LIBVIRT_UNIX_SOCKET_PATH_MAX_BYTES, True),
        (LIBVIRT_UNIX_SOCKET_PATH_MAX_BYTES + 1, False),
    ],
)
def test_oci_root_lifecycle_socket_path_enforces_linux_af_unix_byte_boundary(size: int, accepted: bool):
    suffix = "/lifecycle.sock"
    socket_path = Path("/" + "a" * (size - len(os.fsencode(suffix)) - 1) + suffix)
    assert len(os.fsencode(socket_path)) == size
    spec = _oci_root_spec()
    candidate = OCIRootDomainSpec(**{**spec.__dict__, "lifecycle_socket": socket_path})

    if accepted:
        build_oci_root_domain_xml(candidate, _X86_PROFILE)
    else:
        with pytest.raises(KvmError, match="AF_UNIX pathname limit"):
            build_oci_root_domain_xml(candidate, _X86_PROFILE)


def test_oci_root_lifecycle_socket_path_rejects_embedded_nul():
    spec = _oci_root_spec()
    candidate = OCIRootDomainSpec(
        **{**spec.__dict__, "lifecycle_socket": Path("/var/lib/palimpsest/\0/lifecycle.sock")}
    )

    with pytest.raises(KvmError, match="lifecycle socket path"):
        build_oci_root_domain_xml(candidate, _X86_PROFILE)


def test_cloud_domain_xml_does_not_gain_oci_lifecycle_topology():
    xml = ET.fromstring(build_domain_xml(_spec(), _X86_PROFILE))
    names = [target.get("name") for target in xml.findall("./devices/channel/target")]
    assert OCI_CONTROL_CHANNEL_NAME not in names
    assert xml.find("./devices/controller[@type='virtio-serial']") is None
    assert xml.findall("./devices/disk/source/seclabel") == []


def test_host_boot_artifact_policy_hashes_valid_explicit_files(tmp_path: Path):
    kernel = tmp_path / "vmlinuz"
    kernel_bytes = bytearray(0x206)
    kernel_bytes[0x202:0x206] = b"HdrS"
    kernel.write_bytes(kernel_bytes)
    initramfs = tmp_path / "initramfs"
    initramfs.write_bytes(b"\x1f\x8b" + b"payload")

    verified = verify_host_boot_artifacts(kernel.resolve(), initramfs.resolve())

    assert verified.architecture == "x86_64"
    assert verified.kernel.digest.startswith("sha256:")
    assert "path" not in verified.to_dict()["kernel"]
    assert str(tmp_path) not in repr(verified.to_dict())


def test_host_boot_artifact_policy_rejects_symlink_writable_and_digest_rebinding(tmp_path: Path):
    kernel = tmp_path / "vmlinuz"
    kernel_bytes = bytearray(0x206)
    kernel_bytes[0x202:0x206] = b"HdrS"
    kernel.write_bytes(kernel_bytes)
    initramfs = tmp_path / "initramfs"
    initramfs.write_bytes(b"\x1f\x8bpayload")
    link = tmp_path / "kernel-link"
    link.symlink_to(kernel)

    with pytest.raises(StateError, match="securely read"):
        verify_host_boot_artifacts(link.absolute(), initramfs.resolve())
    kernel.chmod(0o666)
    with pytest.raises(StateError, match="metadata is unsafe"):
        verify_host_boot_artifacts(kernel.resolve(), initramfs.resolve())
    kernel.chmod(0o644)
    with pytest.raises(StateError, match="does not match"):
        verify_host_boot_artifacts(
            kernel.resolve(),
            initramfs.resolve(),
            expected_kernel_digest="sha256:" + "0" * 64,
        )


def test_layer_blob_path_matches_oci_image_layout():
    assert layer_blob_path(_ROOT, _DIGESTS[0]) == _ROOT / "blobs" / "sha256" / ("a" * 64)


@pytest.mark.parametrize(
    "bad", ["../../etc/passwd", "sha256:../x", "sha256:zz", "", "sha256:" + "a" * 63, "md5:" + "a" * 32]
)
def test_layer_blob_path_rejects_traversal_and_malformed(bad):
    with pytest.raises(KvmError):
        layer_blob_path(_ROOT, bad)


def test_layer_disks_assign_sequential_virtio_targets():
    disks = build_layer_disks(_ROOT, _DIGESTS)
    assert [disk.target_dev for disk in disks] == ["vdb", "vdc", "vdd"]
    assert all(disk.target_dev != "vda" for disk in disks)


def test_layer_disk_serial_is_truncated_to_qemu_limit():
    for disk in build_layer_disks(_ROOT, _DIGESTS):
        assert len(disk.serial) == 20
        assert disk.blob_digest[7:].startswith(disk.serial)


def test_layer_disks_reject_empty_and_over_limit():
    with pytest.raises(KvmError):
        build_layer_disks(_ROOT, [])
    with pytest.raises(KvmError, match="limit"):
        build_layer_disks(_ROOT, [f"sha256:{index:064x}" for index in range(MAX_LAYER_DISKS + 1)])


def test_domain_xml_marks_every_layer_disk_readonly():
    xml = ET.fromstring(build_domain_xml(_spec(), _X86_PROFILE))
    layers = [
        disk
        for disk in xml.findall("./devices/disk")
        if (disk.find("target") is not None and disk.find("target").get("dev", "").startswith("vd"))
        and disk.find("target").get("dev") != "vda"
    ]
    assert len(layers) == 3
    assert all(disk.find("readonly") is not None and disk.find("driver").get("type") == "raw" for disk in layers)


def test_domain_xml_root_disk_is_writable_qcow2():
    xml = ET.fromstring(build_domain_xml(_spec(), _X86_PROFILE))
    root = next(disk for disk in xml.findall("./devices/disk") if disk.find("target").get("dev") == "vda")
    assert root.find("readonly") is None
    assert root.find("driver").get("type") == "qcow2"


def test_domain_xml_carries_serial_for_stable_guest_lookup():
    disks = build_layer_disks(_ROOT, _DIGESTS)
    xml = ET.fromstring(build_domain_xml(_spec(disks), _X86_PROFILE))
    assert [disk.findtext("serial") for disk in xml.findall("./devices/disk") if disk.findtext("serial")] == [
        disk.serial for disk in disks
    ]


def test_domain_xml_attaches_writable_volume_as_raw_virtio_block():
    volume = VolumeDisk(
        name="data",
        host_path=Path("/var/lib/palimpsest/volumes/demo/data.raw"),
        target_dev="vde",
        serial="0123456789abcdefghij",
        mount_path="/var/lib/data",
    )
    xml = ET.fromstring(build_domain_xml(DomainSpec(**{**_spec().__dict__, "volumes": [volume]}), _X86_PROFILE))
    disk = next(item for item in xml.findall("./devices/disk") if item.findtext("serial") == volume.serial)

    assert disk.find("readonly") is None
    assert disk.find("driver").get("type") == "raw"
    assert disk.find("target").attrib == {"dev": "vde", "bus": "virtio"}


def test_activation_script_mounts_writable_volume_by_stable_id():
    volume = VolumeDisk(
        name="data",
        host_path=Path("/var/lib/palimpsest/volumes/demo/data.raw"),
        target_dev="vde",
        serial="0123456789abcdefghij",
        mount_path="/var/lib/data",
    )
    script = build_layer_activation_script([], volumes=[volume])

    assert "/dev/disk/by-id/virtio-0123456789abcdefghij" in script
    assert "mount -t ext4 -o rw,noatime" in script
    assert "/dev/vde" not in script


def test_domain_xml_attaches_nocloud_seed_as_cdrom():
    xml = ET.fromstring(build_domain_xml(_spec(), _X86_PROFILE))
    cdrom = next(disk for disk in xml.findall("./devices/disk") if disk.get("device") == "cdrom")
    assert cdrom.find("source").get("file").endswith("-seed.iso")
    assert cdrom.find("readonly") is not None


def test_builder_domain_has_only_explicit_serial_channel():
    spec = DomainSpec(
        **{**_spec().__dict__, "guest_agent": False, "control_socket": Path("/run/palimpsest/builder.sock")}
    )
    xml = ET.fromstring(build_domain_xml(spec, _X86_PROFILE))
    names = [channel.find("target").get("name") for channel in xml.findall("./devices/channel")]
    assert names == ["org.afterglow.palimpsest.builder.v1"]
    source = xml.find("./devices/channel/source")
    assert source is not None and source.get("mode") == "bind"


@pytest.mark.parametrize(
    ("field", "value"), [("name", "Bad Name"), ("name", "../evil"), ("memory_mib", 16), ("vcpus", 0), ("vcpus", 9999)]
)
def test_domain_xml_rejects_invalid_spec(field, value):
    spec = _spec()
    with pytest.raises(KvmError):
        build_domain_xml(DomainSpec(**{**spec.__dict__, field: value}), _X86_PROFILE)


def test_seed_iso_command_is_argument_list_not_shell():
    assert build_seed_iso_command(Path("/s/seed.iso"), Path("/s/user-data"), Path("/s/meta-data")) == [
        "cloud-localds",
        "/s/seed.iso",
        "/s/user-data",
        "/s/meta-data",
    ]


def test_seed_iso_command_rejects_empty_paths():
    with pytest.raises(KvmError):
        build_seed_iso_command(Path("/s/seed.iso"), Path(""), Path("/s/meta-data"))


def test_run_seed_iso_reports_missing_tool_actionably():
    with patch("palimpsest_local.kvm.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(KvmError, match="cloud-image-utils"):
            run_seed_iso(Path("/s/seed.iso"), Path("/s/user-data"), Path("/s/meta-data"))


def test_run_seed_iso_passes_no_shell():
    with patch("palimpsest_local.kvm.subprocess.run") as run:
        run_seed_iso(Path("/s/seed.iso"), Path("/s/user-data"), Path("/s/meta-data"))
    args, kwargs = run.call_args
    assert isinstance(args[0], list)
    assert "shell" not in kwargs or kwargs["shell"] is False


def test_hdiutil_seed_command_is_argument_list_not_shell():
    assert build_hdiutil_seed_command(Path("/s/seed.iso"), Path("/s/seed.d")) == [
        "hdiutil",
        "makehybrid",
        "-iso",
        "-joliet",
        "-default-volume-name",
        "CIDATA",
        "-o",
        "/s/seed.iso",
        "/s/seed.d",
    ]


def test_hdiutil_seed_command_rejects_relative_paths():
    with pytest.raises(KvmError):
        build_hdiutil_seed_command(Path("seed.iso"), Path("/s/seed.d"))
    with pytest.raises(KvmError):
        build_hdiutil_seed_command(Path("/s/seed.iso"), Path("seed.d"))


def test_run_hdiutil_seed_iso_reports_missing_tool_actionably():
    with patch("palimpsest_local.kvm.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(KvmError, match="hdiutil"):
            run_hdiutil_seed_iso(Path("/s/seed.iso"), Path("/s/seed.d"))


def test_run_hdiutil_seed_iso_passes_no_shell():
    with patch("palimpsest_local.kvm.subprocess.run") as run:
        run_hdiutil_seed_iso(Path("/s/seed.iso"), Path("/s/seed.d"))
    args, kwargs = run.call_args
    assert isinstance(args[0], list)
    assert "shell" not in kwargs or kwargs["shell"] is False


def test_activation_script_uses_by_id_not_device_names():
    disks = build_layer_disks(_ROOT, _DIGESTS)
    script = build_layer_activation_script(disks)
    assert "/dev/vdb" not in script
    assert all(f"/dev/disk/by-id/virtio-{disk.serial}" in script for disk in disks)


def test_activation_script_reverses_chain_for_lowerdir():
    script = build_layer_activation_script(build_layer_disks(_ROOT, _DIGESTS))
    lowerdir = next(line for line in script.splitlines() if "lowerdir=" in line)
    assert lowerdir.index("lower2") < lowerdir.index("lower1") < lowerdir.index("lower0")


def test_activation_script_keeps_upper_and_work_on_local_disk():
    script = build_layer_activation_script(build_layer_disks(_ROOT, _DIGESTS))
    assert "upperdir=/opt/layers/upper" in script
    assert "workdir=/opt/layers/work" in script


def test_activation_script_mounts_layers_read_only_and_waits_for_udev():
    script = build_layer_activation_script(build_layer_disks(_ROOT, _DIGESTS))
    assert script.count("mount -t squashfs -o ro") == 3
    assert "mount -t nfs" not in script
    assert "nfs4" not in script
    assert "seq 1 30" in script
    assert script.startswith("set -euo pipefail")


def test_activation_script_quotes_merged_dir():
    assert "'/opt/layers/my merged'" in build_layer_activation_script(
        build_layer_disks(_ROOT, _DIGESTS), merged_dir="/opt/layers/my merged"
    )


def test_activation_script_skips_overlay_mount_without_layers():
    script = build_layer_activation_script([])
    assert "mount -t overlay" not in script
    assert "mkdir -p /opt/layers/upper /opt/layers/work /opt/layers/merged" in script


def test_connect_is_unavailable_without_uri():
    with pytest.raises(KvmUnavailable, match="kvm_uri"):
        connect("")


def test_connect_reports_missing_libvirt_actionably():
    with patch("palimpsest_local.kvm._libvirt", side_effect=KvmUnavailable("libvirt-python not installed")):
        with pytest.raises(KvmUnavailable):
            connect("qemu:///system")


def test_validate_network_requires_exact_active_network():
    active = MagicMock()
    active.isActive.return_value = 1
    conn = MagicMock()
    conn.networkLookupByName.return_value = active

    validate_network("default", conn=conn)

    conn.networkLookupByName.assert_called_once_with("default")


def test_validate_network_rejects_missing_inactive_and_indeterminate():
    missing = MagicMock()
    missing.networkLookupByName.side_effect = RuntimeError("missing")
    with pytest.raises(KvmError, match="does not exist"):
        validate_network("missing", conn=missing)

    inactive_network = MagicMock()
    inactive_network.isActive.return_value = 0
    inactive = MagicMock()
    inactive.networkLookupByName.return_value = inactive_network
    with pytest.raises(KvmError, match="not active"):
        validate_network("default", conn=inactive)

    unknown_network = MagicMock()
    unknown_network.isActive.side_effect = RuntimeError("cannot query")
    unknown = MagicMock()
    unknown.networkLookupByName.return_value = unknown_network
    with pytest.raises(KvmError, match="cannot determine"):
        validate_network("default", conn=unknown)


def test_validate_network_skips_all_checks_for_user_hostfwd_profile():
    conn = MagicMock()
    validate_network("Not A Valid Name!", conn=conn, profile=_HVF_PROFILE)
    conn.networkLookupByName.assert_not_called()


def test_validate_domain_name_available_rejects_collision_and_ambiguity():
    class FakeLibvirtError(RuntimeError):
        def __init__(self, message: str, code: int):
            super().__init__(message)
            self.code = code

        def get_error_code(self) -> int:
            return self.code

    fake_libvirt = SimpleNamespace(libvirtError=FakeLibvirtError, VIR_ERR_NO_DOMAIN=42)
    missing = MagicMock()
    missing.lookupByName.side_effect = FakeLibvirtError("missing", 42)
    collision = MagicMock()
    collision.lookupByName.return_value = MagicMock()
    ambiguous = MagicMock()
    ambiguous.lookupByName.side_effect = FakeLibvirtError("connection failed", 99)

    with patch("palimpsest_local.kvm._libvirt", return_value=fake_libvirt):
        validate_domain_name_available("palimpsest-demo", conn=missing)
        with pytest.raises(KvmError, match="already reserved"):
            validate_domain_name_available("palimpsest-demo", conn=collision)
        with pytest.raises(KvmError, match="cannot determine"):
            validate_domain_name_available("palimpsest-demo", conn=ambiguous)


def test_destroy_and_undefine_is_best_effort_when_domain_absent():
    fake_libvirt = MagicMock()
    fake_libvirt.libvirtError = RuntimeError
    conn = MagicMock()
    conn.lookupByName.side_effect = RuntimeError("no domain")
    with patch("palimpsest_local.kvm._libvirt", return_value=fake_libvirt):
        destroy_and_undefine(conn, "palimpsest-demo")
    conn.lookupByName.assert_called_once_with("palimpsest-demo")


def test_get_domain_run_id_prefers_libvirt_metadata_and_falls_back_to_domain_xml():
    run_id = "862ffb44-6795-4618-b2d8-c0750439fac3"
    libvirt = SimpleNamespace(VIR_DOMAIN_METADATA_ELEMENT=2)
    metadata_domain = MagicMock()
    metadata_domain.metadata.return_value = (
        f'<palimpsest:run xmlns:palimpsest="{DOMAIN_MARKER_NAMESPACE}" '
        f'id="{run_id}" schema="1" version="{DOMAIN_MARKER_VERSION}" />'
    )
    with patch("palimpsest_local.kvm._libvirt", return_value=libvirt):
        assert get_domain_run_id(metadata_domain) == run_id

    fallback_domain = MagicMock()
    fallback_domain.metadata.side_effect = RuntimeError("metadata API unavailable")
    fallback_spec = DomainSpec(**{**_spec([]).__dict__, "run_id": run_id})
    fallback_domain.XMLDesc.return_value = build_domain_xml(fallback_spec, _X86_PROFILE)
    with patch("palimpsest_local.kvm._libvirt", return_value=libvirt):
        assert get_domain_run_id(fallback_domain) == run_id


def test_get_domain_run_id_fails_closed_for_missing_or_malformed_metadata():
    libvirt = SimpleNamespace(VIR_DOMAIN_METADATA_ELEMENT=2)
    domain = MagicMock()
    domain.metadata.return_value = "<not-xml"
    domain.XMLDesc.return_value = "<also-not-xml"
    with patch("palimpsest_local.kvm._libvirt", return_value=libvirt):
        assert get_domain_run_id(domain) is None


@pytest.mark.parametrize(
    "metadata_xml",
    [
        f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" schema="1" version="{DOMAIN_MARKER_VERSION}" />',
        f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="not-a-uuid" schema="1" version="{DOMAIN_MARKER_VERSION}" />',
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862FFB44-6795-4618-B2D8-C0750439FAC3" '
            f'schema="1" version="{DOMAIN_MARKER_VERSION}" />'
        ),
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb4467954618b2d8c0750439fac3" '
            f'schema="1" version="{DOMAIN_MARKER_VERSION}" />'
        ),
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            f'version="{DOMAIN_MARKER_VERSION}" />'
        ),
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            f'schema="2" version="{DOMAIN_MARKER_VERSION}" />'
        ),
        (f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb44-6795-4618-b2d8-c0750439fac3" schema="1" />'),
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            'schema="1" version="9.9.9" />'
        ),
        (f'<run id="862ffb44-6795-4618-b2d8-c0750439fac3" schema="1" version="{DOMAIN_MARKER_VERSION}" />'),
        (
            '<marker xmlns="https://example.invalid/foreign" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            f'schema="1" version="{DOMAIN_MARKER_VERSION}" />'
        ),
        (
            f'<marker xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            f'schema="1" version="{DOMAIN_MARKER_VERSION}" />'
        ),
    ],
)
def test_get_domain_run_id_rejects_untrusted_marker_shapes(metadata_xml: str):
    libvirt = SimpleNamespace(VIR_DOMAIN_METADATA_ELEMENT=2)
    domain = MagicMock()
    domain.metadata.return_value = metadata_xml
    domain.XMLDesc.return_value = "<domain><metadata /></domain>"
    with patch("palimpsest_local.kvm._libvirt", return_value=libvirt):
        assert get_domain_run_id(domain) is None


@pytest.mark.parametrize(
    "metadata_xml",
    [
        None,
        "<not-xml",
        f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" schema="1" version="{DOMAIN_MARKER_VERSION}" />',
        (
            '<run xmlns="https://example.invalid/foreign" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            f'schema="1" version="{DOMAIN_MARKER_VERSION}" />'
        ),
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862FFB44-6795-4618-B2D8-C0750439FAC3" '
            f'schema="1" version="{DOMAIN_MARKER_VERSION}" />'
        ),
    ],
)
def test_get_domain_run_id_uses_valid_xmldesc_after_untrusted_metadata(metadata_xml: str | None):
    run_id = "862ffb44-6795-4618-b2d8-c0750439fac3"
    libvirt = SimpleNamespace(VIR_DOMAIN_METADATA_ELEMENT=2)
    domain = MagicMock()
    domain.metadata.return_value = metadata_xml
    fallback_spec = DomainSpec(**{**_spec([]).__dict__, "run_id": run_id})
    domain.XMLDesc.return_value = build_domain_xml(fallback_spec, _X86_PROFILE)
    with patch("palimpsest_local.kvm._libvirt", return_value=libvirt):
        assert get_domain_run_id(domain) == run_id


@pytest.mark.parametrize(
    "marker_xml",
    [
        f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" schema="1" version="{DOMAIN_MARKER_VERSION}" />',
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            f'schema="2" version="{DOMAIN_MARKER_VERSION}" />'
        ),
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            'schema="1" version="9.9.9" />'
        ),
        (
            f'<run xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862FFB44-6795-4618-B2D8-C0750439FAC3" '
            f'schema="1" version="{DOMAIN_MARKER_VERSION}" />'
        ),
        (
            f'<marker xmlns="{DOMAIN_MARKER_NAMESPACE}" id="862ffb44-6795-4618-b2d8-c0750439fac3" '
            f'schema="1" version="{DOMAIN_MARKER_VERSION}" />'
        ),
    ],
)
def test_get_domain_run_id_rejects_untrusted_xmldesc_fallback_markers(marker_xml: str):
    libvirt = SimpleNamespace(VIR_DOMAIN_METADATA_ELEMENT=2)
    domain = MagicMock()
    domain.metadata.side_effect = RuntimeError("metadata API unavailable")
    domain.XMLDesc.return_value = f"<domain><metadata>{marker_xml}</metadata></domain>"
    with patch("palimpsest_local.kvm._libvirt", return_value=libvirt):
        assert get_domain_run_id(domain) is None


def test_domain_xml_builder_does_not_parse_untrusted_input():
    source = inspect.getsource(build_domain_xml)
    assert "fromstring" not in source
    assert "ET.parse" not in source


def test_kvm_golden_bytes_are_stable():
    fixture_dir = Path(__file__).parents[1] / "fixtures"
    disks = build_layer_disks(_ROOT, _DIGESTS)
    assert build_domain_xml(_spec(disks), _X86_PROFILE) == (fixture_dir / "domain.xml").read_text(encoding="utf-8")
    assert build_layer_activation_script(disks) == (fixture_dir / "layer-activation.sh").read_text(encoding="utf-8")
    expected_argv = (
        "\0".join(
            build_seed_iso_command(
                Path("/var/lib/palimpsest/runs/demo/seed.iso"),
                Path("/var/lib/palimpsest/runs/demo/user-data"),
                Path("/var/lib/palimpsest/runs/demo/meta-data"),
            )
        )
        + "\0"
    )
    assert expected_argv == (fixture_dir / "seed-argv.nul").read_text(encoding="utf-8")


def test_kvm_golden_bytes_are_stable_for_aarch64_kvm():
    fixture_dir = Path(__file__).parents[1] / "fixtures"
    disks = build_layer_disks(_ROOT, _DIGESTS)
    xml_text = build_domain_xml(_spec(disks), _AARCH64_KVM_PROFILE)
    assert xml_text == (fixture_dir / "domain-aarch64.xml").read_text(encoding="utf-8")
    xml = ET.fromstring(xml_text)
    assert xml.get("type") == "kvm"
    assert xml.find("./os").get("firmware") == "efi"
    assert xml.find("./os/loader") is None
    assert xml.find("./os/nvram") is None
    assert xml.find("./features/apic") is None
    assert xml.find("./features/acpi") is not None
    controller = xml.find("./devices/controller")
    assert controller is not None and controller.attrib == {"type": "scsi", "index": "0", "model": "virtio-scsi"}
    cdrom = next(disk for disk in xml.findall("./devices/disk") if disk.get("device") == "cdrom")
    assert cdrom.find("target").get("bus") == "scsi"
    assert xml.find("./devices/interface") is not None


def test_kvm_golden_bytes_are_stable_for_hvf():
    fixture_dir = Path(__file__).parents[1] / "fixtures"
    disks = build_layer_disks(_ROOT, _DIGESTS)
    spec = DomainSpec(
        **{
            **_spec(disks).__dict__,
            "ssh_host_port": 2222,
            "nvram": Path("/var/lib/palimpsest/domains/demo-nvram.fd"),
        }
    )
    xml_text = build_domain_xml(spec, _HVF_PROFILE)
    assert xml_text == (fixture_dir / "domain-hvf.xml").read_text(encoding="utf-8")
    assert 'xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0"' in xml_text
    xml = ET.fromstring(xml_text)
    assert xml.get("type") == "hvf"
    assert xml.find("./os").get("firmware") is None
    assert xml.find("./os/loader").text == "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"
    assert xml.find("./os/nvram").text == "/var/lib/palimpsest/domains/demo-nvram.fd"
    assert xml.find("./features/apic") is None
    assert xml.find("./features/acpi") is not None
    controller = xml.find("./devices/controller")
    assert controller is not None and controller.attrib == {"type": "scsi", "index": "0", "model": "virtio-scsi"}
    cdrom = next(disk for disk in xml.findall("./devices/disk") if disk.get("device") == "cdrom")
    assert cdrom.find("target").get("bus") == "scsi"
    assert xml.find("./devices/interface") is None
    qemu_ns = "http://libvirt.org/schemas/domain/qemu/1.0"
    args = [arg.get("value") for arg in xml.findall(f"./{{{qemu_ns}}}commandline/{{{qemu_ns}}}arg")]
    assert args == [
        "-netdev",
        "user,id=palimpsest0,hostfwd=tcp:127.0.0.1:2222-:22",
        "-device",
        "virtio-net-pci,netdev=palimpsest0",
    ]


def test_domain_xml_rejects_missing_nvram_for_pflash_firmware():
    disks = build_layer_disks(_ROOT, _DIGESTS)
    spec = DomainSpec(**{**_spec(disks).__dict__, "ssh_host_port": 2222})
    with pytest.raises(KvmError, match="nvram"):
        build_domain_xml(spec, _HVF_PROFILE)


def test_domain_xml_rejects_relative_nvram_for_pflash_firmware():
    disks = build_layer_disks(_ROOT, _DIGESTS)
    spec = DomainSpec(**{**_spec(disks).__dict__, "ssh_host_port": 2222, "nvram": Path("relative-nvram.fd")})
    with pytest.raises(KvmError, match="nvram"):
        build_domain_xml(spec, _HVF_PROFILE)


def test_domain_xml_rejects_missing_ssh_host_port_for_user_hostfwd():
    disks = build_layer_disks(_ROOT, _DIGESTS)
    spec = DomainSpec(**{**_spec(disks).__dict__, "nvram": Path("/var/lib/palimpsest/domains/demo-nvram.fd")})
    with pytest.raises(KvmError, match="ssh host port"):
        build_domain_xml(spec, _HVF_PROFILE)


@pytest.mark.parametrize("bad_port", [0, 65536, -1])
def test_domain_xml_rejects_out_of_range_ssh_host_port(bad_port):
    disks = build_layer_disks(_ROOT, _DIGESTS)
    spec = DomainSpec(
        **{
            **_spec(disks).__dict__,
            "ssh_host_port": bad_port,
            "nvram": Path("/var/lib/palimpsest/domains/demo-nvram.fd"),
        }
    )
    with pytest.raises(KvmError, match="ssh host port"):
        build_domain_xml(spec, _HVF_PROFILE)


def test_kvm_marker_uses_stable_namespace_prefix():
    spec = DomainSpec(**{**_spec().__dict__, "run_id": "run-uuid"})
    assert '<palimpsest:run id="run-uuid" schema="1" version="0.1.0" />' in build_domain_xml(spec, _X86_PROFILE)


def test_pure_tests_never_import_afterglow_app():
    for path in Path(__file__).parents[1].rglob("test_*.py"):
        assert not re.search(r"^\s*(from|import)\s+backend\b", path.read_text(encoding="utf-8"), re.MULTILINE), path
