"""Experimental, controller-enrolled Nova bootstrap over loopback mutual TLS.

Enrollment is an operator/controller assertion, not independent Nova attestation.
The controller must verify the exact server, project, image, port and plan before
publishing an enrollment. Ceph credentials never belong in a carrier or argv.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any

CONFIG_SCHEMA = "palimpsest.nova-bootstrap-service.v1"
ENROLLMENT_SCHEMA = "palimpsest.nova-enrollment.v1"
BOOTSTRAP_SCHEMA = "palimpsest.nova-bootstrap.v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_ENROLLMENT_BYTES = 256 * 1024
_RUN = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CONFIG_KEYS = {"schema", "bind", "port", "cert_file", "key_file", "client_ca_file", "enrollments_dir", "results_dir"}
_ENROLLMENT_KEYS = {"schema", "run_id", "client_cert_sha256", "expires_unix", "revoked", "plan", "ceph_access"}


class BootstrapError(RuntimeError):
    """A deliberately non-sensitive local configuration or persistence error."""


class _Denied(Exception):
    def __init__(self, status: int):
        self.status = status


def _require(condition: bool) -> None:
    if not condition:
        raise BootstrapError("invalid bootstrap configuration or state")


def _matches(pattern: re.Pattern, value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _text(value: Any, maximum: int = 4096) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _exact(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _path(value: Any) -> Path:
    _require(_text(value) and value.startswith("/") and "//" not in value)
    path = Path(value)
    _require(all(part not in (".", "..") for part in value.split("/")[1:]) and str(path) == value)
    return path


def _private_info(info: os.stat_result, *, directory: bool = False) -> None:
    _require(info.st_uid == os.geteuid())
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    _require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600))
    if not directory:
        _require(info.st_nlink == 1)


def _open_directory(path: Path) -> int:
    """Walk every component without following links; hold the final private dir."""
    path = _path(str(path))
    descriptor = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        _private_info(os.fstat(descriptor), directory=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_at(directory: int, name: str, maximum: int) -> bytes:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        _private_info(before)
        _require(before.st_size <= maximum)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        _require(len(content) <= maximum and len(content) == before.st_size)
        _require(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        )
        _private_info(after)
        return content
    finally:
        os.close(descriptor)


def _read_private(path: Path, maximum: int = MAX_ENROLLMENT_BYTES) -> bytes:
    directory = _open_directory(path.parent)
    try:
        return _read_at(directory, path.name, maximum)
    finally:
        os.close(directory)


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _bounded(value: Any, depth: int = 0) -> None:
    _require(depth <= 24)
    if isinstance(value, dict):
        _require(len(value) <= 256)
        for key, child in value.items():
            _require(_text(key, 128))
            _bounded(child, depth + 1)
    elif isinstance(value, list):
        _require(len(value) <= 1024)
        for child in value:
            _bounded(child, depth + 1)
    elif isinstance(value, str):
        _require(len(value) <= 65536 and "\x00" not in value)
    else:
        _require(value is None or type(value) in (bool, int, float))
        if type(value) is int:
            _require(abs(value) < 2**63)
        if type(value) is float:
            _require(float("-inf") < value < float("inf"))


def _decode(content: bytes) -> dict:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(BootstrapError("invalid JSON")),
        )
        _require(isinstance(value, dict))
        _bounded(value)
        return value
    except (ValueError, UnicodeError, RecursionError, BootstrapError):
        raise BootstrapError("invalid bootstrap JSON") from None


def _encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")


def validate_plan(plan: Any, run_id: str) -> None:
    """Validate asserted identity and bounded shape, not the assertion's truth."""
    keys = {"run_id", "server_id", "project_id", "carrier_digest", "image", "network", "upper", "gpu", "command"}
    _require(isinstance(plan, dict) and set(plan) in (keys, keys | {"storage"}))
    if "storage" in plan:
        storage = plan["storage"]
        _require(_exact(storage, {"export", "fs_name", "data_pool", "namespace"}))
        _require(_text(storage["export"]))
        for field, maximum in (("fs_name", 64), ("data_pool", 128), ("namespace", 128)):
            _require(
                isinstance(storage[field], str)
                and len(storage[field]) <= maximum
                and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", storage[field]) is not None
            )
    _require(plan["run_id"] == run_id and _matches(_ID, plan["server_id"]) and _matches(_ID, plan["project_id"]))
    _require(_matches(_DIGEST, plan["carrier_digest"]))
    image = plan["image"]
    _require(_exact(image, {"source", "runtime", "layers"}))
    _require(all(isinstance(image[key], (str, dict)) and bool(image[key]) for key in ("source", "runtime")))
    _require(isinstance(image["layers"], list) and 0 < len(image["layers"]) <= 128)
    _require(all(isinstance(layer, (str, dict)) and bool(layer) for layer in image["layers"]))
    network = plan["network"]
    _require(_exact(network, {"address", "cidr", "gateway", "dns"}))
    try:
        address = ipaddress.IPv4Address(network["address"])
        subnet = ipaddress.IPv4Network(network["cidr"], strict=False)
        gateway = ipaddress.IPv4Address(network["gateway"])
        _require(address in subnet and gateway in subnet)
        _require(isinstance(network["dns"], list) and 0 < len(network["dns"]) <= 8)
        for item in network["dns"]:
            ipaddress.IPv4Address(item)
    except (ValueError, TypeError):
        raise BootstrapError("invalid bootstrap network plan") from None
    _require(plan["upper"] == {"label": "PAL-UPPER"})
    gpu = plan["gpu"]
    _require(_exact(gpu, {"enabled", "kernel_release", "driver_version", "expected_pci_alias"}))
    _require(type(gpu["enabled"]) is bool)
    _require(all(_text(gpu[key], 128) for key in ("kernel_release", "driver_version", "expected_pci_alias")))
    _require(isinstance(plan["command"], list) and 0 < len(plan["command"]) <= 128)
    _require(all(isinstance(item, str) and 0 < len(item) <= 8192 and "\x00" not in item for item in plan["command"]))
    _bounded(plan)


