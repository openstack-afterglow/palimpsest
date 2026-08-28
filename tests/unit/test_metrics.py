"""Unit contracts for the side-effect-free baseline metric schema."""

from __future__ import annotations

import ast
import hashlib
import json
import traceback
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import palimpsest_local.metrics as metrics_module
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.metrics import (
    CACHE_HIT_LEVELS,
    CACHE_TEMPERATURES,
    FAILURE_CATEGORIES,
    INT64_MAX,
    METRIC_SCHEMA_VERSION,
    MISSING_REASONS,
    PHASES,
    ClockDisclosure,
    DistributionObservation,
    EnvironmentDisclosure,
    MetricEvent,
    ObservedInt,
    ObservedText,
    Outcome,
    PhaseObservation,
    RawEvidence,
    ResourceObservation,
    canonical_json_bytes,
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


def measured(value: int) -> ObservedInt:
    return ObservedInt.measured(value)


def text(value: str) -> ObservedText:
    return ObservedText.measured(value)


def sample_environment() -> EnvironmentDisclosure:
    return EnvironmentDisclosure(
        system="palimpsest",
        system_version=text("0.1.0"),
        adapter="palimpsest-local",
        adapter_version=text("1"),
        provider=text("local-kvm"),
        host_os=text("linux-6.8"),
        architecture=text("x86_64"),
        virtualization=text("kvm"),
        cpu_count=measured(8),
        memory_bytes=measured(16 * 1024**3),
    )


def sample_phases() -> tuple[PhaseObservation, ...]:
    return tuple(
        PhaseObservation(
            phase=phase,
            outcome="succeeded",
            started_at_ns=measured(index * 1_000),
            duration_ns=measured(500),
        )
        for index, phase in enumerate(PHASES)
    )


def sample_resources(*, missing: bool = False) -> ResourceObservation:
    observation = ObservedInt.missing("not_supported") if missing else measured(100)
    return ResourceObservation(
        scope="vm",
        boundary="whole-run",
        cpu_time_ns=measured(4_000),
        peak_memory_bytes=measured(8_000),
        disk_read_bytes=measured(500),
        disk_write_bytes=observation,
        network_receive_bytes=measured(300),
        network_transmit_bytes=measured(200),
    )


def sample_event(*, evidence: tuple[RawEvidence, ...] | None = None) -> MetricEvent:
    return MetricEvent(
        run_id="run-20260828-001",
        environment=sample_environment(),
        clock=ClockDisclosure(
            source="monotonic",
            resolution_ns=measured(1),
            wall_started_unix_ns=measured(1_777_000_000_000_000_000),
        ),
        phases=sample_phases(),
        distribution=DistributionObservation(
            executed=True,
            cache_temperature="cold",
            cache_hit_level="miss",
            placement="region-local",
            distribution_mode="registry-pull",
            materialization_mode="eager",
            source_region=text("kr-seoul-1"),
            destination_region=text("kr-seoul-1"),
            registry_bytes_received=measured(1_000),
            cross_region_bytes_received=measured(0),
            unique_storage_bytes=measured(800),
            writable_upper_growth_bytes=measured(0),
        ),
        resources=sample_resources(),
        outcome=Outcome(status="succeeded", failure_category=None),
        evidence=evidence
        if evidence is not None
        else (RawEvidence("artifact://baseline/run-001/raw.json", digest("a")),),
    )


def decode(payload: bytes) -> dict[str, object]:
    value = json.loads(payload)
    assert isinstance(value, dict)
    return value


def encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def test_metric_event_has_stable_canonical_bytes_digest_and_round_trip():
    first = sample_event()
    second = sample_event()

    assert first == second
    assert first.to_json_bytes() == second.to_json_bytes()
    assert first.to_json_bytes() == canonical_json_bytes(first.to_dict())
    assert first.digest == "sha256:" + hashlib.sha256(first.to_json_bytes()).hexdigest()
    assert MetricEvent.from_json_bytes(first.to_json_bytes()) == first
    assert b" " not in first.to_json_bytes()
    assert decode(first.to_json_bytes())["schema_version"] == METRIC_SCHEMA_VERSION


def test_contracts_are_frozen_and_nested_collections_are_immutable():
    event = sample_event()

    with pytest.raises(FrozenInstanceError):
        event.run_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        event.environment.system = "changed"  # type: ignore[misc]
    assert isinstance(event.phases, tuple)
    assert isinstance(event.evidence, tuple)


def test_all_required_phases_have_one_fixed_order_and_explicit_units():
    assert PHASES == (
        "resolve",
        "locate",
        "fetch",
        "verify",
        "materialize",
        "vm_boot",
        "mount",
        "initialize",
        "model_load",
        "application_ready",
    )

    event = sample_event()
    payload = event.to_dict()
    assert [item["phase"] for item in payload["phases"]] == list(PHASES)
    assert set(payload["phases"][0]) == {"phase", "outcome", "started_at_ns", "duration_ns"}


@pytest.mark.parametrize("bad_phases", [(), sample_phases()[:-1], tuple(reversed(sample_phases()))])
def test_event_rejects_missing_or_reordered_phases(bad_phases: tuple[PhaseObservation, ...]):
    with pytest.raises(ArtifactValidationError, match="canonical order"):
        replace(sample_event(), phases=bad_phases)


def test_event_rejects_phase_timestamps_that_move_backwards():
    phases = list(sample_phases())
    phases[4] = replace(phases[4], started_at_ns=measured(1))

    with pytest.raises(ArtifactValidationError, match="nondecreasing"):
        replace(sample_event(), phases=tuple(phases))


@pytest.mark.parametrize("outcome", ["skipped", "not_applicable"])
def test_nonexecuted_phase_cannot_fabricate_timing(outcome: str):
    with pytest.raises(ArtifactValidationError, match="cannot claim timing"):
        PhaseObservation(
            phase="model_load",
            outcome=outcome,
            started_at_ns=measured(1),
            duration_ns=ObservedInt.missing("not_applicable"),
        )

    phase = PhaseObservation(
        phase="model_load",
        outcome=outcome,
        started_at_ns=ObservedInt.missing("not_executed" if outcome == "skipped" else "not_applicable"),
        duration_ns=ObservedInt.missing("not_executed" if outcome == "skipped" else "not_applicable"),
    )
    assert phase.started_at_ns.value is None


@pytest.mark.parametrize("reason", sorted(MISSING_REASONS))
def test_missing_observations_require_a_supported_explicit_reason(reason: str):
    integer = ObservedInt.missing(reason)
    string = ObservedText.missing(reason)

    assert integer.to_dict() == {"missing_reason": reason, "value": None}
    assert string.to_dict() == {"missing_reason": reason, "value": None}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value": None, "missing_reason": None},
        {"value": None, "missing_reason": "unknown"},
        {"value": 1, "missing_reason": "not_reported"},
        {"value": True, "missing_reason": None},
        {"value": 1.0, "missing_reason": None},
        {"value": -1, "missing_reason": None},
        {"value": INT64_MAX + 1, "missing_reason": None},
    ],
)
def test_integer_observation_rejects_ambiguous_inexact_or_out_of_range_values(kwargs: dict[str, object]):
    with pytest.raises(ArtifactValidationError):
        ObservedInt(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value": None, "missing_reason": None},
        {"value": None, "missing_reason": "unknown"},
        {"value": "measured", "missing_reason": "not_reported"},
        {"value": "", "missing_reason": None},
        {"value": " surrounding ", "missing_reason": None},
        {"value": 1, "missing_reason": None},
    ],
)
def test_text_observation_rejects_ambiguous_or_invalid_values(kwargs: dict[str, object]):
    with pytest.raises(ArtifactValidationError):
        ObservedText(**kwargs)  # type: ignore[arg-type]


