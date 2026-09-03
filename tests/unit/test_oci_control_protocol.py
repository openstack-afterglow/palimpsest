"""Closed-world tests for the production-inert OCI lifecycle wire contract."""

from __future__ import annotations

import json
import struct
from dataclasses import replace

import pytest

from palimpsest_local.oci_control_protocol import (
    MAX_OCI_CONTROL_FRAME_BYTES,
    HostOCIControlSession,
    OCIControlBinding,
    OCIControlFrameDecoder,
    OCIControlMessage,
    OCIControlProtocolError,
    decode_frame,
    encode_frame,
)

_RUN_ID = "f6f546e2-e734-4920-9eff-1762b348a249"
_BOOT_GENERATION = "b22b1c81-dfa4-478a-b352-27b5b35fe5b7"
_NONCE_1 = "1" * 64
_NONCE_2 = "2" * 64
_NONCE_3 = "3" * 64
_NONCE_4 = "4" * 64


def _binding(**changes: object) -> OCIControlBinding:
    values = {
        "run_id": _RUN_ID,
        "domain_core_digest": "sha256:" + "a" * 64,
        "stage1_artifact_digest": "sha256:" + "b" * 64,
    }
    values.update(changes)
    return OCIControlBinding(**values)  # type: ignore[arg-type]


def _message(
    kind: str = "READY",
    *,
    nonce: str = _NONCE_1,
    sequence: int = 1,
    request_id: int = 1,
    binding: OCIControlBinding | None = None,
    payload: dict[str, object] | None = None,
    boot_generation: str = _BOOT_GENERATION,
    reply_to: int | None = 1,
) -> OCIControlMessage:
    defaults: dict[str, dict[str, object]] = {
        "HELLO": {},
        "READY": {},
        "SNAPSHOT": {"state": "ready", "stop_request_id": None, "terminal": None},
        "STOP": {"signal": 15},
        "TERMINAL": {"terminal": {"exit_code": 0, "signal": None}},
    }
    host_message = kind in {"HELLO", "STOP"}
    return OCIControlMessage(
        kind=kind,
        binding=binding or _binding(),
        host_nonce=nonce,
        payload=defaults[kind] if payload is None else payload,
        request_id=request_id if host_message else None,
        sequence=None if host_message else sequence,
        boot_generation=boot_generation if kind != "HELLO" else None,
        reply_to=None if host_message else reply_to,
    )


def test_all_message_kinds_round_trip_as_bounded_canonical_frames() -> None:
    messages = (
        _message("HELLO"),
        _message("READY"),
        _message("SNAPSHOT"),
        _message("STOP"),
        _message("TERMINAL"),
        _message(
            "SNAPSHOT",
            payload={
                "state": "terminal",
                "stop_request_id": None,
                "terminal": {"exit_code": None, "signal": 9},
            },
        ),
    )
    for message in messages:
        frame = encode_frame(message)
        assert len(frame) <= MAX_OCI_CONTROL_FRAME_BYTES
        assert struct.unpack(">I", frame[:4])[0] == len(frame) - 4
        assert decode_frame(frame) == message
        assert (
            frame[4:]
            == json.dumps(message.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
    assert set(messages[0].to_dict()) == {
        "domain_core_digest",
        "host_nonce",
        "kind",
        "payload",
        "request_id",
        "run_id",
        "schema",
        "stage1_artifact_digest",
    }
    assert "request_id" not in messages[1].to_dict()


def test_incremental_decoder_matches_whole_frame_at_every_boundary() -> None:
    frames = encode_frame(_message("READY")) + encode_frame(_message("TERMINAL", sequence=2))
    expected = (_message("READY"), _message("TERMINAL", sequence=2))
    for split in range(len(frames) + 1):
        decoder = OCIControlFrameDecoder()
        actual = decoder.feed(frames[:split]) + decoder.feed(frames[split:])
        decoder.finish()
        assert actual == expected

    decoder = OCIControlFrameDecoder()
    actual = tuple(message for byte in frames for message in decoder.feed(bytes([byte])))
    decoder.finish()
    assert actual == expected

    one_frame = encode_frame(_message())
    count = MAX_OCI_CONTROL_FRAME_BYTES // len(one_frame) + 2
    combined = one_frame * count
    assert len(combined) > MAX_OCI_CONTROL_FRAME_BYTES
    decoder = OCIControlFrameDecoder()
    assert decoder.feed(combined) == (_message(),) * count
    assert decoder.buffered_bytes == 0
    decoder.finish()


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value.update(extra=True), "fields"),
        (lambda value: value.update(kind="UNKNOWN"), "kind"),
        (lambda value: value.update(run_id="F6F546E2-E734-4920-9EFF-1762B348A249"), "canonical"),
        (lambda value: value.update(domain_core_digest="sha256:" + "A" * 64), "digest"),
        (lambda value: value.update(host_nonce="short"), "nonce"),
        (lambda value: value.update(boot_generation=True), "generation"),
        (lambda value: value.update(sequence=0), "sequence"),
        (lambda value: value.update(reply_to=True), "reply-to"),
        (lambda value: value.update(payload={"extra": 1}), "payload"),
    ],
)
def test_decoder_rejects_unknown_fields_and_wrong_types(mutate, match: str) -> None:
    value = _message().to_dict()
    mutate(value)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(OCIControlProtocolError, match=match):
        decode_frame(struct.pack(">I", len(payload)) + payload)


