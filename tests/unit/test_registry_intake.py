from __future__ import annotations

import base64
import json
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
    # Publish the descendant PID before the monitor can reject the oversized archive.
    program = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
        "pathlib.Path(sys.argv[2]).write_text(str(child.pid));" + prefix + "time.sleep(30)"
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
