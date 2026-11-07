from __future__ import annotations

import hashlib
import json
import struct
from types import SimpleNamespace

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.image_source_policy import ImageSourcePolicy, ImageSourcePolicyError, ImageSourceRule
from palimpsest_local.layer_cache import LayerCache
from palimpsest_local.nova_assets import AssetCatalog
from palimpsest_local.registry_worker import _request_digest, run_job, validate_request


def _policy():
    return ImageSourcePolicy(
        (
            ImageSourceRule("docker.io", "pytorch", "pytorch"),
            ImageSourceRule("docker.io", "tensorflow", "tensorflow"),
        )
    )


def _request():
    return {
        "schema": "palimpsest.registry-worker-job.v1",
        "job_id": "job-one",
        "project_id": "project-one",
        "reference": "docker.io/pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "../escape"),
        ("project_id", "foreign\nproject"),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("nan")),
        ("packer", "../mksquashfs"),
        ("reference", "http://registry/image"),
        ("reference", "https://user:password@registry/image"),
        ("consumer", "../escape"),
    ],
)
def test_unsafe_job_cannot_start_acquisition(field, value):
    request = _request()
    request[field] = value
    with pytest.raises(ArtifactValidationError):
        validate_request(request)


def test_prior_uncertain_job_and_changed_request_do_not_repeat_network_or_conversion(tmp_path):
    cache_root = tmp_path / "cache"
    cache_root.mkdir(mode=0o700)
    assets_root = tmp_path / "assets"
    assets_root.mkdir(mode=0o700)
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir(mode=0o700)
    LayerCache(cache_root, initialize=True)
    AssetCatalog(assets_root, "project-one", initialize=True)
    request = validate_request(_request())
    job = jobs_root / "job-one.json"
    previous = {
        "schema": "palimpsest.registry-worker-result.v1",
        "job_id": "job-one",
        "project_id": "project-one",
        "request_digest": _request_digest(request, _policy()),
        "status": "intent",
        "stage": "acquire",
    }
    job.write_text(json.dumps(previous))
    job.chmod(0o600)
    before = job.read_bytes()
    with pytest.raises(ArtifactValidationError, match="uncertain"):
        run_job(
            request,
            source_policy=_policy(),
            current_policy=_policy,
            cache_root=cache_root,
            jobs_root=jobs_root,
            assets_root=assets_root,
        )
    changed = {**request, "reference": "docker.io/tensorflow/tensorflow:2.21.0-gpu"}
    with pytest.raises(ArtifactValidationError, match="different immutable request"):
        run_job(
            changed,
            source_policy=_policy(),
            current_policy=_policy,
            cache_root=cache_root,
            jobs_root=jobs_root,
            assets_root=assets_root,
        )
    assert job.read_bytes() == before
    assert LayerCache(cache_root).inspect()["images"] == {}
    assert AssetCatalog(assets_root, "project-one").inspect()["assets"] == {}


@pytest.mark.parametrize(
    "policy",
    [ImageSourcePolicy(), ImageSourcePolicy((ImageSourceRule("docker.io", "nvidia", "cuda"),))],
)
def test_denied_job_has_no_cache_catalog_or_job_effects(tmp_path, policy):
    with pytest.raises(ImageSourcePolicyError, match="not allowed"):
        run_job(
            _request(),
            source_policy=policy,
            current_policy=lambda: policy,
            cache_root=tmp_path / "cache",
            jobs_root=tmp_path / "jobs",
            assets_root=tmp_path / "assets",
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["source_policy", "current_policy", "current_policy_provider", "operator_roots"])
def test_request_cannot_select_its_own_policy(field):
    with pytest.raises(ArtifactValidationError):
        validate_request({**_request(), field: _policy().to_dict()})


def test_policy_change_cannot_rebind_uncertain_job(tmp_path):
    cache_root, jobs_root, assets_root = (tmp_path / name for name in ("cache", "jobs", "assets"))
    for directory in (cache_root, jobs_root, assets_root):
        directory.mkdir(mode=0o700)
    LayerCache(cache_root, initialize=True)
    AssetCatalog(assets_root, "project-one", initialize=True)
    request = validate_request(_request())
    job = jobs_root / "job-one.json"
    previous = {"request_digest": _request_digest(request, _policy()), "status": "intent"}
    job.write_text(json.dumps(previous))
    job.chmod(0o600)
    narrower = ImageSourcePolicy((ImageSourceRule("docker.io", "pytorch", "pytorch"),))
    with pytest.raises(ArtifactValidationError, match="different immutable request"):
        run_job(
            request,
            source_policy=narrower,
            current_policy=lambda: narrower,
            cache_root=cache_root,
            jobs_root=jobs_root,
            assets_root=assets_root,
        )
    assert json.loads(job.read_text()) == previous


