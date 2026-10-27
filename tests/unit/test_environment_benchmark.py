from __future__ import annotations

import copy
import json
import math
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from palimpsest_local import environment_benchmark as bench


def fio_data(direction="read", *, runtime=10000, count=1024, block=4096):
    size = count * block
    return {
        "jobs": [
            {
                "error": 0,
                direction: {
                    "total_ios": count,
                    "io_bytes": size,
                    "runtime": runtime,
                    "bw_bytes": size * 1000 / runtime,
                    "iops": count * 1000 / runtime,
                    "clat_ns": {"N": count, "percentile": {"95.000000": 2_000_000, "99.000000": 4_000_000}},
                },
            }
        ]
    }


def iperf_data(reverse=False):
    summary = {"seconds": 10, "bytes": 10 * 1024**2, "bits_per_second": 8 * 1024**2}
    return {
        "start": {"test_start": {"protocol": "TCP", "reverse": int(reverse)}},
        "end": {"sum_sent": {**summary, "retransmits": 3}, "sum_received": dict(summary)},
        "intervals": [
            {"sum": {"start": second, "end": second + 1, "bytes": 1024**2, "bits_per_second": 8 * 1024**2}}
            for second in range(10)
        ],
    }


def curl_data(direction="download", size=4096):
    return {
        "http_code": 200,
        "ssl_verify_result": 0,
        "num_redirects": 0,
        "size_download": size if direction == "download" else 12,
        "size_upload": size if direction == "upload" else 0,
        "time_total": 2,
        "time_namelookup": 0.1,
        "time_connect": 0.3,
        "time_appconnect": 0.7,
        "time_starttransfer": 0.8,
    }


def test_fio_derives_units_from_real_counters_and_completion_samples():
    result = bench.parse_fio(fio_data(), "read", 10)
    assert result["bandwidth_mib_per_second"] == 0.4
    assert result["iops"] == 102.4
    assert result["latency_p95_ms"] == 2
    assert result["latency_p99_ms"] == 4
    assert result["actual_io_count"] == result["latency_sample_count"] == 1024
    assert result["elapsed_seconds"] == 10


@pytest.mark.parametrize(
    "failure",
    [
        "error",
        "missing-error",
        "no-jobs",
        "two-jobs",
        "empty-io",
        "single-io",
        "single-latency",
        "no-latency",
        "partial-window",
        "nan",
        "infinity",
        "negative",
        "fake-bandwidth",
        "fake-iops",
        "no-p99",
        "backward-percentiles",
    ],
)
def test_fio_rejects_plausible_but_unmeasured_or_failed_results(failure):
    data = fio_data()
    job = data["jobs"][0]
    sample = job["read"]
    if failure == "error":
        job["error"] = 22
    elif failure == "missing-error":
        del job["error"]
    elif failure == "no-jobs":
        data["jobs"] = []
    elif failure == "two-jobs":
        data["jobs"].append(copy.deepcopy(job))
    elif failure in ("empty-io", "single-io"):
        sample["total_ios"] = int(failure == "single-io")
    elif failure == "single-latency":
        sample["clat_ns"]["N"] = 1
    elif failure == "no-latency":
        del sample["clat_ns"]
    elif failure == "partial-window":
        sample["runtime"] = 2000
    elif failure in ("nan", "infinity", "negative"):
        sample["bw_bytes"] = {"nan": float("nan"), "infinity": float("inf"), "negative": -1}[failure]
    elif failure == "fake-bandwidth":
        sample["bw_bytes"] *= 2
    elif failure == "fake-iops":
        sample["iops"] *= 2
    elif failure == "no-p99":
        del sample["clat_ns"]["percentile"]["99.000000"]
    else:
        sample["clat_ns"]["percentile"]["95.000000"] = 10_000_000
    with pytest.raises(bench.BenchmarkError):
        bench.parse_fio(data, "read", 10)


def test_fio_microsecond_latency_is_converted_without_fabricating_nanoseconds():
    data = fio_data()
    sample = data["jobs"][0]["read"]
    sample["clat_us"] = sample.pop("clat_ns")
    sample["clat_us"]["percentile"] = {"95.000000": 2000, "99.000000": 4000}
    assert bench.parse_fio(data, "read", 10)["latency_p99_ms"] == 4


