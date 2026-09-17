from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import cli, registry_intake
from palimpsest_local.registry_intake import RegistryIntakeError
from palimpsest_local.state import StatePaths


def test_parser_exposes_explicit_oci_pull_surface():
    parsed = cli.build_parser().parse_args(["oci", "pull", "quay.io/acme/demo:v1", "--output", "demo.oci.tar"])
    assert parsed.oci_operation == "pull"
    assert parsed.reference == "quay.io/acme/demo:v1"
    assert parsed.output == Path("demo.oci.tar")
    assert parsed.platform == "linux/amd64"
    assert parsed.timeout == 300.0


@pytest.mark.parametrize(
    "reference",
    [
        "busybox:latest",
        "library/busybox:latest",
        "https://quay.io/acme/demo:v1",
        "user:password@quay.io/acme/demo:v1",
    ],
)
def test_public_pull_requires_clean_fully_qualified_reference(reference: str):
    with pytest.raises(RegistryIntakeError):
        registry_intake.resolve_anonymous_reference(reference)


def test_skopeo_argv_forces_anonymous_verified_tls_and_platform(tmp_path: Path):
    argv = registry_intake.skopeo_copy_argv(
        Path("/usr/bin/skopeo"), "registry.example.com/team/app:v1", tmp_path / "app.oci.tar"
    )
    assert argv == (
        "/usr/bin/skopeo",
        "--override-os",
        "linux",
        "--override-arch",
        "amd64",
        "copy",
        "--multi-arch=system",
        "--preserve-digests",
        "--src-no-creds",
        "--src-tls-verify=true",
        "docker://registry.example.com/team/app:v1",
        f"oci-archive:{tmp_path / 'app.oci.tar'}",
    )
    assert not any("password" in item or "creds=" in item or "tls-verify=false" in item for item in argv)


