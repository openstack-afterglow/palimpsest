"""Automatic run acquisition contracts, with no real Docker or registry access."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_registry_intake as intake_fixtures

from palimpsest_local import cli, registry_intake, run_source, state
from palimpsest_local.errors import PalimpsestError, StateError
from palimpsest_local.oci_layout import (
    MEDIA_TYPE_IMAGE_QCOW2,
    MEDIA_TYPE_INDEX,
    MEDIA_TYPE_LAYER_CONFIG,
    MEDIA_TYPE_MANIFEST,
    ContentStore,
    canonical_json,
    config_digest,
)
from palimpsest_local.oci_source import LocalArchiveSource, SourceCAS
from palimpsest_local.packages import snapshot_package
from palimpsest_local.platforms import HostPlatform
from palimpsest_local.registry import (
    RegistryConfig,
    RegistryError,
    RegistryProfile,
    load_registry_config,
    save_registry_config,
)
from palimpsest_local.registry_intake import RegistryIntakeError, RegistryPullReceipt
from palimpsest_local.run_source import preflight_oci_capabilities as native_preflight
from palimpsest_local.runtime_types import RuntimePreflightError


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> state.StatePaths:
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker-config"))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("PALIMPSEST_REGISTRY", raising=False)
    monkeypatch.setattr(run_source, "preflight_oci_capabilities", lambda **kw: None)
    # Even a newly added path cannot accidentally contact Docker or a registry.
    for name in ("run_docker_command", "export_docker_image_to_oci_archive", "pull_registry_oci_archive"):
        monkeypatch.setattr(run_source, name, lambda *a, **kw: pytest.fail("unmocked acquisition boundary"))
    monkeypatch.setattr(run_source.shutil, "which", lambda name: "/isolated/bin/docker" if name == "docker" else None)
    result = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    save_registry_config(
        result,
        RegistryConfig(
            1,
            "alpha",
            {
                "docker": RegistryProfile(alias="docker", endpoint="docker.io", namespace="library"),
                "alpha": RegistryProfile(alias="alpha", endpoint="alpha.example.com", namespace="team"),
                "beta": RegistryProfile(alias="beta", endpoint="beta.example.com", namespace="team"),
                "native": RegistryProfile(
                    alias="native",
                    endpoint="native.example.com",
                    namespace="team",
                    protocol="palimpsest",
                    api_base="https://native.example.com",
                ),
            },
        ),
    )
    intake_fixtures._allow_image_sources(
        tmp_path,
        ("docker.io", "library", "service"),
        ("alpha.example.com", "team", "service"),
        ("beta.example.com", "team", "service"),
        config_dir=result.config,
    )
    return result


def _receipt(reference: str, output: Path) -> RegistryPullReceipt:
    output.write_bytes(b"retained test archive")
    return RegistryPullReceipt(
        reference=reference,
        output=output,
        platform="linux/amd64",
        root_digest="sha256:" + "1" * 64,
        manifest_digest="sha256:" + "2" * 64,
        source_cas_id="test-cas",
        source_binding_digest="sha256:" + "3" * 64,
    )


@pytest.fixture
def docker(roots: state.StatePaths, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    images: dict[str, str] = {}
    events: list[tuple[str, str]] = []
    failures: dict[str, str | Exception] = {}

    def inspect(argv, **kwargs):
        assert argv[:5] == ["docker", "--config", os.environ["DOCKER_CONFIG"], "image", "inspect"]
        assert len(argv) == 6
        assert kwargs == {"timeout_seconds": 15, "check": False}
        reference = argv[-1]
        events.append(("inspect", reference))
        if reference in failures:
            error = failures[reference]
            if isinstance(error, Exception):
                raise error
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=error)
        if reference not in images:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr=f"Error response from daemon: No such image: {reference}"
            )
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([{"Id": images[reference]}]), stderr="")

    def export(reference, output, _roots, **kwargs):
        assert kwargs["docker_config_dir"] == Path(os.environ["DOCKER_CONFIG"])
        events.append(("export", reference))
        canonical = registry_intake.resolve_image_reference(
            reference,
            registry_intake.default_registry_config(),
            registry_alias=None if registry_intake._has_explicit_registry(reference) else "docker",
            environment={},
        ).canonical
        return _receipt(canonical, output)

    monkeypatch.setattr(run_source, "run_docker_command", inspect)
    monkeypatch.setattr(run_source, "export_docker_image_to_oci_archive", export)
    return SimpleNamespace(images=images, events=events, failures=failures)


def _layout(path: Path, *, native: bool) -> Path:
    blobs = path / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def blob(payload: bytes, media_type: str) -> dict:
        digest = config_digest(payload)
        (blobs / digest.removeprefix("sha256:")).write_bytes(payload)
        return {"mediaType": media_type, "digest": digest, "size": len(payload)}

    config = (
        {
            "chain_id": "ordinary-cloud-bundle",
            "blob_digest": config_digest(b"cloud disk bytes"),
            "arch": "x86_64",
        }
        if native
        else {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Cmd": ["/bin/true"]},
            "rootfs": {"type": "layers", "diff_ids": []},
        }
    )
    config_type = MEDIA_TYPE_LAYER_CONFIG if native else "application/vnd.oci.image.config.v1+json"
    manifest = {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE_MANIFEST,
        "config": blob(canonical_json(config), config_type),
        "layers": [blob(b"cloud disk bytes", MEDIA_TYPE_IMAGE_QCOW2)] if native else [],
    }
    descriptor = blob(canonical_json(manifest), MEDIA_TYPE_MANIFEST)
    (path / "oci-layout").write_bytes(canonical_json({"imageLayoutVersion": "1.0.0"}))
    (path / "index.json").write_bytes(
        canonical_json({"schemaVersion": 2, "mediaType": MEDIA_TYPE_INDEX, "manifests": [descriptor]})
    )
    return path


@pytest.mark.parametrize("native,expected", [(True, "cloud-image"), (False, "oci-root")])
def test_layout_routing_uses_artifact_semantics(roots, tmp_path, native, expected):
    path = _layout(tmp_path / "layout", native=native)
    resolved = run_source.resolve_run_source(roots, str(path))
    assert resolved.source_format == expected
    assert resolved.target == path.resolve()
    if native:
        # The ordinary bundle remains consumable by the conventional stack path.
        stack = cli._resolve_runtime_stack(ContentStore(roots.store), resolved.target, [], None)
        assert stack.base.local_path.read_bytes() == b"cloud disk bytes"


def test_explicit_layout_kind_overrides_inference(roots, tmp_path):
    path = tmp_path / "invalid-layout"
    path.mkdir()
    (path / "oci-layout").write_text("invalid metadata")
    assert run_source.resolve_run_source(roots, str(path), runtime_kind="cloud-image").source_format == "cloud-image"
    assert run_source.resolve_run_source(roots, str(path), runtime_kind="oci-root").source_format == "oci-root"


def _cloud_tag(roots: state.StatePaths) -> str:
    payload = b"local cloud disk"
    digest = config_digest(payload)
    store = ContentStore(roots.store)
    store.write_stream([payload], expected_digest=digest)
    store.write_metadata(digest, {"kind": "cloud-image", "disk_format": "qcow2", "arch": "x86_64"})
    state.write_tag_record(
        roots,
        state.TagRecord(
            schema_version=1,
            tag="service",
            digest=digest,
            media_type=MEDIA_TYPE_IMAGE_QCOW2,
            size_bytes=len(payload),
            parent_digest=None,
            base_image_digest=None,
            source="test",
            created_at="2026-10-10T00:00:00Z",
        ),
    )
    return digest


def test_cloud_tag_without_registry_stays_local(roots):
    digest = _cloud_tag(roots)
    resolved = run_source.resolve_run_source(roots, "service")
    assert resolved.source_type == "cloud-tag"
    assert resolved.target == digest


def test_explicit_profile_skips_cloud_tag_and_other_docker_candidates(roots, docker):
    _cloud_tag(roots)
    docker.images.update(
        {
            "service": "sha256:" + "a" * 64,
            "alpha.example.com/team/service:latest": "sha256:" + "b" * 64,
            "beta.example.com/team/service:latest": "sha256:" + "c" * 64,
        }
    )
    resolved = run_source.resolve_run_source(roots, "service", registry_alias="beta")
    assert resolved.source_type == "docker-daemon"
    assert docker.events == [
        ("inspect", "beta.example.com/team/service:latest"),
        ("export", "beta.example.com/team/service:latest"),
    ]


def test_unknown_registry_does_not_hide_behind_cloud_tag(roots, docker):
    _cloud_tag(roots)
    with pytest.raises(RegistryError, match="unknown registry"):
        run_source.resolve_run_source(roots, "service", registry_alias="missing")
    assert docker.events == []


@pytest.mark.parametrize("kind", [None, "cloud-image"])
def test_cloud_digest_remains_local_with_unknown_registry(roots, kind):
    digest = _cloud_tag(roots)
    resolved = run_source.resolve_run_source(roots, digest, registry_alias="missing", runtime_kind=kind)
    assert resolved.source_type == "cloud-store"
    assert resolved.target == digest


@pytest.mark.parametrize("directory", [False, True])
def test_explicit_path_remains_local_with_unknown_registry(roots, tmp_path, directory):
    path = _layout(tmp_path / "bundle", native=True) if directory else tmp_path / "disk.qcow2"
    if not directory:
        path.write_bytes(b"cloud disk bytes")
    resolved = run_source.resolve_run_source(roots, str(path), registry_alias="missing")
    assert resolved.source_format == "cloud-image"
    assert resolved.target == path.resolve()


def test_qualified_docker_hit_is_inspected_before_any_remote_acquisition(roots, docker):
    docker.images["beta.example.com/team/service:latest"] = "sha256:" + "a" * 64
    resolved = run_source.resolve_run_source(roots, "service")
    assert docker.events == [
        ("inspect", "service"),
        ("inspect", "alpha.example.com/team/service:latest"),
        ("inspect", "beta.example.com/team/service:latest"),
        ("inspect", "docker.io/library/service:latest"),
        ("export", "beta.example.com/team/service:latest"),
    ]
    assert resolved.source_type == "docker-daemon"
    # Retained runs bind the root, not the selected platform's child manifest.
    assert resolved.manifest_digest == "sha256:" + "1" * 64
    assert resolved.target.is_file()


def test_same_docker_identity_is_exported_only_once_after_all_inspections(roots, docker):
    docker.images.update(
        {
            "service": "sha256:" + "a" * 64,
            "beta.example.com/team/service:latest": "sha256:" + "a" * 64,
        }
    )
    run_source.resolve_run_source(roots, "service")
    assert [event for event in docker.events if event[0] == "export"] == [("export", "service")]
    assert docker.events[-1] == ("export", "service")
    assert len(docker.events) == 5


@pytest.mark.parametrize("first", ["service", "alpha.example.com/team/service:latest"])
def test_conflicting_docker_candidates_fail_before_export_or_pull(roots, docker, first):
    docker.images.update(
        {
            first: "sha256:" + "a" * 64,
            "beta.example.com/team/service:latest": "sha256:" + "b" * 64,
        }
    )
    with pytest.raises(StateError, match="ambiguous local Docker"):
        run_source.resolve_run_source(roots, "service")
    assert len(docker.events) == 4
    assert all(event[0] == "inspect" for event in docker.events)


def test_explicit_authority_restricts_docker_to_one_name(roots, docker):
    reference = "beta.example.com/team/service:latest"
    docker.images.update({reference: "sha256:" + "a" * 64, "service": "sha256:" + "b" * 64})
    run_source.resolve_run_source(roots, reference)
    assert docker.events == [("inspect", reference), ("export", reference)]


def test_all_docker_misses_precede_remote_pull(roots, docker, monkeypatch):
    def pull(reference, output, _roots, *, profile, **kwargs):
        docker.events.append(("pull", profile.alias))
        return _receipt(f"{profile.endpoint}/team/service:latest", output)

    monkeypatch.setattr(run_source, "pull_registry_oci_archive", pull)
    resolved = run_source.resolve_run_source(roots, "service")
    assert resolved.source_type == "oci-registry"
    assert len(docker.events) == 5
    assert all(event[0] == "inspect" for event in docker.events[:-1])
    assert docker.events[-1] == ("pull", "alpha")


@pytest.mark.parametrize(
    "failure",
    [
        "Cannot connect to the Docker daemon at unix:///unavailable.sock",
        "permission denied while trying to connect to the Docker daemon socket",
        "Error response from daemon: No such image: service\nCannot connect to the Docker daemon",
        PalimpsestError("Docker inspection timed out"),
    ],
)
def test_daemon_failure_is_not_a_miss_even_after_a_local_hit(roots, docker, failure):
    docker.images["service"] = "sha256:" + "a" * 64
    docker.failures["alpha.example.com/team/service:latest"] = failure
    with pytest.raises(RegistryIntakeError, match="Docker"):
        run_source.resolve_run_source(roots, "service")
    assert docker.events == [("inspect", "service"), ("inspect", "alpha.example.com/team/service:latest")]


@pytest.mark.parametrize("stdout", ["not json", "[]", '[{"Id":"invalid"}]', "{}"])
def test_invalid_inspection_identity_does_not_authorize_pull(roots, monkeypatch, stdout):
    monkeypatch.setattr(
        run_source,
        "run_docker_command",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=""),
    )
    with pytest.raises(RegistryIntakeError, match="invalid image identity"):
        run_source.resolve_run_source(roots, "service")


def test_temporary_authority_profile_does_not_collide_with_user_alias(roots, docker, monkeypatch):
    intake_fixtures._allow_image_sources(
        roots.config.parent, ("new.example.com", "team", "service"), config_dir=roots.config
    )
    config = load_registry_config(roots)
    config = run_source.add_profile(
        config, RegistryProfile(alias="run-explicit", endpoint="user.example.com", namespace="private")
    )
    config = run_source.add_profile(config, RegistryProfile(alias="run-explicit-1", endpoint="other.example.com"))
    save_registry_config(roots, config)
    queried = []

    def pull(reference, output, _roots, *, profile, **kwargs):
        queried.append(profile)
        assert profile.alias not in config.registries
        assert profile.endpoint == "new.example.com"
        return _receipt(reference, output)

    monkeypatch.setattr(run_source, "pull_registry_oci_archive", pull)
    resolved = run_source.resolve_run_source(roots, "new.example.com/team/service:v1")
    assert resolved.source_type == "oci-registry"
    assert docker.events == [("inspect", "new.example.com/team/service:v1")]
    assert len(queried) == 1
    assert load_registry_config(roots) == config


def test_explicit_profile_miss_pulls_only_selected_profile(roots, docker, monkeypatch):
    queried = []

    def pull(reference, output, _roots, *, profile, **kwargs):
        queried.append(profile.alias)
        return _receipt(f"{profile.endpoint}/team/service:latest", output)

    _cloud_tag(roots)
    monkeypatch.setattr(run_source, "pull_registry_oci_archive", pull)
    run_source.resolve_run_source(roots, "service", registry_alias="beta")
    assert docker.events == [("inspect", "beta.example.com/team/service:latest")]
    assert queried == ["beta"]


def test_acquisition_fixture_isolates_ambient_environment(roots, tmp_path):
    assert os.environ["DOCKER_CONFIG"] == str(tmp_path / "docker-config")
    assert "DOCKER_HOST" not in os.environ
    assert "DOCKER_CONTEXT" not in os.environ
    assert "PALIMPSEST_REGISTRY" not in os.environ
    with pytest.raises(pytest.fail.Exception, match="unmocked acquisition boundary"):
        run_source.run_docker_command(["docker", "info"])


@pytest.mark.parametrize(
    "diagnostic",
    [
        "No such image: service",
        "Error: No such object: service",
        "Error response from daemon: No such image: service\n",
    ],
)
def test_genuine_docker_absence_diagnostics_are_misses(roots, docker, diagnostic):
    docker.failures["service"] = diagnostic
    assert run_source.find_docker_daemon_source(roots, ["service"]) is None
    assert docker.events == [("inspect", "service")]


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
@pytest.mark.parametrize("acquisition", ["docker-daemon", "oci-registry"])
def test_forced_darwin_upstream_acquisition_retains_consumable_platform(
    roots, tmp_path, monkeypatch, architecture, acquisition
):
    archives = {arch: intake_fixtures._platform_archive(architectures=(arch,)) for arch in ("amd64", "arm64")}
    _payload, root, manifests = archives[architecture]
    executable = tmp_path / "skopeo"
    executable.write_bytes(b"tool")
    monkeypatch.setattr(
        run_source.shutil, "which", lambda name: str(executable) if name in {"docker", "skopeo"} else None
    )
    monkeypatch.setattr(run_source, "preflight_oci_capabilities", native_preflight)
    monkeypatch.setattr(run_source, "detect_host", lambda: HostPlatform("Darwin", "aarch64"))

    def inspect(argv, **_kwargs):
        if acquisition == "oci-registry":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"No such image: {argv[-1]}")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([{"Id": "sha256:" + "a" * 64}]), stderr="")

    def copy(argv, archive, _staging, **_kwargs):
        if "save" in argv:
            archive.write_bytes(b"docker save bytes")
        else:
            # Model conversion selection, not the caller's requested platform.
            exported_architecture = argv[argv.index("--override-arch") + 1]
            archive.write_bytes(archives[exported_architecture][0])

    monkeypatch.setattr(run_source, "run_docker_command", inspect)
    monkeypatch.setattr(registry_intake, "run_docker_command", inspect)
    monkeypatch.setattr(registry_intake, "_run_bounded_copy", copy)
    monkeypatch.setattr(
        run_source, "export_docker_image_to_oci_archive", registry_intake.export_docker_image_to_oci_archive
    )
    monkeypatch.setattr(run_source, "pull_registry_oci_archive", registry_intake.pull_registry_oci_archive)
    reference = "docker.io/library/service:stable"
    resolved = run_source.resolve_run_source(
        roots, reference, platform="linux/" + architecture, force_architecture=True, backend="lima-qemu-oci"
    )
    assert resolved.source_type == acquisition
    assert resolved.manifest_digest == root["digest"]
    # Both declared-root package resolution and acquisition config verification are real.
    with snapshot_package(resolved.target, manifest=resolved.manifest_digest) as package:
        snapshot = LocalArchiveSource(package.archive, root_digest=package.root_digest).snapshot_acquisition(
            None, SourceCAS(roots.oci_source_cas), platform="linux/" + architecture
        )
        assert snapshot.image.config.architecture == architecture
        assert snapshot.manifest.descriptor.digest == manifests[0]["digest"]
    retained = run_source.find_retained_oci_source(roots, reference)
    assert retained.platform == "linux/" + architecture
    assert retained.root_digest == root["digest"]
    assert retained.manifest_digest == manifests[0]["digest"]
    lookups = (reference, retained.root_digest, retained.manifest_digest, retained.archive_digest)
    for query in lookups:
        reused = run_source.resolve_run_source(roots, query, runtime_kind="oci-root")
        assert reused.source_type == "retained-source"
        assert reused.target == resolved.target

    intake_fixtures._allow_image_sources(tmp_path, config_dir=roots.config)
    before = {path: path.read_bytes() for path in (roots.state / "sources").rglob("*") if path.is_file()}

    def unexpected_effect(*_args, **_kwargs):
        pytest.fail("revoked retained source reached runtime discovery, Docker, or registry")

    monkeypatch.setattr(run_source, "preflight_oci_capabilities", unexpected_effect)
    monkeypatch.setattr(run_source, "run_docker_command", unexpected_effect)
    monkeypatch.setattr(run_source, "export_docker_image_to_oci_archive", unexpected_effect)
    monkeypatch.setattr(run_source, "pull_registry_oci_archive", unexpected_effect)
    for query in lookups:
        with pytest.raises(RegistryIntakeError):
            run_source.resolve_run_source(roots, query, runtime_kind="oci-root")
    assert {path: path.read_bytes() for path in (roots.state / "sources").rglob("*") if path.is_file()} == before
    # Explicit archive inputs do not claim an upstream publisher identity.
    local = run_source.resolve_run_source(roots, str(resolved.target), runtime_kind="oci-root")
    assert local.source_type == "local-file"
    assert local.target == resolved.target


@pytest.mark.parametrize("direct_cache_lookup", [False, True])
def test_native_darwin_docker_cache_keeps_native_preflight(roots, docker, monkeypatch, direct_cache_lookup):
    reference = "docker.io/library/service:stable"
    docker.images[reference] = "sha256:" + "a" * 64
    monkeypatch.setattr(run_source, "preflight_oci_capabilities", native_preflight)
    monkeypatch.setattr(run_source, "detect_host", lambda: HostPlatform("Darwin", "aarch64"))
    with pytest.raises(RuntimePreflightError):
        if direct_cache_lookup:
            run_source.find_docker_daemon_source(roots, [reference])
        else:
            run_source.resolve_run_source(roots, reference)
    assert docker.events == ([("inspect", reference)] if direct_cache_lookup else [])


@pytest.mark.parametrize("reference", ["service", "docker.io/library/service:stable"])
@pytest.mark.parametrize("policy_state", ["absent", "empty", "other-repository"])
@pytest.mark.parametrize("docker_available", [False, True])
def test_run_cannot_adopt_denied_docker_store_image(
    roots, docker, monkeypatch, reference, policy_state, docker_available
):
    # A generic Docker pull may have populated the daemon independently; that
    # local hit must not turn denied upstream adoption into a local-artifact run.
    docker.images[reference] = "sha256:" + "a" * 64
    if policy_state == "absent":
        (roots.config / "config.toml").unlink()
    else:
        rules = (("docker.io", "library", "other"),) if policy_state == "other-repository" else ()
        intake_fixtures._allow_image_sources(roots.config.parent, *rules, config_dir=roots.config)
    if not docker_available:
        monkeypatch.setattr(run_source.shutil, "which", lambda _name: None)

    def unexpected_effect(*_args, **_kwargs):
        pytest.fail("source denial fell through to acquisition or output retention")

    monkeypatch.setattr(run_source, "acquire_remote_oci_source", unexpected_effect)
    monkeypatch.setattr(run_source, "_retain_archive", unexpected_effect)
    with pytest.raises(RegistryIntakeError):
        run_source.resolve_run_source(roots, reference, force_architecture=True)
    assert docker.events == []
    assert not (roots.state / "sources").exists()


def test_later_denied_docker_candidate_is_checked_before_any_inspection(roots, docker):
    intake_fixtures._allow_image_sources(
        roots.config.parent, ("docker.io", "library", "service"), config_dir=roots.config
    )
    docker.images["service"] = "sha256:" + "a" * 64
    with pytest.raises(RegistryIntakeError):
        run_source.find_docker_daemon_source(
            roots, ["service", "beta.example.com/team/service:latest"], force_architecture=True
        )
    assert docker.events == []
    assert not (roots.state / "sources").exists()


@pytest.mark.parametrize("local_kind", ["archive", "layout", "digest"])
def test_empty_policy_preserves_explicit_local_artifact_inputs(roots, tmp_path, local_kind):
    intake_fixtures._allow_image_sources(tmp_path, config_dir=roots.config)
    if local_kind == "archive":
        path = tmp_path / "local.oci.tar"
        path.write_bytes(intake_fixtures._platform_archive(architectures=("amd64",))[0])
        query, expected = str(path), path.resolve()
    elif local_kind == "layout":
        path = _layout(tmp_path / "local-layout", native=False)
        query, expected = str(path), path.resolve()
    else:
        query = expected = _cloud_tag(roots)
    resolved = run_source.resolve_run_source(roots, query)
    assert resolved.target == expected
    assert resolved.source_type == ("cloud-store" if local_kind == "digest" else "local-file")


@pytest.mark.parametrize("options", [{"plain_http": True}, {"tls_skip_verify": True}])
def test_run_daemon_adoption_checks_operator_transport_policy_before_inspect(roots, docker, options):
    config = load_registry_config(roots)
    profile = RegistryProfile("beta", "beta.example.com", namespace="team", **options)
    save_registry_config(roots, run_source.add_profile(config, profile, replace_existing=True))
    reference = "beta.example.com/team/service:latest"
    docker.images[reference] = "sha256:" + "a" * 64
    with pytest.raises(RegistryIntakeError, match="verified OCI HTTPS"):
        run_source.resolve_run_source(roots, reference, force_architecture=True)
    assert docker.events == []
    assert not (roots.state / "sources").exists()


@pytest.mark.parametrize("force_architecture", [False, True])
def test_unmatched_forced_oci_digest_is_a_local_miss_before_admission_or_transport(
    roots, monkeypatch, force_architecture
):
    digest = "sha256:" + "d" * 64
    intake_fixtures._allow_image_sources(roots.config.parent, config_dir=roots.config)

    def unexpected_effect(*_args, **_kwargs):
        pytest.fail("bare local digest reached upstream admission, runtime discovery, or acquisition")

    monkeypatch.setattr(run_source, "admit_image_source", unexpected_effect)
    monkeypatch.setattr(run_source, "preflight_oci_capabilities", unexpected_effect)
    monkeypatch.setattr(run_source, "find_docker_daemon_source", unexpected_effect)
    monkeypatch.setattr(run_source, "acquire_remote_oci_source", unexpected_effect)
    with pytest.raises(StateError, match="local OCI digest is not retained"):
        run_source.resolve_run_source(
            roots,
            digest,
            runtime_kind="oci-root",
            registry_alias="missing",
            force_architecture=force_architecture,
        )
    assert not (roots.state / "sources").exists()


def test_direct_daemon_lookup_does_not_invent_upstream_identity_for_bare_digest(roots, docker, monkeypatch):
    digest = "sha256:" + "d" * 64
    docker.images[digest] = digest
    monkeypatch.setattr(
        run_source, "admit_image_source", lambda *_a, **_kw: pytest.fail("bare digest reached upstream admission")
    )
    with pytest.raises(StateError, match="bare digests identify local artifacts"):
        run_source.find_docker_daemon_source(roots, [digest], force_architecture=True)
    assert docker.events == []
    assert not (roots.state / "sources").exists()


@pytest.mark.parametrize("transport", ["docker-daemon", "oci-registry"])
def test_native_source_acquisition_preserves_busy_quarantine_without_wait_or_transport_replay(
    roots, tmp_path, monkeypatch, transport
):
    import fcntl
    import time

    from palimpsest_local import vz_native_runtime as native

    home = tmp_path / "pinned-native-home"
    home.mkdir(mode=0o700)
    custody = home / "_palimpsest-native"
    custody.mkdir(mode=0o700)
    value = {"lima_home": str(home), "expires_unix": int(time.time()) + 3600}
    config_path = tmp_path / "native-host.json"
    config_path.write_bytes(native._json(value))
    config_path.chmod(0o600)
    _, digest, stamp = native._file(config_path, private=True, limit=native._MAX_JSON)
    home_info = home.stat()
    config = native.VZHostConfig(config_path, digest, tuple(stamp), value, {}, {}, (home_info.st_dev, home_info.st_ino))
    monkeypatch.setattr(native.VZHostConfig, "from_environment", classmethod(lambda _cls: config))
    monkeypatch.setattr(run_source, "select_backend", lambda *_a, **_kw: "lima-vz")
    quarantine = roots.run_deletions / ("locked-acquisition-" + "a" * 32)
    quarantine.mkdir(mode=0o700)
    artifact = quarantine / "artifact"
    artifact.write_bytes(b"pending committed deletion")
    before = artifact.stat()
    calls = []

    def assert_custody_owned():
        holder = os.open(custody / "lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(holder)

    def preflight(*_args, **_kwargs):
        assert_custody_owned()
        calls.append("preflight")

    def inspect(argv, **kwargs):
        assert_custody_owned()
        calls.append("inspect")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([{"Id": "sha256:" + "c" * 64}]), stderr="")

    def acquire(reference, output, _roots, **kwargs):
        assert_custody_owned()
        assert (custody / "active.json").is_file()
        assert artifact.read_bytes() == b"pending committed deletion"
        assert artifact.stat().st_ino == before.st_ino
        assert kwargs["acquisition_deadline"].remaining(30) > 0
        calls.append(transport)
        return _receipt(reference, output)

    monkeypatch.setattr(native, "preflight_pinned_lima", preflight)
    monkeypatch.setattr(run_source, "preflight_oci_capabilities", preflight)
    monkeypatch.setattr(run_source, "run_docker_command", inspect)
    monkeypatch.setattr(
        run_source,
        "export_docker_image_to_oci_archive" if transport == "docker-daemon" else "pull_registry_oci_archive",
        acquire,
    )
    # A regression to the unbounded flock branch must fail before entering the
    # kernel, since this process already owns the same lock through another fd.
    flock = fcntl.flock
    locked_identity = [None]

    def reject_blocking_run_lock(fd, operation):
        opened = os.fstat(fd)
        if (
            operation & fcntl.LOCK_EX
            and not operation & fcntl.LOCK_NB
            and (opened.st_dev, opened.st_ino) == locked_identity[0]
        ):
            pytest.fail("source custody attempted a blocking run mutex")
        return flock(fd, operation)

    monkeypatch.setattr(state.fcntl, "flock", reject_blocking_run_lock)
    # Bounded-but-waiting regressions must also fail on their first wait.
    monkeypatch.setattr(state.time, "sleep", lambda _delay: pytest.fail("source custody waited on a run mutex"))
    with state._new_run_name_lock(roots, "locked-acquisition"):
        held_lock = (roots.locks / "locked-acquisition.lock").stat()
        locked_identity[0] = (held_lock.st_dev, held_lock.st_ino)
        with run_source._external_acquisition_scope(
            platform="linux/arm64",
            backend="lima-vz",
            force_architecture=False,
            timeout_seconds=60,
            no_force=False,
        ) as (no_force, remaining, deadline):
            if transport == "docker-daemon":
                result = run_source.find_docker_daemon_source(
                    roots,
                    ["service:v1"],
                    platform="linux/arm64",
                    backend="lima-vz",
                    no_force=no_force,
                    timeout_seconds=remaining,
                    acquisition_deadline=deadline,
                )
            else:
                result = run_source.acquire_remote_oci_source(
                    roots,
                    "service:v1",
                    registry_alias="alpha",
                    platform="linux/arm64",
                    backend="lima-vz",
                    no_force=no_force,
                    timeout_seconds=remaining,
                    acquisition_deadline=deadline,
                )
            assert result.source_type == transport
            assert result.target.read_bytes() == b"retained test archive"
        assert artifact.read_bytes() == b"pending committed deletion"
        after = artifact.stat()
        assert (after.st_dev, after.st_ino, after.st_ctime_ns, after.st_mode, after.st_nlink) == (
            before.st_dev,
            before.st_ino,
            before.st_ctime_ns,
            before.st_mode,
            before.st_nlink,
        )
    locked_identity[0] = None
    assert calls == ["preflight", *(["inspect"] if transport == "docker-daemon" else []), "preflight", transport]
    assert not (custody / "active.json").exists()
    assert not list(custody.glob("hold-*.json")) and not list(custody.glob("*.actor.json"))
    with native.source_acquisition_custody(config):
        pass
    # The skipped committed deletion remains eligible for ordinary later cleanup.
    state.init_resolved_roots(roots)
    assert not quarantine.exists()
