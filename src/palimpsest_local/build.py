"""Strict Palimpsestfile parser, integrity guards, and build metadata helpers."""

from __future__ import annotations

import datetime
import hashlib
import posixpath
import re
import shlex
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import cloudinit, kvm, platforms, state
from .digest import InvalidDigestError, digest_file, require_digest, require_file_digest
from .errors import ArtifactValidationError, BuildError, StateError
from .oci_layout import MEDIA_TYPE_LAYER_SQUASHFS, ContentStore
from .refs import BuildSpec, RunSpec, StackRef
from .state import TagRecord, read_tag_record, validate_tag, write_tag_record

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UNSUPPORTED_INSTRUCTIONS = {
    "COPY",
    "ADD",
    "ARG",
    "USER",
    "SHELL",
    "EXPOSE",
    "VOLUME",
    "ENTRYPOINT",
    "CMD",
    "LABEL",
    "MAINTAINER",
    "STOPSIGNAL",
    "HEALTHCHECK",
    "ONBUILD",
}

MAX_PALIMPSESTFILE_BYTES = 1024 * 1024  # 1 MiB

PALIMPSEST_CLEAN_V1 = "palimpsest-clean-v1"
CLEAN_TARGETS_V1: tuple[str, ...] = (
    "tmp",
    "run",
    "var/tmp",
    "var/cache/apt",
    "var/lib/apt/lists",
    "var/log",
    "root",
    "etc/machine-id",
    "etc/resolv.conf",
    "etc/hosts",
)


@dataclass(frozen=True)
class RunInstruction:
    """A parsed RUN instruction in a Palimpsestfile."""

    line: int
    command: str
    env: dict[str, str]
    workdir: str


