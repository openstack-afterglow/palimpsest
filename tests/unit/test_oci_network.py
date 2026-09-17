"""Closed-world contract for selectable OCI-root guest networking."""

from __future__ import annotations

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_network import (
    MAX_OCI_PUBLISHED_PORTS,
    OCI_NETWORK_DEFAULT,
    OCI_NETWORK_GUEST_ADDRESS,
    OCI_NETWORK_NONE,
    OCINetworkConfig,
    OCIPublishedPort,
    validated_qemu_arguments,
)

RUN_ID = "f6f546e2-e734-4920-9eff-1762b348a249"
OTHER_RUN_ID = "a1ed4f44-4dd5-48ac-b948-86425eb2e710"


def test_omitted_network_with_publish_still_uses_the_nat_default() -> None:
    config = OCINetworkConfig.resolve(None, ["18080:80"])

    assert config.mode == "nat"
    assert config.published_ports == (OCIPublishedPort("127.0.0.1", 18080, 80, "tcp"),)


def test_omitted_network_selects_nat_egress_without_inbound_exposure() -> None:
    config = OCINetworkConfig.resolve()

    assert config == OCI_NETWORK_DEFAULT
    assert config.mode == "nat"
    assert config.enabled and config.egress
    assert config.published_ports == ()
    assert config.external_ports == ()
    assert config.nameservers == ("10.0.2.3",)
    assert config.kernel_cmdline_fragment() == "ip=10.0.2.15::10.0.2.2:255.255.255.0::eth0:off:10.0.2.3"


def test_explicit_none_keeps_the_no_nic_domain_and_guest_contract() -> None:
    config = OCINetworkConfig.resolve("none")

    assert config == OCI_NETWORK_NONE
    assert not config.enabled and not config.egress
    assert config.qemu_arguments(RUN_ID) == ()
    assert config.kernel_cmdline_fragment() == ""
    assert config.guest_contract(RUN_ID) == {
        "address": "",
        "gateway": "",
        "interface": "eth0",
        "mac": "",
        "mode": "none",
        "nameservers": [],
        "netmask": "",
    }


def test_host_only_keeps_the_private_address_without_egress_or_resolver() -> None:
    config = OCINetworkConfig.resolve("host-only", ["127.0.0.1:18080:80"])

    assert config.enabled and not config.egress
    assert config.nameservers == ()
    assert "restrict=on" in config.qemu_arguments(RUN_ID)[1]
    assert "dns=" not in config.qemu_arguments(RUN_ID)[1]
    assert config.kernel_cmdline_fragment().endswith(":eth0:off")
    assert config.guest_contract(RUN_ID)["nameservers"] == []


def test_publish_defaults_to_loopback_and_marks_explicit_external_exposure() -> None:
    loopback = OCIPublishedPort.from_override_value("18080:80")
    external = OCIPublishedPort.from_override_value("0.0.0.0:19090:9090/udp")

    assert loopback == OCIPublishedPort("127.0.0.1", 18080, 80, "tcp")
    assert not loopback.external
    assert external.protocol == "udp" and external.external

    config = OCINetworkConfig.resolve("nat", ["18080:80", "0.0.0.0:19090:9090/udp"])
    assert config.external_ports == (external,)
    assert config.endpoints() == (
        f"127.0.0.1:18080->{OCI_NETWORK_GUEST_ADDRESS}:80/tcp",
        f"0.0.0.0:19090->{OCI_NETWORK_GUEST_ADDRESS}:9090/udp",
    )


def test_qemu_argument_vector_is_deterministic_and_run_bound() -> None:
    config = OCINetworkConfig.resolve("nat", ["127.0.0.1:18080:18080"])
    arguments = config.qemu_arguments(RUN_ID)

    assert arguments == (
        "-netdev",
        "user,id=pnet0,net=10.0.2.0/24,host=10.0.2.2,dns=10.0.2.3,hostfwd=tcp:127.0.0.1:18080-:18080",
        "-device",
        f"virtio-net-pci,netdev=pnet0,mac={config.guest_mac_address(RUN_ID)},bus=pcie.0,addr=0x14",
    )
    assert validated_qemu_arguments(arguments) == arguments
    assert config.guest_mac_address(RUN_ID).startswith("52:54:00")
    assert config.guest_mac_address(RUN_ID) != config.guest_mac_address(OTHER_RUN_ID)
    assert config.guest_contract(RUN_ID)["mac"] == config.guest_mac_address(RUN_ID)


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("-netdev", "user,id=pnet0,net=10.0.2.0/24,host=10.0.2.2,dns=10.0.2.3"),
        (
            "-netdev",
            "tap,id=pnet0,ifname=tap0",
            "-device",
            "virtio-net-pci,netdev=pnet0,mac=52:54:00:ab:cd:ef,bus=pcie.0,addr=0x14",
        ),
        ("-device", "virtio-net-pci,netdev=pnet0,mac=52:54:00:ab:cd:ef", "-netdev", "user,id=pnet0"),
        (
            "-netdev",
            "user,id=pnet0,net=10.0.2.0/24,host=10.0.2.2,dns=10.0.2.3,smb=/etc",
            "-device",
            "virtio-net-pci,netdev=pnet0,mac=52:54:00:ab:cd:ef,bus=pcie.0,addr=0x14",
        ),
        (
            "-netdev",
            "user,id=pnet0,net=10.0.2.0/24,host=10.0.2.2,dns=10.0.2.3",
            "-device",
            "vfio-pci,host=0000:01:00.0",
        ),
        (
            "-netdev",
            "user,id=pnet0,net=10.0.2.0/24,host=192.168.1.1,dns=10.0.2.3",
            "-device",
            "virtio-net-pci,netdev=pnet0,mac=52:54:00:ab:cd:ef,bus=pcie.0,addr=0x14",
        ),
    ],
)
def test_foreign_qemu_network_arguments_are_refused(arguments: tuple[str, ...]) -> None:
    with pytest.raises(ArtifactValidationError):
        validated_qemu_arguments(arguments)


