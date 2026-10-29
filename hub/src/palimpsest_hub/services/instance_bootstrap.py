"""Cloud-init payload: verified Hub bytes, private overlay, real chroot execution.

The capability is intentionally only in root-readable guest bootstrap input. It is
never a Keystone token and never appears in argv, callback URLs, or public state.
The selected base must already supply python3, mount, chroot, systemd, SquashFS
and OverlayFS support; bootstrap failure is reported rather than running bare base.
"""

from __future__ import annotations

import base64
import json

GUEST_RUNNER = r"""import hashlib, json, os, pathlib, subprocess, time, urllib.request
ROOT = pathlib.Path('/var/lib/palimpsest-instance')
spec = json.loads((ROOT / 'spec.json').read_text())
deadline = spec['expires_at']
headers = {'Authorization': 'Bearer ' + spec['capability']}
process = None
mounts = []
loop_device = None
backing_file = ROOT / 'writable.ext4'

def bounded(path):
    with open(path, 'rb') as stream:
        return stream.read(8192).decode('utf-8', 'replace').replace(spec['capability'], '[redacted bootstrap capability]')[:8192]

def report(state, code=None):
    data = {'instance_id': spec['instance_id'], 'status': state,
            'exit_code': process.poll() if process else None,
            'stdout': bounded(ROOT / 'stdout') if (ROOT / 'stdout').exists() else '',
            'stderr': bounded(ROOT / 'stderr') if (ROOT / 'stderr').exists() else '',
            'error_code': code}
    def serialize():
        return json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    body = serialize()
    if len(body) > 70000:
        # JSON control-character escapes expand beyond the raw capture limit.
        # Trim only this observation, before constructing the identical retries.
        channel = max(('stdout', 'stderr'), key=lambda name: len(json.dumps(data[name]).encode()))
        output = data[channel]
        low, high = 0, len(output)
        while low < high:
            middle = (low + high + 1) // 2
            data[channel] = output[:middle]
            if len(serialize()) <= 70000:
                low = middle
            else:
                high = middle - 1
        data[channel] = output[:low]
        body = serialize()
    request = urllib.request.Request(spec['origin'] + '/v1/instance-bootstrap/' + spec['instance_id'] + '/result',
                                     data=body, headers={**headers, 'Content-Type':'application/json'}, method='PUT')
    # Only retry the identical bounded observation; never retry execution.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read(1024)
            return
        except Exception:
            if time.time() + 2 >= deadline:
                raise
            time.sleep(1)
    raise RuntimeError('result_delivery_failed')

def run(argv):
    subprocess.run(argv, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=30)

def mount(argv, target):
    run(['mount', *argv, str(target)])
    mounts.append(target)

def runtime_identity():
    # Called only AFTER chroot: image symlinks cannot resolve against base files.
    user, _, group = (spec['runtime'].get('user') or '0').partition(':')
    match = None
    if pathlib.Path('/etc/passwd').exists():
        with open('/etc/passwd', 'r') as stream:
            for line in stream:
                entry = line.rstrip('\n').split(':')
                if len(line) <= 8192 and len(entry) >= 4 and (
                    entry[0] == user or (user.isdigit() and entry[2].isdigit() and int(entry[2]) == int(user))
                ):
                    match = entry
                    break
    uid = int(user) if user.isdigit() else int(match[2]) if match else None
    # OCI/Docker unmatched numeric users retain GID0 unless an explicit :group
    # is supplied; matched numeric UIDs use the image account's primary group.
    gid = int(match[3]) if match else 0
    if group:
        if group.isdigit():
            gid = int(group)
        else:
            with open('/etc/group', 'r') as stream:
                entry = next((line.rstrip('\n').split(':') for line in stream
                              if line.split(':', 1)[0] == group and len(line) <= 8192), None)
            gid = int(entry[2]) if entry and len(entry) >= 3 else None
    if uid is None or gid is None or not 0 <= uid < 2**32 or not 0 <= gid < 2**32:
        raise RuntimeError('invalid_runtime_identity')
    return uid, gid

def enter_runtime():
    # No /bin/sh requirement: exec the actual OCI/request argv even in distroless images.
    os.chroot(ROOT / 'merged')
    runtime_uid, runtime_gid = runtime_identity()
    os.chdir(spec['runtime'].get('working_dir') or '/')
    os.setgroups([])
    os.setgid(runtime_gid)
    os.setuid(runtime_uid)

def pump(timeout=0.2):
    for key, _ in selector.select(timeout):
        chunk = os.read(key.fileobj.fileno(), 4096)
        if not chunk:
            selector.unregister(key.fileobj)
            key.fileobj.close()
            continue
        output, remaining = outputs[key.fd]
        kept = chunk[:remaining]
        if kept:
            output.write(kept)
        outputs[key.fd] = (output, remaining - len(kept))

try:
    if time.time() >= deadline:
        raise RuntimeError('bootstrap_expired')
    import platform
    if platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('guest_architecture_mismatch')
    import shutil
    if any(shutil.which(tool) is None for tool in ('mount', 'umount', 'truncate', 'mkfs.ext4', 'losetup')):
        raise RuntimeError('guest_filesystem_tools_unavailable')
    writable_bytes = spec['writable_bytes']
    if type(writable_bytes) is not int or not 1024**3 <= writable_bytes <= 4096 * 1024**3:
        raise RuntimeError('invalid_writable_capacity')
    available = os.statvfs(ROOT)
    required = writable_bytes + sum(layer['size_bytes'] for layer in spec['layers']) + 1024**3
    if available.f_bavail * available.f_frsize < required:
        raise RuntimeError('guest_disk_capacity_insufficient')
    # Private mount propagation prevents application mounts reaching the base namespace.
    run(['mount', '--make-rprivate', '/'])
    os.chdir(ROOT)
    lower = []
    for index, layer in enumerate(spec['layers']):
        blob = ROOT / ('layer-%d.squashfs' % index)
        request = urllib.request.Request(spec['origin'] + '/v1/instance-bootstrap/' + spec['instance_id'] +
                                         '/layers/' + layer['digest'], headers=headers)
        digest = hashlib.sha256()
        count = 0
        with urllib.request.urlopen(request, timeout=30) as response, open(blob, 'xb') as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > layer['size_bytes'] or time.time() >= deadline:
                    raise RuntimeError('layer_download_failed')
                digest.update(chunk)
                output.write(chunk)
        if count != layer['size_bytes'] or 'sha256:' + digest.hexdigest() != layer['digest']:
            raise RuntimeError('layer_digest_mismatch')
        target = ROOT / ('lower-%d' % index)
        target.mkdir()
        mount(['-t', 'squashfs', '-o', 'loop,ro,nodev,nosuid', str(blob)], target)
        lower.insert(0, target.name)
    # Application mode includes the boot root as a lower. Its writable layer
    # MUST be a different filesystem: a descendant upper/work on that same
    # filesystem is an overlapping OverlayFS layer and Linux rejects it.
    writable = ROOT / 'writable'
    writable.mkdir()
    available = os.statvfs(ROOT)
    if available.f_bavail * available.f_frsize < writable_bytes + 1024**3:
        raise RuntimeError('guest_disk_capacity_insufficient')
    # Format ONLY this exclusively created regular file, never a discovered device.
    fd = os.open(backing_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        run(['truncate', '-s', str(writable_bytes), str(backing_file)])
        os.posix_fallocate(fd, 0, writable_bytes)
        os.fsync(fd)
    finally:
        os.close(fd)
    run(['mkfs.ext4', '-q', '-F', '-m', '0', str(backing_file)])
    attached = subprocess.run(['losetup', '--find', '--show', str(backing_file)], check=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=30)
    import re
    selected = attached.stdout.strip()
    if not re.fullmatch(r'/dev/loop[0-9]+', selected):
        raise RuntimeError('invalid_private_loop_device')
    loop_device = selected
    mount(['-t', 'ext4', '-o', 'nodev,nosuid', loop_device], writable)
    for name in ('upper', 'work'):
        (writable / name).mkdir()
    if spec['mode'] == 'application':
        base = ROOT / 'base'
        base.mkdir()
        mount(['--bind', '/'], base)
        run(['mount', '-o', 'remount,bind,ro', str(base)])
        lower.append(base.name)
    (ROOT / 'merged').mkdir()
    merged = ROOT / 'merged'
    options = 'lowerdir=' + ':'.join(lower) + ',upperdir=writable/upper,workdir=writable/work'
    if len(options.encode()) >= 4096:
        raise RuntimeError('overlay_option_limit')
    mount(['-t', 'overlay', '-o', options, 'overlay'], merged)
    for name in ('dev', 'proc', 'sys'):
        if (merged / name).is_symlink():
            raise RuntimeError('unsafe_runtime_mountpoint')
        (merged / name).mkdir(exist_ok=True)
    mount(['--bind', '/dev'], merged / 'dev')
    mount(['-t', 'proc', '-o', 'nosuid,nodev,noexec', 'proc'], merged / 'proc')
    mount(['--bind', '/sys'], merged / 'sys')
    run(['mount', '-o', 'remount,bind,ro,nosuid,nodev,noexec', str(merged / 'sys')])
    # Docker injects these guest-local network essentials too. Reject image
    # symlinks rather than resolving their absolute targets against the base.
    etc = merged / 'etc'
    if etc.is_symlink():
        raise RuntimeError('unsafe_runtime_network_directory')
    etc.mkdir(exist_ok=True)
    for name in ('resolv.conf', 'hosts'):
        target = etc / name
        if target.is_symlink():
            raise RuntimeError('unsafe_runtime_network_file')
        with open('/etc/' + name, 'rb') as source:
            data = source.read(65537)
        if len(data) > 65536:
            raise RuntimeError('runtime_network_file_limit')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
    environment = {'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME':'/'}
    for entry in spec['runtime'].get('env', []):
        key, value = entry.split('=', 1)
        environment[key] = value
    import selectors
    process = subprocess.Popen(spec['command'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                               preexec_fn=enter_runtime, start_new_session=True)
    selector = selectors.DefaultSelector()
    outputs = {}
    for pipe, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
        outputs[pipe.fileno()] = (open(ROOT / name, 'wb', buffering=0), 8192)
        selector.register(pipe, selectors.EVENT_READ)
    try:
        if spec['readiness_argv']:
            ready = False
            while time.time() < deadline and process.poll() is None:
                try:
                    probe = subprocess.run(spec['readiness_argv'], env=environment,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, preexec_fn=enter_runtime,
                                           timeout=min(10, max(1, deadline-time.time())))
                    if probe.returncode == 0:
                        ready = True
                        break
                except subprocess.TimeoutExpired:
                    pass
                pump(1)
            if not ready:
                raise RuntimeError('readiness_failed')
            pump(0)
            report('ready')
            # systemd owns the running command lifecycle until instance shutdown.
            while process.poll() is None:
                pump()
        else:
            while process.poll() is None and time.time() < deadline:
                pump()
            if process.poll() is None:
                raise RuntimeError('command_timeout')
            drain_deadline = min(deadline, time.time() + 2)
            while selector.get_map() and time.time() < drain_deadline:
                pump()
            report('ready' if process.returncode == 0 else 'error', None if process.returncode == 0 else 'command_failed')
    finally:
        selector.close()
        for output, _ in outputs.values():
            output.close()
        for pipe in (process.stdout, process.stderr):
            pipe.close()
except BaseException:
    if process is not None and process.poll() is None:
        import signal
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    try:
        report('error', 'guest_execution_failed')
    except Exception:
        pass  # No credentials or unbounded exception text in guest console logs.
finally:
    remaining_mounts = set(mounts)
    for target in reversed(mounts):
        try:
            run(['umount', str(target)])
            remaining_mounts.discard(target)
        except Exception:
            pass
    if loop_device is not None and writable not in remaining_mounts:
        try:
            backing = subprocess.run(['losetup', '--noheadings', '--output', 'BACK-FILE', loop_device],
                check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=30).stdout.strip()
            if backing == str(backing_file):
                run(['losetup', '--detach', loop_device])
        except Exception:
            pass  # Retain an unverifiable/live attachment rather than detach another device.
    # The fixed expiry is server-enforced even if a guest cannot remove input.
    (ROOT / 'spec.json').unlink(missing_ok=True)
"""


def render_bootstrap(spec: dict) -> str:
    """Return cloud-config JSON (also valid YAML), without caller-controlled shell."""
    unit = """[Unit]
Description=Palimpsest verified instance runtime
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 /var/lib/palimpsest-instance/runner.py
PrivateMounts=yes
KillMode=control-group
Restart=no
[Install]
WantedBy=multi-user.target
"""

    def file(path: str, content: str, permissions: str = "0600") -> dict:
        return {
            "path": path,
            "permissions": permissions,
            "encoding": "b64",
            "content": base64.b64encode(content.encode()).decode(),
        }

    payload = {
        "write_files": [
            file("/var/lib/palimpsest-instance/spec.json", json.dumps(spec, separators=(",", ":"))),
            file("/var/lib/palimpsest-instance/runner.py", GUEST_RUNNER),
            file("/etc/systemd/system/palimpsest-instance.service", unit, "0644"),
        ],
        "runcmd": [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", "palimpsest-instance.service"]],
    }
    if spec.get("ca_certificates"):
        payload["ca_certs"] = {"trusted": spec["ca_certificates"]}
    return "#cloud-config\n" + json.dumps(payload, separators=(",", ":")) + "\n"
