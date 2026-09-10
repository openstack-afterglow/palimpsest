#!/usr/bin/env python3
"""Generate the exhaustive argparse-derived Palimpsest CLI reference."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OUTPUT = ROOT / "docs" / "cli" / "reference.md"
sys.path.insert(0, str(SRC))

from palimpsest_local.cli import build_parser  # noqa: E402

DESCRIPTIONS = {
    "version": "Print the installed Palimpsest version and exit.",
    "url": "Override the Palimpsest Hub base URL.",
    "ubuntu_base": "Filter or record the Ubuntu base release.",
    "arch": "Filter or record the image CPU architecture.",
    "os_variant": "Filter or record the libvirt OS variant.",
    "disk_format": "Select the cloud-image disk format.",
    "limit": "Maximum results to return (valid range: 1–200).",
    "digest": "Expected or selected SHA-256 content digest.",
    "output": "Write or copy output at this path.",
    "path": "Local filesystem path to inspect.",
    "name": "Managed object, run, profile, or publication name.",
    "publish": "Make the uploaded Hub artifact publicly visible.",
    "references": "One or more Docker image references or IDs.",
    "format": "Select the output representation or Docker format template.",
    "platform": "Select an OCI platform.",
    "registry": "Resolve the operation through this registry profile.",
    "reference": "Docker image reference or ID.",
    "no_trunc": "Do not truncate Docker output.",
    "quiet": "Suppress normal detail or print identifiers only.",
    "force": "Replace, remove, or overwrite despite the normal protection.",
    "no_prune": "Do not delete untagged parents.",
    "input_path": "Read the Docker image archive from this file instead of stdin.",
    "kind": "Filter by artifact kind.",
    "parent": "Record or filter by the parent layer digest.",
    "directory": "Directory to pack or verify.",
    "tag": "Tag to assign to the produced artifact.",
    "value": "Local layer tag or digest to upload.",
    "base_image": "Cloud-image digest anchoring the layer chain.",
    "leaf_digest": "Leaf layer digest of the bundle chain.",
    "include_base": "Include the cloud-image base in the downloaded bundle.",
    "source": "Local OCI archive or image-layout source.",
    "packer": "Absolute SquashFS packer executable (verified before use).",
    "timeout": "Materializer timeout in seconds; must be positive and finite.",
    "volume_id": "Exact retained OCI root-volume UUID.",
    "context": "Dockerfile build context directory.",
    "frontend": "Choose frontend explicitly or infer it from the other arguments.",
    "base": "Legacy Palimpsestfile cloud-image base digest.",
    "recipe": "Build recipe path (Dockerfile or Palimpsestfile).",
    "layer": "Add a legacy layer digest; repeat to preserve chain order.",
    "network": "Select build or runtime networking.",
    "offline": "Enforce an offline Dockerfile build.",
    "target": "Dockerfile build stage to target.",
    "build_arg": "Set a Dockerfile build argument; repeatable.",
    "local_image": "Add `ALIAS=/absolute/layout@sha256:DIGEST` as a verified local OCI build context; repeatable.",
    "cache_scope": "Select the Palimpsest Hub cache scope.",
    "cache_from": "Add an external BuildKit cache source; repeatable.",
    "cache_to": "Add an external BuildKit cache destination; repeatable.",
    "no_cache": "Disable cache reuse (offline builds only).",
    "pull": "Always resolve newer base images.",
    "load": "Load the build result into the local Docker image store.",
    "progress": "Select BuildKit progress rendering.",
    "rootfs_output": "Export the built root filesystem at this path.",
    "runtime_tag": "Tag for the generated SquashFS runtime block.",
    "runtime_base": "Base digest for the generated runtime-block chain.",
    "runtime_block_size": "SquashFS block size: power of two from 4096 through 1048576 bytes (semantic default: 131072).",
    "endpoint": "Registry hostname/endpoint.",
    "namespace": "Namespace prepended to short image references.",
    "mirror": "Add a registry mirror; repeatable.",
    "ca": "Add a trusted registry CA file; repeatable.",
    "plain_http": "Allow unencrypted HTTP for this registry.",
    "tls_skip_verify": "Disable TLS certificate verification for this registry.",
    "default": "Make this the default registry profile.",
    "server": "Docker registry server or configured profile name.",
    "username": "Docker registry username.",
    "password_stdin": "Read the registry password from stdin.",
    "all_tags": "Operate on every tag; reference must be an untagged repository.",
    "repository": "Optional repository filter.",
    "all": "Include intermediate and dangling images.",
    "digests": "Show content digests.",
    "filter": "Add a Docker image-list filter; repeatable.",
    "tree": "Show Docker's image tree view.",
    "docker_args": "Arguments passed verbatim to the Docker CLI.",
    "image_or_bundle": "Cloud digest/bundle directory or local OCI archive/layout.",
    "memory": "Guest memory in MiB (valid range: 256–1,048,576).",
    "vcpus": "Guest virtual CPUs (valid range: 1–256).",
    "backend": "Select or automatically detect the runtime backend.",
    "runtime_kind": "Disambiguate cloud-image and local OCI-root execution.",
    "project_file": "Compose-shaped project file path.",
    "project_name": "Override the deterministic project name.",
    "project_directory": "Base directory for project-contained resources.",
    "env_file": "Load a project-contained interpolation file; repeatable.",
    "services": "Optional service names; omission selects the command default set.",
    "detach": "Return after startup instead of following workload/log output.",
    "no_recreate": "Keep existing matching service runs.",
    "force_recreate": "Replace existing service runs.",
    "volumes": "Also request removal of managed conventional/project volumes.",
    "follow": "Continue streaming appended logs.",
    "service": "Project service name.",
    "command": "Command and arguments passed to the managed workload.",
    "private_port": "Guest/private port number.",
    "protocol": "Published transport protocol.",
    "completion_record": "Persist the OCI exec completion record at this path.",
    "port": "UI listen port; 0 selects an available port.",
    "no_browser": "Do not open the UI in a browser.",
    "destination": "New local store path.",
    "keep_source": "Keep source bytes after moving the store.",
    "shell": "Shell completion target.",
}


PATH_DESCRIPTIONS = {
    (("image", "verify"), "path"): "Local image file whose bytes must match `--digest`.",
    (("image", "push"), "path"): "Local qcow2/raw cloud-image file to upload to Hub `/v1`.",
    (("oci", "init-runtime"), "path"): "Parent directory in which to create secured OCI runtime state.",
    (("oci", "exec-record"), "path"): "Durable OCI exec completion-record path to validate and print.",
    (("build",), "output"): "OCI archive output path; an invocation-unique state path is chosen when omitted.",
    (("image", "import"), "path"): "Local qcow2/raw cloud-image file to import into the local store.",
    (("tag",), "source"): "Existing Docker image reference or ID.",
    (("tag",), "target"): "New target reference, resolved through the selected registry profile.",
    (("oci", "materialize"), "output"): "New file for the canonical materialization receipt; stdout if omitted.",
    (("run",), "network"): "Runtime network; OCI-root currently permits only `none`.",
    (("run",), "layer"): "Append a verified SquashFS layer digest to a cloud-image stack.",
    (("run",), "detach"): "OCI-root only: return after authenticated READY and leave the VM running.",
    (("store", "rm"), "force"): "Accepted for compatibility; does not bypass referenced-object or lease protections.",
}


def _display_default(action: argparse.Action) -> str:
    if _is_semantically_required(action):
        return "required"
    if action.default is argparse.SUPPRESS:
        return "—"
    if action.default is None:
        return _code("None")
    if isinstance(action.default, Path):
        return _code(action.default)
    return _code(repr(action.default))


def _is_semantically_required(action: argparse.Action) -> bool:
    """Normalize argparse's version-dependent positional ``required`` field."""
    if action.option_strings:
        return action.required
    return action.nargs not in ("?", "*", argparse.REMAINDER)


