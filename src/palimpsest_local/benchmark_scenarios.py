"""Reproducible API/SDK scenarios; fixed programs, durable effects, offline monitoring."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import ssl
import stat
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime
from html import escape
from pathlib import Path

from .benchmark_reporting import _safe_text, render_report
from .errors import ArtifactValidationError
from .state import atomic_write_json

SCHEMA = "palimpsest.benchmark-scenario.v1"
RESULT_SCHEMA = "palimpsest.benchmark-scenario-result.v1"
METHODS = ("openstack-container", "api-import", "api-build", "api-reuse")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_SECRET = re.compile(r"password|secret|credential|authorization|(^|_)token($|_)", re.IGNORECASE)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ArtifactValidationError(f"scenario {label} must be {low}..{high}")
    return value


def _public(value):
    if isinstance(value, dict):
        return {str(k): _public(v) for k, v in value.items() if not _SECRET.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_public(v) for v in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _validate_environment_context(context):
    if not isinstance(context, dict):
        raise ArtifactValidationError("benchmark requires an explicit hourly dedicated-station environment_context")
    root = context.get("measurement_root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ArtifactValidationError("station measurement_root must be an absolute controller directory")
    for name in ("station_id", "project_id", "data_volume_id"):
        if not isinstance(context.get(name), str) or not context[name]:
            raise ArtifactValidationError("station context requires exact " + name)
    _integer(context.get("max_age_seconds", 3600), 1, 3600, "station context maximum age")
    if "disk_checkpoint_root" in context and (
        not isinstance(context["disk_checkpoint_root"], str) or not Path(context["disk_checkpoint_root"]).is_absolute()
    ):
        raise ArtifactValidationError("disk checkpoint root must be an absolute controller directory")


def _environment_context(config):
    from .benchmark_station_measurements import latest_measurement

    context = config["environment_context"]
    sample = latest_measurement(context["measurement_root"], context.get("max_age_seconds", 3600))
    station = sample.get("station", {})
    if not sample.get("cache", {}).get("usable") or any(
        station.get(k) != context[source]
        for k, source in (
            ("server_id", "station_id"),
            ("project_id", "project_id"),
            ("data_volume_id", "data_volume_id"),
        )
    ):
        raise ArtifactValidationError(
            "station checkpoint is missing, stale, changed or belongs to another measurement host"
        )
    if station.get("project_id") != config["project_id"]:
        raise ArtifactValidationError("station project differs")
    environment = dict(sample["environment"])
    if "disk_checkpoint_root" in context:
        from .benchmark_station_disk import latest_disk_sample

        disk = latest_disk_sample(context["disk_checkpoint_root"], context.get("max_age_seconds", 3600))
        binding = disk.get("station", {})
        keys = ("server_id", "project_id", "boot_volume_id", "boot_id", "data_volume_id", "filesystem_uuid")
        if (
            not disk.get("cache", {}).get("usable")
            or not disk.get("raw_logs_complete")
            or any(binding.get(key) != station.get(key) for key in keys)
            or disk.get("probe_source_sha256") != sample.get("probe_source_sha256")
        ):
            raise ArtifactValidationError(
                "separate disk checkpoint is stale, unverified or belongs to another station/boot/volume"
            )
        environment["disk"] = disk["disk"]
        environment["disk_reference"] = {
            key: disk.get(key)
            for key in (
                "schema",
                "result_path",
                "started_unix_seconds",
                "finished_unix_seconds",
                "driver_source_sha256",
                "probe_source_sha256",
                "station",
            )
        }
        sample = dict(sample, disk_checkpoint=disk, network_checkpoint_environment=sample["environment"])
    if environment.get("disk", {}).get("status") != "success":
        raise ArtifactValidationError("dedicated500GiB disk qualification is unavailable")
    sample = dict(sample, environment=environment)
    environment.update(
        measurement_reference=sample["sample_reference"],
        measurement_scope="dedicated4vCPU8GiB500GiB station; not workload-local disk or network",
        contextual_only=True,
        disk_reference=environment.get("disk_reference"),
    )
    return environment, sample


def validate_scenario(config):
    from .benchmark_scenario_cloud import validate_cloud_config
    from .benchmark_scenario_workloads import validate_profile

    if not isinstance(config, dict) or config.get("schema") != SCHEMA:
        raise ArtifactValidationError("unsupported benchmark scenario schema")
    if (
        not _NAME.fullmatch(config.get("job_id", ""))
        or len(config["job_id"]) > 32
        or config.get("method") not in METHODS
    ):
        raise ArtifactValidationError("scenario requires job_id of 1..32 characters and an explicit method")
    if config["scenario"] == "environment":
        raise ArtifactValidationError(
            "environment measurement uses benchmark station / scripts/benchmarks/environment.py; never create a new calibration VM per experiment"
        )
    validate_profile(config)
    validate_cloud_config(config)
    _validate_environment_context(config.get("environment_context"))
    _integer(config.get("repetitions", 1), 1, 10, "repetitions")
    _integer(config.get("deadline_seconds", 7200), 60, 25200, "deadline_seconds")
    approval = config.get("approval", {})
    if not _NAME.fullmatch(approval.get("id", "")) or approval.get("project_id") != config["project_id"]:
        raise ArtifactValidationError("explicit project-bound approval declaration required")
    if config["scenario"] not in approval.get("allowed_scenarios", []):
        raise ArtifactValidationError("scenario is outside the declared approval")
    if not isinstance(approval.get("user_id"), str) or not approval["user_id"]:
        raise ArtifactValidationError("approval requires an exact original actor user_id")
    if config["placement"]["flavor_id"] not in approval.get("allowed_flavor_ids", []):
        raise ArtifactValidationError("flavor is outside the exact declared approval")
    _integer(approval.get("max_vcpus"), 1, 1024, "approved vCPUs per VM")
    _integer(approval.get("max_ram_mib"), 128, 8388608, "approved RAM MiB per VM")
    try:
        expires = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None or expires.timestamp() <= time.time():
            raise ValueError("expired")
    except (KeyError, ValueError, TypeError):
        raise ArtifactValidationError("approval must have an unexpired timezone-aware expiry") from None
    resources = config.get("resources", {})
    for key, upper in (("max_creates", 128), ("max_live", 32), ("max_boot_gib", 32768)):
        requested = _integer(resources.get(key), 1, upper, key)
        permitted = _integer(approval.get(key), 1, upper, "approval " + key)
        if requested > permitted:
            raise ArtifactValidationError("requested resource envelope exceeds approval")
    if not isinstance(approval.get("ledger_root"), str) or not Path(approval["ledger_root"]).is_absolute():
        raise ArtifactValidationError("approval requires an absolute shared private ledger_root")
    required = plan_scenario(config, validate=False)
    if (
        resources["max_creates"] < required["cumulative_creates"]
        or resources["max_live"] < required["max_live"]
        or resources["max_boot_gib"] < required["cumulative_boot_gib"]
    ):
        raise ArtifactValidationError("resource budget cannot complete the selected scenario")
    if config["scenario"] == "k3s" and approval.get("allow_cluster") is not True:
        raise ArtifactValidationError("cluster provisioning requires explicit cluster approval")
    if config["scenario"] in {"torch-cuda", "tensorflow-cuda", "cuda"} and approval.get("allow_gpu") is not True:
        raise ArtifactValidationError("GPU allocation requires explicit GPU approval")
    if config["method"].startswith("api-"):
        placement = config["placement"]
        if (
            not 8 <= placement["boot_gib"] <= 4096
            or not 1 <= placement.get("writable_gib", 8) <= 1024
            or placement.get("writable_gib", 8) >= placement["boot_gib"]
        ):
            raise ArtifactValidationError("API placement exceeds public boot/writable bounds")
        from urllib.parse import urlsplit

        parsed = urlsplit(config.get("hub", {}).get("url", ""))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ArtifactValidationError("scenario Hub must be an explicit verified HTTPS origin")

    # Configuration is persisted; accept credential references, never literal secrets.
    def secrets(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET.search(str(key)):
                    raise ArtifactValidationError("scenario config must not contain literal credentials")
                secrets(item)
        elif isinstance(value, list):
            for item in value:
                secrets(item)

    secrets(config)


def plan_scenario(config, *, validate=True):
    if validate:
        validate_scenario(config)
    repetitions = config.get("repetitions", 1)
    if config["scenario"] == "k3s":
        nodes = 1 + config.get("cluster", {}).get("agents", 1)
        creates, live = repetitions * nodes, nodes
    elif config["method"] == "openstack-container":
        creates, live = repetitions, 1
    else:
        creates, live = repetitions * (2 if config["method"] == "api-reuse" else 1), 1
    return {
        "schema": "palimpsest.benchmark-scenario-plan.v1",
        "job_id": config["job_id"],
        "scenario": config["scenario"],
        "method": config["method"],
        "config_sha256": _digest(config),
        "cumulative_creates": creates,
        "max_live": live,
        "cumulative_boot_gib": creates * config["placement"]["boot_gib"],
        "gpu_required": config["scenario"] in {"torch-cuda", "cuda", "tensorflow-cuda"},
        "cloud_effects": 0,
        "approval_is_operator_declaration_not_cloud_authorization": True,
    }


class ResourceBudget:
    """One approval-wide no-refund creation ledger, shared by queue jobs/processes."""

    def __init__(self, config, output):
        self.config, self.output = config, Path(output)
        approval = config["approval"]
        root = Path(approval["ledger_root"])
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ArtifactValidationError("approval ledger root must be private, owned, not a symlink")
        self.path, self.lockpath = root / (approval["id"] + ".json"), root / (approval["id"] + ".lock")
        self.policy = {
            k: approval[k]
            for k in (
                "id",
                "project_id",
                "user_id",
                "allowed_scenarios",
                "allowed_flavor_ids",
                "max_vcpus",
                "max_ram_mib",
                "max_creates",
                "max_live",
                "max_boot_gib",
                "expires_at",
            )
        }
        self.policy.update(
            allow_gpu=approval.get("allow_gpu", False), allow_cluster=approval.get("allow_cluster", False)
        )
        self.policy.update(
            max_security_groups=approval.get("max_security_groups", 0),
            max_floating_ips=approval.get("max_floating_ips", 0),
        )

    @contextmanager
    def _locked(self):
        fd = os.open(self.lockpath, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                info = self.path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ArtifactValidationError("unsafe approval ledger")
                value = json.loads(self.path.read_text())
                if value["policy"] != self.policy:
                    raise ArtifactValidationError("consumed approval policy changed; no reset or resource refunds")
            else:
                value = {"policy": self.policy, "reservations": {}}
            yield value
            atomic_write_json(self.path, value)

    def check_allocation(self, deadline=None):
        expiry = datetime.fromisoformat(self.policy["expires_at"].replace("Z", "+00:00")).timestamp()
        if time.time() >= expiry:
            raise ArtifactValidationError("approval expired before allocation")
        if deadline is not None and time.monotonic() >= deadline:
            raise ArtifactValidationError("scenario deadline before allocation")

    def reserve(self, role, boot_gib):
        _integer(boot_gib, 1, 32768, "reserved boot GiB")
        if not isinstance(role, str) or not _NAME.fullmatch(role):
            raise ArtifactValidationError("bounded creation role required")
        with self._locked() as value:
            self.check_allocation()
            rows = list(value["reservations"].values())
            own = [r for r in rows if r["job_id"] == self.config["job_id"]]
            if any(r["role"] == role for r in own):
                raise ArtifactValidationError("creation role consumed; inspect instead of replaying")
            for limits, selected in ((self.policy, rows), (self.config["resources"], own)):
                if (
                    len(selected) >= limits["max_creates"]
                    or sum(not r["released"] for r in selected) >= limits["max_live"]
                    or sum(r["boot_gib"] for r in selected) + boot_gib > limits["max_boot_gib"]
                ):
                    raise ArtifactValidationError("approval cumulative/live/boot budget exhausted")
            identity = str(uuid.uuid4())
            value["reservations"][identity] = {
                "job_id": self.config["job_id"],
                "role": role,
                "boot_gib": boot_gib,
                "released": False,
                "server_id": None,
                "reserved_unix_seconds": time.time(),
            }
            return identity

    def bind(self, identity, server_id):
        with self._locked() as value:
            row = value["reservations"][identity]
            if row["job_id"] != self.config["job_id"]:
                raise ArtifactValidationError("reservation belongs to another run")
            if row["server_id"] not in (None, server_id):
                raise ArtifactValidationError("reservation server identity changed")
            row["server_id"] = server_id

    def release(self, identity):
        with self._locked() as value:
            row = value["reservations"][identity]
            if row["job_id"] != self.config["job_id"]:
                raise ArtifactValidationError("reservation belongs to another run")
            row["released"] = True

    def reserve_aux(self, kind, role):
        if kind not in {"security_groups", "floating_ips"} or not isinstance(role, str) or not _NAME.fullmatch(role):
            raise ArtifactValidationError("explicit bounded auxiliary resource kind and role required")
        with self._locked() as value:
            if time.time() >= datetime.fromisoformat(self.policy["expires_at"]).timestamp():
                raise ArtifactValidationError("approval expired before auxiliary allocation")
            rows = value.setdefault("auxiliary", {})
            if any(r["job_id"] == self.config["job_id"] and r["role"] == role for r in rows.values()):
                raise ArtifactValidationError("auxiliary creation role consumed; no replay")
            limit = self.policy["max_" + kind]
            if type(limit) is not int or limit < 1 or sum(r["kind"] == kind for r in rows.values()) >= limit:
                raise ArtifactValidationError("approval auxiliary cumulative budget exhausted")
            identity = str(uuid.uuid4())
            rows[identity] = {
                "kind": kind,
                "role": role,
                "job_id": self.config["job_id"],
                "provider_id": None,
                "released": False,
            }
            return identity

    def bind_aux(self, identity, provider_id):
        with self._locked() as value:
            row = value["auxiliary"][identity]
            if row["job_id"] != self.config["job_id"] or row["provider_id"] not in (None, provider_id):
                raise ArtifactValidationError("auxiliary ownership or provider identity changed")
            row["provider_id"] = provider_id

    def release_aux(self, identity):
        with self._locked() as value:
            row = value["auxiliary"][identity]
            if row["job_id"] != self.config["job_id"] or not row["provider_id"]:
                raise ArtifactValidationError("auxiliary reservation identity unresolved")
            row["released"] = True


def connection_from_stdin():
    """Consume original-caller auth only through a private pipe; never persist it."""
    import logging
    import sys
    from urllib.parse import urlsplit

    logging.disable(logging.CRITICAL)
    raw = sys.stdin.buffer.read(60001)
    if len(raw) > 60000:
        raise ArtifactValidationError("private SDK authentication input exceeds bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ArtifactValidationError("explicit private SDK authentication object required")
    mode = value.get("auth_type", "v3token")
    common = {"auth_type", "auth_url", "project_id", "region_name"}
    if mode == "v3token":
        allowed, required = common | {"token", "user_id"}, {"token"}
    elif mode == "v3password":
        allowed = common | {"username", "password", "user_domain_id", "user_domain_name"}
        required = {"username", "password"}
        if bool(value.get("user_domain_id")) == bool(value.get("user_domain_name")):
            raise ArtifactValidationError("private password auth requires exactly one original user domain")
    else:
        raise ArtifactValidationError("only explicit original-caller v3token/v3password authentication is supported")
    if set(value) - allowed or any(
        not isinstance(value.get(k), str) or not 0 < len(value[k]) <= 8192
        for k in required | {"auth_url", "project_id", "region_name"}
    ):
        raise ArtifactValidationError("explicit bounded private SDK authentication fields required")
    origin = urlsplit(value["auth_url"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
    ):
        raise ArtifactValidationError("private SDK input requires a verified credential-free HTTPS auth endpoint")
    import openstack

    auth = {k: v for k, v in value.items() if k not in {"auth_type", "region_name", "user_id"}}
    connection = None
    try:
        connection = openstack.connect(
            auth_type=mode,
            auth=auth,
            region_name=value["region_name"],
            interface="public",
            verify=True,
            api_timeout=30,
            load_yaml_config=False,
            load_envvars=False,
        )
        if connection.session.verify is not True:
            raise ArtifactValidationError("private SDK authentication requires verified system TLS trust")
        connection.authorize()
        if connection.current_project_id != value["project_id"]:
            raise ArtifactValidationError("original-caller SDK scope differs from private input")
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise ArtifactValidationError(
            "private original-caller SDK authentication failed; no credential details recorded"
        ) from None


def _hub(config, connection):
    from urllib.parse import urlsplit

    from .hub import HubClient, _NoRedirectHandler

    origin = config.get("hub", {}).get("url", "")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or any(ord(c) < 33 for c in origin)
    ):
        raise ArtifactValidationError("authenticated Hub transport requires a credential-free HTTPS origin")
    token = connection.session.get_token()
    client = HubClient(config["hub"]["url"], token, project_id=config["project_id"], timeout_seconds=60)
    context = ssl.create_default_context()
    if config["hub"].get("ca_file"):
        context.load_verify_locations(cafile=config["hub"]["ca_file"])
    client._opener = urllib.request.build_opener(_NoRedirectHandler(), urllib.request.HTTPSHandler(context=context))
    return client


def _wait(get, terminal, deadline, event, kind):
    previous = None
    heartbeat = 0.0
    while time.monotonic() < deadline:
        row = get()
        if row["status"] != previous or time.monotonic() - heartbeat >= 15:
            event(kind, {"id": row["id"], "status": row["status"], "error_code": row.get("error_code")})
            previous = row["status"]
            heartbeat = time.monotonic()
        if row["status"] in terminal:
            return row
        if row["status"] in {"error", "failed", "missing", "uncertain"}:
            raise ArtifactValidationError(
                "remote " + kind + " stopped: " + _safe_text(row.get("error_code")) + "; no replay"
            )
        time.sleep(2)
    raise ArtifactValidationError("scenario observation deadline; inspect persisted IDs, never replay")


def _delete_consumer(
    config, output, role, spec, admitted, row, client, cloud, budget, reservation, deadline, event, kind
):
    """Journal the original deletion once; ambiguous acceptance is observation-only."""
    identity = {
        "job_id": config["job_id"],
        "config_sha256": _digest(config),
        "role": role,
        "request_id": spec.get("request_id"),
        "instance_id": admitted.get("id"),
        "server_id": row.get("server_id"),
        "boot_volume_id": row.get("boot_volume_id"),
        "reservation_id": reservation,
    }
    path = Path(output) / (role + "-delete.json")
    existing = path.exists()
    if existing and json.loads(path.read_text()) != identity:
        raise ArtifactValidationError("original API delete identity changed; retain reservation, never replay")
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise ArtifactValidationError("original API delete identity uncertain; no budget release")

    def validate(observed):
        if any(
            observed.get(key) != identity[source]
            for key, source in (
                ("id", "instance_id"),
                ("request_id", "request_id"),
                ("server_id", "server_id"),
                ("boot_volume_id", "boot_volume_id"),
            )
        ):
            raise ArtifactValidationError("original API delete identity changed; retain reservation, never replay")
        return observed

    validate(row)
    result_path = Path(output) / (role + "-result.json")
    originals = [admitted]
    if result_path.exists():
        originals.append(json.loads(result_path.read_text()))
    for original in originals:
        if any(
            original.get(key) and original[key] != row.get(key)
            for key in ("id", "request_id", "server_id", "boot_volume_id")
        ):
            raise ArtifactValidationError("original API delete identity changed; retain reservation, never replay")
    with budget._locked() as ledger:
        reserved = ledger["reservations"].get(reservation, {})
        if (
            reserved.get("job_id") != config["job_id"]
            or reserved.get("role") != role
            or reserved.get("server_id") not in (None, identity["server_id"])
        ):
            raise ArtifactValidationError("original API delete reservation changed; no budget release")
    budget.bind(reservation, identity["server_id"])
    if not existing:
        if row["status"] not in {"deleting", "deleted"}:
            cloud.verify_external(
                identity["server_id"], expected_marker={"palimpsest_instance_id": identity["instance_id"]}
            )
        atomic_write_json(path, identity)
        if row["status"] not in {"deleting", "deleted"}:
            event("consumer-delete-started", {"id": identity["instance_id"], "role": role})
            client.delete_instance(identity["instance_id"])
    if row["status"] != "deleted":
        row = _wait(
            lambda: validate(_hub(config, cloud.connection).get_instance(identity["instance_id"])),
            {"deleted"},
            deadline,
            event,
            kind,
        )
    atomic_write_json(Path(output) / (role + "-deleted.json"), row)
    cloud.verify_absent(identity["server_id"], identity["boot_volume_id"])
    budget.release(reservation)
    return row


def _source_identity():
    import platform
    import sys

    root = Path(__file__).parent
    names = (
        "benchmark_scenarios.py",
        "benchmark_scenario_cloud.py",
        "benchmark_scenario_workloads.py",
        "benchmark_station_cloud.py",
        "benchmark_station_measurements.py",
        "benchmark_station_cli.py",
        "benchmark_station_disk.py",
        "benchmark_cli.py",
        "benchmark_worker.py",
        "environment_benchmark.py",
        "benchmark_openstack.py",
        "nova_build.py",
        "hub.py",
    )
    files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}
    return {
        "files": files,
        "sha256": _digest(files),
        "python": sys.version,
        "controller": platform.node(),
        "source_hashes_authoritative": True,
    }


@contextmanager
def _executor_lock(output):
    fd = os.open(Path(output) / ".executor.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArtifactValidationError(
                "scenario executor still owns this run; cleanup is not cancellation"
            ) from None
        yield


def run_scenario(config, output, *, cloud_factory=None, connection=None):
    validate_scenario(config)
    output = Path(output).expanduser().absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(output, 0o700)
    with _executor_lock(output):
        return _run_scenario(config, output, cloud_factory=cloud_factory, connection=connection)


def _run_scenario(config, output, *, cloud_factory=None, connection=None):
    from .benchmark_scenario_cloud import ScenarioCloud
    from .benchmark_scenario_workloads import (
        container_command,
        deploy_cluster,
        gpu_preflight,
        validate_result,
        workload_argv,
    )

    atomic_write_json(output / "config.json", config)
    state = {
        "schema": RESULT_SCHEMA,
        "job_id": config["job_id"],
        "scenario": config["scenario"],
        "method": config["method"],
        "status": "running",
        "phase": "preflight",
        "config_sha256": _digest(config),
        "started_unix_seconds": time.time(),
        "measurements": [],
        "resources": [],
        "issues": [],
    }
    state["runner_source"] = _source_identity()
    atomic_write_json(output / "runner-source.json", state["runner_source"])
    started = time.monotonic()
    deadline = started + config.get("deadline_seconds", 7200)

    def event(kind, detail):
        row = {
            "sequence": state.get("event_sequence", 0) + 1,
            "unix_seconds": time.time(),
            "elapsed_seconds": time.monotonic() - started,
            "event": kind,
            "detail": _public(detail),
        }
        fd = os.open(output / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        state.update(event_sequence=row["sequence"], phase=kind, heartbeat_unix_seconds=time.time())
        atomic_write_json(output / "result.json", state)

    live = []
    hub_live = []
    try:
        event("preflight", {"config_sha256": state["config_sha256"]})
        budget = ResourceBudget(config, output)
        kwargs = {"budget": budget}
        if config["method"].startswith("api-"):
            kwargs["observation_only"] = True
        if connection is not None:
            kwargs["connection"] = connection
        cloud = (cloud_factory or ScenarioCloud)(config, output, event, **kwargs)
        for repetition in range(config.get("repetitions", 1)):
            if _source_identity()["files"] != state["runner_source"]["files"]:
                raise ArtifactValidationError("runner source changed during this run")
            if time.monotonic() >= deadline:
                raise ArtifactValidationError("scenario deadline before next repetition")
            prefix = "r" + str(repetition + 1)
            environment, sample = _environment_context(config)
            context_root = output / (prefix + "-station-context")
            context_root.mkdir(mode=0o700)
            atomic_write_json(context_root / "result.json", sample)
            from .benchmark_station_measurements import render_measurement

            render_sample = dict(
                sample, environment=sample.get("network_checkpoint_environment", sample["environment"])
            )
            for name, content in render_measurement(render_sample).items():
                if name not in {"overview.html", "report.md", "issues.md"}:
                    raise ArtifactValidationError("unexpected station report path")
                (context_root / name).write_text(content)
            if sample.get("disk_checkpoint"):
                from .benchmark_reporting import render_report

                disk_sample = sample["disk_checkpoint"]
                disk_view = {
                    "schema": "palimpsest.environment-result.v1",
                    "status": "success",
                    "mode": "openstack",
                    "job_id": "separate-disk-checkpoint",
                    "server_id": sample["station"]["server_id"],
                    "flavor": {"actual": sample["station"]["flavor"]},
                    "disk": disk_sample["disk"],
                    "network": {},
                    "started_unix_seconds": disk_sample["started_unix_seconds"],
                    "issues": [],
                }
                disk_report = render_report([disk_view])
                (context_root / "disk.html").write_text(disk_report["html"])
                note = (
                    "Separate disk-only checkpoint: "
                    + str(disk_sample["started_unix_seconds"])
                    + "–"
                    + str(disk_sample["finished_unix_seconds"])
                    + "; original network cohort is unchanged."
                )
                with (context_root / "overview.html").open("a") as stream:
                    from html import escape

                    stream.write("<p>" + escape(note) + " <a href=disk.html>Disk-only result</a></p>")
                with (context_root / "report.md").open("a") as stream:
                    stream.write("\n\n" + note + " [Disk-only result](disk.html)\n")
            event(
                "station-context-reused",
                {
                    "station_id": sample["station"]["server_id"],
                    "attempt_id": sample["attempt_id"],
                    "finished_unix_seconds": sample["finished_unix_seconds"],
                    "status": sample["status"],
                    "network_requests": 0,
                },
            )
            measurement = {
                "repetition": repetition + 1,
                "environment": environment,
                "environment_context_report": str(context_root / "overview.html"),
                "status": "running",
            }
            state["measurements"].append(measurement)
            if sample["status"] != "success":
                state["issues"].append(
                    {
                        "category": "station-context-partial",
                        "severity": "warning",
                        "message": "Original hourly station cohort is partial; explicit component references retain their own timestamps and failures are not rewritten",
                        "attempt_id": sample["attempt_id"],
                    }
                )
            resource = None
            if config["method"] == "openstack-container":
                resource = cloud.create(prefix + "-workload")
                live.append(resource)
                state["resources"].append(_public(resource))
                if _source_identity()["files"] != state["runner_source"]["files"]:
                    raise ArtifactValidationError("runner source changed during allocation; workload not started")
            if config["scenario"] == "k3s":
                nodes = [resource]
                for index in range(config.get("cluster", {}).get("agents", 1)):
                    node = cloud.create(prefix + "-agent" + str(index + 1))
                    live.append(node)
                    nodes.append(node)
                    state["resources"].append(_public(node))
                event("cluster-deployment-started", {"server_ids": [r["server_id"] for r in nodes]})
                measurement["workload"] = deploy_cluster(cloud, nodes, config, event)
                measurement["status"] = "success"
            elif config["method"] == "openstack-container":
                name = "pal-bench-" + config["job_id"] + "-" + prefix
                if config["scenario"] in {"cuda", "torch-cuda", "tensorflow-cuda"}:
                    measurement["gpu_prerequisites"] = gpu_preflight(cloud, resource, config)
                event(
                    "workload-started",
                    {"server_id": resource["server_id"], "image": config["images"][config["scenario"]]},
                )
                pull = cloud.run_guest(
                    resource,
                    ["sudo", "-n", "docker", "pull", config["images"][config["scenario"]]],
                    label=prefix + "-image-acquisition",
                    timeout=min(1800, max(1, deadline - time.monotonic())),
                )
                if pull["returncode"]:
                    raise ArtifactValidationError("pinned workload image acquisition failed before execution")
                result = cloud.run_guest(
                    resource,
                    container_command(config, name=name),
                    label=prefix + "-workload",
                    timeout=min(
                        config.get("workload", {}).get("timeout_seconds", 300), max(1, deadline - time.monotonic())
                    ),
                )
                if result["returncode"]:
                    raise ArtifactValidationError("real workload command failed; raw bounded evidence retained")
                measurement.update(
                    status="success",
                    workload=validate_result(config, result["stdout"]),
                    workload_seconds=result["elapsed_seconds"],
                )
            else:
                client = _hub(config, cloud.connection)
                identity = str(uuid.uuid5(uuid.NAMESPACE_URL, config["job_id"] + ":" + prefix + ":image"))
                reference = config["images"][config["scenario"]]
                request = {
                    "request_id": identity,
                    "name": "benchmark-" + config["job_id"].replace("_", "-"),
                    "platform": "linux/amd64",
                }
                if config["method"] == "api-build":
                    request["dockerfile"] = "FROM " + reference + "\nCMD " + json.dumps(workload_argv(config)) + "\n"
                    operation = client.build_dockerfile
                else:
                    request["reference"] = reference
                    operation = client.import_registry_image
                budget.check_allocation(deadline)
                atomic_write_json(output / (prefix + "-image-intent.json"), request)
                event("image-submission-started", {"request_id": identity})
                budget.check_allocation(deadline)
                admitted = operation(request)
                atomic_write_json(output / (prefix + "-image-accepted.json"), admitted)
                job = _wait(
                    lambda identity=admitted["id"]: _hub(config, cloud.connection).get_image_job(identity),
                    {"complete"},
                    deadline,
                    event,
                    "image-status",
                )
                atomic_write_json(output / (prefix + "-image-result.json"), job)
                previous = None
                measurement["consumers"] = []
                for index in range(2 if config["method"] == "api-reuse" else 1):
                    role = prefix + "-consumer" + str(index + 1)
                    reservation = budget.reserve(role, config["placement"]["boot_gib"])
                    atomic_write_json(output / (role + "-reservation.json"), {"id": reservation, "role": role})
                    spec = {
                        k: v
                        for k, v in config["placement"].items()
                        if k
                        in {"image_id", "flavor_id", "network_id", "security_group_ids", "boot_gib", "writable_gib"}
                    }
                    spec.update(
                        request_id=str(uuid.uuid5(uuid.NAMESPACE_URL, config["job_id"] + ":" + role)),
                        name="pal-bench-" + role + "-" + config["job_id"],
                        command=workload_argv(config),
                    )
                    spec.update(source_instance_id=previous["id"]) if previous else spec.update(image_job_id=job["id"])
                    atomic_write_json(output / (role + "-intent.json"), spec)
                    event("consumer-create-started", {"role": role, "request_id": spec["request_id"]})
                    at = time.monotonic()
                    admitted = _hub(config, cloud.connection).create_instance(spec)
                    atomic_write_json(output / (role + "-accepted.json"), admitted)
                    state["resources"].append(
                        {
                            "origin": "api",
                            "role": role,
                            "instance_id": admitted["id"],
                            "request_id": spec["request_id"],
                            "status": admitted.get("status"),
                            "server_id": admitted.get("server_id"),
                            "reservation_id": reservation,
                        }
                    )
                    hub_live.append(state["resources"][-1])
                    if admitted.get("server_id"):
                        budget.bind(reservation, admitted["server_id"])
                    row = _wait(
                        lambda identity=admitted["id"]: _hub(config, cloud.connection).get_instance(identity),
                        {"ready"},
                        deadline,
                        event,
                        "consumer-status",
                    )
                    hub_live[-1].update(
                        server_id=row.get("server_id"), boot_volume_id=row.get("boot_volume_id"), status=row["status"]
                    )
                    observed = cloud.verify_external(
                        row["server_id"], expected_marker={"palimpsest_instance_id": row["id"]}
                    )
                    runtime = row.get("result") or {}
                    if runtime.get("exit_code") != 0:
                        raise ArtifactValidationError(
                            "guest readiness without successful benchmark command is not a measurement"
                        )
                    proof = validate_result(config, runtime.get("stdout", ""))
                    if previous and row["layer_digests"] != previous["layer_digests"]:
                        raise ArtifactValidationError("reuse changed the retained ordered layer digests")
                    atomic_write_json(output / (role + "-result.json"), row)
                    measurement["consumers"].append(
                        {
                            "instance_id": row["id"],
                            "server_id": row["server_id"],
                            "elapsed_seconds": time.monotonic() - at,
                            "workload": proof,
                            "sdk": _public(observed),
                            "layer_digests": row["layer_digests"],
                        }
                    )
                    _delete_consumer(
                        config,
                        output,
                        role,
                        spec,
                        admitted,
                        row,
                        _hub(config, cloud.connection),
                        cloud,
                        budget,
                        reservation,
                        deadline,
                        event,
                        "consumer-delete-status",
                    )
                    hub_live[-1]["status"] = "deleted"
                    hub_live.pop()
                    previous = row
                measurement["status"] = "success"
            while live:
                target = live[-1]
                cloud.delete(target)
                live.pop()
            if _source_identity()["files"] != state["runner_source"]["files"]:
                for item in state["measurements"]:
                    item["status"] = "source-invalid"
                raise ArtifactValidationError("runner source changed during execution; measurements are not qualified")
            event("repetition-completed", {"repetition": repetition + 1})
        state.update(status="success", phase="completed")
    except (Exception, KeyboardInterrupt) as error:
        failed_stage = state["phase"]
        retained = list(live)
        if "cloud" in locals() and callable(getattr(cloud, "resources", None)):
            try:
                retained = cloud.resources()
                existing = {r.get("role") for r in state["resources"] if r.get("source") == "sdk"}
                state["resources"].extend(_public(r) for r in retained if r.get("role") not in existing)
            except ArtifactValidationError:
                state["issues"].append(
                    {
                        "category": "receipt-unavailable",
                        "message": "Original resource receipt failed validation; no cloud adoption or cleanup attempted",
                    }
                )
        state.update(status="failed", phase="held-no-replay")
        state["issues"].append(
            {
                "category": type(error).__name__,
                "message": _safe_text(str(error))[:1024]
                if isinstance(error, ArtifactValidationError)
                else "Scenario stopped; consult bounded private command evidence before any new run",
                "stage": failed_stage,
                "retained_sdk_servers": [r["server_id"] for r in retained if r.get("server_id")],
                "uncertain_sdk_roles": [r.get("role") for r in retained if not r.get("server_id")],
                "retained_hub_instances": [
                    {key: r.get(key) for key in ("instance_id", "server_id", "request_id", "role")} for r in hub_live
                ],
            }
        )
        event("held-no-replay", state["issues"][-1])
    finally:
        state.update(finished_unix_seconds=time.time(), elapsed_seconds=time.monotonic() - started)
        if abs(state["finished_unix_seconds"] - state["started_unix_seconds"] - state["elapsed_seconds"]) > 15:
            state.update(status="failed", phase="timing-invalid")
            for measurement in state["measurements"]:
                measurement["status"] = "timing-invalid"
            state["issues"].append(
                {
                    "category": "clock-disagreement",
                    "message": "Controller wall and monotonic clocks diverged; no valid comparison timing",
                }
            )
        atomic_write_json(output / "result.json", state)
        render_scenario_report(state, output)
    return state


def cleanup_scenario(output, *, connection=None):
    with _executor_lock(output):
        return _cleanup_scenario(output, connection=connection)


def _cleanup_scenario(output, *, connection=None):
    """Explicit cleanup reads original receipts; never creates or replays a workload."""
    from .benchmark_scenario_cloud import ScenarioCloud

    output = Path(output).expanduser().absolute()
    config = json.loads((output / "config.json").read_text())
    recorded = json.loads((output / "result.json").read_text())
    if recorded.get("schema") != RESULT_SCHEMA or recorded.get("config_sha256") != _digest(config):
        raise ArtifactValidationError("cleanup configuration differs from the original run")
    # Kernel lock was acquired: a persisted running result is an abandoned owner,
    # not evidence that a dead executor still owns resources.
    if recorded.get("status") == "running":
        recorded.update(status="failed", phase="interrupted-no-replay")
        atomic_write_json(output / "result.json", recorded)
    attempt = output / ("cleanup-" + uuid.uuid4().hex)
    attempt.mkdir(mode=0o700)
    events = []

    def event(kind, detail):
        events.append({"event": kind, "detail": _public(detail), "unix_seconds": time.time()})
        atomic_write_json(attempt / "events.json", events)

    budget = ResourceBudget(config, output)
    cloud = ScenarioCloud(config, output, event, budget=budget, connection=connection, cleanup_only=True)
    deleted = []
    for resource in reversed(cloud.resources()):
        cloud.delete(resource)
        deleted.append({"origin": "sdk", "server_id": resource.get("server_id")})
    for path in sorted(output.glob("r*-consumer*-intent.json")):
        spec = json.loads(path.read_text())
        role = path.name.removesuffix("-intent.json")
        accepted = output / (role + "-accepted.json")
        client = _hub(config, cloud.connection)
        if accepted.exists():
            admitted = json.loads(accepted.read_text())
            row = client.get_instance(admitted["id"])
        else:
            matches = [row for row in client.list_instances(limit=50) if row["request_id"] == spec["request_id"]]
            if len(matches) != 1:
                raise ArtifactValidationError(
                    "uncertain API create cannot be uniquely observed; retain reservation, never replay"
                )
            row = admitted = matches[0]
        reservation_row = json.loads((output / (role + "-reservation.json")).read_text())
        if reservation_row.get("role") != role:
            raise ArtifactValidationError("original API delete reservation role changed; no budget release")
        row = _delete_consumer(
            config,
            output,
            role,
            spec,
            admitted,
            row,
            client,
            cloud,
            budget,
            reservation_row["id"],
            time.monotonic() + 600,
            event,
            "api-cleanup-status",
        )
        deleted.append({"origin": "api", "instance_id": row["id"], "server_id": row.get("server_id")})
    result = {"status": "success", "job_id": config["job_id"], "deleted": deleted, "cloud_creates": 0}
    atomic_write_json(attempt / "result.json", result)
    return result


def scenario_status(output):
    path = Path(output) / "result.json"
    value = json.loads(path.read_text())
    if value.get("schema") != RESULT_SCHEMA:
        raise ArtifactValidationError("not a recorded scenario result")
    fields = (
        "job_id",
        "scenario",
        "method",
        "status",
        "phase",
        "event_sequence",
        "heartbeat_unix_seconds",
        "started_unix_seconds",
        "finished_unix_seconds",
        "elapsed_seconds",
    )
    compact = {key: value[key] for key in fields if key in value}
    compact["measurements_completed"] = sum(row.get("status") == "success" for row in value.get("measurements", []))
    compact["resources"] = [
        {
            key: row.get(key)
            for key in ("origin", "role", "instance_id", "request_id", "server_id", "boot_volume_id", "status")
        }
        for row in value.get("resources", [])
    ]
    compact["environment_contexts"] = [
        {
            "reference": row.get("environment", {}).get("measurement_reference"),
            "disk_reference": row.get("environment", {}).get("disk_reference"),
            "scope": row.get("environment", {}).get("measurement_scope"),
            "report": row.get("environment_context_report"),
        }
        for row in value.get("measurements", [])
    ]
    compact["issues"] = _public(value.get("issues", []))
    compact["report"] = str(Path(output) / "overview.html")
    compact["unacknowledged_api_intents"] = []
    for intent in sorted(Path(output).glob("r*-consumer*-intent.json")):
        if not intent.with_name(intent.name.replace("-intent.json", "-accepted.json")).exists():
            request = json.loads(intent.read_text())
            compact["unacknowledged_api_intents"].append({"file": intent.name, "request_id": request.get("request_id")})
    return compact


def render_scenario_report(result, output):
    """No logs/config/secrets in the overview; failures stay in a separate issue page."""
    output = Path(output)
    title = "Benchmark scenario: " + _safe_text(result["scenario"])
    rows = []
    environments = []

    def metrics(proof):
        from .benchmark_reporting import _fmt

        return [
            _safe_text(proof.get("device")),
            _fmt(proof.get("iterations")),
            _fmt(proof.get("elapsed_seconds")),
            _fmt(proof.get("operations_per_second")),
        ]

    for measurement in result.get("measurements", []):
        environments.append(measurement["environment"])
        if measurement.get("status") != "success":
            continue
        if "consumers" in measurement:
            for index, consumer in enumerate(measurement["consumers"], 1):
                rows.append(
                    [
                        str(measurement["repetition"]),
                        "consumer " + str(index),
                        str(round(consumer["elapsed_seconds"], 6)),
                        _safe_text(consumer["server_id"]),
                        *metrics(consumer.get("workload", {})),
                    ]
                )
        else:
            seconds = measurement.get("workload_seconds", measurement.get("workload", {}).get("elapsed_seconds"))
            rows.append(
                [
                    str(measurement["repetition"]),
                    "environment" if result["scenario"] == "environment" else "workload",
                    "N/A" if seconds is None else str(round(seconds, 6)),
                    "verified",
                    *metrics(measurement.get("workload", {})),
                ]
            )
    headers = [
        "Repetition",
        "Boundary",
        "Elapsed seconds",
        "Evidence",
        "Device",
        "Iterations",
        "Inner compute seconds",
        "Operations/s",
    ]
    markdown = (
        "# "
        + escape(title)
        + "\n\nMethod: "
        + escape(_safe_text(result["method"]))
        + ". Status: "
        + escape(_safe_text(result["status"]))
        + ".\n\n| "
        + " | ".join(headers)
        + " |\n| "
        + " | ".join("---" for _ in headers)
        + " |\n"
    )
    markdown += "\n".join(
        "| " + " | ".join(escape(_safe_text(c)).replace("|", "&#124;") for c in row) + " |" for row in rows
    )
    html = (
        "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>"
        + escape(title)
        + "</title><style>:root{color-scheme:light dark}body{font:16px/1.6 system-ui,sans-serif;margin:2rem auto;padding:0 1.2rem;max-width:1100px}h1{font-size:1.8rem}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:.7rem;border-bottom:1px solid #8886;white-space:nowrap}th{font-size:.85rem}a{color:inherit}main{overflow-wrap:anywhere}table{display:block;overflow-x:auto;overflow-wrap:normal}caption{text-align:left;font-weight:600}p{max-width:85ch}</style><main><h1>"
        + escape(title)
        + "</h1><p>"
        + escape(result["method"] + ": " + result["status"])
        + "</p><table><thead><tr>"
        + "".join("<th>" + escape(h) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join("<td>" + escape(_safe_text(c)) + "</td>" for c in row) + "</tr>" for row in rows)
        + "</tbody></table><p>Successful rows only. Setup/acquisition, request-to-ready and inner computation timings are distinct boundaries; missing data is N/A.</p><p><a href=environment.html>Disk and network measurements</a> · <a href=issues.md>Issues and retained resources</a> · <a href=result.json>Machine-readable result</a></p></main></html>"
    )
    context_paths = [
        Path(row["environment_context_report"])
        for row in result.get("measurements", [])
        if row.get("environment_context_report")
    ]
    if context_paths:
        links = [(path.parent.name + "/overview.html", path.parent.name) for path in context_paths]
        rendered = {
            "markdown": "# Hourly dedicated-station context\n\nNot workload-local hardware. Provider-specific results and two-provider means:\n\n"
            + "\n".join("- [" + escape(label) + "](" + escape(link) + ")" for link, label in links),
            "html": "<!doctype html><html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><h1>Hourly dedicated-station context</h1><p>Not workload-local hardware. Each report preserves both providers and their equal-weight means.</p><ul>"
            + "".join(
                "<li><a href='" + escape(link, quote=True) + "'>" + escape(label) + "</a></li>" for link, label in links
            )
            + "</ul></html>",
            "issues_markdown": "\n".join((path.parent / "issues.md").read_text() for path in context_paths),
        }
    else:
        rendered = render_report(environments)
    (output / "report.md").write_text(
        markdown
        + "\n\nDisk/network: environment.md. Issues: issues.md. Exact source/input hashes and events: result.json/events.jsonl.\n"
    )
    (output / "overview.html").write_text(html)
    (output / "environment.md").write_text(rendered["markdown"])
    (output / "environment.html").write_text(rendered["html"])
    (output / "issues.md").write_text(
        "# Scenario issues\n\n"
        + "\n".join(
            "- " + escape(_safe_text(i["category"])) + ": " + escape(_safe_text(i["message"]))
            for i in result.get("issues", [])
        )
        + "\n\n"
        + rendered["issues_markdown"]
    )
    return {
        "report": str(output / "report.md"),
        "html": str(output / "overview.html"),
        "issues": str(output / "issues.md"),
    }
