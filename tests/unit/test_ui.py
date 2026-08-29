from __future__ import annotations

import http.client
import json
import socket
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from palimpsest_local import state, ui
from palimpsest_local.runtime_types import (
    CapabilityCheck,
    ExistingRunRecord,
    RuntimeBackend,
    _issue_lifecycle_adapter_outcome,
)
from palimpsest_local.state import init_roots


def _setup_roots(tmp_path: Path) -> state.StatePaths:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return init_roots({"XDG_CONFIG_HOME": str(config_dir), "XDG_STATE_HOME": str(state_dir)})


def _write_ui_run_ledger(
    roots: state.StatePaths,
    *,
    backend: str,
    runtime_kind: str = "cloud-image",
) -> state.RunPaths:
    rpaths = state.run_paths(roots, "ui-vm")
    rpaths.root.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    state.atomic_write_json(
        rpaths.owner,
        {"schema_version": 1, "run_id": run_id, "name": "ui-vm"},
    )
    state.atomic_write_json(
        rpaths.state,
        {
            "schema_version": 2,
            "runtime_kind": runtime_kind,
            "backend": backend,
            "name": "ui-vm",
            "run_id": run_id,
            "status": "stopped",
        },
    )
    return rpaths


_UI_LIFECYCLE_REQUESTS = (
    ("start", "start", "POST", "/api/v1/vms/ui-vm/start", {}),
    ("stop", "stop", "POST", "/api/v1/vms/ui-vm/stop", {}),
    ("rm", "rm", "DELETE", "/api/v1/vms/ui-vm?volumes=true", {"volumes": True}),
)


@pytest.fixture(autouse=True)
def _stub_operation_capability_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ui.runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "test-present", True),
    )


@pytest.fixture
def server_env(tmp_path: Path):
    roots = _setup_roots(tmp_path)
    token = "secret-test-token-12345"

    # Create dummy server to assign an ephemeral port
    dummy_server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
    port = dummy_server.server_address[1]
    dummy_server.server_close()

    origin = f"http://127.0.0.1:{port}"
    handler_cls = ui.build_handler(roots, token=token, origin=origin)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    server.daemon_threads = True

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "roots": roots,
        "port": port,
        "token": token,
        "origin": origin,
        "server": server,
    }

    server.shutdown()
    server.server_close()


