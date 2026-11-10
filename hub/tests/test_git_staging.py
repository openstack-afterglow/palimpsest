from __future__ import annotations

import os
import subprocess
import tarfile

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

    def __init__(self, files=None, fail_fetch=False, links=None):
        self.files = files if files is not None else {"Dockerfile": "FROM scratch\n", "src/app.py": "x\n"}
        self.fail_fetch = fail_fetch
        self.links = links or {}
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
            for rel, target_path in self.links.items():
                (cwd / rel).parent.mkdir(parents=True, exist_ok=True)
                os.symlink(target_path, cwd / rel)
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


async def _stage(store, runner, **extra):
    return await stage_git_context(
        request(**extra),
        allowed_hosts=["github.com"],
        store=store,
        max_context_bytes=10**6,
        resolver=lambda h, p: PUBLIC,
        runner=runner,
    )


def tar_names(store, digest):
    with store.open_read(digest) as handle, tarfile.open(fileobj=handle) as tar:
        return sorted(m.name for m in tar.getmembers())


async def test_context_subdir_packs_only_that_directory(store, head):
    runner = Runner(
        files={"Dockerfile": "x\n", "app/Dockerfile": "FROM scratch\n", "app/main.py": "y\n", "other.txt": "z\n"}
    )
    result = await _stage(store, runner, context_subdir="app", dockerfile_path="app/Dockerfile")
    names = tar_names(store, result.blob_digest)
    assert "app/Dockerfile" not in names and "app/main.py" not in names
    assert "Dockerfile" in names and "main.py" in names
    assert "other.txt" not in names


async def test_context_subdir_uses_its_own_dockerignore(store, head):
    runner = Runner(
        files={
            "app/Dockerfile": "FROM scratch\n",
            "app/tmp/junk": "x\n",
            "app/.dockerignore": "tmp/\n",
            "tmp/root-kept": "k\n",
        }
    )
    result = await _stage(store, runner, context_subdir="app", dockerfile_path="app/Dockerfile")
    names = tar_names(store, result.blob_digest)
    assert not any(n.startswith("tmp") for n in names)


async def test_a_context_subdir_that_is_missing_or_a_file_is_refused(store, head):
    with pytest.raises(GitSourceError) as info:
        await _stage(store, Runner(), context_subdir="nope")
    assert info.value.code == "context"
    with pytest.raises(GitSourceError) as info:
        await _stage(store, Runner(files={"Dockerfile": "x\n", "app": "not a dir\n"}), context_subdir="app")
    assert info.value.code == "context"
    assert scratch_dirs(store) == []


async def test_a_context_subdir_symlink_out_of_the_checkout_is_refused(store, head):
    runner = Runner(links={"app": "/etc"})
    with pytest.raises(GitSourceError) as info:
        await _stage(store, runner, context_subdir="app")
    assert info.value.code == "context"
    assert scratch_dirs(store) == []


async def test_a_missing_dockerfile_is_refused(store, head):
    with pytest.raises(GitSourceError) as info:
        await _stage(store, Runner(files={"src.txt": "x\n"}), dockerfile_path="Missing")
    assert info.value.code == "dockerfile"
    assert scratch_dirs(store) == []


async def test_a_symlinked_dockerfile_is_refused(store, head):
    runner = Runner(files={"real.txt": "FROM scratch\n"}, links={"Dockerfile": "real.txt"})
    with pytest.raises(GitSourceError) as info:
        await _stage(store, runner)
    assert info.value.code == "dockerfile"


async def test_a_credential_reference_is_refused_before_any_git_call(store, head):
    runner = Runner()
    with pytest.raises(GitSourceError) as info:
        await _stage(store, runner, credential_ref="deploy-key")
    assert info.value.code == "credential"
    assert runner.calls == []


async def test_a_missing_git_binary_is_a_fetch_refusal_not_a_server_error(store, head):
    def no_git(args, *a, **kw):
        raise FileNotFoundError("git")

    with pytest.raises(GitSourceError) as info:
        await stage_git_context(
            request(),
            allowed_hosts=["github.com"],
            store=store,
            max_context_bytes=10**6,
            resolver=lambda h, p: PUBLIC,
            runner=no_git,
        )
    assert info.value.code == "fetch"
    assert scratch_dirs(store) == []


async def test_stale_scratch_from_a_dead_process_is_swept_while_live_and_foreign_dirs_stay(store, head):
    import os
    import time

    old = time.time() - 7200
    dead = store.uploads_dir / "gitctx-dead"
    dead.mkdir()
    (dead / "partial").write_text("x")
    os.utime(dead, (old, old))
    live = store.uploads_dir / "gitctx-live"
    live.mkdir()
    foreign = store.uploads_dir / "other-old"
    foreign.mkdir()
    os.utime(foreign, (old, old))
    await _stage(store, Runner())
    assert not dead.exists()
    assert live.exists()
    assert foreign.exists()


async def test_a_dockerignore_that_drops_the_dockerfile_still_stages_the_inline_text(store, head):
    runner = Runner(files={"Dockerfile": "FROM scratch\n", "keep.txt": "k\n", ".dockerignore": "Dockerfile\n"})
    result = await _stage(store, runner)
    assert result.dockerfile_text == "FROM scratch\n"
    names = tar_names(store, result.blob_digest)
    assert "Dockerfile" not in names and "keep.txt" in names


async def test_a_dockerfile_outside_the_context_still_stages_because_the_worker_writes_it_inline(store, head):
    runner = Runner(files={"Dockerfile": "FROM scratch\n", "app/main.py": "y\n"})
    result = await _stage(store, runner, context_subdir="app", dockerfile_path="Dockerfile")
    assert result.dockerfile_text == "FROM scratch\n"
    assert tar_names(store, result.blob_digest) == ["main.py"]


async def test_a_context_over_the_worker_member_limit_is_refused_before_publication(store, head):
    before = blobs(store)
    with pytest.raises(GitSourceError) as info:
        await stage_git_context(
            request(),
            allowed_hosts=["github.com"],
            store=store,
            max_context_bytes=10**6,
            max_members=1,
            resolver=lambda h, p: PUBLIC,
            runner=Runner(),
        )
    assert info.value.code == "context"
    assert blobs(store) == before
    assert scratch_dirs(store) == []


async def test_a_context_over_the_worker_expanded_byte_limit_is_refused(store, head):
    with pytest.raises(GitSourceError) as info:
        await stage_git_context(
            request(),
            allowed_hosts=["github.com"],
            store=store,
            max_context_bytes=10**6,
            max_expanded_bytes=4,
            resolver=lambda h, p: PUBLIC,
            runner=Runner(),
        )
    assert info.value.code == "context"
