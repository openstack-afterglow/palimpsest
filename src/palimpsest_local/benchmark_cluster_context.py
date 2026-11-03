"""External-only immutable k3s airgap acquisition; never called inside a guest."""

from __future__ import annotations

import hashlib
import os
import selectors
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ArtifactValidationError

K3S_IMAGE = "docker.io/rancher/k3s:v1.37.1-k3s1@sha256:ca7f37d993d82ef0dcdcfecb2e0e2618ea541dbaffc620c8cedebe01a82acd0d"
PAUSE_IMAGE = (
    "docker.io/rancher/mirrored-pause:3.10.2@sha256:412c4a7219cb8a299a37337f3d87810c5340095322e15594a1637785adad0f17"
)
K3S_VERSION = "v1.37.1+k3s1"
AIRGAP_URL = "https://github.com/k3s-io/k3s/releases/download/v1.37.1%2Bk3s1/k3s-airgap-images-amd64.tar"
AIRGAP_SHA256 = "202d334501e61561354a2d45f4fe36d3a4104a3c95a9d2c9c47587c704864014"
AIRGAP_BYTES = 195939840
MAX_CONTEXT_BYTES = 1024**3


def _url(value, *, redirected=False):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(ord(char) <= 32 for char in value)
        or (not redirected and value != AIRGAP_URL)
        or (
            redirected
            and (
                parsed.hostname != "release-assets.githubusercontent.com"
                or not parsed.path.startswith("/github-production-release-asset/")
            )
        )
    ):
        raise ArtifactValidationError("fixed k3s asset requires credential-free allowlisted HTTPS")
    return value


def _transfer(url, target, *, deadline, check, redirected=False):
    """One owned curl, no automatic redirects/retries/proxy credentials."""
    check(deadline)
    remaining = deadline - time.monotonic()
    curl = shutil.which("curl")
    if curl is None or remaining <= 0:
        raise ArtifactValidationError("fixed k3s acquisition requires curl and an unexpired deadline")
    command = [
        curl,
        "-q",
        "--fail",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--noproxy",
        "*",
        "--max-redirs",
        "0",
        "--max-time",
        str(remaining),
        "--connect-timeout",
        str(min(30, remaining)),
        "--max-filesize",
        str(AIRGAP_BYTES),
        "--write-out",
        "%{stderr}%{http_code}\n%{redirect_url}",
        "--output",
        "-",
        "--url",
        _url(url, redirected=redirected),
    ]
    digest, received = hashlib.sha256(), 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
            start_new_session=True,
            env={"PATH": os.defpath, "LC_ALL": "C"},
        )
        try:
            with selectors.DefaultSelector() as selector, Path(target).open("xb") as stream:
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    check(deadline)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ArtifactValidationError("fixed k3s acquisition absolute deadline")
                    if not selector.select(min(remaining, 0.25)):
                        continue
                    chunk = os.read(process.stdout.fileno(), min(1024**2, AIRGAP_BYTES - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > AIRGAP_BYTES:
                        raise ArtifactValidationError("fixed k3s asset exceeds exact byte bound")
                    digest.update(chunk)
                    stream.write(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ArtifactValidationError("fixed k3s acquisition absolute deadline")
                try:
                    status = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise ArtifactValidationError("fixed k3s acquisition absolute deadline") from None
                check(deadline)
                stream.flush()
                os.fsync(stream.fileno())
                check(deadline)
            errors.seek(0)
            response = errors.read(16385)
            if status or len(response) > 16384:
                raise ArtifactValidationError("fixed k3s transfer failed; no retry")
            code, separator, redirect = response.partition(b"\n")
            if not separator or code not in {b"200", b"302"}:
                raise ArtifactValidationError("fixed k3s transfer status invalid; no retry")
            if code == b"302":
                if redirected:
                    raise ArtifactValidationError("fixed k3s redirect limit exhausted")
                return None, _url(redirect.decode("ascii"), redirected=True)
            if redirect or received != AIRGAP_BYTES or digest.hexdigest() != AIRGAP_SHA256:
                raise ArtifactValidationError("fixed k3s asset exact size/hash verification failed")
            check(deadline)
            return received, None
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()


def acquire_context(directory, *, deadline, check, event, max_context_bytes=MAX_CONTEXT_BYTES):
    """Acquire exactly one fixed release asset into an empty owned job directory."""
    directory = Path(directory)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or any(directory.iterdir())
    ):
        raise ArtifactValidationError("k3s context requires an empty fresh private owned directory")
    if type(max_context_bytes) is not int or not 1 <= max_context_bytes <= MAX_CONTEXT_BYTES:
        raise ArtifactValidationError("fixed k3s context maximum exceeds oneGiB")
    if AIRGAP_BYTES + 10240 > max_context_bytes:
        raise ArtifactValidationError("fixed k3s airgap cannot fit approved context budget")
    check(deadline)
    event("dependency-download-started", {"url": AIRGAP_URL, "size_bytes": AIRGAP_BYTES, "sha256": AIRGAP_SHA256})
    target = directory / "k3s-airgap-images-amd64.tar"
    received, redirect = _transfer(AIRGAP_URL, target, deadline=deadline, check=check)
    if redirect is not None:
        target.unlink()
        received, _ = _transfer(redirect, target, deadline=deadline, check=check, redirected=True)
    check(deadline)
    receipt = {"url": AIRGAP_URL, "size_bytes": received, "sha256": AIRGAP_SHA256}
    event("dependency-download-verified", receipt)
    archive = directory / "context.tar"
    with tarfile.open(archive, "x", format=tarfile.USTAR_FORMAT) as stream:
        check(deadline)
        member = tarfile.TarInfo(target.name)
        member.size, member.mode = received, 0o644
        with target.open("rb") as payload:
            stream.addfile(member, payload)
    with archive.open("r+b") as stream:
        os.fsync(stream.fileno())
        check(deadline)
        digest = hashlib.sha256()
        while chunk := stream.read(1024**2):
            check(deadline)
            digest.update(chunk)
    check(deadline)
    size = archive.stat().st_size
    if size > max_context_bytes:
        raise ArtifactValidationError("fixed k3s context exceeds approved byte bound")
    return archive, {
        "assets": [receipt],
        "download_bytes": received,
        "context_bytes": size,
        "context_digest": "sha256:" + digest.hexdigest(),
        "external_acquisition": True,
        "worker_run_network": "none",
        "guest_acquisition": False,
    }
