"""Hub publication/pull client behavior against an in-process stdlib fake Hub.

No real Hub, Keystone or VM is contacted. The fake implements only the route
shapes, visibility rules and error contract the client depends on (M2 client
contract, round 2 source closure).
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shlex
import sqlite3
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from palimpsest_local import cli, hub_publish, inventory, state
from palimpsest_local.artifact_store import ArtifactStore
from palimpsest_local.errors import HubError, StateError
from palimpsest_local.hub import HUB_API_PREFIX, HubClient
from palimpsest_local.image_manifest import (
    SCHEMA,
    layer_identity,
    manifest_digest,
    native_image_projection,
    normalize_manifest,
    public_manifest,
)
from palimpsest_local.local_catalog import Catalog, CatalogError, canonical_hub_url, open_catalog
from palimpsest_local.native_root_source import qualify_native_root_source
from palimpsest_local.oci_image import OCIImageRef
from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS
from palimpsest_local.state import StatePaths


def _key(character: str) -> str:
    return "ppi_v1_" + character * 32 + "." + "S" * 43


KEY, KEY2, KEY3, PUBLISH_ONLY_KEY = _key("a"), _key("b"), _key("c"), _key("d")
TOKEN = "keystone-token-secret-value"
# Credential -> (user, project). KEY3 is another user in KEY's project.
IDENTITIES = {
    "Bearer " + KEY: ("user-1", "project-1"),
    "Bearer " + KEY2: ("user-2", "project-2"),
    "Bearer " + KEY3: ("user-3", "project-1"),
    "Bearer " + PUBLISH_ONLY_KEY: ("user-1", "project-1"),
    TOKEN: ("user-1", "project-1"),
}
# Keys holding images:publish without images:read: every image read is 403.
NO_READ = {"Bearer " + PUBLISH_ONLY_KEY}
# A publish-only key of another user in project-1 (replay of user-1's request id conflicts).
PUBLISH_ONLY_KEY3 = _key("e")
IDENTITIES["Bearer " + PUBLISH_ONLY_KEY3] = ("user-3", "project-1")
NO_READ.add("Bearer " + PUBLISH_ONLY_KEY3)
# A publish-only key of project-2.
PUBLISH_ONLY_KEY2 = _key("f")
IDENTITIES["Bearer " + PUBLISH_ONLY_KEY2] = ("user-2", "project-2")
NO_READ.add("Bearer " + PUBLISH_ONLY_KEY2)
# Keystone-token style credentials (no public key id): another user of project-1, and a token that cannot read.
TOKEN3, TOKEN_NO_READ = "keystone-token-user-three", "keystone-token-without-read"
IDENTITIES[TOKEN3] = ("user-3", "project-1")
IDENTITIES[TOKEN_NO_READ] = ("user-1", "project-1")
NO_READ.add("Bearer " + TOKEN_NO_READ)
_INDEX = "application/vnd.oci.image.index.v1+json"
_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_CONFIG = "application/vnd.oci.image.config.v1+json"
_LAYER = "application/vnd.oci.image.layer.v1.tar"


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_bytes(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _tar(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(name)
        entry.size = len(payload)
        archive.addfile(entry, io.BytesIO(payload))
    return buffer.getvalue()


# Ordinal 0 is the original base; ordinal 2 repeats ordinal 0's exact bytes.
OCI_LAYERS = (_tar("etc/os-release", b'ID=ubuntu\nVERSION_ID="24.04"\n'), _tar("app/run", b"#!/bin/true\n"))
OCI_LAYERS = (*OCI_LAYERS, OCI_LAYERS[0])


def _blob(layout: Path, media_type: str, payload: bytes) -> dict:
    (layout / "blobs" / "sha256" / _sha(payload)[7:]).write_bytes(payload)
    return {"mediaType": media_type, "digest": _sha(payload), "size": len(payload)}


def _oci_layout(path: Path, *, architecture="arm64", env="APP_SECRET=config-env-value", with_index=True) -> dict:
    """A standard OCI layout like a local build output (same shape as test_packages helpers)."""
    (path / "blobs" / "sha256").mkdir(parents=True)
    (path / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
    layers = [_blob(path, _LAYER, payload) for payload in OCI_LAYERS]
    config = {
        "architecture": architecture,
        "os": "linux",
        "config": {
            "Entrypoint": ["/app/run"],
            "Cmd": ["--serve"],
            "Env": ["PATH=/usr/bin", env],
            "WorkingDir": "/srv",
            "User": "1000",
        },
        "rootfs": {"type": "layers", "diff_ids": [_sha(payload) for payload in OCI_LAYERS]},
    }
    root = _blob(
        path,
        _MANIFEST,
        _json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": _MANIFEST,
                "config": _blob(path, _CONFIG, _json_bytes(config)),
                "layers": layers,
            }
        ),
    )
    if with_index:
        entry = {**root, "platform": {"architecture": architecture, "os": "linux"}}
        root = _blob(path, _INDEX, _json_bytes({"schemaVersion": 2, "mediaType": _INDEX, "manifests": [entry]}))
    (path / "index.json").write_bytes(_json_bytes({"schemaVersion": 2, "mediaType": _INDEX, "manifests": [root]}))
    return root


def _squashfs(ordinal_payload: bytes) -> bytes:
    """Deterministic block-aligned stand-in for one layer's SquashFS conversion."""
    return (b"hsqs" + _sha(ordinal_payload).encode()).ljust(4096, b"\0")


PAYLOADS = tuple(_squashfs(payload) for payload in OCI_LAYERS)


def _roots(base: Path, name: str) -> StatePaths:
    selected = base.resolve() / name
    selected.mkdir(mode=0o700)
    return StatePaths(config=base.resolve() / "config", state=selected)


def _local_image(roots: StatePaths, *, layout: Path | None = None, **options) -> dict:
    """Acquire a real local OCI layout through the existing intake, then record conversions.

    Receipts carry this state root's own store/occurrence identities, as real
    conversions would; only the SquashFS bytes are synthetic.
    """
    layout = layout or roots.state.parent / f"{roots.state.name}-source"
    if not layout.exists():
        _oci_layout(layout, **options)
    (roots.state / "runtime-packs").mkdir(mode=0o700, exist_ok=True)
    platform = f"linux/{options.get('architecture', 'arm64')}"
    snapshot = LocalLayoutSource(layout=layout).snapshot_acquisition(
        None, SourceCAS(roots.oci_source_cas), platform=platform
    )
    registered = inventory.record_oci_source(roots, snapshot)
    store = ArtifactStore(roots.store)
    for payload in PAYLOADS:
        store.publish_blob([payload], expected_digest=_sha(payload))
    local = hashlib.sha256(str(roots.state).encode()).hexdigest()
    image = {
        key: registered[key]
        for key in (
            "image_id",
            "reference",
            "root_descriptor",
            "manifest_descriptor",
            "config_descriptor",
            "platform",
            "layers",
            "source_binding_digest",
            "source_cas_id",
            "source_kind",
        )
    }
    image["derived"] = [
        {
            "store_id": "oci-store-v1:" + local,
            "occurrence_digest": _sha(f"{local}:{index}".encode()),
            "record_digest": _sha(b"record" + payload),
            "key_digest": _sha(b"key" + payload),
            "source_snapshot_binding_digest": image["source_binding_digest"],
            "source_image_digest": image["image_id"],
            "ordinal": index,
            "image_digest": _sha(payload),
            "image_size": len(payload),
            "filesystem": "squashfs",
        }
        for index, payload in enumerate(PAYLOADS)
    ]
    image["materialization_complete"] = True
    with open_catalog(roots) as catalog:
        catalog.register_image(image)
        return next(item for item in catalog.read_images() if item["image_id"] == image["image_id"])


