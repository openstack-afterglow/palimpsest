#!/usr/bin/env python3
"""Create, inspect or ordinarily remove the fresh approval-owned benchmark peer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from palimpsest_local.benchmark_scenario_peer import cleanup_peer, create_peer, status_peer  # noqa: E402
from palimpsest_local.benchmark_workflow import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "status", "cleanup"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--auth-stdin", action="store_true", help="read bounded credentials from private stdin, never argv or files"
    )
    args = parser.parse_args()
    if args.action == "create" and args.config is None:
        parser.error("create requires --config")
    if args.action != "create" and args.config is not None:
        parser.error("status/cleanup use the original output receipts, not --config")
    if args.action == "status" and args.auth_stdin:
        parser.error("status is offline and does not accept authentication")
    connection = None
    try:
        if args.auth_stdin:
            from palimpsest_local.benchmark_scenarios import connection_from_stdin

            connection = connection_from_stdin()
        if args.action == "create":
            result = create_peer(load_config(args.config), args.output, connection=connection)
        elif args.action == "cleanup":
            result = cleanup_peer(args.output, connection=connection)
        else:
            result = status_peer(args.output)
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0 if result["status"] in {"ready", "deleted"} else 1
    except Exception:
        # SDK exception strings can contain credential-bearing HTTP headers/URLs.
        print(
            json.dumps(
                {
                    "status": "held",
                    "issue": "Peer action failed; inspect original private receipts. No automatic replay.",
                },
                separators=(",", ":"),
            )
        )
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
