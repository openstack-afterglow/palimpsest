"""Provider-boundary tests: real SQL/API/CAS, fake original-scoped cloud only.

These do not qualify a guest kernel/mount runtime. The separate Linux smoke must
exercise GUEST_RUNNER with real SquashFS/OverlayFS and an HTTPS callback.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.api import hub, instances
from palimpsest_hub.auth import get_os_conn
from palimpsest_hub.instance_models import PalimpsestHubInstance
from palimpsest_hub.models import Base, PalimpsestHubLayer, RuntimeBlobReference
from palimpsest_hub.services import instances as service
from palimpsest_hub.services.hub_store import (
    MEDIA_TYPE_LAYER_SQUASHFS,
    LocalPathBlobStore,
)

REAL_FRESH_WRITER = service.fresh_writer

PROJECT = "a" * 32
OTHER = "b" * 32
USER = "c" * 32
BASE = "sha256:" + "d" * 64
PAYLOAD = b"test SquashFS bytes served unchanged, not a guest execution proof"
DIGEST = "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()


class Provider:
    def __init__(self):
        self.servers_by_id = {}
        self.volumes_by_id = {}
        self.creates = []
        self.deletes = []
        self.stops = []
        self.network_owner = PROJECT
        self.group_owner = PROJECT
        self.arch = "x86_64"
        self.timeout_after_create = False
        self.extra_attachments = []
        self.compute = self
        self.network = self
        self.image = self
        self.block_storage = self
        self._afterglow_project_id = PROJECT
        self._afterglow_user_id = USER

    def get_image(self, image_id):
        return {
            "id": image_id,
            "status": "active",
            "architecture": self.arch,
            "disk_format": "qcow2",
            "container_format": "bare",
            "min_disk": 1,
            "min_ram": 512,
            "properties": {"palimpsest_digest": BASE},
        }

    def get_flavor(self, flavor_id):
        return {"id": flavor_id, "ram": 1024, "is_public": True}

    def get_network(self, network_id):
        return {"id": network_id, "project_id": self.network_owner}

    def get_security_group(self, group_id):
        return {"id": group_id, "project_id": self.group_owner}

    def create_server(self, **kwargs):
        self.creates.append(kwargs)
        server_id, volume_id = str(uuid4()), str(uuid4())
        row = {
            "id": server_id,
            "project_id": PROJECT,
            "metadata": kwargs["metadata"],
            "status": "ACTIVE",
            "root_device_name": "/dev/vda",
            "volume_id": volume_id,
        }
        self.servers_by_id[server_id] = row
        self.volumes_by_id[volume_id] = {
            "id": volume_id,
            "project_id": PROJECT,
            "bootable": True,
            "volume_image_metadata": {"image_id": kwargs["block_device_mapping_v2"][0]["uuid"]},
            "attachments": [{"server_id": server_id}],
        }
        if self.timeout_after_create:
            raise TimeoutError("secret provider diagnostics must not escape")
        return row

    def get_server(self, server_id):
        return self.servers_by_id.get(server_id)

    def servers(self, **kwargs):
        return iter(self.servers_by_id.values())

    def volume_attachments(self, server_id, **kwargs):
        server = self.servers_by_id[server_id]
        return [
            {"volume_id": server["volume_id"], "device": "/dev/vda", "delete_on_termination": True},
            *self.extra_attachments,
        ]

    def get_volume(self, volume_id):
        return self.volumes_by_id.get(volume_id)

    def delete_server(self, server_id, **kwargs):
        self.deletes.append(server_id)
        server = self.servers_by_id.pop(server_id, None)
        if server is not None:
            self.volumes_by_id.pop(server["volume_id"], None)

    def stop_server(self, server_id):
        self.stops.append(server_id)
        self.servers_by_id[server_id]["status"] = "SHUTOFF"


def request_body(**overrides):
    return {
        "request_id": str(uuid4()),
        "name": "actual-api-instance",
        "image_id": str(uuid4()),
        "flavor_id": "small",
        "network_id": str(uuid4()),
        "security_group_ids": [str(uuid4())],
        "layer_digests": [DIGEST],
        "command": ["/bin/echo", "actual command"],
        "boot_gib": 4,
        "writable_gib": 1,
        **overrides,
    }


@pytest.fixture
async def instance_hub(tmp_path, monkeypatch):
    # Import the image table before create_all, as the production bootstrap does.
    from palimpsest_hub import image_models  # noqa: F401

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'instances.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    store.blobs_dir.mkdir(parents=True, exist_ok=True)
    store.blob_path(DIGEST).write_bytes(PAYLOAD)
    settings = SimpleNamespace(
        palimpsest_hub_package_public_origin="https://hub.example.test",
        palimpsest_hub_instance_bootstrap_ttl_seconds=3600,
        palimpsest_hub_max_instances_per_project=16,
        palimpsest_hub_instance_max_boot_gib=256,
        palimpsest_hub_instance_max_writable_gib=64,
        palimpsest_hub_max_blob_bytes=1024 * 1024,
        palimpsest_hub_instance_base_images={},
        palimpsest_hub_instance_ca_certificates=(),
    )
    async with factory() as session:
        session.add_all(
            [
                PalimpsestHubLayer(
                    blob_digest=BASE,
                    size_bytes=1,
                    media_type="application/octet-stream",
                    disk_format="qcow2",
                    arch="x86_64",
                    config_digest=BASE,
                    name="base",
                    kind="cloud-image",
                    config_json={},
                    project_id=PROJECT,
                ),
                PalimpsestHubLayer(
                    blob_digest=DIGEST,
                    size_bytes=len(PAYLOAD),
                    media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                    arch="x86_64",
                    config_digest=DIGEST,
                    name="application",
                    kind="squashfs",
                    config_json={"base_image_digest": BASE},
                    project_id=PROJECT,
                ),
            ]
        )
        await session.commit()
    provider = Provider()
    app = FastAPI()
    app.include_router(instances.router, prefix="/v1")

    async def actor(request: Request):
        if request.headers.get("X-Deny"):
            raise HTTPException(403, "native human package policy denied")
        return {
            "project_id": request.headers.get("X-Project", PROJECT),
            "user_id": USER,
            "token": "fixture-original-human-token",
            "package_actions": set(
                request.headers.get("X-Actions", "packages:write,packages:inventory,packages:read").split(",")
            ),
            "is_system_admin": False,
        }

    async def conn():
        yield provider

    async def fresh(actor, conn):
        service.request_project(actor, conn)
        await hub._legacy_writer(actor)
        return actor

    app.dependency_overrides[hub._legacy_writer] = actor
    app.dependency_overrides[hub._legacy_inventory] = actor
    app.dependency_overrides[get_os_conn] = conn
    monkeypatch.setattr(instances, "_factory_or_503", lambda: factory)
    monkeypatch.setattr(instances, "_store_or_503", lambda: store)
    monkeypatch.setattr(instances, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "fresh_writer", fresh)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
        yield SimpleNamespace(client=client, provider=provider, factory=factory, store=store, settings=settings)
    await engine.dispose()


def guest_spec(provider):
    cloud = base64.b64decode(provider.creates[-1]["user_data"]).decode()
    payload = json.loads(cloud.split("\n", 1)[1])
    content = next(file["content"] for file in payload["write_files"] if file["path"].endswith("spec.json"))
    return json.loads(base64.b64decode(content))


@pytest.mark.asyncio
async def test_create_idempotency_durable_snapshot_and_provider_active_not_ready(instance_hub):
    h = instance_hub
    body = request_body()
    created = await h.client.post("/v1/instances", json=body)
    assert created.status_code == 202, created.text
    item = created.json()
    assert item["status"] == "booting" and item["result"] is None
    repeated = await h.client.post("/v1/instances", json=body)
    assert repeated.json()["id"] == item["id"] and len(h.provider.creates) == 1
    changed = await h.client.post("/v1/instances", json={**body, "command": ["/bin/false"]})
    assert changed.status_code == 409 and len(h.provider.creates) == 1
    observed = await h.client.get("/v1/instances/" + item["id"])
    assert observed.status_code == 200, observed.text
    assert observed.json()["provider_status"] == "ACTIVE" and observed.json()["status"] == "booting"
    guest = guest_spec(h.provider)
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.capability_hash == hashlib.sha256(guest["capability"].encode()).hexdigest()
        assert guest["capability"] not in json.dumps(row.spec_json)
        refs = list(
            await session.scalars(select(RuntimeBlobReference.digest).where(RuntimeBlobReference.resource_id == row.id))
        )
        assert set(refs) == {BASE, DIGEST}
    assert guest["capability"] not in created.text
    assert "token" not in json.dumps(guest)
    bdm = h.provider.creates[0]["block_device_mapping_v2"][0]
    assert bdm["delete_on_termination"] is True and bdm["source_type"] == "image"


@pytest.mark.asyncio
async def test_lost_create_response_reconciles_exact_marker_without_reallocation(instance_hub):
    h = instance_hub
    h.provider.timeout_after_create = True
    body = request_body()
    created = await h.client.post("/v1/instances", json=body)
    assert created.json()["status"] == "uncertain"
    assert "secret provider" not in created.text
    assert (await h.client.post("/v1/instances", json=body)).json()["id"] == created.json()["id"]
    detail = await h.client.get("/v1/instances/" + created.json()["id"])
    assert detail.json()["status"] == "booting" and detail.json()["server_id"]
    assert len(h.provider.creates) == 1


@pytest.mark.asyncio
async def test_guest_capability_exact_binding_result_sealing_and_delete_revocation(instance_hub):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    guest = guest_spec(h.provider)
    auth = {"Authorization": "Bearer " + guest["capability"]}
    path = "/v1/instance-bootstrap/" + item["id"]
    layer = await h.client.get(path + "/layers/" + DIGEST, headers=auth)
    assert layer.content == PAYLOAD and layer.headers["cache-control"] == "no-store"
    assert (await h.client.get(path + "/layers/" + BASE, headers=auth)).status_code == 404
    assert (await h.client.get(path + "/layers/" + DIGEST)).status_code == 404
    result = {"instance_id": item["id"], "status": "ready", "exit_code": 0, "stdout": "actual command\n", "stderr": ""}
    assert (
        await h.client.put(path + "/result", json={**result, "instance_id": str(uuid4())}, headers=auth)
    ).status_code == 409
    assert (await h.client.put(path + "/result", json={**result, "exit_code": None}, headers=auth)).status_code == 422
    assert (await h.client.put(path + "/result", json=result, headers=auth)).status_code == 204
    assert (await h.client.put(path + "/result", json=result, headers=auth)).status_code == 204
    assert (await h.client.put(path + "/result", json={**result, "stdout": "changed"}, headers=auth)).status_code == 409
    assert (await h.client.put(path + "/result", content=b"x" * 70001, headers=auth)).status_code == 413
    assert (await h.client.get("/v1/instances/" + item["id"])).json()["status"] == "ready"
    assert (await h.client.delete("/v1/instances/" + item["id"])).status_code == 202
    assert (await h.client.get(path + "/layers/" + DIGEST, headers=auth)).status_code == 404
    assert (await h.client.get("/v1/instances/" + item["id"])).json()["status"] == "deleted"
    assert (await h.client.delete("/v1/instances/" + item["id"])).json()["status"] == "deleted"
    assert h.store.exists(DIGEST)


@pytest.mark.asyncio
async def test_reuse_deleted_instance_exact_snapshot_and_no_rebuild(instance_hub):
    h = instance_hub
    original = (await h.client.post("/v1/instances", json=request_body())).json()
    await h.client.delete("/v1/instances/" + original["id"])
    await h.client.get("/v1/instances/" + original["id"])
    reused = await h.client.post(
        "/v1/instances", json=request_body(layer_digests=[], source_instance_id=original["id"])
    )
    assert reused.status_code == 202, reused.text
    assert reused.json()["source_instance_id"] == original["id"] and reused.json()["layer_digests"] == [DIGEST]
    assert len(h.provider.creates) == 2 and h.store.blob_path(DIGEST).read_bytes() == PAYLOAD
    async with h.factory() as session:
        old = await session.get(PalimpsestHubInstance, original["id"])
        assert old.status == "deleted" and old.spec_json["layer_digests"] == [DIGEST]


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", ["project", "network", "group", "arch", "boot", "policy"])
async def test_admission_failure_has_no_provider_effect(instance_hub, violation):
    h = instance_hub
    body = request_body()
    headers = {}
    if violation == "project":
        headers["X-Project"] = OTHER
    if violation == "network":
        h.provider.network_owner = OTHER
    if violation == "group":
        h.provider.group_owner = OTHER
    if violation == "arch":
        h.provider.arch = "aarch64"
    if violation == "boot":
        body["boot_gib"] = 257
    if violation == "policy":
        headers["X-Deny"] = "yes"
    response = await h.client.post("/v1/instances", json=body, headers=headers)
    assert response.status_code in (403, 422), response.text
    assert not h.provider.creates


@pytest.mark.asyncio
async def test_stop_and_cleanup_refuse_foreign_marker_and_unknown_dot_volume(instance_hub):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    assert (await h.client.post(path + "/stop")).status_code == 202
    assert (await h.client.get(path)).json()["status"] == "stopped"
    server = h.provider.servers_by_id[item["server_id"]]
    server["metadata"][service.MARKER] = str(uuid4())
    assert (await h.client.delete(path)).status_code == 409
    assert not h.provider.deletes
    server["metadata"][service.MARKER] = item["id"]
    h.provider.extra_attachments = [{"volume_id": str(uuid4()), "device": "/dev/vdb", "delete_on_termination": True}]
    assert (await h.client.delete(path)).status_code == 409
    assert not h.provider.deletes
    h.provider.extra_attachments[0]["delete_on_termination"] = False
    assert (await h.client.delete(path)).status_code == 202
    assert h.provider.deletes == [item["server_id"]]


@pytest.mark.asyncio
async def test_expired_capability_cannot_report_and_foreign_project_cannot_reuse(instance_hub):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    guest = guest_spec(h.provider)
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        row.capability_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    response = await h.client.get(
        "/v1/instance-bootstrap/" + item["id"] + "/layers/" + DIGEST,
        headers={"Authorization": "Bearer " + guest["capability"]},
    )
    assert response.status_code == 404
    assert (await h.client.get("/v1/instances/" + item["id"])).json()["error_code"] == "bootstrap_expired"
    h.provider._afterglow_project_id = OTHER
    foreign = await h.client.post(
        "/v1/instances",
        headers={"X-Project": OTHER},
        json=request_body(layer_digests=[], source_instance_id=item["id"]),
    )
    assert foreign.status_code == 404 and len(h.provider.creates) == 1


@pytest.mark.asyncio
async def test_explicit_image_job_derives_ordered_repeated_stack_and_default_runtime(instance_hub):
    from palimpsest_hub.image_models import PalimpsestHubImageJob

    h = instance_hub
    job_id = str(uuid4())
    runtime = {
        "entrypoint": ["/app/server"],
        "cmd": ["--port", "8080"],
        "env": ["PORT=8080"],
        "working_dir": "/app",
        "user": "1000:1000",
    }
    source = {"root_digest": BASE, "manifest_digest": BASE, "config_digest": BASE}
    async with h.factory() as session:
        layer = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == DIGEST))
        layer.config_json = {"instance_mode": "oci-layer", "base_image_digest": None}
        session.add(
            PalimpsestHubImageJob(
                id=job_id,
                project_id=PROJECT,
                created_by=USER,
                request_id=str(uuid4()),
                kind="registry",
                name="oci-source",
                request_digest=BASE,
                request_json={},
                status="complete",
                output_digests=[DIGEST, DIGEST],
                result_json={
                    "layers": [{"digest": DIGEST, "size_bytes": len(PAYLOAD)}] * 2,
                    "source": source,
                    "runtime": runtime,
                },
            )
        )
        await session.commit()
    created = await h.client.post(
        "/v1/instances",
        json=request_body(layer_digests=[], image_job_id=job_id, command=[], readiness_argv=["/app/check-ready"]),
    )
    assert created.status_code == 202, created.text
    assert created.json()["mode"] == "oci-chroot" and created.json()["layer_digests"] == [DIGEST, DIGEST]
    guest = guest_spec(h.provider)
    assert guest["command"] == ["/app/server", "--port", "8080"] and guest["runtime"] == runtime
    assert guest["mode"] == "oci-chroot" and len(guest["layers"]) == 2
    assert "PORT=8080" not in created.text
    original = created.json()
    await h.client.delete("/v1/instances/" + original["id"])
    await h.client.get("/v1/instances/" + original["id"])
    reused = await h.client.post(
        "/v1/instances", json=request_body(layer_digests=[], source_instance_id=original["id"], command=[])
    )
    assert reused.status_code == 202, reused.text
    assert guest_spec(h.provider)["runtime"] == runtime
    assert guest_spec(h.provider)["command"] == ["/app/server", "--port", "8080"]


@pytest.mark.asyncio
async def test_unobserved_create_never_reallocates_or_falsely_settles_delete(instance_hub):
    h = instance_hub

    def lost_without_observation(**kwargs):
        h.provider.creates.append(kwargs)
        raise TimeoutError("provider outcome unknown")

    h.provider.create_server = lost_without_observation
    body = request_body()
    created = (await h.client.post("/v1/instances", json=body)).json()
    assert created["status"] == "uncertain"
    path = "/v1/instances/" + created["id"]
    assert (await h.client.get(path)).json()["status"] == "uncertain"
    assert (await h.client.delete(path)).json()["status"] == "deleting"
    assert (await h.client.get(path)).json()["status"] == "deleting"
    assert (await h.client.post("/v1/instances", json=body)).json()["id"] == created["id"]
    assert len(h.provider.creates) == 1


@pytest.mark.asyncio
async def test_fresh_policy_revocation_before_effect_prevents_allocation(instance_hub, monkeypatch):
    h = instance_hub
    calls = 0

    async def revoke_before_effect(actor, conn):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(403, "fresh native write authority revoked")
        return actor

    monkeypatch.setattr(service, "fresh_writer", revoke_before_effect)
    response = await h.client.post("/v1/instances", json=request_body())
    assert response.status_code == 403 and not h.provider.creates


def test_instance_openapi_documents_bounded_result_body():
    app = FastAPI()
    app.include_router(instances.router, prefix="/v1")
    schema = app.openapi()
    body = schema["paths"]["/v1/instance-bootstrap/{instance_id}/result"]["put"]["requestBody"]
    assert (
        body["required"] is True
        and body["content"]["application/json"]["schema"]["properties"]["stdout"]["maxLength"] == 8192
    )


@pytest.mark.asyncio
async def test_bootstrap_result_redacts_bearer_equivalent_and_seals_observation_timestamp(instance_hub):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    guest = guest_spec(h.provider)
    response = await h.client.put(
        "/v1/instance-bootstrap/" + item["id"] + "/result",
        headers={"Authorization": "Bearer " + guest["capability"]},
        json={"instance_id": item["id"], "status": "ready", "exit_code": 0, "stdout": guest["capability"]},
    )
    assert response.status_code == 204
    detail = await h.client.get("/v1/instances/" + item["id"])
    assert guest["capability"] not in detail.text and "[redacted bootstrap capability]" in detail.text
    assert detail.json()["result_recorded_at"] and "point-in-time" in detail.json()["readiness_semantics"]


@pytest.mark.asyncio
@pytest.mark.parametrize("writable,boot", [(0, 16), (16, 16), (65, 128), (1025, 2048)])
async def test_invalid_disk_writable_capacity_rejected_before_provider_effect(instance_hub, writable, boot):
    h = instance_hub
    response = await h.client.post("/v1/instances", json=request_body(writable_gib=writable, boot_gib=boot))
    assert response.status_code == 422, response.text
    assert not h.provider.creates


@pytest.mark.asyncio
async def test_original_token_revoked_during_provider_inspection_never_admits_or_allocates(instance_hub, monkeypatch):
    from palimpsest_hub import auth

    h = instance_hub
    state = {"revoked": False}
    original_image = h.provider.get_image

    def image(image_id):
        result = original_image(image_id)
        state["revoked"] = True
        return result

    def validate(token, project):
        if state["revoked"]:
            raise HTTPException(401, "Original token expired")
        return {"token": token, "project_id": project, "user_id": USER}

    async def member(token_info):
        return {**token_info, "package_actions": {"packages:write"}}

    monkeypatch.setattr(h.provider, "get_image", image)
    monkeypatch.setattr(auth, "validate_token", validate)
    monkeypatch.setattr(auth, "get_package_member_info", member)
    monkeypatch.setattr(service, "fresh_writer", REAL_FRESH_WRITER)
    response = await h.client.post("/v1/instances", json=request_body())
    assert response.status_code == 401
    assert not h.provider.creates
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestHubInstance))).all() == []


@pytest.mark.asyncio
async def test_async_delete_survives_stop_and_provider_error_until_absence(instance_hub, monkeypatch):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    created = (await h.client.post("/v1/instances", json=request_body())).json()
    path = "/v1/instances/" + created["id"]

    def asynchronous_delete(server_id, **kwargs):
        h.provider.deletes.append(server_id)

    monkeypatch.setattr(h.provider, "delete_server", asynchronous_delete)
    assert (await h.client.delete(path)).json()["status"] == "deleting"
    assert (await h.client.post(path + "/stop")).status_code == 409
    assert not h.provider.stops
    h.provider.servers_by_id[created["server_id"]]["status"] = "ERROR"
    observed = (await h.client.get(path)).json()
    assert observed["status"] == "deleting" and observed["provider_status"] == "ERROR"
    h.provider.servers_by_id.pop(created["server_id"])
    h.provider.volumes_by_id.clear()
    assert (await h.client.get(path)).json()["status"] == "deleted"
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 202
    assert h.store.blob_path(DIGEST).read_bytes() == PAYLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unqualified", "qualified", "changed-content", "wrong-architecture", "data-only"])
async def test_unannotated_boot_image_requires_unchanged_operator_qualified_content(instance_hub, monkeypatch, case):
    h = instance_hub
    body = request_body()
    h.provider.arch = "aarch64" if case == "wrong-architecture" else None
    original = h.provider.get_image

    def image(identity):
        row = original(identity)
        row.update(os_hash_algo="sha512", os_hash_value="a" * 128)
        if case == "changed-content":
            row["os_hash_value"] = "b" * 128
        if case == "data-only":
            row["palimpsest_artifact_type"] = "external-layer-store"
        return row

    monkeypatch.setattr(h.provider, "get_image", image)
    if case != "unqualified":
        h.settings.palimpsest_hub_instance_base_images[body["image_id"]] = {
            "architecture": "x86_64",
            "os_type": "linux",
            "os_hash_algo": "sha512",
            "os_hash_value": "a" * 128,
        }
    response = await h.client.post("/v1/instances", json=body)
    if case == "qualified":
        assert response.status_code == 202 and response.json()["status"] == "booting"
        assert response.json()["result"] is None
    else:
        assert response.status_code == 422
        assert not h.provider.creates


@pytest.mark.asyncio
@pytest.mark.parametrize("actions", ["packages:write", "packages:write,packages:inventory", "packages:read"])
async def test_instance_payload_admission_requires_separate_read_and_write_grants(instance_hub, actions):
    h = instance_hub
    response = await h.client.post("/v1/instances", json=request_body(), headers={"X-Actions": actions})
    assert response.status_code == 403
    assert not h.provider.creates
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestHubInstance))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", [1, 2])
async def test_download_revocation_at_each_fresh_fence_prevents_capability_effect(instance_hub, monkeypatch, fence):
    from palimpsest_hub import auth

    h = instance_hub
    calls = 0

    def validate(token, project):
        return {"token": token, "project_id": project, "user_id": USER}

    async def member(token_info):
        nonlocal calls
        calls += 1
        actions = {"packages:write", "packages:inventory"}
        if calls < fence:
            actions.add("packages:read")
        return {**token_info, "package_actions": actions}

    monkeypatch.setattr(auth, "validate_token", validate)
    monkeypatch.setattr(auth, "get_package_member_info", member)
    monkeypatch.setattr(service, "fresh_writer", REAL_FRESH_WRITER)
    body = request_body()
    response = await h.client.post("/v1/instances", json=body)
    assert response.status_code == 403 and not h.provider.creates
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubInstance))).all()
        assert len(rows) == fence - 1
        if rows:
            assert rows[0].status == "error" and rows[0].error_code == "create_authority_revoked"
            assert rows[0].capability_hash is None
    if fence == 2:
        repeated = await h.client.post("/v1/instances", json=body)
        assert repeated.status_code == 202 and not h.provider.creates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["failed-create", "active", "previously-active", "bound", "ready", "foreign", "unknown"]
)
async def test_only_verified_empty_failed_create_can_delete_without_boot_volume(instance_hub, monkeypatch, case):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    server = h.provider.servers_by_id[item["server_id"]]
    server["status"] = "ACTIVE" if case == "active" else "ERROR"
    if case == "foreign":
        server["project_id"] = OTHER
    if case in ("bound", "ready", "previously-active"):
        async with h.factory() as session:
            row = await session.get(PalimpsestHubInstance, item["id"])
            if case == "bound":
                row.boot_volume_id = server["volume_id"]
            elif case == "ready":
                row.status, row.result_json = "ready", {"status": "ready"}
            else:
                row.provider_status = "ACTIVE"
            await session.commit()

    def attachments(*args, **kwargs):
        if case == "unknown":
            raise TimeoutError("not verified empty")
        return []

    monkeypatch.setattr(h.provider, "volume_attachments", attachments)
    response = await h.client.delete(path)
    if case == "failed-create":
        assert response.status_code == 202 and response.json()["status"] == "deleting"
        assert h.provider.deletes == [item["server_id"]]
        assert (await h.client.get(path)).json()["status"] == "deleted"
        assert (await h.client.delete(path)).json()["status"] == "deleted"
        assert len(h.provider.deletes) == 1 and len(h.provider.creates) == 1
    else:
        assert response.status_code == (502 if case == "unknown" else 409)
        assert not h.provider.deletes


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_before_delete", [False, True])
async def test_deleted_server_retains_capacity_until_bound_volume_disappears(
    instance_hub, monkeypatch, missing_before_delete
):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    assert (await h.client.get(path)).json()["boot_volume_id"]
    volume_id = h.provider.servers_by_id[item["server_id"]]["volume_id"]

    def delayed_volume_delete(server_id, **kwargs):
        h.provider.deletes.append(server_id)
        h.provider.servers_by_id.pop(server_id)

    monkeypatch.setattr(h.provider, "delete_server", delayed_volume_delete)
    if missing_before_delete:
        h.provider.servers_by_id.pop(item["server_id"])
    assert (await h.client.delete(path)).json()["status"] == "deleting"
    for _ in range(2):
        pending = (await h.client.get(path)).json()
        assert pending["status"] == "deleting" and pending["error_code"] == "provider_boot_volume_pending"
        assert (await h.client.delete(path)).json()["status"] == "deleting"
        assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    assert volume_id in h.provider.volumes_by_id
    h.provider.volumes_by_id.pop(volume_id)
    assert (await h.client.get(path)).json()["status"] == "deleted"
    assert (await h.client.delete(path)).json()["status"] == "deleted"
    assert len(h.provider.deletes) == (0 if missing_before_delete else 1)
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 202


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["observe", "delete"])
async def test_volume_observation_failure_never_fabricates_absence_or_frees_capacity(instance_hub, monkeypatch, via):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    await h.client.get(path)
    server = h.provider.servers_by_id.pop(item["server_id"])
    if via == "observe":
        async with h.factory() as session:
            row = await session.get(PalimpsestHubInstance, item["id"])
            row.status = "deleting"
            await session.commit()

    def unavailable(volume_id):
        raise TimeoutError("private provider diagnostics")

    monkeypatch.setattr(h.provider, "get_volume", unavailable)
    response = await (h.client.get(path) if via == "observe" else h.client.delete(path))
    assert response.status_code == 502 and "private provider diagnostics" not in response.text
    assert server["volume_id"] in h.provider.volumes_by_id
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.status != "deleted" and row.boot_volume_id == server["volume_id"]
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    assert not h.provider.deletes


@pytest.mark.asyncio
async def test_empty_failed_create_deletion_rechecks_before_provider_effect(instance_hub, monkeypatch):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    server = h.provider.servers_by_id[item["server_id"]]
    server["status"] = "ERROR"
    observations = 0

    def changing_attachments(*args, **kwargs):
        nonlocal observations
        observations += 1
        if observations == 1:
            return []
        return [{"volume_id": str(uuid4()), "device": "/dev/vdb", "delete_on_termination": True}]

    monkeypatch.setattr(h.provider, "volume_attachments", changing_attachments)
    response = await h.client.delete("/v1/instances/" + item["id"])
    assert response.status_code == 409 and observations == 2
    assert not h.provider.deletes
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.status == "deleting" and row.capability_hash is None


@pytest.mark.asyncio
async def test_failed_create_with_foreign_boot_attachment_is_never_deleted(instance_hub):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    server = h.provider.servers_by_id[item["server_id"]]
    server["status"] = "ERROR"
    h.provider.volumes_by_id[server["volume_id"]]["project_id"] = OTHER
    response = await h.client.delete("/v1/instances/" + item["id"])
    assert response.status_code == 409 and not h.provider.deletes


@pytest.mark.asyncio
async def test_late_valid_boot_attachment_requires_durable_binding_before_delete(instance_hub, monkeypatch):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    server = h.provider.servers_by_id[item["server_id"]]
    server["status"] = "ERROR"
    volume_id = server["volume_id"]
    original_attachments = h.provider.volume_attachments
    observations = 0

    def late_attachment(*args, **kwargs):
        nonlocal observations
        observations += 1
        return [] if observations == 1 else original_attachments(*args, **kwargs)

    def delayed_volume_delete(server_id, **kwargs):
        h.provider.deletes.append(server_id)
        h.provider.servers_by_id.pop(server_id)

    monkeypatch.setattr(h.provider, "volume_attachments", late_attachment)
    monkeypatch.setattr(h.provider, "delete_server", delayed_volume_delete)
    response = await h.client.delete(path)
    assert response.status_code == 409 and observations == 2
    assert not h.provider.deletes
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.status == "deleting" and row.boot_volume_id is None
    # An explicit retry admits and commits the newly observed original binding.
    response = await h.client.delete(path)
    assert response.status_code == 202 and response.json()["boot_volume_id"] == volume_id
    assert h.provider.deletes == [item["server_id"]]
    assert (await h.client.get(path)).json()["status"] == "deleting"
    assert volume_id in h.provider.volumes_by_id
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    h.provider.volumes_by_id.pop(volume_id)
    assert (await h.client.get(path)).json()["status"] == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,reason",
    [
        ("authority", "create_authority_revoked"),
        ("expired", "create_bootstrap_expired"),
        ("input-limit", "create_bootstrap_input_limit"),
    ],
)
async def test_definite_preallocation_failure_retains_reason_and_can_release_quota(
    instance_hub, monkeypatch, failure, reason
):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    fresh = service.fresh_writer
    calls = 0

    async def authority(actor, conn):
        nonlocal calls
        current = await fresh(actor, conn)
        calls += 1
        if failure == "authority" and calls == 2:
            raise HTTPException(403, "Original human authority revoked before allocation")
        return current

    monkeypatch.setattr(service, "fresh_writer", authority)
    if failure == "expired":
        h.settings.palimpsest_hub_instance_bootstrap_ttl_seconds = -1
    original_render = service.render_bootstrap
    if failure == "input-limit":
        monkeypatch.setattr(service, "render_bootstrap", lambda spec: "x" * 50000)
    body = request_body()
    failed = await h.client.post("/v1/instances", json=body)
    assert failed.status_code == (403 if failure == "authority" else 202)
    assert not h.provider.creates
    async with h.factory() as session:
        row = (await session.scalars(select(PalimpsestHubInstance))).one()
        instance_id = row.id
        assert row.status == "error" and row.error_code == reason
        assert row.server_id is None and row.boot_volume_id is None and row.capability_hash is None
    path = "/v1/instances/" + instance_id

    def unexpected_observation(*args, **kwargs):
        raise AssertionError("A definite pre-allocation outcome requires no provider inventory")

    monkeypatch.setattr(h.provider, "servers", unexpected_observation)
    monkeypatch.setattr(h.provider, "get_server", unexpected_observation)
    for _ in range(2):
        observed = await h.client.get(path)
        assert observed.status_code == 200
        assert observed.json()["status"] == "error" and observed.json()["error_code"] == reason
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    assert (await h.client.delete(path, headers={"X-Actions": "packages:read"})).status_code == 403
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    # Restored write authority can discard only the proven unallocated intent;
    # read authority is deliberately not required for lifecycle cleanup.
    deleted = await h.client.delete(path, headers={"X-Actions": "packages:write"})
    assert deleted.status_code == 202 and deleted.json()["status"] == "deleted"
    assert deleted.json()["error_code"] == reason
    assert (await h.client.get(path)).json()["error_code"] == reason
    assert (await h.client.delete(path)).json()["status"] == "deleted"
    assert (await h.client.post("/v1/instances", json=body)).json()["status"] == "deleted"
    assert not h.provider.creates and not h.provider.deletes
    h.settings.palimpsest_hub_instance_bootstrap_ttl_seconds = 3600
    monkeypatch.setattr(service, "render_bootstrap", original_render)
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 202
    assert len(h.provider.creates) == 1
