"""Strict, deterministic ``palimpsest.yml`` project configuration.

The project file intentionally supports a small YAML 1.2-shaped subset instead of
depending on a general YAML object deserializer.  Maps, sequences, quoted/plain
scalars, JSON-style flow collections, and literal (``|``/``|-``) blocks are
supported.  Tags, anchors, aliases, merge keys, directives, implicit timestamps,
and arbitrary Python objects are rejected.

The public entry point is :func:`load_project`.  It returns frozen model objects;
:func:`canonical_project_payload` and :func:`canonical_project_json` provide the
stable, relative-path-only representation suitable for ``config`` output and
hashing.  Environment templates are kept as templates in that representation so
resolved credentials are never copied into project state.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

from .digest import InvalidDigestError, require_digest
from .errors import PalimpsestError

DEFAULT_PROJECT_FILE = "palimpsest.yml"
PROJECT_SCHEMA_VERSION = "1"
MAX_PROJECT_BYTES = 1024 * 1024
MAX_YAML_DEPTH = 32
MAX_YAML_NODES = 10_000
MAX_ENV_FILE_BYTES = 256 * 1024

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLAIN_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_INTERPOLATION_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<operator>:-|:\?)(?P<operand>[^{}$]*))?$")
_PURE_INTERPOLATION_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::\?[^{}$]*)?\}$")
_MEMORY_RE = re.compile(r"^(?P<number>[1-9][0-9]*)(?P<unit>MiB|GiB)$", re.IGNORECASE)
_SECRET_WORD_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:password|passwd|token|secret|private[_-]?key|api[_-]?key|credential)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:ghp|github_pat|glpat|xox[baprs])-[_A-Za-z0-9-]{12,}\b"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(
        r"(?:^|[\s,{;])(?:password|passwd|token|secret|private[_-]?key|api[_-]?key|credential)"
        r"\s*[:=]\s*(?!\$\{)(?=[^\s#])",
        re.IGNORECASE | re.MULTILINE,
    ),
)


class ProjectError(PalimpsestError):
    """A ``palimpsest.yml`` file is malformed, unsafe, or unsupported."""


class _NoInterpolationString(str):
    """A single-quoted YAML/dotenv scalar with Compose-style literal dollars."""


@dataclass(frozen=True)
class ProjectFile:
    """A project-root-relative, symlink-free file or directory reference."""

    reference: str
    path: Path
    content_sha256: str | None = None


@dataclass(frozen=True)
class NetworkSpec:
    name: str
    driver: Literal["nat", "bridge", "isolated"] = "nat"
    external: bool = False
    external_name: str | None = None


@dataclass(frozen=True)
class VolumeSpec:
    name: str
    driver: Literal["block"] = "block"
    size_mib: int = 10 * 1024
    external: bool = False
    external_name: str | None = None


@dataclass(frozen=True)
class MountSpec:
    type: Literal["volume", "bind"]
    source: str
    target: str
    read_only: bool = False
    source_path: Path | None = None


@dataclass(frozen=True)
class PortSpec:
    host_port: int
    guest_port: int
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str = "127.0.0.1"


@dataclass(frozen=True)
class CloudInitWriteFile:
    path: str
    content: str
    permissions: str = "0644"


@dataclass(frozen=True)
class CloudInitSpec:
    """Backend-neutral cloud-init subset; raw user-data is deliberately excluded."""

    packages: tuple[str, ...] = ()
    write_files: tuple[CloudInitWriteFile, ...] = ()
    runcmd: tuple[tuple[str, ...], ...] = ()
    source: ProjectFile | None = None


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    image: str | None
    bundle: ProjectFile | None
    layers: tuple[str, ...]
    memory_mib: int
    vcpus: int
    networks: tuple[str, ...]
    volumes: tuple[MountSpec, ...]
    ports: tuple[PortSpec, ...]
    environment: Mapping[str, str]
    env_files: tuple[ProjectFile, ...]
    cloud_init: CloudInitSpec | None
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class Project:
    version: str
    name: str
    source: Path
    root: Path
    services: Mapping[str, ServiceSpec]
    networks: Mapping[str, NetworkSpec]
    volumes: Mapping[str, VolumeSpec]

    def service(self, name: str) -> ServiceSpec:
        try:
            return self.services[name]
        except KeyError as exc:
            raise ProjectError(f"unknown service: {name!r}") from exc


@dataclass(frozen=True)
class _YamlLine:
    number: int
    indent: int
    content: str


def _context(line: int | None, message: str) -> ProjectError:
    return ProjectError(f"line {line}: {message}" if line is not None else message)


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    depth = 0
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
        elif character == "#" and depth == 0 and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _split_mapping_entry(content: str, line: int) -> tuple[str, str]:
    quote: str | None = None
    escaped = False
    depth = 0
    for index, character in enumerate(content):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
        elif character == ":" and depth == 0 and (index + 1 == len(content) or content[index + 1].isspace()):
            raw_key = content[:index].strip()
            value = content[index + 1 :].strip()
            if not raw_key:
                raise _context(line, "mapping key cannot be empty")
            if raw_key.startswith(('"', "'")):
                key = _parse_scalar(raw_key, line)
                if not isinstance(key, str):
                    raise _context(line, "mapping keys must be strings")
            else:
                if _PLAIN_KEY_RE.fullmatch(raw_key) is None:
                    raise _context(line, f"unsupported mapping key: {raw_key!r}")
                key = raw_key
            if key == "<<":
                raise _context(line, "YAML merge keys are not supported")
            return key, value
    raise _context(line, "expected a 'key: value' mapping entry")


class _FlowParser:
    def __init__(self, text: str, line: int, node_budget: list[int] | None = None) -> None:
        self.text = text
        self.line = line
        self.index = 0
        self.node_budget = node_budget if node_budget is not None else [0]

    def parse(self) -> Any:
        value = self._value(0)
        self._space()
        if self.index != len(self.text):
            raise _context(self.line, f"unexpected flow syntax near {self.text[self.index :]!r}")
        return value

    def _space(self) -> None:
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1

    def _value(self, depth: int) -> Any:
        if depth > MAX_YAML_DEPTH:
            raise _context(self.line, f"flow nesting exceeds {MAX_YAML_DEPTH}")
        self.node_budget[0] += 1
        if self.node_budget[0] > MAX_YAML_NODES:
            raise _context(self.line, f"project exceeds {MAX_YAML_NODES} YAML nodes")
        self._space()
        if self.index >= len(self.text):
            raise _context(self.line, "flow collection contains an empty value")
        character = self.text[self.index]
        if character == "[":
            return self._sequence(depth)
        if character == "{":
            return self._mapping(depth)
        if character in {'"', "'"}:
            return self._quoted()
        start = self.index
        while self.index < len(self.text) and self.text[self.index] not in ",]}":
            self.index += 1
        token = self.text[start : self.index].strip()
        return _parse_plain_scalar(token, self.line)

    def _quoted(self) -> str:
        quote = self.text[self.index]
        start = self.index
        self.index += 1
        escaped = False
        while self.index < len(self.text):
            character = self.text[self.index]
            self.index += 1
            if quote == '"' and character == "\\" and not escaped:
                escaped = True
                continue
            if character == quote and not escaped:
                return _parse_scalar(self.text[start : self.index], self.line, self.node_budget)
            escaped = False
        raise _context(self.line, "unterminated quoted scalar")

    def _sequence(self, depth: int) -> list[Any]:
        self.index += 1
        result: list[Any] = []
        self._space()
        if self.index < len(self.text) and self.text[self.index] == "]":
            self.index += 1
            return result
        while True:
            result.append(self._value(depth + 1))
            self._space()
            if self.index >= len(self.text):
                raise _context(self.line, "unterminated flow sequence")
            separator = self.text[self.index]
            self.index += 1
            if separator == "]":
                return result
            if separator != ",":
                raise _context(self.line, "flow sequence requires ',' separators")

    def _mapping(self, depth: int) -> dict[str, Any]:
        self.index += 1
        result: dict[str, Any] = {}
        self._space()
        if self.index < len(self.text) and self.text[self.index] == "}":
            self.index += 1
            return result
        while True:
            self._space()
            if self.index >= len(self.text):
                raise _context(self.line, "unterminated flow mapping")
            if self.text[self.index] in {'"', "'"}:
                key = self._quoted()
            else:
                start = self.index
                while self.index < len(self.text) and self.text[self.index] != ":":
                    self.index += 1
                key = self.text[start : self.index].strip()
                if _PLAIN_KEY_RE.fullmatch(key) is None:
                    raise _context(self.line, f"unsupported flow mapping key: {key!r}")
            self._space()
            if self.index >= len(self.text) or self.text[self.index] != ":":
                raise _context(self.line, "flow mapping requires ':' after each key")
            self.index += 1
            if key in result:
                raise _context(self.line, f"duplicate mapping key: {key!r}")
            result[key] = self._value(depth + 1)
            self._space()
            if self.index >= len(self.text):
                raise _context(self.line, "unterminated flow mapping")
            separator = self.text[self.index]
            self.index += 1
            if separator == "}":
                return result
            if separator != ",":
                raise _context(self.line, "flow mapping requires ',' separators")


def _parse_plain_scalar(value: str, line: int) -> Any:
    if not value:
        raise _context(line, "scalar cannot be empty")
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        digits = value.removeprefix("-")
        if len(digits) > 20:
            raise _context(line, "integer scalar exceeds the 20-digit safety limit")
        try:
            return int(value)
        except ValueError as exc:
            raise _context(line, "invalid integer scalar") from exc
    if value[0] in "&*!%@`>|":
        raise _context(line, f"unsupported YAML scalar syntax: {value!r}")
    if re.search(r"(?:^|\s)[&*!][^\s]+", value):
        raise _context(line, "YAML anchors, aliases, and tags are not supported")
    if any(character in value for character in "\x00\r\n"):
        raise _context(line, "control characters are not allowed")
    return value


def _parse_scalar(raw: str, line: int, node_budget: list[int] | None = None) -> Any:
    value = _strip_inline_comment(raw)
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _context(line, f"invalid double-quoted scalar: {exc.msg}") from exc
        if not isinstance(parsed, str):
            raise _context(line, "double-quoted scalar must contain a string")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise _context(line, "unterminated single-quoted scalar")
        return _NoInterpolationString(value[1:-1].replace("''", "'"))
    if value.startswith(("[", "{")):
        return _FlowParser(value, line, node_budget).parse()
    return _parse_plain_scalar(value, line)


class _StrictYamlParser:
    def __init__(self, text: str) -> None:
        self.raw_lines = text.splitlines()
        self.lines: list[_YamlLine] = []
        self.index = 0
        self.node_budget = [0]
        for number, raw in enumerate(self.raw_lines, start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
                raise _context(number, "tabs are forbidden in YAML indentation")
            if len(raw.encode("utf-8")) > 65_536:
                raise _context(number, "line exceeds the 64 KiB safety limit")
            stripped = raw.lstrip(" ")
            if not stripped or stripped.startswith("#"):
                continue
            if stripped in {"---", "..."} or stripped.startswith("%YAML"):
                raise _context(number, "YAML directives and document markers are not supported")
            indent = len(raw) - len(stripped)
            if indent % 2:
                raise _context(number, "indentation must use multiples of two spaces")
            self.lines.append(_YamlLine(number, indent, _strip_inline_comment(stripped)))

    def parse(self) -> Any:
        if not self.lines:
            raise ProjectError("project file is empty")
        if self.lines[0].indent != 0:
            raise _context(self.lines[0].number, "top-level mapping must start at column 1")
        result = self._block(0, 0)
        if self.index != len(self.lines):
            line = self.lines[self.index]
            raise _context(line.number, "unexpected trailing YAML content")
        return result

    def _count(self, line: int) -> None:
        self.node_budget[0] += 1
        if self.node_budget[0] > MAX_YAML_NODES:
            raise _context(line, f"project exceeds {MAX_YAML_NODES} YAML nodes")

    def _block(self, indent: int, depth: int) -> Any:
        if depth > MAX_YAML_DEPTH:
            raise _context(self.lines[self.index].number, f"YAML nesting exceeds {MAX_YAML_DEPTH}")
        if self.index >= len(self.lines):
            return None
        line = self.lines[self.index]
        if line.indent != indent:
            raise _context(line.number, f"expected indentation of {indent} spaces")
        if line.content == "-" or line.content.startswith("- "):
            return self._sequence(indent, depth)
        return self._mapping(indent, depth)

    def _nested_or_null(self, indent: int, depth: int) -> Any:
        if self.index >= len(self.lines) or self.lines[self.index].indent <= indent:
            return None
        child = self.lines[self.index]
        if child.indent != indent + 2:
            raise _context(child.number, f"nested content must be indented exactly {indent + 2} spaces")
        return self._block(indent + 2, depth + 1)

    def _mapping(self, indent: int, depth: int, initial: tuple[str, str, int] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        pending = initial
        while pending is not None or self.index < len(self.lines):
            if pending is not None:
                key, raw_value, number = pending
                pending = None
            else:
                line = self.lines[self.index]
                if line.indent < indent:
                    break
                if line.indent > indent:
                    raise _context(line.number, f"unexpected indentation; expected {indent} spaces")
                if line.content == "-" or line.content.startswith("- "):
                    break
                key, raw_value = _split_mapping_entry(line.content, line.number)
                number = line.number
                self.index += 1
            if key in result:
                raise _context(number, f"duplicate mapping key: {key!r}")
            self._count(number)
            if raw_value in {"|", "|-"}:
                result[key] = self._literal(indent, number, keep_final_newline=raw_value == "|")
            elif raw_value:
                result[key] = _parse_scalar(raw_value, number, self.node_budget)
            else:
                result[key] = self._nested_or_null(indent, depth)
        return result

    def _sequence(self, indent: int, depth: int) -> list[Any]:
        result: list[Any] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.indent < indent:
                break
            if line.indent != indent or not (line.content == "-" or line.content.startswith("- ")):
                if line.indent > indent:
                    raise _context(line.number, f"unexpected indentation; expected {indent} spaces")
                break
            self.index += 1
            self._count(line.number)
            raw_item = line.content[1:].strip()
            if not raw_item:
                result.append(self._nested_or_null(indent, depth))
                continue
            try:
                key, raw_value = _split_mapping_entry(raw_item, line.number)
            except ProjectError:
                result.append(_parse_scalar(raw_item, line.number, self.node_budget))
                continue
            item: dict[str, Any] = {}
            if raw_value in {"|", "|-"}:
                item[key] = self._literal(indent, line.number, keep_final_newline=raw_value == "|")
            elif raw_value:
                item[key] = _parse_scalar(raw_value, line.number, self.node_budget)
            else:
                item[key] = self._nested_or_null(indent, depth)
            if self.index < len(self.lines) and self.lines[self.index].indent > indent:
                continuation = self.lines[self.index]
                if continuation.indent != indent + 2:
                    raise _context(continuation.number, f"sequence mapping must use {indent + 2} spaces")
                more = self._mapping(indent + 2, depth + 1)
                duplicate = set(item) & set(more)
                if duplicate:
                    raise _context(continuation.number, f"duplicate mapping key: {min(duplicate)!r}")
                item.update(more)
            result.append(item)
        return result

    def _literal(self, parent_indent: int, line: int, *, keep_final_newline: bool) -> str:
        # Literal content was normalized into ``self.lines``.  It therefore cannot
        # include comment-only or blank lines, so reconstruct it directly from the
        # raw source and advance the normalized cursor over all structural lines it
        # covers.  This preserves cloud-init comments and blank lines.
        raw_index = line
        collected: list[tuple[int, str]] = []
        while raw_index < len(self.raw_lines):
            raw = self.raw_lines[raw_index]
            if not raw.strip():
                collected.append((parent_indent + 2, ""))
                raw_index += 1
                continue
            leading = len(raw) - len(raw.lstrip(" "))
            if "\t" in raw[:leading]:
                raise _context(raw_index + 1, "tabs are forbidden in literal-block indentation")
            if leading <= parent_indent:
                break
            collected.append((leading, raw[leading:]))
            raw_index += 1
        nonblank = [indent for indent, value in collected if value]
        required = min(nonblank, default=parent_indent + 2)
        if required < parent_indent + 2 or required % 2:
            raise _context(line, "literal block indentation is invalid")
        rendered = [" " * max(0, indent - required) + value for indent, value in collected]
        last_line = raw_index
        while self.index < len(self.lines) and self.lines[self.index].number <= last_line:
            self.index += 1
        value = "\n".join(rendered)
        if keep_final_newline:
            return value.rstrip("\n") + "\n"
        return value.rstrip("\n")


def parse_yaml_subset(text: str) -> Any:
    """Parse the safe YAML subset used by ``palimpsest.yml``.

    This function is public primarily for diagnostics and tests; callers normally
    want :func:`load_project` or :func:`parse_project_text`.
    """

    encoded = text.encode("utf-8")
    if len(encoded) > MAX_PROJECT_BYTES:
        raise ProjectError(f"project file exceeds the {MAX_PROJECT_BYTES}-byte limit")
    if "\x00" in text:
        raise ProjectError("project file contains a NUL byte")
    return _StrictYamlParser(text).parse()


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectError(f"{context} must be a mapping")
    return value


def _only_keys(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProjectError(f"{context} contains unsupported key(s): {', '.join(unknown)}")


def _string(value: Any, context: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise ProjectError(f"{context} must be a string")
    if any(character in value for character in "\x00\r"):
        raise ProjectError(f"{context} contains a control character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectError(f"{context} must contain valid UTF-8 text") from exc
    if nonempty and not value.strip():
        raise ProjectError(f"{context} cannot be empty")
    return value


def _resolved_string(
    value: Any,
    context: str,
    environment: Mapping[str, str] | None,
    *,
    nonempty: bool = True,
) -> str:
    """Resolve one structural scalar; single-quoted values remain literal."""

    template = _string(value, context, nonempty=nonempty)
    if isinstance(template, _NoInterpolationString):
        result = str(template)
    else:
        variables = interpolation_variables(template)
        if variables and environment is None:
            raise ProjectError(f"{context} requires an interpolation environment for {', '.join(variables)}")
        result = interpolate(template, {} if environment is None else environment, context=context)
    _reject_secret_value(result, context)
    if nonempty and not result.strip():
        raise ProjectError(f"{context} cannot resolve to an empty string")
    return result


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ProjectError(f"{context} must be true or false")
    return value


def _resolved_boolean(value: Any, context: str, environment: Mapping[str, str] | None) -> bool:
    if isinstance(value, bool):
        return value
    resolved = _resolved_string(value, context, environment).lower()
    if resolved == "true":
        return True
    if resolved == "false":
        return False
    raise ProjectError(f"{context} must resolve to true or false")


def _name(value: Any, context: str) -> str:
    result = _string(value, context)
    if _NAME_RE.fullmatch(result) is None:
        raise ProjectError(f"{context} must match {_NAME_RE.pattern}")
    return result


def _string_list(value: Any, context: str) -> list[str]:
    if isinstance(value, str):
        return [_string(value, context)]
    if not isinstance(value, list):
        raise ProjectError(f"{context} must be a string or sequence of strings")
    return [_string(item, f"{context}[{index}]") for index, item in enumerate(value)]


def _memory_mib(value: Any, context: str, *, minimum: int = 1, maximum: int = 16 * 1024 * 1024) -> int:
    if isinstance(value, bool):
        raise ProjectError(f"{context} must be an integer MiB value or a MiB/GiB string")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and (match := _MEMORY_RE.fullmatch(value)):
        number = match.group("number")
        if len(number) > 10:
            raise ProjectError(f"{context} numeric component exceeds 10 digits")
        result = int(number) * (1024 if match.group("unit").lower() == "gib" else 1)
    else:
        raise ProjectError(f"{context} must be an integer MiB value or a string such as '512MiB' or '4GiB'")
    if not minimum <= result <= maximum:
        raise ProjectError(f"{context} must be between {minimum} and {maximum} MiB")
    return result


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"([a-z])([A-Z])", r"\1_\2", key).lower()
    return _SECRET_WORD_RE.search(normalized) is not None


def _is_external_secret_reference(value: str) -> bool:
    return not isinstance(value, _NoInterpolationString) and _PURE_INTERPOLATION_RE.fullmatch(value) is not None


def _reject_secret_value(value: str, context: str) -> None:
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            raise ProjectError(f"{context} contains secret-shaped material; use an environment reference or env_file")


def interpolation_variables(template: str) -> tuple[str, ...]:
    """Return referenced variable names while validating the interpolation grammar."""

    if isinstance(template, _NoInterpolationString):
        return ()
    variables: list[str] = []
    index = 0
    while index < len(template):
        if template[index] != "$":
            index += 1
            continue
        if index + 1 < len(template) and template[index + 1] == "$":
            index += 2
            continue
        if index + 1 >= len(template) or template[index + 1] != "{":
            raise ProjectError("interpolation only supports ${VAR}, ${VAR:-default}, ${VAR:?error}, and $$")
        end = template.find("}", index + 2)
        if end < 0:
            raise ProjectError("unterminated ${...} interpolation")
        expression = template[index + 2 : end]
        match = _INTERPOLATION_RE.fullmatch(expression)
        if match is None:
            raise ProjectError(f"unsupported interpolation expression: ${{{expression}}}")
        variables.append(match.group("name"))
        index = end + 1
    return tuple(dict.fromkeys(variables))


def interpolate(template: str, environment: Mapping[str, str], *, context: str = "value") -> str:
    """Resolve the deliberately small Compose-compatible interpolation subset."""

    if isinstance(template, _NoInterpolationString):
        return str(template)
    output: list[str] = []
    index = 0
    while index < len(template):
        character = template[index]
        if character != "$":
            output.append(character)
            index += 1
            continue
        if index + 1 < len(template) and template[index + 1] == "$":
            output.append("$")
            index += 2
            continue
        if index + 1 >= len(template) or template[index + 1] != "{":
            raise ProjectError(
                f"{context}: interpolation only supports ${{VAR}}, ${{VAR:-default}}, ${{VAR:?error}}, and $$"
            )
        end = template.find("}", index + 2)
        if end < 0:
            raise ProjectError(f"{context}: unterminated ${{...}} interpolation")
        expression = template[index + 2 : end]
        match = _INTERPOLATION_RE.fullmatch(expression)
        if match is None:
            raise ProjectError(f"{context}: unsupported interpolation expression: ${{{expression}}}")
        name = match.group("name")
        operator = match.group("operator")
        operand = match.group("operand") or ""
        present = name in environment and environment[name] != ""
        if operator == ":-":
            value = environment[name] if present else operand
        elif operator == ":?":
            if not present:
                raise ProjectError(f"{context}: {operand or name + ' is required'}")
            value = environment[name]
        else:
            if name not in environment:
                raise ProjectError(f"{context}: environment variable {name} is not set")
            value = environment[name]
        output.append(value)
        index = end + 1
    result = "".join(output)
    if any(character in result for character in "\x00\r"):
        raise ProjectError(f"{context}: interpolation result contains a control character")
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectError(f"{context}: interpolation result must be valid UTF-8") from exc
    return result


def _validate_template(template: str, context: str, environment: Mapping[str, str] | None) -> None:
    _string(template, context, nonempty=False)
    if isinstance(template, _NoInterpolationString):
        _reject_secret_value(str(template), context)
        return
    try:
        interpolation_variables(template)
        if environment is not None:
            interpolate(template, environment, context=context)
    except ProjectError as exc:
        if str(exc).startswith(f"{context}:"):
            raise
        raise ProjectError(f"{context}: {exc}") from exc
    _reject_secret_value(template, context)


def _safe_project_path(
    root: Path,
    raw: Any,
    context: str,
    *,
    kind: Literal["file", "directory", "either"] = "file",
    max_bytes: int | None = None,
) -> ProjectFile:
    reference = _string(raw, context)
    if "\\" in reference:
        raise ProjectError(f"{context} must use portable '/' path separators")
    pure = PurePosixPath(reference)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ProjectError(f"{context} must be a normalized project-relative path without '.' or '..'")
    try:
        root = root.resolve()
    except OSError as exc:
        raise ProjectError(f"{context} cannot resolve project root") from exc
    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ProjectError(f"{context} cannot access path: {reference}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ProjectError(f"{context} cannot traverse a symlink: {reference}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProjectError(f"{context} cannot resolve path: {reference}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ProjectError(f"{context} escapes the project root: {reference}") from exc
    try:
        is_file = resolved.is_file()
        is_directory = resolved.is_dir()
    except OSError as exc:
        raise ProjectError(f"{context} cannot inspect path: {reference}") from exc
    if kind == "file" and not is_file:
        raise ProjectError(f"{context} must reference a regular file: {reference}")
    if kind == "directory" and not is_directory:
        raise ProjectError(f"{context} must reference a directory: {reference}")
    if kind == "either" and not (is_file or is_directory):
        raise ProjectError(f"{context} must reference a regular file or directory: {reference}")
    content_sha256: str | None = None
    if is_file:
        hasher = hashlib.sha256()
        total = 0
        try:
            with resolved.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if max_bytes is not None and opened.st_size > max_bytes:
                    raise ProjectError(f"{context} exceeds the {max_bytes}-byte limit: {reference}")
                chunk_size = 1024 * 1024 if max_bytes is None else min(1024 * 1024, max_bytes + 1)
                for block in iter(lambda: stream.read(chunk_size), b""):
                    total += len(block)
                    if max_bytes is not None and total > max_bytes:
                        raise ProjectError(f"{context} exceeds the {max_bytes}-byte limit: {reference}")
                    hasher.update(block)
        except OSError as exc:
            raise ProjectError(f"{context} cannot read path: {reference}") from exc
        content_sha256 = f"sha256:{hasher.hexdigest()}"
    return ProjectFile(reference=reference, path=resolved, content_sha256=content_sha256)


def _parse_networks(value: Any, environment: Mapping[str, str] | None) -> dict[str, NetworkSpec]:
    if value is None:
        value = {}
    raw_networks = _mapping(value, "networks")
    result: dict[str, NetworkSpec] = {}
    for raw_name, raw_config in raw_networks.items():
        name = _name(raw_name, f"network name {raw_name!r}")
        config = {} if raw_config is None else _mapping(raw_config, f"networks.{name}")
        _only_keys(config, {"driver", "external", "name"}, f"networks.{name}")
        driver = _resolved_string(config.get("driver", "nat"), f"networks.{name}.driver", environment)
        if driver not in {"nat", "bridge", "isolated"}:
            raise ProjectError(f"networks.{name}.driver must be nat, bridge, or isolated")
        external = _resolved_boolean(config.get("external", False), f"networks.{name}.external", environment)
        external_name = config.get("name")
        if external_name is not None:
            external_name = _name(
                _resolved_string(external_name, f"networks.{name}.name", environment),
                f"networks.{name}.name",
            )
        if external_name is not None and not external:
            raise ProjectError(f"networks.{name}.name requires external: true")
        result[name] = NetworkSpec(name, driver, external, external_name)
    if "default" not in result:
        result["default"] = NetworkSpec("default")
    return result


def _parse_volumes(value: Any, environment: Mapping[str, str] | None) -> dict[str, VolumeSpec]:
    if value is None:
        value = {}
    raw_volumes = _mapping(value, "volumes")
    result: dict[str, VolumeSpec] = {}
    for raw_name, raw_config in raw_volumes.items():
        name = _name(raw_name, f"volume name {raw_name!r}")
        config = {} if raw_config is None else _mapping(raw_config, f"volumes.{name}")
        _only_keys(config, {"driver", "size", "external", "name"}, f"volumes.{name}")
        driver = _resolved_string(config.get("driver", "block"), f"volumes.{name}.driver", environment)
        if driver != "block":
            raise ProjectError(f"volumes.{name}.driver must be block")
        raw_size = config.get("size", "10GiB")
        if isinstance(raw_size, str):
            raw_size = _resolved_string(raw_size, f"volumes.{name}.size", environment)
        size_mib = _memory_mib(raw_size, f"volumes.{name}.size", minimum=16)
        external = _resolved_boolean(config.get("external", False), f"volumes.{name}.external", environment)
        external_name = config.get("name")
        if external_name is not None:
            external_name = _name(
                _resolved_string(external_name, f"volumes.{name}.name", environment),
                f"volumes.{name}.name",
            )
        if external_name is not None and not external:
            raise ProjectError(f"volumes.{name}.name requires external: true")
        if external and "size" in config:
            raise ProjectError(f"volumes.{name}.size cannot be set for an external volume")
        result[name] = VolumeSpec(name, "block", size_mib, external, external_name)
    return result


def _guest_path(raw: Any, context: str) -> str:
    value = _string(raw, context)
    pure = PurePosixPath(value)
    if not pure.is_absolute() or any(part in {".", ".."} for part in pure.parts):
        raise ProjectError(f"{context} must be a normalized absolute guest path")
    normalized = str(pure)
    if value.startswith("//") or normalized != value:
        raise ProjectError(f"{context} must be a normalized absolute guest path")
    if normalized == "/":
        raise ProjectError(f"{context} cannot mount over the guest root")
    return normalized


def _reserved_guest_path(path: str) -> bool:
    """Return true when a path would overwrite or obscure runtime-owned state."""

    fixed = ("/dev", "/proc", "/sys", "/etc/palimpsest", "/opt/layers", "/mnt/palimpsest")
    if any(path == item or path.startswith(item + "/") or item.startswith(path + "/") for item in fixed):
        return True
    for parent in ("/usr/local/libexec", "/etc/systemd/system"):
        if parent == path or parent.startswith(path + "/"):
            return True
        if path.startswith(parent + "/"):
            first = path[len(parent) + 1 :].split("/", 1)[0]
            if first.startswith("palimpsest-"):
                return True
    return False


def _parse_mount(
    raw: Any,
    root: Path,
    volumes: Mapping[str, VolumeSpec],
    context: str,
    environment: Mapping[str, str] | None,
) -> MountSpec:
    if isinstance(raw, str):
        resolved_mount = _resolved_string(raw, context, environment)
        parts = resolved_mount.split(":")
        if len(parts) not in {2, 3} or any(not part for part in parts):
            raise ProjectError(f"{context} must use source:target[:ro|rw] syntax")
        source, target = parts[:2]
        mode = parts[2] if len(parts) == 3 else "rw"
        if mode not in {"ro", "rw"}:
            raise ProjectError(f"{context} mount mode must be ro or rw")
        read_only = mode == "ro"
        mount_type = "bind" if source.startswith(".") else "volume"
    else:
        config = _mapping(raw, context)
        _only_keys(config, {"type", "source", "target", "read_only"}, context)
        mount_type = _resolved_string(config.get("type", "volume"), f"{context}.type", environment)
        if mount_type not in {"volume", "bind"}:
            raise ProjectError(f"{context}.type must be volume or bind")
        source = _resolved_string(config.get("source"), f"{context}.source", environment)
        target = _resolved_string(config.get("target"), f"{context}.target", environment)
        read_only = _resolved_boolean(config.get("read_only", False), f"{context}.read_only", environment)
    guest_target = _guest_path(target, f"{context}.target")
    if _reserved_guest_path(guest_target):
        raise ProjectError(f"{context}.target overlaps a Palimpsest- or kernel-owned guest path: {guest_target}")
    if mount_type == "volume":
        volume_name = _name(source, f"{context}.source")
        if volume_name not in volumes:
            raise ProjectError(f"{context} references undefined top-level volume {volume_name!r}")
        return MountSpec("volume", volume_name, guest_target, read_only)
    raise ProjectError(f"{context}.type bind is unsupported; use a named top-level block volume")


def _port_number(value: Any, context: str) -> int:
    if isinstance(value, bool):
        raise ProjectError(f"{context} must be an integer between 1 and 65535")
    if isinstance(value, str) and value.isdigit():
        if len(value) > 10:
            raise ProjectError(f"{context} numeric component exceeds 10 digits")
        value = int(value)
    if not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ProjectError(f"{context} must be an integer between 1 and 65535")
    return value


def normalize_host_ip(value: Any, context: str = "host IP") -> str:
    """Validate and canonicalize one literal IPv4 or IPv6 host address."""

    raw = _string(value, context)
    try:
        return ipaddress.ip_address(raw).compressed
    except ValueError as exc:
        raise ProjectError(f"{context} must be a literal IPv4 or IPv6 address") from exc


def _parse_port(raw: Any, context: str, environment: Mapping[str, str] | None) -> PortSpec:
    if isinstance(raw, str):
        resolved_port = _resolved_string(raw, context, environment)
        value, separator, protocol = resolved_port.partition("/")
        protocol = protocol if separator else "tcp"
        parts = value.split(":")
        if len(parts) == 2:
            host_ip = "127.0.0.1"
            published, target = parts
        elif len(parts) == 3:
            host_ip, published, target = parts
        else:
            raise ProjectError(f"{context} must use [host_ip:]host_port:guest_port[/tcp|udp] syntax")
    else:
        config = _mapping(raw, context)
        _only_keys(config, {"target", "published", "protocol", "host_ip"}, context)
        if "target" not in config or "published" not in config:
            raise ProjectError(f"{context} requires target and published")
        target = config["target"]
        published = config["published"]
        if isinstance(target, str):
            target = _resolved_string(target, f"{context}.target", environment)
        if isinstance(published, str):
            published = _resolved_string(published, f"{context}.published", environment)
        protocol = _resolved_string(config.get("protocol", "tcp"), f"{context}.protocol", environment)
        host_ip = _resolved_string(config.get("host_ip", "127.0.0.1"), f"{context}.host_ip", environment)
    if protocol != "tcp":
        raise ProjectError(f"{context}.protocol must be tcp in project schema v1")
    return PortSpec(
        host_port=_port_number(published, f"{context}.published"),
        guest_port=_port_number(target, f"{context}.target"),
        protocol=protocol,
        host_ip=normalize_host_ip(host_ip, f"{context}.host_ip"),
    )


def _parse_environment(
    value: Any,
    context: str,
    environment: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    pairs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, raw_value in value.items():
            if raw_value is None:
                raw_value = f"${{{key}:?{key} is required}}"
            elif isinstance(raw_value, bool):
                raw_value = "true" if raw_value else "false"
            elif isinstance(raw_value, int):
                raw_value = str(raw_value)
            pairs.append((key, _string(raw_value, f"{context}.{key}", nonempty=False)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            item = _string(item, f"{context}[{index}]")
            literal = isinstance(item, _NoInterpolationString)
            key, separator, raw_value = item.partition("=")
            template: str = raw_value if separator else f"${{{key}:?{key} is required}}"
            if literal:
                template = _NoInterpolationString(template)
            pairs.append((key, template))
    else:
        raise ProjectError(f"{context} must be a mapping or KEY=value sequence")
    result: dict[str, str] = {}
    for key, template in pairs:
        if _ENV_KEY_RE.fullmatch(key) is None:
            raise ProjectError(f"{context} contains invalid environment key {key!r}")
        if key in result:
            raise ProjectError(f"{context} contains duplicate environment key {key!r}")
        _validate_template(template, f"{context}.{key}", environment)
        if _is_sensitive_key(key) and not _is_external_secret_reference(template):
            raise ProjectError(
                f"{context}.{key} is secret-shaped and must be an environment-only reference such as ${{{key}:?required}}"
            )
        result[key] = template
    return MappingProxyType(dict(sorted(result.items())))


def _read_env_file_templates(
    project_file: ProjectFile,
    *,
    environment: Mapping[str, str] | None,
) -> Mapping[str, str]:
    """Read the strict dotenv subset without retaining resolved values."""

    path = project_file.path
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProjectError(f"env_file changed, disappeared, or is inaccessible: {project_file.reference}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ProjectError(f"env_file must remain a regular non-symlink file: {project_file.reference}")
    if metadata.st_size > MAX_ENV_FILE_BYTES:
        raise ProjectError(f"env_file exceeds the {MAX_ENV_FILE_BYTES}-byte limit: {project_file.reference}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectError(f"cannot read env_file as UTF-8: {project_file.reference}") from exc
    actual_digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    if project_file.content_sha256 is not None and actual_digest != project_file.content_sha256:
        raise ProjectError(f"env_file changed after project validation: {project_file.reference}")
    if "\x00" in text or "\r" in text:
        raise ProjectError(f"env_file contains forbidden control characters: {project_file.reference}")
    result: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        if len(raw_line.encode("utf-8")) > 65_536:
            raise ProjectError(f"{project_file.reference}:{number}: line exceeds 64 KiB")
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw_value = raw_line.partition("=")
        key = key.strip()
        if not separator or _ENV_KEY_RE.fullmatch(key) is None:
            raise ProjectError(f"{project_file.reference}:{number}: expected KEY=VALUE")
        if key in result:
            raise ProjectError(f"{project_file.reference}:{number}: duplicate environment key {key!r}")
        value = _strip_inline_comment(raw_value.strip())
        if value.startswith('"'):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ProjectError(f"{project_file.reference}:{number}: invalid double-quoted value") from exc
            if not isinstance(parsed, str):
                raise ProjectError(f"{project_file.reference}:{number}: quoted value must be a string")
            value = parsed
        elif value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise ProjectError(f"{project_file.reference}:{number}: unterminated single-quoted value")
            value = _NoInterpolationString(value[1:-1].replace("''", "'"))
        context = f"{project_file.reference}:{number}:{key}"
        _validate_template(value, context, environment)
        if _is_sensitive_key(key) and not _is_external_secret_reference(value):
            raise ProjectError(
                f"{context} is secret-shaped and must be an environment-only reference such as ${{{key}:?required}}"
            )
        result[key] = value
    return MappingProxyType(result)


def _parse_cloud_init(
    value: Any,
    root: Path,
    context: str,
    environment: Mapping[str, str] | None,
) -> CloudInitSpec:
    config = _mapping(value, context)
    _only_keys(config, {"inline", "file"}, context)
    if ("inline" in config) == ("file" in config):
        raise ProjectError(f"{context} requires exactly one of inline or file")
    source: ProjectFile | None = None
    if "file" in config:
        source_reference = _resolved_string(config["file"], f"{context}.file", environment)
        source = _safe_project_path(
            root,
            source_reference,
            f"{context}.file",
            max_bytes=256 * 1024,
        )
        try:
            metadata = source.path.stat()
            if metadata.st_size > 256 * 1024:
                raise ProjectError(f"{context}.file exceeds the 256 KiB limit")
            cloud_text = source.path.read_text(encoding="utf-8")
            actual_digest = "sha256:" + hashlib.sha256(cloud_text.encode("utf-8")).hexdigest()
            if source.content_sha256 is not None and actual_digest != source.content_sha256:
                raise ProjectError(f"{context}.file changed during project validation")
            cloud_document = parse_yaml_subset(cloud_text)
        except (OSError, UnicodeDecodeError) as exc:
            raise ProjectError(f"cannot read {context}.file as UTF-8: {source.reference}") from exc
        typed = _mapping(cloud_document, f"{context}.file contents")
    else:
        typed = _mapping(config["inline"], f"{context}.inline")

    typed_context = f"{context}.{'file contents' if source is not None else 'inline'}"
    _only_keys(typed, {"packages", "write_files", "runcmd"}, typed_context)

    packages = tuple(_string_list(typed.get("packages", []), f"{typed_context}.packages"))
    if len(packages) > 128:
        raise ProjectError(f"{typed_context}.packages supports at most 128 entries")
    if len(set(packages)) != len(packages):
        raise ProjectError(f"{typed_context}.packages cannot contain duplicates")
    for package in packages:
        _validate_template(package, f"{typed_context}.packages", None)
        checked_package = package
        unresolved_template = not isinstance(package, _NoInterpolationString) and bool(interpolation_variables(package))
        if not unresolved_template and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@/-]{0,127}", checked_package) is None:
            raise ProjectError(f"{typed_context}.packages contains an invalid package name: {checked_package!r}")

    raw_write_files = typed.get("write_files", [])
    if not isinstance(raw_write_files, list):
        raise ProjectError(f"{typed_context}.write_files must be a sequence")
    if len(raw_write_files) > 64:
        raise ProjectError(f"{typed_context}.write_files supports at most 64 entries")
    write_files: list[CloudInitWriteFile] = []
    for index, raw_write in enumerate(raw_write_files):
        write_context = f"{typed_context}.write_files[{index}]"
        write = _mapping(raw_write, write_context)
        _only_keys(write, {"path", "content", "permissions"}, write_context)
        if "path" not in write or "content" not in write:
            raise ProjectError(f"{write_context} requires path and content")
        guest_path = _guest_path(
            _resolved_string(write["path"], f"{write_context}.path", environment),
            f"{write_context}.path",
        )
        if _reserved_guest_path(guest_path):
            raise ProjectError(f"{write_context}.path overlaps a Palimpsest- or kernel-owned guest path: {guest_path}")
        content = _string(write["content"], f"{write_context}.content", nonempty=False)
        if len(content.encode("utf-8")) > 64 * 1024:
            raise ProjectError(f"{write_context}.content exceeds the 64 KiB limit")
        permissions = _resolved_string(
            write.get("permissions", "0644"),
            f"{write_context}.permissions",
            environment,
        )
        if re.fullmatch(r"0[0-7]{3}", permissions) is None:
            raise ProjectError(f"{write_context}.permissions must be a four-digit octal string such as '0644'")
        _validate_template(content, f"{write_context}.content", None)
        for line in content.splitlines():
            key, separator, raw_value = line.strip().partition(":")
            if separator and _is_sensitive_key(key) and raw_value.strip() and "${" not in raw_value:
                raise ProjectError(f"{write_context}.content contains literal secret-shaped field {key!r}")
        write_files.append(CloudInitWriteFile(guest_path, content, permissions))
    paths = [item.path for item in write_files]
    if len(set(paths)) != len(paths):
        raise ProjectError(f"{typed_context}.write_files cannot contain duplicate guest paths")

    raw_runcmd = typed.get("runcmd", [])
    if not isinstance(raw_runcmd, list):
        raise ProjectError(f"{typed_context}.runcmd must be a sequence of argv sequences")
    if len(raw_runcmd) > 64:
        raise ProjectError(f"{typed_context}.runcmd supports at most 64 commands")
    commands: list[tuple[str, ...]] = []
    for index, raw_command in enumerate(raw_runcmd):
        command_context = f"{typed_context}.runcmd[{index}]"
        if not isinstance(raw_command, list) or not raw_command:
            raise ProjectError(f"{command_context} must be a nonempty argv sequence; shell strings are forbidden")
        if len(raw_command) > 64:
            raise ProjectError(f"{command_context} supports at most 64 arguments")
        argv = tuple(_string(argument, f"{command_context}[]") for argument in raw_command)
        for argument in argv:
            _validate_template(argument, command_context, None)
        commands.append(argv)
    return CloudInitSpec(tuple(packages), tuple(write_files), tuple(commands), source)


def _parse_service(
    name: str,
    raw: Any,
    root: Path,
    networks: Mapping[str, NetworkSpec],
    volumes: Mapping[str, VolumeSpec],
    environment: Mapping[str, str] | None,
) -> ServiceSpec:
    context = f"services.{name}"
    config = _mapping(raw, context)
    _only_keys(
        config,
        {
            "image",
            "bundle",
            "layers",
            "memory",
            "vcpus",
            "networks",
            "volumes",
            "ports",
            "environment",
            "env_file",
            "cloud_init",
            "depends_on",
        },
        context,
    )
    if ("image" in config) == ("bundle" in config):
        raise ProjectError(f"{context} requires exactly one of image or bundle")
    image: str | None = None
    bundle: ProjectFile | None = None
    if "image" in config:
        try:
            image = require_digest(_resolved_string(config["image"], f"{context}.image", environment))
        except InvalidDigestError as exc:
            raise ProjectError(f"{context}.image must be a sha256 cloud-image digest") from exc
    else:
        bundle_reference = _resolved_string(config["bundle"], f"{context}.bundle", environment)
        bundle = _safe_project_path(root, bundle_reference, f"{context}.bundle", kind="directory")
    raw_layers = _string_list(config["layers"], f"{context}.layers") if "layers" in config else []
    try:
        layers = tuple(
            require_digest(_resolved_string(layer, f"{context}.layers[{index}]", environment))
            for index, layer in enumerate(raw_layers)
        )
    except InvalidDigestError as exc:
        raise ProjectError(f"{context}.layers entries must be sha256 digests") from exc
    if len(layers) > 25:
        raise ProjectError(f"{context}.layers supports at most 25 entries")
    if len(set(layers)) != len(layers):
        raise ProjectError(f"{context}.layers cannot contain duplicate digests")
    raw_memory = config.get("memory", "4GiB")
    if isinstance(raw_memory, str):
        raw_memory = _resolved_string(raw_memory, f"{context}.memory", environment)
    memory_mib = _memory_mib(raw_memory, f"{context}.memory", minimum=256, maximum=1_048_576)
    vcpus = config.get("vcpus", 2)
    if isinstance(vcpus, str):
        vcpus = _resolved_string(vcpus, f"{context}.vcpus", environment)
        if vcpus.isdigit():
            if len(vcpus) > 10:
                raise ProjectError(f"{context}.vcpus numeric component exceeds 10 digits")
            vcpus = int(vcpus)
    if isinstance(vcpus, bool) or not isinstance(vcpus, int) or not 1 <= vcpus <= 256:
        raise ProjectError(f"{context}.vcpus must be an integer between 1 and 256")
    network_names = tuple(
        _resolved_string(network, f"{context}.networks[{index}]", environment)
        for index, network in enumerate(_string_list(config.get("networks", ["default"]), f"{context}.networks"))
    )
    if not network_names:
        raise ProjectError(f"{context}.networks cannot be empty")
    if len(network_names) > 1:
        raise ProjectError(f"{context}.networks supports exactly one attachment in project schema v1")
    if len(set(network_names)) != len(network_names):
        raise ProjectError(f"{context}.networks cannot contain duplicates")
    for network_name in network_names:
        _name(network_name, f"{context}.networks")
        if network_name not in networks:
            raise ProjectError(f"{context}.networks references undefined network {network_name!r}")
    raw_mounts = config.get("volumes", [])
    if not isinstance(raw_mounts, list):
        raise ProjectError(f"{context}.volumes must be a sequence")
    mounts = tuple(
        _parse_mount(item, root, volumes, f"{context}.volumes[{index}]", environment)
        for index, item in enumerate(raw_mounts)
    )
    targets = [mount.target for mount in mounts]
    if len(set(targets)) != len(targets):
        raise ProjectError(f"{context}.volumes contains duplicate guest mount targets")
    raw_ports = config.get("ports", [])
    if not isinstance(raw_ports, list):
        raise ProjectError(f"{context}.ports must be a sequence")
    ports = tuple(_parse_port(item, f"{context}.ports[{index}]", environment) for index, item in enumerate(raw_ports))
    guest_bindings = [(port.guest_port, port.protocol) for port in ports]
    if len(set(guest_bindings)) != len(guest_bindings):
        raise ProjectError(f"{context}.ports contains duplicate guest port/protocol mappings")
    env_files = tuple(
        _safe_project_path(
            root,
            _resolved_string(item, f"{context}.env_file[{index}]", environment),
            f"{context}.env_file[{index}]",
            max_bytes=MAX_ENV_FILE_BYTES,
        )
        for index, item in enumerate(_string_list(config.get("env_file", []), f"{context}.env_file"))
    )
    if len({item.path for item in env_files}) != len(env_files):
        raise ProjectError(f"{context}.env_file cannot contain duplicates")
    for env_file in env_files:
        _read_env_file_templates(env_file, environment=None)
    cloud_init = (
        _parse_cloud_init(config["cloud_init"], root, f"{context}.cloud_init", environment)
        if "cloud_init" in config
        else None
    )
    depends_on = tuple(
        _resolved_string(dependency, f"{context}.depends_on[{index}]", environment)
        for index, dependency in enumerate(_string_list(config.get("depends_on", []), f"{context}.depends_on"))
    )
    if len(set(depends_on)) != len(depends_on):
        raise ProjectError(f"{context}.depends_on cannot contain duplicates")
    service = ServiceSpec(
        name=name,
        image=image,
        bundle=bundle,
        layers=layers,
        memory_mib=memory_mib,
        vcpus=vcpus,
        networks=network_names,
        volumes=mounts,
        ports=ports,
        environment=_parse_environment(config.get("environment"), f"{context}.environment", None),
        env_files=env_files,
        cloud_init=cloud_init,
        depends_on=depends_on,
    )
    return service


def _validate_dependencies(services: Mapping[str, ServiceSpec]) -> None:
    for name, service in services.items():
        for dependency in service.depends_on:
            if dependency not in services:
                raise ProjectError(f"services.{name}.depends_on references unknown service {dependency!r}")
            if dependency == name:
                raise ProjectError(f"services.{name}.depends_on cannot reference itself")
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            start = visiting.index(name)
            cycle = visiting[start:] + [name]
            raise ProjectError(f"service dependency cycle detected: {' -> '.join(cycle)}")
        visiting.append(name)
        for dependency in sorted(services[name].depends_on):
            visit(dependency)
        visiting.pop()
        visited.add(name)

    for name in sorted(services):
        visit(name)


def _validate_host_port_collisions(services: Mapping[str, ServiceSpec]) -> None:
    claimed: list[tuple[PortSpec, str]] = []
    for name in sorted(services):
        for port in services[name].ports:
            for existing, owner in claimed:
                same_socket = port.host_port == existing.host_port and port.protocol == existing.protocol
                wildcard = port.host_ip in {"0.0.0.0", "::"} or existing.host_ip in {"0.0.0.0", "::"}
                if same_socket and (wildcard or port.host_ip == existing.host_ip):
                    raise ProjectError(
                        f"host port {port.host_ip}:{port.host_port}/{port.protocol} conflicts with "
                        f"{existing.host_ip}:{existing.host_port}/{existing.protocol}; claimed by both "
                        f"services {owner!r} and {name!r}"
                    )
            claimed.append((port, name))


def deterministic_project_name(
    project_file: Path,
    declared_name: str | None = None,
    *,
    project_directory: Path | None = None,
) -> str:
    """Return a stable libvirt-safe project name (maximum 63 characters)."""

    try:
        source = project_file.expanduser().resolve(strict=False)
        base_directory = (
            source.parent if project_directory is None else project_directory.expanduser().resolve(strict=True)
        )
    except OSError as exc:
        raise ProjectError(f"cannot resolve project path for deterministic naming: {project_file}") from exc
    if project_directory is not None and not base_directory.is_dir():
        raise ProjectError(f"project directory is not a directory: {base_directory}")
    raw = declared_name if declared_name is not None else base_directory.name
    if declared_name is not None:
        _name(declared_name, "name")
    collision_key = declared_name if declared_name is not None else str(base_directory)
    return _runtime_slug(raw, collision_key=collision_key)


def deterministic_service_name(project_name: str, service_name: str, replica: int = 1) -> str:
    """Return the deterministic VM name for one project service replica."""

    _name(project_name, "project name")
    _name(service_name, "service name")
    if isinstance(replica, bool) or not isinstance(replica, int) or replica < 1:
        raise ProjectError("service replica must be a positive integer")
    raw = f"{project_name}-{service_name}-{replica}"
    return _runtime_slug(raw, collision_key=raw)


def _runtime_slug(raw: str, *, collision_key: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-") or "project"
    changed = slug != raw or len(slug) > 63
    if changed:
        suffix = hashlib.sha256(collision_key.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug[: 63 - len(suffix) - 1].rstrip('-')}-{suffix}"
    return slug[:63]


def parse_project_document(
    document: Any,
    *,
    source: Path,
    environment: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> Project:
    """Validate a decoded project mapping and return the normalized model.

    Structural strings are resolved before type/path/digest validation.  Environment
    and typed cloud-init values intentionally remain templates for execution-time
    resolution so credentials never enter canonical project state.  Single-quoted
    YAML strings are literal.  Mapping keys are never interpolated.
    """

    root_config = _mapping(document, "project")
    _only_keys(root_config, {"version", "name", "services", "networks", "volumes"}, "project")
    if "services" not in root_config:
        raise ProjectError("project.services is required")
    version_value = root_config.get("version", PROJECT_SCHEMA_VERSION)
    version = (
        str(version_value) if isinstance(version_value, int) and not isinstance(version_value, bool) else version_value
    )
    if isinstance(version, str):
        version = _resolved_string(version, "project.version", environment)
    if version != PROJECT_SCHEMA_VERSION:
        raise ProjectError(f"project.version must be {PROJECT_SCHEMA_VERSION!r}")
    try:
        source = source.expanduser().resolve(strict=False)
        root = source.parent.resolve() if project_root is None else project_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProjectError(f"cannot resolve project source path: {source}") from exc
    if project_root is not None and not root.is_dir():
        raise ProjectError(f"project root is not a directory: {root}")
    declared_name = root_config.get("name")
    if declared_name is not None:
        declared_name = _name(_resolved_string(declared_name, "project.name", environment), "project.name")
    project_name = deterministic_project_name(source, declared_name, project_directory=root)
    networks = _parse_networks(root_config.get("networks"), environment)
    volumes = _parse_volumes(root_config.get("volumes"), environment)
    raw_services = _mapping(root_config["services"], "services")
    if not raw_services:
        raise ProjectError("project.services cannot be empty")
    if len(raw_services) > 128:
        raise ProjectError("project.services supports at most 128 services")
    services: dict[str, ServiceSpec] = {}
    for raw_name, raw_service in raw_services.items():
        name = _name(raw_name, f"service name {raw_name!r}")
        services[name] = _parse_service(name, raw_service, root, networks, volumes, environment)
    _validate_dependencies(services)
    _validate_host_port_collisions(services)
    return Project(
        version=version,
        name=project_name,
        source=source,
        root=root,
        services=MappingProxyType(dict(sorted(services.items()))),
        networks=MappingProxyType(dict(sorted(networks.items()))),
        volumes=MappingProxyType(dict(sorted(volumes.items()))),
    )


def parse_project_text(
    text: str,
    *,
    source: Path,
    environment: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> Project:
    """Parse and normalize project YAML text."""

    return parse_project_document(
        parse_yaml_subset(text),
        source=source,
        environment=environment,
        project_root=project_root,
    )


def load_project(
    path: Path | str = DEFAULT_PROJECT_FILE,
    environment: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
) -> Project:
    """Load a regular, non-symlink project file and normalize its structure.

    ``environment=None`` uses the current process environment for structural
    strings such as image, path, network, and resource fields.  Service environment
    and typed cloud-init values remain validated templates; their required-variable
    checks happen only in :func:`resolve_service_environment` and
    :func:`resolve_cloud_init` immediately before ``up`` creates a runtime.
    """

    raw_path = Path(path).expanduser()
    try:
        metadata = raw_path.lstat()
    except OSError as exc:
        raise ProjectError(f"cannot access project file: {raw_path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ProjectError(f"project file must be a regular file, not a symlink: {raw_path}")
    if metadata.st_size > MAX_PROJECT_BYTES:
        raise ProjectError(f"project file exceeds the {MAX_PROJECT_BYTES}-byte limit")
    try:
        text = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectError(f"cannot read project file as UTF-8: {raw_path}") from exc
    active_environment = os.environ if environment is None else environment
    try:
        resolved_path = raw_path.resolve(strict=True)
    except OSError as exc:
        raise ProjectError(f"cannot resolve project file: {raw_path}") from exc
    return parse_project_text(
        text,
        source=resolved_path,
        environment=active_environment,
        project_root=project_root,
    )


def service_start_order(project: Project, selected: Sequence[str] | None = None) -> tuple[str, ...]:
    """Return dependencies-first, lexically deterministic service order."""

    if isinstance(selected, (str, bytes)):
        raise ProjectError("selected services must be a sequence, not a single string")
    requested = set(project.services if selected is None else selected)
    unknown = sorted(requested - set(project.services))
    if unknown:
        raise ProjectError(f"unknown selected service(s): {', '.join(unknown)}")
    closure: set[str] = set()

    def include(name: str) -> None:
        if name in closure:
            return
        for dependency in project.services[name].depends_on:
            include(dependency)
        closure.add(name)

    for name in requested:
        include(name)
    order: list[str] = []
    emitted: set[str] = set()
    while len(emitted) < len(closure):
        ready = sorted(name for name in closure - emitted if set(project.services[name].depends_on).issubset(emitted))
        if not ready:  # pragma: no cover - parse validation already proves acyclicity
            raise ProjectError("service dependency graph has no runnable node")
        order.extend(ready)
        emitted.update(ready)
    return tuple(order)


def resolve_service_environment(service: ServiceSpec, environment: Mapping[str, str]) -> dict[str, str]:
    """Merge dotenv files then service values and resolve only for execution.

    Files are applied in declaration order (later files override earlier files),
    followed by the inline ``environment`` mapping.  A template may refer to the
    host environment or a value resolved earlier in the merge.  No resolved value
    is written back to the frozen model or canonical project payload.
    """

    resolved: dict[str, str] = {}
    for env_file in service.env_files:
        templates = _read_env_file_templates(env_file, environment=None)
        for key, template in templates.items():
            scope = {**environment, **resolved}
            resolved[key] = interpolate(template, scope, context=f"{env_file.reference}:{key}")
    inline_scope = {**environment, **resolved}
    inline_values: dict[str, str] = {}
    for key, template in service.environment.items():
        inline_values[key] = interpolate(
            template,
            inline_scope,
            context=f"services.{service.name}.environment.{key}",
        )
    resolved.update(inline_values)
    return resolved


def load_interpolation_environment(
    project_root: Path,
    files: Sequence[Path | str],
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load project dotenv files for YAML interpolation, with host values highest.

    Dotenv files are project-contained regular files and are applied in declaration
    order.  Later file values override earlier file values, then ``base`` (the
    process environment by default) overrides every file.  Templates may reference
    earlier file values or host values; no resolved data is retained by a model.
    """

    if isinstance(files, (str, bytes)):
        raise ProjectError("interpolation env files must be a sequence, not a single string")
    try:
        root = project_root.expanduser().resolve()
    except OSError as exc:
        raise ProjectError(f"cannot resolve interpolation project root: {project_root}") from exc
    base_environment = dict(os.environ if base is None else base)
    resolved_files: dict[str, str] = {}
    for index, raw_file in enumerate(files):
        project_file = _safe_project_path(
            root,
            os.fspath(raw_file),
            f"interpolation env file [{index}]",
            max_bytes=MAX_ENV_FILE_BYTES,
        )
        templates = _read_env_file_templates(project_file, environment=None)
        for key, template in templates.items():
            scope = {**resolved_files, **base_environment}
            resolved_files[key] = interpolate(template, scope, context=f"{project_file.reference}:{key}")
    return {**resolved_files, **base_environment}


