"""Guest-observed boot I/O samples and honest repeated-run summaries.

Counters describe guest virtual devices/interfaces, never Ceph physical disks or
link capacity. Provider image copy before guest startup is outside this window.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from .errors import ArtifactValidationError

GUEST_SAMPLER_SOURCE = r"""#!/usr/bin/python3
import json
import os
import re
import sys
import time
from pathlib import Path

output = Path(sys.argv[1])
seconds = int(sys.argv[2])
if not 1 <= seconds <= 14400 or not output.is_absolute():
    raise SystemExit('invalid sampler bounds')
output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
deadline = time.monotonic() + seconds
with os.fdopen(fd, 'w', buffering=1) as stream:
    while True:
        blocks = {}
        for line in Path('/proc/diskstats').read_text().splitlines():
            fields = line.split()
            name = fields[2]
            # Whole virtual disks only; do not count their partitions a second time.
            if re.fullmatch(r'(?:[vsh]d[a-z]+|xvd[a-z]+|nvme\d+n\d+)', name):
                blocks[name] = {'read_ios': int(fields[3]), 'read_bytes': int(fields[5]) * 512,
                                'write_ios': int(fields[7]), 'write_bytes': int(fields[9]) * 512,
                                'io_milliseconds': int(fields[12])}
        networks = {}
        for interface in Path('/sys/class/net').iterdir():
            if interface.name == 'lo' or not interface.is_dir():
                continue
            counters = interface / 'statistics'
            networks[interface.name] = {key: int((counters / key).read_text())
                                       for key in ('rx_bytes', 'tx_bytes')}
        stream.write(json.dumps({'schema': 'palimpsest.guest-io-sample.v1', 'boot_id': boot_id,
                                 'monotonic_seconds': time.monotonic(), 'blocks': blocks,
                                 'networks': networks}, sort_keys=True) + '\n')
        if time.monotonic() >= deadline or Path('/run/palimpsest-metrics.stop').exists():
            stream.flush()
            os.fsync(stream.fileno())
            break
        time.sleep(1)
"""


def _error(message: str) -> None:
    raise ArtifactValidationError("Nova measurement: " + message)


def summarize_samples(samples: list[dict[str, Any]], *, end_monotonic: float | None = None) -> dict:
    """Aggregate measured counter deltas, rejecting mixed boots, resets and gaps in devices."""
    if end_monotonic is not None:
        samples = [sample for sample in samples if sample.get("monotonic_seconds", math.inf) <= end_monotonic]
    if len(samples) < 2:
        _error("at least two actual samples are required")
    first, last = samples[0], samples[-1]
    boot_id = first.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id:
        _error("boot identity is absent")
    previous = None
    device_fields = {
        "blocks": ("read_bytes", "write_bytes", "read_ios", "write_ios", "io_milliseconds"),
        "networks": ("rx_bytes", "tx_bytes"),
    }
    maximum = {group: {} for group in device_fields}
    for sample in samples:
        stamp = sample.get("monotonic_seconds")
        if (
            sample.get("schema") != "palimpsest.guest-io-sample.v1"
            or sample.get("boot_id") != boot_id
            or type(stamp) not in (int, float)
            or not math.isfinite(stamp)
        ):
            _error("sample schema, boot or time is invalid")
        if previous is not None and stamp <= previous["monotonic_seconds"]:
            _error("sample clock must increase strictly")
        for group, fields in device_fields.items():
            current = sample.get(group)
            if not isinstance(current, dict) or set(current) != set(first.get(group, {})):
                _error("measured device set changed")
            for name, counters in current.items():
                if not isinstance(counters, dict) or set(counters) != set(fields):
                    _error("counter fields are invalid")
                for field in fields:
                    value = counters[field]
                    if type(value) is not int or value < 0:
                        _error("counters must be nonnegative integers")
                    if previous is not None:
                        delta = value - previous[group][name][field]
                        if delta < 0:
                            _error("counter reset prevents a valid throughput measurement")
                        if field.endswith("bytes"):
                            rates = maximum[group].setdefault(name, {})
                            rate = delta / (stamp - previous["monotonic_seconds"]) / 1024**2
                            rates[field] = max(rates.get(field, 0.0), rate)
        previous = sample
    duration = last["monotonic_seconds"] - first["monotonic_seconds"]
    result = {
        "scope": "guest-virtual-device-counters",
        "boot_id": boot_id,
        "samples": len(samples),
        "start_seconds_after_boot": first["monotonic_seconds"],
        "end_seconds_after_boot": last["monotonic_seconds"],
        "duration_seconds": duration,
        "blocks": {},
        "networks": {},
        "not_measured": ["provider-image-copy-before-guest-sampler", "ceph-physical-io", "link-capacity"],
    }
    for group, fields in device_fields.items():
        for name in first[group]:
            delta = {field: last[group][name][field] - first[group][name][field] for field in fields}
            result[group][name] = {
                "delta": delta,
                "average_mib_per_second": {
                    field: value / duration / 1024**2 for field, value in delta.items() if field.endswith("bytes")
                },
                "maximum_sample_mib_per_second": maximum[group][name],
            }
    result["maximum_sampling_gap_seconds"] = max(
        right["monotonic_seconds"] - left["monotonic_seconds"]
        for left, right in zip(samples, samples[1:], strict=False)
    )
    return result


def summarize_runs(runs: list[dict], *, expected: int = 5) -> dict:
    """Never turn failures or missing repetitions into five successful boots."""
    if type(expected) is not int or expected <= 0:
        _error("expected repetitions must be positive")
    identities = [run.get("run_id") for run in runs]
    if any(not isinstance(value, str) or not value for value in identities) or len(set(identities)) != len(identities):
        _error("each attempt requires a unique run identity")
    successful = []
    boots: set[str] = set()
    for run in runs:
        if run.get("status") != "success":
            continue
        if run.get("cpu_fallback") is not False or not run.get("boot_id") or not run.get("gpu_uuid"):
            _error("successful boot lacks actual GPU and boot identity")
        if run["boot_id"] in boots:
            _error("the same boot cannot count as independent repeated measurements")
        boots.add(run["boot_id"])
        seconds = run.get("request_to_first_nvidia_smi_seconds")
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            _error("successful boot lacks a measured first nvidia-smi time")
        successful.append(seconds)
    return {
        "expected_successes": expected,
        "attempts": len(runs),
        "successes": len(successful),
        "failures": len(runs) - len(successful),
        "complete": len(successful) == expected,
        "request_to_first_nvidia_smi_seconds": {
            "raw": successful,
            "minimum": min(successful),
            "median": statistics.median(successful),
            "maximum": max(successful),
        }
        if successful
        else None,
    }
