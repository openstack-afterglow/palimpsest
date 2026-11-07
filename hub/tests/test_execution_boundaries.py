"""HTTP confidentiality and finite operator bounds for API execution requests."""

from __future__ import annotations

import ast
import base64
import contextlib
import json
import time
import urllib.request
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from palimpsest_hub.api.hub import _legacy_writer
from palimpsest_hub.api.published_images import image_writer
from palimpsest_hub.auth import get_os_conn
from palimpsest_hub.config import BuildWorkerSettings, Settings, configured_image_source_policy
from palimpsest_hub.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,payload",
    [
        ("/v1/instances", {"command": [{"private-input": "never-reflect-execution-secret"}]}),
        ("/v1/dockerfile-builds", {"dockerfile": {"private-input": "never-reflect-execution-secret"}}),
        ("/v1/published-images", {"manifest": {"private-input": "never-reflect-execution-secret"}}),
        ("/v1/native-instances", {"command": [{"private-input": "never-reflect-execution-secret"}]}),
    ],
)
async def test_invalid_execution_inputs_are_not_reflected_or_cached(path, payload):
    async def actor():
        return {"project_id": "test-project", "user_id": "test-user", "package_actions": {"packages:write"}}

    async def connection():
        yield SimpleNamespace()

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[_legacy_writer] = actor
    app.dependency_overrides[image_writer] = actor
    app.dependency_overrides[get_os_conn] = connection
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid"
        ) as client:
            response = await client.post(path, json=payload)
        assert response.status_code == 422
        assert "never-reflect-execution-secret" not in response.text
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize(
    "hosts",
    [
        (),
        ("https://ghcr.io",),
        ("ghcr.io/path",),
        ("user@ghcr.io",),
        ("ghcr.io", "ghcr.io"),
        ("GHCR.IO",),
        ("ghcr.io:0",),
    ],
)
def test_worker_registry_policy_rejects_urls_ambiguous_authorities_and_empty_lists(hosts):
    with pytest.raises(ValidationError):
        BuildWorkerSettings(
            palimpsest_hub_url="https://hub.example",
            palimpsest_hub_build_worker_key_file="/run/worker.key",
            palimpsest_hub_build_work_path="/var/lib/worker",
            palimpsest_hub_registry_hosts=hosts,
        )


@pytest.fixture(params=[Settings, BuildWorkerSettings])
def source_settings(request):
    if request.param is Settings:
        values = {
            "database_url": "sqlite+aiosqlite:///fixture.sqlite",
            "redis_url": "redis://localhost/0",
            "palimpsest_hub_local_path": "/var/lib/hub",
            "os_auth_url": "https://identity.invalid/v3",
            "os_username": "fixture",
            "os_password": "fixture",
            "os_project_name": "fixture",
        }
    else:
        values = {
            "palimpsest_hub_url": "https://hub.invalid",
            "palimpsest_hub_build_worker_key_file": "/run/worker.key",
            "palimpsest_hub_build_work_path": "/var/lib/worker",
        }
    return lambda **overrides: request.param(**{**values, **overrides})


def test_controller_and_worker_default_source_policy_denies_allowed_host(source_settings, monkeypatch):
    from palimpsest_local.image_source_policy import ImageSourcePolicyError
    from palimpsest_local.registry_intake import resolve_anonymous_reference

    monkeypatch.delenv("PALIMPSEST_HUB_IMAGE_SOURCE_POLICY", raising=False)
    policy = configured_image_source_policy(source_settings())
    assert policy.rules == ()
    with pytest.raises(ImageSourcePolicyError, match="not allowed"):
        policy.admit(resolve_anonymous_reference("docker.io/library/alpine:3"))


@pytest.mark.parametrize(
    "rule",
    [
        {"registry": "docker.io", "namespace": "library", "repository": "*"},
        {"registry": "docker.io", "namespace": "*", "repository": "alpine"},
        {"registry": "docker.io", "repository": "library/alpine"},
        {"registry": "unselected.invalid", "namespace": "library", "repository": "alpine"},
    ],
)
def test_controller_and_worker_reject_nonexact_or_host_unselected_rules(source_settings, rule):
    with pytest.raises(ValidationError):
        source_settings(
            palimpsest_hub_image_source_policy={
                "schema_version": 1,
                "publisher_policy": "registry-namespace",
                "rules": [rule],
            }
        )


