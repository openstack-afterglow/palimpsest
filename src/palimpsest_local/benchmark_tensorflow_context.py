"""Hash-authoritative, external-only acquisition for the fixed TensorFlow recipe."""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
from pathlib import Path

from .errors import ArtifactValidationError

MAX_CONTEXT_BYTES = 1024**3
MAX_WHEEL_BYTES = 700 * 1024**2
_HOSTS = {"pypi.org", "files.pythonhosted.org"}


def _url(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(ord(char) <= 32 for char in value)
    ):
        raise ArtifactValidationError("fixed TensorFlow acquisition requires allowlisted credential-free HTTPS")
    return value


def _curl_command(url, remaining, maximum):
    curl = shutil.which("curl")
    if curl is None:
        raise ArtifactValidationError("fixed TensorFlow acquisition requires existing controller curl")
    return [
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
        str(maximum),
        "--write-out",
        "%{stderr}%{http_code}",
        "--output",
        "-",
        "--url",
        _url(url),
    ]


def _download(url, target, *, deadline, check, maximum):
    """Own and reap the whole transfer process; a trickle cannot extend expiry."""
    check(deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0 or maximum < 1:
        raise ArtifactValidationError("fixed TensorFlow acquisition deadline or byte budget exhausted")
    digest = hashlib.sha256()
    received = 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            _curl_command(url, remaining, maximum),
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
                        raise ArtifactValidationError("fixed TensorFlow acquisition absolute deadline")
                    ready = selector.select(min(remaining, 0.25))
                    if not ready:
                        continue
                    chunk = os.read(process.stdout.fileno(), min(1024**2, maximum - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > maximum:
                        raise ArtifactValidationError("fixed TensorFlow wheel exceeds acquisition byte bounds")
                    digest.update(chunk)
                    stream.write(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ArtifactValidationError("fixed TensorFlow acquisition absolute deadline")
                try:
                    status = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise ArtifactValidationError("fixed TensorFlow acquisition absolute deadline") from None
                check(deadline)
                errors.seek(0)
                response_status = errors.read(4096)
                if status or received == 0 or response_status != b"200":
                    raise ArtifactValidationError("fixed TensorFlow wheel transfer failed or truncated; no retry")
            return received, digest.hexdigest()
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()


def fixed_wheels():
    lock = Path(__file__).with_name("tensorflow_delta.lock").read_text()
    wheels = []
    for line in lock.splitlines():
        match = re.fullmatch(r"(https://files\.pythonhosted\.org/\S+) --hash=sha256:([0-9a-f]{64})", line)
        if match is None:
            raise ArtifactValidationError("fixed TensorFlow lock format changed")
        filename = urllib.parse.urlsplit(match[1]).path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", filename):
            raise ArtifactValidationError("fixed TensorFlow wheel filename invalid")
        name, version, *_ = filename.split("-")
        wheels.append(
            {"filename": filename, "name": name, "version": version, "sha256": match[2], "url": _url(match[1])}
        )
    if len(wheels) != 18 or wheels[0]["name"] != "tensorflow" or wheels[0]["version"] != "2.21.0":
        raise ArtifactValidationError("fixed TensorFlow lock must contain TensorFlow2.21 and17 supplemental wheels")
    return lock, wheels


def acquire_context(directory, *, deadline, check, event, max_context_bytes=MAX_CONTEXT_BYTES):
    """Download exact wheels once, verify, and return an uncompressed offline tar.

    The caller starts its cold clock before entering. No cached wheel shortcuts,
    guest acquisition, arbitrary URLs, retries or environment-proxy credentials.
    """
    directory = Path(directory)
    if type(max_context_bytes) is not int or not 1 <= max_context_bytes <= MAX_CONTEXT_BYTES:
        raise ArtifactValidationError("fixed TensorFlow context maximum exceeds oneGiB")
    wheels_dir = directory / "wheels"
    wheels_dir.mkdir(mode=0o700)
    lock, wheels = fixed_wheels()
    total = 0
    receipts = []
    for wheel in wheels:
        check(deadline)
        url = _url(wheel["url"])
        event("dependency-download-started", wheel)
        check(deadline)
        maximum = min(MAX_WHEEL_BYTES, max_context_bytes - 1024**2 - total)
        received, digest = _download(
            url,
            wheels_dir / wheel["filename"],
            deadline=deadline,
            check=check,
            maximum=maximum,
        )
        if digest != wheel["sha256"]:
            raise ArtifactValidationError("fixed TensorFlow wheel size/hash verification failed")
        total += received
        receipt = {**wheel, "size_bytes": received}
        receipts.append(receipt)
        event("dependency-download-verified", receipt)
    install_lock = "".join(
        "/opt/palimpsest-wheels/" + item["filename"] + " --hash=sha256:" + item["sha256"] + "\n" for item in wheels
    )
    lock_path = directory / "tensorflow.lock"
    lock_path.write_text(install_lock)
    archive = directory / "context.tar"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as stream:
        for path, name in [
            (lock_path, "tensorflow.lock"),
            *[(wheels_dir / item["filename"], "wheels/" + item["filename"]) for item in wheels],
        ]:
            check(deadline)
            info = stream.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with path.open("rb") as payload:
                stream.addfile(info, payload)
    if archive.stat().st_size > max_context_bytes:
        raise ArtifactValidationError("fixed TensorFlow context exceeds oneGiB bound")
    context_hash = hashlib.sha256()
    with archive.open("rb") as stream:
        while chunk := stream.read(1024**2):
            check(deadline)
            context_hash.update(chunk)
    return archive, {
        "wheels": receipts,
        "download_bytes": total,
        "context_bytes": archive.stat().st_size,
        "context_digest": "sha256:" + context_hash.hexdigest(),
        "source_lock_sha256": hashlib.sha256(lock.encode()).hexdigest(),
        "install_lock_sha256": hashlib.sha256(install_lock.encode()).hexdigest(),
        "external_acquisition": True,
        "worker_run_network": "none",
        "guest_acquisition": False,
    }
