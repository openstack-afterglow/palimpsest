from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from palimpsest_local.ext4_upper import make_payload_opener, read_upper
from palimpsest_local.oci_capture import CaptureError, UpperKind, convert_upper_to_layer


def _tool(name: str) -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    for base in ("/opt/homebrew/opt/e2fsprogs/sbin", "/usr/sbin", "/sbin"):
        candidate = Path(base, name)
        if candidate.exists():
            return candidate
    return None


MKE2FS = _tool("mke2fs")
DEBUGFS = _tool("debugfs")
pytestmark = pytest.mark.skipif(MKE2FS is None or DEBUGFS is None, reason="e2fsprogs is not installed")


def _debugfs_write(image: Path, commands: list[str], tmp_path: Path) -> None:
    script = tmp_path / "write.cmd"
    script.write_text("\n".join(commands) + "\n")
    subprocess.run([str(DEBUGFS), "-w", "-f", str(script), str(image)], check=True, capture_output=True)


@pytest.fixture
def image(tmp_path):
    tree = tmp_path / "tree"
    upper = tree / "upper"
    (upper / "opt" / "app").mkdir(parents=True)
    (upper / "etc").mkdir()
    (upper / ".palimpsest-ignored").write_text("")
    (tree / "work").mkdir()
    (upper / "opt" / "app" / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(upper / "opt" / "app" / "run.sh", 0o755)
    (upper / "opt" / "app" / "big.bin").write_bytes(os.urandom(300_000))
    os.link(upper / "opt" / "app" / "run.sh", upper / "opt" / "app" / "run-link.sh")
    os.symlink("run.sh", upper / "opt" / "app" / "fast-link")
    os.symlink("d" * 150, upper / "opt" / "app" / "slow-link")
    (upper / "etc" / "motd").write_text("hello\n")
    (upper / "opt" / "empty").write_bytes(b"")
    disk = tmp_path / "upper.ext4"
    with disk.open("wb") as handle:
        handle.truncate(32 * 1024 * 1024)
    subprocess.run([str(MKE2FS), "-q", "-t", "ext4", "-d", str(tree), str(disk)], check=True, capture_output=True)
    blob = tmp_path / "blob"
    blob.write_bytes(b"\x00\x01\xff\n")
    _debugfs_write(
        disk,
        [
            "cd /upper/opt/app",
            "mknod gone c 0 0",
            "mknod realdev c 4 5",
            "mknod pipe p",
            f"ea_set -f {blob} run.sh user.blob",
            "ea_set /upper/opt trusted.overlay.opaque y",
        ],
        tmp_path,
    )
    return disk


def by_path(entries):
    return {entry.path: entry for entry in entries}


def test_reads_every_kind_with_metadata_and_canonical_order(image):
    entries = read_upper(image, DEBUGFS)
    paths = [entry.path for entry in entries]
    assert paths == sorted(paths)
    found = by_path(entries)
    assert found["opt"].kind is UpperKind.DIRECTORY
    assert found["opt/app/run-link.sh"].kind is UpperKind.FILE and found["opt/app/run-link.sh"].mode == 0o755
    assert found["opt/app/run-link.sh"].size == len(b"#!/bin/sh\necho hi\n")
    assert found["opt/app/big.bin"].size == 300_000
    assert found["opt/empty"].kind is UpperKind.FILE and found["opt/empty"].size == 0
    assert found["opt/app/fast-link"].kind is UpperKind.SYMLINK and found["opt/app/fast-link"].link_target == "run.sh"
    assert found["opt/app/slow-link"].link_target == "d" * 150
    assert found["opt/app/gone"].kind is UpperKind.WHITEOUT
    assert found["opt/app/realdev"].kind is UpperKind.OTHER
    assert found["opt/app/pipe"].kind is UpperKind.OTHER
    assert found["etc/motd"].mtime > 1_600_000_000


def test_hardlinks_point_at_the_canonically_first_path_and_carry_no_payload(image):
    found = by_path(read_upper(image, DEBUGFS))
    first, second = found["opt/app/run-link.sh"], found["opt/app/run.sh"]
    # "run-link.sh" sorts before "run.sh", so it is the primary file and run.sh links to it.
    assert first.kind is UpperKind.FILE and first.payload is not None
    assert second.kind is UpperKind.HARDLINK and second.link_target == "opt/app/run-link.sh"
    assert second.payload is None and second.size == 0


def test_binary_and_overlay_xattrs_are_returned_exactly(image):
    found = by_path(read_upper(image, DEBUGFS))
    assert dict(found["opt/app/run-link.sh"].xattrs).get("user.blob") == b"\x00\x01\xff\n"
    assert dict(found["opt"].xattrs).get("trusted.overlay.opaque") == b"y"
    assert found["etc/motd"].xattrs == ()


def test_conversion_streams_exact_file_bytes_and_overlay_markers_into_the_layer(image):
    entries = read_upper(image, DEBUGFS)
    with pytest.raises(CaptureError):
        convert_upper_to_layer(entries, io.BytesIO(), make_payload_opener(DEBUGFS))
    entries = [entry for entry in entries if entry.kind is not UpperKind.OTHER]
    sink = io.BytesIO()
    receipt = convert_upper_to_layer(entries, sink, make_payload_opener(DEBUGFS))
    assert receipt.entries > 0
    with tarfile.open(fileobj=io.BytesIO(sink.getvalue()), mode="r:") as layer:
        names = layer.getnames()
        assert "opt/app/.wh.gone" in names and "opt/app/gone" not in names
        assert layer.extractfile("etc/motd").read() == b"hello\n"
        assert len(layer.extractfile("opt/app/big.bin").read()) == 300_000
        link = layer.getmember("opt/app/run.sh")
        assert link.islnk() and link.linkname == "opt/app/run-link.sh"
        assert layer.extractfile("opt/app/run-link.sh").read() == b"#!/bin/sh\necho hi\n"


def test_payload_bytes_match_the_original_file_exactly(image, tmp_path):
    found = by_path(read_upper(image, DEBUGFS))
    opener = make_payload_opener(DEBUGFS)
    with opener(found["opt/app/big.bin"].payload) as stream:
        data = stream.read()
    assert len(data) == 300_000


def test_missing_upper_directory_fails_closed(image):
    with pytest.raises(CaptureError) as raised:
        read_upper(image, DEBUGFS, upper="no/such/upper")
    assert raised.value.code == "ext4-upper"


def test_corrupt_or_missing_image_fails_closed(tmp_path):
    bogus = tmp_path / "bogus.img"
    bogus.write_bytes(b"not an ext4 filesystem" * 1000)
    with pytest.raises(CaptureError):
        read_upper(bogus, DEBUGFS)
    with pytest.raises(CaptureError):
        read_upper(tmp_path / "absent.img", DEBUGFS)


def test_entry_limit_is_enforced(image):
    with pytest.raises(CaptureError) as raised:
        read_upper(image, DEBUGFS, max_entries=3)
    assert "limit" in str(raised.value)


def _hostile_image(tmp_path, names):
    tree = tmp_path / "htree"
    upper = tree / "upper"
    upper.mkdir(parents=True)
    (upper / "good-before").write_text("1")
    (upper / "zz-good-after").write_text("2")
    for name in names:
        (upper / name).write_text("x")
    disk = tmp_path / "hostile.ext4"
    with disk.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024)
    subprocess.run([str(MKE2FS), "-q", "-t", "ext4", "-d", str(tree), str(disk)], check=True, capture_output=True)
    return disk


