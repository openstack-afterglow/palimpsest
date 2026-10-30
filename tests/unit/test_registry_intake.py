from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import cli, registry_intake, state
from palimpsest_local.errors import ArtifactValidationError, PalimpsestError
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
)
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
        "--multi-arch=all",
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


def test_default_docker_store_preserves_helper_environment_and_excludes_native_credentials(tmp_path, monkeypatch):
    config = tmp_path / "docker-config"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text(json.dumps({"credsStore": "fixture"}))
    home = tmp_path / "user-home"
    home.mkdir()
    secret = home / "registry-secret"
    secret.write_text("selected-registry-secret")
    helper = tmp_path / "docker-credential-fixture"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "assert sys.argv[1:] == ['get']\n"
        "assert sys.stdin.read().strip() == 'https://index.docker.io/v1/'\n"
        f"assert os.environ['DOCKER_CONFIG'] == {str(config)!r}\n"
        f"assert os.environ['HOME'] == {str(home)!r}\n"
        "assert 'PALIMPSEST_TOKEN' not in os.environ\n"
        "assert 'PALIMPSEST_PACKAGE_KEY' not in os.environ\n"
        "secret = pathlib.Path(os.environ['FIXTURE_REGISTRY_SECRET']).read_text()\n"
        "print(json.dumps({'Username': 'registry-user', 'Secret': secret}))\n"
    )
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FIXTURE_REGISTRY_SECRET", str(secret))
    monkeypatch.setenv("PALIMPSEST_TOKEN", "native-token-must-not-reach-docker-helper")
    monkeypatch.setenv("PALIMPSEST_PACKAGE_KEY", "native-key-must-not-reach-docker-helper")
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)

    resolved = registry_intake._stage_docker_auth(config, "docker.io", staging, timeout_seconds=2)

    auth_file = resolved / "config.json"
    auth = json.loads(auth_file.read_text())["auths"]
    assert set(auth) == {"docker.io"}
    assert base64.b64decode(auth["docker.io"]["auth"]).decode() == "registry-user:selected-registry-secret"
    assert auth_file.stat().st_mode & 0o777 == 0o600
    assert resolved.stat().st_mode & 0o777 == 0o700