def test_environment_can_honestly_disclose_unavailable_values_without_sentinels():
    missing = ObservedText.missing("not_reported")
    environment = replace(
        sample_environment(),
        provider=missing,
        virtualization=ObservedText.missing("not_supported"),
        cpu_count=ObservedInt.missing("permission_denied"),
    )

    assert environment.provider.value is None
    assert environment.cpu_count.missing_reason == "permission_denied"


@pytest.mark.parametrize("field_name", ["cpu_count", "memory_bytes"])
def test_environment_rejects_impossible_zero_capacity(field_name: str):
    with pytest.raises(ArtifactValidationError, match="greater than zero"):
        replace(sample_environment(), **{field_name: measured(0)})


@pytest.mark.parametrize("cache_hit_level", sorted(CACHE_HIT_LEVELS))
def test_every_required_distribution_cache_hit_level_round_trips(cache_hit_level: str):
    temperature = "cold" if cache_hit_level == "miss" else "warm"
    profile_changes: dict[str, object] = {}
    if cache_hit_level == "node":
        profile_changes["placement"] = "node-local"
    elif cache_hit_level == "prefetched":
        profile_changes["distribution_mode"] = "prefetched"
        profile_changes["registry_bytes_received"] = measured(0)
    elif cache_hit_level == "retained-root":
        profile_changes = {
            "placement": "retained-root",
            "distribution_mode": "retained-root",
            "materialization_mode": "prebuilt-root",
            "registry_bytes_received": measured(0),
        }
    distribution = replace(
        sample_event().distribution,
        cache_temperature=temperature,
        cache_hit_level=cache_hit_level,
        **profile_changes,
    )
    event = replace(sample_event(), distribution=distribution)

    assert MetricEvent.from_json_bytes(event.to_json_bytes()).distribution.cache_hit_level == cache_hit_level


