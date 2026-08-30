"""BuildKit cache, offline resolution, and archive safety contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import palimpsest_local.buildkit as buildkit
from palimpsest_local.buildkit import (
    CACHE_ARCHIVE_SCHEMA,
    BuildKitSpec,
    NamedOCIContext,
    build_buildx_command,
    build_with_buildkit,
    compute_build_key,
    compute_context_digest,
    create_deterministic_tar,
    extract_cache_tar,
    image_arch_for_platform,
    preflight_buildx_oci_exporter,
    validate_offline_dockerfile,
    validate_online_dockerfile,
)
from palimpsest_local.digest import digest_file
from palimpsest_local.errors import DigestMismatchError, HubError, PalimpsestError, StateError
from palimpsest_local.hub import KIND_BUILDKIT_CACHE, MEDIA_TYPE_BUILDKIT_CACHE
from palimpsest_local.oci_layout import MEDIA_TYPE_LAYER_SQUASHFS, ContentStore
from palimpsest_local.state import StatePaths, init_roots, read_tag_record

D_IMAGE = "sha256:" + "a" * 64
D_OTHER_IMAGE = "sha256:" + "b" * 64
BUILDX_VERSION = "github.com/docker/buildx v0.30.1"
BUILDKIT_VERSION = "v0.32.2"


def _test_builder_fingerprint() -> str:
    payload = json.dumps(
        {
            "driver": "docker-container",
            "buildx_version": BUILDX_VERSION,
            "buildkit_versions": [BUILDKIT_VERSION],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@pytest.mark.parametrize(
    ("platform", "arch"),
    [("linux/amd64", "x86_64"), ("linux/arm64", "aarch64"), ("linux/arm64/v8", "aarch64")],
)
def test_image_arch_for_platform_uses_cloud_image_arch_names(platform: str, arch: str):
    assert image_arch_for_platform(platform) == arch


@pytest.mark.parametrize("offline", [False, True])
def test_buildkit_syntax_build_arg_cannot_override_verified_frontend(tmp_path: Path, offline: bool):
    with pytest.raises(PalimpsestError, match="BUILDKIT_SYNTAX"):
        _spec(
            tmp_path,
            "FROM scratch\n",
            build_args=("BUILDKIT_SYNTAX=docker/dockerfile:1",),
            offline=offline,
            network="none" if offline else "default",
        )


def _write_minimal_oci_layout(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8")
    (path / "index.json").write_text('{"schemaVersion":2,"manifests":[]}\n', encoding="utf-8")
    (path / "blobs" / "sha256").mkdir(parents=True)
    return path


def _add_oci_blob(layout: Path, payload: bytes) -> str:
    blobs = layout / "blobs" / "sha256"
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    (blobs / digest.split(":", 1)[1]).write_bytes(payload)
    return digest


def _add_oci_descriptor(layout: Path, payload: bytes, *, config: bytes = b"{}") -> str:
    config_digest = _add_oci_blob(layout, config)
    layer_digest = _add_oci_blob(layout, payload)
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": layer_digest,
                    "size": len(payload),
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _add_oci_blob(layout, manifest)


def _oci_descriptor(layout: Path, digest: str, media_type: str, **extensions: object) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "mediaType": media_type,
        "digest": digest,
        "size": (layout / "blobs" / "sha256" / digest.split(":", 1)[1]).stat().st_size,
    }
    descriptor.update(extensions)
    return descriptor


def _add_oci_index(layout: Path, manifests: list[dict[str, object]]) -> str:
    return _add_oci_blob(
        layout,
        json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": manifests,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


def _spec(
    tmp_path: Path,
    dockerfile_text: str = "FROM scratch\nCOPY app.txt /app.txt\n",
    **overrides: object,
) -> BuildKitSpec:
    context = tmp_path / "context"
    context.mkdir(parents=True, exist_ok=True)
    dockerfile = context / "Dockerfile"
    dockerfile.write_text(dockerfile_text, encoding="utf-8")
    (context / "app.txt").write_text("hello\n", encoding="utf-8")
    values: dict[str, object] = {
        "context": context,
        "dockerfile": dockerfile,
        "tag": "example/app:test",
        "output": tmp_path / "image.oci.tar",
        "platform": "linux/amd64",
        "network": "default",
        "cache_scope": "example-app",
    }
    values.update(overrides)
    return BuildKitSpec(**values)  # type: ignore[arg-type]


def test_context_digest_and_build_key_are_stable_and_input_sensitive(tmp_path: Path):
    spec = _spec(tmp_path)

    context_digest = compute_context_digest(spec.context)
    assert context_digest == compute_context_digest(spec.context)
    key = compute_build_key(spec)
    assert key == compute_build_key(spec)
    assert key.startswith("sha256:")

    assert compute_build_key(replace(spec, build_args=("MODE=release",))) != key
    assert compute_build_key(replace(spec, platform="linux/arm64")) != key
    assert compute_build_key(replace(spec, target="runtime")) != key
    assert compute_build_key(replace(spec, cache_scope="other-scope")) != key
    assert compute_build_key(replace(spec, runtime_base_digest=D_IMAGE)) == key
    assert compute_build_key(replace(spec, runtime_block_size=262144)) == key

    (spec.context / "app.txt").write_text("changed\n", encoding="utf-8")
    assert compute_context_digest(spec.context) != context_digest
    assert compute_build_key(spec) != key


def test_build_key_includes_pinned_named_oci_context_digest(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    first_digest = _add_oci_descriptor(layout, b"first OCI descriptor")
    second_digest = _add_oci_descriptor(layout, b"second OCI descriptor")
    first = NamedOCIContext.parse(f"base={layout}@{first_digest}")
    second = NamedOCIContext.parse(f"base={layout}@{second_digest}")
    spec = _spec(tmp_path, "FROM base\n", local_images=(first,), offline=True, network="none")

    assert compute_build_key(spec) != compute_build_key(replace(spec, local_images=(second,)))


def test_named_oci_context_accepts_direct_and_index_pins_after_shared_verifier_extraction(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    manifest_digest = _add_oci_descriptor(layout, b"opaque layer bytes")
    direct = NamedOCIContext.parse(f"direct={layout}@{manifest_digest}")
    index_digest = _add_oci_index(
        layout,
        [
            _oci_descriptor(
                layout,
                manifest_digest,
                "application/vnd.oci.image.manifest.v1+json",
                annotations={"example.invalid/retained": "yes"},
                platform={"os": "linux", "architecture": "amd64"},
                urls=["https://example.invalid/unused"],
            )
        ],
    )
    indexed = NamedOCIContext.parse(f"indexed={layout}@{index_digest}")

    assert direct.manifest_digest == manifest_digest
    assert indexed.manifest_digest == index_digest
    assert direct.buildx_value == f"oci-layout://{layout}@{manifest_digest}"
    assert indexed.buildx_value == f"oci-layout://{layout}@{index_digest}"


def test_named_oci_context_remains_platform_agnostic_for_buildkit(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "arm-layout")
    manifest_digest = _add_oci_descriptor(layout, b"arm64 opaque layer")
    index_digest = _add_oci_index(
        layout,
        [
            _oci_descriptor(
                layout,
                manifest_digest,
                "application/vnd.oci.image.manifest.v1+json",
                platform={"os": "linux", "architecture": "arm64", "variant": "v8"},
            )
        ],
    )
    local = NamedOCIContext.parse(f"base={layout}@{index_digest}")
    spec = _spec(
        tmp_path,
        "FROM base\n",
        local_images=(local,),
        platform="linux/arm64/v8",
        offline=True,
        network="none",
    )
    argv = build_buildx_command(
        spec,
        cache_from=tmp_path / "cache-in",
        cache_to=tmp_path / "cache-out",
        metadata_file=tmp_path / "metadata.json",
    )

    assert argv[argv.index("--platform") + 1] == "linux/arm64/v8"
    assert argv[argv.index("--build-context") + 1] == f"base={local.buildx_value}"


def test_named_oci_context_verifies_every_reachable_index_branch_before_solve(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "multi-platform-layout")
    selected_digest = _add_oci_descriptor(layout, b"selected layer")
    unsafe_config = json.dumps({"config": {"OnBuild": ["RUN false"]}}).encode()
    unsafe_digest = _add_oci_descriptor(layout, b"nonselected layer", config=unsafe_config)
    index_digest = _add_oci_index(
        layout,
        [
            _oci_descriptor(
                layout,
                selected_digest,
                "application/vnd.oci.image.manifest.v1+json",
                platform={"os": "linux", "architecture": "amd64"},
            ),
            _oci_descriptor(
                layout,
                unsafe_digest,
                "application/vnd.oci.image.manifest.v1+json",
                platform={"os": "linux", "architecture": "arm64"},
            ),
        ],
    )

    with pytest.raises(PalimpsestError, match="OnBuild triggers"):
        NamedOCIContext.parse(f"base={layout}@{index_digest}")


def test_named_oci_context_recurses_nested_indexes_and_rejects_deep_onbuild(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "nested-layout")
    config = json.dumps({"config": {"OnBuild": ["RUN false"]}}).encode()
    manifest_digest = _add_oci_descriptor(layout, b"deep layer", config=config)
    nested_digest = _add_oci_index(
        layout,
        [_oci_descriptor(layout, manifest_digest, "application/vnd.oci.image.manifest.v1+json")],
    )
    root_digest = _add_oci_index(
        layout,
        [_oci_descriptor(layout, nested_digest, "application/vnd.oci.image.index.v1+json")],
    )

    with pytest.raises(PalimpsestError, match="OnBuild triggers"):
        NamedOCIContext.parse(f"base={layout}@{root_digest}")


@pytest.mark.parametrize("onbuild", ["RUN false", {"command": "RUN false"}, 1, True])
def test_named_oci_context_rejects_malformed_onbuild(tmp_path: Path, onbuild: object):
    layout = _write_minimal_oci_layout(tmp_path / "malformed-onbuild")
    config = json.dumps({"config": {"OnBuild": onbuild}}).encode()
    manifest_digest = _add_oci_descriptor(layout, b"layer", config=config)

    with pytest.raises(PalimpsestError, match="malformed OnBuild"):
        NamedOCIContext.parse(f"base={layout}@{manifest_digest}")


def test_named_oci_context_keeps_empty_layer_and_docker_v2_buildkit_contract(tmp_path: Path):
    empty_layout = _write_minimal_oci_layout(tmp_path / "empty-layout")
    empty_config = b"{}"
    empty_config_digest = _add_oci_blob(empty_layout, empty_config)
    empty_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "config": {"digest": empty_config_digest, "size": len(empty_config)},
            "layers": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    empty_manifest_digest = _add_oci_blob(empty_layout, empty_manifest)
    NamedOCIContext.parse(f"empty={empty_layout}@{empty_manifest_digest}")

    docker_layout = _write_minimal_oci_layout(tmp_path / "docker-layout")
    docker_manifest_digest = _add_oci_descriptor(docker_layout, b"docker layer")
    docker_manifest_path = docker_layout / "blobs" / "sha256" / docker_manifest_digest.split(":", 1)[1]
    docker_manifest = json.loads(docker_manifest_path.read_text(encoding="utf-8"))
    docker_manifest["mediaType"] = "application/vnd.docker.distribution.manifest.v2+json"
    docker_manifest["config"]["mediaType"] = "application/vnd.docker.container.image.v1+json"
    docker_manifest["layers"][0]["mediaType"] = "application/vnd.docker.image.rootfs.diff.tar.gzip"
    docker_manifest_digest = _add_oci_blob(
        docker_layout,
        json.dumps(docker_manifest, sort_keys=True, separators=(",", ":")).encode(),
    )
    NamedOCIContext.parse(f"docker={docker_layout}@{docker_manifest_digest}")


def test_named_oci_context_rejects_same_size_digest_corruption_before_solve(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    manifest_digest = _add_oci_descriptor(layout, b"layer payload")
    manifest_path = layout / "blobs" / "sha256" / manifest_digest.split(":", 1)[1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layer_digest = manifest["layers"][0]["digest"]
    layer_path = layout / "blobs" / "sha256" / layer_digest.split(":", 1)[1]
    layer_path.write_bytes(b"other payload")
    assert layer_path.stat().st_size == manifest["layers"][0]["size"]

    with pytest.raises(DigestMismatchError, match="digest mismatch"):
        NamedOCIContext.parse(f"base={layout}@{manifest_digest}")


def test_named_oci_context_rejects_missing_transitive_blob_before_solve(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    manifest_digest = _add_oci_descriptor(layout, b"layer payload")
    manifest_path = layout / "blobs" / "sha256" / manifest_digest.split(":", 1)[1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layer_digest = manifest["layers"][0]["digest"]
    (layout / "blobs" / "sha256" / layer_digest.split(":", 1)[1]).unlink()

    with pytest.raises(PalimpsestError, match="missing referenced blob"):
        NamedOCIContext.parse(f"base={layout}@{manifest_digest}")


def test_named_oci_context_rejects_bool_descriptor_size(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    config = b"{}"
    config_digest = _add_oci_blob(layout, config)
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "config": {"digest": config_digest, "size": True},
            "layers": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_digest = _add_oci_blob(layout, manifest)

    with pytest.raises(PalimpsestError, match="digest and nonnegative size"):
        NamedOCIContext.parse(f"base={layout}@{manifest_digest}")


def test_named_oci_context_hashes_each_repeated_blob_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    layer = b"repeated layer"
    layer_digest = _add_oci_blob(layout, layer)
    config = b"{}"
    config_digest = _add_oci_blob(layout, config)
    layer_descriptor = {
        "digest": layer_digest,
        "size": len(layer),
        "mediaType": "application/vnd.oci.image.layer.v1.tar",
    }
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "config": {"digest": config_digest, "size": len(config)},
            "layers": [layer_descriptor, layer_descriptor],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_digest = _add_oci_blob(layout, manifest)
    original = buildkit.verify_blob_chunks
    calls: list[str] = []

    def counting_verifier(*, expected_digest: str, expected_size: int, chunks) -> None:
        calls.append(expected_digest)
        original(expected_digest=expected_digest, expected_size=expected_size, chunks=chunks)

    monkeypatch.setattr(buildkit, "verify_blob_chunks", counting_verifier)
    NamedOCIContext.parse(f"base={layout}@{manifest_digest}")

    assert calls.count(layer_digest) == 1


def test_named_oci_context_path_is_transport_only_and_alias_order_is_deterministic(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "first-layout")
    manifest_digest = _add_oci_descriptor(layout, b"layer")
    relocated_layout = tmp_path / "relocated-layout"
    shutil.copytree(layout, relocated_layout)
    first = NamedOCIContext.parse(f"zbase={layout}@{manifest_digest}")
    second = NamedOCIContext.parse(f"abase={layout}@{manifest_digest}")
    relocated = NamedOCIContext.parse(f"zbase={relocated_layout}@{manifest_digest}")
    spec = _spec(
        tmp_path,
        "FROM scratch\n",
        local_images=(first, second),
        offline=True,
        network="none",
    )

    assert compute_build_key(spec) == compute_build_key(replace(spec, local_images=(second, first)))
    assert compute_build_key(spec) == compute_build_key(replace(spec, local_images=(relocated, second)))
    argv = build_buildx_command(
        spec,
        cache_from=tmp_path / "cache-in",
        cache_to=tmp_path / "cache-out",
        metadata_file=tmp_path / "metadata.json",
    )
    contexts = [argv[index + 1] for index, item in enumerate(argv) if item == "--build-context"]
    assert contexts == [f"abase={second.buildx_value}", f"zbase={first.buildx_value}"]


def test_destination_registry_and_export_controls_do_not_change_solve_key(tmp_path: Path):
    spec = _spec(tmp_path)
    key = compute_build_key(spec, builder_fingerprint=_test_builder_fingerprint())

    transport_variant = replace(
        spec,
        tag="registry.example.com/team/app:v2",
        additional_tags=("registry.example.com/team/app:stable",),
        pull=True,
        load=True,
        push_image=True,
        external_cache_from=("type=registry,ref=registry.example.com/team/cache:in",),
        external_cache_to=("type=registry,ref=registry.example.com/team/cache:out,mode=max",),
        registry_profile="corp",
        registry_config_digest=D_IMAGE,
        progress="tty",
    )

    assert compute_build_key(transport_variant, builder_fingerprint=_test_builder_fingerprint()) == key


def test_named_oci_context_rejects_corrupt_transitive_blob(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    manifest_digest = _add_oci_descriptor(layout, b"layer payload")
    manifest_path = layout / "blobs" / "sha256" / manifest_digest.split(":", 1)[1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layer_digest = manifest["layers"][0]["digest"]
    (layout / "blobs" / "sha256" / layer_digest.split(":", 1)[1]).write_bytes(b"tampered")

    with pytest.raises(PalimpsestError, match="size mismatch"):
        NamedOCIContext.parse(f"base={layout}@{manifest_digest}")


def test_named_oci_context_rejects_onbuild_in_config_reachable_through_index(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    config = json.dumps({"config": {"OnBuild": ["RUN curl https://example.invalid"]}}).encode()
    manifest_digest = _add_oci_descriptor(layout, b"layer payload", config=config)
    manifest_blob = layout / "blobs" / "sha256" / manifest_digest.split(":", 1)[1]
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                    "size": manifest_blob.stat().st_size,
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    index_digest = _add_oci_blob(layout, index)

    with pytest.raises(PalimpsestError, match="OnBuild triggers"):
        NamedOCIContext.parse(f"base={layout}@{index_digest}")

    empty_layout = _write_minimal_oci_layout(tmp_path / "empty-onbuild-layout")
    empty_digest = _add_oci_descriptor(
        empty_layout,
        b"layer payload",
        config=json.dumps({"config": {"OnBuild": []}}).encode(),
    )
    NamedOCIContext.parse(f"base={empty_layout}@{empty_digest}")


def test_offline_validation_allows_scratch(tmp_path: Path):
    spec = _spec(tmp_path, "FROM scratch\n", offline=True, network="none")
    validate_offline_dockerfile(spec)


def test_offline_validation_allows_only_pinned_local_oci_from(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    manifest_digest = _add_oci_descriptor(layout, b"local OCI descriptor")
    local = NamedOCIContext.parse(f"base={layout}@{manifest_digest}")
    spec = _spec(tmp_path, "FROM base\n", local_images=(local,), offline=True, network="none")

    validate_offline_dockerfile(spec)


def test_offline_validation_allows_run_mount_from_local_context_and_prior_stage(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    manifest_digest = _add_oci_descriptor(layout, b"local OCI descriptor")
    local = NamedOCIContext.parse(f"base={layout}@{manifest_digest}")
    spec = _spec(
        tmp_path,
        """FROM base AS seed
