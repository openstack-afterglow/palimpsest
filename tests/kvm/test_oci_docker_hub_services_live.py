"""Opt-in native compatibility matrix for four unchanged official service images."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
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
    test_only_random_password: bool = False


_MYSQL_RANDOM_WRAPPER = """set -euo pipefail
set +x
umask 077
secret_hex=$(/usr/bin/od -An -N32 -tx1 /dev/urandom | /usr/bin/tr -d ' \\n')
[[ $secret_hex =~ ^[0-9a-f]{64}$ ]]
export MYSQL_ROOT_PASSWORD="palimpsest-test-${secret_hex}"
unset secret_hex
exec /usr/local/bin/docker-entrypoint.sh "$@"
"""


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
    ServiceCase(
        "MYSQL_USER",
        "mysql:8.4",
        2048,
        ("mysqld",),
        b"ready for connections",
        ("mysqld", "--version"),
        b"Ver 8.4",
        ("/bin/sh", "-c", "command -v mysqladmin >/dev/null || exit 77; mysqladmin --protocol=socket ping"),
        b"mysqld is alive\n",
        user_override="mysql",
    ),
    ServiceCase(
        "MYSQL_USER_RANDOM_PASSWORD", "mysql:8.4", 2048, ("mysqld",),
        b"ready for connections", ("mysqld", "--version"), b"Ver 8.4",
        ("/bin/sh", "-c", "command -v mysqladmin >/dev/null || exit 77; mysqladmin --protocol=socket ping"),
        b"mysqld is alive\n", user_override="mysql", test_only_random_password=True,
    ),
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _derived_mysql_random_password_archive(selection, destination: Path):
    assert selection.archive.stat().st_size <= 512 * 1024 * 1024
    with tarfile.open(selection.archive, "r:*") as source:
        members = source.getmembers()
        payloads = {member.name: source.extractfile(member).read() for member in members if member.isfile()}
    manifest_name = "blobs/sha256/" + selection.manifest_digest.removeprefix("sha256:")
    manifest = json.loads(payloads[manifest_name])
    config_name = "blobs/sha256/" + manifest["config"]["digest"].removeprefix("sha256:")
    config = json.loads(payloads[config_name])
    assert config["config"]["Entrypoint"] == ["docker-entrypoint.sh"]
    assert config["config"]["Cmd"] == ["mysqld"]
    forbidden = {"MYSQL_ROOT_PASSWORD", "MYSQL_ALLOW_EMPTY_PASSWORD", "MYSQL_RANDOM_ROOT_PASSWORD"}
    assert not any(item.split("=", 1)[0] in forbidden for item in config["config"].get("Env", []))
    config["config"]["Entrypoint"] = ["/usr/bin/bash", "-c", _MYSQL_RANDOM_WRAPPER, "palimpsest-mysql-random"]
    config_payload = _json_bytes(config)
    config_hex = hashlib.sha256(config_payload).hexdigest()
    manifest["config"] = {**manifest["config"], "digest": "sha256:" + config_hex, "size": len(config_payload)}
    manifest_payload = _json_bytes(manifest)
    manifest_hex = hashlib.sha256(manifest_payload).hexdigest()
    index = json.loads(payloads["index.json"])
    selected = [item for item in index["manifests"] if item["digest"] == selection.manifest_digest]
    assert len(selected) == 1
    selected[0].update(digest="sha256:" + manifest_hex, size=len(manifest_payload))
    payloads["index.json"] = _json_bytes(index)
    payloads["blobs/sha256/" + config_hex] = config_payload
    payloads["blobs/sha256/" + manifest_hex] = manifest_payload
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as output:
        for name in sorted(payloads):
            info = tarfile.TarInfo(name)
            info.mode, info.size = 0o644, len(payloads[name])
            output.addfile(info, io.BytesIO(payloads[name]))
    digest = legacy._file_sha256(destination)
    return type(selection)(destination.resolve(), digest, "sha256:" + manifest_hex)


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


def _wait_mysql_final_ready(parent: Path, environment: dict[str, str], name: str) -> None:
    path = parent / "state" / "runs" / name / "io" / "console.log"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if path.is_file():
            with path.open("rb") as stream:
                payload = stream.read(legacy._MAX_CONSOLE_BYTES + 1)
            assert len(payload) <= legacy._MAX_CONSOLE_BYTES
            assert re.search(rb"palimpsest-test-[0-9a-f]{64}", payload) is None, (
                "generated password appeared on the guest console"
            )
            if _mysql_final_ready(payload):
                return
        state, _ = _domain_state(environment, name)
        if state is not None and state != "running":
            pytest.fail(f"random-password workload became {state!r} before final readiness; preserve {path}")
        time.sleep(0.1)
    pytest.fail(f"final MySQL readiness was not observed; preserve {path}")


def _mysql_final_ready(payload: bytes) -> bool:
    initialized, ready = b"MySQL init process done", b"ready for connections"
    boundary = payload.find(initialized)
    return boundary >= 0 and payload.find(ready, boundary + len(initialized)) >= 0


def _retain_redacted_console(parent: Path, environment: dict[str, str], name: str) -> bool:
    console = resolve_roots(environment).runs / name / "io" / "console.log"
    if not console.is_file():
        return False
    with console.open("rb") as stream:
        payload = stream.read(legacy._MAX_CONSOLE_BYTES + 1)
    assert len(payload) <= legacy._MAX_CONSOLE_BYTES
    leaked = re.search(rb"palimpsest-test-[0-9a-f]{64}", payload) is not None
    (parent / "random-password-console.redacted").write_bytes(
        re.sub(rb"palimpsest-test-[0-9a-f]{64}", b"[REDACTED]", payload)
    )
    return leaked


def _assert_no_password_leak(parent: Path) -> None:
    for path in parent.iterdir():
        if not path.is_file() or path.suffix not in {".json", ".stdout", ".stderr", ".redacted"}:
            continue
        with path.open("rb") as stream:
            payload = stream.read(legacy._MAX_COMMAND_OUTPUT + 1)
        assert len(payload) <= legacy._MAX_COMMAND_OUTPUT
        assert re.search(rb"palimpsest-test-[0-9a-f]{64}", payload) is None, "generated password in evidence"


def _save_service_result(parent: Path, name: str, result, *, secret_safe: bool):
    if secret_safe and re.search(rb"palimpsest-test-[0-9a-f]{64}", result.stdout + result.stderr):
        pattern = rb"palimpsest-test-[0-9a-f]{64}"
        redacted = type(result)(result.args, result.returncode, re.sub(pattern, b"[REDACTED]", result.stdout),
                                re.sub(pattern, b"[REDACTED]", result.stderr))
        legacy._save(parent, name, redacted)
        raise AssertionError("generated password appeared in command output")
    return legacy._save(parent, name, result)


def _service_probe_ok(case: ServiceCase, probe: subprocess.CompletedProcess[bytes]) -> bool:
    if case.test_only_random_password:
        # mysqladmin documents that ping succeeds when the server is reachable
        # even if authentication is denied. This passwordless probe deliberately
        # proves Unix-socket reachability only.
        return probe.returncode == 0
    if case.user_override is not None:
        return probe.returncode == 0 and probe.stdout == case.probe_marker
    return probe.returncode == 0 and case.probe_marker in probe.stdout


def _save_cleanup_result(parent: Path, name: str, result) -> tuple[object, bool, str | None]:
    pattern = rb"palimpsest-test-[0-9a-f]{64}"
    leaked = re.search(pattern, result.stdout + result.stderr) is not None
    redacted = type(result)(result.args, result.returncode, re.sub(pattern, b"[REDACTED]", result.stdout),
                            re.sub(pattern, b"[REDACTED]", result.stderr))
    try:
        saved = legacy._save(parent, name, redacted)
    except OSError as exc:
        return redacted, leaked, type(exc).__name__
    return saved, leaked, None


def _run_disposal_commands(parent: Path, name: str, state: str, command) -> tuple[bool, tuple[str, ...]]:
    leaked = False
    evidence_errors: list[str] = []
    if state == "running":
        try:
            stopped, stop_leak, stop_error = _save_cleanup_result(parent, "failure-stop", command("stop"))
            leaked = leaked or stop_leak
            if stop_error:
                evidence_errors.append(stop_error)
            legacy._success(stopped)
        except (AssertionError, OSError) as exc:
            evidence_errors.append("stop-" + type(exc).__name__)
    try:
        removed, rm_leak, rm_error = _save_cleanup_result(parent, "failure-rm", command("rm"))
        leaked = leaked or rm_leak
        if rm_error:
            evidence_errors.append(rm_error)
        legacy._success(removed)
    except (AssertionError, OSError) as exc:
        evidence_errors.append("rm-" + type(exc).__name__)
    return leaked, tuple(evidence_errors)


def _capture_then_dispose(
    parent: Path, environment: dict[str, str], name: str, state: str, command
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    try:
        leaked = _retain_redacted_console(parent, environment, name)
    except (OSError, AssertionError) as exc:
        leaked = False
        errors.append(type(exc).__name__)
    command_leak, command_errors = _run_disposal_commands(parent, name, state, command)
    return leaked or command_leak, tuple(errors) + command_errors


def _secret_safe_cli(parent: Path, environment: dict[str, str], *args: object, timeout: int):
    try:
        return legacy._cli(environment, *args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        pattern = rb"palimpsest-test-[0-9a-f]{64}"
        redacted = subprocess.CompletedProcess(
            tuple(map(str, args)), 124, re.sub(pattern, b"[REDACTED]", stdout),
            re.sub(pattern, b"[REDACTED]", stderr),
        )
        legacy._save(parent, "timeout", redacted)
        raise AssertionError("secret-safe owned command timed out") from None


def _case_cli(case: ServiceCase, parent: Path, environment: dict[str, str], *args: object, timeout: int):
    if case.test_only_random_password:
        return _secret_safe_cli(parent, environment, *args, timeout=timeout)
    return legacy._cli(environment, *args, timeout=timeout)


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
    short = {
        "POSTGRES": "pg", "REDIS": "rd", "MYSQL": "my", "NGINX": "ng",
        "REDIS_USER": "rdu", "MYSQL_USER": "myu", "MYSQL_USER_RANDOM_PASSWORD": "myr",
    }[case.key]
    parent, environment = legacy._setup(legacy._environment(), "svc-" + short)
    if case.test_only_random_password:
        environment.pop("PALIMPSEST_PROOF_EVIDENCE_DIR", None)
    root_volume_directory = resolve_roots(environment).oci_root_volumes
    root_volume_before = (
        {path.name for path in root_volume_directory.iterdir()}
        if case.test_only_random_password and root_volume_directory.is_dir()
        else set()
    )
    name_prefix = "hub-service-mysql-random-" if case.test_only_random_password else (
        "hub-service-" + case.key.lower().replace("_", "-") + "-"
    )
    name = name_prefix + uuid.uuid4().hex[:8]
    if case.test_only_random_password:
        assert not (resolve_roots(environment).runs / name).exists()
        virsh = legacy.shutil.which("virsh", path=environment.get("PATH"))
        assert virsh
        inventory = legacy._bounded_command(
            [virsh, "-c", "qemu:///system", "list", "--all", "--name"],
            environment=environment, timeout=15,
        )
        legacy._success(inventory)
        assert name not in legacy._inventory_lines(inventory.stdout, encoding="utf-8")
    original_selection = selection
    original_source_hash = legacy._file_sha256(selection.archive)
    assert original_source_hash == selection.archive_digest
    source_hash = original_source_hash
    domain_uuid = None
    completed = False
    disposed = False
    primary_error: BaseException | None = None
    loopback_security_error: AssertionError | None = None
    try:
        process = legacy._authenticate(selection, parent)
        process.require_bootable()
        assert process.argv[-len(case.argv_suffix) :] == case.argv_suffix
        if case.test_only_random_password:
            original_environment = dict(process.environment)
            assert not any(
                key in original_environment
                for key in ("MYSQL_ROOT_PASSWORD", "MYSQL_ALLOW_EMPTY_PASSWORD", "MYSQL_RANDOM_ROOT_PASSWORD")
            )
            selection = _derived_mysql_random_password_archive(selection, parent / "mysql-random-derived.oci.tar")
            source_hash = legacy._file_sha256(selection.archive)
            assert source_hash == selection.archive_digest
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
                "test_only_random_password": case.test_only_random_password,
                "vcpus": 1,
            },
        )
        _save_json(parent, "authenticated-process.json", process.to_dict())
        launched = _save_service_result(
            parent, "run", _case_cli(case, parent, environment, *_run_arguments(case, selection, name), timeout=240),
            secret_safe=case.test_only_random_password,
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
        if case.test_only_random_password:
            _wait_mysql_final_ready(parent, environment, name)
        else:
            _wait_ready_or_inactive(parent, environment, name, case.readiness)
        state, observed_uuid = _domain_state(environment, name)
        assert state == "running" and observed_uuid == domain_uuid
        _assert_running_domain_has_no_interface(parent, environment, name, domain_uuid)
        before = legacy._root_proof(environment, name)
        _save_json(parent, "root-proof-before.json", before)
        assert before["domain"]["uuid"] == domain_uuid
        version = _save_service_result(
            parent, "version",
            _case_cli(case, parent, environment, "exec", name, "--", *case.version_argv, timeout=60),
            secret_safe=case.test_only_random_password,
        )
        legacy._success(version)
        assert case.version_marker in version.stdout + version.stderr
        if case.user_override is not None:
            loopback = _save_service_result(
                parent,
                "guest-loopback-security",
                _case_cli(
                    case, parent, environment,
                    "exec",
                    name,
                    "--",
                    "/bin/sh",
                    "-c",
                    _loopback_security_command(),
                    timeout=60,
                ),
                secret_safe=case.test_only_random_password,
            )
            legacy._success(loopback)
            try:
                _assert_loopback_security(loopback.stdout)
            except AssertionError as exc:
                loopback_security_error = exc
        probe = _save_service_result(
            parent, "service-probe",
            _case_cli(case, parent, environment, "exec", name, "--", *case.probe_argv, timeout=60),
            secret_safe=case.test_only_random_password,
        )
        probe_ok = _service_probe_ok(case, probe)
        if probe.returncode == 77:
            _save_json(
                parent, "service-probe.json", {"result": "skipped", "reason": "image client absent", "returncode": 77}
            )
        else:
            receipt = {
                "result": "passed" if probe_ok else "failed",
                "returncode": probe.returncode,
                "transport": "guest loopback-or-unix",
            }
            if case.test_only_random_password:
                receipt.update(transport="unix-socket", reachability=probe_ok, authenticated_sql=False)
            _save_json(parent, "service-probe.json", receipt)
        root = _save_service_result(
            parent,
            "root",
            _case_cli(
                case, parent, environment, "exec", name, "--", "/bin/sh", "-c",
                "stat -c '%d %i' /; cat /etc/os-release", timeout=60,
            ),
            secret_safe=case.test_only_random_password,
        )
        legacy._success(root)
        lines = root.stdout.decode("utf-8").splitlines()
        device, inode = (int(value) for value in lines[0].split())
        assert any(line.startswith("ID=") for line in lines[1:])
        denied = _save_service_result(
            parent,
            "pid1-refusal",
            _case_cli(
                case, parent, environment, "exec", name, "--", "/bin/sh", "-c",
                "LC_ALL=C cat /proc/1/root/etc/os-release", timeout=60,
            ),
            secret_safe=case.test_only_random_password,
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
        if case.test_only_random_password:
            assert not _retain_redacted_console(parent, environment, name), "generated password appeared on console"
            _assert_no_password_leak(parent)
        stopped = _save_service_result(
            parent, "stop", _case_cli(case, parent, environment, "stop", name, timeout=90),
            secret_safe=case.test_only_random_password,
        )
        legacy._success(stopped)
        removed = _save_service_result(
            parent, "rm", _case_cli(case, parent, environment, "rm", name, timeout=90),
            secret_safe=case.test_only_random_password,
        )
        legacy._success(removed)
        legacy._assert_domain_absent(environment, name, domain_uuid)
        assert not (parent / "state" / "runs" / name).exists()
        if case.test_only_random_password:
            remaining = {path.name for path in root_volume_directory.iterdir()} if root_volume_directory.is_dir() else set()
            assert remaining == root_volume_before
        assert legacy._file_sha256(selection.archive) == source_hash == selection.archive_digest
        disposed = True
        completed = True
    except BaseException as exc:
        primary_error = exc
        try:
            if case.test_only_random_password:
                run_path = resolve_roots(environment).runs / name
                cleanup_evidence_errors: list[str] = []
                leak_detected = False
                if run_path.exists():
                    binding = load_oci_run_binding(resolve_roots(environment), name)
                    assert binding.record.name == name
                    if domain_uuid is None:
                        domain_uuid = binding.domain_uuid
                    assert binding.domain_uuid == domain_uuid
                    state, observed_uuid = _domain_state(environment, name)
                    assert observed_uuid == domain_uuid
                    leak_detected, command_errors = _capture_then_dispose(
                        parent, environment, name, state,
                        lambda operation: _secret_safe_cli(parent, environment, operation, name, timeout=90),
                    )
                    cleanup_evidence_errors.extend(command_errors)
                    legacy._assert_domain_absent(environment, name, domain_uuid)
                    assert not run_path.exists()
                remaining = (
                    {path.name for path in root_volume_directory.iterdir()} if root_volume_directory.is_dir() else set()
                )
                assert remaining == root_volume_before
                disposed = True
                _assert_no_password_leak(parent)
                assert not cleanup_evidence_errors, "console evidence capture failed before owned disposal"
                assert not leak_detected, "generated password was detected and redacted before owned disposal"
            else:
                _preserve_failed_owned_runtime(parent, environment, name, domain_uuid)
        except BaseException as cleanup_exc:
            exc.add_note(f"owned failure handling also failed: {cleanup_exc!r}")
        raise
    finally:
        try:
            assert legacy._record_source_hashes(parent, source_hash, selection.archive) == source_hash
            assert legacy._file_sha256(original_selection.archive) == original_source_hash
            _save_json(
                parent, "completion.json",
                {"application_completed": completed, "owned_resources_disposed": disposed},
            )
        except BaseException as evidence_exc:
            if primary_error is None:
                raise
            primary_error.add_note(f"final evidence recording also failed: {evidence_exc!r}")
