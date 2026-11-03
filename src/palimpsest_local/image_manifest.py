"""Canonical image metadata: logical occurrences are distinct from shared payloads.

Only SHA-256 content identities cross the publication boundary. No transport or
cloud dependency is needed to construct the exact descriptor before upload.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

SCHEMA = "palimpsest.image-manifest.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _digest(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("invalid SHA256 identity")
    return value


def _text(value, maximum=4096):
    if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > maximum:
        raise ValueError("invalid or oversized manifest string")
    return value


def _json(value, depth=0):
    if depth > 12:
        raise ValueError("manifest metadata is too deeply nested")
    if value is None or type(value) in (str, bool, int):
        if isinstance(value, str):
            _text(value, 16384)
        if type(value) is int and abs(value) > 2**63 - 1:
            raise ValueError("oversized manifest integer")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list) and len(value) <= 1024:
        return [_json(item, depth + 1) for item in value]
    if isinstance(value, dict) and len(value) <= 256:
        return {_text(key, 256): _json(item, depth + 1) for key, item in value.items()}
    raise ValueError("invalid manifest JSON value")


def _hash(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        ).hexdigest()
    )


def normalize_manifest(value: dict) -> dict:
    """Validate and copy a complete descriptor, deriving its single-parent chain."""
    keys = {
        "schema",
        "mode",
        "platform",
        "os_family",
        "os_version",
        "base_image_digest",
        "runtime",
        "source",
        "metadata",
        "layers",
    }
    if not isinstance(value, dict) or set(value) - keys or value.get("schema") != SCHEMA:
        raise ValueError("unsupported image manifest schema or fields")
    if value.get("mode") not in ("oci-root", "application") or value.get("platform") != "linux/amd64":
        raise ValueError("unsupported image execution compatibility")
    result = {key: value[key] for key in ("schema", "mode", "platform")}
    for key in ("os_family", "os_version"):
        result[key] = _text(value.get(key), 128)
        if not result[key].strip():
            raise ValueError("explicit OS family and version are required")
    base = value.get("base_image_digest")
    if result["mode"] == "application":
        base = _digest(base)
    elif base is not None:
        raise ValueError("oci-root must not declare a cloud base")
    result["base_image_digest"] = base
    runtime = value.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {"entrypoint", "cmd", "env", "working_dir", "user"}:
        raise ValueError("complete runtime descriptor is required")
    normalized_runtime = {}
    for key, maximum in (("entrypoint", 256), ("cmd", 256), ("env", 1024)):
        items = runtime[key]
        if not isinstance(items, list) or len(items) > maximum:
            raise ValueError("invalid runtime array")
        normalized_runtime[key] = [_text(item, 16384) for item in items]
    for key in ("working_dir", "user"):
        normalized_runtime[key] = _text(runtime[key])
    result["runtime"] = normalized_runtime
    source = value.get("source")
    source_keys = {
        "kind",
        "reference",
        "manifest_digest",
        "config_digest",
        "root_digest",
        "repository",
        "registry",
        "diff_ids",
        "metadata",
    }
    if not isinstance(source, dict) or source.get("kind") not in ("local", "registry") or set(source) - source_keys:
        raise ValueError("invalid image provenance")
    result["source"] = _json(source)
    for key in ("reference", "registry", "repository"):
        if key in source:
            _text(source[key], 512)
    for key in ("manifest_digest", "config_digest", "root_digest"):
        if key in source:
            _digest(source[key])
    if "diff_ids" in source:
        if not isinstance(source["diff_ids"], list):
            raise ValueError("invalid DiffID provenance")
        for item in source["diff_ids"]:
            _digest(item)
    result["metadata"] = _json(value.get("metadata", {}))
    if not isinstance(result["metadata"], dict):
        raise ValueError("metadata must be an object")
    layers = value.get("layers")
    if not isinstance(layers, list) or not 1 <= len(layers) <= 128:
        raise ValueError("image requires one to 128 ordered layers")
    result["layers"] = []
    parent = None
    sizes = {}
    compatibility = {key: result[key] for key in ("mode", "platform", "os_family", "os_version", "base_image_digest")}
    for layer in layers:
        if not isinstance(layer, dict) or set(layer) - {"digest", "size_bytes", "id", "parent_id", "source"}:
            raise ValueError("invalid logical layer descriptor")
        digest, size = _digest(layer.get("digest")), layer.get("size_bytes")
        if type(size) is not int or not 1 <= size <= 2**63 - 1:
            raise ValueError("layer size must be a positive bounded integer")
        if digest in sizes and sizes[digest] != size:
            raise ValueError("shared payload digest has contradictory sizes")
        sizes[digest] = size
        identity = _hash(
            {
                "schema": "palimpsest.layer-node.v1",
                "parent_id": parent,
                "digest": digest,
                "size_bytes": size,
                **compatibility,
            }
        )
        if ("id" in layer and layer["id"] != identity) or ("parent_id" in layer and layer["parent_id"] != parent):
            raise ValueError("logical layer identity contradicts its parent chain")
        item = {"id": identity, "parent_id": parent, "digest": digest, "size_bytes": size}
        if "source" in layer:
            item["source"] = _json(layer["source"])
        result["layers"].append(item)
        parent = identity
    if len(json.dumps(result, ensure_ascii=True).encode()) > 1024 * 1024:
        raise ValueError("image manifest exceeds byte limit")
    return result


def manifest_digest(manifest: dict) -> str:
    return _hash(normalize_manifest(manifest))


def public_manifest(manifest: dict) -> dict:
    """Project-visible descriptive metadata with private runtime values removed.

    Metadata is explicitly public-within-project label data, not secret storage.
    Raw source documents and conversion receipts stay private; only specific
    provenance fields are projected. This is NOT the re-hashable full manifest.
    """
    full = normalize_manifest(manifest)
    runtime = full["runtime"]
    layers = []
    for item in full["layers"]:
        layer = {key: item[key] for key in ("id", "parent_id", "digest", "size_bytes")}
        source = item.get("source", {})
        if isinstance(source, dict):
            provenance = {
                key: source[key]
                for key in (
                    "compressed_digest",
                    "compressed_size",
                    "media_type",
                    "diff_id",
                    "key_digest",
                    "image_digest",
                    "occurrence_digest",
                    "record_digest",
                )
                if key in source
            }
            oci = source.get("oci_layer", {})
            if isinstance(oci, dict):
                if "diff_id" in oci:
                    provenance["diff_id"] = oci["diff_id"]
                compressed = oci.get("compressed", {})
                if isinstance(compressed, dict):
                    provenance.update(
                        {
                            key: compressed[field]
                            for key, field in (
                                ("compressed_digest", "digest"),
                                ("compressed_size", "size"),
                                ("media_type", "mediaType"),
                            )
                            if field in compressed
                        }
                    )
            conversion = source.get("conversion", {})
            receipt = conversion.get("receipt", {}) if isinstance(conversion, dict) else {}
            if isinstance(receipt, dict):
                provenance.update(
                    {
                        key: receipt[key]
                        for key in (
                            "key_digest",
                            "image_digest",
                            "image_size",
                            "occurrence_digest",
                            "record_digest",
                        )
                        if key in receipt
                    }
                )
            provenance = {
                key: value
                for key, value in provenance.items()
                if (
                    (key in ("compressed_size", "image_size") and type(value) is int and 0 < value <= 2**63 - 1)
                    or (key == "media_type" and isinstance(value, str) and len(value) <= 256)
                    or (
                        key not in ("compressed_size", "image_size", "media_type")
                        and isinstance(value, str)
                        and _DIGEST.fullmatch(value)
                    )
                )
            }
            if provenance:
                layer["source"] = provenance
        layers.append(layer)
    return {
        **{
            key: full[key]
            for key in ("schema", "mode", "platform", "os_family", "os_version", "base_image_digest", "metadata")
        },
        "projection": "public",
        "runtime": {key: runtime[key] for key in ("entrypoint", "cmd", "working_dir", "user")}
        | {
            "env_names": [
                item.partition("=")[0]
                for item in runtime["env"]
                if "=" in item and item.partition("=")[0].isascii() and item.partition("=")[0].isidentifier()
            ]
        },
        "source": {key: value for key, value in full["source"].items() if key != "metadata"},
        "layers": layers,
    }


def native_image_projection(manifest: dict) -> dict:
    full = normalize_manifest(manifest)
    return {
        "source": {"kind": "published", "manifest_digest": manifest_digest(full), "manifest": full},
        "runtime": full["runtime"],
        "layers": [
            {"digest": item["digest"], "size": item["size_bytes"], "ordinal": ordinal}
            for ordinal, item in enumerate(full["layers"])
        ],
    }


def manifest_from_result(
    result: dict,
    *,
    os_family: str,
    os_version: str,
    source_kind: str,
    metadata: dict | None = None,
    mode: str = "oci-root",
    base_image_digest: str | None = None,
) -> dict:
    """Build a canonical descriptor from real materialized output or local artifacts.

    The caller supplies actual OS information (or explicit ``unknown``), never a
    guessed distribution. Optional per-layer source/receipt metadata is retained.
    """
    source = dict(result.get("source", {}))
    source["kind"] = source_kind
    return normalize_manifest(
        {
            "schema": SCHEMA,
            "mode": mode,
            "platform": "linux/amd64",
            "os_family": os_family,
            "os_version": os_version,
            "base_image_digest": base_image_digest,
            "runtime": result["runtime"],
            "source": source,
            "metadata": metadata or {},
            "layers": result["layers"],
        }
    )
