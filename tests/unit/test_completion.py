"""Unit tests for palimpsest_local.completion and shell completion CLI integration."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from palimpsest_local import cli, completion


def test_root_completion_candidates():
    parser = cli.build_parser()
    candidates = completion.resolve_candidates(parser, [""])

    top_actions = next(action for action in parser._actions if isinstance(action, cli.argparse._SubParsersAction))
    expected_subcommands = set(top_actions.choices.keys())
    expected_root_options = {"-h", "--help", "--url", "--version"}

    assert expected_subcommands.issubset(set(candidates))
    assert expected_root_options.issubset(set(candidates))

    # Never return filesystem entries
    cwd_files = {p.name for p in Path.cwd().iterdir()}
    assert not cwd_files.intersection(set(candidates))

    # Empty list input behaves identically
    assert completion.resolve_candidates(parser, []) == candidates


def test_nested_completion_candidates():
    parser = cli.build_parser()

    # image subcommands
    image_candidates = completion.resolve_candidates(parser, ["image", ""])
    assert "ls" in image_candidates
    assert "pull" in image_candidates
    assert "import" in image_candidates
    assert "verify" in image_candidates
    assert "--ubuntu-base" not in image_candidates  # image ls option, not image root option

    # store subcommands
    store_candidates = completion.resolve_candidates(parser, ["store", ""])
    assert set(store_candidates) >= {"show", "ls", "rm", "move", "set"}

    # compose subcommands
    compose_candidates = completion.resolve_candidates(parser, ["compose", ""])
    assert set(compose_candidates) >= {"config", "up", "down", "ps", "logs", "exec", "stop", "port"}

    # compose config options
    compose_config_candidates = completion.resolve_candidates(parser, ["compose", "config", ""])
    assert "--format" in compose_config_candidates
    assert "--quiet" in compose_config_candidates
    assert "--services" in compose_config_candidates


def test_option_and_choices_completion():
    parser = cli.build_parser()

    # Option prefix
    assert completion.resolve_candidates(parser, ["run", "--b"]) == ["--backend"]
    assert completion.resolve_candidates(parser, ["compose", "config", "--f"]) == ["--format"]
    assert "--url" not in completion.resolve_candidates(parser, ["run", "--"])

    # Choices completion
    backend_choices = completion.resolve_candidates(parser, ["run", "--backend", ""])
    assert backend_choices == ["auto", "kvm", "libvirt-hvf", "lima-vz"]

    backend_filtered = completion.resolve_candidates(parser, ["run", "--backend", "l"])
    assert backend_filtered == ["libvirt-hvf", "lima-vz"]

    arch_choices = completion.resolve_candidates(parser, ["image", "import", "--arch", ""])
    assert arch_choices == ["aarch64", "x86_64"]

    disk_format_filtered = completion.resolve_candidates(parser, ["image", "import", "--disk-format", "q"])
    assert disk_format_filtered == ["qcow2"]

    # --opt=val syntax
    assert completion.resolve_candidates(parser, ["run", "--backend=a"]) == ["--backend=auto"]
    assert completion.resolve_candidates(parser, ["run", "--backend=l"]) == [
        "--backend=libvirt-hvf",
        "--backend=lima-vz",
    ]


def test_prefix_filtering():
    parser = cli.build_parser()

    i_candidates = completion.resolve_candidates(parser, ["i"])
    assert set(i_candidates) == {"image", "images", "inspect"}

    st_candidates = completion.resolve_candidates(parser, ["st"])
    assert set(st_candidates) == {"stop", "store"}


def test_aliases_and_short_long_flags():
    parser = cli.build_parser()

    # Short/long option flags for build (-t/--tag, -f/--file)
    build_flags = completion.resolve_candidates(parser, ["build", "-"])
    assert "-t" in build_flags
    assert "--tag" in build_flags
    assert "-f" in build_flags
    assert "--file" in build_flags


def test_no_path_fallback_and_no_choices_option():
    parser = cli.build_parser()

    # Option with value but no defined choices -> empty list, no fallback
    assert completion.resolve_candidates(parser, ["run", "--memory", ""]) == []
    assert completion.resolve_candidates(parser, ["run", "--name", ""]) == []

    # Positional arg with no defined choices -> empty list, no fallback
    assert completion.resolve_candidates(parser, ["exec", "demo", ""]) == []


def test_remainder_command_completion():
    parser = cli.build_parser()

    # docker takes REMAINDER args
    assert completion.resolve_candidates(parser, ["docker", ""]) == []
    assert completion.resolve_candidates(parser, ["docker", "ps"]) == []

    # exec command takes REMAINDER args after target name
    assert completion.resolve_candidates(parser, ["exec", "demo", "ls"]) == []

    # compose exec command takes REMAINDER args
    assert completion.resolve_candidates(parser, ["compose", "exec", "svc", "ls"]) == []

    # -- argument separator forces remainder mode
    assert completion.resolve_candidates(parser, ["run", "img", "--", "foo"]) == []


def test_generated_registration_scripts():
    zsh_script = completion.generate_completion_script("zsh")
    assert "#compdef palimpsest" in zsh_script
    assert "compdef _palimpsest palimpsest" in zsh_script
    assert "${words[@]:1:$((CURRENT-1))}" in zsh_script
    assert "emulate -L zsh" in zsh_script
    assert 'compstate[insert]=""' in zsh_script
    assert "return 0" in zsh_script

    bash_script = completion.generate_completion_script("bash")
    assert "COMPREPLY" in bash_script
    assert "complete -F _palimpsest_completion palimpsest" in bash_script
    assert "COMP_WORDS" in bash_script
    assert "COMP_CWORD" in bash_script

    fish_script = completion.generate_completion_script("fish")
    assert "complete -c palimpsest -f" in fish_script
    assert "commandline -xpc" in fish_script
    assert "commandline -ct" in fish_script
    assert "command $cmd __complete" in fish_script
    assert "__palimpsest_completion" in fish_script

    with pytest.raises(ValueError, match="unsupported shell"):
        completion.generate_completion_script("powershell")


def test_conditional_shell_syntax_checks():
    if shutil.which("bash"):
        script = completion.generate_completion_script("bash")
        proc = subprocess.run(["bash", "-n"], input=script.encode("utf-8"), capture_output=True)
        assert proc.returncode == 0, f"bash -n failed: {proc.stderr.decode()}"

    if shutil.which("zsh"):
        script = completion.generate_completion_script("zsh")
        proc = subprocess.run(["zsh", "-n"], input=script.encode("utf-8"), capture_output=True)
        assert proc.returncode == 0, f"zsh -n failed: {proc.stderr.decode()}"

    if shutil.which("fish"):
        script = completion.generate_completion_script("fish")
        proc = subprocess.run(["fish", "-n"], input=script.encode("utf-8"), capture_output=True)
        assert proc.returncode == 0, f"fish -n failed: {proc.stderr.decode()}"


def test_executable_bash_completion():
    if not shutil.which("bash"):
        pytest.skip("bash not installed")

    repo_root = Path(__file__).parent.parent.parent
    src_dir = repo_root / "src"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        palimpsest_bin = bin_dir / "palimpsest"
        palimpsest_bin.write_text(f'#!/usr/bin/env sh\nexec "{sys.executable}" -m palimpsest_local.cli "$@"\n')
        palimpsest_bin.chmod(0o755)

        script = completion.generate_completion_script("bash")
        script_file = tmp_path / "completion.bash"
        script_file.write_text(script)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["PYTHONPATH"] = f"{src_dir}:{env.get('PYTHONPATH', '')}"

        # Case 1: --backend=a yields auto
        test_cmd_1 = f"""
