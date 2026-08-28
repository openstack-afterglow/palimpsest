"""OCI layer changeset conversion and filesystem semantics probing for Palimpsest."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from .errors import ArtifactValidationError, PalimpsestError, UnsupportedPlatformError

OCI_LAYER_FILESYSTEM: str | None = None


def get_oci_layer_filesystem() -> str:
    """Return the selected OCI layer filesystem established by probe.

    Raises UnsupportedPlatformError if no passing probe has established it.
    """
    if OCI_LAYER_FILESYSTEM is None:
        raise UnsupportedPlatformError(
            "OCI_LAYER_FILESYSTEM has not been established by a passing Linux probe."
        )
    return OCI_LAYER_FILESYSTEM


def check_platform_support() -> None:
    """Ensure host platform supports OCI filesystem probing and mounting.

    Must be called BEFORE executing any packer or mount subprocesses.
    """
    if sys.platform != "linux":
        raise UnsupportedPlatformError(
            f"OCI layer filesystem probing and OverlayFS mounting require a Linux host; {sys.platform} is unsupported."
        )


def to_base36(n: int) -> str:
    """Convert non-negative integer to lowercase base36 representation."""
    if n < 0:
        raise ValueError("base36 index must be non-negative")
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    res: list[str] = []
    while n > 0:
        n, rem = divmod(n, 36)
        res.append(chars[rem])
    return "".join(reversed(res))


def check_lowerdir_page_budget(num_layers: int = 128) -> tuple[bool, int, int]:
    """Check if lowerdir string for num_layers stays below 75% of host page size limit.

    Returns:
        (is_valid, opt_bytes_length, budget_bytes)
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, AttributeError, OSError):
        page_size = 4096

    budget = int(0.75 * page_size)
    paths = [f"/l/{to_base36(i)}" for i in reversed(range(num_layers))]
    lowerdir_opt = "lowerdir=" + ":".join(paths)
    opt_bytes = lowerdir_opt.encode("utf-8")
    length = len(opt_bytes)
    return (length <= budget, length, budget)


def _validate_tar_path(path: str) -> str:
    """Normalize and validate archive path against traversal and reserved paths."""
    if not path or path == ".":
        return "."
    normalized = os.path.normpath(path)
    if (
        normalized.startswith("/")
        or normalized.startswith("../")
        or "/../" in normalized
        or normalized == ".."
        or "\x00" in path
        or "\\" in path
    ):
        raise ArtifactValidationError(f"Invalid member path in OCI layer tar: {path}")
    if normalized.startswith(".palimpsest") or normalized.startswith("./.palimpsest"):
        raise ArtifactValidationError(f"Reserved path in OCI layer tar: {path}")
    return normalized


