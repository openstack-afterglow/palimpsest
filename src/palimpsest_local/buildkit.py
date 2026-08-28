"""BuildKit-backed Dockerfile builds with verified Hub and offline cache paths.

BuildKit cache records and VM runtime artifacts deliberately have different
lifecycles:

* BuildKit's local cache exporter is archived for transport through the
  content-addressed Palimpsest Hub.  BuildKit remains the authority that
  validates and consumes those cache records.
* A merged rootfs export can be packed into one immutable SquashFS image.  The
  existing runtime attaches that image as a read-only ``virtio-blk`` disk.

Online builds always resolve the Hub before invoking the builder.  Hub errors
or corrupt cache archives fail closed; only an explicit empty result is a
cache miss.  Offline builds never construct or call a Hub client and only
accept local OCI image-layout named contexts.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import state
from .digest import digest_file, require_digest, require_file_digest
from .errors import ArtifactValidationError, BuildError, HubError
from .hub import KIND_BUILDKIT_CACHE, MEDIA_TYPE_BUILDKIT_CACHE
from .oci_layout import MEDIA_TYPE_LAYER_SQUASHFS, ContentStore
from .registry import RegistryError, validate_cache_spec
from .state import TagRecord, validate_tag, write_tag_record

BUILD_KEY_SCHEMA = "palimpsest-buildkit-cache-v1"
CACHE_ARCHIVE_SCHEMA = "palimpsest-buildkit-cache-archive-v1"
RUNTIME_PACK_SCHEMA = "palimpsest-runtime-pack-v2"
RUNTIME_MANIFEST_PATH = ".palimpsest/runtime-pack.json"

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")
_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,47}$")
_PLATFORM_RE = re.compile(r"^linux/(amd64|arm64)(?:/[a-z0-9_.-]+)?$")
_FROM_RE = re.compile(r"^FROM\s+(?:(?:--[a-zA-Z0-9_-]+=[^\s]+)\s+)*([^\s]+)(?:\s+AS\s+([^\s]+))?\s*$", re.I)
_REMOTE_ADD_RE = re.compile(r"^ADD\s+(?:--[^\s]+\s+)*(?:https?|git)://", re.I)
_REMOTE_SOURCE_RE = re.compile(r"(?i)(?:https?|git|ssh)://")
_SCP_GIT_RE = re.compile(r"(?i)(?:^|[\s\"'\[])git@[a-z0-9.-]+:[^\s\"'\]]+(?:[#\s\"'\]]|$)")
_REMOTE_GIT_RE = re.compile(r"(?i)(?:git(?:\+https?)?|ssh)://|https?://[^\s\"'\]]+\.git(?:[#?\s\"'\]]|$)")
_ENCODED_SOURCE_RE = re.compile(r"\\(?:u[0-9a-f]{4}|x[0-9a-f]{2})", re.IGNORECASE)
_PINNED_REMOTE_REF_RE = re.compile(r"@sha256:[0-9a-f]{64}$", re.I)
_ADD_CHECKSUM_OPTION_RE = re.compile(r"^--checksum=sha256:[0-9a-f]{64}$", re.I)
_PARSER_DIRECTIVE_RE = re.compile(r"^#\s*(syntax|escape)\s*=\s*(.*?)\s*$", re.I)
_CACHE_GENERATION_RE = re.compile(r"^bk-[0-9a-f]{12}$")
_BUILDX_DRIVER_RE = re.compile(r"^Driver:\s*(\S+)\s*$", re.MULTILINE)
_BUILDX_NAME_RE = re.compile(r"^Name:\s*(\S+)\s*$", re.MULTILINE)
_BUILDKIT_VERSION_RE = re.compile(r"^BuildKit(?:\s+version)?:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)
_OCI_EXPORT_BUILDX_DRIVERS = frozenset({"docker-container", "kubernetes", "remote"})
_CACHE_POINTER_SCHEMA_VERSION = 1
_MAX_CACHE_MEMBERS = 250_000
_MAX_CACHE_BYTES = 32 * 1024 * 1024 * 1024
_MAX_OCI_DESCRIPTORS = 100_000
_MAX_RUNTIME_MANIFEST_BYTES = 64 * 1024
_READ_CHUNK = 1024 * 1024


def image_arch_for_platform(platform: str) -> str:
    """Map a supported BuildKit platform to the cloud-image architecture vocabulary."""
    if _PLATFORM_RE.fullmatch(platform) is None:
        raise ArtifactValidationError("platform must be linux/amd64 or linux/arm64 (optional variant allowed)")
    return {"amd64": "x86_64", "arm64": "aarch64"}[platform.split("/", 2)[1]]


def _oci_blob_path(layout: Path, digest: str) -> Path:
    normalized = require_digest(digest)
    return layout / "blobs" / "sha256" / normalized.split(":", 1)[1]


def _verify_oci_blob(layout: Path, descriptor: dict[str, Any], seen: set[str]) -> tuple[str, Path]:
    raw_digest = descriptor.get("digest")
    raw_size = descriptor.get("size")
    if not isinstance(raw_digest, str) or not isinstance(raw_size, int) or raw_size < 0:
        raise ArtifactValidationError("OCI descriptor must contain a digest and nonnegative size")
    digest = require_digest(raw_digest)
    blob = _oci_blob_path(layout, digest)
    if not blob.is_file():
        raise ArtifactValidationError(f"OCI layout is missing referenced blob {digest}")
    if blob.stat().st_size != raw_size:
        raise ArtifactValidationError(f"OCI descriptor size mismatch for {digest}")
    if digest not in seen:
        require_file_digest(blob, digest)
        seen.add(digest)
        if len(seen) > _MAX_OCI_DESCRIPTORS:
            raise ArtifactValidationError("OCI layout references too many descriptors")
    return digest, blob


def _verify_oci_manifest_graph(layout: Path, root_digest: str) -> None:
    """Verify every config/layer reachable from a pinned OCI manifest or index."""
    seen: set[str] = set()
    visited_json: set[str] = set()

    def visit_json_descriptor(descriptor: dict[str, Any]) -> None:
        digest, blob = _verify_oci_blob(layout, descriptor, seen)
        if digest in visited_json:
            return
        visited_json.add(digest)
        try:
            document = json.loads(blob.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError(f"OCI image descriptor {digest} is not valid JSON") from exc
        if not isinstance(document, dict) or document.get("schemaVersion") != 2:
            raise ArtifactValidationError(f"OCI image descriptor {digest} is not schemaVersion 2")
        if isinstance(document.get("manifests"), list):
            for child in document["manifests"]:
                if not isinstance(child, dict):
                    raise ArtifactValidationError(f"OCI index {digest} contains a malformed manifest descriptor")
                visit_json_descriptor(child)
            return
        config = document.get("config")
        layers = document.get("layers")
        if not isinstance(config, dict) or not isinstance(layers, list):
            raise ArtifactValidationError(f"OCI descriptor {digest} is neither an image manifest nor an index")
        config_digest, config_blob = _verify_oci_blob(layout, config, seen)
        try:
            image_config = json.loads(config_blob.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError(f"OCI image config {config_digest} is not valid JSON") from exc
        if not isinstance(image_config, dict):
            raise ArtifactValidationError(f"OCI image config {config_digest} must be a JSON object")
        runtime_config = image_config.get("config")
        if runtime_config is not None and not isinstance(runtime_config, dict):
            raise ArtifactValidationError(f"OCI image config {config_digest} has a malformed config object")
        onbuild = runtime_config.get("OnBuild") if isinstance(runtime_config, dict) else None
        if onbuild is not None and not isinstance(onbuild, list):
            raise ArtifactValidationError(f"OCI image config {config_digest} has a malformed OnBuild field")
        if onbuild:
            raise ArtifactValidationError(
                f"OCI image config {config_digest} contains OnBuild triggers, which local named contexts forbid"
            )
        for layer in layers:
            if not isinstance(layer, dict):
                raise ArtifactValidationError(f"OCI manifest {digest} contains a malformed layer descriptor")
            _verify_oci_blob(layout, layer, seen)

    root_blob = _oci_blob_path(layout, root_digest)
    if not root_blob.is_file():
        raise ArtifactValidationError(f"OCI context does not contain referenced descriptor {root_digest}")
    visit_json_descriptor({"digest": root_digest, "size": root_blob.stat().st_size})


@dataclass(frozen=True)
class NamedOCIContext:
    """A named BuildKit context backed by a verified local OCI image layout."""

    alias: str
    layout: Path
    manifest_digest: str

    def __post_init__(self) -> None:
        alias = self.alias.strip().lower()
        if _ALIAS_RE.fullmatch(alias) is None:
            raise ArtifactValidationError(f"invalid local OCI context alias: {self.alias!r}")
        layout = self.layout.expanduser().resolve()
        if not layout.is_dir():
            raise ArtifactValidationError(f"local OCI context is not a directory: {layout}")
        for required in (layout / "oci-layout", layout / "index.json"):
            if not required.is_file():
                raise ArtifactValidationError(f"local OCI context is missing {required.name}: {layout}")
        try:
            layout_marker = json.loads((layout / "oci-layout").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError(f"invalid oci-layout file: {layout}") from exc
        if layout_marker != {"imageLayoutVersion": "1.0.0"}:
            raise ArtifactValidationError(f"unsupported OCI image layout version: {layout_marker!r}")

        digest = require_digest(self.manifest_digest)
        _verify_oci_manifest_graph(layout, digest)

        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "layout", layout)
        object.__setattr__(self, "manifest_digest", digest)

    @classmethod
    def parse(cls, value: str) -> NamedOCIContext:
        """Parse ``alias=/absolute/layout@sha256:<digest>``."""
        if "=" not in value:
            raise ArtifactValidationError("--local-image must use alias=/path@sha256:<digest>")
        alias, raw_reference = value.split("=", 1)
        if "@" not in raw_reference:
            raise ArtifactValidationError("--local-image must pin an OCI descriptor with @sha256:<digest>")
        raw_path, digest = raw_reference.rsplit("@", 1)
        if not raw_path:
            raise ArtifactValidationError("--local-image path must be nonempty")
        return cls(alias=alias, layout=Path(raw_path), manifest_digest=digest)

    @property
    def buildx_value(self) -> str:
        return f"oci-layout://{self.layout}@{self.manifest_digest}"


@dataclass(frozen=True)
class BuildKitSpec:
    """A complete, reproducible BuildKit build request."""

    context: Path
    dockerfile: Path
    tag: str
    output: Path
    rootfs_output: Path | None = None
    runtime_rootfs_archive: Path | None = None
    platform: str = "linux/amd64"
    target: str | None = None
    build_args: tuple[str, ...] = ()
    local_images: tuple[NamedOCIContext, ...] = ()
    network: str = "default"
    offline: bool = False
    no_cache: bool = False
    push_cache: bool = True
    cache_scope: str = "default"
    runtime_tag: str | None = None
    runtime_base_digest: str | None = None
    runtime_block_size: int = 131072
    additional_tags: tuple[str, ...] = ()
    pull: bool = False
    load: bool = False
    push_image: bool = False
    external_cache_from: tuple[str, ...] = ()
    external_cache_to: tuple[str, ...] = ()
    registry_profile: str | None = None
    registry_config_digest: str | None = None
    progress: str = "plain"
    push: bool = False

    def __post_init__(self) -> None:
        context = self.context.expanduser().resolve()
        dockerfile = self.dockerfile.expanduser().resolve()
        output = self.output.expanduser().resolve()
        rootfs_output = self.rootfs_output.expanduser().resolve() if self.rootfs_output is not None else None
        runtime_rootfs_archive = (
            self.runtime_rootfs_archive.expanduser().resolve() if self.runtime_rootfs_archive is not None else None
        )
        if not context.is_dir():
            raise ArtifactValidationError(f"BuildKit context is not a directory: {context}")
        if not dockerfile.is_file():
            raise ArtifactValidationError(f"Dockerfile not found: {dockerfile}")
        tags = (self.tag, *self.additional_tags)
        if any(not tag or any(char in tag for char in "\r\n\0") for tag in tags):
            raise ArtifactValidationError("BuildKit tags must be nonempty and single-line")
        if len(set(tags)) != len(tags):
            raise ArtifactValidationError("BuildKit tags must be unique")
        image_arch_for_platform(self.platform)
        if self.network not in {"none", "default"}:
            raise ArtifactValidationError("BuildKit network must be 'none' or 'default'")
        if _SCOPE_RE.fullmatch(self.cache_scope) is None:
            raise ArtifactValidationError("cache scope must match ^[a-z0-9][a-z0-9.-]{0,47}$")
        if self.offline and self.network != "none":
            raise ArtifactValidationError("offline BuildKit builds require --network none")
        if self.offline and self.push:
            raise ArtifactValidationError("offline BuildKit builds cannot upload artifacts")
        if self.offline and self.push_image:
            raise ArtifactValidationError("offline BuildKit builds cannot push OCI images")
        if self.offline and self.pull:
            raise ArtifactValidationError("offline BuildKit builds cannot use --pull")
        if self.offline and (self.external_cache_from or self.external_cache_to):
            raise ArtifactValidationError("offline BuildKit builds cannot use external cache backends")
        if self.progress not in {"auto", "none", "plain", "quiet", "rawjson", "tty"}:
            raise ArtifactValidationError("unsupported BuildKit progress mode")
        for cache_spec in (*self.external_cache_from, *self.external_cache_to):
            _validate_external_cache_spec(cache_spec)
        if self.registry_profile is not None and (
            not self.registry_profile or any(character in self.registry_profile for character in "\r\n\0")
        ):
            raise ArtifactValidationError("registry profile must be nonempty and single-line")
        if self.registry_config_digest is not None:
            object.__setattr__(self, "registry_config_digest", require_digest(self.registry_config_digest))
        if self.no_cache and not self.offline:
            raise ArtifactValidationError("--no-cache is incompatible with mandatory online Hub cache reuse")
        if self.runtime_block_size < 4096 or self.runtime_block_size > 1024 * 1024:
            raise ArtifactValidationError("runtime block size must be between 4096 and 1048576 bytes")
        if self.runtime_block_size & (self.runtime_block_size - 1):
            raise ArtifactValidationError("runtime block size must be a power of two")
        if self.runtime_tag is not None:
            validate_tag(self.runtime_tag)
            if runtime_rootfs_archive is None:
                raise ArtifactValidationError("runtime_tag requires a metadata-preserving runtime rootfs archive")
            if self.runtime_base_digest is None:
                raise ArtifactValidationError("runtime_tag requires runtime_base_digest for stack verification")
        if self.runtime_base_digest is not None:
            object.__setattr__(self, "runtime_base_digest", require_digest(self.runtime_base_digest))
        aliases = [item.alias for item in self.local_images]
        if len(set(aliases)) != len(aliases):
            raise ArtifactValidationError("local OCI context aliases must be unique")
        for item in self.build_args:
            key, separator, _value = item.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ArtifactValidationError("build args must use NAME=VALUE syntax")
            if key.upper() == "BUILDKIT_SYNTAX":
                raise ArtifactValidationError(
                    "BUILDKIT_SYNTAX cannot override Palimpsest's verified builtin Dockerfile frontend"
                )

        for generated in (output, rootfs_output, runtime_rootfs_archive):
            if generated is None:
                continue
            try:
                generated.relative_to(context)
            except ValueError:
                pass
            else:
                raise ArtifactValidationError("BuildKit output paths must be outside the build context")

        object.__setattr__(self, "context", context)
        object.__setattr__(self, "dockerfile", dockerfile)
        object.__setattr__(self, "output", output)
        object.__setattr__(self, "rootfs_output", rootfs_output)
        object.__setattr__(self, "runtime_rootfs_archive", runtime_rootfs_archive)


def _validate_external_cache_spec(value: str) -> None:
    """Accept Docker cache backend syntax without permitting inline credentials."""
    try:
        validate_cache_spec(value)
    except RegistryError as exc:
        raise ArtifactValidationError(str(exc)) from exc


def _cache_spec_receipt(value: str) -> dict[str, str]:
    digest = f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
    return {"spec_digest": digest}


def _hash_file(path: Path, hasher: Any) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            hasher.update(chunk)


def compute_context_digest(context: Path, *, exclude: Iterable[Path] = ()) -> str:
    """Conservatively hash an entire context tree in stable path order.

    This deliberately over-invalidates relative to ``.dockerignore`` rather
    than risk a false cache hit.  A future LLB-aware index can narrow the input
    set without changing BuildKit's cache authority.
    """
    root = context.expanduser().resolve()
    if not root.is_dir():
        raise ArtifactValidationError(f"build context is not a directory: {root}")
    excluded = {item.expanduser().resolve() for item in exclude}
    hasher = hashlib.sha256()
    hasher.update(b"palimpsest-context-v1\0")

    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().encode("utf-8")):
        resolved_parent = path.parent.resolve()
        if any(path.resolve(strict=False) == item or item in path.resolve(strict=False).parents for item in excluded):
            continue
        if any(resolved_parent == item or item in resolved_parent.parents for item in excluded):
            continue
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            kind = b"d"
            payload = b""
        elif stat.S_ISREG(info.st_mode):
            kind = b"f"
            payload = None
        elif stat.S_ISLNK(info.st_mode):
            kind = b"l"
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
        else:
            raise ArtifactValidationError(f"unsupported special file in build context: {path}")
        hasher.update(kind + b"\0")
        hasher.update(relative.encode("utf-8", errors="surrogateescape") + b"\0")
        hasher.update(f"{mode:o}".encode("ascii") + b"\0")
        if payload is None:
            hasher.update(str(info.st_size).encode("ascii") + b"\0")
            _hash_file(path, hasher)
        else:
            hasher.update(payload)
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_build_key(spec: BuildKitSpec, *, builder_fingerprint: str = "unresolved") -> str:
    """Compute the pre-build lookup key; raw build-arg values are never persisted."""
    context_digest = compute_context_digest(spec.context)
    dockerfile_digest = digest_file(spec.dockerfile)
    build_args_digest = f"sha256:{hashlib.sha256(_canonical_json(list(spec.build_args))).hexdigest()}"
    definition = {
        "schema": BUILD_KEY_SCHEMA,
        "frontend": "dockerfile.v0-builtin",
        "context_digest": context_digest,
        "dockerfile_digest": dockerfile_digest,
        "platform": spec.platform,
        "target": spec.target,
        "network": spec.network,
        "cache_scope": spec.cache_scope,
        "build_args_digest": build_args_digest,
        "local_images": [
            {"alias": item.alias, "manifest_digest": item.manifest_digest}
            for item in sorted(spec.local_images, key=lambda x: x.alias)
        ],
        "builder_contract": "docker-buildx-local-cache-v1",
        "builder_fingerprint": builder_fingerprint,
    }
    return f"sha256:{hashlib.sha256(_canonical_json(definition)).hexdigest()}"


def _logical_dockerfile_lines(path: Path) -> list[str]:
    try:
        # Docker's parser discards one UTF-8 BOM from the first line before it
        # interprets parser directives; mirror that behavior for policy checks.
        raw_lines = path.read_text(encoding="utf-8-sig").split("\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise ArtifactValidationError(f"cannot read Dockerfile as UTF-8: {path}") from exc
    logical: list[str] = []
    current: str | None = None
    for raw in raw_lines:
        if raw.endswith("\r"):
            raw = raw[:-1]
        stripped = raw.strip(" \t")
        if not stripped or stripped.startswith("#"):
            if current is None and _PARSER_DIRECTIVE_RE.fullmatch(stripped):
                logical.append(stripped)
            continue
        # BuildKit removes the escape plus newline without inventing a space.
        # Preserve an explicit space before the escape and the next line's
        # actual indentation; otherwise `ht\` + `tps://` would be misparsed.
        current = stripped if current is None else current + raw.rstrip(" \t")
        continuation_candidate = current.rstrip(" \t")
        if continuation_candidate.endswith("\\") and not continuation_candidate.endswith("\\\\"):
            current = continuation_candidate[:-1]
            continue
        logical.append(current.strip(" \t"))
        current = None
    if current is not None:
        logical.append(current.strip(" \t"))
    return logical


def _is_instruction(line: str, instruction: str) -> bool:
    return re.match(rf"^{re.escape(instruction)}\s+", line, re.IGNORECASE) is not None


def _instruction_leading_options(line: str, instruction: str) -> tuple[str, ...]:
    """Return exact leading Dockerfile flag tokens, stopping at bare ``--``."""
    remainder = re.sub(rf"^{re.escape(instruction)}\s+", "", line, count=1, flags=re.IGNORECASE)
    options: list[str] = []
    while remainder.startswith("--"):
        token_match = re.match(r"(\S+)(?:\s+|$)", remainder)
        if token_match is None:  # pragma: no cover - defensive; \S+ covers nonempty input
            break
        token = token_match.group(1)
        if any(character in token for character in ("\\", "'", '"')):
            raise ArtifactValidationError(
                f"{instruction} leading flags cannot use quotes or backslash escapes; use canonical --name=value syntax"
            )
        if token == "--":
            break
        options.append(token)
        remainder = remainder[token_match.end() :].lstrip()
    return tuple(options)


def _add_has_pinned_checksum_option(line: str) -> bool:
    """Inspect only ADD's leading option tokens, never source/destination text."""
    return any(_ADD_CHECKSUM_OPTION_RE.fullmatch(token) for token in _instruction_leading_options(line, "ADD"))