def test_decoder_rejects_duplicate_noncanonical_oversized_and_truncated_frames() -> None:
    canonical = encode_frame(_message())
    pretty = json.dumps(_message().to_dict(), indent=2, sort_keys=True).encode()
    with pytest.raises(OCIControlProtocolError, match="canonical"):
        decode_frame(struct.pack(">I", len(pretty)) + pretty)

    duplicate = canonical[4:].replace(b'"kind":"READY"', b'"kind":"READY","kind":"READY"')
    with pytest.raises(OCIControlProtocolError, match="duplicate"):
        decode_frame(struct.pack(">I", len(duplicate)) + duplicate)
    with pytest.raises(OCIControlProtocolError, match="length"):
        decode_frame(struct.pack(">I", MAX_OCI_CONTROL_FRAME_BYTES))
    with pytest.raises(OCIControlProtocolError, match="truncated"):
        decode_frame(canonical[:-1])
    with pytest.raises(OCIControlProtocolError, match="trailing"):
        decode_frame(canonical + b"x")

    decoder = OCIControlFrameDecoder()
    decoder.feed(canonical[:-1])
    with pytest.raises(OCIControlProtocolError, match="truncated"):
        decoder.finish()

    decoder = OCIControlFrameDecoder()
    oversized = struct.pack(">I", MAX_OCI_CONTROL_FRAME_BYTES) + b"x" * (MAX_OCI_CONTROL_FRAME_BYTES * 2)
    with pytest.raises(OCIControlProtocolError, match="frame length"):
        decoder.feed(oversized)
    assert decoder.buffered_bytes == 0
    assert decoder.feed(canonical) == (_message(),)
    decoder.finish()


def test_kind_specific_exact_payloads_fail_closed() -> None:
    invalid = (
        ("STOP", {"signal": 9}),
        ("TERMINAL", {"terminal": {"exit_code": 0, "signal": 9}}),
        (
            "SNAPSHOT",
            {"state": "ready", "stop_request_id": None, "terminal": {"exit_code": 0, "signal": None}},
        ),
        ("SNAPSHOT", {"state": "lost", "stop_request_id": None, "terminal": None}),
    )
    for kind, payload in invalid:
        with pytest.raises(OCIControlProtocolError):
            _message(kind, payload=payload)


