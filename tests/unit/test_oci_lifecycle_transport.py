from __future__ import annotations

import json
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from palimpsest_local.oci_control_protocol_v2 import (
    HostOCIControlV2Session,
    OCIControlV2Binding,
    OCIControlV2Message,
    decode_frame,
    encode_frame,
    sign_message,
)
from palimpsest_local.oci_lifecycle_transport import (
    OCILifecycleTransportError,
    complete_initial_lifecycle_handoff,
)
from palimpsest_local.oci_monitor_control import MonitorStopControl

RUN_ID = "f6f546e2-e734-4920-9eff-1762b348a249"
ATTEMPT_ID = "aca88126-d991-4de8-b66b-90dc07904dff"
BOOT_GENERATION = "b22b1c81-dfa4-478a-b352-27b5b35fe5b7"
NONCE = "1" * 64
KEY = bytes(range(32))
BINDING = OCIControlV2Binding(RUN_ID, "sha256:" + "a" * 64, "sha256:" + "b" * 64)


def _session(_binding: OCIControlV2Binding) -> HostOCIControlV2Session:
    return HostOCIControlV2Session(
        BINDING,
        nonce_factory=lambda: NONCE,
        boot_attempt_factory=lambda: ATTEMPT_ID,
    )


class _GuestStream:
    def __init__(
        self,
        *,
        signal_number: int | None = None,
        terminal_delay: int = 0,
        trailing_terminal_wire: str | None = None,
        await_stop: bool = False,
    ) -> None:
        self.signal_number = signal_number
        self.terminal_delay = terminal_delay
        self.trailing_terminal_wire = trailing_terminal_wire
        self.await_stop = await_stop
        self.host = bytearray()
        self.guest: list[bytes | int] = [-2]
        self.abort_calls = 0
        self.free_calls = 0
        self.sent_kinds: list[str] = []
        self.send_would_block = True

    def send(self, payload: bytes) -> int:
        if self.send_would_block:
            self.send_would_block = False
            return -2
        if not self.host:
            self.host.extend(payload[: min(7, len(payload))])
            return min(7, len(payload))
        self.host.extend(payload)
        while len(self.host) >= 4:
            length = struct.unpack(">I", self.host[:4])[0] + 4
            if len(self.host) < length:
                break
            envelope = decode_frame(bytes(self.host[:length]))
            del self.host[:length]
            self.sent_kinds.append(envelope.body.kind)
            if envelope.body.kind == "HELLO":
                bootstrap = sign_message(
                    OCIControlV2Message(
                        "BOOTSTRAP",
                        BINDING,
                        ATTEMPT_ID,
                        NONCE,
                        1,
                        1,
                        {"boot_key": KEY.hex()},
                        boot_generation=BOOT_GENERATION,
                        reply_to=envelope.body.request_id,
                    ),
                    KEY,
                )
                encoded = encode_frame(bootstrap)
                self.guest.extend((encoded[:3], encoded[3:19], encoded[19:]))
            elif envelope.body.kind == "KEY_ACK":
                ready = sign_message(
                    OCIControlV2Message(
                        "READY",
                        BINDING,
                        ATTEMPT_ID,
                        NONCE,
                        1,
                        2,
                        {},
                        boot_generation=BOOT_GENERATION,
                        reply_to=envelope.body.wire_sequence,
                    ),
                    KEY,
                )
                terminal_payload = (
                    {"exit_code": 42, "signal": None}
                    if self.signal_number is None
                    else {"exit_code": None, "signal": self.signal_number}
                )
                terminal = sign_message(
                    OCIControlV2Message(
                        "TERMINAL",
                        BINDING,
                        ATTEMPT_ID,
                        NONCE,
                        1,
                        3,
                        {"terminal": terminal_payload},
                        boot_generation=BOOT_GENERATION,
                    ),
                    KEY,
                )
                trailing = b""
                if self.trailing_terminal_wire == "frame":
                    trailing = encode_frame(terminal)
                elif self.trailing_terminal_wire == "truncated":
                    trailing = b"\x00\x00"
                if self.await_stop:
                    self.guest.append(encode_frame(ready))
                elif self.terminal_delay:
                    self.guest.append(encode_frame(ready))
                    self.guest.extend([-2] * self.terminal_delay)
                    self.guest.append(encode_frame(terminal) + trailing)
                else:
                    self.guest.append(encode_frame(ready) + encode_frame(terminal) + trailing)
            elif envelope.body.kind == "STOP":
                terminal = sign_message(
                    OCIControlV2Message(
                        "TERMINAL",
                        BINDING,
                        ATTEMPT_ID,
                        NONCE,
                        1,
                        3,
                        {"terminal": {"exit_code": None, "signal": 15}},
                        boot_generation=BOOT_GENERATION,
                        reply_to=envelope.body.request_id,
                    ),
                    KEY,
                )
                self.guest.append(encode_frame(terminal))
        return len(payload)

    def recv(self, _size: int) -> bytes | int:
        return self.guest.pop(0) if self.guest else -2

    def abort(self) -> None:
        self.abort_calls += 1

    def free(self) -> None:
        self.free_calls += 1


