"""Opt-in public proof that one exclusive retained OCI root survives a new boot."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from .test_oci_public_cli_live import _cli, _success
from .virsh_output import virsh_inventory_lines, virsh_uuid_inventory

_ENABLE = "PALIMPSEST_OCI_RETAINED_ROOT_CLI_LIVE"
_IMAGE = "PALIMPSEST_OCI_EXEC_LIVE_IMAGE"
_BOOT_KEYS = ("KERNEL", "KERNEL_DIGEST", "KERNEL_CONFIG", "KERNEL_CONFIG_DIGEST", "PACKER")

pytestmark = [
    pytest.mark.kvm,
    pytest.mark.skipif(
        os.environ.get(_ENABLE) != "1",
        reason=f"set {_ENABLE}=1 for the separate public retained-root proof",
    ),
]


def _root_proof(environment: dict[str, str], name: str) -> dict[str, object]:
    result = _cli(environment, "oci", "root-proof", name)
    _success(result)
    assert result.stderr == b""
    return json.loads(result.stdout)


def _retained_volume_id(result: subprocess.CompletedProcess[bytes], name: str) -> str:
    _success(result)
    lines = result.stdout.decode("ascii").splitlines()
    assert len(lines) == 2 and lines[0] == f"removed {name}"
    prefix = "retained root\t"
    assert lines[1].startswith(prefix)
    identifier = lines[1].removeprefix(prefix)
    assert str(uuid.UUID(identifier)) == identifier
    return identifier


def test_public_retained_root_reuse_preserves_guest_write_across_distinct_boots():
    assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
    image_value = os.environ.get(_IMAGE)
    assert image_value, f"{_IMAGE} must name the accepted shell-capable OCI archive"
    archive = Path(image_value).resolve(strict=True)
    assert archive.is_file()
    receipt = json.loads(archive.with_name("acceptance.json").read_text())
    assert receipt["schema"] == "palimpsest.oci-root-build-run-acceptance.v2"
    assert receipt["archive_sha256"] == "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    for key in _BOOT_KEYS:
        assert os.environ.get("PALIMPSEST_OCI_" + key), f"missing PALIMPSEST_OCI_{key}"
    virsh = shutil.which("virsh", path=os.environ.get("PATH"))
    assert virsh is not None

    parent = Path("/tmp") / ("p-retained-root-" + uuid.uuid4().hex[:8])
    assert not parent.exists()
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONNOUSERSITE"] = "1"
    print(f"public retained-root runtime evidence: {parent}", flush=True)
    _success(_cli(environment, "oci", "init-runtime", parent))
    environment["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    environment["XDG_CONFIG_HOME"] = str(parent / "config")
    source_hash = hashlib.sha256(archive.read_bytes()).digest()
    marker = "palimpsest-retained-" + uuid.uuid4().hex
    marker_path = "/palimpsest-public-retained-root"
    suffix = uuid.uuid4().hex[:8]

    try:
        first_name = f"retained-first-{suffix}"
        first = _cli(
            environment,
            "run",
            archive,
            "--name",
            first_name,
            "--memory",
            "512",
            "--vcpus",
            "1",
            "--root-retention",
            "retain",
            "-d",
            timeout=180,
        )
        _success(first)
        assert first.stdout == (first_name + "\n").encode()
        first_proof = _root_proof(environment, first_name)
        written = _cli(
            environment,
            "exec",
            first_name,
            "--",
            "/bin/sh",
            "-c",
            'umask 077; printf "%s\\n" "$1" > "$2"; cat "$2"',
            "retained-write",
            marker,
            marker_path,
        )
        _success(written)
        assert written.stdout == (marker + "\n").encode() and written.stderr == b""
        _success(_cli(environment, "stop", first_name))
        volume_id = _retained_volume_id(_cli(environment, "rm", first_name), first_name)
        assert not (parent / "state" / "runs" / first_name).exists()

        second_name = f"retained-second-{suffix}"
        second = _cli(
            environment,
            "run",
            archive,
            "--name",
            second_name,
            "--memory",
            "512",
            "--vcpus",
            "1",
            "--root-retention",
            "retain",
            "--root-volume",
            volume_id,
            "-d",
            timeout=180,
        )
        _success(second)
        assert second.stdout == (second_name + "\n").encode()
        second_proof = _root_proof(environment, second_name)
        assert first_proof["run"] != second_proof["run"]
        assert first_proof["boot"] != second_proof["boot"]
        assert first_proof["domain"]["uuid"] != second_proof["domain"]["uuid"]
        read = _cli(
            environment,
            "exec",
            second_name,
            "--",
            "/bin/sh",
            "-c",
            'value=$(cat "$1") || exit 90; test "$value" = "$2" || exit 91; '
            'pid1=$(LC_ALL=C cat "/proc/1/root$1" 2>&1) && exit 92; '
            'case "$pid1" in *"Permission denied"*) ;; *) exit 93;; esac; printf "%s\\n" "$value"',
            "retained-read",
            marker_path,
            marker,
        )
        _success(read)
        assert read.stdout == (marker + "\n").encode() and read.stderr == b""
        _success(_cli(environment, "stop", second_name))
        assert _retained_volume_id(_cli(environment, "rm", second_name), second_name) == volume_id
        assert hashlib.sha256(archive.read_bytes()).digest() == source_hash
        assert not tuple((parent / "state" / "runs").iterdir())

        from palimpsest_local.oci_root_volume import OCIRootVolumeRecord

        stem = volume_id.replace("-", "")
        raw = parent / "state" / "oci-root-volumes" / f"{stem}.raw"
        record_path = parent / "state" / "oci-root-volumes" / f"{stem}.json"
        assert raw.is_file() and record_path.is_file()
        record = OCIRootVolumeRecord.from_dict(json.loads(record_path.read_bytes()))
        assert record.volume_id == volume_id and record.status == "retained"
        assert record.attached_run_id is None and record.attached_run_name is None

        names = subprocess.run(
            [virsh, "-c", "qemu:///system", "list", "--all", "--name"], capture_output=True, check=False, timeout=15
        )
        uuids = subprocess.run(
            [virsh, "-c", "qemu:///system", "list", "--all", "--uuid"], capture_output=True, check=False, timeout=15
        )
        _success(names)
        _success(uuids)
        assert first_name not in virsh_inventory_lines(names.stdout, encoding="utf-8")
        assert second_name not in virsh_inventory_lines(names.stdout, encoding="utf-8")
        assert first_proof["domain"]["uuid"] not in virsh_uuid_inventory(uuids.stdout)
        assert second_proof["domain"]["uuid"] not in virsh_uuid_inventory(uuids.stdout)
        print(f"public reusable retained root: {volume_id}", flush=True)
    except BaseException:
        print(f"public retained-root failure evidence preserved: {parent}", flush=True)
        raise
