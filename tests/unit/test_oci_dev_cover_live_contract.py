"""Portable source contract for the opt-in populated-image /dev proof."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_dev_cover_proof_is_explicit_bounded_and_uses_production_policies() -> None:
    test = (ROOT / "tests/kvm/test_oci_dev_cover_live.py").read_text()
    probe = (ROOT / "tests/kvm/assets/dev-cover-probe.c").read_text()
    assert 'PALIMPSEST_OCI_DEV_COVER_LIVE' in test
    assert '"planned_boots": 2, "executed_boots": 0' in test
    assert 'receipt.update(executed_boots=2, result="boots-complete-unqualified")' in test
    assert 'receipt["result"] = "passed"' in test
    assert '"probe_source_sha256": probe_sha256' in test
    assert '"-net", "none"' in test
    assert 'transition_target_policy_checked("/dev", TRANSITION_TARGET_DEV' in probe
    assert 'transition_target_ready_checked("/dev", TRANSITION_TARGET_DEV' in probe
    assert '"/trusted", (i64)"/dev", 0, MS_MOVE' in probe
    assert 'hold_filesystem("/trusted", 0x01021994, &trusted)' in probe
    assert 'verify_held_filesystem("/dev", &trusted)' in probe
    assert 'sc0(SYS_fork)' in probe
    assert 'sc3(SYS_close_range, 3, 0xffffffffU, 0)' in probe
    assert 'sc1(SYS_unshare, CLONE_NEWNS)' in probe
    assert 'make_safe_workload_stdio_aliases()' in probe
    assert 'safe_workload_dev_entries()' in probe
    assert probe.index('sc1(SYS_close, target_fd)') < probe.index(
        '"/trusted", (i64)"/dev", 0, MS_MOVE'
    )
    assert probe.index('DEV_COVER_PREFIX "REJECT') < probe.index(
        'DEV_COVER_PREFIX "TRUSTED_DEVTMPFS'
    )
    assert 'DEV_COVER_PREFIX "PARENT_DEVTMPFS_UNCHANGED' in probe
