"""Bounded, explicit-target environment measurements; also executable standalone.

Only a newly created regular scratch file is written. Disk results describe the
selected filesystem (guest virtual/Cinder devices when applicable), not physical
Ceph OSD throughput. Private raw evidence may contain endpoint information;
result.json deliberately contains neither endpoint URLs nor subprocess errors.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import platform
import re
import resource
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


class BenchmarkError(ValueError):
    """A safe, endpoint-independent measurement or configuration error."""


def _number(value, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise BenchmarkError(f"invalid {name}")
    return float(value)


def _integer(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BenchmarkError(f"invalid {name} bounds")
    return value


def _json(text: str) -> dict:
    try:
        value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise BenchmarkError("invalid measurement JSON") from None
    if not isinstance(value, dict):
        raise BenchmarkError("measurement JSON must be an object")
    return value


def summarize_values(values: list[float]) -> dict:
    """Population CV over actual repetitions; missing samples are not zeros."""
    if not values:
        raise BenchmarkError("no actual samples")
    samples = [_number(value, "sample") for value in values]
    mean = statistics.fmean(samples)
    return {
        "count": len(samples),
        "mean": mean,
        "min": min(samples),
        "max": max(samples),
        "cv": statistics.pstdev(samples) / mean if mean else 0.0,
    }


def summarize_intervals(intervals: list[dict]) -> dict:
    if not intervals:
        raise BenchmarkError("no actual intervals")
    previous = None
    rates, gaps, durations = [], [], []
    for item in intervals:
        start = _number(item.get("start"), "interval start")
        end = _number(item.get("end"), "interval end", positive=True)
        if end <= start or (previous is not None and start < previous - 0.001):
            raise BenchmarkError("invalid interval ordering")
        rates.append(_number(item.get("bandwidth_mib_per_second"), "interval bandwidth"))
        gaps.append(max(0.0, start - previous) if previous is not None else start)
        durations.append(end - start)
        previous = end
    summary = summarize_values(rates)
    return {
        "count": len(rates),
        "cv": summary["cv"],
        "maximum_gap_seconds": max(gaps + durations),
        "maximum_unobserved_gap_seconds": max(gaps),
        "maximum_interval_seconds": max(durations),
        "stalls": sum(rate == 0 for rate in rates),
        "low_rate_intervals": sum(rate < summary["mean"] * 0.1 for rate in rates),
    }


def parse_fio(data: dict, direction: str, runtime_seconds: int) -> dict:
    """Accept one real timed fio job, including completion samples and latency."""
    if direction not in ("read", "write"):
        raise BenchmarkError("invalid fio measurement direction")
    _integer(runtime_seconds, "fio requested duration", 2, 300)
    try:
        jobs = data["jobs"]
        if not isinstance(jobs, list) or len(jobs) != 1:
            raise BenchmarkError("fio requires exactly one job")
        job = jobs[0]
        if type(job.get("error")) is not int or job["error"] != 0:
            raise BenchmarkError("fio reported a runtime error")
        metrics = job[direction]
        count = _integer(metrics.get("total_ios"), "fio completed IO count", 2, 2**63 - 1)
        size = _number(metrics.get("io_bytes"), "fio bytes", positive=True)
        elapsed_ms = _number(metrics.get("runtime"), "fio runtime", positive=True)
        if elapsed_ms < runtime_seconds * 980:
            raise BenchmarkError("fio ended before the measurement window")
        latency = metrics.get("clat_ns")
        scale = 1_000_000
        if not isinstance(latency, dict):
            latency = metrics.get("clat_us")
            scale = 1000
        if not isinstance(latency, dict):
            raise BenchmarkError("fio completion latency is absent")
        _integer(latency.get("N"), "fio latency sample count", 2, count)
        percentiles = latency["percentile"]
        selected = {}
        for key, value in percentiles.items():
            percentile = float(key)
            if percentile in (95.0, 99.0):
                selected[percentile] = _number(value, "fio latency percentile") / scale
        if set(selected) != {95.0, 99.0} or selected[99.0] < selected[95.0]:
            raise BenchmarkError("fio required percentiles are absent or invalid")
        bandwidth = _number(metrics.get("bw_bytes"), "fio bandwidth", positive=True) / 1024**2
        iops = _number(metrics.get("iops"), "fio IOPS", positive=True)
        # Detect superficially plausible summaries detached from their actual counters.
        if not math.isclose(bandwidth, size / (elapsed_ms / 1000) / 1024**2, rel_tol=0.05):
            raise BenchmarkError("fio bandwidth disagrees with completed bytes")
        if not math.isclose(iops, count / (elapsed_ms / 1000), rel_tol=0.05):
            raise BenchmarkError("fio IOPS disagrees with completion count")
        return {
            "bandwidth_mib_per_second": bandwidth,
            "iops": iops,
            "latency_p95_ms": selected[95.0],
            "latency_p99_ms": selected[99.0],
            "actual_io_count": count,
            "latency_sample_count": latency["N"],
            "bytes": size,
            "elapsed_seconds": elapsed_ms / 1000,
        }
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as error:
        if isinstance(error, BenchmarkError):
            raise
        raise BenchmarkError("fio measurement fields are invalid") from None


def parse_fio_intervals(text: str, direction: str) -> dict:
    """fio bandwidth logs contain milliseconds and KiB/s, not byte/s."""
    intervals = []
    previous = 0.0
    wanted = 0 if direction == "read" else 1
    try:
        for line in text.splitlines():
            columns = line.split(",")
            if len(columns) < 3 or int(columns[2]) != wanted:
                raise BenchmarkError("invalid fio bandwidth interval")
            end = _number(float(columns[0]), "fio interval timestamp", positive=True) / 1000
            rate = _number(float(columns[1]), "fio interval bandwidth") / 1024
            intervals.append({"start": previous, "end": end, "bandwidth_mib_per_second": rate})
            previous = end
        return summarize_intervals(intervals)
    except (ValueError, IndexError):
        raise BenchmarkError("invalid fio bandwidth intervals") from None


def parse_iperf(data: dict, reverse: bool, duration_seconds: int) -> dict:
    """TCP only. Both endpoint summaries and actual intervals are required."""
    if type(reverse) is not bool:
        raise BenchmarkError("invalid iperf3 requested direction")
    _integer(duration_seconds, "iperf3 requested duration", 2, 300)
    try:
        if "error" in data:
            raise BenchmarkError("iperf3 reported an error")
        settings = data["start"]["test_start"]
        if settings.get("protocol") != "TCP" or settings.get("reverse") != int(reverse):
            raise BenchmarkError("iperf3 protocol or direction disagrees")
        sender, receiver = data["end"]["sum_sent"], data["end"]["sum_received"]
        for summary in (sender, receiver):
            seconds = _number(summary.get("seconds"), "iperf3 duration", positive=True)
            if seconds < duration_seconds * 0.9:
                raise BenchmarkError("iperf3 ended before the measurement window")
            size = _number(summary.get("bytes"), "iperf3 bytes", positive=True)
            rate = _number(summary.get("bits_per_second"), "iperf3 rate", positive=True)
            if not math.isclose(rate, size * 8 / seconds, rel_tol=0.05):
                raise BenchmarkError("iperf3 rate disagrees with bytes")
        retransmits = (
            _integer(sender["retransmits"], "iperf3 retransmits", 0, 2**63 - 1) if "retransmits" in sender else None
        )
        intervals = []
        for interval in data["intervals"]:
            sample = interval["sum"]
            start = _number(sample.get("start"), "iperf3 interval start")
            end = _number(sample.get("end"), "iperf3 interval end", positive=True)
            rate = _number(sample.get("bits_per_second"), "iperf3 interval rate")
            size = _number(sample.get("bytes"), "iperf3 interval bytes")
            if end <= start or not math.isclose(rate, size * 8 / (end - start), rel_tol=0.05, abs_tol=1):
                raise BenchmarkError("iperf3 interval counters disagree")
            intervals.append({"start": start, "end": end, "bandwidth_mib_per_second": rate / 8 / 1024**2})
        interval_summary = summarize_intervals(intervals)
        if len(intervals) < 2 or intervals[-1]["end"] < duration_seconds * 0.9:
            raise BenchmarkError("iperf3 actual intervals are incomplete")
        result = {
            "protocol": "tcp",
            "direction": "receive" if reverse else "send",
            "bandwidth_mib_per_second": receiver["bits_per_second"] / 8 / 1024**2,
            "sender_bandwidth_mib_per_second": sender["bits_per_second"] / 8 / 1024**2,
            "bytes": receiver["bytes"],
            "elapsed_seconds": receiver["seconds"],
            "interval_summary": interval_summary,
        }
        if retransmits is None:
            result["retransmits_status"] = "not-measured"
        else:
            result["retransmits"] = retransmits
        return result
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as error:
        if isinstance(error, BenchmarkError):
            raise
        raise BenchmarkError("iperf3 measurement fields are invalid") from None


def parse_curl(data: dict, direction: str, expected_bytes: int) -> dict:
    if direction not in ("download", "upload"):
        raise BenchmarkError("invalid HTTPS measurement direction")
    _integer(expected_bytes, "HTTPS requested byte count", 1024, 64 * 1024**2)
    try:
        if (
            type(data.get("http_code")) is not int
            or data["http_code"] not in range(200, 300)
            or data.get("ssl_verify_result") != 0
        ):
            raise BenchmarkError("HTTPS response or certificate verification failed")
        if data.get("num_redirects") != 0:
            raise BenchmarkError("HTTPS redirects are forbidden")
        size = _number(data.get("size_download" if direction == "download" else "size_upload"), "HTTPS bytes")
        if size != expected_bytes:
            raise BenchmarkError("HTTPS actual byte count differs from requested bound")
        total = _number(data.get("time_total"), "HTTPS total time", positive=True)
        dns = _number(data.get("time_namelookup"), "DNS time")
        connect = _number(data.get("time_connect"), "connect time", positive=True)
        tls = _number(data.get("time_appconnect"), "TLS time", positive=True)
        ttfb = _number(data.get("time_starttransfer"), "first-byte time", positive=True)
        if not dns <= connect <= tls <= ttfb <= total:
            raise BenchmarkError("HTTPS connection timings are inconsistent")
        response_bound = expected_bytes if direction == "download" else 65536
        if _number(data.get("size_download"), "HTTPS response bytes") > response_bound:
            raise BenchmarkError("HTTPS response exceeds bound")
        return {
            "protocol": "https",
            "direction": direction,
            "bytes": size,
            "http_status_code": data["http_code"],
            "connectivity": {"dns": "success", "tls": "success", "http": "success"},
            "bandwidth_mib_per_second": size / total / 1024**2,
            "dns_seconds": dns,
            "connect_seconds": connect - dns,
            "tls_seconds": tls - connect,
            "ttfb_seconds": ttfb,
            "total_seconds": total,
        }
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, BenchmarkError):
            raise
        raise BenchmarkError("HTTPS measurement fields are invalid") from None


def _open_directory(path: Path) -> int:
    """Reject symlinks in every component, including an existing final directory."""
    if not path.is_absolute() or ".." in path.parts:
        raise BenchmarkError("directory must be absolute without parent traversal")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _private_write(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _run(
    args: list[str], output: Path, label: str, timeout: int, *, pass_fds=(), file_size_limit: int | None = None
) -> str:
    """No shell, no inherited stdin, bounded lifetime including child processes."""
    with (output / f"{label}.stdout").open("xb") as stdout, (output / f"{label}.stderr").open("xb") as stderr:
        os.chmod(stdout.name, 0o600)
        os.chmod(stderr.name, 0o600)
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
        environment.update({"LC_ALL": "C", "LANG": "C"})

        def child_limits():
            # Also bounds chunked HTTP bodies on older curl versions whose
            # --max-filesize only checks Content-Length. Applies to regular
            # response files, not /dev/null, and is inherited by descendants.
            if file_size_limit is not None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_size_limit, file_size_limit))

        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=environment,
            start_new_session=True,
            pass_fds=pass_fds,
            preexec_fn=child_limits if file_size_limit is not None else None,
        )
        try:
            code = process.wait(timeout=timeout)
        except BaseException as error:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            if not isinstance(error, Exception):
                raise
            raise BenchmarkError("measurement process interrupted or timed out") from None
        finally:
            # A wrapper must not leave a background measurement child alive.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if code != 0:
        raise BenchmarkError("measurement process failed; see private evidence")
    try:
        return (output / f"{label}.stdout").read_text()
    except (UnicodeError, OSError):
        raise BenchmarkError("measurement process output is unreadable") from None


def _tool(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise BenchmarkError(f"required executable unavailable: {name}")
    return value


def _aggregate(repetitions: list[dict]) -> dict:
    if not repetitions:
        return {}
    names = set.intersection(*(set(item) for item in repetitions))
    return {
        name: summarize_values([item[name] for item in repetitions])
        for name in sorted(names)
        if all(type(item[name]) in (int, float) for item in repetitions)
    }


def _disk(config: dict, output: Path) -> dict:
    if not isinstance(config.get("directory"), str) or "\x00" in config["directory"]:
        raise BenchmarkError("disk directory must be explicitly configured")
    directory = Path(config["directory"])
    size = _integer(config.get("size_mib", 1024), "disk size MiB", 16, 16384) * 1024**2
    duration = _integer(config.get("runtime_seconds", 10), "disk duration", 2, 300)
    repeats = _integer(config.get("repetitions", 3), "disk repetitions", 1, 10)
    depth = _integer(config.get("iodepth", 16), "disk IO depth", 1, 128)
    system = platform.system()
    if system not in ("Linux", "Darwin"):
        raise BenchmarkError("direct disk measurement requires Linux or Darwin")
    fio = _tool("fio")
    if not re.fullmatch(
        r"fio-\d+\.\d+(?:[.\-+][A-Za-z0-9.\-+]+)?\s*", _run([fio, "--version"], output, "fio-version", 10)
    ):
        raise BenchmarkError("executable is not the fio IO benchmark")
    parent = _open_directory(directory)
    scratch = f".palimpsest-benchmark-{uuid.uuid4().hex}"
    fd = None
    identity = None
    try:
        fd = os.open(scratch, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        identity = os.fstat(fd)
        os.ftruncate(fd, size)
        # Reopen only the inherited, pinned regular inode, not a replaceable path.
        filename = f"/proc/self/fd/{fd}" if system == "Linux" else f"/dev/fd/{fd}"
        engine = "libaio" if system == "Linux" else "posixaio"
        common = [
            fio,
            "--name=environment",
            f"--filename={filename}",
            "--allow_file_create=0",
            "--direct=1",
            f"--ioengine={engine}",
            f"--iodepth={depth}",
            "--numjobs=1",
            "--max-jobs=1",
            f"--size={size}",
            "--eta=never",
            "--output-format=json",
            "--percentile_list=95:99",
        ]
        initialized = _json(
            _run(common + ["--rw=write", "--bs=1m", "--end_fsync=1"], output, "fio-initialize", 600, pass_fds=(fd,))
        )
        try:
            jobs = initialized["jobs"]
            if (
                len(jobs) != 1
                or type(jobs[0]["error"]) is not int
                or jobs[0]["error"] != 0
                or jobs[0]["write"]["io_bytes"] != size
            ):
                raise BenchmarkError("fio scratch initialization incomplete")
            _integer(jobs[0]["write"].get("total_ios"), "initialization IO count", 2, 2**63 - 1)
            _number(jobs[0]["write"].get("runtime"), "initialization runtime", positive=True)
        except (KeyError, TypeError):
            raise BenchmarkError("fio scratch initialization invalid") from None
        result = {
            "status": "success",
            "protocol": "fio-direct",
            "ioengine": engine,
            "direct": True,
            "scope": "selected-filesystem-regular-file-not-physical-osd",
            "size_bytes": size,
            "runtime_seconds": duration,
            "requested_repetitions": repeats,
            "iodepth": depth,
            "cache_scope": "direct IO requested; device/controller caches are not flushed",
            "profiles": {},
            "issues": [],
        }
        result["device_id"] = identity.st_dev
        if system == "Darwin":
            result["cache_scope"] = (
                "fio posixaio direct uses Darwin F_NOCACHE; not Linux O_DIRECT or physical-media isolation"
            )
        for name, rw, direction, block in (
            ("sequential-read", "read", "read", 1024**2),
            ("sequential-write", "write", "write", 1024**2),
            ("random-read-4k", "randread", "read", 4096),
            ("random-write-4k", "randwrite", "write", 4096),
        ):
            profile = {
                "status": "success",
                "protocol": "fio-direct",
                "direction": direction,
                "block_size_bytes": block,
                "repetitions": [],
                "issues": [],
            }
            for repetition in range(repeats):
                label = f"fio-{name}-{repetition + 1}"
                try:
                    text = _run(
                        common
                        + [
                            f"--rw={rw}",
                            f"--bs={block}",
                            "--time_based=1",
                            f"--runtime={duration}",
                            f"--write_bw_log={output / label}",
                            "--log_avg_msec=1000",
                        ],
                        output,
                        label,
                        duration + 30,
                        pass_fds=(fd,),
                    )
                    measurement = parse_fio(_json(text), direction, duration)
                    log = output / f"{label}_bw.1.log"
                    if log.exists():
                        os.chmod(log, 0o600)
                        measurement["interval_summary"] = parse_fio_intervals(log.read_text(), direction)
                    else:
                        measurement["intervals_status"] = "not-measured"
                    measurement["proof"] = f"{label}.stdout"
                    profile["repetitions"].append(measurement)
                except (BenchmarkError, OSError):
                    profile["status"] = "failed"
                    profile["issues"].append(f"{label}: measurement failed; see private evidence")
            profile["aggregate"] = _aggregate(profile["repetitions"])
            if profile["status"] != "success":
                result["status"] = "failed"
            result["profiles"][name] = profile
        return result
    finally:
        if fd is not None:
            os.close(fd)
        try:
            if identity is not None:
                try:
                    current = os.stat(scratch, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is not None and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(scratch, dir_fd=parent)
        finally:
            os.close(parent)


def _url(value) -> str:
    try:
        parsed = urlsplit(value)
        if (
            not isinstance(value, str)
            or len(value) > 8192
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
        ):
            raise BenchmarkError("explicit credential-free HTTPS endpoint required")
        _ = parsed.port  # Access validates an explicit malformed/out-of-range port.
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise BenchmarkError("external DNS measurement requires a hostname, not an IP literal")
        return value
    except (ValueError, TypeError, AttributeError):
        raise BenchmarkError("explicit credential-free HTTPS endpoint required") from None


def _external(config: dict, output: Path) -> dict:
    download, upload = _url(config.get("download_url")), _url(config.get("upload_url"))
    sizes = {
        "download": _integer(config.get("download_bytes", 16777216), "download bytes", 1024, 64 * 1024**2),
        "upload": _integer(config.get("upload_bytes", 4194304), "upload bytes", 1024, 64 * 1024**2),
    }
    repeats = _integer(config.get("repetitions", 3), "external repetitions", 1, 10)
    curl = _tool("curl")
    result = {
        "status": "success",
        "protocol": "https",
        "requested_repetitions": repeats,
        "directions": {},
        "issues": [],
        "scope": "explicit-endpoint-verified-https-no-redirects",
    }
    payload = output / "upload-payload.bin"
    fd = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        remaining = sizes["upload"]
        while remaining:
            chunk = os.urandom(min(remaining, 65536))
            stream.write(chunk)
            remaining -= len(chunk)
    fields = (
        "http_code",
        "ssl_verify_result",
        "num_redirects",
        "size_download",
        "size_upload",
        "time_total",
        "time_namelookup",
        "time_connect",
        "time_appconnect",
        "time_starttransfer",
    )
    # Explicit fields work with curl versions predating %{json}; URLs are never exported.
    template = "{" + ",".join(f'"{field}":%{{{field}}}' for field in fields) + "}"
    try:
        for direction, url in (("download", download), ("upload", upload)):
            section = {
                "status": "success",
                "protocol": "https",
                "direction": direction,
                "repetitions": [],
                "issues": [],
            }
            for repetition in range(repeats):
                label = f"https-{direction}-{repetition + 1}"
                response = output / f"{label}.response"
                response_fd = os.open(response, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(response_fd)
                response_bound = sizes["download"] if direction == "download" else 65536
                # --disable is first: user curlrc must not add redirects/auth/insecure/proxies.
                args = [
                    curl,
                    "--disable",
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--max-redirs",
                    "0",
                    "--noproxy",
                    "*",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    "120",
                    "--max-filesize",
                    str(response_bound),
                    "--output",
                    str(response),
                    "--write-out",
                    template,
                ]
                if direction == "upload":
                    args += [
                        "--request",
                        "POST",
                        "--header",
                        "Content-Type: application/octet-stream",
                        "--data-binary",
                        f"@{payload}",
                    ]
                args += ["--url", url]
                try:
                    measurement = parse_curl(
                        _json(_run(args, output, label, 125, file_size_limit=response_bound)),
                        direction,
                        sizes[direction],
                    )
                    measurement["proof"] = f"{label}.stdout"
                    section["repetitions"].append(measurement)
                except (BenchmarkError, OSError):
                    section["status"] = "failed"
                    section["issues"].append(f"{label}: measurement failed; see private evidence")
            section["aggregate"] = _aggregate(section["repetitions"])
            if section["status"] != "success":
                result["status"] = "failed"
            result["directions"][direction] = section
        return result
    finally:
        payload.unlink()


def _internal(config: dict, output: Path) -> dict:
    try:
        address = ipaddress.ip_address(config.get("host", ""))
    except ValueError:
        raise BenchmarkError("internal target must be an explicit private IP address") from None
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        raise BenchmarkError("internal target must be a routable private IP address")
    port = _integer(config.get("port", 5201), "iperf3 port", 1, 65535)
    duration = _integer(config.get("duration_seconds", 10), "internal duration", 2, 300)
    repeats = _integer(config.get("repetitions", 3), "internal repetitions", 1, 10)
    parallel = _integer(config.get("parallel", 1), "iperf3 parallel streams", 1, 16)
    if config.get("protocol", "tcp") != "tcp":
        raise BenchmarkError("internal UDP is not supported by this measurement schema")
    iperf = _tool("iperf3")
    result = {
        "status": "success",
        "protocol": "tcp",
        "requested_repetitions": repeats,
        "parallel": parallel,
        "directions": {},
        "issues": [],
        "udp": {"status": "not-measured", "reason": "UDP was not requested/supported"},
    }
    for reverse, direction in ((False, "send"), (True, "receive")):
        section = {"status": "success", "protocol": "tcp", "direction": direction, "repetitions": [], "issues": []}
        for repetition in range(repeats):
            label = f"iperf3-{direction}-{repetition + 1}"
            args = [
                iperf,
                "--client",
                str(address),
                "--port",
                str(port),
                "--time",
                str(duration),
                "--parallel",
                str(parallel),
                "--json",
                "--interval",
                "1",
                "--connect-timeout",
                "10000",
            ]
            if reverse:
                args.append("--reverse")
            try:
                measurement = parse_iperf(_json(_run(args, output, label, duration + 30)), reverse, duration)
                measurement["proof"] = f"{label}.stdout"
                section["repetitions"].append(measurement)
            except (BenchmarkError, OSError):
                section["status"] = "failed"
                section["issues"].append(f"{label}: measurement failed; see private evidence")
        section["aggregate"] = _aggregate(section["repetitions"])
        if section["status"] != "success":
            result["status"] = "failed"
        result["directions"][direction] = section
    return result


def run_environment(config: dict, output: Path) -> dict:
    """Create an exclusive private evidence directory and a durable result.json.

    Invalid output locations raise (nothing is overwritten). Invalid config or
    unavailable tools produce failed records, never successful empty summaries.
    """
    output = Path(output).absolute()
    parent = _open_directory(output.parent)
    try:
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
    finally:
        os.close(parent)
    started, clock = time.time(), time.monotonic()
    result = {
        "schema": "palimpsest.environment-result.v1",
        "status": "failed",
        "started_unix_seconds": started,
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "hostname": socket.gethostname(),
            "python": platform.python_version(),
        },
        "disk": {"status": "not-measured"},
        "network": {"status": "failed", "external": {"status": "not-measured"}, "internal": {"status": "not-measured"}},
        "issues": [],
    }
    valid = isinstance(config, dict) and config.get("schema") == "palimpsest.benchmark-environment.v1"
    if not valid:
        result["issues"].append("environment configuration schema is invalid")
    else:
        disk = config.get("disk")
        network = config.get("network")
        sections = [
            ("disk", disk, _disk),
            ("external", network.get("external") if isinstance(network, dict) else None, _external),
            ("internal", network.get("internal") if isinstance(network, dict) else None, _internal),
        ]
        for name, section, runner in sections:
            target = result if name == "disk" else result["network"]
            if not isinstance(section, dict):
                target[name] = {"status": "not-measured", "issues": [f"{name}: explicit configuration is required"]}
                result["issues"].append(f"{name}: explicit configuration is required")
                continue
            try:
                target[name] = runner(section, output)
            except (BenchmarkError, OSError):
                target[name] = {
                    "status": "failed",
                    "issues": [f"{name}: measurement prerequisites or execution failed; see private evidence"],
                }
            if target[name]["status"] != "success":
                result["issues"].append(f"{name}: required measurement failed")
        result["network"]["status"] = (
            "success"
            if all(result["network"][name]["status"] == "success" for name in ("external", "internal"))
            else "failed"
        )
        if result["disk"]["status"] == result["network"]["status"] == "success":
            result["status"] = "success"
    result["duration_seconds"] = time.monotonic() - clock
    result["wall_duration_seconds"] = time.time() - started
    if abs(result["wall_duration_seconds"] - result["duration_seconds"]) > 15:
        result["status"] = "failed"
        result["issues"].append("controller wall/monotonic disagreement; environment qualification invalid")
    _private_write(output / "result.json", json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return result


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = _json(args.config.read_text())
        result = run_environment(config, args.output)
    except (BenchmarkError, OSError, UnicodeError):
        print("environment benchmark could not initialize; inspect private inputs/output", file=sys.stderr)
        return 2
    print(json.dumps({"schema": result["schema"], "status": result["status"]}))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
