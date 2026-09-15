"""Opt-in native proofs for selectable OCI-root guest networking.

Three independent nodes cover the public contract: NAT with a published
loopback listener and real outbound API traffic, host-only with a published
listener and proven absence of egress, and NAT with an explicit wildcard
listener reachable from the host's own LAN address. Each node uses an
operator-supplied original image archive, verifies the durable network
contract, and removes only its own run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

from palimpsest_local.oci_network import (
    OCI_NETWORK_GUEST_ADDRESS,
    OCI_NETWORK_NAMESERVER,
    OCI_NETWORK_NETMASK,
    OCINetworkConfig,
)
from palimpsest_local.state import resolve_roots

_LEGACY_PATH = Path(__file__).with_name("test_oci_docker_hub_cli_live.py")
_SPEC = importlib.util.spec_from_file_location("palimpsest_network_legacy_helpers", _LEGACY_PATH)
assert _SPEC is not None and _SPEC.loader is not None
legacy = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = legacy
_SPEC.loader.exec_module(legacy)

_PREFIX = "PALIMPSEST_OCI_NETWORK_"
_PRIVATE_DISK_BUDGET = 40 * 1024 * 1024 * 1024
_SMALL_DISK_BUDGET = 8 * 1024 * 1024 * 1024
pytestmark = pytest.mark.kvm

_SERVICE_READY = b"NET_SERVICE_READY pytorch transformer-encoder-6x256 cpu"
_SERVICE_PROGRAM = """import hashlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import torch

torch.set_num_threads(2)
torch.set_num_interop_threads(1)
torch.manual_seed(0)
layer = torch.nn.TransformerEncoderLayer(
    d_model=256,
    nhead=8,
    dim_feedforward=1024,
    dropout=0.0,
    activation="gelu",
    batch_first=True,
)
model = torch.nn.TransformerEncoder(layer, num_layers=6).eval()
sample = torch.linspace(-1.0, 1.0, steps=128 * 256, dtype=torch.float32).reshape(1, 128, 256)
request_count = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, _format, *_args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/healthz":
            self.send_json(404, {"error": "not-found"})
            return
        self.send_json(
            200,
            {
                "cuda": torch.cuda.is_available(),
                "device": "cpu",
                "framework": "pytorch",
                "model": "transformer-encoder-6x256",
                "status": "ready",
                "version": torch.__version__,
            },
        )

    def do_POST(self):
        global request_count
        if self.path != "/infer":
            self.send_json(404, {"error": "not-found"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 1 <= length <= 256:
                raise ValueError
            request = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "invalid-request"})
            return
        if request != {"iterations": 4, "scale": 1.0}:
            self.send_json(400, {"error": "unsupported-request"})
            return
        value = sample * request["scale"]
        with torch.inference_mode():
            for _iteration in range(request["iterations"]):
                value = model(value)
        request_count += 1
        self.send_json(
            200,
            {
                "cuda": torch.cuda.is_available(),
                "device": str(value.device),
                "finite": bool(torch.isfinite(value).all().item()),
                "iterations": request["iterations"],
                "model": "transformer-encoder-6x256",
                "output_sha256": hashlib.sha256(value.contiguous().numpy().tobytes()).hexdigest(),
                "requests": request_count,
                "shape": list(value.shape),
            },
        )


server = HTTPServer(("0.0.0.0", 18080), Handler)
print("NET_SERVICE_READY pytorch transformer-encoder-6x256 cpu", flush=True)
server.serve_forever()
"""

# Guest-side outbound proof: the resolver file written by PID 1, the committed
# address and default route, DNS through the virtual network, and one real
# HTTPS API request and response.
_OUTBOUND_PROGRAM = """import json
import socket
import ssl
import urllib.request

with open("/etc/resolv.conf", "r", encoding="ascii") as handle:
    resolver = handle.read()
assert resolver == "nameserver %(nameserver)s\\n", resolver
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
probe.connect(("%(gateway)s", 9))
assert probe.getsockname()[0] == "%(address)s", probe.getsockname()
probe.close()
resolved = sorted({info[4][0] for info in socket.getaddrinfo("api.github.com", 443, socket.AF_INET)})
assert resolved, "no DNS answer"
mode = "verified"
try:
    context = ssl.create_default_context()
    request = urllib.request.Request("https://api.github.com/meta", headers={"User-Agent": "palimpsest-proof"})
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        status = response.status
        payload = json.load(response)