def test_registry_specific_docker_helper_overrides_default_store_and_inline_auth(tmp_path, monkeypatch):
    config = tmp_path / "docker-config"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text(
        json.dumps(
            {
                "credsStore": "unavailable-default-store",
                "credHelpers": {"registry.example": "selected"},
                "auths": {"registry.example": {"auth": "wrong-inline-auth"}},
            }
        )
    )
    helper = tmp_path / "docker-credential-selected"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "assert sys.argv[1:] == ['get']\n"
        "assert sys.stdin.read().strip() == 'registry.example'\n"
        "print(json.dumps({'Username': '<token>', 'Secret': 'selected-identity-token'}))\n"
    )
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)

    resolved = registry_intake._stage_docker_auth(config, "registry.example", staging, timeout_seconds=2)

    assert json.loads((resolved / "config.json").read_text()) == {
        "auths": {"registry.example": {"identitytoken": "selected-identity-token"}},
    }


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
        + "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
        + "pathlib.Path(sys.argv[2]).write_text(str(child.pid));"
        + prefix
        + "time.sleep(30)"
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

        def snapshot_acquisition(self, reference, source_cas, **kwargs):
            assert kwargs["platform"] == "linux/amd64"
            assert self.archive.read_bytes() == b"archive"
            assert reference.registry == "quay.io"
            assert source_cas._root == cas  # test-only confirmation of the selected private CAS
            events.append("verify")
            descriptor = SimpleNamespace(digest="sha256:" + "a" * 64)
            return SimpleNamespace(
                image=SimpleNamespace(platform=SimpleNamespace(os="linux", architecture="amd64")),
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

        def snapshot_acquisition(self, _reference, _source_cas, **_kwargs):
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

        def snapshot_acquisition(self, _reference, _source_cas, **_kwargs):
            descriptor = SimpleNamespace(digest="sha256:" + "a" * 64)
            return SimpleNamespace(
                image=SimpleNamespace(platform=SimpleNamespace(os="linux", architecture="amd64")),
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


def test_skopeo_pull_argv_with_profile_and_authfile(tmp_path: Path):
    from palimpsest_local.registry import RegistryProfile

    docker_config = tmp_path / "docker"
    docker_config.mkdir()
    (docker_config / "config.json").write_text('{"auths":{}}', encoding="utf-8")
    profile = RegistryProfile(
        alias="custom",
        endpoint="registry.secure.test",
        tls_skip_verify=True,
        ca=(str(tmp_path / "ca.crt"),),
    )
    argv = registry_intake.skopeo_pull_argv(
        Path("/usr/bin/skopeo"),
        "registry.secure.test/app:v1",
        tmp_path / "app.oci.tar",
        profile=profile,
        docker_config_dir=docker_config,
    )
    assert "--src-tls-verify=false" in argv
    assert "--src-cert-dir" in argv
    assert str(tmp_path / "ca.crt") in argv
    assert "--authfile" in argv
    assert str(docker_config / "config.json") in argv
    assert "--src-no-creds" not in argv


@pytest.mark.parametrize(
    "name",
    [
        "build401",
        "build403",
        "credentials",
        "tls",
        "x509",
        "certificate",
        "handshake",
        "timeout",
        "denied",
        "unauthorized",
    ],
)
def test_protocol_miss_is_not_overridden_by_reference_text(name: str):
    error = registry_intake._classify_skopeo_error(
        f"reading manifest {name} in registry.example/team/{name}: manifest unknown",
        1,
    )
    assert isinstance(error, registry_intake.RegistryImageNotFoundError)


@pytest.mark.parametrize(
    "diagnostic, expected",
    [
        ("manifest unknown: authentication required", registry_intake.RegistryAuthenticationError),
        ("manifest unknown: x509: certificate signed by unknown authority", registry_intake.RegistryTLSError),
        ("manifest unknown: dial tcp 127.0.0.1:443: connection refused", registry_intake.RegistryNetworkError),
    ],
)
def test_hard_registry_failure_precedes_protocol_miss(diagnostic: str, expected: type):
    assert isinstance(registry_intake._classify_skopeo_error(diagnostic, 1), expected)


def test_classify_skopeo_error_distinguishes_missing_auth_tls_network():
    # Missing
    err = registry_intake._classify_skopeo_error("Error: manifest unknown: requested image was not found", 1)
    assert isinstance(err, registry_intake.RegistryImageNotFoundError)
    assert "not found" in str(err)

    # Auth
    err = registry_intake._classify_skopeo_error("Error: 401 Unauthorized: authentication required", 1)
    assert isinstance(err, registry_intake.RegistryAuthenticationError)
    assert "authentication failed" in str(err)

    # TLS
    err = registry_intake._classify_skopeo_error("Error: x509: certificate signed by unknown authority", 1)
    assert isinstance(err, registry_intake.RegistryTLSError)
    assert "TLS verification failed" in str(err)

    # Network
    err = registry_intake._classify_skopeo_error("Error: dial tcp: connection refused", 1)
    assert isinstance(err, registry_intake.RegistryNetworkError)
    assert "network connection failed" in str(err)

    # Generic fallback contains "could not copy"
    err = registry_intake._classify_skopeo_error("some unclassified error", 7)
    assert isinstance(err, registry_intake.RegistryIntakeError)
    assert "could not copy" in str(err)


def test_export_docker_image_missing_raises_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import subprocess

    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    fake_skopeo = tmp_path / "skopeo"
    fake_skopeo.write_bytes(b"tool")
    fake_skopeo.chmod(0o700)

    # Mock inspect to fail (image not found in daemon)
    monkeypatch.setattr(
        registry_intake,
        "run_docker_command",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, returncode=1, stderr="No such image: myapp:v1"),
    )
    with pytest.raises(registry_intake.RegistryImageNotFoundError, match="not found in.*Docker daemon"):
        registry_intake.export_docker_image_to_oci_archive(
            "myapp:v1",
            tmp_path / "out.oci.tar",
            roots,
            skopeo=fake_skopeo,
        )


def test_inspection_uses_selected_docker_credentials_and_bounded_private_output(tmp_path, monkeypatch):
    from palimpsest_local.registry import default_registry_config

    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    profile = default_registry_config().registries["docker"]
    seen = []
    monkeypatch.setattr(registry_intake.shutil, "which", lambda _name: "/usr/bin/skopeo")

    def stage(config, endpoint, staging, *, timeout_seconds):
        assert endpoint == "docker.io" and timeout_seconds == 10
        private = staging / "docker-auth"
        private.mkdir(mode=0o700)
        (private / "config.json").write_text(json.dumps({"auths": {endpoint: {"auth": "synthetic-only"}}}))
        return private

    monkeypatch.setattr(registry_intake, "_stage_docker_auth", stage)

    def run(argv, output, staging, *, timeout_seconds, maximum_bytes, stdout):
        seen.append((argv, staging))
        assert timeout_seconds == 10 and maximum_bytes == 4 * 1024 * 1024
        assert "--tls-verify=true" in argv and "--authfile" in argv
        assert not any("synthetic-only" in arg or "PALIMPSEST_TOKEN" in arg for arg in argv)
        assert Path(argv[argv.index("--authfile") + 1]).is_file()
        stdout.write(json.dumps({"Tags": ["latest"]}).encode())

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", run)
    result = registry_intake.inspect_registry_reference(
        "library/ubuntu:latest", roots, profile=profile, tags=True, timeout_seconds=10
    )
    assert result == {"Tags": ["latest"]}
    assert seen[0][0][-1] == "docker://docker.io/library/ubuntu"
    assert not seen[0][1].exists()


@pytest.mark.parametrize("options", [{"plain_http": True}, {"tls_skip_verify": True}])
def test_inspection_rejects_insecure_profile_before_acquisition(tmp_path, monkeypatch, options):
    from palimpsest_local.registry import RegistryProfile

    profile = RegistryProfile("private", "example.test", **options)
    monkeypatch.setattr(registry_intake.shutil, "which", lambda _name: pytest.fail("tool selection occurred"))
    with pytest.raises(RegistryIntakeError, match="verified OCI HTTPS"):
        registry_intake.inspect_registry_reference(
            "example.test/app:latest", StatePaths(tmp_path / "config", tmp_path / "state"), profile=profile
        )


def _platform_archive(*, architectures=("amd64", "arm64"), indexed=True, bad_diff_id=False, config_architecture=None):
    members = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}'}

    def add(value, media_type):
        payload = value if isinstance(value, bytes) else json.dumps(value).encode()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        members["blobs/sha256/" + digest.split(":")[1]] = payload
        return {"mediaType": media_type, "digest": digest, "size": len(payload)}

    manifests = []
    for architecture in architectures:
        raw = ("layer for " + architecture).encode()
        layer = add(gzip.compress(raw, mtime=0), OCI_LAYER_GZIP_MEDIA_TYPE)
        config = add(
            {
                "os": "linux",
                "architecture": config_architecture or architecture,
                "rootfs": {
                    "type": "layers",
                    "diff_ids": [
                        "sha256:" + ("0" * 64 if bad_diff_id else hashlib.sha256(raw).hexdigest()),
                    ],
                },
            },
            OCI_IMAGE_CONFIG_MEDIA_TYPE,
        )
        manifest = add(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                "config": config,
                "layers": [layer],
            },
            OCI_IMAGE_MANIFEST_MEDIA_TYPE,
        )
        manifests.append({**manifest, "platform": {"os": "linux", "architecture": architecture}})
    root = (
        add(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
                "manifests": manifests,
            },
            OCI_IMAGE_INDEX_MEDIA_TYPE,
        )
        if indexed
        else manifests[0]
    )
    members["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [root]}).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, payload in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return stream.getvalue(), root, manifests


@pytest.mark.parametrize("anonymous", [False, True])
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
@pytest.mark.parametrize("pin", ["tag", "index", "child"])
def test_selected_platform_is_verified_and_retained(tmp_path, monkeypatch, anonymous, architecture, pin):
    payload, root, manifests = _platform_archive(
        architectures=(architecture,) if pin == "child" else ("amd64", "arm64"),
        indexed=pin != "child",
    )
    selected = next(item for item in manifests if item["platform"]["architecture"] == architecture)
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    output_dir = tmp_path / "output"
    output_dir.mkdir(mode=0o700)
    seen = []

    def copy(argv, archive, _staging, **kwargs):
        seen.append(argv)
        assert kwargs["timeout_seconds"] == 15
        assert kwargs["maximum_bytes"] == 1024 * 1024
        archive.write_bytes(payload)

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    suffix = ":stable" if pin == "tag" else "@" + root["digest"]
    pull = registry_intake.pull_anonymous_oci_archive if anonymous else registry_intake.pull_registry_oci_archive
    output = output_dir / "image.oci.tar"
    roots = state.init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    docker_cfg = tmp_path / "docker"
    docker_cfg.mkdir()
    (docker_cfg / "config.json").write_text("{}", encoding="utf-8")
    pull_kw = {"docker_config_dir": docker_cfg} if not anonymous else {}
    receipt = pull(
        "registry.example.com/team/app" + suffix,
        output,
        roots,
        platform="linux/" + architecture,
        skopeo=executable,
        timeout_seconds=15,
        maximum_bytes=1024 * 1024,
        **pull_kw,
    )
    assert seen[0][seen[0].index("--override-arch") + 1] == architecture
    assert "--multi-arch=all" in seen[0]
    assert "--preserve-digests" in seen[0]
    assert receipt.platform == receipt.to_dict()["platform"] == "linux/" + architecture
    assert receipt.root_digest == root["digest"]
    assert receipt.manifest_digest == selected["digest"]
    assert receipt.source_cas_id.startswith("source-cas-v1:")
    assert receipt.source_binding_digest.startswith("sha256:")
    assert output.read_bytes() == payload
    with pytest.raises(RegistryIntakeError, match="already exists"):
        pull("registry.example.com/team/app" + suffix, output, roots, platform=receipt.platform, skopeo=executable)


@pytest.mark.parametrize("platform", ["linux/386", "windows/amd64", "linux/arm64/v9", "arm64", ""])
def test_unsupported_acquisition_platform_rejects_before_copy(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(registry_intake.shutil, "which", lambda _: pytest.fail("must reject before lookup"))
    with pytest.raises(RegistryIntakeError, match="supports only"):
        registry_intake.pull_registry_oci_archive(
            "registry.example.com/team/app:stable",
            tmp_path / "image.tar",
            StatePaths(tmp_path / "config", tmp_path / "state"),
            platform=platform,
        )


@pytest.mark.parametrize("failure", ["mismatch", "ambiguous", "diffid", "pin"])
def test_failed_selected_platform_verification_never_publishes(tmp_path, monkeypatch, failure):
    payload, _root, _manifests = _platform_archive(
        architectures=("amd64",)
        if failure == "mismatch"
        else (("arm64", "arm64") if failure == "ambiguous" else ("arm64",)),
        bad_diff_id=failure == "diffid",
    )
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    parent = tmp_path / "output"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(
        registry_intake, "_run_bounded_copy", lambda _argv, archive, _staging, **_kw: archive.write_bytes(payload)
    )
    docker_cfg = tmp_path / "docker"
    docker_cfg.mkdir()
    (docker_cfg / "config.json").write_text("{}", encoding="utf-8")
    suffix = "@sha256:" + "0" * 64 if failure == "pin" else ":stable"
    with pytest.raises(
        ArtifactValidationError,
        match={
            "mismatch": "no exact linux/arm64",
            "ambiguous": "ambiguous",
            "diffid": "DiffID mismatch",
            "pin": "requested reference digest",
        }[failure],
    ):
        registry_intake.pull_registry_oci_archive(
            "registry.example.com/team/app" + suffix,
            parent / "image.tar",
            state.init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state")),
            platform="linux/arm64",
            skopeo=executable,
            docker_config_dir=docker_cfg,
        )
    assert list(parent.iterdir()) == []


def test_docker_export_rejects_config_disagreeing_with_requested_platform(tmp_path, monkeypatch):
    payload, _root, _manifests = _platform_archive(architectures=("arm64",), config_architecture="amd64")
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    parent = tmp_path / "output"
    parent.mkdir(mode=0o700)
    roots = state.init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    monkeypatch.setattr(
        registry_intake,
        "run_docker_command",
        lambda argv, **_kw: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )

    def copy(argv, archive, _staging, **_kwargs):
        archive.write_bytes(b"docker save bytes" if "save" in argv else payload)

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    with pytest.raises(ArtifactValidationError, match="image config must target exactly linux/arm64"):
        registry_intake.export_docker_image_to_oci_archive(
            "service:stable", parent / "image.tar", roots, platform="linux/arm64", skopeo=executable
        )
    assert list(parent.iterdir()) == []


def _cached_image(layout: Path, label: str) -> tuple[dict, dict]:
    """Two distinct image graphs with one identical compressed layer."""
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True, mode=0o700)

    def blob(payload, media_type):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, separators=(",", ":")).encode()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        path = blobs / digest.split(":")[1]
        if not path.exists():
            path.write_bytes(payload)
        return {"mediaType": media_type, "digest": digest, "size": len(payload)}

    layer = blob(gzip.compress(b"shared payload", mtime=0), "application/vnd.oci.image.layer.v1.tar+gzip")
    config = blob(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Env": ["LABEL=" + label]},
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(b"shared payload").hexdigest()]},
        },
        "application/vnd.oci.image.config.v1+json",
    )
    manifest = blob(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config,
            "layers": [layer],
        },
        "application/vnd.oci.image.manifest.v1+json",
    )
    selected = {**manifest, "annotations": {"org.opencontainers.image.ref.name": "selected"}}
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (layout / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [selected]}))
    return manifest, layer


