"""Isolated protocol exchanges exercise remote identity, paging and integrity."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
import urllib.parse
from email.message import Message

import pytest

from palimpsest_local import docker_hub as docker
from palimpsest_local.errors import PalimpsestError, StateError
from palimpsest_local.registry import RegistryProfile


def _bytes(value):
    return json.dumps(value, separators=(",", ":")).encode()


def _digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


class _Response(io.BytesIO):
    def __init__(self, url, payload, headers=None):
        super().__init__(payload)
        self.url, self.headers, self.status = url, headers or {}, 200

    def geturl(self):
        return self.url


@pytest.fixture
def peer(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker"))
    selected = {"handler": None, "requests": []}

    class Transport:
        def open(self, request, timeout):
            selected["requests"].append(request)
            return selected["handler"](request)

    monkeypatch.setattr(docker.urllib.request, "build_opener", lambda *_args: Transport())
    selected["catalog"] = docker.DockerHubCatalog(RegistryProfile("docker", "docker.io", namespace="library"))
    selected["directory"] = tmp_path / "docker"
    return selected


def _image_graph():
    config = _bytes(
        {
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {"type": "layers", "diff_ids": [_digest(b"uncompressed")]},
            "config": {"Env": ["IMAGE_ENV=not-a-catalog-response"]},
        }
    )
    manifest = _bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": _digest(config),
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": _digest(b"compressed"),
                    "size": 123,
                }
            ],
        }
    )
    index = _bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": _digest(manifest),
                    "size": len(manifest),
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": _digest(b"arm64-manifest"),
                    "size": 15,
                    "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"},
                },
            ],
        }
    )
    return config, manifest, index


def _registry_peer(peer, *, corrupt_config=False, root_headers=None):
    config, manifest, index = _image_graph()

    def handler(request):
        url = urllib.parse.urlsplit(request.full_url)
        if url.hostname == "auth.docker.io":
            return _Response(request.full_url, _bytes({"token": "synthetic-registry-bearer"}))
        if url.path.endswith("/manifests/latest") or url.path.endswith("/manifests/" + _digest(index)):
            return _Response(
                request.full_url, index, root_headers or {"Content-Type": "application/vnd.oci.image.index.v1+json"}
            )
        if url.path.endswith("/manifests/" + _digest(manifest)):
            return _Response(request.full_url, manifest, {"Content-Type": "application/vnd.oci.image.manifest.v1+json"})
        if url.path.endswith("/blobs/" + _digest(config)):
            return _Response(request.full_url, b"changed-config" if corrupt_config else config)
        pytest.fail("unexpected metadata request")

    peer["handler"] = handler
    return config, manifest, index


def test_public_repository_pages_normalize_official_names_without_following_next_urls(peer):
    def handler(request):
        assert request.get_header("Authorization") is None
        page = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["page"][0]
        name = "alpine" if page == "1" else "team/app"
        return _Response(
            request.full_url,
            _bytes(
                {"results": [{"repo_name": name}], "next": "https://untrusted.invalid/page" if page == "1" else None}
            ),
        )

    peer["handler"] = handler
    first = peer["catalog"].search("alpine", limit=1)
    assert first["items"][0]["repository"] == "library/alpine"
    assert first["next_cursor"] == "1"
    second = peer["catalog"].search("alpine", limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["repository"] == "team/app" and second["next_cursor"] is None
    assert {urllib.parse.urlsplit(request.full_url).hostname for request in peer["requests"]} == {"hub.docker.com"}


def test_tag_filter_advances_unfiltered_server_page_without_losing_later_matches(peer):
    def handler(request):
        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.hostname == "auth.docker.io":
            return _Response(request.full_url, _bytes({"token": "registry-only"}))
        query = urllib.parse.parse_qs(parsed.query)
        tags = ["0.1", "0.2", "latest"] if "last" not in query else ["latest"]
        return _Response(request.full_url, _bytes({"name": "library/alpine", "tags": tags}))

    peer["handler"] = handler
    first = peer["catalog"].tags("library/alpine", query="latest", limit=2)
    assert first["items"] == [] and first["next_cursor"] == "0.2"
    second = peer["catalog"].tags("library/alpine", query="latest", limit=2, cursor=first["next_cursor"])
    assert second["next_cursor"] is None
    assert second["items"][0]["reference"] == "docker.io/library/alpine:latest"


def test_index_child_layers_and_download_reference_remain_immutable(peer, monkeypatch):
    config, manifest, index = _registry_peer(peer)
    monkeypatch.setattr(docker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(docker.platform, "machine", lambda: "arm64")
    summary = peer["catalog"].inspect("docker.io/library/alpine:latest")
    assert summary["digest"] == _digest(index) and summary["layers"] == []
    assert {item["architecture"] for item in summary["platforms"]} == {"amd64", "arm64"}
    assert summary["config"] is None and summary["manifest_digest"] is None
    detail = peer["catalog"].inspect(summary["immutable_reference"], manifest=_digest(manifest))
    assert detail["immutable_reference"] == summary["immutable_reference"]
    assert detail["download_reference"] == "docker.io/library/alpine@" + _digest(manifest)
    assert detail["config"]["digest"] == _digest(config)
    assert detail["manifest_digest"] == _digest(manifest)
    assert (detail["config"]["os"], detail["config"]["architecture"]) == ("linux", "amd64")
    assert {item["architecture"] for item in detail["platforms"]} == {"amd64", "arm64"}
    assert detail["layers"] == [
        {
            "ordinal": 0,
            "digest": _digest(b"compressed"),
            "diff_id": _digest(b"uncompressed"),
            "size_bytes": 123,
            "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
        }
    ]
    assert detail["total_size_bytes"] == 123 and detail["download_supported"] is True
    assert detail["run_supported"] is False
    assert "IMAGE_ENV" not in json.dumps(detail)


def test_foreign_child_and_corrupt_manifest_header_are_rejected(peer):
    _registry_peer(peer)
    with pytest.raises(StateError):
        peer["catalog"].inspect("docker.io/library/alpine:latest", manifest=_digest(b"foreign"))
    _registry_peer(peer, root_headers={"Docker-Content-Digest": _digest(b"wrong-root")})
    with pytest.raises(StateError):
        peer["catalog"].inspect("docker.io/library/alpine:latest")


def test_corrupt_config_is_not_projected_as_trusted_platform_or_layers(peer):
    _, manifest, _ = _registry_peer(peer, corrupt_config=True)
    with pytest.raises(StateError):
        peer["catalog"].inspect("docker.io/library/alpine:latest", manifest=_digest(manifest))


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_remote_status_is_visible_without_error_body_or_credentials(peer, status):
    headers = Message()
    headers["Retry-After"] = "7"
    headers["WWW-Authenticate"] = 'Bearer realm="https://untrusted.invalid/token"'

    def handler(request):
        raise urllib.error.HTTPError(
            request.full_url, status, "remote-error", headers, io.BytesIO(b"private-error-body")
        )

    peer["handler"] = handler
    with pytest.raises(StateError) as error:
        peer["catalog"].inspect("docker.io/library/alpine:latest")
    assert str(status) in str(error.value)
    assert "private-error-body" not in str(error.value)
    assert all(urllib.parse.urlsplit(request.full_url).hostname == "auth.docker.io" for request in peer["requests"])


def test_redirect_never_sends_selected_credentials_to_another_authority(peer):
    peer["directory"].mkdir()
    basic = base64.b64encode(b"selected-user:selected-secret").decode()
    (peer["directory"] / "config.json").write_text(json.dumps({"auths": {"docker.io": {"auth": basic}}}))
    headers = Message()
    headers["Location"] = "https://untrusted.invalid/collect"

    def handler(request):
        assert urllib.parse.urlsplit(request.full_url).hostname == "auth.docker.io"
        assert request.get_header("Authorization") == "Basic " + basic
        raise urllib.error.HTTPError(request.full_url, 307, "redirect", headers, io.BytesIO())

    peer["handler"] = handler
    with pytest.raises(StateError):
        peer["catalog"].inspect("docker.io/library/alpine:latest")
    assert len(peer["requests"]) == 1


def test_selected_docker_credentials_never_reuse_keystone_or_package_keys(peer, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_TOKEN", "foreign-keystone-secret")
    monkeypatch.setenv("PALIMPSEST_PACKAGE_KEY", "foreign-native-key")
    peer["directory"].mkdir()
    basic = base64.b64encode(b"selected-user:selected-secret").decode()
    (peer["directory"] / "config.json").write_text(json.dumps({"auths": {"docker.io": {"auth": basic}}}))
    registry_credential = "catalog-fixture-" + "credential"

    def handler(request):
        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.hostname == "auth.docker.io":
            assert request.get_header("Authorization") == "Basic " + basic
            return _Response(request.full_url, _bytes({"token": registry_credential}))
        assert request.get_header("Authorization") == "Bearer " + registry_credential
        assert "selected-secret" not in repr(request.headers)
        return _Response(request.full_url, _bytes({"name": "library/alpine", "tags": ["latest"]}))

    peer["handler"] = handler
    result = peer["catalog"].tags("library/alpine")
    assert result["items"][0]["tag"] == "latest"
    serialized = repr([(request.full_url, request.headers, request.data) for request in peer["requests"]])
    assert "foreign-keystone-secret" not in serialized and "foreign-native-key" not in serialized
    assert not list(peer["directory"].glob("docker-auth*"))


def test_invalid_selected_credential_configuration_does_not_fall_back_to_anonymous(peer):
    peer["directory"].mkdir()
    (peer["directory"] / "config.json").write_text("not-json")
    with pytest.raises(PalimpsestError):
        peer["catalog"].tags("library/alpine")
    assert peer["requests"] == []


def test_config_cdn_redirect_is_one_hop_credential_free_and_digest_verified(peer):
    config, manifest, _ = _registry_peer(peer)
    original = peer["handler"]
    cdn = "https://production.cloudfront.docker.com/config?signature=public-fixture"

    def handler(request):
        if request.full_url == cdn:
            assert request.get_header("Authorization") is None
            return _Response(request.full_url, config)
        if "/blobs/" in request.full_url:
            headers = Message()
            headers["Location"] = cdn
            raise urllib.error.HTTPError(request.full_url, 307, "redirect", headers, io.BytesIO())
        return original(request)

    peer["handler"] = handler
    result = peer["catalog"].inspect("docker.io/library/alpine:latest", manifest=_digest(manifest))
    assert result["config"]["digest"] == _digest(config)
    assert result["layers"][0]["digest"] == _digest(b"compressed")
    assert "signature" not in json.dumps(result)


@pytest.mark.parametrize(
    "location",
    [
        "http://production.cloudfront.docker.com/config",
        "https://127.0.0.1/config",
        "https://untrusted.invalid/config",
        "https://user:secret@production.cloudfront.docker.com/config",
        "https://production.cloudfront.docker.com:8443/config",
    ],
)
def test_config_redirect_never_reaches_foreign_or_credential_bearing_targets(peer, location):
    _, manifest, _ = _registry_peer(peer)
    original = peer["handler"]

    def handler(request):
        if "/blobs/" in request.full_url:
            headers = Message()
            headers["Location"] = location
            raise urllib.error.HTTPError(request.full_url, 307, "redirect", headers, io.BytesIO())
        return original(request)

    peer["handler"] = handler
    with pytest.raises(StateError):
        peer["catalog"].inspect("docker.io/library/alpine:latest", manifest=_digest(manifest))
    assert {urllib.parse.urlsplit(request.full_url).hostname for request in peer["requests"]} <= {
        "auth.docker.io",
        "registry-1.docker.io",
    }
