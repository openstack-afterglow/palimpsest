"""OCI source closure publication: coherence, scoped storage, membership downloads and the source cap.

Runs real HTTP routes against SQLite and a real CAS store; identity is replaced only at
the Keystone boundary by the shared ``hub_images`` fixture.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
import test_public_images
from palimpsest_local.image_manifest import manifest_from_result
from sqlalchemy import func, select
from test_public_images import ALICE, AMY, BOB, PROJECT_A, PROJECT_B, digest, human, stage

from palimpsest_hub import auth
from palimpsest_hub.image_models import (
    PalimpsestHubImageMembership,
    PalimpsestHubImageSourceBlob,
    PalimpsestHubLayerNode,
    PalimpsestHubPublishedImage,
)
from palimpsest_hub.models import PalimpsestHubLayer, RuntimeBlobReference
from palimpsest_hub.services import image_quota
from palimpsest_hub.services import published_images as service
from palimpsest_hub.services.worker_leases import sql_mutex

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
RUNTIME = {"entrypoint": ["/bin/sh"], "cmd": [], "env": [], "working_dir": "/", "user": "0"}
# Reuse the shared HTTP/SQLite/CAS fixture with Keystone-boundary identity only.
hub_images = test_public_images.hub_images


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def oci_source(tag, *, layers=2, root=True, prefix=b""):
    """A real-shaped OCI chain: index -> manifest -> config + compressed layers."""
    compressed = [prefix + f"gzip-{tag}-{index}".encode() * (index + 3) for index in range(layers)]
    diff_ids = [digest(b"diff-" + item) for item in compressed]
    config = _canonical({"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": diff_ids}})
    manifest = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": digest(config),
                "size": len(config),
            },
            "layers": [{"mediaType": OCI_LAYER, "digest": digest(item), "size": len(item)} for item in compressed],
        }
    )
    index = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [{"mediaType": OCI_MANIFEST, "digest": digest(manifest), "size": len(manifest)}],
        }
    )
    blobs = {"config": config, "manifest": manifest, "layers": compressed, "root": index if root else None}
    items = [
        {"role": "manifest", "digest": digest(manifest), "size_bytes": len(manifest), "ordinal": 0},
        {"role": "config", "digest": digest(config), "size_bytes": len(config), "ordinal": 0},
        *[
            {"role": "layer", "digest": digest(item), "size_bytes": len(item), "ordinal": ordinal}
            for ordinal, item in enumerate(compressed)
        ],
    ]
    if root:
        items.insert(0, {"role": "root", "digest": digest(index), "size_bytes": len(index), "ordinal": 0})
    payloads = [f"hsqs-{tag}-{index}".encode() for index in range(layers)]
    image = manifest_from_result(
        {
            "layers": [
                {
                    "digest": digest(payload),
                    "size_bytes": len(payload),
                    "source": {
                        "oci_layer": {
                            "ordinal": ordinal,
                            "compressed": {"mediaType": OCI_LAYER, "digest": digest(item), "size": len(item)},
                            "diff_id": digest(b"diff-" + item),
                        }
                    },
                }
                for ordinal, (payload, item) in enumerate(zip(payloads, compressed, strict=True))
            ],
            "source": {
                "manifest_digest": digest(manifest),
                "config_digest": digest(config),
                "root_digest": digest(index) if root else digest(manifest),
                "diff_ids": diff_ids,
            },
            "runtime": RUNTIME,
        },
        os_family="alpine",
        os_version="3",
        source_kind="local",
        metadata={"tag": tag},
    )
    return {"manifest": image, "items": items, "blobs": blobs, "payloads": payloads}


def every_blob(source):
    values = [source["blobs"]["config"], source["blobs"]["manifest"], *source["blobs"]["layers"], *source["payloads"]]
    return [*values, source["blobs"]["root"]] if source["blobs"]["root"] is not None else values


async def stage_all(h, blobs, headers):
    for blob in blobs:
        response = await stage(h, blob, headers)
        assert response.status_code == 200, response.text


async def publish_source(h, source, headers, *, visibility="private", name="image", items=None, request_id=None):
    return await h.client.post(
        "/v1/published-images",
        headers=headers,
        json={
            "request_id": request_id or str(uuid4()),
            "name": name,
            "manifest": source["manifest"],
            "visibility": visibility,
            "source_closure": source["items"] if items is None else items,
        },
    )


async def set_policy(h, visibility, **limits):
    values = {"max_images": None, "max_chain_bytes": None, "max_source_bytes": None, **limits}
    async with sql_mutex(h.factory, service.IMAGE_MUTEX) as session:
        await image_quota.update_policy(session, "free", visibility, values, "admin")
        await session.commit()


async def counts(h):
    async with h.factory() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (PalimpsestHubPublishedImage, PalimpsestHubImageSourceBlob, PalimpsestHubImageMembership)
        }


ALICE_H, AMY_H, BOB_H = human(ALICE, PROJECT_A), human(AMY, PROJECT_A), human(BOB, PROJECT_B)


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["private", "internal", "public"])
async def test_coherent_closure_registers_projects_and_downloads_byte_exact(hub_images, visibility):
    h = hub_images
    source = oci_source(visibility)
    await stage_all(h, every_blob(source), ALICE_H)
    response = await publish_source(h, source, ALICE_H, visibility=visibility)
    assert response.status_code == 201, response.text
    image = response.json()
    assert image["source_closure"] == source["items"]
    readers = {"private": [ALICE_H], "internal": [ALICE_H, AMY_H], "public": [ALICE_H, AMY_H, BOB_H, {}]}[visibility]
    expected = {item["digest"]: blob for item, blob in zip(source["items"], [b for b in _ordered(source)], strict=True)}
    for headers in readers:
        detail = await h.client.get("/v1/published-images/" + image["id"], headers=headers)
        assert detail.status_code == 200, (visibility, headers)
        assert detail.json()["source_closure"] == source["items"]
        assert "path" not in json.dumps(detail.json()["source_closure"])
        for blob_digest, blob in expected.items():
            download = await h.client.get(f"/v1/published-images/{image['id']}/blobs/{blob_digest}", headers=headers)
            assert download.status_code == 200, (visibility, headers, blob_digest)
            assert download.content == blob
            assert download.headers["content-type"].startswith("application/octet-stream")
            assert download.headers["x-content-type-options"] == "nosniff"
        payload = await h.client.get(
            f"/v1/published-images/{image['id']}/blobs/{digest(source['payloads'][0])}", headers=headers
        )
        assert payload.status_code == 200 and payload.content == source["payloads"][0]
        assert payload.headers["x-content-type-options"] == "nosniff"
    async with h.factory() as session:
        references = set(
            (
                await session.scalars(
                    select(RuntimeBlobReference.digest).where(
                        RuntimeBlobReference.resource_kind == "published-image",
                        RuntimeBlobReference.resource_id == image["id"],
                    )
                )
            ).all()
        )
    # Closure blobs are retained exactly like payloads.
    assert {item["digest"] for item in source["items"]} | {digest(item) for item in source["payloads"]} == references


def _ordered(source):
    blobs = source["blobs"]
    root = [blobs["root"]] if blobs["root"] is not None else []
    return [*root, blobs["manifest"], blobs["config"], *blobs["layers"]]


@pytest.mark.asyncio
async def test_non_member_digest_is_refused_even_when_present(hub_images):
    h = hub_images
    source, other = oci_source("member"), oci_source("other")
    await stage_all(h, every_blob(source) + every_blob(other), ALICE_H)
    image = (await publish_source(h, source, ALICE_H, visibility="public")).json()
    stray = digest(other["blobs"]["layers"][0])  # config bytes are shared by both; layers are not
    response = await h.client.get(f"/v1/published-images/{image['id']}/blobs/{stray}")
    assert (response.status_code, response.json()["detail"]) == (404, "publication payload not found")


def _mutate(source, case):
    items = [dict(item) for item in source["items"]]
    layers = [item for item in items if item["role"] == "layer"]
    if case == "config-digest":
        config = next(item for item in items if item["role"] == "config")
        config.update(digest=layers[0]["digest"], size_bytes=layers[0]["size_bytes"])
    elif case == "missing-layer":
        items.remove(layers[-1])
    elif case == "wrong-order":
        layers[0]["ordinal"], layers[1]["ordinal"] = 1, 0
    elif case == "wrong-size":
        layers[0]["size_bytes"] += 1
    elif case == "extra-blob":
        items.append({**layers[0], "ordinal": len(layers)})
    elif case == "duplicate-slot":
        items.append(dict(layers[0]))
    elif case == "two-manifests":
        items.append({**next(item for item in items if item["role"] == "manifest"), "ordinal": 1})
    return items


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["config-digest", "missing-layer", "wrong-order", "wrong-size", "extra-blob", "duplicate-slot", "two-manifests"],
)
async def test_incoherent_closure_declarations_are_rejected(hub_images, case):
    h = hub_images
    source = oci_source(case)
    await stage_all(h, every_blob(source), ALICE_H)
    response = await publish_source(h, source, ALICE_H, items=_mutate(source, case))
    assert response.status_code == 422, (case, response.text)
    assert response.json()["detail"].startswith("image source closure is incoherent")
    assert (await counts(h))["palimpsest_hub_published_images"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "manifest-not-json",
        "root-without-manifest",
        "source-field",
        "oci-layer",
        "no-manifest-digest",
        "no-config-digest",
        "no-root-digest",
        "no-oci-layer",
    ],
)
async def test_incoherent_closure_documents_are_rejected(hub_images, case):
    h = hub_images
    source = oci_source(case)
    items = [dict(item) for item in source["items"]]
    if case == "manifest-not-json":
        junk = b"\x00not json at all"
        manifest = next(item for item in items if item["role"] == "manifest")
        manifest.update(digest=digest(junk), size_bytes=len(junk))
        source["blobs"]["extra"] = junk
    elif case == "root-without-manifest":
        index = _canonical(
            {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [{"digest": digest(b"x"), "size": 1}]}
        )
        root = next(item for item in items if item["role"] == "root")
        root.update(digest=digest(index), size_bytes=len(index))
        source["blobs"]["extra"] = index
    elif case == "source-field":
        source["manifest"] = {
            **source["manifest"],
            "source": {**source["manifest"]["source"], "config_digest": digest(b"x")},
        }
    elif case == "oci-layer":
        layers = source["manifest"]["layers"]
        swapped = [dict(layer) for layer in layers]
        swapped[0]["source"] = {
            **swapped[0]["source"],
            "oci_layer": {**swapped[0]["source"]["oci_layer"], "ordinal": 1},
        }
        source["manifest"] = {
            **source["manifest"],
            "layers": [{key: layer[key] for key in ("digest", "size_bytes", "source")} for layer in swapped],
        }
    elif case in ("no-manifest-digest", "no-config-digest", "no-root-digest"):
        key = case.removeprefix("no-").replace("-", "_")
        source["manifest"] = {
            **source["manifest"],
            "source": {name: value for name, value in source["manifest"]["source"].items() if name != key},
        }
    elif case == "no-oci-layer":
        layers = [
            {key: layer[key] for key in ("digest", "size_bytes", "source")} for layer in source["manifest"]["layers"]
        ]
        layers[1] = {key: layers[1][key] for key in ("digest", "size_bytes")}
        source["manifest"] = {**source["manifest"], "layers": layers}
    blobs = every_blob(source) + ([source["blobs"]["extra"]] if "extra" in source["blobs"] else [])
    await stage_all(h, blobs, ALICE_H)
    response = await publish_source(h, source, ALICE_H, items=items)
    assert response.status_code == 422, (case, response.text)
    assert response.json()["detail"].startswith("image source closure is incoherent")
    assert (await counts(h))["palimpsest_hub_published_images"] == 0


@pytest.mark.asyncio
async def test_a_trusted_manifest_cannot_be_republished_with_a_different_closure(hub_images):
    h = hub_images
    trusted = oci_source("trusted")
    await stage_all(h, every_blob(trusted), ALICE_H)
    original = await publish_source(h, trusted, ALICE_H, visibility="public", name="trusted")
    assert original.status_code == 201
    # Bob holds the same SquashFS payloads but pairs the identical manifest with his own coherent OCI chain.
    swapped = oci_source("attacker")
    await stage_all(h, [*every_blob(swapped), *trusted["payloads"]], BOB_H)
    forged = {**swapped, "manifest": trusted["manifest"]}
    response = await publish_source(h, forged, BOB_H, visibility="public", name="trusted")
    assert response.status_code == 422
    assert response.json()["detail"].startswith("image source closure is incoherent")
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubPublishedImage))).all()
    assert [row.id for row in rows] == [original.json()["id"]]


@pytest.mark.asyncio
async def test_application_images_cannot_carry_an_oci_closure(hub_images):
    h = hub_images
    source = oci_source("application")
    payload = b"hsqs-application"
    source["manifest"] = manifest_from_result(
        {"layers": [{"digest": digest(payload), "size_bytes": len(payload)}], "source": {}, "runtime": RUNTIME},
        os_family="alpine",
        os_version="3",
        source_kind="local",
        mode="application",
        base_image_digest=digest(b"cloud-image"),
    )
    await stage_all(h, [*every_blob(source), payload], ALICE_H)
    response = await publish_source(h, source, ALICE_H)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_private_closure_is_invisible_and_identical_bytes_grant_nothing(hub_images):
    h = hub_images
    source = oci_source("secret")
    await stage_all(h, every_blob(source), ALICE_H)
    image = (await publish_source(h, source, ALICE_H)).json()
    config = digest(source["blobs"]["config"])
    # Bob independently holds and stages identical bytes; that grants no read of Alice's image.
    await stage_all(h, [source["blobs"]["config"]], BOB_H)
    for headers in (AMY_H, BOB_H, {}):
        detail = await h.client.get("/v1/published-images/" + image["id"], headers=headers)
        blob = await h.client.get(f"/v1/published-images/{image['id']}/blobs/{config}", headers=headers)
        assert detail.status_code == blob.status_code == 404
        assert blob.json()["detail"] == "published image not found"
    # Bob cannot publish a closure over blobs he never staged, even though the bytes exist.
    bob_source = oci_source("secret")
    response = await publish_source(h, bob_source, BOB_H)
    assert (response.status_code, response.json()["detail"]) == (404, "publication payload not found")
    # Once he stages every byte himself (possession), his own publication works and is separate.
    await stage_all(h, every_blob(bob_source), BOB_H)
    own = await publish_source(h, bob_source, BOB_H)
    assert own.status_code == 201, own.text
    assert (await h.client.get("/v1/published-images/" + image["id"], headers=BOB_H)).status_code == 404


@pytest.mark.asyncio
async def test_source_cap_boundary_is_exact_and_separate_from_the_chain_cap(hub_images):
    h = hub_images
    source = oci_source("cap")
    await stage_all(h, every_blob(source), ALICE_H)
    total = sum({item["digest"]: item["size_bytes"] for item in source["items"]}.values())
    await set_policy(h, "private", max_source_bytes=total - 1)
    over = await publish_source(h, source, ALICE_H)
    assert over.status_code == 413
    assert over.json()["detail"] == {
        "code": "image_source_size_limit",
        "visibility": "private",
        "limit_bytes": total - 1,
        "source_bytes": total,
    }
    # The chain cap is independent: tiny chain cap gives the chain code, not the source code.
    await set_policy(h, "private", max_chain_bytes=1, max_source_bytes=total)
    chain = await publish_source(h, source, ALICE_H)
    assert (chain.status_code, chain.json()["detail"]["code"]) == (413, "image_chain_size_limit")
    await set_policy(h, "private", max_source_bytes=total)
    exact = await publish_source(h, source, ALICE_H)
    assert exact.status_code == 201, exact.text


@pytest.mark.asyncio
async def test_source_cap_counts_each_distinct_digest_once(hub_images):
    h = hub_images
    source = oci_source("repeat", layers=2)
    # An OCI manifest may list the same compressed layer twice.
    layers = source["blobs"]["layers"]
    layers[1] = layers[0]
    rebuilt = oci_source("repeat", layers=2)
    manifest = json.loads(rebuilt["blobs"]["manifest"])
    manifest["layers"][1] = manifest["layers"][0]
    manifest_bytes = _canonical(manifest)
    index = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [{"digest": digest(manifest_bytes), "size": len(manifest_bytes)}],
        }
    )
    items = [dict(item) for item in rebuilt["items"]]
    for item in items:
        if item["role"] == "manifest":
            item.update(digest=digest(manifest_bytes), size_bytes=len(manifest_bytes))
        if item["role"] == "root":
            item.update(digest=digest(index), size_bytes=len(index))
        if item["role"] == "layer" and item["ordinal"] == 1:
            item.update(digest=digest(layers[0]), size_bytes=len(layers[0]))
    image_layers = [dict(layer) for layer in rebuilt["manifest"]["layers"]]
    image_layers[1]["source"] = {
        "oci_layer": {
            **image_layers[1]["source"]["oci_layer"],
            "compressed": dict(image_layers[0]["source"]["oci_layer"]["compressed"]),
        }
    }
    source["manifest"] = {
        **rebuilt["manifest"],
        "source": {
            **rebuilt["manifest"]["source"],
            "manifest_digest": digest(manifest_bytes),
            "root_digest": digest(index),
        },
        "layers": [{key: layer[key] for key in ("digest", "size_bytes", "source")} for layer in image_layers],
    }
    await stage_all(h, [manifest_bytes, index, rebuilt["blobs"]["config"], layers[0], *rebuilt["payloads"]], ALICE_H)
    distinct = sum({item["digest"]: item["size_bytes"] for item in items}.values())
    assert distinct < sum(item["size_bytes"] for item in items)
    await set_policy(h, "private", max_source_bytes=distinct)
    response = await publish_source(h, source, ALICE_H, items=items)
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_admin_policy_put_sets_max_source_bytes(hub_images):
    h = hub_images
    h.app.dependency_overrides[auth.require_admin] = lambda: {"user_id": "admin", "project_id": PROJECT_A}
    try:
        response = await h.client.put(
            "/v1/admin/image-quota/policies/free/internal",
            json={"max_images": 9, "max_chain_bytes": 1000, "max_source_bytes": 777},
        )
        assert response.status_code == 200, response.text
        assert response.json()["max_source_bytes"] == 777
        partial = await h.client.put(
            "/v1/admin/image-quota/policies/free/internal", json={"max_images": 9, "max_chain_bytes": 1000}
        )
        assert partial.status_code == 422
        listed = (await h.client.get("/v1/admin/image-quota/policies")).json()["items"]
        internal = next(item for item in listed if (item["tier"], item["visibility"]) == ("free", "internal"))
        assert internal["max_source_bytes"] == 777
        seed = next(item for item in listed if (item["tier"], item["visibility"]) == ("paid", "public"))
        assert seed["max_source_bytes"] == seed["max_chain_bytes"] == 100 * 2**30
    finally:
        h.app.dependency_overrides.pop(auth.require_admin, None)


@pytest.mark.asyncio
async def test_failed_closure_storage_rolls_back_every_row(hub_images):
    h = hub_images
    source = oci_source("rollback")
    await stage_all(h, every_blob(source), ALICE_H)
    # The compressed layer bytes vanish from the CAS after staging (payload rows already flushed).
    h.store.blob_path(digest(source["blobs"]["layers"][-1])).unlink()
    response = await publish_source(h, source, ALICE_H)
    assert (response.status_code, response.json()["detail"]) == (409, "publication payload unavailable or changed")
    assert await counts(h) == {
        "palimpsest_hub_published_images": 0,
        "palimpsest_hub_image_source_blobs": 0,
        "palimpsest_hub_image_memberships": 0,
    }
    async with h.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PalimpsestHubLayerNode)) == 0
        assert (
            await session.scalar(
                select(func.count()).select_from(PalimpsestHubLayer).where(PalimpsestHubLayer.kind == "image-payload")
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RuntimeBlobReference)
                .where(RuntimeBlobReference.resource_kind == "published-image")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_request_replay_binds_the_closure(hub_images):
    h = hub_images
    source = oci_source("replay")
    await stage_all(h, every_blob(source), ALICE_H)
    request_id = str(uuid4())
    first = await publish_source(h, source, ALICE_H, request_id=request_id)
    assert first.status_code == 201
    again = await publish_source(h, source, ALICE_H, request_id=request_id)
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"]
    without = await h.client.post(
        "/v1/published-images",
        headers=ALICE_H,
        json={"request_id": request_id, "name": "image", "manifest": source["manifest"], "visibility": "private"},
    )
    assert without.status_code == 409


@pytest.mark.asyncio
async def test_extension_of_a_closure_image_accepts_no_closure_and_stays_valid(hub_images):
    h = hub_images
    source = oci_source("extend")
    await stage_all(h, every_blob(source), ALICE_H)
    base = (await publish_source(h, source, ALICE_H, name="base")).json()
    child = b"hsqs-extension-child"
    await stage_all(h, [child], ALICE_H)
    extension = await h.client.post(
        "/v1/published-images/extensions",
        headers=ALICE_H,
        json={
            "request_id": str(uuid4()),
            "name": "child",
            "base_image_id": base["id"],
            "layers": [{"digest": digest(child), "size_bytes": len(child)}],
        },
    )
    assert extension.status_code == 201, extension.text
    assert extension.json()["source_closure"] == []
    assert (await h.client.get("/v1/published-images/" + base["id"], headers=ALICE_H)).json()["source_closure"] == (
        source["items"]
    )
    rejected = await h.client.post(
        "/v1/published-images/extensions",
        headers=ALICE_H,
        json={
            "request_id": str(uuid4()),
            "name": "child2",
            "base_image_id": base["id"],
            "layers": [{"digest": digest(child), "size_bytes": len(child)}],
            "source_closure": source["items"],
        },
    )
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_non_squashfs_blob_listed_as_layer_payload_is_rejected_without_a_layer_row(hub_images):
    h = hub_images
    opaque = b"\x1f\x8bthis is gzip, not squashfs"
    await stage_all(h, [opaque], ALICE_H)  # staging is possession proof only
    manifest = manifest_from_result(
        {"layers": [{"digest": digest(opaque), "size_bytes": len(opaque)}], "source": {}, "runtime": RUNTIME},
        os_family="alpine",
        os_version="3",
        source_kind="local",
    )
    response = await h.client.post(
        "/v1/published-images",
        headers=ALICE_H,
        json={"request_id": str(uuid4()), "name": "opaque", "manifest": manifest, "visibility": "private"},
    )
    assert (response.status_code, response.json()["detail"]) == (422, "publication payload is not SquashFS")
    async with h.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PalimpsestHubLayer)
                .where(PalimpsestHubLayer.blob_digest == digest(opaque))
            )
            == 0
        )
    assert (await counts(h))["palimpsest_hub_published_images"] == 0


@pytest.mark.asyncio
async def test_squashfs_looking_closure_blob_is_judged_only_by_closure_rules(hub_images):
    h = hub_images
    # Compressed layer bytes that happen to start with the SquashFS magic are still just closure bytes.
    source = oci_source("magic", prefix=b"hsqs")
    await stage_all(h, every_blob(source), ALICE_H)
    wrong = _mutate(source, "wrong-order")
    rejected = await publish_source(h, source, ALICE_H, items=wrong)
    assert rejected.status_code == 422 and rejected.json()["detail"].startswith("image source closure is incoherent")
    accepted = await publish_source(h, source, ALICE_H)
    assert accepted.status_code == 201, accepted.text
    async with h.factory() as session:
        # Closure blobs never become layer rows, SquashFS-looking or not.
        layer = digest(source["blobs"]["layers"][0])
        assert (
            await session.scalar(
                select(func.count()).select_from(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == layer)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_opaque_staging_still_enforces_authentication_media_digest_and_size(hub_images):
    h = hub_images
    blob = b'{"schemaVersion": 2}'
    path = "/v1/published-images/blobs/" + digest(blob)
    base = {"Content-Type": "application/octet-stream", "Content-Length": str(len(blob))}
    assert (await h.client.put(path, content=blob, headers=base)).status_code == 401
    wrong_type = await h.client.put(path, content=blob, headers={**ALICE_H, **base, "Content-Type": "text/plain"})
    assert wrong_type.status_code == 415
    other = "/v1/published-images/blobs/" + digest(b"something else")
    mismatch = await h.client.put(other, content=blob, headers={**ALICE_H, **base})
    assert (mismatch.status_code, mismatch.json()["detail"]) == (422, "content digest mismatch")
    short = await h.client.put(path, content=blob, headers={**ALICE_H, **base, "Content-Length": str(len(blob) + 5)})
    assert short.status_code in (413, 422)
    accepted = await h.client.put(path, content=blob, headers={**ALICE_H, **base})
    assert accepted.status_code == 200 and accepted.json() == {"digest": digest(blob), "size_bytes": len(blob)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    ["3-then-2", "2-then-3"],
    ids=["manifest-schema3-then-2", "manifest-schema2-then-3"],
)
async def test_closure_manifest_rejects_duplicate_schema_version_keys_in_both_orders(hub_images, order):
    h = hub_images
    source = oci_source(f"dup-{order}")
    items = [dict(item) for item in source["items"]]
    # Build raw JSON with duplicate keys by hand (json.dumps cannot emit duplicate keys).
    first, second = (3, 2) if order == "3-then-2" else (2, 3)
    config_item = next(item for item in items if item["role"] == "config")
    layer_items = [item for item in items if item["role"] == "layer"]
    raw_manifest = (
        f'{{"schemaVersion":{first},"schemaVersion":{second},"mediaType":"{OCI_MANIFEST}",'
        f'"config":{{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"{config_item["digest"]}","size":{config_item["size_bytes"]}}},'
        f'"layers":['
        + ",".join(
            f'{{"mediaType":"{OCI_LAYER}","digest":"{lyr["digest"]}","size":{lyr["size_bytes"]}}}'
            for lyr in layer_items
        )
        + "]}"
    ).encode()
    manifest_item = next(item for item in items if item["role"] == "manifest")
    manifest_item.update(digest=digest(raw_manifest), size_bytes=len(raw_manifest))
    source["blobs"]["manifest"] = raw_manifest
    # Update root index to point to this new raw manifest digest and size.
    raw_index = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [{"mediaType": OCI_MANIFEST, "digest": digest(raw_manifest), "size": len(raw_manifest)}],
        }
    )
    root_item = next(item for item in items if item["role"] == "root")
    root_item.update(digest=digest(raw_index), size_bytes=len(raw_index))
    source["blobs"]["root"] = raw_index
    source["manifest"] = {
        **source["manifest"],
        "source": {
            **source["manifest"]["source"],
            "manifest_digest": digest(raw_manifest),
            "root_digest": digest(raw_index),
        },
    }
    await stage_all(h, every_blob(source), ALICE_H)
    response = await publish_source(h, source, ALICE_H, items=items)
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("image source closure is incoherent")


@pytest.mark.asyncio
async def test_closure_documents_reject_duplicate_keys_in_config_and_index(hub_images):
    h = hub_images
    # Duplicate key in config:
    source = oci_source("dup-config")
    items = [dict(item) for item in source["items"]]
    diff_ids = [digest(b"diff-" + item) for item in source["blobs"]["layers"]]
    diff_json = json.dumps(diff_ids)
    raw_config = (
        f'{{"architecture":"arm64","architecture":"amd64","os":"linux","rootfs":{{"type":"layers","diff_ids":{diff_json}}}}}'
    ).encode()
    config_item = next(item for item in items if item["role"] == "config")
    config_item.update(digest=digest(raw_config), size_bytes=len(raw_config))
    source["blobs"]["config"] = raw_config
    # Update manifest to point to this new config digest and size.
    layer_items = [item for item in items if item["role"] == "layer"]
    raw_manifest = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": digest(raw_config),
                "size": len(raw_config),
            },
            "layers": [
                {"mediaType": OCI_LAYER, "digest": lyr["digest"], "size": lyr["size_bytes"]} for lyr in layer_items
            ],
        }
    )
    manifest_item = next(item for item in items if item["role"] == "manifest")
    manifest_item.update(digest=digest(raw_manifest), size_bytes=len(raw_manifest))
    source["blobs"]["manifest"] = raw_manifest
    raw_index = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [{"mediaType": OCI_MANIFEST, "digest": digest(raw_manifest), "size": len(raw_manifest)}],
        }
    )
    root_item = next(item for item in items if item["role"] == "root")
    root_item.update(digest=digest(raw_index), size_bytes=len(raw_index))
    source["blobs"]["root"] = raw_index
    source["manifest"] = {
        **source["manifest"],
        "source": {
            **source["manifest"]["source"],
            "config_digest": digest(raw_config),
            "manifest_digest": digest(raw_manifest),
            "root_digest": digest(raw_index),
        },
    }
    await stage_all(h, every_blob(source), ALICE_H)
    response = await publish_source(h, source, ALICE_H, items=items)
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("image source closure is incoherent")


@pytest.mark.asyncio
async def test_config_diff_ids_matching_and_rejections(hub_images):
    h = hub_images
    source = oci_source("diff-mismatch")
    items = [dict(item) for item in source["items"]]
    # 1. Config rootfs.diff_ids differs from manifest source.diff_ids -> 422
    bad_diffs = [digest(b"wrong-diff-1"), digest(b"wrong-diff-2")]
    raw_config = _canonical(
        {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": bad_diffs}}
    )
    config_item = next(item for item in items if item["role"] == "config")
    config_item.update(digest=digest(raw_config), size_bytes=len(raw_config))
    source["blobs"]["config"] = raw_config
    layer_items = [item for item in items if item["role"] == "layer"]
    raw_manifest = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": digest(raw_config),
                "size": len(raw_config),
            },
            "layers": [
                {"mediaType": OCI_LAYER, "digest": lyr["digest"], "size": lyr["size_bytes"]} for lyr in layer_items
            ],
        }
    )
    manifest_item = next(item for item in items if item["role"] == "manifest")
    manifest_item.update(digest=digest(raw_manifest), size_bytes=len(raw_manifest))
    source["blobs"]["manifest"] = raw_manifest
    raw_index = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [{"mediaType": OCI_MANIFEST, "digest": digest(raw_manifest), "size": len(raw_manifest)}],
        }
    )
    root_item = next(item for item in items if item["role"] == "root")
    root_item.update(digest=digest(raw_index), size_bytes=len(raw_index))
    source["blobs"]["root"] = raw_index
    source["manifest"] = {
        **source["manifest"],
        "source": {
            **source["manifest"]["source"],
            "config_digest": digest(raw_config),
            "manifest_digest": digest(raw_manifest),
            "root_digest": digest(raw_index),
        },
    }
    await stage_all(h, every_blob(source), ALICE_H)
    response = await publish_source(h, source, ALICE_H, items=items)
    assert response.status_code == 422
    assert "rootfs.diff_ids differs" in response.json()["detail"]

    # 2. Manifest source has no diff_ids -> accepted!
    no_diff_source = oci_source("no-diffs")
    manifest_dict = dict(no_diff_source["manifest"])
    source_block = {k: v for k, v in manifest_dict["source"].items() if k != "diff_ids"}
    manifest_dict["source"] = source_block
    no_diff_source["manifest"] = manifest_dict
    await stage_all(h, every_blob(no_diff_source), ALICE_H)
    accepted = await publish_source(h, no_diff_source, ALICE_H, name="no-diff-img")
    assert accepted.status_code == 201, accepted.text


@pytest.mark.asyncio
async def test_config_blob_must_be_valid_json_object(hub_images):
    h = hub_images
    # Config is not JSON at all:
    source = oci_source("config-not-json")
    items = [dict(item) for item in source["items"]]
    not_json = b"not-json-binary-data"
    config_item = next(item for item in items if item["role"] == "config")
    config_item.update(digest=digest(not_json), size_bytes=len(not_json))
    source["blobs"]["config"] = not_json
    layer_items = [item for item in items if item["role"] == "layer"]
    raw_manifest = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": digest(not_json),
                "size": len(not_json),
            },
            "layers": [
                {"mediaType": OCI_LAYER, "digest": lyr["digest"], "size": lyr["size_bytes"]} for lyr in layer_items
            ],
        }
    )
    manifest_item = next(item for item in items if item["role"] == "manifest")
    manifest_item.update(digest=digest(raw_manifest), size_bytes=len(raw_manifest))
    source["blobs"]["manifest"] = raw_manifest
    raw_index = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [{"mediaType": OCI_MANIFEST, "digest": digest(raw_manifest), "size": len(raw_manifest)}],
        }
    )
    root_item = next(item for item in items if item["role"] == "root")
    root_item.update(digest=digest(raw_index), size_bytes=len(raw_index))
    source["blobs"]["root"] = raw_index
    source["manifest"] = {
        **source["manifest"],
        "source": {
            **source["manifest"]["source"],
            "config_digest": digest(not_json),
            "manifest_digest": digest(raw_manifest),
            "root_digest": digest(raw_index),
        },
    }
    await stage_all(h, every_blob(source), ALICE_H)
    response = await publish_source(h, source, ALICE_H, items=items)
    assert response.status_code == 422
    assert "config document is not JSON" in response.json()["detail"]

    # Config is JSON but not an object (e.g. array):
    source2 = oci_source("config-array")
    items2 = [dict(item) for item in source2["items"]]
    array_json = b"[1, 2, 3]"
    config_item2 = next(item for item in items2 if item["role"] == "config")
    config_item2.update(digest=digest(array_json), size_bytes=len(array_json))
    source2["blobs"]["config"] = array_json
    layer_items2 = [item for item in items2 if item["role"] == "layer"]
    raw_manifest2 = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": digest(array_json),
                "size": len(array_json),
            },
            "layers": [
                {"mediaType": OCI_LAYER, "digest": lyr["digest"], "size": lyr["size_bytes"]} for lyr in layer_items2
            ],
        }
    )
    manifest_item2 = next(item for item in items2 if item["role"] == "manifest")
    manifest_item2.update(digest=digest(raw_manifest2), size_bytes=len(raw_manifest2))
    source2["blobs"]["manifest"] = raw_manifest2
    raw_index2 = _canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [{"mediaType": OCI_MANIFEST, "digest": digest(raw_manifest2), "size": len(raw_manifest2)}],
        }
    )
    root_item2 = next(item for item in items2 if item["role"] == "root")
    root_item2.update(digest=digest(raw_index2), size_bytes=len(raw_index2))
    source2["blobs"]["root"] = raw_index2
    source2["manifest"] = {
        **source2["manifest"],
        "source": {
            **source2["manifest"]["source"],
            "config_digest": digest(array_json),
            "manifest_digest": digest(raw_manifest2),
            "root_digest": digest(raw_index2),
        },
    }
    await stage_all(h, every_blob(source2), ALICE_H)
    response2 = await publish_source(h, source2, ALICE_H, items=items2)
    assert response2.status_code == 422
    assert "config document must be a JSON object" in response2.json()["detail"]
