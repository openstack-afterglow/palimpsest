from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from palimpsest_hub.services import native_instances, preflight

PROJECT = "p" * 32


class FakeConn:
    """Read-only fake OpenStack connection that counts every allocation attempt."""

    def __init__(
        self,
        *,
        services=None,
        instances=(10, 0),
        volumes=(10, 0),
        gigabytes=(1000, 0),
        cores=(16, 0),
        ram=(32768, 0),
        ports=(100, 0, 0),
        flavor=(2, 2048),
        project=PROJECT,
    ):
        self._afterglow_project_id = project
        self.allocations: list[str] = []
        self.endpoint_regions: list[str | None] = []
        self._services = set(services if services is not None else preflight.REQUIRED_SERVICES)
        conn = self

        def get_endpoint(service_type, interface=None, region_name=None):
            conn.endpoint_regions.append(region_name)
            return "https://x" if service_type in conn._services else None

        self.session = SimpleNamespace(get_endpoint=get_endpoint)
        self.compute = SimpleNamespace(
            get_limits=lambda: SimpleNamespace(
                absolute=SimpleNamespace(
                    instances=instances[0],
                    instances_used=instances[1],
                    total_cores=cores[0],
                    total_cores_used=cores[1],
                    total_ram=ram[0],
                    total_ram_used=ram[1],
                )
            ),
            get_flavor=lambda flavor_id: (
                SimpleNamespace(vcpus=flavor[0], ram=flavor[1]) if flavor is not None else None
            ),
            create_server=lambda **kw: conn.allocations.append("server"),
        )
        self.block_storage = SimpleNamespace(
            get_limits=lambda: SimpleNamespace(
                absolute=SimpleNamespace(
                    max_total_volumes=volumes[0],
                    total_volumes_used=volumes[1],
                    max_total_volume_gigabytes=gigabytes[0],
                    total_gigabytes_used=gigabytes[1],
                )
            ),
            create_volume=lambda **kw: conn.allocations.append("volume"),
        )
        self.network = SimpleNamespace(
            get_quota=lambda project_id, details: {
                "ports": {"limit": ports[0], "used": ports[1], "reserved": ports[2]}
            },
            create_port=lambda **kw: conn.allocations.append("port"),
        )


def actor(actions=("packages:write",), project=PROJECT):
    return {"project_id": project, "package_actions": list(actions)}


NEED = preflight.PreflightNeed(volume_gib=8, flavor_id="small", ports=1)


def allocate(conn):
    preflight.require(conn, actor(), NEED)
    conn.compute.create_server()
    conn.block_storage.create_volume()
    conn.network.create_port()


def test_all_checks_passing_allows_allocation():
    conn = FakeConn()
    allocate(conn)
    assert conn.allocations == ["server", "volume", "port"]


@pytest.mark.parametrize(
    ("conn", "status", "check", "service"),
    [
        (FakeConn(services={"compute", "network", "image"}), 424, "catalog", "volumev3"),
        (FakeConn(services={"network", "image", "volumev3"}), 424, "catalog", "compute"),
        (FakeConn(project="q" * 32), 403, "token_scope", "identity"),
        (FakeConn(instances=(3, 3)), 409, "quota", "compute"),
        (FakeConn(volumes=(5, 5)), 409, "quota", "volumev3"),
        (FakeConn(gigabytes=(10, 5)), 409, "quota", "volumev3"),
        (FakeConn(cores=(1, 0)), 409, "quota", "compute"),
        (FakeConn(ram=(2048, 1024)), 409, "quota", "compute"),
        (FakeConn(ports=(1, 1, 0)), 409, "quota", "network"),
        (FakeConn(cores=(None, None)), 409, "quota", "compute"),
        (FakeConn(flavor=None), 422, "resource", "compute"),
    ],
)
def test_failure_is_structured_and_nothing_is_allocated(conn, status, check, service):
    with pytest.raises(HTTPException) as raised:
        allocate(conn)
    assert raised.value.status_code == status
    detail = raised.value.detail
    assert detail["code"] == "preflight_failed"
    assert any(item["check"] == check and item["service"] == service and item["action"] for item in detail["failures"])
    assert conn.allocations == []


