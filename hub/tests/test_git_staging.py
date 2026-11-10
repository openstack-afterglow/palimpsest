from __future__ import annotations

import subprocess

import pytest

from palimpsest_hub.services import git_source
from palimpsest_hub.services.git_source import GitBuildRequest, GitSourceError
from palimpsest_hub.services.git_staging import GitStagingUnavailable, stage_git_context
from palimpsest_hub.services.hub_store import HubStoreError, LocalPathBlobStore

SHA = "a" * 40
URL = "https://github.com/o/r.git"
PUBLIC = ["140.82.112.3"]


class Runner:
    """Stands in for the git binary: `checkout` materialises files; every call is recorded."""

    def __init__(self, files=None, fail_fetch=False):
        self.files = files if files is not None else {"Dockerfile": "FROM scratch\n", "src/app.py": "x\n"}
        self.fail_fetch = fail_fetch
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd=None, **_):
        self.calls.append(list(args))
        if "fetch" in args and self.fail_fetch:
            raise subprocess.CalledProcessError(128, args)
        if "init" in args:
            (cwd / ".git").mkdir()
        if "checkout" in args:
            for rel, text in self.files.items():
                target = cwd / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def store(tmp_path):
    s = LocalPathBlobStore(tmp_path / "root")
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    s.blobs_dir.mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def head(monkeypatch):
    """fetch verifies HEAD with a direct subprocess call; make it report the pinned commit."""
    state = {"head": SHA}
    real = subprocess.run

    def fake(args, *a, **kw):
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, state["head"] + "\n", "")
        return real(args, *a, **kw)

    monkeypatch.setattr(git_source.subprocess, "run", fake)
    return state


def request(**extra):
    return GitBuildRequest(repo_url=URL, revision=SHA, **extra)


def blobs(store):
    return sorted(p.name for p in store.blobs_dir.rglob("*") if p.is_file())


def scratch_dirs(store):
    return sorted(p.name for p in store.uploads_dir.glob("gitctx-*"))


async def test_an_empty_allow_list_refuses_before_any_network_or_git_call(store):
    runner = Runner()
    with pytest.raises(GitStagingUnavailable):
        await stage_git_context(
            request(),
            allowed_hosts=[],
            store=store,
            max_context_bytes=10**6,
            resolver=lambda h, p: PUBLIC,
            runner=runner,
        )
    assert runner.calls == []


async def test_a_host_outside_the_allow_list_is_refused_without_git(store):
    runner = Runner()
    with pytest.raises(GitSourceError) as info:
        await stage_git_context(
            request(),
            allowed_hosts=["gitlab.example"],
            store=store,
            max_context_bytes=10**6,
            resolver=lambda h, p: PUBLIC,
            runner=runner,
        )
    assert info.value.code == "host-not-allowed"
    assert runner.calls == []


async def test_a_host_resolving_to_a_private_address_is_refused_without_git(store):
    runner = Runner()
    with pytest.raises(GitSourceError) as info:
        await stage_git_context(
            request(),
            allowed_hosts=["github.com"],
            store=store,
            max_context_bytes=10**6,
            resolver=lambda h, p: ["10.0.0.5"],
            runner=runner,
        )
    assert info.value.code == "address"
    assert runner.calls == []


async def test_a_verified_revision_is_staged_as_a_content_addressed_blob(store, head):
    runner = Runner()
    result = await stage_git_context(
        request(),
        allowed_hosts=["github.com"],
        store=store,
        max_context_bytes=10**6,
        resolver=lambda h, p: PUBLIC,
        runner=runner,
    )
    assert result.revision == SHA
    assert result.blob_digest.startswith("sha256:")
    assert result.entries >= 2
    assert store.exists(result.blob_digest)
    assert scratch_dirs(store) == []


async def test_dockerignore_in_the_checkout_is_honoured_and_git_metadata_never_enters(store, head):
    runner = Runner(files={"Dockerfile": "FROM scratch\n", "tmp/scratch.txt": "drop\n", ".dockerignore": "tmp/\n"})
    result = await stage_git_context(
        request(),
        allowed_hosts=["github.com"],
        store=store,
        max_context_bytes=10**6,
        resolver=lambda h, p: PUBLIC,
        runner=runner,
    )
    assert result.entries == 2  # Dockerfile and .dockerignore; tmp/ is dropped
    assert scratch_dirs(store) == []


async def test_a_checkout_at_a_different_commit_is_refused_and_publishes_nothing(store, head):
    head["head"] = "b" * 40
    before = blobs(store)
    with pytest.raises(GitSourceError) as info:
        await stage_git_context(
            request(),
            allowed_hosts=["github.com"],
            store=store,
            max_context_bytes=10**6,
            resolver=lambda h, p: PUBLIC,
            runner=Runner(),
        )
    assert info.value.code == "revision"
    assert blobs(store) == before
    assert scratch_dirs(store) == []


async def test_a_failed_fetch_is_reported_and_leaves_no_scratch_directory(store, head):
    with pytest.raises(GitSourceError) as info:
        await stage_git_context(
            request(),
            allowed_hosts=["github.com"],
            store=store,
            max_context_bytes=10**6,
            resolver=lambda h, p: PUBLIC,
            runner=Runner(fail_fetch=True),
        )
    assert info.value.code == "fetch"
    assert scratch_dirs(store) == []


async def test_a_context_over_the_byte_limit_is_refused_and_leaves_no_scratch_directory(store, head):
    before = blobs(store)
    with pytest.raises(HubStoreError):
        await stage_git_context(
            request(),
            allowed_hosts=["github.com"],
            store=store,
            max_context_bytes=64,
            resolver=lambda h, p: PUBLIC,
            runner=Runner(),
        )
    assert scratch_dirs(store) == []
    assert blobs(store) == before