def _validate_enrollment(value: dict, run_id: str) -> None:
    _require(_exact(value, _ENROLLMENT_KEYS) and value["schema"] == ENROLLMENT_SCHEMA and value["run_id"] == run_id)
    _require(_matches(_HEX, value["client_cert_sha256"]))
    _require(type(value["expires_unix"]) is int and 0 < value["expires_unix"] < 2**53)
    _require(type(value["revoked"]) is bool)
    validate_plan(value["plan"], run_id)
    access = value["ceph_access"]
    _require(_exact(access, {"export", "name", "secret"}) or _exact(access, {"export", "name", "secret", "fs_name"}))
    _require(all(_text(access[key]) for key in access))
    if "fs_name" in access:
        _require(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", access["fs_name"]) is not None)
    if "storage" in value["plan"]:
        _require(access.get("export") == value["plan"]["storage"]["export"])
        _require(access.get("fs_name") == value["plan"]["storage"]["fs_name"])


def _load_config(path: Path) -> dict:
    value = _decode(_read_private(path, 16384))
    _require(_exact(value, _CONFIG_KEYS) and value["schema"] == CONFIG_SCHEMA)
    _require(value["bind"] == "127.0.0.1" and type(value["port"]) is int and value["port"] == 9443)
    for key in ("cert_file", "key_file", "client_ca_file", "enrollments_dir", "results_dir"):
        _path(value[key])
    _require(value["enrollments_dir"] != value["results_dir"])
    return value


def _publish(directory: int, name: str, content: bytes) -> None:
    """Durably publish a complete immutable file, never replacing an existing name."""
    temporary = ".bootstrap-" + secrets.token_hex(16)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(descriptor)
        _private_info(os.fstat(descriptor))
        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def _run_lock(directory: int, run_id: str):
    descriptor = os.open(
        run_id + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory
    )
    try:
        _private_info(os.fstat(descriptor))
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


class BootstrapHTTPServer(ThreadingHTTPServer):
    """Loopback-only server; all application errors intentionally omit details."""

    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, config_path: str | Path, *, _listen_port: int | None = None):
        self.config_path = _path(str(config_path))
        self.config = _load_config(self.config_path)
        # Internal test seam: production configuration and CLI remain fixed at
        # 127.0.0.1:9443; local TLS fixtures may request a kernel-selected port.
        _require(_listen_port is None or (type(_listen_port) is int and _listen_port == 0))
        self._directories: dict[str, int] = {}
        self._active_lock = threading.Lock()
        self._active_requests: dict[ssl.SSLSocket, threading.Timer] = {}
        self._closing = False
        try:
            for key in ("enrollments_dir", "results_dir"):
                self._directories[key] = _open_directory(Path(self.config[key]))
            for key in ("cert_file", "key_file", "client_ca_file"):
                _read_private(Path(self.config[key]))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_cert_chain(self.config["cert_file"], self.config["key_file"])
            context.load_verify_locations(cafile=self.config["client_ca_file"])
            self.tls_context = context
            super().__init__(
                (self.config["bind"], self.config["port"] if _listen_port is None else _listen_port), _Handler
            )
        except Exception:
            for descriptor in self._directories.values():
                os.close(descriptor)
            self._directories.clear()
            raise BootstrapError("bootstrap service initialization failed") from None

    def server_bind(self):
        # HTTPServer resolves its display name with getfqdn(), which can block
        # on the host resolver. This fixed numeric loopback listener needs no DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def get_request(self):
        connection, address = super().get_request()
        # Handshake in the request thread, so an idle TLS peer cannot block accept.
        connection.settimeout(15)
        secured = None
        try:
            secured = self.tls_context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
            timer = threading.Timer(15, self._abort_request, args=(secured,))
            timer.daemon = True
            with self._active_lock:
                if self._closing:
                    secured.close()
                    raise OSError("bootstrap listener is closing")
                self._active_requests[secured] = timer
            timer.start()
            return secured, address
        except Exception:
            if secured is not None:
                with self._active_lock:
                    timer = self._active_requests.pop(secured, None)
                if timer is not None:
                    timer.cancel()
                secured.close()
            connection.close()
            raise

    @staticmethod
    def _abort_request(request):
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                timer = self._active_requests.pop(request, None)
            if timer is not None:
                timer.cancel()

    def handle_error(self, request, client_address):
        # BaseServer's traceback can include request material. Never log it.
        pass

    def server_close(self):
        # Stop active TLS/header/body reads before ThreadingMixIn joins handlers.
        # Socket inactivity timeouts alone do not bound a peer that keeps dripping.
        with self._active_lock:
            self._closing = True
            active = list(self._active_requests)
        for request in active:
            self._abort_request(request)
        super().server_close()
        for descriptor in self._directories.values():
            os.close(descriptor)
        self._directories.clear()

    def _recheck(self) -> None:
        _require(_load_config(self.config_path) == self.config)
        for key, held in self._directories.items():
            current = _open_directory(Path(self.config[key]))
            try:
                a, b = os.fstat(current), os.fstat(held)
                _require((a.st_dev, a.st_ino) == (b.st_dev, b.st_ino))
                _private_info(b, directory=True)
            finally:
                os.close(current)

    def _admit(self, run_id: str, fingerprint: str) -> dict:
        self._recheck()
        try:
            content = _read_at(self._directories["enrollments_dir"], run_id + ".json", MAX_ENROLLMENT_BYTES)
        except FileNotFoundError:
            # The controller publishes only after fresh server/port binding.
            # mTLS admission has already succeeded; this exact leaf is pending.
            raise _Denied(404) from None
        enrollment = _decode(content)
        _validate_enrollment(enrollment, run_id)
        if (
            enrollment["client_cert_sha256"] != fingerprint
            or enrollment["revoked"]
            or enrollment["expires_unix"] <= time.time()
        ):
            raise _Denied(403)
        return enrollment

    def transact(self, endpoint: str, run_id: str, fingerprint: str, body: bytes) -> dict:
        _require(_matches(_RUN, run_id))
        payload = _decode(body)
        if not _exact(payload, {"nonce"} if endpoint == "bootstrap" else {"nonce", "result"}):
            raise _Denied(400)
        if not _matches(_NONCE, payload["nonce"]):
            raise _Denied(400)
        if endpoint == "results" and not isinstance(payload["result"], dict):
            raise _Denied(400)
        # A CA-valid but unenrolled/revoked peer must not allocate shared state.
        self._admit(run_id, fingerprint)
        state = self._directories["results_dir"]
        with _run_lock(state, run_id):
            # Reload under the lock for every request, including all retries.
            enrollment = self._admit(run_id, fingerprint)
            response = {
                "schema": BOOTSTRAP_SCHEMA,
                "run_id": run_id,
                "nonce": payload["nonce"],
                "expires_unix": enrollment["expires_unix"],
                "plan": enrollment["plan"],
                "ceph_access": enrollment["ceph_access"],
            }
            journal = {
                "schema": "palimpsest.nova-bootstrap-delivery.v1",
                "run_id": run_id,
                "nonce": payload["nonce"],
                "client_cert_sha256": fingerprint,
                "enrollment_sha256": hashlib.sha256(_encode(enrollment)).hexdigest(),
                "response_sha256": hashlib.sha256(_encode(response)).hexdigest(),
            }
            delivery_name = run_id + ".delivery.json"
            try:
                delivered = _decode(_read_at(state, delivery_name, 16384))
            except FileNotFoundError:
                delivered = None
            if delivered is None:
                if endpoint != "bootstrap":
                    raise _Denied(409)
                _publish(state, delivery_name, _encode(journal))
            elif delivered != journal:
                raise _Denied(409)
            os.fsync(state)
            if endpoint == "bootstrap":
                return response
            result_name = run_id + ".result.json"
            request_digest = hashlib.sha256(body).hexdigest()
            try:
                previous = _decode(_read_at(state, result_name, MAX_JSON_BYTES * 2))
            except FileNotFoundError:
                previous = None
            if previous is not None:
                _require(_exact(previous, {"schema", "run_id", "nonce", "request_sha256", "received_unix", "result"}))
                _require(previous["schema"] == "palimpsest.nova-bootstrap-result.v1" and previous["run_id"] == run_id)
                _require(_matches(_NONCE, previous["nonce"]) and _matches(_HEX, previous["request_sha256"]))
                _require(
                    type(previous["received_unix"]) is int
                    and previous["received_unix"] > 0
                    and isinstance(previous["result"], dict)
                )
                if previous["request_sha256"] != request_digest:
                    raise _Denied(409)
                _require(previous["nonce"] == payload["nonce"] and previous["result"] == payload["result"])
            else:
                result = {
                    "schema": "palimpsest.nova-bootstrap-result.v1",
                    "run_id": run_id,
                    "nonce": payload["nonce"],
                    "request_sha256": request_digest,
                    "received_unix": int(time.time()),
                    "result": payload["result"],
                }
                _publish(state, result_name, _encode(result))
            os.fsync(state)
            return {"schema": "palimpsest.nova-bootstrap-result-ack.v1", "run_id": run_id, "accepted": True}