@dataclass(frozen=True)
class Palimpsestfile:
    """Parsed, validated single-output Palimpsestfile representation."""

    base_digest: str
    layers: tuple[str, ...]
    runs: tuple[RunInstruction, ...]
    final_env: dict[str, str]
    final_workdir: str
    recipe_sha256: str


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Convert raw Palimpsestfile text into logical lines, handling backslash continuation."""
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) > MAX_PALIMPSESTFILE_BYTES:
        raise BuildError(f"Palimpsestfile exceeds 1 MiB limit ({len(raw_bytes)} bytes)")

    result: list[tuple[int, str]] = []
    pending = ""
    start_line = 0

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        stripped = line.strip()

        if not pending and (not stripped or stripped.startswith("#")):
            continue

        if "<<" in stripped:
            raise BuildError(f"line {lineno}: heredocs ('<<') are not supported")

        if not pending:
            start_line = lineno

        continued = stripped.endswith("\\")
        chunk = stripped[:-1].rstrip() if continued else stripped
        pending = f"{pending} {chunk}".strip() if pending else chunk

        if not continued:
            result.append((start_line, pending))
            pending = ""

    if pending:
        raise BuildError(f"line {start_line}: unclosed line continuation at end of file")

    return result


def _parse_env(args: str, line: int) -> dict[str, str]:
    """Parse ENV instruction arguments into key-value pairs."""
    if not args.strip():
        raise BuildError(f"line {line}: ENV arguments cannot be empty")

    env: dict[str, str] = {}
    tokens = args.split()

    if "=" in tokens[0]:
        try:
            parts = shlex.split(args)
        except ValueError as exc:
            raise BuildError(f"line {line}: unable to parse ENV syntax: {args!r}") from exc
        for part in parts:
            if "=" not in part:
                raise BuildError(f"line {line}: ENV key=value format required, got {part!r}")
            key, value = part.split("=", 1)
            if not _ENV_KEY_RE.fullmatch(key):
                raise BuildError(f"line {line}: invalid ENV key: {key!r}")
            if any(ch in value for ch in "\r\n\x00"):
                raise BuildError(f"line {line}: control characters not allowed in ENV value")
            env[key] = value
        return env

    try:
        parts = shlex.split(args)
    except ValueError as exc:
        raise BuildError(f"line {line}: unable to parse ENV syntax: {args!r}") from exc
    if len(parts) < 2:
        raise BuildError(f"line {line}: ENV KEY VALUE format required")

    key = parts[0]
    value = " ".join(parts[1:])
    if not _ENV_KEY_RE.fullmatch(key):
        raise BuildError(f"line {line}: invalid ENV key: {key!r}")
    if any(ch in value for ch in "\r\n\x00"):
        raise BuildError(f"line {line}: control characters not allowed in ENV value")
    return {key: value}


def _parse_workdir(args: str, line: int) -> str:
    """Parse and validate WORKDIR instruction path."""
    if not args.strip():
        raise BuildError(f"line {line}: WORKDIR argument cannot be empty")
    if any(ch in args for ch in "\r\n\x00"):
        raise BuildError(f"line {line}: control characters not allowed in WORKDIR")
    if not args.startswith("/"):
        raise BuildError(f"line {line}: WORKDIR must be an absolute path starting with '/', got {args!r}")

    parts = [p for p in args.split("/") if p]
    if ".." in parts or "." in parts:
        raise BuildError(f"line {line}: WORKDIR path cannot contain '.' or '..' traversal segments: {args!r}")

    normalized = posixpath.normpath(args)
    if not normalized.startswith("/"):
        raise BuildError(f"line {line}: WORKDIR must be an absolute path: {args!r}")
    return normalized


def parse_palimpsestfile_text(text: str) -> Palimpsestfile:
    """Parse raw Palimpsestfile text into a structured Palimpsestfile object."""
    logical_lines = _logical_lines(text)
    if not logical_lines:
        raise BuildError("Palimpsestfile is empty")

    seen_from = False
    seen_exec_or_config = False
    base_digest: str | None = None
    layers: list[str] = []
    runs: list[RunInstruction] = []
    env: dict[str, str] = {}
    workdir: str = "/"

    for line_num, logical in logical_lines:
        match = re.match(r"^([A-Za-z]+)(?:\s+(.*))?$", logical)
        if not match:
            raise BuildError(f"line {line_num}: invalid instruction format: {logical!r}")

        instruction = match.group(1).upper()
        args = (match.group(2) or "").strip()

        if not seen_from and instruction != "FROM":
            raise BuildError(f"line {line_num}: first instruction must be FROM, got {instruction}")

        if instruction == "FROM":
            if seen_from:
                raise BuildError(f"line {line_num}: multi-stage builds are not supported; second FROM found")
            if args.startswith("--") or " AS " in f" {args.upper()} ":
                raise BuildError(f"line {line_num}: FROM flags or 'AS' aliases are not supported")
            if args.lower() == "scratch" or args.lower().startswith("ubuntu"):
                raise BuildError(
                    f"line {line_num}: FROM scratch or bare ubuntu image tags are not supported; "
                    "must specify sha256:<64hex> image digest"
                )
            try:
                base_digest = require_digest(args)
            except InvalidDigestError as exc:
                raise BuildError(f"line {line_num}: FROM digest must be sha256:<64hex>, got {args!r}") from exc
            seen_from = True

        elif instruction == "LAYER":
            if seen_exec_or_config:
                raise BuildError(f"line {line_num}: LAYER instruction cannot appear after ENV, WORKDIR, or RUN")
            if args.startswith("--"):
                raise BuildError(f"line {line_num}: LAYER options or flags are not supported")
            try:
                norm_layer = require_digest(args)
            except InvalidDigestError as exc:
                raise BuildError(f"line {line_num}: LAYER digest must be sha256:<64hex>, got {args!r}") from exc
            layers.append(norm_layer)
            if len(layers) > 25:
                raise BuildError(f"line {line_num}: Palimpsestfile supports at most 25 LAYER instructions")

        elif instruction == "ENV":
            seen_exec_or_config = True
            env_updates = _parse_env(args, line_num)
            env.update(env_updates)

        elif instruction == "WORKDIR":
            seen_exec_or_config = True
            workdir = _parse_workdir(args, line_num)

        elif instruction == "RUN":
            seen_exec_or_config = True
            if not args:
                raise BuildError(f"line {line_num}: RUN command cannot be empty")
            if args.startswith("--") or "--mount" in args or "--network" in args or "--security" in args:
                raise BuildError(f"line {line_num}: unsupported RUN flag or option in {args!r}")
            runs.append(RunInstruction(line=line_num, command=args, env=dict(env), workdir=workdir))

        elif instruction in _UNSUPPORTED_INSTRUCTIONS:
            raise BuildError(f"line {line_num}: instruction {instruction} is not supported in Palimpsestfile v1")
        else:
            raise BuildError(f"line {line_num}: unknown instruction: {instruction}")

    if not seen_from or base_digest is None:
        raise BuildError("Palimpsestfile is missing FROM instruction")
    if not runs:
        raise BuildError("Palimpsestfile must contain at least one RUN instruction")

    normalized_text = "\n".join(logical_text for _, logical_text in logical_lines)
    recipe_sha256 = f"sha256:{hashlib.sha256(normalized_text.encode('utf-8')).hexdigest()}"

    return Palimpsestfile(
        base_digest=base_digest,
        layers=tuple(layers),
        runs=tuple(runs),
        final_env=dict(env),
        final_workdir=workdir,
        recipe_sha256=recipe_sha256,
    )


def parse_palimpsestfile(content_or_path: str | Path) -> Palimpsestfile:
    """Parse a Palimpsestfile from text content or a file path."""
    if isinstance(content_or_path, Path):
        path = content_or_path.resolve()
        if not path.is_file():
            raise BuildError(f"recipe file not found: {path}")
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > MAX_PALIMPSESTFILE_BYTES:
            raise BuildError(f"Palimpsestfile exceeds 1 MiB limit ({len(raw_bytes)} bytes)")
        return parse_palimpsestfile_text(raw_bytes.decode("utf-8"))

    if "\n" in content_or_path or content_or_path.startswith("FROM") or content_or_path.startswith("#"):
        return parse_palimpsestfile_text(content_or_path)

    p = Path(content_or_path)
    if p.is_file():
        raw_bytes = p.read_bytes()
        if len(raw_bytes) > MAX_PALIMPSESTFILE_BYTES:
            raise BuildError(f"Palimpsestfile exceeds 1 MiB limit ({len(raw_bytes)} bytes)")
        return parse_palimpsestfile_text(raw_bytes.decode("utf-8"))

    return parse_palimpsestfile_text(content_or_path)


def verify_build_integrity(
    recipe: Palimpsestfile,
    cli_base: str,
    cli_layers: Sequence[str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Verify that CLI flags match the recipe's FROM and LAYER values exactly."""
    norm_cli_base = require_digest(cli_base)
    if norm_cli_base != recipe.base_digest:
        raise BuildError(
            f"base digest mismatch: CLI specified {norm_cli_base}, but recipe FROM specifies {recipe.base_digest}"
        )

    if cli_layers is not None:
        norm_cli_layers = tuple(require_digest(layer) for layer in cli_layers)
        if norm_cli_layers != recipe.layers:
            raise BuildError(
                f"layer chain mismatch: CLI specified {norm_cli_layers}, but recipe LAYER instructions specify {recipe.layers}"
            )
        effective_layers = norm_cli_layers
    else:
        effective_layers = recipe.layers

    return norm_cli_base, effective_layers


