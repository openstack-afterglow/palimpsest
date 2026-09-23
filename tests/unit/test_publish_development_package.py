"""Behavioral contracts for immutable development-package publication."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "publish_development_package", ROOT / "scripts" / "publish_development_package.py"
)
assert SPEC is not None and SPEC.loader is not None
publication = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = publication
SPEC.loader.exec_module(publication)

REPOSITORY = "openstack-afterglow/palimpsest"
SHA = "a" * 40


def _result(command: tuple[str, ...], returncode: int = 0, payload: object | bytes = b"", error: str = ""):
    if not isinstance(payload, bytes):
        payload = json.dumps(payload).encode()
    return publication.subprocess.CompletedProcess(command, returncode, payload, error.encode())


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    artifacts = {"palimpsest_local-0.1.4-py3-none-any.whl": b"wheel", "palimpsest_local-0.1.4.tar.gz": b"sdist"}
    for name, payload in artifacts.items():
        (dist / name).write_bytes(payload)
    (dist / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(payload).hexdigest()}  {name}\n" for name, payload in sorted(artifacts.items())),
        encoding="ascii",
    )
    return dist


class FakeGh:
    def __init__(self, expected, *, tag_sha: str | None = None, assets: dict[str, bytes] | None = None) -> None:
        self.expected = expected
        self.tag_sha = tag_sha
        self.assets = assets
        self.metadata = {
            "tag_name": expected.tag,
            "name": expected.title,
            "body": expected.notes,
            "draft": False,
            "prerelease": True,
        }
        self.tag_create_conflict = False
        self.release_create_conflict_assets: dict[str, bytes] | None = None
        self.upload_conflicts: set[str] = set()
        self.tag_create_error: str | None = None
        self.release_create_error_assets: dict[str, bytes] | None = None
        self.upload_errors: dict[str, str] = {}
        self.upload_hard_failures: set[str] = set()
        self.pending: dict[str, int] = {}
        self.commands: list[tuple[str, ...]] = []

    @property
    def asset_bytes(self) -> dict[str, bytes]:
        return {asset.name: asset.path.read_bytes() for asset in self.expected.assets}

    def _release(self) -> dict[str, object]:
        assert self.assets is not None
        assets: list[dict[str, str]] = []
        for name in sorted(self.assets):
            remaining = self.pending.get(name, 0)
            if remaining:
                self.pending[name] = remaining - 1
                assets.append({"name": name, "state": "starting"})
            else:
                assets.append({"name": name, "state": "uploaded"})
        return {**self.metadata, "assets": assets}

    def __call__(self, command) -> publication.CommandResult:
        command = tuple(command)
        self.commands.append(command)
        if command[:3] == ("gh", "api", f"repos/{self.expected.repository}/git/ref/tags/{self.expected.tag}"):
            if self.tag_sha is None:
                return _result(command, 1, error="gh: Not Found (HTTP 404)")
            return _result(
                command,
                payload={
                    "ref": f"refs/tags/{self.expected.tag}",
                    "object": {"type": "commit", "sha": self.tag_sha},
                },
            )
        if command[:4] == ("gh", "api", f"repos/{self.expected.repository}/git/refs", "--method"):
            self.tag_sha = self.expected.sha
            if self.tag_create_error is not None:
                error = self.tag_create_error
                self.tag_create_error = None
                return _result(command, 1, error=error)
            if self.tag_create_conflict:
                self.tag_create_conflict = False
                return _result(command, 1, error="gh: Reference already exists (HTTP 422)")
            return _result(command, payload={})
        if command[:3] == ("gh", "api", f"repos/{self.expected.repository}/releases/tags/{self.expected.tag}"):
            if self.assets is None:
                return _result(command, 1, error="gh: Not Found (HTTP 404)")
            return _result(command, payload=self._release())
        if command[:3] == ("gh", "release", "create"):
            if self.release_create_error_assets is not None:
                self.assets = self.release_create_error_assets
                self.release_create_error_assets = None
                return _result(command, 1, error="connection interrupted after release creation")
            if self.release_create_conflict_assets is not None:
                self.assets = self.release_create_conflict_assets
                self.release_create_conflict_assets = None
                return _result(command, 1, error="gh: already_exists (HTTP 422)")
            asset_paths = command[4 : command.index("--repo")]
            self.assets = {Path(path).name: Path(path).read_bytes() for path in asset_paths}
            return _result(command)
        if command[:3] == ("gh", "release", "download"):
            assert self.assets is not None
            name = command[command.index("--pattern") + 1]
            if name not in self.assets:
                return _result(command, 1, error="asset not found")
            directory = Path(command[command.index("--dir") + 1])
            (directory / name).write_bytes(self.assets[name])
            return _result(command)
        if command[:3] == ("gh", "release", "upload"):
            assert self.assets is not None
            path = Path(command[4])
            if path.name in self.upload_hard_failures:
                return _result(command, 1, error="gh: HTTP 500 upload failed")
            payload = path.read_bytes()
            if path.name in self.upload_errors:
                error = self.upload_errors.pop(path.name)
                self.assets[path.name] = payload
                return _result(command, 1, error=error)
            if path.name in self.upload_conflicts:
                self.upload_conflicts.remove(path.name)
                self.assets[path.name] = payload
                return _result(command, 1, error="gh: already_exists (HTTP 422)")
            self.assets[path.name] = payload
            return _result(command)
        raise AssertionError(f"unexpected command: {command}")

    def mutations(self) -> list[tuple[str, ...]]:
        return [
            command
            for command in self.commands
            if (command[:3] == ("gh", "api", f"repos/{self.expected.repository}/git/refs") and "POST" in command)
            or command[:3] in {("gh", "release", "create"), ("gh", "release", "upload")}
        ]


@pytest.fixture
def expected(tmp_path: Path):
    return publication._expected_release(REPOSITORY, SHA, _dist(tmp_path))


def test_absent_publication_creates_and_verifies_every_expected_resource(expected) -> None:
    fake = FakeGh(expected)

    publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.tag_sha == SHA
    assert fake.assets == fake.asset_bytes
    assert len(fake.mutations()) == 2
    assert fake.mutations()[0][2].endswith("/git/refs")
    assert fake.mutations()[1][:3] == ("gh", "release", "create")


def test_exact_existing_publication_is_a_read_and_byte_verification_only(expected) -> None:
    fake = FakeGh(expected, tag_sha=SHA, assets=FakeGh(expected).asset_bytes)

    publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.mutations() == []
    assert sum(command[:3] == ("gh", "release", "download") for command in fake.commands) == 3


def test_different_tag_fails_before_any_release_mutation(expected) -> None:
    fake = FakeGh(expected, tag_sha="b" * 40, assets=FakeGh(expected).asset_bytes)

    with pytest.raises(publication.PublicationError, match="tag does not resolve"):
        publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.mutations() == []


def test_release_metadata_mismatch_fails_without_mutation(expected) -> None:
    fake = FakeGh(expected, tag_sha=SHA, assets=FakeGh(expected).asset_bytes)
    fake.metadata["prerelease"] = False

    with pytest.raises(publication.PublicationError, match="metadata"):
        publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.mutations() == []


@pytest.mark.parametrize(
    ("assets", "message"),
    [
        ({"palimpsest_local-0.1.4-py3-none-any.whl": b"changed"}, "does not match local bytes"),
        ({"unexpected.txt": b"unreviewed"}, "unexpected assets"),
    ],
)
def test_asset_mismatch_or_extra_asset_fails_without_replacing_remote_state(expected, assets, message: str) -> None:
    complete = FakeGh(expected).asset_bytes
    complete.update(assets)
    fake = FakeGh(expected, tag_sha=SHA, assets=complete)

    with pytest.raises(publication.PublicationError, match=message):
        publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.mutations() == []


def test_existing_release_with_only_missing_assets_is_repaired_without_clobbering(expected) -> None:
    partial = FakeGh(expected).asset_bytes
    missing = "SHA256SUMS"
    del partial[missing]
    fake = FakeGh(expected, tag_sha=SHA, assets=partial)

    publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.assets == fake.asset_bytes
    assert [command[:3] for command in fake.mutations()] == [("gh", "release", "upload")]
    assert "--clobber" not in fake.mutations()[0]


def test_tag_release_and_asset_create_races_reread_then_verify(expected) -> None:
    fake = FakeGh(expected)
    fake.tag_create_conflict = True
    incomplete = FakeGh(expected).asset_bytes
    del incomplete["palimpsest_local-0.1.4.tar.gz"]
    fake.release_create_conflict_assets = incomplete
    fake.upload_conflicts.add("palimpsest_local-0.1.4.tar.gz")

    publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.tag_sha == SHA
    assert fake.assets == fake.asset_bytes
    assert [command[:3] for command in fake.mutations()] == [
        ("gh", "api", f"repos/{REPOSITORY}/git/refs"),
        ("gh", "release", "create"),
        ("gh", "release", "upload"),
    ]
    assert all("--clobber" not in command for command in fake.commands)


def test_ambiguous_tag_release_and_asset_failures_reread_exact_remote_state(expected) -> None:
    fake = FakeGh(expected)
    fake.tag_create_error = "connection interrupted after tag creation"
    incomplete = FakeGh(expected).asset_bytes
    missing = "palimpsest_local-0.1.4.tar.gz"
    del incomplete[missing]
    fake.release_create_error_assets = incomplete
    fake.upload_errors[missing] = "connection interrupted after asset upload"

    publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.tag_sha == SHA
    assert fake.assets == fake.asset_bytes
    assert [command[:3] for command in fake.mutations()] == [
        ("gh", "api", f"repos/{REPOSITORY}/git/refs"),
        ("gh", "release", "create"),
        ("gh", "release", "upload"),
    ]


def test_asset_still_uploading_elsewhere_is_awaited_instead_of_reuploaded(expected, monkeypatch) -> None:
    monkeypatch.setattr(publication, "_REPAIR_DELAY_SECONDS", 0)
    fake = FakeGh(expected, tag_sha=SHA, assets=FakeGh(expected).asset_bytes)
    fake.pending["SHA256SUMS"] = 1

    publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.mutations() == []
    assert sum(command[:3] == ("gh", "release", "download") for command in fake.commands) == 5


def test_never_completed_concurrent_upload_fails_closed_without_clobbering(expected, monkeypatch) -> None:
    monkeypatch.setattr(publication, "_REPAIR_DELAY_SECONDS", 0)
    fake = FakeGh(expected, tag_sha=SHA, assets=FakeGh(expected).asset_bytes)
    fake.pending["SHA256SUMS"] = publication._REPAIR_ATTEMPTS + 1

    with pytest.raises(publication.PublicationError, match="remain incomplete"):
        publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert fake.mutations() == []


def test_persistently_missing_asset_surfaces_the_upload_failure_after_bounded_retries(expected, monkeypatch) -> None:
    monkeypatch.setattr(publication, "_REPAIR_DELAY_SECONDS", 0)
    partial = FakeGh(expected).asset_bytes
    missing = "SHA256SUMS"
    del partial[missing]
    fake = FakeGh(expected, tag_sha=SHA, assets=partial)
    fake.upload_hard_failures.add(missing)

    with pytest.raises(publication.PublicationError, match=f"upload missing release asset {missing}"):
        publication.publish_development_package(REPOSITORY, SHA, expected.assets[0].path.parent, fake)

    assert set(fake.assets) == set(partial)
    uploads = [command for command in fake.mutations() if command[:3] == ("gh", "release", "upload")]
    assert len(uploads) == publication._REPAIR_ATTEMPTS
    assert all("--clobber" not in command for command in fake.commands)