def test_stop_is_worker_signed_once_with_guard_before_every_partial_send():
    stream = _GuestStream(await_stop=True)
    control = MonitorStopControl()
    guards = []
    writes = []
    original_send = stream.send
    admitted = False

    def send(payload):
        if admitted:
            writes.append(len(payload))
            assert len(guards) == len(writes)
        return original_send(payload)

    def ready(_receipt):
        nonlocal admitted
        control.mark_ready()
        assert control.request() == control.request() == "stop-accepted"
        stream.send_would_block = True
        admitted = True

    stream.send = send
    result = complete_initial_lifecycle_handoff(
        stream,
        BINDING,
        on_ready=ready,
        session=_session(BINDING),
        stop_control=control,
        before_stop_send=lambda: guards.append(True),
    )
    assert stream.sent_kinds == ["HELLO", "KEY_ACK", "STOP"]
    assert len(guards) == len(writes) == 3  # EAGAIN, short write, final write.
    assert result.terminal.returncode == -15
    assert control.request() == "stop-accepted"  # Receipt is not durable yet.
    assert not control.take_stop()
    control.mark_terminal()
    assert control.request() == "stop-terminal"
    encoded = json.dumps(result.to_dict())
    assert KEY.hex() not in encoded and '"tag"' not in encoded


def test_buffered_natural_terminal_takes_priority_over_admitted_stop():
    stream = _GuestStream()
    control = MonitorStopControl()

    def ready(_receipt):
        control.mark_ready()
        assert control.request() == "stop-accepted"

    receipt = complete_initial_lifecycle_handoff(
        stream,
        BINDING,
        on_ready=ready,
        session=_session(BINDING),
        stop_control=control,
        before_stop_send=lambda: pytest.fail("unexpected STOP write"),
    )
    assert receipt.terminal.returncode == 42
    assert stream.sent_kinds == ["HELLO", "KEY_ACK"]
    assert not control.take_stop()


