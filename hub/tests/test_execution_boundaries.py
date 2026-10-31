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
from palimpsest_hub.auth import get_os_conn
from palimpsest_hub.config import BuildWorkerSettings
from palimpsest_hub.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,payload",
    [
        ("/v1/instances", {"command": [{"private-input": "never-reflect-execution-secret"}]}),
        ("/v1/dockerfile-builds", {"dockerfile": {"private-input": "never-reflect-execution-secret"}}),
    ],
)
async def test_invalid_execution_inputs_are_not_reflected_or_cached(path, payload):
    async def actor():
        return {"project_id": "test-project", "user_id": "test-user", "package_actions": {"packages:write"}}

    async def connection():
        yield SimpleNamespace()

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[_legacy_writer] = actor
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