def test_interval_statistics_include_zero_io_and_sampling_gaps():
    result = bench.parse_fio_intervals("1000, 1024, 0, 4096\n2000, 0, 0, 4096\n5000, 2048, 0, 4096\n", "read")
    assert result["count"] == 3
    assert result["stalls"] == 1
    assert result["low_rate_intervals"] == 1
    assert result["maximum_gap_seconds"] == 3
    assert result["cv"] == pytest.approx(math.sqrt(2 / 3))
    assert bench.summarize_values([1, 2, 3]) == {
        "count": 3,
        "mean": 2,
        "min": 1,
        "max": 3,
        "cv": pytest.approx(math.sqrt(2 / 3) / 2),
    }


@pytest.mark.parametrize("text", ["", "1000, nan, 0", "1000, 10, 1", "1000, 10, 0\n500, 10, 0", "0, 1, 0"])
def test_fio_interval_absence_direction_and_time_are_not_synthetic_success(text):
    with pytest.raises(bench.BenchmarkError):
        bench.parse_fio_intervals(text, "read")


def test_iperf_requires_both_endpoint_summaries_and_exposes_protocol_direction():
    for reverse in (False, True):
        result = bench.parse_iperf(iperf_data(reverse), reverse, 10)
        assert result["protocol"] == "tcp"
        assert result["direction"] == ("receive" if reverse else "send")
        assert result["bandwidth_mib_per_second"] == 1
        assert result["retransmits"] == 3
        assert result["interval_summary"]["count"] == 10
        assert result["interval_summary"]["cv"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        "server-error",
        "udp",
        "direction",
        "missing-receiver",
        "zero-bytes",
        "partial-window",
        "negative-retransmits",
        "nan",
        "fake-rate",
        "no-intervals",
        "single-interval",
        "short-intervals",
        "backwards",
        "fake-interval-rate",
    ],
)
def test_iperf_rejects_false_successes(failure):
    data = iperf_data()
    if failure == "server-error":
        data["error"] = "private remote diagnostic"
    elif failure == "udp":
        data["start"]["test_start"]["protocol"] = "UDP"
    elif failure == "direction":
        data["start"]["test_start"]["reverse"] = 1
    elif failure == "missing-receiver":
        del data["end"]["sum_received"]
    elif failure == "zero-bytes":
        data["end"]["sum_received"]["bytes"] = 0
    elif failure == "partial-window":
        data["end"]["sum_received"]["seconds"] = 1
    elif failure == "negative-retransmits":
        data["end"]["sum_sent"]["retransmits"] = -1
    elif failure == "nan":
        data["end"]["sum_sent"]["bits_per_second"] = float("nan")
    elif failure == "fake-rate":
        data["end"]["sum_sent"]["bits_per_second"] *= 2
    elif failure == "no-intervals":
        data["intervals"] = []
    elif failure == "single-interval":
        data["intervals"] = data["intervals"][:1]
    elif failure == "short-intervals":
        data["intervals"] = data["intervals"][:2]
    elif failure == "backwards":
        data["intervals"][1]["sum"]["start"] = 0
    else:
        data["intervals"][0]["sum"]["bits_per_second"] *= 4
    with pytest.raises(bench.BenchmarkError):
        bench.parse_iperf(data, False, 10)


def test_unsupported_tcp_retransmit_counter_is_missing_not_zero():
    data = iperf_data()
    del data["end"]["sum_sent"]["retransmits"]
    result = bench.parse_iperf(data, False, 10)
    assert "retransmits" not in result
    assert result["retransmits_status"] == "not-measured"


def test_https_actual_transfer_and_verified_connection_timings():
    result = bench.parse_curl(curl_data(), "download", 4096)
    assert result["connectivity"] == {"dns": "success", "tls": "success", "http": "success"}
    assert result["http_status_code"] == 200
    assert result["bandwidth_mib_per_second"] == 4096 / 2 / 1024**2
    assert result["connect_seconds"] == pytest.approx(0.2)
    assert result["tls_seconds"] == pytest.approx(0.4)
    assert bench.parse_curl(curl_data("upload"), "upload", 4096)["bytes"] == 4096


