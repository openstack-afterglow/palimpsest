"""Native package source snapshots and HTTPS transport behavior (no live services)."""

from __future__ import annotations

import base64
import email.message
import hashlib
import io
import json
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
import uuid

import pytest

from palimpsest_local import packages
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.package_source import PackageLimits, snapshot_package
from palimpsest_local.packages import NativePackageClient, PackageError, PackageHTTPError
from palimpsest_local.registry import RegistryProfile

_KEY_ID = "1" * 32
_KEY = "ppk_v1_" + _KEY_ID + "." + base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
_PROJECT = "Project.Exact-1"
_OWNER = "a" * 64  # federated Keystone user IDs are 64-hex, not UUIDs
_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_INDEX = "application/vnd.oci.image.index.v1+json"
_CONFIG = "application/vnd.oci.image.config.v1+json"
_LAYER = "application/vnd.oci.image.layer.v1.tar"
_RUNTIME_CONFIG = "application/vnd.afterglow.palimpsest.layer.config.v1+json"
_SQUASHFS = "application/vnd.afterglow.palimpsest.layer.squashfs.v1"
_QCOW2 = "application/vnd.afterglow.palimpsest.image.qcow2.v1"
_BASE = "https://example.test/api/custom"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _blob(layout, media_type, payload):
    digest = _digest(payload)
    (layout / "blobs" / "sha256" / digest.split(":")[1]).write_bytes(payload)
    return {"mediaType": media_type, "digest": digest, "size": len(payload)}


def _new_layout(path):
    (path / "blobs" / "sha256").mkdir(parents=True)
    (path / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')


def _set_roots(layout, roots):
    (layout / "index.json").write_bytes(_json({"schemaVersion": 2, "mediaType": _INDEX, "manifests": roots}))


def _image(layout, architecture="amd64", config_extra=None):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        entry = tarfile.TarInfo("hello")
        entry.size = len(architecture)
        tar.addfile(entry, io.BytesIO(architecture.encode()))
    layer_bytes = payload.getvalue()
    layer = _blob(layout, _LAYER, layer_bytes)
    config = {
        "os": "linux",
        "architecture": architecture,
        "rootfs": {"type": "layers", "diff_ids": [_digest(layer_bytes)]},
    }
    config.update(config_extra or {})
    config_descriptor = _blob(layout, _CONFIG, _json(config))
    return _blob(
        layout,
        _MANIFEST,
        _json({"schemaVersion": 2, "mediaType": _MANIFEST, "config": config_descriptor, "layers": [layer]}),
    )


def _layout(path):
    _new_layout(path)
    root = _image(path)
    _set_roots(path, [root])
    return root


def _archive(layout, destination):
    with tarfile.open(destination, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(layout.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(layout).as_posix(), recursive=False)


def _actor(**changes):
    return {
        "actor_type": "package-key",
        "key_id": str(uuid.UUID(_KEY_ID)),
        "name": "ci",
        "project_id": _PROJECT,
        "owner_user_id": _OWNER,
        "namespace": "team",
        "scope": {"packages": ["app"]},
        "actions": ["packages:read", "packages:write", "cache:read", "cache:write"],
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "revoked_at": None,
        **changes,
    }


class _Response(io.BytesIO):
    def __init__(self, data, url, status=200, headers=None):
        super().__init__(data)
        self.url, self.status, self.headers = url, status, headers or {}

    def geturl(self):
        return self.url


def _error(request, status, body):
    return urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))


def _not_found(request):
    return _error(request, 404, _json({"error": {"code": "NOT_FOUND", "message": "missing", "request_id": "r"}}))


class _Hub:
    """Fake Hub that enforces the bearer key and serves server-shaped bodies."""

    def __init__(self, actor=None):
        self.actor = actor or _actor()
        self.routes = {}
        self.requests = []
        self.uploaded = b""

    def open(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        assert url.scheme == "https" and url.netloc == "example.test"
        assert request.get_header("Authorization") == "Bearer " + _KEY
        path = url.path.removeprefix("/api/custom")
        self.requests.append((request.get_method(), path))
        if path == "/auth/me":
            return _Response(_json(self.actor), request.full_url)
        handler = self.routes.get((request.get_method(), path))
        result = _not_found(request) if handler is None else handler(request)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, dict):
            return _Response(_json(result), request.full_url)
        return result


