"""Source transfer must never include unrelated dirty work or private configuration."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "package_native_afterglow", Path(__file__).resolve().parents[2] / "scripts/package_native_afterglow.py"
)
assert SPEC is not None and SPEC.loader is not None
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def git(repository: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repository, text=True).strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "--quiet")
    for name, contents in {
        "Dockerfile": "FROM baseline\n",
        "frontend/app.txt": "authorized source\n",
        "afterglow.conf": "PRIVATE_ROOT_CONFIG\n",
        ".env.production": "PRIVATE_ENV\n",
        "backend/tests/integration/credentials.toml": "PRIVATE_INTEGRATION_CONFIG\n",
        "frontend/.env.test": "PRIVATE_NESTED_ENV\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    git(root, "add", "-f", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    return root, git(root, "rev-parse", "HEAD")


def contents(path):
    with tarfile.open(path) as archive:
        return {entry.name: archive.extractfile(entry).read() for entry in archive if entry.isfile()}


def test_pinned_commit_plus_explicit_native_overlay_excludes_foreign_dirty_source_and_secrets(repository, tmp_path):
    root, ref = repository
    (root / "frontend/app.txt").write_text("unapproved foreign edit\n")
    (root / "foreign.txt").write_text("unapproved untracked source\n")
    (root / "Dockerfile").write_text("FROM native\n")
    output = tmp_path / "source.tar.gz"
    manifest = helper.package(root, ref, ["Dockerfile"], output)
    unpacked = contents(output)
    assert unpacked == {"Dockerfile": b"FROM native\n", "frontend/app.txt": b"authorized source\n"}
    assert manifest["afterglow_ref"] == ref
    assert manifest["bundle_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert {record["path"]: record["sha256"] for record in manifest["files"]} == {
        name: hashlib.sha256(value).hexdigest() for name, value in unpacked.items()
    }


@pytest.mark.parametrize("overlay", ["frontend/app.txt", "afterglow.conf", "../Dockerfile"])
def test_unapproved_overlays_fail_before_publishing(repository, tmp_path, overlay):
    root, ref = repository
    output = tmp_path / "forbidden.tar.gz"
    with pytest.raises(ValueError):
        helper.package(root, ref, [overlay], output)
    assert not output.exists()


def test_native_overlay_cannot_follow_a_host_symlink(repository, tmp_path):
    root, ref = repository
    scripts = root / "backend/scripts"
    scripts.mkdir(parents=True)
    private = tmp_path / "private.py"
    private.write_text("PRIVATE_OUTSIDE_SOURCE\n")
    (scripts / "native_vm.py").symlink_to(private)
    with pytest.raises(ValueError, match="Symlink"):
        helper.package(root, ref, ["backend/scripts/native_vm.py"], tmp_path / "symlink.tar.gz")


def test_repeated_approved_inputs_produce_the_same_transfer_digest(repository, tmp_path):
    root, ref = repository
    first = helper.package(root, ref, [], tmp_path / "first.tar.gz")
    second = helper.package(root, ref, [], tmp_path / "second.tar.gz")
    assert first["bundle_sha256"] == second["bundle_sha256"]


@pytest.mark.parametrize("ref", ["HEAD", "dev", "ff007ed"])
def test_source_transfer_requires_full_immutable_sha(repository, tmp_path, ref):
    root, _ = repository
    with pytest.raises(ValueError, match="immutable commit SHA"):
        helper.package(root, ref, [], tmp_path / "moving-ref.tar.gz")


def test_git_replacement_cannot_change_the_approved_baseline(repository, tmp_path):
    root, ref = repository
    (root / "frontend/app.txt").write_text("unapproved replacement tree\n")
    git(root, "add", "frontend/app.txt")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "replacement",
    )
    replacement = git(root, "rev-parse", "HEAD")
    git(root, "replace", ref, replacement)
    output = tmp_path / "original.tar.gz"
    helper.package(root, ref, [], output)
    assert contents(output)["frontend/app.txt"] == b"authorized source\n"


def test_overlay_leaf_substitution_fails_without_archiving_private_bytes(repository, tmp_path, monkeypatch):
    root, ref = repository
    private = tmp_path / "private.conf"
    private.write_text("PRIVATE_SUBSTITUTION\n")
    original_popen = subprocess.Popen

    def substitute(*args, **kwargs):
        if "archive" in args[0]:
            (root / "Dockerfile").unlink()
            (root / "Dockerfile").symlink_to(private)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(helper.subprocess, "Popen", substitute)
    output = tmp_path / "leaf-substitution.tar.gz"
    with pytest.raises(RuntimeError, match="Overlay changed"):
        helper.package(root, ref, ["Dockerfile"], output)
    assert not output.exists()


def test_overlay_ancestor_substitution_uses_the_verified_descriptor(repository, tmp_path, monkeypatch):
    root, ref = repository
    scripts = root / "backend/scripts"
    scripts.mkdir(parents=True)
    (scripts / "native_vm.py").write_text("reviewed native bytes\n")
    private = tmp_path / "private-directory"
    private.mkdir()
    (private / "native_vm.py").write_text("PRIVATE_SUBSTITUTION\n")
    original_popen = subprocess.Popen

    def substitute(*args, **kwargs):
        if "archive" in args[0]:
            scripts.rename(root / "backend/old-scripts")
            scripts.symlink_to(private, target_is_directory=True)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(helper.subprocess, "Popen", substitute)
    output = tmp_path / "ancestor-substitution.tar.gz"
    helper.package(root, ref, ["backend/scripts/native_vm.py"], output)
    assert contents(output)["backend/scripts/native_vm.py"] == b"reviewed native bytes\n"


def test_interrupted_manifest_publication_recovers_only_identical_bundle(repository, tmp_path, monkeypatch):
    root, ref = repository
    output = tmp_path / "recover.tar.gz"
    original_link = helper.os.link

    def interrupt(source, destination):
        if str(destination).endswith(".manifest.json"):
            raise OSError("simulated interruption before commit marker")
        original_link(source, destination)

    with monkeypatch.context() as scope:
        scope.setattr(helper.os, "link", interrupt)
        with pytest.raises(OSError, match="simulated interruption"):
            helper.package(root, ref, [], output)
    before = output.read_bytes()
    assert not Path(str(output) + ".manifest.json").exists()
    manifest = helper.package(root, ref, [], output)
    assert output.read_bytes() == before
    assert manifest["bundle_sha256"] == hashlib.sha256(before).hexdigest()


def test_an_unrelated_orphan_bundle_is_never_overwritten(repository, tmp_path):
    root, ref = repository
    output = tmp_path / "foreign.tar.gz"
    output.write_bytes(b"FOREIGN_ARTIFACT")
    output.chmod(0o600)
    with pytest.raises(ValueError, match="differs from approved"):
        helper.package(root, ref, [], output)
    assert output.read_bytes() == b"FOREIGN_ARTIFACT"
    assert not Path(str(output) + ".manifest.json").exists()
