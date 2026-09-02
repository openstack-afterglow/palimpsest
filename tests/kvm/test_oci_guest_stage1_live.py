"""Qualified actual-PID1/virtio-blk proof for the packaged guest stage-1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from palimpsest_local._oci_stage1_kvm_proof import (
    EVIDENCE_ENV,
    EVIDENCE_FILE_NAMES,
    FILESYSTEM_NEGATIVE_CONTROL_NAMES,
    FILESYSTEM_REJECTION_MARKER,
    NEGATIVE_CONTROL_NAMES,
    PREPARATION_FAILURE_MARKER,
    REJECTION_MARKER,
    SUCCESS_MARKER,
    _logical_line_count,
    run_oci_stage1_kvm_proof,
)

pytestmark = [pytest.mark.kvm, pytest.mark.stage1_kvm]


def test_packaged_stage1_verifies_filesystems_and_rejects_topology_and_filesystem_matrices() -> None:
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
    assert receipt["filesystem_verified"] is True
    assert receipt["root_filesystem_verified"] is True
    assert receipt["root_content_verified"] is False
    assert receipt["lower_filesystem_verified"] is True
    assert receipt["lower_content_verified"] is True
    assert receipt["mount_attempted"] is False
    assert _logical_line_count(result.console, SUCCESS_MARKER) == 1
    assert _logical_line_count(result.console, REJECTION_MARKER) == 0
    assert set(result.negative_consoles) == set(NEGATIVE_CONTROL_NAMES)
    for console in result.negative_consoles.values():
        assert _logical_line_count(console, REJECTION_MARKER) == 1
        assert _logical_line_count(console, SUCCESS_MARKER) == 0
        assert _logical_line_count(console, PREPARATION_FAILURE_MARKER) == 0
    assert set(result.filesystem_negative_consoles) == set(FILESYSTEM_NEGATIVE_CONTROL_NAMES)
    for console in result.filesystem_negative_consoles.values():
        assert _logical_line_count(console, FILESYSTEM_REJECTION_MARKER) == 1
        assert _logical_line_count(console, REJECTION_MARKER) == 0
        assert _logical_line_count(console, SUCCESS_MARKER) == 0
        assert _logical_line_count(console, PREPARATION_FAILURE_MARKER) == 0

    evidence_value = os.environ.get(EVIDENCE_ENV)
    if evidence_value is not None:
        evidence = Path(evidence_value)
        assert {path.name for path in evidence.iterdir()} == set(EVIDENCE_FILE_NAMES)
        assert all(path.stat().st_mode & 0o777 == 0o400 for path in evidence.iterdir())
