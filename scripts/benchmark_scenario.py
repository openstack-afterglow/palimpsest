#!/usr/bin/env python3
"""Fixed benchmark entrypoint; also shared by per-scenario scripts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from palimpsest_local.benchmark_workflow import load_config  # noqa: E402


def main(profile: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reproducible API/SDK benchmark: plan, enqueue, execute or inspect; no generated hooks"
    )
    parser.add_argument("action", choices=("list", "plan", "run", "submit", "status", "report", "cleanup"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path, help="existing resident benchmark queue for submit")
    parser.add_argument(
        "--auth-stdin", action="store_true", help="private original-caller SDK auth JSON on stdin for run/cleanup only"
    )
    args = parser.parse_args()
    if args.auth_stdin and args.action not in {"run", "cleanup"}:
        parser.error("--auth-stdin applies only to run/cleanup; resident submission uses controller-private SDK config")
    if args.action in {"plan", "run", "submit"}:
        if args.config is None:
            parser.error("--config is required")
        config = load_config(args.config)
        if profile is not None and config.get("scenario") != profile:
            parser.error("configuration scenario must match this fixed script: " + profile)
    if args.action == "submit":
        if args.root is None:
            parser.error("submit requires --root")
        from palimpsest_local.benchmark_worker import submit

        print(json.dumps(submit(args.root, config), sort_keys=True))
        return 0
    if args.action in {"run", "status", "report", "cleanup"} and args.output is None:
        parser.error("--output is required")
    from palimpsest_local.benchmark_cli import dispatch

    args.benchmark_mode = "scenario"
    args.scenario_action = args.action
    return dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