def translate_oci_tar_to_overlay_tar(in_tar_bytes: bytes) -> bytes:
    """Translate OCI layer tar stream into OverlayFS-compatible tar stream.

    Translates OCI whiteouts:
    - Ordinary whiteout (.wh.NAME) -> character device 0:0 at NAME (mode S_IFCHR | 0600)
    - Opaque whiteout (.wh..wh..opq) -> PAX header SCHILY.xattr.trusted.overlay.opaque=y on parent directory
    - Encodes xattrs into PAX headers SCHILY.xattr.<key>
    - Resolves same-layer whiteout/replacement last-wins semantics before emission.
    - Synthesizes parent directory entries for file-to-directory transitions.
    - Emits hardlink entries after their target regular files.
    """
    in_buf = io.BytesIO(in_tar_bytes)
    opaque_dirs: set[str] = set()
    table: dict[str, dict] = {}

    with tarfile.open(fileobj=in_buf, mode="r:*") as in_tar:
        for member in in_tar.getmembers():
            norm_name = _validate_tar_path(member.name)
            if not norm_name or norm_name == ".":
                continue
            basename = os.path.basename(norm_name)
            parent_dir = os.path.dirname(norm_name)

            if basename == ".wh..wh..opq":
                if parent_dir:
                    opaque_dirs.add(parent_dir)
                continue

            if basename.startswith(".wh."):
                target_name = basename[4:]
                target_path = os.path.join(parent_dir, target_name) if parent_dir else target_name

                # Purge target_path and any pending descendants of target_path from table
                keys_to_del = [k for k in table if k == target_path or k.startswith(target_path + "/")]
                for k in keys_to_del:
                    del table[k]

                table[target_path] = {
                    "is_whiteout": True,
                    "name": target_path,
                    "mtime": member.mtime,
                }
            else:
                # If non-directory member replaces a directory, purge pending descendants
                if not member.isdir():
                    keys_to_del = [k for k in table if k.startswith(norm_name + "/")]
                    for k in keys_to_del:
                        del table[k]

                pax_headers = dict(member.pax_headers or {})
                content = in_tar.extractfile(member).read() if member.isreg() else None

                table[norm_name] = {
                    "is_whiteout": False,
                    "name": norm_name,
                    "member": member,
                    "content": content,
                    "pax_headers": pax_headers,
                }

                # Synthesize missing parent directories for file-to-directory transitions
                curr = parent_dir
                while curr and curr != ".":
                    if (
                        curr not in table
                        or table[curr].get("is_whiteout")
                        or not table[curr]["member"].isdir()
                    ):
                        synth_info = tarfile.TarInfo(name=curr)
                        synth_info.type = tarfile.DIRTYPE
                        synth_info.mode = 0o755 | stat.S_IFDIR
                        synth_info.uid = 0
                        synth_info.gid = 0
                        synth_info.mtime = member.mtime
                        table[curr] = {
                            "is_whiteout": False,
                            "name": curr,
                            "member": synth_info,
                            "content": None,
                            "pax_headers": {},
                        }
                    curr = os.path.dirname(curr)

    # Ensure opaque dirs have directory entries in table with opaque PAX header
    for opq_dir in opaque_dirs:
        if opq_dir and opq_dir != ".":
            if (
                opq_dir not in table
                or table[opq_dir].get("is_whiteout")
                or not table[opq_dir]["member"].isdir()
            ):
                dir_info = tarfile.TarInfo(name=opq_dir)
                dir_info.type = tarfile.DIRTYPE
                dir_info.mode = 0o755 | stat.S_IFDIR
                dir_info.uid = 0
                dir_info.gid = 0
                dir_info.mtime = 0
                table[opq_dir] = {
                    "is_whiteout": False,
                    "name": opq_dir,
                    "member": dir_info,
                    "content": None,
                    "pax_headers": {"SCHILY.xattr.trusted.overlay.opaque": "y"},
                }
            else:
                table[opq_dir]["pax_headers"]["SCHILY.xattr.trusted.overlay.opaque"] = "y"

    # Order paths for emission: topological sort for hardlinks
    sorted_paths = sorted(table.keys())
    emitted_set: set[str] = set()
    ordered_paths: list[str] = []

    remaining = list(sorted_paths)
    while remaining:
        progress = False
        next_remaining = []
        for p in remaining:
            item = table[p]
            if not item["is_whiteout"] and item["member"].islnk():
                target = _validate_tar_path(item["member"].linkname)
                if target in table and target not in emitted_set:
                    next_remaining.append(p)
                    continue
            ordered_paths.append(p)
            emitted_set.add(p)
            progress = True
        if not progress:
            ordered_paths.extend(next_remaining)
            break
        remaining = next_remaining

    # Emit output tar
    out_buf = io.BytesIO()
    with tarfile.open(fileobj=out_buf, mode="w", format=tarfile.PAX_FORMAT) as out_tar:
        for p in ordered_paths:
            item = table[p]
            if item["is_whiteout"]:
                wh_info = tarfile.TarInfo(name=p)
                wh_info.type = tarfile.CHRTYPE
                wh_info.mode = 0o600 | stat.S_IFCHR
                wh_info.uid = 0
                wh_info.gid = 0
                wh_info.size = 0
                wh_info.mtime = item["mtime"]
                wh_info.devmajor = 0
                wh_info.devminor = 0
                out_tar.addfile(wh_info)
            else:
                orig_member = item["member"]
                new_info = tarfile.TarInfo(name=p)
                new_info.type = orig_member.type
                new_info.mode = orig_member.mode
                new_info.uid = orig_member.uid
                new_info.gid = orig_member.gid
                new_info.uname = str(orig_member.uid)
                new_info.gname = str(orig_member.gid)
                new_info.size = orig_member.size
                new_info.mtime = orig_member.mtime
                new_info.linkname = orig_member.linkname
                new_info.devmajor = orig_member.devmajor
                new_info.devminor = orig_member.devminor
                pax_headers = dict(item["pax_headers"])
                pax_headers.setdefault("uid", str(orig_member.uid))
                pax_headers.setdefault("gid", str(orig_member.gid))
                if pax_headers:
                    new_info.pax_headers = pax_headers

                if orig_member.isreg() and item["content"] is not None:
                    out_tar.addfile(new_info, io.BytesIO(item["content"]))
                else:
                    out_tar.addfile(new_info)

    return out_buf.getvalue()


