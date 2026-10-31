#!/usr/bin/env python3
"""Fixed tensorflow-cuda benchmark; configuration and durable evidence are mandatory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark_scenario import main

if __name__ == "__main__":
    raise SystemExit(main("tensorflow-cuda"))
