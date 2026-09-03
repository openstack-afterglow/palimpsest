"""Transport-neutral OCI-root lifecycle control wire contract.

The protocol is intentionally production-inert: it defines bounded frames and
the host-side replay/order checks needed by a future virtio-serial consumer,
but it does not open a channel or dispatch lifecycle operations.
"""

from __future__ import annotations

import json
import re
import secrets
import struct
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .errors import ArtifactValidationError
from .oci_provenance import canonical_json_bytes

OCI_CONTROL_PROTOCOL = "palimpsest.oci-lifecycle-control.v1"
OCI_CONTROL_CHANNEL_NAME = "org.palimpsest.oci.lifecycle.0"
MAX_OCI_CONTROL_FRAME_BYTES = 64 * 1024
_FRAME_HEADER_BYTES = 4
_MAX_PAYLOAD_BYTES = MAX_OCI_CONTROL_FRAME_BYTES - _FRAME_HEADER_BYTES
_MAX_COUNTER = (1 << 63) - 1
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_KINDS = frozenset({"HELLO", "READY", "SNAPSHOT", "STOP", "TERMINAL"})
_COMMON_FIELDS = {"domain_core_digest", "host_nonce", "kind", "run_id", "schema", "stage1_artifact_digest"}


class OCIControlProtocolError(ArtifactValidationError):
    """A stable fail-closed lifecycle wire or session validation failure."""


def _exact_object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise OCIControlProtocolError(f"{label} fields are invalid")
    return value


def _canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise OCIControlProtocolError(f"{label} is invalid")
    try:
        parsed = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise OCIControlProtocolError(f"{label} is invalid") from None
    if parsed != value:
        raise OCIControlProtocolError(f"{label} is not canonical")
    return value