def generate_cleaning_command(upper_dir: str = "/mnt/palimpsest/capture/upper") -> str:
    """Return a guest shell snippet to apply palimpsest-clean-v1 to upper_dir."""
    targets_str = " ".join(f"{upper_dir}/{target}" for target in CLEAN_TARGETS_V1)
    return (
        f"for t in {targets_str}; do "
        'if [ -d "$t" ] && [ ! -L "$t" ]; then '
        'rm -rf "$t"/* "$t"/.[!.]* "$t"/..?* 2>/dev/null || true; '
        'elif [ -e "$t" ] || [ -L "$t" ]; then '
        'rm -f "$t" 2>/dev/null || true; '
        "fi; "
        "done"
    )


def create_build_record(
    *,
    build_id: str,
    base_digest: str,
    parent_digests: Sequence[str],
    recipe_sha256: str,
    network: Literal["none", "default"] = "none",
    output_tag: str | None = None,
    output_digest: str | None = None,
    output_media_type: str | None = None,
    output_size_bytes: int | None = None,
    created_at: str | None = None,
    finished_at: str | None = None,
    status: Literal["pending", "running", "success", "failed"] = "success",
) -> dict[str, Any]:
    """Construct a canonical build record dictionary matching schema_version 1."""
    base = require_digest(base_digest)
    parents = [require_digest(p) for p in parent_digests]
    rec_sha = require_digest(recipe_sha256)
    if network not in {"none", "default"}:
        raise ArtifactValidationError(f"invalid build network: {network!r}")
    if output_digest is not None:
        output_digest = require_digest(output_digest)

    now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

    return {
        "schema_version": 1,
        "build_id": build_id,
        "base_digest": base,
        "parent_digests": parents,
        "recipe_sha256": rec_sha,
        "cleaning_policy": PALIMPSEST_CLEAN_V1,
        "network": network,
        "output_tag": output_tag,
        "output_digest": output_digest,
        "output_media_type": output_media_type,
        "output_size_bytes": output_size_bytes,
        "created_at": created_at or now,
        "finished_at": finished_at,
        "status": status,
    }


