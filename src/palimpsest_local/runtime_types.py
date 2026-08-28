"""Small immutable contracts shared by runtime ledger routing."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

from .errors import PalimpsestError

_RUN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class RuntimeKind(StrEnum):
    CLOUD_IMAGE = "cloud-image"
    OCI_ROOT = "oci-root"


class RuntimeBackend(StrEnum):
    KVM = "kvm"
    LIBVIRT_HVF = "libvirt-hvf"
    LIMA_VZ = "lima-vz"


class RuntimeOperation(StrEnum):
    START = "start"
    STOP = "stop"
    RM = "rm"
    INSPECT = "inspect"
    LOGS = "logs"


ALLOWED_RUNTIME_COMBINATIONS = frozenset(
    {
        (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
        (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
        (RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    }
)


@dataclass(frozen=True, slots=True)
class DispatchKey:
    runtime_kind: RuntimeKind
    backend: RuntimeBackend

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_kind, RuntimeKind) or not isinstance(self.backend, RuntimeBackend):
            raise TypeError("dispatch key requires RuntimeKind and RuntimeBackend")
        if (self.runtime_kind, self.backend) not in ALLOWED_RUNTIME_COMBINATIONS:
            raise ValueError(f"unsupported runtime/backend combination: {self.runtime_kind.value}/{self.backend.value}")


@dataclass(frozen=True, slots=True)
class ExistingRunRecord:
    """Validated immutable identity and routing fields from an existing run ledger."""

    name: str
    run_id: str
    state_schema_version: int
    dispatch_key: DispatchKey

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _RUN_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("existing run record has an invalid name")
        if not isinstance(self.run_id, str):
            raise TypeError("existing run record requires a string run ID")
        try:
            parsed_run_id = uuid.UUID(self.run_id)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("existing run record has an invalid run ID") from None
        if str(parsed_run_id) != self.run_id:
            raise ValueError("existing run record run ID is not canonical")
        if type(self.state_schema_version) is not int or self.state_schema_version not in {1, 2}:
            raise ValueError("existing run record has an invalid state schema version")
        if not isinstance(self.dispatch_key, DispatchKey):
            raise TypeError("existing run record requires a DispatchKey")


class RuntimeCapabilityError(PalimpsestError):
    """An exact runtime/backend pair cannot yet perform an operation."""

    code = "runtime-operation-unavailable"

    def __init__(self, operation: RuntimeOperation, dispatch_key: DispatchKey) -> None:
        self.operation = operation
        self.dispatch_key = dispatch_key
        super().__init__(
            f"runtime operation '{operation.value}' is unavailable for "
            f"{dispatch_key.runtime_kind.value}/{dispatch_key.backend.value}"
        )


__all__ = (
    "ALLOWED_RUNTIME_COMBINATIONS",
    "DispatchKey",
    "ExistingRunRecord",
    "RuntimeBackend",
    "RuntimeCapabilityError",
    "RuntimeKind",
    "RuntimeOperation",
)