def _value(action: argparse.Action) -> str:
    if action.choices is not None:
        return "one of: " + ", ".join(_code(choice) for choice in action.choices)
    if action.nargs == 0:
        return "flag"
    type_name = getattr(action.type, "__name__", None) or "string"
    suffix = {
        "?": " (optional)",
        "*": " (zero or more)",
        "+": " (one or more)",
        argparse.REMAINDER: " (remainder)",
    }.get(action.nargs, "")
    repeat = "; repeatable" if isinstance(action, argparse._AppendAction) else ""
    return f"{type_name}{suffix}{repeat}"


def _label(action: argparse.Action) -> str:
    if action.option_strings:
        return ", ".join(_code(item) for item in action.option_strings)
    return _code(action.dest)


def _code(value: object) -> str:
    content = str(value).replace("&", "&amp;").replace("|", "&#124;").replace("`", "&#96;")
    return f"`{content}`"


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ")


def _description(path: tuple[str, ...], action: argparse.Action) -> str:
    parser_help = action.help if action.help not in (None, argparse.SUPPRESS) else None
    authored = PATH_DESCRIPTIONS.get((path, action.dest), DESCRIPTIONS.get(action.dest))
    description = parser_help or authored
    if description is None:
        raise RuntimeError(f"missing authored CLI description for {' '.join(path) or '<root>'}:{action.dest}")
    return _escape_cell(description)