def _counter(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_COUNTER:
        raise OCIControlProtocolError(f"{label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise OCIControlProtocolError(f"{label} is invalid")
    return value


def _terminal(value: Any) -> dict[str, int | None]:
    terminal = _exact_object(value, {"exit_code", "signal"}, "terminal")
    exit_code = terminal["exit_code"]
    signal = terminal["signal"]
    if (exit_code is None) == (signal is None):
        raise OCIControlProtocolError("terminal status must contain exactly one result")
    if exit_code is not None and (type(exit_code) is not int or not 0 <= exit_code <= 255):
        raise OCIControlProtocolError("terminal exit code is invalid")
    if signal is not None and (type(signal) is not int or not 1 <= signal <= 64):
        raise OCIControlProtocolError("terminal signal is invalid")
    return {"exit_code": exit_code, "signal": signal}


def _freeze_object(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {key: _freeze_object(item) if isinstance(item, Mapping) else item for key, item in value.items()}
    )


def _plain_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _plain_object(item) if isinstance(item, Mapping) else item for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class OCIControlBinding:
    """Path-free identity known to the host before the guest boots."""

    run_id: str
    domain_core_digest: str
    stage1_artifact_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _canonical_uuid(self.run_id, "run ID"))
        object.__setattr__(self, "domain_core_digest", _digest(self.domain_core_digest, "domain-core digest"))
        object.__setattr__(
            self,
            "stage1_artifact_digest",
            _digest(self.stage1_artifact_digest, "stage-1 artifact digest"),
        )


@dataclass(frozen=True, slots=True)
class OCIControlMessage:
    """One exact message with kind-specific request or guest-event fields."""

    kind: str
    binding: OCIControlBinding
    host_nonce: str
    payload: Mapping[str, Any]
    request_id: int | None = None
    sequence: int | None = None
    boot_generation: str | None = None
    reply_to: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise OCIControlProtocolError("lifecycle message kind is invalid")
        if not isinstance(self.binding, OCIControlBinding):
            raise OCIControlProtocolError("lifecycle message binding is invalid")
        if not isinstance(self.host_nonce, str) or _NONCE_RE.fullmatch(self.host_nonce) is None:
            raise OCIControlProtocolError("host nonce is invalid")
        payload = dict(self.payload) if isinstance(self.payload, Mapping) else None
        if payload is None:
            raise OCIControlProtocolError("lifecycle message payload is invalid")
        if self.kind == "HELLO":
            _exact_object(payload, set(), "HELLO payload")
            _counter(self.request_id, "request ID")
            if any(value is not None for value in (self.sequence, self.boot_generation, self.reply_to)):
                raise OCIControlProtocolError("HELLO fields are invalid")
        elif self.kind == "STOP":
            _exact_object(payload, {"signal"}, "STOP payload")
            if payload["signal"] != 15:
                raise OCIControlProtocolError("STOP signal must be SIGTERM")
            _counter(self.request_id, "request ID")
            _canonical_uuid(self.boot_generation, "boot generation")
            if self.sequence is not None or self.reply_to is not None:
                raise OCIControlProtocolError("STOP fields are invalid")
        else:
            _counter(self.sequence, "sequence")
            _canonical_uuid(self.boot_generation, "boot generation")
            if self.request_id is not None:
                raise OCIControlProtocolError(f"{self.kind} fields are invalid")
            if self.kind in {"READY", "SNAPSHOT"}:
                _counter(self.reply_to, "reply-to request ID")
            elif self.reply_to is not None:
                _counter(self.reply_to, "reply-to request ID")
            if self.kind == "READY":
                _exact_object(payload, set(), "READY payload")
            elif self.kind == "TERMINAL":
                _exact_object(payload, {"terminal"}, "TERMINAL payload")
                payload["terminal"] = _terminal(payload["terminal"])
            else:
                state = payload.get("state")
                _exact_object(payload, {"state", "stop_request_id", "terminal"}, "SNAPSHOT payload")
                stop_request_id = payload["stop_request_id"]
                if state == "ready" and payload["terminal"] is None and stop_request_id is None:
                    pass
                elif state == "terminal":
                    payload["terminal"] = _terminal(payload["terminal"])
                    if stop_request_id is not None:
                        _counter(stop_request_id, "snapshot STOP request ID")
                elif state == "stopping" and payload["terminal"] is None:
                    _counter(stop_request_id, "snapshot STOP request ID")
                else:
                    raise OCIControlProtocolError("SNAPSHOT state is invalid")
        object.__setattr__(self, "payload", _freeze_object(payload))

    def to_dict(self) -> dict[str, Any]:
        value = {
            "domain_core_digest": self.binding.domain_core_digest,
            "host_nonce": self.host_nonce,
            "kind": self.kind,
            "payload": _plain_object(self.payload),
            "run_id": self.binding.run_id,
            "schema": OCI_CONTROL_PROTOCOL,
            "stage1_artifact_digest": self.binding.stage1_artifact_digest,
        }
        if self.kind in {"HELLO", "STOP"}:
            value["request_id"] = self.request_id
        else:
            value.update(
                boot_generation=self.boot_generation,
                reply_to=self.reply_to,
                sequence=self.sequence,
            )
        if self.kind == "STOP":
            value["boot_generation"] = self.boot_generation
        return value

    @classmethod
    def from_dict(cls, value: Any) -> OCIControlMessage:
        if not isinstance(value, dict):
            raise OCIControlProtocolError("lifecycle message fields are invalid")
        kind = value.get("kind")
        if kind not in _KINDS:
            raise OCIControlProtocolError("lifecycle message kind is invalid")
        fields = _COMMON_FIELDS | {"payload"}
        if kind == "HELLO":
            fields |= {"request_id"}
        elif kind == "STOP":
            fields |= {"boot_generation", "request_id"}
        else:
            fields |= {"boot_generation", "reply_to", "sequence"}
        data = _exact_object(value, fields, "lifecycle message")
        if data["schema"] != OCI_CONTROL_PROTOCOL:
            raise OCIControlProtocolError("lifecycle protocol schema is unsupported")
        return cls(
            kind=kind,
            binding=OCIControlBinding(
                run_id=data["run_id"],
                domain_core_digest=data["domain_core_digest"],
                stage1_artifact_digest=data["stage1_artifact_digest"],
            ),
            host_nonce=data["host_nonce"],
            payload=data["payload"],
            request_id=data.get("request_id"),
            sequence=data.get("sequence"),
            boot_generation=data.get("boot_generation"),
            reply_to=data.get("reply_to"),
        )


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OCIControlProtocolError("lifecycle message contains a duplicate key")
        result[key] = value
    return result


def decode_payload(payload: bytes) -> OCIControlMessage:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_PAYLOAD_BYTES:
        raise OCIControlProtocolError("lifecycle payload size is invalid")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_pairs)
    except OCIControlProtocolError:
        raise
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise OCIControlProtocolError("lifecycle payload JSON is invalid") from None
    message = OCIControlMessage.from_dict(value)
    try:
        encoded = canonical_json_bytes(message.to_dict())
    except ArtifactValidationError:
        raise OCIControlProtocolError("lifecycle payload is not canonical JSON") from None
    if encoded != payload:
        raise OCIControlProtocolError("lifecycle payload must be canonical UTF-8 JSON")
    return message


