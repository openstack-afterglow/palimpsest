"""Closed-world qualification for the pre-production lifecycle v2 contract."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace

import pytest

from palimpsest_local.oci_control_protocol_v2 import (
    MAX_OCI_CONTROL_CONNECTIONS,
    MAX_OCI_CONTROL_FRAME_BYTES,
    OCI_CONTROL_CHANNEL_CARRIER,
    OCI_CONTROL_CONSOLE_CARRIER,
    HostOCIControlV2Session,
    OCIControlProtocolV2Error,
    OCIControlV2Binding,
    OCIControlV2Envelope,
    OCIControlV2FrameDecoder,
    OCIControlV2Message,
    decode_boundary_line,
    decode_frame,
    encode_boundary_line,
    encode_frame,
    key_identifier,
    lifecycle_state_digest,
    sign_message,
    transcript_projection,
    verify_message_authentication,
)

RUN = "f6f546e2-e734-4920-9eff-1762b348a249"
ATTEMPT = "aca88126-d991-4de8-b66b-90dc07904dff"
GENERATION = "b22b1c81-dfa4-478a-b352-27b5b35fe5b7"
BOUNDARY = "f53b27c5-b2bc-49a3-8878-73d0b054f98f"
BOUNDARY2 = "12aa4a92-b20c-4229-8158-b2bf5e22aa55"
NONCE1, NONCE2 = "1" * 64, "2" * 64
NONCE3 = "3" * 64
KEY = bytes(range(32))
READY_STATE = {"state": "ready", "stop_request_id": None, "terminal": None}
STATE_DIGEST = lifecycle_state_digest("ready", stop_request_id=None, terminal=None)
ROOT_IDENTITY = {
    "schema": "palimpsest.oci-root-identity.v1",
    "pid": 1,
    "filesystem": "overlayfs",
    "device": 42,
    "inode": 99,
}


def binding(**changes: str) -> OCIControlV2Binding:
    values = {
        "run_id": RUN,
        "domain_core_digest": "sha256:" + "a" * 64,
        "stage1_artifact_digest": "sha256:" + "b" * 64,
    }
    values.update(changes)
    return OCIControlV2Binding(**values)


def body(kind: str, *, nonce: str = NONCE1, epoch: int = 1, wire: int = 1, **changes: object) -> OCIControlV2Message:
    payloads: dict[str, dict[str, object]] = {
        "HELLO": {},
        "BOOTSTRAP": {"boot_key": KEY.hex()},
        "KEY_ACK": {},
        "RECONNECT": {"boundary_ack_digest": "sha256:" + "d" * 64, "boundary_id": BOUNDARY},
        "BOUNDARY_ACK": {
            "boundary_id": BOUNDARY,
            "discarded_header_bytes": 0,
            "discarded_payload_bytes": 0,
            "discarded_payload_expected": 0,
            "last_accepted_h2g_wire_sequence": 2,
            "last_attempted_g2h_wire_sequence": wire,
            "lifecycle_state": READY_STATE,
            "previous_epoch": epoch - 1,
            "state_digest": STATE_DIGEST,
        },
        "READY": {},
        "SNAPSHOT": {"state": "ready", "stop_request_id": None, "terminal": None},
        "STOP": {"signal": 15},
        "TERMINAL": {"terminal": {"exit_code": 42, "signal": None}},
    }
    host_request = kind in {"HELLO", "RECONNECT", "STOP"}
    values: dict[str, object] = {
        "kind": kind,
        "binding": binding(),
        "boot_attempt_id": ATTEMPT,
        "host_nonce": nonce,
        "epoch": epoch,
        "wire_sequence": wire,
        "payload": payloads[kind],
        "request_id": 1 if host_request else None,
        "boot_generation": None if kind == "HELLO" else GENERATION,
        "reply_to": None if host_request else 1,
    }
    values.update(changes)
    return OCIControlV2Message(**values)  # type: ignore[arg-type]


def signed(kind: str, **changes: object) -> OCIControlV2Envelope:
    return sign_message(body(kind, **changes), KEY)


def session() -> HostOCIControlV2Session:
    nonces = iter((NONCE1, NONCE2, NONCE3))
    return HostOCIControlV2Session(binding(), nonce_factory=lambda: next(nonces), boot_attempt_factory=lambda: ATTEMPT)


def test_host_enforces_guest_connection_nonce_limit() -> None:
    nonces = iter(f"{index:064x}" for index in range(1, MAX_OCI_CONTROL_CONNECTIONS + 2))
    control = HostOCIControlV2Session(
        binding(), nonce_factory=lambda: next(nonces), boot_attempt_factory=lambda: ATTEMPT
    )
    for _ in range(MAX_OCI_CONTROL_CONNECTIONS):
        control._fresh_nonce()
    with pytest.raises(OCIControlProtocolV2Error, match="connection limit"):
        control._fresh_nonce()


def bootstrap_ready(control: HostOCIControlV2Session) -> tuple[OCIControlV2Envelope, OCIControlV2Envelope]:
    hello = control.hello()
    bootstrap = signed("BOOTSTRAP", reply_to=hello.body.request_id)
    control.accept(bootstrap)
    key_ack = control.key_ack()
    ready = signed("READY", wire=2, reply_to=key_ack.body.wire_sequence)
    control.accept(ready)
    return bootstrap, ready


def boundary_ack(
    *,
    discarded_header: int = 0,
    discarded_payload: int = 0,
    discarded_expected: int = 0,
    last_host_wire: int = 2,
    wire: int = 3,
    state: dict[str, object] | None = None,
) -> tuple[OCIControlV2Envelope, bytes]:
    state = READY_STATE if state is None else state
    state_digest = lifecycle_state_digest(
        str(state["state"]),
        stop_request_id=state["stop_request_id"],  # type: ignore[arg-type]
        terminal=state["terminal"],  # type: ignore[arg-type]
    )
    ack_body = body(
        "BOUNDARY_ACK",
        epoch=2,
        wire=wire,
        reply_to=1,
        payload={
            "boundary_id": BOUNDARY,
            "discarded_header_bytes": discarded_header,
            "discarded_payload_bytes": discarded_payload,
            "discarded_payload_expected": discarded_expected,
            "last_accepted_h2g_wire_sequence": last_host_wire,
            "last_attempted_g2h_wire_sequence": wire,
            "lifecycle_state": state,
            "previous_epoch": 1,
            "state_digest": state_digest,
        },
    )
    ack = sign_message(ack_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    return ack, encode_boundary_line(ack)


def test_outer_envelope_is_exact_canonical_and_bounded() -> None:
    hello = OCIControlV2Envelope(body("HELLO"))
    bootstrap = signed("BOOTSTRAP")
    for envelope in (hello, bootstrap, signed("READY"), signed("STOP"), signed("TERMINAL")):
        frame = encode_frame(envelope)
        assert len(frame) <= MAX_OCI_CONTROL_FRAME_BYTES
        assert struct.unpack(">I", frame[:4])[0] == len(frame) - 4
        assert decode_frame(frame) == envelope
        assert (
            frame[4:]
            == json.dumps(envelope.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
    assert hello.to_dict()["mac"] is None
    assert set(bootstrap.to_dict()) == {"body", "mac"}
    assert set(bootstrap.to_dict()["mac"]) == {"key_id", "tag"}


def test_incremental_decoder_handles_every_split_and_rejects_truncation() -> None:
    frames = encode_frame(signed("READY")) + encode_frame(signed("TERMINAL", wire=2))
    expected = (signed("READY"), signed("TERMINAL", wire=2))
    for split in range(len(frames) + 1):
        decoder = OCIControlV2FrameDecoder()
        assert decoder.feed(frames[:split]) + decoder.feed(frames[split:]) == expected
        decoder.finish()
    decoder = OCIControlV2FrameDecoder()
    decoder.feed(frames[:-1])
    with pytest.raises(OCIControlProtocolV2Error, match="truncated"):
        decoder.finish()


def test_hkdf_mac_binds_body_length_direction_carrier_and_full_identity() -> None:
    ready = signed("READY")
    assert ready.key_id == "sha256:45c364655e70c03aada3bbed089f4b3c365db74c2dd372145e93592d403a73ea"
    assert ready.tag == "ce6d16e4577762a994234ec1a68ddf968fca1e17ec1a65c7273141e880c841ce"
    verify_message_authentication(ready, KEY)
    mutations = (
        replace(ready.body, boot_attempt_id="00000000-0000-4000-8000-000000000000"),
        replace(ready.body, boot_generation="00000000-0000-4000-8000-000000000000"),
        replace(ready.body, epoch=2),
        replace(ready.body, wire_sequence=2),
        replace(ready.body, binding=binding(domain_core_digest="sha256:" + "f" * 64)),
    )
    for mutated in mutations:
        with pytest.raises(OCIControlProtocolV2Error, match="tag"):
            verify_message_authentication(OCIControlV2Envelope(mutated, ready.key_id, ready.tag), KEY)
    with pytest.raises(OCIControlProtocolV2Error, match="carrier"):
        verify_message_authentication(ready, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)


def test_key_id_is_domain_separated_and_wrong_key_rejected() -> None:
    expected = "sha256:" + hashlib.sha256(b"palimpsest.oci-lifecycle-control.v2\0key-id\0" + KEY).hexdigest()
    assert key_identifier(KEY) == expected
    ready = signed("READY")
    with pytest.raises(OCIControlProtocolV2Error, match="key ID"):
        verify_message_authentication(ready, bytes(reversed(KEY)))


def test_strict_lowercase_full_width_mac_and_duplicate_keys_fail_closed() -> None:
    ready = signed("READY")
    value = ready.to_dict()
    value["mac"]["tag"] = ready.tag.upper()  # type: ignore[index,union-attr]
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(OCIControlProtocolV2Error, match="tag"):
        decode_frame(struct.pack(">I", len(payload)) + payload)
    canonical = encode_frame(ready)
    duplicate = canonical[4:].replace(b'"mac":', b'"mac":null,"mac":', 1)
    with pytest.raises(OCIControlProtocolV2Error, match="duplicate"):
        decode_frame(struct.pack(">I", len(duplicate)) + duplicate)


def test_bootstrap_is_self_authenticated_before_key_ack_and_ready() -> None:
    control = session()
    hello = control.hello()
    assert hello.body.kind == "HELLO" and hello.key_id is None
    bootstrap = signed("BOOTSTRAP", reply_to=hello.body.request_id)
    control.accept(bootstrap)
    assert control.state == "bootstrap-received" and control.key_id == key_identifier(KEY)
    ack = control.key_ack()
    assert ack.body.kind == "KEY_ACK" and ack.body.reply_to == bootstrap.body.wire_sequence
    verify_message_authentication(ack, KEY)
    ready = signed("READY", wire=2, reply_to=ack.body.wire_sequence)
    control.accept(ready)
    assert control.state == "ready"


def test_lost_initial_ready_recovers_only_through_authenticated_boundary() -> None:
    control = session()
    hello = control.hello()
    bootstrap = signed("BOOTSTRAP", reply_to=hello.body.request_id)
    control.accept(bootstrap)
    control.key_ack()
    ack, line = boundary_ack(wire=3, last_host_wire=2)
    control.admit_boundary(ack, line)
    assert control.state == "ready"
    reconnect = control.reconnect()
    assert reconnect.body.kind == "RECONNECT"


def test_bootstrap_tamper_cross_boot_and_cross_binding_do_not_mutate_state() -> None:
    factories = (session(), session(), session())
    for control in factories:
        control.hello()
    valid = signed("BOOTSTRAP")
    bad_tag = OCIControlV2Envelope(valid.body, valid.key_id, "f" * 64)
    cross_boot = sign_message(replace(valid.body, boot_attempt_id="00000000-0000-4000-8000-000000000000"), KEY)
    cross_binding = sign_message(
        replace(valid.body, binding=binding(run_id="3a7bacf4-ee0c-419a-808f-4e20ece87ea6")), KEY
    )
    for control, candidate in zip(factories, (bad_tag, cross_boot, cross_binding), strict=True):
        before = (control.state, control.key_id, control.boot_generation)
        with pytest.raises(OCIControlProtocolV2Error):
            control.accept(candidate)
        assert (control.state, control.key_id, control.boot_generation) == before


def test_console_boundary_ack_must_precede_and_bind_reconnect() -> None:
    control = session()
    bootstrap_ready(control)
    with pytest.raises(OCIControlProtocolV2Error, match="BOUNDARY_ACK"):
        control.reconnect()
    ack, line = boundary_ack()
    assert decode_boundary_line(line) == ack
    control.admit_boundary(ack, line)
    reconnect = control.reconnect()
    assert reconnect.body.kind == "RECONNECT" and reconnect.body.epoch == 2
    assert reconnect.body.host_nonce == NONCE2
    assert reconnect.body.payload["boundary_id"] == BOUNDARY
    assert (
        reconnect.body.payload["boundary_ack_digest"]
        == "sha256:"
        + hashlib.sha256(json.dumps(ack.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    verify_message_authentication(reconnect, KEY)


@pytest.mark.parametrize(
    "change,match",
    [
        ({"epoch": 3}, "epoch"),
        ({"wire_sequence": 2}, "ordering"),
        ({"reply_to": 9}, "opener"),
        ({"host_nonce": NONCE2}, "opener"),
    ],
)
def test_boundary_replay_and_identity_mutations_are_rejected(change: dict[str, object], match: str) -> None:
    control = session()
    bootstrap_ready(control)
    ack, _line = boundary_ack()
    mutated = sign_message(replace(ack.body, **change), KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    with pytest.raises(OCIControlProtocolV2Error, match=match):
        control.admit_boundary(mutated, encode_boundary_line(mutated))
    assert control.state == "ready" and control.epoch == 1


def test_boundary_parser_state_and_sequence_commitment_are_exact() -> None:
    control = session()
    bootstrap_ready(control)
    ack, _ = boundary_ack()
    for payload_change in (
        {"last_accepted_h2g_wire_sequence": 1},
        {"last_attempted_g2h_wire_sequence": 4},
        {"previous_epoch": 2},
    ):
        payload = {**ack.body.payload, **payload_change}
        changed = sign_message(replace(ack.body, payload=payload), KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
        with pytest.raises(OCIControlProtocolV2Error):
            control.admit_boundary(changed, encode_boundary_line(changed))
    partial, line = boundary_ack(discarded_header=3)
    control.admit_boundary(partial, line)
    assert control.epoch == 2


@pytest.mark.parametrize(
    ("header", "payload", "expected"),
    [
        (0, 0, 0),
        (1, 0, 0),
        (2, 0, 0),
        (3, 0, 0),
        (0, 0, 1),
        (0, 1, 2),
        (0, 65531, 65532),
    ],
)
def test_boundary_parser_state_accepts_only_reachable_states(header: int, payload: int, expected: int) -> None:
    ack, _ = boundary_ack(
        discarded_header=header,
        discarded_payload=payload,
        discarded_expected=expected,
    )
    assert ack.body.payload["discarded_header_bytes"] == header


@pytest.mark.parametrize(
    ("header", "payload", "expected"),
    [
        (0, 1, 0),
        (1, 1, 0),
        (1, 0, 1),
        (0, 1, 1),
        (0, 2, 1),
        (0, 0, 65533),
    ],
)
def test_boundary_parser_state_rejects_impossible_combinations(header: int, payload: int, expected: int) -> None:
    with pytest.raises(OCIControlProtocolV2Error, match="payload state|parser state"):
        boundary_ack(
            discarded_header=header,
            discarded_payload=payload,
            discarded_expected=expected,
        )


def test_boundary_state_digest_rejection_is_non_mutating() -> None:
    control = session()
    bootstrap_ready(control)
    ack, _ = boundary_ack()
    before = (
        control.state,
        control.epoch,
        control.host_nonce,
        control.key_id,
        control._guest_wire,
        control._boundary,
    )
    with pytest.raises(OCIControlProtocolV2Error, match="state"):
        replace(ack.body, payload={**ack.body.payload, "state_digest": "sha256:" + "f" * 64})
    assert (
        control.state,
        control.epoch,
        control.host_nonce,
        control.key_id,
        control._guest_wire,
        control._boundary,
    ) == before
    with pytest.raises(OCIControlProtocolV2Error, match="BOUNDARY_ACK"):
        control.reconnect()


def test_boot_wide_boundary_id_reuse_is_rejected_without_state_change() -> None:
    control = session()
    bootstrap_ready(control)
    first_ack, first_line = boundary_ack()
    control.admit_boundary(first_ack, first_line)
    reconnect = control.reconnect()
    snapshot = signed(
        "SNAPSHOT",
        nonce=NONCE2,
        epoch=2,
        wire=4,
        reply_to=reconnect.body.request_id,
        payload=READY_STATE,
    )
    control.accept(snapshot)
    reused_body = body(
        "BOUNDARY_ACK",
        nonce=NONCE2,
        epoch=3,
        wire=5,
        reply_to=reconnect.body.request_id,
        payload={
            **first_ack.body.payload,
            "last_accepted_h2g_wire_sequence": reconnect.body.wire_sequence,
            "last_attempted_g2h_wire_sequence": 5,
            "previous_epoch": 2,
        },
    )
    reused = sign_message(reused_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    before = (
        control.state,
        control._epoch,
        control._guest_wire,
        control._boundary,
        control._boundary_state,
        frozenset(control._used_boundary_ids),
    )
    with pytest.raises(OCIControlProtocolV2Error, match="reused"):
        control.admit_boundary(reused, encode_boundary_line(reused))
    assert (
        control.state,
        control._epoch,
        control._guest_wire,
        control._boundary,
        control._boundary_state,
        frozenset(control._used_boundary_ids),
    ) == before


def test_terminal_boundary_commits_exact_public_terminal_state() -> None:
    control = session()
    bootstrap_ready(control)
    terminal = signed("TERMINAL", wire=3, reply_to=None)
    control.accept(terminal)
    terminal_digest = lifecycle_state_digest(
        "terminal", stop_request_id=None, terminal={"exit_code": 42, "signal": None}
    )
    ack_body = body(
        "BOUNDARY_ACK",
        epoch=2,
        wire=4,
        reply_to=1,
        payload={
            "boundary_id": BOUNDARY,
            "discarded_header_bytes": 0,
            "discarded_payload_bytes": 0,
            "discarded_payload_expected": 0,
            "last_accepted_h2g_wire_sequence": 2,
            "last_attempted_g2h_wire_sequence": 4,
            "lifecycle_state": {
                "state": "terminal",
                "stop_request_id": None,
                "terminal": {"exit_code": 42, "signal": None},
            },
            "previous_epoch": 1,
            "state_digest": terminal_digest,
        },
    )
    ack = sign_message(ack_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    control.admit_boundary(ack, encode_boundary_line(ack))
    assert control.epoch == 2


def test_reconnect_snapshot_then_stop_keeps_logical_id_separate_from_wire_sequence() -> None:
    control = session()
    bootstrap_ready(control)
    ack, line = boundary_ack()
    control.admit_boundary(ack, line)
    reconnect = control.reconnect()
    mismatched = signed(
        "SNAPSHOT",
        nonce=NONCE2,
        epoch=2,
        wire=4,
        reply_to=reconnect.body.request_id,
        payload={"state": "stopping", "stop_request_id": 2, "terminal": None},
    )
    with pytest.raises(OCIControlProtocolV2Error, match="differs from BOUNDARY_ACK"):
        control.accept(mismatched)
    snapshot = signed(
        "SNAPSHOT",
        nonce=NONCE2,
        epoch=2,
        wire=4,
        reply_to=reconnect.body.request_id,
    )
    control.accept(snapshot)
    stop = control.stop()
    assert stop.body.request_id == 3
    assert stop.body.wire_sequence == 4
    assert stop.body.request_id != stop.body.wire_sequence
    terminal = signed("TERMINAL", nonce=NONCE2, epoch=2, wire=5, reply_to=stop.body.request_id)
    control.accept(terminal)
    assert control.state == "terminal"


def test_partial_stop_boundary_ready_retries_same_logical_stop_with_new_wire_sequence() -> None:
    control = session()
    bootstrap_ready(control)
    partial_stop = control.stop()
    assert partial_stop.body.request_id == 2
    assert partial_stop.body.wire_sequence == 3

    ack, line = boundary_ack(last_host_wire=2, state=READY_STATE)
    control.admit_boundary(ack, line)
    reconnect = control.reconnect()
    snapshot = signed(
        "SNAPSHOT",
        nonce=NONCE2,
        epoch=2,
        wire=4,
        reply_to=reconnect.body.request_id,
        payload=READY_STATE,
    )
    control.accept(snapshot)
    retried_stop = control.stop()
    assert retried_stop.body.request_id == partial_stop.body.request_id == 2
    assert retried_stop.body.wire_sequence == 5
    assert retried_stop.tag != partial_stop.tag


def test_same_stream_stop_retry_reuses_logical_id_on_fresh_authenticated_wire() -> None:
    control = session()
    bootstrap_ready(control)
    first = control.stop()
    retry = control.retry_stop()
    assert retry.body.request_id == first.body.request_id
    assert retry.body.wire_sequence == first.body.wire_sequence + 1
    assert retry.tag != first.tag
    verify_message_authentication(retry, KEY)


def test_partial_reconnect_retries_same_logical_request_with_fresh_epoch_nonce_and_wire() -> None:
    control = session()
    bootstrap_ready(control)
    first_ack, first_line = boundary_ack()
    control.admit_boundary(first_ack, first_line)
    first_reconnect = control.reconnect()

    second_ack_body = body(
        "BOUNDARY_ACK",
        nonce=NONCE1,
        epoch=3,
        wire=4,
        reply_to=1,
        payload={
            **first_ack.body.payload,
            "boundary_id": "40284d98-f031-4bc8-8231-675b29fbc113",
            "last_attempted_g2h_wire_sequence": 4,
            "previous_epoch": 2,
        },
    )
    second_ack = sign_message(second_ack_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    control.admit_boundary(second_ack, encode_boundary_line(second_ack))
    retried = control.reconnect()

    assert retried.body.request_id == first_reconnect.body.request_id
    assert retried.body.wire_sequence == first_reconnect.body.wire_sequence + 1
    assert retried.body.epoch == 3
    assert retried.body.host_nonce == NONCE3
    assert retried.body.payload["boundary_id"] == second_ack.body.payload["boundary_id"]
    assert retried.body.payload["boundary_ack_digest"] != first_reconnect.body.payload["boundary_ack_digest"]

    snapshot = signed(
        "SNAPSHOT",
        nonce=NONCE3,
        epoch=3,
        wire=5,
        reply_to=retried.body.request_id,
        payload=READY_STATE,
    )
    control.accept(snapshot)
    assert control.state == "ready"


def test_accepted_reconnect_with_partial_snapshot_starts_fresh_logical_reconnect() -> None:
    control = session()
    bootstrap_ready(control)
    first_ack, first_line = boundary_ack()
    control.admit_boundary(first_ack, first_line)
    first_reconnect = control.reconnect()

    accepted_ack_body = body(
        "BOUNDARY_ACK",
        nonce=NONCE2,
        epoch=3,
        wire=5,
        reply_to=first_reconnect.body.request_id,
        payload={
            **first_ack.body.payload,
            "boundary_id": "d8e88bd8-2955-4584-8b59-c757c6b16b47",
            "last_accepted_h2g_wire_sequence": first_reconnect.body.wire_sequence,
            "last_attempted_g2h_wire_sequence": 5,
            "previous_epoch": 2,
        },
    )
    accepted_ack = sign_message(accepted_ack_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    control.admit_boundary(accepted_ack, encode_boundary_line(accepted_ack))
    recovery = control.reconnect()

    assert recovery.body.request_id == first_reconnect.body.request_id + 1
    assert recovery.body.wire_sequence == first_reconnect.body.wire_sequence + 1
    assert recovery.body.epoch == 3
    assert recovery.body.host_nonce == NONCE3
    assert recovery.body.payload["boundary_id"] == accepted_ack.body.payload["boundary_id"]

    snapshot = signed(
        "SNAPSHOT",
        nonce=NONCE3,
        epoch=3,
        wire=6,
        reply_to=recovery.body.request_id,
        payload=READY_STATE,
    )
    control.accept(snapshot)
    assert control.state == "ready"


def test_partial_reconnect_rejects_mixed_old_and_attempted_connection_identity() -> None:
    control = session()
    bootstrap_ready(control)
    first_ack, first_line = boundary_ack()
    control.admit_boundary(first_ack, first_line)
    reconnect = control.reconnect()
    mixed_body = body(
        "BOUNDARY_ACK",
        nonce=NONCE2,
        epoch=3,
        wire=4,
        reply_to=1,
        payload={
            **first_ack.body.payload,
            "boundary_id": "b1fce981-6503-4d23-889d-4bd1189a3db9",
            "last_attempted_g2h_wire_sequence": 4,
            "previous_epoch": 2,
        },
    )
    mixed = sign_message(mixed_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    with pytest.raises(OCIControlProtocolV2Error, match="commitment"):
        control.admit_boundary(mixed, encode_boundary_line(mixed))
    assert control.state == "reconnect-sent"
    assert control.host_nonce == reconnect.body.host_nonce


def test_accepted_reconnect_ack_allows_natural_terminal_progression_and_exact_snapshot() -> None:
    control = session()
    bootstrap_ready(control)
    first_ack, first_line = boundary_ack()
    control.admit_boundary(first_ack, first_line)
    first_reconnect = control.reconnect()
    terminal_state = {
        "state": "terminal",
        "stop_request_id": None,
        "terminal": {"exit_code": 42, "signal": None},
    }
    terminal_ack_body = body(
        "BOUNDARY_ACK",
        nonce=NONCE2,
        epoch=3,
        wire=5,
        reply_to=first_reconnect.body.request_id,
        payload={
            **first_ack.body.payload,
            "boundary_id": "830a6c46-8661-4518-863f-0d70972d53fe",
            "last_accepted_h2g_wire_sequence": first_reconnect.body.wire_sequence,
            "last_attempted_g2h_wire_sequence": 5,
            "lifecycle_state": terminal_state,
            "previous_epoch": 2,
            "state_digest": lifecycle_state_digest(
                "terminal",
                stop_request_id=None,
                terminal={"exit_code": 42, "signal": None},
            ),
        },
    )
    terminal_ack = sign_message(terminal_ack_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    control.admit_boundary(terminal_ack, encode_boundary_line(terminal_ack))
    assert control.state == "terminal"
    recovery = control.reconnect()
    snapshot = signed(
        "SNAPSHOT",
        nonce=NONCE3,
        epoch=3,
        wire=6,
        reply_to=recovery.body.request_id,
        payload=terminal_state,
    )
    control.accept(snapshot)
    assert control.state == "terminal"


@pytest.mark.parametrize("last_host_wire", [2, 3])
def test_natural_terminal_after_stop_preserves_accepted_wire_ambiguity_until_boundary(
    last_host_wire: int,
) -> None:
    control = session()
    bootstrap_ready(control)
    stop = control.stop()
    natural = signed("TERMINAL", wire=3, reply_to=None)
    control.accept(natural)
    terminal_state = {
        "state": "terminal",
        "stop_request_id": None,
        "terminal": {"exit_code": 42, "signal": None},
    }
    ack_body = body(
        "BOUNDARY_ACK",
        epoch=2,
        wire=4,
        reply_to=1,
        payload={
            "boundary_id": BOUNDARY,
            "discarded_header_bytes": 0,
            "discarded_payload_bytes": 0,
            "discarded_payload_expected": 0,
            "last_accepted_h2g_wire_sequence": last_host_wire,
            "last_attempted_g2h_wire_sequence": 4,
            "lifecycle_state": terminal_state,
            "previous_epoch": 1,
            "state_digest": lifecycle_state_digest(
                "terminal",
                stop_request_id=None,
                terminal={"exit_code": 42, "signal": None},
            ),
        },
    )
    ack = sign_message(ack_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    control.admit_boundary(ack, encode_boundary_line(ack))
    assert control._accepted_host_wire == last_host_wire
    assert control._accepted_host_wire_candidates is None
    assert stop.body.wire_sequence == 3


def test_stop_caused_terminal_boundary_requires_exact_stop_wire_commitment() -> None:
    control = session()
    bootstrap_ready(control)
    stop = control.stop()
    terminal_state = {
        "state": "terminal",
        "stop_request_id": stop.body.request_id,
        "terminal": {"exit_code": 42, "signal": None},
    }
    ack_body = body(
        "BOUNDARY_ACK",
        epoch=2,
        wire=3,
        reply_to=1,
        payload={
            "boundary_id": BOUNDARY,
            "discarded_header_bytes": 0,
            "discarded_payload_bytes": 0,
            "discarded_payload_expected": 0,
            "last_accepted_h2g_wire_sequence": 2,
            "last_attempted_g2h_wire_sequence": 3,
            "lifecycle_state": terminal_state,
            "previous_epoch": 1,
            "state_digest": lifecycle_state_digest(
                "terminal",
                stop_request_id=stop.body.request_id,
                terminal={"exit_code": 42, "signal": None},
            ),
        },
    )
    stale = sign_message(ack_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    with pytest.raises(OCIControlProtocolV2Error, match="host sequence"):
        control.admit_boundary(stale, encode_boundary_line(stale))

    committed_body = replace(
        ack_body,
        payload={**ack_body.payload, "last_accepted_h2g_wire_sequence": stop.body.wire_sequence},
    )
    committed = sign_message(committed_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    control.admit_boundary(committed, encode_boundary_line(committed))
    assert control._accepted_host_wire == stop.body.wire_sequence


def test_ready_boundary_discards_all_unaccepted_stop_wire_candidates() -> None:
    control = session()
    bootstrap_ready(control)
    first = control.stop()
    second = control.retry_stop()
    ack, line = boundary_ack(last_host_wire=2, state=READY_STATE)
    control.admit_boundary(ack, line)
    assert control._stop_wire_candidates == set()

    reconnect = control.reconnect()
    control.accept(
        signed(
            "SNAPSHOT",
            nonce=NONCE2,
            epoch=2,
            wire=4,
            reply_to=reconnect.body.request_id,
            payload=READY_STATE,
        )
    )
    assert control._stop_wire_candidates == set()
    assert {first.body.wire_sequence, second.body.wire_sequence} == {3, 4}


def test_stopping_boundary_after_accepted_reconnect_preserves_stop_causality() -> None:
    control = session()
    bootstrap_ready(control)
    stop = control.stop()
    stopping = {"state": "stopping", "stop_request_id": stop.body.request_id, "terminal": None}
    ack, line = boundary_ack(last_host_wire=stop.body.wire_sequence, state=stopping)
    control.admit_boundary(ack, line)

    reconnect = control.reconnect()
    control.accept(
        signed(
            "SNAPSHOT",
            nonce=NONCE2,
            epoch=2,
            wire=4,
            reply_to=reconnect.body.request_id,
            payload=stopping,
        )
    )
    next_ack_body = body(
        "BOUNDARY_ACK",
        nonce=NONCE2,
        epoch=3,
        wire=5,
        reply_to=reconnect.body.request_id,
        payload={
            "boundary_id": BOUNDARY2,
            "discarded_header_bytes": 0,
            "discarded_payload_bytes": 0,
            "discarded_payload_expected": 0,
            "last_accepted_h2g_wire_sequence": reconnect.body.wire_sequence,
            "last_attempted_g2h_wire_sequence": 5,
            "lifecycle_state": stopping,
            "previous_epoch": 2,
            "state_digest": lifecycle_state_digest("stopping", stop_request_id=stop.body.request_id, terminal=None),
        },
    )
    next_ack = sign_message(next_ack_body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    control.admit_boundary(next_ack, encode_boundary_line(next_ack))
    assert control._accepted_host_wire == reconnect.body.wire_sequence
    assert control._stop_wire_candidates == {stop.body.wire_sequence}

    second_reconnect = control.reconnect()
    assert second_reconnect.body.host_nonce == NONCE3


def test_reconnect_snapshot_must_equal_boundary_committed_state() -> None:
    control = session()
    bootstrap_ready(control)
    control.stop()
    stopping = {"state": "stopping", "stop_request_id": 2, "terminal": None}
    ack, line = boundary_ack(last_host_wire=3, state=stopping)
    control.admit_boundary(ack, line)
    reconnect = control.reconnect()
    mismatched = signed(
        "SNAPSHOT",
        nonce=NONCE2,
        epoch=2,
        wire=4,
        reply_to=reconnect.body.request_id,
        payload=READY_STATE,
    )
    before = (control.state, control._guest_wire, control._reconnect_expected_state)
    with pytest.raises(OCIControlProtocolV2Error, match="differs from BOUNDARY_ACK"):
        control.accept(mismatched)
    assert (control.state, control._guest_wire, control._reconnect_expected_state) == before

    matching = sign_message(replace(mismatched.body, payload=stopping), KEY)
    control.accept(matching)
    assert control.state == "stop-sent"


def test_authenticated_semantic_mutation_is_rejected_before_state_change() -> None:
    control = session()
    control.hello()
    control.accept(signed("BOOTSTRAP"))
    ack = control.key_ack()
    ready = signed("READY", wire=2, reply_to=ack.body.wire_sequence)
    mutated = OCIControlV2Envelope(replace(ready.body, reply_to=99), ready.key_id, ready.tag)
    before = (control.state, control.epoch, control.host_nonce)
    with pytest.raises(OCIControlProtocolV2Error, match="tag"):
        control.accept(mutated)
    assert (control.state, control.epoch, control.host_nonce) == before


def test_receipt_projection_and_repr_never_expose_boot_key_or_tag() -> None:
    bootstrap = signed("BOOTSTRAP")
    frame = encode_frame(bootstrap)
    projection = transcript_projection(bootstrap, frame, carrier=OCI_CONTROL_CHANNEL_CARRIER, key=KEY)
    rendered = repr(bootstrap) + repr(projection)
    assert KEY.hex() not in rendered
    assert bootstrap.tag not in rendered
    assert set(projection) == {
        "authentication_verified",
        "body_digest",
        "boot_attempt_id",
        "boot_generation",
        "carrier",
        "direction",
        "envelope_digest",
        "epoch",
        "host_nonce",
        "key_id",
        "kind",
        "projection_digest",
        "reply_to",
        "request_id",
        "size_bytes",
        "wire_sequence",
    }


def test_ready_root_identity_is_closed_world_and_retained_only_after_authentication() -> None:
    ready = signed("READY", payload={"root_identity": ROOT_IDENTITY})
    projection = transcript_projection(ready, encode_frame(ready), carrier=OCI_CONTROL_CHANNEL_CARRIER, key=KEY)
    assert projection["root_identity"] == ROOT_IDENTITY
    assert projection["run_id"] == RUN
    assert projection["domain_core_digest"] == binding().domain_core_digest
    assert projection["stage1_artifact_digest"] == binding().stage1_artifact_digest


@pytest.mark.parametrize(
    "identity",
    [
        {**ROOT_IDENTITY, "pid": True},
        {**ROOT_IDENTITY, "device": True},
        {**ROOT_IDENTITY, "device": -1},
        {**ROOT_IDENTITY, "device": 1 << 64},
        {**ROOT_IDENTITY, "inode": 0},
        {**ROOT_IDENTITY, "inode": 1 << 64},
        {**ROOT_IDENTITY, "filesystem": "ext4"},
        {**ROOT_IDENTITY, "extra": 1},
    ],
)
def test_ready_root_identity_rejects_types_ranges_and_unknown_fields(identity: dict[str, object]) -> None:
    with pytest.raises(OCIControlProtocolV2Error, match="root identity"):
        body("READY", payload={"root_identity": identity})


def test_ready_legacy_empty_payload_remains_protocol_compatible() -> None:
    assert body("READY").payload == {}
    with pytest.raises(OCIControlProtocolV2Error, match="READY payload"):
        body("READY", payload={"unexpected": ROOT_IDENTITY})


def test_carrier_kind_matrix_and_projection_evidence_are_fail_closed() -> None:
    hello = OCIControlV2Envelope(body("HELLO"))
    ready = signed("READY")
    ack, ack_line = boundary_ack()
    with pytest.raises(OCIControlProtocolV2Error, match="console carrier"):
        sign_message(ready.body, KEY, carrier=OCI_CONTROL_CONSOLE_CARRIER)
    with pytest.raises(OCIControlProtocolV2Error, match="console carrier"):
        encode_boundary_line(ready)
    with pytest.raises(OCIControlProtocolV2Error, match="console carrier"):
        transcript_projection(ready, encode_frame(ready), carrier=OCI_CONTROL_CONSOLE_CARRIER, key=KEY)
    with pytest.raises(OCIControlProtocolV2Error, match="console carrier"):
        verify_message_authentication(ack, KEY)
    assert (
        transcript_projection(hello, encode_frame(hello), carrier=OCI_CONTROL_CHANNEL_CARRIER, key=None)[
            "authentication_verified"
        ]
        is False
    )
    with pytest.raises(OCIControlProtocolV2Error, match="unsigned"):
        transcript_projection(hello, encode_frame(hello), carrier=OCI_CONTROL_CHANNEL_CARRIER, key=KEY)
    with pytest.raises(OCIControlProtocolV2Error, match="requires a key"):
        transcript_projection(ready, encode_frame(ready), carrier=OCI_CONTROL_CHANNEL_CARRIER, key=None)
    with pytest.raises(OCIControlProtocolV2Error, match="canonical"):
        transcript_projection(
            ready,
            encode_frame(ready) + b"x",
            carrier=OCI_CONTROL_CHANNEL_CARRIER,
            key=KEY,
        )
    tampered = OCIControlV2Envelope(ready.body, ready.key_id, "f" * 64)
    with pytest.raises(OCIControlProtocolV2Error, match="tag"):
        transcript_projection(
            tampered,
            encode_frame(tampered),
            carrier=OCI_CONTROL_CHANNEL_CARRIER,
            key=KEY,
        )
    assert transcript_projection(ack, ack_line, carrier=OCI_CONTROL_CONSOLE_CARRIER, key=KEY)["kind"] == "BOUNDARY_ACK"


def test_active_v1_protocol_remains_separate_until_atomic_guest_activation() -> None:
    from palimpsest_local.oci_control_protocol import (
        OCI_CONTROL_PROTOCOL,
        OCIControlBinding,
        OCIControlMessage,
    )
    from palimpsest_local.oci_control_protocol import encode_frame as encode_v1_frame

    assert OCI_CONTROL_PROTOCOL == "palimpsest.oci-lifecycle-control.v1"
    assert OCI_CONTROL_PROTOCOL != "palimpsest.oci-lifecycle-control.v2"
    v1 = encode_v1_frame(
        OCIControlMessage(
            "HELLO",
            OCIControlBinding(RUN, "sha256:" + "a" * 64, "sha256:" + "b" * 64),
            NONCE1,
            {},
            request_id=1,
        )
    )
    with pytest.raises(OCIControlProtocolV2Error):
        decode_frame(v1)