@pytest.mark.parametrize(
    "key,value",
    [
        ("http_code", 302),
        ("http_code", 500),
        ("ssl_verify_result", 60),
        ("num_redirects", 1),
        ("size_download", 4095),
        ("size_download", 8192),
        ("time_total", 0),
        ("time_total", float("inf")),
        ("time_appconnect", 0),
        ("time_appconnect", 3),
        ("time_connect", -1),
        ("time_namelookup", float("nan")),
    ],
)
def test_https_status_certificate_bounds_and_timings_are_mandatory(key, value):
    data = curl_data()
    data[key] = value
    with pytest.raises(bench.BenchmarkError):
        bench.parse_curl(data, "download", 4096)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/",
        "https://user:secret@example.test/x",
        "https://example.test/#secret",
        "https://example.test/a\nsecret",
        "https://127.0.0.1/x",
        "file:///tmp/foo",
        None,
    ],
)
def test_external_url_prerequisites_export_no_credentials(url):
    with pytest.raises(bench.BenchmarkError) as error:
        bench._url(url)
    assert "secret" not in str(error.value)


def test_cloudflare_bounded_query_is_allowed_but_not_required_in_results():
    url = "https://speed.cloudflare.com/__down?bytes=4096"
    assert bench._url(url) == url


def test_json_rejects_nonfinite_and_nonobject_documents():
    for text in ('{"rate":NaN}', '{"rate":Infinity}', "[]", "null", "not-json"):
        with pytest.raises(bench.BenchmarkError):
            bench._json(text)


def test_missing_required_sections_write_failed_durable_private_record(tmp_path):
    output = tmp_path.resolve() / "out"
    result = bench.run_environment({"schema": "palimpsest.benchmark-environment.v1"}, output)
    assert result["status"] == "failed"
    assert result["disk"]["status"] == "not-measured"
    assert result["network"]["status"] == "failed"
    assert result["network"]["external"]["status"] == "not-measured"
    assert json.loads((output / "result.json").read_text()) == result
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "result.json").stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        bench.run_environment({}, output)


def test_failed_part_cannot_be_overridden_by_other_successes_or_secret_error(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_disk", lambda *_: {"status": "success"})
    monkeypatch.setattr(bench, "_external", lambda *_: {"status": "success"})

    def fail(*_):
        raise bench.BenchmarkError("https://user:secret@example.test/?token=secret")

    monkeypatch.setattr(bench, "_internal", fail)
    result = bench.run_environment(
        {"schema": "palimpsest.benchmark-environment.v1", "disk": {}, "network": {"external": {}, "internal": {}}},
        tmp_path.resolve() / "out",
    )
    assert result["status"] == "failed"
    assert result["network"]["status"] == "failed"
    assert "secret" not in json.dumps(result)
    assert "https:" not in json.dumps(result)


def test_all_three_real_runner_successes_are_required(tmp_path, monkeypatch):
    calls = []
    for name in ("disk", "external", "internal"):

        def success(config, output, name=name):
            calls.append(name)
            return {"status": "success"}

        monkeypatch.setattr(bench, "_" + name, success)
    result = bench.run_environment(
        {"schema": "palimpsest.benchmark-environment.v1", "disk": {}, "network": {"external": {}, "internal": {}}},
        tmp_path.resolve() / "out",
    )
    assert result["status"] == "success"
    assert calls == ["disk", "external", "internal"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_seconds", 1),
        ("runtime_seconds", 301),
        ("runtime_seconds", True),
        ("repetitions", 0),
        ("repetitions", 11),
        ("size_mib", 0),
        ("size_mib", 16385),
        ("iodepth", 129),
        ("directory", None),
    ],
)
def test_disk_bounds_fail_before_any_measurement(field, value, tmp_path, monkeypatch):
    config = {"directory": str(tmp_path.resolve()), field: value}

    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid configuration launched a process")

    monkeypatch.setattr(bench, "_run", forbidden)
    with pytest.raises(bench.BenchmarkError):
        bench._disk(config, tmp_path.resolve())


def test_fiona_binary_is_rejected_before_scratch_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_tool", lambda _: "/tools/fio")
    monkeypatch.setattr(bench, "_run", lambda *_args, **_kwargs: "fio, version 1.10.1\n")
    with pytest.raises(bench.BenchmarkError, match="not the fio"):
        bench._disk({"directory": str(tmp_path.resolve())}, tmp_path.resolve())
    assert list(tmp_path.iterdir()) == []


