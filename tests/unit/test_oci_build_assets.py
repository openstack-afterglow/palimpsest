"""Local evidence/graph boundaries; fake conversion is not a source-build proof."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local.buildkit import BuildKitSpec, compute_build_key
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.layer_cache import LayerCache
from palimpsest_local.nova_assets import AssetCatalog
from palimpsest_local.oci_build_assets import LocalBuildEvidence, register_local_oci_build
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
)

HANDOFF = "build/out/data-linux-amd64.tar.zst"


def _sha(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _archive(path, *, member_path="payload", payload=b"hello", hardlink=False, upper_marker=None):
    """A real OCI archive containing two occurrences of one compressed layer."""
    files = {}

    def blob(value, media_type):
        payload = value if isinstance(value, bytes) else json.dumps(value).encode()
        digest = _sha(payload)
        files["blobs/sha256/" + digest.split(":")[1]] = payload
        return {"mediaType": media_type, "digest": digest, "size": len(payload)}

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as stream:
        member = tarfile.TarInfo("carrier" if hardlink else member_path)
        member.size = len(payload)
        stream.addfile(member, io.BytesIO(payload))
        if hardlink:
            alias = tarfile.TarInfo(member_path)
            alias.type = tarfile.LNKTYPE
            alias.linkname = "carrier"
            stream.addfile(alias)
    layer = blob(gzip.compress(raw.getvalue(), mtime=0), OCI_LAYER_GZIP_MEDIA_TYPE)
    layers = [layer, layer]
    diff_ids = [_sha(raw.getvalue())] * 2
    if upper_marker is not None:
        upper = io.BytesIO()
        with tarfile.open(fileobj=upper, mode="w") as stream:
            stream.addfile(tarfile.TarInfo(upper_marker), io.BytesIO(b""))
        layers.append(blob(gzip.compress(upper.getvalue(), mtime=0), OCI_LAYER_GZIP_MEDIA_TYPE))
        diff_ids.append(_sha(upper.getvalue()))
    config = blob(
        {
            "os": "linux",
            "architecture": "amd64",
            "config": {"Cmd": ["/bin/true"]},
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        },
        OCI_IMAGE_CONFIG_MEDIA_TYPE,
    )
    manifest = blob(
        {"schemaVersion": 2, "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE, "config": config, "layers": layers},
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    files["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
    files["index.json"] = json.dumps(
        {"schemaVersion": 2, "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE, "manifests": [manifest]}
    ).encode()
    with tarfile.open(path, "w") as stream:
        for name, payload in files.items():
            member = tarfile.TarInfo(name)
            member.mode = 0o644
            member.size = len(payload)
            stream.addfile(member, io.BytesIO(payload))
    return manifest["digest"]


def _evidence(spec, root, path):
    record = {
        "schema_version": 2,
        "engine": "buildkit",
        "status": "success",
        "builder_fingerprint": "fixture-builder",
        "platform": spec.platform,
        "build_key": compute_build_key(spec, builder_fingerprint="fixture-builder"),
        "output_path": str(spec.output),
        "registry_image_pushed": False,
        "output_oci_archive_digest": _sha(spec.output.read_bytes()),
        "output_oci_manifest_digest": root,
        "bytes": {"oci_archive": spec.output.stat().st_size},
    }
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    return LocalBuildEvidence(spec, path, _sha(path.read_bytes()))


def _builds(tmp_path):
    source = tmp_path / "source"
    assembly = tmp_path / "assembly"
    export = tmp_path / "result"
    for root in (source, assembly):
        (root / "package").mkdir(parents=True)
        (root / "Dockerfile").write_text("FROM scratch AS result\n")
        (root / "package/Dockerfile").write_text("FROM scratch\nCOPY build/out /opt\n")
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TEMPLATE_DIR": "",
    }
    subprocess.run(["git", "init", "--quiet", str(source)], check=True, capture_output=True, env=environment)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True, env=environment)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "source fixture",
        ],
        check=True,
        capture_output=True,
        env=environment,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True, env=environment
    ).strip()
    for root in (assembly, export):
        (root / HANDOFF).parent.mkdir(parents=True)
        (root / HANDOFF).write_bytes(b"source-built-archive-fixture")
    first = BuildKitSpec(
        context=source,
        dockerfile=source / "Dockerfile",
        tag="source:fixture",
        output=tmp_path / "source.oci.tar",
        rootfs_output=export,
        target="result",
        build_args=("COMMIT=" + commit,),
        cache_mode="local",
    )
    second = BuildKitSpec(
        context=assembly,
        dockerfile=assembly / "package/Dockerfile",
        tag="runtime:fixture",
        output=tmp_path / "runtime.oci.tar",
        cache_mode="local",
    )
    source_root = _archive(first.output, member_path=HANDOFF, payload=b"source-built-archive-fixture")
    runtime_root = _archive(second.output)
    cache_root = tmp_path / "cache"
    catalog_root = tmp_path / "assets"
    cache_root.mkdir(mode=0o700)
    catalog_root.mkdir(mode=0o700)
    return {
        "cache": LayerCache(cache_root, initialize=True),
        "assets": AssetCatalog(catalog_root, "project-one", initialize=True),
        "source_build": _evidence(first, source_root, tmp_path / "source-record.json"),
        "runtime_build": _evidence(second, runtime_root, tmp_path / "runtime-record.json"),
        "source_commit": commit,
        "handoff_path": HANDOFF,
        "name": "source-built",
    }


def _fake_conversion(monkeypatch, cache, *, mutate=None, timestamp=0):
    """Replace only the hard-worker boundary; source import and catalog are real."""
    payload = b"unit-conversion".ljust(64, b"\0")
    used = 96 + len(payload)
    header = struct.pack(
        "<5I6H8Q",
        0x73717368,
        1,
        timestamp,
        131072,
        0,
        1,
        17,
        0,
        1,
        4,
        0,
        0,
        used,
        144,
        2**64 - 1,
        96,
        112,
        2**64 - 1,
        2**64 - 1,
    )
    content = header + payload + b"\0" * (((used + 511) // 512) * 512 - used)
    digest = _sha(content)
    path = cache.roots.store / "blobs/sha256" / digest.split(":")[1]
    path.write_bytes(content)
    path.chmod(0o400)

    def materialize(root, **_kwargs):
        imported = cache.inspect(root)
        image = imported["image"]
        recipe_digest = _sha(b"conversion-recipe")
        binding = _sha(b"host-snapshot")
        results = [
            {
                "cache_result": "warm_hit",
                "receipt": {
                    "ordinal": ordinal,
                    "source_image_digest": root,
                    "source_snapshot_binding_digest": binding,
                    "key_digest": recipe_digest,
                    "image_digest": digest,
                    "image_size": len(content),
                },
            }
            for ordinal in range(len(image["layers"]))
        ]
        receipt = {
            "root_descriptor": image["root_descriptor"],
            "manifest_digest": image["manifest_descriptor"]["digest"],
            "config_descriptor": image["config_descriptor"],
            "platform": {"os": "linux", "architecture": "amd64"},
            "source_image_digest": root,
            "source_snapshot_binding_digest": binding,
            "layers": copy.deepcopy(image["layers"]),
            "results": results,
        }
        layer = image["layers"][0]
        value = {
            **imported,
            "receipt": receipt,
            "blob_paths": [str(path)] * len(results),
            "derived": {
                recipe_digest: {
                    "compressed_digest": layer["compressed"]["digest"],
                    "diff_id": layer["diff_id"],
                    "image_digest": digest,
                    "image_size": len(content),
                }
            },
        }
        if mutate is not None:
            mutate(value)
        return value

    monkeypatch.setattr(cache, "materialize", materialize)
    return path


def test_paired_build_roundtrip_uses_original_canonical_paths_and_occurrences(tmp_path, monkeypatch):
    args = _builds(tmp_path)
    path = _fake_conversion(monkeypatch, args["cache"])
    before = path.stat().st_ino, path.read_bytes()
    registered = register_local_oci_build(**args)
    descriptor = registered["descriptor"]
    assert descriptor["source"]["kind"] == "oci-build"
    assert descriptor["source"]["source_commit"] == args["source_commit"]
    assert descriptor["source"]["build_result_digest"] == args["runtime_build"].record_digest
    first, second = descriptor["layers"]
    assert first["digest"] == second["digest"]
    assert first["occurrence_id"] != second["occurrence_id"]
    assert second["parent_occurrence"] == first["occurrence_id"]
    assert registered["blobs"] == {first["digest"]: {"path": str(path)}}
    reopened = AssetCatalog(args["assets"].root, "project-one")
    assert reopened.inspect("source-built", verify=True)["assets"][registered["asset_id"]]["descriptor"] == descriptor
    assert register_local_oci_build(**args)["asset_id"] == registered["asset_id"]
    assert (path.stat().st_ino, path.read_bytes()) == before


@pytest.mark.parametrize("change", ["commit", "record", "context", "archive", "manifest", "handoff", "dockerfile"])
def test_wrong_build_provenance_refuses_asset_publication(tmp_path, change):
    args = _builds(tmp_path)
    runtime = args["runtime_build"]
    source = args["source_build"]
    if change == "commit":
        args["source_commit"] = "f" * 40
    elif change == "record":
        args["runtime_build"] = replace(runtime, record_digest="sha256:" + "f" * 64)
    elif change == "context":
        (runtime.spec.context / "unrecorded").write_text("changed")
    elif change == "archive":
        runtime.spec.output.write_bytes(b"not the exported archive")
    elif change == "manifest":
        record = json.loads(runtime.record_path.read_bytes())
        record["output_oci_manifest_digest"] = "sha256:" + "f" * 64
        runtime.record_path.write_text(json.dumps(record))
        args["runtime_build"] = replace(runtime, record_digest=_sha(runtime.record_path.read_bytes()))
    elif change == "handoff":
        (source.spec.rootfs_output / HANDOFF).write_bytes(b"different generated source artifact")
    else:
        runtime.spec.dockerfile.write_text("FROM scratch\n# not upstream package\n")
        args["runtime_build"] = _evidence(
            runtime.spec,
            json.loads(runtime.record_path.read_bytes())["output_oci_manifest_digest"],
            runtime.record_path,
        )
    before = (args["assets"].root / "assets.json").read_bytes()
    with pytest.raises((ArtifactValidationError, FileNotFoundError)):
        register_local_oci_build(**args)
    assert (args["assets"].root / "assets.json").read_bytes() == before


@pytest.mark.parametrize("change", ["order", "config", "recipe", "path", "snapshot"])
def test_wrong_materialized_graph_cannot_register(tmp_path, monkeypatch, change):
    args = _builds(tmp_path)

    def mutate(value):
        if change == "order":
            value["receipt"]["results"].reverse()
        elif change == "config":
            value["receipt"]["config_descriptor"] = {"digest": "sha256:" + "e" * 64}
        elif change == "recipe":
            next(iter(value["derived"].values()))["diff_id"] = "sha256:" + "e" * 64
        elif change == "path":
            value["blob_paths"][0] = str(tmp_path / "other-store")
        else:
            value["receipt"]["results"][0]["receipt"]["source_snapshot_binding_digest"] = "sha256:" + "e" * 64

    _fake_conversion(monkeypatch, args["cache"], mutate=mutate)
    with pytest.raises(ArtifactValidationError, match="materialized"):
        register_local_oci_build(**args)
    assert args["assets"].inspect()["assets"] == {}


def test_build_oci_remains_strictly_zero_time_normalized(tmp_path, monkeypatch):
    args = _builds(tmp_path)
    path = _fake_conversion(monkeypatch, args["cache"], timestamp=1790000000)
    before = path.stat().st_ino, path.read_bytes()
    with pytest.raises(ArtifactValidationError, match="deterministic time"):
        register_local_oci_build(**args)
    assert args["assets"].inspect()["assets"] == {}
    assert (path.stat().st_ino, path.read_bytes()) == before


def test_source_kind_requires_complete_build_and_occurrence_provenance(tmp_path, monkeypatch):
    args = _builds(tmp_path)
    _fake_conversion(monkeypatch, args["cache"])
    record = register_local_oci_build(**args)
    layers = [
        {key: node[key] for key in ("digest", "size", "compressed_digest", "diff_id", "recipe_digest")}
        for node in record["descriptor"]["layers"]
    ]
    paths = [Path(record["blobs"][node["digest"]]["path"]) for node in layers]
    source = record["descriptor"]["source"]
    incomplete = {key: value for key, value in source.items() if key != "source_build_result_digest"}
    with pytest.raises(ArtifactValidationError, match="paired build"):
        args["assets"].register(arch="x86_64", source=incomplete, layers=layers, blob_paths=paths)
    with pytest.raises(ArtifactValidationError, match="OCI occurrences"):
        args["assets"].register(
            arch="x86_64",
            source=source,
            layers=[{"digest": layers[0]["digest"], "size": layers[0]["size"]}],
            blob_paths=paths[:1],
        )


def test_evidence_change_during_conversion_refuses_registration(tmp_path, monkeypatch):
    args = _builds(tmp_path)

    def mutate(_value):
        (args["source_build"].spec.context / "source-replaced").write_text("changed")

    _fake_conversion(monkeypatch, args["cache"], mutate=mutate)
    with pytest.raises(ArtifactValidationError, match="context"):
        register_local_oci_build(**args)
    assert args["assets"].inspect()["assets"] == {}


def test_replaced_export_with_same_source_build_key_is_not_source_output(tmp_path):
    args = _builds(tmp_path)
    source = args["source_build"]
    runtime = args["runtime_build"]
    replacement = tmp_path / "unrelated-export"
    (replacement / HANDOFF).parent.mkdir(parents=True)
    (replacement / HANDOFF).write_bytes(b"downloaded-release-artifact")
    (runtime.spec.context / HANDOFF).write_bytes(b"downloaded-release-artifact")
    altered = replace(source.spec, rootfs_output=replacement)
    assert compute_build_key(altered, builder_fingerprint="fixture-builder") == compute_build_key(
        source.spec, builder_fingerprint="fixture-builder"
    )
    args["source_build"] = replace(source, spec=altered)
    root = json.loads(runtime.record_path.read_bytes())["output_oci_manifest_digest"]
    args["runtime_build"] = _evidence(runtime.spec, root, runtime.record_path)
    with pytest.raises(ArtifactValidationError, match="successful source OCI output"):
        register_local_oci_build(**args)
    assert args["assets"].inspect()["assets"] == {}


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("mksquashfs") is None,
    reason="real canonical hard materializer requires Linux and mksquashfs >=4.6",
)
def test_real_oci_fixture_materialization_registers_canonical_paths_without_replacement(tmp_path):
    """Real source import/hard conversion/catalog; receipts remain constructed fixtures."""
    args = _builds(tmp_path)
    packer = Path(shutil.which("mksquashfs")).resolve()
    result = register_local_oci_build(**args, packer_path=packer)
    paths = [Path(value["path"]) for value in result["blobs"].values()]
    before = {str(path): (path.stat().st_ino, path.read_bytes()) for path in paths}
    assert args["assets"].inspect(result["asset_id"], verify=True)["verified"] is True
    repeated = register_local_oci_build(**args, packer_path=packer)
    assert repeated["asset_id"] == result["asset_id"]
    assert all(value["cache_result"] == "warm_hit" for value in repeated["materialization"]["results"])
    assert {str(path): (path.stat().st_ino, path.read_bytes()) for path in paths} == before
    for path in paths:
        assert path.parent == args["cache"].roots.store / "blobs/sha256"


def test_source_oci_hardlink_handoff_uses_validated_carrier_bytes(tmp_path, monkeypatch):
    args = _builds(tmp_path)
    source = args["source_build"]
    root = _archive(source.spec.output, member_path=HANDOFF, payload=b"source-built-archive-fixture", hardlink=True)
    args["source_build"] = _evidence(source.spec, root, source.record_path)
    _fake_conversion(monkeypatch, args["cache"])
    registered = register_local_oci_build(**args)
    assert registered["descriptor"]["source"]["handoff_digest"] == _sha(b"source-built-archive-fixture")


@pytest.mark.parametrize("marker", ["build/.wh.out", "build/out/.wh..wh..opq"])
def test_source_oci_ancestor_whiteout_or_opaque_hides_lower_handoff(tmp_path, marker):
    args = _builds(tmp_path)
    source = args["source_build"]
    root = _archive(
        source.spec.output, member_path=HANDOFF, payload=b"source-built-archive-fixture", upper_marker=marker
    )
    args["source_build"] = _evidence(source.spec, root, source.record_path)
    with pytest.raises(ArtifactValidationError, match="does not contain the regular handoff"):
        register_local_oci_build(**args)
    assert args["assets"].inspect()["assets"] == {}


@pytest.mark.parametrize("change", ["untracked", "ignored", "tracked", "wrong-head", "missing-git"])
def test_source_commit_requires_real_pristine_matching_checkout(tmp_path, change):
    args = _builds(tmp_path)
    evidence = args["source_build"]
    context = evidence.spec.context
    if change == "untracked":
        (context / "untracked-source").write_text("not in source commit")
    elif change == "ignored":
        (context / ".git/info").mkdir(exist_ok=True)
        (context / ".git/info/exclude").write_text("ignored-source\n")
        (context / "ignored-source").write_text("ignored but uncommitted build input")
    elif change == "tracked":
        evidence.spec.dockerfile.write_text("FROM scratch\n# not committed\n")
    elif change == "wrong-head":
        args["source_commit"] = "f" * 40
        altered = replace(evidence.spec, build_args=("COMMIT=" + args["source_commit"],))
        root = json.loads(evidence.record_path.read_bytes())["output_oci_manifest_digest"]
        args["source_build"] = _evidence(altered, root, evidence.record_path)
    else:
        (context / ".git").rename(tmp_path / "removed-git")
    if change in {"untracked", "ignored", "tracked", "missing-git"}:
        root = json.loads(evidence.record_path.read_bytes())["output_oci_manifest_digest"]
        args["source_build"] = _evidence(evidence.spec, root, evidence.record_path)
    with pytest.raises(ArtifactValidationError, match="source context"):
        register_local_oci_build(**args)
    assert args["assets"].inspect()["assets"] == {}
