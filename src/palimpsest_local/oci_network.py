"""Typed OCI-root guest networking selected explicitly per run.

The runtime offers one fixed virtual network per VM through QEMU's built-in
user-mode (SLIRP) backend.  ``nat`` gives the guest a private address with
outbound NAT, ``host-only`` gives the same private address without any egress,
and ``none`` keeps the historical no-NIC domain.  Inbound reachability is never
implicit: every host listener comes from an explicit published port and
defaults to loopback.

This module is pure policy.  It never creates a libvirt network, writes a
firewall rule, starts a helper daemon, or binds a socket: the only host
listeners are the ones QEMU itself opens for the authored forwarding rules, and
they disappear with the VM process.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import ArtifactValidationError

OCI_NETWORK_SCHEMA = "palimpsest.oci-root-network.v1"
OCI_NETWORK_BACKEND = "qemu-user-mode-slirp.v1"
OCI_NETWORK_MODE_NONE = "none"
OCI_NETWORK_MODE_NAT = "nat"
OCI_NETWORK_MODE_HOST_ONLY = "host-only"
OCI_NETWORK_MODES = (OCI_NETWORK_MODE_NONE, OCI_NETWORK_MODE_NAT, OCI_NETWORK_MODE_HOST_ONLY)
OCI_NETWORK_DEFAULT_MODE = OCI_NETWORK_MODE_NAT

OCI_NETWORK_SUBNET = "10.0.2.0/24"
OCI_NETWORK_GUEST_ADDRESS = "10.0.2.15"
OCI_NETWORK_GATEWAY = "10.0.2.2"
OCI_NETWORK_NETMASK = "255.255.255.0"
OCI_NETWORK_NAMESERVER = "10.0.2.3"
OCI_NETWORK_INTERFACE = "eth0"
OCI_NETWORK_NETDEV_ID = "pnet0"
# libvirt allocates its own pcie-root-ports from slot 0x1 upward and never
# reaches slot 0x14 for the bounded OCI-root device set. An unaddressed
# passthrough device would race libvirt for slot 0x1 and abort the domain, so
# the authored NIC pins its own root-complex address.
OCI_NETWORK_PCI_BUS = "pcie.0"
OCI_NETWORK_PCI_ADDRESS = "0x14"
OCI_NETWORK_MAC_PREFIX = "52:54:00"
OCI_NETWORK_PROTOCOLS = ("tcp", "udp")
OCI_NETWORK_LOOPBACK_HOST_IP = "127.0.0.1"
OCI_NETWORK_WILDCARD_HOST_IP = "0.0.0.0"

MAX_OCI_PUBLISHED_PORTS = 32
MIN_OCI_PUBLISHED_HOST_PORT = 1024
MAX_OCI_PORT = 65535

_IPV4_RE = re.compile(r"^(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}$")
_MAC_SUFFIX_RE = re.compile(r"^[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")
_PUBLISH_RE = re.compile(
    r"^(?:(?P<host_ip>[0-9.]+):)?(?P<host_port>[0-9]{1,5}):(?P<guest_port>[0-9]{1,5})(?:/(?P<protocol>[a-z]{3}))?$"
)


def _octets(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, str) or _IPV4_RE.fullmatch(value) is None:
        return None
    parts = tuple(int(item) for item in value.split("."))
    if any(item > 255 for item in parts):
        return None
    return parts  # type: ignore[return-value]


def valid_host_bind_address(value: Any) -> bool:
    """Accept only a unicast or wildcard IPv4 literal for a host listener."""

    parts = _octets(value)
    if parts is None:
        return False
    if parts[0] >= 224:
        # Multicast, reserved and the all-ones broadcast address are never
        # host listener addresses.
        return False
    return True


def loopback_host_bind_address(value: Any) -> bool:
    parts = _octets(value)
    return parts is not None and parts[0] == 127


@dataclass(frozen=True, slots=True)
class OCIPublishedPort:
    """One explicit host listener forwarded to one guest port."""

    host_ip: str
    host_port: int
    guest_port: int
    protocol: str = "tcp"

    def __post_init__(self) -> None:
        if not valid_host_bind_address(self.host_ip):
            raise ArtifactValidationError("published port host address must be an IPv4 literal")
        for label, port in (("host", self.host_port), ("guest", self.guest_port)):
            if type(port) is not int or not 1 <= port <= MAX_OCI_PORT:
                raise ArtifactValidationError(f"published {label} port must be between 1 and {MAX_OCI_PORT}")
        if self.host_port < MIN_OCI_PUBLISHED_HOST_PORT:
            raise ArtifactValidationError(
                f"published host port must be {MIN_OCI_PUBLISHED_HOST_PORT} or above; "
                "the VM process never receives privileged-port authority"
            )
        if self.protocol not in OCI_NETWORK_PROTOCOLS:
            raise ArtifactValidationError("published port protocol must be tcp or udp")

    @property
    def external(self) -> bool:
        """Report whether this listener is reachable beyond the host itself."""

        return not loopback_host_bind_address(self.host_ip)

    @property
    def identity(self) -> tuple[str, int, str]:
        return (self.host_ip, self.host_port, self.protocol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guest_port": self.guest_port,
            "host_ip": self.host_ip,
            "host_port": self.host_port,
            "protocol": self.protocol,
        }

    @classmethod
    def from_dict(cls, value: Any) -> OCIPublishedPort:
        if not isinstance(value, Mapping) or set(value) != {"guest_port", "host_ip", "host_port", "protocol"}:
            raise ArtifactValidationError("published port fields are invalid")
        return cls(
            host_ip=value["host_ip"],
            host_port=value["host_port"],
            guest_port=value["guest_port"],
            protocol=value["protocol"],
        )

    @classmethod
    def from_override_value(cls, value: Any) -> OCIPublishedPort:
        """Parse ``[HOST_IP:]HOST_PORT:GUEST_PORT[/tcp|/udp]``.

        The host address defaults to loopback, so publishing a port never
        exposes a service beyond the host unless the caller writes an explicit
        non-loopback address such as ``0.0.0.0``.
        """

        if not isinstance(value, str):
            raise ArtifactValidationError("published port must be a string")
        match = _PUBLISH_RE.fullmatch(value)
        if match is None:
            raise ArtifactValidationError(
                "published port must be [HOST_IP:]HOST_PORT:GUEST_PORT[/tcp|/udp]; "
                "port ranges and container-only ports are unsupported"
            )
        host_port = match.group("host_port")
        guest_port = match.group("guest_port")
        if host_port != str(int(host_port)) or guest_port != str(int(guest_port)):
            raise ArtifactValidationError("published port numbers must be canonical decimals")
        return cls(
            host_ip=match.group("host_ip") or OCI_NETWORK_LOOPBACK_HOST_IP,
            host_port=int(host_port),
            guest_port=int(guest_port),
            protocol=match.group("protocol") or "tcp",
        )


@dataclass(frozen=True, slots=True)
class OCINetworkConfig:
    """The complete, durable networking intent of one OCI-root run."""

    mode: str = OCI_NETWORK_DEFAULT_MODE
    published_ports: tuple[OCIPublishedPort, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.mode not in OCI_NETWORK_MODES:
            raise ArtifactValidationError("OCI run network mode must be nat, host-only, or none")
        if not isinstance(self.published_ports, tuple) or any(
            not isinstance(item, OCIPublishedPort) for item in self.published_ports
        ):
            raise ArtifactValidationError("OCI run published ports must be typed")
        if len(self.published_ports) > MAX_OCI_PUBLISHED_PORTS:
            raise ArtifactValidationError(f"at most {MAX_OCI_PUBLISHED_PORTS} ports can be published for one run")
        if self.mode == OCI_NETWORK_MODE_NONE and self.published_ports:
            raise ArtifactValidationError("--publish requires --network nat or --network host-only")
        if self.mode == OCI_NETWORK_MODE_HOST_ONLY and any(port.external for port in self.published_ports):
            raise ArtifactValidationError(
                "host-only networking publishes to loopback addresses only; use --network nat for external exposure"
            )
        identities = [port.identity for port in self.published_ports]
        if len(identities) != len(set(identities)):
            raise ArtifactValidationError("published host listeners must be unique")
        wildcard = {
            (port.host_port, port.protocol)
            for port in self.published_ports
            if port.host_ip == OCI_NETWORK_WILDCARD_HOST_IP
        }
        specific = {
            (port.host_port, port.protocol)
            for port in self.published_ports
            if port.host_ip != OCI_NETWORK_WILDCARD_HOST_IP
        }
        if wildcard & specific:
            raise ArtifactValidationError("a published port cannot use both a wildcard and a specific host address")

    @property
    def enabled(self) -> bool:
        return self.mode != OCI_NETWORK_MODE_NONE

    @property
    def egress(self) -> bool:
        return self.mode == OCI_NETWORK_MODE_NAT

    @property
    def nameservers(self) -> tuple[str, ...]:
        return (OCI_NETWORK_NAMESERVER,) if self.egress else ()

    @property
    def external_ports(self) -> tuple[OCIPublishedPort, ...]:
        return tuple(port for port in self.published_ports if port.external)

    def to_dict(self) -> dict[str, Any]:
        """Return the host-side network authority bound into the domain core.

        The guest projection is intentionally not duplicated here: it is
        derived per run in the stage-1 plan, which is itself bound to this
        contract through the domain core digest.
        """

        return {
            "backend": OCI_NETWORK_BACKEND if self.enabled else None,
            "mode": self.mode,
            "published_ports": [port.to_dict() for port in self.published_ports],
            "schema": OCI_NETWORK_SCHEMA,
            "subnet": OCI_NETWORK_SUBNET if self.enabled else None,
        }

    def guest_contract(self, run_id: str) -> dict[str, Any]:
        """Return the guest-visible NIC contract carried in the stage-1 plan.

        PID 1 uses this to identify exactly one expected non-loopback NIC by
        MAC and address before the workload starts. Host publications are
        deliberately absent: they are a host concern and stay bound through the
        domain core digest.
        """

        enabled = self.enabled
        return {
            "address": OCI_NETWORK_GUEST_ADDRESS if enabled else "",
            "gateway": OCI_NETWORK_GATEWAY if enabled else "",
            "interface": OCI_NETWORK_INTERFACE,
            "mac": self.guest_mac_address(run_id) if enabled else "",
            "mode": self.mode,
            "nameservers": list(self.nameservers),
            "netmask": OCI_NETWORK_NETMASK if enabled else "",
        }

    def kernel_cmdline_fragment(self) -> str:
        """Return the static kernel IP configuration, or an empty string."""

        if not self.enabled:
            return ""
        fragment = (
            f"ip={OCI_NETWORK_GUEST_ADDRESS}::{OCI_NETWORK_GATEWAY}:{OCI_NETWORK_NETMASK}::{OCI_NETWORK_INTERFACE}:off"
        )
        for nameserver in self.nameservers:
            fragment = f"{fragment}:{nameserver}"
        return fragment

    def guest_mac_address(self, run_id: str) -> str:
        """Derive the deterministic per-run MAC inside QEMU's assigned range."""

        import hashlib
        import uuid as uuid_module

        try:
            canonical = str(uuid_module.UUID(run_id))
        except (AttributeError, TypeError, ValueError):
            raise ArtifactValidationError("OCI network MAC derivation requires a canonical run ID") from None
        if canonical != run_id:
            raise ArtifactValidationError("OCI network MAC derivation requires a canonical run ID")
        digest = hashlib.sha256(f"palimpsest-oci-root-network-mac-v1\0{canonical}".encode()).hexdigest()
        suffix = ":".join(digest[index : index + 2] for index in (0, 2, 4))
        if _MAC_SUFFIX_RE.fullmatch(suffix) is None:  # pragma: no cover - hex digest is fixed width
            raise ArtifactValidationError("derived OCI network MAC is invalid")
        return f"{OCI_NETWORK_MAC_PREFIX}:{suffix}"

    def qemu_arguments(self, run_id: str) -> tuple[str, ...]:
        """Render the exact QEMU argument vector for this run's NIC.

        Every interpolated value is a validated enum, IPv4 literal, or bounded
        integer, so the comma-delimited QEMU option language cannot be extended
        by caller input.
        """

        if not self.enabled:
            return ()
        options = [
            "user",
            f"id={OCI_NETWORK_NETDEV_ID}",
            f"net={OCI_NETWORK_SUBNET}",
            f"host={OCI_NETWORK_GATEWAY}",
        ]
        if self.egress:
            options.append(f"dns={OCI_NETWORK_NAMESERVER}")
        else:
            options.append("restrict=on")
        for port in self.published_ports:
            options.append(f"hostfwd={port.protocol}:{port.host_ip}:{port.host_port}-:{port.guest_port}")
        device = (
            f"virtio-net-pci,netdev={OCI_NETWORK_NETDEV_ID},mac={self.guest_mac_address(run_id)},"
            f"bus={OCI_NETWORK_PCI_BUS},addr={OCI_NETWORK_PCI_ADDRESS}"
        )
        return ("-netdev", ",".join(options), "-device", device)

    def endpoints(self) -> tuple[str, ...]:
        """Describe each published listener for operator output."""

        return tuple(
            f"{port.host_ip}:{port.host_port}->{OCI_NETWORK_GUEST_ADDRESS}:{port.guest_port}/{port.protocol}"
            for port in self.published_ports
        )

    @classmethod
    def from_dict(cls, value: Any) -> OCINetworkConfig:
        if not isinstance(value, Mapping) or set(value) != {
            "backend",
            "mode",
            "published_ports",
            "schema",
            "subnet",
        }:
            raise ArtifactValidationError("OCI run network fields are invalid")
        if value.get("schema") != OCI_NETWORK_SCHEMA:
            raise ArtifactValidationError("OCI run network schema is invalid")
        raw_ports = value.get("published_ports")
        if not isinstance(raw_ports, Sequence) or isinstance(raw_ports, (str, bytes)):
            raise ArtifactValidationError("OCI run published ports are invalid")
        config = cls(
            mode=value.get("mode"),
            published_ports=tuple(OCIPublishedPort.from_dict(item) for item in raw_ports),
        )
        if config.to_dict() != {key: _plain(item) for key, item in value.items()}:
            raise ArtifactValidationError("OCI run network contract is not canonical")
        return config

    @classmethod
    def from_guest_contract(cls, value: Any, *, run_id: str) -> OCINetworkConfig:
        """Rebuild the mode-only guest view carried in the stage-1 plan.

        The guest contract deliberately omits host forwarding: published host
        addresses are a host-side concern and never enter the guest. Host
        publications stay bound through the domain core digest instead.
        """

        if not isinstance(value, Mapping) or set(value) != {
            "address",
            "gateway",
            "interface",
            "mac",
            "mode",
            "nameservers",
            "netmask",
        }:
            raise ArtifactValidationError("OCI guest network fields are invalid")
        config = cls(mode=value.get("mode"))
        if config.guest_contract(run_id) != {key: _plain(item) for key, item in value.items()}:
            raise ArtifactValidationError("OCI guest network contract is not canonical")
        return config

    @classmethod
    def resolve(cls, mode: Any = None, published: Any = ()) -> OCINetworkConfig:
        """Build a configuration from CLI-shaped input.

        ``mode`` defaults to NAT so an unqualified run behaves like a Docker
        container: a private address with outbound NAT and no inbound exposure.
        """

        if mode is None:
            mode = OCI_NETWORK_DEFAULT_MODE
        if published is None:
            published = ()
        if isinstance(published, (str, bytes)) or not isinstance(published, Sequence):
            raise ArtifactValidationError("OCI run published ports must be a sequence of strings")
        return cls(
            mode=mode,
            published_ports=tuple(OCIPublishedPort.from_override_value(item) for item in published),
        )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


