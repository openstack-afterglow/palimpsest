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
    dockerfile: str  # path inside the blob, relative to the build context


def _real_dir_inside(checkout: Path, rel: str) -> Path:
    path = checkout / rel
    if path.is_symlink() or not path.is_dir():
        raise GitSourceError("context", "context_subdir must be a real directory in the checkout")
    if not path.resolve(strict=True).is_relative_to(checkout.resolve(strict=True)):
        raise GitSourceError("context", "context_subdir escapes the checkout")
    return path


def _check_dockerfile(checkout: Path, context: Path, dockerfile_path: str) -> str:
    path = checkout / dockerfile_path
    if path.is_symlink() or not path.is_file():
        raise GitSourceError("dockerfile", "dockerfile_path must be a regular file in the checkout")
    if not path.resolve(strict=True).is_relative_to(context.resolve(strict=True)):
        raise GitSourceError("dockerfile", "dockerfile_path must lie inside the build context")
    return path.resolve(strict=True).relative_to(context.resolve(strict=True)).as_posix()


def _read_dockerignore(context: Path) -> list[str]:
    path = context / ".dockerignore"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        raise ContextError(".dockerignore is not valid UTF-8") from None


def _pack(context: Path, out: Path, dockerfile: str) -> tuple[str, int]:
    ignore = _read_dockerignore(context)
    with out.open("wb") as handle:
        digest, entries = write_context_tar(context, handle, ignore_lines=ignore)
    with tarfile.open(out) as tar:
        if dockerfile not in tar.getnames():
            # Docker always sends the Dockerfile; a blob without it cannot build, so refuse here.
            raise GitSourceError("dockerfile", ".dockerignore removes the Dockerfile from the context")
    return digest, entries


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
    if request.credential_ref is not None:
        raise GitSourceError("credential", "credential_ref is not supported yet; refusing to fetch anonymously")
    pinned = await run_blocking(git_source.validate, request, hosts, resolver=resolver)
    scratch = Path(await run_blocking(tempfile.mkdtemp, prefix="gitctx-", dir=store.uploads_dir))
    try:
        checkout = scratch / "src"
        await run_blocking(git_source.fetch, pinned, checkout, runner=runner)
        context = await run_blocking(_real_dir_inside, checkout, request.context_subdir)
        dockerfile = await run_blocking(_check_dockerfile, checkout, context, request.dockerfile_path)
        tar_path = scratch / "context.tar"
        digest, entries = await run_blocking(_pack, context, tar_path, dockerfile)
        finalized = await run_blocking(store.promote_file, tar_path, max_bytes=max_context_bytes)
        if finalized.blob_digest != f"sha256:{digest}":
            raise GitSourceError("digest", "staged blob digest does not match the packed context")
        return StagedGitContext(
            blob_digest=finalized.blob_digest,
            size_bytes=finalized.size_bytes,
            entries=entries,
            revision=pinned.revision,
            host=pinned.host,
            dockerfile=dockerfile,
        )
    finally:
        await run_blocking(shutil.rmtree, scratch, True)