def test_host_session_correlates_binding_nonce_generation_and_order() -> None:
    nonces = iter((_NONCE_1, _NONCE_2, _NONCE_3, _NONCE_4))
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: next(nonces))
    hello = session.hello()
    assert hello.kind == "HELLO" and hello.host_nonce == _NONCE_1

    for rejected, match in (
        (_message(binding=_binding(run_id="aca88126-d991-4de8-b66b-90dc07904dff")), "binding"),
        (_message(nonce=_NONCE_2), "nonce"),
    ):
        with pytest.raises(OCIControlProtocolError, match=match):
            session.accept(rejected)
    with pytest.raises(OCIControlProtocolError, match="transition"):
        session.accept(_message("TERMINAL", reply_to=hello.request_id))
    session.accept(_message(reply_to=hello.request_id))
    assert session.state == "ready"
    with pytest.raises(OCIControlProtocolError, match="generation"):
        session.accept(
            _message(
                "TERMINAL",
                sequence=2,
                reply_to=None,
                boot_generation="aca88126-d991-4de8-b66b-90dc07904dff",
            )
        )
    with pytest.raises(OCIControlProtocolError, match="ordering"):
        session.accept(_message("TERMINAL", reply_to=None))

    reconnect = session.hello(reconnect=True)
    assert reconnect.host_nonce == _NONCE_2 and reconnect.request_id == 2
    with pytest.raises(OCIControlProtocolError, match="SNAPSHOT"):
        session.accept(_message("READY", nonce=_NONCE_2, sequence=2, reply_to=reconnect.request_id))
    with pytest.raises(OCIControlProtocolError, match="nonce"):
        session.accept(_message("SNAPSHOT", nonce=_NONCE_1, sequence=2, reply_to=reconnect.request_id))
    with pytest.raises(OCIControlProtocolError, match="SNAPSHOT"):
        session.accept(_message("SNAPSHOT", nonce=_NONCE_2, sequence=2, reply_to=hello.request_id))
    session.accept(_message("SNAPSHOT", nonce=_NONCE_2, sequence=2, reply_to=reconnect.request_id))
    assert session.state == "ready"
    stop = session.stop()
    assert stop.kind == "STOP" and stop.payload == {"signal": 15}
    assert stop.request_id == 3 and stop.boot_generation == _BOOT_GENERATION
    session.accept(_message("TERMINAL", nonce=_NONCE_2, sequence=3, reply_to=stop.request_id))
    assert session.state == "terminal"


def test_initial_hello_response_loss_rotates_nonce_but_still_requires_ready() -> None:
    nonces = iter((_NONCE_1, _NONCE_2, _NONCE_3, _NONCE_4))
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: next(nonces))
    first = session.hello()
    retry = session.hello(reconnect=True)
    assert (first.request_id, retry.request_id) == (1, 2)
    with pytest.raises(OCIControlProtocolError, match="nonce"):
        session.accept(_message(nonce=_NONCE_1, sequence=1, reply_to=first.request_id))
    with pytest.raises(OCIControlProtocolError, match="transition"):
        session.accept(_message(nonce=_NONCE_2, sequence=2, reply_to=first.request_id))
    with pytest.raises(OCIControlProtocolError, match="transition"):
        session.accept(
            _message(
                "SNAPSHOT",
                nonce=_NONCE_2,
                sequence=2,
                reply_to=retry.request_id,
                payload={"state": "stopping", "stop_request_id": 9, "terminal": None},
            )
        )
    session.accept(_message(nonce=_NONCE_2, sequence=2, reply_to=retry.request_id))
    assert session.state == "ready"
    with pytest.raises(OCIControlProtocolError, match="ordering"):
        session.accept(_message("TERMINAL", nonce=_NONCE_2, sequence=2, reply_to=None))


def test_initial_hello_response_loss_accepts_only_current_ready_snapshot() -> None:
    nonces = iter((_NONCE_1, _NONCE_2))
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: next(nonces))
    first = session.hello()
    retry = session.hello(reconnect=True)
    with pytest.raises(OCIControlProtocolError, match="nonce"):
        session.accept(_message("SNAPSHOT", nonce=_NONCE_1, sequence=1, reply_to=first.request_id))
    with pytest.raises(OCIControlProtocolError, match="transition"):
        session.accept(_message("SNAPSHOT", nonce=_NONCE_2, sequence=2, reply_to=first.request_id))
    session.accept(_message("SNAPSHOT", nonce=_NONCE_2, sequence=2, reply_to=retry.request_id))
    assert session.state == "ready" and session.boot_generation == _BOOT_GENERATION


