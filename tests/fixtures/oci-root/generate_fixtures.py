"""Generator script for 2-layer OCI changeset test fixtures and receipt."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent


def generate_fixtures() -> tuple[bytes, bytes, dict]:
    """Generate base layer tar, leaf layer tar, and expected merged receipt dict."""
    mtime_ts = 1700000000.125

    # 1. Build Base Layer Tar
    base_buf = io.BytesIO()
    with tarfile.open(fileobj=base_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
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
        cap.pax_headers = {"SCHILY.xattr.security.capability": "cap_data"}
        tar.addfile(cap, io.BytesIO(cap_data))

    base_bytes = base_buf.getvalue()

    # 2. Build Leaf Layer Tar
    leaf_buf = io.BytesIO()
    with tarfile.open(fileobj=leaf_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
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
        "dir1": {
            "type": "dir",
            "mode": 0o755 | stat.S_IFDIR,
            "uid": 1000,
            "gid": 1000,
            "mtime": mtime_ts,
            "xattrs": {},
        },
        "dir1/file1.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 1000,
            "gid": 1000,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"leaf file1 updated content").hexdigest(),
            "xattrs": {"user.note": "base_val"},
        },
        "dir1/subfile.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 1000,
            "gid": 1000,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"subfile content").hexdigest(),
            "xattrs": {},
        },
        "dir_opaque": {
            "type": "dir",
            "mode": 0o755 | stat.S_IFDIR,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "xattrs": {"trusted.overlay.opaque": "y"},
        },
        "dir_opaque/new_file.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"new file in leaf opaque dir").hexdigest(),
            "xattrs": {},
        },
        "file_to_dir": {
            "type": "dir",
            "mode": 0o755 | stat.S_IFDIR,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "xattrs": {},
        },
        "file_to_dir/nested.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"nested inside file_to_dir").hexdigest(),
            "xattrs": {},
        },
        "dir_to_file": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"now a file in leaf").hexdigest(),
            "xattrs": {},
        },
        "hardlink_target.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"hardlink target data").hexdigest(),
            "inode_group": "hardlink_group_1",
            "xattrs": {},
        },
        "hardlink_source.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"hardlink target data").hexdigest(),
            "link_target": "hardlink_target.txt",
            "inode_group": "hardlink_group_1",
            "xattrs": {},
        },
        "forward_link_1.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"forward hardlink target data").hexdigest(),
            "link_target": "forward_link_2.txt",
            "inode_group": "forward_link_group",
            "xattrs": {},
        },
        "forward_link_2.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"forward hardlink target data").hexdigest(),
            "inode_group": "forward_link_group",
            "xattrs": {},
        },
        "symlink.txt": {
            "type": "symlink",
            "mode": 0o777 | stat.S_IFLNK,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "target": "dir1/file1.txt",
            "xattrs": {},
        },
        "fifo_dev": {
            "type": "fifo",
            "mode": 0o666 | stat.S_IFIFO,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "xattrs": {},
        },
        "char_dev": {
            "type": "chardev",
            "mode": 0o600 | stat.S_IFCHR,
            "uid": 0,
            "gid": 0,
            "rdev_major": 1,
            "rdev_minor": 3,
            "mtime": mtime_ts,
            "xattrs": {},
        },
        "block_dev": {
            "type": "blkdev",
            "mode": 0o600 | stat.S_IFBLK,
            "uid": 0,
            "gid": 0,
            "rdev_major": 8,
            "rdev_minor": 0,
            "mtime": mtime_ts,
            "xattrs": {},
        },
        "cap_file.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"file with capability").hexdigest(),
            "xattrs": {"security.capability": "cap_data"},
        },
        "user_xattr_file.txt": {
            "type": "file",
            "mode": 0o644 | stat.S_IFREG,
            "uid": 0,
            "gid": 0,
            "mtime": mtime_ts,
            "sha256": hashlib.sha256(b"xattr test file").hexdigest(),
            "xattrs": {"user.custom": "hello_xattr"},
        },
    }

    receipt = {"version": "1.0", "entries": expected_entries}
    return base_bytes, leaf_bytes, receipt


def write_fixture_files() -> None:
    """Generate and write fixture files to tests/fixtures/oci-root/."""
    base_bytes, leaf_bytes, receipt = generate_fixtures()

    (FIXTURES_DIR / "base_layer.tar").write_bytes(base_bytes)
    (FIXTURES_DIR / "leaf_layer.tar").write_bytes(leaf_bytes)
    (FIXTURES_DIR / "expected_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    write_fixture_files()
