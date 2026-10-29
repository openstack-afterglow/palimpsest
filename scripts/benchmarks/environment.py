#!/usr/bin/env python3
"""One retained 4CPU/8GiB station and500GiB disk; hourly measurements, not per experiment."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from palimpsest_local.benchmark_station_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