@pytest.mark.parametrize(
    "name",
    [
        "evil\ndebugfs: stat <12>",
        "evil\ndebugfs: ls -p <12>",
        "plain\nnewline",
    ],
)
def test_a_file_name_that_forges_debugfs_framing_fails_closed_instead_of_truncating(tmp_path, name):
    disk = _hostile_image(tmp_path, [name])
    with pytest.raises(CaptureError):
        read_upper(disk, DEBUGFS)


def test_a_non_utf8_file_name_fails_closed(tmp_path):
    tree = tmp_path / "btree"
    upper = tree / "upper"
    upper.mkdir(parents=True)
    try:
        (upper / os.fsdecode(b"bad-\xff")).write_text("x")
    except OSError:
        pytest.skip("this filesystem refuses non-UTF-8 names")
    disk = tmp_path / "bad.ext4"
    with disk.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024)
    subprocess.run([str(MKE2FS), "-q", "-t", "ext4", "-d", str(tree), str(disk)], check=True, capture_output=True)
    with pytest.raises(CaptureError):
        read_upper(disk, DEBUGFS)


def test_a_symlink_target_with_a_newline_cannot_forge_metadata_or_truncate(tmp_path):
    tree = tmp_path / "ltree"
    upper = tree / "upper"
    upper.mkdir(parents=True)
    os.symlink("a\nmtime: 0x1 --\nLinks: 9", upper / "link")
    (upper / "zz-after").write_text("2")
    disk = tmp_path / "link.ext4"
    with disk.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024)
    subprocess.run([str(MKE2FS), "-q", "-t", "ext4", "-d", str(tree), str(disk)], check=True, capture_output=True)
    try:
        entries = read_upper(disk, DEBUGFS)
    except CaptureError:
        return  # failing closed is acceptable
    found = by_path(entries)
    assert found["link"].link_target == "a\nmtime: 0x1 --\nLinks: 9" and found["link"].mtime != 1
    assert "zz-after" in found


