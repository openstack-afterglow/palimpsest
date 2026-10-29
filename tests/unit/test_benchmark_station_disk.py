"""Offline disk checkpoint binding, evidence, freshness and no-replay boundaries."""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import benchmark_station_disk as disk
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.state import atomic_write_json

SERVER = "00000000-0000-4000-8000-000000000001"
BOOT_VOLUME = "00000000-0000-4000-8000-000000000002"
DATA_VOLUME = "00000000-0000-4000-8000-000000000003"
FILESYSTEM = "00000000-0000-4000-8000-000000000004"
BOOT = "00000000-0000-4000-8000-000000000005"


def leaf(manifest):
    return {
        "status": "success",
        "protocol": "fio-direct",
        "direct": True,
        "ioengine": "libaio",
        "size_bytes": 1024**3,
        "runtime_seconds": 20,
        "requested_repetitions": 5,
        "iodepth": 16,
        "device_id": 42,
        "profiles": {
            name: {
                "status": "success",
                "repetitions": [{"proof": next(iter(manifest)), "bandwidth_mib_per_second": 10.0} for _ in range(5)],
            }
            for name in disk._PROFILES
        },
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=7200.0)
    monkeypatch.setattr(disk.time, "time", lambda: clock.now)
    root = tmp_path / "disk-measurements"
    resource = {
        "source": "sdk",
        "server_id": SERVER,
        "project_id": "project-one",
        "user_id": "human-one",
        "boot_volume_id": BOOT_VOLUME,
        "boot_id": BOOT,
        "data_volume_id": DATA_VOLUME,
        "filesystem_uuid": FILESYSTEM,
        "data_size_gib": 500,
        "data_mountpoint": "/mnt/palimpsest-benchmark",
        "data_directory": "/mnt/palimpsest-benchmark/measurements",
    }
    observed = {
        **resource,
        "status": "ACTIVE",
        "flavor": {"actual": {"vcpus": 4, "ram": 8192}},
        "volumes": [{"id": BOOT_VOLUME, "size": 20}, {"id": DATA_VOLUME, "size": 500}],
    }
    calls = []

    class Cloud:
        def inspect(self, item):
            assert item == resource
            return copy.deepcopy(observed)

    station = SimpleNamespace(
        output=tmp_path,
        cloud=Cloud(),
        resource=lambda: resource,
        config={"project_id": "project-one", "approval": {"user_id": "human-one"}},
    )

    def execute(cloud, item, payload, output):
        assert (output / "result.json").is_file()
        assert json.loads((root / "disk-attempt.json").read_text())["attempt_id"] == payload["attempt_id"]
        assert "probe_source" not in json.loads((root / "disk-attempt.json").read_text())
        assert payload["disk"] == dict(disk.DISK_PROFILE, directory=resource["data_directory"])
        assert set(payload) == {
            "attempt_id",
            "station",
            "remote_root",
            "disk",
            "probe_source",
            "probe_source_sha256",
            "driver_source_sha256",
        }
        calls.append(payload)
        raw = output / "raw"
        raw.mkdir(mode=0o700, exist_ok=True)
        name = payload["attempt_id"] + "-disk-fio.stdout"
        path = raw / name
        path.write_bytes(b"complete fio raw log\n")
        path.chmod(0o600)
        manifest = {
            name: {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "collected": True,
                "local_file": str(path),
            }
        }
        return {
            "complete": True,
            "raw_logs_complete": True,
            "disk": leaf(manifest),
            "guest_logs": manifest,
            "filesystem_binding": {"device_id": 42},
            "filesystem_binding_after": {"device_id": 42},
            "started_unix_seconds": clock.now,
            "finished_unix_seconds": clock.now,
            "issues": [],
        }

    monkeypatch.setattr(disk, "_execute", execute)
    return SimpleNamespace(
        root=root, clock=clock, resource=resource, observed=observed, station=station, calls=calls, execute=execute
    )


def test_fixed_profile_and_receipt_precede_guest_and_sources_are_saved(harness):
    result = disk.run_disk_sample(harness.station, harness.root)
    assert result["schema"] == "palimpsest.station-disk.v1"
    assert result["status"] == "success"
    assert result["cache"]["usable"] is True
    assert result["cache"]["reused"] is False
    assert result["raw_logs_complete"] is True
    assert result["station"]["boot_id"] == BOOT
    assert result["station"]["flavor"] == {"vcpus": 4, "ram_mib": 8192}
    assert result["station"]["data_gib"] == 500
    assert result["station"]["user_id"] == "human-one"
    assert "network" not in result
    output = Path(result["result_path"]).parent
    for name, key in (
        ("environment_benchmark.py", "probe_source_sha256"),
        ("benchmark_station_disk.py", "driver_source_sha256"),
    ):
        assert hashlib.sha256((output / "sources" / name).read_bytes()).hexdigest() == result[key]
    assert (harness.root / "disk-latest.json").is_file()


