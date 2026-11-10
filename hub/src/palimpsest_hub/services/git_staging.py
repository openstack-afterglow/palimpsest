"""Stage a pinned Git revision as a content-addressed build-context blob.

Order of effects, each gated by the previous one:
1. an empty operator allow-list refuses before any DNS or network work (`GitStagingUnavailable`);
2. `git_source.validate` admits the request (https, allow-listed host, every resolved address
   public) and pins the first address;
3. `git_source.fetch` checks out exactly the pinned commit into a scratch directory under the
   store root, with redirects off and the address pinned, then bounds the tree;
4. the deterministic tar is written and promoted through `store.promote_file`, which verifies
   the digest and enforces `max_context_bytes`;
5. the scratch directory is removed on every path, success or failure.

The blob is not attached to any project or build row here. Ownership is decided by the route
that will expose this (M4-05), so this module never leaves an unowned row behind.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from palimpsest_hub.services import git_source
from palimpsest_hub.services.git_context import ContextError, write_context_tar
from palimpsest_hub.services.git_source import GitBuildRequest, GitSourceError, default_resolver
from palimpsest_hub.services.hub_store import LocalPathBlobStore
from palimpsest_hub.services.worker_leases import run_blocking


class GitStagingUnavailable(RuntimeError):
    """No hosts are allow-listed, so Git staging is switched off and nothing is contacted."""


@dataclass(frozen=True, slots=True)
class StagedGitContext:
    blob_digest: str
    size_bytes: int
    entries: int
    revision: str
    host: str


def _read_dockerignore(src: Path) -> list[str]:
    path = src / ".dockerignore"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        raise ContextError(".dockerignore is not valid UTF-8") from None


def _pack(src: Path, out: Path) -> tuple[str, int]:
    ignore = _read_dockerignore(src)
    with out.open("wb") as handle:
        return write_context_tar(src, handle, ignore_lines=ignore)


async def stage_git_context(
    request: GitBuildRequest,
    *,
    allowed_hosts: Iterable[str],
    store: LocalPathBlobStore,
    max_context_bytes: int,
    resolver: Callable[[str, int], list[str]] = default_resolver,
    runner=subprocess.run,
) -> StagedGitContext:
    hosts = tuple(allowed_hosts)
    if not hosts:
        raise GitStagingUnavailable("git staging has no allow-listed hosts")
    pinned = await run_blocking(git_source.validate, request, hosts, resolver=resolver)
    scratch = Path(await run_blocking(tempfile.mkdtemp, prefix="gitctx-", dir=store.uploads_dir))
    try:
        src = scratch / "src"
        await run_blocking(git_source.fetch, pinned, src, runner=runner)
        tar_path = scratch / "context.tar"
        digest, entries = await run_blocking(_pack, src, tar_path)
        finalized = await run_blocking(store.promote_file, tar_path, max_bytes=max_context_bytes)
        if finalized.blob_digest != f"sha256:{digest}":
            raise GitSourceError("digest", "staged blob digest does not match the packed context")
        return StagedGitContext(
            blob_digest=finalized.blob_digest,
            size_bytes=finalized.size_bytes,
            entries=entries,
            revision=pinned.revision,
            host=pinned.host,
        )
    finally:
        await run_blocking(shutil.rmtree, scratch, True)