def _client(hub):
    profile = RegistryProfile("native", "example.test", protocol="palimpsest", api_base=_BASE, namespace="team")
    client = NativePackageClient(profile, credential=_KEY)
    client._opener = hub
    return client


def _session():
    return {
        "upload_id": "3" * 32,
        "project_id": _PROJECT,
        "namespace": "team",
        "package": "app",
        "key_id": str(uuid.UUID(_KEY_ID)),
        "owner_user_id": _OWNER,
        "received_bytes": 0,
        "expires_at": "2099-01-01T00:00:00Z",
        "status": "uploading",
        "result": None,
    }


def _version(snapshot):
    return {
        "project_id": _PROJECT,
        "namespace": "team",
        "package": "app",
        "root_digest": snapshot.root_digest,
        "root_media_type": snapshot.root_media_type,
        "package_type": snapshot.package_type,
        "graph": snapshot.graph,
        "platforms": list(snapshot.platforms),
        "archive_digest": snapshot.archive_digest,
        "archive_size_bytes": snapshot.archive_size_bytes,
        "total_bytes": 1,
        "provenance": {},
        "pushed_by": "b" * 32,
        "pushed_key_id": str(uuid.uuid4()),
        "pushed_at": "2026-01-01T00:00:00Z",
        "root_descriptor": {"digest": snapshot.root_digest, "mediaType": snapshot.root_media_type, "size": 1},
    }


# Source snapshots ----------------------------------------------------------


def test_tar_snapshot_preserves_original_bytes_after_source_changes(tmp_path):
    layout = tmp_path / "layout"
    root = _layout(layout)
    archive = tmp_path / "image.tar"
    _archive(layout, archive)
    original = archive.read_bytes()
    with snapshot_package(archive) as snapshot:
        archive.write_bytes(b"replaced after the snapshot")
        assert snapshot.archive.read_bytes() == original
        assert (snapshot.archive_digest, snapshot.archive_size_bytes) == (_digest(original), len(original))
        assert (snapshot.root_digest, snapshot.package_type) == (root["digest"], "oci-image")
    assert not snapshot.archive.exists()


def test_directory_snapshots_are_byte_reproducible(tmp_path):
    layout = tmp_path / "layout"
    _layout(layout)
    with snapshot_package(layout) as first:
        first_bytes = first.archive.read_bytes()
    (layout / "index.json").touch()  # metadata-only change must not alter archive bytes
    with snapshot_package(layout) as second:
        assert second.archive.read_bytes() == first_bytes
        assert second.archive_digest == first.archive_digest


