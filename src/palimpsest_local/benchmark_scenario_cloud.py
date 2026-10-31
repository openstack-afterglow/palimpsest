"""Exact-project scenario guests, durable effect fences and console-pinned SSH.

Credentials remain in SDK configuration and SSH identity files. Nothing in this
adapter imports a cloud credential into a guest. API consumers are observations,
not SDK-owned resources, and can never acquire deletion authority here.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

from .cloud_cli import _connect
from .errors import ArtifactValidationError
from .layer_cache import _directory, _private_directory
from .nova_build import NovaController, _field
from .state import atomic_write_json

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_MAX_OUTPUT = 1024 * 1024
_TOOLS = ["python3", "fio", "curl", "iperf3", "util-linux", "coreutils", "sudo"]
_DOCKER_SETUP = """import json,shutil,subprocess,sys
installed=False
if shutil.which('docker') is None:
 if sys.argv[1]=='gpu':
  raise SystemExit('GPU prerequisites absent: qualified image must already provide Docker with NVIDIA runtime; no engine or driver installation attempted')
 subprocess.run(['apt-get','install','--no-install-recommends','-y','docker.io'],check=True,timeout=300)
 installed=True
 if shutil.which('docker') is None:raise SystemExit('Docker package installation did not provide the required engine')
print(json.dumps({'docker_preserved':not installed,'docker_installed_during_unmeasured_setup':installed}))
"""


def _fail(message):
    raise ArtifactValidationError("scenario cloud: " + message)


def _id(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(name + " must be an explicit bounded identifier")
    return value


def _file(value, name):
    if not isinstance(value, str) or not value:
        _fail(name + " file reference is required")
    path = Path(value).expanduser().absolute()
    if not path.is_file() or path.is_symlink():
        _fail(name + " must reference an existing regular file")
    return path


def _private(path):
    with _directory(path) as fd:
        _private_directory(fd)


def _new_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            _fail("private receipt ownership or mode changed")
        text = stream.read(_MAX_OUTPUT + 1)
        if len(text) > _MAX_OUTPUT:
            _fail("receipt exceeds bound")
        return json.loads(text)


def _seconds(config, name, default, maximum=14400):
    value = config.get("timeouts", {}).get(name, default)
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= maximum:
        _fail("invalid " + name + " deadline")
    return float(value)


def validate_cloud_config(config, *, require_ssh=True, purpose="benchmark"):
    """Offline prerequisites; only a fresh peer may request a floating network."""
    if purpose not in {"benchmark", "peer", "station"}:
        _fail("purpose must be benchmark, peer or station")
    for key in ("job_id", "project_id"):
        _id(config.get(key), key)
    for key in ("cloud", "project_name", "project_domain_name"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            _fail(key + " is required")
    placement = config.get("placement", {})
    for key in ("image_id", "flavor_id", "network_id"):
        _id(placement.get(key), key)
    approval = config.get("approval", {})
    _id(approval.get("user_id"), "approved user")
    allowed = approval.get("allowed_flavor_ids")
    if not isinstance(allowed, list) or not allowed or placement["flavor_id"] not in allowed:
        _fail("requested flavor is outside explicit approved flavor IDs")
    for key in ("max_vcpus", "max_ram_mib"):
        if type(approval.get(key)) is not int or approval[key] <= 0:
            _fail("explicit approval " + key + " is required")
    if "floating_network_id" in placement:
        if purpose != "peer":
            _fail("floating_network_id is only permitted for a fresh peer")
        _id(placement["floating_network_id"], "floating network")
    groups = placement.get("security_group_ids")
    if not isinstance(groups, list) or (not groups and purpose != "peer") or len(set(groups)) != len(groups):
        _fail("explicit unique security groups are required")
    for group in groups:
        _id(group, "security group")
    if type(placement.get("boot_gib")) is not int or not 10 <= placement["boot_gib"] <= 16384:
        _fail("boot_gib must be between 10 and 16384")
    writable = placement.get("writable_gib", placement["boot_gib"])
    if type(writable) is not int or not 1 <= writable <= placement["boot_gib"]:
        _fail("writable_gib is boot filesystem scratch capacity, and must not exceed boot_gib")
    if not require_ssh:
        return
    ssh = config.get("ssh", {})
    if not isinstance(ssh.get("user"), str) or not _USER.fullmatch(ssh["user"]):
        _fail("explicit SSH user is required")
    _file(ssh.get("identity_file"), "SSH identity")
    _file(ssh.get("known_hosts"), "SSH pinned known-hosts")
    public = _file(ssh.get("public_key_file"), "SSH public key").read_text().strip()
    if "\n" in public or "\r" in public or len(public.encode()) > 16384:
        _fail("SSH public key must be one bounded public-key line")
    fields = public.split()
    if len(fields) < 2 or fields[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"}:
        _fail("SSH public key format is unsupported")
    try:
        base64.b64decode(fields[1], validate=True)
    except ValueError:
        _fail("SSH public key encoding is invalid")
    if ssh.get("config_file"):
        _file(ssh["config_file"], "SSH config")
    if ssh.get("jump") and not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]{1,255}", ssh["jump"]):
        _fail("SSH jump must be one explicit clean target")
    if ssh.get("jump", "").startswith("-"):
        _fail("SSH jump must not be an option")
    for name, default in (
        ("active_seconds", 900),
        ("ready_seconds", 1200),
        ("delete_seconds", 300),
        ("guest_seconds", 3600),
    ):
        _seconds(config, name, default)
    if type(ssh.get("direct_private", False)) is not bool:
        _fail("direct_private must be an explicit boolean")
    if purpose == "station" and config.get("data_disk", {}).get("mountpoint") != "/mnt/palimpsest-benchmark":
        _fail("station requires its dedicated fixed data mountpoint")
    environment = config.get("environment")
    if purpose in {"peer", "station"} and environment is None:
        return
    if purpose == "benchmark" and environment is None and isinstance(config.get("environment_context"), dict):
        return
    if not isinstance(environment, dict) or environment.get("schema") != "palimpsest.benchmark-environment.v1":
        _fail("an explicit benchmark-environment.v1 configuration is required")
    if not isinstance(environment.get("disk"), dict) or not isinstance(environment.get("network"), dict):
        _fail("disk and both network measurements are required")
    if any(not isinstance(environment["network"].get(k), dict) for k in ("external", "internal")):
        _fail("HTTPS download/upload and private TCP both directions are required")
    directory = environment["disk"].get("directory")
    if (
        not isinstance(directory, str)
        or not directory.startswith("/")
        or ".." in Path(directory).parts
        or len(Path(directory).parts) < 3
        or Path(directory).parts[1] in {"proc", "sys", "dev", "etc", "usr", "bin", "sbin", "root"}
    ):
        _fail("disk directory must be a dedicated absolute guest scratch directory")
    size = environment["disk"].get("size_mib", 1024)
    if type(size) is not int or not 16 <= size <= writable * 1024:
        _fail("fio scratch size exceeds explicit writable_gib")


def _without_host_alias(seed, alias):
    """Keep unrelated pins; inherited alias trust never supplements the console key."""
    lines = []
    for line in seed.splitlines(keepends=True):
        fields = list(re.finditer(r"\S+", line))
        if not fields or fields[0].group().startswith("#"):
            lines.append(line)
            continue
        index = 1 if fields[0].group().startswith("@") else 0
        if len(fields) < index + 3:
            lines.append(line)
            continue
        hosts = fields[index]
        retained = []
        for host in hosts.group().split(","):
            pattern = host.removeprefix("!")
            qualified = re.fullmatch(r"\[([^\]]+)\]:[0-9]+", pattern)
            if qualified:
                pattern = qualified.group(1)
            if pattern.startswith("|"):
                parts = pattern.split("|")
                if len(parts) != 4 or parts[:2] != ["", "1"]:
                    _fail("inherited hashed known-host entry is unsupported")
                try:
                    salt = base64.b64decode(parts[2], validate=True)
                    digest = base64.b64decode(parts[3], validate=True)
                except ValueError:
                    _fail("inherited hashed known-host entry is invalid")
                if len(salt) != 20 or len(digest) != 20:
                    _fail("inherited hashed known-host entry is invalid")
                if any(
                    hmac.compare_digest(hmac.digest(salt, candidate.encode(), "sha1"), digest)
                    for candidate in {alias, alias.lower()}
                ):
                    _fail("inherited hashed known-host entry matches fresh console alias")
            elif pattern.lower() == alias.lower():
                continue
            elif re.fullmatch(re.escape(pattern.lower()).replace(r"\*", ".*").replace(r"\?", "."), alias.lower()):
                _fail("inherited known-host pattern matches fresh console alias")
            retained.append(host)
        if retained:
            lines.append(line[: hosts.start()] + ",".join(retained) + line[hosts.end() :])
    return "".join(lines)


def _redact(text):
    # Guest programs must never emit credentials; protect evidence if they do.
    text = re.sub(
        r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", text
    )
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|(?:token|password|secret|credential|access_key)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)


def _supports_retention(compute):
    try:
        from keystoneauth1.discover import version_between
    except ImportError:
        _fail("install the cloud extra for OpenStack SDK capabilities")
    endpoint = compute.get_endpoint_data()
    if endpoint is None:
        return False
    minimum, maximum = endpoint.min_microversion, endpoint.max_microversion
    return bool(minimum and maximum and version_between(minimum, maximum, "2.79"))


def _kill_owned(process):
    """Match LocalProcessSession's bounded Darwin denial/exit-race rule."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as denied:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            raise denied from None
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        raise denied