_NETDEV_RE = re.compile(
    "^user,id="
    + re.escape(OCI_NETWORK_NETDEV_ID)
    + ",net="
    + re.escape(OCI_NETWORK_SUBNET)
    + ",host="
    + re.escape(OCI_NETWORK_GATEWAY)
    + "(?:,dns="
    + re.escape(OCI_NETWORK_NAMESERVER)
    + "|,restrict=on)"
    + "(?:,hostfwd=(?:"
    + "|".join(OCI_NETWORK_PROTOCOLS)
    + r"):(?:[0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]{1,5}-:[0-9]{1,5})*$"
)
_DEVICE_RE = re.compile(
    "^virtio-net-pci,netdev="
    + re.escape(OCI_NETWORK_NETDEV_ID)
    + ",mac="
    + re.escape(OCI_NETWORK_MAC_PREFIX)
    + "(?::[0-9a-f]{2}){3},bus="
    + re.escape(OCI_NETWORK_PCI_BUS)
    + ",addr="
    + re.escape(OCI_NETWORK_PCI_ADDRESS)
    + "$"
)


def validated_qemu_arguments(values: Sequence[str]) -> tuple[str, ...]:
    """Fail closed unless ``values`` is exactly one authored OCI-root NIC vector.

    This re-validates a QEMU argument vector recovered from a defined libvirt
    domain. Only the closed user-mode netdev and its virtio device are
    admitted: a tap device, bridge, file descriptor, host device, monitor,
    guest agent, or any other QEMU option is rejected.
    """

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ArtifactValidationError("OCI-root QEMU network arguments are invalid")
    arguments = tuple(values)
    if len(arguments) != 4 or any(not isinstance(item, str) for item in arguments):
        raise ArtifactValidationError("OCI-root QEMU network argument vector is invalid")
    if arguments[0] != "-netdev" or arguments[2] != "-device":
        raise ArtifactValidationError("OCI-root QEMU network argument order is invalid")
    if _NETDEV_RE.fullmatch(arguments[1]) is None or _DEVICE_RE.fullmatch(arguments[3]) is None:
        raise ArtifactValidationError("OCI-root QEMU network argument value is invalid")
    forwards = [item for item in arguments[1].split(",") if item.startswith("hostfwd=")]
    if len(forwards) > MAX_OCI_PUBLISHED_PORTS or len(forwards) != len(set(forwards)):
        raise ArtifactValidationError("OCI-root QEMU network forwarding set is invalid")
    for forward in forwards:
        protocol, host_ip, endpoint = forward.removeprefix("hostfwd=").split(":", 2)
        host_port, guest_port = endpoint.split("-:", 1)
        OCIPublishedPort(
            host_ip=host_ip,
            host_port=int(host_port),
            guest_port=int(guest_port),
            protocol=protocol,
        )
    return arguments