def build_layer_filesystem(
    tar_bytes: bytes, target_fs: str = "squashfs", out_path: Path | None = None
) -> Path:
    """Build candidate lower filesystem image (SquashFS or EROFS) from OCI tar stream."""
    check_platform_support()

    translated_tar = translate_oci_tar_to_overlay_tar(tar_bytes)

    auto_created = False
    if out_path is None:
        tmp_fd, tmp_file = tempfile.mkstemp(prefix=f"oci_layer_{target_fs}_", suffix=f".{target_fs}")
        os.close(tmp_fd)
        out_path = Path(tmp_file)
        auto_created = True

    try:
        if target_fs == "squashfs":
            mksquashfs_bin = shutil.which("mksquashfs")
            if not mksquashfs_bin:
                raise UnsupportedPlatformError("mksquashfs tool is not installed or not in PATH")

            cmd = [mksquashfs_bin, "-", str(out_path), "-tar", "-noappend", "-xattrs"]
            proc = subprocess.run(
                cmd, input=translated_tar, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
            )
            if proc.returncode != 0:
                raise ArtifactValidationError(
                    f"mksquashfs failed with code {proc.returncode}: {proc.stderr.decode('utf-8', errors='replace')}"
                )
        elif target_fs == "erofs":
            mkerofs_bin = shutil.which("mkfs.erofs")
            if not mkerofs_bin:
                raise UnsupportedPlatformError("mkfs.erofs tool is not installed or not in PATH")

            # mkfs.erofs --tar=file image output
            tmp_tar_fd, tmp_tar_path = tempfile.mkstemp(prefix="oci_layer_erofs_tar_", suffix=".tar")
            os.close(tmp_tar_fd)
            try:
                Path(tmp_tar_path).write_bytes(translated_tar)
                cmd = [mkerofs_bin, f"--tar=file", str(out_path), tmp_tar_path]
                proc = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
                )
                if proc.returncode != 0:
                    raise ArtifactValidationError(
                        f"mkfs.erofs failed with code {proc.returncode}: {proc.stderr.decode('utf-8', errors='replace')}"
                    )
            finally:
                if os.path.exists(tmp_tar_path):
                    os.unlink(tmp_tar_path)
        else:
            raise ValueError(f"Unsupported filesystem type: {target_fs}")
    except Exception:
        if auto_created and out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        raise
    return out_path


