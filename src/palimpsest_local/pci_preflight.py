"""Bounded, read-only PCI sysfs facts; never allocation or assignment authority."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import StateError

_BDF = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.[0-7]")
_HEX = re.compile(r"0x[0-9a-f]{4}")
_CLASS = re.compile(r"0x[0-9a-f]{6}")
_REVISION = re.compile(r"0x[0-9a-f]{2}")
_DRIVER = re.compile(r"[A-Za-z0-9_.+-]{1,128}")
_MAX_FACT_BYTES = 64
_MAX_GROUP_MEMBERS = 64


class PCIPreflightError(StateError):
    """Fixed failure from a non-mutating PCI inventory read."""


@dataclass(frozen=True, slots=True)
class PCIPreflightReport:
    schema: str
    address: str
    vendor_id: str
    device_id: str
    subsystem_vendor_id: str
    subsystem_device_id: str
    class_code: str
    revision: str
    driver_status: str
    driver: str | None
    boot_display: str
    reset_surface: str
    iommu_group: int | None
    iommu_group_members: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema != "palimpsest.pci-preflight.v1" or _BDF.fullmatch(self.address) is None:
            raise ValueError("PCI preflight report identity is invalid")
        for value, pattern in (
            (self.vendor_id, _HEX),
            (self.device_id, _HEX),
            (self.subsystem_vendor_id, _HEX),
            (self.subsystem_device_id, _HEX),
            (self.class_code, _CLASS),
            (self.revision, _REVISION),
        ):
            if type(value) is not str or pattern.fullmatch(value) is None:
                raise ValueError("PCI preflight identity fact is invalid")
        if self.driver_status not in {"bound", "unbound", "unknown"}:
            raise ValueError("PCI preflight driver status is invalid")
        if (self.driver_status == "bound") != (self.driver is not None):
            raise ValueError("PCI preflight driver fact is inconsistent")
        if self.driver is not None and _DRIVER.fullmatch(self.driver) is None:
            raise ValueError("PCI preflight driver is invalid")
        if self.boot_display not in {"yes", "no", "unknown"}:
            raise ValueError("PCI preflight boot-display fact is invalid")
        if self.reset_surface not in {"present", "absent", "unknown"}:
            raise ValueError("PCI preflight reset fact is invalid")
        if (self.iommu_group is None) != (not self.iommu_group_members):
            raise ValueError("PCI preflight IOMMU facts are inconsistent")
        if self.iommu_group is not None and (
            not 0 <= self.iommu_group <= 2**31 - 1
            or tuple(sorted(set(self.iommu_group_members))) != self.iommu_group_members
            or self.address not in self.iommu_group_members
            or any(_BDF.fullmatch(member) is None for member in self.iommu_group_members)
        ):
            raise ValueError("PCI preflight IOMMU group is invalid")


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _open_directory(path: Path) -> tuple[int, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise PCIPreflightError("PCI sysfs directory is invalid")
    return descriptor, info


def _read_fact(directory_fd: int, name: str, pattern: re.Pattern[str]) -> str:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or not _same(before, visible):
            raise PCIPreflightError("PCI sysfs fact is invalid")
        content = bytearray()
        while len(content) <= _MAX_FACT_BYTES:
            chunk = os.read(descriptor, _MAX_FACT_BYTES + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) > _MAX_FACT_BYTES or not _same(before, after):
            raise PCIPreflightError("PCI sysfs fact changed")
    finally:
        os.close(descriptor)
    try:
        value = bytes(content).decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        raise PCIPreflightError("PCI sysfs fact is invalid") from None
    if pattern.fullmatch(value) is None:
        raise PCIPreflightError("PCI sysfs fact is invalid")
    return value


def _resolved_link(parent: Path, name: str, allowed_parent: Path) -> tuple[Path, int]:
    visible = parent / name
    descriptor = -1
    try:
        if not stat.S_ISLNK(visible.lstat().st_mode):
            raise PCIPreflightError("PCI sysfs link is invalid")
        target = visible.resolve(strict=True)
        allowed = allowed_parent.resolve(strict=True)
        target.relative_to(allowed)
        descriptor, opened = _open_directory(target)
        followed = visible.stat()
        if not _same(opened, followed):
            raise PCIPreflightError("PCI sysfs link changed")
    except PCIPreflightError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except (OSError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
        raise PCIPreflightError("PCI sysfs link is invalid") from None
    return target, descriptor


def _optional_link(parent: Path, name: str) -> bool:
    try:
        info = (parent / name).lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISLNK(info.st_mode):
        raise PCIPreflightError("PCI sysfs link is invalid")
    return True


def _revalidate_link(visible: Path, expected: os.stat_result) -> None:
    try:
        if not stat.S_ISLNK(visible.lstat().st_mode) or not _same(visible.stat(), expected):
            raise PCIPreflightError("PCI sysfs link changed")
    except OSError:
        raise PCIPreflightError("PCI sysfs link changed") from None


def _bounded_names(directory_fd: int) -> list[str]:
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if len(names) == _MAX_GROUP_MEMBERS:
                raise PCIPreflightError("PCI IOMMU group is invalid")
            names.append(entry.name)
    return sorted(names)


def inspect_pci_device(address: str, *, sysfs_root: Path = Path("/sys")) -> PCIPreflightReport:
    """Read one exact PCI device and its current IOMMU group without mutation."""

    if type(address) is not str or _BDF.fullmatch(address) is None:
        raise PCIPreflightError("PCI address must be canonical")
    try:
        root = sysfs_root.resolve(strict=True)
        if not root.is_absolute() or not stat.S_ISDIR(root.stat().st_mode):
            raise PCIPreflightError("PCI sysfs root is invalid")
        devices = root / "bus" / "pci" / "devices"
        device, device_fd = _resolved_link(devices, address, root / "devices")
        if device.name != address:
            os.close(device_fd)
            raise PCIPreflightError("PCI device identity is invalid")
        device_identity = os.fstat(device_fd)
        try:
            facts = {
                "vendor_id": _read_fact(device_fd, "vendor", _HEX),
                "device_id": _read_fact(device_fd, "device", _HEX),
                "subsystem_vendor_id": _read_fact(device_fd, "subsystem_vendor", _HEX),
                "subsystem_device_id": _read_fact(device_fd, "subsystem_device", _HEX),
                "class_code": _read_fact(device_fd, "class", _CLASS),
                "revision": _read_fact(device_fd, "revision", _REVISION),
            }
            driver_status, driver = "unbound", None
            if _optional_link(device, "driver"):
                driver_path, driver_fd = _resolved_link(device, "driver", root / "bus" / "pci" / "drivers")
                driver_identity = os.fstat(driver_fd)
                try:
                    driver = driver_path.name
                    if (
                        driver_path.parent != (root / "bus" / "pci" / "drivers").resolve(strict=True)
                        or _DRIVER.fullmatch(driver) is None
                    ):
                        raise PCIPreflightError("PCI driver fact is invalid")
                    driver_status = "bound"
                    _revalidate_link(device / "driver", driver_identity)
                finally:
                    os.close(driver_fd)
            boot_display = "unknown"
            try:
                boot_display = {"0": "no", "1": "yes"}[_read_fact(device_fd, "boot_vga", re.compile("[01]"))]
            except FileNotFoundError:
                pass
            try:
                reset_info = os.stat("reset", dir_fd=device_fd, follow_symlinks=False)
                if not stat.S_ISREG(reset_info.st_mode):
                    raise PCIPreflightError("PCI reset fact is invalid")
                reset_surface = "present"
            except FileNotFoundError:
                reset_surface = "absent"
            group_members: tuple[str, ...] = ()
            group_number = None
            if _optional_link(device, "iommu_group"):
                group_path, group_fd = _resolved_link(device, "iommu_group", root / "kernel" / "iommu_groups")
                group_identity = os.fstat(group_fd)
                try:
                    if (
                        group_path.parent != (root / "kernel" / "iommu_groups").resolve(strict=True)
                        or re.fullmatch(r"0|[1-9][0-9]{0,9}", group_path.name) is None
                    ):
                        raise PCIPreflightError("PCI IOMMU group is invalid")
                    group_number = int(group_path.name)
                    if group_number > 2**31 - 1:
                        raise PCIPreflightError("PCI IOMMU group is invalid")
                    members_path = group_path / "devices"
                    members_fd, members_identity = _open_directory(members_path)
                    try:
                        names = _bounded_names(members_fd)
                        if not names or address not in names or any(_BDF.fullmatch(name) is None for name in names):
                            raise PCIPreflightError("PCI IOMMU group is invalid")
                        member_identities: list[tuple[Path, os.stat_result]] = []
                        for name in names:
                            member_path, member_fd = _resolved_link(members_path, name, root / "devices")
                            try:
                                if member_path.name != name:
                                    raise PCIPreflightError("PCI IOMMU group is invalid")
                                member_identities.append((members_path / name, os.fstat(member_fd)))
                            finally:
                                os.close(member_fd)
                        if not _same(members_identity, members_path.stat()):
                            raise PCIPreflightError("PCI IOMMU group changed")
                        for visible, identity in member_identities:
                            _revalidate_link(visible, identity)
                        group_members = tuple(names)
                    finally:
                        os.close(members_fd)
                    _revalidate_link(device / "iommu_group", group_identity)
                finally:
                    os.close(group_fd)
            _revalidate_link(devices / address, device_identity)
            if not _same(device_identity, device.stat()):
                raise PCIPreflightError("PCI device changed during inspection")
        finally:
            os.close(device_fd)
    except PCIPreflightError:
        raise
    except (OSError, KeyError, ValueError):
        raise PCIPreflightError("PCI sysfs inspection failed") from None
    return PCIPreflightReport(
        "palimpsest.pci-preflight.v1",
        address,
        **facts,
        driver_status=driver_status,
        driver=driver,
        boot_display=boot_display,
        reset_surface=reset_surface,
        iommu_group=group_number,
        iommu_group_members=group_members,
    )


__all__ = ["PCIPreflightError", "PCIPreflightReport", "inspect_pci_device"]
