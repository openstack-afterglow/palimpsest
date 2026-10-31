"""Unit-only scheduler, source/binding, report and recovery boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import benchmark_station_measurements as station
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.state import atomic_write_json

SERVER = "00000000-0000-4000-8000-000000000001"
BOOT_VOLUME = "00000000-0000-4000-8000-000000000002"
DATA_VOLUME = "00000000-0000-4000-8000-000000000003"
FILESYSTEM = "00000000-0000-4000-8000-000000000004"
BOOT = "00000000-0000-4000-8000-000000000005"
PROJECT = "project-one"


def measurement():
    return {
        "providers": [
            {
                "provider_id": "cloudflare",
                "download_url": "https://speed.cloudflare.com/__down?bytes=10485760",
                "upload_url": "https://speed.cloudflare.com/__up",
                "download_bytes": 10485760,
                "upload_bytes": 10485760,
            },
            {
                "provider_id": "ovh",
                "download_url": "https://proof.ovh.net/files/10Mb.dat",
                "upload_url": "https://proof.ovh.net/speedtest/upload",
                "download_bytes": 10485760,
                "upload_bytes": 10485760,
            },
        ],
        "internal": {"host": "192.168.0.233", "duration_seconds": 10, "repetitions": 1},
    }


def provider(identity, rates=(10.0,), *, upload_rates=None):
    return {
        "provider_id": identity,
        "status": "success",
        "requested_repetitions": len(rates),
        "directions": {
            direction: {
                "status": "success",
                "repetitions": [{"bandwidth_mib_per_second": rate, "bytes": 10485760} for rate in samples],
            }
            for direction, samples in (("download", rates), ("upload", upload_rates or rates))
        },
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    clock = SimpleNamespace(wall=7200.0, mono=7200.0)
    monkeypatch.setattr(station.time, "time", lambda: clock.wall)
    monkeypatch.setattr(station.time, "monotonic", lambda: clock.mono)
    resource = {
        "source": "sdk",
        "server_id": SERVER,
        "boot_volume_id": BOOT_VOLUME,
        "boot_id": BOOT,
        "data_volume_id": DATA_VOLUME,
        "filesystem_uuid": FILESYSTEM,
        "data_size_gib": 500,
        "data_mountpoint": "/mnt/palimpsest-benchmark",
        "data_directory": "/mnt/palimpsest-benchmark/measurements",
        "project_id": PROJECT,
    }
    config = {"job_id": "station-test", "project_id": PROJECT, "measurement": measurement()}
    observed = {
        **resource,
        "status": "ACTIVE",
        "flavor": {"actual": {"id": "4x8", "vcpus": 4, "ram": 8192}},
        "volumes": [{"id": BOOT_VOLUME, "size": 20}, {"id": DATA_VOLUME, "size": 500}],
    }
    calls = []

    class Cloud:
        output = tmp_path

        def inspect(self, item):
            assert item == resource
            return copy.deepcopy(observed)

    def execute(cloud, item, payload, output):
        schedule = json.loads((tmp_path / "measurement-schedule.json").read_text())
        assert schedule["attempt_id"] == payload["attempt_id"]
        assert schedule["started_unix_seconds"] == clock.wall
        assert (output / "result.json").exists()
        calls.append(payload)
        providers = [provider("cloudflare"), provider("ovh", (30.0,))]
        for p in providers:
            p.update(
                server_id=SERVER,
                cohort_id=payload["attempt_id"],
                started_unix_seconds=clock.wall,
                finished_unix_seconds=clock.wall,
            )
        return {
            "schema": "palimpsest.environment-result.v1",
            "source_hashes": payload["source_hashes"],
            "complete": True,
            "raw_logs_complete": True,
            "boot_id": BOOT,
            "boot_id_after": BOOT,
            "started_unix_seconds": clock.wall,
            "finished_unix_seconds": clock.wall,
            "disk": {"status": "success", "device_id": 42},
            "network": {"internal": {"status": "success", "directions": {}}, "external": {"providers": providers}},
            "issues": [],
            "guest_logs": {},
        }

    monkeypatch.setattr(station, "_execute", execute)
    return SimpleNamespace(
        clock=clock,
        resource=resource,
        config=config,
        observed=observed,
        cloud=Cloud(),
        root=tmp_path,
        calls=calls,
        execute=execute,
    )


def run(harness):
    return station.run_measurement(harness.cloud, harness.resource, harness.config, harness.root)


@pytest.mark.parametrize("interval", [1800, 3600])
def test_bounded_intervals_and_default_profile(interval):
    config = measurement()
    config["interval_seconds"] = interval
    station.validate_measurement(config)
    assert station.DISK_PROFILE == {"size_mib": 1024, "runtime_seconds": 20, "repetitions": 5, "iodepth": 16}


@pytest.mark.parametrize(
    "change",
    [
        {"interval_seconds": 1799},
        {"interval_seconds": 3601},
        {"interval_seconds": True},
        {"not_before_unix_seconds": float("nan")},
        {"disk": {"directory": "/"}},
        {"disk": {"repetitions": 0}},
        {"disk": {"runtime_seconds": 301}},
        {"providers": []},
        {"internal": {"host": "8.8.8.8"}},
        {"internal": {"host": "127.0.0.1"}},
        {"internal": {"host": "192.168.0.1", "protocol": "udp"}},
    ],
)
def test_configuration_rejects_unbounded_or_implicit_probes(change):
    config = measurement()
    config.update(change)
    with pytest.raises(ArtifactValidationError):
        station.validate_measurement(config)


@pytest.mark.parametrize(
    "change",
    [
        {"provider_id": "cloudflare"},
        {"download_url": "http://proof.ovh.net/files/10Mb.dat"},
        {"upload_url": "https://user:secret@proof.ovh.net/upload"},
        {"upload_bytes": None},
        {"download_bytes": 64 * 1024**2 + 1},
        {"repetitions": 4},
    ],
)
def test_provider_must_be_explicit_distinct_and_bounded(change):
    config = measurement()
    config["providers"][1].update(change)
    with pytest.raises(ArtifactValidationError):
        station.validate_measurement(config)


def test_disk_override_retains_explicit_data_directory(harness):
    harness.config["measurement"]["disk"] = {"runtime_seconds": 30, "size_mib": 2048}
    result = run(harness)
    assert result["status"] == "success"
    assert harness.calls[0]["disk"] == {
        "runtime_seconds": 30,
        "size_mib": 2048,
        "repetitions": 5,
        "iodepth": 16,
        "directory": harness.resource["data_directory"],
    }
    assert all(p["repetitions"] == 1 for p in harness.calls[0]["providers"])


def test_attempt_is_durable_before_any_guest_traffic_and_saved_sources_match(harness):
    result = run(harness)
    assert result["schema"] == station.SCHEMA
    assert result["station"]["flavor"]["vcpus"] == 4
    assert result["station"]["data_gib"] == 500
    assert result["station"]["data_volume_id"] != result["station"]["boot_volume_id"]
    assert result["cache"]["usable"] is True
    assert result["cache"]["reused"] is False
    output = Path(result["result_path"]).parent
    for name, digest in result["source_hashes"].items():
        assert hashlib.sha256((output / "sources" / name).read_bytes()).hexdigest() == digest
    assert result["probe_source_sha256"] == result["source_hashes"]["environment_benchmark.py"]
    assert (output / "report.md").is_file()
    assert (output / "overview.html").is_file()
    assert (output / "issues.md").is_file()


def test_utc_bucket_edge_never_bursts(harness):
    harness.clock.wall = harness.clock.mono = 10799.0
    first = run(harness)
    harness.clock.wall += 2
    harness.clock.mono += 2
    cached = run(harness)
    assert cached["attempt_id"] == first["attempt_id"]
    assert cached["scheduler"]["due"] is False
    assert len(harness.calls) == 1
    harness.clock.wall += 3598
    harness.clock.mono += 3598
    assert run(harness)["attempt_id"] != first["attempt_id"]
    assert len(harness.calls) == 2


def test_forward_wall_jump_cannot_bypass_monotonic_interval(harness):
    run(harness)
    harness.clock.wall += 7200
    harness.clock.mono += 1
    assert run(harness)["scheduler"]["due"] is False
    assert run(harness)["scheduler"]["next_eligible_unix_seconds"] >= harness.clock.wall + 3599
    assert len(harness.calls) == 1


def test_clock_reset_waits_full_current_uptime(harness):
    run(harness)
    harness.clock.wall += 7200
    harness.clock.mono = 100
    assert run(harness)["scheduler"]["due"] is False
    assert len(harness.calls) == 1


def test_failed_attempt_holds_interval_without_replay(harness, monkeypatch):
    def fail(*args):
        harness.calls.append("failure")
        raise OSError("provider transport unavailable")

    monkeypatch.setattr(station, "_execute", fail)
    failed = run(harness)
    assert failed["status"] == "failed"
    assert failed["cache"]["usable"] is False
    harness.clock.wall += 3599
    harness.clock.mono += 3599
    held = run(harness)
    assert held["attempt_id"] == failed["attempt_id"]
    assert len(harness.calls) == 1


def test_lowering_interval_cannot_shorten_previous_claim(harness):
    run(harness)
    harness.config["measurement"]["interval_seconds"] = 1800
    harness.clock.wall += 1801
    harness.clock.mono += 1801
    assert run(harness)["scheduler"]["due"] is False


def test_first_sample_embargo_does_not_even_inspect_cloud(harness, monkeypatch):
    harness.config["measurement"]["not_before_unix_seconds"] = harness.clock.wall + 1800
    monkeypatch.setattr(harness.cloud, "inspect", lambda *_: pytest.fail("no SDK/SSH during embargo"))
    result = run(harness)
    assert result["scheduler"]["due"] is False
    assert result["cache"]["usable"] is False
    assert not (harness.root / "measurement-schedule.json").exists()
    assert not harness.calls


def test_concurrent_workers_share_one_claim(harness, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []

    def execute(*args):
        entered.set()
        assert release.wait(5)
        return harness.execute(*args)

    def first_worker():
        try:
            results.append(run(harness))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(station, "_execute", execute)
    worker = threading.Thread(target=first_worker)
    worker.start()
    try:
        assert entered.wait(5)
        second = run(harness)
        assert second["scheduler"]["due"] is False
        assert "another worker" in second["scheduler"]["reason"]
        assert second["scheduler"]["next_eligible_unix_seconds"] > harness.clock.wall
        from palimpsest_local.benchmark_station_cloud import StationOperationBusy
        from palimpsest_local.benchmark_station_disk import run_disk_sample

        with pytest.raises(StationOperationBusy):
            run_disk_sample(SimpleNamespace(output=harness.cloud.output), harness.root / "other-disk-output")
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert len(harness.calls) == 1
    assert results[0]["status"] == "success"


def test_provider_arithmetic_mean_is_not_weighted_by_bytes_or_repetitions():
    small = provider("cloudflare", (2.0, 4.0, 6.0))
    large = provider("ovh", (20.0,))
    for sample in small["directions"]["download"]["repetitions"]:
        sample["bytes"] = 1024
    large["directions"]["download"]["repetitions"][0]["bytes"] = 64 * 1024**2
    combined = station._combine([small, large])
    assert combined["download"]["provider_means_mib_per_second"] == {"cloudflare": 4, "ovh": 20}
    assert combined["download"]["two_provider_mean_mib_per_second"] == 12


@pytest.mark.parametrize("bad", [None, 0, float("nan"), float("inf"), True])
def test_missing_or_invalid_direction_has_no_two_provider_mean(bad):
    providers = [provider("cloudflare"), provider("ovh")]
    providers[1]["directions"]["upload"]["repetitions"][0]["bandwidth_mib_per_second"] = bad
    combined = station._combine(providers)
    assert combined["download"]["two_provider_mean_mib_per_second"] == 10
    assert combined["upload"]["two_provider_mean_mib_per_second"] is None
    assert combined["upload"]["provider_means_mib_per_second"] == {"cloudflare": 10}


def test_failed_provider_still_allows_real_disk_context(harness, monkeypatch):
    def execute(*args):
        value = harness.execute(*args)
        leaf = value["network"]["external"]["providers"][0]
        leaf["status"] = "failed"
        leaf["directions"]["upload"] = {"status": "failed", "repetitions": [], "issues": ["HTTP 403"]}
        return value

    monkeypatch.setattr(station, "_execute", execute)
    result = run(harness)
    assert result["status"] == "partial"
    assert result["cache"]["usable"] is True
    assert result["environment"]["disk"]["status"] == "success"
    assert (
        result["environment"]["network"]["external"]["combined"]["upload"]["two_provider_mean_mib_per_second"] is None
    )
    report = station.render_measurement(result)
    assert "N/A" in report["report.md"]
    assert "cloudflare" in report["issues.md"]
    assert "403" in report["issues.md"]


@pytest.mark.parametrize(
    "field,value",
    [("data_size_gib", 499), ("data_volume_id", BOOT_VOLUME), ("data_directory", "/tmp"), ("source", "api")],
)
def test_original_data_binding_rejected_before_claim(harness, field, value):
    harness.resource[field] = value
    with pytest.raises(ArtifactValidationError):
        run(harness)
    assert not harness.calls
    assert not (harness.root / "measurement-schedule.json").exists()


@pytest.mark.parametrize("change", ["flavor", "attachment", "size", "project", "server"])
def test_actual_cloud_identity_mismatch_is_not_a_station(harness, change):
    if change == "flavor":
        harness.observed["flavor"]["actual"]["vcpus"] = 2
    elif change == "attachment":
        harness.observed["volumes"] = harness.observed["volumes"][:1]
    elif change == "size":
        harness.observed["volumes"][1]["size"] = 20
    elif change == "project":
        harness.observed["project_id"] = "other"
    else:
        harness.observed["server_id"] = BOOT_VOLUME
    with pytest.raises(ArtifactValidationError):
        run(harness)
    assert not harness.calls


def test_root_cannot_be_reused_by_another_station(harness):
    run(harness)
    harness.resource["server_id"] = "00000000-0000-4000-8000-000000000099"
    with pytest.raises(ArtifactValidationError, match="different original station"):
        run(harness)
    assert len(harness.calls) == 1


@pytest.mark.parametrize("mismatch", ["source", "provider-vm", "cohort", "boot", "logs"])
def test_guest_identity_boundaries_fail_without_repeating_probes(harness, monkeypatch, mismatch):
    def execute(*args):
        value = harness.execute(*args)
        if mismatch == "source":
            value["source_hashes"] = {}
        elif mismatch == "provider-vm":
            value["network"]["external"]["providers"][1]["server_id"] = "other-vm"
        elif mismatch == "cohort":
            value["network"]["external"]["providers"][1]["cohort_id"] = "different-time-window"
        elif mismatch == "boot":
            value["boot_id_after"] = "changed"
        else:
            value["raw_logs_complete"] = False
        return value

    monkeypatch.setattr(station, "_execute", execute)
    assert run(harness)["status"] == "failed"
    assert len(harness.calls) == 1
    assert run(harness)["scheduler"]["due"] is False
    assert len(harness.calls) == 1


def test_context_age_is_from_finished_not_started(harness):
    result = run(harness)
    saved = json.loads((harness.root / "measurement-latest.json").read_text())
    saved["started_unix_seconds"] -= 400
    atomic_write_json(harness.root / "measurement-latest.json", saved)
    harness.clock.wall += 3599
    context = station.latest_measurement(harness.root)
    assert context["cache"]["age_seconds"] == 3599
    assert context["cache"]["usable"] is True
    harness.clock.wall += 2
    stale = station.latest_measurement(harness.root)
    assert stale["cache"]["stale"] is True
    assert stale["cache"]["usable"] is False
    assert stale["attempt_id"] == result["attempt_id"]
    assert len(harness.calls) == 1


@pytest.mark.parametrize("change", ["future", "source", "vm", "provider", "probe-hash"])
def test_latest_rejects_future_unknown_source_or_vm_context_without_traffic(harness, change):
    run(harness)
    path = harness.root / "measurement-latest.json"
    value = json.loads(path.read_text())
    if change == "future":
        value["finished_unix_seconds"] += 100
    elif change == "source":
        value["source_hashes"]["environment_benchmark.py"] = "0" * 64
    elif change == "vm":
        value["environment"]["server_id"] = "other-vm"
    elif change == "provider":
        value["environment"]["network"]["external"]["providers"][0]["server_id"] = "other-vm"
    else:
        value["probe_source_sha256"] = "0" * 64
    atomic_write_json(path, value)
    context = station.latest_measurement(harness.root)
    assert context["cache"]["usable"] is False
    assert context["cache"]["issues"]
    assert len(harness.calls) == 1


def test_missing_context_is_visible_without_creating_a_schedule(tmp_path):
    context = station.latest_measurement(tmp_path)
    assert context["status"] == "failed"
    assert context["cache"]["usable"] is False
    assert not list(tmp_path.iterdir())


def test_symlink_scheduler_lock_is_rejected_without_execution(harness, tmp_path):
    target = tmp_path / "other-lock"
    target.write_text("")
    (harness.root / "stationmeasurement.lock").symlink_to(target)
    with pytest.raises(OSError):
        run(harness)
    assert not harness.calls


def test_transport_recovery_reads_checkpoint_then_collects_full_logs_only(tmp_path):
    value = {
        "schema": "palimpsest.environment-result.v1",
        "issues": [],
        "guest_logs": {"fio.stdout": {"bytes": 10, "sha256": "0" * 64, "truncated": True}},
    }
    calls, collected = [], []

    class Cloud:
        def run_guest(self, resource, argv, **kwargs):
            calls.append((argv, kwargs))
            if len(calls) == 1:
                raise OSError("connection interrupted")
            assert kwargs["label"].startswith("station-recover-")
            return {"returncode": 0, "stdout": json.dumps(value)}

        def _collect_environment_logs(self, resource, result, output):
            collected.append((resource, output))
            result["guest_logs"]["fio.stdout"]["collected"] = True

    payload = {
        "attempt_id": "one",
        "remote_root": "/tmp/palimpsest-scenario-environment-one",
        "execution_timeout_seconds": 3600,
    }
    result = station._execute(Cloud(), {"server_id": SERVER}, payload, tmp_path)
    assert len(calls) == 2
    assert "checkpoint.json" in calls[1][0][-1]
    assert collected == [({"server_id": SERVER}, tmp_path)]
    assert result["raw_logs_complete"] is True
    assert "without repeating traffic" in result["issues"][0]


def test_successful_private_input_reads_checkpoint_without_false_interruption(tmp_path):
    calls = []
    value = {"schema": "palimpsest.environment-result.v1", "issues": [], "guest_logs": {}}

    class Cloud:
        def run_guest(self, resource, argv, **kwargs):
            calls.append(kwargs)
            if "input_bytes" in kwargs:
                return {"returncode": 0, "stdout": "[PRIVATE INPUT: OUTPUT WITHHELD]\n"}
            return {"returncode": 0, "stdout": json.dumps(value)}

        def _collect_environment_logs(self, *args):
            pass

    payload = {
        "attempt_id": "one",
        "remote_root": "/tmp/palimpsest-scenario-environment-one",
        "execution_timeout_seconds": 3600,
    }
    result = station._execute(Cloud(), {}, payload, tmp_path)
    assert len(calls) == 2
    assert result["issues"] == []
    assert result["raw_logs_complete"] is True


def test_raw_log_failure_retains_manifest_but_never_qualifies(tmp_path):
    value = {"schema": "palimpsest.environment-result.v1", "issues": [], "guest_logs": {}}

    class Cloud:
        def run_guest(self, *args, **kwargs):
            return {"returncode": 0, "stdout": json.dumps(value)}

        def _collect_environment_logs(self, *args):
            raise OSError("bounded recovery failed")

    result = station._execute(
        Cloud(),
        {},
        {
            "attempt_id": "one",
            "remote_root": "/tmp/palimpsest-scenario-environment-one",
            "execution_timeout_seconds": 3600,
        },
        tmp_path,
    )
    assert result["raw_logs_complete"] is False
    assert result["issues"]
    assert json.loads((tmp_path / "guest-result.json").read_text())["raw_logs_complete"] is False


def test_sdk_exception_text_never_enters_checkpoint_or_reports(harness, monkeypatch):
    secret = "Bearer-private-credential-must-not-leak"

    def fail(*args):
        raise OSError(secret)

    monkeypatch.setattr(station, "_execute", fail)
    result = run(harness)
    assert result["status"] == "failed"
    assert secret not in json.dumps(result)
    assert "guest execution and bounded log recovery: OSError" in result["issues"][0]
    assert secret not in Path(result["result_path"]).read_text()
    assert all(secret not in content for content in station.render_measurement(result).values())


def test_missing_checkpoint_report_has_real_schema_but_no_invented_success():
    report = station.render_measurement(
        {"schema": station.SCHEMA, "status": "failed", "issues": ["no station checkpoint exists"]}
    )
    assert "Unsupported or missing result schema" not in report["issues.md"]
    assert "no station checkpoint exists" in report["issues.md"]
    assert "N/AvCPU/N/AMiB" in report["report.md"]
    assert "4vCPU/8192MiB" not in report["report.md"]


def test_previous_completed_checkpoint_remains_usable_while_refresh_executes(harness, monkeypatch):
    previous = run(harness)
    harness.clock.wall += 3600
    harness.clock.mono += 3600

    def refresh(*args):
        visible = station.latest_measurement(harness.root)
        assert visible["attempt_id"] == previous["attempt_id"]
        assert visible["cache"]["usable"] is True
        return harness.execute(*args)

    monkeypatch.setattr(station, "_execute", refresh)
    current = run(harness)
    assert current["attempt_id"] != previous["attempt_id"]
    assert station.latest_measurement(harness.root)["attempt_id"] == current["attempt_id"]


def test_probe_profile_that_cannot_finish_within_interval_is_rejected():
    value = measurement()
    value["disk"] = {"runtime_seconds": 300, "repetitions": 4}
    with pytest.raises(ArtifactValidationError, match="cannot complete"):
        station.validate_measurement(value)
