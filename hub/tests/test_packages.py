"""Project/key boundaries with real bounded archives, SQL transactions and filesystem CAS."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tarfile
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.api import hub as legacy
from palimpsest_hub.api import packages
from palimpsest_hub.auth import get_package_member_info
from palimpsest_hub.models import (
    Base,
    PackageCache,
    PackageKey,
    PackageTag,
    PackageUpload,
    PackageVersion,
    PalimpsestHubLayerAccess,
    RegistryPackage,
)
from palimpsest_hub.services import image_exports
from palimpsest_hub.services import package_registry as registry
from palimpsest_hub.services.hub_store import LocalPathBlobStore

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"


def digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def encoded(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def tar_bytes(files):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in files:
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
    return result.getvalue()


def image_archive(marker=b"first image"):
    layer = tar_bytes([("marker", marker)])
    config = encoded(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [digest(layer)]},
            "config": {"Cmd": ["/bin/sh"]},
        }
    )
    blobs = {digest(layer): layer, digest(config): config}
    config_descriptor = {"digest": digest(config), "size": len(config), "mediaType": CONFIG}
    root = encoded(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "config": config_descriptor,
            "layers": [{"digest": digest(layer), "size": len(layer), "mediaType": LAYER}],
        }
    )
    blobs[digest(root)] = root
    index = encoded(
        {"schemaVersion": 2, "manifests": [{"digest": digest(root), "size": len(root), "mediaType": MANIFEST}]}
    )
    archive = tar_bytes(
        [
            ("oci-layout", encoded({"imageLayoutVersion": "1.0.0"})),
            ("index.json", index),
            *[("blobs/sha256/" + key[7:], value) for key, value in blobs.items()],
        ]
    )
    body = {
        "package_type": "oci-image",
        "tag": "v1",
        "root_digest": digest(root),
        "archive_digest": digest(archive),
        "archive_size_bytes": len(archive),
        "expected_tag_digest": None,
        "provenance": {"source_revision": "fixture-revision"},
    }
    return archive, body, blobs


def cache_archive(binding):
    layer = tar_bytes([("cache-data", b"cached result")])
    config = encoded(
        {
            "layers": [{"blob": digest(layer), "parent": -1}],
            "records": [{"digest": digest(b"recipe"), "layers": [{"layer": 0}]}],
        }
    )
    root = encoded(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "config": {
                "digest": digest(config),
                "size": len(config),
                "mediaType": "application/vnd.buildkit.cacheconfig.v0",
            },
            "layers": [{"digest": digest(layer), "size": len(layer), "mediaType": LAYER}],
        }
    )
    descriptor = {"digest": digest(root), "size": len(root), "mediaType": MANIFEST}
    index = encoded({"schemaVersion": 2, "manifests": [descriptor]})
    wrapper = {"schema": "palimpsest-buildkit-cache-archive-v1", **binding, "oci_manifest_digest": None}
    return tar_bytes(
        [
            ("palimpsest-cache.json", encoded(wrapper)),
            ("cache/oci-layout", encoded({"imageLayoutVersion": "1.0.0"})),
            ("cache/index.json", index),
            *[("cache/blobs/sha256/" + digest(value)[7:], value) for value in (layer, config, root)],
        ]
    )


@pytest.fixture
async def hub(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'registry.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    blob_store = LocalPathBlobStore(tmp_path / "cas")
    settings = SimpleNamespace(
        palimpsest_hub_package_forbidden_project_ids=("ProtectedProject",),
        palimpsest_hub_package_forbidden_user_ids=("ProtectedUser",),
        palimpsest_hub_package_namespace_bindings={"alpha": "Project-A", "beta": "Project-B"},
        palimpsest_hub_package_public_origin="https://registry.example",
        palimpsest_hub_max_blob_bytes=4 * 1024 * 1024,
        palimpsest_hub_max_bundle_expanded_bytes=8 * 1024 * 1024,
    )
    people = {
        "one": {
            "user_id": "f" * 64,
            "project_id": "Project-A",
            "project_name": "Renamable project",
            "roles": ["member"],
            "can_write": True,
        },
        "two": {
            "user_id": "OtherMember",
            "project_id": "Project-A",
            "project_name": "Renamable project",
            "roles": ["member"],
            "can_write": True,
        },
        "foreign": {
            "user_id": "ForeignMember",
            "project_id": "Project-B",
            "project_name": "Renamable project",
            "roles": ["member"],
            "can_write": True,
        },
        "admin": {
            "user_id": "AdminRole",
            "project_id": "Project-A",
            "project_name": "Renamable project",
            "roles": ["admin", "member"],
            "can_write": True,
        },
        "protected": {
            "user_id": "ProtectedUser",
            "project_id": "Project-A",
            "project_name": "Renamable project",
            "roles": ["member"],
            "can_write": True,
        },
    }

    async def original_token(request, x_auth_token=None, x_project_id=None):
        token = x_auth_token or request.headers.get("x-auth-token")
        if token not in people:
            raise registry.RegistryError(401, "AUTH_REQUIRED", "member token required")
        value = people[token].copy()
        asserted = x_project_id or request.headers.get("x-project-id")
        if asserted and asserted != value["project_id"]:
            raise registry.RegistryError(403, "PROJECT_SCOPE_MISMATCH", "wrong original project")
        return {**value, "token": token}

    async def original_member(token_info):
        registry.require_policy(token_info)
        return token_info

    async def member_dependency(request: Request):
        return await original_member(await original_token(request))

    disabled = set()

    def owner(user_id, project_id):
        if user_id in disabled:
            raise registry.RegistryError(403, "PACKAGE_SCOPE_DENIED", "membership removed")
        for person in people.values():
            if person["user_id"] == user_id and person["project_id"] == project_id:
                return person.copy()
        raise registry.RegistryError(403, "PACKAGE_SCOPE_DENIED", "membership removed")

    monkeypatch.setattr(registry, "get_session_factory", lambda: factory)
    monkeypatch.setattr(registry, "get_blob_store", lambda: blob_store)
    monkeypatch.setattr(registry, "get_settings", lambda: settings)
    monkeypatch.setattr(registry, "validate_package_owner", owner)
    monkeypatch.setattr(packages, "require_token", original_token)
    monkeypatch.setattr(packages, "get_package_member_info", original_member)
    monkeypatch.setattr(legacy, "get_session_factory", lambda: factory)
    monkeypatch.setattr(legacy, "get_blob_store", lambda: blob_store)
    monkeypatch.setattr(legacy, "get_settings", lambda: settings)
    monkeypatch.setattr(image_exports, "get_session_factory", lambda: factory)
    monkeypatch.setattr(image_exports, "get_blob_store", lambda: blob_store)
    app = FastAPI()
    app.include_router(packages.router, prefix="/v1")
    app.include_router(legacy.router, prefix="/v1")
    app.dependency_overrides[get_package_member_info] = member_dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        yield SimpleNamespace(
            client=client, store=blob_store, factory=factory, settings=settings, people=people, disabled=disabled
        )
    await engine.dispose()


async def key(hub, person="one", package="test", actions=None):
    member = hub.people[person]
    token = {"X-Auth-Token": person}
    context = await hub.client.put(f"/v1/projects/{member['project_id']}/namespace", headers=token, json={})
    assert context.status_code in {200, 201}
    namespace = context.json()["namespace"]
    response = await hub.client.post(
        f"/v1/projects/{namespace}/keys",
        headers=token,
        json={
            "name": "behavioral test",
            "scope": {"packages": [package]},
            "actions": actions or ["packages:read", "packages:write"],
            "expires_in_days": 1,
        },
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    return namespace, response.json(), {"Authorization": "Bearer " + response.json()["secret"]}


async def staged(hub, namespace, credential, payload, body, *, package="test", resource="package"):
    segment = "cache/uploads" if resource == "cache" else "uploads"
    path = f"/v1/projects/{namespace}/{segment}"
    started = await hub.client.post(path, params={"package": package}, headers=credential, json=body)
    assert started.status_code == 201
    upload = started.json()
    path += "/" + upload["upload_id"]
    appended = await hub.client.patch(
        path,
        params={"package": package},
        headers={**credential, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
        content=payload,
    )
    assert appended.status_code == 204
    assert appended.headers["upload-offset"] == str(len(payload))
    return path, upload


async def publish(hub, namespace, credential, payload, body, *, package="test"):
    path, upload = await staged(hub, namespace, credential, payload, body, package=package)
    result = await hub.client.put(path, params={"package": package}, headers=credential, json={})
    return result, path, upload


@pytest.mark.asyncio
async def test_private_complete_image_preserves_federated_owner_and_graph(hub):
    namespace, issued, credential = await key(hub)
    archive, body, graph = image_archive()
    response, path, upload = await publish(hub, namespace, credential, archive, body)
    assert response.status_code == 201
    result = response.json()
    assert result["project_id"] == "Project-A"
    assert result["digest"] == body["root_digest"]
    assert upload["owner_user_id"] == "f" * 64
    inventory = await hub.client.get(f"/v1/projects/{namespace}/packages", headers={"X-Auth-Token": "two"})
    item = inventory.json()["items"][0]
    assert (item["name"], item["version_count"], item["latest_pushed_by"]) == ("test", 1, "f" * 64)
    version_path = f"/v1/projects/{namespace}/versions/{body['root_digest']}"
    metadata = (await hub.client.get(version_path, params={"package": "test"}, headers=credential)).json()
    assert set(metadata["graph"]) == set(graph)
    assert metadata["pushed_by"] == "f" * 64
    assert metadata["total_bytes"] == sum(len(value) for value in graph.values())
    downloaded = await hub.client.get(version_path + "/download", params={"package": "test"}, headers=credential)
    assert downloaded.content == archive
    assert downloaded.headers["cache-control"] == "private, no-store"
    layer_digest = next(iter(graph))
    ranged = await hub.client.get(
        version_path + "/blobs/" + layer_digest,
        params={"package": "test"},
        headers={**credential, "Range": "bytes=0-2"},
    )
    assert ranged.status_code == 206 and ranged.content == graph[layer_digest][:3]
    hidden = await hub.client.get(
        version_path + "/blobs/" + digest(b"unreachable"), params={"package": "test"}, headers=credential
    )
    assert hidden.status_code == 404
    repeated = await hub.client.put(path, params={"package": "test"}, headers=credential, json={})
    assert repeated.status_code == 200 and repeated.json()["digest"] == result["digest"]
    keys = (await hub.client.get(f"/v1/projects/{namespace}/keys", headers={"X-Auth-Token": "one"})).json()["items"]
    assert keys[0]["owner_user_id"] == "f" * 64
    assert issued["secret"] not in json.dumps(keys) and "secret_hash" not in json.dumps(keys)


@pytest.mark.asyncio
async def test_scope_project_and_owner_key_sessions_are_exact(hub):
    namespace, _, first = await key(hub, package="test")
    _, _, second = await key(hub, person="two", package="test")
    _, _, other_scope = await key(hub, package="test/child")
    foreign_namespace, _, foreign = await key(hub, person="foreign")
    archive, body, _ = image_archive()
    path, upload = await staged(hub, namespace, first, archive, body)
    for credential in (second, foreign):
        for method in ("get", "put", "delete"):
            response = await getattr(hub.client, method)(
                path, params={"package": "test"}, headers=credential, **({"json": {}} if method == "put" else {})
            )
            assert response.status_code in {403, 404}
    assert (
        await hub.client.post(
            f"/v1/projects/{namespace}/uploads", params={"package": "test"}, headers=other_scope, json=body
        )
    ).status_code == 403
    assert (
        await hub.client.post(
            f"/v1/projects/{foreign_namespace}/uploads", params={"package": "test"}, headers=first, json=body
        )
    ).status_code == 403
    assert (
        await hub.client.post(
            f"/v1/projects/{namespace}/uploads", params={"package": "test"}, headers={"X-Auth-Token": "one"}, json=body
        )
    ).status_code == 401
    assert (await hub.client.get("/v1/auth/me", headers={**first, "X-Project-Id": "project-a"})).status_code == 403
    assert (await hub.client.get("/v1/auth/me", headers={**first, "X-Auth-Token": "one"})).status_code == 401
    async with hub.factory() as session:
        assert await session.scalar(select(func.count()).select_from(RegistryPackage)) == 0
        assert await session.scalar(select(func.count()).select_from(PalimpsestHubLayerAccess)) == 0
        row = await session.get(PackageUpload, upload["upload_id"])
        assert row.received_bytes == len(archive) and row.key_id == first["Authorization"][14:46]
    assert not hub.store.blobs_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "membership", "expiry", "role"])
async def test_authority_removed_during_transfer_prevents_publication(hub, change):
    namespace, issued, credential = await key(hub)
    archive, body, _ = image_archive()
    path, _ = await staged(hub, namespace, credential, archive, body)
    if change == "revoke":
        assert (
            await hub.client.delete(
                f"/v1/projects/{namespace}/keys/{issued['key']['key_id']}", headers={"X-Auth-Token": "one"}
            )
        ).status_code == 204
    elif change == "membership":
        hub.disabled.add(hub.people["one"]["user_id"])
    elif change == "role":
        hub.people["one"]["can_write"] = False
        hub.people["one"]["roles"] = ["reader"]
        assert (await hub.client.get("/v1/auth/me", headers=credential)).status_code == 200
    else:
        async with hub.factory() as session:
            row = await session.get(PackageKey, issued["key"]["key_id"].replace("-", ""))
            row.expires_at = registry.now() - timedelta(seconds=1)
            await session.commit()
    final = await hub.client.put(path, params={"package": "test"}, headers=credential, json={})
    assert final.status_code == (403 if change in {"membership", "role"} else 401)
    async with hub.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PackageVersion)) == 0
        assert await session.scalar(select(func.count()).select_from(PackageTag)) == 0
    assert not hub.store.blobs_dir.exists()


@pytest.mark.asyncio
async def test_key_expiring_during_keystone_lookup_cannot_start_upload(hub, monkeypatch):
    namespace, issued, credential = await key(hub)
    async with hub.factory() as session:
        deadline = (await session.get(PackageKey, issued["key"]["key_id"].replace("-", ""))).expires_at
    clock = [deadline - timedelta(seconds=1)]
    monkeypatch.setattr(registry, "now", lambda: clock[0])
    original_owner = registry.validate_package_owner

    def slow_identity(user, project):
        member = original_owner(user, project)
        clock[0] = deadline
        return member

    monkeypatch.setattr(registry, "validate_package_owner", slow_identity)
    _, body, _ = image_archive()
    response = await hub.client.post(
        f"/v1/projects/{namespace}/uploads", params={"package": "test"}, headers=credential, json=body
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "KEY_EXPIRED"
    async with hub.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PackageUpload)) == 0


@pytest.mark.asyncio
async def test_revocation_during_identity_lookup_denies_the_waiting_request(hub, monkeypatch):
    from threading import Event

    _, issued, credential = await key(hub)
    entered, release = Event(), Event()
    original_owner = registry.validate_package_owner

    def held_identity(user, project):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("identity fixture release timed out")
        return original_owner(user, project)

    monkeypatch.setattr(registry, "validate_package_owner", held_identity)
    waiting = asyncio.create_task(hub.client.get("/v1/auth/me", headers=credential))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        async with hub.factory() as session:
            row = await session.get(PackageKey, issued["key"]["key_id"].replace("-", ""))
            row.revoked_at = registry.now()
            await session.commit()
    finally:
        release.set()
    response = await waiting
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "KEY_REVOKED"


@pytest.mark.asyncio
async def test_tag_compare_and_set_retains_winner_and_immutable_history(hub):
    namespace, _, credential = await key(hub)
    first_archive, first, _ = image_archive(b"initial")
    assert (await publish(hub, namespace, credential, first_archive, first))[0].status_code == 201
    one_archive, one, _ = image_archive(b"one contender")
    two_archive, two, _ = image_archive(b"two contender")
    one["expected_tag_digest"] = two["expected_tag_digest"] = first["root_digest"]
    one_path, _ = await staged(hub, namespace, credential, one_archive, one)
    two_path, _ = await staged(hub, namespace, credential, two_archive, two)
    results = await asyncio.gather(
        *[
            hub.client.put(path, params={"package": "test"}, headers=credential, json={})
            for path in (one_path, two_path)
        ]
    )
    assert sorted(result.status_code for result in results) == [201, 412]
    winner = next(result.json()["digest"] for result in results if result.status_code == 201)
    resolved = await hub.client.get(
        f"/v1/projects/{namespace}/resolve", params={"package": "test", "tag": "v1"}, headers=credential
    )
    assert resolved.json()["digest"] == winner and resolved.headers["etag"] == '"' + winner + '"'
    history = (
        await hub.client.get(f"/v1/projects/{namespace}/versions", params={"package": "test"}, headers=credential)
    ).json()["items"]
    assert {row["root_digest"] for row in history} == {first["root_digest"], winner}


@pytest.mark.asyncio
async def test_equal_bytes_require_independent_publication_and_survive_legacy_gc(hub):
    namespace, _, credential = await key(hub)
    other_namespace, _, other = await key(hub, person="foreign")
    archive, body, graph = image_archive()
    assert (await publish(hub, namespace, credential, archive, body))[0].status_code == 201
    remote = f"/v1/projects/{other_namespace}/versions/{body['root_digest']}"
    assert (await hub.client.get(remote, params={"package": "test"}, headers=other)).status_code == 404
    # Existing equal bytes in global CAS cannot fill a graph missing from this upload.
    incomplete = tar_bytes(
        [
            ("oci-layout", encoded({"imageLayoutVersion": "1.0.0"})),
            (
                "index.json",
                encoded(
                    {
                        "schemaVersion": 2,
                        "manifests": [
                            {
                                "digest": body["root_digest"],
                                "size": len(graph[body["root_digest"]]),
                                "mediaType": MANIFEST,
                            }
                        ],
                    }
                ),
            ),
            ("blobs/sha256/" + body["root_digest"][7:], graph[body["root_digest"]]),
        ]
    )
    incomplete_body = {**body, "archive_digest": digest(incomplete), "archive_size_bytes": len(incomplete)}
    denied, _, _ = await publish(hub, other_namespace, other, incomplete, incomplete_body)
    assert denied.status_code == 422
    assert (await hub.client.get(remote, params={"package": "test"}, headers=other)).status_code == 404
    assert (await publish(hub, other_namespace, other, archive, body))[0].status_code == 201
    for blob in set(graph) | {digest(archive)}:
        await legacy._discard_unregistered_blob(hub.store, hub.factory, blob)
        assert hub.store.exists(blob)
        os.utime(hub.store.blob_path(blob), (1, 1))
    await image_exports.run_export_maintenance(max_age_seconds=1)
    assert all(hub.store.exists(blob) for blob in set(graph) | {digest(archive)})
    async with hub.factory() as session:
        assert await session.scalar(select(func.count()).select_from(RegistryPackage)) == 2
        assert await session.scalar(select(func.count()).select_from(PalimpsestHubLayerAccess)) == 0


@pytest.mark.asyncio
async def test_package_only_key_cannot_cache_and_valid_cache_has_no_inventory_side_effect(hub):
    namespace, _, only_package = await key(hub)
    _, _, cache_key = await key(hub, actions=["packages:read", "cache:read", "cache:write"])
    binding = {
        "project_id": "Project-A",
        "namespace": namespace,
        "package": "test",
        "build_key": digest(b"build"),
        "cache_scope": "default",
        "platform": "linux/amd64",
        "builder_fingerprint": digest(b"builder"),
    }
    archive = cache_archive(binding)
    body = {key: value for key, value in binding.items() if key not in {"project_id", "namespace", "package"}}
    body.update(archive_digest=digest(archive), archive_size_bytes=len(archive))
    assert (
        await hub.client.post(
            f"/v1/projects/{namespace}/cache/uploads", params={"package": "test"}, headers=only_package, json=body
        )
    ).status_code == 403
    path, _ = await staged(hub, namespace, cache_key, archive, body, resource="cache")
    assert (await hub.client.put(path, params={"package": "test"}, headers=cache_key, json={})).status_code == 201
    lookup = {key: value for key, value in body.items() if key not in {"archive_digest", "archive_size_bytes"}}
    resolved = await hub.client.get(
        f"/v1/projects/{namespace}/cache/resolve", params={"package": "test", **lookup}, headers=cache_key
    )
    assert resolved.json()["resolution"] == "exact"
    lookup["build_key"] = digest(b"other build")
    fallback = await hub.client.get(
        f"/v1/projects/{namespace}/cache/resolve", params={"package": "test", **lookup}, headers=cache_key
    )
    assert fallback.json()["resolution"] == "scope" and fallback.json()["build_key"] == binding["build_key"]
    inventory = await hub.client.get(f"/v1/projects/{namespace}/packages", headers=only_package)
    assert inventory.json()["items"] == []
    async with hub.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PackageCache)) == 1
        assert await session.scalar(select(func.count()).select_from(RegistryPackage)) == 0


@pytest.mark.asyncio
async def test_protected_and_admin_member_tokens_and_empty_policy_fail_closed(hub):
    for person in ("admin", "protected"):
        context = await hub.client.get("/v1/projects/current", headers={"X-Auth-Token": person})
        assert context.status_code == 403
        assert (await hub.client.post("/v1/uploads", headers={"X-Auth-Token": person}, json={})).status_code == 403
    hub.settings.palimpsest_hub_package_forbidden_user_ids = ()
    assert (await hub.client.get("/v1/projects/current", headers={"X-Auth-Token": "one"})).status_code == 503
    assert not hub.store.uploads_dir.exists()


@pytest.mark.asyncio
async def test_key_inventory_scope_and_explicit_whole_project_are_distinct(hub):
    namespace, _, exact = await key(hub, package="test")
    _, _, other = await key(hub, person="two", package="other")
    archive, body, _ = image_archive()
    assert (await publish(hub, namespace, exact, archive, body))[0].status_code == 201
    assert (await publish(hub, namespace, other, archive, body, package="other"))[0].status_code == 201
    own = (await hub.client.get(f"/v1/projects/{namespace}/packages", headers=exact)).json()
    assert [item["name"] for item in own["items"]] == ["test"]
    assert (
        await hub.client.get(f"/v1/projects/{namespace}/package", params={"package": "other"}, headers=exact)
    ).status_code == 403
    issued = await hub.client.post(
        f"/v1/projects/{namespace}/keys",
        headers={"X-Auth-Token": "one"},
        json={"name": "explicit whole project", "scope": {"all_packages": True}, "actions": ["packages:read"]},
    )
    assert issued.status_code == 201
    whole = {"Authorization": "Bearer " + issued.json()["secret"]}
    inventory = (await hub.client.get(f"/v1/projects/{namespace}/packages", headers=whole)).json()
    assert [item["name"] for item in inventory["items"]] == ["other", "test"]
    malformed = [
        {"scope": {"all_packages": 1}},
        {"scope": {"all_packages": True, "packages": ["test"]}},
        {"scope": {"packages": ["test", "test"]}},
        {"actions": ["packages:write"]},
        {"expires_in_days": 91},
    ]
    for override in malformed:
        request = {
            "name": "invalid delegation",
            "scope": {"packages": ["test"]},
            "actions": ["packages:read"],
            **override,
        }
        assert (
            await hub.client.post(f"/v1/projects/{namespace}/keys", headers={"X-Auth-Token": "one"}, json=request)
        ).status_code == 422
    own_keys = (await hub.client.get(f"/v1/projects/{namespace}/keys", headers={"X-Auth-Token": "one"})).json()["items"]
    assert {item["name"] for item in own_keys} == {"behavioral test", "explicit whole project"}


@pytest.mark.asyncio
async def test_offset_acknowledgment_discards_unacknowledged_residue_and_new_key_cannot_resume(hub):
    namespace, _, credential = await key(hub)
    _, _, replacement = await key(hub)
    archive, body, _ = image_archive()
    started = await hub.client.post(
        f"/v1/projects/{namespace}/uploads", params={"package": "test"}, headers=credential, json=body
    )
    upload = started.json()
    path = f"/v1/projects/{namespace}/uploads/{upload['upload_id']}"
    midpoint = len(archive) // 2
    first = await hub.client.patch(
        path,
        params={"package": "test"},
        headers={**credential, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
        content=archive[:midpoint],
    )
    assert first.status_code == 204 and first.headers["upload-offset"] == str(midpoint)
    conflict = await hub.client.patch(
        path,
        params={"package": "test"},
        headers={**credential, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
        content=b"wrong offset",
    )
    assert conflict.status_code == 409 and conflict.headers["upload-offset"] == str(midpoint)
    assert (await hub.client.get(path, params={"package": "test"}, headers=replacement)).status_code == 404
    with hub.store.upload_path(upload["upload_id"]).open("ab") as handle:
        handle.write(b"unacknowledged crash residue")
    second = await hub.client.patch(
        path,
        params={"package": "test"},
        headers={**credential, "Upload-Offset": str(midpoint), "Content-Type": "application/octet-stream"},
        content=archive[midpoint:],
    )
    assert second.status_code == 204 and second.headers["upload-offset"] == str(len(archive))
    assert hub.store.upload_path(upload["upload_id"]).read_bytes() == archive
    assert (await hub.client.put(path, params={"package": "test"}, headers=credential, json={})).status_code == 201


@pytest.mark.asyncio
async def test_same_root_repush_retains_immutable_archive_and_acknowledges_current_actor(hub):
    namespace, _, original = await key(hub)
    _, issued, second = await key(hub, person="two")
    archive, body, _ = image_archive()
    assert (await publish(hub, namespace, original, archive, body))[0].status_code == 201
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        files = [(member.name, source.extractfile(member).read()) for member in source.getmembers()]
    repacked = tar_bytes(reversed(files))
    result, _, _ = await publish(
        hub,
        namespace,
        second,
        repacked,
        {**body, "archive_digest": digest(repacked), "archive_size_bytes": len(repacked)},
    )
    assert result.status_code == 200 and result.json()["already_published"] is True
    assert result.json()["pushed_by"] == "OtherMember"
    assert result.json()["pushed_key_id"] == issued["key"]["key_id"]
    metadata = (
        await hub.client.get(
            f"/v1/projects/{namespace}/versions/{body['root_digest']}", params={"package": "test"}, headers=second
        )
    ).json()
    assert metadata["pushed_by"] == "f" * 64 and metadata["archive_digest"] == digest(archive)
    assert not hub.store.exists(digest(repacked))


@pytest.mark.asyncio
async def test_namespace_registration_is_explicit_immutable_and_not_a_display_name(hub):
    project = "c" * 32
    hub.people["one"]["project_id"] = project
    token = {"X-Auth-Token": "one"}
    context = await hub.client.get("/v1/projects/current", headers=token)
    assert context.json()["namespace"] is None
    assert (await hub.client.get(f"/v1/projects/p-{project}/packages", headers=token)).status_code == 404
    assert (
        await hub.client.put(f"/v1/projects/{project}/namespace", headers=token, json={"namespace": "arbitrary-alias"})
    ).status_code == 422
    registered = await hub.client.put(f"/v1/projects/{project}/namespace", headers=token, json={})
    assert registered.status_code == 201 and registered.json()["namespace"] == "p-" + project
    hub.people["one"]["project_name"] = "Changed display name"
    hub.settings.palimpsest_hub_package_namespace_bindings = {"new-name": project}
    existing = await hub.client.put(f"/v1/projects/{project}/namespace", headers=token, json={})
    assert existing.status_code == 200 and existing.json()["namespace"] == "p-" + project
    assert existing.json()["project_name"] == "Changed display name"
    assert (await hub.client.put("/v1/projects/foreign-project/namespace", headers=token, json={})).status_code == 403


@pytest.mark.asyncio
async def test_package_expanded_byte_limit_returns_413_without_inventory_or_cas_grants(hub):
    namespace, _, credential = await key(hub)
    archive, body, graph = image_archive()
    hub.settings.palimpsest_hub_max_bundle_expanded_bytes = 64
    response, path, upload = await publish(hub, namespace, credential, archive, body)
    assert response.status_code == 413
    state = await hub.client.get(path, params={"package": "test"}, headers=credential)
    assert state.json()["status"] == "failed"
    inventory = await hub.client.get(f"/v1/projects/{namespace}/packages", headers=credential)
    assert inventory.json()["items"] == []
    assert not hub.store.upload_path(upload["upload_id"].replace("-", "")).exists()
    for blob_digest in {*graph, body["archive_digest"]}:
        assert not hub.store.exists(blob_digest)
