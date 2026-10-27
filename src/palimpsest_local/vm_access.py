"""Detail-only VM access. Observation never grants lifecycle ownership.

Public capabilities are eligibility, not an SSH health check. Every transport
open rechecks the selected identity; only fixed, bounded probes serve GETs.
"""

from __future__ import annotations

import datetime
import math
import os
import re
import stat
import threading
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import lima, platforms, runtime_dispatch, state
from .errors import PalimpsestError
from .process_session import bind_process_session_context, spawn_process_session
from .runtime_types import ExpectedRunIdentity, ProcessOutputEvent, ProcessStatusEvent, ProcessStream

if TYPE_CHECKING:
    from .vm_exec_session import ExecOwners

MAX_OUTPUT_BYTES = 512 * 1024
PROBE_TIMEOUT_SECONDS = 10.0


class VMAccessError(PalimpsestError):
    code = "vm-access-error"

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


def capabilities(row: dict, *, live: bool = True, collision: bool = False) -> dict:
    reason = message = None
    transport = None
    if not live:
        reason, message = "unobserved", "Live VM identity has not been observed"
    elif row.get("stale") or collision:
        reason, message = "identity", "VM identity is stale or collides with a selected run"
    elif row.get("backend") in platforms.LIMA_BACKENDS:
        if not row.get("managed") and row.get("run_id") is None:
            reason, message = "identity", "Unmarked external Lima identity cannot be authenticated"
        else:
            transport = "lima"
    elif row.get("managed"):
        transport = "oci-monitor" if row.get("runtime_kind") == "oci-root" else "owned-ssh"
    else:
        reason, message = "credentials", "No authenticated transport is available for this external VM"
    running = row.get("status") == "running"
    available = transport is not None
    return {
        "shell": available and running and row.get("runtime_kind") != "oci-root",
        "exec": available and running,
        "metrics": available and running,
        "logs": available,
        "transport": transport,
        "reason": reason or (None if running else "stopped"),
        "message": message or (None if running else "VM is not running; guest access is unavailable"),
    }


def _collect(session, *, limit=MAX_OUTPUT_BYTES, timeout=PROBE_TIMEOUT_SECONDS) -> bytes:
    expired = threading.Event()

    def cancel():
        expired.set()
        session.close()

    timer = threading.Timer(timeout, cancel)
    timer.daemon = True
    output = bytearray()
    size = 0
    result = None
    timer.start()
    try:
        for event in session.events():
            if isinstance(event, ProcessOutputEvent):
                size += len(event.data)
                if size > limit:
                    raise VMAccessError("output-limit", "VM probe exceeded its output limit")
                if event.stream is ProcessStream.STDOUT:
                    output.extend(event.data)
            elif isinstance(event, ProcessStatusEvent):
                result = event.result
        if expired.is_set():
            raise VMAccessError("timeout", "VM probe timed out")
        if result is None or result.exit_code != 0:
            raise VMAccessError("probe", "VM probe failed; guest command or credentials are unavailable")
        return bytes(output)
    finally:
        timer.cancel()
        session.close()


def _instance(name: str) -> dict:
    output = _collect(
        spawn_process_session(("limactl", "list", "--format", "json", name), stdin=False, tty=False), timeout=5
    )
    try:
        instances = lima._decode_json_objects(output.decode("utf-8"), "instance")
        matches = [value for value in instances if value.get("name") == name]
        if len(matches) != 1:
            raise ValueError
        instance = matches[0]
        if lima.instance_backend(instance) is None:
            raise ValueError
        # Strict parser rejects malformed markers, including ones hidden by observation.
        lima._instance_run_id(instance)
        return instance
    except (ValueError, UnicodeError, PalimpsestError):
        raise VMAccessError("identity", "Current Lima identity is unavailable") from None


def _directory(instance: dict) -> str:
    path = instance.get("dir")
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.realpath(path) != path:
        raise VMAccessError("identity", "Physical Lima instance identity is unavailable")
    return path


def _safe_stat(value):
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o022:
        raise VMAccessError("identity", "Physical Lima instance identity is unsafe")
    return value.st_dev, value.st_ino


