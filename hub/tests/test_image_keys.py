"""`ppi_v1_` image API keys: issuance, storage, authentication, attenuation and revalidation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_public_images as public_harness
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_auth import FEDERATED_OWNER, ORIGINAL_SUBJECT, PROJECT_A, PROJECT_B, keystone_http, settings  # noqa: F401
from test_public_images import ALICE, AMY, BOB, digest, human, publish, publish_raw, stage

from palimpsest_hub.api import hub
from palimpsest_hub.api import image_keys as image_keys_api
from palimpsest_hub.api import published_images as published_images_api
from palimpsest_hub.image_key_models import PalimpsestImageKey
from palimpsest_hub.models import Base, PackageKey, PackageNamespace
from palimpsest_hub.services import image_keys as image_keys_service
from palimpsest_hub.services import package_registry, public_images
from palimpsest_hub.services.hub_store import LocalPathBlobStore

ALPHA, BETA = public_harness.PROJECT_A, public_harness.PROJECT_B
# Reuse the synthetic-boundary harness fixture under its own name.
hub_images = public_harness.hub_images


_SCHEMA_SCRIPT = """
import asyncio, sys
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from palimpsest_hub import database

async def main(url):
    assert "palimpsest_hub.image_key_models" not in sys.modules
    database._engine = create_async_engine(url)
    try:
        await database.create_schema()
        async with database._engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    finally:
        await database._engine.dispose()
    print("palimpsest_image_keys" in names)