def test_transient_flavor_lookup_failure_is_not_reported_as_bad_input():
    conn = FakeConn()

    def unavailable(flavor_id):
        raise TimeoutError("compute timeout")

    conn.compute.get_flavor = unavailable
    with pytest.raises(HTTPException) as raised:
        allocate(conn)
    failures = raised.value.detail["failures"]
    assert raised.value.status_code == 409
    assert not any(item["check"] == "resource" for item in failures)
    assert any("flavor lookup failed: TimeoutError" in item["detail"] for item in failures)
    assert conn.allocations == []


def test_missing_policy_action_is_403_and_allocates_nothing():
    conn = FakeConn()
    with pytest.raises(HTTPException) as raised:
        preflight.require(conn, actor(actions=("packages:read",)), NEED)
    assert raised.value.status_code == 403
    assert raised.value.detail["failures"][0]["check"] == "policy"
    assert conn.allocations == []


def test_every_failed_check_is_reported_together_and_quota_waits_for_catalog():
    conn = FakeConn(services={"compute"}, project="q" * 32, instances=(1, 1))
    failures = preflight.run(conn, actor(actions=()), NEED)
    checks = [item["check"] for item in failures]
    assert checks.count("catalog") == 3
    assert {"token_scope", "policy"} <= set(checks)
    assert "quota" not in checks


def test_unlimited_quota_and_exact_fit_pass_but_one_over_fails():
    assert preflight.run(FakeConn(instances=(-1, 99), volumes=(-1, 5), gigabytes=(-1, 5)), actor(), NEED) == []
    assert preflight.run(FakeConn(gigabytes=(13, 5)), actor(), NEED) == []
    assert [i["service"] for i in preflight.run(FakeConn(gigabytes=(12, 5)), actor(), NEED)] == ["volumev3"]


def test_quota_checks_use_flavor_size_reserved_ports_and_fail_closed_fields():
    core = preflight.run(FakeConn(cores=(1, 0)), actor(), NEED)
    assert any(item["service"] == "compute" and "core quota" in item["detail"] for item in core)
    ram = preflight.run(FakeConn(ram=(2048, 1024)), actor(), NEED)
    assert any(item["service"] == "compute" and "RAM quota" in item["detail"] for item in ram)
    ports = preflight.run(FakeConn(ports=(2, 0, 2)), actor(), NEED)
    assert any(item["service"] == "network" and "port quota" in item["detail"] for item in ports)
    missing = preflight.run(FakeConn(ports=(None, 0, 0)), actor(), NEED)
    assert any(item["service"] == "network" and "quota unreadable" in item["detail"] for item in missing)


def test_catalog_resolution_is_region_specific():
    conn = FakeConn()
    assert preflight.run(conn, actor(), NEED, interface="internal", region_name="RegionOne") == []
    assert conn.endpoint_regions and set(conn.endpoint_regions) == {"RegionOne"}


def test_shared_file_system_is_required_only_when_the_request_uses_it():
    assert preflight.run(FakeConn(), actor(), preflight.PreflightNeed()) == []
    failures = preflight.run(FakeConn(), actor(), preflight.PreflightNeed(shared_files=True))
    assert [(i["check"], i["service"]) for i in failures] == [("catalog", "shared-file-system")]


@pytest.mark.parametrize(("phase", "flavor"), [("cpu", "cpu.small"), ("gpu", "gpu.medium")])
def test_native_preflight_uses_granted_boot_size_and_phase_flavor(phase, flavor):
    need = native_instances._native_preflight_need(
        {"boot_gib": 32, "flavors": {"cpu": "cpu.small", "gpu": "gpu.medium"}},
        {"phase": phase},
    )
    assert need.volumes == 1 and need.volume_gib == 32
    assert need.flavor_id == flavor and need.ports == 1 and need.shared_files


def test_native_preflight_rejects_missing_operator_sizing():
    with pytest.raises(HTTPException, match="grant lacks boot volume or flavor sizing"):
        native_instances._native_preflight_need({"flavors": {}}, {"phase": "cpu"})


def test_unreadable_limits_fail_closed_instead_of_passing():
    conn = FakeConn()
    conn.compute.get_limits = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    failures = preflight.run(conn, actor(), NEED)
    assert failures and failures[0]["check"] == "quota"
