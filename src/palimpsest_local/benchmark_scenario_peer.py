"""Fresh, approval-budgeted private throughput peer and console-pinned bastion.

Only original local receipts authorize cleanup. An interrupted request is inspected,
never repeated; uncertain ownership holds its reservation for operator disposition.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .benchmark_scenario_cloud import ScenarioCloud, _field, _id, _new_json, _private, _read, validate_cloud_config
from .benchmark_scenarios import ResourceBudget, _digest, _executor_lock, _source_identity
from .errors import ArtifactValidationError
from .state import atomic_write_json

SCHEMA = "palimpsest.benchmark-peer.v1"
RESULT_SCHEMA = "palimpsest.benchmark-peer-result.v1"
_SECRET = re.compile(r"password|secret|credential|authorization|(^|_)token($|_)", re.I)
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_RULE_FIELDS = (
    "direction",
    "ether_type",
    "protocol",
    "port_range_min",
    "port_range_max",
    "remote_ip_prefix",
    "remote_group_id",
    "remote_address_group_id",
)


def _fail(message):
    raise ArtifactValidationError("benchmark peer: " + message)


def validate_peer(config, *, cleanup=False):
    """Validate the envelope; original-receipt cleanup needs no SSH files or unexpired grant."""
    if not isinstance(config, dict) or config.get("schema") != SCHEMA or config.get("scenario") != "peer":
        _fail("explicit benchmark-peer.v1 configuration required")
    if not isinstance(config.get("job_id"), str) or not _NAME.fullmatch(config["job_id"]):
        _fail("bounded job_id required")

    def credentials(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET.search(str(key)):
                    _fail("configuration must contain file references, never credentials")
                credentials(item)
        elif isinstance(value, list):
            for item in value:
                credentials(item)
        elif isinstance(value, str) and (
            "PRIVATE KEY-----" in value or "\x00" in value or "\r" in value or "\n" in value
        ):
            _fail("configuration contains unsafe literal data")

    credentials(config)
    if any(key in config for key in ("setup_hooks", "before_hooks", "after_hooks", "argv", "command", "program")):
        _fail("peer uses a fixed service program, never arbitrary hooks")
    validate_cloud_config(config, require_ssh=not cleanup, purpose="peer")
    approval = config.get("approval", {})
    scenarios = approval.get("allowed_scenarios")
    if (
        not isinstance(scenarios, list)
        or any(not isinstance(item, str) for item in scenarios)
        or "peer" not in scenarios
    ):
        _fail("allowed_scenarios must list the exact peer scenario")
    if (
        not isinstance(approval.get("id"), str)
        or not _NAME.fullmatch(approval["id"])
        or approval.get("project_id") != config["project_id"]
        or "peer" not in scenarios
    ):
        _fail("peer must belong to the explicit project approval")
    if approval.get("allow_gpu", False) is not False or approval.get("allow_cluster", False) is not False:
        _fail("peer grant does not permit GPU or cluster allocation")
    for key, maximum in (
        ("max_creates", 3),
        ("max_live", 2),
        ("max_boot_gib", 80),
        ("max_security_groups", 1),
        ("max_floating_ips", 1),
    ):
        allowed, requested = approval.get(key), config.get("resources", {}).get(key)
        if (
            type(allowed) is not int
            or not 1 <= allowed <= maximum
            or type(requested) is not int
            or not 1 <= requested <= allowed
        ):
            _fail("resource envelope exceeds the fresh peer grant")
    if approval.get("max_vcpus") != 2 or approval.get("max_ram_mib") != 8192 or config["placement"]["boot_gib"] != 20:
        _fail("peer requires the approved 2-vCPU/8192-MiB/20-GiB envelope")
    if config["placement"].get("security_group_ids") != []:
        _fail("existing security groups cannot be adopted")
    if not config["placement"].get("floating_network_id"):
        _fail("one fresh peer floating IP is required")
    if config.get("peer", {}).get("port") != 5201:
        _fail("peer port must be 5201")
    if not cleanup:
        ssh = config["ssh"]
        if ssh.get("jump") or ssh.get("config_file") or ssh.get("host"):
            _fail("fresh peer requires direct SSH, not an inherited jump or host")
        for key in ("identity_file", "public_key_file", "known_hosts"):
            if not Path(ssh[key]).expanduser().is_absolute() or any(c in ssh[key] for c in ('"', "%")):
                _fail("SSH references must be absolute literal paths")
    deadline = config.get("deadline_seconds", 7200)
    if type(deadline) is not int or not 60 <= deadline <= 25200:
        _fail("deadline_seconds must be 60..25200")
    root = approval.get("ledger_root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        _fail("approval requires an absolute shared ledger root")
    try:
        expiry = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or (not cleanup and expiry.timestamp() <= time.time()):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        _fail("timezone-aware unexpired approval required")


def _rules(group_id):
    rules = []

    def rule(direction, protocol, port, **remote):
        rules.append(
            dict(
                direction=direction,
                ether_type="IPv4",
                protocol=protocol,
                port_range_min=port,
                port_range_max=port,
                **remote,
            )
        )

    for address in ("172.31.0.50/32", "172.30.0.50/32"):
        rule("ingress", "tcp", 22, remote_ip_prefix=address)
    for direction in ("ingress", "egress"):
        for port in (22, 5201):
            rule(direction, "tcp", port, remote_group_id=group_id)
    for protocol, port in (("tcp", 80), ("tcp", 443), ("tcp", 53), ("udp", 53), ("udp", 123)):
        rule("egress", protocol, port, remote_ip_prefix="0.0.0.0/0")
    return rules


def _aliased(row, key, wire):
    canonical, legacy = _field(row, key), _field(row, wire)
    if canonical is not None and legacy is not None and canonical != legacy:
        _fail("conflicting SDK and wire resource ownership fields")
    return canonical if canonical is not None else legacy


def _rule(row):
    value = {key: _field(row, key) for key in _RULE_FIELDS}
    value["ether_type"] = _aliased(row, "ether_type", "ethertype")
    return value


def _not_found(error):
    return getattr(error, "status_code", None) == 404 or getattr(error, "http_status", None) == 404


def _get(proxy, identity):
    try:
        row = proxy(identity)
    except Exception as error:
        if _not_found(error):
            return None
        raise
    if row is None or _field(row, "id") != identity:
        _fail("exact GET returned no identifiable resource; only 404 proves absence")
    return row


class _SecurityGroup:
    def __init__(self, cloud, budget, output):
        self.cloud, self.budget, self.output = cloud, budget, Path(output)
        self.path = self.output / "peer-security-group.json"
        self.network = cloud.connection.network
        self.marker = "palimpsest-peer:" + cloud.config["approval"]["id"] + ":" + cloud.config["job_id"]
        self.name = "palimpsest-" + cloud.config["job_id"] + "-peer"

    def _save(self, row):
        atomic_write_json(self.path, row)

    def _check(self, group, row):
        self.cloud._scope()
        if (
            _field(group, "id") != row["id"]
            or _field(group, "project_id") != self.cloud.project_id
            or _field(group, "description") != self.marker
            or _field(group, "name") != self.name
        ):
            _fail("security group differs from the original project/scope receipt")

    def _wait_absent(self, proxy, identity):
        deadline = time.monotonic() + min(300, self.cloud.config.get("timeouts", {}).get("delete_seconds", 300))
        while _get(proxy, identity) is not None:
            if time.monotonic() >= deadline:
                _fail("ordinary delete remains uncertain; reservation held without replay")
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def _remaining(self):
        self.cloud._remaining(1)
        self.cloud._scope()

    def create(self):
        self._remaining()
        if self.path.exists() or self.path.is_symlink():
            _fail("security group intent exists; never replay allocation")
        row = {
            "schema": "palimpsest.peer-security-group.v1",
            "project_id": self.cloud.project_id,
            "name": self.name,
            "description": self.marker,
            "phase": "create-intent",
            "reservation_id": self.budget.reserve_aux("security_groups", "peer-sg"),
            "rules": [],
            "defaults": [],
        }
        _new_json(self.path, row)
        group = self.network.create_security_group(
            name=self.name, description=self.marker, project_id=self.cloud.project_id
        )
        row["id"] = _id(_field(group, "id"), "fresh security group")
        self._save(row)
        self._check(group, row)
        self.budget.bind_aux(row["reservation_id"], row["id"])
        group = _get(self.network.get_security_group, row["id"])
        self._check(group, row)
        defaults = _field(group, "security_group_rules", [])
        if not isinstance(defaults, list):
            _fail("provider default rules unavailable")
        for default in defaults:
            spec = _rule(default)
            if (
                spec["direction"] != "egress"
                or spec["ether_type"] not in {"IPv4", "IPv6"}
                or any(
                    spec[key] is not None for key in ("protocol", "port_range_min", "port_range_max", "remote_group_id")
                )
                or spec["remote_ip_prefix"] not in (None, "0.0.0.0/0" if spec["ether_type"] == "IPv4" else "::/0")
                or _field(default, "security_group_id") != row["id"]
                or _aliased(default, "project_id", "tenant_id") != self.cloud.project_id
                or spec["remote_address_group_id"] is not None
            ):
                _fail("unexpected provider rule; no mutation or adoption")
            row["defaults"].append(
                {"id": _id(_field(default, "id"), "original default rule"), "spec": spec, "phase": "observed"}
            )
        # Record the complete original default-rule set before the first removal.
        row["phase"] = "remove-defaults"
        self._save(row)
        for default in row["defaults"]:
            self._remaining()
            self._check(_get(self.network.get_security_group, row["id"]), row)
            self._check_rule(_get(self.network.get_security_group_rule, default["id"]), row, default)
            default["phase"] = "delete-intent"
            self._save(row)
            self.network.delete_security_group_rule(default["id"], ignore_missing=False)
            self._wait_absent(self.network.get_security_group_rule, default["id"])
            default["phase"] = "absent"
            self._save(row)
        for index, spec in enumerate(_rules(row["id"])):
            self._remaining()
            self._check(_get(self.network.get_security_group, row["id"]), row)
            intent = {"spec": _rule(spec), "description": self.marker + ":rule:" + str(index), "phase": "create-intent"}
            row["rules"].append(intent)
            self._save(row)
            rule = self.network.create_security_group_rule(
                security_group_id=row["id"], project_id=self.cloud.project_id, description=intent["description"], **spec
            )
            intent["id"] = _id(_field(rule, "id"), "fresh security group rule")
            self._save(row)
            self._check_rule(rule, row, intent)
            intent["phase"] = "created"
            self._save(row)
        row["phase"] = "ready"
        self._save(row)
        self._verify_rules(_get(self.network.get_security_group, row["id"]), row)
        return row["id"]

    def _check_rule(self, rule, row, recorded):
        if (
            rule is None
            or _field(rule, "id") != recorded.get("id")
            or _aliased(rule, "project_id", "tenant_id") != self.cloud.project_id
            or _field(rule, "security_group_id") != row["id"]
            or _rule(rule) != recorded["spec"]
            or ("description" in recorded and _field(rule, "description") != recorded["description"])
        ):
            _fail("security group rule differs from original receipt")

    def _verify_rules(self, group, row):
        self._check(group, row)
        current = _field(group, "security_group_rules")
        if not isinstance(current, list):
            _fail("security group rules cannot be attested")
        for rule in current:
            matches = [r for r in row["rules"] + row["defaults"] if r.get("id") == _field(rule, "id")]
            if not matches:
                # Lost POST response: accept only the exact already-recorded rule
                # intent, scope, owner and complete rule specification, never a retry.
                matches = [
                    r
                    for r in row["rules"]
                    if not r.get("id") and r["description"] == _field(rule, "description") and r["spec"] == _rule(rule)
                ]
                if len(matches) == 1:
                    matches[0]["id"] = _id(_field(rule, "id"), "original rule intent")
                    self._check_rule(rule, row, matches[0])
                    self._save(row)
            if len(matches) != 1:
                _fail("unrecorded or ambiguous rule prevents security group cleanup")
            self._check_rule(rule, row, matches[0])
            if matches[0].get("phase") == "absent":
                _fail("previously absent provider rule reappeared")
        if row["phase"] == "ready" and {r["id"] for r in row["rules"]} != {_field(r, "id") for r in current}:
            _fail("ready security group rule inventory changed")

    def cleanup(self):
        if not self.path.exists():
            return None
        row = _read(self.path)
        if (
            row.get("project_id") != self.cloud.project_id
            or row.get("name") != self.name
            or row.get("description") != self.marker
            or row.get("schema") != "palimpsest.peer-security-group.v1"
        ):
            _fail("security group original receipt changed")
        self.cloud._scope()
        if not row.get("id"):
            matches = [
                g
                for g in self.network.security_groups(project_id=self.cloud.project_id)
                if _field(g, "description") == self.marker
                and _field(g, "name") == self.name
                and _field(g, "project_id") == self.cloud.project_id
            ]
            if len(matches) != 1:
                _fail("security group create uncertain; hold without allocation replay")
            row["id"] = _id(_field(matches[0], "id"), "original security group")
            self._save(row)
            self.budget.bind_aux(row["reservation_id"], row["id"])
            # A lost group POST never authorizes adopting unknown provider rules.
            if _field(matches[0], "security_group_rules"):
                _fail("original provider defaults were not recorded; hold security group")
        group = _get(self.network.get_security_group, row["id"])
        if group is not None:
            self._check(group, row)
            self.budget.bind_aux(row["reservation_id"], row["id"])
            if row["phase"] in {"delete-intent", "absent"}:
                if row["phase"] == "absent":
                    _fail("previously absent security group reappeared")
                self._wait_absent(self.network.get_security_group, row["id"])
            else:
                self._verify_rules(group, row)
                # Neutron has returned unrelated ports for security_group_id filters.
                # Enumerate all visible project ports and test membership ourselves.
                for port in self.network.ports(project_id=self.cloud.project_id):
                    if row["id"] in (_field(port, "security_group_ids", []) or []):
                        _fail("security group still attached; remove targets before peer cleanup")
                row["phase"] = "delete-intent"
                self._save(row)
                self.cloud._scope()
                self.network.delete_security_group(row["id"], ignore_missing=False)
                self._wait_absent(self.network.get_security_group, row["id"])
        elif row["phase"] not in {"delete-intent", "absent"}:
            _fail("security group disappeared without original deletion intent")
        row["phase"] = "absent"
        self._save(row)
        self.budget.release_aux(row["reservation_id"])
        row["budget_released"] = True
        self._save(row)
        return row["id"]


_PACKAGE_SERVICE_SETUP = """stock='iperf3.service'
info=subprocess.check_output(['systemctl','show',stock,'--property=LoadState','--property=FragmentPath','--property=DropInPaths'],text=True,timeout=10)
package=dict(line.split('=',1) for line in info.splitlines())
if package.get('LoadState')=='loaded':
 fragment=package.get('FragmentPath','')
 listed=subprocess.check_output(['dpkg-query','--listfiles','iperf3'],text=True,timeout=10).splitlines()
 approved={'/lib/systemd/system/iperf3.service','/usr/lib/systemd/system/iperf3.service'}
 if fragment not in approved or package.get('DropInPaths') or not any(os.path.realpath(path)==os.path.realpath(fragment) for path in listed if path in approved):
  raise SystemExit('iperf3 service is not an unmodified package unit identity')
 metadata=os.stat(fragment,follow_symlinks=False)
 if metadata.st_uid!=0 or not stat.S_ISREG(metadata.st_mode) or metadata.st_mode&0o022:
  raise SystemExit('unsafe iperf3 package unit ownership')
 verification=subprocess.run(['dpkg','--verify','iperf3'],check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
 if verification.stdout.strip():raise SystemExit('iperf3 package payload changed; no service mutation')
 subprocess.run(['systemctl','disable','--now',stock],check=True,timeout=30)
 stopped=subprocess.check_output(['systemctl','show',stock,'--property=ActiveState','--property=MainPID'],text=True,timeout=10)
 stopped=dict(line.split('=',1) for line in stopped.splitlines())
 if stopped.get('ActiveState')!='inactive' or stopped.get('MainPID')!='0':raise SystemExit('packaged iperf3 service did not stop')
elif package.get('LoadState')!='not-found':
 raise SystemExit('iperf3 package unit state cannot be attested')
"""


_SERVICE = (
    """import ipaddress,json,os,socket,stat,subprocess,sys,time
"""
    + _PACKAGE_SERVICE_SETUP
    + """
address=str(ipaddress.IPv4Address(sys.argv[1]))
unit='palimpsest-benchmark-peer.service'
path='/etc/systemd/system/'+unit
text='[Unit]\\nDescription=Palimpsest private benchmark peer\\nAfter=network-online.target\\n[Service]\\nType=simple\\nExecStart=/usr/bin/iperf3 --server --bind '+address+' --port 5201\\nRestart=no\\nUser=nobody\\nNoNewPrivileges=yes\\nProtectSystem=strict\\nProtectHome=yes\\nPrivateTmp=yes\\n[Install]\\nWantedBy=multi-user.target\\n'
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
with os.fdopen(fd,'w') as stream:
 stream.write(text);stream.flush();os.fsync(stream.fileno())
subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)
subprocess.run(['systemctl','start',unit],check=True,timeout=30)
for attempt in range(30):
 info=subprocess.check_output(['systemctl','show',unit,'--property=MainPID','--property=ActiveState','--property=SubState'],text=True,timeout=10)
 fields=dict(line.split('=',1) for line in info.splitlines())
 pid=int(fields.get('MainPID','0'))
 if fields.get('ActiveState')=='active' and fields.get('SubState')=='running' and pid>1:
  args=open('/proc/'+str(pid)+'/cmdline','rb').read().split(b'\\0')
  expected=[b'/usr/bin/iperf3',b'--server',b'--bind',address.encode(),b'--port',b'5201']
  if args[:6]!=expected:raise SystemExit('unexpected peer process')
  inodes={os.readlink('/proc/'+str(pid)+'/fd/'+name) for name in os.listdir('/proc/'+str(pid)+'/fd')}
  endpoint=socket.inet_aton(address)[::-1].hex().upper()+':1451'
  listeners=[line.split() for line in open('/proc/net/tcp').readlines()[1:]]
  if any(row[1]==endpoint and row[3]=='0A' and 'socket:['+row[9]+']' in inodes for row in listeners):
   with socket.create_connection((address,5201),timeout=5):pass
   print(json.dumps({'ready':True,'private_ip':address,'port':5201,'unit':unit,'pid':pid,'socket_owned_by_process':True}));break
 time.sleep(1)
