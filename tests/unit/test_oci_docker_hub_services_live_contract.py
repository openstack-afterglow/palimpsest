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
    assert 'netdev_path: str = "/proc/net/dev"' in source and "extra_interfaces=0" in source
    assert 'status_path: str = "/proc/self/status"' in source
    assert "Uid:|Gid:) printf" in source
    assert "CapInh:|CapPrm:|CapEff:|CapBnd:|CapAmb:|NoNewPrivs:|Seccomp:" in source
    assert r"\binet 127\.0\.0\.1/8\b" in source
    assert 'probe.stdout == case.probe_marker' in source


def test_loopback_shell_probe_skips_both_headers_and_counts_only_lo(tmp_path: Path) -> None:
    netdev = tmp_path / "net-dev"
    status = tmp_path / "status"
    netdev.write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        "    lo: 10 1 0 0 0 0 0 0 10 1 0 0 0 0 0 0\n",
        encoding="ascii",
    )
    status.write_text(
        "Uid:\t999\t999\t999\t999\nGid:\t1000\t1000\t1000\t1000\n"
        "CapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n"
        "CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\nCapAmb:\t0000000000000000\n"
        "NoNewPrivs:\t1\nSeccomp:\t2\n",
        encoding="ascii",
    )
    result = subprocess.run(
        ["/bin/sh", "-c", services._loopback_security_command(str(netdev), str(status))],
        env={"PATH": ""},
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert result.stderr == b""
    services._assert_loopback_security(result.stdout)


def test_loopback_security_receipt_parser_accepts_exact_bounded_sample() -> None:
    services._assert_loopback_security(
        b"loopback_count=1\nextra_interfaces=0\n"
        b"Uid=999 999 999 999\nGid=1000 1000 1000 1000\n"
        b"CapInh=0000000000000000\nCapPrm=0000000000000000\n"
        b"CapEff=0000000000000000\nCapBnd=0000000000000000\nCapAmb=0000000000000000\n"
        b"NoNewPrivs=1\nSeccomp=2\nip_tool=present\n1: lo    inet 127.0.0.1/8 scope host lo\n"
    )


@pytest.mark.parametrize("changed", [b"extra_interfaces=1", b"CapEff=1", b"NoNewPrivs=0", b"inet 127.0.0.2/8"])
def test_loopback_security_receipt_parser_rejects_drift(changed: bytes) -> None:
    sample = (
        b"loopback_count=1\nextra_interfaces=0\n"
        b"Uid=999 999 999 999\nGid=1000 1000 1000 1000\n"
        b"CapInh=0\nCapPrm=0\nCapEff=0\nCapBnd=0\nCapAmb=0\n"
        b"NoNewPrivs=1\nSeccomp=2\nip_tool=present\ninet 127.0.0.1/8\n"
    )
    originals = {
        b"extra_interfaces=1": b"extra_interfaces=0",
        b"CapEff=1": b"CapEff=0",
        b"NoNewPrivs=0": b"NoNewPrivs=1",
        b"inet 127.0.0.2/8": b"inet 127.0.0.1/8",
    }
    with pytest.raises(AssertionError):
        services._assert_loopback_security(sample.replace(originals[changed], changed))


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
