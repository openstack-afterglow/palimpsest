"""Provider choices offered to a dashboard user: only what a create request could name."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from palimpsest_hub.api import hub, instances
from palimpsest_hub.auth import get_os_conn
from palimpsest_hub.services import instance_options

PROJECT = "p" * 32
OTHER = "o" * 32
USER = "u" * 32


def image(identity, **changes):
    return {
        "id": identity,
        "name": "image-" + identity,
        "status": "active",
        "architecture": "x86_64",
        "disk_format": "qcow2",
        "container_format": "bare",
        "size": 3 * 1024**3,
        "min_disk": 4,
        "min_ram": 512,
        "visibility": "public",
        "properties": {},
        **changes,
    }


class Provider:
    def __init__(self):
        self.compute = self.network = self.image = self
        self._afterglow_project_id = PROJECT
        self._afterglow_user_id = USER
        self.calls = []
        self.image_rows = [image("usable")]
        self.flavor_rows = [{"id": "small", "name": "cpu.small", "vcpus": 1, "ram": 1024, "disk": 0}]
        self.network_rows = [{"id": "net-own", "name": "own", "project_id": PROJECT}]
        self.keypair_rows = [{"name": "laptop"}]

    def images(self, **filters):
        self.calls.append(("images", filters))
        return iter(self.image_rows)

    def flavors(self, **filters):
        self.calls.append(("flavors", filters))
        return iter(self.flavor_rows)

    def networks(self, **filters):
        self.calls.append(("networks", filters))
        return iter(self.network_rows)

    def keypairs(self):
        self.calls.append(("keypairs", {}))
        return iter(self.keypair_rows)


def settings(base_images=None):
    return SimpleNamespace(
        palimpsest_hub_instance_base_images=base_images or {},
        palimpsest_hub_instance_max_boot_gib=256,
        palimpsest_hub_instance_max_writable_gib=64,
    )


def actor(project=PROJECT):
    return {"project_id": project, "user_id": USER, "package_actions": {"packages:write"}}


def test_only_images_that_a_create_request_could_boot_are_offered():
    provider = Provider()
    provider.image_rows = [
        image("usable"),
        image("raw", disk_format="raw"),
        image("amd64", architecture="amd64"),
        image("declared-in-properties", architecture=None, properties={"architecture": "x86_64"}),
        image("queued", status="queued"),
        image("arm", architecture="aarch64"),
        image("undeclared", architecture=None),
        image("windows", os_type="windows"),
        image("iso", disk_format="iso"),
        image("container", container_format="ova"),
        image("layer-store", properties={"palimpsest_artifact_type": "external-layer-store"}),
        image("qualified", architecture=None, os_hash_algo="sha512", os_hash_value="0"),
        image("qualified-other-bytes", architecture=None, os_hash_algo="sha512", os_hash_value="1"),
        image("from-hub", properties={"palimpsest_digest": "sha256:" + "a" * 64}),
    ]
    content = {"architecture": "x86_64", "os_type": "linux", "os_hash_algo": "sha512", "os_hash_value": "0"}
    qualified = {"qualified": content, "qualified-other-bytes": content}
    result = instance_options.choices(provider, actor(), settings(qualified))
    offered = {item["id"]: item for item in result["images"]}
    assert set(offered) == {"usable", "raw", "amd64", "declared-in-properties", "qualified", "from-hub"}
    assert offered["usable"] == {
        "id": "usable",
        "name": "image-usable",
        "architecture": "x86_64",
        "disk_format": "qcow2",
        "size_bytes": 3 * 1024**3,
        "min_disk_gib": 4,
        "min_ram_mib": 512,
        "visibility": "public",
        "base_image_digest": None,
    }
    assert offered["from-hub"]["base_image_digest"] == "sha256:" + "a" * 64


def test_a_network_of_another_project_is_never_offered_even_if_the_provider_returns_it():
    provider = Provider()
    provider.network_rows = [
        {"id": "net-own", "name": "own", "project_id": PROJECT},
        {"id": "net-shared", "name": "shared", "project_id": OTHER},
        {"id": "net-unowned", "name": "unowned"},
    ]
    result = instance_options.choices(provider, actor(), settings())
    assert result["networks"] == [{"id": "net-own", "name": "own"}]
    assert ("networks", {"project_id": PROJECT}) in provider.calls


def test_disabled_and_gpu_flavors_are_left_out_and_key_names_are_distinct():
    provider = Provider()
    provider.flavor_rows = [
        {
            "id": "big",
            "name": "cpu.big",
            "vcpus": 8,
            "ram": 16384,
            "disk": 0,
            "extra_specs": {"hw:cpu_policy": "shared"},
        },
        {"id": "off", "name": "cpu.off", "vcpus": 1, "ram": 512, "disk": 0, "is_disabled": True},
        {
            "id": "pci",
            "name": "gpu.pci",
            "vcpus": 4,
            "ram": 8192,
            "disk": 0,
            "extra_specs": {"pci_passthrough:alias": "a:1"},
        },
        {"id": "vgpu", "name": "gpu.v", "vcpus": 4, "ram": 8192, "disk": 0, "extra_specs": {"resources:VGPU": "1"}},
        {"id": "small", "name": "cpu.small", "vcpus": 1, "ram": 1024, "disk": 0},
    ]
    provider.keypair_rows = [{"name": "work"}, {"name": "laptop"}, {"name": "work"}, {"name": None}]
    result = instance_options.choices(provider, actor(), settings())
    assert [item["id"] for item in result["flavors"]] == ["small", "big"]
    assert result["flavors"][0] == {"id": "small", "name": "cpu.small", "vcpus": 1, "ram_mib": 1024, "disk_gib": 0}
    assert result["key_names"] == ["laptop", "work"]
    assert ("flavors", {"details": True, "is_public": True}) in provider.calls
    assert result["limits"] == {"max_boot_gib": 256, "max_writable_gib": 64}


def test_a_long_provider_list_is_cut_and_reported_as_cut():
    provider = Provider()
    provider.flavor_rows = [
        {"id": f"f{index:04d}", "name": "f", "vcpus": 1, "ram": 1024, "disk": 0}
        for index in range(instance_options.MAX_CHOICES + 1)
    ]
    result = instance_options.choices(provider, actor(), settings())
    assert len(result["flavors"]) == instance_options.MAX_CHOICES
    assert result["truncated"] == {"images": False, "flavors": True, "networks": False, "key_names": False}


def test_a_token_scoped_to_another_project_reads_nothing():
    provider = Provider()
    with pytest.raises(HTTPException) as caught:
        instance_options.choices(provider, actor(OTHER), settings())
    assert caught.value.status_code == 403
    assert provider.calls == []


def test_a_provider_failure_is_reported_without_its_text():
    provider = Provider()

    def broken(**filters):
        raise RuntimeError("https://nova.internal.example:8774 token=secret")

    provider.flavors = broken
    with pytest.raises(HTTPException) as caught:
        instance_options.choices(provider, actor(), settings())
    assert caught.value.status_code == 502
    assert caught.value.detail == "Provider choices are unavailable"


@pytest.mark.asyncio
async def test_the_route_needs_write_authority_and_is_never_cached(monkeypatch):
    provider = Provider()
    app = FastAPI()
    app.include_router(instances.router, prefix="/v1")

    async def writer(request: Request):
        if request.headers.get("X-Deny"):
            raise HTTPException(403, "Project write authority required")
        return actor()

    async def conn():
        yield provider

    app.dependency_overrides[hub._legacy_writer] = writer
    app.dependency_overrides[get_os_conn] = conn
    monkeypatch.setattr(instances, "get_settings", settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test") as client:
        denied = await client.get("/v1/instance-options", headers={"X-Deny": "1"})
        assert denied.status_code == 403 and provider.calls == []
        allowed = await client.get("/v1/instance-options")
        assert allowed.status_code == 200 and allowed.headers["cache-control"] == "no-store"
        assert allowed.json()["project_id"] == PROJECT
        assert [item["id"] for item in allowed.json()["images"]] == ["usable"]
        provider._afterglow_project_id = OTHER
        mismatch = await client.get("/v1/instance-options")
        assert mismatch.status_code == 403 and mismatch.headers["cache-control"] == "no-store"