asyncio.run(main(sys.argv[1]))
"""


def test_offline_create_schema_creates_image_key_table_in_a_fresh_process(tmp_path):
    """bootstrap.py runs create_schema without importing the API; the key table must still exist."""
    import os
    import subprocess
    import sys

    url = f"sqlite+aiosqlite:///{tmp_path / 'bootstrap.sqlite'}"
    result = subprocess.run(
        [sys.executable, "-c", _SCHEMA_SCRIPT, url],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "True"


async def issue(h, user, project, actions, **values) -> httpx.Response:
    return await h.client.post(
        "/v1/image-keys", headers=human(user, project), json={"name": "ci", "actions": actions, **values}
    )


async def bearer(h, user, project, actions) -> dict[str, str]:
    response = await issue(h, user, project, actions)
    assert response.status_code == 201, response.text
    return {"Authorization": "Bearer " + response.json()["secret"]}


async def key_row(h, key_id: str) -> PalimpsestImageKey:
    async with h.factory() as session:
        return await session.get(PalimpsestImageKey, key_id.replace("-", ""))


@pytest.mark.asyncio
async def test_issuance_returns_secret_once_and_stores_only_its_hash(hub_images, caplog):
    h = hub_images
    caplog.set_level(logging.DEBUG)
    response = await issue(h, ALICE, ALPHA, ["images:read", "images:publish"], expires_in_days=90)
    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    secret, key = response.json()["secret"], response.json()["key"]
    assert secret.startswith("ppi_v1_") and "secret" not in key
    assert (key["owner_user_id"], key["project_id"]) == (ALICE, ALPHA)
    raw = base64.urlsafe_b64decode(secret.split(".", 1)[1] + "=")
    row = await key_row(h, key["key_id"])
    assert row.secret_hash == hashlib.sha256(raw).hexdigest()
    async with h.factory() as session:
        stored = (await session.execute(select(PalimpsestImageKey.__table__))).all()
    assert len(stored) == 1
    flattened = repr(stored)
    assert secret not in flattened and secret.split(".", 1)[1] not in flattened
    assert row.expires_at - row.created_at == timedelta(days=90)
    listing = await h.client.get("/v1/image-keys", headers=human(ALICE, ALPHA))
    assert listing.status_code == 200 and listing.headers["Cache-Control"] == "no-store"
    assert [item["key_id"] for item in listing.json()["items"]] == [key["key_id"]]
    assert secret.split(".", 1)[1] not in listing.text and "secret" not in listing.json()["items"][0]
    await h.client.get("/v1/published-images", headers={"Authorization": "Bearer " + secret})
    assert secret.split(".", 1)[1] not in caplog.text
    # Keys are listed only to their owner.
    assert (await h.client.get("/v1/image-keys", headers=human(AMY, ALPHA))).json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        {"expires_in_days": 91},
        {"expires_in_days": 0},
        {"actions": ["packages:read"]},
        {"actions": ["images:read", "images:read"]},
        {"actions": []},
        {"name": ""},
        {"scope": {"all_packages": True}},
    ],
)
async def test_issuance_rejects_invalid_requests(hub_images, values):
    h = hub_images
    body = {"name": "ci", "actions": ["images:read"], **values}
    response = await h.client.post("/v1/image-keys", headers=human(ALICE, ALPHA), json=body)
    assert response.status_code == 422
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestImageKey))).all() == []


@pytest.mark.asyncio
async def test_reader_issues_only_read_keys_and_keys_cannot_manage_keys(hub_images):
    h = hub_images
    h.members[(AMY, ALPHA)] = "reader"
    assert (await issue(h, AMY, ALPHA, ["images:publish"])).status_code == 403
    assert (await issue(h, AMY, ALPHA, ["images:read", "images:publish"])).status_code == 403
    read_only = await issue(h, AMY, ALPHA, ["images:read"])
    assert read_only.status_code == 201, read_only.text
    assert read_only.json()["key"]["actions"] == ["images:read"]
    amy_key = {"Authorization": "Bearer " + read_only.json()["secret"]}
    assert (await h.client.get("/v1/published-images", headers=amy_key)).status_code == 200
    assert (await stage(h, b"hsqs-reader-key", amy_key)).status_code == 403
    h.members[(AMY, ALPHA)] = "member"
    key = await bearer(h, ALICE, ALPHA, ["images:read", "images:publish"])
    assert (
        await h.client.post("/v1/image-keys", headers=key, json={"name": "x", "actions": ["images:read"]})
    ).status_code == 401
    assert (await h.client.get("/v1/image-keys", headers=key)).status_code == 401
    listed = (await h.client.get("/v1/image-keys", headers=human(ALICE, ALPHA))).json()["items"]
    assert (await h.client.delete("/v1/image-keys/" + listed[0]["key_id"], headers=key)).status_code == 401
    both = {**key, **human(ALICE, ALPHA)}
    assert (
        await h.client.post("/v1/image-keys", headers=both, json={"name": "x", "actions": ["images:read"]})
    ).status_code == 401
    async with h.factory() as session:
        assert len((await session.scalars(select(PalimpsestImageKey))).all()) == 2


@pytest.mark.asyncio
async def test_revoked_expired_and_tampered_keys_are_401(hub_images):
    h = hub_images
    response = await issue(h, ALICE, ALPHA, ["images:read"])
    secret, key_id = response.json()["secret"], response.json()["key"]["key_id"]
    headers = {"Authorization": "Bearer " + secret}
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 200
    tampered = secret[:-1] + ("A" if secret[-1] != "A" else "B")
    assert (
        await h.client.get("/v1/published-images", headers={"Authorization": "Bearer " + tampered})
    ).status_code == 401
    async with h.factory() as session:
        row = await session.get(PalimpsestImageKey, key_id.replace("-", ""))
        row.expires_at = image_keys_service.now() - timedelta(seconds=1)
        await session.commit()
    expired = await h.client.get("/v1/published-images", headers=headers)
    assert expired.status_code == 401 and expired.json()["detail"]["code"] == "KEY_EXPIRED"

    other = await issue(h, ALICE, ALPHA, ["images:read"])
    other_headers = {"Authorization": "Bearer " + other.json()["secret"]}
    other_id = other.json()["key"]["key_id"]
    assert (await h.client.delete("/v1/image-keys/" + other_id, headers=human(AMY, ALPHA))).status_code == 404
    assert (await h.client.delete("/v1/image-keys/" + other_id, headers=human(ALICE, ALPHA))).status_code == 204
    assert (await h.client.delete("/v1/image-keys/" + other_id, headers=human(ALICE, ALPHA))).status_code == 204
    revoked = await h.client.get("/v1/published-images", headers=other_headers)
    assert revoked.status_code == 401 and revoked.json()["detail"]["code"] == "KEY_REVOKED"
    listed = (await h.client.get("/v1/image-keys", headers=human(ALICE, ALPHA))).json()["items"]
    assert next(item for item in listed if item["key_id"] == other_id)["revoked_at"] is not None


@pytest.mark.asyncio
async def test_read_and_publish_actions_are_enforced_separately(hub_images):
    h = hub_images
    public = await publish(h, b"hsqs-key-public", human(ALICE, ALPHA), visibility="public", name="public")
    reader = await bearer(h, ALICE, ALPHA, ["images:read"])
    publisher = await bearer(h, ALICE, ALPHA, ["images:publish"])

    leaf = public["leaf_id"]
    read_paths = [
        "/v1/published-images",
        "/v1/published-images/" + public["id"],
        "/v1/published-images/" + public["id"] + "/blobs/" + digest(b"hsqs-key-public"),
        f"/v1/layer-nodes/{leaf}",
        f"/v1/layer-nodes/{leaf}/children",
        f"/v1/layer-nodes/{leaf}/ancestors",
    ]
    for path in read_paths:
        assert (await h.client.get(path, headers=reader)).status_code == 200, path
        # Publication authority does not imply read authority.
        assert (await h.client.get(path, headers=publisher)).status_code == 403, path

    payload = b"hsqs-key-published"
    assert (await stage(h, payload, reader)).status_code == 403
    assert (await publish_raw(h, [payload], reader, visibility="private")).status_code == 403
    assert (await stage(h, payload, publisher)).status_code == 200
    published = await publish_raw(h, [payload], publisher, visibility="internal", name="by-key")
    assert published.status_code == 201, published.text
    # The publication is bound to the issuing user and project.
    owned = await h.client.get("/v1/published-images/" + published.json()["id"], headers=human(ALICE, ALPHA))
    assert owned.json()["created_by"] == ALICE and owned.json()["project_id"] == ALPHA
    extension_payload = b"hsqs-key-extension"
    assert (await stage(h, extension_payload, publisher)).status_code == 200
    extension = {
        "request_id": str(uuid4()),
        "name": "by-key-child",
        "base_image_id": published.json()["id"],
        "layers": [{"digest": digest(extension_payload), "size_bytes": len(extension_payload)}],
        "visibility": "internal",
    }
    # Extension reads the base manifest/graph, so publish-only is not enough.
    denied = await h.client.post("/v1/published-images/extensions", headers=publisher, json=extension)
    assert denied.status_code == 403
    both = await bearer(h, ALICE, ALPHA, ["images:read", "images:publish"])
    extended = await h.client.post("/v1/published-images/extensions", headers=both, json=extension)
    assert extended.status_code == 201, extended.text
    assert (
        await key_row(
            h, (await h.client.get("/v1/image-keys", headers=human(ALICE, ALPHA))).json()["items"][0]["key_id"]
        )
    ).last_used_at is not None


@pytest.mark.asyncio
async def test_key_actions_follow_current_owner_authority_and_membership(hub_images):
    h = hub_images
    publisher = await bearer(h, ALICE, ALPHA, ["images:read", "images:publish"])
    payload = b"hsqs-membership"
    assert (await stage(h, payload, publisher)).status_code == 200
    h.members[(ALICE, ALPHA)] = "reader"
    assert (await stage(h, payload, publisher)).status_code == 403
    assert (await publish_raw(h, [payload], publisher)).status_code == 403
    assert (await h.client.get("/v1/published-images", headers=publisher)).status_code == 200
    del h.members[(ALICE, ALPHA)]
    removed = await h.client.get("/v1/published-images", headers=publisher)
    assert removed.status_code == 403
    h.members[(ALICE, ALPHA)] = "member"
    assert (await h.client.get("/v1/published-images", headers=publisher)).status_code == 200


@pytest.mark.asyncio
async def test_membership_removed_between_admission_and_publication_stops_key(hub_images, monkeypatch):
    h = hub_images
    publisher = await bearer(h, ALICE, ALPHA, ["images:publish"])
    payload = b"hsqs-race"
    assert (await stage(h, payload, publisher)).status_code == 200
    original = image_keys_service.fresh_actor

    async def remove_then_recheck(actor, action):
        h.members.pop((ALICE, ALPHA), None)
        return await original(actor, action)

    monkeypatch.setattr(image_keys_service, "fresh_actor", remove_then_recheck)
    response = await publish_raw(h, [payload], publisher, visibility="private")
    assert response.status_code == 403
    h.members[(ALICE, ALPHA)] = "member"
    assert (await h.client.get("/v1/published-images", headers=human(ALICE, ALPHA))).json()["items"] == []


@pytest.mark.asyncio
async def test_internal_and_private_visibility_through_keys(hub_images):
    h = hub_images
    internal = await publish(h, b"hsqs-internal", human(ALICE, ALPHA), visibility="internal", name="internal")
    private = await publish(h, b"hsqs-private", human(ALICE, ALPHA), visibility="private", name="private")
    alice = await bearer(h, ALICE, ALPHA, ["images:read"])
    amy = await bearer(h, AMY, ALPHA, ["images:read"])
    bob = await bearer(h, BOB, BETA, ["images:read"])

    def ids(response):
        return {item["id"] for item in response.json()["items"]}

    assert ids(await h.client.get("/v1/published-images", headers=alice)) == {internal["id"], private["id"]}
    assert ids(await h.client.get("/v1/published-images", headers=amy)) == {internal["id"]}
    assert ids(await h.client.get("/v1/published-images", headers=bob)) == set()
    for image, payload, allowed in (
        (internal, b"hsqs-internal", (alice, amy)),
        (private, b"hsqs-private", (alice,)),
    ):
        for headers in (alice, amy, bob):
            expected = 200 if headers in allowed else 404
            assert (await h.client.get("/v1/published-images/" + image["id"], headers=headers)).status_code == expected
            blob = await h.client.get(
                "/v1/published-images/" + image["id"] + "/blobs/" + digest(payload), headers=headers
            )
            assert blob.status_code == expected
            node = await h.client.get("/v1/layer-nodes/" + image["leaf_id"], headers=headers)
            assert node.status_code == expected


@pytest.mark.asyncio
async def test_key_project_assertion_and_logical_target_are_rejected(hub_images):
    h = hub_images
    key = await bearer(h, ALICE, ALPHA, ["images:read"])
    assert (await h.client.get("/v1/published-images", headers={**key, "X-Project-Id": ALPHA})).status_code == 200
    assert (await h.client.get("/v1/published-images", headers={**key, "X-Project-Id": BETA})).status_code == 403
    assert (await h.client.get("/v1/published-images", headers={**key, "X-Target-Project-Id": BETA})).status_code == 403


@pytest.mark.asyncio
async def test_existing_package_key_has_no_image_authority(hub_images, monkeypatch):
    h = hub_images
    secret = bytes(range(32))
    wire = "ppk_v1_" + "c" * 32 + "." + base64.urlsafe_b64encode(secret).decode().rstrip("=")
    async with h.factory() as session:
        session.add(PackageNamespace(project_id=ALPHA, namespace="alpha", project_name="alpha"))
        await session.flush()
        session.add(
            PackageKey(
                id="c" * 32,
                project_id=ALPHA,
                owner_user_id=ALICE,
                secret_hash=hashlib.sha256(secret).hexdigest(),
                name="package",
                scope={"all_packages": True},
                actions=["packages:inventory", "packages:read", "packages:write"],
                created_at=image_keys_service.now(),
                expires_at=image_keys_service.now() + timedelta(days=30),
            )
        )
        await session.commit()
    monkeypatch.setattr(package_registry, "get_session_factory", lambda: h.factory)
    monkeypatch.setattr(
        package_registry,
        "get_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_package_forbidden_project_ids=("protected-project",),
            palimpsest_hub_package_forbidden_user_ids=("protected-user",),
        ),
    )
    monkeypatch.setattr(package_registry, "validate_package_owner", image_keys_service.validate_package_owner)
    # The credential is a currently valid package key for its own registry...
    actor = await package_registry.authenticate_key(wire)
    assert actor.project_id == ALPHA and "packages:write" in actor.actions
    # ...but grants nothing on any image route.
    public = await publish(h, b"hsqs-package-key", human(ALICE, ALPHA), visibility="public", name="public")
    headers = {"Authorization": "Bearer " + wire}
    for path in (
        "/v1/published-images",
        "/v1/published-images/" + public["id"],
        f"/v1/layer-nodes/{public['leaf_id']}",
    ):
        assert (await h.client.get(path, headers=headers)).status_code == 401
    assert (await stage(h, b"hsqs-package-key-write", headers)).status_code == 401
    assert (await h.client.get("/v1/image-keys", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_publish_only_key_cannot_reuse_payloads_it_did_not_stage(hub_images):
    h = hub_images
    amy_payload = b"hsqs-amy-internal"
    await publish(h, amy_payload, human(AMY, ALPHA), visibility="internal", name="amy")
    publisher = await bearer(h, ALICE, ALPHA, ["images:publish"])
    # Reusing bytes from an image the owner can see would reveal their presence.
    probe = await publish_raw(h, [amy_payload], publisher, visibility="internal", name="probe")
    assert probe.status_code == 404
    both = await bearer(h, ALICE, ALPHA, ["images:read", "images:publish"])
    reused = await publish_raw(h, [amy_payload], both, visibility="internal", name="reuse")
    assert reused.status_code == 201, reused.text


@pytest.mark.asyncio
async def test_active_key_cap_counts_only_unrevoked_unexpired_keys(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_image_key_max_active_per_user = 2
    first = await issue(h, ALICE, ALPHA, ["images:read"])
    second = await issue(h, ALICE, ALPHA, ["images:read"])
    assert first.status_code == second.status_code == 201
    limited = await issue(h, ALICE, ALPHA, ["images:read"])
    assert limited.status_code == 409
    assert limited.json()["detail"]["code"] == "image_key_limit"
    assert (limited.json()["detail"]["limit"], limited.json()["detail"]["active"]) == (2, 2)
    assert "secret" not in limited.text
    # The cap is per user.
    assert (await issue(h, AMY, ALPHA, ["images:read"])).status_code == 201
    revoked = await h.client.delete("/v1/image-keys/" + first.json()["key"]["key_id"], headers=human(ALICE, ALPHA))
    assert revoked.status_code == 204
    assert (await issue(h, ALICE, ALPHA, ["images:read"])).status_code == 201
    assert (await issue(h, ALICE, ALPHA, ["images:read"])).status_code == 409
    async with h.factory() as session:
        row = await session.get(PalimpsestImageKey, second.json()["key"]["key_id"].replace("-", ""))
        row.expires_at = image_keys_service.now() - timedelta(seconds=1)
        await session.commit()
    assert (await issue(h, ALICE, ALPHA, ["images:read"])).status_code == 201
    async with h.factory() as session:
        owned = (
            await session.scalars(select(PalimpsestImageKey).where(PalimpsestImageKey.owner_user_id == ALICE))
        ).all()
    assert len(owned) == 4


@pytest.mark.asyncio
async def test_key_use_locks_and_writes_only_when_last_use_is_due(hub_images, monkeypatch):
    h = hub_images
    response = await issue(h, ALICE, ALPHA, ["images:read"])
    headers, key_id = {"Authorization": "Bearer " + response.json()["secret"]}, response.json()["key"]["key_id"]
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 200
    first_use = (await key_row(h, key_id)).last_used_at
    assert first_use is not None
    calls = []
    original_commit, original_refresh = AsyncSession.commit, AsyncSession.refresh
    original_rollback = AsyncSession.rollback

    async def commit(self):
        calls.append("commit")
        return await original_commit(self)

    async def rollback(self):
        calls.append("rollback")
        return await original_rollback(self)

    async def refresh(self, instance, *args, **kwargs):
        calls.append("lock" if kwargs.get("with_for_update") else "refresh")
        return await original_refresh(self, instance, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(AsyncSession, "rollback", rollback)
    monkeypatch.setattr(AsyncSession, "refresh", refresh)
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 200
    # The revocation re-read starts a new transaction (fresh snapshot) and takes no lock.
    assert calls == ["rollback", "refresh", "rollback"]
    assert (await key_row(h, key_id)).last_used_at == first_use
    async with h.factory() as session:
        row = await session.get(PalimpsestImageKey, key_id.replace("-", ""))
        row.last_used_at = first_use - image_keys_service.LAST_USED_GRANULARITY
        await original_commit(session)
    calls.clear()
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 200
    assert calls == ["rollback", "refresh", "lock", "commit", "rollback"]
    assert (await key_row(h, key_id)).last_used_at > first_use


@pytest.mark.asyncio
async def test_revocation_during_identity_validation_stops_the_unlocked_request(hub_images, monkeypatch):
    h = hub_images
    response = await issue(h, ALICE, ALPHA, ["images:read"])
    headers, key_id = {"Authorization": "Bearer " + response.json()["secret"]}, response.json()["key"]["key_id"]
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 200
    loop = asyncio.get_running_loop()
    validate = image_keys_service.validate_package_owner

    async def revoke():
        async with h.factory() as session:
            row = await session.get(PalimpsestImageKey, key_id.replace("-", ""))
            row.revoked_at = image_keys_service.now()
            await session.commit()

    def revoking_owner(user_id, project_id):
        asyncio.run_coroutine_threadsafe(revoke(), loop).result(timeout=30)
        return validate(user_id, project_id)

    monkeypatch.setattr(image_keys_service, "validate_package_owner", revoking_owner)
    # last_used_at was just recorded, so this request takes the lock-free path.
    revoked = await h.client.get("/v1/published-images", headers=headers)
    assert revoked.status_code == 401 and revoked.json()["detail"]["code"] == "KEY_REVOKED"


@pytest.mark.asyncio
async def test_bearer_scheme_is_case_insensitive_but_only_bearer(hub_images):
    h = hub_images
    secret = (await issue(h, ALICE, ALPHA, ["images:read"])).json()["secret"]
    for value in ("bearer " + secret, "BEARER " + secret, "BeArEr " + secret, "Bearer  " + secret):
        assert (await h.client.get("/v1/published-images", headers={"Authorization": value})).status_code == 200
    for value in ("Basic " + secret, "Bearer", "Bearer ", secret, "Token " + secret):
        assert (await h.client.get("/v1/published-images", headers={"Authorization": value})).status_code == 401


@pytest.mark.asyncio
async def test_openapi_declares_keystone_token_and_image_key_schemes(hub_images):
    schema = hub_images.app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["KeystoneToken"]["type"] == "apiKey" and schemes["KeystoneToken"]["name"] == "X-Auth-Token"
    assert schemes["ImageKey"]["type"] == "http" and schemes["ImageKey"]["scheme"] == "bearer"
    for path, method in (
        ("/v1/published-images", "get"),
        ("/v1/published-images", "post"),
        ("/v1/published-images/{image_id}", "get"),
        ("/v1/published-images/{image_id}/blobs/{digest}", "get"),
        ("/v1/published-images/blobs/{digest}", "put"),
        ("/v1/published-images/extensions", "post"),
        ("/v1/layer-nodes/{node_id}", "get"),
        ("/v1/image-quota/me", "get"),
    ):
        security = schema["paths"][path][method]["security"]
        assert {"KeystoneToken": []} in security and {"ImageKey": []} in security, (path, method)
    for method in ("get", "post"):
        security = schema["paths"]["/v1/image-keys"][method]["security"]
        assert {"KeystoneToken": []} in security and {"ImageKey": []} not in security


@pytest.mark.asyncio
async def test_image_key_reads_own_quota_view_through_real_dependency(hub_images):
    h = hub_images
    await publish(h, b"hsqs-quota-view", human(ALICE, ALPHA), visibility="private", name="quota")
    reader = await bearer(h, ALICE, ALPHA, ["images:read"])
    publisher = await bearer(h, ALICE, ALPHA, ["images:publish"])
    by_key = await h.client.get("/v1/image-quota/me", headers=reader)
    assert by_key.status_code == 200, by_key.text
    by_token = await h.client.get("/v1/image-quota/me", headers=human(ALICE, ALPHA))
    assert by_token.status_code == 200 and by_key.json() == by_token.json()
    assert (await h.client.get("/v1/image-quota/me", headers=publisher)).status_code == 403
    assert (await h.client.get("/v1/image-quota/me")).status_code == 401
    assert (await h.client.get("/v1/image-quota/me", headers={"Authorization": "Bearer junk"})).status_code == 401


@pytest.fixture
async def keystone_images(keystone_http, tmp_path, monkeypatch):  # noqa: F811
    """Real keystoneauth/keystoneclient requests against the synthetic Keystone fixture."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'keystone-images.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    values = SimpleNamespace(
        palimpsest_hub_max_blob_bytes=1024 * 1024,
        palimpsest_hub_anonymous_rate_limit_enabled=False,
    )
    for module in (hub, image_keys_service):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub, "get_blob_store", lambda: store)
    for module in (published_images_api, public_images):
        monkeypatch.setattr(module, "get_settings", lambda: values)
    app = FastAPI()
    app.include_router(published_images_api.router, prefix="/v1")
    app.include_router(image_keys_api.router, prefix="/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keystone.test") as client:
        yield SimpleNamespace(client=client, factory=factory)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("methods", [["openid"], ["openid", "token"]])
