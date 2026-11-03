"""Independent direct-IO checkpoints on the original retained station.

This driver never invokes the hourly measurement driver. An uncertain attempt
can only recover its durable checkpoint; it never replays the disk probe.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import inspect
import json
import math
import os
import re
import stat
import time
import uuid
from pathlib import Path

from . import environment_benchmark
from .errors import ArtifactValidationError
from .state import atomic_write_json

SCHEMA = "palimpsest.station-disk.v1"
DISK_PROFILE = {"size_mib": 1024, "runtime_seconds": 20, "repetitions": 5, "iodepth": 16}
_PROFILES = {"sequential-read", "sequential-write", "random-read-4k", "random-write-4k"}
_EXECUTION_SECONDS = 1650  # initialization plus all twenty bounded fio invocations


def _fail(message):
    raise ArtifactValidationError("station disk: " + message)


def _sources():
    return {
        "probe_source_sha256": hashlib.sha256(Path(environment_benchmark.__file__).read_bytes()).hexdigest(),
        "driver_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def _read(path, *, maximum=8 * 1024**2):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            _fail("checkpoint must be an owner-only regular file")
        if info.st_size > maximum:
            _fail("checkpoint exceeds its bound")
        value = json.load(stream)
    if not isinstance(value, dict):
        _fail("checkpoint must be an object")
    return value


def _station(station, resource):
    observed = station.cloud.inspect(resource)
    actual = observed.get("flavor", {}).get("actual", {})
    if (
        observed.get("status") != "ACTIVE"
        or actual.get("vcpus") != 4
        or actual.get("ram_mib", actual.get("ram")) != 8192
    ):
        _fail("original station must be ACTIVE with 4vCPU and 8192MiB")
    for key in ("server_id", "boot_volume_id", "data_volume_id", "filesystem_uuid"):
        try:
            uuid.UUID(resource[key])
        except (KeyError, TypeError, ValueError, AttributeError):
            _fail("invalid original " + key)
    boot = resource.get("station_binding", {}).get("boot_id", resource.get("boot_id"))
    try:
        uuid.UUID(boot)
    except (ValueError, TypeError, AttributeError):
        _fail("original guest boot identity is missing")
    project = station.config.get("project_id")
    user = station.config.get("approval", {}).get("user_id", resource.get("user_id"))
    if (
        resource.get("source") != "sdk"
        or observed.get("server_id") != resource["server_id"]
        or not project
        or resource.get("project_id") != project
        or observed.get("project_id") != project
        or not user
    ):
        _fail("original SDK station project/user identity differs")
    volumes = observed.get("volumes", [])
    if (
        {v.get("id") for v in volumes} != {resource["boot_volume_id"], resource["data_volume_id"]}
        or len(volumes) != 2
        or resource["boot_volume_id"] == resource["data_volume_id"]
        or resource.get("data_size_gib") != 500
        or any(v.get("size") != (500 if v.get("id") == resource["data_volume_id"] else 20) for v in volumes)
    ):
        _fail("original boot and dedicated 500GiB data attachments differ")
    mount = resource.get("data_mountpoint")
    directory = resource.get("data_directory")
    if (
        not isinstance(mount, str)
        or not Path(mount).is_absolute()
        or ".." in Path(mount).parts
        or mount == "/"
        or directory != mount.rstrip("/") + "/measurements"
    ):
        _fail("original measurement directory differs")
    return {
        "server_id": resource["server_id"],
        "project_id": project,
        "user_id": user,
        "boot_id": boot,
        "boot_volume_id": resource["boot_volume_id"],
        "data_volume_id": resource["data_volume_id"],
        "data_gib": 500,
        "filesystem_uuid": resource["filesystem_uuid"],
        "flavor": {"vcpus": 4, "ram_mib": 8192},
        "data_mountpoint": mount,
        "data_directory": directory,
    }


def _guest_main(payload):
    import base64
    import hashlib
    import json
    import os
    import runpy
    import signal
    import stat
    import subprocess
    import time
    from pathlib import Path

    def check(condition):
        if not condition:
            raise RuntimeError("original disk binding differs")

    def interrupted(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    check(os.geteuid() != 0)
    root = Path(payload["remote_root"])
    root.mkdir(mode=0o700)
    evidence = root / "evidence"
    evidence.mkdir(mode=0o700)
    source = base64.b64decode(payload["probe_source"], validate=True)
    check(hashlib.sha256(source).hexdigest() == payload["probe_source_sha256"])
    (root / "probe.py").write_bytes(source)
    probes = runpy.run_path(str(root / "probe.py"))
    station = payload["station"]
    started = time.time()
    value = {
        "schema": "palimpsest.station-disk-guest.v1",
        "complete": False,
        "probe_source_sha256": payload["probe_source_sha256"],
        "driver_source_sha256": payload["driver_source_sha256"],
        "station": station,
        "attempt_id": payload["attempt_id"],
        "started_unix_seconds": started,
        "disk": {"status": "failed"},
        "issues": [],
        "remote_evidence_directory": str(evidence),
        "guest_logs": {},
    }

    def capture():
        logs, total = {}, 0
        for path in sorted(evidence.iterdir()):
            info = path.lstat()
            check(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid())
            check(info.st_size <= 16 * 1024**2)
            total += info.st_size
            check(total <= 64 * 1024**2 and len(logs) < 512)
            data = path.read_bytes()
            logs[path.name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "truncated": True}
        value["guest_logs"] = logs
        value["finished_unix_seconds"] = time.time()
        with (root / "checkpoint.tmp").open("w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        (root / "checkpoint.tmp").replace(root / "checkpoint.json")

    def command(argv):
        return json.loads(subprocess.check_output(argv, text=True, timeout=20))

    def flatten(nodes):
        for node in nodes:
            yield node
            yield from flatten(node.get("children") or [])

    def serials(identity):
        return {identity, identity[:20], identity.replace("-", ""), identity.replace("-", "")[:20]}

    def binding():
        check(Path("/proc/sys/kernel/random/boot_id").read_text().strip() == station["boot_id"])
        check(os.cpu_count() == 4)
        memory = next(line for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))
        # Linux reserves a small part of the API-attested 8192MiB guest RAM.
        check(7168 * 1024 <= int(memory.split()[1]) <= 8192 * 1024)
        fd = probes["_open_directory"](Path(station["data_directory"]))
        try:
            info = os.fstat(fd)
            check(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
            device = info.st_dev
        finally:
            os.close(fd)
        mount = command(
            [
                "findmnt",
                "--json",
                "--target",
                station["data_directory"],
                "--output",
                "SOURCE,FSTYPE,TARGET,UUID,MAJ:MIN",
            ]
        )["filesystems"][0]
        root_mount = command(["findmnt", "--json", "--target", "/", "--output", "SOURCE,MAJ:MIN"])["filesystems"][0]
        check(mount["target"] == station["data_mountpoint"] and mount["fstype"] == "ext4")
        check(mount["uuid"] == station["filesystem_uuid"] and mount["maj:min"] != root_mount["maj:min"])
        check(mount["maj:min"] == str(os.major(device)) + ":" + str(os.minor(device)))
        trees = []
        for target, identity, size in (
            (mount, station["data_volume_id"], 500),
            (root_mount, station["boot_volume_id"], 20),
        ):
            tree = command(
                [
                    "lsblk",
                    "--json",
                    "--inverse",
                    "--paths",
                    "--bytes",
                    "--output",
                    "NAME,SERIAL,SIZE,TYPE,MAJ:MIN",
                    target["source"],
                ]
            )
            nodes = list(flatten(tree["blockdevices"]))
            matches = [node for node in nodes if node.get("serial") in serials(identity)]
            check(len(matches) == 1 and matches[0]["type"] == "disk" and int(matches[0]["size"]) == size * 1024**3)
            trees.append(tree)
        return {"mount": mount, "root_mount": root_mount, "block_devices": trees, "device_id": device}

    directory = root / "disk"
    directory.mkdir(mode=0o700)
    try:
        value["filesystem_binding"] = binding()
        capture()
        try:
            value["disk"] = probes["_disk"](payload["disk"], directory)
        finally:
            for path in directory.iterdir():
                if path.is_file() and path.suffix in (".stdout", ".stderr", ".log"):
                    path.rename(evidence / (payload["attempt_id"] + "-disk-" + path.name))
        for profile in value["disk"].get("profiles", {}).values():
            for repetition in profile.get("repetitions", []):
                repetition["proof"] = payload["attempt_id"] + "-disk-" + repetition["proof"]
        value["filesystem_binding_after"] = binding()
        check(value["filesystem_binding_after"] == value["filesystem_binding"])
        check(value["disk"].get("device_id") == value["filesystem_binding"]["device_id"])
        value["complete"] = True
    except BaseException as error:
        value["issues"].append("disk checkpoint failed: " + type(error).__name__)
    finally:
        capture()
    print(json.dumps(value, allow_nan=False))


class _RecoveryTransport:
    """Retain the shared log collector with unique read-only command labels."""

    def __init__(self, cloud):
        self.cloud = cloud
        self.suffix = uuid.uuid4().hex[:12]

    def __getattr__(self, name):
        return getattr(self.cloud, name)

    def run_guest(self, resource, argv, *, label, **kwargs):
        return self.cloud.run_guest(resource, argv, label=label + "-" + self.suffix, **kwargs)


def _recover(cloud, resource, payload, output):
    # Reading an existing checkpoint is the only recovery operation.
    response = cloud.run_guest(
        resource,
        [
            "python3",
            "-c",
            "import os,stat,sys; fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW); "
            "s=os.fstat(fd); assert stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and s.st_size<=8388608; "
            "print(os.read(fd,8388609).decode()); os.close(fd)",
            payload["remote_root"] + "/checkpoint.json",
        ],
        label="station-disk-recover-" + uuid.uuid4().hex,
        timeout=60,
    )
    atomic_write_json(output / ("recovery-" + uuid.uuid4().hex + ".json"), response)
    if response.get("returncode") != 0 or response.get("truncated") or response.get("timed_out"):
        _fail("durable guest checkpoint is not recoverable; probe will not be replayed")
    try:
        value = json.loads(response["stdout"])
    except (ValueError, KeyError, TypeError):
        _fail("invalid durable guest checkpoint")
    if not isinstance(value, dict) or value.get("schema") != "palimpsest.station-disk-guest.v1":
        _fail("guest checkpoint schema differs")
    for key in ("attempt_id", "station", "probe_source_sha256", "driver_source_sha256"):
        if value.get(key) != payload[key]:
            _fail("guest checkpoint original identity/source differs")
    atomic_write_json(output / "guest-result.json", value)
    logs = value.get("guest_logs")
    if not isinstance(logs, dict):
        _fail("guest log manifest is missing")
    # A fresh local destination allows read-only recovery after an interrupted
    # transfer without overwriting evidence already recovered by a prior call.
    recovered = output / ("logs-" + uuid.uuid4().hex)
    recovered.mkdir(mode=0o700)
    type(cloud)._collect_environment_logs(_RecoveryTransport(cloud), resource, value, recovered)
    value["raw_logs_complete"] = bool(logs) and all(entry.get("collected") for entry in logs.values())
    atomic_write_json(output / "guest-result.json", value)
    return value


def _execute(cloud, resource, payload, output):
    script = (
        "import json\n" + inspect.getsource(_guest_main) + "\n_guest_main(json.loads(__import__('sys').stdin.read()))\n"
    )
    try:
        response = cloud.run_guest(
            resource,
            ["python3", "-c", script],
            label="station-disk-" + payload["attempt_id"],
            timeout=_EXECUTION_SECONDS,
            input_bytes=json.dumps(payload).encode(),
        )
        atomic_write_json(output / "execution.json", response)
    except Exception as error:
        atomic_write_json(output / "execution.json", {"interrupted": True, "error_type": type(error).__name__})
    return _recover(cloud, resource, payload, output)


def _disk_valid(disk, manifest):
    if not isinstance(disk, dict) or not isinstance(manifest, dict) or not isinstance(disk.get("profiles"), dict):
        return False
    if (
        disk.get("status") != "success"
        or disk.get("protocol") != "fio-direct"
        or disk.get("direct") is not True
        or disk.get("ioengine") != "libaio"
        or disk.get("size_bytes") != 1024**3
        or disk.get("runtime_seconds") != 20
        or disk.get("requested_repetitions") != 5
        or disk.get("iodepth") != 16
        or set(disk.get("profiles", {})) != _PROFILES
    ):
        return False
    for profile in disk["profiles"].values():
        if not isinstance(profile, dict):
            return False
        repetitions = profile.get("repetitions", [])
        if profile.get("status") != "success" or not isinstance(repetitions, list) or len(repetitions) != 5:
            return False
        if any(not isinstance(rep, dict) or rep.get("proof") not in manifest for rep in repetitions):
            return False
    return True


def _logs_valid(manifest):
    if not isinstance(manifest, dict) or not manifest or len(manifest) > 512:
        return False
    total = 0
    for name, entry in manifest.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", name)
            or name in {".", ".."}
            or not isinstance(entry, dict)
            or entry.get("collected") is not True
            or type(entry.get("bytes")) is not int
            or not 0 <= entry["bytes"] <= 16 * 1024**2
            or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))
        ):
            return False
        total += entry["bytes"]
        if total > 64 * 1024**2:
            return False
        try:
            path = Path(entry["local_file"])
            directory = environment_benchmark._open_directory(path.parent)
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            finally:
                os.close(directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077
                    or info.st_size != entry["bytes"]
                ):
                    return False
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    return False
        except (OSError, KeyError, TypeError, ValueError):
            return False
    return True


def _view(result, root, maximum, *, reused=True):
    result = copy.deepcopy(result)
    now = time.time()
    started, finished = result.get("started_unix_seconds"), result.get("finished_unix_seconds")
    valid_time = (
        type(started) in (int, float)
        and type(finished) in (int, float)
        and math.isfinite(started)
        and math.isfinite(finished)
        and 0 <= started <= finished <= now
    )
    guest_window = result.get("guest_time_window")
    if (
        not isinstance(guest_window, dict)
        or guest_window.get("started_unix_seconds") != started
        or guest_window.get("finished_unix_seconds") != finished
    ):
        valid_time = False
    age = now - finished if valid_time else None
    stale = age is None or age > maximum
    issues = []
    if stale:
        issues.append("disk checkpoint is stale or its time window is invalid")
    if result.get("schema") != SCHEMA or any(result.get(k) != v for k, v in _sources().items()):
        issues.append("disk checkpoint source/schema differs")
    identity = result.get("station", {})
    if not isinstance(identity, dict):
        identity = {}
    if (
        identity.get("flavor") != {"vcpus": 4, "ram_mib": 8192}
        or identity.get("data_gib") != 500
        or not identity.get("project_id")
        or not identity.get("user_id")
        or identity.get("boot_volume_id") == identity.get("data_volume_id")
    ):
        issues.append("disk checkpoint original hardware/identity differs")
    for key in ("server_id", "boot_id", "boot_volume_id", "data_volume_id", "filesystem_uuid"):
        try:
            uuid.UUID(identity.get(key))
        except (ValueError, TypeError, AttributeError):
            issues.append("disk checkpoint original identity is missing: " + key)
    manifest = result.get("raw_log_manifest", {})
    if result.get("raw_logs_complete") is not True or not _logs_valid(manifest):
        issues.append("disk checkpoint full raw log identity is not proven")
    if not _disk_valid(result.get("disk", {}), manifest):
        issues.append("disk checkpoint fixed direct-IO profile is not successful")
    if result.get("completed") is not True:
        issues.append("disk checkpoint has not completed")
    usable = not issues and result.get("status") == "success"
    path = result.get("result_path", str(Path(root).absolute() / "disk-latest.json"))
    result["cache"] = {
        "reused": reused,
        "age_seconds": age,
        "stale": stale,
        "usable": usable,
        "result_path": path,
        "issues": issues,
    }
    result["sample_reference"] = {
        key: result.get(key)
        for key in (
            "schema",
            "attempt_id",
            "station",
            "started_unix_seconds",
            "finished_unix_seconds",
            "probe_source_sha256",
            "driver_source_sha256",
            "status",
            "result_path",
        )
    }
    result["sample_reference"].update(stale=stale, usable=usable)
    return result


def latest_disk_sample(root, max_age_seconds=3600) -> dict:
    """Read only; validate freshness, fixed profile, sources and full local logs."""
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        _fail("cache maximum age must be between 1 and 3600 seconds")
    root = Path(root).absolute()
    try:
        directory = environment_benchmark._open_directory(root)
        os.close(directory)
        result = _read(root / "disk-latest.json")
    except FileNotFoundError:
        result = {"schema": SCHEMA, "status": "failed", "issues": ["no completed disk checkpoint exists"]}
    return _view(result, root, max_age_seconds)


def run_disk_sample(station, root) -> dict:
    """Serialize data-only execution against every station probe/lifecycle effect."""
    from .benchmark_station_cloud import station_operation

    with station_operation(station.output):
        return _run_disk_sample(station, root)


def _run_disk_sample(station, root) -> dict:
    """Run once or recover a pending original-station attempt without replay."""
    root = Path(root).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = environment_benchmark._open_directory(root)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            _fail("disk checkpoint root must be owner-only")
    finally:
        os.close(directory)
    fd = os.open(root / "station-disk.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            _fail("disk checkpoint lock must be owner-only")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("another worker owns this disk checkpoint")
        resource = station.resource()
        identity = _station(station, resource)
        pending = root / "disk-attempt.json"
        payload = None
        if pending.exists() or pending.is_symlink():
            payload = _read(pending)
            if (
                not isinstance(payload.get("attempt_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", payload["attempt_id"])
                or payload.get("remote_root") != "/tmp/palimpsest-scenario-environment-" + payload["attempt_id"]
                or payload.get("disk") != dict(DISK_PROFILE, directory=identity["data_directory"])
            ):
                _fail("pending disk attempt fixed execution binding differs")
            if payload.get("station") != identity:
                _fail("pending disk attempt belongs to a different original station")
            output = root / ("disk-" + payload["attempt_id"])
            result = _read(output / "result.json")
            if result.get("completed") is True:
                if (
                    result.get("station") != identity
                    or result.get("attempt_id") != payload["attempt_id"]
                    or result.get("result_path") != str(output / "result.json")
                ):
                    _fail("completed disk result differs from its original pending attempt")
                finished = result.get("finished_unix_seconds")
                if type(finished) not in (int, float) or not math.isfinite(finished) or time.time() - finished < 3600:
                    # Recover a controller interruption after result commit,
                    # including when a prior checkpoint still owns latest.
                    # Validate the actual original time/identity/logs, never
                    # synthesize freshness or rerun a completed guest probe.
                    view = _view(result, root, 3600)
                    if view["cache"]["usable"]:
                        latest = root / "disk-latest.json"
                        try:
                            previous = _read(latest)
                        except FileNotFoundError:
                            previous = None
                        newer_success = (
                            previous is not None
                            and _view(previous, root, 3600)["cache"]["usable"]
                            and previous["finished_unix_seconds"] > finished
                        )
                        if not newer_success and previous != result:
                            atomic_write_json(latest, result)
                    return view
                payload = None
            else:
                execute = _recover
        if payload is None:
            attempt = uuid.uuid4().hex
            output = root / ("disk-" + attempt)
            output.mkdir(mode=0o700)
            sources = _sources()
            payload = {
                "attempt_id": attempt,
                "station": identity,
                **sources,
                "remote_root": "/tmp/palimpsest-scenario-environment-" + attempt,
                "disk": dict(DISK_PROFILE, directory=identity["data_directory"]),
            }
            result = {
                "schema": SCHEMA,
                "attempt_id": attempt,
                "station": identity,
                **sources,
                "status": "failed",
                "completed": False,
                "disk": {"status": "not-measured"},
                "started_unix_seconds": time.time(),
                "finished_unix_seconds": None,
                "raw_logs_complete": False,
                "raw_log_manifest": {},
                "issues": [],
                "result_path": str(output / "result.json"),
            }
            atomic_write_json(output / "result.json", result)
            source_directory = output / "sources"
            source_directory.mkdir(mode=0o700)
            for name, path, key in (
                ("environment_benchmark.py", environment_benchmark.__file__, "probe_source_sha256"),
                ("benchmark_station_disk.py", __file__, "driver_source_sha256"),
            ):
                data = Path(path).read_bytes()
                if hashlib.sha256(data).hexdigest() != sources[key]:
                    _fail("source changed before disk attempt")
                environment_benchmark._private_write(source_directory / name, data.decode())
            # Durable intent contains no credentials or source payload. Its
            # existence fences every later invocation into read-only recovery.
            atomic_write_json(pending, payload)
            execute = _execute
        if any(payload.get(key) != value for key, value in _sources().items()):
            _fail("pending disk attempt source differs; preserve for inspection")
        payload = dict(
            payload,
            probe_source=base64.b64encode((output / "sources" / "environment_benchmark.py").read_bytes()).decode(),
        )
        try:
            guest = execute(station.cloud, resource, payload, output)
            result["disk"] = guest.get("disk", {"status": "failed"})
            result["raw_log_manifest"] = guest.get("guest_logs", {})
            result["raw_logs_complete"] = guest.get("raw_logs_complete") is True
            result["issues"] = guest.get("issues", [])
            if guest.get("complete") is not True:
                _fail("guest disk attempt is incomplete; recovery only")
            if (
                guest.get("filesystem_binding") != guest.get("filesystem_binding_after")
                or not guest.get("filesystem_binding")
                or result["disk"].get("device_id") != guest["filesystem_binding"].get("device_id")
            ):
                _fail("guest filesystem binding changed")
            if _station(station, station.resource()) != identity or _sources() != {
                key: payload[key] for key in _sources()
            }:
                _fail("original station/source changed during disk attempt")
            recovered = time.time()
            started, finished = guest.get("started_unix_seconds"), guest.get("finished_unix_seconds")
            if (
                type(started) not in (int, float)
                or type(finished) not in (int, float)
                or not math.isfinite(started)
                or not math.isfinite(finished)
                or not 0 <= started <= finished <= recovered
                or started < result.get("attempt_started_unix_seconds", result["started_unix_seconds"]) - 15
            ):
                _fail("guest probe completion time is invalid")
            result.setdefault("attempt_started_unix_seconds", result["started_unix_seconds"])
            result["guest_time_window"] = {"started_unix_seconds": started, "finished_unix_seconds": finished}
            result["started_unix_seconds"] = started
            result["finished_unix_seconds"] = finished
            result["recovered_unix_seconds"] = recovered
            result["completed"] = True
            if (
                result["raw_logs_complete"]
                and _logs_valid(result["raw_log_manifest"])
                and _disk_valid(result["disk"], result["raw_log_manifest"])
            ):
                result["status"] = "success"
        except Exception as error:
            result["issues"].append("disk attempt failed: " + type(error).__name__)
            result["recovered_unix_seconds"] = time.time()
        atomic_write_json(output / "result.json", result)
        # Incomplete checkpoints cannot displace a completed disk checkpoint.
        if result["completed"]:
            atomic_write_json(root / "disk-latest.json", result)
        return _view(result, root, 3600, reused=execute is _recover)
