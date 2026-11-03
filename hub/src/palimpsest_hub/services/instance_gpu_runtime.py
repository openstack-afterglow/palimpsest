"""Guest-only vendor runtime injection; this module is embedded, never imported there.

The content-pinned base must supply libnvidia-container >= 1.18 with its
rootfs-confining path_resolve_full implementation. We do not discover/copy host
libraries or execute image hooks. Whole-VM trust is not container isolation.
"""

from typing import Literal, get_args

GpuErrorCode = Literal[
    "gpu_vendor_command_timeout",
    "gpu_vendor_output_limit",
    "gpu_vendor_command_failed",
    "gpu_vendor_tools_unavailable",
    "gpu_vendor_tools_unqualified",
    "gpu_vendor_version_unqualified",
    "gpu_device_count_mismatch",
    "gpu_driver_or_uuid_mismatch",
    "gpu_duplicate_uuid",
    "gpu_mount_inventory_limit",
    "gpu_unsafe_mountpoint",
    "gpu_vendor_mount_escaped",
    "gpu_vendor_injection_empty",
    "gpu_runtime_mode_mismatch",
    "gpu_injected_runtime_mismatch",
    "gpu_mount_identity_limit",
    "gpu_mount_identity_unavailable",
    "gpu_mount_cleanup_failed",
    "gpu_children_not_quiescent",
]
GPU_ERROR_CODES = get_args(GpuErrorCode)

