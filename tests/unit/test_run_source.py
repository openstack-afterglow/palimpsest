"""Automatic run acquisition contracts, with no real Docker or registry access."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import cli, run_source, state
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
from palimpsest_local.registry import (
    RegistryConfig,
    RegistryError,
    RegistryProfile,
    load_registry_config,
    save_registry_config,
)
from palimpsest_local.registry_intake import RegistryIntakeError, RegistryPullReceipt


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
        return _receipt(reference, output)

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
        {"chain_id": "ordinary-cloud-bundle"}
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
