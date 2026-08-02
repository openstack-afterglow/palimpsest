"""Digest parsing and streaming verification for Palimpsest artifacts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .errors import DigestMismatchError, InvalidDigestError

_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


def normalize_digest(value: str) -> str:
    """Return a canonical lower-case ``sha256:<hex>`` digest."""
    match = _SHA256_RE.fullmatch(value)
    if match is None:
        raise InvalidDigestError(f"invalid sha256 digest: {value!r}")
    return f"sha256:{match.group(1).lower()}"


def require_digest(value: str) -> str:
    """Validate and canonicalize a SHA-256 digest."""
    return normalize_digest(value)


def digest_hex(value: str) -> str:
    """Return the hexadecimal portion of a validated digest."""
    return require_digest(value).split(":", 1)[1]


def digest_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file into SHA-256 without loading it into memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(chunk_size):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def require_file_digest(path: Path, expected: str) -> None:
    """Raise when ``path`` does not have ``expected`` SHA-256 bytes."""
    actual = digest_file(path)
    normalized = require_digest(expected)
    if actual != normalized:
        raise DigestMismatchError(f"digest mismatch for {path}: expected {normalized}, got {actual}")