def test_multi_root_requires_manifest_and_index_keeps_every_platform(tmp_path):
    layout = tmp_path / "layout"
    _new_layout(layout)
    amd = _image(layout)
    arm = _image(layout, "arm64")
    index = _blob(
        layout,
        _INDEX,
        _json(
            {
                "schemaVersion": 2,
                "mediaType": _INDEX,
                "manifests": [
                    {**amd, "platform": {"os": "linux", "architecture": "amd64"}},
                    {**arm, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
                ],
            }
        ),
    )
    _set_roots(layout, [amd, index])
    with pytest.raises(ArtifactValidationError, match="specify --manifest"):
        with snapshot_package(layout):
            pass
    with snapshot_package(layout, manifest=index["digest"]) as snapshot:
        assert (snapshot.root_digest, snapshot.root_media_type) == (index["digest"], _INDEX)
        # A config without variant takes the declared index variant.
        assert snapshot.platforms == (
            {"os": "linux", "architecture": "amd64"},
            {"os": "linux", "architecture": "arm64", "variant": "v8"},
        )


@pytest.mark.parametrize("subject_is_sibling", [True, False])
def test_buildkit_attestation_child_is_verified_but_not_a_platform(tmp_path, subject_is_sibling):
    layout = tmp_path / "layout"
    _new_layout(layout)
    image = _image(layout)
    statement = _blob(layout, "application/vnd.in-toto+json", b'{"_type":"https://in-toto.io/Statement/v0.1"}')
    config = _blob(
        layout,
        _CONFIG,
        _json(
            {
                "architecture": "unknown",
                "os": "unknown",
                "rootfs": {"type": "layers", "diff_ids": [statement["digest"]]},
            }
        ),
    )
    attestation = _blob(
        layout, _MANIFEST, _json({"schemaVersion": 2, "mediaType": _MANIFEST, "config": config, "layers": [statement]})
    )
    subject = image["digest"] if subject_is_sibling else "sha256:" + "7" * 64
    index = _blob(
        layout,
        _INDEX,
        _json(
            {
                "schemaVersion": 2,
                "mediaType": _INDEX,
                "manifests": [
                    {**image, "platform": {"os": "linux", "architecture": "amd64"}},
                    {
                        **attestation,
                        "platform": {"os": "unknown", "architecture": "unknown"},
                        "annotations": {
                            "vnd.docker.reference.type": "attestation-manifest",
                            "vnd.docker.reference.digest": subject,
                        },
                    },
                ],
            }
        ),
    )
    _set_roots(layout, [index])
    if not subject_is_sibling:
        with pytest.raises(ArtifactValidationError, match="sibling"):
            with snapshot_package(layout):
                pass
        return
    with snapshot_package(layout) as snapshot:
        assert snapshot.platforms == ({"os": "linux", "architecture": "amd64"},)
        assert statement["digest"] in snapshot.graph


def test_index_platform_contradicting_config_is_rejected(tmp_path):
    layout = tmp_path / "layout"
    _new_layout(layout)
    arm = _image(layout, "arm64", {"variant": "v7"})
    index = _blob(
        layout,
        _INDEX,
        _json(
            {
                "schemaVersion": 2,
                "mediaType": _INDEX,
                "manifests": [{**arm, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}}],
            }
        ),
    )
    _set_roots(layout, [index])
    with pytest.raises(ArtifactValidationError, match="disagrees"):
        with snapshot_package(layout):
            pass


def test_runtime_bundle_resolves_ancestor_configs_from_other_roots(tmp_path):
    layout = tmp_path / "layout"
    _new_layout(layout)
    base = _blob(layout, _QCOW2, b"qcow2-base")
    first = _blob(layout, _SQUASHFS, b"hsqs-first")
    second = _blob(layout, _SQUASHFS, b"hsqs-second")

    def root(layers, config):
        descriptor = _blob(layout, _RUNTIME_CONFIG, _json(config))
        return _blob(
            layout,
            _MANIFEST,
            _json({"schemaVersion": 2, "mediaType": _MANIFEST, "config": descriptor, "layers": layers}),
        )

    base_root = root([base], {"kind": "cloud-image", "disk_format": "qcow2", "arch": "x86_64"})
    first_root = root([base, first], {"kind": "squashfs", "parent_digest": None, "base_image_digest": base["digest"]})
    chain = "sha256:" + hashlib.sha256(f"{first['digest']} {second['digest']}".encode()).hexdigest()
    leaf_config = {
        "kind": "squashfs",
        "parent_digest": first["digest"],
        "base_image_digest": base["digest"],
        "chain_id": chain,
    }
    leaf_root = root([base, first, second], leaf_config)
    _set_roots(layout, [base_root, first_root, leaf_root])
    with snapshot_package(layout, manifest=leaf_root["digest"]) as snapshot:
        assert snapshot.package_type == "runtime-bundle"
        assert snapshot.platforms == ({"os": "linux", "architecture": "amd64"},)
        assert {first_root["digest"], base_root["digest"], leaf_root["digest"]} <= set(snapshot.graph)

    bad_root = root([base, first, second], {**leaf_config, "parent_digest": None})
    _set_roots(layout, [base_root, first_root, bad_root])
    with pytest.raises(ArtifactValidationError, match="parent"):
        with snapshot_package(layout, manifest=bad_root["digest"]):
            pass


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside", "file"),
        ("blobs/sha256/" + "0" * 64, "symlink"),
        ("index.json", "duplicate"),
        ("./oci-layout", "file"),
    ],
)
def test_archive_rejects_traversal_links_duplicates_and_dot_paths(tmp_path, name, kind):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo(name)
        if kind == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
            tar.addfile(info)
        else:
            info.size = 2
            tar.addfile(info, io.BytesIO(b"{}"))
            if kind == "duplicate":
                tar.addfile(info, io.BytesIO(b"{}"))
    with pytest.raises(ArtifactValidationError):
        with snapshot_package(archive):
            pass
    assert not (tmp_path / "outside").exists()