def test_isolated_environment_routes_all_writable_client_state_under_staging(tmp_path: Path):
    environment = registry_intake._isolated_environment(tmp_path)
    assert set(("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR")) <= set(environment)
    assert environment["HOME"] == os.fspath(tmp_path)
    for name in ("TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
        directory = Path(environment[name])
        assert directory.parent == tmp_path
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700
    assert "REGISTRY_AUTH_FILE" not in environment


def test_real_subprocess_timeout_is_bounded(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    with pytest.raises(RegistryIntakeError, match="timed out"):
        registry_intake._run_bounded_copy(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            archive,
            tmp_path,
            timeout_seconds=0.05,
            maximum_bytes=1024,
        )


def test_real_subprocess_staging_size_is_bounded(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    program = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_bytes(b'x'*2048); time.sleep(10)"
    with pytest.raises(RegistryIntakeError, match="size limit"):
        registry_intake._run_bounded_copy(
            (sys.executable, "-c", program, os.fspath(archive)),
            archive,
            tmp_path,
            timeout_seconds=2,
            maximum_bytes=1024,
        )


def test_real_subprocess_nonzero_is_rejected(tmp_path: Path):
    with pytest.raises(RegistryIntakeError, match="could not copy"):
        registry_intake._run_bounded_copy(
            (sys.executable, "-c", "raise SystemExit(7)"),
            tmp_path / "image.oci.tar",
            tmp_path,
            timeout_seconds=2,
            maximum_bytes=1024,
        )


def test_real_subprocess_symlink_archive_is_rejected(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    program = "import os,sys,time; os.symlink('/dev/null', sys.argv[1]); time.sleep(10)"
    with pytest.raises(RegistryIntakeError, match="unsafe staging entry"):
        registry_intake._run_bounded_copy(
            (sys.executable, "-c", program, os.fspath(archive)),
            archive,
            tmp_path,
            timeout_seconds=2,
            maximum_bytes=1024,
        )


@pytest.mark.parametrize("failure", ["timeout", "size"])
def test_real_subprocess_error_kills_spawned_descendant(tmp_path: Path, failure: str):
    archive = tmp_path / "image.oci.tar"
    pid_file = tmp_path / "child.pid"
    prefix = "pathlib.Path(sys.argv[1]).write_bytes(b'x'*2048);" if failure == "size" else ""
    program = (
        "import pathlib,subprocess,sys,time;"
        + prefix
        + "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
        + "pathlib.Path(sys.argv[2]).write_text(str(child.pid));time.sleep(30)"
    )
    with pytest.raises(RegistryIntakeError, match="size limit" if failure == "size" else "timed out"):
        registry_intake._run_bounded_copy(
            (sys.executable, "-c", program, os.fspath(archive), os.fspath(pid_file)),
            archive,
            tmp_path,
            timeout_seconds=2 if failure == "size" else 0.1,
            maximum_bytes=1024 if failure == "size" else 4096,
        )
    child_pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("spawned registry-client descendant survived process-group teardown")


def test_nominal_subprocess_exit_kills_spawned_descendant(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    pid_file = tmp_path / "child.pid"
    program = (
        "import pathlib,subprocess,sys;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
    )
    registry_intake._run_bounded_copy(
        (sys.executable, "-c", program, os.fspath(pid_file)),
        archive,
        tmp_path,
        timeout_seconds=2,
        maximum_bytes=4096,
    )
    child_pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("spawned registry-client descendant survived nominal process-group teardown")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_invalid_timeout_is_rejected_before_subprocess(tmp_path: Path, timeout: float):
    with pytest.raises(RegistryIntakeError, match="timeout"):
        registry_intake._run_bounded_copy(
            (sys.executable, "-c", "raise AssertionError('must not execute')"),
            tmp_path / "image.oci.tar",
            tmp_path,
            timeout_seconds=timeout,
            maximum_bytes=1024,
        )


def test_output_parent_symlink_is_rejected(tmp_path: Path):
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(RegistryIntakeError, match="symbolic links"):
        registry_intake._safe_output_parent(alias / "image.oci.tar")


def test_group_writable_output_parent_is_rejected(tmp_path: Path):
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o770)
    parent.chmod(0o770)
    with pytest.raises(RegistryIntakeError, match="owner-bound"):
        registry_intake._safe_output_parent(parent / "image.oci.tar")


def test_pull_verifies_before_nonoverwriting_publish(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    output_parent = tmp_path / "output"
    output_parent.mkdir(mode=0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    cas = state / "runtime-packs" / "oci-source-v1"
    cas.mkdir(parents=True, mode=0o700)
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    executable.chmod(0o700)
    events: list[str] = []

    def copy(_argv, archive, _home, *, timeout_seconds, maximum_bytes):
        assert timeout_seconds == 12
        assert maximum_bytes == 1024
        events.append("copy")
        archive.write_bytes(b"archive")

    class Archive:
        def __init__(self, archive):
            self.archive = archive

        def snapshot(self, reference, source_cas):
            assert self.archive.read_bytes() == b"archive"
            assert reference.registry == "quay.io"
            assert source_cas._root == cas  # test-only confirmation of the selected private CAS
            events.append("verify")
            descriptor = SimpleNamespace(digest="sha256:" + "a" * 64)
            return SimpleNamespace(
                root=SimpleNamespace(descriptor=descriptor),
                manifest=SimpleNamespace(descriptor=descriptor),
                cas_id="source-cas-v1:" + "b" * 64,
                binding_digest="sha256:" + "c" * 64,
            )

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    monkeypatch.setattr(registry_intake, "LocalArchiveSource", Archive)
    output = output_parent / "demo.oci.tar"
    receipt = registry_intake.pull_anonymous_oci_archive(
        "quay.io/acme/demo:v1",
        output,
        StatePaths(tmp_path / "config", state),
        timeout_seconds=12,
        maximum_bytes=1024,
        skopeo=executable,
    )
    assert events == ["copy", "verify"]
    assert output.read_bytes() == b"archive"
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert receipt.manifest_digest == "sha256:" + "a" * 64
    with pytest.raises(RegistryIntakeError, match="already exists"):
        registry_intake.pull_anonymous_oci_archive(
            "quay.io/acme/demo:v1", output, StatePaths(tmp_path / "config", state), skopeo=executable
        )


def test_failed_verification_never_publishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    output_parent = tmp_path / "output"
    output_parent.mkdir(mode=0o700)
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    executable.chmod(0o700)
    state = tmp_path / "state"
    (state / "runtime-packs" / "oci-source-v1").mkdir(parents=True, mode=0o700)

    def copy(_argv, archive, _home, **_kwargs):
        archive.write_bytes(b"invalid")

    class Archive:
        def __init__(self, _archive):
            pass

        def snapshot(self, _reference, _source_cas):
            raise RegistryIntakeError("invalid OCI graph")

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    monkeypatch.setattr(registry_intake, "LocalArchiveSource", Archive)
    output = output_parent / "demo.oci.tar"
    with pytest.raises(RegistryIntakeError, match="invalid OCI graph"):
        registry_intake.pull_anonymous_oci_archive(
            "quay.io/acme/demo:v1",
            output,
            StatePaths(tmp_path / "config", state),
            skopeo=executable,
        )
    assert not output.exists()
    assert list(output_parent.iterdir()) == []


def test_real_invalid_oci_archive_never_publishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    output_parent = tmp_path / "output"
    output_parent.mkdir(mode=0o700)
    state = tmp_path / "state"
    (state / "runtime-packs" / "oci-source-v1").mkdir(parents=True, mode=0o700)
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    executable.chmod(0o700)

    def copy(_argv, archive, _home, **_kwargs):
        archive.write_bytes(b"not an OCI archive")

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    output = output_parent / "image.oci.tar"
    with pytest.raises(Exception, match="OCI archive"):
        registry_intake.pull_anonymous_oci_archive(
            "quay.io/acme/demo:v1", output, StatePaths(tmp_path / "config", state), skopeo=executable
        )
    assert not output.exists()
    assert list(output_parent.iterdir()) == []


def test_publish_race_never_overwrites_competing_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    output_parent = tmp_path / "output"
    output_parent.mkdir(mode=0o700)
    state = tmp_path / "state"
    (state / "runtime-packs" / "oci-source-v1").mkdir(parents=True, mode=0o700)
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    executable.chmod(0o700)
    output = output_parent / "image.oci.tar"

    def copy(_argv, archive, _home, **_kwargs):
        archive.write_bytes(b"archive")
        output.write_bytes(b"competitor")

    class Archive:
        def __init__(self, _archive):
            pass

        def snapshot(self, _reference, _source_cas):
            descriptor = SimpleNamespace(digest="sha256:" + "a" * 64)
            return SimpleNamespace(
                root=SimpleNamespace(descriptor=descriptor),
                manifest=SimpleNamespace(descriptor=descriptor),
                cas_id="source-cas-v1:" + "b" * 64,
                binding_digest="sha256:" + "c" * 64,
            )

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    monkeypatch.setattr(registry_intake, "LocalArchiveSource", Archive)
    with pytest.raises(RegistryIntakeError, match="already exists"):
        registry_intake.pull_anonymous_oci_archive(
            "quay.io/acme/demo:v1", output, StatePaths(tmp_path / "config", state), skopeo=executable
        )
    assert output.read_bytes() == b"competitor"


def test_source_cas_failure_never_publishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    output_parent = tmp_path / "output"
    output_parent.mkdir(mode=0o700)
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    executable.chmod(0o700)

    def copy(_argv, archive, _home, **_kwargs):
        archive.write_bytes(b"archive")

    def fail_cas(_root):
        raise RegistryIntakeError("source CAS unavailable")

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    monkeypatch.setattr(registry_intake, "SourceCAS", fail_cas)
    output = output_parent / "image.oci.tar"
    with pytest.raises(RegistryIntakeError, match="CAS unavailable"):
        registry_intake.pull_anonymous_oci_archive(
            "registry.internal/acme/demo:v1",
            output,
            StatePaths(tmp_path / "config", tmp_path / "state"),
            skopeo=executable,
        )
    assert not output.exists()
    assert list(output_parent.iterdir()) == []


def test_swapped_output_parent_does_not_publish_and_cleans_owned_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    output_parent = tmp_path / "output"
    output_parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "moved"
    state = tmp_path / "state"
    (state / "runtime-packs" / "oci-source-v1").mkdir(parents=True, mode=0o700)
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    executable.chmod(0o700)

    def copy(_argv, archive, _home, **_kwargs):
        archive.write_bytes(b"archive")
        output_parent.rename(moved_parent)
        output_parent.mkdir(mode=0o700)

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    with pytest.raises(RegistryIntakeError, match="filesystem boundary"):
        registry_intake.pull_anonymous_oci_archive(
            "registry.internal/acme/demo:v1",
            output_parent / "image.oci.tar",
            StatePaths(tmp_path / "config", state),
            skopeo=executable,
        )
    assert list(output_parent.iterdir()) == []
    assert list(moved_parent.iterdir()) == []
