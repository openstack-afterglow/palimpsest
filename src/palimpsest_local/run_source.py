"""Local-first run inputs; retained metadata is not authority over image bytes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from collections.abc import Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import state
from .digest import digest_hex, require_digest
from .errors import PalimpsestError, StateError
from .oci_layout import MAX_JSON_BYTES, MEDIA_TYPE_LAYER_CONFIG, MEDIA_TYPE_MANIFEST, ContentStore
from .package_reference import package_reference_path, read_package_reference
from .platforms import (
    CapabilityErrorCategory,
    DispatchKey,
    HostPlatform,
    PreflightReportPurpose,
    RuntimeBackend,
    RuntimeKind,
    RuntimeOperation,
    capability_profile,
    detect_host,
    evaluate_capability_profile,
)
from .registry import (
    RegistryError,
    RegistryProfile,
    add_profile,
    docker_image_inspect_argv,
    load_registry_config,
    resolve_docker_config_dir,
    resolve_image_reference,
    run_docker_command,
)
from .registry_intake import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ARCHIVE_BYTES,
    SUPPORTED_PLATFORM,
    RegistryImageNotFoundError,
    RegistryIntakeError,
    RegistryPullReceipt,
    _has_explicit_registry,
    export_docker_image_to_oci_archive,
    pull_registry_oci_archive,
)
from .runtime_types import RuntimePreflightError
from .state import StatePaths, pinned_owner_directory, utc_now_iso, validate_tag

_RUN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_BUILD_ID_RE = re.compile(r"^bk-[0-9a-f]{12}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIR_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY


def validate_run_name(name: str) -> str:
    if not isinstance(name, str) or _RUN_NAME_RE.fullmatch(name) is None:
        raise PalimpsestError("run name must match ^[a-z0-9][a-z0-9-]{0,62}$")
    return name


def generate_run_name(image_ref: str, roots: StatePaths | None = None) -> str:
    base = image_ref.partition("@")[0].rsplit("/", 1)[-1].partition(":")[0].lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:20] or "run"
    for _ in range(100):
        candidate = f"{base}-{secrets.token_hex(4)}"
        if roots is None or not os.path.lexists(roots.runs / candidate):
            return candidate
    raise StateError("cannot allocate a unique run name")


def preflight_oci_capabilities(*, network: str | None = None, host: HostPlatform | None = None) -> None:
    profile = capability_profile(
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM), RuntimeOperation.RUN, network=network
    )
    report = evaluate_capability_profile(
        profile,
        subject_digest="sha256:" + hashlib.sha256(f"early-oci:{profile.profile_id}:{network}".encode()).hexdigest(),
        arch="x86_64",
        host=host or detect_host(),
        purpose=PreflightReportPurpose.DISCOVERY,
    )
    if not report.successful:
        failed = next(item for item in report.checks if not item.passed)
        raise RuntimePreflightError(
            failed.error_category or CapabilityErrorCategory.CHECK_FAILED,
            failed.remediation or "host cannot run OCI-root workloads (Linux amd64 KVM required)",
            report=report,
            capability_id=failed.capability_id,
        )


@dataclass(frozen=True, slots=True)
class RetainedSourceRecord:
    reference: str
    canonical_reference: str
    archive_path: str
    archive_digest: str
    archive_size_bytes: int
    manifest_digest: str | None
    root_digest: str
    source_type: str
    platform: str
    created_at: str
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tags, (tuple, list)) or len(self.tags) > 128:
            raise StateError("invalid retained source tags")
        for value in (self.reference, self.canonical_reference, self.archive_path, self.created_at, *self.tags):
            if not isinstance(value, str) or not value or len(value) > 4096 or any(char in value for char in "\0\r\n"):
                raise StateError("invalid retained source text")
        object.__setattr__(self, "tags", tuple(self.tags))
        for value in (self.archive_digest, self.root_digest):
            if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
                raise StateError("retained source digest is not canonical")
        if self.manifest_digest is not None and (
            not isinstance(self.manifest_digest, str) or _DIGEST_RE.fullmatch(self.manifest_digest) is None
        ):
            raise StateError("retained manifest digest is not canonical")
        if type(self.archive_size_bytes) is not int or not 0 < self.archive_size_bytes <= MAX_ARCHIVE_BYTES:
            raise StateError("invalid retained source archive size")
        if not Path(self.archive_path).is_absolute() or self.platform != SUPPORTED_PLATFORM:
            raise StateError("invalid retained source path or platform")
        if self.source_type not in {"docker-daemon", "oci-registry"}:
            raise StateError("invalid retained source type")


@dataclass(frozen=True, slots=True)
class ResolvedRunSource:
    source_format: str
    target: str | Path
    manifest_digest: str | None = None
    receipt: RegistryPullReceipt | None = None
    source_type: str = "local"

    @property
    def is_oci(self) -> bool:
        return self.source_format == "oci-root"


@contextmanager
def _source_directory(roots: StatePaths, leaf: str, *, create: bool = False):
    """Pin the two fixed owned directories without following intermediate links."""
    with ExitStack() as stack:
        parent = stack.enter_context(pinned_owner_directory(roots.state, missing_ok=not create))
        if parent is None:
            yield None
            return
        for name in ("sources", leaf):
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                if create:
                    raise StateError("retained source directory disappeared") from None
                yield None
                return
            except OSError:
                raise StateError("retained source directory cannot be securely opened") from None
            stack.callback(os.close, child)
            opened = os.fstat(child)
            visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                opened.st_uid != os.geteuid()
                or opened.st_mode & 0o022
                or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
            ):
                raise StateError("retained source directory is not owner-bound")
            parent = child
        yield parent


def _record_keys(record: RetainedSourceRecord) -> set[str]:
    keys = {
        hashlib.sha256(ref.encode()).hexdigest() for ref in (record.reference, record.canonical_reference, *record.tags)
    }
    keys.update(
        digest_hex(value) for value in (record.archive_digest, record.root_digest, record.manifest_digest) if value
    )
    return keys


def _read_record(directory: int, filename: str) -> RetainedSourceRecord:
    info = os.stat(filename, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 65536:
        raise StateError("retained source record must be an owner-only bounded regular file")
    data = state._read_pinned_json_object(directory, filename)
    try:
        record = RetainedSourceRecord(**data)
    except (TypeError, ValueError):
        raise StateError("invalid retained source record") from None
    if re.fullmatch(r"[0-9a-f]{64}\.json", filename) is None or filename[:-5] not in _record_keys(record):
        raise StateError("retained source record identity mismatch")
    return record


def save_retained_source_record(roots: StatePaths, record: RetainedSourceRecord) -> None:
    payload = state._json_bytes(asdict(record))
    if len(payload) > 65536:
        raise StateError("retained source record exceeds its size bound")
    with _source_directory(roots, "records", create=True) as directory:
        for key in sorted(_record_keys(record)):
            temporary = ".tmp-" + secrets.token_hex(12)
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, key + ".json", src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass


def _archive_digest(
    path: Path, expected: str | None = None, *, directory_fd: int | None = None
) -> tuple[str, os.stat_result]:
    """Hash the bounded regular file pinned by this exact name/descriptor."""
    target = path.name if directory_fd is not None else path

    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    try:
        before = os.stat(target, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or not 0 < before.st_size <= MAX_ARCHIVE_BYTES
        ):
            raise StateError("local OCI archive is not an owned bounded regular file")
        fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if identity(opened) != identity(before):
                raise StateError("local OCI archive changed before verification")
            digest = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise StateError("local OCI archive changed during verification")
                digest.update(chunk)
                remaining -= len(chunk)
            if (
                stream.read(1)
                or identity(os.fstat(stream.fileno())) != identity(opened)
                or identity(os.stat(target, dir_fd=directory_fd, follow_symlinks=False)) != identity(opened)
            ):
                raise StateError("local OCI archive changed during verification")
        value = "sha256:" + digest.hexdigest()
        if expected is not None and value != require_digest(expected):
            raise StateError("local OCI archive digest mismatch")
        return value, opened
    except OSError:
        raise StateError("local OCI archive is missing or cannot be securely read") from None


def _queries(roots: StatePaths, query: str, registry_alias: str | None = None) -> set[str]:
    if _DIGEST_RE.fullmatch(query) or _BUILD_ID_RE.fullmatch(query):
        return {query}
    config = load_registry_config(roots)
    if registry_alias is not None or _has_explicit_registry(query):
        resolved = resolve_image_reference(query, config, registry_alias=registry_alias)
        return {resolved.canonical}
    values = {query}
    if ":" not in query and "@" not in query:
        values.add(query + ":latest")
    for alias, profile in config.registries.items():
        try:
            values.add(resolve_image_reference(query, config, registry_alias=alias).canonical)
        except RegistryError:
            if profile.protocol != "palimpsest":
                raise
            # A native profile with no namespace cannot represent a short OCI name.
    return values


def find_retained_oci_source(
    roots: StatePaths, query: str, *, registry_alias: str | None = None
) -> RetainedSourceRecord | None:
    queries = _queries(roots, query, registry_alias)
    keys = {
        digest_hex(value) if _DIGEST_RE.fullmatch(value) else hashlib.sha256(value.encode()).hexdigest()
        for value in queries
    }
    matches: dict[tuple[str, str], RetainedSourceRecord] = {}
    with _source_directory(roots, "records") as directory:
        if directory is None:
            return None
        for key in sorted(keys):
            try:
                record = _read_record(directory, key + ".json")
            except FileNotFoundError:
                continue
            if (
                registry_alias is not None
                and record.canonical_reference not in queries
                and query not in {record.archive_digest, record.root_digest, record.manifest_digest}
            ):
                continue
            if Path(record.archive_path).parent != roots.state / "sources" / "blobs":
                raise StateError("retained source archive escaped managed storage")
            with _source_directory(roots, "blobs") as blobs:
                if blobs is None:
                    raise StateError("retained OCI archive storage is missing")
                _, info = _archive_digest(Path(record.archive_path), record.archive_digest, directory_fd=blobs)
            if info.st_size != record.archive_size_bytes:
                raise StateError("retained source archive size mismatch")
            matches[(record.root_digest, record.archive_digest)] = record
    if len(matches) > 1:
        raise StateError("ambiguous local OCI reference; select a digest or archive path")
    return next(iter(matches.values()), None)


def list_retained_oci_sources(roots: StatePaths) -> list[dict[str, Any]]:
    results: dict[tuple[str, str], dict[str, Any]] = {}
    with _source_directory(roots, "records") as directory:
        if directory is None:
            return []
        for filename in sorted(os.listdir(directory)):
            if re.fullmatch(r"[0-9a-f]{64}\.json", filename) is None:
                continue
            try:
                record = _read_record(directory, filename)
                path = Path(record.archive_path)
                if path.parent != roots.state.resolve() / "sources" / "blobs":
                    raise StateError("retained source archive escaped managed storage")
                try:
                    info = path.lstat()
                    availability = (
                        "available"
                        if stat.S_ISREG(info.st_mode) and info.st_size == record.archive_size_bytes
                        else "unavailable"
                    )
                except FileNotFoundError:
                    availability = "missing"
                except OSError:
                    availability = "unavailable"
            except (OSError, PalimpsestError):
                continue
            key = (record.root_digest, record.archive_digest)
            if key in results:
                continue
            results[key] = {
                "digest": record.root_digest,
                "digest_type": "oci-manifest",
                "kind": "oci-image",
                "source": "retained-source",
                "editable": False,
                "availability": availability,
                "name": record.canonical_reference,
                "output_tags": list(record.tags or (record.reference,)),
                "size_bytes": record.archive_size_bytes,
                "output_oci_archive_digest": record.archive_digest,
                "output_oci_manifest_digest": record.manifest_digest,
                "oci_root_digest": record.root_digest,
                "output_path": record.archive_path,
                "created_at": record.created_at,
                "platform": record.platform,
                "arch": "x86_64",
                "tags": [],
                "referenced_by": {"runs": [], "projects": []},
                "builds": [],
                "build_ids": [],
            }
    return list(results.values())


def find_buildkit_oci_source(
    roots: StatePaths, query: str, *, registry_alias: str | None = None
) -> tuple[Path, str | None, str] | None:
    queries = _queries(roots, query, registry_alias)
    matches = {}
    with pinned_owner_directory(roots.builds.resolve(), missing_ok=True) as builds:
        if builds is None:
            return None
        for name in sorted(os.listdir(builds)):
            if _BUILD_ID_RE.fullmatch(name) is None:
                continue
            try:
                directory = os.open(name, _DIR_FLAGS, dir_fd=builds)
                try:
                    info = os.fstat(directory)
                    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                        raise StateError("build directory is not owner-bound")
                    record = state._read_pinned_json_object(directory, "record.json")
                finally:
                    os.close(directory)
            except (OSError, StateError):
                if name in queries:
                    raise StateError("cannot securely read selected build record") from None
                continue
            if record.get("engine") != "buildkit" or record.get("status") != "success":
                continue
            tags = record.get("output_tags", [])
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                if name in queries:
                    raise StateError("selected build has invalid output tags")
                continue
            archive_digest = record.get("output_oci_archive_digest")
            manifest = record.get("output_oci_manifest_digest")
            if not queries.intersection({name, archive_digest, manifest, *tags}):
                continue
            require_digest(archive_digest)
            if manifest is not None:
                require_digest(manifest)
            output = record.get("output_path")
            if not isinstance(output, str) or not output:
                raise StateError("selected build has no OCI archive path")
            path = Path(output)
            if not path.is_absolute():
                path = roots.builds / name / path
            _archive_digest(path, archive_digest)
            matches[(manifest, archive_digest)] = (path, manifest, archive_digest)
    if len(matches) > 1:
        raise StateError("ambiguous local BuildKit tag; select a digest or archive path")
    return next(iter(matches.values()), None)


def find_package_reference_source(
    roots: StatePaths, query: str, *, registry_alias: str | None = None
) -> tuple[Path, str, str] | None:
    matches = {}
    for reference in sorted(_queries(roots, query, registry_alias)):
        if not os.path.lexists(package_reference_path(roots, reference)):
            continue
        record = read_package_reference(roots, reference)
        path = Path(record.archive)
        _archive_digest(path, record.archive_digest)
        if record.package_type != "oci-image":
            raise StateError("native runtime bundles are not standalone OCI images")
        matches[(record.root_digest, record.archive_digest)] = (path, record.root_digest, record.archive_digest)
    if len(matches) > 1:
        raise StateError("ambiguous local package reference; select a digest or archive path")
    return next(iter(matches.values()), None)


def _retain_archive(
    roots: StatePaths, receipt: RegistryPullReceipt, reference: str, source_type: str
) -> ResolvedRunSource:
    archive_digest, before = _archive_digest(receipt.output)
    filename = digest_hex(archive_digest) + ".oci.tar"
    final = roots.state.resolve() / "sources" / "blobs" / filename
    with _source_directory(roots, "blobs", create=True) as directory:
        try:
            os.link(receipt.output.name, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            linked = os.stat(filename, dir_fd=directory, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino, linked.st_size, linked.st_mtime_ns) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                raise StateError("acquired OCI archive changed during retention")
            os.fsync(directory)
        except FileExistsError:
            _archive_digest(final, archive_digest)
        os.unlink(receipt.output.name, dir_fd=directory)
        os.fsync(directory)
    record = RetainedSourceRecord(
        reference,
        receipt.reference,
        str(final),
        archive_digest,
        before.st_size,
        receipt.manifest_digest,
        receipt.root_digest,
        source_type,
        receipt.platform,
        utc_now_iso(),
        tuple(dict.fromkeys((reference, receipt.reference))),
    )
    save_retained_source_record(roots, record)
    return ResolvedRunSource("oci-root", final, record.root_digest, receipt, source_type)


def _docker_candidates(roots: StatePaths, image_ref: str, registry_alias: str | None) -> list[str]:
    config = load_registry_config(roots)
    if registry_alias is not None or _has_explicit_registry(image_ref):
        resolved = resolve_image_reference(image_ref, config, registry_alias=registry_alias)
        profile = config.registries.get(resolved.registry_alias)
        return [] if profile is not None and profile.protocol != "oci" else [resolved.canonical]
    candidates = {image_ref}
    for alias, profile in config.registries.items():
        if profile.protocol == "oci":
            candidates.add(resolve_image_reference(image_ref, config, registry_alias=alias).canonical)
    return [image_ref, *sorted(candidates - {image_ref})]


def find_docker_daemon_source(
    roots: StatePaths,
    image_refs: Sequence[str],
    *,
    docker_config_dir: Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ResolvedRunSource | None:
    """Inspect every eligible name before exporting one unambiguous local image."""
    if not image_refs or shutil.which("docker") is None:
        return None
    config_dir = docker_config_dir or resolve_docker_config_dir()
    matches: dict[str, str] = {}
    for image_ref in dict.fromkeys(image_refs):
        try:
            result = run_docker_command(
                docker_image_inspect_argv(config_dir, [image_ref]), timeout_seconds=15, check=False
            )
        except PalimpsestError:
            raise RegistryIntakeError("cannot inspect the configured local Docker daemon") from None
        if result.returncode != 0:
            # Only a complete, single-image absence diagnostic is a miss. A daemon
            # error mentioning a missing image must not authorize remote fallback.
            detail = str(result.stderr or "").strip()
            if re.fullmatch(
                r"(?:Error(?: response from daemon)?: )?No such (?:image|object): [^\r\n]+", detail, re.IGNORECASE
            ):
                continue
            raise RegistryIntakeError("local Docker image inspection failed; not an image miss")
        try:
            inspected = json.loads(result.stdout)
            identity = inspected[0]["Id"]
            if (
                not isinstance(inspected, list)
                or len(inspected) != 1
                or not isinstance(identity, str)
                or _DIGEST_RE.fullmatch(identity) is None
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, IndexError):
            raise RegistryIntakeError("local Docker image inspection returned an invalid image identity") from None
        matches.setdefault(identity, image_ref)
    if len(matches) > 1:
        raise StateError("ambiguous local Docker images; select an explicit registry reference or archive path")
    if not matches:
        return None
    image_ref = next(iter(matches.values()))
    preflight_oci_capabilities()
    state.init_resolved_roots(roots)
    with _source_directory(roots, "blobs", create=True):
        destination = (
            roots.state.resolve() / "sources" / "blobs" / (".docker-export-" + secrets.token_hex(12) + ".oci.tar")
        )
        receipt = export_docker_image_to_oci_archive(
            image_ref, destination, roots, docker_config_dir=config_dir, timeout_seconds=timeout_seconds
        )
        return _retain_archive(roots, receipt, image_ref, "docker-daemon")


def acquire_remote_oci_source(
    roots: StatePaths,
    image_ref: str,
    *,
    registry_alias: str | None = None,
    docker_config_dir: Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ResolvedRunSource:
    config = load_registry_config(roots)
    if registry_alias is not None or _has_explicit_registry(image_ref):
        if _has_explicit_registry(image_ref) and registry_alias is None:
            authority = image_ref.partition("/")[0]
            if not any(profile.endpoint == authority for profile in config.registries.values()):
                alias = "run-explicit"
                suffix = 0
                while alias in config.registries:
                    suffix += 1
                    alias = f"run-explicit-{suffix}"
                config = add_profile(config, RegistryProfile(alias=alias, endpoint=authority))
        resolved = resolve_image_reference(image_ref, config, registry_alias=registry_alias)
        profiles = [config.registries[resolved.registry_alias]]
        if profiles[0].protocol != "oci":
            raise RegistryIntakeError(
                "native profiles cannot be acquired through Docker /v2; retain a local package first"
            )
    else:
        first = os.environ.get("PALIMPSEST_REGISTRY") or config.default
        if first not in config.registries:
            raise RegistryIntakeError("configured default registry profile does not exist")
        profiles = [
            config.registries[alias]
            for alias in [first, *sorted(set(config.registries) - {first})]
            if config.registries[alias].protocol == "oci"
        ]
    preflight_oci_capabilities()
    state.init_resolved_roots(roots)
    with _source_directory(roots, "blobs", create=True):
        for profile in profiles:
            destination = (
                roots.state.resolve() / "sources" / "blobs" / (".oci-pull-" + secrets.token_hex(12) + ".oci.tar")
            )
            try:
                receipt = pull_registry_oci_archive(
                    image_ref,
                    destination,
                    roots,
                    profile=profile,
                    docker_config_dir=docker_config_dir,
                    timeout_seconds=timeout_seconds,
                )
            except RegistryImageNotFoundError:
                continue
            return _retain_archive(roots, receipt, image_ref, "oci-registry")
    raise RegistryImageNotFoundError("image was not found in the selected OCI registry profiles")


def _layout_source_format(path: Path) -> str:
    """Infer routing from artifact semantics, not the shared OCI layout marker.

    This reads only bounded metadata. The selected runtime still performs full
    layout/blob verification before using the image bytes.
    """
    if not (path / "oci-layout").is_file():
        return "cloud-image"

    def document(relative: str) -> dict[str, Any]:
        metadata_path = path / relative
        try:
            if metadata_path.is_symlink() or not metadata_path.is_file():
                raise ValueError
            with metadata_path.open("rb") as stream:
                payload = stream.read(MAX_JSON_BYTES + 1)
            if len(payload) > MAX_JSON_BYTES:
                raise ValueError
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, ValueError):
            raise StateError("cannot infer run source from invalid layout metadata") from None

    manifests = document("index.json").get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise StateError("cannot infer run source from an empty or invalid layout index")
    formats = set()
    for descriptor in manifests:
        if not isinstance(descriptor, dict):
            raise StateError("cannot infer run source from an invalid layout descriptor")
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise StateError("cannot infer run source from an invalid layout descriptor digest")
        manifest = document("blobs/sha256/" + digest_hex(digest))
        config = manifest.get("config")
        native = (
            descriptor.get("mediaType") == MEDIA_TYPE_MANIFEST
            and isinstance(config, dict)
            and config.get("mediaType") == MEDIA_TYPE_LAYER_CONFIG
        )
        formats.add("cloud-image" if native else "oci-root")
    if len(formats) != 1:
        raise StateError("mixed cloud and OCI layout; select an explicit runtime kind")
    return formats.pop()


def resolve_run_source(
    roots: StatePaths,
    image_ref: str,
    *,
    runtime_kind: str | None = None,
    registry_alias: str | None = None,
    backend: str = "auto",
    network: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ResolvedRunSource:
    path = Path(image_ref).expanduser()
    if path.is_file():
        kind = runtime_kind or ("oci-root" if image_ref.endswith((".oci.tar", ".oci")) else "cloud-image")
        return ResolvedRunSource(kind, path.resolve(), source_type="local-file")
    if path.is_dir():
        kind = runtime_kind or _layout_source_format(path)
        return ResolvedRunSource(kind, path.resolve(), source_type="local-file")
    if image_ref.startswith("cloud:"):
        return ResolvedRunSource("cloud-image", image_ref, source_type="cloud-store")
    if runtime_kind != "oci-root":
        store = ContentStore(roots.store)
        if _DIGEST_RE.fullmatch(image_ref) and store.exists(image_ref):
            metadata = store.read_metadata(image_ref)
            if metadata.get("kind") == "cloud-image":
                return ResolvedRunSource("cloud-image", image_ref, source_type="cloud-store")
            if metadata.get("kind") == "squashfs":
                raise StateError("a SquashFS layer is not a standalone boot image")
        # Explicit paths and digests remain local; named tags do not override an
        # explicit registry selector (including an invalid/unknown alias).
        if registry_alias is not None and not _DIGEST_RE.fullmatch(image_ref):
            resolve_image_reference(image_ref, load_registry_config(roots), registry_alias=registry_alias)
        try:
            tag = validate_tag(image_ref)
        except StateError:
            tag = None
        if tag is not None and registry_alias is None:
            with pinned_owner_directory(roots.tags.resolve(), missing_ok=True) as directory:
                if directory is not None:
                    try:
                        os.stat(tag + ".json", dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        record = state.read_tag_record_at(directory, tag)
                        if not store.exists(record.digest):
                            raise StateError("local cloud tag points to a missing artifact")
                        metadata = store.read_metadata(record.digest)
                        if metadata.get("kind") != "cloud-image":
                            raise StateError("local tag is not a standalone cloud image")
                        return ResolvedRunSource("cloud-image", record.digest, source_type="cloud-tag")
        if runtime_kind == "cloud-image" and (registry_alias is None or _DIGEST_RE.fullmatch(image_ref)):
            return ResolvedRunSource("cloud-image", image_ref, source_type="cloud-store")
    # Prefer local build/package bindings to acquired mutable-reference cache entries.
    built = find_buildkit_oci_source(roots, image_ref, registry_alias=registry_alias)
    if built is not None:
        archive, manifest, _ = built
        return ResolvedRunSource("oci-root", archive, manifest, source_type="build-record")
    package = find_package_reference_source(roots, image_ref, registry_alias=registry_alias)
    if package is not None:
        archive, root, _ = package
        return ResolvedRunSource("oci-root", archive, root, source_type="package-reference")
    retained = find_retained_oci_source(roots, image_ref, registry_alias=registry_alias)
    if retained is not None:
        return ResolvedRunSource(
            "oci-root", Path(retained.archive_path), retained.root_digest, source_type="retained-source"
        )
    if runtime_kind is None and _DIGEST_RE.fullmatch(image_ref):
        return ResolvedRunSource("cloud-image", image_ref, source_type="cloud-store")
    if backend not in {"auto", "kvm"}:
        raise StateError("OCI-root run supports only the KVM backend")
    preflight_oci_capabilities(network=network)
    candidates = _docker_candidates(roots, image_ref, registry_alias)
    local = find_docker_daemon_source(roots, candidates, timeout_seconds=timeout_seconds)
    if local is not None:
        return local
    return acquire_remote_oci_source(roots, image_ref, registry_alias=registry_alias, timeout_seconds=timeout_seconds)