def test_reconnect_hello_response_loss_preserves_ready_origin() -> None:
    nonces = iter((_NONCE_1, _NONCE_2, _NONCE_3))
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: next(nonces))
    initial = session.hello()
    session.accept(_message(reply_to=initial.request_id))
    lost = session.hello(reconnect=True)
    retry = session.hello(reconnect=True)
    assert (lost.request_id, retry.request_id) == (2, 3)
    with pytest.raises(OCIControlProtocolError, match="nonce"):
        session.accept(_message("SNAPSHOT", nonce=_NONCE_2, sequence=2, reply_to=lost.request_id))
    session.accept(_message("SNAPSHOT", nonce=_NONCE_3, sequence=2, reply_to=retry.request_id))
    assert session.state == "ready"


def test_reconnect_accepts_exact_terminal_snapshot_and_nonce_never_repeats() -> None:
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: _NONCE_1)
    first = session.hello()
    session.accept(_message(reply_to=first.request_id))
    with pytest.raises(OCIControlProtocolError, match="fresh"):
        session.hello(reconnect=True)

    nonces = iter((_NONCE_1, _NONCE_2, _NONCE_3, _NONCE_4))
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: next(nonces))
    first = session.hello()
    session.accept(_message(reply_to=first.request_id))
    reconnect = session.hello(reconnect=True)
    session.accept(
        _message(
            "SNAPSHOT",
            nonce=_NONCE_2,
            sequence=2,
            reply_to=reconnect.request_id,
            payload={
                "state": "terminal",
                "stop_request_id": None,
                "terminal": {"exit_code": 137, "signal": None},
            },
        )
    )
    assert session.state == "terminal"
    lost = session.hello(reconnect=True)
    retry = session.hello(reconnect=True)
    assert (lost.request_id, retry.request_id) == (3, 4)
    with pytest.raises(OCIControlProtocolError, match="nonce"):
        session.accept(
            _message(
                "SNAPSHOT",
                nonce=_NONCE_3,
                sequence=3,
                reply_to=lost.request_id,
                payload={
                    "state": "terminal",
                    "stop_request_id": None,
                    "terminal": {"exit_code": 137, "signal": None},
                },
            )
        )
    session.accept(
        _message(
            "SNAPSHOT",
            nonce=_NONCE_4,
            sequence=3,
            reply_to=retry.request_id,
            payload={
                "state": "terminal",
                "stop_request_id": None,
                "terminal": {"exit_code": 137, "signal": None},
            },
        )
    )
    assert session.state == "terminal"
    with pytest.raises(OCIControlProtocolError, match="STOP"):
        session.stop()


def test_disconnect_after_stop_preserves_original_request_until_terminal() -> None:
    nonces = iter((_NONCE_1, _NONCE_2, _NONCE_3))
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: next(nonces))
    hello = session.hello()
    session.accept(_message(reply_to=hello.request_id))
    stop = session.stop()
    lost = session.hello(reconnect=True)
    reconnect = session.hello(reconnect=True)
    assert (hello.request_id, stop.request_id, lost.request_id, reconnect.request_id) == (1, 2, 3, 4)

    stopping = {"state": "stopping", "stop_request_id": stop.request_id, "terminal": None}
    with pytest.raises(OCIControlProtocolError, match="STOP request"):
        session.accept(
            _message(
                "SNAPSHOT",
                nonce=_NONCE_3,
                sequence=2,
                reply_to=reconnect.request_id,
                payload={**stopping, "stop_request_id": 99},
            )
        )
    session.accept(
        _message(
            "SNAPSHOT",
            nonce=_NONCE_3,
            sequence=2,
            reply_to=reconnect.request_id,
            payload=stopping,
        )
    )
    assert session.state == "stop-sent"
    with pytest.raises(OCIControlProtocolError, match="ordering"):
        session.accept(
            _message(
                "SNAPSHOT",
                nonce=_NONCE_3,
                sequence=2,
                reply_to=reconnect.request_id,
                payload=stopping,
            )
        )
    with pytest.raises(OCIControlProtocolError, match="transition"):
        session.accept(_message("TERMINAL", nonce=_NONCE_3, sequence=3, reply_to=reconnect.request_id))
    with pytest.raises(OCIControlProtocolError, match="STOP"):
        session.stop()
    session.accept(_message("TERMINAL", nonce=_NONCE_3, sequence=3, reply_to=stop.request_id))
    assert session.state == "terminal"


