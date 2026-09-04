"""Authenticated OCI lifecycle protocol v2 for the pre-production OCI-root path.

V2 anchors its one unsigned HELLO in the owner-pinned private QEMU Unix peer.
PID 1 then returns a self-authenticated per-boot key and all later traffic is
authenticated. Production runtime dispatch remains disabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import struct
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .errors import ArtifactValidationError
from .oci_provenance import canonical_json_bytes

OCI_CONTROL_PROTOCOL_V2 = "palimpsest.oci-lifecycle-control.v2"
OCI_CONTROL_CHANNEL_CARRIER = "channel-frame"
OCI_CONTROL_CONSOLE_CARRIER = "console-line"
OCI_CONTROL_BOUNDARY_PREFIX = b"palimpsest guest stage1: lifecycle boundary ack "
MAX_OCI_CONTROL_FRAME_BYTES = 64 * 1024
MAX_OCI_CONTROL_CONNECTIONS = 16
_MAX_PAYLOAD_BYTES = MAX_OCI_CONTROL_FRAME_BYTES - 4
_MAX_COUNTER = (1 << 63) - 1
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{64}$")
_KINDS = frozenset(
    {"HELLO", "BOOTSTRAP", "KEY_ACK", "RECONNECT", "BOUNDARY_ACK", "READY", "SNAPSHOT", "STOP", "TERMINAL"}
)
_HOST_KINDS = frozenset({"HELLO", "KEY_ACK", "RECONNECT", "STOP"})
_BODY_FIELDS = {
    "boot_attempt_id",
    "domain_core_digest",
    "epoch",
    "host_nonce",
    "kind",
    "payload",
    "run_id",
    "schema",
    "stage1_artifact_digest",
    "wire_sequence",
}


class OCIControlProtocolV2Error(ArtifactValidationError):
    """Stable fail-closed v2 wire or state-machine validation failure."""


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OCIControlProtocolV2Error(f"{label} fields are invalid")
    normalized = dict(value)
    if set(normalized) != fields:
        raise OCIControlProtocolV2Error(f"{label} fields are invalid")
    return normalized


def _uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise OCIControlProtocolV2Error(f"{label} is invalid")
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise OCIControlProtocolV2Error(f"{label} is invalid") from None
    if canonical != value:
        raise OCIControlProtocolV2Error(f"{label} is not canonical")
    return value


def _counter(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_COUNTER:
        raise OCIControlProtocolV2Error(f"{label} is invalid")
    return value


def _hex32(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HEX32_RE.fullmatch(value) is None:
        raise OCIControlProtocolV2Error(f"{label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise OCIControlProtocolV2Error(f"{label} is invalid")
    return value


def _terminal(value: Any) -> dict[str, int | None]:
    terminal = _exact(value, {"exit_code", "signal"}, "terminal")
    exit_code, signal = terminal["exit_code"], terminal["signal"]
    if (exit_code is None) == (signal is None):
        raise OCIControlProtocolV2Error("terminal status must contain exactly one result")
    if exit_code is not None and (type(exit_code) is not int or not 0 <= exit_code <= 255):
        raise OCIControlProtocolV2Error("terminal exit code is invalid")
    if signal is not None and (type(signal) is not int or not 1 <= signal <= 64):
        raise OCIControlProtocolV2Error("terminal signal is invalid")
    return {"exit_code": exit_code, "signal": signal}


def _freeze(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze(item) if isinstance(item, Mapping) else item for key, item in value.items()})


def _plain(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _plain(item) if isinstance(item, Mapping) else item for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class OCIControlV2Binding:
    run_id: str
    domain_core_digest: str
    stage1_artifact_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid(self.run_id, "run ID"))
        object.__setattr__(self, "domain_core_digest", _digest(self.domain_core_digest, "domain-core digest"))
        object.__setattr__(self, "stage1_artifact_digest", _digest(self.stage1_artifact_digest, "stage-1 digest"))


@dataclass(frozen=True, slots=True)
class OCIControlV2Message:
    kind: str
    binding: OCIControlV2Binding
    boot_attempt_id: str
    host_nonce: str
    epoch: int
    wire_sequence: int
    payload: Mapping[str, Any] = field(repr=False)
    request_id: int | None = None
    boot_generation: str | None = None
    reply_to: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise OCIControlProtocolV2Error("lifecycle message kind is invalid")
        if not isinstance(self.binding, OCIControlV2Binding):
            raise OCIControlProtocolV2Error("lifecycle binding is invalid")
        _uuid(self.boot_attempt_id, "boot attempt ID")
        _hex32(self.host_nonce, "host nonce")
        _counter(self.epoch, "epoch")
        _counter(self.wire_sequence, "wire sequence")
        payload = dict(self.payload) if isinstance(self.payload, Mapping) else None
        if payload is None:
            raise OCIControlProtocolV2Error("lifecycle payload is invalid")
        if self.kind == "HELLO":
            _exact(payload, set(), "HELLO payload")
            _counter(self.request_id, "request ID")
            if self.epoch != 1 or self.boot_generation is not None or self.reply_to is not None:
                raise OCIControlProtocolV2Error("HELLO fields are invalid")
        else:
            _uuid(self.boot_generation, "boot generation")
            if self.kind in {"RECONNECT", "STOP"}:
                _counter(self.request_id, "request ID")
                if self.reply_to is not None:
                    raise OCIControlProtocolV2Error(f"{self.kind} fields are invalid")
            elif self.request_id is not None:
                raise OCIControlProtocolV2Error(f"{self.kind} fields are invalid")
            if self.kind == "BOOTSTRAP":
                _hex32(_exact(payload, {"boot_key"}, "BOOTSTRAP payload")["boot_key"], "boot key")
                _counter(self.reply_to, "reply-to request ID")
                if self.epoch != 1:
                    raise OCIControlProtocolV2Error("BOOTSTRAP epoch is invalid")
            elif self.kind == "KEY_ACK":
                _exact(payload, set(), "KEY_ACK payload")
                _counter(self.reply_to, "reply-to guest wire sequence")
                if self.epoch != 1:
                    raise OCIControlProtocolV2Error("KEY_ACK epoch is invalid")
            elif self.kind == "RECONNECT":
                reconnect = _exact(payload, {"boundary_ack_digest", "boundary_id"}, "RECONNECT payload")
                _digest(reconnect["boundary_ack_digest"], "boundary acknowledgement digest")
                _uuid(reconnect["boundary_id"], "boundary ID")
                if self.epoch <= 1:
                    raise OCIControlProtocolV2Error("RECONNECT epoch is invalid")
            elif self.kind == "BOUNDARY_ACK":
                boundary = _exact(
                    payload,
                    {
                        "boundary_id",
                        "discarded_header_bytes",
                        "discarded_payload_bytes",
                        "discarded_payload_expected",
                        "last_accepted_h2g_wire_sequence",
                        "last_attempted_g2h_wire_sequence",
                        "lifecycle_state",
                        "previous_epoch",
                        "state_digest",
                    },
                    "BOUNDARY_ACK payload",
                )
                for name in {
                    "discarded_header_bytes",
                    "last_accepted_h2g_wire_sequence",
                    "last_attempted_g2h_wire_sequence",
                    "previous_epoch",
                }:
                    value = boundary[name]
                    if type(value) is not int or not 0 <= value <= _MAX_COUNTER:
                        raise OCIControlProtocolV2Error("BOUNDARY_ACK counter is invalid")
                for name in {"discarded_payload_bytes", "discarded_payload_expected"}:
                    value = boundary[name]
                    if type(value) is not int or not 0 <= value <= _MAX_PAYLOAD_BYTES:
                        raise OCIControlProtocolV2Error("BOUNDARY_ACK parser state is invalid")
                if boundary["discarded_header_bytes"] > 3:
                    raise OCIControlProtocolV2Error("BOUNDARY_ACK header state is invalid")
                header_bytes = boundary["discarded_header_bytes"]
                payload_bytes = boundary["discarded_payload_bytes"]
                payload_expected = boundary["discarded_payload_expected"]
                parser_state_is_valid = (
                    (header_bytes == 0 and payload_bytes == 0 and payload_expected == 0)
                    or (1 <= header_bytes <= 3 and payload_bytes == 0 and payload_expected == 0)
                    or (
                        header_bytes == 0
                        and 1 <= payload_expected <= _MAX_PAYLOAD_BYTES
                        and 0 <= payload_bytes < payload_expected
                    )
                )
                if not parser_state_is_valid:
                    raise OCIControlProtocolV2Error("BOUNDARY_ACK payload state is invalid")
                _uuid(boundary["boundary_id"], "boundary ID")
                lifecycle_state = _exact(
                    boundary["lifecycle_state"],
                    {"state", "stop_request_id", "terminal"},
                    "BOUNDARY_ACK lifecycle state",
                )
                expected_state_digest = lifecycle_state_digest(
                    lifecycle_state["state"],
                    stop_request_id=lifecycle_state["stop_request_id"],
                    terminal=lifecycle_state["terminal"],
                )
                if not hmac.compare_digest(
                    _digest(boundary["state_digest"], "lifecycle state digest"),
                    expected_state_digest,
                ):
                    raise OCIControlProtocolV2Error("BOUNDARY_ACK lifecycle state digest is invalid")
                _counter(self.reply_to, "reply-to connection opener request ID")
                if self.epoch <= 1:
                    raise OCIControlProtocolV2Error("BOUNDARY_ACK fields are invalid")
            elif self.kind == "READY":
                _exact(payload, set(), "READY payload")
                _counter(self.reply_to, "reply-to host wire sequence")
            elif self.kind == "SNAPSHOT":
                _counter(self.reply_to, "reply-to request ID")
                state = payload.get("state")
                _exact(payload, {"state", "stop_request_id", "terminal"}, "SNAPSHOT payload")
                stop_id = payload["stop_request_id"]
                if state == "ready" and stop_id is None and payload["terminal"] is None:
                    pass
                elif state == "stopping" and payload["terminal"] is None:
                    _counter(stop_id, "snapshot STOP request ID")
                elif state == "terminal":
                    payload["terminal"] = _terminal(payload["terminal"])
                    if stop_id is not None:
                        _counter(stop_id, "snapshot STOP request ID")
                else:
                    raise OCIControlProtocolV2Error("SNAPSHOT state is invalid")
            elif self.kind == "TERMINAL":
                payload["terminal"] = _terminal(_exact(payload, {"terminal"}, "TERMINAL payload")["terminal"])
                if self.reply_to is not None:
                    _counter(self.reply_to, "reply-to request ID")
            else:
                if _exact(payload, {"signal"}, "STOP payload")["signal"] != 15:
                    raise OCIControlProtocolV2Error("STOP signal must be SIGTERM")
        object.__setattr__(self, "payload", _freeze(payload))

    @property
    def direction(self) -> str:
        return "host-to-guest" if self.kind in _HOST_KINDS else "guest-to-host"

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "boot_attempt_id": self.boot_attempt_id,
            "domain_core_digest": self.binding.domain_core_digest,
            "epoch": self.epoch,
            "host_nonce": self.host_nonce,
            "kind": self.kind,
            "payload": _plain(self.payload),
            "run_id": self.binding.run_id,
            "schema": OCI_CONTROL_PROTOCOL_V2,
            "stage1_artifact_digest": self.binding.stage1_artifact_digest,
            "wire_sequence": self.wire_sequence,
        }
        if self.kind in {"HELLO", "RECONNECT", "STOP"}:
            value["request_id"] = self.request_id
        if self.kind != "HELLO":
            value.update(boot_generation=self.boot_generation, reply_to=self.reply_to)
        return value

    @classmethod
    def from_dict(cls, value: Any) -> OCIControlV2Message:
        if not isinstance(value, dict) or value.get("kind") not in _KINDS:
            raise OCIControlProtocolV2Error("lifecycle message kind is invalid")
        kind = value["kind"]
        fields = set(_BODY_FIELDS)
        if kind in {"HELLO", "RECONNECT", "STOP"}:
            fields.add("request_id")
        if kind != "HELLO":
            fields |= {"boot_generation", "reply_to"}
        data = _exact(value, fields, "lifecycle message body")
        if data["schema"] != OCI_CONTROL_PROTOCOL_V2:
            raise OCIControlProtocolV2Error("lifecycle protocol schema is unsupported")
        return cls(
            kind,
            OCIControlV2Binding(data["run_id"], data["domain_core_digest"], data["stage1_artifact_digest"]),
            data["boot_attempt_id"],
            data["host_nonce"],
            data["epoch"],
            data["wire_sequence"],
            data["payload"],
            request_id=data.get("request_id"),
            boot_generation=data.get("boot_generation"),
            reply_to=data.get("reply_to"),
        )


@dataclass(frozen=True, slots=True)
class OCIControlV2Envelope:
    body: OCIControlV2Message
    key_id: str | None = None
    tag: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.body, OCIControlV2Message):
            raise OCIControlProtocolV2Error("lifecycle envelope body is invalid")
        if self.body.kind == "HELLO":
            if self.key_id is not None or self.tag is not None:
                raise OCIControlProtocolV2Error("initial HELLO envelope must be unsigned")
        else:
            _digest(self.key_id, "key ID")
            _hex32(self.tag, "authentication tag")

    def to_dict(self) -> dict[str, Any]:
        mac = None if self.body.kind == "HELLO" else {"key_id": self.key_id, "tag": self.tag}
        return {"body": self.body.to_dict(), "mac": mac}


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OCIControlProtocolV2Error("lifecycle message contains a duplicate key")
        result[key] = value
    return result


def key_identifier(key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise OCIControlProtocolV2Error("lifecycle key is invalid")
    return "sha256:" + hashlib.sha256(OCI_CONTROL_PROTOCOL_V2.encode() + b"\0key-id\0" + key).hexdigest()


def lifecycle_state_digest(
    state: str,
    *,
    stop_request_id: int | None,
    terminal: Mapping[str, Any] | None,
) -> str:
    """Digest the public lifecycle state committed by a boundary ACK."""

    if state == "ready" and stop_request_id is None and terminal is None:
        pass
    elif state == "stopping" and terminal is None:
        _counter(stop_request_id, "state STOP request ID")
    elif state == "terminal":
        if stop_request_id is not None:
            _counter(stop_request_id, "state STOP request ID")
        if terminal is None:
            raise OCIControlProtocolV2Error("terminal lifecycle state is invalid")
        terminal = _terminal(dict(terminal))
    else:
        raise OCIControlProtocolV2Error("lifecycle state is invalid")
    value = {
        "schema": "palimpsest.oci-lifecycle-public-state.v1",
        "state": state,
        "stop_request_id": stop_request_id,
        "terminal": dict(terminal) if terminal is not None else None,
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _validate_carrier_kind(message: OCIControlV2Message, carrier: str) -> None:
    if carrier == OCI_CONTROL_CONSOLE_CARRIER:
        if message.kind != "BOUNDARY_ACK":
            raise OCIControlProtocolV2Error("console carrier requires BOUNDARY_ACK")
    elif carrier == OCI_CONTROL_CHANNEL_CARRIER:
        if message.kind == "BOUNDARY_ACK":
            raise OCIControlProtocolV2Error("BOUNDARY_ACK requires console carrier")
    else:
        raise OCIControlProtocolV2Error("lifecycle carrier is invalid")


def _hkdf_subkey(key: bytes, message: OCIControlV2Message, carrier: str) -> bytes:
    direction = message.direction
    _validate_carrier_kind(message, carrier)
    salt = hashlib.sha256(OCI_CONTROL_PROTOCOL_V2.encode() + b"\0hkdf-salt\0").digest()
    prk = hmac.new(salt, key, hashlib.sha256).digest()
    binding = canonical_json_bytes(
        {
            "boot_attempt_id": message.boot_attempt_id,
            "boot_generation": message.boot_generation,
            "domain_core_digest": message.binding.domain_core_digest,
            "run_id": message.binding.run_id,
            "stage1_artifact_digest": message.binding.stage1_artifact_digest,
        }
    )
    info = (
        OCI_CONTROL_PROTOCOL_V2.encode()
        + b"\0subkey\0"
        + direction.encode()
        + b"\0"
        + carrier.encode()
        + b"\0"
        + binding
    )
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def _mac_input(message: OCIControlV2Message, carrier: str) -> bytes:
    body = canonical_json_bytes(message.to_dict())
    return (
        OCI_CONTROL_PROTOCOL_V2.encode()
        + b"\0frame\0"
        + message.direction.encode()
        + b"\0"
        + carrier.encode()
        + b"\0"
        + struct.pack(">I", len(body))
        + body
    )


def sign_message(
    message: OCIControlV2Message,
    key: bytes,
    *,
    carrier: str = OCI_CONTROL_CHANNEL_CARRIER,
) -> OCIControlV2Envelope:
    if message.kind == "HELLO":
        raise OCIControlProtocolV2Error("initial HELLO cannot be signed")
    _validate_carrier_kind(message, carrier)
    subkey = _hkdf_subkey(key, message, carrier)
    tag = hmac.new(subkey, _mac_input(message, carrier), hashlib.sha256).hexdigest()
    return OCIControlV2Envelope(message, key_identifier(key), tag)


def verify_message_authentication(
    envelope: OCIControlV2Envelope,
    key: bytes,
    *,
    carrier: str = OCI_CONTROL_CHANNEL_CARRIER,
) -> None:
    if envelope.body.kind == "HELLO":
        raise OCIControlProtocolV2Error("initial HELLO has no authentication tag")
    _validate_carrier_kind(envelope.body, carrier)
    if not hmac.compare_digest(envelope.key_id or "", key_identifier(key)):
        raise OCIControlProtocolV2Error("lifecycle key ID is invalid")
    subkey = _hkdf_subkey(key, envelope.body, carrier)
    expected = hmac.new(subkey, _mac_input(envelope.body, carrier), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(envelope.tag or "", expected):
        raise OCIControlProtocolV2Error("lifecycle authentication tag is invalid")


def encode_frame(envelope: OCIControlV2Envelope) -> bytes:
    if not isinstance(envelope, OCIControlV2Envelope):
        raise OCIControlProtocolV2Error("lifecycle envelope is invalid")
    payload = canonical_json_bytes(envelope.to_dict())
    if not payload or len(payload) > _MAX_PAYLOAD_BYTES:
        raise OCIControlProtocolV2Error("lifecycle frame exceeds 64 KiB")
    return struct.pack(">I", len(payload)) + payload


def decode_payload(payload: bytes) -> OCIControlV2Envelope:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_PAYLOAD_BYTES:
        raise OCIControlProtocolV2Error("lifecycle payload size is invalid")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_pairs)
    except OCIControlProtocolV2Error:
        raise
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise OCIControlProtocolV2Error("lifecycle payload JSON is invalid") from None
    outer = _exact(value, {"body", "mac"}, "lifecycle envelope")
    body = OCIControlV2Message.from_dict(outer["body"])
    if body.kind == "HELLO":
        if outer["mac"] is not None:
            raise OCIControlProtocolV2Error("initial HELLO envelope must be unsigned")
        envelope = OCIControlV2Envelope(body)
    else:
        mac = _exact(outer["mac"], {"key_id", "tag"}, "lifecycle MAC")
        envelope = OCIControlV2Envelope(body, mac["key_id"], mac["tag"])
    if canonical_json_bytes(envelope.to_dict()) != payload:
        raise OCIControlProtocolV2Error("lifecycle payload must be canonical UTF-8 JSON")
    return envelope


def decode_frame(frame: bytes) -> OCIControlV2Envelope:
    if not isinstance(frame, bytes) or len(frame) < 4:
        raise OCIControlProtocolV2Error("lifecycle frame is truncated")
    length = struct.unpack(">I", frame[:4])[0]
    if length == 0 or length > _MAX_PAYLOAD_BYTES:
        raise OCIControlProtocolV2Error("lifecycle frame length is invalid")
    if len(frame) != length + 4:
        raise OCIControlProtocolV2Error("lifecycle frame is truncated or contains trailing bytes")
    return decode_payload(frame[4:])


def encode_boundary_line(envelope: OCIControlV2Envelope) -> bytes:
    _validate_carrier_kind(envelope.body, OCI_CONTROL_CONSOLE_CARRIER)
    return OCI_CONTROL_BOUNDARY_PREFIX + canonical_json_bytes(envelope.to_dict()) + b"\n"


def decode_boundary_line(line: bytes) -> OCIControlV2Envelope:
    if not isinstance(line, bytes) or not line.startswith(OCI_CONTROL_BOUNDARY_PREFIX) or not line.endswith(b"\n"):
        raise OCIControlProtocolV2Error("lifecycle boundary line is invalid")
    envelope = decode_payload(line[len(OCI_CONTROL_BOUNDARY_PREFIX) : -1])
    if envelope.body.kind != "BOUNDARY_ACK":
        raise OCIControlProtocolV2Error("console message is not BOUNDARY_ACK")
    return envelope


def transcript_projection(
    envelope: OCIControlV2Envelope,
    encoded: bytes,
    *,
    carrier: str,
    key: bytes | None,
) -> Mapping[str, Any]:
    """Return a receipt-safe projection with neither boot-key nor MAC tag."""

    _validate_carrier_kind(envelope.body, carrier)
    if not isinstance(encoded, bytes) or not encoded:
        raise OCIControlProtocolV2Error("lifecycle transcript input is invalid")
    if envelope.body.kind == "HELLO":
        if key is not None:
            raise OCIControlProtocolV2Error("unsigned HELLO projection requires no key")
        authentication_verified = False
    else:
        if key is None:
            raise OCIControlProtocolV2Error("signed lifecycle projection requires a key")
        verify_message_authentication(envelope, key, carrier=carrier)
        authentication_verified = True
    canonical = encode_frame(envelope) if carrier == OCI_CONTROL_CHANNEL_CARRIER else encode_boundary_line(envelope)
    if not hmac.compare_digest(encoded, canonical):
        raise OCIControlProtocolV2Error("lifecycle transcript encoding is not canonical")
    message = envelope.body
    body_bytes = canonical_json_bytes(message.to_dict())
    projection: dict[str, Any] = {
        "authentication_verified": authentication_verified,
        "body_digest": f"sha256:{hashlib.sha256(body_bytes).hexdigest()}",
        "boot_attempt_id": message.boot_attempt_id,
        "boot_generation": message.boot_generation,
        "carrier": carrier,
        "direction": message.direction,
        "envelope_digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        "epoch": message.epoch,
        "host_nonce": message.host_nonce,
        "key_id": envelope.key_id,
        "kind": message.kind,
        "reply_to": message.reply_to,
        "request_id": message.request_id,
        "size_bytes": len(encoded),
        "wire_sequence": message.wire_sequence,
    }
    projection["projection_digest"] = f"sha256:{hashlib.sha256(canonical_json_bytes(projection)).hexdigest()}"
    return MappingProxyType(projection)


class OCIControlV2FrameDecoder:
    def __init__(self) -> None:
        self._header = bytearray()
        self._payload = bytearray()
        self._expected: int | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._header) + len(self._payload)

    def feed(self, data: bytes) -> tuple[OCIControlV2Envelope, ...]:
        if not isinstance(data, bytes):
            raise OCIControlProtocolV2Error("lifecycle frame chunk must be bytes")
        cursor, envelopes = 0, []
        while cursor < len(data):
            if self._expected is None:
                count = min(4 - len(self._header), len(data) - cursor)
                self._header.extend(data[cursor : cursor + count])
                cursor += count
                if len(self._header) != 4:
                    continue
                self._expected = struct.unpack(">I", self._header)[0]
                self._header.clear()
                if self._expected == 0 or self._expected > _MAX_PAYLOAD_BYTES:
                    self._expected = None
                    raise OCIControlProtocolV2Error("lifecycle frame length is invalid")
            count = min(self._expected - len(self._payload), len(data) - cursor)
            self._payload.extend(data[cursor : cursor + count])
            cursor += count
            if len(self._payload) == self._expected:
                envelopes.append(decode_payload(bytes(self._payload)))
                self._payload.clear()
                self._expected = None
        return tuple(envelopes)

    def finish(self) -> None:
        if self.buffered_bytes or self._expected is not None:
            raise OCIControlProtocolV2Error("lifecycle frame stream is truncated")


class HostOCIControlV2Session:
    """Host state machine; rejected input never changes semantic state."""

    def __init__(
        self,
        binding: OCIControlV2Binding,
        *,
        nonce_factory: Callable[[], str] | None = None,
        boot_attempt_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(binding, OCIControlV2Binding):
            raise OCIControlProtocolV2Error("session binding is invalid")
        self.binding = binding
        self.boot_attempt_id = _uuid((boot_attempt_factory or (lambda: str(uuid.uuid4())))(), "boot attempt ID")
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))
        self._used_nonces: set[str] = set()
        self._used_boundary_ids: set[str] = set()
        self._nonce: str | None = None
        self._key: bytes | None = None
        self._key_id: str | None = None
        self._boot_generation: str | None = None
        self._host_wire = 0
        self._accepted_host_wire = 0
        self._accepted_host_wire_candidates: frozenset[int] | None = None
        self._guest_wire = 0
        self._request = 0
        self._pending_request: int | None = None
        self._stop_request: int | None = None
        self._terminal: Mapping[str, Any] | None = None
        self._terminal_stop_request: int | None = None
        self._epoch = 1
        self._boundary: tuple[str, str] | None = None
        self._boundary_state: Mapping[str, Any] | None = None
        self._reconnect_expected_state: Mapping[str, Any] | None = None
        self._connection_opener_request: int | None = None
        self._accepted_connection_opener_request: int | None = None
        self._accepted_nonce: str | None = None
        self._pending_host_wire: int | None = None
        self._stop_wire: int | None = None
        self._stop_wire_candidates: set[int] = set()
        self._retry_reconnect_request: int | None = None
        self._reconnect_from: str | None = None
        self.state = "new"

    @property
    def key_id(self) -> str | None:
        return self._key_id

    @property
    def boot_generation(self) -> str | None:
        return self._boot_generation

    @property
    def host_nonce(self) -> str | None:
        return self._nonce

    @property
    def epoch(self) -> int:
        return self._epoch

    def transcript_projection(
        self,
        envelope: OCIControlV2Envelope,
        encoded: bytes,
        *,
        carrier: str = OCI_CONTROL_CHANNEL_CARRIER,
    ) -> Mapping[str, Any]:
        """Project one admitted/sent frame without exposing its key or tag."""

        key = None if envelope.body.kind == "HELLO" else self._key
        return transcript_projection(envelope, encoded, carrier=carrier, key=key)

    def assert_receipt_safe(self, serialized: bytes, observed_tags: set[str]) -> None:
        """Fail if receipt material contains this session's raw key or a MAC tag."""

        if not isinstance(serialized, bytes):
            raise OCIControlProtocolV2Error("serialized lifecycle evidence is invalid")
        if self._key is not None and self._key.hex().encode("ascii") in serialized:
            raise OCIControlProtocolV2Error("serialized lifecycle evidence exposes the boot key")
        for tag in observed_tags:
            if not isinstance(tag, str) or _HEX32_RE.fullmatch(tag) is None:
                raise OCIControlProtocolV2Error("observed lifecycle tag is invalid")
            if tag.encode("ascii") in serialized:
                raise OCIControlProtocolV2Error("serialized lifecycle evidence exposes a MAC tag")

    def _fresh_nonce(self) -> str:
        if len(self._used_nonces) >= MAX_OCI_CONTROL_CONNECTIONS:
            raise OCIControlProtocolV2Error("lifecycle connection limit was exhausted")
        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or _HEX32_RE.fullmatch(nonce) is None or nonce in self._used_nonces:
            raise OCIControlProtocolV2Error("nonce factory did not produce a fresh canonical nonce")
        self._used_nonces.add(nonce)
        return nonce

    def hello(self) -> OCIControlV2Envelope:
        if self.state != "new":
            raise OCIControlProtocolV2Error("session HELLO was already sent")
        self._nonce = self._fresh_nonce()
        self._request += 1
        self._host_wire += 1
        self._pending_request = self._request
        self._pending_host_wire = self._host_wire
        self.state = "hello-sent"
        self._connection_opener_request = self._request
        return OCIControlV2Envelope(
            OCIControlV2Message(
                "HELLO",
                self.binding,
                self.boot_attempt_id,
                self._nonce,
                1,
                self._host_wire,
                {},
                request_id=self._request,
            )
        )

    def key_ack(self) -> OCIControlV2Envelope:
        if self.state != "bootstrap-received" or self._key is None or self._boot_generation is None:
            raise OCIControlProtocolV2Error("KEY_ACK requires authenticated BOOTSTRAP")
        self._host_wire += 1
        self._pending_host_wire = self._host_wire
        envelope = sign_message(
            OCIControlV2Message(
                "KEY_ACK",
                self.binding,
                self.boot_attempt_id,
                self._nonce or "",
                1,
                self._host_wire,
                {},
                boot_generation=self._boot_generation,
                reply_to=self._guest_wire,
            ),
            self._key,
        )
        self.state = "key-ack-sent"
        return envelope

    def admit_boundary(self, envelope: OCIControlV2Envelope, encoded_line: bytes) -> None:
        if self._key is None or self._boot_generation is None:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK arrived before BOOTSTRAP")
        verify_message_authentication(envelope, self._key, carrier=OCI_CONTROL_CONSOLE_CARRIER)
        message = envelope.body
        if message.kind != "BOUNDARY_ACK" or message.binding != self.binding:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK binding is invalid")
        if message.boot_attempt_id != self.boot_attempt_id or message.boot_generation != self._boot_generation:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK boot identity is stale")
        if message.epoch != self._epoch + 1:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK epoch is stale")
        if message.wire_sequence <= self._guest_wire:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK ordering is stale")
        payload = message.payload
        if payload["last_attempted_g2h_wire_sequence"] != message.wire_sequence:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK guest sequence is stale")
        if payload["previous_epoch"] != self._epoch:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK previous epoch is stale")
        if self.state not in {"key-ack-sent", "ready", "stop-sent", "terminal", "reconnect-sent"}:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK transition is invalid")
        boundary_state = payload["lifecycle_state"]
        state_name = boundary_state["state"]
        state_stop = boundary_state["stop_request_id"]
        state_terminal = boundary_state["terminal"]
        reconnect_was_accepted: bool | None = None
        boundary_from_key_ack = self.state == "key-ack-sent"
        if self.state == "reconnect-sent":
            previous_state = self._reconnect_expected_state
            if previous_state is None:
                allowed = False
            elif previous_state["state"] == "ready":
                allowed = (state_name == "ready") or (state_name == "terminal" and state_stop is None)
            elif previous_state["state"] == "stopping":
                previous_stop = previous_state["stop_request_id"]
                allowed = (state_name == "stopping" and state_stop == previous_stop) or (
                    state_name == "terminal" and state_stop in {None, previous_stop}
                )
            else:
                allowed = boundary_state == previous_state
            unaccepted_identity = (
                message.host_nonce == self._accepted_nonce
                and message.reply_to == self._accepted_connection_opener_request
                and payload["last_accepted_h2g_wire_sequence"] == self._accepted_host_wire
            )
            accepted_identity = (
                message.host_nonce == self._nonce
                and message.reply_to == self._connection_opener_request
                and payload["last_accepted_h2g_wire_sequence"] == self._pending_host_wire
            )
            if unaccepted_identity == accepted_identity:
                raise OCIControlProtocolV2Error("BOUNDARY_ACK reconnect commitment is stale")
            reconnect_was_accepted = accepted_identity
            expected_host_wire = self._pending_host_wire if accepted_identity else self._accepted_host_wire
        elif boundary_from_key_ack:
            expected_host_wires = {self._pending_host_wire or 0}
            allowed = (state_name == "ready" and state_stop is None) or (
                state_name == "terminal" and state_stop is None
            )
        elif self.state == "ready":
            expected_host_wire = self._accepted_host_wire
            allowed = (state_name == "ready") or (state_name == "terminal" and state_stop is None)
        elif self.state == "stop-sent":
            expected_host_wires = {self._accepted_host_wire, *self._stop_wire_candidates}
            allowed = (
                (state_name == "ready" and state_stop is None)
                or (state_name == "stopping" and state_stop == self._stop_request)
                or (state_name == "terminal" and state_stop in {None, self._stop_request})
            )
            if state_stop is not None and state_stop == self._stop_request and state_name in {"stopping", "terminal"}:
                expected_host_wires = set(self._stop_wire_candidates)
                if self._stop_wire_candidates and self._accepted_host_wire > max(self._stop_wire_candidates):
                    # A later authenticated RECONNECT may be the guest's most
                    # recently accepted host wire while the lifecycle state is
                    # still causally tied to the earlier STOP wire.
                    expected_host_wires.add(self._accepted_host_wire)
            elif state_name == "terminal" and state_stop is None:
                expected_host_wires.add(self._stop_wire or 0)
        else:
            expected_host_wires = set(self._accepted_host_wire_candidates or {self._accepted_host_wire})
            allowed = (
                state_name == "terminal"
                and state_stop == self._terminal_stop_request
                and state_terminal == self._terminal
            )
        if self.state != "reconnect-sent" and (
            message.host_nonce != self._accepted_nonce or message.reply_to != self._accepted_connection_opener_request
        ):
            raise OCIControlProtocolV2Error("BOUNDARY_ACK connection opener is stale")
        if not allowed:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK lifecycle state is stale")
        if self.state == "reconnect-sent":
            expected_host_wires = {expected_host_wire or 0}
        elif self.state == "ready":
            expected_host_wires = {expected_host_wire}
        committed_host_wire = payload["last_accepted_h2g_wire_sequence"]
        if committed_host_wire not in expected_host_wires:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK host sequence is stale")
        canonical_line = encode_boundary_line(envelope)
        if not hmac.compare_digest(encoded_line, canonical_line):
            raise OCIControlProtocolV2Error("BOUNDARY_ACK console encoding is invalid")
        boundary_id = str(payload["boundary_id"])
        if boundary_id in self._used_boundary_ids:
            raise OCIControlProtocolV2Error("BOUNDARY_ACK boundary ID was reused")
        canonical_envelope = canonical_json_bytes(envelope.to_dict())
        boundary_digest = f"sha256:{hashlib.sha256(canonical_envelope).hexdigest()}"
        reconnect_state = (
            {"ready": "ready", "stopping": "stop-sent", "terminal": "terminal"}[state_name]
            if self.state == "reconnect-sent"
            else None
        )
        self._used_boundary_ids.add(boundary_id)
        self._epoch = message.epoch
        self._guest_wire = message.wire_sequence
        self._accepted_host_wire = committed_host_wire
        self._accepted_host_wire_candidates = None
        if state_name in {"stopping", "terminal"} and state_stop is not None and state_stop == self._stop_request:
            if committed_host_wire in self._stop_wire_candidates:
                self._stop_wire_candidates = {committed_host_wire}
            elif not self._stop_wire_candidates or committed_host_wire <= max(self._stop_wire_candidates):
                raise OCIControlProtocolV2Error("BOUNDARY_ACK STOP wire commitment is stale")
            # A newer committed RECONNECT wire does not replace the older wire
            # that actually carried the logical STOP.
        else:
            # A ready boundary proves every in-flight STOP wire was discarded;
            # natural terminal similarly leaves no STOP delivery candidate.
            self._stop_wire_candidates.clear()
        if reconnect_was_accepted:
            self._accepted_nonce = self._nonce
            self._accepted_connection_opener_request = self._connection_opener_request
        self._boundary = (boundary_id, boundary_digest)
        self._boundary_state = _freeze(boundary_state)
        if boundary_from_key_ack:
            self.state = "ready" if state_name == "ready" else "terminal"
            if state_name == "terminal":
                self._terminal = state_terminal
                self._terminal_stop_request = None
            self._pending_request = None
            self._pending_host_wire = None
        elif self.state == "reconnect-sent":
            self.state = reconnect_state or ""
            if state_name == "terminal":
                self._terminal = state_terminal
                self._terminal_stop_request = state_stop
            self._retry_reconnect_request = None if reconnect_was_accepted else self._pending_request
            self._pending_request = None
            self._pending_host_wire = None
            self._reconnect_from = None
            self._reconnect_expected_state = None

    def reconnect(self) -> OCIControlV2Envelope:
        if self._key is None or self._boot_generation is None or self._boundary is None:
            raise OCIControlProtocolV2Error("RECONNECT requires authenticated BOUNDARY_ACK")
        if self.state not in {"ready", "stop-sent", "terminal"}:
            raise OCIControlProtocolV2Error("session is not reconnectable")
        self._reconnect_from = self.state
        self._nonce = self._fresh_nonce()
        if self._retry_reconnect_request is None:
            self._request += 1
            reconnect_request = self._request
        else:
            reconnect_request = self._retry_reconnect_request
            self._retry_reconnect_request = None
        self._host_wire += 1
        self._pending_request = reconnect_request
        self._pending_host_wire = self._host_wire
        boundary_id, digest = self._boundary
        self._boundary = None
        self._reconnect_expected_state = self._boundary_state
        self._boundary_state = None
        self.state = "reconnect-sent"
        self._connection_opener_request = reconnect_request
        return sign_message(
            OCIControlV2Message(
                "RECONNECT",
                self.binding,
                self.boot_attempt_id,
                self._nonce,
                self._epoch,
                self._host_wire,
                {"boundary_ack_digest": digest, "boundary_id": boundary_id},
                request_id=reconnect_request,
                boot_generation=self._boot_generation,
            ),
            self._key,
        )

    def stop(self) -> OCIControlV2Envelope:
        if self.state != "ready" or self._key is None or self._boot_generation is None:
            raise OCIControlProtocolV2Error("STOP requires a current ready snapshot")
        self._host_wire += 1
        self._stop_wire = self._host_wire
        self._stop_wire_candidates.add(self._host_wire)
        if self._stop_request is None:
            self._request += 1
            self._stop_request = self._request
        envelope = sign_message(
            OCIControlV2Message(
                "STOP",
                self.binding,
                self.boot_attempt_id,
                self._nonce or "",
                self._epoch,
                self._host_wire,
                {"signal": 15},
                request_id=self._stop_request,
                boot_generation=self._boot_generation,
            ),
            self._key,
        )
        self.state = "stop-sent"
        return envelope

    def retry_stop(self) -> OCIControlV2Envelope:
        """Retry one admitted logical STOP on a fresh authenticated wire."""

        if (
            self.state != "stop-sent"
            or self._key is None
            or self._boot_generation is None
            or self._stop_request is None
        ):
            raise OCIControlProtocolV2Error("STOP retry requires an outstanding logical STOP")
        self._host_wire += 1
        self._stop_wire = self._host_wire
        self._stop_wire_candidates.add(self._host_wire)
        return sign_message(
            OCIControlV2Message(
                "STOP",
                self.binding,
                self.boot_attempt_id,
                self._nonce or "",
                self._epoch,
                self._host_wire,
                {"signal": 15},
                request_id=self._stop_request,
                boot_generation=self._boot_generation,
            ),
            self._key,
        )

    def accept(self, envelope: OCIControlV2Envelope) -> None:
        if not isinstance(envelope, OCIControlV2Envelope) or envelope.body.binding != self.binding:
            raise OCIControlProtocolV2Error("guest lifecycle binding is invalid")
        message = envelope.body
        if message.kind == "BOOTSTRAP":
            if self.state != "hello-sent" or message.reply_to != self._pending_request:
                raise OCIControlProtocolV2Error("BOOTSTRAP transition is invalid")
            if message.boot_attempt_id != self.boot_attempt_id or message.host_nonce != self._nonce:
                raise OCIControlProtocolV2Error("BOOTSTRAP identity is stale")
            key = bytes.fromhex(str(message.payload["boot_key"]))
            verify_message_authentication(envelope, key)
            if message.epoch != 1 or message.wire_sequence != 1:
                raise OCIControlProtocolV2Error("BOOTSTRAP ordering is stale")
            self._key = key
            self._key_id = envelope.key_id
            self._boot_generation = message.boot_generation
            self._guest_wire = message.wire_sequence
            self._accepted_host_wire = self._pending_host_wire or 0
            self._accepted_nonce = self._nonce
            self._accepted_connection_opener_request = self._connection_opener_request
            self._pending_host_wire = None
            self.state = "bootstrap-received"
            return
        if self._key is None or self._boot_generation is None:
            raise OCIControlProtocolV2Error("guest message arrived before BOOTSTRAP")
        verify_message_authentication(envelope, self._key)
        if message.boot_attempt_id != self.boot_attempt_id or message.boot_generation != self._boot_generation:
            raise OCIControlProtocolV2Error("guest lifecycle boot identity is stale")
        if message.host_nonce != self._nonce or message.epoch != self._epoch:
            raise OCIControlProtocolV2Error("guest lifecycle nonce or epoch is stale")
        if message.wire_sequence <= self._guest_wire:
            raise OCIControlProtocolV2Error("guest lifecycle wire ordering is stale")
        accepted_host_wire_candidates: frozenset[int] | None = None
        if self.state == "key-ack-sent" and message.kind == "READY" and message.reply_to == self._host_wire:
            next_state = "ready"
            accepted_host_wire = self._host_wire
        elif (
            self.state == "reconnect-sent" and message.kind == "SNAPSHOT" and message.reply_to == self._pending_request
        ):
            if self._reconnect_expected_state is None or message.payload != self._reconnect_expected_state:
                raise OCIControlProtocolV2Error("reconnect SNAPSHOT differs from BOUNDARY_ACK state")
            snapshot = message.payload["state"]
            if snapshot == "ready" and self._reconnect_from not in {"ready", "stop-sent"}:
                raise OCIControlProtocolV2Error("reconnect lifecycle state regressed")
            if snapshot == "stopping" and (
                self._reconnect_from != "stop-sent" or message.payload["stop_request_id"] != self._stop_request
            ):
                raise OCIControlProtocolV2Error("reconnect STOP identity is invalid")
            if snapshot == "terminal":
                stop_id = message.payload["stop_request_id"]
                if self._reconnect_from == "ready" and stop_id is not None:
                    raise OCIControlProtocolV2Error("natural terminal snapshot has STOP identity")
                if self._reconnect_from == "stop-sent" and stop_id not in {None, self._stop_request}:
                    raise OCIControlProtocolV2Error("terminal snapshot STOP identity is invalid")
                if self._reconnect_from == "terminal" and (
                    message.payload["terminal"] != self._terminal or stop_id != self._terminal_stop_request
                ):
                    raise OCIControlProtocolV2Error("terminal snapshot changed")
            next_state = {"ready": "ready", "stopping": "stop-sent", "terminal": "terminal"}[snapshot]
            accepted_host_wire = self._pending_host_wire
            if snapshot == "terminal":
                self._terminal = message.payload["terminal"]
                self._terminal_stop_request = message.payload["stop_request_id"]
        elif self.state == "ready" and message.kind == "TERMINAL" and message.reply_to is None:
            next_state = "terminal"
            accepted_host_wire = self._accepted_host_wire
            self._terminal = message.payload["terminal"]
            self._terminal_stop_request = None
        elif (
            self.state == "stop-sent" and message.kind == "TERMINAL" and message.reply_to in {None, self._stop_request}
        ):
            next_state = "terminal"
            if message.reply_to is None:
                accepted_host_wire = self._accepted_host_wire
                accepted_host_wire_candidates = frozenset({self._accepted_host_wire, self._stop_wire or 0})
            elif self._stop_wire_candidates and self._accepted_host_wire > max(self._stop_wire_candidates):
                # An authenticated reconnect/SNAPSHOT accepted after STOP is
                # necessarily the newest host wire, even though TERMINAL still
                # replies to the logical STOP request ID.
                accepted_host_wire = self._accepted_host_wire
                accepted_host_wire_candidates = frozenset({self._accepted_host_wire})
            else:
                accepted_host_wire = self._stop_wire
                accepted_host_wire_candidates = frozenset(self._stop_wire_candidates)
            self._terminal = message.payload["terminal"]
            self._terminal_stop_request = message.reply_to
        else:
            raise OCIControlProtocolV2Error("guest lifecycle transition is invalid")
        self._guest_wire = message.wire_sequence
        self._accepted_host_wire = accepted_host_wire or 0
        self._accepted_host_wire_candidates = accepted_host_wire_candidates
        if message.kind in {"READY", "SNAPSHOT"}:
            self._accepted_nonce = self._nonce
            self._accepted_connection_opener_request = self._connection_opener_request
        if message.kind in {"READY", "SNAPSHOT"} and message.payload.get("state", "ready") == "ready":
            self._stop_wire_candidates.clear()
        self._pending_request = None
        self._pending_host_wire = None
        self._reconnect_from = None
        self._reconnect_expected_state = None
        self.state = next_state


__all__ = [
    "MAX_OCI_CONTROL_CONNECTIONS",
    "MAX_OCI_CONTROL_FRAME_BYTES",
    "OCI_CONTROL_BOUNDARY_PREFIX",
    "OCI_CONTROL_CHANNEL_CARRIER",
    "OCI_CONTROL_CONSOLE_CARRIER",
    "OCI_CONTROL_PROTOCOL_V2",
    "HostOCIControlV2Session",
    "OCIControlProtocolV2Error",
    "OCIControlV2Binding",
    "OCIControlV2Envelope",
    "OCIControlV2FrameDecoder",
    "OCIControlV2Message",
    "decode_boundary_line",
    "decode_frame",
    "decode_payload",
    "encode_boundary_line",
    "encode_frame",
    "key_identifier",
    "lifecycle_state_digest",
    "sign_message",
    "transcript_projection",
    "verify_message_authentication",
]
