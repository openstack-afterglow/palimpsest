"""Opt-in public CLI proofs using unchanged, digest-pinned Docker Hub images."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from palimpsest_local.oci_source import LocalArchiveSource, SourceCAS

_PREFIX = "PALIMPSEST_OCI_DOCKER_HUB_"
_BOOT_KEYS = ("KERNEL", "KERNEL_DIGEST", "KERNEL_CONFIG", "KERNEL_CONFIG_DIGEST", "PACKER")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_COMMAND_OUTPUT = 4 * 1024 * 1024
_MAX_CONSOLE_BYTES = 8 * 1024 * 1024
pytestmark = pytest.mark.kvm


def _failure_evidence(environment, command, stdout, stderr, reason, timeout):
    raw_parent = environment.get("PALIMPSEST_PROOF_EVIDENCE_DIR")
    if not raw_parent:
        return
    parent = Path(raw_parent)
    token = "command-failure-" + uuid.uuid4().hex
    try:
        (parent / f"{token}.stdout").write_bytes(stdout)
        (parent / f"{token}.stderr").write_bytes(stderr)
        (parent / f"{token}.json").write_text(
            json.dumps(
                {
                    "command": command,
                    "max_output_bytes_per_stream": _MAX_COMMAND_OUTPUT,
                    "reason": reason,
                    "timeout_seconds": timeout,
                },
                sort_keys=True,
            )
            + "\n"
        )
    except OSError as exc:
        print(f"could not persist bounded command failure: {exc}", flush=True)


def _kill_owned(process):
    if process.poll() is not None:
        process.wait()
        return
    process.kill()
    process.wait(timeout=1)


def _attempt_owned_kill(process):
    try:
        _kill_owned(process)
    except BaseException as exc:
        print(f"could not SIGKILL/reap owned CLI child {process.pid}: {exc}", flush=True)


def _bounded_command(command, *, environment, timeout):
    process = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    reason = None
    evidence_saved = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            for key, _events in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.fileobj].extend(chunk)
                if len(streams[key.fileobj]) > _MAX_COMMAND_OUTPUT:
                    reason = "output-overflow"
                    break
            if reason:
                break
        if reason:
            stdout = bytes(streams[process.stdout][:_MAX_COMMAND_OUTPUT])
            stderr = bytes(streams[process.stderr][:_MAX_COMMAND_OUTPUT])
            _failure_evidence(environment, list(map(str, command)), stdout, stderr, reason, timeout)
            evidence_saved = True
            _attempt_owned_kill(process)
        else:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
                stdout = bytes(streams[process.stdout][:_MAX_COMMAND_OUTPUT])
                stderr = bytes(streams[process.stderr][:_MAX_COMMAND_OUTPUT])
                _failure_evidence(environment, list(map(str, command)), stdout, stderr, reason, timeout)
                evidence_saved = True
                _attempt_owned_kill(process)
    except BaseException:
        if process.poll() is None:
            _attempt_owned_kill(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    stdout = bytes(streams[process.stdout][:_MAX_COMMAND_OUTPUT])
    stderr = bytes(streams[process.stderr][:_MAX_COMMAND_OUTPUT])
    if reason:
        if not evidence_saved:
            _failure_evidence(environment, list(map(str, command)), stdout, stderr, reason, timeout)
        if reason == "timeout":
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        raise AssertionError(f"owned command output exceeded {_MAX_COMMAND_OUTPUT} bytes")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _cli(environment, *args, timeout=90):
    return _bounded_command(
        [sys.executable, "-m", "palimpsest_local.cli", *map(str, args)], environment=environment, timeout=timeout
    )


def _success(result):
    assert result.returncode == 0, (
        result.returncode,
        result.stdout.decode(errors="replace"),
        result.stderr.decode(errors="replace"),
    )


def _wait_console(path, marker, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            with path.open("rb") as source:
                payload = source.read(_MAX_CONSOLE_BYTES + 1)
            if len(payload) > _MAX_CONSOLE_BYTES:
                pytest.fail(f"default workload console exceeds {_MAX_CONSOLE_BYTES} bytes; preserve {path}")
            if marker in payload:
                return
        time.sleep(0.05)
    pytest.fail(f"missing default workload readiness {marker!r}; preserve {path}")


def _inventory_lines(output: bytes, *, encoding: str) -> list[str]:
    assert len(output) <= 1024 * 1024
    if output in (b"", b"\n"):
        return []
    assert output.endswith(b"\n")
    text = output.decode(encoding)[:-1]
    if text.endswith("\n"):
        text = text[:-1]
    lines = text.split("\n")
    assert len(lines) <= 65536 and all(lines)
    return lines


def _uuid_inventory(output: bytes) -> list[str]:
    identifiers = _inventory_lines(output, encoding="ascii")
    assert all(str(uuid.UUID(value)) == value for value in identifiers)
    assert len(identifiers) == len(set(identifiers))
    return identifiers


@dataclass(frozen=True)
class DockerHubImageSelection:
    archive: Path
    archive_digest: str
    manifest_digest: str


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _selection(kind: str, environment: dict[str, str]) -> DockerHubImageSelection:
    stem = _PREFIX + kind + "_"
    if environment.get(stem + "LIVE") != "1":
        pytest.skip(f"set {stem}LIVE=1 for this independent Docker Hub proof")
    raw_path = environment.get(stem + "IMAGE", "")
    archive_digest = environment.get(stem + "ARCHIVE_SHA256", "")
    manifest_digest = environment.get(stem + "MANIFEST_SHA256", "")
    assert raw_path and Path(raw_path).is_absolute(), f"{stem}IMAGE must be an absolute local OCI archive"
    archive = Path(raw_path).resolve(strict=True)
    assert archive.is_file()
    assert _DIGEST.fullmatch(archive_digest), f"{stem}ARCHIVE_SHA256 must be canonical"
    assert _DIGEST.fullmatch(manifest_digest), f"{stem}MANIFEST_SHA256 must be canonical"
    assert _file_sha256(archive) == archive_digest
    return DockerHubImageSelection(archive, archive_digest, manifest_digest)


def _environment() -> dict[str, str]:
    assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
    result = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result["PYTHONNOUSERSITE"] = "1"
    for key in _BOOT_KEYS:
        assert result.get("PALIMPSEST_OCI_" + key), f"missing PALIMPSEST_OCI_{key}"
    assert Path("/dev/kvm").exists() and shutil.which("virsh", path=result.get("PATH"))
    return result


def _setup(environment: dict[str, str], label: str):
    suffix = uuid.uuid4().hex[:8]
    parent = Path("/tmp") / f"p-hub-{label}-{suffix}"
    setup_evidence = Path("/tmp") / f"p-hub-evidence-{label}-{suffix}"
    setup_evidence.mkdir(mode=0o700)
    print(f"Docker Hub {label} evidence: {parent}", flush=True)
    selected = dict(environment)
    selected["PALIMPSEST_PROOF_EVIDENCE_DIR"] = str(setup_evidence)
    print(f"Docker Hub {label} setup-failure evidence: {setup_evidence}", flush=True)
    _success(_cli(selected, "oci", "init-runtime", parent))
    selected["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    selected["XDG_CONFIG_HOME"] = str(parent / "config")
    return parent, selected


def _authenticate(selection: DockerHubImageSelection, parent: Path):
    snapshot = LocalArchiveSource(selection.archive, selection.manifest_digest).snapshot(
        None, SourceCAS((parent / "state" / "runtime-packs" / "oci-source-v1").resolve())
    )
    assert snapshot.image.manifest_descriptor.digest == selection.manifest_digest
    snapshot.image.config.process.require_bootable()
    return snapshot.image.config.process


def _save(parent: Path, label: str, result: subprocess.CompletedProcess[bytes]):
    (parent / f"{label}.stdout").write_bytes(result.stdout)
    (parent / f"{label}.stderr").write_bytes(result.stderr)
    return result


def _root_proof(environment: dict[str, str], name: str):
    result = _cli(environment, "oci", "root-proof", name)
    _success(result)
    return json.loads(result.stdout)


def _domain_info(environment: dict[str, str], name: str):
    virsh = shutil.which("virsh", path=environment.get("PATH"))
    assert virsh
    return _bounded_command(
        [virsh, "-c", "qemu:///system", "dominfo", name],
        environment=environment,
        timeout=15,
    )


def _domain_uuid(environment: dict[str, str], name: str) -> str:
    virsh = shutil.which("virsh", path=environment.get("PATH"))
    assert virsh
    result = _bounded_command(
        [virsh, "-c", "qemu:///system", "domuuid", name],
        environment=environment,
        timeout=15,
    )
    _success(result)
    identifiers = _uuid_inventory(result.stdout)
    assert len(identifiers) == 1
    return identifiers[0]


def _assert_domain_absent(environment: dict[str, str], name: str, domain_uuid: str) -> None:
    virsh = shutil.which("virsh", path=environment.get("PATH"))
    assert virsh
    names = _bounded_command(
        [virsh, "-c", "qemu:///system", "list", "--all", "--name"],
        environment=environment,
        timeout=15,
    )
    identifiers = _bounded_command(
        [virsh, "-c", "qemu:///system", "list", "--all", "--uuid"],
        environment=environment,
        timeout=15,
    )
    _success(names)
    _success(identifiers)
    assert name not in _inventory_lines(names.stdout, encoding="utf-8")
    assert domain_uuid not in _uuid_inventory(identifiers.stdout)


def _record_source_hashes(parent: Path, before: str, archive: Path) -> str | None:
    try:
        after = _file_sha256(archive)
        (parent / "source-hashes.json").write_text(
            json.dumps({"after": after, "before": before}, sort_keys=True) + "\n"
        )
        return after
    except OSError as exc:
        print(f"could not record final source hash without replacing primary result: {exc}", flush=True)
        return None


def test_docker_hub_hello_world_foreground_default_process():
    selection = _selection("HELLO", os.environ)
    parent, environment = _setup(_environment(), "hello")
    name = "hub-hello-" + uuid.uuid4().hex[:8]
    before = _file_sha256(selection.archive)
    try:
        assert _authenticate(selection, parent).argv == ("/hello",)
        result = _save(
            parent,
            "run",
            _cli(
                environment,
                "run",
                selection.archive,
                "--manifest",
                selection.manifest_digest,
                "--name",
                name,
                "--memory",
                "512",
                "--vcpus",
                "1",
                timeout=180,
            ),
        )
        _success(result)
        assert b"Hello from Docker!" in result.stdout
        domain_uuid = _domain_uuid(environment, name)
        _success(_save(parent, "rm", _cli(environment, "rm", name, timeout=90)))
        _assert_domain_absent(environment, name, domain_uuid)
        assert not (parent / "state" / "runs" / name).exists()
        assert _file_sha256(selection.archive) == before == selection.archive_digest
    except BaseException:
        print(f"Docker Hub hello failure preserved: {parent}", flush=True)
        raise
    finally:
        _record_source_hashes(parent, before, selection.archive)


def _detached_default_service(
    kind: str,
    readiness: bytes,
    expected_argv_suffix: tuple[str, ...],
    version_binary: str,
    version_flag: str,
    version_marker: bytes,
):
    selection = _selection(kind, os.environ)
    parent, environment = _setup(_environment(), kind.lower())
    name = "hub-" + kind.lower() + "-" + uuid.uuid4().hex[:8]
    before_hash = _file_sha256(selection.archive)
    try:
        process = _authenticate(selection, parent)
        assert process.argv[-len(expected_argv_suffix) :] == expected_argv_suffix
        (parent / "authenticated-process.json").write_text(json.dumps(process.to_dict(), sort_keys=True) + "\n")
        launched = _save(
            parent,
            "run",
            _cli(
                environment,
                "run",
                selection.archive,
                "--manifest",
                selection.manifest_digest,
                "--name",
                name,
                "--memory",
                "512",
                "--vcpus",
                "1",
                "-d",
                timeout=180,
            ),
        )
        _success(launched)
        assert launched.stdout == (name + "\n").encode()
        _wait_console(parent / "state" / "runs" / name / "io" / "console.log", readiness, timeout=45)
        running = _save(parent, "domain", _domain_info(environment, name))
        _success(running)
        assert b"running" in running.stdout.lower()
        before = _root_proof(environment, name)
        domain_uuid = before["domain"]["uuid"]
        version = _save(
            parent, "version", _cli(environment, "exec", name, "--", version_binary, version_flag, timeout=60)
        )
        _success(version)
        assert version_marker in version.stdout + version.stderr
        identity = _save(parent, "user", _cli(environment, "exec", name, "--", "/bin/sh", "-c", "id -u", timeout=60))
        _success(identity)
        if process.user.user.isdecimal():
            assert identity.stdout == (process.user.user + "\n").encode()
        root = _save(
            parent,
            "root",
            _cli(
                environment,
                "exec",
                name,
                "--",
                "/bin/sh",
                "-c",
                "read release < /etc/alpine-release; stat -c '%d %i' /; printf '%s\\n' \"$release\"",
                timeout=60,
            ),
        )
        _success(root)
        identity, release = root.stdout.decode("ascii").splitlines()
        device, inode = (int(item) for item in identity.split())
        assert re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", release)
        refusal = _save(
            parent,
            "pid1-refusal",
            _cli(
                environment,
                "exec",
                name,
                "--",
                "/bin/sh",
                "-c",
                "LC_ALL=C cat /proc/1/root/etc/alpine-release",
                timeout=60,
            ),
        )
        assert refusal.returncode != 0 and refusal.stdout == b"" and b"Permission denied" in refusal.stderr
        after = _root_proof(environment, name)
        assert {key: before[key] for key in ("run", "boot", "domain")} == {
            key: after[key] for key in ("run", "boot", "domain")
        }
        assert before["root_identity"] == after["root_identity"]
        assert (device, inode) == (after["root_identity"]["device"], after["root_identity"]["inode"])
        _success(_save(parent, "stop", _cli(environment, "stop", name, timeout=90)))
        # Normal successful rm may emit expected late libvirt not-found diagnostics.
        _success(_save(parent, "rm", _cli(environment, "rm", name, timeout=90)))
        _assert_domain_absent(environment, name, domain_uuid)
        assert not (parent / "state" / "runs" / name).exists()
        assert _file_sha256(selection.archive) == before_hash == selection.archive_digest
    except BaseException:
        print(f"Docker Hub {kind.lower()} failure preserved: {parent}", flush=True)
        raise
    finally:
        _record_source_hashes(parent, before_hash, selection.archive)


def test_docker_hub_redis_detached_default_process_and_public_exec():
    # Redis may expose a real capability-policy incompatibility; do not override its config to pass.
    _detached_default_service(
        "REDIS", b"Ready to accept connections", ("redis-server",), "redis-server", "--version", b"Redis server v="
    )


def test_docker_hub_nginx_unprivileged_detached_default_process_and_public_exec():
    _detached_default_service(
        "NGINX",
        b"start worker processes",
        ("nginx", "-g", "daemon off;"),
        "nginx",
        "-v",
        b"nginx version:",
    )