def test_snapshot_rejects_diffid_mismatch_and_member_limit(tmp_path):
    layout = tmp_path / "layout"
    _new_layout(layout)
    root = _image(layout, config_extra={"rootfs": {"type": "layers", "diff_ids": ["sha256:" + "0" * 64]}})
    _set_roots(layout, [root])
    with pytest.raises(ArtifactValidationError, match="DiffID"):
        with snapshot_package(layout):
            pass
    with pytest.raises(ArtifactValidationError, match="too many members"):
        with snapshot_package(layout, limits=PackageLimits(max_members=2)):
            pass


# Authentication --------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_type": "member"},
        {"namespace": "other"},
        {"key_id": str(uuid.UUID("2" * 32))},
        {"scope": {"packages": ["app/child"]}},
        {"actions": ["packages:read"]},
        {"actions": [["packages:read"]]},
        {"actions": [{"packages:read": True}]},
        {"project_id": "../escape"},
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"revoked_at": "2026-01-01T00:00:00Z"},
    ],
)
def test_push_authorization_fails_before_any_package_request(tmp_path, changes):
    layout = tmp_path / "layout"
    _layout(layout)
    hub = _Hub(_actor(**changes))
    with snapshot_package(layout) as snapshot:
        with pytest.raises(PackageError):
            _client(hub).push("app", "v1", snapshot)
    assert hub.requests == [("GET", "/auth/me")]


def test_federated_owner_and_opaque_project_are_preserved_exactly():
    client = _client(_Hub())
    client.authorize("app", ("packages:read", "cache:write"))
    assert (client.project_id, client.owner_user_id) == (_PROJECT, _OWNER)


def test_redirect_is_not_followed_and_key_reaches_only_configured_authority(monkeypatch):
    opened = []

    class RedirectingHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request):
            opened.append(urllib.parse.urlsplit(request.full_url).netloc)
            headers = email.message.Message()
            headers["Location"] = "https://attacker.test/steal"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    monkeypatch.setattr(packages.urllib.request, "HTTPSHandler", RedirectingHTTPS)
    profile = RegistryProfile("native", "example.test", protocol="palimpsest", api_base=_BASE, namespace="team")
    with pytest.raises(PackageHTTPError) as error:
        NativePackageClient(profile, credential=_KEY).authenticate()
    assert error.value.status == 302
    assert opened == ["example.test"]


@pytest.mark.parametrize(
    "status,body",
    [(404, b"<html>gateway</html>"), (403, _json({"error": {"code": "PACKAGE_SCOPE_DENIED"}})), (503, b"")],
)
def test_only_enveloped_404_is_an_authoritative_miss(status, body):
    hub = _Hub()
    hub.routes[("GET", "/projects/team/resolve")] = lambda request: _error(request, status, body)
    hub.routes[("GET", "/projects/team/cache/resolve")] = lambda request: _error(request, status, body)
    client = _client(hub)
    with pytest.raises(PackageHTTPError):
        client.resolve("app", "v1")
    with pytest.raises(PackageHTTPError):
        client.resolve_cache(
            "app", build_key="sha256:" + "4" * 64, cache_scope="main", platform="linux/amd64", builder_fingerprint="bk"
        )
    hub.routes.clear()
    assert client.resolve("app", "v1") is None


# Push/pull -------------------------------------------------------------------