def test_distribution_rejects_unknown_cache_dimension_and_discloses_regions_and_bytes():
    distribution = sample_event().distribution
    assert distribution.source_region.value == "kr-seoul-1"
    assert distribution.destination_region.value == "kr-seoul-1"
    assert distribution.registry_bytes_received.value == 1_000
    assert distribution.unique_storage_bytes.value == 800

    with pytest.raises(ArtifactValidationError, match="cache_temperature"):
        replace(distribution, cache_temperature="hot")
    for temperature in CACHE_TEMPERATURES:
        contradictory_level = "node" if temperature == "cold" else "miss"
        with pytest.raises(ArtifactValidationError, match="contradict"):
            replace(distribution, cache_temperature=temperature, cache_hit_level=contradictory_level)


def test_resource_observations_keep_missing_values_honest():
    resources = sample_resources(missing=True)

    assert resources.disk_write_bytes.value is None
    assert resources.disk_write_bytes.missing_reason == "not_supported"
    assert set(resources.to_dict()) == {
        "boundary",
        "cpu_time_ns",
        "peak_memory_bytes",
        "scope",
        "disk_read_bytes",
        "disk_write_bytes",
        "network_receive_bytes",
        "network_transmit_bytes",
    }


def test_clock_discloses_source_origin_resolution_and_rejects_invalid_values():
    clock = sample_event().clock
    assert clock.source == "monotonic"
    assert clock.wall_started_unix_ns.value is not None
    assert clock.resolution_ns.value == 1

    with pytest.raises(ArtifactValidationError, match="source"):
        replace(clock, source="time.time")
    with pytest.raises(ArtifactValidationError, match="greater than zero"):
        replace(clock, resolution_ns=measured(0))


@pytest.mark.parametrize("failure_category", sorted(FAILURE_CATEGORIES - {"cancelled"}))
def test_failed_outcome_uses_stable_failure_taxonomy(failure_category: str):
    assert Outcome("failed", failure_category).failure_category == failure_category


@pytest.mark.parametrize(
    ("status", "category"),
    [
        ("succeeded", "internal"),
        ("failed", None),
        ("failed", "cancelled"),
        ("cancelled", None),
        ("cancelled", "timeout"),
        ("unknown", None),
    ],
)
def test_outcome_rejects_contradictory_or_unknown_failure_claims(status: str, category: str | None):
    with pytest.raises(ArtifactValidationError):
        Outcome(status, category)


def test_raw_evidence_requires_absolute_uri_and_canonical_digest():
    evidence = RawEvidence("s3://evidence-bucket/runs/1.json", digest("b"))
    assert evidence.uri.startswith("s3://")

    for uri in ("relative/path.json", " https://example.test/a ", "https://example.test/bad path"):
        with pytest.raises(ArtifactValidationError, match="uri"):
            RawEvidence(uri, digest("b"))
    for invalid_digest in ("b" * 64, "sha256:" + "B" * 64, "sha512:" + "b" * 64):
        with pytest.raises(ArtifactValidationError, match="digest"):
            RawEvidence("artifact://run/raw", invalid_digest)


def test_evidence_order_is_canonical_and_duplicates_are_rejected():
    evidence_a = RawEvidence("artifact://run/a", digest("a"))
    evidence_b = RawEvidence("artifact://run/b", digest("b"))
    first = sample_event(evidence=(evidence_b, evidence_a))
    second = sample_event(evidence=(evidence_a, evidence_b))

    assert first.evidence == (evidence_a, evidence_b)
    assert first.to_json_bytes() == second.to_json_bytes()
    assert first.digest == second.digest
    with pytest.raises(ArtifactValidationError, match="URI"):
        sample_event(evidence=(evidence_a, evidence_a))
    with pytest.raises(ArtifactValidationError, match="nonempty"):
        sample_event(evidence=())


def test_unknown_missing_and_non_string_fields_are_rejected_at_every_boundary():
    payload = sample_event().to_dict()
    invalid_values = [
        {**payload, "unknown": True},
        {key: value for key, value in payload.items() if key != "clock"},
        {**payload, "clock": {**payload["clock"], "unit": "ns"}},
        {**payload, "environment": {**payload["environment"], "system": 1}},
        {**payload, "phases": [{**payload["phases"][0], "future": 1}, *payload["phases"][1:]]},
        {**payload, "evidence": [{1: "bad", **payload["evidence"][0]}]},
    ]

    for invalid in invalid_values:
        with pytest.raises(ArtifactValidationError):
            MetricEvent.from_dict(invalid)