def _finalize_build_output(
    tmp_host_out: Path,
    *,
    spec: BuildSpec,
    roots: state.StatePaths,
    existing_tag: TagRecord | None,
    build_dir: Path,
    record_data: dict[str, Any],
    recipe: Palimpsestfile,
    parent_digests: list[str],
    output_digest: str | None = None,
) -> dict[str, Any]:
    if not tmp_host_out.is_file() or tmp_host_out.stat().st_size < 4:
        raise BuildError("builder output.squashfs is missing or truncated")
    with tmp_host_out.open("rb") as fp:
        if fp.read(4) != b"hsqs":
            raise BuildError("builder output file is not a valid SquashFS archive (invalid magic)")
    actual_digest = digest_file(tmp_host_out)
    if output_digest is not None and actual_digest != output_digest:
        raise BuildError("builder output digest changed after transport verification")
    output_size = tmp_host_out.stat().st_size
    if existing_tag is not None and existing_tag.digest != actual_digest:
        raise BuildError(
            f"tag '{spec.output_name}' already exists with digest {existing_tag.digest}, conflicting with built digest {actual_digest}"
        )

    store = ContentStore(roots.store)
    store.ingest_file(tmp_host_out, expected_digest=actual_digest)
    store.write_metadata(
        actual_digest,
        {
            "kind": "squashfs",
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
            "parent_digest": spec.parent_layers[-1].digest if spec.parent_layers else None,
            "base_image_digest": spec.base.digest,
        },
    )
    tmp_host_out.unlink(missing_ok=True)
    tag_record = TagRecord(
        schema_version=1,
        tag=spec.output_name,
        digest=actual_digest,
        media_type=MEDIA_TYPE_LAYER_SQUASHFS,
        size_bytes=output_size,
        parent_digest=spec.parent_layers[-1].digest if spec.parent_layers else None,
        base_image_digest=spec.base.digest,
        source="build",
        created_at=state.utc_now_iso(),
    )
    write_tag_record(roots, tag_record)
    final_record = create_build_record(
        build_id=record_data["build_id"],
        base_digest=spec.base.digest,
        parent_digests=parent_digests,
        recipe_sha256=recipe.recipe_sha256,
        network=spec.network,
        output_tag=spec.output_name,
        output_digest=actual_digest,
        output_media_type=MEDIA_TYPE_LAYER_SQUASHFS,
        output_size_bytes=output_size,
        created_at=record_data["created_at"],
        finished_at=state.utc_now_iso(),
        status="success",
    )
    state.atomic_write_json(build_dir / "record.json", final_record)
    return final_record


