"""The fixed v1 argparse surface for the ``palimpsest`` command."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, lima
from .build import build_layer, parse_palimpsestfile, verify_build_integrity
from .digest import digest_file, digest_hex, require_digest
from .errors import PalimpsestError
from .hub import DISK_FORMAT_MEDIA_TYPES, KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS, HubClient
from .oci_layout import ContentStore, extract_bundle_tar, verify_layout_dir
from .refs import BuildSpec, ImageRef, LayerRef, RunSpec, StackRef
from .runtime import (
    commit,
    exec_command,
    inspect_run,
    logs,
    ps,
    rm,
    run,
    shell_command,
    stop,
)
from .state import TagRecord, init_roots, read_tag_record, run_paths, write_tag_record


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


def _resolve_image_ref(store: ContentStore, digest: str, explicit_url: str | None) -> ImageRef:
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
    build.add_argument("--base", required=True)
    build.add_argument("--tag", required=True)
    build.add_argument("-f", dest="recipe", type=Path, default=Path("./Palimpsestfile"))
    build.add_argument("--layer", action="append", default=[])
    build.add_argument("--network", choices=("none", "default"), default="none")

    run = commands.add_parser("run")
    run.add_argument("image_or_bundle")
    run.add_argument("--name", required=True)
    run.add_argument("--layer", action="append", default=[])
    run.add_argument("--memory", type=int, default=4096)
    run.add_argument("--vcpus", type=int, default=2)
    run.add_argument("--network", default="default")

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
    stop = commands.add_parser("stop")
    stop.add_argument("name")
    remove = commands.add_parser("rm")
    remove.add_argument("name")
    remove.add_argument("--volumes", action="store_true")
    commit = commands.add_parser("commit")
    commit.add_argument("name")
    commit.add_argument("--tag", required=True)
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if hasattr(args, "limit") and not 1 <= args.limit <= 200:
        parser.error("--limit must be between 1 and 200")
    if args.operation == "run":
        if not 256 <= args.memory <= 1_048_576:
            parser.error("--memory must be between 256 and 1048576")
        if not 1 <= args.vcpus <= 256:
            parser.error("--vcpus must be between 1 and 256")
    if args.operation == "exec":
        if args.command[:1] == ["--"]:
            args.command = args.command[1:]
        if not args.command:
            parser.error("exec requires a command after --")


def dispatch_args(args: argparse.Namespace) -> int:
    op = args.operation
    roots = init_roots()
    store = ContentStore(roots.store)

    if op == "image":
        img_op = args.image_operation
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
            if not args.path.is_file():
                raise PalimpsestError(f"image path not found: {args.path}")
            image_digest = digest_file(args.path)
            store.ingest_file(args.path, expected_digest=image_digest)
            store.write_metadata(
                image_digest,
                {
                    "kind": KIND_CLOUD_IMAGE,
                    "disk_format": args.disk_format,
                    "arch": args.arch,
                    "os_variant": args.os_variant,
                    "name": args.path.name,
                },
            )
            print(image_digest)
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
            else:
                if not args.name:
                    raise PalimpsestError("--name is required when pushing a layer path")
                path = path_val
                name = args.name
                parent_digest = require_digest(args.parent) if args.parent else None
                base_image_digest = require_digest(args.base_image) if args.base_image else None
            metadata = {
                "name": name,
                "kind": "squashfs",
                "parent_digest": parent_digest,
                "base_image_digest": base_image_digest,
                "ubuntu_base": args.ubuntu_base,
                "is_published": args.publish,
            }
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
        recipe_path = args.recipe
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
                output_name=args.tag,
            ),
            roots=roots,
        )
        print(record["output_digest"])

    elif op == "run":
        target_path = Path(args.image_or_bundle)
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
            base_ref = ImageRef(
                digest=base_entry.digest,
                disk_format=disk_format,
                arch="x86_64",
                os_variant=None,
                local_path=base_entry.local_path,
            )
            bundle_layer_refs = tuple(
                LayerRef(digest=entry.digest, media_type=entry.media_type, local_path=entry.local_path)
                for entry in entries[1:]
            )
            continuation_parent = bundle_layer_refs[-1].digest if bundle_layer_refs else None
            cli_layer_refs = _layer_refs_from_store(
                store,
                args.layer,
                base_ref.digest,
                parent_digest=continuation_parent,
            )
            stack = StackRef(base=base_ref, layers=bundle_layer_refs + cli_layer_refs)
        else:
            base_ref = _resolve_image_ref(store, args.image_or_bundle, args.url)
            stack = StackRef(base=base_ref, layers=_layer_refs_from_store(store, args.layer, base_ref.digest))
        run_spec = RunSpec(
            name=args.name,
            stack=stack,
            memory_mib=args.memory,
            vcpus=args.vcpus,
            network=args.network,
        )
        res_dict = (
            lima.run(run_spec, roots=roots)
            if lima.available() and stack.base.arch == "aarch64"
            else run(run_spec, roots=roots)
        )
        if res_dict.get("backend") == "lima-vz":
            print(f"limactl shell {args.name}")
        else:
            print(res_dict.get("guest_ip", args.name))

    elif op == "ps":
        runs = ps(roots=roots)
        print(f"{'NAME':<20} {'STATUS':<12} {'BASE':<12} {'LAYERS':<8} {'IP':<16} {'CREATED':<24}")
        for run_record in runs:
            base_hex = str(run_record["base_digest"]).split(":", 1)[-1][:12]
            guest_ip = run_record.get("guest_ip") or "-"
            created_at = run_record.get("created_at") or "-"
            print(
                f"{run_record['name']:<20} {run_record['status']:<12} {base_hex:<12} "
                f"{run_record['layers_count']:<8} {guest_ip:<16} {created_at:<24}"
            )

    elif op == "inspect":
        info = inspect_run(args.name, roots=roots)
        print(json.dumps(info, indent=2))

    elif op == "logs":
        for chunk in logs(args.name, roots=roots, follow=args.follow):
            sys.stdout.write(chunk)
            sys.stdout.flush()

    elif op == "shell":
        argv = (
            lima.shell_command(args.name, roots=roots)
            if lima.is_lima_run(run_paths(roots, args.name))
            else shell_command(args.name, roots=roots)
        )
        proc = subprocess.run(argv, shell=False)
        return proc.returncode

    elif op == "exec":
        argv = (
            lima.exec_command(args.name, args.command, roots=roots)
            if lima.is_lima_run(run_paths(roots, args.name))
            else exec_command(args.name, args.command, roots=roots)
        )
        proc = subprocess.run(argv, shell=False)
        return proc.returncode
    elif op == "stop":
        if lima.is_lima_run(run_paths(roots, args.name)):
            lima.stop(args.name, roots=roots)
        else:
            stop(args.name, roots=roots)
        print(f"stopped {args.name}")

    elif op == "rm":
        if lima.is_lima_run(run_paths(roots, args.name)):
            lima.rm(args.name, roots=roots, volumes=args.volumes)
        else:
            rm(args.name, roots=roots, volumes=args.volumes)
        print(f"removed {args.name}")

    elif op == "commit":
        if lima.is_lima_run(run_paths(roots, args.name)):
            raise PalimpsestError("native macOS Lima runs do not support commit; use palimpsest build")
        result = commit(args.name, args.tag, roots=roots)
        print(result["digest"])

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        _validate_args(args, parser)
        return dispatch_args(args)
    except SystemExit as exc:
        return int(exc.code)
    except PalimpsestError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
