"""Existing root claims must preserve the shared parent through tool callbacks."""

import stat

import pytest
import test_oci_shared_traversal as shared_tests

from palimpsest_local import oci_root_volume as volumes
from palimpsest_local.errors import StateError
from palimpsest_local.oci_store import ArtifactLeaseOwner

case = shared_tests.case


@pytest.mark.parametrize("retained", [False, True])
def test_existing_claim_rejects_parent_drift_after_qemu_info(case, retained):
    shared_tests._join(case)
    volume_id = case.plan.root_volume["volume_id"]
    record = volumes.load_oci_root_volume(case.roots, volume_id, runner=case.runner).record
    owner = ArtifactLeaseOwner(record.attached_run_id, record.attached_run_name, "root-lower")
    if retained:
        record = volumes.release_oci_root_volume(
            case.roots,
            volume_id,
            owner=owner,
            lower_graph_digest=record.lower_graph_digest,
            delete=False,
            runner=case.runner,
        )
    _, record_path, _ = volumes._paths(case.roots, volume_id)
    before = record_path.read_bytes()
    calls = []

    def runner(argv):
        result = case.runner(argv)
        if argv[:2] == ["qemu-img", "info"]:
            calls.append(argv)
            case.roots.oci_root_volumes.chmod(0o700)
        return result

    with pytest.raises(StateError):
        volumes.claim_oci_root_volume(
            case.roots,
            volume_id,
            owner=owner,
            lower_graph_digest=record.lower_graph_digest,
            size_bytes=record.size_bytes,
            retention_policy=record.retention_policy,
            runner=runner,
        )
    assert calls
    assert record_path.read_bytes() == before
    assert stat.S_IMODE(case.roots.oci_root_volumes.stat().st_mode) == 0o700
