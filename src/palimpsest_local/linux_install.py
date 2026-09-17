"""Root-only Linux provisioning for the dedicated Palimpsest service identity."""

from __future__ import annotations

import grp
import os
import pwd
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .errors import PalimpsestError

ACCOUNT = "palimpsest"
STATE_ROOT = Path("/var/lib/palimpsest")
LOG_ROOT = Path("/var/log/palimpsest")
HOME = STATE_ROOT
NOLOGIN_SHELLS = frozenset({"/sbin/nologin", "/usr/sbin/nologin", "/bin/false"})
ADMIN_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def _tool(name: str) -> str:
    for parent in ADMIN_PATH.split(":"):
        candidate = Path(parent) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise PalimpsestError(f"required Linux account tool is unavailable: {name}")


def _run(command: Sequence[str]) -> None:
    try:
        result = subprocess.run(
            tuple(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            env={"PATH": ADMIN_PATH, "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PalimpsestError(
            "Linux account provisioning command did not complete; inspect the partial identity before retrying"
        ) from exc
    if result.returncode != 0:
        raise PalimpsestError("Linux account provisioning command failed; inspect the partial identity before retrying")


def _lookup_group() -> grp.struct_group | None:
    try:
        return grp.getgrnam(ACCOUNT)
    except KeyError:
        return None


def _lookup_user() -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(ACCOUNT)
    except KeyError:
        return None


def _validate_group(group: grp.struct_group) -> None:
    if group.gr_name != ACCOUNT or group.gr_gid <= 0:
        raise PalimpsestError("existing palimpsest group conflicts with the required system identity")


def _validate_user(user: pwd.struct_passwd, group: grp.struct_group) -> None:
    if (
        user.pw_name != ACCOUNT
        or user.pw_uid <= 0
        or user.pw_gid != group.gr_gid
        or user.pw_dir != str(HOME)
        or user.pw_shell not in NOLOGIN_SHELLS
    ):
        raise PalimpsestError("existing palimpsest user conflicts with the required system identity")


def _validate_ancestor(path: Path) -> None:
    current = Path("/")
    for component in path.parent.parts[1:]:
        current /= component
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise PalimpsestError("Palimpsest system-directory ancestor is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise PalimpsestError("Palimpsest system-directory ancestor is untrusted")


def _inspect_directory(path: Path, uid: int, gid: int) -> bool:
    _validate_ancestor(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PalimpsestError("cannot inspect Palimpsest system directory") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or info.st_gid != gid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise PalimpsestError("existing Palimpsest system directory conflicts with required ownership")
    return True


def _ensure_directory(path: Path, uid: int, gid: int) -> None:
    if not _inspect_directory(path, uid, gid):
        descriptor = -1
        try:
            os.mkdir(path, 0o700)
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            created = os.fstat(descriptor)
            visible = os.lstat(path)
            if (
                not stat.S_ISDIR(created.st_mode)
                or created.st_uid != 0
                or (created.st_dev, created.st_ino) != (visible.st_dev, visible.st_ino)
            ):
                raise PalimpsestError("Palimpsest system directory changed during provisioning")
            os.fchmod(descriptor, 0o700)
            os.fchown(descriptor, uid, gid)
            os.fsync(descriptor)
        except OSError as exc:
            raise PalimpsestError("cannot provision Palimpsest system directory") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    after = os.lstat(path)
    if (
        not stat.S_ISDIR(after.st_mode)
        or after.st_uid != uid
        or after.st_gid != gid
        or stat.S_IMODE(after.st_mode) != 0o700
        or ("created" in locals() and (after.st_dev, after.st_ino) != (created.st_dev, created.st_ino))
    ):
        raise PalimpsestError("Palimpsest system directory changed during provisioning")


def _target_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PalimpsestError("cannot inspect Palimpsest system directory") from exc
    return True


def provision() -> None:
    if not sys.platform.startswith("linux"):
        raise PalimpsestError("Linux provisioning is supported only on Linux")
    if os.geteuid() != 0:
        raise PalimpsestError("Linux provisioning must run as root")

    for path in (STATE_ROOT, LOG_ROOT):
        _validate_ancestor(path)

    group = _lookup_group()
    user = _lookup_user()
    if (group is None) != (user is None):
        raise PalimpsestError("existing palimpsest account and group are incomplete")
    if group is not None:
        _validate_group(group)
        assert user is not None
        _validate_user(user, group)
    elif any(_target_exists(path) for path in (STATE_ROOT, LOG_ROOT)):
        raise PalimpsestError("existing Palimpsest system directory has no matching service identity")

    group_tool = user_tool = shell = None
    if group is None:
        group_tool = _tool("groupadd")
        user_tool = _tool("useradd")
        shell = next(
            (value for value in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false") if Path(value).is_file()),
            None,
        )
        if shell is None:
            raise PalimpsestError("no supported no-login shell is available")

    if group is None:
        assert group_tool is not None
        _run((group_tool, "--system", ACCOUNT))
        group = _lookup_group()
        if group is None:
            raise PalimpsestError("palimpsest group was not created")
    _validate_group(group)

    if user is None:
        assert user_tool is not None and shell is not None
        _run(
            (
                user_tool,
                "--system",
                "--gid",
                ACCOUNT,
                "--home-dir",
                str(HOME),
                "--no-create-home",
                "--shell",
                shell,
                ACCOUNT,
            )
        )
        user = _lookup_user()
        if user is None:
            raise PalimpsestError("palimpsest user was not created")
    _validate_user(user, group)

    for path in (STATE_ROOT, LOG_ROOT):
        _inspect_directory(path, user.pw_uid, group.gr_gid)
    _ensure_directory(STATE_ROOT, user.pw_uid, group.gr_gid)
    _ensure_directory(LOG_ROOT, user.pw_uid, group.gr_gid)


def main() -> int:
    try:
        provision()
    except PalimpsestError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Palimpsest Linux service identity and storage roots are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