def test_completed_checkpoint_is_reused_not_reprobed(harness):
    first = disk.run_disk_sample(harness.station, harness.root)
    harness.clock.now += 10
    second = disk.run_disk_sample(harness.station, harness.root)
    assert second["attempt_id"] == first["attempt_id"]
    assert second["cache"]["reused"] is True
    assert len(harness.calls) == 1


def test_latest_is_read_only_and_stale_is_unusable(harness):
    disk.run_disk_sample(harness.station, harness.root)
    before = {str(path): path.read_bytes() for path in harness.root.rglob("*") if path.is_file()}
    harness.clock.now += 3601
    result = disk.latest_disk_sample(harness.root)
    assert result["cache"]["stale"] is True
    assert result["cache"]["usable"] is False
    assert {str(path): path.read_bytes() for path in harness.root.rglob("*") if path.is_file()} == before
    assert len(harness.calls) == 1


def test_missing_latest_does_not_create_root(tmp_path):
    root = tmp_path / "missing"
    assert disk.latest_disk_sample(root)["cache"]["usable"] is False
    assert not root.exists()


@pytest.mark.parametrize("maximum", [0, 3601, True, 1.5])
def test_latest_rejects_invalid_age(maximum, tmp_path):
    with pytest.raises(ArtifactValidationError):
        disk.latest_disk_sample(tmp_path, maximum)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "SHUTOFF"},
        {"project_id": "other"},
        {"server_id": BOOT_VOLUME},
        {"flavor": {"actual": {"vcpus": 2, "ram": 8192}}},
        {"flavor": {"actual": {"vcpus": 4, "ram": 4096}}},
        {"volumes": [{"id": BOOT_VOLUME, "size": 20}, {"id": DATA_VOLUME, "size": 499}]},
        {"volumes": [{"id": BOOT_VOLUME, "size": 21}, {"id": DATA_VOLUME, "size": 500}]},
        {"volumes": [{"id": BOOT_VOLUME, "size": 20}]},
    ],
)
def test_api_identity_failures_prevent_probe(harness, change):
    harness.observed.update(change)
    with pytest.raises(ArtifactValidationError):
        disk.run_disk_sample(harness.station, harness.root)
    assert not harness.calls


@pytest.mark.parametrize(
    "change",
    [
        {"source": "api"},
        {"boot_id": None},
        {"data_size_gib": 499},
        {"filesystem_uuid": "not-uuid"},
        {"data_directory": "/"},
        {"data_mountpoint": "/mnt/../foreign"},
        {"boot_volume_id": DATA_VOLUME},
    ],
)
def test_original_resource_failures_prevent_probe(harness, change):
    harness.resource.update(change)
    with pytest.raises(ArtifactValidationError):
        disk.run_disk_sample(harness.station, harness.root)
    assert not harness.calls


def test_incomplete_attempt_only_recovers_never_reexecutes(harness, monkeypatch):
    def interrupted(*args):
        harness.calls.append("one execution")
        raise OSError("private transport details must not be copied")

    monkeypatch.setattr(disk, "_execute", interrupted)
    first = disk.run_disk_sample(harness.station, harness.root)
    assert first["status"] == "failed"
    assert not (harness.root / "disk-latest.json").exists()
    monkeypatch.setattr(disk, "_recover", harness.execute)
    second = disk.run_disk_sample(harness.station, harness.root)
    assert second["attempt_id"] == first["attempt_id"]
    assert second["status"] == "success"
    assert harness.calls[0] == "one execution"
    assert len(harness.calls) == 2
    assert "private transport" not in json.dumps(first)


def test_recovery_refuses_different_original_station(harness, monkeypatch):
    monkeypatch.setattr(disk, "_execute", lambda *args: (_ for _ in ()).throw(OSError()))
    disk.run_disk_sample(harness.station, harness.root)
    harness.resource["filesystem_uuid"] = SERVER
    with pytest.raises(ArtifactValidationError, match="different original station"):
        disk.run_disk_sample(harness.station, harness.root)


def test_recovery_refuses_changed_driver_source(harness, monkeypatch):
    monkeypatch.setattr(disk, "_execute", lambda *args: (_ for _ in ()).throw(OSError()))
    disk.run_disk_sample(harness.station, harness.root)
    sources = disk._sources()
    monkeypatch.setattr(disk, "_sources", lambda: dict(sources, driver_source_sha256="0" * 64))
    with pytest.raises(ArtifactValidationError, match="source differs"):
        disk.run_disk_sample(harness.station, harness.root)