def _copy_from_sources(line: str) -> tuple[str, ...]:
    instruction = "ADD" if _is_instruction(line, "ADD") else "COPY"
    return tuple(
        token.split("=", 1)[1]
        for token in _instruction_leading_options(line, instruction)
        if token.lower().startswith("--from=")
    )


def validate_offline_dockerfile(spec: BuildKitSpec) -> None:
    """Reject every Dockerfile construct that could resolve a remote source."""
    if not spec.offline:
        return
    local_aliases = {item.alias.lower() for item in spec.local_images}
    stages: set[str] = set()
    stage_count = 0
    for line in _logical_dockerfile_lines(spec.dockerfile):
        directive = _PARSER_DIRECTIVE_RE.fullmatch(line)
        if directive:
            if directive.group(1).lower() == "syntax":
                raise ArtifactValidationError("offline Dockerfiles cannot use an external # syntax= frontend")
            raise ArtifactValidationError("offline Dockerfiles cannot change the parser escape directive")
        if _is_instruction(line, "FROM") and any(character in line for character in ("\\", "'", '"')):
            raise ArtifactValidationError("offline FROM instructions cannot use quotes or backslash escapes")
        from_match = _FROM_RE.fullmatch(line)
        if from_match:
            source, stage_alias = from_match.groups()
            lowered = source.lower()
            if "$" in source:
                raise ArtifactValidationError("offline FROM values cannot be expanded from ARG variables")
            if lowered != "scratch" and lowered not in local_aliases and lowered not in stages:
                if not (lowered.isdigit() and int(lowered) < stage_count):
                    raise ArtifactValidationError(
                        f"offline Dockerfile FROM {source!r} is not scratch, a prior stage, or a --local-image alias"
                    )
            if stage_alias:
                alias = stage_alias.lower()
                if _ALIAS_RE.fullmatch(alias) is None:
                    raise ArtifactValidationError(f"invalid Dockerfile stage alias: {stage_alias!r}")
                stages.add(alias)
            stages.add(str(stage_count))
            stage_count += 1
            continue
        if _is_instruction(line, "ADD"):
            if (
                "$" in line
                or _ENCODED_SOURCE_RE.search(line)
                or _REMOTE_ADD_RE.match(line)
                or _REMOTE_SOURCE_RE.search(line)
                or _SCP_GIT_RE.search(line)
            ):
                raise ArtifactValidationError(
                    "offline Dockerfiles cannot ADD dynamic, remote URL, or remote Git sources"
                )
        for raw_source in _copy_from_sources(line):
            source = raw_source.lower()
            if source not in local_aliases and source not in stages:
                raise ArtifactValidationError(
                    f"offline COPY/ADD --from={source} does not name a local context or stage"
                )
        if _is_instruction(line, "RUN"):
            run_options = _instruction_leading_options(line, "RUN")
            for option in run_options:
                if option.lower().startswith("--network=") and option.split("=", 1)[1].lower() != "none":
                    raise ArtifactValidationError("offline RUN instructions may only use --network=none")
            for mount_value in (
                option.split("=", 1)[1] for option in run_options if option.lower().startswith("--mount=")
            ):
                fields: dict[str, str] = {}
                for field in mount_value.strip("\"'").split(","):
                    key, separator, value = field.partition("=")
                    if separator:
                        fields[key.strip().lower()] = value.strip().strip("\"'")
                source = fields.get("from")
                if source is None:
                    continue
                lowered = source.lower()
                if "$" in source or (lowered not in local_aliases and lowered not in stages):
                    raise ArtifactValidationError(
                        f"offline RUN --mount from={source!r} does not name a local context or prior stage"
                    )
    if stage_count == 0:
        raise ArtifactValidationError("Dockerfile must contain at least one FROM instruction")


