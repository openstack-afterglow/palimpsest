"""Current operator authority over an existing worker process's private pipes.

The initial policy is provenance. Each publication needs a fresh, single-use
challenge; copying that policy to the worker is not current authorization.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager

from .errors import PalimpsestError
from .image_source_policy import ImageSourcePolicy
from .oci_image import strict_json_object

PREFIX = b"PALIMPSEST-POLICY "
MAX_FRAME = 2048
MAX_STDOUT = 16 * 1024 * 1024
MAX_STDERR = 1024 * 1024
CHECK_TIMEOUT = 30.0
# A TERM-resistant process stays owned, including its open pipes, for reconciliation.
_HELD_PROCESSES: list[subprocess.Popen] = []


class PolicyPublicationHold(PalimpsestError):
    """Current publication authority is denied or unknown; never replay the job."""


def require_current_policy(
    initial: ImageSourcePolicy, provider: Callable[[], ImageSourcePolicy], deadline: float
) -> None:
    try:
        if time.monotonic() >= deadline:
            raise PolicyPublicationHold("HOLD: publication deadline expired")
        current = provider()
        if not isinstance(current, ImageSourcePolicy) or current.to_dict() != initial.to_dict():
            raise PolicyPublicationHold("HOLD: current operator source policy changed")
        if time.monotonic() >= deadline:
            raise PolicyPublicationHold("HOLD: publication deadline expired during policy reload")
    except PolicyPublicationHold:
        raise
    except Exception as error:
        raise PolicyPublicationHold("HOLD: current operator source policy unavailable") from error


class CurrentPolicyAuthority:
    def __init__(self, initial: ImageSourcePolicy, provider: Callable[[], ImageSourcePolicy]):
        self.initial = initial
        self.provider = provider

    def __call__(self, stage: str, deadline: float) -> None:
        require_current_policy(self.initial, self.provider, deadline)


def _write(fd: int, payload: bytes, deadline: float) -> None:
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while payload:
            if not selector.select(max(0.0, deadline - time.monotonic())):
                raise PolicyPublicationHold("HOLD: policy channel write deadline expired")
            count = os.write(fd, payload)
            payload = payload[count:]


def _frame(value: dict) -> bytes:
    payload = PREFIX + json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(payload) > MAX_FRAME:
        raise PolicyPublicationHold("HOLD: policy channel frame exceeds bound")
    return payload


class PipePolicyAuthority:
    def __init__(self, initial: ImageSourcePolicy, *, read_fd: int = 0, write_fd: int = 1):
        self.initial = initial
        self.read_fd = read_fd
        self.write_fd = write_fd

    def __call__(self, stage: str, deadline: float) -> None:
        deadline = min(deadline, time.monotonic() + CHECK_TIMEOUT)
        request = {"nonce": uuid.uuid4().hex, "initial_policy_digest": self.initial.digest, "stage": stage}
        try:
            if time.monotonic() >= deadline:
                raise PolicyPublicationHold("HOLD: publication deadline expired")
            _write(self.write_fd, _frame(request), deadline)
            payload = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(self.read_fd, selectors.EVENT_READ)
                while not payload.endswith(b"\n"):
                    if not selector.select(max(0.0, deadline - time.monotonic())):
                        raise PolicyPublicationHold("HOLD: current policy reply deadline expired")
                    # Do not read ahead into a subsequent reply or another protocol.
                    part = os.read(self.read_fd, 1)
                    if not part or len(payload) >= MAX_FRAME:
                        raise PolicyPublicationHold("HOLD: current policy reply missing or oversized")
                    payload.extend(part)
            if not payload.startswith(PREFIX):
                raise PolicyPublicationHold("HOLD: unknown current policy reply")
            reply = strict_json_object(bytes(payload[len(PREFIX) :]), "policy reply")
            if reply != {**request, "allow": True} or time.monotonic() >= deadline:
                raise PolicyPublicationHold("HOLD: publication denied by current operator policy")
        except PolicyPublicationHold:
            raise
        except Exception as error:
            raise PolicyPublicationHold("HOLD: current policy channel unavailable") from error


@contextmanager
def isolated_policy_channel(initial: ImageSourcePolicy):
    """Reserve original SSH pipes exclusively for protocol and the final receipt.

    Duplicated descriptors are non-inheritable. Children see EOF on stdin and
    their stdout is bounded worker stderr, not policy protocol or receipt bytes.
    Restore standard descriptors before the caller emits its final JSON result.
    """
    sys.stdout.flush()
    with ExitStack() as stack:
        read_fd = os.dup(0)
        stack.callback(os.close, read_fd)
        write_fd = os.dup(1)
        stack.callback(os.close, write_fd)
        null_fd = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
        stack.callback(os.close, null_fd)
        try:
            os.dup2(null_fd, 0)
            os.dup2(2, 1)
            yield PipePolicyAuthority(initial, read_fd=read_fd, write_fd=write_fd)
        finally:
            try:
                sys.stdout.flush()
            finally:
                try:
                    os.dup2(read_fd, 0)
                finally:
                    os.dup2(write_fd, 1)


def run_policy_process(
    argv: list[str],
    *,
    initial: ImageSourcePolicy,
    current_policy: Callable[[], ImageSourcePolicy],
    policy_timeout: float,
    process_timeout: float,
) -> subprocess.CompletedProcess:
    """Supervise the existing SSH command; retain only bounded final stdout/stderr.

    A denial is answered, not raised, so the worker can durably journal HOLD and
    settle. Transport failure/overflow/expiry closes authorization and uses TERM
    plus bounded reap only; a resistant process remains in explicit custody.
    """
    started = time.monotonic()
    deadline = started + policy_timeout
    process_deadline = started + process_timeout
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    output, errors, pending = bytearray(), bytearray(), bytearray()
    seen: set[str] = set()
    consumed = 0
    denied = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = process_deadline - time.monotonic()
                if remaining <= 0:
                    raise PolicyPublicationHold("HOLD: worker process deadline expired")
                for key, _ in selector.select(min(remaining, 1.0)):
                    part = os.read(key.fd, 65536)
                    if not part:
                        selector.unregister(key.fileobj)
                        if key.data == "stdout" and pending:
                            output.extend(pending)
                            pending.clear()
                        continue
                    if key.data == "stderr":
                        errors.extend(part)
                        if len(errors) > MAX_STDERR:
                            raise PolicyPublicationHold("HOLD: worker stderr exceeds bound")
                        continue
                    consumed += len(part)
                    if consumed > MAX_STDOUT:
                        raise PolicyPublicationHold("HOLD: worker stdout exceeds bound")
                    pending.extend(part)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        if not line.startswith(PREFIX):
                            output.extend(line + b"\n")
                            continue
                        if len(line) + 1 > MAX_FRAME:
                            raise PolicyPublicationHold("HOLD: policy challenge exceeds bound")
                        request = strict_json_object(bytes(line[len(PREFIX) :]), "policy challenge")
                        if (
                            set(request) != {"nonce", "initial_policy_digest", "stage"}
                            or not isinstance(request["nonce"], str)
                            or re.fullmatch(r"[0-9a-f]{32}", request["nonce"]) is None
                            or request["nonce"] in seen
                            or request["initial_policy_digest"] != initial.digest
                            or not isinstance(request["stage"], str)
                            or re.fullmatch(r"[a-z][a-z0-9:._-]{0,127}", request["stage"]) is None
                        ):
                            raise PolicyPublicationHold("HOLD: invalid or replayed policy challenge")
                        seen.add(request["nonce"])
                        allowed = True
                        try:
                            require_current_policy(initial, current_policy, deadline)
                        except PolicyPublicationHold:
                            allowed = False
                            denied = True
                        _write(
                            process.stdin.fileno(),
                            _frame({**request, "allow": allowed}),
                            min(process_deadline, time.monotonic() + CHECK_TIMEOUT),
                        )
                    if pending.startswith(PREFIX) and len(pending) >= MAX_FRAME:
                        raise PolicyPublicationHold("HOLD: incomplete policy challenge exceeds bound")
            returncode = process.wait(timeout=max(0.0, process_deadline - time.monotonic()))
            if returncode == 0 and (not seen or denied):
                raise PolicyPublicationHold("HOLD: worker reported success without current publication authorization")
            return subprocess.CompletedProcess(argv, returncode, bytes(output), bytes(errors))
    except BaseException:
        # EOF prevents any subsequent publication check from succeeding remotely.
        process.stdin.close()
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            _HELD_PROCESSES.append(process)
            raise PolicyPublicationHold("HOLD: TERM did not reap worker SSH process; custody retained") from error
        raise
    finally:
        if process.poll() is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