except ssl.SSLError:
    mode = "unverified"
    context = ssl._create_unverified_context()
    request = urllib.request.Request("https://api.github.com/meta", headers={"User-Agent": "palimpsest-proof"})
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        status = response.status
        payload = json.load(response)
assert status == 200, status
assert isinstance(payload.get("api"), list) and payload["api"], sorted(payload)
print("NET_EGRESS_OK", mode, len(resolved), status, len(payload["api"]))
"""

# Fail-closed isolation probe. A missing or unusable tool must never be
# recorded as proven isolation, so the program refuses unless every tool exists
# and proves the TCP probe against a reachable in-guest target before any
# negative result is trusted.
#
# The isolation evidence is the two TCP negatives — an external address and the
# virtual gateway — because those use the positively controlled probe. The
# resolver legs are policy checks, not independent egress evidence: PID 1 must
# have written no nameserver for host-only, and a name lookup must not resolve.
# The lookup alone would be near-tautological with an empty resolver file, so it
# is kept only as a redundant signal.
_NO_EGRESS_PROGRAM = (
    "set -u; "
    "command -v getent >/dev/null 2>&1 || { echo NET_TOOL_MISSING=getent; exit 94; }; "
    "command -v nc >/dev/null 2>&1 || { echo NET_TOOL_MISSING=nc; exit 94; }; "
    "command -v ip >/dev/null 2>&1 || { echo NET_TOOL_MISSING=ip; exit 94; }; "
    "nc -w 3 -z 127.0.0.1 %(service_port)s >/dev/null 2>&1 || { echo NET_PROBE_UNUSABLE; exit 96; }; "
    'if grep -q "^nameserver" /etc/resolv.conf 2>/dev/null; then echo NET_RESOLVER_PRESENT; exit 98; fi; '
    "if getent hosts api.github.com >/dev/null 2>&1; then echo NET_DNS_REACHED; exit 91; fi; "
    "if nc -w 3 -z 1.1.1.1 443 >/dev/null 2>&1; then echo NET_TCP_REACHED; exit 92; fi; "
    "if nc -w 3 -z %(gateway)s 22 >/dev/null 2>&1; then echo NET_HOST_REACHED; exit 93; fi; "
    'ip -4 addr show eth0 | grep -q "inet %(address)s/24" || { echo NET_ADDRESS_MISSING; exit 97; }; '
    "echo NET_NO_EGRESS_OK"
)


@dataclass(frozen=True)
class NetworkCase:
    key: str
    name_prefix: str
    memory_mib: int
    vcpus: int
    disk_budget: int


PYTORCH = NetworkCase("PYTORCH", "net-nat-service", 8192, 2, _PRIVATE_DISK_BUDGET)
REDIS = NetworkCase("REDIS", "net-host-only", 1024, 1, _SMALL_DISK_BUDGET)
NGINX = NetworkCase("NGINX", "net-external", 1024, 1, _SMALL_DISK_BUDGET)


def _selection(case: NetworkCase):
    if os.environ.get(_PREFIX + "LIVE") != "1":
        pytest.skip(f"set {_PREFIX}LIVE=1 for the independent networking proof")
    stem = _PREFIX + case.key + "_"
    archive = Path(os.environ.get(stem + "IMAGE", ""))
    archive_digest = os.environ.get(stem + "ARCHIVE_SHA256", "")
    manifest = os.environ.get(stem + "MANIFEST_SHA256", "")
    assert archive.is_absolute() and archive.resolve(strict=True).is_file()
    assert legacy._DIGEST.fullmatch(archive_digest) and legacy._file_sha256(archive) == archive_digest
    assert legacy._DIGEST.fullmatch(manifest)
    return legacy.DockerHubImageSelection(archive.resolve(), archive_digest, manifest)


def _setup(environment: dict[str, str], name: str):
    root = Path(environment.get(_PREFIX + "PROOF_ROOT", ""))
    assert root.is_absolute() and root.resolve(strict=True) == root
    info = root.stat(follow_symlinks=False)
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o711
    for _attempt in range(16):
        parent = root / ("n-" + uuid.uuid4().hex[:8])
        try:
            parent.lstat()
        except FileNotFoundError:
            break
    else:
        raise AssertionError("could not select a fresh networking runtime parent")
    selected = dict(environment)
    legacy._success(legacy._cli(selected, "oci", "init-runtime", parent))
    selected["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    selected["XDG_CONFIG_HOME"] = str(parent / "config")
    roots = resolve_roots(selected)
    assert len(os.fsencode(roots.runs / name / "io" / "lifecycle.sock")) <= 97
    return parent, selected


def _free_host_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert 1024 <= port <= 65535
    return port


def _host_lan_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("198.51.100.1", 9))
        address = probe.getsockname()[0]
    assert not address.startswith("127."), address
    return address


def _assert_authored_user_mode_nic(environment: dict[str, str], name: str, network: OCINetworkConfig, run_id: str):
    virsh = shutil.which("virsh", path=environment.get("PATH"))
    assert virsh is not None
    domain = legacy._bounded_command(
        [virsh, "-c", "qemu:///system", "dumpxml", name], environment=environment, timeout=15
    )
    legacy._success(domain)
    root = ET.fromstring(domain.stdout)
    # libvirt owns no NIC, network, bridge, or filter for an OCI-root run.
    assert root.findall("./devices/interface") == []
    assert root.findall("./devices/hostdev") == []
    assert root.findall("./devices/filesystem") == []
    namespace = "{http://libvirt.org/schemas/domain/qemu/1.0}"
    arguments = tuple(element.get("value", "") for element in root.findall(f"./{namespace}commandline/{namespace}arg"))
    assert arguments == network.qemu_arguments(run_id)
    return root


def _network_status(environment: dict[str, str], name: str) -> dict[str, object]:
    result = legacy._cli(environment, "oci", "network", name, timeout=30)
    legacy._success(result)
    return json.loads(result.stdout)


def _wait_for_listener(address: str, port: int, *, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, port), timeout=5):
                return
        except OSError as error:
            last = error
            time.sleep(1.0)
    raise AssertionError(f"published listener never accepted a connection: {last}")


def _http_json(url: str, *, payload: bytes | None = None, timeout: float = 180.0):
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.load(response)


def _redis_ping(address: str, port: int) -> bytes:
    with socket.create_connection((address, port), timeout=15) as stream:
        stream.sendall(b"*1\r\n$4\r\nPING\r\n")
        stream.settimeout(15)
        return stream.recv(64)


def _guest_network_expectations() -> dict[str, str]:
    return {
        "address": OCI_NETWORK_GUEST_ADDRESS,
        "gateway": "10.0.2.2",
        "nameserver": OCI_NETWORK_NAMESERVER,
        "netmask": OCI_NETWORK_NETMASK,
    }


def _cleanup(environment: dict[str, str], parent: Path, name: str, domain_uuid: str, roots) -> None:
    stopped = legacy._save(parent, "stop", legacy._cli(environment, "stop", name, timeout=120))
    legacy._success(stopped)
    removed = legacy._save(parent, "rm", legacy._cli(environment, "rm", name, timeout=120))
    legacy._success(removed)
    legacy._assert_domain_absent(environment, name, domain_uuid)
    assert not (roots.runs / name).exists()


def test_nat_publishes_inbound_api_and_reaches_an_external_api() -> None:
    case = PYTORCH
    selection = _selection(case)
    name = f"{case.name_prefix}-{uuid.uuid4().hex[:8]}"
    parent, environment = _setup(legacy._environment(), name)
    host_port = _free_host_port()
    network = OCINetworkConfig.resolve("nat", [f"127.0.0.1:{host_port}:18080"])
    values = _guest_network_expectations()
    service_command = ("/opt/conda/bin/python", "-u", "-c", _SERVICE_PROGRAM)
    source_hash = legacy._file_sha256(selection.archive)
    roots = resolve_roots(environment)
    assert shutil.disk_usage(parent).free >= case.disk_budget
    completed = False
    try:
        legacy._authenticate(selection, parent)
        launched = legacy._save(
            parent,
            "run",
            legacy._cli(
                environment,
                "run",
                selection.archive,
                "--manifest",
                selection.manifest_digest,
                "--name",
                name,
                "--memory",
                str(case.memory_mib),
                "--vcpus",
                str(case.vcpus),
                "--network",
                "nat",
                "--publish",
                f"127.0.0.1:{host_port}:18080",
                "-d",
                "--",
                *service_command,
                timeout=1200,
            ),
        )
        legacy._success(launched)
        assert launched.stdout == (name + "\n").encode()
        proof = legacy._root_proof(environment, name)
        domain_uuid = proof["domain"]["uuid"]
        run_id = proof["run"]["run_id"]
        _assert_authored_user_mode_nic(environment, name, network, run_id)
        status = _network_status(environment, name)
        assert status["mode"] == "nat" and status["egress"] is True
        assert status["guest"]["address"] == values["address"]
        assert status["guest"]["nameservers"] == [values["nameserver"]]
        assert status["published_ports"] == [
            {"guest_port": 18080, "host_ip": "127.0.0.1", "host_port": host_port, "protocol": "tcp"}
        ]
        assert status["exposure"]["external"] is False
        legacy._wait_console(roots.runs / name / "io" / "console.log", _SERVICE_READY, timeout=180)
        _wait_for_listener("127.0.0.1", host_port)

        health_status, health = _http_json(f"http://127.0.0.1:{host_port}/healthz", timeout=60)
        assert health_status == 200
        assert health["framework"] == "pytorch" and health["device"] == "cpu" and health["cuda"] is False
        payload = json.dumps({"iterations": 4, "scale": 1.0}, sort_keys=True).encode()
        first_status, first = _http_json(f"http://127.0.0.1:{host_port}/infer", payload=payload)
        second_status, second = _http_json(f"http://127.0.0.1:{host_port}/infer", payload=payload)
        assert (first_status, second_status) == (200, 200)
        for expected_requests, result in enumerate((first, second), start=1):
            assert result["requests"] == expected_requests
            assert result["shape"] == [1, 128, 256]
            assert result["iterations"] == 4
            assert result["device"] == "cpu" and result["cuda"] is False and result["finite"] is True
            assert re.fullmatch(r"[0-9a-f]{64}", result["output_sha256"])
        assert first["output_sha256"] == second["output_sha256"]
        (parent / "inbound-api.json").write_text(
            json.dumps({"health": health, "first": first, "second": second}, sort_keys=True) + "\n"
        )

        egress = legacy._save(
            parent,
            "egress",
            legacy._cli(
                environment,
                "exec",
                "--timeout",
                "180",
                name,
                "--",
                "/opt/conda/bin/python",
                "-c",
                _OUTBOUND_PROGRAM % values,
                timeout=210,
            ),
        )
        legacy._success(egress)
        assert re.fullmatch(rb"NET_EGRESS_OK (verified|unverified) [1-9][0-9]* 200 [1-9][0-9]*\n", egress.stdout)

        identity = legacy._save(
            parent,
            "root",
            legacy._cli(environment, "exec", name, "--", "/bin/sh", "-c", "stat -c '%d %i' /", timeout=60),
        )
        legacy._success(identity)
        refusal = legacy._save(
            parent,
            "pid1-refusal",
            legacy._cli(
                environment,
                "exec",
                name,
                "--",
                "/bin/sh",
                "-c",
                "LC_ALL=C cat /proc/1/root/etc/os-release",
                timeout=60,
            ),
        )
        assert refusal.returncode != 0 and refusal.stdout == b"" and b"Permission denied" in refusal.stderr

        _cleanup(environment, parent, name, domain_uuid, roots)
        with pytest.raises(OSError):
            with socket.create_connection(("127.0.0.1", host_port), timeout=5):
                pass
        assert legacy._file_sha256(selection.archive) == source_hash == selection.archive_digest
        completed = True
    finally:
        if not completed:
            print(f"OCI network NAT proof failure preserved: {parent}", flush=True)


def test_host_only_publishes_a_service_without_any_egress() -> None:
    case = REDIS
    selection = _selection(case)
    name = f"{case.name_prefix}-{uuid.uuid4().hex[:8]}"
    parent, environment = _setup(legacy._environment(), name)
    host_port = _free_host_port()
    network = OCINetworkConfig.resolve("host-only", [f"127.0.0.1:{host_port}:6379"])
    values = _guest_network_expectations()
    source_hash = legacy._file_sha256(selection.archive)
    roots = resolve_roots(environment)
    assert shutil.disk_usage(parent).free >= case.disk_budget
    completed = False
    try:
        legacy._authenticate(selection, parent)
        launched = legacy._save(
            parent,
            "run",
            legacy._cli(
                environment,
                "run",
                selection.archive,
                "--manifest",
                selection.manifest_digest,
                "--name",
                name,
                "--memory",
                str(case.memory_mib),
                "--vcpus",
                str(case.vcpus),
                "--user",
                "redis",
                "--network",
                "host-only",
                "--publish",
                f"127.0.0.1:{host_port}:6379",
                "-d",
                timeout=600,
            ),
        )
        legacy._success(launched)
        assert launched.stdout == (name + "\n").encode()
        proof = legacy._root_proof(environment, name)
        domain_uuid = proof["domain"]["uuid"]
        run_id = proof["run"]["run_id"]
        _assert_authored_user_mode_nic(environment, name, network, run_id)
        status = _network_status(environment, name)
        assert status["mode"] == "host-only" and status["egress"] is False
        assert status["guest"]["nameservers"] == []
        assert status["exposure"]["external"] is False

        _wait_for_listener("127.0.0.1", host_port)
        assert _redis_ping("127.0.0.1", host_port) == b"+PONG\r\n"
        (parent / "inbound-redis.txt").write_bytes(b"+PONG\r\n")

        isolation = legacy._save(
            parent,
            "no-egress",
            legacy._cli(
                environment,
                "exec",
                "--timeout",
                "60",
                name,
                "--",
                "/bin/sh",
                "-c",
                _NO_EGRESS_PROGRAM % {**values, "service_port": 6379},
                timeout=90,
            ),
        )
        legacy._success(isolation)
        # Fail-closed: the probe only prints this after proving both tools work.
        assert isolation.stdout.strip() == b"NET_NO_EGRESS_OK"

        _cleanup(environment, parent, name, domain_uuid, roots)
        assert legacy._file_sha256(selection.archive) == source_hash == selection.archive_digest
        completed = True
    finally:
        if not completed:
            print(f"OCI network host-only proof failure preserved: {parent}", flush=True)


def test_explicit_wildcard_publish_is_reachable_from_the_host_address() -> None:
    case = NGINX
    selection = _selection(case)
    name = f"{case.name_prefix}-{uuid.uuid4().hex[:8]}"
    parent, environment = _setup(legacy._environment(), name)
    host_port = _free_host_port()
    network = OCINetworkConfig.resolve("nat", [f"0.0.0.0:{host_port}:8080"])
    lan_address = _host_lan_address()
    source_hash = legacy._file_sha256(selection.archive)
    roots = resolve_roots(environment)
    assert shutil.disk_usage(parent).free >= case.disk_budget
    completed = False
    try:
        legacy._authenticate(selection, parent)
        launched = legacy._save(
            parent,
            "run",
            legacy._cli(
                environment,
                "run",
                selection.archive,
                "--manifest",
                selection.manifest_digest,
                "--name",
                name,
                "--memory",
                str(case.memory_mib),
                "--vcpus",
                str(case.vcpus),
                "--network",
                "nat",
                "--publish",
                f"0.0.0.0:{host_port}:8080",
                "-d",
                timeout=600,
            ),
        )
        legacy._success(launched)
        proof = legacy._root_proof(environment, name)
        domain_uuid = proof["domain"]["uuid"]
        run_id = proof["run"]["run_id"]
        _assert_authored_user_mode_nic(environment, name, network, run_id)
        status = _network_status(environment, name)
        assert status["exposure"]["external"] is True
        assert status["exposure"]["external_ports"] == [
            {"guest_port": 8080, "host_ip": "0.0.0.0", "host_port": host_port, "protocol": "tcp"}
        ]

        _wait_for_listener("127.0.0.1", host_port)
        with urllib.request.urlopen(f"http://{lan_address}:{host_port}/", timeout=60) as response:
            body = response.read(4096)
            external_status = response.status
        assert external_status == 200 and b"nginx" in body.lower()
        (parent / "external-endpoint.json").write_text(
            json.dumps({"address": lan_address, "port": host_port, "status": external_status}, sort_keys=True) + "\n"
        )

        _cleanup(environment, parent, name, domain_uuid, roots)
        with pytest.raises(OSError):
            with socket.create_connection((lan_address, host_port), timeout=5):
                pass
        assert legacy._file_sha256(selection.archive) == source_hash == selection.archive_digest
        completed = True
    finally:
        if not completed:
            print(f"OCI network wildcard proof failure preserved: {parent}", flush=True)