def _request(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | bytes | str | None = None,
) -> tuple[int, dict[str, str], Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    req_headers = headers.copy() if headers else {}

    raw_body: bytes | None = None
    if isinstance(body, dict):
        raw_body = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    elif isinstance(body, str):
        raw_body = body.encode("utf-8")
    elif isinstance(body, bytes):
        raw_body = body

    conn.request(method, path, body=raw_body, headers=req_headers)
    resp = conn.getresponse()
    resp_headers = dict(resp.getheaders())
    content = resp.read().decode("utf-8")

    json_data = None
    if "application/json" in resp_headers.get("Content-Type", ""):
        try:
            json_data = json.loads(content)
        except Exception:
            pass

    conn.close()
    return resp.status, resp_headers, json_data or content


def test_auth_semantics(server_env: dict[str, Any]):
    port = server_env["port"]
    token = server_env["token"]

    # 1. No token -> 401
    status, _, json_data = _request(port, "GET", "/api/v1/summary")
    assert status == 401
    assert json_data == {"error": "Unauthorized"}

    # 2. Invalid Bearer token -> 401
    status, _, json_data = _request(port, "GET", "/api/v1/summary", headers={"Authorization": "Bearer wrong-token"})
    assert status == 401

    # 3. Invalid query token -> 401
    status, _, json_data = _request(port, "GET", "/api/v1/summary?token=wrong-token")
    assert status == 401

    # 4. Valid query token on GET -> 200
    # 4. Valid query token on GET / -> 200 (query token allowed only for GET / and /index.html)
    status, resp_headers, content = _request(port, "GET", f"/?token={token}")
    assert status == 200
    assert "<!DOCTYPE html>" in content

    # 5. Query token on API route -> 401 (query token not allowed on API endpoints)
    status, _, json_data = _request(port, "GET", f"/api/v1/summary?token={token}")
    assert status == 401

    # 6. Valid Bearer token on GET -> 200
    status, _, json_data = _request(port, "GET", "/api/v1/summary", headers={"Authorization": f"Bearer {token}"})
    assert status == 200


def test_ui_vm_inventory_uses_safe_aggregation_projection(
    server_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots: state.StatePaths = server_env["roots"]
    rpaths = _write_ui_run_ledger(roots, backend="kvm")
    record = state.read_run_state(rpaths)
    state.atomic_write_json(
        rpaths.state,
        {
            **record,
            "base": {
                "digest": "sha256:" + "a" * 64,
                "arch": "x86_64",
                "local_path": "/private/SENSITIVE_VALUE/base.qcow2",
            },
            "layers": [
                {
                    "digest": "sha256:" + "b" * 64,
                    "target_dev": "vdb",
                    "local_path": "/private/SENSITIVE_VALUE/layer.squashfs",
                }
            ],
        },
    )
    monkeypatch.setattr(
        ui.inventory.runtime_dispatch.cloud_runtime,
        "reconcile_run",
        lambda *_a, **_k: {"state": {"guest_ip": "SENSITIVE_VALUE"}},
    )

    status, _headers, payload = _request(
        server_env["port"],
        "GET",
        "/api/v1/vms",
        headers={"Authorization": f"Bearer {server_env['token']}"},
    )

    assert status == 200
    assert [vm["name"] for vm in payload["vms"]] == ["ui-vm"]
    assert payload["vms"][0]["base_digest"] == "sha256:" + "a" * 64
    assert "SENSITIVE_VALUE" not in repr(payload)
    assert "local_path" not in repr(payload)


def test_csrf_origin_semantics(server_env: dict[str, Any]):
    port = server_env["port"]
    token = server_env["token"]
    origin = server_env["origin"]

    headers = {"Authorization": f"Bearer {token}"}

    # 1. POST with forbidden Origin -> 403
    bad_headers = headers | {"Origin": "http://evil.local"}
    status, _, json_data = _request(
        port, "POST", "/api/v1/storage/set", headers=bad_headers, body={"destination": "/tmp/test"}
    )
    assert status == 403
    assert json_data == {"error": "Forbidden: invalid Origin"}

    # 2. POST with forbidden Sec-Fetch-Site -> 403
    bad_headers2 = headers | {"Sec-Fetch-Site": "cross-site"}
    status, _, json_data = _request(
        port, "POST", "/api/v1/storage/set", headers=bad_headers2, body={"destination": "/tmp/test"}
    )
    assert status == 403
    assert json_data == {"error": "Forbidden: Sec-Fetch-Site must be same-origin"}

    # 3. POST with matching Origin -> 200 / 400 (not 403)
    good_headers = headers | {"Origin": origin}
    status, _, json_data = _request(
        port, "POST", "/api/v1/storage/set", headers=good_headers, body={"destination": "/tmp/test"}
    )
    assert status in (200, 400, 409)


@pytest.mark.parametrize(
    ("backend", "adapter_name", "expected_backend"),
    [
        ("kvm", "cloud_runtime", RuntimeBackend.KVM),
        ("libvirt-hvf", "cloud_runtime", RuntimeBackend.LIBVIRT_HVF),
        ("lima-vz", "lima", RuntimeBackend.LIMA_VZ),
    ],
)
@pytest.mark.parametrize(("operation", "target_name", "method", "path", "expected_kwargs"), _UI_LIFECYCLE_REQUESTS)
def test_ui_vm_lifecycle_routes_by_durable_dispatch_and_preserves_json_contract(
    server_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    adapter_name: str,
    expected_backend: RuntimeBackend,
    operation: str,
    target_name: str,
    method: str,
    path: str,
    expected_kwargs: dict[str, object],
) -> None:
    roots: state.StatePaths = server_env["roots"]
    _write_ui_run_ledger(roots, backend=backend)
    calls: list[tuple[str, dict[str, object]]] = []

    def selected(name: str, **kwargs: object) -> object:
        calls.append((name, kwargs))
        if operation == "logs":
            return iter(("one\n", "two\n", "three\n"))
        expected = kwargs["_expected_record"]
        assert isinstance(expected, ExistingRunRecord)
        expected_snapshot = kwargs["_expected_snapshot"]
        assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
        with state.locked_existing_run(roots, name, expected=expected, expected_snapshot=expected_snapshot) as mutation:
            terminal = "running" if operation == "start" else "removed" if operation == "rm" else "stopped"
            initial = mutation.initial_snapshot
            written = (
                mutation.mutable_state()
                if initial.state["status"] == terminal and operation != "rm"
                else mutation.write_state(terminal, mutation.mutable_state())
            )
            outcome = _issue_lifecycle_adapter_outcome(
                mutation.record,
                initial.state["status"],
                state.lifecycle_revision(initial),
                terminal,
                state.lifecycle_revision(written),
            )
            if operation == "rm":
                mutation.delete_run_tree()
            return outcome

    selected_adapter = getattr(ui.runtime_dispatch, adapter_name)
    other_adapter = ui.runtime_dispatch.lima if adapter_name == "cloud_runtime" else ui.runtime_dispatch.cloud_runtime
    monkeypatch.setattr(selected_adapter, target_name, selected)
    monkeypatch.setattr(
        other_adapter,
        target_name,
        lambda *_args, **_kwargs: pytest.fail("UI dispatcher selected the wrong runtime adapter"),
    )
    headers = {
        "Authorization": f"Bearer {server_env['token']}",
        "Origin": server_env["origin"],
    }

    status, response_headers, payload = _request(server_env["port"], method, path, headers=headers)

    assert status == 200
    assert "application/json" in response_headers["Content-Type"]
    if operation == "logs":
        assert payload == {"log": "two\nthree\n"}
    else:
        assert payload == {
            "name": "ui-vm",
            "run_id": payload["run_id"],
            "runtime_kind": "cloud-image",
            "backend": backend,
            "operation": operation,
            "previous_status": "stopped",
            "status": "running" if operation == "start" else "removed" if operation == "rm" else "stopped",
            "lifecycle_revision": 0 if operation == "stop" else 1,
            "warning_category": None,
            "fallback_used": False,
        }
    assert len(calls) == 1
    called_name, kwargs = calls[0]
    assert called_name == "ui-vm"
    expected_record = kwargs.pop("_expected_record")
    expected_snapshot = kwargs.pop("_expected_snapshot", None)
    assert isinstance(expected_record, ExistingRunRecord)
    assert expected_record.dispatch_key.backend is expected_backend
    if operation in {"start", "stop", "rm"}:
        assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
    assert kwargs == {"roots": roots, **expected_kwargs}


@pytest.mark.parametrize(("operation", "target_name", "method", "path", "_expected_kwargs"), _UI_LIFECYCLE_REQUESTS)
def test_ui_oci_vm_lifecycle_returns_typed_409_before_backend_side_effects(
    server_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    target_name: str,
    method: str,
    path: str,
    _expected_kwargs: dict[str, object],
) -> None:
    roots: state.StatePaths = server_env["roots"]
    rpaths = _write_ui_run_ledger(roots, backend="kvm", runtime_kind="oci-root")
    before = (
        rpaths.owner.read_bytes(),
        rpaths.state.read_bytes(),
        tuple(sorted(path.name for path in rpaths.root.iterdir())),
    )
    effects: list[str] = []

    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"UI backend side effect reached: {effect}")

    monkeypatch.setattr(
        ui.runtime_dispatch.cloud_runtime,
        target_name,
        lambda *_args, **_kwargs: forbidden("cloud"),
    )
    monkeypatch.setattr(
        ui.runtime_dispatch.lima,
        target_name,
        lambda *_args, **_kwargs: forbidden("lima"),
    )
    headers = {
        "Authorization": f"Bearer {server_env['token']}",
        "Origin": server_env["origin"],
    }

    status, _, payload = _request(server_env["port"], method, path, headers=headers)

    assert status == 409
    assert payload == {"error": f"runtime operation '{operation}' is unavailable for oci-root/kvm"}
    assert effects == []
    assert before == (
        rpaths.owner.read_bytes(),
        rpaths.state.read_bytes(),
        tuple(sorted(path.name for path in rpaths.root.iterdir())),
    )


@pytest.mark.parametrize(("_operation", "_target_name", "method", "path", "_expected_kwargs"), _UI_LIFECYCLE_REQUESTS)
def test_ui_corrupt_vm_ledger_fails_closed_as_409_without_value_reflection(
    server_env: dict[str, Any],
    _operation: str,
    _target_name: str,
    method: str,
    path: str,
    _expected_kwargs: dict[str, object],
) -> None:
    roots: state.StatePaths = server_env["roots"]
    rpaths = _write_ui_run_ledger(roots, backend="kvm")
    rpaths.state.write_text('{"schema_version":"sensitive-corrupt-value"}\n', encoding="utf-8")
    before = rpaths.state.read_bytes()
    headers = {
        "Authorization": f"Bearer {server_env['token']}",
        "Origin": server_env["origin"],
    }

    status, _, payload = _request(server_env["port"], method, path, headers=headers)

    assert status == 409
    assert payload == {"error": "invalid run state schema"}
    assert "sensitive-corrupt-value" not in json.dumps(payload)
    assert rpaths.state.read_bytes() == before


def test_static_asset_routes(server_env: dict[str, Any]):
    port = server_env["port"]
    token = server_env["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. GET / with query token returns 200 (bootstrap HTML)
    status, resp_headers, content = _request(port, "GET", f"/?token={token}")
    assert status == 200
    assert "text/html" in resp_headers.get("Content-Type", "")
    assert "<!DOCTYPE html>" in content
    assert "fetch('/app.css'" in content
    assert "fetch('/app.js'" in content

    # 2. Unauthenticated asset fetches return 401
    status, _, _ = _request(port, "GET", "/app.css")
    assert status == 401
    status, _, _ = _request(port, "GET", "/app.js")
    assert status == 401

    # 3. Asset fetches with query token return 401 (token query parameter only allowed on / and /index.html)
    status, _, _ = _request(port, "GET", f"/app.css?token={token}")
    assert status == 401
    status, _, _ = _request(port, "GET", f"/app.js?token={token}")
    assert status == 401

    # 4. Authenticated asset fetches with Bearer token return 200
    status, resp_headers, content = _request(port, "GET", "/app.js", headers=headers)
    assert status == 200
    assert "javascript" in resp_headers.get("Content-Type", "")

    status, resp_headers, content = _request(port, "GET", "/app.css", headers=headers)
    assert status == 200
    assert "text/css" in resp_headers.get("Content-Type", "")


def test_get_routes_and_not_found(server_env: dict[str, Any]):
    roots: state.StatePaths = server_env["roots"]
    port = server_env["port"]
    token = server_env["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Synthesize VM run ledger
    run_dir = roots.runs / "demo-vm"
    run_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        run_dir / "owner.json",
        {"schema_version": 1, "run_id": "11111111-1111-1111-1111-111111111111", "name": "demo-vm"},
    )
    state.atomic_write_json(
        run_dir / "state.json",
        {
            "name": "demo-vm",
            "run_id": "11111111-1111-1111-1111-111111111111",
            "backend": "kvm",
            "status": "running",
            "base": {"digest": "sha256:" + "a" * 64, "arch": "x86_64"},
            "created_at": "2026-08-24T00:00:00Z",
        },
    )
    (run_dir / "console.log").write_text("vm console line 1\nvm console line 2\n", encoding="utf-8")
    run_dir.chmod(0o700)
    (run_dir / "console.log").chmod(0o600)

    # Synthesize build record
    b_id = "b-000000000999"
    build_dir = roots.builds / b_id
    build_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        build_dir / "record.json",
        {
            "schema_version": 1,
            "build_id": b_id,
            "status": "success",
            "created_at": "2026-08-24T00:00:00Z",
            "finished_at": "2026-08-24T00:01:00Z",
        },
    )
    (build_dir / "console.log").write_text("build log step 1\n", encoding="utf-8")

    # Test GET /api/v1/summary
    status, _, summary = _request(port, "GET", "/api/v1/summary", headers=headers)
    assert status == 200
    assert "host" in summary
    assert "backends" in summary
    assert "storage" in summary

    # Test GET /api/v1/vms
    status, _, vms_data = _request(port, "GET", "/api/v1/vms", headers=headers)
    assert status == 200
    assert "vms" in vms_data
    assert any(vm["name"] == "demo-vm" for vm in vms_data["vms"])

    # Test GET /api/v1/vms/<name>
    status, _, vm_data = _request(port, "GET", "/api/v1/vms/demo-vm", headers=headers)
    assert status == 200
    assert vm_data["name"] == "demo-vm"

    # Test GET /api/v1/vms/<name>/logs
    status, _, log_data = _request(port, "GET", "/api/v1/vms/demo-vm/logs?tail=10", headers=headers)
    assert status == 200
    assert "vm console line 1" in log_data["log"]

    # Test GET /api/v1/store/artifacts
    status, _, artifacts = _request(port, "GET", "/api/v1/store/artifacts", headers=headers)
    assert status == 200
    assert "images" in artifacts
    assert "layers" in artifacts

    # Test GET /api/v1/builds
    status, _, builds = _request(port, "GET", "/api/v1/builds", headers=headers)
    assert status == 200
    assert isinstance(builds, list)
    assert any(b["build_id"] == b_id for b in builds)

    # Test GET /api/v1/builds/<id>
    status, _, build = _request(port, "GET", f"/api/v1/builds/{b_id}", headers=headers)
    assert status == 200
    assert build["build_id"] == b_id

    # Test GET /api/v1/builds/<id>/log
    status, _, build_log = _request(port, "GET", f"/api/v1/builds/{b_id}/log?tail=5", headers=headers)
    assert status == 200
    assert "build log step 1" in build_log["log"]

    # Test GET /api/v1/storage
    status, _, storage = _request(port, "GET", "/api/v1/storage", headers=headers)
    assert status == 200
    assert "state_root" in storage

    # Test GET unknown route -> 404
    status, _, err = _request(port, "GET", "/api/v1/unknown-route", headers=headers)
    assert status == 404
    assert err == {"error": "Not Found"}


def test_malformed_digest_400(server_env: dict[str, Any]):
    port = server_env["port"]
    token = server_env["token"]
    origin = server_env["origin"]
    headers = {"Authorization": f"Bearer {token}", "Origin": origin}

    status, _, json_data = _request(port, "DELETE", "/api/v1/store/artifacts/bad-digest-format", headers=headers)
    assert status == 400
    assert json_data == {"error": "invalid digest"}


def test_referenced_artifact_deletion_409(server_env: dict[str, Any]):
    roots: state.StatePaths = server_env["roots"]
    port = server_env["port"]
    token = server_env["token"]
    origin = server_env["origin"]
    headers = {"Authorization": f"Bearer {token}", "Origin": origin}

    base_digest = "sha256:" + "c" * 64

    # Create dummy artifact files in store
    blob_file = roots.store / "blobs" / "sha256" / ("c" * 64)
    blob_file.parent.mkdir(parents=True, exist_ok=True)
    blob_file.write_bytes(b"fake image bytes")

    meta_file = roots.store / "metadata" / f"{'c' * 64}.json"
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        meta_file,
        {
            "digest": base_digest,
            "kind": "cloud-image",
            "disk_format": "qcow2",
            "arch": "x86_64",
            "size_bytes": 16,
            "created_at": "2026-08-24T00:00:00Z",
        },
    )

    # Synthesize VM run ledger referencing base_digest
    run_dir = roots.runs / "active-vm"
    run_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(
        run_dir / "owner.json",
        {"schema_version": 1, "run_id": "22222222-2222-2222-2222-222222222222", "name": "active-vm"},
    )
    state.atomic_write_json(
        run_dir / "state.json",
        {
            "name": "active-vm",
            "run_id": "22222222-2222-2222-2222-222222222222",
            "backend": "kvm",
            "status": "running",
            "base": {"digest": base_digest, "arch": "x86_64"},
            "created_at": "2026-08-24T00:00:00Z",
        },
    )

    # Attempt deleting referenced artifact -> 409
    status, _, json_data = _request(port, "DELETE", f"/api/v1/store/artifacts/{base_digest}", headers=headers)
    assert status == 409
    assert "still used by: active-vm" in json_data["error"]