def validate_online_dockerfile(spec: BuildKitSpec) -> None:
    """Require immutable registry inputs before using a pre-build Hub cache key."""
    if spec.offline:
        return
    local_aliases = {item.alias.lower() for item in spec.local_images}
    stages: set[str] = set()
    stage_count = 0
    for line in _logical_dockerfile_lines(spec.dockerfile):
        directive = _PARSER_DIRECTIVE_RE.fullmatch(line)
        if directive:
            if directive.group(1).lower() == "escape":
                raise ArtifactValidationError("online Dockerfiles cannot change the parser escape directive")
            frontend = directive.group(2)
            if _PINNED_REMOTE_REF_RE.search(frontend) is None:
                raise ArtifactValidationError(
                    "online Dockerfile # syntax= frontends must be pinned with @sha256:<64hex>"
                )
            continue
        if _is_instruction(line, "FROM") and any(character in line for character in ("\\", "'", '"')):
            raise ArtifactValidationError("online FROM instructions cannot use quotes or backslash escapes")
        from_match = _FROM_RE.fullmatch(line)
        if from_match is not None:
            source, stage_alias = from_match.groups()
            lowered = source.lower()
            is_prior_stage = lowered in stages or (lowered.isdigit() and int(lowered) < stage_count)
            if "$" in source:
                raise ArtifactValidationError(
                    "online FROM values cannot be expanded from ARG variables; pin the image digest"
                )
            if lowered != "scratch" and lowered not in local_aliases and not is_prior_stage:
                if _PINNED_REMOTE_REF_RE.search(source) is None:
                    raise ArtifactValidationError(
                        f"online Dockerfile FROM {source!r} must be pinned with @sha256:<64hex>"
                    )
            if stage_alias:
                alias = stage_alias.lower()
                if _ALIAS_RE.fullmatch(alias) is None:
                    raise ArtifactValidationError(f"invalid Dockerfile stage alias: {stage_alias!r}")
                stages.add(alias)
            stages.add(str(stage_count))
            stage_count += 1
            continue

        if _is_instruction(line, "ADD"):
            if "$" in line or _ENCODED_SOURCE_RE.search(line):
                raise ArtifactValidationError("online ADD sources cannot be expanded or escape-encoded")
            if _SCP_GIT_RE.search(line) or _REMOTE_GIT_RE.search(line):
                raise ArtifactValidationError("online remote Git ADD is not accepted; vendor it into the context")
            if re.search(r"(?i)https?://", line) and not _add_has_pinned_checksum_option(line):
                raise ArtifactValidationError("online HTTP ADD must declare --checksum=sha256:<64hex>")

        for source in _copy_from_sources(line):
            lowered = source.lower()
            is_local = lowered in local_aliases or lowered in stages
            if "$" in source or (not is_local and _PINNED_REMOTE_REF_RE.search(source) is None):
                raise ArtifactValidationError(
                    f"online COPY/ADD --from={source} must name a local stage/context or digest-pinned image"
                )

        if _is_instruction(line, "RUN"):
            for mount_value in (
                option.split("=", 1)[1]
                for option in _instruction_leading_options(line, "RUN")
                if option.lower().startswith("--mount=")
            ):
                fields = {
                    key.strip().lower(): value.strip().strip("\"'")
                    for field in mount_value.strip("\"'").split(",")
                    for key, separator, value in [field.partition("=")]
                    if separator
                }
                source = fields.get("from")
                if source is None:
                    continue
                lowered = source.lower()
                is_local = lowered in local_aliases or lowered in stages
                if "$" in source or (not is_local and _PINNED_REMOTE_REF_RE.search(source) is None):
                    raise ArtifactValidationError(
                        f"online RUN --mount from={source!r} must name a local stage/context or digest-pinned image"
                    )
    if stage_count == 0:
        raise ArtifactValidationError("Dockerfile must contain at least one FROM instruction")


