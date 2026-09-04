"""Opt-in live qualification of the production-inert OCI-root libvirt handoff."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

import palimpsest_local.oci_root_kvm as oci_root_kvm_module
from palimpsest_local import kvm, oci_root_runtime, platforms
from palimpsest_local._oci_stage1_kvm_proof import (
    KERNEL_CONFIG_ENV,
    KERNEL_ENV,
    LIFECYCLE_READY_COMMITTED_MARKER,
    ROOT_TRANSITION_MARKER,
    WORKLOAD_STARTED_MARKER,
    load_proof_filesystems,
    verify_kernel_config,
    verify_kvm_api,
)
from palimpsest_local.errors import StateError
from palimpsest_local.oci_converter import (
    DEFAULT_LAYER_CONVERSION_LIMITS,
    LAYER_INTAKE_POLICY_ID,
    LayerIntakeReceipt,
)
from palimpsest_local.oci_initramfs import build_bootstrap_initramfs
from palimpsest_local.oci_materializer import OCIImageMaterializationReceipt
from palimpsest_local.oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
    SQUASHFS_STRUCTURAL_VERIFIER_ID,
    LeasedSquashFS,
    PackedSquashFSReceipt,
    SquashFSToolchainIdentity,
    VerifiedSquashFSToolchain,
)
from palimpsest_local.oci_process import OCIProcessSpec, OCIUserSpec
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    Descriptor,
)
from palimpsest_local.oci_root_kvm import (
    build_oci_root_domain_plan,
    commit_oci_root_domain_plan,
    verify_first_party_bootstrap_initramfs,
    verify_host_boot_artifacts,
)
from palimpsest_local.oci_root_prepare import (
    PreparedOCIRootRun,
    prepare_oci_root_run,
    release_prepared_oci_root_run,
)
from palimpsest_local.oci_root_runtime import (
    define_committed_oci_root_domain,
    launch_defined_oci_root_domain,
)
from palimpsest_local.oci_root_volume import load_oci_root_volume
from palimpsest_local.oci_store import (
    DerivedLayerOccurrence,
    DerivedSquashFSKey,
    MaterializationResult,
    OCIStore,
)
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind
from palimpsest_local.state import StatePaths, init_resolved_roots, read_run_ledger_snapshot, reserve_new_run

pytestmark = [pytest.mark.kvm, pytest.mark.oci_root_libvirt]

_ENABLE_ENV = "PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT"
_ROOT_SIZE_BYTES = 16 * 1024 * 1024
_DAC_BASELABEL_RE = re.compile(r"^\+((?:0|[1-9][0-9]*)):\+((?:0|[1-9][0-9]*))$")
_MAX_DAC_ID = 2**32 - 2
_MAX_CONSOLE_TAIL_BYTES = 128 * 1024


def _strict_scalar(element: ET.Element, *, attributes: dict[str, str] | None = None) -> str:
    if (
        element.attrib != (attributes or {})
        or list(element)
        or element.text is None
        or element.text.strip() != element.text
        or (element.tail is not None and element.tail.strip())
    ):
        raise ValueError("libvirt DAC capability is not canonical")
    return element.text


def _parse_qemu_dac_baselabel(capabilities: str) -> tuple[int, int]:
    if not isinstance(capabilities, str):
        raise ValueError("libvirt capabilities are invalid")
    try:
        root = ET.fromstring(capabilities)
    except ET.ParseError:
        raise ValueError("libvirt capabilities are invalid") from None
    if root.tag != "capabilities" or root.attrib or (root.text is not None and root.text.strip()):
        raise ValueError("libvirt capabilities root is invalid")
    hosts = [child for child in root if child.tag == "host"]
    if len(hosts) != 1:
        raise ValueError("libvirt capabilities host is ambiguous")
    host = hosts[0]
    dac_models: list[ET.Element] = []
    for secmodel in (child for child in host if child.tag == "secmodel"):
        models = [child for child in secmodel if child.tag == "model"]
        if any(model.text == "dac" for model in models):
            dac_models.append(secmodel)
    if len(dac_models) != 1:
        raise ValueError("libvirt DAC capability is ambiguous")
    secmodel = dac_models[0]
    if secmodel.attrib or (secmodel.text is not None and secmodel.text.strip()):
        raise ValueError("libvirt DAC capability is not canonical")
    models = [child for child in secmodel if child.tag == "model"]
    if len(models) != 1 or _strict_scalar(models[0]) != "dac":
        raise ValueError("libvirt DAC capability is not canonical")
    dois = [child for child in secmodel if child.tag == "doi"]
    if len(dois) != 1 or _strict_scalar(dois[0]) != "0":
        raise ValueError("libvirt DAC capability is not canonical")
    labels = [child for child in secmodel if child.tag == "baselabel" and child.get("type") == "kvm"]
    if len(labels) != 1:
        raise ValueError("libvirt KVM DAC baselabel is ambiguous")
    encoded = _strict_scalar(labels[0], attributes={"type": "kvm"})
    match = _DAC_BASELABEL_RE.fullmatch(encoded)
    if match is None:
        raise ValueError("libvirt KVM DAC baselabel is invalid")
    uid, gid = (int(value) for value in match.groups())
    if uid > _MAX_DAC_ID or gid > _MAX_DAC_ID:
        raise ValueError("libvirt KVM DAC baselabel is invalid")
    return uid, gid


def _stat_identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _validate_qualification_acl_target(root: Path, target: Path, *, kind: str) -> tuple[Path, os.stat_result]:
    """Return a closed-FD identity snapshot, not an ACL mutation authority.

    A future path-based ``setfacl`` call must repeat this walk immediately before
    mutation and still cannot make the validation/mutation pair atomic.  Keeping
    the final descriptor open and addressing ``/proc/self/fd/<n>`` narrows that
    gap on Linux, but ``setfacl`` itself remains pathname-based.
    """

    if kind not in {"directory", "regular"} or not root.is_absolute() or not target.is_absolute():
        raise ValueError("qualification ACL target is invalid")
    try:
        root_lexical = Path(os.path.abspath(root))
        target_lexical = Path(os.path.abspath(target))
        root_resolved = root.resolve(strict=True)
        target_resolved = target.resolve(strict=True)
        relative = target_lexical.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("qualification ACL target escapes the test root") from None
    if root_lexical != root_resolved or target_lexical != target_resolved:
        raise ValueError("qualification ACL target contains a symlink")
    descriptors: list[int] = []
    try:
        root_visible = root_resolved.lstat()
        root_fd = os.open(root_resolved, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
        descriptors.append(root_fd)
        root_opened = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_visible.st_mode)
            or not stat.S_ISDIR(root_opened.st_mode)
            or (root_visible.st_dev, root_visible.st_ino) != (root_opened.st_dev, root_opened.st_ino)
            or root_visible.st_uid != os.geteuid()
            or root_opened.st_uid != os.geteuid()
        ):
            raise ValueError("qualification ACL root identity is unsafe")
        opened = root_opened
        parent_fd = root_fd
        for index, component in enumerate(relative.parts):
            expected_kind = kind if index == len(relative.parts) - 1 else "directory"
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if expected_kind == "directory":
                flags |= os.O_DIRECTORY
            visible = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            opened = os.fstat(child_fd)
            predicate = stat.S_ISDIR if expected_kind == "directory" else stat.S_ISREG
            if (
                not predicate(visible.st_mode)
                or not predicate(opened.st_mode)
                or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
                or visible.st_uid != os.geteuid()
                or opened.st_uid != os.geteuid()
            ):
                raise ValueError("qualification ACL target identity is unsafe")
            parent_fd = child_fd
        final = target_resolved.lstat()
    except ValueError:
        raise
    except OSError:
        raise ValueError("qualification ACL target cannot be securely opened") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    predicate = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if (
        not predicate(opened.st_mode)
        or not predicate(final.st_mode)
        or (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino)
        or opened.st_uid != os.geteuid()
        or final.st_uid != os.geteuid()
    ):
        raise ValueError("qualification ACL target identity is unsafe")
    return target_resolved, opened


def _remove_failed_kernel_copy(destination: Path, expected: os.stat_result) -> None:
    try:
        visible = destination.lstat()
    except OSError:
        raise ValueError("failed qualified kernel copy identity changed") from None
    if (
        not stat.S_ISREG(visible.st_mode)
        or visible.st_uid != os.geteuid()
        or (visible.st_dev, visible.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ValueError("failed qualified kernel copy identity changed")
    try:
        destination.unlink()
    except OSError:
        raise ValueError("failed qualified kernel copy could not be removed") from None


def _copy_qualified_kernel(source: Path, test_root: Path) -> Path:
    try:
        source = source.resolve(strict=True)
        visible = source.lstat()
        source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        raise ValueError("qualified kernel cannot be securely opened") from None
    destination = test_root / "k"
    destination_fd: int | None = None
    created: os.stat_result | None = None
    try:
        opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(visible.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("qualified kernel identity is unsafe")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                created = os.fstat(destination_fd)
            except OSError:
                raise ValueError(
                    "qualified kernel copy identity is unavailable; partial destination was preserved"
                ) from None
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise ValueError("qualified kernel copy did not make progress")
                    view = view[written:]
            os.fchmod(destination_fd, 0o400)
            os.fsync(destination_fd)
            after = os.fstat(source_fd)
            current = source.lstat()
            if _stat_identity(after) != _stat_identity(opened) or _stat_identity(current) != _stat_identity(opened):
                raise ValueError("qualified kernel changed while it was copied")
            _, validated = _validate_qualification_acl_target(test_root, destination, kind="regular")
            destination_current = os.fstat(destination_fd)
            if not (
                (validated.st_dev, validated.st_ino)
                == (created.st_dev, created.st_ino)
                == (destination_current.st_dev, destination_current.st_ino)
            ):
                raise ValueError("qualified kernel copy destination identity changed")
            if stat.S_IMODE(destination_current.st_mode) != 0o400:
                raise ValueError("qualified kernel copy mode is unsafe")
        except (OSError, ValueError) as exc:
            if created is None:
                # O_EXCL proves that this call created some directory entry, but
                # without an FD identity a same-UID replacement is
                # indistinguishable.  Preserve the path instead of unlinking an
                # inode that this call may not own.
                if isinstance(exc, ValueError):
                    raise
                raise ValueError(
                    "qualified kernel copy identity is unavailable; partial destination was preserved"
                ) from None
            try:
                _remove_failed_kernel_copy(destination, created)
            except ValueError as cleanup_error:
                raise cleanup_error from exc
            if isinstance(exc, ValueError):
                raise
            raise ValueError("qualified kernel cannot be securely copied") from None
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    return destination


def _create_qualification_console(test_root: Path) -> Path:
    destination = test_root / "console.log"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        visible = destination.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(visible.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or opened.st_size != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
            or _stat_identity(visible) != _stat_identity(opened)
        ):
            raise ValueError("qualification console identity is unsafe")
    except OSError:
        raise ValueError("qualification console cannot be securely created") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return destination


def _qualification_console_tail(path: Path) -> str:
    descriptor: int | None = None
    try:
        visible = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(visible.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or visible.st_uid != os.geteuid()
            or opened.st_uid != os.geteuid()
            or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_nlink != 1
            or opened.st_size < 0
        ):
            return "<qualification console identity is unsafe>"
        offset = max(0, opened.st_size - _MAX_CONSOLE_TAIL_BYTES)
        payload = os.pread(descriptor, min(opened.st_size, _MAX_CONSOLE_TAIL_BYTES), offset)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino) or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            return "<qualification console identity changed while reading>"
        prefix = "[truncated] " if offset else ""
        return f"{prefix}{payload.decode('utf-8', errors='backslashreplace')}"
    except OSError as exc:
        return f"<qualification console unavailable: {type(exc).__name__}>"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _annotate_qualification_console_failure(error: BaseException, path: Path) -> None:
    error.add_note(f"qualification guest console tail:\n{_qualification_console_tail(path)}")


def _remove_failed_lower_copy(parent_descriptor: int, name: str, expected: os.stat_result) -> None:
    try:
        visible = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        raise ValueError("failed qualified lower copy identity changed") from None
    if (
        not stat.S_ISREG(visible.st_mode)
        or visible.st_uid != os.geteuid()
        or (visible.st_dev, visible.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ValueError("failed qualified lower copy identity changed")
    try:
        os.unlink(name, dir_fd=parent_descriptor)
    except OSError:
        raise ValueError("failed qualified lower copy could not be removed") from None


def _hash_pinned_file(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise ValueError("qualified lower copy did not contain its declared size")
        digest.update(chunk)
        offset += len(chunk)
    return f"sha256:{digest.hexdigest()}"


def _stage_qualified_lower(source: Path, digest: str, size: int, stage_root: Path) -> Path:
    """Create or revalidate one owner-only server-qualified raw-disk test copy."""

    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest or "") is None or type(size) is not int or size < 0:
        raise ValueError("qualified lower identity is invalid")
    try:
        stage_root_resolved, stage_identity = _validate_qualification_acl_target(
            stage_root,
            stage_root,
            kind="directory",
        )
        source_visible = source.lstat()
        source_descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        raise ValueError("qualified lower source cannot be securely opened") from None
    destination = stage_root_resolved / f"{digest.removeprefix('sha256:')}.raw"
    destination_name = destination.name
    stage_descriptor: int | None = None
    destination_descriptor: int | None = None
    created: os.stat_result | None = None
    try:
        source_opened = os.fstat(source_descriptor)
        stage_descriptor = os.open(
            stage_root_resolved,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        stage_opened = os.fstat(stage_descriptor)
        if (
            not stat.S_ISDIR(stage_identity.st_mode)
            or stage_identity.st_uid != os.geteuid()
            or stat.S_IMODE(stage_identity.st_mode) != 0o700
            or _stat_identity(stage_opened) != _stat_identity(stage_identity)
            or not source.is_absolute()
            or not stat.S_ISREG(source_visible.st_mode)
            or not stat.S_ISREG(source_opened.st_mode)
            or source_visible.st_uid != os.geteuid()
            or source_opened.st_uid != os.geteuid()
            or source_opened.st_nlink != 1
            or stat.S_IMODE(source_opened.st_mode) not in {0o400, 0o444}
            or source_opened.st_size != size
            or (source_visible.st_dev, source_visible.st_ino) != (source_opened.st_dev, source_opened.st_ino)
            or _hash_pinned_file(source_descriptor, size) != digest
        ):
            raise ValueError("qualified lower source identity is unsafe")
        source_after_hash = os.fstat(source_descriptor)
        source_current = source.lstat()
        if _stat_identity(source_after_hash) != _stat_identity(source_opened) or _stat_identity(
            source_current
        ) != _stat_identity(source_opened):
            raise ValueError("qualified lower source changed during verification")

        try:
            destination_descriptor = os.open(
                destination_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=stage_descriptor,
            )
        except FileExistsError:
            destination_descriptor = os.open(
                destination_name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=stage_descriptor,
            )
            existing = os.fstat(destination_descriptor)
            existing_visible = os.stat(destination_name, dir_fd=stage_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_uid != os.geteuid()
                or existing.st_nlink != 1
                or stat.S_IMODE(existing.st_mode) != 0o400
                or existing.st_size != size
                or (existing_visible.st_dev, existing_visible.st_ino) != (existing.st_dev, existing.st_ino)
                or _hash_pinned_file(destination_descriptor, size) != digest
                or _stat_identity(os.fstat(destination_descriptor)) != _stat_identity(existing)
                or _stat_identity(destination.lstat()) != _stat_identity(existing)
            ):
                raise ValueError("qualified lower staged copy is invalid") from None
            return destination

        try:
            try:
                created = os.fstat(destination_descriptor)
            except OSError:
                raise ValueError(
                    "qualified lower copy identity is unavailable; partial destination was preserved"
                ) from None
            offset = 0
            while offset < size:
                chunk = os.pread(source_descriptor, min(1024 * 1024, size - offset), offset)
                if not chunk:
                    raise ValueError("qualified lower source changed while it was copied")
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    if written <= 0:
                        raise ValueError("qualified lower copy did not make progress")
                    view = view[written:]
                offset += len(chunk)
            os.fchmod(destination_descriptor, 0o400)
            os.fsync(destination_descriptor)
            source_after_copy = os.fstat(source_descriptor)
            source_current = source.lstat()
            destination_current = os.fstat(destination_descriptor)
            destination_visible = os.stat(destination_name, dir_fd=stage_descriptor, follow_symlinks=False)
            if _stat_identity(source_after_copy) != _stat_identity(source_opened) or _stat_identity(
                source_current
            ) != _stat_identity(source_opened):
                raise ValueError("qualified lower source changed while it was copied")
            if (
                not stat.S_ISREG(destination_current.st_mode)
                or destination_current.st_uid != os.geteuid()
                or destination_current.st_nlink != 1
                or stat.S_IMODE(destination_current.st_mode) != 0o400
                or destination_current.st_size != size
                or (destination_current.st_dev, destination_current.st_ino) != (created.st_dev, created.st_ino)
                or (destination_visible.st_dev, destination_visible.st_ino) != (created.st_dev, created.st_ino)
                or _hash_pinned_file(destination_descriptor, size) != digest
                or _stat_identity(os.fstat(destination_descriptor)) != _stat_identity(destination_current)
                or _stat_identity(destination.lstat()) != _stat_identity(destination_current)
            ):
                raise ValueError("qualified lower changed while it was staged")
        except (OSError, ValueError) as exc:
            if created is None:
                if isinstance(exc, ValueError):
                    raise
                raise ValueError(
                    "qualified lower copy identity is unavailable; partial destination was preserved"
                ) from None
            try:
                _remove_failed_lower_copy(stage_descriptor, destination_name, created)
            except ValueError as cleanup_error:
                raise cleanup_error from exc
            if isinstance(exc, ValueError):
                raise
            raise ValueError("qualified lower cannot be securely staged") from None
        return destination
    finally:
        os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if stage_descriptor is not None:
            os.close(stage_descriptor)


def _create_qualification_root() -> tuple[Path, os.stat_result]:
    temporary_parent = Path("/tmp")
    try:
        parent = temporary_parent.lstat()
    except OSError:
        raise ValueError("qualification /tmp prerequisite is unavailable") from None
    if (
        stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) != 0o1777
    ):
        raise ValueError("qualification /tmp prerequisite is unsafe")
    path = Path(tempfile.mkdtemp(prefix="p-", dir=temporary_parent))
    visible = path.lstat()
    if not stat.S_ISDIR(visible.st_mode) or visible.st_uid != os.geteuid() or stat.S_IMODE(visible.st_mode) != 0o700:
        raise ValueError("qualification root is unsafe")
    return path, visible


def _acl_mode(perms: str) -> int:
    if not re.fullmatch(r"[r-][w-][x-]", perms):
        raise ValueError("qualification ACL permissions are invalid")
    return (4 if perms[0] == "r" else 0) | (2 if perms[1] == "w" else 0) | (1 if perms[2] == "x" else 0)


@dataclass(frozen=True)
class _ACLStructure:
    user: str
    named_users: tuple[tuple[int, str], ...]
    group: str
    mask: str | None
    other: str

    def setfile_text(self) -> str:
        lines = [f"user::{self.user}"]
        lines.extend(f"user:{uid}:{permission}" for uid, permission in self.named_users)
        lines.append(f"group::{self.group}")
        if self.mask is not None:
            lines.append(f"mask::{self.mask}")
        lines.append(f"other::{self.other}")
        return "\n".join(lines) + "\n"


def _parse_acl(payload: str) -> _ACLStructure:
    if not isinstance(payload, str) or not payload.endswith("\n"):
        raise ValueError("qualification target ACL is noncanonical")
    lines = payload.splitlines()
    trailing_blank_lines = 0
    while lines and lines[-1] == "":
        lines.pop()
        trailing_blank_lines += 1
    if trailing_blank_lines > 1:
        raise ValueError("qualification target ACL is noncanonical")
    if any(not line for line in lines) or len(lines) < 3:
        raise ValueError("qualification target ACL is noncanonical")

    def permission(line: str, prefix: str) -> str:
        if not line.startswith(prefix) or len(line) != len(prefix) + 3:
            raise ValueError("qualification target ACL is noncanonical")
        value = line.removeprefix(prefix)
        _acl_mode(value)
        return value

    index = 0
    user = permission(lines[index], "user::")
    index += 1
    named_users: list[tuple[int, str]] = []
    while index < len(lines) and lines[index].startswith("user:") and not lines[index].startswith("user::"):
        match = re.fullmatch(r"user:(0|[1-9][0-9]*):([r-][w-][x-])", lines[index])
        if match is None:
            raise ValueError("qualification target ACL is noncanonical")
        uid = int(match.group(1))
        if uid > _MAX_DAC_ID or any(existing == uid for existing, _ in named_users):
            raise ValueError("qualification target ACL is noncanonical")
        named_users.append((uid, match.group(2)))
        index += 1
    if index >= len(lines):
        raise ValueError("qualification target ACL is noncanonical")
    group = permission(lines[index], "group::")
    index += 1
    mask: str | None = None
    if index < len(lines) and lines[index].startswith("mask::"):
        mask = permission(lines[index], "mask::")
        index += 1
    if index >= len(lines):
        raise ValueError("qualification target ACL is noncanonical")
    other = permission(lines[index], "other::")
    index += 1
    if index != len(lines) or (named_users and mask is None):
        raise ValueError("qualification target ACL is noncanonical")
    return _ACLStructure(user, tuple(named_users), group, mask, other)


@dataclass
class _HeldACLTarget:
    path: Path
    descriptor: int
    opened: os.stat_result
    permission: str
    original_acl: _ACLStructure

    @property
    def fd_path(self) -> str:
        return f"/proc/self/fd/{self.descriptor}"


class _QualificationDACBroker:
    def __init__(
        self,
        root: Path,
        uid: int,
        specifications: tuple[tuple[Path, str, str], ...],
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if uid == os.geteuid() or not 0 <= uid <= _MAX_DAC_ID:
            raise ValueError("qualification DAC uid is invalid")
        self.root = root.resolve(strict=True)
        self.uid = uid
        self._runner = runner
        self.targets: list[_HeldACLTarget] = []
        self.applied = False
        self.restored = False
        self.ambiguous = False
        try:
            for path, kind, permission in specifications:
                resolved, snapshot = _validate_qualification_acl_target(self.root, path, kind=kind)
                flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
                if kind == "directory":
                    flags |= os.O_DIRECTORY
                descriptor = os.open(resolved, flags)
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (snapshot.st_dev, snapshot.st_ino):
                    os.close(descriptor)
                    raise ValueError("qualification ACL target changed before it was held")
                _acl_mode(permission)
                target = _HeldACLTarget(
                    resolved,
                    descriptor,
                    opened,
                    permission,
                    _ACLStructure("---", (), "---", None, "---"),
                )
                self.targets.append(target)
                target.original_acl = self._getfacl(target)
                if target.original_acl.named_users or target.original_acl.mask is not None:
                    raise ValueError("qualification target has a pre-existing extended ACL")
                expected_mode = (
                    (_acl_mode(target.original_acl.user) << 6)
                    | (_acl_mode(target.original_acl.group) << 3)
                    | _acl_mode(target.original_acl.other)
                )
                if stat.S_IMODE(opened.st_mode) != expected_mode:
                    raise ValueError("qualification ACL snapshot and mode disagree")
        except BaseException:
            self.close()
            raise

    def _command(self, arguments: list[str], target: _HeldACLTarget, *, input_text: str | None = None) -> str:
        result = self._runner(
            arguments,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            pass_fds=(target.descriptor,),
            timeout=10,
        )
        if result.returncode != 0 or result.stderr or not isinstance(result.stdout, str):
            raise ValueError("qualification ACL command failed")
        return result.stdout

    def _getfacl(self, target: _HeldACLTarget) -> _ACLStructure:
        return _parse_acl(self._command(["getfacl", "-cpn", "--", target.fd_path], target))

    def _verify_held(self, target: _HeldACLTarget) -> os.stat_result:
        current = os.fstat(target.descriptor)
        visible = target.path.lstat()
        if (
            (current.st_dev, current.st_ino) != (target.opened.st_dev, target.opened.st_ino)
            or (visible.st_dev, visible.st_ino) != (target.opened.st_dev, target.opened.st_ino)
            or current.st_uid != os.geteuid()
            or visible.st_uid != os.geteuid()
            or current.st_gid != target.opened.st_gid
            or visible.st_gid != target.opened.st_gid
            or current.st_nlink != target.opened.st_nlink
            or visible.st_nlink != target.opened.st_nlink
            or stat.S_IFMT(current.st_mode) != stat.S_IFMT(target.opened.st_mode)
            or stat.S_IFMT(visible.st_mode) != stat.S_IFMT(target.opened.st_mode)
        ):
            raise ValueError("qualification ACL held target identity changed")
        return current

    def apply(self) -> None:
        if self.applied or self.restored or self.ambiguous:
            raise ValueError("qualification ACL broker state is invalid")
        touched: list[_HeldACLTarget] = []
        try:
            for target in self.targets:
                self._verify_held(target)
                touched.append(target)
                self._command(
                    ["setfacl", "-m", f"u:{self.uid}:{target.permission}", "--", target.fd_path],
                    target,
                )
                original = target.original_acl
                mask = _acl_mode(original.group) | _acl_mode(target.permission)
                mask_text = f"{'r' if mask & 4 else '-'}{'w' if mask & 2 else '-'}{'x' if mask & 1 else '-'}"
                expected_acl = _ACLStructure(
                    original.user,
                    ((self.uid, target.permission),),
                    original.group,
                    mask_text,
                    original.other,
                )
                actual_acl = self._getfacl(target)
                current = self._verify_held(target)
                if actual_acl != expected_acl or stat.S_IMODE(current.st_mode) != (
                    (_acl_mode(original.user) << 6) | (mask << 3) | _acl_mode(original.other)
                ):
                    raise ValueError("qualification ACL grant verification failed")
            self.applied = True
        except BaseException as exc:
            try:
                self._restore_targets(tuple(reversed(touched)))
            except BaseException as restore_error:
                self.applied = True
                self.ambiguous = True
                raise ValueError("qualification ACL rollback is ambiguous") from restore_error
            self.restored = True
            raise exc

    def _restore_targets(self, targets: tuple[_HeldACLTarget, ...]) -> None:
        for target in targets:
            self._verify_held(target)
            self._command(
                ["setfacl", "--set-file=-", "--", target.fd_path],
                target,
                input_text=target.original_acl.setfile_text(),
            )
            current = self._verify_held(target)
            if self._getfacl(target) != target.original_acl or stat.S_IMODE(current.st_mode) != stat.S_IMODE(
                target.opened.st_mode
            ):
                raise ValueError("qualification ACL restoration failed")

    def restore(self) -> None:
        if not self.applied or self.restored or self.ambiguous:
            raise ValueError("qualification ACL broker cannot restore")
        try:
            self._restore_targets(tuple(reversed(self.targets)))
        except BaseException:
            self.ambiguous = True
            raise
        self.restored = True

    def restore_after_domain_absent(self) -> None:
        if not self.applied or self.restored:
            raise ValueError("qualification ACL broker cannot restore")
        try:
            self._restore_targets(tuple(reversed(self.targets)))
        except BaseException:
            self.ambiguous = True
            raise
        self.ambiguous = False
        self.restored = True

    def close(self) -> None:
        if self.ambiguous or (self.applied and not self.restored):
            raise ValueError("qualification ACL broker cannot release held targets")
        for target in reversed(self.targets):
            try:
                os.close(target.descriptor)
            except OSError:
                pass
        self.targets.clear()


class _ActivationDomainProxy:
    def __init__(self, domain: Any, domain_uuid: str, broker: _QualificationDACBroker) -> None:
        self._domain = domain
        self._domain_uuid = domain_uuid
        self._broker = broker
        self._create_called = False

    def create(self) -> Any:
        if self._create_called:
            raise ValueError("qualification domain create proxy was reused")
        self._create_called = True
        self._broker.apply()
        try:
            result = self._domain.create()
        except BaseException:
            try:
                active = self._domain.isActive()
                domain_uuid = self._domain.UUIDString()
            except BaseException:
                self._broker.ambiguous = True
                raise
            if active != 0 or domain_uuid != self._domain_uuid:
                self._broker.ambiguous = True
            raise
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._domain, name)


class _ActivationConnectionProxy:
    def __init__(self, connection: Any, domain_uuid: str, broker: _QualificationDACBroker) -> None:
        self._connection = connection
        self._domain_uuid = domain_uuid
        self._broker = broker
        self.domain_proxy: _ActivationDomainProxy | None = None

    def _wrap_for_create(self, domain: Any) -> Any:
        if domain.UUIDString() != self._domain_uuid:
            return domain
        if self._broker.restored:
            return domain
        if self.domain_proxy is None:
            self.domain_proxy = _ActivationDomainProxy(domain, self._domain_uuid, self._broker)
        return self.domain_proxy

    def lookupByName(self, name: str) -> Any:
        return self._wrap_for_create(self._connection.lookupByName(name))

    def lookupByUUIDString(self, domain_uuid: str) -> Any:
        return self._connection.lookupByUUIDString(domain_uuid)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _qualification_acl_specifications(root: Path, domain_xml: str) -> tuple[tuple[Path, str, str], ...]:
    xml = ET.fromstring(domain_xml)
    files: dict[Path, str] = {}
    for element_name in ("kernel", "initrd"):
        value = xml.findtext(f"./os/{element_name}")
        if not value:
            raise ValueError("qualification boot artifact path is missing")
        files[Path(value)] = "r--"
    root_disks = 0
    for disk in xml.findall("./devices/disk"):
        source = disk.find("./source")
        target = disk.find("./target")
        labels = source.findall("./seclabel") if source is not None else []
        if (
            source is None
            or target is None
            or set(source.attrib) != {"file"}
            or set(target.attrib) != {"dev", "bus"}
            or len(labels) != 1
            or len(list(source)) != 1
            or labels[0].attrib != {"model": "dac", "relabel": "no"}
            or list(labels[0])
            or (source.text or "").strip()
            or (labels[0].text or "").strip()
            or (labels[0].tail or "").strip()
        ):
            raise ValueError("qualification disk binding is invalid")
        permission = "rw-" if target.get("dev") == "vda" else "r--"
        root_disks += permission == "rw-"
        path = Path(source.attrib["file"])
        if path in files:
            raise ValueError("qualification file ACL target is duplicated")
        files[path] = permission
    if root_disks != 1:
        raise ValueError("qualification root disk binding is invalid")
    consoles = xml.findall("./devices/console")
    if len(consoles) != 1:
        raise ValueError("qualification console binding is ambiguous")
    console = consoles[0]
    console_sources = console.findall("./source")
    console_targets = console.findall("./target")
    console_labels = console_sources[0].findall("./seclabel") if console_sources else []
    if (
        console.attrib != {"type": "file"}
        or len(console_sources) != 1
        or len(console_targets) != 1
        or len(list(console)) != 2
        or console_sources[0].attrib.get("append") != "on"
        or set(console_sources[0].attrib) != {"append", "path"}
        or console_targets[0].attrib != {"port": "0", "type": "serial"}
        or len(console_labels) != 1
        or len(list(console_sources[0])) != 1
        or console_labels[0].attrib != {"model": "dac", "relabel": "no"}
        or list(console_labels[0])
        or list(console_targets[0])
        or (console.text or "").strip()
        or (console_sources[0].text or "").strip()
        or (console_labels[0].text or "").strip()
        or (console_labels[0].tail or "").strip()
        or (console_targets[0].text or "").strip()
        or (console_targets[0].tail or "").strip()
    ):
        raise ValueError("qualification console binding is invalid")
    console_path = Path(console_sources[0].attrib["path"])
    if console_path in files:
        raise ValueError("qualification file ACL target is duplicated")
    files[console_path] = "rw-"
    channels = xml.findall("./devices/channel")
    lifecycle_parents: set[Path] = set()
    for channel in channels:
        target = channel.find("./target")
        source = channel.find("./source")
        if target is not None and target.get("name") == "org.palimpsest.oci.lifecycle.0":
            if source is None or set(source.attrib) != {"mode", "path"} or source.get("mode") != "bind":
                raise ValueError("qualification lifecycle socket binding is invalid")
            lifecycle_parents.add(Path(source.attrib["path"]).parent)
    if len(lifecycle_parents) != 1:
        raise ValueError("qualification lifecycle socket binding is ambiguous")
    directories: dict[Path, str] = {root: "--x"}
    for path in (*files, *lifecycle_parents):
        try:
            relative = path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            raise ValueError("qualification ACL target escapes the root") from None
        cursor = root
        for component in relative.parts[:-1] if path in files else relative.parts:
            cursor /= component
            directories.setdefault(cursor, "--x")
    for parent in lifecycle_parents:
        directories[parent] = "-wx"
    ordered_directories = sorted(directories.items(), key=lambda item: (len(item[0].parts), os.fspath(item[0])))
    ordered_files = sorted(files.items(), key=lambda item: os.fspath(item[0]))
    return tuple((path, "directory", permission) for path, permission in ordered_directories) + tuple(
        (path, "regular", permission) for path, permission in ordered_files
    )


def _empty_held_qualification_directory(descriptor: int, verify_root_binding: Callable[[], None]) -> None:
    names = os.listdir(descriptor)
    verify_root_binding()
    for name in names:
        verify_root_binding()
        if name in {"", ".", ".."} or "/" in name:
            raise ValueError("qualification root cleanup entry is invalid")
        visible = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(visible.st_mode):
            flags |= os.O_DIRECTORY
        child_descriptor = os.open(name, flags, dir_fd=descriptor)
        try:
            opened = os.fstat(child_descriptor)
            if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("qualification root cleanup entry identity changed")
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            held = os.fstat(child_descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise ValueError("qualification root cleanup entry identity changed")
            if stat.S_ISREG(held.st_mode) and held.st_nlink != 1:
                raise ValueError("qualification root cleanup entry link count is invalid")
            verify_root_binding()
            quarantine_name = f".p-{secrets.token_hex(16)}.cleanup"
            try:
                os.stat(quarantine_name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("qualification root cleanup child quarantine collided")
            verify_root_binding()
            os.rename(name, quarantine_name, src_dir_fd=descriptor, dst_dir_fd=descriptor)

            def verify_child_binding(
                child_quarantine_name: str = quarantine_name,
                held_child_descriptor: int = child_descriptor,
            ) -> None:
                verify_root_binding()
                quarantined = os.stat(child_quarantine_name, dir_fd=descriptor, follow_symlinks=False)
                child = os.fstat(held_child_descriptor)
                if (quarantined.st_dev, quarantined.st_ino) != (child.st_dev, child.st_ino):
                    raise ValueError("qualification root cleanup child identity changed while quarantined")

            verify_child_binding()
            if stat.S_ISDIR(held.st_mode):
                _empty_held_qualification_directory(child_descriptor, verify_child_binding)
            elif not stat.S_ISREG(held.st_mode):
                raise ValueError("qualification root cleanup entry kind is invalid")
            verify_child_binding()
            if stat.S_ISDIR(held.st_mode):
                os.rmdir(quarantine_name, dir_fd=descriptor)
            else:
                os.unlink(quarantine_name, dir_fd=descriptor)
            child = os.fstat(child_descriptor)
            if child.st_nlink != 0:
                raise ValueError("qualification root cleanup child unlink was not proven")
            try:
                os.stat(quarantine_name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("qualification root cleanup child quarantine remains")
        finally:
            os.close(child_descriptor)


def _remove_qualification_root(path: Path, expected: os.stat_result) -> None:
    if path.parent != Path("/tmp") or not path.name.startswith("p-"):
        raise ValueError("qualification root cleanup identity changed")
    parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_descriptor = -1
    quarantine_name = f".{path.name}.{secrets.token_hex(16)}.cleanup"
    try:
        root_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        visible = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        opened = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(visible.st_mode)
            or visible.st_uid != os.geteuid()
            or visible.st_gid != expected.st_gid
            or stat.S_IMODE(visible.st_mode) != 0o700
            or (visible.st_dev, visible.st_ino) != (expected.st_dev, expected.st_ino)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
            or opened.st_uid != visible.st_uid
            or opened.st_gid != visible.st_gid
            or opened.st_nlink != visible.st_nlink
        ):
            raise ValueError("qualification root cleanup identity changed")
        # Moving the held inode to a private random name makes a deterministic
        # same-UID replacement at the public name detectable before deletion.
        # A fully hostile same-UID process can still race any pathname unlink;
        # this qualification-only cleanup therefore rechecks the moved name
        # against the held FD and refuses deletion on every observed mismatch.
        os.rename(path.name, quarantine_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)

        def verify_quarantine_binding() -> None:
            quarantined = os.stat(quarantine_name, dir_fd=parent_descriptor, follow_symlinks=False)
            held = os.fstat(root_descriptor)
            if (quarantined.st_dev, quarantined.st_ino) != (expected.st_dev, expected.st_ino) or (
                held.st_dev,
                held.st_ino,
            ) != (expected.st_dev, expected.st_ino):
                raise ValueError("qualification root cleanup identity changed while quarantined")

        verify_quarantine_binding()
        _empty_held_qualification_directory(root_descriptor, verify_quarantine_binding)
        verify_quarantine_binding()
        os.rmdir(quarantine_name, dir_fd=parent_descriptor)
        held = os.fstat(root_descriptor)
        if held.st_nlink != 0:
            raise ValueError("qualification root cleanup unlink was not proven")
        try:
            os.stat(quarantine_name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("qualification root cleanup quarantine remains")
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def _remove_inactive_domain_then_restore(
    broker: _QualificationDACBroker,
    remove_exact_inactive: Callable[[], None],
    prove_absent: Callable[[], bool],
) -> None:
    try:
        remove_exact_inactive()
        if not prove_absent():
            raise ValueError("qualification domain absence was not proven")
    except BaseException:
        broker.ambiguous = True
        raise
    broker.restore_after_domain_absent()


_DAC_CAPABILITIES = """\
<capabilities>
  <host>
    <secmodel>
      <model>dac</model>
      <doi>0</doi>
      <baselabel type="qemu">+64055:+64055</baselabel>
      <baselabel type="kvm">+107:+108</baselabel>
    </secmodel>
  </host>