class FakeHub:
    """Published-image routes with Hub visibility, request_id and closure rules."""

    def __init__(self):
        self.staged: dict[str, bytes] = {}
        self.staged_by: dict[tuple[str, str], set[str]] = {}
        self.images: dict[str, dict] = {}
        self.by_request: dict[tuple[str, str], str] = {}
        self.requests: list[dict] = []
        self.posts: list[dict] = []
        self.failure: tuple[int, object] | None = None
        self.fail_after_commit = False
        self.delay_post = 0.0
        self.mismatch = False
        self.corrupt: dict[str, bytes] = {}
        self.short_body: set[str] = set()
        self.redirect_gets = False
        self.on_first_request = None
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self):
                hub.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})
                if hub.on_first_request is not None:
                    callback, hub.on_first_request = hub.on_first_request, None
                    callback()

            def _json(self, status, value, headers=()):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, header in headers:
                    self.send_header(name, header)
                self.end_headers()
                self.wfile.write(body)

            def _actor(self):
                credential = self.headers.get("Authorization") or self.headers.get("X-Auth-Token")
                return IDENTITIES.get(credential) if credential else None

            def _readable(self, image, actor):
                if image["visibility"] == "public":
                    return True
                if actor is None:
                    return False
                if image["visibility"] == "internal":
                    return actor[1] == image["project_id"]
                return actor[0] == image["created_by"]

            def _projection(self, image, actor):
                value = {key: item for key, item in image.items() if key not in ("created_by", "project_id")}
                if actor is not None:
                    value["project_id"] = image["project_id"]
                    if actor[0] == image["created_by"]:
                        value["created_by"] = image["created_by"]
                return copy.deepcopy(value)

            def _routed(self):
                """Only the published-image routes are recorded; stray local traffic is ignored."""
                if self.path.split("?", 1)[0].startswith(f"{HUB_API_PREFIX}/published-images"):
                    return True
                self._json(404, {"detail": "not found"})
                return False

            def do_PUT(self):
                if not self._routed():
                    return None
                self._record()
                body = self.rfile.read(int(self.headers["Content-Length"]))
                digest = self.path.rsplit("/", 1)[1]
                if _sha(body) != digest or self.headers["X-Content-SHA256"] != digest:
                    return self._json(400, {"detail": "digest mismatch"})
                hub.staged[digest] = body
                actor = self._actor()
                if actor is not None:
                    hub.staged_by.setdefault(actor, set()).add(digest)
                return self._json(200, {"digest": digest, "size_bytes": len(body)})

            def do_POST(self):
                if not self._routed():
                    return None
                self._record()
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                hub.posts.append(payload)
                actor = self._actor()
                if actor is None:
                    return self._json(401, {"detail": "authentication required"})
                if hub.failure is not None:
                    status, detail = hub.failure
                    return self._json(status, {"detail": detail})
                manifest = normalize_manifest(payload["manifest"])
                if self.headers.get("Authorization") in NO_READ:
                    # The real Hub makes a publish-only key stage every manifest layer itself, BEFORE
                    # it looks up the request id.
                    if not {layer["digest"] for layer in manifest["layers"]} <= hub.staged_by.get(actor, set()):
                        return self._json(404, {"detail": "publication payload not found"})
                key = (actor[1], payload["request_id"])
                image_id = hub.by_request.get(key)
                if image_id not in hub.images:
                    image_id = None  # a deleted image no longer answers its request id
                if image_id is not None:
                    existing = hub.images[image_id]
                    if (
                        existing["created_by"] != actor[0]
                        or existing["manifest_digest"] != manifest_digest(manifest)
                        or existing["name"] != payload["name"]
                        or existing["visibility"] != payload["visibility"]
                    ):
                        return self._json(409, {"detail": "publication request_id conflicts"})
                else:
                    closure = payload.get("source_closure") or []
                    needed = {layer["digest"] for layer in manifest["layers"]} | {item["digest"] for item in closure}
                    if any(digest not in hub.staged for digest in needed):
                        return self._json(404, {"detail": "publication payload not found"})
                    image_id = str(uuid.uuid4())
                    hub.by_request[key] = image_id
                    hub.images[image_id] = {
                        "id": image_id,
                        "name": payload["name"],
                        "manifest_digest": manifest_digest(manifest),
                        "visibility": payload["visibility"],
                        "manifest": public_manifest(manifest),
                        "leaf_id": manifest["layers"][-1]["id"],
                        "source_closure": closure,
                        "project_id": actor[1],
                        "created_by": actor[0],
                        "created_at": "2026-11-07T00:00:00+00:00",
                    }
                if hub.fail_after_commit:
                    return self._json(500, {"detail": "committed then failed"})
                if hub.delay_post:
                    time.sleep(hub.delay_post)
                value = self._projection(hub.images[image_id], actor)
                if hub.mismatch:
                    value["manifest_digest"] = _sha(b"other")
                return self._json(201, value)

            def do_GET(self):
                if not self._routed():
                    return None
                self._record()
                if hub.redirect_gets:
                    self.send_response(307)
                    self.send_header("Location", "https://elsewhere.invalid/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                presented = self.headers.get("Authorization") or "Bearer " + (self.headers.get("X-Auth-Token") or "")
                if presented in NO_READ:
                    return self._json(403, {"detail": "image key lacks images:read"})
                actor = self._actor()
                parts = self.path.split("/")
                image = hub.images.get(parts[3]) if len(parts) > 3 else None
                if image is None or not self._readable(image, actor):
                    return self._json(404, {"detail": "published image not found"})
                if len(parts) == 4:
                    return self._json(200, self._projection(image, actor))
                digest = parts[5]
                members = {layer["digest"] for layer in image["manifest"]["layers"]}
                members |= {item["digest"] for item in image["source_closure"]}
                if digest not in members:
                    return self._json(404, {"detail": "publication payload not found"})
                body = hub.corrupt.get(digest, hub.staged.get(digest))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2] if digest in hub.short_body else body)
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def fake_hub():
    hub = FakeHub()
    yield hub
    hub.close()


def _client(hub, key=KEY, **options):
    return HubClient(hub.url, key, credential="image-key", **options)


def _publish(roots, image, hub, *, client=None, name="app", visibility="private", **options):
    return hub_publish.publish_local_image(
        roots,
        image["image_id"],
        client=client or _client(hub),
        hub_url=hub.url,
        name=name,
        visibility=visibility,
        os_family="ubuntu",
        os_version="24.04",
        confirm_public=visibility == "public",
        **options,
    )


def _anonymous(hub):
    return HubClient(hub.url, None, credential="anonymous")


def _pull(roots, hub_image_id, hub, **options):
    return hub_publish.pull_published_image(
        roots, hub_image_id, client=options.pop("client", _anonymous(hub)), hub_url=hub.url, **options
    )


def _publications(roots):
    with open_catalog(roots, create=False) as catalog:
        return catalog.read_publications()


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


# -- manifest -----------------------------------------------------------------


def test_manifest_from_local_image_preserves_order_parent_chain_and_original_base(tmp_path):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    manifest, blobs = hub_publish.local_image_manifest(roots, image, os_family="ubuntu", os_version="24.04")
    assert manifest["mode"] == "oci-root" and manifest["platform"] == "linux/arm64"
    assert manifest["base_image_digest"] is None
    assert [(layer["digest"], layer["size_bytes"]) for layer in manifest["layers"]] == [
        (_sha(item), len(item)) for item in PAYLOADS
    ]
    compatibility = {key: manifest[key] for key in ("mode", "platform", "os_family", "os_version", "base_image_digest")}
    parent = None
    for layer in manifest["layers"]:
        assert layer["parent_id"] == parent
        assert layer["id"] == layer_identity(parent, layer["digest"], layer["size_bytes"], compatibility)
        parent = layer["id"]
    # The repeated payload is a distinct logical occurrence but one shared upload.
    assert manifest["layers"][0]["id"] != manifest["layers"][2]["id"]
    assert set(blobs) == {_sha(PAYLOADS[0]), _sha(PAYLOADS[1])}
    # The original base layer keeps its compressed descriptor and DiffID.
    assert manifest["layers"][0]["source"]["oci_layer"] == image["layers"][0]
    assert manifest["source"]["diff_ids"] == [layer["diff_id"] for layer in image["layers"]]
    assert manifest["source"]["root_digest"] == image["root_descriptor"]["digest"]
    assert manifest["source"]["config_digest"] == image["config_descriptor"]["digest"]
    assert manifest["runtime"] == {
        "entrypoint": ["/app/run"],
        "cmd": ["--serve"],
        "env": ["PATH=/usr/bin", "APP_SECRET=config-env-value"],
        "working_dir": "/srv",
        "user": "1000",
    }


def test_same_image_from_two_state_roots_has_the_same_manifest_without_machine_identities(tmp_path):
    first, second = _roots(tmp_path, "one"), _roots(tmp_path, "two")
    layout = tmp_path.resolve() / "shared-source"
    _oci_layout(layout)
    images = [_local_image(roots, layout=layout) for roots in (first, second)]
    assert images[0]["derived"][0]["store_id"] != images[1]["derived"][0]["store_id"]
    assert images[0]["source_binding_digest"] != images[1]["source_binding_digest"]
    manifests = [
        hub_publish.local_image_manifest(roots, image, os_family="ubuntu", os_version="24.04")[0]
        for roots, image in zip((first, second), images, strict=True)
    ]
    assert manifest_digest(manifests[0]) == manifest_digest(manifests[1])
    rendered = json.dumps(manifests[0])
    for value in (
        images[0]["derived"][0]["store_id"],
        images[0]["derived"][0]["occurrence_digest"],
        images[0]["source_binding_digest"],
        images[0]["source_cas_id"],
    ):
        assert value not in rendered
    receipt = manifests[0]["layers"][1]["source"]["conversion"]["receipt"]
    assert set(receipt) == {
        "key_digest",
        "record_digest",
        "image_digest",
        "image_size",
        "filesystem",
        "ordinal",
        "source_image_digest",
    }


def test_incomplete_or_corrupt_local_image_is_never_published(tmp_path):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    with pytest.raises(hub_publish.PublicationError, match="completely materialized"):
        hub_publish.local_image_manifest(
            roots, {**image, "materialization_status": "partial"}, os_family="ubuntu", os_version="24.04"
        )
    blob = roots.store / "blobs" / "sha256" / _sha(PAYLOADS[1])[7:]
    blob.chmod(0o600)
    blob.write_bytes(b"x" * len(PAYLOADS[1]))
    with pytest.raises(hub_publish.PublicationError, match="missing or corrupt"):
        hub_publish.local_image_manifest(roots, image, os_family="ubuntu", os_version="24.04")


# -- publication --------------------------------------------------------------