def _upload_routes(hub, publish, *, ack=None):
    hub.starts = []

    def start(request):
        hub.starts.append(json.loads(request.data))
        return _session()

    def patch(request):
        offset = int(request.get_header("Upload-offset"))
        assert offset == len(hub.uploaded)
        while chunk := request.data.read(65536):
            hub.uploaded += chunk
        return _Response(b"", request.full_url, 204, {"Upload-Offset": str(len(hub.uploaded)) if ack is None else ack})

    session = "/projects/team/uploads/" + "3" * 32
    hub.routes[("POST", "/projects/team/uploads")] = start
    hub.routes[("PATCH", session)] = patch
    hub.routes[("PUT", session)] = publish
    hub.routes[("DELETE", session)] = lambda request: _Response(b"", request.full_url, 204)


def test_push_streams_frozen_bytes_with_tag_cas_and_accepts_shared_version_receipt(tmp_path):
    layout = tmp_path / "layout"
    _layout(layout)
    previous = "sha256:" + "9" * 64
    hub = _Hub()
    with snapshot_package(layout) as snapshot:
        hub.routes[("GET", "/projects/team/resolve")] = lambda request: {
            **_version(snapshot),
            "root_digest": previous,
            "digest": previous,
            "tag": "v1",
            "package_type": "oci-image",
        }
        publish = {
            "project_id": _PROJECT,
            "namespace": "team",
            "package": "app",
            "tag": "v1",
            "digest": snapshot.root_digest,
            "package_type": "oci-image",
            "visibility": "project",
            "platforms": [{"os": "linux", "architecture": "amd64"}],
            "already_published": False,
            # Another member first published this immutable root.
            "pushed_by": "Other.Member",
            "pushed_key_id": str(uuid.uuid4()),
            "archive_digest": "sha256:" + "8" * 64,
            "archive_size_bytes": 10,
            "web_url": "https://example.test/p",
        }
        _upload_routes(hub, lambda request: publish)
        receipt = _client(hub).push("app", "v1", snapshot, provenance={"build_id": "b1"})
        frozen = snapshot.archive.read_bytes()
    assert hub.uploaded == frozen
    assert hub.starts[0]["expected_tag_digest"] == previous
    assert hub.starts[0]["provenance"] == {"build_id": "b1"}
    assert receipt["digest"] == snapshot.root_digest
    assert (receipt["upload_archive_digest"], receipt["upload_archive_size_bytes"]) == (
        snapshot.archive_digest,
        len(frozen),
    )


def test_push_wrong_offset_ack_aborts_session_without_finalizing(tmp_path):
    layout = tmp_path / "layout"
    _layout(layout)
    hub = _Hub()
    _upload_routes(hub, lambda request: pytest.fail("must not finalize"), ack="0")
    with snapshot_package(layout) as snapshot:
        with pytest.raises(PackageError, match="byte offset"):
            _client(hub).push("app", "v1", snapshot)
    assert hub.starts[0]["expected_tag_digest"] is None
    assert ("DELETE", "/projects/team/uploads/" + "3" * 32) in hub.requests
    assert ("PUT", "/projects/team/uploads/" + "3" * 32) not in hub.requests


def _pull_hub(snapshot, payload):
    hub = _Hub()
    version = f"/projects/team/versions/{snapshot.root_digest}"
    hub.routes[("GET", version)] = lambda request: _version(snapshot)
    hub.routes[("GET", version + "/download")] = lambda request: _Response(payload, request.full_url)
    return hub


def test_pull_verifies_graph_before_replacing_destination(tmp_path):
    layout = tmp_path / "layout"
    _layout(layout)
    archive = tmp_path / "source.tar"
    _archive(layout, archive)
    destination = tmp_path / "out" / "image.tar"
    destination.parent.mkdir()
    destination.write_bytes(b"previous valid archive")
    with snapshot_package(archive) as snapshot:
        payload = snapshot.archive.read_bytes()
        with pytest.raises(PackageError, match="digest/size"):
            _client(_pull_hub(snapshot, payload[:-1] + b"x")).pull(
                "app", digest=snapshot.root_digest, destination=destination
            )
        assert destination.read_bytes() == b"previous valid archive"
        receipt = _client(_pull_hub(snapshot, payload)).pull(
            "app", digest=snapshot.root_digest, destination=destination
        )
    assert destination.read_bytes() == payload
    assert (receipt["digest"], receipt["archive_digest"]) == (snapshot.root_digest, _digest(payload))
    assert sorted(path.name for path in destination.parent.iterdir()) == ["image.tar"]


