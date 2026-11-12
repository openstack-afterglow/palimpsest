"""Provider-boundary tests: real SQL/API/CAS, fake original-scoped cloud only.

These do not qualify a guest kernel/mount runtime. The separate Linux smoke must
exercise GUEST_RUNNER with real SquashFS/OverlayFS and an HTTPS callback.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.api import hub, instances
from palimpsest_hub.auth import get_os_conn
from palimpsest_hub.instance_models import PalimpsestHubInstance
from palimpsest_hub.models import Base, PalimpsestHubLayer, RuntimeBlobReference
from palimpsest_hub.native_models import PalimpsestNativeInstance
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
        self.volume_creates = []
        self.volume_deletes = []
        self.volume_reads = []
        self.volume_lists = []
        self.server_lists = []
        self.volume_status = "available"
        self.timeout_after_volume_create = False
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
        self.session = SimpleNamespace(
            get_endpoint=lambda service_type, interface=None, region_name=None: "https://endpoint.invalid"
        )

    def get_limits(self):
        return SimpleNamespace(
            absolute=SimpleNamespace(
                instances=-1,
                instances_used=0,
                total_cores=-1,
                total_cores_used=0,
                total_ram=-1,
                total_ram_used=0,
                max_total_volumes=-1,
                total_volumes_used=0,
                max_total_volume_gigabytes=-1,
                total_gigabytes_used=0,
            )
        )

    def get_quota(self, project_id, details=True):
        return {"ports": {"limit": -1, "used": 0, "reserved": 0}}

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
        return {"id": flavor_id, "ram": 1024, "disk": 0, "vcpus": 1, "is_public": True}

    def fetch_flavor_extra_specs(self, flavor):
        return {**flavor, "extra_specs": {}}

    def get_network(self, network_id):
        return {"id": network_id, "project_id": self.network_owner}

    def get_security_group(self, group_id):
        return {"id": group_id, "project_id": self.group_owner}

    def create_volume(self, **kwargs):
        self.volume_creates.append(kwargs)
        volume_id = str(uuid4())
        row = {
            "id": volume_id,
            "project_id": PROJECT,
            "name": kwargs["name"],
            "size": kwargs["size"],
            "status": self.volume_status,
            "metadata": dict(kwargs["metadata"]),
            "bootable": True,
            "volume_image_metadata": {"image_id": kwargs["image_id"]},
            "attachments": [],
        }
        self.volumes_by_id[volume_id] = row
        if self.timeout_after_volume_create:
            raise TimeoutError("private Cinder diagnostics must not escape")
        return row

    def volumes(self, **kwargs):
        self.volume_lists.append(kwargs)
        return iter(self.volumes_by_id.values())

    def delete_volume(self, volume_id):
        self.volume_deletes.append(volume_id)
        self.volumes_by_id.pop(volume_id, None)

    def create_server(self, **kwargs):
        self.creates.append(kwargs)
        server_id = str(uuid4())
        mapping = kwargs["block_device_mapping_v2"][0]
        volume_id = mapping["uuid"]
        assert mapping == {
            "uuid": volume_id,
            "source_type": "volume",
            "destination_type": "volume",
            "boot_index": "0",
            "delete_on_termination": True,
        }
        volume = self.volumes_by_id[volume_id]
        assert volume["status"] == "available" and volume["attachments"] == []
        row = {
            "id": server_id,
            "project_id": PROJECT,
            "metadata": kwargs["metadata"],
            "status": "ACTIVE",
            "root_device_name": "/dev/vda",
            "volume_id": volume_id,
        }
        self.servers_by_id[server_id] = row
        volume.update(status="in-use", attachments=[{"server_id": server_id}])
        if self.timeout_after_create:
            raise TimeoutError("secret provider diagnostics must not escape")
        return row

    def get_server(self, server_id):
        return self.servers_by_id.get(server_id)

    def servers(self, **kwargs):
        self.server_lists.append(kwargs)
        return iter(self.servers_by_id.values())

    def volume_attachments(self, server_id, **kwargs):
        server = self.servers_by_id[server_id]
        return [
            {"volume_id": server["volume_id"], "device": "/dev/vda", "delete_on_termination": True},
            *self.extra_attachments,
        ]

    def get_volume(self, volume_id):
        self.volume_reads.append(volume_id)
        from openstack.exceptions import NotFoundException

        if volume_id not in self.volumes_by_id:
            raise NotFoundException("exact volume not found", http_status=404)
        return self.volumes_by_id[volume_id]

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
    clock = SimpleNamespace(now=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is not None else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", FixedDatetime)
    from palimpsest_hub.services import instance_cluster_runtime

    monkeypatch.setattr(instance_cluster_runtime, "datetime", FixedDatetime)
    monkeypatch.setattr(instances, "datetime", FixedDatetime)
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
        palimpsest_hub_instance_gpu_profiles={},
        palimpsest_hub_instance_cluster_profiles={},
        palimpsest_hub_build_worker_key=SecretStr("test-worker-secret-" + "x" * 32),
        palimpsest_hub_instance_ca_certificates=(),
        os_interface="internal",
        os_region_name="RegionOne",
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
            "user_id": request.headers.get("X-User", USER),
            "token": "fixture-original-human-token",
            "package_actions": set(
                request.headers.get(
                    "X-Actions", "packages:write,packages:inventory,packages:read,packages:manage"
                ).split(",")
            ),
            "is_system_admin": False,
        }

    async def conn():
        yield provider

    async def fresh(actor, conn):
        service.request_project(actor, conn)
        await hub._legacy_writer(actor)
        return actor

    async def controller(request: Request):
        return await hub._instance_controller(await actor(request))

    async def control(actor, conn, row):
        service.request_project(actor, conn)
        service.require_instance_control(actor, row)
        return actor

    app.dependency_overrides[hub._legacy_writer] = actor
    app.dependency_overrides[hub._legacy_inventory] = actor
    app.dependency_overrides[hub._instance_controller] = controller
    app.dependency_overrides[instances.get_package_manager_info] = actor
    app.dependency_overrides[get_os_conn] = conn
    monkeypatch.setattr(instances, "_factory_or_503", lambda: factory)
    monkeypatch.setattr(instances, "_store_or_503", lambda: store)
    monkeypatch.setattr(instances, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "fresh_writer", fresh)
    monkeypatch.setattr(service, "fresh_instance_controller", control)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
        yield SimpleNamespace(
            client=client, provider=provider, factory=factory, store=store, settings=settings, clock=clock
        )
    await engine.dispose()


async def advance_instance(h, item, *, operation="create-server", headers=None):
    return await h.client.post(
        "/v1/instances/" + item["id"] + "/advance",
        json={"operation": operation, "request_id": item["request_id"], "boot_volume_id": item["boot_volume_id"]},
        headers=headers,
    )


async def create_and_advance(h, **kwargs):
    """Exercise both explicit API writes for existing guest/runtime proof fixtures."""
    accepted = await h.client.post("/v1/instances", **kwargs)
    if accepted.status_code == 202 and accepted.json().get("provider_phase") == "volume_ready":
        return await advance_instance(h, accepted.json(), headers=kwargs.get("headers"))
    return accepted


def guest_spec(provider):
    cloud = base64.b64decode(provider.creates[-1]["user_data"]).decode()
    payload = json.loads(cloud.split("\n", 1)[1])
    content = next(file["content"] for file in payload["write_files"] if file["path"].endswith("spec.json"))
    return json.loads(base64.b64decode(content))


@pytest.mark.asyncio
async def test_create_idempotency_durable_snapshot_and_provider_active_not_ready(instance_hub):
    h = instance_hub
    body = request_body()
    created = await create_and_advance(h, json=body)
    assert created.status_code == 202, created.text
    item = created.json()
    assert item["status"] == "booting" and item["result"] is None
    repeated = await create_and_advance(h, json=body)
    assert repeated.json()["id"] == item["id"] and len(h.provider.creates) == 1
    changed = await create_and_advance(h, json={**body, "command": ["/bin/false"]})
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
    assert bdm["delete_on_termination"] is True and bdm["source_type"] == "volume"
    assert bdm["uuid"] == item["boot_volume_id"]
    assert h.provider.volume_creates[0]["image_id"] == body["image_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("break_it", "status", "check"),
    [
        (
            lambda p: setattr(
                p,
                "session",
                SimpleNamespace(get_endpoint=lambda service_type, interface=None, region_name=None: None),
            ),
            424,
            "catalog",
        ),
        (
            lambda p: setattr(
                p, "get_limits", lambda: SimpleNamespace(absolute=SimpleNamespace(instances=1, instances_used=1))
            ),
            409,
            "quota",
        ),
    ],
)
async def test_failed_preflight_returns_structured_4xx_and_allocates_nothing(instance_hub, break_it, status, check):
    h = instance_hub
    break_it(h.provider)
    response = await create_and_advance(h, json=request_body())
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "preflight_failed" and any(item["check"] == check for item in detail["failures"])
    assert h.provider.creates == h.provider.volume_creates == []


@pytest.mark.asyncio
async def test_preflight_network_calls_never_run_on_the_event_loop_thread(instance_hub):
    import threading

    h = instance_hub
    loop_thread = threading.get_ident()
    seen = []
    endpoint = h.provider.session.get_endpoint
    limits = h.provider.get_limits
    h.provider.session.get_endpoint = lambda service_type, interface=None, region_name=None: (
        seen.append(threading.get_ident()) or endpoint(service_type, interface, region_name)
    )
    h.provider.get_limits = lambda: seen.append(threading.get_ident()) or limits()
    flavor = h.provider.get_flavor
    quota = h.provider.get_quota
    h.provider.get_flavor = lambda flavor_id: seen.append(threading.get_ident()) or flavor(flavor_id)
    h.provider.get_quota = lambda project_id, details=True: (
        seen.append(threading.get_ident()) or quota(project_id, details)
    )
    created = await create_and_advance(h, json=request_body())
    assert created.status_code == 202, created.text
    assert len(seen) >= 6 and loop_thread not in seen


@pytest.mark.asyncio
async def test_quota_is_rechecked_before_server_post(instance_hub):
    h = instance_hub
    accepted = await h.client.post("/v1/instances", json=request_body())
    assert accepted.status_code == 202, accepted.text
    item = accepted.json()
    assert item["provider_phase"] == "volume_ready"
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == []
    h.provider.get_limits = lambda: SimpleNamespace(
        absolute=SimpleNamespace(
            instances=-1,
            instances_used=0,
            total_cores=0,
            total_cores_used=0,
            total_ram=-1,
            total_ram_used=0,
        )
    )
    response = await advance_instance(h, item)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "preflight_failed"
    assert any(entry["service"] == "compute" and "core quota" in entry["detail"] for entry in detail["failures"])
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == []
    assert h.provider.volume_deletes == [] and len(h.provider.volumes_by_id) == 1
    row = (await h.client.get("/v1/instances/" + item["id"])).json()
    assert row["error_code"] == "preflight_failed", row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_at", "stage"),
    [(3, "after volume_create_uncertain commit"), (5, "after nova_create_uncertain commit")],
)
async def test_quota_exhausted_after_uncertain_commit_sends_no_provider_post(instance_hub, monkeypatch, fail_at, stage):
    from fastapi import HTTPException

    h = instance_hub
    calls = []
    real = service.preflight.require

    def counted(*args, **kwargs):
        calls.append(1)
        if len(calls) == fail_at:
            raise HTTPException(409, {"code": "preflight_failed", "failures": []})
        return real(*args, **kwargs)

    monkeypatch.setattr(service.preflight, "require", counted)
    accepted = await h.client.post("/v1/instances", json=request_body())
    if fail_at == 3:
        assert accepted.status_code == 409, (stage, accepted.text)
        assert h.provider.volume_creates == [] and h.provider.creates == []
        return
    assert accepted.status_code == 202, accepted.text
    response = await advance_instance(h, accepted.json())
    assert response.status_code == 409, (stage, response.text)
    assert len(calls) == fail_at
    assert h.provider.creates == [] and h.provider.volume_deletes == []
    assert len(h.provider.volumes_by_id) == 1


@pytest.mark.asyncio
async def test_replay_of_an_existing_request_survives_a_later_quota_exhaustion(instance_hub):
    h = instance_hub
    body = request_body()
    first = await create_and_advance(h, json=body)
    assert first.status_code == 202, first.text
    h.provider.get_limits = lambda: SimpleNamespace(absolute=SimpleNamespace(instances=1, instances_used=1))
    again = await create_and_advance(h, json=body)
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    assert len(h.provider.creates) == 1


@pytest.mark.asyncio
async def test_lost_create_response_reconciles_exact_marker_without_reallocation(instance_hub):
    h = instance_hub
    h.provider.timeout_after_create = True
    body = request_body()
    created = await create_and_advance(h, json=body)
    assert created.json()["status"] == "uncertain"
    assert "secret provider" not in created.text
    assert (await create_and_advance(h, json=body)).json()["id"] == created.json()["id"]
    detail = await h.client.get("/v1/instances/" + created.json()["id"])
    assert detail.json()["status"] == "booting" and detail.json()["server_id"]
    assert len(h.provider.creates) == 1


@pytest.mark.asyncio
async def test_guest_capability_exact_binding_result_sealing_and_delete_revocation(instance_hub):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body())).json()
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
    original = (await create_and_advance(h, json=request_body())).json()
    await h.client.delete("/v1/instances/" + original["id"])
    await h.client.get("/v1/instances/" + original["id"])
    reused = await create_and_advance(h, json=request_body(layer_digests=[], source_instance_id=original["id"]))
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
    response = await create_and_advance(h, json=body, headers=headers)
    assert response.status_code in (403, 422), response.text
    assert not h.provider.creates


@pytest.mark.asyncio
async def test_stop_and_cleanup_refuse_foreign_marker_and_unknown_dot_volume(instance_hub):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body())).json()
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
    item = (await create_and_advance(h, json=request_body())).json()
    guest = guest_spec(h.provider)
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        row.capability_expires_at = h.clock.now - timedelta(seconds=1)
        await session.commit()
    response = await h.client.get(
        "/v1/instance-bootstrap/" + item["id"] + "/layers/" + DIGEST,
        headers={"Authorization": "Bearer " + guest["capability"]},
    )
    assert response.status_code == 404
    assert (await h.client.get("/v1/instances/" + item["id"])).json()["error_code"] == "bootstrap_expired"
    h.provider._afterglow_project_id = OTHER
    foreign = await create_and_advance(
        h,
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
    created = await create_and_advance(
        h,
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
    reused = await create_and_advance(
        h, json=request_body(layer_digests=[], source_instance_id=original["id"], command=[])
    )
    assert reused.status_code == 202, reused.text
    assert guest_spec(h.provider)["runtime"] == runtime
    assert guest_spec(h.provider)["command"] == ["/app/server", "--port", "8080"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_error", [TimeoutError("provider outcome unknown"), HTTPException(403, "denied")])
async def test_unobserved_create_never_reallocates_or_falsely_settles_delete(instance_hub, provider_error):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1

    def lost_without_observation(**kwargs):
        h.provider.creates.append(kwargs)
        h.clock.now += timedelta(seconds=h.settings.palimpsest_hub_instance_bootstrap_ttl_seconds)
        raise provider_error

    h.provider.create_server = lost_without_observation
    body = request_body()
    created = (await create_and_advance(h, json=body)).json()
    assert created["status"] == "uncertain" and created["error_code"] == "provider_create_uncertain"
    volume_id = created["boot_volume_id"]
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, created["id"])
        assert row.server_id is None and row.boot_volume_id == volume_id and row.capability_hash is not None
        assert service.preparation(row)["server_create_attempted"] is True
    path = "/v1/instances/" + created["id"]
    assert (await h.client.get(path)).json()["status"] == "uncertain"
    assert (await h.client.delete(path)).json()["status"] == "deleting"
    assert (await h.client.get(path)).json()["status"] == "deleting"
    assert (await create_and_advance(h, json=body)).json()["id"] == created["id"]
    assert len(h.provider.creates) == 1
    assert h.provider.volume_deletes == [] and set(h.provider.volumes_by_id) == {volume_id}
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, created["id"])
        assert row.status == "deleting" and row.error_code == "provider_create_uncertain"
        assert row.server_id is None and row.boot_volume_id == volume_id and row.capability_hash is None
        refs = await session.scalars(
            select(RuntimeBlobReference.digest).where(RuntimeBlobReference.resource_id == row.id)
        )
        assert set(refs) == {BASE, DIGEST}


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
    response = await create_and_advance(h, json=request_body())
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
    item = (await create_and_advance(h, json=request_body())).json()
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
    response = await create_and_advance(h, json=request_body(writable_gib=writable, boot_gib=boot))
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
    response = await create_and_advance(h, json=request_body())
    assert response.status_code == 401
    assert not h.provider.creates
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestHubInstance))).all() == []


@pytest.mark.asyncio
async def test_async_delete_survives_stop_and_provider_error_until_absence(instance_hub, monkeypatch):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    created = (await create_and_advance(h, json=request_body())).json()
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
    assert (await create_and_advance(h, json=request_body())).status_code == 202
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
    response = await create_and_advance(h, json=body)
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
    response = await create_and_advance(h, json=request_body(), headers={"X-Actions": actions})
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
    response = await create_and_advance(h, json=body)
    assert response.status_code == 403 and not h.provider.creates
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubInstance))).all()
        assert len(rows) == fence - 1
        if rows:
            assert rows[0].status == "error" and rows[0].error_code == "create_authority_revoked"
            assert rows[0].capability_hash is None
    if fence == 2:
        repeated = await create_and_advance(h, json=body)
        assert repeated.status_code == 202 and not h.provider.creates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["failed-create", "active", "previously-active", "bound", "ready", "foreign", "unknown"]
)
async def test_historical_server_without_attachment_never_proves_empty_boot_census(instance_hub, monkeypatch, case):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    server = h.provider.servers_by_id[item["server_id"]]
    server["status"] = "ACTIVE" if case == "active" else "ERROR"
    if case == "foreign":
        server["project_id"] = OTHER
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        row.spec_json = {key: val for key, val in row.spec_json.items() if key != "_boot_prepare"}
        row.boot_volume_id = server["volume_id"] if case == "bound" else None
        row.provider_status = "ACTIVE" if case == "previously-active" else None
        if case == "ready":
            row.status, row.result_json = "ready", {"status": "ready"}
        await session.commit()

    def attachments(*args, **kwargs):
        if case == "unknown":
            raise TimeoutError("not verified empty")
        return []

    monkeypatch.setattr(h.provider, "volume_attachments", attachments)
    legacy = next(entry for entry in (await h.client.get("/v1/instances")).json() if entry["id"] == item["id"])
    assert legacy["provider_phase"] == "legacy" and legacy["server_create_attempted"] is None
    response = await h.client.delete(path)
    assert response.status_code == (502 if case == "unknown" else 409), response.text
    assert h.provider.deletes == [] and h.provider.volume_deletes == []
    assert set(h.provider.servers_by_id) == {item["server_id"]}
    assert set(h.provider.volumes_by_id) == {server["volume_id"]}
    if case == "failed-create":
        async with h.factory() as session:
            row = await session.get(PalimpsestHubInstance, item["id"])
            assert row.boot_volume_id is None and row.error_code == "provider_boot_volume_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_before_delete", [False, True])
async def test_deleted_server_retains_capacity_until_bound_volume_disappears(
    instance_hub, monkeypatch, missing_before_delete
):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    item = (await create_and_advance(h, json=request_body())).json()
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
        assert (await create_and_advance(h, json=request_body())).status_code == 429
    assert volume_id in h.provider.volumes_by_id
    h.provider.volumes_by_id.pop(volume_id)
    assert (await h.client.get(path)).json()["status"] == "deleted"
    assert (await h.client.delete(path)).json()["status"] == "deleted"
    assert len(h.provider.deletes) == (0 if missing_before_delete else 1)
    assert (await create_and_advance(h, json=request_body())).status_code == 202


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["observe", "delete"])
@pytest.mark.parametrize("root_observation", ["unbound", "none", "404"])
async def test_legacy_missing_server_requires_bound_root_actual_404(instance_hub, monkeypatch, via, root_observation):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    item = (await create_and_advance(h, json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    server = h.provider.servers_by_id.pop(item["server_id"])
    volume_id = server["volume_id"]
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        row.spec_json = {key: val for key, val in row.spec_json.items() if key != "_boot_prepare"}
        row.boot_volume_id = None if root_observation == "unbound" else volume_id
        if via == "observe":
            row.status = "deleting"
        await session.commit()
    original_get_volume = h.provider.get_volume
    if root_observation == "none":

        def unavailable(identity):
            assert identity == volume_id
            return None

        monkeypatch.setattr(h.provider, "get_volume", unavailable)
    elif root_observation == "404":
        h.provider.volumes_by_id.pop(volume_id)
    response = await (h.client.get(path) if via == "observe" else h.client.delete(path))
    assert response.status_code == (502 if root_observation == "none" else 200 if via == "observe" else 202)
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        if root_observation == "404":
            assert row.status == "deleted" and row.boot_volume_id == volume_id
        else:
            assert row.status != "deleted"
            if root_observation == "unbound":
                assert row.status == "deleting" and row.error_code == "provider_boot_volume_unknown"
                assert row.boot_volume_id is None
            else:
                assert row.boot_volume_id == volume_id
    if root_observation != "404":
        observed = await h.client.get(path)
        assert observed.status_code == (502 if root_observation == "none" and via == "observe" else 200)
        if observed.status_code == 200:
            assert observed.json()["status"] != "deleted"
        assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
        assert volume_id in h.provider.volumes_by_id
    else:
        monkeypatch.setattr(h.provider, "get_volume", original_get_volume)
        assert (await h.client.post("/v1/instances", json=request_body())).status_code == 202
    assert h.provider.deletes == [] and h.provider.volume_deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["observe", "delete"])
async def test_volume_observation_failure_never_fabricates_absence_or_frees_capacity(instance_hub, monkeypatch, via):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    item = (await create_and_advance(h, json=request_body())).json()
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
    assert (await create_and_advance(h, json=request_body())).status_code == 429
    assert not h.provider.deletes


@pytest.mark.asyncio
async def test_empty_attachment_observation_blocks_delete_before_any_provider_effect(instance_hub, monkeypatch):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body())).json()
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
    assert response.status_code == 409 and observations == 1
    assert not h.provider.deletes
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.server_id == item["server_id"] and row.boot_volume_id == server["volume_id"]


@pytest.mark.asyncio
async def test_failed_create_with_foreign_boot_attachment_is_never_deleted(instance_hub):
    h = instance_hub
    item = (await create_and_advance(h, json=request_body())).json()
    server = h.provider.servers_by_id[item["server_id"]]
    server["status"] = "ERROR"
    h.provider.volumes_by_id[server["volume_id"]]["project_id"] = OTHER
    response = await h.client.delete("/v1/instances/" + item["id"])
    assert response.status_code == 409 and not h.provider.deletes


@pytest.mark.asyncio
async def test_late_valid_boot_attachment_requires_durable_binding_before_delete(instance_hub, monkeypatch):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    item = (await create_and_advance(h, json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    server = h.provider.servers_by_id[item["server_id"]]
    server["status"] = "ERROR"
    volume_id = server["volume_id"]
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        row.spec_json = {key: val for key, val in row.spec_json.items() if key != "_boot_prepare"}
        row.boot_volume_id = None
        row.provider_status = None
        await session.commit()
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
    assert response.status_code == 409 and observations == 1
    assert not h.provider.deletes
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.server_id == item["server_id"] and row.boot_volume_id is None
    # An explicit retry admits and commits the newly observed original binding.
    response = await h.client.delete(path)
    assert response.status_code == 202 and response.json()["boot_volume_id"] == volume_id
    assert h.provider.deletes == [item["server_id"]]
    assert (await h.client.get(path)).json()["status"] == "deleting"
    assert volume_id in h.provider.volumes_by_id
    assert (await create_and_advance(h, json=request_body())).status_code == 429
    h.provider.volumes_by_id.pop(volume_id)
    assert (await h.client.get(path)).json()["status"] == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,reason",
    [
        ("authority", "create_authority_revoked"),
        ("expired", "create_bootstrap_expired"),
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
    body = request_body()
    failed = await create_and_advance(h, json=body)
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
    assert (await create_and_advance(h, json=request_body())).status_code == 429
    assert (await h.client.delete(path, headers={"X-Actions": "packages:read"})).status_code == 403
    assert (await create_and_advance(h, json=request_body())).status_code == 429
    # Restored manager authority can discard only the proven unallocated intent;
    # read authority is deliberately not required for lifecycle cleanup.
    deleted = await h.client.delete(path, headers={"X-Actions": "packages:write,packages:manage"})
    assert deleted.status_code == 202 and deleted.json()["status"] == "deleted"
    assert deleted.json()["error_code"] == reason
    assert (await h.client.get(path)).json()["error_code"] == reason
    assert (await h.client.delete(path)).json()["status"] == "deleted"
    assert (await create_and_advance(h, json=body)).json()["status"] == "deleted"
    assert not h.provider.creates and not h.provider.deletes
    h.settings.palimpsest_hub_instance_bootstrap_ttl_seconds = 3600
    assert (await create_and_advance(h, json=request_body())).status_code == 202
    assert len(h.provider.creates) == 1


async def gpu_request(h, monkeypatch):
    """Only SQL/provider fixtures, never a live hardware qualification."""
    from palimpsest_hub.image_models import PalimpsestHubImageJob

    job_id = str(uuid4())
    async with h.factory() as session:
        layer = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == DIGEST))
        layer.config_json = {"base_image_digest": None}
        session.add(
            PalimpsestHubImageJob(
                id=job_id,
                project_id=PROJECT,
                created_by=USER,
                request_id=str(uuid4()),
                kind="registry",
                name="gpu-source",
                request_digest=BASE,
                request_json={},
                status="complete",
                output_digests=[DIGEST],
                result_json={
                    "source": {"root_digest": BASE, "manifest_digest": BASE, "config_digest": BASE},
                    "runtime": {
                        "entrypoint": [],
                        "cmd": ["/app/proof"],
                        "env": [],
                        "working_dir": "/",
                        "user": "1000:1000",
                    },
                },
            )
        )
        await session.commit()
    body = request_body(layer_digests=[], image_job_id=job_id, gpu_profile="rtx-qualified", command=[])
    h.settings.palimpsest_hub_instance_gpu_profiles["rtx-qualified"] = {
        "image_id": body["image_id"],
        "flavor_id": body["flavor_id"],
        "pci_alias": "RTX-3060-LHR:1,GA106-audio:1",
        "driver_version": "580.95.05",
        "device_count": 1,
        "project_ids": [PROJECT],
    }
    h.settings.palimpsest_hub_instance_base_images[body["image_id"]] = {
        "architecture": "x86_64",
        "os_type": "linux",
        "os_hash_algo": "sha512",
        "os_hash_value": "a" * 128,
    }
    image = h.provider.get_image
    monkeypatch.setattr(
        h.provider,
        "get_image",
        lambda identity: {
            **image(identity),
            "os_hash_algo": "sha512",
            "os_hash_value": "a" * 128,
        },
    )
    monkeypatch.setattr(
        h.provider,
        "fetch_flavor_extra_specs",
        lambda flavor: {
            **flavor,
            "extra_specs": {"pci_passthrough:alias": "RTX-3060-LHR:1,GA106-audio:1"},
        },
    )
    return body


@pytest.mark.asyncio
async def test_gpu_snapshots_trusted_profile_and_preserves_image_identity_on_reuse(instance_hub, monkeypatch):
    h = instance_hub
    body = await gpu_request(h, monkeypatch)
    response = await create_and_advance(h, json=body)
    assert response.status_code == 202, response.text
    item = response.json()
    assert item["gpu_profile"] == "rtx-qualified" and item["gpu"]["device_count"] == 1
    assert "project_ids" not in item["gpu"] and item["gpu"]["project_id"] == PROJECT
    guest = guest_spec(h.provider)
    assert guest["gpu"] == item["gpu"] and guest["runtime"]["user"] == "1000:1000"
    assert guest["command"] == ["/app/proof"]
    repeated = await create_and_advance(h, json=body)
    assert repeated.json()["id"] == item["id"] and len(h.provider.creates) == 1
    warm = await create_and_advance(
        h,
        json=request_body(
            image_id=body["image_id"],
            layer_digests=[],
            source_instance_id=item["id"],
            gpu_profile="rtx-qualified",
        ),
    )
    assert warm.status_code == 202, warm.text
    assert warm.json()["layer_digests"] == item["layer_digests"] and len(h.provider.creates) == 2
    assert guest_spec(h.provider)["gpu"] == guest["gpu"]


@pytest.mark.asyncio
async def test_project_visible_private_gpu_flavor_does_not_require_tenant_acl_admin(instance_hub, monkeypatch):
    from openstack.exceptions import ForbiddenException

    h = instance_hub
    body = await gpu_request(h, monkeypatch)
    monkeypatch.setattr(
        h.provider, "get_flavor", lambda identity: {"id": identity, "vcpus": 1, "ram": 1024, "is_public": False}
    )
    monkeypatch.setattr(h.provider, "flavors", lambda **kwargs: iter([{"id": body["flavor_id"]}]), raising=False)

    def tenant_acl(identity):
        raise ForbiddenException("Tenant ACL enumeration is administrative")

    monkeypatch.setattr(h.provider, "get_flavor_access", tenant_acl, raising=False)
    response = await create_and_advance(h, json=body)
    assert response.status_code == 202, response.text
    assert len(h.provider.creates) == 1
    assert response.json()["gpu"]["project_id"] == PROJECT


@pytest.mark.asyncio
async def test_admin_inventory_does_not_grant_unassigned_private_gpu_flavor(instance_hub, monkeypatch):
    h = instance_hub
    body = await gpu_request(h, monkeypatch)
    monkeypatch.setattr(
        h.provider, "get_flavor", lambda identity: {"id": identity, "vcpus": 1, "ram": 1024, "is_public": False}
    )

    def available_flavors(*, is_public=None, **kwargs):
        # Nova's unrestricted admin inventory includes private IDs with no grant.
        return [] if is_public is True else [{"id": body["flavor_id"]}]

    monkeypatch.setattr(h.provider, "flavors", available_flavors, raising=False)
    response = await create_and_advance(h, json=body)
    assert response.status_code == 403, response.text
    assert not h.provider.creates
    async with h.factory() as session:
        assert not (await session.scalars(select(PalimpsestHubInstance))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "disabled",
        "image",
        "flavor",
        "unqualified",
        "hash",
        "driver",
        "project",
        "alias",
        "private-flavor",
        "mode",
    ],
)
async def test_gpu_mismatch_never_admits_or_allocates(instance_hub, monkeypatch, failure):
    h = instance_hub
    body = await gpu_request(h, monkeypatch)
    profile = h.settings.palimpsest_hub_instance_gpu_profiles["rtx-qualified"]
    if failure == "disabled":
        h.settings.palimpsest_hub_instance_gpu_profiles.clear()
    elif failure in {"image", "flavor"}:
        body[failure + "_id"] = str(uuid4())
    elif failure == "unqualified":
        h.settings.palimpsest_hub_instance_base_images.clear()
    elif failure == "hash":
        h.settings.palimpsest_hub_instance_base_images[body["image_id"]]["os_hash_value"] = "b" * 128
    elif failure == "driver":
        profile.pop("driver_version")
    elif failure == "project":
        profile["project_ids"] = [OTHER]
    elif failure == "alias":
        monkeypatch.setattr(h.provider, "fetch_flavor_extra_specs", lambda flavor: {**flavor, "extra_specs": {}})
    elif failure == "private-flavor":
        monkeypatch.setattr(
            h.provider,
            "get_flavor",
            lambda identity: {"id": identity, "vcpus": 1, "ram": 1024, "is_public": False},
        )
        monkeypatch.setattr(h.provider, "flavors", lambda **kwargs: [{"id": str(uuid4())}], raising=False)
    elif failure == "mode":
        async with h.factory() as session:
            layer = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == DIGEST))
            layer.config_json = {"base_image_digest": BASE}
            await session.commit()
    response = await create_and_advance(h, json=body)
    assert response.status_code == (403 if failure in {"project", "private-flavor"} else 422), response.text
    assert not h.provider.creates
    async with h.factory() as session:
        assert not (await session.scalars(select(PalimpsestHubInstance))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["profile", "disabled", "project", "qualification", "hash", "alias", "access"])
async def test_gpu_second_fence_changes_fail_without_effect_or_replay(instance_hub, monkeypatch, failure):
    h = instance_hub
    body = await gpu_request(h, monkeypatch)
    if failure == "access":
        monkeypatch.setattr(
            h.provider,
            "get_flavor",
            lambda identity: {"id": identity, "vcpus": 1, "ram": 1024, "is_public": False},
        )
        monkeypatch.setattr(h.provider, "flavors", lambda **kwargs: [{"id": body["flavor_id"]}], raising=False)
    original = service.qualified_gpu
    calls = 0

    def mutate(spec, project, settings):
        nonlocal calls
        calls += 1
        if calls == 2:
            profile = h.settings.palimpsest_hub_instance_gpu_profiles["rtx-qualified"]
            if failure == "profile":
                profile["driver_version"] = "581.12.00"
            elif failure == "disabled":
                h.settings.palimpsest_hub_instance_gpu_profiles.clear()
            elif failure == "project":
                profile["project_ids"] = [OTHER]
            elif failure == "qualification":
                h.settings.palimpsest_hub_instance_base_images.clear()
            elif failure == "hash":
                image = h.provider.get_image
                monkeypatch.setattr(
                    h.provider, "get_image", lambda identity: {**image(identity), "os_hash_value": "b" * 128}
                )
            elif failure == "alias":
                monkeypatch.setattr(
                    h.provider, "fetch_flavor_extra_specs", lambda flavor: {**flavor, "extra_specs": {}}
                )
            elif failure == "access":
                monkeypatch.setattr(
                    h.provider,
                    "get_flavor",
                    lambda identity: {"id": identity, "vcpus": 1, "ram": 1024, "is_public": False},
                )
                monkeypatch.setattr(h.provider, "flavors", lambda **kwargs: [], raising=False)
        return original(spec, project, settings)

    monkeypatch.setattr(service, "qualified_gpu", mutate)
    response = await create_and_advance(h, json=body)
    assert response.status_code == (403 if failure in {"project", "access"} else 422), response.text
    assert not h.provider.creates
    async with h.factory() as session:
        row = (await session.scalars(select(PalimpsestHubInstance))).one()
        assert row.error_code == "create_inputs_changed" and row.capability_hash is None
        assert row.boot_volume_id is None and row.server_id is None
        assert h.provider.volume_creates == []
    repeated = await create_and_advance(h, json=body)
    assert repeated.status_code == 202 and repeated.json()["error_code"] == "create_inputs_changed"
    assert not h.provider.creates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"pci_passthrough:alias": "gpu:1"},
        {"resources:VGPU": "1"},
        {"resources1:VGPU": "1"},
        {"resources42:CUSTOM_GPU": "1"},
        {"resourcesgpu:VGPU": "1"},
        {"resourcesgroup-a:CUSTOM_GPU": "1"},
        {"resourcesgroup_b:CUSTOM_GPU": "1"},
    ],
)
async def test_gpu_flavor_without_explicit_profile_never_allocates(instance_hub, monkeypatch, extra):
    h = instance_hub
    monkeypatch.setattr(h.provider, "fetch_flavor_extra_specs", lambda flavor: {**flavor, "extra_specs": extra})
    response = await create_and_advance(h, json=request_body())
    assert response.status_code == 422 and not h.provider.creates


@pytest.mark.asyncio
async def test_cpu_default_needs_no_gpu_profile_or_guest_tools(instance_hub):
    h = instance_hub
    response = await create_and_advance(h, json=request_body())
    assert response.status_code == 202, response.text
    assert response.json()["gpu"] is None and response.json()["gpu_profile"] is None
    assert "gpu" not in guest_spec(h.provider)


@pytest.mark.parametrize(
    "change",
    [
        {"device_count": 0},
        {"device_count": 17},
        {"device_count": True},
        {"device_count": "1"},
        {"driver_version": ""},
        {"driver_version": "580.95;id"},
        {"pci_alias": "gpu:0"},
        {"pci_alias": "gpu:1,gpu:1"},
        {"pci_alias": "gpu:1, audio:1"},
        {"project_ids": []},
        {"project_ids": [PROJECT, PROJECT]},
        {"project_ids": ["not-a-project"]},
        {"host_library_path": "/usr/lib"},
        {"image_id": "not-an-image"},
    ],
)
def test_gpu_profile_rejects_unsafe_or_incomplete_operator_data(change):
    from pydantic import ValidationError

    from palimpsest_hub.config import InstanceGpuProfile

    profile = {
        "image_id": str(uuid4()),
        "flavor_id": "gpu",
        "pci_alias": "gpu:1,audio:1",
        "driver_version": "580.95.05",
        "device_count": 1,
        "project_ids": [PROJECT],
    }
    with pytest.raises(ValidationError):
        InstanceGpuProfile.model_validate({**profile, **change})


def gpu_helper_namespace():
    import os
    import pathlib
    import subprocess
    import time

    from palimpsest_hub.services.instance_gpu_runtime import GPU_RUNTIME

    namespace = {"os": os, "pathlib": pathlib, "subprocess": subprocess, "time": time}
    exec(compile(GPU_RUNTIME, "embedded-gpu-runtime.py", "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    "version,observation,error",
    [
        ("1.17.8", "GPU-11111111-2222-3333-4444-555555555555, 580.95.05\n", "gpu_vendor_version_unqualified"),
        ("1.18.0", "", "gpu_device_count_mismatch"),
        ("1.18.0", "GPU-11111111-2222-3333-4444-555555555555, 579.00\n", "gpu_driver_or_uuid_mismatch"),
        ("1.18.0", "not-a-uuid, 580.95.05\n", "gpu_driver_or_uuid_mismatch"),
        ("1.18.0", "GPU-11111111-2222-3333-4444-555555555555, 580.95.05\n", None),
    ],
)
def test_gpu_prerequisite_boundary_fails_closed_without_driver_install(version, observation, error):
    import stat

    namespace = gpu_helper_namespace()
    trusted = SimpleNamespace(
        resolve=lambda **kwargs: trusted, stat=lambda: SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
    )
    namespace["pathlib"] = SimpleNamespace(Path=lambda tool: trusted)
    namespace["os"] = SimpleNamespace(access=lambda path, permission: True, X_OK=1)
    commands = []

    def capture(argv):
        commands.append(argv)
        return (
            "cli-version: " + version + "\nlib-version: " + version + "\n" if argv[-1] == "--version" else observation
        )

    namespace["gpu_capture"] = capture
    profile = {"driver_version": "580.95.05", "device_count": 1}
    if error:
        with pytest.raises(RuntimeError, match=error):
            namespace["gpu_prerequisites"](profile)
    else:
        assert namespace["gpu_prerequisites"](profile) == ["GPU-11111111-2222-3333-4444-555555555555"]
    assert all(argv[0] in {"/usr/bin/nvidia-smi", "/usr/bin/nvidia-container-cli"} for argv in commands)


@pytest.mark.parametrize("partial_failure", [False, True])
def test_gpu_vendor_mounts_tracked_even_on_partial_failure_and_cleaned_in_reverse(tmp_path, partial_failure):
    namespace = gpu_helper_namespace()
    merged = tmp_path / "merged"
    for name in ("dev", "proc", "sys", "etc"):
        (merged / name).mkdir(parents=True)
    namespace["gpu_mount_table"] = lambda: dict(current)
    current = {1: merged.parent}
    commands = []
    owned = {}

    def capture(argv):
        commands.append(argv)
        current.update({10: merged / "usr/lib/libcuda.so", 11: merged / "dev/nvidia0"})
        if partial_failure:
            raise RuntimeError("gpu_vendor_command_failed")
        return ""

    namespace["gpu_capture"] = capture
    if partial_failure:
        with pytest.raises(RuntimeError, match="gpu_vendor_command_failed"):
            namespace["gpu_configure"]({}, ["GPU-fixture"], merged, owned)
    else:
        namespace["gpu_configure"]({}, ["GPU-fixture"], merged, owned)
    assert set(owned) == {10, 11}
    assert commands == [
        [
            "/usr/bin/nvidia-container-cli",
            "configure",
            "--no-cgroups",
            "--compute",
            "--utility",
            "--no-persistenced",
            "--no-fabricmanager",
            "--ldconfig=@/sbin/ldconfig",
            "--device=GPU-fixture",
            str(merged),
        ]
    ]
    removed = []

    namespace["gpu_resolved_mount"] = lambda path: next(
        identity for identity, target in current.items() if target == path
    )

    def unmount(path, identity):
        assert current[identity] == path
        removed.append(["umount", str(path)])
        current.pop(identity)

    namespace["gpu_unmount_owned"] = unmount
    namespace["gpu_cleanup"](owned)
    assert removed == [["umount", str(merged / "usr/lib/libcuda.so")], ["umount", str(merged / "dev/nvidia0")]]
    assert current == {1: merged.parent}


def test_gpu_runtime_managed_symlink_never_reaches_vendor_or_base(tmp_path):
    namespace = gpu_helper_namespace()
    merged = tmp_path / "merged"
    merged.mkdir()
    (merged / "dev").symlink_to("/dev")
    namespace["gpu_capture"] = lambda argv: pytest.fail("vendor must not receive redirected managed root")
    with pytest.raises(RuntimeError, match="gpu_unsafe_mountpoint"):
        namespace["gpu_configure"]({}, ["GPU-fixture"], merged, {})


def test_gpu_injected_probe_uses_runtime_identity_and_exact_device_driver():
    namespace = gpu_helper_namespace()
    calls = []
    enter = object()
    environment = {"PATH": "/usr/bin", "NVIDIA_VISIBLE_DEVICES": "GPU-fixture"}

    def capture(argv, preexec, env):
        calls.append((argv, preexec, env))
        return "GPU-fixture, 580.95.05\n"

    namespace["gpu_capture"] = capture
    namespace["gpu_verify_injected"]({"driver_version": "580.95.05"}, ["GPU-fixture"], enter, environment)
    assert calls[0][1] is enter and calls[0][2] is environment
    with pytest.raises(RuntimeError, match="gpu_injected_runtime_mismatch"):
        namespace["gpu_verify_injected"]({"driver_version": "581.00"}, ["GPU-fixture"], enter, environment)


def test_settings_gpu_defaults_disabled_and_requires_content_pinned_base():
    from pydantic import ValidationError

    from palimpsest_hub.config import Settings

    values = {
        "database_url": "sqlite+aiosqlite:///fixture.db",
        "redis_url": "redis://localhost:6379",
        "palimpsest_hub_local_path": "/tmp/fixture-cas",
        "os_auth_url": "https://keystone.example/v3",
        "os_username": "service",
        "os_password": "fixture",
        "os_project_name": "service",
    }
    assert Settings(**values).palimpsest_hub_instance_gpu_profiles == {}
    profile = {
        "image_id": str(uuid4()),
        "flavor_id": "gpu",
        "pci_alias": "gpu:1,audio:1",
        "driver_version": "580.95.05",
        "device_count": 1,
        "project_ids": [PROJECT],
    }
    with pytest.raises(ValidationError, match="content-pinned"):
        Settings(**values, palimpsest_hub_instance_gpu_profiles={"qualified": profile})


@pytest.mark.asyncio
async def test_cpu_request_digest_is_compatible_with_pre_gpu_api(instance_hub):
    h = instance_hub
    body = request_body()
    expected = instances.InstanceRequest.model_validate(body).model_dump(mode="json")
    expected.pop("gpu_profile")
    expected.pop("cluster_profile")
    expected.pop("cluster_server_id")
    response = await create_and_advance(h, json=body)
    assert response.status_code == 202, response.text
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, response.json()["id"])
        assert (
            row.request_digest
            == "sha256:"
            + hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        )


@pytest.mark.asyncio
async def test_cpu_flavor_becoming_gpu_at_second_fence_cannot_allocate(instance_hub, monkeypatch):
    h = instance_hub
    original = h.provider.get_image
    calls = 0

    def mutate(identity):
        nonlocal calls
        calls += 1
        if calls == 2:
            monkeypatch.setattr(
                h.provider,
                "fetch_flavor_extra_specs",
                lambda flavor: {
                    **flavor,
                    "extra_specs": {"pci_passthrough:alias": "gpu:1"},
                },
            )
        return original(identity)

    monkeypatch.setattr(h.provider, "get_image", mutate)
    response = await create_and_advance(h, json=request_body())
    assert response.status_code == 422 and not h.provider.creates


def test_gpu_escaped_vendor_mount_is_owned_for_cleanup_and_fails_closed(tmp_path):
    namespace = gpu_helper_namespace()
    merged = tmp_path / "merged"
    escaped = tmp_path / "outside"
    namespace["gpu_mount_table"] = lambda: {1: tmp_path, 10: escaped}
    owned = {}
    with pytest.raises(RuntimeError, match="gpu_vendor_mount_escaped"):
        namespace["gpu_track_mounts"]({1: tmp_path}, merged, owned)
    assert owned == {10: escaped}


def test_gpu_vendor_capture_bounds_actual_subprocess_output():
    import sys

    namespace = gpu_helper_namespace()
    assert namespace["gpu_capture"]([sys.executable, "-c", "print('bounded')"]) == "bounded\n"
    with pytest.raises(RuntimeError, match="gpu_vendor_output_limit"):
        namespace["gpu_capture"]([sys.executable, "-c", "import sys; sys.stdout.write('x' * 20000)"])


@pytest.mark.parametrize("unowned_top,retain_after_success", [(False, False), (True, False), (False, True)])
def test_gpu_cleanup_same_path_resolves_stack_not_numeric_ids(tmp_path, unowned_top, retain_after_success):
    namespace = gpu_helper_namespace()
    path = tmp_path / "injected-library"
    # ID 900 is the BOTTOM: numeric ordering must not determine unmount order.
    current = {900: path, 12: path}
    stack = [900, 12]
    if unowned_top:
        current[3] = path
        stack.append(3)
    removed = []
    namespace["gpu_mount_table"] = lambda: dict(current)

    def resolve(target):
        assert target == path
        return stack[-1]

    def unmount(target, identity):
        assert target == path
        assert identity in {900, 12}, "unowned replacement must never be unmounted"
        removed.append(identity)
        if not retain_after_success:
            assert stack.pop() == identity
            current.pop(identity)

    namespace["gpu_resolved_mount"] = resolve
    namespace["gpu_unmount_owned"] = unmount
    if unowned_top or retain_after_success:
        with pytest.raises(RuntimeError, match="gpu_mount_cleanup_failed"):
            namespace["gpu_cleanup"]({900: path, 12: path})
        assert current[900] == path and current[12] == path
        if unowned_top:
            assert removed == [] and current[3] == path
    else:
        namespace["gpu_cleanup"]({900: path, 12: path})
        assert removed == [12, 900] and not current


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["packages:read", "packages:write"])
async def test_second_provider_reads_revoke_authority_before_nova(instance_hub, monkeypatch, revoked):
    h = instance_hub
    original_image = h.provider.get_image
    state = {"calls": 0, "revoked": False}

    def inspect(identity):
        state["calls"] += 1
        if state["calls"] == 2:
            state["revoked"] = True
        return original_image(identity)

    async def authority(actor, conn):
        if state["revoked"]:
            if revoked == "packages:write":
                raise HTTPException(403, "Fresh write authority revoked during provider reads")
            return {**actor, "package_actions": actor["package_actions"] - {revoked}}
        return actor

    monkeypatch.setattr(h.provider, "get_image", inspect)
    monkeypatch.setattr(service, "fresh_writer", authority)
    response = await create_and_advance(h, json=request_body())
    assert response.status_code == 403 and not h.provider.creates
    async with h.factory() as session:
        row = (await session.scalars(select(PalimpsestHubInstance))).one()
        assert row.error_code == "create_authority_revoked" and row.capability_hash is None


@pytest.mark.asyncio
async def test_every_bounded_gpu_failure_is_accepted_and_visible_in_actual_callback(instance_hub):
    from palimpsest_hub.services.instance_gpu_runtime import GPU_ERROR_CODES

    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 100
    for code in GPU_ERROR_CODES:
        item = (await create_and_advance(h, json=request_body())).json()
        guest = guest_spec(h.provider)
        response = await h.client.put(
            "/v1/instance-bootstrap/" + item["id"] + "/result",
            headers={"Authorization": "Bearer " + guest["capability"]},
            json={"instance_id": item["id"], "status": "error", "error_code": code},
        )
        assert response.status_code == 204, (code, response.text)
        observed = (await h.client.get("/v1/instances/" + item["id"])).json()
        assert observed["status"] == "error" and observed["result"]["error_code"] == code
        await h.client.delete("/v1/instances/" + item["id"])
        await h.client.get("/v1/instances/" + item["id"])


@pytest.mark.asyncio
async def test_large_valid_cpu_payload_allocates_without_gpu_bootstrap_budget_cost(instance_hub, monkeypatch):
    h = instance_hub
    body = await gpu_request(h, monkeypatch)
    command = ["/app/proof", *(["x" * 4000] * 4)]
    body["command"] = command
    extra_specs = h.provider.fetch_flavor_extra_specs
    monkeypatch.setattr(h.provider, "fetch_flavor_extra_specs", lambda flavor: {**flavor, "extra_specs": {}})
    cpu = await create_and_advance(h, json={**body, "gpu_profile": None})
    assert cpu.status_code == 202 and cpu.json()["status"] == "booting", cpu.text
    assert len(h.provider.creates) == 1
    guest = guest_spec(h.provider)
    assert guest["command"] == command and guest["runtime"]["user"] == "1000:1000"
    assert len(h.provider.creates[0]["user_data"]) <= 65535

    # The same near-limit argv plus the vendor payload cannot be silently
    # truncated or allocated with a CPU fallback when GPU was explicitly asked.
    monkeypatch.setattr(h.provider, "fetch_flavor_extra_specs", extra_specs)
    gpu = await create_and_advance(h, json={**body, "request_id": str(uuid4())})
    assert gpu.status_code == 202, gpu.text
    assert gpu.json()["status"] == "error" and gpu.json()["error_code"] == "create_bootstrap_input_limit"
    gpu_volume_id = gpu.json()["boot_volume_id"]
    cpu_volume_id = cpu.json()["boot_volume_id"]
    assert gpu_volume_id != cpu_volume_id and gpu.json()["server_id"] is None
    assert set(h.provider.volumes_by_id) == {cpu_volume_id, gpu_volume_id}
    assert set(h.provider.servers_by_id) == {cpu.json()["server_id"]}
    assert len(h.provider.creates) == 1 and len(h.provider.volume_creates) == 2
    assert h.provider.volume_deletes == h.provider.deletes == []
    assert h.provider.creates[0]["block_device_mapping_v2"][0]["uuid"] == cpu_volume_id
    reserved = h.provider.volumes_by_id[gpu_volume_id]
    assert reserved["metadata"][service.MARKER] == gpu.json()["id"]
    assert reserved["volume_image_metadata"]["image_id"] == body["image_id"]
    assert reserved["attachments"] == [] and reserved["status"] == "available"
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, gpu.json()["id"])
        assert row.boot_volume_id == gpu_volume_id and row.server_id is None and row.capability_hash is None


async def cluster_request(h, monkeypatch):
    """Real SQL/API admission with fake provider, not a guest k3s qualification."""
    body = await gpu_request(h, monkeypatch)
    body.update(gpu_profile=None, cluster_profile="k3s-qualified")
    h.settings.palimpsest_hub_instance_cluster_profiles["k3s-qualified"] = {
        "image_id": body["image_id"],
        "flavor_ids": [body["flavor_id"]],
        "project_ids": [PROJECT],
    }
    monkeypatch.setattr(h.provider, "fetch_flavor_extra_specs", lambda flavor: {**flavor, "extra_specs": {}})
    monkeypatch.setattr(
        h.provider,
        "server_interfaces",
        lambda server: [
            {"net_id": body["network_id"], "port_id": "cluster-port", "fixed_ips": [{"ip_address": "10.88.0.2"}]}
        ],
        raising=False,
    )
    monkeypatch.setattr(
        h.provider,
        "get_port",
        lambda port: {
            "id": port,
            "project_id": PROJECT,
            "network_id": body["network_id"],
            "device_id": next(iter(h.provider.servers_by_id)),
            "fixed_ips": [{"ip_address": "10.88.0.2"}],
        },
        raising=False,
    )
    return body


def cluster_agent_body(body, server):
    return {
        **body,
        "request_id": str(uuid4()),
        "image_job_id": None,
        "source_instance_id": server["id"],
        "cluster_server_id": server["id"],
    }


@pytest.mark.asyncio
async def test_cluster_two_member_admission_private_capability_and_one_agent(instance_hub, monkeypatch):
    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    response = await create_and_advance(h, json=body)
    assert response.status_code == 202, response.text
    server = response.json()
    assert server["status"] == "booting", server
    server_guest = guest_spec(h.provider)
    assert server["mode"] == "oci-chroot" and server["cluster"]["role"] == "server"
    assert server["cluster"]["node_name"] == "pal-" + server["id"].replace("-", "")
    assert "qualification" not in server["cluster"]
    auth = {"Authorization": "Bearer " + server_guest["capability"]}
    path = "/v1/instance-bootstrap/" + server["id"] + "/cluster"
    initial = await h.client.get(path, headers=auth)
    assert initial.status_code == 200 and initial.headers["cache-control"] == "no-store"
    assert len(initial.json()["members"]) == 1
    agent_response = await create_and_advance(h, json=cluster_agent_body(body, server))
    assert agent_response.status_code == 202, agent_response.text
    agent = agent_response.json()
    assert agent["status"] == "booting" and agent["cluster"]["server_address"] == "10.88.0.2"
    agent_guest = guest_spec(h.provider)
    agent_auth = {"Authorization": "Bearer " + agent_guest["capability"]}
    agent_cap = await h.client.get("/v1/instance-bootstrap/" + agent["id"] + "/cluster", headers=agent_auth)
    server_cap = (await h.client.get(path, headers=auth)).json()
    assert len(server_cap["members"]) == 2 and agent_cap.json()["token"] == server_cap["token"]
    assert initial.json()["token"] == server_cap["token"]
    assert h.settings.palimpsest_hub_build_worker_key.get_secret_value() not in agent_cap.text
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubInstance))).all()
        assert len(rows) == 2
        assert all(server_cap["token"] not in json.dumps(row.spec_json) for row in rows)
    assert server_cap["token"] not in (await h.client.get("/v1/instances")).text
    denied = await h.client.get(path, headers=agent_auth)
    assert denied.status_code == 404
    duplicate = await create_and_advance(h, json=cluster_agent_body(body, server))
    assert duplicate.status_code == 409 and len(h.provider.creates) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disabled", "project", "image", "flavor", "gpu", "command", "probe", "key"])
async def test_cluster_admission_rejects_unqualified_or_caller_execution(instance_hub, monkeypatch, failure):
    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    profile = h.settings.palimpsest_hub_instance_cluster_profiles["k3s-qualified"]
    if failure == "disabled":
        h.settings.palimpsest_hub_instance_cluster_profiles.clear()
    elif failure == "project":
        profile["project_ids"] = [OTHER]
    elif failure in {"image", "flavor"}:
        body[failure + "_id"] = str(uuid4())
    elif failure == "gpu":
        body["gpu_profile"] = "rtx-qualified"
    elif failure == "command":
        body["command"] = ["/bin/sh"]
    elif failure == "probe":
        body["readiness_argv"] = ["/bin/true"]
    elif failure == "key":
        h.settings.palimpsest_hub_build_worker_key = SecretStr("")
    response = await create_and_advance(h, json=body)
    assert response.status_code in (403, 422, 503), response.text
    assert not h.provider.creates


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["foreign-user", "foreign-port", "provider-not-active", "public-ip"])
async def test_cluster_agent_requires_original_subject_and_exact_private_interface(instance_hub, monkeypatch, failure):
    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    server = (await create_and_advance(h, json=body)).json()
    if failure == "foreign-user":
        async with h.factory() as session:
            source = await session.get(PalimpsestHubInstance, server["id"])
            source.created_by = OTHER
            await session.commit()
    elif failure == "foreign-port":
        port = h.provider.get_port("cluster-port")
        monkeypatch.setattr(h.provider, "get_port", lambda identity: {**port, "project_id": OTHER})
    elif failure == "provider-not-active":
        h.provider.servers_by_id[server["server_id"]]["status"] = "BUILD"
    else:
        port = h.provider.get_port("cluster-port")
        monkeypatch.setattr(h.provider, "get_port", lambda identity: {**port, "fixed_ips": [{"ip_address": "8.8.8.8"}]})
    response = await create_and_advance(h, json=cluster_agent_body(body, server))
    assert response.status_code == 409, response.text
    assert len(h.provider.creates) == 1


def cluster_fixture_proof(server, agent):
    pause = "docker.io/rancher/mirrored-pause:3.10.2@sha256:412c4a7219cb8a299a37337f3d87810c5340095322e15594a1637785adad0f17"
    return {
        "palimpsest_cluster": {
            "schema": "palimpsest.cluster-result.v1",
            "success": True,
            "k3s_version": "v1.37.1+k3s1",
            "checks": {"nodes": True, "numerical": True},
            "nodes": [
                {
                    "name": item["cluster"]["node_name"],
                    "internal_ip": "10.88.0." + str(index + 2),
                    "kubelet_version": "v1.37.1+k3s1",
                    "container_runtime": "containerd://2.3.2",
                }
                for index, item in enumerate((server, agent))
            ],
            "pods": [
                {
                    "node": item["cluster"]["node_name"],
                    "name": "pal-proof-" + str(index),
                    "image": pause,
                    "ready": True,
                    "restarts": 0,
                    "numerical_sum": 4950,
                }
                for index, item in enumerate((server, agent))
            ],
        }
    }


@pytest.mark.asyncio
async def test_cluster_callback_validates_full_proof_then_seals_and_revokes(instance_hub, monkeypatch):
    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    server = (await create_and_advance(h, json=body)).json()
    auth = {"Authorization": "Bearer " + guest_spec(h.provider)["capability"]}
    path = "/v1/instance-bootstrap/" + server["id"]
    agent = (await create_and_advance(h, json=cluster_agent_body(body, server))).json()
    agent_auth = {"Authorization": "Bearer " + guest_spec(h.provider)["capability"]}
    agent_result = await h.client.put(
        "/v1/instance-bootstrap/" + agent["id"] + "/result",
        headers=agent_auth,
        json={"instance_id": agent["id"], "status": "ready"},
    )
    assert agent_result.status_code == 204, agent_result.text
    proof = cluster_fixture_proof(server, agent)
    result = {"instance_id": server["id"], "status": "ready", "exit_code": 0, "stdout": json.dumps(proof)}
    marker = await h.client.put(
        path + "/result", headers=auth, json={**result, "stdout": '{"palimpsest_cluster":true}'}
    )
    assert marker.status_code == 422
    proof["palimpsest_cluster"]["pods"][0]["numerical_sum"] = 0
    bad = await h.client.put(path + "/result", headers=auth, json={**result, "stdout": json.dumps(proof)})
    assert bad.status_code == 422
    successful = await h.client.put(path + "/result", headers=auth, json=result)
    assert successful.status_code == 204, successful.text
    repeated = await h.client.put(path + "/result", headers=auth, json=result)
    assert repeated.status_code == 204
    changed = await h.client.put(
        path + "/result",
        headers=auth,
        json={"instance_id": server["id"], "status": "error", "error_code": "cluster_proof_failed"},
    )
    assert changed.status_code == 409
    h.settings.palimpsest_hub_instance_cluster_profiles.clear()
    assert (await h.client.get(path + "/cluster", headers=auth)).status_code != 200


@pytest.mark.asyncio
async def test_cluster_second_fence_profile_revocation_is_durable_not_replayed(instance_hub, monkeypatch):
    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    image = h.provider.get_image
    calls = 0

    def inspect(identity):
        nonlocal calls
        calls += 1
        if calls == 1:
            h.settings.palimpsest_hub_instance_cluster_profiles.clear()
        return image(identity)

    monkeypatch.setattr(h.provider, "get_image", inspect)
    response = await create_and_advance(h, json=body)
    assert response.status_code == 422 and not h.provider.creates
    repeated = await create_and_advance(h, json=body)
    assert repeated.status_code == 202 and repeated.json()["error_code"] == "create_inputs_changed"
    assert not h.provider.creates


@pytest.mark.parametrize("change", [{"flavor_ids": []}, {"project_ids": ["invalid"]}, {"extra": "forbidden"}])
def test_cluster_configuration_rejects_invalid_privilege_authority(change):
    from pydantic import ValidationError

    from palimpsest_hub.config import InstanceClusterProfile

    with pytest.raises(ValidationError):
        InstanceClusterProfile.model_validate(
            {"image_id": str(uuid4()), "flavor_ids": ["small"], "project_ids": [PROJECT], **change}
        )


@pytest.mark.asyncio
async def test_cluster_concurrent_agents_share_project_admission_lock(instance_hub, monkeypatch):
    import asyncio

    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    server = (await create_and_advance(h, json=body)).json()
    responses = await asyncio.gather(*[create_and_advance(h, json=cluster_agent_body(body, server)) for _ in range(2)])
    assert sorted(response.status_code for response in responses) == [202, 409]
    assert len(h.provider.creates) == 2


@pytest.mark.asyncio
async def test_cluster_private_route_rejects_cpu_and_expired_capability(instance_hub, monkeypatch):
    h = instance_hub
    cpu = (await create_and_advance(h, json=request_body())).json()
    auth = {"Authorization": "Bearer " + guest_spec(h.provider)["capability"]}
    assert (await h.client.get("/v1/instance-bootstrap/" + cpu["id"] + "/cluster", headers=auth)).status_code == 404
    body = await cluster_request(h, monkeypatch)
    server = (await create_and_advance(h, json=body)).json()
    auth = {"Authorization": "Bearer " + guest_spec(h.provider)["capability"]}
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, server["id"])
        row.capability_expires_at = h.clock.now - timedelta(seconds=1)
        await session.commit()
    assert (await h.client.get("/v1/instance-bootstrap/" + server["id"] + "/cluster", headers=auth)).status_code == 404


@pytest.mark.asyncio
async def test_cluster_bounded_failure_codes_use_existing_callback(instance_hub, monkeypatch):
    from palimpsest_hub.services.instance_cluster_runtime import CLUSTER_ERROR_CODES

    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    for code in sorted(CLUSTER_ERROR_CODES):
        response = await create_and_advance(h, json={**body, "request_id": str(uuid4())})
        server = response.json()
        auth = {"Authorization": "Bearer " + guest_spec(h.provider)["capability"]}
        callback = await h.client.put(
            "/v1/instance-bootstrap/" + server["id"] + "/result",
            headers=auth,
            json={"instance_id": server["id"], "status": "error", "error_code": code},
        )
        assert callback.status_code == 204, callback.text
        assert (await h.client.get("/v1/instances/" + server["id"])).json()["error_code"] == code


def test_cluster_private_asset_symlink_rejected_before_chroot(tmp_path):
    import os
    import pathlib
    import subprocess
    import time

    from palimpsest_hub.services.instance_cluster_runtime import CLUSTER_RUNTIME

    namespace = {"os": os, "pathlib": pathlib, "subprocess": subprocess, "time": time, "merged": tmp_path}
    exec(compile(CLUSTER_RUNTIME, "embedded-cluster-runtime.py", "exec"), namespace)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "opt").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="cluster_prerequisite_failed"):
        namespace["cluster_private"]("/opt/palimpsest/probe-busybox")
    assert not (outside / "palimpsest").exists()


@pytest.mark.parametrize("mutation", ["boolean-marker", "numeric-check", "extra-node", "restarted", "wrong-image"])
def test_cluster_proof_validator_rejects_forged_or_incomplete_result(mutation):
    from palimpsest_hub.services.instance_cluster_runtime import validate_cluster_result

    server = {"cluster": {"node_name": "pal-" + "a" * 32}}
    agent = {"cluster": {"node_name": "pal-" + "b" * 32}}
    proof = cluster_fixture_proof(server, agent)
    capability = {
        "role": "server",
        "server_address": "10.88.0.2",
        "members": [
            {"node_name": server["cluster"]["node_name"], "role": "server"},
            {"node_name": agent["cluster"]["node_name"], "role": "agent"},
        ],
    }
    if mutation == "boolean-marker":
        proof = {"palimpsest_cluster": True}
    elif mutation == "numeric-check":
        proof["palimpsest_cluster"]["checks"]["numerical"] = 1
    elif mutation == "extra-node":
        proof["palimpsest_cluster"]["nodes"].append(dict(proof["palimpsest_cluster"]["nodes"][0]))
    elif mutation == "restarted":
        proof["palimpsest_cluster"]["pods"][0]["restarts"] = 1
    else:
        proof["palimpsest_cluster"]["pods"][0]["image"] = "arbitrary:latest"
    with pytest.raises(HTTPException) as failure:
        validate_cluster_result(
            {"status": "ready", "exit_code": 0, "stdout": json.dumps(proof), "stderr": ""}, capability
        )
    assert failure.value.status_code == 422


def test_cluster_generated_pods_execute_multicall_numerical_probe(tmp_path):
    """Execute generated argv through a basename-dispatch executable, not a path-string assertion."""
    import os
    import pathlib
    import subprocess
    import time

    from palimpsest_hub.services.instance_cluster_runtime import CLUSTER_RUNTIME

    namespace = {"os": os, "pathlib": pathlib, "subprocess": subprocess, "time": time}
    exec(compile(CLUSTER_RUNTIME, "embedded-cluster-runtime.py", "exec"), namespace)
    manifest = namespace["cluster_probe_manifest"]({"node-a", "node-b"}, "fixture-pinned-pause")
    for index, pod in enumerate(manifest["items"]):
        container = pod["spec"]["containers"][0]
        mount = container["volumeMounts"][0]
        executable = tmp_path / str(index) / mount["mountPath"].lstrip("/")
        executable.parent.mkdir(parents=True)
        # Model BusyBox's actual basename dispatcher, then exercise the real fixed shell program.
        executable.write_text('#!/bin/sh\n[ "${0##*/}" = busybox ] || exit 127\nexec "$@"\n')
        executable.chmod(0o755)
        argv = container["command"]
        result = subprocess.run(
            [str(tmp_path / str(index) / argv[0].lstrip("/")), *argv[1:]], capture_output=True, text=True, timeout=5
        )
        assert result.returncode == 0 and result.stdout.strip() == "4950", result.stderr


@pytest.mark.asyncio
async def test_cluster_agent_failure_seals_after_server_capability_expires(instance_hub, monkeypatch):
    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    server = (await create_and_advance(h, json=body)).json()
    agent = (await create_and_advance(h, json=cluster_agent_body(body, server))).json()
    auth = {"Authorization": "Bearer " + guest_spec(h.provider)["capability"]}
    path = "/v1/instance-bootstrap/" + agent["id"]
    token = (await h.client.get(path + "/cluster", headers=auth)).json()["token"]
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, server["id"])
        row.capability_expires_at = h.clock.now - timedelta(seconds=1)
        await session.commit()
    assert (await h.client.get(path + "/cluster", headers=auth)).status_code == 404
    result = {"instance_id": agent["id"], "status": "error", "error_code": "cluster_bootstrap_failed", "stderr": token}
    first = await h.client.put(path + "/result", headers=auth, json=result)
    assert first.status_code == 204, first.text
    assert (await h.client.put(path + "/result", headers=auth, json=result)).status_code == 204
    stored = (await h.client.get("/v1/instances/" + agent["id"])).json()["result"]
    assert token not in json.dumps(stored) and stored["stderr"] == "[redacted cluster token]"


def test_cluster_private_existing_wrong_owner_is_not_truncated_or_repermissioned(tmp_path):
    import os
    import pathlib
    import subprocess
    import time

    from palimpsest_hub.services.instance_cluster_runtime import CLUSTER_RUNTIME

    target = tmp_path / "token"
    target.write_text("original-untrusted-content")
    target.chmod(0o644)
    real_os = os

    class WrongOwnerOS:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def fstat(self, fd):
            observed = real_os.fstat(fd)
            return SimpleNamespace(st_mode=observed.st_mode, st_uid=1000, st_nlink=observed.st_nlink)

        def fchmod(self, *args):
            pytest.fail("wrong-owner leaf must be rejected before permissions change")

        def ftruncate(self, *args):
            pytest.fail("wrong-owner leaf must be rejected before truncation")

    namespace = {"os": WrongOwnerOS(), "pathlib": pathlib, "subprocess": subprocess, "time": time, "merged": tmp_path}
    exec(compile(CLUSTER_RUNTIME, "embedded-cluster-runtime.py", "exec"), namespace)
    with pytest.raises(RuntimeError, match="cluster_prerequisite_failed"):
        namespace["cluster_private"]("/token", "private-join-token")
    assert target.read_text() == "original-untrusted-content"
    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "removed", "profile-allowlist"])
async def test_cluster_agent_rejects_unobserved_source_authority_change(instance_hub, monkeypatch, change):
    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    server = (await create_and_advance(h, json=body)).json()
    if change == "profile-allowlist":
        h.settings.palimpsest_hub_instance_cluster_profiles["k3s-qualified"]["project_ids"].append(OTHER)
    else:
        async with h.factory() as session:
            source = await session.get(PalimpsestHubInstance, server["id"])
            if change == "expired":
                source.capability_expires_at = h.clock.now - timedelta(seconds=1)
            else:
                source.capability_hash = None
            await session.commit()
    # No GET reconciles the expired source before admission.
    response = await create_and_advance(h, json=cluster_agent_body(body, server))
    assert response.status_code == 409, response.text
    assert len(h.provider.creates) == 1
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubInstance))).all()
        assert [row.id for row in rows] == [server["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "removed", "profile-allowlist"])
async def test_cluster_agent_source_fence_after_provider_read_definitely_prevents_nova(
    instance_hub, monkeypatch, change
):
    h = instance_hub
    body = await cluster_request(h, monkeypatch)
    server = (await create_and_advance(h, json=body)).json()
    original = service.run_blocking
    address_reads = 0

    async def inspect(function, *args, **kwargs):
        nonlocal address_reads
        result = await original(function, *args, **kwargs)
        if function is service.server_address:
            address_reads += 1
            if address_reads == 2:
                if change == "profile-allowlist":
                    h.settings.palimpsest_hub_instance_cluster_profiles["k3s-qualified"]["project_ids"].append(OTHER)
                else:
                    from sqlalchemy.ext.asyncio import async_object_session

                    source = args[1]
                    if change == "expired":
                        source.capability_expires_at = h.clock.now - timedelta(seconds=1)
                    else:
                        source.capability_hash = None
                    await async_object_session(source).flush()
        return result

    # Both modules use the same boundary helper; count admission then allocation reads.
    from palimpsest_hub.services import worker_leases

    monkeypatch.setattr(service, "run_blocking", inspect)
    monkeypatch.setattr(worker_leases, "run_blocking", inspect)
    response = await create_and_advance(h, json=cluster_agent_body(body, server))
    assert response.status_code == 409 and address_reads == 2, response.text
    assert len(h.provider.creates) == 1
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubInstance))).all()
        agent = next(row for row in rows if row.id != server["id"])
        assert agent.error_code == "create_inputs_changed" and agent.capability_hash is None
        assert agent.boot_volume_id is None and agent.server_id is None
        assert len(h.provider.volume_creates) == 1
        assert set(h.provider.volumes_by_id) == {server["boot_volume_id"]}


def cluster_process_regression(body, tmp_path):
    """Isolate Linux subreaper ownership from the pytest process itself."""
    import pathlib
    import subprocess
    import sys

    from palimpsest_hub.services.instance_cluster_runtime import CLUSTER_RUNTIME

    if sys.platform != "linux" or not pathlib.Path("/proc/self/task").exists():
        pytest.skip("Linux /proc and child-subreaper behavior required")
    script = (
        "import os, pathlib, subprocess, time, sys\n"
        + CLUSTER_RUNTIME
        + "\nassert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0\n"
        + "ROOT = pathlib.Path(sys.argv[1])\n"
        + body
    )
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_cluster_cleanup_drains_real_independent_sessions(tmp_path):
    cluster_process_regression(
        r'''
ready = ROOT / 'ready'
program = """import os, pathlib, subprocess, sys, time
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
pathlib.Path(sys.argv[1]).write_text('ready')
time.sleep(60)
"""
child = subprocess.Popen([sys.executable, '-c', program, str(ready)], start_new_session=True)
until = time.monotonic() + 3
while not ready.exists() and time.monotonic() < until:
    time.sleep(.01)
assert ready.exists()
cluster_cleanup()
assert not cluster_children()
assert child.poll() is not None
''',
        tmp_path,
    )


@pytest.mark.parametrize("mode", ["overflow", "trickle", "escaped-session"])
def test_cluster_exec_real_children_obey_output_and_deadline_bounds(tmp_path, mode):
    cluster_process_regression(
        "mode = "
        + repr(mode)
        + "\n"
        + r"""
