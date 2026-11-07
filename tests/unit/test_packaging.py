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