def resolve_cloud_init(spec: CloudInitSpec, environment: Mapping[str, str]) -> CloudInitSpec:
    """Resolve the typed cloud-init templates into an execution-only copy."""

    packages = tuple(
        interpolate(value, environment, context=f"cloud_init.packages[{index}]")
        for index, value in enumerate(spec.packages)
    )
    for package in packages:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@/-]{0,127}", package) is None:
            raise ProjectError(f"resolved cloud_init package is invalid: {package!r}")
    if len(set(packages)) != len(packages):
        raise ProjectError("resolved cloud_init packages cannot contain duplicates")
    write_files = tuple(
        CloudInitWriteFile(
            item.path,
            interpolate(item.content, environment, context=f"cloud_init.write_files[{index}].content"),
            item.permissions,
        )
        for index, item in enumerate(spec.write_files)
    )
    for index, item in enumerate(write_files):
        if len(item.content.encode("utf-8")) > 64 * 1024:
            raise ProjectError(f"resolved cloud_init.write_files[{index}].content exceeds 64 KiB")
    commands = tuple(
        tuple(
            interpolate(argument, environment, context=f"cloud_init.runcmd[{command_index}][{argument_index}]")
            for argument_index, argument in enumerate(command)
        )
        for command_index, command in enumerate(spec.runcmd)
    )
    for index, command in enumerate(commands):
        if not command[0]:
            raise ProjectError(f"resolved cloud_init.runcmd[{index}] executable cannot be empty")
    return CloudInitSpec(packages, write_files, commands, spec.source)