real_popen = subprocess.Popen
secret = 'fixture-secret-output'
programs = {
    'overflow': "import os, time; os.write(1, b'fixture-secret-output' * 10000); time.sleep(60)",
    'trickle': "import os, time\nwhile True:\n os.write(1, b'x'); time.sleep(.02)",
    'escaped-session': "import os, pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        "os.write(1, b'fixture-secret-output' * 10000)\ntime.sleep(60)",
}
def command(argv, **kwargs):
    kwargs.pop('preexec_fn')  # Exercise capture/ownership, not a privileged chroot.
    return real_popen([sys.executable, '-c', programs[mode]], **kwargs)
subprocess.Popen = command
deadline = time.time() + (0.4 if mode == 'trickle' else 3)
started = time.monotonic()
try:
    cluster_exec(['fixed-fixture-command'])
except RuntimeError as error:
    assert str(error) == 'cluster_proof_failed' and secret not in str(error)
else:
    raise AssertionError('unbounded fixture must fail closed')
assert time.monotonic() - started < 6
assert not cluster_children()
""",
        tmp_path,
    )


def test_cluster_cleanup_refuses_unmount_when_children_cannot_quiesce(tmp_path):
    import os
    import pathlib
    import subprocess
    import time

    from palimpsest_hub.services.instance_cluster_runtime import CLUSTER_RUNTIME

    namespace = {"os": os, "pathlib": pathlib, "subprocess": subprocess, "time": time, "ROOT": tmp_path}
    exec(compile(CLUSTER_RUNTIME, "embedded-cluster-runtime.py", "exec"), namespace)
    (tmp_path / "merged").mkdir()

    def stuck():
        raise RuntimeError("cluster_proof_failed")

    namespace["cluster_reap_children"] = stuck
    namespace["signal"] = SimpleNamespace(SIGTERM=15, SIGINT=2, SIG_IGN=1, signal=lambda *args: None)
    namespace["subprocess"] = SimpleNamespace(run=lambda *args, **kwargs: pytest.fail("live children forbid unmount"))
    with pytest.raises(RuntimeError, match="cluster_proof_failed"):
        namespace["cluster_cleanup"]()


def test_cluster_child_drain_only_signals_unreaped_direct_children(tmp_path):
    import pathlib
    import time

    from palimpsest_hub.services.instance_cluster_runtime import CLUSTER_RUNTIME

    current = [101]
    killed = []

    def kill(pid, number):
        assert pid in current, "stale or non-child numeric identities must never be signalled"
        killed.append(pid)

    def wait(pid, options):
        current.remove(pid)
        if pid == 101:
            current.append(202)  # Adopt the escaped-session orphan only after its parent exits.
        return pid, 0

    class NoProcScan:
        @staticmethod
        def Path(path):
            assert path != "/proc", "grandchild PID scans cannot authorize a root signal"
            return pathlib.Path(path)

    namespace = {
        "os": SimpleNamespace(kill=kill, waitpid=wait, WNOHANG=1, getpid=lambda: 100),
        "pathlib": NoProcScan,
        "time": time,
    }
    exec(compile(CLUSTER_RUNTIME, "embedded-cluster-runtime.py", "exec"), namespace)
    namespace["cluster_children"] = lambda: list(current)
    namespace["cluster_reap_children"]()
    assert killed == [101, 202] and not current


def test_cluster_exec_bounded_stdin_preserves_k3s_leader_and_adopted_daemon_until_cleanup(tmp_path):
    cluster_process_regression(
        r"""
