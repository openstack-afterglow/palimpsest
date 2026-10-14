"""Shell-safe completion, using argparse for Palimpsest and Docker's native protocol.

The internal shell protocol is a directive header followed by literal value<TAB>help
records. Neither input words nor output records are shell programs. Docker's file
filters are expanded here, not interpolated into shell patterns or evaluated.
"""

from __future__ import annotations

import argparse
import os
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import PalimpsestError

SUPPORTED_SHELLS = ("zsh", "bash", "fish")
_DOCKER_TIMEOUT = 2.0
_MAX_OUTPUT = 1024 * 1024
_MAX_FILES = 4096
_ERROR, _NO_SPACE, _NO_FILES, _FILE_EXT, _DIRS, _KEEP_ORDER = 1, 2, 4, 8, 16, 32
_PROTOCOL_HEADER = "__palimpsest_directive__"


@dataclass(frozen=True)
class Candidate:
    value: str
    description: str = ""


@dataclass(frozen=True)
class CompletionResult:
    candidates: tuple[Candidate, ...] = ()
    directive: int = _NO_FILES


def _safe_text(value: str) -> bool:
    return bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _description(value: str | None) -> str:
    if value == argparse.SUPPRESS:
        return ""
    literal = "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in value or "")
    return " ".join(literal.split())


def _action_takes_arg(action: argparse.Action) -> bool:
    return action.nargs != 0