@pytest.mark.parametrize("mutation", ["missing", "extra", "identity"])
def test_pull_rejects_inexact_graph_without_replacing_destination(tmp_path, mutation):
    layout = tmp_path / "layout"
    _layout(layout)
    destination = tmp_path / "image.tar"
    destination.write_bytes(b"previous verified package")
    with snapshot_package(layout) as snapshot:
        hub = _pull_hub(snapshot, snapshot.archive.read_bytes())
        graph = dict(snapshot.graph)
        if mutation == "missing":
            graph.pop(snapshot.root_digest)
        elif mutation == "extra":
            graph["sha256:" + "9" * 64] = {"media_type": _LAYER, "size_bytes": 1}
        else:
            graph[snapshot.root_digest] = {"media_type": _MANIFEST, "size_bytes": 1}
        hub.routes[("GET", f"/projects/team/versions/{snapshot.root_digest}")] = lambda request: {
            **_version(snapshot),
            "graph": graph,
        }
        with pytest.raises(PackageError):
            _client(hub).pull("app", digest=snapshot.root_digest, destination=destination)
    assert destination.read_bytes() == b"previous verified package"


@pytest.mark.parametrize(
    "config_extra",
    [
        {"os.features": "not-an-array"},
        {"os.features": [""]},
        {"history": [{"empty_layer": True}]},
        {"history": [{"empty_layer": 1}]},
        {"history": [{"created_by": 7}]},
    ],
)
def test_source_rejects_invalid_features_and_history_before_upload(tmp_path, config_extra):
    layout = tmp_path / "layout"
    _new_layout(layout)
    root = _image(layout, config_extra=config_extra)
    _set_roots(layout, [root])
    with pytest.raises(ArtifactValidationError):
        with snapshot_package(layout):
            pytest.fail("invalid image metadata was admitted")


# Build cache -----------------------------------------------------------------


def _cache_receipt(payload, **changes):
    return {
        "project_id": _PROJECT,
        "namespace": "team",
        "package": "app",
        "build_key": "sha256:" + "5" * 64,
        "cache_scope": "main",
        "platform": "linux/amd64",
        "builder_fingerprint": "bk",
        "archive_digest": _digest(payload),
        "archive_size_bytes": len(payload),
        "created_at": "2026-01-01T00:00:00Z",
        "created_by": _OWNER,
        "resolution": "scope",
        **changes,
    }


def test_cache_scope_hit_pulls_only_the_resolved_bounded_archive(tmp_path):
    payload = b"cache-archive"
    hub = _Hub()
    hub.routes[("GET", "/projects/team/cache/resolve")] = lambda request: _cache_receipt(payload)
    hub.routes[("GET", "/projects/team/cache/archives/" + _digest(payload))] = lambda request: _Response(
        payload, request.full_url
    )
    client = _client(hub)
    receipt = client.resolve_cache(
        "app", build_key="sha256:" + "4" * 64, cache_scope="main", platform="linux/amd64", builder_fingerprint="bk"
    )
    forged = {**receipt, "archive_size_bytes": receipt["archive_size_bytes"] + 1}
    with pytest.raises(PackageError, match="resolve_cache"):
        client.pull_cache("app", forged, tmp_path / "forged.part")
    target = client.pull_cache("app", receipt, tmp_path / "cache.part")
    assert target.read_bytes() == payload
    assert not (tmp_path / "forged.part").exists()


def test_push_cache_rejects_descriptor_bound_to_another_project_before_upload(tmp_path):
    archive = tmp_path / "cache.tar"
    archive.write_bytes(b"cache")
    hub = _Hub()
    descriptor = {
        "schema": "palimpsest-buildkit-cache-archive-v1",
        "project_id": "another-project",
        "namespace": "team",
        "package": "app",
        "build_key": "sha256:" + "5" * 64,
        "cache_scope": "main",
        "platform": "linux/amd64",
        "builder_fingerprint": "bk",
        "oci_manifest_digest": "sha256:" + "6" * 64,
    }
    with pytest.raises(PackageError, match="another project"):
        _client(hub).push_cache("app", archive, descriptor)
    assert hub.requests == [("GET", "/auth/me")]