def test_malformed_and_traversal_build_id_400(server_env: dict[str, Any]):
    port = server_env["port"]
    token = server_env["token"]
    headers = {"Authorization": f"Bearer {token}"}

    for path in (
        "/api/v1/builds/invalid",
        "/api/v1/builds/../b-000000000999",
        "/api/v1/builds/bk-123",
        "/api/v1/builds/invalid/log",
        "/api/v1/builds/../b-000000000999/log",
    ):
        status, _, json_data = _request(port, "GET", path, headers=headers)
        assert status == 400
        assert json_data == {"error": "invalid build id"}


def test_negative_content_length_400(server_env: dict[str, Any]):
    port = server_env["port"]
    token = server_env["token"]
    origin = server_env["origin"]
    headers = {"Authorization": f"Bearer {token}", "Origin": origin, "Content-Length": "-1"}

    status, _, json_data = _request(port, "POST", "/api/v1/storage/set", headers=headers)
    assert status == 400
    assert json_data == {"error": "Invalid Content-Length"}


def test_truncated_body_400(server_env: dict[str, Any]):
    port = server_env["port"]
    token = server_env["token"]
    origin = server_env["origin"]

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", port))

    req = (
        f"POST /api/v1/storage/set HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Origin: {origin}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: 100\r\n\r\n"
        f'{{"short": 1}}'
    ).encode()

    sock.sendall(req)
    sock.shutdown(socket.SHUT_WR)

    resp_bytes = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp_bytes += chunk
    sock.close()

    resp_text = resp_bytes.decode("utf-8", errors="replace")
    assert "400 Bad Request" in resp_text or "Truncated request body" in resp_text


