from __future__ import annotations

import copy

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.nova_metrics import summarize_runs, summarize_samples


def _sample(stamp, *, read=0, write=0, rx=0, tx=0):
    return {
        "schema": "palimpsest.guest-io-sample.v1",
        "boot_id": "boot-one",
        "monotonic_seconds": stamp,
        "blocks": {
            "vda": {"read_bytes": read, "write_bytes": write, "read_ios": 0, "write_ios": 0, "io_milliseconds": 0}
        },
        "networks": {"ens3": {"rx_bytes": rx, "tx_bytes": tx}},
    }


def test_measured_deltas_rates_and_cutoff_do_not_include_later_work():
    samples = [
        _sample(10),
        _sample(12, read=4 * 1024**2, rx=2 * 1024**2),
        _sample(16, read=8 * 1024**2, write=6 * 1024**2, rx=10 * 1024**2),
        _sample(20, read=100 * 1024**2, rx=100 * 1024**2),
    ]
    result = summarize_samples(samples, end_monotonic=16)
    assert result["duration_seconds"] == 6
    assert result["blocks"]["vda"]["delta"]["read_bytes"] == 8 * 1024**2
    assert result["blocks"]["vda"]["maximum_sample_mib_per_second"]["read_bytes"] == 2
    assert result["blocks"]["vda"]["average_mib_per_second"]["write_bytes"] == 1
    assert result["networks"]["ens3"]["maximum_sample_mib_per_second"]["rx_bytes"] == 2
    assert result["maximum_sampling_gap_seconds"] == 4


@pytest.mark.parametrize("change", ["boot", "reset", "clock", "devices", "negative", "nan"])
def test_invalid_counter_windows_cannot_be_reported_as_throughput(change):
    left, right = _sample(1, read=100), _sample(2, read=200)
    if change == "boot":
        right["boot_id"] = "another-boot"
    elif change == "reset":
        right["blocks"]["vda"]["read_bytes"] = 0
    elif change == "clock":
        right["monotonic_seconds"] = 1
    elif change == "devices":
        right["blocks"]["vdb"] = copy.deepcopy(right["blocks"]["vda"])
    elif change == "negative":
        right["networks"]["ens3"]["tx_bytes"] = -1
    else:
        right["monotonic_seconds"] = float("nan")
    with pytest.raises(ArtifactValidationError):
        summarize_samples([left, right])


def test_repetition_summary_reports_failed_and_missing_runs_not_five_successes():
    runs = [
        {
            "run_id": "one",
            "status": "success",
            "cpu_fallback": False,
            "boot_id": "b1",
            "gpu_uuid": "g1",
            "request_to_first_nvidia_smi_seconds": 120.0,
        },
        {"run_id": "two", "status": "failed"},
    ]
    result = summarize_runs(runs)
    assert result["complete"] is False
    assert (result["attempts"], result["successes"], result["failures"]) == (2, 1, 1)
    assert result["request_to_first_nvidia_smi_seconds"]["raw"] == [120.0]
    runs[0]["cpu_fallback"] = True
    with pytest.raises(ArtifactValidationError):
        summarize_runs(runs)


def test_repeated_attempt_identity_is_not_an_independent_measurement():
    with pytest.raises(ArtifactValidationError):
        summarize_runs([{"run_id": "same", "status": "failed"}, {"run_id": "same", "status": "failed"}])