real_popen = subprocess.Popen
ready = ROOT / 'daemon-pid'
helper = "import pathlib, subprocess, sys\nchild=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\npathlib.Path(sys.argv[1]).write_text(str(child.pid))"
leader = "import subprocess, sys, time\nsubprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]], check=True)\ntime.sleep(60)"
cluster_process = real_popen([sys.executable, '-c', leader, helper, str(ready)], start_new_session=True)
until = time.monotonic() + 3
daemon = None
while time.monotonic() < until:
    if ready.exists():
        daemon = int(ready.read_text())
        if daemon in cluster_children():
            break
    time.sleep(.01)
assert daemon is not None and daemon in cluster_children()
def command(argv, **kwargs):
    kwargs.pop('preexec_fn')
    return real_popen([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'], **kwargs)
subprocess.Popen = command
deadline = time.time() + 3
assert cluster_exec(['fixed-fixture-command'], b'bounded-input') == 'bounded-input'
assert cluster_process.poll() is None
os.kill(daemon, 0)
assert daemon in cluster_children()  # A successful kubectl must not kill live runtime orphans.
subprocess.Popen = real_popen
cluster_cleanup()
assert not cluster_children()
""",
        tmp_path,
    )


@pytest.mark.asyncio
async def test_cinder_preparation_waits_for_exact_available_volume_before_one_nova(instance_hub):
    h = instance_hub
    h.provider.volume_status = "creating"
    body = request_body()
    accepted = await h.client.post("/v1/instances", json=body)
    assert accepted.status_code == 202, accepted.text
    item = accepted.json()
    path = "/v1/instances/" + item["id"]
    volume_id = item["boot_volume_id"]
    assert item["status"] == "booting" and item["error_code"] == "create_boot_volume_pending"
    assert item["server_id"] is None and h.provider.creates == []
    assert set(h.provider.volumes_by_id) == {volume_id}
    assert len(h.provider.volume_creates) == 1
    volume = h.provider.volumes_by_id[volume_id]
    assert volume["size"] == body["boot_gib"]
    assert volume["volume_image_metadata"]["image_id"] == body["image_id"]
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        expiry = row.capability_expires_at
        assert row.boot_volume_id == volume_id and row.capability_hash is None
        assert row.created_by == USER and row.project_id == PROJECT
        assert "fixture-original-human-token" not in json.dumps(row.spec_json)
        assert volume["metadata"] == {
            service.MARKER: row.id,
            "palimpsest_request_digest": row.request_digest,
            "palimpsest_volume_role": "boot",
            "palimpsest_image_id": body["image_id"],
        }
    for status in ("creating", "downloading", "downloading"):
        volume["status"] = status
        observed = await h.client.get(path)
        assert observed.status_code == 200, observed.text
        assert observed.json()["boot_volume_id"] == volume_id
        assert observed.json()["server_id"] is None and observed.json()["error_code"] == "create_boot_volume_pending"
        assert h.provider.creates == [] and len(h.provider.volume_creates) == 1
        assert (await h.client.post("/v1/instances", json=body)).json()["id"] == item["id"]
        pending_inventory = (await h.client.get("/v1/instances")).json()
        assert [(entry["id"], entry["boot_volume_id"], entry["server_id"]) for entry in pending_inventory] == [
            (item["id"], volume_id, None)
        ]
    h.clock.now += timedelta(minutes=7)
    volume["status"] = "available"
    for _ in range(2):
        ready = await h.client.get(path)
        assert ready.status_code == 200 and ready.json()["provider_phase"] == "volume_ready", ready.text
        assert ready.json()["server_create_attempted"] is False and ready.json()["server_id"] is None
        assert h.provider.creates == [] and h.provider.volume_deletes == []
    started = await advance_instance(h, item)
    assert started.status_code == 202, started.text
    server_id = started.json()["server_id"]
    assert started.json()["status"] == "booting" and server_id in h.provider.servers_by_id
    assert set(h.provider.servers_by_id) == {server_id}
    assert len(h.provider.creates) == 1 and len(h.provider.volume_creates) == 1
    assert h.provider.creates[0]["block_device_mapping_v2"] == [
        {
            "uuid": volume_id,
            "source_type": "volume",
            "destination_type": "volume",
            "boot_index": "0",
            "delete_on_termination": True,
        }
    ]
    guest = guest_spec(h.provider)
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.capability_expires_at == expiry
        assert guest["expires_at"] == int(expiry.replace(tzinfo=UTC).timestamp())
        assert row.capability_hash == hashlib.sha256(guest["capability"].encode()).hexdigest()
        persisted = {column.name: str(getattr(row, column.name)) for column in row.__table__.columns}
        assert guest["capability"] not in json.dumps(persisted)
    for _ in range(3):
        assert (await h.client.get(path)).json()["server_id"] == server_id
    inventory = (await h.client.get("/v1/instances")).json()
    assert any(entry["id"] == item["id"] and entry["boot_volume_id"] == volume_id for entry in inventory)
    assert len(h.provider.creates) == len(h.provider.volume_creates) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("volume_status", ["creating", "available"])
async def test_partial_cinder_post_binds_only_authoritative_exact_detail(instance_hub, monkeypatch, volume_status):
    h = instance_hub
    h.provider.volume_status = volume_status
    create = h.provider.create_volume

    def partial_create(**kwargs):
        volume = create(**kwargs)
        return {"id": volume["id"]}  # Real Cinder POST need not include tenant ownership.

    monkeypatch.setattr(h.provider, "create_volume", partial_create)
    body = request_body()
    response = await h.client.post("/v1/instances", json=body)
    assert response.status_code == 202, response.text
    item = response.json()
    volume_id = next(iter(h.provider.volumes_by_id))
    assert item["boot_volume_id"] == volume_id and item["server_id"] is None
    assert item["provider_phase"] == ("volume_preparing" if volume_status == "creating" else "volume_ready")
    assert h.provider.volume_reads and set(h.provider.volume_reads) == {volume_id}
    assert h.provider.volume_lists == [] and h.provider.creates == []
    assert len(h.provider.volume_creates) == 1
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.boot_volume_id == volume_id and row.capability_hash is None
    assert (await h.client.post("/v1/instances", json=body)).json()["id"] == item["id"]
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    ["other-id", "missing-owner", "foreign-owner", "none", "missing", "throws", "attached", "image", "size", "marker"],
)
async def test_partial_cinder_post_unproven_detail_stays_durably_uncertain(instance_hub, monkeypatch, detail):
    h = instance_hub
    create = h.provider.create_volume
    get = h.provider.get_volume

    def partial_create(**kwargs):
        return {"id": create(**kwargs)["id"]}

    def unproven_detail(volume_id):
        volume = dict(get(volume_id))
        if detail == "other-id":
            volume["id"] = str(uuid4())
        elif detail == "missing-owner":
            volume.pop("project_id")
        elif detail == "foreign-owner":
            volume["project_id"] = OTHER
        elif detail == "none":
            return None
        elif detail == "missing":
            from openstack.exceptions import NotFoundException

            raise NotFoundException("exact created volume is absent", http_status=404)
        elif detail == "throws":
            raise TimeoutError("private detail diagnostics must not escape")
        elif detail == "attached":
            volume["attachments"] = [{"server_id": str(uuid4())}]
        elif detail == "image":
            volume["volume_image_metadata"] = {"image_id": str(uuid4())}
        elif detail == "size":
            volume["size"] += 1
        else:
            volume["metadata"] = {**volume["metadata"], "palimpsest_volume_role": "other"}
        return volume

    monkeypatch.setattr(h.provider, "create_volume", partial_create)
    monkeypatch.setattr(h.provider, "get_volume", unproven_detail)
    body = request_body()
    response = await h.client.post("/v1/instances", json=body)
    assert response.status_code == 202, response.text
    item = response.json()
    assert item["status"] == "uncertain" and item["boot_volume_id"] is None and item["server_id"] is None
    assert item["error_code"] == "provider_volume_create_uncertain" and item["server_create_attempted"] is False
    assert "private detail diagnostics" not in response.text
    assert h.provider.volume_reads == list(h.provider.volumes_by_id)
    assert h.provider.volume_lists == [] and len(h.provider.volume_creates) == 1 and h.provider.creates == []
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.boot_volume_id is None and row.server_id is None and row.capability_hash is None
        assert service.preparation(row)["phase"] == "volume_create_uncertain"
        assert service.preparation(row)["volume_create_attempted"] is True
    repeated = await h.client.post("/v1/instances", json=body)
    assert repeated.status_code == 202 and repeated.json()["id"] == item["id"], repeated.text
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == []


@pytest.mark.asyncio
async def test_partial_cinder_post_invalid_uuid_never_selects_detail_or_reposts(instance_hub, monkeypatch):
    h = instance_hub
    create = h.provider.create_volume

    def invalid_identity(**kwargs):
        create(**kwargs)
        return {"id": "not-a-volume-uuid"}

    monkeypatch.setattr(h.provider, "create_volume", invalid_identity)
    body = request_body()
    response = await h.client.post("/v1/instances", json=body)
    assert response.status_code == 202, response.text
    item = response.json()
    assert item["status"] == "uncertain" and item["boot_volume_id"] is None and item["server_id"] is None
    assert h.provider.volume_reads == [] and h.provider.volume_lists == [] and h.provider.creates == []
    assert len(h.provider.volume_creates) == 1
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert service.preparation(row)["phase"] == "volume_create_uncertain"
        assert service.preparation(row)["volume_create_attempted"] is True
    repeated = await h.client.post("/v1/instances", json=body)
    assert repeated.status_code == 202 and repeated.json()["id"] == item["id"], repeated.text
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observation",
    ["exact", "absent", "multiple", "foreign", "changed-marker", "attached", "wrong-image", "wrong-size", "overbound"],
)
async def test_lost_cinder_post_recovers_only_one_exact_owned_intent(instance_hub, monkeypatch, observation):
    h = instance_hub
    h.provider.timeout_after_volume_create = True
    h.provider.volume_status = "creating"
    volumes = h.provider.volumes

    def project_scoped_volumes(**kwargs):
        assert h.provider._afterglow_project_id == PROJECT
        if "project_id" in kwargs:
            raise HTTPException(400, "Invalid filters: project_id")
        return volumes(**kwargs)

    monkeypatch.setattr(h.provider, "volumes", project_scoped_volumes)
    body = request_body()
    accepted = await h.client.post("/v1/instances", json=body)
    assert accepted.status_code == 202, accepted.text
    item = accepted.json()
    assert item["server_id"] is None and h.provider.creates == []
    assert item["status"] == "uncertain" and item["boot_volume_id"] is None
    volume_id = next(iter(h.provider.volumes_by_id))
    volume = h.provider.volumes_by_id[volume_id]
    if observation == "absent":
        h.provider.volumes_by_id.clear()
    elif observation == "multiple":
        other_id = str(uuid4())
        h.provider.volumes_by_id[other_id] = {**volume, "id": other_id, "metadata": dict(volume["metadata"])}
    elif observation == "foreign":
        volume["project_id"] = OTHER
    elif observation == "changed-marker":
        volume["metadata"]["palimpsest_request_digest"] = "sha256:" + "0" * 64
    elif observation == "attached":
        volume["attachments"] = [{"server_id": str(uuid4())}]
        volume["status"] = "available"
    elif observation == "wrong-image":
        volume["volume_image_metadata"]["image_id"] = str(uuid4())
    elif observation == "wrong-size":
        volume["size"] += 1
    elif observation == "overbound":
        for _ in range(1000):
            other_id = str(uuid4())
            h.provider.volumes_by_id[other_id] = {**volume, "id": other_id, "metadata": {}}
    path = "/v1/instances/" + item["id"]
    for _ in range(2):
        observed = await h.client.get(path)
        if observation == "exact":
            assert observed.status_code == 200, observed.text
            assert observed.json()["boot_volume_id"] == volume_id
            assert observed.json()["error_code"] == "create_boot_volume_pending"
        elif observation == "absent":
            assert observed.status_code == 200, observed.text
            assert observed.json()["status"] == "uncertain" and observed.json()["boot_volume_id"] is None
        else:
            assert observed.status_code == (200 if observation in ("foreign", "changed-marker") else 409), observed.text
            if observed.status_code == 200:
                assert observed.json()["server_id"] is None and observed.json()["status"] in ("error", "uncertain")
        assert h.provider.creates == [] and len(h.provider.volume_creates) == 1
    assert h.provider.volume_lists and all(call.get("details") is True for call in h.provider.volume_lists)
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.boot_volume_id == (volume_id if observation == "exact" else None)
        assert row.server_id is None and row.capability_hash is None
    assert (await h.client.post("/v1/instances", json=body)).json()["id"] == item["id"]
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == []
    if observation == "absent":
        assert (await h.client.delete(path)).json()["status"] == "deleting"
        for _ in range(2):
            assert (await h.client.get(path)).json()["status"] == "deleting"
        assert h.provider.volume_deletes == [] and h.provider.deletes == []
        assert len(h.provider.volume_creates) == 1 and h.provider.creates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["foreign", "marker", "attached", "wrong-image", "wrong-size", "wrong-volume"])
async def test_bound_prepared_volume_identity_changes_never_reach_nova(instance_hub, monkeypatch, change):
    h = instance_hub
    h.provider.volume_status = "downloading"
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    volume_id = item["boot_volume_id"]
    volume = h.provider.volumes_by_id[volume_id]
    volume["status"] = "available"
    if change == "foreign":
        volume["project_id"] = OTHER
    elif change == "marker":
        volume["metadata"][service.MARKER] = str(uuid4())
    elif change == "attached":
        volume["attachments"] = [{"server_id": str(uuid4())}]
    elif change == "wrong-image":
        volume["volume_image_metadata"]["image_id"] = str(uuid4())
    elif change == "wrong-volume":
        wrong_volume_id = str(uuid4())
        monkeypatch.setattr(h.provider, "get_volume", lambda identity: {**volume, "id": wrong_volume_id})
    else:
        volume["size"] += 1
    response = await h.client.get("/v1/instances/" + item["id"])
    assert response.status_code == (200 if change == "attached" else 409), response.text
    if change == "attached":
        assert response.json()["status"] == "error" and response.json()["server_id"] is None
    assert h.provider.creates == [] and h.provider.volume_deletes == []
    assert len(h.provider.volume_creates) == 1 and set(h.provider.volumes_by_id) == {volume_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["reader", "other-human", "foreign-project"])
async def test_prepared_volume_observation_cannot_allocate_under_different_authority(instance_hub, access):
    h = instance_hub
    h.provider.volume_status = "creating"
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    volume_id = item["boot_volume_id"]
    h.provider.volumes_by_id[volume_id]["status"] = "available"
    headers = {"X-Actions": "packages:inventory,packages:read"}
    if access == "other-human":
        h.provider._afterglow_user_id = OTHER
        headers = {"X-User": OTHER}
    elif access == "foreign-project":
        h.provider._afterglow_project_id = OTHER
        headers = {"X-Project": OTHER}
    response = await h.client.get("/v1/instances/" + item["id"], headers=headers)
    assert response.status_code == (404 if access == "foreign-project" else 200), response.text
    assert h.provider.creates == [] and len(h.provider.volume_creates) == 1
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.server_id is None and row.capability_hash is None and row.boot_volume_id == volume_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["outage", "unauthorized", "forbidden", "revoked", "expired"])
@pytest.mark.parametrize("read_fence", [1, 2])
async def test_slow_prepared_volume_read_rechecks_original_authority_before_nova(
    instance_hub, monkeypatch, failure, read_fence
):
    h = instance_hub
    h.provider.volume_status = "downloading"
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    volume_id = item["boot_volume_id"]
    h.provider.volumes_by_id[volume_id]["status"] = "available"
    ready = await h.client.get("/v1/instances/" + item["id"])
    assert ready.status_code == 200 and ready.json()["provider_phase"] == "volume_ready", ready.text
    assert h.provider.creates == []
    original_read = h.provider.get_volume
    fresh = service.fresh_writer
    state = {"reads": 0}

    def slow_read(identity):
        row = original_read(identity)
        state["reads"] += 1
        if failure == "expired" and state["reads"] == read_fence:
            h.clock.now += timedelta(hours=2)
        return row

    async def authority(actor, conn):
        if state["reads"] >= read_fence and failure != "expired":
            raise HTTPException(
                {"outage": 503, "unauthorized": 401, "forbidden": 403, "revoked": 403}[failure],
                "private identity failure",
            )
        return await fresh(actor, conn)

    monkeypatch.setattr(h.provider, "get_volume", slow_read)
    monkeypatch.setattr(service, "fresh_writer", authority)
    response = await advance_instance(h, item)
    assert (
        response.status_code
        == {"outage": 503, "unauthorized": 401, "forbidden": 403, "revoked": 403, "expired": 202}[failure]
    ), response.text
    if failure == "expired":
        assert response.json()["error_code"] == "create_bootstrap_expired"
    assert state["reads"] >= read_fence and h.provider.creates == [] and len(h.provider.volume_creates) == 1
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.server_id is None and row.capability_hash is None and row.boot_volume_id == volume_id


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
@pytest.mark.parametrize("pending_status", ["creating", "downloading"])
async def test_pre_nova_cinder_cleanup_deletes_once_then_requires_exact_absence(
    instance_hub, monkeypatch, lost_response, pending_status
):
    from openstack.exceptions import NotFoundException

    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    h.provider.volume_status = pending_status
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    volume_id = item["boot_volume_id"]
    assert (await h.client.delete(path)).json()["status"] == "deleting"
    assert h.provider.volume_deletes == [] and h.provider.deletes == [] and h.provider.creates == []
    assert (await h.client.get(path)).json()["status"] == "deleting"
    h.provider.volumes_by_id[volume_id]["status"] = "available"
    ready = await h.client.get(path)
    assert ready.status_code == 200 and ready.json()["provider_phase"] == "delete_volume_ready", ready.text
    assert h.provider.volume_deletes == [] and h.provider.creates == []

    def delete_exact(identity):
        assert identity == volume_id
        h.provider.volume_deletes.append(identity)
        h.provider.volumes_by_id[identity]["status"] = "deleting"
        if lost_response:
            raise TimeoutError("lost exact Cinder DELETE response")

    monkeypatch.setattr(h.provider, "delete_volume", delete_exact)
    deleted = await advance_instance(h, item, operation="delete-volume")
    assert deleted.status_code == 202 and deleted.json()["status"] == "deleting", deleted.text
    for _ in range(2):
        assert (await h.client.get(path)).json()["status"] == "deleting"
        assert (await h.client.delete(path)).json()["status"] == "deleting"
        assert h.provider.volume_deletes == [volume_id]
        assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    h.provider.volumes_by_id.pop(volume_id)
    original_get_volume = h.provider.get_volume

    def absent_exact(identity):
        assert identity == volume_id
        h.provider.volume_reads.append(identity)
        raise NotFoundException("exact Cinder volume is absent", http_status=404)

    monkeypatch.setattr(h.provider, "get_volume", absent_exact)
    assert (await h.client.get(path)).json()["status"] == "deleted"
    assert (await h.client.delete(path)).json()["status"] == "deleted"
    assert h.provider.volume_deletes == [volume_id] and h.provider.deletes == [] and h.provider.creates == []
    assert set(h.provider.volumes_by_id) == set()
    monkeypatch.setattr(h.provider, "get_volume", original_get_volume)
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 202


@pytest.mark.asyncio
async def test_lost_nova_response_after_prepared_volume_recovers_exact_server_once(instance_hub):
    h = instance_hub
    h.provider.volume_status = "creating"
    h.provider.timeout_after_create = True
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    path = "/v1/instances/" + item["id"]
    volume_id = item["boot_volume_id"]
    h.provider.volumes_by_id[volume_id]["status"] = "available"
    ready = await h.client.get(path)
    assert ready.status_code == 200 and ready.json()["provider_phase"] == "volume_ready", ready.text
    assert h.provider.creates == []
    uncertain = await advance_instance(h, item)
    assert uncertain.status_code == 202 and uncertain.json()["status"] == "uncertain", uncertain.text
    server_id = next(iter(h.provider.servers_by_id))
    for _ in range(3):
        recovered = await h.client.get(path)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["server_id"] == server_id and recovered.json()["boot_volume_id"] == volume_id
    listed = (await h.client.get("/v1/instances")).json()
    assert any(entry["id"] == item["id"] and entry["server_id"] == server_id for entry in listed)
    assert h.provider.server_lists
    assert len(h.provider.creates) == len(h.provider.volume_creates) == 1
    assert set(h.provider.servers_by_id) == {server_id} and set(h.provider.volumes_by_id) == {volume_id}


@pytest.mark.asyncio
async def test_bootstrap_budget_failure_releases_only_exact_prepared_cinder_reservation(instance_hub, monkeypatch):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    original_render = service.render_bootstrap
    monkeypatch.setattr(service, "render_bootstrap", lambda spec: "x" * 50000)
    failed = await create_and_advance(h, json=request_body())
    assert failed.status_code == 202, failed.text
    item = failed.json()
    volume_id = item["boot_volume_id"]
    assert item["status"] == "error" and item["error_code"] == "create_bootstrap_input_limit"
    assert item["server_id"] is None and h.provider.creates == []
    assert len(h.provider.volume_creates) == 1 and set(h.provider.volumes_by_id) == {volume_id}
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.capability_hash is None and row.boot_volume_id == volume_id
    path = "/v1/instances/" + item["id"]
    assert (await h.client.get(path)).json()["error_code"] == "create_bootstrap_input_limit"
    assert (await create_and_advance(h, json=request_body())).status_code == 429
    deleting = await h.client.delete(path)
    assert deleting.status_code == 202 and deleting.json()["status"] == "deleting", deleting.text
    assert h.provider.volume_deletes == [] and h.provider.deletes == []
    ready = await h.client.get(path)
    assert ready.status_code == 200 and ready.json()["provider_phase"] == "delete_volume_ready", ready.text
    assert h.provider.volume_deletes == []
    disposed = await advance_instance(h, item, operation="delete-volume")
    assert disposed.status_code == 202 and h.provider.volume_deletes == [volume_id], disposed.text
    assert (await h.client.get(path)).json()["status"] == "deleted"
    assert (await h.client.delete(path)).json()["status"] == "deleted"
    monkeypatch.setattr(service, "render_bootstrap", original_render)
    replacement = await create_and_advance(h, json=request_body())
    assert replacement.status_code == 202 and replacement.json()["server_id"], replacement.text
    assert len(h.provider.volume_creates) == 2 and len(h.provider.creates) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["absent", "multiple", "foreign", "changed-marker"])
async def test_lost_nova_post_never_reallocates_from_unproven_server_inventory(instance_hub, recovery):
    h = instance_hub
    h.provider.timeout_after_create = True
    body = request_body()
    accepted = await create_and_advance(h, json=body)
    assert accepted.status_code == 202 and accepted.json()["status"] == "uncertain", accepted.text
    item = accepted.json()
    volume_id = item["boot_volume_id"]
    server_id = next(iter(h.provider.servers_by_id))
    server = h.provider.servers_by_id[server_id]
    if recovery == "absent":
        h.provider.servers_by_id.clear()
    elif recovery == "multiple":
        duplicate_id = str(uuid4())
        h.provider.servers_by_id[duplicate_id] = {**server, "id": duplicate_id, "metadata": dict(server["metadata"])}
    elif recovery == "foreign":
        server["project_id"] = OTHER
    else:
        server["metadata"]["palimpsest_request_digest"] = "sha256:" + "0" * 64
    path = "/v1/instances/" + item["id"]
    for _ in range(3):
        observed = await h.client.get(path)
        assert observed.status_code == (409 if recovery == "multiple" else 200), observed.text
        if observed.status_code == 200:
            assert observed.json()["status"] == "uncertain" and observed.json()["server_id"] is None
            assert observed.json()["boot_volume_id"] == volume_id
        assert len(h.provider.creates) == len(h.provider.volume_creates) == 1
    assert (await create_and_advance(h, json=body)).json()["id"] == item["id"]
    assert set(h.provider.volumes_by_id) == {volume_id}
    assert h.provider.deletes == h.provider.volume_deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "unauthorized", "forbidden", "none"])
async def test_cinder_observation_outage_preserves_reservation_without_nova(instance_hub, monkeypatch, failure):
    h = instance_hub
    h.provider.volume_status = "creating"
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    volume_id = item["boot_volume_id"]

    def unavailable(identity):
        assert identity == volume_id
        if failure == "none":
            return None
        if failure != "timeout":
            from openstack.exceptions import HttpException

            raise HttpException("private storage endpoint", http_status=401 if failure == "unauthorized" else 403)
        raise TimeoutError("private storage endpoint")

    monkeypatch.setattr(h.provider, "get_volume", unavailable)
    for _ in range(2):
        observed = await h.client.get("/v1/instances/" + item["id"])
        assert observed.status_code == 502 and "private storage endpoint" not in observed.text
        assert h.provider.creates == [] and len(h.provider.volume_creates) == 1
    assert set(h.provider.volumes_by_id) == {volume_id}
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.boot_volume_id == volume_id and row.server_id is None and row.capability_hash is None


@pytest.mark.asyncio
async def test_cinder_error_retains_owned_root_until_explicit_delete_and_exact_404(instance_hub):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    h.provider.volume_status = "creating"
    body = request_body()
    item = (await h.client.post("/v1/instances", json=body)).json()
    volume_id = item["boot_volume_id"]
    path = "/v1/instances/" + item["id"]
    h.provider.volumes_by_id[volume_id]["status"] = "error"
    for _ in range(2):
        observed = await h.client.get(path)
        assert observed.status_code == 200, observed.text
        assert observed.json()["status"] == "error" and observed.json()["error_code"] == "provider_boot_volume_error"
        assert observed.json()["server_id"] is None and observed.json()["boot_volume_id"] == volume_id
        assert h.provider.creates == [] and h.provider.volume_deletes == []
    assert (await h.client.post("/v1/instances", json=body)).json()["id"] == item["id"]
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    deleted = await h.client.delete(path)
    assert deleted.status_code == 202 and deleted.json()["status"] == "deleting", deleted.text
    assert h.provider.volume_deletes == [] and h.provider.deletes == []
    ready = await h.client.get(path)
    assert ready.status_code == 200 and ready.json()["provider_phase"] == "delete_volume_ready", ready.text
    assert h.provider.volume_deletes == []
    disposed = await advance_instance(h, item, operation="delete-volume")
    assert disposed.status_code == 202 and h.provider.volume_deletes == [volume_id], disposed.text
    assert (await h.client.get(path)).json()["status"] == "deleted"
    assert (await h.client.delete(path)).json()["status"] == "deleted"
    assert h.provider.volume_deletes == [volume_id]
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == []
    assert set(h.provider.volumes_by_id) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["project", "marker", "attached"])
async def test_serverless_cinder_cleanup_never_deletes_foreign_or_attached_volume(instance_hub, change):
    h = instance_hub
    h.provider.volume_status = "creating"
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    volume_id = item["boot_volume_id"]
    volume = h.provider.volumes_by_id[volume_id]
    volume["status"] = "available"
    if change == "project":
        volume["project_id"] = OTHER
    elif change == "marker":
        volume["metadata"]["palimpsest_volume_role"] = "unrelated-data"
    else:
        volume["attachments"] = [{"server_id": str(uuid4())}]
    response = await h.client.delete("/v1/instances/" + item["id"])
    assert response.status_code == (202 if change == "attached" else 409), response.text
    if change == "attached":
        assert response.json()["status"] == "deleting" and response.json()["boot_volume_id"] == volume_id
    assert h.provider.volume_deletes == h.provider.deletes == h.provider.creates == []
    assert len(h.provider.volume_creates) == 1 and set(h.provider.volumes_by_id) == {volume_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["nova_not_attempted", "nova_create_uncertain"])
@pytest.mark.parametrize("contender", ["advance", "delete"])
async def test_prepared_boot_commit_races_never_duplicate_or_delete_a_late_create(
    instance_hub, monkeypatch, boundary, contender
):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    h = instance_hub
    h.provider.volume_status = "creating"
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    volume_id = item["boot_volume_id"]
    path = "/v1/instances/" + item["id"]
    h.provider.volumes_by_id[volume_id]["status"] = "available"
    ready = await h.client.get(path)
    assert ready.status_code == 200 and ready.json()["provider_phase"] == "volume_ready", ready.text
    assert h.provider.creates == []
    committed = asyncio.Event()
    resume = asyncio.Event()
    state = {"owner": None, "paused": False}
    real_commit = AsyncSession.commit

    async def interleaved_commit(session):
        pause = (
            not state["paused"]
            and asyncio.current_task() is state["owner"]
            and any(
                isinstance(row, PalimpsestHubInstance)
                and row.id == item["id"]
                and row.spec_json.get("_boot_prepare", {}).get("phase") == boundary
                for row in session.identity_map.values()
            )
        )
        await real_commit(session)
        if pause:
            state["paused"] = True
            committed.set()
            await resume.wait()

    async def original_advance():
        state["owner"] = asyncio.current_task()
        return await advance_instance(h, item)

    monkeypatch.setattr(AsyncSession, "commit", interleaved_commit)
    async with asyncio.timeout(10):
        original = asyncio.create_task(original_advance())
        try:
            await committed.wait()
            assert h.provider.creates == [] and h.provider.volume_deletes == []
            for _ in range(2):
                read_only = await h.client.get(path)
                assert read_only.status_code == 200, read_only.text
                assert read_only.json()["server_id"] is None and h.provider.creates == []
                assert h.provider.volume_deletes == []
            raced = await (advance_instance(h, item) if contender == "advance" else h.client.delete(path))
            assert raced.status_code == (
                409 if contender == "advance" and boundary == "nova_create_uncertain" else 202
            ), raced.text
            if contender == "delete":
                assert h.provider.creates == []
                if boundary == "nova_create_uncertain":
                    assert raced.json()["status"] == "deleting"
                    assert h.provider.volume_deletes == [] and set(h.provider.volumes_by_id) == {volume_id}
                else:
                    assert raced.json()["status"] == "deleting" and h.provider.volume_deletes == []
            elif boundary == "nova_not_attempted":
                assert len(h.provider.creates) == 1
                assert raced.json()["server_id"] in h.provider.servers_by_id
            else:
                assert h.provider.servers_by_id == {} and h.provider.creates == []
        finally:
            resume.set()
            first = await original
        assert first.status_code == 202, first.text
        final = await h.client.get(path)
        assert final.status_code == 200, final.text
    assert state["paused"] and len(h.provider.volume_creates) == 1
    assert h.provider.deletes == []
    if contender == "advance":
        server_id = final.json()["server_id"]
        assert final.json()["status"] == "booting" and server_id in h.provider.servers_by_id
        assert len(h.provider.creates) == 1 and h.provider.volume_deletes == []
        assert set(h.provider.servers_by_id) == {server_id} and set(h.provider.volumes_by_id) == {volume_id}
        assert h.provider.creates[0]["block_device_mapping_v2"][0]["uuid"] == volume_id
    else:
        assert final.json()["server_id"] is None and h.provider.creates == []
        assert final.json()["status"] == "deleting" and final.json()["provider_phase"] == "delete_volume_ready"
        assert final.json()["server_create_attempted"] is False
        assert h.provider.volume_deletes == [] and set(h.provider.volumes_by_id) == {volume_id}
        disposed = await advance_instance(h, item, operation="delete-volume")
        assert disposed.status_code == 202 and h.provider.volume_deletes == [volume_id], disposed.text
        assert (await h.client.get(path)).json()["status"] == "deleted"
        assert h.provider.volumes_by_id == {} and h.provider.creates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["volume_create_uncertain", "volume_waiting", "nova_create_uncertain", "nova_observed"]
)
async def test_unknown_allocation_commit_outcome_never_replays_provider_post(instance_hub, monkeypatch, boundary):
    """Inject a lost COMMIT reply after real durability, not a delayed server commit."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    h = instance_hub
    body = request_body()
    real_commit = AsyncSession.commit
    failure = OperationalError("COMMIT", {}, TimeoutError("unknown COMMIT outcome"), connection_invalidated=True)
    state = {"injected": False, "instance_id": None}

    async def unknown_commit(session):
        matching = next(
            (
                row
                for row in session.identity_map.values()
                if isinstance(row, PalimpsestHubInstance)
                and row.request_id == body["request_id"]
                and row.spec_json.get("_boot_prepare", {}).get("phase") == boundary
            ),
            None,
        )
        inject = matching is not None and not state["injected"]
        if inject:
            state["instance_id"] = matching.id
        await real_commit(session)
        if inject:
            state["injected"] = True
            raise failure

    monkeypatch.setattr(AsyncSession, "commit", unknown_commit)
    with pytest.raises(OperationalError) as raised:
        await create_and_advance(h, json=body)
    assert raised.value is failure
    assert state["injected"] and state["instance_id"]
    path = "/v1/instances/" + state["instance_id"]
    expected_volumes = 0 if boundary == "volume_create_uncertain" else 1
    expected_servers = 1 if boundary == "nova_observed" else 0
    for _ in range(3):
        observed = await h.client.get(path)
        assert observed.status_code == 200, observed.text
        assert observed.json()["id"] == state["instance_id"]
        assert len(h.provider.volume_creates) == expected_volumes
        assert len(h.provider.creates) == expected_servers
        if expected_servers:
            assert observed.json()["status"] == "booting"
            assert set(h.provider.servers_by_id) == {observed.json()["server_id"]}
            assert set(h.provider.volumes_by_id) == {observed.json()["boot_volume_id"]}
        elif boundary == "volume_waiting":
            assert observed.json()["status"] == "booting" and observed.json()["provider_phase"] == "volume_ready"
            assert observed.json()["server_id"] is None and observed.json()["server_create_attempted"] is False
        else:
            assert observed.json()["status"] == "uncertain" and observed.json()["server_id"] is None
    repeated = await h.client.post("/v1/instances", json=body)
    assert repeated.status_code == 202 and repeated.json()["id"] == state["instance_id"]
    assert len(h.provider.volume_creates) == expected_volumes and len(h.provider.creates) == expected_servers
    assert h.provider.volume_deletes == h.provider.deletes == []
    assert h.store.blob_path(DIGEST).read_bytes() == PAYLOAD
    if boundary == "volume_waiting":
        started = await advance_instance(h, observed.json())
        assert started.status_code == 202 and started.json()["server_id"] in h.provider.servers_by_id, started.text
        assert len(h.provider.volume_creates) == len(h.provider.creates) == 1
        assert set(h.provider.volumes_by_id) == {started.json()["boot_volume_id"]}


@pytest.mark.asyncio
async def test_available_initial_create_and_all_gets_are_observation_only(instance_hub):
    h = instance_hub
    body = request_body()
    accepted = await h.client.post("/v1/instances", json=body)
    assert accepted.status_code == 202, accepted.text
    item = accepted.json()
    volume_id = item["boot_volume_id"]
    assert item["provider_phase"] == "volume_ready" and item["server_create_attempted"] is False
    assert item["server_id"] is None and h.provider.creates == []
    for _ in range(3):
        detail = await h.client.get("/v1/instances/" + item["id"])
        assert detail.status_code == 200 and detail.json()["provider_phase"] == "volume_ready", detail.text
        assert detail.json()["server_create_attempted"] is False and detail.json()["boot_volume_id"] == volume_id
        listed = (await h.client.get("/v1/instances")).json()
        assert [(entry["id"], entry["server_id"], entry["boot_volume_id"]) for entry in listed] == [
            (item["id"], None, volume_id)
        ]
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == h.provider.volume_deletes == []
    assert set(h.provider.volumes_by_id) == {volume_id} and h.provider.servers_by_id == {}
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.capability_hash is None and row.server_id is None and row.boot_volume_id == volume_id
    started = await advance_instance(h, item)
    assert started.status_code == 202 and started.json()["server_create_attempted"] is True, started.text
    server_id = started.json()["server_id"]
    assert set(h.provider.servers_by_id) == {server_id} and len(h.provider.creates) == 1
    repeated = await advance_instance(h, item)
    assert repeated.status_code == 409, repeated.text
    assert len(h.provider.creates) == len(h.provider.volume_creates) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["request", "volume", "operation", "extra", "missing", "reader", "other-human", "foreign-project"]
)
async def test_explicit_advance_requires_exact_accepted_intent_and_original_writer(instance_hub, mismatch):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    payload = {"operation": "create-server", "request_id": item["request_id"], "boot_volume_id": item["boot_volume_id"]}
    headers = {}
    if mismatch == "request":
        payload["request_id"] = str(uuid4())
    elif mismatch == "volume":
        payload["boot_volume_id"] = str(uuid4())
    elif mismatch == "operation":
        payload["operation"] = "delete-volume"
    elif mismatch == "extra":
        payload["force"] = True
    elif mismatch == "missing":
        payload.pop("boot_volume_id")
    elif mismatch == "reader":
        headers["X-Actions"] = "packages:inventory,packages:read"
    elif mismatch == "foreign-project":
        headers["X-Project"] = OTHER
        h.provider._afterglow_project_id = OTHER
    else:
        headers["X-User"] = OTHER
        h.provider._afterglow_user_id = OTHER
    response = await h.client.post("/v1/instances/" + item["id"] + "/advance", json=payload, headers=headers)
    expected = (
        422
        if mismatch in ("extra", "missing")
        else 403
        if mismatch in ("reader", "other-human")
        else 404
        if mismatch == "foreign-project"
        else 409
    )
    assert response.status_code == expected, response.text
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == h.provider.volume_deletes == []
    assert set(h.provider.volumes_by_id) == {item["boot_volume_id"]}
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.server_id is None and row.capability_hash is None and row.boot_volume_id == item["boot_volume_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["volume_create_uncertain", "nova_create_uncertain"])
@pytest.mark.parametrize("failure", ["revoked", "expired"])
@pytest.mark.parametrize("lose_terminal_commit_reply", [False, True])
async def test_authority_loss_after_intent_commit_proves_no_call_and_allows_exact_cleanup(
    instance_hub, monkeypatch, boundary, failure, lose_terminal_commit_reply
):
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    h = instance_hub
    body = request_body()
    real_commit = AsyncSession.commit
    fresh = service.fresh_writer
    state = {"crossed": False, "instance_id": None, "revoked": False, "lost_terminal": False}
    unknown_terminal = OperationalError(
        "COMMIT", {}, TimeoutError("unknown terminal no-call COMMIT outcome"), connection_invalidated=True
    )

    async def change_after_commit(session):
        matching = next(
            (
                row
                for row in session.identity_map.values()
                if isinstance(row, PalimpsestHubInstance)
                and row.request_id == body["request_id"]
                and row.spec_json.get("_boot_prepare", {}).get("phase") == boundary
            ),
            None,
        )
        change = matching is not None and not state["crossed"]
        if change:
            state["instance_id"] = matching.id
        lose_terminal = (
            lose_terminal_commit_reply
            and state["crossed"]
            and not state["lost_terminal"]
            and any(
                isinstance(row, PalimpsestHubInstance)
                and row.request_id == body["request_id"]
                and row.status == "error"
                and row.error_code
                == ("create_authority_revoked" if failure == "revoked" else "create_bootstrap_expired")
                for row in session.identity_map.values()
            )
        )
        await real_commit(session)
        if lose_terminal:
            state["lost_terminal"] = True
            raise unknown_terminal
        if change:
            state["crossed"] = True
            if failure == "expired":
                h.clock.now += timedelta(hours=2)
            else:
                state["revoked"] = True

    async def authority(actor, conn):
        if state["revoked"]:
            raise HTTPException(403, "original write authority revoked")
        return await fresh(actor, conn)

    monkeypatch.setattr(AsyncSession, "commit", change_after_commit)
    monkeypatch.setattr(service, "fresh_writer", authority)
    if lose_terminal_commit_reply:
        with pytest.raises(OperationalError) as raised:
            await create_and_advance(h, json=body)
        assert raised.value is unknown_terminal and state["lost_terminal"]
    else:
        response = await create_and_advance(h, json=body)
        assert response.status_code == (403 if failure == "revoked" else 202), response.text
    assert state["crossed"] and state["instance_id"]
    expected_volumes = 0 if boundary == "volume_create_uncertain" else 1
    assert len(h.provider.volume_creates) == expected_volumes and h.provider.creates == []
    state["revoked"] = False
    path = "/v1/instances/" + state["instance_id"]
    observed = await h.client.get(path)
    assert observed.status_code == 200 and observed.json()["status"] == "error", observed.text
    assert observed.json()["server_create_attempted"] is False and observed.json()["server_id"] is None
    assert observed.json()["error_code"] == (
        "create_authority_revoked" if failure == "revoked" else "create_bootstrap_expired"
    )
    volume_id = observed.json()["boot_volume_id"]
    assert set(h.provider.volumes_by_id) == ({volume_id} if expected_volumes else set())
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, state["instance_id"])
        assert row.capability_hash is None and row.server_id is None
    deleting = await h.client.delete(path)
    assert deleting.status_code == 202, deleting.text
    assert h.provider.volume_deletes == []
    if expected_volumes:
        assert deleting.json()["status"] == "deleting"
        ready = await h.client.get(path)
        assert ready.status_code == 200 and ready.json()["provider_phase"] == "delete_volume_ready", ready.text
        assert h.provider.volume_deletes == []
        disposed = await advance_instance(h, ready.json(), operation="delete-volume")
        assert disposed.status_code == 202 and h.provider.volume_deletes == [volume_id], disposed.text
    settled = await h.client.get(path)
    assert settled.status_code == 200 and settled.json()["status"] == "deleted", settled.text
    assert h.provider.volume_deletes == ([volume_id] if expected_volumes else [])
    assert len(h.provider.volume_creates) == expected_volumes and h.provider.creates == h.provider.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["volume_create_uncertain", "nova_create_uncertain"])
