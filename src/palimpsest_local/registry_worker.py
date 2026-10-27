"""Internal acquisition jobs over the canonical permanent OCI cache and asset catalog.

Execute this module in the worker instance with its already mounted private cache.
No registry blobs are acquired by a client forwarding the job. Images are data until
an explicit consumer policy activates their verified layers; registry metadata never
becomes root activation commands.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import time
from pathlib import Path

from .errors import ArtifactValidationError, PalimpsestError
from .layer_cache import LayerCache, _directory, _private_directory, _read_file
from .nova_assets import AssetCatalog
from .oci_image import strict_json_object
from .registry_intake import RegistryIntakeError, resolve_anonymous_reference
from .state import atomic_write_json

SCHEMA = "palimpsest.registry-worker-job.v1"
RESULT_SCHEMA = "palimpsest.registry-worker-result.v1"
_JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _fail(message: str) -> None:
    raise ArtifactValidationError("registry worker: " + message)


def validate_request(value: dict) -> dict:
    required = {"schema", "job_id", "project_id", "reference"}
    optional = {"timeout_seconds", "packer", "consumer"}
    if not isinstance(value, dict) or not required <= value.keys() or set(value) - required - optional:
        _fail("request fields are invalid")
    if value["schema"] != SCHEMA:
        _fail("unsupported job schema")
    for field in ("job_id", "project_id"):
        if not isinstance(value[field], str) or _JOB.fullmatch(value[field]) is None:
            _fail("job and project must be exact bounded identifiers")
    try:
        reference = resolve_anonymous_reference(value["reference"]).canonical
    except RegistryIntakeError:
        _fail("reference must be an explicit credential-free OCI registry reference")
    timeout = value.get("timeout_seconds", 3600.0)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
        _fail("stage timeout must be between one and 3600 seconds")
    packer = value.get("packer", "/usr/bin/mksquashfs")
    if not isinstance(packer, str) or not Path(packer).is_absolute() or ".." in Path(packer).parts:
        _fail("packer must be an explicit absolute worker path")
    consumer = value.get("consumer")
    if consumer is not None and (
        not isinstance(consumer, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}", consumer) is None
    ):
        _fail("invalid consumer identifier")
    return {**value, "reference": reference, "timeout_seconds": float(timeout), "packer": packer}


def _request_digest(request: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_job(request: dict, *, cache_root: Path, jobs_root: Path, assets_root: Path) -> dict:
    """Acquire once inside this worker, convert, register and durably settle the exact job."""
    request = validate_request(request)
    job_id = request["job_id"]
    jobs_root = jobs_root.expanduser().absolute()
    cache = LayerCache(cache_root)
    assets = AssetCatalog(assets_root, request["project_id"])
    with _directory(jobs_root) as jobs_fd:
        _private_directory(jobs_fd)
        name = job_id + ".json"
        lock_fd = os.open(
            job_id + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=jobs_fd
        )
        try:
            lock_info = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != os.geteuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600
            ):
                _fail("job lock is not an owner-private regular file")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            identity = _request_digest(request)
            try:
                previous = strict_json_object(
                    _read_file(jobs_fd, name, mode=0o600, maximum=4 * 1024 * 1024), "registry job"
                )
            except FileNotFoundError:
                previous = None
            if previous is not None:
                if previous.get("request_digest") != identity:
                    _fail("job ID is already bound to a different immutable request")
                if previous.get("status") != "success":
                    _fail("prior job is failed or uncertain; preserve it and choose a new explicit job ID")
                cache.inspect(previous["root_digest"])
                assets.inspect(previous["asset_id"], verify=True)
                return {**previous, "reused_completed_job": True}
            result = {
                "schema": RESULT_SCHEMA,
                "job_id": job_id,
                "project_id": request["project_id"],
                "request_digest": identity,
                "reference": request["reference"],
                "status": "intent",
                "worker": {"hostname": socket.gethostname(), "machine_id_sha256": None},
                "started_unix_seconds": time.time(),
                "started_monotonic_seconds": time.monotonic(),
                "stage": "acquire",
            }
            machine_id = Path("/etc/machine-id")
            if machine_id.is_file():
                result["worker"]["machine_id_sha256"] = hashlib.sha256(machine_id.read_bytes()).hexdigest()
            boot_id = Path("/proc/sys/kernel/random/boot_id")
            if boot_id.is_file():
                result["worker"]["boot_id"] = boot_id.read_text().strip()
            path = jobs_root / name
            atomic_write_json(path, result)
            started = time.monotonic()
            try:
                acquired = cache.pull(request["reference"], timeout_seconds=request["timeout_seconds"])
                acquired_at = time.monotonic()
                result.update(root_digest=acquired["root_digest"], stage="materialize")
                atomic_write_json(path, result)
                converted = cache.materialize(
                    acquired["root_digest"],
                    packer_path=Path(request["packer"]),
                    timeout_seconds=request["timeout_seconds"],
                )
                converted_at = time.monotonic()
                receipt = converted["receipt"]
                layers = []
                for source, output in zip(acquired["image"]["layers"], receipt["results"], strict=True):
                    derived = output["receipt"]
                    layers.append(
                        {
                            "digest": derived["image_digest"],
                            "size": derived["image_size"],
                            "compressed_digest": source["compressed"]["digest"],
                            "diff_id": source["diff_id"],
                            "recipe_digest": derived["key_digest"],
                        }
                    )
                source = {
                    "kind": "oci-registry",
                    "reference": request["reference"],
                    "root_digest": acquired["root_digest"],
                    "manifest_digest": acquired["image"]["manifest_descriptor"]["digest"],
                    "config_digest": acquired["image"]["config_descriptor"]["digest"],
                }
                result["stage"] = "register"
                atomic_write_json(path, result)
                registered = assets.register(
                    arch="x86_64",
                    source=source,
                    layers=layers,
                    blob_paths=[Path(p) for p in converted["blob_paths"]],
                    name=job_id,
                )
                if request.get("consumer") is not None:
                    cache.retain(acquired["root_digest"], request["consumer"])
                result.update(
                    status="success",
                    stage="complete",
                    asset_id=registered["asset_id"],
                    arch="x86_64",
                    platform=acquired["image"]["platform"],
                    graph=registered["descriptor"],
                    materialization=receipt,
                    blob_paths=converted["blob_paths"],
                    retention="permanent",
                    phase_monotonic_seconds={
                        "acquire_started": started,
                        "acquire_completed": acquired_at,
                        "materialize_completed": converted_at,
                        "registration_completed": time.monotonic(),
                    },
                    timings={
                        "acquire_seconds": acquired_at - started,
                        "materialize_seconds": converted_at - acquired_at,
                        "registration_seconds": time.monotonic() - converted_at,
                        "total_seconds": time.monotonic() - started,
                    },
                )
                atomic_write_json(path, result)
                return result
            except BaseException as error:
                result.update(
                    status="error", error_class=type(error).__name__, elapsed_seconds=time.monotonic() - started
                )
                atomic_write_json(path, result)
                raise
        finally:
            os.close(lock_fd)


def inspect_job(job_id: str, *, jobs_root: Path, cache_root: Path, assets_root: Path, project_id: str) -> dict:
    if not isinstance(job_id, str) or _JOB.fullmatch(job_id) is None:
        _fail("invalid job identity")
    with _directory(jobs_root.expanduser().absolute()) as fd:
        _private_directory(fd)
        result = strict_json_object(
            _read_file(fd, job_id + ".json", mode=0o600, maximum=4 * 1024 * 1024), "registry job"
        )
    if result.get("project_id") != project_id or result.get("job_id") != job_id:
        _fail("job belongs to another project or identity")
    if result.get("status") == "success":
        LayerCache(cache_root).inspect(result["root_digest"])
        AssetCatalog(assets_root, project_id).inspect(result["asset_id"], verify=True)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--assets-root", type=Path, required=True)
    operations = parser.add_subparsers(dest="operation", required=True)
    execute = operations.add_parser("run")
    execute.add_argument("request", type=Path)
    inspect = operations.add_parser("inspect")
    inspect.add_argument("job_id")
    inspect.add_argument("--project-id", required=True)
    args = parser.parse_args(argv)
    try:
        roots = {"cache_root": args.cache_root, "jobs_root": args.jobs_root, "assets_root": args.assets_root}
        if args.operation == "run":
            result = run_job(strict_json_object(args.request.read_bytes(), "registry job request"), **roots)
        else:
            result = inspect_job(args.job_id, project_id=args.project_id, **roots)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (PalimpsestError, OSError, ValueError) as error:
        parser.exit(1, "registry worker failed: " + type(error).__name__ + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