def test_sampler_ignores_sysfs_bonding_metadata_file(tmp_path, monkeypatch):
    import json
    import pathlib
    import sys
    import time

    from palimpsest_local.nova_metrics import GUEST_SAMPLER_SOURCE

    net = tmp_path / "sys-class-net"
    counters = net / "ens3" / "statistics"
    counters.mkdir(parents=True)
    (net / "bonding_masters").write_text("")
    (counters / "rx_bytes").write_text("123")
    (counters / "tx_bytes").write_text("456")
    boot = tmp_path / "boot-id"
    boot.write_text("actual-boot\n")
    disks = tmp_path / "diskstats"
    disks.write_text("252 0 vda 1 0 2 0 3 0 4 0 0 5 0\n")
    output = tmp_path / "samples.jsonl"
    original = type(tmp_path)
    paths = {
        "/sys/class/net": net,
        "/proc/sys/kernel/random/boot_id": boot,
        "/proc/diskstats": disks,
        "/run/palimpsest-metrics.stop": tmp_path / "stop",
    }
    stamps = iter([0.0, 0.5, 1.0])
    with monkeypatch.context() as context:
        context.setattr(pathlib, "Path", lambda value: paths.get(str(value), original(value)))
        context.setattr(sys, "argv", ["sampler", str(output), "1"])
        context.setattr(time, "monotonic", lambda: next(stamps))
        exec(compile(GUEST_SAMPLER_SOURCE, "generated-sampler", "exec"), {"__name__": "__main__"})
    sample = json.loads(output.read_text())
    assert sample["networks"] == {"ens3": {"rx_bytes": 123, "tx_bytes": 456}}
    assert sample["blocks"]["vda"]["read_bytes"] == 1024


def test_same_boot_cannot_be_counted_as_five_independent_measurements():
    run = {
        "run_id": "one",
        "status": "success",
        "cpu_fallback": False,
        "boot_id": "same-boot",
        "gpu_uuid": "gpu",
        "request_to_first_nvidia_smi_seconds": 120.0,
    }
    with pytest.raises(ArtifactValidationError, match="same boot"):
        summarize_runs([run, {**run, "run_id": "two"}])


