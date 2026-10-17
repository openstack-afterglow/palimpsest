"""Local dashboard web UI server and request handler for Palimpsest."""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import signal
import socketserver
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import inventory, platforms, state
from .console import ConsoleError, ConsoleManager
from .errors import PalimpsestError
from .remote_artifacts import HubConnection
from .runtime_types import ProcessSignal
from .vm_exec_session import ExecOwners
from .web_operations import OperationManager

WEBUI_DIR = Path(__file__).parent / "webui"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_UI_LOG_LINES = 5_000


def _vm_identifier(encoded: str) -> str | None:
    """Decode one bounded VM ID for detail, consoles, or confirmed removal.

    Managed run names retain their existing syntax; host IDs identify an exact
    backend observation and may contain URL-encoded spaces or Unicode names.
    """
    if re.search(r"%(?![0-9a-fA-F]{2})", encoded):
        return None
    try:
        identifier = urllib.parse.unquote(encoded, errors="strict")
    except UnicodeDecodeError:
        return None
    if NAME_RE.fullmatch(identifier):
        return identifier
    parts = identifier.split(":", 2)
    if (
        len(identifier) <= 512
        and len(parts) == 3
        and parts[0] == "host"
        and parts[1] in platforms.BACKENDS
        and parts[2] not in {"", ".", ".."}
        and "/" not in parts[2]
        and "\\" not in parts[2]
        and not any(ord(char) < 32 or ord(char) == 127 for char in identifier)
    ):
        return identifier
    return None


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """Loopback dashboard server that binds without a reverse DNS lookup.

    ``HTTPServer.server_bind`` resolves ``socket.getfqdn(host)``, which blocked
    ``palimpsest web`` for longer than 15 seconds on macOS 15 hosts before the
    URL was printed. The dashboard never uses the resolved server name.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def build_handler(
    roots: state.StatePaths, *, token: str, origin: str, read_only: bool = True
) -> type[BaseHTTPRequestHandler]:
    """Bind the authenticated dashboard; mutations require explicit opt-in.

    Detail/logs/metrics/console accept managed names or decoded stable host IDs.
    Logs (tail 1..5000, default 200) and metrics return inventory projections
    unchanged and forward only the optional expected_run_id query identity.
    Console forwards that identity from JSON; start/stop remain managed-only.
    """

    active_roots = {"current": roots}
    control_lock = threading.RLock()
    operations = OperationManager()
    exec_owners = ExecOwners()
    consoles = ConsoleManager(exec_owners=exec_owners)

    class DashboardHandler(BaseHTTPRequestHandler):
        @staticmethod
        def close_transports() -> None:
            exec_owners.stop_admitting()
            try:
                consoles.close_all()
            finally:
                try:
                    operations.close_all()
                finally:
                    unresolved = exec_owners.wait()
                    if unresolved:
                        print(
                            f"Dashboard stopped: {unresolved} OCI exec acknowledgement(s) unconfirmed; "
                            "preserve original evidence and do not rerun",
                            file=sys.stderr,
                        )

        def _send_console_error(self, error: ConsoleError) -> None:
            status = {"invalid": 400, "missing": 404, "limit": 429}.get(error.category, 409)
            self._send_json({"error": str(error), "category": error.category}, status=status)

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress default HTTP logging to keep output clean
            pass

        def _send_response(self, body: bytes, content_type: str, status: int = 200) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The peer cancelled this response; there is no connection to reply to again.
                self.close_connection = True

        def _send_json(self, data: Any, status: int = 200) -> None:
            self._send_response(json.dumps(data).encode("utf-8"), "application/json", status)

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._send_json({"error": "Not Found"}, status=404)
                return
            self._send_response(path.read_bytes(), content_type)

        def _authenticate(self) -> bool:
            auth_hdr = self.headers.get("Authorization", "")
            if auth_hdr.startswith("Bearer "):
                bearer_token = auth_hdr[7:].strip()
                if secrets.compare_digest(bearer_token, token):
                    return True
            self._send_json({"error": "Unauthorized"}, status=401)
            return False

        def _check_csrf(self) -> bool:
            origin_hdr = self.headers.get("Origin")
            if origin_hdr is not None and origin_hdr != origin:
                self._send_json({"error": "Forbidden: invalid Origin"}, status=403)
                return False
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site is not None and fetch_site != "same-origin":
                self._send_json({"error": "Forbidden: Sec-Fetch-Site must be same-origin"}, status=403)
                return False
            return True

        def _check_writable(self) -> bool:
            if read_only:
                self._send_json(
                    {"error": "Dashboard is read-only; restart with --allow-control to enable changes"}, status=403
                )
                return False
            return True

        def _read_json_body(self) -> dict[str, Any] | None:
            content_length_str = self.headers.get("Content-Length", "0")
            try:
                content_length = int(content_length_str)
            except ValueError:
                self._send_json({"error": "Invalid Content-Length"}, status=400)
                return None
            if content_length < 0:
                self._send_json({"error": "Invalid Content-Length"}, status=400)
                return None
            if content_length > 65536:
                self._send_json({"error": "Request body exceeds 64 KiB cap"}, status=400)
                return None
            raw_body = self.rfile.read(content_length)
            if content_length > 0 and len(raw_body) < content_length:
                self._send_json({"error": "Truncated request body"}, status=400)
                return None
            if len(raw_body) > 65536:
                self._send_json({"error": "Request body exceeds 64 KiB cap"}, status=400)
                return None
            if not raw_body:
                return {}
            try:
                data = json.loads(raw_body.decode("utf-8"))
                if not isinstance(data, dict):
                    self._send_json({"error": "JSON body must be an object"}, status=400)
                    return None
                return data
            except Exception:
                self._send_json({"error": "Invalid JSON body"}, status=400)
                return None

        def do_GET(self) -> None:
            roots = active_roots["current"]
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            # The shell is static and credential-free; it bootstraps the tab-scoped token, and every asset/API needs it.
            if path in ("/", "/index.html"):
                self._send_file(WEBUI_DIR / "index.html", "text/html; charset=utf-8")
                return
            if not self._authenticate():
                return
            if path == "/app.js":
                self._send_file(WEBUI_DIR / "app.js", "application/javascript; charset=utf-8")
                return
            if path == "/app.css":
                self._send_file(WEBUI_DIR / "app.css", "text/css; charset=utf-8")
                return
            if path == "/vendor/xterm.js":
                self._send_file(WEBUI_DIR / "vendor" / "xterm.js", "application/javascript; charset=utf-8")
                return
            if path == "/vendor/xterm.css":
                self._send_file(WEBUI_DIR / "vendor" / "xterm.css", "text/css; charset=utf-8")
                return

            try:
                if path == "/api/v1/remotes":
                    self._send_json(
                        inventory.remote_connections(roots, hub_connection=HubConnection.from_headers(self.headers))
                    )
                    return
                if path == "/api/v1/remotes/hub/identity":
                    self._send_json(
                        inventory.remote_hub_identity(roots, hub_connection=HubConnection.from_headers(self.headers))
                    )
                    return
                if path == "/api/v1/remotes/artifact":
                    self._send_json(
                        inventory.inspect_remote_artifact(
                            roots,
                            connection_id=qs.get("connection", [""])[0],
                            reference=qs.get("reference", [""])[0],
                            manifest=qs.get("manifest", [None])[0],
                            hub_connection=HubConnection.from_headers(self.headers),
                        )
                    )
                    return
                if path == "/api/v1/remotes/artifacts":
                    limit_str = qs.get("limit", ["30"])[0]
                    try:
                        limit = int(limit_str)
                    except ValueError:
                        self._send_json({"error": "invalid remote result limit"}, status=400)
                        return
                    self._send_json(
                        inventory.browse_remote_artifacts(
                            roots,
                            connection_id=qs.get("connection", [""])[0],
                            kind=qs.get("kind", ["all"])[0],
                            query=qs.get("query", [""])[0],
                            repository=qs.get("repository", [""])[0],
                            limit=limit,
                            cursor=qs.get("cursor", [None])[0],
                            arch=qs.get("arch", [""])[0],
                            disk_format=qs.get("disk_format", [""])[0],
                            parent=qs.get("parent", [""])[0],
                            hub_connection=HubConnection.from_headers(self.headers),
                        )
                    )
                    return
                if path == "/api/v1/operations":
                    self._send_json({"operations": operations.list()})
                    return
                if path.startswith("/api/v1/consoles/"):
                    session_id = path[len("/api/v1/consoles/") :]
                    try:
                        after = int(qs.get("after", ["0"])[0])
                        wait = float(qs.get("wait", ["1"])[0])
                    except ValueError:
                        self._send_json({"error": "invalid console cursor or wait"}, status=400)
                        return
                    self._send_json(consoles.read(session_id, after=after, wait_seconds=wait))
                    return
                if path == "/api/v1/summary":
                    host_info = platforms.detect_host()
                    backends_info = {}
                    for b in platforms.BACKENDS:
                        try:
                            report = platforms.backend_capability_report(b, host=host_info)
                            failure = next((item for item in report.checks if not item.passed), None)
                            backends_info[b] = {
                                "available": report.successful,
                                "reason": failure.remediation if failure is not None else None,
                                "profile": report.profile.profile_id,
                            }
                        except PalimpsestError as e:
                            backends_info[b] = {"available": False, "reason": str(e), "profile": None}
                    self._send_json(
                        {
                            "host": {"system": host_info.system, "machine": host_info.machine},
                            "backends": backends_info,
                            "read_only": read_only,
                        }
                    )
                    return

                if path == "/api/v1/vms":
                    res = inventory.list_vms(roots)
                    self._send_json(res)
                    return

                if path == "/api/v1/volumes":
                    self._send_json(inventory.list_volumes(roots))
                    return

                if path == "/api/v1/networks":
                    self._send_json(inventory.list_networks(roots))
                    return

                if path.startswith("/api/v1/vms/"):
                    rest = path[len("/api/v1/vms/") :]
                    resource = "logs" if rest.endswith("/logs") else "metrics" if rest.endswith("/metrics") else None
                    encoded = rest[: -(len(resource) + 1)] if resource else rest
                    identifier = _vm_identifier(encoded)
                    if identifier is None:
                        self._send_json({"error": "invalid VM identifier"}, status=400)
                        return
                    if resource == "logs":
                        tail_str = qs.get("tail", ["200"])[0]
                        try:
                            tail = int(tail_str)
                        except ValueError:
                            self._send_json({"error": "invalid tail parameter"}, status=400)
                            return
                        if tail < 1 or tail > _MAX_UI_LOG_LINES:
                            self._send_json({"error": "tail parameter is out of range"}, status=400)
                            return
                        self._send_json(
                            inventory.vm_logs(
                                roots,
                                identifier,
                                tail=tail,
                                expected_run_id=qs.get("expected_run_id", [None])[0],
                                exec_owners=exec_owners,
                            )
                        )
                    elif resource == "metrics":
                        self._send_json(
                            inventory.vm_metrics(
                                roots,
                                identifier,
                                expected_run_id=qs.get("expected_run_id", [None])[0],
                                exec_owners=exec_owners,
                            )
                        )
                    else:
                        self._send_json(inventory.get_vm(roots, identifier))
                    return

                if path == "/api/v1/store/artifacts":
                    res = inventory.list_artifacts(roots)
                    self._send_json(res)
                    return

                if path == "/api/v1/builds":
                    limit_str = qs.get("limit", ["100"])[0]
                    try:
                        limit = int(limit_str)
                    except ValueError:
                        self._send_json({"error": "invalid limit parameter"}, status=400)
                        return
                    res = inventory.list_builds(roots, limit=limit)
                    self._send_json(res)
                    return

                if path.startswith("/api/v1/builds/"):
                    rest = path[len("/api/v1/builds/") :]
                    if rest.endswith("/log"):
                        build_id = rest[:-4]
                        if inventory.BUILD_ID_RE.fullmatch(build_id) is None:
                            self._send_json({"error": "invalid build id"}, status=400)
                            return
                        tail_str = qs.get("tail", ["400"])[0]
                        try:
                            tail = int(tail_str)
                        except ValueError:
                            self._send_json({"error": "invalid tail parameter"}, status=400)
                            return
                        log_text = inventory.build_log(roots, build_id, tail=tail)
                        self._send_json({"log": log_text})
                        return
                    else:
                        build_id = rest
                        if inventory.BUILD_ID_RE.fullmatch(build_id) is None:
                            self._send_json({"error": "invalid build id"}, status=400)
                            return
                        res = inventory.get_build(roots, build_id)
                        self._send_json(res)
                        return

                if path == "/api/v1/storage":
                    res = inventory.storage_report(roots)
                    self._send_json(res)
                    return

                self._send_json({"error": "Not Found"}, status=404)
            except ConsoleError as err:
                self._send_console_error(err)
            except PalimpsestError as err:
                self._send_json({"error": str(err)}, status=409)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json({"error": "Internal server error"}, status=500)

        def do_POST(self) -> None:
            if not self._authenticate() or not self._check_csrf() or not self._check_writable():
                return
            self._post_started = time.monotonic()
            with control_lock:
                self._post_control()

        def _post_control(self) -> None:
            roots = active_roots["current"]
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            try:
                if path == "/api/v1/transfers":
                    body = self._read_json_body()
                    if body is None:
                        return
                    self._send_json(
                        inventory.transfer_artifact(
                            roots,
                            operations,
                            body,
                            hub_connection=HubConnection.from_headers(self.headers),
                        ),
                        status=202,
                    )
                    return
                if path in {"/api/v1/builds", "/api/v1/vms"}:
                    body = self._read_json_body()
                    if body is None:
                        return
                    launch = inventory.build_image if path == "/api/v1/builds" else inventory.run_image
                    self._send_json(launch(roots, operations, body), status=202)
                    return
                if path == "/api/v1/volumes":
                    body = self._read_json_body()
                    if body is None:
                        return
                    self._send_json(inventory.create_volume(roots, body), status=201)
                    return
                if path.startswith("/api/v1/consoles/"):
                    rest = path[len("/api/v1/consoles/") :]
                    session_id, _, action = rest.partition("/")
                    body = self._read_json_body()
                    if body is None:
                        return
                    if action == "input":
                        encoded = body.get("data")
                        if not isinstance(encoded, str):
                            self._send_json({"error": "data must be base64"}, status=400)
                            return
                        try:
                            data = base64.b64decode(encoded, validate=True)
                        except (ValueError, binascii.Error):
                            self._send_json({"error": "data must be base64"}, status=400)
                            return
                        consoles.write(session_id, data)
                    elif action == "resize":
                        consoles.resize(session_id, body.get("rows"), body.get("columns"))
                    elif action == "signal":
                        try:
                            requested_signal = ProcessSignal(body.get("signal"))
                        except (ValueError, TypeError):
                            self._send_json({"error": "invalid console signal"}, status=400)
                            return
                        consoles.signal(session_id, requested_signal)
                    else:
                        self._send_json({"error": "Not Found"}, status=404)
                        return
                    self._send_json({"id": session_id, "status": "accepted"})
                    return
                if path.startswith("/api/v1/vms/"):
                    rest = path[len("/api/v1/vms/") :]
                    if rest.endswith("/console"):
                        identifier = _vm_identifier(rest[:-8])
                        if identifier is None:
                            self._send_json({"error": "invalid VM identifier"}, status=400)
                            return
                        body = self._read_json_body()
                        if body is None:
                            return
                        # A queued request cannot outlive its cancellation lease.
                        if time.monotonic() - self._post_started >= ConsoleManager.IDLE_SECONDS:
                            raise ConsoleError("closed", "Console open request expired")
                        self._send_json(
                            consoles.open(
                                roots,
                                identifier,
                                mode=body.get("mode", "shell"),
                                argv=body.get("argv", []),
                                session_id=body.get("id"),
                                expected_run_id=body.get("expected_run_id"),
                            ),
                            status=201,
                        )
                        return
                    for action in ("stop", "start"):
                        if rest.endswith("/" + action):
                            name = rest[: -(len(action) + 1)]
                            if NAME_RE.fullmatch(name) is None:
                                self._send_json({"error": "invalid run name"}, status=400)
                                return
                            body = self._read_json_body()
                            if body is None:
                                return
                            if set(body) - {"expected_run_id"}:
                                self._send_json({"error": "unsupported lifecycle field"}, status=400)
                                return
                            self._send_json(
                                inventory.vm_lifecycle(
                                    roots,
                                    name,
                                    action,
                                    expected_run_id=body.get("expected_run_id"),
                                )
                            )
                            return

                if path.startswith("/api/v1/store/artifacts/") and path.endswith("/tag"):
                    digest = path[len("/api/v1/store/artifacts/") : -4]
                    if DIGEST_RE.fullmatch(digest) is None:
                        self._send_json({"error": "invalid digest"}, status=400)
                        return
                    body = self._read_json_body()
                    if body is None:
                        return
                    if not isinstance(body.get("tag"), str) or not body["tag"]:
                        self._send_json({"error": "missing or invalid field 'tag'"}, status=400)
                        return
                    result = inventory.tag_artifact(roots, digest, tag=body["tag"])
                    self._send_json({"digest": result["digest"], "tag": result["tag"]})
                    return

                if path == "/api/v1/store/import":
                    body = self._read_json_body()
                    if body is None:
                        return
                    for field in ("path", "disk_format", "arch"):
                        if field not in body or not isinstance(body[field], str):
                            self._send_json({"error": f"missing or invalid field '{field}'"}, status=400)
                            return
                    res = inventory.import_cloud_image(
                        roots,
                        path=Path(body["path"]),
                        disk_format=body["disk_format"],
                        arch=body["arch"],
                        os_variant=body.get("os_variant"),
                    )
                    self._send_json(res)
                    return

                if path == "/api/v1/storage/move":
                    if operations.has_active() or consoles.has_active():
                        self._send_json(
                            {"error": "Disconnect consoles and wait for active operations before relocating storage"},
                            status=409,
                        )
                        return
                    body = self._read_json_body()
                    if body is None:
                        return
                    if "destination" not in body or not isinstance(body["destination"], str):
                        self._send_json({"error": "missing field 'destination'"}, status=400)
                        return
                    res = inventory.move_state_root(
                        roots,
                        destination=Path(body["destination"]),
                        keep_source=bool(body.get("keep_source", False)),
                    )
                    new_roots = state.init_roots({"XDG_CONFIG_HOME": str(roots.config.parent)})
                    active_roots["current"] = new_roots
                    self._send_json(res)
                    return

                if path == "/api/v1/storage/set":
                    if operations.has_active() or consoles.has_active():
                        self._send_json(
                            {"error": "Disconnect consoles and wait for active operations before changing storage"},
                            status=409,
                        )
                        return
                    body = self._read_json_body()
                    if body is None:
                        return
                    if "destination" not in body or not isinstance(body["destination"], str):
                        self._send_json({"error": "missing field 'destination'"}, status=400)
                        return
                    res = inventory.set_state_root(roots, destination=Path(body["destination"]))
                    new_roots = state.init_roots({"XDG_CONFIG_HOME": str(roots.config.parent)})
                    active_roots["current"] = new_roots
                    self._send_json(res)
                    return

                self._send_json({"error": "Not Found"}, status=404)
            except ConsoleError as err:
                self._send_console_error(err)
            except PalimpsestError as err:
                self._send_json({"error": str(err)}, status=409)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json({"error": "Internal server error"}, status=500)

        def do_DELETE(self) -> None:
            if not self._authenticate() or not self._check_csrf() or not self._check_writable():
                return
            with control_lock:
                self._delete_control()

        def _delete_control(self) -> None:
            roots = active_roots["current"]
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            try:
                if path.startswith("/api/v1/consoles/"):
                    session_id = path[len("/api/v1/consoles/") :]
                    consoles.close(session_id)
                    self._send_json({"id": session_id, "status": "disconnected"})
                    return
                if path.startswith("/api/v1/vms/"):
                    name = _vm_identifier(path[len("/api/v1/vms/") :])
                    if name is None:
                        self._send_json({"error": "invalid VM inventory identifier"}, status=400)
                        return
                    body = self._read_json_body()
                    if body is None:
                        return
                    if (
                        set(body) - {"expected_run_id", "expected_removal_identity", "confirmation", "volumes"}
                        or "volumes" in qs
                        or (name.startswith("host:") and "expected_removal_identity" not in body)
                    ):
                        self._send_json(
                            {"error": "VM removal requires JSON identity, confirmation and boolean volumes"}, status=400
                        )
                        return
                    self._send_json(
                        inventory.remove_vm(
                            roots,
                            name,
                            expected_run_id=body.get("expected_run_id"),
                            expected_removal_identity=body.get("expected_removal_identity"),
                            confirmation=body.get("confirmation"),
                            volumes=body.get("volumes", False),
                        )
                    )
                    return

                if path.startswith("/api/v1/volumes/"):
                    volume_id = urllib.parse.unquote(path[len("/api/v1/volumes/") :])
                    body = self._read_json_body()
                    if body is None:
                        return
                    if set(body) != {"confirmation"}:
                        self._send_json({"error": "volume deletion requires exact-name confirmation"}, status=400)
                        return
                    self._send_json(inventory.delete_volume(roots, volume_id, confirmation=body["confirmation"]))
                    return
                if path.startswith("/api/v1/store/artifacts/"):
                    digest = urllib.parse.unquote(path[len("/api/v1/store/artifacts/") :])
                    if not DIGEST_RE.match(digest):
                        self._send_json({"error": "invalid digest"}, status=400)
                        return
                    force = qs.get("force", ["0"])[0].lower() in ("1", "true")
                    body = self._read_json_body() if self.headers.get("Content-Length") else {}
                    if body is None:
                        return
                    if set(body) != {"confirmation"} or body["confirmation"] != digest:
                        self._send_json({"error": "confirmation must exactly match the artifact digest"}, status=400)
                        return
                    res = inventory.remove_artifact(roots, digest, force=force)
                    self._send_json(res)
                    return

                self._send_json({"error": "Not Found"}, status=404)
            except ConsoleError as err:
                self._send_console_error(err)
            except PalimpsestError as err:
                self._send_json({"error": str(err)}, status=409)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json({"error": "Internal server error"}, status=500)

    return DashboardHandler


def serve(
    roots: state.StatePaths,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    read_only: bool = True,
) -> int:
    """Serve a foreground, read-only-by-default dashboard on IPv4 loopback only."""
    token = secrets.token_urlsafe(32)
    # Server binds loopback 127.0.0.1 only regardless of caller host input
    server = _LoopbackHTTPServer(
        ("127.0.0.1", port), build_handler(roots, token=token, origin=f"http://127.0.0.1:{port}", read_only=read_only)
    )
    server.daemon_threads = True
    actual_port = server.server_address[1]

    if port == 0:
        server.RequestHandlerClass = build_handler(
            roots, token=token, origin=f"http://127.0.0.1:{actual_port}", read_only=read_only
        )

    url = f"http://127.0.0.1:{actual_port}/?token={token}"
    print(url)
    sys.stdout.flush()

    def stop_requested(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    def launch_browser() -> None:
        try:
            opened = webbrowser.open(url)
        except (webbrowser.Error, OSError):
            opened = False
        if not opened:
            print("Browser unavailable; open the printed URL manually.", file=sys.stderr)

    main_thread = threading.current_thread() is threading.main_thread()
    previous_sigterm = signal.getsignal(signal.SIGTERM) if main_thread else None
    try:
        if main_thread:
            signal.signal(signal.SIGTERM, stop_requested)
        if open_browser:
            # GenericBrowser can wait for a foreground browser to exit.
            threading.Thread(target=launch_browser, name="palimpsest-web-browser", daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.RequestHandlerClass.close_transports()
        finally:
            try:
                if main_thread:
                    signal.signal(signal.SIGTERM, previous_sigterm)
            finally:
                server.server_close()

    return actual_port