def _cached_roots(tmp_path: Path) -> tuple[StatePaths, Path]:
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    roots.oci_source_cas.parent.mkdir(parents=True, mode=0o700)
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    executable.chmod(0o700)
    return roots, executable


def _stub_layout_inspection(monkeypatch, source_for_reference):
    def inspect(_executable, reference, _home, **_kwargs):
        source = source_for_reference(reference)
        root = json.loads((source / "index.json").read_bytes())["manifests"][0]
        payload = (source / "blobs/sha256" / root["digest"].split(":")[1]).read_bytes()
        return registry_intake.Descriptor(root["mediaType"], root["digest"], root["size"]), payload

    monkeypatch.setattr(registry_intake, "_inspect_anonymous_root", inspect)


def test_layout_pulls_deduplicate_different_images_without_archive_or_cas_reimport(tmp_path, monkeypatch):
    roots, executable = _cached_roots(tmp_path)
    transfers = []

    def remote(_reference):
        source = tmp_path / ("remote-" + str(len(transfers)))
        _cached_image(source, str(len(transfers)))
        return source

    _stub_layout_inspection(monkeypatch, remote)

    def transfer(argv, layout, staging, *, incremental_bytes, **_kwargs):
        assert layout == roots.oci_source_cas
        assert not staging.is_relative_to(layout)
        assert argv[-1] == f"oci:{layout}:selected"
        manifest, layer = _cached_image(layout, str(len(transfers)))
        Path(argv[argv.index("--digestfile") + 1]).write_text(manifest["digest"] + "\n")
        assert 0 < incremental_bytes() < 4096
        transfers.append((manifest, layer))

    def no_import(*_args, **_kwargs):
        pytest.fail("retained OCI blobs must not be copied through SourceCAS import temporaries")

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", transfer)
    monkeypatch.setattr(registry_intake.SourceCAS, "import_source_blob", no_import)
    first = registry_intake.pull_anonymous_oci_layout("quay.io/acme/first:v1", roots, skopeo=executable)
    layer_path = roots.oci_source_cas / "blobs" / "sha256" / transfers[0][1]["digest"].split(":")[1]
    before = layer_path.stat()
    # The retained cache may grow beyond the maximum permitted *new* download.
    retained = roots.oci_source_cas / "retained-previous-transfer"
    retained.write_bytes(b"x" * 8192)
    second = registry_intake.pull_anonymous_oci_layout(
        "quay.io/acme/second:v1",
        roots,
        skopeo=executable,
        maximum_bytes=4096,
    )
    assert first.root_digest != second.root_digest
    assert second.output == roots.oci_source_cas
    assert first.source_cas_id == second.source_cas_id
    assert first.source_binding_digest != second.source_binding_digest
    assert layer_path.stat().st_ino == before.st_ino
    assert layer_path.stat().st_nlink == 1
    assert len(list((roots.oci_source_cas / "blobs" / "sha256").iterdir())) == 5
    assert all(path.stat().st_mode & 0o777 == 0o400 for path in (roots.oci_source_cas / "blobs" / "sha256").iterdir())
    assert (roots.oci_source_cas / "index.json").stat().st_mode & 0o777 == 0o600
    assert (roots.oci_source_cas / "oci-layout").stat().st_mode & 0o777 == 0o600
    assert retained.read_bytes() == b"x" * 8192
    assert (roots.oci_source_cas / "blobs" / "sha256" / first.root_digest.split(":")[1]).exists()


