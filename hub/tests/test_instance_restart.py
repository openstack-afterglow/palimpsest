# ruff: noqa: F811
"""Main-command restart policy: Hub admission, the guest's restart-status capability and report.

Real SQL and instance API through `instance_hub`; the cloud is fake. The guest loop itself is run as
a subprocess in `test_instance_runner_resume.py`. Nothing here qualifies a real crash or OOM kill.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from test_instance_lifecycle import make_dev, trust_boundary  # noqa: F401
from test_instances import create_and_advance, guest_spec, instance_hub, request_body  # noqa: F401

from palimpsest_hub.instance_models import PalimpsestHubInstance

READINESS = ["/bin/true"]


async def ready(h, item):
    spec = guest_spec(h.provider)
    body = {"instance_id": item["id"], "status": "ready", "exit_code": None, "stdout": "", "stderr": ""}
    response = await h.client.put(
        "/v1/instance-bootstrap/" + item["id"] + "/result",
        json=body,
        headers={"Authorization": "Bearer " + spec["capability"]},
    )
    assert response.status_code == 204, response.text
    return spec


def status_body(item, **overrides):
    return {"instance_id": item["id"], "restarts": 1, "last_exit_code": -9, "state": "backoff", **overrides}


@pytest.mark.asyncio
async def test_a_service_instance_with_a_readiness_probe_restarts_by_default_and_reports_status(instance_hub):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body(readiness_argv=READINESS))).json()
    assert item["restart"] == {
        "policy": "unless-stopped",
        "restarts": 0,
        "last_exit_code": None,
        "state": None,
        "recorded_at": None,
    }
    spec = await ready(h, item)
    restart = spec["restart"]
    assert restart["policy"] == "unless-stopped" and restart["capability"] != spec["capability"]
    path = "/v1/instance-bootstrap/" + item["id"] + "/restart-status"
    auth = {"Authorization": "Bearer " + restart["capability"]}
    assert (await h.client.put(path, json=status_body(item), headers=auth)).status_code == 204
    running = status_body(item, restarts=2, last_exit_code=137, state="running")
    assert (await h.client.put(path, json=running, headers=auth)).status_code == 204
    shown = (await h.client.get("/v1/instances/" + item["id"])).json()
    assert shown["status"] == "ready"  # Observation only: a restart never changes the instance status.
    assert (shown["restart"]["restarts"], shown["restart"]["last_exit_code"], shown["restart"]["state"]) == (
        2,
        137,
        "running",
    )
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert restart["capability"] not in str(row.spec_json)


@pytest.mark.asyncio
async def test_restart_status_requires_its_own_capability_identity_and_a_running_instance(instance_hub):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body(readiness_argv=READINESS))).json()
    spec = guest_spec(h.provider)
    path = "/v1/instance-bootstrap/" + item["id"] + "/restart-status"
    auth = {"Authorization": "Bearer " + spec["restart"]["capability"]}
    # Not ready yet: nothing restartable is running.
    assert (await h.client.put(path, json=status_body(item), headers=auth)).status_code == 409
    await ready(h, item)
    bootstrap = {"Authorization": "Bearer " + spec["capability"]}
    assert (await h.client.put(path, json=status_body(item), headers=bootstrap)).status_code == 404
    assert (await h.client.put(path, json=status_body(item))).status_code == 404
    other = status_body(item, instance_id=str(uuid4()))
    assert (await h.client.put(path, json=other, headers=auth)).status_code == 409
    for invalid in ({"restarts": 0}, {"state": "stopped"}, {"last_exit_code": 4096}, {"extra": 1}):
        assert (await h.client.put(path, json=status_body(item, **invalid), headers=auth)).status_code == 422
    assert (await h.client.put(path, content=b"x" * 2048, headers=auth)).status_code == 413
    assert (await h.client.get("/v1/instances/" + item["id"])).json()["restart"]["restarts"] == 0
    assert (await h.client.delete("/v1/instances/" + item["id"])).status_code == 202
    assert (await h.client.put(path, json=status_body(item), headers=auth)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "policy"),
    [
        ({}, "no"),  # run-to-completion command: nothing to keep running
        ({"readiness_argv": READINESS, "restart": "no"}, "no"),
        ({"readiness_argv": READINESS, "restart": "unless-stopped"}, "unless-stopped"),
    ],
)
async def test_restart_policy_follows_the_request_and_only_a_restarting_guest_gets_the_capability(
    instance_hub, overrides, policy
):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body(**overrides))).json()
    assert item["restart"]["policy"] == policy
    assert ("restart" in guest_spec(h.provider)) is (policy == "unless-stopped")


@pytest.mark.asyncio
async def test_a_dev_instance_does_not_restart_unless_asked(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h, readiness_argv=READINESS)
    assert (await h.client.get("/v1/instances/" + instance_id)).json()["restart"]["policy"] == "no"
    assert "restart" not in guest_spec(h.provider)
    asked = await make_dev(h, readiness_argv=READINESS, restart="unless-stopped")
    assert (await h.client.get("/v1/instances/" + asked)).json()["restart"]["policy"] == "unless-stopped"
    assert guest_spec(h.provider)["restart"]["policy"] == "unless-stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"restart": "unless-stopped"},  # no readiness probe: a finished command is not restarted
        {"restart": "always", "readiness_argv": READINESS},
        {"restart": "unless-stopped", "readiness_argv": READINESS, "gpu_profile": "gpu"},
    ],
)
async def test_unsupported_restart_requests_are_refused_before_any_allocation(instance_hub, fields):
    h = instance_hub
    response = await h.client.post("/v1/instances", json=request_body(**fields))
    assert response.status_code == 422 and not h.provider.creates


@pytest.mark.asyncio
async def test_omitting_restart_keeps_the_request_replayable(instance_hub):
    h = instance_hub
    body = request_body(readiness_argv=READINESS)
    first = await h.client.post("/v1/instances", json=body)
    again = await h.client.post("/v1/instances", json=body)
    assert first.status_code == again.status_code == 202 and first.json()["id"] == again.json()["id"]
    changed = await h.client.post("/v1/instances", json={**body, "restart": "no"})
    assert changed.status_code == 409


@pytest.mark.asyncio
async def test_an_instance_created_before_the_option_existed_is_shown_as_not_restarting(instance_hub):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body(readiness_argv=READINESS))).json()
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        row.spec_json = {key: val for key, val in row.spec_json.items() if not key.startswith("restart")}
        await session.commit()
    assert (await h.client.get("/v1/instances/" + item["id"])).json()["restart"]["policy"] == "no"