@pytest.fixture
def benchmark_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts/benchmark_nova_gpu.py"
    spec = importlib.util.spec_from_file_location("nova_benchmark_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("field", ["gigabytes", "volumes"])
def test_retained_boot_refuses_exhausted_capacity(benchmark_module, field):
    from types import SimpleNamespace

    quota = SimpleNamespace(gigabytes=100, volumes=10, usage={"gigabytes": 80, "volumes": 9})
    benchmark_module._check_boot_quota(quota, 20)
    quota.usage[field] += 1
    with pytest.raises(benchmark_module.PalimpsestError, match=field + " quota"):
        benchmark_module._check_boot_quota(quota, 20)


@pytest.mark.parametrize("size", [None, True, 9, 16385])
def test_retained_boot_requires_explicit_valid_size(benchmark_module, size):
    from types import SimpleNamespace

    quota = SimpleNamespace(gigabytes=-1, volumes=-1, usage={})
    with pytest.raises(benchmark_module.PalimpsestError, match="explicit valid boot_gib"):
        benchmark_module._check_boot_quota(quota, size)


@pytest.mark.parametrize(
    "condition", ["foreign-ip", "bound-ip", "missing-port", "foreign-port", "two-ports", "bad-confirmation", "valid"]
)
def test_measurement_routing_rejects_unsafe_binding(benchmark_module, tmp_path, monkeypatch, condition):
    from types import SimpleNamespace

    module = benchmark_module
    updates = []
    floating = SimpleNamespace(id="ip", project_id="project", port_id=None, floating_ip_address="192.0.2.1")
    ports = [SimpleNamespace(id="port", project_id="project", device_id="server")]
    if condition == "foreign-ip":
        floating.project_id = "another-project"
    if condition == "bound-ip":
        floating.port_id = "another-server-port"
    if condition == "missing-port":
        ports = []
    if condition == "foreign-port":
        ports[0].project_id = "another-project"
    if condition == "two-ports":
        ports += [SimpleNamespace(id="port2", project_id="project", device_id="server")]

    def update_ip(resource, *, port_id):
        updates.append(port_id)
        resource.port_id = "wrong-port" if condition == "bad-confirmation" else port_id

    network = SimpleNamespace(get_ip=lambda _: floating, ports=lambda **_: ports, update_ip=update_ip)
    connection = SimpleNamespace(
        network=network,
        block_storage=SimpleNamespace(
            get_quota_set=lambda *a, **k: SimpleNamespace(
                gigabytes=100, volumes=10, usage={"gigabytes": 0, "volumes": 0}
            )
        ),
    )
    controller = SimpleNamespace(
        create=lambda **_: {
            "server_id": "server",
            "request_unix_seconds": __import__("time").time(),
            "request_monotonic_seconds": __import__("time").monotonic(),
        },
        wait_active=lambda *a, **k: {
            "addresses": {"net": [{"addr": "10.0.0.1", "OS-EXT-IPS:type": "fixed", "version": 4}]}
        },
    )
    monkeypatch.setattr(module, "NovaController", lambda *a: controller)

    def prepare_user_data(case, run_id, run_root):
        (run_root / "host.pub").write_text("ssh-ed25519 fixture\n")
        return "#cloud-config\n", "/tmp/samples"

    monkeypatch.setattr(module, "_userdata", prepare_user_data)
    monkeypatch.setattr(module, "_delete_owned_server", lambda *a: None)

    def stop_after_route(plan, run_root, target):
        assert target == "192.0.2.1"
        raise module.PalimpsestError("routing-complete")

    monkeypatch.setattr(module, "_ssh_prefix", stop_after_route)
    plan = {"marker": "routing", "project_id": "project", "launch": {"boot_gib": 20}, "management_floating_ip_id": "ip"}
    case = {"name": "case", "framework": "pytorch", "method": "nova-layer"}
    result = module._measure(plan, case, 1, connection, tmp_path, tmp_path)
    if condition == "valid":
        assert result["error"] == "routing-complete"
        assert result["fip_binding_seconds"] >= 0
    elif condition == "bad-confirmation":
        assert "binding was not confirmed" in result["error"]
    else:
        assert updates == []
        assert "floating IP must be" in result["error"] or "one exact-owned" in result["error"]


def test_metadata_failure_preserves_observed_nvidia_readiness(benchmark_module, tmp_path, monkeypatch):
    import json
    import subprocess
    from types import SimpleNamespace

    module = benchmark_module
    controller = SimpleNamespace(
        create=lambda **_: {
            "server_id": "server",
            "request_unix_seconds": __import__("time").time(),
            "request_monotonic_seconds": __import__("time").monotonic(),
        },
        wait_active=lambda *a, **k: {
            "addresses": {"net": [{"addr": "192.0.2.1", "OS-EXT-IPS:type": "fixed", "version": 4}]}
        },
    )
    connection = SimpleNamespace(
        block_storage=SimpleNamespace(
            get_quota_set=lambda *a, **k: SimpleNamespace(
                gigabytes=100, volumes=10, usage={"gigabytes": 0, "volumes": 0}
            )
        )
    )
    monkeypatch.setattr(module, "NovaController", lambda *a: controller)

    def userdata(case, run_id, root):
        (root / "host.pub").write_text("ssh-ed25519 fixture\n")
        return "#cloud-config\n", "/tmp/samples"

    monkeypatch.setattr(module, "_userdata", userdata)
    monkeypatch.setattr(module, "_ssh_prefix", lambda *a: ["synthetic-ssh"])
    monkeypatch.setattr(module, "_delete_owned_server", lambda *a: None)
    monkeypatch.setattr(module, "_wait_guest", lambda *a, **k: subprocess.CompletedProcess([], 0, "GPU observed", ""))

    def metadata_failure(*args, **kwargs):
        raise subprocess.CalledProcessError(255, args[0], output="", stderr="metadata transport failed")

    monkeypatch.setattr(module, "_run", metadata_failure)
    result = module._measure(
        {"marker": "metadata", "project_id": "project", "launch": {"boot_gib": 20}},
        {"name": "case", "framework": "pytorch", "method": "nova-layer"},
        1,
        connection,
        tmp_path,
        tmp_path,
    )
    saved = json.loads((tmp_path / "metadata-case-1" / "result.json").read_text())
    assert result["status"] == saved["status"] == "failed"
    assert saved["request_to_first_nvidia_smi_seconds"] > 0
    assert "request_to_framework_gpu_ready_seconds" not in saved
    assert saved["subprocess_returncode"] == 255


def test_ssh_retry_covers_transport_failure_only(benchmark_module, monkeypatch):
    import subprocess

    module = benchmark_module
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    calls = []

    def flaky(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(255, argv, stderr="Connection timed out during banner exchange")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(module, "_run", flaky)
    assert module._run_ssh_retrying_transport(["ssh", "host", "true"]).stdout == "ok"
    assert len(calls) == 2

    calls.clear()

    def real_failure(argv, **kwargs):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv, stderr="remote command failed")

    monkeypatch.setattr(module, "_run", real_failure)
    with pytest.raises(subprocess.CalledProcessError):
        module._run_ssh_retrying_transport(["ssh", "host", "false"])
    assert len(calls) == 1, "a genuine remote failure must never be retried"


def test_host_sleep_makes_request_relative_timings_invalid(benchmark_module, tmp_path, monkeypatch):
    import json
    import subprocess
    from types import SimpleNamespace

    module = benchmark_module
    controller = SimpleNamespace(
        create=lambda **_: {
            "server_id": "server",
            "request_unix_seconds": module.time.time() - 6000,  # wall clock ran, monotonic did not
            "request_monotonic_seconds": module.time.monotonic(),
        },
        wait_active=lambda *a, **k: {
            "addresses": {"net": [{"addr": "192.0.2.1", "OS-EXT-IPS:type": "fixed", "version": 4}]}
        },
    )
    connection = SimpleNamespace(
        block_storage=SimpleNamespace(
            get_quota_set=lambda *a, **k: SimpleNamespace(
                gigabytes=100, volumes=10, usage={"gigabytes": 0, "volumes": 0}
            )
        )
    )
    monkeypatch.setattr(module, "NovaController", lambda *a: controller)

    def userdata(case, run_id, root):
        (root / "host.pub").write_text("ssh-ed25519 fixture\n")
        return "#cloud-config\n", "/tmp/samples"

    monkeypatch.setattr(module, "_userdata", userdata)
    monkeypatch.setattr(module, "_ssh_prefix", lambda *a: ["synthetic-ssh"])
    monkeypatch.setattr(module, "_delete_owned_server", lambda *a: None)
    monkeypatch.setattr(module, "_wait_guest", lambda *a, **k: subprocess.CompletedProcess([], 0, "GPU observed", ""))
    monkeypatch.setattr(
        module,
        "_run_ssh_retrying_transport",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "stale-boot\n6147.26\n", ""),
    )
    result = module._measure(
        {"marker": "identity", "project_id": "project", "launch": {"boot_gib": 20}},
        {"name": "case", "framework": "tensorflow", "method": "cloud-init"},
        1,
        connection,
        tmp_path,
        tmp_path,
    )
    saved = json.loads((tmp_path / "identity-case-1" / "result.json").read_text())
    assert result["status"] == saved["status"] == "failed"
    assert saved["clock_inconsistent"] is True
    assert "request_to_first_nvidia_smi_seconds" not in saved, "a timing taken across a host sleep must be discarded"
    assert "request_to_framework_gpu_ready_seconds" not in saved