@pytest.mark.parametrize(
    "mode, published",
    [
        ("bridge", []),
        ("NAT", []),
        ("none", ["18080:80"]),
        ("host-only", ["0.0.0.0:18080:80"]),
        ("nat", ["80:80"]),
        ("nat", ["1023:80"]),
        ("nat", ["18080:0"]),
        ("nat", ["18080:70000"]),
        ("nat", ["18080"]),
        ("nat", ["80"]),
        ("nat", ["18080-18090:80"]),
        ("nat", ["[::1]:18080:80"]),
        ("nat", ["::1:18080:80"]),
        ("nat", ["localhost:18080:80"]),
        ("nat", ["127.0.0.1:18080:80/sctp"]),
        ("nat", ["127.0.0.1:18080:80/TCP"]),
        ("nat", ["127.0.0.1:018080:80"]),
        ("nat", ["224.0.0.1:18080:80"]),
        ("nat", ["999.1.1.1:18080:80"]),
        ("nat", [" 18080:80"]),
        ("nat", ["18080:80 "]),
        ("nat", ["18080:80", "18080:81"]),
        ("nat", ["0.0.0.0:18080:80", "127.0.0.1:18080:81"]),
    ],
)
def test_unsupported_network_selection_is_refused(mode: str | None, published: list[str]) -> None:
    with pytest.raises(ArtifactValidationError):
        OCINetworkConfig.resolve(mode, published)


def test_published_port_count_is_bounded() -> None:
    accepted = [f"{20000 + index}:80" for index in range(MAX_OCI_PUBLISHED_PORTS)]
    assert len(OCINetworkConfig.resolve("nat", accepted).published_ports) == MAX_OCI_PUBLISHED_PORTS
    with pytest.raises(ArtifactValidationError):
        OCINetworkConfig.resolve("nat", [*accepted, "20099:80"])


def test_durable_contract_round_trips_without_the_guest_projection() -> None:
    config = OCINetworkConfig.resolve("nat", ["127.0.0.1:18080:80", "0.0.0.0:18443:443"])
    value = config.to_dict()

    assert set(value) == {"backend", "mode", "published_ports", "schema", "subnet"}
    assert value["backend"] == "qemu-user-mode-slirp.v1"
    assert value["subnet"] == "10.0.2.0/24"
    assert OCINetworkConfig.from_dict(value) == config
    assert OCINetworkConfig.from_dict(OCI_NETWORK_NONE.to_dict()) == OCI_NETWORK_NONE
    assert OCI_NETWORK_NONE.to_dict()["backend"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": "palimpsest.oci-root-network.v2"},
        {"mode": "none"},
        {"backend": None},
        {"subnet": "192.168.0.0/24"},
    ],
)
def test_non_canonical_durable_network_state_is_refused(mutation: dict[str, object]) -> None:
    value = {**OCINetworkConfig.resolve("nat", ["18080:80"]).to_dict(), **mutation}

    with pytest.raises(ArtifactValidationError):
        OCINetworkConfig.from_dict(value)


def test_legacy_absent_or_string_network_state_is_never_decoded_as_nat() -> None:
    for value in (None, "default", "nat", {}, {"mode": "nat"}):
        with pytest.raises(ArtifactValidationError):
            OCINetworkConfig.from_dict(value)


@pytest.mark.parametrize("mode", ["nat", "host-only", "none"])
def test_guest_contract_round_trip_is_run_bound(mode: str) -> None:
    config = OCINetworkConfig.resolve(mode, ["18080:80"] if mode != "none" else [])
    contract = config.guest_contract(RUN_ID)

    assert OCINetworkConfig.from_guest_contract(contract, run_id=RUN_ID).mode == mode
    if mode == "none":
        return
    with pytest.raises(ArtifactValidationError):
        OCINetworkConfig.from_guest_contract(contract, run_id=OTHER_RUN_ID)