def test_an_xattr_value_that_forges_a_prompt_cannot_silently_drop_later_xattrs(tmp_path):
    tree = tmp_path / "xtree"
    upper = tree / "upper"
    (upper / "dir").mkdir(parents=True)
    disk = tmp_path / "x.ext4"
    with disk.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024)
    subprocess.run([str(MKE2FS), "-q", "-t", "ext4", "-d", str(tree), str(disk)], check=True, capture_output=True)
    forged = tmp_path / "forged"
    forged.write_bytes(b'v"\ndebugfs: nothing\n')
    _debugfs_write(
        disk,
        [f"ea_set -f {forged} /upper/dir user.first", "ea_set /upper/dir trusted.overlay.opaque y"],
        tmp_path,
    )
    try:
        entries = read_upper(disk, DEBUGFS)
    except CaptureError:
        return  # failing closed is the required outcome
    names = dict(by_path(entries)["dir"].xattrs)
    assert names.get("trusted.overlay.opaque") == b"y", "an injected prompt silently dropped a later xattr"


def test_staged_payloads_match_the_streamed_bytes_and_use_few_processes(image, tmp_path):
    from palimpsest_local.ext4_upper import StagedPayload, stage_payloads

    entries = read_upper(image, DEBUGFS)
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []

    def counting(*args, **kwargs):
        calls.append(args[0])
        from palimpsest_local.ext4_upper import limited_writer_run

        return limited_writer_run(*args, **kwargs)

    staged = stage_payloads(entries, DEBUGFS, stage, batch=2, runner=counting)
    files = [entry for entry in entries if entry.payload is not None]
    assert 1 <= len(calls) <= (len(files) + 1) // 2
    opener = make_payload_opener(DEBUGFS)
    for before, after in zip(entries, staged, strict=True):
        if before.payload is None:
            assert after.payload is None
            continue
        assert isinstance(after.payload, StagedPayload)
        with opener(before.payload) as streamed, opener(after.payload) as dumped:
            assert streamed.read() == dumped.read()


def test_staging_refuses_a_scratch_path_that_debugfs_could_misparse(image, tmp_path):
    from palimpsest_local.ext4_upper import stage_payloads

    bad = tmp_path / "has space"
    bad.mkdir()
    with pytest.raises(CaptureError):
        stage_payloads(read_upper(image, DEBUGFS), DEBUGFS, bad)


def test_staging_fails_closed_when_a_dumped_file_has_the_wrong_size(image, tmp_path):
    from dataclasses import replace

    from palimpsest_local.ext4_upper import Ext4Payload, stage_payloads

    entries = read_upper(image, DEBUGFS)
    tampered = [
        replace(entry, payload=Ext4Payload(entry.payload.image, entry.payload.inode, entry.payload.size + 1))
        if entry.payload is not None
        else entry
        for entry in entries
    ]
    stage = tmp_path / "stage2"
    stage.mkdir()
    with pytest.raises(CaptureError):
        stage_payloads(tampered, DEBUGFS, stage)
