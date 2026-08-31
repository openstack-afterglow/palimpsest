"""Unit contracts for run-bound OCI materialization evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import FrozenInstanceError, replace

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.metrics import ObservedInt
from palimpsest_local.oci_metrics import (
    MAX_OCI_MATERIALIZATION_FRAGMENT_BYTES,
    OCI_MATERIALIZATION_SUCCESS_CACHE_RESULTS,
    OCILayerMaterializationFragment,
    OCIMaterializationStep,
)
from palimpsest_local.oci_store import MATERIALIZATION_CACHE_RESULTS, DerivedLayerReceipt, MaterializationResult


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _run_id() -> str:
    return str(uuid.UUID("12345678-1234-5678-9234-567812345678"))


def _result(cache_result: str = "cold_miss") -> MaterializationResult:
    return MaterializationResult(
        receipt=DerivedLayerReceipt(
            store_id="oci-store-v1:" + "0" * 64,
            occurrence_digest=_digest("1"),
            record_digest=_digest("2"),
            key_digest=_digest("3"),
            source_snapshot_binding_digest=_digest("4"),
            source_image_digest=_digest("5"),
            ordinal=2,
            image_digest=_digest("6"),
            image_size=1024,
        ),
        cache_result=cache_result,
    )


def _cold_fragment() -> OCILayerMaterializationFragment:
    result = _result()
    receipt = result.receipt
    return OCILayerMaterializationFragment.succeeded(
        run_id=_run_id(),
        source_snapshot_binding_digest=receipt.source_snapshot_binding_digest,
        source_image_digest=receipt.source_image_digest,
        ordinal=receipt.ordinal,
        key_digest=receipt.key_digest,
        cache_result=result.cache_result,
        occurrence_digest=receipt.occurrence_digest,
        record_digest=receipt.record_digest,
        image_digest=receipt.image_digest,
        store_duration_ns=300,
        stage=OCIMaterializationStep.succeeded(100),
        pack=OCIMaterializationStep.succeeded(150),
    )


def test_cold_fragment_is_frozen_canonical_and_round_trips() -> None:
    fragment = _cold_fragment()
    payload = fragment.to_json_bytes()

    assert OCILayerMaterializationFragment.from_json_bytes(payload) == fragment
    assert fragment.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert payload == json.dumps(fragment.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    assert fragment.store_total.duration_ns.value == 300
    assert fragment.stage.duration_ns.value == 100
    assert fragment.pack.duration_ns.value == 150
    with pytest.raises(FrozenInstanceError):
        fragment.cache_result = "warm_hit"  # type: ignore[misc]


def test_fragment_and_store_success_cache_results_cannot_drift() -> None:
    assert OCI_MATERIALIZATION_SUCCESS_CACHE_RESULTS == MATERIALIZATION_CACHE_RESULTS


def test_warm_fragment_requires_skipped_stage_and_pack() -> None:
    result = _result("warm_hit")
    receipt = result.receipt
    fragment = OCILayerMaterializationFragment.succeeded(
        run_id=_run_id(),
        source_snapshot_binding_digest=receipt.source_snapshot_binding_digest,
        source_image_digest=receipt.source_image_digest,
        ordinal=receipt.ordinal,
        key_digest=receipt.key_digest,
        cache_result=result.cache_result,
        occurrence_digest=receipt.occurrence_digest,
        record_digest=receipt.record_digest,
        image_digest=receipt.image_digest,
        store_duration_ns=25,
        stage=OCIMaterializationStep.skipped(),
        pack=OCIMaterializationStep.skipped(),
    )

    assert fragment.cache_result == "warm_hit"
    assert fragment.stage.duration_ns == ObservedInt.missing("not_executed")
    assert fragment.pack.duration_ns == ObservedInt.missing("not_executed")

    with pytest.raises(ArtifactValidationError, match="steps do not match"):
        replace(fragment, stage=OCIMaterializationStep.succeeded(1))


def test_fragment_store_total_is_an_independent_nested_span() -> None:
    result = _result()
    receipt = result.receipt
    fragment = OCILayerMaterializationFragment.succeeded(
        run_id=_run_id(),
        source_snapshot_binding_digest=receipt.source_snapshot_binding_digest,
        source_image_digest=receipt.source_image_digest,
        ordinal=receipt.ordinal,
        key_digest=receipt.key_digest,
        cache_result=result.cache_result,
        occurrence_digest=receipt.occurrence_digest,
        record_digest=receipt.record_digest,
        image_digest=receipt.image_digest,
        store_duration_ns=10,
        stage=OCIMaterializationStep.succeeded(100),
        pack=OCIMaterializationStep.succeeded(200),
    )

    assert fragment.store_total.duration_ns.value == 10
    assert fragment.stage.duration_ns.value + fragment.pack.duration_ns.value == 300


def test_failed_fragment_has_no_receipt_and_preserves_stable_stage_only() -> None:
    fragment = OCILayerMaterializationFragment(
        run_id=_run_id(),
        source_snapshot_binding_digest=_digest("4"),
        source_image_digest=_digest("5"),
        ordinal=2,
        key_digest=_digest("3"),
        cache_result="cold_miss",
        stage=OCIMaterializationStep.succeeded(100),
        pack=OCIMaterializationStep.failed(None),
        store_total=OCIMaterializationStep.failed(250),
        overall_outcome="failed",
        failure_stage="pack",
        occurrence_digest=None,
        record_digest=None,
        image_digest=None,
    )

    assert OCILayerMaterializationFragment.from_json_bytes(fragment.to_json_bytes()) == fragment
    assert fragment.pack.duration_ns == ObservedInt.missing("collection_failed")

    with pytest.raises(ArtifactValidationError, match="cannot contain receipt"):
        replace(fragment, image_digest=_digest("6"))


def _step(outcome: str) -> OCIMaterializationStep:
    if outcome == "skipped":
        return OCIMaterializationStep.skipped()
    if outcome == "succeeded":
        return OCIMaterializationStep.succeeded(1)
    return OCIMaterializationStep.failed(1)


def test_failed_fragment_transition_matrix_is_closed() -> None:
    allowed = {
        ("warm_hit", "store", "skipped", "skipped"),
        ("warm_hit", "cleanup", "skipped", "skipped"),
        ("unknown", "store", "skipped", "skipped"),
        ("unknown", "cleanup", "skipped", "skipped"),
        ("repair_deferred", "store", "succeeded", "succeeded"),
        ("repair_deferred", "cleanup", "succeeded", "succeeded"),
        ("cold_miss", "stage", "failed", "skipped"),
        ("cold_miss", "pack", "succeeded", "failed"),
        ("cold_miss", "store", "succeeded", "succeeded"),
        ("cold_miss", "cleanup", "succeeded", "succeeded"),
        ("cold_repair", "stage", "failed", "skipped"),
        ("cold_repair", "pack", "succeeded", "failed"),
        ("cold_repair", "store", "succeeded", "succeeded"),
        ("cold_repair", "cleanup", "succeeded", "succeeded"),
    }
    cache_results = {item[0] for item in allowed}
    failure_stages = {item[1] for item in allowed}
    step_outcomes = {"succeeded", "failed", "skipped"}

    for cache_result in cache_results:
        for failure_stage in failure_stages:
            for stage_outcome in step_outcomes:
                for pack_outcome in step_outcomes:
                    state = (cache_result, failure_stage, stage_outcome, pack_outcome)
                    arguments = {
                        "run_id": _run_id(),
                        "source_snapshot_binding_digest": _digest("4"),
                        "source_image_digest": _digest("5"),
                        "ordinal": 2,
                        "key_digest": _digest("3"),
                        "cache_result": cache_result,
                        "stage": _step(stage_outcome),
                        "pack": _step(pack_outcome),
                        "store_total": OCIMaterializationStep.failed(1),
                        "overall_outcome": "failed",
                        "failure_stage": failure_stage,
                        "occurrence_digest": None,
                        "record_digest": None,
                        "image_digest": None,
                    }
                    if state in allowed:
                        assert OCILayerMaterializationFragment(**arguments).failure_stage == failure_stage
                    else:
                        with pytest.raises(ArtifactValidationError, match="inconsistent"):
                            OCILayerMaterializationFragment(**arguments)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "NOT-A-UUID"),
        ("source_image_digest", "sha256:ABC"),
        ("ordinal", True),
        ("cache_result", "/Users/example/.docker/config.json"),
        ("failure_stage", "/tmp/secret"),
    ],
)
def test_fragment_rejects_noncanonical_or_path_bearing_values(field: str, value: object) -> None:
    fragment = _cold_fragment()

    with pytest.raises(ArtifactValidationError):
        replace(fragment, **{field: value})


@pytest.mark.parametrize("field", ["cache_result", "overall_outcome"])
def test_fragment_rejects_unhashable_enum_values(field: str) -> None:
    with pytest.raises(ArtifactValidationError):
        replace(_cold_fragment(), **{field: []})

    value = _cold_fragment().to_dict()
    value[field] = []
    with pytest.raises(ArtifactValidationError):
        OCILayerMaterializationFragment.from_json_bytes(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )


def test_failed_fragment_rejects_unhashable_failure_stage_and_step_outcome() -> None:
    fragment = replace(
        _cold_fragment(),
        overall_outcome="failed",
        failure_stage="store",
        occurrence_digest=None,
        record_digest=None,
        image_digest=None,
        store_total=OCIMaterializationStep.failed(1),
    )

    with pytest.raises(ArtifactValidationError):
        replace(fragment, failure_stage=[])
    with pytest.raises(ArtifactValidationError):
        OCIMaterializationStep([], ObservedInt.measured(1))  # type: ignore[arg-type]


def test_fragment_parser_rejects_unknown_duplicate_nonfinite_and_noncanonical_json() -> None:
    fragment = _cold_fragment()
    value = fragment.to_dict()
    value["unknown"] = "field"
    with pytest.raises(ArtifactValidationError, match="invalid fields"):
        OCILayerMaterializationFragment.from_json_bytes(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )

    payload = fragment.to_json_bytes().replace(b'"ordinal":2', b'"ordinal":2,"ordinal":2')
    with pytest.raises(ArtifactValidationError, match="duplicate"):
        OCILayerMaterializationFragment.from_json_bytes(payload)

    payload = fragment.to_json_bytes().replace(b'"ordinal":2', b'"ordinal":NaN')
    with pytest.raises(ArtifactValidationError, match="non-finite"):
        OCILayerMaterializationFragment.from_json_bytes(payload)

    with pytest.raises(ArtifactValidationError, match="canonical UTF-8"):
        OCILayerMaterializationFragment.from_json_bytes(fragment.to_json_bytes() + b"\n")


def test_fragment_parser_enforces_size_and_depth_limits() -> None:
    with pytest.raises(ArtifactValidationError, match="maximum size"):
        OCILayerMaterializationFragment.from_json_bytes(b" " * (MAX_OCI_MATERIALIZATION_FRAGMENT_BYTES + 1))

    nested: object = None
    for _index in range(10):
        nested = {"x": nested}
    with pytest.raises(ArtifactValidationError, match="maximum depth"):
        OCILayerMaterializationFragment.from_json_bytes(json.dumps(nested).encode())


@pytest.mark.parametrize("payload", [b"not-json", b"\xff"])
def test_fragment_parser_hides_low_level_parse_context(payload: bytes) -> None:
    with pytest.raises(ArtifactValidationError, match="JSON is invalid") as captured:
        OCILayerMaterializationFragment.from_json_bytes(payload)

    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None


def test_executed_step_rejects_fabricated_or_inapplicable_timing() -> None:
    with pytest.raises(ArtifactValidationError, match="requires an observed duration"):
        OCIMaterializationStep("succeeded", ObservedInt.missing("not_executed"))
    with pytest.raises(ArtifactValidationError, match="must be not_executed"):
        OCIMaterializationStep("skipped", ObservedInt.measured(0))
