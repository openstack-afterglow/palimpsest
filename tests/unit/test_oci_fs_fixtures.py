from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from palimpsest_local.oci_convert import _probe_fixture_digest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPOSITORY_ROOT / "tests" / "fixtures" / "oci-root"
GENERATOR_PATH = FIXTURES_DIR / "generate_fixtures.py"
COMMITTED_NAMES = (
    "base_layer.tar",
    "leaf_layer.tar",
    "expected_receipt.json",
    "fixture-manifest.json",
)


def _load_generator_module():
    spec = importlib.util.spec_from_file_location("oci_root_fixture_generator", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in COMMITTED_NAMES if (root / name).exists()}


def _copy_fixture_set(destination: Path) -> None:
    destination.mkdir()
    for name in COMMITTED_NAMES:
        shutil.copy2(FIXTURES_DIR / name, destination / name)


def test_committed_fixtures_match_deterministic_regeneration_exactly():
    generator = _load_generator_module()
    before = _snapshot(FIXTURES_DIR)

    payloads = generator.generate_fixture_payloads()
    calculated_manifest = generator.calculate_fixture_manifest(payloads)
    manifest_bytes = (FIXTURES_DIR / "fixture-manifest.json").read_bytes()

    assert tuple(payloads) == generator.FIXTURE_PAYLOAD_NAMES
    assert json.loads(manifest_bytes) == calculated_manifest
    assert manifest_bytes == generator.render_fixture_manifest(calculated_manifest)
    for name, expected_bytes in payloads.items():
        assert (FIXTURES_DIR / name).read_bytes() == expected_bytes
    assert generator.check_fixture_files(FIXTURES_DIR) == ()
    assert generator.main(["--check"]) == 0
    assert _snapshot(FIXTURES_DIR) == before


def test_retained_development_evidence_matches_current_fixture_and_receipt():
    receipt = json.loads((FIXTURES_DIR / "expected_receipt.json").read_text(encoding="utf-8"))
    fixture_digest = _probe_fixture_digest(
        (FIXTURES_DIR / "base_layer.tar").read_bytes(),
        (FIXTURES_DIR / "leaf_layer.tar").read_bytes(),
        receipt,
    )
    receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()

    evidence_dir = FIXTURES_DIR / "evidence"
    squashfs = json.loads((evidence_dir / "squashfs-linuxkit-aarch64.json").read_text(encoding="utf-8"))
    squashfs_replay = json.loads((evidence_dir / "squashfs-linuxkit-aarch64-replay.json").read_text(encoding="utf-8"))
    erofs = json.loads((evidence_dir / "erofs-linuxkit-aarch64.json").read_text(encoding="utf-8"))

    assert squashfs["fixture_digest"] == fixture_digest
    assert squashfs_replay == squashfs
    assert erofs["fixture_digest"] == fixture_digest
    assert squashfs["merged_receipt_sha256"] == receipt_digest
    assert squashfs["candidate"] == "squashfs"
    assert erofs["candidate"] == "erofs"
    assert erofs["status"] == "failed"
    assert len(squashfs["pack_commands"]) == 4
    assert all(command[0] == "<verified-packer>" for command in squashfs["pack_commands"])


@pytest.mark.parametrize(
    ("case", "expected_failures"),
    [
        ("missing", ("missing or unreadable",)),
        ("same-size-corruption", ("sha256 mismatch", "byte regeneration mismatch")),
        ("size-corruption", ("size mismatch", "sha256 mismatch", "byte regeneration mismatch")),
        ("manifest-corruption", ("byte regeneration mismatch", "content mismatch")),
    ],
)
def test_check_rejects_missing_or_corrupt_fixtures_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_failures: tuple[str, ...],
):
    generator = _load_generator_module()
    fixtures = tmp_path / "fixtures"
    _copy_fixture_set(fixtures)

    if case == "missing":
        (fixtures / "leaf_layer.tar").unlink()
    elif case == "same-size-corruption":
        path = fixtures / "base_layer.tar"
        payload = bytearray(path.read_bytes())
        payload[len(payload) // 2] ^= 0xFF
        path.write_bytes(payload)
    elif case == "size-corruption":
        path = fixtures / "expected_receipt.json"
        path.write_bytes(path.read_bytes() + b" ")
    else:
        path = fixtures / "fixture-manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["files"]["base_layer.tar"]["size"] = 1
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    before = _snapshot(fixtures)
    failures = generator.check_fixture_files(fixtures)
    monkeypatch.setattr(generator, "FIXTURES_DIR", fixtures)

    assert failures
    assert all(any(expected in failure for failure in failures) for expected in expected_failures)
    assert generator.main(["--check"]) == 1
    assert _snapshot(fixtures) == before
