"""Pure standard OCI image graph contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_image import (
    MAX_IMAGE_JSON_BYTES,
    MAX_IMAGE_LAYERS,
    RESERVED_PATH_POLICY_ID,
    OCIImageRef,
    resolve_image,
)
from palimpsest_local.oci_provenance import (
    DOCKER_IMAGE_CONFIG_MEDIA_TYPE,
    DOCKER_IMAGE_MANIFEST_MEDIA_TYPE,
    DOCKER_LAYER_GZIP_MEDIA_TYPE,
    DOCKER_MANIFEST_LIST_MEDIA_TYPE,
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    Descriptor,
)

DIFF_A = "sha256:" + "a" * 64
DIFF_B = "sha256:" + "b" * 64


class Graph:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.reads: list[str] = []

    def add(self, value: object, media_type: str) -> Descriptor:
        payload = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        self.blobs[digest] = payload
        return Descriptor(media_type=media_type, digest=digest, size=len(payload))

    def read(self, descriptor: Descriptor) -> bytes:
        self.reads.append(descriptor.digest)
        return self.blobs[descriptor.digest]


def ref() -> OCIImageRef:
    return OCIImageRef(
        registry="registry.example.com",
        repository="team/app",
        requested_reference="registry.example.com/team/app:stable",
    )


def image_graph(
    *,
    docker: bool = False,
    layer_count: int = 1,
    architecture: str = "amd64",
    os_name: str = "linux",
    rootfs_type: str = "layers",
    diff_ids: list[str] | None = None,
    layer_media_type: str | None = None,
) -> tuple[Graph, Descriptor, Descriptor, list[Descriptor]]:
    graph = Graph()
    config_media_type = DOCKER_IMAGE_CONFIG_MEDIA_TYPE if docker else OCI_IMAGE_CONFIG_MEDIA_TYPE
    manifest_media_type = DOCKER_IMAGE_MANIFEST_MEDIA_TYPE if docker else OCI_IMAGE_MANIFEST_MEDIA_TYPE
    selected_layer_type = layer_media_type or (DOCKER_LAYER_GZIP_MEDIA_TYPE if docker else OCI_LAYER_GZIP_MEDIA_TYPE)
    ids = diff_ids if diff_ids is not None else [f"sha256:{index + 1:064x}" for index in range(layer_count)]
    config = graph.add(
        {
            "architecture": architecture,
            "os": os_name,
            "rootfs": {"type": rootfs_type, "diff_ids": ids},
            "config": {"Env": ["A=B"]},
        },
        config_media_type,
    )
    layers = [graph.add(f"layer-{index}".encode(), selected_layer_type) for index in range(layer_count)]
    manifest = graph.add(
        {
            "schemaVersion": 2,
            "mediaType": manifest_media_type,
            "config": config.to_dict(),
            "layers": [layer.to_dict() for layer in layers],
        },
        manifest_media_type,
    )
    return graph, manifest, config, layers


def add_index(
    graph: Graph,
    manifests: list[tuple[Descriptor, dict[str, object] | None]],
    *,
    docker: bool = False,
) -> Descriptor:
    media_type = DOCKER_MANIFEST_LIST_MEDIA_TYPE if docker else OCI_IMAGE_INDEX_MEDIA_TYPE
    entries: list[dict[str, object]] = []
    for descriptor, platform in manifests:
        entry: dict[str, object] = descriptor.to_dict()
        if platform is not None:
            entry["platform"] = platform
        entries.append(entry)
    return graph.add({"schemaVersion": 2, "mediaType": media_type, "manifests": entries}, media_type)


def exact_platform() -> dict[str, object]:
    return {"os": "linux", "architecture": "amd64"}


@pytest.mark.parametrize(
    "docker,expected_manifest,expected_config,expected_layer",
    [
        (False, OCI_IMAGE_MANIFEST_MEDIA_TYPE, OCI_IMAGE_CONFIG_MEDIA_TYPE, OCI_LAYER_GZIP_MEDIA_TYPE),
        (True, DOCKER_IMAGE_MANIFEST_MEDIA_TYPE, DOCKER_IMAGE_CONFIG_MEDIA_TYPE, DOCKER_LAYER_GZIP_MEDIA_TYPE),
    ],
)
def test_direct_manifest_resolves_source_only_receipt(
    docker: bool, expected_manifest: str, expected_config: str, expected_layer: str
) -> None:
    graph, manifest, _, _ = image_graph(docker=docker, layer_count=2, diff_ids=[DIFF_A, DIFF_B])

    image = resolve_image(ref(), manifest, graph.read)

    assert image.index_descriptor is None
    assert image.manifest_descriptor.media_type == expected_manifest
    assert image.config.descriptor.media_type == expected_config
    assert [layer.compressed.media_type for layer in image.layers] == [expected_layer, expected_layer]
    assert [(layer.ordinal, layer.diff_id) for layer in image.layers] == [(0, DIFF_A), (1, DIFF_B)]
    assert image.reserved_path_policy == RESERVED_PATH_POLICY_ID
    assert image.source.to_dict()["repository"] == "team/app"
    assert image.digest.startswith("sha256:")


@pytest.mark.parametrize("docker", [False, True])
def test_index_selects_one_exact_linux_amd64_manifest(docker: bool) -> None:
    graph, manifest, _, _ = image_graph(docker=docker)
    arm_graph, arm_manifest, _, _ = image_graph(docker=docker, architecture="arm64")
    graph.blobs.update(arm_graph.blobs)
    index = add_index(
        graph,
        [
            (arm_manifest, {"os": "linux", "architecture": "arm64", "variant": "v8"}),
            (manifest, exact_platform()),
        ],
        docker=docker,
    )

    image = resolve_image(ref(), index, graph.read)

    assert image.index_descriptor == index
    assert image.manifest_descriptor == manifest
    assert arm_manifest.digest not in graph.reads


@pytest.mark.parametrize(
    "platform",
    [
        None,
        {"os": "linux", "architecture": "arm64"},
        {"os": "windows", "architecture": "amd64"},
        {"os": "linux", "architecture": "amd64", "variant": "v1"},
        {"os": "linux", "architecture": "amd64", "os.version": "6.8"},
        {"os": "linux", "architecture": "amd64", "os.features": ["sse4"]},
    ],
)
def test_index_rejects_missing_exact_linux_amd64(platform: dict[str, object] | None) -> None:
    graph, manifest, _, _ = image_graph()
    index = add_index(graph, [(manifest, platform)])

    with pytest.raises(ArtifactValidationError, match="no exact linux/amd64"):
        resolve_image(ref(), index, graph.read)


def test_index_rejects_malformed_platform_and_ambiguous_matches() -> None:
    graph, manifest, _, _ = image_graph()
    malformed = add_index(graph, [(manifest, {"os": "linux"})])
    with pytest.raises(ArtifactValidationError, match="platform is malformed"):
        resolve_image(ref(), malformed, graph.read)

    ambiguous = add_index(graph, [(manifest, exact_platform()), (manifest, exact_platform())])
    with pytest.raises(ArtifactValidationError, match="ambiguous"):
        resolve_image(ref(), ambiguous, graph.read)


@pytest.mark.parametrize("architecture,os_name", [("arm64", "linux"), ("amd64", "windows")])
def test_selected_manifest_config_must_prove_linux_amd64(architecture: str, os_name: str) -> None:
    graph, manifest, _, _ = image_graph(architecture=architecture, os_name=os_name)
    index = add_index(graph, [(manifest, exact_platform())])

    with pytest.raises(ArtifactValidationError, match="exactly linux/amd64"):
        resolve_image(ref(), index, graph.read)


@pytest.mark.parametrize("layer_count", [1, 2, 64, MAX_IMAGE_LAYERS])
def test_layer_boundaries_and_order_are_preserved(layer_count: int) -> None:
    graph, manifest, _, layers = image_graph(layer_count=layer_count)

    image = resolve_image(ref(), manifest, graph.read)

    assert [item.ordinal for item in image.layers] == list(range(layer_count))
    assert [item.compressed for item in image.layers] == layers


@pytest.mark.parametrize("layer_count", [0, MAX_IMAGE_LAYERS + 1])
def test_layer_count_outside_bounds_is_rejected_before_layer_reads(layer_count: int) -> None:
    graph, manifest, _, layers = image_graph(layer_count=layer_count)

    with pytest.raises(ArtifactValidationError, match="between 1 and 128"):
        resolve_image(ref(), manifest, graph.read)
    assert all(layer.digest not in graph.reads for layer in layers)


def test_repeated_compressed_descriptor_remains_two_occurrences_and_layers_are_never_read() -> None:
    graph, manifest, config, layers = image_graph(layer_count=1, diff_ids=[DIFF_A])
    repeated_manifest = graph.add(
        {
            "schemaVersion": 2,
            "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            "config": replace(config).to_dict(),
            "layers": [layers[0].to_dict(), layers[0].to_dict()],
        },
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    config_payload = {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [DIFF_A, DIFF_B]},
    }
    repeated_config = graph.add(config_payload, OCI_IMAGE_CONFIG_MEDIA_TYPE)
    raw_manifest = json.loads(graph.blobs[repeated_manifest.digest])
    raw_manifest["config"] = repeated_config.to_dict()
    repeated_manifest = graph.add(raw_manifest, OCI_IMAGE_MANIFEST_MEDIA_TYPE)

    image = resolve_image(ref(), repeated_manifest, graph.read)

    assert [item.compressed.digest for item in image.layers] == [layers[0].digest, layers[0].digest]
    assert [item.diff_id for item in image.layers] == [DIFF_A, DIFF_B]
    assert layers[0].digest not in graph.reads


@pytest.mark.parametrize("diff_ids", [[], [DIFF_A, DIFF_B]])
def test_diff_id_count_must_match_layer_occurrences(diff_ids: list[str]) -> None:
    graph, manifest, _, _ = image_graph(layer_count=1, diff_ids=diff_ids)
    with pytest.raises(ArtifactValidationError, match="DiffID count"):
        resolve_image(ref(), manifest, graph.read)


@pytest.mark.parametrize("diff_id", ["sha256:ABC", "sha512:" + "a" * 128, "sha256:" + "g" * 64])
def test_diff_ids_must_be_canonical_sha256(diff_id: str) -> None:
    graph, manifest, _, _ = image_graph(diff_ids=[diff_id])
    with pytest.raises(ArtifactValidationError, match="canonical sha256"):
        resolve_image(ref(), manifest, graph.read)


@pytest.mark.parametrize(
    "media_type",
    [
        "application/vnd.oci.image.layer.nondistributable.v1.tar",
        "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
        "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
        "application/example.layer",
    ],
)
def test_unsupported_layer_media_types_fail_closed(media_type: str) -> None:
    graph, manifest, _, _ = image_graph(layer_media_type=media_type)
    with pytest.raises(ArtifactValidationError, match="unsupported layer media type"):
        resolve_image(ref(), manifest, graph.read)


def test_uncompressed_oci_layer_media_type_is_supported() -> None:
    graph, manifest, _, _ = image_graph(layer_media_type=OCI_LAYER_MEDIA_TYPE)
    assert resolve_image(ref(), manifest, graph.read).layers[0].compressed.media_type == OCI_LAYER_MEDIA_TYPE


def test_schema_media_type_and_role_substitution_are_rejected() -> None:
    graph, manifest, _, _ = image_graph()
    document = json.loads(graph.blobs[manifest.digest])
    document["schemaVersion"] = 1
    schema1 = graph.add(document, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    with pytest.raises(ArtifactValidationError, match="schemaVersion 2"):
        resolve_image(ref(), schema1, graph.read)

    document["schemaVersion"] = 2
    document["mediaType"] = DOCKER_IMAGE_MANIFEST_MEDIA_TYPE
    mismatched = graph.add(document, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    with pytest.raises(ArtifactValidationError, match="does not match"):
        resolve_image(ref(), mismatched, graph.read)

    wrong_root = replace(manifest, media_type=OCI_IMAGE_CONFIG_MEDIA_TYPE)
    with pytest.raises(ArtifactValidationError, match="root descriptor"):
        resolve_image(ref(), wrong_root, graph.read)


@pytest.mark.parametrize(
    "payload",
    [b"{", b"\xff", b'{"schemaVersion":2,"schemaVersion":2}'],
)
def test_malformed_invalid_utf8_and_duplicate_key_json_are_rejected(payload: bytes) -> None:
    graph = Graph()
    root = graph.add(payload, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    with pytest.raises(ArtifactValidationError, match="strict UTF-8 JSON|duplicate object key"):
        resolve_image(ref(), root, graph.read)


def test_missing_size_and_digest_mismatch_are_rejected() -> None:
    graph, manifest, _, _ = image_graph()
    missing_reader = lambda descriptor: graph.blobs["sha256:" + "f" * 64]  # noqa: E731
    with pytest.raises(ArtifactValidationError, match="missing referenced blob"):
        resolve_image(ref(), manifest, missing_reader)

    with pytest.raises(ArtifactValidationError, match="size mismatch"):
        resolve_image(ref(), replace(manifest, size=manifest.size + 1), graph.read)

    wrong_digest = replace(manifest, digest="sha256:" + "f" * 64)
    graph.blobs[wrong_digest.digest] = graph.blobs[manifest.digest]
    with pytest.raises(ArtifactValidationError, match="digest mismatch"):
        resolve_image(ref(), wrong_digest, graph.read)


def test_descriptor_rejects_bool_negative_and_overflow_size() -> None:
    for size in (True, -1, 2**63):
        with pytest.raises(ArtifactValidationError, match="exact integer"):
            Descriptor(media_type=OCI_IMAGE_MANIFEST_MEDIA_TYPE, digest="sha256:" + "a" * 64, size=size)


def test_receipt_is_immutable_canonical_and_has_no_source_or_runtime_path() -> None:
    graph, manifest, _, _ = image_graph(diff_ids=[DIFF_A])
    first = resolve_image(ref(), manifest, graph.read)
    second = resolve_image(ref(), manifest, graph.read)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest == second.digest
    assert b"/tmp" not in first.canonical_bytes()
    assert "derived" not in first.to_dict()
    assert "runtime" not in first.to_dict()
    with pytest.raises(FrozenInstanceError):
        first.reserved_path_policy = "changed"  # type: ignore[misc]


def test_occurrence_order_changes_receipt_identity() -> None:
    graph, manifest, _, layers = image_graph(layer_count=2, diff_ids=[DIFF_A, DIFF_B])
    first = resolve_image(ref(), manifest, graph.read)
    raw = json.loads(graph.blobs[manifest.digest])
    raw["layers"] = [layers[1].to_dict(), layers[0].to_dict()]
    swapped_manifest = graph.add(raw, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    second = resolve_image(ref(), swapped_manifest, graph.read)

    assert [item.compressed for item in second.layers] == [layers[1], layers[0]]
    assert first.digest != second.digest


def test_digest_pinned_requested_reference_must_match_root_descriptor() -> None:
    graph, manifest, _, _ = image_graph()
    matching = OCIImageRef(
        registry="registry.example.com",
        repository="team/app",
        requested_reference=f"registry.example.com/team/app@{manifest.digest}",
    )
    assert resolve_image(matching, manifest, graph.read).manifest_descriptor == manifest

    mismatched = replace(matching, requested_reference="registry.example.com/team/app@sha256:" + "f" * 64)
    with pytest.raises(ArtifactValidationError, match="requested reference digest"):
        resolve_image(mismatched, manifest, graph.read)


def test_json_size_limit_is_enforced_before_reader_call() -> None:
    graph, manifest, _, _ = image_graph()
    oversized = replace(manifest, size=MAX_IMAGE_JSON_BYTES + 1)
    called = False

    def reader(descriptor: Descriptor) -> bytes:
        nonlocal called
        called = True
        return graph.read(descriptor)

    with pytest.raises(ArtifactValidationError, match="JSON limit"):
        resolve_image(ref(), oversized, reader)
    assert called is False


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numeric_constants_are_rejected(constant: str) -> None:
    graph = Graph()
    payload = (
        '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",'
        f'"future":{constant},"config":{{}},"layers":[]}}'
    ).encode()
    manifest = graph.add(payload, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    with pytest.raises(ArtifactValidationError, match="non-JSON numeric constant"):
        resolve_image(ref(), manifest, graph.read)


def test_unknown_oci_extensions_do_not_break_identity_selection() -> None:
    graph, manifest, _, _ = image_graph(diff_ids=[DIFF_A])
    raw_manifest = json.loads(graph.blobs[manifest.digest])
    raw_manifest["futureTopLevel"] = {"version": 2}
    raw_manifest["config"]["futureDescriptorField"] = ["ignored"]
    config_digest = raw_manifest["config"]["digest"]
    raw_config = json.loads(graph.blobs[config_digest])
    raw_config["rootfs"]["futureRootfsField"] = True
    new_config = graph.add(raw_config, OCI_IMAGE_CONFIG_MEDIA_TYPE)
    raw_manifest["config"].update(new_config.to_dict())
    manifest = graph.add(raw_manifest, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    index = add_index(graph, [(manifest, {**exact_platform(), "futurePlatformField": "ignored"})])
    raw_index = json.loads(graph.blobs[index.digest])
    raw_index["manifests"][0]["futureDescriptorField"] = {"ignored": True}
    index = graph.add(raw_index, OCI_IMAGE_INDEX_MEDIA_TYPE)

    image = resolve_image(ref(), index, graph.read)

    assert image.manifest_descriptor == manifest
    assert image.config.descriptor == new_config


def test_embedded_descriptor_data_is_an_explicit_unsupported_subset() -> None:
    graph, manifest, _, _ = image_graph()
    raw = json.loads(graph.blobs[manifest.digest])
    raw["config"]["data"] = "e30="
    manifest = graph.add(raw, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    with pytest.raises(ArtifactValidationError, match="unsupported embedded descriptor data"):
        resolve_image(ref(), manifest, graph.read)


@pytest.mark.parametrize(
    "manifest_type,config_type,layer_type",
    [
        (OCI_IMAGE_MANIFEST_MEDIA_TYPE, DOCKER_IMAGE_CONFIG_MEDIA_TYPE, OCI_LAYER_GZIP_MEDIA_TYPE),
        (OCI_IMAGE_MANIFEST_MEDIA_TYPE, OCI_IMAGE_CONFIG_MEDIA_TYPE, DOCKER_LAYER_GZIP_MEDIA_TYPE),
        (DOCKER_IMAGE_MANIFEST_MEDIA_TYPE, OCI_IMAGE_CONFIG_MEDIA_TYPE, DOCKER_LAYER_GZIP_MEDIA_TYPE),
        (DOCKER_IMAGE_MANIFEST_MEDIA_TYPE, DOCKER_IMAGE_CONFIG_MEDIA_TYPE, OCI_LAYER_GZIP_MEDIA_TYPE),
    ],
)
def test_oci_and_docker_wire_profiles_cannot_be_mixed(manifest_type: str, config_type: str, layer_type: str) -> None:
    graph, manifest, _, _ = image_graph()
    raw = json.loads(graph.blobs[manifest.digest])
    raw["mediaType"] = manifest_type
    raw["config"]["mediaType"] = config_type
    raw["layers"][0]["mediaType"] = layer_type
    mixed = graph.add(raw, manifest_type)
    with pytest.raises(ArtifactValidationError, match="different wire profile"):
        resolve_image(ref(), mixed, graph.read)


def test_index_and_selected_manifest_wire_profiles_cannot_be_mixed() -> None:
    graph, manifest, _, _ = image_graph(docker=True)
    mixed = add_index(graph, [(manifest, exact_platform())], docker=False)
    with pytest.raises(ArtifactValidationError, match="different wire profiles"):
        resolve_image(ref(), mixed, graph.read)