def test_disk_uses_owned_pinned_regular_file_initializes_before_read_and_removes_only_it(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    retained = root / "keep.bin"
    retained.write_bytes(b"do not touch")
    evidence = root / "evidence"
    evidence.mkdir()
    monkeypatch.setattr(bench, "platform", type("Platform", (), {"system": staticmethod(lambda: "Linux")}))
    monkeypatch.setattr(bench, "_tool", lambda _: "/tools/fio")
    calls = []

    def fake_run(args, output, label, timeout, **kwargs):
        calls.append(label)
        if label == "fio-version":
            return "fio-3.36\n"
        fd = kwargs["pass_fds"][0]
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        assert f"--filename=/proc/self/fd/{fd}" in args
        assert "--direct=1" in args and "--ioengine=libaio" in args
        if label == "fio-initialize":
            return json.dumps(
                {"jobs": [{"error": 0, "write": {"io_bytes": 16 * 1024**2, "total_ios": 16, "runtime": 10}}]}
            )
        assert calls[1] == "fio-initialize"
        direction = "read" if "read" in label else "write"
        block = int(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--bs=")))
        return json.dumps(fio_data(direction, block=block))

    monkeypatch.setattr(bench, "_run", fake_run)
    result = bench._disk({"directory": str(root), "size_mib": 16, "repetitions": 1}, evidence)
    assert result["status"] == "success"
    assert set(result["profiles"]) == {"sequential-read", "sequential-write", "random-read-4k", "random-write-4k"}
    assert all(profile["aggregate"]["iops"]["count"] == 1 for profile in result["profiles"].values())
    assert retained.read_bytes() == b"do not touch"
    assert not list(root.glob(".palimpsest-benchmark-*"))


def test_initialization_failure_removes_owned_scratch_without_touching_unrelated_file(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    retained = root / ".palimpsest-benchmark-unrelated"
    retained.write_bytes(b"retain")
    monkeypatch.setattr(bench, "_tool", lambda _: "/tools/fio")

    def fake_run(args, output, label, timeout, **kwargs):
        if label == "fio-version":
            return "fio-3.36\n"
        raise bench.BenchmarkError("simulated initialization failure")

    monkeypatch.setattr(bench, "_run", fake_run)
    with pytest.raises(bench.BenchmarkError):
        bench._disk({"directory": str(root), "size_mib": 16}, root)
    assert list(root.iterdir()) == [retained]
    assert retained.read_bytes() == b"retain"


def test_symlink_directory_is_refused_and_existing_output_is_never_overwritten(tmp_path):
    root = tmp_path.resolve()
    real = root / "real"
    real.mkdir()
    alias = root / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        bench._open_directory(alias)
    with pytest.raises(OSError):
        bench.run_environment({}, alias / "out")
    assert not list(real.iterdir())


@pytest.mark.parametrize(
    "config",
    [
        {"host": "8.8.8.8"},
        {"host": "127.0.0.1"},
        {"host": "169.254.1.1"},
        {"host": "peer.example.test"},
        {"host": "10.1.2.3", "duration_seconds": 1},
        {"host": "10.1.2.3", "parallel": 17},
        {"host": "10.1.2.3", "protocol": "udp"},
    ],
)
def test_internal_targets_and_duration_are_explicit_and_bounded(config, tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid target launched a process")

    monkeypatch.setattr(bench, "_run", forbidden)
    with pytest.raises(bench.BenchmarkError):
        bench._internal(config, tmp_path.resolve())


def test_internal_always_attempts_both_directions_and_failed_reverse_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_tool", lambda _: "/tools/iperf3")
    calls = []

    def fake_run(args, output, label, timeout):
        calls.append(args)
        if "--reverse" in args:
            raise bench.BenchmarkError("simulated reverse failure")
        return json.dumps(iperf_data())

    monkeypatch.setattr(bench, "_run", fake_run)
    result = bench._internal({"host": "10.1.2.3", "repetitions": 1}, tmp_path.resolve())
    assert result["status"] == "failed"
    assert result["directions"]["send"]["status"] == "success"
    assert result["directions"]["receive"]["status"] == "failed"
    assert result["directions"]["receive"]["aggregate"] == {}
    assert len(calls) == 2 and "--reverse" in calls[1]


def test_scratch_cleanup_never_unlinks_a_replacement_symlink(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    keep = root / "keep"
    keep.write_bytes(b"retained")
    monkeypatch.setattr(bench, "_tool", lambda _: "/tools/fio")

    def fake_run(args, output, label, timeout, **kwargs):
        if label == "fio-version":
            return "fio-3.36\n"
        scratch = next(root.glob(".palimpsest-benchmark-*"))
        scratch.unlink()
        scratch.symlink_to(keep)
        raise bench.BenchmarkError("injected replacement")

    monkeypatch.setattr(bench, "_run", fake_run)
    with pytest.raises(bench.BenchmarkError):
        bench._disk({"directory": str(root), "size_mib": 16}, root)
    replacement = next(root.glob(".palimpsest-benchmark-*"))
    assert replacement.is_symlink()
    assert keep.read_bytes() == b"retained"


def test_external_invocations_disable_user_config_redirects_and_bound_responses(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_tool", lambda _: "/tools/curl")
    calls = []

    def fake_run(args, output, label, timeout, **kwargs):
        calls.append((args, kwargs))
        assert args[1] == "--disable"
        assert "--location" not in args and "--insecure" not in args
        assert args[args.index("--max-redirs") + 1] == "0"
        assert args[args.index("--max-time") + 1] == "120"
        direction = "download" if "download" in label else "upload"
        assert kwargs["file_size_limit"] == (4096 if direction == "download" else 65536)
        if direction == "upload":
            payload = args[args.index("--data-binary") + 1][1:]
            assert Path(payload).stat().st_size == 4096
        return json.dumps(curl_data(direction))

    monkeypatch.setattr(bench, "_run", fake_run)
    result = bench._external(
        {
            "download_url": "https://example.test/__down?bytes=4096&signature=private",
            "upload_url": "https://example.test/up?token=private",
            "download_bytes": 4096,
            "upload_bytes": 4096,
            "repetitions": 1,
        },
        tmp_path.resolve(),
    )
    assert result["status"] == "success" and len(calls) == 2
    assert "private" not in json.dumps(result) and "example.test" not in json.dumps(result)
    assert not (tmp_path / "upload-payload.bin").exists()


def test_process_timeout_terminates_then_kills_and_waits_for_owned_group(tmp_path, monkeypatch):
    class Process:
        pid = 12345
        calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls <= 2:
                raise subprocess.TimeoutExpired("private secret command", timeout)
            return -9

    process = Process()
    invocations, signals = [], []

    def spawn(args, **kwargs):
        invocations.append(kwargs)
        return process

    monkeypatch.setattr(bench.subprocess, "Popen", spawn)
    monkeypatch.setattr(bench.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(bench.BenchmarkError) as error:
        bench._run(["fake-tool"], tmp_path.resolve(), "timeout", 1)
    assert "secret" not in str(error.value)
    assert signals[:2] == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]
    assert process.calls == 3
    assert invocations[0]["start_new_session"] is True
    assert invocations[0]["stdin"] == subprocess.DEVNULL
    assert "shell" not in invocations[0]


def test_real_child_response_size_limit_is_enforced(tmp_path):
    root = tmp_path.resolve()
    body = root / "body"
    with pytest.raises(bench.BenchmarkError):
        bench._run(
            [sys.executable, "-I", "-S", "-c", "import sys; open(sys.argv[1], 'wb').write(b'x' * 8192)", str(body)],
            root,
            "bounded-body",
            5,
            file_size_limit=1024,
        )
    assert body.stat().st_size <= 1024


def test_cli_exit_status_and_stdout_do_not_dump_sensitive_configuration(tmp_path, monkeypatch, capsys):
    root = tmp_path.resolve()
    config = root / "config.json"
    config.write_text(
        json.dumps({"schema": "wrong", "network": {"upload_url": "https://secret.invalid/?token=private"}})
    )
    assert bench.main(["--config", str(config), "--output", str(root / "out")]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "failed"
    assert "private" not in captured.out + captured.err
