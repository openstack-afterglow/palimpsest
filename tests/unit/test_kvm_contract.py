"""Behavioral contract for the standalone KVM primitives."""

from __future__ import annotations

import inspect
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from palimpsest_local.kvm import (
    MAX_LAYER_DISKS,
    DomainSpec,
    KvmError,
    KvmUnavailable,
    build_domain_xml,
    build_layer_activation_script,
    build_layer_disks,
    build_seed_iso_command,
    connect,
    destroy_and_undefine,
    layer_blob_path,
    run_seed_iso,
)

_ROOT = Path("/var/lib/palimpsest/layers")
_DIGESTS = [f"sha256:{chr(ord('a') + index) * 64}" for index in range(3)]


def _spec(layers=None) -> DomainSpec:
    return DomainSpec(
        name="palimpsest-demo",
        memory_mib=4096,
        vcpus=2,
        root_disk=Path("/var/lib/palimpsest/domains/demo.qcow2"),
        seed_iso=Path("/var/lib/palimpsest/domains/demo-seed.iso"),
        layers=layers if layers is not None else build_layer_disks(_ROOT, _DIGESTS),
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
    xml = ET.fromstring(build_domain_xml(_spec()))
    layers = [
        disk
        for disk in xml.findall("./devices/disk")
        if (disk.find("target") is not None and disk.find("target").get("dev", "").startswith("vd"))
        and disk.find("target").get("dev") != "vda"
    ]
    assert len(layers) == 3
    assert all(disk.find("readonly") is not None and disk.find("driver").get("type") == "raw" for disk in layers)


def test_domain_xml_root_disk_is_writable_qcow2():
    xml = ET.fromstring(build_domain_xml(_spec()))
    root = next(disk for disk in xml.findall("./devices/disk") if disk.find("target").get("dev") == "vda")
    assert root.find("readonly") is None
    assert root.find("driver").get("type") == "qcow2"


def test_domain_xml_carries_serial_for_stable_guest_lookup():
    disks = build_layer_disks(_ROOT, _DIGESTS)
    xml = ET.fromstring(build_domain_xml(_spec(disks)))
    assert [disk.findtext("serial") for disk in xml.findall("./devices/disk") if disk.findtext("serial")] == [
        disk.serial for disk in disks
    ]


def test_domain_xml_attaches_nocloud_seed_as_cdrom():
    xml = ET.fromstring(build_domain_xml(_spec()))
    cdrom = next(disk for disk in xml.findall("./devices/disk") if disk.get("device") == "cdrom")
    assert cdrom.find("source").get("file").endswith("-seed.iso")
    assert cdrom.find("readonly") is not None


def test_builder_domain_has_only_explicit_serial_channel():
    spec = DomainSpec(
        **{**_spec().__dict__, "guest_agent": False, "control_socket": Path("/run/palimpsest/builder.sock")}
    )
    xml = ET.fromstring(build_domain_xml(spec))
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
        build_domain_xml(DomainSpec(**{**spec.__dict__, field: value}))


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


def test_destroy_and_undefine_is_best_effort_when_domain_absent():
    fake_libvirt = MagicMock()
    fake_libvirt.libvirtError = RuntimeError
    conn = MagicMock()
    conn.lookupByName.side_effect = RuntimeError("no domain")
    with patch("palimpsest_local.kvm._libvirt", return_value=fake_libvirt):
        destroy_and_undefine(conn, "palimpsest-demo")
    conn.lookupByName.assert_called_once_with("palimpsest-demo")


def test_module_generates_xml_without_parsing_untrusted_input():
    assert "fromstring" not in inspect.getsource(__import__("palimpsest_local.kvm", fromlist=["*"]))
    assert "ET.parse" not in inspect.getsource(__import__("palimpsest_local.kvm", fromlist=["*"]))


def test_kvm_golden_bytes_are_stable():
    fixture_dir = Path(__file__).parents[1] / "fixtures"
    disks = build_layer_disks(_ROOT, _DIGESTS)
    assert build_domain_xml(_spec(disks)) == (fixture_dir / "domain.xml").read_text(encoding="utf-8")
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


def test_kvm_marker_uses_stable_namespace_prefix():
    spec = DomainSpec(**{**_spec().__dict__, "run_id": "run-uuid"})
    assert '<palimpsest:run id="run-uuid" schema="1" version="0.1.0" />' in build_domain_xml(spec)


def test_pure_tests_never_import_afterglow_app():
    for path in Path(__file__).parents[1].rglob("test_*.py"):
        assert not re.search(r"^\s*(from|import)\s+backend\b", path.read_text(encoding="utf-8"), re.MULTILINE), path
