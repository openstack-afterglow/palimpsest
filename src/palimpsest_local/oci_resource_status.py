"""Bounded, read-only Linux observations relevant to OCI worker RLIMIT_NPROC."""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Callable

try:  # The public command imports this module before issuing its typed platform error.
    import resource as _resource
except ImportError:  # pragma: no cover - exercised on platforms without resource(3)
    _resource = None

from .errors import UnsupportedPlatformError
from .oci_worker_limits import OCI_WORKER_NPROC_LIMIT

RESOURCE_STATUS_SCHEMA = "palimpsest.oci-resource-status.v1"
_PROC_ROOT = "/proc"
_MAX_PROC_ENTRIES = 65_536
_MAX_STATUS_BYTES = 16 * 1024
_REQUIRED_FIELDS = frozenset({"Pid", "Tgid", "Uid", "Threads"})


def _render_limit(value: int) -> int | None:
    assert _resource is not None
    return None if value == _resource.RLIM_INFINITY else value


def _prospective_limits(soft: int, hard: int) -> tuple[int, int]:
    assert _resource is not None
    target_hard = OCI_WORKER_NPROC_LIMIT if hard == _resource.RLIM_INFINITY else min(OCI_WORKER_NPROC_LIMIT, hard)
    target_soft = target_hard if soft == _resource.RLIM_INFINITY else min(soft, target_hard)
    return target_soft, target_hard


def _decimal(raw: bytes, *, maximum: int) -> int:
    if not raw or len(raw) > 20 or not raw.isascii() or not raw.isdigit() or (len(raw) > 1 and raw.startswith(b"0")):
        raise ValueError("non-canonical decimal")
    value = int(raw, 10)
    if value > maximum:
        raise ValueError("decimal out of range")
    return value


def _parse_status(payload: bytes, expected_pid: int) -> tuple[int, int]:
    fields: dict[bytes, bytes] = {}
    required = {name.encode("ascii") for name in _REQUIRED_FIELDS}
    for line in payload.splitlines():
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        if name in required:
            if name in fields:
                raise ValueError("duplicate status field")
            fields[name] = value.strip()
    if set(fields) != required:
        raise ValueError("missing status field")
    try:
        pid = _decimal(fields[b"Pid"], maximum=2**31 - 1)
        tgid = _decimal(fields[b"Tgid"], maximum=2**31 - 1)
        uid_parts = fields[b"Uid"].split()
        threads = _decimal(fields[b"Threads"], maximum=2**31 - 1)
        if len(uid_parts) != 4:
            raise ValueError
        uids = tuple(_decimal(part, maximum=2**32 - 1) for part in uid_parts)
    except ValueError as exc:
        raise ValueError("malformed status field") from exc
    if pid != expected_pid or tgid != expected_pid or threads <= 0:
        raise ValueError("inconsistent process status")
    return uids[0], threads


