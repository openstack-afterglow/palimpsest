"""Opt-in native proof for the public durable OCI exec-record CLI.

This is deliberately narrower than the guest matrix: it reuses an accepted v2
archive, performs one recorded exec, and proves the local record survives normal
VM removal. Successful runs retain the private record directory for inspection.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from .test_oci_public_cli_live import _cli, _success

_ENABLE = "PALIMPSEST_OCI_EXEC_RECORD_CLI_LIVE"
_IMAGE = "PALIMPSEST_OCI_EXEC_LIVE_IMAGE"
_BOOT_KEYS = (
    "KERNEL",
    "KERNEL_DIGEST",
    "KERNEL_CONFIG",
    "KERNEL_CONFIG_DIGEST",
    "PACKER",
)
_GUIDANCE = (
    "Local historical metadata only; it grants no run or monitor authority and is not proof that replay is safe."
)
_MAX_VIRSH_INVENTORY_BYTES = 1024 * 1024
_MAX_VIRSH_INVENTORY_RECORDS = 65536

pytestmark = [
    pytest.mark.kvm,
    pytest.mark.skipif(
        os.environ.get(_ENABLE) != "1",
        reason=f"set {_ENABLE}=1 for the separate public OCI exec-record proof",
    ),
]


def _record_files(path: Path) -> dict[str, bytes]:
    expected = {"pending.json", "observed.json", "confirmed.json"}
    directory = path.lstat()
    assert stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.geteuid()
    assert stat.S_IMODE(directory.st_mode) == 0o700
    assert {entry.name for entry in path.iterdir()} == expected
    contents = {}
    for name in sorted(expected):
        artifact = path / name
        metadata = artifact.lstat()
        assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        contents[name] = artifact.read_bytes()
    return contents


def _inspect(environment: dict[str, str], path: Path) -> tuple[subprocess.CompletedProcess[bytes], dict]:
    result = _cli(environment, "oci", "exec-record", path)
    _success(result)
    assert result.stderr == b""
    return result, json.loads(result.stdout)


def _virsh_inventory_lines(output: bytes, *, encoding: str) -> list[str]:
    assert len(output) <= _MAX_VIRSH_INVENTORY_BYTES
    if output in (b"", b"\n"):
        return []
    assert output.endswith(b"\n")
    text = output.decode(encoding)
    records = text[:-1]
    if records.endswith("\n"):
        records = records[:-1]
        assert records
    lines = records.split("\n")
    assert len(lines) <= _MAX_VIRSH_INVENTORY_RECORDS
    assert all(line and all(character.isprintable() for character in line) for line in lines)
    return lines


def _virsh_uuid_inventory(output: bytes) -> list[str]:
    identifiers = _virsh_inventory_lines(output, encoding="ascii")
    for identifier in identifiers:
        parsed = uuid.UUID(identifier)
        assert str(parsed) == identifier
    assert len(set(identifiers)) == len(identifiers)
    return identifiers


def test_public_recorded_exec_survives_vm_removal_without_runtime_or_boot_configuration():
    assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
    image_value = os.environ.get(_IMAGE)
    assert image_value, f"{_IMAGE} must name the Palimpsest-built OCI archive"
    archive = Path(image_value).resolve(strict=True)
    assert archive.is_file()
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = json.loads(archive.with_name("acceptance.json").read_text())
    assert receipt["schema"] == "palimpsest.oci-root-build-run-acceptance.v2"
    assert receipt["archive_sha256"] == "sha256:" + archive_digest
    marker = receipt["marker"]
    assert type(marker) is str and re.fullmatch("palimpsest-local-build-[0-9a-f]{32}", marker)

    suffix = uuid.uuid4().hex[:8]
    runtime_parent = Path("/tmp") / f"p-execrecord-runtime-{suffix}"
    record_parent = Path("/tmp") / f"p-execrecord-retained-{suffix}"
    record_path = record_parent / "new-record"
    assert not runtime_parent.exists() and not record_parent.exists()
    assert runtime_parent not in record_path.parents
    record_parent.mkdir(mode=0o700)
    record_parent_owned = record_parent.lstat()
    assert stat.S_ISDIR(record_parent_owned.st_mode) and record_parent_owned.st_uid == os.geteuid()
    assert stat.S_IMODE(record_parent_owned.st_mode) == 0o700

    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONNOUSERSITE"] = "1"
    print(f"public OCI exec-record runtime evidence: {runtime_parent}", flush=True)
    print(f"public OCI exec-record retained evidence: {record_parent}", flush=True)
    _success(_cli(environment, "oci", "init-runtime", runtime_parent))
    runtime_owned = runtime_parent.lstat()
    environment["PALIMPSEST_STATE_HOME"] = str(runtime_parent / "state")
    environment["XDG_CONFIG_HOME"] = str(runtime_parent / "config")
    name = f"exec-record-cli-{suffix}"
    completed = False
    try:
        launched = _cli(environment, "run", archive, "--name", name, "-d", timeout=180)
        (runtime_parent / "launch.stdout").write_bytes(launched.stdout)
        (runtime_parent / "launch.stderr").write_bytes(launched.stderr)
        _success(launched)
        assert launched.stdout == (name + "\n").encode()

        proof_before = _cli(environment, "oci", "root-proof", name)
        _success(proof_before)
        before_report = json.loads(proof_before.stdout)

        literal = "literal $HOME; $(uname) with spaces"
        stderr_line = "separate guest stderr"
        command = (
            'test "$(cat /palimpsest-e2e-root-marker)" = "$2" && '
            'test "$(cat /proc/self/root/palimpsest-e2e-root-marker)" = "$2" || exit 90; '
            "pid1_error=$(LC_ALL=C cat /proc/1/root/palimpsest-e2e-root-marker 2>&1) && exit 91; "
            'case "$pid1_error" in *"Permission denied"*) ;; *) exit 92;; esac; '
            'printf "%s\\n" "$1"; printf "separate guest stderr\\n" >&2; exit 17'
        )
        result = _cli(
            environment,
            "exec",
            "--completion-record",
            record_path,
            name,
            "--",
            "/bin/sh",
            "-c",
            command,
            "exec-record-probe",
            literal,
            marker,
            timeout=60,
        )
        (runtime_parent / "recorded.stdout").write_bytes(result.stdout)
        (runtime_parent / "recorded.stderr").write_bytes(result.stderr)
        assert result.returncode == 17, result.stderr.decode(errors="replace")
        assert result.stdout == (literal + "\n").encode()
        assert result.stderr == (stderr_line + "\n").encode()

        inspected, report = _inspect(environment, record_path)
        (runtime_parent / "inspect-before.stdout").write_bytes(inspected.stdout)
        (runtime_parent / "inspect-before.stderr").write_bytes(inspected.stderr)
        record_id = report["record_id"]
        parsed_id = uuid.UUID(record_id)
        assert parsed_id.version == 4 and parsed_id.variant == uuid.RFC_4122 and str(parsed_id) == record_id
        assert report == {
            "schema": "palimpsest.oci-exec-record",
            "version": 1,
            "record_id": record_id,
            "phase": "confirmed",
            "observation": {
                "terminal": {
                    "returncode": 17,
                    "exit_code": 17,
                    "signal_number": None,
                    "category": "exited",
                },
                "reason": "completed",
                "stdout_bytes": len(result.stdout),
                "stderr_bytes": len(result.stderr),
                "acknowledgement": "confirmed",
            },
            "classification": "local-historical-metadata",
            "guidance": _GUIDANCE,
        }
        recorded_contents = _record_files(record_path)

        proof_after = _cli(environment, "oci", "root-proof", name)
        _success(proof_after)
        after_report = json.loads(proof_after.stdout)
        assert {key: before_report[key] for key in ("run", "boot", "domain")} == {
            key: after_report[key] for key in ("run", "boot", "domain")
        }
        assert before_report["root_identity"] == after_report["root_identity"]

        virsh = shutil.which("virsh", path=environment.get("PATH"))
        assert virsh is not None

        def virsh_query(*arguments: str):
            return subprocess.run(
                [virsh, "-c", "qemu:///system", *arguments],
                env=environment,
                capture_output=True,
                check=False,
                timeout=15,
            )

        domain_uuid_result = virsh_query("domuuid", name)
        _success(domain_uuid_result)
        domain_uuid_text = domain_uuid_result.stdout.decode("ascii")
        assert domain_uuid_text.endswith("\n") and domain_uuid_text.count("\n") == 1
        domain_uuid = domain_uuid_text.removesuffix("\n")
        parsed_domain_uuid = uuid.UUID(domain_uuid)
        assert str(parsed_domain_uuid) == domain_uuid
        domain_name_result = virsh_query("domname", domain_uuid)
        _success(domain_name_result)
        assert domain_name_result.stdout == (name + "\n").encode()
        assert domain_uuid == before_report["domain"]["uuid"] == after_report["domain"]["uuid"]

        _success(_cli(environment, "stop", name, timeout=60))
        _success(_cli(environment, "rm", name, timeout=60))
        remaining_names = virsh_query("list", "--all", "--name")
        _success(remaining_names)
        assert remaining_names.stderr == b""
        remaining_name_values = _virsh_inventory_lines(remaining_names.stdout, encoding="utf-8")
        assert len(set(remaining_name_values)) == len(remaining_name_values)
        assert name not in remaining_name_values
        remaining_uuids = virsh_query("list", "--all", "--uuid")
        _success(remaining_uuids)
        assert remaining_uuids.stderr == b""
        assert domain_uuid not in _virsh_uuid_inventory(remaining_uuids.stdout)
        assert not (runtime_parent / "state" / "runs" / name).exists()
        assert _record_files(record_path) == recorded_contents

        offline = dict(environment)
        offline["PALIMPSEST_STATE_HOME"] = str(runtime_parent / "absent-state")
        offline["XDG_CONFIG_HOME"] = str(runtime_parent / "absent-config")
        for key in _BOOT_KEYS:
            offline.pop("PALIMPSEST_OCI_" + key, None)
        assert not Path(offline["PALIMPSEST_STATE_HOME"]).exists()
        assert not Path(offline["XDG_CONFIG_HOME"]).exists()
        inspected_after, after_removal_report = _inspect(offline, record_path)
        assert after_removal_report == report
        assert inspected_after.stdout == inspected.stdout
        assert not Path(offline["PALIMPSEST_STATE_HOME"]).exists()
        assert not Path(offline["XDG_CONFIG_HOME"]).exists()
        assert _record_files(record_path) == recorded_contents
        assert archive.is_file() and hashlib.sha256(archive.read_bytes()).hexdigest() == archive_digest
        retained = record_parent.lstat()
        assert stat.S_ISDIR(retained.st_mode)
        assert (retained.st_uid, retained.st_gid) == (record_parent_owned.st_uid, record_parent_owned.st_gid)
        assert (retained.st_dev, retained.st_ino) == (
            record_parent_owned.st_dev,
            record_parent_owned.st_ino,
        )
        assert stat.S_IMODE(retained.st_mode) == stat.S_IMODE(record_parent_owned.st_mode)
        assert _record_files(record_path) == recorded_contents
        completed = True
    finally:
        if completed:
            visible = runtime_parent.lstat()
            assert stat.S_ISDIR(visible.st_mode) and visible.st_uid == os.geteuid()
            assert (visible.st_dev, visible.st_ino) == (runtime_owned.st_dev, runtime_owned.st_ino)
            shutil.rmtree(runtime_parent)
            print(f"public OCI exec-record SUCCESS retained evidence: {record_parent}", flush=True)
        else:
            print(f"public OCI exec-record failure runtime evidence preserved: {runtime_parent}", flush=True)
            print(f"public OCI exec-record failure record evidence preserved: {record_parent}", flush=True)