class _Handler(BaseHTTPRequestHandler):
    server: BootstrapHTTPServer
    protocol_version = "HTTP/1.0"
    server_version = "PalimpsestBootstrap"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.do_handshake()

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        # Also redact BaseHTTPRequestHandler's parse/header errors.
        self._reply(code, {"error": "request rejected"})

    def _reply(self, status: int, value: dict):
        content = _encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)
        self.close_connection = True

    def do_POST(self):
        try:
            match = re.fullmatch(r"/v1/(bootstrap|results)/([a-z0-9][a-z0-9_-]{0,95})", self.path)
            if match is None:
                raise _Denied(404)
            if self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Content-Encoding"):
                raise _Denied(400)
            lengths = self.headers.get_all("Content-Length", [])
            types = self.headers.get_all("Content-Type", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,7}", lengths[0]) or types != ["application/json"]:
                raise _Denied(400)
            length = int(lengths[0])
            if not 0 < length <= (4096 if match[1] == "bootstrap" else MAX_JSON_BYTES):
                raise _Denied(413)
            body = self.rfile.read(length)
            if len(body) != length:
                raise _Denied(400)
            certificate = self.connection.getpeercert(binary_form=True)
            if not certificate:
                raise _Denied(403)
            value = self.server.transact(match[1], match[2], hashlib.sha256(certificate).hexdigest(), body)
            self._reply(200, value)
        except _Denied as error:
            self._reply(error.status, {"error": "request rejected"})
        except Exception:
            self._reply(503, {"error": "bootstrap unavailable"})