def _scan_proc(proc_root: str, real_uid: int) -> dict[str, int | bool | None]:
    counters = {
        "entries_examined": 0,
        "numeric_entries_examined": 0,
        "matching_processes_observed": 0,
        "matching_threads_observed": 0,
        "unavailable_entries": 0,
        "rejected_entries": 0,
        "scan_unavailable": False,
    }
    partial = False
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        proc_fd = os.open(proc_root, flags)
    except OSError:
        counters["scan_unavailable"] = True
        counters["partial"] = True
        counters["matching_processes_observed"] = None
        counters["matching_threads_observed"] = None
        return counters
    try:
        try:
            entries = os.scandir(proc_fd)
        except OSError:
            counters["scan_unavailable"] = True
            counters["partial"] = True
            counters["matching_processes_observed"] = None
            counters["matching_threads_observed"] = None
            return counters
        try:
            with entries:
                for entry in entries:
                    if counters["entries_examined"] >= _MAX_PROC_ENTRIES:
                        partial = True
                        break
                    counters["entries_examined"] += 1
                    if not entry.name.isascii() or not entry.name.isdecimal():
                        continue
                    counters["numeric_entries_examined"] += 1
                    pid = int(entry.name, 10)
                    pid_fd = status_fd = None
                    try:
                        pid_fd = os.open(entry.name, flags, dir_fd=proc_fd)
                        status_flags = os.O_RDONLY | os.O_CLOEXEC
                        if hasattr(os, "O_NOFOLLOW"):
                            status_flags |= os.O_NOFOLLOW
                        if hasattr(os, "O_NONBLOCK"):
                            status_flags |= os.O_NONBLOCK
                        status_fd = os.open("status", status_flags, dir_fd=pid_fd)
                        metadata = os.fstat(status_fd)
                        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_STATUS_BYTES:
                            raise ValueError("unsafe status file")
                        chunks: list[bytes] = []
                        remaining = _MAX_STATUS_BYTES + 1
                        while remaining:
                            chunk = os.read(status_fd, remaining)
                            if not chunk:
                                break
                            chunks.append(chunk)
                            remaining -= len(chunk)
                        payload = b"".join(chunks)
                        if len(payload) > _MAX_STATUS_BYTES or (payload and not payload.endswith(b"\n")):
                            raise ValueError("truncated or oversized status")
                        uid, threads = _parse_status(payload, pid)
                        if uid == real_uid:
                            counters["matching_processes_observed"] += 1
                            counters["matching_threads_observed"] += threads
                    except OSError as exc:
                        if exc.errno in {errno.ENOENT, errno.EACCES, errno.EPERM, errno.ESRCH}:
                            counters["unavailable_entries"] += 1
                        else:
                            counters["rejected_entries"] += 1
                        partial = True
                    except (ValueError, OverflowError):
                        counters["rejected_entries"] += 1
                        partial = True
                    finally:
                        if status_fd is not None:
                            os.close(status_fd)
                        if pid_fd is not None:
                            os.close(pid_fd)
        except OSError:
            counters["scan_unavailable"] = True
            partial = True
            if counters["entries_examined"] == 0:
                counters["matching_processes_observed"] = None
                counters["matching_threads_observed"] = None
    finally:
        os.close(proc_fd)
    counters["partial"] = partial
    return counters


def _build_resource_status(
    *,
    proc_root: str = _PROC_ROOT,
    getrlimit: Callable[[int], tuple[int, int]] | None = None,
    getuid: Callable[[], int] | None = None,
    platform: str = sys.platform,
) -> dict[str, object]:
    """Build the report; injectable arguments are private test seams."""
    if platform != "linux" or _resource is None or not hasattr(_resource, "RLIMIT_NPROC") or not hasattr(os, "getuid"):
        raise UnsupportedPlatformError("OCI resource status is supported only on Linux with RLIMIT_NPROC")
    limit_reader = getrlimit or _resource.getrlimit
    uid_reader = getuid or os.getuid
    soft, hard = limit_reader(_resource.RLIMIT_NPROC)
    target_soft, target_hard = _prospective_limits(soft, hard)
    real_uid = uid_reader()
    observation = _scan_proc(proc_root, real_uid)
    return {
        "schema": RESOURCE_STATUS_SCHEMA,
        "resource": "RLIMIT_NPROC",
        "identity_scope": "visible_process_leader_real_uid_thread_counts",
        "inherited": {"soft": _render_limit(soft), "hard": _render_limit(hard), "null_means_infinity": True},
        "worker": {
            "configured_ceiling": OCI_WORKER_NPROC_LIMIT,
            "prospective_soft": target_soft,
            "prospective_hard": target_hard,
            "prospective_effective_ceiling": target_soft,
        },
        "procfs_observation": observation,
        "admission_verdict": None,
        "limitations": [
            "procfs visibility is namespace-dependent, permission-dependent, partial, and racy",
            "leader Threads values are non-atomic observations and do not inspect heterogeneous per-thread credentials",
            "observed thread counts are not exact free slots and do not guarantee worker admission",
            "RLIMIT_NPROC may not constrain real UID 0 or processes with relevant capabilities",
            "cgroup, memory, and capability enforcement are not measured",
        ],
    }


def resource_status() -> dict[str, object]:
    return _build_resource_status()


__all__ = ["RESOURCE_STATUS_SCHEMA", "resource_status"]