def test_publish_persists_request_id_first_uploads_closure_and_records_owner(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    observed, notices = [], []
    fake_hub.on_first_request = lambda: observed.extend(_publications(roots))
    result = _publish(roots, image, fake_hub, notify=notices.append)
    assert [row["status"] for row in observed] == ["pending"]
    assert result["status"] == "published"
    assert result["request_id"] == observed[0]["request_id"] == fake_hub.posts[0]["request_id"]
    assert (result["hub_project_id"], result["hub_created_by"]) == ("project-1", "user-1")
    manifest, _ = hub_publish.local_image_manifest(roots, image, os_family="ubuntu", os_version="24.04")
    assert result["manifest_digest"] == manifest_digest(manifest)
    # The closure: root index, manifest, config and every compressed layer in order.
    closure = fake_hub.posts[0]["source_closure"]
    assert [(item["role"], item["ordinal"]) for item in closure] == [
        ("root", 0),
        ("manifest", 0),
        ("config", 0),
        ("layer", 0),
        ("layer", 1),
        ("layer", 2),
    ]
    assert [item["digest"] for item in closure] == [
        image["root_descriptor"]["digest"],
        image["manifest_descriptor"]["digest"],
        image["config_descriptor"]["digest"],
        *[layer["compressed"]["digest"] for layer in image["layers"]],
    ]
    assert all(item["digest"] in fake_hub.staged for item in closure)
    assert result["source_mode"] == "closure" and result["source_closure"] == closure
    assert (
        len(notices) == 1 and "environment variable VALUES" in notices[0] and "you (the publishing user)" in notices[0]
    )
    assert {request["headers"].get("Authorization") for request in fake_hub.requests} == {"Bearer " + KEY}


def test_published_row_is_revalidated_with_the_current_credential(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    first = _publish(roots, image, fake_hub)
    fake_hub.requests.clear()
    assert _publish(roots, image, fake_hub) == first
    assert [request["method"] for request in fake_hub.requests] == ["GET"]
    # A different key in another project cannot see the private image: new intent.
    second = _publish(roots, image, fake_hub, client=_client(fake_hub, KEY2))
    assert second["status"] == "published" and second["request_id"] != first["request_id"]
    assert (second["generation"], second["hub_project_id"]) == (1, "project-2")
    assert second["hub_image_id"] != first["hub_image_id"]
    # A deleted Hub image is never trusted: the next run publishes again.
    del fake_hub.images[second["hub_image_id"]]
    third = _publish(roots, image, fake_hub, client=_client(fake_hub, KEY2))
    assert third["generation"] == 2 and third["hub_image_id"] in fake_hub.images
    assert [row["generation"] for row in _publications(roots)] == [0, 1, 2]


def test_publish_only_key_revalidates_by_replaying_the_stored_request_id(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    client = _client(fake_hub, PUBLISH_ONLY_KEY)
    first = _publish(roots, image, fake_hub, client=client)
    assert first["status"] == "published" and len(fake_hub.posts) == 1
    fake_hub.requests.clear()
    assert _publish(roots, image, fake_hub, client=client) == first
    # The read is forbidden, so the stored request id is replayed: same image, nothing new.
    assert [request["method"] for request in fake_hub.requests] == ["GET", "POST"]
    assert fake_hub.posts[1]["request_id"] == first["request_id"]
    assert len(fake_hub.images) == 1
    assert [(row["generation"], row["status"]) for row in _publications(roots)] == [(0, "published")]


def _posted_ids(hub):
    return [post["request_id"] for post in hub.posts]


def test_publish_only_key_never_replays_or_adopts_another_credentials_row(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    first = _publish(roots, image, fake_hub)  # KEY: user-1, project-1, can read
    same_project = _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY3))  # user-3, project-1
    other_project = _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY2))  # project-2
    # Each key registered its own image under its own request id; nobody replayed another credential's id.
    assert same_project["hub_created_by"] == "user-3" and other_project["hub_project_id"] == "project-2"
    ids = {first["request_id"], same_project["request_id"], other_project["request_id"]}
    assert len(ids) == 3 and set(_posted_ids(fake_hub)) == ids and len(fake_hub.posts) == 3
    assert same_project["hub_request_id"] is None and other_project["hub_request_id"] is None
    assert [(row["generation"], row["status"], row["terminal"]) for row in _publications(roots)] == [
        (0, "published", False),
        (1, "published", False),
        (2, "published", False),
    ]
    assert _images_per_project(fake_hub, "app") == {"project-1": 2, "project-2": 1}


def test_alternating_credentials_create_one_hub_image_per_project(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    sequence = [
        _publish(roots, image, fake_hub, client=_client(fake_hub, key))["hub_image_id"]
        for key in (KEY, KEY2, KEY, KEY2)
    ]
    assert sequence[0] == sequence[2] and sequence[1] == sequence[3] and sequence[0] != sequence[1]
    per_project = {}
    for item in fake_hub.images.values():
        per_project[item["project_id"]] = per_project.get(item["project_id"], 0) + 1
    assert per_project == {"project-1": 1, "project-2": 1} and len(fake_hub.posts) == 2
    assert [(row["generation"], row["hub_project_id"]) for row in _publications(roots)] == [
        (0, "project-1"),
        (1, "project-2"),
    ]


def _images_per_project(hub, name: str | None = None) -> dict:
    counts: dict = {}
    for item in hub.images.values():
        if name is not None and item["name"] != name:
            continue
        counts[item["project_id"]] = counts.get(item["project_id"], 0) + 1
    return counts


def test_publish_only_key_after_hub_deletion_rebinds_the_row_without_another_post(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    client = _client(fake_hub, PUBLISH_ONLY_KEY)
    first = _publish(roots, image, fake_hub, client=client)
    assert first["hub_credential"] == "ppi_v1_" + "d" * 32
    del fake_hub.images[first["hub_image_id"]]
    second = _publish(roots, image, fake_hub, client=client)
    # The replay re-registered the image under the same (project, request id): that IS the publication.
    assert len(fake_hub.posts) == 2 and fake_hub.posts[1]["request_id"] == first["request_id"]
    assert second["hub_image_id"] != first["hub_image_id"] and second["hub_image_id"] in fake_hub.images
    assert (second["request_id"], second["generation"], second["status"]) == (first["request_id"], 0, "published")
    assert _images_per_project(fake_hub) == {"project-1": 1}
    assert [(row["generation"], row["hub_image_id"]) for row in _publications(roots)] == [(0, second["hub_image_id"])]
    # Idempotent afterwards: one replay POST, no new row, no new image.
    assert _publish(roots, image, fake_hub, client=client) == second
    assert len(fake_hub.posts) == 3 and _images_per_project(fake_hub) == {"project-1": 1}


def test_publish_only_keys_of_two_projects_each_replay_only_their_own_row(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    first = _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY))
    other = _client(fake_hub, PUBLISH_ONLY_KEY2)
    second = _publish(roots, image, fake_hub, client=other)
    assert second["hub_project_id"] == "project-2" and second["request_id"] != first["request_id"]
    # Re-runs are idempotent: each key replays the request id of the row it recorded itself.
    assert _publish(roots, image, fake_hub, client=other) == second
    assert _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY)) == first
    assert _posted_ids(fake_hub) == [
        first["request_id"],
        second["request_id"],
        second["request_id"],
        first["request_id"],
    ]
    assert _images_per_project(fake_hub) == {"project-1": 1, "project-2": 1}
    assert [(row["generation"], row["hub_project_id"], row["status"]) for row in _publications(roots)] == [
        (0, "project-1", "published"),
        (1, "project-2", "published"),
    ]


def test_publish_only_key_tracks_only_its_own_publications(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    _publish(roots, image, fake_hub, client=_client(fake_hub, KEY))
    _publish(roots, image, fake_hub, client=_client(fake_hub, KEY2))
    posts = len(fake_hub.posts)
    # A key that cannot read cannot tell whether those rows are its own images: it never replays them.
    own = _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY))
    assert own["generation"] == 2 and len(fake_hub.posts) == posts + 1
    assert _images_per_project(fake_hub) == {"project-1": 2, "project-2": 1}
    assert _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY)) == own
    assert _images_per_project(fake_hub) == {"project-1": 2, "project-2": 1}


def test_a_credential_without_a_key_id_that_cannot_read_refuses_instead_of_guessing(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY))
    before = _publications(roots)
    token = HubClient(fake_hub.url, TOKEN_NO_READ)
    with pytest.raises(hub_publish.PublicationError, match="images:read"):
        _publish(roots, image, fake_hub, client=token)
    assert _publications(roots) == before and _images_per_project(fake_hub) == {"project-1": 1}


def test_another_credentials_unresolved_attempt_neither_blocks_nor_diverts_the_owner(tmp_path, fake_hub):
    """An uncertain or failed attempt of key B must not make key A push B's request id or skip its own replay."""
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    first = _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY))  # project-1
    fake_hub.fail_after_commit = True
    with pytest.raises(hub_publish.PublicationError, match="recorded as unknown"):
        _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY2))  # project-2, committed
    fake_hub.fail_after_commit = False
    fake_hub.failure = (403, {"code": "image_count_limit"})
    with pytest.raises(hub_publish.PublicationError, match="count limit"):
        _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY3))  # project-1, refused
    fake_hub.failure = None
    posts = len(fake_hub.posts)
    # The owner re-runs: its own row is replayed (one POST with its own id), nothing new is registered.
    assert _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY)) == first
    assert _posted_ids(fake_hub)[posts:] == [first["request_id"]]
    assert _images_per_project(fake_hub) == {"project-1": 1, "project-2": 1}
    # Key B's uncertain attempt is retried with ITS OWN request id by B alone: the Hub answers the commit.
    retried = _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY2))
    assert retried["status"] == "published" and retried["hub_project_id"] == "project-2"
    assert retried["generation"] == 1 and _images_per_project(fake_hub) == {"project-1": 1, "project-2": 1}
    assert _posted_ids(fake_hub)[-1] == retried["request_id"]


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        (429, "slow down", "refused the replay"),
        (403, {"code": "action_denied"}, "refused the replay"),
        (503, "unavailable", "outcome is unknown"),
    ],
)
def test_a_failed_replay_never_starts_a_new_generation_or_image(tmp_path, fake_hub, status, detail, message):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    client = _client(fake_hub, PUBLISH_ONLY_KEY)
    first = _publish(roots, image, fake_hub, client=client)
    fake_hub.failure = (status, detail)
    with pytest.raises(hub_publish.PublicationError, match=message) as raised:
        _publish(roots, image, fake_hub, client=client)
    assert "nothing changed" not in str(raised.value)
    fake_hub.failure = None
    assert _images_per_project(fake_hub) == {"project-1": 1} and len(_publications(roots)) == 1
    assert _publish(roots, image, fake_hub, client=client) == first
    assert _images_per_project(fake_hub) == {"project-1": 1} and len(_publications(roots)) == 1


