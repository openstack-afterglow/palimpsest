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
        "publication_failure": record.get("publication_failure"),
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


def upload(instance, record: dict, result: dict, deadline: float | None) -> None:
    """Resume only immutable packed bytes, fenced by authoritative receipts.

    No executor invocation or lease renewal occurs here. A lost mutation is
    settled by a lock-serialized status read before any exact retry.
    """
    from .services.digest import compute_config_digest

    result = validate_result(result, instance.settings.palimpsest_hub_max_blob_bytes)
    fingerprint = compute_config_digest(result)
    progress = record.get("publication")
    if progress is not None and progress.get("result_digest") != fingerprint:
        raise worker.WorkerHalted("image publication journal conflicts")
    streams = []
    operation = "status"
    http_status = None
    initial_deadline_missing = deadline is None

    def status():
        nonlocal http_status
        with instance.transport.request(
            "GET", instance._route(record), lease=record["lease"], deadline=deadline, wait_for_response=True
        ) as response:
            http_status = response.status
            if response.status != 200:
                raise worker.WorkerHalted("image publication receipt unavailable")
            return worker._json_response(response)

    def receipt(value):
        if value.get("id") != record["id"] or value.get("status") not in {"building", "complete", "error"}:
            raise worker.WorkerHalted("invalid image publication receipt")
        publication = value.get("publication")
        if not isinstance(publication, dict) or publication.get("version") != 1:
            raise worker.WorkerHalted("image publication receipt unsupported")
        bound = publication.get("result_digest")
        if bound is not None and bound != fingerprint:
            raise worker.WorkerHalted("image publication descriptor conflicts")
        if value["status"] == "complete":
            if bound != fingerprint or value.get("result") != result:
                raise worker.WorkerHalted("completed image descriptor conflicts")
            return publication, None
        if value["status"] == "error":
            return publication, None
        expiry = datetime.fromisoformat(publication["deadline"])
        if expiry.tzinfo is None or expiry.utcoffset().total_seconds() != 0:
            raise worker.WorkerHalted("invalid image publication deadline")
        remaining = (expiry - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise worker.WorkerHalted("image publication lease expired")
        expected = {item["digest"]: item["size_bytes"] for item in result["layers"]}
        staged = publication.get("staged_outputs")
        if not isinstance(staged, list) or len(staged) > 128:
            raise worker.WorkerHalted("invalid image staged receipt")
        seen = set()
        for item in staged:
            if not isinstance(item, dict) or set(item) != {"digest", "size_bytes"}:
                raise worker.WorkerHalted("invalid image staged descriptor")
            if item["digest"] in seen or expected.get(item["digest"]) != item["size_bytes"]:
                raise worker.WorkerHalted("image staged descriptor conflicts")
            seen.add(item["digest"])
        return publication, time.monotonic() + remaining

    def pin(end):
        offset = 0
        reserve = worker._size(
            record["output_reserve_bytes"], instance.settings.palimpsest_hub_image_scratch_bytes // 4
        )
        for descriptor in result["layers"]:
            fd = worker._open_private(instance.journal.job_dir(record) / "outputs.pack")
            stream = os.fdopen(fd, "rb")
            streams.append((stream, offset))
            if os.fstat(fd).st_size != reserve or offset + descriptor["size_bytes"] > reserve:
                raise worker.BuildFailed("invalid image output reserve")
            stream.seek(offset)
            if stream.read(4) != b"hsqs":
                raise worker.BuildFailed("invalid image output")
            digest = hashlib.sha256()
            body = worker.PinnedBody(
                _PackedRange(stream, offset, descriptor["size_bytes"], reserve),
                descriptor["size_bytes"],
                end,
                instance.stop,
            )
            for chunk in iter(body.read, b""):
                digest.update(chunk)
            if "sha256:" + digest.hexdigest() != descriptor["digest"]:
                raise worker.BuildFailed("image output digest mismatch")
            offset += descriptor["size_bytes"]
        return reserve

    try:
        initial = None
        if deadline is None and progress is not None and progress.get("deadline") is not None:
            expiry = datetime.fromisoformat(progress["deadline"])
            if expiry.tzinfo is None or expiry.utcoffset().total_seconds() != 0:
                raise worker.WorkerHalted("invalid saved image publication deadline")
            remaining = (expiry - datetime.now(UTC)).total_seconds()
            # Expired leases still need a bounded read to settle terminal state;
            # they never authorize another mutation or gain more lease time.
            if remaining > 0:
                deadline = time.monotonic() + remaining
        if initial_deadline_missing:
            initial = status()
            _, deadline = receipt(initial)
            if deadline is None:
                instance._dispose(record)
                return
        reserve_bytes = pin(deadline)
        record.update(phase="output_pending", output=result)
        progress = progress or {"result_digest": fingerprint, "outputs": {}, "completion": "unattempted"}
        record["publication"] = progress
        instance.journal.save(record)
        value = initial or status()
        publication, server_end = receipt(value)
        if server_end is None:
            instance._dispose(record)
            return
        previous_deadline = progress.get("deadline")
        if previous_deadline is not None and previous_deadline != publication["deadline"]:
            raise worker.WorkerHalted("image publication deadline changed")
        progress["deadline"] = publication["deadline"]
        instance.journal.save(record)
        deadline = min(deadline, server_end)
        if publication["result_digest"] is None:
            operation, http_status = "intent", None
            progress["intent"] = "attempted"
            instance.journal.save(record)
            payload = json.dumps(result).encode()
            with instance.transport.request(
                "POST",
                instance._route(record, "/publication-intent"),
                lease=record["lease"],
                body=payload,
                deadline=deadline,
                wait_for_response=True,
                headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
            ) as response:
                http_status = response.status
                if response.status != 200:
                    raise worker.WorkerHalted("image publication intent uncertain")
                value = worker._json_response(response)
            publication, server_end = receipt(value)
            if server_end is None:
                instance._dispose(record)
                return
            if publication["result_digest"] != fingerprint:
                raise worker.WorkerHalted("image publication intent unbound")
            deadline = min(deadline, server_end)
        present = {item["digest"] for item in publication["staged_outputs"]}
        for (stream, offset), descriptor in zip(streams, result["layers"], strict=True):
            digest = descriptor["digest"]
            if digest in present:
                progress["outputs"][digest] = "confirmed"
                continue
            operation, http_status = "stage", None
            progress["outputs"][digest] = "attempted"
            instance.journal.save(record)
            with instance.transport.request(
                "PUT",
                instance._route(record, "/outputs/" + digest),
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
                    "X-Content-Digest": digest,
                    "Content-Type": "application/octet-stream",
                },
            ) as response:
                http_status = response.status
                if response.status != 200:
                    raise worker.WorkerHalted("image output disposition uncertain")
                worker._json_response(response)
            present.add(digest)
            progress["outputs"][digest] = "confirmed"
            instance.journal.save(record)
        # This read shares the server output lock with PUT/complete. A previous
        # completion must finish before it can authorize another exact attempt.
        operation, http_status = "status", None
        value = status()
        publication, server_end = receipt(value)
        if server_end is None:
            instance._dispose(record)
            return
        if (
            publication["result_digest"] != fingerprint
            or {item["digest"] for item in publication["staged_outputs"]} != present
        ):
            raise worker.WorkerHalted("image publication is not ready")
        operation, http_status = "complete", None
        progress["completion"] = "attempted"
        instance.journal.save(record)
        payload = json.dumps(result).encode()
        with instance.transport.request(
            "POST",
            instance._route(record, "/complete"),
            lease=record["lease"],
            body=payload,
            deadline=min(deadline, server_end),
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        ) as response:
            http_status = response.status
            if response.status != 200:
                raise worker.WorkerHalted("image completion disposition uncertain")
            receipt(worker._json_response(response))
        operation, http_status = "verify", None
        instance._status(record)
        instance._dispose(record)
    except Exception as exc:
        if record.get("phase") == "output_pending":
            record["publication_failure"] = {
                "operation": operation,
                "http_status": http_status,
                "category": worker._safe_error_class(exc),
                "errno": exc.errno if isinstance(exc, OSError) and type(exc.errno) is int else None,
            }
            instance.journal.save(record)
            retain_failure(instance, record, exc)
            raise worker.WorkerHalted("image publication retained for receipt settlement") from exc
        raise
    finally:
        for stream, _ in streams:
            stream.close()