@pytest.mark.parametrize("becomes_ready", [True, False])
def test_readiness_poll_survives_probe_timeout_until_deadline(benchmark_module, monkeypatch, tmp_path, becomes_ready):
    import json
    import subprocess

    module = benchmark_module
    clock = [0.0]
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def probe(argv, *, timeout, **kwargs):
        calls.append(timeout)
        if len(calls) > 1 and becomes_ready:
            clock[0] += 1
            return subprocess.CompletedProcess(argv, 0, "GPU ready", "")
        clock[0] += timeout
        raise subprocess.TimeoutExpired(argv, timeout, output=b"", stderr=b"banner still unavailable")

    monkeypatch.setattr(module, "_run", probe)
    prefix = tmp_path / "probe"
    if becomes_ready:
        result = module._wait_guest(["ssh", "host"], ["nvidia-smi"], 65, evidence_prefix=prefix)
        assert result.stdout == "GPU ready"
        assert len(calls) == 2
        assert json.loads(prefix.with_suffix(".status.json").read_text())["returncode"] == 0
    else:
        with pytest.raises(module.PalimpsestError, match="readiness deadline expired"):
            module._wait_guest(["ssh", "host"], ["nvidia-smi"], 65, evidence_prefix=prefix)
        assert clock[0] == 65
        assert json.loads(prefix.with_suffix(".status.json").read_text())["timed_out"] is True
        assert prefix.with_suffix(".stderr").read_text() == "banner still unavailable"
