"""Actual SQLite/private-filesystem persistence and failure boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest

from palimpsest_local.local_catalog import (
    CatalogError,
    open_catalog,
    prepare_artifact_observation,
    prepare_build_observation,
    prepare_run_observation,
    quiesce_catalog,
    validate_catalog_text,
    validate_catalog_timing,
)
from palimpsest_local.oci_provenance import canonical_json_bytes
from palimpsest_local.package_reference import LocalPackageReference
from palimpsest_local.state import StatePaths


def _digest(character: str) -> str:
    return "sha256:" + character * 64


@pytest.fixture
def roots(tmp_path: Path) -> StatePaths:
    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    return StatePaths(config=tmp_path.resolve() / "config", state=state)


def _image() -> dict:
    manifest = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": _digest("1"), "size": 200}
    image = {
        "image_id": _digest("2"),
        "root_descriptor": manifest.copy(),
        "manifest_descriptor": manifest.copy(),
        "reference": {"registry": "registry.example", "repository": "original/image", "requested_reference": "latest"},
        "config_descriptor": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": _digest("3"),
            "size": 100,
        },
        "platform": {"os": "linux", "architecture": "arm64"},
        "layers": [
            {
                "ordinal": ordinal,
                "compressed": {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": _digest("4"),
                    "size": 50,
                },
                "diff_id": _digest("5"),
            }
            for ordinal in range(2)
        ],
        "source_cas_id": "source-cas-v1:" + "6" * 64,
        "source_kind": "oci-root",
        "derived": [],
        "materialization_complete": False,
    }
    _bind(image)
    return image


def _bind(image: dict) -> None:
    members = {
        "domain": "palimpsest.source-snapshot.v1",
        "cas_id": image["source_cas_id"],
        "image_digest": image["image_id"],
        "root": image["root_descriptor"],
        "manifest": image["manifest_descriptor"],
        "config": image["config_descriptor"],
        "layers": [layer["compressed"] for layer in image["layers"]],
    }
    image["source_binding_digest"] = "sha256:" + hashlib.sha256(canonical_json_bytes(members)).hexdigest()


def _derived(image: dict, ordinal: int) -> dict:
    return {
        "store_id": "oci-store-v1:" + "7" * 64,
        "occurrence_digest": _digest(str(ordinal + 8)),
        "record_digest": _digest("a"),
        "key_digest": _digest("b"),
        "source_snapshot_binding_digest": image["source_binding_digest"],
        "source_image_digest": image["image_id"],
        "ordinal": ordinal,
        "image_digest": _digest("c"),
        "image_size": 4096,
        "filesystem": "squashfs",
    }


def _observation(*, build_id: str = "build-1") -> dict:
    artifact = {
        "digest": _digest("c"),
        "kind": "squashfs",
        "tags": [{"tag": "stable", "digest": _digest("c")}],
        "base_digest": _digest("d"),
        "parent_digests": [_digest("e"), _digest("e")],
    }
    run = {
        "run_id": str(uuid.uuid4()),
        "name": "consumer",
        "runtime_kind": "oci-root",
        "backend": "lima-vz",
        "status": "stopped",
        "layers": [{"digest": _digest("c")}, {"digest": _digest("c")}],
        "source_image_digest": _digest("2"),
        "source_binding_digest": _image()["source_binding_digest"],
    }
    build = {
        "build_id": build_id,
        "engine": "buildkit",
        "status": "success",
        "output_digest": _digest("c"),
        "base_digest": _digest("d"),
        "parent_digests": [_digest("e"), _digest("e")],
        "output_tags": ["output:label", "output:label"],
        "started_at": "2026-11-04T00:00:00Z",
    }
    return {
        "artifacts": {
            "artifacts": [artifact],
            "images": [],
            "layers": [artifact],
            "unknown": [],
            "client": {"system": "Darwin", "arch": "arm64"},
        },
        "builds": [build],
        "runs": [run],
        "warnings": [],
        "captured_at": "2026-11-04T00:00:01Z",
    }


def _child(roots: StatePaths, body: str, *, timeout: float = 15) -> subprocess.CompletedProcess:
    log_root = roots.config.parent / "child-logs"
    log_root.mkdir(mode=0o700, exist_ok=True)
    code = (
        "from pathlib import Path\nfrom palimpsest_local.state import StatePaths\nfrom palimpsest_local.local_catalog import open_catalog, CatalogError\nimport json, sys, time\nroots=StatePaths(config=Path(sys.argv[1]),state=Path(sys.argv[2]))\n"
        + body
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(roots.config), str(roots.state)],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "PALIMPSEST_LOG_HOME": str(log_root),
        },
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_absent_selected_state_and_catalog_reads_do_not_initialize(tmp_path: Path, roots: StatePaths) -> None:
    absent = StatePaths(config=tmp_path.resolve() / "absent-config", state=tmp_path.resolve() / "absent-state")
    with open_catalog(absent) as catalog:
        assert catalog is None
    assert not absent.state.exists()
    assert not absent.config.exists()
    with open_catalog(roots, create=False) as catalog:
        assert catalog is None
    assert list(roots.state.iterdir()) == []


def test_order_repeated_source_occurrences_and_materialization_progress(roots: StatePaths) -> None:
    image = _image()
    with open_catalog(roots) as catalog:
        catalog.register_image(image)
        assert catalog.read_images()[0]["materialization_status"] == "source-only"
        image["derived"] = [_derived(image, 0)]
        catalog.register_image(image)
        assert catalog.read_images()[0]["materialization_status"] == "partial"
        image["derived"].append(_derived(image, 1))
        image["materialization_complete"] = True
        catalog.register_image(image)
        catalog.register_image(_image())
        result = catalog.read_images()[0]
        assert result["layers"] == image["layers"]
        assert result["derived"] == image["derived"]
        assert result["materialization_status"] == "complete"
        assert "base_digest" not in result
    with sqlite3.connect(roots.metadata_db) as connection:
        assert connection.execute(
            "SELECT ordinal,compressed_digest FROM source_layers ORDER BY ordinal"
        ).fetchall() == [(0, _digest("4")), (1, _digest("4"))]
        assert connection.execute("SELECT ordinal,image_digest FROM derived_layers ORDER BY ordinal").fetchall() == [
            (0, _digest("c")),
            (1, _digest("c")),
        ]


def test_registration_rejects_conflict_or_false_completeness_without_partial_commit(roots: StatePaths) -> None:
    image = _image()
    with open_catalog(roots) as catalog:
        catalog.register_image(image)
        changed = copy.deepcopy(image)
        changed["layers"][1]["diff_id"] = _digest("f")
        with pytest.raises(CatalogError, match="immutable"):
            catalog.register_image(changed)
        changed = copy.deepcopy(image)
        changed["materialization_complete"] = True
        with pytest.raises(CatalogError, match="complete"):
            catalog.register_image(changed)
        changed = copy.deepcopy(image)
        changed["layers"][1]["ordinal"] = 0
        with pytest.raises(CatalogError, match="ordinal"):
            catalog.register_image(changed)
        changed = copy.deepcopy(image)
        changed["source_binding_digest"] = _digest("f")
        with pytest.raises(CatalogError, match="binding"):
            catalog.register_image(changed)
        assert catalog.read_images()[0]["layers"] == image["layers"]
        assert catalog.read_images()[0]["derived"] == []


def test_separate_process_reopens_committed_snapshot_and_graph(roots: StatePaths) -> None:
    observation = _observation()
    with open_catalog(roots) as catalog:
        catalog.register_image(_image())
        assert catalog.replace_observation(**observation) == 1
        identity = catalog.read_observation()["catalog_id"]
    result = _child(roots, "with open_catalog(roots,create=False) as c:\n print(json.dumps(c.read_snapshot()))\n")
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    assert snapshot["observation"]["catalog_id"] == identity
    assert snapshot["observation"]["revision"] == 1
    assert snapshot["builds"] == observation["builds"]
    assert snapshot["runs"] == observation["runs"]
    assert snapshot["images"][0]["layers"] == _image()["layers"]
    assert not roots.config.exists()
    assert {path.name for path in roots.catalog.iterdir()} == {"metadata.sqlite3", "writer.lock"}
    assert roots.catalog.stat().st_mode & 0o777 == 0o700
    assert roots.metadata_db.stat().st_mode & 0o777 == 0o600


def test_failed_snapshot_rolls_back_all_relations_and_revision(roots: StatePaths) -> None:
    before = _observation()
    with open_catalog(roots) as catalog:
        catalog.register_image(_image())
        catalog.replace_observation(**before)
        failed = _observation(build_id="replacement")
        failed["builds"].append(failed["builds"][0].copy())
        with pytest.raises(CatalogError, match="transaction failed"):
            catalog.replace_observation(**failed)
        assert catalog.read_observation()["revision"] == 1
        assert catalog.read_builds() == before["builds"]
        assert catalog.read_runs() == before["runs"]
        assert len(catalog.read_images()) == 1
    with sqlite3.connect(roots.metadata_db) as connection:
        assert connection.execute("SELECT ordinal,digest FROM build_parents ORDER BY ordinal").fetchall() == [
            (0, _digest("e")),
            (1, _digest("e")),
        ]
        assert connection.execute("SELECT label FROM build_labels ORDER BY ordinal").fetchall() == [
            ("output:label",),
            ("output:label",),
        ]
        assert connection.execute("SELECT name FROM aliases").fetchall() == [("stable",)]
        assert connection.execute("SELECT count(*) FROM run_images").fetchone()[0] == 1
        assert connection.execute("SELECT role FROM build_artifacts").fetchall() == [("output",)]


def test_idle_collection_has_no_sql_transaction_and_external_reader_gets_atomic_snapshots(roots: StatePaths) -> None:
    before = _observation()
    after = _observation(build_id="second")
    with open_catalog(roots) as catalog:
        catalog.replace_observation(**before)
        with sqlite3.connect(roots.metadata_db, timeout=0.2, isolation_level=None) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT build_id FROM builds").fetchone()[0] == "build-1"
            reader.execute("COMMIT")
            # An idle catalog handle must not hold an implicit read/write transaction.
            reader.execute("BEGIN IMMEDIATE")
            reader.execute("ROLLBACK")
        catalog.replace_observation(**after)
        with sqlite3.connect(roots.metadata_db) as reader:
            assert reader.execute("SELECT build_id FROM builds").fetchall() == [("second",)]
            assert reader.execute("SELECT revision FROM catalog").fetchone()[0] == 2


@pytest.mark.parametrize("mutation", ["corrupt", "version", "application", "extra-table", "trigger"])
def test_corrupt_or_unknown_schema_is_visible_and_never_rebuilt(roots: StatePaths, mutation: str) -> None:
    with open_catalog(roots) as catalog:
        catalog.register_image(_image())
    if mutation == "corrupt":
        roots.metadata_db.write_bytes(b"not a SQLite database")
    else:
        with sqlite3.connect(roots.metadata_db) as connection:
            statements = {
                "version": "PRAGMA user_version=99",
                "application": "PRAGMA application_id=99",
                "extra-table": "CREATE TABLE unexpected(x)",
                "trigger": "CREATE TRIGGER unexpected AFTER INSERT ON images BEGIN DELETE FROM images; END",
            }
            connection.execute(statements[mutation])
    before = roots.metadata_db.read_bytes()
    with pytest.raises(CatalogError):
        with open_catalog(roots, create=False):
            pytest.fail("unsafe metadata was accepted")
    assert roots.metadata_db.read_bytes() == before


@pytest.mark.parametrize("entry", ["state", "directory", "database", "lock", "journal"])
def test_symlinks_are_refused_without_following_target(roots: StatePaths, entry: str, tmp_path: Path) -> None:
    with open_catalog(roots):
        pass
    path = {
        "state": roots.state,
        "directory": roots.catalog,
        "database": roots.metadata_db,
        "lock": roots.catalog / "writer.lock",
        "journal": roots.catalog / "metadata.sqlite3-journal",
    }[entry]
    target = tmp_path.resolve() / ("target-" + entry)
    if path.exists():
        path.rename(target)
    else:
        target.write_bytes(b"preserved")
        target.chmod(0o600)
    path.symlink_to(target, target_is_directory=target.is_dir())
    with pytest.raises(CatalogError):
        with open_catalog(roots):
            pytest.fail("unsafe symlink was accepted")
    assert path.is_symlink()
    assert target.exists()


@pytest.mark.parametrize("entry", ["database", "lock", "journal"])
def test_hardlinks_and_public_modes_are_refused(roots: StatePaths, entry: str, tmp_path: Path) -> None:
    with open_catalog(roots):
        pass
    path = {
        "database": roots.metadata_db,
        "lock": roots.catalog / "writer.lock",
        "journal": roots.catalog / "metadata.sqlite3-journal",
    }[entry]
    if not path.exists():
        path.touch(mode=0o600)
    os.link(path, tmp_path / ("linked-" + entry))
    with pytest.raises(CatalogError):
        with open_catalog(roots):
            pytest.fail("hardlinked metadata was accepted")
    (tmp_path / ("linked-" + entry)).unlink()
    path.chmod(0o644)
    with pytest.raises(CatalogError):
        with open_catalog(roots):
            pytest.fail("public metadata was accepted")


def test_replaced_identity_is_refused_on_use_and_close(roots: StatePaths) -> None:
    with pytest.raises(CatalogError, match="identity changed"):
        with open_catalog(roots) as catalog:
            roots.metadata_db.rename(roots.catalog / "old-database")
            roots.metadata_db.touch(mode=0o600)
            catalog.read_observation()


def test_protected_state_is_not_persisted(roots: StatePaths) -> None:
    observation = _observation()
    observation["runs"][0].update(
        env={"PASSWORD": "private-value"}, ssh={"identity": "/private/key"}, config={"token": "private-value"}
    )
    observation["builds"][0]["definition"] = "RUN private-value"
    with open_catalog(roots) as catalog:
        catalog.replace_observation(**observation)
        run = catalog.read_runs()[0]
        assert not {"env", "ssh", "config"} & run.keys()
        assert "definition" not in catalog.read_builds()[0]
    assert b"private-value" not in roots.metadata_db.read_bytes()


def test_writer_lock_times_out_and_parallel_process_writes_are_serialized(roots: StatePaths) -> None:
    with open_catalog(roots):
        started = time.monotonic()
        result = _child(
            roots,
            "try:\n with open_catalog(roots,timeout_seconds=0.1): pass\nexcept CatalogError as exc:\n print(str(exc))\n sys.exit(23)\n",
        )
        assert result.returncode == 23, result.stderr
        assert "timed out" in result.stdout
        assert time.monotonic() - started < 5
    body = "with open_catalog(roots,timeout_seconds=3) as c:\n time.sleep(0.05)\n c.replace_observation(artifacts={'artifacts':[]},builds=[],runs=[],warnings=[],captured_at='now')\n"
    code = (
        "from pathlib import Path\nfrom palimpsest_local.state import StatePaths\nfrom palimpsest_local.local_catalog import open_catalog\nimport time, sys\nroots=StatePaths(config=Path(sys.argv[1]),state=Path(sys.argv[2]))\n"
        + body
    )
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(roots.config), str(roots.state)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for child in children:
            _, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    with open_catalog(roots, create=False) as catalog:
        assert catalog.read_observation()["revision"] == 2


def test_quiescent_copy_requires_explicit_rebinding_and_drops_nontransferable_facts(
    roots: StatePaths, tmp_path: Path
) -> None:
    with open_catalog(roots) as catalog:
        catalog.register_image(_image())
        catalog.replace_observation(**_observation())
        source_id = catalog.read_observation()["catalog_id"]
    destination = StatePaths(config=roots.config, state=tmp_path.resolve() / "moved")
    with quiesce_catalog(roots):
        shutil.copytree(roots.state, destination.state)
        with pytest.raises(CatalogError, match="different selected state"):
            with open_catalog(destination, create=False):
                pytest.fail("copied root was implicitly trusted")
        with open_catalog(destination, create=False, _rebind_from=roots.state) as catalog:
            catalog.rebind_destination(source_state=roots.state)
            moved = catalog.read_images()[0]
            assert moved["layers"] == _image()["layers"]
            assert moved["source_cas_id"] is None
            assert moved["source_binding_verified"] is False
            assert moved["materialization_status"] == "source-unavailable"
            assert moved["derived"] == []
            assert moved["materialization_complete"] is False
            rebound = _image()
            rebound["source_cas_id"] = "source-cas-v1:" + "a" * 64
            _bind(rebound)
            catalog.register_image(rebound)
            assert catalog.read_images()[0]["source_binding_verified"] is True
            assert catalog.read_images()[0]["source_cas_id"] == rebound["source_cas_id"]
            assert catalog.read_builds() == []
            assert catalog.read_observation()["catalog_id"] != source_id
            assert catalog.read_observation()["revision"] == 0
    with open_catalog(roots, create=False) as catalog:
        assert catalog.read_observation()["catalog_id"] == source_id
        assert len(catalog.read_images()) == 1
    with quiesce_catalog(destination):
        shutil.rmtree(destination.state)
    assert not destination.state.exists()


@pytest.mark.parametrize(("entry", "mode"), [("state", 0o770), ("catalog", 0o755)])
def test_unsafe_directories_are_refused_without_permission_repair(roots: StatePaths, entry: str, mode: int) -> None:
    with open_catalog(roots):
        pass
    path = roots.state if entry == "state" else roots.catalog
    path.chmod(mode)
    with pytest.raises(CatalogError):
        with open_catalog(roots):
            pytest.fail("unsafe directory was accepted")
    assert path.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize("mode", [0o711, 0o755])
def test_shared_traversal_state_permissions_are_preserved(roots: StatePaths, mode: int) -> None:
    roots.state.chmod(mode)
    with open_catalog(roots) as catalog:
        catalog.register_image(_image())
    with open_catalog(roots, create=False) as catalog:
        assert len(catalog.read_images()) == 1
    assert roots.state.stat().st_mode & 0o777 == mode
    assert roots.catalog.stat().st_mode & 0o777 == 0o700
    assert roots.metadata_db.stat().st_mode & 0o777 == 0o600


def test_verified_source_cas_change_never_transfers_completed_derived_metadata(roots: StatePaths) -> None:
    image = _image()
    image["derived"] = [_derived(image, 0), _derived(image, 1)]
    image["materialization_complete"] = True
    with open_catalog(roots) as catalog:
        catalog.register_image(image)
        changed = copy.deepcopy(image)
        changed["source_cas_id"] = "source-cas-v1:" + "f" * 64
        _bind(changed)
        changed["derived"] = []
        changed["materialization_complete"] = False
        with pytest.raises(CatalogError, match="immutable"):
            catalog.register_image(changed)
        persisted = catalog.read_images()[0]
        assert persisted["source_cas_id"] == image["source_cas_id"]
        assert persisted["source_binding_digest"] == image["source_binding_digest"]
        assert persisted["materialization_status"] == "complete"
        assert persisted["derived"] == image["derived"]


def test_native_package_aliases_are_relational_and_not_build_output_labels(roots: StatePaths) -> None:
    observation = _observation()
    reference = LocalPackageReference(
        reference="packages.example/team/image:latest",
        authority="packages.example",
        api_base="https://packages.example/api/v1",
        namespace="team",
        package="image",
        archive=str(roots.state / "original.oci.tar"),
        archive_digest=_digest("f"),
        archive_size_bytes=100,
        root_digest=_digest("c"),
        package_type="oci-image",
        build_id="build-1",
    )
    artifact = observation["artifacts"]["artifacts"][0]
    artifact["package_references"] = [asdict(reference)]
    with open_catalog(roots) as catalog:
        catalog.replace_observation(**observation)
    with open_catalog(roots, create=False) as catalog:
        assert catalog.read_artifacts()["artifacts"][0]["package_references"] == [asdict(reference)]
    with sqlite3.connect(roots.metadata_db) as connection:
        aliases = connection.execute("SELECT name,digest,kind,projection FROM aliases ORDER BY ordinal").fetchall()
        assert [row[:3] for row in aliases] == [
            ("stable", _digest("c"), "local-tag"),
            (reference.reference, reference.root_digest, "native-package-reference"),
        ]
        assert json.loads(aliases[1][3])["archive_digest"] == reference.archive_digest
        assert json.loads(aliases[1][3])["project_id"] is None
        assert connection.execute("SELECT label FROM build_labels ORDER BY ordinal").fetchall() == [
            ("output:label",),
            ("output:label",),
        ]
        assert all(row[0] != "output:label" for row in aliases)
    changed = copy.deepcopy(observation)
    changed["artifacts"]["artifacts"][0]["package_references"][0]["root_digest"] = _digest("a")
    with open_catalog(roots) as catalog:
        with pytest.raises(CatalogError, match="alias does not match"):
            catalog.replace_observation(**changed)
        assert catalog.read_observation()["revision"] == 1


def test_known_build_provenance_and_semantic_record_binding_survive_reopen(roots: StatePaths) -> None:
    observation = _observation()
    provenance = {
        "recipe_sha256": None,
        "build_key": _digest("1"),
        "context_digest": _digest("2"),
        "dockerfile_digest": _digest("3"),
        "source_record_digest": _digest("4"),
        "source_record_digest_kind": "canonical-json-semantic-v1",
    }
    observation["builds"][0].update(provenance)
    with open_catalog(roots) as catalog:
        catalog.replace_observation(**observation)
    with open_catalog(roots, create=False) as catalog:
        persisted = catalog.get_build("build-1")
        assert {name: persisted[name] for name in provenance} == provenance
    with sqlite3.connect(roots.metadata_db) as connection:
        assert connection.execute(
            "SELECT recipe_sha256,build_key,context_digest,dockerfile_digest,source_record_digest,source_record_digest_kind FROM build_provenance"
        ).fetchall() == [tuple(provenance.values())]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    invalid = copy.deepcopy(observation)
    invalid["builds"][0]["source_record_digest_kind"] = "raw-byte-attestation"
    with open_catalog(roots) as catalog:
        with pytest.raises(CatalogError, match="binding kind"):
            catalog.replace_observation(**invalid)
        assert catalog.get_build("build-1")["source_record_digest_kind"] == "canonical-json-semantic-v1"
        assert catalog.read_observation()["revision"] == 1


def test_configured_parent_alias_keeps_missing_state_absent_in_public_cli(tmp_path: Path) -> None:
    physical = tmp_path.resolve() / "physical"
    physical.mkdir(mode=0o700)
    alias = tmp_path.resolve() / "configured-parent"
    alias.symlink_to(physical, target_is_directory=True)
    selected = StatePaths(config=tmp_path.resolve() / "config", state=alias / "not-initialized")
    with open_catalog(selected) as catalog:
        assert catalog is None
    result = _child(
        selected,
        "import os\nos.environ['PALIMPSEST_STATE_HOME']=str(roots.state)\nos.environ['XDG_CONFIG_HOME']=str(roots.config)\nfrom palimpsest_local.cli import main\nsys.exit(main(['store','catalog']))\n",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["observation"]["catalog_id"] is None
    assert not (physical / "not-initialized").exists()
    assert not selected.config.exists()
    assert alias.is_symlink()


def test_configured_parent_alias_and_real_spelling_share_catalog_binding_in_public_cli(tmp_path: Path) -> None:
    physical = tmp_path.resolve() / "physical"
    physical.mkdir(mode=0o700)
    alias = tmp_path.resolve() / "configured-parent"
    alias.symlink_to(physical, target_is_directory=True)
    actual = StatePaths(config=tmp_path.resolve() / "config", state=physical / "state")
    actual.state.mkdir(mode=0o711)
    selected = StatePaths(config=actual.config, state=alias / "state")
    with open_catalog(selected) as catalog:
        catalog.register_image(_image())
        identity = catalog.read_observation()["catalog_id"]
    for spelling in (selected, actual):
        result = _child(
            spelling,
            "import os\nos.environ['PALIMPSEST_STATE_HOME']=str(roots.state)\nos.environ['XDG_CONFIG_HOME']=str(roots.config)\nfrom palimpsest_local.cli import main\nsys.exit(main(['store','catalog']))\n",
        )
        assert result.returncode == 0, result.stderr
        snapshot = json.loads(result.stdout)
        assert snapshot["observation"]["catalog_id"] == identity
        assert snapshot["images"][0]["image_id"] == _image()["image_id"]
    assert actual.state.stat().st_mode & 0o777 == 0o711
    assert alias.is_symlink()
    assert not actual.config.exists()


@pytest.mark.parametrize("failure", ["connect", "mid-ddl", "directory-fsync"])
def test_failed_initialization_removes_only_owned_unpublished_database_and_retries(
    roots: StatePaths, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    output = roots.state / "producer-output"
    output.write_bytes(b"independently committed producer output")
    real_connect = sqlite3.connect
    real_fsync = os.fsync

    class FailedDDLConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            result = super().execute(sql, parameters)
            if sql.startswith("CREATE TABLE source_layers"):
                raise sqlite3.OperationalError("injected after actual DDL")
            return result

    with monkeypatch.context() as patch:
        if failure == "connect":

            def fail_connect(*args, **kwargs):
                raise sqlite3.OperationalError("injected connection failure")

            patch.setattr(sqlite3, "connect", fail_connect)
        elif failure == "mid-ddl":
            patch.setattr(
                sqlite3, "connect", lambda *args, **kwargs: real_connect(*args, factory=FailedDDLConnection, **kwargs)
            )
        else:
            failed = False

            def fail_fsync_once(fd):
                nonlocal failed
                if roots.metadata_db.exists() and not failed:
                    failed = True
                    raise OSError("injected directory durability failure")
                return real_fsync(fd)

            patch.setattr(os, "fsync", fail_fsync_once)
        with pytest.raises(CatalogError):
            with open_catalog(roots):
                pytest.fail("failed initialization yielded a catalog")
    assert not roots.metadata_db.exists()
    assert not (roots.catalog / "metadata.sqlite3-journal").exists()
    assert output.read_bytes() == b"independently committed producer output"
    with open_catalog(roots) as catalog:
        assert catalog.read_observation()["revision"] == 0
        catalog.register_image(_image())
    with open_catalog(roots, create=False) as catalog:
        assert len(catalog.read_images()) == 1


def test_failed_initialization_does_not_remove_a_replaced_database(
    roots: StatePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    preserved = roots.state / "original-fresh-inode"

    def replace_before_connect(*args, **kwargs):
        roots.metadata_db.rename(preserved)
        roots.metadata_db.write_bytes(b"replacement belongs to another operation")
        roots.metadata_db.chmod(0o600)
        raise sqlite3.OperationalError("injected replacement")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", replace_before_connect)
        with pytest.raises(CatalogError, match="cleanup"):
            with open_catalog(roots):
                pytest.fail("changed database identity was accepted")
    assert roots.metadata_db.read_bytes() == b"replacement belongs to another operation"
    assert preserved.exists()


def test_existing_empty_database_is_not_mistaken_for_owned_failed_initialization(roots: StatePaths) -> None:
    roots.catalog.mkdir(mode=0o700)
    (roots.catalog / "writer.lock").touch(mode=0o600)
    roots.metadata_db.touch(mode=0o600)
    original = roots.metadata_db.stat()
    with pytest.raises(CatalogError, match="schema"):
        with open_catalog(roots):
            pytest.fail("preexisting unknown database was repaired")
    assert roots.metadata_db.read_bytes() == b""
    assert roots.metadata_db.stat().st_ino == original.st_ino


def test_fresh_database_failure_preserves_preexisting_journal(roots: StatePaths) -> None:
    roots.catalog.mkdir(mode=0o700)
    (roots.catalog / "writer.lock").touch(mode=0o600)
    journal = roots.catalog / "metadata.sqlite3-journal"
    journal.write_bytes(b"preexisting companion must not be recovered or discarded")
    journal.chmod(0o600)
    original = journal.stat()
    with pytest.raises(CatalogError, match="preexisting journal"):
        with open_catalog(roots):
            pytest.fail("a preexisting companion was accepted for a fresh database")
    assert not roots.metadata_db.exists()
    assert journal.read_bytes() == b"preexisting companion must not be recovered or discarded"
    assert journal.stat().st_ino == original.st_ino


def test_unicode_projection_is_bounded_by_utf8_bytes_not_ascii_escape_expansion(roots: StatePaths) -> None:
    observation = _observation()
    description = "界" * 180_000
    observation["artifacts"]["artifacts"][0]["description"] = description
    with open_catalog(roots) as catalog:
        catalog.replace_observation(**observation)
    with open_catalog(roots, create=False) as catalog:
        assert catalog.read_artifacts()["artifacts"][0]["description"] == description
    with sqlite3.connect(roots.metadata_db) as connection:
        text_size, byte_size, projection = connection.execute(
            "SELECT length(projection),length(CAST(projection AS BLOB)),projection FROM artifacts"
        ).fetchone()
        assert text_size < byte_size < 1024 * 1024
        assert "界" in projection
        assert "\\u754c" not in projection
    oversized = copy.deepcopy(observation)
    oversized["artifacts"]["artifacts"][0]["description"] = "界" * 350_000
    with open_catalog(roots) as catalog:
        with pytest.raises(CatalogError, match="bounded projection"):
            catalog.replace_observation(**oversized)
        assert catalog.read_observation()["revision"] == 1
        assert catalog.read_artifacts()["artifacts"][0]["description"] == description


@pytest.mark.parametrize("invalid", ["a" * 4097, "nul\x00label", "invalid\ud800label"])
def test_shared_text_rule_rejects_invalid_labels_without_losing_previous_sql_snapshot(
    roots: StatePaths, invalid: str
) -> None:
    valid = "界" * 4096
    assert validate_catalog_text(valid) == valid
    observation = _observation()
    observation["builds"][0]["output_tags"] = [valid]
    with open_catalog(roots) as catalog:
        catalog.replace_observation(**observation)
        with pytest.raises(CatalogError):
            validate_catalog_text(invalid)
        invalid_observation = copy.deepcopy(observation)
        invalid_observation["builds"][0]["output_tags"] = [invalid]
        with pytest.raises(CatalogError):
            catalog.replace_observation(**invalid_observation)
        assert catalog.read_observation()["revision"] == 1
        assert catalog.get_build("build-1")["output_tags"] == [valid]
    with sqlite3.connect(roots.metadata_db) as connection:
        assert connection.execute("SELECT label FROM build_labels").fetchall() == [(valid,)]


@pytest.mark.parametrize("malformed", ["kind-nul", "kind-long", "protected-timing", "run-name", "projection-limit"])
def test_shared_entry_admission_rejects_malformed_facts_before_sql_and_preserves_snapshot(
    roots: StatePaths, malformed: str
) -> None:
    observation = _observation()
    bad = copy.deepcopy(observation)
    if malformed in ("kind-nul", "kind-long", "projection-limit"):
        entry = bad["artifacts"]["artifacts"][0]
        helper = prepare_artifact_observation
        if malformed == "projection-limit":
            entry["description"] = "界" * 350_000
        else:
            entry["kind"] = "invalid\x00kind" if malformed == "kind-nul" else "x" * 4097
    elif malformed == "protected-timing":
        entry = bad["builds"][0]
        entry["timings_ms"] = {"private_key": 1}
        helper = prepare_build_observation
    else:
        entry = bad["runs"][0]
        entry["name"] = "invalid\x00run"
        helper = prepare_run_observation
    with pytest.raises(CatalogError):
        helper(entry)
    with open_catalog(roots, timeout_seconds=0.1) as catalog:
        catalog.replace_observation(**observation)
        with sqlite3.connect(roots.metadata_db, isolation_level=None) as external:
            external.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(CatalogError) as error:
                    catalog.replace_observation(**bad)
                # A reserved SQLite writer must not mask admission as a busy SQL transaction.
                assert "transaction failed" not in str(error.value)
            finally:
                external.execute("ROLLBACK")
        assert catalog.read_observation()["revision"] == 1
        assert catalog.read_builds() == observation["builds"]
        assert catalog.read_runs() == observation["runs"]


def test_timing_admission_keeps_large_finite_integer_without_inventing_a_quota(roots: StatePaths) -> None:
    large = 10**1000
    assert validate_catalog_timing("total", large) == ("total", large)
    observation = _observation()
    observation["builds"][0]["timings_ms"] = {"total": large}
    admitted = prepare_build_observation(observation["builds"][0])
    with open_catalog(roots) as catalog:
        catalog.replace_observation(**observation)
    with open_catalog(roots, create=False) as catalog:
        assert catalog.get_build("build-1")["timings_ms"] == admitted["timings_ms"]
    for name, value in (("private_key", 1), ("total", True), ("total", float("inf")), ("invalid\x00name", 1)):
        with pytest.raises(CatalogError):
            validate_catalog_timing(name, value)