def create_server(config_path: str | Path) -> BootstrapHTTPServer:
    """Construct without starting threads; caller must close the returned server."""
    try:
        return BootstrapHTTPServer(config_path)
    except Exception:
        raise BootstrapError("bootstrap service initialization failed") from None


def serve(config_path: str | Path) -> None:
    """Serve until SIGTERM/SIGINT, then perform ordinary HTTP server shutdown."""
    server = create_server(config_path)
    stop = threading.Event()
    previous = {}

    def terminate(signum, frame):
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, terminate)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="nova-bootstrap")
    try:
        thread.start()
        while thread.is_alive() and not stop.wait(0.2):
            pass
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def certificate_fingerprint(cert_file: str | Path) -> str:
    """Return lowercase SHA256 of the certificate DER, never a key fingerprint."""
    try:
        pem = _read_private(_path(str(cert_file))).decode("ascii")
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    except Exception:
        raise BootstrapError("certificate fingerprint failed") from None


class CertificateProcessHold(BootstrapError):
    """TERM did not establish a reaped child; retain artifacts and owned handles.

    The caller owns ``process``, ``directory_fd`` and ``staging_directory_fd``
    after this error. Do not remove staging or close its recovery handles until
    the child is confirmed reaped. No automatic escalation to SIGKILL occurs.
    """

    def __init__(self, process):
        super().__init__("certificate child disposition unknown; private artifacts retained")
        self.process = process
        self.directory_fd: int | None = None
        self.staging_directory_fd: int | None = None
        self.staging_directory: Path | None = None


