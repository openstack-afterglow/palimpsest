"""A publication quota refusal settles an image job instead of halting the shared worker."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from palimpsest_local.image_manifest import manifest_from_result
from palimpsest_local.image_source_policy import ImageSourcePolicy, ImageSourceRule
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub import build_worker
from palimpsest_hub.api import builds, hub, image_jobs
from palimpsest_hub.image_models import PalimpsestHubImageJob, PalimpsestHubPublishedImage
from palimpsest_hub.models import Base, PalimpsestHubLayer
from palimpsest_hub.services import image_jobs as service
from palimpsest_hub.services import image_quota
from palimpsest_hub.services import published_images as publications
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_store import LocalPathBlobStore
from palimpsest_hub.services.worker_leases import sql_mutex

KEY = "disposable-image-worker-key-01234567890123456789"
WORKER = {"X-Palimpsest-Build-Worker-Key": KEY}
HUMAN = {"X-Identity": "human"}
SOURCE_POLICY = ImageSourcePolicy((ImageSourceRule("docker.io", "library", "alpine"),))


def digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.fixture
async def images(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'images.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    settings = SimpleNamespace(
        palimpsest_hub_build_worker_key=SecretStr(KEY),
        palimpsest_hub_build_timeout_seconds=60,
        palimpsest_hub_max_blob_bytes=1024 * 1024,
        palimpsest_hub_registry_hosts=("docker.io",),
        palimpsest_hub_image_source_policy=SOURCE_POLICY.to_dict(),
        palimpsest_hub_max_context_bytes=16 * 1024 * 1024,
        palimpsest_hub_max_context_expanded_bytes=256 * 1024 * 1024,
        palimpsest_hub_max_context_members=10000,
    )
    monkeypatch.setattr(hub, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub, "get_blob_store", lambda: store)
    for module in (hub, builds, image_jobs, service, publications):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(image_jobs.router, prefix="/v1")

    async def identity(request: Request):
        if request.headers.get("X-Identity") != "human":
            raise HTTPException(status_code=403, detail="human native authority required")
        return {
            "project_id": "alpha",
            "user_id": "caller",
            "package_actions": {"packages:read", "packages:write", "packages:inventory"},
            "is_package_system_admin": False,
            "token": "disposable-native-token:caller",
        }

    app.dependency_overrides[hub._legacy_writer] = identity
    app.dependency_overrides[hub._legacy_inventory] = identity

    async def refresh(token_info):
        return token_info

    monkeypatch.setattr(service, "get_package_member_info", refresh)
    monkeypatch.setattr(
        service,
        "validate_token",
        lambda token, project: {
            "project_id": project,
            "user_id": "caller",
            "token": token,
            "package_actions": {"packages:read", "packages:write", "packages:inventory"},
        },
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://image.test") as client:
        yield SimpleNamespace(client=client, factory=factory, store=store)
    await engine.dispose()


def descriptor(*payloads):
    value = {
        "layers": [{"digest": digest(item), "size_bytes": len(item)} for item in payloads],
        "source": {
            "root_digest": digest(b"root"),
            "manifest_digest": digest(b"manifest"),
            "config_digest": digest(b"config"),
        },
        "runtime": {"entrypoint": ["/bin/sh"], "cmd": [], "env": [], "working_dir": "/", "user": "0"},
    }
    value["manifest"] = manifest_from_result(value, os_family="alpine", os_version="3", source_kind="registry")
    value["manifest"]["source"]["metadata"] = {
        "source_policy": SOURCE_POLICY.to_dict(),
        "source_policy_digest": SOURCE_POLICY.digest,
    }
    return value


async def staged_job(h, payload):
    """Real HTTP lifecycle up to the executor's final POST /complete."""
    queued = await h.client.post(
        "/v1/registry-imports",
        headers=HUMAN,
        json={
            "request_id": str(uuid4()),
            "name": "image",
            "reference": "alpine:3",
            "os_family": "alpine",
            "os_version": "3",
        },
    )
    assert queued.status_code == 202, queued.text
    claimed = await h.client.post(
        "/v1/image-worker/claim",
        headers={**WORKER, "X-Palimpsest-Worker-Channel": str(uuid4()), "X-Palimpsest-Claim-Sequence": "1"},
    )
    assert claimed.status_code == 202, claimed.text
    job = claimed.json()
    headers = {**WORKER, "X-Palimpsest-Build-Lease": job["lease"]}
    route = f"/v1/image-worker/jobs/{job['id']}"
    result = descriptor(payload)
    assert (await h.client.post(route + "/publication-intent", headers=headers, json=result)).status_code == 200
    staged = await h.client.put(
        route + "/outputs/" + digest(payload),
        content=payload,
        headers={**headers, "Content-Length": str(len(payload))},
    )
    assert staged.status_code == 200, staged.text
    return job, headers, route, result


async def set_policy(h, max_images, max_chain_bytes):
    limits = {"max_images": max_images, "max_chain_bytes": max_chain_bytes, "max_source_bytes": None}
    async with sql_mutex(h.factory, publications.IMAGE_MUTEX) as session:
        await image_quota.update_policy(session, "free", "private", limits, "admin")
        await session.commit()


class _Recorded:
    """Replays one real Hub HTTP response through the worker's own response reader."""

    def __init__(self, response):
        self.status = response.status_code
        self._body = response.content

    def getheader(self, name, default=None):
        return default

    def read1(self, size):
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk


class _ReplayTransport:
    def __init__(self, responses):
        self.responses = list(responses)

    @contextmanager
    def request(self, method, path, **_):
        yield _Recorded(self.responses.pop(0))


