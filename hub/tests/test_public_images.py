"""Anonymous public image reads, the anonymous rate limit and legacy visibility regressions.

Identity is replaced only at the Keystone boundary (token parsing and current
membership); credential selection, image-key authentication, visibility and
projection run for real over HTTP against SQLite and a real CAS store.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_image_jobs import publication_manifest

from palimpsest_hub import auth
from palimpsest_hub.api import hub, image_admin, image_jobs
from palimpsest_hub.api import image_keys as image_keys_api
from palimpsest_hub.api import published_images as published_images_api
from palimpsest_hub.models import Base, PalimpsestHubLayer
from palimpsest_hub.services import image_jobs as image_jobs_service
from palimpsest_hub.services import image_keys as image_keys_service
from palimpsest_hub.services import public_images
from palimpsest_hub.services import published_images as published_images_service
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_store import MEDIA_TYPE_LAYER_SQUASHFS, LocalPathBlobStore

PROJECT_A = "project-alpha"
PROJECT_B = "project-beta"
ALICE = "user-alice"
AMY = "user-amy"
BOB = "user-bob"
READ_ACTIONS = frozenset({"packages:inventory", "packages:read", "cache:read", "keys:list", "keys:revoke_own"})
ROLE_ACTIONS = {
    "reader": READ_ACTIONS,
    "member": READ_ACTIONS | {"packages:write", "cache:write", "packages:manage_own"},
}


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def token(user: str, project: str) -> str:
    return f"synthetic-subject:{user}:{project}"


def human(user: str, project: str) -> dict[str, str]:
    return {"X-Auth-Token": token(user, project)}


@pytest.fixture
async def hub_images(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'images.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    settings = SimpleNamespace(
        palimpsest_hub_build_worker_key=SecretStr(""),
        palimpsest_hub_max_blob_bytes=1024 * 1024,
        palimpsest_hub_max_bundle_expanded_bytes=1024 * 1024,
        palimpsest_hub_anonymous_rate_limit_enabled=True,
        palimpsest_hub_anonymous_rate_limit_requests=1000,
        palimpsest_hub_anonymous_rate_limit_window_seconds=60,
        palimpsest_hub_anonymous_rate_limit_max_clients=1000,
        palimpsest_hub_image_key_max_active_per_user=50,
    )
    members = {(ALICE, PROJECT_A): "member", (AMY, PROJECT_A): "member", (BOB, PROJECT_B): "member"}
    revoked_tokens: set[str] = set()

    def parse(value: str) -> dict:
        parts = (value or "").split(":")
        if len(parts) != 3 or parts[0] != "synthetic-subject" or value in revoked_tokens:
            raise HTTPException(status_code=401, detail="Invalid or expired Keystone token")
        return {
            "token": value,
            "user_id": parts[1],
            "project_id": parts[2],
            "roles": [members.get((parts[1], parts[2]), "none")],
            "auth_methods": ("password",),
            "is_system_admin": False,
            "is_delegated": False,
        }

    def owner(user_id: str, project_id: str, **_):
        role = members.get((user_id, project_id))
        if role is None:
            raise HTTPException(status_code=403, detail="Current native project membership is required")
        return {"user_id": user_id, "project_id": project_id, "roles": [role], "package_actions": ROLE_ACTIONS[role]}

    async def require_token(request, x_auth_token=None, x_project_id=None, **_):
        if not x_auth_token:
            raise HTTPException(status_code=401, detail="X-Auth-Token header is required")
        info = parse(x_auth_token)
        if x_project_id and x_project_id != info["project_id"]:
            raise HTTPException(status_code=403, detail="Project header does not match the original token scope")
        return info

    async def member(token_info=None, **_):
        current = owner(token_info["user_id"], token_info["project_id"])
        return {**token_info, "package_actions": current["package_actions"]}

    async def legacy_identity(request: Request):
        return await member(await require_token(request, request.headers.get("x-auth-token")))

    for module in (hub, image_keys_service):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub, "get_blob_store", lambda: store)
    for module in (
        hub,
        image_jobs,
        image_jobs_service,
        image_keys_service,
        published_images_service,
        published_images_api,
        public_images,
    ):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "require_token", require_token)
    monkeypatch.setattr(auth, "get_package_member_info", member)
    monkeypatch.setattr(published_images_api, "get_package_manager_info", member)
    for module in (image_keys_service, image_jobs_service):
        monkeypatch.setattr(module, "validate_token", lambda value, project: parse(value))
        monkeypatch.setattr(module, "get_package_member_info", member)
    monkeypatch.setattr(image_keys_service, "validate_package_owner", owner)
    monkeypatch.setattr(public_images, "_limiter", public_images.AnonymousRateLimiter())

    app = FastAPI()
    app.include_router(published_images_api.router, prefix="/v1")
    app.include_router(image_keys_api.router, prefix="/v1")
    app.include_router(hub.router, prefix="/v1")
    app.include_router(image_admin.router, prefix="/v1")
    # Legacy /v1/layers routes keep their FastAPI-bound native identity dependency.
    app.dependency_overrides[auth.get_package_manager_info] = legacy_identity

    clients = []

    def client(address: str = "198.51.100.10") -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=(address, 40000))
        opened = httpx.AsyncClient(transport=transport, base_url="http://image.test")
        clients.append(opened)
        return opened

    try:
        yield SimpleNamespace(
            app=app,
            client=client(),
            new_client=client,
            factory=factory,
            store=store,
            settings=settings,
            members=members,
            revoked_tokens=revoked_tokens,
        )
    finally:
        for opened in clients:
            await opened.aclose()
        await engine.dispose()


async def stage(h, payload: bytes, headers: dict) -> httpx.Response:
    return await h.client.put(
        "/v1/published-images/blobs/" + digest(payload),
        content=payload,
        headers={**headers, "Content-Type": "application/octet-stream", "Content-Length": str(len(payload))},
    )


async def publish_raw(h, payloads, headers, *, visibility="private", name="image") -> httpx.Response:
    return await h.client.post(
        "/v1/published-images",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "name": name,
            "manifest": publication_manifest(*payloads),
            "visibility": visibility,
        },
    )


async def publish(h, payload: bytes, headers: dict, *, visibility: str, name: str) -> dict:
    staged = await stage(h, payload, headers)
    assert staged.status_code == 200, staged.text
    response = await publish_raw(h, [payload], headers, visibility=visibility, name=name)
    assert response.status_code == 201, response.text
    return response.json()


async def three_visibilities(h) -> dict[str, tuple[dict, bytes]]:
    published = {}
    for visibility in ("private", "internal", "public"):
        payload = b"hsqs-" + visibility.encode() + b"-payload"
        published[visibility] = (
            await publish(h, payload, human(ALICE, PROJECT_A), visibility=visibility, name=visibility),
            payload,
        )
    return published


def assert_anonymous_projection(value: dict) -> None:
    for hidden in ("created_by", "storage_binding", "project_id"):
        assert hidden not in value
    for node in value["nodes"]:
        assert "project_id" not in node and "payload_path" not in node
    assert value["visibility"] == "public"


@pytest.mark.asyncio
async def test_anonymous_list_pages_are_capped_while_authenticated_pages_are_not(hub_images, monkeypatch):
    from palimpsest_hub.services import public_images

    h = hub_images
    monkeypatch.setattr(public_images, "ANONYMOUS_LIST_LIMIT", 1)
    first = await publish(h, b"hsqs-one", human(ALICE, PROJECT_A), visibility="public", name="one")
    second = await publish(h, b"hsqs-two", human(ALICE, PROJECT_A), visibility="public", name="two")
    anonymous_page = await h.client.get("/v1/published-images", params={"limit": 200})
    assert anonymous_page.status_code == 200 and len(anonymous_page.json()["items"]) == 1
    next_page = await h.client.get("/v1/published-images", params={"limit": 200, "offset": 1})
    assert {first["id"], second["id"]} == {
        item["id"] for response in (anonymous_page, next_page) for item in response.json()["items"]
    }
    full = await h.client.get("/v1/published-images", params={"limit": 200}, headers=human(ALICE, PROJECT_A))
    assert {item["id"] for item in full.json()["items"]} == {first["id"], second["id"]}


@pytest.mark.asyncio
async def test_anonymous_reads_see_only_public_images_without_owner_or_storage_fields(hub_images):
    h = hub_images
    published = await three_visibilities(h)
    public, public_payload = published["public"]

    listing = await h.client.get("/v1/published-images")
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [public["id"]]
    assert_anonymous_projection(listing.json()["items"][0])
    assert "not-in-public-get" not in listing.text and str(h.store.root) not in listing.text

    detail = await h.client.get("/v1/published-images/" + public["id"])
    assert detail.status_code == 200
    assert_anonymous_projection(detail.json())
    assert detail.json()["manifest_digest"] == public["manifest_digest"]

    blob = await h.client.get("/v1/published-images/" + public["id"] + "/blobs/" + digest(public_payload))
    assert blob.status_code == 200 and blob.content == public_payload
    assert str(h.store.root) not in str(blob.headers)

    for visibility in ("private", "internal"):
        hidden, payload = published[visibility]
        assert (await h.client.get("/v1/published-images/" + hidden["id"])).status_code == 404
        assert (
            await h.client.get("/v1/published-images/" + hidden["id"] + "/blobs/" + digest(payload))
        ).status_code == 404
        # Bytes present in the shared CAS are not downloadable through an unrelated public image.
        assert (
            await h.client.get("/v1/published-images/" + public["id"] + "/blobs/" + digest(payload))
        ).status_code == 404
    assert (await h.client.get("/v1/published-images/" + str(uuid4()))).status_code == 404

    # The authenticated owner projection is unchanged.
    owned = await h.client.get("/v1/published-images/" + public["id"], headers=human(ALICE, PROJECT_A))
    assert owned.json()["created_by"] == ALICE and owned.json()["project_id"] == PROJECT_A


@pytest.mark.asyncio
async def test_anonymous_writes_graph_and_key_management_require_authentication(hub_images):
    h = hub_images
    public = (await three_visibilities(h))["public"][0]
    payload = b"hsqs-anonymous-write"
    assert (await stage(h, payload, {})).status_code == 401
    assert (await publish_raw(h, [payload], {}, visibility="public")).status_code == 401
    extension = await h.client.post(
        "/v1/published-images/extensions",
        json={
            "request_id": str(uuid4()),
            "name": "child",
            "base_image_id": public["id"],
            "layers": [{"digest": digest(payload), "size_bytes": len(payload)}],
            "visibility": "public",
        },
    )
    assert extension.status_code == 401
    leaf = public["leaf_id"]
    for path in (f"/v1/layer-nodes/{leaf}", f"/v1/layer-nodes/{leaf}/children", f"/v1/layer-nodes/{leaf}/ancestors"):
        assert (await h.client.get(path)).status_code == 401
    assert (await h.client.get("/v1/image-keys")).status_code == 401
    created = await h.client.post("/v1/image-keys", json={"name": "anon", "actions": ["images:read"]})
    assert created.status_code == 401
    assert (await h.client.delete("/v1/image-keys/" + str(uuid4()))).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Auth-Token": "expired-or-unknown"},
        {"Authorization": "Bearer not-a-key"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer ppi_v1_" + "0" * 32 + "." + "A" * 43},
        {"Authorization": "Bearer ppk_v1_" + "0" * 32 + "." + "A" * 43},
        {"Authorization": "Bearer ppi_v1_" + "0" * 32 + "." + "A" * 43, "X-Auth-Token": token(ALICE, PROJECT_A)},
    ],
)
async def test_present_invalid_credentials_are_rejected_not_downgraded(hub_images, headers):
    h = hub_images
    public, payload = (await three_visibilities(h))["public"]
    for path in (
        "/v1/published-images",
        "/v1/published-images/" + public["id"],
        "/v1/published-images/" + public["id"] + "/blobs/" + digest(payload),
    ):
        response = await h.client.get(path, headers=headers)
        assert response.status_code == 401, (path, response.text)
        assert payload not in response.content


@pytest.mark.asyncio
async def test_revoked_keystone_token_is_401_even_for_public_reads(hub_images):
    h = hub_images
    public = (await three_visibilities(h))["public"][0]
    h.revoked_tokens.add(token(ALICE, PROJECT_A))
    response = await h.client.get("/v1/published-images/" + public["id"], headers=human(ALICE, PROJECT_A))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_rate_limit_is_per_peer_address_with_retry_after(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_anonymous_rate_limit_requests = 3
    first, second = h.new_client("203.0.113.1"), h.new_client("203.0.113.2")
    for _ in range(3):
        assert (await first.get("/v1/published-images")).status_code == 200
    limited = await first.get("/v1/published-images")
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    # Spoofed forwarding headers do not select another counter.
    spoofed = await first.get("/v1/published-images", headers={"X-Forwarded-For": "192.0.2.99"})
    assert spoofed.status_code == 429
    assert (await second.get("/v1/published-images")).status_code == 200
    # Authenticated callers are not charged against the anonymous budget.
    assert (await first.get("/v1/published-images", headers=human(ALICE, PROJECT_A))).status_code == 200
    h.settings.palimpsest_hub_anonymous_rate_limit_enabled = False
    assert (await first.get("/v1/published-images")).status_code == 200


def test_anonymous_rate_limiter_window_and_bounded_memory():
    limiter = public_images.AnonymousRateLimiter()
    assert limiter.hit("a", 100.0, limit=2, window=10, capacity=16) is None
    assert limiter.hit("a", 101.0, limit=2, window=10, capacity=16) is None
    assert limiter.hit("a", 101.5, limit=2, window=10, capacity=16) == 9
    assert limiter.hit("a", 109.9, limit=2, window=10, capacity=16) == 1
    assert limiter.hit("a", 110.0, limit=2, window=10, capacity=16) is None
    for index in range(5000):
        limiter.hit(f"client-{index}", 200.0 + index / 1000, limit=2, window=10, capacity=16)
    assert len(limiter) == 16
    # The oldest windows are evicted first; the newest client keeps its count.
    assert limiter.hit("client-4999", 205.0, limit=2, window=10, capacity=16) is None
    assert limiter.hit("client-4999", 205.0, limit=2, window=10, capacity=16) == 10


@pytest.mark.parametrize(
    "host,bucket",
    [
        ("203.0.113.9", "203.0.113.9"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("testclient", "testclient"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_anonymous_bucket_normalizes_mapped_ipv4_and_groups_ipv6_per_64(host, bucket):
    assert public_images.client_bucket(host) == bucket


@pytest.mark.asyncio
async def test_anonymous_rate_limit_shares_ipv6_64_and_mapped_ipv4_buckets(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_anonymous_rate_limit_requests = 2
    first, sibling = h.new_client("2001:db8:7:7::1"), h.new_client("2001:db8:7:7:ffff::2")
    other_prefix = h.new_client("2001:db8:7:8::1")
    assert (await first.get("/v1/published-images")).status_code == 200
    assert (await sibling.get("/v1/published-images")).status_code == 200
    # Rotating addresses inside one /64 does not reset the budget.
    assert (await sibling.get("/v1/published-images")).status_code == 429
    assert (await other_prefix.get("/v1/published-images")).status_code == 200
    plain, mapped = h.new_client("192.0.2.44"), h.new_client("::ffff:192.0.2.44")
    assert (await plain.get("/v1/published-images")).status_code == 200
    assert (await mapped.get("/v1/published-images")).status_code == 200
    assert (await plain.get("/v1/published-images")).status_code == 429


async def seed_legacy_layer(h, payload: bytes, *, name, project, user, parent=None, published=False):
    d = h.store
    session_id = uuid4().hex
    d.start_upload(session_id)
    d.upload_path(session_id).write_bytes(payload)
    verified = d.finalize_upload_inspection(session_id, digest(payload), max_bytes=1024 * 1024)
    d.publish_verified(d.upload_path(session_id), verified)
    config = {
        "name": name,
        "kind": "squashfs",
        "ubuntu_base": None,
        "python_version": None,
        "parent_digest": parent,
        "chain_id": None,
        "blob_digest": digest(payload),
        "disk_format": None,
        "arch": "x86_64",
        "os_variant": None,
        "base_image_digest": None,
    }
    async with h.factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=digest(payload),
                blob_md5=None,
                size_bytes=len(payload),
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                arch="x86_64",
                config_digest=compute_config_digest(config),
                chain_id=None,
                parent_digest=parent,
                name=name,
                kind="squashfs",
                config_json=config,
                project_id=project,
                is_published=published,
                created_by=user,
            )
        )
        await session.commit()
    return digest(payload)


async def assert_private_parent_hidden(h, child: str, parent: str, headers: dict) -> None:
    detail = await h.client.get("/v1/layers/" + child, headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["ancestors"] == []
    assert detail.json()["project_id"] is None and detail.json()["created_by"] is None
    chain = await h.client.get("/v1/layers/" + child + "/ancestors", headers=headers)
    assert [item["blob_digest"] for item in chain.json()] == [child]
    assert (await h.client.get("/v1/layers/" + parent, headers=headers)).status_code == 404
    assert (await h.client.get("/v1/layers/" + parent + "/blob", headers=headers)).status_code == 404
    bundle = await h.client.post("/v1/bundles", json={"refs": [child]}, headers=headers)
    assert bundle.status_code == 409
    assert b"hsqs-private-parent" not in bundle.content


@pytest.mark.asyncio
async def test_legacy_published_child_does_not_expose_private_ancestor(hub_images):
    """Gap (a): `is_published` on a child never makes its private parent readable."""
    h = hub_images
    parent_payload, child_payload = b"hsqs-private-parent", b"hsqs-published-child"
    parent = await seed_legacy_layer(h, parent_payload, name="private-parent", project=PROJECT_A, user=ALICE)
    child = await seed_legacy_layer(
        h, child_payload, name="published-child", project=PROJECT_A, user=ALICE, parent=parent, published=True
    )
    await assert_private_parent_hidden(h, child, parent, human(BOB, PROJECT_B))
    # A publication cannot adopt the hidden parent bytes through the published child.
    leaked = await publish_raw(h, [parent_payload, child_payload], human(BOB, PROJECT_B), visibility="public")
    assert leaked.status_code == 404
    # The public child alone remains reusable by design and carries no parent bytes.
    reused = await publish_raw(h, [child_payload], human(BOB, PROJECT_B), visibility="public", name="reuse")
    assert reused.status_code == 201, reused.text
    assert [node["digest"] for node in reused.json()["nodes"]] == [child]
    anonymous = await h.client.get("/v1/published-images/" + reused.json()["id"] + "/blobs/" + parent)
    assert anonymous.status_code == 404


async def legacy_upload(h, payload: bytes, meta: dict, headers: dict) -> httpx.Response:
    started = await h.client.post("/v1/uploads", json={}, headers=headers)
    assert started.status_code == 200, started.text
    upload = started.json()["session_id"]
    patched = await h.client.patch(f"/v1/uploads/{upload}", content=payload, headers={**headers, "Upload-Offset": "0"})
    assert patched.status_code == 200, patched.text
    return await h.client.put(
        f"/v1/uploads/{upload}", json=meta, headers={**headers, "Upload-Offset": str(len(payload))}
    )


@pytest.mark.asyncio
async def test_identical_bytes_upload_grants_only_the_proven_layer_not_its_private_chain(hub_images):
    """Gap (b): proving possession of a private child's bytes never reveals its private ancestry."""
    h = hub_images
    parent_payload, child_payload = b"hsqs-private-parent", b"hsqs-private-child"
    parent = await seed_legacy_layer(h, parent_payload, name="private-parent", project=PROJECT_A, user=ALICE)
    child = await seed_legacy_layer(
        h, child_payload, name="private-child", project=PROJECT_A, user=ALICE, parent=parent
    )
    bob = human(BOB, PROJECT_B)
    assert (await h.client.get("/v1/layers/" + child, headers=bob)).status_code == 404
    meta = {"name": "private-child", "kind": "squashfs", "parent_digest": parent, "arch": "x86_64"}
    registered = await legacy_upload(h, child_payload, meta, bob)
    assert registered.status_code == 200, registered.text
    assert registered.json()["already_present"] is True
    await assert_private_parent_hidden(h, child, parent, bob)
    # The grant is not publication authority over the foreign private child either.
    adopted = await publish_raw(h, [child_payload], bob, visibility="public", name="adopted")
    assert adopted.status_code == 404
