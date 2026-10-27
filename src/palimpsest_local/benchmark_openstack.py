"""API-bound benchmarks on an explicit existing Nova guest, never the controller disk.

Provisioning stays in NovaController/workflow commands. This adapter verifies
project, original job marker, flavor, ports and Cinder attachments before SSH.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

from .cloud_cli import _connect
from .errors import ArtifactValidationError
from .nova_build import NovaController, _field
from .state import atomic_write_json


def _transport(ssh: dict) -> list[str]:
    for key in ("identity_file", "known_hosts"):
        if not isinstance(ssh.get(key), str) or not Path(ssh[key]).expanduser().is_file():
            raise ArtifactValidationError("benchmark SSH identity and pinned known-hosts must exist")
    host, user = ssh.get("host"), ssh.get("user")
    if (
        not isinstance(host, str)
        or not host
        or host.startswith("-")
        or not isinstance(user, str)
        or not user
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in user)
    ):
        raise ArtifactValidationError("benchmark SSH target must be explicit")
    argv = [
        "ssh",
        "-i",
        str(Path(ssh["identity_file"]).expanduser()),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + str(Path(ssh["known_hosts"]).expanduser()),
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if ssh.get("config_file"):
        if not Path(ssh["config_file"]).expanduser().is_file():
            raise ArtifactValidationError("explicit benchmark SSH config is missing")
        argv[1:1] = ["-F", str(Path(ssh["config_file"]).expanduser())]
    if ssh.get("jump"):
        argv += ["-J", ssh["jump"]]
    return argv + [user + "@" + host]


def run_openstack_environment(config: dict, output: Path, *, connection=None) -> dict:
    if config.get("schema") != "palimpsest.benchmark-openstack.v1":
        raise ArtifactValidationError("unsupported OpenStack benchmark schema")
    c = connection or _connect(config["cloud"])
    if c.current_project_id != config["project_id"]:
        raise ArtifactValidationError("benchmark cloud project differs from explicit project")
    if not isinstance(config.get("receipts"), str) or not config.get("job_id"):
        raise ArtifactValidationError("original private Nova receipt directory and job ID are required")
    owned = NovaController(c, config["project_id"], Path(config["receipts"])).inspect(config["job_id"])
    if owned.get("server_id") != config["server_id"] or owned.get("uncertain_create"):
        raise ArtifactValidationError("benchmark target differs from the original create receipt")
    server = c.compute.get_server(config["server_id"])
    if _field(server, "project_id") != config["project_id"] or _field(server, "status") != "ACTIVE":
        raise ArtifactValidationError("benchmark requires an active exact-project Nova server")
    metadata = _field(server, "metadata", {}) or {}
    if not config.get("job_id") or metadata.get("palimpsest_job") != config["job_id"]:
        raise ArtifactValidationError("benchmark target original job marker differs")
    ports = [
        p
        for p in c.network.ports(device_id=config["server_id"])
        if _field(p, "device_id") == config["server_id"] and _field(p, "project_id") == config["project_id"]
    ]
    addresses = {str(ip["ip_address"]) for p in ports for ip in (_field(p, "fixed_ips", []) or [])}
    for p in ports:
        addresses.update(
            str(_field(ip, "floating_ip_address"))
            for ip in c.network.ips(port_id=_field(p, "id"))
            if _field(ip, "project_id") == config["project_id"] and _field(ip, "port_id") == _field(p, "id")
        )
    if config["ssh"]["host"] not in addresses:
        raise ArtifactValidationError("benchmark SSH address does not belong to the exact Nova guest")
    requested = owned.get("flavor_at_request")
    if not isinstance(requested, dict) or not requested.get("id"):
        raise ArtifactValidationError(
            "creation receipt flavor snapshot is required; current GET is not historical evidence"
        )
    actual = _field(server, "flavor", {})
    output = output.expanduser().absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(output, 0o700)
    volume_metadata = []
    for attachment in _field(server, "attached_volumes", []) or []:
        v = c.block_storage.get_volume(_field(attachment, "id"))
        if _field(v, "project_id") != config["project_id"]:
            raise ArtifactValidationError("benchmark attached volume project mismatch")
        volume_metadata.append({key: _field(v, key) for key in ("id", "size", "volume_type", "status")})
    expected_volume = config.get("disk_volume_id")
    if expected_volume is not None and expected_volume not in {v["id"] for v in volume_metadata}:
        raise ArtifactValidationError("measured disk volume is not attached to the original guest")
    prefix = _transport(config["ssh"])
    # A fixed source bundle, with no caller credential or arbitrary installation,
    # runs beneath an exclusive /tmp directory owned by the guest account.
    from . import environment_benchmark

    source = Path(environment_benchmark.__file__).read_bytes()
    encoded = base64.b64encode(source).decode()
    env_encoded = base64.b64encode(json.dumps(config["environment"]).encode()).decode()
    code = (
        "import base64,hashlib,json,tempfile,runpy,signal,subprocess,os;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()));"
        "signal.signal(signal.SIGHUP,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()));"
        "boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip();"
        "r=Path(tempfile.mkdtemp(prefix='palimpsest-benchmark-'));"
        f"b=base64.b64decode({encoded!r});assert hashlib.sha256(b).hexdigest()=={hashlib.sha256(source).hexdigest()!r};"
        "(r/'benchmark.py').write_bytes(b);"
        f"(r/'config.json').write_bytes(base64.b64decode({env_encoded!r}));"
        "module=runpy.run_path(str(r/'benchmark.py'));"
        "v=module['run_environment'](json.loads((r/'config.json').read_text()),r/'evidence');"
        "assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==boot;v['boot_id']=boot;"
        f"v['probe_source_sha256']={hashlib.sha256(source).hexdigest()!r};"
        f"directory={config['environment']['disk']['directory']!r};"
        "v['filesystem_binding']=json.loads(subprocess.check_output(['findmnt','--json','--target',directory,'--output','SOURCE,FSTYPE,TARGET,OPTIONS,MAJ:MIN'],text=True));"
        "mount_source=json.loads(subprocess.check_output(['findmnt','--json','--target',directory,'--output','SOURCE'],text=True))['filesystems'][0]['source'];"
        f"expected_volume={expected_volume!r};"
        "v['block_devices']=json.loads(subprocess.check_output(['lsblk','--json','--inverse','--paths','--bytes','--output','NAME,SERIAL,SIZE,TYPE',mount_source],text=True)) if expected_volume is not None else {'blockdevices':[]};"
        "mount_source=v['filesystem_binding']['filesystems'][0]['source'];"
        "mount_device=v['filesystem_binding']['filesystems'][0]['maj:min'];"
        "assert v['disk'].get('status')!='success' or mount_device==str(os.major(v['disk']['device_id']))+':'+str(os.minor(v['disk']['device_id']));"
        "flatten=lambda ns:[v for n in ns for v in [n,*flatten(n.get('children',[]))]];"
        "assert expected_volume is None or any(d.get('serial') in (expected_volume,expected_volume[:20],expected_volume.replace('-',''),expected_volume.replace('-','')[:20]) for d in flatten(v['block_devices']['blockdevices']));"
        "v['remote_evidence_directory']=str(r/'evidence');print(json.dumps(v))"
    )
    # PTY hangup signals the remote interpreter, whose handler cleans up its
    # separately grouped fio/iperf children. No script bytes go through canonical
    # tty input: a bounded base64 bootstrap is an argv, with stdin closed.
    bootstrap = "exec(__import__('base64').b64decode(" + repr(base64.b64encode(code.encode()).decode()) + "))"
    if len(bootstrap.encode()) > 120000:
        raise ArtifactValidationError("remote benchmark bootstrap exceeds safe argument bound")
    command = shlex.join(["timeout", "--signal=TERM", "--kill-after=10s", "25200s", "python3", "-c", bootstrap])
    response = subprocess.run(
        [*prefix[:-1], "-tt", prefix[-1], command],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=25320,
    )
    (output / "transport.stderr").write_text(response.stderr)
    if response.returncode:
        raise ArtifactValidationError("remote environment benchmark failed; private transport evidence retained")
    lines = response.stdout.strip().splitlines()
    if not lines:
        raise ArtifactValidationError("remote environment benchmark returned no result")
    result = json.loads(lines[-1])
    if result.get("schema") != "palimpsest.environment-result.v1":
        raise ArtifactValidationError("remote environment result schema differs")
    result.update(
        mode="openstack",
        server_id=config["server_id"],
        job_id=config["job_id"],
        project_id=config["project_id"],
        flavor={"requested": requested, "actual": actual},
        volumes=volume_metadata,
    )
    identity = subprocess.run(
        [*prefix, "cat /proc/sys/kernel/random/boot_id"], capture_output=True, text=True, timeout=30
    )
    if identity.returncode:
        raise ArtifactValidationError("cannot bind benchmark result to current guest boot")
    result["boot_id_after"] = identity.stdout.strip()
    if result["boot_id_after"] != result.get("boot_id"):
        raise ArtifactValidationError("benchmark crossed a guest reboot; no valid measurement reported")
    atomic_write_json(output / "result.json", result)
    return result