def encode_frame(message: OCIControlMessage) -> bytes:
    if not isinstance(message, OCIControlMessage):
        raise OCIControlProtocolError("lifecycle message is invalid")
    try:
        payload = canonical_json_bytes(message.to_dict())
    except ArtifactValidationError:
        raise OCIControlProtocolError("lifecycle message is not canonical JSON") from None
    if not payload or len(payload) > _MAX_PAYLOAD_BYTES:
        raise OCIControlProtocolError("lifecycle frame exceeds 64 KiB")
    return struct.pack(">I", len(payload)) + payload


def decode_frame(frame: bytes) -> OCIControlMessage:
    if not isinstance(frame, bytes) or len(frame) < _FRAME_HEADER_BYTES:
        raise OCIControlProtocolError("lifecycle frame is truncated")
    length = struct.unpack(">I", frame[:_FRAME_HEADER_BYTES])[0]
    if length == 0 or length > _MAX_PAYLOAD_BYTES:
        raise OCIControlProtocolError("lifecycle frame length is invalid")
    if len(frame) != _FRAME_HEADER_BYTES + length:
        raise OCIControlProtocolError("lifecycle frame is truncated or contains trailing bytes")
    return decode_payload(frame[_FRAME_HEADER_BYTES:])


class OCIControlFrameDecoder:
    """Incremental decoder with a bounded buffer and explicit EOF check."""

    def __init__(self) -> None:
        self._header = bytearray()
        self._payload = bytearray()
        self._expected: int | None = None

    @property
    def buffered_bytes(self) -> int:
        """Bytes retained for the current incomplete frame."""

        return len(self._header) + len(self._payload)

    def feed(self, data: bytes) -> tuple[OCIControlMessage, ...]:
        if not isinstance(data, bytes):
            raise OCIControlProtocolError("lifecycle frame chunk must be bytes")
        view = memoryview(data)
        cursor = 0
        messages: list[OCIControlMessage] = []
        while cursor < len(view):
            if self._expected is None:
                header_bytes = min(_FRAME_HEADER_BYTES - len(self._header), len(view) - cursor)
                self._header.extend(view[cursor : cursor + header_bytes])
                cursor += header_bytes
                if len(self._header) < _FRAME_HEADER_BYTES:
                    continue
                self._expected = struct.unpack(">I", self._header)[0]
                self._header.clear()
                if self._expected == 0 or self._expected > _MAX_PAYLOAD_BYTES:
                    self._expected = None
                    raise OCIControlProtocolError("lifecycle frame length is invalid")
            payload_bytes = min(self._expected - len(self._payload), len(view) - cursor)
            self._payload.extend(view[cursor : cursor + payload_bytes])
            cursor += payload_bytes
            if len(self._payload) < self._expected:
                continue
            payload = bytes(self._payload)
            self._payload.clear()
            self._expected = None
            messages.append(decode_payload(payload))
        return tuple(messages)

    def finish(self) -> None:
        if self._expected is not None or self._header or self._payload:
            raise OCIControlProtocolError("lifecycle frame stream is truncated")