def test_json_parser_rejects_duplicate_keys_noncanonical_bytes_and_nonfinite_numbers():
    event = sample_event()

    with pytest.raises(ArtifactValidationError, match="duplicate"):
        MetricEvent.from_json_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ArtifactValidationError, match="canonical"):
        MetricEvent.from_json_bytes(json.dumps(event.to_dict()).encode())
    with pytest.raises(ArtifactValidationError, match="non-finite"):
        MetricEvent.from_json_bytes(event.to_json_bytes().replace(b'"schema_version":1', b'"schema_version":NaN'))
    with pytest.raises(ArtifactValidationError, match="UTF-8"):
        MetricEvent.from_json_bytes(b"\xff")
    with pytest.raises(ArtifactValidationError, match="bytes"):
        MetricEvent.from_json_bytes("{}")  # type: ignore[arg-type]


@pytest.mark.parametrize("schema_version", [True, 1.0, 0, 2])
def test_schema_version_is_an_exact_supported_integer(schema_version: object):
    with pytest.raises(ArtifactValidationError, match="schema_version"):
        replace(sample_event(), schema_version=schema_version)  # type: ignore[arg-type]


def test_parser_rejects_float_bool_and_out_of_range_metrics_without_coercion():
    payload = sample_event().to_dict()
    for bad_value in (True, 1.0, -1, INT64_MAX + 1):
        invalid = decode(encode(payload))
        invalid["resources"]["cpu_time_ns"]["value"] = bad_value
        with pytest.raises(ArtifactValidationError, match="integer"):
            MetricEvent.from_json_bytes(encode(invalid))


def test_failed_event_round_trips_without_requiring_successful_later_phases():
    phases = list(sample_phases())
    failure_index = PHASES.index("vm_boot")
    phases[failure_index] = replace(phases[failure_index], outcome="failed")
    for index in range(failure_index + 1, len(phases)):
        phases[index] = PhaseObservation(
            phase=PHASES[index],
            outcome="skipped",
            started_at_ns=ObservedInt.missing("not_executed"),
            duration_ns=ObservedInt.missing("not_executed"),
        )
    event = replace(
        sample_event(),
        phases=tuple(phases),
        outcome=Outcome("failed", "vm_boot"),
    )

    assert MetricEvent.from_json_bytes(event.to_json_bytes()) == event


def test_run_and_phase_outcomes_cannot_contradict_each_other():
    phases = list(sample_phases())
    phases[0] = replace(phases[0], outcome="failed")

    with pytest.raises(ArtifactValidationError, match="terminal"):
        replace(sample_event(), phases=tuple(phases))
    with pytest.raises(ArtifactValidationError, match="terminal phase"):
        replace(sample_event(), outcome=Outcome("failed", "internal"))


def test_module_has_no_operational_imports_or_import_time_calls():
    module_path = Path(metrics_module.__file__)
    tree = ast.parse(module_path.read_text())
    forbidden_roots = {
        "kvm",
        "os",
        "pathlib",
        "registry",
        "requests",
        "runtime",
        "socket",
        "subprocess",
        "urllib",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] not in forbidden_roots for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[-1] not in forbidden_roots
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            pytest.fail(f"unexpected import-time call at line {node.lineno}")


def test_raw_event_contract_does_not_embed_percentile_aggregation():
    payload = sample_event().to_dict()

    assert not ({"percentile", "p50", "p95", "p99", "sample_count"} & set(payload))


def test_cancelled_and_timed_out_phases_are_terminal_and_align_with_run_outcome():
    for phase_outcome, run_status, category in (
        ("cancelled", "cancelled", "cancelled"),
        ("timed_out", "failed", "timeout"),
    ):
        phases = list(sample_phases())
        terminal_index = PHASES.index("vm_boot")
        phases[terminal_index] = replace(phases[terminal_index], outcome=phase_outcome)
        for index in range(terminal_index + 1, len(phases)):
            phases[index] = PhaseObservation(
                phase=PHASES[index],
                outcome="skipped",
                started_at_ns=ObservedInt.missing("not_executed"),
                duration_ns=ObservedInt.missing("not_executed"),
            )
        event = replace(
            sample_event(),
            phases=tuple(phases),
            outcome=Outcome(run_status, category),
        )
        assert MetricEvent.from_json_bytes(event.to_json_bytes()) == event


def test_first_terminal_phase_requires_every_later_phase_to_be_nonexecuted():
    phases = list(sample_phases())
    phases[1] = replace(phases[1], outcome="failed")

    with pytest.raises(ArtifactValidationError, match="terminal"):
        replace(sample_event(), phases=tuple(phases), outcome=Outcome("failed", "resolution"))


def test_cancelled_run_cannot_claim_every_phase_succeeded():
    with pytest.raises(ArtifactValidationError, match="cancelled"):
        replace(sample_event(), outcome=Outcome("cancelled", "cancelled"))


