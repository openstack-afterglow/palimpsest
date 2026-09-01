"""Canonical, shell-free OCI image process configuration."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ArtifactValidationError

MAX_PROCESS_ARGUMENTS = 4096
MAX_PROCESS_ENVIRONMENT = 4096
MAX_PROCESS_BYTES = 256 * 1024
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
_SIGNALS = {
    "SIGHUP": 1,
    "SIGINT": 2,
    "SIGQUIT": 3,
    "SIGABRT": 6,
    "SIGKILL": 9,
    "SIGUSR1": 10,
    "SIGUSR2": 12,
    "SIGPIPE": 13,
    "SIGALRM": 14,
    "SIGTERM": 15,
    "SIGCHLD": 17,
    "SIGCONT": 18,
    "SIGSTOP": 19,
    "SIGTSTP": 20,
    "SIGTTIN": 21,
    "SIGTTOU": 22,
}


def _plain_string(value: Any, field_name: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or "\0" in value:
        raise ArtifactValidationError(f"{field_name} is invalid")
    if len(value.encode("utf-8")) > 32 * 1024:
        raise ArtifactValidationError(f"{field_name} is too large")
    return value


def _account(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field_name} is invalid")
    if value.isdecimal():
        number = int(value)
        if number > 2**32 - 2 or str(number) != value:
            raise ArtifactValidationError(f"{field_name} is not canonical")
    elif _ACCOUNT_RE.fullmatch(value) is None:
        raise ArtifactValidationError(f"{field_name} is invalid")
    return value


def _stop_signal(value: Any) -> int:
    if value is None or value == "":
        return 15
    if type(value) is int:
        number = value
    elif isinstance(value, str):
        rendered = value.upper()
        if rendered.isdecimal():
            number = int(rendered)
            if str(number) != rendered:
                raise ArtifactValidationError("image process stop signal is not canonical")
        else:
            rendered = rendered if rendered.startswith("SIG") else f"SIG{rendered}"
            number = _SIGNALS.get(rendered, 0)
    else:
        number = 0
    if not 1 <= number <= 64:
        raise ArtifactValidationError("image process stop signal is unsupported")
    return number


@dataclass(frozen=True, slots=True)
class OCIUserSpec:
    user: str
    group: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user", _account(self.user, "image process user"))
        if self.group is not None:
            object.__setattr__(self, "group", _account(self.group, "image process group"))

    def to_dict(self) -> dict[str, Any]:
        return {"group": self.group, "user": self.user}

    @classmethod
    def from_value(cls, value: Any) -> OCIUserSpec:
        if value is None or value == "":
            return cls("0", "0")
        if not isinstance(value, str) or value.count(":") > 1:
            raise ArtifactValidationError("image process user is invalid")
        user, separator, group = value.partition(":")
        if not user or (separator and not group):
            raise ArtifactValidationError("image process user is invalid")
        return cls(user, group if separator else None)

    @classmethod
    def from_dict(cls, value: Any) -> OCIUserSpec:
        if not isinstance(value, Mapping) or set(value) != {"group", "user"}:
            raise ArtifactValidationError("image process user fields are invalid")
        user = cls(value["user"], value["group"])
        if user.to_dict() != dict(value):
            raise ArtifactValidationError("image process user is not canonical")
        return user


@dataclass(frozen=True, slots=True)
class OCIProcessSpec:
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: str
    user: OCIUserSpec
    stop_signal: int

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or len(self.argv) > MAX_PROCESS_ARGUMENTS:
            raise ArtifactValidationError("image process argv is invalid")
        argv = tuple(_plain_string(value, f"image process argv[{index}]") for index, value in enumerate(self.argv))
        if not isinstance(self.environment, tuple) or len(self.environment) > MAX_PROCESS_ENVIRONMENT:
            raise ArtifactValidationError("image process environment is invalid")
        environment: list[tuple[str, str]] = []
        names: set[str] = set()
        for index, item in enumerate(self.environment):
            if not isinstance(item, tuple) or len(item) != 2:
                raise ArtifactValidationError("image process environment entry is invalid")
            name, raw_value = item
            if not isinstance(name, str) or _ENV_NAME_RE.fullmatch(name) is None or name in names:
                raise ArtifactValidationError("image process environment name is invalid or duplicated")
            names.add(name)
            environment.append((name, _plain_string(raw_value, f"image process environment[{index}].value")))
        cwd = _plain_string(self.cwd, "image process cwd", allow_empty=False)
        if not cwd.startswith("/") or posixpath.normpath(cwd) != cwd or "//" in cwd:
            raise ArtifactValidationError("image process cwd must be a canonical absolute path")
        if not isinstance(self.user, OCIUserSpec):
            raise ArtifactValidationError("image process user is invalid")
        signal_number = _stop_signal(self.stop_signal)
        total = sum(len(value.encode("utf-8")) + 1 for value in argv)
        total += sum(len(name.encode()) + len(value.encode("utf-8")) + 2 for name, value in environment)
        total += len(cwd.encode("utf-8")) + len(str(self.user.to_dict()).encode())
        if total > MAX_PROCESS_BYTES:
            raise ArtifactValidationError("image process contract is too large")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "environment", tuple(environment))
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "stop_signal", signal_number)

    @property
    def bootable(self) -> bool:
        return bool(self.argv and self.argv[0])

    def require_bootable(self) -> None:
        if not self.bootable:
            raise ArtifactValidationError("OCI image has no Entrypoint or Cmd to execute")

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "environment": [{"name": name, "value": value} for name, value in self.environment],
            "stop_signal": self.stop_signal,
            "user": self.user.to_dict(),
        }

    @classmethod
    def empty(cls) -> OCIProcessSpec:
        return cls((), (), "/", OCIUserSpec("0", "0"), 15)

    @classmethod
    def from_config(cls, value: Any) -> OCIProcessSpec:
        if value is None:
            return cls.empty()
        if not isinstance(value, Mapping):
            raise ArtifactValidationError("image config.config must be an object or null")
        args_escaped = value.get("ArgsEscaped")
        if args_escaped is not None and args_escaped is not False:
            raise ArtifactValidationError("image process ArgsEscaped is unsupported on linux")

        def string_array(field_name: str) -> tuple[str, ...]:
            raw = value.get(field_name)
            if raw is None:
                return ()
            if not isinstance(raw, list):
                raise ArtifactValidationError(f"image process {field_name} must be an array or null")
            return tuple(_plain_string(item, f"image process {field_name}[{index}]") for index, item in enumerate(raw))

        entrypoint = string_array("Entrypoint")
        command = string_array("Cmd")
        raw_environment = value.get("Env")
        environment: list[tuple[str, str]] = []
        if raw_environment is not None:
            if not isinstance(raw_environment, list):
                raise ArtifactValidationError("image process Env must be an array or null")
            for item in raw_environment:
                rendered = _plain_string(item, "image process Env entry")
                name, separator, env_value = rendered.partition("=")
                if not separator:
                    raise ArtifactValidationError("image process Env entry must contain '='")
                environment.append((name, env_value))
        raw_cwd = value.get("WorkingDir")
        if raw_cwd is None or raw_cwd == "":
            raw_cwd = "/"
        cwd = posixpath.normpath(_plain_string(raw_cwd, "image process WorkingDir", allow_empty=False))
        return cls(
            argv=(*entrypoint, *command),
            environment=tuple(environment),
            cwd=cwd,
            user=OCIUserSpec.from_value(value.get("User")),
            stop_signal=_stop_signal(value.get("StopSignal")),
        )

    @classmethod
    def from_dict(cls, value: Any) -> OCIProcessSpec:
        if not isinstance(value, Mapping) or set(value) != {"argv", "cwd", "environment", "stop_signal", "user"}:
            raise ArtifactValidationError("image process fields are invalid")
        raw_environment = value.get("environment")
        if not isinstance(value.get("argv"), list) or not isinstance(raw_environment, list):
            raise ArtifactValidationError("image process arrays are invalid")
        environment: list[tuple[str, str]] = []
        for item in raw_environment:
            if not isinstance(item, Mapping) or set(item) != {"name", "value"}:
                raise ArtifactValidationError("image process environment fields are invalid")
            environment.append((item["name"], item["value"]))
        process = cls(
            tuple(value["argv"]),
            tuple(environment),
            value["cwd"],
            OCIUserSpec.from_dict(value["user"]),
            value["stop_signal"],
        )
        if process.to_dict() != dict(value):
            raise ArtifactValidationError("image process contract is not canonical")
        return process


__all__ = ["MAX_PROCESS_ARGUMENTS", "MAX_PROCESS_BYTES", "MAX_PROCESS_ENVIRONMENT", "OCIProcessSpec", "OCIUserSpec"]