else:raise SystemExit('peer process/listener readiness deadline expired')
"""
)


def _source():
    result = _source_identity()
    files = dict(result["files"])
    files["benchmark_scenario_peer.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    launcher = Path(__file__).resolve().parents[2] / "scripts" / "benchmarks" / "peer.py"
    files["scripts/benchmarks/peer.py"] = hashlib.sha256(launcher.read_bytes()).hexdigest()
    result.update(files=files, sha256=_digest(files))
    return result


@contextmanager
def _interrupt_cleanup():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _connection_data(resource, group_id, output):
    ssh = resource["ssh"]
    private, public = (
        str(ipaddress.IPv4Address(resource["private_ip"])),
        str(ipaddress.IPv4Address(resource["public_ip"])),
    )
    if (
        not ipaddress.ip_address(private).is_private
        or ssh.get("host") != public
        or ssh.get("host_key_alias") != resource["job_id"]
    ):
        _fail("peer transport is not console-pinned to its original floating IP")
    known = Path(ssh["known_hosts"])
    _readable = known.read_text()
    if not any(line.startswith(resource["job_id"] + " ") for line in _readable.splitlines()):
        _fail("peer host-key alias absent from pinned known-hosts")

    def quote(value):
        if not isinstance(value, str) or any(c in value for c in ('"', "\n", "\r", "\x00", "%")):
            _fail("unsafe SSH configuration data")
        return '"' + value + '"'

    config = (
        "Host "
        + public
        + "\n  HostName "
        + public
        + "\n  User "
        + ssh["user"]
        + "\n  IdentityFile "
        + quote(ssh["identity_file"])
        + "\n  UserKnownHostsFile "
        + quote(str(known))
        + "\n  HostKeyAlias "
        + resource["job_id"]
        + "\n  StrictHostKeyChecking yes\n  GlobalKnownHostsFile /dev/null\n  ForwardAgent no\n"
        "  IdentitiesOnly yes\n  IdentityAgent none\n  BatchMode yes\n  PasswordAuthentication no\n"
        "  KbdInteractiveAuthentication no\n  ClearAllForwardings yes\n  ConnectTimeout 15\n"
    )
    destination = output / "peer-ssh.config"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(config)
        stream.flush()
        os.fsync(stream.fileno())
    data = {
        "schema": "palimpsest.benchmark-peer-connection.v1",
        "private_ip": private,
        "public_ip": public,
        "port": 5201,
        "security_group_ids": [group_id],
        "ssh_jump": ssh["user"] + "@" + public,
        "ssh_config_file": str(destination),
        "pinned_known_hosts": str(known),
        "host_key_alias": resource["job_id"],
        "server_id": resource["server_id"],
        "floating_ip_id": resource["floating_ip_id"],
    }
    _new_json(output / "connection.json", data)
    return data


def _state(output):
    _private(output)
    config, result = _read(output / "config.json"), _read(output / "result.json")
    if result.get("schema") != RESULT_SCHEMA or result.get("config_sha256") != _digest(config):
        _fail("original configuration or result receipt changed")
    return config, result


def _events(output, state):
    def event(kind, detail):
        # No raw exception, SDK response, guest stream or arbitrary detail is public.
        state["phase"] = kind if isinstance(kind, str) and re.fullmatch(r"[a-z0-9-]{1,80}", kind) else "cloud-event"
        state["heartbeat_unix_seconds"] = time.time()
        atomic_write_json(output / "result.json", state)

    return event


def _cleanup(cloud, budget, output):
    with budget._locked() as ledger:
        if any(
            row["job_id"] != cloud.config["job_id"] and not row["released"]
            for row in ledger.get("reservations", {}).values()
        ):
            _fail("approved target reservations remain live or uncertain; retain peer")
    group = _SecurityGroup(cloud, budget, output)
    resources = cloud.resources()
    if group.path.exists():
        original = _read(group.path)
        group_id = original.get("id")
        own_servers = {r["server_id"] for r in resources if r.get("server_id")}
        for resource in resources:
            if not resource.get("server_id"):
                original_vm = cloud.controller.inspect(resource["job_id"])
                if (
                    original_vm.get("uncertain_create")
                    or not original_vm.get("server_id")
                    or original_vm.get("project_id") != cloud.project_id
                    or original_vm.get("job_id") != resource["job_id"]
                ):
                    _fail("original peer allocation remains uncertain; retain peer without replay")
                own_servers.add(_id(original_vm["server_id"], "original peer server"))
        cloud._scope()
        if group_id:
            for port in cloud.connection.network.ports(project_id=cloud.project_id):
                if group_id in (_field(port, "security_group_ids", []) or []):
                    if _field(port, "project_id") != cloud.project_id or _field(port, "device_id") not in own_servers:
                        _fail("target or unknown port still uses peer group; retain peer until targets are removed")
    for resource in reversed(cloud.resources()):
        cloud.delete(resource)
    if cloud.resources():
        _fail("original peer VM/FIP/boot cleanup remains unresolved")
    group_id = group.cleanup()
    return {"security_group_id": group_id, "original_resources_absent": True}


def _error_fields(error, stage):
    name = type(error).__name__
    fields = {
        "error_category": name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception",
        "failed_stage": stage,
    }
    for key in ("status_code", "http_status"):
        value = getattr(error, key, None)
        if type(value) is int and 100 <= value <= 599:
            fields[key] = value
    return fields


def create_peer(config, output, *, connection=None):
    validate_peer(config)
    config = copy.deepcopy(config)
    output = Path(output).expanduser().absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    _private(output)
    with _executor_lock(output), _interrupt_cleanup():
        _new_json(output / "config.json", config)
        state = {
            "schema": RESULT_SCHEMA,
            "job_id": config["job_id"],
            "scenario": "peer",
            "status": "running",
            "phase": "preflight",
            "config_sha256": _digest(config),
            "started_unix_seconds": time.time(),
        }
        _new_json(output / "result.json", state)
        _new_json(output / "runner-source.json", _source())
        cloud = budget = None
        try:
            state["phase"] = "approval-budget"
            budget = ResourceBudget(config, output)
            state["phase"] = "peer-cloud-preflight"
            cloud = ScenarioCloud(
                config, output, _events(output, state), connection=connection, budget=budget, purpose="peer"
            )
            state["phase"] = "peer-security-group-create"
            group_id = _SecurityGroup(cloud, budget, output).create()
            cloud.config["placement"]["security_group_ids"] = [group_id]
            state["phase"] = "peer-vm-create"
            resource = cloud.create("peer")
            private = ipaddress.IPv4Address(resource["private_ip"])
            if (
                not private.is_private
                or private.is_loopback
                or private.is_link_local
                or private.is_unspecified
                or private.is_multicast
            ):
                _fail("peer requires an owned private IPv4 address")
            state["phase"] = "peer-service-start"
            reply = cloud.run_guest(
                resource,
                ["sudo", "-n", "python3", "-c", _SERVICE, str(private)],
                label="peer-iperf-service",
                timeout=120,
            )
            try:
                readiness = json.loads(reply["stdout"])
                if (
                    reply["returncode"] != 0
                    or readiness.get("ready") is not True
                    or readiness.get("private_ip") != str(private)
                    or readiness.get("port") != 5201
                    or readiness.get("socket_owned_by_process") is not True
                    or readiness.get("unit") != "palimpsest-benchmark-peer.service"
                    or type(readiness.get("pid")) is not int
                    or readiness["pid"] <= 1
                ):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                _fail("peer process and bound socket readiness was not demonstrated")
            _new_json(
                output / "service.json",
                {
                    key: readiness[key]
                    for key in ("ready", "private_ip", "port", "unit", "pid", "socket_owned_by_process")
                },
            )
            state["phase"] = "peer-connection-export"
            data = _connection_data(resource, group_id, output)
            state.update(status="ready", phase="peer-ready", connection=data, server_id=resource["server_id"])
        except BaseException as error:
            state.update(_error_fields(error, state["phase"]))
            state.update(
                status="held",
                phase="interrupted" if isinstance(error, KeyboardInterrupt) else "create-failed",
                issue="Peer creation did not complete; only original-receipt cleanup is permitted.",
            )
            atomic_write_json(output / "result.json", state)
            if cloud is not None:
                try:
                    state["cleanup"] = _cleanup(cloud, budget, output)
                    state.update(status="failed", phase="failed-cleaned")
                except BaseException as cleanup_error:
                    state.update(
                        status="held", phase="cleanup-held", cleanup_error=_error_fields(cleanup_error, state["phase"])
                    )
            state["finished_unix_seconds"] = time.time()
            atomic_write_json(output / "result.json", state)
            if not isinstance(error, Exception):
                raise
            return status_peer(output)
        finally:
            if cloud is not None and connection is None:
                cloud.connection.close()
        atomic_write_json(output / "result.json", state)
        return status_peer(output)


def status_peer(output):
    output = Path(output).expanduser().absolute()
    _, result = _state(output)
    return {
        key: result[key]
        for key in (
            "schema",
            "job_id",
            "scenario",
            "status",
            "phase",
            "started_unix_seconds",
            "heartbeat_unix_seconds",
            "finished_unix_seconds",
            "server_id",
            "connection",
            "issue",
            "cleanup",
            "error_category",
            "failed_stage",
            "status_code",
            "http_status",
            "cleanup_error",
        )
        if key in result
    }


def cleanup_peer(output, *, connection=None):
    output = Path(output).expanduser().absolute()
    _private(output)
    with _executor_lock(output), _interrupt_cleanup():
        config, state = _state(output)
        validate_peer(config, cleanup=True)
        budget = ResourceBudget(config, output)
        cloud = ScenarioCloud(
            config,
            output,
            _events(output, state),
            connection=connection,
            budget=budget,
            purpose="peer",
            cleanup_only=True,
        )
        try:
            state["phase"] = "peer-cleanup"
            state["cleanup"] = _cleanup(cloud, budget, output)
            state.update(status="deleted", phase="original-resources-absent")
        except Exception as error:
            state.update(_error_fields(error, state["phase"]))
            state.update(
                status="held",
                phase="cleanup-held",
                issue="Original resource cleanup is uncertain or permission-bound; no requests replayed.",
            )
        finally:
            state["finished_unix_seconds"] = time.time()
            atomic_write_json(output / "result.json", state)
            if connection is None:
                cloud.connection.close()
        return status_peer(output)