def canonical_project_payload(project: Project) -> dict[str, Any]:
    """Return deterministic, state-safe configuration with only relative paths."""

    def network_payload(network: NetworkSpec) -> dict[str, Any]:
        return {
            "driver": network.driver,
            "external": network.external,
            "name": network.external_name,
        }

    def volume_payload(volume: VolumeSpec) -> dict[str, Any]:
        return {
            "driver": volume.driver,
            "external": volume.external,
            "name": volume.external_name,
            "size_mib": volume.size_mib,
        }

    def mount_payload(mount: MountSpec) -> dict[str, Any]:
        return {
            "read_only": mount.read_only,
            "source": mount.source,
            "target": mount.target,
            "type": mount.type,
        }

    def port_payload(port: PortSpec) -> dict[str, Any]:
        return {
            "guest_port": port.guest_port,
            "host_ip": port.host_ip,
            "host_port": port.host_port,
            "protocol": port.protocol,
        }

    def service_payload(service: ServiceSpec) -> dict[str, Any]:
        cloud_init: dict[str, Any] | None = None
        if service.cloud_init is not None:
            cloud_init = {
                "packages": list(service.cloud_init.packages),
                "runcmd": [list(command) for command in service.cloud_init.runcmd],
                "source": service.cloud_init.source.reference if service.cloud_init.source is not None else None,
                "write_files": [
                    {
                        "content": item.content,
                        "path": item.path,
                        "permissions": item.permissions,
                    }
                    for item in service.cloud_init.write_files
                ],
            }
        return {
            "bundle": service.bundle.reference if service.bundle is not None else None,
            "cloud_init": cloud_init,
            "depends_on": list(service.depends_on),
            "env_file": [item.reference for item in service.env_files],
            "environment": dict(service.environment),
            "image": service.image,
            "layers": list(service.layers),
            "memory_mib": service.memory_mib,
            "networks": list(service.networks),
            "ports": [port_payload(item) for item in service.ports],
            "vcpus": service.vcpus,
            "volumes": [mount_payload(item) for item in service.volumes],
        }

    payload = {
        "name": project.name,
        "networks": {name: network_payload(project.networks[name]) for name in sorted(project.networks)},
        "services": {name: service_payload(project.services[name]) for name in sorted(project.services)},
        "version": project.version,
        "volumes": {name: volume_payload(project.volumes[name]) for name in sorted(project.volumes)},
    }
    _assert_state_safe(payload)
    return payload


def _assert_state_safe(value: Any, *, key: str | None = None) -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            _assert_state_safe(child, key=child_key)
    elif isinstance(value, list):
        for child in value:
            _assert_state_safe(child, key=key)
    elif isinstance(value, str):
        _reject_secret_value(value, f"canonical project field {key or '<value>'}")
        if key is not None and _is_sensitive_key(key) and not _is_external_secret_reference(value):
            raise ProjectError(f"secret-shaped field {key!r} cannot be serialized into project state")


def canonical_project_json(project: Project) -> str:
    """Return canonical UTF-8 JSON text with a trailing newline."""

    return (
        json.dumps(canonical_project_payload(project), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    )


def project_config_digest(project: Project) -> str:
    """Hash the canonical unresolved project payload."""

    return "sha256:" + hashlib.sha256(canonical_project_json(project).encode("utf-8")).hexdigest()