def test_elapsed_clock_and_optional_wall_anchor_are_distinct_fields():
    clock_payload = sample_event().clock.to_dict()

    assert "wall_started_unix_ns" in clock_payload
    assert "origin_unix_ns" not in clock_payload


def test_distribution_discloses_separate_cache_placement_and_materialization_dimensions():
    payload = sample_event().distribution.to_dict()

    assert set(payload) == {
        "cache_temperature",
        "cache_hit_level",
        "placement",
        "distribution_mode",
        "executed",
        "materialization_mode",
        "source_region",
        "destination_region",
        "registry_bytes_received",
        "cross_region_bytes_received",
        "unique_storage_bytes",
        "writable_upper_growth_bytes",
    }


def test_resource_counters_disclose_one_comparable_scope_and_boundary():
    payload = sample_resources().to_dict()

    assert payload["scope"] == "vm"
    assert payload["boundary"] == "whole-run"


@pytest.mark.parametrize("run_id", ["/tmp/run", "../run", "token=secret", "run?id=1", "run#fragment"])
def test_run_id_is_an_opaque_token_not_a_path_or_secret_container(run_id: str):
    with pytest.raises(ArtifactValidationError, match="run_id"):
        replace(sample_event(), run_id=run_id)


@pytest.mark.parametrize(
    "uri",
    [
        "file:///var/lib/private/evidence.json",
        "https://user:password@example.test/evidence",
        "https://example.test/evidence?token=secret",
        "https://example.test/evidence#credential",
        "ftp://example.test/evidence",
    ],
)
def test_evidence_uri_rejects_local_paths_credentials_and_unapproved_schemes(uri: str):
    with pytest.raises(ArtifactValidationError, match="uri"):
        RawEvidence(uri, digest("a"))


def test_one_evidence_uri_cannot_claim_two_different_digests():
    with pytest.raises(ArtifactValidationError, match="URI"):
        sample_event(
            evidence=(
                RawEvidence("artifact://run/raw", digest("a")),
                RawEvidence("artifact://run/raw", digest("b")),
            )
        )


def test_validation_errors_do_not_echo_unknown_or_duplicate_attacker_keys():
    attacker_key = "token_super_secret_123"
    payload = sample_event().to_dict()
    payload[attacker_key] = True

    with pytest.raises(ArtifactValidationError) as unknown:
        MetricEvent.from_dict(payload)
    assert attacker_key not in str(unknown.value)

    duplicate = b'{"token_super_secret_123":1,"token_super_secret_123":2}'
    with pytest.raises(ArtifactValidationError) as repeated:
        MetricEvent.from_json_bytes(duplicate)
    assert attacker_key not in str(repeated.value)


def test_json_parser_rejects_oversized_and_excessively_nested_payloads_cleanly():
    oversized = b'"' + b"a" * (1024 * 1024) + b'"'
    with pytest.raises(ArtifactValidationError, match="size"):
        MetricEvent.from_json_bytes(oversized)

    nested: object = None
    for _ in range(32):
        nested = [nested]
    with pytest.raises(ArtifactValidationError, match="depth"):
        MetricEvent.from_json_bytes(json.dumps(nested).encode())


def terminal_event(phase: str, category: str, *, phase_outcome: str = "failed") -> MetricEvent:
    phases = list(sample_phases())
    terminal_index = PHASES.index(phase)
    phases[terminal_index] = replace(phases[terminal_index], outcome=phase_outcome)
    for index in range(terminal_index + 1, len(phases)):
        phases[index] = PhaseObservation(
            phase=PHASES[index],
            outcome="skipped",
            started_at_ns=ObservedInt.missing("not_executed"),
            duration_ns=ObservedInt.missing("not_executed"),
        )
    changes: dict[str, object] = {"phases": tuple(phases), "outcome": Outcome("failed", category)}
    if terminal_index < PHASES.index("fetch"):
        changes["distribution"] = desired_unexecuted_distribution()
    return replace(sample_event(), **changes)


@pytest.mark.parametrize(
    ("category", "phase"),
    [
        ("resolution", "resolve"),
        ("source_unavailable", "locate"),
        ("verification", "verify"),
        ("materialization", "materialize"),
        ("vm_boot", "vm_boot"),
        ("mount", "mount"),
        ("initialization", "initialize"),
        ("model_load", "model_load"),
        ("application", "application_ready"),
    ],
)
def test_failure_category_has_an_explicit_allowed_terminal_phase(category: str, phase: str):
    event = terminal_event(phase, category)

    assert phase in metrics_module.FAILURE_CATEGORY_PHASES[category]
    assert MetricEvent.from_json_bytes(event.to_json_bytes()) == event


