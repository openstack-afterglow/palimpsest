from __future__ import annotations

import http.client
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
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


def _run_ui_consumer(start, end, invocation, data):
    """Execute the production handler, capturing its outbound request (not an echo)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("UI JavaScript consumer regressions require Node.js")
    source = Path(ui.__file__).with_name("webui").joinpath("app.js").read_text()
    handler = source[source.index(start) : source.index(end, source.index(start))]
    script = (
        r"""
import fs from 'node:fs';
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const requests = [], messages = [], elements = new Map();
let submit;
function $(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', checked: false, open: false,
    addEventListener: (_event, callback) => { submit = callback; }
  });
  return elements.get(id);
}
const state = { summary: { data: { host: data.host } }, volumes: { data: {} } };
const remoteState = { result: { connection_id: 'docker' } };
const remoteDetail = {}, hubState = {}, pendingRemoteRuns = new Set();
let actionBusy = false, refreshPromise = null;
const array = value => Array.isArray(value) ? value : [];
const object = value => value || {};
const text = value => String(value);
const canControl = () => true, freshForMutation = () => true;
const selectedRunVolumes = () => [];
const updateControls = () => {}, schedule = () => {}, rememberOperation = () => {};
const refresh = async () => {}, closeRemoteDetail = () => {};
const feedback = message => messages.push(message);
const notice = (_target, message) => messages.push(message);
async function api(path, options) { requests.push({ path, ...options }); return { id: 'submitted' }; }
$('remote-connection').value = 'docker';
$('run-image').value = '/private/image.oci';
$('run-runtime-kind').value = data.runtime_kind || 'oci-root';
$('run-force-architecture').checked = data.force || false;
"""
        + handler
        + "\n"
        + invocation
        + "\nconsole.log(JSON.stringify({ requests, messages }));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "machine,selection,expected",
    [
        ("arm64", "config-amd64", "linux/amd64"),
        ("x86_64", "config-arm64", "linux/arm64"),
        ("arm64", "manifest", "linux/amd64"),
        ("x86_64", "root", "linux/amd64"),
        ("amd64", "root", "linux/amd64"),
        ("arm64", "root", "linux/arm64"),
        ("aarch64", "root", "linux/arm64"),
        ("x86_64", "root-digestless", "linux/amd64"),
    ],
)
def test_remote_download_uses_selected_child_or_actual_host(machine, selection, expected):
    parent, child = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    item = {
        "download_supported": True,
        "run_supported": True,
        "immutable_reference": "docker.io/library/alpine@" + parent,
        "platforms": [
            {"digest": "sha256:" + "3" * 64, "os": "linux", "architecture": "arm64"},
            {"digest": child, "os": "linux", "architecture": "amd64"},
        ],
    }
    if selection == "root-digestless":
        for platform in item["platforms"]:
            platform.pop("digest")
    if not selection.startswith("root"):
        item["manifest_digest"] = child
        item["download_reference"] = "docker.io/library/alpine@" + child
    if selection.startswith("config-"):
        item["config"] = {"os": "linux", "architecture": selection.removeprefix("config-")}
        if selection == "config-arm64":
            item["platforms"][0]["digest"], item["platforms"][1]["digest"] = (child, "sha256:" + "3" * 64)
    result = _run_ui_consumer(
        "  function remoteDownloadPlatform(",
        "  function setupRunFromReceipt(",
        "await startRemoteDownload(data.item, true);",
        {"host": {"system": "Darwin" if machine == "arm64" else "Linux", "machine": machine}, "item": item},
    )
    assert len(result["requests"]) == 1, result["messages"]
    request = result["requests"][0]
    assert request["path"] == "/api/v1/transfers" and request["method"] == "POST"
    assert request["body"]["reference"] == item.get("download_reference", item["immutable_reference"])
    assert request["body"]["platform"] == expected


@pytest.mark.parametrize("force,runtime_kind", [(True, "oci-root"), (False, "oci-root"), (False, "cloud-image")])
def test_run_form_emits_disposable_carrier_but_retains_native_roots(force, runtime_kind):
    result = _run_ui_consumer(
        "  $('form-run-vm').addEventListener(",
        "  $('form-build').addEventListener(",
        "await submit({ preventDefault() {} });",
        {"host": {"system": "Linux", "machine": "x86_64"}, "force": force, "runtime_kind": runtime_kind},
    )
    assert len(result["requests"]) == 1, result["messages"]
    request = result["requests"][0]
    assert request["path"] == "/api/v1/vms" and request["method"] == "POST"
    assert request["body"]["root_retention"] == ("delete" if force else "retain")
    assert request["body"].get("force_architecture", False) is force


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
    ("rm", "rm", "DELETE", "/api/v1/vms/ui-vm", {"volumes": True}),
)


@pytest.fixture(autouse=True)
def _stub_operation_capability_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ui.inventory.runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "test-present", True),
    )
    monkeypatch.setattr(
        ui.inventory.host_inventory,
        "observe",
        lambda: {
            "vms": [],
            "volumes": [],
            "networks": [],
            "warnings": [],
        },
    )


@pytest.fixture
def server_env(tmp_path: Path, request: pytest.FixtureRequest):
    roots = _setup_roots(tmp_path)
    token = "secret-test-token-12345"

    # Create dummy server to assign an ephemeral port
    dummy_server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
    port = dummy_server.server_address[1]
    dummy_server.server_close()

    origin = f"http://127.0.0.1:{port}"
    handler_options = {} if getattr(request, "param", False) is None else {"read_only": False}
    handler_cls = ui.build_handler(roots, token=token, origin=origin, **handler_options)
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
    handler_cls.close_transports()
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


@pytest.mark.parametrize("path", ["/api/v1/operations", "/app.js"])
@pytest.mark.parametrize("disconnect_at", ["headers", "body"])
def test_disconnected_response_does_not_retry_or_break_next_request(
    server_env, monkeypatch, capsys, path, disconnect_at
):
    server = server_env["server"]
    handler = server.RequestHandlerClass
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    statuses = []
    errors = []
    original_headers = handler.end_headers
    original_response = handler.send_response
    original_process = server.process_request_thread
    original_error = server.handle_error

    def pause_response(self):
        if entered.is_set():
            return original_headers(self)
        if disconnect_at == "body":
            original_headers(self)
        entered.set()
        assert release.wait(5)
        if disconnect_at == "headers":
            original_headers(self)

    def record_response(self, status, message=None):
        statuses.append(status)
        return original_response(self, status, message)

    def complete_request(request, address):
        try:
            original_process(request, address)
        finally:
            completed.set()

    def record_error(request, address):
        errors.append(sys.exc_info()[1])
        original_error(request, address)

    monkeypatch.setattr(handler, "end_headers", pause_response)
    monkeypatch.setattr(handler, "send_response", record_response)
    monkeypatch.setattr(server, "process_request_thread", complete_request)
    monkeypatch.setattr(server, "handle_error", record_error)
    peer = socket.create_connection(("127.0.0.1", server_env["port"]), timeout=5)
    try:
        peer.sendall(f"GET {path} HTTP/1.0\r\nAuthorization: Bearer {server_env['token']}\r\n\r\n".encode())
        assert entered.wait(5)
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        peer.close()
    finally:
        peer.close()
        release.set()
    assert completed.wait(5)
    assert statuses == [200], "a closed response must not be replaced with an HTTP 500"
    assert errors == []
    assert capsys.readouterr().err == ""
    assert _request(server_env["port"], "GET", "/api/v1/operations")[0] == 401
    status, _, result = _request(
        server_env["port"],
        "GET",
        "/api/v1/operations",
        headers={"Authorization": f"Bearer {server_env['token']}"},
    )
    assert status == 200
    assert result == {"operations": []}


@pytest.mark.parametrize(
    ("system", "machine", "supported"),
    [
        ("Darwin", "aarch64", {"lima-vz", "lima-krunkit", "libvirt-hvf"}),
        ("Darwin", "x86_64", {"libvirt-hvf"}),
        ("Linux", "x86_64", {"kvm"}),
        ("Linux", "aarch64", {"kvm"}),
        ("Windows", "x86_64", set()),
    ],
)
def test_summary_rejects_unsupported_backends_before_probing(server_env, monkeypatch, system, machine, supported):
    host = ui.platforms.HostPlatform(system, machine)
    monkeypatch.setattr(ui.platforms, "detect_host", lambda: host)

    def probe(requirement, *, dispatch_key, **_kwargs):
        assert dispatch_key.backend.value in supported, "unsupported backends must not contact host services"
        return CapabilityCheck(requirement.capability_id, "test-present", True)

    monkeypatch.setattr(ui.platforms, "_check_capability", probe)
    status, _, result = _request(
        server_env["port"],
        "GET",
        "/api/v1/summary",
        headers={"Authorization": f"Bearer {server_env['token']}"},
    )
    assert status == 200
    assert {backend for backend, item in result["backends"].items() if item["available"]} == supported
    for backend, item in result["backends"].items():
        if backend not in supported:
            assert item["reason"]


def test_internal_request_failure_still_returns_error_and_logs(server_env, monkeypatch, capsys):
    def fail(_self):
        raise RuntimeError("deliberate internal failure")

    monkeypatch.setattr(ui.OperationManager, "list", fail)
    status, _, result = _request(
        server_env["port"],
        "GET",
        "/api/v1/operations",
        headers={"Authorization": f"Bearer {server_env['token']}"},
    )
    assert status == 500
    assert "error" in result
    assert "RuntimeError" in capsys.readouterr().err


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

    # 4. The static shell is credential-free so a token-stripped URL can reload from the tab session.
    for shell_path in ("/", f"/?token={token}", "/index.html"):
        status, resp_headers, content = _request(port, "GET", shell_path)
        assert status == 200
        assert "<!DOCTYPE html>" in content

    # 5. Query token on API route -> 401 (APIs accept only the Authorization header)
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


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "a" * 64,
        "ui-vm%0A",
        "../ui-vm",
        "ui-vm%2Fother",
        "ui-vm%252Fother",
        "host:kvm:",
        "host:unknown:vm",
        "other:kvm:vm",
        "host:kvm:.",
        "host:kvm:..",
        "host:kvm:vm%2Fother",
        "host:kvm:vm%5Cother",
        "host:kvm:vm%00",
        "host:kvm:vm%0A",
        "host:kvm:vm%7F",
        "host:kvm:%FF",
        "host:kvm:vm%",
        "host:kvm:vm%GG",
        "host:kvm:" + "a" * (513 - len("host:kvm:")),
    ],
)
def test_vm_resource_routes_reject_malformed_identifiers_before_forwarding(
    server_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    encoded: str,
) -> None:
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("Malformed VM identifier reached backend")

    for method in ("get_vm", "vm_logs", "vm_metrics", "remove_vm"):
        monkeypatch.setattr(ui.inventory, method, forbidden)
    monkeypatch.setattr(ui.ConsoleManager, "open", forbidden)
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    for method, suffix in (("GET", ""), ("GET", "/logs"), ("GET", "/metrics"), ("POST", "/console"), ("DELETE", "")):
        status, _, result = _request(
            server_env["port"],
            method,
            "/api/v1/vms/" + encoded + suffix,
            headers,
            body={} if method == "POST" else None,
        )
        assert status == 400
    assert calls == []


@pytest.mark.parametrize("tail", ["-1", "0", "5001", "10001", "1.5", "arbitrary", "999999999999999999999999999999"])
def test_vm_logs_reject_invalid_tail_before_forwarding(server_env, monkeypatch, tail):
    calls = []
    monkeypatch.setattr(ui.inventory, "vm_logs", lambda *a, **k: calls.append((a, k)))
    headers = {"Authorization": f"Bearer {server_env['token']}"}
    for identifier in ("ui-vm", "host:kvm:external"):
        status, _, result = _request(
            server_env["port"],
            "GET",
            "/api/v1/vms/" + urllib.parse.quote(identifier, safe="") + "/logs?tail=" + tail,
            headers,
        )
        assert status == 400
        assert "tail parameter" in result["error"]
    assert calls == []


@pytest.mark.parametrize(
    ("identifier", "mode", "argv", "expected_run_id"),
    [
        ("ui-vm", "shell", [], "11111111-1111-4111-8111-111111111111"),
        ("host:lima-vz:external", "shell", [], None),
        ("host:libvirt-hvf:Guest café:one", "exec", ["printf", "hello world"], "22222222-2222-4222-8222-222222222222"),
    ],
)
def test_vm_console_forwards_decoded_stable_id_and_expected_run_id(
    server_env,
    monkeypatch,
    identifier,
    mode,
    argv,
    expected_run_id,
):
    calls = []
    session_id = "a" * 32
    response = {"id": session_id, "name": identifier, "capabilities": {"stdin": True}, "cursor": 0}

    def open_console(self, roots, actual_identifier, **kwargs):
        calls.append((roots, actual_identifier, kwargs))
        return response

    monkeypatch.setattr(ui.ConsoleManager, "open", open_console)
    body = {"mode": mode, "argv": argv, "id": session_id}
    if expected_run_id is not None:
        body["expected_run_id"] = expected_run_id
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    status, _, result = _request(
        server_env["port"],
        "POST",
        "/api/v1/vms/" + urllib.parse.quote(identifier, safe="") + "/console",
        headers,
        body,
    )
    assert status == 201
    assert result == response
    assert calls == [
        (
            server_env["roots"],
            identifier,
            {
                "mode": mode,
                "argv": argv,
                "session_id": session_id,
                "expected_run_id": expected_run_id,
            },
        )
    ]
    assert list(server_env["roots"].runs.iterdir()) == []


@pytest.mark.parametrize(
    ("body", "query"),
    [
        ("{", ""),
        ("[]", ""),
        ("null", ""),
        (b'"not-an-object"', ""),
        ({"confirmation": "x" * 65536}, ""),
        ({"delete_files": True}, ""),
        ({"volumes": False}, "?volumes=true"),
    ],
)
def test_host_vm_removal_rejects_invalid_body_before_inventory(server_env, monkeypatch, body, query):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid DELETE body reached inventory removal")

    monkeypatch.setattr(ui.inventory, "remove_vm", forbidden)
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    path = "/api/v1/vms/" + urllib.parse.quote("host:lima-vz:external", safe="") + query
    status, _, result = _request(server_env["port"], "DELETE", path, headers, body)
    assert status == 400
    assert "error" in result


def test_host_vm_start_stop_routes_never_forward_to_dispatch(server_env, monkeypatch):
    calls = []
    for method in ("start", "stop", "rm"):
        monkeypatch.setattr(ui.inventory.runtime_dispatch, method, lambda *a, **k: calls.append((a, k)))
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    for identifier in ("host:lima-vz:external", "host:libvirt-hvf:Guest café"):
        for encoded in {identifier, urllib.parse.quote(identifier, safe="")}:
            if not encoded.isascii():
                continue
            for method, suffix in (("POST", "/start"), ("POST", "/stop")):
                status, _, result = _request(server_env["port"], method, "/api/v1/vms/" + encoded + suffix, headers)
                assert status == 400
                assert result == {"error": "invalid run name"}
    assert calls == []
    assert list(server_env["roots"].runs.iterdir()) == []


@pytest.mark.parametrize("server_env", [None], indirect=True)
def test_read_only_console_and_removal_deny_stable_ids_before_mutation(server_env, monkeypatch):
    calls = []
    monkeypatch.setattr(ui.ConsoleManager, "open", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(ui.inventory, "remove_vm", lambda *a, **k: calls.append((a, k)))
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    for identifier in ("ui-vm", "host:lima-vz:external"):
        status, _, result = _request(
            server_env["port"],
            "POST",
            "/api/v1/vms/" + urllib.parse.quote(identifier, safe="") + "/console",
            headers,
            {"expected_run_id": "11111111-1111-4111-8111-111111111111"},
        )
        assert status == 403
        assert "--allow-control" in result["error"]
    assert calls == []


def test_vm_resource_routes_preserve_auth_and_mutation_csrf(server_env, monkeypatch):
    calls = []
    for method in ("get_vm", "vm_logs", "vm_metrics", "remove_vm"):
        monkeypatch.setattr(ui.inventory, method, lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(ui.ConsoleManager, "open", lambda *a, **k: calls.append((a, k)))
    path = "/api/v1/vms/" + urllib.parse.quote("host:lima-vz:external", safe="")
    for method, suffix in (("GET", ""), ("GET", "/logs"), ("GET", "/metrics"), ("POST", "/console"), ("DELETE", "")):
        status, _, result = _request(server_env["port"], method, path + suffix, body={})
        assert status == 401
        assert result == {"error": "Unauthorized"}
    auth = {"Authorization": f"Bearer {server_env['token']}"}
    for extra in ({"Origin": "https://foreign.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
        for method, suffix in (("POST", "/console"), ("DELETE", "")):
            status, _, _ = _request(server_env["port"], method, path + suffix, auth | extra, {})
            assert status == 403
    assert calls == []


def test_vm_console_default_body_preserves_managed_route_contract(server_env, monkeypatch):
    calls = []

    def open_console(self, roots, identifier, **kwargs):
        calls.append((roots, identifier, kwargs))
        return {"id": "a" * 32, "name": identifier}

    monkeypatch.setattr(ui.ConsoleManager, "open", open_console)
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    status, _, result = _request(server_env["port"], "POST", "/api/v1/vms/ui-vm/console", headers, {})
    assert status == 201
    assert result["name"] == "ui-vm"
    assert calls == [
        (
            server_env["roots"],
            "ui-vm",
            {
                "mode": "shell",
                "argv": [],
                "session_id": None,
                "expected_run_id": None,
            },
        )
    ]


@pytest.mark.parametrize("resource", ["logs", "metrics"])
def test_vm_http_reads_reject_replaced_run_before_guest_probe(server_env, monkeypatch, resource):
    from palimpsest_local import vm_access

    identifier = "host:lima-vz:redis.qa-1"
    current_run = "22222222-2222-4222-8222-222222222222"
    selected_run = "11111111-1111-4111-8111-111111111111"
    row = {
        "id": identifier,
        "name": "redis.qa-1",
        "run_id": current_run,
        "backend": "lima-vz",
        "runtime_kind": "cloud-image",
        "status": "running",
        "managed": False,
        "source": "host-backend",
        "stale": False,
    }
    monkeypatch.setattr(
        ui.inventory.host_inventory,
        "observe",
        lambda: {
            "vms": [row],
            "volumes": [],
            "networks": [],
            "warnings": [],
        },
    )
    effects = []

    def forbidden(*args, **kwargs):
        effects.append((args, kwargs))
        pytest.fail("replaced VM reached guest probe")

    monkeypatch.setattr(vm_access, "spawn_process_session", forbidden)
    monkeypatch.setattr(vm_access.runtime_dispatch, "exec", forbidden)
    path = "/api/v1/vms/" + urllib.parse.quote(identifier, safe="") + "/" + resource
    status, _, result = _request(
        server_env["port"],
        "GET",
        path + "?expected_run_id=" + selected_run,
        {"Authorization": "Bearer " + server_env["token"]},
    )
    assert status == 200
    assert result["status"] == "unavailable"
    assert result["reason"] == "identity"
    if resource == "logs":
        assert result["log"] == "" and result["source"] is None
    else:
        assert result["run_id"] == selected_run
        assert result["cpu"] is None and result["memory"] is None
    assert effects == []
    assert list(server_env["roots"].runs.iterdir()) == []


def test_vm_console_preserves_backend_identity_refusal(server_env, monkeypatch):
    def refuse(self, roots, identifier, **kwargs):
        assert identifier == "host:lima-vz:external"
        assert kwargs["expected_run_id"] == "11111111-1111-4111-8111-111111111111"
        raise ui.ConsoleError("identity", "Selected VM identity changed")

    monkeypatch.setattr(ui.ConsoleManager, "open", refuse)
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    status, _, result = _request(
        server_env["port"],
        "POST",
        "/api/v1/vms/host%3Alima-vz%3Aexternal/console",
        headers,
        {"expected_run_id": "11111111-1111-4111-8111-111111111111"},
    )
    assert status == 409
    assert result == {"error": "Selected VM identity changed", "category": "identity"}


def test_vm_console_preserves_expired_open_guard(server_env, monkeypatch):
    calls = []
    monkeypatch.setattr(ui.ConsoleManager, "open", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(ui.ConsoleManager, "IDLE_SECONDS", 0)
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    status, _, result = _request(server_env["port"], "POST", "/api/v1/vms/ui-vm/console", headers, {})
    assert status == 409
    assert result == {"error": "Console open request expired", "category": "closed"}
    assert calls == []


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
    rpaths = _write_ui_run_ledger(roots, backend=backend)
    record = state.read_run_state(rpaths)
    previous_status = "running" if operation == "stop" else "stopped"
    state.atomic_write_json(rpaths.state, {**record, "status": previous_status})
    for adapter in (ui.inventory.runtime_dispatch.cloud_runtime, ui.inventory.runtime_dispatch.lima):
        monkeypatch.setattr(
            adapter,
            "reconcile_run",
            lambda name, **kwargs: {
                "state": state.read_run_state(state.run_paths(roots, name)),
                "warnings": [],
            },
        )
    calls: list[tuple[str, dict[str, object]]] = []

    def selected(name: str, **kwargs: object) -> object:
        calls.append((name, kwargs))
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

    selected_adapter = getattr(ui.inventory.runtime_dispatch, adapter_name)
    other_adapter = (
        ui.inventory.runtime_dispatch.lima
        if adapter_name == "cloud_runtime"
        else ui.inventory.runtime_dispatch.cloud_runtime
    )
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

    body = {"expected_run_id": record["run_id"]}
    if operation == "rm":
        body.update(confirmation="ui-vm", volumes=True)
    status, response_headers, payload = _request(server_env["port"], method, path, headers=headers, body=body)

    assert status == 200
    assert "application/json" in response_headers["Content-Type"]
    assert payload == {
        "name": "ui-vm",
        "run_id": payload["run_id"],
        "runtime_kind": "cloud-image",
        "backend": backend,
        "operation": operation,
        "previous_status": previous_status,
        "status": "running" if operation == "start" else "removed" if operation == "rm" else "stopped",
        "lifecycle_revision": 1,
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
    assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
    assert kwargs == {"roots": roots, **expected_kwargs}


@pytest.mark.parametrize(("operation", "target_name", "method", "path", "_expected_kwargs"), _UI_LIFECYCLE_REQUESTS)
def test_ui_oci_vm_lifecycle_fails_closed_with_typed_409_before_backend_side_effects(
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
        ui.inventory.runtime_dispatch.cloud_runtime,
        target_name,
        lambda *_args, **_kwargs: forbidden("cloud"),
    )
    monkeypatch.setattr(
        ui.inventory.runtime_dispatch.lima,
        target_name,
        lambda *_args, **_kwargs: forbidden("lima"),
    )
    headers = {
        "Authorization": f"Bearer {server_env['token']}",
        "Origin": server_env["origin"],
    }

    record = state.read_run_state(rpaths)
    body = {"expected_run_id": record["run_id"]}
    if operation == "rm":
        body.update(confirmation="ui-vm", volumes=True)
    status, _, payload = _request(server_env["port"], method, path, headers=headers, body=body)

    assert status == 409
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

    owner = json.loads(rpaths.owner.read_bytes())
    body = {"expected_run_id": owner["run_id"]}
    if method == "DELETE":
        body.update(confirmation="ui-vm", volumes=True)
    status, _, payload = _request(server_env["port"], method, path, headers=headers, body=body)

    assert status == 409
    assert "sensitive-corrupt-value" not in json.dumps(payload)
    assert rpaths.state.read_bytes() == before


def test_static_asset_routes(server_env: dict[str, Any]):
    port = server_env["port"]
    token = server_env["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. GET / returns the credential-free bootstrap HTML
    status, resp_headers, content = _request(port, "GET", f"/?token={token}")
    assert status == 200
    assert "text/html" in resp_headers.get("Content-Type", "")

    # 2. Unauthenticated asset fetches return 401
    status, _, _ = _request(port, "GET", "/app.css")
    assert status == 401
    status, _, _ = _request(port, "GET", "/app.js")
    assert status == 401

    # 3. Asset fetches with query token return 401 (assets accept only the Authorization header)
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


def test_get_routes_and_not_found(server_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
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
    def vm_logs(actual_roots, identifier, *, tail, expected_run_id, **_kwargs):
        return {"status": "available", "log": "vm journal line 1\n", "source": "guest-journal", "truncated": False}

    monkeypatch.setattr(ui.inventory, "vm_logs", vm_logs)
    status, _, log_data = _request(port, "GET", "/api/v1/vms/demo-vm/logs?tail=10", headers=headers)
    assert status == 200
    assert "vm journal line 1" in log_data["log"]

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
    status, _, json_data = _request(
        port,
        "DELETE",
        f"/api/v1/store/artifacts/{base_digest}",
        headers=headers,
        body={"confirmation": base_digest},
    )
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


@pytest.mark.parametrize("server_env", [None], indirect=True)
def test_read_only_default_refuses_all_mutation_routes_without_state_changes(server_env: dict[str, Any]) -> None:
    roots = server_env["roots"]
    _write_ui_run_ledger(roots, backend="kvm")
    source = roots.state / "import.raw"
    source.write_bytes(b"cloud-image-fixture")
    destination = roots.state.parent / "new-root"
    before = {path.relative_to(roots.state): path.read_bytes() for path in roots.state.rglob("*") if path.is_file()}
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    requests = [
        ("POST", "/api/v1/vms/ui-vm/start", None),
        ("POST", "/api/v1/vms/ui-vm/stop", None),
        ("DELETE", "/api/v1/vms/ui-vm?volumes=true", None),
        (
            "POST",
            "/api/v1/builds",
            {"context": str(roots.state), "tag": "example", "dockerfile_text": "FROM scratch\n"},
        ),
        ("POST", "/api/v1/vms", {"image": "example"}),
        ("POST", "/api/v1/volumes", {"name": "data", "backend": "kvm", "size_bytes": 1073741824}),
        ("DELETE", "/api/v1/volumes/named:kvm:data", {"confirmation": "data"}),
        ("DELETE", "/api/v1/volumes/retained-instance:" + "a" * 64, {"confirmation": "saved"}),
        ("POST", "/api/v1/vms/ui-vm/console", {"mode": "shell"}),
        ("POST", "/api/v1/consoles/" + "a" * 32 + "/input", {"data": "eA=="}),
        ("POST", "/api/v1/consoles/" + "a" * 32 + "/resize", {"rows": 24, "columns": 80}),
        ("POST", "/api/v1/consoles/" + "a" * 32 + "/signal", {"signal": "interrupt"}),
        ("DELETE", "/api/v1/consoles/" + "a" * 32, None),
        ("DELETE", "/api/v1/store/artifacts/sha256:" + "a" * 64, None),
        ("POST", "/api/v1/store/import", {"path": str(source), "disk_format": "raw", "arch": "x86_64"}),
        ("POST", "/api/v1/store/artifacts/sha256:" + "a" * 64 + "/tag", {"tag": "example"}),
        ("POST", "/api/v1/storage/set", {"destination": str(destination)}),
        ("POST", "/api/v1/storage/move", {"destination": str(destination)}),
    ]
    for method, path, body in requests:
        status, _, payload = _request(server_env["port"], method, path, headers=headers, body=body)
        assert status == 403
    after = {path.relative_to(roots.state): path.read_bytes() for path in roots.state.rglob("*") if path.is_file()}
    assert after == before
    assert not destination.exists()

    status, _, summary = _request(server_env["port"], "GET", "/api/v1/summary", headers=headers)
    assert status == 200
    assert summary["read_only"] is True
    status, _, operations = _request(server_env["port"], "GET", "/api/v1/operations", headers=headers)
    assert status == 200 and operations == {"operations": []}


def test_web_tag_edit_persists_and_refuses_reassignment(server_env: dict[str, Any], tmp_path: Path) -> None:
    roots = server_env["roots"]
    images = []
    for index in range(2):
        source = tmp_path / f"image-{index}.raw"
        source.write_bytes(f"image-{index}".encode())
        images.append(ui.inventory.import_cloud_image(roots, source, disk_format="raw", arch="aarch64")["digest"])
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    endpoint = f"/api/v1/store/artifacts/{images[0]}/tag"
    status, _, _ = _request(server_env["port"], "POST", endpoint, body={"tag": "web-image"})
    assert status == 401
    status, _, _ = _request(
        server_env["port"],
        "POST",
        endpoint,
        headers={**headers, "Origin": "https://foreign.invalid"},
        body={"tag": "web-image"},
    )
    assert status == 403
    assert not (roots.tags / "web-image.json").exists()
    status, _, payload = _request(server_env["port"], "POST", endpoint, headers=headers, body={"tag": "web-image"})
    assert status == 200
    assert payload == {"digest": images[0], "tag": "web-image"}
    assert state.read_tag_record(roots, "web-image").digest == images[0]
    status, _, _ = _request(
        server_env["port"],
        "POST",
        f"/api/v1/store/artifacts/{images[1]}/tag",
        headers=headers,
        body={"tag": "web-image"},
    )
    assert status == 409
    assert state.read_tag_record(roots, "web-image").digest == images[0]
    status, _, inventory = _request(server_env["port"], "GET", "/api/v1/store/artifacts", headers=headers)
    assert status == 200
    assert next(image for image in inventory["images"] if image["digest"] == images[0])["tags"][0]["tag"] == "web-image"


def test_console_routes_lifecycle_and_precancellation(
    server_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from palimpsest_local import console
    from palimpsest_local.process_session import LocalProcessSession

    roots = server_env["roots"]
    rpaths = _write_ui_run_ledger(roots, backend="kvm")
    run_id = state.read_run_state(rpaths)["run_id"]
    calls = []

    def resolve(actual_roots, identifier, *, expected_run_id):
        assert actual_roots is roots
        assert identifier == "ui-vm"
        assert expected_run_id == run_id
        calls.append((identifier, expected_run_id))
        return SimpleNamespace(
            open=lambda mode, argv: LocalProcessSession(
                (sys.executable, "-c", "import time; time.sleep(60)"),
                tty=False,
                stdin=True,
            )
        )

    monkeypatch.setattr(console.vm_access.Access, "resolve", resolve)

    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}

    # 1. Pre-cancellation test: DELETE before open
    pre_id = "00001111222233334444555566667777"
    status, _, res = _request(server_env["port"], "DELETE", f"/api/v1/consoles/{pre_id}", headers=headers)
    assert status == 200 and res["status"] == "disconnected"
    status, _, err = _request(
        server_env["port"],
        "POST",
        "/api/v1/vms/ui-vm/console",
        headers=headers,
        body={"mode": "shell", "id": pre_id, "expected_run_id": run_id},
    )
    assert status == 409
    assert "cancelled" in err["error"]

    # 2. Normal open with explicit ID
    sess_id = "88889999aaaabbbbccccddddeeeeffff"
    status, _, opened = _request(
        server_env["port"],
        "POST",
        "/api/v1/vms/ui-vm/console",
        headers=headers,
        body={"mode": "shell", "id": sess_id, "expected_run_id": run_id},
    )
    assert status == 201
    assert opened["id"] == sess_id
    assert opened["capabilities"]["stdin"] is True
    assert calls == [("ui-vm", run_id)]

    # 3. Write input
    status, _, _ = _request(
        server_env["port"],
        "POST",
        f"/api/v1/consoles/{sess_id}/input",
        headers=headers,
        body={"data": "aGVsbG8="},
    )
    assert status == 200

    # 4. Disconnect via DELETE
    status, _, closed = _request(server_env["port"], "DELETE", f"/api/v1/consoles/{sess_id}", headers=headers)
    assert status == 200
    assert closed["status"] == "disconnected"


@pytest.mark.parametrize("inherit_ignored_sigint", [False, True])
@pytest.mark.parametrize(("open_browser", "blocking_browser"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_web_process_browser_choice_and_shutdown(
    tmp_path: Path,
    open_browser: bool,
    blocking_browser: bool,
    stop_signal: int,
    inherit_ignored_sigint: bool,
) -> None:
    roots = _setup_roots(tmp_path)
    marker = tmp_path / "browser-url"
    release_browser = tmp_path / "release-browser"
    browser_command = tmp_path / "browser.py"
    browser_command.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport sys, time\n"
        "Path(sys.argv[1]).write_text(sys.argv[2])\n"
        f"while {blocking_browser!r} and not Path(sys.argv[3]).exists():\n    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    browser_command.chmod(0o700)
    environment = dict(os.environ)
    environment.update(
        PALIMPSEST_STATE_HOME=str(roots.state),
        XDG_CONFIG_HOME=str(roots.config.parent),
        PALIMPSEST_URL="http://127.0.0.1:1",
        PALIMPSEST_TOKEN="not-used-by-local-web",
        BROWSER=f"{browser_command} {marker} %s {release_browser}",
    )
    args = [sys.executable, "-m", "palimpsest_local.cli", "web"]
    if not open_browser:
        args.append("--no-browser")
    if inherit_ignored_sigint:
        args = [
            sys.executable,
            "-c",
            "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_IGN); "
            "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])",
            *args[1:],
        ]
    process = subprocess.Popen(args, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 15)[0], "web did not print its listening URL"
        url = process.stdout.readline().strip()
        parsed = urllib.parse.urlparse(url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port is not None
        token = urllib.parse.parse_qs(parsed.query)["token"][0]
        headers = {"Authorization": f"Bearer {token}"}
        status, _, summary = _request(parsed.port, "GET", "/api/v1/summary", headers=headers)
        assert status == 200
        assert summary["read_only"] is True
        if open_browser:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.read_text() == url
            assert not release_browser.exists()
        else:
            assert not marker.exists()
        process.send_signal(stop_signal)
        process.wait(timeout=10)
        release_browser.touch()
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        with socket.socket() as connection:
            assert connection.connect_ex(("127.0.0.1", parsed.port)) != 0
    finally:
        release_browser.touch()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("ignore_original", [False, True])
def test_web_serve_restores_actual_original_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_signal: int, ignore_original: bool
) -> None:
    roots = _setup_roots(tmp_path)

    def original_interrupt(_signum, _frame):
        pytest.fail("original interrupt handler ran while web server owned signals")

    def original_terminate(_signum, _frame):
        pytest.fail("original terminate handler ran while web server owned signals")

    originals = {
        signal.SIGINT: signal.SIG_IGN if ignore_original else original_interrupt,
        signal.SIGTERM: signal.SIG_IGN if ignore_original else original_terminate,
    }
    saved = {signum: signal.getsignal(signum) for signum in originals}
    observed_server = []

    def serve_until_signalled(server):
        observed_server.append(server)
        for signum, original in originals.items():
            assert callable(signal.getsignal(signum))
            assert signal.getsignal(signum) != original
        os.kill(os.getpid(), stop_signal)
        pytest.fail("web stop signal returned without interrupting serve loop")

    monkeypatch.setattr(ui._LoopbackHTTPServer, "serve_forever", serve_until_signalled)
    try:
        for signum, original in originals.items():
            signal.signal(signum, original)
        port = ui.serve(roots, open_browser=False)
        assert {signum: signal.getsignal(signum) for signum in originals} == originals
        assert observed_server[0].socket.fileno() == -1
        with socket.socket() as connection:
            assert connection.connect_ex(("127.0.0.1", port)) != 0
    finally:
        for signum, original in saved.items():
            signal.signal(signum, original)


def test_dashboard_bind_does_not_reverse_resolve_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # http.server's default bind calls socket.getfqdn(), which hung `web` on macOS 15 runners.
    monkeypatch.setattr(socket, "getfqdn", lambda *_args: pytest.fail("dashboard bind performed reverse DNS"))
    server = ui._LoopbackHTTPServer(("127.0.0.1", 0), lambda *_args: None)
    try:
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.server_address[1] != 0
    finally:
        server.server_close()


def test_host_vm_detail_uses_stable_id_without_creating_run(server_env, monkeypatch):
    vm = {
        "id": "host:lima-vz:external",
        "name": "external",
        "run_id": None,
        "backend": "lima-vz",
        "runtime_kind": "external",
        "status": "running",
        "managed": False,
        "source": "host-backend",
    }
    monkeypatch.setattr(
        ui.inventory.host_inventory, "observe", lambda: {"vms": [vm], "volumes": [], "networks": [], "warnings": []}
    )
    headers = {"Authorization": "Bearer " + server_env["token"]}
    status, _, result = _request(
        server_env["port"], "GET", "/api/v1/vms/" + urllib.parse.quote(vm["id"], safe=""), headers
    )
    assert status == 200
    assert result["name"] == "external" and result["managed"] is False
    assert list(server_env["roots"].runs.iterdir()) == []
    for method, suffix in (("POST", "/start"), ("POST", "/stop")):
        status, _, _ = _request(server_env["port"], method, "/api/v1/vms/" + vm["id"] + suffix, headers)
        assert status == 400
    assert list(server_env["roots"].runs.iterdir()) == []


@pytest.fixture
def catalog_hub():
    """An isolated HTTP peer enforces original caller authority for real client IO."""
    import hashlib
    from http.server import BaseHTTPRequestHandler

    from palimpsest_local.hub import MEDIA_TYPE_LAYER_SQUASHFS

    payload = b"hsqs-local-catalog-consumer-proof"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    descriptor = {
        "blob_digest": digest,
        "name": "catalog-owned-layer",
        "kind": "squashfs",
        "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
        "size_bytes": len(payload),
        "arch": "aarch64",
        "parent_digest": None,
        "base_image_digest": None,
        "ancestors": [],
        "chain_complete": True,
    }

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            selected = urllib.parse.urlsplit(self.path)
            if self.headers.get("X-Auth-Token") != "synthetic-tab-keystone":
                code, body = 401, {"detail": "never echo remote credential rejection bodies"}
            elif self.headers.get("X-Project-Id") not in (None, "tab-project"):
                code, body = 403, {"detail": "foreign project assertion"}
            elif selected.path == "/v1/me":
                code, body = (
                    200,
                    {
                        "project_id": "tab-project",
                        "user_id": "tab-user",
                        "capabilities": {"inventory": True, "download": True, "upload": False},
                        "untrusted-extra": "synthetic-tab-keystone",
                    },
                )
            elif selected.path == "/v1/layers":
                query = urllib.parse.parse_qs(selected.query)
                matches = all(
                    not query.get(key) or query[key][0] == descriptor.get(key)
                    for key in ("kind", "arch", "name", "disk_format")
                )
                code, body = 200, [descriptor] if matches else []
            elif selected.path == "/v1/layers/" + digest:
                code, body = 200, descriptor
            elif selected.path == "/v1/layers/" + digest + "/blob":
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            else:
                code, body = 404, {"detail": "missing"}
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}", "digest": digest, "payload": payload}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("server_env", [None], indirect=True)
def test_read_only_catalog_connect_inspect_and_remote_failure_do_not_unlock_or_logout(
    server_env, catalog_hub, monkeypatch
):
    monkeypatch.setenv("PALIMPSEST_URL", "https://unrelated.invalid")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "unrelated-secret")
    monkeypatch.setenv("PALIMPSEST_PROJECT_ID", "unrelated-project")
    headers = {
        "Authorization": "Bearer " + server_env["token"],
        "X-Palimpsest-Hub-Url": catalog_hub["url"],
        "X-Palimpsest-Hub-Token": "synthetic-tab-keystone",
    }
    port = server_env["port"]
    code, _, identity = _request(port, "GET", "/api/v1/remotes/hub/identity", headers)
    assert code == 200 and identity["project_id"] == "tab-project"
    assert "synthetic-tab-keystone" not in json.dumps(identity)
    code, _, page = _request(port, "GET", "/api/v1/remotes/artifacts?connection=legacy&arch=aarch64", headers)
    assert code == 200 and page["items"][0]["reference"] == catalog_hub["digest"]
    code, _, page = _request(port, "GET", "/api/v1/remotes/artifacts?connection=legacy&arch=x86_64", headers)
    assert code == 200 and page["items"] == []
    detail_path = "/api/v1/remotes/artifact?" + urllib.parse.urlencode(
        {"connection": "legacy", "reference": catalog_hub["digest"]}
    )
    code, _, detail = _request(port, "GET", detail_path, headers)
    assert code == 200 and detail["chain"][0]["digest"] == catalog_hub["digest"]
    assert detail["download_supported"] is True and detail["run_supported"] is False
    denied_headers = {**headers, "X-Palimpsest-Hub-Token": "wrong-secret"}
    code, _, denial = _request(port, "GET", detail_path, denied_headers)
    assert code == 409 and "never echo" not in json.dumps(denial)
    code, _, _ = _request(port, "GET", "/api/v1/operations", headers)
    assert code == 200
    code, _, _ = _request(
        port,
        "POST",
        "/api/v1/transfers",
        {**headers, "Origin": server_env["origin"]},
        {"action": "download", "connection_id": "legacy", "reference": catalog_hub["digest"]},
    )
    assert code == 403
    assert os.environ["PALIMPSEST_PROJECT_ID"] == "unrelated-project"


def test_header_authority_survives_owned_transfer_child_without_process_global_credentials(
    server_env, catalog_hub, monkeypatch
):
    from palimpsest_local.oci_layout import ContentStore

    monkeypatch.setenv("PALIMPSEST_URL", "https://unrelated.invalid")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "unrelated-secret")
    monkeypatch.setenv("PALIMPSEST_PROJECT_ID", "unrelated-project")
    headers = {
        "Authorization": "Bearer " + server_env["token"],
        "Origin": server_env["origin"],
        "X-Palimpsest-Hub-Url": catalog_hub["url"],
        "X-Palimpsest-Hub-Token": "synthetic-tab-keystone",
    }
    code, _, operation = _request(
        server_env["port"],
        "POST",
        "/api/v1/transfers",
        headers,
        {"action": "download", "connection_id": "legacy", "reference": catalog_hub["digest"]},
    )
    assert code == 202
    deadline = time.monotonic() + 8
    while True:
        _, _, listing = _request(server_env["port"], "GET", "/api/v1/operations", headers)
        result = next(item for item in listing["operations"] if item["id"] == operation["id"])
        if result["status"] != "running":
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert result["status"] == "succeeded", result
    assert ContentStore(server_env["roots"].store).verify_blob(catalog_hub["digest"]) == len(catalog_hub["payload"])
    assert "synthetic-tab-keystone" not in json.dumps(result)
    assert os.environ["PALIMPSEST_PROJECT_ID"] == "unrelated-project"


@pytest.fixture
def external_lima(server_env, tmp_path, monkeypatch):
    import shutil

    from palimpsest_local import host_vm_control, lima

    home = (tmp_path / "lima").resolve()
    home.mkdir(mode=0o700)
    native = home / "external-vm"
    native.mkdir(mode=0o700)
    (native / "diffdisk").write_bytes(b"native VM disk")
    independent = home / "independent.raw"
    independent.write_bytes(b"independent disk")
    monkeypatch.setenv("LIMA_HOME", str(home))
    instance = {"name": native.name, "dir": str(native), "vmType": "vz", "status": "Running", "config": {}}
    control = {"instances": [instance], "fail_delete": False, "native": native, "independent": independent}

    def command(argv, **kwargs):
        if argv == ["limactl", "list", "--format", "json"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(row) for row in control["instances"]), "")
        if argv == ["limactl", "stop", "--force", "--", native.name]:
            control["instances"][0]["status"] = "Stopped"
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv == ["limactl", "delete", "--force", "--", native.name]
        if control["fail_delete"]:
            return subprocess.CompletedProcess(argv, 1, "", "sensitive backend failure")
        shutil.rmtree(native)
        control["instances"].clear()
        return subprocess.CompletedProcess(argv, 0, "", "")

    def observed():
        return {
            "vms": [
                {
                    "id": "host:lima-vz:" + row["name"],
                    "name": row["name"],
                    "run_id": lima._instance_run_id(row),
                    "backend": "lima-vz",
                    "runtime_kind": "external",
                    "status": row["status"].lower(),
                    "managed": False,
                    "source": "host-backend",
                    "removal_identity": host_vm_control.lima_removal_identity(row),
                }
                for row in control["instances"]
            ],
            "volumes": [],
            "networks": [],
            "warnings": [],
        }

    monkeypatch.setattr(lima, "_run_command", command)
    monkeypatch.setattr(ui.inventory.host_inventory, "observe", observed)
    control["headers"] = {"Authorization": "Bearer " + server_env["token"], "Origin": server_env["origin"]}
    control["path"] = "/api/v1/vms/" + urllib.parse.quote("host:lima-vz:" + native.name, safe="")
    code, _, row = _request(server_env["port"], "GET", control["path"], control["headers"])
    assert code == 200
    control["body"] = {
        "expected_run_id": row["run_id"],
        "expected_removal_identity": row["removal_identity"],
        "confirmation": row["name"],
        "volumes": False,
    }
    control["row"] = row
    return control


@pytest.mark.parametrize("status", ["Running", "Stopped"])
@pytest.mark.parametrize("marked", [False, True])
def test_external_lima_removal_without_original_state(server_env, external_lima, status, marked):
    native = external_lima
    native["instances"][0]["status"] = status
    if marked:
        native["instances"][0]["config"] = {"env": {"PALIMPSEST_RUN_ID": str(uuid.uuid4())}}
    code, _, row = _request(server_env["port"], "GET", native["path"], native["headers"])
    assert code == 200
    assert row["lifecycle"]["remove"] is True
    assert row["lifecycle"]["start"] is False and row["lifecycle"]["stop"] is False
    body = {**native["body"], "expected_run_id": row["run_id"], "expected_removal_identity": row["removal_identity"]}
    code, _, receipt = _request(server_env["port"], "DELETE", native["path"], native["headers"], body)
    assert code == 200 and receipt["status"] == "removed"
    assert receipt["storage_policy"] == "instance-root-and-topology-retained"
    archive = server_env["roots"].state / "retained-instances" / receipt["retained_instance"].split(":", 1)[1]
    assert (archive / "diffdisk").read_bytes() == b"native VM disk"
    assert not native["native"].exists()
    assert native["independent"].read_bytes() == b"independent disk"
    assert list(server_env["roots"].runs.iterdir()) == []
    _, _, inventory = _request(server_env["port"], "GET", "/api/v1/vms", native["headers"])
    assert inventory["vms"] == []


@pytest.mark.parametrize("change", ["name", "native-id", "run-id", "status", "backend-failure"])
def test_external_lima_removal_refuses_changed_or_unsupported_selection(server_env, external_lima, change):
    native = external_lima
    body = dict(native["body"])
    if change == "name":
        body["confirmation"] += " "
    elif change == "native-id":
        body["expected_removal_identity"] = "0" * 64
    elif change == "run-id":
        body["expected_run_id"] = str(uuid.uuid4())
    elif change == "status":
        native["instances"][0]["status"] = "Starting"
    else:
        native["fail_delete"] = True
    code, _, error = _request(server_env["port"], "DELETE", native["path"], native["headers"], body)
    assert code == 409 and "sensitive" not in json.dumps(error)
    assert (native["native"] / "diffdisk").read_bytes() == b"native VM disk"
    assert native["independent"].read_bytes() == b"independent disk"
    assert list(server_env["roots"].runs.iterdir()) == []


def test_external_lima_replacement_at_final_backend_check_preserves_both_vms(server_env, external_lima, monkeypatch):
    from palimpsest_local import lima

    native = external_lima["native"]
    held = native.with_name("original-held")
    original = lima._instance_info_or_none
    calls = 0

    def replaced(name):
        nonlocal calls
        calls += 1
        if calls == 2:
            native.rename(held)
            native.mkdir(mode=0o700)
            (native / "diffdisk").write_bytes(b"replacement VM disk")
        return original(name)

    monkeypatch.setattr(lima, "_instance_info_or_none", replaced)
    code, _, error = _request(
        server_env["port"], "DELETE", external_lima["path"], external_lima["headers"], external_lima["body"]
    )
    assert code == 409 and "identity changed" in error["error"]
    assert (held / "diffdisk").read_bytes() == b"native VM disk"
    assert (native / "diffdisk").read_bytes() == b"replacement VM disk"


@pytest.mark.parametrize("authority", ["missing-token", "foreign-origin", "cross-site"])
def test_external_vm_removal_preserves_http_authority(server_env, external_lima, authority):
    headers = dict(external_lima["headers"])
    if authority == "missing-token":
        headers.pop("Authorization")
    elif authority == "foreign-origin":
        headers["Origin"] = "https://foreign.invalid"
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    code, _, _ = _request(server_env["port"], "DELETE", external_lima["path"], headers, external_lima["body"])
    assert code == (401 if authority == "missing-token" else 403)
    assert (external_lima["native"] / "diffdisk").read_bytes() == b"native VM disk"


@pytest.mark.parametrize("server_env", [None], indirect=True)
def test_external_vm_removal_read_only_preserves_native_disk(server_env, external_lima):
    code, _, _ = _request(
        server_env["port"], "DELETE", external_lima["path"], external_lima["headers"], external_lima["body"]
    )
    assert code == 403
    assert (external_lima["native"] / "diffdisk").read_bytes() == b"native VM disk"


def test_external_vm_removal_does_not_adopt_or_delete_same_named_ledger(server_env, external_lima):
    native = external_lima["native"]
    run = server_env["roots"].runs / native.name
    run.mkdir(mode=0o700)
    run_id = str(uuid.uuid4())
    state.atomic_write_json(run / "owner.json", {"schema_version": 1, "name": native.name, "run_id": run_id})
    state.atomic_write_json(
        run / "state.json",
        {
            "schema_version": 2,
            "name": native.name,
            "run_id": run_id,
            "backend": "lima-vz",
            "runtime_kind": "cloud-image",
            "status": "running",
        },
    )
    code, _, error = _request(
        server_env["port"], "DELETE", external_lima["path"], external_lima["headers"], external_lima["body"]
    )
    assert code == 200 and error["status"] == "removed"
    archive = server_env["roots"].state / "retained-instances" / error["retained_instance"].split(":", 1)[1]
    assert (archive / "diffdisk").read_bytes() == b"native VM disk"
    assert not native.exists()
    assert state.read_run_ledger_snapshot(server_env["roots"], native.name).record.run_id == run_id


@pytest.mark.parametrize("confirmation", [None, "wrong", True])
def test_artifact_http_delete_requires_exact_digest_before_removing_bytes(server_env, confirmation):
    roots = server_env["roots"]
    from palimpsest_local.oci_layout import ContentStore

    store = ContentStore(roots.store)
    digest = f"sha256:{store.write_stream([b'keep until exact confirmation']).name}"
    headers = {"Authorization": f"Bearer {server_env['token']}", "Origin": server_env["origin"]}
    body = {} if confirmation is None else {"confirmation": confirmation}
    status, _, _ = _request(
        server_env["port"], "DELETE", f"/api/v1/store/artifacts/{digest}", headers=headers, body=body
    )
    assert status == 400
    assert store.exists(digest)
    status, _, _ = _request(
        server_env["port"],
        "DELETE",
        f"/api/v1/store/artifacts/{urllib.parse.quote(digest, safe='')}",
        headers=headers,
        body={"confirmation": digest},
    )
    assert status == 200
    assert not store.exists(digest)
