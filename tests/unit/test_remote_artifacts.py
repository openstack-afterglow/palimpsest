"""Connected artifact boundaries; isolated fake transports, never remote credentials."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import remote_artifacts as remote
from palimpsest_local.digest import digest_file
from palimpsest_local.errors import StateError
from palimpsest_local.hub import MEDIA_TYPE_LAYER_SQUASHFS
from palimpsest_local.oci_layout import ContentStore
from palimpsest_local.registry import RegistryProfile, add_profile, default_registry_config, save_registry_config
from palimpsest_local.state import atomic_write_json, init_roots


def _roots(tmp_path):
    return init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _chain():
    base_bytes, root_bytes, leaf_bytes = b"raw-cloud-disk", b"hsqs-root-layer", b"hsqs-leaf-layer"
    base, root, leaf = map(_digest, (base_bytes, root_bytes, leaf_bytes))
    metadata = {
        base: {"digest": base, "kind": "cloud-image", "name": "base", "disk_format": "raw", "arch": "aarch64"},
        root: {
            "digest": root,
            "kind": "squashfs",
            "name": "root",
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
            "base_image_digest": base,
            "parent_digest": None,
            "arch": "aarch64",
        },
        leaf: {
            "digest": leaf,
            "kind": "squashfs",
            "name": "leaf",
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
            "base_image_digest": base,
            "parent_digest": root,
            "arch": "aarch64",
        },
    }
    return base, root, leaf, metadata, {base: base_bytes, root: root_bytes, leaf: leaf_bytes}


class _Hub:
    def __init__(self, metadata, blobs):
        self.metadata, self.blobs, self.actions, self.uploads = metadata, blobs, [], []

    def identity(self, action):
        self.actions.append(action)
        return {"project_id": "explicit-project", "user_id": "user", "capabilities": {action: True}}

    def get_layer(self, digest):
        return self.metadata[digest]

    def pull_blob(self, digest, destination, *, resume):
        assert resume is False
        destination.write_bytes(self.blobs[digest])
        return destination

    def push_blob(self, path, metadata, *, resume):
        assert resume is False
        self.uploads.append((digest_file(path), metadata))
        return {"blob_digest": digest_file(path)}


def test_legacy_download_complete_chain_is_canonical_run_source(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    backend = remote.RemoteArtifacts(roots)
    base, root, leaf, metadata, blobs = _chain()
    hub = _Hub(metadata, blobs)
    monkeypatch.setattr(backend, "_hub", lambda: hub)
    receipt = backend.download("legacy", leaf)
    assert hub.actions == ["download"]
    assert receipt["image"] == base and receipt["layers"] == [root, leaf]
    assert receipt["runtime_kind"] == "cloud-image"
    store = ContentStore(roots.store)
    for digest in (base, root, leaf):
        assert store.verify_blob(digest) == len(blobs[digest])
    # Exercise the existing run consumer's immutable chain boundary.
    from palimpsest_local.cli import _layer_refs_from_store

    assert [layer.digest for layer in _layer_refs_from_store(store, [root, leaf], base)] == [root, leaf]
    assert json.loads(Path(receipt["receipt"]).read_text())["requested_reference"] == leaf


def test_legacy_upload_dependencies_first_and_complete_metadata(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    backend = remote.RemoteArtifacts(roots)
    base, root, leaf, metadata, blobs = _chain()
    store = ContentStore(roots.store)
    for digest, data in blobs.items():
        store.write_stream([data], expected_digest=digest)
        store.write_metadata(digest, metadata[digest])
    hub = _Hub(metadata, blobs)
    monkeypatch.setattr(backend, "_hub", lambda: hub)
    result = backend.upload("legacy", {"kind": "local", "reference": leaf}, "selected-build")
    assert result["uploaded"] == 3 and hub.actions == ["upload"]
    assert [digest for digest, _ in hub.uploads] == [base, root, leaf]
    assert hub.uploads[0][1]["disk_format"] == "raw"
    assert hub.uploads[-1][1]["name"] == "selected-build"
    assert hub.uploads[-1][1]["parent_digest"] == root
    assert all(metadata["is_published"] is False for _, metadata in hub.uploads)


@pytest.mark.parametrize("mutation", ["cycle", "missing-base", "different-base"])
def test_chain_rejects_incomplete_or_conflicting_dependencies_before_transfer(tmp_path, mutation):
    backend = remote.RemoteArtifacts(_roots(tmp_path))
    base, root, leaf, metadata, _ = _chain()
    if mutation == "cycle":
        metadata[root]["parent_digest"] = leaf
    elif mutation == "missing-base":
        metadata[root].pop("base_image_digest")
    else:
        metadata[leaf]["base_image_digest"] = "sha256:" + "d" * 64
    with pytest.raises(StateError):
        backend._legacy_chain(leaf, metadata.__getitem__)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://user:password@example.test",
        "https://example.test?token=s",
        "https://example.test#secret",
    ],
)
def test_legacy_production_url_rejects_insecure_or_embedded_credentials(url):
    with pytest.raises(StateError):
        remote._safe_url(url)


def test_loopback_http_only_for_isolated_hub():
    assert remote._safe_url("http://127.0.0.1:8123/") == "http://127.0.0.1:8123"


def test_hub_identity_requires_explicit_project_and_action(monkeypatch):
    client = remote._ConnectedHub("https://example.test", "synthetic-token", "explicit-project", ())
    actor = {"project_id": "other-project", "user_id": "user", "capabilities": {"upload": True}}
    monkeypatch.setattr(client, "_json_request", lambda *_args, **_kwargs: actor)
    with pytest.raises(StateError, match="explicit PALIMPSEST_PROJECT_ID"):
        client.identity("upload")
    actor["project_id"] = "explicit-project"
    actor["capabilities"]["upload"] = False
    with pytest.raises(StateError, match="does not authorize"):
        client.identity("upload")


def test_native_profile_never_consumes_global_other_connection_key(tmp_path, monkeypatch):
    backend = remote.RemoteArtifacts(_roots(tmp_path))
    profile = RegistryProfile(
        "team", "example.test", protocol="palimpsest", api_base="https://example.test/v1", namespace="team"
    )
    monkeypatch.setenv("PALIMPSEST_PACKAGE_KEY", "foreign-key-not-for-this-profile")
    monkeypatch.setattr(remote, "get_package_key", lambda profile, namespace: None)
    with pytest.raises(StateError, match="credential-helper key"):
        backend._native(profile)


def test_non_docker_registry_truthfully_has_no_global_search(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    save_registry_config(
        roots,
        add_profile(default_registry_config(), RegistryProfile("private", "registry.example.test", namespace="team")),
    )
    monkeypatch.setattr(
        remote, "inspect_registry_reference", lambda *_args, **_kwargs: pytest.fail("no exact repository was selected")
    )
    result = remote.RemoteArtifacts(roots).browse("registry:private", query="app")
    assert result["items"] == []
    assert result["search_mode"] == "exact-reference/tags"
    assert "no configured repository search API" in result["message"]


def test_docker_hub_search_uses_authoritative_https_and_repo_followthrough(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    seen = []

    class Response(io.BytesIO):
        def geturl(self):
            return seen[0]

    class Opener:
        def open(self, request, timeout):
            seen.append(request.full_url)
            assert request.get_header("Authorization") is None
            assert timeout == 15
            return Response(json.dumps({"results": [{"repo_name": "library/ubuntu"}]}).encode())

    monkeypatch.setattr(remote, "_opener", lambda *_args: Opener())
    result = remote.RemoteArtifacts(roots).browse("registry:docker", query="ubuntu", limit=1)
    assert seen[0].startswith("https://hub.docker.com/v2/search/repositories/?")
    assert result["items"][0]["repository"] == "library/ubuntu"
    assert result["items"][0]["download_supported"] is False


def test_oci_exact_manifest_reports_architecture_not_conventional_vm(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    calls = []

    def inspect(reference, roots, *, profile, raw=False, **kwargs):
        calls.append((reference, raw))
        if raw:
            return {"schemaVersion": 2, "config": {}, "layers": []}
        return {"Architecture": "amd64", "Digest": "sha256:" + "a" * 64}

    monkeypatch.setattr(remote, "inspect_registry_reference", inspect)
    monkeypatch.setattr(remote.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(remote.platform, "machine", lambda: "arm64")
    item = remote.RemoteArtifacts(roots).browse("registry:docker", repository="library/ubuntu:latest")["items"][0]
    assert item["architectures"] == ["amd64"]
    assert item["runtime_kind"] == "oci-root" and item["run_supported"] is False
    assert "not a conventional VM disk" in item["run_limitation"]
    assert calls == [("docker.io/library/ubuntu:latest", True), ("docker.io/library/ubuntu:latest", False)]


def test_foreign_connection_reference_rejected_before_job(tmp_path):
    backend = remote.RemoteArtifacts(_roots(tmp_path))
    with pytest.raises(remote.PalimpsestError):
        backend.start_download(
            SimpleNamespace(start=lambda *_args, **_kwargs: pytest.fail("foreign reference spawned")),
            "registry:docker",
            "foreign.example.test/app:latest",
        )


def test_transfer_job_has_fixed_module_and_no_credentials_argv(tmp_path, monkeypatch):
    backend = remote.RemoteArtifacts(_roots(tmp_path))
    calls = []
    monkeypatch.setenv("PALIMPSEST_TOKEN", "synthetic-secret")
    operations = SimpleNamespace(start=lambda *args, **kwargs: calls.append((args, kwargs)) or {"id": "job"})
    result = backend.start_download(operations, "registry:docker", "library/ubuntu:latest")
    assert result == {"id": "job"}
    assert calls[0][0][2] == ["download", "--", "registry:docker", "library/ubuntu:latest"]
    assert calls[0][1]["module"] == "palimpsest_local.remote_artifacts"
    assert "synthetic-secret" not in repr(calls)


@pytest.mark.parametrize(
    "selector",
    [
        {"kind": "path", "path": "/etc/passwd"},
        {"kind": "local", "reference": "tag", "args": ["--secret"]},
        {"kind": "build", "build_id": "../foreign"},
    ],
)
def test_upload_rejects_arbitrary_paths_args_and_foreign_build_ids(tmp_path, selector):
    with pytest.raises(StateError):
        remote.RemoteArtifacts(_roots(tmp_path))._source(selector)


def test_runtime_pack_keeps_manifest_identity_for_legacy_upload():
    _, root, _, metadata, _ = _chain()
    record = metadata[root]
    record["runtime_pack_manifest_digest"] = "sha256:" + "f" * 64
    assert remote._descriptor(record)["chain_id"] == record["runtime_pack_manifest_digest"]


def test_remote_retained_source_cannot_change_after_download(tmp_path):
    roots = _roots(tmp_path)
    backend = remote.RemoteArtifacts(roots)
    archive = roots.state / "retained.oci.tar"
    archive.write_bytes(b"original")
    reference = "docker.io/library/app:latest"
    connection = "registry:docker"
    identity = hashlib.sha256((connection + "\0" + reference).encode()).hexdigest()
    path = roots.state / "remote-references" / (identity + ".json")
    atomic_write_json(
        path,
        {
            "schema": "palimpsest-remote-reference-v1",
            "connection_id": connection,
            "requested_reference": reference,
            "kind": "oci-image",
            "archive": str(archive),
            "archive_digest": digest_file(archive),
            "package_manifest": "sha256:" + "b" * 64,
        },
    )
    selector = {"kind": "remote", "connection_id": connection, "reference": reference}
    assert backend._source(selector)["archive"] == str(archive)
    archive.write_bytes(b"changed")
    with pytest.raises(StateError, match="has changed"):
        backend._source(selector)


def test_hub_json_and_blob_responses_are_bounded():
    with pytest.raises(StateError, match="byte limit"):
        remote._read_json(io.BytesIO(b"x" * (remote._MAX_JSON + 1)))
    response = remote._BoundedResponse(io.BytesIO(b"oversize"))
    response.remaining = 2
    with pytest.raises(StateError, match="16 GiB"):
        response.read(10)


def test_tag_pages_are_one_inspection_not_per_tag(tmp_path, monkeypatch):
    backend = remote.RemoteArtifacts(_roots(tmp_path))
    calls = []

    def inspect(reference, roots, *, profile, tags, timeout_seconds):
        calls.append(reference)
        assert tags is True and timeout_seconds == 10
        return {"Tags": ["v3", "v1", "v2"]}

    monkeypatch.setattr(remote, "inspect_registry_reference", inspect)
    page = backend.browse("registry:docker", repository="library/app", limit=1, cursor="1")
    assert page["next_cursor"] == "2"
    assert page["items"][0]["reference"] == "docker.io/library/app:v2"
    assert page["items"][0]["repository"] == "docker.io/library/app:v2"
    assert len(calls) == 1


def test_legacy_digest_prefix_pagination_preserves_chain_fields(tmp_path, monkeypatch):
    backend = remote.RemoteArtifacts(_roots(tmp_path))
    _, root, leaf, metadata, blobs = _chain()
    hub = _Hub(metadata, blobs)
    seen = []

    def listing(**kwargs):
        seen.append(kwargs)
        return [metadata[root], metadata[leaf]]

    hub.list_layers = listing
    monkeypatch.setattr(backend, "_hub", lambda: hub)
    page = backend.browse("legacy", query="sha256:abcd", limit=1, cursor="3")
    assert seen == [{"digest_prefix": "sha256:abcd", "kind": None, "limit": 2, "offset": 3}]
    assert page["next_cursor"] == "4"
    assert page["items"][0]["base_digest"] == metadata[root]["base_image_digest"]


def test_qualified_query_inspects_instead_of_public_search(tmp_path, monkeypatch):
    backend = remote.RemoteArtifacts(_roots(tmp_path))
    monkeypatch.setattr(remote, "_opener", lambda *_args: pytest.fail("public search invoked for exact reference"))
    monkeypatch.setattr(
        remote,
        "inspect_registry_reference",
        lambda *_args, **_kwargs: {
            "manifests": [{"platform": {"architecture": "amd64"}}],
        },
    )
    page = backend.browse("registry:docker", query="docker.io/library/app:latest")
    assert page["items"][0]["reference"] == "docker.io/library/app:latest"
    assert page["search_mode"] == "exact-reference/tags"


def test_shared_read_budget_rejects_drip_response(tmp_path, monkeypatch):
    moments = iter([0.0, 0.5, 1.1])
    monkeypatch.setattr(remote.time, "monotonic", lambda: next(moments))
    response = remote._BoundedResponse(io.BytesIO(b"{}"), deadline=1.0)
    with pytest.raises(StateError, match="aggregate read budget"):
        response.read(10)


@pytest.mark.parametrize("cursor", ["https://evil.test/page", "-1", "1;secret", "x" * 100])
def test_page_cursor_is_typed_not_remote_url(cursor):
    with pytest.raises(StateError):
        remote._page_cursor(cursor)


def test_hub_client_exposes_digest_prefix_and_offset_without_token_in_query(monkeypatch):
    from palimpsest_local.hub import HubClient

    client = HubClient("https://example.test", "synthetic-secret", project_id="explicit-project")
    seen = []
    monkeypatch.setattr(client, "_json_request", lambda *args, **kwargs: seen.append((args, kwargs)) or [])
    assert client.list_layers(digest_prefix="sha256:abcd", offset=30, limit=31) == []
    assert seen[0][1]["query"]["digest_prefix"] == "sha256:abcd"
    assert seen[0][1]["query"]["offset"] == 30
    assert "synthetic-secret" not in repr(seen)


@pytest.mark.parametrize(
    "platform, supported",
    [
        ({"os": "linux", "architecture": "amd64"}, True),
        ({"os": "windows", "architecture": "amd64"}, False),
        ({"os": "linux", "architecture": "arm64"}, False),
        ({"architecture": "amd64"}, False),
    ],
)
def test_exact_oci_download_and_run_require_linux_amd64(tmp_path, monkeypatch, platform, supported):
    backend = remote.RemoteArtifacts(_roots(tmp_path))
    monkeypatch.setattr(remote.platform, "system", lambda: "Linux")
    monkeypatch.setattr(remote.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        remote, "inspect_registry_reference", lambda *_args, **_kwargs: {"manifests": [{"platform": platform}]}
    )
    page = backend.browse("registry:docker", query="docker.io/library/app:latest")
    item = page["items"][0]
    assert item["platforms"] == [platform]
    assert item["download_supported"] is supported
    assert item["run_supported"] is supported
    assert item["runtime_kind"] == "oci-root"


def test_connections_are_secret_free_and_do_not_claim_credential_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_URL", "https://hub.example.test")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "synthetic-token-never-exported")
    monkeypatch.setenv("PALIMPSEST_PROJECT_ID", "explicit-project")
    rows = remote.RemoteArtifacts(_roots(tmp_path)).connections()
    legacy = next(row for row in rows if row["id"] == "legacy")
    assert legacy["label"] == "Configured Hub"
    assert legacy["namespace"] == "explicit-project"
    assert legacy["credential_available"] is True
    assert legacy["capabilities"]["upload"] is True
    registry = next(row for row in rows if row["id"] == "registry:docker")
    assert registry["credential_available"] is None
    assert registry["credential_state"] == "resolved-on-use"
    assert registry["capabilities"]["upload"] is False
    assert "synthetic-token-never-exported" not in repr(rows)


def test_legacy_download_preserves_existing_canonical_runtime_pack_metadata(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    backend = remote.RemoteArtifacts(roots)
    base, root, leaf, metadata, blobs = _chain()
    store = ContentStore(roots.store)
    store.write_stream([blobs[root]], expected_digest=root)
    canonical = {
        **metadata[root],
        "name": "canonical-local-name",
        "runtime_pack_manifest_digest": _digest(b"verified-runtime-pack-manifest"),
        "oci_vm_proof": {"source": "verified-local-oci-root"},
    }
    store.write_metadata(root, canonical)
    before = store.read_metadata(root)
    monkeypatch.setattr(backend, "_hub", lambda: _Hub(metadata, blobs))
    result = backend.download("legacy", leaf)
    assert result["image"] == base and result["layers"] == [root, leaf]
    assert store.read_metadata(root) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("parent_digest", "sha256:" + "b" * 64),
        ("base_image_digest", "sha256:" + "c" * 64),
        ("media_type", "application/octet-stream"),
    ],
)
def test_legacy_download_rejects_existing_sidecar_conflicts_before_copy(tmp_path, monkeypatch, field, value):
    roots = _roots(tmp_path)
    backend = remote.RemoteArtifacts(roots)
    _, root, leaf, metadata, blobs = _chain()
    store = ContentStore(roots.store)
    store.write_stream([blobs[root]], expected_digest=root)
    store.write_metadata(root, {**metadata[root], field: value})
    before = store.read_metadata(root)
    hub = _Hub(metadata, blobs)
    monkeypatch.setattr(hub, "pull_blob", lambda *_args, **_kwargs: pytest.fail("blob copy preceded sidecar preflight"))
    monkeypatch.setattr(backend, "_hub", lambda: hub)
    with pytest.raises(StateError):
        backend.download("legacy", leaf)
    assert store.read_metadata(root) == before
    assert not store.exists(leaf)


@pytest.mark.parametrize("runtime_pack", [False, True])
def test_legacy_layer_only_chain_downloads_and_reuploads_without_fake_base(tmp_path, monkeypatch, runtime_pack):
    roots = _roots(tmp_path)
    backend = remote.RemoteArtifacts(roots)
    base, root, leaf, metadata, blobs = _chain()
    metadata[root].pop("base_image_digest")
    metadata[leaf].pop("base_image_digest")
    pack_manifest = _digest(b"runtime-pack-manifest")
    if runtime_pack:
        metadata[root]["runtime_pack_manifest_digest"] = pack_manifest
    reference = root if runtime_pack else leaf
    layers = [root] if runtime_pack else [root, leaf]
    hub = _Hub(metadata, blobs)
    monkeypatch.setattr(backend, "_hub", lambda: hub)
    result = backend.download("legacy", reference)
    assert result["run_supported"] is False
    assert "complete cloud-image base" in result["run_limitation"]
    assert result["source"] == reference and result["image"] is None
    assert result["layers"] == layers
    assert Path(result["receipt"]).is_file()
    store = ContentStore(roots.store)
    assert not store.exists(base)
    for digest in layers:
        assert store.verify_blob(digest) is not None
    upload = backend.upload(
        "legacy", {"kind": "remote", "connection_id": "legacy", "reference": reference}, "retained-layer"
    )
    assert upload["uploaded"] == len(layers)
    assert [digest for digest, _ in hub.uploads] == layers
    assert hub.actions == ["download", "upload"]
    if runtime_pack:
        assert store.read_metadata(root)["runtime_pack_manifest_digest"] == pack_manifest
        assert hub.uploads[-1][1]["chain_id"] == pack_manifest


@pytest.mark.parametrize("method", ["GET", "PATCH", "PUT"])
def test_legacy_upload_control_responses_obey_json_not_blob_limit(monkeypatch, method):
    hub = remote._ConnectedHub("https://example.test", "synthetic-token", "a" * 32, ())
    response = io.BytesIO(b"x" * (remote._MAX_JSON + 1))
    response.status = 200
    response.geturl = lambda: "https://example.test/v1/uploads/session"
    hub._opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
    with pytest.raises(StateError, match="JSON response exceeds"):
        with hub._open(method, "/uploads/session") as bounded:
            bounded.read()
    assert response.closed


def test_legacy_blob_response_keeps_transfer_byte_limit():
    hub = remote._ConnectedHub("https://example.test", "synthetic-token", "a" * 32, ())
    digest = "sha256:" + "b" * 64
    payload = b"x" * (remote._MAX_JSON + 1)
    response = io.BytesIO(payload)
    response.status = 200
    response.geturl = lambda: "https://example.test/v1/layers/" + digest + "/blob"
    hub._opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
    with hub._open("GET", "/layers/" + digest + "/blob") as bounded:
        assert bounded.max_bytes == 16 * 1024**3
        assert bounded.read() == payload


@pytest.mark.parametrize("kind", ["cloud-image", "runtime-bundle"])
def test_conventional_vm_compatibility_rejects_darwin_x86_64(monkeypatch, kind):
    monkeypatch.setattr(remote.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(remote.platform, "machine", lambda: "x86_64")
    result = remote._run_info(kind, ["x86_64"])
    assert result["run_supported"] is False
    assert "Darwin x86_64 is unsupported" in result["run_limitation"]
    monkeypatch.setattr(remote.platform, "machine", lambda: "arm64")
    assert remote._run_info(kind, ["aarch64"])["run_supported"] is True


def test_package_upload_source_is_a_verified_typed_record_not_a_filesystem_path(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    backend = remote.RemoteArtifacts(roots)
    archive = tmp_path / "managed.oci.tar"
    archive.write_bytes(b"digest-pinned-record-fixture")
    reference = "example.test/team/app:selected"
    root = _digest(b"selected-package-root")
    record = SimpleNamespace(archive=str(archive), archive_digest=digest_file(archive), root_digest=root)
    seen = []
    monkeypatch.setattr(remote, "read_package_reference", lambda selected, ref: seen.append((selected, ref)) or record)
    source = backend._source({"kind": "package", "reference": reference})
    assert source == {"kind": "package", "archive": str(archive), "manifest": root}
    assert seen == [(roots, reference)]
    with pytest.raises(StateError, match="local CAS upload"):
        backend._source({"kind": "local", "reference": reference})
    with pytest.raises(StateError, match="unsupported upload source"):
        backend._source({"kind": "package", "archive": str(archive), "manifest": root})
    archive.write_bytes(b"changed-after-record")
    with pytest.raises(StateError, match="native local source has changed"):
        backend._source({"kind": "package", "reference": reference})
