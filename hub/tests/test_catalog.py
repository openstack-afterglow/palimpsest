"""Actual native catalog routes and SQL, isolated from operational state and CAS."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from catalog_fixture import digest, isolated_catalog, layer

from palimpsest_hub.api import hub as hub_api
from palimpsest_hub.api import packages
from palimpsest_hub.models import (
    PackageKey,
    PackageNamespace,
    PackageTag,
    PackageVersion,
    PalimpsestImageExport,
    RegistryPackage,
)
from palimpsest_hub.services import catalog as native_catalog
from palimpsest_hub.services import package_registry as registry


@pytest.fixture
async def catalog(tmp_path):
    async with (
        isolated_catalog(tmp_path, include_packages=True) as fixture,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url="http://fixture") as client,
    ):
        fixture.client = client
        yield fixture


async def get(catalog, path, token="member", **params):
    return await catalog.client.get(path, headers={"X-Auth-Token": token}, params=params)


async def test_visible_named_leaves_do_not_disclose_private_children_or_ancestry(catalog, monkeypatch):
    def no_cas():
        raise AssertionError("Read-only catalog must not access CAS")

    monkeypatch.setattr(hub_api, "get_blob_store", no_cas)
    response = await get(catalog, "/v1/catalog/assets")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    items = {item["name"]: item for item in response.json()["items"]}
    assert set(items) == {"torch-leaf", "shared-tensorflow", "site-bundle", "project-bundle", "fake-build"}
    assert items["torch-leaf"]["layer_count"] == 2
    assert items["torch-leaf"]["size_bytes"] == 6  # two layers plus the included base content
    assert items["torch-leaf"]["local_supported"] and items["torch-leaf"]["local_reason"] is None
    assert not items["project-bundle"]["local_supported"]
    assert items["project-bundle"]["local_reason"] == "base-image-required"
    assert items["project-bundle"]["chain_complete"]
    assert items["shared-tensorflow"]["visibility"] == "shared"
    assert items["site-bundle"]["visibility"] == "site"
    assert items["project-bundle"]["visibility"] == "project"
    assert all(item["chain_complete"] and item["download_allowed"] for item in items.values())
    assert "Project-B" not in response.text
    assert "private-child" not in response.text
    assert "private-root" not in response.text
    assert "config_json" not in response.text
    assert "parent_digest" not in response.text
    assert (await get(catalog, "/v1/catalog/assets", q="torch-root")).json()["items"] == []
    reader = (await get(catalog, "/v1/catalog/assets", token="reader")).json()["items"]
    assert reader and all(not item["download_allowed"] for item in reader)


async def test_real_consumer_inventory_gate_denies_missing_capability(catalog):
    response = await get(catalog, "/v1/catalog/assets", token="no-inventory")
    assert response.status_code == 403
    assert "torch-leaf" not in response.text and "private-root" not in response.text


async def test_real_admin_consumer_bypass_reports_existing_download_authority(catalog):
    response = await get(catalog, "/v1/catalog/assets", token="human-admin")
    assert response.status_code == 200
    items = response.json()["items"]
    assert items and all(item["download_allowed"] for item in items)
    assert "private-root" not in response.text and "private-child" not in response.text
    download = await get(catalog, f"/v1/layers/{digest(2)}/blob", token="human-admin")
    assert download.status_code == 200


async def test_real_consumer_download_gate_denies_inventory_only_reader(catalog):
    response = await get(catalog, f"/v1/layers/{digest(2)}/blob", token="reader")
    assert response.status_code == 403


@pytest.mark.parametrize("root_base", [None, 0])
@pytest.mark.parametrize("leaf_base", [0, 4, 999])
async def test_catalog_complete_graph_rejects_descendant_only_or_conflicting_base(catalog, root_base, leaf_base):
    from sqlalchemy import select

    from palimpsest_hub.models import PalimpsestHubLayer

    async with catalog.factory() as session:
        root = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(1)))
        leaf = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(2)))
        root.config_json = {"base_image_digest": digest(root_base)} if root_base is not None else {}
        leaf.config_json = {"base_image_digest": digest(leaf_base)}
        await session.commit()
    complete = root_base == leaf_base
    response = await get(catalog, "/v1/catalog/assets", q="torch-leaf")
    assert response.status_code == 200
    assert bool(response.json()["items"]) is complete
    if complete:
        assert response.json()["items"][0]["local_supported"]
        assert response.json()["items"][0]["base_image_digest"] == digest(root_base)
    assert "private-root" not in response.text
    admin = await get(catalog, f"/v1/admin/catalog/layers/{digest(2)}", token="human-admin")
    assert admin.status_code == 200
    assert admin.json()["chain_complete"] is complete


async def test_assets_search_pagination_and_input_bounds(catalog):
    response = await get(catalog, "/v1/catalog/assets", q="TORCH")
    assert [row["name"] for row in response.json()["items"]] == ["torch-leaf"]
    assert (await get(catalog, "/v1/catalog/assets", q="%")).json()["items"] == []
    first = (await get(catalog, "/v1/catalog/assets", limit=2)).json()
    assert first["has_more"] and first["next_offset"] == 2
    pages = [first]
    for _ in range(8):
        next_offset = pages[-1]["next_offset"]
        if next_offset is None:
            break
        pages.append((await get(catalog, "/v1/catalog/assets", limit=2, offset=next_offset)).json())
    assert len({item["id"] for page in pages for item in page["items"]}) == 5
    assert pages[-1]["next_offset"] is None and not pages[-1]["has_more"]
    assert all(len(page["items"]) <= 2 for page in pages)
    for params in ({"q": "x" * 129}, {"offset": -1}, {"limit": 101}):
        assert (await get(catalog, "/v1/catalog/assets", **params)).status_code == 422


async def test_admin_inventory_is_global_with_real_ancestry_and_unknown_origin(catalog):
    response = await get(catalog, "/v1/admin/catalog/layers", token="human-admin", q="PRIVATE")
    assert response.status_code == 200
    assert {row["name"] for row in response.json()["items"]} == {"private-child", "private-root"}
    selected = await get(catalog, f"/v1/admin/catalog/layers/{digest(3)}", token="human-admin")
    assert selected.status_code == 200
    item = selected.json()
    assert [row["id"] for row in item["ancestors"]] == [digest(1), digest(2)]
    assert item["parent_id"] == digest(2) and item["project_id"] == "Project-B"
    assert item["visibility"] == "private" and item["chain_complete"]
    assert item["ancestors"][1]["origin"] == {
        "kind": "build",
        "source": "hub-build",
        "evidence": "recorded",
        "build_id": "00000000-0000-0000-0000-000000000001",
        "job_id": "00000000-0000-0000-0000-000000000001",
        "revision": None,
    }
    assert item["origin"]["kind"] == "unknown"
    forged = (await get(catalog, f"/v1/admin/catalog/layers/{digest(12)}", token="human-admin")).json()
    assert forged["origin"]["kind"] == "unknown"
    assert "caller-forged" not in str(forged)
    assert "private recipe" not in selected.text
    for number in (9, 10, 11):
        broken = (await get(catalog, f"/v1/admin/catalog/layers/{digest(number)}", token="human-admin")).json()
        assert not broken["chain_complete"]
        assert digest(number) not in [row["id"] for row in broken["ancestors"]]
    parent = (await get(catalog, f"/v1/admin/catalog/layers/{digest(2)}", token="human-admin")).json()
    assert [row["id"] for row in parent["children"]] == [digest(3)]
    assert not parent["children_has_more"] and parent["bundles"] == []


async def test_admin_image_export_uses_durable_job_link_only(catalog):
    async with catalog.factory() as session:
        session.add(
            PalimpsestImageExport(
                id="00000000-0000-0000-0000-000000000002",
                project_id="Project-B",
                source_image_id="image-id",
                source_name="private-image-name",
                source_disk_format="qcow2",
                source_size_bytes=7,
                source_fingerprint="a" * 64,
                artifact_key="b" * 64,
                target_disk_format="qcow2",
                result_blob_digest=digest(6),
                result_size_bytes=7,
                status="complete",
            )
        )
        await session.commit()
    item = (await get(catalog, f"/v1/admin/catalog/layers/{digest(6)}", token="human-admin")).json()
    assert item["origin"]["kind"] == "image-export"
    assert item["origin"]["source"] == "glance" and item["origin"]["evidence"] == "recorded"
    assert "private-image-name" not in str(item)
    consumer = await get(catalog, "/v1/catalog/assets", q="tensorflow")
    assert "job_id" not in consumer.text


async def test_admin_children_are_direct_and_bounded(catalog):
    async with catalog.factory() as session:
        session.add_all(layer(n, f"child-{n}", parent=8) for n in range(100, 151))
        await session.commit()
    item = (await get(catalog, f"/v1/admin/catalog/layers/{digest(8)}", token="human-admin")).json()
    assert len(item["children"]) == 50 and item["children_has_more"]
    assert all(row["parent_id"] == digest(8) for row in item["children"])


@pytest.mark.parametrize("token", ["member", "service-admin", None])
async def test_admin_catalog_denies_nonhuman_and_project_authority(catalog, token):
    for path in ("/v1/admin/catalog/layers", f"/v1/admin/catalog/layers/{digest(4)}"):
        response = await get(catalog, path, token=token or "missing")
        assert response.status_code == (401 if token is None else 403)
        assert "private-root" not in response.text


@pytest.mark.parametrize("fence", ["revoked", "deny_policy"])
async def test_admin_catalog_revalidates_every_request(catalog, fence):
    assert (await get(catalog, "/v1/admin/catalog/layers", token="human-admin")).status_code == 200
    setattr(catalog, fence, True)
    assert (await get(catalog, "/v1/admin/catalog/layers", token="human-admin")).status_code == 403
    assert (await get(catalog, f"/v1/admin/catalog/layers/{digest(4)}", token="human-admin")).status_code == 403


async def test_unavailable_sql_is_not_an_empty_catalog(catalog, monkeypatch):
    monkeypatch.setattr(hub_api, "get_session_factory", lambda: None)
    assert (await get(catalog, "/v1/catalog/assets")).status_code == 503
    assert (await get(catalog, "/v1/admin/catalog/layers", token="human-admin")).status_code == 503


async def test_private_package_query_tag_escape_cursor_and_scope(catalog):
    async with catalog.factory() as session:
        session.add_all(
            PackageNamespace(project_id=project, namespace=namespace, project_name=project)
            for project, namespace in (("Project-A", "alpha"), ("Project-B", "beta"))
        )
        await session.flush()
        for number, project in ((1, "Project-A"), (2, "Project-B")):
            session.add(
                PackageKey(
                    id=f"{number:032x}",
                    project_id=project,
                    owner_user_id="member",
                    secret_hash="a" * 64,
                    name="catalog-fixture",
                    scope={"all_packages": True},
                    actions=["packages:inventory"],
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        await session.flush()
        for number, name, project, tag in (
            (1, "torch-one", "Project-A", "latest"),
            (2, "torch-two", "Project-A", "v2"),
            (3, "other", "Project-A", "Torch-CUDA"),
            (4, "foreign-torch", "Project-B", "torch"),
            (5, "wild", "Project-A", "v_1%\\pin"),
        ):
            package_id = f"{number:032x}"
            session.add(RegistryPackage(id=package_id, project_id=project, name=name, package_type="oci-image"))
            await session.flush()
            session.add(
                PackageVersion(
                    package_id=package_id,
                    root_digest=digest(number),
                    root_media_type="application/vnd.oci.image.manifest.v1+json",
                    graph={},
                    platforms=[],
                    archive_digest=digest(900),
                    archive_size_bytes=1,
                    total_bytes=1,
                    provenance={},
                    pushed_by="member",
                    pushed_key_id=f"{2 if project == 'Project-B' else 1:032x}",
                )
            )
            await session.flush()
            session.add(PackageTag(package_id=package_id, tag=tag, root_digest=digest(number), updated_by="member"))
        await session.commit()
    actor = registry.Actor(project_id="Project-A", user_id="member", namespace="alpha", actions=("packages:inventory",))
    catalog.app.dependency_overrides[packages.read_actor] = lambda: actor
    first = (await get(catalog, "/v1/projects/alpha/packages", q="TORCH", limit=2)).json()
    assert [row["name"] for row in first["items"]] == ["other", "torch-one"]
    cursor = first["next_cursor"]
    second = (await get(catalog, "/v1/projects/alpha/packages", q="torch", limit=2, cursor=cursor)).json()
    assert [row["name"] for row in second["items"]] == ["torch-two"]
    for params in ({"q": "other", "cursor": cursor}, {"cursor": cursor}, {"q": "x" * 129}):
        assert (await get(catalog, "/v1/projects/alpha/packages", **params)).status_code == 422
    literal = (await get(catalog, "/v1/projects/alpha/packages", q="_1%\\")).json()
    assert [row["name"] for row in literal["items"]] == ["wild"]
    legacy_cursor = registry.make_cursor(registry.cursor_binding(actor, "packages", None), "other")
    compatible = await get(catalog, "/v1/projects/alpha/packages", cursor=legacy_cursor)
    assert compatible.status_code == 200
    assert [row["name"] for row in compatible.json()["items"]] == ["torch-one", "torch-two", "wild"]
    actor = replace(actor, key_id="a" * 32, scope={"packages": ["torch-two"]})
    scoped = (await get(catalog, "/v1/projects/alpha/packages", q="torch")).json()
    assert [row["name"] for row in scoped["items"]] == ["torch-two"]
    assert (await get(catalog, "/v1/projects/beta/packages", q="torch")).status_code == 403
    typed = (await get(catalog, "/v1/projects/alpha/packages", q="torch", package_type="runtime-bundle")).json()
    assert typed["items"] == []
    actor = replace(actor, actions=())
    assert (await get(catalog, "/v1/projects/alpha/packages", q="torch")).status_code == 403


async def test_declared_base_must_be_visible_cloud_image(catalog):
    public_base = layer(50, "base", published=True)
    public_base.kind = "cloud-image"
    public_base.disk_format = "qcow2"
    public_base.media_type = "application/x-qemu-disk"
    private_base = layer(52, "secret-base", project="Project-B")
    private_base.kind = "cloud-image"
    private_base.disk_format = "qcow2"
    private_base.media_type = "application/x-qemu-disk"
    async with catalog.factory() as session:
        session.add_all(
            [
                public_base,
                private_base,
                layer(51, "base-visible", published=True, config={"base_image_digest": digest(50)}),
                layer(53, "base-private", published=True, config={"base_image_digest": digest(52)}),
                layer(54, "base-missing", published=True, config={"base_image_digest": digest(500)}),
                layer(55, "base-not-image", published=True, config={"base_image_digest": digest(1)}),
            ]
        )
        await session.commit()
    response = await get(catalog, "/v1/catalog/assets", q="base-")
    assert response.status_code == 200
    assert [row["name"] for row in response.json()["items"]] == ["base-visible"]
    assert response.json()["items"][0]["base_image_digest"] == digest(50)
    assert digest(52) not in response.text and "secret-base" not in response.text
    admin = (await get(catalog, f"/v1/admin/catalog/layers/{digest(54)}", token="human-admin")).json()
    assert not admin["chain_complete"]


async def test_candidate_pagination_resumes_after_empty_invalid_pages_without_rescanning(catalog, monkeypatch):
    async with catalog.factory() as session:
        session.add_all(layer(1000 + n, f"bounded-invalid-{n:02}", published=True, parent=999999) for n in range(12))
        session.add(layer(1100, "bounded-valid", published=True))
        await session.commit()
    seen = []
    original_chain = native_catalog._Graph.chain

    async def observed_chain(graph, row):
        seen.append(row.name)
        return await original_chain(graph, row)

    monkeypatch.setattr(native_catalog._Graph, "chain", observed_chain)
    first = (await get(catalog, "/v1/catalog/assets", q="bounded-", limit=5)).json()
    assert first == {"items": [], "offset": 0, "has_more": True, "next_offset": 5}
    assert seen == [f"bounded-invalid-{n:02}" for n in range(5)]
    seen.clear()
    second = (await get(catalog, "/v1/catalog/assets", q="bounded-", limit=5, offset=5)).json()
    assert second == {"items": [], "offset": 5, "has_more": True, "next_offset": 10}
    assert seen == [f"bounded-invalid-{n:02}" for n in range(5, 10)]
    seen.clear()
    third = (await get(catalog, "/v1/catalog/assets", q="bounded-", limit=5, offset=10)).json()
    assert [item["name"] for item in third["items"]] == ["bounded-valid"]
    assert third["offset"] == 10 and not third["has_more"] and third["next_offset"] is None
    assert seen == ["bounded-invalid-10", "bounded-invalid-11", "bounded-valid"]


async def test_catalog_ancestry_depth_limit_fails_explicitly(catalog):
    maximum = native_catalog.MAX_DEPTH
    async with catalog.factory() as session:
        session.add_all(
            layer(2000 + n, f"long-chain-{n:03}", published=True, parent=1999 + n if n else None)
            for n in range(maximum + 2)
        )
        await session.commit()
    leaf_number = 2000 + maximum + 1
    leaf_name = f"long-chain-{maximum + 1:03}"
    for path, token, params in (
        ("/v1/catalog/assets", "member", {"q": leaf_name}),
        ("/v1/admin/catalog/layers", "human-admin", {"q": leaf_name}),
        (f"/v1/admin/catalog/layers/{digest(leaf_number)}", "human-admin", {}),
    ):
        response = await get(catalog, path, token=token, **params)
        assert response.status_code == 413
        assert response.json() == {"detail": "catalog ancestry exceeds depth limit"}


async def test_global_admin_visibility_is_independent_of_original_project(catalog, monkeypatch):
    original_validate = native_catalog._project_id
    monkeypatch.setattr(native_catalog, "_project_id", lambda info: "Project-A")
    with_project = (await get(catalog, "/v1/admin/catalog/layers", token="human-admin")).json()
    monkeypatch.setattr(native_catalog, "_project_id", lambda info: "Project-B")
    other_project = (await get(catalog, "/v1/admin/catalog/layers", token="human-admin")).json()
    assert with_project == other_project
    private = next(row for row in with_project["items"] if row["id"] == digest(8))
    shared = next(row for row in with_project["items"] if row["id"] == digest(6))
    assert private["visibility"] == "private" and shared["visibility"] == "shared"
    monkeypatch.setattr(native_catalog, "_project_id", original_validate)


@pytest.mark.parametrize(
    "base_arch,layer_arch,disk_format,reason",
    [
        ("x86_64", "x86_64", "qcow2", None),
        ("aarch64", "aarch64", "raw", None),
        (None, "x86_64", "qcow2", "architecture-unknown"),
        ("x86_64", None, "qcow2", "architecture-unknown"),
        ("x86_64", "aarch64", "qcow2", "architecture-mismatch"),
        ("x86_64", "x86_64", "vmdk", "base-image-format-unsupported"),
    ],
)
async def test_local_support_is_metadata_qualified_and_size_includes_base(
    catalog, base_arch, layer_arch, disk_format, reason
):
    base = layer(3000, "local-proof-base", published=True)
    base.kind = "cloud-image"
    base.disk_format = disk_format
    base.arch = base_arch
    leaf = layer(3001, "local-proof-leaf", published=True, config={"base_image_digest": digest(3000)})
    leaf.arch = layer_arch
    async with catalog.factory() as session:
        session.add_all([base, leaf])
        await session.commit()
    response = await get(catalog, "/v1/catalog/assets", q="local-proof-")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    item = response.json()["items"][0]
    assert item["chain_complete"]
    assert item["local_supported"] is (reason is None)
    assert item["local_reason"] == reason
    assert item["size_bytes"] == base.size_bytes + leaf.size_bytes
