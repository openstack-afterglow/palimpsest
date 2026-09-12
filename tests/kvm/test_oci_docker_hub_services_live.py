"""Opt-in native compatibility matrix for four unchanged official service images."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

from palimpsest_local.oci_run_cleanup import load_oci_run_binding
from palimpsest_local.state import resolve_roots

_LEGACY_PATH = Path(__file__).with_name("test_oci_docker_hub_cli_live.py")
_SPEC = importlib.util.spec_from_file_location("palimpsest_docker_hub_legacy_helpers", _LEGACY_PATH)
assert _SPEC is not None and _SPEC.loader is not None
legacy = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = legacy
_SPEC.loader.exec_module(legacy)

pytestmark = pytest.mark.kvm
_PREFIX = "PALIMPSEST_OCI_DOCKER_HUB_SERVICE_"


@dataclass(frozen=True)
class ServiceCase:
    key: str
    image: str
    memory_mib: int
    argv_suffix: tuple[str, ...]
    readiness: bytes
    version_argv: tuple[str, ...]
    version_marker: bytes
    probe_argv: tuple[str, ...]
    probe_marker: bytes
    user_override: str | None = None


CASES = (
    ServiceCase(
        "POSTGRES",
        "postgres:17",
        2048,
        ("postgres",),
        b"database system is ready to accept connections",
        ("postgres", "--version"),
        b"postgres (PostgreSQL) 17",
        (
            "/bin/sh",
            "-c",
            "command -v psql >/dev/null || exit 77; psql -h /var/run/postgresql -U postgres -d postgres -Atqc 'SELECT 1'",
        ),
        b"1\n",
    ),
    ServiceCase(
        "REDIS",
        "redis:7-alpine",
        512,
        ("redis-server",),
        b"Ready to accept connections",
        ("redis-server", "--version"),
        b"Redis server v=7",
        ("/bin/sh", "-c", "command -v redis-cli >/dev/null || exit 77; redis-cli -h 127.0.0.1 ping"),
        b"PONG\n",
    ),
    ServiceCase(
        "MYSQL",
        "mysql:8.4",
        2048,
        ("mysqld",),
        b"ready for connections",
        ("mysqld", "--version"),
        b"Ver 8.4",
        ("/bin/sh", "-c", "command -v mysqladmin >/dev/null || exit 77; mysqladmin --protocol=socket ping"),
        b"mysqld is alive\n",
    ),
    ServiceCase(
        "NGINX",
        "nginx:stable-alpine",
        512,
        ("nginx", "-g", "daemon off;"),
        b"start worker processes",
        ("nginx", "-v"),
        b"nginx version:",
        ("/bin/sh", "-c", "command -v wget >/dev/null || exit 77; wget -qO- http://127.0.0.1/"),
        b"Welcome to nginx!",
    ),
    ServiceCase(
        "REDIS_USER",
        "redis:7-alpine",
        512,
        ("redis-server",),
        b"Ready to accept connections",
        ("redis-server", "--version"),
        b"Redis server v=7",
        ("/bin/sh", "-c", "command -v redis-cli >/dev/null || exit 77; redis-cli -h 127.0.0.1 ping"),
        b"PONG\n",
        user_override="redis",
    ),
)


def _selection(case: ServiceCase, environment: dict[str, str]):
    stem = _PREFIX + case.key + "_"
    if environment.get(stem + "LIVE") != "1":
        pytest.skip(f"set {stem}LIVE=1 for the independent {case.image} proof")
    translated = dict(environment)
    for suffix in ("IMAGE", "ARCHIVE_SHA256", "MANIFEST_SHA256"):
        value = environment.get(stem + suffix)
        if value is not None:
            translated[f"PALIMPSEST_OCI_DOCKER_HUB_{case.key}_{suffix}"] = value
    translated[f"PALIMPSEST_OCI_DOCKER_HUB_{case.key}_LIVE"] = "1"
    return legacy._selection(case.key, translated)


def _save_json(parent: Path, name: str, value: object) -> None:
    (parent / name).write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _run_arguments(case: ServiceCase, selection, name: str) -> tuple[object, ...]:
    arguments: tuple[object, ...] = (
        "run",
        selection.archive,
        "--manifest",
        selection.manifest_digest,
        "--name",
        name,
        "--memory",
        str(case.memory_mib),
        "--vcpus",
        "1",
        "-d",
    )
    if case.user_override is not None:
        arguments += ("--user", case.user_override)
    return arguments


def _assert_loopback_security(payload: bytes) -> None:
    assert len(payload) <= 64 * 1024
    lines = payload.splitlines()
    counters: dict[bytes, int] = {}
    for key in (b"netdev_count", b"sysfs_count"):
        matches = [line for line in lines if line.startswith(key + b"=")]
        assert len(matches) == 1
        value = matches[0][len(key) + 1 :]
        assert re.fullmatch(rb"[0-9]+", value)
        counters[key] = int(value)

    assert lines.count(b"netdev_begin") == 1 and lines.count(b"netdev_end") == 1
    begin, end = lines.index(b"netdev_begin"), lines.index(b"netdev_end")
    assert begin < end
    netdev_names: list[bytes] = []
    for line in lines[begin + 1 : end]:
        match = re.fullmatch(rb"netdev_interface=([A-Za-z0-9_.-]+)", line)
        assert match is not None
        netdev_names.append(match.group(1))

    records: dict[bytes, tuple[int, int, int]] = {}
    interface_lines = [line for line in lines if line.startswith(b"interface ")]
    for line in interface_lines:
        match = re.fullmatch(
            rb"interface name=([A-Za-z0-9_.-]+) flags=(0x[0-9a-f]+) type=([0-9]+) ifindex=([0-9]+)", line
        )
        assert match is not None
        name = match.group(1)
        assert name not in records
        flags, kind, index = int(match.group(2), 16), int(match.group(3)), int(match.group(4))
        assert index > 0
        records[name] = (flags, kind, index)

    assert len(netdev_names) == len(set(netdev_names)) == counters[b"netdev_count"]
    assert len(records) == counters[b"sysfs_count"]
    assert len({record[2] for record in records.values()}) == len(records)
    assert set(netdev_names) == set(records)
    loopback = records.pop(b"lo", None)
    assert loopback is not None
    assert loopback[:2] in {(0x9, 772), (0x49, 772)}
    allowed_tunnels = {b"tunl0": (0x80, 768), b"ip6tnl0": (0x80, 769)}
    for name, (flags, kind, _index) in records.items():
        assert name in allowed_tunnels
        assert (flags, kind) == allowed_tunnels[name]
    assert len([line for line in lines if line.startswith(b"Uid=")]) == 1
    assert len([line for line in lines if line.startswith(b"Gid=")]) == 1
    uid = re.findall(rb"^Uid=([0-9]+)\s+\1\s+\1\s+\1\s*$", payload, re.M)
    gid = re.findall(rb"^Gid=([0-9]+)\s+\1\s+\1\s+\1\s*$", payload, re.M)
    assert len(uid) == 1 and int(uid[0]) != 0
    assert len(gid) == 1 and int(gid[0]) != 0
    for field in (b"CapInh", b"CapPrm", b"CapEff", b"CapBnd", b"CapAmb"):
        assert len([line for line in lines if line.startswith(field + b"=")]) == 1
        assert len(re.findall(rb"^" + field + rb"=0+\s*$", payload, re.M)) == 1
    assert len([line for line in lines if line.startswith(b"NoNewPrivs=")]) == 1
    assert len([line for line in lines if line.startswith(b"Seccomp=")]) == 1
    assert lines.count(b"NoNewPrivs=1") == 1 and lines.count(b"Seccomp=2") == 1
    ip_tool = [line for line in lines if line.startswith(b"ip_tool=")]
    assert len(ip_tool) == 1 and ip_tool[0] in {b"ip_tool=present", b"ip_tool=absent"}
    if ip_tool[0] == b"ip_tool=present":
        assert re.search(rb"\binet 127\.0\.0\.1/8\b", payload)


def _loopback_security_command(
    netdev_path: str = "/proc/net/dev",
    status_path: str = "/proc/self/status",
    sysfs_net_path: str = "/sys/class/net",
) -> str:
    netdev = shlex.quote(netdev_path)
    status = shlex.quote(status_path)
    sysfs_net = shlex.quote(sysfs_net_path)
    return (
        "netdev_count=0; "
        "while IFS=: read -r iface rest; do [ -n \"$rest\" ] || continue; set -- $iface; dev=${1-}; "
        "netdev_count=$((netdev_count+1)); "
        f"done < {netdev}; printf 'netdev_count=%s\\n' \"$netdev_count\"; "
        "printf 'netdev_begin\\n'; "
        f"while IFS=: read -r iface rest; do [ -n \"$rest\" ] || continue; set -- $iface; printf 'netdev_interface=%s\\n' \"${{1-}}\"; done < {netdev}; "
        "printf 'netdev_end\\n'; "
        "sysfs_count=0; "
        f"for entry in {sysfs_net}/*; do [ -e \"$entry\" ] || continue; name=${{entry##*/}}; "
        "IFS= read -r flags < \"$entry/flags\" || exit 78; IFS= read -r type < \"$entry/type\" || exit 78; "
        "IFS= read -r ifindex < \"$entry/ifindex\" || exit 78; "
        "sysfs_count=$((sysfs_count+1)); printf 'interface name=%s flags=%s type=%s ifindex=%s\\n' \"$name\" \"$flags\" \"$type\" \"$ifindex\"; done; "
        "printf 'sysfs_count=%s\\n' \"$sysfs_count\"; "
        "while read -r key value rest; do case $key in "
        "Uid:|Gid:) printf '%s=%s %s\\n' \"${key%:}\" \"$value\" \"$rest\";; "
        "CapInh:|CapPrm:|CapEff:|CapBnd:|CapAmb:|NoNewPrivs:|Seccomp:) "
        f"printf '%s=%s\\n' \"${{key%:}}\" \"$value\";; esac; done < {status}; "
        "if command -v ip >/dev/null 2>&1; then printf 'ip_tool=present\\n'; ip -4 addr show dev lo; "
        "else printf 'ip_tool=absent\\n'; fi"
    )


def _assert_running_domain_has_no_interface(
    parent: Path, environment: dict[str, str], name: str, expected_uuid: str
) -> None:
    virsh = legacy.shutil.which("virsh", path=environment.get("PATH"))
    assert virsh
    result = legacy._bounded_command(
        [virsh, "-c", "qemu:///system", "dumpxml", name], environment=environment, timeout=15
    )
    assert len(result.stdout) <= 1024 * 1024 and len(result.stderr) <= 1024 * 1024
    result = legacy._save(parent, "running-domain-xml", result)
    legacy._success(result)
    root = ET.fromstring(result.stdout)
    assert root.tag == "domain"
    identifiers = root.findall("./uuid")
    assert len(identifiers) == 1 and identifiers[0].text == expected_uuid
    devices = root.findall("./devices")
    assert len(devices) == 1
    assert devices[0].findall("interface") == []


def _domain_state(environment: dict[str, str], name: str):
    result = legacy._domain_info(environment, name)
    legacy._success(result)
    match = re.search(rb"^State:\s+(.+?)\s*$", result.stdout, re.M | re.I)
    assert match is not None
    return match.group(1).decode("ascii").lower(), legacy._domain_uuid(environment, name)


def _wait_ready_or_inactive(parent: Path, environment: dict[str, str], name: str, marker: bytes) -> None:
    path = parent / "state" / "runs" / name / "io" / "console.log"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if path.is_file():
            with path.open("rb") as source:
                payload = source.read(legacy._MAX_CONSOLE_BYTES + 1)
            assert len(payload) <= legacy._MAX_CONSOLE_BYTES
            if marker in payload:
                return
        state, _ = _domain_state(environment, name)
        if state is not None and state != "running":
            pytest.fail(f"default workload became {state!r} before readiness; preserve {path}")
        time.sleep(0.1)
    pytest.fail(f"missing default workload readiness {marker!r}; preserve {path}")


def _preserve_failed_owned_runtime(
    parent: Path, environment: dict[str, str], name: str, expected_uuid: str | None
) -> None:
    run_path = resolve_roots(environment).runs / name
    if run_path.exists():
        binding = load_oci_run_binding(resolve_roots(environment), name)
        assert binding.record.name == name
        if expected_uuid is None:
            expected_uuid = binding.domain_uuid
        else:
            assert expected_uuid == binding.domain_uuid
    virsh = legacy.shutil.which("virsh", path=environment.get("PATH"))
    assert virsh
    names_result = legacy._save(
        parent,
        "failure-domain-names",
        legacy._bounded_command(
            [virsh, "-c", "qemu:///system", "list", "--all", "--name"], environment=environment, timeout=15
        ),
    )
    uuids_result = legacy._save(
        parent,
        "failure-domain-uuids",
        legacy._bounded_command(
            [virsh, "-c", "qemu:///system", "list", "--all", "--uuid"], environment=environment, timeout=15
        ),
    )
    legacy._success(names_result)
    legacy._success(uuids_result)
    names = legacy._inventory_lines(names_result.stdout, encoding="utf-8")
    identifiers = legacy._uuid_inventory(uuids_result.stdout)
    if name in names:
        state, observed_uuid = _domain_state(environment, name)
    else:
        state, observed_uuid = None, None
        if expected_uuid is not None:
            assert expected_uuid not in identifiers
    if observed_uuid is not None:
        assert expected_uuid is not None and observed_uuid == expected_uuid
    if state == "running":
        assert observed_uuid is not None and observed_uuid == expected_uuid
        stopped = legacy._save(parent, "failure-stop", legacy._cli(environment, "stop", name, timeout=90))
        legacy._success(stopped)
        state, after_uuid = _domain_state(environment, name)
        assert state == "shut off" and after_uuid == observed_uuid
    if state is not None:
        assert state == "shut off"
    _save_json(
        parent,
        "retained-failure.json",
        {"domain_state": state, "domain_uuid": observed_uuid, "name": name, "runtime_preserved": True},
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key.lower())
def test_official_service_default_process_compatibility(case: ServiceCase) -> None:
    selection = _selection(case, os.environ)
    short = {"POSTGRES": "pg", "REDIS": "rd", "MYSQL": "my", "NGINX": "ng", "REDIS_USER": "rdu"}[case.key]
    parent, environment = legacy._setup(legacy._environment(), "svc-" + short)
    name = "hub-service-" + case.key.lower().replace("_", "-") + "-" + uuid.uuid4().hex[:8]
    source_hash = legacy._file_sha256(selection.archive)
    domain_uuid = None
    completed = False
    primary_error: BaseException | None = None
    loopback_security_error: AssertionError | None = None
    try:
        process = legacy._authenticate(selection, parent)
        process.require_bootable()
        assert process.argv[-len(case.argv_suffix) :] == case.argv_suffix
        _save_json(
            parent,
            "case.json",
            {
                "expected_image": case.image,
                "memory_mib": case.memory_mib,
                "name": name,
                "user_override": case.user_override,
                "vcpus": 1,
            },
        )
        _save_json(parent, "authenticated-process.json", process.to_dict())
        launched = legacy._save(
            parent, "run", legacy._cli(environment, *_run_arguments(case, selection, name), timeout=240)
        )
        _save_json(
            parent,
            "run-result.json",
            {
                "returncode": launched.returncode,
                "stderr_bytes": len(launched.stderr),
                "stdout_bytes": len(launched.stdout),
            },
        )
        legacy._success(launched)
        assert launched.stdout == (name + "\n").encode()
        effective_process = legacy._effective_ledger_process(
            environment, name, image_process=process, user_override=case.user_override
        )
        _save_json(parent, "effective-ledger-process.json", effective_process.to_dict())
        binding = load_oci_run_binding(resolve_roots(environment), name)
        assert binding.record.name == name
        domain_uuid = binding.domain_uuid
        _wait_ready_or_inactive(parent, environment, name, case.readiness)
        state, observed_uuid = _domain_state(environment, name)
        assert state == "running" and observed_uuid == domain_uuid
        _assert_running_domain_has_no_interface(parent, environment, name, domain_uuid)
        before = legacy._root_proof(environment, name)
        _save_json(parent, "root-proof-before.json", before)
        assert before["domain"]["uuid"] == domain_uuid
        version = legacy._save(
            parent, "version", legacy._cli(environment, "exec", name, "--", *case.version_argv, timeout=60)
        )
        legacy._success(version)
        assert case.version_marker in version.stdout + version.stderr
        if case.key == "REDIS_USER":
            loopback = legacy._save(
                parent,
                "guest-loopback-security",
                legacy._cli(
                    environment,
                    "exec",
                    name,
                    "--",
                    "/bin/sh",
                    "-c",
                    _loopback_security_command(),
                    timeout=60,
                ),
            )
            legacy._success(loopback)
            try:
                _assert_loopback_security(loopback.stdout)
            except AssertionError as exc:
                loopback_security_error = exc
        probe = legacy._save(
            parent, "service-probe", legacy._cli(environment, "exec", name, "--", *case.probe_argv, timeout=60)
        )
        probe_ok = probe.returncode == 0 and case.probe_marker in probe.stdout
        if case.key == "REDIS_USER":
            probe_ok = probe.returncode == 0 and probe.stdout == case.probe_marker
        if probe.returncode == 77:
            _save_json(
                parent, "service-probe.json", {"result": "skipped", "reason": "image client absent", "returncode": 77}
            )
        else:
            _save_json(
                parent,
                "service-probe.json",
                {
                    "result": "passed" if probe_ok else "failed",
                    "returncode": probe.returncode,
                    "transport": "guest loopback-or-unix",
                },
            )
        root = legacy._save(
            parent,
            "root",
            legacy._cli(
                environment, "exec", name, "--", "/bin/sh", "-c", "stat -c '%d %i' /; cat /etc/os-release", timeout=60
            ),
        )
        legacy._success(root)
        lines = root.stdout.decode("utf-8").splitlines()
        device, inode = (int(value) for value in lines[0].split())
        assert any(line.startswith("ID=") for line in lines[1:])
        denied = legacy._save(
            parent,
            "pid1-refusal",
            legacy._cli(
                environment, "exec", name, "--", "/bin/sh", "-c", "LC_ALL=C cat /proc/1/root/etc/os-release", timeout=60
            ),
        )
        assert denied.returncode != 0 and denied.stdout == b"" and b"Permission denied" in denied.stderr
        after = legacy._root_proof(environment, name)
        _save_json(parent, "root-proof-after.json", after)
        assert {key: before[key] for key in ("run", "boot", "domain")} == {
            key: after[key] for key in ("run", "boot", "domain")
        }
        assert before["root_identity"] == after["root_identity"]
        assert (device, inode) == (after["root_identity"]["device"], after["root_identity"]["inode"])
        assert loopback_security_error is None, (
            "guest loopback-only security receipt did not pass; service/root/PID1 evidence was retained"
        )
        assert probe_ok, "official image service probe did not pass; root/PID1 evidence was retained"
        legacy._success(legacy._save(parent, "stop", legacy._cli(environment, "stop", name, timeout=90)))
        legacy._success(legacy._save(parent, "rm", legacy._cli(environment, "rm", name, timeout=90)))
        legacy._assert_domain_absent(environment, name, domain_uuid)
        assert not (parent / "state" / "runs" / name).exists()
        assert legacy._file_sha256(selection.archive) == source_hash == selection.archive_digest
        completed = True
    except BaseException as exc:
        primary_error = exc
        try:
            _preserve_failed_owned_runtime(parent, environment, name, domain_uuid)
        except BaseException as cleanup_exc:
            exc.add_note(f"failure preservation also failed: {cleanup_exc!r}")
        raise
    finally:
        try:
            assert legacy._record_source_hashes(parent, source_hash, selection.archive) == source_hash
            _save_json(parent, "completion.json", {"successful_owned_cleanup": completed})
        except BaseException as evidence_exc:
            if primary_error is None:
                raise
            primary_error.add_note(f"final evidence recording also failed: {evidence_exc!r}")