@pytest.mark.parametrize("damage", ["blob", "selected", "digest", "marker", "index", "symlink"])
def test_layout_pull_rejects_damaged_graph_without_removing_retained_bytes(tmp_path, monkeypatch, damage):
    roots, executable = _cached_roots(tmp_path)
    registry_intake.SourceCAS(roots.oci_source_cas)
    retained = roots.oci_source_cas / "prior-content"
    retained.write_bytes(b"keep")
    remote = tmp_path / "remote"
    _cached_image(remote, "bad")
    _stub_layout_inspection(monkeypatch, lambda _reference: remote)

    def transfer(argv, layout, _staging, **_kwargs):
        manifest, layer = _cached_image(layout, "bad")
        digest = manifest["digest"]
        if damage == "blob":
            path = layout / "blobs" / "sha256" / layer["digest"].split(":")[1]
            path.write_bytes(b"x" * layer["size"])
        elif damage == "selected":
            index = json.loads((layout / "index.json").read_bytes())
            index["manifests"][0]["annotations"] = {}
            (layout / "index.json").write_text(json.dumps(index))
        elif damage == "digest":
            digest = "sha256:" + "f" * 64
        elif damage == "marker":
            (layout / "oci-layout").write_text('{"imageLayoutVersion":"0.9.0"}')
        elif damage == "index":
            index = json.loads((layout / "index.json").read_bytes())
            index["mediaType"] = "application/invalid"
            (layout / "index.json").write_text(json.dumps(index))
        else:
            (layout / "unsafe").symlink_to(retained)
        Path(argv[argv.index("--digestfile") + 1]).write_text(digest)

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", transfer)
    with pytest.raises(PalimpsestError, match="corrupt|selected root|layout version|unsafe layout|media type"):
        registry_intake.pull_anonymous_oci_layout("quay.io/acme/app:v1", roots, skopeo=executable)
    assert retained.read_bytes() == b"keep"


