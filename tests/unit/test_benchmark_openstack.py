from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from palimpsest_local.benchmark_openstack import run_openstack_environment
from palimpsest_local.errors import ArtifactValidationError


def _target(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    receipt = {
        "project_id": "project-one",
        "job_id": "job-one",
        "server_id": "server-one",
        "name": "owned-guest",
        "spec_digest": "sha256:original",
    }
    path = receipts / "job-one.json"
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    server = {
        "id": "server-one",
        "name": "owned-guest",
        "project_id": "project-one",
        "status": "ACTIVE",
        "metadata": {"palimpsest_job": "job-one", "palimpsest_spec": "sha256:original"},
    }
    connection = SimpleNamespace(
        current_project_id="project-one",
        compute=SimpleNamespace(get_server=lambda _: server),
        network=SimpleNamespace(
            ports=lambda **_: [
                {
                    "id": "port-one",
                    "device_id": "server-one",
                    "project_id": "project-one",
                    "fixed_ips": [{"ip_address": "10.0.0.2"}],
                }
            ],
            ips=lambda **_: [],
        ),
    )
    config = {
        "schema": "palimpsest.benchmark-openstack.v1",
        "project_id": "project-one",
        "server_id": "server-one",
        "job_id": "job-one",
        "receipts": str(receipts),
        "flavor_at_request": {"id": "forged", "ram": 999999},
        "ssh": {"host": "10.0.0.2"},
    }
    return config, connection, server


def test_matching_job_name_but_different_spec_is_not_an_authorized_benchmark_target(tmp_path):
    config, connection, server = _target(tmp_path)
    server["metadata"]["palimpsest_spec"] = "sha256:another-request"
    with pytest.raises(ArtifactValidationError, match="original identity"):
        run_openstack_environment(config, tmp_path / "evidence", connection=connection)
    assert not (tmp_path / "evidence").exists()


def test_caller_flavor_cannot_fill_a_missing_creation_time_snapshot(tmp_path):
    config, connection, _ = _target(tmp_path)
    with pytest.raises(ArtifactValidationError, match="creation receipt flavor snapshot"):
        run_openstack_environment(config, tmp_path / "evidence", connection=connection)
    assert not (tmp_path / "evidence").exists()
