"""Portable contracts for the independent official-service compatibility matrix."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "kvm" / "test_oci_docker_hub_services_live.py"
SPEC = importlib.util.spec_from_file_location("docker_hub_services_live_contract_target", PATH)
assert SPEC is not None and SPEC.loader is not None
services = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = services
SPEC.loader.exec_module(services)


def _environment(tmp_path: Path, key: str):
    archive = tmp_path / f"{key.lower()}.oci.tar"
    archive.write_bytes(b"official image")
    stem = f"PALIMPSEST_OCI_DOCKER_HUB_SERVICE_{key}_"
    return archive, {
        stem + "LIVE": "1",
        stem + "IMAGE": str(archive),
        stem + "ARCHIVE_SHA256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
        stem + "MANIFEST_SHA256": "sha256:" + "a" * 64,
    }


def test_matrix_pins_exact_images_resources_and_separate_redis_user() -> None:
    assert [(case.key, case.image, case.memory_mib, case.user_override) for case in services.CASES] == [
        ("POSTGRES", "postgres:17", 2048, None),
        ("REDIS", "redis:7-alpine", 512, None),
        ("MYSQL", "mysql:8.4", 2048, None),
        ("NGINX", "nginx:stable-alpine", 512, None),
        ("REDIS_USER", "redis:7-alpine", 512, "redis"),
    ]


@pytest.mark.parametrize("case", services.CASES, ids=lambda case: case.key.lower())
def test_each_case_has_independent_opt_in_and_exact_run_arguments(tmp_path: Path, case) -> None:
    with pytest.raises(pytest.skip.Exception):
        services._selection(case, {})
    archive, environment = _environment(tmp_path, case.key)
    selected = services._selection(case, environment)
    arguments = services._run_arguments(case, selected, "proof")
    assert arguments[:7] == (
        "run",
        archive.resolve(),
        "--manifest",
        "sha256:" + "a" * 64,
        "--name",
        "proof",
        "--memory",
    )
    assert arguments[7:] == (str(case.memory_mib), "--vcpus", "1", "-d") + (
        ("--user", "redis") if case.user_override else ()
    )


def test_matrix_uses_public_cli_and_preserves_failed_runtime_without_rm_or_hypervisor_force() -> None:
    source = PATH.read_text(encoding="utf-8")
    assert 'legacy._cli(environment, "stop", name' in source
    failure = source[source.index("def _preserve_failed_owned_runtime") : source.index("@pytest.mark.parametrize")]
    assert '"rm"' not in failure and "destroy" not in failure and "undefine" not in failure
    assert "docker run" not in source and "docker exec" not in source
    assert '"retained-failure.json"' in source and '"run-result.json"' in source


def test_service_probes_are_guest_internal_and_missing_client_is_not_a_pass() -> None:
    probes = {case.key: case.probe_argv for case in services.CASES}
    assert "127.0.0.1" in " ".join(probes["REDIS"])
    assert "/var/run/postgresql" in " ".join(probes["POSTGRES"])
    assert "--protocol=socket" in " ".join(probes["MYSQL"])
    assert "127.0.0.1" in " ".join(probes["NGINX"])
    source = PATH.read_text(encoding="utf-8")
    assert '"result": "skipped", "reason": "image client absent", "returncode": 77' in source
    assert '"result": "passed" if probe_ok else "failed"' in source
    assert "assert probe_ok" in source
    assert 'if case.key == "REDIS_USER"' in source
    assert '"guest-loopback-security"' in source
    assert 'netdev_path: str = "/proc/net/dev"' in source and "netdev_count" in source
    assert 'sysfs_net_path: str = "/sys/class/net"' in source
    assert "netdev_begin" in source and "interface name=%s flags=%s type=%s ifindex=%s" in source
    assert 'status_path: str = "/proc/self/status"' in source
    assert "Uid:|Gid:) printf" in source
    assert "CapInh:|CapPrm:|CapEff:|CapBnd:|CapAmb:|NoNewPrivs:|Seccomp:" in source
    assert r"\binet 127\.0\.0\.1/8\b" in source
    assert 'probe.stdout == case.probe_marker' in source


def test_loopback_shell_probe_skips_both_headers_and_accepts_allowed_subset(tmp_path: Path) -> None:
    netdev = tmp_path / "net-dev"
    status = tmp_path / "status"
    netdev.write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        "    lo: 10 1 0 0 0 0 0 0 10 1 0 0 0 0 0 0\n"
        " tunl0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
        encoding="ascii",
    )
    status.write_text(
        "Uid:\t999\t999\t999\t999\nGid:\t1000\t1000\t1000\t1000\n"
        "CapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n"
        "CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\nCapAmb:\t0000000000000000\n"
        "NoNewPrivs:\t1\nSeccomp:\t2\n",
        encoding="ascii",
    )
    sysfs_net = tmp_path / "sys-class-net"
    interface = sysfs_net / "lo"
    interface.mkdir(parents=True)
    (interface / "flags").write_text("0x49\n", encoding="ascii")
    (interface / "type").write_text("772\n", encoding="ascii")
    (interface / "ifindex").write_text("1\n", encoding="ascii")
    tunnel = sysfs_net / "tunl0"
    tunnel.mkdir()
    (tunnel / "flags").write_text("0x80\n", encoding="ascii")
    (tunnel / "type").write_text("768\n", encoding="ascii")
    (tunnel / "ifindex").write_text("2\n", encoding="ascii")
    result = subprocess.run(
        ["/bin/sh", "-c", services._loopback_security_command(str(netdev), str(status), str(sysfs_net))],
        env={"PATH": ""},
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert result.stderr == b""
    assert b"netdev_begin\n" in result.stdout and b"netdev_end\n" in result.stdout
    assert b"netdev_count=2\n" in result.stdout and b"sysfs_count=2\n" in result.stdout
    services._assert_loopback_security(result.stdout)


def _receipt(*interfaces: tuple[str, str, int, int], netdev_names: tuple[str, ...] | None = None) -> bytes:
    names = netdev_names or tuple(interface[0] for interface in interfaces)
    return (
        f"netdev_count={len(names)}\nnetdev_begin\n".encode()
        + b"".join(f"netdev_interface={name}\n".encode() for name in names)
        + b"netdev_end\n"
        + b"".join(
            f"interface name={name} flags={flags} type={kind} ifindex={index}\n".encode()
            for name, flags, kind, index in interfaces
        )
        + f"sysfs_count={len(interfaces)}\n".encode()
        + b"Uid=999 999 999 999\nGid=1000 1000 1000 1000\n"
        b"CapInh=0000000000000000\nCapPrm=0000000000000000\n"
        b"CapEff=0000000000000000\nCapBnd=0000000000000000\nCapAmb=0000000000000000\n"
        b"NoNewPrivs=1\nSeccomp=2\nip_tool=present\n1: lo    inet 127.0.0.1/8 scope host lo\n"
    )


@pytest.mark.parametrize(
    "interfaces",
    [
        (("lo", "0x9", 772, 1),),
        (("lo", "0x49", 772, 1), ("tunl0", "0x80", 768, 2)),
        (("lo", "0x49", 772, 1), ("ip6tnl0", "0x80", 769, 3)),
        (("lo", "0x49", 772, 1), ("tunl0", "0x80", 768, 2), ("ip6tnl0", "0x80", 769, 3)),
    ],
)
def test_loopback_security_receipt_parser_accepts_exact_allowed_subsets(interfaces) -> None:
    services._assert_loopback_security(_receipt(*interfaces))


@pytest.mark.parametrize("changed", [b"CapEff=1", b"NoNewPrivs=0", b"inet 127.0.0.2/8"])
def test_loopback_security_receipt_parser_rejects_drift(changed: bytes) -> None:
    sample = _receipt(("lo", "0x49", 772, 1))
    originals = {
        b"CapEff=1": b"CapEff=0000000000000000",
        b"NoNewPrivs=0": b"NoNewPrivs=1",
        b"inet 127.0.0.2/8": b"inet 127.0.0.1/8",
    }
    with pytest.raises(AssertionError):
        services._assert_loopback_security(sample.replace(originals[changed], changed))


@pytest.mark.parametrize(
    "contradiction",
    [b"Uid=0 0 0 0\n", b"CapEff=1\n", b"NoNewPrivs=0\n", b"Seccomp=0\n", b"ip_tool=absent\n"],
)
def test_loopback_security_receipt_rejects_contradictory_duplicate_security_fields(contradiction: bytes) -> None:
    with pytest.raises(AssertionError):
        services._assert_loopback_security(_receipt(("lo", "0x49", 772, 1)) + contradiction)


@pytest.mark.parametrize(
    "interfaces,names",
    [
        ((("lo", "0x49", 772, 1), ("eth0", "0x1003", 1, 2)), None),
        ((("lo", "0x49", 772, 1), ("tunl0", "0x81", 768, 2)), None),
        ((("lo", "0x49", 772, 1), ("tunl0", "0x80", 769, 2)), None),
        ((("lo", "0x49", 772, 1), ("renamed", "0x80", 768, 2)), None),
        ((("lo", "0x49", 772, 0),), None),
        ((("lo", "0x49", 772, 1), ("tunl0", "0x80", 768, 1)), None),
        ((("lo", "0x49", 772, 1),), ("lo", "tunl0")),
        ((("lo", "0x49", 772, 1),), ("lo", "lo")),
        ((("lo", "0x49", 772, 1), ("lo", "0x49", 772, 2)), None),
    ],
)
def test_loopback_security_receipt_rejects_interface_drift(interfaces, names) -> None:
    with pytest.raises(AssertionError):
        services._assert_loopback_security(_receipt(*interfaces, netdev_names=names))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.replace(b"interface name=lo", b"interface malformed=lo"),
        lambda value: value.replace(b"interface name=lo flags=0x49 type=772 ifindex=1\n", b""),
        lambda value: value.replace(b"sysfs_count=1", b"sysfs_count=2"),
    ],
)
def test_loopback_security_receipt_rejects_malformed_missing_or_count_mismatch(mutation) -> None:
    with pytest.raises(AssertionError):
        services._assert_loopback_security(mutation(_receipt(("lo", "0x49", 772, 1))))


def test_running_domain_xml_requires_one_devices_container_and_no_interface(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(services.legacy.shutil, "which", lambda *args, **kwargs: "/usr/bin/virsh")
    monkeypatch.setattr(
        services.legacy,
        "_bounded_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, b"<domain><uuid>expected</uuid><devices><disk/></devices></domain>", b""
        ),
    )
    services._assert_running_domain_has_no_interface(tmp_path, {}, "proof", "expected")
    assert (tmp_path / "running-domain-xml.stdout").is_file()


@pytest.mark.parametrize(
    "xml",
    [
        b"<domain><uuid>expected</uuid><devices><interface type='network'/></devices></domain>",
        b"<domain/>",
        b"<domain><devices/></domain><extra/>",
        b"<domain><uuid>wrong</uuid><devices/></domain>",
    ],
)
def test_running_domain_xml_rejects_interface_missing_devices_or_malformed(monkeypatch, tmp_path: Path, xml: bytes) -> None:
    monkeypatch.setattr(services.legacy.shutil, "which", lambda *args, **kwargs: "/usr/bin/virsh")
    monkeypatch.setattr(
        services.legacy, "_bounded_command", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, xml, b"")
    )
    with pytest.raises((AssertionError, services.ET.ParseError)):
        services._assert_running_domain_has_no_interface(tmp_path, {}, "proof", "expected")


def _install_retention_fakes(
    monkeypatch, tmp_path: Path, *, state="shut off", observed_uuid="00000000-0000-0000-0000-000000000001"
):
    name = "owned"
    runs = tmp_path / "runs"
    (runs / name).mkdir(parents=True)
    monkeypatch.setattr(services, "resolve_roots", lambda environment: SimpleNamespace(runs=runs))
    binding = SimpleNamespace(record=SimpleNamespace(name=name), domain_uuid="00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(services, "load_oci_run_binding", lambda roots, selected: binding)
    results = iter(
        (
            subprocess.CompletedProcess([], 0, b"owned\n", b""),
            subprocess.CompletedProcess([], 0, (observed_uuid + "\n").encode(), b""),
        )
    )
    monkeypatch.setattr(services.legacy.shutil, "which", lambda *args, **kwargs: "/usr/bin/virsh")
    monkeypatch.setattr(services.legacy, "_bounded_command", lambda *args, **kwargs: next(results))
    monkeypatch.setattr(services, "_domain_state", lambda environment, selected: (state, observed_uuid))
    return name


def test_inactive_failure_is_retained_without_cleanup(monkeypatch, tmp_path: Path) -> None:
    name = _install_retention_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(services.legacy, "_cli", lambda *args, **kwargs: pytest.fail("must not stop inactive domain"))
    services._preserve_failed_owned_runtime(tmp_path, {}, name, None)
    retained = (tmp_path / "retained-failure.json").read_text()
    assert '"domain_state": "shut off"' in retained and '"runtime_preserved": true' in retained


def test_failure_retention_rejects_name_match_with_wrong_uuid(monkeypatch, tmp_path: Path) -> None:
    name = _install_retention_fakes(monkeypatch, tmp_path, observed_uuid="00000000-0000-0000-0000-000000000002")
    with pytest.raises(AssertionError):
        services._preserve_failed_owned_runtime(tmp_path, {}, name, None)


def test_failure_retention_rejects_inventory_query_error(monkeypatch, tmp_path: Path) -> None:
    name = _install_retention_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        services.legacy, "_bounded_command", lambda *args, **kwargs: subprocess.CompletedProcess([], 1, b"", b"failed")
    )
    with pytest.raises(AssertionError):
        services._preserve_failed_owned_runtime(tmp_path, {}, name, None)


def test_running_failure_requires_successful_public_stop(monkeypatch, tmp_path: Path) -> None:
    name = _install_retention_fakes(monkeypatch, tmp_path, state="running")
    monkeypatch.setattr(
        services.legacy, "_cli", lambda *args, **kwargs: subprocess.CompletedProcess([], 1, b"", b"stop failed")
    )
    with pytest.raises(AssertionError):
        services._preserve_failed_owned_runtime(tmp_path, {}, name, None)


def test_unproven_paused_failure_is_not_reported_quiescent(monkeypatch, tmp_path: Path) -> None:
    name = _install_retention_fakes(monkeypatch, tmp_path, state="paused")
    with pytest.raises(AssertionError):
        services._preserve_failed_owned_runtime(tmp_path, {}, name, None)
