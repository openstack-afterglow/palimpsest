from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from palimpsest_hub.services import preflight

PROJECT = "p" * 32


class FakeConn:
    """Read-only fake OpenStack connection that counts every allocation attempt."""

    def __init__(self, *, services=None, instances=(10, 0), volumes=(10, 0), gigabytes=(1000, 0), project=PROJECT):
        self._afterglow_project_id = project
        self.allocations: list[str] = []
        self._services = set(services if services is not None else preflight.REQUIRED_SERVICES)
        conn = self
        self.session = SimpleNamespace(
            get_endpoint=lambda service_type, interface=None: "https://x" if service_type in conn._services else None
        )
        self.compute = SimpleNamespace(
            get_limits=lambda: SimpleNamespace(
                absolute=SimpleNamespace(instances=instances[0], instances_used=instances[1])
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


def actor(actions=("packages:write",), project=PROJECT):
    return {"project_id": project, "package_actions": list(actions)}


NEED = preflight.PreflightNeed(volume_gib=8)


def allocate(conn):
    preflight.require(conn, actor(), NEED)
    conn.compute.create_server()
    conn.block_storage.create_volume()


def test_all_checks_passing_allows_allocation():
    conn = FakeConn()
    allocate(conn)
    assert conn.allocations == ["server", "volume"]


@pytest.mark.parametrize(
    ("conn", "status", "check", "service"),
    [
        (FakeConn(services={"compute", "network", "image"}), 424, "catalog", "volumev3"),
        (FakeConn(services={"network", "image", "volumev3"}), 424, "catalog", "compute"),
        (FakeConn(project="q" * 32), 403, "token_scope", "identity"),
        (FakeConn(instances=(3, 3)), 409, "quota", "compute"),
        (FakeConn(volumes=(5, 5)), 409, "quota", "volumev3"),
        (FakeConn(gigabytes=(10, 5)), 409, "quota", "volumev3"),
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


def test_shared_file_system_is_required_only_when_the_request_uses_it():
    assert preflight.run(FakeConn(), actor(), preflight.PreflightNeed()) == []
    failures = preflight.run(FakeConn(), actor(), preflight.PreflightNeed(shared_files=True))
    assert [(i["check"], i["service"]) for i in failures] == [("catalog", "shared-file-system")]


def test_unreadable_limits_fail_closed_instead_of_passing():
    conn = FakeConn()
    conn.compute.get_limits = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    failures = preflight.run(conn, actor(), NEED)
    assert failures and failures[0]["check"] == "quota"
