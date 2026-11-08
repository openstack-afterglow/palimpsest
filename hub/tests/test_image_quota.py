"""Published-image quota: unique-name counts, distinct chain bytes, admin policy and accounting."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from palimpsest_local.image_manifest import manifest_from_result, normalize_manifest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub import auth
from palimpsest_hub.api import hub, image_admin
from palimpsest_hub.api import published_images as published_images_api
from palimpsest_hub.image_models import (
    PalimpsestHubImageQuotaPolicy,
    PalimpsestHubPublicationPayload,
    PalimpsestHubPublishedImage,
)
from palimpsest_hub.models import Base, PalimpsestHubLayer
from palimpsest_hub.services import image_quota, published_images
from palimpsest_hub.services.hub_store import MEDIA_TYPE_LAYER_SQUASHFS, LocalPathBlobStore
from palimpsest_hub.services.worker_leases import sql_mutex

P1, P2 = "a" * 32, "b" * 32
U1, U2 = "c" * 32, "d" * 32
ADMIN = "e" * 32
GIB = 2**30
RUNTIME = {"entrypoint": ["/bin/sh"], "cmd": [], "env": [], "working_dir": "/", "user": "0"}


def actor(project=P1, user=U1):
    return {"project_id": project, "user_id": user}


def manifest(layers, *, tag="x", mode="oci-root", base=None, platform="linux/amd64"):
    value = manifest_from_result(
        {"layers": layers, "source": {}, "runtime": RUNTIME},
        os_family="fixture-os",
        os_version="1",
        source_kind="local",
        metadata={"tag": tag},
        mode=mode,
        base_image_digest=base,
    )
    if platform == value["platform"]:
        return value
    # Layer identities bind the platform, so re-derive them from bare descriptors.
    return normalize_manifest(
        {
            **value,
            "platform": platform,
            "layers": [{"digest": item["digest"], "size_bytes": item["size_bytes"]} for item in value["layers"]],
        }
    )


def arm64_supported():
    try:
        manifest([{"digest": "sha256:" + "1" * 64, "size_bytes": 1}], platform="linux/arm64")
    except ValueError:
        return False
    return True


@pytest.fixture
async def quota(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quota.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    monkeypatch.setattr(
        published_images, "get_settings", lambda: SimpleNamespace(palimpsest_hub_max_blob_bytes=1024 * 1024)
    )
    monkeypatch.setattr(hub, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub, "get_blob_store", lambda: store)
    payloads = {}

    def payload(label, size):
        body = (b"hsqs" + label.encode() + b"-" * size)[:size]
        assert len(body) == size
        path = tmp_path / f"payload-{label}"
        path.write_bytes(body)
        verified = store.inspect_file(path, max_bytes=size)
        store.publish_verified(path, verified)
        payloads[label] = {"digest": verified.blob_digest, "size_bytes": size}
        return payloads[label]

    async def stage(who, *labels):
        async with factory() as session:
            for label in labels:
                item = payloads[label]
                if await session.get(
                    PalimpsestHubPublicationPayload,
                    {"project_id": who["project_id"], "created_by": who["user_id"], "blob_digest": item["digest"]},
                ):
                    continue
                session.add(
                    PalimpsestHubPublicationPayload(
                        project_id=who["project_id"],
                        created_by=who["user_id"],
                        blob_digest=item["digest"],
                        size_bytes=item["size_bytes"],
                    )
                )
            await session.commit()

    async def publish(who, name, labels, *, visibility="private", tag=None, request_id=None, **kwargs):
        await stage(who, *dict.fromkeys(labels))
        return await published_images.publish_image(
            factory,
            store,
            who,
            request_id or str(uuid4()),
            name,
            manifest([payloads[label] for label in labels], tag=tag or name, **kwargs),
            visibility=visibility,
        )

    async def set_policy(tier, visibility, max_images, max_chain_bytes, max_source_bytes=None):
        limits = {"max_images": max_images, "max_chain_bytes": max_chain_bytes, "max_source_bytes": max_source_bytes}
        async with sql_mutex(factory, published_images.IMAGE_MUTEX) as session:
            await image_quota.update_policy(session, tier, visibility, limits, ADMIN)
            await session.commit()

    async def count_images():
        async with factory() as session:
            return await session.scalar(select(func.count()).select_from(PalimpsestHubPublishedImage))

    yield SimpleNamespace(
        factory=factory,
        store=store,
        payload=payload,
        payloads=payloads,
        stage=stage,
        publish=publish,
        set_policy=set_policy,
        count_images=count_images,
        engine=engine,
    )
    await engine.dispose()


@pytest.fixture
async def admin_client(quota):
    app = FastAPI()
    app.include_router(image_admin.router, prefix="/v1")
    app.include_router(published_images_api.router, prefix="/v1")
    state = {"admin": True}

    async def token(request: Request):
        return {"user_id": request.headers.get("X-User", U1), "project_id": P1, "is_system_admin": False, "token": "t"}

    def admin_or_real(request: Request):
        if not state["admin"]:
            # Exercise the real system-admin gate with a non-admin token.
            return auth.require_admin(token_info={"user_id": U1, "project_id": P1, "is_system_admin": False})
        return {"user_id": ADMIN, "project_id": P1, "package_actions": {"system:manage"}}

    async def inventory(request: Request):
        return {
            "user_id": request.headers.get("X-User", U1),
            "project_id": request.headers.get("X-Project", P1),
            "package_actions": {"packages:inventory"},
        }

    app.dependency_overrides[auth.require_token] = token
    app.dependency_overrides[auth.require_admin] = admin_or_real
    app.dependency_overrides[published_images_api.image_graph_reader] = inventory
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://quota.test") as client:
        yield SimpleNamespace(client=client, state=state)


def count_error(visibility, scope, limit, used):
    return {"code": "image_count_limit", "visibility": visibility, "scope": scope, "limit": limit, "used": used}


@pytest.mark.asyncio
async def test_unique_names_per_user_scope_and_replay_before_quota(quota):
    quota.payload("a", 64)
    quota.payload("b", 64)
    rid = str(uuid4())
    first = await quota.publish(actor(), "one", ["a"], request_id=rid)
    await quota.publish(actor(P2, U1), "two", ["b"])  # private scope is the user across projects
    with pytest.raises(HTTPException) as denied:
        await quota.publish(actor(), "three", ["a"])
    assert denied.value.status_code == 403
    assert denied.value.detail == count_error("private", "user", 2, 2)
    assert await quota.count_images() == 2
    # A new digest under an existing name is not a new image.
    await quota.publish(actor(), "two", ["a", "b"], tag="two-v2")
    # Exact request_id replay returns the existing image even though the scope is full.
    replay = await quota.publish(actor(), "one", ["a"], request_id=rid)
    assert replay["id"] == first["id"]
    # Another user's private scope is independent.
    await quota.publish(actor(P1, U2), "three", ["a"])
    assert await quota.count_images() == 4


@pytest.mark.asyncio
async def test_internal_scope_is_project_and_public_count_is_unlimited(quota):
    quota.payload("a", 64)
    await quota.set_policy("free", "internal", 1, GIB)
    await quota.publish(actor(P1, U1), "shared", ["a"], visibility="internal")
    with pytest.raises(HTTPException) as denied:
        await quota.publish(actor(P1, U2), "other", ["a"], visibility="internal")
    assert denied.value.status_code == 403
    assert denied.value.detail == count_error("internal", "project", 1, 1)
    await quota.publish(actor(P1, U2), "shared", ["a"], visibility="internal", tag="second-digest")
    await quota.publish(actor(P2, U2), "other", ["a"], visibility="internal")
    for index in range(4):  # seed free/public max_images is NULL
        await quota.publish(actor(), f"public-{index}", ["a"], visibility="public")
    assert await quota.count_images() == 7


@pytest.mark.asyncio
async def test_chain_bytes_count_distinct_digests_with_exact_boundary(quota):
    quota.payload("a", 100)
    quota.payload("b", 50)
    quota.payload("c", 51)
    await quota.set_policy("free", "private", None, 150)
    # a + b + a: naive sum 250, distinct 150 == limit.
    await quota.publish(actor(), "repeat", ["a", "b", "a"])
    with pytest.raises(HTTPException) as denied:
        await quota.publish(actor(), "over", ["a", "c"])
    assert denied.value.status_code == 413
    assert denied.value.detail == {
        "code": "image_chain_size_limit",
        "visibility": "private",
        "limit_bytes": 150,
        "chain_bytes": 151,
    }
    assert await quota.count_images() == 1


@pytest.mark.asyncio
async def test_application_base_image_is_outside_chain_cap(quota):
    quota.payload("a", 100)
    await quota.set_policy("free", "private", None, 100)
    base = "sha256:" + hashlib.sha256(b"original-cloud-image").hexdigest()
    await quota.publish(actor(), "app", ["a"], mode="application", base=base)
    assert await quota.count_images() == 1


@pytest.mark.asyncio
async def test_paid_tier_raises_caps_and_admin_changes_apply_immediately(quota, admin_client):
    quota.payload("a", 64)
    quota.payload("b", 64)
    await quota.publish(actor(), "one", ["a"])
    await quota.publish(actor(), "two", ["a"])
    with pytest.raises(HTTPException):
        await quota.publish(actor(), "three", ["a"])
    response = await admin_client.client.put(f"/v1/admin/image-quota/tiers/user/{U1}", json={"tier": "paid"})
    assert response.status_code == 200 and response.json()["tier"] == "paid" and response.json()["assigned"]
    await quota.publish(actor(), "three", ["a"])
    # Live policy change, no restart: paid private now allows exactly 3 names.
    response = await admin_client.client.put(
        "/v1/admin/image-quota/policies/paid/private",
        json={"max_images": 3, "max_chain_bytes": 64, "max_source_bytes": None},
    )
    assert response.status_code == 200
    assert response.json()["max_images"] == 3 and response.json()["updated_by"] == ADMIN
    with pytest.raises(HTTPException) as denied:
        await quota.publish(actor(), "four", ["a"])
    assert denied.value.detail == count_error("private", "user", 3, 3)
    with pytest.raises(HTTPException) as too_big:
        await quota.publish(actor(), "three", ["a", "b"])
    assert too_big.value.status_code == 413
    response = await admin_client.client.put(
        "/v1/admin/image-quota/policies/paid/private",
        json={"max_images": None, "max_chain_bytes": None, "max_source_bytes": None},
    )
    assert response.status_code == 200
    await quota.publish(actor(), "four", ["a", "b"])
    # Project tier does not leak into private (user) scopes.
    response = await admin_client.client.get(f"/v1/admin/image-quota/tiers/project/{P1}")
    assert response.json() == {
        "subject_kind": "project",
        "subject_id": P1,
        "tier": "free",
        "assigned": False,
        "updated_by": None,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_admin_routes_reject_non_admin_and_invalid_input(quota, admin_client):
    admin_client.state["admin"] = False
    for method, path, body in (
        ("GET", "/v1/admin/image-quota/policies", None),
        (
            "PUT",
            "/v1/admin/image-quota/policies/free/private",
            {"max_images": 99, "max_chain_bytes": None, "max_source_bytes": None},
        ),
        ("GET", f"/v1/admin/image-quota/tiers/user/{U1}", None),
        ("PUT", f"/v1/admin/image-quota/tiers/user/{U1}", {"tier": "paid"}),
        ("GET", f"/v1/admin/image-quota/usage?subject_kind=user&subject_id={U1}", None),
        ("GET", "/v1/admin/image-quota/physical", None),
    ):
        response = await admin_client.client.request(method, path, json=body)
        assert response.status_code == 403, path
    async with quota.factory() as session:
        assert (await session.scalars(select(PalimpsestHubImageQuotaPolicy))).all() == []
    admin_client.state["admin"] = True
    for path, body in (
        (
            "/v1/admin/image-quota/policies/free/private",
            {"max_images": -1, "max_chain_bytes": None, "max_source_bytes": None},
        ),
        (
            "/v1/admin/image-quota/policies/free/private",
            {"max_images": "5", "max_chain_bytes": None, "max_source_bytes": None},
        ),
        ("/v1/admin/image-quota/policies/free/private", {"max_images": 5, "max_chain_bytes": None}),
        (
            "/v1/admin/image-quota/policies/gold/private",
            {"max_images": 5, "max_chain_bytes": None, "max_source_bytes": None},
        ),
        (
            "/v1/admin/image-quota/policies/free/secret",
            {"max_images": 5, "max_chain_bytes": None, "max_source_bytes": None},
        ),
        ("/v1/admin/image-quota/tiers/user/" + U1, {"tier": "gold"}),
        ("/v1/admin/image-quota/tiers/group/" + U1, {"tier": "paid"}),
        ("/v1/admin/image-quota/tiers/user/bad%20id", {"tier": "paid"}),
    ):
        response = await admin_client.client.put(path, json=body)
        assert response.status_code == 422, (path, body)
    response = await admin_client.client.get("/v1/admin/image-quota/policies")
    assert response.status_code == 200
    items = {(item["tier"], item["visibility"]): item for item in response.json()["items"]}
    assert items[("free", "private")] | {"updated_at": None} == {
        "tier": "free",
        "visibility": "private",
        "scope": "user",
        "max_images": 2,
        "max_chain_bytes": GIB,
        "max_source_bytes": GIB,
        "source": "seed",
        "updated_by": None,
        "updated_at": None,
    }
    assert items[("paid", "public")]["max_images"] is None
    assert items[("paid", "public")]["max_chain_bytes"] == 100 * GIB


@pytest.mark.asyncio
async def test_seed_rows_are_inserted_once_and_never_overwrite_admin_values(quota):
    quota.payload("a", 64)
    await quota.set_policy("free", "public", 7, 1234)
    await quota.publish(actor(), "pub", ["a"], visibility="public")
    async with quota.factory() as session:
        rows = {
            (row.tier, row.visibility): row for row in (await session.scalars(select(PalimpsestHubImageQuotaPolicy)))
        }
    assert len(rows) == 6
    assert (rows[("free", "public")].max_images, rows[("free", "public")].max_chain_bytes) == (7, 1234)
    assert rows[("free", "private")].updated_by is None and rows[("free", "private")].max_images == 2


@pytest.mark.asyncio
async def test_downgrade_preserves_images_reports_over_limit_and_rejects_new_names(quota, admin_client):
    quota.payload("a", 100)
    quota.payload("b", 50)
    kept = [await quota.publish(actor(), name, ["a", "b"]) for name in ("one", "two")]
    response = await admin_client.client.put(
        "/v1/admin/image-quota/policies/free/private",
        json={"max_images": 1, "max_chain_bytes": 120, "max_source_bytes": None},
    )
    assert response.status_code == 200
    for image in kept:
        assert (await published_images.resolve_image(quota.factory, image["id"], actor()))["id"] == image["id"]
    with pytest.raises(HTTPException) as denied:
        await quota.publish(actor(), "three", ["a"])
    assert denied.value.status_code == 403
    assert denied.value.detail == count_error("private", "user", 1, 2)
    usage = await admin_client.client.get(f"/v1/admin/image-quota/usage?subject_kind=user&subject_id={U1}")
    assert usage.status_code == 200
    private = next(item for item in usage.json()["scopes"] if item["visibility"] == "private")
    assert private | {} == {
        "visibility": "private",
        "scope": "user",
        "subject_id": U1,
        "tier": "free",
        "max_images": 1,
        "max_chain_bytes": 120,
        "max_source_bytes": None,
        "unique_names": 2,
        "images": 2,
        "largest_chain_bytes": 150,
        "images_over_chain_limit": 2,
        "largest_source_bytes": 0,
        "images_over_source_limit": 0,
        "can_publish_new_name": False,
        "over_limit_images": True,
        "over_limit_chain_bytes": True,
        "over_limit_source_bytes": False,
        "over_limit": True,
    }
    me = await admin_client.client.get("/v1/published-images/quota")
    assert me.status_code == 200
    assert me.json()["user_tier"] == "free" and me.json()["project_tier"] == "free"
    assert [item["visibility"] for item in me.json()["scopes"]] == ["private", "public", "internal"]
    assert me.json()["scopes"][0]["over_limit"] is True
    other = await admin_client.client.get("/v1/published-images/quota", headers={"X-User": U2})
    assert other.json()["scopes"][0]["unique_names"] == 0
    assert await quota.count_images() == 2


@pytest.mark.asyncio
async def test_extension_publishes_are_quota_checked_including_parent_layers(quota):
    quota.payload("a", 100)
    quota.payload("b", 50)
    base = await quota.publish(actor(), "base", ["a"])
    await quota.stage(actor(), "b")
    await quota.set_policy("free", "private", None, 149)
    with pytest.raises(HTTPException) as too_big:
        await published_images.extend_image(
            quota.factory, quota.store, actor(), str(uuid4()), "ext", base["id"], [quota.payloads["b"]]
        )
    assert too_big.value.status_code == 413
    assert too_big.value.detail["chain_bytes"] == 150
    await quota.set_policy("free", "private", 1, None)
    with pytest.raises(HTTPException) as denied:
        await published_images.extend_image(
            quota.factory, quota.store, actor(), str(uuid4()), "ext", base["id"], [quota.payloads["b"]]
        )
    assert denied.value.detail == count_error("private", "user", 1, 1)
    extended = await published_images.extend_image(
        quota.factory, quota.store, actor(), str(uuid4()), "base", base["id"], [quota.payloads["b"]]
    )
    assert extended["name"] == "base" and await quota.count_images() == 2


@pytest.mark.asyncio
async def test_worker_registration_path_uses_the_same_hook(quota):
    quota.payload("a", 64)
    await quota.set_policy("free", "internal", 0, None)
    async with sql_mutex(quota.factory, "build-api:image:single-flight") as session:
        with pytest.raises(HTTPException) as denied:
            await published_images.register_image(
                session,
                quota.store,
                actor(),
                str(uuid4()),
                "job-output",
                manifest([quota.payloads["a"]]),
                visibility="internal",
                image_id=str(uuid4()),
            )
        await session.rollback()
    assert denied.value.detail == count_error("internal", "project", 0, 0)


@pytest.mark.asyncio
async def test_concurrent_publishes_at_the_limit_never_exceed_it(quota, monkeypatch):
    quota.payload("a", 64)
    await quota.publish(actor(), "first", ["a"])
    original = image_quota.enforce

    async def widened(*args, **kwargs):
        await original(*args, **kwargs)
        await asyncio.sleep(0.1)  # Without the global mutex both racers would pass the check here.

    monkeypatch.setattr(image_quota, "enforce", widened)
    results = await asyncio.gather(
        *(quota.publish(actor(), f"racer-{index}", ["a"]) for index in range(3)), return_exceptions=True
    )
    accepted = [item for item in results if isinstance(item, dict)]
    rejected = [item for item in results if isinstance(item, HTTPException)]
    assert len(accepted) == 1 and len(rejected) == 2, results
    assert all(item.detail == count_error("private", "user", 2, 2) for item in rejected)
    assert await quota.count_images() == 2


@pytest.mark.asyncio
async def test_physical_report_counts_namespaces_and_never_guesses_sizes(quota, admin_client):
    quota.payload("a", 100)
    quota.payload("b", 50)
    known = "sha256:" + hashlib.sha256(b"known-cloud-image").hexdigest()
    unknown = "sha256:" + hashlib.sha256(b"unknown-cloud-image").hexdigest()
    async with quota.factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=known,
                size_bytes=4096,
                media_type="application/octet-stream",
                config_digest="sha256:" + "0" * 64,
                name="cloud",
                kind="cloud-image",
                config_json={},
            )
        )
        await session.commit()
    await quota.publish(actor(), "one", ["a", "b"])
    await quota.publish(actor(), "two", ["a"])
    await quota.publish(actor(), "one", ["a", "b", "a"], tag="v2")
    await quota.publish(actor(), "app", ["b"], visibility="internal", mode="application", base=known)
    await quota.publish(actor(P2, U2), "app", ["b"], visibility="public", mode="application", base=unknown)
    await quota.publish(actor(P2, U2), "app2", ["b"], visibility="public", mode="application", base=known)
    response = await admin_client.client.get("/v1/admin/image-quota/physical")
    assert response.status_code == 200
    body = response.json()
    assert body["scopes"]["private"] == {
        "images": 3,
        "unique_names": 2,
        "logical_bytes": 150 + 100 + 150,
        "logical_source_bytes": 0,
    }
    assert body["scopes"]["internal"] == {
        "images": 1,
        "unique_names": 1,
        "logical_bytes": 50,
        "logical_source_bytes": 0,
    }
    assert body["scopes"]["public"]["unique_names"] == 2
    # Local storage: one Hub-wide namespace, each digest counted once.
    assert body["namespaces"] == [
        {
            "kind": "local",
            "share_id": None,
            "scope_key": None,
            "distinct_blobs": 2,
            "physical_bytes": 150,
            "payload_bytes": 150,
            "source_bytes": 0,
        }
    ]
    assert body["hub_cas"] == {"distinct_blobs": 2, "physical_bytes": 150}
    assert body["total"] == {"images": 6, "logical_bytes": 550, "logical_source_bytes": 0}
    bases = {item["digest"]: item for item in body["base_images"]}
    assert bases[known]["referencing_images"] == 2 and bases[known]["size_bytes"] == 4096
    assert bases[known]["by_visibility"] == {"private": 0, "internal": 1, "public": 1}
    assert bases[unknown]["size_bytes"] is None and bases[unknown]["size_known"] is False
    assert body["store"]["kind"] == "local_path"
    assert body["store"]["total_bytes"] >= body["store"]["free_bytes"] >= 0


@pytest.mark.asyncio
async def test_physical_report_counts_each_manila_namespace_separately(quota, admin_client, monkeypatch):
    quota.payload("a", 100)
    quota.payload("b", 50)
    await quota.publish(actor(P1, U1), "one", ["a", "b"])
    await quota.publish(actor(P1, U2), "two", ["a"])
    await quota.publish(actor(P1, U1), "three", ["a"], visibility="public")
    share = str(uuid4())
    scopes = {"private": "user-", "internal": "project-", "public": "public"}
    async with quota.factory() as session:
        for row in (await session.scalars(select(PalimpsestHubPublishedImage))).all():
            # Bindings as scoped_destination records them on a Manila deployment.
            key = (
                "public"
                if row.visibility == "public"
                else scopes[row.visibility] + hashlib.sha256(row.created_by.encode()).hexdigest()[:32]
            )
            row.storage_binding_json = {"kind": "manila-namespace-v1", "share_id": share, "scope_key": key}
        await session.commit()
    monkeypatch.setattr(
        published_images,
        "get_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_max_blob_bytes=1024 * 1024,
            palimpsest_hub_publication_manila_share_id=share,
            palimpsest_hub_publication_manila_export="ceph:/volumes/x",
            palimpsest_hub_publication_manila_root="/mnt/share",
        ),
    )
    body = (await admin_client.client.get("/v1/admin/image-quota/physical")).json()
    by_scope = {item["scope_key"]: item for item in body["namespaces"]}
    assert {item["kind"] for item in body["namespaces"]} == {"manila-namespace-v1"}
    user1 = "user-" + hashlib.sha256(U1.encode()).hexdigest()[:32]
    user2 = "user-" + hashlib.sha256(U2.encode()).hexdigest()[:32]
    # Digest "a" is physically present in three namespaces and counted in each.
    assert by_scope[user1]["physical_bytes"] == 150 and by_scope[user1]["distinct_blobs"] == 2
    assert by_scope[user2]["physical_bytes"] == 100
    assert by_scope["public"]["physical_bytes"] == 100
    assert body["hub_cas"] == {"distinct_blobs": 2, "physical_bytes": 150}
    # Manila capacity is not measured and is never labelled as a local path.
    assert body["store"] == {"kind": "manila", "free_bytes": None, "total_bytes": None}


async def physical_arch(quota, label):
    async with quota.factory() as session:
        return await session.scalar(
            select(PalimpsestHubLayer.arch).where(PalimpsestHubLayer.blob_digest == quota.payloads[label]["digest"])
        )


ARM64_REASON = "palimpsest_local normalize_manifest does not yet accept linux/arm64 (client slice widening pending)"


@pytest.mark.asyncio
async def test_amd64_payload_rows_are_x86_64_and_accept_a_differently_labelled_existing_row(quota):
    quota.payload("a", 64)
    quota.payload("b", 64)
    await quota.publish(actor(), "amd", ["a"])
    assert await physical_arch(quota, "a") == "x86_64"
    async with quota.factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=quota.payloads["b"]["digest"],
                size_bytes=64,
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                arch="aarch64",
                config_digest="sha256:" + "0" * 64,
                name="arm-payload",
                kind="image-payload",
                config_json={},
            )
        )
        await session.commit()
    # The other platform's label on identical bytes neither blocks nor reveals anything.
    published = await quota.publish(actor(), "amd", ["b"], tag="v2")
    assert published["manifest"]["platform"] == "linux/amd64"
    assert await physical_arch(quota, "b") == "aarch64"


@pytest.mark.asyncio
@pytest.mark.skipif(not arm64_supported(), reason=ARM64_REASON)
async def test_cross_tenant_identical_bytes_publish_for_both_platforms_without_oracle(quota):
    quota.payload("a", 64)
    # A private arm64 publication first records the shared bytes as aarch64...
    await quota.publish(actor(P1, U1), "arm", ["a"], platform="linux/arm64")
    assert await physical_arch(quota, "a") == "aarch64"
    # ...another tenant publishing the same bytes for amd64 must not learn of it or be blocked.
    other = await quota.publish(actor(P2, U2), "amd", ["a"])
    assert other["manifest"]["platform"] == "linux/amd64"
    # And the reverse direction for the first tenant.
    again = await quota.publish(actor(P1, U1), "amd", ["a"])
    assert again["manifest"]["platform"] == "linux/amd64"
    assert await quota.count_images() == 3


def _reference_usage(rows, visibility, subject):
    """The pre-SQL implementation: walk every stored manifest."""
    owner = "project_id" if visibility == "internal" else "created_by"
    scoped = [row for row in rows if row.visibility == visibility and getattr(row, owner) == subject]
    chains = [image_quota.chain_bytes(row.manifest_json) for row in scoped]
    return {
        "images": len(scoped),
        "unique_names": len({row.name for row in scoped}),
        "largest_chain_bytes": max(chains, default=0),
    }


@pytest.mark.asyncio
async def test_sql_accounting_equals_manifest_walk_on_multi_version_fixture(quota):
    for label, size in (("a", 100), ("b", 50), ("c", 7)):
        quota.payload(label, size)
    await quota.set_policy("free", "private", None, None)
    await quota.set_policy("free", "internal", None, None)
    for who, name, labels, visibility in (
        (actor(P1, U1), "web", ["a"], "private"),
        (actor(P1, U1), "web", ["a", "b"], "private"),
        (actor(P1, U1), "web", ["a", "b", "a", "c"], "private"),
        (actor(P2, U1), "db", ["c", "c"], "private"),
        (actor(P1, U2), "web", ["b"], "private"),
        (actor(P1, U1), "shared", ["a", "c"], "internal"),
        (actor(P1, U2), "shared", ["b", "a", "b"], "internal"),
        (actor(P1, U2), "tool", ["c"], "internal"),
        (actor(P2, U2), "pub", ["a", "b", "c"], "public"),
    ):
        await quota.publish(who, name, labels, visibility=visibility, tag=f"{name}-{len(labels)}-{who['project_id']}")
    async with quota.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubPublishedImage))).all()
        policies = await image_quota.load_policies(session)
        for visibility, subject in (("private", U1), ("private", U2), ("internal", P1), ("public", U2)):
            measured = await image_quota.scope_usage(session, visibility, subject, "free", policies)
            assert {key: measured[key] for key in ("images", "unique_names", "largest_chain_bytes")} == (
                _reference_usage(rows, visibility, subject)
            ), (visibility, subject)
        report = await image_quota.physical_report(session, quota.store, manila=False)
    for visibility in image_quota.VISIBILITIES:
        scoped = [row for row in rows if row.visibility == visibility]
        owner = "project_id" if visibility == "internal" else "created_by"
        assert report["scopes"][visibility]["images"] == len(scoped)
        assert report["scopes"][visibility]["unique_names"] == len({(getattr(row, owner), row.name) for row in scoped})
        assert report["scopes"][visibility]["logical_bytes"] == sum(
            image_quota.chain_bytes(row.manifest_json) for row in scoped
        )
    distinct = {item["digest"]: item["size_bytes"] for row in rows for item in row.manifest_json["layers"]}
    assert report["hub_cas"] == {"distinct_blobs": len(distinct), "physical_bytes": sum(distinct.values())}


@pytest.mark.asyncio
async def test_usage_reports_and_enforcement_never_load_stored_manifests(quota):
    import re
    import tracemalloc

    from sqlalchemy import event, insert

    from palimpsest_hub.image_models import PalimpsestHubImageMembership

    digest = "sha256:" + hashlib.sha256(b"bulk").hexdigest()
    pad = "x" * (256 * 1024)
    count = 200  # ~50 MiB of stored manifests in total
    async with quota.factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=digest,
                size_bytes=10,
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                arch="x86_64",
                config_digest="sha256:" + "0" * 64,
                name="bulk",
                kind="image-payload",
                config_json={},
            )
        )
        images = [
            {
                "id": str(uuid4()),
                "project_id": P1,
                "created_by": U1,
                "request_id": str(uuid4()),
                "name": f"n{index % 7}",
                "visibility": "private",
                "manifest_digest": "sha256:" + "1" * 64,
                "manifest_json": {"mode": "oci-root", "layers": [{"digest": digest, "size_bytes": 10}], "pad": pad},
                "leaf_id": "sha256:" + "2" * 64,
                "storage_binding_json": {"kind": "local", "root": "blobs/sha256"},
            }
            for index in range(count)
        ]
        await session.execute(insert(PalimpsestHubPublishedImage), images)
        await session.execute(
            insert(PalimpsestHubImageMembership),
            [
                {
                    "image_id": item["id"],
                    "ordinal": 0,
                    "project_id": P1,
                    "node_id": item["leaf_id"],
                    "blob_digest": digest,
                    "reuse_projects_json": [],
                }
                for item in images
            ],
        )
        await session.commit()
    del images
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(quota.engine.sync_engine, "before_cursor_execute", record)
    tracemalloc.start()
    try:
        async with sql_mutex(quota.factory, published_images.IMAGE_MUTEX) as session:
            await image_quota.enforce(
                session, actor(), "n3", {"layers": [{"digest": digest, "size_bytes": 10}]}, "private"
            )
            await session.rollback()
        async with quota.factory() as session:
            usage = await image_quota.subject_usage(session, "user", U1)
            report = await image_quota.physical_report(session, quota.store, manila=False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        event.remove(quota.engine.sync_engine, "before_cursor_execute", record)
    private = usage["scopes"][0]
    assert (private["images"], private["unique_names"], private["largest_chain_bytes"]) == (count, 7, 10)
    assert report["scopes"]["private"]["logical_bytes"] == 10 * count
    assert peak < 8 * 1024 * 1024, peak
    # Only JSON-path extraction of single fields may touch the manifest column.
    extraction = re.compile(r"json_extract\(\s*palimpsest_hub_published_images\.manifest_json\s*,", re.I)
    assert not [item for item in statements if "manifest_json" in extraction.sub("", item)]


@pytest.mark.asyncio
async def test_corrupt_publication_rows_give_a_clear_409_not_a_500(quota, admin_client):
    async with quota.factory() as session:
        session.add(
            PalimpsestHubPublishedImage(
                id=str(uuid4()),
                project_id=P1,
                created_by=U1,
                request_id=str(uuid4()),
                name="broken",
                visibility="private",
                manifest_digest="sha256:" + "1" * 64,
                manifest_json="not-a-manifest",
                leaf_id="sha256:" + "2" * 64,
                storage_binding_json={"kind": "local"},
            )
        )
        await session.commit()
    usage = await admin_client.client.get(f"/v1/admin/image-quota/usage?subject_kind=user&subject_id={U1}")
    assert (usage.status_code, usage.json()["detail"]) == (409, "published image membership is incomplete")
    physical = await admin_client.client.get("/v1/admin/image-quota/physical")
    assert (physical.status_code, physical.json()["detail"]) == (409, "published image membership is incomplete")


@pytest.mark.asyncio
async def test_admin_body_errors_use_quota_wording(admin_client):
    response = await admin_client.client.put(
        "/v1/admin/image-quota/policies/free/private", json={"max_images": 1, "max_chain_bytes": 2}
    )
    assert (response.status_code, response.json()["detail"]) == (422, "invalid image quota request")
    response = await admin_client.client.put(f"/v1/admin/image-quota/tiers/user/{U1}", content=b"{")
    assert (response.status_code, response.json()["detail"]) == (422, "invalid image quota request")


@pytest.mark.asyncio
async def test_quota_routes_are_no_store_through_the_real_app(quota):
    from palimpsest_hub import main

    async def inventory():
        return {"user_id": U1, "project_id": P1, "package_actions": {"packages:inventory"}}

    def admin():
        return {"user_id": ADMIN, "project_id": P1, "package_actions": {"system:manage"}}

    main.app.dependency_overrides[published_images_api.image_graph_reader] = inventory
    main.app.dependency_overrides[auth.require_admin] = admin
    try:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://quota.test") as client:
            for path in (
                "/v1/published-images/quota",
                "/v1/admin/image-quota/policies",
                f"/v1/admin/image-quota/usage?subject_kind=user&subject_id={U1}",
                "/v1/admin/image-quota/tiers/user/bad%20id",
            ):
                response = await client.get(path)
                assert response.status_code in (200, 422), (path, response.status_code)
                assert response.headers["cache-control"] == "no-store", path
            # The pre-0.3.14 path is gone (clean cutover); "quota" is not captured by the image-id route.
            assert (await client.get("/v1/image-quota/me")).status_code == 404
    finally:
        main.app.dependency_overrides.pop(published_images_api.image_graph_reader, None)
        main.app.dependency_overrides.pop(auth.require_admin, None)


@pytest.mark.asyncio
async def test_tier_ids_are_byte_exact_and_a_case_collision_is_a_409(tmp_path, monkeypatch):
    from sqlalchemy.dialects.mysql import VARCHAR

    async def engine_with(collation):
        with monkeypatch.context() as schema:
            if collation:
                # Model a case-insensitive SQL collation for the tier key, as legacy MySQL may have.
                schema.setattr(
                    Base.metadata.tables["palimpsest_hub_image_quota_tiers"].c.subject_id,
                    "type",
                    VARCHAR(128, collation=collation),
                )
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'tiers-{collation}.sqlite'}")
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    async def assign(factory, subject, tier):
        async with sql_mutex(factory, published_images.IMAGE_MUTEX) as session:
            value = await image_quota.assign_tier(session, "user", subject, tier, ADMIN)
            await session.commit()
            return value

    engine, factory = await engine_with(None)
    try:
        await assign(factory, "AbC", "paid")
        await assign(factory, "abc", "free")
        async with factory() as session:
            assert (await image_quota.subject_tier(session, "user", "AbC"))["tier"] == "paid"
            assert (await image_quota.subject_tier(session, "user", "abc"))["tier"] == "free"
    finally:
        await engine.dispose()
    engine, factory = await engine_with("NOCASE")
    try:
        await assign(factory, "AbC", "paid")
        assert (await assign(factory, "AbC", "free"))["tier"] == "free"  # idempotent update of the exact key
        with pytest.raises(HTTPException) as collision:
            await assign(factory, "abc", "paid")
        assert collision.value.status_code == 409
        async with factory() as session:
            # Exact lookup never returns the differently-cased row.
            assert (await image_quota.subject_tier(session, "user", "abc"))["assigned"] is False
            assert (await image_quota.subject_tier(session, "user", "AbC"))["tier"] == "free"
    finally:
        await engine.dispose()
