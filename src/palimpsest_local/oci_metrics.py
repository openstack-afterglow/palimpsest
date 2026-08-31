"""Pure run-bound observations for OCI layer materialization.

These fragments deliberately do not extend or replace ``MetricEvent``.  A
runtime adapter may retain a fragment as raw evidence and project the complete
wall time into the canonical run-level ``materialize`` phase.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from .digest import require_digest
from .errors import ArtifactValidationError, InvalidDigestError
from .metrics import ObservedInt, canonical_json_bytes

OCI_MATERIALIZATION_FRAGMENT_SCHEMA = "palimpsest.oci-layer-materialization.v1"
MAX_OCI_MATERIALIZATION_FRAGMENT_BYTES = 64 * 1024
MAX_OCI_MATERIALIZATION_FRAGMENT_DEPTH = 8

OCI_MATERIALIZATION_SUCCESS_CACHE_RESULTS = frozenset({"warm_hit", "cold_miss", "cold_repair"})
OCI_MATERIALIZATION_CACHE_RESULTS = frozenset(
    {*OCI_MATERIALIZATION_SUCCESS_CACHE_RESULTS, "repair_deferred", "unknown"}
)
OCI_MATERIALIZATION_STEP_OUTCOMES = frozenset({"succeeded", "failed", "skipped"})
OCI_MATERIALIZATION_FAILURE_STAGES = frozenset({"stage", "pack", "store", "cleanup"})

_EXECUTED_MISSING_REASONS = frozenset({"collection_failed", "clock_incompatible"})


def _exact_fields(data: Any, expected: set[str], field_name: str) -> dict[str, Any]:
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise ArtifactValidationError(f"{field_name} must be an object")
    if set(data) != expected:
        raise ArtifactValidationError(f"invalid fields in {field_name}")
    return data


def _canonical_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field_name} must be a canonical sha256 digest")
    try:
        normalized = require_digest(value)
    except InvalidDigestError:
        raise ArtifactValidationError(f"{field_name} must be a canonical sha256 digest") from None
    if normalized != value:
        raise ArtifactValidationError(f"{field_name} must use canonical sha256:<lowercase-hex> syntax")
    return value


def _optional_digest(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _canonical_digest(value, field_name)


def _canonical_run_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError("run_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ArtifactValidationError("run_id must be a canonical UUID") from None
    if str(parsed) != value:
        raise ArtifactValidationError("run_id must be a canonical UUID")
    return value


@dataclass(frozen=True, slots=True)
class OCIMaterializationStep:
    """One bounded materialization step with an honest duration."""

    outcome: str
    duration_ns: ObservedInt

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, str) or self.outcome not in OCI_MATERIALIZATION_STEP_OUTCOMES:
            raise ArtifactValidationError("OCI materialization step outcome is invalid")
        if not isinstance(self.duration_ns, ObservedInt):
            raise ArtifactValidationError("OCI materialization step duration is invalid")
        if self.outcome == "skipped":
            if self.duration_ns != ObservedInt.missing("not_executed"):
                raise ArtifactValidationError("a skipped OCI materialization step must be not_executed")
        elif self.duration_ns.value is None and self.duration_ns.missing_reason not in _EXECUTED_MISSING_REASONS:
            raise ArtifactValidationError("an executed OCI materialization step requires an observed duration")

    @classmethod
    def succeeded(cls, duration_ns: int) -> OCIMaterializationStep:
        return cls(outcome="succeeded", duration_ns=ObservedInt.measured(duration_ns))

    @classmethod
    def failed(cls, duration_ns: int | None, *, missing_reason: str = "collection_failed") -> OCIMaterializationStep:
        duration = ObservedInt.measured(duration_ns) if duration_ns is not None else ObservedInt.missing(missing_reason)
        return cls(outcome="failed", duration_ns=duration)

    @classmethod
    def skipped(cls) -> OCIMaterializationStep:
        return cls(outcome="skipped", duration_ns=ObservedInt.missing("not_executed"))

    def to_dict(self) -> dict[str, Any]:
        return {"duration_ns": self.duration_ns.to_dict(), "outcome": self.outcome}

    @classmethod
    def from_dict(cls, data: Any, field_name: str) -> OCIMaterializationStep:
        value = _exact_fields(data, {"duration_ns", "outcome"}, field_name)
        return cls(
            outcome=value["outcome"],
            duration_ns=ObservedInt.from_dict(value["duration_ns"], f"{field_name}.duration_ns"),
        )


@dataclass(frozen=True, slots=True)
class OCILayerMaterializationFragment:
    """Canonical evidence for one run-bound OCI layer occurrence.

    ``store_total`` is a nested wall-clock span: on a cold path it includes
    recipe-lock wait, lookup, producer execution, and durable publication.  It
    must not be added to the stage or pack durations.
    """

    run_id: str
    source_snapshot_binding_digest: str
    source_image_digest: str
    ordinal: int
    key_digest: str
    cache_result: str
    stage: OCIMaterializationStep
    pack: OCIMaterializationStep
    store_total: OCIMaterializationStep
    overall_outcome: str
    failure_stage: str | None
    occurrence_digest: str | None
    record_digest: str | None
    image_digest: str | None
    schema: str = OCI_MATERIALIZATION_FRAGMENT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _canonical_run_id(self.run_id))
        for field_name in (
            "source_snapshot_binding_digest",
            "source_image_digest",
            "key_digest",
        ):
            object.__setattr__(self, field_name, _canonical_digest(getattr(self, field_name), field_name))
        for field_name in ("occurrence_digest", "record_digest", "image_digest"):
            object.__setattr__(self, field_name, _optional_digest(getattr(self, field_name), field_name))
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ArtifactValidationError("ordinal must be a nonnegative integer")
        if self.schema != OCI_MATERIALIZATION_FRAGMENT_SCHEMA:
            raise ArtifactValidationError("OCI materialization fragment schema is unsupported")
        if not isinstance(self.cache_result, str) or self.cache_result not in OCI_MATERIALIZATION_CACHE_RESULTS:
            raise ArtifactValidationError("OCI materialization cache result is invalid")
        if any(not isinstance(step, OCIMaterializationStep) for step in (self.stage, self.pack, self.store_total)):
            raise ArtifactValidationError("OCI materialization fragment steps are invalid")
        if not isinstance(self.overall_outcome, str) or self.overall_outcome not in {"succeeded", "failed"}:
            raise ArtifactValidationError("OCI materialization overall outcome is invalid")
        self._validate_outcome()

    def _validate_outcome(self) -> None:
        receipt_digests = (self.occurrence_digest, self.record_digest, self.image_digest)
        if self.overall_outcome == "succeeded":
            if self.failure_stage is not None or any(value is None for value in receipt_digests):
                raise ArtifactValidationError("successful OCI materialization requires complete receipt evidence")
            if self.store_total.outcome != "succeeded":
                raise ArtifactValidationError("successful OCI materialization requires a successful store span")
            expected = "skipped" if self.cache_result == "warm_hit" else "succeeded"
            if self.cache_result not in OCI_MATERIALIZATION_SUCCESS_CACHE_RESULTS:
                raise ArtifactValidationError("successful OCI materialization has a nonterminal cache result")
            if self.stage.outcome != expected or self.pack.outcome != expected:
                raise ArtifactValidationError("OCI materialization steps do not match the cache result")
            return

        if not isinstance(self.failure_stage, str) or self.failure_stage not in OCI_MATERIALIZATION_FAILURE_STAGES:
            raise ArtifactValidationError("failed OCI materialization requires a stable failure stage")
        if any(value is not None for value in receipt_digests):
            raise ArtifactValidationError("failed OCI materialization cannot contain receipt evidence")
        if self.store_total.outcome != "failed":
            raise ArtifactValidationError("failed OCI materialization requires a failed store span")
        actual_steps = (self.stage.outcome, self.pack.outcome)
        if self.cache_result in {"warm_hit", "unknown"}:
            expected_steps = ("skipped", "skipped")
            allowed_failure_stages = {"store", "cleanup"}
        elif self.cache_result == "repair_deferred":
            expected_steps = ("succeeded", "succeeded")
            allowed_failure_stages = {"store", "cleanup"}
        elif self.failure_stage == "stage":
            expected_steps = ("failed", "skipped")
            allowed_failure_stages = {"stage"}
        elif self.failure_stage == "pack":
            expected_steps = ("succeeded", "failed")
            allowed_failure_stages = {"pack"}
        else:
            expected_steps = ("succeeded", "succeeded")
            allowed_failure_stages = {"store", "cleanup"}
        if self.failure_stage not in allowed_failure_stages or actual_steps != expected_steps:
            raise ArtifactValidationError("OCI materialization failure stage and step outcomes are inconsistent")

    @classmethod
    def succeeded(
        cls,
        *,
        run_id: str,
        source_snapshot_binding_digest: str,
        source_image_digest: str,
        ordinal: int,
        key_digest: str,
        cache_result: str,
        occurrence_digest: str,
        record_digest: str,
        image_digest: str,
        store_duration_ns: int,
        stage: OCIMaterializationStep,
        pack: OCIMaterializationStep,
    ) -> OCILayerMaterializationFragment:
        return cls(
            run_id=run_id,
            source_snapshot_binding_digest=source_snapshot_binding_digest,
            source_image_digest=source_image_digest,
            ordinal=ordinal,
            key_digest=key_digest,
            cache_result=cache_result,
            stage=stage,
            pack=pack,
            store_total=OCIMaterializationStep.succeeded(store_duration_ns),
            overall_outcome="succeeded",
            failure_stage=None,
            occurrence_digest=occurrence_digest,
            record_digest=record_digest,
            image_digest=image_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_result": self.cache_result,
            "failure_stage": self.failure_stage,
            "image_digest": self.image_digest,
            "key_digest": self.key_digest,
            "occurrence_digest": self.occurrence_digest,
            "ordinal": self.ordinal,
            "overall_outcome": self.overall_outcome,
            "pack": self.pack.to_dict(),
            "record_digest": self.record_digest,
            "run_id": self.run_id,
            "schema": self.schema,
            "source_image_digest": self.source_image_digest,
            "source_snapshot_binding_digest": self.source_snapshot_binding_digest,
            "stage": self.stage.to_dict(),
            "store_total": self.store_total.to_dict(),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.to_json_bytes()).hexdigest()}"

    @classmethod
    def from_dict(cls, data: Any) -> OCILayerMaterializationFragment:
        fields = {
            "cache_result",
            "failure_stage",
            "image_digest",
            "key_digest",
            "occurrence_digest",
            "ordinal",
            "overall_outcome",
            "pack",
            "record_digest",
            "run_id",
            "schema",
            "source_image_digest",
            "source_snapshot_binding_digest",
            "stage",
            "store_total",
        }
        value = _exact_fields(data, fields, "OCI materialization fragment")
        return cls(
            run_id=value["run_id"],
            source_snapshot_binding_digest=value["source_snapshot_binding_digest"],
            source_image_digest=value["source_image_digest"],
            ordinal=value["ordinal"],
            key_digest=value["key_digest"],
            cache_result=value["cache_result"],
            stage=OCIMaterializationStep.from_dict(value["stage"], "stage"),
            pack=OCIMaterializationStep.from_dict(value["pack"], "pack"),
            store_total=OCIMaterializationStep.from_dict(value["store_total"], "store_total"),
            overall_outcome=value["overall_outcome"],
            failure_stage=value["failure_stage"],
            occurrence_digest=value["occurrence_digest"],
            record_digest=value["record_digest"],
            image_digest=value["image_digest"],
            schema=value["schema"],
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> OCILayerMaterializationFragment:
        value = _load_strict_json(payload)
        fragment = cls.from_dict(value)
        if payload != fragment.to_json_bytes():
            raise ArtifactValidationError("OCI materialization fragment JSON must be canonical UTF-8")
        return fragment


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ArtifactValidationError("non-finite JSON numbers are forbidden")


def _load_strict_json(payload: bytes) -> Any:
    if not isinstance(payload, bytes):
        raise ArtifactValidationError("OCI materialization fragment JSON must be bytes")
    if len(payload) > MAX_OCI_MATERIALIZATION_FRAGMENT_BYTES:
        raise ArtifactValidationError("OCI materialization fragment JSON exceeds the maximum size")
    invalid = False
    value: Any = None
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except ArtifactValidationError:
        raise
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        invalid = True
    if invalid:
        raise ArtifactValidationError("OCI materialization fragment JSON is invalid") from None
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_OCI_MATERIALIZATION_FRAGMENT_DEPTH:
            raise ArtifactValidationError("OCI materialization fragment JSON exceeds the maximum depth")
        if isinstance(item, dict):
            stack.extend((nested, depth + 1) for nested in item.values())
        elif isinstance(item, list):
            stack.extend((nested, depth + 1) for nested in item)
    return value


__all__ = [
    "MAX_OCI_MATERIALIZATION_FRAGMENT_BYTES",
    "OCI_MATERIALIZATION_CACHE_RESULTS",
    "OCI_MATERIALIZATION_FAILURE_STAGES",
    "OCI_MATERIALIZATION_FRAGMENT_SCHEMA",
    "OCI_MATERIALIZATION_SUCCESS_CACHE_RESULTS",
    "OCI_MATERIALIZATION_STEP_OUTCOMES",
    "OCILayerMaterializationFragment",
    "OCIMaterializationStep",
]
