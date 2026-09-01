"""Qualified actual-PID1/virtio-blk proof for the packaged guest stage-1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from palimpsest_local._oci_stage1_kvm_proof import (
    EVIDENCE_ENV,
    REJECTION_MARKER,
    SUCCESS_MARKER,
    _logical_line_count,
    run_oci_stage1_kvm_proof,
)

pytestmark = [pytest.mark.kvm, pytest.mark.stage1_kvm]


def test_packaged_stage1_runs_as_pid1_and_checks_readonly_virtio_transport() -> None:
    if os.environ.get("PALIMPSEST_REQUIRE_STAGE1_KVM") != "1":
        pytest.skip("set PALIMPSEST_REQUIRE_STAGE1_KVM=1 on the qualified native Linux/KVM runner")

    result = run_oci_stage1_kvm_proof()

    receipt = result.receipt.to_dict()
    assert receipt["qualification"] == {
        "accelerator": "kvm",
        "architecture": "x86_64",
        "cpu": "host",
        "kvm_api_version": 12,
        "live_pid1": True,
    }
    assert receipt["root_assembly"] is False
    assert receipt["pre_mount_devices"] is True
    assert receipt["filesystem_verified"] is False
    assert receipt["content_verified"] is False
    assert receipt["mount_attempted"] is False
    assert _logical_line_count(result.console, SUCCESS_MARKER) == 1
    assert _logical_line_count(result.console, REJECTION_MARKER) == 0
    assert _logical_line_count(result.writable_console, REJECTION_MARKER) == 1
    assert _logical_line_count(result.writable_console, SUCCESS_MARKER) == 0

    evidence_value = os.environ.get(EVIDENCE_ENV)
    if evidence_value is not None:
        evidence = Path(evidence_value)
        assert {path.name for path in evidence.iterdir()} == {
            "console.bin",
            "receipt.json",
            "writable-console.bin",
        }
        assert all(path.stat().st_mode & 0o777 == 0o400 for path in evidence.iterdir())
