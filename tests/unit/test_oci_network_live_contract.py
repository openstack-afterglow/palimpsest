"""Portable contract for the opt-in native OCI-root networking proof."""

from __future__ import annotations

import ast
from pathlib import Path

from palimpsest_local.oci_network import OCINetworkConfig

PROOF = Path(__file__).resolve().parents[1] / "kvm" / "test_oci_network_live.py"
SOURCE = PROOF.read_text(encoding="utf-8")


def _module() -> ast.Module:
    return ast.parse(SOURCE)


def test_proof_defines_exactly_the_three_selectable_network_nodes() -> None:
    names = [node.name for node in _module().body if isinstance(node, ast.FunctionDef) and node.name.startswith("test")]

    assert names == [
        "test_nat_publishes_inbound_api_and_reaches_an_external_api",
        "test_host_only_publishes_a_service_without_any_egress",
        "test_explicit_wildcard_publish_is_reachable_from_the_host_address",
    ]


def test_every_node_is_opt_in_and_uses_operator_supplied_pinned_archives() -> None:
    assert 'os.environ.get(_PREFIX + "LIVE") != "1"' in SOURCE
    assert 'stem + "IMAGE"' in SOURCE and 'stem + "ARCHIVE_SHA256"' in SOURCE and 'stem + "MANIFEST_SHA256"' in SOURCE
    assert "legacy._file_sha256(archive) == archive_digest" in SOURCE
    # Source archives are never rewritten and are rechecked after each node.
    assert SOURCE.count("== source_hash == selection.archive_digest") == 3


def test_each_node_selects_its_mode_and_publication_explicitly() -> None:
    assert SOURCE.count('"--network",\n                "nat",') == 2
    assert SOURCE.count('"--network",\n                "host-only",') == 1
    assert SOURCE.count('"--publish",') == 3
    assert 'f"127.0.0.1:{host_port}:18080"' in SOURCE
    assert 'f"127.0.0.1:{host_port}:6379"' in SOURCE
    assert 'f"0.0.0.0:{host_port}:8080"' in SOURCE


def test_nodes_assert_libvirt_owns_no_network_resource() -> None:
    assert 'root.findall("./devices/interface") == []' in SOURCE
    assert 'root.findall("./devices/hostdev") == []' in SOURCE
    assert 'root.findall("./devices/filesystem") == []' in SOURCE
    assert "arguments == network.qemu_arguments(run_id)" in SOURCE


def test_inbound_and_outbound_evidence_is_real_traffic_not_a_status_field() -> None:
    assert '_http_json(f"http://127.0.0.1:{host_port}/healthz"' in SOURCE
    assert '_http_json(f"http://127.0.0.1:{host_port}/infer"' in SOURCE
    assert 'stream.sendall(b"*1\\r\\n$4\\r\\nPING\\r\\n")' in SOURCE
    assert '_redis_ping("127.0.0.1", host_port) == b"+PONG\\r\\n"' in SOURCE
    assert 'urllib.request.urlopen(f"http://{lan_address}:{host_port}/"' in SOURCE
    assert "NET_EGRESS_OK" in SOURCE and "https://api.github.com/meta" in SOURCE


def test_host_only_node_requires_proven_absence_of_egress() -> None:
    assert "NET_DNS_REACHED" in SOURCE and "NET_TCP_REACHED" in SOURCE and "NET_HOST_REACHED" in SOURCE
    assert "NET_NO_EGRESS_OK" in SOURCE
    assert 'status["egress"] is False' in SOURCE


def test_isolation_probe_cannot_record_tool_absence_as_isolation() -> None:
    # A missing or unusable tool must fail the node instead of printing the
    # success marker, and each tool is proven against a reachable target first.
    for guard in ("getent", "nc", "ip"):
        assert f"command -v {guard} >/dev/null 2>&1 || {{ echo NET_TOOL_MISSING={guard}; exit 94; }}" in SOURCE
    assert "getent hosts localhost >/dev/null 2>&1 || { echo NET_RESOLVER_UNUSABLE; exit 95; }" in SOURCE
    assert "nc -w 3 -z 127.0.0.1 %(service_port)s >/dev/null 2>&1 || { echo NET_PROBE_UNUSABLE; exit 96; }" in SOURCE
    assert "echo NET_ADDRESS_MISSING; exit 97" in SOURCE
    assert 'assert isolation.stdout.strip() == b"NET_NO_EGRESS_OK"' in SOURCE
    assert 'endswith(b"NET_NO_EGRESS_OK")' not in SOURCE


def test_published_listeners_are_proven_gone_after_owned_removal() -> None:
    assert SOURCE.count("with pytest.raises(OSError):") == 2
    assert SOURCE.count("_cleanup(environment, parent, name, domain_uuid, roots)") == 3
    assert "legacy._assert_domain_absent(environment, name, domain_uuid)" in SOURCE


def test_guest_expectations_come_from_the_product_contract() -> None:
    values = {
        "address": "10.0.2.15",
        "gateway": "10.0.2.2",
        "nameserver": "10.0.2.3",
        "netmask": "255.255.255.0",
    }
    config = OCINetworkConfig.resolve("nat")
    guest = config.guest_contract("f6f546e2-e734-4920-9eff-1762b348a249")

    assert guest["address"] == values["address"]
    assert guest["gateway"] == values["gateway"]
    assert guest["netmask"] == values["netmask"]
    assert guest["nameservers"] == [values["nameserver"]]
    assert 'assert resolver == "nameserver %(nameserver)s' in SOURCE
    assert 'probe.getsockname()[0] == "%(address)s"' in SOURCE


def test_proof_never_mutates_host_networking() -> None:
    for forbidden in ("iptables", "nft", "dnsmasq", "ip route add", "sysctl -w", "net-define", "net-start"):
        assert forbidden not in SOURCE
