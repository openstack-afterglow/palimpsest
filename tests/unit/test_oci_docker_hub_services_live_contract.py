"""Portable contracts for the independent official-service compatibility matrix."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
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
        ("MYSQL_USER", "mysql:8.4", 2048, "mysql"),
        ("MYSQL_USER_RANDOM_PASSWORD", "mysql:8.4", 2048, "mysql"),
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
        ("--user", case.user_override) if case.user_override else ()
    )


def test_mysql_user_is_a_separate_no_injection_copy_of_the_default_case() -> None:
    default = next(case for case in services.CASES if case.key == "MYSQL")
    diagnostic = next(case for case in services.CASES if case.key == "MYSQL_USER")
    assert diagnostic.user_override == "mysql" and default.user_override is None
    assert diagnostic.image == default.image
    assert diagnostic.memory_mib == default.memory_mib
    assert diagnostic.argv_suffix == default.argv_suffix
    assert diagnostic.readiness == default.readiness
    assert diagnostic.version_argv == default.version_argv
    assert diagnostic.version_marker == default.version_marker
    assert diagnostic.probe_argv == default.probe_argv
    assert diagnostic.probe_marker == default.probe_marker
    arguments = services._run_arguments(
        diagnostic,
        SimpleNamespace(archive=Path("/tmp/mysql.oci.tar"), manifest_digest="sha256:" + "a" * 64),
        "proof",
    )
    assert arguments[-2:] == ("--user", "mysql")
    assert "--env" not in arguments and "-e" not in arguments


def test_random_password_derivation_contains_only_fixed_guest_generator_code(tmp_path: Path) -> None:
    config = {"config": {"Entrypoint": ["docker-entrypoint.sh"], "Cmd": ["mysqld"], "Env": ["PATH=/usr/bin"]}}
    config_payload = services._json_bytes(config)
    config_hex = hashlib.sha256(config_payload).hexdigest()
    manifest = {"schemaVersion": 2, "config": {"digest": "sha256:" + config_hex, "size": len(config_payload)}}
    manifest_payload = services._json_bytes(manifest)
    manifest_hex = hashlib.sha256(manifest_payload).hexdigest()
    payloads = {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "index.json": services._json_bytes({"manifests": [{"digest": "sha256:" + manifest_hex, "size": len(manifest_payload)}]}),
        "blobs/sha256/" + config_hex: config_payload,
        "blobs/sha256/" + manifest_hex: manifest_payload,
    }
    source = tmp_path / "source.tar"
    with tarfile.open(source, "w") as archive:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    original = source.read_bytes()
    selected = services.legacy.DockerHubImageSelection(
        source, services.legacy._file_sha256(source), "sha256:" + manifest_hex
    )
    derived = services._derived_mysql_random_password_archive(selected, tmp_path / "derived.tar")
    assert source.read_bytes() == original
    with tarfile.open(derived.archive) as archive:
        index = json.load(archive.extractfile("index.json"))
        derived_manifest = json.load(archive.extractfile("blobs/sha256/" + index["manifests"][0]["digest"][7:]))
        derived_config = json.load(archive.extractfile("blobs/sha256/" + derived_manifest["config"]["digest"][7:]))
    entrypoint = derived_config["config"]["Entrypoint"]
    assert entrypoint[:2] == ["/usr/bin/bash", "-c"] and entrypoint[-1] == "palimpsest-mysql-random"
    assert derived_config["config"]["Cmd"] == ["mysqld"]
    assert "MYSQL_RANDOM_ROOT_PASSWORD" not in entrypoint[2]
    assert "palimpsest-test-${secret_hex}" in entrypoint[2]
    assert re.search(r"palimpsest-test-[0-9a-f]{64}", entrypoint[2]) is None


def test_random_password_wrapper_generates_without_printing_and_unsets_intermediate() -> None:
    validation = (
        "[[ $MYSQL_ROOT_PASSWORD =~ ^palimpsest-test-[0-9a-f]{64}$ ]] && "
        "[[ -z ${secret_hex+x} ]]"
    )
    command = services._MYSQL_RANDOM_WRAPPER.replace(
        'exec /usr/local/bin/docker-entrypoint.sh "$@"', validation
    )
    completed = subprocess.run(["/bin/bash", "-c", command, "test", "mysqld"], capture_output=True, timeout=5)
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == b""


def test_final_mysql_readiness_rejects_temporary_server_and_requires_ordered_final_server() -> None:
    ready = b"ready for connections"
    initialized = b"MySQL init process done"
    assert not services._mysql_final_ready(ready)
    assert not services._mysql_final_ready(ready + initialized)
    assert services._mysql_final_ready(ready + initialized + ready)


def test_secret_output_is_redacted_before_persistence_and_raises_constant_failure(tmp_path: Path) -> None:
    secret = b"palimpsest-test-" + b"a" * 64
    result = subprocess.CompletedProcess(["fixed"], 1, b"before " + secret + b" after", secret)
    with pytest.raises(AssertionError, match="generated password appeared in command output"):
        services._save_service_result(tmp_path, "leak", result, secret_safe=True)
    assert secret not in (tmp_path / "leak.stdout").read_bytes()
    assert secret not in (tmp_path / "leak.stderr").read_bytes()
    assert b"[REDACTED]" in (tmp_path / "leak.stdout").read_bytes()


def test_cleanup_secret_output_is_redacted_without_interrupting_caller(tmp_path: Path) -> None:
    secret = b"palimpsest-test-" + b"b" * 64
    result = subprocess.CompletedProcess(["stop"], 0, secret, b"")
    saved, leaked, error = services._save_cleanup_result(tmp_path, "cleanup", result)
    assert saved.returncode == 0 and leaked is True and error is None
    assert secret not in (tmp_path / "cleanup.stdout").read_bytes()


def test_cleanup_write_failure_does_not_prevent_following_rm(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(services.legacy, "_save", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))

    def command(operation: str):
        calls.append(operation)
        return subprocess.CompletedProcess([operation], 0, b"", b"")

    leaked, errors = services._run_disposal_commands(tmp_path, "owned", "running", command)
    assert calls == ["stop", "rm"]
    assert leaked is False and errors == ("OSError", "OSError")


def test_cleanup_stop_command_failure_still_attempts_public_rm(tmp_path: Path) -> None:
    calls: list[str] = []

    def command(operation: str):
        calls.append(operation)
        if operation == "stop":
            raise AssertionError("constant sanitized timeout")
        return subprocess.CompletedProcess([operation], 1, b"", b"still running")

    leaked, errors = services._run_disposal_commands(tmp_path, "owned", "running", command)
    assert calls == ["stop", "rm"]
    assert leaked is False and errors == ("stop-AssertionError", "rm-AssertionError")


def test_oversized_console_does_not_prevent_stop_and_rm(monkeypatch, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    console = runs / "owned" / "io" / "console.log"
    console.parent.mkdir(parents=True)
    console.write_bytes(b"x" * (services.legacy._MAX_CONSOLE_BYTES + 1))
    monkeypatch.setattr(services, "resolve_roots", lambda environment: SimpleNamespace(runs=runs))
    calls: list[str] = []

    def command(operation: str):
        calls.append(operation)
        return subprocess.CompletedProcess([operation], 0, b"", b"")

    leaked, errors = services._capture_then_dispose(tmp_path, {}, "owned", "running", command)
    assert calls == ["stop", "rm"]
    assert leaked is False and errors == ("AssertionError",)


def test_random_password_cleanup_is_flagged_and_missing_root_directory_is_an_empty_baseline() -> None:
    diagnostic = next(case for case in services.CASES if case.key == "MYSQL_USER_RANDOM_PASSWORD")
    assert diagnostic.test_only_random_password is True
    source = PATH.read_text()
    assert 'if case.test_only_random_password and root_volume_directory.is_dir()' in source
    assert 'remaining == root_volume_before' in source
    assert 'legacy._cli(environment, "failure-rm"' not in source
    assert "_capture_then_dispose(" in source
    assert 'lambda operation: _secret_safe_cli(parent, environment, operation, name' in source
    assert '"application_completed": completed' in source
    assert '"owned_resources_disposed": disposed' in source
    assert 'stream.read(legacy._MAX_CONSOLE_BYTES + 1)' in source
    assert 'cleanup_evidence_errors' in source


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
    assert source.count("if case.user_override is not None:") >= 3
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
