"""Stage a pinned Git revision as a content-addressed build-context blob.

Order of effects, each gated by the previous one:
1. an empty operator allow-list refuses before any DNS or network work (`GitStagingUnavailable`);
2. a set `credential_ref` is refused: private-Git credentials are not implemented yet, and
   silently fetching anonymously would build something other than what was asked for;
3. `git_source.validate` admits the request (https, allow-listed host, every resolved address
   public) and pins the first address;
4. `git_source.fetch` checks out exactly the pinned commit into a scratch directory under the
   store root, with redirects off and the address pinned, then bounds the tree;
5. the build context is `context_subdir` of the checkout: it must be a real directory inside the
   checkout, and `dockerfile_path` must be a regular file inside that context (otherwise the blob
   could not build it); the context's own `.dockerignore` applies;
6. the deterministic tar is promoted through `store.promote_file`, which verifies the digest and
   enforces `max_context_bytes`;
7. the scratch directory is removed on every path, success or failure.

The blob is not attached to any project or build row here. Ownership is decided by the route
that will expose this (M4-05), so this module never leaves an unowned row behind.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from palimpsest_hub.services import git_source
from palimpsest_hub.services.git_context import ContextError, write_context_tar
from palimpsest_hub.services.git_source import GitBuildRequest, GitSourceError, default_resolver
from palimpsest_hub.services.hub_store import LocalPathBlobStore
from palimpsest_hub.services.worker_leases import run_blocking

STALE_SCRATCH_SECONDS = 3600.0
# Defaults mirror the Hub settings palimpsest_hub_max_context_members / _expanded_bytes.
DEFAULT_MAX_MEMBERS = 10000
DEFAULT_MAX_EXPANDED_BYTES = 256 * 1024**2


class GitStagingUnavailable(RuntimeError):
    """No hosts are allow-listed, so Git staging is switched off and nothing is contacted."""


@dataclass(frozen=True, slots=True)
class StagedGitContext:
    blob_digest: str
    size_bytes: int
    entries: int
    revision: str
    host: str
    dockerfile_text: str  # the Dockerfile body, sent inline to the image job


def sweep_stale_scratch(uploads_dir: Path, *, now: float, max_age: float = STALE_SCRATCH_SECONDS) -> int:
    """Remove `gitctx-*` scratch trees left by a process that died mid-stage.

    A live stage keeps its directory's mtime fresh by writing into it, so an hour without
    activity means no live stage owns it. Anything else under `uploads/` is left alone.
    """
    if not uploads_dir.is_dir():
        return 0
    removed = 0
    for entry in uploads_dir.iterdir():
        if not entry.name.startswith("gitctx-") or entry.is_symlink() or not entry.is_dir():
            continue
        if now - entry.stat().st_mtime > max_age:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def _real_dir_inside(checkout: Path, rel: str) -> Path:
    path = checkout / rel
    if path.is_symlink() or not path.is_dir():
        raise GitSourceError("context", "context_subdir must be a real directory in the checkout")
    if not path.resolve(strict=True).is_relative_to(checkout.resolve(strict=True)):
        raise GitSourceError("context", "context_subdir escapes the checkout")
    return path


def _check_dockerfile(checkout: Path, dockerfile_path: str) -> None:
    # The worker writes the admitted Dockerfile text itself and never reads one from the context,
    # so the file only has to exist in the checkout to be read.
    path = checkout / dockerfile_path
    if path.is_symlink() or not path.is_file():
        raise GitSourceError("dockerfile", "dockerfile_path must be a regular file in the checkout")
    # A symlinked parent directory can point outside the checkout; check the resolved location.
    if not path.resolve(strict=True).is_relative_to(checkout.resolve(strict=True)):
        raise GitSourceError("dockerfile", "dockerfile_path resolves outside the checkout")


def _read_dockerignore(context: Path) -> list[str]:
    path = context / ".dockerignore"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        raise ContextError(".dockerignore is not valid UTF-8") from None


MAX_DOCKERFILE_BYTES = 1024 * 1024


def _read_dockerfile(checkout: Path, dockerfile_path: str) -> str:
    path = checkout / dockerfile_path
    if path.stat().st_size > MAX_DOCKERFILE_BYTES:
        raise GitSourceError("dockerfile", "Dockerfile exceeds the byte limit")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise GitSourceError("dockerfile", "Dockerfile is not valid UTF-8") from None


def _pack(context: Path, out: Path, max_members: int, max_expanded_bytes: int) -> tuple[str, int]:
    ignore = _read_dockerignore(context)
    with out.open("wb") as handle:
        digest, entries = write_context_tar(context, handle, ignore_lines=ignore)
    # Mirror the worker's extract_context limits so an oversized repo fails here, not in the worker.
    with tarfile.open(out) as tar:
        members = tar.getmembers()
    expanded = sum(member.size for member in members)
    if len(members) > max_members or expanded > max_expanded_bytes:
        raise GitSourceError("context", "build context exceeds the worker's member or byte limit")
    return digest, entries


async def stage_git_context(
    request: GitBuildRequest,
    *,
    allowed_hosts: Iterable[str],
    store: LocalPathBlobStore,
    max_context_bytes: int,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    resolver: Callable[[str, int], list[str]] = default_resolver,
    runner=None,
) -> StagedGitContext:
    hosts = tuple(allowed_hosts)
    if not hosts:
        raise GitStagingUnavailable("git staging has no allow-listed hosts")
    if request.credential_ref is not None:
        raise GitSourceError("credential", "credential_ref is not supported yet; refusing to fetch anonymously")
    run = subprocess.run if runner is None else runner  # looked up at call time, not import time
    pinned = await run_blocking(git_source.validate, request, hosts, resolver=resolver)
    await run_blocking(lambda: store.uploads_dir.mkdir(parents=True, exist_ok=True))
    await run_blocking(sweep_stale_scratch, store.uploads_dir, now=time.time())
    scratch = Path(await run_blocking(tempfile.mkdtemp, prefix="gitctx-", dir=store.uploads_dir))
    try:
        checkout = scratch / "src"
        await run_blocking(git_source.fetch, pinned, checkout, runner=run)
        context = await run_blocking(_real_dir_inside, checkout, request.context_subdir)
        await run_blocking(_check_dockerfile, checkout, request.dockerfile_path)
        dockerfile_text = await run_blocking(_read_dockerfile, checkout, request.dockerfile_path)
        tar_path = scratch / "context.tar"
        digest, entries = await run_blocking(_pack, context, tar_path, max_members, max_expanded_bytes)
        finalized = await run_blocking(store.promote_file, tar_path, max_bytes=max_context_bytes)
        if finalized.blob_digest != f"sha256:{digest}":
            raise GitSourceError("digest", "staged blob digest does not match the packed context")
        return StagedGitContext(
            blob_digest=finalized.blob_digest,
            size_bytes=finalized.size_bytes,
            entries=entries,
            revision=pinned.revision,
            host=pinned.host,
            dockerfile_text=dockerfile_text,
        )
    finally:
        await run_blocking(shutil.rmtree, scratch, True)
