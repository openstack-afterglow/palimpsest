from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import cloud_layer_worker as worker


@pytest.fixture
def short_socket_path():
    # Darwin's sockaddr_un cannot hold pytest's full default temporary path.
    with tempfile.TemporaryDirectory(prefix="plw-", dir="/tmp") as directory:
        yield Path(directory) / "buildkit.sock"


def _store(tmp_path):
    store = tmp_path / "store"
    (store / "blobs" / "sha256").mkdir(parents=True)
    (store / ".incoming").mkdir()
    (store / "images").mkdir()
    return store


def _source(tmp_path, payload=b"verified-layer"):
    source = tmp_path / "layer"
    source.write_bytes(payload)
    return source, "sha256:" + hashlib.sha256(payload).hexdigest()


def _record():
    return {
        "schema": "palimpsest.cloud-layer-image.v1",
        "name": "example",
        "source": {"root_digest": "sha256:" + "a" * 64},
        "layers": [{"digest": "sha256:" + "b" * 64, "size": 10}],
        "runtime": {"cmd": ["true"]},
        "built_at_unix": 1.0,
    }


@pytest.mark.parametrize(
    "digest",
    [
        None,
        "",
        "sha256",
        "sha512:" + "a" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:../../outside",
        "sha256:" + "a" * 64 + "\n",
    ],
)
def test_invalid_digest_is_rejected_before_any_path_access(tmp_path, digest, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid digest reached path access")

    monkeypatch.setattr(Path, "lstat", unexpected)
    with pytest.raises(worker.WorkerError, match="digest"):
        worker.publish_blob(tmp_path / "missing", digest, tmp_path / "missing-store")


def test_competing_blob_writers_link_once_without_clobber(tmp_path, monkeypatch):
    store = _store(tmp_path)
    source, digest = _source(tmp_path)
    target = store / "blobs" / "sha256" / digest[7:]
    barrier = threading.Barrier(2)
    real_link = os.link

    def competing_link(src, dst, **kwargs):
        barrier.wait(timeout=5)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(worker.os, "link", competing_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker.publish_blob, source, digest, store) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(result["published"] for result in results) == ["existing", "new"]
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o444
    assert list((store / ".incoming").iterdir()) == []


def test_blob_race_preserves_a_different_winning_file(tmp_path, monkeypatch):
    store = _store(tmp_path)
    source, digest = _source(tmp_path)
    target = store / "blobs" / "sha256" / digest[7:]
    winner = tmp_path / "winner"
    winner.write_bytes(b"different-writer")
    real_link = os.link

    def race(src, dst, **kwargs):
        real_link(winner, dst)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(worker.os, "link", race)
    with pytest.raises(worker.WorkerError, match="differs"):
        worker.publish_blob(source, digest, store)
    assert target.read_bytes() == b"different-writer"
    assert target.stat().st_ino == winner.stat().st_ino
    assert list((store / ".incoming").iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
@pytest.mark.parametrize("location", ["source", "target"])
def test_publication_rejects_nonregular_files(tmp_path, kind, location):
    store = _store(tmp_path)
    source, digest = _source(tmp_path)
    path = source if location == "source" else store / "blobs" / "sha256" / digest[7:]
    if location == "source":
        source.unlink()
    referent = tmp_path / "referent"
    referent.write_bytes(b"verified-layer")
    if kind == "symlink":
        path.symlink_to(referent)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(worker.WorkerError, match="regular"):
        worker.publish_blob(source, digest, store)
    assert os.path.lexists(path)
    assert referent.read_bytes() == b"verified-layer"
    assert list((store / ".incoming").iterdir()) == []


def test_bad_source_is_rejected_even_when_target_matches(tmp_path):
    store = _store(tmp_path)
    source, digest = _source(tmp_path)
    worker.publish_blob(source, digest, store)
    source.write_bytes(b"wrong")
    with pytest.raises(worker.WorkerError, match="verification"):
        worker.publish_blob(source, digest, store)
    assert (store / "blobs" / "sha256" / digest[7:]).read_bytes() == b"verified-layer"


def test_failed_publication_cleans_only_its_temporary(tmp_path, monkeypatch):
    store = _store(tmp_path)
    source, digest = _source(tmp_path)
    unrelated = store / ".incoming" / "other-worker"
    unrelated.write_bytes(b"preserve")

    def fail_link(*args, **kwargs):
        raise OSError("publication unavailable")

    monkeypatch.setattr(worker.os, "link", fail_link)
    with pytest.raises(OSError, match="publication unavailable"):
        worker.publish_blob(source, digest, store)
    assert list((store / ".incoming").iterdir()) == [unrelated]


def test_preexisting_temporary_is_never_removed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    source, digest = _source(tmp_path)
    temporary = store / ".incoming" / (digest[7:] + ".fixed")
    temporary.write_bytes(b"other-worker")
    monkeypatch.setattr(worker.uuid, "uuid4", lambda: type("ID", (), {"hex": "fixed"})())
    with pytest.raises(FileExistsError):
        worker.publish_blob(source, digest, store)
    assert temporary.read_bytes() == b"other-worker"


def test_image_binding_reuse_preserves_original_timestamp_and_inode(tmp_path):
    store = _store(tmp_path)
    path = store / "images" / "example.json"
    original = _record()
    worker._atomic_json(path, original)
    before = path.read_bytes(), path.stat().st_ino
    repeated = {**original, "built_at_unix": 999.0}
    assert worker._atomic_json(path, repeated) == original
    assert (path.read_bytes(), path.stat().st_ino) == before
    assert path.stat().st_mode & 0o777 == 0o444
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("field", ["source", "layers", "runtime"])
def test_image_binding_collision_never_replaces_tag(tmp_path, field):
    store = _store(tmp_path)
    path = store / "images" / "example.json"
    original = _record()
    worker._atomic_json(path, original)
    before = path.read_bytes(), path.stat().st_ino
    changed = copy.deepcopy(original)
    changed[field] = {"different": True} if field != "layers" else []
    with pytest.raises(worker.WorkerError, match="immutable binding"):
        worker._atomic_json(path, changed)
    assert (path.read_bytes(), path.stat().st_ino) == before
    assert list(path.parent.iterdir()) == [path]


def test_image_binding_race_preserves_winning_record(tmp_path, monkeypatch):
    store = _store(tmp_path)
    path = store / "images" / "example.json"
    winner = tmp_path / "winner.json"
    winning_record = {**_record(), "runtime": {"cmd": ["other"]}}
    winner.write_text(json.dumps(winning_record))
    real_link = os.link

    def race(src, dst, **kwargs):
        real_link(winner, dst)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(worker.os, "link", race)
    with pytest.raises(worker.WorkerError, match="immutable binding"):
        worker._atomic_json(path, _record())
    assert json.loads(path.read_text()) == winning_record
    assert path.stat().st_ino == winner.stat().st_ino
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_existing_image_record_must_be_regular(tmp_path, kind):
    store = _store(tmp_path)
    path = store / "images" / "example.json"
    if kind == "symlink":
        referent = tmp_path / "record.json"
        referent.write_text(json.dumps(_record()))
        path.symlink_to(referent)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(worker.WorkerError, match="regular"):
        worker._atomic_json(path, _record())
    assert os.path.lexists(path)


def test_worker_lock_is_exclusive_and_nonblocking(tmp_path):
    lock = tmp_path / "worker.lock"
    with worker._exclusive_lock(lock):
        with pytest.raises(worker.WorkerHold, match="already held"):
            with worker._exclusive_lock(lock):
                pytest.fail("second worker acquired the lock")
    with worker._exclusive_lock(lock):
        pass


def test_preexisting_socket_is_preserved_without_starting_process(tmp_path, monkeypatch):
    address = tmp_path / "buildkit.sock"
    address.write_bytes(b"not-owned")
    monkeypatch.setattr(worker, "BUILDKIT_SOCKET", address)

    def unexpected(*args, **kwargs):
        pytest.fail("preexisting socket started a process")

    monkeypatch.setattr(worker.subprocess, "Popen", unexpected)
    with pytest.raises(worker.WorkerHold, match="preexisting"):
        worker._buildkit(tmp_path, tmp_path, tmp_path / "out", tmp_path / "root", tmp_path / "log", 1e20)
    assert address.read_bytes() == b"not-owned"


@pytest.mark.parametrize("stuck", ["client", "daemon"])
def test_uncertain_shutdown_is_term_only_hold_and_preserves_socket(tmp_path, monkeypatch, stuck, short_socket_path):
    address = short_socket_path
    monkeypatch.setattr(worker, "BUILDKIT_SOCKET", address)
    bound = socket.socket(socket.AF_UNIX)
    processes = []

    class Process:
        def __init__(self, argv, **kwargs):
            self.kind = "daemon" if argv[0] == "buildkitd" else "client"
            self.pid = len(processes) + 100
            self.stopped = False
            self.terms = 0
            processes.append(self)
            if self.kind == "daemon":
                bound.bind(str(address))

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.terms += 1

        def wait(self, timeout):
            if self.kind == stuck:
                raise subprocess.TimeoutExpired(self.kind, timeout)
            self.stopped = True
            return 0

        def kill(self):
            pytest.fail("forced kill is forbidden")

    monkeypatch.setattr(worker.subprocess, "Popen", Process)
    try:
        with pytest.raises(worker.WorkerHold, match="TERM"):
            worker._buildkit(tmp_path, tmp_path, tmp_path / "out", tmp_path / "root", tmp_path / "log", 1e20)
        assert address.exists()
        assert (tmp_path / "buildkit.hold.json").is_file()
        assert address.with_suffix(".hold.json").is_file()
        assert (tmp_path / "buildkit-owner.json").is_file()
        assert (tmp_path / "buildkitd.log").is_file()
        assert processes[0].terms == 1
        assert processes[1].terms == (1 if stuck == "client" else 0)
    finally:
        bound.close()


def test_build_holds_scratch_lock_through_publication_and_keeps_evidence(tmp_path, monkeypatch):
    store = _store(tmp_path)
    scratch = tmp_path / "scratch"
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    source, digest = _source(tmp_path)
    config_path = tmp_path / ("c" * 64)
    config_path.write_text(json.dumps({"config": {"Cmd": ["true"]}}))
    seen = []

    def locked(stage):
        with pytest.raises(worker.WorkerHold, match="already held"):
            with worker._exclusive_lock(scratch / "worker.lock"):
                pytest.fail("build released its scratch lock early")
        seen.append(stage)

    def extract(archive, context, **kwargs):
        locked("extract")
        context.mkdir()

    def buildkit(context, definition, output, root, log, deadline):
        locked("buildkit")
        output.write_bytes(b"retained OCI evidence")
        log.write_text("retained client log")
        (log.parent / "buildkitd.log").write_text("retained daemon log")

    class Cache:
        def __init__(self, root, **kwargs):
            locked("cache")

        def import_source(self, archive):
            return {"root_digest": "sha256:" + "a" * 64}

        def materialize(self, digest_arg, **kwargs):
            locked("materialize")
            return {
                "receipt": {
                    "results": [{"receipt": {"image_digest": digest, "image_size": source.stat().st_size}}],
                    "root_descriptor": {"digest": "sha256:" + "a" * 64},
                    "manifest_digest": "sha256:" + "b" * 64,
                },
                "image": {"config_descriptor": {"digest": "sha256:" + "c" * 64}},
                "blob_paths": [str(source)],
                "source_blob_paths": [str(config_path)],
            }

    real_publish = worker.publish_blob
    real_record = worker._atomic_json

    def publish(*args):
        locked("blob")
        return real_publish(*args)

    def record(*args):
        locked("record")
        return real_record(*args)

    monkeypatch.setattr(worker, "sys", SimpleNamespace(**{**vars(worker.sys), "platform": "linux"}))
    monkeypatch.setattr(worker.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    # Simulate root ownership without requiring a root pytest process.
    real_fstat = worker.os.fstat

    def fstat(fd):
        metadata = list(real_fstat(fd))
        metadata[4] = 0
        return os.stat_result(metadata)

    monkeypatch.setattr(worker.os, "fstat", fstat)
    monkeypatch.setattr(worker, "require_ceph_store", lambda store: None)
    monkeypatch.setattr(worker, "extract_context", extract)
    monkeypatch.setattr(worker, "_buildkit", buildkit)
    monkeypatch.setattr(worker, "LayerCache", Cache)
    monkeypatch.setattr(worker, "publish_blob", publish)
    monkeypatch.setattr(worker, "_atomic_json", record)
    result = worker.build(
        dockerfile=dockerfile,
        context_tar=tmp_path / "context.tar",
        store=store,
        scratch=scratch,
        name="example",
        timeout_seconds=60,
    )
    assert seen == ["extract", "buildkit", "cache", "materialize", "blob", "record"]
    evidence = Path(result["evidence_directory"])
    assert (evidence / "image.oci.tar").read_bytes() == b"retained OCI evidence"
    assert (evidence / "buildctl.log").read_text() == "retained client log"
    assert (evidence / "buildkitd.log").read_text() == "retained daemon log"
    with worker._exclusive_lock(scratch / "worker.lock"):
        pass


def test_cli_reports_hold_not_success(tmp_path, monkeypatch, capsys):
    def held(**kwargs):
        raise worker.WorkerHold("HOLD: uncertain shutdown")

    monkeypatch.setattr(worker, "build", held)
    result = worker.main(
        [
            "build",
            "--dockerfile",
            str(tmp_path / "Dockerfile"),
            "--context-tar",
            str(tmp_path / "context.tar"),
            "--store",
            str(tmp_path / "store"),
            "--scratch",
            str(tmp_path / "scratch"),
            "--name",
            "example",
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert json.loads(captured.err)["status"] == "hold"


@pytest.mark.parametrize("outcome", ["success", "timeout", "replaced-socket"])
def test_confirmed_shutdown_only_removes_its_owned_socket(tmp_path, monkeypatch, outcome, short_socket_path):
    address = short_socket_path
    monkeypatch.setattr(worker, "BUILDKIT_SOCKET", address)
    bound = socket.socket(socket.AF_UNIX)
    processes = []

    class Process:
        def __init__(self, argv, **kwargs):
            self.kind = "daemon" if argv[0] == "buildkitd" else "client"
            self.pid = len(processes) + 100
            self.stopped = False
            self.terms = 0
            processes.append(self)
            if self.kind == "daemon":
                bound.bind(str(address))

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.terms += 1

        def wait(self, timeout):
            if self.kind == "client" and outcome == "timeout" and not self.terms:
                raise subprocess.TimeoutExpired(self.kind, timeout)
            if self.kind == "client" and outcome == "replaced-socket":
                address.unlink()
                address.write_bytes(b"replacement must survive")
            self.stopped = True
            return 0

        def kill(self):
            pytest.fail("forced kill is forbidden")

    monkeypatch.setattr(worker.subprocess, "Popen", Process)
    try:
        args = (tmp_path, tmp_path, tmp_path / "out", tmp_path / "root", tmp_path / "log", 1e20)
        if outcome == "success":
            worker._buildkit(*args)
        elif outcome == "timeout":
            with pytest.raises(worker.WorkerError, match="deadline") as error:
                worker._buildkit(*args)
            assert not isinstance(error.value, worker.WorkerHold)
        else:
            with pytest.raises(worker.WorkerHold, match="ownership changed"):
                worker._buildkit(*args)
        assert all(process.stopped for process in processes)
        assert processes[0].terms == 1
        if outcome == "replaced-socket":
            assert address.read_bytes() == b"replacement must survive"
        else:
            assert not os.path.lexists(address)
        assert (tmp_path / "buildkit-owner.json").is_file()
        assert (tmp_path / "buildkitd.log").is_file()
        assert (tmp_path / "log").is_file()
    finally:
        bound.close()


@pytest.fixture
def registry_worker(tmp_path, monkeypatch):
    """A real filesystem publisher around a controlled cache callback boundary."""
    store = _store(tmp_path)
    scratch = tmp_path / "scratch"
    cas = tmp_path / "source-cas"
    cas.mkdir()
    paths = []

    def descriptor(payload, media_type):
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        path = cas / digest[7:]
        path.write_bytes(payload)
        paths.append(str(path))
        return {"digest": digest, "size": len(payload), "mediaType": media_type}

    originals = [
        {
            "ordinal": ordinal,
            "compressed": descriptor(f"compressed-{ordinal}".encode(), "application/vnd.oci.image.layer.v1.tar+gzip"),
            "diff_id": "sha256:" + hashlib.sha256(f"diff-{ordinal}".encode()).hexdigest(),
        }
        for ordinal in range(3)
    ]
    process = {"Entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"], "Env": ["CUDA_VERSION=12.2.2"]}
    config = {
        "os": "linux",
        "architecture": "amd64",
        "config": process,
        "rootfs": {"type": "layers", "diff_ids": [layer["diff_id"] for layer in originals]},
    }
    config_descriptor = descriptor(json.dumps(config).encode(), "application/vnd.oci.image.config.v1+json")
    manifest = {"schemaVersion": 2, "config": config_descriptor, "layers": [layer["compressed"] for layer in originals]}
    manifest_descriptor = descriptor(json.dumps(manifest).encode(), "application/vnd.oci.image.manifest.v1+json")
    pin = manifest_descriptor["digest"]
    reference = f"docker.io/nvidia/cuda@{pin}"
    imported = {
        "root_digest": pin,
        "image": {
            "root_descriptor": manifest_descriptor,
            "manifest_descriptor": manifest_descriptor,
            "config_descriptor": config_descriptor,
            "platform": {"os": "linux", "architecture": "amd64"},
            "layers": originals,
        },
        "source_blob_paths": paths,
    }
    derived = tmp_path / "derived"
    graph_digest = "sha256:" + hashlib.sha256(b"canonical-image-graph-not-manifest").hexdigest()
    derived.mkdir()
    blob_paths = []
    receipts = []
    for ordinal, original in enumerate(originals):
        # Repeated derived bytes intentionally exercise one canonical Ceph blob
        # despite distinct source ordinals and conversion keys.
        payload = b"derived-squashfs".ljust(4096, b"\0")
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        path = derived / digest[7:]
        path.write_bytes(payload)
        blob_paths.append(path)
        key_digest = "sha256:" + hashlib.sha256(f"key-{ordinal}".encode()).hexdigest()
        record_digest = "sha256:" + hashlib.sha256(f"record-{ordinal}".encode()).hexdigest()
        snapshot_digest = "sha256:" + "2" * 64
        occurrence = {
            "schema": worker.DERIVED_OCCURRENCE_SCHEMA,
            "key_digest": key_digest,
            "record_digest": record_digest,
            "ordinal": ordinal,
            "source": {
                "compressed_digest": original["compressed"]["digest"],
                "compressed_size": original["compressed"]["size"],
                "media_type": original["compressed"]["mediaType"],
                "diff_id": original["diff_id"],
            },
            "source_image_digest": graph_digest,
            "source_snapshot_binding_digest": snapshot_digest,
        }
        receipts.append(
            {
                "store_id": "oci-store-v1:" + "1" * 64,
                "key_digest": key_digest,
                "record_digest": record_digest,
                "occurrence_digest": worker._json_digest(occurrence),
                "source_snapshot_binding_digest": snapshot_digest,
                "source_image_digest": graph_digest,
                "ordinal": ordinal,
                "image_digest": digest,
                "image_size": len(payload),
                "filesystem": "squashfs",
            }
        )
    state = {
        "store": store,
        "scratch": scratch,
        "reference": reference,
        "imported": imported,
        "receipts": receipts,
        "blob_paths": blob_paths,
        "config": config,
        "events": [],
        "fail_before": None,
        "truncate": False,
        "job_id": "a" * 32,
    }
    real_fsync = worker._fsync_dir

    def fsync(directory):
        real_fsync(directory)
        state["events"].append(("fsync", directory))
        for layer in directory.glob("layer-*.json"):
            state["events"].append(("durable-layer", layer))

    class Cache:
        def __init__(self, root, *, initialize):
            state["events"].append(("cache", initialize))

        def pull(self, selected, *, timeout_seconds):
            assert selected == reference
            assert 0 < timeout_seconds <= 60
            state["events"].append(("pull", selected))
            return imported

        def materialize(self, selected, *, timeout_seconds, pack_profile, on_layer):
            assert selected == pin
            assert 0 < timeout_seconds <= 60
            assert pack_profile is worker.SquashFSPackProfile.SERVICE_IMAGE
            with pytest.raises(worker.WorkerHold, match="already held"):
                with worker._exclusive_lock(scratch / "worker.lock"):
                    pytest.fail("intake released the scratch lock")
            for ordinal, receipt in enumerate(receipts):
                if state["truncate"] and ordinal == 2:
                    break
                state["events"].append(("convert", ordinal))
                assert not (store / "images" / "cuda.json").exists()
                if ordinal:
                    journal = store / "jobs" / state["job_id"]
                    previous = journal / f"layer-{ordinal - 1:06d}.json"
                    prefix = json.loads(previous.read_text())
                    assert previous.stat().st_mode & 0o777 == 0o444
                    assert ("durable-layer", previous) in state["events"]
                    assert (store / prefix["receipt"]).stat().st_mode & 0o777 == 0o444
                    assert (store / "blobs" / "sha256" / prefix["digest"][7:]).exists()
                if state["fail_before"] == ordinal:
                    raise worker.WorkerError(f"conversion {ordinal} failed")
                on_layer(ordinal, blob_paths[ordinal], receipt)
            return {
                **imported,
                "blob_paths": [str(path) for path in blob_paths],
                "receipt": {
                    "root_descriptor": manifest_descriptor,
                    "manifest_digest": pin,
                    "config_descriptor": config_descriptor,
                    "platform": {"os": "linux", "architecture": "amd64"},
                    "layers": originals,
                    "source_image_digest": graph_digest,
                    "source_snapshot_binding_digest": "sha256:" + "2" * 64,
                    "results": [{"receipt": value, "cache_result": "materialized"} for value in receipts],
                },
            }

    monkeypatch.setattr(worker, "sys", SimpleNamespace(**{**vars(worker.sys), "platform": "linux"}))
    monkeypatch.setattr(worker.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    real_fstat = worker.os.fstat

    def fstat(fd):
        metadata = list(real_fstat(fd))
        metadata[4] = 0
        return os.stat_result(metadata)

    monkeypatch.setattr(worker.os, "fstat", fstat)
    monkeypatch.setattr(worker, "require_ceph_store", lambda selected: state["events"].append(("ceph", selected)))
    monkeypatch.setattr(worker, "LayerCache", Cache)
    monkeypatch.setattr(worker, "_fsync_dir", fsync)
    return state


def _pull_registry(state, **options):
    return worker.pull(
        reference=state["reference"],
        store=state["store"],
        scratch=state["scratch"],
        name="cuda",
        timeout_seconds=60,
        job_id=state["job_id"],
        **options,
    )


def test_registry_pull_commits_each_layer_before_next_conversion_and_deduplicates(registry_worker):
    state = registry_worker
    result = _pull_registry(state)
    assert result["job_id"] == state["job_id"]
    assert result["source"]["reference"] == state["reference"]
    assert result["source"]["config"] == state["config"]
    assert "Cmd" not in result["source"]["config"]["config"]
    assert "cmd" not in result["runtime"]
    assert result["runtime"]["entrypoint"] == ["/opt/nvidia/nvidia_entrypoint.sh"]
    assert result["publication"] == ["new", "existing", "existing"]
    assert len(list((state["store"] / "blobs" / "sha256").iterdir())) == 1
    assert [layer["ordinal"] for layer in result["layers"]] == [0, 1, 2]
    assert len({layer["conversion_key"] for layer in result["layers"]}) == 3
    for ordinal, layer in enumerate(result["layers"]):
        metadata = json.loads((state["store"] / layer["receipt"]).read_text())
        assert metadata["source"] == state["imported"]["image"]["layers"][ordinal]
        assert metadata["conversion"] == state["receipts"][ordinal]
        assert metadata["manifest_descriptor"] == state["imported"]["image"]["manifest_descriptor"]
    assert json.loads((state["store"] / "images" / "cuda.json").read_text())["layers"] == result["layers"]


def test_registry_conversion_failure_retains_only_committed_prefix(registry_worker):
    state = registry_worker
    state["fail_before"] = 1
    with pytest.raises(worker.WorkerError, match="conversion 1 failed"):
        _pull_registry(state)
    journal = state["store"] / "jobs" / state["job_id"]
    assert (journal / "layer-000000.json").exists()
    assert not (journal / "layer-000001.json").exists()
    assert not (state["store"] / "images" / "cuda.json").exists()
    assert json.loads((journal / "failure.json").read_text())["completed_layers"] == 1
    assert ("convert", 2) not in state["events"]
    with pytest.raises(worker.WorkerHold, match="automatic replay"):
        _pull_registry(state)


@pytest.mark.parametrize("field", ["ordinal", "source_image_digest", "occurrence_digest", "image_size", "image_digest"])
def test_registry_rejects_receipt_contradictions_before_publication(registry_worker, field):
    state = registry_worker
    value = state["receipts"][0]
    value[field] = 9 if field == "ordinal" else 8192 if field == "image_size" else "sha256:" + "f" * 64
    with pytest.raises(worker.WorkerError):
        _pull_registry(state)
    assert list((state["store"] / "blobs" / "sha256").iterdir()) == []
    assert not (state["store"] / "images" / "cuda.json").exists()
    assert ("convert", 1) not in state["events"]


def test_registry_callback_publication_error_holds_and_never_converts_next(registry_worker, monkeypatch):
    state = registry_worker
    real_publish = worker.publish_blob
    calls = 0

    def unavailable(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("CephFS journal/publication outcome unknown")
        return real_publish(*args)

    monkeypatch.setattr(worker, "publish_blob", unavailable)
    with pytest.raises(worker.WorkerHold, match="requires review"):
        _pull_registry(state)
    assert ("convert", 2) not in state["events"]
    assert (state["scratch"] / "intake.hold.json").exists()
    assert not (state["store"] / "images" / "cuda.json").exists()
    state["job_id"] = "b" * 32
    with pytest.raises(worker.WorkerHold, match="prior worker"):
        _pull_registry(state)


def test_registry_final_sequence_mismatch_has_no_image_success(registry_worker):
    state = registry_worker
    state["truncate"] = True
    with pytest.raises(worker.WorkerError, match="exact published source sequence"):
        _pull_registry(state)
    assert not (state["store"] / "images" / "cuda.json").exists()
    assert len(list((state["store"] / "jobs" / state["job_id"]).glob("layer-*.json"))) == 2


def test_existing_blob_hit_verifies_source_without_ceph_copy(tmp_path, monkeypatch):
    store = _store(tmp_path)
    source, digest = _source(tmp_path)
    worker.publish_blob(source, digest, store)
    target = store / "blobs" / "sha256" / digest[7:]
    before = target.stat().st_ino

    def no_copy(*args, **kwargs):
        pytest.fail("existing blob hit copied data into CephFS")

    monkeypatch.setattr(worker.shutil, "copyfileobj", no_copy)
    assert worker.publish_blob(source, digest, store)["published"] == "existing"
    assert target.stat().st_ino == before
    source.write_bytes(b"bad source")
    with pytest.raises(worker.WorkerError, match="verification"):
        worker.publish_blob(source, digest, store)
    assert list((store / ".incoming").iterdir()) == []


@pytest.mark.parametrize(
    "reference",
    [
        "nvidia/cuda@sha256:" + "a" * 64,
        "docker.io/nvidia/cuda:latest",
        "docker.io/nvidia/cuda:12.2.2@sha256:" + "a" * 64,
        "https://docker.io/nvidia/cuda@sha256:" + "a" * 64,
        "docker.io/nvidia/cuda@sha256:" + "A" * 64,
        "docker.io/nvidia/cuda@sha256:" + "a" * 63,
        "docker.io/nvidia/cuda@sha256:" + "a" * 64 + "\n",
        "docker.io/nvidia/cuda@sha512:" + "a" * 64,
    ],
)
def test_pull_rejects_nonexplicit_or_mutable_pins_before_worker_access(tmp_path, monkeypatch, reference):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid pin reached worker store access")

    monkeypatch.setattr(worker, "require_ceph_store", unexpected)
    monkeypatch.setattr(worker, "sys", SimpleNamespace(**{**vars(worker.sys), "platform": "linux"}))
    monkeypatch.setattr(worker.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    with pytest.raises(worker.WorkerError, match="digest|fully qualified"):
        worker.pull(reference=reference, store=tmp_path, scratch=tmp_path, name="cuda", timeout_seconds=60)


def test_pull_cli_passes_reference_job_and_shared_options(tmp_path, monkeypatch, capsys):
    seen = {}

    def intake(**kwargs):
        seen.update(kwargs)
        return {"name": kwargs["name"], "job_id": kwargs["job_id"]}

    monkeypatch.setattr(worker, "pull", intake)
    reference = "docker.io/nvidia/cuda@sha256:" + "a" * 64
    assert (
        worker.main(
            [
                "pull",
                "--reference",
                reference,
                "--job-id",
                "b" * 32,
                "--store",
                str(tmp_path / "store"),
                "--scratch",
                str(tmp_path / "scratch"),
                "--name",
                "cuda",
                "--timeout",
                "120",
            ]
        )
        == 0
    )
    assert seen == {
        "reference": reference,
        "job_id": "b" * 32,
        "store": tmp_path / "store",
        "scratch": tmp_path / "scratch",
        "name": "cuda",
        "timeout_seconds": 120,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "success"


def test_registry_worker_real_cache_callback_binds_graph_not_manifest(tmp_path, monkeypatch):
    import io
    from contextlib import contextmanager
    from dataclasses import replace

    import test_layer_cache as cache_fixtures
    import test_oci_store as store_fixtures

    from palimpsest_local import layer_cache, oci_materializer, registry_intake
    from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS
    from palimpsest_local.oci_store import DerivedLayerOccurrence, DerivedSquashFSKey

    layout, pin, _ = cache_fixtures._layout(tmp_path / "layout", repeat=True)
    store = _store(tmp_path)
    scratch = tmp_path / "scratch"
    packer = tmp_path / "packer"
    packer.write_bytes(b"pinned-test-packer")
    toolchain = store_fixtures._toolchain_capability()
    job_id = "c" * 32
    events = []

    def transfer(reference, roots, **kwargs):
        events.append("registry")
        snapshot = LocalLayoutSource(layout).snapshot(None, SourceCAS(roots.oci_source_cas))
        entry = {**snapshot.root.descriptor.to_dict(), "annotations": {"org.opencontainers.image.ref.name": "selected"}}
        index = roots.oci_source_cas / "index.json"
        index.write_text(json.dumps({"schemaVersion": 2, "manifests": [entry]}))
        index.chmod(0o600)
        return registry_intake.RegistryPullReceipt(
            reference,
            roots.oci_source_cas,
            "linux/amd64",
            snapshot.root.descriptor.digest,
            snapshot.manifest.descriptor.digest,
            snapshot.cas_id,
            snapshot.binding_digest,
        )

    def materialize(snapshot, ordinal, **kwargs):
        events.append(ordinal)
        assert snapshot.image.digest != pin
        assert not (store / "images" / "real-cache.json").exists()
        if ordinal:
            prefix = store / "jobs" / job_id / "layer-000000.json"
            assert prefix.stat().st_mode & 0o777 == 0o444
            assert json.loads(prefix.read_text())["ordinal"] == 0
        occurrence = DerivedLayerOccurrence.from_image(snapshot, ordinal)
        key = DerivedSquashFSKey.for_occurrence(
            occurrence,
            intake_policy_id=layer_cache.LAYER_INTAKE_POLICY_ID,
            intake_policy_fingerprint=layer_cache.DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
            pack_policy_id=layer_cache.SQUASHFS_PACK_POLICY_ID,
            pack_policy_fingerprint=kwargs["pack_profile"].policy.fingerprint,
            toolchain=toolchain,
        )

        @contextmanager
        def produce():
            image = store_fixtures._squashfs()
            intake, packed_receipt = store_fixtures._receipts(occurrence, image)
            packed_receipt = replace(packed_receipt, policy_fingerprint=kwargs["pack_profile"].policy.fingerprint)
            packed = store_fixtures.LeasedSquashFS(io.BytesIO(image), packed_receipt)
            try:
                yield intake, packed
            finally:
                failure = packed._close()
                if failure is not None:
                    raise failure

        return kwargs["store"].materialize_observed(occurrence, key, produce)

    class Cache(layer_cache.LayerCache):
        def materialize(self, root_digest, **kwargs):
            return super().materialize(root_digest, packer_path=packer, **kwargs)

    class RootOS:
        # Root is a worker prerequisite only. Keep the real cache's descriptor
        # ownership checks bound to the actual test UID, not a globally faked os.
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def geteuid():
            return 0

        @staticmethod
        def fstat(fd):
            metadata = list(os.fstat(fd))
            metadata[4] = 0
            return os.stat_result(metadata)

    monkeypatch.setattr(worker, "os", RootOS())
    # Spoof only this module's admission guard, not other modules' syscall selection.
    monkeypatch.setattr(worker, "sys", SimpleNamespace(**{**vars(worker.sys), "platform": "linux"}))
    monkeypatch.setattr(worker.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(worker, "require_ceph_store", lambda selected: None)
    monkeypatch.setattr(worker, "LayerCache", Cache)
    monkeypatch.setattr(registry_intake, "pull_anonymous_oci_layout", transfer)
    monkeypatch.setattr(layer_cache, "discover_squashfs_toolchain", lambda *args, **kwargs: toolchain)
    monkeypatch.setattr(oci_materializer, "materialize_layer_hard", materialize)
    result = worker.pull(
        reference=f"registry.example.com/team/image@{pin}",
        store=store,
        scratch=scratch,
        name="real-cache",
        timeout_seconds=60,
        job_id=job_id,
    )
    assert events == ["registry", 0, 1]
    assert result["source"]["root_digest"] == pin
    assert result["source"]["image_digest"] != pin
    assert result["publication"] == ["new", "existing"]
    assert len(list((store / "blobs" / "sha256").iterdir())) == 1
    for layer in result["layers"]:
        metadata = json.loads((store / layer["receipt"]).read_text())
        assert metadata["conversion"]["source_image_digest"] == result["source"]["image_digest"]
    assert not (scratch / "intake.hold.json").exists()


def test_registry_publication_consumes_original_deadline_without_image_success(registry_worker, monkeypatch):
    from types import SimpleNamespace

    state = registry_worker
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: 100.0))
    real_publish = worker.publish_blob

    def slow_publish(*args):
        result = real_publish(*args)
        clock.now = 61.0
        return result

    monkeypatch.setattr(worker, "publish_blob", slow_publish)
    with pytest.raises(worker.WorkerError, match="deadline expired"):
        _pull_registry(state)
    assert ("convert", 1) not in state["events"]
    journal = state["store"] / "jobs" / state["job_id"]
    assert (journal / "layer-000000.json").exists()
    assert json.loads((journal / "failure.json").read_text())["completed_layers"] == 1
    assert not (state["store"] / "images" / "cuda.json").exists()


@pytest.mark.parametrize("fault", ["pin", "platform", "config-bytes", "diff-id"])
def test_registry_source_contradictions_fail_before_conversion(registry_worker, fault):
    state = registry_worker
    image = state["imported"]["image"]
    if fault == "pin":
        state["imported"]["root_digest"] = "sha256:" + "f" * 64
    elif fault == "platform":
        image["platform"]["architecture"] = "arm64"
    elif fault == "config-bytes":
        digest = image["config_descriptor"]["digest"]
        path = next(Path(path) for path in state["imported"]["source_blob_paths"] if Path(path).name == digest[7:])
        path.write_bytes(b"different original config")
    else:
        image["layers"][0]["diff_id"] = "sha256:" + "f" * 64
    with pytest.raises(worker.WorkerError):
        _pull_registry(state)
    assert ("convert", 0) not in state["events"]
    assert list((state["store"] / "blobs" / "sha256").iterdir()) == []
    assert not (state["store"] / "images" / "cuda.json").exists()


@pytest.mark.parametrize("failure", ["cache", "registry"])
def test_deterministic_acquisition_failure_does_not_quarantine_worker(registry_worker, monkeypatch, failure):
    from palimpsest_local.layer_cache import LayerCacheError

    state = registry_worker
    error_type = LayerCacheError if failure == "cache" else worker.RegistryIntakeError

    def rejected(self, reference, **kwargs):
        raise error_type("registry rejected requested source")

    monkeypatch.setattr(worker.LayerCache, "pull", rejected)
    with pytest.raises(error_type, match="rejected requested source"):
        _pull_registry(state)
    assert not (state["scratch"] / "intake.hold.json").exists()
    journal = state["store"] / "jobs" / state["job_id"]
    assert json.loads((journal / "failure.json").read_text())["status"] == "failed"
    assert ("convert", 0) not in state["events"]
    assert not (state["store"] / "images" / "cuda.json").exists()
