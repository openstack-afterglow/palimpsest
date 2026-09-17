from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local import pci_preflight
from palimpsest_local.pci_preflight import PCIPreflightError, inspect_pci_device


def _fact(parent: Path, name: str, value: str) -> None:
    (parent / name).write_text(value + "\n", encoding="ascii")


def _sysfs(tmp_path: Path, *, members: tuple[str, ...] = ("0000:01:00.0", "0000:01:00.1")) -> Path:
    root = tmp_path / "sys"
    real_devices = root / "devices" / "pci0000:00"
    bus_devices = root / "bus" / "pci" / "devices"
    drivers = root / "bus" / "pci" / "drivers"
    group = root / "kernel" / "iommu_groups" / "17"
    for directory in (real_devices, bus_devices, drivers / "nvidia", group / "devices"):
        directory.mkdir(parents=True, exist_ok=True)
    for address in members:
        device = real_devices / address
        device.mkdir()
        for name, value in {
            "vendor": "0x10de",
            "device": "0x2208",
            "subsystem_vendor": "0x1043",
            "subsystem_device": "0x87c7",
            "class": "0x030000",
            "revision": "0xa1",
        }.items():
            _fact(device, name, value)
        (bus_devices / address).symlink_to(device)
        (group / "devices" / address).symlink_to(device)
    selected = real_devices / members[0]
    _fact(selected, "boot_vga", "1")
    (selected / "reset").touch()
    (selected / "driver").symlink_to(drivers / "nvidia")
    (selected / "iommu_group").symlink_to(group)
    return root


def test_reports_exact_read_only_group_and_optional_facts(tmp_path: Path):
    root = _sysfs(tmp_path)
    before = {path.relative_to(root): os.lstat(path) for path in root.rglob("*")}

    report = inspect_pci_device("0000:01:00.0", sysfs_root=root)

    assert report.schema == "palimpsest.pci-preflight.v1"
    assert (report.vendor_id, report.device_id, report.class_code, report.revision) == (
        "0x10de",
        "0x2208",
        "0x030000",
        "0xa1",
    )
    assert (report.subsystem_vendor_id, report.subsystem_device_id) == ("0x1043", "0x87c7")
    assert (report.driver_status, report.driver) == ("bound", "nvidia")
    assert report.boot_display == "yes" and report.reset_surface == "present"
    assert report.iommu_group == 17
    assert report.iommu_group_members == ("0000:01:00.0", "0000:01:00.1")
    after = {path.relative_to(root): os.lstat(path) for path in root.rglob("*")}
    assert before.keys() == after.keys()
    assert all(
        (before[name].st_dev, before[name].st_ino) == (after[name].st_dev, after[name].st_ino) for name in before
    )


def test_missing_optional_surfaces_report_non_authoritative_states(tmp_path: Path):
    root = _sysfs(tmp_path, members=("0000:01:00.0",))
    device = root / "devices" / "pci0000:00" / "0000:01:00.0"
    (device / "driver").unlink()
    (device / "iommu_group").unlink()
    (device / "boot_vga").unlink()
    (device / "reset").unlink()

    report = inspect_pci_device("0000:01:00.0", sysfs_root=root)

    assert (report.driver_status, report.driver) == ("unbound", None)
    assert report.boot_display == "unknown" and report.reset_surface == "absent"
    assert report.iommu_group is None and report.iommu_group_members == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vendor_id", "10de"),
        ("device_id", "0x12345"),
        ("subsystem_vendor_id", True),
        ("subsystem_device_id", "0xzzzz"),
        ("class_code", "0x0300"),
        ("revision", "0x001"),
    ],
)
def test_typed_report_rejects_malformed_identity_facts(tmp_path: Path, field: str, value):
    report = inspect_pci_device("0000:01:00.0", sysfs_root=_sysfs(tmp_path))
    with pytest.raises(ValueError, match="identity fact"):
        replace(report, **{field: value})


def test_fact_reader_ignores_sysfs_reported_size_but_caps_actual_bytes(tmp_path: Path, monkeypatch):
    parent = tmp_path / "facts"
    parent.mkdir()
    _fact(parent, "vendor", "0x10de")
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    original = pci_preflight.os.fstat

    def page_sized(fd):
        result = original(fd)
        values = list(result)
        values[6] = 4096
        return os.stat_result(values)

    try:
        monkeypatch.setattr(pci_preflight.os, "fstat", page_sized)
        assert pci_preflight._read_fact(descriptor, "vendor", pci_preflight._HEX) == "0x10de"
        (parent / "vendor").write_bytes(b"0" * 65)
        with pytest.raises(PCIPreflightError, match="changed"):
            pci_preflight._read_fact(descriptor, "vendor", pci_preflight._HEX)
    finally:
        os.close(descriptor)