def test_undelivered_stop_ready_snapshot_enables_only_exact_same_id_retransmission() -> None:
    nonces = iter((_NONCE_1, _NONCE_2))
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: next(nonces))
    hello = session.hello()
    session.accept(_message(reply_to=hello.request_id))
    original = session.stop()
    reconnect = session.hello(reconnect=True)
    session.accept(
        _message(
            "SNAPSHOT",
            nonce=_NONCE_2,
            sequence=2,
            reply_to=reconnect.request_id,
            payload={"state": "ready", "stop_request_id": None, "terminal": None},
        )
    )
    retransmitted = session.stop()
    assert retransmitted.request_id == original.request_id
    assert retransmitted.boot_generation == original.boot_generation
    assert retransmitted.payload == original.payload == {"signal": 15}
    assert retransmitted.host_nonce == _NONCE_2 != original.host_nonce
    with pytest.raises(OCIControlProtocolError, match="STOP requires"):
        session.stop()
    with pytest.raises(OCIControlProtocolError, match="nonce"):
        session.accept(_message("TERMINAL", nonce=_NONCE_1, sequence=3, reply_to=original.request_id))
    session.accept(_message("TERMINAL", nonce=_NONCE_2, sequence=3, reply_to=original.request_id))
    assert session.state == "terminal"


@pytest.mark.parametrize("terminal_stop_binding", [None, "original"])
def test_terminal_snapshot_after_stop_accepts_only_undelivered_or_original_stop(
    terminal_stop_binding: str | None,
) -> None:
    nonces = iter((_NONCE_1, _NONCE_2))
    session = HostOCIControlSession(_binding(), nonce_factory=lambda: next(nonces))
    hello = session.hello()
    session.accept(_message(reply_to=hello.request_id))
    stop = session.stop()
    reconnect = session.hello(reconnect=True)
    terminal = {"exit_code": 143, "signal": None}
    with pytest.raises(OCIControlProtocolError, match="STOP request"):
        session.accept(
            _message(
                "SNAPSHOT",
                nonce=_NONCE_2,
                sequence=2,
                reply_to=reconnect.request_id,
                payload={"state": "terminal", "stop_request_id": 99, "terminal": terminal},
            )
        )
    stop_request_id = stop.request_id if terminal_stop_binding == "original" else None
    session.accept(
        _message(
            "SNAPSHOT",
            nonce=_NONCE_2,
            sequence=2,
            reply_to=reconnect.request_id,
            payload={"state": "terminal", "stop_request_id": stop_request_id, "terminal": terminal},
        )
    )
    assert session.state == "terminal"
    assert session.terminal_stop_request_id == stop_request_id


def test_message_value_objects_reject_stale_generation_and_are_immutable() -> None:
    message = _message()
    with pytest.raises(OCIControlProtocolError, match="generation"):
        replace(message, boot_generation="not-a-uuid")
    with pytest.raises(TypeError):
        message.payload["unexpected"] = True  # type: ignore[index]
    terminal = _message("TERMINAL")
    with pytest.raises(TypeError):
        terminal.payload["terminal"]["exit_code"] = 7  # type: ignore[index]
    encoded = message.to_dict()
    encoded["payload"]["unexpected"] = True
    assert message.payload == {}
