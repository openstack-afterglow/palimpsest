from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_package", ROOT / "scripts/build_package.py")
assert SPEC is not None and SPEC.loader is not None
packaging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packaging)


def _sdist(path: Path, payload: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(f"palimpsest-local/src/{packaging.ASSET}")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


def test_archive_validators_require_exact_packaged_asset(tmp_path: Path) -> None:
    payload = b"\x7fELF-sealed"
    sdist = tmp_path / "package.tar.gz"
    wheel = tmp_path / "package.whl"
    _sdist(sdist, payload)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(packaging.ASSET, payload)
    packaging._validate_sdist(sdist, payload)
    packaging._validate_wheel(wheel, payload)
    with pytest.raises(RuntimeError, match="does not match"):
        packaging._validate_sdist(sdist, b"different")
    with pytest.raises(RuntimeError, match="does not match"):
        packaging._validate_wheel(wheel, b"different")


def test_install_smoke_is_offline_isolated_and_uses_runtime_elf_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[list[str], Path, dict[str, str] | None]] = []
    monkeypatch.setattr(
        packaging, "_run", lambda command, *, env=None, cwd=packaging.ROOT: commands.append((command, cwd, env))
    )
    wheel = tmp_path / "package.whl"
    packaging._verify_install(wheel, tmp_path, "/opt/uv", "1.2.3")
    assert commands[0][0][:2] == ["/opt/uv", "venv"]
    assert commands[1][0][0:3] == ["/opt/uv", "pip", "install"]
    assert "--no-deps" in commands[1][0] and "--no-index" in commands[1][0]
    assert commands[2][0][1:3] == ["-I", "-c"]
    assert "_bootstrap_stage1_binary" in commands[2][0][3]
    assert "is_relative_to" in commands[2][0][3]
    assert commands[3][0][:3] == ["/opt/uv", "tool", "install"]
    assert "--no-index" in commands[3][0] and "--no-deps" not in commands[3][0]
    assert commands[3][2]["UV_TOOL_DIR"] == str(tmp_path / "tool-envs")
    assert commands[3][2]["UV_TOOL_BIN_DIR"] == str(tmp_path / "tool-bin")
    assert commands[4][0] == [str(tmp_path / "tool-bin/palimpsest"), "--version"]
    assert all(command[1] == tmp_path for command in commands[2:])
    assert all(env is None or "PYTHONPATH" not in env for _, _, env in commands[2:])


def test_output_directory_must_be_new_and_not_a_symlink(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(RuntimeError, match="already exists"):
        packaging._prepare_output(existing)
    link = tmp_path / "link"
    link.symlink_to(existing, target_is_directory=True)
    with pytest.raises(RuntimeError, match="already exists"):
        packaging._prepare_output(link)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(RuntimeError, match="already exists"):
        packaging._prepare_output(dangling)


def test_source_date_epoch_rejects_invalid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    with pytest.raises(ValueError, match="non-negative integer"):
        packaging._source_date_epoch()
