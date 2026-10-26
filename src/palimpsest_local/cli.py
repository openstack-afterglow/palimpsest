"""Argument parsing, help, and command routing for ``palimpsest``."""

from __future__ import annotations

import argparse
import codecs
import getpass
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal as signal_module
import subprocess
import sys
import tempfile
import termios
import threading
import tomllib
import tty
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from . import __version__, completion, host_journal, inventory, runtime_dispatch, ui
from .build import build_layer, parse_palimpsestfile, verify_build_integrity
from .buildkit import BuildKitSpec, NamedOCIContext, build_with_buildkit, image_arch_for_platform
from .digest import digest_file, digest_hex, require_digest
from .errors import ArtifactValidationError, PalimpsestError
from .hub import DEFAULT_HUB_URL, DISK_FORMAT_MEDIA_TYPES, KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS, HubClient
from .inventory import import_cloud_image
from .oci_image import OCIImageRef
from .oci_layout import ContentStore, extract_bundle_tar, verify_layout_dir
from .oci_materializer import materialize_image_hard
from .oci_network import OCI_NETWORK_MODES
from .oci_packer import discover_squashfs_toolchain
from .oci_process import OCIUserSpec
from .oci_run_cleanup import OCIRunRemovalResult
from .oci_run_request import resolve_local_oci_run_request
from .oci_source import LocalArchiveSource, LocalLayoutSource, SourceCAS
from .oci_store import OCIStore
from .package_credentials import (
    erase_package_key,
    require_credential_helper,
    store_package_key,
    validate_package_key,
    validate_package_username,
)
from .package_reference import LocalPackageReference, read_package_reference, write_package_reference
from .packages import NativePackageClient, PackageSnapshot, snapshot_package
from .project import (
    DEFAULT_PROJECT_FILE,
    Project,
    ServiceSpec,
    _memory_mib,
    canonical_project_json,
    deterministic_project_name,
    load_interpolation_environment,
    load_project,
    normalize_host_ip,
)
from .project_adapter import build_project_callbacks
from .project_runtime import (
    down_project,
    managed_run_name,
    project_log_targets,
    project_logs,
    project_ps,
    project_service_operation,
    stop_project_services,
    up_project,
)
from .project_volumes import _validate_name
from .refs import BuildSpec, ImageRef, LayerRef, RunSpec, StackRef
from .registry import (
    RegistryConfig,
    RegistryProfile,
    ResolvedImageReference,
    _docker_subcommand_index,
    add_profile,
    docker_command_argv,
    docker_history_argv,
    docker_image_inspect_argv,
    docker_image_rm_argv,
    docker_images_argv,
    docker_load_argv,
    docker_login_argv,
    docker_logout_argv,
    docker_pull_argv,
    docker_push_argv,
    docker_save_argv,
    docker_tag_argv,
    inspect_profile,
    list_profiles,
    load_registry_config,
    normalize_endpoint,
    registry_config_digest,
    remove_profile,
    render_buildkitd_toml,
    resolve_docker_config_dir,
    resolve_image_reference,
    run_docker_passthrough,
    select_registry_profile,
    update_registry_config,
    use_profile,
)
from .registry_intake import DEFAULT_TIMEOUT_SECONDS as REGISTRY_PULL_TIMEOUT
from .registry_intake import pull_anonymous_oci_archive
from .run_source import generate_run_name, resolve_run_source, validate_run_name
from .runtime_types import (
    CloudImageInspectDetail,
    ExpectedRunIdentity,
    InspectRecord,
    LogDataEvent,
    LogStreamError,
    LogTerminalCategory,
    LogTerminalEvent,
    OCIRootInspectDetail,
    ProcessOutputEvent,
    ProcessSession,
    ProcessSignal,
    ProcessStatusEvent,
    ProcessStream,
    RunAttachmentMode,
    RunResult,
    RunVolumeIntent,
    RunWarningCategory,
)
from .state import (
    StatePaths,
    TagRecord,
    fsync_directory,
    init_resolved_roots,
    init_roots,
    read_tag_record,
    resolve_roots,
    write_tag_record,
)

_DOCKER_IMAGE_ID_RE = re.compile(r"(?:sha256:[0-9a-f]{64}|[0-9a-f]{12,64})")


def _display_width(value: str) -> int:
    """Count terminal cells, including wide letters and zero-width marks."""
    return sum(
        0
        if unicodedata.category(char) in {"Mn", "Me", "Cf"}
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in value
    )


