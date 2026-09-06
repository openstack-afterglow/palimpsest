"""Fresh private coordinator for an already defined and explicitly granted run.

The caller may use libvirt and threads. Only a new, clean Python process invokes
the existing monitor spawn protocol. A returned endpoint means launch accepted,
not guest READY. Ambiguous failures preserve the monitor's ownership evidence.
"""

from __future__ import annotations

import math
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import oci_monitor_ipc as ipc
from .errors import StateError
from .oci_monitor_launch import MonitorLaunchAuthority

_REQUEST_SCHEMA = "palimpsest.monitor-coordinator-request.v1"
_RESPONSE_SCHEMA = "palimpsest.monitor-coordinator-response.v1"
_MAX_REQUEST_BYTES = ipc._MAX_CONFIG_FRAME_BYTES
_MAX_RESPONSE_BYTES = ipc._MAX_FRAME_BYTES
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _invalid():
    return StateError("OCI monitor coordinator request or response is invalid")


def _uncertain():
    return StateError("OCI monitor coordinator outcome is uncertain; preserve and inspect the exact run evidence")


def _seconds(value, minimum, maximum):
    if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
        raise _invalid()
    return float(value)


@dataclass(frozen=True, slots=True)
class MonitorCoordinatorRequest:
    """Closed data envelope; descriptors are carried only by pass_fds."""

    generation: str
    nonce: str
    timeout_ms: int
    authority: dict[str, Any]

    def __post_init__(self):
        if (
            not ipc._canonical_uuid(self.generation)
            or type(self.nonce) is not str
            or ipc._NONCE_RE.fullmatch(self.nonce) is None
            or type(self.timeout_ms) is not int
            or not 100 <= self.timeout_ms <= 30000
            or type(self.authority) is not dict
        ):
            raise _invalid()

    def to_dict(self):
        self.__post_init__()
        return {
            "schema": _REQUEST_SCHEMA,
            "generation": self.generation,
            "nonce": self.nonce,
            "timeout_ms": self.timeout_ms,
            "authority": self.authority,
        }

    def to_bytes(self):
        value = ipc._canonical_bytes(self.to_dict())
        if not value or len(value) > _MAX_REQUEST_BYTES:
            raise _invalid()
        return value

    @classmethod
    def from_dict(cls, value):
        if (
            type(value) is not dict
            or set(value) != {"schema", *cls.__dataclass_fields__}
            or value["schema"] != _REQUEST_SCHEMA
        ):
            raise _invalid()
        request = cls(**{key: value[key] for key in cls.__dataclass_fields__})
        request.to_bytes()
        return request


def _response(nonce, endpoint=None):
    return {
        "schema": _RESPONSE_SCHEMA,
        "nonce": nonce,
        "state": "launch-accepted" if endpoint is not None else "refused",
        "endpoint": None if endpoint is None else endpoint.to_dict(),
    }


def _parse_response(value, request, identity):
    if (
        type(value) is not dict
        or set(value) != {"schema", "nonce", "state", "endpoint"}
        or value["schema"] != _RESPONSE_SCHEMA
        or value["nonce"] != request.nonce
    ):
        raise _invalid()
    if value["state"] == "refused" and value["endpoint"] is None:
        raise _uncertain()
    if value["state"] != "launch-accepted":
        raise _invalid()
    endpoint = ipc.MonitorExecEndpoint.from_dict(value["endpoint"])
    if endpoint.identity != identity or endpoint.receipt_schema != ipc._RECEIPT_SCHEMA:
        raise _invalid()
    return endpoint


