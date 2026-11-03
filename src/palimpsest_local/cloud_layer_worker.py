"""Worker-side Dockerfile builds and pinned anonymous OCI intake into Manila CephFS.

Runs as root on a dedicated Nova worker VM provisioned by ``cloud_env``. The
CLI uploads a context archive and Dockerfile, then invokes::

    python -m palimpsest_local.cloud_layer_worker build --dockerfile F --context-tar T \
        --store /mnt/palimpsest-layers --scratch /var/lib/palimpsest-cloud --name NAME

BuildKit produces an OCI archive; ``LayerCache`` converts it into ordered,
digest-verified SquashFS layers (OCI whiteouts translated). Each layer is
published once into the RW-mounted Manila CephFS share under
``blobs/sha256/<hex>`` (write-once: temp file, fsync, atomic link, mode 0444). The
image record ``images/<name>.json`` lists the ordered layers and runtime config.
Consumers mount the same share with a separate read-only cephx identity.

Registry intake runs ``pull --reference registry/repository@sha256:...`` on this
worker, never on the client. It retains original source/config documents and
commits each conversion's immutable receipt and job journal before converting
the next layer. These local image records do not claim Hub metadata authority.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import ArtifactValidationError
from .hub_image_builder import extract_context
from .layer_cache import LayerCache
from .oci_image import strict_json_object
from .oci_packer import SquashFSPackProfile
from .oci_provenance import Descriptor
from .oci_store import DERIVED_OCCURRENCE_SCHEMA, DerivedLayerReceipt
from .registry_intake import RegistryIntakeError, resolve_anonymous_reference

BUILDKIT_SOCKET = Path("/run/palimpsest-cloud-buildkit.sock")
MAX_CONTEXT_BYTES = 8 * 1024**3
MAX_CONTEXT_MEMBERS = 500_000
SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


class WorkerError(RuntimeError):
    pass


class WorkerHold(WorkerError):
    """Process or socket ownership is uncertain; preserve evidence for operator review."""


def _regular_file(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise WorkerError(f"cannot open regular non-symlink file {path}: {error}") from error
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise WorkerError(f"not a regular non-symlink file: {path}")
    return os.fdopen(fd, "rb")


def _directory(path: Path) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise WorkerError(f"not a non-symlink directory: {path}")


def _digest_hex(digest: str) -> str:
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise WorkerError("layer digest must be sha256 followed by 64 lowercase hexadecimal characters")
    return digest[7:]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _regular_file(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def validate_name(name: str) -> str:
    if not name or len(name) > 128 or name[0] in "._-" or not set(name) <= NAME_CHARS:
        raise WorkerError("image name must be lowercase [a-z0-9._-], at most 128 characters")
    return name


def require_ceph_store(store: Path) -> None:
    """The publication target must be the mounted CephFS share, never a local directory."""
    found = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "--target", str(store)], capture_output=True, text=True, env=SAFE_ENV
    )
    if found.returncode != 0 or found.stdout.strip() != "ceph":
        raise WorkerError(f"layer store {store} is not a mounted CephFS share")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict) -> dict:
    """Bind an image name once; timestamps do not change an existing binding."""
    _directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    owned = False
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            owned = True
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            with _regular_file(path) as stream:
                if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o444:
                    raise WorkerError(f"existing immutable binding record has unexpected permissions: {path}") from None
                existing = strict_json_object(stream.read(), "existing image record")
            binding = {key: item for key, item in value.items() if key != "built_at_unix"}
            previous = {key: item for key, item in existing.items() if key != "built_at_unix"}
            if previous != binding:
                raise WorkerError(f"image tag {path.stem} already has a different immutable binding") from None
            return existing
        _fsync_dir(path.parent)
        return value
    finally:
        if owned:
            temporary.unlink()


def publish_blob(source: Path, digest: str, store: Path) -> dict:
    """Publish without replacement, including when another writer wins the link race."""
    hex_digest = _digest_hex(digest)
    blobs = store / "blobs" / "sha256"
    for directory in (store, store / "blobs", blobs, store / ".incoming"):
        _directory(directory)
    target = blobs / hex_digest
    # A cache hit still authenticates the supplied source, but never writes a
    # second copy across CephFS merely to discover an existing immutable blob.
    with _regular_file(source) as stream:
        size = os.fstat(stream.fileno()).st_size
        source_digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    if source_digest != digest:
        raise WorkerError(f"source layer {digest} failed verification")

    def existing() -> dict:
        with _regular_file(target) as stream:
            metadata = os.fstat(stream.fileno())
            existing_digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if metadata.st_size != size or existing_digest != digest or stat.S_IMODE(metadata.st_mode) != 0o444:
            raise WorkerError(f"existing published layer {digest} differs; refusing to overwrite")
        return {"digest": digest, "size": size, "published": "existing"}

    if os.path.lexists(target):
        return existing()
    temporary = store / ".incoming" / f"{hex_digest}.{uuid.uuid4().hex}"
    owned = False
    try:
        with _regular_file(source) as incoming, temporary.open("xb") as outgoing:
            owned = True
            size = os.fstat(incoming.fileno()).st_size
            shutil.copyfileobj(incoming, outgoing, 4 * 1024 * 1024)
            outgoing.flush()
            os.fchmod(outgoing.fileno(), 0o444)
            os.fsync(outgoing.fileno())
        if _sha256(temporary) != digest or temporary.stat().st_size != size:
            raise WorkerError(f"copied layer {digest} failed verification")
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            return existing()
        _fsync_dir(blobs)
        return {"digest": digest, "size": size, "published": "new"}
    finally:
        if owned:
            temporary.unlink()


@contextmanager
def _exclusive_lock(path: Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise WorkerHold(f"HOLD: lock is not an owned regular file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkerHold(f"HOLD: worker lock is already held: {path}") from error
        yield
    finally:
        os.close(fd)


def _term_wait(process: subprocess.Popen | None) -> bool:
    if process is None or process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=30)
    except ProcessLookupError:
        return process.poll() is not None
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _buildkit(context: Path, definition: Path, output: Path, root: Path, log: Path, deadline: float) -> None:
    # The short socket is shared even if callers select different scratch directories.
    with _exclusive_lock(BUILDKIT_SOCKET.with_suffix(".lock")):
        socket_hold = BUILDKIT_SOCKET.with_suffix(".hold.json")
        if os.path.lexists(socket_hold):
            raise WorkerHold(f"HOLD: prior socket shutdown requires review: {socket_hold}")
        if os.path.lexists(BUILDKIT_SOCKET):
            raise WorkerHold(f"HOLD: preexisting BuildKit socket requires review: {BUILDKIT_SOCKET}")
        config = log.parent / "buildkitd.toml"
        config.write_text('[worker.oci]\nmax-parallelism = 2\n[frontend."gateway.v0"]\nenabled = false\n')
        root.mkdir(mode=0o700, exist_ok=True)
        _directory(root)
        client = None
        socket_identity = None
        with (log.parent / "buildkitd.log").open("ab") as daemon_log:
            daemon = subprocess.Popen(
                [
                    "buildkitd",
                    "--config",
                    str(config),
                    "--root",
                    str(root),
                    "--addr",
                    "unix://" + str(BUILDKIT_SOCKET),
                    "--oci-worker=true",
                    "--containerd-worker=false",
                    "--oci-worker-snapshotter=overlayfs",
                    "--oci-worker-binary=/usr/local/bin/buildkit-runc",
                    "--oci-worker-net=host",
                ],
                stdin=subprocess.DEVNULL,
                stdout=daemon_log,
                stderr=subprocess.STDOUT,
                env=SAFE_ENV,
            )
        try:
            while True:
                if daemon.poll() is not None:
                    raise WorkerError("buildkitd exited before serving; see buildkitd.log")
                if time.monotonic() >= deadline:
                    raise WorkerError("build deadline expired while starting buildkitd")
                try:
                    metadata = BUILDKIT_SOCKET.lstat()
                except FileNotFoundError:
                    time.sleep(0.1)
                    continue
                if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                    raise WorkerHold("HOLD: BuildKit socket is not an owned Unix socket")
                socket_identity = (metadata.st_dev, metadata.st_ino)
                break
            ownership = {
                "daemon_pid": daemon.pid,
                "socket": str(BUILDKIT_SOCKET),
                "device": socket_identity[0],
                "inode": socket_identity[1],
            }
            with (log.parent / "buildkit-owner.json").open("x", encoding="utf-8") as stream:
                json.dump(ownership, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            argv = [
                "buildctl",
                "--addr",
                "unix://" + str(BUILDKIT_SOCKET),
                "build",
                "--frontend",
                "dockerfile.v0",
                "--opt",
                "platform=linux/amd64",
                "--local",
                "context=" + str(context),
                "--local",
                "dockerfile=" + str(definition),
                "--output",
                "type=oci,dest=" + str(output) + ",oci-mediatypes=true",
            ]
            with log.open("ab") as client_log:
                client = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=client_log,
                    stderr=subprocess.STDOUT,
                    env=SAFE_ENV,
                )
                try:
                    result = client.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    raise WorkerError("Dockerfile build deadline expired") from error
            if result != 0:
                raise WorkerError("Dockerfile build failed; see buildctl.log")
        finally:
            client_stopped = _term_wait(client)
            daemon_stopped = _term_wait(daemon)
            if not client_stopped or not daemon_stopped:
                try:
                    for hold in (root.parent / "buildkit.hold.json", socket_hold):
                        if not os.path.lexists(hold):
                            with hold.open("x", encoding="utf-8") as stream:
                                json.dump(
                                    {
                                        "job": str(log.parent),
                                        "daemon_pid": daemon.pid,
                                        "client_pid": client.pid if client is not None else None,
                                    },
                                    stream,
                                )
                                stream.flush()
                                os.fsync(stream.fileno())
                except OSError as error:
                    raise WorkerHold(
                        f"HOLD: shutdown uncertain and hold recording failed; preserved {log.parent}"
                    ) from error
                raise WorkerHold(f"HOLD: TERM did not confirm BuildKit shutdown; preserved {log.parent}")
            try:
                metadata = BUILDKIT_SOCKET.lstat()
            except FileNotFoundError:
                pass
            else:
                if socket_identity != (metadata.st_dev, metadata.st_ino) or not stat.S_ISSOCK(metadata.st_mode):
                    raise WorkerHold("HOLD: socket ownership changed; preserving socket and job")
                BUILDKIT_SOCKET.unlink()


def build(
    *, dockerfile: Path, context_tar: Path, store: Path, scratch: Path, name: str, timeout_seconds: float
) -> dict:
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64") or os.geteuid() != 0:
        raise WorkerError("the cloud layer worker requires root on Linux amd64")
    validate_name(name)
    require_ceph_store(store)
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(scratch)
    with _exclusive_lock(scratch / "worker.lock"):
        if any(os.path.lexists(scratch / item) for item in ("buildkit.hold.json", "intake.hold.json")):
            raise WorkerHold("HOLD: a prior worker shutdown or intake requires operator review")
        deadline = time.monotonic() + timeout_seconds
        started = time.time()
        _directory(store)
        for directory in (store / "blobs", store / "blobs" / "sha256", store / "images", store / ".incoming"):
            directory.mkdir(mode=0o755, exist_ok=True)
            _directory(directory)
        jobs = scratch / "jobs"
        jobs.mkdir(mode=0o700, exist_ok=True)
        _directory(jobs)
        job = jobs / uuid.uuid4().hex
        job.mkdir(mode=0o700)
        context = job / "context"
        extract_context(context_tar, context, maximum_bytes=MAX_CONTEXT_BYTES, maximum_members=MAX_CONTEXT_MEMBERS)
        definition = job / "definition"
        definition.mkdir(mode=0o700)
        with _regular_file(dockerfile) as incoming, (definition / "Dockerfile").open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        oci_archive = job / "image.oci.tar"
        _buildkit(context, definition, oci_archive, scratch / "buildkit", job / "buildctl.log", deadline)
        built = time.time()
        cache_root = scratch / "layer-cache"
        fresh = not os.path.lexists(cache_root)
        if fresh:
            cache_root.mkdir(mode=0o700)
        _directory(cache_root)
        cache = LayerCache(cache_root, initialize=fresh)
        imported = cache.import_source(oci_archive)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerError("build deadline expired before SquashFS conversion")
        materialized = cache.materialize(
            imported["root_digest"], timeout_seconds=remaining, pack_profile=SquashFSPackProfile.SERVICE_IMAGE
        )
        converted = time.time()
        receipt = materialized["receipt"]
        paths = [Path(path) for path in materialized["blob_paths"]]
        if not paths or len(paths) != len(receipt["results"]):
            raise WorkerError("materialization returned an inconsistent layer list")
        config_descriptor = materialized["image"]["config_descriptor"]
        config_hex = _digest_hex(config_descriptor["digest"])
        config_path = next(Path(path) for path in materialized["source_blob_paths"] if Path(path).name == config_hex)
        with _regular_file(config_path) as stream:
            config = strict_json_object(stream.read(), "built image config").get("config") or {}
        layers = []
        for ordinal, path in enumerate(paths):
            expected = receipt["results"][ordinal]["receipt"]
            _digest_hex(expected["image_digest"])
            digest = _sha256(path)
            if digest != expected["image_digest"] or path.stat().st_size != expected["image_size"]:
                raise WorkerError("materialized layer differs from its conversion receipt")
            layers.append({"ordinal": ordinal, **publish_blob(path, digest, store)})
        record = {
            "schema": "palimpsest.cloud-layer-image.v1",
            "name": name,
            "source": {
                "root_digest": receipt["root_descriptor"]["digest"],
                "manifest_digest": receipt["manifest_digest"],
                "config_digest": config_descriptor["digest"],
            },
            "layers": [{"digest": layer["digest"], "size": layer["size"]} for layer in layers],
            "runtime": {
                "entrypoint": config.get("Entrypoint") or [],
                "cmd": config.get("Cmd") or [],
                "env": config.get("Env") or [],
                "working_dir": config.get("WorkingDir") or "/",
                "user": config.get("User") or "0",
            },
            "built_at_unix": round(started, 3),
        }
        record = _atomic_json(store / "images" / f"{name}.json", record)
        return {
            **record,
            "evidence_directory": str(job),
            "publication": [layer["published"] for layer in layers],
            "timings_seconds": {
                "buildkit": round(built - started, 3),
                "squashfs": round(converted - built, 3),
                "publish": round(time.time() - converted, 3),
            },
        }


def _json_digest(value: dict) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _pull_reference(reference: str) -> tuple[str, str]:
    if not isinstance(reference, str) or reference.count("@") != 1:
        raise WorkerError("registry pull requires an explicit registry/repository@sha256 digest pin")
    repository, pin = reference.split("@")
    _digest_hex(pin)
    if "/" not in repository or ":" in repository.rsplit("/", 1)[1] or "://" in reference:
        raise WorkerError("registry pull requires an explicit registry/repository digest pin without a tag")
    try:
        canonical = resolve_anonymous_reference(reference).canonical
    except RegistryIntakeError as error:
        raise WorkerError(str(error)) from error
    if canonical.rsplit("@", 1)[-1] != pin:
        raise WorkerError("registry reference changed its digest pin")
    return canonical, pin


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WorkerError("registry intake deadline expired")
    return remaining


def _source_document(imported: dict, descriptor: dict) -> dict:
    expected = Descriptor.from_dict(descriptor)
    hex_digest = _digest_hex(expected.digest)
    paths = [Path(path) for path in imported["source_blob_paths"] if Path(path).name == hex_digest]
    if len(paths) != 1:
        raise WorkerError("registry source descriptor has no unique retained blob")
    with _regular_file(paths[0]) as stream:
        payload = stream.read()
    if len(payload) != expected.size or "sha256:" + hashlib.sha256(payload).hexdigest() != expected.digest:
        raise WorkerError("registry source blob differs from its original descriptor")
    return strict_json_object(payload, "registry source document")


def _pull_source(imported: dict, reference: str, pin: str) -> tuple[dict, dict]:
    image = imported["image"]
    if imported["root_digest"] != pin or image["root_descriptor"]["digest"] != pin:
        raise WorkerError("registry result does not match the requested digest pin")
    if image["platform"].get("os") != "linux" or image["platform"].get("architecture") != "amd64":
        raise WorkerError("registry intake requires the original linux/amd64 image")
    root = _source_document(imported, image["root_descriptor"])
    manifest = _source_document(imported, image["manifest_descriptor"])
    config = _source_document(imported, image["config_descriptor"])
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise WorkerError("registry config is not linux/amd64")
    if {key: manifest.get("config", {}).get(key) for key in ("mediaType", "digest", "size")} != image[
        "config_descriptor"
    ]:
        raise WorkerError("registry manifest contradicts its original config descriptor")
    layers = image["layers"]
    if not layers or [
        {key: entry.get(key) for key in ("mediaType", "digest", "size")} for entry in manifest.get("layers", [])
    ] != [layer["compressed"] for layer in layers]:
        raise WorkerError("registry manifest contradicts its ordered source layers")
    rootfs = config.get("rootfs")
    if (
        not isinstance(rootfs, dict)
        or rootfs.get("type") != "layers"
        or rootfs.get("diff_ids") != [layer["diff_id"] for layer in layers]
    ):
        raise WorkerError("registry config contradicts its ordered source DiffIDs")
    for ordinal, layer in enumerate(layers):
        if type(layer["ordinal"]) is not int or layer["ordinal"] != ordinal:
            raise WorkerError("registry source ordinal is inconsistent")
        Descriptor.from_dict(layer["compressed"])
        _digest_hex(layer["diff_id"])
    process = config.get("config") or {}
    if not isinstance(process, dict):
        raise WorkerError("registry process config must be an object")
    source = {
        "reference": reference,
        "platform": image["platform"],
        "root_digest": pin,
        "manifest_digest": image["manifest_descriptor"]["digest"],
        "config_digest": image["config_descriptor"]["digest"],
        "root_descriptor": image["root_descriptor"],
        "manifest_descriptor": image["manifest_descriptor"],
        "config_descriptor": image["config_descriptor"],
        "root_document": root,
        "manifest": manifest,
        "config": config,
    }
    return source, process


def pull(
    *, reference: str, store: Path, scratch: Path, name: str, timeout_seconds: float, job_id: str | None = None
) -> dict:
    """Acquire anonymous pinned OCI bytes on the worker, publishing each completed layer in order."""
    reference, pin = _pull_reference(reference)
    validate_name(name)
    if (
        not isinstance(timeout_seconds, (float, int))
        or isinstance(timeout_seconds, bool)
        or not 0 < float(timeout_seconds) < float("inf")
    ):
        raise WorkerError("registry intake deadline must be finite and positive")
    if job_id is None:
        job_id = uuid.uuid4().hex
    if not isinstance(job_id, str) or re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
        raise WorkerError("registry intake job ID must be 32 lowercase hexadecimal characters")
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64") or os.geteuid() != 0:
        raise WorkerError("the cloud layer worker requires root on Linux amd64")
    deadline = time.monotonic() + float(timeout_seconds)
    started = time.time()
    require_ceph_store(store)
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(scratch)
    with _exclusive_lock(scratch / "worker.lock"):
        if any(os.path.lexists(scratch / item) for item in ("buildkit.hold.json", "intake.hold.json")):
            raise WorkerHold("HOLD: a prior worker shutdown or intake requires operator review")
        _directory(store)
        for directory in (
            store / "blobs",
            store / "blobs" / "sha256",
            store / "images",
            store / ".incoming",
            store / "receipts",
            store / "receipts" / "sha256",
            store / "jobs",
            scratch / "jobs",
        ):
            directory.mkdir(mode=0o755 if directory.is_relative_to(store) else 0o700, exist_ok=True)
            _directory(directory)
        job = scratch / "jobs" / job_id
        journal = store / "jobs" / job_id
        if os.path.lexists(job) or os.path.lexists(journal):
            raise WorkerHold("HOLD: registry intake job already exists; automatic replay is forbidden")
        job.mkdir(mode=0o700)
        layers = []
        receipts = []
        acquiring = True
        request = {"job_id": job_id, "reference": reference, "name": name, "platform": "linux/amd64"}
        try:
            _fsync_dir(job.parent)
            journal.mkdir(mode=0o755)
            _fsync_dir(journal.parent)
            _atomic_json(job / "request.json", request)
            _atomic_json(journal / "request.json", request)
            cache_root = scratch / "layer-cache"
            fresh = not os.path.lexists(cache_root)
            if fresh:
                cache_root.mkdir(mode=0o700)
            _directory(cache_root)
            cache = LayerCache(cache_root, initialize=fresh)
            imported = cache.pull(reference, timeout_seconds=_remaining(deadline))
            source, config = _pull_source(imported, reference, pin)
            _atomic_json(journal / "source.json", source)
            acquired = time.time()

            def publish_layer(ordinal: int, path: Path, value: dict) -> None:
                _remaining(deadline)
                if type(ordinal) is not int or ordinal != len(layers) or ordinal >= len(imported["image"]["layers"]):
                    raise WorkerError("materialization callback source ordinal is inconsistent")
                receipt = DerivedLayerReceipt.from_dict(value)
                original = imported["image"]["layers"][ordinal]
                occurrence = {
                    "schema": DERIVED_OCCURRENCE_SCHEMA,
                    "key_digest": receipt.key_digest,
                    "record_digest": receipt.record_digest,
                    "ordinal": ordinal,
                    "source": {
                        "compressed_digest": original["compressed"]["digest"],
                        "compressed_size": original["compressed"]["size"],
                        "media_type": original["compressed"]["mediaType"],
                        "diff_id": original["diff_id"],
                    },
                    "source_image_digest": receipt.source_image_digest,
                    "source_snapshot_binding_digest": receipt.source_snapshot_binding_digest,
                }
                if (
                    receipt.ordinal != ordinal
                    or (receipts and receipt.source_image_digest != receipts[0]["source_image_digest"])
                    or receipt.occurrence_digest != _json_digest(occurrence)
                    or (
                        receipts
                        and receipt.source_snapshot_binding_digest != receipts[0]["source_snapshot_binding_digest"]
                    )
                    or (receipts and receipt.store_id != receipts[0]["store_id"])
                ):
                    raise WorkerError("materialization receipt contradicts its source ordinal or occurrence")
                with _regular_file(path) as stream:
                    if os.fstat(stream.fileno()).st_size != receipt.image_size:
                        raise WorkerError("materialized layer differs from its conversion receipt size")
                published = publish_blob(path, receipt.image_digest, store)
                metadata = {
                    "schema": "palimpsest.cloud-layer-receipt.v1",
                    "root_descriptor": source["root_descriptor"],
                    "manifest_descriptor": source["manifest_descriptor"],
                    "config_descriptor": source["config_descriptor"],
                    "source": original,
                    "conversion": value,
                    "blob": {"digest": published["digest"], "size": published["size"]},
                }
                receipt_digest = _json_digest(metadata)
                relative = f"receipts/sha256/{receipt_digest[7:]}.json"
                _atomic_json(store / relative, metadata)
                layer = {
                    "digest": published["digest"],
                    "size": published["size"],
                    "ordinal": ordinal,
                    "compressed": original["compressed"],
                    "diff_id": original["diff_id"],
                    "conversion_key": receipt.key_digest,
                    "receipt": relative,
                }
                # Commit the immutable fsynced prefix before conversion N+1.
                _atomic_json(journal / f"layer-{ordinal:06d}.json", {**layer, "published": published["published"]})
                layers.append({**layer, "published": published["published"]})
                receipts.append(value)
                _remaining(deadline)

            acquiring = False
            materialized = cache.materialize(
                pin,
                timeout_seconds=_remaining(deadline),
                pack_profile=SquashFSPackProfile.SERVICE_IMAGE,
                on_layer=publish_layer,
            )
            final = materialized["receipt"]
            if (
                materialized["image"] != imported["image"]
                or final["root_descriptor"] != source["root_descriptor"]
                or final["manifest_digest"] != source["manifest_digest"]
                or final["config_descriptor"] != source["config_descriptor"]
                or final["platform"] != {"os": "linux", "architecture": "amd64"}
                or final["layers"] != imported["image"]["layers"]
                or not receipts
                or final["source_image_digest"] != receipts[0]["source_image_digest"]
                or final["source_snapshot_binding_digest"] != receipts[0]["source_snapshot_binding_digest"]
                or len(layers) != len(imported["image"]["layers"])
                or [result["receipt"] for result in final["results"]] != receipts
                or [Path(path).name for path in materialized["blob_paths"]] != [layer["digest"][7:] for layer in layers]
            ):
                raise WorkerError("materialization did not complete the exact published source sequence")
            _remaining(deadline)
            source = {
                **source,
                "image_digest": final["source_image_digest"],
                "snapshot_binding_digest": final["source_snapshot_binding_digest"],
            }
            record = {
                "schema": "palimpsest.cloud-layer-image.v1",
                "name": name,
                "source": source,
                "layers": [{key: value for key, value in layer.items() if key != "published"} for layer in layers],
                "runtime": {
                    "entrypoint": config.get("Entrypoint") or [],
                    "env": config.get("Env") or [],
                    "working_dir": config.get("WorkingDir") or "/",
                    "user": config.get("User") or "0",
                    **({"cmd": config["Cmd"]} if "Cmd" in config else {}),
                },
                "built_at_unix": round(started, 3),
            }
            record = _atomic_json(store / "images" / f"{name}.json", record)
            return {
                **record,
                "job_id": job_id,
                "evidence_directory": str(job),
                "journal_directory": str(journal),
                "publication": [layer["published"] for layer in layers],
                "layer_journals": [f"jobs/{job_id}/layer-{ordinal:06d}.json" for ordinal in range(len(layers))],
                "timings_seconds": {
                    "pull": round(acquired - started, 3),
                    "convert_publish": round(time.time() - acquired, 3),
                },
            }
        except BaseException as error:
            # Deterministic acquisition/source rejection has no live converter
            # or uncertain payload publication. Preserve its failed job without
            # quarantining unrelated future jobs on this worker.
            acquisition_rejection = acquiring and isinstance(error, (ArtifactValidationError, RegistryIntakeError))
            held = isinstance(error, WorkerHold) or (not isinstance(error, WorkerError) and not acquisition_rejection)
            failure = {
                **request,
                "status": "hold" if held else "failed",
                "completed_layers": len(layers),
                "error": f"{type(error).__name__}: {error}",
            }
            try:
                _atomic_json(job / "failure.json", failure)
                if held:
                    _atomic_json(scratch / "intake.hold.json", failure)
                _atomic_json(journal / "failure.json", failure)
            except Exception as journal_error:
                raise WorkerHold(
                    f"HOLD: registry intake failure journal is uncertain; inspect {job}"
                ) from journal_error
            if held and not isinstance(error, WorkerHold):
                raise WorkerHold(f"HOLD: registry intake requires review; inspect {job}: {error}") from error
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m palimpsest_local.cloud_layer_worker")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("build")
    run.add_argument("--dockerfile", type=Path, required=True)
    run.add_argument("--context-tar", type=Path, required=True)
    intake = commands.add_parser("pull")
    intake.add_argument("--reference", required=True)
    intake.add_argument("--job-id")
    for command in (run, intake):
        command.add_argument("--store", type=Path, required=True)
        command.add_argument("--scratch", type=Path, required=True)
        command.add_argument("--name", required=True)
        command.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args(argv)
    try:
        common = {"store": args.store, "scratch": args.scratch, "name": args.name, "timeout_seconds": args.timeout}
        if args.command == "pull":
            result = pull(reference=args.reference, job_id=args.job_id, **common)
        else:
            result = build(dockerfile=args.dockerfile, context_tar=args.context_tar, **common)
    except Exception as error:  # noqa: BLE001 - one JSON failure line for the remote caller
        status = "hold" if isinstance(error, WorkerHold) else "failed"
        print(json.dumps({"status": status, "error": f"{type(error).__name__}: {error}"}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "success", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
