"""Prepare the build half of the OCI-root two-host acceptance gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

_SCHEMA = "palimpsest.oci-root-build-run-acceptance.v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="local OCI layout path pinned with @sha256:<manifest>")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--platform", default="linux/amd64")
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    archive = output_dir / "image.oci.tar"
    rootfs = output_dir / "rootfs-proof"
    marker = "palimpsest-local-build-" + uuid.uuid4().hex

    with tempfile.TemporaryDirectory(prefix="palimpsest-oci-root-e2e-") as temporary:
        context = Path(temporary)
        (context / "root-marker").write_text(marker + "\n", encoding="utf-8")
        probe = context / "palimpsest-e2e-probe"
        probe.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            f'test "$(cat /palimpsest-e2e-root-marker)" = {marker!r}\n'
            f'test "$(cat /proc/1/root/palimpsest-e2e-root-marker)" = {marker!r}\n'
            f"printf 'PALIMPSEST_OCI_ROOT_OK:{marker}\\n'\n",
            encoding="utf-8",
        )
        probe.chmod(0o755)
        (context / "Dockerfile").write_text(
            "FROM base\n"
            "COPY root-marker /palimpsest-e2e-root-marker\n"
            "COPY palimpsest-e2e-probe /usr/local/bin/palimpsest-e2e-probe\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "palimpsest_local.cli",
                "build",
                os.fspath(context),
                "--frontend",
                "dockerfile",
                "--tag",
                f"local/oci-root-e2e-{uuid.uuid4().hex[:12]}:latest",
                "--offline",
                "--network",
                "none",
                "--no-cache",
                "--platform",
                args.platform,
                "--local-image",
                f"base={args.base}",
                "--output",
                os.fspath(archive),
                "--rootfs-output",
                os.fspath(rootfs),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode
    if (rootfs / "palimpsest-e2e-root-marker").read_text(encoding="utf-8") != marker + "\n":
        raise RuntimeError("Palimpsest rootfs export does not contain the acceptance marker")
    receipt = {
        "archive_sha256": f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}",
        "manifest_digest": completed.stdout.strip(),
        "marker": marker,
        "platform": args.platform,
        "schema": _SCHEMA,
    }
    (output_dir / "acceptance.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