@pytest.mark.parametrize("split", [1, 2, 3, 4, 5, 17])
def test_partial_natural_terminal_completes_without_injecting_stop(split):
    stream = _GuestStream(terminal_delay=1)
    control = MonitorStopControl()

    def ready(_receipt):
        control.mark_ready()
        control.request()
        terminal_frame = stream.guest[-1]
        stream.guest[:] = [terminal_frame[:split], -2, terminal_frame[split:]]

    receipt = complete_initial_lifecycle_handoff(
        stream,
        BINDING,
        on_ready=ready,
        session=_session(BINDING),
        stop_control=control,
        before_stop_send=lambda: pytest.fail("unexpected STOP write"),
    )
    assert receipt.terminal.returncode == 42
    assert stream.sent_kinds == ["HELLO", "KEY_ACK"]


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_stop_authority_revocation_prevents_next_write(failure_at):
    stream = _GuestStream(await_stop=True)
    control = MonitorStopControl()
    checks = 0

    def ready(_receipt):
        control.mark_ready()
        control.request()
        stream.send_would_block = True

    def guard():
        nonlocal checks
        checks += 1
        if checks == failure_at:
            raise OCILifecycleTransportError("authority revoked")

    with pytest.raises(OCILifecycleTransportError, match="authority revoked"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=ready,
            session=_session(BINDING),
            stop_control=control,
            before_stop_send=guard,
        )
    assert checks == failure_at
    assert "STOP" not in stream.sent_kinds
    assert control.accepted and control.request() == "control-lost"
    assert stream.abort_calls == stream.free_calls == 1


@pytest.mark.parametrize("mode", ["backpressure", "no-terminal", "partial-terminal", "partial-header"])
def test_stop_deadline_bounds_backpressure_terminal_wait_and_partial_input(monkeypatch, mode):
    clock = [0.0]
    monkeypatch.setattr("palimpsest_local.oci_monitor_control.time", SimpleNamespace(monotonic=lambda: clock[0]))
    stream = _GuestStream(await_stop=True)
    control = MonitorStopControl()
    original_send = stream.send
    admitted = False

    def send(payload):
        if admitted and mode == "backpressure":
            return -2
        result = original_send(payload)
        if "STOP" in stream.sent_kinds:
            stream.guest.clear()
        return result

    def ready(_receipt):
        nonlocal admitted
        control.mark_ready()
        control.request()
        admitted = True
        if mode == "partial-terminal":
            stream.guest.append(b"\x00\x00")
        elif mode == "partial-header":
            stream.guest.append(b"\x00\x00\x01\x00")

    def wait(_seconds):
        clock[0] += 1
        if admitted:
            assert control.request() == "stop-accepted"

    stream.send = send
    with pytest.raises(OCILifecycleTransportError, match="timed out"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=ready,
            session=_session(BINDING),
            stop_control=control,
            before_stop_send=lambda: None,
            monotonic=lambda: clock[0],
            wait=wait,
        )
    assert clock[0] <= 32
    assert control.request() == "control-lost"
    if mode != "no-terminal":
        assert "STOP" not in stream.sent_kinds


