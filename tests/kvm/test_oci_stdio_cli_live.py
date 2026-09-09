"""Opt-in native diagnostic for main-console and public-exec stdio FDs."""

from __future__ import annotations

import json
import os
import platform
import re
import stat
import sys
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from .test_oci_docker_hub_cli_live import (
    _assert_domain_absent,
    _bounded_command,
    _cli,
    _domain_uuid,
    _file_sha256,
    _root_proof,
    _save,
    _success,
)
from .test_oci_public_cli_live import _image_layout

_ENABLE = "PALIMPSEST_OCI_STDIO_CLI_LIVE"
_TOOLCHAIN = "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1"
_PREFIX = b"PALIMPSEST_STDIO_FD_V1 "
_MAX_LINE = 2048
_MAX_CONSOLE = 8 * 1024 * 1024
_FIELDS = (
    "role uid gid groups capinh capprm capeff capbnd capamb securebits nnp seccomp "
    "fd1type fd1mode fd1uid fd1gid fd1dev fd1ino fd1reopen "
    "fd2type fd2mode fd2uid fd2gid fd2dev fd2ino fd2reopen "
    "stdout_alias stderr_alias rootdev rootino pid1root"
).split()
_DECIMAL = re.compile(r"0|[1-9][0-9]*")
_HEX16 = re.compile(r"[0-9a-f]{16}")


def parse_probe_record(line: bytes, *, expected_role: str) -> dict[str, int | str]:
    if len(line) > _MAX_LINE or not line.endswith(b"\n") or not line.startswith(_PREFIX):
        raise ValueError("invalid diagnostic record envelope")
    try:
        words = line[len(_PREFIX) : -1].decode("ascii").split(" ")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid diagnostic record encoding") from exc
    if len(words) != len(_FIELDS) or any(not word for word in words):
        raise ValueError("invalid diagnostic record field count")
    result: dict[str, int | str] = {}
    for expected, word in zip(_FIELDS, words, strict=True):
        key, separator, value = word.partition("=")
        if separator != "=" or key != expected or key in result or not value:
            raise ValueError("invalid diagnostic record field")
        if key == "role":
            if value not in {"service", "exec"}:
                raise ValueError("invalid diagnostic role")
            result[key] = value
        elif key.startswith("cap"):
            if not _HEX16.fullmatch(value):
                raise ValueError("invalid diagnostic hexadecimal field")
            result[key] = int(value, 16)
        else:
            if not _DECIMAL.fullmatch(value):
                raise ValueError("invalid diagnostic decimal field")
            number = int(value)
            if number > (1 << 64) - 1:
                raise ValueError("diagnostic integer out of range")
            result[key] = number
    if result["role"] != expected_role:
        raise ValueError("unexpected diagnostic role")
    return result


def _records(payload: bytes, *, role: str, partial_tail: bool = False) -> list[dict[str, int | str]]:
    assert len(payload) <= _MAX_CONSOLE
    lines = payload.splitlines(keepends=True)
    if partial_tail and lines and not lines[-1].endswith(b"\n"):
        lines.pop()
    records = []
    for line in lines:
        if not line.startswith(_PREFIX):
            continue
        if partial_tail and line.endswith(b"\r\n"):
            line = line[:-2] + b"\n"
        records.append(parse_probe_record(line, expected_role=role))
    return records


def _compile(parent: Path, environment: dict[str, str]) -> Path:
    source = Path(__file__).with_name("assets") / "stdio-fd-probe.c"
    output = parent / "stdio-fd-probe"
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/tmp",
        "--env",
        "LANG=C",
        "--env",
        "LC_ALL=C",
        "--env",
        "TZ=UTC",
        "--pids-limit",
        "16",
        "--memory",
        "128m",
        "--cpus",
        "0.25",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        f"type=bind,src={source},dst=/src/probe.c,readonly",
        "--mount",
        f"type=bind,src={parent},dst=/out",
        "--entrypoint",
        "/usr/local/bin/gcc",
        _TOOLCHAIN,
        "-std=c11",
        "-Os",
        "-nostdlib",
        "-static",
        "-fno-builtin",
        "-fno-ident",
        "-fno-stack-protector",
        "-fno-unwind-tables",
        "-fno-pie",
        "-no-pie",
        "-ffreestanding",
        "-fno-tree-loop-distribute-patterns",
        "-mno-red-zone",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wl,--build-id=none,-z,noexecstack,-s",
        "-o",
        "/out/stdio-fd-probe",
        "/src/probe.c",
    ]
    result = _bounded_command(command, environment=environment, timeout=60)
    _save(parent, "compile", result)
    _success(result)
    assert output.is_file() and stat.S_IMODE(output.stat().st_mode) & 0o111
    return output


def _wait_main(console: Path) -> tuple[bytes, list[dict[str, int | str]]]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if console.is_file():
            with console.open("rb") as source:
                payload = source.read(_MAX_CONSOLE + 1)
            assert len(payload) <= _MAX_CONSOLE
            records = _records(payload, role="service", partial_tail=True)
            if len(records) >= 2:
                return payload, records
        time.sleep(0.05)
    pytest.fail(f"missing bounded main stdio records; preserve {console}")


