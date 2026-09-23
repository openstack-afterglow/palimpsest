#!/usr/bin/env python3
"""Create or verify an immutable SHA-specific development prerelease."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

CommandResult = subprocess.CompletedProcess[bytes]
CommandRunner = Callable[[Sequence[str]], CommandResult]

_REPAIR_ATTEMPTS = 5
_REPAIR_DELAY_SECONDS = 2.0
_ASSET_COMPLETE_STATE = "uploaded"


class PublicationError(RuntimeError):
    """The remote publication is absent, inconsistent, or cannot be verified."""


@dataclass(frozen=True)
class Asset:
    name: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class ExpectedRelease:
    repository: str
    sha: str
    tag: str
    title: str
    notes: str
    assets: tuple[Asset, ...]


def _run(command: Sequence[str]) -> CommandResult:
    return subprocess.run(command, capture_output=True, check=False)


def _error(result: CommandResult) -> str:
    return (
        result.stderr.decode("utf-8", errors="replace").strip()
        or result.stdout.decode("utf-8", errors="replace").strip()
    )


def _is_not_found(result: CommandResult) -> bool:
    return result.returncode != 0 and "(http 404)" in _error(result).lower()


def _expect_success(result: CommandResult, action: str) -> bytes:
    if result.returncode != 0:
        raise PublicationError(f"{action} failed: {_error(result)}")
    return result.stdout


def _json(result: CommandResult, action: str) -> Mapping[str, object]:
    try:
        value = json.loads(_expect_success(result, action))
    except json.JSONDecodeError as exc:
        raise PublicationError(f"{action} did not return JSON") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{action} returned an unexpected JSON value")
    return value


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_assets(dist_dir: Path) -> tuple[Asset, ...]:
    manifest = dist_dir / "SHA256SUMS"
    if not manifest.is_file():
        raise PublicationError("dist directory does not contain SHA256SUMS")
    entries: dict[str, str] = {}
    for line in manifest.read_text(encoding="ascii").splitlines():
        try:
            checksum, name = line.split("  ", maxsplit=1)
        except ValueError as exc:
            raise PublicationError("SHA256SUMS has an invalid entry") from exc
        if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
            raise PublicationError("SHA256SUMS has an invalid SHA-256 digest")
        if not name or Path(name).name != name or name in entries:
            raise PublicationError("SHA256SUMS has an unsafe or duplicate filename")
        entries[name] = checksum

    files = tuple(dist_dir.iterdir())
    if len(files) != 3 or any(not path.is_file() for path in files):
        raise PublicationError("dist directory must contain exactly three regular files")
    distribution_names = {path.name for path in files if path.name != manifest.name}
    if len(distribution_names) != 2 or set(entries) != distribution_names:
        raise PublicationError("SHA256SUMS must describe exactly the wheel and source distribution")
    if (
        sum(name.endswith(".whl") for name in distribution_names) != 1
        or sum(name.endswith(".tar.gz") for name in distribution_names) != 1
    ):
        raise PublicationError("dist directory must contain exactly one wheel and one source distribution")

    assets: list[Asset] = []
    for path in sorted(files, key=lambda item: item.name):
        expected = entries.get(path.name)
        actual = _digest(path)
        if expected is not None and actual != expected:
            raise PublicationError(f"local checksum does not match SHA256SUMS: {path.name}")
        assets.append(Asset(path.name, path, actual))
    return tuple(assets)


def _expected_release(repository: str, sha: str, dist_dir: Path) -> ExpectedRelease:
    if not repository or "/" not in repository:
        raise PublicationError("repository must be an owner/repository name")
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha.lower()):
        raise PublicationError("sha must be a full hexadecimal commit SHA")
    sha = sha.lower()
    tag = f"package-{sha}"
    return ExpectedRelease(
        repository=repository,
        sha=sha,
        tag=tag,
        title=f"Development package {sha}",
        notes=(
            f"Unqualified development package built from commit {sha}. "
            "This is not a stable release or Gate 2 qualification."
        ),
        assets=_manifest_assets(dist_dir),
    )


def _read_tag(expected: ExpectedRelease, runner: CommandRunner) -> Mapping[str, object] | None:
    result = runner(("gh", "api", f"repos/{expected.repository}/git/ref/tags/{expected.tag}"))
    if _is_not_found(result):
        return None
    return _json(result, "read tag")


def _verify_tag(tag: Mapping[str, object], expected: ExpectedRelease) -> None:
    reference = tag.get("ref")
    object_value = tag.get("object")
    if not isinstance(object_value, dict):
        raise PublicationError("tag response has no object")
    if (
        reference != f"refs/tags/{expected.tag}"
        or object_value.get("type") != "commit"
        or object_value.get("sha") != expected.sha
    ):
        raise PublicationError("existing tag does not resolve to the requested commit SHA")


def _ensure_tag(expected: ExpectedRelease, runner: CommandRunner) -> None:
    tag = _read_tag(expected, runner)
    if tag is None:
        result = runner(
            (
                "gh",
                "api",
                f"repos/{expected.repository}/git/refs",
                "--method",
                "POST",
                "--raw-field",
                f"ref=refs/tags/{expected.tag}",
                "--raw-field",
                f"sha={expected.sha}",
            )
        )
        tag = _read_tag(expected, runner)
        if tag is None:
            if result.returncode != 0:
                _expect_success(result, "create tag")
            raise PublicationError("tag was absent after successful creation")
    _verify_tag(tag, expected)


def _read_release(expected: ExpectedRelease, runner: CommandRunner) -> Mapping[str, object] | None:
    result = runner(("gh", "api", f"repos/{expected.repository}/releases/tags/{expected.tag}"))
    if _is_not_found(result):
        return None
    return _json(result, "read release")


def _verify_release_metadata(release: Mapping[str, object], expected: ExpectedRelease) -> None:
    actual = {
        "tag_name": release.get("tag_name"),
        "name": release.get("name"),
        "body": release.get("body"),
        "draft": release.get("draft"),
        "prerelease": release.get("prerelease"),
    }
    wanted = {
        "tag_name": expected.tag,
        "name": expected.title,
        "body": expected.notes,
        "draft": False,
        "prerelease": True,
    }
    if actual != wanted:
        raise PublicationError("existing release metadata does not match the immutable development prerelease")


def _create_release(expected: ExpectedRelease, runner: CommandRunner) -> CommandResult:
    return runner(
        (
            "gh",
            "release",
            "create",
            expected.tag,
            *(str(asset.path) for asset in expected.assets),
            "--repo",
            expected.repository,
            "--verify-tag",
            "--title",
            expected.title,
            "--notes",
            expected.notes,
            "--prerelease",
            "--latest=false",
        )
    )


def _asset_states(release: Mapping[str, object]) -> dict[str, str]:
    """Return every declared asset name with its upload state."""
    value = release.get("assets")
    if not isinstance(value, list) or not all(isinstance(asset, dict) for asset in value):
        raise PublicationError("release response has invalid assets")
    states: dict[str, str] = {}
    for asset in value:
        name = asset.get("name")
        state = asset.get("state", _ASSET_COMPLETE_STATE)
        if not isinstance(name, str) or not name or name in states:
            raise PublicationError("release assets have missing or duplicate names")
        states[name] = state if isinstance(state, str) else _ASSET_COMPLETE_STATE
    return states


def _download_and_verify_asset(expected: ExpectedRelease, asset: Asset, runner: CommandRunner) -> None:
    with tempfile.TemporaryDirectory(prefix="palimpsest-development-package-") as temporary:
        destination = Path(temporary)
        result = runner(
            (
                "gh",
                "release",
                "download",
                expected.tag,
                "--repo",
                expected.repository,
                "--pattern",
                asset.name,
                "--dir",
                str(destination),
            )
        )
        _expect_success(result, f"download release asset {asset.name}")
        downloaded = destination / asset.name
        if not downloaded.is_file() or _digest(downloaded) != asset.sha256:
            raise PublicationError(f"remote release asset does not match local bytes: {asset.name}")


def _verify_or_repair_assets(release: Mapping[str, object], expected: ExpectedRelease, runner: CommandRunner) -> None:
    """Verify uploaded bytes, upload only absent assets, and await a concurrent publisher."""
    expected_by_name = {asset.name: asset for asset in expected.assets}
    failure: tuple[Asset, CommandResult] | None = None
    for attempt in range(_REPAIR_ATTEMPTS):
        states = _asset_states(release)
        extra = set(states) - set(expected_by_name)
        if extra:
            raise PublicationError(f"release has unexpected assets: {', '.join(sorted(extra))}")

        complete = {name for name, state in states.items() if state == _ASSET_COMPLETE_STATE}
        for name in sorted(complete):
            _download_and_verify_asset(expected, expected_by_name[name], runner)
        if complete == set(expected_by_name):
            return

        if attempt:
            # Another run of this commit may still be uploading; never clobber it.
            time.sleep(_REPAIR_DELAY_SECONDS)
        for asset in (item for item in expected.assets if item.name not in states):
            result = runner(
                (
                    "gh",
                    "release",
                    "upload",
                    expected.tag,
                    str(asset.path),
                    "--repo",
                    expected.repository,
                )
            )
            if result.returncode != 0:
                failure = (asset, result)

        reread = _read_release(expected, runner)
        if reread is None:
            raise PublicationError("release disappeared while repairing missing assets")
        _verify_release_metadata(reread, expected)
        release = reread

    if failure is not None:
        asset, result = failure
        _expect_success(result, f"upload missing release asset {asset.name}")
    raise PublicationError("release assets remain incomplete after repair")


def publish_development_package(repository: str, sha: str, dist_dir: Path, runner: CommandRunner = _run) -> None:
    """Create or verify the immutable tag, prerelease metadata, and artifact bytes."""
    expected = _expected_release(repository, sha, dist_dir)
    _ensure_tag(expected, runner)
    release = _read_release(expected, runner)
    if release is None:
        result = _create_release(expected, runner)
        release = _read_release(expected, runner)
        if release is None:
            if result.returncode != 0:
                _expect_success(result, "create release")
            raise PublicationError("release was absent after successful creation")
    _verify_release_metadata(release, expected)
    _verify_or_repair_assets(release, expected, runner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--sha", required=True, help="full commit SHA")
    parser.add_argument("--dist-dir", type=Path, required=True, help="locally checksum-verified artifact directory")
    arguments = parser.parse_args(argv)
    try:
        publish_development_package(arguments.repository, arguments.sha, arguments.dist_dir)
    except (OSError, PublicationError, ValueError) as exc:
        parser.exit(1, f"development package publication failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