@pytest.mark.parametrize("mutation", ["source", "time", "profile", "manifest", "raw-bytes", "raw-symlink"])
def test_cache_revalidates_sources_profile_time_and_exact_log_bytes(harness, mutation):
    disk.run_disk_sample(harness.station, harness.root)
    path = harness.root / "disk-latest.json"
    result = json.loads(path.read_text())
    if mutation == "source":
        result["driver_source_sha256"] = "0" * 64
    elif mutation == "time":
        result["finished_unix_seconds"] = harness.clock.now + 1
    elif mutation == "profile":
        result["disk"]["iodepth"] = 8
    elif mutation == "manifest":
        next(iter(result["raw_log_manifest"].values()))["collected"] = False
    else:
        raw = Path(next(iter(result["raw_log_manifest"].values()))["local_file"])
        if mutation == "raw-bytes":
            raw.write_bytes(b"different")
        else:
            saved = raw.with_suffix(".saved")
            raw.rename(saved)
            raw.symlink_to(saved)
    atomic_write_json(path, result)
    assert disk.latest_disk_sample(harness.root)["cache"]["usable"] is False


def test_checkpoint_symlink_is_rejected(harness):
    result = disk.run_disk_sample(harness.station, harness.root)
    latest = harness.root / "disk-latest.json"
    latest.unlink()
    latest.symlink_to(result["result_path"])
    with pytest.raises(OSError):
        disk.latest_disk_sample(harness.root)


def test_guest_program_has_only_disk_probe_call_and_read_only_binding_commands():
    source = inspect.getsource(disk._guest_main)
    tree = ast.parse(source)
    called_probe_keys = [
        node.func.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Subscript)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "probes"
        and isinstance(node.func.slice, ast.Constant)
    ]
    assert called_probe_keys.count("_disk") == 1
    assert set(called_probe_keys) == {"_disk", "_open_directory"}
    assert "curl" not in source and "iperf" not in source and "sudo" not in source
    assert "os.geteuid() != 0" in source
    assert '"findmnt"' in source and '"lsblk"' in source
    assert 'station["boot_volume_id"]' in source
    assert 'station["filesystem_uuid"]' in source
    assert "os.cpu_count() == 4" in source


def test_transport_failure_recovers_json_without_repeating_probe(tmp_path):
    calls = []

    class Cloud:
        def run_guest(self, resource, argv, **kwargs):
            calls.append((argv, kwargs))
            if kwargs.get("input_bytes"):
                raise OSError("lost connection")
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "schema": "palimpsest.station-disk-guest.v1",
                        "attempt_id": "attempt",
                        "station": {},
                        "probe_source_sha256": "probe",
                        "driver_source_sha256": "driver",
                        "guest_logs": {},
                        "complete": False,
                    }
                ),
                "stderr": "",
            }

        def _collect_environment_logs(self, resource, result, output):
            assert not result["guest_logs"]

    payload = {
        "attempt_id": "attempt",
        "station": {},
        "probe_source_sha256": "probe",
        "driver_source_sha256": "driver",
        "remote_root": "/tmp/palimpsest-scenario-environment-attempt",
    }
    result = disk._execute(Cloud(), {}, payload, tmp_path)
    assert result["raw_logs_complete"] is False
    assert len(calls) == 2
    assert sum(bool(kwargs.get("input_bytes")) for _, kwargs in calls) == 1
    assert calls[0][1]["label"] != calls[1][1]["label"]
    assert "_disk" not in calls[1][0][2]
    assert (tmp_path / "execution.json").is_file()
    assert (tmp_path / "guest-result.json").is_file()


def test_log_recovery_transport_uses_unique_labels():
    labels = []
    cloud = SimpleNamespace(run_guest=lambda resource, argv, **kwargs: labels.append(kwargs["label"]))
    for _ in range(2):
        disk._RecoveryTransport(cloud).run_guest({}, [], label="environment-log-same-0", timeout=60)
    assert labels[0] != labels[1]


def test_lock_symlink_rejected(harness):
    harness.root.mkdir(mode=0o700)
    target = harness.root / "foreign"
    target.write_text("")
    os.chmod(target, 0o600)
    (harness.root / "station-disk.lock").symlink_to(target)
    with pytest.raises(OSError):
        disk.run_disk_sample(harness.station, harness.root)
    assert not harness.calls