def test_controller_and_worker_source_setting_uses_canonical_shared_engine(source_settings, monkeypatch):
    from palimpsest_local.image_source_policy import ImageSourcePolicyError
    from palimpsest_local.registry_intake import resolve_anonymous_reference

    selected = {
        "schema_version": 1,
        "publisher_policy": "registry-namespace",
        "rules": [{"registry": "docker.io", "namespace": "library", "repository": "alpine"}],
    }
    monkeypatch.setenv("PALIMPSEST_HUB_IMAGE_SOURCE_POLICY", json.dumps(selected))
    policy = configured_image_source_policy(source_settings())
    allowed = policy.admit(resolve_anonymous_reference("docker.io/library/alpine:3"))
    assert allowed.to_dict()["publisher_policy"] == "registry-namespace"
    assert allowed.policy_digest == policy.digest
    with pytest.raises(ImageSourcePolicyError, match="not allowed"):
        policy.admit(resolve_anonymous_reference("docker.io/library/ubuntu:24.04"))


@pytest.mark.parametrize("output", [b"\x00" * 8192, ("\x01雪😀\n" * 8192).encode(), b"\xff" * 8192])
def test_generated_guest_reporter_bounds_actual_json_envelope_and_retries_identical_observation(tmp_path, output):
    from palimpsest_hub.api.instances import InstanceResult
    from palimpsest_hub.services.instance_bootstrap import render_bootstrap

    capability = "sensitive-bootstrap-capability-value-123456789"
    spec = {
        "instance_id": "11111111-2222-4333-8444-555555555555",
        "capability": capability,
        "origin": "https://hub.example.test",
        "expires_at": int(time.time()) + 300,
    }
    cloud = json.loads(render_bootstrap(spec).split("\n", 1)[1])
    runner = next(file for file in cloud["write_files"] if file["path"].endswith("runner.py"))
    tree = ast.parse(base64.b64decode(runner["content"]).decode())
    # Execute the actual generated reporter functions without any guest workload,
    # mounts or network. Captured HTTP bytes, not source spelling, are the proof.
    reporter = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("bounded", "report")],
        type_ignores=[],
    )
    (tmp_path / "stdout").write_bytes(capability.encode() + output)
    (tmp_path / "stderr").write_bytes(output)
    requests = []

    def deliver(request, timeout):
        requests.append(request)
        if len(requests) < 3:
            raise TimeoutError("retry only the captured observation")
        return contextlib.nullcontext(SimpleNamespace(read=lambda count: b""))

    namespace = {
        "ROOT": tmp_path,
        "spec": spec,
        "deadline": spec["expires_at"],
        "headers": {"Authorization": "Bearer " + capability},
        "process": SimpleNamespace(poll=lambda: 0),
        "json": json,
        "time": SimpleNamespace(time=time.time, sleep=lambda seconds: None),
        "urllib": SimpleNamespace(request=SimpleNamespace(Request=urllib.request.Request, urlopen=deliver)),
    }
    exec(compile(reporter, "generated-instance-reporter.py", "exec"), namespace)
    namespace["report"]("ready")
    assert len(requests) == 3
    assert requests[0] is requests[1] is requests[2]
    body = requests[0].data
    assert len(body) <= 70000 and capability.encode() not in body
    result = InstanceResult.model_validate_json(body)
    assert result.status == "ready" and result.exit_code == 0
    assert "[redacted bootstrap capability]" in result.stdout
    assert result.stdout and result.stderr
    assert len(result.stdout) <= 8192 and len(result.stderr) <= 8192


@pytest.mark.parametrize(
    "change",
    [
        {"root": "relative/native"},
        {"grant_path": "/srv/native/../shared/grant.json"},
        {"root": "/srv/shared"},
        {"root": "/srv/shared/private"},
        {"grant_path": "/srv/shared/grant.json"},
    ],
)
def test_native_profile_cannot_place_private_state_in_consumer_payload_storage(change):
    from palimpsest_hub.config import NativeProfile

    values = {
        "environment": "sdk-proof",
        "root": "/srv/native",
        "grant_path": "/srv/native/grant.json",
        "project_id": "a" * 32,
        "user_id": "b" * 32,
        "manifest_digest": "sha256:" + "c" * 64,
        "share_id": "11111111-2222-4333-8444-555555555555",
        "export": "192.0.2.1:6789:/volumes/approved",
        "publication_root": "/srv/shared",
        **change,
    }
    with pytest.raises(ValidationError):
        NativeProfile.model_validate(values)
