"""Qualified actual-PID1/virtio-blk proof for the packaged guest stage-1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from palimpsest_local._oci_stage1_kvm_proof import (
    ASSEMBLY_NEGATIVE_CONTROL_NAMES,
    ASSEMBLY_REJECTION_MARKER,
    EVIDENCE_ENV,
    EVIDENCE_FILE_NAMES,
    FILESYSTEM_NEGATIVE_CONTROL_NAMES,
    FILESYSTEM_REJECTION_MARKER,
    NEGATIVE_CONTROL_NAMES,
    PREPARATION_FAILURE_MARKER,
    REJECTION_MARKER,
    ROOT_TRANSITION_MARKER,
    ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES,
    ROOT_TRANSITION_REJECTION_MARKER,
    SUCCESS_MARKER,
    WORKLOAD_NEGATIVE_CONTROL_NAMES,
    WORKLOAD_NEGATIVE_REJECTION_MARKERS,
    WORKLOAD_STARTED_MARKER,
    _logical_line_count,
    run_oci_stage1_kvm_proof,
)

pytestmark = [pytest.mark.kvm, pytest.mark.stage1_kvm]


def test_packaged_stage1_supervises_workload_and_rejects_all_30_boot_control_matrices() -> None:
    if os.environ.get("PALIMPSEST_REQUIRE_STAGE1_KVM") != "1":
        pytest.skip("set PALIMPSEST_REQUIRE_STAGE1_KVM=1 on the qualified native Linux/KVM runner")

    result = run_oci_stage1_kvm_proof()

    receipt = result.receipt.to_dict()
    assert receipt["executed_boots"] == 30
    assert receipt["qualification"] == {
        "accelerator": "kvm",
        "architecture": "x86_64",
        "cpu": "host",
        "kvm_api_version": 12,
        "live_pid1": True,
    }
    assert receipt["root_assembly"] is True
    assert receipt["root_is_slash"] is True
    assert receipt["pivot_root"] is False
    assert receipt["switch_root"] is True
    assert receipt["root_transition"] == {
        "contract": "palimpsest.stage1-root-transition.v1",
        "method": "move-mount-chroot",
        "pid1_root_matches_slash": True,
        "pivot_root": False,
        "pseudo_filesystems": ["dev", "sys", "proc"],
        "root_filesystem": "overlay",
        "switch_root": True,
        "workload_started": False,
    }
    assert receipt["workload_started"] is True
    assert receipt["supervisor"] == {
        "contract": "palimpsest.guest-pid1-supervisor.v1",
        "descendant_status": 43,
        "forwarded_signal": 15,
        "main_status": 42,
        "process_group": True,
        "reaped_children": 2,
        "terminal_state": "parent-marker-then-fail-closed-wait",
    }
    assert set(receipt["workload_negative_controls"]) == set(WORKLOAD_NEGATIVE_CONTROL_NAMES)
    assert receipt["pre_mount_devices"] is True
    assert receipt["filesystem_verified"] is True
    assert receipt["root_filesystem_verified"] is True
    assert receipt["root_content_verified"] is False
    assert receipt["lower_filesystem_verified"] is True
    assert receipt["lower_content_verified"] is True
    assert receipt["mount_attempted"] is True
    assert receipt["root_filesystem_mounted"] is True
    assert receipt["lower_filesystems_mounted"] is True
    assert receipt["overlay_assembled"] is True
    assert _logical_line_count(result.console, SUCCESS_MARKER) == 1
    assert _logical_line_count(result.console, ROOT_TRANSITION_MARKER) == 1
    assert _logical_line_count(result.console, WORKLOAD_STARTED_MARKER) == 1
    assert _logical_line_count(result.console, REJECTION_MARKER) == 0
    assert _logical_line_count(result.console, ASSEMBLY_REJECTION_MARKER) == 0
    assert _logical_line_count(result.console, ROOT_TRANSITION_REJECTION_MARKER) == 0
    assert _logical_line_count(result.retained_console, SUCCESS_MARKER) == 1
    assert _logical_line_count(result.retained_console, ROOT_TRANSITION_MARKER) == 1
    assert _logical_line_count(result.retained_console, WORKLOAD_STARTED_MARKER) == 1
    assert set(result.negative_consoles) == set(NEGATIVE_CONTROL_NAMES)
    for console in result.negative_consoles.values():
        assert _logical_line_count(console, REJECTION_MARKER) == 1
        assert _logical_line_count(console, SUCCESS_MARKER) == 0
        assert _logical_line_count(console, PREPARATION_FAILURE_MARKER) == 0
        assert _logical_line_count(console, ROOT_TRANSITION_REJECTION_MARKER) == 0
    assert set(result.filesystem_negative_consoles) == set(FILESYSTEM_NEGATIVE_CONTROL_NAMES)
    for console in result.filesystem_negative_consoles.values():
        assert _logical_line_count(console, FILESYSTEM_REJECTION_MARKER) == 1
        assert _logical_line_count(console, REJECTION_MARKER) == 0
        assert _logical_line_count(console, SUCCESS_MARKER) == 0
        assert _logical_line_count(console, PREPARATION_FAILURE_MARKER) == 0
        assert _logical_line_count(console, ROOT_TRANSITION_REJECTION_MARKER) == 0
    assert set(result.assembly_negative_consoles) == set(ASSEMBLY_NEGATIVE_CONTROL_NAMES)
    for console in result.assembly_negative_consoles.values():
        assert _logical_line_count(console, ASSEMBLY_REJECTION_MARKER) == 1
        assert _logical_line_count(console, SUCCESS_MARKER) == 0
        assert _logical_line_count(console, ROOT_TRANSITION_REJECTION_MARKER) == 0
    assert set(result.root_transition_negative_consoles) == set(ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES)
    for console in result.root_transition_negative_consoles.values():
        assert _logical_line_count(console, ROOT_TRANSITION_REJECTION_MARKER) == 1
        assert _logical_line_count(console, REJECTION_MARKER) == 0
        assert _logical_line_count(console, FILESYSTEM_REJECTION_MARKER) == 0
        assert _logical_line_count(console, ASSEMBLY_REJECTION_MARKER) == 0
        assert _logical_line_count(console, SUCCESS_MARKER) == 0
        assert _logical_line_count(console, PREPARATION_FAILURE_MARKER) == 0
    assert set(result.workload_negative_consoles) == set(WORKLOAD_NEGATIVE_CONTROL_NAMES)
    for name, console in result.workload_negative_consoles.items():
        assert _logical_line_count(console, ROOT_TRANSITION_MARKER) == 1
        assert _logical_line_count(console, WORKLOAD_NEGATIVE_REJECTION_MARKERS[name]) == 1
        assert _logical_line_count(console, WORKLOAD_STARTED_MARKER) == 0
        assert _logical_line_count(console, SUCCESS_MARKER) == 0
        assert _logical_line_count(console, REJECTION_MARKER) == 0
        assert _logical_line_count(console, FILESYSTEM_REJECTION_MARKER) == 0
        assert _logical_line_count(console, ASSEMBLY_REJECTION_MARKER) == 0
        assert _logical_line_count(console, ROOT_TRANSITION_REJECTION_MARKER) == 0
        assert _logical_line_count(console, PREPARATION_FAILURE_MARKER) == 0

    evidence_value = os.environ.get(EVIDENCE_ENV)
    if evidence_value is not None:
        evidence = Path(evidence_value)
        assert {path.name for path in evidence.iterdir()} == set(EVIDENCE_FILE_NAMES)
        assert all(path.stat().st_mode & 0o777 == 0o400 for path in evidence.iterdir())