def preflight_buildx_oci_exporter(
    *,
    strict_offline: bool = False,
    environment_out: dict[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Verify that the currently selected Buildx builder can export OCI archives.

    The preflight is deliberately read-only: it does not create, bootstrap, or
    select a builder.  ``BUILDX_BUILDER`` and the user's existing Buildx
    selection therefore apply to both this inspection and the subsequent build.
    """
    command = ["docker", "buildx", "inspect"]
    setup_hint = (
        "Create an isolated capable builder with "
        "`docker buildx create --name palimpsest --driver docker-container`, then run with "
        "`BUILDX_BUILDER=palimpsest palimpsest build ...`; Palimpsest will not change the selected builder."
    )
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise BuildError("Docker CLI with the Buildx plugin is required for Dockerfile builds. " + setup_hint) from exc
    if getattr(result, "returncode", 0) != 0:
        stderr = str(getattr(result, "stderr", ""))[-4000:].strip()
        detail = f" Buildx reported: {stderr}" if stderr else ""
        raise BuildError(
            "cannot inspect the currently selected Docker Buildx builder; run `docker buildx ls` to diagnose it."
            f"{detail} {setup_hint}"
        )

    output = str(getattr(result, "stdout", ""))
    match = _BUILDX_DRIVER_RE.search(output)
    if match is None:
        raise BuildError(
            "docker buildx inspect did not report the selected builder driver; update the Buildx plugin. " + setup_hint
        )
    driver = match.group(1).lower()
    if driver not in _OCI_EXPORT_BUILDX_DRIVERS:
        supported = ", ".join(sorted(_OCI_EXPORT_BUILDX_DRIVERS))
        raise BuildError(
            f"selected Docker Buildx driver {driver!r} is not verified to support the OCI exporter; "
            f"use one of: {supported}. {setup_hint}"
        )
    name_match = _BUILDX_NAME_RE.search(output)
    if name_match is None:
        raise BuildError("docker buildx inspect did not report the selected builder name")
    builder_name = name_match.group(1)
    if strict_offline:
        if driver != "docker-container":
            raise BuildError(
                "strict offline builds require a local docker-container builder; remote and kubernetes builders "
                "cannot be proven network-isolated by this client"
            )
        nodes_marker = re.search(r"(?m)^Nodes:\s*$", output)
        if nodes_marker is None:
            raise BuildError("strict offline Buildx inspection did not report a Nodes section")
        nodes_output = output[nodes_marker.end() :]
        node_names = re.findall(r"(?m)^Name:\s*(\S+)\s*$", nodes_output)
        node_endpoints = re.findall(r"(?m)^Endpoint:\s*(\S+)\s*$", nodes_output)
        if len(node_names) != 1 or len(node_endpoints) != 1:
            raise BuildError("strict offline builds require exactly one local Buildx node and endpoint")

        current_context_result = _run_checked(
            runner,
            ["docker", "context", "show"],
            operation="offline Docker context inspection",
        )
        current_context = str(getattr(current_context_result, "stdout", "")).strip()
        if not current_context:
            raise BuildError("docker context show returned an empty context name")
        context_host_result = _run_checked(
            runner,
            [
                "docker",
                "context",
                "inspect",
                "--format",
                '{{(index .Endpoints "docker").Host}}',
                current_context,
            ],
            operation="offline Docker endpoint inspection",
        )
        context_host = str(getattr(context_host_result, "stdout", "")).strip()
        if not context_host.startswith(("unix://", "npipe://")):
            raise BuildError(
                f"strict offline builds require a local Unix/named-pipe Docker endpoint, got {context_host!r}"
            )
        if node_endpoints[0] not in {current_context, context_host}:
            raise BuildError(
                "strict offline Buildx node endpoint does not match the current local Docker context: "
                f"{node_endpoints[0]!r}"
            )
        containers_result = _run_checked(
            runner,
            [
                "docker",
                "ps",
                "--filter",
                f"name=^/buildx_buildkit_{re.escape(builder_name)}[0-9]+$",
                "--format",
                "{{.ID}}",
            ],
            operation="offline BuildKit container inspection",
        )
        container_ids = [
            line.strip() for line in str(getattr(containers_result, "stdout", "")).splitlines() if line.strip()
        ]
        if len(container_ids) != 1:
            raise BuildError(
                "the selected single-node offline builder must have exactly one running local BuildKit container; "
                "preload its image and bootstrap it before disconnecting the host"
            )
        for container_id in container_ids:
            network_result = _run_checked(
                runner,
                ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", container_id],
                operation="offline BuildKit network inspection",
            )
            network_mode = str(getattr(network_result, "stdout", "")).strip()
            if network_mode != "none":
                raise BuildError(
                    f"offline BuildKit container {container_id} uses Docker network mode {network_mode!r}; "
                    "recreate the selected builder with --driver-opt network=none"
                )
            attachments_result = _run_checked(
                runner,
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container_id],
                operation="offline BuildKit network attachment inspection",
            )
            try:
                attachments = json.loads(str(getattr(attachments_result, "stdout", "")).strip())
            except json.JSONDecodeError as exc:
                raise BuildError("Docker returned malformed BuildKit network attachment metadata") from exc
            if not isinstance(attachments, dict) or not set(attachments).issubset({"none"}):
                names = sorted(attachments) if isinstance(attachments, dict) else []
                raise BuildError(
                    f"offline BuildKit container {container_id} has active Docker network attachments: {names}"
                )
    if environment_out is not None:
        buildkit_versions = sorted(set(_BUILDKIT_VERSION_RE.findall(output)))
        if not buildkit_versions:
            raise BuildError(
                "docker buildx inspect did not report a running BuildKit version; bootstrap the selected builder "
                "before using content-addressed Hub cache keys"
            )
        version_result = _run_checked(
            runner,
            ["docker", "buildx", "version"],
            operation="docker buildx version inspection",
        )
        buildx_version = str(getattr(version_result, "stdout", "")).strip()
        if not buildx_version:
            raise BuildError("docker buildx version returned an empty version string")
        environment_payload = {
            "driver": driver,
            "buildx_version": buildx_version,
            "buildkit_versions": buildkit_versions,
        }
        environment_out.update(
            {
                "driver": driver,
                "builder_name": builder_name,
                "buildx_version": buildx_version,
                "buildkit_versions": ",".join(buildkit_versions),
                "fingerprint": f"sha256:{hashlib.sha256(_canonical_json(environment_payload)).hexdigest()}",
            }
        )
    return driver


def build_buildx_command(
    spec: BuildKitSpec,
    *,
    cache_from: Path | None,
    cache_to: Path,
    metadata_file: Path,
    builder_name: str | None = None,
) -> list[str]:
    """Return a shell-free ``docker buildx build`` argv."""
    command = [
        "docker",
        "buildx",
        "build",
    ]
    if builder_name is not None:
        command.extend(["--builder", builder_name])
    command.extend(
        [
            "--file",
            str(spec.dockerfile),
            "--tag",
            spec.tag,
            "--platform",
            spec.platform,
            "--network",
            spec.network,
            "--progress",
            spec.progress,
            "--provenance=false",
            "--sbom=false",
            "--metadata-file",
            str(metadata_file),
            "--output",
            f"type=oci,dest={spec.output}",
        ]
    )
    for tag in spec.additional_tags:
        command.extend(["--tag", tag])
    if spec.pull:
        command.append("--pull")
    if spec.load:
        command.extend(["--output", "type=docker"])
    if spec.push_image:
        command.extend(["--output", "type=registry"])
    if spec.rootfs_output is not None:
        command.extend(["--output", f"type=local,dest={spec.rootfs_output}"])
    if spec.runtime_rootfs_archive is not None:
        # The tar exporter retains numeric uid/gid, hardlinks, device nodes, and
        # PAX xattrs.  A host directory (type=local) cannot be the authority for
        # a VM rootfs because extraction changes ownership on unprivileged hosts.
        command.extend(["--output", f"type=tar,dest={spec.runtime_rootfs_archive}"])
    if spec.target:
        command.extend(["--target", spec.target])
    for item in spec.build_args:
        command.extend(["--build-arg", item])
    for item in sorted(spec.local_images, key=lambda entry: entry.alias):
        command.extend(["--build-context", f"{item.alias}={item.buildx_value}"])
    if spec.no_cache:
        command.append("--no-cache")
    elif cache_from is not None:
        command.extend(["--cache-from", f"type=local,src={cache_from}"])
    for cache_spec in spec.external_cache_from:
        command.extend(["--cache-from", cache_spec])
    command.extend(["--cache-to", f"type=local,dest={cache_to},mode=max"])
    for cache_spec in spec.external_cache_to:
        command.extend(["--cache-to", cache_spec])
    command.append(str(spec.context))
    return command


def _safe_tar_name(name: str) -> PurePosixPath:
    if not name or name.startswith("/") or "\\" in name or "\0" in name:
        raise ArtifactValidationError(f"unsafe cache archive member: {name!r}")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactValidationError(f"unsafe cache archive member: {name!r}")
    return path


def _tar_info(path: Path, archive_name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(archive_name)
    source = path.lstat()
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = stat.S_IMODE(source.st_mode)
    if stat.S_ISDIR(source.st_mode):
        info.type = tarfile.DIRTYPE
        info.size = 0
    elif stat.S_ISREG(source.st_mode):
        info.type = tarfile.REGTYPE
        info.size = source.st_size
    elif stat.S_ISLNK(source.st_mode):
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
        info.size = 0
    else:
        raise ArtifactValidationError(f"unsupported special file in cache directory: {path}")
    return info


def _add_tree(archive: tarfile.TarFile, source: Path, *, prefix: str = "") -> None:
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix().encode("utf-8")):
        relative = path.relative_to(source).as_posix()
        archive_name = f"{prefix}/{relative}" if prefix else relative
        info = _tar_info(path, archive_name)
        if info.isreg():
            with path.open("rb") as handle:
                archive.addfile(info, handle)
        else:
            archive.addfile(info)


def create_deterministic_tar(source: Path, destination: Path) -> Path:
    """Archive a directory with stable order, ownership, and timestamps."""
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        raise ArtifactValidationError(f"cache source is not a directory: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
            _add_tree(archive, source)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _create_cache_archive(cache_dir: Path, destination: Path, descriptor: dict[str, Any]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    descriptor_bytes = _canonical_json(descriptor) + b"\n"
    try:
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
            descriptor_info = tarfile.TarInfo("palimpsest-cache.json")
            descriptor_info.size = len(descriptor_bytes)
            descriptor_info.mode = 0o444
            descriptor_info.uid = descriptor_info.gid = descriptor_info.mtime = 0
            archive.addfile(descriptor_info, io.BytesIO(descriptor_bytes))
            cache_root = tarfile.TarInfo("cache")
            cache_root.type = tarfile.DIRTYPE
            cache_root.mode = 0o755
            cache_root.uid = cache_root.gid = cache_root.mtime = 0
            archive.addfile(cache_root)
            _add_tree(archive, cache_dir, prefix="cache")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def extract_cache_tar(
    archive_path: Path,
    destination: Path,
    *,
    expected_build_key: str | None = None,
    expected_scope: str | None = None,
    expected_platform: str | None = None,
    expected_builder_fingerprint: str | None = None,
) -> Path:
    """Stream-extract a plain or key-bound cache without loading its TOC into RAM."""
    archive_path = archive_path.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not archive_path.is_file():
        raise ArtifactValidationError(f"cache archive does not exist: {archive_path}")
    if expected_build_key is not None:
        expected_build_key = require_digest(expected_build_key)
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ArtifactValidationError(f"cache extraction target must be an empty directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)

    total_size = 0
    member_count = 0
    seen: set[str] = set()
    wrapped: bool | None = None
    descriptor_seen = False
    try:
        with tarfile.open(archive_path, "r|*") as archive:
            for member in archive:
                member_count += 1
                if member_count > _MAX_CACHE_MEMBERS:
                    raise ArtifactValidationError("cache archive has too many members")
                if wrapped is None:
                    wrapped = member.name == "palimpsest-cache.json"
                    if expected_build_key is not None and not wrapped:
                        raise ArtifactValidationError(
                            "an exact Hub cache hit must begin with a cache-key binding descriptor"
                        )
                if member.name == "palimpsest-cache.json":
                    if descriptor_seen or not wrapped or not member.isreg() or member.size > 64 * 1024:
                        raise ArtifactValidationError("cache archive must contain one valid leading descriptor")
                    descriptor_seen = True
                    descriptor_stream = archive.extractfile(member)
                    if descriptor_stream is None:
                        raise ArtifactValidationError("invalid cache archive descriptor")
                    try:
                        descriptor = json.load(descriptor_stream)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ArtifactValidationError("invalid cache archive descriptor JSON") from exc
                    if descriptor.get("schema") != CACHE_ARCHIVE_SCHEMA:
                        raise ArtifactValidationError("unsupported cache archive schema")
                    archive_key = require_digest(descriptor.get("build_key", ""))
                    if expected_build_key is not None and archive_key != expected_build_key:
                        raise ArtifactValidationError(
                            f"Hub cache key mismatch: expected {expected_build_key}, archive declares {archive_key}"
                        )
                    expected_fields = {
                        "cache_scope": expected_scope,
                        "platform": expected_platform,
                        "builder_fingerprint": expected_builder_fingerprint,
                    }
                    for field, expected in expected_fields.items():
                        if expected is not None and descriptor.get(field) != expected:
                            raise ArtifactValidationError(
                                f"Hub cache {field} mismatch: expected {expected!r}, "
                                f"archive declares {descriptor.get(field)!r}"
                            )
                    continue

                path = _safe_tar_name(member.name)
                if wrapped:
                    if member.name == "cache":
                        if not member.isdir():
                            raise ArtifactValidationError("wrapped cache root must be a directory")
                        continue
                    if not path.parts or path.parts[0] != "cache":
                        raise ArtifactValidationError(f"unexpected wrapped cache member: {member.name}")
                    relative_parts = path.parts[1:]
                    if not relative_parts:
                        continue
                    relative = PurePosixPath(*relative_parts)
                else:
                    relative = path
                relative_name = relative.as_posix()
                if relative_name in seen:
                    raise ArtifactValidationError(f"duplicate cache archive member: {relative_name}")
                seen.add(relative_name)
                target = destination.joinpath(*relative.parts)
                try:
                    target.resolve(strict=False).relative_to(destination)
                except ValueError as exc:
                    raise ArtifactValidationError(f"cache member escapes destination: {member.name}") from exc
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=member.mode & 0o777)
                    continue
                if not member.isreg() or member.size < 0:
                    raise ArtifactValidationError(f"cache archive contains unsupported entry type: {member.name}")
                total_size += member.size
                if total_size > _MAX_CACHE_BYTES:
                    raise ArtifactValidationError("cache archive expands beyond the safety limit")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise ArtifactValidationError(f"cannot read cache archive member: {member.name}")
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output, _READ_CHUNK)
                os.chmod(target, member.mode & 0o777)
        if wrapped and not descriptor_seen:
            raise ArtifactValidationError("wrapped cache archive is missing its descriptor")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


@dataclass(frozen=True)
class RuntimePackResult:
    digest: str
    packer_version: str
    manifest_digest: str
    source_tar_digest: str


@dataclass(frozen=True)
class PackerIdentity:
    version: str
    fingerprint: str
    executable_digest: str
    compressor_library_digests: tuple[str, ...]

    def manifest_binding(self) -> dict[str, Any]:
        return {
            "packer_fingerprint": self.fingerprint,
            "packer_executable_digest": self.executable_digest,
            "compressor_library_digests": list(self.compressor_library_digests),
        }


def _runtime_tar_name(name: str) -> str | None:
    """Return one canonical relative rootfs name or ``None`` for the tar root."""
    if not name or name.startswith("/") or "\\" in name or "\0" in name:
        raise ArtifactValidationError(f"unsafe runtime rootfs tar member: {name!r}")
    while name.startswith("./"):
        name = name[2:]
    if name in {"", "."}:
        return None
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactValidationError(f"unsafe runtime rootfs tar member: {name!r}")
    return path.as_posix()


def _copy_runtime_tar_with_manifest(source: Path, destination: Path, manifest: dict[str, Any]) -> str:
    """Bind a metadata-preserving BuildKit rootfs tar to its VM stack contract."""
    if not source.is_file():
        raise ArtifactValidationError(f"runtime rootfs tar does not exist: {source}")
    manifest_bytes = _canonical_json(manifest) + b"\n"
    manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    seen: dict[str, bytes] = {}
    parent_kind: bytes | None = None

    try:
        with (
            tarfile.open(source, "r:*") as input_tar,
            tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as output_tar,
        ):
            member_count = 0
            for member in input_tar:
                member_count += 1
                if member_count > _MAX_CACHE_MEMBERS:
                    raise ArtifactValidationError("runtime rootfs tar has too many members")
                name = _runtime_tar_name(member.name)
                if name is None:
                    continue
                if name in seen:
                    raise ArtifactValidationError(f"duplicate runtime rootfs tar member: {name}")
                if name == RUNTIME_MANIFEST_PATH:
                    raise ArtifactValidationError(
                        f"image rootfs uses reserved Palimpsest path {RUNTIME_MANIFEST_PATH!r}"
                    )
                if name == ".palimpsest":
                    parent_kind = member.type
                    if not member.isdir():
                        raise ArtifactValidationError("image rootfs .palimpsest entry must be a directory")
                if member.isreg():
                    source_stream = input_tar.extractfile(member)
                    if source_stream is None:
                        raise ArtifactValidationError(f"cannot read runtime rootfs member: {name}")
                elif (
                    member.isdir()
                    or member.issym()
                    or member.islnk()
                    or member.ischr()
                    or member.isblk()
                    or member.isfifo()
                ):
                    source_stream = None
                else:
                    raise ArtifactValidationError(f"unsupported runtime rootfs entry type: {name}")

                copied = tarfile.TarInfo(name)
                copied.type = member.type
                copied.mode = member.mode
                copied.uid = member.uid
                copied.gid = member.gid
                copied.uname = ""
                copied.gname = ""
                copied.mtime = 0
                copied.size = member.size if member.isreg() else 0
                copied.linkname = member.linkname
                copied.devmajor = member.devmajor
                copied.devminor = member.devminor
                copied.pax_headers = {
                    key: value
                    for key, value in member.pax_headers.items()
                    if key not in {"path", "linkpath", "mtime", "atime", "ctime"}
                }
                output_tar.addfile(copied, source_stream)
                seen[name] = member.type

            if parent_kind is None:
                parent = tarfile.TarInfo(".palimpsest")
                parent.type = tarfile.DIRTYPE
                parent.mode = 0o755
                parent.uid = parent.gid = parent.mtime = 0
                output_tar.addfile(parent)

            descriptor = tarfile.TarInfo(RUNTIME_MANIFEST_PATH)
            descriptor.type = tarfile.REGTYPE
            descriptor.mode = 0o444
            descriptor.uid = descriptor.gid = descriptor.mtime = 0
            descriptor.size = len(manifest_bytes)
            output_tar.addfile(descriptor, io.BytesIO(manifest_bytes))
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return manifest_digest


def _mksquashfs_version(*, runner: Callable[..., Any]) -> str:
    result = _run_checked(runner, ["mksquashfs", "-version"], operation="mksquashfs version check")
    output = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"
    match = re.search(r"mksquashfs\s+version\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", output, re.I)
    if match is None:
        raise BuildError("cannot determine mksquashfs version; squashfs-tools 4.5 or newer is required")
    version = match.group(1)
    parts = tuple(int(part) for part in version.split("."))
    if parts[:2] < (4, 5):
        raise BuildError(f"mksquashfs {version} is too old; metadata-preserving tar input requires 4.5+")
    return version


def _packer_identity_from_components(
    version: str,
    executable_digest: str,
    compressor_library_digests: Iterable[str],
) -> PackerIdentity:
    libraries = tuple(sorted(require_digest(value) for value in compressor_library_digests))
    executable_digest = require_digest(executable_digest)
    payload = {
        "schema": "palimpsest-mksquashfs-identity-v1",
        "version": version,
        "executable_digest": executable_digest,
        "compressor": "zstd",
        "compressor_library_digests": list(libraries),
    }
    fingerprint = f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"
    return PackerIdentity(version, fingerprint, executable_digest, libraries)


def _injected_runner_packer_identity(version: str) -> PackerIdentity:
    """Stable identity used only by deterministic injected test runners."""
    executable_digest = f"sha256:{hashlib.sha256(f'injected:mksquashfs:{version}'.encode()).hexdigest()}"
    return _packer_identity_from_components(version, executable_digest, ())


def _dynamic_zstd_library_digests(executable: Path) -> tuple[str, ...]:
    if sys.platform == "darwin":
        tool = shutil.which("otool")
        command = [tool, "-L", str(executable)] if tool else []
    elif sys.platform.startswith("linux"):
        tool = shutil.which("ldd")
        command = [tool, str(executable)] if tool else []
    else:
        raise BuildError(f"cannot fingerprint mksquashfs dynamic libraries on {sys.platform!r}")
    if not command:
        raise BuildError("cannot fingerprint mksquashfs dynamic libraries: dependency inspection tool is missing")
    result = _run_checked(subprocess.run, command, operation="mksquashfs dependency fingerprint")
    candidates: set[Path] = set()
    for raw_line in str(getattr(result, "stdout", "")).splitlines():
        if "zstd" not in raw_line.lower():
            continue
        if "not found" in raw_line.lower():
            raise BuildError(f"mksquashfs zstd dependency is unresolved: {raw_line.strip()}")
        line = raw_line.strip()
        if "=>" in line:
            candidate = line.split("=>", 1)[1].strip().split()[0]
        else:
            candidate = line.split(" (", 1)[0].split()[0]
        if not candidate.startswith("/"):
            raise BuildError(f"cannot resolve mksquashfs zstd dependency path: {candidate!r}")
        path = Path(candidate).resolve()
        if not path.is_file():
            raise BuildError(f"mksquashfs zstd dependency is not a readable file: {path}")
        candidates.add(path)
    return tuple(sorted(digest_file(path) for path in candidates))


def _mksquashfs_identity(*, runner: Callable[..., Any]) -> PackerIdentity:
    version = _mksquashfs_version(runner=runner)
    if runner is not subprocess.run:
        return _injected_runner_packer_identity(version)
    executable_name = shutil.which("mksquashfs")
    if executable_name is None:
        raise BuildError("mksquashfs executable not found while computing its toolchain fingerprint")
    executable = Path(executable_name).resolve()
    return _packer_identity_from_components(
        version,
        digest_file(executable),
        _dynamic_zstd_library_digests(executable),
    )


def _bound_runtime_manifest(manifest: dict[str, Any], packer_version: str) -> dict[str, Any]:
    return {**manifest, "packer": "mksquashfs", "packer_version": packer_version}


def _runtime_manifest_digest(manifest: dict[str, Any], packer_version: str) -> str:
    payload = _canonical_json(_bound_runtime_manifest(manifest, packer_version)) + b"\n"
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def build_runtime_pack_command(rootfs_tar: Path, output: Path, *, block_size: int) -> list[str]:
    if not rootfs_tar.is_file():
        raise ArtifactValidationError(f"runtime rootfs tar does not exist: {rootfs_tar}")
    return [
        "mksquashfs",
        "-",
        str(output),
        "-tar",
        "-comp",
        "zstd",
        "-Xcompression-level",
        "3",
        "-b",
        str(block_size),
        "-noappend",
        "-no-exports",
        "-reproducible",
        "-all-time",
        "0",
        "-mkfs-time",
        "0",
        "-root-uid",
        "0",
        "-root-gid",
        "0",
        "-root-mode",
        "0755",
    ]


def _run_checked(runner: Callable[..., Any], command: list[str], *, operation: str) -> Any:
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise BuildError(f"{operation} executable not found: {command[0]}") from exc
    if getattr(result, "returncode", 0) != 0:
        stderr = str(getattr(result, "stderr", ""))[-4000:]
        raise BuildError(f"{operation} failed with exit code {result.returncode}: {stderr}")
    return result


def pack_runtime_block(
    rootfs_tar: Path,
    output: Path,
    *,
    block_size: int,
    manifest: dict[str, Any],
    packer_version: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> RuntimePackResult:
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if packer_version is None:
        packer_identity = _mksquashfs_identity(runner=runner)
        packer_version = packer_identity.version
        manifest = {**manifest, **packer_identity.manifest_binding()}
    bound_manifest = _bound_runtime_manifest(manifest, packer_version)
    normalized_tar = output.parent / "runtime-rootfs.bound.tar"
    manifest_digest = _copy_runtime_tar_with_manifest(rootfs_tar, normalized_tar, bound_manifest)
    command = build_runtime_pack_command(normalized_tar, output, block_size=block_size)
    try:
        with normalized_tar.open("rb") as source:
            result = runner(command, stdin=source, capture_output=True, text=False, check=False)
    except FileNotFoundError as exc:
        raise BuildError("mksquashfs executable not found: mksquashfs") from exc
    if getattr(result, "returncode", 0) != 0:
        raw_stderr = getattr(result, "stderr", b"")
        stderr = raw_stderr.decode("utf-8", errors="replace") if isinstance(raw_stderr, bytes) else str(raw_stderr)
        raise BuildError(f"mksquashfs failed with exit code {result.returncode}: {stderr[-4000:]}")
    if not output.is_file() or output.stat().st_size < 4:
        raise BuildError("mksquashfs did not produce a runtime block image")
    with output.open("rb") as handle:
        if handle.read(4) != b"hsqs":
            raise BuildError("runtime block image has invalid SquashFS magic")
    runtime_digest = digest_file(output)
    _verify_runtime_block(
        output,
        runtime_digest,
        expected_manifest=bound_manifest,
        runner=runner,
    )
    return RuntimePackResult(
        digest=runtime_digest,
        packer_version=packer_version,
        manifest_digest=manifest_digest,
        source_tar_digest=digest_file(rootfs_tar),
    )


def _cache_name(scope: str, platform: str, builder_fingerprint: str) -> str:
    """Partition mutable scope fallbacks by BuildKit compatibility contract."""
    partition = hashlib.sha256(
        _canonical_json(
            {
                "scope": scope,
                "platform": platform,
                "builder_fingerprint": builder_fingerprint,
            }
        )
    ).hexdigest()[:16]
    return f"cache-{scope[:32]}-{partition}"


def _metadata_digest(item: dict[str, Any]) -> str:
    value = item.get("blob_digest", item.get("digest"))
    if not isinstance(value, str):
        raise HubError("Hub cache result is missing blob_digest")
    return require_digest(value)


def _validate_cache_metadata(item: dict[str, Any]) -> None:
    if item.get("kind") != KIND_BUILDKIT_CACHE:
        raise HubError(f"Hub cache result has unexpected kind: {item.get('kind')!r}")
    if item.get("media_type") != MEDIA_TYPE_BUILDKIT_CACHE:
        raise HubError(f"Hub cache result has unexpected media_type: {item.get('media_type')!r}")


def _resolve_hub_cache(
    hub_client: Any,
    spec: BuildKitSpec,
    build_key: str,
    build_dir: Path,
    cache_blob_root: Path,
    builder_fingerprint: str,
) -> tuple[Path | None, str]:
    exact = hub_client.list_layers(kind=KIND_BUILDKIT_CACHE, chain_id=build_key, limit=2)
    distinct = {_metadata_digest(item) for item in exact}
    if len(distinct) > 1:
        raise HubError(f"Hub returned conflicting cache blobs for {build_key}")
    selected: dict[str, Any] | None = exact[0] if exact else None
    source = "hub-exact" if selected is not None else "none"
    expected_key: str | None = build_key if selected is not None else None
    if selected is None:
        scoped = hub_client.list_layers(
            name=_cache_name(spec.cache_scope, spec.platform, builder_fingerprint),
            kind=KIND_BUILDKIT_CACHE,
            limit=1,
        )
        if scoped:
            selected = scoped[0]
            source = "hub-scope"
    if selected is None:
        return None, source
    _validate_cache_metadata(selected)
    digest = _metadata_digest(selected)
    digest_hex = digest.split(":", 1)[1]
    archive = cache_blob_root / "sha256" / f"{digest_hex}.tar"
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if archive.is_file():
        try:
            require_file_digest(archive, digest)
        except Exception:
            # Preserve poisoned generated data with the per-build evidence, then
            # repair the CAS only from the Hub's digest-verified response.
            os.replace(archive, build_dir / "rejected-local-cache.tar")
        else:
            source = f"{source}-local-cas"
    if not archive.is_file():
        temporary = archive.parent / f".{digest_hex}.{uuid.uuid4().hex}.part"
        try:
            hub_client.pull_blob(digest, temporary)
            require_file_digest(temporary, digest)
            os.replace(temporary, archive)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    extracted = build_dir / "hub-cache"
    extract_cache_tar(
        archive,
        extracted,
        expected_build_key=expected_key,
        expected_scope=spec.cache_scope,
        expected_platform=spec.platform,
        expected_builder_fingerprint=builder_fingerprint,
    )
    return extracted, source


def _promote_scope_cache(cache_export: Path, scope_root: Path, build_id: str) -> Path:
    """Commit a complete cache generation, then atomically advance its pointer.

    The caller must hold the cache-scope lock.  A failed pointer write leaves an
    unreachable generation for later cleanup while the previous pointer remains
    authoritative.
    """
    if _CACHE_GENERATION_RE.fullmatch(build_id) is None:
        raise BuildError(f"invalid BuildKit cache generation: {build_id!r}")
    if not (cache_export / "index.json").is_file():
        raise BuildError("cannot promote a BuildKit cache without index.json")

    scope_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(scope_root, 0o700)
    generations = scope_root / "generations"
    generations.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(generations, 0o700)
    generation = generations / build_id
    if generation.exists():
        raise BuildError(f"BuildKit cache generation already exists: {generation}")

    os.replace(cache_export, generation)
    os.chmod(generation, 0o700)
    state.fsync_directory(generations)
    state.atomic_write_json(
        scope_root / "current.json",
        {"schema_version": _CACHE_POINTER_SCHEMA_VERSION, "generation": build_id},
    )

    # Cleanup happens only after the pointer commit.  Failure to remove stale
    # optimization data must not turn an already committed build into a failure.
    legacy_current = scope_root / "current"
    try:
        if legacy_current.is_symlink() or legacy_current.is_file():
            legacy_current.unlink()
        elif legacy_current.is_dir():
            shutil.rmtree(legacy_current)
    except OSError:
        pass
    for candidate in generations.iterdir():
        if candidate == generation or _CACHE_GENERATION_RE.fullmatch(candidate.name) is None:
            continue
        try:
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
            elif candidate.is_dir():
                shutil.rmtree(candidate)
        except OSError:
            pass
    return generation


def _read_scope_cache(scope_root: Path) -> Path | None:
    """Resolve the committed cache generation without following pointer paths."""
    pointer = scope_root / "current.json"
    if not pointer.exists():
        # One-way compatibility with the pre-generation local cache layout.
        legacy = scope_root / "current"
        return legacy if (legacy / "index.json").is_file() else None

    value = state.read_json(pointer)
    generation_name = value.get("generation")
    if value.get("schema_version") != _CACHE_POINTER_SCHEMA_VERSION or not isinstance(generation_name, str):
        raise BuildError(f"invalid BuildKit cache pointer: {pointer}")
    if _CACHE_GENERATION_RE.fullmatch(generation_name) is None:
        raise BuildError(f"invalid BuildKit cache generation in {pointer}: {generation_name!r}")
    generation = scope_root / "generations" / generation_name
    if not (generation / "index.json").is_file():
        raise BuildError(f"BuildKit cache pointer references an incomplete generation: {generation_name}")
    return generation


def _read_buildx_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BuildError("BuildKit did not write its metadata file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("BuildKit wrote malformed metadata JSON") from exc
    if not isinstance(value, dict):
        raise BuildError("BuildKit metadata must be a JSON object")
    return value


def _runtime_pack_index_path(roots: state.StatePaths, pack_key: str) -> Path:
    normalized = require_digest(pack_key)
    return roots.runtime_packs / f"{normalized.split(':', 1)[1]}.json"


def _runtime_pack_metadata(
    spec: BuildKitSpec,
    *,
    pack_key: str,
    packer_version: str,
    packer_identity: PackerIdentity,
    runtime_arch: str,
    source_rootfs_tar_digest: str,
    oci_archive_digest: str,
    oci_manifest_digest: str,
) -> dict[str, Any]:
    return {
        "kind": "squashfs",
        "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
        "parent_digest": None,
        "base_image_digest": spec.runtime_base_digest,
        "platform": spec.platform,
        "arch": runtime_arch,
        "runtime_pack_schema": RUNTIME_PACK_SCHEMA,
        "runtime_pack_manifest_digest": pack_key,
        "packer": "mksquashfs",
        "packer_version": packer_version,
        **packer_identity.manifest_binding(),
        "source_rootfs_tar_digest": source_rootfs_tar_digest,
        "source_oci_archive_digest": oci_archive_digest,
        "source_oci_manifest_digest": oci_manifest_digest,
        "filesystem": "squashfs",
        "compression": "zstd",
        "block_size": spec.runtime_block_size,
        "root_uid": 0,
        "root_gid": 0,
        "root_mode": "0755",
        "readonly": True,
    }


def _write_runtime_pack_index(roots: state.StatePaths, pack_key: str, runtime_digest: str) -> None:
    state.atomic_write_json(
        _runtime_pack_index_path(roots, pack_key),
        {
            "schema_version": 1,
            "runtime_pack_manifest_digest": require_digest(pack_key),
            "runtime_block_digest": require_digest(runtime_digest),
        },
    )


def _require_squashfs_blob(path: Path, digest: str) -> None:
    require_file_digest(path, digest)
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise ArtifactValidationError(f"cannot read runtime block image: {path}") from exc
    if magic != b"hsqs":
        raise ArtifactValidationError(f"runtime block image has invalid SquashFS magic: {path}")


def _verify_runtime_block(
    path: Path,
    digest: str,
    *,
    expected_manifest: dict[str, Any],
    runner: Callable[..., Any],
) -> None:
    """Verify real SquashFS structure and its exact embedded source/policy binding."""
    _require_squashfs_blob(path, digest)
    command = ["unsquashfs", "-cat", str(path), RUNTIME_MANIFEST_PATH]
    if runner is subprocess.run:
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise BuildError("unsquashfs is required to verify runtime block images") from exc
        if process.stdout is None:  # pragma: no cover - guaranteed by stdout=PIPE
            process.kill()
            process.wait()
            raise BuildError("cannot capture the runtime block manifest from unsquashfs")
        raw_manifest = process.stdout.read(_MAX_RUNTIME_MANIFEST_BYTES + 1)
        if len(raw_manifest) > _MAX_RUNTIME_MANIFEST_BYTES:
            process.kill()
            process.wait()
            raise ArtifactValidationError("runtime block manifest exceeds the safety limit")
        returncode = process.wait()
        stderr = ""
    else:
        # The injectable runner exists for deterministic unit/host-contract
        # tests. Production subprocesses always take the bounded pipe path.
        try:
            result = runner(command, capture_output=True, text=False, check=False)
        except FileNotFoundError as exc:
            raise BuildError("unsquashfs is required to verify runtime block images") from exc
        returncode = getattr(result, "returncode", 0)
        raw_stderr = getattr(result, "stderr", b"")
        stderr = raw_stderr.decode("utf-8", errors="replace") if isinstance(raw_stderr, bytes) else str(raw_stderr)
        raw_manifest = getattr(result, "stdout", b"")
    if returncode != 0:
        raise ArtifactValidationError(f"cannot read the runtime block manifest with unsquashfs: {stderr[-4000:]}")
    if isinstance(raw_manifest, str):
        raw_manifest = raw_manifest.encode("utf-8")
    if not isinstance(raw_manifest, bytes) or len(raw_manifest) > _MAX_RUNTIME_MANIFEST_BYTES:
        raise ArtifactValidationError("runtime block manifest is missing or exceeds the safety limit")
    expected_bytes = _canonical_json(expected_manifest) + b"\n"
    if raw_manifest != expected_bytes:
        raise ArtifactValidationError("runtime block embedded manifest does not match the requested source/policy")


def _load_local_runtime_pack(
    roots: state.StatePaths,
    store: ContentStore,
    *,
    pack_key: str,
    expected_base: str,
    expected_platform: str,
    expected_arch: str,
    expected_packer_version: str,
    expected_manifest: dict[str, Any],
    runner: Callable[..., Any],
) -> str | None:
    index_path = _runtime_pack_index_path(roots, pack_key)
    if not index_path.is_file():
        return None
    index = state.read_json(index_path)
    if index.get("schema_version") != 1 or index.get("runtime_pack_manifest_digest") != pack_key:
        raise BuildError(f"invalid runtime-pack conversion cache index: {index_path}")
    runtime_digest = require_digest(index.get("runtime_block_digest", ""))
    _verify_runtime_block(
        store.blob_path(runtime_digest),
        runtime_digest,
        expected_manifest=_bound_runtime_manifest(expected_manifest, expected_packer_version),
        runner=runner,
    )
    metadata = store.read_metadata(runtime_digest)
    expected = {
        "runtime_pack_manifest_digest": pack_key,
        "base_image_digest": expected_base,
        "platform": expected_platform,
        "arch": expected_arch,
        "packer_version": expected_packer_version,
        "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            raise BuildError(
                f"runtime-pack conversion cache {field} mismatch: expected {value!r}, got {metadata.get(field)!r}"
            )
    return runtime_digest


def _pull_hub_runtime_pack(
    hub_client: Any,
    store: ContentStore,
    build_dir: Path,
    *,
    pack_key: str,
    expected_base: str,
    expected_arch: str,
    expected_manifest: dict[str, Any],
    expected_packer_version: str,
    runner: Callable[..., Any],
) -> str | None:
    results = hub_client.list_layers(kind="squashfs", chain_id=pack_key, limit=2)
    distinct = {_metadata_digest(item) for item in results}
    if len(distinct) > 1:
        raise HubError(f"Hub returned conflicting runtime blocks for pack key {pack_key}")
    if not results:
        return None
    item = results[0]
    if item.get("kind") != "squashfs" or item.get("media_type") != MEDIA_TYPE_LAYER_SQUASHFS:
        raise HubError("Hub runtime-pack result has an incompatible kind or media type")
    if item.get("chain_id") != pack_key or item.get("base_image_digest") != expected_base:
        raise HubError("Hub runtime-pack result does not match the requested pack/base identity")
    config = item.get("config_json") if isinstance(item.get("config_json"), dict) else {}
    item_arch = item.get("arch") or config.get("arch")
    if item_arch != expected_arch:
        raise HubError(f"Hub runtime-pack architecture mismatch: expected {expected_arch}, got {item_arch!r}")
    digest = _metadata_digest(item)
    if store.exists(digest):
        _verify_runtime_block(
            store.blob_path(digest),
            digest,
            expected_manifest=_bound_runtime_manifest(expected_manifest, expected_packer_version),
            runner=runner,
        )
        return digest
    downloaded = build_dir / "runtime-from-hub.squashfs"
    hub_client.pull_blob(digest, downloaded)
    _verify_runtime_block(
        downloaded,
        digest,
        expected_manifest=_bound_runtime_manifest(expected_manifest, expected_packer_version),
        runner=runner,
    )
    store.ingest_file(downloaded, expected_digest=digest)
    return digest


def build_with_buildkit(
    spec: BuildKitSpec,
    roots: state.StatePaths,
    *,
    hub_client: Any | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Build, cache, optionally pack, and optionally upload one Dockerfile."""
    if spec.offline:
        validate_offline_dockerfile(spec)
    else:
        validate_online_dockerfile(spec)
        if hub_client is None:
            raise BuildError("online BuildKit builds require a Hub client; use --offline for an air-gapped build")

    started_at = state.utc_now_iso()
    started = time.monotonic()
    phase = time.monotonic()
    builder_environment: dict[str, str] = {}
    buildx_driver = preflight_buildx_oci_exporter(
        strict_offline=spec.offline,
        environment_out=builder_environment,
        runner=runner,
    )
    timings: dict[str, int] = {
        "buildx_driver_preflight": round((time.monotonic() - phase) * 1000),
    }
    build_id = f"bk-{uuid.uuid4().hex[:12]}"
    build_dir = roots.builds / build_id
    build_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    spec.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if spec.rootfs_output is not None:
        if spec.rootfs_output.exists():
            if not spec.rootfs_output.is_dir() or any(spec.rootfs_output.iterdir()):
                raise BuildError(f"rootfs output path must be an empty directory: {spec.rootfs_output}")
        spec.rootfs_output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if spec.runtime_rootfs_archive is not None:
        if spec.runtime_rootfs_archive.exists():
            raise BuildError(f"runtime rootfs archive output already exists: {spec.runtime_rootfs_archive}")
        spec.runtime_rootfs_archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    scope_root = roots.build_cache / spec.cache_scope
    scope_lock = roots.locks / f"build-cache-{spec.cache_scope}.lock"
    with state.file_lock(scope_lock):
        phase = time.monotonic()
        build_key = compute_build_key(spec, builder_fingerprint=builder_environment["fingerprint"])
        timings["context_scan"] = round((time.monotonic() - phase) * 1000)

        cache_from: Path | None = None
        cache_source = "none"
        if not spec.no_cache:
            phase = time.monotonic()
            if not spec.offline:
                cache_from, cache_source = _resolve_hub_cache(
                    hub_client,
                    spec,
                    build_key,
                    build_dir,
                    roots.build_cache / "blobs",
                    builder_environment["fingerprint"],
                )
                timings["hub_cache_resolve_pull_verify"] = round((time.monotonic() - phase) * 1000)
            if cache_from is None:
                cache_from = _read_scope_cache(scope_root)
                if cache_from is not None:
                    cache_source = "local"
        cache_export = build_dir / "cache-export"
        metadata_file = build_dir / "buildkit-metadata.json"
        command = build_buildx_command(
            spec,
            cache_from=cache_from,
            cache_to=cache_export,
            metadata_file=metadata_file,
            builder_name=builder_environment["builder_name"],
        )

        phase = time.monotonic()
        result = _run_checked(runner, command, operation="docker buildx build")
        timings["buildkit_solve_export"] = round((time.monotonic() - phase) * 1000)
        (build_dir / "buildkit.log").write_text(
            f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}", encoding="utf-8"
        )
        if not spec.output.is_file():
            raise BuildError(f"BuildKit OCI exporter did not create {spec.output}")
        if spec.runtime_rootfs_archive is not None and not spec.runtime_rootfs_archive.is_file():
            raise BuildError(f"BuildKit tar exporter did not create {spec.runtime_rootfs_archive}")
        if not (cache_export / "index.json").is_file():
            raise BuildError("BuildKit local cache exporter did not create index.json")

        metadata = _read_buildx_metadata(metadata_file)
        oci_archive_digest = digest_file(spec.output)
        oci_manifest_digest = metadata.get("containerimage.digest")
        if isinstance(oci_manifest_digest, str):
            oci_manifest_digest = require_digest(oci_manifest_digest)
        else:
            oci_manifest_digest = None

        phase = time.monotonic()
        cache_archive = build_dir / "buildkit-cache.tar"
        cache_descriptor = {
            "schema": CACHE_ARCHIVE_SCHEMA,
            "build_key": build_key,
            "cache_scope": spec.cache_scope,
            "platform": spec.platform,
            "builder_fingerprint": builder_environment["fingerprint"],
            "oci_manifest_digest": oci_manifest_digest,
        }
        _create_cache_archive(cache_export, cache_archive, cache_descriptor)
        cache_archive_digest = digest_file(cache_archive)
        timings["cache_archive"] = round((time.monotonic() - phase) * 1000)

        if not spec.offline and spec.push_cache:
            phase = time.monotonic()
            hub_client.push_blob(
                cache_archive,
                {
                    "name": _cache_name(
                        spec.cache_scope,
                        spec.platform,
                        builder_environment["fingerprint"],
                    ),
                    "kind": KIND_BUILDKIT_CACHE,
                    "chain_id": build_key,
                    "media_type": MEDIA_TYPE_BUILDKIT_CACHE,
                    "is_published": False,
                },
            )
            timings["hub_cache_upload"] = round((time.monotonic() - phase) * 1000)

        _promote_scope_cache(cache_export, scope_root, build_id)

    runtime_digest: str | None = None
    runtime_size: int | None = None
    runtime_pack_key: str | None = None
    runtime_packer_version: str | None = None
    runtime_packer_identity: PackerIdentity | None = None
    runtime_cache_source: str | None = None
    if spec.runtime_tag is not None and spec.runtime_rootfs_archive is not None:
        runtime_arch = image_arch_for_platform(spec.platform)
        if oci_manifest_digest is None:
            raise BuildError("BuildKit did not report an OCI manifest digest required for runtime pack binding")
        phase = time.monotonic()
        runtime_packer_identity = _mksquashfs_identity(runner=runner)
        runtime_packer_version = runtime_packer_identity.version
        source_rootfs_tar_digest = digest_file(spec.runtime_rootfs_archive)
        runtime_manifest = {
            "schema": RUNTIME_PACK_SCHEMA,
            "base_image_digest": spec.runtime_base_digest,
            "platform": spec.platform,
            "arch": runtime_arch,
            "source_rootfs_tar_digest": source_rootfs_tar_digest,
            "source_oci_manifest_digest": oci_manifest_digest,
            "filesystem": "squashfs",
            "compression": "zstd",
            "compression_level": 3,
            "block_size": spec.runtime_block_size,
            "root_uid": 0,
            "root_gid": 0,
            "root_mode": "0755",
            "readonly": True,
            **runtime_packer_identity.manifest_binding(),
        }
        runtime_pack_key = _runtime_manifest_digest(runtime_manifest, runtime_packer_version)
        store = ContentStore(roots.store)

        runtime_lock = roots.locks / f"runtime-pack-{runtime_pack_key.split(':', 1)[1]}.lock"
        with state.file_lock(runtime_lock):
            runtime_digest = _load_local_runtime_pack(
                roots,
                store,
                pack_key=runtime_pack_key,
                expected_base=spec.runtime_base_digest,
                expected_platform=spec.platform,
                expected_arch=runtime_arch,
                expected_packer_version=runtime_packer_version,
                expected_manifest=runtime_manifest,
                runner=runner,
            )
            if runtime_digest is not None:
                runtime_cache_source = "local"
            elif not spec.offline:
                runtime_digest = _pull_hub_runtime_pack(
                    hub_client,
                    store,
                    build_dir,
                    pack_key=runtime_pack_key,
                    expected_base=spec.runtime_base_digest,
                    expected_arch=runtime_arch,
                    expected_manifest=runtime_manifest,
                    expected_packer_version=runtime_packer_version,
                    runner=runner,
                )
                if runtime_digest is not None:
                    runtime_cache_source = "hub"

            if runtime_digest is None:
                runtime_output = build_dir / "runtime.squashfs"
                pack_result = pack_runtime_block(
                    spec.runtime_rootfs_archive,
                    runtime_output,
                    block_size=spec.runtime_block_size,
                    manifest=runtime_manifest,
                    packer_version=runtime_packer_version,
                    runner=runner,
                )
                if pack_result.manifest_digest != runtime_pack_key:
                    raise BuildError("runtime packer produced an unexpected bound manifest digest")
                if pack_result.source_tar_digest != source_rootfs_tar_digest:
                    raise BuildError("runtime rootfs tar changed while the block image was being packed")
                runtime_digest = pack_result.digest
                store.ingest_file(runtime_output, expected_digest=runtime_digest)
                runtime_cache_source = "built"

            runtime_size = store.size(runtime_digest)
            store.write_metadata(
                runtime_digest,
                _runtime_pack_metadata(
                    spec,
                    pack_key=runtime_pack_key,
                    packer_version=runtime_packer_version,
                    packer_identity=runtime_packer_identity,
                    runtime_arch=runtime_arch,
                    source_rootfs_tar_digest=source_rootfs_tar_digest,
                    oci_archive_digest=oci_archive_digest,
                    oci_manifest_digest=oci_manifest_digest,
                ),
            )
            _write_runtime_pack_index(roots, runtime_pack_key, runtime_digest)

        write_tag_record(
            roots,
            TagRecord(
                schema_version=1,
                tag=spec.runtime_tag,
                digest=runtime_digest,
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                size_bytes=runtime_size,
                parent_digest=None,
                base_image_digest=spec.runtime_base_digest,
                source="buildkit-runtime-pack",
                created_at=state.utc_now_iso(),
            ),
        )
        if spec.push:
            hub_client.push_blob(
                store.blob_path(runtime_digest),
                {
                    "name": spec.runtime_tag,
                    "kind": "squashfs",
                    "parent_digest": None,
                    "chain_id": runtime_pack_key,
                    "base_image_digest": spec.runtime_base_digest,
                    "arch": runtime_arch,
                    "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                    "is_published": False,
                },
            )
        timings["runtime_pack_store_upload"] = round((time.monotonic() - phase) * 1000)

    timings["total"] = round((time.monotonic() - started) * 1000)
    record = {
        "schema_version": 2,
        "engine": "buildkit",
        "buildx_builder": builder_environment["builder_name"],
        "buildx_driver": buildx_driver,
        "buildx_version": builder_environment["buildx_version"],
        "buildkit_versions": builder_environment["buildkit_versions"].split(","),
        "builder_fingerprint": builder_environment["fingerprint"],
        "build_id": build_id,
        "build_key": build_key,
        "status": "success",
        "mode": "offline" if spec.offline else "online",
        "cache_source": cache_source,
        "cache_archive_digest": cache_archive_digest,
        "cache_scope": spec.cache_scope,
        "output_tag": spec.tag,
        "output_tags": [spec.tag, *spec.additional_tags],
        "registry_image_pushed": spec.push_image,
        "docker_image_loaded": spec.load,
        "pull_requested": spec.pull,
        "external_cache_from": [_cache_spec_receipt(value) for value in spec.external_cache_from],
        "external_cache_to": [_cache_spec_receipt(value) for value in spec.external_cache_to],
        "registry_profile": spec.registry_profile,
        "registry_config_digest": spec.registry_config_digest,
        "output_path": str(spec.output),
        "output_oci_archive_digest": oci_archive_digest,
        "output_oci_manifest_digest": oci_manifest_digest,
        "runtime_tag": spec.runtime_tag,
        "runtime_block_digest": runtime_digest,
        "runtime_block_size_bytes": runtime_size,
        "runtime_pack_manifest_digest": runtime_pack_key,
        "runtime_packer_version": runtime_packer_version,
        "runtime_packer_fingerprint": (
            runtime_packer_identity.fingerprint if runtime_packer_identity is not None else None
        ),
        "runtime_packer_executable_digest": (
            runtime_packer_identity.executable_digest if runtime_packer_identity is not None else None
        ),
        "runtime_compressor_library_digests": (
            list(runtime_packer_identity.compressor_library_digests) if runtime_packer_identity is not None else None
        ),
        "runtime_cache_source": runtime_cache_source,
        "platform": spec.platform,
        "network": spec.network,
        "build_arg_names": sorted(item.split("=", 1)[0] for item in spec.build_args),
        "local_image_digests": {item.alias: item.manifest_digest for item in spec.local_images},
        "timings_ms": timings,
        "bytes": {
            "oci_archive": spec.output.stat().st_size,
            "cache_archive": cache_archive.stat().st_size,
            "runtime_block": runtime_size or 0,
        },
        "started_at": started_at,
        "finished_at": state.utc_now_iso(),
    }
    state.atomic_write_json(build_dir / "record.json", record)
    return record
