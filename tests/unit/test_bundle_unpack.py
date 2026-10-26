"""Native package archives unpack through the installed CLI's verified bundle path."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from palimpsest_local import cli
from palimpsest_local.errors import ArtifactValidationError, DigestMismatchError
from palimpsest_local.oci_layout import (
    ANNOTATION_NAME,
    MAX_BUNDLE_MEMBERS,
    MEDIA_TYPE_IMAGE_QCOW2,
    MEDIA_TYPE_INDEX,
    MEDIA_TYPE_LAYER_CONFIG,
    MEDIA_TYPE_LAYER_SQUASHFS,
    MEDIA_TYPE_MANIFEST,
    ContentStore,
    build_bundle_tar_bytes,
    canonical_json,
    config_digest,
    extract_bundle_tar,
    verify_layout_dir,
)
from palimpsest_local.package_source import snapshot_package


@pytest.fixture
def runtime_layout(tmp_path: Path) -> Path:
    layout = tmp_path / "source"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def blob(media_type: str, payload: bytes) -> dict:
        digest = config_digest(payload)
        (blobs / digest[7:]).write_bytes(payload)
        return {"mediaType": media_type, "digest": digest, "size": len(payload)}

    layers = []
    configs = []
    for name, media_type, payload in (
        ("base", MEDIA_TYPE_IMAGE_QCOW2, b"qcow2 boot disk"),
        ("dependencies", MEDIA_TYPE_LAYER_SQUASHFS, b"hsqs dependencies"),
        ("application", MEDIA_TYPE_LAYER_SQUASHFS, b"hsqs application"),
    ):
        layer = blob(media_type, payload)
        config = {"blob_digest": layer["digest"], "arch": "x86_64"}
        if not layers:
            config.update(kind="cloud-image", disk_format="qcow2")
        else:
            config.update(
                kind="squashfs",
                parent_digest=layers[-1]["digest"] if len(layers) > 1 else None,
                base_image_digest=layers[0]["digest"],
            )
        descriptor = blob(MEDIA_TYPE_LAYER_CONFIG, canonical_json(config))
        layer["annotations"] = {
            ANNOTATION_NAME: name,
            "dev.afterglow.palimpsest.config-digest": descriptor["digest"],
        }
        layers.append(layer)
        configs.append(descriptor)
    manifest = blob(
        MEDIA_TYPE_MANIFEST,
        canonical_json({"schemaVersion": 2, "mediaType": MEDIA_TYPE_MANIFEST, "config": configs[-1], "layers": layers}),
    )
    (layout / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
    (layout / "index.json").write_bytes(
        canonical_json({"schemaVersion": 2, "mediaType": MEDIA_TYPE_INDEX, "manifests": [manifest]})
    )
    return layout


@pytest.fixture
def runtime_archive(runtime_layout: Path, tmp_path: Path) -> Path:
    archive = tmp_path / "palimpsest-runtime.oci.tar"
    with snapshot_package(runtime_layout) as snapshot:
        assert snapshot.package_type == "runtime-bundle"
        archive.write_bytes(snapshot.archive.read_bytes())
    return archive


def test_native_snapshot_unpack_resolves_complete_runtime_stack_without_layer_selection(
    runtime_archive: Path, tmp_path: Path
) -> None:
    with tarfile.open(runtime_archive) as archive:
        assert [entry.name for entry in archive if entry.isdir()] == ["blobs", "blobs/sha256"]
    destination = tmp_path / "palimpsest-bundle"
    verified = extract_bundle_tar(runtime_archive, destination)
    assert verify_layout_dir(destination) == verified
    stack = cli._resolve_runtime_stack(ContentStore(tmp_path / "store"), destination, (), None)
    entries = verified.manifests[0].entries
    assert stack.base.digest == entries[0].digest
    assert stack.base.disk_format == "qcow2"
    assert stack.base.arch == "x86_64"
    assert stack.base.local_path.read_bytes() == b"qcow2 boot disk"
    assert [layer.digest for layer in stack.layers] == [entry.digest for entry in entries[1:]]
    assert [layer.arch for layer in stack.layers] == ["x86_64", "x86_64"]
    assert [layer.local_path.read_bytes() for layer in stack.layers] == [b"hsqs dependencies", b"hsqs application"]
    assert all(entry.local_path.is_relative_to(destination) for entry in entries)


def test_bundle_unpack_parser_registers_exact_command_and_requires_output() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["bundle", "unpack", "./palimpsest-runtime.oci.tar", "--output", "./palimpsest-bundle"])
    assert (args.operation, args.bundle_operation) == ("bundle", "unpack")
    assert args.archive == Path("./palimpsest-runtime.oci.tar")
    assert args.output == Path("./palimpsest-bundle")
    with pytest.raises(SystemExit) as failure:
        parser.parse_args(["bundle", "unpack", "./palimpsest-runtime.oci.tar"])
    assert failure.value.code == 2


@pytest.fixture
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, relative in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("PALIMPSEST_LOG_HOME", "journal"),
    ):
        root = tmp_path / relative
        root.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(root))
    monkeypatch.setattr(cli, "HubClient", lambda *a, **k: pytest.fail("local unpack must not contact Hub"))


def test_bundle_unpack_real_cli_handler(
    runtime_archive: Path, tmp_path: Path, isolated_cli: None, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "palimpsest-bundle"
    assert cli.main(["bundle", "unpack", str(runtime_archive), "--output", str(destination)]) == 0
    assert capsys.readouterr().out == f"{destination}\n"
    stack = cli._resolve_runtime_stack(ContentStore(tmp_path / "store"), destination, (), None)
    assert len(stack.layers) == 2
    assert stack.base.arch == "x86_64"


def _directory(name: str, *, size: int = 0, kind: bytes = tarfile.DIRTYPE, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.size, info.linkname = kind, 0o755, size, link
    return info


def _with_members(source: Path, destination: Path, extra: list[tarfile.TarInfo]) -> None:
    with tarfile.open(source) as original, tarfile.open(destination, "w", format=tarfile.USTAR_FORMAT) as output:
        for info in original:
            output.addfile(info, original.extractfile(info) if info.isfile() else None)
        for info in extra:
            output.addfile(info, io.BytesIO(b"x" * info.size) if info.size else None)


@pytest.mark.parametrize(
    "name,size,kind,link",
    [
        ("unexpected", 0, tarfile.DIRTYPE, ""),
        ("../outside", 0, tarfile.DIRTYPE, ""),
        ("/blobs", 0, tarfile.DIRTYPE, ""),
        ("./blobs", 0, tarfile.DIRTYPE, ""),
        ("blobs/./sha256", 0, tarfile.DIRTYPE, ""),
        ("blobs//sha256", 0, tarfile.DIRTYPE, ""),
        ("blobs///", 0, tarfile.DIRTYPE, ""),
        ("blobs/sha256/" + "a" * 64, 0, tarfile.DIRTYPE, ""),
        ("blobs", 1, tarfile.DIRTYPE, ""),
        ("blobs", 0, tarfile.DIRTYPE, "../outside"),
        ("blobs", 0, tarfile.SYMTYPE, "../outside"),
        ("blobs", 0, tarfile.LNKTYPE, "index.json"),
        ("blobs", 0, tarfile.FIFOTYPE, ""),
        ("blobs", 0, tarfile.CHRTYPE, ""),
        ("blobs", 0, tarfile.REGTYPE, ""),
        ("blobs", 0, tarfile.DIRTYPE, ""),
        ("blobs/sha256", 0, tarfile.DIRTYPE, ""),
    ],
)
def test_unsafe_directory_members_leave_no_partial_destination(
    runtime_archive: Path, tmp_path: Path, name: str, size: int, kind: bytes, link: str
) -> None:
    archive = tmp_path / "unsafe.tar"
    _with_members(runtime_archive, archive, [_directory(name, size=size, kind=kind, link=link)])
    destination = tmp_path / "unpacked"
    with pytest.raises(ArtifactValidationError):
        extract_bundle_tar(archive, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".tmp_layout_*"))
    assert not (tmp_path / "outside").exists()


def test_directory_markers_count_toward_bundle_member_bound(runtime_archive: Path, tmp_path: Path) -> None:
    archive = tmp_path / "too-many.tar"
    _with_members(runtime_archive, archive, [_directory("blobs") for _ in range(MAX_BUNDLE_MEMBERS)])
    destination = tmp_path / "unpacked"
    with pytest.raises(ArtifactValidationError, match="member count"):
        extract_bundle_tar(archive, destination)
    assert not destination.exists()


@pytest.mark.parametrize("populated", [False, True])
def test_unpack_preserves_existing_destination_contract(runtime_archive: Path, tmp_path: Path, populated: bool) -> None:
    destination = tmp_path / "unpacked"
    destination.mkdir()
    if populated:
        sentinel = destination / "keep"
        sentinel.write_bytes(b"existing user content")
        with pytest.raises(ArtifactValidationError, match="not empty"):
            extract_bundle_tar(runtime_archive, destination)
        assert sentinel.read_bytes() == b"existing user content"
        assert list(destination.iterdir()) == [sentinel]
    else:
        assert extract_bundle_tar(runtime_archive, destination) == verify_layout_dir(destination)


def test_legacy_hub_file_only_bundle_still_unpacks(runtime_layout: Path, tmp_path: Path) -> None:
    members = {
        str(path.relative_to(runtime_layout)): path.read_bytes() for path in runtime_layout.rglob("*") if path.is_file()
    }
    archive = tmp_path / "legacy-hub.tar"
    archive.write_bytes(build_bundle_tar_bytes(members))
    destination = tmp_path / "legacy-bundle"
    verified = extract_bundle_tar(archive, destination)
    assert len(verified.manifests[0].entries) == 3
    assert verify_layout_dir(destination) == verified


def test_native_directory_markers_do_not_bypass_digest_checks_or_atomic_cleanup(
    runtime_archive: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "corrupt.tar"
    payload = runtime_archive.read_bytes()
    assert payload.count(b"hsqs application") == 1
    archive.write_bytes(payload.replace(b"hsqs application", b"hsqs corrupt app", 1))
    destination = tmp_path / "unpacked"
    with pytest.raises(DigestMismatchError):
        extract_bundle_tar(archive, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".tmp_layout_*"))


@pytest.mark.parametrize("archive_kind", ["missing", "directory", "not-tar", "truncated"])
def test_bundle_unpack_cli_reports_archive_input_failures_without_partial_destination(
    runtime_archive: Path,
    tmp_path: Path,
    isolated_cli: None,
    archive_kind: str,
) -> None:
    archive = tmp_path / "invalid.oci.tar"
    if archive_kind == "directory":
        archive.mkdir()
    elif archive_kind == "not-tar":
        archive.write_bytes(b"this is not an OCI archive")
    elif archive_kind == "truncated":
        archive.write_bytes(runtime_archive.read_bytes()[:1300])
    destination = tmp_path / "unpacked"
    assert cli.main(["bundle", "unpack", str(archive), "--output", str(destination)]) == 1
    assert not destination.exists()
    assert not list(tmp_path.glob(".tmp_layout_*"))


@pytest.mark.parametrize("layout_kind", ["annotated-stack", "unannotated-base", "unannotated-stack"])
@pytest.mark.parametrize("mismatched_blob", [False, True])
def test_optional_native_blob_binding_preserves_runtime_architecture(
    runtime_layout: Path, tmp_path: Path, layout_kind: str, mismatched_blob: bool
) -> None:
    blobs = runtime_layout / "blobs" / "sha256"
    index = json.loads((runtime_layout / "index.json").read_bytes())
    manifest = json.loads((blobs / index["manifests"][0]["digest"][7:]).read_bytes())
    if layout_kind == "unannotated-base":
        manifest["layers"] = manifest["layers"][:1]

    def descriptor(media_type: str, value: dict) -> dict:
        payload = canonical_json(value)
        digest = config_digest(payload)
        (blobs / digest[7:]).write_bytes(payload)
        return {"mediaType": media_type, "digest": digest, "size": len(payload)}

    for layer in manifest["layers"]:
        annotations = layer["annotations"]
        config = json.loads((blobs / annotations["dev.afterglow.palimpsest.config-digest"][7:]).read_bytes())
        config.pop("blob_digest")
        if mismatched_blob:
            config["blob_digest"] = "sha256:" + "f" * 64
        manifest["config"] = descriptor(MEDIA_TYPE_LAYER_CONFIG, config)
        if layout_kind == "annotated-stack":
            annotations["dev.afterglow.palimpsest.config-digest"] = manifest["config"]["digest"]
        else:
            annotations.pop("dev.afterglow.palimpsest.config-digest")
    index["manifests"] = [descriptor(MEDIA_TYPE_MANIFEST, manifest)]
    (runtime_layout / "index.json").write_bytes(canonical_json(index))
    destination = tmp_path / "optional-binding"
    if mismatched_blob:
        archive = tmp_path / "mismatched.tar"
        archive.write_bytes(
            build_bundle_tar_bytes(
                {
                    str(path.relative_to(runtime_layout)): path.read_bytes()
                    for path in runtime_layout.rglob("*")
                    if path.is_file()
                }
            )
        )
        with pytest.raises(ArtifactValidationError, match="different blob"):
            extract_bundle_tar(archive, destination)
        assert not destination.exists()
        with pytest.raises(ArtifactValidationError, match="different blob"):
            verify_layout_dir(runtime_layout)
        return
    if layout_kind == "unannotated-stack":
        archive = tmp_path / "unannotated.tar"
        archive.write_bytes(
            build_bundle_tar_bytes(
                {
                    str(path.relative_to(runtime_layout)): path.read_bytes()
                    for path in runtime_layout.rglob("*")
                    if path.is_file()
                }
            )
        )
        extracted = extract_bundle_tar(archive, destination)
        assert verify_layout_dir(destination) == extracted
        assert [entry.arch for entry in extracted.manifests[0].entries] == [None, None, "x86_64"]
        return
    with snapshot_package(runtime_layout) as snapshot:
        assert snapshot.package_type == "runtime-bundle"
        extracted = extract_bundle_tar(snapshot.archive, destination)
    assert verify_layout_dir(destination) == extracted
    assert all(entry.arch == "x86_64" for entry in extracted.manifests[0].entries)
    stack = cli._resolve_runtime_stack(ContentStore(tmp_path / "store"), destination, (), None)
    assert stack.base.arch == "x86_64"
    assert len(stack.layers) == (2 if layout_kind == "annotated-stack" else 0)
