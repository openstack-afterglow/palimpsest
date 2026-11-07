from __future__ import annotations

import hashlib
import io
import os
import tarfile
import time

import pytest

from palimpsest_local.cloud_env import CloudEnvError, context_archive
from palimpsest_local.cloud_layer_worker import WorkerError, publish_blob, validate_name
from palimpsest_local.image_policy_channel import CurrentPolicyAuthority
from palimpsest_local.image_source_policy import ImageSourcePolicy, ImageSourceRule


def _commit():
    policy = ImageSourcePolicy((ImageSourceRule("docker.io", "nvidia", "cuda"),))
    CurrentPolicyAuthority(policy, lambda: policy)("payload", time.monotonic() + 60)


def _store(tmp_path):
    store = tmp_path / "store"
    (store / "blobs" / "sha256").mkdir(parents=True)
    (store / ".incoming").mkdir()
    return store


def _layer(tmp_path, name, payload):
    path = tmp_path / name
    path.write_bytes(payload)
    return path, "sha256:" + hashlib.sha256(payload).hexdigest()


def test_publish_is_write_once_and_read_only(tmp_path):
    store = _store(tmp_path)
    source, digest = _layer(tmp_path, "layer", b"squashfs-bytes")

    first = publish_blob(source, digest, store, before_commit=_commit)
    second = publish_blob(source, digest, store, before_commit=_commit)

    target = store / "blobs" / "sha256" / digest.split(":")[1]
    assert first["published"] == "new" and second["published"] == "existing"
    assert target.read_bytes() == b"squashfs-bytes"
    assert target.stat().st_mode & 0o777 == 0o444
    assert list((store / ".incoming").iterdir()) == []


def test_publish_refuses_to_replace_a_differing_existing_blob(tmp_path):
    store = _store(tmp_path)
    source, digest = _layer(tmp_path, "layer", b"expected")
    target = store / "blobs" / "sha256" / digest.split(":")[1]
    target.write_bytes(b"tampered")

    with pytest.raises(WorkerError, match="differs"):
        publish_blob(source, digest, store, before_commit=_commit)
    assert target.read_bytes() == b"tampered"


def test_publish_rejects_bytes_that_do_not_match_the_claimed_digest(tmp_path):
    store = _store(tmp_path)
    source, _ = _layer(tmp_path, "layer", b"actual")
    wrong = "sha256:" + hashlib.sha256(b"claimed").hexdigest()

    with pytest.raises(WorkerError, match="verification"):
        publish_blob(source, wrong, store, before_commit=_commit)
    assert list((store / "blobs" / "sha256").iterdir()) == []
    assert list((store / ".incoming").iterdir()) == []


@pytest.mark.parametrize("name", ["", "-lead", "Upper", "has space", "a/b", "x" * 129])
def test_image_names_are_restricted(name):
    with pytest.raises(WorkerError):
        validate_name(name)


def test_context_archive_excludes_git_and_simple_dockerignore_entries(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "token").write_text("x")
    (tmp_path / "app.py").write_text("print(1)")
    (tmp_path / ".dockerignore").write_text("# comment\nsecrets/\n")

    with tarfile.open(fileobj=io.BytesIO(context_archive(tmp_path))) as archive:
        names = set(archive.getnames())

    assert {"app.py", ".dockerignore"} <= names
    assert not any(name == ".git" or name.startswith((".git/", "secrets")) for name in names)


@pytest.mark.parametrize("pattern", ["*.log", "!secrets", "**/token", "a/../token", r"escaped\ name"])
def test_context_archive_rejects_unsupported_ignores_before_returning_payload(tmp_path, pattern):
    (tmp_path / ".dockerignore").write_text(pattern + "\n")
    (tmp_path / "secret.log").write_text("must not transfer")
    with pytest.raises(CloudEnvError, match="supported subset"):
        context_archive(tmp_path)


@pytest.mark.parametrize("kind", ["file", "empty", "symlink", "dangling", "directory", "fifo"])
def test_context_archive_rejects_root_plmignore_before_returning_payload(tmp_path, kind):
    (tmp_path / "secret.txt").write_text("must not transfer")
    ignore = tmp_path / ".plmignore"
    if kind == "file":
        ignore.write_text("secret.txt\n")
    elif kind == "empty":
        ignore.touch()
    elif kind in {"symlink", "dangling"}:
        ignore.symlink_to("secret.txt" if kind == "symlink" else "missing")
    elif kind == "directory":
        ignore.mkdir()
    else:
        os.mkfifo(ignore)
    # Even excluding the unsupported ignore file itself must not hide its presence.
    (tmp_path / ".dockerignore").write_text(".plmignore\n")

    with pytest.raises(CloudEnvError, match=r"\.plmignore is unsupported for cloud env remote build contexts"):
        context_archive(tmp_path)


def test_context_archive_normalizes_regular_hardlinks(tmp_path):
    (tmp_path / "first").write_bytes(b"context")
    os.link(tmp_path / "first", tmp_path / "second")
    with tarfile.open(fileobj=io.BytesIO(context_archive(tmp_path))) as archive:
        assert all(item.isfile() for item in archive.getmembers())
        assert archive.extractfile("first").read() == archive.extractfile("second").read() == b"context"
