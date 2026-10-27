"""Explicit Nova/worker commands, isolated from local libvirt and host state roots."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .errors import ArtifactValidationError, PalimpsestError
from .nova_assets import AssetCatalog
from .nova_build import NovaController, build_nova_layer_user_data, spec_digest, validate_build_spec
from .oci_image import strict_json_object


def _json(path: Path) -> dict:
    try:
        return strict_json_object(path.expanduser().read_bytes(), "cloud command config")
    except (OSError, ValueError) as error:
        raise ArtifactValidationError("cannot read cloud command config") from error


def _connect(cloud: str):
    try:
        import openstack
    except ImportError:
        raise PalimpsestError("Nova commands require palimpsest-client[cloud]") from None
    # No --insecure or arbitrary credential forwarding to a guest.
    connection = openstack.connect(
        cloud=cloud, interface="public", verify=True, compute_api_version="2.60", api_timeout=40
    )
    connection.authorize()
    return connection


def _controller(config: dict) -> NovaController:
    return NovaController(_connect(config["cloud"]), config["project_id"], Path(config["receipts"]))


def register_build(catalog: AssetCatalog, result: dict, *, name: str | None = None) -> dict:
    if (
        result.get("schema") != "palimpsest.nova-build-result.v1"
        or result.get("status") != "success"
        or result.get("mode") != "capture"
        or result.get("project_id") != catalog.project_id
        or not result.get("boot_id")
        or type(result.get("checks_succeeded")) is not int
        or result["checks_succeeded"] < 1
    ):
        raise ArtifactValidationError("only successful, checked, project-bound capture results can become assets")
    output = result["output"]
    blob = Path(output["path"])
    if (
        blob.name != output["digest"].split(":")[-1]
        or blob.parent.name != "sha256"
        or blob.parent.parent.name != "blobs"
    ):
        raise ArtifactValidationError("build output must be in its digest-addressed attached layer store")
    parents = result["parent_layers"]
    layers = [*parents, {"digest": output["digest"], "size": output["size"]}]
    return catalog.register(
        arch=result["arch"],
        source={
            "kind": "nova-build",
            "base_image_id": result["base_image_id"],
            "recipe_digest": result["recipe_digest"],
        },
        layers=layers,
        blob_paths=[blob.parent / layer["digest"].split(":")[1] for layer in layers],
        name=name,
    )


def launch_batch(config: dict) -> dict:
    """Verify/preacquire before fan-out; every consumer mounts the same frozen RO store."""
    catalog = AssetCatalog(Path(config["assets_root"]), config["project_id"])
    inspected = catalog.inspect(config["asset_id"], verify=True)
    asset_id = next(iter(inspected["assets"]))
    requests = config["instances"]
    if not isinstance(requests, list) or not 1 <= len(requests) <= 16:
        raise ArtifactValidationError("batch requires one to sixteen explicit instance requests")
    consumers = [request["job_id"] for request in requests]
    reservation = catalog.retain_many(asset_id, consumers, volume_id=config["volume_id"])

    # Keep the reservation on uncertain/partial provider failures. Never release a
    # potentially active consumer or delete an unrelated instance to repair a batch.
    def create(request):
        controller = _controller(config)
        binding = {"id": config["volume_id"], "read_only": True}
        userdata = Path(request["user_data"]).expanduser().read_text()
        return controller.create(
            **{k: v for k, v in request.items() if k != "user_data"}, user_data=userdata, data_volumes=[binding]
        )

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        results = list(pool.map(create, requests))
    return {"reservation": reservation, "instances": results, "phase": "created-not-ready"}


def run_nova_build(config: dict, *, name: str) -> dict:
    """Wait for the actual cloud-init result and register it inside the worker's mounted catalog.

    The operator supplies a pinned SSH host key and a worker Python containing this
    reviewed package, typically installed from a retained wheel in the prepared
    layer filesystem. No cloud credential is passed through this transport.
    """
    spec = validate_build_spec(config["spec"])
    if spec["mode"] != "capture" or spec["project_id"] != config["project_id"]:
        raise ArtifactValidationError("Nova build requires a project-bound capture specification")
    transport = config["ssh"]
    user = transport["user"]
    if not isinstance(user, str) or not user or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in user):
        raise ArtifactValidationError("SSH user must be an explicit safe Unix account")
    for key in ("identity_file", "known_hosts"):
        if not isinstance(transport[key], str) or not Path(transport[key]).expanduser().is_file():
            raise ArtifactValidationError("pinned SSH identity and known-hosts files must exist before allocation")
    if not isinstance(transport["python"], str) or not transport["python"].startswith("/"):
        raise ArtifactValidationError("worker Python must be an explicit absolute installed path")
    if not isinstance(config["assets_root"], str) or not config["assets_root"].startswith("/srv/palimpsest/"):
        raise ArtifactValidationError("worker asset catalog must be on the explicit /srv/palimpsest storage")
    try:
        import yaml
    except ImportError:
        raise PalimpsestError("Nova cloud-init composition requires palimpsest-client[cloud]") from None
    payload = yaml.safe_load(build_nova_layer_user_data(spec))
    additions = config.get("cloud_init", {})
    if not isinstance(additions, dict):
        raise ArtifactValidationError("cloud_init additions must be an operator-owned mapping")
    for key, value in additions.items():
        if key in {"write_files", "runcmd", "bootcmd", "packages"}:
            if not isinstance(value, list):
                raise ArtifactValidationError("cloud-init list additions must be lists")
            payload[key] = [*payload.get(key, []), *value]
        elif key in payload and payload[key] != value:
            raise ArtifactValidationError("cloud-init additions cannot replace builder policy")
        else:
            payload[key] = value
    rendered = "#cloud-config\n" + yaml.safe_dump(payload, sort_keys=False)
    launch = dict(config["launch"])
    if launch.get("job_id") != spec["job_id"] or launch.get("image_id") != spec["base_image_id"]:
        raise ArtifactValidationError("Nova launch identity does not match the build specification")
    launch["data_volumes"] = [{"id": spec["storage"]["volume_id"], "read_only": False}]
    controller = _controller(config)
    controller.create(**launch, user_data=rendered)
    active = controller.wait_active(spec["job_id"])
    addresses = sorted(
        {
            address["addr"]
            for values in active["addresses"].values()
            for address in values
            if address.get("OS-EXT-IPS:type") == "fixed" and address.get("version") == 4
        }
    )
    if len(addresses) != 1:
        raise ArtifactValidationError("new Nova builder must expose one unambiguous private IPv4 address")
    target = user + "@" + addresses[0]
    prefix = [
        "ssh",
        "-i",
        os.fspath(Path(transport["identity_file"]).expanduser()),
        "-o",
        "UserKnownHostsFile=" + os.fspath(Path(transport["known_hosts"]).expanduser()),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "HostKeyAlias=" + spec["job_id"],
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    if transport.get("jump"):
        prefix += ["-J", transport["jump"]]
    prefix += [target]
    result_path = "/srv/palimpsest/builds/" + spec["job_id"] + "/result.json"
    # The helper writes result.json only after success. Its existing systemd
    # failed state is terminal evidence, not another reason to wait four hours.
    probe = (
        "sudo -n cat "
        + shlex.quote(result_path)
        + " || { if sudo -n systemctl is-failed --quiet palimpsest-nova-build.service; "
        "then exit 42; else exit 1; fi; }"
    )
    deadline = time.monotonic() + 14400
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PalimpsestError("cloud-init build result deadline expired; owned resources retained")
        try:
            received = subprocess.run([*prefix, probe], capture_output=True, text=True, timeout=min(30, remaining))
        except subprocess.TimeoutExpired:
            received = None
        if received is not None:
            if received.returncode == 42:
                raise PalimpsestError("cloud-init build service failed; owned resources and journal retained")
            if received.returncode == 0:
                result = strict_json_object(received.stdout.encode(), "Nova guest result")
                if result.get("recipe_digest") != spec_digest(spec):
                    raise ArtifactValidationError("guest recipe digest differs from the requested build")
                controller.record_guest_result(spec["job_id"], result)
                break
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    # Registration executes where the actual retained payload lives. Do not create
    # a metadata-only client-side success or copy multi-GB layers back to the Mac.
    remote = [
        "sudo",
        "-n",
        transport["python"],
        "-m",
        "palimpsest_local.cli",
        "cloud",
        "assets",
        "--root",
        config["assets_root"],
        "--project-id",
        config["project_id"],
        "register-build",
        "--result",
        result_path,
        "--name",
        name,
    ]
    try:
        published = subprocess.run([*prefix, shlex.join(remote)], capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired as error:
        raise PalimpsestError(
            "build passed but worker asset registration timed out; registration is uncertain, result and volumes retained"
        ) from error
    if published.returncode != 0:
        raise PalimpsestError("build passed but worker asset registration failed; result and volumes retained")
    return {
        "build": controller.inspect(spec["job_id"]),
        "asset": strict_json_object(published.stdout.encode(), "registered cloud asset"),
    }


def add_parser(commands) -> None:
    cloud = commands.add_parser("cloud", help="Nova GPU builds, persistent assets and internal registry workers")
    operations = cloud.add_subparsers(dest="cloud_operation", required=True)
    emitter = operations.add_parser("build-user-data", help="emit cloud-init for a checked attached-storage Nova build")
    emitter.add_argument("config", type=Path)
    emitter.add_argument("--output", type=Path)
    build = operations.add_parser("build", help="create a Nova cloud-init builder and register checked layer output")
    build.add_argument("config", type=Path)
    build.add_argument("--name", required=True, help="immutable output asset name")
    instance = operations.add_parser("instance", help="operate only exact-owned Nova job receipts")
    instance.add_argument("--config", type=Path, required=True)
    instance_ops = instance.add_subparsers(dest="instance_operation", required=True)
    create = instance_ops.add_parser("create")
    create.add_argument("--user-data", type=Path, required=True)
    for operation in ("inspect", "wait-active", "stop", "delete"):
        instance_ops.add_parser(operation).add_argument("job_id")
    batch = operations.add_parser(
        "launch-batch", help="preverify immutable lowers before concurrent private writable consumers"
    )
    batch.add_argument("config", type=Path)
    assets = operations.add_parser("assets", help="inspect and retain permanent project-bound layer graphs")
    runtime = operations.add_parser(
        "prepare-runtime", help="verify assets and precreate empty mountpoints before read-only provider sealing"
    )
    runtime.add_argument("--assets-root", type=Path, required=True)
    runtime.add_argument("--project-id", required=True)
    runtime.add_argument("config", type=Path)
    assets.add_argument("--root", type=Path, required=True)
    assets.add_argument("--project-id", required=True)
    asset_ops = assets.add_subparsers(dest="asset_operation", required=True)
    asset_ops.add_parser("init")
    asset_ops.add_parser("ls")
    for operation in ("inspect", "verify"):
        asset_ops.add_parser(operation).add_argument("asset")
    register = asset_ops.add_parser("register-build")
    register.add_argument("--result", type=Path, required=True)
    register.add_argument("--name")
    bind = asset_ops.add_parser("bind-store")
    bind.add_argument("asset")
    bind.add_argument("--volume-id", required=True)
    bind.add_argument("--filesystem-uuid", required=True)
    retain = asset_ops.add_parser("retain")
    retain.add_argument("asset")
    retain.add_argument("--volume-id", required=True)
    retain.add_argument("consumers", nargs="+")
    asset_ops.add_parser("release").add_argument("consumer")
    worker = operations.add_parser(
        "registry-worker", help="run OCI acquisition and canonical decomposition inside this worker"
    )
    worker.add_argument("--cache-root", type=Path, required=True)
    worker.add_argument("--jobs-root", type=Path, required=True)
    worker.add_argument("--assets-root", type=Path, required=True)
    worker.add_argument("request", type=Path)


def dispatch(args) -> int:
    operation = args.cloud_operation
    try:
        if operation == "build-user-data":
            rendered = build_nova_layer_user_data(_json(args.config))
            if args.output is None:
                sys.stdout.write(rendered)
            else:
                fd = os.open(args.output.expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(rendered)
            return 0
        if operation == "registry-worker":
            from .registry_worker import run_job

            result = run_job(
                _json(args.request), cache_root=args.cache_root, jobs_root=args.jobs_root, assets_root=args.assets_root
            )
        elif operation == "build":
            result = run_nova_build(_json(args.config), name=args.name)
        elif operation == "prepare-runtime":
            from .nova_runtime import prepare_runtime

            result = prepare_runtime(AssetCatalog(args.assets_root, args.project_id), _json(args.config))
        elif operation == "launch-batch":
            result = launch_batch(_json(args.config))
        elif operation == "instance":
            config = _json(args.config)
            controller = _controller(config)
            action = args.instance_operation
            if action == "create":
                result = controller.create(**config["launch"], user_data=args.user_data.expanduser().read_text())
            elif action == "wait-active":
                result = controller.wait_active(args.job_id)
            else:
                result = getattr(controller, action)(args.job_id)
        else:
            catalog = AssetCatalog(args.root, args.project_id, initialize=args.asset_operation == "init")
            action = args.asset_operation
            if action == "register-build":
                result = register_build(catalog, _json(args.result), name=args.name)
            elif action == "bind-store":
                result = catalog.bind_store(args.asset, volume_id=args.volume_id, filesystem_uuid=args.filesystem_uuid)
            elif action == "retain":
                result = catalog.retain_many(args.asset, args.consumers, volume_id=args.volume_id)
            elif action == "release":
                result = catalog.release(args.consumer)
            else:
                result = catalog.inspect(
                    args.asset if action in {"inspect", "verify"} else None, verify=action == "verify"
                )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise PalimpsestError("cloud command failed: " + type(error).__name__) from None
