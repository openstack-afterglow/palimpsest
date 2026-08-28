"""Adversarial contracts for pure sandbox-policy intent and preflight."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import palimpsest_local.sandbox_policy as policy_module
from palimpsest_local.sandbox_policy import (
    CLOSED_SANDBOX_POLICY,
    PHASE1_SANDBOX_CAPABILITIES,
    AuditPolicy,
    BinaryPolicy,
    BinaryPolicyHook,
    DeviceAccessPolicy,
    DeviceRequest,
    EgressPolicy,
    PolicyPreflightCode,
    PolicyPreflightIssue,
    PolicyPreflightResult,
    SandboxPolicyCapabilities,
    SandboxPolicyRequest,
    SandboxPolicyValidationError,
    SecretDeliveryPolicy,
    SecretReference,
    SnapshotScrubPolicy,
    UnsupportedSandboxPolicyError,
    canonical_policy_json_bytes,
    preflight_sandbox_policy,
)


def sha256(character: str = "a") -> str:
    return "sha256:" + character * 64


def decode(payload: bytes) -> dict[str, object]:
    value = json.loads(payload)
    assert isinstance(value, dict)
    return value


def encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def future_request() -> SandboxPolicyRequest:
    return SandboxPolicyRequest(
        egress=EgressPolicy(mode="allow_all"),
        secret_delivery=SecretDeliveryPolicy(
            mode="references",
            references=(SecretReference(provider="barbican", reference_id="database-production"),),
        ),
        device_access=DeviceAccessPolicy(
            mode="allowlist",
            devices=(DeviceRequest(device_class="gpu", allocation_id="allocation-7"),),
        ),
        audit=AuditPolicy(mode="disabled"),
        snapshot_scrub=SnapshotScrubPolicy(enabled=True, scrub_required=True),
        binary_policy=BinaryPolicy(
            mode="evaluate",
            hooks=(BinaryPolicyHook(policy_id="signed-binary-v1", policy_digest=sha256()),),
        ),
    )


def test_closed_defaults_are_explicit_immutable_and_inspection_safe():
    assert CLOSED_SANDBOX_POLICY == SandboxPolicyRequest()
    assert CLOSED_SANDBOX_POLICY.to_dict() == {
        "audit": {"mode": "required"},
        "binary_policy": {"hooks": [], "mode": "no_hooks"},
        "device_access": {"devices": [], "mode": "deny"},
        "egress": {"mode": "deny"},
        "schema_version": 1,
        "secret_delivery": {"mode": "none", "references": []},
        "snapshot_scrub": {"enabled": False, "scrub_required": True},
    }
    with pytest.raises(FrozenInstanceError):
        CLOSED_SANDBOX_POLICY.schema_version = 2  # type: ignore[misc]


def test_default_preflight_is_supported_and_serializes_stably():
    result = preflight_sandbox_policy(CLOSED_SANDBOX_POLICY)

    assert result == PolicyPreflightResult(supported=True, issues=())
    assert result.to_dict() == {"issues": [], "schema_version": 1, "supported": True}
    assert result.to_json_bytes() == b'{"issues":[],"schema_version":1,"supported":true}'
    result.raise_for_unsupported()


def test_policy_roundtrip_bytes_and_digest_cover_exact_canonical_payload():
    request = future_request()
    payload = request.to_json_bytes()

    assert payload == canonical_policy_json_bytes(request.to_dict())
    assert request.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert SandboxPolicyRequest.from_json_bytes(payload) == request
    assert SandboxPolicyRequest.from_dict(request.to_dict()) == request
    assert b" " not in payload


def test_set_semantics_sort_and_deduplicate_references_devices_and_hooks():
    reference_a = SecretReference("vault", "alpha")
    reference_b = SecretReference("barbican", "beta")
    device_a = DeviceRequest("gpu", "allocation-b")
    device_b = DeviceRequest("fpga", "allocation-a")
    hook_a = BinaryPolicyHook("z-policy", sha256("f"))
    hook_b = BinaryPolicyHook("a-policy", sha256("e"))

    request = SandboxPolicyRequest(
        secret_delivery=SecretDeliveryPolicy("references", (reference_a, reference_b, reference_a)),
        device_access=DeviceAccessPolicy("allowlist", (device_a, device_b, device_a)),
        binary_policy=BinaryPolicy("evaluate", (hook_a, hook_b, hook_a)),
    )

    assert request.secret_delivery.references == (reference_b, reference_a)
    assert request.device_access.devices == (device_b, device_a)
    assert request.binary_policy.hooks == (hook_b, hook_a)
    assert SandboxPolicyRequest.from_dict(request.to_dict()).to_json_bytes() == request.to_json_bytes()


@pytest.mark.parametrize(
    ("factory", "args"),
    [
        (EgressPolicy, {"mode": "default"}),
        (EgressPolicy, {"mode": True}),
        (SecretDeliveryPolicy, {"mode": "none", "references": []}),
        (SecretDeliveryPolicy, {"mode": "references", "references": ()}),
        (DeviceAccessPolicy, {"mode": "deny", "devices": []}),
        (DeviceAccessPolicy, {"mode": "allowlist", "devices": ()}),
        (AuditPolicy, {"mode": False}),
        (SnapshotScrubPolicy, {"enabled": 0, "scrub_required": True}),
        (SnapshotScrubPolicy, {"enabled": False, "scrub_required": 1}),
        (BinaryPolicy, {"mode": "no_hooks", "hooks": []}),
        (BinaryPolicy, {"mode": "deny", "hooks": ()}),
        (BinaryPolicy, {"mode": "evaluate", "hooks": ()}),
    ],
)
def test_nested_types_reject_wrong_exact_types_and_contradictory_requests(
    factory: type[object], args: dict[str, object]
):
    with pytest.raises(SandboxPolicyValidationError):
        factory(**args)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", b"secret"),
        ("provider", "Vault"),
        ("provider", "/etc/provider"),
        ("reference_id", "../private/key"),
        ("reference_id", "password=hunter2"),
        ("reference_id", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"),
        ("reference_id", "AKIAIOSFODNN7EXAMPLE"),
        ("reference_id", "-----BEGIN PRIVATE KEY-----"),
    ],
)
def test_secret_references_reject_values_bytes_credentials_and_paths(field: str, value: object):
    kwargs: dict[str, object] = {"provider": "vault", "reference_id": "opaque-id"}
    kwargs[field] = value
    with pytest.raises(SandboxPolicyValidationError) as caught:
        SecretReference(**kwargs)  # type: ignore[arg-type]

    assert "hunter2" not in str(caught.value)
    assert "PRIVATE KEY" not in str(caught.value)


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (DeviceRequest, {"device_class": "gpu", "allocation_id": "/dev/nvidia0"}),
        (DeviceRequest, {"device_class": "gpu", "allocation_id": "token=abc"}),
        (BinaryPolicyHook, {"policy_id": "/usr/bin/policy", "policy_digest": sha256()}),
        (BinaryPolicyHook, {"policy_id": "policy", "policy_digest": "sha256:" + "A" * 64}),
        (BinaryPolicyHook, {"policy_id": "policy", "policy_digest": b"a" * 64}),
    ],
)
def test_device_and_binary_requests_cannot_smuggle_paths_credentials_or_binaries(
    factory: type[object], kwargs: dict[str, object]
):
    with pytest.raises(SandboxPolicyValidationError):
        factory(**kwargs)  # type: ignore[call-arg]


def test_opaque_reference_serialization_contains_only_identifiers_not_secret_delivery_material():
    request = SandboxPolicyRequest(
        secret_delivery=SecretDeliveryPolicy(
            "references",
            (SecretReference("barbican", "production-database"),),
        )
    )
    payload = request.to_json_bytes()

    assert b"production-database" in payload
    assert b"mount" not in payload
    assert b"value" not in payload
    assert b"credential" not in payload
    assert b"/" not in payload


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("audit"),
        lambda value: value.update({"future": {}}),
        lambda value: value["audit"].update({"future": True}),
        lambda value: value["secret_delivery"]["references"].append({"provider": "vault"}),
        lambda value: value["device_access"]["devices"].append(
            {"allocation_id": "alloc", "device_class": "gpu", "host_path": "/dev/nvidia0"}
        ),
        lambda value: value["binary_policy"]["hooks"].append(
            {"policy_digest": sha256(), "policy_id": "policy", "command": "/usr/bin/check"}
        ),
    ],
)
def test_missing_unknown_and_nested_namespace_substitution_are_rejected(mutation):
    value = CLOSED_SANDBOX_POLICY.to_dict()
    mutation(value)
    with pytest.raises(SandboxPolicyValidationError):
        SandboxPolicyRequest.from_dict(value)


@pytest.mark.parametrize("schema_version", [2, 0, -1, True, False, 1.0, "1", None])
def test_unknown_or_non_integer_schema_versions_fail_closed(schema_version: object):
    value = CLOSED_SANDBOX_POLICY.to_dict()
    value["schema_version"] = schema_version

    with pytest.raises(SandboxPolicyValidationError, match="schema_version"):
        SandboxPolicyRequest.from_dict(value)


def test_duplicate_top_level_and_nested_json_fields_are_rejected_without_reflection():
    payload = CLOSED_SANDBOX_POLICY.to_json_bytes()
    duplicate_top = payload[:-1] + b',"schema_version":1}'
    duplicate_nested = payload.replace(b'{"mode":"required"}', b'{"mode":"required","mode":"disabled"}')

    for invalid in (duplicate_top, duplicate_nested):
        with pytest.raises(SandboxPolicyValidationError, match="duplicate"):
            SandboxPolicyRequest.from_json_bytes(invalid)


def test_json_requires_bytes_utf8_finite_numbers_size_limit_and_canonical_encoding():
    valid = CLOSED_SANDBOX_POLICY.to_dict()
    pretty = json.dumps(valid, indent=2).encode()
    nonfinite = CLOSED_SANDBOX_POLICY.to_json_bytes().replace(b'"schema_version":1', b'"schema_version":NaN')

    for invalid in (pretty, nonfinite, b"\xff", b"[", b"x" * (policy_module.MAX_POLICY_JSON_BYTES + 1)):
        with pytest.raises(SandboxPolicyValidationError):
            SandboxPolicyRequest.from_json_bytes(invalid)
    with pytest.raises(SandboxPolicyValidationError):
        SandboxPolicyRequest.from_json_bytes("{}")  # type: ignore[arg-type]


def test_malformed_json_does_not_chain_a_parser_error_that_can_echo_payload_material():
    with pytest.raises(SandboxPolicyValidationError) as caught:
        SandboxPolicyRequest.from_json_bytes(b'{"future":"password=hunter2"')

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "hunter2" not in str(caught.value)


@pytest.mark.parametrize(
    "parser",
    [
        SandboxPolicyRequest.from_json_bytes,
        SandboxPolicyCapabilities.from_json_bytes,
        PolicyPreflightResult.from_json_bytes,
    ],
)
def test_invalid_utf8_does_not_retain_attacker_bytes_in_a_chained_decode_error(parser):
    payload = b'{"future":"credential=super-secret-\xff"}'

    with pytest.raises(SandboxPolicyValidationError) as caught:
        parser(payload)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "super-secret" not in str(caught.value)


def test_recursive_python_data_is_a_typed_validation_failure():
    recursive: dict[str, object] = {}
    recursive["self"] = recursive

    with pytest.raises(SandboxPolicyValidationError, match="nesting"):
        SandboxPolicyRequest.from_dict(recursive)
    with pytest.raises(SandboxPolicyValidationError, match="nesting"):
        canonical_policy_json_bytes(recursive)


@pytest.mark.parametrize(
    "key_value",
    [
        ("password", "hunter2"),
        ("api_key", "abc"),
        ("authorization", "Bearer abcdef"),
        ("future", "https://user:password@example.test/private"),
        ("future", "token=super-secret"),
        ("future", "/home/operator/private"),
        ("future", "C:\\Users\\operator\\private"),
    ],
)
def test_raw_payload_secret_shaped_keys_values_credentials_and_paths_fail_without_echo(key_value: tuple[str, str]):
    key, secret = key_value
    value = CLOSED_SANDBOX_POLICY.to_dict()
    value[key] = secret

    with pytest.raises(SandboxPolicyValidationError) as caught:
        SandboxPolicyRequest.from_dict(value)

    assert secret not in str(caught.value)


def test_generic_canonical_codec_cannot_be_used_to_serialize_secret_material():
    for value in (
        {"password": "hunter2"},
        {"database_password_value": "hunter2"},
        {"clientSecretMaterial": "hunter2"},
        {"future": "token=super-secret"},
        {"future": "/home/operator/private"},
        {"future": ("safe", "Bearer extremely-sensitive-credential")},
        {"future": ["eyJhbGciOiJIUzI1NiJ9", "aaaaaaaa.bbbbbbbb.cccccccc"]},
        {"future": "https://operator:credential@example.test/private"},
        {"future": "prefix /home/operator/private"},
    ):
        with pytest.raises(SandboxPolicyValidationError) as caught:
            canonical_policy_json_bytes(value)

        assert "hunter2" not in str(caught.value)
        assert "super-secret" not in str(caught.value)
        assert "/home/operator/private" not in str(caught.value)


@pytest.mark.parametrize(
    "sensitive",
    [
        "prefix-token=super-secret",
        "prefix.clientSecret=super-secret",
        "apparently-safe/relative-segment",
        "apparently-safe\\relative-segment",
        "operator:credential@example.test",
        "prefix(operator:credential@example.test)suffix",
    ],
)
def test_canonical_codec_rejects_compound_assignments_and_any_path_or_userinfo_material(sensitive: str):
    with pytest.raises(SandboxPolicyValidationError) as caught:
        canonical_policy_json_bytes({"future": sensitive})

    assert sensitive not in str(caught.value)


def test_canonical_codec_scans_dict_subclasses_and_nested_tuple_list_containers():
    class PolicyMapping(dict[str, object]):
        pass

    value = PolicyMapping({"future": [("safe", {"databaseTokenSuffix": "opaque"})]})

    with pytest.raises(SandboxPolicyValidationError):
        canonical_policy_json_bytes(value)


def test_unknown_field_names_are_not_reflected_in_validation_errors():
    value = CLOSED_SANDBOX_POLICY.to_dict()
    sensitive_key = "sk-production-credential-material"
    value[sensitive_key] = "opaque"

    with pytest.raises(SandboxPolicyValidationError) as caught:
        SandboxPolicyRequest.from_dict(value)

    assert sensitive_key not in str(caught.value)


def test_every_nondefault_phase1_policy_is_reported_in_stable_field_order():
    result = preflight_sandbox_policy(future_request())

    assert result.supported is False
    assert tuple(issue.code for issue in result.issues) == (
        PolicyPreflightCode.UNSUPPORTED_EGRESS,
        PolicyPreflightCode.UNSUPPORTED_SECRET_DELIVERY,
        PolicyPreflightCode.UNSUPPORTED_DEVICE_ACCESS,
        PolicyPreflightCode.UNSUPPORTED_AUDIT,
        PolicyPreflightCode.UNSUPPORTED_SNAPSHOT,
        PolicyPreflightCode.UNSUPPORTED_BINARY_POLICY,
    )
    assert tuple(issue.field for issue in result.issues) == (
        "egress",
        "secret_delivery",
        "device_access",
        "audit",
        "snapshot_scrub",
        "binary_policy",
    )
    assert result.to_json_bytes() == canonical_policy_json_bytes(result.to_dict())


@pytest.mark.parametrize(
    ("policy_request", "expected"),
    [
        (replace(CLOSED_SANDBOX_POLICY, egress=EgressPolicy("allow_all")), PolicyPreflightCode.UNSUPPORTED_EGRESS),
        (
            replace(
                CLOSED_SANDBOX_POLICY,
                secret_delivery=SecretDeliveryPolicy("references", (SecretReference("vault", "production-database"),)),
            ),
            PolicyPreflightCode.UNSUPPORTED_SECRET_DELIVERY,
        ),
        (
            replace(
                CLOSED_SANDBOX_POLICY,
                device_access=DeviceAccessPolicy("allowlist", (DeviceRequest("gpu", "allocation-1"),)),
            ),
            PolicyPreflightCode.UNSUPPORTED_DEVICE_ACCESS,
        ),
        (replace(CLOSED_SANDBOX_POLICY, audit=AuditPolicy("disabled")), PolicyPreflightCode.UNSUPPORTED_AUDIT),
        (
            replace(CLOSED_SANDBOX_POLICY, snapshot_scrub=SnapshotScrubPolicy(True, True)),
            PolicyPreflightCode.UNSUPPORTED_SNAPSHOT,
        ),
        (
            replace(CLOSED_SANDBOX_POLICY, snapshot_scrub=SnapshotScrubPolicy(False, False)),
            PolicyPreflightCode.UNSAFE_SNAPSHOT_SCRUB,
        ),
        (
            replace(
                CLOSED_SANDBOX_POLICY,
                binary_policy=BinaryPolicy("evaluate", (BinaryPolicyHook("policy", sha256()),)),
            ),
            PolicyPreflightCode.UNSUPPORTED_BINARY_POLICY,
        ),
    ],
)
def test_each_nondefault_request_has_one_stable_phase1_rejection(
    policy_request: SandboxPolicyRequest, expected: PolicyPreflightCode
):
    result = preflight_sandbox_policy(policy_request)

    assert result.supported is False
    assert tuple(issue.code for issue in result.issues) == (expected,)


def test_unsupported_error_contains_only_stable_codes_and_never_request_identifiers():
    request = replace(
        CLOSED_SANDBOX_POLICY,
        secret_delivery=SecretDeliveryPolicy("references", (SecretReference("barbican", "production-database"),)),
    )
    result = preflight_sandbox_policy(request)

    with pytest.raises(UnsupportedSandboxPolicyError) as caught:
        result.raise_for_unsupported()

    assert caught.value.issues == result.issues
    assert "unsupported_secret_delivery" in str(caught.value)
    assert "production-database" not in str(caught.value)
    assert "barbican" not in str(caught.value)


def test_declarative_capabilities_can_support_well_formed_future_intent_without_enforcing_it():
    request = future_request()
    capabilities = SandboxPolicyCapabilities(
        egress_modes=("allow_all", "deny", "allow_all"),
        secret_delivery_modes=("references", "none"),
        secret_references=request.secret_delivery.references,
        device_access_modes=("allowlist", "deny"),
        device_requests=request.device_access.devices,
        audit_modes=("disabled", "required"),
        snapshots_enabled=True,
        snapshot_scrub_required=True,
        binary_policy_modes=("evaluate", "no_hooks"),
        binary_policy_hooks=request.binary_policy.hooks,
    )

    assert capabilities.egress_modes == ("allow_all", "deny")
    assert preflight_sandbox_policy(request, capabilities).supported is True
    assert capabilities != PHASE1_SANDBOX_CAPABILITIES
    assert capabilities.to_json_bytes() == canonical_policy_json_bytes(capabilities.to_dict())
    assert capabilities.digest == "sha256:" + hashlib.sha256(capabilities.to_json_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("field_name", "request_update"),
    [
        (
            "secret_delivery",
            SecretDeliveryPolicy("references", (SecretReference("barbican", "unknown-secret"),)),
        ),
        (
            "device_access",
            DeviceAccessPolicy("allowlist", (DeviceRequest("gpu", "unknown-allocation"),)),
        ),
        (
            "binary_policy",
            BinaryPolicy("evaluate", (BinaryPolicyHook("unknown-policy", sha256("f")),)),
        ),
    ],
)
def test_advertising_a_mode_does_not_authorize_unknown_policy_identities(field_name: str, request_update: object):
    supported = future_request()
    capabilities = SandboxPolicyCapabilities(
        egress_modes=("deny", "allow_all"),
        secret_delivery_modes=("none", "references"),
        secret_references=supported.secret_delivery.references,
        device_access_modes=("deny", "allowlist"),
        device_requests=supported.device_access.devices,
        audit_modes=("required", "disabled"),
        snapshots_enabled=True,
        snapshot_scrub_required=True,
        binary_policy_modes=("no_hooks", "evaluate"),
        binary_policy_hooks=supported.binary_policy.hooks,
    )
    request = replace(CLOSED_SANDBOX_POLICY, **{field_name: request_update})

    result = preflight_sandbox_policy(request, capabilities)

    assert result.supported is False
    assert len(result.issues) == 1
    assert result.issues[0].field == field_name


@pytest.mark.parametrize(
    "kwargs",
    [
        {"secret_delivery_modes": ("none", "references")},
        {"secret_references": (SecretReference("vault", "id"),)},
        {"device_access_modes": ("deny", "allowlist")},
        {"device_requests": (DeviceRequest("gpu", "allocation"),)},
        {"binary_policy_modes": ("no_hooks", "evaluate")},
        {"binary_policy_hooks": (BinaryPolicyHook("policy", sha256()),)},
    ],
)
def test_capabilities_reject_mode_and_exact_selector_contradictions(kwargs: dict[str, object]):
    with pytest.raises(SandboxPolicyValidationError):
        SandboxPolicyCapabilities(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid",
    [
        SandboxPolicyRequest,
        CLOSED_SANDBOX_POLICY.to_dict(),
        object(),
    ],
)
def test_preflight_rejects_untyped_requests(invalid: object):
    with pytest.raises(SandboxPolicyValidationError, match="request"):
        preflight_sandbox_policy(invalid)  # type: ignore[arg-type]


def test_preflight_rejects_untyped_capabilities():
    with pytest.raises(SandboxPolicyValidationError, match="capabilities"):
        preflight_sandbox_policy(CLOSED_SANDBOX_POLICY, {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"egress_modes": []},
        {"egress_modes": ()},
        {"egress_modes": ("deny", 1)},
        {"egress_modes": ("deny", "future-mode")},
        {"snapshots_enabled": 0},
        {"snapshot_scrub_required": 1},
        {"snapshot_scrub_required": False},
        {"schema_version": 2},
        {"schema_version": True},
    ],
)
def test_capability_contract_requires_exact_immutable_versioned_types(kwargs: dict[str, object]):
    with pytest.raises(SandboxPolicyValidationError):
        SandboxPolicyCapabilities(**kwargs)  # type: ignore[arg-type]


def test_capability_codec_roundtrip_digest_and_strict_fields():
    request = future_request()
    capabilities = SandboxPolicyCapabilities(
        egress_modes=("deny", "allow_all"),
        secret_delivery_modes=("none", "references"),
        secret_references=request.secret_delivery.references,
        device_access_modes=("deny", "allowlist"),
        device_requests=request.device_access.devices,
        audit_modes=("required", "disabled"),
        snapshots_enabled=True,
        snapshot_scrub_required=True,
        binary_policy_modes=("no_hooks", "evaluate"),
        binary_policy_hooks=request.binary_policy.hooks,
    )

    assert SandboxPolicyCapabilities.from_dict(capabilities.to_dict()) == capabilities
    assert SandboxPolicyCapabilities.from_json_bytes(capabilities.to_json_bytes()) == capabilities
    assert capabilities.digest == "sha256:" + hashlib.sha256(capabilities.to_json_bytes()).hexdigest()

    for mutation in (
        lambda value: value.pop("audit_modes"),
        lambda value: value.update({"future": True}),
        lambda value: value.update({"schema_version": 2}),
    ):
        invalid = capabilities.to_dict()
        mutation(invalid)
        with pytest.raises(SandboxPolicyValidationError):
            SandboxPolicyCapabilities.from_dict(invalid)

    with pytest.raises(SandboxPolicyValidationError, match="canonical"):
        SandboxPolicyCapabilities.from_json_bytes(json.dumps(capabilities.to_dict(), indent=2).encode())


def test_capability_json_rejects_duplicate_fields():
    payload = PHASE1_SANDBOX_CAPABILITIES.to_json_bytes()
    duplicate = payload[:-1] + b',"schema_version":1}'

    with pytest.raises(SandboxPolicyValidationError, match="duplicate"):
        SandboxPolicyCapabilities.from_json_bytes(duplicate)


@pytest.mark.parametrize(
    "parser",
    [
        SandboxPolicyCapabilities.from_json_bytes,
        PolicyPreflightResult.from_json_bytes,
    ],
)
def test_auxiliary_codecs_share_size_depth_and_nonreflecting_input_limits(parser):
    with pytest.raises(SandboxPolicyValidationError, match="size"):
        parser(b"x" * (policy_module.MAX_POLICY_JSON_BYTES + 1))
    with pytest.raises(SandboxPolicyValidationError):
        parser(("[" * 2000 + "]" * 2000).encode())


def test_auxiliary_from_dict_codecs_normalize_recursive_input_to_typed_errors():
    recursive: dict[str, object] = {}
    recursive["self"] = recursive

    with pytest.raises(SandboxPolicyValidationError, match="nesting"):
        SandboxPolicyCapabilities.from_dict(recursive)
    with pytest.raises(SandboxPolicyValidationError, match="nesting"):
        PolicyPreflightResult.from_dict(recursive)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"supported": 1, "issues": ()},
        {"supported": True, "issues": []},
        {"supported": True, "issues": (PolicyPreflightIssue(PolicyPreflightCode.UNSUPPORTED_EGRESS, "egress"),)},
        {"supported": False, "issues": ()},
        {"supported": True, "issues": (), "schema_version": 2},
    ],
)
def test_preflight_result_cannot_represent_contradictory_or_untyped_state(kwargs: dict[str, object]):
    with pytest.raises(SandboxPolicyValidationError):
        PolicyPreflightResult(**kwargs)  # type: ignore[arg-type]


def test_preflight_result_issues_have_canonical_set_semantics():
    first = PolicyPreflightIssue(PolicyPreflightCode.UNSUPPORTED_EGRESS, "egress")
    second = PolicyPreflightIssue(PolicyPreflightCode.UNSUPPORTED_AUDIT, "audit")

    result = PolicyPreflightResult(supported=False, issues=(second, first, second))

    assert result.issues == (first, second)
    assert result.to_json_bytes() == canonical_policy_json_bytes(result.to_dict())


@pytest.mark.parametrize(
    ("code", "field"),
    [
        (PolicyPreflightCode.UNSUPPORTED_EGRESS, "audit"),
        (PolicyPreflightCode.UNSUPPORTED_SECRET_DELIVERY, "device_access"),
        (PolicyPreflightCode.UNSUPPORTED_DEVICE_ACCESS, "binary_policy"),
        (PolicyPreflightCode.UNSUPPORTED_AUDIT, "egress"),
        (PolicyPreflightCode.UNSUPPORTED_SNAPSHOT, "binary_policy"),
        (PolicyPreflightCode.UNSAFE_SNAPSHOT_SCRUB, "audit"),
        (PolicyPreflightCode.UNSUPPORTED_BINARY_POLICY, "snapshot_scrub"),
    ],
)
def test_preflight_issue_code_has_one_exact_field(code: PolicyPreflightCode, field: str):
    with pytest.raises(SandboxPolicyValidationError):
        PolicyPreflightIssue(code, field)


def test_preflight_result_codec_roundtrip_digest_and_strict_issue_parser():
    result = preflight_sandbox_policy(future_request())

    assert PolicyPreflightResult.from_dict(result.to_dict()) == result
    assert PolicyPreflightResult.from_json_bytes(result.to_json_bytes()) == result
    assert result.digest == "sha256:" + hashlib.sha256(result.to_json_bytes()).hexdigest()

    for mutation in (
        lambda value: value.pop("supported"),
        lambda value: value.update({"future": True}),
        lambda value: value.update({"schema_version": 2}),
        lambda value: value["issues"][0].update({"future": True}),
        lambda value: value["issues"][0].update({"field": "audit"}),
        lambda value: value["issues"][0].update({"code": "future_code"}),
    ):
        invalid = result.to_dict()
        mutation(invalid)
        with pytest.raises(SandboxPolicyValidationError):
            PolicyPreflightResult.from_dict(invalid)

    with pytest.raises(SandboxPolicyValidationError, match="canonical"):
        PolicyPreflightResult.from_json_bytes(json.dumps(result.to_dict(), indent=2).encode())


def test_preflight_result_json_rejects_duplicate_fields():
    payload = preflight_sandbox_policy(future_request()).to_json_bytes()
    duplicate = payload[:-1] + b',"supported":false}'

    with pytest.raises(SandboxPolicyValidationError, match="duplicate"):
        PolicyPreflightResult.from_json_bytes(duplicate)


def test_ambiguous_policy_identifiers_are_rejected():
    with pytest.raises(SandboxPolicyValidationError, match="policy_id"):
        BinaryPolicy(
            "evaluate",
            (
                BinaryPolicyHook("same-policy", sha256("a")),
                BinaryPolicyHook("same-policy", sha256("b")),
            ),
        )
    with pytest.raises(SandboxPolicyValidationError, match="allocation_id"):
        DeviceAccessPolicy(
            "allowlist",
            (
                DeviceRequest("gpu", "same-allocation"),
                DeviceRequest("fpga", "same-allocation"),
            ),
        )


def test_snapshot_enablement_can_never_disable_required_scrubbing():
    with pytest.raises(SandboxPolicyValidationError, match="scrub"):
        SnapshotScrubPolicy(enabled=True, scrub_required=False)


def test_binary_policy_closed_default_means_no_external_hooks_not_binary_execution_denial():
    policy = BinaryPolicy(mode="no_hooks", hooks=())

    assert policy == BinaryPolicy()
    assert policy.to_dict() == {"hooks": [], "mode": "no_hooks"}
    assert preflight_sandbox_policy(replace(CLOSED_SANDBOX_POLICY, binary_policy=policy)).supported is True


def test_policy_module_has_no_infrastructure_or_side_effect_imports():
    source_path = Path(policy_module.__file__ or "")
    tree = ast.parse(source_path.read_text())
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not imports.intersection(
        {
            "asyncio",
            "libvirt",
            "os",
            "pathlib",
            "requests",
            "socket",
            "subprocess",
            "urllib",
        }
    )


def test_policy_module_exposes_no_enforcement_execution_or_mutation_entry_points():
    forbidden = ("apply", "enforce", "execute", "launch", "mount", "persist", "run", "write")
    public_functions = {
        node.name
        for node in ast.parse(Path(policy_module.__file__ or "").read_text()).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }

    assert all(not any(word in function_name for word in forbidden) for function_name in public_functions)


def test_declared_public_exports_are_present_and_do_not_include_sensitive_helpers():
    assert set(policy_module.__all__) <= set(dir(policy_module))
    assert all("secret_value" not in name and "credential" not in name for name in policy_module.__all__)