def probe_oci_filesystem_semantics(
    base_tar: bytes, leaf_tar: bytes, expected_receipt: dict
) -> str:
    """Linux privileged probe for 2-layer OCI changeset OverlayFS semantics.

    Probes SquashFS first, then EROFS. Records passing choice in global OCI_LAYER_FILESYSTEM.
    """
    check_platform_support()

    if os.geteuid() != 0:
        raise UnsupportedPlatformError("OCI layer filesystem probing requires root privileges for mounting")

    is_budget_ok, opt_len, budget = check_lowerdir_page_budget(128)
    if not is_budget_ok:
        raise ArtifactValidationError(
            f"128-lowerdir option string length ({opt_len} bytes) exceeds 75% page-size budget ({budget} bytes)"
        )

    candidates = ["squashfs", "erofs"]
    probe_errors: list[str] = []

    for fs in candidates:
        if not shutil.which("mksquashfs" if fs == "squashfs" else "mkfs.erofs"):
            probe_errors.append(f"{fs}: tool missing")
            continue

        with tempfile.TemporaryDirectory(prefix=f"oci_probe_{fs}_") as tmpdir:
            tmp_path = Path(tmpdir)
            base_img = tmp_path / f"base.{fs}"
            leaf_img = tmp_path / f"leaf.{fs}"
            base_mnt = tmp_path / "mnt_base"
            leaf_mnt = tmp_path / "mnt_leaf"
            overlay_mnt = tmp_path / "mnt_overlay"

            base_mnt.mkdir()
            leaf_mnt.mkdir()
            overlay_mnt.mkdir()

            try:
                build_layer_filesystem(base_tar, target_fs=fs, out_path=base_img)
                build_layer_filesystem(leaf_tar, target_fs=fs, out_path=leaf_img)
            except Exception as exc:
                probe_errors.append(f"{fs} build failed: {exc}")
                continue

            # Mount lower layers read-only and nodev
            mount_bin = shutil.which("mount")
            umount_bin = shutil.which("umount")
            if not mount_bin or not umount_bin:
                raise UnsupportedPlatformError("mount/umount tools missing")

            base_mounted = False
            leaf_mounted = False
            overlay_mounted = False

            try:
                res_base = subprocess.run(
                    [mount_bin, "-t", fs, "-o", "ro,nodev", str(base_img), str(base_mnt)],
                    capture_output=True,
                )
                if res_base.returncode != 0:
                    probe_errors.append(f"{fs} mount base failed: {res_base.stderr.decode()}")
                    continue
                base_mounted = True

                res_leaf = subprocess.run(
                    [mount_bin, "-t", fs, "-o", "ro,nodev", str(leaf_img), str(leaf_mnt)],
                    capture_output=True,
                )
                if res_leaf.returncode != 0:
                    probe_errors.append(f"{fs} mount leaf failed: {res_leaf.stderr.decode()}")
                    continue
                leaf_mounted = True

                res_ovl = subprocess.run(
                    [
                        mount_bin,
                        "-t",
                        "overlay",
                        "overlay",
                        "-o",
                        f"lowerdir={leaf_mnt}:{base_mnt}",
                        str(overlay_mnt),
                    ],
                    capture_output=True,
                )
                if res_ovl.returncode != 0:
                    probe_errors.append(f"{fs} overlay mount failed: {res_ovl.stderr.decode()}")
                    continue
                overlay_mounted = True

                # 1. Verify nodev lower mount keeps device entries inert
                dev_opened = False
                for dev_name in ["char_dev", "block_dev"]:
                    for mnt_dir in (base_mnt, leaf_mnt):
                        dev_p = mnt_dir / dev_name
                        if os.path.lexists(dev_p):
                            try:
                                with open(dev_p, "rb") as dev_f:
                                    dev_f.read(1)
                                    probe_errors.append(
                                        f"{fs}: device {dev_name} in {mnt_dir.name} opened on nodev mount!"
                                    )
                                    dev_opened = True
                                    break
                            except (PermissionError, OSError):
                                pass
                    if dev_opened:
                        break

                if dev_opened:
                    continue

                # 2. Verify merged tree matches expected receipt
                receipt_ok, diff_msg = _verify_merged_tree(
                    overlay_mnt, expected_receipt.get("entries", {})
                )
                if not receipt_ok:
                    probe_errors.append(f"{fs} receipt mismatch: {diff_msg}")
                    continue

                # All assertions passed! Establish selection
                global OCI_LAYER_FILESYSTEM
                OCI_LAYER_FILESYSTEM = fs
                return fs
            finally:
                if overlay_mounted:
                    subprocess.run([umount_bin, str(overlay_mnt)], capture_output=True)
                if leaf_mounted:
                    subprocess.run([umount_bin, str(leaf_mnt)], capture_output=True)
                if base_mounted:
                    subprocess.run([umount_bin, str(base_mnt)], capture_output=True)

    raise ArtifactValidationError(
        "OCI layer filesystem probe failed for all candidate filesystems. Reasons: " + "; ".join(probe_errors)
    )