def test_layout_transfer_failure_preserves_previous_graph_and_partial_bytes(tmp_path, monkeypatch):
    roots, executable = _cached_roots(tmp_path)
    registry_intake.SourceCAS(roots.oci_source_cas)
    manifest, layer = _cached_image(roots.oci_source_cas, "existing")
    layer_path = roots.oci_source_cas / "blobs" / "sha256" / layer["digest"].split(":")[1]
    before = layer_path.read_bytes()
    remote = tmp_path / "remote"
    _cached_image(remote, "next")
    _stub_layout_inspection(monkeypatch, lambda _reference: remote)

    def transfer(_argv, layout, staging, **_kwargs):
        (layout / "partial-new-download").write_bytes(b"partial")
        assert not staging.is_relative_to(layout)
        raise RegistryIntakeError("registry pull timed out")

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", transfer)
    with pytest.raises(RegistryIntakeError, match="timed out"):
        registry_intake.pull_anonymous_oci_layout("quay.io/acme/next:v1", roots, skopeo=executable)
    assert layer_path.read_bytes() == before
    assert (roots.oci_source_cas / "partial-new-download").read_bytes() == b"partial"
    assert (roots.oci_source_cas / "blobs" / "sha256" / manifest["digest"].split(":")[1]).exists()


def test_real_subprocess_bounds_new_layout_bytes_plus_private_scratch(tmp_path):
    layout = tmp_path / "layout"
    layout.mkdir()
    staging = tmp_path / "scratch"
    staging.mkdir()
    existing = layout / "cached"
    existing.write_bytes(b"x" * 8192)
    program = (
        "import pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_bytes(b'x'*600); "
        "pathlib.Path(sys.argv[2]).write_bytes(b'x'*600); time.sleep(10)"
    )

    def incremental_bytes():
        return sum(path.stat().st_size for path in layout.iterdir() if path != existing)

    with pytest.raises(RegistryIntakeError, match="size limit"):
        registry_intake._run_bounded_copy(
            (sys.executable, "-c", program, os.fspath(layout / "new"), os.fspath(staging / "new")),
            layout,
            staging,
            timeout_seconds=5,
            maximum_bytes=1024,
            incremental_bytes=incremental_bytes,
        )
    assert existing.read_bytes() == b"x" * 8192


