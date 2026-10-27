"""Pure cloud-init exporter for operator-authored external runtime layer policy.

The exporter provisions no servers or disks. The referenced ext4 filesystem must
already contain the digest-addressed SquashFS blobs; it is never formatted.
Shared stores require precreated mountpoints and block-level read-only attachment.
"""

from __future__ import annotations

import json
import re
import uuid

from .errors import ArtifactValidationError

SCHEMA = "palimpsest.external-layer-runtime.v2"
RUNTIME_ROOT = "/srv/palimpsest"
HELPER_PATH = "/usr/libexec/palimpsest-external-layers"
UNIT_NAME = "palimpsest-external-layers.service"


def _fail(message: str) -> None:
    raise ArtifactValidationError(f"external layer runtime: {message}")


def _keys(value: object, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{label} must be an object")
    if not required <= value.keys() or value.keys() - required - optional:
        _fail(f"{label} has missing or unknown fields")
    return value


def _path(value: object, *, absolute: bool, label: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") != absolute:
        _fail(f"{label} must be a {'absolute' if absolute else 'relative'} path")
    # Delimiters used by mount, ldconfig and shell PATH are deliberately excluded.
    if re.fullmatch(r"/?[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*", value) is None:
        _fail(f"{label} must be a normalized safe path")
    if any(part in {".", ".."} for part in value.split("/")):
        _fail(f"{label} must not contain traversal")
    return value


def _inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent + "/")


def _validate(manifest: object) -> dict:
    item = _keys(
        manifest,
        {"schema", "arch", "filesystem_uuid", "stacks"},
        {
            "exports",
            "paths",
            "library_paths",
            "activation_commands",
            "checks",
            "storage_read_only",
            "command_links",
            "blob_paths",
            "consumer_state_root",
        },
        "manifest",
    )
    if item["schema"] != SCHEMA:
        _fail(f"schema must be {SCHEMA}")
    if item["arch"] not in ("x86_64", "aarch64"):
        _fail("arch must be x86_64 or aarch64")
    filesystem_uuid = item["filesystem_uuid"]
    try:
        valid_uuid = isinstance(filesystem_uuid, str) and str(uuid.UUID(filesystem_uuid)) == filesystem_uuid
    except (ValueError, AttributeError):
        valid_uuid = False
    if not valid_uuid:
        _fail("filesystem_uuid must be a canonical lowercase UUID")
    storage_read_only = item.get("storage_read_only", False)
    if not isinstance(storage_read_only, bool):
        _fail("storage_read_only must be a boolean")
    state_root = _path(
        item.get("consumer_state_root", "/var/lib/palimpsest/consumer"), absolute=True, label="consumer_state_root"
    )
    if len(state_root) > 255:
        _fail("consumer_state_root must be at most 255 characters")
    if not state_root.startswith("/var/lib/") and not state_root.startswith("/mnt/"):
        _fail("consumer_state_root must be strictly under /var/lib or /mnt")
    if _inside(state_root, RUNTIME_ROOT):
        _fail("consumer state must not be in the lower store")
    stacks = item["stacks"]
    if not isinstance(stacks, list) or not stacks:
        _fail("stacks must be a nonempty list")
    names: set[str] = set()
    result = {
        "schema": SCHEMA,
        "arch": item["arch"],
        "filesystem_uuid": filesystem_uuid,
        "storage_read_only": storage_read_only,
        "consumer_state_root": state_root,
        "stacks": [],
    }
    for stack in stacks:
        stack = _keys(stack, {"name", "layers"}, set(), "stack")
        name = stack["name"]
        if not isinstance(name, str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None or len(name) > 255:
            _fail("stack name must be a lowercase slug of at most 255 characters")
        if name in names:
            _fail(f"duplicate stack name: {name}")
        names.add(name)
        layers = stack["layers"]
        if not isinstance(layers, list) or not layers:
            _fail(f"stack {name} requires nonempty layers")
        if any(
            not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None for digest in layers
        ):
            _fail(f"stack {name} requires canonical sha256 digests")
        if len(set(layers)) != len(layers):
            _fail(f"stack {name} repeats a layer")
        result["stacks"].append({"name": name, "layers": list(layers)})
        options = "rw,nosuid,nodev,lowerdir=" + ":".join(f"{RUNTIME_ROOT}/layers/{digest[7:]}" for digest in layers)
        options += f",upperdir={state_root}/{name}/upper,workdir={state_root}/{name}/work"
        if len(options.encode()) >= 4096:
            _fail(f"stack {name} exceeds the bounded kernel mount option size")
    blob_paths = item.get("blob_paths", {})
    digests = {digest for stack in result["stacks"] for digest in stack["layers"]}
    if not isinstance(blob_paths, dict) or set(blob_paths) - digests:
        _fail("blob_paths must bind only declared stack digests")
    result["blob_paths"] = {}
    for digest, path in blob_paths.items():
        path = _path(path, absolute=False, label="blob path")
        if path.split("/")[-1] != digest.split(":")[1]:
            _fail("blob path must end in its full digest identity")
        result["blob_paths"][digest] = path
    exports = item.get("exports", [])
    if not isinstance(exports, list):
        _fail("exports must be a list")
    result["exports"] = []
    targets: list[str] = []
    for export in exports:
        export = _keys(export, {"stack", "path", "target"}, set(), "export")
        if not isinstance(export["stack"], str) or export["stack"] not in names:
            _fail("export references an unknown stack")
        path = _path(export["path"], absolute=False, label="export path")
        target = _path(export["target"], absolute=True, label="export target")
        if not (target.startswith("/opt/") or target.startswith("/usr/local/")):
            _fail("export target must be strictly under /opt or /usr/local")
        if any(_inside(target, previous) or _inside(previous, target) for previous in targets):
            _fail("export targets must not overlap")
        targets.append(target)
        if _inside(target, state_root) or _inside(state_root, target):
            _fail("export target must not overlap consumer state")
        result["exports"].append({"stack": export["stack"], "path": path, "target": target})
    allowed = targets + [f"{RUNTIME_ROOT}/runtimes/{name}" for name in names]
    for key in ("paths", "library_paths"):
        values = item.get(key, [])
        if not isinstance(values, list):
            _fail(f"{key} must be a list")
        normalized = [_path(value, absolute=True, label=key) for value in values]
        if any(not any(_inside(value, prefix) for prefix in allowed) for value in normalized):
            _fail(f"{key} must be inside a configured export or runtime stack")
        if len(set(normalized)) != len(normalized):
            _fail(f"{key} contains duplicate directories")
        result[key] = normalized
    links = item.get("command_links", {})
    if not isinstance(links, dict):
        _fail("command_links must be a command-name to exported-path map")
    result["command_links"] = {}
    for name, target in links.items():
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", name) is None:
            _fail("command link name is invalid")
        target = _path(target, absolute=True, label="command link target")
        if not any(_inside(target, prefix) for prefix in allowed):
            _fail("command link must reference a configured export or runtime stack")
        result["command_links"][name] = target
    for key in ("activation_commands", "checks"):
        commands = item.get(key, [])
        if not isinstance(commands, list):
            _fail(f"{key} must be a list")
        normalized = []
        for command in commands:
            if (
                not isinstance(command, list)
                or not command
                or not command[0]
                or any(not isinstance(arg, str) or any(c in arg for c in "\x00\r\n") for arg in command)
            ):
                _fail(f"{key} entries must be nonempty argv arrays of NUL/newline-free strings")
            normalized.append(list(command))
        result[key] = normalized
    return result


_GUEST_SOURCE = r'''#!/usr/bin/python3
"""Activate only the exact externally supplied root boot policy. No repair paths."""
import hashlib
import fcntl
import json
import os
import platform
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path('/srv/palimpsest')
PROFILE = Path('/etc/profile.d/palimpsest-runtime.sh')
LIBRARIES = Path('/etc/ld.so.conf.d/palimpsest-runtime.conf')
BASHRC = Path('/etc/bash.bashrc')
COMMAND_DIRECTORY = Path('/usr/local/bin')
BASE_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'


def refuse(message):
    raise RuntimeError('external layer runtime: ' + message)


def run(argv, *, capture=False, env=None):
    return subprocess.run(argv, check=True, stdin=subprocess.DEVNULL,
                          text=True, stdout=subprocess.PIPE if capture else None,
                          env=env or {'PATH': BASE_PATH, 'LANG': 'C'}).stdout


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def mounts():
    result = {}
    protected = [str(ROOT), POLICY['consumer_state_root']] + [item['target'] for item in POLICY['exports']]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields, fs = left.split(), right.split()
        target = unescape(fields[4])
        if target in result and any(target == prefix or target.startswith(prefix + '/') for prefix in protected):
            refuse('stacked mounts at ' + target)
        result[target] = {'device': fields[2], 'root': unescape(fields[3]),
                          'options': set(fields[5].split(',')), 'type': fs[0],
                          'source': unescape(fs[1]), 'super': unescape(fs[2]).split(',')}
    return result


def directory(path, *, create=False):
    path = Path(path)
    # Walk every component, never following a symlink or accepting writable parents.
    for part in reversed((path, *path.parents)):
        if create and not part.exists() and not part.is_symlink():
            part.mkdir(mode=0o755)
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            refuse('directory must be real, root-owned and not group/world-writable: ' + str(part))
    return path


def empty_target(path):
    path = directory(path, create=True)
    if any(path.iterdir()):
        refuse('refusing to cover nonempty unmounted directory: ' + str(path))


def require_mount(path, kind, *, readonly=True):
    entry = mounts().get(str(path))
    options = {'nosuid', 'nodev', 'ro' if readonly else 'rw'}
    if entry is None or entry['type'] != kind or not options <= entry['options']:
        refuse('unexpected filesystem or mount options at ' + str(path))
    return entry


def verify_storage(policy):
    device = Path('/dev/disk/by-uuid') / policy['filesystem_uuid']
    real_device = device.resolve(strict=True)
    info = real_device.stat()
    if not stat.S_ISBLK(info.st_mode):
        refuse('filesystem UUID does not identify a block device')
    if run(['blkid', '-p', '-s', 'UUID', '-o', 'value', str(real_device)], capture=True).strip() != policy['filesystem_uuid']:
        refuse('external filesystem UUID mismatch')
    if run(['blkid', '-p', '-s', 'TYPE', '-o', 'value', str(real_device)], capture=True).strip() != 'ext4':
        refuse('external filesystem must be ext4')
    directory(ROOT)
    readonly = policy['storage_read_only']
    entry = require_mount(ROOT, 'ext4', readonly=readonly)
    if entry['device'] != f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}' or entry['root'] != '/':
        refuse('external mount is not the complete expected UUID filesystem')
    if readonly:
        if not {'noload', 'norecovery'} & set(entry['super']):
            refuse('shared storage must disable ext4 journal replay')
        if (Path('/sys/dev/block') / entry['device'] / 'ro').read_text().strip() != '1':
            refuse('shared storage requires a read-only block device')
    return entry


def verify_blob(path, digest, storage):
    directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            refuse('blob must be a regular root-owned immutable-to-users file: ' + str(path))
        if f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}' != storage['device']:
            refuse('blob is not on the external filesystem: ' + str(path))
        actual = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            while True:
                data = stream.read(1024 * 1024)
                if not data:
                    break
                actual.update(data)
        after = path.lstat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        ):
            refuse('blob changed while hashing: ' + str(path))
        if actual.hexdigest() != digest:
            refuse('blob digest mismatch: ' + str(path))
    finally:
        os.close(descriptor)


def verify_layer(path, blob):
    entry = require_mount(path, 'squashfs')
    if entry['root'] != '/':
        refuse('layer mount must expose the whole SquashFS: ' + str(path))
    loop = Path('/sys/dev/block') / entry['device']
    if (loop / 'ro').read_text().strip() != '1':
        refuse('layer loop device is writable: ' + str(path))
    backing = unescape((loop / 'loop/backing_file').read_text().strip())
    if not backing.startswith('/'):
        backing = '/' + backing
    if Path(backing).resolve(strict=True) != blob.resolve(strict=True):
        refuse('layer loop device references a different blob: ' + str(path))
    if (loop / 'loop/offset').read_text().strip() != '0' or (loop / 'loop/sizelimit').read_text().strip() != '0':
        refuse('layer loop device has an unexpected offset or size limit')


def consumer_state(policy, storage):
    root = Path(policy['consumer_state_root'])
    ancestor = root
    while not ancestor.exists() and not ancestor.is_symlink():
        ancestor = ancestor.parent
    directory(ancestor)
    table = mounts()
    entry = next((table[str(parent)] for parent in (root, *root.parents) if str(parent) in table), None)
    boot = table.get('/')
    if entry is None or boot is None or 'rw' not in entry['options'] or entry['type'] not in {'ext4', 'xfs'}:
        refuse('consumer state requires writable ext4 or XFS storage')
    if entry['device'] == storage['device'] or ancestor.stat().st_dev == ROOT.stat().st_dev:
        refuse('consumer state must not use the lower store filesystem')
    if policy['consumer_state_root'] == '/var/lib/palimpsest/consumer':
        if entry['device'] != boot['device'] or entry['root'] != boot['root']:
            refuse('default consumer state must use the local boot filesystem')
    elif entry['root'] != '/' or entry['device'] == boot['device']:
        refuse('explicit consumer state requires an owned separate whole filesystem')
    if not (Path('/sys/dev/block') / entry['device']).exists():
        refuse('consumer state must be on block-backed local storage')
    identity = {}
    for name, paths, pattern in (
        ('machine', ['/etc/machine-id'], r'[0-9a-f]{32}'),
        ('vm', ['/sys/class/dmi/id/product_uuid', '/sys/firmware/devicetree/base/vm,uuid'], r'[0-9a-f-]{36}'),
    ):
        value = next((Path(path).read_text().strip().strip('\x00').lower() for path in paths if Path(path).is_file()), '')
        if re.fullmatch(pattern, value) is None:
            refuse('missing stable consumer identity: ' + name)
        identity[name] = value
    state_uuid = run(['blkid', '-p', '-s', 'UUID', '-o', 'value', '/dev/block/' + entry['device']], capture=True).strip().lower()
    if re.fullmatch(r'[0-9a-f-]{36}', state_uuid) is None:
        refuse('consumer state filesystem requires a stable UUID')
    identity.update(filesystem_uuid=policy['filesystem_uuid'], state_uuid=state_uuid, state_root=entry['root'])
    directory(root, create=True)
    return root, identity


def identity_record(path, content):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.identity-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content.encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path, follow_symlinks=False)
        finally:
            if temporary is not None:
                temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 16384 or os.read(descriptor, 16385) != content.encode()):
            refuse('refusing to overwrite unrelated consumer identity: ' + str(path))
    finally:
        os.close(descriptor)


def stack_state(root, identity, stack):
    target = root / stack['name']
    if not target.exists() and not target.is_symlink():
        target.mkdir(mode=0o700)
    target = directory(target)
    if target.stat().st_mode & 0o077:
        refuse('consumer stack state must be root-private')
    owner = target / 'identity.json'
    record = {**identity, 'schema': 'palimpsest.consumer-state.v1', 'stack': stack['name'], 'layers': stack['layers']}
    if not owner.exists() and not owner.is_symlink() and any(target.iterdir()):
        refuse('unrecorded consumer state: ' + str(target))
    if owner.exists() or owner.is_symlink():
        info = owner.lstat()
        if info.st_size > 16384 or info.st_nlink != 1:
            refuse('unsafe consumer identity record')
    upper = directory(target / 'upper', create=not owner.exists())
    work = directory(target / 'work', create=not owner.exists())
    if upper.stat().st_dev != work.stat().st_dev or upper.stat().st_dev != root.stat().st_dev:
        refuse('upper and work must share the consumer state filesystem')
    record['upper_inode'] = upper.stat().st_ino
    record['work_inode'] = work.stat().st_ino
    identity_record(owner, json.dumps(record, sort_keys=True) + '\n')
    return upper, work

def activate_mounts(policy):
    if platform.machine() != policy['arch']:
        refuse('guest architecture mismatch: expected ' + policy['arch'] + ', got ' + platform.machine())
    storage = verify_storage(policy)
    state_root, consumer_identity = consumer_state(policy, storage)
    expected = {str(ROOT), str(state_root)}
    expected.update(str(ROOT / 'layers' / digest.split(':', 1)[1]) for stack in policy['stacks'] for digest in stack['layers'])
    expected.update(str(ROOT / 'runtimes' / stack['name']) for stack in policy['stacks'])
    expected.update(export['target'] for export in policy['exports'])
    protected = [str(ROOT), str(state_root)] + [export['target'] for export in policy['exports']]
    for target in mounts():
        if any(target == prefix or target.startswith(prefix + '/') for prefix in protected) and target not in expected:
            refuse('unexpected nested mount in runtime storage or export: ' + target)
    layers = {}
    # Validate all blob bytes before making any layer or export mount.
    for stack in policy['stacks']:
        for digest in stack['layers']:
            digest = digest.split(':', 1)[1]
            if digest not in layers:
                blob = ROOT / policy['blob_paths'].get('sha256:' + digest, 'blobs/sha256/' + digest)
                verify_blob(blob, digest, storage)
                layers[digest] = (blob, ROOT / 'layers' / digest)
    for blob, target in layers.values():
        if str(target) not in mounts():
            empty_target(target)
            run(['mount', '-t', 'squashfs', '-o', 'loop,ro,nosuid,nodev', str(blob), str(target)])
        directory(target)
        verify_layer(target, blob)
    runtime_types = {}
    for stack in policy['stacks']:
        target = ROOT / 'runtimes' / stack['name']
        runtime_types[stack['name']] = 'overlay'
        upper, work = stack_state(state_root, consumer_identity, stack)
        lowers = ':'.join(str(layers[digest.split(':', 1)[1]][1]) for digest in reversed(stack['layers']))
        if str(target) not in mounts():
            empty_target(target)
            run(['mount', '-t', 'overlay', 'overlay', '-o',
                 'rw,nosuid,nodev,lowerdir=' + lowers + ',upperdir=' + str(upper) + ',workdir=' + str(work), str(target)])
        directory(target)
        entry = require_mount(target, 'overlay', readonly=False)
        options = dict(option.split('=', 1) for option in entry['super'] if '=' in option)
        if (entry['root'] != '/' or entry['source'] != 'overlay' or options.get('lowerdir') != lowers
                or options.get('upperdir') != str(upper) or options.get('workdir') != str(work)):
            refuse('existing overlay has a different stack identity: ' + str(target))
    for export in policy['exports']:
        source = directory(ROOT / 'runtimes' / export['stack'] / export['path'])
        target = Path(export['target'])
        existing = mounts().get(str(target))
        if existing is None:
            empty_target(target)
            run(['mount', '--bind', str(source), str(target)])
            run(['mount', '-o', 'remount,bind,rw,nosuid,nodev', str(target)])
        directory(target)
        kind = runtime_types[export['stack']]
        entry = require_mount(target, kind, readonly=False)
        stack_entry = require_mount(ROOT / 'runtimes' / export['stack'], kind, readonly=False)
        source_info, target_info = source.stat(), target.stat()
        if entry['root'] != '/' + export['path'] or entry['device'] != stack_entry['device'] or (
            source_info.st_dev, source_info.st_ino
        ) != (target_info.st_dev, target_info.st_ino):
            refuse('export target has a different source identity: ' + str(target))
    for path in policy['paths'] + policy['library_paths']:
        directory(path)


def managed_file(path, content):
    directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or path.read_text() != content:
            refuse('refusing to overwrite unrelated runtime configuration: ' + str(path))
    else:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)


def configure(policy):
    prefix = ':'.join(policy['paths'])
    profile = '# Managed by palimpsest external layer runtime.\n'
    if prefix:
        quoted = shlex.quote(prefix)
        profile += ('case "${PATH:-' + BASE_PATH + '}" in\n'
                    '  ' + quoted + '|' + quoted + ':*) ;;\n'
                    '  *) PATH=' + quoted + ':"${PATH:-' + BASE_PATH + '}" ;;\n'
                    'esac\nexport PATH\n')
    managed_file(PROFILE, profile)
    managed_file(LIBRARIES, '# Managed by palimpsest external layer runtime.\n' + ''.join(path + '\n' for path in policy['library_paths']))
    # Debian/Ubuntu interactive non-login Bash does not source /etc/profile.d.
    begin = '# BEGIN palimpsest external layer runtime'
    end = '# END palimpsest external layer runtime'
    block = begin + '\n. ' + str(PROFILE) + '\n' + end + '\n'
    directory(BASHRC.parent)
    info = BASHRC.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        refuse('unsafe system Bash configuration')
    original = BASHRC.read_text()
    if begin in original or end in original:
        if original.count(begin) != 1 or original.count(end) != 1 or block not in original:
            refuse('conflicting managed Bash configuration')
    else:
        with BASHRC.open('a') as stream:
            stream.write('\n' + block)
    # SSH remote commands do not source profile.d or interactive bashrc. Publish
    # explicit native commands, not sudo/chroot wrappers or a global loader override.
    if policy['command_links']:
        directory(COMMAND_DIRECTORY, create=True)
    for name, source in policy['command_links'].items():
        target = COMMAND_DIRECTORY / name
        resolved = Path(source).resolve(strict=True)
        prefixes = [item['target'] for item in policy['exports']] + [str(ROOT / 'runtimes' / item['name']) for item in policy['stacks']]
        if not any(str(resolved).startswith(prefix + '/') for prefix in prefixes):
            refuse('command source escapes its exported runtime')
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or not info.st_mode & 0o111:
            refuse('command source must be a safe executable regular file')
        if target.is_symlink():
            if os.readlink(target) != source:
                refuse('refusing to replace an unrelated command link')
        elif target.exists():
            refuse('refusing to replace an existing native command')
        else:
            target.symlink_to(source)
    run(['ldconfig'])
    return {'PATH': (prefix + ':' if prefix else '') + BASE_PATH, 'LANG': 'C'}


def main(policy):
    if os.geteuid() != 0:
        refuse('activation requires root')
    activate_mounts(policy)
    environment = configure(policy)
    for command in policy['activation_commands']:
        run(command, env=environment)
    for command in policy['checks']:
        run(command, env=environment)


def locked_main(policy):
    directory('/run/palimpsest', create=True)
    descriptor = os.open('/run/palimpsest/external-layers.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            refuse('unsafe activation lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main(policy)
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    try:
        locked_main(POLICY)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
'''


def build_external_layer_user_data(manifest: object) -> str:
    """Render validated, account-free cloud-init data without I/O or authentication.

    Commands are explicitly trusted operator root policy, never Hub metadata.
    Optional list fields default to empty. Layer order is base to top.
    """
    policy = _validate(manifest)
    helper = _GUEST_SOURCE.replace(
        "ROOT = Path('/srv/palimpsest')",
        "POLICY = json.loads(" + repr(json.dumps(policy, sort_keys=True)) + ")\n\nROOT = Path('/srv/palimpsest')",
        1,
    )
    gate = f"[Unit]\nRequires={UNIT_NAME}\nAfter={UNIT_NAME}\n"
    systemd_arch = {"x86_64": "x86-64", "aarch64": "arm64"}[policy["arch"]]
    mount_unit = (
        "[Unit]\nDescription=Prepopulated Palimpsest external layer filesystem\n"
        f"AssertArchitecture={systemd_arch}\n"
        "[Mount]\nWhat=/dev/disk/by-uuid/" + policy["filesystem_uuid"] + "\n"
        "Where=/srv/palimpsest\nType=ext4\nOptions="
        + ("ro,noload," if policy["storage_read_only"] else "")
        + "nosuid,nodev\nTimeoutSec=90\n"
    )
    service = (
        "[Unit]\nDescription=Verify and activate external Palimpsest runtime layers\n"
        "Requires=srv-palimpsest.mount\nAfter=srv-palimpsest.mount\n"
        f"RequiresMountsFor={policy['consumer_state_root']}\n"
        "Before=ssh.service\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"ExecStart=/usr/bin/python3 {HELPER_PATH}\nTimeoutStartSec=0\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    files = [
        (HELPER_PATH, "0755", helper),
        ("/etc/systemd/system/srv-palimpsest.mount", "0644", mount_unit),
        (f"/etc/systemd/system/{UNIT_NAME}", "0644", service),
        ("/etc/systemd/system/ssh.service.d/palimpsest-runtime.conf", "0644", gate),
    ]
    # bootcmd runs on every boot, while runcmd runs once per instance. Only the
    # initial bootstrap needs to stop SSH before write_files installs its gate.
    boot_gate = (
        "from pathlib import Path\n"
        "import subprocess\n"
        "if not Path('/etc/systemd/system/ssh.service.d/palimpsest-runtime.conf').exists():\n"
        "    root=Path('/run/systemd/system/ssh.service.d')\n"
        "    root.mkdir(parents=True,exist_ok=True)\n"
        f"    (root/'palimpsest-runtime.conf').write_text({gate!r})\n"
        "    subprocess.run(['systemctl','daemon-reload'],check=True)\n"
        "    subprocess.run(['systemctl','stop','ssh.service'],check=True)\n"
    )
    lines = [
        "#cloud-config",
        "bootcmd:",
        "  - " + json.dumps(["/usr/bin/python3", "-c", boot_gate]),
        "write_files:",
    ]
    for path, mode, content in files:
        lines.extend([f"  - path: {path}", f"    permissions: '{mode}'", "    owner: root:root", "    content: |"])
        lines.extend("      " + line for line in content.rstrip("\n").split("\n"))
    # A single shell command is deliberate: cloud-init otherwise continues runcmd
    # entries after a failure. SSH cannot be started on the failure branch.
    lines.extend(
        [
            "runcmd:",
            "  - "
            + json.dumps(
                [
                    "/bin/sh",
                    "-ec",
                    f"systemctl daemon-reload && systemctl enable {UNIT_NAME} && "
                    f"systemctl start {UNIT_NAME} && systemctl start ssh.service",
                ]
            ),
        ]
    )
    return "\n".join(lines) + "\n"