RUN --mount=type=bind,from=base,target=/base true
FROM scratch
COPY --exclude=--from=unknown --from=seed /seed /seed
RUN --mount=type=bind,from=seed,target=/seed --mount=type=bind,from=0,target=/zero true
""",
        local_images=(local,),
        offline=True,
        network="none",
    )

    validate_offline_dockerfile(spec)


@pytest.mark.parametrize(
    "mount_source",
    [
        "unknown",
        "docker.io/library/alpine@sha256:" + "c" * 64,
        "${BASE}",
    ],
)
def test_offline_validation_rejects_run_mount_from_unknown_or_remote(tmp_path: Path, mount_source: str):
    spec = _spec(
        tmp_path,
        f"FROM scratch\nRUN --mount=type=cache,target=/tmp --mount=type=bind,from={mount_source},target=/src true\n",
        offline=True,
        network="none",
    )

    with pytest.raises(PalimpsestError, match="RUN --mount"):
        validate_offline_dockerfile(spec)


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM ubuntu:24.04\n",
        f"FROM ubuntu@{D_IMAGE}\n",
        "ARG BASE=ubuntu:24.04\nFROM ${BASE}\n",
        "FROM scratch AS build\nARG BASE\nFROM ${BASE}\n",
        "# syntax=docker/dockerfile:1\nFROM scratch\n",
        "\ufeff# syntax=docker/dockerfile:1\nFROM scratch\n",
        "# escape=`\nFROM scratch\nADD app.txt `\n https://example.invalid/archive.tar /opt/\n",
        'FROM scratch\nADD ["https://example.invalid/archive.tar", "/opt/"]\n',
        "FROM scratch\nADD app.txt https://example.invalid/archive.tar /opt/\n",
        "FROM scratch\nARG SOURCE\nADD ${SOURCE} /opt/\n",
        "FROM scratch\nADD git@github.com:example/repository.git /src/\n",
        "FROM scratch\nADD git@github.com:example/repository /src/\n",
        "FROM scratch\nADD\thttps://example.invalid/mutable /x\n",
        "FROM scratch\nRUN\t--mount=type=bind,from=docker.io/library/alpine:latest,target=/x true\n",
        "FROM scratch\nRUN echo foo " + "\\\\" + "\nADD https://example.invalid/mutable /x\n",
        "FROM scratch\nRUN --mount=type=cache,target=/tmp,id=--network=none --network=host true\n",
        "FROM scratch\nADD local.txt \\\n# benign\nhttps://example.invalid/a.tar /opt/\n",
        "FROM scratch\nADD local.txt \\\n\nhttps://example.invalid/a.tar /opt/\n",
        "FROM scratch\nADD ht\\\ntps://example.invalid/a.tar /opt/\n",
        "FROM scratch AS seed\nFROM \\\n# benign\nubuntu:latest\n",
        "FROM scratch AS seed\nFROM ubu\\\nntu:latest\n",
        "FROM scratch AS seed\nFROM --platf\\orm=linux/amd64 ubuntu:latest\n",
        'FROM scratch AS seed\nFROM --platf"orm"=linux/amd64 ubuntu:latest\n',
        "FROM scratch AS seed\nFROM scratch\n"
        "COPY --exclude=--from=seed --from=docker.io/library/alpine:latest /bin/sh /bin/sh\n",
        "FROM scratch\nCOPY --fr\\om=docker.io/library/alpine:latest /bin/sh /bin/sh\n",
        'FROM scratch\nCOPY --fr"om"=docker.io/library/alpine:latest /bin/sh /bin/sh\n',
        "FROM scratch\nRUN --mo\\unt=type=bind,from=docker.io/library/alpine:latest,target=/src true\n",
    ],
)
def test_offline_validation_rejects_remote_arg_and_external_frontend(tmp_path: Path, dockerfile: str):
    spec = _spec(tmp_path, dockerfile, offline=True, network="none")

    with pytest.raises(PalimpsestError):
        validate_offline_dockerfile(spec)


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM ubuntu:24.04\n",
        "# syntax=docker/dockerfile:1\nFROM scratch\n",
        "#syntax=docker/dockerfile:1\nFROM scratch\n",
        "# syntax = docker/dockerfile:1\nFROM scratch\n",
        "\ufeff# syntax=docker/dockerfile:1\nFROM scratch\n",
        "# escape=`\nFROM scratch\nADD app.txt `\n https://example.invalid/archive.tar /opt/\n",
        "FROM scratch\nADD local.txt \\\n# benign\nhttps://example.invalid/a.tar /opt/\n",
        "FROM scratch\nADD local.txt \\\n\nhttps://example.invalid/a.tar /opt/\n",
        "FROM scratch\nADD ht\\\ntps://example.invalid/a.tar /opt/\n",
        "FROM scratch AS seed\nFROM \\\n# benign\nubuntu:latest\n",
        "FROM scratch AS seed\nFROM ubu\\\nntu:latest\n",
        "FROM scratch\nRUN echo foo " + "\\\\" + "\nADD https://example.invalid/mutable /x\n",
        "FROM scratch AS seed\nFROM --platf\\orm=linux/amd64 ubuntu:latest\n",
        'FROM scratch AS seed\nFROM --platf"orm"=linux/amd64 ubuntu:latest\n',
    ],
)
def test_online_unpinned_sources_fail_before_buildx_preflight(tmp_path: Path, dockerfile: str):
    spec = _spec(tmp_path, dockerfile)
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append([os.fspath(part) for part in argv])
        raise AssertionError("invalid online Dockerfile reached Buildx")

    with pytest.raises(PalimpsestError, match="pinned|escape|checksum"):
        build_with_buildkit(
            spec,
            init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}),
            hub_client=object(),
            runner=runner,
        )
    assert calls == []


@pytest.mark.parametrize(
    "dockerfile",
    [
        f"FROM docker.io/library/ubuntu@{D_IMAGE}\n",
        f"# syntax=docker/dockerfile:1@{D_IMAGE}\nFROM scratch\n",
    ],
)
def test_online_digest_pinned_sources_are_allowed(tmp_path: Path, dockerfile: str):
    validate_online_dockerfile(_spec(tmp_path, dockerfile))


@pytest.mark.parametrize("separator", ["\f", "\v", "\x85", "\u2028"])
@pytest.mark.parametrize("offline", [False, True])
def test_non_lf_characters_cannot_hide_command_after_backslash(
    tmp_path: Path,
    separator: str,
    offline: bool,
):
    dockerfile = "FROM scratch\nRUN echo \\" + separator + "\nADD https://example.invalid/mutable /x\n"
    spec = _spec(tmp_path, dockerfile, offline=offline, network="none" if offline else "default")

    with pytest.raises(PalimpsestError):
        (validate_offline_dockerfile if offline else validate_online_dockerfile)(spec)


@pytest.mark.parametrize(
    "instruction",
    [
        "ADD https://example.invalid/archive.tar /opt/",
        "ADD git+https://example.invalid/repository.git /src/",
        f"ADD --checksum={D_IMAGE} https://example.invalid/repository.git /src/",
        "ADD git@github.com:example/repository /src/",
        "ADD\thttps://example.invalid/mutable /x",
        f"ADD https://example.invalid/archive.tar /tmp/--checksum={D_IMAGE}",
        f"ADD -- --checksum={D_IMAGE} https://example.invalid/archive.tar /opt/",
        r'ADD ["h\u0074tp://example.invalid/archive.tar", "/opt/"]',
        "COPY --exclude=--from=seed --from=docker.io/library/alpine:latest /bin/sh /bin/sh",
        "COPY --fr\\om=docker.io/library/alpine:latest /bin/sh /bin/sh",
        'COPY --fr"om"=docker.io/library/alpine:latest /bin/sh /bin/sh',
        "RUN --mo\\unt=type=bind,from=docker.io/library/alpine:latest,target=/src true",
        "RUN\t--mount=type=bind,from=docker.io/library/alpine:latest,target=/x true",
        "COPY --from=docker.io/library/alpine:latest /bin/sh /bin/sh",
        "RUN --mount=type=bind,from=docker.io/library/alpine:latest,target=/src true",
    ],
)
def test_online_external_sources_require_immutable_identity(tmp_path: Path, instruction: str):
    with pytest.raises(PalimpsestError):
        validate_online_dockerfile(_spec(tmp_path, f"FROM scratch\n{instruction}\n"))


def test_online_http_add_and_external_mount_allow_sha256_pins(tmp_path: Path):
    validate_online_dockerfile(
        _spec(
            tmp_path,
            (
                "FROM scratch\n"
                f"ADD --chown=0:0 --checksum={D_IMAGE} https://example.invalid/archive.tar /opt/\n"
                f"COPY --exclude=--from=seed --from=docker.io/library/alpine@{D_IMAGE} /bin/sh /bin/sh\n"
                f"RUN --mount=type=bind,from=docker.io/library/alpine@{D_OTHER_IMAGE},target=/src true\n"
            ),
        )
    )


def test_offline_buildx_argv_uses_named_oci_context_network_none_and_local_cache(tmp_path: Path):
    layout = _write_minimal_oci_layout(tmp_path / "base-layout")
    manifest_digest = _add_oci_descriptor(layout, b"local OCI descriptor")
    local = NamedOCIContext.parse(f"base={layout}@{manifest_digest}")
    spec = _spec(
        tmp_path,
        "FROM base\n",
        local_images=(local,),
        offline=True,
        network="none",
    )
    cache_from = tmp_path / "cache-in"
    cache_to = tmp_path / "cache-out"
    metadata = tmp_path / "metadata.json"

    argv = build_buildx_command(spec, cache_from=cache_from, cache_to=cache_to, metadata_file=metadata)
    rendered = " ".join(os.fspath(item) for item in argv)

    assert argv[:3] == ["docker", "buildx", "build"]
    assert "--build-context" in argv or "--build-context=" in rendered
    assert "base=oci-layout://" in rendered
    assert os.fspath(layout) in rendered
    assert manifest_digest in rendered
    assert "--network none" in rendered or "--network=none" in rendered
    assert "type=local" in rendered and f"src={cache_from}" in rendered
    assert f"dest={cache_to}" in rendered
    assert f"--metadata-file {metadata}" in rendered or f"--metadata-file={metadata}" in rendered
    assert "type=registry" not in rendered


def test_buildx_argv_supports_repeated_tags_outputs_pull_and_additive_external_cache(tmp_path: Path):
    spec = _spec(
        tmp_path,
        additional_tags=("registry.example.com/team/app:stable",),
        pull=True,
        load=True,
        push_image=True,
        progress="tty",
        external_cache_from=("type=registry,ref=registry.example.com/team/cache:in",),
        external_cache_to=("type=registry,ref=registry.example.com/team/cache:out,mode=max",),
    )
    cache_from = tmp_path / "cache-in"
    cache_to = tmp_path / "cache-out"
    metadata = tmp_path / "metadata.json"

    argv = build_buildx_command(
        spec,
        cache_from=cache_from,
        cache_to=cache_to,
        metadata_file=metadata,
        builder_name="palimpsest",
    )

    assert argv[:5] == ["docker", "buildx", "build", "--builder", "palimpsest"]
    assert [argv[index + 1] for index, item in enumerate(argv) if item == "--tag"] == [
        "example/app:test",
        "registry.example.com/team/app:stable",
    ]
    assert "--pull" in argv
    outputs = [argv[index + 1] for index, item in enumerate(argv) if item == "--output"]
    assert outputs == [f"type=oci,dest={spec.output}", "type=docker", "type=registry"]
    cache_imports = [argv[index + 1] for index, item in enumerate(argv) if item == "--cache-from"]
    assert cache_imports == [
        f"type=local,src={cache_from}",
        "type=registry,ref=registry.example.com/team/cache:in",
    ]
    cache_exports = [argv[index + 1] for index, item in enumerate(argv) if item == "--cache-to"]
    assert cache_exports == [
        f"type=local,dest={cache_to},mode=max",
        "type=registry,ref=registry.example.com/team/cache:out,mode=max",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"push_image": True},
        {"pull": True},
        {"external_cache_from": ("type=registry,ref=registry.example.com/cache",)},
        {"external_cache_to": ("type=registry,ref=registry.example.com/cache",)},
    ],
)
def test_offline_spec_rejects_registry_network_controls(tmp_path: Path, overrides: dict[str, object]):
    with pytest.raises(PalimpsestError):
        _spec(tmp_path, offline=True, network="none", **overrides)


def test_external_cache_spec_rejects_inline_secret_shaped_options(tmp_path: Path):
    with pytest.raises(PalimpsestError, match="credential"):
        _spec(
            tmp_path,
            external_cache_from=("type=registry,ref=registry.example.com/cache,token=secret",),
        )


@pytest.mark.parametrize(
    "cache_spec",
    [
        "type=s3,region=us-east-1,bucket=cache,access_key_id=AKIA_DO_NOT_STORE",
        "type=gha,ghtoken=ghp_do_not_store",
        "type=registry,ref=user:password@registry.example.com/cache",
    ],
)
def test_external_cache_spec_rejects_all_inline_credential_forms(tmp_path: Path, cache_spec: str):
    with pytest.raises(PalimpsestError, match="secret|credential"):
        _spec(tmp_path, external_cache_to=(cache_spec,))


def test_external_cache_receipt_records_only_backend_and_spec_digest() -> None:
    value = "type=registry,ref=registry.example.com/team/cache"
    receipt = buildkit._cache_spec_receipt(value)

    assert receipt == {
        "spec_digest": f"sha256:{hashlib.sha256(value.encode()).hexdigest()}",
    }
    assert value not in json.dumps(receipt)


@pytest.mark.parametrize("driver", ["docker-container", "kubernetes", "remote"])
def test_buildx_preflight_accepts_selected_oci_exporter_driver_without_mutation(driver: str):
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        command = [os.fspath(part) for part in argv]
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"Name: palimpsest\nDriver:        {driver}\n",
            stderr="",
        )

    assert preflight_buildx_oci_exporter(runner=runner) == driver
    assert calls == [["docker", "buildx", "inspect"]]


def test_buildx_preflight_rejects_default_docker_driver_with_non_disruptive_setup_hint():
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Name: default\nDriver: docker\n",
            stderr="",
        )

    with pytest.raises(PalimpsestError, match="not verified to support the OCI exporter") as raised:
        preflight_buildx_oci_exporter(runner=runner)

    message = str(raised.value)
    assert "docker-container" in message
    assert "BUILDX_BUILDER=palimpsest" in message
    assert "will not change the selected builder" in message


def test_buildx_preflight_reports_inspect_failure_with_diagnostics():
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="buildx plugin is not installed")

    with pytest.raises(PalimpsestError, match="cannot inspect the currently selected") as raised:
        preflight_buildx_oci_exporter(runner=runner)

    assert "docker buildx ls" in str(raised.value)
    assert "buildx plugin is not installed" in str(raised.value)


def test_strict_offline_preflight_accepts_real_buildx_format_and_proves_container_network_none():
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        command = [os.fspath(part) for part in argv]
        calls.append(command)
        if command == ["docker", "buildx", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "Name: palimpsest-offline\n"
                    "Driver: docker-container\n"
                    "Nodes:\n"
                    "Name: palimpsest-offline0\n"
                    "Endpoint: default\n"
                    f"BuildKit version: {BUILDKIT_VERSION}\n"
                ),
                stderr="",
            )
        if command == ["docker", "context", "show"]:
            return subprocess.CompletedProcess(command, 0, stdout="default\n", stderr="")
        if command[:3] == ["docker", "context", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout="unix:///var/run/docker.sock\n", stderr="")
        if command[:2] == ["docker", "ps"]:
            assert "name=^/buildx_buildkit_palimpsest\\-offline[0-9]+$" in command
            return subprocess.CompletedProcess(command, 0, stdout="container-id\n", stderr="")
        if command[:3] == ["docker", "inspect", "--format"]:
            if command[3] == "{{json .NetworkSettings.Networks}}":
                return subprocess.CompletedProcess(command, 0, stdout='{"none":{}}\n', stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="none\n", stderr="")
        if command == ["docker", "buildx", "version"]:
            return subprocess.CompletedProcess(command, 0, stdout=BUILDX_VERSION + "\n", stderr="")
        raise AssertionError(command)

    environment: dict[str, str] = {}
    assert (
        preflight_buildx_oci_exporter(
            strict_offline=True,
            environment_out=environment,
            runner=runner,
        )
        == "docker-container"
    )
    assert environment["buildkit_versions"] == BUILDKIT_VERSION
    assert environment["fingerprint"] == _test_builder_fingerprint()


def test_strict_offline_preflight_rejects_multi_node_builder_before_docker_access():
    def runner(argv, **_kwargs):
        command = [os.fspath(part) for part in argv]
        if command == ["docker", "buildx", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "Name: mixed\n"
                    "Driver: docker-container\n"
                    "Nodes:\n"
                    "Name: mixed0\nEndpoint: default\n"
                    "Name: mixed1\nEndpoint: tcp://remote.example:2376\n"
                    f"BuildKit version: {BUILDKIT_VERSION}\n"
                ),
                stderr="",
            )
        raise AssertionError(f"multi-node builder reached Docker access: {command}")

    with pytest.raises(PalimpsestError, match="exactly one local Buildx node"):
        preflight_buildx_oci_exporter(strict_offline=True, runner=runner)


def test_strict_offline_preflight_rejects_added_container_network():
    def runner(argv, **_kwargs):
        command = [os.fspath(part) for part in argv]
        if command == ["docker", "buildx", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "Name: isolated\nDriver: docker-container\nNodes:\n"
                    "Name: isolated0\nEndpoint: default\n"
                    f"BuildKit version: {BUILDKIT_VERSION}\n"
                ),
                stderr="",
            )
        if command == ["docker", "context", "show"]:
            return subprocess.CompletedProcess(command, 0, stdout="default\n", stderr="")
        if command[:3] == ["docker", "context", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout="unix:///var/run/docker.sock\n", stderr="")
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, stdout="container-id\n", stderr="")
        if command[:3] == ["docker", "inspect", "--format"]:
            output = '{"none":{},"bridge":{}}\n' if command[3].startswith("{{json") else "none\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        raise AssertionError(command)

    with pytest.raises(PalimpsestError, match="active Docker network attachments"):
        preflight_buildx_oci_exporter(strict_offline=True, runner=runner)


def test_deterministic_cache_tar_normalizes_order_and_mtime(tmp_path: Path):
    source = tmp_path / "cache"
    (source / "nested").mkdir(parents=True)
    (source / "z.txt").write_text("z\n", encoding="utf-8")
    (source / "nested" / "a.txt").write_text("a\n", encoding="utf-8")
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"

    create_deterministic_tar(source, first)
    os.utime(source / "z.txt", (2_000_000_000, 2_000_000_000))
    os.utime(source / "nested" / "a.txt", (1_000_000_000, 1_000_000_000))
    create_deterministic_tar(source, second)

    assert first.read_bytes() == second.read_bytes()


def test_cache_tar_round_trip_and_traversal_rejection(tmp_path: Path):
    source = tmp_path / "cache"
    (source / "blobs" / "sha256").mkdir(parents=True)
    (source / "index.json").write_text('{"schemaVersion":2}\n', encoding="utf-8")
    (source / "blobs" / "sha256" / ("a" * 64)).write_bytes(b"cache-blob")
    archive = tmp_path / "cache.tar"
    destination = tmp_path / "expanded"

    create_deterministic_tar(source, archive)
    extract_cache_tar(archive, destination)

    assert (destination / "index.json").read_text(encoding="utf-8") == '{"schemaVersion":2}\n'
    assert (destination / "blobs" / "sha256" / ("a" * 64)).read_bytes() == b"cache-blob"

    malicious = tmp_path / "malicious.tar"
    with tarfile.open(malicious, "w") as handle:
        payload = b"escape"
        info = tarfile.TarInfo("../escaped")
        info.size = len(payload)
        handle.addfile(info, io.BytesIO(payload))

    with pytest.raises(PalimpsestError):
        extract_cache_tar(malicious, tmp_path / "unsafe")
    assert not (tmp_path / "escaped").exists()


def _option_values(argv: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, argument in enumerate(argv):
        if argument == option:
            values.append(argv[index + 1])
        elif argument.startswith(option + "="):
            values.append(argument.split("=", 1)[1])
    return values


def _csv_path(value: str, key: str) -> Path:
    fields = dict(field.split("=", 1) for field in value.split(",") if "=" in field)
    return Path(fields[key])


class _SuccessfulBuildxRunner:
    def __init__(self, spec: BuildKitSpec, *, require_imported_cache: bool = False):
        self.spec = spec
        self.require_imported_cache = require_imported_cache
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        command = [os.fspath(part) for part in argv]
        self.calls.append(command)

        if command == ["docker", "buildx", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "Name: palimpsest\n"
                    "Driver: docker-container\n"
                    "Nodes:\n"
                    "Name: palimpsest0\n"
                    "Endpoint: default\n"
                    f"BuildKit: {BUILDKIT_VERSION}\n"
                ),
                stderr="",
            )
        if command == ["docker", "buildx", "version"]:
            return subprocess.CompletedProcess(command, 0, stdout=BUILDX_VERSION + "\n", stderr="")
        if command == ["docker", "context", "show"]:
            return subprocess.CompletedProcess(command, 0, stdout="default\n", stderr="")
        if command[:3] == ["docker", "context", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout="unix:///var/run/docker.sock\n", stderr="")
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, stdout="buildkit-container\n", stderr="")
        if command[:3] == ["docker", "inspect", "--format"]:
            if command[3] == "{{json .NetworkSettings.Networks}}":
                return subprocess.CompletedProcess(command, 0, stdout='{"none":{}}\n', stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="none\n", stderr="")

        cache_from_values = _option_values(command, "--cache-from")
        if self.require_imported_cache:
            assert cache_from_values, "Hub cache hit was not passed back to BuildKit"
            imported = _csv_path(cache_from_values[0], "src")
            assert (imported / "index.json").read_text(encoding="utf-8") == '{"remote":true}\n'

        cache_to_values = _option_values(command, "--cache-to")
        assert cache_to_values
        cache_to = _csv_path(cache_to_values[0], "dest")
        cache_to.mkdir(parents=True, exist_ok=True)
        (cache_to / "index.json").write_text('{"schemaVersion":2}\n', encoding="utf-8")
        (cache_to / "cache-blob").write_bytes(b"new-buildkit-cache")

        metadata_values = _option_values(command, "--metadata-file")
        assert metadata_values
        metadata = Path(metadata_values[0])
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(json.dumps({"containerimage.digest": D_IMAGE}), encoding="utf-8")

        self.spec.output.parent.mkdir(parents=True, exist_ok=True)
        self.spec.output.write_bytes(b"oci-output")
        if self.spec.rootfs_output is not None:
            self.spec.rootfs_output.mkdir(parents=True, exist_ok=True)
            (self.spec.rootfs_output / "app").write_bytes(b"rootfs")
        if self.spec.runtime_rootfs_archive is not None:
            self.spec.runtime_rootfs_archive.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(self.spec.runtime_rootfs_archive, "w", format=tarfile.PAX_FORMAT) as archive:
                payload = b"rootfs"
                info = tarfile.TarInfo("app")
                info.size = len(payload)
                info.mode = 0o755
                info.uid = 1234
                info.gid = 2345
                info.pax_headers = {"SCHILY.xattr.user.test": "preserved"}
                archive.addfile(info, io.BytesIO(payload))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def _write_cache_export(path: Path, marker: str) -> Path:
    path.mkdir(parents=True)
    (path / "index.json").write_text(json.dumps({"marker": marker}), encoding="utf-8")
    return path


def test_scope_cache_pointer_commit_is_atomic_and_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scope_root = tmp_path / "build-cache" / "demo"
    first_id = "bk-111111111111"
    first = buildkit._promote_scope_cache(_write_cache_export(tmp_path / "first", "first"), scope_root, first_id)

    assert first == scope_root / "generations" / first_id
    assert json.loads((scope_root / "current.json").read_text(encoding="utf-8")) == {
        "generation": first_id,
        "schema_version": 1,
    }
    assert buildkit._read_scope_cache(scope_root) == first

    original_atomic_write = buildkit.state.atomic_write_json

    def fail_pointer_write(path: Path, value: dict[str, object]) -> None:
        if path == scope_root / "current.json":
            raise OSError("simulated pointer replacement failure")
        original_atomic_write(path, value)

    monkeypatch.setattr(buildkit.state, "atomic_write_json", fail_pointer_write)
    second_id = "bk-222222222222"
    with pytest.raises(OSError, match="simulated pointer"):
        buildkit._promote_scope_cache(_write_cache_export(tmp_path / "second", "second"), scope_root, second_id)

    assert buildkit._read_scope_cache(scope_root) == first
    assert (scope_root / "generations" / second_id / "index.json").is_file()

    original_atomic_write(
        scope_root / "current.json",
        {"schema_version": 1, "generation": "../../outside"},
    )
    with pytest.raises(PalimpsestError, match="invalid BuildKit cache generation"):
        buildkit._read_scope_cache(scope_root)


def test_scope_cache_generation_sync_failure_does_not_advance_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scope_root = tmp_path / "build-cache" / "demo"
    first_id = "bk-111111111111"
    first = buildkit._promote_scope_cache(_write_cache_export(tmp_path / "first", "first"), scope_root, first_id)
    original_fsync = buildkit.state.fsync_directory

    def fail_generation_sync(path: Path) -> None:
        if path == scope_root / "generations":
            raise StateError("simulated generation directory sync failure")
        original_fsync(path)

    monkeypatch.setattr(buildkit.state, "fsync_directory", fail_generation_sync)
    second_id = "bk-222222222222"
    with pytest.raises(StateError, match="generation directory sync"):
        buildkit._promote_scope_cache(_write_cache_export(tmp_path / "second", "second"), scope_root, second_id)

    assert buildkit._read_scope_cache(scope_root) == first
    assert (scope_root / "generations" / second_id / "index.json").is_file()


class _OnlineCacheHub:
    def __init__(self, cache_tar: Path, build_key: str):
        self.cache_tar = cache_tar
        self.build_key = build_key
        self.calls: list[tuple[str, object]] = []

    def list_layers(self, **query):
        self.calls.append(("list_layers", query))
        assert query == {"kind": KIND_BUILDKIT_CACHE, "chain_id": self.build_key, "limit": 2}
        return [
            {
                "blob_digest": digest_file(self.cache_tar),
                "kind": KIND_BUILDKIT_CACHE,
                "media_type": MEDIA_TYPE_BUILDKIT_CACHE,
                "chain_id": self.build_key,
            }
        ]

    def pull_blob(self, digest: str, destination: Path):
        self.calls.append(("pull_blob", digest))
        assert digest == digest_file(self.cache_tar)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.cache_tar.read_bytes())
        return destination

    def push_blob(self, path: Path, metadata: dict[str, object]):
        self.calls.append(("push_blob", metadata))
        assert path.is_file()
        assert metadata["kind"] == KIND_BUILDKIT_CACHE
        assert metadata["chain_id"] == self.build_key
        assert metadata["media_type"] == MEDIA_TYPE_BUILDKIT_CACHE
        return {"blob_digest": digest_file(path)}


def _remote_cache_tar(tmp_path: Path, build_key: str) -> Path:
    archive = tmp_path / "remote-cache.tar"
    descriptor = (
        json.dumps(
            {
                "schema": CACHE_ARCHIVE_SCHEMA,
                "build_key": build_key,
                "cache_scope": "example-app",
                "platform": "linux/amd64",
                "builder_fingerprint": _test_builder_fingerprint(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    index = b'{"remote":true}\n'
    blob = b"remote-buildkit-cache"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as handle:
        for name, payload in (
            ("palimpsest-cache.json", descriptor),
            ("cache/index.json", index),
            ("cache/blob", blob),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o444
            info.uid = info.gid = info.mtime = 0
            handle.addfile(info, io.BytesIO(payload))
    return archive


def test_hub_scope_fallback_is_partitioned_by_platform_and_builder(tmp_path: Path):
    spec = _spec(tmp_path)
    fingerprint = _test_builder_fingerprint()
    build_key = compute_build_key(spec, builder_fingerprint=fingerprint)

    class EmptyHub:
        def __init__(self):
            self.calls: list[dict[str, object]] = []

        def list_layers(self, **query):
            self.calls.append(query)
            return []

    hub = EmptyHub()
    cache, source = buildkit._resolve_hub_cache(
        hub,
        spec,
        build_key,
        tmp_path / "build",
        tmp_path / "cache-blobs",
        fingerprint,
    )

    compatible_name = buildkit._cache_name(spec.cache_scope, spec.platform, fingerprint)
    assert cache is None
    assert source == "none"
    assert hub.calls == [
        {"kind": KIND_BUILDKIT_CACHE, "chain_id": build_key, "limit": 2},
        {"name": compatible_name, "kind": KIND_BUILDKIT_CACHE, "limit": 1},
    ]
    assert len(compatible_name) <= 64
    assert compatible_name != buildkit._cache_name(spec.cache_scope, "linux/arm64", fingerprint)
    assert compatible_name != buildkit._cache_name(spec.cache_scope, spec.platform, f"sha256:{'f' * 64}")


def test_online_hub_hit_is_pulled_and_imported_into_buildkit(tmp_path: Path):
    roots: StatePaths = init_roots(
        {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    )
    spec = _spec(tmp_path)
    build_key = compute_build_key(spec, builder_fingerprint=_test_builder_fingerprint())
    hub = _OnlineCacheHub(_remote_cache_tar(tmp_path, build_key), build_key)
    runner = _SuccessfulBuildxRunner(spec, require_imported_cache=True)

    build_with_buildkit(spec, roots, hub_client=hub, runner=runner)

    assert runner.calls
    assert [name for name, _ in hub.calls][:2] == ["list_layers", "pull_blob"]
    assert "push_blob" in [name for name, _ in hub.calls]


def test_online_hub_errors_propagate_without_running_buildkit(tmp_path: Path):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    spec = _spec(tmp_path)

    class BrokenHub:
        def list_layers(self, **_query):
            raise HubError("hub unavailable")

    runner = _SuccessfulBuildxRunner(spec)
    with pytest.raises(HubError, match="hub unavailable"):
        build_with_buildkit(spec, roots, hub_client=BrokenHub(), runner=runner)
    assert runner.calls == [["docker", "buildx", "inspect"], ["docker", "buildx", "version"]]


def test_offline_build_never_calls_hub(tmp_path: Path):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    spec = _spec(tmp_path, offline=True, network="none")

    class ForbiddenHub:
        def __getattr__(self, name: str):
            raise AssertionError(f"offline build attempted Hub access through {name}")

    runner = _SuccessfulBuildxRunner(spec)
    build_with_buildkit(spec, roots, hub_client=ForbiddenHub(), runner=runner)

    assert runner.calls
    build_call = next(call for call in runner.calls if call[:3] == ["docker", "buildx", "build"])
    rendered = " ".join(build_call)
    assert "--builder palimpsest" in rendered
    assert "--network none" in rendered or "--network=none" in rendered


def test_same_scope_buildkit_solves_are_singleflight(tmp_path: Path):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    first_spec = _spec(tmp_path, offline=True, network="none")
    second_spec = replace(first_spec, output=tmp_path / "second-image.oci.tar")
    first_buildx = _SuccessfulBuildxRunner(first_spec)
    second_buildx = _SuccessfulBuildxRunner(second_spec)
    first_solve_entered = threading.Event()
    release_first_solve = threading.Event()
    second_preflight_done = threading.Event()
    second_solve_entered = threading.Event()
    errors: list[BaseException] = []

    def first_runner(argv, **kwargs):
        command = [os.fspath(part) for part in argv]
        if command[:3] == ["docker", "buildx", "build"]:
            first_solve_entered.set()
            assert release_first_solve.wait(5), "timed out waiting to release first solve"
        return first_buildx(argv, **kwargs)

    def second_runner(argv, **kwargs):
        command = [os.fspath(part) for part in argv]
        if command == ["docker", "buildx", "inspect"]:
            result = second_buildx(argv, **kwargs)
            second_preflight_done.set()
            return result
        if command[:3] == ["docker", "buildx", "build"]:
            second_solve_entered.set()
        return second_buildx(argv, **kwargs)

    def run_build(spec: BuildKitSpec, runner) -> None:
        try:
            build_with_buildkit(spec, roots, runner=runner)
        except BaseException as exc:  # pragma: no cover - asserted below across threads
            errors.append(exc)

    first_thread = threading.Thread(target=run_build, args=(first_spec, first_runner))
    second_thread = threading.Thread(target=run_build, args=(second_spec, second_runner))
    first_thread.start()
    assert first_solve_entered.wait(5)
    second_thread.start()
    assert second_preflight_done.wait(5)
    assert not second_solve_entered.wait(0.2)

    release_first_solve.set()
    first_thread.join(5)
    second_thread.join(5)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert second_solve_entered.is_set()


def test_buildkit_rootfs_is_compacted_to_one_verified_runtime_block(tmp_path: Path):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    runtime_base = "sha256:" + "e" * 64
    spec = _spec(
        tmp_path,
        offline=True,
        network="none",
        runtime_rootfs_archive=tmp_path / "rootfs.tar",
        runtime_tag="demo-runtime",
        runtime_base_digest=runtime_base,
    )
    buildx = _SuccessfulBuildxRunner(spec)
    packed_manifests: dict[Path, bytes] = {}

    def runner(argv, **kwargs):
        command = [os.fspath(part) for part in argv]
        if command[0] == "mksquashfs":
            if command[1:] == ["-version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="mksquashfs version 4.7.5 (test)\n",
                    stderr="",
                )
            assert command[1] == "-" and "-tar" in command
            assert command[command.index("-root-uid") + 1] == "0"
            assert command[command.index("-root-gid") + 1] == "0"
            assert command[command.index("-root-mode") + 1] == "0755"
            source = kwargs["stdin"]
            source.seek(0)
            with tarfile.open(fileobj=source, mode="r:") as archive:
                app = archive.getmember("app")
                assert (app.uid, app.gid) == (1234, 2345)
                assert app.pax_headers["SCHILY.xattr.user.test"] == "preserved"
                manifest_stream = archive.extractfile(".palimpsest/runtime-pack.json")
                assert manifest_stream is not None
                manifest_bytes = manifest_stream.read()
                manifest = json.loads(manifest_bytes)
                assert manifest["base_image_digest"] == runtime_base
                assert manifest["packer_version"] == "4.7.5"
                assert manifest["packer_fingerprint"].startswith("sha256:")
                assert (manifest["root_uid"], manifest["root_gid"], manifest["root_mode"]) == (0, 0, "0755")
            source.seek(0)
            source_digest = hashlib.sha256(source.read()).digest()
            output = Path(command[2])
            output.write_bytes(b"hsqs" + source_digest)
            packed_manifests[output] = manifest_bytes
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        if command[0] == "unsquashfs":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=packed_manifests[Path(command[2])],
                stderr=b"",
            )
        return buildx(argv, **kwargs)

    record = build_with_buildkit(spec, roots, runner=runner)
    runtime_digest = record["runtime_block_digest"]
    assert isinstance(runtime_digest, str)
    assert record["buildx_driver"] == "docker-container"
    assert record["started_at"] <= record["finished_at"]
    assert "runtime_transport" not in record

    tag = read_tag_record(roots, "demo-runtime")
    assert tag.digest == runtime_digest
    assert tag.parent_digest is None
    assert tag.base_image_digest == runtime_base
    store = ContentStore(roots.store)
    metadata = store.read_metadata(runtime_digest)
    assert metadata["media_type"] == MEDIA_TYPE_LAYER_SQUASHFS
    assert metadata["filesystem"] == "squashfs"
    assert metadata["readonly"] is True
    assert metadata["platform"] == "linux/amd64"
    assert metadata["arch"] == "x86_64"
    assert "attach_bus" not in metadata


def test_runtime_pack_conversion_cache_reuses_bound_block_and_separates_base(tmp_path: Path):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    first_base = "sha256:" + "e" * 64
    second_base = "sha256:" + "f" * 64
    first = _spec(
        tmp_path,
        offline=True,
        network="none",
        runtime_rootfs_archive=tmp_path / "first-rootfs.tar",
        runtime_tag="runtime-first",
        runtime_base_digest=first_base,
    )
    second = replace(
        first,
        output=tmp_path / "second.oci.tar",
        runtime_rootfs_archive=tmp_path / "second-rootfs.tar",
        runtime_tag="runtime-second",
    )
    different_base = replace(
        first,
        output=tmp_path / "different-base.oci.tar",
        runtime_rootfs_archive=tmp_path / "different-base-rootfs.tar",
        runtime_tag="runtime-different-base",
        runtime_base_digest=second_base,
    )
    pack_calls: list[list[str]] = []

    def expected_manifest_bytes(spec: BuildKitSpec) -> bytes:
        manifest = {
            "schema": buildkit.RUNTIME_PACK_SCHEMA,
            "base_image_digest": spec.runtime_base_digest,
            "platform": spec.platform,
            "arch": image_arch_for_platform(spec.platform),
            "source_rootfs_tar_digest": digest_file(spec.runtime_rootfs_archive),
            "source_oci_manifest_digest": D_IMAGE,
            "filesystem": "squashfs",
            "compression": "zstd",
            "compression_level": 3,
            "block_size": spec.runtime_block_size,
            "root_uid": 0,
            "root_gid": 0,
            "root_mode": "0755",
            "readonly": True,
            **buildkit._injected_runner_packer_identity("4.7.5").manifest_binding(),
        }
        return buildkit._canonical_json(buildkit._bound_runtime_manifest(manifest, "4.7.5")) + b"\n"

    def run(spec: BuildKitSpec) -> dict[str, object]:
        buildx = _SuccessfulBuildxRunner(spec)

        def runner(argv, **kwargs):
            command = [os.fspath(part) for part in argv]
            if command[0] == "mksquashfs":
                if command[1:] == ["-version"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="mksquashfs version 4.7.5 (test)\n",
                        stderr="",
                    )
                pack_calls.append(command)
                source = kwargs["stdin"]
                source.seek(0)
                output = Path(command[2])
                output.write_bytes(b"hsqs" + hashlib.sha256(source.read()).digest())
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
            if command[0] == "unsquashfs":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=expected_manifest_bytes(spec),
                    stderr=b"",
                )
            return buildx(argv, **kwargs)

        return build_with_buildkit(spec, roots, runner=runner)

    first_record = run(first)
    second_record = run(second)
    different_base_record = run(different_base)

    assert first_record["runtime_cache_source"] == "built"
    assert second_record["runtime_cache_source"] == "local"
    assert second_record["runtime_block_digest"] == first_record["runtime_block_digest"]
    assert second_record["runtime_pack_manifest_digest"] == first_record["runtime_pack_manifest_digest"]
    assert different_base_record["runtime_cache_source"] == "built"
    assert different_base_record["runtime_block_digest"] != first_record["runtime_block_digest"]
    assert different_base_record["runtime_pack_manifest_digest"] != first_record["runtime_pack_manifest_digest"]
    assert len(pack_calls) == 2


def test_hub_runtime_pack_pull_requires_unsquashfs_and_exact_embedded_manifest(tmp_path: Path):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    store = ContentStore(roots.store)
    base = "sha256:" + "d" * 64
    source_tar_digest = "sha256:" + "e" * 64
    runtime_manifest = {
        "schema": buildkit.RUNTIME_PACK_SCHEMA,
        "base_image_digest": base,
        "platform": "linux/amd64",
        "arch": "x86_64",
        "source_rootfs_tar_digest": source_tar_digest,
        "source_oci_manifest_digest": D_IMAGE,
        "filesystem": "squashfs",
        "compression": "zstd",
        "compression_level": 3,
        "block_size": 131072,
        "root_uid": 0,
        "root_gid": 0,
        "root_mode": "0755",
        "readonly": True,
        **buildkit._injected_runner_packer_identity("4.7.5").manifest_binding(),
    }
    pack_key = buildkit._runtime_manifest_digest(runtime_manifest, "4.7.5")
    expected_bytes = buildkit._canonical_json(buildkit._bound_runtime_manifest(runtime_manifest, "4.7.5")) + b"\n"
    payload = b"hsqs-runtime-block"

    class RuntimeHub:
        def __init__(self, blob: bytes):
            self.blob = blob
            self.digest = f"sha256:{hashlib.sha256(blob).hexdigest()}"

        def list_layers(self, **query):
            assert query == {"kind": "squashfs", "chain_id": pack_key, "limit": 2}
            return [
                {
                    "blob_digest": self.digest,
                    "kind": "squashfs",
                    "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                    "chain_id": pack_key,
                    "base_image_digest": base,
                    "arch": "x86_64",
                }
            ]

        def pull_blob(self, requested: str, destination: Path):
            assert requested == self.digest
            destination.write_bytes(self.blob)

    valid_hub = RuntimeHub(payload)
    (tmp_path / "build-ok").mkdir()

    def valid_unsquashfs(argv, **_kwargs):
        assert argv[0:2] == ["unsquashfs", "-cat"]
        return subprocess.CompletedProcess(argv, 0, stdout=expected_bytes, stderr=b"")

    assert (
        buildkit._pull_hub_runtime_pack(
            valid_hub,
            store,
            tmp_path / "build-ok",
            pack_key=pack_key,
            expected_base=base,
            expected_arch="x86_64",
            expected_manifest=runtime_manifest,
            expected_packer_version="4.7.5",
            runner=valid_unsquashfs,
        )
        == valid_hub.digest
    )

    store.delete(valid_hub.digest)
    (tmp_path / "build-bad").mkdir()
    with pytest.raises(PalimpsestError, match="embedded manifest"):
        buildkit._pull_hub_runtime_pack(
            RuntimeHub(payload + b"-other"),
            store,
            tmp_path / "build-bad",
            pack_key=pack_key,
            expected_base=base,
            expected_arch="x86_64",
            expected_manifest=runtime_manifest,
            expected_packer_version="4.7.5",
            runner=lambda argv, **_kwargs: subprocess.CompletedProcess(
                argv,
                0,
                stdout=b'{"base_image_digest":"wrong"}\n',
                stderr=b"",
            ),
        )


def test_runtime_manifest_reader_kills_unsquashfs_at_bounded_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    block = tmp_path / "runtime.squashfs"
    block.write_bytes(b"hsqs-test-runtime")

    class OversizedProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"x" * (buildkit._MAX_RUNTIME_MANIFEST_BYTES + 1))
            self.killed = False

        def kill(self):
            self.killed = True

        def wait(self):
            return -9 if self.killed else 0

    process = OversizedProcess()

    def fake_popen(command, **kwargs):
        assert command[:2] == ["unsquashfs", "-cat"]
        assert kwargs == {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
        return process

    monkeypatch.setattr(buildkit.subprocess, "Popen", fake_popen)

    with pytest.raises(PalimpsestError, match="exceeds the safety limit"):
        buildkit._verify_runtime_block(
            block,
            digest_file(block),
            expected_manifest={},
            runner=subprocess.run,
        )
    assert process.killed is True


def test_packer_fingerprint_binds_executable_and_compressor_library_bytes():
    executable_a = "sha256:" + "1" * 64
    executable_b = "sha256:" + "2" * 64
    library_a = "sha256:" + "3" * 64
    library_b = "sha256:" + "4" * 64

    first = buildkit._packer_identity_from_components("4.7.5", executable_a, (library_a,))
    assert (
        first.fingerprint != buildkit._packer_identity_from_components("4.7.5", executable_b, (library_a,)).fingerprint
    )
    assert (
        first.fingerprint != buildkit._packer_identity_from_components("4.7.5", executable_a, (library_b,)).fingerprint
    )


def test_online_runtime_pack_hub_hit_skips_compaction_and_materializes_local_state(tmp_path: Path):
    roots = init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    runtime_base = "sha256:" + "e" * 64
    runtime_blob = b"hsqs-hub-runtime-block"
    runtime_digest = f"sha256:{hashlib.sha256(runtime_blob).hexdigest()}"
    spec = _spec(
        tmp_path,
        runtime_rootfs_archive=tmp_path / "rootfs.tar",
        runtime_tag="hub-runtime",
        runtime_base_digest=runtime_base,
        push=True,
    )
    buildx = _SuccessfulBuildxRunner(spec)
    hub_calls: list[tuple[str, object]] = []

    class RuntimeHitHub:
        def list_layers(self, **query):
            hub_calls.append(("list_layers", query))
            if query.get("kind") == KIND_BUILDKIT_CACHE:
                return []
            assert query["kind"] == "squashfs"
            return [
                {
                    "blob_digest": runtime_digest,
                    "kind": "squashfs",
                    "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                    "chain_id": query["chain_id"],
                    "base_image_digest": runtime_base,
                    "arch": "x86_64",
                }
            ]

        def pull_blob(self, digest: str, destination: Path):
            hub_calls.append(("pull_blob", digest))
            assert digest == runtime_digest
            destination.write_bytes(runtime_blob)

        def push_blob(self, path: Path, metadata: dict[str, object]):
            hub_calls.append(("push_blob", metadata))
            if metadata["kind"] == "squashfs":
                assert metadata["name"] == "hub-runtime"
                assert metadata["chain_id"].startswith("sha256:")
                assert digest_file(path) == runtime_digest
            else:
                assert metadata["kind"] == KIND_BUILDKIT_CACHE
            return {"blob_digest": digest_file(path)}

    def expected_manifest_bytes() -> bytes:
        manifest = {
            "schema": buildkit.RUNTIME_PACK_SCHEMA,
            "base_image_digest": runtime_base,
            "platform": "linux/amd64",
            "arch": "x86_64",
            "source_rootfs_tar_digest": digest_file(spec.runtime_rootfs_archive),
            "source_oci_manifest_digest": D_IMAGE,
            "filesystem": "squashfs",
            "compression": "zstd",
            "compression_level": 3,
            "block_size": 131072,
            "root_uid": 0,
            "root_gid": 0,
            "root_mode": "0755",
            "readonly": True,
            **buildkit._injected_runner_packer_identity("4.7.5").manifest_binding(),
        }
        return buildkit._canonical_json(buildkit._bound_runtime_manifest(manifest, "4.7.5")) + b"\n"

    def runner(argv, **kwargs):
        command = [os.fspath(part) for part in argv]
        if command == ["mksquashfs", "-version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="mksquashfs version 4.7.5 (test)\n",
                stderr="",
            )
        if command[0] == "mksquashfs":
            raise AssertionError("Hub runtime conversion hit unexpectedly invoked mksquashfs")
        if command[0] == "unsquashfs":
            return subprocess.CompletedProcess(command, 0, stdout=expected_manifest_bytes(), stderr=b"")
        return buildx(argv, **kwargs)

    record = build_with_buildkit(spec, roots, hub_client=RuntimeHitHub(), runner=runner)

    assert record["runtime_cache_source"] == "hub"
    assert record["runtime_block_digest"] == runtime_digest
    assert read_tag_record(roots, "hub-runtime").digest == runtime_digest
    metadata = ContentStore(roots.store).read_metadata(runtime_digest)
    assert metadata["runtime_pack_manifest_digest"] == record["runtime_pack_manifest_digest"]
    assert [name for name, _ in hub_calls].count("pull_blob") == 1
    assert [name for name, _ in hub_calls].count("push_blob") == 2
