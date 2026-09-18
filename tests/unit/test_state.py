"""Unit tests for palimpsest_local.state."""

from __future__ import annotations

import tempfile
import threading
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
        assert state.permission_bits(roots.build_cache) == 0o700
        assert state.permission_bits(roots.runtime_packs) == 0o700
        assert state.permission_bits(roots.projects) == 0o700
        assert state.permission_bits(roots.volumes) == 0o700


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


def test_project_paths_are_scoped_and_traversal_safe(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    project = state.project_paths(roots, "shop")

    assert project.root == roots.projects / "shop"
    assert project.volumes == roots.volumes / "shop"
    assert project.lock == roots.locks / "project-shop.lock"
    with pytest.raises(StateError, match="invalid project name"):
        state.project_paths(roots, "../escape")


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


def test_atomic_write_json_syncs_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state" / "record.json"
    synced: list[Path] = []
    monkeypatch.setattr(state, "fsync_directory", lambda path: synced.append(path))

    state.atomic_write_json(target, {"status": "complete"})

    assert synced == [target.parent]
    assert state.read_json(target) == {"status": "complete"}


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


def test_conflicting_tag_writers_are_serialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st")})
    first_entered_write = threading.Event()
    release_first_write = threading.Event()
    original_atomic_write = state.atomic_write_json

    def record(digest_character: str) -> state.TagRecord:
        return state.TagRecord(
            schema_version=1,
            tag="concurrent",
            digest="sha256:" + digest_character * 64,
            media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            size_bytes=1024,
            parent_digest=None,
            base_image_digest=None,
            source="test",
            created_at=state.utc_now_iso(),
        )

    def delayed_atomic_write(path: Path, value: dict[str, object]) -> None:
        if path == state.tag_path(roots, "concurrent") and value.get("digest") == "sha256:" + "a" * 64:
            first_entered_write.set()
            assert release_first_write.wait(5)
        original_atomic_write(path, value)

    monkeypatch.setattr(state, "atomic_write_json", delayed_atomic_write)
    outcomes: list[str] = []

    def write(item: state.TagRecord) -> None:
        try:
            state.write_tag_record(roots, item)
            outcomes.append("success")
        except StateError:
            outcomes.append("conflict")

    first = threading.Thread(target=write, args=(record("a"),))
    second = threading.Thread(target=write, args=(record("b"),))
    first.start()
    assert first_entered_write.wait(5)
    second.start()
    release_first_write.set()
    first.join(5)
    second.join(5)

    assert outcomes.count("success") == 1
    assert outcomes.count("conflict") == 1
    assert state.read_tag_record(roots, "concurrent").digest in {
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    }


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


def test_state_root_precedence_and_source_reporting(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    default_st = tmp_path / "default_st"
    config_st = tmp_path / "config_st"
    env_st = tmp_path / "env_st"

    env = {
        "XDG_CONFIG_HOME": str(cfg_dir),
        "XDG_STATE_HOME": str(default_st),
    }

    # 1. Default precedence
    assert state.state_root_source(env) == "default"
    roots = state.init_roots(env)
    assert roots.state == default_st / "palimpsest"

    # 2. Config precedence
    state.write_state_root(roots, config_st)
    assert state.state_root_source(env) == "config"
    roots_cfg = state.init_roots(env)
    assert roots_cfg.state == config_st

    # 3. Env precedence over config and default
    env_with_var = dict(env)
    env_with_var["PALIMPSEST_STATE_HOME"] = str(env_st)
    assert state.state_root_source(env_with_var) == "env"
    roots_env = state.init_roots(env_with_var)
    assert roots_env.state == env_st


def test_invalid_relative_palimpsest_state_home(tmp_path: Path) -> None:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_STATE_HOME": str(tmp_path / "st"),
        "PALIMPSEST_STATE_HOME": "relative/path/to/state",
    }
    with pytest.raises(StateError, match="PALIMPSEST_STATE_HOME must be an absolute path"):
        state.init_roots(env)


def test_invalid_relative_config_state_root(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg" / "palimpsest"
    cfg_dir.mkdir(parents=True)
    config_file = cfg_dir / "config.toml"
    config_file.write_text("[storage]\nstate_root = 'relative/path'\n", encoding="utf-8")

    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    with pytest.raises(StateError, match=r"invalid storage\.state_root in"):
        state.init_roots(env)


def test_invalid_toml_config_state_root(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg" / "palimpsest"
    cfg_dir.mkdir(parents=True)
    config_file = cfg_dir / "config.toml"
    config_file.write_text("[storage\nstate_root = ", encoding="utf-8")

    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    with pytest.raises(StateError, match=r"invalid storage\.state_root in"):
        state.init_roots(env)


def test_write_state_root_url_preservation(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    config_file = cfg_dir / "palimpsest" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[hub]\nurl = 'https://hub.example.com'\n", encoding="utf-8")

    env = {"XDG_CONFIG_HOME": str(cfg_dir)}
    roots = state.init_roots(env)

    new_st = tmp_path / "new_state"
    state.write_state_root(roots, new_st)

    content = config_file.read_text(encoding="utf-8")
    assert "[hub]" in content
    assert 'url = "https://hub.example.com"' in content
    assert "[storage]" in content
    assert f'state_root = "{new_st}"' in content

    reloaded_roots = state.init_roots(env)
    assert reloaded_roots.state == new_st


def test_write_state_root_requires_absolute_path(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    with pytest.raises(StateError, match="state root destination must be an absolute path"):
        state.write_state_root(roots, Path("relative/dest"))
