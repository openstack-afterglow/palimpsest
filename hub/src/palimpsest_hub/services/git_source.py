"""Git build-source admission and bounded fetch for the build worker.

The API only validates and records a pinned request; the isolated worker performs the
fetch. Validation is pure (injected resolver) so it can be tested without a network:
https only, no userinfo, a 40-hex revision, an operator host allow-list (default deny),
every resolved address public, and the first resolved address pinned for the connection.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA = re.compile(r"^[0-9a-f]{40}$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 50_000
FETCH_TIMEOUT = 300


class GitSourceError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


class GitBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_url: str = Field(min_length=1, max_length=2048)
    revision: str
    dockerfile_path: str = Field(default="Dockerfile", min_length=1, max_length=1024)
    context_subdir: str = Field(default=".", min_length=1, max_length=1024)
    credential_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    @field_validator("revision")
    @classmethod
    def pinned(cls, value: str) -> str:
        value = value.lower()
        if not _SHA.fullmatch(value):
            raise ValueError("revision must be a full 40-character commit SHA")
        return value

    @field_validator("dockerfile_path", "context_subdir")
    @classmethod
    def relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\x00" in value or "\\" in value:
            raise ValueError("path must be relative and stay inside the repository")
        return path.as_posix()


@dataclass(frozen=True, slots=True)
class PinnedSource:
    url: str
    host: str
    port: int
    address: str
    revision: str


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [item[4][0] for item in infos]


def validate(
    request: GitBuildRequest,
    allowed_hosts: Iterable[str],
    *,
    resolver: Callable[[str, int], list[str]] = default_resolver,
) -> PinnedSource:
    """Admit a request or raise `GitSourceError`; never touches the network except DNS."""
    parts = urlsplit(request.repo_url)
    if parts.scheme != "https":
        raise GitSourceError("scheme", "only https repository URLs are allowed")
    if parts.username or parts.password or "@" in parts.netloc:
        raise GitSourceError("userinfo", "credentials must not be embedded in the URL; use credential_ref")
    if parts.query or parts.fragment:
        raise GitSourceError("url", "query strings and fragments are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not _HOST.fullmatch(host):
        raise GitSourceError("host", "invalid repository host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise GitSourceError("host", "IP literal hosts are not allowed")
    allowed = {item.lower().rstrip(".") for item in allowed_hosts}
    if host not in allowed:
        raise GitSourceError("host-not-allowed", f"{host} is not in the operator allow-list")
    port = parts.port or 443
    addresses = resolver(host, port)
    if not addresses:
        raise GitSourceError("dns", "host did not resolve")
    for address in addresses:
        if not _public(address):
            raise GitSourceError("address", f"{host} resolves to a non-public address")
    return PinnedSource(request.repo_url, host, port, addresses[0], request.revision)


def check_tree(root: Path, *, max_bytes: int = MAX_BYTES, max_files: int = MAX_FILES) -> tuple[int, int]:
    """Enforce size and file-count caps and refuse symlinks that escape the checkout."""
    total = files = 0
    base = root.resolve()
    for current, dirs, names in os.walk(root, followlinks=False):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in names:
            path = Path(current, name)
            if path.is_symlink():
                target = (path.parent / os.readlink(path)).resolve()
                if base != target and base not in target.parents:
                    raise GitSourceError("symlink", f"{path.relative_to(root)} points outside the checkout")
                continue
            files += 1
            total += path.stat().st_size
            if files > max_files or total > max_bytes:
                raise GitSourceError("size", "repository exceeds the file-count or byte limit")
    return total, files


def _git(args: list[str], cwd: Path, env: dict[str, str], runner) -> None:
    runner(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, timeout=FETCH_TIMEOUT)


def fetch(
    source: PinnedSource,
    dest: Path,
    *,
    runner=subprocess.run,
    allow_schemes: str = "https",
    max_bytes: int = MAX_BYTES,
    max_files: int = MAX_FILES,
) -> Path:
    """Fetch exactly `source.revision` with redirects off, connecting to the pinned address.

    `dest` must not exist. The `.git` directory is removed so it never enters the build
    context. Submodules and LFS are never initialised.
    """
    if dest.exists():
        raise GitSourceError("dest", "destination already exists")
    dest.mkdir(parents=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(dest.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": allow_schemes,
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    resolve = f"{source.host}:{source.port}:{source.address}"
    cfg = [
        "-c",
        "http.followRedirects=false",
        "-c",
        f"http.curloptResolve={resolve}",
        "-c",
        "protocol.version=2",
        "-c",
        "submodule.recurse=false",
    ]
    try:
        _git(["init", "-q"], dest, env, runner)
        _git([*cfg, "fetch", "-q", "--depth=1", "--no-tags", source.url, source.revision], dest, env, runner)
        _git(["checkout", "-q", "--detach", "FETCH_HEAD"], dest, env, runner)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=dest, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()
        if head != source.revision:
            raise GitSourceError("revision", "fetched commit does not match the pinned revision")
        check_tree(dest, max_bytes=max_bytes, max_files=max_files)
    except subprocess.SubprocessError as exc:
        raise GitSourceError("fetch", f"git failed: {type(exc).__name__}") from None
    except GitSourceError:
        raise
    import shutil

    shutil.rmtree(dest / ".git")
    return dest