def test_an_unconfirmed_replay_resends_the_stored_request_id_instead_of_registering_a_duplicate(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    client = _client(fake_hub, PUBLISH_ONLY_KEY)
    first = _publish(roots, image, fake_hub, client=client)
    fake_hub.staged_by.clear()  # the key's staged payloads are gone: the replay answers 404 before any lookup
    second = _publish(roots, image, fake_hub, client=client)
    # The new generation re-staged the payloads and re-sent the SAME Hub request id: the Hub answered its image.
    assert second["generation"] == 1 and second["hub_request_id"] == first["request_id"]
    assert second["hub_image_id"] == first["hub_image_id"] and _images_per_project(fake_hub) == {"project-1": 1}
    assert set(_posted_ids(fake_hub)) == {first["request_id"]}


def test_concurrent_reruns_of_one_publish_only_key_register_nothing_new(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    first = _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY))
    results, errors = [], []

    def run():
        try:
            results.append(_publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY)))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            errors.append(repr(exc))

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and results == [first] * 3
    assert _images_per_project(fake_hub) == {"project-1": 1} and len(_publications(roots)) == 1


def test_a_mismatching_replay_response_is_not_adopted(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    client = _client(fake_hub, PUBLISH_ONLY_KEY)
    first = _publish(roots, image, fake_hub, client=client)
    fake_hub.mismatch = True
    with pytest.raises(hub_publish.PublicationError, match="does not match"):
        _publish(roots, image, fake_hub, client=client)
    rows = _publications(roots)
    assert rows[0] == {**first, "updated_at": rows[0]["updated_at"]}
    assert [(row["generation"], row["status"], row["error_code"]) for row in rows] == [
        (0, "published", None),
        (1, "unknown", "response_mismatch"),
    ]


def test_rebind_keeps_the_intent_key_and_stores_only_the_public_key_id(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    first = _publish(roots, image, fake_hub, client=_client(fake_hub, PUBLISH_ONLY_KEY))
    replacement = str(uuid.uuid4())
    with open_catalog(roots, create=False) as catalog:
        with pytest.raises(CatalogError, match="another project"):
            catalog.rebind_publication(
                first["request_id"], hub_image_id=replacement, hub_project_id="project-2", hub_created_by=None, now="t"
            )
        with pytest.raises(CatalogError, match="public image-key id"):
            catalog.rebind_publication(
                first["request_id"],
                hub_image_id=replacement,
                hub_project_id="project-1",
                hub_created_by=None,
                hub_credential=PUBLISH_ONLY_KEY,
                now="t",
            )
        rebound = catalog.rebind_publication(
            first["request_id"], hub_image_id=replacement, hub_project_id="project-1", hub_created_by=None, now="t"
        )
    assert {key: rebound[key] for key in ("generation", "status", "hub_project_id", "hub_credential")} == {
        "generation": 0,
        "status": "published",
        "hub_project_id": "project-1",
        "hub_credential": first["hub_credential"],
    }
    assert [row["hub_image_id"] for row in _publications(roots)] == [replacement]
    secret = PUBLISH_ONLY_KEY.split(".", 1)[1].encode()
    assert not any(secret in path.read_bytes() for path in roots.state.rglob("*") if path.is_file())


def test_concurrent_pushes_after_a_stale_revalidation_create_one_generation(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    first = _publish(roots, image, fake_hub)
    barrier = threading.Barrier(2, timeout=10)
    handler = fake_hub.server.RequestHandlerClass
    original = handler.do_GET

    def gated(self):
        if len(self.path.split("/")) == 4:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return original(self)

    handler.do_GET = gated
    results, errors = [], []

    def run():
        try:
            results.append(_publish(roots, image, fake_hub, client=_client(fake_hub, KEY2)))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            errors.append(repr(exc))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    handler.do_GET = original
    assert errors == [] and len(results) == 2
    assert {item["request_id"] for item in results} != {first["request_id"]}
    assert len({item["request_id"] for item in results}) == 1
    assert [(row["generation"], row["status"]) for row in _publications(roots)] == [(0, "published"), (1, "published")]
    assert len([item for item in fake_hub.images.values() if item["project_id"] == "project-2"]) == 1
    assert len({post["request_id"] for post in fake_hub.posts}) == 2


def test_local_bytes_changed_after_hashing_is_a_local_integrity_error(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    client = _client(fake_hub)
    real_push = client.push_image
    target = roots.oci_source_cas / "blobs" / "sha256" / image["layers"][1]["compressed"]["digest"][7:]

    def tampering_push(*args, **kwargs):
        size = target.stat().st_size
        target.chmod(0o600)
        target.write_bytes(b"Z" * size)
        target.chmod(0o400)
        return real_push(*args, **kwargs)

    client.push_image = tampering_push
    with pytest.raises(hub_publish.PublicationError, match="local integrity error") as raised:
        _publish(roots, image, fake_hub, client=client)
    assert "nothing was registered on the Hub" in str(raised.value) and "uncertain" not in str(raised.value)
    [row] = _publications(roots)
    assert (row["status"], row["error_code"]) == ("failed", "local_integrity")
    assert fake_hub.posts == [] and not any(body == b"Z" * len(body) for body in fake_hub.staged.values())


def test_unknown_row_retried_by_another_user_conflicts_terminally_then_gets_a_new_request_id(tmp_path, fake_hub):
    """Credentials without a key id share their unresolved attempts; another user's retry ends the request id."""
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    user_one = HubClient(fake_hub.url, TOKEN)
    user_three = HubClient(fake_hub.url, TOKEN3)
    fake_hub.fail_after_commit = True
    with pytest.raises(hub_publish.PublicationError, match="recorded as unknown"):
        _publish(roots, image, fake_hub, client=user_one)
    fake_hub.fail_after_commit = False
    [row] = _publications(roots)
    with pytest.raises(hub_publish.PublicationError, match="request id already belongs") as raised:
        _publish(roots, image, fake_hub, client=user_three)
    assert "next attempt uses a new request id" in str(raised.value)
    [conflicted] = _publications(roots)
    assert conflicted["request_id"] == row["request_id"]
    assert (conflicted["status"], conflicted["error_code"], conflicted["terminal"]) == (
        "failed",
        "http_409:request_id_conflict",
        True,
    )
    result = _publish(roots, image, fake_hub, client=user_three)
    assert result["status"] == "published" and result["generation"] == 1
    assert result["request_id"] != row["request_id"] and result["hub_created_by"] == "user-3"


@pytest.mark.parametrize(
    ("owner_key", "other_key", "other_user", "other_project"),
    [
        (KEY, KEY3, "user-3", "project-1"),
        (PUBLISH_ONLY_KEY, PUBLISH_ONLY_KEY3, "user-3", "project-1"),
        (PUBLISH_ONLY_KEY, PUBLISH_ONLY_KEY2, "user-2", "project-2"),
    ],
)
def test_an_image_key_retries_only_its_own_unresolved_attempt_even_after_others_publish(
    tmp_path, fake_hub, owner_key, other_key, other_user, other_project
):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    owner = _client(fake_hub, owner_key)
    fake_hub.fail_after_commit = True
    with pytest.raises(hub_publish.PublicationError, match="recorded as unknown"):
        _publish(roots, image, fake_hub, client=owner)
    fake_hub.fail_after_commit = False
    [row] = _publications(roots)
    result = _publish(roots, image, fake_hub, client=_client(fake_hub, other_key))
    # No request-id conflict and no adoption: the other key's uncertain row is not its to retry.
    assert result["generation"] == 1 and result["request_id"] != row["request_id"]
    assert result["hub_created_by"] == other_user and result["hub_project_id"] == other_project
    assert _publications(roots)[0]["status"] == "unknown"
    # The other key's published row is newer, yet the owner still retries ITS OWN committed request id.
    retried = _publish(roots, image, fake_hub, client=owner)
    assert (retried["request_id"], retried["generation"], retried["status"]) == (row["request_id"], 0, "published")
    assert _posted_ids(fake_hub)[-1] == row["request_id"]
    owner_project = "project-1"
    expected = {owner_project: 1} if other_project != owner_project else {owner_project: 2}
    if other_project != owner_project:
        expected[other_project] = 1
    assert _images_per_project(fake_hub) == expected
    # Idempotent afterwards: no new rows, no new images.
    assert _publish(roots, image, fake_hub, client=owner) == retried
    assert len(_publications(roots)) == 2 and _images_per_project(fake_hub) == expected


def test_repeated_response_mismatch_becomes_terminal(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    fake_hub.mismatch = True
    with pytest.raises(hub_publish.PublicationError, match="does not match the local manifest") as raised:
        _publish(roots, image, fake_hub)
    assert "new request id" not in str(raised.value)
    with pytest.raises(hub_publish.PublicationError, match="next attempt uses a new request id"):
        _publish(roots, image, fake_hub)
    [row] = _publications(roots)
    assert (row["status"], row["error_code"], row["terminal"]) == ("unknown", "response_mismatch", True)
    fake_hub.mismatch = False
    result = _publish(roots, image, fake_hub)
    assert result["status"] == "published" and result["request_id"] != row["request_id"]


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        ("server-error", "unknown", "http_500"),
        ("timeout", "unknown", "timeout"),
        ("forbidden", "failed", "http_403"),
        ("unreachable", "unknown", "transport"),
    ],
)
def test_failed_or_uncertain_publish_is_never_published_and_retry_reuses_request_id(
    tmp_path, fake_hub, failure, status, code
):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    client = _client(fake_hub, timeout_seconds=0.5)
    if failure == "server-error":
        fake_hub.failure = (500, "boom")
    elif failure == "forbidden":
        fake_hub.failure = (403, "forbidden")
    elif failure == "timeout":
        fake_hub.delay_post = 2.0
    else:
        client = HubClient("http://127.0.0.1:9", KEY, credential="image-key", timeout_seconds=0.5)
    with pytest.raises(hub_publish.PublicationError) as raised:
        _publish(roots, image, fake_hub, client=client)
    assert f"recorded as {status}" in str(raised.value)
    assert KEY not in str(raised.value)
    [row] = _publications(roots)
    assert (row["status"], row["error_code"], row["hub_image_id"], row["terminal"]) == (status, code, None, False)
    fake_hub.failure, fake_hub.delay_post = None, 0.0
    result = _publish(roots, image, fake_hub)
    assert result["status"] == "published" and result["request_id"] == row["request_id"]
    assert {post["request_id"] for post in fake_hub.posts} == {row["request_id"]}
    # A timed-out request that the Hub committed is replayed, not duplicated.
    assert len(fake_hub.images) == 1


def test_unrecorded_final_status_names_the_hub_image_and_rerun_reconciles(tmp_path, fake_hub, monkeypatch):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    original = Catalog.settle_publication

    def failing(self, request_id, **outcome):
        if outcome.get("status") == "published":
            raise CatalogError("injected")
        return original(self, request_id, **outcome)

    monkeypatch.setattr(Catalog, "settle_publication", failing)
    with pytest.raises(hub_publish.PublicationError) as raised:
        _publish(roots, image, fake_hub)
    [hub_image_id] = fake_hub.images
    assert hub_image_id in str(raised.value) and "re-running the same command reconciles" in str(raised.value)
    assert "recorded as" not in str(raised.value)
    [row] = _publications(roots)
    assert row["status"] == "pending"
    monkeypatch.setattr(Catalog, "settle_publication", original)
    result = _publish(roots, image, fake_hub)
    assert (result["status"], result["hub_image_id"], result["request_id"]) == (
        "published",
        hub_image_id,
        row["request_id"],
    )


@pytest.mark.parametrize(
    ("status", "detail", "expected", "code"),
    [
        (
            403,
            {"code": "image_count_limit", "visibility": "private", "scope": "user", "limit": 2, "used": 2},
            ["count limit", "private images per user", "limited to 2", "2 are in use", "existing name"],
            "http_403:image_count_limit",
        ),
        (
            413,
            {"code": "image_chain_size_limit", "visibility": "private", "limit_bytes": 2**30, "chain_bytes": 2**30 + 1},
            ["chain size limit", "1073741825 bytes", "1073741824 bytes (1.00 GiB)", "private limit"],
            "http_413:image_chain_size_limit",
        ),
        (
            413,
            {"code": "image_source_size_limit", "visibility": "private", "limit_bytes": 10, "source_bytes": 11},
            ["source size limit", "OCI source closure", "--no-source"],
            "http_413:image_source_size_limit",
        ),
        (
            422,
            [{"loc": ["body", "name"], "msg": "String should match pattern", "type": "string_pattern_mismatch"}],
            ["HTTP 422", "name: String should match pattern"],
            "http_422",
        ),
        (401, "invalid credential", ["rejected the credential", "PALIMPSEST_IMAGE_KEY"], "http_401"),
        (403, "forbidden", ["not allowed", "images:publish"], "http_403"),
    ],
)
def test_quota_validation_and_permission_errors_are_actionable_and_failed(
    tmp_path, fake_hub, status, detail, expected, code
):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    fake_hub.failure = (status, detail)
    with pytest.raises(hub_publish.PublicationError) as raised:
        _publish(roots, image, fake_hub)
    message = str(raised.value)
    for fragment in expected:
        assert fragment in message
    assert KEY not in message and "recorded as failed" in message
    [row] = _publications(roots)
    assert (row["status"], row["error_code"]) == ("failed", code)


def test_invalid_name_and_unconfirmed_public_are_refused_before_any_effect(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    with pytest.raises(hub_publish.PublicationError, match="must match"):
        _publish(roots, image, fake_hub, name="App_Name")
    with pytest.raises(hub_publish.PublicationError, match="--confirm-public"):
        hub_publish.publish_local_image(
            roots, image["image_id"], client=_client(fake_hub), hub_url=fake_hub.url, name="app", visibility="public"
        )
    assert fake_hub.requests == [] and _publications(roots) == []


def test_publish_without_source_sends_no_closure_and_is_a_separate_intent(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    notices = []
    plain = _publish(roots, image, fake_hub, include_source=False, notify=notices.append)
    assert "source_closure" not in fake_hub.posts[0]
    assert notices == [] and (plain["source_mode"], plain["source_closure"]) == ("none", None)
    staged = set(fake_hub.staged)
    assert staged == {_sha(payload) for payload in PAYLOADS}
    full = _publish(roots, image, fake_hub)
    assert full["request_id"] != plain["request_id"] and full["source_mode"] == "closure"


# -- credentials and transport ------------------------------------------------


def test_credentials_are_validated_and_never_echoed(tmp_path):
    with pytest.raises(hub_publish.PublicationError, match="requires credentials"):
        hub_publish.resolve_credential({}, allow_anonymous=False)
    assert hub_publish.resolve_credential({}, allow_anonymous=True).kind == "anonymous"
    malformed = "ppi_v1_not-a-real-key-but-secret"
    with pytest.raises(hub_publish.PublicationError) as raised:
        hub_publish.resolve_credential({"PALIMPSEST_IMAGE_KEY": malformed}, allow_anonymous=False)
    assert malformed not in str(raised.value)
    for token in ("tok en", "tok\r\nX-Injected: 1", "tök"):
        with pytest.raises(hub_publish.PublicationError, match="printable ASCII") as raised:
            hub_publish.resolve_credential({"PALIMPSEST_TOKEN": token}, allow_anonymous=False)
        assert token not in str(raised.value)
    key_file = tmp_path / "key"
    key_file.write_text(KEY + "\n")
    key_file.chmod(0o644)
    with pytest.raises(hub_publish.PublicationError, match="owner-only"):
        hub_publish.resolve_credential({}, key_file=key_file, allow_anonymous=False)
    key_file.chmod(0o600)
    credential = hub_publish.resolve_credential({"PALIMPSEST_TOKEN": TOKEN}, key_file=key_file, allow_anonymous=False)
    assert (credential.kind, credential.secret) == ("image-key", KEY)
    assert KEY not in repr(credential)
    assert hub_publish.resolve_credential({"PALIMPSEST_TOKEN": TOKEN}, allow_anonymous=False).kind == "keystone"


def test_hub_urls_are_canonical_and_credentials_need_secure_transport():
    with pytest.raises(hub_publish.PublicationError, match="credentials"):
        hub_publish.selected_hub_url("https://user:pass@hub.example/")
    assert hub_publish.selected_hub_url("HTTPS://Hub.Example:8443/api/") == "https://hub.example:8443/api"
    assert canonical_hub_url("https://hub.example:443/v") == "https://hub.example/v"
    assert canonical_hub_url("http://hub.example:80") == "http://hub.example"
    keyed = hub_publish.HubCredential("image-key", KEY)
    with pytest.raises(hub_publish.PublicationError, match="--allow-insecure-http"):
        hub_publish.require_secure_transport("http://hub.example", keyed, allow_insecure_http=False)
    for url in ("http://127.0.0.1:8080", "http://[::1]:8080", "http://localhost", "https://hub.example"):
        hub_publish.require_secure_transport(url, keyed, allow_insecure_http=False)
    hub_publish.require_secure_transport("http://hub.example", keyed, allow_insecure_http=True)
    hub_publish.require_secure_transport(
        "http://hub.example", hub_publish.HubCredential("anonymous"), allow_insecure_http=False
    )


def test_hub_client_sends_exactly_one_credential_form():
    def headers(client):
        captured = {}

        class Opener:
            def open(self, request, timeout):
                captured.update({name.lower(): value for name, value in request.header_items()})
                raise TimeoutError

        client._opener = Opener()
        with pytest.raises(TimeoutError):
            client._open("GET", "/published-images")
        return captured

    assert "x-auth-token" not in headers(HubClient("http://hub", KEY, credential="image-key"))
    assert headers(HubClient("http://hub", KEY, credential="image-key"))["authorization"] == "Bearer " + KEY
    anonymous = headers(HubClient("http://hub", None, credential="anonymous"))
    assert "authorization" not in anonymous and "x-auth-token" not in anonymous
    keystone = headers(HubClient("http://hub", TOKEN, project_id="p"))
    assert keystone["x-auth-token"] == TOKEN and "authorization" not in keystone
    with pytest.raises(HubError) as raised:
        HubClient("http://hub", "ppi_v1_secret-but-wrong", credential="image-key")
    assert "secret-but-wrong" not in str(raised.value)


# -- pull ---------------------------------------------------------------------


def test_pull_rebuilds_a_byte_identical_runnable_layout_and_registers_the_source(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    image = _local_image(publisher)
    published = _publish(publisher, image, fake_hub, visibility="public")
    first, second = _roots(tmp_path, "consumer-a"), _roots(tmp_path, "consumer-b")
    fake_hub.requests.clear()
    pulled = _pull(first, published["hub_image_id"], fake_hub, expected_name="app")
    assert all(
        "Authorization" not in request["headers"] and "X-Auth-Token" not in request["headers"]
        for request in fake_hub.requests
    )
    assert pulled["created"] is True and pulled["source"] == "published" and pulled["runnable"] is True
    assert pulled["manifest_digest"] == published["manifest_digest"]
    assert [layer["digest"] for layer in pulled["layers"]] == [_sha(item) for item in PAYLOADS]
    for item in PAYLOADS:
        # SquashFS lowers are kept in the store for a later conversion-skipping optimization.
        assert (first.store / "blobs" / "sha256" / _sha(item)[7:]).read_bytes() == item
    layout = first.state / pulled["source_layout"]
    assert pulled["source_layout"] == f"published/{published['hub_image_id']}/layout"
    original = publisher.state.parent / "publisher-source"
    assert _files(layout) == _files(original)
    assert not any(path.is_symlink() for path in layout.rglob("*"))
    assert {oct(path.stat().st_mode & 0o777) for path in layout.rglob("*") if path.is_file()} == {"0o400"}
    assert {oct(path.stat().st_mode & 0o777) for path in (layout, *layout.rglob("*")) if path.is_dir()} == {"0o700"}
    assert shlex.split(pulled["run_hint"]) == [
        "plm",
        "run",
        str(layout),
        "--runtime-kind",
        "oci-root",
        "--manifest",
        image["root_descriptor"]["digest"],
        "--platform",
        "linux/arm64",
        "--backend",
        "lima-vz",
    ]
    # Registered through the existing intake: same OCI graph and image identity as the publisher's.
    assert pulled["local_image_id"] == image["image_id"]
    with open_catalog(first, create=False) as catalog:
        [registered] = catalog.read_images()
    for key in ("image_id", "root_descriptor", "manifest_descriptor", "config_descriptor", "platform", "layers"):
        assert registered[key] == image[key]
    assert registered["source_cas_id"] != image["source_cas_id"] and registered["source_binding_verified"]
    reference = OCIImageRef(**image["reference"])
    snapshots = [
        LocalLayoutSource(layout=path).snapshot_acquisition(
            reference, SourceCAS(tmp_path.resolve() / f"cas-{index}"), platform="linux/arm64"
        )
        for index, path in enumerate((original, layout))
    ]
    assert snapshots[0].image == snapshots[1].image
    assert snapshots[0].image.digest == image["image_id"]
    assert [item.descriptor for item in snapshots[0].layers] == [item.descriptor for item in snapshots[1].layers]
    # The native-root source qualification (actual OS bytes -> boot profile; no VM) agrees.
    qualified = [
        qualify_native_root_source(
            LocalLayoutSource(layout=path),
            SourceCAS(tmp_path.resolve() / f"qualify-{index}"),
            platform="linux/arm64",
            backend="lima-vz",
        )
        for index, path in enumerate((original, layout))
    ]
    reports = [item.to_dict() for item in qualified]
    for key in ("platform", "index_descriptor", "layer_occurrences", "boot_profile", "execution_admitted"):
        assert reports[0][key] == reports[1][key]
    assert {(item.os_release.os_id, item.os_release.version_id) for item in qualified} == {("ubuntu", "24.04")}
    # Identical content is idempotent and downloads nothing again.
    again = _pull(first, published["hub_image_id"], fake_hub)
    assert (again["created"], again["downloaded"]) == (False, [])
    assert not second.metadata_db.exists() and not second.store.exists()
    _pull(second, published["hub_image_id"], fake_hub)
    with open_catalog(first, create=False) as a, open_catalog(second, create=False) as b:
        assert a.read_observation()["catalog_id"] != b.read_observation()["catalog_id"]
        assert len(a.read_pulled_images()) == len(b.read_pulled_images()) == 1
    for item in PAYLOADS:
        a_blob = os.stat(first.store / "blobs" / "sha256" / _sha(item)[7:])
        b_blob = os.stat(second.store / "blobs" / "sha256" / _sha(item)[7:])
        assert (a_blob.st_dev, a_blob.st_ino) != (b_blob.st_dev, b_blob.st_ino)
    with open_catalog(publisher, create=False) as catalog:
        assert catalog.read_pulled_images() == []


def test_pull_of_manifest_rooted_amd64_layout(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    image = _local_image(publisher, architecture="amd64", with_index=False)
    published = _publish(publisher, image, fake_hub, visibility="public")
    assert [item["role"] for item in fake_hub.posts[0]["source_closure"]][:2] == ["manifest", "config"]
    consumer = _roots(tmp_path, "consumer")
    pulled = _pull(consumer, published["hub_image_id"], fake_hub)
    layout = consumer.state / pulled["source_layout"]
    assert _files(layout) == _files(publisher.state.parent / "publisher-source")
    assert pulled["local_image_id"] == image["image_id"] and "--platform" not in pulled["run_hint"]
    assert pulled["run_requirement"] == "a Linux x86_64 host with KVM (OCI-root)"


@pytest.mark.parametrize("target", ["manifest", "layer"])
def test_tampered_source_blob_is_rejected_without_partial_layout_or_row(tmp_path, fake_hub, target):
    publisher = _roots(tmp_path, "publisher")
    image = _local_image(publisher)
    published = _publish(publisher, image, fake_hub, visibility="public")
    digest = (
        image["manifest_descriptor"]["digest"] if target == "manifest" else image["layers"][1]["compressed"]["digest"]
    )
    fake_hub.corrupt[digest] = b"X" * len(fake_hub.staged[digest])
    consumer = _roots(tmp_path, "consumer")
    with pytest.raises(hub_publish.PublicationError, match="failed SHA-256 verification"):
        _pull(consumer, published["hub_image_id"], fake_hub)
    assert os.listdir(consumer.state / "published") == []
    with open_catalog(consumer, create=False) as catalog:
        assert catalog.read_pulled_images() == [] and catalog.read_images() == []


@pytest.mark.parametrize("mode", ["same-size", "truncated", "oversized", "incomplete-read"])
def test_pull_rejects_corrupt_payload_and_leaves_no_partial_file_or_row(tmp_path, fake_hub, mode):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public", include_source=False)
    target = _sha(PAYLOADS[1])
    if mode == "incomplete-read":
        fake_hub.short_body.add(target)
    else:
        fake_hub.corrupt[target] = {
            "same-size": b"X" * len(PAYLOADS[1]),
            "truncated": PAYLOADS[1][:-1],
            "oversized": PAYLOADS[1] + b"!",
        }[mode]
    consumer = _roots(tmp_path, "consumer")
    with pytest.raises(hub_publish.PublicationError, match="nothing was stored"):
        _pull(consumer, published["hub_image_id"], fake_hub)
    blobs = consumer.store / "blobs" / "sha256"
    assert not (blobs / target[7:]).exists()
    assert [name for name in os.listdir(blobs) if name.startswith(".")] == []
    with open_catalog(consumer, create=False) as catalog:
        assert catalog.read_pulled_images() == []


def test_no_source_pull_is_a_clear_non_runnable_record(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public", include_source=False)
    consumer = _roots(tmp_path, "consumer")
    pulled = _pull(consumer, published["hub_image_id"], fake_hub)
    assert (pulled["runnable"], pulled["source_layout"], pulled["source_closure"]) == (False, None, [])
    assert "cannot be run with plm run" in pulled["message"] and "run_hint" not in pulled
    assert not (consumer.state / "published").exists()


def test_pull_whose_source_registration_fails_is_not_marked_runnable(tmp_path, fake_hub, monkeypatch):
    from palimpsest_local.errors import PalimpsestError

    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    consumer = _roots(tmp_path, "consumer")

    def refuse(*args, **kwargs):
        raise PalimpsestError("source intake refused the layout")

    monkeypatch.setattr(hub_publish, "_register_source", refuse)
    pulled = _pull(consumer, published["hub_image_id"], fake_hub)
    assert pulled["runnable"] is False and pulled["local_image_id"] is None
    assert pulled["source_registration_error"] == "source intake refused the layout"
    assert "run_hint" not in pulled and "not marked runnable" in pulled["message"]
    assert (consumer.state / pulled["source_layout"] / "index.json").is_file()


def test_pull_refuses_oversized_or_unfittable_images_before_downloading(tmp_path, fake_hub, monkeypatch):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    consumer = _roots(tmp_path, "consumer")
    fake_hub.requests.clear()
    with pytest.raises(hub_publish.PublicationError, match="pull cap"):
        _pull(consumer, published["hub_image_id"], fake_hub, max_bytes=4096)
    monkeypatch.setattr(hub_publish, "_free_bytes", lambda path: 1024)
    with pytest.raises(hub_publish.PublicationError, match="lacks free space"):
        _pull(consumer, published["hub_image_id"], fake_hub)
    assert all("/blobs/" not in request["path"] for request in fake_hub.requests)


def test_pull_refuses_differing_content_hides_private_images_and_reports_redirects(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    image = _local_image(publisher)
    public = _publish(publisher, image, fake_hub, name="public-app", visibility="public")
    private = _publish(publisher, image, fake_hub, name="private-app")
    consumer = _roots(tmp_path, "consumer")
    with pytest.raises(hub_publish.PublicationError, match="not found or is not visible"):
        _pull(consumer, private["hub_image_id"], fake_hub)
    with pytest.raises(hub_publish.PublicationError, match="name differs"):
        _pull(consumer, public["hub_image_id"], fake_hub, expected_name="other")
    _pull(consumer, public["hub_image_id"], fake_hub)
    # Descriptive projection changes alone are not a content difference.
    fake_hub.images[public["hub_image_id"]]["manifest"]["metadata"] = {"note": "edited"}
    assert _pull(consumer, public["hub_image_id"], fake_hub)["created"] is False
    fake_hub.images[public["hub_image_id"]]["name"] = "renamed"
    with pytest.raises(hub_publish.PublicationError, match="refusing to overwrite"):
        _pull(consumer, public["hub_image_id"], fake_hub)
    forged = fake_hub.images[public["hub_image_id"]]
    forged["name"] = "public-app"
    forged["manifest"]["layers"][1]["parent_id"] = None
    other = _roots(tmp_path, "other")
    with pytest.raises(hub_publish.PublicationError, match="failed verification"):
        _pull(other, public["hub_image_id"], fake_hub)
    del forged["manifest"]["layers"]
    with pytest.raises(hub_publish.PublicationError, match="failed verification"):
        _pull(other, public["hub_image_id"], fake_hub)
    fake_hub.redirect_gets = True
    with pytest.raises(hub_publish.PublicationError, match="redirect"):
        _pull(other, public["hub_image_id"], fake_hub)


def test_store_rm_refuses_pulled_published_payloads(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    consumer = state.init_resolved_roots(
        StatePaths(config=tmp_path.resolve() / "consumer-config", state=tmp_path.resolve() / "consumer")
    )
    _pull(consumer, published["hub_image_id"], fake_hub)
    target = _sha(PAYLOADS[1])
    with pytest.raises(StateError, match="published:" + published["hub_image_id"]):
        inventory.remove_artifact(consumer, target)
    assert (consumer.store / "blobs" / "sha256" / target[7:]).read_bytes() == PAYLOADS[1]


def test_store_listing_marks_pulled_published_payloads_as_not_deletable(tmp_path, fake_hub):
    from palimpsest_local.oci_layout import MEDIA_TYPE_LAYER_SQUASHFS, ContentStore

    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    consumer = state.init_resolved_roots(
        StatePaths(config=tmp_path.resolve() / "consumer-config", state=tmp_path.resolve() / "consumer")
    )
    _pull(consumer, published["hub_image_id"], fake_hub)
    target = _sha(PAYLOADS[1])
    # The same digest also has content-store metadata (for example a layer built locally before the pull).
    ContentStore(consumer.store).write_metadata(target, {"kind": "squashfs", "media_type": MEDIA_TYPE_LAYER_SQUASHFS})
    [row] = [item for item in inventory.list_artifacts(consumer)["artifacts"] if item["digest"] == target]
    owner = "published:" + published["hub_image_id"]
    assert row["delete_supported"] is False
    assert row["referenced_by"]["published"] == [owner] and row["deletion_blockers"]["published"] == [owner]
    with pytest.raises(StateError, match=owner):
        inventory.remove_artifact(consumer, target)


def test_store_move_without_keep_source_handles_a_pulled_layout(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    consumer = state.init_resolved_roots(
        StatePaths(config=tmp_path.resolve() / "consumer-config", state=tmp_path.resolve() / "consumer")
    )
    _pull(consumer, published["hub_image_id"], fake_hub)
    destination = tmp_path.resolve() / "moved"
    inventory.move_state_root(consumer, destination)
    assert not consumer.state.exists()
    assert (destination / "published" / published["hub_image_id"] / "layout" / "index.json").is_file()


def test_concurrent_pulls_of_one_closure_image_share_one_layout(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    closure = {item["digest"] for item in fake_hub.images[published["hub_image_id"]]["source_closure"]}
    handler = fake_hub.server.RequestHandlerClass
    original = handler.do_GET

    def slow(self):
        parts = self.path.split("/")
        if len(parts) == 6 and parts[5] in closure:
            time.sleep(0.2)  # every puller is inside its own staging directory at once
        return original(self)

    handler.do_GET = slow
    consumer = _roots(tmp_path, "consumer")
    barrier = threading.Barrier(4)
    results, errors = [], []

    def run():
        barrier.wait()
        try:
            results.append(_pull(consumer, published["hub_image_id"], fake_hub))
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion below
            errors.append(repr(exc))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    handler.do_GET = original
    assert errors == [] and len(results) == 4
    assert os.listdir(consumer.state / "published") == [published["hub_image_id"]]
    assert len({item["local_image_id"] for item in results}) == 1
    with open_catalog(consumer, create=False) as catalog:
        assert len(catalog.read_pulled_images()) == 1 and len(catalog.read_images()) == 1


def test_pull_requires_closure_commitments_by_ordinal(tmp_path, fake_hub):
    roots_a, roots_b = _roots(tmp_path, "pub-a"), _roots(tmp_path, "pub-b")
    image_a = _local_image(roots_a)
    image_b = _local_image(roots_b, env="MALICIOUS=1")
    a = _publish(roots_a, image_a, fake_hub, name="app-a", visibility="public")
    b = _publish(roots_b, image_b, fake_hub, name="app-b", visibility="public")
    record = fake_hub.images[a["hub_image_id"]]
    record["source_closure"] = copy.deepcopy(fake_hub.images[b["hub_image_id"]]["source_closure"])
    fake_hub.requests.clear()
    with pytest.raises(hub_publish.PublicationError, match="does not commit"):
        _pull(_roots(tmp_path, "consumer-1"), a["hub_image_id"], fake_hub)
    for key in ("manifest_digest", "config_digest", "root_digest"):
        record["manifest"]["source"].pop(key, None)
    consumer = _roots(tmp_path, "consumer-2")
    with pytest.raises(hub_publish.PublicationError, match="does not commit"):
        _pull(consumer, a["hub_image_id"], fake_hub)
    assert all("/blobs/" not in request["path"] for request in fake_hub.requests)
    with open_catalog(consumer) as catalog:
        assert catalog.read_pulled_images() == [] and catalog.read_images() == []
    # A layer commitment out of order is refused the same way.
    record["source_closure"] = copy.deepcopy(fake_hub.images[b["hub_image_id"]]["source_closure"])
    record["manifest"]["source"] = copy.deepcopy(fake_hub.images[b["hub_image_id"]]["manifest"]["source"])
    record["manifest"]["layers"][0]["source"], record["manifest"]["layers"][1]["source"] = (
        record["manifest"]["layers"][1]["source"],
        record["manifest"]["layers"][0]["source"],
    )
    with pytest.raises(hub_publish.PublicationError, match="layer 0"):
        _pull(_roots(tmp_path, "consumer-3"), a["hub_image_id"], fake_hub)


def test_oversized_documents_and_unregistrable_closures_are_refused_before_download(tmp_path, fake_hub, monkeypatch):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    record = fake_hub.images[published["hub_image_id"]]
    original = copy.deepcopy(record)
    big = os.urandom(2 * 2**20)
    fake_hub.staged[_sha(big)] = big
    for item in record["source_closure"]:
        if item["role"] == "manifest":
            item["digest"], item["size_bytes"] = _sha(big), len(big)
    record["manifest"]["source"]["manifest_digest"] = _sha(big)
    fake_hub.requests.clear()
    with pytest.raises(hub_publish.PublicationError, match="document declares"):
        _pull(_roots(tmp_path, "consumer-1"), published["hub_image_id"], fake_hub)
    fake_hub.images[published["hub_image_id"]] = original
    monkeypatch.setattr(hub_publish, "SOURCE_INTAKE_MAX_BYTES", 1024)
    with pytest.raises(hub_publish.PublicationError, match="source intake limit"):
        _pull(_roots(tmp_path, "consumer-2"), published["hub_image_id"], fake_hub)
    monkeypatch.setattr(hub_publish, "SOURCE_INTAKE_MAX_BYTES", 8 * 2**30)
    # Free space must cover the closure twice (layout + source CAS copy) plus the payloads.
    closure_bytes = sum({item["digest"]: item["size_bytes"] for item in original["source_closure"]}.values())
    payload_bytes = sum({_sha(payload): len(payload) for payload in PAYLOADS}.values())
    once = payload_bytes + closure_bytes + 256 * 2**20
    monkeypatch.setattr(hub_publish, "_free_bytes", lambda path: once)
    with pytest.raises(hub_publish.PublicationError, match="lacks free space"):
        _pull(_roots(tmp_path, "consumer-3"), published["hub_image_id"], fake_hub)
    # None of the three refusals downloaded a single blob.
    assert all("/blobs/" not in request["path"] for request in fake_hub.requests)
    monkeypatch.setattr(hub_publish, "_free_bytes", lambda path: once + closure_bytes)
    assert _pull(_roots(tmp_path, "consumer-4"), published["hub_image_id"], fake_hub)["runnable"] is True


def test_duplicate_keys_in_closure_documents_are_refused(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    record = fake_hub.images[published["hub_image_id"]]
    manifest_item = next(item for item in record["source_closure"] if item["role"] == "manifest")
    original = fake_hub.staged[manifest_item["digest"]]
    duplicated = b'{"schemaVersion":2,' + original[1:]
    fake_hub.staged[_sha(duplicated)] = duplicated
    root_item = next(item for item in record["source_closure"] if item["role"] == "root")
    record["source_closure"] = [item for item in record["source_closure"] if item["role"] != "root"]
    manifest_item["digest"], manifest_item["size_bytes"] = _sha(duplicated), len(duplicated)
    record["manifest"]["source"]["manifest_digest"] = _sha(duplicated)
    record["manifest"]["source"]["root_digest"] = _sha(duplicated)
    assert root_item["digest"] != manifest_item["digest"]
    consumer = _roots(tmp_path, "consumer")
    with pytest.raises(hub_publish.PublicationError, match="repeats an object key"):
        _pull(consumer, published["hub_image_id"], fake_hub)
    assert os.listdir(consumer.state / "published") == []


@pytest.mark.parametrize("kind", ["regular-file", "symlink", "published-dir-symlink"])
def test_unexpected_published_entries_are_refused_without_following_them(tmp_path, fake_hub, kind):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    consumer = _roots(tmp_path, "consumer")
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    if kind == "published-dir-symlink":
        (consumer.state / "published").symlink_to(outside)
    else:
        (consumer.state / "published").mkdir(mode=0o700)
        target = consumer.state / "published" / published["hub_image_id"]
        if kind == "regular-file":
            target.write_bytes(b"x")
        else:
            target.symlink_to(outside)
    with pytest.raises(hub_publish.PublicationError):
        _pull(consumer, published["hub_image_id"], fake_hub)
    assert os.listdir(outside) == []


def test_stale_staging_directories_are_swept_without_following_links(tmp_path, fake_hub):
    publisher = _roots(tmp_path, "publisher")
    published = _publish(publisher, _local_image(publisher), fake_hub, visibility="public")
    consumer = _roots(tmp_path, "consumer")
    base = consumer.state / "published"
    base.mkdir(mode=0o700)
    stale, fresh, active = base / ".staging-old", base / ".staging-new", base / ".staging-active"
    (stale / "layout").mkdir(parents=True)
    (stale / "layout" / "blob").write_bytes(b"partial")
    (stale / "layout").chmod(0o500)
    (active / "layout" / "blobs").mkdir(parents=True)
    (active / "layout" / "blobs" / "partial").write_bytes(b"still downloading")
    fresh.mkdir()
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    (base / ".staging-link").symlink_to(outside)
    old = time.time() - 2 * 24 * 3600
    for path in (stale / "layout" / "blob", stale / "layout", stale):
        os.utime(path, (old, old))
    # A slow download only updates its blob file: every directory is old, the blob is not.
    for path in (active / "layout" / "blobs", active / "layout", active):
        os.utime(path, (old, old))
    os.utime(base / ".staging-link", (old, old), follow_symlinks=False)
    _pull(consumer, published["hub_image_id"], fake_hub)
    assert sorted(os.listdir(base)) == sorted(
        [".staging-active", ".staging-link", ".staging-new", published["hub_image_id"]]
    )
    assert (active / "layout" / "blobs" / "partial").read_bytes() == b"still downloading"
    assert (outside / "keep").read_bytes() == b"keep"


# -- CLI ----------------------------------------------------------------------


def test_cli_publish_pull_status_never_expose_secrets(tmp_path, fake_hub, monkeypatch, capsys):
    publisher = _roots(tmp_path, "publisher")
    image = _local_image(publisher)
    monkeypatch.setenv("PALIMPSEST_IMAGE_KEY", KEY)
    monkeypatch.setenv("PALIMPSEST_TOKEN", TOKEN)
    monkeypatch.delenv("PALIMPSEST_URL", raising=False)
    base = ["--url", fake_hub.url, "published"]
    push = [
        *base,
        "push",
        image["image_id"],
        "--name",
        "app",
        "--visibility",
        "public",
        "--state-root",
        str(publisher.state),
    ]
    assert cli.main(push) == 1  # public without --confirm-public: refused before any request
    assert fake_hub.requests == []
    fake_hub.failure = (
        403,
        {"code": "image_count_limit", "visibility": "public", "scope": "user", "limit": 1, "used": 1},
    )
    assert cli.main([*push, "--confirm-public"]) == 1
    fake_hub.failure = None
    assert cli.main([*push, "--confirm-public"]) == 0
    consumer = tmp_path.resolve() / "fresh" / "consumer"
    assert cli.main([*base, "pull", str(uuid.uuid4()), "--state-root", str(consumer), "--anonymous"]) == 1
    hub_image_id = next(iter(fake_hub.images))
    assert cli.main([*base, "pull", hub_image_id, "--name", "app", "--state-root", str(consumer), "--anonymous"]) == 0
    assert cli.main([*base, "status", "--state-root", str(consumer)]) == 0
    output = capsys.readouterr()
    status = json.loads(output.out[output.out.rindex('{\n  "publications"') :])
    assert status["pulled_images"][0]["hub_image_id"] == hub_image_id
    assert "--confirm-public" in output.err and "count limit" in output.err
    assert "readable by anyone, anonymously" in output.err and "not found or is not visible" in output.err
    assert all(KEY not in text and TOKEN not in text for text in (output.out, output.err))
    for database in (publisher.metadata_db, consumer / "catalog-v1" / "metadata.sqlite3"):
        raw = database.read_bytes()
        assert KEY.encode() not in raw and TOKEN.encode() not in raw and b"config-env-value" not in raw
    pulls = [request for request in fake_hub.requests if request["method"] == "GET"]
    assert pulls and all("Authorization" not in request["headers"] for request in pulls)


def test_cli_never_prints_a_malformed_token(tmp_path, fake_hub, monkeypatch, capsys):
    publisher = _roots(tmp_path, "publisher")
    image = _local_image(publisher)
    token = "secret-token-part\r\nX-Injected: yes"
    monkeypatch.delenv("PALIMPSEST_IMAGE_KEY", raising=False)
    monkeypatch.setenv("PALIMPSEST_TOKEN", token)
    code = cli.main(
        [
            "--url",
            fake_hub.url,
            "published",
            "push",
            image["image_id"],
            "--name",
            "app",
            "--state-root",
            str(publisher.state),
        ]
    )
    output = capsys.readouterr()
    assert code == 1 and "printable ASCII" in output.err
    assert "secret-token-part" not in output.err + output.out and fake_hub.requests == []


def test_cli_wraps_unexpected_errors_without_their_text(tmp_path, monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise ValueError("Invalid header value b'leaked-secret'")

    monkeypatch.setattr(hub_publish, "pull_published_image", explode)
    monkeypatch.delenv("PALIMPSEST_IMAGE_KEY", raising=False)
    monkeypatch.delenv("PALIMPSEST_TOKEN", raising=False)
    code = cli.main(
        ["--url", "http://127.0.0.1:9", "published", "pull", str(uuid.uuid4()), "--state-root", str(tmp_path / "s")]
    )
    output = capsys.readouterr()
    assert code == 1 and "ValueError" in output.err and "leaked-secret" not in output.err


# -- catalog ------------------------------------------------------------------


def test_version_one_catalog_is_read_unchanged_and_upgraded_only_by_publication(tmp_path, fake_hub):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    connection = sqlite3.connect(roots.metadata_db)
    connection.executescript(
        "BEGIN; DROP TABLE pulled_layers; DROP TABLE pulled_images; DROP TABLE publications; PRAGMA user_version=1; COMMIT;"
    )
    connection.close()
    with open_catalog(roots, create=False) as catalog:
        assert catalog.read_publications() == [] and catalog.read_pulled_images() == []
        assert catalog.read_images()[0]["image_id"] == image["image_id"]
    assert sqlite3.connect(roots.metadata_db).execute("PRAGMA user_version").fetchone()[0] == 1
    assert _publish(roots, image, fake_hub)["status"] == "published"
    assert sqlite3.connect(roots.metadata_db).execute("PRAGMA user_version").fetchone()[0] == 2


def test_catalog_publication_rules_and_restricted_image_deletion(tmp_path):
    roots = _roots(tmp_path, "state")
    image = _local_image(roots)
    intent = {
        "image_id": image["image_id"],
        "hub_url": "https://hub.example",
        "name": "app",
        "visibility": "private",
        "manifest_digest": _sha(b"m"),
        "source_closure": None,
        "now": "t",
    }
    with open_catalog(roots) as catalog:
        with pytest.raises(CatalogError, match="must not embed credentials"):
            catalog.begin_publication(**{**intent, "hub_url": "https://user:pw@hub.example"})
        with pytest.raises(CatalogError, match="source closure"):
            catalog.begin_publication(
                **{**intent, "source_closure": [{"role": "layer", "digest": _sha(b"x"), "size_bytes": 1, "ordinal": 0}]}
            )
        row = catalog.begin_publication(**intent)
        with pytest.raises(CatalogError, match="Hub image id and project"):
            catalog.settle_publication(row["request_id"], status="published", hub_image_id=str(uuid.uuid4()), now="t")
        with pytest.raises(CatalogError, match="error code"):
            catalog.settle_publication(row["request_id"], status="failed", error_code="Token: secret", now="t")
        hub_id = str(uuid.uuid4())
        catalog.settle_publication(
            row["request_id"], status="published", hub_image_id=hub_id, hub_project_id="p", now="t"
        )
        with pytest.raises(CatalogError, match="immutable"):
            catalog.settle_publication(row["request_id"], status="unknown", now="t")
        with pytest.raises(CatalogError, match="not registered"):
            catalog.begin_publication(**{**intent, "image_id": _sha(b"absent")})
    connection = sqlite3.connect(roots.metadata_db)
    connection.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM images WHERE image_id=?", (image["image_id"],))
    connection.close()


# -- arm64 consumers ----------------------------------------------------------


def _manifest(platform: str, mode: str = "oci-root") -> dict:
    return {
        "schema": SCHEMA,
        "mode": mode,
        "platform": platform,
        "os_family": "ubuntu",
        "os_version": "24.04",
        "base_image_digest": _sha(b"cloud") if mode == "application" else None,
        "runtime": {"entrypoint": [], "cmd": ["/bin/true"], "env": [], "working_dir": "/", "user": "0"},
        "source": {"kind": "local"},
        "metadata": {},
        "layers": [{"digest": _sha(b"layer"), "size_bytes": 4096}],
    }


def test_arm64_is_publishable_for_oci_root_only_and_every_native_consumer_fails_closed():
    from palimpsest_local import nova_ceph_boot

    arm = normalize_manifest(_manifest("linux/arm64"))
    assert arm["platform"] == "linux/arm64"
    with pytest.raises(ValueError, match="compatibility"):
        normalize_manifest(_manifest("linux/arm64", mode="application"))
    with pytest.raises(ValueError, match="compatibility"):
        normalize_manifest(_manifest("linux/riscv64"))
    with pytest.raises(ValueError, match="linux/amd64"):
        native_image_projection(arm)
    # Even a hand-built projection cannot pass the carrier/boot validator.
    amd = native_image_projection(_manifest("linux/amd64"))
    forged = copy.deepcopy(amd)
    forged["source"]["manifest"] = arm
    forged["source"]["manifest_digest"] = manifest_digest(arm)
    nova_ceph_boot.validate_image_projection(amd, amd["source"]["manifest_digest"])
    with pytest.raises(nova_ceph_boot.BootError) as raised:
        nova_ceph_boot.validate_image_projection(forged, forged["source"]["manifest_digest"])
    assert raised.value.code == "published_manifest"
