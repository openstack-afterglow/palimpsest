"""Behavioral archive validation: original identities, graph edges and limits."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import zstandard

from palimpsest_hub.services.hub_bundle import ANNOTATION_CONFIG_DIGEST
from palimpsest_hub.services.hub_store import (
    DISK_FORMAT_MEDIA_TYPES,
    MEDIA_TYPE_LAYER_CONFIG,
    MEDIA_TYPE_LAYER_SQUASHFS,
)
from palimpsest_hub.services.package_contents import (
    CACHE_CONFIG,
    OCI_CONFIG,
    OCI_INDEX,
    OCI_MANIFEST,
    PackageContentError,
    PackageContentLimitError,
    validate_cache_archive,
    validate_package_archive,
)

RAW = "application/vnd.oci.image.layer.v1.tar"
GZIP = RAW + "+gzip"
ZSTD = RAW + "+zstd"
LIMIT = 8 * 1024 * 1024


def encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class Layout:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.roots: list[dict] = []

    def add(self, payload: bytes | dict, media: str) -> dict:
        data = encoded(payload) if isinstance(payload, dict) else payload
        identity = digest(data)
        self.blobs[identity] = data
        return {"digest": identity, "mediaType": media, "size": len(data)}

    def image(
        self,
        *,
        media: str = GZIP,
        architecture: str = "amd64",
        config_patch: dict | None = None,
        layer_payload: bytes | None = None,
    ) -> tuple[dict, dict, dict]:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo("sentinel")
            info.size = 7
            archive.addfile(info, io.BytesIO(b"content"))
        plain = stream.getvalue() if layer_payload is None else layer_payload
        compressed = (
            gzip.compress(plain, mtime=0)
            if media == GZIP
            else zstandard.ZstdCompressor().compress(plain)
            if media == ZSTD
            else plain
        )
        layer = self.add(compressed, media)
        config_value = {
            "os": "linux",
            "architecture": architecture,
            "rootfs": {"type": "layers", "diff_ids": [digest(plain)]},
            "history": [{"created_by": "fixture"}],
            "config": {
                "Entrypoint": ["/bin/sh"],
                "Cmd": ["-c", "true"],
                "Env": ["A=B"],
                "User": "1000:1000",
                "WorkingDir": "/",
            },
        }
        config_value.update(config_patch or {})
        config = self.add(config_value, OCI_CONFIG)
        root = self.add(
            {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": config, "layers": [layer]}, OCI_MANIFEST
        )
        self.roots.append(root)
        return root, config, layer

    def members(self) -> dict[str, bytes]:
        return {
            "oci-layout": encoded({"imageLayoutVersion": "1.0.0"}),
            "index.json": encoded({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": self.roots}),
            **{"blobs/sha256/" + identity[7:]: value for identity, value in self.blobs.items()},
        }

    def archive(
        self,
        path: Path,
        *,
        extra: list[tuple[tarfile.TarInfo, bytes | None]] | None = None,
        members: dict[str, bytes] | None = None,
    ) -> Path:
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
            for name, value in (self.members() if members is None else members).items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            for info, value in extra or []:
                archive.addfile(info, io.BytesIO(value) if value is not None else None)
        return path


def validate(path: Path, stage: Path, root: dict, *, kind: str = "oci-image", expanded: int = LIMIT, blob: int = LIMIT):
    return validate_package_archive(
        path, stage, package_type=kind, root_digest=root["digest"], max_blob_bytes=blob, max_expanded_bytes=expanded
    )


@pytest.mark.parametrize("media", [RAW, GZIP, ZSTD])
def test_preserves_original_graph_bytes_and_compressed_identities(tmp_path: Path, media: str) -> None:
    layout = Layout()
    root, config, layer = layout.image(media=media)
    archive = layout.archive(tmp_path / "image.tar")
    result = validate(archive, tmp_path / "stage", root)
    assert result.root_digest == root["digest"]
    assert result.root_media_type == OCI_MANIFEST
    assert result.platforms == [{"os": "linux", "architecture": "amd64"}]
    assert set(result.graph) == {root["digest"], config["digest"], layer["digest"]}
    assert result.total_bytes == sum(len(layout.blobs[key]) for key in result.graph)
    for identity, path in result.blob_paths.items():
        assert path.read_bytes() == layout.blobs[identity]
        assert digest(path.read_bytes()) == identity
        assert path.parent.parent == tmp_path / "stage"


def test_buildkit_layout_directories_are_accepted_but_other_directories_are_not(tmp_path: Path) -> None:
    layout = Layout()
    root, _, _ = layout.image()
    directories = []
    for name in ("blobs", "blobs/sha256"):
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        directories.append((info, None))
    # BuildKit's OCI exporter writes the layout directories before their blobs.
    with tarfile.open(tmp_path / "buildkit.tar", "w", format=tarfile.PAX_FORMAT) as archive:
        for info, _ in directories:
            archive.addfile(info)
        for name, value in layout.members().items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    assert validate(tmp_path / "buildkit.tar", tmp_path / "stage", root).root_digest == root["digest"]
    unexpected = tarfile.TarInfo("extra")
    unexpected.type = tarfile.DIRTYPE
    with pytest.raises(PackageContentError, match="unexpected archive directory"):
        validate(layout.archive(tmp_path / "other.tar", extra=[(unexpected, None)]), tmp_path / "other-stage", root)


def test_explicit_root_excludes_unrelated_incomplete_graph(tmp_path: Path) -> None:
    layout = Layout()
    root, config, layer = layout.image()
    unrelated, unrelated_config, _ = layout.image(architecture="arm64")
    del layout.blobs[unrelated_config["digest"]]
    result = validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root)
    assert set(result.graph) == {root["digest"], config["digest"], layer["digest"]}
    assert unrelated["digest"] not in result.graph


def test_selected_multiplatform_index_keeps_every_child(tmp_path: Path) -> None:
    layout = Layout()
    amd, _, _ = layout.image()
    arm, _, _ = layout.image(architecture="arm64")
    children = [
        dict(amd, platform={"os": "linux", "architecture": "amd64"}),
        dict(arm, platform={"os": "linux", "architecture": "arm64"}),
    ]
    index = layout.add({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": children}, OCI_INDEX)
    layout.roots = [index]
    result = validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", index)
    assert result.root_media_type == OCI_INDEX
    assert set(result.graph) == set(layout.blobs)
    assert result.platforms == [{"os": "linux", "architecture": "amd64"}, {"os": "linux", "architecture": "arm64"}]


def test_layout_index_can_itself_be_selected_without_reencoding(tmp_path: Path) -> None:
    layout = Layout()
    layout.image()
    members = layout.members()
    identity = digest(members["index.json"])
    root = {"digest": identity}
    result = validate(layout.archive(tmp_path / "image.tar", members=members), tmp_path / "stage", root)
    assert result.blob_paths[identity].read_bytes() == members["index.json"]
    assert set(result.graph) == set(layout.blobs) | {identity}


@pytest.mark.parametrize(
    "patch,match",
    [
        ({"rootfs": {"type": "layers", "diff_ids": ["sha256:" + "0" * 64]}}, "DiffID mismatch"),
        ({"rootfs": {"type": "layers", "diff_ids": []}}, "DiffID count"),
        ({"history": [{"empty_layer": True}]}, "history nonempty layer count"),
        ({"history": [{"empty_layer": 1}]}, "invalid config history entry"),
        ({"config": {"Env": ["NOEQUALS"]}}, "NAME=value"),
        ({"config": {"Entrypoint": ["/bin/sh", 1]}}, "invalid process Entrypoint"),
        ({"config": {"ArgsEscaped": "true"}}, "ArgsEscaped"),
        ({"config": {"Cmd": "echo"}}, "Cmd must be an array"),
        ({"config": {"Labels": ["not", "a", "map"]}}, "Labels must be an object"),
    ],
)
def test_inconsistent_config_fails_before_publication_and_preserves_foreign_staging(
    tmp_path: Path, patch: dict, match: str
) -> None:
    layout = Layout()
    root, _, _ = layout.image(config_patch=patch)
    stage = tmp_path / "stage"
    stage.mkdir()
    foreign = stage / "other-operation"
    foreign.write_text("preserve")
    with pytest.raises(PackageContentError, match=match):
        validate(layout.archive(tmp_path / "image.tar"), stage, root)
    assert list(stage.iterdir()) == [foreign]
    assert foreign.read_text() == "preserve"


@pytest.mark.parametrize("mismatch", ["platform", "features", "size", "missing", "bytes"])
def test_descriptor_consistency_not_filename_alone(tmp_path: Path, mismatch: str) -> None:
    layout = Layout()
    root, config, layer = layout.image(
        config_patch={"os.features": ["different-feature"]} if mismatch == "features" else None
    )
    if mismatch == "platform":
        layout.roots = [dict(root, platform={"os": "linux", "architecture": "arm64"})]
    elif mismatch == "features":
        layout.roots = [
            dict(root, platform={"os": "linux", "architecture": "amd64", "os.features": ["requires-feature"]})
        ]
    elif mismatch == "size":
        layout.roots = [dict(root, size=root["size"] + 1)]
    elif mismatch == "missing":
        del layout.blobs[layer["digest"]]
    else:
        layout.blobs[config["digest"]] = b" " * len(layout.blobs[config["digest"]])
    with pytest.raises(PackageContentError):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/escape", tarfile.REGTYPE),
        ("symlink", tarfile.SYMTYPE),
        ("hardlink", tarfile.LNKTYPE),
        ("index.json", tarfile.REGTYPE),
    ],
)
def test_unsafe_or_duplicate_archive_members_are_rejected(tmp_path: Path, name: str, kind: bytes) -> None:
    layout = Layout()
    root, _, _ = layout.image()
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = "index.json" if kind != tarfile.REGTYPE else ""
    with pytest.raises(PackageContentError):
        validate(
            layout.archive(tmp_path / "image.tar", extra=[(info, b"" if kind == tarfile.REGTYPE else None)]),
            tmp_path / "stage",
            root,
        )
    assert not (tmp_path / "escape").exists()


def test_remote_urls_never_supply_missing_layer(tmp_path: Path) -> None:
    layout = Layout()
    old, config, layer = layout.image()
    del layout.blobs[layer["digest"]]
    root = layout.add(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": config,
            "layers": [dict(layer, urls=["https://example.invalid/layer"])],
        },
        OCI_MANIFEST,
    )
    layout.roots = [root]
    with pytest.raises(PackageContentError, match="missing"):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root)
    assert old["digest"] != root["digest"]


@pytest.mark.parametrize("media", [GZIP, ZSTD])
def test_streaming_layer_expansion_is_bounded(tmp_path: Path, media: str) -> None:
    layout = Layout()
    root, _, _ = layout.image(media=media, layer_payload=b"x" * (2 * 1024 * 1024))
    with pytest.raises(PackageContentError, match="expanded"):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root, expanded=128 * 1024)


def test_truncated_zstd_cannot_match_a_declared_prefix_diffid(tmp_path: Path) -> None:
    layout = Layout()
    root, config, layer = layout.image(media=ZSTD)
    truncated = layout.blobs[layer["digest"]][:-1]
    broken_layer = layout.add(truncated, ZSTD)
    root = layout.add(
        {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": config, "layers": [broken_layer]}, OCI_MANIFEST
    )
    layout.roots = [root]
    with pytest.raises(PackageContentError, match="zstd"):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root)


def test_duplicate_json_keys_are_rejected_even_with_correct_hash(tmp_path: Path) -> None:
    layout = Layout()
    root, _, _ = layout.image()
    members = layout.members()
    members["oci-layout"] = b'{"imageLayoutVersion":"1.0.0","imageLayoutVersion":"1.0.0"}'
    with pytest.raises(PackageContentError):
        validate(layout.archive(tmp_path / "image.tar", members=members), tmp_path / "stage", root)


def runtime_layout(
    *, parent: str | None = None, chain_id: str | None = None, base_arch: str = "x86_64", separate_base: bool = False
) -> tuple[Layout, dict, dict]:
    layout = Layout()
    base = layout.add(b"original cloud image bytes", DISK_FORMAT_MEDIA_TYPES["raw"])
    base_config = layout.add(
        {
            "blob_digest": base["digest"],
            "kind": "cloud-image",
            "disk_format": "raw",
            "arch": base_arch,
            "parent_digest": None,
        },
        MEDIA_TYPE_LAYER_CONFIG,
    )
    base_desc = dict(base, annotations={ANNOTATION_CONFIG_DIGEST: base_config["digest"]})
    layer = layout.add(b"original squashfs bytes", MEDIA_TYPE_LAYER_SQUASHFS)
    config = layout.add(
        {
            "blob_digest": layer["digest"],
            "kind": "squashfs",
            "parent_digest": parent,
            "chain_id": layer["digest"] if chain_id is None else chain_id,
            "base_image_digest": base["digest"],
            "arch": "x86_64",
        },
        MEDIA_TYPE_LAYER_CONFIG,
    )
    layer_desc = dict(layer, annotations={ANNOTATION_CONFIG_DIGEST: config["digest"]})
    root = layout.add(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": config,
            "layers": [layer_desc] if separate_base else [base_desc, layer_desc],
        },
        OCI_MANIFEST,
    )
    layout.roots = [root]
    if separate_base:
        base_root = layout.add(
            {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": base_config, "layers": [base_desc]}, OCI_MANIFEST
        )
        layout.roots.append(base_root)
    return layout, root, base


@pytest.mark.parametrize("separate_base", [False, True])
def test_runtime_base_bytes_configs_and_original_root_are_reachable(tmp_path: Path, separate_base: bool) -> None:
    layout, root, base = runtime_layout(separate_base=separate_base)
    result = validate(layout.archive(tmp_path / "runtime.tar"), tmp_path / "stage", root, kind="runtime-bundle")
    assert set(result.graph) == set(layout.blobs)
    assert result.blob_paths[root["digest"]].read_bytes() == layout.blobs[root["digest"]]
    assert result.blob_paths[base["digest"]].read_bytes() == b"original cloud image bytes"
    assert result.platforms == [{"os": "linux", "architecture": "amd64"}]


@pytest.mark.parametrize("problem", ["parent", "chain", "architecture", "missing-base"])
def test_runtime_parent_chain_and_base_invariants(tmp_path: Path, problem: str) -> None:
    layout, root, base = runtime_layout(
        parent="sha256:" + "1" * 64 if problem == "parent" else None,
        chain_id="sha256:" + "2" * 64 if problem == "chain" else None,
        base_arch="aarch64" if problem == "architecture" else "x86_64",
    )
    if problem == "missing-base":
        del layout.blobs[base["digest"]]
    with pytest.raises(PackageContentError):
        validate(layout.archive(tmp_path / "runtime.tar"), tmp_path / "stage", root, kind="runtime-bundle")


BINDING = {
    "project_id": "a" * 64,
    "namespace": "p-h-" + "b" * 56,
    "package": "application",
    "build_key": "sha256:" + "c" * 64,
    "cache_scope": "ci",
    "platform": "linux/amd64",
    "builder_fingerprint": "sha256:" + "d" * 64,
}


def cache_archive(
    path: Path,
    *,
    binding_patch: dict | None = None,
    record_patch: dict | None = None,
    parent: int = -1,
    indexed: bool = False,
) -> Path:
    layout = Layout()
    payload = gzip.compress(b"cache layer bytes", mtime=0)
    layer = layout.add(payload, GZIP)
    record = {"digest": "sha256:" + "e" * 64, "layers": [{"layer": 0}]}
    record.update(record_patch or {})
    config = layout.add(
        {
            "layers": [
                {
                    "blob": layer["digest"],
                    "parent": parent,
                    "annotations": {"diffID": digest(b"cache layer bytes"), "size": len(payload), "mediaType": GZIP},
                }
            ],
            "records": [record],
        },
        CACHE_CONFIG,
    )
    root_document = (
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [config, layer]}
        if indexed
        else {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": config, "layers": [layer]}
    )
    root = layout.add(root_document, OCI_INDEX if indexed else OCI_MANIFEST)
    layout.roots = [root]
    # Built-image provenance is deliberately unrelated to the cache export root.
    binding = {
        "schema": "palimpsest-buildkit-cache-archive-v1",
        **BINDING,
        "oci_manifest_digest": "sha256:" + "f" * 64,
        **(binding_patch or {}),
    }
    members = {
        "palimpsest-cache.json": encoded(binding),
        **{"cache/" + name: value for name, value in layout.members().items()},
    }
    return layout.archive(path, members=members)


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("image_provenance", [None, "sha256:" + "f" * 64])
def test_cache_accepts_real_graph_and_opaque_federated_project_binding(
    tmp_path: Path, indexed: bool, image_provenance: str | None
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    foreign = stage / "other-operation"
    foreign.write_bytes(b"preserved")
    validate_cache_archive(
        cache_archive(tmp_path / "cache.tar", indexed=indexed, binding_patch={"oci_manifest_digest": image_provenance}),
        stage,
        expected_binding=BINDING,
        max_blob_bytes=LIMIT,
        max_expanded_bytes=LIMIT,
    )
    assert list(stage.iterdir()) == [foreign]
    assert foreign.read_bytes() == b"preserved"


@pytest.mark.parametrize("field", list(BINDING))
def test_cache_rejects_every_foreign_input_or_owner_binding(tmp_path: Path, field: str) -> None:
    with pytest.raises(PackageContentError, match="binding mismatch"):
        validate_cache_archive(
            cache_archive(tmp_path / "cache.tar", binding_patch={field: "foreign"}),
            tmp_path / "stage",
            expected_binding=BINDING,
            max_blob_bytes=LIMIT,
            max_expanded_bytes=LIMIT,
        )


@pytest.mark.parametrize(
    "parent,record",
    [
        (0, {}),
        (9, {}),
        (-1, {"layers": [{"layer": 9}]}),
        (-1, {"inputs": [[{"link": 0}]]}),
        (-1, {"inputs": [[{"link": 9}]]}),
    ],
)
def test_cache_rejects_cycles_missing_links_and_invalid_results(tmp_path: Path, parent: int, record: dict) -> None:
    with pytest.raises(PackageContentError):
        validate_cache_archive(
            cache_archive(tmp_path / "cache.tar", parent=parent, record_patch=record),
            tmp_path / "stage",
            expected_binding=BINDING,
            max_blob_bytes=LIMIT,
            max_expanded_bytes=LIMIT,
        )


def test_cache_never_becomes_a_runtime_package(tmp_path: Path) -> None:
    layout = Layout()
    root, _, _ = layout.image()
    with pytest.raises(PackageContentError, match="unsupported package type"):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root, kind="buildkit-cache")


@pytest.mark.parametrize("compression", ["gzip", "zstd"])
def test_outer_archive_decompression_is_bounded_and_preserves_input(tmp_path: Path, compression: str) -> None:
    layout = Layout()
    root, _, _ = layout.image()
    archive = layout.archive(tmp_path / "plain.tar")
    original = archive.read_bytes()
    compressed = tmp_path / "compressed.tar"
    compressed.write_bytes(
        gzip.compress(original) if compression == "gzip" else zstandard.ZstdCompressor().compress(original)
    )
    with pytest.raises(PackageContentLimitError, match="expanded"):
        validate(compressed, tmp_path / "stage", root, expanded=1024)
    assert archive.read_bytes() == original
    assert compressed.exists()
    assert list((tmp_path / "stage").iterdir()) == []


def test_blob_limit_rejects_oversized_descriptor_before_graph_publication(tmp_path: Path) -> None:
    layout = Layout()
    root, _, _ = layout.image(media=RAW)
    with pytest.raises(PackageContentLimitError, match="blob byte limit"):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root, blob=1024)


def test_stop_signal_accepts_spec_names_and_rejects_malformed_syntax(tmp_path: Path) -> None:
    # OCI image-spec allows SIGNAME forms such as systemd's SIGRTMIN+3; policy is a run-time concern.
    accepted = Layout()
    root, _, _ = accepted.image(
        config_patch={"config": {"StopSignal": "SIGRTMIN+3", "User": "app:staff", "WorkingDir": "srv"}}
    )
    validate(accepted.archive(tmp_path / "accepted.tar"), tmp_path / "accepted", root)
    rejected = Layout()
    root, _, _ = rejected.image(config_patch={"config": {"StopSignal": "SIG TERM;"}})
    with pytest.raises(PackageContentError, match="StopSignal"):
        validate(rejected.archive(tmp_path / "rejected.tar"), tmp_path / "rejected", root)


def test_large_window_zstd_layer_verifies_diffid(tmp_path: Path) -> None:
    payload = bytes(range(256)) * (16 * 1024)  # 4 MiB, level 3 uses a multi-MiB window
    layout = Layout()
    root, _, layer = layout.image(media=ZSTD, layer_payload=payload)
    result = validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root, expanded=16 * 1024 * 1024)
    assert result.blob_paths[layer["digest"]].read_bytes() == layout.blobs[layer["digest"]]


def test_attestation_child_is_verified_but_not_a_platform(tmp_path: Path) -> None:
    layout = Layout()
    image, _, _ = layout.image()
    statement = layout.add(b'{"_type":"https://in-toto.io/Statement/v0.1"}', "application/vnd.in-toto+json")
    att_config = layout.add(
        {
            "architecture": "unknown",
            "os": "unknown",
            "config": {},
            "rootfs": {"type": "layers", "diff_ids": [statement["digest"]]},
        },
        OCI_CONFIG,
    )
    attestation = layout.add(
        {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": att_config, "layers": [statement]}, OCI_MANIFEST
    )
    attestation_entry = dict(
        attestation,
        platform={"os": "unknown", "architecture": "unknown"},
        annotations={
            "vnd.docker.reference.type": "attestation-manifest",
            "vnd.docker.reference.digest": image["digest"],
        },
    )
    index = layout.add(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [dict(image, platform={"os": "linux", "architecture": "amd64"}), attestation_entry],
        },
        OCI_INDEX,
    )
    layout.roots = [index]
    result = validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", index)
    assert result.platforms == [{"os": "linux", "architecture": "amd64"}]
    assert set(result.graph) == set(layout.blobs)


def test_tar_member_count_is_bounded_including_unselected_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from palimpsest_hub.services import package_contents

    monkeypatch.setattr(package_contents, "MAX_MEMBERS", 6)
    layout = Layout()
    root, _, _ = layout.image()
    layout.add(b"unselected one", RAW)
    layout.add(b"unselected two", RAW)
    with pytest.raises(PackageContentError, match="member limit"):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root)


def test_cache_omitted_parent_means_layer_zero_per_buildkit_omitempty(tmp_path: Path) -> None:
    layout = Layout()
    plain = [b"base cache layer", b"child cache layer"]
    layers = [layout.add(gzip.compress(item, mtime=0), GZIP) for item in plain]
    config = layout.add(
        {
            "layers": [{"blob": layers[0]["digest"], "parent": -1}, {"blob": layers[1]["digest"]}],
            "records": [
                {"digest": "sha256:" + "e" * 64, "layers": [{"layer": 1}]},
                {"digest": "sha256:" + "e" * 64, "chains": [{"layers": [0, 1]}]},
            ],
        },
        CACHE_CONFIG,
    )
    root = layout.add({"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": config, "layers": layers}, OCI_MANIFEST)
    layout.roots = [root]
    binding = {"schema": "palimpsest-buildkit-cache-archive-v1", **BINDING, "oci_manifest_digest": None}
    archive = layout.archive(
        tmp_path / "cache.tar",
        members={
            "palimpsest-cache.json": encoded(binding),
            **{"cache/" + name: value for name, value in layout.members().items()},
        },
    )
    validate_cache_archive(
        archive, tmp_path / "stage", expected_binding=BINDING, max_blob_bytes=LIMIT, max_expanded_bytes=LIMIT
    )


def test_json_depth_is_bounded_even_for_ignored_extension_fields(tmp_path: Path) -> None:
    extension: dict = {}
    for _ in range(70):
        extension = {"nested": extension}
    layout = Layout()
    root, _, _ = layout.image(config_patch={"futureExtension": extension})
    with pytest.raises(PackageContentLimitError, match="depth"):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root)


def test_descriptor_edge_budget_is_a_limit_not_a_graph_defect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from palimpsest_hub.services import package_contents

    monkeypatch.setattr(package_contents, "MAX_EDGES", 2)
    layout = Layout()
    root, _, _ = layout.image()
    with pytest.raises(PackageContentLimitError, match="edge limit"):
        validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root)


def test_arm_variant_can_come_from_descriptor_without_reencoding_config(tmp_path: Path) -> None:
    layout = Layout()
    root, config, _ = layout.image(architecture="arm64")
    layout.roots = [dict(root, platform={"os": "linux", "architecture": "arm64", "variant": "v8"})]
    result = validate(layout.archive(tmp_path / "image.tar"), tmp_path / "stage", root)
    assert result.platforms == [{"os": "linux", "architecture": "arm64", "variant": "v8"}]
    assert result.blob_paths[config["digest"]].read_bytes() == layout.blobs[config["digest"]]


def test_cache_binding_requires_namespace_not_legacy_unqualified_metadata(tmp_path: Path) -> None:
    with pytest.raises(PackageContentError, match="namespace binding"):
        validate_cache_archive(
            cache_archive(tmp_path / "cache.tar", binding_patch={"namespace": None}),
            tmp_path / "stage",
            expected_binding=BINDING,
            max_blob_bytes=LIMIT,
            max_expanded_bytes=LIMIT,
        )


def test_cache_opaque_project_identity_is_case_sensitive(tmp_path: Path) -> None:
    expected = dict(BINDING, project_id="Federated-Project")
    with pytest.raises(PackageContentError, match="project_id binding"):
        validate_cache_archive(
            cache_archive(tmp_path / "cache.tar", binding_patch={"project_id": "federated-project"}),
            tmp_path / "stage",
            expected_binding=expected,
            max_blob_bytes=LIMIT,
            max_expanded_bytes=LIMIT,
        )


@pytest.mark.parametrize("ambiguous", [False, True])
def test_runtime_local_multiroot_ancestor_configs_are_verified(tmp_path: Path, ambiguous: bool) -> None:
    layout = Layout()
    first = layout.add(b"first squashfs", MEDIA_TYPE_LAYER_SQUASHFS)
    second = layout.add(b"second squashfs", MEDIA_TYPE_LAYER_SQUASHFS)
    first_config = layout.add(
        {"kind": "squashfs", "blob_digest": first["digest"], "parent_digest": None, "chain_id": first["digest"]},
        MEDIA_TYPE_LAYER_CONFIG,
    )
    ancestor = layout.add(
        {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": first_config, "layers": [first]}, OCI_MANIFEST
    )
    chain_id = digest(f"{first['digest']} {second['digest']}".encode())
    second_config = layout.add(
        {"kind": "squashfs", "blob_digest": second["digest"], "parent_digest": first["digest"], "chain_id": chain_id},
        MEDIA_TYPE_LAYER_CONFIG,
    )
    leaf = layout.add(
        {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": second_config, "layers": [first, second]},
        OCI_MANIFEST,
    )
    layout.roots = [ancestor, leaf]
    if ambiguous:
        other_config = layout.add(
            {
                "kind": "squashfs",
                "blob_digest": first["digest"],
                "parent_digest": None,
                "chain_id": first["digest"],
                "name": "other",
            },
            MEDIA_TYPE_LAYER_CONFIG,
        )
        other_root = layout.add(
            {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": other_config, "layers": [first]}, OCI_MANIFEST
        )
        layout.roots.append(other_root)
        with pytest.raises(PackageContentError, match="unambiguous"):
            validate(layout.archive(tmp_path / "runtime.tar"), tmp_path / "stage", leaf, kind="runtime-bundle")
    else:
        result = validate(layout.archive(tmp_path / "runtime.tar"), tmp_path / "stage", leaf, kind="runtime-bundle")
        assert set(result.graph) == set(layout.blobs)
        assert result.blob_paths[ancestor["digest"]].read_bytes() == layout.blobs[ancestor["digest"]]
        assert result.blob_paths[first_config["digest"]].read_bytes() == layout.blobs[first_config["digest"]]


def test_runtime_index_platform_claim_must_match_verified_config(tmp_path: Path) -> None:
    layout, root, _ = runtime_layout()
    index = layout.add(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [dict(root, platform={"os": "linux", "architecture": "arm64"})],
        },
        OCI_INDEX,
    )
    layout.roots = [index]
    with pytest.raises(PackageContentError, match="platform mismatch"):
        validate(layout.archive(tmp_path / "runtime.tar"), tmp_path / "stage", index, kind="runtime-bundle")


def test_runtime_root_annotation_cannot_contradict_leaf_chain_id(tmp_path: Path) -> None:
    layout, old_root, _ = runtime_layout()
    manifest = json.loads(layout.blobs[old_root["digest"]])
    manifest["annotations"] = {"dev.afterglow.palimpsest.chain-id": "sha256:" + "0" * 64}
    root = layout.add(manifest, OCI_MANIFEST)
    layout.roots = [root]
    with pytest.raises(PackageContentError, match="chain-id annotation"):
        validate(layout.archive(tmp_path / "runtime.tar"), tmp_path / "stage", root, kind="runtime-bundle")