GPU_RUNTIME = (
    "GPU_ERROR_CODES = "
    + repr(GPU_ERROR_CODES)
    + "\n"
    + r"""
def gpu_become_subreaper():
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'gpu_subreaper_unavailable')


def gpu_reap_children():
    import signal
    until = time.monotonic() + 5
    while time.monotonic() < until:
        with open('/proc/self/task/' + str(os.getpid()) + '/children', 'rb') as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise RuntimeError('gpu_children_not_quiescent')
        children = [int(item) for item in data.split()]
        if not children:
            return
        for child in children:
            # These are unreaped direct children: their IDs cannot be recycled
            # before this single-threaded parent calls waitpid below.
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for child in children:
            try:
                os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                pass
        time.sleep(0.01)  # Orphans reparent to the subreaper; drain until empty.
    raise RuntimeError('gpu_children_not_quiescent')


def gpu_capture(argv, enter=None, environment=None):
    import selectors
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=enter,
                             env=environment or {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    selected = selectors.DefaultSelector()
    selected.register(child.stdout, selectors.EVENT_READ)
    result = bytearray()
    until = time.monotonic() + 30
    try:
        while selected.get_map():
            if time.monotonic() >= until:
                raise RuntimeError('gpu_vendor_command_timeout')
            for key, _ in selected.select(0.2):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selected.unregister(key.fileobj)
                else:
                    result.extend(chunk)
                    if len(result) > 16384:
                        raise RuntimeError('gpu_vendor_output_limit')
        if child.wait(timeout=max(0.1, until - time.monotonic())) != 0:
            raise RuntimeError('gpu_vendor_command_failed')
        return result.decode('ascii', 'strict')
    finally:
        import signal
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()
        selected.close()


def gpu_prerequisites(gpu):
    import re, stat
    for tool in ('/usr/bin/nvidia-smi', '/usr/bin/nvidia-container-cli', '/sbin/ldconfig'):
        try:
            target = pathlib.Path(tool).resolve(strict=True)
            mode = target.stat()
        except OSError:
            raise RuntimeError('gpu_vendor_tools_unavailable') from None
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != 0 or mode.st_mode & 0o022 or not os.access(target, os.X_OK):
            raise RuntimeError('gpu_vendor_tools_unqualified')
    version = gpu_capture(['/usr/bin/nvidia-container-cli', '--version'])
    for component in ('cli-version', 'lib-version'):
        match = re.search(r'^' + component + r':\s*([0-9]+)\.([0-9]+)\.([0-9]+)\s*$', version, re.M)
        if match is None or tuple(map(int, match.groups())) < (1, 18, 0):
            raise RuntimeError('gpu_vendor_version_unqualified')
    observation = gpu_capture(['/usr/bin/nvidia-smi', '--query-gpu=uuid,driver_version', '--format=csv,noheader,nounits'])
    records = [line.strip().split(',') for line in observation.splitlines() if line.strip()]
    if len(records) != gpu['device_count']:
        raise RuntimeError('gpu_device_count_mismatch')
    uuids = []
    for row in records:
        if (len(row) != 2 or re.fullmatch(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', row[0].strip()) is None
                or row[1].strip() != gpu['driver_version']):
            raise RuntimeError('gpu_driver_or_uuid_mismatch')
        uuids.append(row[0].strip())
    if len(set(uuids)) != len(uuids):
        raise RuntimeError('gpu_duplicate_uuid')
    return uuids


def gpu_verify_injected(gpu, uuids, enter, environment):
    observation = gpu_capture(['/usr/bin/nvidia-smi', '--query-gpu=uuid,driver_version',
                               '--format=csv,noheader,nounits'], enter, environment)
    rows = [tuple(part.strip() for part in line.split(',')) for line in observation.splitlines() if line.strip()]
    if len(rows) != len(uuids) or set(rows) != {(identity, gpu['driver_version']) for identity in uuids}:
        raise RuntimeError('gpu_injected_runtime_mismatch')


def gpu_mount_table():
    import re
    with open('/proc/self/mountinfo', 'rb') as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise RuntimeError('gpu_mount_inventory_limit')
    records = {}
    for line in data.decode('utf-8', 'strict').splitlines():
        fields = line.split(' ')
        target = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4])
        records[int(fields[0])] = pathlib.Path(target)
    return records


def gpu_track_mounts(before, merged, owned):
    current = gpu_mount_table()
    escaped = False
    for identity, path in current.items():
        if identity in before or identity in owned:
            continue
        # Kernel-reported absolute mountpoints, not caller paths or vendor logs.
        if merged not in path.parents:
            escaped = True
        # Namespace-private newly created mounts are ours even on vendor escape;
        # capture IDs for bounded cleanup, then fail without starting the image.
        owned[identity] = path
    if escaped:
        raise RuntimeError('gpu_vendor_mount_escaped')


def gpu_configure(gpu, uuids, merged, owned):
    # Explicit managed mountpoints cannot be redirected into the guest base.
    for name in ('dev', 'proc', 'sys', 'etc'):
        path = merged / name
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError('gpu_unsafe_mountpoint')
    before = gpu_mount_table()
    try:
        gpu_capture(['/usr/bin/nvidia-container-cli', 'configure', '--no-cgroups', '--compute', '--utility',
                     '--no-persistenced', '--no-fabricmanager', '--ldconfig=@/sbin/ldconfig',
                     '--device=' + ','.join(uuids), str(merged)])
    finally:
        # Partial vendor failure must retain the exact newly owned mount IDs too.
        gpu_track_mounts(before, merged, owned)
    if not owned:
        raise RuntimeError('gpu_vendor_injection_empty')


def gpu_resolved_mount(path):
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        with open('/proc/self/fdinfo/' + str(fd), 'rb') as stream:
            info = stream.read(8193)
        if len(info) > 8192:
            raise RuntimeError('gpu_mount_identity_limit')
        identities = [line.split(b':', 1)[1].strip() for line in info.splitlines() if line.startswith(b'mnt_id:')]
        if len(identities) != 1 or not identities[0].isdigit():
            raise RuntimeError('gpu_mount_identity_unavailable')
        return int(identities[0])
    finally:
        os.close(fd)  # Keeping the victim mount pinned would make normal umount EBUSY.


def gpu_unmount_owned(path, identity):
    # The private namespace has no live workload/vendor children at cleanup.
    # Pin only the parent mount, then revalidate both resolution and mountinfo.
    parent = os.open(path.parent, os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        target = pathlib.Path('/proc/self/fd/' + str(parent)) / path.name
        if gpu_resolved_mount(target) != identity or gpu_mount_table().get(identity) != path:
            raise RuntimeError('gpu_mount_identity_unavailable')
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        unmount = libc.umount2
        unmount.argtypes = [ctypes.c_char_p, ctypes.c_int]
        unmount.restype = ctypes.c_int
        if unmount(os.fsencode(target), 0) != 0:
            raise OSError(ctypes.get_errno(), 'gpu_mount_unmount_failed')
    finally:
        os.close(parent)


def gpu_cleanup(owned):
    # Depth orders descendants, but mount IDs NEVER imply same-path stack order.
    paths = sorted(set(owned.values()), key=lambda path: len(path.parts), reverse=True)
    for _ in range(len(owned)):
        progress = False
        for path in paths:
            current = gpu_mount_table()
            if not any(identity in current and current[identity] == path
                       for identity, original in owned.items() if original == path):
                continue
            try:
                identity = gpu_resolved_mount(path)
                if identity not in owned or owned[identity] != path or current.get(identity) != path:
                    continue  # An unowned overmount must never be removed.
                gpu_unmount_owned(path, identity)
                if identity in gpu_mount_table():
                    continue  # A successful syscall is not proof of disappearance.
                progress = True
            except Exception:
                continue
        if not progress:
            break
    if set(owned).intersection(gpu_mount_table()):
        raise RuntimeError('gpu_mount_cleanup_failed')
"""
)
