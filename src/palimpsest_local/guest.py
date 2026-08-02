"""Guest SSH/SCP command construction for Palimpsest KVM runs.

Pure argv builders — stdlib only, no libvirt, no shell composition. Every guest
interaction returns a ``list[str]`` argument vector; callers invoke it (e.g. via
``subprocess.run(..., shell=False)``), they never receive or build a shell string.
``shell`` is the only interactive path — it supplies no remote command, so a real
login shell attaches. ``exec`` instead forwards a caller-supplied argv through
``/usr/local/libexec/palimpsest-exec`` (installed by
:mod:`palimpsest_local.cloudinit`) as a single alphabet-constrained base64url
payload: OpenSSH still hands the remote command line to the guest's login shell for
parsing, but since that line is always exactly ``<helper path> <payload>`` with no
spaces, quotes, or metacharacters in either token, there is nothing for that shell to
misinterpret.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from pathlib import Path

from .cloudinit import EXEC_HELPER_PATH, GUEST_USER, read_public_key_line
from .errors import LifecycleError as GuestError

SSH_BINARY = "ssh"
SCP_BINARY = "scp"
_FORBIDDEN_HOST_CHARS = frozenset(" \t\r\n\x00")
# Absolute, metacharacter-free paths — `.`/`..` segments are rejected separately
# below (a charset alone can't exclude them). scp's remote endpoint is
# `user@host:path`, and pre-9.0 OpenSSH's legacy SCP protocol hands that path to a
# remote shell, so a permissive charset here would reopen the shell-injection
# surface `exec` avoids.
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z")


def _valid_host(host: str) -> str:
    if not host or any(ch in _FORBIDDEN_HOST_CHARS for ch in host):
        raise GuestError(f"invalid guest host: {host!r}")
    return host


def _valid_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise GuestError(f"port is outside the valid range: {port}")
    return port


def _require_absolute(label: str, path: Path) -> Path:
    if not path.is_absolute():
        raise GuestError(f"{label} must be an absolute path: {path}")
    return path


def build_known_hosts_entry(host: str, host_public_key: Path | str, *, port: int = 22) -> str:
    """A single deterministic ``known_hosts`` line pinning the run's guest host key.

    OpenSSH looks up a non-default port under the bracketed ``[host]:port`` form, so
    the entry must match whatever ``port`` the accompanying ``build_*_command`` call
    uses or StrictHostKeyChecking will reject the connection.
    """
    valid_host = _valid_host(host)
    address = valid_host if _valid_port(port) == 22 else f"[{valid_host}]:{port}"
    return f"{address} {read_public_key_line(host_public_key)}\n"


def _common_options(*, identity: Path, known_hosts: Path) -> list[str]:
    """Shared ``-i``/``-o`` flags for every ssh(1)/scp(1) invocation against a guest."""
    identity = _require_absolute("identity", identity)
    known_hosts = _require_absolute("known_hosts", known_hosts)
    return [
        "-i",
        str(identity),
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
    ]


def _ssh_options(*, identity: Path, known_hosts: Path, port: int) -> list[str]:
    return [*_common_options(identity=identity, known_hosts=known_hosts), "-p", str(_valid_port(port))]


def build_shell_command(
    host: str,
    *,
    identity: Path,
    known_hosts: Path,
    port: int = 22,
) -> list[str]:
    """Interactive SSH argv; supplies no remote command, so a real login shell attaches."""
    return [
        SSH_BINARY,
        "-tt",
        *_ssh_options(identity=identity, known_hosts=known_hosts, port=port),
        f"{GUEST_USER}@{_valid_host(host)}",
    ]


def encode_exec_payload(argv: Sequence[str]) -> str:
    """Encode ``argv`` as the URL-safe base64 JSON payload the guest helper decodes.

    Padding is stripped: the payload alphabet is exactly ``[A-Za-z0-9_-]``, so it
    never needs quoting when it becomes an SSH remote-command token.
    """
    tokens = list(argv)
    if not tokens:
        raise GuestError("exec requires a nonempty argv")
    for token in tokens:
        if not isinstance(token, str) or "\x00" in token:
            raise GuestError("exec argv must be NUL-free strings")
    raw = json.dumps(tokens, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_exec_command(
    host: str,
    argv: Sequence[str],
    *,
    identity: Path,
    known_hosts: Path,
    port: int = 22,
) -> list[str]:
    """Non-interactive SSH argv forwarding ``argv`` through the base64 exec helper."""
    payload = encode_exec_payload(argv)
    return [
        SSH_BINARY,
        *_ssh_options(identity=identity, known_hosts=known_hosts, port=port),
        f"{GUEST_USER}@{_valid_host(host)}",
        EXEC_HELPER_PATH,
        payload,
    ]


def build_scp_download_command(
    host: str,
    remote_path: str,
    local_path: Path,
    *,
    identity: Path,
    known_hosts: Path,
    port: int = 22,
) -> list[str]:
    """SCP argv copying ``remote_path`` on the guest to ``local_path`` on the host.

    Forces the SFTP subsystem (``-s``, OpenSSH >= 8.7) so the transfer never falls
    back to the legacy SCP wire protocol, which historically let a crafted remote
    path reach a shell on the server side (CVE-2020-15778); ``remote_path`` is
    additionally restricted to an absolute, traversal-free, metacharacter-free path.
    """
    segments = remote_path.split("/")
    if not _REMOTE_PATH_RE.fullmatch(remote_path) or any(segment in (".", "..") for segment in segments):
        raise GuestError(f"invalid remote path: {remote_path!r}")
    return [
        SCP_BINARY,
        "-s",
        *_common_options(identity=identity, known_hosts=known_hosts),
        "-P",
        str(_valid_port(port)),
        f"{GUEST_USER}@{_valid_host(host)}:{remote_path}",
        str(local_path),
    ]