def test_failure_category_rejects_an_unrelated_terminal_phase():
    with pytest.raises(ArtifactValidationError, match="failure category"):
        terminal_event("vm_boot", "resolution")
    with pytest.raises(ArtifactValidationError, match="failure category"):
        terminal_event("verify", "source_unavailable")


def test_failure_category_phase_mapping_is_complete_immutable_and_bounded():
    mapping = metrics_module.FAILURE_CATEGORY_PHASES

    assert set(mapping) == FAILURE_CATEGORIES
    assert mapping["source_unavailable"] == {"resolve", "locate", "fetch"}
    assert all(phases and phases <= set(PHASES) for phases in mapping.values())
    with pytest.raises(TypeError):
        mapping["resolution"] = frozenset({"vm_boot"})  # type: ignore[index]


@pytest.mark.parametrize("category", ["resource_exhausted", "policy_denied", "unsupported", "internal"])
def test_cross_cutting_failure_categories_remain_bounded_to_canonical_phases(category: str):
    event = terminal_event("fetch", category)

    assert MetricEvent.from_json_bytes(event.to_json_bytes()) == event


def test_elapsed_clock_source_cannot_be_a_wall_clock():
    with pytest.raises(ArtifactValidationError, match="clock source"):
        replace(sample_event().clock, source="wall")


def test_cross_region_counter_and_placement_must_agree():
    local = sample_event().distribution
    with pytest.raises(ArtifactValidationError, match="cross-region"):
        replace(local, cross_region_bytes_received=measured(1))

    cross_region = replace(
        local,
        placement="cross-region",
        source_region=text("us-east-1"),
        destination_region=text("kr-seoul-1"),
        cross_region_bytes_received=measured(10),
    )
    assert cross_region.cross_region_bytes_received.value == 10
    with pytest.raises(ArtifactValidationError, match="cross-region"):
        replace(cross_region, cross_region_bytes_received=measured(0))
    assert (
        replace(
            cross_region,
            cross_region_bytes_received=ObservedInt.missing("not_reported"),
        ).cross_region_bytes_received.value
        is None
    )


def test_registry_counter_and_distribution_mode_must_agree():
    registry_pull = sample_event().distribution
    with pytest.raises(ArtifactValidationError, match="registry"):
        replace(registry_pull, registry_bytes_received=measured(0))
    with pytest.raises(ArtifactValidationError, match="registry"):
        replace(registry_pull, registry_bytes_received=ObservedInt.missing("not_applicable"))

    shared = replace(
        registry_pull,
        distribution_mode="shared-store",
        registry_bytes_received=measured(0),
    )
    assert shared.registry_bytes_received.value == 0
    with pytest.raises(ArtifactValidationError, match="registry"):
        replace(shared, registry_bytes_received=measured(1))
    assert (
        replace(
            shared,
            registry_bytes_received=ObservedInt.missing("not_applicable"),
        ).registry_bytes_received.value
        is None
    )


@pytest.mark.parametrize(
    "uri",
    [
        "artifact://run/../private",
        "artifact://run/./raw",
        "https://example.test/raw/token=secret",
        "s3://bucket/evidence/credential=value",
        "gs://bucket/api_key=value",
    ],
)
def test_evidence_uri_rejects_dot_segments_and_secret_assignments(uri: str):
    with pytest.raises(ArtifactValidationError, match="uri"):
        RawEvidence(uri, digest("a"))


def test_schema_and_json_constant_errors_do_not_reflect_attacker_values():
    attacker_value = "attacker-secret-schema-value"
    with pytest.raises(ArtifactValidationError) as schema_error:
        replace(sample_event(), schema_version=attacker_value)  # type: ignore[arg-type]
    assert attacker_value not in str(schema_error.value)

    with pytest.raises(ArtifactValidationError) as constant_error:
        MetricEvent.from_json_bytes(b'{"value":Infinity}')
    assert "Infinity" not in str(constant_error.value)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("not_applicable", "not_reported"),
        ("skipped", "not_applicable"),
        ("succeeded", "not_applicable"),
        ("failed", "not_executed"),
        ("cancelled", "evidence_missing"),
        ("timed_out", "not_applicable"),
    ],
)
def test_phase_timing_missing_reason_must_align_with_outcome(outcome: str, reason: str):
    with pytest.raises(ArtifactValidationError, match="missing reason"):
        PhaseObservation(
            phase="model_load",
            outcome=outcome,
            started_at_ns=ObservedInt.missing(reason),
            duration_ns=ObservedInt.missing(reason),
        )


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("not_applicable", "not_applicable"),
        ("skipped", "not_executed"),
        ("succeeded", "not_reported"),
        ("failed", "collection_failed"),
        ("cancelled", "clock_incompatible"),
        ("timed_out", "permission_denied"),
    ],
)
def test_phase_timing_accepts_only_honest_outcome_specific_missing_reason(outcome: str, reason: str):
    phase = PhaseObservation(
        phase="model_load",
        outcome=outcome,
        started_at_ns=ObservedInt.missing(reason),
        duration_ns=ObservedInt.missing(reason),
    )

    assert phase.started_at_ns.missing_reason == reason