source "{script_file}"
COMP_WORDS=("palimpsest" "run" "--backend" "=" "a")
COMP_CWORD=4
_palimpsest_completion
printf "%s\\n" "${{COMPREPLY[@]}}"
"""
        proc1 = subprocess.run(
            ["bash", "-c", test_cmd_1],
            env=env,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc1.stdout.strip() == "auto"

        # Case 2: ordinary completion
        test_cmd_2 = f"""
source "{script_file}"
COMP_WORDS=("palimpsest" "st")
COMP_CWORD=1
_palimpsest_completion
printf "%s\\n" "${{COMPREPLY[@]}}"
"""
        proc2 = subprocess.run(
            ["bash", "-c", test_cmd_2],
            env=env,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [line.strip() for line in proc2.stdout.splitlines() if line.strip()]
        assert "stop" in lines
        assert "store" in lines


def test_executable_fish_completion():
    if not shutil.which("fish"):
        return

    repo_root = Path(__file__).parent.parent.parent
    src_dir = repo_root / "src"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        palimpsest_bin = bin_dir / "palimpsest"
        palimpsest_bin.write_text(f'#!/usr/bin/env sh\nexec "{sys.executable}" -m palimpsest_local.cli "$@"\n')
        palimpsest_bin.chmod(0o755)

        script = completion.generate_completion_script("fish")
        script_file = tmp_path / "completion.fish"
        script_file.write_text(script)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["PYTHONPATH"] = f"{src_dir}:{env.get('PYTHONPATH', '')}"

        test_cmd = f"source '{script_file}'; complete -C 'palimpsest run --backend=a'"
        proc = subprocess.run(
            ["fish", "-c", test_cmd],
            env=env,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "auto" in proc.stdout


def test_cli_main_complete_interception_and_completion_cmd(capsys: pytest.CaptureFixture[str]):
    # Hidden __complete interception
    ret = cli.main(["__complete", "--", "run", "--backend", "a"])
    assert ret == 0
    out = capsys.readouterr().out
    assert out.strip() == "auto"

    # Visible completion command
    ret_zsh = cli.main(["completion", "zsh"])
    assert ret_zsh == 0
    out_zsh = capsys.readouterr().out
    assert "compdef _palimpsest palimpsest" in out_zsh

    ret_bash = cli.main(["completion", "bash"])
    assert ret_bash == 0
    out_bash = capsys.readouterr().out
    assert "complete -F _palimpsest_completion palimpsest" in out_bash

    ret_fish = cli.main(["completion", "fish"])
    assert ret_fish == 0
    out_fish = capsys.readouterr().out
    assert "complete -c palimpsest -f" in out_fish


def test_exact_top_level_command_set():
    parser = cli.build_parser()
    top_actions = next(action for action in parser._actions if isinstance(action, cli.argparse._SubParsersAction))
    expected = {
        "image",
        "layer",
        "bundle",
        "build",
        "registry",
        "login",
        "logout",
        "pull",
        "push",
        "tag",
        "images",
        "history",
        "rmi",
        "save",
        "load",
        "docker",
        "run",
        "compose",
        "ps",
        "inspect",
        "logs",
        "shell",
        "exec",
        "stop",
        "rm",
        "commit",
        "ui",
        "store",
        "completion",
    }
    assert set(top_actions.choices.keys()) == expected
