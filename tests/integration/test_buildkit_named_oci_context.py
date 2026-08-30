"""Real BuildKit acceptance for a digest-pinned local OCI named context."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
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


def _layer_tar() -> bytes:
    payload = b"palimpsest named OCI context\n"
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


def _write_layout(root: Path) -> str:
    (root / "blobs" / "sha256").mkdir(parents=True)
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8")
    layer = _layer_tar()
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