def test_explicit_stale_disk_call_starts_new_attempt_without_touching_network(harness):
    network = harness.root.parent / "measurement-latest.json"
    network.write_bytes(b"original immutable partial hourly cohort")
    first = disk.run_disk_sample(harness.station, harness.root)
    harness.clock.now += 3601
    second = disk.run_disk_sample(harness.station, harness.root)
    assert first["attempt_id"] != second["attempt_id"]
    assert len(harness.calls) == 2
    assert second["started_unix_seconds"] > first["finished_unix_seconds"]
    assert network.read_bytes() == b"original immutable partial hourly cohort"


def test_completed_result_publication_recovers_without_probe_replay(harness):
    first = disk.run_disk_sample(harness.station, harness.root)
    (harness.root / "disk-latest.json").unlink()
    second = disk.run_disk_sample(harness.station, harness.root)
    assert second["attempt_id"] == first["attempt_id"]
    assert len(harness.calls) == 1
    assert disk.latest_disk_sample(harness.root)["cache"]["usable"] is True


@pytest.mark.parametrize("pointer", ["older", "missing", "different-safe-binding"])
def test_completed_pending_result_republishes_over_previous_latest_without_new_probe(harness, monkeypatch, pointer):
    first = disk.run_disk_sample(harness.station, harness.root)
    historical_path = Path(first["result_path"])
    historical_bytes = historical_path.read_bytes()
    harness.clock.now += 3601
    write_json = disk.atomic_write_json

    def stop_before_latest(path, value):
        if Path(path) == harness.root / "disk-latest.json":
            raise KeyboardInterrupt("controller stopped after committing completed result")
        write_json(path, value)

    monkeypatch.setattr(disk, "atomic_write_json", stop_before_latest)
    with pytest.raises(KeyboardInterrupt):
        disk.run_disk_sample(harness.station, harness.root)
    monkeypatch.setattr(disk, "atomic_write_json", write_json)
    latest = harness.root / "disk-latest.json"
    pending = json.loads((harness.root / "disk-attempt.json").read_text())
    committed_path = harness.root / ("disk-" + pending["attempt_id"]) / "result.json"
    committed_bytes = committed_path.read_bytes()
    committed = json.loads(committed_bytes)
    assert committed["completed"] is True and committed["status"] == "success"
    assert json.loads(latest.read_text())["attempt_id"] == first["attempt_id"]
    if pointer == "missing":
        latest.unlink()
    elif pointer == "different-safe-binding":
        previous = json.loads(latest.read_text())
        previous["station"]["filesystem_uuid"] = SERVER
        write_json(latest, previous)
    calls_before = len(harness.calls)
    recovered = disk.run_disk_sample(harness.station, harness.root)
    assert len(harness.calls) == calls_before == 2
    assert recovered["attempt_id"] == committed["attempt_id"]
    assert recovered["cache"]["reused"] is recovered["cache"]["usable"] is True
    assert recovered["finished_unix_seconds"] == committed["finished_unix_seconds"]
    assert recovered["started_unix_seconds"] == committed["started_unix_seconds"]
    assert json.loads(latest.read_text()) == committed
    assert disk.latest_disk_sample(harness.root)["attempt_id"] == committed["attempt_id"]
    assert historical_path.read_bytes() == historical_bytes
    assert committed_path.read_bytes() == committed_bytes


def test_completed_pending_recovery_never_regresses_newer_valid_success(harness):
    pending = disk.run_disk_sample(harness.station, harness.root)
    latest_path = harness.root / "disk-latest.json"
    newer = json.loads(latest_path.read_text())
    harness.clock.now += 10
    newer.update(attempt_id="f" * 32, started_unix_seconds=harness.clock.now, finished_unix_seconds=harness.clock.now)
    newer["guest_time_window"] = {
        "started_unix_seconds": harness.clock.now,
        "finished_unix_seconds": harness.clock.now,
    }
    output = harness.root / ("disk-" + newer["attempt_id"])
    output.mkdir(mode=0o700)
    newer["result_path"] = str(output / "result.json")
    atomic_write_json(output / "result.json", newer)
    atomic_write_json(latest_path, newer)
    assert disk.latest_disk_sample(harness.root)["cache"]["usable"] is True
    before = latest_path.read_bytes()
    recovered = disk.run_disk_sample(harness.station, harness.root)
    assert recovered["attempt_id"] == pending["attempt_id"]
    assert len(harness.calls) == 1
    assert latest_path.read_bytes() == before
    assert disk.latest_disk_sample(harness.root)["attempt_id"] == newer["attempt_id"]