async def test_federated_openid_token_uses_scope_project_for_image_routes_and_keys(
    keystone_images,
    keystone_http,  # noqa: F811
    methods,
):
    keystone_http["token_methods"] = methods
    h = keystone_images
    original = {"X-Auth-Token": ORIGINAL_SUBJECT}
    assert (await h.client.get("/v1/published-images", headers=original)).status_code == 200
    mismatch = await h.client.get("/v1/published-images", headers={**original, "X-Project-Id": PROJECT_B})
    assert mismatch.status_code == 403
    issued = await h.client.post(
        "/v1/image-keys", headers=original, json={"name": "federated", "actions": ["images:read", "images:publish"]}
    )
    assert issued.status_code == 201, issued.text
    key = issued.json()["key"]
    assert (key["project_id"], key["owner_user_id"]) == (PROJECT_A, FEDERATED_OWNER)
    headers = {"Authorization": "Bearer " + issued.json()["secret"]}
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 200
    # Removing the owner from the project stops the already issued key.
    keystone_http["project_roles"] = []
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 403
    keystone_http["project_roles"] = ["member"]
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 200
    keystone_http["user_enabled"] = False
    assert (await h.client.get("/v1/published-images", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_unscoped_keystone_token_is_rejected_for_images_and_keys(keystone_images, keystone_http):  # noqa: F811
    keystone_http["token_methods"] = ["openid"]
    keystone_http["project_id"] = ""
    h = keystone_images
    original = {"X-Auth-Token": ORIGINAL_SUBJECT}
    assert (await h.client.get("/v1/published-images", headers=original)).status_code == 401
    created = await h.client.post("/v1/image-keys", headers=original, json={"name": "x", "actions": ["images:read"]})
    assert created.status_code == 401
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestImageKey))).all() == []
