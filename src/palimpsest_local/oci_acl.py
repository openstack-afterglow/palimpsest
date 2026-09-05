"""Private, exact-FD Linux ACL I/O; no ownership or lifecycle decisions.

Only the two OCI runtime I/O ACL shapes are understood. The caller owns the
descriptor, identity checks, durable intent, fsync and any recovery policy.
Command failure is ambiguous and never triggers an implicit restoration.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import StateError

_MAX_DAC_ID = 2**32 - 2
_MAX_ACL_BYTES = 4096


class OCIACLError(StateError):
    """Path-free ACL refusal; backend errors never authorize rollback."""


def _invalid() -> OCIACLError:
    return OCIACLError("OCI runtime ACL is invalid or changed")


@dataclass(frozen=True, slots=True)
class ACLStructure:
    user: str
    named_users: tuple[tuple[int, str], ...]
    group: str
    mask: str | None
    other: str

    def __post_init__(self) -> None:
        if (
            type(self.user) is not str
            or type(self.group) is not str
            or type(self.other) is not str
            or (self.mask is not None and type(self.mask) is not str)
            or self.user not in {"rwx", "rw-"}
            or self.group != "---"
            or self.other != "---"
        ):
            raise _invalid()
        if type(self.named_users) is not tuple or len(self.named_users) > 1:
            raise _invalid()
        if not self.named_users:
            if self.mask is not None:
                raise _invalid()
            return
        entry = self.named_users[0]
        permission = "-wx" if self.user == "rwx" else "rw-"
        if (
            type(entry) is not tuple
            or len(entry) != 2
            or type(entry[0]) is not int
            or not 0 <= entry[0] <= _MAX_DAC_ID
            or entry[1] != permission
            or self.mask != permission
        ):
            raise _invalid()

    def setfile_text(self) -> str:
        self.__post_init__()
        lines = [f"user::{self.user}"]
        lines.extend(f"user:{uid}:{permission}" for uid, permission in self.named_users)
        lines.append(f"group::{self.group}")
        if self.mask is not None:
            lines.append(f"mask::{self.mask}")
        lines.append(f"other::{self.other}")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "user": self.user,
            "named_users": [[uid, permission] for uid, permission in self.named_users],
            "group": self.group,
            "mask": self.mask,
            "other": self.other,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ACLStructure:
        if not isinstance(value, Mapping) or set(value) != {"user", "named_users", "group", "mask", "other"}:
            raise _invalid()
        entries = value["named_users"]
        if type(entries) not in {list, tuple} or any(type(entry) not in {list, tuple} for entry in entries):
            raise _invalid()
        try:
            return cls(
                value["user"], tuple(tuple(entry) for entry in entries), value["group"], value["mask"], value["other"]
            )
        except (TypeError, ValueError):
            raise _invalid() from None


def baseline_acl(*, directory: bool) -> ACLStructure:
    if type(directory) is not bool:
        raise _invalid()
    return ACLStructure("rwx" if directory else "rw-", (), "---", None, "---")


def grant_acl(baseline: ACLStructure, uid: int) -> ACLStructure:
    if type(baseline) is not ACLStructure:
        raise _invalid()
    baseline.__post_init__()
    if baseline.named_users:
        raise _invalid()
    permission = "-wx" if baseline.user == "rwx" else "rw-"
    return ACLStructure(baseline.user, ((uid, permission),), "---", permission, "---")


def parse_acl(payload: str) -> ACLStructure:
    """Parse only canonical numeric GNU getfacl output, including one blank tail."""
    if type(payload) is not str or not payload.endswith("\n") or len(payload) > _MAX_ACL_BYTES or "\r" in payload:
        raise _invalid()
    lines = payload.split("\n")
    lines.pop()
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) not in {3, 5}:
        raise _invalid()
    user = re.fullmatch(r"user::(rwx|rw-)", lines[0])
    if user is None:
        raise _invalid()
    named: tuple[tuple[int, str], ...] = ()
    mask = None
    if len(lines) == 5:
        entry = re.fullmatch(r"user:(0|[1-9][0-9]{0,9}):(-wx|rw-)", lines[1])
        mask_entry = re.fullmatch(r"mask::(-wx|rw-)", lines[3])
        if entry is None or mask_entry is None or lines[2] != "group::---":
            raise _invalid()
        named = ((int(entry.group(1)), entry.group(2)),)
        mask = mask_entry.group(1)
    elif lines[1] != "group::---":
        raise _invalid()
    if lines[-1] != "other::---":
        raise _invalid()
    return ACLStructure(user.group(1), named, "---", mask, "---")


class LinuxFdACLBackend:
    """Fixed commands against one borrowed FD, with bounded command execution.

    ``runner`` is an explicit backend-test seam, not bootstrap configuration.
    An injected backend is responsible for its environment; the default needs
    Linux procfs and installed GNU ACL utilities at their system paths.
    """

    def __init__(self, *, runner: Callable[..., Any] | None = None) -> None:
        if runner is not None and not callable(runner):
            raise _invalid()
        self._runner = subprocess.run if runner is None else runner
        self._native = runner is None

    def _command(self, fd: int, *, acl: ACLStructure | None = None) -> str:
        if type(fd) is not int or not 3 <= fd <= 2**31 - 1 or (self._native and sys.platform != "linux"):
            raise _invalid()
        try:
            os.fstat(fd)
        except OSError:
            raise _invalid() from None
        target = f"/proc/self/fd/{fd}"
        argv = (
            ["/usr/bin/getfacl", "-cpn", "--", target]
            if acl is None
            else ["/usr/bin/setfacl", "--no-mask", "--set-file=-", "--", target]
        )
        try:
            result = self._runner(
                argv,
                input=None if acl is None else acl.setfile_text(),
                text=True,
                capture_output=True,
                check=False,
                pass_fds=(fd,),
                timeout=10,
                env={"LC_ALL": "C"},
            )
        except Exception:
            raise OCIACLError("OCI runtime ACL command failed") from None
        if type(result) is not subprocess.CompletedProcess:
            raise OCIACLError("OCI runtime ACL command failed")
        returncode = getattr(result, "returncode", None)
        stdout = getattr(result, "stdout", None)
        stderr = getattr(result, "stderr", None)
        try:
            output_valid = type(stdout) is str and len(stdout.encode("utf-8")) <= _MAX_ACL_BYTES
        except UnicodeError:
            output_valid = False
        if (
            type(returncode) is not int
            or returncode != 0
            or stderr != ""
            or not output_valid
            or (acl is not None and stdout != "")
        ):
            raise OCIACLError("OCI runtime ACL command failed")
        return stdout

    def read_acl(self, fd: int) -> ACLStructure:
        return parse_acl(self._command(fd))

    def write_acl(self, fd: int, acl: ACLStructure) -> ACLStructure:
        if type(acl) is not ACLStructure:
            raise _invalid()
        acl.__post_init__()
        self._command(fd, acl=acl)
        observed = self.read_acl(fd)
        if observed != acl:
            raise OCIACLError("OCI runtime ACL readback changed")
        return observed


def _scalar(element: ET.Element, *, attributes: dict[str, str] | None = None) -> str:
    if (
        element.attrib != (attributes or {})
        or list(element)
        or element.text is None
        or element.text.strip() != element.text
        or (element.tail is not None and element.tail.strip())
    ):
        raise _invalid()
    return element.text


def parse_qemu_dac_baselabel(capabilities: str) -> tuple[int, int]:
    """Select exactly one canonical DAC KVM uid/gid; caller enforces UID policy."""
    if type(capabilities) is not str or len(capabilities) > 1024 * 1024 or "<!" in capabilities:
        raise _invalid()
    try:
        root = ET.fromstring(capabilities)
    except ET.ParseError:
        raise _invalid() from None
    if root.tag != "capabilities" or root.attrib or (root.text is not None and root.text.strip()):
        raise _invalid()
    hosts = [child for child in root if child.tag == "host"]
    if len(hosts) != 1:
        raise _invalid()
    models = [
        model
        for model in hosts[0]
        if model.tag == "secmodel"
        and any(child.tag == "model" and child.text is not None and child.text.strip() == "dac" for child in model)
    ]
    if len(models) != 1:
        raise _invalid()
    model = models[0]
    if (
        model.attrib
        or (model.text is not None and model.text.strip())
        or any(child.tag not in {"model", "doi", "baselabel"} for child in model)
    ):
        raise _invalid()
    names = [child for child in model if child.tag == "model"]
    dois = [child for child in model if child.tag == "doi"]
    labels = [child for child in model if child.tag == "baselabel" and child.get("type") == "kvm"]
    if len(names) != 1 or _scalar(names[0]) != "dac" or len(dois) != 1 or _scalar(dois[0]) != "0" or len(labels) != 1:
        raise _invalid()
    match = re.fullmatch(r"\+(0|[1-9][0-9]{0,9}):\+(0|[1-9][0-9]{0,9})", _scalar(labels[0], attributes={"type": "kvm"}))
    if match is None:
        raise _invalid()
    uid, gid = (int(value) for value in match.groups())
    if uid > _MAX_DAC_ID or gid > _MAX_DAC_ID:
        raise _invalid()
    return uid, gid
