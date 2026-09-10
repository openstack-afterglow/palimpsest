"""Regression contracts for the generated argparse CLI reference."""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from palimpsest_local import cli

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "generate_cli_reference.py"
LANES_SCRIPT = ROOT / "scripts" / "test_lanes.py"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reference = _load_script("palimpsest_cli_reference", GENERATOR)
lanes = _load_script("palimpsest_cli_reference_lanes", LANES_SCRIPT)


def _parser_inventory(
    parser: argparse.ArgumentParser,
    path: tuple[str, ...] = (),
) -> tuple[dict[tuple[str, ...], argparse.ArgumentParser], int]:
    """Walk argparse independently of the generator's private traversal helper."""
    commands = {path: parser}
    argument_count = 0
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                descendants, child_count = _parser_inventory(child, (*path, name))
                commands.update(descendants)
                argument_count += child_count
        elif action.dest != "help":
            argument_count += 1
    return commands, argument_count


def _heading_paths(rendered: str) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for title in re.findall(r"^#{2,3} `palimpsest(?: ([^`]+))?`$", rendered, re.MULTILINE):
        paths.add(tuple(title.split()) if title else ())
    return paths


def _command_section(rendered: str, path: tuple[str, ...]) -> str:
    title = "palimpsest" + (" " + " ".join(path) if path else "")
    match = re.search(
        rf"^#{'{2,3}'} `{re.escape(title)}`\n(?P<body>.*?)(?=^#{'{2,3}'} `palimpsest(?: |`)|\Z)",
        rendered,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, title
    return match.group("body")


def test_render_covers_every_parser_path_argument_and_alias_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = cli.build_parser()
    commands, argument_count = _parser_inventory(parser)
    monkeypatch.setattr(cli, "main", lambda *_args, **_kwargs: pytest.fail("generation dispatched the CLI"))

    rendered = reference.render()

    assert _heading_paths(rendered) == set(commands)
    assert (
        f"Coverage: **{len(commands) - 1} command paths**, **{argument_count} positional/option definitions**"
        in rendered
    )
    for path, command in commands.items():
        section = _command_section(rendered, path)
        assert command.format_usage().strip() in section
        for action in command._actions:
            if action.dest == "help" or isinstance(action, argparse._SubParsersAction):
                continue
            expected_aliases = action.option_strings or [action.dest]
            assert all(f"`{alias}`" in section for alias in expected_aliases)


def test_render_preserves_choices_types_repeatability_defaults_and_markdown_escaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = argparse.ArgumentParser(prog="palimpsest")
    parser.add_argument("--mode", choices=("fast|safe", "tick`mode"), default="fast|safe")
    parser.add_argument("--path", type=Path, default=Path("a|b"))
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--label", action="append", default=[], help="repeat | `label`")
    parser.add_argument("--hidden", default=argparse.SUPPRESS)
    monkeypatch.setattr(reference, "build_parser", lambda: parser)
    monkeypatch.setattr(
        reference,
        "DESCRIPTIONS",
        {
            **reference.DESCRIPTIONS,
            "mode": "Mode.",
            "count": "Count.",
            "label": "Repeatable label.",
            "hidden": "Hidden.",
        },
    )

    rendered = reference.render()

    assert "one of:" in rendered
    assert "fast&#124;safe" in rendered and "tick&#96;mode" in rendered
    assert "int" in rendered and "required" in rendered
    assert "string; repeatable" in rendered and "`[]`" in rendered
    assert "`a&#124;b`" in rendered
    assert "repeat &#124; &#96;label&#96;" in rendered
    assert "| `--hidden` | string | — |" in rendered


def test_render_is_independent_of_terminal_width_and_explains_automatic_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "24")
    narrow = reference.render()
    monkeypatch.setenv("COLUMNS", "240")
    wide = reference.render()

    assert narrow == wide
    assert "`-h`/`--help`" in narrow
    assert "every command" in narrow


@pytest.mark.parametrize(("contents", "diagnostic"), [(None, "missing"), ("stale\n", "stale")])
def test_check_rejects_missing_or_stale_output(
    tmp_path: Path,
    contents: str | None,
    diagnostic: str,
) -> None:
    output = tmp_path / "nested" / "reference.md"
    if contents is not None:
        output.parent.mkdir()
        output.write_text(contents, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check", "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"{diagnostic} generated CLI reference: {output}" in result.stderr
    if contents is not None:
        assert output.read_text(encoding="utf-8") == contents


def test_write_is_repeatable_and_then_passes_check(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "reference.md"
    command = [sys.executable, str(GENERATOR), "--output", str(output)]

    first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    assert first.returncode == 0
    initial = output.read_bytes()
    second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    assert second.returncode == 0
    assert output.read_bytes() == initial
    checked = subprocess.run(
        [sys.executable, str(GENERATOR), "--check", "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0
    assert f"CLI reference is current: {output}" in checked.stdout


def test_generated_tool_changes_select_only_core_cli() -> None:
    for path in ("scripts/generate_cli_reference.py", "scripts/build_package.py"):
        selection = lanes.select_changed((path,))
        assert selection.lanes == ("core-cli",)
        assert selection.suggested == ()
