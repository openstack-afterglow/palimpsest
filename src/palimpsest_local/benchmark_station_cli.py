"""Fixed resident measurement-station commands; credentials stay on private stdin."""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path

from .errors import ArtifactValidationError
from .state import atomic_write_json


def configure_parser(parser):
    actions = parser.add_subparsers(dest="station_action", required=True)
    for action in ("plan", "create", "sample", "run", "status", "report", "repair-access", "disk", "cleanup"):
        command = actions.add_parser(action)
        if action in {"plan", "create"}:
            command.add_argument("--config", type=Path, required=True)
        if action != "plan":
            command.add_argument("--output", type=Path, required=True)
        if action == "disk":
            command.add_argument(
                "--checkpoint-output",
                type=Path,
                help="new explicitly selected disk checkpoint root; original attempts are never overwritten or replayed",
            )
        if action in {"create", "sample", "run", "repair-access", "disk", "cleanup"}:
            command.add_argument(
                "--auth-stdin",
                action="store_true",
                help="consume original-caller auth through private stdin, never argv/environment/files",
            )
        if action == "run":
            command.add_argument(
                "--duration-seconds",
                type=int,
                default=7200,
                help="finite active-experiment window; station is retained afterward",
            )
    return parser


def _load(path):
    from .benchmark_workflow import load_config

    return load_config(path)


def _summary(output):
    from .benchmark_station_measurements import latest_measurement

    output = Path(output)
    state = _load(output / "station.json")
    sample = latest_measurement(output / "measurements", max_age_seconds=3600)
    compact = {
        key: sample.get(key)
        for key in (
            "schema",
            "status",
            "station",
            "attempt_id",
            "started_unix_seconds",
            "finished_unix_seconds",
            "interval_seconds",
            "cache",
            "sample_reference",
            "issues",
        )
    }
    compact["provider_averages"] = sample.get("environment", {}).get("network", {}).get("external", {}).get("combined")
    return {
        "station": state,
        "latest_measurement": compact,
        "retained": True,
        "report": str(output / "measurements" / "overview.html"),
    }


def dispatch(args):
    from .benchmark_scenarios import connection_from_stdin
    from .benchmark_station_cloud import StationCloud, validate_station
    from .benchmark_station_measurements import render_measurement, run_measurement

    action = args.station_action
    if action == "run" and (type(args.duration_seconds) is not int or not 1 <= args.duration_seconds <= 86400):
        raise ArtifactValidationError("station active-experiment duration must be 1..86400 seconds")
    if action in {"sample", "disk", "repair-access", "cleanup", "run"}:
        from .benchmark_station_cloud import station_operation

        # Pre-auth busy rejection is advisory; the operation itself holds the
        # same fence for its full lifetime, closing the race after this check.
        with station_operation(args.output):
            pass
    if action == "plan":
        config = _load(args.config)
        validate_station(config)
        result = {
            "schema": "palimpsest.benchmark-station-plan.v1",
            "cloud_effects": 0,
            "creates_once": 1,
            "vcpus": 4,
            "ram_mib": 8192,
            "boot_gib": 20,
            "data_gib": 500,
            "interval_seconds": config["measurement"].get("interval_seconds", 3600),
            "retained": True,
        }
    elif action in {"status", "report"}:
        result = _summary(args.output)
        if action == "report":
            from .benchmark_station_measurements import latest_measurement

            paths = render_measurement(latest_measurement(args.output / "measurements", max_age_seconds=3600))
            for name, text in paths.items():
                if name not in {"overview.html", "report.md", "issues.md"}:
                    raise ArtifactValidationError("unexpected station report path")
                destination = args.output / "measurements" / name
                destination.parent.mkdir(mode=0o700, exist_ok=True)
                destination.write_text(text)
            result = {
                "report": str(args.output / "measurements" / "overview.html"),
                "status": result["latest_measurement"].get("status"),
                "retained": True,
            }
    else:
        connection = connection_from_stdin() if args.auth_stdin else None
        try:
            config = _load(args.config) if action == "create" else _load(args.output / "station-config.json")
            if action == "run" and args.duration_seconds > config.get("deadline_seconds", 7200):
                raise ArtifactValidationError("active station window exceeds its configured adapter deadline")
            station = StationCloud(config, args.output, connection=connection)
            if action == "create":
                result = station.create()
            elif action == "cleanup":
                result = station.cleanup()
            elif action == "sample":
                result = run_measurement(station.cloud, station.resource(), config, args.output / "measurements")
            elif action == "repair-access":
                result = station.repair_access()
            elif action == "disk":
                from .benchmark_station_disk import run_disk_sample

                result = run_disk_sample(station, args.checkpoint_output or args.output / "disk-measurements")
            else:
                from .benchmark_scenarios import _executor_lock

                deadline = time.monotonic() + args.duration_seconds
                stopped = False

                def stop(*_):
                    nonlocal stopped
                    stopped = True

                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                with _executor_lock(args.output):
                    resource = station.resource()
                    state = {
                        "status": "running",
                        "started_unix_seconds": time.time(),
                        "duration_seconds": args.duration_seconds,
                        "interval_seconds": config["measurement"].get("interval_seconds", 3600),
                    }
                    atomic_write_json(args.output / "worker.json", state)
                    print(
                        json.dumps(
                            {
                                "station_worker_ready": True,
                                "output": str(args.output),
                                "duration_seconds": args.duration_seconds,
                            }
                        ),
                        flush=True,
                    )
                    while not stopped and time.monotonic() < deadline:
                        latest = run_measurement(station.cloud, resource, config, args.output / "measurements")
                        state.update(last_result_status=latest.get("status"), heartbeat_unix_seconds=time.time())
                        atomic_write_json(args.output / "worker.json", state)
                        next_due = latest.get("scheduler", {}).get(
                            "next_eligible_unix_seconds",
                            latest.get("started_unix_seconds", time.time())
                            + config["measurement"].get("interval_seconds", 3600),
                        )
                        next_due = max(config["measurement"].get("not_before_unix_seconds", 0), next_due)
                        while not stopped and time.monotonic() < deadline and time.time() < next_due:
                            time.sleep(max(0, min(30.0, deadline - time.monotonic(), next_due - time.time())))
                            state["heartbeat_unix_seconds"] = time.time()
                            atomic_write_json(args.output / "worker.json", state)
                    state.update(status="stopped", finished_unix_seconds=time.time(), retained=True)
                    atomic_write_json(args.output / "worker.json", state)
                    result = state
        finally:
            if connection is not None:
                connection.close()
    print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
    return 1 if result.get("status") in {"failed", "held"} else 0


def main(argv=None):
    parser = configure_parser(
        argparse.ArgumentParser(
            description="One retained 4CPU/8GiB station, 500GiB data disk, two-provider hourly measurements"
        )
    )
    return dispatch(parser.parse_args(argv))