def test_real_skopeo_reuses_shared_blob_unavailable_from_second_source(tmp_path, monkeypatch):
    skopeo = shutil.which("skopeo")
    if skopeo is None:
        pytest.skip("requires real skopeo OCI layout transport")
    roots, _executable = _cached_roots(tmp_path)
    first_source = tmp_path / "first-source"
    second_source = tmp_path / "second-source"
    first_manifest, shared = _cached_image(first_source, "first")
    second_manifest, _ = _cached_image(second_source, "second")
    # This graph cannot be freshly downloaded. Only destination blob reuse can
    # make Skopeo's second independent copy succeed.
    (second_source / "blobs" / "sha256" / shared["digest"].split(":")[1]).unlink()
    build_argv = registry_intake.skopeo_layout_copy_argv
    _stub_layout_inspection(monkeypatch, lambda reference: first_source if "/first" in reference else second_source)

    def local_source(executable, reference, destination, digest_file):
        argv = list(build_argv(executable, reference, destination, digest_file))
        source = first_source if "/first@" in reference else second_source
        argv[-2] = f"oci:{source}:selected"
        return tuple(argv)

    monkeypatch.setattr(registry_intake, "skopeo_layout_copy_argv", local_source)
    first = registry_intake.pull_anonymous_oci_layout("quay.io/acme/first:v1", roots, skopeo=Path(skopeo))
    shared_path = roots.oci_source_cas / "blobs" / "sha256" / shared["digest"].split(":")[1]
    before = shared_path.stat()
    same = registry_intake.pull_anonymous_oci_layout("quay.io/acme/first:v1", roots, skopeo=Path(skopeo))
    assert same.root_digest == first.root_digest
    assert shared_path.stat().st_ino == before.st_ino
    assert (roots.oci_source_cas / "blobs/sha256" / first.root_digest.split(":")[1]).stat().st_mode & 0o777 == 0o400
    second = registry_intake.pull_anonymous_oci_layout("quay.io/acme/second:v1", roots, skopeo=Path(skopeo))
    assert first.root_digest == first_manifest["digest"]
    assert second.root_digest == second_manifest["digest"]
    assert shared_path.stat().st_ino == before.st_ino
    assert shared_path.stat().st_nlink == 1
    assert shared_path.stat().st_mode & 0o777 == 0o400
    assert len(list((roots.oci_source_cas / "blobs" / "sha256").iterdir())) == 5