@pytest.mark.parametrize("mutation", ["future-time", "source", "raw-log", "failed-cohort"])
def test_completed_pending_invalid_evidence_never_republishes_or_reprobes(harness, mutation):
    result = disk.run_disk_sample(harness.station, harness.root)
    result_path = Path(result["result_path"])
    committed = json.loads(result_path.read_text())
    if mutation == "future-time":
        committed["finished_unix_seconds"] += 10
    elif mutation == "source":
        committed["driver_source_sha256"] = "0" * 64
    elif mutation == "raw-log":
        Path(next(iter(committed["raw_log_manifest"].values()))["local_file"]).write_bytes(b"changed")
    else:
        committed["status"] = "failed"
    atomic_write_json(result_path, committed)
    before = result_path.read_bytes()
    latest = harness.root / "disk-latest.json"
    latest.unlink()
    recovered = disk.run_disk_sample(harness.station, harness.root)
    assert recovered["cache"]["usable"] is False
    assert not latest.exists()
    assert len(harness.calls) == 1
    assert result_path.read_bytes() == before


def test_delayed_recovery_keeps_actual_probe_time_and_does_not_replay(harness, monkeypatch):
    saved = {}
    probe_finished = harness.clock.now

    def lost_after_probe(*args):
        saved["guest"] = harness.execute(*args)
        raise OSError("transport lost after guest completion")

    monkeypatch.setattr(disk, "_execute", lost_after_probe)
    first = disk.run_disk_sample(harness.station, harness.root)
    assert first["completed"] is False
    assert len(harness.calls) == 1
    harness.clock.now += 3601
    recoveries = []

    def recover_only(*args):
        recoveries.append(True)
        return copy.deepcopy(saved["guest"])

    monkeypatch.setattr(disk, "_recover", recover_only)
    recovered = disk.run_disk_sample(harness.station, harness.root)
    assert recovered["status"] == "success"
    assert recovered["finished_unix_seconds"] == probe_finished
    assert recovered["started_unix_seconds"] == probe_finished
    assert recovered["recovered_unix_seconds"] == harness.clock.now
    assert recovered["cache"]["age_seconds"] == 3601
    assert recovered["cache"]["stale"] is True
    assert recovered["cache"]["usable"] is False
    assert recovered["sample_reference"]["finished_unix_seconds"] == probe_finished
    assert disk.latest_disk_sample(harness.root)["cache"]["usable"] is False
    assert len(harness.calls) == 1
    assert len(recoveries) == 1


def test_cache_rejects_recovery_time_masquerading_as_probe_time(harness):
    disk.run_disk_sample(harness.station, harness.root)
    harness.clock.now += 3601
    path = harness.root / "disk-latest.json"
    result = json.loads(path.read_text())
    result["finished_unix_seconds"] = harness.clock.now
    result["recovered_unix_seconds"] = harness.clock.now
    atomic_write_json(path, result)
    viewed = disk.latest_disk_sample(harness.root)
    assert viewed["cache"]["stale"] is True
    assert viewed["cache"]["usable"] is False


def test_one_station_fence_blocks_other_disk_roots_sampling_and_cleanup(harness, monkeypatch):
    import threading

    from palimpsest_local import benchmark_station_cloud as lifecycle
    from palimpsest_local import benchmark_station_measurements as hourly

    entered, release = threading.Event(), threading.Event()
    failures = []

    def held_execute(*args):
        entered.set()
        assert release.wait(5)
        return harness.execute(*args)

    def owner():
        try:
            disk.run_disk_sample(harness.station, harness.root)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(disk, "_execute", held_execute)
    worker = threading.Thread(target=owner)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(lifecycle.StationOperationBusy):
            disk.run_disk_sample(harness.station, harness.root.parent / "another-disk-output")
        assert not (harness.root.parent / "another-disk-output").exists()
        view = hourly.run_measurement(
            SimpleNamespace(output=harness.station.output), {}, {}, harness.root.parent / "hourly"
        )
        assert view["scheduler"]["due"] is False
        lifecycle_instance = object.__new__(lifecycle.StationCloud)
        lifecycle_instance.output = harness.station.output
        with pytest.raises(lifecycle.StationOperationBusy):
            lifecycle_instance.cleanup()
        with pytest.raises(lifecycle.StationOperationBusy):
            lifecycle_instance.repair_access()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not failures
    assert len(harness.calls) == 1


def test_station_active_lock_rejects_symlink_without_following_it(harness):
    from palimpsest_local.benchmark_station_cloud import station_operation

    target = harness.station.output / "unrelated-file"
    target.write_text("untouched")
    (harness.station.output / "station-active-operation.lock").symlink_to(target)
    with pytest.raises(OSError):
        with station_operation(harness.station.output):
            pytest.fail("symlink must never acquire station ownership")
    assert target.read_text() == "untouched"