class _AuthenticatedLimaSession:
    """Fence guest argv behind guest identity and a host binding check."""

    def __init__(self, session, nonce, check):
        self._session = session
        self._events = iter(session.events())
        self._check = check
        self._pending = b""
        self._pending_stream = ProcessStream.STDOUT
        self._consumed = False
        timer = threading.Timer(PROBE_TIMEOUT_SECONDS, session.close)
        timer.daemon = True
        timer.start()
        try:
            self._through(b"\x1ePALIMPSEST_READY:" + nonce.encode() + b"\x1f")
            check()
            session.write_stdin((nonce + "\n").encode())
            self._through(b"\x1ePALIMPSEST_GO:" + nonce.encode() + b"\x1f")
        finally:
            timer.cancel()

    def _through(self, marker):
        pending = self._pending
        size = len(pending)
        for event in self._events:
            if isinstance(event, ProcessStatusEvent):
                raise VMAccessError("identity", "Guest identity authentication failed")
            if not isinstance(event, ProcessOutputEvent):
                continue
            size += len(event.data)
            if size > MAX_OUTPUT_BYTES:
                raise VMAccessError("output-limit", "VM transport startup exceeded its output limit")
            if event.stream not in {ProcessStream.STDOUT, ProcessStream.PTY}:
                continue
            pending += event.data
            start = pending.find(marker)
            if start >= 0:
                self._pending = pending[start + len(marker) :]
                self._pending_stream = event.stream
                return
            pending = pending[-len(marker) :]
        self._check()
        raise VMAccessError("transport", "Cannot authenticate VM transport")

    @property
    def capabilities(self):
        return self._session.capabilities

    def events(self):
        if self._consumed:
            raise VMAccessError("transport", "VM transport output is already consumed")
        self._consumed = True
        if self._pending:
            yield ProcessOutputEvent(self._pending_stream, self._pending)
            self._pending = b""
        for event in self._events:
            if isinstance(event, ProcessStatusEvent):
                self._check()
            yield event

    def write_stdin(self, data):
        return self._session.write_stdin(data)

    def close_stdin(self):
        return self._session.close_stdin()

    def resize(self, rows, columns):
        return self._session.resize(rows, columns)

    def signal(self, requested):
        return self._session.signal(requested)

    def wait(self):
        return self._session.wait()

    def close(self):
        return self._session.close()


def _lima_gate(run_id, nonce):
    # Read the identity persisted by Lima's seed, not host-injected shell env.
    return (
        f"grep -Fqx 'PALIMPSEST_RUN_ID={run_id}' /etc/environment || exit 125\n"
        "if test -t 0; then stty -echo; fi\n"
        f"printf '\\036PALIMPSEST_READY:{nonce}\\037'\n"
        "IFS= read -r release || exit 125\n"
        f"test \"$release\" = '{nonce}' || exit 125\n"
        "if test -t 0; then stty echo; fi\n"
        f"printf '\\036PALIMPSEST_GO:{nonce}\\037'\n"
        'if test "$#" -eq 0; then exec "${SHELL:-/bin/sh}" -l; fi\n'
        'exec "$@"\n'
    )


