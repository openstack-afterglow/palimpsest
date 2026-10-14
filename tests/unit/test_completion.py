"""Unit tests for palimpsest_local.completion and shell completion CLI integration."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from palimpsest_local import cli, completion


def test_root_completion_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    parser = cli.build_parser()
    candidates = completion.resolve_candidates(parser, [""])

    top_actions = next(action for action in parser._actions if isinstance(action, cli.argparse._SubParsersAction))
    expected_subcommands = set(top_actions.choices.keys())
    expected_root_options = {"-h", "--help", "--url", "--version"}

    assert expected_subcommands.issubset(set(candidates))
    assert expected_root_options.issubset(set(candidates))

    # Never return filesystem entries from an unrelated cwd
    sentinel = tmp_path / "zz-not-a-subcommand"
    sentinel.mkdir()
    monkeypatch.chdir(tmp_path)
    candidates = completion.resolve_candidates(parser, [""])
    assert "zz-not-a-subcommand" not in candidates

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

    assert "materialize" in completion.resolve_candidates(parser, ["oci", ""])
    assert "--manifest" in completion.resolve_candidates(parser, ["oci", "materialize", "--"])


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
    assert set(st_candidates) == {"start", "stop", "store"}


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


def test_remainder_command_completion(monkeypatch: pytest.MonkeyPatch):
    parser = cli.build_parser()

    # Docker owns its command surface rather than argparse's REMAINDER.
    monkeypatch.setattr(completion, "_docker_output", lambda words: "ps\tList containers\n:4\n")
    assert completion.resolve_candidates(parser, ["docker", ""]) == ["ps"]
    assert completion.resolve_candidates(parser, ["docker", "ps"]) == ["ps"]
    # exec command takes REMAINDER args after target name
    assert completion.resolve_candidates(parser, ["exec", "demo", "ls"]) == []

    # compose exec command takes REMAINDER args
    assert completion.resolve_candidates(parser, ["compose", "exec", "svc", "ls"]) == []

    # -- argument separator forces remainder mode
    assert completion.resolve_candidates(parser, ["run", "img", "--", "foo"]) == []


def test_unsupported_shell_is_rejected():
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


def test_interactive_bash_readline_tab_completion():
    if not shutil.which("bash") or os.name != "posix":
        pytest.skip("interactive bash pty testing requires POSIX bash")

    import pty
    import time

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

        def run_tab(setup_commands: str, typed_input: str) -> str:
            master, slave = pty.openpty()
            child_pid = os.fork()
            if child_pid == 0:
                os.close(master)
                os.setsid()
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                os.close(slave)
                os.environ["PS1"] = "PROMPT$ "
                os.environ["TERM"] = "dumb"
                os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
                os.environ["PYTHONPATH"] = f"{src_dir}:{os.environ.get('PYTHONPATH', '')}"
                os.execl("/bin/bash", "/bin/bash", "--norc", "-i")
            os.close(slave)
            time.sleep(0.1)
            os.read(master, 2048)
            os.write(master, setup_commands.encode() + b"\n")
            time.sleep(0.1)
            os.read(master, 2048)
            os.write(master, typed_input.encode() + b"\t")
            time.sleep(0.4)
            output_bytes = b""
            while True:
                try:
                    chunk = os.read(master, 1024)
                    if not chunk:
                        break
                    output_bytes += chunk
                    if len(chunk) < 1024:
                        break
                except OSError:
                    break
            os.write(master, b"\nexit\n")
            time.sleep(0.05)
            os.kill(child_pid, 9)
            os.close(master)
            return output_bytes.decode("utf-8", "replace")

        out1 = run_tab(f". {script_file}", "palimpsest run --backend=a")
        assert "palimpsest run --backend=auto " in out1

        out2 = run_tab(f". {script_file}", "palimpsest stor")
        assert "palimpsest store " in out2

        out3 = run_tab(f". {script_file}", "palimpsest run --memory 1024 ")
        assert "README.md" not in out3 and "pyproject.toml" not in out3

        mock_output = "output=$'__palimpsest_directive__\t4\nmy special&file$var.txt'"
        meta_script = script.replace(
            'output=$("${cmd:-palimpsest}" __complete --shell bash -- "${words[@]}" 2>/dev/null)',
            mock_output,
        )
        meta_file = tmp_path / "meta.bash"
        meta_file.write_text(meta_script)
        out4 = run_tab(f". {meta_file}", "palimpsest my")
        assert "my\\ special\\&file\\$var.txt " in out4 or "\\&" in out4


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


def test_cli_main_plain_completion(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["__complete", "--", "run", "--backend", "a"]) == 0
    assert capsys.readouterr().out.strip() == "auto"


def test_positional_values_do_not_offer_flags():
    parser = cli.build_parser()
    assert completion.resolve_candidates(parser, ["run", ""]) == []
    assert completion.resolve_candidates(parser, ["run", "image-prefix"]) == []
    assert completion.resolve_candidates(parser, ["run", "image", "--b"]) == ["--backend"]
    assert completion.resolve_candidates(parser, ["run", "image", "--", "--b"]) == []


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_shell_protocol_keeps_values_separate_from_descriptions(shell, capsys, monkeypatch):
    monkeypatch.setattr(completion, "_docker_output", lambda words: "run\tRun a container\n:4\n")
    assert cli.main(["__complete", "--shell", shell, "--", "docker", "r"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "__palimpsest_directive__\t4"
    assert lines[1:] == (["run"] if shell == "bash" else ["run\tRun a container"])


@pytest.mark.parametrize(
    "native", [":1\n", "--rm\tRemove container\n:64\n", "bad\n:999999999999999999999999\n", "bad\n"]
)
def test_native_errors_and_invalid_directives_are_not_candidates(native, monkeypatch):
    monkeypatch.setattr(completion, "_docker_output", lambda words: native)
    assert completion.resolve_candidates(cli.build_parser(), ["docker", ""]) == []


def test_native_values_descriptions_order_and_nospace(monkeypatch):
    native = "zeta\tZ value\nalpha\tA value\n:38\n"
    monkeypatch.setattr(completion, "_docker_output", lambda words: native)
    result = completion.resolve_completion(
        cli.build_parser(),
        [
            "docker",
            "plugin",
            "--mode=",
        ],
    )
    assert result.candidates == (
        completion.Candidate("--mode=zeta", "Z value"),
        completion.Candidate("--mode=alpha", "A value"),
    )
    assert result.directive == 38


def test_native_control_characters_cannot_escape_records(monkeypatch):
    monkeypatch.setattr(
        completion, "_docker_output", lambda words: "safe\tFirst\tsecond\nunsafe\x1b[0m\tterminal\n:4\n"
    )
    result = completion.resolve_completion(cli.build_parser(), ["docker", ""])
    assert result.candidates == (completion.Candidate("safe", "First second"),)


def test_native_file_directives_are_literal_and_filtered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "spec.yaml").write_text("fixture")
    (tmp_path / "spec.txt").write_text("fixture")
    (tmp_path / "space dir").mkdir()
    monkeypatch.setattr(completion, "_docker_output", lambda words: "yaml\n:8\n")
    assert completion.resolve_candidates(cli.build_parser(), ["docker", "plugin", "--file", "spec"]) == ["spec.yaml"]
    monkeypatch.setattr(completion, "_docker_output", lambda words: ":16\n")
    assert completion.resolve_candidates(cli.build_parser(), ["docker", "plugin", "s"]) == ["space dir/"]
    monkeypatch.setattr(completion, "_docker_output", lambda words: ":4\n")
    assert completion.resolve_candidates(cli.build_parser(), ["docker", "plugin", "s"]) == []


@pytest.fixture
def native_docker_fixture(tmp_path, monkeypatch):
    executable = tmp_path / "docker"

    def install(body):
        executable.write_text(f"#!{sys.executable}\n" + body)
        executable.chmod(0o700)

    monkeypatch.setenv("PATH", str(tmp_path))
    return install


def test_native_subprocess_preserves_plugin_globals_and_quoted_values(native_docker_fixture, tmp_path):
    marker = tmp_path / "must-not-exist"
    quoted = f"$(touch {marker})"
    # The fixture only returns completions in the actual native request context.
    native_docker_fixture(
        "import sys\n"
        f"expected = ['__complete', '--context', 'local name', 'buildx', 'build', '--build-arg', {quoted!r}, '--']\n"
        "assert sys.argv[1:] == expected\n"
        "print('--platform\\tTarget platform\\n:4')\n"
    )
    result = completion.resolve_completion(
        cli.build_parser(), ["docker", "--context", "local name", "buildx", "build", "--build-arg", quoted, "--"]
    )
    assert result.candidates == (completion.Candidate("--platform", "Target platform"),)
    assert not marker.exists()


@pytest.mark.parametrize(
    "body,diagnostic,deadline",
    [
        ("import time\ntime.sleep(30)\n", "timed out", 0.2),
        (
            "import sys,time\nsys.stdout.write('x' * 10000)\nsys.stdout.flush()\ntime.sleep(30)\n",
            "output exceeded",
            2.0,
        ),
    ],
)
def test_native_subprocess_is_bounded(body, diagnostic, deadline, native_docker_fixture, monkeypatch, capsys):
    native_docker_fixture(body)
    monkeypatch.setattr(completion, "_DOCKER_TIMEOUT", deadline)
    monkeypatch.setattr(completion, "_MAX_OUTPUT", 1024)
    assert completion.resolve_candidates(cli.build_parser(), ["docker", ""]) == []
    assert diagnostic in capsys.readouterr().err


def test_missing_docker_reports_actionable_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert completion.resolve_candidates(cli.build_parser(), ["docker", ""]) == []
    assert "Install Docker CLI" in capsys.readouterr().err


@pytest.fixture
def completion_home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home with spaces ' and $"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.mark.parametrize(
    "shell,relative",
    [
        ("bash", ".bashrc"),
        ("zsh", ".zshrc"),
        ("fish", ".config/fish/completions/palimpsest.fish"),
    ],
)
def test_install_creates_and_is_read_only_on_repeat(completion_home, shell, relative, monkeypatch):
    path = completion.install_completion(shell)
    assert path == completion_home / relative
    content = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = path.stat()

    # A repeated install must not even attempt publication or create a temp.
    def unexpected_write(*args, **kwargs):
        pytest.fail("Idempotent installation attempted a write")

    monkeypatch.setattr(completion.os, "write", unexpected_write)
    monkeypatch.setattr(completion.os, "replace", unexpected_write)
    monkeypatch.setattr(completion.os, "link", unexpected_write)
    assert completion.install_completion(shell) == path
    second = path.stat()
    assert (first.st_ino, first.st_mtime_ns, first.st_ctime_ns) == (
        second.st_ino,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )
    assert path.read_bytes() == content
    assert not list(path.parent.glob(".palimpsest-completion-*.tmp"))


def test_install_respects_shell_environment(completion_home, monkeypatch):
    zdotdir = completion_home / "zsh settings"
    xdg = completion_home / "xdg settings"
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert completion.install_completion("zsh") == zdotdir / ".zshrc"
    fish = completion.install_completion("fish")
    assert fish == xdg / "fish/completions/palimpsest.fish"
    for directory in (zdotdir, xdg, xdg / "fish", fish.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert completion.install_completion("bash") == completion_home / ".bashrc"
    assert not (completion_home / ".zshrc").exists()
    assert not (completion_home / ".config").exists()


@pytest.mark.parametrize(
    "variable,shell,value",
    [
        ("HOME", "bash", "relative"),
        ("HOME", "bash", ""),
        ("ZDOTDIR", "zsh", "relative"),
        ("ZDOTDIR", "zsh", ""),
        ("XDG_CONFIG_HOME", "fish", "relative"),
        ("ZDOTDIR", "zsh", "/somewhere/../elsewhere"),
        ("HOME", "bash", "/" + "/".join(["component"] * 65)),
    ],
)
def test_install_rejects_unsafe_environment(completion_home, monkeypatch, variable, shell, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(completion.CompletionInstallError, match="absolute|components|nonempty"):
        completion.install_completion(shell)
    assert not list(completion_home.iterdir())


def test_empty_xdg_uses_default(completion_home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert completion.install_completion("fish") == completion_home / ".config/fish/completions/palimpsest.fish"


@pytest.mark.parametrize("shell,filename", [("bash", ".bashrc"), ("zsh", ".zshrc")])
@pytest.mark.parametrize(
    "old", [b"", b"# existing\r\nexport MINE=yes\r\n", b"# nonutf8: \xff\n", b"export MINE=yes", b"# continued\\"]
)
def test_install_preserves_profile_bytes_and_permissions(completion_home, shell, filename, old):
    path = completion_home / filename
    path.write_bytes(old)
    path.chmod(0o640)
    completion.install_completion(shell)
    new = path.read_bytes()
    assert new.startswith(old)
    assert new.count(completion._COMPLETION_BEGIN) == 1
    assert new.count(completion._COMPLETION_END) == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    if old and not old.endswith(b"\n"):
        assert new[len(old) :].startswith(b"\n\n")
    # User edits OUTSIDE the block remain valid and unchanged on reinstall.
    changed = b"# custom header\n" + new + b"# custom footer\n"
    path.write_bytes(changed)
    completion.install_completion(shell)
    assert path.read_bytes() == changed


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("kind", ["edited", "missing-end", "missing-start", "duplicate", "inline", "obsolete"])
def test_install_preserves_edited_or_malformed_blocks(completion_home, monkeypatch, shell, kind):
    path = completion.install_completion(shell)
    content = path.read_bytes()
    if kind == "edited":
        content = content.replace(b"__complete", b"user-edited", 1)
    elif kind == "missing-end":
        content = content.replace(completion._COMPLETION_END, b"# removed")
    elif kind == "missing-start":
        content = content.replace(completion._COMPLETION_BEGIN, b"# removed")
    elif kind == "duplicate":
        content += content
    elif kind == "inline":
        content = b"echo " + content
    else:
        generator = completion.generate_completion_script
        monkeypatch.setattr(
            completion, "generate_completion_script", lambda selected: generator(selected) + "# newer version\n"
        )
    path.write_bytes(content)
    with pytest.raises(completion.CompletionInstallError, match="Preserving|review"):
        completion.install_completion(shell)
    assert path.read_bytes() == content


@pytest.mark.parametrize("content", [b"", b"# user completion\n", b"complete -c palimpsest\n"])
def test_fish_preserves_any_differing_existing_file(completion_home, content):
    path = completion_home / ".config/fish/completions/palimpsest.fish"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_bytes(content)
    with pytest.raises(completion.CompletionInstallError, match="Preserving.*Fish"):
        completion.install_completion("fish")
    assert path.read_bytes() == content


@pytest.mark.parametrize("dangling", [False, True])
def test_install_refuses_target_symlink(completion_home, dangling):
    other = completion_home / "other"
    if not dangling:
        other.write_bytes(b"untouched")
    target = completion_home / ".bashrc"
    target.symlink_to(other)
    with pytest.raises(completion.CompletionInstallError, match="unsafe|symlink"):
        completion.install_completion("bash")
    assert target.is_symlink()
    assert not other.exists() if dangling else other.read_bytes() == b"untouched"


@pytest.mark.parametrize("shell", ["zsh", "fish"])
def test_install_refuses_symlink_directory(completion_home, monkeypatch, shell):
    real = completion_home / "real"
    real.mkdir()
    alias = completion_home / "alias"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("ZDOTDIR" if shell == "zsh" else "XDG_CONFIG_HOME", str(alias / "new-child"))
    with pytest.raises(completion.CompletionInstallError, match="symlinks"):
        completion.install_completion(shell)
    assert not list(real.iterdir())


def test_install_refuses_shared_parent(completion_home):
    completion_home.chmod(0o777)
    with pytest.raises(completion.CompletionInstallError, match="Unsafe"):
        completion.install_completion("bash")
    assert not (completion_home / ".bashrc").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_install_bounds_combined_file_size(completion_home, monkeypatch, existing):
    path = completion_home / ".bashrc"
    if existing:
        path.write_bytes(b"# large profile\n" * 20)
    old = path.read_bytes() if existing else None
    monkeypatch.setattr(completion, "_COMPLETION_MAX_BYTES", 256)
    with pytest.raises(completion.CompletionInstallError, match="limit|exceed"):
        completion.install_completion("bash")
    assert path.read_bytes() == old if existing else not path.exists()
    assert not list(completion_home.glob(".palimpsest-completion-*.tmp"))


def test_install_bounds_combined_profile_and_script(completion_home, monkeypatch):
    path = completion_home / ".bashrc"
    path.write_bytes(b"#" * 200)
    monkeypatch.setattr(completion, "_COMPLETION_MAX_BYTES", 256)
    monkeypatch.setattr(completion, "generate_completion_script", lambda shell: "# short generated script\n")
    with pytest.raises(completion.CompletionInstallError, match="exceed"):
        completion.install_completion("bash")
    assert path.read_bytes() == b"#" * 200


def test_install_cleans_temporary_on_failed_write(completion_home, monkeypatch):
    path = completion_home / ".bashrc"
    path.write_bytes(b"# preserve me\n")

    def fail_write(*args):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(completion.os, "write", fail_write)
    with pytest.raises(completion.CompletionInstallError, match="No space.*retry"):
        completion.install_completion("bash")
    assert path.read_bytes() == b"# preserve me\n"
    assert not list(completion_home.glob(".palimpsest-completion-*.tmp"))


def test_install_detects_concurrent_profile_edit(completion_home, monkeypatch):
    path = completion_home / ".bashrc"
    path.write_bytes(b"# original\n")
    read = completion._completion_read
    calls = 0

    def change_before_final_read(directory, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"# concurrent edit\n")
        return read(directory, target)

    monkeypatch.setattr(completion, "_completion_read", change_before_final_read)
    with pytest.raises(completion.CompletionInstallError, match="changed"):
        completion.install_completion("bash")
    assert path.read_bytes() == b"# concurrent edit\n"
    assert not list(completion_home.glob(".palimpsest-completion-*.tmp"))


def test_install_does_not_clobber_racing_creation(completion_home, monkeypatch):
    path = completion_home / ".bashrc"
    actual_link = os.link

    def racing_link(source, destination, **kwargs):
        path.write_bytes(b"# won the race\n")
        return actual_link(source, destination, **kwargs)

    monkeypatch.setattr(completion.os, "link", racing_link)
    with pytest.raises(completion.CompletionInstallError, match="Cannot safely"):
        completion.install_completion("bash")
    assert path.read_bytes() == b"# won the race\n"
    assert not list(completion_home.glob(".palimpsest-completion-*.tmp"))


def test_install_invalid_shell_has_no_side_effects(completion_home):
    with pytest.raises(ValueError, match="unsupported shell"):
        completion.install_completion("powershell")
    assert not list(completion_home.iterdir())


def test_completion_cli_install_failure_is_actionable(completion_home, capsys):
    target = completion_home / ".bashrc"
    target.symlink_to(completion_home / "missing")
    assert cli.main(["completion", "bash", "--install"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert str(target) in captured.err
    assert "symlink" in captured.err
    assert not (completion_home / "missing").exists()


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_installed_scripts_parse(completion_home, shell):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} unavailable")
    path = completion.install_completion(shell)
    result = subprocess.run([executable, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_installed_bash_registers_in_interactive_shell(completion_home):
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("bash unavailable")
    path = completion.install_completion("bash")
    result = subprocess.run(
        [executable, "--noprofile", "--rcfile", str(path), "-ic", "complete -p palimpsest"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "_palimpsest_completion" in result.stdout


def test_installed_zsh_initializes_and_registers(completion_home):
    executable = shutil.which("zsh")
    if executable is None:
        pytest.skip("zsh unavailable")
    completion.install_completion("zsh")
    # Isolated HOME, no global rc; explicitly source installed profile while
    # interactive. Compinit may create a dump only inside the temporary HOME.
    result = subprocess.run(
        [executable, "-f", "-ic", 'source "$HOME/.zshrc"; print -r -- "${_comps[palimpsest]}"'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "_palimpsest"


def test_installed_fish_autoloads_completion(completion_home):
    executable = shutil.which("fish")
    if executable is None:
        pytest.skip("fish unavailable")
    completion.install_completion("fish")
    # A local executable supplies candidates; never invoke a host-installed
    # Palimpsest command while triggering Fish's normal completion autoload.
    bindir = completion_home / "bin"
    bindir.mkdir()
    command = bindir / "palimpsest"
    command.write_text(
        "#!/bin/sh\nprintf '%s\\n' '__palimpsest_directive__\t4' 'installed-probe\tInstalled candidate'\n"
    )
    command.chmod(0o700)
    environment = os.environ.copy()
    environment["PATH"] = str(bindir) + os.pathsep + environment.get("PATH", "")
    result = subprocess.run(
        [executable, "-c", "complete -C 'palimpsest '"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "installed-probe" in result.stdout


def test_install_rejects_oversized_profile_before_read(completion_home, monkeypatch):
    path = completion_home / ".bashrc"
    path.write_bytes(b"#" * 257)
    monkeypatch.setattr(completion, "_COMPLETION_MAX_BYTES", 256)
    monkeypatch.setattr(completion, "generate_completion_script", lambda shell: "# short\n")
    with pytest.raises(completion.CompletionInstallError, match="exceeds"):
        completion.install_completion("bash")
    assert path.read_bytes() == b"#" * 257


def test_install_detects_parent_replacement(completion_home, monkeypatch):
    read = completion._completion_read
    calls = 0
    moved = completion_home.with_name("moved-home")

    def swap_parent_during_final_read(directory, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            completion_home.rename(moved)
            completion_home.mkdir(mode=0o700)
        return read(directory, target)

    monkeypatch.setattr(completion, "_completion_read", swap_parent_during_final_read)
    with pytest.raises(completion.CompletionInstallError, match="Directory.*changed"):
        completion.install_completion("bash")
    assert not (completion_home / ".bashrc").exists()
    assert not (moved / ".bashrc").exists()
    assert not list(moved.glob(".palimpsest-completion-*.tmp"))


def test_install_rejects_zero_progress_write(completion_home, monkeypatch):
    monkeypatch.setattr(completion.os, "write", lambda fd, data: 0)
    with pytest.raises(completion.CompletionInstallError, match="no progress"):
        completion.install_completion("bash")
    assert not (completion_home / ".bashrc").exists()
    assert not list(completion_home.glob(".palimpsest-completion-*.tmp"))
