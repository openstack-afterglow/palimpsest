"""Unit tests for palimpsest_local.state."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import palimpsest_local.state as state
from palimpsest_local.errors import StateError


def test_xdg_roots_and_permissions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {
            "XDG_CONFIG_HOME": str(tmppath / "cfg"),
            "XDG_STATE_HOME": str(tmppath / "st"),
        }
        roots = state.init_roots(env)
        assert roots.config == tmppath / "cfg" / "palimpsest"
        assert roots.state == tmppath / "st" / "palimpsest"

        assert state.permission_bits(roots.config) == 0o700
        assert state.permission_bits(roots.state) == 0o700
        assert state.permission_bits(roots.runs) == 0o700
        assert state.permission_bits(roots.locks) == 0o700
        assert state.permission_bits(roots.transfers) == 0o700
        assert state.permission_bits(roots.tags) == 0o700
        assert state.permission_bits(roots.builds) == 0o700


def test_run_paths_and_owner_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)

        rpaths = state.run_paths(roots, "my-run")
        assert rpaths.root.resolve() == (roots.runs / "my-run").resolve()

        owner = state.write_owner_record(rpaths)
        assert owner.name == "my-run"
        assert owner.schema_version == 1
        assert owner.run_id != ""
        assert state.permission_bits(rpaths.owner) == 0o600

        # Owner record is immutable
        with pytest.raises(StateError, match="immutable"):
            state.write_owner_record(rpaths)

        read_owner = state.read_owner_record(rpaths)
        assert read_owner == owner


def test_run_state_and_locks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)
        rpaths = state.run_paths(roots, "my-run")
        state.write_owner_record(rpaths)

        with state.locked(rpaths):
            st_data = state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.10"})
            assert st_data["status"] == "running"
            assert st_data["guest_ip"] == "192.168.122.10"

        read_st = state.read_run_state(rpaths)
        assert read_st["status"] == "running"
        assert read_st["guest_ip"] == "192.168.122.10"


def test_secret_scanning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)
        rpaths = state.run_paths(roots, "my-run")
        state.write_owner_record(rpaths)

        with pytest.raises(StateError, match="secret-shaped field"):
            state.write_run_state(rpaths, status="running", data={"auth_token": "secret123"})

        with pytest.raises(StateError, match="key-material-shaped string"):
            state.atomic_write_json(rpaths.root / "key.json", {"key": "-----BEGIN PRIVATE KEY-----\nabc\n"})


def test_traversal_and_name_rejection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)

        with pytest.raises(StateError, match="invalid run name"):
            state.run_paths(roots, "../escape")

        with pytest.raises(StateError, match="invalid tag name"):
            state.tag_path(roots, "../../etc/passwd")


def test_tag_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)
        digest = "sha256:" + "a" * 64

        tag_rec = state.TagRecord(
            schema_version=1,
            tag="v1.0",
            digest=digest,
            media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            size_bytes=1024,
            parent_digest=None,
            base_image_digest=None,
            source="pack",
            created_at=state.utc_now_iso(),
        )

        state.write_tag_record(roots, tag_rec)
        read_tag = state.read_tag_record(roots, "v1.0")
        assert read_tag == tag_rec

        # Idempotent write with same identity succeeds
        state.write_tag_record(roots, tag_rec)

        # Conflicting write fails
        conflicting = state.TagRecord(
            schema_version=1,
            tag="v1.0",
            digest="sha256:" + "b" * 64,
            media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            size_bytes=1024,
            parent_digest=None,
            base_image_digest=None,
            source="pack",
            created_at=state.utc_now_iso(),
        )
        with pytest.raises(StateError, match="already maps to a different digest"):
            state.write_tag_record(roots, conflicting)


def test_transfer_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        env = {"XDG_CONFIG_HOME": str(tmppath / "cfg"), "XDG_STATE_HOME": str(tmppath / "st")}
        roots = state.init_roots(env)
        digest = "sha256:" + "c" * 64

        t_rec = state.TransferRecord(
            schema_version=1,
            digest=digest,
            path_fingerprint="fp123",
            session_id="sess456",
            acknowledged_offset=512,
            updated_at=state.utc_now_iso(),
        )

        state.write_transfer_record(roots, t_rec)
        read_t = state.read_transfer_record(roots, digest)
        assert read_t == t_rec

        records = state.list_transfer_records(roots)
        assert len(records) == 1
        assert records[0] == t_rec

        state.delete_transfer_record(roots, digest)
        with pytest.raises(StateError, match="state file not found"):
            state.read_transfer_record(roots, digest)
