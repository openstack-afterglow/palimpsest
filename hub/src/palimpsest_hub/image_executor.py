"""Image-job worker protocol; mutation uncertainty is never an execution retry."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from datetime import UTC, datetime

from . import build_worker as worker


def validate_claim(value: dict, settings) -> dict:
    worker._uuid(value["id"])
    worker._uuid(value["lease"])
    deadline = datetime.fromisoformat(value["deadline"])
    if deadline.tzinfo is None or deadline.utcoffset().total_seconds() != 0:
        raise ValueError("invalid image lease")
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    seconds = value["timeout_seconds"]
    if type(seconds) not in (int, float) or not 0 < seconds <= 3600 or remaining <= 0:
        raise ValueError("invalid image timeout")
    if value["kind"] not in ("registry", "dockerfile") or value["platform"] != "linux/amd64":
        raise ValueError("unsupported image job")
    if not isinstance(value["name"], str) or not 0 < len(value["name"]) <= 256:
        raise ValueError("invalid image name")
    maximum = worker._size(value["max_output_bytes"], settings.palimpsest_hub_max_blob_bytes)
    request = value["request"]
    if not isinstance(request, dict):
        raise ValueError("invalid image request")
    if value["kind"] == "registry":
        from palimpsest_local.hub_image_builder import normalize_reference

        # Resolve again using native source configuration; the isolated child
        # repeats this check rather than trusting an admitted claim blindly.
        request = {
            **request,
            "reference": normalize_reference(request["reference"], settings.palimpsest_hub_registry_hosts),
        }
    else:
        text = request["dockerfile"]
        if not isinstance(text, str) or not 0 < len(text.encode()) <= worker.RECIPE_LIMIT:
            raise ValueError("invalid Dockerfile")
        args = request.get("build_args", {})
        if (
            not isinstance(args, dict)
            or len(args) > 128
            or any(
                not isinstance(k, str)
                or not isinstance(v, str)
                or len(k) > 256
                or len(v) > 8192
                or k.startswith("BUILDKIT_")
                or "\0" in k + v
                for k, v in args.items()
            )
        ):
            raise ValueError("invalid build arguments")
        target = request.get("target")
        if target is not None and (not isinstance(target, str) or not 0 < len(target) <= 128 or "\0" in target):
            raise ValueError("invalid target")
    context = value.get("context")
    if context is not None:
        if value["kind"] != "dockerfile" or worker._digest(context["digest"]) != request.get("context_digest"):
            raise ValueError("invalid context binding")
        worker._size(context["size_bytes"], settings.palimpsest_hub_max_context_bytes)
    elif request.get("context_digest") is not None:
        raise ValueError("missing context descriptor")
    return {
        **value,
        "request": request,
        "max_output_bytes": maximum,
        "timeout_seconds": min(float(seconds), remaining, settings.palimpsest_hub_build_timeout_seconds),
    }


def build(instance, claim: dict, record: dict, deadline: float) -> dict:
    directory = instance.journal.job_dir(record)
    directory.mkdir(mode=0o700)
    manifest = {key: claim[key] for key in ("kind", "name", "platform", "request", "max_output_bytes")}
    manifest["context"] = claim.get("context")
    settings = instance.settings
    manifest.update(
        registry_hosts=list(settings.palimpsest_hub_registry_hosts),
        max_context_expanded_bytes=settings.palimpsest_hub_max_context_expanded_bytes,
        max_context_members=settings.palimpsest_hub_max_context_members,
        run_network=settings.palimpsest_hub_image_run_network,
        cgroup_root=settings.palimpsest_hub_image_cgroup_root,
        scratch_bytes=settings.palimpsest_hub_image_scratch_bytes,
    )
    if claim.get("context") is not None:
        instance._download(record, claim["context"], directory / "context.tar", deadline)
    manifest["timeout_seconds"] = deadline - time.monotonic()
    if manifest["timeout_seconds"] <= 0:
        raise worker.BuildTimeout("image build deadline exceeded")
    path = directory / "input.json"
    fd = worker._open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "w") as stream:
        json.dump(manifest, stream)
        stream.flush()
        os.fsync(stream.fileno())
    record["output_reserve_bytes"] = min(claim["max_output_bytes"], settings.palimpsest_hub_image_scratch_bytes // 4)
    record["phase"] = "executing"
    instance.journal.save(record)
    worker.run_executor(
        [str(path), str(os.getpid())],
        manifest["timeout_seconds"],
        instance.stop,
        module="palimpsest_local.hub_image_builder",
        log=directory / "executor.log",
    )
    fd = worker._open_private(directory / "result.json")
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(worker.JSON_LIMIT + 1)
    if len(raw) > worker.JSON_LIMIT:
        raise worker.BuildFailed("image result exceeds limit")
    return validate_result(json.loads(raw), claim["max_output_bytes"])


FAILURE_FILE = "last-image-failure.json"
FAILURE_LIMIT = 4 * worker.LOG_LIMIT + 4096


def retain_failure(instance, record: dict, exc: Exception | None = None) -> None:
    """Called only AFTER exact executor cleanup; never read mounted scratch.

    Fixed host-side filenames only. One atomic owner-only artifact replaces the
    previous failure, independently of the operation journal and job disposal.
    Raw tool tails are private evidence, never API fields or logger arguments.
    """
    directory = instance.journal.job_dir(record)
    tails = {}
    for name in ("executor.log", "buildctl.log", "buildkitd.log"):
        try:
            fd = worker._open_private(directory / name)
        except FileNotFoundError:
            continue
        with os.fdopen(fd, "rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - worker.LOG_LIMIT))
            tails[name] = base64.b64encode(stream.read(worker.LOG_LIMIT)).decode("ascii")
    value = {
        "job_id": worker._uuid(record["id"]),
        "recorded_at": datetime.now(UTC).isoformat(),
        "cause": worker._safe_error_class(exc) if exc is not None else "Interrupted",
        "errno": exc.errno if isinstance(exc, OSError) and type(exc.errno) is int else None,
        "tails_base64": tails,
    }
    raw = json.dumps(value, ensure_ascii=True).encode("ascii")
    if len(raw) > FAILURE_LIMIT:
        raise worker.WorkerHalted("image failure evidence exceeds limit")
    target = instance.journal.root / FAILURE_FILE
    temporary = instance.journal.root / (FAILURE_FILE + ".new")
    fd = worker._open_private(temporary, os.O_CREAT | os.O_WRONLY | os.O_EXCL)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    instance.journal._sync()


def validate_result(result: dict, maximum: int) -> dict:
    if not isinstance(result, dict) or set(result) != {"layers", "source", "runtime"}:
        raise ValueError("invalid image result")
    layers = result["layers"]
    if not isinstance(layers, list) or not 0 < len(layers) <= 128:
        raise ValueError("invalid ordered image layers")
    total = 0
    for layer in layers:
        if set(layer) != {"digest", "size_bytes"}:
            raise ValueError("invalid layer descriptor")
        worker._digest(layer["digest"])
        total += worker._size(layer["size_bytes"], maximum)
    if total > maximum:
        raise ValueError("image output exceeds limit")
    if set(result["source"]) != {"root_digest", "manifest_digest", "config_digest"}:
        raise ValueError("invalid source provenance")
    for value in result["source"].values():
        worker._digest(value)
    runtime = result["runtime"]
    if set(runtime) != {"entrypoint", "cmd", "env", "working_dir", "user"}:
        raise ValueError("invalid runtime metadata")
    for key in ("entrypoint", "cmd", "env"):
        count = 1024 if key == "env" else 256
        if (
            not isinstance(runtime[key], list)
            or len(runtime[key]) > count
            or any(not isinstance(value, str) or "\0" in value or len(value.encode()) > 16384 for value in runtime[key])
        ):
            raise ValueError("invalid runtime metadata")
    for key in ("working_dir", "user"):
        count = 4096 if key == "working_dir" else 256
        if not isinstance(runtime[key], str) or "\0" in runtime[key] or len(runtime[key]) > count:
            raise ValueError("invalid runtime metadata")
    if len(json.dumps(result).encode()) > 512 * 1024:
        raise ValueError("image completion descriptor exceeds wire limit")
    return result


class _PackedRange:
    """A pinned regular-file span, with EOF independent of adjacent layers."""

    def __init__(self, stream, offset: int, size: int, reserve_bytes: int):
        self.stream = stream
        info = os.fstat(stream.fileno())
        self.identity = (info.st_dev, info.st_ino, info.st_size)
        if info.st_size != reserve_bytes or not 0 <= offset < offset + size <= reserve_bytes:
            raise worker.BuildFailed("invalid packed output span")
        self.remaining = size
        stream.seek(offset)

    def read(self, size: int = -1) -> bytes:
        info = os.fstat(self.stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size) != self.identity:
            raise worker.BuildFailed("packed output changed")
        amount = self.remaining if size < 0 else min(size, self.remaining)
        if not amount:
            return b""
        data = self.stream.read(amount)
        if len(data) != amount:
            raise worker.BuildFailed("packed output span truncated")
        self.remaining -= len(data)
        return data


def upload(instance, record: dict, result: dict, deadline: float) -> None:
    # Pin and validate every output before the first remote mutation. This
    # journaled phase is a one-way fence: interrupted staging is never replayed.
    streams = []
    offset = 0
    reserve_bytes = worker._size(
        record["output_reserve_bytes"], instance.settings.palimpsest_hub_image_scratch_bytes // 4
    )
    try:
        for descriptor in result["layers"]:
            fd = worker._open_private(instance.journal.job_dir(record) / "outputs.pack")
            stream = os.fdopen(fd, "rb")
            streams.append((stream, offset))
            if os.fstat(fd).st_size != reserve_bytes or offset + descriptor["size_bytes"] > reserve_bytes:
                raise worker.BuildFailed("invalid image output reserve")
            stream.seek(offset)
            if stream.read(4) != b"hsqs":
                raise worker.BuildFailed("invalid image output")
            stream.seek(offset)
            digest = hashlib.sha256()
            body = worker.PinnedBody(
                _PackedRange(stream, offset, descriptor["size_bytes"], reserve_bytes),
                descriptor["size_bytes"],
                deadline,
                instance.stop,
            )
            for chunk in iter(body.read, b""):
                digest.update(chunk)
            if "sha256:" + digest.hexdigest() != descriptor["digest"]:
                raise worker.BuildFailed("image output digest mismatch")
            stream.seek(offset)
            offset += descriptor["size_bytes"]
        record.update(phase="output_pending", output=result)
        instance.journal.save(record)
        try:
            uploaded = set()
            for (stream, offset), descriptor in zip(streams, result["layers"], strict=True):
                if descriptor["digest"] in uploaded:
                    continue
                with instance.transport.request(
                    "PUT",
                    instance._route(record, "/outputs/" + descriptor["digest"]),
                    lease=record["lease"],
                    body=worker.PinnedBody(
                        _PackedRange(stream, offset, descriptor["size_bytes"], reserve_bytes),
                        descriptor["size_bytes"],
                        deadline,
                        instance.stop,
                    ),
                    deadline=deadline,
                    headers={
                        "Content-Length": str(descriptor["size_bytes"]),
                        "X-Content-Digest": descriptor["digest"],
                        "Content-Type": "application/octet-stream",
                    },
                ) as response:
                    if response.status != 200:
                        raise worker.WorkerHalted("image output disposition uncertain")
                    worker._json_response(response)
                uploaded.add(descriptor["digest"])
            payload = json.dumps(result).encode()
            with instance.transport.request(
                "POST",
                instance._route(record, "/complete"),
                lease=record["lease"],
                body=payload,
                deadline=deadline,
                headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
            ) as response:
                if response.status != 200:
                    raise worker.WorkerHalted("image completion disposition uncertain")
                worker._json_response(response)
        except Exception:
            instance._status(record)
        else:
            instance._status(record)
    finally:
        for stream, _ in streams:
            stream.close()
    instance._dispose(record)
