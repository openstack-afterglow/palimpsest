"""Unit contracts for pure OCI source-to-runtime provenance."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import palimpsest_local.oci_provenance as provenance_module
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_provenance import (
    EMPTY_CONFIG_DESCRIPTOR,
    OCI_EMPTY_CONFIG_BYTES,
    OCI_EMPTY_CONFIG_DIGEST,
    OCI_EMPTY_CONFIG_MEDIA_TYPE,
    OCI_EMPTY_CONFIG_SIZE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    PROVENANCE_ARTIFACT_TYPE,
    PROVENANCE_BLOB_MEDIA_TYPE,
    PROVENANCE_DIGEST_ANNOTATION,
    Descriptor,
    LayerOccurrence,
    Platform,
    ProvenanceReferrerManifest,
    ProvenanceSource,
    SourceDerivedProvenance,
    build_provenance_referrer_manifest,
    canonical_json_bytes,
    parse_provenance_referrer_manifest,
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


def descriptor(character: str, *, media_type: str = "application/vnd.example.content", size: int = 10) -> Descriptor:
    return Descriptor(media_type=media_type, digest=digest(character), size=size)


def occurrence(
    ordinal: int,
    *,
    compressed_digest: str | None = None,
    diff_id: str | None = None,
    derived_digest: str | None = None,
) -> LayerOccurrence:
    return LayerOccurrence(
        ordinal=ordinal,
        compressed=Descriptor(
            "application/vnd.oci.image.layer.v1.tar+gzip",
            compressed_digest or digest(str(ordinal + 1)),
            100 + ordinal,
        ),
        diff_id=diff_id or digest(chr(ord("a") + ordinal)),
        derived=Descriptor(
            "application/vnd.pieroot.palimpsest.layer.squashfs.v1",
            derived_digest or digest(chr(ord("d") + ordinal)),
            80 + ordinal,
        ),
        filesystem="squashfs",
        backend="dev.pieroot.palimpsest.artifact-backend.squashfs-v1",
        converter="mksquashfs-4.6.1@sha256:" + "e" * 64,
    )


def sample_provenance(*, occurrences: tuple[LayerOccurrence, ...] | None = None) -> SourceDerivedProvenance:
    return SourceDerivedProvenance(
        source=ProvenanceSource(
            registry="registry-1.docker.io",
            repository="library/alpine",
            requested_reference="alpine:3.20",
            index_descriptor=descriptor("1", media_type="application/vnd.oci.image.index.v1+json", size=901),
            manifest_descriptor=descriptor("2", media_type=OCI_IMAGE_MANIFEST_MEDIA_TYPE, size=702),
            config_descriptor=descriptor("3", media_type="application/vnd.oci.image.config.v1+json", size=503),
            platform=Platform(os="linux", architecture="amd64", variant=None),
        ),
        occurrences=occurrences if occurrences is not None else (occurrence(0), occurrence(1)),
        runtime_index=descriptor("4", media_type="application/vnd.pieroot.palimpsest.runtime-index.v1+json", size=304),
    )


def decode(payload: bytes) -> dict[str, object]:
    value = json.loads(payload)
    assert isinstance(value, dict)
    return value


def encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def test_canonical_reproducibility_and_digest_are_over_exact_utf8_bytes():
    first = sample_provenance()
    second = sample_provenance()

    assert first.to_json_bytes() == second.to_json_bytes()
    assert first.to_json_bytes() == canonical_json_bytes(first.to_dict())
    assert first.digest == "sha256:" + hashlib.sha256(first.to_json_bytes()).hexdigest()
    assert b" " not in first.to_json_bytes()
    assert SourceDerivedProvenance.from_json_bytes(first.to_json_bytes()) == first


def test_contract_types_are_frozen_and_nested_collections_are_immutable():
    value = sample_provenance()

    with pytest.raises(FrozenInstanceError):
        value.schema_version = 2  # type: ignore[misc]
    assert isinstance(value.occurrences, tuple)


@pytest.mark.parametrize(
    ("media_type", "value_digest", "size"),
    [
        ("", digest("1"), 1),
        ("not-a-media-type", digest("1"), 1),
        ("application/example", "1" * 64, 1),
        ("application/example", "sha256:" + "A" * 64, 1),
        ("application/example", "sha512:" + "1" * 64, 1),
        ("application/example", digest("1"), -1),
        ("application/example", digest("1"), True),
        ("application/example", digest("1"), 1.0),
    ],
)
def test_descriptor_strictly_rejects_every_invalid_field(media_type: str, value_digest: str, size: object):
    with pytest.raises(ArtifactValidationError):
        Descriptor(media_type=media_type, digest=value_digest, size=size)  # type: ignore[arg-type]


def test_descriptor_parser_rejects_missing_unknown_and_namespace_substitution():
    valid = descriptor("1").to_dict()

    for invalid in (
        {"digest": valid["digest"], "size": valid["size"]},
        {**valid, "annotations": {}},
        {"media_type": valid["mediaType"], "digest": valid["digest"], "size": valid["size"]},
    ):
        with pytest.raises(ArtifactValidationError, match="fields"):
            Descriptor.from_dict(invalid)

    with pytest.raises(ArtifactValidationError, match="field"):
        Descriptor.from_dict({1: "non-string key", **valid})


def test_descriptor_media_type_restricted_names_are_canonical_lowercase_and_max_127_characters():
    descriptor_value = Descriptor("APPLICATION/VND.EXAMPLE.CONTENT+JSON", digest("1"), 1)

    assert descriptor_value.media_type == "application/vnd.example.content+json"
    assert Descriptor("a" * 127 + "/" + "b" * 127, digest("1"), 1).media_type == ("a" * 127 + "/" + "b" * 127)
    for invalid in ("a" * 128 + "/b", "a/" + "b" * 128):
        with pytest.raises(ArtifactValidationError, match="media_type"):
            Descriptor(invalid, digest("1"), 1)


def test_media_type_case_equivalence_cannot_create_distinct_provenance_digests():
    canonical = sample_provenance()
    uppercase_manifest = Descriptor(
        "APPLICATION/VND.OCI.IMAGE.MANIFEST.V1+JSON",
        canonical.source.manifest_descriptor.digest,
        canonical.source.manifest_descriptor.size,
    )
    equivalent = replace(canonical, source=replace(canonical.source, manifest_descriptor=uppercase_manifest))

    assert uppercase_manifest == canonical.source.manifest_descriptor
    assert equivalent.to_json_bytes() == canonical.to_json_bytes()
    assert equivalent.digest == canonical.digest


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("registry", "https://registry-1.docker.io"),
        ("registry", "user:password@registry.example"),
        ("registry", "/var/lib/registry"),
        ("repository", "Library/Alpine"),
        ("repository", "/library/alpine"),
        ("repository", "library//alpine"),
        ("requested_reference", "https://user:pass@example.test/image:tag"),
        ("requested_reference", "user:pass@registry.example/library/alpine:3.20"),
        ("requested_reference", "token=super-secret"),
        ("requested_reference", "/tmp/private/layout"),
        ("requested_reference", "oci-layout:///opt/private/layout"),
    ],
)
def test_source_rejects_credentials_paths_and_bad_namespaces(field_name: str, bad_value: str):
    valid = sample_provenance().source

    with pytest.raises(ArtifactValidationError):
        replace(valid, **{field_name: bad_value})


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"os": "", "architecture": "amd64"}, "platform.os"),
        ({"os": "Linux", "architecture": "amd64"}, "platform.os"),
        ({"os": "linux", "architecture": "amd 64"}, "platform.architecture"),
        ({"os": "linux", "architecture": "amd64", "variant": "/v8"}, "platform.variant"),
    ],
)
def test_platform_rejects_invalid_fields(kwargs: dict[str, object], message: str):
    with pytest.raises(ArtifactValidationError, match=message):
        Platform(**kwargs)  # type: ignore[arg-type]


def test_platform_requires_linux_and_canonicalizes_optional_oci_os_fields():
    platform = Platform(
        os="linux",
        architecture="amd64",
        variant=None,
        os_version="6.8.12",
        os_features=("z-feature", "a-feature", "z-feature"),
    )

    assert platform.os_version == "6.8.12"
    assert platform.os_features == ("a-feature", "z-feature")
    assert platform.to_dict() == {
        "architecture": "amd64",
        "os": "linux",
        "os_features": ["a-feature", "z-feature"],
        "os_version": "6.8.12",
        "variant": None,
    }
    assert Platform.from_dict(platform.to_dict()) == platform


@pytest.mark.parametrize(
    "kwargs",
    [
        {"os": "darwin", "architecture": "amd64"},
        {"os": "linux", "architecture": "amd64", "os_version": ""},
        {"os": "linux", "architecture": "amd64", "os_features": ["feature"]},
        {"os": "linux", "architecture": "amd64", "os_features": ("",)},
        {"os": "linux", "architecture": "amd64", "os_features": (7,)},
    ],
)
def test_platform_rejects_non_linux_and_invalid_optional_oci_os_fields(kwargs: dict[str, object]):
    with pytest.raises(ArtifactValidationError, match="platform"):
        Platform(**kwargs)  # type: ignore[arg-type]


def test_platform_parser_strictly_rejects_missing_and_unknown_optional_field_names():
    valid = Platform("linux", "amd64").to_dict()
    missing = dict(valid)
    missing.pop("os_features")
    unknown = {**valid, "features": []}

    for payload in (missing, unknown):
        with pytest.raises(ArtifactValidationError, match="fields"):
            Platform.from_dict(payload)


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("ordinal", True),
        ("ordinal", -1),
        ("diff_id", "a" * 64),
        ("diff_id", "sha256:" + "A" * 64),
        ("filesystem", "/tmp/squashfs"),
        ("backend", "password=hunter2"),
        ("converter", "Bearer abcdefghijklmnop"),
        ("converter", "tool /Users/alice/private/converter"),
        ("converter", "tool=/opt/palimpsest/converter"),
        ("converter", "tool=C:\\private\\converter.exe"),
        ("backend", "vm-123e4567-e89b-12d3-a456-426614174000"),
    ],
)
def test_occurrence_rejects_invalid_fields_and_values_outside_typed_id_grammars(field_name: str, bad_value: object):
    with pytest.raises(ArtifactValidationError):
        replace(occurrence(0), **{field_name: bad_value})


def test_schema_version_requires_exact_int_not_bool_or_float_in_constructor_and_json():
    value = sample_provenance()

    for invalid in (True, 1.0):
        with pytest.raises(ArtifactValidationError, match="schema_version"):
            replace(value, schema_version=invalid)

    payload = decode(value.to_json_bytes())
    payload["schema_version"] = 1.0
    with pytest.raises(ArtifactValidationError, match="schema_version"):
        SourceDerivedProvenance.from_json_bytes(encode(payload))


def test_descriptor_size_rejects_values_larger_than_signed_64_bit():
    with pytest.raises(ArtifactValidationError, match="size"):
        Descriptor("application/example", digest("1"), 2**63)

    assert Descriptor("application/example", digest("1"), 2**63 - 1).size == 2**63 - 1


@pytest.mark.parametrize(
    "registry",
    [
        "localhost",
        "localhost:5000",
        "registry-1.docker.io",
        "123",
        "127.0.0.1:443",
        "[2001:db8::1]",
        "[2001:db8::1]:5000",
    ],
)
def test_registry_authority_accepts_hostname_localhost_ip_and_valid_port(registry: str):
    assert replace(sample_provenance().source, registry=registry).registry == registry


@pytest.mark.parametrize(
    "registry",
    [
        "registry..example",
        ".registry.example",
        "registry.example.",
        "-registry.example",
        "registry-.example",
        "registry_example",
        "256.1.1.1",
        "2001:db8::1",
        "[not-ipv6]",
        "[fe80::1%eth0]",
        "localhost:0",
        "localhost:65536",
        "localhost:notaport",
        "localhost/path",
        "user@localhost",
    ],
)
def test_registry_authority_rejects_malformed_hosts_ports_userinfo_and_paths(registry: str):
    with pytest.raises(ArtifactValidationError, match="registry"):
        replace(sample_provenance().source, registry=registry)


@pytest.mark.parametrize(
    ("registry", "canonical"),
    [
        ("REGISTRY.Example:05000", "registry.example:5000"),
        ("LOCALHOST:00443", "localhost:443"),
        ("[2001:0DB8:0:0:0:0:0:1]:05000", "[2001:db8::1]:5000"),
    ],
)
def test_registry_authority_is_canonicalized(registry: str, canonical: str):
    assert replace(sample_provenance().source, registry=registry).registry == canonical


@pytest.mark.parametrize("repository", ["foo__bar", "foo--bar", "foo_bar/baz.qux", "a/b/c"])
def test_repository_accepts_distribution_path_component_grammar(repository: str):
    assert replace(sample_provenance().source, repository=repository).repository == repository


@pytest.mark.parametrize(
    "repository",
    ["", "foo//bar", "foo/", "foo___bar", "foo..bar", "foo--", "Foo/bar", "foo bar"],
)
def test_repository_rejects_non_distribution_grammar(repository: str):
    with pytest.raises(ArtifactValidationError, match="repository"):
        replace(sample_provenance().source, repository=repository)


def test_distribution_grammar_does_not_invent_a_255_character_repository_limit():
    repository = "a" * 300
    source = replace(sample_provenance().source, repository=repository, requested_reference=repository)

    assert source.repository == repository
    assert source.requested_reference == repository


@pytest.mark.parametrize(
    "reference",
    [
        "alpine",
        "alpine:3.20",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "library/alpine:3.20",
        "registry.example/foo__bar/baz--qux:tag",
        "localhost:5000/team/image:123e4567-e89b-12d3-a456-426614174000",
        "[2001:db8::1]:5000/team/image:latest",
        "alpine@" + digest("a"),
        "docker.io/library/alpine:3.20@" + digest("a"),
    ],
)
def test_requested_reference_accepts_real_named_reference_tag_and_digest_forms(reference: str):
    assert replace(sample_provenance().source, requested_reference=reference).requested_reference == reference


@pytest.mark.parametrize(
    "reference",
    [
        "",
        " alpine:latest",
        "alpine latest",
        "docker://alpine:latest",
        "../layout/image",
        "~/.cache/image",
        "user@registry.example/team/image",
        "user:pass@registry.example/team/image",
        "registry..example/team/image",
        "registry.example:0/team/image",
        "registry.example/team//image",
        "registry.example/team/image:",
        "registry.example/team/image@sha256:" + "A" * 64,
        "registry.example/team/image@sha256:short",
        "registry.example/team/image@sha512:" + "a" * 64,
    ],
)
def test_requested_reference_rejects_non_reference_credentials_paths_and_bad_digest(reference: str):
    with pytest.raises(ArtifactValidationError, match="requested_reference"):
        replace(sample_provenance().source, requested_reference=reference)


@pytest.mark.parametrize(
    ("reference", "canonical"),
    [
        ("REGISTRY.Example:05000/team/image:Tag", "registry.example:5000/team/image:Tag"),
        ("LOCALHOST/team/image:latest", "localhost/team/image:latest"),
        (
            "[2001:0DB8:0:0:0:0:0:1]:05000/team/image:latest",
            "[2001:db8::1]:5000/team/image:latest",
        ),
    ],
)
def test_requested_reference_canonicalizes_only_explicit_registry_authority(reference: str, canonical: str):
    assert replace(sample_provenance().source, requested_reference=reference).requested_reference == canonical


def test_equivalent_registry_authorities_produce_identical_provenance_bytes_and_digest():
    original = sample_provenance()
    noncanonical = replace(
        original,
        source=replace(
            original.source,
            registry="REGISTRY-1.DOCKER.IO:00443",
            requested_reference="REGISTRY-1.DOCKER.IO:00443/library/alpine:3.20",
        ),
    )
    canonical = replace(
        original,
        source=replace(
            original.source,
            registry="registry-1.docker.io:443",
            requested_reference="registry-1.docker.io:443/library/alpine:3.20",
        ),
    )

    assert noncanonical.to_json_bytes() == canonical.to_json_bytes()
    assert noncanonical.digest == canonical.digest


def test_descriptor_roles_reject_media_type_substitution_for_every_identity():
    value = sample_provenance()
    wrong = descriptor("f", media_type="application/vnd.example.wrong")

    for field_name in ("manifest_descriptor", "index_descriptor", "config_descriptor"):
        with pytest.raises(ArtifactValidationError, match="media type"):
            replace(value.source, **{field_name: wrong})

    with pytest.raises(ArtifactValidationError, match="compressed"):
        replace(value, occurrences=(replace(value.occurrences[0], compressed=wrong), value.occurrences[1]))
    with pytest.raises(ArtifactValidationError, match="derived"):
        replace(value, occurrences=(replace(value.occurrences[0], derived=wrong), value.occurrences[1]))
    with pytest.raises(ArtifactValidationError, match="runtime_index"):
        replace(value, runtime_index=wrong)


def test_descriptor_roles_accept_supported_oci_and_docker_media_types():
    value = sample_provenance()
    docker_source = replace(
        value.source,
        manifest_descriptor=replace(
            value.source.manifest_descriptor,
            media_type="application/vnd.docker.distribution.manifest.v2+json",
        ),
        index_descriptor=replace(
            value.source.index_descriptor,
            media_type="application/vnd.docker.distribution.manifest.list.v2+json",
        ),
        config_descriptor=replace(
            value.source.config_descriptor,
            media_type="application/vnd.docker.container.image.v1+json",
        ),
    )
    assert docker_source.manifest_descriptor.media_type.startswith("application/vnd.docker.")

    compressed_media_types = (
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
    )
    for media_type in compressed_media_types:
        assert replace(occurrence(0), compressed=replace(occurrence(0).compressed, media_type=media_type))

    assert replace(
        occurrence(0),
        filesystem="erofs",
        backend="dev.pieroot.palimpsest.artifact-backend.erofs-v1",
        derived=replace(
            occurrence(0).derived,
            media_type="application/vnd.pieroot.palimpsest.layer.erofs.v1",
        ),
        converter="mkfs.erofs@" + digest("e"),
    )


def test_typed_occurrence_profiles_accept_exact_squashfs_and_erofs_bindings():
    squashfs = occurrence(0)
    erofs = replace(
        squashfs,
        filesystem="erofs",
        backend="dev.pieroot.palimpsest.artifact-backend.erofs-v1",
        derived=replace(
            squashfs.derived,
            media_type="application/vnd.pieroot.palimpsest.layer.erofs.v1",
        ),
        converter="mkfs.erofs-1.8.4@" + digest("b"),
    )

    assert squashfs.filesystem == "squashfs"
    assert erofs.filesystem == "erofs"


@pytest.mark.parametrize(
    "changes",
    [
        {"filesystem": "erofs"},
        {"backend": "dev.pieroot.palimpsest.artifact-backend.erofs-v1"},
        {"derived_media_type": "application/vnd.pieroot.palimpsest.layer.erofs.v1"},
        {"converter": "mkfs.erofs@" + digest("a")},
    ],
)
def test_typed_occurrence_profiles_reject_contradictory_cross_field_combinations(changes: dict[str, object]):
    value = occurrence(0)
    applied_changes = dict(changes)
    derived_media_type = applied_changes.pop("derived_media_type", None)
    if derived_media_type is not None:
        applied_changes["derived"] = replace(value.derived, media_type=derived_media_type)  # type: ignore[arg-type]

    with pytest.raises(ArtifactValidationError, match="profile"):
        replace(value, **applied_changes)


@pytest.mark.parametrize(
    ("field_name", "invalid"),
    [
        ("filesystem", "ext4"),
        ("filesystem", "squashfs-v4"),
        ("backend", "squashfs-v1"),
        ("backend", "dev.pieroot.palimpsest.artifact-backend."),
        ("backend", "dev.pieroot.palimpsest.artifact-backend.Bad"),
        ("converter", "../private/tool"),
        ("converter", "password=secret"),
        ("converter", "vm-42"),
        ("converter", "tool/name@" + digest("a")),
        ("converter", "tool=name@" + digest("a")),
        ("converter", "tool name@" + digest("a")),
        ("converter", "tool@sha256:" + "A" * 64),
    ],
)
def test_typed_occurrence_identity_grammars_reject_free_form_values(field_name: str, invalid: str):
    with pytest.raises(ArtifactValidationError):
        replace(occurrence(0), **{field_name: invalid})


def test_repeated_descriptor_occurrences_are_preserved_not_deduplicated():
    repeated_compressed = digest("9")
    repeated_diff_id = digest("8")
    repeated_derived = digest("7")
    first = occurrence(
        0,
        compressed_digest=repeated_compressed,
        diff_id=repeated_diff_id,
        derived_digest=repeated_derived,
    )
    occurrences = (
        first,
        replace(first, ordinal=1),
    )

    value = sample_provenance(occurrences=occurrences)
    parsed = SourceDerivedProvenance.from_json_bytes(value.to_json_bytes())

    assert len(parsed.occurrences) == 2
    assert parsed.occurrences[0].compressed == parsed.occurrences[1].compressed
    assert parsed.occurrences[0].ordinal == 0
    assert parsed.occurrences[1].ordinal == 1


@pytest.mark.parametrize(
    "ordinals",
    [
        (1,),
        (0, 0),
        (0, 2),
        (1, 0),
    ],
)
def test_occurrence_ordinal_gap_duplicate_and_order_are_rejected(ordinals: tuple[int, ...]):
    with pytest.raises(ArtifactValidationError, match="contiguous"):
        sample_provenance(occurrences=tuple(occurrence(value) for value in ordinals))


def test_occurrences_must_be_an_immutable_tuple():
    with pytest.raises(ArtifactValidationError, match="immutable tuple"):
        SourceDerivedProvenance(
            source=sample_provenance().source,
            occurrences=[occurrence(0)],  # type: ignore[arg-type]
            runtime_index=sample_provenance().runtime_index,
        )


def test_compressed_digest_and_diff_id_are_distinct_and_not_substituted():
    value = sample_provenance(occurrences=(occurrence(0, compressed_digest=digest("1"), diff_id=digest("2")),))
    payload = decode(value.to_json_bytes())
    item = payload["occurrences"][0]  # type: ignore[index]

    assert item["compressed"]["digest"] == digest("1")  # type: ignore[index]
    assert item["diff_id"] == digest("2")  # type: ignore[index]


def test_chain_id_is_sensitive_to_every_tuple_field_and_order():
    original = sample_provenance(occurrences=(occurrence(0), occurrence(1)))
    changes = (
        (occurrence(0, compressed_digest=digest("9")), occurrence(1)),
        (occurrence(0, diff_id=digest("9")), occurrence(1)),
        (occurrence(0, derived_digest=digest("9")), occurrence(1)),
        (
            replace(occurrence(1), ordinal=0),
            replace(occurrence(0), ordinal=1),
        ),
    )

    assert all(sample_provenance(occurrences=items).chain_id != original.chain_id for items in changes)


def test_provenance_digest_is_sensitive_beyond_chain_identity():
    original = sample_provenance()
    changed_source = replace(original, source=replace(original.source, requested_reference="alpine:latest"))
    changed_runtime = replace(original, runtime_index=replace(original.runtime_index, digest=digest("9")))
    changed_converter = replace(
        original,
        occurrences=(
            replace(original.occurrences[0], converter="mksquashfs-4.7@" + digest("e")),
            original.occurrences[1],
        ),
    )

    assert changed_source.chain_id == original.chain_id
    assert changed_runtime.chain_id == original.chain_id
    assert changed_converter.chain_id == original.chain_id
    assert {changed_source.digest, changed_runtime.digest, changed_converter.digest}.isdisjoint({original.digest})


def test_declared_chain_id_tampering_is_rejected():
    value = sample_provenance()
    payload = decode(value.to_json_bytes())
    payload["chain_id"] = digest("0")

    with pytest.raises(ArtifactValidationError, match="chain_id mismatch"):
        SourceDerivedProvenance.from_json_bytes(encode(payload))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"unknown": True}),
        lambda payload: payload.pop("runtime_index"),
        lambda payload: payload["source"].update({"password": "secret"}),  # type: ignore[union-attr]
        lambda payload: payload["occurrences"][0].update({"run_id": "vm-42"}),  # type: ignore[index,union-attr]
        lambda payload: payload["source"]["platform"].update({"features": []}),  # type: ignore[index,union-attr]
    ],
)
def test_provenance_parser_rejects_unknown_and_missing_fields(mutation):
    payload = decode(sample_provenance().to_json_bytes())
    mutation(payload)

    with pytest.raises(ArtifactValidationError):
        SourceDerivedProvenance.from_json_bytes(encode(payload))


def test_provenance_parser_rejects_noncanonical_duplicate_and_invalid_utf8_json():
    value = sample_provenance()

    with pytest.raises(ArtifactValidationError, match="canonical"):
        SourceDerivedProvenance.from_json_bytes(json.dumps(value.to_dict()).encode())
    with pytest.raises(ArtifactValidationError, match="duplicate"):
        SourceDerivedProvenance.from_json_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ArtifactValidationError, match="UTF-8"):
        SourceDerivedProvenance.from_json_bytes(b"\xff")


def test_optional_index_and_platform_variant_round_trip_strictly():
    value = sample_provenance()
    value = replace(
        value, source=replace(value.source, index_descriptor=None, platform=Platform("linux", "arm64", "v8"))
    )

    payload = decode(value.to_json_bytes())
    assert payload["source"]["index_descriptor"] is None  # type: ignore[index]
    assert payload["source"]["platform"]["variant"] == "v8"  # type: ignore[index]
    assert SourceDerivedProvenance.from_json_bytes(value.to_json_bytes()) == value


def test_runtime_index_is_explicit_caller_content_identity_with_no_run_id_field():
    value = sample_provenance()
    payload = value.to_json_bytes()

    assert decode(payload)["runtime_index"] == value.runtime_index.to_dict()
    assert b"run_id" not in payload
    assert b"vm_id" not in payload
    assert b"instance_id" not in payload


def test_empty_config_constants_are_the_exact_oci_empty_json_descriptor():
    assert OCI_EMPTY_CONFIG_BYTES == b"{}"
    assert OCI_EMPTY_CONFIG_SIZE == len(OCI_EMPTY_CONFIG_BYTES) == 2
    assert OCI_EMPTY_CONFIG_DIGEST == "sha256:" + hashlib.sha256(OCI_EMPTY_CONFIG_BYTES).hexdigest()
    assert EMPTY_CONFIG_DESCRIPTOR == Descriptor(
        media_type=OCI_EMPTY_CONFIG_MEDIA_TYPE,
        digest=OCI_EMPTY_CONFIG_DIGEST,
        size=2,
    )


def test_referrer_is_an_oci_image_manifest_with_exact_subject_artifact_and_blob_descriptor():
    value = sample_provenance()
    manifest_bytes = build_provenance_referrer_manifest(value)
    manifest = decode(manifest_bytes)

    assert manifest["schemaVersion"] == 2
    assert manifest["mediaType"] == OCI_IMAGE_MANIFEST_MEDIA_TYPE
    assert manifest["artifactType"] == PROVENANCE_ARTIFACT_TYPE
    assert manifest["config"] == EMPTY_CONFIG_DESCRIPTOR.to_dict()
    assert manifest["subject"] == value.source.manifest_descriptor.to_dict()
    assert manifest["layers"] == [
        {
            "mediaType": PROVENANCE_BLOB_MEDIA_TYPE,
            "digest": value.digest,
            "size": len(value.to_json_bytes()),
        }
    ]
    assert manifest["annotations"] == {PROVENANCE_DIGEST_ANNOTATION: value.digest}


def test_referrer_round_trip_accepts_unknown_string_annotations_without_interpreting_values():
    value = sample_provenance()
    manifest_bytes = build_provenance_referrer_manifest(
        value,
        annotations={
            "com.example.note": "an arbitrary interoperable value",
            "org.opencontainers.image.title": "provenance",
        },
    )

    parsed = parse_provenance_referrer_manifest(
        manifest_bytes,
        value.to_json_bytes(),
        expected_subject=value.source.manifest_descriptor,
    )

    assert dict(parsed.annotations)["com.example.note"] == "an arbitrary interoperable value"
    assert dict(parsed.annotations)["org.opencontainers.image.title"] == "provenance"
    assert parsed.to_json_bytes() == manifest_bytes


def test_referrer_consumer_preserves_unknown_reserved_and_unusual_string_annotations():
    value = sample_provenance()
    manifest = decode(build_provenance_referrer_manifest(value))
    manifest["annotations"] = {
        "": "",
        "org.opencontainers.future.reserved": "\n\x00unusual value",
        "UPPER CASE / vendor key": "any JSON string",
    }
    manifest_bytes = encode(manifest)

    parsed = parse_provenance_referrer_manifest(manifest_bytes, value.to_json_bytes())

    assert dict(parsed.annotations) == manifest["annotations"]
    assert parsed.to_json_bytes() == manifest_bytes


@pytest.mark.parametrize(
    "reserved_key",
    [
        "org.opencontainers.future.reserved",
        "org.opencontainers.image.not-predefined",
    ],
)
def test_referrer_builder_rejects_unknown_reserved_oci_annotations(reserved_key: str):
    with pytest.raises(ArtifactValidationError, match="annotation"):
        build_provenance_referrer_manifest(sample_provenance(), annotations={reserved_key: "value"})


def test_referrer_builder_accepts_custom_string_annotations_without_enforcing_reverse_domain_guidance():
    value = sample_provenance()
    annotations = {
        "": "",
        "not-reverse-domain": "value",
        "Com.Example.Key": "UPPER CASE remains a JSON string",
    }

    manifest_bytes = build_provenance_referrer_manifest(value, annotations=annotations)
    parsed = parse_provenance_referrer_manifest(manifest_bytes, value.to_json_bytes())

    for key, annotation_value in annotations.items():
        assert dict(parsed.annotations)[key] == annotation_value


def test_referrer_builder_rejects_non_string_annotation_value():
    with pytest.raises(ArtifactValidationError, match="annotation"):
        build_provenance_referrer_manifest(
            sample_provenance(),
            annotations={"com.example.key": 7},  # type: ignore[dict-item]
        )


def test_referrer_builder_allows_whitelisted_standard_title_and_custom_extensions():
    manifest = decode(
        build_provenance_referrer_manifest(
            sample_provenance(),
            annotations={
                "org.opencontainers.image.title": "provenance",
                "com.example.provenance.note": "extension",
            },
        )
    )

    assert manifest["annotations"]["org.opencontainers.image.title"] == "provenance"  # type: ignore[index]
    assert manifest["annotations"]["com.example.provenance.note"] == "extension"  # type: ignore[index]


@pytest.mark.parametrize(
    "standard_key",
    [
        "org.opencontainers.image.created",
        "org.opencontainers.image.authors",
        "org.opencontainers.image.url",
        "org.opencontainers.image.documentation",
        "org.opencontainers.image.source",
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.vendor",
        "org.opencontainers.image.licenses",
        "org.opencontainers.image.ref.name",
        "org.opencontainers.image.title",
        "org.opencontainers.image.description",
        "org.opencontainers.image.base.digest",
        "org.opencontainers.image.base.name",
    ],
)
def test_referrer_builder_accepts_every_predefined_oci_image_annotation(standard_key: str):
    value = sample_provenance()
    manifest_bytes = build_provenance_referrer_manifest(value, annotations={standard_key: "value"})

    assert decode(manifest_bytes)["annotations"][standard_key] == "value"  # type: ignore[index]
    assert parse_provenance_referrer_manifest(manifest_bytes, value.to_json_bytes())


def test_caller_provenance_digest_annotation_must_match_even_when_auto_annotation_is_disabled():
    value = sample_provenance()

    with pytest.raises(ArtifactValidationError, match="conflicts"):
        build_provenance_referrer_manifest(
            value,
            include_digest_annotation=False,
            annotations={PROVENANCE_DIGEST_ANNOTATION: digest("0")},
        )

    manifest_bytes = build_provenance_referrer_manifest(
        value,
        include_digest_annotation=False,
        annotations={PROVENANCE_DIGEST_ANNOTATION: value.digest},
    )
    assert parse_provenance_referrer_manifest(manifest_bytes, value.to_json_bytes())


def test_referrer_digest_annotation_is_optional():
    value = sample_provenance()
    manifest_bytes = build_provenance_referrer_manifest(value, include_digest_annotation=False)

    assert "annotations" not in decode(manifest_bytes)
    assert parse_provenance_referrer_manifest(manifest_bytes, value.to_json_bytes()).to_json_bytes() == manifest_bytes


@pytest.mark.parametrize(
    ("field_path", "replacement"),
    [
        (("schemaVersion",), 1),
        (("schemaVersion",), 2.0),
        (("mediaType",), "application/vnd.oci.artifact.manifest.v1+json"),
        (("artifactType",), "application/vnd.example.wrong"),
        (("config", "digest"), digest("0")),
        (("config", "size"), 3),
        (("subject", "digest"), digest("0")),
        (("layers", 0, "digest"), digest("0")),
        (("layers", 0, "size"), 0),
        (("layers", 0, "mediaType"), "application/vnd.example.wrong"),
        (("annotations", PROVENANCE_DIGEST_ANNOTATION), digest("0")),
    ],
)
def test_referrer_parser_rejects_all_identity_and_descriptor_tampering(
    field_path: tuple[object, ...], replacement: object
):
    value = sample_provenance()
    manifest = decode(build_provenance_referrer_manifest(value))
    target = manifest
    for component in field_path[:-1]:
        target = target[component]  # type: ignore[index,assignment]
    target[field_path[-1]] = replacement  # type: ignore[index]

    with pytest.raises(ArtifactValidationError):
        parse_provenance_referrer_manifest(encode(manifest), value.to_json_bytes())


def test_referrer_parser_rejects_extra_top_level_fields_and_multiple_layers():
    value = sample_provenance()
    manifest = decode(build_provenance_referrer_manifest(value))
    manifest["urls"] = ["https://example.test"]

    with pytest.raises(ArtifactValidationError, match="unknown"):
        parse_provenance_referrer_manifest(encode(manifest), value.to_json_bytes())

    manifest = decode(build_provenance_referrer_manifest(value))
    manifest["layers"].append(manifest["layers"][0])  # type: ignore[union-attr,index]
    with pytest.raises(ArtifactValidationError, match="exactly one"):
        parse_provenance_referrer_manifest(encode(manifest), value.to_json_bytes())


def test_referrer_parser_rejects_a_different_expected_subject():
    value = sample_provenance()

    with pytest.raises(ArtifactValidationError, match="subject"):
        parse_provenance_referrer_manifest(
            build_provenance_referrer_manifest(value),
            value.to_json_bytes(),
            expected_subject=replace(value.source.manifest_descriptor, digest=digest("9")),
        )


def test_referrer_model_rejects_non_image_manifest_subject_media_type():
    value = sample_provenance()
    with pytest.raises(ArtifactValidationError, match="subject.*media type"):
        ProvenanceReferrerManifest(
            subject=descriptor("f", media_type="application/vnd.example.wrong"),
            provenance_blob=Descriptor(PROVENANCE_BLOB_MEDIA_TYPE, value.digest, len(value.to_json_bytes())),
        )


def test_referrer_parser_rejects_noncanonical_manifest_and_changed_provenance_bytes():
    value = sample_provenance()
    manifest_bytes = build_provenance_referrer_manifest(value)

    with pytest.raises(ArtifactValidationError):
        parse_provenance_referrer_manifest(json.dumps(decode(manifest_bytes)).encode(), value.to_json_bytes())

    changed = replace(value, runtime_index=replace(value.runtime_index, digest=digest("9")))
    with pytest.raises(ArtifactValidationError, match="descriptor"):
        parse_provenance_referrer_manifest(manifest_bytes, changed.to_json_bytes())


def test_custom_annotation_cannot_override_the_provenance_digest():
    value = sample_provenance()

    with pytest.raises(ArtifactValidationError, match="conflicts"):
        build_provenance_referrer_manifest(
            value,
            annotations={PROVENANCE_DIGEST_ANNOTATION: digest("0")},
        )


def test_module_import_graph_has_no_network_process_runtime_or_mutating_filesystem_dependencies():
    source_path = Path(provenance_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    relative_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative_modules.add(node.module or "")
            elif node.module:
                imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= {
        "__future__",
        "collections",
        "dataclasses",
        "hashlib",
        "ipaddress",
        "json",
        "re",
        "typing",
    }
    assert relative_modules == {"digest", "errors"}
    forbidden_named_calls = {
        "open",
        "exec",
        "eval",
        "compile",
        "system",
        "run",
        "Popen",
        "urlopen",
        "socket",
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
    }
    forbidden_attribute_calls = forbidden_named_calls - {"compile"}
    called_names = {
        node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_attributes = {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not forbidden_named_calls.intersection(called_names)
    assert not forbidden_attribute_calls.intersection(called_attributes)