def test_stop_admission_racing_initial_deadline_read_still_bounds_send(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("palimpsest_local.oci_monitor_control.time", SimpleNamespace(monotonic=lambda: clock[0]))
    stream = _GuestStream(await_stop=True)
    control = MonitorStopControl()
    original_take = control.take_stop
    original_send = stream.send
    after_ready = False

    def take():
        control.request()
        return original_take()

    def ready(_receipt):
        nonlocal after_ready
        control.mark_ready()
        after_ready = True

    def wait(_seconds):
        clock[0] += 1
        assert clock[0] < 40

    control.take_stop = take
    stream.send = lambda payload: -2 if after_ready else original_send(payload)
    with pytest.raises(OCILifecycleTransportError, match="timed out"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=ready,
            session=_session(BINDING),
            stop_control=control,
            before_stop_send=lambda: None,
            monotonic=lambda: clock[0],
            wait=wait,
        )
    assert control.accepted and control.request() == "control-lost"


def test_stop_guard_exhausting_deadline_never_writes_stop(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("palimpsest_local.oci_monitor_control.time", SimpleNamespace(monotonic=lambda: clock[0]))
    stream = _GuestStream(await_stop=True)
    control = MonitorStopControl()

    def ready(_receipt):
        control.mark_ready()
        control.request()

    def guard():
        clock[0] = 31.0

    with pytest.raises(OCILifecycleTransportError, match="timed out"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=ready,
            session=_session(BINDING),
            stop_control=control,
            before_stop_send=guard,
            monotonic=lambda: clock[0],
        )
    assert stream.sent_kinds == ["HELLO", "KEY_ACK"]
    assert not stream.host


@pytest.mark.parametrize("tamper", ["tag", "reply", "binding", "trailing"])
def test_stop_still_requires_exact_authenticated_terminal(tamper):
    stream = _GuestStream(await_stop=True)
    control = MonitorStopControl()
    original_send = stream.send

    def ready(_receipt):
        control.mark_ready()
        control.request()

    def send(payload):
        count = original_send(payload)
        if "STOP" in stream.sent_kinds and stream.guest:
            frame = stream.guest[-1]
            terminal = decode_frame(frame)
            if tamper == "tag":
                changed = sign_message(terminal.body, b"x" * 32)
            elif tamper == "reply":
                changed = sign_message(replace(terminal.body, reply_to=123456), KEY)
            elif tamper == "binding":
                changed = sign_message(replace(terminal.body, binding=replace(BINDING, run_id=ATTEMPT_ID)), KEY)
            else:
                changed = terminal
            stream.guest[-1] = encode_frame(changed) + (b"\x00" if tamper == "trailing" else b"")
        return count

    stream.send = send
    with pytest.raises(OCILifecycleTransportError, match="TERMINAL"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=ready,
            session=_session(BINDING),
            stop_control=control,
            before_stop_send=lambda: None,
        )
    assert control.request() == "control-lost"


@pytest.mark.parametrize(
    ("signal_number", "returncode", "category"),
    [(None, 42, "exited"), (15, -15, "signaled")],
)
def test_handoff_handles_fragmentation_coalescing_would_block_and_redacts_receipt(
    signal_number: int | None,
    returncode: int,
    category: str,
) -> None:
    stream = _GuestStream(signal_number=signal_number)
    ready = []

    receipt = complete_initial_lifecycle_handoff(
        stream,
        BINDING,
        on_ready=ready.append,
        session=_session(BINDING),
    )

    assert stream.sent_kinds == ["HELLO", "KEY_ACK"]
    assert stream.abort_calls == stream.free_calls == 1
    assert len(ready) == 1 and ready[0].phase == "ready"
    assert receipt.terminal is not None
    assert receipt.terminal.returncode == returncode
    assert receipt.terminal.category.value == category
    serialized = json.dumps(receipt.to_dict(), sort_keys=True)
    assert KEY.hex() not in serialized
    assert '"tag"' not in serialized
    assert [item["kind"] for item in receipt.to_dict()["transcript"]] == [
        "HELLO",
        "BOOTSTRAP",
        "KEY_ACK",
        "READY",
        "TERMINAL",
    ]


def test_handoff_uses_writable_wait_only_for_send_backpressure_and_removes_events_before_abort() -> None:
    stream = _GuestStream()
    waits: list[str] = []
    cleanup: list[str] = []
    original_abort = stream.abort
    original_free = stream.free

    def abort() -> None:
        cleanup.append("abort")
        original_abort()

    def free() -> None:
        cleanup.append("free")
        original_free()

    stream.abort = abort  # type: ignore[method-assign]
    stream.free = free  # type: ignore[method-assign]
    receipt = complete_initial_lifecycle_handoff(
        stream,
        BINDING,
        on_ready=lambda _receipt: None,
        wait=lambda _seconds: waits.append("readable"),
        wait_writable=lambda _seconds: waits.append("writable"),
        before_stream_close=lambda: cleanup.append("event-remove"),
        session=_session(BINDING),
    )

    assert receipt.phase == "terminal"
    assert waits[0] == "writable"
    assert "readable" in waits
    assert cleanup == ["event-remove", "abort", "free"]


def test_handoff_event_cleanup_failure_retains_stream_without_abort_or_free() -> None:
    stream = _GuestStream()

    def fail_event_cleanup() -> None:
        raise RuntimeError("event cleanup failed")

    with pytest.raises(OCILifecycleTransportError, match="stream event cleanup failed; stream retained"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            before_stream_close=fail_event_cleanup,
            session=_session(BINDING),
        )

    assert stream.abort_calls == stream.free_calls == 0


class _GuestStreamWithoutFree:
    """Mirror the public surface exposed by Python libvirt's virStream."""

    def __init__(self) -> None:
        self.delegate = _GuestStream()

    def send(self, payload: bytes) -> int:
        return self.delegate.send(payload)

    def recv(self, size: int) -> bytes | int:
        return self.delegate.recv(size)

    def abort(self) -> None:
        self.delegate.abort()


def test_handoff_accepts_python_libvirt_stream_without_public_free() -> None:
    stream = _GuestStreamWithoutFree()

    receipt = complete_initial_lifecycle_handoff(
        stream,
        BINDING,
        on_ready=lambda _receipt: None,
        session=_session(BINDING),
    )

    assert receipt.terminal is not None and receipt.terminal.returncode == 42
    assert stream.delegate.abort_calls == 1
    assert stream.delegate.free_calls == 0


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def wait(self, duration: float) -> None:
        self.now += duration


def test_ready_timeout_does_not_cap_unbounded_terminal_wait() -> None:
    clock = _Clock()
    stream = _GuestStream(terminal_delay=10)
    receipt = complete_initial_lifecycle_handoff(
        stream,
        BINDING,
        on_ready=lambda _receipt: None,
        timeout_seconds=0.05,
        terminal_timeout_seconds=None,
        monotonic=clock.monotonic,
        wait=clock.wait,
        session=_session(BINDING),
    )
    assert receipt.terminal is not None and receipt.terminal.returncode == 42
    assert clock.now > 0.05
    assert stream.abort_calls == stream.free_calls == 1


@pytest.mark.parametrize("trailing_terminal_wire", ["frame", "truncated"])
def test_handoff_rejects_trailing_wire_observed_with_terminal(
    trailing_terminal_wire: str,
) -> None:
    stream = _GuestStream(trailing_terminal_wire=trailing_terminal_wire)

    with pytest.raises(OCILifecycleTransportError, match="TERMINAL had trailing frame data"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            session=_session(BINDING),
        )

    assert stream.abort_calls == stream.free_calls == 1


class _BlockedStream:
    def __init__(self, recv_result: bytes | int = -2) -> None:
        self.recv_result = recv_result
        self.abort_calls = 0
        self.free_calls = 0

    def send(self, payload: bytes) -> int:
        return len(payload)

    def recv(self, _size: int) -> bytes | int:
        return self.recv_result

    def abort(self) -> None:
        self.abort_calls += 1

    def free(self) -> None:
        self.free_calls += 1


def test_handoff_timeout_is_monotonic_and_closes_stream() -> None:
    clock = _Clock()
    stream = _BlockedStream()
    with pytest.raises(OCILifecycleTransportError, match="timed out"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            timeout_seconds=0.03,
            monotonic=clock.monotonic,
            wait=clock.wait,
            session=_session(BINDING),
        )
    assert stream.abort_calls == stream.free_calls == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    [(b"", "ended before terminal"), (b"\x00\x00\x00\x01x", "frame is invalid")],
)
def test_handoff_rejects_eof_and_protocol_failure_and_closes_stream(payload: bytes, message: str) -> None:
    stream = _BlockedStream(payload)
    with pytest.raises(OCILifecycleTransportError, match=message):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            session=_session(BINDING),
        )
    assert stream.abort_calls == stream.free_calls == 1


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_handoff_rejects_nonfinite_timeouts_and_closes_stream(timeout: float) -> None:
    stream = _BlockedStream()
    with pytest.raises(OCILifecycleTransportError, match="timeout is invalid"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            timeout_seconds=timeout,
            session=_session(BINDING),
        )
    assert stream.abort_calls == stream.free_calls == 1


