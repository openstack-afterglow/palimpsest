"""Immediate original-caller OpenStack effects behind durable, non-replayed intents."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update

from palimpsest_hub.auth import package_target_project_id
from palimpsest_hub.config import InstanceGpuProfile, Settings
from palimpsest_hub.instance_models import PalimpsestHubInstance
from palimpsest_hub.models import PalimpsestWorkerLease, exact_identity
from palimpsest_hub.services.blob_references import retain_blob
from palimpsest_hub.services.hub_store import MEDIA_TYPE_LAYER_SQUASHFS
from palimpsest_hub.services.instance_bootstrap import render_bootstrap
from palimpsest_hub.services.instance_cluster_runtime import (
    cluster_snapshot,
    qualified_cluster,
    require_cluster_source,
    server_address,
)
from palimpsest_hub.services.worker_leases import run_blocking, sql_mutex

MARKER = "palimpsest_instance_id"


def value(resource, name, default=None):
    return resource.get(name, default) if isinstance(resource, dict) else getattr(resource, name, default)


def aware(stamp):
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def request_project(actor, conn):
    project = package_target_project_id(actor)
    if not project or actor.get("project_id") != project or getattr(conn, "_afterglow_project_id", None) != project:
        raise HTTPException(403, "Instance effects require the exact original caller project")
    if getattr(conn, "_afterglow_user_id", None) != actor.get("user_id"):
        raise HTTPException(403, "Instance effects require the exact original caller subject")
    return project


async def fresh_writer(actor, conn):
    from palimpsest_hub.api.hub import _legacy_writer
    from palimpsest_hub.auth import (
        get_package_member_info,
        revalidate_package_admin,
        validate_token,
    )

    if actor.get("is_package_system_admin"):
        current = await revalidate_package_admin(actor)
    else:
        original = await asyncio.to_thread(validate_token, actor["token"], actor["project_id"])
        if original.get("user_id") != actor.get("user_id") or original.get("project_id") != actor.get("project_id"):
            raise HTTPException(403, "Instance original subject or scope changed")
        current = await get_package_member_info(token_info=original)
    await _legacy_writer(current)
    if current["user_id"] != actor["user_id"] or package_target_project_id(current) != package_target_project_id(actor):
        raise HTTPException(403, "Instance authority changed")
    request_project(current, conn)
    return current


async def payload_authority(actor, conn, session=None, spec=None):
    from palimpsest_hub.api.hub import _legacy_download, _load_visible

    current = await fresh_writer(actor, conn)
    await _legacy_download(current)
    if session is not None:
        for digest in dict.fromkeys(
            [*spec["layer_digests"], *([spec["base_image_digest"]] if spec["base_image_digest"] else [])]
        ):
            await _load_visible(session, digest, current)
    return current


def projection(row):
    return {
        "id": row.id,
        "request_id": row.request_id,
        "name": row.name,
        "status": row.status,
        "server_id": row.server_id,
        "provider_status": row.provider_status,
        "provider_phase": provider_phase(row),
        "server_create_attempted": server_create_attempted(row),
        "layer_digests": row.spec_json["layer_digests"],
        "source_instance_id": row.source_instance_id,
        "image_job_id": row.spec_json.get("image_job_id"),
        "boot_volume_id": row.boot_volume_id,
        "mode": row.spec_json["mode"],
        "gpu_profile": row.spec_json.get("gpu_profile"),
        "gpu": row.spec_json.get("gpu"),
        "cluster": (
            {key: item for key, item in row.spec_json["cluster"].items() if key != "qualification"}
            if row.spec_json.get("cluster")
            else None
        ),
        "provenance": row.spec_json.get("provenance"),
        "writable_gib": row.spec_json["writable_gib"],
        "writable_storage": "guest-private boot-disk ext4 loop filesystem",
        "result": row.result_json,
        "error_code": row.error_code,
        "readiness_semantics": "point-in-time execution/probe proof, not ongoing health",
        "result_recorded_at": aware(row.result_recorded_at).isoformat() if row.result_recorded_at else None,
        "created_at": aware(row.created_at).isoformat(),
        "updated_at": aware(row.updated_at).isoformat(),
    }


async def owned(session, instance_id, project):
    row = await session.get(PalimpsestHubInstance, instance_id)
    if row is None or row.project_id != project:
        raise HTTPException(404, "Instance not found")
    return row


def qualified_gpu(spec, project, settings):
    identity = spec.get("gpu_profile")
    if identity is None:
        return None
    configured = settings.palimpsest_hub_instance_gpu_profiles.get(identity)
    if configured is None:
        raise HTTPException(422, "GPU profile is not operator-qualified")
    try:
        profile = InstanceGpuProfile.model_validate(
            configured.model_dump() if isinstance(configured, InstanceGpuProfile) else configured
        )
    except ValueError:
        raise HTTPException(422, "GPU profile qualification is invalid") from None
    if project not in profile.project_ids:
        raise HTTPException(403, "GPU profile is not qualified for the original caller project")
    if spec["mode"] != "oci-chroot" or spec["image_id"] != profile.image_id or spec["flavor_id"] != profile.flavor_id:
        raise HTTPException(422, "GPU profile requires its exact OCI guest image and flavor")
    qualification = settings.palimpsest_hub_instance_base_images.get(profile.image_id)
    if qualification is None:
        raise HTTPException(422, "GPU guest image requires exact operator-qualified content")
    try:
        Settings.qualified_base_images({profile.image_id: qualification})
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "GPU guest content qualification is invalid") from None
    return {**profile.model_dump(exclude={"project_ids"}), "project_id": project, "base_image": dict(qualification)}


def provider_inputs(conn, spec, project, max_boot, base_images=None, gpu=None):
    """No allocation until every explicitly selected provider object is checked."""
    image = conn.image.get_image(spec["image_id"])
    if image is None or str(value(image, "status", "")).lower() != "active":
        raise HTTPException(422, "An active Glance base image is required")
    props = {**(value(image, "extra_properties", {}) or {}), **(value(image, "properties", {}) or {})}
    if value(image, "palimpsest_artifact_type", props.get("palimpsest_artifact_type")) == "external-layer-store":
        raise HTTPException(422, "A data-only external layer store is not a boot image")
    qualification = (base_images or {}).get(spec["image_id"])
    if qualification is not None:
        algorithm = value(image, "hash_algo") or value(image, "os_hash_algo")
        content = value(image, "hash_value") or value(image, "os_hash_value")
        if (algorithm, content) != (qualification["os_hash_algo"], qualification["os_hash_value"]):
            raise HTTPException(422, "Qualified base image content changed")
    if gpu is not None and (qualification is None or qualification != gpu["base_image"]):
        raise HTTPException(422, "GPU guest content qualification changed")
    arch = value(image, "architecture") or props.get("architecture")
    if arch is None and qualification:
        arch = qualification["architecture"]
    if arch not in ("x86_64", "amd64"):
        raise HTTPException(422, "Base image needs declared architecture or exact operator-qualified content")
    os_type = value(image, "os_type") or props.get("os_type")
    if os_type is None and qualification:
        os_type = qualification["os_type"]
    if os_type not in (None, "linux"):
        raise HTTPException(422, "Base image must be Linux")
    if value(image, "disk_format") not in ("qcow2", "raw") or value(image, "container_format", "bare") != "bare":
        raise HTTPException(422, "Base image must be a bootable raw or qcow2 cloud image")
    if spec["boot_gib"] > max_boot or spec["boot_gib"] < int(value(image, "min_disk", 0) or 0):
        raise HTTPException(422, "Boot volume size is outside configured or image limits")
    if int(value(image, "size", 0) or 0) > spec["boot_gib"] * 1024**3:
        raise HTTPException(422, "Boot volume is smaller than the base image")
    if spec["writable_gib"] >= spec["boot_gib"]:
        raise HTTPException(422, "Writable filesystem capacity must be smaller than boot volume")
    if spec["mode"] == "application":
        base = props.get("palimpsest_digest") or value(image, "palimpsest_digest")
        if base != spec["base_image_digest"]:
            raise HTTPException(422, "Application layer base digest does not match Glance image provenance")
    flavor = conn.compute.get_flavor(spec["flavor_id"])
    if flavor is None or int(value(flavor, "ram", 0) or 0) < int(value(image, "min_ram", 0) or 0):
        raise HTTPException(422, "Flavor is unavailable or smaller than image minimum RAM")
    if value(flavor, "is_disabled") is True:
        raise HTTPException(422, "Flavor is disabled")
    public = value(flavor, "is_public")
    # Nova's public filter includes project grants; SDK omission means ALL.
    # os-flavor-access enumerates other tenants and requires admin policy.
    if public is not True and (
        public is not False
        or not any(
            value(item, "id") == spec["flavor_id"] for item in conn.compute.flavors(details=False, is_public=True)
        )
    ):
        raise HTTPException(403, "Flavor must be public or visible in the original caller's available flavors")
    fetched = conn.compute.fetch_flavor_extra_specs(flavor)
    extra = value(fetched, "extra_specs", {}) or {}
    if gpu is not None:
        if extra.get("pci_passthrough:alias") != gpu["pci_alias"]:
            raise HTTPException(422, "GPU flavor must declare the exact qualified PCI aliases")
    elif extra.get("pci_passthrough:alias") or any(
        re.fullmatch(r"resources[A-Za-z0-9_-]*:.+", key) and "GPU" in key.upper() for key in extra
    ):
        raise HTTPException(422, "PCI/GPU flavors require an explicit operator-qualified GPU profile")
    network = conn.network.get_network(spec["network_id"])
    if network is None or value(network, "project_id") != project:
        raise HTTPException(403, "Network must belong to the exact caller project")
    groups = []
    for group_id in spec["security_group_ids"]:
        group = conn.network.get_security_group(group_id)
        if group is None or value(group, "project_id") != project:
            raise HTTPException(403, "Security groups must belong to the exact caller project")
        groups.append({"name": group_id})
    return groups


def validate_runtime(runtime):
    if not isinstance(runtime, dict) or set(runtime) != {"entrypoint", "cmd", "env", "working_dir", "user"}:
        raise HTTPException(422, "Image runtime provenance is incomplete")
    for key in ("entrypoint", "cmd", "env"):
        items = runtime[key]
        if (
            not isinstance(items, list)
            or len(items) > 128
            or any(not isinstance(item, str) or "\x00" in item or len(item.encode()) > 4096 for item in items)
        ):
            raise HTTPException(422, "Image runtime arguments exceed instance limits")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", entry, re.S) for entry in runtime["env"]):
        raise HTTPException(422, "Invalid image environment")
    directory = runtime["working_dir"] or "/"
    if not isinstance(directory, str) or not directory.startswith("/") or "\x00" in directory or len(directory) > 4096:
        raise HTTPException(422, "Invalid image working directory")
    user = runtime["user"] or "0"
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", user):
        raise HTTPException(422, "Invalid image runtime user")
    return runtime


async def snapshot(session, req, actor, store):
    # Import lazily: API owns visibility and CAS locks, not a second visibility policy.
    from palimpsest_hub.api.hub import _load_visible

    source_id = req.get("source_instance_id")
    source = await owned(session, source_id, package_target_project_id(actor)) if source_id else None
    digests = source.spec_json["layer_digests"] if source else req["layer_digests"]
    if not digests:
        raise HTTPException(422, "An explicit complete layer stack is required")
    job = None
    if source is None:
        from palimpsest_hub.image_models import PalimpsestHubImageJob

        if req.get("image_job_id"):
            job = await session.get(PalimpsestHubImageJob, req["image_job_id"])
            if job is None or job.project_id != package_target_project_id(actor):
                raise HTTPException(404, "Image job not found")
            if job.status != "complete" or job.output_digests != digests:
                raise HTTPException(422, "Image job must be complete and match the exact ordered layer stack")
        else:
            from palimpsest_hub.models import RuntimeBlobReference

            candidates = select(RuntimeBlobReference.resource_id).where(
                RuntimeBlobReference.resource_kind == "image-job", RuntimeBlobReference.digest == digests[0]
            )
            jobs = (
                await session.scalars(
                    select(PalimpsestHubImageJob).where(
                        PalimpsestHubImageJob.id.in_(candidates),
                        exact_identity(PalimpsestHubImageJob.project_id, package_target_project_id(actor)),
                        PalimpsestHubImageJob.status == "complete",
                    )
                )
            ).all()
            matches = [item for item in jobs if item.output_digests == digests]
            if matches:
                job = matches[0]
                if any(item.result_json != job.result_json for item in matches[1:]):
                    raise HTTPException(
                        422, "Select image_job_id to disambiguate identical layer bytes with different runtime metadata"
                    )
    previous = None
    layers = []
    base_digest = None
    modes = set()
    for digest in digests:
        layer = await _load_visible(session, digest, actor)
        config = layer.config_json or {}
        if layer.kind != "squashfs" or layer.media_type != MEDIA_TYPE_LAYER_SQUASHFS or layer.arch != "x86_64":
            raise HTTPException(422, "Instance inputs must be x86_64 SquashFS runtime layers")
        if layer.size_bytes <= 0 or not store.exists(digest):
            raise HTTPException(422, "Instance layer stack is incomplete or unavailable")
        if (job is not None or (source is not None and source.spec_json["mode"] == "oci-chroot")) and config.get(
            "base_image_digest"
        ) is None:
            if layer.parent_digest is not None:
                raise HTTPException(422, "OCI physical layers must use contextual image-job graph provenance")
            modes.add("oci-chroot")
        elif config.get("base_image_digest"):
            if layer.parent_digest != previous:
                raise HTTPException(422, "Application layer parent chain is incomplete")
            modes.add("application")
            if base_digest and base_digest != config["base_image_digest"]:
                raise HTTPException(422, "Application layer base provenance differs")
            base_digest = config["base_image_digest"]
        elif previous and modes == {"application"}:
            if layer.parent_digest != previous:
                raise HTTPException(422, "Application layer parent chain is incomplete")
        else:
            raise HTTPException(422, "Layer runtime mode/provenance is not supported")
        previous = digest
        layers.append({"digest": digest, "size_bytes": layer.size_bytes})
    if len(modes) != 1:
        raise HTTPException(422, "Layer stack mixes incompatible runtime modes")
    mode = next(iter(modes))
    if mode == "application":
        base = await _load_visible(session, base_digest, actor)
        if base.kind != "cloud-image" or base.arch != "x86_64":
            raise HTTPException(422, "Application layer cloud base provenance is unavailable or incompatible")
    if source:
        if source.spec_json["mode"] != mode or source.spec_json.get("base_image_digest") != base_digest:
            raise HTTPException(409, "Retained instance layer provenance changed")
        runtime = validate_runtime(source.spec_json["runtime"])
        provenance = source.spec_json.get("provenance")
    elif mode == "oci-chroot":
        # Exact completed project job, not a user-written descriptor flag, is authority.
        if job is None:
            raise HTTPException(422, "Exact completed project image-job provenance is required")
        runtime = validate_runtime(job.result_json["runtime"])
        provenance = job.result_json["source"]
    else:
        runtime = {"entrypoint": [], "cmd": [], "env": [], "working_dir": "/", "user": "0"}
        provenance = {"base_image_digest": base_digest}
    command = (
        ["/bin/k3s"] if req.get("cluster_profile") else req["command"] or [*runtime["entrypoint"], *runtime["cmd"]]
    )
    if not command:
        raise HTTPException(422, "An executable command or image default command is required")
    if len(command) > 128 or sum(len(arg.encode()) for arg in command) > 16384 or not command[0]:
        raise HTTPException(422, "Resolved runtime command exceeds instance argument limits")
    return {
        **req,
        "layer_digests": list(digests),
        "layers": layers,
        "mode": mode,
        "runtime": runtime,
        "provenance": provenance,
        "base_image_digest": base_digest,
        "command": command,
    }


async def create(factory, store, settings, actor, conn, req):
    from palimpsest_hub.api.hub import _legacy_download, _load_visible, _locked_blobs

    project = request_project(actor, conn)
    await _legacy_download(actor)
    if req["writable_gib"] > settings.palimpsest_hub_instance_max_writable_gib:
        raise HTTPException(422, "Writable filesystem capacity exceeds configured limit")
    # Adding an opt-in selector must not invalidate pre-upgrade CPU idempotency.
    digest_req = {
        key: item
        for key, item in req.items()
        if key not in ("gpu_profile", "cluster_profile", "cluster_server_id") or item is not None
    }
    digest = (
        "sha256:" + hashlib.sha256(json.dumps(digest_req, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    # Discover only immutable candidate identities without a writer mutex. Every
    # publication/admission takes lexical CAS locks BEFORE a SQL write fence.
    async with factory() as session:
        old = await session.scalar(
            select(PalimpsestHubInstance).where(
                exact_identity(PalimpsestHubInstance.project_id, project),
                PalimpsestHubInstance.request_id == req["request_id"],
            )
        )
        if old is not None:
            if old.request_digest != digest:
                raise HTTPException(409, "request_id is already bound to a different immutable request")
            return projection(old)
        source = await owned(session, req["source_instance_id"], project) if req.get("source_instance_id") else None
        digests = source.spec_json["layer_digests"] if source else req["layer_digests"]
        effective_req = req
        if not digests and req.get("image_job_id"):
            from palimpsest_hub.image_models import PalimpsestHubImageJob

            selected_job = await session.get(PalimpsestHubImageJob, req["image_job_id"])
            if selected_job is None or selected_job.project_id != project:
                raise HTTPException(404, "Image job not found")
            if selected_job.status != "complete":
                raise HTTPException(422, "Image job must be complete")
            digests = selected_job.output_digests
            effective_req = {**req, "layer_digests": digests}
        lock_digests = list(digests)
        for layer_digest in set(digests):
            layer = await _load_visible(session, layer_digest, actor)
            base = (layer.config_json or {}).get("base_image_digest")
            if base:
                lock_digests.append(base)
    async with (
        _locked_blobs(store, lock_digests),
        sql_mutex(factory, "build-api:instance-project:" + project) as session,
    ):
        old = await session.scalar(
            select(PalimpsestHubInstance).where(
                exact_identity(PalimpsestHubInstance.project_id, project),
                PalimpsestHubInstance.request_id == req["request_id"],
            )
        )
        if old is not None:
            if old.request_digest != digest:
                raise HTTPException(409, "request_id is already bound to a different immutable request")
            return projection(old)
        active = await session.scalar(
            select(func.count())
            .select_from(PalimpsestHubInstance)
            .where(exact_identity(PalimpsestHubInstance.project_id, project), PalimpsestHubInstance.status != "deleted")
        )
        if active >= settings.palimpsest_hub_max_instances_per_project:
            raise HTTPException(429, "Project instance limit reached")
        spec = await snapshot(session, effective_req, actor, store)
        spec["gpu"] = qualified_gpu(spec, project, settings)
        spec["cluster"] = await cluster_snapshot(session, spec, actor, conn, settings)
        if not set(spec["layer_digests"]) <= set(lock_digests) or (
            spec["base_image_digest"] and spec["base_image_digest"] not in lock_digests
        ):
            raise HTTPException(409, "Layer provenance changed during admission; retry the request")
        if sum(layer["size_bytes"] for layer in spec["layers"]) > settings.palimpsest_hub_max_blob_bytes:
            raise HTTPException(422, "Layer stack exceeds configured byte limit")
        try:
            await run_blocking(
                provider_inputs,
                conn,
                spec,
                project,
                settings.palimpsest_hub_instance_max_boot_gib,
                settings.palimpsest_hub_instance_base_images,
                spec["gpu"],
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "Provider input validation unavailable") from None
        spec["_boot_prepare"] = {
            "version": 1,
            "phase": "volume_not_attempted",
            "volume_create_attempted": False,
            "server_create_attempted": False,
        }
        await payload_authority(actor, conn, session, spec)
        now = datetime.now(UTC)
        row = PalimpsestHubInstance(
            id=str(uuid.uuid4()),
            project_id=project,
            created_by=actor["user_id"],
            request_id=req["request_id"],
            request_digest=digest,
            name=req["name"],
            spec_json=spec,
            source_instance_id=req.get("source_instance_id"),
            status="uncertain",
            error_code="create_pending",
            capability_hash=None,
            capability_expires_at=now + timedelta(seconds=settings.palimpsest_hub_instance_bootstrap_ttl_seconds),
            created_at=now,
            updated_at=now,
        )
        if spec["cluster"] is not None:
            spec["cluster"]["node_name"] = "pal-" + uuid.UUID(row.id).hex
        session.add(row)
        for bound_digest in dict.fromkeys(
            [*spec["layer_digests"], *([spec["base_image_digest"]] if spec["base_image_digest"] else [])]
        ):
            await retain_blob(session, bound_digest, "instance", row.id)
        await session.commit()  # Durable intent precedes the first allocation; never replay it.
        instance_id = row.id
    async with sql_mutex(factory, "build-api:instance:" + instance_id) as session:
        row = await owned(session, instance_id, project)
        await advance_prepared_boot(session, row, actor, conn, settings, allocate=True)
        return projection(row)


def preparation(row):
    state = row.spec_json.get("_boot_prepare")
    phases = {
        "volume_not_attempted",
        "volume_create_uncertain",
        "volume_waiting",
        "nova_not_attempted",
        "nova_create_uncertain",
        "nova_observed",
    }
    return (
        state
        if isinstance(state, dict)
        and type(state.get("version")) is int
        and state["version"] == 1
        and state.get("phase") in phases
        else None
    )


def server_create_attempted(row):
    state = preparation(row)
    if state is None or type(state.get("server_create_attempted")) is not bool:
        return None
    if row.server_id is not None:
        return True
    if state["phase"] in ("nova_create_uncertain", "nova_observed"):
        return True
    return state["server_create_attempted"]


def provider_phase(row):
    state = preparation(row)
    if state is None:
        return "legacy"
    if row.status == "deleted":
        return "deleted"
    if row.status == "deleting":
        if state.get("delete_phase") == "uncertain":
            return "uncertain"
        return "delete_volume_ready" if state.get("delete_ready") else "delete_volume_pending"
    if state.get("terminal"):
        return "uncertain"
    return {
        "volume_not_attempted": "uncertain",
        "volume_create_uncertain": "uncertain",
        "volume_waiting": "volume_preparing",
        "nova_not_attempted": "volume_ready",
        "nova_create_uncertain": "server_attempted",
        "nova_observed": "server_attempted",
    }[state["phase"]]


def preparation_update(row, **changes):
    # JSON columns do not track nested mutations; always replace the whole value.
    row.spec_json = {**row.spec_json, "_boot_prepare": {**preparation(row), **changes}}
    row.updated_at = datetime.now(UTC)


async def preparation_refence(session, row):
    # A durable intent commit releases sql_mutex's transaction lock. Retake the
    # SAME mutex and refresh before continuing, never trust the pre-commit row.
    await session.execute(
        update(PalimpsestWorkerLease)
        .where(PalimpsestWorkerLease.name == "build-api:instance:" + row.id)
        .values(token=uuid.uuid4().hex, expires_at=datetime.now(UTC))
    )
    await session.refresh(row)


def can_continue_preparation(row, phase):
    state = preparation(row)
    return (
        state is not None
        and state["phase"] == phase
        and not state.get("terminal")
        and row.status not in ("deleting", "deleted")
    )


def volume_markers(row):
    return {
        MARKER: row.id,
        "palimpsest_request_digest": row.request_digest,
        "palimpsest_volume_role": "boot",
        "palimpsest_image_id": row.spec_json["image_id"],
    }


def checked_prepared_volume(row, volume, *, image_required=False):
    metadata = value(volume, "metadata", {}) or {}
    image = (value(volume, "volume_image_metadata", {}) or {}).get("image_id")
    if (
        value(volume, "project_id", value(volume, "os-vol-tenant-attr:tenant_id")) != row.project_id
        or any(metadata.get(key) != item for key, item in volume_markers(row).items())
        or value(volume, "size") != row.spec_json["boot_gib"]
        or (image is not None and image != row.spec_json["image_id"])
        or (image_required and image != row.spec_json["image_id"])
    ):
        raise HTTPException(409, "Prepared boot volume ownership or image intent does not match")
    uuid.UUID(str(value(volume, "id", "")))
    return volume


def get_prepared_volume(conn, row):
    if row.boot_volume_id:
        try:
            volume = conn.block_storage.get_volume(row.boot_volume_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        if volume is None:
            raise HTTPException(502, "Provider did not prove prepared volume presence or absence")
        if str(value(volume, "id", "")) != row.boot_volume_id:
            raise HTTPException(409, "Provider returned a different prepared boot volume identity")
        return checked_prepared_volume(row, volume)
    # Exhaust the project inventory before adopting anything. A truncated or
    # ambiguous read is not absence and cannot authorize another POST.
    matches = []
    for index, volume in enumerate(conn.block_storage.volumes(details=True, project_id=row.project_id)):
        if index >= 1000:
            raise HTTPException(409, "Prepared volume recovery inventory exceeds observation bound")
        metadata = value(volume, "metadata", {}) or {}
        if value(volume, "project_id", value(volume, "os-vol-tenant-attr:tenant_id")) == row.project_id and all(
            metadata.get(key) == item for key, item in volume_markers(row).items()
        ):
            if value(volume, "attachments", []):
                raise HTTPException(409, "Cannot recover an already attached prepared boot volume")
            matches.append(checked_prepared_volume(row, volume))
    if len(matches) > 1:
        raise HTTPException(409, "Prepared boot volume intent marker is ambiguous")
    return matches[0] if matches else None


def fail_preparation(row, code, *, no_call=None):
    row.status, row.error_code, row.capability_hash = "error", code, None
    preparation_update(row, terminal=True)
    if no_call == "volume":
        preparation_update(row, phase="volume_not_attempted", volume_create_attempted=False)
    elif no_call == "server":
        preparation_update(row, phase="nova_not_attempted", server_create_attempted=False)


async def allocation_authority(session, row, actor, conn, settings, *, no_call=None):
    """Revalidate after slow provider reads, without renewing the accepted expiry."""
    spec = row.spec_json
    try:
        gpu = qualified_gpu(spec, row.project_id, settings)
        if gpu != spec["gpu"] or qualified_cluster(spec, row.project_id, settings) != (spec.get("cluster") or {}).get(
            "qualification"
        ):
            raise HTTPException(422, "Runtime qualification changed before allocation")
        await run_blocking(
            provider_inputs,
            conn,
            spec,
            row.project_id,
            settings.palimpsest_hub_instance_max_boot_gib,
            settings.palimpsest_hub_instance_base_images,
            gpu,
        )
        if spec.get("cluster") and spec["cluster"]["role"] == "agent":
            source = await owned(session, spec["cluster"]["server_instance_id"], row.project_id)
            require_cluster_source(source, spec, actor, settings, spec["cluster"]["qualification"])
            address = await run_blocking(server_address, conn, source, spec["network_id"])
            if address != spec["cluster"]["server_address"]:
                raise HTTPException(409, "Cluster source network identity changed")
            await session.refresh(source, with_for_update=True)
            require_cluster_source(source, spec, actor, settings, spec["cluster"]["qualification"])
    except Exception as exc:
        fail_preparation(row, "create_inputs_changed", no_call=no_call)
        await session.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, "Provider input validation unavailable") from None
    try:
        await payload_authority(actor, conn, session, spec)
    except HTTPException:
        fail_preparation(row, "create_authority_revoked", no_call=no_call)
        await session.commit()
        raise
    if aware(row.capability_expires_at) <= datetime.now(UTC):
        fail_preparation(row, "create_bootstrap_expired", no_call=no_call)
        await session.commit()
        return False
    return True


async def advance_prepared_boot(session, row, actor, conn, settings, *, allocate=False, launch=False):
    state = preparation(row)
    if not state or row.status in ("deleting", "deleted"):
        return
    if state.get("terminal"):
        # Expiry/revocation ends allocation authority, not ownership recovery.
        # Keep the terminal cause while binding one exact previously-called root.
        if state["phase"] != "volume_not_attempted":
            try:
                volume = await run_blocking(get_prepared_volume, conn, row)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Provider prepared volume observation unavailable") from None
            if volume is not None:
                row.boot_volume_id = str(value(volume, "id"))
                row.updated_at = datetime.now(UTC)
                await session.commit()
        return
    phase = state["phase"]
    if phase in ("nova_create_uncertain", "nova_observed"):
        return  # The Nova POST may already have happened. Observation only.
    if aware(row.capability_expires_at) <= datetime.now(UTC):
        fail_preparation(row, "create_bootstrap_expired")
        await session.commit()
        return
    # An inventory reader can observe settlement, never spend accepted authority.
    may_allocate = (
        allocate
        and settings is not None
        and actor is not None
        and actor.get("user_id") == row.created_by
        and "packages:write" in actor.get("package_actions", ())
    )
    if phase == "volume_not_attempted":
        if not may_allocate or not await allocation_authority(session, row, actor, conn, settings):
            return
        preparation_update(row, phase="volume_create_uncertain", volume_create_attempted=True)
        row.status, row.error_code = "uncertain", "provider_volume_create_uncertain"
        await session.commit()  # Crash after this commit MUST NOT replay Cinder POST.
        await preparation_refence(session, row)
        if not can_continue_preparation(row, "volume_create_uncertain"):
            if preparation(row)["phase"] == "volume_create_uncertain":
                preparation_update(row, phase="volume_not_attempted", volume_create_attempted=False, terminal=True)
                if row.status == "deleting":
                    row.status, row.error_code = "deleted", None
                    preparation_update(row, delete_phase="absent", delete_ready=False)
                await session.commit()
            return
        if not await allocation_authority(session, row, actor, conn, settings, no_call="volume"):
            return
        try:
            volume = await run_blocking(
                conn.block_storage.create_volume,
                name=row.name + "-boot",
                size=row.spec_json["boot_gib"],
                image_id=row.spec_json["image_id"],
                metadata=volume_markers(row),
            )
            checked_prepared_volume(row, volume)
        except Exception:
            # Provider uncertainty is already durable. Never retry POST or a
            # possibly-failed SQL COMMIT in a provider exception handler.
            return
        row.boot_volume_id = str(value(volume, "id"))
        preparation_update(row, phase="volume_waiting")
        row.status, row.error_code = "booting", "create_boot_volume_pending"
        await session.commit()
        await preparation_refence(session, row)
        if not can_continue_preparation(row, "volume_waiting"):
            return
    try:
        volume = await run_blocking(get_prepared_volume, conn, row)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Provider prepared volume observation unavailable") from None
    if volume is None:
        if row.boot_volume_id:
            fail_preparation(row, "provider_boot_volume_missing")
            await session.commit()
        return
    row.boot_volume_id = str(value(volume, "id"))
    preparation_update(row, phase="volume_waiting")
    row.status, row.error_code = "booting", "create_boot_volume_pending"
    status = str(value(volume, "status", "UNKNOWN")).lower()
    if status in ("creating", "downloading"):
        if may_allocate:
            try:
                await payload_authority(actor, conn, session, row.spec_json)
            except HTTPException:
                fail_preparation(row, "create_authority_revoked")
                await session.commit()
                raise
        if aware(row.capability_expires_at) <= datetime.now(UTC):
            fail_preparation(row, "create_bootstrap_expired")
        await session.commit()
        return
    if status != "available" or value(volume, "attachments", []):
        fail_preparation(row, "provider_boot_volume_error")
        await session.commit()
        return
    checked_prepared_volume(row, volume, image_required=True)
    preparation_update(row, phase="nova_not_attempted")
    await session.commit()
    await preparation_refence(session, row)
    if not can_continue_preparation(row, "nova_not_attempted"):
        return
    if not launch or not may_allocate or not await allocation_authority(session, row, actor, conn, settings):
        return
    # Final volume observation precedes the final original-human fence.
    volume = await run_blocking(get_prepared_volume, conn, row)
    if volume is None or str(value(volume, "status", "")).lower() != "available" or value(volume, "attachments", []):
        fail_preparation(row, "provider_boot_volume_error")
        await session.commit()
        return
    checked_prepared_volume(row, volume, image_required=True)
    try:
        await payload_authority(actor, conn, session, row.spec_json)
    except HTTPException:
        fail_preparation(row, "create_authority_revoked")
        await session.commit()
        raise
    if aware(row.capability_expires_at) <= datetime.now(UTC):
        fail_preparation(row, "create_bootstrap_expired")
        await session.commit()
        return
    spec = row.spec_json
    token = secrets.token_urlsafe(32)
    guest = {
        "instance_id": row.id,
        "origin": settings.palimpsest_hub_package_public_origin.rstrip("/"),
        "capability": token,
        "expires_at": int(aware(row.capability_expires_at).timestamp()),
        "layers": spec["layers"],
        "mode": spec["mode"],
        "runtime": spec["runtime"],
        **({"gpu": spec["gpu"]} if spec["gpu"] is not None else {}),
        **(
            {"cluster": {key: item for key, item in spec["cluster"].items() if key != "qualification"}}
            if spec.get("cluster") is not None
            else {}
        ),
        "writable_bytes": spec["writable_gib"] * 1024**3,
        "command": spec["command"],
        "readiness_argv": spec["readiness_argv"],
        "ca_certificates": list(settings.palimpsest_hub_instance_ca_certificates),
    }
    user_data = base64.b64encode(render_bootstrap(guest).encode()).decode()
    if len(user_data) > 65535:
        fail_preparation(row, "create_bootstrap_input_limit")
        await session.commit()
        return
    row.capability_hash = hashlib.sha256(token.encode()).hexdigest()
    preparation_update(row, phase="nova_create_uncertain", server_create_attempted=True)
    row.status, row.error_code = "uncertain", "provider_create_uncertain"
    await session.commit()  # Only this request holds raw token; never replay Nova POST.
    await preparation_refence(session, row)
    if (
        not can_continue_preparation(row, "nova_create_uncertain")
        or row.capability_hash != hashlib.sha256(token.encode()).hexdigest()
    ):
        if preparation(row)["phase"] == "nova_create_uncertain":
            preparation_update(row, phase="nova_not_attempted", server_create_attempted=False, terminal=True)
            row.capability_hash = None
            await session.commit()
        return
    # Reacquire cluster-source authority/lock, settings, human and payload after
    # the commit and potentially slow mutex wait; retain these locks through POST.
    if not await allocation_authority(session, row, actor, conn, settings, no_call="server"):
        return
    try:
        server = await run_blocking(
            conn.compute.create_server,
            name=spec["name"],
            flavor_id=spec["flavor_id"],
            networks=[{"uuid": spec["network_id"]}],
            security_groups=[{"name": gid} for gid in spec["security_group_ids"]],
            block_device_mapping_v2=[
                {
                    "boot_index": "0",
                    "uuid": row.boot_volume_id,
                    "source_type": "volume",
                    "destination_type": "volume",
                    "delete_on_termination": True,
                }
            ],
            metadata={MARKER: row.id, "palimpsest_request_digest": row.request_digest},
            user_data=user_data,
        )
        server_id = str(value(server, "id", ""))
        uuid.UUID(server_id)
        row.server_id = server_id
        preparation_update(row, phase="nova_observed")
        row.status, row.error_code = "booting", None
    except Exception:
        row.status, row.error_code = "uncertain", "provider_create_uncertain"
    row.updated_at = datetime.now(UTC)
    await session.commit()


def exact_server(conn, row):
    """Inspect ownership AND exact intent marker before any lifecycle effect."""
    state = preparation(row)
    if state and state["phase"] not in ("nova_create_uncertain", "nova_observed"):
        return None
    server = conn.compute.get_server(row.server_id) if row.server_id else None
    if server is None and row.server_id is None:
        matches = []
        for index, candidate in enumerate(conn.compute.servers(details=True)):
            if index >= 1000:
                raise HTTPException(409, "Provider instance recovery inventory exceeds observation bound")
            if (
                value(candidate, "project_id") == row.project_id
                and (value(candidate, "metadata", {}) or {}).get(MARKER) == row.id
                and (value(candidate, "metadata", {}) or {}).get("palimpsest_request_digest") == row.request_digest
            ):
                matches.append(candidate)
        if len(matches) > 1:
            raise HTTPException(409, "Provider intent marker is ambiguous")
        server = matches[0] if matches else None
    if server is not None and (
        row.server_id is not None
        and str(value(server, "id", "")) != row.server_id
        or value(server, "project_id") != row.project_id
        or (value(server, "metadata", {}) or {}).get(MARKER) != row.id
        or (value(server, "metadata", {}) or {}).get("palimpsest_request_digest") != row.request_digest
    ):
        raise HTTPException(409, "Provider instance ownership or intent marker does not match")
    return server


def inspect_boot_volume(conn, row, server, *, deleting=False):
    """Bind the one original image boot volume; never auto-delete a later attachment."""
    attachments = list(conn.compute.volume_attachments(value(server, "id"), microversion="2.79"))
    if deleting and not attachments and preparation(row) and row.boot_volume_id:
        volume = get_prepared_volume(conn, row)
        if volume is not None and not value(volume, "attachments", []):
            return row.boot_volume_id
    root_device = value(server, "root_device_name")
    boot = []
    for attachment in attachments:
        volume_id = str(value(attachment, "volume_id", value(attachment, "id", "")))
        dot = value(attachment, "delete_on_termination")
        if type(dot) is not bool:
            raise HTTPException(409, "Provider must expose volume delete-on-termination ownership")
        is_boot = (
            row.boot_volume_id == volume_id
            if row.boot_volume_id
            else value(attachment, "boot_index") in (0, "0")
            or (root_device is not None and value(attachment, "device") == root_device)
            or (len(attachments) == 1 and dot)
        )
        if is_boot:
            volume = conn.block_storage.get_volume(volume_id)
            if preparation(row):
                checked_prepared_volume(row, volume, image_required=True)
            owner = value(volume, "project_id", value(volume, "os-vol-tenant-attr:tenant_id"))
            attached = value(volume, "attachments", []) or []
            image_meta = value(volume, "volume_image_metadata", {}) or {}
            if image_meta.get("image_id") != row.spec_json["image_id"]:
                raise HTTPException(409, "Boot volume image provenance does not match original create intent")
            if (
                volume is None
                or owner != row.project_id
                or not dot
                or not any(value(item, "server_id") == value(server, "id") for item in attached)
                or value(volume, "bootable", value(volume, "is_bootable")) not in (True, "true")
            ):
                raise HTTPException(409, "Original boot volume ownership or attachment is not verified")
            boot.append(volume_id)
        elif deleting and dot:
            raise HTTPException(409, "Refusing to auto-delete an unknown attached volume")
    if len(boot) != 1 or (row.boot_volume_id is not None and boot[0] != row.boot_volume_id):
        raise HTTPException(409, "Exactly one original owned boot volume must be observed")
    return boot[0]


async def settle_missing_server(row, conn):
    if row.boot_volume_id is not None:
        try:
            volume = await run_blocking(conn.block_storage.get_volume, row.boot_volume_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                volume = None
            else:
                raise HTTPException(502, "Provider boot volume observation unavailable") from None
        if volume is not None:
            row.status, row.error_code = "deleting", "provider_boot_volume_pending"
            return
    row.status, row.error_code, row.capability_hash = "deleted", None, None


async def dispose_prepared_volume(session, row, actor, conn, *, allow_delete=False):
    """Only positively owned, unattached settled roots admit one ordinary DELETE."""
    state = preparation(row)
    row.capability_hash = None
    row.status = "deleting"
    if state["phase"] == "volume_not_attempted":
        preparation_update(row, terminal=True, delete_phase="absent", delete_ready=False)
        row.status, row.error_code = "deleted", None
        await session.commit()
        return
    try:
        volume = await run_blocking(get_prepared_volume, conn, row)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Provider prepared volume observation unavailable") from None
    if volume is None:
        if row.boot_volume_id:
            preparation_update(row, terminal=True, delete_phase="absent", delete_ready=False)
            row.status, row.error_code = "deleted", None
        else:
            row.error_code = "provider_volume_create_uncertain"
        await session.commit()
        return
    row.boot_volume_id = str(value(volume, "id"))
    status = str(value(volume, "status", "UNKNOWN")).lower()
    preparation_update(
        row,
        delete_ready=status in ("available", "error") and not value(volume, "attachments", []),
    )
    if state.get("delete_phase") == "uncertain":
        row.error_code = "provider_volume_delete_uncertain"
        await session.commit()
        return  # A lost DELETE is settled by GET only, never another DELETE.
    if status not in ("available", "error") or value(volume, "attachments", []):
        row.error_code = "provider_boot_volume_pending"
        await session.commit()
        return
    if not allow_delete or actor is None or "packages:write" not in actor.get("package_actions", ()):
        row.error_code = "provider_boot_volume_pending"
        await session.commit()
        return
    await fresh_writer(actor, conn)  # Fresh writer after the slow ownership read.
    preparation_update(row, delete_phase="uncertain")
    row.error_code = "provider_volume_delete_uncertain"
    await session.commit()
    await preparation_refence(session, row)
    if row.status != "deleting" or preparation(row).get("delete_phase") != "uncertain":
        return
    volume = await run_blocking(get_prepared_volume, conn, row)
    if volume is None:
        preparation_update(row, terminal=True, delete_phase="absent", delete_ready=False)
        row.status, row.error_code = "deleted", None
        await session.commit()
        return
    if str(value(volume, "status", "")).lower() not in ("available", "error") or value(volume, "attachments", []):
        preparation_update(row, delete_phase="not_attempted", delete_ready=False)
        await session.commit()
        return
    try:
        await fresh_writer(actor, conn)
    except HTTPException:
        preparation_update(row, delete_phase="not_attempted")
        await session.commit()
        raise
    # The prior durable intent remains uncertain on every failed response; no replay.
    with suppress(Exception):
        await run_blocking(conn.block_storage.delete_volume, row.boot_volume_id)
    await session.commit()


def create_not_attempted(row):
    """Only durable pre-allocation failures prove no provider create was issued."""
    state = preparation(row)
    if state:
        return state["phase"] == "volume_not_attempted" and row.status == "error"
    return (
        row.status == "error"
        and row.server_id is None
        and row.boot_volume_id is None
        and row.capability_hash is None
        and row.error_code
        in {
            "create_authority_revoked",
            "create_bootstrap_expired",
            "create_bootstrap_input_limit",
            "create_inputs_changed",
        }
    )


async def reconcile(session, row, conn, actor=None, settings=None):
    if row.status == "deleted":
        return
    state = preparation(row)
    if state and state["phase"] not in ("nova_create_uncertain", "nova_observed"):
        if row.status == "deleting":
            await dispose_prepared_volume(session, row, actor, conn)
        else:
            await advance_prepared_boot(session, row, actor, conn, settings)
        return  # Observation-only continuation; never use a row after its commit.
    if create_not_attempted(row):
        return
    try:
        server = await run_blocking(exact_server, conn, row)
    except HTTPException:
        raise
    except Exception as exc:
        # SDK not-found may be raised rather than returned, but arbitrary failures
        # must never be interpreted as absence or authorize recreation.
        if getattr(exc, "status_code", None) == 404 and row.server_id:
            server = None
        else:
            raise HTTPException(502, "Provider instance observation unavailable") from None
    if server is None:
        if row.status == "deleting" and row.server_id:
            if state:
                await dispose_prepared_volume(session, row, actor, conn)
                return
            else:
                await settle_missing_server(row, conn)
        elif row.server_id:
            row.status = "error"
            row.error_code = "provider_instance_missing"
            row.capability_hash = None
        # An unobserved uncertain create is never declared absent or retried.
    else:
        row.server_id = str(value(server, "id"))
        if state:
            preparation_update(row, phase="nova_observed")
        status = str(value(server, "status", "UNKNOWN")).upper()
        row.provider_status = status[:32]
        if status not in ("BUILD", "DELETED", "ERROR"):
            try:
                row.boot_volume_id = await run_blocking(inspect_boot_volume, conn, row, server)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Provider boot volume observation unavailable") from None
        if status == "ERROR":
            row.error_code = "provider_instance_error"
            if row.status not in {"deleting", "stopping"}:
                row.status = "error"
        elif row.status == "uncertain":
            row.status, row.error_code = "booting", None
        elif row.status == "stopping" and status == "SHUTOFF":
            row.status = "stopped"
        # ACTIVE is provider state, never proof of guest readiness.
    if row.status == "booting" and aware(row.capability_expires_at) <= datetime.now(UTC):
        row.status, row.error_code = "error", "bootstrap_expired"
        row.capability_hash = None
    row.updated_at = datetime.now(UTC)
    await session.commit()


async def observe(factory, actor, conn, instance_id, settings=None):
    project = request_project(actor, conn)
    async with sql_mutex(factory, "build-api:instance:" + instance_id) as session:
        row = await owned(session, instance_id, project)
        await reconcile(session, row, conn, actor, settings)
        return projection(row)


async def advance(factory, actor, conn, instance_id, request, settings):
    project = request_project(actor, conn)
    async with sql_mutex(factory, "build-api:instance:" + instance_id) as session:
        row = await owned(session, instance_id, project)
        if row.request_id != request["request_id"] or row.boot_volume_id != request["boot_volume_id"]:
            raise HTTPException(409, "Advance does not match the original request and prepared boot volume")
        state = preparation(row)
        if state is None:
            raise HTTPException(409, "Legacy instances cannot advance provider creation")
        if actor.get("user_id") != row.created_by:
            raise HTTPException(403, "Only the original instance subject can advance its accepted intent")
        await fresh_writer(actor, conn)
        if request["operation"] == "create-server":
            if (
                row.status != "booting"
                or state.get("terminal")
                or state["phase"] != "nova_not_attempted"
                or server_create_attempted(row) is not False
            ):
                raise HTTPException(409, "Instance has no unattempted ready server creation intent")
            await advance_prepared_boot(session, row, actor, conn, settings, allocate=True, launch=True)
        else:
            if row.status != "deleting":
                raise HTTPException(409, "Volume disposal requires the original explicit deletion intent")
            attempted = server_create_attempted(row)
            if attempted is None:
                raise HTTPException(409, "Unknown server attempt history cannot authorize volume disposal")
            if attempted is True:
                if row.server_id is None:
                    raise HTTPException(409, "Unknown server creation cannot authorize volume disposal")
                try:
                    server = await run_blocking(exact_server, conn, row)
                except Exception as exc:
                    if getattr(exc, "status_code", None) == 404:
                        server = None
                    elif isinstance(exc, HTTPException):
                        raise
                    else:
                        raise HTTPException(502, "Provider instance observation unavailable") from None
                if server is not None:
                    raise HTTPException(409, "Owned provider server must be absent before volume disposal")
            await dispose_prepared_volume(session, row, actor, conn, allow_delete=True)
        return projection(row)


async def action(factory, actor, conn, instance_id, operation):
    project = request_project(actor, conn)
    async with sql_mutex(factory, "build-api:instance:" + instance_id) as session:
        row = await owned(session, instance_id, project)
        if row.status == "deleted":
            return projection(row)
        if row.status == "deleting" and operation == "stop":
            raise HTTPException(409, "Cannot stop an instance with a pending deletion")
        await fresh_writer(actor, conn)
        if create_not_attempted(row):
            if operation != "delete":
                raise HTTPException(409, "Instance create was never attempted")
            # Retain the definite no-call reason through DELETE and future GET;
            # do not turn it into an ambiguous provider-create outcome.
            if preparation(row):
                preparation_update(row, terminal=True, delete_phase="absent", delete_ready=False)
            row.status = "deleted"
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return projection(row)
        try:
            server = await run_blocking(exact_server, conn, row)
        except HTTPException:
            raise
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404 and row.server_id:
                server = None
            else:
                raise HTTPException(502, "Provider instance observation unavailable") from None
        if operation == "delete":
            row.capability_hash = None
            if server is not None:
                try:
                    row.boot_volume_id = await run_blocking(inspect_boot_volume, conn, row, server, deleting=True)
                except HTTPException:
                    row.error_code = "provider_boot_volume_unknown"
                    await session.commit()
                    raise
                except Exception:
                    raise HTTPException(502, "Provider boot volume observation unavailable") from None
            # Unknown create outcome cannot be settled by an empty inventory race.
            if (
                server is None
                and preparation(row)
                and (row.server_id or preparation(row)["phase"] not in ("nova_create_uncertain", "nova_observed"))
            ):
                await dispose_prepared_volume(session, row, actor, conn)
                return projection(row)
            if server is None and row.server_id is None:
                row.status, row.error_code = "deleting", "provider_create_uncertain"
            elif server is None:
                await settle_missing_server(row, conn)
            else:
                row.server_id = str(value(server, "id"))
                row.status, row.error_code = "deleting", None
        else:
            if server is None:
                raise HTTPException(409, "Instance has no observed owned provider server")
            row.status, row.error_code = "stopping", None
        row.updated_at = datetime.now(UTC)
        await session.commit()  # Durable lifecycle intent before effect.
    if server is not None:
        async with sql_mutex(factory, "build-api:instance:" + instance_id) as session:
            row = await owned(session, instance_id, project)
            if operation == "stop" and row.status != "stopping":
                return projection(row)
            try:
                await fresh_writer(actor, conn)
                current = await run_blocking(exact_server, conn, row)
                if current is not None:
                    if operation == "delete":
                        boot_volume_id = await run_blocking(inspect_boot_volume, conn, row, current, deleting=True)
                        if boot_volume_id != row.boot_volume_id:
                            raise HTTPException(
                                409, "Boot volume appeared after deletion intent; retry to bind it durably"
                            )
                        await run_blocking(conn.compute.delete_server, value(current, "id"), ignore_missing=False)
                    elif str(value(current, "status", "")).upper() != "SHUTOFF":
                        await run_blocking(conn.compute.stop_server, value(current, "id"))
            except HTTPException:
                raise
            except Exception:
                row.error_code = "provider_" + operation + "_uncertain"
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return projection(row)
    return projection(row)


def capability(row, supplied):
    actual = hashlib.sha256(supplied.encode()).hexdigest()
    if (
        not row.capability_hash
        or not hmac.compare_digest(actual, row.capability_hash)
        or aware(row.capability_expires_at) <= datetime.now(UTC)
        or row.status in ("deleting", "deleted")
    ):
        raise HTTPException(404, "Bootstrap capability not found or expired")
