"""Opt-in native compatibility matrix for four unchanged official service images."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import uuid
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
        before = legacy._root_proof(environment, name)
        _save_json(parent, "root-proof-before.json", before)
        assert before["domain"]["uuid"] == domain_uuid
        version = legacy._save(
            parent, "version", legacy._cli(environment, "exec", name, "--", *case.version_argv, timeout=60)
        )
        legacy._success(version)
        assert case.version_marker in version.stdout + version.stderr
        probe = legacy._save(
            parent, "service-probe", legacy._cli(environment, "exec", name, "--", *case.probe_argv, timeout=60)
        )
        probe_ok = probe.returncode == 0 and case.probe_marker in probe.stdout
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
