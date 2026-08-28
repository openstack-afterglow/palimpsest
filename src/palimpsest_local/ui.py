"""Local dashboard web UI server and request handler for Palimpsest."""

from __future__ import annotations

import json
import re
import secrets
import sys
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import inventory, platforms, runtime_dispatch, state
from .errors import PalimpsestError

WEBUI_DIR = Path(__file__).parent / "webui"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def build_handler(roots: state.StatePaths, *, token: str, origin: str) -> type[BaseHTTPRequestHandler]:
    """Factory creating a request handler bound to state paths, auth token, and origin."""

    active_roots = {"current": roots}

    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress default HTTP logging to keep output clean
            pass

        def _send_json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._send_json({"error": "Not Found"}, status=404)
                return
            content = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _authenticate(self) -> bool:
            auth_hdr = self.headers.get("Authorization", "")
            if auth_hdr.startswith("Bearer "):
                bearer_token = auth_hdr[7:].strip()
                if secrets.compare_digest(bearer_token, token):
                    return True
            parsed = urllib.parse.urlparse(self.path)
            if self.command == "GET" and parsed.path in ("/", "/index.html"):
                qs = urllib.parse.parse_qs(parsed.query)
                q_token = qs.get("token", [None])[0]
                if q_token and secrets.compare_digest(q_token, token):
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
            if not self._authenticate():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                self._send_file(WEBUI_DIR / "index.html", "text/html; charset=utf-8")
                return
            if path == "/app.js":
                self._send_file(WEBUI_DIR / "app.js", "application/javascript; charset=utf-8")
                return
            if path == "/app.css":
                self._send_file(WEBUI_DIR / "app.css", "text/css; charset=utf-8")
                return

            try:
                if path == "/api/v1/summary":
                    host_info = platforms.detect_host()
                    backends_info = {}
                    for b in platforms.BACKENDS:
                        try:
                            platforms.preflight(b, host=host_info)
                            backends_info[b] = {"available": True, "reason": None}
                        except PalimpsestError as e:
                            backends_info[b] = {"available": False, "reason": str(e)}
                    storage = inventory.storage_report(roots)
                    self._send_json(
                        {
                            "host": {"system": host_info.system, "machine": host_info.machine},
                            "backends": backends_info,
                            "storage": storage,
                        }
                    )
                    return

                if path == "/api/v1/vms":
                    res = inventory.list_vms(roots)
                    self._send_json(res)
                    return

                if path.startswith("/api/v1/vms/"):
                    rest = path[len("/api/v1/vms/") :]
                    if rest.endswith("/logs"):
                        name = rest[:-5]
                        if not NAME_RE.match(name):
                            self._send_json({"error": "invalid run name"}, status=400)
                            return
                        tail_str = qs.get("tail", ["400"])[0]
                        try:
                            tail = int(tail_str)
                        except ValueError:
                            self._send_json({"error": "invalid tail parameter"}, status=400)
                            return
                        lines = list(runtime_dispatch.logs(name, roots=roots, follow=False))
                        tail_lines = lines[-tail:] if tail > 0 else lines
                        self._send_json({"log": "".join(tail_lines)})
                        return
                    else:
                        name = rest
                        if not NAME_RE.match(name):
                            self._send_json({"error": "invalid run name"}, status=400)
                            return
                        res = inventory.get_vm(roots, name)
                        self._send_json(res)
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
            except PalimpsestError as err:
                self._send_json({"error": str(err)}, status=409)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json({"error": "Internal server error"}, status=500)

        def do_POST(self) -> None:
            roots = active_roots["current"]
            if not self._authenticate() or not self._check_csrf():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            try:
                if path.startswith("/api/v1/vms/"):
                    rest = path[len("/api/v1/vms/") :]
                    if rest.endswith("/stop"):
                        name = rest[:-5]
                        if not NAME_RE.match(name):
                            self._send_json({"error": "invalid run name"}, status=400)
                            return
                        res = runtime_dispatch.stop(name, roots=roots)
                        self._send_json(res)
                        return
                    if rest.endswith("/start"):
                        name = rest[:-6]
                        if not NAME_RE.match(name):
                            self._send_json({"error": "invalid run name"}, status=400)
                            return
                        res = runtime_dispatch.start(name, roots=roots)
                        self._send_json(res)
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
            except PalimpsestError as err:
                self._send_json({"error": str(err)}, status=409)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json({"error": "Internal server error"}, status=500)

        def do_DELETE(self) -> None:
            roots = active_roots["current"]
            if not self._authenticate() or not self._check_csrf():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            try:
                if path.startswith("/api/v1/vms/"):
                    name = path[len("/api/v1/vms/") :]
                    if not NAME_RE.match(name):
                        self._send_json({"error": "invalid run name"}, status=400)
                        return
                    volumes = qs.get("volumes", ["0"])[0].lower() in ("1", "true")
                    res = runtime_dispatch.rm(name, volumes=volumes, roots=roots)
                    self._send_json(res)
                    return

                if path.startswith("/api/v1/store/artifacts/"):
                    digest = path[len("/api/v1/store/artifacts/") :]
                    if not DIGEST_RE.match(digest):
                        self._send_json({"error": "invalid digest"}, status=400)
                        return
                    force = qs.get("force", ["0"])[0].lower() in ("1", "true")
                    res = inventory.remove_artifact(roots, digest, force=force)
                    self._send_json(res)
                    return

                self._send_json({"error": "Not Found"}, status=404)
            except PalimpsestError as err:
                self._send_json({"error": str(err)}, status=409)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json({"error": "Internal server error"}, status=500)

    return DashboardHandler


def serve(roots: state.StatePaths, *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> int:
    """Serve the local dashboard on loopback only and open a browser window."""
    token = secrets.token_urlsafe(32)
    # Server binds loopback 127.0.0.1 only regardless of caller host input
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), build_handler(roots, token=token, origin=f"http://127.0.0.1:{port}")
    )
    server.daemon_threads = True
    actual_port = server.server_address[1]

    if port == 0:
        server.RequestHandlerClass = build_handler(roots, token=token, origin=f"http://127.0.0.1:{actual_port}")

    url = f"http://127.0.0.1:{actual_port}/?token={token}"
    print(url)
    sys.stdout.flush()

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return actual_port