def build_layer(
    spec: BuildSpec,
    *,
    roots: state.StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str = "qemu:///system",
    output_receiver: Callable[[Path, Path], str] | None = None,
) -> dict[str, Any]:
    """Execute a standalone build according to spec, returning the final build record."""
    roots = roots or state.init_roots()
    validate_tag(spec.output_name)

    tag_path = state.tag_path(roots, spec.output_name)
    existing_tag: TagRecord | None = None
    if tag_path.is_file():
        existing_tag = read_tag_record(roots, spec.output_name)

    recipe = parse_palimpsestfile(spec.recipe)
    cli_base = spec.base.digest
    cli_layers = tuple(layer.digest for layer in spec.parent_layers)
    verify_build_integrity(recipe, cli_base=cli_base, cli_layers=cli_layers)

    try:
        require_file_digest(spec.base.local_path, spec.base.digest)
    except Exception as exc:
        raise ArtifactValidationError(f"base image digest mismatch: {exc}") from exc
    backend = platforms.select_backend(spec.base.arch)
    if backend == "lima-vz":
        from . import lima

        return lima.build_layer(spec, roots=roots)
    for layer in spec.parent_layers:
        try:
            require_file_digest(layer.local_path, layer.digest)
        except Exception as exc:
            raise ArtifactValidationError(f"parent layer digest mismatch: {exc}") from exc

    build_id = f"b-{uuid.uuid4().hex[:12]}"
    build_dir = roots.builds / build_id
    build_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    parent_digests = [layer.digest for layer in spec.parent_layers]
    record_data = create_build_record(
        build_id=build_id,
        base_digest=spec.base.digest,
        parent_digests=parent_digests,
        recipe_sha256=recipe.recipe_sha256,
        network=spec.network,
        output_tag=spec.output_name,
        status="running",
    )
    state.atomic_write_json(build_dir / "record.json", record_data)
    console_log = build_dir / "console.log"
    console_log.write_text("", encoding="utf-8")

    builder_name = f"builder-{build_id}"
    builder_stack = StackRef(base=spec.base, layers=spec.parent_layers)
    builder_run_spec = RunSpec(
        name=builder_name,
        stack=builder_stack,
        memory_mib=4096,
        vcpus=2,
        network=spec.network,
    )

    from . import runtime

    builder_state: dict[str, Any] | None = None
    builder_cleaned = False

    def cleanup_builder() -> None:
        rpaths = state.run_paths(roots, builder_name)
        try:
            if rpaths.console.is_file():
                shutil.copyfile(rpaths.console, console_log)
        except Exception:
            pass
        runtime.rm(builder_name, roots=roots, volumes=True, conn=conn, kvm_uri=kvm_uri)

    try:
        layer_disks = [
            kvm.LayerDisk(
                blob_digest=layer.digest,
                host_path=layer.local_path,
                target_dev=f"vd{kvm._DISK_LETTERS[index]}",
                serial=layer.digest.split(":", 1)[1][:20],
            )
            for index, layer in enumerate(spec.parent_layers)
        ]
        serial_job = {
            "network": spec.network,
            "parent_mounts": [f"/mnt/palimpsest/lower{index}" for index in range(len(layer_disks))],
            "runs": [
                {"line": run.line, "command": run.command, "env": run.env, "workdir": run.workdir}
                for run in recipe.runs
            ],
        }
        user_data = cloudinit.build_serial_builder_user_data(
            activation_script=kvm.build_layer_activation_script(layer_disks),
            job=serial_job,
        )
        builder_state = runtime.start_serial_builder(
            builder_run_spec,
            user_data=user_data,
            roots=roots,
            conn=conn,
            kvm_uri=kvm_uri,
        )
        if builder_state.get("status") != "running":
            raise BuildError("serial builder did not reach the running state")
        tmp_host_out = build_dir / "output.squashfs"
        serial_socket = state.run_paths(roots, builder_name).root / "builder.sock"
        if output_receiver is None:
            output_digest = runtime.receive_serial_builder_output(serial_socket, tmp_host_out)
        else:
            output_digest = output_receiver(serial_socket, tmp_host_out)
        cleanup_builder()
        builder_cleaned = True
        return _finalize_build_output(
            tmp_host_out,
            spec=spec,
            roots=roots,
            existing_tag=existing_tag,
            build_dir=build_dir,
            record_data=record_data,
            recipe=recipe,
            parent_digests=parent_digests,
            output_digest=output_digest,
        )

    except Exception as exc:
        fin_at = state.utc_now_iso()
        failed_record = create_build_record(
            build_id=build_id,
            base_digest=spec.base.digest,
            parent_digests=parent_digests,
            recipe_sha256=recipe.recipe_sha256,
            network=spec.network,
            output_tag=spec.output_name,
            created_at=record_data["created_at"],
            finished_at=fin_at,
            status="failed",
        )
        state.atomic_write_json(build_dir / "record.json", failed_record)
        if isinstance(exc, (BuildError, ArtifactValidationError, StateError)):
            raise
        raise BuildError(f"build execution failed: {exc}") from exc
    finally:
        try:
            if not builder_cleaned:
                cleanup_builder()
        finally:
            (build_dir / "output.squashfs").unlink(missing_ok=True)


build = build_layer