def test_worker_cli_does_not_trust_cache_config(tmp_path, monkeypatch, capsys):
    from palimpsest_local.registry_worker import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "operator-config"))
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "state"))
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    (cache / "config").mkdir(mode=0o700)
    cache_config = cache / "config" / "config.toml"
    cache_config.write_text(
        '[image_sources]\nschema_version=1\npublisher_policy="registry-namespace"\n'
        '[[image_sources.rules]]\nregistry="docker.io"\nnamespace="pytorch"\nrepository="pytorch"\n'
    )
    cache_config.chmod(0o600)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_request()))
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--cache-root",
                str(cache),
                "--jobs-root",
                str(tmp_path / "jobs"),
                "--assets-root",
                str(tmp_path / "assets"),
                "run",
                str(request),
            ]
        )
    assert error.value.code == 1
    assert "ImageSourcePolicyError" in capsys.readouterr().err
    assert not (tmp_path / "jobs").exists()
    assert not (tmp_path / "assets").exists()


def test_worker_cli_operator_policy_reaches_bound_completed_job_validation(tmp_path, monkeypatch, capsys):
    from palimpsest_local.registry_worker import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "operator-config"))
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "state"))
    config = tmp_path / "operator-config" / "palimpsest"
    config.mkdir(mode=0o700, parents=True)
    operator_config = config / "config.toml"
    operator_config.write_text(
        '[image_sources]\nschema_version=1\npublisher_policy="registry-namespace"\n'
        '[[image_sources.rules]]\nregistry="docker.io"\nnamespace="pytorch"\nrepository="pytorch"\n'
        '[[image_sources.rules]]\nregistry="docker.io"\nnamespace="tensorflow"\nrepository="tensorflow"\n'
    )
    operator_config.chmod(0o600)
    cache, jobs, assets = (tmp_path / name for name in ("cache", "jobs", "assets"))
    for directory in (cache, jobs, assets):
        directory.mkdir(mode=0o700)
    LayerCache(cache, initialize=True)
    AssetCatalog(assets, "project-one", initialize=True)
    prior = {
        "request_digest": _request_digest(validate_request(_request()), _policy()),
        "status": "success",
        "root_digest": "invalid-retained-digest",
    }
    journal = jobs / "job-one.json"
    journal.write_text(json.dumps(prior))
    journal.chmod(0o600)
    before = journal.read_bytes()
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_request()))
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--cache-root",
                str(cache),
                "--jobs-root",
                str(jobs),
                "--assets-root",
                str(assets),
                "run",
                str(request),
            ]
        )
    assert error.value.code == 1
    # A wrong policy would fail the request binding with ArtifactValidationError;
    # this distinct error proves the configured snapshot matched before cache validation.
    assert "LayerCacheError" in capsys.readouterr().err
    assert journal.read_bytes() == before
    assert LayerCache(cache).inspect()["images"] == {}
    assert AssetCatalog(assets, "project-one").inspect()["assets"] == {}


@pytest.fixture
def publication_job(tmp_path, monkeypatch):
    from palimpsest_local import registry_worker
    from palimpsest_local.image_source_policy import load_image_source_policy
    from palimpsest_local.state import resolve_roots

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "operator"))
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "state"))
    config = tmp_path / "operator" / "palimpsest"
    config.mkdir(mode=0o700, parents=True)
    operator = config / "config.toml"
    allowed = (
        '[image_sources]\nschema_version=1\npublisher_policy="registry-namespace"\n'
        '[[image_sources.rules]]\nregistry="docker.io"\nnamespace="pytorch"\nrepository="pytorch"\n'
    )
    operator.write_text(allowed)
    operator.chmod(0o600)
    policy = load_image_source_policy(resolve_roots())
    cache_root, jobs_root, assets_root = (tmp_path / name for name in ("cache", "jobs", "assets"))
    for root in (cache_root, jobs_root, assets_root):
        root.mkdir(mode=0o700)
    catalog = AssetCatalog(assets_root, "project-one", initialize=True)
    content = struct.pack(
        "<5I6H8Q",
        0x73717368,
        1,
        0,
        131072,
        0,
        1,
        17,
        0,
        1,
        4,
        0,
        0,
        160,
        144,
        2**64 - 1,
        96,
        112,
        2**64 - 1,
        2**64 - 1,
    ) + b"fixture".ljust(416, b"\0")
    blob = tmp_path / "layer"
    blob.write_bytes(content)
    blob.chmod(0o400)
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    root_digest = "sha256:" + "a" * 64
    acquired = {
        "root_digest": root_digest,
        "image": {
            "layers": [{"compressed": {"digest": "sha256:" + "b" * 64}, "diff_id": "sha256:" + "c" * 64}],
            "manifest_descriptor": {"digest": root_digest},
            "config_descriptor": {"digest": "sha256:" + "d" * 64},
            "platform": {"os": "linux", "architecture": "amd64"},
        },
    }
    state = {"revoke_at": None, "retained": [], "acquired": 0}

    def revoke():
        operator.write_text('[image_sources]\nschema_version=1\npublisher_policy="registry-namespace"\nrules=[]\n')

    class Cache:
        def __init__(self, root):
            pass

        def pull(self, reference, **kwargs):
            state["acquired"] += 1
            return acquired

        def materialize(self, selected, **kwargs):
            if state["revoke_at"] == "materialize":
                revoke()
            return {
                "receipt": {
                    "results": [
                        {
                            "receipt": {
                                "image_digest": digest,
                                "image_size": len(content),
                                "key_digest": "sha256:" + "e" * 64,
                            }
                        }
                    ]
                },
                "blob_paths": [str(blob)],
            }

        def retain(self, selected, consumer, *, before_commit):
            before_commit()
            state["retained"].append((selected, consumer))

    original_register = AssetCatalog.register

    def register(self, **kwargs):
        result = original_register(self, **kwargs)
        if state["revoke_at"] == "register":
            revoke()
        return result

    from palimpsest_local import nova_assets

    original_verify = nova_assets.verify_blob

    def verify(*args, **kwargs):
        result = original_verify(*args, **kwargs)
        if state["revoke_at"] == "asset-verification":
            revoke()
        return result

    monkeypatch.setattr(nova_assets, "verify_blob", verify)

    monkeypatch.setattr(registry_worker, "LayerCache", Cache)
    monkeypatch.setattr(AssetCatalog, "register", register)
    return SimpleNamespace(
        state=state,
        policy=policy,
        operator=operator,
        allowed=allowed,
        catalog=catalog,
        provider=lambda: load_image_source_policy(resolve_roots()),
        roots={"cache_root": cache_root, "jobs_root": jobs_root, "assets_root": assets_root},
    )