def test_new_pinned_index_reuses_known_child_without_rewriting_sealed_manifest(tmp_path, monkeypatch):
    skopeo = shutil.which("skopeo")
    if skopeo is None:
        pytest.skip("requires real skopeo OCI layout transport")
    roots, _ = _cached_roots(tmp_path)
    source = tmp_path / "remote"
    manifest, _ = _cached_image(source, "first")
    _stub_layout_inspection(monkeypatch, lambda _reference: source)
    original = registry_intake.skopeo_layout_copy_argv

    def local_source(executable, reference, destination, digest_file):
        argv = list(original(executable, reference, destination, digest_file))
        argv[-2] = f"oci:{source}:selected"
        return tuple(argv)

    monkeypatch.setattr(registry_intake, "skopeo_layout_copy_argv", local_source)
    first = registry_intake.pull_anonymous_oci_layout("quay.io/acme/app:v1", roots, skopeo=Path(skopeo))
    inode = (roots.oci_source_cas / "blobs/sha256" / first.root_digest.split(":")[1]).stat().st_ino
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{**manifest, "platform": {"os": "linux", "architecture": "amd64"}}],
    }
    payload = json.dumps(index, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    descriptor = {"mediaType": index["mediaType"], "digest": digest, "size": len(payload)}
    (source / "blobs/sha256" / digest.split(":")[1]).write_bytes(payload)
    (source / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [descriptor]}))

    def no_copy(*_args, **_kwargs):
        pytest.fail("known selected graph must not be recopied under its sealed manifest")

    monkeypatch.setattr(registry_intake, "_run_bounded_copy", no_copy)
    pinned = registry_intake.pull_anonymous_oci_layout(f"quay.io/acme/app@{digest}", roots, skopeo=Path(skopeo))
    assert pinned.root_digest == digest and pinned.manifest_digest == first.manifest_digest
    assert (roots.oci_source_cas / "blobs/sha256" / first.root_digest.split(":")[1]).stat().st_ino == inode
    assert (roots.oci_source_cas / "blobs/sha256" / digest.split(":")[1]).read_bytes() == payload