def _commands(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _commands(child, (*path, name))


def _format_usage(parser: argparse.ArgumentParser) -> str:
    """Render canonical syntax independent of Python and terminal wrapping."""
    original = parser.formatter_class
    normalized: list[tuple[argparse.Action, bool]] = []
    for action in parser._actions:
        if not action.option_strings and action.nargs in ("?", "*", argparse.REMAINDER):
            normalized.append((action, action.required))
            action.required = False
    parser.formatter_class = lambda prog: argparse.HelpFormatter(prog, width=1_000_000, max_help_position=24)
    try:
        return " ".join(parser.format_usage().split())
    finally:
        parser.formatter_class = original
        for action, required in normalized:
            action.required = required


GUIDE_ANCHORS = {
    "": "invocation-and-configuration",
    "image": "hub-cloud-images-layers-and-bundles",
    "layer": "hub-cloud-images-layers-and-bundles",
    "bundle": "hub-cloud-images-layers-and-bundles",
    "registry": "registry-profiles-and-docker-wrapper-commands",
    "login": "registry-profiles-and-docker-wrapper-commands",
    "logout": "registry-profiles-and-docker-wrapper-commands",
    "pull": "registry-profiles-and-docker-wrapper-commands",
    "push": "registry-profiles-and-docker-wrapper-commands",
    "tag": "registry-profiles-and-docker-wrapper-commands",
    "images": "registry-profiles-and-docker-wrapper-commands",
    "history": "registry-profiles-and-docker-wrapper-commands",
    "rmi": "registry-profiles-and-docker-wrapper-commands",
    "save": "registry-profiles-and-docker-wrapper-commands",
    "load": "registry-profiles-and-docker-wrapper-commands",
    "docker": "registry-profiles-and-docker-wrapper-commands",
    "build": "build",
    "run": "run-cloud-image-versus-oci-root",
    "oci": "runtime-lifecycle-and-observation",
    "compose": "compose-shaped-projects",
    "store": "local-store-and-ui",
    "ui": "local-store-and-ui",
    "completion": "invocation-and-configuration",
}


def render() -> str:
    parser = build_parser()
    entries = list(_commands(parser))
    option_count = sum(
        1
        for _path, command in entries
        for action in command._actions
        if not isinstance(action, argparse._SubParsersAction) and action.dest != "help"
    )
    lines = [
        "<!-- Generated by scripts/generate_cli_reference.py; do not edit. -->",
        "# Palimpsest CLI syntax reference",
        "",
        "This file is generated from `palimpsest_local.cli.build_parser`. It inventories every command, positional, option, choice, and parser default. For behavior, examples, and runtime restrictions, read [the usage guide](usage.md). Every command except raw `docker` pass-through accepts automatic `-h`/`--help`.",
        "",
        f"Coverage: **{len(entries) - 1} command paths**, **{option_count} positional/option definitions** (excluding automatic `-h/--help`).",
        "",
    ]
    for path, command in entries:
        title = "palimpsest" if not path else "palimpsest " + " ".join(path)
        level = "##" if len(path) <= 1 else "###"
        anchor = GUIDE_ANCHORS.get(path[0] if path else "", "runtime-lifecycle-and-observation")
        help_note = (
            "`-h`/`--help` is forwarded to Docker; this pass-through parser has no Palimpsest help flag."
            if path == ("docker",)
            else "Automatic `-h`/`--help` prints help for this command and exits."
        )
        lines.extend(
            [
                f"{level} `{title}`",
                "",
                f"[Behavior, restrictions, and examples](usage.md#{anchor}).",
                "",
                help_note,
                "",
                "```text",
                _format_usage(command),
                "```",
                "",
            ]
        )
        subcommands: list[str] = []
        actions: list[argparse.Action] = []
        for action in command._actions:
            if isinstance(action, argparse._SubParsersAction):
                subcommands.extend(action.choices)
            elif action.dest != "help":
                actions.append(action)
        if subcommands:
            lines.extend(["Subcommands: " + ", ".join(f"`{name}`" for name in subcommands) + ".", ""])
        if actions:
            lines.extend(["| Argument | Value | Parser default | Description |", "| --- | --- | --- | --- |"])
            for action in actions:
                lines.append(
                    f"| {_label(action)} | {_value(action)} | "
                    f"{_display_default(action)} | {_description(path, action)} |"
                )
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the generated reference is stale")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    expected = render()
    output = args.output.resolve(strict=False)
    if args.check:
        try:
            actual = output.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"missing generated CLI reference: {output}", file=sys.stderr)
            return 1
        if actual != expected:
            print(f"stale generated CLI reference: {output}", file=sys.stderr)
            return 1
        print(f"CLI reference is current: {output}")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(expected, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
