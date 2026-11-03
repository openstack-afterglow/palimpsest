"""Explicit grant-bound native execution, not the HTTPS/chroot compatibility lane.

Only operator-prepared environments are opened. A durable SQL intent precedes
all effects; a private journal and SQL checkpoint precede each provider mutation.
A lost checkpoint permanently closes the adapter to further effects in that
request. Restart/retry observes the existing intent, never replays allocation.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import math
import os
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from palimpsest_local.cloud_env import SCHEMA, CloudEnv, CloudEnvError
from palimpsest_local.image_manifest import native_image_projection
from palimpsest_local.nova_native_cloud import (
    NativeCloud,
    _private_json,
    _private_state_json,
    native_delivery_preflight,
    native_prepared_dns,
)
from sqlalchemy import select

from palimpsest_hub.auth import package_target_project_id, validate_token
from palimpsest_hub.models import RuntimeBlobReference, exact_identity
from palimpsest_hub.native_models import PalimpsestNativeInstance
from palimpsest_hub.services.hub_store import HubStoreError
from palimpsest_hub.services.worker_leases import run_blocking, sql_mutex


def _digest(value):
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    )


def _profile(settings, name):
    profile = settings.palimpsest_hub_native_profiles.get(name)
    if profile is None:
        raise HTTPException(503, "Native operator profile is not configured")
    result = profile.model_dump(mode="json") if hasattr(profile, "model_dump") else dict(profile)
    if result.get("enabled") is not True:
        raise HTTPException(503, "Native operator profile is inactive")
    for field in ("root", "grant_path", "publication_root"):
        path = Path(result[field])
        if not path.is_absolute() or ".." in path.parts or str(path) != result[field]:
            raise HTTPException(503, "Native profile paths must be canonical operator-owned paths")
    return result


def _operator_paths(profile):
    publication = Path(profile["publication_root"])
    for field in ("root", "grant_path", "publication_root"):
        path = Path(profile[field])
        if path.resolve(strict=True) != path:
            raise CloudEnvError("native operator path traverses a symlink; HOLD")
        if field != "publication_root" and (path == publication or publication in path.parents):
            raise CloudEnvError("private native state/approval must be outside the exported payload root; HOLD")
    if os.path.lexists(Path(profile["root"]) / profile["environment"] / "hub-sql-hold.json"):
        raise CloudEnvError("native SQL uncertainty sentinel requires explicit operator reconciliation; HOLD")
    if os.path.lexists(Path(profile["root"]) / profile["environment"] / "hub-settlement-pending.json"):
        raise CloudEnvError("native terminal SQL settlement is pending or uncertain; HOLD")
    return _private_json(Path(profile["grant_path"]))


def _hold(profile):
    directory = Path(profile["root"]) / profile["environment"]
    with _private_directory(directory) as parent:
        try:
            descriptor = os.open(
                "hub-sql-hold.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
        except FileExistsError:
            return
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b'{"schema":"palimpsest.native-sql-hold.v1","status":"hold"}\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)


def _scope(actor, conn, profile):
    project, user = package_target_project_id(actor), actor["user_id"]
    if (
        project != actor["project_id"]
        or project != profile["project_id"]
        or user != profile["user_id"]
        or getattr(conn, "_afterglow_project_id", None) != project
        or getattr(conn, "_afterglow_user_id", None) != user
        or getattr(conn, "_afterglow_token", None) != actor["token"]
    ):
        raise HTTPException(403, "Native execution requires the exact original caller profile identity")
    return project, user


def _approval_source(profile, approval, phase, manifest_digest=None):
    """The original approval selects either one image or its CPU-only project."""
    if "source_project_id" in approval:
        if (
            approval["source_project_id"] != profile["project_id"]
            or approval.get("project_id") != profile["project_id"]
            or "source_digest" not in approval
            or approval.get("source_digest") is not None
            or profile.get("manifest_digest") is not None
            or approval.get("limits", {}).get("gpu_servers") != 0
            or approval.get("flavors", {}).get("gpu") is not None
            or approval.get("max_gpu_seconds") != 0
            or phase != "cpu"
        ):
            raise CloudEnvError("native project-image approval requires the exact project and CPU-only policy")
    elif (
        not profile.get("manifest_digest")
        or approval.get("source_digest") != profile["manifest_digest"]
        or (manifest_digest is not None and manifest_digest != profile["manifest_digest"])
    ):
        raise CloudEnvError("native approval/profile binds another exact image manifest")


def _namespace_manager(profile, env, binding):
    from palimpsest_local.ceph_path_access import CephPathAccessManager

    configuration = {
        key: profile.get("ceph_" + key)
        for key in ("fs_name", "data_pool", "issuer_client", "conf_file", "keyring_file")
    }
    configuration["publication_root"] = profile["publication_root"]
    if not isinstance(binding, dict) or any(
        binding.get(key) != expected
        for key, expected in {
            "kind": "manila-namespace-v1",
            "share_id": profile["share_id"],
            "base_export": profile["export"],
            "fs_name": configuration["fs_name"],
            "data_pool": configuration["data_pool"],
        }.items()
    ):
        raise CloudEnvError("native namespace capability differs from the operator base binding; HOLD")
    return CephPathAccessManager(env, configuration, binding)


def _published_storage_admission(profile, approval, env, published):
    from palimpsest_hub.services.ceph_scoped_store import namespace_for, scope_for

    if approval.get("allow_path_scoped_ceph") is not True:
        raise CloudEnvError("published native images require explicit isolated storage approval")
    configuration = {key: profile.get("ceph_" + key) for key in ("fs_name", "data_pool")}
    binding = published.get("storage_binding")
    owner = {"project_id": published.get("project_id"), "user_id": published.get("created_by")}
    if not all(owner.values()):
        raise CloudEnvError("published native storage requires the authoritative image owner")
    scope = scope_for(owner, published.get("visibility"), profile["share_id"])
    namespace = namespace_for(profile["share_id"], scope)
    expected = {
        "kind": "manila-namespace-v1",
        "share_id": profile["share_id"],
        "base_export": profile["export"],
        "export": profile["export"].rstrip("/") + "/cas/" + scope,
        "root": "blobs/sha256",
        "scope_key": scope,
        "fs_name": configuration["fs_name"],
        "data_pool": configuration["data_pool"],
        "namespace": namespace,
    }
    if binding != expected:
        raise CloudEnvError("native published namespace differs from the exact operator base/owner binding")
    manager = _namespace_manager(profile, env, binding)
    manager.preflight()
    env._native_capability_manager = manager


def _verify_published_storage(store, profile, published):
    from palimpsest_hub.services.ceph_scoped_store import scoped_destination
    from palimpsest_hub.services.published_images import storage_destination

    destination, fresh_binding = storage_destination(store)
    scope = None
    try:
        destination.check_identity()
        expected = {
            "kind": "manila",
            "share_id": profile["share_id"],
            "export": profile["export"],
            "root": "blobs/sha256",
        }
        if fresh_binding != expected or str(destination.root) != profile["publication_root"]:
            raise CloudEnvError("published bytes differ from the freshly verified operator Manila base")
        scope = scoped_destination(
            (destination, fresh_binding),
            {"project_id": published["project_id"], "user_id": published["created_by"]},
            published["visibility"],
            fs_name=profile["ceph_fs_name"],
            data_pool=profile["ceph_data_pool"],
            create=False,
        )
        if scope.binding != published["storage_binding"]:
            raise CloudEnvError("published namespace differs from the fresh immutable store binding")
        scope.check_identity()
        for node in published["nodes"]:
            scope.check_payload_layout(node["digest"])
    finally:
        if scope is not None:
            scope.close()
        else:
            destination.close()


def _environment(profile):
    # The path, not a public alias, is the shared serialization identity.
    return _digest({"root": profile["root"], "environment": profile["environment"]})


def _journal(value):
    # SQL JSON null is an existing empty intent, including on deleted rows.
    # Other falsey JSON values are corruption, never an empty grant floor.
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CloudEnvError("native SQL journal is corrupt; HOLD")
    if value.get("run") is not None and not isinstance(value["run"], dict):
        raise CloudEnvError("native SQL run journal is corrupt; HOLD")
    for section in ("resources", "grant"):
        records = value.get(section, {})
        if not isinstance(records, dict) or any(not isinstance(item, dict) for item in records.values()):
            raise CloudEnvError("native SQL resource/grant journal is corrupt; HOLD")
    for grant in value.get("grant", {}).values():
        used = grant.get("used", {})
        if (
            not isinstance(used, dict)
            or any(type(spent) is not int or spent < 0 for spent in used.values())
            or type(grant.get("gpu_seconds", 0)) is not int
            or grant.get("gpu_seconds", 0) < 0
        ):
            raise CloudEnvError("native SQL approval journal is corrupt; HOLD")
    return value


def projection(row):
    run = _journal(row.journal_json).get("run") or {}
    result = _result_projection(run.get("verified_result"))
    return {
        "id": row.id,
        "request_id": row.request_id,
        "name": row.spec_json["request"]["name"],
        "image_id": row.image_id,
        "manifest_digest": row.manifest_digest,
        "profile": row.profile,
        "status": row.status,
        "server_id": row.server_id,
        "run_id": row.run_id,
        "error_code": row.error_code,
        "keep": row.spec_json["request"]["keep"],
        "command_exit_code": (result.get("command") or {}).get("exit_code"),
        "result": result,
        "allocation_deadline": run.get("allocation_deadline"),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "semantics": "bounded-command-then-idle-pid1; not long-lived readiness or attestation",
    }


def _result_projection(result):
    """Never expose the private plan, manifest, enrollment or arbitrary result keys."""
    if not isinstance(result, dict):
        return {}
    fields = {
        "root": ("pid1", "filesystem", "device", "inode", "mount_id", "proc1_same_root", "proc1_device", "proc1_inode"),
        "checks": ("carrier_inventory", "instance_metadata", "plan_policy", "upper_private", "provider_readonly_errno"),
        "command": ("exit_code", "uid", "gid", "runtime_user", "cmd_present"),
        "gpu": ("enabled", "driver_version", "kernel_release", "uuid", "smi_uuid", "expected_pci_alias"),
    }
    safe = {
        section: {
            key: result[section][key]
            for key in keys
            if key in result.get(section, {})
            and type(result[section][key]) in (str, int, float, bool, type(None))
            and (not isinstance(result[section][key], float) or math.isfinite(result[section][key]))
            and (not isinstance(result[section][key], str) or len(result[section][key].encode()) <= 256)
        }
        for section, keys in fields.items()
        if isinstance(result.get(section), dict)
    }
    checks = result.get("checks")
    if isinstance(checks, dict) and isinstance(checks.get("layers"), list):
        safe.setdefault("checks", {})["layers"] = [
            {"sha256": item["sha256"], "size": item["size"]}
            for item in checks["layers"][:128]
            if isinstance(item, dict)
            and isinstance(item.get("sha256"), str)
            and len(item["sha256"]) == 64
            and all(char in "0123456789abcdef" for char in item["sha256"])
            and type(item.get("size")) is int
        ]
    gpu = result.get("gpu")
    computation = gpu.get("computation") if isinstance(gpu, dict) else None
    if isinstance(computation, dict):
        selected = {
            key: computation[key]
            for key in ("success", "device_index", "device_uuid", "runtime_version", "checksum", "cublas_checksum")
            if (
                type(computation.get(key)) in (bool, int, float)
                and (not isinstance(computation[key], float) or math.isfinite(computation[key]))
            )
            or (key == "device_uuid" and isinstance(computation.get(key), str) and len(computation[key]) <= 128)
        }
        for key in ("results", "cublas_results"):
            value = computation.get(key)
            if (
                isinstance(value, list)
                and len(value) <= 32
                and all(
                    type(item) in (int, float) and (not isinstance(item, float) or math.isfinite(item))
                    for item in value
                )
            ):
                selected[key] = value
        safe.setdefault("gpu", {})["computation"] = selected
    return safe


async def _owned(session, identity, actor):
    row = await session.get(PalimpsestNativeInstance, identity)
    if row is None or row.project_id != package_target_project_id(actor) or row.created_by != actor["user_id"]:
        raise HTTPException(404, "Native instance not found")
    return row


async def list_instances(factory, actor, limit):
    async with factory() as session:
        rows = (
            await session.scalars(
                select(PalimpsestNativeInstance)
                .where(
                    exact_identity(PalimpsestNativeInstance.project_id, package_target_project_id(actor)),
                    exact_identity(PalimpsestNativeInstance.created_by, actor["user_id"]),
                )
                .order_by(PalimpsestNativeInstance.created_at.desc(), PalimpsestNativeInstance.id.desc())
                .limit(limit)
            )
        ).all()
        return [projection(row) for row in rows]


async def get(factory, actor, identity):
    # Deliberately SQL-only. NativeCloud.status can publish newly read results,
    # so it must not be used from an observation-only public GET.
    async with factory() as session:
        return projection(await _owned(session, identity, actor))


@contextmanager
def _private_directory(path):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CloudEnvError("native environment directory must be owned mode0700; HOLD")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _operation_lock(profile):
    directory = Path(profile["root"]) / profile["environment"]
    with _private_directory(directory) as parent:
        lock = os.open("operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise CloudEnvError("native operation lock identity invalid; HOLD")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield parent
        finally:
            os.close(lock)


class _CallerEnvironment(CloudEnv):
    """CloudEnv journals/ownership checks with no clouds.yaml/global connection."""

    def __init__(self, profile, actor, conn, checkpoint):
        self.profile, self.actor, self.caller, self.checkpoint = profile, actor, conn, checkpoint
        self.sql_held = False
        super().__init__(profile["environment"], root=Path(profile["root"]))
        self._hub_published_image = None
        self._native_authorize = None
        self._native_capability_manager = None

    def grant(self, role, level):
        if self._native_capability_manager is None or level != "ro":
            raise CloudEnvError("server native storage requires the selected namespace read-only issuer")
        return self._native_capability_manager.issue(role)

    def _revoke(self, role):
        record = self.resource("access_rules", role)
        if record is None:
            return
        if record.get("provider") != "ceph-namespace-v1":
            return super()._revoke(role)
        manager = _namespace_manager(self.profile, self, record.get("binding"))
        return manager.revoke(role)

    def native_ceph_access(self, run):
        if self._native_capability_manager is None:
            raise CloudEnvError("native selected namespace issuer is unavailable")
        access = self._native_capability_manager.access(run["run_id"])
        return {
            "export": access["export"],
            "name": access["access_to"],
            "secret": access["access_key"],
            "fs_name": access["fs_name"],
        }

    def native_ceph_absent(self, record):
        manager = _namespace_manager(self.profile, self, record.get("binding"))
        manager.check_absent(record)

    def native_authorize_image(self):
        if not callable(self._native_authorize):
            raise CloudEnvError("native published image requires fresh server Hub authorization")
        self._native_authorize()

    def _reload(self):
        self.state = _private_state_json(self.state_path)
        if self.state.get("schema") != SCHEMA or self.state.get("name") != self.name:
            raise CloudEnvError("native operator environment identity mismatch; HOLD")

    def _authenticate(self, cloud):
        if self.sql_held:
            raise CloudEnvError("native SQL checkpoint uncertain; HOLD, no further effects")
        _scope(self.actor, self.caller, self.profile)
        fresh = validate_token(self.actor["token"], self.actor["project_id"])
        if fresh["project_id"] != self.profile["project_id"] or fresh["user_id"] != self.profile["user_id"]:
            raise CloudEnvError("original caller identity changed; HOLD")
        if self.state.get("project_id") != fresh["project_id"] or self.state.get("user_id") != fresh["user_id"]:
            raise CloudEnvError("operator environment belongs to another caller; HOLD")
        self._checkpoint()
        self._connection = self.caller

    def _checkpoint(self):
        if self.sql_held:
            raise CloudEnvError("native SQL checkpoint uncertain; HOLD")
        try:
            self.checkpoint(copy.deepcopy(self.state))
        except BaseException:
            self.sql_held = True
            _hold(self.profile)
            raise

    def save(self):
        # File first: a SQL commit that becomes uncertain cannot lose the local
        # one-shot resource intent. No subsequent boundary passes SQL failure.
        if self.sql_held:
            raise CloudEnvError("native SQL checkpoint uncertain; HOLD")
        super().save()
        self._checkpoint()


async def _checkpoint(factory, identity, actor, profile_digest, state, *, resuming=False):
    async with factory() as session:
        row = await _owned(session, identity, actor)
        if row.profile_digest != profile_digest or row.status == "deleted":
            raise CloudEnvError("native SQL ownership/lifecycle changed; HOLD")
        run = state.get("native", {}).get("runs", {}).get(row.run_id)
        resources = {
            kind: records[row.run_id] for kind, records in state.get("resources", {}).items() if row.run_id in records
        }
        grants = state.get("native", {}).get("grants", {})
        _journal({"run": run, "resources": resources, "grant": grants})
        previous = _journal(row.journal_json)
        if previous.get("run") is not None and run is None:
            raise CloudEnvError("native durable run disappeared from operator journal; HOLD")
        if run and previous.get("run"):
            for key in (
                "run_id",
                "phase",
                "grant_id",
                "source_digest",
                "source_project_id",
                "package_sha256",
                "command",
                "keep",
                "allocation_seconds",
                "boot_gib",
                "flavor_id",
            ):
                if key in previous["run"] and run.get(key) != previous["run"][key]:
                    raise CloudEnvError("native immutable run binding changed; HOLD")
            if row.server_id and run.get("server_id") != row.server_id:
                raise CloudEnvError("native exact server identity changed; HOLD")
        # Deleted consumers remain grant high-water authority. Restoring an old
        # private state file cannot refund a completed request's allocation.
        prior_journals = (
            await session.scalars(
                select(PalimpsestNativeInstance.journal_json).where(
                    PalimpsestNativeInstance.environment_digest == row.environment_digest,
                    PalimpsestNativeInstance.id != row.id,
                    PalimpsestNativeInstance.journal_json.is_not(None),
                )
            )
        ).all()
        for journal in [previous, *prior_journals]:
            for grant_id, original in _journal(journal).get("grant", {}).items():
                current = grants.get(grant_id)
                if (
                    current is None
                    or current.get("digest") != original.get("digest")
                    or current.get("gpu_seconds", 0) < original.get("gpu_seconds", 0)
                    or any(
                        current.get("used", {}).get(kind, -1) < spent
                        for kind, spent in original.get("used", {}).items()
                    )
                ):
                    raise CloudEnvError("native environment approval ledger regressed; HOLD, no budget refund")
        for kind, original in previous.get("resources", {}).items():
            current = resources.get(kind)
            if current is None:
                absent = any(
                    item.get("kind") == kind
                    and item.get("role") == row.run_id
                    and item.get("resource", {}).get("id") == original.get("id")
                    and item.get("absent_at")
                    and (
                        original.get("id")
                        or (
                            kind == "native_ssh_keys"
                            and item.get("resource", {}).get("line") == original.get("line")
                            and item.get("resource", {}).get("worker_id") == original.get("worker_id")
                            and original.get("line")
                            and original.get("worker_id")
                        )
                    )
                    for item in state.get("history", [])
                )
                if not absent:
                    raise CloudEnvError("native resource intent disappeared without exact absence evidence; HOLD")
            elif (original.get("id") is not None and current.get("id") != original["id"]) or (
                resuming and original.get("id") != current.get("id")
            ):
                raise CloudEnvError("native durable provider identity changed; HOLD, never adopt")
        if run and run.get("status") == "cleaned" and resources:
            raise CloudEnvError("native resources remain behind a claimed cleanup; HOLD")
        row.journal_json = {
            "run": run,
            "resources": resources,
            "grant": grants,
        }
        if run:
            row.server_id = run.get("server_id")
        row.updated_at = datetime.now(UTC)
        await session.commit()


def _execute(profile, actor, conn, checkpoint, identity, published, request, approval_digest, deleting):
    with _operation_lock(profile):
        # Recheck after flock: a competing terminal commit may have become
        # visible before its acknowledgement, leaving a durable HOLD fence.
        if _digest(_operator_paths(profile)) != approval_digest:
            raise CloudEnvError("native original approval bytes changed; HOLD")
        env = _CallerEnvironment(profile, actor, conn, checkpoint)
        env._authenticate(env.state["cloud"])
        share = env._confirmed("shares", "layers")
        if not share or share["id"] != profile["share_id"] or share.get("export") != profile["export"]:
            raise CloudEnvError("native operator share binding differs; HOLD")
        controller = NativeCloud(env)
        grant_path = Path(profile["grant_path"])
        if deleting:
            if identity not in controller.state["runs"]:
                raise CloudEnvError("native journal run missing; HOLD, no reconstruction")
            return controller.cleanup(grant_path=grant_path, run_id=identity)
        _published_storage_admission(profile, _operator_paths(profile), env, published)
        approval = _operator_paths(profile)
        _approval_source(profile, approval, request["phase"], published["manifest_digest"])
        env._native_authorize = getattr(checkpoint, "authorize", None)
        env._hub_published_image = {
            "project_id": profile["project_id"],
            "image_id": published["id"],
            "manifest_digest": published["manifest_digest"],
            "storage_binding": copy.deepcopy(published["storage_binding"]),
        }
        image = native_image_projection(published["manifest"])
        if profile.get("manifest_digest") is not None and published["manifest_digest"] != profile["manifest_digest"]:
            raise CloudEnvError("native profile approves another image manifest; HOLD")
        return controller.run(
            grant_path=grant_path,
            image=image,
            phase=request["phase"],
            timeout=request["timeout"],
            command=request["command"],
            keep=request["keep"],
            run_id=identity,
        )


async def _commit_settlement(factory, actor, identity, held):
    async with factory() as session:
        row = await _owned(session, identity, actor)
        if row.status not in {"creating", "deleting"}:
            return
        run = _journal(row.journal_json).get("run") or {}
        status = run.get("status")
        row.status = "held" if held else {"cleaned": "deleted", "retained": "retained"}.get(status, "held")
        row.error_code = "native_operation_held" if row.status == "held" else None
        row.updated_at = datetime.now(UTC)
        await session.commit()


def _settle_locked(profile, commit):
    with _operation_lock(profile) as parent:
        # Existence alone fences crash/commit-unknown recovery. Only the same
        # continuously locked transaction removes this after acknowledged SQL.
        try:
            descriptor = os.open(
                "hub-settlement-pending.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b'{"schema":"palimpsest.native-settlement.v1","status":"pending"}\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
            commit()
            os.unlink("hub-settlement-pending.json", dir_fd=parent)
            os.fsync(parent)
        except BaseException:
            _hold(profile)
            raise


async def _settle(factory, actor, identity, profile, *, held=False):
    loop = asyncio.get_running_loop()

    def commit():
        asyncio.run_coroutine_threadsafe(_commit_settlement(factory, actor, identity, held), loop).result()

    await run_blocking(_settle_locked, profile, commit)


async def _run(factory, actor, conn, profile, row, deleting=False):
    loop = asyncio.get_running_loop()
    identity, run_id, spec, profile_digest = row.id, row.run_id, copy.deepcopy(row.spec_json), row.profile_digest
    first_checkpoint = True

    def checkpoint(state):
        nonlocal first_checkpoint
        asyncio.run_coroutine_threadsafe(
            _checkpoint(factory, identity, actor, profile_digest, state, resuming=first_checkpoint), loop
        ).result()
        first_checkpoint = False

    async def authorize_selected():
        from palimpsest_hub.services.published_images import resolve_image

        fresh = await resolve_image(factory, spec["published"]["id"], actor)
        if fresh != spec["published"]:
            raise CloudEnvError("native published image snapshot or authorization changed; HOLD")

    def authorize():
        asyncio.run_coroutine_threadsafe(authorize_selected(), loop).result()

    checkpoint.authorize = authorize

    try:
        await run_blocking(
            _execute,
            profile,
            actor,
            conn,
            checkpoint,
            run_id,
            spec["published"],
            spec["request"],
            spec["approval_digest"],
            deleting,
        )
    except asyncio.CancelledError:
        # run_blocking has drained the driver before reaching this branch.
        await _settle(factory, actor, identity, profile)
        raise
    except Exception:
        await _settle(factory, actor, identity, profile, held=True)
    else:
        await _settle(factory, actor, identity, profile)
    return await get(factory, actor, identity)


async def _background_run(factory, settings, original, identity, profile_digest, deleting):
    """One accepted intent, one fresh caller connection; never a restart queue."""
    from palimpsest_hub.auth import get_os_conn, get_package_manager_info

    profile = None
    try:
        fresh = await run_blocking(validate_token, original["token"], original["project_id"])
        if fresh["project_id"] != original["project_id"] or fresh["user_id"] != original["user_id"]:
            raise CloudEnvError("native background original caller identity changed; HOLD")
        actor = await get_package_manager_info(token_info=fresh)
        if "packages:write" not in actor.get("package_actions", ()):
            raise CloudEnvError("native background original caller lost publication authority; HOLD")
        async with factory() as session:
            row = await _owned(session, identity, actor)
            if row.status != ("deleting" if deleting else "creating"):
                return
            if row.profile_digest != profile_digest:
                raise CloudEnvError("native background intent changed; HOLD, never replay")
            candidate = _profile(settings, row.profile)
            if _digest(candidate) != profile_digest:
                raise CloudEnvError("native background operator profile changed; HOLD")
            profile = candidate
            session.expunge(row)
        connection = get_os_conn(token_info=actor)
        try:
            conn = await anext(connection)
            _scope(actor, conn, profile)
            await _run(factory, actor, conn, profile, row, deleting=deleting)
        finally:
            await connection.aclose()
    except Exception:
        if profile is not None:
            directory = Path(profile["root"]) / profile["environment"]
            if any(os.path.lexists(directory / name) for name in ("hub-settlement-pending.json", "hub-sql-hold.json")):
                return  # Preserve the original commit-unknown fence; never submit a second terminal COMMIT.
            await _settle(factory, original, identity, profile, held=True)
        else:
            await _commit_settlement(factory, original, identity, True)


def _schedule(background, factory, settings, actor, profile, row, deleting=False):
    original = {key: actor[key] for key in ("token", "project_id", "user_id")}
    background.add_task(_background_run, factory, settings, original, row.id, _digest(profile), deleting)


async def create(factory, store, settings, actor, conn, request, *, background=None):
    from palimpsest_hub.services.published_images import resolve_image

    project = package_target_project_id(actor)
    _scope(actor, conn, {"project_id": project, "user_id": actor["user_id"]})
    fingerprint = _digest(request)
    async with factory() as session:
        existing = await session.scalar(
            select(PalimpsestNativeInstance).where(
                exact_identity(PalimpsestNativeInstance.project_id, project),
                PalimpsestNativeInstance.request_id == request["request_id"],
            )
        )
        if existing is not None:
            if existing.created_by != actor["user_id"] or existing.request_digest != fingerprint:
                raise HTTPException(409, "Native request ID is already bound to another immutable request")
            return projection(existing)

    profile = _profile(settings, request["profile"])
    project, user = _scope(actor, conn, profile)
    try:
        approval = await run_blocking(_operator_paths, profile)
    except CloudEnvError:
        raise HTTPException(409, "Native private operator state is pending or held") from None
    if (
        approval.get("project_id") != project
        or approval.get("environment") != profile["environment"]
        or approval.get("allow_path_scoped_ceph") is not True
        or type(approval.get("expires_unix")) is not int
        or approval["expires_unix"] <= time.time()
    ):
        raise HTTPException(409, "Native original operator approval does not currently bind this profile")
    if request["keep"] and approval.get("allow_retained") is not True:
        raise HTTPException(409, "Native retained instance requires explicit allow_retained operator approval")
    published = await resolve_image(factory, request["image_id"], actor)
    try:
        _approval_source(profile, approval, request["phase"], published["manifest_digest"])
    except CloudEnvError:
        raise HTTPException(409, "Native original operator approval does not bind this image selection") from None
    if published["manifest"]["mode"] != "oci-root" or published["manifest"]["platform"] != "linux/amd64":
        raise HTTPException(409, "Native execution requires an explicitly published linux/amd64 OCI-root manifest")
    try:
        await run_blocking(_verify_published_storage, store, profile, published)
        image = native_image_projection(published["manifest"])
        environment_state = await run_blocking(_CallerEnvironment, profile, actor, conn, None)
        native_delivery_preflight(
            image,
            request["command"],
            request["phase"],
            project,
            dns=native_prepared_dns(environment_state.state),
            storage=published["storage_binding"],
        )
    except (CloudEnvError, HubStoreError, ValueError, OSError):
        raise HTTPException(
            409, "Published image exceeds native bootstrap enrollment/plan/seed admission bounds"
        ) from None
    try:
        await run_blocking(_published_storage_admission, profile, approval, environment_state, published)
    except (CloudEnvError, ValueError, OSError):
        raise HTTPException(409, "Published native images require an approved secure storage boundary") from None
    environment = _environment(profile)
    async with sql_mutex(factory, "build-api:native-env:" + environment[7:]) as session:
        existing = await session.scalar(
            select(PalimpsestNativeInstance).where(
                exact_identity(PalimpsestNativeInstance.project_id, project),
                PalimpsestNativeInstance.request_id == request["request_id"],
            )
        )
        if existing is not None:
            if existing.created_by != user or existing.request_digest != fingerprint:
                raise HTTPException(409, "Native request ID is already bound to another immutable request")
            return projection(existing)
        active = await session.scalar(
            select(PalimpsestNativeInstance.id)
            .where(
                PalimpsestNativeInstance.environment_digest == environment,
                PalimpsestNativeInstance.status != "deleted",
            )
            .limit(1)
        )
        if active is not None:
            raise HTTPException(409, "Native environment retains an active or uncertain consumer; HOLD")
        identity = str(uuid.uuid4())
        row = PalimpsestNativeInstance(
            id=identity,
            project_id=project,
            created_by=user,
            request_id=request["request_id"],
            request_digest=fingerprint,
            profile=request["profile"],
            profile_digest=_digest(profile),
            environment_digest=environment,
            image_id=published["id"],
            manifest_digest=published["manifest_digest"],
            run_id="native-" + uuid.uuid4().hex,
            spec_json={"request": request, "published": published, "approval_digest": _digest(approval)},
            status="creating",
            journal_json=None,
        )
        session.add(row)
        for digest in {node["digest"] for node in published["nodes"]}:
            session.add(RuntimeBlobReference(resource_kind="native-instance", resource_id=identity, digest=digest))
        await session.commit()
        # Copy before session close; never rely on expire_on_commit settings.
        await session.refresh(row)
        session.expunge(row)
    if background is not None:
        _schedule(background, factory, settings, actor, profile, row)
        return projection(row)
    return await _run(factory, actor, conn, profile, row)


async def delete(factory, settings, actor, conn, identity, *, background=None):
    async with factory() as session:
        row = await _owned(session, identity, actor)
        name, environment = row.profile, row.environment_digest
    profile = _profile(settings, name)
    _scope(actor, conn, profile)
    if _environment(profile) != environment:
        raise HTTPException(409, "Native operator environment changed; HOLD")
    async with sql_mutex(factory, "build-api:native-env:" + environment[7:]) as session:
        row = await _owned(session, identity, actor)
        if row.profile_digest != _digest(profile):
            raise HTTPException(409, "Native operator profile changed; HOLD")
        if row.status == "deleted":
            return projection(row)
        if row.status in {"creating", "deleting"}:
            raise HTTPException(409, "Native operation outcome uncertain or in progress; HOLD")
        row.status = "deleting"
        row.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
    if background is not None:
        _schedule(background, factory, settings, actor, profile, row, deleting=True)
        return projection(row)
    return await _run(factory, actor, conn, profile, row, deleting=True)
