"""Real BuildKit acceptance for a digest-pinned local OCI named context."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.buildkit_e2e,
    pytest.mark.skipif(
        os.environ.get("PALIMPSEST_BUILDKIT_E2E") != "1",
        reason="set PALIMPSEST_BUILDKIT_E2E=1 to run real BuildKit acceptance",
    ),
]


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _write_blob(layout: Path, payload: bytes) -> str:
    digest = _digest(payload)
    (layout / "blobs" / "sha256" / digest.split(":", 1)[1]).write_bytes(payload)
    return digest


def _layer_tar(payload: bytes = b"palimpsest named OCI context\n") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("sentinel.txt")
        member.size = len(payload)
        member.mode = 0o644
        member.uid = 0
        member.gid = 0
        member.mtime = 0
        archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def _write_layout(root: Path, payload: bytes = b"palimpsest named OCI context\n") -> str:
    (root / "blobs" / "sha256").mkdir(parents=True)
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8")
    layer = _layer_tar(payload)
    layer_digest = _write_blob(root, layer)
    config = json.dumps(
        {
            "architecture": "amd64",
            "config": {},
            "os": "linux",
            "rootfs": {"diff_ids": [layer_digest], "type": "layers"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    config_digest = _write_blob(root, config)
    manifest = json.dumps(
        {
            "config": {
                "digest": config_digest,
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config),
            },
            "layers": [
                {
                    "digest": layer_digest,
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "size": len(layer),
                }
            ],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_digest = _write_blob(root, manifest)
    index = json.dumps(
        {
            "manifests": [
                {
                    "digest": manifest_digest,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": {"architecture": "amd64", "os": "linux"},
                    "size": len(manifest),
                }
            ],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    (root / "index.json").write_text(index + "\n", encoding="utf-8")
    return manifest_digest


def _verified_archive_manifest(archive: Path, expected_sentinel: bytes) -> str:
    members: dict[str, bytes] = {}
    with tarfile.open(archive, mode="r:") as outer:
        for member in outer.getmembers():
            if not member.isfile():
                continue
            if member.name in members or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise AssertionError(f"unsafe or duplicate OCI archive member: {member.name}")
            stream = outer.extractfile(member)
            assert stream is not None
            members[member.name] = stream.read()

    assert json.loads(members["oci-layout"])["imageLayoutVersion"] == "1.0.0"
    index = json.loads(members["index.json"])
    assert index["schemaVersion"] == 2
    assert index["mediaType"] == "application/vnd.oci.image.index.v1+json"
    assert len(index["manifests"]) == 1

    def verified_blob(descriptor: dict[str, object]) -> bytes:
        digest = descriptor["digest"]
        size = descriptor["size"]
        assert isinstance(digest, str) and digest.startswith("sha256:")
        assert type(size) is int
        payload = members[f"blobs/sha256/{digest.removeprefix('sha256:')}"]
        assert len(payload) == size
        assert f"sha256:{hashlib.sha256(payload).hexdigest()}" == digest
        return payload

    manifest_descriptor = index["manifests"][0]
    assert manifest_descriptor["mediaType"] == "application/vnd.oci.image.manifest.v1+json"
    manifest = json.loads(verified_blob(manifest_descriptor))
    assert manifest["schemaVersion"] == 2
    assert manifest["mediaType"] == "application/vnd.oci.image.manifest.v1+json"
    assert manifest["config"]["mediaType"] == "application/vnd.oci.image.config.v1+json"
    config = json.loads(verified_blob(manifest["config"]))
    assert config["os"] == "linux"
    assert config["architecture"] == "amd64"
    assert config["rootfs"]["type"] == "layers"
    assert len(config["rootfs"]["diff_ids"]) == len(manifest["layers"])
    sentinel_found = False
    for ordinal, layer_descriptor in enumerate(manifest["layers"]):
        layer = verified_blob(layer_descriptor)
        media_type = layer_descriptor["mediaType"]
        if media_type == "application/vnd.oci.image.layer.v1.tar+gzip":
            uncompressed = gzip.decompress(layer)
        else:
            assert media_type == "application/vnd.oci.image.layer.v1.tar"
            uncompressed = layer
        assert config["rootfs"]["diff_ids"][ordinal] == f"sha256:{hashlib.sha256(uncompressed).hexdigest()}"
        with tarfile.open(fileobj=io.BytesIO(uncompressed), mode="r:") as filesystem:
            try:
                sentinel = filesystem.extractfile("sentinel.txt")
            except KeyError:
                continue
            assert sentinel is not None
            assert sentinel.read() == expected_sentinel
            sentinel_found = True
    assert sentinel_found
    return manifest_descriptor["digest"]


def _rewrite_with_invalid_diff_id(source: Path, target: Path) -> None:
    with tarfile.open(source, mode="r:") as archive:
        members = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and archive.extractfile(member) is not None
        }
    index = json.loads(members["index.json"])
    manifest_descriptor = index["manifests"][0]
    manifest_path = f"blobs/sha256/{manifest_descriptor['digest'].removeprefix('sha256:')}"
    manifest = json.loads(members[manifest_path])
    config_descriptor = manifest["config"]
    config_path = f"blobs/sha256/{config_descriptor['digest'].removeprefix('sha256:')}"
    config = json.loads(members[config_path])
    config["rootfs"]["diff_ids"][0] = "sha256:" + "0" * 64
    config_payload = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_digest = f"sha256:{hashlib.sha256(config_payload).hexdigest()}"
    members[f"blobs/sha256/{config_digest.removeprefix('sha256:')}"] = config_payload
    manifest["config"] = {**config_descriptor, "digest": config_digest, "size": len(config_payload)}
    manifest_payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_digest = f"sha256:{hashlib.sha256(manifest_payload).hexdigest()}"
    members[f"blobs/sha256/{manifest_digest.removeprefix('sha256:')}"] = manifest_payload
    index["manifests"][0] = {
        **manifest_descriptor,
        "digest": manifest_digest,
        "size": len(manifest_payload),
    }
    members["index.json"] = json.dumps(index, sort_keys=True, separators=(",", ":")).encode()
    with tarfile.open(target, mode="w") as archive:
        for name, payload in sorted(members.items()):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def test_real_buildkit_named_oci_context_extracts_pinned_layer(tmp_path: Path) -> None:
    layout = tmp_path / "layout"
    manifest_digest = _write_layout(layout)
    context = tmp_path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "FROM scratch\nCOPY --from=base /sentinel.txt /sentinel.txt\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    builder = os.environ.get("PALIMPSEST_BUILDKIT_BUILDER", "palimpsest-e2e")
    command = [
        "docker",
        "buildx",
        "build",
        "--builder",
        builder,
        "--build-context",
        f"base=oci-layout://{layout}@{manifest_digest}",
        "--network",
        "none",
        "--platform",
        "linux/amd64",
        "--output",
        f"type=local,dest={output}",
        os.fspath(context),
    ]

    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)

    assert completed.returncode == 0, completed.stderr
    assert (output / "sentinel.txt").read_bytes() == b"palimpsest named OCI context\n"


def test_palimpsest_cli_builds_a_pinned_local_image_and_exports_its_rootfs(tmp_path: Path) -> None:
    """Product-level build gate; OCI-root VM execution is a later, separate gate."""
    layout = tmp_path / "layout"
    sentinel = f"palimpsest-cli-{uuid.uuid4().hex}\n".encode()
    manifest_digest = _write_layout(layout, sentinel)
    context = tmp_path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "FROM scratch\nCOPY --from=base /sentinel.txt /sentinel.txt\n",
        encoding="utf-8",
    )
    archive = tmp_path / "palimpsest-output.oci.tar"
    rootfs = tmp_path / "rootfs"
    xdg_config = tmp_path / "xdg-config"
    xdg_state = tmp_path / "xdg-state"
    environment = {
        **os.environ,
        "BUILDX_BUILDER": os.environ.get("PALIMPSEST_BUILDKIT_BUILDER", "palimpsest-e2e"),
        "XDG_CONFIG_HOME": os.fspath(xdg_config),
        "XDG_STATE_HOME": os.fspath(xdg_state),
    }
    command = [
        sys.executable,
        "-m",
        "palimpsest_local.cli",
        "build",
        os.fspath(context),
        "--frontend",
        "dockerfile",
        "--tag",
        "local/palimpsest-e2e:latest",
        "--offline",
        "--network",
        "none",
        "--platform",
        "linux/amd64",
        "--local-image",
        f"base={layout}@{manifest_digest}",
        "--output",
        os.fspath(archive),
        "--rootfs-output",
        os.fspath(rootfs),
        "--no-cache",
    ]

    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=180, env=environment)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == _verified_archive_manifest(archive, sentinel)
    assert archive.is_file() and archive.stat().st_size > 0
    assert (rootfs / "sentinel.txt").read_bytes() == sentinel
    records = list((xdg_state / "palimpsest" / "builds").glob("bk-*/record.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "success"
    assert record["mode"] == "offline"
    assert record["network"] == "none"
    assert record["local_image_digests"] == {"base": manifest_digest}
    assert record["output_oci_manifest_digest"] == completed.stdout.strip()
    assert record["output_oci_archive_digest"] == f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"
    assert record["output_path"] == os.fspath(archive)
    invalid_archive = tmp_path / "invalid-diff-id.oci.tar"
    _rewrite_with_invalid_diff_id(archive, invalid_archive)
    with pytest.raises(AssertionError):
        _verified_archive_manifest(invalid_archive, sentinel)