async def test_unproven_intent_commit_cannot_dispatch_or_replay_a_provider_effect(instance_hub, monkeypatch, boundary):
    """An exception before real COMMIT leaves no permission to dispatch that POST."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    h = instance_hub
    body = request_body()
    real_commit = AsyncSession.commit
    failure = OperationalError("COMMIT", {}, TimeoutError("unproven COMMIT outcome"), connection_invalidated=True)
    state = {"injected": False, "instance_id": None}

    async def unproven_commit(session):
        row = next(
            (
                candidate
                for candidate in session.identity_map.values()
                if isinstance(candidate, PalimpsestHubInstance)
                and candidate.request_id == body["request_id"]
                and candidate.spec_json.get("_boot_prepare", {}).get("phase") == boundary
            ),
            None,
        )
        if row is not None and not state["injected"]:
            state["instance_id"], state["injected"] = row.id, True
            raise failure
        await real_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", unproven_commit)
    with pytest.raises(OperationalError) as raised:
        await create_and_advance(h, json=body)
    assert raised.value is failure and state["injected"]
    expected_volumes = 0 if boundary == "volume_create_uncertain" else 1
    for _ in range(3):
        observed = await h.client.get("/v1/instances/" + state["instance_id"])
        assert observed.status_code == 200 and observed.json()["server_id"] is None, observed.text
        assert len(h.provider.volume_creates) == expected_volumes and h.provider.creates == []
    assert (await h.client.post("/v1/instances", json=body)).json()["id"] == state["instance_id"]
    assert len(h.provider.volume_creates) == expected_volumes and h.provider.creates == []
    assert h.provider.volume_deletes == h.provider.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", ["missing", None, 0, "false", False, True])
@pytest.mark.parametrize("phase", ["volume_waiting", "nova_create_uncertain"])
@pytest.mark.parametrize("version", [1, True], ids=["version-one", "boolean-not-version"])
async def test_unknown_or_attempted_history_never_authorizes_serverless_cinder_disposal(
    instance_hub, attempt, phase, version
):
    h = instance_hub
    item = (await h.client.post("/v1/instances", json=request_body())).json()
    volume_id = item["boot_volume_id"]
    path = "/v1/instances/" + item["id"]
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        state = {**row.spec_json["_boot_prepare"], "version": version, "phase": phase}
        if attempt == "missing":
            state.pop("server_create_attempted", None)
        else:
            state["server_create_attempted"] = attempt
        row.spec_json = {**row.spec_json, "_boot_prepare": state}
        await session.commit()
    intent = await h.client.delete(path)
    assert intent.status_code == 202 and intent.json()["status"] == "deleting", intent.text
    assert h.provider.volume_deletes == h.provider.deletes == h.provider.creates == []
    listed = next(entry for entry in (await h.client.get("/v1/instances")).json() if entry["id"] == item["id"])
    recognized = type(version) is int and version == 1
    expected_attempted = (
        (True if phase == "nova_create_uncertain" else attempt) if recognized and type(attempt) is bool else None
    )
    assert listed["server_create_attempted"] is expected_attempted
    if not recognized:
        assert listed["provider_phase"] == "legacy"
    disposed = await advance_instance(h, item, operation="delete-volume")
    permitted = recognized and phase == "volume_waiting" and attempt is False
    assert disposed.status_code == (202 if permitted else 409), disposed.text
    assert h.provider.volume_deletes == ([volume_id] if permitted else [])
    assert len(h.provider.volume_creates) == 1 and h.provider.creates == h.provider.deletes == []
    if permitted:
        settled = await h.client.get(path)
        assert settled.status_code == 200 and settled.json()["status"] == "deleted", settled.text
        assert h.provider.volumes_by_id == {}
    else:
        assert set(h.provider.volumes_by_id) == {volume_id}
        assert h.provider.volumes_by_id[volume_id]["attachments"] == []
        async with h.factory() as session:
            row = await session.get(PalimpsestHubInstance, item["id"])
            assert row.status == "deleting" and row.boot_volume_id == volume_id and row.server_id is None


async def assert_expired_nova_root_cleanup(h, item, body, monkeypatch):
    """No Nova call is not no allocation: only exact Cinder404 frees capacity."""
    volume_id = item["boot_volume_id"]
    path = "/v1/instances/" + item["id"]
    assert h.provider.creates == h.provider.deletes == []
    assert len(h.provider.volume_creates) == 1 and set(h.provider.volumes_by_id) == {volume_id}
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.status == "error" and row.error_code == "create_bootstrap_expired"
        assert row.server_id is None and row.boot_volume_id == volume_id and row.capability_hash is None
        assert row.created_by == USER and row.project_id == PROJECT
        state = service.preparation(row)
        assert state["phase"] == "nova_not_attempted" and state["terminal"] is True
        assert state["server_create_attempted"] is False and state["volume_create_attempted"] is True
        service.checked_prepared_volume(row, h.provider.volumes_by_id[volume_id], image_required=True)
        refs = await session.scalars(
            select(RuntimeBlobReference.digest).where(RuntimeBlobReference.resource_id == row.id)
        )
        assert set(refs) == {BASE, DIGEST}

    def unexpected_server_read(*args, **kwargs):
        raise AssertionError("Definite Nova no-call must not require server inventory")

    monkeypatch.setattr(h.provider, "servers", unexpected_server_read)
    monkeypatch.setattr(h.provider, "get_server", unexpected_server_read)
    for _ in range(2):
        observed = await h.client.get(path)
        assert observed.status_code == 200 and observed.json()["error_code"] == "create_bootstrap_expired"
        assert observed.json()["server_create_attempted"] is False
    repeated = await h.client.post("/v1/instances", json=body)
    assert repeated.status_code == 202 and repeated.json()["id"] == item["id"]
    assert (await advance_instance(h, item)).status_code == 409
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    assert (await h.client.delete(path, headers={"X-Actions": "packages:read"})).status_code == 403
    deleting = await h.client.delete(path, headers={"X-Actions": "packages:write,packages:manage"})
    assert deleting.status_code == 202 and deleting.json()["status"] == "deleting"
    assert h.provider.volume_deletes == []
    ready = await h.client.get(path)
    assert ready.status_code == 200 and ready.json()["provider_phase"] == "delete_volume_ready"
    # A successful DELETE reply without actual absence is not settlement.
    monkeypatch.setattr(h.provider, "delete_volume", lambda identity: h.provider.volume_deletes.append(identity))
    disposed = await advance_instance(h, ready.json(), operation="delete-volume")
    assert disposed.status_code == 202 and h.provider.volume_deletes == [volume_id]
    assert (await h.client.get(path)).json()["status"] == "deleting"
    assert (await h.client.post("/v1/instances", json=request_body())).status_code == 429
    h.provider.volumes_by_id.pop(volume_id)
    settled = await h.client.get(path)
    assert settled.status_code == 200 and settled.json()["status"] == "deleted"
    assert volume_id not in h.provider.volumes_by_id and not h.provider.servers_by_id
    assert (await h.client.delete(path)).json()["status"] == "deleted"
    assert (await h.client.post("/v1/instances", json=body)).json()["status"] == "deleted"
    assert h.provider.creates == h.provider.deletes == [] and h.provider.volume_deletes == [volume_id]
    async with h.factory() as session:
        refs = await session.scalars(
            select(RuntimeBlobReference.digest).where(RuntimeBlobReference.resource_id == item["id"])
        )
        assert set(refs) == {BASE, DIGEST}
    replacement = await h.client.post("/v1/instances", json=request_body())
    assert replacement.status_code == 202 and replacement.json()["boot_volume_id"] != volume_id


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["final-authority", "executor-queue"])
@pytest.mark.parametrize("fractional", [False, True], ids=["sql-expired", "guest-floor-expired"])
async def test_final_nova_deadline_proves_no_call_but_retains_prepared_root(
    instance_hub, monkeypatch, boundary, fractional
):
    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    h.clock.now = h.clock.now.replace(microsecond=500000)
    body = request_body()
    accepted = await h.client.post("/v1/instances", json=body)
    assert accepted.status_code == 202 and accepted.json()["provider_phase"] == "volume_ready"
    item = accepted.json()
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        expires = service.aware(row.capability_expires_at)
    floor = datetime.fromtimestamp(int(expires.timestamp()), UTC)
    final_time = floor + timedelta(microseconds=250000) if fractional else expires
    original_authority = service.payload_authority
    original_run_blocking = service.run_blocking
    dispatched = False
    authority_expired = False

    async def authority(actor, conn, session=None, spec=None):
        nonlocal authority_expired
        result = await original_authority(actor, conn, session, spec)
        if boundary == "final-authority" and spec["_boot_prepare"]["phase"] == "nova_create_uncertain":
            h.clock.now = final_time
            authority_expired = True
        return result

    async def dispatch(fn, *args, **kwargs):
        nonlocal dispatched
        if fn.__name__ == "create_server":
            assert not h.provider.creates
            dispatched = True
            if boundary == "executor-queue":
                h.clock.now = final_time
        return await original_run_blocking(fn, *args, **kwargs)

    monkeypatch.setattr(service, "payload_authority", authority)
    monkeypatch.setattr(service, "run_blocking", dispatch)
    failed = await advance_instance(h, item)
    assert failed.status_code == 202 and failed.json()["error_code"] == "create_bootstrap_expired", failed.text
    assert authority_expired is (boundary == "final-authority")
    assert dispatched is (boundary == "executor-queue" or fractional)
    if fractional:
        assert floor <= h.clock.now < expires
    await assert_expired_nova_root_cleanup(h, item, body, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["queued-provider", "expired-commit"])
async def test_cancelled_expired_nova_durably_preserves_no_call_and_prepared_root(instance_hub, monkeypatch, cancel_at):
    from sqlalchemy.ext.asyncio import AsyncSession

    h = instance_hub
    h.settings.palimpsest_hub_max_instances_per_project = 1
    h.clock.now = h.clock.now.replace(microsecond=500000)
    body = request_body()
    accepted = await h.client.post("/v1/instances", json=body)
    assert accepted.status_code == 202 and accepted.json()["provider_phase"] == "volume_ready"
    item = accepted.json()
    worker_started = asyncio.Event()
    commit_started = asyncio.Event()
    release_thread = Event()
    release_commit = asyncio.Event()
    original_run_blocking = service.run_blocking
    original_commit = AsyncSession.commit

    async def delayed_dispatch(fn, *args, **kwargs):
        if fn.__name__ != "create_server":
            return await original_run_blocking(fn, *args, **kwargs)
        if cancel_at == "expired-commit":
            h.clock.now += timedelta(hours=2)
            return await original_run_blocking(fn, *args, **kwargs)
        loop = asyncio.get_running_loop()

        def blocked():
            loop.call_soon_threadsafe(worker_started.set)
            if not release_thread.wait(5):
                raise RuntimeError("test did not release provider worker")
            return fn(*args, **kwargs)

        return await original_run_blocking(blocked)

    async def delayed_commit(session):
        if any(
            isinstance(row, PalimpsestHubInstance)
            and row.id == item["id"]
            and row.error_code == "create_bootstrap_expired"
            for row in session.identity_map.values()
        ):
            commit_started.set()
            await release_commit.wait()
        return await original_commit(session)

    monkeypatch.setattr(service, "run_blocking", delayed_dispatch)
    monkeypatch.setattr(AsyncSession, "commit", delayed_commit)
    request = asyncio.create_task(advance_instance(h, item))
    try:
        if cancel_at == "queued-provider":
            await asyncio.wait_for(worker_started.wait(), timeout=5)
            h.clock.now += timedelta(hours=2)
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            release_thread.set()
        await asyncio.wait_for(commit_started.wait(), timeout=5)
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
    finally:
        release_thread.set()
        release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, timeout=5)
    await assert_expired_nova_root_cleanup(h, item, body, monkeypatch)


@pytest.fixture
async def native_hub(instance_hub, tmp_path, monkeypatch):
    from palimpsest_local.image_manifest import manifest_digest, normalize_manifest

    from palimpsest_hub.api import native_instances as native_api
    from palimpsest_hub.services import native_instances as native_service
    from palimpsest_hub.services import published_images

    operator_paths, original_execute = native_service._operator_paths, native_service._execute
    storage_admission = native_service._published_storage_admission

    h = instance_hub
    native_payload = PAYLOAD.ljust(128, b"\0")
    native_digest = "sha256:" + hashlib.sha256(native_payload).hexdigest()
    h.store.blob_path(native_digest).write_bytes(native_payload)
    manifest = normalize_manifest(
        {
            "schema": "palimpsest.image-manifest.v1",
            "mode": "oci-root",
            "platform": "linux/amd64",
            "os_family": "ubuntu",
            "os_version": "22.04",
            "source": {"kind": "local"},
            "runtime": {
                "entrypoint": [],
                "cmd": [],
                "env": ["PRIVATE=do-not-project"],
                "working_dir": "/",
                "user": "0",
            },
            "layers": [{"digest": native_digest, "size_bytes": len(native_payload)}],
        }
    )
    share_id = str(uuid4())
    scope_key = "user-" + hashlib.sha256(USER.encode()).hexdigest()[:32]
    binding = {
        "kind": "manila-namespace-v1",
        "share_id": share_id,
        "base_export": "10.0.0.1:6789:/share",
        "export": "10.0.0.1:6789:/share/cas/" + scope_key,
        "root": "blobs/sha256",
        "scope_key": scope_key,
        "fs_name": "cephfs",
        "data_pool": "cephfs_data",
        "namespace": "pal-" + hashlib.sha256(share_id.encode()).hexdigest()[:24] + "-" + scope_key,
    }
    profile = {
        "enabled": True,
        "root": str(tmp_path / "private"),
        "environment": "prepared",
        "grant_path": str(tmp_path / "approval.json"),
        "project_id": PROJECT,
        "user_id": USER,
        "manifest_digest": manifest_digest(manifest),
        "share_id": binding["share_id"],
        "export": binding["base_export"],
        "ceph_fs_name": binding["fs_name"],
        "ceph_data_pool": binding["data_pool"],
        "publication_root": str(h.store.root),
    }
    from pathlib import Path

    Path(profile["root"]).mkdir(mode=0o700)
    (Path(profile["root"]) / profile["environment"]).mkdir(mode=0o700)
    from palimpsest_local.cloud_env import SCHEMA, CloudEnv

    prepared = CloudEnv(profile["environment"], root=Path(profile["root"]))
    prepared.state = {
        "schema": SCHEMA,
        "name": prepared.name,
        "project_id": PROJECT,
        "user_id": USER,
        "native": {"preparation": {"config": {"dns": ["8.8.8.8", "8.8.4.4"]}}},
    }
    prepared.save()
    h.settings.palimpsest_hub_native_profiles = {"approved": profile}
    published = {
        "id": str(uuid4()),
        "manifest_digest": manifest_digest(manifest),
        "manifest": manifest,
        "nodes": manifest["layers"],
        "leaf_id": manifest["layers"][-1]["id"],
        "storage_binding": binding,
        "project_id": PROJECT,
        "created_by": USER,
        "visibility": "private",
    }
    from palimpsest_hub.image_models import PalimpsestHubPublishedImage

    async with h.factory() as session:
        session.add(
            PalimpsestHubPublishedImage(
                id=published["id"],
                project_id=PROJECT,
                created_by=USER,
                request_id=str(uuid4()),
                name="local",
                manifest_digest=published["manifest_digest"],
                manifest_json=manifest,
                leaf_id=published["leaf_id"],
                storage_binding_json=binding,
            )
        )
        await session.commit()

    async def resolve(factory, identity, actor):
        assert factory is h.factory and identity == published["id"]
        assert actor["project_id"] == PROJECT and actor["user_id"] == USER
        return published

    monkeypatch.setattr(published_images, "resolve_image", resolve)

    def verify_storage(store, profile, image):
        from palimpsest_local.cloud_env import CloudEnvError

        if (
            binding["kind"] != "manila-namespace-v1"
            or binding["share_id"] != profile["share_id"]
            or binding["base_export"] != profile["export"]
        ):
            raise CloudEnvError("synthetic namespace binding mismatch")

    monkeypatch.setattr(native_service, "_verify_published_storage", verify_storage)
    monkeypatch.setattr(native_service, "_published_storage_admission", lambda *args: None)
    monkeypatch.setattr(
        native_service,
        "_operator_paths",
        lambda profile: {
            "grant_id": "explicit-fixture",
            "boot_gib": 32,
            "flavors": {"cpu": "small", "gpu": "small"},
            "project_id": PROJECT,
            "environment": profile["environment"],
            "source_digest": profile["manifest_digest"],
            "allow_retained": True,
            "allow_path_scoped_ceph": True,
            "expires_unix": int(datetime.now(UTC).timestamp()) + 3600,
        },
    )
    monkeypatch.setattr(native_api, "_factory_or_503", lambda: h.factory)
    monkeypatch.setattr(native_api, "_store_or_503", lambda: h.store)
    monkeypatch.setattr(native_api, "get_settings", lambda: h.settings)
    h.provider._afterglow_token = "fixture-original-human-token"
    from palimpsest_hub import auth as native_auth

    def fresh_token(token, project):
        return {"token": token, "project_id": project, "user_id": USER}

    async def fresh_manager(token_info):
        return {**token_info, "package_actions": ["packages:write", "packages:read", "packages:manage"]}

    async def fresh_connection(token_info):
        assert token_info["token"] == h.provider._afterglow_token
        yield h.provider

    monkeypatch.setattr(native_service, "validate_token", fresh_token)
    monkeypatch.setattr(native_auth, "get_package_manager_info", fresh_manager)
    monkeypatch.setattr(native_auth, "get_os_conn", fresh_connection)
    h.client._transport.app.include_router(native_api.router, prefix="/v1")
    calls = []
    server_id = str(uuid4())

    def execute(profile, actor, conn, checkpoint, run_id, image, request, approval_digest, deleting):
        assert conn is h.provider and actor["token"] == conn._afterglow_token
        assert image["id"] == published["id"] and image["manifest"] == manifest
        calls.append((run_id, deleting))
        checkpoint(
            {
                "native": {
                    "runs": {
                        run_id: {
                            "run_id": run_id,
                            "status": "cleaned" if deleting or not request["keep"] else "retained",
                            "server_id": server_id,
                            "verified_result": {"command": {"exit_code": 0}},
                        }
                    },
                    "grants": {"explicit-fixture": {"used": {"cpu_servers": 1}}},
                },
                "resources": {},
            }
        )

    monkeypatch.setattr(native_service, "_execute", execute)
    body = {
        "request_id": str(uuid4()),
        "name": "native",
        "image_id": published["id"],
        "profile": "approved",
        "timeout": 120,
        "command": ["/bin/true"],
        "keep": True,
    }
    return SimpleNamespace(
        h=h,
        body=body,
        calls=calls,
        service=native_service,
        binding=binding,
        profile=profile,
        operator_paths=operator_paths,
        original_execute=original_execute,
        published=published,
        digest=native_digest,
        storage_admission=storage_admission,
    )


async def test_native_api_has_durable_hub_image_refs_and_idempotent_retained_lifecycle(native_hub):
    n = native_hub
    response = await n.h.client.post("/v1/native-instances", json=n.body)
    assert response.status_code == 202, response.text
    item = response.json()
    assert item["status"] == "creating" and item["image_id"] == n.body["image_id"]
    assert (await n.h.client.get("/v1/native-instances/" + item["id"])).json()["status"] == "retained"
    assert "PRIVATE" not in response.text and "do-not-project" not in response.text
    retry = await n.h.client.post("/v1/native-instances", json=n.body)
    assert retry.json()["id"] == item["id"] and len(n.calls) == 1
    async with n.h.factory() as session:
        row = await session.get(PalimpsestNativeInstance, item["id"])
        assert row.created_by == USER and row.project_id == PROJECT
        assert row.spec_json["published"]["manifest"]["source"] == {"kind": "local"}
        refs = (
            await session.scalars(
                select(RuntimeBlobReference.digest).where(
                    RuntimeBlobReference.resource_kind == "native-instance",
                    RuntimeBlobReference.resource_id == item["id"],
                )
            )
        ).all()
        assert refs == [n.digest]
    deletion = await n.h.client.delete("/v1/native-instances/" + item["id"])
    assert deletion.json()["status"] == "deleting" and len(n.calls) == 2
    assert (await n.h.client.delete("/v1/native-instances/" + item["id"])).json()["status"] == "deleted"
    assert len(n.calls) == 2


async def test_native_preflight_network_calls_never_run_on_the_event_loop_thread(native_hub):
    import threading

    n = native_hub
    loop_thread = threading.get_ident()
    seen = []
    endpoint = n.h.provider.session.get_endpoint
    n.h.provider.session.get_endpoint = lambda service_type, interface=None, region_name=None: (
        seen.append(threading.get_ident()) or endpoint(service_type, interface, region_name)
    )
    response = await n.h.client.post("/v1/native-instances", json=n.body)
    assert response.status_code == 202, response.text
    assert seen and loop_thread not in seen


async def test_native_api_unknown_create_is_held_and_never_replayed(native_hub, monkeypatch):
    n = native_hub

    def uncertain(profile, actor, conn, checkpoint, run_id, image, request, approval_digest, deleting):
        n.calls.append((run_id, deleting))
        checkpoint(
            {
                "native": {"runs": {run_id: {"status": "creating"}}, "grants": {}},
                "resources": {"native_servers": {run_id: {"id": None, "status": "creating"}}},
            }
        )
        raise TimeoutError("provider create outcome unknown")

    monkeypatch.setattr(n.service, "_execute", uncertain)
    first = await n.h.client.post("/v1/native-instances", json=n.body)
    assert first.status_code == 202 and first.json()["status"] == "creating"
    assert (await n.h.client.get("/v1/native-instances/" + first.json()["id"])).json()["status"] == "held"
    again = await n.h.client.post("/v1/native-instances", json=n.body)
    assert again.json()["id"] == first.json()["id"] and len(n.calls) == 1
    second = await n.h.client.post("/v1/native-instances", json={**n.body, "request_id": str(uuid4())})
    assert second.status_code == 409 and len(n.calls) == 1


async def test_native_public_get_and_list_never_invoke_runtime_or_publish_results(native_hub, monkeypatch):
    n = native_hub
    item = (await n.h.client.post("/v1/native-instances", json=n.body)).json()

    def forbidden(*args, **kwargs):
        raise AssertionError("observation path cannot execute or reconcile native state")

    monkeypatch.setattr(n.service, "_execute", forbidden)
    before = len(n.calls)
    assert (await n.h.client.get("/v1/native-instances/" + item["id"])).json()["status"] == "retained"
    assert len((await n.h.client.get("/v1/native-instances")).json()) == 1
    assert (await n.h.client.get("/v1/native-instances/" + item["id"], headers={"X-User": OTHER})).status_code == 404
    assert len(n.calls) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile", "inactive"),
        ("image_path", "/caller/image.json"),
        ("grant_path", "/caller/approval.json"),
        ("keep", "true"),
        ("command", []),
    ],
)
async def test_native_api_rejects_caller_paths_and_implicit_approval(native_hub, field, value):
    n = native_hub
    response = await n.h.client.post("/v1/native-instances", json={**n.body, field: value})
    assert response.status_code in {422, 503}
    assert n.calls == []


def test_native_sql_uncertainty_closes_original_caller_adapter_without_cloud_fallback(tmp_path):
    from palimpsest_local.cloud_env import CloudEnvError

    from palimpsest_hub.services.native_instances import _CallerEnvironment

    env = object.__new__(_CallerEnvironment)
    env.state, env.sql_held = {}, False
    env.profile = {"root": str(tmp_path), "environment": "private"}
    (tmp_path / "private").mkdir(mode=0o700)

    def uncertain(state):
        raise RuntimeError("SQL commit disposition unknown")

    env.checkpoint = uncertain
    with pytest.raises(RuntimeError, match="SQL commit"):
        env._checkpoint()
    assert env.sql_held
    sentinel = tmp_path / "private" / "hub-sql-hold.json"
    assert json.loads(sentinel.read_text())["status"] == "hold"
    assert sentinel.stat().st_mode & 0o777 == 0o600
    with pytest.raises(CloudEnvError, match="no further effects"):
        env._authenticate("never-use-clouds-yaml")


async def test_native_driver_cancellation_drains_before_releasing_pending_lifecycle(native_hub, monkeypatch):
    n = native_hub
    started, release = Event(), Event()

    def driver(profile, actor, conn, checkpoint, run_id, image, request, approval_digest, deleting):
        n.calls.append((run_id, deleting))
        checkpoint(
            {
                "native": {"runs": {run_id: {"status": "retained", "server_id": str(uuid4())}}, "grants": {}},
                "resources": {},
            }
        )
        started.set()
        assert release.wait(5), "test must drain native driver custody"

    monkeypatch.setattr(n.service, "_execute", driver)
    request = asyncio.create_task(n.h.client.post("/v1/native-instances", json=n.body))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending = (await n.h.client.get("/v1/native-instances")).json()[0]
        assert pending["status"] == "creating"
        deletion = await n.h.client.delete("/v1/native-instances/" + pending["id"])
        assert deletion.status_code == 409
        retry = await n.h.client.post("/v1/native-instances", json=n.body)
        assert retry.json()["id"] == pending["id"] and len(n.calls) == 1
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=5)
    settled = (await n.h.client.get("/v1/native-instances/" + pending["id"])).json()
    assert settled["status"] == "retained"


async def test_native_preflight_refusal_ends_terminally_and_frees_the_environment(native_hub, monkeypatch):
    n = native_hub

    def refused(profile, actor, conn, checkpoint, run_id, image, request, approval_digest, deleting):
        n.calls.append((run_id, deleting))
        raise n.service._PreflightRefused("native preflight refused before any allocation")

    monkeypatch.setattr(n.service, "_execute", refused)
    created = await n.h.client.post("/v1/native-instances", json=n.body)
    assert created.status_code == 202
    settled = (await n.h.client.get("/v1/native-instances/" + created.json()["id"])).json()
    assert settled["status"] == "deleted" and settled["error_code"] == "preflight_failed"
    # A refused intent holds no consumer slot: a fresh request is admitted, not blocked by HOLD.
    second = await n.h.client.post("/v1/native-instances", json={**n.body, "request_id": str(uuid4())})
    assert second.status_code == 202 and len(n.calls) == 2


async def test_native_api_create_defaults_to_retained_but_requires_explicit_operator_retention(native_hub, monkeypatch):
    n = native_hub
    from palimpsest_hub.api.native_instances import NativeInstanceRequest

    body = {key: value for key, value in n.body.items() if key != "keep"}
    assert NativeInstanceRequest.model_validate(body).keep is True
    monkeypatch.setattr(
        n.service,
        "_operator_paths",
        lambda profile: {
            "grant_id": "no-retention",
            "project_id": PROJECT,
            "environment": profile["environment"],
            "source_digest": profile["manifest_digest"],
            "expires_unix": int(datetime.now(UTC).timestamp()) + 3600,
        },
    )
    response = await n.h.client.post("/v1/native-instances", json=body)
    assert response.status_code == 409 and n.calls == []


@pytest.mark.parametrize("mismatch", ["local-store", "other-share", "other-actor", "expired-approval"])
async def test_native_api_never_falls_back_on_storage_identity_or_grant_failure(native_hub, monkeypatch, mismatch):
    n = native_hub
    headers = {}
    if mismatch == "local-store":
        n.binding["kind"] = "local"
    elif mismatch == "other-share":
        n.profile["share_id"] = str(uuid4())
    elif mismatch == "other-actor":
        headers["X-User"] = OTHER
    else:
        monkeypatch.setattr(
            n.service,
            "_operator_paths",
            lambda profile: {
                "project_id": PROJECT,
                "environment": profile["environment"],
                "source_digest": profile["manifest_digest"],
                "expires_unix": 1,
                "allow_retained": True,
            },
        )
    response = await n.h.client.post("/v1/native-instances", json=n.body, headers=headers)
    assert response.status_code in {403, 409} and n.calls == []


async def test_native_sql_unknown_intent_cannot_disappear_or_be_adopted_on_resume(native_hub):
    n = native_hub
    from palimpsest_local.cloud_env import CloudEnvError

    item = (await n.h.client.post("/v1/native-instances", json=n.body)).json()
    actor = {"project_id": PROJECT, "user_id": USER}
    async with n.h.factory() as session:
        row = await session.get(PalimpsestNativeInstance, item["id"])
        profile_digest, run_id = row.profile_digest, row.run_id
        run = row.journal_json["run"]
        row.journal_json = {
            "run": run,
            "resources": {"native_servers": {"id": None, "status": "creating"}},
            "grant": {},
        }
        await session.commit()
    state = {"native": {"runs": {run_id: run}, "grants": {}}, "resources": {}}
    with pytest.raises(CloudEnvError, match="disappeared"):
        await n.service._checkpoint(n.h.factory, item["id"], actor, profile_digest, state, resuming=True)
    state["resources"] = {"native_servers": {run_id: {"id": str(uuid4()), "status": "confirmed"}}}
    with pytest.raises(CloudEnvError, match="never adopt"):
        await n.service._checkpoint(n.h.factory, item["id"], actor, profile_digest, state, resuming=True)


def test_native_result_projection_excludes_manifest_credentials_and_arbitrary_output():
    from palimpsest_hub.services.native_instances import _result_projection

    result = {
        "source": {"manifest": {"runtime": {"env": ["SECRET=private"]}}},
        "root": {"filesystem": "overlay", "pid1": 1, "ceph_secret": "private"},
        "checks": {"provider_readonly_errno": 13, "layers": [{"sha256": "a" * 64, "size": 128}]},
        "command": {"exit_code": 0, "uid": 1000, "gid": 1001, "stdout": "SECRET=private"},
        "gpu": {"enabled": False, "enrollment": {"secret": "private"}},
        "plan": {"client_key": "private"},
    }
    safe = _result_projection(result)
    assert safe["root"] == {"filesystem": "overlay", "pid1": 1}
    assert safe["command"] == {"exit_code": 0, "uid": 1000, "gid": 1001}
    assert "private" not in json.dumps(safe) and "SECRET" not in json.dumps(safe)
    assert _result_projection({"command": [], "gpu": [], "checks": []}) == {}


async def test_native_sql_checkpoint_never_refunds_operator_grant_budget(native_hub):
    n = native_hub
    from palimpsest_local.cloud_env import CloudEnvError

    item = (await n.h.client.post("/v1/native-instances", json=n.body)).json()
    async with n.h.factory() as session:
        row = await session.get(PalimpsestNativeInstance, item["id"])
        run, run_id, profile_digest = row.journal_json["run"], row.run_id, row.profile_digest
    state = {
        "native": {"runs": {run_id: run}, "grants": {"explicit-fixture": {"used": {"cpu_servers": 0}}}},
        "resources": {},
    }
    with pytest.raises(CloudEnvError, match="no budget refund"):
        await n.service._checkpoint(
            n.h.factory, item["id"], {"project_id": PROJECT, "user_id": USER}, profile_digest, state
        )


def test_native_sql_hold_sentinel_fences_a_fresh_process_before_any_operation(tmp_path):
    from palimpsest_local.cloud_env import CloudEnvError

    from palimpsest_hub.services.native_instances import _hold, _operator_paths

    root, publication, grant = tmp_path / "private", tmp_path / "payloads", tmp_path / "grant.json"
    root.mkdir(mode=0o700)
    (root / "prepared").mkdir(mode=0o700)
    publication.mkdir()
    grant.write_text('{"grant_id":"explicit"}')
    grant.chmod(0o600)
    profile = {
        "root": str(root),
        "environment": "prepared",
        "publication_root": str(publication),
        "grant_path": str(grant),
    }
    assert _operator_paths(profile)["grant_id"] == "explicit"
    _hold(profile)
    with pytest.raises(CloudEnvError, match="explicit operator reconciliation"):
        _operator_paths(profile)


async def test_native_visible_terminal_commit_with_lost_ack_cannot_admit_second_consumer(native_hub, monkeypatch):
    n = native_hub
    from pathlib import Path

    grant = {
        "grant_id": "explicit-fixture",
        "boot_gib": 32,
        "flavors": {"cpu": "small", "gpu": "small"},
        "project_id": PROJECT,
        "environment": n.profile["environment"],
        "source_digest": n.profile["manifest_digest"],
        "allow_retained": True,
        "allow_path_scoped_ceph": True,
        "expires_unix": int(datetime.now(UTC).timestamp()) + 3600,
    }
    grant_path = Path(n.profile["grant_path"])
    grant_path.write_text(json.dumps(grant))
    grant_path.chmod(0o600)
    monkeypatch.setattr(n.service, "_operator_paths", n.operator_paths)
    committed, release = asyncio.Event(), asyncio.Event()
    original_commit = n.service._commit_settlement

    async def lost_ack(factory, actor, identity, held):
        await original_commit(factory, actor, identity, held)
        committed.set()
        await release.wait()
        raise RuntimeError("terminal COMMIT acknowledgement lost")

    monkeypatch.setattr(n.service, "_commit_settlement", lost_ack)
    first = asyncio.create_task(n.h.client.post("/v1/native-instances", json={**n.body, "keep": False}))
    try:
        await asyncio.wait_for(committed.wait(), timeout=5)
        visible = (await n.h.client.get("/v1/native-instances")).json()[0]
        assert visible["status"] == "deleted"  # COMMIT is visible before acknowledgement.
        pending = Path(n.profile["root"]) / n.profile["environment"] / "hub-settlement-pending.json"
        assert pending.exists()
        second = await n.h.client.post("/v1/native-instances", json={**n.body, "request_id": str(uuid4())})
        assert second.status_code == 409 and len(n.calls) == 1
    finally:
        release.set()
        accepted = await asyncio.wait_for(first, timeout=5)
        assert accepted.status_code == 202
    assert pending.exists()
    assert pending.with_name("hub-sql-hold.json").exists()


async def test_native_completed_consumer_budget_survives_private_journal_rollback(native_hub, monkeypatch):
    n = native_hub
    first = await n.h.client.post("/v1/native-instances", json={**n.body, "keep": False})
    assert first.json()["status"] == "creating"
    assert (await n.h.client.get("/v1/native-instances/" + first.json()["id"])).json()["status"] == "deleted"

    def rolled_back(profile, actor, conn, checkpoint, run_id, image, request, approval_digest, deleting):
        # The actual adapter performs this checkpoint before its first provider
        # observation/effect; model the restored pre-A private grant ledger.
        checkpoint(
            {"native": {"runs": {}, "grants": {"explicit-fixture": {"used": {"cpu_servers": 0}}}}, "resources": {}}
        )
        raise AssertionError("rolled-back budget must never reach a provider effect")

    monkeypatch.setattr(n.service, "_execute", rolled_back)
    second = await n.h.client.post("/v1/native-instances", json={**n.body, "request_id": str(uuid4())})
    assert second.status_code == 202 and second.json()["status"] == "creating"
    assert (await n.h.client.get("/v1/native-instances/" + second.json()["id"])).json()["status"] == "held"
    assert len(n.calls) == 1


async def test_native_rechecks_durable_hold_after_waiting_for_environment_lock(native_hub, monkeypatch):
    n = native_hub
    import fcntl
    import os
    from contextlib import contextmanager
    from pathlib import Path

    from palimpsest_local.cloud_env import CloudEnvError

    directory = Path(n.profile["root"]) / n.profile["environment"]
    grant_path = Path(n.profile["grant_path"])
    grant_path.write_text("{}")
    grant_path.chmod(0o600)
    monkeypatch.setattr(n.service, "_operator_paths", n.operator_paths)
    original_lock = n.service._operation_lock
    waiting = Event()

    @contextmanager
    def signaled_lock(profile):
        waiting.set()
        with original_lock(profile) as parent:
            yield parent

    monkeypatch.setattr(n.service, "_operation_lock", signaled_lock)
    lock = os.open(directory / "operation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    driver = asyncio.create_task(
        asyncio.to_thread(
            n.original_execute, n.profile, {}, None, None, "native-" + "1" * 32, {}, {}, "unreached", False
        )
    )
    try:
        assert await asyncio.to_thread(waiting.wait, 5)
        n.service._hold(n.profile)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    with pytest.raises(CloudEnvError, match="uncertainty sentinel"):
        await asyncio.wait_for(driver, timeout=5)


@pytest.mark.parametrize("case", ["oversized", "metadata-key", "descriptor-key", "command"])
async def test_native_consumer_bounds_reject_before_sql_intent_or_driver(native_hub, case):
    n = native_hub
    from palimpsest_local.image_manifest import manifest_digest, normalize_manifest

    manifest = n.published["manifest"]
    if case == "oversized":
        manifest["metadata"] = {str(index): "x" * 16384 for index in range(16)}
    elif case == "metadata-key":
        manifest["metadata"] = {"x" * 129: "valid publication, invalid bootstrap key"}
    elif case == "descriptor-key":
        manifest["layers"][0]["source"] = {"annotations": {"x" * 129: "valid descriptor metadata"}}
    else:
        n.body["command"] = ["x" * 8193]
    n.published["manifest"] = normalize_manifest(manifest)
    n.published["manifest_digest"] = manifest_digest(manifest)
    n.profile["manifest_digest"] = n.published["manifest_digest"]
    response = await n.h.client.post("/v1/native-instances", json=n.body)
    assert response.status_code in {409, 422}, response.text
    assert n.calls == []
    assert n.h.provider.creates == n.h.provider.volume_creates == []
    async with n.h.factory() as session:
        assert (await session.scalars(select(PalimpsestNativeInstance))).all() == []
        refs = (
            await session.scalars(
                select(RuntimeBlobReference).where(RuntimeBlobReference.resource_kind == "native-instance")
            )
        ).all()
        assert refs == []


@pytest.mark.parametrize("prior_journal", [None, [], "", 0, False, {"grant": []}, {"resources": None}])
async def test_native_prior_sql_json_null_is_empty_but_other_corruption_holds(native_hub, prior_journal):
    n = native_hub
    from palimpsest_local.cloud_env import CloudEnvError

    first = (await n.h.client.post("/v1/native-instances", json={**n.body, "keep": False})).json()
    async with n.h.factory() as session:
        previous = await session.get(PalimpsestNativeInstance, first["id"])
        previous.journal_json = prior_journal
        await session.commit()
    second = (await n.h.client.post("/v1/native-instances", json={**n.body, "request_id": str(uuid4())})).json()
    assert second["status"] == "creating"
    terminal = (await n.h.client.get("/v1/native-instances/" + second["id"])).json()
    assert terminal["status"] == ("retained" if prior_journal is None else "held")
    if prior_journal is None:
        assert len(n.calls) == 2
    else:
        with pytest.raises(CloudEnvError, match="corrupt; HOLD"):
            n.service._journal(prior_journal)


async def test_native_exact_delete_reloads_large_private_journal_after_two_successful_runs(native_hub, monkeypatch):
    n = native_hub
    import copy
    from pathlib import Path

    from palimpsest_local.cloud_env import SCHEMA, CloudEnv, CloudEnvError
    from palimpsest_local.nova_native_cloud import _private_json

    initial = CloudEnv(n.profile["environment"], root=Path(n.profile["root"]))
    initial.state = {
        "schema": SCHEMA,
        "name": initial.name,
        "cloud": "unused-original-caller-only",
        "project_id": PROJECT,
        "user_id": USER,
        "resources": {},
        "history": [],
        "native": {
            "preparation": {"config": {"dns": ["8.8.8.8"]}},
            "runs": {},
            "grants": {
                "explicit-fixture": {
                    "digest": "fixture-approval",
                    "used": {"cpu_servers": 0},
                    "gpu_seconds": 0,
                }
            },
        },
    }
    initial.save()
    expected_servers, deleted_servers, observed_prior_runs = {}, [], []

    def driver(profile, actor, conn, checkpoint, run_id, image, request, approval_digest, deleting):
        env = n.service._CallerEnvironment(profile, actor, conn, checkpoint)
        runs = env.state["native"]["runs"]
        if deleting:
            # This lookup must use the newly opened actual journal, not SQL's
            # projection, a cached state, a reconstructed run, or a new grant.
            assert env.state_path.stat().st_size > 1024 * 1024
            record = env.state["resources"]["native_servers"][run_id]
            assert record["id"] == expected_servers[run_id]
            deleted_servers.append(record["id"])
            del env.state["resources"]["native_servers"][run_id]
            env.state["history"].append(
                {
                    "kind": "native_servers",
                    "role": run_id,
                    "resource": record,
                    "absent_at": 1234567890,
                }
            )
            runs[run_id]["status"] = "cleaned"
            env.save()
            return
        observed_prior_runs.append(len(runs))
        server_id = str(uuid4())
        expected_servers[run_id] = server_id
        env.state["native"]["grants"]["explicit-fixture"]["used"]["cpu_servers"] += 1
        runs[run_id] = {
            "run_id": run_id,
            "status": "retained" if request["keep"] else "cleaned",
            "server_id": server_id,
            "outcome": "success",
            "command": request["command"],
            "keep": request["keep"],
            "plan": {"image": copy.deepcopy(image["manifest"])},
            "verified_result": {"command": {"exit_code": 0}, "chunks": ["x" * 65536] * 9},
        }
        if request["keep"]:
            env.state["resources"].setdefault("native_servers", {})[run_id] = {
                "id": server_id,
                "status": "confirmed",
            }
        else:
            deleted_servers.append(server_id)
            env.state["history"].append(
                {
                    "kind": "native_servers",
                    "role": run_id,
                    "resource": {"id": server_id},
                    "absent_at": 1234567890,
                }
            )
        env.save()

    monkeypatch.setattr(n.service, "_execute", driver)
    for _ in range(2):
        response = await n.h.client.post(
            "/v1/native-instances", json={**n.body, "request_id": str(uuid4()), "keep": False}
        )
        assert response.status_code == 202 and response.json()["status"] == "creating", response.text
        assert (await n.h.client.get("/v1/native-instances/" + response.json()["id"])).json()["status"] == "deleted"
    assert initial.state_path.stat().st_size > 1024 * 1024
    with pytest.raises(CloudEnvError, match="owned private"):
        _private_json(initial.state_path)  # Grant/config limits remain unchanged.
    retained = await n.h.client.post("/v1/native-instances", json={**n.body, "request_id": str(uuid4())})
    assert retained.status_code == 202 and retained.json()["status"] == "creating", retained.text
    assert (await n.h.client.get("/v1/native-instances/" + retained.json()["id"])).json()["status"] == "retained"
    response = await n.h.client.delete("/v1/native-instances/" + retained.json()["id"])
    assert response.status_code == 202 and response.json()["status"] == "deleting", response.text
    assert (await n.h.client.get("/v1/native-instances/" + retained.json()["id"])).json()["status"] == "deleted"
    assert observed_prior_runs == [0, 1, 2]
    reread = n.service._CallerEnvironment(n.profile, {}, None, None).state
    assert len(reread["native"]["runs"]) == 3 and len(reread["history"]) == 3
    assert reread["native"]["grants"]["explicit-fixture"]["used"]["cpu_servers"] == 3
    assert reread["resources"]["native_servers"] == {}
    assert deleted_servers == list(expected_servers.values())


async def test_native_nine_prepared_resolvers_rejected_before_sql_intent_or_driver(native_hub):
    n = native_hub
    from pathlib import Path

    from palimpsest_local.cloud_env import CloudEnv

    env = CloudEnv(n.profile["environment"], root=Path(n.profile["root"]))
    env.state["native"]["preparation"]["config"]["dns"] = [f"10.0.0.{index}" for index in range(1, 10)]
    env.save()
    response = await n.h.client.post("/v1/native-instances", json=n.body)
    assert response.status_code == 409, response.text
    assert n.calls == [] and n.h.provider.creates == n.h.provider.volume_creates == []
    async with n.h.factory() as session:
        assert (await session.scalars(select(PalimpsestNativeInstance))).all() == []
        assert (
            await session.scalars(
                select(RuntimeBlobReference).where(RuntimeBlobReference.resource_kind == "native-instance")
            )
        ).all() == []


@pytest.mark.parametrize(
    "case",
    [
        "foreign-project",
        "digest-not-null",
        "fixed-profile",
        "gpu-limit",
        "gpu-flavor",
        "gpu-seconds",
        "gpu-request",
    ],
)
def test_native_project_image_approval_rejects_scope_expansion_before_effects(case):
    from palimpsest_local.cloud_env import CloudEnvError

    from palimpsest_hub.services.native_instances import _approval_source

    profile = {"project_id": PROJECT, "manifest_digest": None}
    approval = {
        "project_id": PROJECT,
        "source_project_id": PROJECT,
        "source_digest": None,
        "limits": {"gpu_servers": 0},
        "flavors": {"gpu": None},
        "max_gpu_seconds": 0,
    }
    phase = "cpu"
    if case == "foreign-project":
        approval["source_project_id"] = OTHER
    elif case == "digest-not-null":
        approval["source_digest"] = "sha256:" + "a" * 64
    elif case == "fixed-profile":
        profile["manifest_digest"] = "sha256:" + "a" * 64
    elif case == "gpu-limit":
        approval["limits"]["gpu_servers"] = 1
    elif case == "gpu-flavor":
        approval["flavors"]["gpu"] = str(uuid4())
    elif case == "gpu-seconds":
        approval["max_gpu_seconds"] = 1
    elif case == "gpu-request":
        phase = "gpu"
    with pytest.raises(CloudEnvError):
        _approval_source(profile, approval, phase, "sha256:" + "b" * 64)


def test_native_project_image_approval_does_not_rewrite_grant_for_new_digests():
    from palimpsest_hub.services.native_instances import _approval_source

    profile = {"project_id": PROJECT, "manifest_digest": None}
    approval = {
        "project_id": PROJECT,
        "source_project_id": PROJECT,
        "source_digest": None,
        "limits": {"gpu_servers": 0},
        "flavors": {"gpu": None},
        "max_gpu_seconds": 0,
    }
    original = json.dumps(approval, sort_keys=True)
    for digest in ("sha256:" + "a" * 64, "sha256:" + "b" * 64):
        _approval_source(profile, approval, "cpu", digest)
    assert json.dumps(approval, sort_keys=True) == original


async def test_native_published_requires_explicit_namespace_permission_before_sql_intent(native_hub, monkeypatch):
    n = native_hub
    monkeypatch.setattr(
        n.service,
        "_operator_paths",
        lambda profile: {
            "grant_id": "old-manila-approval",
            "project_id": PROJECT,
            "environment": profile["environment"],
            "source_digest": profile["manifest_digest"],
            "allow_retained": True,
            "expires_unix": int(datetime.now(UTC).timestamp()) + 3600,
        },
    )
    response = await n.h.client.post("/v1/native-instances", json=n.body)
    assert response.status_code == 409 and n.calls == []
    async with n.h.factory() as session:
        assert (await session.scalars(select(PalimpsestNativeInstance))).all() == []


@pytest.mark.parametrize(
    "visibility,source_project,source_user",
    [
        ("private", OTHER, USER),
        ("internal", PROJECT, OTHER),
        ("public", OTHER, OTHER),
    ],
)
async def test_native_namespace_manager_receives_original_target_and_authorized_source_scope(
    native_hub,
    monkeypatch,
    visibility,
    source_project,
    source_user,
):
    from palimpsest_local import ceph_path_access
    from palimpsest_local.cloud_env import CloudEnvError

    n = native_hub
    issued = []
    from palimpsest_hub.services.ceph_scoped_store import namespace_for, scope_for

    n.published.update(project_id=source_project, created_by=source_user, visibility=visibility)
    scope = scope_for({"project_id": source_project, "user_id": source_user}, visibility, n.profile["share_id"])
    n.binding.update(
        scope_key=scope,
        export=n.profile["export"] + "/cas/" + scope,
        namespace=namespace_for(n.profile["share_id"], scope),
    )

    class Manager:
        def __init__(self, env, configuration, binding):
            self.env, self.configuration, self.binding = env, configuration, binding
            self.preflight_called = False

        def preflight(self):
            self.preflight_called = True

        def issue(self, role):
            issued.append(role)
            return {"id": "synthetic-ceph-client", "type": "ceph-path"}

        def access(self, role):
            assert role in issued
            return {
                "export": self.binding["export"],
                "access_to": "synthetic-ceph-client",
                "access_key": "synthetic-private-key",
                "fs_name": self.binding["fs_name"],
            }

        def revoke(self, role):
            issued.remove(role)

    monkeypatch.setattr(ceph_path_access, "CephPathAccessManager", Manager)
    env = n.service._CallerEnvironment(n.profile, {}, None, None)
    n.storage_admission(n.profile, {"allow_path_scoped_ceph": True}, env, n.published)
    assert env._native_capability_manager.preflight_called
    assert env._native_capability_manager.configuration["fs_name"] == n.profile["ceph_fs_name"]
    env._native_authorize = lambda: None
    env.grant("native-synthetic", "ro")
    env.state.setdefault("resources", {}).setdefault("access_rules", {})["native-synthetic"] = {
        "provider": "ceph-namespace-v1",
        "binding": n.binding,
    }
    credential = env.native_ceph_access({"run_id": "native-synthetic"})
    assert credential == {
        "export": n.binding["export"],
        "name": "synthetic-ceph-client",
        "secret": "synthetic-private-key",
        "fs_name": n.binding["fs_name"],
    }
    with pytest.raises(CloudEnvError):
        env.grant("native-rw", "rw")
    env._revoke("native-synthetic")
    assert issued == []
    assert env.state["project_id"] == PROJECT
    assert env._native_capability_manager.binding["scope_key"] == scope
    for field in ("export", "namespace", "data_pool", "fs_name"):
        changed = copy.deepcopy(n.published)
        changed["storage_binding"][field] += "-foreign"
        with pytest.raises(CloudEnvError):
            n.storage_admission(n.profile, {"allow_path_scoped_ceph": True}, env, changed)


async def test_native_asgi_sends_pending_202_before_driver_and_uses_fresh_closed_connections(native_hub, monkeypatch):
    from palimpsest_hub import auth as native_auth

    n = native_hub
    started, release = Event(), Event()
    opened, closed = [], []
    server_id = str(uuid4())
    response_sent = asyncio.get_running_loop().create_future()

    async def fresh_connection(token_info):
        conn = Provider()
        conn._afterglow_token = token_info["token"]
        opened.append(conn)
        try:
            yield conn
        finally:
            closed.append(conn)

    def driver(profile, actor, conn, checkpoint, run_id, image, request, approval_digest, deleting):
        assert conn is not n.h.provider and conn in opened
        n.calls.append((run_id, deleting))
        if not deleting:
            started.set()
            assert release.wait(5), "protocol test must release native driver custody"
        checkpoint(
            {
                "native": {
                    "runs": {
                        run_id: {
                            "run_id": run_id,
                            "status": "cleaned" if deleting else "retained",
                            "server_id": server_id,
                            "keep": True,
                        }
                    },
                    "grants": {},
                },
                "resources": {},
            }
        )

    app = n.h.client._transport.app

    async def observed(scope, receive, send):
        status, body = None, bytearray()

        async def captured(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False) and not response_sent.done():
                    response_sent.set_result((status, bytes(body)))
            await send(message)

        await app(scope, receive, captured)

    monkeypatch.setattr(native_auth, "get_os_conn", fresh_connection)
    monkeypatch.setattr(n.service, "_execute", driver)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observed),
        base_url="http://test",
        headers=n.h.client.headers,
    ) as client:
        request = asyncio.create_task(client.post("/v1/native-instances", json=n.body))
        try:
            status, body = await asyncio.wait_for(response_sent, 5)
            pending = json.loads(body)
            assert status == 202 and pending["status"] == "creating"
            assert await asyncio.to_thread(started.wait, 5)
            assert not request.done()
            current = (await n.h.client.get("/v1/native-instances/" + pending["id"])).json()
            assert current["status"] == "creating"
            duplicate = await n.h.client.post("/v1/native-instances", json=n.body)
            assert duplicate.json()["id"] == pending["id"] and duplicate.json()["status"] == "creating"
            assert len(n.calls) == len(opened) == 1 and closed == []
            assert (await n.h.client.delete("/v1/native-instances/" + pending["id"])).status_code == 409
        finally:
            release.set()
            await asyncio.wait_for(request, 5)
    assert closed == opened
    assert (await n.h.client.get("/v1/native-instances/" + pending["id"])).json()["status"] == "retained"
    deletion = await n.h.client.delete("/v1/native-instances/" + pending["id"])
    assert deletion.status_code == 202 and deletion.json()["status"] == "deleting"
    assert (await n.h.client.get("/v1/native-instances/" + pending["id"])).json()["status"] == "deleted"
    assert len(opened) == len(closed) == len(n.calls) == 2
    async with n.h.factory() as session:
        row = await session.get(PalimpsestNativeInstance, pending["id"])
        assert "fixture-original-human-token" not in json.dumps(row.spec_json)


@pytest.mark.parametrize("failure", ["expired-token", "changed-user", "lost-membership"])
async def test_native_background_fresh_authority_failure_holds_without_connection_or_effects(
    native_hub,
    monkeypatch,
    failure,
):
    from palimpsest_hub import auth as native_auth

    n = native_hub

    def refresh(token, project):
        if failure == "expired-token":
            raise HTTPException(401, "expired")
        return {"token": token, "project_id": project, "user_id": OTHER if failure == "changed-user" else USER}

    async def membership(token_info):
        return {**token_info, "package_actions": []}

    async def forbidden_connection(token_info):
        raise AssertionError("invalid fresh authority must not open an OpenStack connection")
        yield

    monkeypatch.setattr(n.service, "validate_token", refresh)
    monkeypatch.setattr(native_auth, "get_os_conn", forbidden_connection)
    if failure == "lost-membership":
        monkeypatch.setattr(native_auth, "get_package_manager_info", membership)
    response = await n.h.client.post("/v1/native-instances", json=n.body)
    assert response.status_code == 202 and response.json()["status"] == "creating"
    assert (await n.h.client.get("/v1/native-instances/" + response.json()["id"])).json()["status"] == "held"
    assert n.calls == [] and n.h.provider.creates == n.h.provider.volume_creates == []


async def test_native_delete_asgi_sends_pending_before_exact_cleanup_and_never_reschedules(native_hub, monkeypatch):
    n = native_hub
    created = (await n.h.client.post("/v1/native-instances", json=n.body)).json()
    identity = created["id"]
    async with n.h.factory() as session:
        row = await session.get(PalimpsestNativeInstance, identity)
        run = copy.deepcopy(row.journal_json["run"])
        grants = copy.deepcopy(row.journal_json["grant"])
    started, release = Event(), Event()
    sent = asyncio.get_running_loop().create_future()

    def cleanup(profile, actor, conn, checkpoint, run_id, image, request, approval_digest, deleting):
        assert deleting and run_id == run["run_id"]
        n.calls.append((run_id, deleting))
        started.set()
        assert release.wait(5)
        checkpoint({"native": {"runs": {run_id: {**run, "status": "cleaned"}}, "grants": grants}, "resources": {}})

    app = n.h.client._transport.app

    async def observed(scope, receive, send):
        status = None

        async def captured(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body", False) and not sent.done():
                sent.set_result((status, json.loads(message["body"])))
            await send(message)

        await app(scope, receive, captured)

    monkeypatch.setattr(n.service, "_execute", cleanup)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observed),
        base_url="http://test",
        headers=n.h.client.headers,
    ) as client:
        deletion = asyncio.create_task(client.delete("/v1/native-instances/" + identity))
        try:
            status, pending = await asyncio.wait_for(sent, 5)
            assert status == 202 and pending["status"] == "deleting"
            assert await asyncio.to_thread(started.wait, 5)
            assert not deletion.done()
            assert (await n.h.client.get("/v1/native-instances/" + identity)).json()["status"] == "deleting"
            assert (await n.h.client.delete("/v1/native-instances/" + identity)).status_code == 409
            assert len(n.calls) == 2
        finally:
            release.set()
            await asyncio.wait_for(deletion, 5)
    assert (await n.h.client.get("/v1/native-instances/" + identity)).json()["status"] == "deleted"
    assert (await n.h.client.delete("/v1/native-instances/" + identity)).json()["status"] == "deleted"
    assert len(n.calls) == 2


async def test_native_background_never_overwrites_a_terminal_row(native_hub, monkeypatch):
    n = native_hub
    created = (await n.h.client.post("/v1/native-instances", json={**n.body, "keep": False})).json()
    identity = created["id"]
    async with n.h.factory() as session:
        row = await session.get(PalimpsestNativeInstance, identity)
        assert row.status == "deleted"
        digest = row.profile_digest
    calls = list(n.calls)
    original = {"project_id": PROJECT, "user_id": USER, "token": n.h.provider._afterglow_token}
    await n.service._background_run(n.h.factory, n.h.settings, original, identity, digest, False)
    await n.service._commit_settlement(n.h.factory, original, identity, True)
    assert (await n.h.client.get("/v1/native-instances/" + identity)).json()["status"] == "deleted"
    assert n.calls == calls


async def test_real_native_execute_refuses_on_exhausted_quota_before_controller_run(native_hub, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import pytest

    n = native_hub
    approval = {
        "grant_id": "explicit-fixture",
        "boot_gib": 32,
        "flavors": {"cpu": "small", "gpu": "small"},
        "source_digest": n.profile["manifest_digest"],
        "project_id": PROJECT,
        "environment": n.profile["environment"],
    }

    class Environment:
        def __init__(self, profile, actor, conn, checkpoint):
            self.state = {"cloud": "original"}

        def _authenticate(self, cloud):
            pass

        def _confirmed(self, kind, name):
            return {"id": n.profile["share_id"], "export": n.profile["export"]}

    run = Mock()

    class Controller:
        def __init__(self, env):
            self.state = {"runs": {}}
            self.run = run

    monkeypatch.setattr(n.service, "_CallerEnvironment", Environment)
    monkeypatch.setattr(n.service, "NativeCloud", Controller)
    monkeypatch.setattr(n.service, "_operator_paths", lambda profile: approval)
    monkeypatch.setattr(n.service, "_published_storage_admission", Mock())
    monkeypatch.setattr(n.service, "_approval_source", Mock())
    n.h.provider.get_limits = lambda: SimpleNamespace(
        absolute=SimpleNamespace(
            instances=-1, instances_used=0, total_cores=0, total_cores_used=0, total_ram=-1, total_ram_used=0
        )
    )
    published = {**n.published, "storage_binding": {"kind": "manila"}}
    checkpoint = SimpleNamespace(settings=n.h.settings)
    request = {"phase": "cpu", "timeout": 60, "command": ["/bin/true"], "keep": True}
    actor = {"project_id": PROJECT, "user_id": USER, "package_actions": ["packages:write"]}
    with pytest.raises(n.service._PreflightRefused):
        n.original_execute(
            n.profile,
            actor,
            n.h.provider,
            checkpoint,
            "native-" + "2" * 32,
            published,
            request,
            n.service._digest(approval),
            False,
        )
    run.assert_not_called()


async def test_real_native_delete_skips_new_image_admission_for_legacy_run(native_hub, monkeypatch):
    n = native_hub
    from unittest.mock import Mock

    role = "native-" + "1" * 32
    approval = {
        "grant_id": "historical-cleanup",
        "source_digest": n.profile["manifest_digest"],
        "project_id": PROJECT,
        "environment": n.profile["environment"],
        "actions": ["cleanup"],
    }

    class Environment:
        def __init__(self, profile, actor, conn, checkpoint):
            self.state = {"cloud": "original"}

        def _authenticate(self, cloud):
            assert cloud == "original"

        def _confirmed(self, kind, name):
            assert (kind, name) == ("shares", "layers")
            return {"id": n.profile["share_id"], "export": n.profile["export"]}

    cleanup = Mock(return_value={"status": "cleaned"})

    class Controller:
        def __init__(self, env):
            self.state = {"runs": {role: {"source_digest": approval["source_digest"]}}}
            self.cleanup = cleanup

    monkeypatch.setattr(n.service, "_CallerEnvironment", Environment)
    monkeypatch.setattr(n.service, "NativeCloud", Controller)
    monkeypatch.setattr(n.service, "_operator_paths", lambda profile: approval)
    monkeypatch.setattr(
        n.service,
        "_published_storage_admission",
        Mock(
            side_effect=AssertionError("cleanup must not require new namespace/run admission"),
        ),
    )
    published = {**n.published, "storage_binding": {"kind": "manila"}}
    result = n.original_execute(
        n.profile,
        {},
        n.h.provider,
        lambda state: None,
        role,
        published,
        {"phase": "cpu"},
        n.service._digest(approval),
        True,
    )
    assert result == {"status": "cleaned"}
    from pathlib import Path

    cleanup.assert_called_once_with(grant_path=Path(n.profile["grant_path"]), run_id=role)


def test_native_legacy_access_cleanup_dispatches_to_original_manila_path(monkeypatch):
    from unittest.mock import Mock

    from palimpsest_local.cloud_env import CloudEnv

    from palimpsest_hub.services.native_instances import _CallerEnvironment

    env = object.__new__(_CallerEnvironment)
    env.state = {"resources": {"access_rules": {"native-old": {"id": "old-manila-rule", "access_to": "old"}}}}
    legacy = Mock()
    monkeypatch.setattr(CloudEnv, "_revoke", legacy)
    env._revoke("native-old")
    legacy.assert_called_once_with("native-old")


async def test_worker_published_payload_remains_consumable_without_private_result_leak(instance_hub, monkeypatch):
    from palimpsest_local.image_manifest import manifest_from_result

    from palimpsest_hub.image_models import PalimpsestHubImageJob, PalimpsestHubPublicationPayload
    from palimpsest_hub.services import published_images

    h = instance_hub
    payload = b"hsqs-new-worker-contract"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    source = h.store.root / "new-worker-output"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    h.store.publish_verified(source, h.store.inspect_file(source, max_bytes=len(payload)))
    runtime = {
        "entrypoint": ["/bin/sh"],
        "cmd": ["-c", "echo private"],
        "env": ["PRIVATE_ENV=value"],
        "working_dir": "/",
        "user": "0",
    }
    manifest = manifest_from_result(
        {
            "layers": [{"digest": digest, "size_bytes": len(payload)}],
            "runtime": runtime,
            "source": {},
        },
        os_family="fixture-os",
        os_version="1",
        source_kind="local",
    )
    monkeypatch.setattr(published_images, "get_settings", lambda: SimpleNamespace())
    job_id, job_request = str(uuid4()), str(uuid4())
    async with h.factory() as session:
        session.add(
            PalimpsestHubPublicationPayload(
                project_id=PROJECT,
                created_by=USER,
                blob_digest=digest,
                size_bytes=len(payload),
            )
        )
        await session.commit()
        await published_images.register_image(
            session,
            h.store,
            {"project_id": PROJECT, "user_id": USER},
            job_request,
            "worker-private",
            manifest,
            image_id=job_id,
            require_staged=True,
        )
        session.add(
            PalimpsestHubImageJob(
                id=job_id,
                project_id=PROJECT,
                created_by=USER,
                request_id=job_request,
                name="worker-private",
                kind="dockerfile",
                request_digest=digest,
                request_json={},
                status="complete",
                output_digests=[digest],
                result_json={"layers": manifest["layers"], "source": {}, "runtime": runtime, "manifest": manifest},
            )
        )
        await session.commit()
    request = request_body(command=[])
    request.update(layer_digests=[], image_job_id=job_id)
    response = await create_and_advance(h, json=request)
    assert response.status_code == 202, response.text
    item = response.json()
    assert item["mode"] == "oci-chroot"
    assert guest_spec(h.provider)["runtime"]["env"] == ["PRIVATE_ENV=value"]
    assert guest_spec(h.provider)["command"] == ["/bin/sh", "-c", "echo private"]
    outsider = "e" * 32
    h.provider._afterglow_user_id = outsider
    headers = {"X-User": outsider}
    assert (await h.client.get("/v1/instances/" + item["id"], headers=headers)).status_code == 404
    visible = await h.client.get("/v1/instances", headers=headers)
    assert all(row["id"] != item["id"] for row in visible.json())
    retry = await h.client.post("/v1/instances", json=request, headers=headers)
    assert retry.status_code == 404
    fresh = await h.client.post("/v1/instances", json={**request, "request_id": str(uuid4())}, headers=headers)
    assert fresh.status_code == 404
    assert len(h.provider.creates) == len(h.provider.volume_creates) == 1


async def test_withdrawn_self_cleanup_action_cannot_stop_delete_or_dispose_volume(instance_hub):
    h = instance_hub
    member = {"X-Actions": "packages:write,packages:inventory,packages:read"}
    created = await create_and_advance(h, json=request_body(), headers=member)
    assert created.status_code == 202
    item = created.json()
    path = "/v1/instances/" + item["id"]
    assert (await h.client.post(path + "/stop", headers=member)).status_code == 403
    assert (await h.client.delete(path, headers=member)).status_code == 403
    # Even the alternate advance route must require manager authority for disposal.
    disposal = await advance_instance(h, item, operation="delete-volume", headers=member)
    assert disposal.status_code == 403
    assert h.provider.stops == h.provider.deletes == h.provider.volume_deletes == []
    assert (await h.client.post(path + "/stop")).status_code == 202
    assert (await h.client.delete(path)).status_code == 202


async def test_native_member_without_self_cleanup_action_cannot_delete_retained_consumer(native_hub):
    n = native_hub
    member = {"X-Actions": "packages:write,packages:inventory,packages:read"}
    created = await n.h.client.post("/v1/native-instances", headers=member, json=n.body)
    assert created.status_code == 202
    path = "/v1/native-instances/" + created.json()["id"]
    calls = list(n.calls)
    assert (await n.h.client.delete(path, headers=member)).status_code == 403
    assert n.calls == calls


async def test_native_delete_background_withdrawn_cleanup_action_holds_without_effect(native_hub, monkeypatch):
    from palimpsest_hub import auth

    n = native_hub
    item = (await n.h.client.post("/v1/native-instances", json=n.body)).json()
    calls = list(n.calls)

    async def current_member(token_info):
        return {**token_info, "package_actions": {"packages:write", "packages:read"}}

    monkeypatch.setattr(auth, "get_package_manager_info", current_member)
    path = "/v1/native-instances/" + item["id"]
    accepted = await n.h.client.delete(path)
    assert accepted.status_code == 202
    assert (await n.h.client.get(path)).json()["status"] == "held"
    assert n.calls == calls


@pytest.mark.parametrize("fence", ["missing", "revoked"])
def test_native_effect_authentication_requires_current_action_fence(monkeypatch, fence):
    from palimpsest_local.cloud_env import CloudEnvError

    from palimpsest_hub.services import native_instances as native

    env = object.__new__(native._CallerEnvironment)
    env.actor = {"token": "original", "project_id": PROJECT}
    env.caller, env.sql_held = object(), False
    env.profile = {"project_id": PROJECT, "user_id": USER}
    env.state = {"project_id": PROJECT, "user_id": USER}
    observed = []

    def checkpoint(state):
        observed.append(state)

    if fence == "revoked":

        def deny():
            raise CloudEnvError("current manager action revoked")

        checkpoint.authorize_actor = deny
    env.checkpoint = checkpoint
    monkeypatch.setattr(native, "_scope", lambda *args: None)
    monkeypatch.setattr(native, "validate_token", lambda *args: {"project_id": PROJECT, "user_id": USER})
    with pytest.raises(CloudEnvError, match="action"):
        env._authenticate("original")
    assert observed == []


@pytest.mark.parametrize("mismatch", [None, "request", "volume", "ownership"])
async def test_project_manager_disposes_other_creators_recorded_prepared_root(instance_hub, mismatch):
    h = instance_hub
    creator = {"X-Actions": "packages:write,packages:inventory,packages:read"}
    accepted = await h.client.post("/v1/instances", headers=creator, json=request_body())
    assert accepted.status_code == 202
    item = accepted.json()
    identity, volume_id = item["id"], item["boot_volume_id"]
    path = "/v1/instances/" + identity
    original_markers = dict(h.provider.volumes_by_id[volume_id]["metadata"])
    h.provider._afterglow_user_id = OTHER
    manager = {"X-User": OTHER, "X-Actions": "packages:write,packages:manage,packages:inventory"}
    # Creation stays creator-bound even when the other caller is a manager.
    assert (await advance_instance(h, item, headers=manager)).status_code == 403
    deleting = await h.client.delete(path, headers=manager)
    assert deleting.status_code == 202 and deleting.json()["status"] == "deleting"
    assert h.provider.volume_deletes == h.provider.creates == []
    if mismatch == "request":
        item = {**item, "request_id": str(uuid4())}
    elif mismatch == "volume":
        item = {**item, "boot_volume_id": str(uuid4())}
    elif mismatch == "ownership":
        h.provider.volumes_by_id[volume_id]["project_id"] = OTHER
    disposed = await advance_instance(h, item, operation="delete-volume", headers=manager)
    assert disposed.status_code == (409 if mismatch else 202), disposed.text
    assert h.provider.volume_deletes == ([] if mismatch else [volume_id])
    assert h.provider.creates == h.provider.deletes == []
    if mismatch is None:
        observed = await h.client.get(path, headers=manager)
        assert observed.status_code == 200 and observed.json()["status"] == "deleted"
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, identity)
        assert row.created_by == USER and row.project_id == PROJECT and row.boot_volume_id == volume_id
        assert row.status == ("deleting" if mismatch else "deleted")
    if mismatch:
        assert h.provider.volumes_by_id[volume_id]["metadata"] == original_markers


async def test_member_creator_cleans_private_instance_without_widening_other_manager_reach(instance_hub):
    h = instance_hub
    member = {"X-Actions": "packages:write,packages:inventory,packages:read,packages:manage_own"}
    created = await create_and_advance(h, json=request_body(), headers=member)
    assert created.status_code == 202
    item = created.json()
    path = "/v1/instances/" + item["id"]
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        row.spec_json = {**row.spec_json, "published_visibility": "private"}
        await session.commit()
    h.provider._afterglow_user_id = OTHER
    for actions in ("packages:manage_own", "packages:manage"):
        other = {"X-User": OTHER, "X-Actions": actions}
        assert (await h.client.post(path + "/stop", headers=other)).status_code == 404
        assert (await h.client.delete(path, headers=other)).status_code == 404
    assert h.provider.stops == h.provider.deletes == []
    h.provider._afterglow_user_id = USER
    assert (await h.client.post(path + "/stop", headers=member)).status_code == 202
    assert (await h.client.delete(path, headers=member)).status_code == 202
    assert h.provider.stops == h.provider.deletes == [item["server_id"]]
    assert h.provider.volumes_by_id == {}


@pytest.mark.parametrize("write_allowed", [True, False])
async def test_member_creator_disposes_only_own_exact_prepared_boot_root(instance_hub, write_allowed):
    h = instance_hub
    member = {"X-Actions": "packages:write,packages:inventory,packages:read,packages:manage_own"}
    accepted = await h.client.post("/v1/instances", json=request_body(), headers=member)
    assert accepted.status_code == 202
    item = accepted.json()
    path = "/v1/instances/" + item["id"]
    if not write_allowed:
        member = {"X-Actions": "packages:inventory,packages:read,packages:manage_own"}
        assert (await advance_instance(h, item, headers=member)).status_code == 403
    h.provider._afterglow_user_id = OTHER
    other = {**member, "X-User": OTHER}
    assert (await h.client.delete(path, headers=other)).status_code == 403
    assert (await advance_instance(h, item, operation="delete-volume", headers=other)).status_code == 403
    h.provider._afterglow_user_id = USER
    assert (await h.client.delete(path, headers=member)).status_code == 202
    assert h.provider.volume_deletes == []
    disposed = await advance_instance(h, item, operation="delete-volume", headers=member)
    assert disposed.status_code == 202
    assert h.provider.volume_deletes == [item["boot_volume_id"]]
    assert (await h.client.get(path, headers=member)).json()["status"] == "deleted"
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, item["id"])
        assert row.created_by == USER and row.project_id == PROJECT


@pytest.mark.parametrize("operation", ["stop", "delete"])
async def test_creator_cleanup_action_withdrawal_before_effect_never_mutates_provider(
    instance_hub, monkeypatch, operation
):
    h = instance_hub
    member = {"X-Actions": "packages:write,packages:inventory,packages:read,packages:manage_own"}
    item = (await create_and_advance(h, json=request_body(), headers=member)).json()
    path = "/v1/instances/" + item["id"]
    original = service.fresh_instance_controller
    checks = 0

    async def withdraw(actor, conn, row):
        nonlocal checks
        checks += 1
        if checks == 2:
            actor = {**actor, "package_actions": set(actor["package_actions"]) - {"packages:manage_own"}}
        return await original(actor, conn, row)

    monkeypatch.setattr(service, "fresh_instance_controller", withdraw)
    response = await (
        h.client.post(path + "/stop", headers=member) if operation == "stop" else h.client.delete(path, headers=member)
    )
    assert response.status_code == 403 and checks == 2
    assert h.provider.stops == h.provider.deletes == h.provider.volume_deletes == []


async def test_native_profile_bound_member_can_delete_own_retained_consumer(native_hub, monkeypatch):
    from palimpsest_hub import auth

    n = native_hub
    actions = {"packages:write", "packages:read", "packages:inventory", "packages:manage_own"}

    async def current_member(token_info):
        return {**token_info, "package_actions": actions}

    monkeypatch.setattr(auth, "get_package_manager_info", current_member)
    member = {"X-Actions": ",".join(sorted(actions))}
    accepted = await n.h.client.post("/v1/native-instances", json=n.body, headers=member)
    assert accepted.status_code == 202
    path = "/v1/native-instances/" + accepted.json()["id"]
    calls = list(n.calls)
    n.h.provider._afterglow_user_id = OTHER
    assert (await n.h.client.delete(path, headers={**member, "X-User": OTHER})).status_code == 404
    assert n.calls == calls
    n.h.provider._afterglow_user_id = USER
    assert (await n.h.client.delete(path, headers=member)).status_code == 202
    assert (await n.h.client.get(path, headers=member)).json()["status"] == "deleted"
    assert len(n.calls) == len(calls) + 1 and n.calls[-1][1] is True


async def test_native_self_cleanup_fresh_effect_callback_cannot_reuse_withdrawn_action(native_hub, monkeypatch):
    from palimpsest_hub import auth

    n = native_hub
    actions = {"packages:write", "packages:read", "packages:manage_own"}

    async def current_member(token_info):
        return {**token_info, "package_actions": actions}

    monkeypatch.setattr(auth, "get_package_manager_info", current_member)
    member = {"X-Actions": ",".join(sorted(actions))}
    accepted = await n.h.client.post("/v1/native-instances", json=n.body, headers=member)
    assert accepted.status_code == 202
    path = "/v1/native-instances/" + accepted.json()["id"]
    calls = list(n.calls)
    original = service.fresh_instance_controller
    withdrawn = False

    async def authority(actor, conn, row):
        if withdrawn:
            actor = {**actor, "package_actions": set(actor["package_actions"]) - {"packages:manage_own"}}
        return await original(actor, conn, row)

    def driver(profile, actor, conn, checkpoint, *args):
        nonlocal withdrawn
        withdrawn = True
        checkpoint.authorize_actor()
        raise AssertionError("withdrawn self cleanup cannot reach a provider effect")

    monkeypatch.setattr(service, "fresh_instance_controller", authority)
    monkeypatch.setattr(n.service, "_execute", driver)
    assert (await n.h.client.delete(path, headers=member)).status_code == 202
    assert (await n.h.client.get(path, headers=member)).json()["status"] == "held"
    assert n.calls == calls


@pytest.mark.parametrize("operation,observation", [("stop", "server"), ("delete", "server"), ("delete", "volume")])
async def test_creator_cleanup_rechecks_revocation_during_final_provider_observation(
    instance_hub, monkeypatch, operation, observation
):
    h = instance_hub
    member = {"X-Actions": "packages:write,packages:inventory,packages:read,packages:manage_own"}
    item = (await create_and_advance(h, json=request_body(), headers=member)).json()
    path = "/v1/instances/" + item["id"]
    original_authority = service.fresh_instance_controller
    original_observe = h.provider.get_server if observation == "server" else h.provider.get_volume
    state = {"reads": 0, "revoked": False, "denied": False}

    def observe(identity):
        result = original_observe(identity)
        state["reads"] += 1
        # One observation binds the durable intent; the next is in the final
        # effect section, after its first fresh authority check.
        if state["reads"] == 2:
            state["revoked"] = True
        return result

    async def authority(actor, conn, row):
        if state["revoked"]:
            state["denied"] = True
            actor = {**actor, "package_actions": set(actor["package_actions"]) - {"packages:manage_own"}}
        return await original_authority(actor, conn, row)

    monkeypatch.setattr(h.provider, "get_server" if observation == "server" else "get_volume", observe)
    monkeypatch.setattr(service, "fresh_instance_controller", authority)
    response = await (
        h.client.post(path + "/stop", headers=member) if operation == "stop" else h.client.delete(path, headers=member)
    )
    assert response.status_code == 403
    assert state["reads"] == 2 and state["revoked"] and state["denied"]
    assert h.provider.stops == h.provider.deletes == h.provider.volume_deletes == []
    assert item["server_id"] in h.provider.servers_by_id
    assert item["boot_volume_id"] in h.provider.volumes_by_id


async def _published_job_image(h, monkeypatch, *, platform, stored_arch):
    """A worker-published image whose single payload row was first labelled ``stored_arch``."""
    from palimpsest_local.image_manifest import manifest_from_result, normalize_manifest

    from palimpsest_hub.image_models import PalimpsestHubImageJob, PalimpsestHubPublicationPayload
    from palimpsest_hub.services import published_images

    payload = b"hsqs-platform-" + platform.encode()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    source = h.store.root / ("platform-output-" + platform.replace("/", "-"))
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    h.store.publish_verified(source, h.store.inspect_file(source, max_bytes=len(payload)))
    runtime = {"entrypoint": ["/bin/sh"], "cmd": [], "env": [], "working_dir": "/", "user": "0"}
    manifest = manifest_from_result(
        {"layers": [{"digest": digest, "size_bytes": len(payload)}], "runtime": runtime, "source": {}},
        os_family="fixture-os",
        os_version="1",
        source_kind="local",
    )
    if platform != manifest["platform"]:
        manifest = normalize_manifest(
            {**manifest, "platform": platform, "layers": [{"digest": digest, "size_bytes": len(payload)}]}
        )
    monkeypatch.setattr(published_images, "get_settings", lambda: SimpleNamespace())
    job_id, job_request = str(uuid4()), str(uuid4())
    async with h.factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=digest,
                size_bytes=len(payload),
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                arch=stored_arch,
                config_digest="sha256:" + "0" * 64,
                name="first-registrant",
                kind="image-payload",
                config_json={"instance_mode": "oci-layer"},
            )
        )
        session.add(
            PalimpsestHubPublicationPayload(
                project_id=PROJECT, created_by=USER, blob_digest=digest, size_bytes=len(payload)
            )
        )
        await session.commit()
        await published_images.register_image(
            session,
            h.store,
            {"project_id": PROJECT, "user_id": USER},
            job_request,
            "platform-image",
            manifest,
            image_id=job_id,
            require_staged=True,
        )
        session.add(
            PalimpsestHubImageJob(
                id=job_id,
                project_id=PROJECT,
                created_by=USER,
                request_id=job_request,
                name="platform-image",
                kind="dockerfile",
                request_digest=digest,
                request_json={},
                status="complete",
                output_digests=[digest],
                result_json={"layers": manifest["layers"], "source": {}, "runtime": runtime, "manifest": manifest},
            )
        )
        await session.commit()
    return job_id


async def test_amd64_image_is_not_rejected_for_a_payload_first_labelled_aarch64(instance_hub, monkeypatch):
    h = instance_hub
    job_id = await _published_job_image(h, monkeypatch, platform="linux/amd64", stored_arch="aarch64")
    request = request_body(command=[])
    request.update(layer_digests=[], image_job_id=job_id)
    response = await create_and_advance(h, json=request)
    assert response.status_code == 202, response.text
    assert response.json()["mode"] == "oci-chroot"


def _arm64_manifest_supported():
    from palimpsest_local.image_manifest import manifest_from_result, normalize_manifest

    layers = [{"digest": "sha256:" + "1" * 64, "size_bytes": 1}]
    runtime = {"entrypoint": [], "cmd": [], "env": [], "working_dir": "/", "user": "0"}
    manifest = manifest_from_result(
        {"layers": layers, "runtime": runtime, "source": {}}, os_family="x", os_version="1", source_kind="local"
    )
    try:
        normalize_manifest({**manifest, "platform": "linux/arm64", "layers": layers})
    except ValueError:
        return False
    return True


@pytest.mark.skipif(not _arm64_manifest_supported(), reason="manifest normalization does not accept linux/arm64")
async def test_amd64_instance_path_still_refuses_arm64_images(instance_hub, monkeypatch):
    h = instance_hub
    # Even if the shared payload row says x86_64, the sealed image's platform decides.
    job_id = await _published_job_image(h, monkeypatch, platform="linux/arm64", stored_arch="x86_64")
    request = request_body(command=[])
    request.update(layer_digests=[], image_job_id=job_id)
    response = await create_and_advance(h, json=request)
    assert response.status_code == 422
    assert response.json()["detail"] == "Instance inputs must be x86_64 SquashFS runtime layers"
    assert h.provider.creates == h.provider.volume_creates == []
