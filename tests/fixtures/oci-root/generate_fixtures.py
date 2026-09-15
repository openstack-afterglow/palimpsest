"""Generator script for 2-layer OCI changeset test fixtures and receipt."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import stat
import sys
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent
FIXTURE_MANIFEST_SCHEMA_VERSION = 1
FIXTURE_GENERATOR_CONTRACT = "palimpsest-oci-root-fixtures-v1"
FIXTURE_PAYLOAD_NAMES = (
    "base_layer.tar",
    "leaf_layer.tar",
    "expected_receipt.json",
)
CAPABILITY_V2_BYTES = b"\x01\x00\x00\x02\x01\x00\x00\x00" + (b"\x00" * 12)


def _xattrs_b64(values: Mapping[str, bytes | str]) -> dict[str, str]:
    return {
        name: base64.b64encode(value.encode() if isinstance(value, str) else value).decode("ascii")
        for name, value in values.items()
    }


def generate_fixtures() -> tuple[bytes, bytes, dict]:
    """Generate base layer tar, leaf layer tar, and expected merged receipt dict."""
    mtime_ts = 1700000000

    # 1. Build Base Layer Tar
    base_buf = io.BytesIO()
    with tarfile.open(fileobj=base_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # / root metadata
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755 | stat.S_IFDIR
        root.uid = 0
        root.gid = 0
        root.mtime = mtime_ts
        root.pax_headers = {"SCHILY.xattr.user.root": "root-metadata"}
        tar.addfile(root)

        # /dir1
        d1 = tarfile.TarInfo("dir1")
        d1.type = tarfile.DIRTYPE
        d1.mode = 0o755 | stat.S_IFDIR
        d1.uid = 1000
        d1.gid = 1000
        d1.mtime = mtime_ts
        tar.addfile(d1)

        # /dir1/file1.txt
        f1_data = b"base file1 content"
        f1 = tarfile.TarInfo("dir1/file1.txt")
        f1.type = tarfile.REGTYPE
        f1.mode = 0o644 | stat.S_IFREG
        f1.uid = 1000
        f1.gid = 1000
        f1.size = len(f1_data)
        f1.mtime = mtime_ts
        f1.pax_headers = {"SCHILY.xattr.user.note": "base_val"}
        tar.addfile(f1, io.BytesIO(f1_data))

        # /dir1/file2.txt (to be whiteouted by leaf)
        f2_data = b"to be whiteouted"
        f2 = tarfile.TarInfo("dir1/file2.txt")
        f2.type = tarfile.REGTYPE
        f2.mode = 0o644 | stat.S_IFREG
        f2.uid = 1000
        f2.gid = 1000
        f2.size = len(f2_data)
        f2.mtime = mtime_ts
        tar.addfile(f2, io.BytesIO(f2_data))

        # /dir1/subfile.txt
        sub_data = b"subfile content"
        sub = tarfile.TarInfo("dir1/subfile.txt")
        sub.type = tarfile.REGTYPE
        sub.mode = 0o644 | stat.S_IFREG
        sub.uid = 1000
        sub.gid = 1000
        sub.size = len(sub_data)
        sub.mtime = mtime_ts
        tar.addfile(sub, io.BytesIO(sub_data))

        # /dir_opaque (contains file that will be hidden by opaque whiteout in leaf)
        d_opq = tarfile.TarInfo("dir_opaque")
        d_opq.type = tarfile.DIRTYPE
        d_opq.mode = 0o755 | stat.S_IFDIR
        d_opq.mtime = mtime_ts
        tar.addfile(d_opq)

        # /dir_opaque/old_file.txt
        old_data = b"old file in opaque dir"
        old_f = tarfile.TarInfo("dir_opaque/old_file.txt")
        old_f.type = tarfile.REGTYPE
        old_f.mode = 0o644 | stat.S_IFREG
        old_f.size = len(old_data)
        old_f.mtime = mtime_ts
        tar.addfile(old_f, io.BytesIO(old_data))

        # /file_to_dir (file in base, dir in leaf)
        ftd_data = b"i am file in base"
        ftd = tarfile.TarInfo("file_to_dir")
        ftd.type = tarfile.REGTYPE
        ftd.mode = 0o644 | stat.S_IFREG
        ftd.size = len(ftd_data)
        ftd.mtime = mtime_ts
        tar.addfile(ftd, io.BytesIO(ftd_data))

        # /dir_to_file (dir in base, file in leaf)
        dtf = tarfile.TarInfo("dir_to_file")
        dtf.type = tarfile.DIRTYPE
        dtf.mode = 0o755 | stat.S_IFDIR
        dtf.mtime = mtime_ts
        tar.addfile(dtf)

        # /dir_to_file/item.txt
        dtf_item_data = b"inside dir in base"
        dtf_item = tarfile.TarInfo("dir_to_file/item.txt")
        dtf_item.type = tarfile.REGTYPE
        dtf_item.mode = 0o644 | stat.S_IFREG
        dtf_item.size = len(dtf_item_data)
        dtf_item.mtime = mtime_ts
        tar.addfile(dtf_item, io.BytesIO(dtf_item_data))

        # /hardlink_target.txt
        hlt_data = b"hardlink target data"
        hlt = tarfile.TarInfo("hardlink_target.txt")
        hlt.type = tarfile.REGTYPE
        hlt.mode = 0o644 | stat.S_IFREG
        hlt.size = len(hlt_data)
        hlt.mtime = mtime_ts
        tar.addfile(hlt, io.BytesIO(hlt_data))

        # /hardlink_source.txt
        hls = tarfile.TarInfo("hardlink_source.txt")
        hls.type = tarfile.LNKTYPE
        hls.linkname = "hardlink_target.txt"
        hls.mtime = mtime_ts
        tar.addfile(hls)

        # /symlink.txt -> dir1/file1.txt
        sym = tarfile.TarInfo("symlink.txt")
        sym.type = tarfile.SYMTYPE
        sym.mode = 0o777 | stat.S_IFLNK
        sym.linkname = "dir1/file1.txt"
        sym.mtime = mtime_ts
        tar.addfile(sym)

        # /fifo_dev
        fifo = tarfile.TarInfo("fifo_dev")
        fifo.type = tarfile.FIFOTYPE
        fifo.mode = 0o666 | stat.S_IFIFO
        fifo.mtime = mtime_ts
        tar.addfile(fifo)

        # /char_dev
        cdev = tarfile.TarInfo("char_dev")
        cdev.type = tarfile.CHRTYPE
        cdev.mode = 0o600 | stat.S_IFCHR
        cdev.devmajor = 1
        cdev.devminor = 3
        cdev.mtime = mtime_ts
        tar.addfile(cdev)

        # /block_dev
        bdev = tarfile.TarInfo("block_dev")
        bdev.type = tarfile.BLKTYPE
        bdev.mode = 0o600 | stat.S_IFBLK
        bdev.devmajor = 8
        bdev.devminor = 0
        bdev.mtime = mtime_ts
        tar.addfile(bdev)

        # /cap_file.txt
        cap_data = b"file with capability"
        cap = tarfile.TarInfo("cap_file.txt")
        cap.type = tarfile.REGTYPE
        cap.mode = 0o644 | stat.S_IFREG
        cap.size = len(cap_data)
        cap.mtime = mtime_ts
        cap.pax_headers = {"SCHILY.xattr.security.capability": CAPABILITY_V2_BYTES.decode("latin-1")}
        tar.addfile(cap, io.BytesIO(cap_data))

    base_bytes = base_buf.getvalue()

    # 2. Build Leaf Layer Tar
    leaf_buf = io.BytesIO()
    with tarfile.open(fileobj=leaf_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # / root metadata remains explicit in the upper layer
        leaf_root = tarfile.TarInfo(".")
        leaf_root.type = tarfile.DIRTYPE
        leaf_root.mode = 0o755 | stat.S_IFDIR
        leaf_root.uid = 0
        leaf_root.gid = 0
        leaf_root.mtime = mtime_ts
        leaf_root.pax_headers = {"SCHILY.xattr.user.root": "root-metadata"}
        tar.addfile(leaf_root)

        # /dir1 (explicit parent metadata must survive the upper layer)
        leaf_d1 = tarfile.TarInfo("dir1")
        leaf_d1.type = tarfile.DIRTYPE
        leaf_d1.mode = 0o755 | stat.S_IFDIR
        leaf_d1.uid = 1000
        leaf_d1.gid = 1000
        leaf_d1.mtime = mtime_ts
        tar.addfile(leaf_d1)

        # /dir1/file1.txt (overwrite / last-wins)
        lf1_data = b"leaf file1 updated content"
        lf1 = tarfile.TarInfo("dir1/file1.txt")
        lf1.type = tarfile.REGTYPE
        lf1.mode = 0o644 | stat.S_IFREG
        lf1.uid = 1000
        lf1.gid = 1000
        lf1.size = len(lf1_data)
        lf1.mtime = mtime_ts
        lf1.pax_headers = {"SCHILY.xattr.user.note": "base_val"}
        tar.addfile(lf1, io.BytesIO(lf1_data))

        # /dir1/.wh.file2.txt (ordinary whiteout)
        wh = tarfile.TarInfo("dir1/.wh.file2.txt")
        wh.type = tarfile.REGTYPE
        wh.size = 0
        wh.mtime = mtime_ts
        tar.addfile(wh)

        # /dir_opaque/.wh..wh..opq (opaque whiteout)
        opq = tarfile.TarInfo("dir_opaque/.wh..wh..opq")
        opq.type = tarfile.REGTYPE
        opq.size = 0
        opq.mtime = mtime_ts
        tar.addfile(opq)

        # /dir_opaque/new_file.txt
        new_data = b"new file in leaf opaque dir"
        new_f = tarfile.TarInfo("dir_opaque/new_file.txt")
        new_f.type = tarfile.REGTYPE
        new_f.mode = 0o644 | stat.S_IFREG
        new_f.size = len(new_data)
        new_f.mtime = mtime_ts
        tar.addfile(new_f, io.BytesIO(new_data))

        # /file_to_dir/nested.txt (file -> dir replacement)
        nest_data = b"nested inside file_to_dir"
        nest_f = tarfile.TarInfo("file_to_dir/nested.txt")
        nest_f.type = tarfile.REGTYPE
        nest_f.mode = 0o644 | stat.S_IFREG
        nest_f.size = len(nest_data)
        nest_f.mtime = mtime_ts
        tar.addfile(nest_f, io.BytesIO(nest_data))

        # /.wh.dir_to_file (whiteout for dir_to_file)
        wh_dtf = tarfile.TarInfo(".wh.dir_to_file")
        wh_dtf.type = tarfile.REGTYPE
        wh_dtf.size = 0
        wh_dtf.mtime = mtime_ts
        tar.addfile(wh_dtf)

        # /dir_to_file (dir -> file replacement)
        dtf_f_data = b"now a file in leaf"
        dtf_f = tarfile.TarInfo("dir_to_file")
        dtf_f.type = tarfile.REGTYPE
        dtf_f.mode = 0o644 | stat.S_IFREG
        dtf_f.size = len(dtf_f_data)
        dtf_f.mtime = mtime_ts
        tar.addfile(dtf_f, io.BytesIO(dtf_f_data))

        # /user_xattr_file.txt
        ux_data = b"xattr test file"
        ux = tarfile.TarInfo("user_xattr_file.txt")
        ux.type = tarfile.REGTYPE
        ux.mode = 0o644 | stat.S_IFREG
        ux.size = len(ux_data)
        ux.mtime = mtime_ts
        ux.pax_headers = {"SCHILY.xattr.user.custom": "hello_xattr"}
        tar.addfile(ux, io.BytesIO(ux_data))

        # /forward_link_1.txt (hardlink target appears after source)
        fl1 = tarfile.TarInfo("forward_link_1.txt")
        fl1.type = tarfile.LNKTYPE
        fl1.linkname = "forward_link_2.txt"
        fl1.mtime = mtime_ts
        tar.addfile(fl1)

        # /forward_link_2.txt
        fl2_data = b"forward hardlink target data"
        fl2 = tarfile.TarInfo("forward_link_2.txt")
        fl2.type = tarfile.REGTYPE
        fl2.mode = 0o644 | stat.S_IFREG
        fl2.size = len(fl2_data)
        fl2.mtime = mtime_ts
        tar.addfile(fl2, io.BytesIO(fl2_data))

    leaf_bytes = leaf_buf.getvalue()

    # 3. Build Expected Merged Receipt Dict
    expected_entries = {
        ".": {
            "type": "dir",
            "mode": 0o755 | stat.S_IFDIR,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "xattrs_b64": _xattrs_b64({"user.root": "root-metadata"}),
        },
        "dir1": {
            "type": "dir",
            "mode": 0o755 | stat.S_IFDIR,
            "uid": 1000,
            "gid": 1000,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "xattrs_b64": {},
        },
        "dir1/file1.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 1000,
            "gid": 1000,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"leaf file1 updated content").hexdigest(),
            "xattrs_b64": _xattrs_b64({"user.note": "base_val"}),
        },
        "dir1/subfile.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 1000,
            "gid": 1000,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"subfile content").hexdigest(),
            "xattrs_b64": {},
        },
        "dir_opaque": {
            "type": "dir",
            "mode": 0o755 | stat.S_IFDIR,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "xattrs_b64": {},
        },
        "dir_opaque/new_file.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"new file in leaf opaque dir").hexdigest(),
            "xattrs_b64": {},
        },
        "file_to_dir": {
            "type": "dir",
            "mode": 0o755 | stat.S_IFDIR,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "xattrs_b64": {},
        },
        "file_to_dir/nested.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"nested inside file_to_dir").hexdigest(),
            "xattrs_b64": {},
        },
        "dir_to_file": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"now a file in leaf").hexdigest(),
            "xattrs_b64": {},
        },
        "hardlink_target.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"hardlink target data").hexdigest(),
            "inode_group": "hardlink_group_1",
            "xattrs_b64": {},
        },
        "hardlink_source.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"hardlink target data").hexdigest(),
            "link_target": "hardlink_target.txt",
            "inode_group": "hardlink_group_1",
            "xattrs_b64": {},
        },
        "forward_link_1.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"forward hardlink target data").hexdigest(),
            "link_target": "forward_link_2.txt",
            "inode_group": "forward_link_group",
            "xattrs_b64": {},
        },
        "forward_link_2.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"forward hardlink target data").hexdigest(),
            "inode_group": "forward_link_group",
            "xattrs_b64": {},
        },
        "symlink.txt": {
            "type": "symlink",
            "mode": 0o777 | stat.S_IFLNK,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "target": "dir1/file1.txt",
            "xattrs_b64": {},
        },
        "fifo_dev": {
            "type": "fifo",
            "mode": 0o666 | stat.S_IFIFO,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "xattrs_b64": {},
        },
        "char_dev": {
            "type": "chardev",
            "mode": 0o600 | stat.S_IFCHR,
            "uid": 0,
            "gid": 0,
            "rdev_major": 1,
            "rdev_minor": 3,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "xattrs_b64": {},
        },
        "block_dev": {
            "type": "blkdev",
            "mode": 0o600 | stat.S_IFBLK,
            "uid": 0,
            "gid": 0,
            "rdev_major": 8,
            "rdev_minor": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "xattrs_b64": {},
        },
        "cap_file.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"file with capability").hexdigest(),
            "xattrs_b64": _xattrs_b64({"security.capability": CAPABILITY_V2_BYTES}),
        },
        "user_xattr_file.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime_ns": mtime_ts * 1_000_000_000,
            "sha256": hashlib.sha256(b"xattr test file").hexdigest(),
            "xattrs_b64": _xattrs_b64({"user.custom": "hello_xattr"}),
        },
    }

    receipt = {"version": "1.0", "entries": expected_entries}
    return base_bytes, leaf_bytes, receipt


def render_receipt(receipt: Mapping[str, object]) -> bytes:
    """Serialize a receipt in the exact committed fixture format."""
    return (json.dumps(receipt, indent=2) + "\n").encode()


def generate_fixture_payloads() -> dict[str, bytes]:
    """Generate all committed fixture payloads without filesystem side effects."""
    base_bytes, leaf_bytes, receipt = generate_fixtures()
    return {
        "base_layer.tar": base_bytes,
        "leaf_layer.tar": leaf_bytes,
        "expected_receipt.json": render_receipt(receipt),
    }


def calculate_fixture_manifest(payloads: Mapping[str, bytes]) -> dict[str, object]:
    """Calculate the canonical manifest for an exact fixture payload set."""
    if set(payloads) != set(FIXTURE_PAYLOAD_NAMES):
        raise ValueError("fixture payload set does not match the generator contract")

    files: dict[str, object] = {}
    for name in FIXTURE_PAYLOAD_NAMES:
        payload = payloads[name]
        if not isinstance(payload, bytes):
            raise TypeError(f"fixture payload {name!r} is not bytes")
        files[name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    return {
        "schema_version": FIXTURE_MANIFEST_SCHEMA_VERSION,
        "generator_contract": FIXTURE_GENERATOR_CONTRACT,
        "files": files,
    }


def render_fixture_manifest(manifest: Mapping[str, object]) -> bytes:
    """Serialize a fixture manifest in its canonical committed format."""
    return (json.dumps(manifest, indent=2) + "\n").encode()


def check_fixture_files(fixtures_dir: Path | None = None) -> tuple[str, ...]:
    """Return deterministic integrity failures without modifying fixture files."""
    root = FIXTURES_DIR if fixtures_dir is None else fixtures_dir
    expected_payloads = generate_fixture_payloads()
    expected_manifest = calculate_fixture_manifest(expected_payloads)
    expected_manifest_bytes = render_fixture_manifest(expected_manifest)
    failures: list[str] = []

    manifest_path = root / "fixture-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except (OSError, ValueError) as exc:
        failures.append(f"fixture-manifest.json: missing or unreadable: {exc}")
    else:
        if manifest_bytes != expected_manifest_bytes:
            failures.append("fixture-manifest.json: byte regeneration mismatch")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(f"fixture-manifest.json: invalid JSON: {exc}")
        else:
            if manifest != expected_manifest:
                failures.append("fixture-manifest.json: content mismatch")

    manifest_files = expected_manifest["files"]
    if not isinstance(manifest_files, dict):
        raise AssertionError("calculated manifest has an invalid files table")
    for name in FIXTURE_PAYLOAD_NAMES:
        path = root / name
        if path.is_symlink():
            failures.append(f"{name}: symlink is not a fixture file")
            continue
        try:
            payload = path.read_bytes()
        except (OSError, ValueError) as exc:
            failures.append(f"{name}: missing or unreadable: {exc}")
            continue
        if not path.is_file():
            failures.append(f"{name}: not a regular file")
            continue

        expected_entry = manifest_files[name]
        if not isinstance(expected_entry, dict):
            raise AssertionError("calculated manifest has an invalid file entry")
        if len(payload) != expected_entry["size"]:
            failures.append(f"{name}: size mismatch")
        if hashlib.sha256(payload).hexdigest() != expected_entry["sha256"]:
            failures.append(f"{name}: sha256 mismatch")
        if payload != expected_payloads[name]:
            failures.append(f"{name}: byte regeneration mismatch")

    return tuple(failures)


def write_fixture_files() -> None:
    """Generate and write fixture files to tests/fixtures/oci-root/."""
    payloads = generate_fixture_payloads()
    manifest = calculate_fixture_manifest(payloads)

    for name, payload in payloads.items():
        (FIXTURES_DIR / name).write_bytes(payload)
    (FIXTURES_DIR / "fixture-manifest.json").write_bytes(render_fixture_manifest(manifest))


def main(argv: Sequence[str] | None = None) -> int:
    """Generate fixtures, or validate committed fixtures with ``--check``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify committed fixture bytes and manifest without writing files",
    )
    args = parser.parse_args(argv)
    if not args.check:
        write_fixture_files()
        return 0

    failures = check_fixture_files()
    if failures:
        for failure in failures:
            print(f"fixture integrity check failed: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