def assert_executor_settles(job, completed, status, result):
    """Contract keyed on by image_executor.upload's receipt() and BuildWorker._status()."""
    value = completed.json()
    # receipt(): exact id, known status, versioned publication bound to this result; "error" settles.
    assert value["id"] == job["id"] and value["status"] == "error"
    assert value["publication"]["version"] == 1
    assert value["publication"]["result_digest"] in (None, compute_config_digest(result))
    # BuildWorker._status() is executed for real on the recorded server answer; it raises WorkerHalted if uncertain.
    worker = build_worker.BuildWorker(SimpleNamespace(), _ReplayTransport([status]), journal=None, stop=None)
    settled = worker._status({"id": job["id"], "lease": job["lease"], "protocol": "image", "output": result})
    assert settled["status"] in build_worker.TERMINAL and settled["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits,code",
    [((0, None), "image_count_limit"), ((None, 1), "image_chain_size_limit")],
    ids=["count-403", "chain-413"],
)
async def test_quota_refusal_settles_job_as_terminal_error_without_publishing(images, limits, code):
    payload = b"hsqs-quota-refused-output"
    job, headers, route, result = await staged_job(images, payload)
    await set_policy(images, *limits)
    completed = await images.client.post(route + "/complete", headers=headers, json=result)
    assert completed.status_code == 200, completed.text
    assert completed.json()["error_code"] == code
    assert completed.json()["published_image_id"] is None and completed.json()["result"] is None
    status = await images.client.get(route, headers=headers)
    assert status.status_code == 200 and status.json()["status"] == "error"
    assert_executor_settles(job, completed, status, result)
    # The user-facing receipt explains why.
    public = await images.client.get("/v1/image-jobs/" + job["id"], headers=HUMAN)
    assert public.status_code == 200
    assert (public.json()["status"], public.json()["error_code"]) == ("error", code)
    async with images.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PalimpsestHubPublishedImage)) == 0
        row = await session.get(PalimpsestHubImageJob, job["id"])
        assert (row.status, row.error_code) == ("error", code) and row.completed_at is not None
        assert row.output_digests == []
    # Terminal: an exact retry neither publishes nor reopens the attempt.
    retried = await images.client.post(route + "/complete", headers=headers, json=result)
    assert retried.status_code == 409
    async with images.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PalimpsestHubPublishedImage)) == 0


@pytest.mark.asyncio
async def test_non_quota_registration_conflict_still_propagates_unchanged(images):
    payload = b"hsqs-arch-conflict-output"
    job, headers, route, result = await staged_job(images, payload)
    async with images.factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=digest(payload),
                size_bytes=len(payload),
                media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
                arch="x86_64",
                config_digest="sha256:" + "0" * 64,
                # Same bytes already registered as a different artifact kind: still a real conflict.
                name="other-kind",
                kind="cloud-image",
                config_json={},
            )
        )
        await session.commit()
    completed = await images.client.post(route + "/complete", headers=headers, json=result)
    assert (completed.status_code, completed.json()["detail"]) == (409, "payload registration conflicts")
    async with images.factory() as session:
        row = await session.get(PalimpsestHubImageJob, job["id"])
        assert (row.status, row.error_code) == ("building", None)


@pytest.mark.asyncio
async def test_other_structured_refusals_are_not_mistaken_for_quota(images, monkeypatch):
    payload = b"hsqs-other-structured-refusal"
    job, headers, route, result = await staged_job(images, payload)

    async def refused(*args, **kwargs):
        raise HTTPException(status_code=403, detail={"code": "image_policy_denied", "message": "other"})

    monkeypatch.setattr(publications, "register_image", refused)
    completed = await images.client.post(route + "/complete", headers=headers, json=result)
    assert completed.status_code == 403
    assert json.loads(completed.text)["detail"] == {"code": "image_policy_denied", "message": "other"}
    async with images.factory() as session:
        row = await session.get(PalimpsestHubImageJob, job["id"])
        assert (row.status, row.error_code) == ("building", None)


@pytest.mark.asyncio
async def test_source_size_refusal_code_also_settles(images, monkeypatch):
    # Worker outputs carry no closure today; the code set is shared, so this guards the settle path.
    job, headers, route, result = await staged_job(images, b"hsqs-source-code-settles")

    async def refused(*args, **kwargs):
        raise HTTPException(
            status_code=413,
            detail={"code": "image_source_size_limit", "visibility": "private", "limit_bytes": 1, "source_bytes": 2},
        )

    monkeypatch.setattr(publications, "register_image", refused)
    completed = await images.client.post(route + "/complete", headers=headers, json=result)
    assert completed.status_code == 200
    assert (completed.json()["status"], completed.json()["error_code"]) == ("error", "image_source_size_limit")


@pytest.mark.asyncio
async def test_worker_output_staging_still_requires_squashfs(images):
    queued = await images.client.post(
        "/v1/registry-imports",
        headers=HUMAN,
        json={
            "request_id": str(uuid4()),
            "name": "image",
            "reference": "alpine:3",
            "os_family": "alpine",
            "os_version": "3",
        },
    )
    assert queued.status_code == 202
    claimed = await images.client.post(
        "/v1/image-worker/claim",
        headers={**WORKER, "X-Palimpsest-Worker-Channel": str(uuid4()), "X-Palimpsest-Claim-Sequence": "1"},
    )
    job = claimed.json()
    opaque = b'{"not": "squashfs"}'
    response = await images.client.put(
        f"/v1/image-worker/jobs/{job['id']}/outputs/{digest(opaque)}",
        content=opaque,
        headers={**WORKER, "X-Palimpsest-Build-Lease": job["lease"], "Content-Length": str(len(opaque))},
    )
    assert (response.status_code, response.json()["detail"]) == (422, "output is not SquashFS")