def _verify_merged_tree(mnt: Path, expected_entries: dict) -> tuple[bool, str]:
    """Verify mounted OverlayFS directory tree matches expected receipt entries."""
    observed: set[str] = set()

    for root, dirs, files in os.walk(mnt):
        rel_root = Path(root).relative_to(mnt)

        for d in dirs:
            rel_path = str(rel_root / d) if str(rel_root) != "." else d
            observed.add(rel_path)

        for f in files:
            rel_path = str(rel_root / f) if str(rel_root) != "." else f
            observed.add(rel_path)

    expected_keys = set(expected_entries.keys())

    # Check for whiteouts: entries in expected_entries must exist; entries omitted must NOT exist
    missing = expected_keys - observed
    if missing:
        return (False, f"Missing expected entries in merged tree: {sorted(missing)}")

    unexpected = observed - expected_keys
    if unexpected:
        return (
            False,
            f"Unexpected/unhidden entries in merged tree (whiteout failure): {sorted(unexpected)}",
        )

    seen_inode_groups: dict[str, int] = {}

    for rel_path, exp in expected_entries.items():
        p = mnt / rel_path

        if not os.path.lexists(p):
            return (False, f"Path {rel_path} does not exist in merged tree")

        try:
            st = p.lstat()
        except OSError as e:
            return (False, f"Failed to lstat {rel_path}: {e}")

        exp_type = exp.get("type")

        if exp_type == "file" and not stat.S_ISREG(st.st_mode):
            return (False, f"{rel_path} is not regular file")
        elif exp_type == "dir" and not stat.S_ISDIR(st.st_mode):
            return (False, f"{rel_path} is not directory")
        elif exp_type == "symlink" and not stat.S_ISLNK(st.st_mode):
            return (False, f"{rel_path} is not symlink")
        elif exp_type == "fifo" and not stat.S_ISFIFO(st.st_mode):
            return (False, f"{rel_path} is not FIFO")
        elif exp_type == "chardev" and not stat.S_ISCHR(st.st_mode):
            return (False, f"{rel_path} is not char device")
        elif exp_type == "blkdev" and not stat.S_ISBLK(st.st_mode):
            return (False, f"{rel_path} is not block device")

        if "mode" in exp:
            exp_mode_bits = exp["mode"] & 0o7777
            act_mode_bits = st.st_mode & 0o7777
            if act_mode_bits != exp_mode_bits:
                return (
                    False,
                    f"{rel_path} mode mismatch: expected {oct(exp_mode_bits)}, got {oct(act_mode_bits)}",
                )

        if "uid" in exp and st.st_uid != exp["uid"]:
            return (False, f"{rel_path} uid mismatch: expected {exp['uid']}, got {st.st_uid}")

        if "gid" in exp and st.st_gid != exp["gid"]:
            return (False, f"{rel_path} gid mismatch: expected {exp['gid']}, got {st.st_gid}")

        if "mtime" in exp:
            exp_mtime = float(exp["mtime"])
            act_mtime = float(st.st_mtime)
            if abs(act_mtime - exp_mtime) >= 1.0:
                return (
                    False,
                    f"{rel_path} mtime mismatch: expected {exp_mtime}, got {act_mtime}",
                )

        if exp_type in ("chardev", "blkdev"):
            if "rdev_major" in exp:
                act_major = os.major(st.st_rdev)
                if act_major != exp["rdev_major"]:
                    return (
                        False,
                        f"{rel_path} major device number mismatch: expected {exp['rdev_major']}, got {act_major}",
                    )
            if "rdev_minor" in exp:
                act_minor = os.minor(st.st_rdev)
                if act_minor != exp["rdev_minor"]:
                    return (
                        False,
                        f"{rel_path} minor device number mismatch: expected {exp['rdev_minor']}, got {act_minor}",
                    )

        if exp_type == "symlink" and "target" in exp:
            try:
                target = os.readlink(p)
            except OSError as e:
                return (False, f"Failed to readlink {rel_path}: {e}")
            if target != exp["target"]:
                return (
                    False,
                    f"{rel_path} symlink target mismatch: expected {exp['target']}, got {target}",
                )

        if exp_type == "file" and "sha256" in exp:
            try:
                actual_hash = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError as e:
                return (False, f"Failed to read {rel_path} for sha256 check: {e}")
            if actual_hash != exp["sha256"]:
                return (
                    False,
                    f"{rel_path} content hash mismatch: expected {exp['sha256']}, got {actual_hash}",
                )

        link_target = exp.get("link_target") or exp.get("hardlink_target")
        if link_target:
            target_p = mnt / link_target
            if not os.path.lexists(target_p):
                return (False, f"Hardlink target {link_target} for {rel_path} does not exist")
            try:
                target_st = target_p.lstat()
            except OSError as e:
                return (False, f"Failed to lstat hardlink target {link_target} for {rel_path}: {e}")
            if st.st_ino != target_st.st_ino:
                return (
                    False,
                    f"{rel_path} hardlink inode mismatch with {link_target}: {st.st_ino} != {target_st.st_ino}",
                )

        if "inode_group" in exp:
            group = str(exp["inode_group"])
            if group in seen_inode_groups:
                if st.st_ino != seen_inode_groups[group]:
                    return (
                        False,
                        f"{rel_path} inode group {group} mismatch: {st.st_ino} != {seen_inode_groups[group]}",
                    )
            else:
                seen_inode_groups[group] = st.st_ino

        expected_xattrs = exp.get("xattrs")
        if expected_xattrs:
            if not (hasattr(os, "getxattr") and hasattr(os, "listxattr")):
                return (False, f"Platform does not support xattr verification for {rel_path}")

            for attr_name, exp_val in expected_xattrs.items():
                try:
                    act_val_bytes = os.getxattr(p, attr_name, follow_symlinks=False)
                except OSError as e:
                    return (False, f"{rel_path} xattr {attr_name} read error: {e}")

                exp_val_bytes = (
                    exp_val.encode("utf-8") if isinstance(exp_val, str) else bytes(exp_val)
                )
                if act_val_bytes != exp_val_bytes:
                    return (
                        False,
                        f"{rel_path} xattr {attr_name} mismatch: expected {exp_val_bytes!r}, got {act_val_bytes!r}",
                    )
    return (True, "OK")