def _docker_output(words: Sequence[str]) -> str | None:
    """Bound runtime, captured bytes and the whole Docker/plugin process group."""
    from .registry import credential_free_subprocess_environment

    environment = credential_free_subprocess_environment()
    environment["DOCKER_ACTIVE_HELP"] = "0"
    environment.pop("DOCKER_CLI_DISABLE_COMPLETION_DESCRIPTION", None)
    try:
        process = subprocess.Popen(
            ["docker", "__complete", *words],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"Docker completion unavailable: {exc}. Install Docker CLI and put docker on PATH.", file=sys.stderr)
        return None

    assert process.stdout is not None
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + _DOCKER_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Docker completion timed out")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_OUTPUT:
                    raise ValueError("Docker completion output exceeded its limit")
                chunks.append(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Docker completion timed out")
        if process.wait(timeout=remaining) != 0:
            raise ValueError("Docker completion failed; use a Docker CLI supporting __complete")
        return b"".join(chunks).decode("utf-8", errors="replace")
    except (OSError, TimeoutError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"{exc}; try palimpsest docker --help.", file=sys.stderr)
        return None
    finally:
        # A plugin can outlive Docker, even after Docker closed its stdout.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.stdout.close()
        process.wait()


def _file_candidates(
    prefix: str, *, extensions: Sequence[str] = (), directories: bool = False, root: str = ""
) -> tuple[Candidate, ...]:
    """Expand native file directives without treating filenames as shell syntax."""
    directory, partial = os.path.split(prefix)
    search = Path(root or ".") / os.path.expanduser(directory or ".")
    result: list[Candidate] = []
    try:
        with os.scandir(search) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_FILES:
                    break
                if not entry.name.startswith(partial) or (entry.name.startswith(".") and not partial.startswith(".")):
                    continue
                is_dir = entry.is_dir()
                if directories and not is_dir:
                    continue
                if (
                    extensions
                    and not is_dir
                    and not any(entry.name.endswith("." + ext.lstrip(".")) for ext in extensions)
                ):
                    continue
                value = os.path.join(directory, entry.name) + ("/" if is_dir else "")
                if _safe_text(value):
                    result.append(Candidate(value, "Directory" if is_dir else "File"))
    except OSError:
        return ()
    return tuple(sorted(result, key=lambda item: item.value))


def _docker_completion(words: Sequence[str]) -> CompletionResult:
    output = _docker_output(words)
    if output is None:
        return CompletionResult()
    lines = output.rstrip("\n").split("\n")
    header = lines.pop() if lines else ""
    if not header.startswith(":") or len(header) > 3 or not header[1:].isascii() or not header[1:].isdigit():
        print(
            "Docker returned no native completion directive; install a Docker CLI supporting __complete.",
            file=sys.stderr,
        )
        return CompletionResult()
    directive = int(header[1:])
    if directive & _ERROR or directive & ~63 or directive & _FILE_EXT and directive & _DIRS:
        return CompletionResult()
    candidates = []
    for line in lines:
        value, _, description = line.partition("\t")
        if _safe_text(value):
            candidates.append(Candidate(value, _description(description)))
    prefix = words[-1] if words else ""
    # Cobra returns only the value for --flag=value completions.
    option_prefix = ""
    if prefix.startswith("-") and "=" in prefix:
        option_prefix, prefix = prefix.split("=", 1)
        option_prefix += "="
    if directive & _FILE_EXT:
        candidates = list(_file_candidates(prefix, extensions=[item.value for item in candidates]))
    elif directive & _DIRS:
        candidates = list(_file_candidates(prefix, directories=True, root=candidates[0].value if candidates else ""))
    else:
        candidates = [item for item in candidates if item.value.startswith(prefix)]
        if not candidates and not directive & _NO_FILES:
            candidates = list(_file_candidates(prefix))
    if option_prefix:
        candidates = [Candidate(option_prefix + item.value, item.description) for item in candidates]
    # Keep records literal and unique; do not expose native directives as candidates.
    unique = {item.value: item for item in candidates}
    candidates = list(unique.values())
    if not directive & _KEEP_ORDER:
        candidates.sort(key=lambda item: item.value)
    if candidates and all(item.value.endswith("/") for item in candidates):
        directive |= _NO_SPACE
    return CompletionResult(tuple(candidates), (directive & (_NO_SPACE | _KEEP_ORDER)) | _NO_FILES)


def resolve_completion(parser: argparse.ArgumentParser, words: Sequence[str]) -> CompletionResult:
    """Resolve words after the executable, including the current (possibly empty) word."""
    word_list = list(words) if words else [""]
    prefix = word_list[-1]
    curr_parser = parser
    expecting_option: argparse.Action | None = None
    pos_idx = 0

    def positions() -> list[argparse.Action]:
        return [
            a for a in curr_parser._actions if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
        ]

    pos_actions = positions()
    for index, word in enumerate(word_list[:-1]):
        if expecting_option is not None:
            expecting_option = None
            continue
        if word == "--":
            return CompletionResult()
        if word.startswith("-"):
            option, separator, _ = word.partition("=")
            action = next((a for a in curr_parser._actions if option in a.option_strings), None)
            if action is not None and not separator and _action_takes_arg(action):
                expecting_option = action
            continue
        subparsers = next((a for a in curr_parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if subparsers is not None and word in subparsers.choices:
            if curr_parser is parser and word == "docker":
                return _docker_completion(word_list[index + 1 :])
            curr_parser = subparsers.choices[word]
            pos_actions = positions()
            pos_idx = 0
            continue
        if pos_idx < len(pos_actions):
            action = pos_actions[pos_idx]
            if action.nargs == argparse.REMAINDER:
                return CompletionResult()
            if action.nargs not in ("*", "+"):
                pos_idx += 1

    def choices(action: argparse.Action, value_prefix: str, option_prefix: str = "") -> CompletionResult:
        return CompletionResult(
            tuple(
                Candidate(option_prefix + str(value), _description(action.help))
                for value in sorted(action.choices or (), key=str)
                if str(value).startswith(value_prefix)
            )
        )

    if expecting_option is not None:
        return choices(expecting_option, prefix)
    if pos_idx < len(pos_actions) and pos_actions[pos_idx].nargs == argparse.REMAINDER:
        return CompletionResult()
    if prefix.startswith("-") and "=" in prefix:
        option, value_prefix = prefix.split("=", 1)
        action = next((a for a in curr_parser._actions if option in a.option_strings), None)
        return choices(action, value_prefix, option + "=") if action else CompletionResult()

    candidates: dict[str, Candidate] = {}
    for action in curr_parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            summaries = {choice.dest: choice.help for choice in action._choices_actions}
            for name, subparser in action.choices.items():
                if name.startswith(prefix):
                    candidates[name] = Candidate(name, _description(summaries.get(name) or subparser.description))
        # An unfinished positional value is not an option name. Explicit '-' still
        # completes options; an empty word shows options only outside that position.
        elif prefix.startswith("-") or not prefix and pos_idx >= len(pos_actions):
            for option in action.option_strings:
                if option.startswith(prefix) and action.help != argparse.SUPPRESS:
                    candidates[option] = Candidate(option, _description(action.help))
    if pos_idx < len(pos_actions):
        for item in choices(pos_actions[pos_idx], prefix).candidates:
            candidates[item.value] = item
    return CompletionResult(tuple(candidates[name] for name in sorted(candidates)))


def resolve_candidates(parser: argparse.ArgumentParser, words: Sequence[str]) -> list[str]:
    """Compatibility interface returning literal values, without descriptions."""
    return [item.value for item in resolve_completion(parser, words).candidates]


def print_completion(parser: argparse.ArgumentParser, words: Sequence[str], shell: str | None = None) -> None:
    if shell is not None and shell not in SUPPORTED_SHELLS:
        raise ValueError(f"unsupported shell: {shell}")
    result = resolve_completion(parser, words)
    if shell is not None:
        print(f"{_PROTOCOL_HEADER}\t{result.directive}")
    for item in result.candidates:
        if _safe_text(item.value):
            print(item.value + ("\t" + item.description if shell in ("zsh", "fish") else ""))


def generate_completion_script(shell: str) -> str:
    """Generate registrations; words and descriptions are always literal shell data."""
    if shell == "zsh":
        return r"""#compdef palimpsest

_palimpsest() {
    emulate -L zsh
    local -a records candidates descriptions suffix grouping
    local record value description directive=4
    records=("${(@f)$("${(Q)words[1]:-palimpsest}" __complete --shell zsh -- "${(@Q)words[2,CURRENT]}" 2>/dev/null)}")
    if [[ "${records[1]}" != $'__palimpsest_directive__\t'* ]]; then
        compstate[insert]=""
        return 0
    fi
    directive=${records[1]#*$'\t'}
    [[ "$directive" == <-> ]] || return 0
    for record in "${records[@]:1}"; do
        value=${record%%$'\t'*}
        description=${record#*$'\t'}
        [[ -n "$value" ]] || continue
        candidates+=("$value")
        descriptions+=("$value -- $description")
    done
    (( directive & 2 )) && suffix=(-S '')
    grouping=(-J palimpsest)
    (( directive & 32 )) && grouping=(-V palimpsest)
    if (( ${#candidates[@]} > 0 )); then
        compadd -U "${suffix[@]}" "${grouping[@]}" -d descriptions -a candidates
    else
        compstate[insert]=""
    fi
    return 0
}

compdef _palimpsest palimpsest
"""
    if shell == "bash":
        return r"""_palimpsest_completion() {
    local cword=$COMP_CWORD
    local raw_words=("${COMP_WORDS[@]:0:$((cword + 1))}")
    local line="${COMP_LINE:0:$COMP_POINT}"
    local words=()
    local i=1 j=0 token="" prev_idx=0 prev_w="" clean=""

    for (( i = 1; i <= cword; i++ )); do
        token="${raw_words[i]}"
        if (( j > 0 )); then
            prev_idx=$((j - 1))
            prev_w="${words[prev_idx]}"
            if [[ "$token" == [=:] || "$prev_w" == *[=:] ]]; then
                if [[ -z "$line" || ! "$line" =~ [[:space:]]${token} ]]; then
                    words[prev_idx]="${prev_w}${token}"
                    continue
                fi
            fi
        fi
        clean="$token"
        if [[ "$clean" =~ ^\"(.*)\"$ || "$clean" =~ ^\'(.*)\'$ ]]; then
            clean="${BASH_REMATCH[1]}"
        fi
        words[j]="$clean"
        (( j++ ))
    done
    if [[ -n "$line" && "$line" =~ [[:space:]]$ ]]; then
        words+=("")
    fi

    local n_words=${#words[@]}
    local cur="" prev=""
    if (( n_words > 0 )); then
        cur="${words[$((n_words - 1))]}"
    fi
    if (( n_words > 1 )); then
        prev="${words[$((n_words - 2))]}"
    fi

    local cmd="${COMP_WORDS[0]}"
    COMPREPLY=()
    local output line_item directive=4 first=1
    output=$("${cmd:-palimpsest}" __complete --shell bash -- "${words[@]}" 2>/dev/null)

    while IFS= read -r line_item; do
        if (( first )); then
            first=0
            [[ "$line_item" == $'__palimpsest_directive__\t'* ]] || return 0
            directive=${line_item#*$'\t'}
            [[ "$directive" != *[!0-9]* && -n "$directive" ]] || return 0
            continue
        fi
        [[ -n "$line_item" ]] || continue

        local val="${line_item%%$'\t'*}"
        local escaped
        if [[ "$val" == -*=* ]]; then
            local opt_pfx="${val%%=*}="
            local opt_val="${val#*=}"
            printf -v escaped "%q" "$opt_val"
            escaped="${opt_pfx}${escaped}"
        else
            printf -v escaped "%q" "$val"
        fi
        COMPREPLY+=("$escaped")
    done <<< "$output"

    if [[ "$cur" == *=* && "$COMP_WORDBREAKS" == *=* ]]; then
        local eq_prefix="${cur%%"${cur##*=}"}"
        local idx=${#COMPREPLY[*]}
        while ((--idx >= 0)); do
            COMPREPLY[idx]="${COMPREPLY[idx]#"$eq_prefix"}"
        done
    fi
    if [[ "$cur" == *:* && "$COMP_WORDBREAKS" == *:* ]]; then
        local colon_prefix="${cur%%"${cur##*:}"}"
        local idx=${#COMPREPLY[*]}
        while ((--idx >= 0)); do
            COMPREPLY[idx]="${COMPREPLY[idx]#"$colon_prefix"}"
        done
    fi

    if [[ $(type -t compopt) = "builtin" ]]; then
        if (( directive & 2 )); then
            compopt -o nospace 2>/dev/null || :
        else
            compopt +o nospace 2>/dev/null || :
        fi
        if (( directive & 32 )); then
            compopt -o nosort 2>/dev/null || :
        fi
    else
        if (( ${#COMPREPLY[@]} == 1 && !(directive & 2) )); then
            COMPREPLY[0]+=" "
        fi
    fi
}

if [[ $(type -t compopt) = "builtin" ]]; then
    complete -F _palimpsest_completion palimpsest
else
    complete -o nospace -F _palimpsest_completion palimpsest
fi
"""
    if shell == "fish":
        return r"""function __palimpsest_completion
    set -l tokens (commandline -xpc)
    if test (count $tokens) -eq 0
        return
    end
    set -l cmd $tokens[1]
    set -e tokens[1]
    # Cut first, then unescape even unfinished quotes without evaluating them.
    set -l raw (commandline -ct)
    set -l curr (commandline -I "$raw" -ot)
    if test (count $curr) -eq 0
        set curr ""
    end
    set -l records (command $cmd __complete --shell fish -- $tokens "$curr" 2>/dev/null)
    if test (count $records) -eq 0
        return
    end
    set -l header (string split \t -- $records[1])
    if test (count $header) -ne 2; or test "$header[1]" != __palimpsest_directive__
        return
    end
    if not string match -qr '^[0-9]+$' -- $header[2]
        return
    end
    set -e records[1]
    if test (count $records) -eq 1; and test (math "floor($header[2] / 2) % 2") -eq 1
        # Native Cobra's fish strategy: two matches retain the common prefix
        # without inserting a space; no extra candidate for punctuation endings.
        set -l value (string split -m 1 \t -- $records[1])[1]
        if not string match -qr '[@=/:.,]$' -- "$value"
            set -a records "$value."
        end
    end
    if test (count $records) -gt 0
        printf '%s\n' $records
    end
end

complete -c palimpsest -f -k -a "(__palimpsest_completion)"
"""
    raise ValueError(f"unsupported shell: {shell}")


_COMPLETION_MAX_BYTES = 1024 * 1024
_COMPLETION_BEGIN = b"# >>> palimpsest completion >>>"
_COMPLETION_END = b"# <<< palimpsest completion <<<"


class CompletionInstallError(PalimpsestError):
    """An opt-in completion installation could not safely proceed."""


def _completion_absolute(raw: str, label: str) -> Path:
    if not raw or "\0" in raw or len(os.fsencode(raw)) > 4096:
        raise CompletionInstallError(f"{label} must be a nonempty absolute filesystem path.")
    path = Path(raw)
    if not path.is_absolute() or path.anchor != "/" or ".." in path.parts or len(path.parts) > 65:
        raise CompletionInstallError(f"{label} must be an absolute path without '..' (at most 64 components).")
    return path


def _completion_target(shell: str) -> Path:
    if shell == "zsh" and "ZDOTDIR" in os.environ:
        return _completion_absolute(os.environ["ZDOTDIR"], "ZDOTDIR") / ".zshrc"
    if shell == "fish" and os.environ.get("XDG_CONFIG_HOME"):
        return (
            _completion_absolute(os.environ["XDG_CONFIG_HOME"], "XDG_CONFIG_HOME") / "fish/completions/palimpsest.fish"
        )
    try:
        raw_home = os.environ.get("HOME")
        home = _completion_absolute(str(Path.home()) if raw_home is None else raw_home, "HOME")
    except (RuntimeError, OSError) as exc:
        raise CompletionInstallError(
            "Cannot determine HOME; set HOME to your physical absolute home directory."
        ) from exc
    if shell == "bash":
        return home / ".bashrc"
    if shell == "zsh":
        return home / ".zshrc"
    return home / ".config/fish/completions/palimpsest.fish"


def _completion_directory_safe(info: os.stat_result, *, final: bool = False) -> bool:
    if not stat.S_ISDIR(info.st_mode):
        return False
    uid = os.geteuid()
    if final:
        return info.st_uid == uid and not info.st_mode & 0o022
    sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    return info.st_uid in (0, uid) and (not info.st_mode & 0o022 or sticky_root)


def _completion_check_chain(fds: list[int], names: tuple[str, ...], path: Path) -> None:
    for index, fd in enumerate(fds):
        held = os.fstat(fd)
        if not _completion_directory_safe(held, final=index == len(fds) - 1):
            raise CompletionInstallError(
                f"Unsafe directory for {path}; use user-owned, non-shared-writable directories."
            )
        if index:
            visible = os.stat(names[index - 1], dir_fd=fds[index - 1], follow_symlinks=False)
            if not stat.S_ISDIR(visible.st_mode) or (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino):
                raise CompletionInstallError(
                    f"Directory for {path} changed during installation; stop concurrent changes and retry."
                )


@contextmanager
def _completion_directory(path: Path) -> Iterator[tuple[list[int], tuple[str, ...]]]:
    # Keep the complete chain pinned so neither symlink ancestors nor renamed
    # components silently redirect the publication to an unchecked directory.
    names = path.parts[1:]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds = [os.open("/", flags)]
    try:
        for name in names:
            parent = fds[-1]
            if not _completion_directory_safe(os.fstat(parent)):
                raise CompletionInstallError(
                    f"Unsafe parent of {path}; use non-shared-writable directories owned by you or root."
                )
            try:
                child = os.open(name, flags, dir_fd=parent)
            except FileNotFoundError:
                if not _completion_directory_safe(os.fstat(parent), final=True):
                    raise CompletionInstallError(
                        f"Will not create {path} below a shared or foreign-owned directory; create it safely first."
                    ) from None
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(name, flags, dir_fd=parent)
            fds.append(child)
        _completion_check_chain(fds, names, path)
        yield fds, names
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _completion_stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _completion_file_safe(info: os.stat_result, path: Path) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or not info.st_mode & stat.S_IWUSR
    ):
        raise CompletionInstallError(
            f"Refusing unsafe completion target {path}; it must be your writable regular file, "
            "not a symlink, hardlink, special file or group/other-writable file. Review it manually."
        )
    if info.st_size > _COMPLETION_MAX_BYTES:
        raise CompletionInstallError(f"{path} exceeds the 1 MiB installation limit; install completion manually.")


def _completion_read(directory: int, path: Path) -> tuple[bytes, os.stat_result | None]:
    try:
        before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return b"", None
    _completion_file_safe(before, path)
    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        held = os.fstat(fd)
        _completion_file_safe(held, path)
        if _completion_stamp(held) != _completion_stamp(before):
            raise CompletionInstallError(f"{path} changed while opening; stop concurrent edits and retry.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(_COMPLETION_MAX_BYTES + 1)
        after = os.fstat(fd)
        visible = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if len(content) > _COMPLETION_MAX_BYTES:
            raise CompletionInstallError(f"{path} exceeds the 1 MiB installation limit; install completion manually.")
        if (
            len(content) != held.st_size
            or _completion_stamp(after) != _completion_stamp(held)
            or _completion_stamp(visible) != _completion_stamp(held)
        ):
            raise CompletionInstallError(f"{path} changed while reading; stop concurrent edits and retry.")
        return content, held
    finally:
        os.close(fd)


def _completion_content(shell: str, script: str, old: bytes, path: Path) -> bytes:
    if shell == "fish":
        desired = (
            "# Palimpsest generated completion; remove this file to reinstall after reviewing edits.\n" + script
        ).encode("utf-8")
        if old and old != desired:
            raise CompletionInstallError(
                f"Preserving existing Fish completion {path}; review and move/remove it before reinstalling."
            )
        return desired
    if shell == "bash":
        body = "if [[ $- == *i* ]]; then\n" + script + "fi\n"
    else:
        body = (
            "if [[ -o interactive ]]; then\n"
            "    if (( ! $+functions[compdef] )); then\n"
            "        autoload -Uz compinit && compinit\n"
            "    fi\n"
            "    if (( $+functions[compdef] )); then\n" + script + "    fi\nfi\n"
        )
    block = _COMPLETION_BEGIN + b"\n" + body.encode("utf-8") + _COMPLETION_END + b"\n"
    if _COMPLETION_BEGIN in old or _COMPLETION_END in old:
        if old.count(_COMPLETION_BEGIN) == old.count(_COMPLETION_END) == 1 and old.count(block) == 1:
            start = old.index(block)
            if start == 0 or old[start - 1 : start] == b"\n":
                return old
        raise CompletionInstallError(
            f"Preserving edited, outdated or malformed completion block in {path}; "
            "review and remove the Palimpsest marker block before reinstalling."
        )
    # Always add a blank line: a trailing backslash in an unterminated final
    # profile line must not escape the first marker into user shell code.
    separator = b"\n\n" if old and not old.endswith(b"\n") else b"\n" if old else b""
    return old + separator + block


def install_completion(shell: str) -> Path:
    """Install generated completion explicitly, returning its absolute path.

    Raise ValueError for an unsupported shell, CompletionInstallError for a
    safety conflict or filesystem failure. Never overwrite differing managed
    content; preserve all unrelated profile bytes.
    """
    if shell not in SUPPORTED_SHELLS:
        raise ValueError(f"unsupported shell: {shell}")
    if os.name != "posix":
        raise CompletionInstallError(
            "Automatic completion installation requires POSIX; use 'palimpsest completion SHELL' manually."
        )
    script = generate_completion_script(shell)
    if len(script) > _COMPLETION_MAX_BYTES:
        raise CompletionInstallError("Generated completion exceeds the 1 MiB installation limit; install manually.")
    path = _completion_target(shell)
    _completion_absolute(str(path), "Completion target")
    try:
        with _completion_directory(path.parent) as (fds, names):
            directory = fds[-1]
            old, before = _completion_read(directory, path)
            # An existing empty Fish file is still user content, not ours.
            if shell == "fish" and before is not None and not old:
                raise CompletionInstallError(
                    f"Preserving existing empty Fish completion {path}; review and move/remove it before reinstalling."
                )
            desired = _completion_content(shell, script, old, path)
            if len(desired) > _COMPLETION_MAX_BYTES:
                raise CompletionInstallError(f"Installing into {path} would exceed 1 MiB; install completion manually.")
            if before is not None and desired == old:
                return path
            temporary = f".palimpsest-completion-{secrets.token_hex(16)}.tmp"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                view = memoryview(desired)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("Completion temporary write made no progress")
                    view = view[written:]
                os.fchmod(fd, stat.S_IMODE(before.st_mode) if before is not None else 0o600)
                os.fsync(fd)
                current, now = _completion_read(directory, path)
                if current != old or (None if now is None else _completion_stamp(now)) != (
                    None if before is None else _completion_stamp(before)
                ):
                    raise CompletionInstallError(
                        f"{path} changed before installation; stop concurrent edits and retry."
                    )
                _completion_check_chain(fds, names, path.parent)
                if before is None:
                    # Atomic no-clobber publication; even a racing dangling
                    # symlink causes EEXIST rather than being followed/replaced.
                    os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=directory)
                    temporary = ""
                else:
                    os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                    temporary = ""
                os.fsync(directory)
            finally:
                os.close(fd)
                if temporary:
                    os.unlink(temporary, dir_fd=directory)
        return path
    except OSError as exc:
        raise CompletionInstallError(
            f"Cannot safely install completion at {path}: {exc.strerror or exc}. "
            "Check permissions and physical directory paths; symlinks and special files are not followed. "
            "After resolving the problem, retry 'palimpsest completion " + shell + " --install'."
        ) from exc