def _print_table(headings: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Align every displayed cell without truncation, separated by two spaces."""
    widths = [_display_width(heading) for heading in headings]
    for row in rows:
        for column, cell in enumerate(row):
            widths[column] = max(widths[column], _display_width(cell))
    for row in (headings, *rows):
        print(
            "  ".join(
                cell + " " * (widths[column] - _display_width(cell)) if column < len(widths) - 1 else cell
                for column, cell in enumerate(row)
            )
        )


def _inspect_json_payload(inspected: InspectRecord) -> dict[str, object]:
    """Manually serialize the stable public inspect schema without reflective fields."""

    detail = inspected.detail
    ports = [
        {
            "host_ip": port.host_ip,
            "host_port": port.host_port,
            "guest_port": port.guest_port,
            "protocol": port.protocol,
        }
        for port in detail.ports
    ]
    if isinstance(detail, CloudImageInspectDetail):
        detail_payload: dict[str, object] = {
            "type": "cloud-image",
            "base": {
                "digest": detail.base.digest,
                "arch": detail.base.arch,
                "disk_format": detail.base.disk_format,
            },
            "layers": [{"digest": layer.digest, "target_dev": layer.target_dev} for layer in detail.layers],
            "memory_mib": detail.memory_mib,
            "vcpus": detail.vcpus,
            "network": detail.network,
            "ports": ports,
            "volumes": [
                {
                    "name": volume.name,
                    "mount_path": volume.mount_path,
                    "filesystem": volume.filesystem,
                    "read_only": volume.read_only,
                    "target_dev": volume.target_dev,
                }
                for volume in detail.volumes
            ],
            "ssh": {"host": detail.ssh.host, "port": detail.ssh.port},
            "guest_ip": detail.guest_ip,
        }
    elif isinstance(detail, OCIRootInspectDetail):
        detail_payload = {
            "type": "oci-root",
            "memory_mib": detail.memory_mib,
            "vcpus": detail.vcpus,
            "network": detail.network,
            "ports": ports,
            "guest_ip": detail.guest_ip,
        }
    else:  # pragma: no cover - InspectRecord enforces the closed detail union
        raise PalimpsestError("unsupported runtime inspect detail")
    return {
        "schema_version": inspected.schema_version,
        "state_schema_version": inspected.record.state_schema_version,
        "owner": {
            "schema_version": 1,
            "name": inspected.record.name,
            "run_id": inspected.record.run_id,
        },
        "identity": {
            "runtime_kind": inspected.record.dispatch_key.runtime_kind.value,
            "backend": inspected.record.dispatch_key.backend.value,
        },
        "lifecycle": {
            "status": inspected.lifecycle.status,
            "lifecycle_revision": inspected.lifecycle.lifecycle_revision,
            "created_at": inspected.lifecycle.created_at,
            "updated_at": inspected.lifecycle.updated_at,
        },
        "detail": detail_payload,
        "warnings": [warning.value for warning in inspected.warnings],
    }


def _render_run_warnings(result: RunResult) -> None:
    for warning in result.warnings:
        if warning is RunWarningCategory.EXPERIMENTAL_BACKEND:
            print("warning: libvirt-hvf is experimental", file=sys.stderr)


def _render_compose_log_event(
    service_name: str,
    event: LogDataEvent | LogTerminalEvent,
    pending: dict[str, _ComposeLogRenderState],
    output: TextIO,
) -> None:
    """Stream UTF-8 text with bounded decoder state and LF-only framing."""
    rendering = pending.setdefault(service_name, _ComposeLogRenderState())
    if isinstance(event, LogDataEvent):
        remaining = event.data
        while True:
            newline = remaining.find(b"\n")
            if newline < 0:
                if remaining:
                    rendering.start_line(service_name, output)
                    output.write(rendering.decoder.decode(remaining, final=False))
                break
            rendering.start_line(service_name, output)
            output.write(rendering.decoder.decode(remaining[:newline], final=True))
            output.write("\n")
            rendering.reset_line()
            remaining = remaining[newline + 1 :]
    else:
        if rendering.line_started:
            output.write(rendering.decoder.decode(b"", final=True))
            output.write("\n")
            rendering.reset_line()
        if event.outcome.category is LogTerminalCategory.ERROR:
            assert event.outcome.error_category is not None
            raise LogStreamError(event.outcome.error_category)


class _ComposeLogRenderState:
    """Keep only one incremental decoder and whether its logical line began."""

    __slots__ = ("decoder", "line_started")

    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.line_started = False

    def start_line(self, service_name: str, output: TextIO) -> None:
        if not self.line_started:
            output.write(f"{service_name} | ")
            self.line_started = True

    def reset_line(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.line_started = False


def _configured_url() -> str | None:
    config_path = init_roots().config / "config.toml"
    if not config_path.is_file():
        return None
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PalimpsestError(f"invalid Palimpsest configuration: {config_path}") from exc
    hub_config = config.get("hub", {})
    configured = config.get("url") if isinstance(config.get("url"), str) else None
    if configured is None and isinstance(hub_config, dict) and isinstance(hub_config.get("url"), str):
        configured = hub_config["url"]
    return configured.strip().rstrip("/") if configured and configured.strip() else None


def resolve_url(explicit_url: str | None) -> str:
    value = explicit_url or os.environ.get("PALIMPSEST_URL") or _configured_url() or DEFAULT_HUB_URL
    return value.rstrip("/")


def resolve_token() -> str:
    token = os.environ.get("PALIMPSEST_TOKEN")
    if not token:
        raise PalimpsestError("PALIMPSEST_TOKEN is required")
    return token


def _image_ref_from_store(store: ContentStore, digest: str) -> ImageRef:
    normalized = require_digest(digest)
    blob = store.blob_path(normalized)
    if not blob.is_file():
        raise PalimpsestError(f"image blob {normalized} not found in store")
    metadata = store.read_metadata(normalized)
    disk_format = metadata.get("disk_format")
    arch = metadata.get("arch")
    if metadata.get("kind") != KIND_CLOUD_IMAGE or disk_format not in {"qcow2", "raw"}:
        raise PalimpsestError(f"local blob {normalized} is not a verified cloud-image")
    if arch not in {"x86_64", "aarch64"}:
        raise PalimpsestError(f"cloud-image metadata has unsupported architecture for {normalized}: {arch!r}")
    return ImageRef(
        digest=normalized,
        disk_format=disk_format,
        arch=arch,
        os_variant=metadata.get("os_variant") if isinstance(metadata.get("os_variant"), str) else None,
        local_path=blob,
    )


def _pull_blob_into_store(client: HubClient, store: ContentStore, digest: str) -> Path:
    """Download outside the CAS namespace, then publish through its mutation authority."""
    normalized = require_digest(digest)
    if store.exists(normalized):
        try:
            store.verify_blob(normalized)
        except ArtifactValidationError:
            pass
        else:
            return store.blob_path(normalized)
    with tempfile.TemporaryDirectory(prefix="palimpsest-pull-") as temporary:
        staged = Path(temporary) / digest_hex(normalized)
        client.pull_blob(normalized, staged)
        store.ingest_file(staged, expected_digest=normalized)
    return store.blob_path(normalized)


def _resolve_image_ref(
    store: ContentStore,
    digest: str,
    explicit_url: str | None,
    *,
    requested_backend: str = "auto",
    preflight_for_run: bool = False,
    run_network: str | None = None,
) -> ImageRef:
    """Resolve a cloud image from verified local storage or the selected Hub."""
    normalized = require_digest(digest)
    if store.exists(normalized):
        try:
            store.verify_blob(normalized)
        except ArtifactValidationError:
            pass
        else:
            return _image_ref_from_store(store, normalized)
    client = HubClient(resolve_url(explicit_url), resolve_token())
    metadata = client.get_layer(normalized)
    if metadata.get("kind") != KIND_CLOUD_IMAGE:
        raise PalimpsestError(f"digest {normalized} is not a cloud-image (kind={metadata.get('kind')})")
    disk_format = metadata.get("disk_format")
    arch = metadata.get("arch")
    if disk_format not in DISK_FORMAT_MEDIA_TYPES or arch not in {"x86_64", "aarch64"}:
        raise PalimpsestError(f"cloud-image metadata is incomplete for {normalized}")
    if preflight_for_run:
        runtime_dispatch.preflight_run_capabilities(
            arch,
            requested_backend=requested_backend,
            network=run_network,
        )
    _pull_blob_into_store(client, store, normalized)
    store.write_metadata(
        normalized,
        {
            "kind": KIND_CLOUD_IMAGE,
            "disk_format": disk_format,
            "arch": arch,
            "os_variant": metadata.get("os_variant"),
            "ubuntu_base": metadata.get("ubuntu_base"),
            "name": metadata.get("name"),
        },
    )
    return _image_ref_from_store(store, normalized)


def _layer_refs_from_store(
    store: ContentStore,
    digests: Sequence[str],
    base_digest: str,
    parent_digest: str | None = None,
) -> tuple[LayerRef, ...]:
    refs: list[LayerRef] = []
    previous = parent_digest
    base_arch = (
        store.read_metadata(base_digest).get("arch") if digests and store.metadata_path(base_digest).is_file() else None
    )
    for digest in digests:
        normalized = require_digest(digest)
        blob = store.blob_path(normalized)
        if not blob.is_file():
            raise PalimpsestError(f"layer blob {normalized} not found in store")
        metadata = store.read_metadata(normalized)
        if metadata.get("kind") != "squashfs" or metadata.get("media_type") != MEDIA_TYPE_LAYER_SQUASHFS:
            raise PalimpsestError(f"local blob {normalized} is not a verified SquashFS layer")
        actual_parent = metadata.get("parent_digest")
        if previous is None:
            if actual_parent is not None or metadata.get("base_image_digest") != base_digest:
                raise PalimpsestError(f"root layer {normalized} does not belong to base image {base_digest}")
        elif actual_parent != previous:
            raise PalimpsestError(f"layer {normalized} does not continue parent {previous}")
        layer_arch = metadata.get("arch")
        if layer_arch is not None and base_arch is not None and layer_arch != base_arch:
            raise PalimpsestError(f"layer {normalized} architecture differs from base image {base_digest}")
        refs.append(LayerRef(digest=normalized, media_type=MEDIA_TYPE_LAYER_SQUASHFS, local_path=blob, arch=layer_arch))
        previous = normalized
    return tuple(refs)


def build_mksquashfs_command(source: Path, output: Path) -> list[str]:
    if not source.is_dir():
        raise PalimpsestError(f"layer source is not a directory: {source}")
    return [
        "mksquashfs",
        str(source),
        str(output),
        "-comp",
        "zstd",
        "-Xcompression-level",
        "3",
        "-noappend",
        "-no-exports",
    ]


def _add_limit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=50, help="maximum Hub results (1-200; default: 50)")


def _write_process_bytes(target: object, data: bytes) -> None:
    stream = getattr(target, "buffer", target)
    try:
        stream.write(data)
    except TypeError:
        # Test and embedded text streams may not expose a byte buffer. The
        # production CLI always takes the byte path above.
        target.write(data.decode("utf-8", errors="replace"))
    stream.flush()


def _resize_process_session(session: ProcessSession) -> None:
    if not session.capabilities.resize:
        return
    try:
        size = os.get_terminal_size(sys.stdin.fileno())
    except OSError:
        return
    session.resize(size.lines, size.columns)


def _run_process_session(session: ProcessSession, *, interactive: bool) -> int:
    """Bridge terminal bytes and signals without learning adapter process argv."""

    if not isinstance(session, ProcessSession):
        raise PalimpsestError("runtime returned an invalid process session")
    if interactive and (not session.capabilities.stdin or not session.capabilities.tty):
        session.close()
        raise PalimpsestError("runtime shell does not provide an interactive terminal")

    old_terminal: list[object] | None = None
    input_thread: threading.Thread | None = None
    input_stop = threading.Event()
    prior_handlers: dict[int, object] = {}
    terminal_status = None

    def forward(requested: ProcessSignal):
        def handler(_signum: int, _frame: object) -> None:
            session.signal(requested)

        return handler

    try:
        if threading.current_thread() is threading.main_thread():
            if session.capabilities.signal:
                for host_signal, requested in (
                    (signal_module.SIGINT, ProcessSignal.INTERRUPT),
                    (signal_module.SIGTERM, ProcessSignal.TERMINATE),
                ):
                    prior_handlers[host_signal] = signal_module.getsignal(host_signal)
                    signal_module.signal(host_signal, forward(requested))
            if session.capabilities.resize:
                prior_handlers[signal_module.SIGWINCH] = signal_module.getsignal(signal_module.SIGWINCH)
                signal_module.signal(
                    signal_module.SIGWINCH,
                    lambda _signum, _frame: _resize_process_session(session),
                )

        if interactive:
            input_fd = sys.stdin.fileno()
            old_terminal = termios.tcgetattr(input_fd)
            tty.setraw(input_fd)
            _resize_process_session(session)

            def pump_input() -> None:
                while not input_stop.is_set():
                    try:
                        readable, _writable, _exceptional = select.select([input_fd], [], [], 0.1)
                        if not readable:
                            continue
                        chunk = os.read(input_fd, 64 * 1024)
                    except OSError:
                        return
                    if not chunk:
                        session.close_stdin()
                        return
                    try:
                        session.write_stdin(chunk)
                    except PalimpsestError:
                        return

            input_thread = threading.Thread(target=pump_input, name="palimpsest-session-stdin", daemon=True)
            input_thread.start()

        for event in session.events():
            if isinstance(event, ProcessStatusEvent):
                if terminal_status is not None:
                    raise PalimpsestError("runtime returned duplicate terminal process status")
                terminal_status = event.result
                continue
            if not isinstance(event, ProcessOutputEvent):
                raise PalimpsestError("runtime returned an invalid process event")
            if terminal_status is not None:
                raise PalimpsestError("runtime returned output after terminal process status")
            if session.capabilities.tty and event.stream is not ProcessStream.PTY:
                raise PalimpsestError("runtime TTY session returned a split output stream")
            if not session.capabilities.tty and event.stream is ProcessStream.PTY:
                raise PalimpsestError("runtime non-TTY session returned a PTY output stream")
            target = sys.stderr if event.stream is ProcessStream.STDERR else sys.stdout
            _write_process_bytes(target, event.data)
        if terminal_status is None:
            raise PalimpsestError("runtime returned no terminal process status")
        waited = session.wait()
        if waited != terminal_status:
            raise PalimpsestError("runtime process status does not match wait result")
        return waited.returncode
    except BaseException:
        session.close()
        raise
    finally:
        active_exception = sys.exc_info()[0] is not None
        cleanup_error: PalimpsestError | None = None
        input_stop.set()
        if input_thread is not None:
            input_thread.join(timeout=0.5)
            if input_thread.is_alive():
                try:
                    session.close()
                except BaseException:
                    cleanup_error = PalimpsestError("cannot stop runtime session input")
                input_thread.join(timeout=0.5)
            if input_thread.is_alive() and cleanup_error is None:
                cleanup_error = PalimpsestError("cannot stop runtime session input")
        if old_terminal is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_terminal)
            except BaseException:
                if cleanup_error is None:
                    cleanup_error = PalimpsestError("cannot restore local terminal")
        for host_signal, prior in prior_handlers.items():
            try:
                signal_module.signal(host_signal, prior)
            except BaseException:
                if cleanup_error is None:
                    cleanup_error = PalimpsestError("cannot restore local signal handlers")
        if cleanup_error is not None and not active_exception:
            raise cleanup_error


def _parse_run_volume(value: str) -> RunVolumeIntent:
    parts = value.split(":")
    if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2] != "ro"):
        raise argparse.ArgumentTypeError("--volume must be NAME:/guest/path[:ro]")
    try:
        return RunVolumeIntent(parts[0], parts[1], read_only=len(parts) == 3)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid --volume: {exc}") from exc


def _parse_volume_size(value: str) -> int:
    raw_size = int(value) if re.fullmatch(r"[0-9]{1,10}", value) else value
    try:
        return _memory_mib(raw_size, "--size", minimum=16) * 1024 * 1024
    except PalimpsestError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="palimpsest",
        description="Build and store artifacts, run local VMs, and work with Hub or Docker registries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  palimpsest image ls --arch aarch64    # boot images available from Hub\n"
            "  palimpsest store ls                  # locally stored boot images and layers\n"
            "  palimpsest images                    # images in the local Docker daemon\n"
            "  digest=$(palimpsest image import ./ubuntu.qcow2 --disk-format qcow2 --arch aarch64)\n"
            '  palimpsest run "$digest" --name dev\n'
            "  palimpsest build . -t app --offline --output ./app.oci.tar\n"
            "  palimpsest run ./app.oci.tar --name app  # Linux amd64 KVM only\n"
            "  palimpsest docker --help             # help from the installed Docker CLI\n"
            "  palimpsest completion bash\n\n"
            "run accepts imported cloud-image digests or bundles, local OCI archives/layouts,\n"
            "and Docker/registry image references. OCI-root requires Linux amd64 KVM.\n"
            "Use COMMAND --help for options. See docs/cli/usage.md and docs/cli/workflows.md."
        ),
    )
    parser.add_argument("--url", help="Hub API URL (overrides PALIMPSEST_URL and config.toml)")
    parser.add_argument("--version", action="version", version=__version__, help="show the client version and exit")
    commands = parser.add_subparsers(dest="operation", required=True)

    image = commands.add_parser(
        "image",
        help="Hub boot images and local Docker image operations",
        description=(
            "ls/pull/push manage Hub cloud boot images; import/verify use local cloud-image files. "
            "inspect/history/rm/save/load operate on Docker images. "
            "Use store ls for locally stored boot images/layers, or images for local Docker inventory."
        ),
    )
    image_commands = image.add_subparsers(dest="image_operation", required=True)
    image_ls = image_commands.add_parser(
        "ls",
        help="list boot images available from Hub (not local inventory)",
        description="Query Hub boot-image inventory. Use store ls for local artifacts or images for local Docker images.",
    )
    image_ls.add_argument("--ubuntu-base", help="filter by Ubuntu base release")
    image_ls.add_argument("--arch", help="filter by guest architecture, such as x86_64 or aarch64")
    image_ls.add_argument("--os-variant", help="filter by operating-system variant")
    image_ls.add_argument("--disk-format", choices=("qcow2", "raw"), help="filter by boot disk format")
    _add_limit(image_ls)
    image_pull = image_commands.add_parser("pull", help="download a Hub boot image into the local store")
    image_pull.add_argument("digest", help="Hub boot-image SHA-256 digest")
    image_pull.add_argument("--output", type=Path, help="also copy the downloaded image to this path")
    image_verify = image_commands.add_parser("verify", help="verify a local file against a SHA-256 digest")
    image_verify.add_argument("path", type=Path, help="local boot-image file to verify")
    image_verify.add_argument("--digest", required=True, help="expected SHA-256 digest")
    image_import = image_commands.add_parser("import", help="import a local cloud boot image into the store")
    image_import.add_argument("path", type=Path, help="local qcow2 or raw cloud boot-image file")
    image_import.add_argument("--disk-format", choices=("qcow2", "raw"), required=True, help="input disk format")
    image_import.add_argument("--arch", choices=("x86_64", "aarch64"), required=True, help="guest architecture")
    image_import.add_argument("--os-variant", help="operating-system variant, such as ubuntu-24.04")
    image_push = image_commands.add_parser("push", help="upload a cloud boot-image file to Hub")
    image_push.add_argument("path", type=Path, help="local cloud boot-image file to upload")
    image_push.add_argument("--name", required=True, help="Hub display name for the image")
    image_push.add_argument(
        "--disk-format", choices=("qcow2", "raw"), default="qcow2", help="input disk format (default: qcow2)"
    )
    image_push.add_argument(
        "--arch", choices=("x86_64", "aarch64"), default="x86_64", help="guest architecture (default: x86_64)"
    )
    image_push.add_argument("--os-variant", help="operating-system variant, such as ubuntu-24.04")
    image_push.add_argument("--ubuntu-base", help="Ubuntu base release metadata")
    image_push.add_argument("--publish", action="store_true", help="mark the uploaded boot image as published in Hub")
    image_inspect = image_commands.add_parser("inspect", help="inspect local Docker image metadata")
    image_inspect.add_argument("references", nargs="+", help="Docker image references or local image IDs")
    image_inspect.add_argument("-f", "--format", help="Docker output template or format")
    image_inspect.add_argument("--platform", help="Docker image platform to inspect")
    image_inspect.add_argument("--registry", help="registry profile for resolving image references")
    image_history = image_commands.add_parser("history", help="show local Docker image layer history")
    image_history.add_argument("reference", help="local Docker image reference or ID")
    image_history.add_argument("--format", help="Docker output template or format")
    image_history.add_argument("--no-trunc", action="store_true", help="show full layer identifiers and commands")
    image_history.add_argument("--platform", help="Docker image platform")
    image_history.add_argument("-q", "--quiet", action="store_true", help="print layer IDs only")
    image_rm = image_commands.add_parser("rm", help="remove images from the local Docker daemon")
    image_rm.add_argument("references", nargs="+", help="local Docker image references or IDs to remove")
    image_rm.add_argument("-f", "--force", action="store_true", help="ask Docker to force removal")
    image_rm.add_argument("--no-prune", action="store_true", help="keep untagged parent images")
    image_rm.add_argument(
        "--platform", action="append", default=[], help="Docker image platform to remove (repeatable)"
    )
    image_save = image_commands.add_parser("save", help="save local Docker images as a Docker archive")
    image_save.add_argument("references", nargs="+", help="local Docker image references or IDs to export")
    image_save.add_argument("-o", "--output", type=Path, help="Docker archive destination (default: standard output)")
    image_save.add_argument(
        "--platform", action="append", default=[], help="Docker image platform to save (repeatable)"
    )
    image_load = image_commands.add_parser("load", help="load a Docker archive into the local Docker daemon")
    image_load.add_argument(
        "-i", "--input", dest="input_path", type=Path, help="Docker archive source (default: standard input)"
    )
    image_load.add_argument(
        "--platform", action="append", default=[], help="Docker image platform to load (repeatable)"
    )
    image_load.add_argument("-q", "--quiet", action="store_true", help="suppress Docker progress")

    layer = commands.add_parser("layer", help="list, pack, pull, and publish SquashFS layers")
    layer_commands = layer.add_subparsers(dest="layer_operation", required=True)
    layer_ls = layer_commands.add_parser("ls", help="list SquashFS layers available from Hub")
    layer_ls.add_argument("--name")
    layer_ls.add_argument("--kind")
    layer_ls.add_argument("--parent")
    _add_limit(layer_ls)
    layer_pull = layer_commands.add_parser("pull", help="download a Hub layer into the local store")
    layer_pull.add_argument("digest")
    layer_pull.add_argument("--output", type=Path)
    layer_pack = layer_commands.add_parser("pack", help="pack a local directory as a tagged SquashFS layer")
    layer_pack.add_argument("directory", type=Path)
    layer_pack.add_argument("--tag", required=True)
    layer_push = layer_commands.add_parser("push", help="upload a local layer tag or file to Hub")
    layer_push.add_argument("value")
    layer_push.add_argument("--name")
    layer_push.add_argument("--parent")
    layer_push.add_argument("--base-image")
    layer_push.add_argument("--ubuntu-base")
    layer_push.add_argument("--publish", action="store_true")

    bundle = commands.add_parser("bundle", help="download, unpack, and verify cloud-image/layer bundles")
    bundle_commands = bundle.add_subparsers(dest="bundle_operation", required=True)
    bundle_pull = bundle_commands.add_parser("pull", help="download a Hub layer chain as a local bundle")
    bundle_pull.add_argument("leaf_digest")
    bundle_pull.add_argument("--output", required=True, type=Path)
    bundle_pull.add_argument("--include-base", action="store_true")
    bundle_verify = bundle_commands.add_parser("verify", help="verify every descriptor and blob in a local bundle")
    bundle_verify.add_argument("directory", type=Path)
    bundle_unpack = bundle_commands.add_parser("unpack", help="atomically unpack and verify a local bundle archive")
    bundle_unpack.add_argument("archive", type=Path)
    bundle_unpack.add_argument("--output", required=True, type=Path)

    oci = commands.add_parser("oci", help="acquire/materialize local OCI images and inspect OCI-root runtimes")
    oci_commands = oci.add_subparsers(dest="oci_operation", required=True)
    oci_init_runtime = oci_commands.add_parser("init-runtime", help="create an explicit private OCI runtime parent")
    oci_init_runtime.add_argument("path", type=Path)
    oci_pull = oci_commands.add_parser("pull", help="download an anonymous Linux amd64 OCI archive without Docker")
    oci_pull.add_argument("reference")
    oci_pull.add_argument("--output", required=True, type=Path)
    oci_pull.add_argument("--platform", choices=("linux/amd64",), default="linux/amd64")
    oci_pull.add_argument("--timeout", type=float, default=REGISTRY_PULL_TIMEOUT)
    oci_materialize = oci_commands.add_parser(
        "materialize", help="convert a local Linux amd64 OCI image into verified SquashFS layers"
    )
    oci_materialize.add_argument("source", type=Path)
    oci_materialize.add_argument("--manifest", help="pin a root descriptor; required when the local index is ambiguous")
    oci_materialize.add_argument("--platform", choices=("linux/amd64",), default="linux/amd64")
    oci_materialize.add_argument("--packer", type=Path, default=Path("/usr/bin/mksquashfs"))
    oci_materialize.add_argument("--timeout", type=float, default=300.0)
    oci_materialize.add_argument("--output", type=Path)
    oci_root_proof = oci_commands.add_parser("root-proof", help="inspect an OCI-root run's recorded root proof")
    oci_root_proof.add_argument("name")
    oci_exec_status = oci_commands.add_parser("exec-status", help="inspect an OCI-root run's execution status")
    oci_exec_status.add_argument("name")
    oci_network = oci_commands.add_parser("network", help="inspect an OCI-root run's recorded network configuration")
    oci_network.add_argument("name")
    oci_exec_record = oci_commands.add_parser("exec-record", help="validate and print a local durable exec record")
    oci_exec_record.add_argument("path")
    oci_commands.add_parser("resource-status", help="inspect local OCI-root resource availability")
    oci_commands.add_parser("root-volumes", help="list retained local OCI writable-root volumes")
    oci_root_volume = oci_commands.add_parser("root-volume", help="inspect one local OCI writable-root volume")
    oci_root_volume.add_argument("volume_id")

    build = commands.add_parser(
        "build",
        help="build a Dockerfile OCI archive or a Palimpsestfile layer",
        description=(
            "Dockerfile builds export OCI archives using a managed Buildx builder. The default native cache mode "
            "requires Hub cache authority (--cache-registry and --cache-package); --cache-mode local uses only "
            "the host's managed cache with an OCI output profile. --offline also forbids remote inputs. "
            "Run the exported archive with run on Linux amd64 KVM, or use --runtime-tag/--runtime-base "
            "to pack a SquashFS layer for a conventional cloud-image VM. Palimpsestfile builds require --base."
        ),
    )
    build.add_argument(
        "context", nargs="?", type=Path, help="Dockerfile context directory (default: current directory)"
    )
    build.add_argument(
        "--frontend",
        choices=("auto", "palimpsestfile", "dockerfile"),
        default="auto",
        help="recipe frontend (default: infer from inputs)",
    )
    build.add_argument("--base", help="cloud-image SHA-256 digest for a Palimpsestfile build")
    build.add_argument("--tag", "-t", action="append", required=True, help="output tag; repeat for Dockerfile builds")
    build.add_argument(
        "-f", "--file", dest="recipe", type=Path, help="recipe path (default: Palimpsestfile or Dockerfile)"
    )
    build.add_argument("--layer", action="append", help="parent layer digest for a Palimpsestfile build")
    build.add_argument(
        "--network",
        choices=("none", "default"),
        default="none",
        help="build network (default: none; offline requires none)",
    )
    build.add_argument(
        "--offline", action="store_true", help="build without registry/Hub access using a prepared offline builder"
    )
    build.add_argument("--platform", help="Dockerfile target platform, such as linux/amd64 or linux/arm64")
    build.add_argument("--target", help="Dockerfile target stage")
    build.add_argument(
        "--build-arg", action="append", default=[], help="Dockerfile build argument KEY=VALUE (repeatable)"
    )
    build.add_argument(
        "--local-image", action="append", default=[], help="named local OCI context NAME=PATH (repeatable)"
    )
    build.add_argument("--cache-scope", help="local BuildKit cache partition")
    build.add_argument(
        "--cache-mode",
        choices=("native", "local"),
        default="native",
        help="managed cache authority: native Hub (default) or local-only cache",
    )
    build.add_argument("--registry", help="registry profile used to resolve output tags")
    build.add_argument("--cache-registry", help="native profile authorizing mandatory online BuildKit cache transfer")
    build.add_argument("--cache-package", help="exact namespace/package cache partition")
    build.add_argument(
        "--cache-from", action="append", default=[], help="additional online Buildx cache source (repeatable)"
    )
    build.add_argument(
        "--cache-to", action="append", default=[], help="additional online Buildx cache destination (repeatable)"
    )
    build.add_argument(
        "--no-cache", action="store_true", help="disable build-cache reuse (offline or local cache mode)"
    )
    build.add_argument("--pull", action="store_true", help="refresh Dockerfile base images (online only)")
    build.add_argument("--load", action="store_true", help="also load the OCI output into Docker (OCI profiles only)")
    build.add_argument(
        "--progress",
        choices=("auto", "none", "plain", "quiet", "rawjson", "tty"),
        default="plain",
        help="Buildx progress format (default: plain)",
    )
    build.add_argument("--output", type=Path, help="destination OCI archive path")
    build.add_argument("--rootfs-output", type=Path, help="also export a root filesystem archive")
    build.add_argument("--runtime-tag", help="also pack a cloud-image SquashFS layer with this local tag")
    build.add_argument(
        "--runtime-base", help="compatible cloud-image digest for --runtime-tag (must match target architecture)"
    )
    build.add_argument("--runtime-block-size", type=int, help="SquashFS block size for the runtime layer")
    build.add_argument("--push", action="store_true", help="push the OCI image to its configured registry")
    build.add_argument("--runtime-push", action="store_true", help="push the SquashFS runtime block to Palimpsest Hub")

    registry = commands.add_parser(
        "registry",
        help="manage local OCI/native Hub registry profiles",
        description="Manage local endpoint profiles, not remote image inventory. Profiles contain routing and TLS settings, never credentials.",
    )
    registry_commands = registry.add_subparsers(dest="registry_operation", required=True)
    registry_ls = registry_commands.add_parser("ls", help="list configured registry profiles")
    registry_ls.add_argument(
        "--format", choices=("table", "json"), default="table", help="output format (default: table)"
    )
    registry_add = registry_commands.add_parser("add", help="add or replace a local registry profile")
    registry_add.add_argument("name", help="local profile alias")
    registry_add.add_argument("endpoint", help="registry authority, such as registry.example.com:5000")
    registry_add.add_argument(
        "--protocol",
        choices=("oci", "palimpsest"),
        default="oci",
        help="OCI/Docker registry or native Hub package API (default: oci)",
    )
    registry_add.add_argument("--api-base", help="HTTPS native API base, including its gateway path")
    registry_add.add_argument("--namespace", help="default repository/package namespace")
    registry_add.add_argument("--mirror", action="append", default=[], help="OCI mirror endpoint (repeatable)")
    registry_add.add_argument(
        "--ca", action="append", type=Path, default=[], help="trusted CA certificate path (repeatable)"
    )
    registry_add.add_argument("--plain-http", action="store_true", help="allow unencrypted HTTP for this OCI endpoint")
    registry_add.add_argument(
        "--tls-skip-verify", action="store_true", help="disable TLS certificate verification for this OCI endpoint"
    )
    registry_add.add_argument(
        "--cache-from", action="append", default=[], help="default online Buildx cache source (repeatable)"
    )
    registry_add.add_argument(
        "--cache-to", action="append", default=[], help="default online Buildx cache destination (repeatable)"
    )
    registry_add.add_argument("--default", action="store_true", help="also select this profile as the default")
    registry_add.add_argument("--force", action="store_true", help="replace an existing profile with the same alias")
    registry_use = registry_commands.add_parser("use", help="select the default registry profile")
    registry_use.add_argument("name", help="profile alias to select")
    registry_rm = registry_commands.add_parser("rm", help="remove a local registry profile")
    registry_rm.add_argument("name", help="profile alias to remove")
    registry_inspect = registry_commands.add_parser("inspect", help="show one registry profile as JSON")
    registry_inspect.add_argument("name", nargs="?", help="profile alias (default: selected profile)")
    registry_buildkit = registry_commands.add_parser(
        "buildkit-config", help="generate BuildKit registry/TLS configuration"
    )
    registry_buildkit.add_argument("--output", required=True, type=Path, help="destination buildkitd.toml path")
    registry_buildkit.add_argument("--force", action="store_true", help="replace an existing output file")

    login = commands.add_parser("login", help="authenticate to an OCI registry or native Hub namespace")
    login.add_argument("server", nargs="?", help="registry authority (default: selected profile)")
    login.add_argument("-u", "--username", help="registry username")
    login.add_argument(
        "--password-stdin", action="store_true", help="read the password/package key from standard input"
    )
    login.add_argument("--registry", help="configured registry profile alias")
    login.add_argument("--namespace", help="native credential namespace (defaults to profile namespace)")
    logout = commands.add_parser("logout", help="remove stored registry or native Hub credentials")
    logout.add_argument("server", nargs="?", help="registry authority (default: selected profile)")
    logout.add_argument("--registry", help="configured registry profile alias")
    logout.add_argument("--namespace", help="native credential namespace (defaults to profile namespace)")

    pull = commands.add_parser("pull", help="pull an OCI image into Docker or a native package into a local archive")
    pull.add_argument("reference", help="registry image/package reference")
    pull.add_argument("-a", "--all-tags", action="store_true", help="pull all tags (OCI profiles only)")
    pull.add_argument("--platform", help="OCI target platform")
    pull.add_argument("-q", "--quiet", action="store_true", help="suppress Docker progress")
    pull.add_argument("--registry", help="registry profile alias")
    pull.add_argument("--output", type=Path, help="native OCI-layout archive destination; never loads Docker")

    push = commands.add_parser("push", help="push a Docker image or a local native OCI package")
    push.add_argument("reference", help="destination registry image/package reference")
    push.add_argument("-a", "--all-tags", action="store_true", help="push all tags (OCI profiles only)")
    push.add_argument("--platform", help="OCI target platform")
    push.add_argument("-q", "--quiet", action="store_true", help="suppress Docker progress")
    push.add_argument("--registry", help="registry profile alias")
    push.add_argument("--input", type=Path, help="native OCI layout directory or archive")
    push.add_argument("--manifest", help="native selected root SHA-256 (required for multiple roots)")

    tag = commands.add_parser("tag", help="tag a Docker image or a retained native package")
    tag.add_argument("source")
    tag.add_argument("target")
    tag.add_argument("--registry")

    images = commands.add_parser(
        "images",
        help="list images in the local Docker daemon (not Hub or the Palimpsest store)",
        description="List local Docker images. Use image ls for remote Hub boot images or store ls for local Palimpsest boot images/layers.",
    )
    images.add_argument("repository", nargs="?", help="optional Docker repository filter")
    images.add_argument("-a", "--all", action="store_true", help="include intermediate Docker images")
    images.add_argument("--digests", action="store_true", help="show image digests")
    images.add_argument(
        "-f", "--filter", action="append", default=[], help="Docker image filter KEY=VALUE (repeatable)"
    )
    images.add_argument("--format", help="Docker output template or format")
    images.add_argument("--no-trunc", action="store_true", help="show full image identifiers")
    images.add_argument("--tree", action="store_true", help="show the Docker multi-platform image tree")
    images.add_argument("-q", "--quiet", action="store_true", help="print image IDs only")

    history = commands.add_parser("history", help="show local Docker image layer history")
    history.add_argument("reference")
    history.add_argument("--format")
    history.add_argument("--no-trunc", action="store_true")
    history.add_argument("--platform")
    history.add_argument("-q", "--quiet", action="store_true")
    rmi = commands.add_parser("rmi", help="remove images from the local Docker daemon")
    rmi.add_argument("references", nargs="+")
    rmi.add_argument("-f", "--force", action="store_true")
    rmi.add_argument("--no-prune", action="store_true")
    rmi.add_argument("--platform", action="append", default=[])
    save = commands.add_parser("save", help="save local Docker images as a Docker archive")
    save.add_argument("references", nargs="+")
    save.add_argument("-o", "--output", type=Path)
    save.add_argument("--platform", action="append", default=[])
    load = commands.add_parser("load", help="load a Docker archive into the local Docker daemon")
    load.add_argument("-i", "--input", dest="input_path", type=Path)
    load.add_argument("--platform", action="append", default=[])
    load.add_argument("-q", "--quiet", action="store_true")

    docker = commands.add_parser(
        "docker",
        add_help=False,
        help="forward arbitrary arguments and help to the installed Docker CLI",
    )
    docker.add_argument(
        "docker_args", nargs=argparse.REMAINDER, help="unmodified Docker command/options; use docker --help"
    )

    run = commands.add_parser(
        "run",
        help="boot a cloud-image VM or run a local OCI image in a KVM VM",
        description=(
            "Boot a cloud-image digest or cloud-image/layer bundle, or run an OCI image in a KVM VM. "
            "Local cloud images and OCI sources (BuildKit builds, Docker daemon images, and configured OCI registries) "
            "are resolved automatically. OCI-root requires Linux amd64 KVM. "
            "For OCI-root, append -- COMMAND ARG... to override the image command."
        ),
    )
    run.add_argument(
        "image_or_bundle",
        help="cloud-image digest/bundle, local OCI archive/layout, or Docker/registry image reference",
    )
    run.add_argument("--name", default=None, help="unique local VM/run name (default: auto-generated)")
    run.add_argument("--registry", help="explicit registry alias to search or pull from")
    run.add_argument(
        "--layer", action="append", default=[], help="cloud-image SquashFS layer digest (repeatable; not for OCI-root)"
    )
    run.add_argument(
        "--volume",
        action="append",
        type=_parse_run_volume,
        dest="volume_intents",
        default=[],
        metavar="NAME:/guest/path[:ro]",
        help="attach an existing named data volume (repeatable; no host paths)",
    )
    run.add_argument("--memory", type=int, default=4096, help="guest memory in MiB (256-1048576; default: 4096)")
    run.add_argument("--vcpus", type=int, default=2, help="guest virtual CPUs (1-256; default: 2)")
    run.add_argument("--network", default=None, help="backend network; OCI-root: nat, host-only, or none")
    run.add_argument(
        "-p",
        "--publish",
        action="append",
        default=[],
        metavar="[HOST_IP:]HOST_PORT:GUEST_PORT[/tcp|/udp]",
        help="publish one OCI-root guest port on the host (default host address 127.0.0.1)",
    )
    run.add_argument(
        "--backend",
        choices=("auto", "kvm", "lima-vz", "libvirt-hvf", "lima-qemu-oci"),
        default="auto",
        help="VM backend (default: select for host and image architecture)",
    )
    run.add_argument(
        "--runtime-kind",
        choices=("cloud-image", "oci-root"),
        help="explicit input/runtime type; required for OCI layout directories",
    )
    run.add_argument("-d", "--detach", action="store_true", help="leave an OCI-root VM running after READY")
    run.add_argument("--manifest", help="pin the root descriptor of a local OCI image")
    run.add_argument("--user", help="run a local OCI image as USER[:GROUP]")
    run.add_argument(
        "--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64", help="selected OCI image platform"
    )
    run.add_argument(
        "--force-architecture",
        action="store_true",
        help="explicit experimental OCI container-in-Lima/QEMU VM attempt; not native OCI-root",
    )
    run.add_argument(
        "--vm-image",
        type=Path,
        help="matching local Linux carrier disk with preinstalled nerdctl/containerd (force mode only)",
    )
    run.add_argument("--vm-image-digest", help="SHA-256 of the local experimental carrier disk")
    run.add_argument(
        "--root-retention",
        choices=("delete", "retain"),
        default=None,
        help="OCI writable-root removal policy (default: delete)",
    )
    run.add_argument("--root-volume", help="reuse an explicitly retained OCI writable-root UUID")

    compose = commands.add_parser("compose", help="manage VM services declared in palimpsest.yml (not Docker Compose)")
    compose.add_argument("-f", "--file", dest="project_file", type=Path)
    compose.add_argument("-p", "--project-name")
    compose.add_argument("--project-directory", type=Path)
    compose.add_argument("--env-file", action="append", type=Path, default=[])
    compose_commands = compose.add_subparsers(dest="compose_operation", required=True)
    compose_config = compose_commands.add_parser("config", help="validate and render the VM project configuration")
    compose_config.add_argument("--quiet", action="store_true")
    compose_config.add_argument("--format", choices=("json",), default="json")
    compose_config.add_argument("--services", action="store_true")
    compose_up = compose_commands.add_parser("up", help="create or reconcile the project's VM services")
    compose_up.add_argument("services", nargs="*")
    compose_up.add_argument("-d", "--detach", action="store_true")
    compose_up.add_argument("--no-recreate", action="store_true")
    compose_up.add_argument("--force-recreate", action="store_true")
    compose_down = compose_commands.add_parser("down", help="remove the project's managed VM services")
    compose_down.add_argument("--volumes", "-v", action="store_true")
    compose_ps = compose_commands.add_parser("ps", help="list the project's managed VM services")
    compose_ps.add_argument("services", nargs="*")
    compose_ps.add_argument("--format", choices=("table", "json"), default="table")
    compose_logs = compose_commands.add_parser("logs", help="show the project's VM console logs")
    compose_logs.add_argument("services", nargs="*")
    compose_logs.add_argument("--follow", "-f", action="store_true")
    compose_exec = compose_commands.add_parser("exec", help="execute a command in a VM service")
    compose_exec.add_argument("service")
    compose_exec.add_argument("command", nargs=argparse.REMAINDER)
    compose_stop = compose_commands.add_parser("stop", help="stop the project's managed VM services")
    compose_stop.add_argument("services", nargs="*")
    compose_port = compose_commands.add_parser("port", help="show a VM service's published host endpoint")
    compose_port.add_argument("service")
    compose_port.add_argument("private_port", type=int)
    compose_port.add_argument("--protocol", choices=("tcp", "udp"), default="tcp")

    commands.add_parser(
        "ps",
        help="list live local VMs (managed and externally observed)",
        description="Observe local backend VMs, including stopped and externally created VMs. "
        "SCOPE distinguishes managed runs from read-only observations; inspect and controls remain managed-only.",
    )
    for command_name, description in (
        ("volumes", "list local host disks and selected-state volume records"),
        ("networks", "list local backend networks and VM attachments"),
    ):
        resource_list = commands.add_parser(command_name, help=description)
        resource_list.add_argument("--format", choices=("table", "json"), default="table", help="output format")
    volume = commands.add_parser("volume", help="manage named data volumes")
    volume_commands = volume.add_subparsers(dest="volume_operation", required=True)
    for command_name, description in (
        ("create", "create a named data volume"),
        ("inspect", "inspect a named data volume"),
        ("rm", "remove a named data volume"),
    ):
        volume_command = volume_commands.add_parser(command_name, help=description)
        volume_command.add_argument("name")
        volume_command.add_argument(
            "--backend",
            choices=("kvm", "lima-vz", "libvirt-hvf"),
            required=True,
            help="explicit volume backend",
        )
        if command_name == "create":
            volume_command.add_argument(
                "--size", type=_parse_volume_size, required=True, help="capacity in MiB or with a MiB/GiB suffix"
            )
    inspect = commands.add_parser("inspect", help="inspect one managed VM/run")
    inspect.add_argument("name")
    logs = commands.add_parser("logs", help="show a managed VM/run console log")
    logs.add_argument("name")
    logs.add_argument("--follow", action="store_true")
    shell = commands.add_parser("shell", help="open an interactive shell in a managed VM/run")
    shell.add_argument("name")
    execute = commands.add_parser("exec", help="execute a command in a managed VM/run")
    execute.add_argument("--completion-record")
    execute.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="OCI-root guest exec timeout in whole seconds (1-600; default 30)",
    )
    execute.add_argument("name")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    start = commands.add_parser("start", help="start a stopped managed VM/run")
    start.add_argument("name")
    stop = commands.add_parser("stop", help="stop a managed VM/run")
    stop.add_argument("name")
    remove = commands.add_parser("rm", help="remove a managed VM/run")
    remove.add_argument("name")
    remove.add_argument("--volumes", action="store_true")
    commit = commands.add_parser("commit", help="capture a supported cloud-image VM's changes as a layer")
    commit.add_argument("name")
    commit.add_argument("--tag", required=True)
    web = commands.add_parser(
        "web",
        help="serve the local web dashboard until interrupted (no Hub required)",
        description="Run the local dashboard in the foreground on 127.0.0.1. Stop with Ctrl-C. "
        "No Hub connection is needed; ordinary CLI commands do not start this server.",
    )
    web.add_argument("--port", type=int, default=0, help="listen port; 0 selects an available loopback port")
    web.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")
    web.add_argument(
        "--allow-control", action="store_true", help="enable local edits and VM control (default: read-only)"
    )

    store = commands.add_parser(
        "store",
        help="inspect or manage the local Palimpsest artifact store",
        description="Manage local boot images, layers, and their storage root. This is not Hub image inventory or Docker's image store.",
    )
    store_commands = store.add_subparsers(dest="store_operation", required=True)
    store_show = store_commands.add_parser("show", help="show local storage paths and usage")
    store_show.add_argument(
        "--format", choices=("table", "json"), default="table", help="output format (default: table)"
    )
    store_ls = store_commands.add_parser(
        "ls",
        help="list locally stored boot images and layers (not Hub or Docker)",
        description="List boot images and layers already in the local Palimpsest store. Use image ls for Hub or images for Docker.",
    )
    store_ls.add_argument(
        "--kind", choices=("image", "layer", "all"), default="all", help="artifact kind to list (default: all)"
    )
    store_ls.add_argument("--format", choices=("table", "json"), default="table", help="output format (default: table)")
    store_rm = store_commands.add_parser("rm", help="remove a local artifact and its tags")
    store_rm.add_argument("digest", help="local artifact SHA-256 digest")
    store_rm.add_argument(
        "--force", action="store_true", help="never bypasses protection for artifacts referenced by runs or projects"
    )
    store_move = store_commands.add_parser("move", help="migrate local state to another root")
    store_move.add_argument("--to", dest="destination", type=Path, required=True, help="destination state root")
    store_move.add_argument("--keep-source", action="store_true", help="retain the source after migration")
    store_set = store_commands.add_parser("set", help="select an existing local state root")
    store_set.add_argument("--to", dest="destination", type=Path, required=True, help="existing state root to select")
    comp_cmd = commands.add_parser(
        "completion",
        help="print or explicitly install shell completion",
        description="Print a shell completion script by default; --install explicitly adds Bash/Zsh startup registration or a Fish completion file.",
    )
    comp_cmd.add_argument("shell", choices=completion.SUPPORTED_SHELLS, help="shell completion target")
    comp_cmd.add_argument(
        "--install", action="store_true", help="install the script and print its path instead of script contents"
    )
    # Keep command help and shell descriptions on the same argparse schema.
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for choice in action._choices_actions:
                    child = action.choices[choice.dest]
                    if child.description is None:
                        child.description = choice.help
                pending.extend(action.choices.values())
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if hasattr(args, "limit") and not 1 <= args.limit <= 200:
        parser.error("--limit must be between 1 and 200")
    if args.operation == "run":
        if args.name is not None:
            validate_run_name(args.name)
        if not 256 <= args.memory <= 1_048_576:
            parser.error("--memory must be between 256 and 1048576")
        if not 1 <= args.vcpus <= 256:
            parser.error("--vcpus must be between 1 and 256")
        names = [intent.name for intent in args.volume_intents]
        targets = [intent.target for intent in args.volume_intents]
        if len(names) != len(set(names)):
            parser.error("--volume cannot repeat a volume name")
        if len(targets) != len(set(targets)):
            parser.error("--volume cannot repeat a guest target")
    if args.operation == "volume":
        _validate_name(args.name, "volume name")
    if args.operation == "web":
        if args.port != 0 and not 1024 <= args.port <= 65535:
            parser.error("--port must be 0 or between 1024 and 65535")
    if args.operation == "oci" and args.oci_operation == "materialize" and not 0 < args.timeout < float("inf"):
        parser.error("--timeout must be a positive finite number")
    if args.operation == "exec":
        if args.command[:1] == ["--"]:
            args.command = args.command[1:]
        if not args.command:
            parser.error("exec requires a command after --")
        if args.timeout is not None and not 1 <= args.timeout <= 600:
            parser.error("--timeout must be an integer between 1 and 600 seconds")
    if args.operation == "compose":
        if args.compose_operation == "exec":
            if args.command[:1] == ["--"]:
                args.command = args.command[1:]
            if not args.command:
                parser.error("compose exec requires a command after SERVICE")
        if args.compose_operation == "up" and args.no_recreate and args.force_recreate:
            parser.error("--no-recreate and --force-recreate are mutually exclusive")
    if args.operation == "build":
        frontend = _selected_build_frontend(args)
        if frontend == "palimpsestfile":
            if len(args.tag) != 1:
                parser.error("Palimpsestfile builds accept exactly one --tag")
            if not args.base:
                parser.error("Palimpsestfile builds require --base")
            if args.context is not None:
                parser.error("Palimpsestfile builds do not accept a context positional argument")
            buildkit_only = [
                option
                for option, supplied in (
                    ("--offline", args.offline),
                    ("--platform", args.platform is not None),
                    ("--target", args.target is not None),
                    ("--build-arg", bool(args.build_arg)),
                    ("--local-image", bool(args.local_image)),
                    ("--cache-scope", args.cache_scope is not None),
                    ("--cache-mode", args.cache_mode != "native"),
                    ("--registry", args.registry is not None),
                    ("--cache-registry", args.cache_registry is not None),
                    ("--cache-package", args.cache_package is not None),
                    ("--cache-from", bool(args.cache_from)),
                    ("--cache-to", bool(args.cache_to)),
                    ("--no-cache", args.no_cache),
                    ("--pull", args.pull),
                    ("--load", args.load),
                    ("--progress", args.progress != "plain"),
                    ("--output", args.output is not None),
                    ("--rootfs-output", args.rootfs_output is not None),
                    ("--runtime-tag", args.runtime_tag is not None),
                    ("--runtime-base", args.runtime_base is not None),
                    ("--runtime-block-size", args.runtime_block_size is not None),
                    ("--push", args.push),
                    ("--runtime-push", args.runtime_push),
                )
                if supplied
            ]
            if buildkit_only:
                parser.error(
                    "Palimpsestfile builds do not accept Dockerfile/BuildKit options: " + ", ".join(buildkit_only)
                )
        else:
            if args.base or args.layer:
                parser.error("Dockerfile builds use --runtime-base/--local-image, not legacy --base/--layer")
            if args.offline and args.network != "none":
                parser.error("--offline requires --network none")
            if args.offline and args.push:
                parser.error("--offline cannot be combined with --push")
            if args.offline and args.runtime_push:
                parser.error("--offline cannot be combined with --runtime-push")
            if args.offline and args.pull:
                parser.error("--offline cannot be combined with --pull")
            if args.offline and (args.cache_from or args.cache_to):
                parser.error("--offline cannot use external --cache-from/--cache-to backends")
            if args.offline and args.registry:
                parser.error("--offline cannot select an external registry")
            if args.offline and (args.cache_registry is not None or args.cache_package is not None):
                parser.error("--offline cannot select an online cache authority")
            if args.cache_mode == "local" and (
                args.cache_registry is not None or args.cache_package is not None or args.cache_from or args.cache_to
            ):
                parser.error("--cache-mode local cannot select native or external cache authorities")
            if args.no_cache and not args.offline and args.cache_mode == "native":
                parser.error("--no-cache requires --offline or --cache-mode local")
            if bool(args.runtime_tag) != bool(args.runtime_base):
                parser.error("--runtime-tag and --runtime-base must be supplied together")
            if args.runtime_push and not args.runtime_tag:
                parser.error("--runtime-push requires --runtime-tag and --runtime-base")


def _selected_build_frontend(args: argparse.Namespace) -> str:
    if args.frontend != "auto":
        return args.frontend
    if args.base is not None or args.layer:
        return "palimpsestfile"
    if args.context is not None:
        return "dockerfile"
    if args.recipe is not None and args.recipe.name.lower() == "palimpsestfile":
        return "palimpsestfile"
    return "dockerfile"


def _profile_payload(profile: RegistryProfile, *, is_default: bool) -> dict[str, object]:
    return {
        "name": profile.alias,
        "endpoint": profile.endpoint,
        "protocol": profile.protocol,
        "api_base": profile.api_base,
        "namespace": profile.namespace,
        "default": is_default,
        "mirrors": list(profile.mirrors),
        "ca": list(profile.ca),
        "plain_http": profile.plain_http,
        "tls_skip_verify": profile.tls_skip_verify,
        "cache_from": list(profile.cache_from),
        "cache_to": list(profile.cache_to),
    }


def _registry_server(config: RegistryConfig, server: str | None, registry_alias: str | None) -> str:
    if server is not None and registry_alias is not None:
        raise PalimpsestError("specify either a registry server or --registry, not both")
    if registry_alias is not None:
        return inspect_profile(config, registry_alias).endpoint
    if server is not None:
        registries = getattr(config, "registries", {})
        if server in registries:
            return registries[server].endpoint
        return server
    return select_registry_profile(config).endpoint


def _registry_operation_profile(
    config: RegistryConfig,
    server: str | None,
    registry_alias: str | None,
) -> RegistryProfile | None:
    """Return the configured login/logout profile; None is an unconfigured Docker authority."""
    if server is not None and registry_alias is not None:
        raise PalimpsestError("specify either a registry server or --registry, not both")
    if server is None:
        return select_registry_profile(config, explicit_alias=registry_alias)
    if server is not None and server in config.registries:
        return select_registry_profile(config, explicit_alias=server)
    endpoint = _registry_server(config, server, registry_alias)
    try:
        endpoint = normalize_endpoint(endpoint)
    except PalimpsestError:
        raise PalimpsestError("login server must be a configured registry authority or profile name") from None
    matches = [profile for profile in config.registries.values() if profile.endpoint == endpoint]
    if not matches:
        return None
    if len({(profile.protocol, profile.api_base) for profile in matches}) != 1:
        raise PalimpsestError("registry authority is ambiguous; select --registry explicitly")
    return next((profile for profile in matches if profile.alias == config.default), matches[0])


def _reference_profile(config: RegistryConfig, resolved: ResolvedImageReference) -> RegistryProfile | None:
    """Return the configured profile; None is an unconfigured ordinary OCI authority."""
    return None if resolved.registry_alias is None else inspect_profile(config, resolved.registry_alias)


def _is_native(profile: RegistryProfile | None) -> bool:
    return profile is not None and profile.protocol == "palimpsest"


def _native_target(resolved: ResolvedImageReference) -> tuple[str, str]:
    namespace, separator, package = resolved.repository.partition("/")
    if not separator or not namespace or not package:
        raise PalimpsestError("native reference requires namespace/package")
    return namespace, package


def _retain_package_archive(roots: StatePaths, snapshot: PackageSnapshot) -> Path:
    """Keep the verified frozen transport for --input, including directory sources."""
    digest = require_digest(snapshot.archive_digest)
    destination = roots.state / "package-artifacts" / (digest_hex(digest) + ".oci.tar")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if destination.is_symlink() or digest_file(destination) != digest:
            raise PalimpsestError("existing native package archive does not match its identity")
        return destination
    fd, temporary = tempfile.mkstemp(prefix=".package-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as target, snapshot.archive.open("rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if digest_file(Path(temporary)) != digest:
            raise PalimpsestError("native package snapshot changed while retaining its bytes")
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def _local_package_record(
    profile: RegistryProfile,
    resolved: ResolvedImageReference,
    snapshot: PackageSnapshot,
    archive: Path,
    *,
    project_id: str | None = None,
    build_id: str | None = None,
) -> LocalPackageReference:
    namespace, package = _native_target(resolved)
    return LocalPackageReference(
        reference=resolved.canonical,
        authority=profile.endpoint,
        api_base=profile.api_base,
        namespace=namespace,
        package=package,
        archive=str(archive.expanduser().absolute()),
        archive_digest=snapshot.archive_digest,
        archive_size_bytes=snapshot.archive_size_bytes,
        root_digest=snapshot.root_digest,
        package_type=snapshot.package_type,
        project_id=project_id,
        build_id=build_id,
    )


def _dispatch_native_registry(
    args: argparse.Namespace,
    roots: StatePaths,
    config: RegistryConfig,
) -> int | None:
    """Route native profiles before any Docker command is constructed."""
    op = args.operation
    if op in {"login", "logout"}:
        profile = _registry_operation_profile(config, args.server, args.registry)
        if not _is_native(profile):
            if args.namespace is not None:
                raise PalimpsestError("--namespace is supported only for native credentials")
            return None
        namespace = args.namespace or profile.namespace
        require_credential_helper(profile, namespace)
        if op == "logout":
            erase_package_key(profile, namespace)
            print("Native credential erased")
            return 0
        if args.password_stdin:
            credential = sys.stdin.read(512)
            if credential.endswith("\n"):
                credential = credential[:-1]
        else:
            if not sys.stdin.isatty():
                raise PalimpsestError("native login requires --password-stdin or an interactive terminal")
            credential = getpass.getpass("Package key: ")
        key_id = validate_package_key(credential)
        validate_package_username(args.username, key_id)
        client = NativePackageClient(profile, namespace=namespace, credential=credential)
        client.authenticate()
        store_package_key(profile, namespace, credential, args.username)
        print("Native login succeeded")
        return 0
    if op == "tag":
        target = resolve_image_reference(args.target, config, registry_alias=args.registry)
        if _is_native(_reference_profile(config, target)):
            raise PalimpsestError("native package tags are published with push, not Docker tag")
        source = resolve_image_reference(args.source, config)
        if _is_native(_reference_profile(config, source)):
            raise PalimpsestError("native packages cannot be tagged through Docker")
        return None
    if op not in {"push", "pull"}:
        return None
    resolved = resolve_image_reference(
        args.reference,
        config,
        registry_alias=args.registry,
        default_tag=not args.all_tags,
    )
    profile = _reference_profile(config, resolved)
    if not _is_native(profile):
        if op == "push" and (args.input is not None or args.manifest is not None):
            raise PalimpsestError("--input/--manifest are native package options")
        if op == "pull" and args.output is not None:
            raise PalimpsestError("--output is a native package pull option")
        return None
    if args.all_tags:
        raise PalimpsestError("native package transport does not support --all-tags")
    if args.platform is not None:
        raise PalimpsestError("native transfer preserves the complete selected graph; --platform is unsupported")
    namespace, package = _native_target(resolved)
    if op == "push" and (resolved.tag is None or resolved.digest is not None):
        raise PalimpsestError("native push requires a tag reference")
    client = NativePackageClient(profile, namespace=namespace)
    client.authorize(
        package, ("packages:inventory", "packages:write") if op == "push" else ("packages:inventory", "packages:read")
    )
    init_resolved_roots(roots)
    if op == "pull":
        output = args.output or (
            roots.state / "package-artifacts" / (hashlib.sha256(resolved.canonical.encode()).hexdigest() + ".oci.tar")
        )
        receipt = client.pull(package, tag=resolved.tag, digest=resolved.digest, destination=output)
        immutable = replace(resolved, tag=None, digest=receipt["digest"])
        with snapshot_package(output, manifest=receipt["digest"]) as snapshot:
            record = _local_package_record(profile, immutable, snapshot, output, project_id=client.project_id)
        record_path = write_package_reference(roots, replace(record, published_digest=record.root_digest))
        print(
            json.dumps(
                {
                    "reference": immutable.canonical,
                    "digest": record.root_digest,
                    "output": str(output),
                    "receipt": str(record_path),
                }
            )
        )
        return 0
    existing = None if args.input is not None else read_package_reference(roots, resolved.canonical)
    if existing is not None and (
        existing.api_base != profile.api_base
        or existing.namespace != namespace
        or (existing.project_id is not None and existing.project_id != client.project_id)
    ):
        raise PalimpsestError("local package reference does not match the authenticated native target")
    source = args.input if existing is None else Path(existing.archive)
    manifest = args.manifest if args.manifest is not None else (existing.root_digest if existing else None)
    with snapshot_package(source, manifest=manifest) as snapshot:
        if existing is not None and (
            snapshot.root_digest != existing.root_digest
            or snapshot.archive_digest != existing.archive_digest
            or snapshot.archive_size_bytes != existing.archive_size_bytes
        ):
            raise PalimpsestError("local package source changed since its typed reference was recorded")
        archive = _retain_package_archive(roots, snapshot)
        record = _local_package_record(
            profile,
            resolved,
            snapshot,
            archive,
            project_id=client.project_id,
            build_id=existing.build_id if existing else None,
        )
        write_package_reference(roots, record)
        receipt = client.push(
            package, resolved.tag, snapshot, provenance={"build_id": record.build_id} if record.build_id else None
        )
    path = write_package_reference(roots, replace(record, published_digest=receipt["digest"]))
    print(json.dumps({**receipt, "receipt": str(path)}))
    return 0


def _write_generated_config(path: Path, content: str, *, force: bool) -> None:
    target = path.expanduser().resolve(strict=False)
    if target.exists() and not force:
        raise PalimpsestError(f"output already exists; pass --force to replace it: {target}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        fsync_directory(target.parent)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _merge_unique(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _resolve_docker_inspect_reference(
    reference: str,
    config: RegistryConfig,
    registry_alias: str | None,
) -> str:
    if _DOCKER_IMAGE_ID_RE.fullmatch(reference):
        return reference
    resolved = resolve_image_reference(reference, config, registry_alias=registry_alias)
    if _is_native(_reference_profile(config, resolved)):
        raise PalimpsestError("native packages are not Docker-local images; use native pull --output")
    return resolved.canonical


def _resolve_runtime_stack(
    store: ContentStore,
    image_or_bundle: str | Path,
    layer_digests: Sequence[str],
    explicit_url: str | None,
    *,
    requested_backend: str = "auto",
    preflight_for_run: bool = False,
    run_network: str | None = None,
) -> StackRef:
    target_path = Path(image_or_bundle)
    if target_path.is_dir():
        layout = verify_layout_dir(target_path)
        if len(layout.manifests) != 1:
            raise PalimpsestError("run bundle must contain exactly one selectable manifest")
        entries = layout.manifests[0].entries
        if not entries:
            raise PalimpsestError("run bundle does not contain a boot image")
        base_entry = entries[0]
        disk_format = next(
            (
                candidate
                for candidate, media_type in DISK_FORMAT_MEDIA_TYPES.items()
                if media_type == base_entry.media_type
            ),
            None,
        )
        if disk_format is None:
            raise PalimpsestError("first bundle artifact must be a qcow2 or raw cloud-image")
        if any(entry.media_type != MEDIA_TYPE_LAYER_SQUASHFS for entry in entries[1:]):
            raise PalimpsestError("bundle layers must all be SquashFS descriptors")
        if base_entry.arch is None:
            raise PalimpsestError(
                "bundle base architecture is unknown; use a bundle with per-artifact architecture metadata"
            )
        base_ref = ImageRef(
            digest=base_entry.digest,
            disk_format=disk_format,
            arch=base_entry.arch,
            os_variant=None,
            local_path=base_entry.local_path,
        )
        bundle_layers = tuple(
            LayerRef(digest=entry.digest, media_type=entry.media_type, local_path=entry.local_path, arch=entry.arch)
            for entry in entries[1:]
        )
        continuation_parent = bundle_layers[-1].digest if bundle_layers else None
        extra_layers = _layer_refs_from_store(
            store,
            layer_digests,
            base_ref.digest,
            parent_digest=continuation_parent,
        )
        return StackRef(base=base_ref, layers=bundle_layers + extra_layers)
    image_digest = os.fspath(image_or_bundle)
    base_ref = _resolve_image_ref(
        store,
        image_digest,
        explicit_url,
        requested_backend=requested_backend,
        preflight_for_run=preflight_for_run,
        run_network=run_network,
    )
    return StackRef(base=base_ref, layers=_layer_refs_from_store(store, layer_digests, base_ref.digest))


def _compose_invocation_directory() -> Path:
    try:
        return Path.cwd().resolve()
    except OSError as exc:
        raise PalimpsestError("cannot resolve the current working directory") from exc


def _compose_project_directory(
    args: argparse.Namespace,
    project_file: Path | None = None,
    *,
    invocation_directory: Path | None = None,
) -> Path:
    """Return Compose's effective working directory for project resources."""

    invocation = invocation_directory or _compose_invocation_directory()
    if args.project_directory is None:
        directory = invocation if project_file is None else project_file.parent
    else:
        raw_directory = args.project_directory.expanduser()
        directory = raw_directory if raw_directory.is_absolute() else invocation / raw_directory
    try:
        directory = directory.resolve(strict=True)
    except OSError as exc:
        raise PalimpsestError(f"project directory does not exist: {directory}") from exc
    if not directory.is_dir():
        raise PalimpsestError(f"project directory does not exist: {directory}")
    return directory


def _compose_project_path(
    args: argparse.Namespace,
    *,
    invocation_directory: Path | None = None,
) -> Path:
    """Resolve Compose-style project discovery without changing process cwd."""

    invocation = invocation_directory or _compose_invocation_directory()
    discovery_directory = _compose_project_directory(args, invocation_directory=invocation)
    if args.project_file is not None:
        candidate = args.project_file.expanduser()
        return (candidate if candidate.is_absolute() else invocation / candidate).resolve(strict=False)
    primary = discovery_directory / DEFAULT_PROJECT_FILE
    alternate = discovery_directory / "palimpsest.yaml"
    if primary.exists():
        return primary
    if alternate.exists():
        return alternate
    raise PalimpsestError(f"project file not found; expected {primary} or {alternate}")


def _compose_environment(
    project_root: Path,
    env_files: Sequence[Path],
    *,
    invocation_directory: Path | None = None,
) -> dict[str, str]:
    """Load project-contained dotenv inputs with the process environment on top."""

    invocation = invocation_directory or _compose_invocation_directory()
    selected = list(env_files)
    if not selected and (project_root / ".env").is_file():
        selected.append(Path(".env"))
    references: list[Path] = []
    for raw in selected:
        candidate = raw.expanduser()
        if env_files:
            candidate = candidate if candidate.is_absolute() else invocation / candidate
        else:
            candidate = project_root / candidate
        # Normalize '.'/'..' lexically without following symlinks.  The project
        # loader performs the component-wise symlink check after containment.
        absolute = Path(os.path.abspath(os.fspath(candidate)))
        try:
            reference = absolute.relative_to(project_root)
        except ValueError as exc:
            raise PalimpsestError(f"--env-file must stay inside the project directory: {raw}") from exc
        references.append(reference)
    return load_interpolation_environment(project_root, references, base=os.environ)


def _load_compose_project(args: argparse.Namespace) -> tuple[Project, dict[str, str]]:
    invocation = _compose_invocation_directory()
    project_file = _compose_project_path(args, invocation_directory=invocation)
    project_root = _compose_project_directory(args, project_file, invocation_directory=invocation)
    environment = _compose_environment(project_root, args.env_file, invocation_directory=invocation)
    # Only config validates every desired source at load time. Up preflights
    # selected live policies; teardown/observation must not inspect bind sources.
    project = load_project(
        project_file,
        environment=environment,
        project_root=project_root,
        validate_bind_sources=args.compose_operation == "config",
    )
    name_override = (
        args.project_name or environment.get("PALIMPSEST_PROJECT_NAME") or environment.get("COMPOSE_PROJECT_NAME")
    )
    if name_override:
        project = replace(
            project,
            name=deterministic_project_name(
                project.source,
                name_override,
                project_directory=project.root,
            ),
        )
    return project, environment


def _compose_callbacks(
    project: Project,
    environment: dict[str, str],
    roots: StatePaths,
    store: ContentStore,
    explicit_url: str | None,
):
    def resolve_stack(service: ServiceSpec) -> StackRef:
        target: str | Path
        if service.bundle is not None:
            target = service.bundle.path
        elif service.image is not None:
            target = service.image
        else:  # pragma: no cover - project schema enforces exactly one source
            raise PalimpsestError(f"service {service.name!r} has no boot image or bundle")
        return _resolve_runtime_stack(
            store,
            target,
            service.layers,
            explicit_url,
            preflight_for_run=True,
        )

    return build_project_callbacks(project, roots, resolve_stack, environment=environment)


def _managed_compose_runtime_state(
    project: Project,
    service: str,
    callbacks: object,
    roots: StatePaths,
) -> tuple[str, InspectRecord]:
    """Capture one live inspection while the project ledger verifies its owner."""

    inspect_callback = getattr(callbacks, "inspect", None)
    if not callable(inspect_callback):
        raise PalimpsestError("project runtime does not provide inspection")
    captured: dict[str, object | None] = {}

    def capture(run_name: str) -> object | None:
        inspected = inspect_callback(run_name)
        captured[run_name] = inspected
        return inspected

    run_name = managed_run_name(project, service, roots=roots, inspect=capture)
    inspected = captured.get(run_name)
    if not isinstance(inspected, InspectRecord):
        raise PalimpsestError(f"managed run {run_name!r} did not return a typed inspect record")
    status = inspected.lifecycle.status
    if status not in {"creating", "defined", "starting", "running", "stopping", "stopped", "removed", "failed"}:
        raise PalimpsestError(f"managed run {run_name!r} inspection is missing runtime status")
    if status == "removed":
        raise PalimpsestError(f"managed run {run_name!r} has been removed")
    return run_name, inspected


def _applied_compose_ports(
    project: Project,
    service: str,
    callbacks: object,
    roots: StatePaths,
) -> tuple[tuple[str, int, int, str], ...]:
    """Return validated port bindings recorded on the owner-verified runtime."""

    run_name, inspected = _managed_compose_runtime_state(project, service, callbacks, roots)
    raw_ports = inspected.detail.ports
    result: list[tuple[str, int, int, str]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for index, raw_port in enumerate(raw_ports):
        raw_host_ip = raw_port.host_ip
        host_port = raw_port.host_port
        guest_port = raw_port.guest_port
        protocol = raw_port.protocol
        host_ip = normalize_host_ip(
            raw_host_ip,
            f"managed run {run_name!r} applied port [{index}].host_ip",
        )
        if (
            isinstance(host_port, bool)
            or not isinstance(host_port, int)
            or not 1 <= host_port <= 65_535
            or isinstance(guest_port, bool)
            or not isinstance(guest_port, int)
            or not 1 <= guest_port <= 65_535
            or not isinstance(protocol, str)
            or protocol not in {"tcp", "udp"}
        ):
            raise PalimpsestError(f"managed run {run_name!r} applied port [{index}] is malformed")
        binding = (host_ip, host_port, guest_port, protocol)
        if binding in seen:
            raise PalimpsestError(f"managed run {run_name!r} contains duplicate applied port state")
        seen.add(binding)
        result.append(binding)
    return tuple(result)


def _dispatch_compose(
    args: argparse.Namespace,
    roots: StatePaths,
    store: ContentStore,
) -> int:
    project, environment = _load_compose_project(args)
    operation = args.compose_operation

    if operation == "config":
        if args.quiet:
            return 0
        if args.services:
            for service_name in sorted(project.services):
                print(service_name)
        else:
            sys.stdout.write(canonical_project_json(project))
        return 0

    callbacks = _compose_callbacks(project, environment, roots, store, args.url)
    if operation == "up":
        result = up_project(
            project,
            callbacks,
            roots=roots,
            services=args.services or None,
            no_recreate=args.no_recreate,
            force_recreate=args.force_recreate,
        )
        for plan in result.actions:
            print(f"{plan.service}\t{plan.action}")
        return 0
    if operation == "down":
        result = down_project(project, callbacks, roots=roots, volumes=args.volumes)
        for service_name in result.removed_services:
            print(f"removed\t{service_name}")
        for volume_name in result.removed_volumes:
            print(f"removed volume\t{volume_name}")
        return 0
    if operation == "ps":
        rows = project_ps(project, callbacks.inspect, roots=roots)
        if args.services:
            unknown = sorted(set(args.services) - set(project.services))
            if unknown:
                raise PalimpsestError(f"unknown selected service(s): {', '.join(unknown)}")
            selected = set(args.services)
            rows = tuple(row for row in rows if row.service in selected)
        if args.format == "json":
            print(
                json.dumps(
                    [{"service": row.service, "name": row.run_name, "status": row.status} for row in rows],
                    indent=2,
                )
            )
        else:
            _print_table(
                ("SERVICE", "NAME", "STATUS"),
                [(row.service, row.run_name, row.status) for row in rows],
            )
        return 0
    if operation == "logs":
        selected = args.services or None
        targets = project_log_targets(project, selected, roots=roots)
        if args.follow and len(targets) != 1:
            raise PalimpsestError("compose logs --follow currently requires exactly one service")
        pending: dict[str, _ComposeLogRenderState] = {}
        for service_name, event in project_logs(
            project,
            callbacks,
            selected,
            roots=roots,
            follow=args.follow,
        ):
            _render_compose_log_event(service_name, event, pending, sys.stdout)
            sys.stdout.flush()
        return 0
    if operation == "exec":

        def execute_managed(
            run_name: str,
            *,
            expected_identity: ExpectedRunIdentity | None = None,
        ) -> object:
            session = runtime_dispatch.exec(
                run_name,
                args.command,
                roots=roots,
                expected_identity=expected_identity,
            )
            return _run_process_session(session, interactive=False)

        return int(
            project_service_operation(
                project,
                args.service,
                callbacks.inspect,
                execute_managed,
                roots=roots,
            )
        )
    if operation == "stop":
        for service_name in stop_project_services(
            project,
            callbacks,
            args.services or None,
            roots=roots,
        ):
            print(f"stopped\t{service_name}")
        return 0
    if operation == "port":
        if args.service not in project.services:
            raise PalimpsestError(f"unknown project service: {args.service!r}")
        matches = [
            port
            for port in _applied_compose_ports(project, args.service, callbacks, roots)
            if port[2] == args.private_port and port[3] == args.protocol
        ]
        if not matches:
            raise PalimpsestError(
                f"service {args.service!r} has no applied publication for {args.private_port}/{args.protocol}"
            )
        for host_ip, host_port, _guest_port, _protocol in matches:
            rendered_host = f"[{host_ip}]" if ":" in host_ip else host_ip
            print(f"{rendered_host}:{host_port}")
        return 0
    raise PalimpsestError(f"unsupported compose operation: {operation}")


def dispatch_args(args: argparse.Namespace) -> int:
    op = args.operation
    if op == "completion":
        try:
            if args.install:
                print(completion.install_completion(args.shell))
            else:
                print(completion.generate_completion_script(args.shell))
        except (ValueError, OSError) as exc:
            raise PalimpsestError(f"cannot prepare {args.shell} completion: {exc}") from exc
        return 0
    if op == "docker":
        argv = docker_command_argv(resolve_docker_config_dir(), *(args.docker_args or ["--help"]))
        try:
            return run_docker_passthrough(argv).returncode
        except (PalimpsestError, FileNotFoundError) as exc:
            if isinstance(exc, FileNotFoundError) or isinstance(exc.__cause__, FileNotFoundError):
                raise PalimpsestError(
                    "Docker CLI executable not found; install Docker and ensure 'docker' is on PATH, "
                    "then retry palimpsest docker --help"
                ) from exc
            raise
    if op == "oci" and args.oci_operation == "init-runtime":
        from .oci_host import create_runtime_parent

        try:
            selected = args.path.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise PalimpsestError("OCI runtime parent path is invalid") from None
        runtime_parent = create_runtime_parent(selected)
        print("PALIMPSEST_STATE_HOME=" + shlex.quote(str(runtime_parent / "state")))
        return 0
    if op == "oci" and args.oci_operation == "resource-status":
        from .oci_resource_status import resource_status

        print(json.dumps(resource_status(), indent=2, sort_keys=True))
        return 0
    if op == "oci" and args.oci_operation == "exec-record":
        from .oci_exec_record import read_exec_record

        print(json.dumps(read_exec_record(args.path).to_dict(), indent=2, sort_keys=True))
        return 0
    if op == "oci" and args.oci_operation in {"root-volumes", "root-volume"}:
        from .oci_root_volume_inventory import root_volume, root_volumes

        try:
            inventory_roots = resolve_roots()
        except (PalimpsestError, OSError, ValueError, TypeError, OverflowError, RecursionError):
            raise PalimpsestError("OCI root-volume metadata is unavailable or inconsistent") from None
        payload = (
            root_volumes(inventory_roots)
            if args.oci_operation == "root-volumes"
            else root_volume(inventory_roots, args.volume_id)
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    read_only_root_operations = {
        "run",
        "start",
        "stop",
        "rm",
        "inspect",
        "logs",
        "ps",
        "volumes",
        "volume",
        "networks",
        "exec",
        "shell",
    }
    read_only_oci_operations = {"root-proof", "exec-status", "network"}
    deferred_package_state = op in {"login", "logout", "push", "pull", "tag"} or (
        op == "build" and _selected_build_frontend(args) == "dockerfile"
    )
    roots = (
        resolve_roots()
        if deferred_package_state
        or op in read_only_root_operations
        or (op == "oci" and args.oci_operation in read_only_oci_operations)
        else init_roots()
    )

    if op == "volume":
        from . import named_volumes

        if args.volume_operation == "create":
            payload = named_volumes.create_named_volume(
                roots, name=args.name, backend=args.backend, size_bytes=args.size
            )
        elif args.volume_operation == "inspect":
            payload = next(
                (
                    item
                    for item in named_volumes.list_named_volumes(roots)
                    if item["name"] == args.name and item["backend"] == args.backend
                ),
                None,
            )
            if payload is None:
                raise PalimpsestError(f"named volume {args.name!r} does not exist for backend {args.backend!r}")
        else:
            payload = named_volumes.delete_named_volume(roots, name=args.name, backend=args.backend)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if op == "oci":
        if args.oci_operation == "pull":
            receipt = pull_anonymous_oci_archive(
                args.reference,
                args.output,
                roots,
                platform=args.platform,
                timeout_seconds=args.timeout,
            )
            print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.oci_operation == "root-proof":
            from .oci_root_proof import root_proof

            print(json.dumps(root_proof(roots, args.name), indent=2, sort_keys=True))
            return 0
        if args.oci_operation == "exec-status":
            from .oci_exec_status import exec_status

            print(json.dumps(exec_status(roots, args.name), indent=2, sort_keys=True))
            return 0
        if args.oci_operation == "network":
            from .oci_network_status import network_status

            print(json.dumps(network_status(roots, args.name), indent=2, sort_keys=True))
            return 0
        oci_roots = StatePaths(
            config=roots.config.resolve(strict=True),
            state=roots.state.resolve(strict=True),
        )
        manifest_digest = require_digest(args.manifest) if args.manifest is not None else None
        try:
            source_path = args.source.expanduser().resolve(strict=True)
        except OSError:
            raise PalimpsestError(f"local OCI source does not exist: {args.source}") from None
        try:
            packer_path = args.packer.expanduser().resolve(strict=True)
        except OSError:
            raise PalimpsestError(f"SquashFS packer does not exist: {args.packer}") from None
        if not packer_path.is_file():
            raise PalimpsestError(f"SquashFS packer does not exist: {packer_path}")
        source = (
            LocalLayoutSource(layout=source_path, root_digest=manifest_digest)
            if source_path.is_dir()
            else LocalArchiveSource(archive=source_path, root_digest=manifest_digest)
        )
        reference = (
            OCIImageRef(
                registry="local.palimpsest.invalid",
                repository="imported/image",
                requested_reference=f"local.palimpsest.invalid/imported/image@{manifest_digest}",
            )
            if manifest_digest is not None
            else None
        )
        source_cas = SourceCAS(oci_roots.oci_source_cas)
        snapshot = source.snapshot(reference, source_cas)
        toolchain = discover_squashfs_toolchain(
            packer_path,
            expected_packer_sha256=digest_hex(digest_file(packer_path)),
        )
        receipt = materialize_image_hard(
            snapshot,
            source_cas_root=oci_roots.oci_source_cas,
            roots=oci_roots,
            store=OCIStore(oci_roots),
            packer_path=packer_path,
            toolchain=toolchain,
            timeout_seconds=args.timeout,
        )
        payload = {**receipt.to_dict(), "receipt_digest": receipt.digest}
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            _write_generated_config(args.output, rendered, force=False)
            print(args.output.expanduser().resolve(strict=False))
        return 0
    store = ContentStore(roots.store)
    if op == "registry":
        reg_op = args.registry_operation
        if reg_op == "ls":
            config = load_registry_config(roots)
            payload = [
                _profile_payload(profile, is_default=profile.alias == config.default)
                for profile in list_profiles(config)
            ]
            if args.format == "json":
                print(json.dumps(payload, indent=2))
            else:
                print(f"{'NAME':<16} {'ENDPOINT':<32} {'NAMESPACE':<24} {'DEFAULT':<7}")
                for item in payload:
                    print(
                        f"{str(item['name']):<16} {str(item['endpoint']):<32} "
                        f"{str(item['namespace'] or '-'):<24} {'yes' if item['default'] else 'no':<7}"
                    )
        elif reg_op == "add":
            namespace = (
                args.namespace if args.namespace is not None else ("library" if args.name.lower() == "docker" else "")
            )
            profile = RegistryProfile(
                alias=args.name,
                endpoint=args.endpoint,
                protocol=args.protocol,
                api_base=args.api_base or "",
                namespace=namespace,
                mirrors=tuple(args.mirror),
                ca=tuple(os.fspath(path.expanduser().resolve(strict=False)) for path in args.ca),
                plain_http=args.plain_http,
                tls_skip_verify=args.tls_skip_verify,
                cache_from=tuple(args.cache_from),
                cache_to=tuple(args.cache_to),
            )

            def add_transform(config: RegistryConfig) -> RegistryConfig:
                updated = add_profile(config, profile, replace_existing=args.force)
                return use_profile(updated, profile.alias) if args.default else updated

            updated = update_registry_config(roots, add_transform)
            print(
                json.dumps(
                    _profile_payload(updated.registries[profile.alias], is_default=updated.default == profile.alias)
                )
            )
        elif reg_op == "use":
            updated = update_registry_config(roots, lambda config: use_profile(config, args.name))
            print(updated.default)
        elif reg_op == "rm":
            updated = update_registry_config(roots, lambda config: remove_profile(config, args.name))
            print(updated.default)
        elif reg_op == "inspect":
            config = load_registry_config(roots)
            profile = inspect_profile(config, args.name)
            print(json.dumps(_profile_payload(profile, is_default=profile.alias == config.default), indent=2))
        elif reg_op == "buildkit-config":
            config = load_registry_config(roots)
            _write_generated_config(args.output, render_buildkitd_toml(config), force=args.force)
            print(str(args.output.expanduser().resolve(strict=False)))
    elif op in {"login", "logout", "pull", "push", "tag", "images", "history", "rmi", "save", "load"}:
        docker_config = resolve_docker_config_dir()
        config = load_registry_config(roots) if op in {"login", "logout", "pull", "push", "tag"} else None
        if config is not None:
            native_result = _dispatch_native_registry(args, roots, config)
            if native_result is not None:
                return native_result
        if op == "login":
            assert config is not None
            server = _registry_server(config, args.server, args.registry)
            argv = docker_login_argv(
                docker_config,
                server,
                username=args.username,
                password_stdin=args.password_stdin,
            )
        elif op == "logout":
            assert config is not None
            argv = docker_logout_argv(docker_config, _registry_server(config, args.server, args.registry))
        elif op == "pull":
            assert config is not None
            resolved = resolve_image_reference(
                args.reference,
                config,
                registry_alias=args.registry,
                default_tag=not args.all_tags,
            )
            if args.all_tags and (resolved.tag is not None or resolved.digest is not None):
                raise PalimpsestError("--all-tags requires an untagged repository reference")
            reference = resolved.name if args.all_tags else resolved.canonical
            argv = docker_pull_argv(
                docker_config,
                reference,
                platform=args.platform,
                all_tags=args.all_tags,
                quiet=args.quiet,
            )
        elif op == "push":
            assert config is not None
            resolved = resolve_image_reference(
                args.reference,
                config,
                registry_alias=args.registry,
                default_tag=not args.all_tags,
            )
            if args.all_tags and (resolved.tag is not None or resolved.digest is not None):
                raise PalimpsestError("--all-tags requires an untagged repository reference")
            reference = resolved.name if args.all_tags else resolved.canonical
            argv = docker_push_argv(
                docker_config,
                reference,
                platform=args.platform,
                all_tags=args.all_tags,
                quiet=args.quiet,
            )
        elif op == "tag":
            assert config is not None
            target = resolve_image_reference(args.target, config, registry_alias=args.registry).canonical
            argv = docker_tag_argv(docker_config, args.source, target)
        elif op == "images":
            argv = docker_images_argv(
                docker_config,
                args.repository,
                all_images=args.all,
                digests=args.digests,
                quiet=args.quiet,
                no_trunc=args.no_trunc,
                tree=args.tree,
                filters=tuple(args.filter),
                output_format=args.format,
            )
        elif op == "history":
            argv = docker_history_argv(
                docker_config,
                args.reference,
                no_trunc=args.no_trunc,
                quiet=args.quiet,
                output_format=args.format,
                platform=args.platform,
            )
        elif op == "rmi":
            argv = docker_image_rm_argv(
                docker_config,
                args.references,
                force=args.force,
                no_prune=args.no_prune,
                platforms=tuple(args.platform),
            )
        elif op == "save":
            argv = docker_save_argv(
                docker_config,
                args.references,
                output=args.output,
                platforms=tuple(args.platform),
            )
        else:
            argv = docker_load_argv(
                docker_config,
                input_path=args.input_path,
                platforms=tuple(args.platform),
                quiet=args.quiet,
            )
        return run_docker_passthrough(argv).returncode
    elif op == "image":
        img_op = args.image_operation
        if img_op == "inspect":
            if all(_DOCKER_IMAGE_ID_RE.fullmatch(reference) for reference in args.references):
                references = list(args.references)
            else:
                config = load_registry_config(roots)
                references = [
                    _resolve_docker_inspect_reference(reference, config, args.registry) for reference in args.references
                ]
            argv = docker_image_inspect_argv(
                resolve_docker_config_dir(),
                references,
                platform=args.platform,
                output_format=args.format,
            )
            return run_docker_passthrough(argv).returncode
        if img_op == "history":
            argv = docker_history_argv(
                resolve_docker_config_dir(),
                args.reference,
                no_trunc=args.no_trunc,
                quiet=args.quiet,
                output_format=args.format,
                platform=args.platform,
            )
            return run_docker_passthrough(argv).returncode
        if img_op == "rm":
            argv = docker_image_rm_argv(
                resolve_docker_config_dir(),
                args.references,
                force=args.force,
                no_prune=args.no_prune,
                platforms=tuple(args.platform),
            )
            return run_docker_passthrough(argv).returncode
        if img_op == "save":
            argv = docker_save_argv(
                resolve_docker_config_dir(),
                args.references,
                output=args.output,
                platforms=tuple(args.platform),
            )
            return run_docker_passthrough(argv).returncode
        if img_op == "load":
            argv = docker_load_argv(
                resolve_docker_config_dir(),
                input_path=args.input_path,
                platforms=tuple(args.platform),
                quiet=args.quiet,
            )
            return run_docker_passthrough(argv).returncode
        if img_op == "ls":
            client = HubClient(resolve_url(args.url), resolve_token())
            items = client.list_images(
                ubuntu_base=args.ubuntu_base,
                arch=args.arch,
                os_variant=args.os_variant,
                disk_format=args.disk_format,
                limit=args.limit,
            )
            for item in items:
                if isinstance(item, dict):
                    digest_val = item.get("digest", "")
                    name_val = item.get("name", "")
                    df_val = item.get("disk_format", "")
                    arch_val = item.get("arch", "")
                    print(f"{digest_val}\t{name_val}\t{df_val}\t{arch_val}".strip())
                else:
                    print(str(item))
        elif img_op == "pull":
            norm_digest = require_digest(args.digest)
            client = HubClient(resolve_url(args.url), resolve_token())
            meta = client.get_layer(norm_digest)
            kind = meta.get("kind")
            if kind != KIND_CLOUD_IMAGE:
                raise PalimpsestError(f"digest {norm_digest} is not a cloud-image (kind={kind})")
            if meta.get("disk_format") not in DISK_FORMAT_MEDIA_TYPES or meta.get("arch") not in {"x86_64", "aarch64"}:
                raise PalimpsestError(f"cloud-image metadata is incomplete for {norm_digest}")
            blob_path = _pull_blob_into_store(client, store, norm_digest)
            store.write_metadata(
                norm_digest,
                {
                    "kind": KIND_CLOUD_IMAGE,
                    "disk_format": meta.get("disk_format"),
                    "arch": meta.get("arch"),
                    "os_variant": meta.get("os_variant"),
                    "ubuntu_base": meta.get("ubuntu_base"),
                    "name": meta.get("name"),
                },
            )
            if args.output:
                df = meta.get("disk_format", "qcow2")
                out_file = args.output / f"{digest_hex(norm_digest)}.{df}"
                args.output.mkdir(parents=True, exist_ok=True)
                if out_file.is_file():
                    if digest_file(out_file) != norm_digest:
                        raise PalimpsestError(f"output target digest mismatch: {out_file}")
                else:
                    shutil.copy2(blob_path, out_file)
        elif img_op == "verify":
            if not args.path.is_file():
                raise PalimpsestError(f"file not found: {args.path}")
            norm_digest = require_digest(args.digest)
            actual = digest_file(args.path)
            if actual != norm_digest:
                raise PalimpsestError(f"digest mismatch: expected {norm_digest}, got {actual}")
            print(f"{norm_digest} ok")
        elif img_op == "push":
            if not args.path.is_file():
                raise PalimpsestError(f"image path not found: {args.path}")
            client = HubClient(resolve_url(args.url), resolve_token())
            metadata = {
                "name": args.name,
                "kind": KIND_CLOUD_IMAGE,
                "disk_format": args.disk_format,
                "arch": args.arch,
                "os_variant": args.os_variant,
                "ubuntu_base": args.ubuntu_base,
                "is_published": args.publish,
            }
            res = client.push_blob(args.path, metadata)
            image_digest = digest_file(args.path)
            store.ingest_file(args.path, expected_digest=image_digest)
            store.write_metadata(image_digest, metadata)
            print(res.get("blob_digest", image_digest))
        elif img_op == "import":
            rec = import_cloud_image(
                roots,
                args.path,
                disk_format=args.disk_format,
                arch=args.arch,
                os_variant=args.os_variant,
            )
            print(rec["digest"])
    elif op == "layer":
        lyr_op = args.layer_operation
        if lyr_op == "ls":
            client = HubClient(resolve_url(args.url), resolve_token())
            items = client.list_layers(
                name=args.name,
                kind=args.kind,
                parent_digest=args.parent,
                limit=args.limit,
            )
            for item in items:
                if isinstance(item, dict):
                    digest_val = item.get("digest", "")
                    name_val = item.get("name", "")
                    kind_val = item.get("kind", "")
                    print(f"{digest_val}\t{name_val}\t{kind_val}".strip())
                else:
                    print(str(item))
        elif lyr_op == "pull":
            norm_digest = require_digest(args.digest)
            client = HubClient(resolve_url(args.url), resolve_token())
            meta = client.get_layer(norm_digest)
            if meta.get("kind") != "squashfs" or meta.get("media_type") != MEDIA_TYPE_LAYER_SQUASHFS:
                raise PalimpsestError(f"digest {norm_digest} is not a SquashFS layer")
            blob_path = _pull_blob_into_store(client, store, norm_digest)
            store.write_metadata(
                norm_digest,
                {
                    "kind": meta.get("kind"),
                    "media_type": meta.get("media_type", MEDIA_TYPE_LAYER_SQUASHFS),
                    "parent_digest": meta.get("parent_digest"),
                    "base_image_digest": meta.get("base_image_digest"),
                    "name": meta.get("name"),
                },
            )
            if args.output:
                out_file = args.output / f"{digest_hex(norm_digest)}.squashfs"
                args.output.mkdir(parents=True, exist_ok=True)
                if out_file.is_file():
                    if digest_file(out_file) != norm_digest:
                        raise PalimpsestError(f"output target digest mismatch: {out_file}")
                else:
                    shutil.copy2(blob_path, out_file)
        elif lyr_op == "pack":
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_file = Path(tmpdir) / f"{args.tag}.squashfs"
                cmd = build_mksquashfs_command(args.directory, tmp_file)
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise PalimpsestError(f"mksquashfs failed: {proc.stderr}")
                d = digest_file(tmp_file)
                size = tmp_file.stat().st_size
                store.ingest_file(tmp_file, expected_digest=d)
                store.write_metadata(
                    d,
                    {
                        "kind": "squashfs",
                        "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                        "parent_digest": None,
                        "base_image_digest": None,
                    },
                )
                record = TagRecord(
                    schema_version=1,
                    tag=args.tag,
                    digest=d,
                    media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                    size_bytes=size,
                    parent_digest=None,
                    base_image_digest=None,
                    source="pack",
                    created_at=datetime.now(UTC).isoformat(),
                )
                write_tag_record(roots, record)
                print(d)
        elif lyr_op == "push":
            client = HubClient(resolve_url(args.url), resolve_token())
            val = args.value
            tag_rec = None
            try:
                tag_rec = read_tag_record(roots, val)
            except Exception:
                tag_rec = None
            path_val = Path(val)
            if tag_rec is None and not path_val.is_file():
                raise PalimpsestError(
                    f"layer push target '{val}' not found as tag record ({roots.tags}/{val}.json) or local file"
                )
            if tag_rec is not None:
                path = store.blob_path(tag_rec.digest)
                store_metadata = store.read_metadata(tag_rec.digest)
                if args.name and args.name != tag_rec.tag:
                    raise PalimpsestError(f"--name '{args.name}' conflicts with tag '{tag_rec.tag}'")
                name = tag_rec.tag
                parent_digest = tag_rec.parent_digest
                if args.parent:
                    norm_p = require_digest(args.parent)
                    if parent_digest and parent_digest != norm_p:
                        raise PalimpsestError(f"--parent '{norm_p}' conflicts with tag parent '{parent_digest}'")
                    parent_digest = norm_p
                base_image_digest = tag_rec.base_image_digest
                if args.base_image:
                    norm_b = require_digest(args.base_image)
                    if base_image_digest and base_image_digest != norm_b:
                        raise PalimpsestError(f"--base-image '{norm_b}' conflicts with tag base '{base_image_digest}'")
                    base_image_digest = norm_b
                chain_id = store_metadata.get("runtime_pack_manifest_digest")
                arch = store_metadata.get("arch")
                if tag_rec.source == "buildkit-runtime-pack" or chain_id is not None:
                    if not isinstance(chain_id, str):
                        raise PalimpsestError("runtime-pack metadata is missing runtime_pack_manifest_digest")
                    chain_id = require_digest(chain_id)
                    if arch not in {"x86_64", "aarch64"}:
                        raise PalimpsestError(f"runtime-pack metadata has invalid architecture: {arch!r}")
                    metadata_base = store_metadata.get("base_image_digest")
                    if metadata_base != base_image_digest:
                        raise PalimpsestError(
                            "runtime-pack metadata base_image_digest conflicts with its immutable tag record"
                        )
                    if store_metadata.get("parent_digest") is not None:
                        raise PalimpsestError("runtime-pack metadata must not declare a parent layer")
            else:
                if not args.name:
                    raise PalimpsestError("--name is required when pushing a layer path")
                path = path_val
                name = args.name
                parent_digest = require_digest(args.parent) if args.parent else None
                base_image_digest = require_digest(args.base_image) if args.base_image else None
                chain_id = None
                arch = None
            metadata = {
                "name": name,
                "kind": "squashfs",
                "parent_digest": parent_digest,
                "base_image_digest": base_image_digest,
                "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                "ubuntu_base": args.ubuntu_base,
                "is_published": args.publish,
            }
            if chain_id is not None:
                metadata["chain_id"] = chain_id
            if arch is not None:
                metadata["arch"] = arch
            res = client.push_blob(path, metadata)
            print(res.get("blob_digest", digest_file(path)))

    elif op == "bundle":
        bnd_op = args.bundle_operation
        if bnd_op == "pull":
            client = HubClient(resolve_url(args.url), resolve_token())
            norm_leaf = require_digest(args.leaf_digest)
            args.output.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_tar = Path(tmpdir) / "bundle.tar"
                client.pull_bundle(norm_leaf, tmp_tar, include_base_image=args.include_base)
                extract_bundle_tar(tmp_tar, args.output)
            print(str(args.output))
        elif bnd_op == "unpack":
            import tarfile

            try:
                extract_bundle_tar(args.archive, args.output)
            except (OSError, tarfile.TarError) as exc:
                raise PalimpsestError(f"cannot unpack bundle archive {args.archive}: {exc}") from exc
            print(str(args.output))
        elif bnd_op == "verify":
            layout = verify_layout_dir(args.directory)
            for m in layout.manifests:
                for entry in m.entries:
                    print(f"{entry.digest} ok")

    elif op == "build":
        if _selected_build_frontend(args) == "palimpsestfile":
            recipe_path = args.recipe or Path("./Palimpsestfile")
            if not recipe_path.is_file():
                raise PalimpsestError(f"recipe file not found: {recipe_path}")
            recipe = parse_palimpsestfile(recipe_path)
            base_digest, layer_digests = verify_build_integrity(recipe, cli_base=args.base, cli_layers=args.layer)
            base_ref = _resolve_image_ref(store, base_digest, args.url)
            parent_layers = _layer_refs_from_store(store, layer_digests, base_ref.digest)
            record = build_layer(
                BuildSpec(
                    base=base_ref,
                    parent_layers=parent_layers,
                    recipe=recipe_path,
                    network=args.network,
                    output_name=args.tag[0],
                ),
                roots=roots,
            )
            print(record["output_digest"])
        else:
            context = args.context or Path(".")
            dockerfile = args.recipe or (context / "Dockerfile")
            platform = args.platform if args.platform is not None else "linux/amd64"
            cache_scope = args.cache_scope if args.cache_scope is not None else "default"
            runtime_block_size = args.runtime_block_size if args.runtime_block_size is not None else 131072
            selected_registry = None
            selected_registry_digest = None
            native_build = False
            resolved_tags: tuple[ResolvedImageReference, ...] = ()
            build_tags = tuple(args.tag)
            external_cache_from = tuple(args.cache_from)
            external_cache_to = tuple(args.cache_to)
            native_namespace = native_package = None
            package_client = None
            cache_client = None
            cache_package = None
            # Strict offline builds never read registry profiles; publish later with push --input.
            if not args.offline:
                registry_config = load_registry_config(roots)
                resolved_tags = tuple(
                    resolve_image_reference(tag, registry_config, registry_alias=args.registry) for tag in args.tag
                )
                tag_profiles = {_reference_profile(registry_config, tag) for tag in resolved_tags}
                native_build = any(_is_native(profile) for profile in tag_profiles)
                if native_build and args.cache_mode == "local":
                    raise PalimpsestError("local cache mode requires an OCI output profile; select --registry docker")
                if native_build and len(tag_profiles) != 1:
                    raise PalimpsestError("one build cannot mix registry profiles or protocols")
                selected_registry = (
                    next(iter(tag_profiles))
                    if native_build
                    else select_registry_profile(registry_config, explicit_alias=args.registry)
                )
                selected_registry_digest = registry_config_digest(registry_config)
                if args.cache_mode == "native":
                    external_cache_from = _merge_unique(external_cache_from, selected_registry.cache_from)
                    external_cache_to = _merge_unique(external_cache_to, selected_registry.cache_to)
                if native_build or args.push or args.registry:
                    build_tags = tuple(tag.canonical for tag in resolved_tags)
                if native_build:
                    native_namespace, native_package = _native_target(resolved_tags[0])
                    if any(_native_target(tag) != (native_namespace, native_package) for tag in resolved_tags):
                        raise PalimpsestError("native build tags must target one exact namespace/package")
                    if any(tag.digest is not None or tag.tag is None for tag in resolved_tags):
                        raise PalimpsestError("native build requires tag references")
                    if args.load or external_cache_from or external_cache_to:
                        raise PalimpsestError("native builds reject Docker --load and external cache exporters")
                    package_client = NativePackageClient(selected_registry, namespace=native_namespace)
                    if args.push:
                        package_client.authorize(native_package, ("packages:inventory", "packages:write"))
                elif args.cache_mode == "native" and (args.cache_registry is None or args.cache_package is None):
                    raise PalimpsestError(
                        "online OCI build requires --cache-registry and --cache-package namespace/package"
                    )
                if args.cache_mode == "native":
                    cache_profile = (
                        inspect_profile(registry_config, args.cache_registry)
                        if args.cache_registry
                        else selected_registry
                    )
                    if cache_profile.protocol != "palimpsest":
                        raise PalimpsestError("mandatory Hub cache requires a native registry profile")
                    partition = args.cache_package or f"{native_namespace}/{native_package}"
                    cache_ref = resolve_image_reference(
                        f"{cache_profile.endpoint}/{partition}",
                        registry_config,
                        registry_alias=cache_profile.alias,
                        default_tag=False,
                    )
                    if cache_ref.tag is not None or cache_ref.digest is not None:
                        raise PalimpsestError("--cache-package must be an exact namespace/package, not a tag or digest")
                    cache_namespace, cache_package = _native_target(cache_ref)
                    if (
                        package_client is not None
                        and cache_profile == selected_registry
                        and cache_namespace == native_namespace
                    ):
                        cache_client = package_client
                    else:
                        cache_client = NativePackageClient(cache_profile, namespace=cache_namespace)
                    cache_client.authorize(cache_package, ("cache:read", "cache:write"))
            primary_tag = build_tags[0]
            safe_tag = "".join(char if char.isalnum() or char in ".-" else "-" for char in primary_tag).strip("-.")
            safe_tag = safe_tag[:48] or "image"
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            output = args.output or (roots.builds / "outputs" / f"{safe_tag}-{timestamp}.oci.tar")
            local_images = tuple(NamedOCIContext.parse(value) for value in args.local_image)
            init_resolved_roots(roots)
            runtime_client = (
                HubClient(resolve_url(args.url), resolve_token()) if not args.offline and args.runtime_tag else None
            )
            if args.runtime_base:
                if args.offline:
                    runtime_base_ref = _image_ref_from_store(store, args.runtime_base)
                else:
                    runtime_base_ref = _resolve_image_ref(store, args.runtime_base, args.url)
                target_arch = image_arch_for_platform(platform)
                if runtime_base_ref.arch != target_arch:
                    raise PalimpsestError(
                        f"--platform {platform} targets {target_arch}, but --runtime-base "
                        f"{runtime_base_ref.digest} is {runtime_base_ref.arch}"
                    )

            def retain_native_export(build_id: str, manifest: str | None) -> None:
                """Freeze package bytes and receipts before later cache/publication failures."""
                with snapshot_package(output, manifest=manifest) as snapshot:
                    archive = _retain_package_archive(roots, snapshot)
                    for resolved in resolved_tags:
                        record = _local_package_record(
                            selected_registry,
                            resolved,
                            snapshot,
                            archive,
                            project_id=cache_client.project_id
                            if cache_client is not None
                            and cache_client.namespace == native_namespace
                            and cache_client.profile == selected_registry
                            else None,
                            build_id=build_id,
                        )
                        write_package_reference(roots, record)

            def execute_build(
                rootfs_output: Path | None,
                runtime_rootfs_archive: Path | None,
            ) -> dict[str, object]:
                return build_with_buildkit(
                    BuildKitSpec(
                        context=context,
                        dockerfile=dockerfile,
                        tag=primary_tag,
                        additional_tags=build_tags[1:],
                        output=output,
                        rootfs_output=rootfs_output,
                        runtime_rootfs_archive=runtime_rootfs_archive,
                        platform=platform,
                        target=args.target,
                        build_args=tuple(args.build_arg),
                        local_images=local_images,
                        network=args.network,
                        offline=args.offline,
                        no_cache=args.no_cache,
                        push_cache=not args.offline,
                        cache_scope=cache_scope,
                        cache_mode=args.cache_mode,
                        external_cache_from=external_cache_from,
                        external_cache_to=external_cache_to,
                        registry_profile=selected_registry.alias if selected_registry is not None else None,
                        registry_config_digest=selected_registry_digest,
                        pull=args.pull,
                        load=args.load if not native_build else False,
                        push_image=args.push if not native_build else False,
                        progress=args.progress,
                        runtime_tag=args.runtime_tag,
                        runtime_base_digest=args.runtime_base,
                        runtime_block_size=runtime_block_size,
                        push=args.runtime_push,
                    ),
                    roots,
                    hub_client=cache_client,
                    cache_package=cache_package,
                    runtime_hub_client=runtime_client,
                    on_oci_export=retain_native_export if native_build else None,
                )

            if args.runtime_tag:
                with tempfile.TemporaryDirectory(prefix="buildkit-rootfs-", dir=roots.builds) as tmpdir:
                    build_record = execute_build(args.rootfs_output, Path(tmpdir) / "rootfs.tar")
            else:
                build_record = execute_build(args.rootfs_output, None)
            if native_build:
                retained = read_package_reference(roots, resolved_tags[0].canonical)
                with snapshot_package(Path(retained.archive), manifest=retained.root_digest) as snapshot:
                    if (
                        retained.build_id != build_record["build_id"]
                        or snapshot.archive_digest != retained.archive_digest
                        or snapshot.root_digest != retained.root_digest
                    ):
                        raise PalimpsestError("native export bytes changed after its receipt was recorded")
                    build_record["output_oci_manifest_digest"] = snapshot.root_digest
                    # All valid local references survive a failed publication.
                    if args.push:
                        for resolved in resolved_tags:
                            record = read_package_reference(roots, resolved.canonical)
                            receipt = package_client.push(
                                native_package,
                                resolved.tag,
                                snapshot,
                                provenance={"build_id": build_record["build_id"]},
                            )
                            write_package_reference(
                                roots,
                                replace(
                                    record, project_id=package_client.project_id, published_digest=receipt["digest"]
                                ),
                            )
            result_digest = (
                build_record.get("runtime_block_digest")
                or build_record.get("output_oci_manifest_digest")
                or build_record["output_oci_archive_digest"]
            )
            print(result_digest)

    elif op == "compose":
        return _dispatch_compose(args, roots, store)

    elif op == "run":
        from .web_job_client import cancellable_run_preparation

        if args.name is None:
            args.name = generate_run_name(args.image_or_bundle, roots=roots)
        else:
            validate_run_name(args.name)

        volume_intents = tuple(args.volume_intents)

        with cancellable_run_preparation():
            resolved_source = resolve_run_source(
                roots,
                args.image_or_bundle,
                runtime_kind=getattr(args, "runtime_kind", None),
                registry_alias=getattr(args, "registry", None),
                backend=args.backend,
                network=args.network,
                platform=args.platform,
                force_architecture=args.force_architecture,
            )

        if resolved_source.is_oci:
            source = Path(resolved_source.target).resolve()
            if args.layer:
                raise PalimpsestError("OCI-root run does not accept cloud --layer attachments")
            if args.network is not None and args.network not in OCI_NETWORK_MODES:
                raise PalimpsestError(
                    "OCI-root run networking accepts --network nat, --network host-only, or --network none"
                )
            manifest_to_use = getattr(args, "manifest", None) or resolved_source.manifest_digest
            request = resolve_local_oci_run_request(
                source,
                name=args.name,
                manifest_digest=require_digest(manifest_to_use) if manifest_to_use is not None else None,
                user_override=OCIUserSpec.from_override_value(args.user) if args.user is not None else None,
                command_override=getattr(args, "command_override", None),
                detached=args.detach,
                memory_mib=args.memory,
                vcpus=args.vcpus,
                root_retention=args.root_retention or "delete",
                root_volume_id=args.root_volume,
                network=args.network,
                published_ports=tuple(args.publish),
                volume_intents=volume_intents,
                platform=args.platform,
                backend=args.backend,
                force_architecture=args.force_architecture,
                vm_image=args.vm_image,
                vm_image_digest=args.vm_image_digest,
            )
            if request.force_architecture:
                print(
                    "warning: experimental lima-qemu-oci container-in-VM runtime; not native OCI-root; foreign QEMU execution may be slow",
                    file=sys.stderr,
                )
            result = runtime_dispatch.run_local_oci(request, roots=roots)
            if request.force_architecture:
                print(f"experimental VM attempt evidence: {result.state_path}", file=sys.stderr)
                return result.exit_code
            if args.detach:
                if result.terminal is not None:
                    raise PalimpsestError("OCI-root workload exited before detached launch completed")
                print(result.record.name)
                return 0
            if result.session is None:
                raise PalimpsestError("OCI-root foreground launch is missing its process session")
            return _run_process_session(result.session, interactive=False)

        if (
            getattr(args, "manifest", None) is not None
            or getattr(args, "root_retention", None) not in {None, "retain"}
            or getattr(args, "root_volume", None) is not None
            or getattr(args, "user", None) is not None
            or getattr(args, "command_override", None) is not None
            or getattr(args, "publish", None)
            or args.force_architecture
            or args.vm_image is not None
            or args.vm_image_digest is not None
            or args.platform != "linux/amd64"
        ):
            raise PalimpsestError(
                "--manifest, --root-retention=delete, --root-volume, --user, --publish, selected OCI --platform, "
                "--force-architecture, --vm-image, --vm-image-digest and command overrides are supported only for OCI runs"
            )
        network = args.network if args.network is not None else "default"
        with cancellable_run_preparation():
            stack = _resolve_runtime_stack(
                store,
                os.fspath(resolved_source.target),
                args.layer,
                args.url,
                requested_backend=args.backend,
                preflight_for_run=True,
                run_network=network,
            )
        run_spec = RunSpec(
            name=args.name,
            stack=stack,
            memory_mib=args.memory,
            vcpus=args.vcpus,
            network=network,
        )
        request = runtime_dispatch.resolve_run_request(
            run_spec,
            requested_backend=args.backend,
            volume_intents=volume_intents,
            require_volume_binding=bool(volume_intents),
        )
        preflight = runtime_dispatch.preflight_run_request(request)
        result = runtime_dispatch.run(request, preflight=preflight, roots=roots)
        _render_run_warnings(result)
        if not result.ready:
            raise PalimpsestError(f"runtime did not become ready (status: {result.status})")
        if result.attachment_mode is RunAttachmentMode.ATTACHED:
            if result.session is None:  # pragma: no cover - guarded by RunResult
                raise PalimpsestError("attached run result is missing its process session")
            return _run_process_session(result.session, interactive=False)
        print(result.launch_hint)

    elif op == "ps":
        payload = inventory.list_vms(roots)
        for warning in payload.get("warnings", []):
            print(f"warning: {warning}", file=sys.stderr)
        rows = []
        for details in payload["vms"]:
            if details.get("status") == "removed":
                continue
            base_digest = details.get("base_digest") or "-"
            base_hex = str(base_digest).split(":", 1)[-1][:12]
            layers = details.get("layers")
            layers_count = len(layers) if isinstance(layers, (list, tuple)) else 0
            ssh = details.get("ssh")
            ssh_host = ssh.get("host") if isinstance(ssh, Mapping) else None
            guest_ip = details.get("guest_ip") or ssh_host or "-"
            created_at = details.get("created_at") or "-"
            raw_ports = details.get("ports")
            published_ports = "-"
            if isinstance(raw_ports, (list, tuple)) and raw_ports:
                published_ports = (
                    ",".join(
                        f"{port.get('host_ip') or '-'}:{port.get('host_port', '-')}"
                        f"->{port.get('guest_port', '-')}/{port.get('protocol') or '-'}"
                        for port in raw_ports
                        if isinstance(port, Mapping)
                    )
                    or "-"
                )
            scope = "observed" if details.get("managed") is False else "managed"
            rows.append(
                (
                    str(details.get("name") or "-"),
                    str(details.get("status") or "-"),
                    str(details.get("backend") or "-"),
                    scope,
                    base_hex,
                    str(layers_count),
                    str(guest_ip),
                    str(created_at),
                    published_ports,
                )
            )
        _print_table(
            ("NAME", "STATUS", "BACKEND", "SCOPE", "BASE", "LAYERS", "IP", "CREATED", "PORTS"),
            rows,
        )

    elif op in {"volumes", "networks"}:
        payload = inventory.list_volumes(roots) if op == "volumes" else inventory.list_networks(roots)
        for warning in payload["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        if args.format == "json":
            print(json.dumps(payload, indent=2))
        else:
            if op == "volumes":
                headings = (
                    "NAME",
                    "BACKEND",
                    "STATUS",
                    "CAPACITY_BYTES",
                    "FILE_BYTES",
                    "ALLOCATED_BYTES",
                    "ATTACHMENTS",
                )
                values = ("size_bytes", "file_size_bytes", "allocated_bytes")
            else:
                headings = ("NAME", "BACKEND", "STATUS", "MODE", "SUBNET", "GATEWAY", "ATTACHMENTS")
                values = ("mode", "subnet", "gateway")
            rows = []
            for item in payload[op]:
                attached = ",".join(attachment["name"] for attachment in item["attachments"]) or "-"
                measurements = [str(item.get(field)) if item.get(field) is not None else "-" for field in values]
                rows.append(
                    (
                        item["name"],
                        item.get("backend") or "-",
                        item["status"],
                        *measurements,
                        attached,
                    )
                )
            _print_table(headings, rows)

    elif op == "inspect":
        from . import experimental_oci_vm

        if experimental_oci_vm.exists(roots, args.name):
            print(json.dumps(experimental_oci_vm.inspect(roots, args.name), indent=2))
            return 0
        info = runtime_dispatch.inspect_run(args.name, roots=roots)
        print(json.dumps(_inspect_json_payload(info), indent=2))

    elif op == "logs":
        from . import experimental_oci_vm

        if experimental_oci_vm.exists(roots, args.name):
            for data in experimental_oci_vm.logs(roots, args.name, follow=args.follow):
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            return 0
        stream = runtime_dispatch.logs(args.name, roots=roots, follow=args.follow)
        try:
            for event in stream.events():
                if isinstance(event, LogDataEvent):
                    sys.stdout.buffer.write(event.data)
                    sys.stdout.buffer.flush()
                elif event.outcome.category is LogTerminalCategory.ERROR:
                    assert event.outcome.error_category is not None
                    raise LogStreamError(event.outcome.error_category)
        finally:
            stream.close()

    elif op == "shell":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise PalimpsestError("shell requires a local terminal")
        return _run_process_session(runtime_dispatch.shell(args.name, roots=roots), interactive=True)

    elif op == "exec":
        timeout_ms = None if args.timeout is None else args.timeout * 1000
        if args.completion_record is not None:
            return _run_process_session(
                runtime_dispatch.exec(
                    args.name,
                    args.command,
                    roots=roots,
                    completion_record=args.completion_record,
                    timeout_ms=timeout_ms,
                ),
                interactive=False,
            )
        return _run_process_session(
            runtime_dispatch.exec(args.name, args.command, roots=roots, timeout_ms=timeout_ms),
            interactive=False,
        )
    elif op == "start":
        from . import experimental_oci_vm

        if experimental_oci_vm.exists(roots, args.name):
            raise PalimpsestError("experimental OCI VMs are disposable; submit a new run instead of start")
        runtime_dispatch.start(args.name, roots=roots)
        print(f"started {args.name}")

    elif op == "stop":
        from . import experimental_oci_vm

        if experimental_oci_vm.exists(roots, args.name):
            experimental_oci_vm.stop(roots, args.name)
            print(f"stopped {args.name}")
            return 0
        runtime_dispatch.stop(args.name, roots=roots)
        print(f"stopped {args.name}")

    elif op == "rm":
        from . import experimental_oci_vm

        if experimental_oci_vm.exists(roots, args.name):
            experimental_oci_vm.remove(roots, args.name)
            print(f"removed {args.name}; experimental evidence retained")
            return 0
        removal = runtime_dispatch.rm(args.name, roots=roots, volumes=args.volumes)
        print(f"removed {args.name}")
        if isinstance(removal, OCIRunRemovalResult) and removal.retention_policy == "retain":
            print(f"retained root\t{removal.root_volume_id}")

    elif op == "commit":
        result = runtime_dispatch.commit(args.name, args.tag, roots=roots)
        print(result.digest)
    elif op == "web":
        ui.serve(roots, port=args.port, open_browser=not args.no_browser, read_only=not args.allow_control)
        return 0
    elif op == "store":
        sub = args.store_operation
        if sub == "show":
            payload = inventory.storage_report(roots)
            if args.format == "json":
                print(json.dumps(payload, indent=2))
            else:
                print(f"{'COMPONENT':<20}\t{'VALUE':<32}")
                print(f"{'state_root':<20}\t{payload['state_root']}")
                print(f"{'source':<20}\t{payload['source']}")
                print(f"{'total_state_bytes':<20}\t{payload['total_state_bytes']}")
                print(f"{'free_bytes':<20}\t{payload['free_bytes']}")
                print(f"{'total_bytes':<20}\t{payload['total_bytes']}")
                for name, size in payload["directories"].items():
                    print(f"{f'dir:{name}':<20}\t{size}")
        elif sub == "ls":
            payload = inventory.list_artifacts(roots)
            if args.kind == "image":
                items = payload["images"]
            elif args.kind == "layer":
                items = payload["layers"]
            else:
                items = payload["artifacts"]
            if args.format == "json":
                print(json.dumps(items, indent=2))
            else:
                print(f"{'DIGEST':<20}\t{'KIND':<16}\t{'SIZE':<12}\t{'TAGS':<24}")
                for item in items:
                    raw_tags = item.get("tags")
                    tags_list = raw_tags if isinstance(raw_tags, list) else []
                    tags_str = (
                        ", ".join(t.get("tag", "") for t in tags_list if isinstance(t, dict) and t.get("tag")) or "-"
                    )
                    print(
                        f"{item['digest']:<20}\t{str(item.get('kind', '-')):<16}\t"
                        f"{str(item.get('size_bytes', 0)):<12}\t{tags_str:<24}"
                    )
        elif sub == "rm":
            result = inventory.remove_artifact(roots, args.digest, force=args.force)
            print(json.dumps(result, indent=2))
        elif sub == "move":
            result = inventory.move_state_root(roots, args.destination, keep_source=args.keep_source)
            print(json.dumps(result, indent=2))
        elif sub == "set":
            result = inventory.set_state_root(roots, args.destination)
            print(json.dumps(result, indent=2))
        return 0

    return 0


def _docker_help_requested(arguments: Sequence[str]) -> bool:
    """Recognize unambiguous help without skipping journals for arbitrary commands."""
    if not arguments:
        return True
    command_index = _docker_subcommand_index(["docker", "--config", "", *arguments])
    if command_index is not None and arguments[command_index - 3] == "help":
        return True
    # Bare options before any subcommand (e.g. docker --help, docker -h).
    if command_index is None and all(arg in {"-h", "--help"} for arg in arguments):
        return True
    # Single subcommand followed only by help flags (e.g. docker run --help).
    # Do NOT match commands with arguments after the flags or subcommands where
    # flags like -h have another meaning (e.g. docker run -h myhost alpine).
    if command_index is not None:
        sub_args = list(arguments[command_index - 3 :])
        if sub_args and sub_args[-1] == "--help" and all(not a.startswith("-") for a in sub_args[:-1]):
            return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        raw_args = list(sys.argv[1:] if argv is None else argv)
        if not raw_args:
            parser.print_help()
            return 0
        if raw_args[:1] == ["__complete"]:
            comp_args = raw_args[1:]
            shell = None
            if comp_args[:1] == ["--shell"]:
                if len(comp_args) < 3 or comp_args[1] not in completion.SUPPORTED_SHELLS or comp_args[2] != "--":
                    parser.error("__complete requires --shell {zsh,bash,fish} -- WORDS...")
                shell = comp_args[1]
                comp_args = comp_args[2:]
            if comp_args[:1] == ["--"]:
                comp_args = comp_args[1:]
            completion.print_completion(parser, comp_args, shell=shell)
            return 0
        command_index = 0
        while command_index < len(raw_args):
            argument = raw_args[command_index]
            if argument == "--url":
                command_index += 2
            elif argument.startswith("--url="):
                command_index += 1
            else:
                break
        command_args = raw_args[command_index:]
        command_override = None
        if command_args[:1] == ["run"] and "--" in command_args:
            separator = raw_args.index("--", command_index + 1)
            command_override = tuple(raw_args[separator + 1 :])
            if not command_override:
                raise PalimpsestError("OCI run command override after -- must be nonempty")
            raw_args = raw_args[:separator]
        if command_args[:1] == ["docker"]:
            args = parser.parse_args([*raw_args[:command_index], "docker"])
            args.docker_args = command_args[1:]
        else:
            args = parser.parse_args(raw_args)
        if args.operation == "run":
            args.command_override = command_override
        _validate_args(args, parser)
        if args.operation == "completion" or (args.operation == "docker" and _docker_help_requested(args.docker_args)):
            return dispatch_args(args)
        journal = host_journal.begin(args.operation)
        try:
            result = dispatch_args(args)
        except BaseException:
            journal.finish("error")
            raise
        journal.finish("success" if result == 0 else "error")
        return result
    except SystemExit as exc:
        return int(exc.code)
    except PalimpsestError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