class HostOCIControlSession:
    """Host-side nonce, binding, reconnect, and replay state machine."""

    def __init__(
        self,
        binding: OCIControlBinding,
        *,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(binding, OCIControlBinding):
            raise OCIControlProtocolError("session binding is invalid")
        self.binding = binding
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))
        self._used_nonces: set[str] = set()
        self._nonce: str | None = None
        self._sent_request_id = 0
        self._received_sequence = 0
        self._boot_generation: str | None = None
        self._pending_request_id: int | None = None
        self._stop_request_id: int | None = None
        self._terminal: Mapping[str, Any] | None = None
        self._terminal_stop_request_id: int | None = None
        self._reconnecting = False
        self._reconnect_from: str | None = None
        self._initial_hello_retry = False
        self.state = "new"

    @property
    def host_nonce(self) -> str | None:
        return self._nonce

    @property
    def boot_generation(self) -> str | None:
        return self._boot_generation

    @property
    def terminal_stop_request_id(self) -> int | None:
        """Original STOP ID for a stop-caused terminal outcome, otherwise null."""

        return self._terminal_stop_request_id

    def _next_request(self, kind: str, payload: Mapping[str, Any]) -> OCIControlMessage:
        if self._nonce is None:
            raise OCIControlProtocolError("session has no active nonce")
        self._sent_request_id += 1
        self._pending_request_id = self._sent_request_id
        return OCIControlMessage(
            kind=kind,
            binding=self.binding,
            host_nonce=self._nonce,
            payload=payload,
            request_id=self._sent_request_id,
            boot_generation=self._boot_generation if kind == "STOP" else None,
        )

    def hello(self, *, reconnect: bool = False) -> OCIControlMessage:
        if reconnect:
            if self.state == "hello-sent":
                # A lost HELLO response rotates the nonce and request ID but
                # preserves whether this was the initial handshake or a
                # reconnect from an established lifecycle state.
                if not self._reconnecting:
                    self._initial_hello_retry = True
            elif self.state in {"ready", "stop-sent", "terminal"}:
                self._reconnecting = True
                self._reconnect_from = self.state
            else:
                raise OCIControlProtocolError("session is not reconnectable")
        elif self.state != "new":
            raise OCIControlProtocolError("session HELLO was already sent")
        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None or nonce in self._used_nonces:
            raise OCIControlProtocolError("nonce factory did not produce a fresh canonical nonce")
        self._used_nonces.add(nonce)
        self._nonce = nonce
        if not reconnect:
            self._reconnecting = False
            self._reconnect_from = None
            self._initial_hello_retry = False
        self.state = "hello-sent"
        return self._next_request("HELLO", {})

    def stop(self) -> OCIControlMessage:
        if self.state != "ready" or self._reconnecting:
            raise OCIControlProtocolError("STOP requires a current ready snapshot")
        self.state = "stop-sent"
        if self._stop_request_id is None:
            message = self._next_request("STOP", {"signal": 15})
            self._stop_request_id = message.request_id
        else:
            if self._nonce is None or self._boot_generation is None:
                raise OCIControlProtocolError("STOP retransmission state is invalid")
            message = OCIControlMessage(
                kind="STOP",
                binding=self.binding,
                host_nonce=self._nonce,
                payload={"signal": 15},
                request_id=self._stop_request_id,
                boot_generation=self._boot_generation,
            )
        return message

    def accept(self, message: OCIControlMessage) -> None:
        if not isinstance(message, OCIControlMessage):
            raise OCIControlProtocolError("guest lifecycle message is invalid")
        if message.binding != self.binding:
            raise OCIControlProtocolError("guest lifecycle binding is invalid")
        if self._nonce is None or message.host_nonce != self._nonce:
            raise OCIControlProtocolError("guest lifecycle nonce is stale")
        if message.sequence is None or message.sequence <= self._received_sequence:
            raise OCIControlProtocolError("guest lifecycle ordering is stale")
        if self._boot_generation is not None and message.boot_generation != self._boot_generation:
            raise OCIControlProtocolError("guest boot generation is stale")
        if self._reconnecting:
            if message.kind != "SNAPSHOT" or message.reply_to != self._pending_request_id:
                raise OCIControlProtocolError("reconnect requires a current SNAPSHOT")
            snapshot_state = message.payload["state"]
            if snapshot_state == "ready" and self._reconnect_from not in {"ready", "stop-sent"}:
                raise OCIControlProtocolError("reconnect SNAPSHOT regressed lifecycle state")
            if snapshot_state == "stopping":
                if self._reconnect_from != "stop-sent" or message.payload["stop_request_id"] != self._stop_request_id:
                    raise OCIControlProtocolError("reconnect SNAPSHOT STOP request is invalid")
                self.state = "stop-sent"
            elif snapshot_state == "terminal":
                snapshot_stop_request_id = message.payload["stop_request_id"]
                if self._reconnect_from == "stop-sent" and snapshot_stop_request_id not in {
                    None,
                    self._stop_request_id,
                }:
                    raise OCIControlProtocolError("reconnect terminal SNAPSHOT STOP request is invalid")
                if self._reconnect_from == "ready" and snapshot_stop_request_id is not None:
                    raise OCIControlProtocolError("reconnect natural terminal SNAPSHOT has a STOP request")
                if self._reconnect_from == "terminal" and (
                    message.payload["terminal"] != self._terminal
                    or snapshot_stop_request_id != self._terminal_stop_request_id
                ):
                    raise OCIControlProtocolError("reconnect terminal SNAPSHOT changed")
                self._terminal = message.payload["terminal"]
                self._terminal_stop_request_id = snapshot_stop_request_id
                self.state = "terminal"
            else:
                self.state = "ready"
            self._reconnecting = False
            self._reconnect_from = None
        elif self.state == "hello-sent" and message.kind == "READY" and message.reply_to == self._pending_request_id:
            self.state = "ready"
            self._initial_hello_retry = False
        elif (
            self.state == "hello-sent"
            and self._initial_hello_retry
            and message.kind == "SNAPSHOT"
            and message.reply_to == self._pending_request_id
            and message.payload["state"] == "ready"
        ):
            self.state = "ready"
            self._initial_hello_retry = False
        elif self.state == "ready" and message.kind == "TERMINAL" and message.reply_to is None:
            self._terminal = message.payload["terminal"]
            self._terminal_stop_request_id = None
            self.state = "terminal"
        elif self.state == "stop-sent" and message.kind == "TERMINAL" and message.reply_to == self._stop_request_id:
            self._terminal = message.payload["terminal"]
            self._terminal_stop_request_id = self._stop_request_id
            self.state = "terminal"
        else:
            raise OCIControlProtocolError("guest lifecycle transition is invalid")
        if self._boot_generation is None:
            self._boot_generation = message.boot_generation
        self._received_sequence = message.sequence
        self._pending_request_id = None


__all__ = [
    "MAX_OCI_CONTROL_FRAME_BYTES",
    "OCI_CONTROL_CHANNEL_NAME",
    "OCI_CONTROL_PROTOCOL",
    "HostOCIControlSession",
    "OCIControlBinding",
    "OCIControlFrameDecoder",
    "OCIControlMessage",
    "OCIControlProtocolError",
    "decode_frame",
    "decode_payload",
    "encode_frame",
]