</capabilities>
"""


def test_qemu_dac_baselabel_parser_accepts_one_exact_kvm_identity() -> None:
    assert _parse_qemu_dac_baselabel(_DAC_CAPABILITIES) == (107, 108)


@pytest.mark.parametrize(
    "capabilities",
    [
        _DAC_CAPABILITIES.replace("<capabilities>", "<domainCapabilities>", 1).replace(
            "</capabilities>", "</domainCapabilities>", 1
        ),
        _DAC_CAPABILITIES.replace("</capabilities>", "<host /></capabilities>"),
        _DAC_CAPABILITIES.replace("</host>", "<secmodel><model>dac</model></secmodel></host>"),
        _DAC_CAPABILITIES.replace("<model>dac</model>", "<model>dac</model><model>dac</model>"),
        _DAC_CAPABILITIES.replace("<doi>0</doi>", "<doi>1</doi>"),
        _DAC_CAPABILITIES.replace('type="kvm"', 'type="kvm" extra="1"'),
        _DAC_CAPABILITIES.replace("</secmodel>", '<baselabel type="kvm">+109:+110</baselabel></secmodel>'),
        _DAC_CAPABILITIES.replace("+107:+108", "107:108"),
        _DAC_CAPABILITIES.replace("+107:+108", "+0107:+108"),
        _DAC_CAPABILITIES.replace("+107:+108", "+4294967295:+108"),
        _DAC_CAPABILITIES.replace('type="kvm"', 'type="xen"'),
    ],
)
def test_qemu_dac_baselabel_parser_rejects_ambiguous_or_noncanonical_input(capabilities: str) -> None:
    with pytest.raises(ValueError, match="libvirt"):
        _parse_qemu_dac_baselabel(capabilities)


def test_qualification_acl_target_requires_owned_stable_tmp_descendant(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    artifact = directory / "root.raw"
    artifact.write_bytes(b"root")

    resolved_directory, directory_identity = _validate_qualification_acl_target(tmp_path, directory, kind="directory")
    resolved_artifact, artifact_identity = _validate_qualification_acl_target(tmp_path, artifact, kind="regular")

    assert resolved_directory == directory.resolve()
    assert stat.S_ISDIR(directory_identity.st_mode)
    assert resolved_artifact == artifact.resolve()
    assert stat.S_ISREG(artifact_identity.st_mode)


def test_qualification_acl_target_rejects_escape_symlink_and_wrong_kind(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    artifact = directory / "root.raw"
    artifact.write_bytes(b"root")
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        _validate_qualification_acl_target(tmp_path, tmp_path.parent, kind="directory")
    with pytest.raises(ValueError, match="symlink"):
        _validate_qualification_acl_target(tmp_path, alias / artifact.name, kind="regular")
    with pytest.raises(ValueError, match="securely opened"):
        _validate_qualification_acl_target(tmp_path, artifact, kind="directory")


def test_qualification_acl_target_openat_walk_rejects_intermediate_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    displaced = tmp_path / "displaced"
    directory.mkdir()
    artifact = directory / "root.raw"
    artifact.write_bytes(b"root")
    original_open = os.open
    injected = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal injected
        if path == "run" and kwargs.get("dir_fd") is not None and not injected:
            injected = True
            directory.rename(displaced)
            directory.mkdir()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises(ValueError, match="identity"):
        _validate_qualification_acl_target(tmp_path, artifact, kind="regular")
    assert injected is True


def test_qualified_kernel_copy_is_owner_only_and_leaves_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    source.chmod(0o444)
    source_before = source.stat()

    copied = _copy_qualified_kernel(source, tmp_path)

    source_after = source.stat()
    assert copied.name == "k"
    assert copied.read_bytes() == b"kernel"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o400
    assert (source_after.st_dev, source_after.st_ino, source_after.st_mode) == (
        source_before.st_dev,
        source_before.st_ino,
        source_before.st_mode,
    )


@pytest.mark.parametrize("failure", ["read", "write", "fsync", "source-recheck", "final-validation"])
def test_qualified_kernel_copy_removes_exact_partial_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    destination = tmp_path / "k"

    def raise_oserror(*_args):
        raise OSError(failure)

    if failure == "read":
        monkeypatch.setattr(os, "read", raise_oserror)
    elif failure == "write":
        monkeypatch.setattr(os, "write", raise_oserror)
    elif failure == "fsync":
        monkeypatch.setattr(os, "fsync", raise_oserror)
    elif failure == "source-recheck":
        original_lstat = Path.lstat
        source_stats = 0

        def mutate_source(path: Path):
            nonlocal source_stats
            if path == source:
                source_stats += 1
                if source_stats == 2:
                    source.write_bytes(b"changed")
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", mutate_source)
    else:

        def fail_validation(*_args, **_kwargs):
            raise ValueError("injected final validation failure")

        monkeypatch.setitem(globals(), "_validate_qualification_acl_target", fail_validation)

    with pytest.raises(ValueError):
        _copy_qualified_kernel(source, tmp_path)

    assert not destination.exists()


def test_qualified_kernel_copy_does_not_remove_replaced_destination_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    destination = tmp_path / "k"

    def replace_then_fail(_descriptor: int) -> None:
        destination.unlink()
        destination.write_bytes(b"replacement")
        raise OSError("fsync")

    monkeypatch.setattr(os, "fsync", replace_then_fail)

    with pytest.raises(ValueError, match="identity changed"):
        _copy_qualified_kernel(source, tmp_path)

    assert destination.read_bytes() == b"replacement"


def test_qualified_kernel_copy_rejects_same_uid_replacement_on_success_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    destination = tmp_path / "k"
    original_validator = _validate_qualification_acl_target

    def replace_during_validation(root: Path, target: Path, *, kind: str):
        destination.unlink()
        destination.write_bytes(b"replacement")
        return original_validator(root, target, kind=kind)

    monkeypatch.setitem(globals(), "_validate_qualification_acl_target", replace_during_validation)

    with pytest.raises(ValueError, match="identity changed"):
        _copy_qualified_kernel(source, tmp_path)

    assert destination.read_bytes() == b"replacement"


def test_qualified_kernel_copy_preserves_partial_when_initial_fd_identity_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    destination = tmp_path / "k"
    original_fstat = os.fstat
    calls = 0

    def fail_destination_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected destination fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", fail_destination_fstat)

    with pytest.raises(ValueError, match="identity is unavailable.*preserved"):
        _copy_qualified_kernel(source, tmp_path)

    assert destination.is_file()
    assert destination.read_bytes() == b""


def test_qualification_console_is_owner_only_and_tail_is_bounded_binary_safe(tmp_path: Path) -> None:
    console = _create_qualification_console(tmp_path)
    assert console.name == "console.log"
    assert console.read_bytes() == b""
    assert stat.S_IMODE(console.stat().st_mode) == 0o600

    payload = b"a" * (_MAX_CONSOLE_TAIL_BYTES + 7) + b"\xff\nlast"
    console.write_bytes(payload)
    tail = _qualification_console_tail(console)
    assert tail.startswith("[truncated] ")
    assert "\\xff\nlast" in tail
    assert len(tail.encode("utf-8")) <= _MAX_CONSOLE_TAIL_BYTES + 32


def test_qualification_console_tail_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"attacker")
    console = tmp_path / "console.log"
    console.symlink_to(target)

    assert "unavailable" in _qualification_console_tail(console)


def test_qualification_console_failure_note_preserves_primary_error(tmp_path: Path) -> None:
    console = _create_qualification_console(tmp_path)
    console.write_bytes(b"stage1 failed: \xff")
    primary = RuntimeError("launch failed")

    _annotate_qualification_console_failure(primary, console)

    assert str(primary) == "launch failed"
    assert primary.__notes__ == ["qualification guest console tail:\nstage1 failed: \\xff"]


def _qualified_lower_fixture(tmp_path: Path) -> tuple[Path, str, int, Path]:
    payload = b"qualified-squashfs-lower"
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    source = (tmp_path / "source").resolve()
    source.write_bytes(payload)
    source.chmod(0o400)
    stage_root = tmp_path / "l"
    stage_root.mkdir(mode=0o700)
    return source, digest, len(payload), stage_root


def test_qualified_lower_stage_is_owner_only_and_revalidates_both_files(tmp_path: Path) -> None:
    source, digest, size, stage_root = _qualified_lower_fixture(tmp_path)
    source_before = source.stat()

    staged = _stage_qualified_lower(source, digest, size, stage_root)
    assert staged.name == f"{digest.removeprefix('sha256:')}.raw"
    assert staged.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o400
    assert _stage_qualified_lower(source, digest, size, stage_root) == staged
    source_after = source.stat()
    assert (source_after.st_dev, source_after.st_ino, source_after.st_mode) == (
        source_before.st_dev,
        source_before.st_ino,
        source_before.st_mode,
    )

    source.chmod(0o600)
    source.write_bytes(b"x" * size)
    source.chmod(0o400)
    with pytest.raises(ValueError, match="source identity"):
        _stage_qualified_lower(source, digest, size, stage_root)


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_qualified_lower_stage_removes_exact_partial_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source, digest, size, stage_root = _qualified_lower_fixture(tmp_path)
    destination = stage_root / f"{digest.removeprefix('sha256:')}.raw"

    def raise_oserror(*_args):
        raise OSError(failure)

    monkeypatch.setattr(os, failure, raise_oserror)
    with pytest.raises(ValueError):
        _stage_qualified_lower(source, digest, size, stage_root)
    assert not destination.exists()


def test_qualified_lower_stage_rejects_source_swap_and_removes_its_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, digest, size, stage_root = _qualified_lower_fixture(tmp_path)
    destination = stage_root / f"{digest.removeprefix('sha256:')}.raw"
    original_lstat = Path.lstat
    source_stats = 0

    def swap_source(path: Path):
        nonlocal source_stats
        if path == source:
            source_stats += 1
            if source_stats == 3:
                source.unlink()
                source.write_bytes(b"replacement")
                source.chmod(0o400)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", swap_source)
    with pytest.raises(ValueError, match="source changed"):
        _stage_qualified_lower(source, digest, size, stage_root)
    assert source_stats == 3
    assert not destination.exists()


def test_qualified_lower_stage_never_removes_replaced_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, digest, size, stage_root = _qualified_lower_fixture(tmp_path)
    destination = stage_root / f"{digest.removeprefix('sha256:')}.raw"
    original_lstat = Path.lstat
    replaced = False

    def swap_destination(path: Path):
        nonlocal replaced
        if path == destination and not replaced:
            replaced = True
            destination.unlink()
            destination.write_bytes(b"replacement")
            destination.chmod(0o400)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", swap_destination)
    with pytest.raises(ValueError, match="identity changed"):
        _stage_qualified_lower(source, digest, size, stage_root)
    assert replaced is True
    assert destination.read_bytes() == b"replacement"


def test_qualified_lower_stage_rejects_changed_existing_copy(tmp_path: Path) -> None:
    source, digest, size, stage_root = _qualified_lower_fixture(tmp_path)
    staged = _stage_qualified_lower(source, digest, size, stage_root)
    staged.chmod(0o600)
    staged.write_bytes(b"x" * size)
    staged.chmod(0o400)

    with pytest.raises(ValueError, match="staged copy"):
        _stage_qualified_lower(source, digest, size, stage_root)
    assert staged.read_bytes() == b"x" * size


class _FakeACLRunner:
    def __init__(
        self,
        *,
        fail_setfacl_calls: frozenset[int] = frozenset(),
        initial_acl: str | None = None,
    ) -> None:
        self.acls: dict[int, str] = {}
        self.events: list[str] = []
        self.fail_setfacl_calls = fail_setfacl_calls
        self.setfacl_calls = 0
        self.initial_acl = initial_acl

    def __call__(self, arguments, *, input, text, capture_output, check, pass_fds, timeout):
        assert text is capture_output is True
        assert check is False
        assert timeout == 10
        descriptor = int(arguments[-1].rsplit("/", 1)[1])
        assert pass_fds == (descriptor,)
        command = arguments[0]
        self.events.append(command)
        if descriptor not in self.acls:
            mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
            basic_acl = (
                f"user::{'r' if mode & 0o400 else '-'}{'w' if mode & 0o200 else '-'}"
                f"{'x' if mode & 0o100 else '-'}\n"
                f"group::{'r' if mode & 0o040 else '-'}{'w' if mode & 0o020 else '-'}"
                f"{'x' if mode & 0o010 else '-'}\n"
                f"other::{'r' if mode & 0o004 else '-'}{'w' if mode & 0o002 else '-'}"
                f"{'x' if mode & 0o001 else '-'}\n"
            )
            self.acls[descriptor] = self.initial_acl or basic_acl
        if command == "getfacl":
            return subprocess.CompletedProcess(arguments, 0, self.acls[descriptor] + "\n", "")
        self.setfacl_calls += 1
        if self.setfacl_calls in self.fail_setfacl_calls:
            return subprocess.CompletedProcess(arguments, 1, "", "")
        if arguments[1] == "-m":
            permission = arguments[2].split(":", 2)[2]
            original = _parse_acl(self.acls[descriptor])
            assert not original.named_users and original.mask is None
            mask = _acl_mode(original.group) | _acl_mode(permission)
            mask_text = f"{'r' if mask & 4 else '-'}{'w' if mask & 2 else '-'}{'x' if mask & 1 else '-'}"
            applied = _ACLStructure(
                original.user,
                ((int(arguments[2].split(":", 2)[1]), permission),),
                original.group,
                mask_text,
                original.other,
            )
            self.acls[descriptor] = applied.setfile_text()
            os.fchmod(
                descriptor,
                (_acl_mode(original.user) << 6) | (mask << 3) | _acl_mode(original.other),
            )
        else:
            assert arguments[1] == "--set-file=-"
            assert input is not None
            original = _parse_acl(input)
            assert not original.named_users and original.mask is None
            self.acls[descriptor] = input
            os.fchmod(
                descriptor,
                (_acl_mode(original.user) << 6) | (_acl_mode(original.group) << 3) | _acl_mode(original.other),
            )
        return subprocess.CompletedProcess(arguments, 0, "", "")


def test_acl_parser_accepts_gnu_getfacl_trailing_blank_line_and_compares_structure() -> None:
    basic = "user::rw-\ngroup::r--\nother::---\n"
    extended = "user::rw-\nuser:107:r--\ngroup::r--\nmask::r--\nother::---\n\n"

    assert _parse_acl(basic) == _ACLStructure("rw-", (), "r--", None, "---")
    assert _parse_acl(extended) == _ACLStructure("rw-", ((107, "r--"),), "r--", "r--", "---")


@pytest.mark.parametrize(
    "payload",
    [
        "user::rw-\ngroup::---\nother::---",
        "user::rw-\n\ngroup::---\nother::---\n",
        "user::rw-\ngroup::---\nother::---\n\n\n",
        "user::rw-\nuser:01:r--\ngroup::---\nmask::r--\nother::---\n",
        "user::rw-\nuser:7:r--\nuser:7:r--\ngroup::---\nmask::r--\nother::---\n",
        "user::rw-\nuser:7:r--\ngroup::---\nother::---\n",
    ],
)
def test_acl_parser_rejects_noncanonical_structure(payload: str) -> None:
    with pytest.raises(ValueError, match="noncanonical"):
        _parse_acl(payload)


def test_qualification_dac_broker_applies_in_order_and_restores_in_reverse(tmp_path: Path) -> None:
    artifact = tmp_path / "root.raw"
    artifact.write_bytes(b"root")
    artifact.chmod(0o600)
    runner = _FakeACLRunner()
    broker = _QualificationDACBroker(
        tmp_path,
        os.geteuid() + 1,
        ((tmp_path, "directory", "--x"), (artifact, "regular", "rw-")),
        runner=runner,
    )

    broker.apply()
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o710
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o660
    broker.restore()

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert runner.events == ["getfacl", "getfacl", "setfacl", "getfacl", "setfacl", "getfacl"] + [
        "setfacl",
        "getfacl",
        "setfacl",
        "getfacl",
    ]
    broker.close()


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir() or shutil.which("getfacl") is None or shutil.which("setfacl") is None,
    reason="requires Linux /proc/self/fd plus GNU getfacl/setfacl",
)
def test_qualification_dac_broker_real_fd_bound_apply_and_restore(tmp_path: Path) -> None:
    artifact = tmp_path / "root.raw"
    artifact.write_bytes(b"root")
    artifact.chmod(0o600)
    original_root = tmp_path.stat()
    original_artifact = artifact.stat()
    broker = _QualificationDACBroker(
        tmp_path,
        os.geteuid() + 1,
        ((tmp_path, "directory", "--x"), (artifact, "regular", "rw-")),
    )

    broker.apply()
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o710
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o660
    broker.restore()

    restored_root = tmp_path.stat()
    restored_artifact = artifact.stat()
    assert stat.S_IMODE(restored_root.st_mode) == 0o700
    assert stat.S_IMODE(restored_artifact.st_mode) == 0o600
    for original, restored in ((original_root, restored_root), (original_artifact, restored_artifact)):
        assert (restored.st_dev, restored.st_ino, restored.st_uid, restored.st_gid, restored.st_nlink) == (
            original.st_dev,
            original.st_ino,
            original.st_uid,
            original.st_gid,
            original.st_nlink,
        )
    broker.close()


def test_qualification_dac_broker_refuses_replaced_held_path(tmp_path: Path) -> None:
    artifact = tmp_path / "root.raw"
    artifact.write_bytes(b"root")
    artifact.chmod(0o600)
    displaced = tmp_path / "displaced.raw"
    runner = _FakeACLRunner()
    broker = _QualificationDACBroker(
        tmp_path,
        os.geteuid() + 1,
        ((artifact, "regular", "rw-"),),
        runner=runner,
    )
    artifact.rename(displaced)
    artifact.write_bytes(b"replacement")

    with pytest.raises(ValueError, match="identity changed"):
        broker.apply()

    assert artifact.read_bytes() == b"replacement"
    assert stat.S_IMODE(displaced.stat().st_mode) == 0o600
    broker.close()


def test_qualification_dac_broker_rejects_preexisting_extended_acl(tmp_path: Path) -> None:
    artifact = tmp_path / "root.raw"
    artifact.write_bytes(b"root")
    runner = _FakeACLRunner(initial_acl="user::rw-\ngroup::---\nmask::---\nother::---\n")

    with pytest.raises(ValueError, match="pre-existing extended ACL"):
        _QualificationDACBroker(
            tmp_path,
            os.geteuid() + 1,
            ((artifact, "regular", "r--"),),
            runner=runner,
        )


@pytest.mark.parametrize(("failed_calls", "ambiguous"), [(frozenset({2}), False), (frozenset({2, 3}), True)])
def test_qualification_dac_broker_failure_restores_or_retains_exact_state(
    tmp_path: Path,
    failed_calls: frozenset[int],
    ambiguous: bool,
) -> None:
    artifact = tmp_path / "root.raw"
    artifact.write_bytes(b"root")
    artifact.chmod(0o600)
    runner = _FakeACLRunner(fail_setfacl_calls=failed_calls)
    broker = _QualificationDACBroker(
        tmp_path,
        os.geteuid() + 1,
        ((tmp_path, "directory", "--x"), (artifact, "regular", "rw-")),
        runner=runner,
    )

    with pytest.raises(ValueError):
        broker.apply()

    assert broker.ambiguous is ambiguous
    if ambiguous:
        assert broker.applied is True
        assert broker.restored is False
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o710
        with pytest.raises(ValueError, match="cannot release"):
            broker.close()
        broker.restore_after_domain_absent()
    else:
        assert broker.restored is True
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    broker.close()


def test_qualification_acl_specifications_bind_exact_xml_paths_and_permissions(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    paths = {
        "kernel": tmp_path / "k",
        "initrd": tmp_path / "i",
        "root": tmp_path / "root.raw",
        "stage1": run / "stage1.raw",
        "layer": tmp_path / "layer.raw",
        "console": tmp_path / "console.log",
    }
    for path in paths.values():
        path.write_bytes(b"x")
    xml = (
        f"<domain><os><kernel>{paths['kernel']}</kernel><initrd>{paths['initrd']}</initrd></os><devices>"
        f'<disk><source file="{paths["root"]}"><seclabel model="dac" relabel="no"/></source>'
        '<target dev="vda" bus="virtio"/></disk>'
        f'<disk><source file="{paths["stage1"]}"><seclabel model="dac" relabel="no"/></source>'
        '<target dev="vdb" bus="virtio"/></disk>'
        f'<disk><source file="{paths["layer"]}"><seclabel model="dac" relabel="no"/></source>'
        '<target dev="vdc" bus="virtio"/></disk>'
        f'<console type="file"><source path="{paths["console"]}" append="on">'
        '<seclabel model="dac" relabel="no"/></source>'
        '<target type="serial" port="0"/></console>'
        f'<channel><source mode="bind" path="{run / "lifecycle.sock"}"/>'
        '<target type="virtio" name="org.palimpsest.oci.lifecycle.0"/></channel>'
        "</devices></domain>"
    )

    specifications = _qualification_acl_specifications(tmp_path, xml)
    actual = {(path, kind): permission for path, kind, permission in specifications}

    assert actual[(tmp_path, "directory")] == "--x"
    assert actual[(run, "directory")] == "-wx"
    assert actual[(paths["root"], "regular")] == "rw-"
    assert actual[(paths["console"], "regular")] == "rw-"
    for name in ("kernel", "initrd", "stage1", "layer"):
        assert actual[(paths[name], "regular")] == "r--"


@pytest.mark.parametrize(
    "tamper",
    [
        "missing-console",
        "duplicate-console",
        "wrong-type",
        "missing-source",
        "duplicate-source",
        "wrong-source-attrs",
        "source-text",
        "source-child",
        "missing-seclabel",
        "duplicate-seclabel",
        "seclabel-model",
        "seclabel-relabel",
        "seclabel-extra-attribute",
        "seclabel-text",
        "seclabel-child",
        "seclabel-namespace",
        "wrong-target",
        "extra-child",
    ],
)
def test_qualification_acl_specifications_reject_malformed_console(
    tmp_path: Path,
    tamper: str,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    paths = [tmp_path / name for name in ("kernel", "initrd", "root.raw", "console.log")]
    for path in paths:
        path.write_bytes(b"")
    kernel, initrd, root_disk, console_path = paths
    xml = ET.fromstring(
        f"<domain><os><kernel>{kernel}</kernel><initrd>{initrd}</initrd></os><devices>"
        f'<disk><source file="{root_disk}"><seclabel model="dac" relabel="no"/></source>'
        '<target dev="vda" bus="virtio"/></disk>'
        f'<console type="file"><source path="{console_path}" append="on">'
        '<seclabel model="dac" relabel="no"/></source>'
        '<target type="serial" port="0"/></console>'
        f'<channel><source mode="bind" path="{run / "lifecycle.sock"}"/>'
        '<target type="virtio" name="org.palimpsest.oci.lifecycle.0"/></channel>'
        "</devices></domain>"
    )
    devices = xml.find("./devices")
    console = xml.find("./devices/console")
    source = xml.find("./devices/console/source")
    label = xml.find("./devices/console/source/seclabel")
    target = xml.find("./devices/console/target")
    assert (
        devices is not None and console is not None and source is not None and label is not None and target is not None
    )
    if tamper == "missing-console":
        devices.remove(console)
    elif tamper == "duplicate-console":
        devices.append(ET.fromstring(ET.tostring(console, encoding="unicode")))
    elif tamper == "wrong-type":
        console.set("type", "pty")
    elif tamper == "missing-source":
        console.remove(source)
    elif tamper == "duplicate-source":
        console.append(ET.fromstring(ET.tostring(source, encoding="unicode")))
    elif tamper == "wrong-source-attrs":
        source.set("append", "off")
    elif tamper == "source-text":
        source.text = "forbidden"
    elif tamper == "source-child":
        ET.SubElement(source, "attacker")
    elif tamper == "missing-seclabel":
        source.remove(label)
    elif tamper == "duplicate-seclabel":
        source.append(ET.fromstring(ET.tostring(label, encoding="unicode")))
    elif tamper == "seclabel-model":
        label.set("model", "selinux")
    elif tamper == "seclabel-relabel":
        label.set("relabel", "yes")
    elif tamper == "seclabel-extra-attribute":
        label.set("label", "+0:+0")
    elif tamper == "seclabel-text":
        label.text = "forbidden"
    elif tamper == "seclabel-child":
        ET.SubElement(label, "attacker")
    elif tamper == "seclabel-namespace":
        label.tag = "{https://attacker.invalid/domain/v1}seclabel"
    elif tamper == "wrong-target":
        target.set("port", "1")
    else:
        ET.SubElement(console, "attacker")

    with pytest.raises(ValueError, match="console binding"):
        _qualification_acl_specifications(tmp_path, ET.tostring(xml, encoding="unicode"))


def test_qualification_acl_specifications_require_exact_dac_no_relabel(tmp_path: Path) -> None:
    artifact = tmp_path / "root.raw"
    artifact.write_bytes(b"x")
    run = tmp_path / "run"
    run.mkdir()
    xml = (
        f"<domain><os><kernel>{artifact}</kernel><initrd>{artifact}</initrd></os><devices>"
        f'<disk><source file="{artifact}"/><target dev="vda" bus="virtio"/></disk>'
        f'<channel><source mode="bind" path="{run / "lifecycle.sock"}"/>'
        '<target type="virtio" name="org.palimpsest.oci.lifecycle.0"/></channel>'
        "</devices></domain>"
    )

    with pytest.raises(ValueError, match="disk binding"):
        _qualification_acl_specifications(tmp_path, xml)


class _FakeActivationBroker:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.ambiguous = False
        self.restored = False

    def apply(self) -> None:
        self.events.append("apply")

    def restore_after_domain_absent(self) -> None:
        self.events.append("restore-absent")
        self.ambiguous = False
        self.restored = True


class _FakeActivationDomain:
    def __init__(self, *, active_on_failure: int | None = None) -> None:
        self.active_on_failure = active_on_failure
        self.events: list[str] = []

    def UUIDString(self) -> str:
        return "bdffb19a-dd39-4b15-986f-77d1298e0950"

    def create(self) -> int:
        self.events.append("create")
        if self.active_on_failure is not None:
            raise RuntimeError("create")
        return 0

    def isActive(self) -> int:
        self.events.append("active")
        assert self.active_on_failure is not None
        return self.active_on_failure


def test_activation_domain_proxy_applies_once_and_caller_restores_only_after_absence() -> None:
    broker = _FakeActivationBroker()
    domain = _FakeActivationDomain()
    proxy = _ActivationDomainProxy(domain, domain.UUIDString(), broker)  # type: ignore[arg-type]

    assert proxy.create() == 0
    with pytest.raises(ValueError, match="reused"):
        proxy.create()
    broker.restore_after_domain_absent()

    assert broker.events == ["apply", "restore-absent"]
    assert domain.events == ["create"]


def test_activation_connection_proxy_wraps_only_name_lookup_create_surface() -> None:
    broker = _FakeActivationBroker()
    by_name = _FakeActivationDomain()
    by_uuid = _FakeActivationDomain()

    class Connection:
        def lookupByName(self, _name: str):
            return by_name

        def lookupByUUIDString(self, _domain_uuid: str):
            return by_uuid

    connection = _ActivationConnectionProxy(Connection(), by_name.UUIDString(), broker)  # type: ignore[arg-type]

    assert isinstance(connection.lookupByName("demo"), _ActivationDomainProxy)
    assert connection.lookupByUUIDString(by_name.UUIDString()) is by_uuid


@pytest.mark.parametrize(("active", "ambiguous"), [(0, False), (1, True)])
def test_activation_domain_proxy_never_restores_acl_on_create_failure(active: int, ambiguous: bool) -> None:
    broker = _FakeActivationBroker()
    domain = _FakeActivationDomain(active_on_failure=active)
    proxy = _ActivationDomainProxy(domain, domain.UUIDString(), broker)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="create"):
        proxy.create()

    assert broker.events == ["apply"]
    assert broker.ambiguous is ambiguous


def test_remove_inactive_domain_proves_absence_before_acl_restore() -> None:
    events: list[str] = []

    class OrderedBroker(_FakeActivationBroker):
        def restore_after_domain_absent(self) -> None:
            events.append("restore-absent")
            super().restore_after_domain_absent()

    broker = OrderedBroker()

    def remove_exact_inactive() -> None:
        events.extend(("inspect-inactive", "undefine"))

    def prove_absent() -> bool:
        events.append("prove-absent")
        return True

    _remove_inactive_domain_then_restore(  # type: ignore[arg-type]
        broker,
        remove_exact_inactive,
        prove_absent,
    )

    assert events == ["inspect-inactive", "undefine", "prove-absent", "restore-absent"]


def test_remove_inactive_domain_retains_acl_when_domain_reactivated() -> None:
    broker = _FakeActivationBroker()

    def reject_reactivated() -> None:
        raise ValueError("domain is active")

    with pytest.raises(ValueError, match="domain is active"):
        _remove_inactive_domain_then_restore(  # type: ignore[arg-type]
            broker,
            reject_reactivated,
            lambda: pytest.fail("absence proof must not run"),
        )

    assert broker.events == []
    assert broker.ambiguous is True


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires Linux unlink evidence")
def test_empty_held_qualification_directory_removes_only_held_tree_entries(tmp_path: Path) -> None:
    nested = tmp_path / "state"
    nested.mkdir()
    (nested / "ledger.json").write_text("{}", encoding="utf-8")
    (tmp_path / "root.raw").write_bytes(b"root")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    checks = 0

    def verify_root_binding() -> None:
        nonlocal checks
        checks += 1
        assert os.fstat(descriptor).st_ino == tmp_path.stat().st_ino

    try:
        _empty_held_qualification_directory(descriptor, verify_root_binding)
    finally:
        os.close(descriptor)

    assert tuple(tmp_path.iterdir()) == ()
    assert checks >= 4


def test_empty_held_directory_preserves_child_quarantine_on_rename_boundary_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = tmp_path / "root.raw"
    public.write_bytes(b"owned")
    displaced = tmp_path / "owned-displaced"
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    original_rename = os.rename
    injected = False

    def replace_before_child_quarantine(source, destination, *args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            original_rename("root.raw", displaced.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            public.write_bytes(b"replacement")
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", replace_before_child_quarantine)
    try:
        with pytest.raises(ValueError, match="child identity changed while quarantined"):
            _empty_held_qualification_directory(descriptor, lambda: None)

        quarantines = tuple(tmp_path.glob(".p-*.cleanup"))
        assert not public.exists()
        assert displaced.read_bytes() == b"owned"
        assert len(quarantines) == 1
        assert quarantines[0].read_bytes() == b"replacement"
    finally:
        os.close(descriptor)
        monkeypatch.undo()
        for entry in tuple(tmp_path.iterdir()):
            entry.unlink()


def test_empty_held_directory_preserves_late_child_quarantine_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = tmp_path / "root.raw"
    public.write_bytes(b"owned")
    displaced = tmp_path / "owned-displaced"
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    original_stat = os.stat
    original_rename = os.rename
    successful_quarantine_stats = 0

    def replace_before_child_removal(path, *args, **kwargs):
        nonlocal successful_quarantine_stats
        result = original_stat(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".p-") and path.endswith(".cleanup"):
            successful_quarantine_stats += 1
            if successful_quarantine_stats == 2:
                original_rename(path, displaced.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                (tmp_path / path).write_bytes(b"replacement")
                return original_stat(path, *args, **kwargs)
        return result

    monkeypatch.setattr(os, "stat", replace_before_child_removal)
    try:
        with pytest.raises(ValueError, match="child identity changed while quarantined"):
            _empty_held_qualification_directory(descriptor, lambda: None)

        quarantines = tuple(tmp_path.glob(".p-*.cleanup"))
        assert not public.exists()
        assert displaced.read_bytes() == b"owned"
        assert len(quarantines) == 1
        assert quarantines[0].read_bytes() == b"replacement"
    finally:
        os.close(descriptor)
        monkeypatch.undo()
        for entry in tuple(tmp_path.iterdir()):
            entry.unlink()


@pytest.mark.skipif(stat.S_ISLNK(Path("/tmp").lstat().st_mode), reason="requires a non-symlink /tmp")
def test_remove_qualification_root_removes_only_expected_inode() -> None:
    root = Path(tempfile.mkdtemp(prefix="p-", dir="/tmp"))
    expected = root.lstat()
    nested = root / "state"
    nested.mkdir()
    (nested / "ledger.json").write_text("{}", encoding="utf-8")

    _remove_qualification_root(root, expected)

    assert not root.exists()


@pytest.mark.skipif(stat.S_ISLNK(Path("/tmp").lstat().st_mode), reason="requires a non-symlink /tmp")
def test_remove_qualification_root_refuses_same_uid_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="p-", dir="/tmp"))
    displaced = root.with_name(f"{root.name}-original")
    expected = root.lstat()
    original_rename = os.rename
    injected = False

    def replace_before_quarantine(source, destination, *args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            original_rename(root, displaced)
            root.mkdir(mode=0o700)
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", replace_before_quarantine)
    try:
        with pytest.raises(ValueError, match="identity changed while quarantined"):
            _remove_qualification_root(root, expected)

        quarantines = tuple(root.parent.glob(f".{root.name}.*.cleanup"))
        assert not root.exists()
        assert displaced.is_dir()
        assert len(quarantines) == 1
        assert quarantines[0].is_dir()
        assert (quarantines[0].stat().st_dev, quarantines[0].stat().st_ino) != (expected.st_dev, expected.st_ino)
        assert (displaced.stat().st_dev, displaced.stat().st_ino) == (expected.st_dev, expected.st_ino)
    finally:
        for quarantine in root.parent.glob(f".{root.name}.*.cleanup"):
            shutil.rmtree(quarantine)
        shutil.rmtree(displaced)


@pytest.mark.skipif(stat.S_ISLNK(Path("/tmp").lstat().st_mode), reason="requires a non-symlink /tmp")
def test_remove_qualification_root_refuses_late_quarantine_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="p-", dir="/tmp"))
    (root / "owned").write_bytes(b"owned")
    displaced = root.with_name(f"{root.name}-original")
    expected = root.lstat()
    original_listdir = os.listdir
    original_rename = os.rename
    injected = False

    def replace_after_quarantine(descriptor):
        nonlocal injected
        if not injected:
            injected = True
            (quarantine,) = tuple(root.parent.glob(f".{root.name}.*.cleanup"))
            original_rename(quarantine, displaced)
            quarantine.mkdir(mode=0o700)
            (quarantine / "do-not-delete").write_bytes(b"replacement")
        return original_listdir(descriptor)

    monkeypatch.setattr(os, "listdir", replace_after_quarantine)
    try:
        with pytest.raises(ValueError, match="identity changed while quarantined"):
            _remove_qualification_root(root, expected)

        (quarantine,) = tuple(root.parent.glob(f".{root.name}.*.cleanup"))
        assert (quarantine / "do-not-delete").read_bytes() == b"replacement"
        assert displaced.is_dir()
        assert (displaced / "owned").read_bytes() == b"owned"
        assert (displaced.stat().st_dev, displaced.stat().st_ino) == (expected.st_dev, expected.st_ino)
    finally:
        for quarantine in root.parent.glob(f".{root.name}.*.cleanup"):
            shutil.rmtree(quarantine)
        shutil.rmtree(displaced)


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _require_live_host(tmp_path: Path):
    if os.environ.get(_ENABLE_ENV) != "1":
        pytest.skip(f"set {_ENABLE_ENV}=1 on the qualified native Linux/KVM libvirt runner")
    kernel_value = os.environ.get(KERNEL_ENV)
    config_value = os.environ.get(KERNEL_CONFIG_ENV)
    if not kernel_value or not config_value:
        pytest.fail(f"{KERNEL_ENV} and {KERNEL_CONFIG_ENV} must explicitly select qualified artifacts")
    for executable in ("qemu-img", "mkfs.ext4", "debugfs", "getfacl", "setfacl"):
        if shutil.which(executable) is None:
            pytest.fail(f"required live OCI-root tool is unavailable: {executable}")
    if not Path("/proc/self/fd").is_dir():
        pytest.fail("qualified OCI-root DAC broker requires Linux /proc/self/fd")
    assert verify_kvm_api() == 12
    verify_kernel_config(Path(config_value).resolve())
    built = build_bootstrap_initramfs()
    initramfs_path = tmp_path / "palimpsest-oci-root.initramfs"
    initramfs_path.write_bytes(built.payload)
    initramfs_path.chmod(0o400)
    boot = verify_first_party_bootstrap_initramfs(initramfs_path.resolve(), built.manifest)
    kernel_path = _copy_qualified_kernel(Path(kernel_value), tmp_path.resolve(strict=True))
    verified = verify_host_boot_artifacts(
        kernel_path,
        boot.path,
        expected_initramfs_digest=boot.digest,
    )
    profile = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64")
    if profile.uri != "qemu:///system":
        pytest.fail("qualified OCI-root live test requires the qemu:///system profile")
    return verified, profile


def _proof_materialization(store: OCIStore) -> OCIImageMaterializationReceipt:
    # This qualification starts at the derived-store boundary.  The loader
    # below independently verifies the checked-in real mksquashfs outputs and
    # their source/build manifest; OCI registry intake is covered elsewhere.
    filesystems = load_proof_filesystems()
    source_binding = _digest(b"palimpsest-live-libvirt-proof-fixtures-v1")
    source_image = _digest(b"".join(filesystems.lowers))
    toolchain = SquashFSToolchainIdentity(
        "4.7.5",
        _digest(b"source-controlled-mksquashfs-fixture"),
        (_digest(b"source-controlled-zstd-fixture"),),
    )
    capability = VerifiedSquashFSToolchain(toolchain, Path("/source-controlled/mksquashfs"), ())
    occurrences: list[DerivedLayerOccurrence] = []
    results: list[MaterializationResult] = []
    for ordinal, image in enumerate(filesystems.lowers):
        occurrence = DerivedLayerOccurrence(
            source_snapshot_binding_digest=source_binding,
            source_image_digest=source_image,
            ordinal=ordinal,
            media_type=OCI_LAYER_MEDIA_TYPE,
            compressed_digest=_digest(image),
            compressed_size=len(image),
            diff_id=_digest(b"proof-diff-id\0" + ordinal.to_bytes(4, "big") + image),
        )
        key = DerivedSquashFSKey.for_occurrence(
            occurrence,
            intake_policy_id=LAYER_INTAKE_POLICY_ID,
            intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
            pack_policy_id=SQUASHFS_PACK_POLICY_ID,
            pack_policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
            toolchain=capability,
        )
        intake = LayerIntakeReceipt(
            policy_id=LAYER_INTAKE_POLICY_ID,
            policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
            ordinal=ordinal,
            media_type=OCI_LAYER_MEDIA_TYPE,
            compressed_digest=occurrence.compressed_digest,
            compressed_size=occurrence.compressed_size,
            diff_id=occurrence.diff_id,
            uncompressed_size=len(image),
            physical_headers=1,
            members=1,
            regular_bytes=len(image),
            xattr_bytes=0,
        )
        packed_receipt = PackedSquashFSReceipt(
            policy_id=SQUASHFS_PACK_POLICY_ID,
            policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
            source_ordinal=ordinal,
            source_diff_id=occurrence.diff_id,
            normalized_tar_digest=_digest(b"proof-normalized-tar\0" + ordinal.to_bytes(4, "big")),
            normalized_tar_size=10240,
            entries=1,
            packer_version=toolchain.version,
            packer_sha256=toolchain.executable_digest.removeprefix("sha256:"),
            image_digest=_digest(image),
            image_size=len(image),
            structural_verifier=SQUASHFS_STRUCTURAL_VERIFIER_ID,
            toolchain_fingerprint=toolchain.fingerprint,
            toolchain_dependency_digests=toolchain.dependency_digests,
        )

        @contextmanager
        def producer(
            payload: bytes = image,
            bound_intake: LayerIntakeReceipt = intake,
            bound_receipt: PackedSquashFSReceipt = packed_receipt,
        ):
            packed = LeasedSquashFS(io.BytesIO(payload), bound_receipt)
            try:
                yield bound_intake, packed
            finally:
                failure = packed._close()
                if failure is not None:
                    raise failure

        occurrences.append(occurrence)
        results.append(store.materialize_observed(occurrence, key, producer))

    process = OCIProcessSpec(
        (".__palimpsest_workload_proof_v1", "deliberately-not-the-proof-invocation"),
        (("PATH", "/proof/missing:/"),),
        "/proof/workdir",
        OCIUserSpec("palimpsest", None),
        15,
    )
    manifest_digest = _digest(b"palimpsest-live-libvirt-manifest-v1")
    config_digest = _digest(json.dumps(process.to_dict(), sort_keys=True).encode())
    return OCIImageMaterializationReceipt(
        source_snapshot_binding_digest=source_binding,
        source_image_digest=source_image,
        root_descriptor=Descriptor(OCI_IMAGE_MANIFEST_MEDIA_TYPE, manifest_digest, 1),
        manifest_digest=manifest_digest,
        config_descriptor=Descriptor(OCI_IMAGE_CONFIG_MEDIA_TYPE, config_digest, 1),
        platform_os="linux",
        platform_architecture="amd64",
        layer_descriptors=tuple(
            Descriptor(OCI_LAYER_MEDIA_TYPE, occurrence.compressed_digest, occurrence.compressed_size)
            for occurrence in occurrences
        ),
        layer_diff_ids=tuple(occurrence.diff_id for occurrence in occurrences),
        process=process,
        results=tuple(results),
    )


def _owner_marker(domain) -> dict[str, str]:
    root = ET.fromstring(domain.XMLDesc())
    marker = root.find(f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run")
    assert marker is not None
    return dict(marker.attrib)


def _lookup_domain(conn, name: str):
    try:
        return conn.lookupByName(name)
    except kvm._libvirt().libvirtError as exc:
        assert exc.get_error_code() == kvm._libvirt().VIR_ERR_NO_DOMAIN
        return None


def _lookup_domain_uuid(conn, domain_uuid: str):
    try:
        return conn.lookupByUUIDString(domain_uuid)
    except kvm._libvirt().libvirtError as exc:
        assert exc.get_error_code() == kvm._libvirt().VIR_ERR_NO_DOMAIN
        return None


def _inspect_exact_owned_domain(
    conn,
    name: str,
    domain_uuid: str,
    run_id: str,
    plan_digest: str,
    expected_inactive_projection_digest: str,
):
    by_name = _lookup_domain(conn, name)
    by_uuid = _lookup_domain_uuid(conn, domain_uuid)
    assert by_name is not None and by_uuid is not None
    assert by_name.UUIDString() == by_uuid.UUIDString() == domain_uuid
    expected_owner = {
        "contract": plan_digest,
        "id": run_id,
        "schema": "1",
        "version": kvm.DOMAIN_MARKER_VERSION,
    }
    assert _owner_marker(by_name) == _owner_marker(by_uuid) == expected_owner
    inactive_flag = getattr(kvm._libvirt(), "VIR_DOMAIN_XML_INACTIVE", None)
    assert type(inactive_flag) is int
    observed: list[tuple[int, int]] = []
    for candidate in (by_name, by_uuid):
        inactive_xml = candidate.XMLDesc(inactive_flag)
        assert (
            oci_root_runtime._projection_digest(oci_root_runtime._domain_projection(inactive_xml))
            == expected_inactive_projection_digest
        )
        xml_root = ET.fromstring(inactive_xml)
        xml_uuids = xml_root.findall("./uuid")
        assert not xml_uuids or xml_uuids[0].text == domain_uuid
        observed.append((candidate.isActive(), candidate.ID()))
    assert observed[0] == observed[1]
    active, domain_id = observed[0]
    assert active in {0, 1}
    assert type(domain_id) is int
    return by_name, active, domain_id


def _remove_exact_owned_domain(
    conn,
    name: str,
    domain_uuid: str,
    run_id: str,
    plan_digest: str,
    expected_domain_id: int,
    expected_inactive_projection_digest: str,
) -> None:
    domain = _lookup_domain(conn, name)
    if domain is None:
        assert _lookup_domain_uuid(conn, domain_uuid) is None
        return
    domain, active, domain_id = _inspect_exact_owned_domain(
        conn,
        name,
        domain_uuid,
        run_id,
        plan_digest,
        expected_inactive_projection_digest,
    )
    if active == 1:
        assert expected_domain_id > 0 and domain_id == expected_domain_id
        domain, active, domain_id = _inspect_exact_owned_domain(
            conn,
            name,
            domain_uuid,
            run_id,
            plan_digest,
            expected_inactive_projection_digest,
        )
        assert active == 1 and domain_id == expected_domain_id
        domain.destroy()
    domain, active, domain_id = _inspect_exact_owned_domain(
        conn,
        name,
        domain_uuid,
        run_id,
        plan_digest,
        expected_inactive_projection_digest,
    )
    assert active == 0 and domain_id == -1
    domain.undefine()
    assert _lookup_domain(conn, name) is None
    assert _lookup_domain_uuid(conn, domain_uuid) is None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _MissingCleanupDomainError(Exception):
    def get_error_code(self) -> int:
        return 404


class _CleanupTestDomain:
    def __init__(self, conn: _CleanupTestConnection, domain_uuid: str, xml: str, domain_id: int) -> None:
        self.conn = conn
        self.domain_uuid = domain_uuid
        self.xml = xml
        self.active = 1
        self.domain_id = domain_id
        self.destroy_calls = 0
        self.undefine_calls = 0

    def UUIDString(self) -> str:
        return self.domain_uuid

    def XMLDesc(self, _flags: int | None = None) -> str:
        return self.xml

    def isActive(self) -> int:
        return self.active

    def ID(self) -> int:
        return self.domain_id if self.active == 1 else -1

    def destroy(self) -> None:
        self.destroy_calls += 1
        self.active = 0

    def undefine(self) -> None:
        self.undefine_calls += 1
        self.conn.domain = None


class _CleanupTestConnection:
    def __init__(self, name: str, domain_uuid: str, xml: str, domain_id: int) -> None:
        self.name = name
        self.domain: _CleanupTestDomain | None = None
        self.domain = _CleanupTestDomain(self, domain_uuid, xml, domain_id)

    def lookupByName(self, name: str) -> _CleanupTestDomain:
        if self.domain is None or name != self.name:
            raise _MissingCleanupDomainError
        return self.domain

    def lookupByUUIDString(self, domain_uuid: str) -> _CleanupTestDomain:
        if self.domain is None or domain_uuid != self.domain.domain_uuid:
            raise _MissingCleanupDomainError
        return self.domain


def _cleanup_test_xml(domain_uuid: str, run_id: str, plan_digest: str, *, drift: bool = False) -> str:
    suffix = "<drift />" if drift else ""
    return (
        f'<domain><uuid>{domain_uuid}</uuid><metadata xmlns:pali="{kvm.DOMAIN_MARKER_NAMESPACE}">'
        f'<pali:run contract="{plan_digest}" id="{run_id}" schema="1" '
        f'version="{kvm.DOMAIN_MARKER_VERSION}" /></metadata>{suffix}</domain>'
    )


@pytest.mark.parametrize("drift", ["domain_id", "inactive_xml"])
def test_exact_cleanup_refuses_active_domain_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    name = "cleanup-proof"
    domain_uuid = "2cc94a91-eabc-44f4-b2bd-f3f177618fb9"
    run_id = "524e3513-98f1-4406-a152-8d726b396a91"
    plan_digest = "sha256:" + "c" * 64
    expected_xml = _cleanup_test_xml(domain_uuid, run_id, plan_digest)
    actual_xml = _cleanup_test_xml(domain_uuid, run_id, plan_digest, drift=drift == "inactive_xml")
    actual_domain_id = 42 if drift == "domain_id" else 41
    conn = _CleanupTestConnection(name, domain_uuid, actual_xml, actual_domain_id)
    domain = conn.domain
    assert domain is not None
    fake_libvirt = type(
        "FakeLibvirt",
        (),
        {
            "VIR_DOMAIN_XML_INACTIVE": 1,
            "VIR_ERR_NO_DOMAIN": 404,
            "libvirtError": _MissingCleanupDomainError,
        },
    )
    monkeypatch.setattr(kvm, "_libvirt", lambda: fake_libvirt)
    monkeypatch.setattr(oci_root_runtime, "_domain_projection", lambda xml: {"xml": xml})

    with pytest.raises(AssertionError):
        _remove_exact_owned_domain(
            conn,
            name,
            domain_uuid,
            run_id,
            plan_digest,
            41,
            oci_root_runtime._projection_digest({"xml": expected_xml}),
        )

    assert domain.destroy_calls == 0
    assert domain.undefine_calls == 0


def test_exact_cleanup_expected_inactive_refuses_reactivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "cleanup-proof"
    domain_uuid = "2cc94a91-eabc-44f4-b2bd-f3f177618fb9"
    run_id = "524e3513-98f1-4406-a152-8d726b396a91"
    plan_digest = "sha256:" + "c" * 64
    xml = _cleanup_test_xml(domain_uuid, run_id, plan_digest)
    conn = _CleanupTestConnection(name, domain_uuid, xml, 41)
    domain = conn.domain
    assert domain is not None
    fake_libvirt = type(
        "FakeLibvirt",
        (),
        {
            "VIR_DOMAIN_XML_INACTIVE": 1,
            "VIR_ERR_NO_DOMAIN": 404,
            "libvirtError": _MissingCleanupDomainError,
        },
    )
    monkeypatch.setattr(kvm, "_libvirt", lambda: fake_libvirt)
    monkeypatch.setattr(oci_root_runtime, "_domain_projection", lambda value: {"xml": value})

    with pytest.raises(AssertionError):
        _remove_exact_owned_domain(
            conn,
            name,
            domain_uuid,
            run_id,
            plan_digest,
            -1,
            oci_root_runtime._projection_digest({"xml": xml}),
        )

    assert domain.destroy_calls == 0
    assert domain.undefine_calls == 0


def test_exact_cleanup_revalidates_active_domain_before_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "cleanup-proof"
    domain_uuid = "2cc94a91-eabc-44f4-b2bd-f3f177618fb9"
    run_id = "524e3513-98f1-4406-a152-8d726b396a91"
    plan_digest = "sha256:" + "c" * 64
    xml = _cleanup_test_xml(domain_uuid, run_id, plan_digest)
    conn = _CleanupTestConnection(name, domain_uuid, xml, 41)
    domain = conn.domain
    assert domain is not None
    fake_libvirt = type(
        "FakeLibvirt",
        (),
        {
            "VIR_DOMAIN_XML_INACTIVE": 1,
            "VIR_ERR_NO_DOMAIN": 404,
            "libvirtError": _MissingCleanupDomainError,
        },
    )
    monkeypatch.setattr(kvm, "_libvirt", lambda: fake_libvirt)
    monkeypatch.setattr(oci_root_runtime, "_domain_projection", lambda value: {"xml": value})

    _remove_exact_owned_domain(
        conn,
        name,
        domain_uuid,
        run_id,
        plan_digest,
        41,
        oci_root_runtime._projection_digest({"xml": xml}),
    )

    assert domain.destroy_calls == 1
    assert domain.undefine_calls == 1


def test_live_oci_root(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get(_ENABLE_ENV) != "1":
        pytest.skip(f"set {_ENABLE_ENV}=1 on the qualified native Linux/KVM libvirt runner")
    qualification_root, qualification_root_identity = _create_qualification_root()
    try:
        boot, profile = _require_live_host(qualification_root)
    except BaseException:
        try:
            (qualification_root / "k").lstat()
        except FileNotFoundError:
            _remove_qualification_root(qualification_root, qualification_root_identity)
        except OSError:
            pass
        # A present or ambiguous kernel-copy entry may be the deliberately
        # preserved unknown inode from an initial fstat failure.  Retain the
        # entire owner-only qualification root rather than deleting through it.
        raise
    conn: Any | None = None
    try:
        roots = init_resolved_roots(StatePaths(qualification_root / "c", qualification_root / "s"))
        store = OCIStore(roots, repair_min_age_seconds=0)
        materialization = _proof_materialization(store)
        console_path = _create_qualification_console(qualification_root)
        original_build_oci_root_domain_xml = oci_root_kvm_module.build_oci_root_domain_xml

        def build_qualification_domain_xml(spec: kvm.OCIRootDomainSpec, profile_value: platforms.DomainProfile) -> str:
            return original_build_oci_root_domain_xml(replace(spec, console_log=console_path), profile_value)

        monkeypatch.setattr(
            oci_root_kvm_module,
            "build_oci_root_domain_xml",
            build_qualification_domain_xml,
        )
        lower_stage_root = qualification_root / "l"
        lower_stage_root.mkdir(mode=0o700)
        original_verified_lower_path = oci_root_kvm_module._verified_lower_path

        def stage_verified_lower(roots_value: StatePaths, digest: str, size: int) -> Path:
            source = original_verified_lower_path(roots_value, digest, size)
            return _stage_qualified_lower(source, digest, size, lower_stage_root)

        monkeypatch.setattr(oci_root_kvm_module, "_verified_lower_path", stage_verified_lower)
        name = f"p-{uuid.uuid4().hex[:6]}"
        lifecycle_path = roots.runs / name / "lifecycle.sock"
        assert len(os.fsencode(lifecycle_path)) <= kvm.LIBVIRT_UNIX_SOCKET_PATH_MAX_BYTES - 10
        conn = kvm.connect(profile.uri)
        qemu_uid, qemu_gid = _parse_qemu_dac_baselabel(conn.getCapabilities())
        assert 0 <= qemu_uid <= _MAX_DAC_ID
        assert 0 <= qemu_gid <= _MAX_DAC_ID
    except BaseException:
        if conn is not None:
            conn.close()
        _remove_qualification_root(qualification_root, qualification_root_identity)
        raise
    prepared: PreparedOCIRootRun | None = None
    owned_uuid: str | None = None
    owned_domain_id: int | None = None
    plan_digest: str | None = None
    expected_inactive_projection_digest: str | None = None
    broker: _QualificationDACBroker | None = None
    retain_qualification_state = False
    try:
        with reserve_new_run(
            roots,
            name,
            DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
        ) as reservation:
            prepared = prepare_oci_root_run(
                reservation,
                materialization,
                store,
                root_volume_size_bytes=_ROOT_SIZE_BYTES,
            )
        root_path = prepared.root_volume.path
        before_digest = _sha256_file(root_path)
        resolved = build_oci_root_domain_plan(
            roots,
            prepared,
            store,
            boot,
            profile,
            memory_mib=512,
            vcpus=1,
            network=None,
        )
        plan = commit_oci_root_domain_plan(roots, resolved, store)
        plan_digest = plan.digest
        assert _lookup_domain(conn, name) is None
        collision = conn.defineXML(resolved.xml)
        assert collision is not None and collision.isActive() == 0
        owned_uuid = collision.UUIDString()
        owned_domain_id = collision.ID()
        assert owned_domain_id == -1
        assert _owner_marker(collision)["id"] == plan.run_id
        inactive_flag = getattr(kvm._libvirt(), "VIR_DOMAIN_XML_INACTIVE", None)
        assert type(inactive_flag) is int
        expected_inactive_projection_digest = oci_root_runtime._validated_post_define_projection(
            conn,
            resolved,
            collision.XMLDesc(inactive_flag),
        )
        with pytest.raises(StateError, match="domain name is already reserved"):
            define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn)
        assert read_run_ledger_snapshot(roots, name).state["status"] == "creating"
        _remove_exact_owned_domain(
            conn,
            name,
            owned_uuid,
            plan.run_id,
            plan.digest,
            owned_domain_id,
            expected_inactive_projection_digest,
        )
        owned_uuid = None
        owned_domain_id = None

        defined = define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn)
        owned_uuid = defined.domain_uuid
        owned_domain_id = -1
        definition = read_run_ledger_snapshot(roots, name).state["oci_root_definition"]
        assert definition["schema"] == "palimpsest.oci-root-definition.v2"
        expected_inactive_projection_digest = definition["projection_digest"]
        broker = _QualificationDACBroker(
            qualification_root,
            qemu_uid,
            _qualification_acl_specifications(qualification_root, resolved.xml),
        )
        direct_connect_attempts: list[object] = []
        original_connect = socket.socket.connect

        def reject_direct_lifecycle_connect(instance: socket.socket, address: object) -> object:
            if address == os.fspath(lifecycle_path) or address == lifecycle_path:
                direct_connect_attempts.append(address)
                raise AssertionError("production handoff bypassed libvirt openChannel")
            return original_connect(instance, address)  # type: ignore[arg-type]

        monkeypatch.setattr(socket.socket, "connect", reject_direct_lifecycle_connect)
        activation_conn = _ActivationConnectionProxy(conn, defined.domain_uuid, broker)
        try:
            completed = launch_defined_oci_root_domain(
                roots,
                name,
                store,
                boot,
                profile,
                conn=activation_conn,
                timeout_seconds=45,
                terminal_timeout_seconds=45,
            )
        except BaseException as exc:
            _annotate_qualification_console_failure(exc, console_path)
            raise
        assert completed.domain_id > 0
        owned_domain_id = -1
        assert expected_inactive_projection_digest is not None

        assert direct_connect_attempts == []
        assert completed.domain_uuid == defined.domain_uuid
        assert completed.terminal.returncode == 101
        assert completed.terminal.exit_code == 101
        assert completed.terminal.signal_number is None
        lifecycle = completed.lifecycle.to_dict()
        assert lifecycle["schema"] == "palimpsest.oci-root-handoff.v1"
        assert lifecycle["phase"] == "terminal"
        assert [entry["kind"] for entry in lifecycle["transcript"]] == [
            "HELLO",
            "BOOTSTRAP",
            "KEY_ACK",
            "READY",
            "TERMINAL",
        ]
        assert "boot_key" not in repr(lifecycle)
        assert "tag" not in repr(lifecycle)
        console_tail = _qualification_console_tail(console_path)
        for marker in (ROOT_TRANSITION_MARKER, WORKLOAD_STARTED_MARKER, LIFECYCLE_READY_COMMITTED_MARKER):
            assert marker.decode("ascii") in console_tail, (
                f"qualification console is missing {marker!r}; tail:\n{console_tail}"
            )

        snapshot = read_run_ledger_snapshot(roots, name)
        assert snapshot.state["status"] == "exited"
        assert snapshot.state["oci_root_handoff"]["phase"] == "terminal"
        try:
            _, active, current_domain_id = _inspect_exact_owned_domain(
                conn,
                name,
                defined.domain_uuid,
                plan.run_id,
                plan.digest,
                expected_inactive_projection_digest,
            )
            if active != 0 or current_domain_id != -1:
                raise ValueError("qualification terminal domain is not exactly inactive")
        except BaseException:
            broker.ambiguous = True
            retain_qualification_state = True
            raise

        def remove_terminal_domain() -> None:
            _remove_exact_owned_domain(
                conn,
                name,
                defined.domain_uuid,
                plan.run_id,
                plan.digest,
                -1,
                expected_inactive_projection_digest,
            )

        def prove_terminal_domain_absent() -> bool:
            by_name = _lookup_domain(conn, name)
            by_uuid = _lookup_domain_uuid(conn, defined.domain_uuid)
            return by_name is None and by_uuid is None

        try:
            _remove_inactive_domain_then_restore(
                broker,
                remove_terminal_domain,
                prove_terminal_domain_absent,
            )
        except BaseException:
            retain_qualification_state = True
            raise
        owned_uuid = None
        owned_domain_id = None
        assert broker.restored is True
        assert broker.ambiguous is False

        verified_root = load_oci_root_volume(roots, prepared.transaction.volume_id)
        assert verified_root.path == root_path
        assert verified_root.record.status == "attached"
        assert _sha256_file(root_path) != before_digest
        debugfs = subprocess.run(
            ["debugfs", "-R", "stat /.palimpsest/upper", os.fspath(root_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert debugfs.returncode == 0, debugfs.stderr[-2000:]
        assert "Type: directory" in debugfs.stdout

        release_prepared_oci_root_run(roots, prepared, store)
        assert read_run_ledger_snapshot(roots, name).state["status"] == "removed"
        assert _lookup_domain(conn, name) is None
        with pytest.raises(StateError, match="record is missing"):
            load_oci_root_volume(roots, prepared.transaction.volume_id)
        assert not root_path.exists()
        assert store.list_lease_set_intents(prepared.transaction.owner) == ()
        prepared = None
    finally:
        if (
            not retain_qualification_state
            and owned_uuid is not None
            and owned_domain_id is not None
            and plan_digest is not None
            and expected_inactive_projection_digest is not None
            and prepared is not None
        ):
            _remove_exact_owned_domain(
                conn,
                name,
                owned_uuid,
                prepared.transaction.owner.run_id,
                plan_digest,
                owned_domain_id,
                expected_inactive_projection_digest,
            )
        domain_absent = False
        if not retain_qualification_state:
            name_absent = _lookup_domain(conn, name) is None
            uuid_absent = owned_uuid is None or _lookup_domain_uuid(conn, owned_uuid) is None
            domain_absent = name_absent and uuid_absent
        if broker is not None and broker.applied and not broker.restored and domain_absent:
            broker.restore_after_domain_absent()
        access_restored = broker is None or broker.restored or (not broker.applied and not broker.ambiguous)
        if prepared is not None and domain_absent and access_restored:
            release_prepared_oci_root_run(roots, prepared, store)
        if broker is not None and access_restored and not retain_qualification_state:
            broker.close()
        conn.close()
        if domain_absent and access_restored:
            _remove_qualification_root(qualification_root, qualification_root_identity)