def test_source_rejects_config_features_not_declared_by_index(tmp_path):
    layout = tmp_path / "layout"
    _new_layout(layout)
    root = _image(layout, config_extra={"os.features": ["win32k"]})
    index = _blob(
        layout,
        _INDEX,
        _json(
            {
                "schemaVersion": 2,
                "mediaType": _INDEX,
                "manifests": [{**root, "platform": {"os": "linux", "architecture": "amd64"}}],
            }
        ),
    )
    _set_roots(layout, [index])
    with pytest.raises(ArtifactValidationError):
        with snapshot_package(layout):
            pytest.fail("index/config feature contradiction was admitted")


def test_source_rejects_raw_layers_under_docker_manifest(tmp_path):
    layout = tmp_path / "layout"
    root = _layout(layout)
    manifest = json.loads((layout / "blobs" / "sha256" / root["digest"].split(":")[1]).read_bytes())
    config = manifest["config"]
    config["mediaType"] = "application/vnd.docker.container.image.v1+json"
    manifest["mediaType"] = "application/vnd.docker.distribution.manifest.v2+json"
    docker_root = _blob(layout, manifest["mediaType"], _json(manifest))
    _set_roots(layout, [docker_root])
    with pytest.raises(ArtifactValidationError):
        with snapshot_package(layout):
            pytest.fail("Docker manifest with a raw OCI layer was admitted")


@pytest.mark.parametrize("mutation", [None, "platform", "chain_annotation", "arch_type"])
def test_runtime_index_checks_original_platform_and_chain_metadata(tmp_path, mutation):
    layout = tmp_path / "layout"
    _new_layout(layout)
    base = _blob(layout, _QCOW2, b"original cloud base bytes")
    config = _blob(
        layout,
        _RUNTIME_CONFIG,
        _json(
            {
                "kind": "cloud-image",
                "disk_format": "qcow2",
                "arch": [] if mutation == "arch_type" else "x86_64",
            }
        ),
    )
    manifest = {"schemaVersion": 2, "mediaType": _MANIFEST, "config": config, "layers": [base]}
    if mutation == "chain_annotation":
        manifest["annotations"] = {"dev.afterglow.palimpsest.chain-id": "sha256:" + "7" * 64}
    root = _blob(layout, _MANIFEST, _json(manifest))
    index = _blob(
        layout,
        _INDEX,
        _json(
            {
                "schemaVersion": 2,
                "mediaType": _INDEX,
                "manifests": [
                    {
                        **root,
                        "platform": {"os": "linux", "architecture": "arm64" if mutation == "platform" else "amd64"},
                    }
                ],
            }
        ),
    )
    _set_roots(layout, [index])
    if mutation is not None:
        with pytest.raises(ArtifactValidationError):
            with snapshot_package(layout):
                pytest.fail("contradictory runtime index was admitted")
    else:
        with snapshot_package(layout) as snapshot:
            assert snapshot.root_digest == index["digest"]
            assert snapshot.package_type == "runtime-bundle"
            assert snapshot.platforms == ({"os": "linux", "architecture": "amd64"},)


@pytest.mark.parametrize("version", ["Windows Server 2022 build " + "2" * 150, "2" * 257])
def test_platform_version_uses_hub_byte_bound_not_repository_grammar(tmp_path, version):
    layout = tmp_path / "layout"
    _new_layout(layout)
    root = _image(layout, config_extra={"os": "windows", "os.version": version})
    index = _blob(
        layout,
        _INDEX,
        _json(
            {
                "schemaVersion": 2,
                "mediaType": _INDEX,
                "manifests": [{**root, "platform": {"os": "windows", "architecture": "amd64", "os.version": version}}],
            }
        ),
    )
    _set_roots(layout, [index])
    if len(version.encode("utf-8")) > 256:
        with pytest.raises(ArtifactValidationError):
            with snapshot_package(layout):
                pytest.fail("over-limit platform version was admitted")
    else:
        with snapshot_package(layout) as snapshot:
            assert snapshot.platforms == ({"os": "windows", "architecture": "amd64", "os.version": version},)