OCI_NETWORK_NONE = OCINetworkConfig(OCI_NETWORK_MODE_NONE)
OCI_NETWORK_DEFAULT = OCINetworkConfig(OCI_NETWORK_DEFAULT_MODE)

__all__ = [
    "MAX_OCI_PUBLISHED_PORTS",
    "MIN_OCI_PUBLISHED_HOST_PORT",
    "OCI_NETWORK_BACKEND",
    "OCI_NETWORK_DEFAULT",
    "OCI_NETWORK_DEFAULT_MODE",
    "OCI_NETWORK_GATEWAY",
    "OCI_NETWORK_GUEST_ADDRESS",
    "OCI_NETWORK_INTERFACE",
    "OCI_NETWORK_LOOPBACK_HOST_IP",
    "OCI_NETWORK_MAC_PREFIX",
    "OCI_NETWORK_MODES",
    "OCI_NETWORK_MODE_HOST_ONLY",
    "OCI_NETWORK_MODE_NAT",
    "OCI_NETWORK_MODE_NONE",
    "OCI_NETWORK_NAMESERVER",
    "OCI_NETWORK_NETDEV_ID",
    "OCI_NETWORK_NETMASK",
    "OCI_NETWORK_NONE",
    "OCI_NETWORK_PROTOCOLS",
    "OCI_NETWORK_SCHEMA",
    "OCI_NETWORK_SUBNET",
    "OCI_NETWORK_WILDCARD_HOST_IP",
    "OCINetworkConfig",
    "OCIPublishedPort",
    "loopback_host_bind_address",
    "valid_host_bind_address",
    "validated_qemu_arguments",
]