@pytest.mark.parametrize("terminal_timeout", [float("nan"), float("inf"), float("-inf")])
def test_handoff_rejects_nonfinite_terminal_timeouts_and_closes_stream(
    terminal_timeout: float,
) -> None:
    stream = _BlockedStream()
    with pytest.raises(OCILifecycleTransportError, match="terminal timeout is invalid"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            terminal_timeout_seconds=terminal_timeout,
            session=_session(BINDING),
        )
    assert stream.abort_calls == stream.free_calls == 1


def test_handoff_invalid_callback_and_session_still_close_stream() -> None:
    callback_stream = _BlockedStream()
    with pytest.raises(OCILifecycleTransportError, match="callback is invalid"):
        complete_initial_lifecycle_handoff(
            callback_stream,
            BINDING,
            on_ready=None,  # type: ignore[arg-type]
            session=_session(BINDING),
        )
    assert callback_stream.abort_calls == callback_stream.free_calls == 1

    invalid_session = _session(BINDING)
    invalid_session.hello()
    session_stream = _BlockedStream()
    with pytest.raises(OCILifecycleTransportError, match="session is invalid"):
        complete_initial_lifecycle_handoff(
            session_stream,
            BINDING,
            on_ready=lambda _receipt: None,
            session=invalid_session,
        )
    assert session_stream.abort_calls == session_stream.free_calls == 1


