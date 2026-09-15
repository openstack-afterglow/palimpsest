"""Separate qualification caller of the product client; no runtime patches."""

import json
import os
import sys
from pathlib import Path

from palimpsest_local.oci_monitor_client import MonitorClient
from palimpsest_local.oci_monitor_ipc import MonitorExecEndpoint, MonitorPreActivationBinding
from palimpsest_local.state import StatePaths


def main():
    request = json.load(sys.stdin)
    roots = StatePaths(Path(request["config"]), Path(request["state"]))
    binding = MonitorPreActivationBinding.from_dict(request["binding"])
    endpoint = MonitorExecEndpoint.from_dict(request["endpoint"])
    with MonitorClient(roots, binding, endpoint) as client:
        result = client.stop_and_wait(timeout=35)
    print(
        json.dumps(
            {
                "pid": os.getpid(),
                "returncode": result.returncode,
                "exit_code": result.exit_code,
                "signal_number": result.signal_number,
                "category": result.category.value,
            }
        )
    )


if __name__ == "__main__":
    main()