def test_rejects_out_of_range_iommu_group_as_fixed_preflight_error(tmp_path: Path):
    root = _sysfs(tmp_path)
    device = root / "devices" / "pci0000:00" / "0000:01:00.0"
    old_group = root / "kernel" / "iommu_groups" / "17"
    large_group = old_group.with_name("2147483648")
    old_group.rename(large_group)
    (device / "iommu_group").unlink()
    (device / "iommu_group").symlink_to(large_group)

    with pytest.raises(PCIPreflightError, match="IOMMU group is invalid"):
        inspect_pci_device("0000:01:00.0", sysfs_root=root)


def test_rejects_driver_link_swap_during_inspection(tmp_path: Path, monkeypatch):
    root = _sysfs(tmp_path)
    selected = root / "devices" / "pci0000:00" / "0000:01:00.0"
    replacement = root / "bus" / "pci" / "drivers" / "vfio-pci"
    replacement.mkdir()
    original = pci_preflight._revalidate_link
    swapped = False

    def swap_driver(visible: Path, expected: os.stat_result) -> None:
        nonlocal swapped
        if visible.name == "driver" and not swapped:
            swapped = True
            (selected / "driver").unlink()
            (selected / "driver").symlink_to(replacement)
        original(visible, expected)

    monkeypatch.setattr(pci_preflight, "_revalidate_link", swap_driver)
    with pytest.raises(PCIPreflightError, match="link changed"):
        inspect_pci_device("0000:01:00.0", sysfs_root=root)
    assert swapped


@pytest.mark.parametrize("address", ["01:00.0", "0000:01:00.8", "0000:01:00.0/driver", True])
def test_rejects_noncanonical_address_without_sysfs_read(tmp_path: Path, address):
    with pytest.raises(PCIPreflightError, match="canonical"):
        inspect_pci_device(address, sysfs_root=tmp_path / "absent")


@pytest.mark.parametrize(
    "mutation",
    [
        "device-link",
        "device-alias",
        "member-link",
        "member-alias",
        "dangling-driver",
        "dangling-group",
        "missing-selected-member",
        "fact-link",
        "malformed-fact",
        "large-group",
    ],
)
def test_rejects_substitution_malformed_facts_and_bounds(tmp_path: Path, mutation: str):
    root = _sysfs(tmp_path)
    real = root / "devices" / "pci0000:00"
    selected = real / "0000:01:00.0"
    if mutation == "device-link":
        (root / "bus" / "pci" / "devices" / "0000:01:00.0").unlink()
        (root / "bus" / "pci" / "devices" / "0000:01:00.0").symlink_to(tmp_path)
    elif mutation == "device-alias":
        link = root / "bus" / "pci" / "devices" / "0000:01:00.0"
        link.unlink()
        link.symlink_to(real / "0000:01:00.1")
    elif mutation == "member-link":
        member = root / "kernel" / "iommu_groups" / "17" / "devices" / "0000:01:00.1"
        member.unlink()
        member.symlink_to(tmp_path)
    elif mutation == "member-alias":
        member = root / "kernel" / "iommu_groups" / "17" / "devices" / "0000:01:00.0"
        member.unlink()
        member.symlink_to(real / "0000:01:00.1")
    elif mutation == "dangling-driver":
        (selected / "driver").unlink()
        (selected / "driver").symlink_to(root / "bus" / "pci" / "drivers" / "missing")
    elif mutation == "dangling-group":
        (selected / "iommu_group").unlink()
        (selected / "iommu_group").symlink_to(root / "kernel" / "iommu_groups" / "99")
    elif mutation == "missing-selected-member":
        (root / "kernel" / "iommu_groups" / "17" / "devices" / "0000:01:00.0").unlink()
    elif mutation == "fact-link":
        (selected / "vendor").unlink()
        (selected / "vendor").symlink_to(selected / "device")
    elif mutation == "malformed-fact":
        _fact(selected, "vendor", "0xZZZZ")
    else:
        group_devices = root / "kernel" / "iommu_groups" / "17" / "devices"
        for index in range(2, 65):
            address = f"0000:02:{index // 8:02x}.{index % 8}"
            device = real / address
            device.mkdir()
            (group_devices / address).symlink_to(device)

    with pytest.raises(PCIPreflightError):
        inspect_pci_device("0000:01:00.0", sysfs_root=root)