class ScenarioCloud:
    """Own SDK guests with explicit benchmark, peer or retained-station admission.

    Only purpose='peer' accepts placement.floating_network_id or initially empty
    security groups. Station guests may omit legacy probe configuration and
    inspect exactly one receipt-bound data disk in addition to original boot.
    Benchmark guests never automatically acquire public IPs.
    Returned port_id/interfaces capture private interface identity and Neutron
    binding fields; private_ip remains the private measurement/cluster address.
    A peer's public_ip is used only by its console-pinned ssh transport. Its
    separate floating_ip_phase fences allocation and deletion across restarts;
    callers recover original receipts through resources()/inspect()/delete(),
    never by repeating create(). All auxiliary budget reservations are durable.

    cleanup_only=True admits original-receipt deletion without SSH files,
    current provider flavor admission or creation capabilities. It preserves
    exact actor/project/domain authority and forbids creation/guest execution.
    """

    def __init__(
        self,
        config: dict,
        output: Path,
        event,
        connection=None,
        budget=None,
        observation_only=False,
        *,
        purpose="benchmark",
        cleanup_only=False,
    ):
        if type(observation_only) is not bool:
            _fail("observation_only must be a boolean")
        if type(cleanup_only) is not bool:
            _fail("cleanup_only must be a boolean")
        if cleanup_only and observation_only:
            _fail("cleanup_only and observation_only are mutually exclusive")
        self.cleanup_only = cleanup_only
        self.observation_only = observation_only
        validate_cloud_config(config, require_ssh=not (observation_only or cleanup_only), purpose=purpose)
        self.purpose = purpose
        self.config = copy.deepcopy(config)
        self.output = Path(output).expanduser().absolute()
        _private(self.output)
        self.event = event
        self.project_id = config["project_id"]
        self.connection = connection if connection is not None else _connect(config["cloud"])
        self._scope()
        if not cleanup_only:
            self._approved_flavor(self.connection.compute.get_flavor(config["placement"]["flavor_id"]))
        self.receipts = self.output / "nova-receipts"
        self.receipts.mkdir(mode=0o700, exist_ok=True)
        _private(self.receipts)
        self.controller = NovaController(self.connection, self.project_id, self.receipts)
        self.operations = self.output / "cloud-operations"
        self.operations.mkdir(mode=0o700, exist_ok=True)
        _private(self.operations)
        if budget is None and not observation_only:
            from .benchmark_scenarios import ResourceBudget

            budget = ResourceBudget(config, output)
        self.budget = budget
        self._capabilities_checked = False
        self.deadline = time.monotonic() + config.get("deadline_seconds", 7200)

    def _require_sdk(self, *, allow_cleanup=False):
        if self.observation_only:
            _fail("observation-only adapter cannot create, execute, measure or delete guests")
        if self.cleanup_only and not allow_cleanup:
            _fail("cleanup-only adapter cannot create, execute or measure guests")
        if "floating_network_id" in self.config["placement"] and self.purpose != "peer":
            _fail("floating_network_id is only permitted for a fresh peer")

    def _remaining(self, requested):
        remaining = min(float(requested), self.deadline - time.monotonic())
        if remaining < 1:
            _fail("scenario deadline expired; no new guest effect started")
        return remaining

    def resources(self) -> list[dict]:
        """Original local role receipts only, including uncertain intents."""
        _private(self.operations)
        result = []
        for path in sorted(self.operations.glob("*.json")):
            resource = _read(path)
            if resource.get("source") != "sdk" or resource.get("project_id") != self.project_id:
                _fail("local SDK receipt ownership changed")
            if resource.get("phase") not in {"absent", "aborted-before-request"} or not resource.get("budget_released"):
                result.append(resource)
        return result

    def verify_prepared_volume(self, instance_id: str, boot_volume_id: str) -> dict:
        """Observe an exact API-owned unattached root; never acquire SDK delete authority."""
        self._scope()
        instance_id = _id(instance_id, "API instance")
        boot_volume_id = _id(boot_volume_id, "boot volume")
        try:
            volume = self.connection.block_storage.get_volume(boot_volume_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404 or getattr(exc, "http_status", None) == 404:
                return {"boot_volume_id": boot_volume_id, "project_id": self.project_id, "volume_already_absent": True}
            raise
        if (
            volume is None
            or _field(volume, "id") != boot_volume_id
            or _field(volume, "project_id") != self.project_id
            or (_field(volume, "metadata", {}) or {}).get("palimpsest_instance_id") != instance_id
            or _field(volume, "attachments", [])
        ):
            _fail("prepared API root ownership, marker or unattached identity changed")
        return {"boot_volume_id": boot_volume_id, "project_id": self.project_id}

    def verify_absent(
        self, server_id: str | None, boot_volume_id: str | None = None, *, server_not_created: bool = False
    ) -> dict:
        """Exact GET404 proof; null Nova identity requires a caller-validated no-attempt certificate."""
        self._scope()
        deadline = time.monotonic() + _seconds(self.config, "delete_seconds", 300)
        if server_id is None:
            if server_not_created is not True or boot_volume_id is None:
                _fail("unknown Nova identity is not absence evidence")
        else:
            if server_not_created:
                _fail("bound Nova identity conflicts with never-created certificate")
            server_id = _id(server_id, "server")
            self._absent(self.connection.compute.find_server, server_id, deadline)
        if boot_volume_id is not None:
            boot_volume_id = _id(boot_volume_id, "boot volume")
            while True:
                try:
                    volume = self.connection.block_storage.get_volume(boot_volume_id)
                except Exception as exc:
                    if getattr(exc, "status_code", None) == 404 or getattr(exc, "http_status", None) == 404:
                        break
                    raise
                if volume is None or _field(volume, "id") != boot_volume_id:
                    _fail("exact boot volume absence requires an actual provider 404")
                if time.monotonic() >= deadline:
                    _fail("ordinary deletion absence deadline expired; budget remains reserved")
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        return {
            "server_id": server_id,
            "server_absent": "not_created" if server_not_created else True,
            "boot_volume_id": boot_volume_id,
            "boot_volume_absent": True if boot_volume_id else None,
        }

    def _scope(self):
        c = self.connection
        if c.current_project_id != self.project_id:
            _fail("authenticated project differs from explicit project")
        user_id = getattr(c, "current_user_id", None)
        if user_id is None:
            session = getattr(c, "session", None)
            auth = getattr(session, "auth", None)
            get_user_id = getattr(auth, "get_user_id", None)
            if callable(get_user_id):
                user_id = get_user_id(session)
        if user_id != self.config["approval"]["user_id"]:
            _fail("authenticated user differs from the exact approved actor")
        session = getattr(c, "session", None)
        auth = getattr(session, "auth", None)
        get_access = getattr(auth, "get_access", None)
        if callable(get_access):
            access = get_access(session)
            scoped = (access.project_id, access.project_name, access.project_domain_name)
        else:
            project = c.identity.get_project(self.project_id)
            domain = c.identity.get_domain(_field(project, "domain_id"))
            scoped = (_field(project, "id"), _field(project, "name"), _field(domain, "name"))
        if scoped != (self.project_id, self.config["project_name"], self.config["project_domain_name"]):
            _fail("authenticated project name/domain differs from explicit authority")

    def _approved_flavor(self, flavor):
        approval = self.config["approval"]
        if _field(flavor, "id") not in approval["allowed_flavor_ids"]:
            _fail("provider flavor is outside approved IDs")
        for field, limit in (("vcpus", "max_vcpus"), ("ram", "max_ram_mib")):
            value = _field(flavor, field)
            if type(value) is not int or not 1 <= value <= approval[limit]:
                _fail("provider flavor exceeds explicit approved " + limit)

    def _capabilities(self):
        if self._capabilities_checked:
            return
        self._scope()
        c = self.connection
        for proxy, methods in (
            (
                c.compute,
                ("create_server", "get_server", "find_server", "get_server_console_output", "delete_server", "get"),
            ),
            (c.block_storage, ("get_volume", "find_volume", "delete_volume")),
            (c.network, ("ports", "get_network", "get_security_group")),
        ):
            if any(not callable(getattr(proxy, name, None)) for name in methods):
                _fail("SDK lacks required lifecycle or console capability")
        if self.config["placement"].get("floating_network_id") and any(
            not callable(getattr(c.network, name, None)) for name in ("create_ip", "get_ip", "ips", "delete_ip")
        ):
            _fail("SDK lacks fresh floating-IP lifecycle capability")
        if shutil.which("ssh") is None:
            _fail("OpenSSH executable is unavailable")
        # Microversion 2.79 is required to attest that Nova will retain disks.
        if not _supports_retention(c.compute):
            _fail("Nova volume-retention microversion 2.79 is unavailable")
        self._capabilities_checked = True

    def _own(self, resource, *, allow_unallocated=False):
        role = _id(resource.get("role"), "role")
        original = _read(self.operations / (role + ".json"))
        if (
            original.get("source") != "sdk"
            or (not allow_unallocated and not original.get("server_id"))
            or any(resource.get(k) != original.get(k) for k in ("server_id", "job_id", "reservation_id"))
        ):
            _fail("resource differs from original SDK creation receipt")
        return original

    def _save(self, resource):
        atomic_write_json(self.operations / (resource["role"] + ".json"), resource)

    def _placement(self):
        """Read-only admission before consuming approval-wide creation budget."""
        c, p = self.connection, self.config["placement"]
        if not p["security_group_ids"]:
            _fail("fresh peer security group must be set before guest creation")
        if "floating_network_id" in p:
            if self.purpose != "peer":
                _fail("floating_network_id is only permitted for a fresh peer")
            external = c.network.get_network(p["floating_network_id"])
            if _field(external, "id") != p["floating_network_id"] or _field(external, "is_router_external") is not True:
                _fail("floating network must be the exact external network")
        image = c.image.get_image(p["image_id"])
        if _field(image, "status") != "active":
            _fail("selected base image is not active")
        properties = _field(image, "properties", {}) or {}
        if (
            _field(image, "palimpsest_artifact_type", properties.get("palimpsest_artifact_type"))
            == "external-layer-store"
        ):
            _fail("data-only layer-store images cannot boot scenario guests")
        if int(_field(image, "min_disk", 0) or 0) > p["boot_gib"]:
            _fail("requested boot capacity is below the selected image minimum")
        self._image_at_request = {
            key: _field(image, key)
            for key in (
                "id",
                "name",
                "disk_format",
                "container_format",
                "size",
                "min_disk",
                "architecture",
                "os_distro",
                "hash_algo",
                "hash_value",
                "checksum",
            )
        }
        if _field(c.network.get_network(p["network_id"]), "project_id") != self.project_id:
            _fail("network must be explicitly owned by the target project")
        for identity in p["security_group_ids"]:
            if _field(c.network.get_security_group(identity), "project_id") != self.project_id:
                _fail("security group belongs to another project")
        if p.get("boot_volume_type") is not None:
            _id(p["boot_volume_type"], "boot volume type")
        flavor = c.compute.fetch_flavor_extra_specs(c.compute.get_flavor(p["flavor_id"]))
        self._approved_flavor(flavor)
        extra = _field(flavor, "extra_specs", {}) or {}
        if (
            self.config["scenario"] in {"cuda", "torch-cuda", "tensorflow-cuda"}
            and not extra.get("pci_passthrough:alias")
            and not any(k.startswith("resources:") and ("VGPU" in k or "GPU" in k) for k in extra)
        ):
            _fail("selected flavor declares no Nova GPU allocation")

    def _userdata(self, job_id, supplied):
        import yaml

        ssh = self.config["ssh"]
        public = _file(ssh["public_key_file"], "SSH public key").read_text().strip()
        data = copy.deepcopy(supplied or {})
        if not isinstance(data, dict) or set(data) - {"packages", "write_files", "runcmd", "package_update"}:
            _fail("supplemental cloud-init may only contain packages/write_files/runcmd/package_update")
        encoded = json.dumps(data).lower()
        if re.search(r"private key|os_password|os_token|authorization|password\s*[\"']?\s*:", encoded):
            _fail("guest cloud-init must not contain credentials")
        data["package_update"] = True
        data["packages"] = list(dict.fromkeys([*_TOOLS, *data.get("packages", [])]))
        data["groups"] = ["docker"]
        data["users"] = [
            {
                "name": ssh["user"],
                "groups": ["docker"],
                "shell": "/bin/bash",
                "lock_passwd": True,
                "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
                "ssh_authorized_keys": [public],
            }
        ]
        data["ssh_pwauth"] = False
        data["disable_root"] = True
        data["ssh_deletekeys"] = True
        data["ssh_genkeytypes"] = ["ed25519"]
        marker = "PALIMPSEST_SCENARIO_HOSTKEY " + job_id + " "
        publish = "\n".join(
            [
                "import os",
                "from pathlib import Path",
                "message=" + repr(marker) + "+Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().strip()+'\\n'",
                "print(message,end='',flush=True)",
                "for name in set(['console',*Path('/sys/class/tty/console/active').read_text().split()]):",
                " try:",
                "  fd=os.open('/dev/'+name,os.O_WRONLY|os.O_NONBLOCK)",
                "  try: os.write(fd,message.encode())",
                "  finally: os.close(fd)",
                " except OSError: pass",
            ]
        )
        scratch = (
            self.config["data_disk"]["mountpoint"]
            if self.purpose == "station"
            else (
                "/var/tmp/palimpsest-benchmark-peer"
                if self.purpose == "peer" and self.config.get("environment") is None
                else (
                    "/var/tmp/palimpsest-benchmark"
                    if self.config.get("environment") is None
                    and isinstance(self.config.get("environment_context"), dict)
                    else self.config["environment"]["disk"]["directory"]
                )
            )
        )
        prepare = "\n".join(
            [
                "import os,pwd",
                "from pathlib import Path",
                "p=Path(" + repr(scratch) + ")",
                "assert all(not d.is_symlink() for d in [p,*p.parents])",
                "p.mkdir(mode=0o700,parents=True,exist_ok=True)",
                "u=pwd.getpwnam(" + repr(ssh["user"]) + ")",
                "os.chown(p,u.pw_uid,u.pw_gid)",
                "os.chmod(p,0o700)",
            ]
        )
        data["runcmd"] = [
            [
                "python3",
                "-c",
                _DOCKER_SETUP,
                "gpu" if self.config["scenario"] in {"cuda", "torch-cuda", "tensorflow-cuda"} else "cpu",
            ],
            ["systemctl", "enable", "--now", "docker"],
            ["python3", "-c", prepare],
            *data.get("runcmd", []),
            ["python3", "-c", publish],
        ]
        return "#cloud-config\n" + yaml.safe_dump(data, sort_keys=False)

    def create(self, role: str, *, user_data: dict | None = None) -> dict:
        self._require_sdk()
        role = _id(role, "role")
        self._remaining(1)
        self._capabilities()
        self._scope()
        self._placement()
        job_id = _id(self.config["job_id"] + "-" + role, "role job")
        path = self.operations / (role + ".json")
        if path.exists() or path.is_symlink():
            _fail("role intent exists; inspect instead of replaying an uncertain allocation")
        userdata = self._userdata(job_id, user_data)
        if len(userdata.encode()) > 65535:
            _fail("complete cloud-init exceeds Nova user-data bound")
        p = self.config["placement"]
        reservation = self.budget.reserve(role, p["boot_gib"])
        resource = {
            "source": "sdk",
            "role": role,
            "job_id": job_id,
            "project_id": self.project_id,
            "purpose": self.purpose,
            "reservation_id": reservation,
            "phase": "create-intent",
            "image_at_request": self._image_at_request,
        }
        _new_json(path, resource)
        try:
            record = self.controller.create(
                job_id=job_id,
                name=job_id,
                image_id=p["image_id"],
                flavor_id=p["flavor_id"],
                network_id=p["network_id"],
                security_group_ids=p["security_group_ids"],
                user_data=userdata,
                boot_gib=p["boot_gib"],
                boot_volume_type=p.get("boot_volume_type"),
                require_gpu=self.config["scenario"] in {"cuda", "torch-cuda", "tensorflow-cuda"},
            )
        except Exception:
            # NovaController fsyncs its exclusive intent BEFORE its request.
            # No intent means no provider request; cumulative/role budget is
            # still consumed, but this known-unallocated live slot can close.
            intent = self.receipts / (job_id + ".json")
            if not intent.exists() and not intent.is_symlink():
                resource["phase"] = "aborted-before-request"
                self._save(resource)
                self.budget.release(reservation)
                resource["budget_released"] = True
                self._save(resource)
            raise
        resource.update(
            server_id=record["server_id"],
            flavor={"requested": record["flavor_at_request"], "actual": None},
            phase="created",
        )
        self._save(resource)
        self.budget.bind(reservation, resource["server_id"])
        record = self.controller.wait_active(
            job_id, timeout_seconds=self._remaining(_seconds(self.config, "active_seconds", 900))
        )
        resource.update(
            flavor={"requested": record["flavor_at_request"], "actual": record["flavor_at_allocation"]},
            phase="active-not-ready",
            created_unix_seconds=record["request_unix_seconds"],
        )
        self._save(resource)
        observed = self.inspect(resource)
        volumes = observed["volumes"]
        if len(volumes) != 1:
            _fail("new scenario VM must have exactly its original boot volume")
        resource.update(
            boot_volume_id=volumes[0]["id"],
            volumes_at_allocation=volumes,
            private_ip=observed["private_ip"],
            port_id=observed["port_id"],
            interfaces=observed["interfaces"],
        )
        self._save(resource)
        if p.get("floating_network_id"):
            self._create_floating_ip(resource)
        resource["ssh"] = self._pin(resource, resource.get("public_ip", observed["private_ip"]))
        self._save(resource)
        ready = self.run_guest(
            resource,
            ["cloud-init", "status", "--wait"],
            label="cloud-init-ready",
            timeout=_seconds(self.config, "ready_seconds", 1200),
        )
        if ready["returncode"] == 255 or ready.get("timed_out"):
            _fail("cloud-init readiness not observed; SSH transport failed; resource retained")
        if ready["returncode"] != 0:
            _fail("new guest cloud-init did not succeed; resource retained")
        tools = self.run_guest(
            resource,
            [
                "python3",
                "-c",
                "import shutil,subprocess; assert all(shutil.which(x) for x in ('python3','fio','curl','iperf3','findmnt','lsblk','timeout','docker','sudo')); subprocess.run(['sudo','-n','docker','info'],check=True,stdout=subprocess.DEVNULL); print(open('/proc/sys/kernel/random/boot_id').read().strip())",
            ],
            label="guest-tools",
            timeout=60,
        )
        if tools["returncode"] != 0 or not re.fullmatch(r"[0-9a-f-]{36}", tools["stdout"].strip()):
            _fail("new guest tools or boot identity are unavailable")
        resource.update(boot_id=tools["stdout"].strip(), phase="ready", status="ACTIVE")
        self._save(resource)
        self.event("guest-ready", {"server_id": resource["server_id"], "role": role})
        return resource

    def _floating_description(self, resource):
        return "palimpsest peer " + resource["job_id"] + " project " + self.project_id + " port " + resource["port_id"]

    def _check_floating_ip(self, resource, item):
        """Never adopt a borrowed IP, changed binding, or a marker-only match."""
        expected = {
            "project_id": self.project_id,
            "description": self._floating_description(resource),
            "floating_network_id": resource["floating_network_id"],
            "port_id": resource["port_id"],
            "fixed_ip_address": resource["private_ip"],
        }
        if (
            self.purpose != "peer"
            or resource["floating_network_id"] != self.config["placement"].get("floating_network_id")
            or resource.get("floating_ip_description") != expected["description"]
            or any(_field(item, key) != value for key, value in expected.items())
            or (resource.get("floating_ip_id") and _field(item, "id") != resource["floating_ip_id"])
            or (resource.get("public_ip") and _field(item, "floating_ip_address") != resource["public_ip"])
        ):
            _fail("original floating IP ownership, marker or exact port binding changed")
        _id(_field(item, "id"), "floating IP")
        try:
            address = ipaddress.ip_address(_field(item, "floating_ip_address"))
        except ValueError:
            _fail("floating IP has no valid address")
        if address.version != 4 or address.is_unspecified or address.is_multicast or address.is_loopback:
            _fail("peer floating IP must be a routable IPv4 address")

    def _floating_port(self, resource):
        host, _, _, port_id = self._ports(resource["server_id"], details=True)
        if port_id != resource["port_id"] or host != resource["private_ip"]:
            _fail("original floating IP interface binding changed")

    def _find_floating_ip(self, identity, *, ignore_missing=True):
        # Neutron get_ip has no ignore_missing argument; only an actual 404
        # (not a transport failure or a name lookup miss) proves absence.
        try:
            item = self.connection.network.get_ip(identity)
        except Exception as error:
            if not ignore_missing or (
                getattr(error, "status_code", None) != 404 and getattr(error, "http_status", None) != 404
            ):
                raise
            return None
        if item is None or _field(item, "id") != identity:
            _fail("floating IP GET returned no exact resource identity")
        return item

    def _floating_ip(self, resource):
        """GET-only reconciliation of one original intent, never a POST retry.

        Receipt fields: floating_ip_id/public_ip, floating_network_id,
        floating_ip_description, floating_ip_reservation_id and floating_ip_phase.
        port_id/private_ip always remain the original private interface binding.
        """
        self._scope()
        if self.purpose != "peer":
            _fail("floating IP receipts are only valid for a fresh peer")
        self._floating_port(resource)
        if resource.get("floating_ip_id"):
            item = self._find_floating_ip(resource["floating_ip_id"], ignore_missing=False)
        else:
            # Server-side filters are advisory; inspect every returned marker
            # match and reject foreign ownership/binding instead of adopting it.
            items = [
                item
                for item in self.connection.network.ips(
                    project_id=self.project_id, description=resource["floating_ip_description"]
                )
                if _field(item, "description") == resource["floating_ip_description"]
            ]
            if len(items) != 1:
                _fail("floating IP creation remains uncertain; inspect only, no allocation replay")
            item = items[0]
        self._check_floating_ip(resource, item)
        if resource.get("floating_ip_phase") == "create-intent":
            if self.observation_only:
                _fail("observation-only adapter cannot settle an allocation receipt")
            resource.update(
                floating_ip_id=_field(item, "id"),
                public_ip=_field(item, "floating_ip_address"),
            )
            self._save(resource)
            self.budget.bind_aux(resource["floating_ip_reservation_id"], resource["floating_ip_id"])
            resource["floating_ip_phase"] = "bound"
            self._save(resource)
        return item

    def _create_floating_ip(self, resource):
        self._require_sdk()
        self._scope()
        self._remaining(1)
        if self.purpose != "peer" or resource.get("floating_ip_phase"):
            _fail("fresh peer floating IP intent required; no allocation replay")
        if _field(self.connection.compute.get_server(resource["server_id"]), "status") != "ACTIVE":
            _fail("floating IP creation requires the original active peer")
        self.controller.inspect(resource["job_id"])
        self._floating_port(resource)
        reservation = self.budget.reserve_aux("floating_ips", resource["role"] + "-fip")
        resource.update(
            floating_ip_phase="create-intent",
            floating_ip_reservation_id=reservation,
            floating_network_id=self.config["placement"]["floating_network_id"],
            floating_ip_description=self._floating_description(resource),
        )
        self._save(resource)
        item = self.connection.network.create_ip(
            project_id=self.project_id,
            floating_network_id=resource["floating_network_id"],
            port_id=resource["port_id"],
            fixed_ip_address=resource["private_ip"],
            description=resource["floating_ip_description"],
        )
        # Persist the returned identity even if attestation fails. Cleanup then
        # refuses the foreign binding rather than searching for another IP.
        resource["floating_ip_id"] = _id(_field(item, "id"), "floating IP")
        self._save(resource)
        self._check_floating_ip(resource, item)
        self._floating_ip(resource)

    def _delete_floating_ip(self, resource, deadline):
        phase = resource.get("floating_ip_phase")
        if phase is None:
            return
        if self.purpose != "peer":
            _fail("floating IP cleanup is only valid for its original peer")
        if phase in {"delete-intent", "absent"}:
            item = self._find_floating_ip(resource["floating_ip_id"])
            if item is not None:
                self._check_floating_ip(resource, item)
                _fail("floating IP delete intent exists; no destructive replay")
        else:
            self._floating_ip(resource)
            resource["floating_ip_phase"] = "delete-intent"
            self._save(resource)
            self.connection.network.delete_ip(resource["floating_ip_id"], ignore_missing=False)
            self._absent(self._find_floating_ip, resource["floating_ip_id"], deadline)
        resource.update(floating_ip_phase="absent", floating_ip_absent=True)
        self._save(resource)
        if not resource.get("floating_ip_budget_released"):
            self.budget.release_aux(resource["floating_ip_reservation_id"])
            resource["floating_ip_budget_released"] = True
            self._save(resource)

    def _ports(self, server_id, *, details=False):
        ports = list(self.connection.network.ports(device_id=server_id))
        ports = [p for p in ports if _field(p, "device_id") == server_id]
        if not ports or any(_field(p, "project_id") != self.project_id for p in ports):
            _fail("server interfaces are absent or cross-project")
        addresses = [ip["ip_address"] for p in ports for ip in (_field(p, "fixed_ips", []) or [])]
        expected_network = self.config["placement"]["network_id"]
        candidates = [
            ip["ip_address"]
            for p in ports
            if _field(p, "network_id") == expected_network
            for ip in (_field(p, "fixed_ips", []) or [])
        ]
        host = self.config["ssh"].get("host")
        if host is not None:
            if host not in candidates:
                _fail("configured SSH host is not on the requested owned interface")
        elif len(candidates) == 1:
            host = candidates[0]
        else:
            _fail("SSH fixed IP is ambiguous; configure an exact host")
        address = ipaddress.ip_address(host)
        if not address.is_global and not (
            self.config["ssh"].get("jump")
            or self.config["ssh"].get("direct_private") is True
            or (self.purpose == "peer" and self.config["placement"].get("floating_network_id"))
        ):
            _fail("private IP needs explicit SSH jump or direct_private route authority")
        if details:
            interfaces = [
                {
                    key: _field(port, key)
                    for key in (
                        "id",
                        "project_id",
                        "network_id",
                        "device_id",
                        "fixed_ips",
                        "security_group_ids",
                        "binding_host_id",
                        "binding_vif_type",
                        "binding_vnic_type",
                    )
                }
                for port in ports
            ]
            selected = [
                p
                for p in interfaces
                if p["network_id"] == expected_network
                and any(ip["ip_address"] == host for ip in (p["fixed_ips"] or []))
            ]
            if len(selected) != 1:
                _fail("fixed IP is not bound to one exact original interface")
            return host, addresses, interfaces, _id(selected[0]["id"], "original port")
        return host, addresses

    def inspect(self, resource: dict) -> dict:
        if resource.get("source") == "api":
            return self.verify_external(resource)
        original = self._own(resource)
        self._scope()
        observed = self.controller.inspect(original["job_id"])
        allocation = original.get("flavor", {}).get("actual")
        if allocation is not None and observed["flavor_at_allocation"] != allocation:
            _fail("server flavor differs from original allocation")
        server = self.connection.compute.get_server(original["server_id"])
        host, addresses, interfaces, port_id = self._ports(original["server_id"], details=True)
        if original.get("port_id") and (port_id != original["port_id"] or host != original["private_ip"]):
            _fail("original fixed IP or interface binding changed")
        if original.get("floating_ip_phase"):
            self._floating_ip(original)
        volumes = []
        for attachment in observed["attached_volumes"]:
            volume = self.connection.block_storage.get_volume(_field(attachment, "id"))
            attachments = _field(volume, "attachments", []) or []
            if (
                _field(volume, "project_id") != self.project_id
                or len(attachments) != 1
                or _field(attachments[0], "server_id") != original["server_id"]
            ):
                _fail("attached volume is not exclusively bound to original server")
            volumes.append({k: _field(volume, k) for k in ("id", "size", "status", "volume_type")})
        expected = {original["boot_volume_id"]} if original.get("boot_volume_id") else None
        data_ids = original.get("data_volume_ids", [])
        if data_ids:
            if (
                self.purpose != "station"
                or original.get("purpose") != "station"
                or not isinstance(data_ids, list)
                or len(data_ids) != 1
                or not isinstance(data_ids[0], str)
                or not _ID.fullmatch(data_ids[0])
                or data_ids[0] == original.get("boot_volume_id")
                or expected is None
            ):
                _fail("only original station receipts may bind one dedicated data volume")
            expected.update(data_ids)
        if expected is not None and (len(volumes) != len(expected) or {v["id"] for v in volumes} != expected):
            _fail("original boot/data attachment changed")
        return {
            **original,
            "status": _field(server, "status"),
            "private_ip": host,
            "addresses": addresses,
            "port_id": port_id,
            "interfaces": interfaces,
            "volumes": volumes,
            "flavor": {"requested": observed["flavor_at_request"], "actual": observed["flavor_at_allocation"]},
        }

    def verify_external(self, resource: dict | str, *, expected_marker: dict | None = None) -> dict:
        """Read-only API consumer observation; never synthesize an SDK receipt."""
        self._scope()
        if isinstance(resource, str):
            resource = {"server_id": resource}
        server_id = _id(resource.get("server_id"), "API consumer server")
        marker = expected_marker or resource.get("expected_marker") or self.config.get("api", {}).get("expected_marker")
        if (
            not isinstance(marker, dict)
            or not marker
            or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in marker.items())
        ):
            _fail("explicit API consumer metadata marker is required")
        server = self.connection.compute.get_server(server_id)
        metadata = _field(server, "metadata", {}) or {}
        if _field(server, "project_id") != self.project_id or any(metadata.get(k) != v for k, v in marker.items()):
            _fail("API consumer caller project or marker differs")
        raw_flavor = _field(server, "flavor", {}) or {}
        actual = {
            key: _field(raw_flavor, key)
            for key in ("id", "original_name", "vcpus", "ram", "disk", "ephemeral", "swap", "extra_specs")
            if _field(raw_flavor, key) is not None
        }
        flavor_id = actual.get("id") or actual.get("original_name")
        requested = self.connection.compute.get_flavor(self.config["placement"]["flavor_id"])
        self._approved_flavor(requested)
        for field, limit in (("vcpus", "max_vcpus"), ("ram", "max_ram_mib")):
            if field in actual and (
                type(actual[field]) is not int or not 1 <= actual[field] <= self.config["approval"][limit]
            ):
                _fail("API allocated flavor exceeds approved " + limit)
        if flavor_id not in {self.config["placement"]["flavor_id"], _field(requested, "name")}:
            _fail("API consumer flavor differs from requested placement")
        if not actual.get("id"):
            if any(
                field not in actual or actual[field] != _field(requested, field) for field in ("vcpus", "ram", "disk")
            ):
                _fail("API embedded flavor snapshot differs from requested allocation")
        if "extra_specs" in actual:
            requested = self.connection.compute.fetch_flavor_extra_specs(requested)
            if actual["extra_specs"] != (_field(requested, "extra_specs", {}) or {}):
                _fail("API allocated flavor extra specs differ from requested placement")
        ports = [p for p in self.connection.network.ports(device_id=server_id) if _field(p, "device_id") == server_id]
        if not ports or any(_field(p, "project_id") != self.project_id for p in ports):
            _fail("API consumer interfaces are absent or cross-project")
        addresses = [ip["ip_address"] for p in ports for ip in (_field(p, "fixed_ips", []) or [])]
        volumes = []
        for attachment in _field(server, "attached_volumes", []) or []:
            volume = self.connection.block_storage.get_volume(_field(attachment, "id"))
            bindings = _field(volume, "attachments", []) or []
            if (
                _field(volume, "project_id") != self.project_id
                or not bindings
                or any(_field(a, "server_id") != server_id for a in bindings)
            ):
                _fail("API consumer volume ownership or attachment differs")
            volumes.append({k: _field(volume, k) for k in ("id", "size", "status", "volume_type")})
        expected_volumes = resource.get("volume_ids")
        if expected_volumes is not None and set(expected_volumes) != {v["id"] for v in volumes}:
            _fail("API consumer attachments differ from API receipt")
        return {
            **resource,
            "source": "api",
            "server_id": server_id,
            "project_id": self.project_id,
            "expected_marker": marker,
            "status": _field(server, "status"),
            "addresses": addresses,
            "flavor": {"requested_id": self.config["placement"]["flavor_id"], "observed": actual},
            "volumes": volumes,
        }

    def _pin(self, resource, host):
        original = self._own(resource)
        if original.get("floating_ip_phase"):
            self._floating_ip(original)
            if host != original.get("public_ip"):
                _fail("SSH public address differs from the original floating IP")
        elif host != self._ports(original["server_id"])[0]:
            _fail("SSH host differs from the original fixed IP")
        deadline = time.monotonic() + self._remaining(_seconds(self.config, "ready_seconds", 1200))
        marker = "PALIMPSEST_SCENARIO_HOSTKEY " + resource["job_id"] + " "
        while True:
            self.controller.inspect(resource["job_id"])
            response = self.connection.compute.get_server_console_output(resource["server_id"], length=2000)
            text = _field(response, "output", "")
            keys = set(re.findall(re.escape(marker) + r"(ssh-ed25519 [A-Za-z0-9+/]+=*)", text))
            if len(keys) > 1:
                _fail("console host identity is ambiguous")
            if keys:
                key = keys.pop()
                try:
                    blob = base64.b64decode(key.split()[1], validate=True)
                except ValueError:
                    _fail("console host key encoding is invalid")
                if len(blob) != 51 or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
                    _fail("console host key is not an Ed25519 public key")
                break
            if time.monotonic() >= deadline:
                _fail("Nova console did not publish original guest host identity")
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        ssh = copy.deepcopy(self.config["ssh"])
        known = _file(ssh["known_hosts"], "SSH known-hosts")
        if known.stat().st_size > _MAX_OUTPUT:
            _fail("known-hosts exceeds size bound")
        seed = _without_host_alias(known.read_text(), resource["job_id"])
        destination = self.operations / (resource["role"] + ".known-hosts")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(seed + "\n" + resource["job_id"] + " " + key + "\n")
        ssh.update(host=host, known_hosts=str(destination), host_key_alias=resource["job_id"])
        return ssh

    def _transport(self, ssh):
        common = [
            "-F",
            str(_file(ssh["config_file"], "SSH config")) if ssh.get("config_file") else "/dev/null",
            "-i",
            str(_file(ssh["identity_file"], "SSH identity")),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + str(_file(ssh["known_hosts"], "SSH known-hosts")),
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ClearAllForwardings=yes",
        ]
        args = [shutil.which("ssh") or "ssh", *common]
        if ssh.get("jump"):
            # Explicit proxy command applies the SAME known-host policy to jump.
            jump = ssh["jump"]
            args += ["-o", "ProxyCommand=" + shlex.join([args[0], *common, "-W", "%h:%p", jump])]
        if ssh.get("host_key_alias"):
            args += ["-o", "HostKeyAlias=" + ssh["host_key_alias"]]
        return [*args, ssh["user"] + "@" + ssh["host"]]

    def run_guest(
        self, resource: dict, argv: list[str], *, label: str, timeout: float = 300, input_bytes: bytes | None = None
    ) -> dict:
        self._require_sdk()
        if resource.get("source") != "sdk":
            _fail("API consumers do not acquire SDK guest execution authority")
        label = _id(label, "guest command label")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(x, str) or "\x00" in x for x in argv)
            or sum(len(x.encode()) for x in argv) > 120000
        ):
            _fail("guest argv must be bounded and contain no NUL")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 25200:
            _fail("guest command deadline is invalid")
        if label != "environment":
            timeout = min(timeout, _seconds(self.config, "guest_seconds", 3600))
        timeout = self._remaining(timeout)
        if input_bytes is not None and (not isinstance(input_bytes, bytes) or len(input_bytes) > _MAX_OUTPUT):
            _fail("private stdin exceeds its bound")
        observed = self.inspect(resource)
        if observed["status"] != "ACTIVE":
            _fail("guest execution requires the original active server")
        ssh = resource.get("ssh")
        hosts = [observed["public_ip"]] if observed.get("floating_ip_id") else observed["addresses"]
        if not isinstance(ssh, dict) or ssh.get("host") not in hosts:
            _fail("SSH transport is not bound to the observed guest")
        if resource.get("source") == "sdk" and ssh != self._own(resource).get("ssh"):
            _fail("SSH identity differs from original console-pinned receipt")
        prefix = resource.get("role", resource["server_id"])
        root = self.operations / (_id(prefix, "command owner") + "." + label)
        try:
            root.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            _fail("guest command label already used for this resource; no effect replay")
        intent = {
            "schema": "palimpsest.scenario-guest-command.v1",
            "server_id": resource["server_id"],
            "label": label,
            "phase": "intent",
            "private_input": input_bytes is not None,
        }
        _new_json(root / "status.json", intent)
        command = shlex.join(["timeout", "--signal=TERM", "--kill-after=10s", str(math.ceil(timeout)) + "s", *argv])
        transport = [*self._transport(ssh), command]
        started = time.monotonic()
        chunks = {"stdout": bytearray(), "stderr": bytearray()}
        timed_out = truncated = False
        # No SDK/cloud environment or SSH_AUTH_SOCK is inherited by SSH.
        process = subprocess.Popen(
            transport,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"},
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        pending = memoryview(input_bytes or b"")
        try:
            for name in ("stdout", "stderr"):
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin:
                os.set_blocking(process.stdin.fileno(), False)
                if pending:
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while selector.get_map():
                if time.monotonic() - started > timeout + 20:
                    timed_out = True
                    break
                for key, _ in selector.select(0.1):
                    if key.data == "stdin":
                        try:
                            count = os.write(key.fd, pending[:65536])
                            pending = pending[count:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        target = chunks[key.data]
                        room = _MAX_OUTPUT - len(target)
                        target.extend(data[:room])
                        if len(data) > room:
                            truncated = True
                if truncated:
                    break
            if timed_out or truncated:
                _kill_owned(process)
            try:
                returncode = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_owned(process)
                returncode = process.wait(timeout=15)
        finally:
            selector.close()
            if process.poll() is None:
                _kill_owned(process)
                process.wait(timeout=15)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        private = input_bytes is not None
        text = {
            name: "[PRIVATE INPUT: OUTPUT WITHHELD]\n" if private else _redact(bytes(value).decode(errors="replace"))
            for name, value in chunks.items()
        }
        for name, value in text.items():
            fd = os.open(root / (name + ".log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(value)
        result = {
            "returncode": returncode,
            **text,
            "elapsed_seconds": time.monotonic() - started,
            "timed_out": timed_out,
            "truncated": truncated,
            "stdout_path": str(root / "stdout.log"),
            "stderr_path": str(root / "stderr.log"),
            "paths": {name: str(root / (name + ".log")) for name in text},
        }
        if timed_out or truncated:
            result["transport_returncode"] = returncode
            result["returncode"] = 124 if timed_out else 125
        atomic_write_json(
            root / "status.json",
            {**intent, "phase": "finished", **{k: v for k, v in result.items() if k not in ("stdout", "stderr")}},
        )
        self.event(
            "guest-command",
            {
                "server_id": resource["server_id"],
                "label": label,
                "returncode": result["returncode"],
                "timed_out": timed_out,
                "truncated": truncated,
            },
        )
        return result

    def environment(self, resource: dict, output: Path) -> dict:
        """Execute the existing fio/HTTPS/TCP probes inside the fresh owned guest."""
        self._require_sdk()
        if self.purpose == "peer":
            _fail("peer guests cannot execute benchmark environment measurements")
        original = self._own(resource)
        if original.get("phase") != "ready" or not original.get("boot_id"):
            _fail("environment requires a fresh just-created ready guest")
        observed = self.inspect(resource)
        from . import environment_benchmark

        source = Path(environment_benchmark.__file__).read_bytes()
        environment = self.config["environment"]
        volume_id = original["boot_volume_id"]
        script = "\n".join(
            [
                "import base64,hashlib,json,os,runpy,signal,subprocess,tempfile",
                "from pathlib import Path",
                "def interrupted(*_): raise KeyboardInterrupt()",
                "signal.signal(signal.SIGTERM,interrupted); signal.signal(signal.SIGHUP,interrupted)",
                "boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()",
                "assert boot==" + repr(original["boot_id"]),
                "root=Path(tempfile.mkdtemp(prefix='palimpsest-scenario-environment-',dir='/tmp'))",
                "source=base64.b64decode(" + repr(base64.b64encode(source).decode()) + ")",
                "assert hashlib.sha256(source).hexdigest()==" + repr(hashlib.sha256(source).hexdigest()),
                "(root/'probe.py').write_bytes(source)",
                "config=json.loads(" + repr(json.dumps(environment)) + ")",
                "v=runpy.run_path(str(root/'probe.py'))['run_environment'](config,root/'evidence')",
                "v['boot_id']=boot",
                "v['boot_id_after']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()",
                "assert v['boot_id_after']==boot",
                "v['filesystem_binding']=json.loads(subprocess.check_output(['findmnt','--json','--target',config['disk']['directory'],'--output','SOURCE,FSTYPE,TARGET,OPTIONS,MAJ:MIN'],text=True))",
                "mount=v['filesystem_binding']['filesystems'][0]",
                "v['block_devices']=json.loads(subprocess.check_output(['lsblk','--json','--inverse','--paths','--bytes','--output','NAME,SERIAL,SIZE,TYPE',mount['source']],text=True))",
                "def flatten(nodes):\n for node in nodes:\n  yield node\n  yield from flatten(node.get('children',[]))",
                "expected=" + repr(volume_id),
                "assert any(d.get('serial') in (expected,expected[:20],expected.replace('-',''),expected.replace('-','')[:20]) for d in flatten(v['block_devices']['blockdevices']))",
                "assert v['disk'].get('status')!='success' or mount['maj:min']==str(os.major(v['disk']['device_id']))+':'+str(os.minor(v['disk']['device_id']))",
                "v['remote_evidence_directory']=str(root/'evidence')",
                "v['guest_logs']={}; remaining=65536",
                "for path in sorted((root/'evidence').iterdir()):\n if path.is_file() and path.suffix in ('.stdout','.stderr','.log'):\n  data=path.read_bytes(); kept=data[:min(4096,remaining)]; remaining-=len(kept); v['guest_logs'][path.name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'truncated':len(kept)!=len(data),'text':kept.decode(errors='replace')}",
                "print(json.dumps(v))",
            ]
        )
        command = [
            "python3",
            "-c",
            "exec(__import__('base64').b64decode(" + repr(base64.b64encode(script.encode()).decode()) + "))",
        ]
        response = self.run_guest(resource, command, label="environment", timeout=25200)
        if response["returncode"] != 0:
            _fail("guest environment measurement failed; private bounded logs retained")
        try:
            result = json.loads(response["stdout"].strip().splitlines()[-1])
        except (ValueError, IndexError):
            _fail("guest environment returned no valid result")
        if (
            result.get("schema") != "palimpsest.environment-result.v1"
            or result.get("boot_id") != original["boot_id"]
            or result.get("boot_id_after") != original["boot_id"]
        ):
            _fail("environment result boot/schema differs")
        after = self.inspect(resource)
        if after["volumes"] != observed["volumes"]:
            _fail("environment attachment changed during measurement")
        result.update(
            mode="openstack",
            server_id=original["server_id"],
            job_id=original["job_id"],
            project_id=self.project_id,
            flavor=observed["flavor"],
            volumes=observed["volumes"],
            probe_source_sha256=hashlib.sha256(source).hexdigest(),
        )
        output = Path(output).absolute()
        output.mkdir(mode=0o700, exist_ok=False)
        _private(output)
        atomic_write_json(output / "result.json", result)
        self._collect_environment_logs(resource, result, output)
        atomic_write_json(output / "result.json", result)
        return result

    def _collect_environment_logs(self, resource, result, output):
        """Recover complete bounded raw files, not lost prefixes of a deleted VM."""
        logs = result.get("guest_logs", {})
        if not isinstance(logs, dict) or len(logs) > 512:
            _fail("environment evidence manifest exceeds bound; original VM retained")
        total = 0
        for name, entry in logs.items():
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", name)
                or name in {".", ".."}
            ):
                _fail("unsafe guest evidence filename")
            if (
                type(entry.get("bytes")) is not int
                or not 0 <= entry["bytes"] <= 16 * 1024**2
                or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))
            ):
                _fail("invalid environment evidence identity")
            total += entry["bytes"]
        if total > 64 * 1024**2:
            _fail("environment evidence exceeds 64MiB transfer bound; original VM retained")
        raw = output / "raw"
        raw.mkdir(mode=0o700)
        read_chunk = """import base64,json,os,stat,sys
from pathlib import PurePosixPath
root=PurePosixPath(sys.argv[1]);name=sys.argv[2];offset=int(sys.argv[3]);count=int(sys.argv[4])
assert root.is_absolute() and len(root.parts)==4 and root.parts[1]=='tmp' and root.parts[2].startswith('palimpsest-scenario-environment-') and root.parts[3]=='evidence'
assert '/' not in name and name not in ('.','..') and 0<=offset<=16777216 and 1<=count<=262144
directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
try:
 for part in root.parts[1:]:
  next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory);os.close(directory);directory=next_fd
 fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
 with os.fdopen(fd,'rb') as source:
  info=os.fstat(source.fileno());assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_size<=16777216
  source.seek(offset);data=source.read(count)
 print(json.dumps({'offset':offset,'data':base64.b64encode(data).decode()}))
finally:os.close(directory)
"""
        for name, entry in logs.items():
            fd = os.open(raw / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                data = entry.get("text", "").encode()
                if (
                    entry.get("truncated")
                    or len(data) != entry["bytes"]
                    or hashlib.sha256(data).hexdigest() != entry["sha256"]
                ):
                    data = b""
                stream.write(data)
                digest, offset = hashlib.sha256(data), len(data)
                while offset < entry["bytes"]:
                    response = self.run_guest(
                        resource,
                        [
                            "python3",
                            "-c",
                            read_chunk,
                            result.get("remote_evidence_directory", ""),
                            name,
                            str(offset),
                            str(min(262144, entry["bytes"] - offset)),
                        ],
                        label="environment-log-" + hashlib.sha256(name.encode()).hexdigest()[:12] + "-" + str(offset),
                        timeout=60,
                    )
                    if response["returncode"]:
                        _fail("environment raw evidence transfer failed; original VM retained")
                    piece = json.loads(response["stdout"])
                    block = base64.b64decode(piece["data"], validate=True)
                    if piece["offset"] != offset or not block or len(block) > min(262144, entry["bytes"] - offset):
                        _fail("environment raw evidence offset or size differs")
                    stream.write(block)
                    digest.update(block)
                    offset += len(block)
                stream.flush()
                os.fsync(stream.fileno())
                if digest.hexdigest() != entry["sha256"]:
                    _fail("complete probe log byte identity differs; original VM retained")
            entry.pop("text", None)
            entry.update(collected=True, local_file=str(raw / name))

    def _absent(self, proxy, identity, deadline):
        # find/get(ignore_missing=True) returns None only for an actual 404.
        while True:
            item = proxy(identity, ignore_missing=True)
            if item is None:
                return
            if _field(item, "id") != identity:
                _fail("absence probe resolved a different resource identity")
            if time.monotonic() >= deadline:
                _fail("ordinary deletion absence deadline expired; budget remains reserved")
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def _available_boot(self, volume_id, server_id, deadline):
        while True:
            volume = self.connection.block_storage.get_volume(volume_id)
            bindings = _field(volume, "attachments", []) or []
            if (
                _field(volume, "id") != volume_id
                or _field(volume, "project_id") != self.project_id
                or any(_field(a, "server_id") != server_id for a in bindings)
            ):
                _fail("original boot volume identity or exclusive ownership changed")
            if not bindings and _field(volume, "status") == "available":
                return volume
            if time.monotonic() >= deadline:
                _fail("original boot volume detach deadline expired; retained")
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def delete(self, resource: dict) -> dict:
        """First-time ordinary deletes; later cleanup only settles recorded intents."""
        self._require_sdk(allow_cleanup=True)
        original = self._own(resource, allow_unallocated=True)
        self._scope()
        if not original.get("server_id"):
            path = self.receipts / (original["job_id"] + ".json")
            if path.exists() or path.is_symlink():
                created = self.controller.inspect(original["job_id"])
                if created.get("uncertain_create") or not created.get("server_id"):
                    _fail("original create remains uncertain; no broad discovery or destructive replay")
                original.update(
                    server_id=created["server_id"],
                    flavor={"requested": created["flavor_at_request"], "actual": created.get("flavor_at_allocation")},
                    phase="created",
                )
                self._save(original)
                self.budget.bind(original["reservation_id"], original["server_id"])
            else:
                original["phase"] = "aborted-before-request"
                self._save(original)
                self.budget.release(original["reservation_id"])
                original["budget_released"] = True
                self._save(original)
                return original
        phase = original.get("phase")
        volume_id = original.get("boot_volume_id")
        deadline = time.monotonic() + _seconds(self.config, "delete_seconds", 300)
        self._delete_floating_ip(original, deadline)
        if phase == "absent":
            self.verify_absent(original["server_id"], volume_id)
            if not original.get("budget_released"):
                self.budget.release(original["reservation_id"])
                original["budget_released"] = True
                self._save(original)
            return original
        if phase in {"delete-server-intent", "delete-volume-intent"}:
            if self.connection.compute.find_server(original["server_id"], ignore_missing=True) is not None:
                _fail("delete intent exists; original server still present; no destructive replay")
            if phase == "delete-volume-intent":
                if (
                    not volume_id
                    or self.connection.block_storage.find_volume(volume_id, ignore_missing=True) is not None
                ):
                    _fail("delete intent exists; original boot volume still present; no destructive replay")
        else:
            # Ownership is the original Nova receipt and current marker, not
            # guest readiness, SSH routing, ports, or ACTIVE scheduling status.
            observed = self.controller.inspect(original["job_id"])
            if observed.get("server_id") != original["server_id"]:
                _fail("original Nova allocation differs")
            response = self.connection.compute.get(
                "/servers/" + original["server_id"] + "/os-volume_attachments", microversion="2.79"
            )
            response.raise_for_status()
            attachments = response.json().get("volumeAttachments")
            if (
                not isinstance(attachments, list)
                or len(attachments) > 1
                or any(a.get("delete_on_termination") is not False for a in attachments)
            ):
                _fail("ordinary exact boot-volume retention cannot be attested")
            if attachments:
                attached_id = _id(attachments[0].get("volumeId"), "original boot volume")
                if volume_id is not None and volume_id != attached_id:
                    _fail("original boot volume allocation differs")
                volume_id = attached_id
                volume = self.connection.block_storage.get_volume(volume_id)
                bindings = _field(volume, "attachments", []) or []
                if (
                    _field(volume, "id") != volume_id
                    or _field(volume, "project_id") != self.project_id
                    or len(bindings) != 1
                    or _field(bindings[0], "server_id") != original["server_id"]
                ):
                    _fail("attached boot volume is not exclusively bound to original server")
                original.update(
                    boot_volume_id=volume_id,
                    volumes_at_allocation=original.get("volumes_at_allocation")
                    or [{k: _field(volume, k) for k in ("id", "size", "status", "volume_type")}],
                )
            elif volume_id is not None:
                _fail("original boot attachment disappeared; refusing unrelated cleanup")
            else:
                # ERROR/NoValidHost can precede creation of any BFV attachment.
                # This proves no attachment, not absence of an unknown disk.
                original.update(boot_volume_id=None, no_boot_attachment_observed=True)
            original["phase"] = "delete-server-intent"
            self._save(original)
            self.controller.delete(original["job_id"])
            self._absent(self.connection.compute.find_server, original["server_id"], deadline)
        if volume_id is not None and phase != "delete-volume-intent":
            self._available_boot(volume_id, original["server_id"], deadline)
            original["phase"] = "delete-volume-intent"
            self._save(original)
            self.connection.block_storage.delete_volume(volume_id, ignore_missing=False)
            self._absent(self.connection.block_storage.find_volume, volume_id, deadline)
        original.update(phase="absent", server_absent=True, boot_volume_absent=True if volume_id is not None else None)
        self._save(original)
        self.budget.release(original["reservation_id"])
        original["budget_released"] = True
        self._save(original)
        self.event(
            "guest-deleted",
            {
                "server_id": original["server_id"],
                "boot_volume_id": volume_id,
                "server_absent": True,
                "boot_volume_absent": original["boot_volume_absent"],
            },
        )
        return original