@pytest.mark.parametrize("revoke_at", ["materialize", "asset-verification"])
@pytest.mark.parametrize("entrypoint", ["api", "worker-cli", "cloud-cli"])
def test_revocation_before_catalog_commit_prevents_asset_publication(publication_job, tmp_path, entrypoint, revoke_at):
    from palimpsest_local import cloud_cli, registry_worker
    from palimpsest_local.errors import PalimpsestError
    from palimpsest_local.image_policy_channel import PolicyPublicationHold

    fixture = publication_job
    fixture.state["revoke_at"] = revoke_at
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request()))
    if entrypoint == "api":
        with pytest.raises(PolicyPublicationHold):
            run_job(_request(), source_policy=fixture.policy, current_policy=fixture.provider, **fixture.roots)
    elif entrypoint == "worker-cli":
        with pytest.raises(SystemExit) as error:
            registry_worker.main(
                [
                    "--cache-root",
                    str(fixture.roots["cache_root"]),
                    "--jobs-root",
                    str(fixture.roots["jobs_root"]),
                    "--assets-root",
                    str(fixture.roots["assets_root"]),
                    "run",
                    str(request_path),
                ]
            )
        assert error.value.code == 1
    else:
        with pytest.raises(PalimpsestError):
            cloud_cli.dispatch(
                SimpleNamespace(cloud_operation="registry-worker", request=request_path, **fixture.roots)
            )
    held = json.loads((fixture.roots["jobs_root"] / "job-one.json").read_text())
    assert held["status"] == "hold" and held["source_policy_digest"] == fixture.policy.digest
    assert fixture.catalog.inspect()["assets"] == {}
    assert fixture.state["retained"] == []
    fixture.operator.write_text(fixture.allowed)
    with pytest.raises(ArtifactValidationError):
        run_job(_request(), source_policy=fixture.policy, current_policy=fixture.provider, **fixture.roots)
    assert fixture.state["acquired"] == 1


def test_revocation_before_retention_preserves_registered_asset_prefix(publication_job):
    from palimpsest_local.image_policy_channel import PolicyPublicationHold

    fixture = publication_job
    fixture.state["revoke_at"] = "register"
    with pytest.raises(PolicyPublicationHold):
        run_job(
            {**_request(), "consumer": "consumer-one"},
            source_policy=fixture.policy,
            current_policy=fixture.provider,
            **fixture.roots,
        )
    held = json.loads((fixture.roots["jobs_root"] / "job-one.json").read_text())
    assert held["status"] == "hold"
    assert held["asset_id"] in fixture.catalog.inspect()["assets"]
    assert fixture.state["retained"] == []


def test_fresh_job_under_broader_policy_keeps_identical_content_identity(publication_job):
    fixture = publication_job
    first = run_job(_request(), source_policy=fixture.policy, current_policy=fixture.provider, **fixture.roots)
    fixture.operator.write_text(
        fixture.allowed
        + '[[image_sources.rules]]\nregistry="docker.io"\nnamespace="tensorflow"\nrepository="tensorflow"\n'
    )
    broader = fixture.provider()
    second = run_job(
        {**_request(), "job_id": "job-two"},
        source_policy=broader,
        current_policy=fixture.provider,
        **fixture.roots,
    )
    assert first["status"] == second["status"] == "success"
    assert first["asset_id"] == second["asset_id"]
    assert first["source_policy_digest"] != second["source_policy_digest"]
