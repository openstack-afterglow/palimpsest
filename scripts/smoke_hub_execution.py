#!/usr/bin/env python3
"""Fixed live API acceptance, not a performance measurement or a provider shortcut.

Original human SDK credentials authenticate/observe. Every image and consumer
mutation uses public Hub HTTP. Two sequential consumers prove digest reuse.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from palimpsest_local.benchmark_scenario_cloud import ScenarioCloud  # noqa: E402
from palimpsest_local.benchmark_scenarios import (  # noqa: E402
    ResourceBudget,
    _consumer_observer,
    _delete_consumer,
    _digest,
    _hub,
    _wait,
    connection_from_stdin,
)
from palimpsest_local.benchmark_workflow import load_config  # noqa: E402
from palimpsest_local.errors import ArtifactValidationError  # noqa: E402
from palimpsest_local.state import atomic_write_json  # noqa: E402


def run(config, output, *, connection=None):
    import re
    from datetime import datetime

    if config.get("schema") != "palimpsest.hub-execution-smoke-config.v1" or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,31}", config.get("job_id", "")
    ):
        raise ArtifactValidationError("bounded fixed functional smoke configuration required")
    approval = config["approval"]
    if (
        approval["project_id"] != config["project_id"]
        or "hub-execution-smoke" not in approval["allowed_scenarios"]
        or datetime.fromisoformat(approval["expires_at"]).timestamp() <= time.time()
    ):
        raise ArtifactValidationError("functional smoke is outside its original unexpired project authority")
    if (
        not 2 <= config["resources"]["max_creates"] <= approval["max_creates"]
        or not 1 <= config["resources"]["max_live"] <= approval["max_live"]
        or not 2 * config["placement"]["boot_gib"] <= config["resources"]["max_boot_gib"] <= approval["max_boot_gib"]
    ):
        raise ArtifactValidationError("functional smoke envelope cannot contain exactly two sequential consumers")
    if not re.fullmatch(r"[a-z0-9./:@_-]+@sha256:[0-9a-f]{64}", config["smoke"]["image_reference"]):
        raise ArtifactValidationError("functional smoke requires a digest-pinned image")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    state = {
        "schema": "palimpsest.hub-execution-smoke.v1",
        "job_id": config["job_id"],
        "status": "running",
        "scope": "functional API/SDK proof, not a benchmark",
        "config_sha256": _digest(config),
        "steps": [],
        "consumer_creations": 0,
    }
    atomic_write_json(output / "config.json", config)
    started = time.monotonic()
    deadline = started + config.get("deadline_seconds", 2400)

    def event(kind, detail):
        row = {"elapsed_seconds": time.monotonic() - started, "event": kind, "detail": detail}
        with (output / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        state["phase"] = kind
        atomic_write_json(output / "result.json", state)

    try:
        budget = ResourceBudget(config, output)
        observer = ScenarioCloud(config, output, event, connection=connection, budget=budget, observation_only=True)
        client = _hub(config, observer.connection)
        source = Path(config["smoke"]["layer_file"])
        event("layer-upload", {"bytes": source.stat().st_size})
        receipt = client.push_blob(source, {"name": "api-smoke-" + config["job_id"], "arch": "x86_64"}, resume=False)
        digest = receipt["blob_digest"]
        before = client._json_request("GET", "/layers/" + digest)
        updated = client.update_layer(
            digest, {"name": "api-smoke-updated-" + config["job_id"]}, config_digest=before["config_digest"]
        )
        if updated["config_digest"] == before["config_digest"]:
            raise ArtifactValidationError("metadata update did not change its descriptor")
        try:
            client._open(
                "PATCH",
                "/layers/" + digest,
                body=json.dumps({"name": "stale-must-not-win"}).encode(),
                content_type="application/json",
                headers={"If-Match": before["config_digest"]},
            )
        except urllib.error.HTTPError as error:
            if error.code != 412:
                raise
        else:
            raise ArtifactValidationError("stale metadata update was not rejected")
        downloaded = output / "downloaded.sqsh"
        client.pull_blob(digest, downloaded, resume=False)
        if source.read_bytes() != downloaded.read_bytes():
            raise ArtifactValidationError("downloaded layer bytes differ")
        state["steps"].append(
            {"operation": "layer-crud", "digest": digest, "bytes_equal": True, "stale_update_http": 412}
        )
        request = {
            "request_id": str(uuid.uuid4()),
            "name": "smoke-registry-" + config["job_id"],
            "reference": config["smoke"]["image_reference"],
            "platform": "linux/amd64",
        }
        atomic_write_json(output / "registry-intent.json", request)
        event("registry-submission", {"request_id": request["request_id"]})
        accepted = client.import_registry_image(request)
        atomic_write_json(output / "registry-accepted.json", accepted)
        imported = _wait(
            lambda: _hub(config, observer.connection).get_image_job(accepted["id"]),
            {"complete"},
            deadline,
            event,
            "registry-status",
        )
        atomic_write_json(output / "registry-result.json", imported)
        state["steps"].append(
            {"operation": "registry-import", "job_id": imported["id"], "digests": imported["output_digests"]}
        )
        payload = io.BytesIO()
        message = b"public API native multistage COPY and safe symlink\n"
        with tarfile.open(fileobj=payload, mode="w") as archive:
            member = tarfile.TarInfo("message.txt")
            member.size = len(message)
            archive.addfile(member, io.BytesIO(message))
            link = tarfile.TarInfo("message-link.txt")
            link.type = tarfile.SYMTYPE
            link.linkname = "message.txt"
            archive.addfile(link)
        context_path = output / "context.tar"
        context_path.write_bytes(payload.getvalue())
        context = client.upload_build_context(context_path)
        command = [
            "/bin/sh",
            "-c",
            'test "$(id -u):$(id -g)" = 1000:2000 && cat /proof.txt && test ! -e /app/private.marker && echo isolated > /app/private.marker && test -s /app/private.marker && echo PRIVATE_DISK_UID_GID_OK',
        ]
        reference = config["smoke"]["image_reference"]
        definition = (
            "\n".join(
                [
                    "FROM " + reference + " AS source",
                    "COPY message-link.txt /proof.txt",
                    "FROM " + reference,
                    "COPY --from=source /proof.txt /proof.txt",
                    "RUN addgroup -g 2000 proofgroup && adduser -D -u 1000 -G proofgroup proofuser && chown 0:2000 /proof.txt && chmod 0640 /proof.txt && mkdir /app && chown 1000:2000 /app",
                    "USER 1000",
                    "WORKDIR /app",
                    "CMD " + json.dumps(command),
                ]
            )
            + "\n"
        )
        request = {
            "request_id": str(uuid.uuid4()),
            "name": "smoke-dockerfile-" + config["job_id"],
            "dockerfile": definition,
            "context_digest": context["digest"],
            "platform": "linux/amd64",
        }
        atomic_write_json(output / "dockerfile-intent.json", request)
        accepted = client.build_dockerfile(request)
        atomic_write_json(output / "dockerfile-accepted.json", accepted)
        built = _wait(
            lambda: _hub(config, observer.connection).get_image_job(accepted["id"]),
            {"complete"},
            deadline,
            event,
            "dockerfile-status",
        )
        atomic_write_json(output / "dockerfile-result.json", built)
        state["steps"].append(
            {"operation": "dockerfile-multistage-copy", "job_id": built["id"], "digests": built["output_digests"]}
        )
        previous = None
        for index in (1, 2):
            role = "consumer" + str(index)
            reservation = budget.reserve(role, config["placement"]["boot_gib"])
            spec = {
                k: v
                for k, v in config["placement"].items()
                if k in {"image_id", "flavor_id", "network_id", "security_group_ids", "boot_gib", "writable_gib"}
            }
            spec.update(request_id=str(uuid.uuid4()), name="pal-smoke-" + config["job_id"] + "-" + role, command=[])
            spec.update(source_instance_id=previous["id"]) if previous else spec.update(image_job_id=built["id"])
            atomic_write_json(output / (role + "-intent.json"), {"request": spec, "reservation_id": reservation})
            event("consumer-create", {"role": role, "request_id": spec["request_id"]})
            budget.check_allocation(deadline)
            row = _hub(config, observer.connection).create_instance(spec)
            atomic_write_json(output / (role + "-accepted.json"), row)
            state["consumer_creations"] += 1
            ready = _wait(
                _consumer_observer(config, output, role, spec, row, observer, budget, reservation, deadline),
                {"ready"},
                deadline,
                event,
                "consumer-status",
            )
            observed = observer.verify_external(
                ready["server_id"], expected_marker={"palimpsest_instance_id": ready["id"]}
            )
            if (
                ready["result"]["exit_code"] != 0
                or "PRIVATE_DISK_UID_GID_OK" not in ready["result"]["stdout"]
                or not ready.get("boot_volume_id")
            ):
                raise ArtifactValidationError("actual guest process/disk/identity result is not successful")
            if previous and (
                ready["layer_digests"] != previous["layer_digests"]
                or ready["result"]["stdout"] != previous["result"]["stdout"]
            ):
                raise ArtifactValidationError("reused digests or independent guest output differ")
            atomic_write_json(output / (role + "-ready.json"), {"api": ready, "sdk": observed})
            if index == 1:
                atomic_write_json(
                    output / (role + "-stop-intent.json"), {"instance_id": ready["id"], "server_id": ready["server_id"]}
                )
                event("consumer-stop", {"id": ready["id"]})
                _hub(config, observer.connection).stop_instance(ready["id"])
                _wait(
                    lambda identity=ready["id"]: _hub(config, observer.connection).get_instance(identity),
                    {"stopped"},
                    deadline,
                    event,
                    "consumer-stop-status",
                )
            event("consumer-delete", {"id": ready["id"], "server_id": ready["server_id"]})
            deleted = _delete_consumer(
                config,
                output,
                role,
                spec,
                row,
                ready,
                _hub(config, observer.connection),
                observer,
                budget,
                reservation,
                deadline,
                event,
                "consumer-delete-status",
            )
            absence = json.loads((output / (role + "-absence.json")).read_text())
            atomic_write_json(output / (role + "-deleted.json"), {"api": deleted, "sdk": absence})
            state["steps"].append(
                {
                    "operation": role,
                    "instance_id": ready["id"],
                    "server_id": ready["server_id"],
                    "digests": ready["layer_digests"],
                    "private_disk_write": True,
                    "deleted": True,
                }
            )
            previous = ready
        state["status"] = "success"
        event("completed", {"same_digest_reuse": True, "performance_claim": False})
    except (Exception, KeyboardInterrupt) as error:
        state.update(status="failed", phase="held-no-replay", error_class=type(error).__name__)
        event(
            "held-no-replay",
            {
                "error_class": type(error).__name__,
                "action": "inspect persisted original IDs; never rerun with new request IDs",
            },
        )
    atomic_write_json(output / "result.json", state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--auth-stdin",
        action="store_true",
        help="read private original-human v3token/v3password JSON from stdin; never persist it",
    )
    args = parser.parse_args()
    connection = None
    if args.auth_stdin:
        connection = connection_from_stdin()
    try:
        result = run(load_config(args.config), args.output, connection=connection)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "success" else 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
