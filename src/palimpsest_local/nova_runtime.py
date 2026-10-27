"""Prepare immutable lower-store policy with consumer-private writable runtime views.

Run on the single-writer worker before clean unmount/provider read-only sealing.
This creates only empty mountpoints and an account-free cloud-init manifest; it
never mounts writable ext4 on multiple instances or copies canonical layer blobs.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from .errors import ArtifactValidationError
from .external_layers import RUNTIME_ROOT, SCHEMA, _validate, build_external_layer_user_data
from .nova_assets import AssetCatalog


def _directory(path: Path) -> None:
    for component in reversed((path, *path.parents)):
        if not component.exists() and not component.is_symlink():
            component.mkdir(mode=0o755)
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ArtifactValidationError("runtime preparation requires root-owned non-writable physical directories")


def prepare_runtime(catalog: AssetCatalog, config: dict) -> dict:
    required = {"filesystem_uuid", "stacks"}
    optional = {
        "exports",
        "paths",
        "library_paths",
        "activation_commands",
        "checks",
        "command_links",
        "consumer_state_root",
    }
    if not isinstance(config, dict) or not required <= config.keys() or set(config) - required - optional:
        raise ArtifactValidationError("runtime preparation configuration fields are invalid")
    if os.geteuid() != 0:
        raise ArtifactValidationError("runtime mountpoint preparation runs as root only on the single-writer worker")
    root = Path(RUNTIME_ROOT)
    mount = subprocess.run(
        ["findmnt", "--json", "--output", "TARGET,FSTYPE,UUID", "--mountpoint", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    entries = json.loads(mount.stdout).get("filesystems", [])
    if (
        len(entries) != 1
        or entries[0].get("target") != str(root)
        or entries[0].get("fstype") != "ext4"
        or entries[0].get("uuid") != config["filesystem_uuid"]
    ):
        raise ArtifactValidationError(
            "runtime preparation requires the complete expected UUID-bound external ext4 mount"
        )
    requests = config["stacks"]
    if not isinstance(requests, dict) or not requests:
        raise ArtifactValidationError("runtime stacks must map explicit names to immutable asset IDs")
    manifest = {
        **config,
        "schema": SCHEMA,
        "storage_read_only": True,
        "stacks": [],
        "blob_paths": {},
    }
    arch = None
    for name, identity in requests.items():
        result = catalog.inspect(identity, verify=True)
        record = next(iter(result["assets"].values()))
        descriptor = record["descriptor"]
        if arch not in (None, descriptor["arch"]):
            raise ArtifactValidationError("runtime stacks have incompatible target architectures")
        arch = descriptor["arch"]
        manifest["stacks"].append({"name": name, "layers": [layer["digest"] for layer in descriptor["layers"]]})
        for layer in descriptor["layers"]:
            path = Path(record["blobs"][layer["digest"]]["path"])
            try:
                relative = path.relative_to(root)
            except ValueError:
                raise ArtifactValidationError("asset payload is not on the consumer's external filesystem") from None
            if path.stat().st_dev != root.stat().st_dev:
                raise ArtifactValidationError("asset payload is on a different filesystem")
            previous = manifest["blob_paths"].get(layer["digest"], str(relative))
            if previous != str(relative):
                raise ArtifactValidationError("one content digest has inconsistent physical storage paths")
            manifest["blob_paths"][layer["digest"]] = str(relative)
    manifest["arch"] = arch
    manifest = _validate(manifest)
    # Validate all policy before making any filesystem additions. Canonical cache
    # bytes stay at their original paths/inodes, with no hardlinks changing nlink.
    for stack in manifest["stacks"]:
        for digest in stack["layers"]:
            _directory(root / "layers" / digest.split(":")[1])
        _directory(root / "runtimes" / stack["name"])
    return {
        "manifest": manifest,
        "cloud_init": build_external_layer_user_data(manifest),
        "phase": "prepared-not-provider-sealed",
        "retention": "permanent",
    }
