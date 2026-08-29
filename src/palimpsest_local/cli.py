"""The fixed v1 argparse surface for the ``palimpsest`` command."""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import select
import shutil
import signal as signal_module
import subprocess
import sys
import tempfile
import termios
import threading
import tomllib
import tty
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from . import __version__, completion, inventory, lima, runtime_dispatch, ui
from .build import build_layer, parse_palimpsestfile, verify_build_integrity
from .buildkit import BuildKitSpec, NamedOCIContext, build_with_buildkit, image_arch_for_platform
from .digest import digest_file, digest_hex, require_digest
from .errors import PalimpsestError
from .hub import DISK_FORMAT_MEDIA_TYPES, KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS, HubClient
from .inventory import import_cloud_image
from .oci_layout import ContentStore, extract_bundle_tar, verify_layout_dir
from .project import (
    DEFAULT_PROJECT_FILE,
    Project,
    ServiceSpec,
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
from .refs import BuildSpec, ImageRef, LayerRef, RunSpec, StackRef
from .registry import (
    RegistryConfig,
    RegistryProfile,
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
from .runtime import commit
from .runtime_types import (
    CloudImageInspectDetail,
    ExpectedRunIdentity,
    InspectRecord,
    LogDataEvent,
    LogStreamError,
    LogTerminalCategory,
    LogTerminalEvent,
    ProcessOutputEvent,
    ProcessSession,
    ProcessSignal,
    ProcessStatusEvent,
    ProcessStream,
)
from .state import (
    StatePaths,
    TagRecord,
    fsync_directory,
    init_roots,
    read_tag_record,
    resolve_roots,
    run_paths,
    write_tag_record,
)

_DOCKER_IMAGE_ID_RE = re.compile(r"(?:sha256:[0-9a-f]{64}|[0-9a-f]{12,64})")


def _inspect_json_payload(inspected: InspectRecord) -> dict[str, object]:
    """Manually serialize the stable public inspect schema without reflective fields."""

    detail = inspected.detail
    if not isinstance(detail, CloudImageInspectDetail):  # pragma: no cover - closed typed contract
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
        "detail": {
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
            "ports": [
                {
                    "host_ip": port.host_ip,
                    "host_port": port.host_port,
                    "guest_port": port.guest_port,
                    "protocol": port.protocol,
                }
                for port in detail.ports
            ],
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
        },
        "warnings": [warning.value for warning in inspected.warnings],
    }


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
    value = explicit_url or os.environ.get("PALIMPSEST_URL") or _configured_url()
    if not value:
        raise PalimpsestError("Palimpsest Hub URL is required (--url, PALIMPSEST_URL, or config.toml)")
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
    blob_path = store.blob_path(normalized)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    client.pull_blob(normalized, blob_path)
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
        refs.append(LayerRef(digest=normalized, media_type=MEDIA_TYPE_LAYER_SQUASHFS, local_path=blob))
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
    parser.add_argument("--limit", type=int, default=50)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="palimpsest")
    parser.add_argument("--url")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="operation", required=True)

    image = commands.add_parser("image")
    image_commands = image.add_subparsers(dest="image_operation", required=True)
    image_ls = image_commands.add_parser("ls")
    image_ls.add_argument("--ubuntu-base")
    image_ls.add_argument("--arch")
    image_ls.add_argument("--os-variant")
    image_ls.add_argument("--disk-format", choices=("qcow2", "raw"))
    _add_limit(image_ls)
    image_pull = image_commands.add_parser("pull")
    image_pull.add_argument("digest")
    image_pull.add_argument("--output", type=Path)
    image_verify = image_commands.add_parser("verify")
    image_verify.add_argument("path", type=Path)
    image_verify.add_argument("--digest", required=True)
    image_import = image_commands.add_parser("import")
    image_import.add_argument("path", type=Path)
    image_import.add_argument("--disk-format", choices=("qcow2", "raw"), required=True)
    image_import.add_argument("--arch", choices=("x86_64", "aarch64"), required=True)
    image_import.add_argument("--os-variant")
    image_push = image_commands.add_parser("push")
    image_push.add_argument("path", type=Path)
    image_push.add_argument("--name", required=True)
    image_push.add_argument("--disk-format", choices=("qcow2", "raw"), default="qcow2")
    image_push.add_argument("--arch", choices=("x86_64", "aarch64"), default="x86_64")
    image_push.add_argument("--os-variant")
    image_push.add_argument("--ubuntu-base")
    image_push.add_argument("--publish", action="store_true")
    image_inspect = image_commands.add_parser("inspect")
    image_inspect.add_argument("references", nargs="+")
    image_inspect.add_argument("-f", "--format")
    image_inspect.add_argument("--platform")
    image_inspect.add_argument("--registry")
    image_history = image_commands.add_parser("history")
    image_history.add_argument("reference")
    image_history.add_argument("--format")
    image_history.add_argument("--no-trunc", action="store_true")
    image_history.add_argument("--platform")
    image_history.add_argument("-q", "--quiet", action="store_true")
    image_rm = image_commands.add_parser("rm")
    image_rm.add_argument("references", nargs="+")
    image_rm.add_argument("-f", "--force", action="store_true")
    image_rm.add_argument("--no-prune", action="store_true")
    image_rm.add_argument("--platform", action="append", default=[])
    image_save = image_commands.add_parser("save")
    image_save.add_argument("references", nargs="+")
    image_save.add_argument("-o", "--output", type=Path)
    image_save.add_argument("--platform", action="append", default=[])
    image_load = image_commands.add_parser("load")
    image_load.add_argument("-i", "--input", dest="input_path", type=Path)
    image_load.add_argument("--platform", action="append", default=[])
    image_load.add_argument("-q", "--quiet", action="store_true")

    layer = commands.add_parser("layer")
    layer_commands = layer.add_subparsers(dest="layer_operation", required=True)
    layer_ls = layer_commands.add_parser("ls")
    layer_ls.add_argument("--name")
    layer_ls.add_argument("--kind")
    layer_ls.add_argument("--parent")
    _add_limit(layer_ls)
    layer_pull = layer_commands.add_parser("pull")
    layer_pull.add_argument("digest")
    layer_pull.add_argument("--output", type=Path)
    layer_pack = layer_commands.add_parser("pack")
    layer_pack.add_argument("directory", type=Path)
    layer_pack.add_argument("--tag", required=True)
    layer_push = layer_commands.add_parser("push")
    layer_push.add_argument("value")
    layer_push.add_argument("--name")
    layer_push.add_argument("--parent")
    layer_push.add_argument("--base-image")
    layer_push.add_argument("--ubuntu-base")
    layer_push.add_argument("--publish", action="store_true")

    bundle = commands.add_parser("bundle")
    bundle_commands = bundle.add_subparsers(dest="bundle_operation", required=True)
    bundle_pull = bundle_commands.add_parser("pull")
    bundle_pull.add_argument("leaf_digest")
    bundle_pull.add_argument("--output", required=True, type=Path)
    bundle_pull.add_argument("--include-base", action="store_true")
    bundle_verify = bundle_commands.add_parser("verify")
    bundle_verify.add_argument("directory", type=Path)

    build = commands.add_parser("build")
    build.add_argument("context", nargs="?", type=Path)
    build.add_argument("--frontend", choices=("auto", "palimpsestfile", "dockerfile"), default="auto")
    build.add_argument("--base")
    build.add_argument("--tag", "-t", action="append", required=True)
    build.add_argument("-f", "--file", dest="recipe", type=Path)
    build.add_argument("--layer", action="append")
    build.add_argument("--network", choices=("none", "default"), default="none")
    build.add_argument("--offline", action="store_true")
    build.add_argument("--platform")
    build.add_argument("--target")
    build.add_argument("--build-arg", action="append", default=[])
    build.add_argument("--local-image", action="append", default=[])
    build.add_argument("--cache-scope")
    build.add_argument("--registry")
    build.add_argument("--cache-from", action="append", default=[])
    build.add_argument("--cache-to", action="append", default=[])
    build.add_argument("--no-cache", action="store_true")
    build.add_argument("--pull", action="store_true")
    build.add_argument("--load", action="store_true")
    build.add_argument("--progress", choices=("auto", "none", "plain", "quiet", "rawjson", "tty"), default="plain")
    build.add_argument("--output", type=Path)
    build.add_argument("--rootfs-output", type=Path)
    build.add_argument("--runtime-tag")
    build.add_argument("--runtime-base")
    build.add_argument("--runtime-block-size", type=int)
    build.add_argument("--push", action="store_true", help="push the OCI image to its configured registry")
    build.add_argument("--runtime-push", action="store_true", help="push the SquashFS runtime block to Palimpsest Hub")

    registry = commands.add_parser("registry")
    registry_commands = registry.add_subparsers(dest="registry_operation", required=True)
    registry_ls = registry_commands.add_parser("ls")
    registry_ls.add_argument("--format", choices=("table", "json"), default="table")
    registry_add = registry_commands.add_parser("add")
    registry_add.add_argument("name")
    registry_add.add_argument("endpoint")
    registry_add.add_argument("--namespace")
    registry_add.add_argument("--mirror", action="append", default=[])
    registry_add.add_argument("--ca", action="append", type=Path, default=[])
    registry_add.add_argument("--plain-http", action="store_true")
    registry_add.add_argument("--tls-skip-verify", action="store_true")
    registry_add.add_argument("--cache-from", action="append", default=[])
    registry_add.add_argument("--cache-to", action="append", default=[])
    registry_add.add_argument("--default", action="store_true")
    registry_add.add_argument("--force", action="store_true")
    registry_use = registry_commands.add_parser("use")
    registry_use.add_argument("name")
    registry_rm = registry_commands.add_parser("rm")
    registry_rm.add_argument("name")
    registry_inspect = registry_commands.add_parser("inspect")
    registry_inspect.add_argument("name", nargs="?")
    registry_buildkit = registry_commands.add_parser("buildkit-config")
    registry_buildkit.add_argument("--output", required=True, type=Path)
    registry_buildkit.add_argument("--force", action="store_true")

    login = commands.add_parser("login")
    login.add_argument("server", nargs="?")
    login.add_argument("-u", "--username")
    login.add_argument("--password-stdin", action="store_true")
    login.add_argument("--registry")
    logout = commands.add_parser("logout")
    logout.add_argument("server", nargs="?")
    logout.add_argument("--registry")

    pull = commands.add_parser("pull")
    pull.add_argument("reference")
    pull.add_argument("-a", "--all-tags", action="store_true")
    pull.add_argument("--platform")
    pull.add_argument("-q", "--quiet", action="store_true")
    pull.add_argument("--registry")

    push = commands.add_parser("push")
    push.add_argument("reference")
    push.add_argument("-a", "--all-tags", action="store_true")
    push.add_argument("--platform")
    push.add_argument("-q", "--quiet", action="store_true")
    push.add_argument("--registry")

    tag = commands.add_parser("tag")
    tag.add_argument("source")
    tag.add_argument("target")
    tag.add_argument("--registry")

    images = commands.add_parser("images")
    images.add_argument("repository", nargs="?")
    images.add_argument("-a", "--all", action="store_true")
    images.add_argument("--digests", action="store_true")
    images.add_argument("-f", "--filter", action="append", default=[])
    images.add_argument("--format")
    images.add_argument("--no-trunc", action="store_true")
    images.add_argument("--tree", action="store_true")
    images.add_argument("-q", "--quiet", action="store_true")

    history = commands.add_parser("history")
    history.add_argument("reference")
    history.add_argument("--format")
    history.add_argument("--no-trunc", action="store_true")
    history.add_argument("--platform")
    history.add_argument("-q", "--quiet", action="store_true")
    rmi = commands.add_parser("rmi")
    rmi.add_argument("references", nargs="+")
    rmi.add_argument("-f", "--force", action="store_true")
    rmi.add_argument("--no-prune", action="store_true")
    rmi.add_argument("--platform", action="append", default=[])
    save = commands.add_parser("save")
    save.add_argument("references", nargs="+")
    save.add_argument("-o", "--output", type=Path)
    save.add_argument("--platform", action="append", default=[])
    load = commands.add_parser("load")
    load.add_argument("-i", "--input", dest="input_path", type=Path)
    load.add_argument("--platform", action="append", default=[])
    load.add_argument("-q", "--quiet", action="store_true")

    docker = commands.add_parser("docker", add_help=False)
    docker.add_argument("docker_args", nargs=argparse.REMAINDER)

    run = commands.add_parser("run")
    run.add_argument("image_or_bundle")
    run.add_argument("--name", required=True)
    run.add_argument("--layer", action="append", default=[])
    run.add_argument("--memory", type=int, default=4096)
    run.add_argument("--vcpus", type=int, default=2)
    run.add_argument("--network", default="default")
    run.add_argument("--backend", choices=("auto", "kvm", "lima-vz", "libvirt-hvf"), default="auto")

    compose = commands.add_parser("compose")
    compose.add_argument("-f", "--file", dest="project_file", type=Path)
    compose.add_argument("-p", "--project-name")
    compose.add_argument("--project-directory", type=Path)
    compose.add_argument("--env-file", action="append", type=Path, default=[])
    compose_commands = compose.add_subparsers(dest="compose_operation", required=True)
    compose_config = compose_commands.add_parser("config")
    compose_config.add_argument("--quiet", action="store_true")
    compose_config.add_argument("--format", choices=("json",), default="json")
    compose_config.add_argument("--services", action="store_true")
    compose_up = compose_commands.add_parser("up")
    compose_up.add_argument("services", nargs="*")
    compose_up.add_argument("-d", "--detach", action="store_true")
    compose_up.add_argument("--no-recreate", action="store_true")
    compose_up.add_argument("--force-recreate", action="store_true")
    compose_down = compose_commands.add_parser("down")
    compose_down.add_argument("--volumes", "-v", action="store_true")
    compose_ps = compose_commands.add_parser("ps")
    compose_ps.add_argument("services", nargs="*")
    compose_ps.add_argument("--format", choices=("table", "json"), default="table")
    compose_logs = compose_commands.add_parser("logs")
    compose_logs.add_argument("services", nargs="*")
    compose_logs.add_argument("--follow", "-f", action="store_true")
    compose_exec = compose_commands.add_parser("exec")
    compose_exec.add_argument("service")
    compose_exec.add_argument("command", nargs=argparse.REMAINDER)
    compose_stop = compose_commands.add_parser("stop")
    compose_stop.add_argument("services", nargs="*")
    compose_port = compose_commands.add_parser("port")
    compose_port.add_argument("service")
    compose_port.add_argument("private_port", type=int)
    compose_port.add_argument("--protocol", choices=("tcp", "udp"), default="tcp")

    commands.add_parser("ps")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("name")
    logs = commands.add_parser("logs")
    logs.add_argument("name")
    logs.add_argument("--follow", action="store_true")
    shell = commands.add_parser("shell")
    shell.add_argument("name")
    execute = commands.add_parser("exec")
    execute.add_argument("name")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    start = commands.add_parser("start")
    start.add_argument("name")
    stop = commands.add_parser("stop")
    stop.add_argument("name")
    remove = commands.add_parser("rm")
    remove.add_argument("name")
    remove.add_argument("--volumes", action="store_true")
    commit = commands.add_parser("commit")
    commit.add_argument("name")
    commit.add_argument("--tag", required=True)
    ui = commands.add_parser("ui")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--no-browser", action="store_true")

    store = commands.add_parser("store")
    store_commands = store.add_subparsers(dest="store_operation", required=True)
    store_show = store_commands.add_parser("show")
    store_show.add_argument("--format", choices=("table", "json"), default="table")
    store_ls = store_commands.add_parser("ls")
    store_ls.add_argument("--kind", choices=("image", "layer", "all"), default="all")
    store_ls.add_argument("--format", choices=("table", "json"), default="table")
    store_rm = store_commands.add_parser("rm")
    store_rm.add_argument("digest")
    store_rm.add_argument("--force", action="store_true")
    store_move = store_commands.add_parser("move")
    store_move.add_argument("--to", dest="destination", type=Path, required=True)
    store_move.add_argument("--keep-source", action="store_true")
    store_set = store_commands.add_parser("set")
    store_set.add_argument("--to", dest="destination", type=Path, required=True)
    comp_cmd = commands.add_parser("completion")
    comp_cmd.add_argument("shell", choices=completion.SUPPORTED_SHELLS)
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if hasattr(args, "limit") and not 1 <= args.limit <= 200:
        parser.error("--limit must be between 1 and 200")
    if args.operation == "run":
        if not 256 <= args.memory <= 1_048_576:
            parser.error("--memory must be between 256 and 1048576")
        if not 1 <= args.vcpus <= 256:
            parser.error("--vcpus must be between 1 and 256")
        if args.backend == "libvirt-hvf" and args.network not in ("none", "default"):
            parser.error("--backend libvirt-hvf supports --network none or default")
    if args.operation == "ui":
        if args.port != 0 and not 1024 <= args.port <= 65535:
            parser.error("--port must be 0 or between 1024 and 65535")
    if args.operation == "exec":
        if args.command[:1] == ["--"]:
            args.command = args.command[1:]
        if not args.command:
            parser.error("exec requires a command after --")
    if args.operation == "compose":
        if args.compose_operation == "exec":
            if args.command[:1] == ["--"]:
                args.command = args.command[1:]
            if not args.command:
                parser.error("compose exec requires a command after SERVICE")
        if args.compose_operation == "up" and args.no_recreate and args.force_recreate:
            parser.error("--no-recreate and --force-recreate are mutually exclusive")
    if args.operation == "docker" and not args.docker_args:
        parser.error("docker requires a Docker CLI command")
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
                    ("--registry", args.registry is not None),
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
            if args.no_cache and not args.offline:
                parser.error("--no-cache is allowed only in offline mode; online builds must reuse Hub cache")
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
    return resolve_image_reference(reference, config, registry_alias=registry_alias).canonical


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
        if lima.available():
            raise PalimpsestError(
                "OCI run bundles currently have no trusted boot-architecture field and are x86_64/KVM-only; "
                "on Apple Silicon, use a cloud-image digest whose Hub metadata declares aarch64"
            )
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
        base_ref = ImageRef(
            digest=base_entry.digest,
            disk_format=disk_format,
            arch="x86_64",
            os_variant=None,
            local_path=base_entry.local_path,
        )
        bundle_layers = tuple(
            LayerRef(digest=entry.digest, media_type=entry.media_type, local_path=entry.local_path)
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
    project = load_project(project_file, environment=environment, project_root=project_root)
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
            print(f"{'SERVICE':<20} {'NAME':<40} {'STATUS':<12}")
            for row in rows:
                print(f"{row.service:<20} {row.run_name:<40} {row.status:<12}")
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
        print(completion.generate_completion_script(args.shell))
        return 0
    read_only_root_operations = {"run", "start", "stop", "rm", "inspect", "logs", "ps", "exec", "shell"}
    roots = resolve_roots() if op in read_only_root_operations else init_roots()
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
    elif op == "docker":
        argv = docker_command_argv(resolve_docker_config_dir(), *args.docker_args)
        return run_docker_passthrough(argv).returncode
    elif op in {"login", "logout", "pull", "push", "tag", "images", "history", "rmi", "save", "load"}:
        docker_config = resolve_docker_config_dir()
        config = load_registry_config(roots) if op in {"login", "logout", "pull", "push", "tag"} else None
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
            blob_path = store.blob_path(norm_digest)
            if not store.exists(norm_digest):
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                client.pull_blob(norm_digest, blob_path)
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
            blob_path = store.blob_path(norm_digest)
            if not store.exists(norm_digest):
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                client.pull_blob(norm_digest, blob_path)
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
            build_tags = tuple(args.tag)
            selected_registry: RegistryProfile | None = None
            selected_registry_digest: str | None = None
            external_cache_from = tuple(args.cache_from)
            external_cache_to = tuple(args.cache_to)
            if not args.offline:
                registry_config = load_registry_config(roots)
                selected_registry = select_registry_profile(registry_config, explicit_alias=args.registry)
                selected_registry_digest = registry_config_digest(registry_config)
                external_cache_from = _merge_unique(external_cache_from, selected_registry.cache_from)
                external_cache_to = _merge_unique(external_cache_to, selected_registry.cache_to)
                if args.push or args.registry:
                    build_tags = tuple(
                        resolve_image_reference(tag, registry_config, registry_alias=args.registry).canonical
                        for tag in build_tags
                    )
            primary_tag = build_tags[0]
            safe_tag = "".join(char if char.isalnum() or char in ".-" else "-" for char in primary_tag).strip("-.")
            safe_tag = safe_tag[:48] or "image"
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            output = args.output or (roots.builds / "outputs" / f"{safe_tag}-{timestamp}.oci.tar")
            local_images = tuple(NamedOCIContext.parse(value) for value in args.local_image)
            client = None if args.offline else HubClient(resolve_url(args.url), resolve_token())
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
                        external_cache_from=external_cache_from,
                        external_cache_to=external_cache_to,
                        registry_profile=selected_registry.alias if selected_registry is not None else None,
                        registry_config_digest=selected_registry_digest,
                        pull=args.pull,
                        load=args.load,
                        push_image=args.push,
                        progress=args.progress,
                        runtime_tag=args.runtime_tag,
                        runtime_base_digest=args.runtime_base,
                        runtime_block_size=runtime_block_size,
                        push=args.runtime_push,
                    ),
                    roots,
                    hub_client=client,
                )

            if args.runtime_tag:
                with tempfile.TemporaryDirectory(prefix="buildkit-rootfs-", dir=roots.builds) as tmpdir:
                    build_record = execute_build(args.rootfs_output, Path(tmpdir) / "rootfs.tar")
            else:
                build_record = execute_build(args.rootfs_output, None)
            result_digest = (
                build_record.get("runtime_block_digest")
                or build_record.get("output_oci_manifest_digest")
                or build_record["output_oci_archive_digest"]
            )
            print(result_digest)

    elif op == "compose":
        return _dispatch_compose(args, roots, store)

    elif op == "run":
        stack = _resolve_runtime_stack(
            store,
            args.image_or_bundle,
            args.layer,
            args.url,
            requested_backend=args.backend,
            preflight_for_run=True,
            run_network=args.network,
        )
        run_spec = RunSpec(
            name=args.name,
            stack=stack,
            memory_mib=args.memory,
            vcpus=args.vcpus,
            network=args.network,
        )
        request = runtime_dispatch.resolve_run_request(run_spec, requested_backend=args.backend)
        preflight = runtime_dispatch.preflight_run_request(request)
        if request.dispatch_key.backend.value == "libvirt-hvf":
            print("warning: libvirt-hvf is experimental", file=sys.stderr)
        result = runtime_dispatch.run(request, preflight=preflight, roots=roots)
        if result.backend.value == "lima-vz":
            print(f"limactl shell {args.name}")
        else:
            # Preserve the legacy dict result's ``get("guest_ip", name)``
            # behavior: cloud/HVF states contain the key with a null value.
            print(result.guest_ip)

    elif op == "ps":
        aggregation = runtime_dispatch.ps(roots=roots)
        for error in aggregation.errors:
            identity = error.name or error.entry_token or "unknown-entry"
            print(f"warning: {identity}: {error.message}", file=sys.stderr)
        print(f"{'NAME':<20} {'STATUS':<12} {'BASE':<12} {'LAYERS':<8} {'IP':<16} {'CREATED':<24}")
        for summary in aggregation.summaries:
            if summary.status == "removed":
                continue
            details = summary.details
            base_digest = details.get("base_digest") or "-"
            base_hex = str(base_digest).split(":", 1)[-1][:12]
            layers = details.get("layers")
            layers_count = len(layers) if isinstance(layers, tuple) else 0
            ssh = details.get("ssh")
            ssh_host = ssh.get("host") if isinstance(ssh, Mapping) else None
            guest_ip = details.get("guest_ip") or ssh_host or "-"
            created_at = details.get("created_at") or "-"
            print(
                f"{summary.name:<20} {summary.status:<12} {base_hex:<12} "
                f"{layers_count:<8} {guest_ip:<16} {created_at:<24}"
            )

    elif op == "inspect":
        info = runtime_dispatch.inspect_run(args.name, roots=roots)
        print(json.dumps(_inspect_json_payload(info), indent=2))

    elif op == "logs":
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
        return _run_process_session(
            runtime_dispatch.exec(args.name, args.command, roots=roots),
            interactive=False,
        )
    elif op == "start":
        runtime_dispatch.start(args.name, roots=roots)
        print(f"started {args.name}")

    elif op == "stop":
        runtime_dispatch.stop(args.name, roots=roots)
        print(f"stopped {args.name}")

    elif op == "rm":
        runtime_dispatch.rm(args.name, roots=roots, volumes=args.volumes)
        print(f"removed {args.name}")

    elif op == "commit":
        if lima.is_lima_run(run_paths(roots, args.name)):
            raise PalimpsestError("native macOS Lima runs do not support commit; use palimpsest build")
        result = commit(args.name, args.tag, roots=roots)
        print(result["digest"])
    elif op == "ui":
        ui.serve(roots, port=args.port, open_browser=not args.no_browser)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        raw_args = list(sys.argv[1:] if argv is None else argv)
        if raw_args[:1] == ["__complete"]:
            comp_args = raw_args[1:]
            if comp_args[:1] == ["--"]:
                comp_args = comp_args[1:]
            candidates = completion.resolve_candidates(parser, comp_args)
            for c in candidates:
                print(c)
            return 0
        if raw_args[:1] == ["docker"] and len(raw_args) > 1:
            args = argparse.Namespace(operation="docker", docker_args=raw_args[1:])
        else:
            args = parser.parse_args(raw_args)
        _validate_args(args, parser)
        return dispatch_args(args)
    except SystemExit as exc:
        return int(exc.code)
    except PalimpsestError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