@dataclass
class Access:
    roots: state.StatePaths
    row: dict
    record: object | None = None
    exec_owners: ExecOwners | None = None

    @classmethod
    def resolve(cls, roots, identifier, *, expected_run_id=None):
        from .inventory import get_vm

        row = get_vm(roots, identifier)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", row["name"]):
            raise VMAccessError("identity", "Backend VM name is not safe for transport selection")
        access = row["access"]
        if access["transport"] is None:
            raise VMAccessError(access["reason"], access["message"])
        record = None
        if expected_run_id is not None and expected_run_id != row.get("run_id"):
            raise VMAccessError("identity", "Selected VM identity changed")
        if not row["managed"] and expected_run_id is None:
            raise VMAccessError("identity", "External VM access requires the observed run identity")
        if row["managed"]:
            record = runtime_dispatch.resolve_existing_run(row["name"], roots=roots)
            if record.run_id != row["run_id"] or record.dispatch_key.backend.value != row["backend"]:
                raise VMAccessError("identity", "Selected VM identity changed")
            if (
                row["status"] == "running"
                and row["backend"] not in platforms.LIMA_BACKENDS
                and record.dispatch_key.runtime_kind.value == "cloud-image"
            ):
                paths = state.run_paths(roots, record.name)
                try:
                    for artifact in (paths.identity, paths.known_hosts):
                        info = os.stat(artifact, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                            raise OSError
                except OSError:
                    raise VMAccessError("credentials", "Selected VM SSH credentials or trust are unavailable") from None
        return cls(roots, row, record)

    def _require_running(self):
        if self.row["status"] != "running":
            raise VMAccessError("stopped", "VM is not running; guest access is unavailable")

    def _check(self, path, identity, *, running):
        current = _instance(self.row["name"])
        if (
            lima.instance_backend(current) != self.row["backend"]
            or _directory(current) != path
            or lima._instance_run_id(current) != self.row.get("run_id")
            or _safe_stat(os.stat(path, follow_symlinks=False)) != identity
        ):
            raise VMAccessError("identity", "Lima VM identity changed")
        if running and current.get("status") != "Running":
            raise VMAccessError("stopped", "VM is not running; guest access is unavailable")
        if self.record is not None:
            state.require_bound_run_dispatch_record(self.roots, self.record)

    @contextmanager
    def _lima_authority(self, *, running=True):
        with ExitStack() as resources:
            first = _instance(self.row["name"])
            if lima.instance_backend(first) != self.row["backend"] or lima._instance_run_id(first) != self.row.get(
                "run_id"
            ):
                raise VMAccessError("identity", "Lima VM identity changed")
            path = _directory(first)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            resources.callback(os.close, fd)
            identity = _safe_stat(os.fstat(fd))
            self._check(path, identity, running=running)
            yield fd, lambda: self._check(path, identity, running=running)

    def open(self, mode="shell", argv=(), *, probe=False):
        self._require_running()
        if self.row["backend"] not in platforms.LIMA_BACKENDS:
            identity = ExpectedRunIdentity(self.record.name, self.record.run_id, self.record.dispatch_key)
            if mode == "shell":
                return runtime_dispatch.shell(self.row["name"], roots=self.roots, expected_identity=identity)
            if self.row.get("runtime_kind") == "oci-root":
                from .vm_exec_session import RetainedExecSession

                def submit():
                    return runtime_dispatch.exec(
                        self.row["name"],
                        argv,
                        roots=self.roots,
                        expected_identity=identity,
                        timeout_ms=8000 if probe else None,
                    )

                return self.exec_owners.open(submit) if self.exec_owners is not None else RetainedExecSession(submit())
            return runtime_dispatch.exec(self.row["name"], argv, roots=self.roots, expected_identity=identity)
        authority = ExitStack()
        session = None
        try:
            with ExitStack() as setup:
                if self.record is not None:
                    setup.enter_context(
                        state.locked_existing_run(self.roots, self.record.name, expected=self.record, lock_timeout=5)
                    )
                _, check = authority.enter_context(self._lima_authority())
                nonce = uuid.uuid4().hex
                guest_argv = () if mode == "shell" else tuple(argv)
                command = [
                    "limactl",
                    "shell",
                    "--tty=" + ("true" if mode == "shell" else "false"),
                    self.row["name"],
                    "sh",
                    "-c",
                    _lima_gate(self.row["run_id"], nonce),
                    "sh",
                    *guest_argv,
                ]
                session = spawn_process_session(command, tty=mode == "shell", stdin=True)
                check()
                authenticated = _AuthenticatedLimaSession(session, nonce, check)
            return bind_process_session_context(authenticated, authority)
        except BaseException:
            if session is not None:
                session.close()
            authority.close()
            raise

    def probe(self, argv):
        return _collect(self.open("exec", argv, probe=True))

    def logs(self, tail):
        if self.row["status"] == "running":
            data = self.probe(("sudo", "-n", "journalctl", "--no-pager", "--quiet", "-n", str(tail), "-o", "short-iso"))
            return {"log": data.decode("utf-8", "replace"), "source": "guest-journal", "truncated": False}
        if self.record is not None and self.row["backend"] not in platforms.LIMA_BACKENDS:
            console = state.open_pinned_run_console(self.roots, self.record)
            try:
                console.current_status()
                console.position = max(0, console.initial_size - MAX_OUTPUT_BYTES)
                clipped = console.position > 0
                data = bytearray()
                while chunk := console.read(64 * 1024, snapshot=True):
                    data.extend(chunk)
                console.current_status()
                lines = data.decode("utf-8", "replace").splitlines()
                if clipped:
                    lines = lines[1:]
                return {
                    "log": "\n".join(lines[-tail:]),
                    "source": "retained-console",
                    "truncated": clipped or len(lines) > tail,
                }
            finally:
                console.close()
        if self.row["backend"] not in platforms.LIMA_BACKENDS:
            raise VMAccessError("stopped", "Guest journal is unavailable while the VM is stopped")
        # Native Lima's fixed serial console, never a guessed home/instance path.
        with self._lima_authority(running=False) as (directory, check):
            fd = os.open("serial.log", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
                    raise VMAccessError("logs", "Lima serial console is unavailable")
                os.lseek(fd, max(0, before.st_size - MAX_OUTPUT_BYTES), os.SEEK_SET)
                data = os.read(fd, MAX_OUTPUT_BYTES)
                check()
                linked = os.stat("serial.log", dir_fd=directory, follow_symlinks=False)
                if (linked.st_dev, linked.st_ino) != (before.st_dev, before.st_ino):
                    raise VMAccessError("identity", "Lima console identity changed")
                lines = data.decode("utf-8", "replace").splitlines()
                if before.st_size > MAX_OUTPUT_BYTES:
                    lines = lines[1:]
                return {
                    "log": "\n".join(lines[-tail:]),
                    "source": "lima-serial-console",
                    "truncated": before.st_size > MAX_OUTPUT_BYTES or len(lines) > tail,
                }
            finally:
                os.close(fd)


# Fixed Linux inputs only. Section boundaries keep unavailable sources distinct
# from successful empty counters; a guest cannot cause unbounded host retention.
METRICS_PROBE = """export LC_ALL=C
for p in stat loadavg meminfo uptime net/dev diskstats sys/kernel/random/boot_id; do
 printf '\\n@@%s\\n' "$p"
 if raw=$(head -c 65537 "/proc/$p" 2>/dev/null && printf '.'); then
  if test "${#raw}" -le 65537; then printf '%s' "${raw%.}"; else printf 'UNAVAILABLE\\n'; fi
 else
  printf 'UNAVAILABLE\\n'
 fi
done
printf '\\n@@df\\n'
df -Pk 2>/dev/null || printf 'UNAVAILABLE\\n'
printf '\\n@@end\\n'
"""


def parse_metrics(data: bytes) -> dict:
    text = data.decode("utf-8", "strict")
    chunks = re.split(r"(?m)^@@([^\n]+)\n", text)
    sections = dict(zip(chunks[1::2], chunks[2::2], strict=True))
    unavailable = []

    def parse(name, fn):
        try:
            raw = sections[name].strip("\n")
            if "UNAVAILABLE" in raw:
                raise ValueError
            return fn(raw)
        except (KeyError, ValueError, IndexError, OverflowError):
            unavailable.append(name)
            return None

    def number(value):
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError
        return result

    def integer(value):
        if not value.isdecimal():
            raise ValueError
        return int(value)

    def cpu(raw):
        values = raw.splitlines()[0].split()
        if values[0] != "cpu" or len(values) < 9:
            raise ValueError
        ticks = [integer(value) for value in values[1:9]]
        return {
            "total_ticks": sum(ticks),
            "idle_ticks": ticks[3] + ticks[4],
            "user_ticks": ticks[0],
            "system_ticks": ticks[2],
            "iowait_ticks": ticks[4],
        }

    def load(raw):
        values = raw.split()
        if len(values) < 3:
            raise ValueError
        return dict(zip(("one", "five", "fifteen"), [number(value) for value in values[:3]], strict=True))

    def boot(raw):
        import uuid

        if str(uuid.UUID(raw)) != raw:
            raise ValueError
        return raw

    def memory(raw):
        fields = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2] == "kB":
                fields[parts[0].rstrip(":")] = integer(parts[1]) * 1024
        total, available = fields["MemTotal"], fields["MemAvailable"]
        if available > total:
            raise ValueError
        return {"total_bytes": total, "available_bytes": available, "used_bytes": total - available}

    def network(raw):
        rows = []
        lines = raw.splitlines()
        if len(lines) < 3 or "Receive" not in lines[0] or "Transmit" not in lines[0]:
            raise ValueError
        for line in lines[2:]:
            name, counters = line.rsplit(":", 1)
            values = counters.split()
            if len(values) != 16:
                raise ValueError
            rows.append({"name": name.strip(), "rx_bytes": integer(values[0]), "tx_bytes": integer(values[8])})
        return rows

    def blocks(raw):
        rows = []
        for line in raw.splitlines():
            values = line.split()
            if len(values) < 14:
                raise ValueError
            rows.append(
                {
                    "name": values[2],
                    "read_ios": integer(values[3]),
                    "write_ios": integer(values[7]),
                    "read_bytes": integer(values[5]) * 512,
                    "write_bytes": integer(values[9]) * 512,
                    "io_time_ms": integer(values[12]),
                }
            )
        return rows

    def filesystem(raw):
        rows = []
        for line in raw.splitlines()[1:]:
            parts = line.split(None, 5)
            if len(parts) != 6:
                raise ValueError
            rows.append(
                {
                    "device": parts[0],
                    "mountpoint": parts[5],
                    "total_bytes": integer(parts[1]) * 1024,
                    "used_bytes": integer(parts[2]) * 1024,
                    "free_bytes": integer(parts[3]) * 1024,
                }
            )
        if not rows:
            raise ValueError
        return rows

    return {
        "cpu": parse("stat", cpu),
        "load": parse("loadavg", load),
        "memory": parse("meminfo", memory),
        "uptime_seconds": parse("uptime", lambda raw: number(raw.split()[0])),
        "boot_id": parse("sys/kernel/random/boot_id", boot),
        "filesystems": parse("df", filesystem),
        "interfaces": parse("net/dev", network),
        "block_devices": parse("diskstats", blocks),
        "unavailable": unavailable,
    }


def metrics(access: Access) -> dict:
    started = datetime.datetime.now(datetime.UTC)
    data = parse_metrics(access.probe(("sh", "-c", METRICS_PROBE)))
    return {
        "id": access.row["id"],
        "run_id": access.row.get("run_id"),
        "source": "linux-guest",
        "measured_at": started.isoformat(),
        "status": "partial" if data["unavailable"] else "available",
        **data,
    }
