"""Explicit environmental benchmarks, verified workflows and split reporting."""

from __future__ import annotations

import json
import signal
from pathlib import Path

from .benchmark_workflow import load_config


def add_parser(commands) -> None:
    parser = commands.add_parser("benchmark", help="measure disk/network baselines and five-method workflows")
    modes = parser.add_subparsers(dest="benchmark_mode", required=True)
    for mode, description in (
        ("local", "measure disk and external/internal network on this execution host"),
        ("openstack", "API-verify an explicit Nova guest and measure inside that guest"),
        ("workflow", "require disk/network qualification before five explicit method commands"),
    ):
        command = modes.add_parser(mode, help=description)
        command.add_argument("--config", required=True, type=Path, help="operator-owned benchmark JSON configuration")
        command.add_argument(
            "--output", required=True, type=Path, help="new private evidence directory; existing jobs never replay"
        )
    report = modes.add_parser("report", help="render success-only one-page tables/HTML and separate issues")
    report.add_argument("results", nargs="+", type=Path, help="recorded environment/workflow result JSON files")
    report.add_argument(
        "--output", required=True, type=Path, help="new directory for report.md, overview.html and issues.md"
    )
    from .benchmark_station_cli import configure_parser

    configure_parser(
        modes.add_parser(
            "station", help="one retained4CPU/8GiB station,500GiB data disk and hourly two-provider calibration"
        )
    )
    scenario = modes.add_parser("scenario", help="reproducible API/SDK VM, ML and cluster scenarios")
    actions = scenario.add_subparsers(dest="scenario_action", required=True)
    actions.add_parser("list", help="show fixed workload profiles and required capabilities")
    for action in ("plan", "run"):
        command = actions.add_parser(
            action, help="validate without effects" if action == "plan" else "execute once with durable evidence"
        )
        command.add_argument("--config", required=True, type=Path)
        if action == "run":
            command.add_argument("--output", required=True, type=Path)
            command.add_argument(
                "--auth-stdin",
                action="store_true",
                help="read private original-caller v3token/v3password JSON from stdin, never argv/environment/files",
            )
    for action in ("status", "report", "cleanup"):
        command = actions.add_parser(
            action,
            help="inspect recorded state without replay" if action == "status" else action + " an existing owned run",
        )
        command.add_argument("--output", required=True, type=Path)
        if action == "cleanup":
            command.add_argument("--auth-stdin", action="store_true")
    for mode in ("init", "submit", "worker", "status"):
        command = modes.add_parser(mode, help="durable single-executor controller " + mode)
        command.add_argument(
            "--root", required=True, type=Path, help="private durable benchmark controller queue directory"
        )
        if mode == "submit":
            command.add_argument(
                "--config",
                required=True,
                type=Path,
                help="operator-authored workflow or standalone benchmark request JSON",
            )
        elif mode == "worker":
            command.add_argument(
                "--once", action="store_true", help="process current queue once, without waiting for future requests"
            )
        elif mode == "status":
            command.add_argument("job_id", help="original queued benchmark request ID; inspection never replays it")


def dispatch(args) -> int:
    mode = args.benchmark_mode
    if mode == "station":
        from .benchmark_station_cli import dispatch as station_dispatch

        return station_dispatch(args)
    if mode == "scenario":
        from .benchmark_scenarios import (
            cleanup_scenario,
            plan_scenario,
            render_scenario_report,
            run_scenario,
            scenario_status,
        )

        action = args.scenario_action
        if action == "list":
            from .benchmark_scenario_workloads import PROFILES

            result = PROFILES
        elif action == "plan":
            result = plan_scenario(load_config(args.config))
        elif action == "run":
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            if getattr(args, "auth_stdin", False):
                from .benchmark_scenarios import connection_from_stdin

                connection = connection_from_stdin()
                try:
                    result = run_scenario(load_config(args.config), args.output, connection=connection)
                finally:
                    connection.close()
            else:
                result = run_scenario(load_config(args.config), args.output)
            result = scenario_status(args.output)
        elif action == "cleanup":
            if getattr(args, "auth_stdin", False):
                from .benchmark_scenarios import connection_from_stdin

                connection = connection_from_stdin()
                try:
                    result = cleanup_scenario(args.output, connection=connection)
                finally:
                    connection.close()
            else:
                result = cleanup_scenario(args.output)
        else:
            recorded = load_config(args.output / "result.json")
            result = (
                scenario_status(args.output) if action == "status" else render_scenario_report(recorded, args.output)
            )
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 1 if isinstance(result, dict) and result.get("status") == "failed" else 0
    if mode in {"local", "openstack", "workflow", "worker"}:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    if mode in {"init", "submit", "worker", "status"}:
        from .benchmark_worker import initialize, status, submit, work

        if mode == "init":
            initialize(args.root)
            result = {"root": str(args.root), "phase": "initialized"}
        elif mode == "submit":
            result = submit(args.root, load_config(args.config))
        elif mode == "status":
            result = status(args.root, args.job_id)
        else:
            return work(args.root, once=args.once)
        print(json.dumps(result, sort_keys=True))
        return 0
    if mode == "report":
        from .benchmark_reporting import render_report

        args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
        rendered = render_report([load_config(p) for p in args.results])
        for key, name in (("markdown", "report.md"), ("html", "overview.html"), ("issues_markdown", "issues.md")):
            (args.output / name).write_text(rendered[key])
        print(
            json.dumps(
                {
                    "report": str(args.output / "report.md"),
                    "html": str(args.output / "overview.html"),
                    "issues": str(args.output / "issues.md"),
                }
            )
        )
        return 0
    config = load_config(args.config)
    if mode == "local":
        from .environment_benchmark import run_environment

        result = run_environment(config, args.output)
    elif mode == "openstack":
        from .benchmark_openstack import run_openstack_environment

        result = run_openstack_environment(config, args.output)
    else:
        from .benchmark_workflow import run_workflow

        result = run_workflow(config, args.output)
    if mode in {"local", "openstack"}:
        from .benchmark_reporting import render_report

        rendered = render_report([result])
        for key, name in (("markdown", "report.md"), ("html", "overview.html"), ("issues_markdown", "issues.md")):
            (args.output / name).write_text(rendered[key])
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="operation", required=True))
    dispatch_args = __import__("sys").argv[1:]
    raise SystemExit(dispatch(parser.parse_args(["benchmark", *dispatch_args])))
