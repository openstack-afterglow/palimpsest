"""Bounded, read-only same-UID Darwin process identity; never signal a PID.

ABI: local MacOSX27.2.sdk libproc.h and sys/proc_info.h proc_bsdinfo.
Only argc argv strings are copied from KERN_PROCARGS2, never the environment.
Inaccessible, partial or changing observations are errors, not process absence.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from dataclasses import dataclass, field
from functools import cache

from .errors import StateError

_MAX_ARGUMENT_BYTES = 1024 * 1024
_MAX_ARGUMENTS = 4096
_MAX_PIDS = 65536
_PATH_BYTES = 4096
_PROC_PIDTBSDINFO = 3
_PROC_UID_ONLY = 4


class _BSDInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


@dataclass(frozen=True, slots=True)
class DarwinProcessIdentity:
    pid: int
    ppid: int
    uid: int
    pgid: int
    start_time_ns: int
    executable: str | None
    argv: tuple[str, ...] = field(repr=False)


@cache
def _libraries():
    if sys.platform != "darwin" or ctypes.sizeof(_BSDInfo) != 136:
        raise StateError("Darwin process observation ABI is unavailable")
    proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    proc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    proc.proc_pidinfo.restype = ctypes.c_int
    proc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc.proc_pidpath.restype = ctypes.c_int
    proc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    proc.proc_listpids.restype = ctypes.c_int
    libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX.
    maximum = ctypes.c_int()
    length = ctypes.c_size_t(ctypes.sizeof(maximum))
    ctypes.set_errno(0)
    if libc.sysctl(mib, 2, ctypes.byref(maximum), ctypes.byref(length), None, 0):
        raise StateError(f"Darwin argument bound query failed (errno={ctypes.get_errno()})")
    if length.value != ctypes.sizeof(maximum) or not 4 < maximum.value <= _MAX_ARGUMENT_BYTES:
        raise StateError("Darwin argument bound is unsupported")
    return proc, libc, maximum.value


def _info(pid: int) -> _BSDInfo | None:
    proc, _, _ = _libraries()
    value = _BSDInfo()
    ctypes.set_errno(0)
    count = proc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(value), ctypes.sizeof(value))
    error = ctypes.get_errno()
    if count == 0 and error == errno.ESRCH:
        return None
    if count != ctypes.sizeof(value) or value.pid != pid:
        raise StateError(f"Darwin process metadata unavailable for PID {pid} (errno={error})")
    if value.uid != os.geteuid() or value.start_usec >= 1_000_000 or value.start_sec == 0:
        raise StateError("Darwin process identity is not owned or has invalid birth metadata")
    return value


def _metadata(value: _BSDInfo) -> tuple[int, ...]:
    return value.pid, value.ppid, value.uid, value.pgid, value.start_sec, value.start_usec


def _path(pid: int) -> str | None:
    proc, _, _ = _libraries()
    value = ctypes.create_string_buffer(_PATH_BYTES)
    ctypes.set_errno(0)
    count = proc.proc_pidpath(pid, value, len(value))
    error = ctypes.get_errno()
    if count <= 0 and error == errno.ENOENT:
        # A running, unlinked executable can have no vnode path. Retain its
        # process identity in inventory; this is neither absence nor authority
        # to identify it as an expected executable.
        return None
    if count <= 0 or count >= len(value):
        raise StateError(f"Darwin executable observation unavailable for PID {pid} (errno={error})")
    result = os.fsdecode(value.value)
    if not result.startswith("/"):
        raise StateError("Darwin executable observation is not absolute")
    return result


def _nul_end(buffer, position: int, size: int) -> int:
    while position < size and buffer[position] != b"\0":
        position += 1
    if position >= size:
        raise StateError("Darwin argument observation is not NUL terminated")
    return position


def _argv(pid: int) -> tuple[str, ...]:
    _, libc, maximum = _libraries()
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2.
    value = ctypes.create_string_buffer(maximum)
    length = ctypes.c_size_t(maximum)
    ctypes.set_errno(0)
    if libc.sysctl(mib, 3, value, ctypes.byref(length), None, 0):
        raise StateError(f"Darwin argv observation unavailable for PID {pid} (errno={ctypes.get_errno()})")
    size = length.value
    if not 4 < size <= maximum:
        raise StateError("Darwin argument observation has an invalid size")
    argc = int.from_bytes(value[:4], sys.byteorder, signed=True)
    if not 0 < argc <= _MAX_ARGUMENTS:
        raise StateError("Darwin argument observation has an invalid count")
    position = _nul_end(value, 4, size) + 1  # Kernel's executable-path prefix.
    while position < size and value[position] == b"\0":
        position += 1
    args = []
    for _ in range(argc):
        end = _nul_end(value, position, size)
        args.append(os.fsdecode(value[position:end]))
        position = end + 1
    return tuple(args)


def _read_once(pid: int) -> DarwinProcessIdentity | None:
    before = _info(pid)
    if before is None:
        return None
    try:
        executable = _path(pid)
        argv = _argv(pid)
    except StateError:
        if _info(pid) is None:
            return None
        raise
    after = _info(pid)
    if after is None:
        return None
    if _metadata(before) != _metadata(after):
        raise StateError("Darwin process metadata changed during observation")
    return DarwinProcessIdentity(
        before.pid,
        before.ppid,
        before.uid,
        before.pgid,
        before.start_sec * 1_000_000_000 + before.start_usec * 1000,
        executable,
        argv,
    )


def _read_identity(pid: int) -> DarwinProcessIdentity | None:
    """Read complete metadata/argv twice, retaining an unavailable vnode path."""
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        raise StateError("Darwin process PID is invalid")
    first = _read_once(pid)
    if first is None:
        return None
    second = _read_once(pid)
    if second is None:
        return None
    if first != second:
        raise StateError("Darwin process identity changed between observations")
    return second


def read_process(pid: int) -> DarwinProcessIdentity | None:
    """Require exact executable identity; only observed kernel ESRCH is absence."""
    identity = _read_identity(pid)
    if identity is not None and identity.executable is None:
        raise StateError(f"Darwin executable observation unavailable for PID {pid} (errno={errno.ENOENT})")
    return identity


def list_processes() -> tuple[DarwinProcessIdentity, ...]:
    """Enumerate every same-UID identity, even when its vnode path is unavailable.

    Metadata and complete argv remain mandatory and stable. A None executable
    is explicitly unknown, never an executable match or an absent process.
    Never log the resulting argv table. Only relevant owned-instance identities
    belong in a caller's bounded evidence; no process environment is returned.
    """
    proc, _, _ = _libraries()
    ctypes.set_errno(0)
    needed = proc.proc_listpids(_PROC_UID_ONLY, os.geteuid(), None, 0)
    if needed <= 0 or needed % ctypes.sizeof(ctypes.c_int):
        raise StateError(f"Darwin PID enumeration unavailable (errno={ctypes.get_errno()})")
    capacity = needed // ctypes.sizeof(ctypes.c_int) + 256
    if capacity > _MAX_PIDS:
        raise StateError("Darwin PID enumeration exceeds its bound")
    pids = (ctypes.c_int * capacity)()
    ctypes.set_errno(0)
    count = proc.proc_listpids(_PROC_UID_ONLY, os.geteuid(), pids, ctypes.sizeof(pids))
    if count <= 0 or count >= ctypes.sizeof(pids) or count % ctypes.sizeof(ctypes.c_int):
        raise StateError("Darwin PID enumeration is incomplete")
    values = tuple(pids[: count // ctypes.sizeof(ctypes.c_int)])
    if any(pid <= 0 for pid in values) or len(set(values)) != len(values) or os.getpid() not in values:
        raise StateError("Darwin PID enumeration is malformed")
    result = []
    for pid in sorted(values):
        value = _read_identity(pid)
        if value is not None:
            result.append(value)
    return tuple(result)