def _openssl(arguments: list[str]) -> None:
    process = None
    try:
        process = subprocess.Popen(
            ["openssl", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            umask=0o077,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
                "LANG": "C",
                "OPENSSL_CONF": "/dev/null",
            },
        )
        returncode = process.wait(timeout=120)
    except BaseException:
        if process is not None:
            try:
                process.terminate()
            except BaseException:
                # A failed TERM is not evidence of exit. Only wait establishes it.
                pass
            try:
                process.wait(timeout=10)
            except BaseException:
                raise CertificateProcessHold(process) from None
        raise BootstrapError("certificate issuance failed") from None
    _require(returncode == 0)


def _certificate_output(
    directory: str | Path,
    prefix: str,
    ca_cert_file: str | Path | None,
    ca_key_file: str | Path | None,
    common_name: str,
    extensions: str,
    days: int,
) -> dict:
    """Issue into an existing private dir; all OpenSSL secret inputs are paths."""
    staging: Path | None = None
    held: int | None = None
    stage: int | None = None
    preserve = False
    try:
        root = _path(str(directory))
        held = _open_directory(root)
        _require(_text(common_name, 128) and re.fullmatch(r"[A-Za-z0-9._-]+", common_name) is not None)
        _require(type(days) is int and 1 <= days <= 3650)
        for suffix in (".pem", ".key"):
            try:
                os.stat(prefix + suffix, dir_fd=held, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise BootstrapError("certificate output already exists")
        candidate = root / (".issue-" + secrets.token_hex(16))
        os.mkdir(candidate, 0o700)
        staging = candidate
        stage = _open_directory(staging)
        _publish(stage, "extensions.cnf", extensions.encode("ascii"))
        _publish(stage, "request.cnf", b"[req]\ndistinguished_name=dn\n[dn]\n")
        key, cert, request = staging / "key.pem", staging / "cert.pem", staging / "request.pem"
        _openssl(
            [
                "req",
                "-new",
                "-newkey",
                "rsa:3072",
                "-nodes",
                "-sha256",
                "-keyout",
                str(key),
                "-out",
                str(request),
                "-subj",
                "/CN=" + common_name,
                "-config",
                str(staging / "request.cnf"),
            ]
        )
        if ca_cert_file is None:
            _openssl(
                [
                    "x509",
                    "-req",
                    "-in",
                    str(request),
                    "-signkey",
                    str(key),
                    "-out",
                    str(cert),
                    "-days",
                    str(days),
                    "-sha256",
                    "-set_serial",
                    "0x" + secrets.token_hex(16),
                    "-extfile",
                    str(staging / "extensions.cnf"),
                ]
            )
        else:
            ca_cert = _path(str(ca_cert_file))
            ca_key = _path(str(ca_key_file))
            _read_private(ca_cert)
            _read_private(ca_key)
            _openssl(
                [
                    "x509",
                    "-req",
                    "-in",
                    str(request),
                    "-CA",
                    str(ca_cert),
                    "-CAkey",
                    str(ca_key),
                    "-out",
                    str(cert),
                    "-days",
                    str(days),
                    "-sha256",
                    "-set_serial",
                    "0x" + secrets.token_hex(16),
                    "-extfile",
                    str(staging / "extensions.cnf"),
                ]
            )
        cert_data, key_data = _read_private(cert), _read_private(key)
        _publish(held, prefix + ".key", key_data)
        _publish(held, prefix + ".pem", cert_data)
        cert_path = root / (prefix + ".pem")
        return {
            "cert_file": str(cert_path),
            "key_file": str(root / (prefix + ".key")),
            "cert_sha256": certificate_fingerprint(cert_path),
        }
    except CertificateProcessHold as error:
        preserve = True
        error.directory_fd, held = held, None
        error.staging_directory_fd, stage = stage, None
        error.staging_directory = staging
        raise
    except Exception:
        raise BootstrapError("certificate issuance failed") from None
    finally:
        try:
            if staging is not None and not preserve:
                _require(stage is not None)
                for name in ("extensions.cnf", "request.cnf", "key.pem", "cert.pem", "request.pem"):
                    try:
                        os.unlink(name, dir_fd=stage)
                    except FileNotFoundError:
                        pass
                os.rmdir(staging.name, dir_fd=held)
        except Exception:
            raise BootstrapError("certificate issuance failed") from None
        finally:
            if stage is not None:
                os.close(stage)
            if held is not None:
                os.close(held)


def create_ca(directory: str | Path, *, days: int = 30) -> dict:
    return _certificate_output(
        directory,
        "ca",
        None,
        None,
        "palimpsest-nova-bootstrap-ca",
        "basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n",
        days,
    )


def issue_server_certificate(
    directory: str | Path,
    ca_cert_file: str | Path,
    ca_key_file: str | Path,
    *,
    server_name: str = "palimpsest-bootstrap",
    days: int = 7,
) -> dict:
    _require(
        _text(server_name, 128) and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?", server_name) is not None
    )
    return _certificate_output(
        directory,
        "server",
        ca_cert_file,
        ca_key_file,
        server_name,
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always,issuer\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:"
        + server_name
        + ",IP:127.0.0.1\n",
        days,
    )


def issue_client_certificate(
    directory: str | Path, ca_cert_file: str | Path, ca_key_file: str | Path, *, run_id: str, days: int = 1
) -> dict:
    _require(_matches(_RUN, run_id))
    return _certificate_output(
        directory,
        "client-" + run_id,
        ca_cert_file,
        ca_key_file,
        run_id,
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always,issuer\nextendedKeyUsage=clientAuth\n",
        days,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental loopback mTLS Nova bootstrap service")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("serve")
    command.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        serve(args.config)
    except Exception:
        parser.exit(1, "bootstrap service failed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
