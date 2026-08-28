"""Shell completion candidate resolution and script generation for Palimpsest."""

from __future__ import annotations

import argparse
from typing import Sequence

SUPPORTED_SHELLS = ("zsh", "bash", "fish")


def _action_takes_arg(action: argparse.Action) -> bool:
    """Return True if action expects a value parameter."""
    if action.nargs == 0:
        return False
    if isinstance(
        action,
        (
            argparse._StoreTrueAction,
            argparse._StoreFalseAction,
            argparse._HelpAction,
            argparse._VersionAction,
            argparse._CountAction,
        ),
    ):
        return False
    return True


def resolve_candidates(parser: argparse.ArgumentParser, words: Sequence[str]) -> list[str]:
    """Resolve completion candidates for words after executable.

    words includes the partial word being completed at words[-1].
    """
    word_list = list(words) if words is not None and len(words) > 0 else [""]

    prefix = word_list[-1]
    preceding = word_list[:-1]

    curr_parser = parser
    expecting_option: argparse.Action | None = None
    in_remainder = False

    pos_actions = [
        a for a in curr_parser._actions
        if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
    ]
    pos_idx = 0

    for w in preceding:
        if in_remainder:
            break

        if expecting_option is not None:
            expecting_option = None
            continue

        if w == "--":
            in_remainder = True
            break

        if w.startswith("-"):
            if "=" in w:
                opt_name = w.split("=", 1)[0]
                found_action = None
                for action in curr_parser._actions:
                    if opt_name in action.option_strings:
                        found_action = action
                        break
                continue

            found_action = None
            for action in curr_parser._actions:
                if w in action.option_strings:
                    found_action = action
                    break

            if found_action is not None and _action_takes_arg(found_action):
                expecting_option = found_action
            continue

        subparser_action = None
        for action in curr_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparser_action = action
                break

        if subparser_action is not None and w in subparser_action.choices:
            curr_parser = subparser_action.choices[w]
            pos_actions = [
                a for a in curr_parser._actions
                if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
            ]
            pos_idx = 0
            continue

        if pos_idx < len(pos_actions):
            current_pos_action = pos_actions[pos_idx]
            if current_pos_action.nargs == argparse.REMAINDER:
                in_remainder = True
                break
            if current_pos_action.nargs not in ("*", "+"):
                pos_idx += 1
        elif pos_actions and pos_actions[-1].nargs == argparse.REMAINDER:
            in_remainder = True
            break

    if not in_remainder and expecting_option is None:
        if pos_idx < len(pos_actions) and pos_actions[pos_idx].nargs == argparse.REMAINDER:
            in_remainder = True

    if in_remainder:
        return []

    if expecting_option is not None:
        if expecting_option.choices:
            return sorted([str(c) for c in expecting_option.choices if str(c).startswith(prefix)])
        return []

    if prefix.startswith("-") and "=" in prefix:
        opt_name, val_prefix = prefix.split("=", 1)
        found_action = None
        for action in curr_parser._actions:
            if opt_name in action.option_strings:
                found_action = action
                break

        if found_action is not None and found_action.choices:
            return sorted([f"{opt_name}={c}" for c in found_action.choices if str(c).startswith(val_prefix)])
        return []

    candidates: list[str] = []

    subparser_action = None
    for action in curr_parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparser_action = action
            break

    if subparser_action is not None:
        for choice_name in subparser_action.choices.keys():
            if choice_name.startswith(prefix):
                candidates.append(choice_name)

    parsers_to_check = [curr_parser]
    for p in parsers_to_check:
        for action in p._actions:
            for opt_str in action.option_strings:
                if opt_str.startswith(prefix):
                    candidates.append(opt_str)

    if pos_idx < len(pos_actions):
        pos_a = pos_actions[pos_idx]
        if pos_a.choices:
            for c in pos_a.choices:
                str_c = str(c)
                if str_c.startswith(prefix):
                    candidates.append(str_c)

    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)

    return sorted(result)


def generate_completion_script(shell: str) -> str:
    """Generate shell completion script for zsh, bash, or fish."""
    if shell == "zsh":
        return """#compdef palimpsest

_palimpsest() {
    emulate -L zsh
    local -a raw_candidates
    local -a candidates
    raw_candidates=("${(@f)$("${words[1]:-palimpsest}" __complete -- "${words[@]:1:$((CURRENT-1))}")}")
    for c in "${raw_candidates[@]}"; do
        if [[ -n "$c" ]]; then
            candidates+=("$c")
        fi
    done
    if (( ${#candidates[@]} > 0 )); then
        compadd -U -a candidates
    else
        compstate[insert]=""
    fi
    return 0
}

compdef _palimpsest palimpsest
"""
    elif shell == "bash":
        return """_palimpsest_completion() {
    local words=()
    local cword=$COMP_CWORD
    local i=1
    while (( i <= cword )); do
        local w="${COMP_WORDS[i]}"
        if (( i < cword )) && [[ "${COMP_WORDS[i+1]}" == "=" ]]; then
            if (( i + 1 == cword )); then
                words+=("${w}=")
                i=$((i + 2))
            else
                words+=("${w}=${COMP_WORDS[i+2]}")
                i=$((i + 3))
            fi
        else
            words+=("$w")
            i=$((i + 1))
        fi
    done

    local cmd="${COMP_WORDS[0]}"
    local cur="${COMP_WORDS[cword]}"
    local prev=""
    if (( cword > 0 )); then
        prev="${COMP_WORDS[cword-1]}"
    fi

    COMPREPLY=()
    local raw_candidates
    raw_candidates=$("${cmd:-palimpsest}" __complete -- "${words[@]}")

    while IFS= read -r line; do
        if [[ -n "$line" ]]; then
            if [[ "$prev" == "=" || "$prev" == *"=" ]]; then
                COMPREPLY+=("${line#*=}")
            elif [[ "$cur" == "=" ]]; then
                if [[ "$line" == "${prev}="* ]]; then
                    COMPREPLY+=("${line#${prev}}")
                else
                    COMPREPLY+=("${line#*=}")
                fi
            else
                COMPREPLY+=("$line")
            fi
        fi
    done <<< "$raw_candidates"
}

complete -F _palimpsest_completion palimpsest
"""
    elif shell == "fish":
        return """function __palimpsest_completion
    set -l tokens (commandline -xpc)
    if test (count $tokens) -eq 0
        return
    end
    set -l cmd $tokens[1]
    set -e tokens[1]
    set -l curr (commandline -ct)
    if test (count $curr) -eq 0
        set curr ""
    end
    command $cmd __complete -- $tokens $curr
end

complete -c palimpsest -f -a "(__palimpsest_completion)"
"""
    else:
        raise ValueError(f"unsupported shell: {shell}")
