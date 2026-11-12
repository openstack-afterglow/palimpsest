"""Lifecycle route contracts with ASGI and injected service/provider boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError

from palimpsest_hub.api import instances
from palimpsest_hub.auth import get_os_conn, get_package_manager_info
from palimpsest_hub.services import instance_lifecycle as lifecycle


@pytest.fixture
async def lifecycle_hub(monkeypatch):
    factory = object()
    provider = object()
    store = object()
    settings = SimpleNamespace(palimpsest_hub_package_public_origin="https://hub.example.test")
    actor = {
        "project_id": "project-id",
        "user_id": "user-id",
        "token": "original-human-token",
        "package_actions": {"packages:inventory", "packages:manage", "packages:write"},
    }

    async def authenticated_actor(request: Request):
        if request.headers.get("X-Read-Only"):
            return {**actor, "package_actions": {"packages:inventory"}}
        if request.headers.get("X-Actions"):
            return {**actor, "package_actions": set(request.headers["X-Actions"].split(","))}
        return actor

    async def connection():
        yield provider

    app = FastAPI()
    app.include_router(instances.router, prefix="/v1")
    app.dependency_overrides[get_package_manager_info] = authenticated_actor
    app.dependency_overrides[get_os_conn] = connection
    monkeypatch.setattr(instances, "_factory_or_503", lambda: factory)
    monkeypatch.setattr(instances, "_store_or_503", lambda: store)
    monkeypatch.setattr(instances, "get_settings", lambda: settings)
    calls = {}
    for name in ("ingest_report", "resume", "renew_trust", "connection_info"):
        calls[name] = AsyncMock(return_value={"instance_id": "service-result", "operation": name})
        monkeypatch.setattr(lifecycle, name, calls[name], raising=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            instances=instances,
            lifecycle=lifecycle,
            calls=calls,
            factory=factory,
            provider=provider,
            store=store,
            settings=settings,
            actor=actor,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "suffix", "function", "status"),
    [
        ("POST", "/resume", "resume", 202),
        ("POST", "/lifecycle/renew", "renew_trust", 200),
        ("GET", "/connection", "connection_info", 200),
    ],
)
async def test_lifecycle_routes_forward_exact_authenticated_context(lifecycle_hub, verb, suffix, function, status):
    h = lifecycle_hub
    instance_id = str(uuid4())
    response = await h.client.request(verb, "/v1/instances/" + instance_id + suffix)
    assert response.status_code == status, response.text
    assert response.json() == {"instance_id": "service-result", "operation": function}
    h.calls[function].assert_awaited_once_with(h.factory, h.actor, h.provider, instance_id, h.settings)
    assert all(not mock.await_count for name, mock in h.calls.items() if name != function)
    if function == "connection_info":
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["/resume", "/lifecycle/renew"])
async def test_lifecycle_mutations_require_instance_controller(lifecycle_hub, suffix):
    h = lifecycle_hub
    response = await h.client.post("/v1/instances/" + str(uuid4()) + suffix, headers={"X-Read-Only": "1"})
    assert response.status_code == 403
    assert all(not mock.await_count for mock in h.calls.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(("suffix", "function"), [("/resume", "resume"), ("/lifecycle/renew", "renew_trust")])
async def test_lifecycle_mutations_admit_creator_without_publish_authority(lifecycle_hub, suffix, function):
    h = lifecycle_hub
    instance_id = str(uuid4())
    response = await h.client.post(
        "/v1/instances/" + instance_id + suffix, headers={"X-Actions": "packages:manage_own"}
    )
    assert response.status_code == (202 if function == "resume" else 200), response.text
    h.calls[function].assert_awaited_once_with(
        h.factory, {**h.actor, "package_actions": {"packages:manage_own"}}, h.provider, instance_id, h.settings
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["/resume", "/lifecycle/renew"])
async def test_lifecycle_mutations_refuse_publish_only_actor(lifecycle_hub, suffix):
    h = lifecycle_hub
    response = await h.client.post("/v1/instances/" + str(uuid4()) + suffix, headers={"X-Actions": "packages:write"})
    assert response.status_code == 403
    assert all(not mock.await_count for mock in h.calls.values())


@pytest.mark.asyncio
async def test_connection_allows_inventory_actor(lifecycle_hub):
    h = lifecycle_hub
    instance_id = str(uuid4())
    response = await h.client.get("/v1/instances/" + instance_id + "/connection", headers={"X-Read-Only": "1"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    h.calls["connection_info"].assert_awaited_once_with(
        h.factory, {**h.actor, "package_actions": {"packages:inventory"}}, h.provider, instance_id, h.settings
    )


@pytest.mark.asyncio
async def test_guest_session_report_forwards_capability_and_probe(lifecycle_hub):
    h = lifecycle_hub
    instance_id = str(uuid4())
    token = "guest-session-capability-" + "x" * 32
    probe = "PALIMPSEST-UTMP-OK\nPALIMPSEST-SELF 42\nPALIMPSEST-END\n"
    response = await h.client.put(
        "/v1/instance-bootstrap/" + instance_id + "/sessions",
        headers={"Authorization": "Bearer " + token},
        json={"probe": probe},
    )
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["Cache-Control"] == "no-store"
    h.calls["ingest_report"].assert_awaited_once_with(h.factory, instance_id, token, probe)


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer short", "Basic " + "x" * 40, "Bearer " + "x" * 129])
async def test_guest_session_report_requires_bounded_bearer(lifecycle_hub, authorization):
    h = lifecycle_hub
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await h.client.put(
        "/v1/instance-bootstrap/" + str(uuid4()) + "/sessions", headers=headers, json={"probe": "text"}
    )
    assert response.status_code == 404
    h.calls["ingest_report"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer short"])
async def test_guest_session_report_rejects_unauthenticated_large_body_before_read_or_parse(
    lifecycle_hub, monkeypatch, authorization
):
    h = lifecycle_hub
    consumed = []

    async def body():
        consumed.append(True)
        yield b"x" * 5_000_000

    def unexpected_parse(*args, **kwargs):
        pytest.fail("unauthenticated guest report was parsed")

    monkeypatch.setattr(h.instances.InstanceSessionReport, "model_validate_json", unexpected_parse)
    headers = {"Content-Type": "application/json", "Content-Length": "5000000"}
    if authorization:
        headers["Authorization"] = authorization
    response = await h.client.put(
        "/v1/instance-bootstrap/" + str(uuid4()) + "/sessions", headers=headers, content=body()
    )
    assert response.status_code == 404
    assert consumed == []
    h.calls["ingest_report"].assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_session_report_caps_stream_before_parsing(lifecycle_hub, monkeypatch):
    h = lifecycle_hub

    async def body():
        yield b"x" * 100
        yield b"x" * (6 * 65536 + 64 - 99)

    def unexpected_parse(*args, **kwargs):
        pytest.fail("over-cap guest report was parsed")

    monkeypatch.setattr(h.instances.InstanceSessionReport, "model_validate_json", unexpected_parse)
    response = await h.client.put(
        "/v1/instance-bootstrap/" + str(uuid4()) + "/sessions",
        headers={"Authorization": "Bearer " + "x" * 32, "Content-Type": "application/json"},
        content=body(),
    )
    assert response.status_code == 413
    h.calls["ingest_report"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"probe": "text", "extra": True}, {"probe": "x" * 65537}, {"probe": 1}, {}])
async def test_guest_session_report_rejects_invalid_body(lifecycle_hub, body):
    h = lifecycle_hub
    response = await h.client.put(
        "/v1/instance-bootstrap/" + str(uuid4()) + "/sessions",
        headers={"Authorization": "Bearer " + "x" * 32},
        json=body,
    )
    assert response.status_code == 422
    h.calls["ingest_report"].assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_session_report_accepts_probe_at_limit(lifecycle_hub):
    h = lifecycle_hub
    response = await h.client.put(
        "/v1/instance-bootstrap/" + str(uuid4()) + "/sessions",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={"probe": "x" * 65536},
    )
    assert response.status_code == 204
    h.calls["ingest_report"].assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_session_report_accepts_worst_case_escaped_probe_at_limit(lifecycle_hub):
    h = lifecycle_hub
    instance_id = str(uuid4())
    token = "x" * 32
    response = await h.client.put(
        "/v1/instance-bootstrap/" + instance_id + "/sessions",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        content=b'{"probe":"' + b"\\u0000" * 65536 + b'"}',
    )
    assert response.status_code == 204, response.text
    h.calls["ingest_report"].assert_awaited_once_with(h.factory, instance_id, token, "\x00" * 65536)


def instance_request():
    return {
        "request_id": str(uuid4()),
        "name": "dev-box",
        "image_id": str(uuid4()),
        "flavor_id": "m1.small",
        "network_id": str(uuid4()),
        "boot_gib": 20,
        "layer_digests": ["sha256:" + "a" * 64],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["service", "dev"])
async def test_create_passes_lifecycle_and_key_name_to_service(lifecycle_hub, monkeypatch, mode):
    h = lifecycle_hub
    create = AsyncMock(return_value={"id": "created"})
    monkeypatch.setattr(h.instances.service, "create", create)
    body = {**instance_request(), "lifecycle": mode, "key_name": "user@example.test key-1"}
    response = await h.client.post("/v1/instances", json=body)
    assert response.status_code == 202, response.text
    expected = h.instances.InstanceRequest.model_validate(body).model_dump(mode="json")
    create.assert_awaited_once_with(h.factory, h.store, h.settings, h.actor, h.provider, expected)
    assert expected["lifecycle"] == mode
    assert expected["key_name"] == body["key_name"]


@pytest.mark.asyncio
async def test_instance_request_defaults_to_service_without_key(lifecycle_hub):
    request = lifecycle_hub.instances.InstanceRequest.model_validate(instance_request())
    assert request.lifecycle == "service"
    assert request.key_name is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields", [{"lifecycle": "x"}, {"key_name": "bad/key"}, {"key_name": ""}, {"key_name": "x" * 256}, {"extra": True}]
)
async def test_instance_request_rejects_invalid_lifecycle_key_and_extra(lifecycle_hub, fields):
    with pytest.raises(ValidationError):
        lifecycle_hub.instances.InstanceRequest.model_validate({**instance_request(), **fields})


@pytest.mark.asyncio
async def test_dev_instance_rejects_cluster_profile_but_accepts_gpu_profile(lifecycle_hub):
    model = lifecycle_hub.instances.InstanceRequest
    body = {**instance_request(), "lifecycle": "dev"}
    with pytest.raises(ValidationError, match="lifecycle=dev excludes cluster_profile"):
        model.model_validate({**body, "cluster_profile": "k3s-server"})
    assert model.model_validate({**body, "gpu_profile": "rtx"}).gpu_profile == "rtx"


@pytest.mark.asyncio
async def test_lifecycle_operation_ids_are_explicit(lifecycle_hub):
    paths = lifecycle_hub.app.openapi()["paths"]
    for path, verb, operation in (
        ("/v1/instance-bootstrap/{instance_id}/sessions", "put", "report_instance_sessions"),
        ("/v1/instances/{instance_id}/resume", "post", "resume_hub_instance"),
        ("/v1/instances/{instance_id}/lifecycle/renew", "post", "renew_hub_instance_lifecycle"),
        ("/v1/instances/{instance_id}/connection", "get", "get_hub_instance_connection"),
    ):
        assert paths[path][verb]["operationId"] == operation
    report_body = paths["/v1/instance-bootstrap/{instance_id}/sessions"]["put"]["requestBody"]
    assert report_body["required"] is True
    schema = report_body["content"]["application/json"]["schema"]
    assert schema["properties"]["probe"]["maxLength"] == 65536
    assert schema["additionalProperties"] is False
