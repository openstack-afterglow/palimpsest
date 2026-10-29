"""Durable, bounded checkpoints on one retained benchmark station.

A failed or interrupted attempt consumes the same interval as a successful one.
Reading context never executes a probe; only ``run_measurement`` can claim a slot.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import inspect
import ipaddress
import json
import math
import os
import stat
import time
import uuid
from pathlib import Path

from . import environment_benchmark
from .benchmark_reporting import render_report
from .errors import ArtifactValidationError
from .state import atomic_write_json

SCHEMA = "palimpsest.station-measurement.v1"
DISK_PROFILE = {"size_mib": 1024, "runtime_seconds": 20, "repetitions": 5, "iodepth": 16}
_PROVIDERS = {"cloudflare", "ovh"}


def _fail(message):
    raise ArtifactValidationError(message)


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        _fail(f"invalid station measurement {name}")
    return value


def validate_measurement(config: dict) -> None:
    """Validate the measurement subdict without contacting any endpoint."""
    if not isinstance(config, dict):
        _fail("station measurement configuration is required")
    _integer(config.get("interval_seconds", 3600), 1800, 3600, "interval")
    not_before = config.get("not_before_unix_seconds", 0)
    if type(not_before) not in (int, float) or not math.isfinite(not_before) or not_before < 0:
        _fail("invalid station first-sample not-before time")
    providers = config.get("providers")
    if not isinstance(providers, list) or len(providers) != 2:
        _fail("exactly two explicit station providers are required")
    if any(not isinstance(p, dict) for p in providers) or {p.get("provider_id") for p in providers} != _PROVIDERS:
        _fail("distinct cloudflare and ovh provider_id values are required")
    for provider in providers:
        for direction in ("download", "upload"):
            try:
                environment_benchmark._url(provider.get(direction + "_url"))
            except environment_benchmark.BenchmarkError:
                _fail("station provider requires explicit credential-free HTTPS URLs")
            _integer(provider.get(direction + "_bytes"), 1024, 64 * 1024**2, direction + " bytes")
        _integer(provider.get("repetitions", 1), 1, 3, "provider repetitions")
    internal = config.get("internal")
    if not isinstance(internal, dict):
        _fail("station requires the existing explicit private TCP peer")
    try:
        address = ipaddress.ip_address(internal.get("host", ""))
    except ValueError:
        _fail("station peer must be a private IP address")
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        _fail("station peer must be a routable private IP address")
    if internal.get("protocol", "tcp") != "tcp":
        _fail("station peer protocol must be TCP")
    for key, default, low, high in (
        ("port", 5201, 1, 65535),
        ("duration_seconds", 10, 2, 300),
        ("repetitions", 1, 1, 3),
        ("parallel", 1, 1, 16),
    ):
        _integer(internal.get(key, default), low, high, key)
    disk = config.get("disk", {})
    if not isinstance(disk, dict) or set(disk) - set(DISK_PROFILE):
        _fail("station disk accepts only bounded fio profile fields, never a directory override")
    for key, low, high in (
        ("size_mib", 16, 16384),
        ("runtime_seconds", 2, 300),
        ("repetitions", 1, 10),
        ("iodepth", 1, 128),
    ):
        _integer(disk.get(key, DISK_PROFILE[key]), low, high, key)
    if _execution_timeout(config) > config.get("interval_seconds", 3600):
        _fail("station probe profile cannot complete within its sampling interval")


def _execution_timeout(config):
    disk = {**DISK_PROFILE, **config.get("disk", {})}
    internal = config.get("internal", {})
    return (
        300
        + 4 * (disk["runtime_seconds"] + 10) * disk["repetitions"]
        + 2 * (internal.get("duration_seconds", 10) + 10) * internal.get("repetitions", 1)
        + sum(250 * provider.get("repetitions", 1) for provider in config["providers"])
        + 120
    )


def _source_hashes():
    from . import benchmark_scenario_cloud

    return {
        "benchmark_station_measurements.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "environment_benchmark.py": hashlib.sha256(Path(environment_benchmark.__file__).read_bytes()).hexdigest(),
        "benchmark_scenario_cloud.py": hashlib.sha256(Path(benchmark_scenario_cloud.__file__).read_bytes()).hexdigest(),
    }


def _read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        _fail("station checkpoint must be an owner-only regular file")
    if info.st_size > 8 * 1024**2:
        _fail("station checkpoint exceeds its bound")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        _fail("station checkpoint must be an object")
    return value


def _private_root(root):
    root = Path(root).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = environment_benchmark._open_directory(root)
    info = os.fstat(fd)
    os.close(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        _fail("station measurement root must be owner-only")
    return root


def _station(resource, observed, config):
    actual = observed.get("flavor", {}).get("actual", {})
    ram = actual.get("ram_mib", actual.get("ram"))
    if actual.get("vcpus") != 4 or ram != 8192 or observed.get("status") != "ACTIVE":
        _fail("station must be the original ACTIVE 4vCPU/8192MiB VM")
    if resource.get("source") != "sdk" or observed.get("server_id") != resource.get("server_id"):
        _fail("station server identity differs from original SDK receipt")
    for key in ("server_id", "boot_volume_id", "data_volume_id", "filesystem_uuid"):
        try:
            uuid.UUID(resource[key])
        except (ValueError, TypeError, KeyError, AttributeError):
            _fail("station original identity is missing or invalid: " + key)
    if resource["boot_volume_id"] == resource["data_volume_id"] or resource.get("data_size_gib") != 500:
        _fail("station requires a dedicated original 500GiB Cinder data volume")
    project = config.get("project_id")
    if not project or resource.get("project_id") != project or observed.get("project_id") != project:
        _fail("station project differs from original receipt")
    volumes = observed.get("volumes", [])
    if {v.get("id") for v in volumes} != {resource["boot_volume_id"], resource["data_volume_id"]}:
        _fail("station original boot/data attachment set differs")
    data = [v for v in volumes if v.get("id") == resource["data_volume_id"]]
    if len(data) != 1 or data[0].get("size") != 500:
        _fail("station Cinder data size differs")
    mount = resource.get("data_mountpoint")
    directory = resource.get("data_directory")
    if not isinstance(mount, str) or not Path(mount).is_absolute() or directory != mount.rstrip("/") + "/measurements":
        _fail("station data measurement directory differs from original mount")
    return {
        "server_id": resource["server_id"],
        "project_id": project,
        "user_id": config.get("approval", {}).get("user_id", resource.get("user_id")),
        "boot_id": None,
        "initial_boot_id": resource.get("boot_id"),
        "boot_volume_id": resource["boot_volume_id"],
        "flavor": {"id": actual.get("id"), "vcpus": 4, "ram_mib": 8192},
        "data_volume_id": resource["data_volume_id"],
        "data_gib": 500,
        "filesystem_uuid": resource["filesystem_uuid"],
        "data_mountpoint": mount,
        "data_directory": directory,
    }


def _binding(station):
    return {k: station[k] for k in ("server_id", "project_id", "boot_volume_id", "data_volume_id", "filesystem_uuid")}


def _combine(providers):
    """Equal weight per provider, never bytes/time weighting or missing-as-zero."""
    combined = {}
    for direction in ("download", "upload"):
        means = {}
        for provider in providers:
            section = provider.get("directions", {}).get(direction, {})
            samples = section.get("repetitions", [])
            expected = provider.get("requested_repetitions")
            rates = [s.get("bandwidth_mib_per_second") for s in samples]
            if (
                section.get("status") == "success"
                and type(expected) is int
                and expected > 0
                and len(rates) == expected
                and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in rates)
            ):
                means[provider["provider_id"]] = sum(rates) / len(rates)
        complete = set(means) == _PROVIDERS
        combined[direction] = {
            "status": "success" if complete else "partial",
            "provider_means_mib_per_second": means,
            "two_provider_mean_mib_per_second": sum(means.values()) / 2 if complete else None,
            "method": "arithmetic-mean-of-two-provider-means",
        }
    return combined


def _guest_main(payload):
    # This function is sent verbatim; all guest probes use the shipped primitives.
    import base64
    import hashlib
    import json
    import os
    import runpy
    import signal
    import subprocess
    import time
    from pathlib import Path

    def interrupted(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    root = Path(payload["remote_root"])
    root.mkdir(mode=0o700)
    evidence = root / "evidence"
    evidence.mkdir(mode=0o700)
    source = base64.b64decode(payload["probe_source"])
    assert hashlib.sha256(source).hexdigest() == payload["source_hashes"]["environment_benchmark.py"]
    (root / "probe.py").write_bytes(source)
    probes = runpy.run_path(str(root / "probe.py"))
    station = payload["station"]
    started, clock = time.time(), time.monotonic()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    value = {
        "schema": "palimpsest.environment-result.v1",
        "status": "failed",
        "started_unix_seconds": started,
        "boot_id": boot,
        "source_hashes": payload["source_hashes"],
        "disk": {"status": "not-measured"},
        "network": {
            "status": "not-measured",
            "internal": {"status": "not-measured"},
            "external": {"status": "not-measured", "providers": []},
        },
        "issues": [],
        "remote_evidence_directory": str(evidence),
        "guest_logs": {},
        "complete": False,
    }

    def capture():
        logs = {}
        total = 0
        for path in sorted(evidence.iterdir()):
            if not path.is_file():
                continue
            data = path.read_bytes()
            assert len(data) <= 16 * 1024**2
            total += len(data)
            assert total <= 64 * 1024**2 and len(logs) < 512
            logs[path.name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "truncated": True}
        value["guest_logs"] = logs
        value["finished_unix_seconds"] = time.time()
        value["duration_seconds"] = time.monotonic() - clock
        temporary = root / "checkpoint.tmp"
        with temporary.open("w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(root / "checkpoint.json")

    def binding():
        mount = json.loads(
            subprocess.check_output(
                [
                    "findmnt",
                    "--json",
                    "--target",
                    station["data_directory"],
                    "--output",
                    "SOURCE,FSTYPE,TARGET,UUID,MAJ:MIN",
                ],
                text=True,
                timeout=20,
            )
        )["filesystems"][0]
        root_mount = json.loads(
            subprocess.check_output(
                ["findmnt", "--json", "--target", "/", "--output", "MAJ:MIN"], text=True, timeout=20
            )
        )["filesystems"][0]
        assert mount["target"] == station["data_mountpoint"] and mount["fstype"] == "ext4"
        assert mount["uuid"] == station["filesystem_uuid"] and mount["maj:min"] != root_mount["maj:min"]
        devices = json.loads(
            subprocess.check_output(
                [
                    "lsblk",
                    "--json",
                    "--inverse",
                    "--paths",
                    "--bytes",
                    "--output",
                    "NAME,SERIAL,SIZE,TYPE",
                    mount["source"],
                ],
                text=True,
                timeout=20,
            )
        )

        def flatten(nodes):
            for node in nodes:
                yield node
                yield from flatten(node.get("children", []))

        identity = station["data_volume_id"]
        serials = (identity, identity[:20], identity.replace("-", ""), identity.replace("-", "")[:20])
        assert any(
            d.get("serial") in serials and int(d["size"]) == 500 * 1024**3 and d["type"] == "disk"
            for d in flatten(devices["blockdevices"])
        )
        device = os.stat(station["data_directory"]).st_dev
        assert mount["maj:min"] == str(os.major(device)) + ":" + str(os.minor(device))
        return {"mount": mount, "block_devices": devices, "device_id": device}

    def run(name, config, runner):
        evidence_prefix = payload["attempt_id"] + "-" + name + "-"
        directory = root / name
        directory.mkdir(mode=0o700)
        try:
            result = runner(config, directory)
        except (probes["BenchmarkError"], OSError) as error:
            result = {"status": "failed", "issues": [name + ": " + type(error).__name__ + "; see raw evidence"]}
        finally:
            for path in directory.iterdir():
                if path.is_file() and path.suffix in (".stdout", ".stderr", ".log"):
                    path.rename(evidence / (evidence_prefix + path.name))
                elif path.is_file() and path.suffix == ".response":
                    # Response counters/timings remain in the full curl stdout
                    # log; payload bodies are not evidence logs and must not
                    # accumulate on the retained station's small boot volume.
                    path.unlink()

        def proofs(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if key == "proof" and isinstance(child, str):
                        item[key] = evidence_prefix + child
                    else:
                        proofs(child)
            elif isinstance(item, list):
                for child in item:
                    proofs(child)

        proofs(result)
        return result

    try:
        value["filesystem_binding"] = binding()
        capture()
        value["disk"] = run("disk", payload["disk"], probes["_disk"])
        if value["disk"].get("status") == "success":
            assert value["disk"]["device_id"] == value["filesystem_binding"]["device_id"]
        capture()
        value["network"]["internal"] = run("internal", payload["internal"], probes["_internal"])
        capture()
        for provider in payload["providers"]:
            provider_started = time.time()
            result = run(provider["provider_id"], provider, probes["_external"])
            result.update(
                provider_id=provider["provider_id"],
                requested_repetitions=provider["repetitions"],
                server_id=station["server_id"],
                cohort_id=payload["attempt_id"],
                started_unix_seconds=provider_started,
                finished_unix_seconds=time.time(),
            )
            value["network"]["external"]["providers"].append(result)
            capture()
        value["filesystem_binding_after"] = binding()
        assert value["filesystem_binding_after"] == value["filesystem_binding"]
        value["boot_id_after"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        assert boot == value["boot_id_after"]
        value["complete"] = True
    except BaseException as error:
        value["issues"].append("guest checkpoint failed: " + type(error).__name__)
    finally:
        capture()
    print(json.dumps(value, allow_nan=False))


def _response(response):
    if response.get("returncode") != 0:
        _fail("station guest command failed; raw guest checkpoint may be recoverable")
    try:
        result = json.loads(response["stdout"].strip().splitlines()[-1])
    except (ValueError, KeyError, IndexError, TypeError):
        _fail("station guest returned no valid checkpoint")
    if not isinstance(result, dict) or result.get("schema") != "palimpsest.environment-result.v1":
        _fail("station guest checkpoint schema differs")
    return result


def _execute(cloud, resource, payload, output):
    script = inspect.getsource(_guest_main) + "\n_guest_main(json.loads(__import__('sys').stdin.read()))\n"
    command = ["python3", "-c", "import json\n" + script]
    interrupted = False
    try:
        response = cloud.run_guest(
            resource,
            command,
            label="station-" + payload["attempt_id"],
            timeout=payload["execution_timeout_seconds"],
            input_bytes=json.dumps(payload).encode(),
        )
        interrupted = response.get("returncode") != 0
    except Exception:
        interrupted = True
    # ScenarioCloud deliberately withholds output for private stdin. Read the
    # durable checkpoint separately, whether execution returned or was interrupted.
    # This is log recovery, never a second provider request or probe invocation.
    result = _response(
        cloud.run_guest(
            resource,
            [
                "python3",
                "-c",
                "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())",
                payload["remote_root"] + "/checkpoint.json",
            ],
            label="station-recover-" + payload["attempt_id"],
            timeout=60,
        )
    )
    if interrupted:
        result["issues"].append("guest transport interrupted; recovered durable checkpoint without repeating traffic")
    atomic_write_json(output / "guest-result.json", result)
    try:
        cloud._collect_environment_logs(resource, result, output)
        result["raw_logs_complete"] = all(entry.get("collected") for entry in result.get("guest_logs", {}).values())
    except Exception as error:
        result["raw_logs_complete"] = False
        result["issues"].append("complete raw log recovery failed: " + type(error).__name__)
    atomic_write_json(output / "guest-result.json", result)
    return result


def _finish_environment(environment, payload):
    if environment.get("source_hashes") != payload["source_hashes"]:
        _fail("station guest source identity differs")
    if not environment.get("raw_logs_complete"):
        _fail("station complete raw log recovery is not proven")
    if (
        not environment.get("complete")
        or not environment.get("boot_id")
        or environment.get("boot_id_after") != environment["boot_id"]
    ):
        _fail("station guest boot/filesystem checkpoint is incomplete")
    providers = environment.get("network", {}).get("external", {}).get("providers", [])
    if (
        len(providers) != 2
        or {p.get("provider_id") for p in providers} != _PROVIDERS
        or any(
            p.get("server_id") != payload["station"]["server_id"] or p.get("cohort_id") != payload["attempt_id"]
            for p in providers
        )
    ):
        _fail("station provider VM/cohort identities differ")
    combined = _combine(providers)
    external = environment["network"]["external"]
    external.update(
        combined=combined, status="success" if all(v["status"] == "success" for v in combined.values()) else "partial"
    )
    internal = environment["network"]["internal"]
    environment["network"]["status"] = (
        "success" if external["status"] == internal.get("status") == "success" else "partial"
    )
    environment["status"] = (
        "success" if environment["disk"].get("status") == environment["network"]["status"] == "success" else "partial"
    )
    if environment["disk"].get("status") != "success":
        environment["issues"].append("station dedicated-data-disk measurement failed; context is not disk-qualified")
    for provider in providers:
        if provider.get("status") != "success":
            environment["issues"].append(
                provider["provider_id"] + ": incomplete external measurements; no substitute provider"
            )
    return environment


def _view(result, root, now, max_age, *, reused=True):
    result = copy.deepcopy(result)
    issues = []
    started = result.get("started_unix_seconds")
    finished = result.get("finished_unix_seconds")
    age = now - finished if type(finished) in (int, float) and math.isfinite(finished) else None
    if (
        age is None
        or age < 0
        or type(started) not in (int, float)
        or not math.isfinite(started)
        or started > now
        or finished < started
    ):
        issues.append("missing, future, or inconsistent station checkpoint time window")
    stale = age is None or age < 0 or age > max_age
    if stale:
        issues.append("station checkpoint is stale or its clock is invalid")
    if result.get("schema") != SCHEMA or result.get("source_hashes") != _source_hashes():
        issues.append("station checkpoint source/schema differs from the current runner")
    if result.get("probe_source_sha256") != result.get("source_hashes", {}).get("environment_benchmark.py"):
        issues.append("station probe source identity is missing or inconsistent")
    station = result.get("station", {})
    environment = result.get("environment", {})
    if result.get("status") in ("success", "partial"):
        providers = environment.get("network", {}).get("external", {}).get("providers", [])
        if (
            station.get("flavor", {}).get("vcpus") != 4
            or station.get("flavor", {}).get("ram_mib") != 8192
            or station.get("data_gib") != 500
            or not station.get("boot_id")
            or environment.get("server_id") != station.get("server_id")
            or environment.get("project_id") != station.get("project_id")
            or environment.get("boot_id") != station.get("boot_id")
            or environment.get("boot_id_after") != station.get("boot_id")
            or environment.get("source_hashes") != result.get("source_hashes")
            or len(providers) != 2
            or {p.get("provider_id") for p in providers} != _PROVIDERS
            or any(
                p.get("server_id") != station.get("server_id") or p.get("cohort_id") != result.get("attempt_id")
                for p in providers
            )
        ):
            issues.append("station checkpoint hardware, boot, source, or provider VM/cohort binding differs")
    usable = not issues and result.get("status") in ("success", "partial")
    result_path = result.get("result_path", str(Path(root).absolute() / "measurement-latest.json"))
    result["cache"] = {
        "reused": reused,
        "age_seconds": age,
        "stale": stale,
        "usable": usable,
        "result_path": result_path,
        "issues": issues,
    }
    result["sample_reference"] = {
        "schema": SCHEMA,
        "result_path": result_path,
        "attempt_id": result.get("attempt_id"),
        "station": result.get("station"),
        "started_unix_seconds": started,
        "finished_unix_seconds": finished,
        "probe_source_sha256": result.get("probe_source_sha256"),
        "status": result.get("status"),
        "stale": stale,
        "usable": usable,
    }
    return result


def latest_measurement(root, max_age_seconds=3600) -> dict:
    """Read the latest checkpoint only; stale/missing/unknown-source stays visible."""
    _integer(max_age_seconds, 1, 3600, "cache maximum age")
    root = Path(root).absolute()
    try:
        result = _read(root / "measurement-latest.json")
    except FileNotFoundError:
        result = {"schema": SCHEMA, "status": "failed", "issues": ["no station checkpoint exists"]}
    return _view(result, root, time.time(), max_age_seconds)


def run_measurement(cloud, resource: dict, config: dict, root, *, now=None) -> dict:
    """Serialize the entire cohort against all station probes and lifecycle effects."""
    from .benchmark_station_cloud import StationOperationBusy, station_operation

    try:
        with station_operation(cloud.output):
            return _run_measurement(cloud, resource, config, root, now=now)
    except StationOperationBusy:
        result = latest_measurement(root)
        result["scheduler"] = {
            "due": False,
            "reason": "another worker owns the station active operation",
            "next_eligible_unix_seconds": time.time() + 30,
        }
        return result


def _run_measurement(cloud, resource: dict, config: dict, root, *, now=None) -> dict:
    """Claim at most one slot and execute one finite same-VM measurement cohort."""
    measurement = config.get("measurement")
    validate_measurement(measurement)
    interval = measurement.get("interval_seconds", 3600)
    root = _private_root(root)
    wall = time.time() if now is None else now
    if type(wall) not in (int, float) or not math.isfinite(wall) or wall < 0:
        _fail("invalid station scheduler clock")
    mono = time.monotonic()
    lock_fd = os.open(root / "stationmeasurement.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            _fail("station scheduler lock must be owner-only")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result = latest_measurement(root)
            result["scheduler"] = {
                "due": False,
                "reason": "another worker owns the station slot",
                "next_eligible_unix_seconds": time.time() + 30,
            }
            return result
        state_path = root / "measurement-schedule.json"
        state = _read(state_path) if state_path.exists() else {}
        if state:
            prior_binding = state.get("binding", {})
            requested_binding = {
                key: resource.get(key) if key != "project_id" else config.get(key) for key in prior_binding
            }
            if prior_binding != requested_binding:
                _fail("measurement root is bound to a different original station")
            elapsed = wall - state["started_unix_seconds"]
            previous_mono = state["started_monotonic_seconds"]
            # Also hold across wall-clock jumps. After a monotonic reset, wait a
            # full interval of current uptime rather than trusting a UTC bucket.
            monotonic_elapsed = mono - previous_mono if mono >= previous_mono else mono
            hold_interval = max(interval, state["interval_seconds"])
            if elapsed < hold_interval or monotonic_elapsed < hold_interval:
                result = latest_measurement(root)
                result["scheduler"] = {
                    "due": False,
                    "reason": "attempt interval held, including failures",
                    "next_eligible_unix_seconds": wall
                    + max(0.0, hold_interval - elapsed, hold_interval - monotonic_elapsed),
                    "last_attempt_id": state["attempt_id"],
                }
                return result
        not_before = measurement.get("not_before_unix_seconds", 0)
        if wall < not_before:
            result = latest_measurement(root)
            result["scheduler"] = {
                "due": False,
                "reason": "first-sample external-traffic embargo",
                "next_eligible_unix_seconds": not_before,
            }
            return result
        # Local identity inspection is read-only; the traffic fence precedes guest execution.
        station = _station(resource, cloud.inspect(resource), config)
        attempt = uuid.uuid4().hex
        output = root / ("measurement-" + attempt)
        output.mkdir(mode=0o700)
        sources = _source_hashes()
        result = {
            "schema": SCHEMA,
            "attempt_id": attempt,
            "job_id": config.get("job_id"),
            "status": "failed",
            "station": station,
            "interval_seconds": interval,
            "started_unix_seconds": wall,
            "finished_unix_seconds": None,
            "source_hashes": sources,
            "probe_source_sha256": sources["environment_benchmark.py"],
            "result_path": str(output / "result.json"),
            "issues": [],
            "environment": {
                "schema": "palimpsest.environment-result.v1",
                "status": "not-measured",
                "disk": {"status": "not-measured"},
                "network": {},
            },
        }
        state = {
            "attempt_id": attempt,
            "started_unix_seconds": wall,
            "started_monotonic_seconds": mono,
            "clock_origin": wall - mono,
            "interval_seconds": interval,
            "binding": _binding(station),
        }
        atomic_write_json(output / "result.json", result)
        atomic_write_json(state_path, state)
        source_directory = output / "sources"
        source_directory.mkdir(mode=0o700)
        from . import benchmark_scenario_cloud

        for name, path in (
            ("benchmark_station_measurements.py", __file__),
            ("environment_benchmark.py", environment_benchmark.__file__),
            ("benchmark_scenario_cloud.py", benchmark_scenario_cloud.__file__),
        ):
            data = Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest() != sources[name]:
                _fail("station source changed before guest execution; attempt interval remains held")
            environment_benchmark._private_write(source_directory / name, data.decode())
        providers = [dict(p, repetitions=p.get("repetitions", 1)) for p in measurement["providers"]]
        internal = dict(measurement["internal"], repetitions=measurement["internal"].get("repetitions", 1))
        payload = {
            "attempt_id": attempt,
            "station": station,
            "source_hashes": sources,
            "remote_root": "/tmp/palimpsest-scenario-environment-" + attempt,
            "probe_source": base64.b64encode(Path(environment_benchmark.__file__).read_bytes()).decode(),
            "providers": providers,
            "internal": internal,
            "disk": dict(DISK_PROFILE, **measurement.get("disk", {}), directory=station["data_directory"]),
            "execution_timeout_seconds": _execution_timeout(measurement),
        }
        atomic_write_json(output / "measurement-config.json", {k: v for k, v in payload.items() if k != "probe_source"})
        stage = "guest execution and bounded log recovery"
        try:
            environment = _execute(cloud, resource, payload, output)
            result["environment"] = environment
            result["raw_log_manifest"] = environment.get("guest_logs", {})
            stage = "guest source, boot and provider proof validation"
            _finish_environment(environment, payload)
            stage = "original station and source reinspection"
            after = _station(resource, cloud.inspect(resource), config)
            if _binding(after) != _binding(station) or _source_hashes() != sources:
                _fail("station/source binding changed during measurement")
            station["boot_id"] = environment["boot_id"]
            environment.update(
                server_id=station["server_id"],
                project_id=station["project_id"],
                flavor={"actual": station["flavor"]},
                probe_source_sha256=result["probe_source_sha256"],
            )
            result["disk_profile"] = payload["disk"]
            result["status"] = environment["status"]
            result["issues"] = list(environment["issues"])
            result["guest_time_window"] = {
                "started_unix_seconds": environment["started_unix_seconds"],
                "finished_unix_seconds": environment["finished_unix_seconds"],
            }
        except BaseException as error:
            result["issues"].append("station attempt failed during " + stage + ": " + type(error).__name__)
            if not isinstance(error, Exception):
                result["finished_unix_seconds"] = time.time()
                atomic_write_json(output / "result.json", result)
                atomic_write_json(root / "measurement-latest.json", result)
                raise
        result["finished_unix_seconds"] = time.time()
        result["duration_seconds"] = time.monotonic() - mono
        if abs((result["finished_unix_seconds"] - wall) - result["duration_seconds"]) > 15:
            result["status"] = "failed"
            result["issues"].append("controller wall/monotonic disagreement invalidates this checkpoint")
        atomic_write_json(output / "result.json", result)
        atomic_write_json(root / "measurement-latest.json", result)
        for name, content in render_measurement(result).items():
            environment_benchmark._private_write(output / name, content)
        return _view(result, root, time.time(), interval, reused=False)


def render_measurement(result: dict) -> dict[str, str]:
    """Use native reports with separate provider rows and a separate issue ledger."""
    environment = copy.deepcopy(result.get("environment") or {})
    environment.setdefault("schema", "palimpsest.environment-result.v1")
    environment.setdefault("status", "failed")
    environment.setdefault("disk", {"status": "not-measured"})
    environment.setdefault("network", {"status": "not-measured"})
    environment["network"].setdefault("internal", {"status": "not-measured"})
    environment["network"].setdefault("external", {"status": "not-measured"})
    external = environment.get("network", {}).get("external", {})
    records = []
    providers = external.get("providers", [])
    for index, provider in enumerate(providers or [{}]):
        leaf = copy.deepcopy(environment)
        leaf.update(job_id=str(result.get("attempt_id", "station")) + "/" + provider.get("provider_id", "checkpoint"))
        leaf.setdefault("network", {})["external"] = provider
        if index:
            leaf["disk"] = {"status": "not-measured", "issues": ["disk measured once in this shared station cohort"]}
            leaf["network"]["internal"] = {
                "status": "not-measured",
                "issues": ["private TCP measured once in this shared cohort"],
            }
        leaf["issues"] = list(leaf.get("issues", [])) + list(result.get("issues", []))
        records.append(leaf)
    report = render_report(records)
    lines = [
        "## Two-provider arithmetic mean",
        "",
        "Provider means receive equal weight; missing directions remain N/A.",
        "",
        "| Direction | MiB/s |",
        "| --- | ---: |",
    ]
    from html import escape

    rows = []
    for direction in ("download", "upload"):
        value = external.get("combined", {}).get(direction, {}).get("two_provider_mean_mib_per_second")
        text = f"{value:.6f}" if type(value) in (int, float) and math.isfinite(value) else "N/A"
        lines.append(f"| {direction} | {text} |")
        rows.append("<tr><td>" + escape(direction) + "</td><td>" + text + "</td></tr>")
    station = result.get("station", {})
    disk = result.get("disk_profile", DISK_PROFILE)
    scope = (
        f"Station {station.get('server_id', 'N/A')}; dedicated Cinder data {station.get('data_volume_id', 'N/A')} "
        f"({station.get('data_gib', 'N/A')}GiB), {station.get('flavor', {}).get('vcpus', 'N/A')}vCPU/"
        f"{station.get('flavor', {}).get('ram_mib', 'N/A')}MiB. "
        f"Window {result.get('started_unix_seconds', 'N/A')}–{result.get('finished_unix_seconds', 'N/A')}. "
        f"Configured fio dataset {disk.get('size_mib', 1024)}MiB on the dedicated data filesystem (not a full-volume fill), "
        f"runtime {disk.get('runtime_seconds', 20)}s/profile/repetition, {disk.get('repetitions', 5)} repetitions, "
        f"iodepth {disk.get('iodepth', 16)}, Linux O_DIRECT. "
        "These measurements do not describe a separate workload VM's boot disk or hardware."
    )
    html_section = (
        "<section><h2>Persistent station cohort</h2><p>"
        + escape(scope)
        + "</p><h2>Two-provider arithmetic mean (MiB/s)</h2><table><thead><tr><th>Direction</th><th>Mean</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table><p>Equal provider weights; incomplete directions are N/A.</p></section>"
    )
    return {
        "report.md": report["markdown"] + "\n" + scope + "\n\n" + "\n".join(lines) + "\n",
        "overview.html": report["html"].replace("</main>", html_section + "</main>"),
        "issues.md": report["issues_markdown"],
    }
