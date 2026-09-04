"""Private libvirt transport for the production-inert OCI-root handoff.

The public runtime dispatcher deliberately does not import this module.  It is
the narrow synchronous bridge between a previously qualified v2 lifecycle
state machine and one exact libvirt virtio-serial channel.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import StateError
from .oci_control_protocol_v2 import (
    HostOCIControlV2Session,
    OCIControlProtocolV2Error,
    OCIControlV2Binding,
    OCIControlV2Envelope,
    OCIControlV2FrameDecoder,
    encode_frame,
)
from .runtime_types import ProcessExit, ProcessExitCategory

OCI_ROOT_HANDOFF_SCHEMA = "palimpsest.oci-root-handoff.v1"
DEFAULT_HANDOFF_TIMEOUT_SECONDS = 30.0
_READ_SIZE = 64 * 1024


class OCILifecycleTransportError(StateError):
    """Stable failure at the libvirt stream or lifecycle handoff boundary."""


@dataclass(frozen=True, slots=True)
class OCILifecycleHandoffReceipt:
    """Secret-free projection of one authenticated boot lifecycle."""

    boot_attempt_id: str
    boot_generation: str
    key_id: str
    phase: str
    transcript: tuple[Mapping[str, Any], ...]
    terminal: ProcessExit | None = None

    def __post_init__(self) -> None:
        try:
            if str(uuid.UUID(self.boot_attempt_id)) != self.boot_attempt_id:
                raise ValueError
            if str(uuid.UUID(self.boot_generation)) != self.boot_generation:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ValueError("OCI-root lifecycle receipt boot identity is invalid") from None
        if not isinstance(self.key_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", self.key_id) is None:
            raise ValueError("OCI-root lifecycle receipt key ID is invalid")
        if self.phase == "ready" and self.terminal is None:
            pass
        elif self.phase == "terminal" and isinstance(self.terminal, ProcessExit):
            pass
        else:
            raise ValueError("OCI-root lifecycle receipt phase is invalid")
        if not isinstance(self.transcript, tuple) or any(not isinstance(item, Mapping) for item in self.transcript):
            raise ValueError("OCI-root lifecycle receipt transcript is invalid")

    def to_dict(self) -> dict[str, Any]:
        terminal = None
        if self.terminal is not None:
            terminal = {
                "category": self.terminal.category.value,
                "exit_code": self.terminal.exit_code,
                "returncode": self.terminal.returncode,
                "signal_number": self.terminal.signal_number,
            }
        return {
            "boot_attempt_id": self.boot_attempt_id,
            "boot_generation": self.boot_generation,
            "key_id": self.key_id,
            "phase": self.phase,
            "schema": OCI_ROOT_HANDOFF_SCHEMA,
            "terminal": terminal,
            "transcript": [dict(item) for item in self.transcript],
        }


def _terminal_result(envelope: OCIControlV2Envelope) -> ProcessExit:
    terminal = envelope.body.payload["terminal"]
    exit_code = terminal["exit_code"]
    signal_number = terminal["signal"]
    if exit_code is not None:
        return ProcessExit(exit_code, exit_code, None, ProcessExitCategory.EXITED)
    return ProcessExit(-signal_number, None, signal_number, ProcessExitCategory.SIGNALED)


def _remaining(deadline: float | None, monotonic: Callable[[], float]) -> float:
    if deadline is None:
        return 0.01
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise OCILifecycleTransportError("OCI-root lifecycle handoff timed out")
    return remaining


def _pause(deadline: float | None, monotonic: Callable[[], float], wait: Callable[[float], None]) -> None:
    wait(min(0.01, _remaining(deadline, monotonic)))


def _send_all(
    stream: Any,
    payload: bytes,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    wait: Callable[[float], None],
) -> None:
    offset = 0
    while offset < len(payload):
        _remaining(deadline, monotonic)
        try:
            sent = stream.send(payload[offset:])
        except Exception:
            raise OCILifecycleTransportError("OCI-root lifecycle stream send failed") from None
        if sent == -2:
            _pause(deadline, monotonic, wait)
            continue
        if type(sent) is not int or sent <= 0 or sent > len(payload) - offset:
            raise OCILifecycleTransportError("OCI-root lifecycle stream send result is invalid")
        offset += sent


def _receive_one(
    stream: Any,
    decoder: OCIControlV2FrameDecoder,
    pending: list[OCIControlV2Envelope],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    wait: Callable[[float], None],
) -> OCIControlV2Envelope:
    while not pending:
        _remaining(deadline, monotonic)
        try:
            chunk = stream.recv(_READ_SIZE)
        except Exception:
            raise OCILifecycleTransportError("OCI-root lifecycle stream receive failed") from None
        if chunk == -2:
            _pause(deadline, monotonic, wait)
            continue
        if chunk in {b"", 0}:
            try:
                decoder.finish()
            except OCIControlProtocolV2Error:
                raise OCILifecycleTransportError("OCI-root lifecycle stream ended with a truncated frame") from None
            raise OCILifecycleTransportError("OCI-root lifecycle stream ended before terminal status")
        if not isinstance(chunk, bytes):
            raise OCILifecycleTransportError("OCI-root lifecycle stream receive result is invalid")
        try:
            pending.extend(decoder.feed(chunk))
        except OCIControlProtocolV2Error:
            raise OCILifecycleTransportError("OCI-root lifecycle frame is invalid") from None
    return pending.pop(0)


def _receipt(
    session: HostOCIControlV2Session,
    phase: str,
    transcript: list[Mapping[str, Any]],
    terminal: ProcessExit | None,
    observed_tags: set[str],
) -> OCILifecycleHandoffReceipt:
    if session.boot_generation is None or session.key_id is None:
        raise OCILifecycleTransportError("OCI-root lifecycle session identity is incomplete")
    receipt = OCILifecycleHandoffReceipt(
        session.boot_attempt_id,
        session.boot_generation,
        session.key_id,
        phase,
        tuple(transcript),
        terminal,
    )
    serialized = json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    try:
        session.assert_receipt_safe(serialized, observed_tags)
    except OCIControlProtocolV2Error:
        raise OCILifecycleTransportError("OCI-root lifecycle receipt is not safe to persist") from None
    return receipt


def complete_initial_lifecycle_handoff(
    stream: Any,
    binding: OCIControlV2Binding,
    *,
    on_ready: Callable[[OCILifecycleHandoffReceipt], None],
    timeout_seconds: float = DEFAULT_HANDOFF_TIMEOUT_SECONDS,
    terminal_timeout_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
    session: HostOCIControlV2Session | None = None,
) -> OCILifecycleHandoffReceipt:
    """Drive HELLO/BOOTSTRAP/KEY_ACK/READY through authenticated TERMINAL.

    ``stream`` must already be the exact domain channel opened with libvirt's
    nonblocking flag.  It is always aborted on return.  Some Python libvirt
    bindings do not expose ``virStreamFree`` as a public ``free`` method; when
    absent, the binding owns final release when the wrapper is collected.  A
    callable ``free`` extension is invoked after ``abort`` exactly once.
    """

    result: OCILifecycleHandoffReceipt | None = None
    failure: BaseException | None = None
    abort_operation: Callable[[], Any] | None = None
    free_operation: Callable[[], Any] | None = None
    try:
        try:
            abort_candidate = getattr(stream, "abort", None)
        except Exception:
            raise OCILifecycleTransportError("OCI-root lifecycle stream surface is invalid") from None
        abort_operation = abort_candidate if callable(abort_candidate) else None
        try:
            free_candidate = getattr(stream, "free", None)
        except Exception:
            raise OCILifecycleTransportError("OCI-root lifecycle stream surface is invalid") from None
        free_operation = free_candidate if callable(free_candidate) else None
        if (
            any(not callable(getattr(stream, operation, None)) for operation in ("send", "recv"))
            or not callable(abort_candidate)
            or (free_candidate is not None and not callable(free_candidate))
        ):
            raise OCILifecycleTransportError("OCI-root lifecycle stream surface is invalid")
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise OCILifecycleTransportError("OCI-root lifecycle timeout is invalid")
        if terminal_timeout_seconds is not None and (
            type(terminal_timeout_seconds) not in {int, float}
            or not math.isfinite(terminal_timeout_seconds)
            or terminal_timeout_seconds <= 0
        ):
            raise OCILifecycleTransportError("OCI-root lifecycle terminal timeout is invalid")
        if not callable(on_ready):
            raise OCILifecycleTransportError("OCI-root lifecycle ready callback is invalid")
        if not callable(monotonic) or not callable(wait):
            raise OCILifecycleTransportError("OCI-root lifecycle timing callback is invalid")
        ready_deadline = monotonic() + float(timeout_seconds)
        if not math.isfinite(ready_deadline):
            raise OCILifecycleTransportError("OCI-root lifecycle clock result is invalid")
        decoder = OCIControlV2FrameDecoder()
        pending: list[OCIControlV2Envelope] = []
        transcript: list[Mapping[str, Any]] = []
        observed_tags: set[str] = set()
        session = HostOCIControlV2Session(binding) if session is None else session
        if session.binding != binding or session.state != "new":
            raise OCILifecycleTransportError("OCI-root lifecycle session is invalid")
        hello = session.hello()
        encoded = encode_frame(hello)
        _send_all(stream, encoded, deadline=ready_deadline, monotonic=monotonic, wait=wait)
        transcript.append(session.transcript_projection(hello, encoded))

        bootstrap = _receive_one(stream, decoder, pending, deadline=ready_deadline, monotonic=monotonic, wait=wait)
        try:
            session.accept(bootstrap)
        except OCIControlProtocolV2Error:
            raise OCILifecycleTransportError("OCI-root lifecycle BOOTSTRAP was rejected") from None
        if bootstrap.tag is not None:
            observed_tags.add(bootstrap.tag)
        bootstrap_encoded = encode_frame(bootstrap)
        transcript.append(session.transcript_projection(bootstrap, bootstrap_encoded))

        key_ack = session.key_ack()
        encoded = encode_frame(key_ack)
        _send_all(stream, encoded, deadline=ready_deadline, monotonic=monotonic, wait=wait)
        observed_tags.add(key_ack.tag or "")
        transcript.append(session.transcript_projection(key_ack, encoded))

        ready = _receive_one(stream, decoder, pending, deadline=ready_deadline, monotonic=monotonic, wait=wait)
        try:
            session.accept(ready)
        except OCIControlProtocolV2Error:
            raise OCILifecycleTransportError("OCI-root lifecycle READY was rejected") from None
        if session.state != "ready":
            raise OCILifecycleTransportError("OCI-root lifecycle did not become ready")
        if ready.tag is not None:
            observed_tags.add(ready.tag)
        ready_encoded = encode_frame(ready)
        transcript.append(session.transcript_projection(ready, ready_encoded))
        on_ready(_receipt(session, "ready", transcript, None, observed_tags))

        terminal_deadline = None
        if terminal_timeout_seconds is not None:
            terminal_deadline = monotonic() + float(terminal_timeout_seconds)
            if not math.isfinite(terminal_deadline):
                raise OCILifecycleTransportError("OCI-root lifecycle clock result is invalid")
        terminal_envelope = _receive_one(
            stream, decoder, pending, deadline=terminal_deadline, monotonic=monotonic, wait=wait
        )
        try:
            session.accept(terminal_envelope)
        except OCIControlProtocolV2Error:
            raise OCILifecycleTransportError("OCI-root lifecycle TERMINAL was rejected") from None
        if session.state != "terminal" or terminal_envelope.body.kind != "TERMINAL":
            raise OCILifecycleTransportError("OCI-root lifecycle terminal status is invalid")
        if pending:
            raise OCILifecycleTransportError("OCI-root lifecycle TERMINAL had trailing frame data")
        try:
            decoder.finish()
        except OCIControlProtocolV2Error:
            raise OCILifecycleTransportError("OCI-root lifecycle TERMINAL had trailing frame data") from None
        if terminal_envelope.tag is not None:
            observed_tags.add(terminal_envelope.tag)
        terminal_encoded = encode_frame(terminal_envelope)
        transcript.append(session.transcript_projection(terminal_envelope, terminal_encoded))
        result = _receipt(
            session,
            "terminal",
            transcript,
            _terminal_result(terminal_envelope),
            observed_tags,
        )
    except BaseException as exc:
        failure = exc
    close_failed = False
    for operation in (abort_operation, free_operation):
        if operation is None:
            continue
        try:
            operation()
        except Exception:
            close_failed = True
    if failure is not None:
        raise failure
    if close_failed:
        raise OCILifecycleTransportError("OCI-root lifecycle stream cleanup failed")
    if result is None:
        raise OCILifecycleTransportError("OCI-root lifecycle handoff did not complete")
    return result


__all__ = [
    "DEFAULT_HANDOFF_TIMEOUT_SECONDS",
    "OCI_ROOT_HANDOFF_SCHEMA",
    "OCILifecycleHandoffReceipt",
    "OCILifecycleTransportError",
    "complete_initial_lifecycle_handoff",
]