@pytest.mark.parametrize(
    "uri",
    [
        "https://example..test/raw",
        "https://.example.test/raw",
        "https://example.test./raw",
        f"https://{'a' * 64}.example/raw",
        "https://exa_mple.test/raw",
        "https://example.test:0/raw",
        "https://example.test:65536/raw",
        "https://example.test:abc/raw",
        "https://example.test:/raw",
        "https://example.test:443:444/raw",
        "https://userinfo@example.test/raw",
        "s3://Bad_Bucket/raw",
        "gs://bucket_name/raw",
        "https://example.test/raw|pipe",
        "https://example.test/raw[0]",
        "https://example.test/raw\\windows",
        "https://example.test/raw\nnext",
    ],
)
def test_evidence_uri_strictly_rejects_invalid_authority_port_and_path(uri: str):
    with pytest.raises(ArtifactValidationError, match="uri"):
        RawEvidence(uri, digest("a"))


def test_network_evidence_uri_canonicalizes_scheme_dns_host_and_integer_port():
    evidence = RawEvidence("HTTPS://Example.COM:00443/Raw_Data", digest("a"))

    assert evidence.uri == "https://example.com/Raw_Data"


def test_equivalent_network_evidence_uris_have_identical_event_bytes_and_digest():
    canonical = RawEvidence("https://example.com/raw", digest("a"))
    equivalent = RawEvidence("HTTPS://EXAMPLE.COM:00443/raw", digest("a"))
    first = sample_event(evidence=(canonical,))
    second = sample_event(evidence=(equivalent,))

    assert first.to_json_bytes() == second.to_json_bytes()
    assert first.digest == second.digest


def test_https_default_port_and_empty_path_have_one_canonical_uri():
    with_default = RawEvidence("HTTPS://EXAMPLE.COM:443", digest("a"))
    with_root_path = RawEvidence("https://example.com/", digest("a"))

    assert with_default.uri == with_root_path.uri == "https://example.com/"


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b'{"secret":',
        b"[" * 1000 + b"]" * 1000,
    ],
)
def test_parser_wrappers_suppress_attacker_bearing_causes(payload: bytes):
    with pytest.raises(ArtifactValidationError) as error:
        MetricEvent.from_json_bytes(payload)

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert error.value.__suppress_context__ is True


def test_digest_and_canonical_json_wrappers_suppress_attacker_bearing_causes():
    with pytest.raises(ArtifactValidationError) as digest_error:
        RawEvidence("artifact://run/raw", "token=super-secret")
    assert digest_error.value.__cause__ is None
    assert digest_error.value.__context__ is None
    assert digest_error.value.__suppress_context__ is True

    class SecretValue:
        def __repr__(self) -> str:
            return "token=super-secret"

    with pytest.raises(ArtifactValidationError) as json_error:
        canonical_json_bytes(SecretValue())
    assert json_error.value.__cause__ is None
    assert json_error.value.__context__ is None
    assert json_error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "uri",
    [
        "artifact://run/authorization=Bearer:abc123",
        "artifact://run/cookie=session123",
        "artifact://run/private_key=keydata",
        "artifact://run/private-key=keydata",
        "artifact://run/api_key=keydata",
        "artifact://run/client_secret=value",
        "artifact://run/credential=value",
        "artifact://run/Bearer:abc123",
        "artifact://run/Basic=YWJjOjEyMw==",
        "artifact://run/user:pass@host",
    ],
)
def test_evidence_uri_rejects_authorization_cookie_key_and_userinfo_material(uri: str):
    with pytest.raises(ArtifactValidationError, match="uri"):
        RawEvidence(uri, digest("a"))


@pytest.mark.parametrize(
    "operation",
    [
        lambda: RawEvidence("artifact://run/raw", "private_key=super-secret"),
        lambda: MetricEvent.from_json_bytes(b"\xffprivate_key=super-secret"),
        lambda: MetricEvent.from_json_bytes(b'{"private_key":"super-secret"'),
        lambda: MetricEvent.from_json_bytes(b"[" * 1000 + b"]" * 1000),
        lambda: canonical_json_bytes({"private_key": object()}),
    ],
)
def test_attacker_bearing_wrappers_clear_cause_context_and_formatted_traceback(operation):
    with pytest.raises(ArtifactValidationError) as error:
        operation()

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    rendered = "".join(traceback.format_exception_only(error.value))
    assert "super-secret" not in rendered
    assert "private_key" not in rendered