def spawn_monitor_coordinator(
    identity: ipc.MonitorExecIdentity,
    launch_authority: MonitorLaunchAuthority,
    *,
    timeout: float = 5.0,
    coordinator_timeout: float = 30.0,
) -> ipc.MonitorExecEndpoint:
    """Return an authenticated accepted endpoint, never a guest-READY promise.

    This does not take ownership of the caller's authority. It never terminates
    a coordinator/monitor on timeout: a committed monitor may already be active.
    Rediscovery of the exact run journal is the recovery path after uncertainty.
    """
    local = child = None
    process = None
    try:
        if type(identity) is not ipc.MonitorExecIdentity or type(launch_authority) is not MonitorLaunchAuthority:
            raise _invalid()
        identity.__post_init__()
        timeout = _seconds(timeout, ipc._MIN_TIMEOUT_SECONDS, ipc._MAX_TIMEOUT_SECONDS)
        coordinator_timeout = _seconds(coordinator_timeout, timeout, 120.0)
        launch_authority.validate(binding=identity.binding)
        frame = launch_authority.to_dict()
        request = MonitorCoordinatorRequest(identity.generation, os.urandom(32).hex(), int(timeout * 1000), frame)
        payload = request.to_bytes()  # Before any socket or child exists.
        descriptors = launch_authority.pass_fds
        monitor_fd = frame["entries"]["monitor"]["fd"]
        local, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        local.settimeout(coordinator_timeout)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "palimpsest_local.oci_monitor_coordinator",
                "--private-coordinator-v1",
                str(child.fileno()),
            ],
            close_fds=True,
            pass_fds=(child.fileno(), *descriptors),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
            cwd=_PACKAGE_ROOT,
            start_new_session=True,
        )
        child.close()
        child = None
        ipc._send_all(local, payload)
        response = ipc._recv_bounded_frame(local, _MAX_RESPONSE_BYTES)
        endpoint = _parse_response(response, request, identity)
        # The private pipe is only a delivery channel, not independent endpoint
        # authority. Re-authenticate the real monitor and exact durable journal.
        launch_authority.validate(binding=identity.binding)
        if ipc.discover_monitor_exec(monitor_fd, identity.binding, timeout=timeout) != endpoint:
            raise _invalid()
        launch_authority.validate(binding=identity.binding)
        if process.wait(timeout=coordinator_timeout) != 0:
            raise _uncertain()
        return endpoint
    except StateError:
        if process is not None:
            raise _uncertain() from None
        raise
    except Exception:
        raise _uncertain() if process is not None else _invalid() from None
    finally:
        for channel in (local, child):
            if channel is not None:
                channel.close()


def _child_main(channel_fd):
    """Only data validation, then unchanged clean-process monitor spawning."""
    channel = None
    authority = handle = None
    directory_fd = -1
    nonce = "0" * 64
    try:
        if type(channel_fd) is not int or channel_fd < 3:
            raise _invalid()
        ipc._require_spawn_boundary()
        channel = socket.socket(fileno=channel_fd)
        if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_STREAM:
            raise _invalid()
        channel.settimeout(120.0)
        request = MonitorCoordinatorRequest.from_dict(ipc._recv_bounded_frame(channel, _MAX_REQUEST_BYTES))
        nonce = request.nonce
        authority = MonitorLaunchAuthority.from_dict(request.authority, excluded_fds=(channel_fd,))
        identity = ipc.MonitorExecIdentity(
            ipc.MonitorPreActivationBinding.from_dict(request.authority["binding"]), request.generation
        )
        directory_fd = os.dup(request.authority["entries"]["monitor"]["fd"])
        authority.validate(directory_fd=directory_fd, binding=identity.binding)
        handle = ipc.spawn_monitor_exec(
            directory_fd, identity, timeout=request.timeout_ms / 1000, launch_authority=authority
        )
        ipc._send_frame(channel, _response(nonce, handle.endpoint))
        return 0
    except Exception:
        if channel is not None:
            try:
                ipc._send_frame(channel, _response(nonce))
            except Exception:
                pass
        return 1
    finally:
        if handle is not None:
            handle.close()  # Detach only. SHUTDOWN is not detached launch.
        if authority is not None:
            authority.close()
        if directory_fd >= 0:
            os.close(directory_fd)
        if channel is not None:
            channel.close()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if (
        type(args) not in {list, tuple}
        or len(args) != 2
        or any(type(value) is not str for value in args)
        or args[0] != "--private-coordinator-v1"
        or not 1 <= len(args[1]) <= 10
        or not args[1].isascii()
        or not args[1].isdecimal()
        or str(int(args[1])) != args[1]
        or not 3 <= int(args[1]) <= 2**31 - 1
    ):
        return 2
    return _child_main(int(args[1]))


if __name__ == "__main__":
    # Keep exact typed authority classes imported under their canonical module.
    from palimpsest_local.oci_monitor_coordinator import main as canonical_main

    raise SystemExit(canonical_main())
