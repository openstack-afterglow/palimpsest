"""Compatibility facade for the cloud VM runtime public API.

New first-party code should import :mod:`palimpsest_local.cloud_runtime`
directly.  This module intentionally contains no lifecycle implementation;
it preserves the established import surface while runtime dispatch is split
out in later changes.
"""

from __future__ import annotations

from .cloud_runtime import (
    commit,
    commit_run,
    create_and_validate_overlay,
    exec_command,
    inspect_run,
    logs,
    ps,
    receive_serial_builder_output,
    reconcile,
    rm,
    run,
    shell_command,
    start,
    start_serial_builder,
    stop,
)

__all__ = (
    "commit",
    "commit_run",
    "create_and_validate_overlay",
    "exec_command",
    "inspect_run",
    "logs",
    "ps",
    "receive_serial_builder_output",
    "reconcile",
    "rm",
    "run",
    "shell_command",
    "start",
    "start_serial_builder",
    "stop",
)