@pytest.mark.parametrize("keep_source", [False, True])
def test_storage_move_and_set_http_success_and_switch(server_env: dict[str, Any], tmp_path: Path, keep_source: bool):
    port = server_env["port"]
    token = server_env["token"]
    origin = server_env["origin"]
    roots: state.StatePaths = server_env["roots"]

    # 1. Create a sentinel blob and metadata in roots.store
    digest = "sha256:" + "a" * 64
    blob_dir = roots.store / "blobs" / "sha256"
    blob_dir.mkdir(parents=True, exist_ok=True)
    sentinel_blob = blob_dir / ("a" * 64)
    sentinel_blob.write_bytes(b"sentinel artifact data 123")

    meta_dir = roots.store / "metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_file = meta_dir / f"{'a' * 64}.json"
    meta_data = {
        "digest": digest,
        "kind": "cloud-image",
        "disk_format": "qcow2",
        "arch": "x86_64",
        "name": "sentinel-image",
        "size_bytes": 26,
    }
    state.atomic_write_json(meta_file, meta_data)

    initial_state_root = roots.state

    # Verify initial GET /api/v1/store/artifacts sees sentinel artifact
    status, _, artifacts_init = _request(
        port, "GET", "/api/v1/store/artifacts", headers={"Authorization": f"Bearer {token}"}
    )
    assert status == 200
    assert any(a["digest"] == digest for a in artifacts_init.get("images", []))

    # 2. Perform POST /api/v1/storage/move to dest_move
    dest_move = (tmp_path / f"moved_state_{keep_source}").resolve()
    status, _, move_res = _request(
        port,
        "POST",
        "/api/v1/storage/move",
        headers={"Authorization": f"Bearer {token}", "Origin": origin},
        body={"destination": str(dest_move), "keep_source": keep_source},
    )
    assert status == 200
    assert move_res["new_root"] == str(dest_move)

    # 3. Next GET /api/v1/storage reports destination
    status, _, storage_res = _request(port, "GET", "/api/v1/storage", headers={"Authorization": f"Bearer {token}"})
    assert status == 200
    assert storage_res["state_root"] == str(dest_move)

    # 4. Subsequent artifact/store request sees moved data
    status, _, artifacts_after_move = _request(
        port, "GET", "/api/v1/store/artifacts", headers={"Authorization": f"Bearer {token}"}
    )
    assert status == 200
    assert any(a["digest"] == digest for a in artifacts_after_move.get("images", []))
    assert (dest_move / "store" / "blobs" / "sha256" / ("a" * 64)).read_bytes() == b"sentinel artifact data 123"

    # 5. Assert old tree removal matches keep_source
    if keep_source:
        assert initial_state_root.exists()
    else:
        assert not initial_state_root.exists()

    # 6. Analogous set test ensuring subsequent calls switch
    dest_set = (tmp_path / f"set_state_{keep_source}").resolve()
    dest_set.mkdir(parents=True, exist_ok=True)

    status, _, set_res = _request(
        port,
        "POST",
        "/api/v1/storage/set",
        headers={"Authorization": f"Bearer {token}", "Origin": origin},
        body={"destination": str(dest_set)},
    )
    assert status == 200
    assert set_res["new_root"] == str(dest_set)

    status, _, storage_after_set = _request(
        port, "GET", "/api/v1/storage", headers={"Authorization": f"Bearer {token}"}
    )
    assert status == 200
    assert storage_after_set["state_root"] == str(dest_set)

    # Ensure empty set destination gets normal state directory structure/modes
    for sub in (
        "store",
        "runs",
        "locks",
        "transfers",
        "tags",
        "builds",
        "build-cache",
        "runtime-packs",
        "projects",
        "volumes",
    ):
        subdir = dest_set / sub
        assert subdir.is_dir()
        assert (subdir.stat().st_mode & 0o777) == 0o700


def test_storage_move_and_set_http_rejected_when_env_active(
    server_env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    port = server_env["port"]
    token = server_env["token"]
    origin = server_env["origin"]
    headers = {"Authorization": f"Bearer {token}", "Origin": origin}

    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "env_state_override"))

    dest_move = tmp_path / "http_move_dest"
    status, _, json_move = _request(
        port,
        "POST",
        "/api/v1/storage/move",
        headers=headers,
        body={"destination": str(dest_move)},
    )
    assert status == 409
    assert "PALIMPSEST_STATE_HOME" in json_move["error"]
    assert "unset" in json_move["error"]
    assert not dest_move.exists()

    dest_set = tmp_path / "http_set_dest"
    dest_set.mkdir()
    status, _, json_set = _request(
        port,
        "POST",
        "/api/v1/storage/set",
        headers=headers,
        body={"destination": str(dest_set)},
    )
    assert status == 409
    assert "PALIMPSEST_STATE_HOME" in json_set["error"]
    assert "unset" in json_set["error"]
    assert not (dest_set / "store").exists()