@pytest.mark.parametrize(
    "uri",
    [
        "artifact://run/authorization:Basic=YWJj",
        "artifact://run/cookie:session-value",
        "artifact://run/token:token-value",
        "artifact://run/password:password-value",
        "artifact://run/private_key:key-value",
        "artifact://run/private-key:key-value",
        "artifact://run/api_key:key-value",
        "artifact://run/client-secret:secret-value",
        "artifact://run/credential:credential-value",
        "artifact://run/access-key:access-value",
        "artifact://run/signature:signature-value",
        "artifact://run/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "artifact://run/aaaaaaaa.bbbbbbbb.cccccccc",
        "artifact://run/aaaaaaaaa.bbbbbbbbb.ccccccccc",
        "artifact://run/AKIAIOSFODNN7EXAMPLE",
        "artifact://run/ASIAIOSFODNN7EXAMPLE",
    ],
)
def test_evidence_uri_rejects_colon_credentials_jwt_and_aws_access_keys(uri: str):
    with pytest.raises(ArtifactValidationError, match="uri"):
        RawEvidence(uri, digest("a"))


def test_evidence_uri_allows_safe_at_pchar_and_canonical_oci_digest_qualifier():
    release = RawEvidence("artifact://run/release@v1", digest("a"))
    qualified = RawEvidence(f"OCI://REGISTRY.EXAMPLE/repository@{digest('b')}", digest("a"))

    assert release.uri == "artifact://run/release@v1"
    assert qualified.uri == f"oci://registry.example/repository@{digest('b')}"


def test_evidence_uri_allows_safe_dotted_filename_that_is_not_jwt_shaped():
    evidence = RawEvidence("artifact://run/report.v1.json", digest("a"))

    assert evidence.uri == "artifact://run/report.v1.json"


@pytest.mark.parametrize(
    "uri",
    [
        "artifact://run/user:pass@host",
        "artifact://run/service:credential@remote",
    ],
)
def test_evidence_uri_rejects_userinfo_shaped_path_but_not_digest_qualifier(uri: str):
    with pytest.raises(ArtifactValidationError, match="uri"):
        RawEvidence(uri, digest("a"))


def desired_unexecuted_distribution(*, counter: ObservedInt | None = None) -> DistributionObservation:
    not_executed = ObservedInt.missing("not_executed") if counter is None else counter
    return DistributionObservation(
        executed=False,
        cache_temperature="not-observed",
        cache_hit_level="not-observed",
        placement="not-started",
        distribution_mode="not-started",
        materialization_mode="not-started",
        source_region=ObservedText.missing("not_executed"),
        destination_region=ObservedText.missing("not_executed"),
        registry_bytes_received=not_executed,
        cross_region_bytes_received=not_executed,
        unique_storage_bytes=not_executed,
        writable_upper_growth_bytes=not_executed,
    )


def test_unexecuted_distribution_round_trips_without_fabricated_transfer_values():
    distribution = desired_unexecuted_distribution()

    assert distribution.executed is False  # type: ignore[attr-defined]
    assert distribution.registry_bytes_received.missing_reason == "not_executed"
    assert DistributionObservation.from_dict(distribution.to_dict()) == distribution


def test_unexecuted_distribution_allows_known_zero_but_rejects_nonzero_counters():
    assert desired_unexecuted_distribution(counter=measured(0)).registry_bytes_received.value == 0
    with pytest.raises(ArtifactValidationError, match="unexecuted"):
        desired_unexecuted_distribution(counter=measured(1))


def test_resolve_failure_requires_unexecuted_distribution_evidence():
    phases = list(sample_phases())
    phases[0] = replace(phases[0], outcome="failed")
    for index in range(1, len(phases)):
        phases[index] = PhaseObservation(
            phase=PHASES[index],
            outcome="skipped",
            started_at_ns=ObservedInt.missing("not_executed"),
            duration_ns=ObservedInt.missing("not_executed"),
        )

    with pytest.raises(ArtifactValidationError, match="distribution"):
        replace(sample_event(), phases=tuple(phases), outcome=Outcome("failed", "resolution"))
    event = replace(
        sample_event(),
        phases=tuple(phases),
        distribution=desired_unexecuted_distribution(),
        outcome=Outcome("failed", "resolution"),
    )
    assert MetricEvent.from_json_bytes(event.to_json_bytes()) == event


def test_executed_cross_region_distribution_requires_disclosed_distinct_regions():
    distribution = sample_event().distribution

    for missing in (
        ObservedText.missing("not_reported"),
        ObservedText.missing("not_executed"),
        ObservedText.missing("not_applicable"),
    ):
        with pytest.raises(ArtifactValidationError, match="region"):
            replace(
                distribution,
                placement="cross-region",
                source_region=missing,
                cross_region_bytes_received=measured(1),
            )