def test_handoff_validates_stream_surface_before_protocol_and_attempts_cleanup_once() -> None:
    calls = {"abort": 0, "free": 0}

    def count(operation: str) -> None:
        calls[operation] += 1

    stream = type(
        "IncompleteStream",
        (),
        {
            "send": lambda self, payload: len(payload),
            "abort": lambda self: count("abort"),
            "free": lambda self: count("free"),
        },
    )()
    with pytest.raises(OCILifecycleTransportError, match="stream surface is invalid"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            session=_session(BINDING),
        )
    assert calls == {"abort": 1, "free": 1}


def test_handoff_rejects_missing_required_abort_and_attempts_optional_free_once() -> None:
    calls = {"free": 0}
    stream = type(
        "MissingAbortStream",
        (),
        {
            "send": lambda self, payload: len(payload),
            "recv": lambda self, _size: -2,
            "free": lambda self: calls.__setitem__("free", calls["free"] + 1),
        },
    )()

    with pytest.raises(OCILifecycleTransportError, match="stream surface is invalid"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            session=_session(BINDING),
        )

    assert calls == {"free": 1}


@pytest.mark.parametrize("failed_operation", ["abort", "free"])
def test_handoff_success_fails_when_available_stream_cleanup_fails(failed_operation: str) -> None:
    class CleanupFailureGuestStream(_GuestStream):
        def abort(self) -> None:
            self.abort_calls += 1
            if failed_operation == "abort":
                raise RuntimeError("abort failed")

        def free(self) -> None:
            self.free_calls += 1
            if failed_operation == "free":
                raise RuntimeError("free failed")

    stream = CleanupFailureGuestStream()
    with pytest.raises(OCILifecycleTransportError, match="stream cleanup failed"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            session=_session(BINDING),
        )

    assert stream.abort_calls == stream.free_calls == 1


def test_handoff_preserves_primary_failure_when_stream_cleanup_also_fails() -> None:
    class CleanupFailureStream(_BlockedStream):
        def abort(self) -> None:
            self.abort_calls += 1
            raise RuntimeError("abort failed")

        def free(self) -> None:
            self.free_calls += 1
            raise RuntimeError("free failed")

    stream = CleanupFailureStream(b"")
    with pytest.raises(OCILifecycleTransportError, match="ended before terminal"):
        complete_initial_lifecycle_handoff(
            stream,
            BINDING,
            on_ready=lambda _receipt: None,
            session=_session(BINDING),
        )
    assert stream.abort_calls == stream.free_calls == 1