def _assert_security(record: dict[str, int | str], uid: int) -> None:
    assert record["uid"] == record["gid"] == uid and record["groups"] == 0
    assert all(record[key] == 0 for key in ("capinh", "capprm", "capeff", "capbnd", "capamb"))
    assert (record["securebits"], record["nnp"], record["seccomp"]) == (239, 1, 2)
    assert record["stdout_alias"] == record["stderr_alias"] == 2
    assert record["pid1root"] == 13
    for fd in (1, 2):
        assert record[f"fd{fd}type"] in {0o010000, 0o020000, 0o100000, 0o140000}
        assert 0 <= int(record[f"fd{fd}mode"]) <= 0o7777
        assert int(record[f"fd{fd}dev"]) > 0 and int(record[f"fd{fd}ino"]) > 0
        assert int(record[f"fd{fd}reopen"]) in {0, 13}


@pytest.mark.kvm
@pytest.mark.parametrize("uid", [pytest.param(0, id="uid-0"), pytest.param(101, id="uid-101")])
def test_public_stdio_fd_ownership_and_reopen_diagnostic(uid: int) -> None:
    if os.environ.get(_ENABLE) != "1":
        pytest.skip(f"set {_ENABLE}=1 on the reviewed native Linux/x86_64 KVM host")
    assert sys.platform.startswith("linux") and platform.machine() == "x86_64" and Path("/dev/kvm").exists()
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONNOUSERSITE"] = "1"
    suffix = uuid.uuid4().hex[:8]
    parent = Path("/tmp") / f"p-stdio-{uid}-{suffix}"
    print(f"stdio FD diagnostic evidence: {parent}", flush=True)
    initialized = _cli(environment, "oci", "init-runtime", parent)
    _success(initialized)
    evidence = parent / "evidence"
    evidence.mkdir(mode=0o700)
    _save(evidence, "init-runtime", initialized)
    environment["PALIMPSEST_PROOF_EVIDENCE_DIR"] = str(evidence)
    environment["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    environment["XDG_CONFIG_HOME"] = str(parent / "config")
    executable = _compile(evidence, environment)
    layout = _image_layout(evidence, executable, "service")
    archive = evidence / "stdio.oci.tar"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as bundle:
        for entry in sorted(layout.rglob("*")):
            bundle.add(entry, arcname=str(entry.relative_to(layout)), recursive=False)
    before = _file_sha256(archive)
    name = f"stdio-{uid}-{suffix}"
    launched = _save(
        evidence,
        "launch",
        _cli(
            environment,
            "run",
            archive,
            "--name",
            name,
            "--memory",
            "512",
            "--vcpus",
            "1",
            "--network",
            "none",
            "--user",
            f"{uid}:{uid}",
            "-d",
            timeout=180,
        ),
    )
    _success(launched)
    console = parent / "state" / "runs" / name / "io" / "console.log"
    _payload, main_records = _wait_main(console)
    assert len(main_records) == 2 and main_records[0] == main_records[1]
    main = main_records[0]
    _assert_security(main, uid)
    proof_before = _root_proof(environment, name)
    (evidence / "root-before.json").write_text(json.dumps(proof_before, indent=2, sort_keys=True) + "\n")
    assert (main["rootdev"], main["rootino"]) == (
        proof_before["root_identity"]["device"],
        proof_before["root_identity"]["inode"],
    )
    domain_uuid = _domain_uuid(environment, name)
    executed = _save(evidence, "exec", _cli(environment, "exec", name, "--", "/bin/public-proof", "exec", timeout=60))
    _success(executed)
    stdout_records, stderr_records = _records(executed.stdout, role="exec"), _records(executed.stderr, role="exec")
    assert len(stdout_records) == len(stderr_records) == 1 and stdout_records[0] == stderr_records[0]
    additional = stdout_records[0]
    _assert_security(additional, uid)
    assert (additional["rootdev"], additional["rootino"]) == (main["rootdev"], main["rootino"])
    assert all(additional[f"fd{fd}type"] == 0o010000 for fd in (1, 2))
    assert all(additional[f"fd{fd}uid"] == additional[f"fd{fd}gid"] == 0 for fd in (1, 2))
    assert all(additional[f"fd{fd}mode"] == 0o600 for fd in (1, 2))
    assert all(additional[f"fd{fd}reopen"] == (0 if uid == 0 else 13) for fd in (1, 2))
    assert any(
        main[f"fd{fd}type"] != additional[f"fd{fd}type"] or main[f"fd{fd}ino"] != additional[f"fd{fd}ino"]
        for fd in (1, 2)
    )
    proof_after = _root_proof(environment, name)
    (evidence / "root-after.json").write_text(json.dumps(proof_after, indent=2, sort_keys=True) + "\n")
    assert proof_after["root_identity"] == proof_before["root_identity"]
    assert {key: proof_after[key] for key in ("run", "boot", "domain")} == {
        key: proof_before[key] for key in ("run", "boot", "domain")
    }
    (evidence / "diagnostic-receipt.json").write_text(
        json.dumps({"archive_sha256": before, "main": main, "exec": additional, "uid": uid}, indent=2, sort_keys=True)
        + "\n"
    )
    _success(_save(evidence, "stop", _cli(environment, "stop", name, timeout=60)))
    _success(_save(evidence, "rm", _cli(environment, "rm", name, timeout=60)))
    assert not (parent / "state" / "runs" / name).exists()
    _assert_domain_absent(environment, name, domain_uuid)
    assert _file_sha256(archive) == before
