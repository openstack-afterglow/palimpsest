from __future__ import annotations

import json

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.layer_cache import LayerCache
from palimpsest_local.nova_assets import AssetCatalog
from palimpsest_local.registry_worker import _request_digest, run_job, validate_request


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
        "request_digest": _request_digest(request),
        "status": "intent",
        "stage": "acquire",
    }
    job.write_text(json.dumps(previous))
    job.chmod(0o600)
    before = job.read_bytes()
    with pytest.raises(ArtifactValidationError, match="uncertain"):
        run_job(request, cache_root=cache_root, jobs_root=jobs_root, assets_root=assets_root)
    changed = {**request, "reference": "docker.io/tensorflow/tensorflow:2.21.0-gpu"}
    with pytest.raises(ArtifactValidationError, match="different immutable request"):
        run_job(changed, cache_root=cache_root, jobs_root=jobs_root, assets_root=assets_root)
    assert job.read_bytes() == before
    assert LayerCache(cache_root).inspect()["images"] == {}
    assert AssetCatalog(assets_root, "project-one").inspect()["assets"] == {}
