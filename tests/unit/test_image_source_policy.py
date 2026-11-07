from __future__ import annotations

import os
from pathlib import Path

import pytest

from palimpsest_local import state
from palimpsest_local.image_source_policy import (
    ImageSourcePolicy,
    ImageSourcePolicyError,
    ImageSourceRule,
    admit_image_source,
    image_source_policy_from_dict,
    load_image_source_policy,
)
from palimpsest_local.registry import ResolvedImageReference, default_registry_config, resolve_image_reference


def _roots(tmp_path: Path) -> state.StatePaths:
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    return state.StatePaths(config, tmp_path / "state")


def _configure(roots: state.StatePaths) -> Path:
    path = roots.config / "config.toml"
    path.write_text(
        '[storage]\nstate_root = "/unused/source-policy-fixture"\n'
        '[image_sources]\nschema_version = 1\npublisher_policy = "registry-namespace"\n'
        "rules = [\n"
        '  { registry = "docker.io", namespace = "library", repository = "ubuntu" },\n'
        '  { registry = "docker.io", namespace = "nvidia", repository = "cuda" },\n'
        "]\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_configured_sources_use_existing_docker_normalization(tmp_path):
    roots = _roots(tmp_path)
    _configure(roots)
    ubuntu = admit_image_source("ubuntu:24.04", roots)
    cuda = admit_image_source("nvidia/cuda@sha256:" + "a" * 64, roots)
    assert ubuntu.reference.canonical == "docker.io/library/ubuntu:24.04"
    assert cuda.reference.canonical == "docker.io/nvidia/cuda@sha256:" + "a" * 64
    assert ubuntu.publisher_namespace == "library"
    assert cuda.publisher_namespace == "nvidia"


def test_root_level_registry_repository_requires_an_explicit_empty_namespace():
    policy = ImageSourcePolicy((ImageSourceRule("registry.k8s.io", "", "pause"),))
    allowed = resolve_image_reference("registry.k8s.io/pause:3.10", default_registry_config(), environment={})
    assert policy.admit(allowed).reference.repository == "pause"
    for reference in ("registry.k8s.io/other/pause:3.10", "registry.k8s.io/pause-extra:3.10"):
        with pytest.raises(ImageSourcePolicyError, match="not allowed"):
            policy.admit(resolve_image_reference(reference, default_registry_config(), environment={}))
    with pytest.raises(ImageSourcePolicyError):
        ImageSourceRule("registry.k8s.io", "", "team/pause")


@pytest.mark.parametrize(
    "reference",
    [
        "docker.io/library/ubuntuevil:24.04",
        "docker.io/other/ubuntu:24.04",
        "other.example/library/ubuntu:24.04",
        "docker.io.evil.example/library/ubuntu:24.04",
        "docker.io/library/ubuntu/extra:24.04",
    ],
)
def test_configured_source_is_not_a_registry_namespace_or_repository_prefix(tmp_path, reference):
    roots = _roots(tmp_path)
    _configure(roots)
    with pytest.raises(ImageSourcePolicyError, match="not allowed"):
        admit_image_source(reference, roots)
    assert not roots.state.exists()


def test_absent_policy_denies_without_initializing_state_or_config(tmp_path):
    roots = state.StatePaths(tmp_path / "missing-config", tmp_path / "missing-state")
    with pytest.raises(ImageSourcePolicyError, match="not allowed"):
        admit_image_source("ubuntu:24.04", roots)
    assert not roots.config.exists()
    assert not roots.state.exists()


@pytest.mark.parametrize("kind", ["public", "symlink", "fifo"])
def test_source_policy_refuses_unqualified_config_files(tmp_path, kind):
    roots = _roots(tmp_path)
    path = _configure(roots)
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        target = roots.config / "actual.toml"
        path.rename(target)
        path.symlink_to(target)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    with pytest.raises(ImageSourcePolicyError):
        load_image_source_policy(roots)
    assert not roots.state.exists()


@pytest.mark.parametrize("change", ["boolean-version", "unsupported-publisher", "rules-string", "unknown", "duplicate"])
def test_policy_misconfiguration_cannot_expand_admission(change):
    entry = {"registry": "docker.io", "namespace": "library", "repository": "ubuntu"}
    value = {"schema_version": 1, "publisher_policy": "registry-namespace", "rules": [entry]}
    if change == "boolean-version":
        value["schema_version"] = True
    elif change == "unsupported-publisher":
        value["publisher_policy"] = "verified-vendor-badge"
    elif change == "rules-string":
        value["rules"] = "docker.io/library/ubuntu"
    elif change == "unknown":
        value["allow_all"] = True
    else:
        value["rules"] = [entry, entry]
    with pytest.raises(ImageSourcePolicyError):
        image_source_policy_from_dict(value)


def test_admission_does_not_trust_a_forged_resolved_reference():
    policy = ImageSourcePolicy((ImageSourceRule("docker.io", "library", "ubuntu"),))
    forged = ResolvedImageReference(
        original="ubuntu",
        endpoint="docker.io",
        repository="library/ubuntu",
        tag="elsewhere/path",
        digest=None,
        registry_alias="docker",
    )
    with pytest.raises(ImageSourcePolicyError, match="canonical"):
        policy.admit(forged)


def test_policy_change_revokes_the_same_previously_allowed_reference(tmp_path):
    roots = _roots(tmp_path)
    path = _configure(roots)
    reference = resolve_image_reference("ubuntu:24.04", default_registry_config(), environment={})
    load_image_source_policy(roots).admit(reference)
    path.write_text(
        '[image_sources]\nschema_version = 1\npublisher_policy = "registry-namespace"\nrules = []\n',
        encoding="utf-8",
    )
    with pytest.raises(ImageSourcePolicyError, match="not allowed"):
        load_image_source_policy(roots).admit(reference)


def test_config_replacement_during_snapshot_is_rejected(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    path = _configure(roots)
    replacement = roots.config / "replacement.toml"
    replacement.write_text('[image_sources]\nschema_version=1\npublisher_policy="registry-namespace"\nrules=[]\n')
    replacement.chmod(0o600)
    original_stat = os.stat

    def replace_before_visible_lookup(name, *args, **kwargs):
        if name == "config.toml" and kwargs.get("dir_fd") is not None and replacement.exists():
            replacement.replace(path)
        return original_stat(name, *args, **kwargs)

    monkeypatch.setattr(os, "stat", replace_before_visible_lookup)
    with pytest.raises(ImageSourcePolicyError, match="changed"):
        load_image_source_policy(roots)


def test_changing_state_root_preserves_configured_source_authority(tmp_path):
    roots = _roots(tmp_path)
    _configure(roots)
    destination = tmp_path / "new-state"
    state.write_state_root(roots, destination)
    import tomllib

    document = tomllib.loads((roots.config / "config.toml").read_text())
    assert document["storage"]["state_root"] == str(destination)
    assert admit_image_source("ubuntu:24.04", roots).reference.repository == "library/ubuntu"
    assert admit_image_source("nvidia/cuda:12.2.2", roots).reference.repository == "nvidia/cuda"
