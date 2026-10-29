"""One retained, original-receipt Nova station and its dedicated Cinder disk.

Every mutating request has a durable intent. An interrupted request is inspected,
never repeated; creation and measurements never imply permission to clean up.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .benchmark_scenario_cloud import ScenarioCloud, _id, _new_json, _private, _read, validate_cloud_config
from .errors import ArtifactValidationError
from .nova_build import _field
from .state import atomic_write_json

SCHEMA = "palimpsest.benchmark-station.v1"
_MOUNTPOINT = "/mnt/palimpsest-benchmark"
_ROLE = "station"
_SECRET = re.compile(r"password|secret|credential|authorization|(^|_)token($|_)", re.IGNORECASE)


def _fail(message):
    raise ArtifactValidationError("benchmark station: " + message)


class StationOperationBusy(ArtifactValidationError):
    """Another measurement or lifecycle operation owns the retained station."""


@contextmanager
def station_operation(root):
    """Fence the canonical station, independently of checkpoint output roots.

    Always acquire this fence before station metadata or per-output locks. Read
    observations stay separate; no probe/repair/cleanup may interrupt another.
    """
    root = Path(root).expanduser().absolute()
    _private(root)
    fd = os.open(root / "station-active-operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as stream:
        info = os.fstat(stream.fileno())
        import stat

        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            _fail("active-operation lock must be an owner-only original regular file")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StationOperationBusy(
                "another worker owns the station active operation; no probe or lifecycle effect started"
            ) from None
        yield


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_station(config, *, require_ssh=True, require_current_approval=True):
    """Validate persisted, credential-free station authority without provider calls."""
    if not isinstance(config, dict) or config.get("schema") != SCHEMA or config.get("scenario") != "environment":
        _fail("an explicit benchmark-station.v1 environment configuration is required")
    validate_cloud_config(config, purpose="station", require_ssh=require_ssh)
    if len(config["job_id"]) > 32:
        _fail("job_id must have at most 32 characters")
    placement, approval = config["placement"], config["approval"]
    if placement["boot_gib"] != 20 or "floating_network_id" in placement:
        _fail("station requires exactly one 20GiB boot disk and no floating IP allocation")
    if approval.get("project_id") != config["project_id"] or approval.get("allowed_scenarios") != ["environment"]:
        _fail("approval must bind the exact project and environment scenario")
    _id(approval.get("id"), "approval id")
    if approval.get("allowed_flavor_ids") != [placement["flavor_id"]]:
        _fail("approval must name exactly the requested 4vCPU/8192MiB flavor")
    for key, expected in (("max_vcpus", 4), ("max_ram_mib", 8192)):
        if type(approval.get(key)) is not int or approval[key] != expected:
            _fail("approval " + key + " must be exactly " + str(expected))
    for key, expected in (("max_creates", 1), ("max_live", 1), ("max_boot_gib", 20)):
        if any(type(v.get(key)) is not int or v[key] != expected for v in (approval, config.get("resources", {}))):
            _fail(key + " must declare the single station resource envelope")
    if any(approval.get(key, 0) != 0 for key in ("max_security_groups", "max_floating_ips")):
        _fail("station cannot allocate security groups or floating IPs")
    if not isinstance(approval.get("ledger_root"), str) or not Path(approval["ledger_root"]).is_absolute():
        _fail("absolute private approval ledger_root is required")
    try:
        expiry = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or (require_current_approval and expiry.timestamp() <= time.time()):
            raise ValueError("expired")
    except (KeyError, TypeError, ValueError):
        _fail("approval must have a timezone-aware, unexpired allocation expiry")
    disk = config.get("data_disk")
    if (
        not isinstance(disk, dict)
        or disk != {"size_gib": 500, "volume_type": "ceph_hdd", "mountpoint": _MOUNTPOINT}
        or type(disk["size_gib"]) is not int
    ):
        _fail("data_disk must declare the dedicated 500GiB ceph_hdd disk and fixed mountpoint")
    deadline = config.get("deadline_seconds", 7200)
    if type(deadline) is not int or not 60 <= deadline <= 25200:
        _fail("deadline_seconds must be 60..25200")
    from .benchmark_station_measurements import validate_measurement

    validate_measurement(config.get("measurement"))

    def credentials(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET.search(str(key)):
                    _fail("configuration must not contain literal credentials")
                credentials(item)
        elif isinstance(value, list):
            for item in value:
                credentials(item)

    credentials(config)


# Runs only inside the console-pinned guest, as root through bounded run_guest.
# A format intent is persisted locally before this program is sent. Recovery
# calls use allow_format=False and can only inspect an already completed UUID.
_GUEST_DISK = r"""import json,os,pwd,stat,subprocess,sys
from pathlib import Path
p=json.loads(sys.argv[1])
def run(argv):
 return subprocess.check_output(argv,text=True,timeout=60).strip()
def check(condition,message):
 if not condition: raise RuntimeError(message)
def flatten(nodes):
 for n in nodes:
  yield n
  yield from flatten(n.get('children') or [])
check(os.geteuid()==0,'root preparation required')
boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
root=json.loads(run(['findmnt','--json','--target','/','--output','SOURCE,MAJ:MIN']))['filesystems'][0]
check(root['source'].startswith('/dev/'),'root block source unavailable')
root_tree=json.loads(run(['lsblk','--json','--inverse','--paths','--bytes','--output','NAME,SERIAL,SIZE,TYPE,MAJ:MIN',root['source']]))
root_nodes=list(flatten(root_tree['blockdevices']))
serials=lambda s: {s,s[:20],s.replace('-',''),s.replace('-','')[:20]}
check(any(n.get('serial') in serials(p['boot_volume_id']) for n in root_nodes),'root differs from original boot volume')
all_devices=json.loads(run(['lsblk','--json','--paths','--bytes','--output','NAME,SERIAL,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINTS,MAJ:MIN']))
candidates=[n for n in flatten(all_devices['blockdevices']) if n.get('serial') in serials(p['data_volume_id'])]
check(len(candidates)==1,'data serial is missing or ambiguous')
d=candidates[0]; device=d['name']
check(d['type']=='disk' and int(d['size'])==500*1024**3,'data device must be a whole 500GiB disk')
check(d['maj:min'] not in {n['maj:min'] for n in root_nodes},'data is boot/root device')
check(not d.get('children'),'data disk has partitions or holders')
check(stat.S_ISBLK(os.stat(device).st_mode),'data path is not a block device')
check(not any(n.get('uuid')==p['filesystem_uuid'] and n['maj:min']!=d['maj:min'] for n in flatten(all_devices['blockdevices'])),'filesystem UUID is not unique')
mount=Path(p['data_mountpoint'])
check(all(not x.is_symlink() for x in [mount,*mount.parents]),'unsafe mountpoint symlink')
if p['allow_format']: mount.mkdir(mode=0o700,parents=True,exist_ok=True)
else: check(mount.is_dir(),'original mountpoint missing; inspect only')
mounts=[m for m in (d.get('mountpoints') or []) if m]
check(not mounts or mounts==[str(mount)],'data device is mounted elsewhere')
existing=json.loads(run(['findmnt','--json','--target',str(mount),'--output','SOURCE,FSTYPE,TARGET,UUID,MAJ:MIN,OPTIONS']))['filesystems'][0]
check(existing['target']!=str(mount) or (existing['maj:min']==d['maj:min'] and existing.get('uuid')==p['filesystem_uuid']),'mountpoint is occupied by another filesystem')
fs_type=run(['lsblk','--nodeps','--noheadings','--output','FSTYPE',device])
fs_uuid=run(['lsblk','--nodeps','--noheadings','--output','UUID',device])
if fs_type or fs_uuid:
 check(fs_type=='ext4' and fs_uuid==p['filesystem_uuid'],'unknown existing filesystem; never format')
else:
 check(p['allow_format'],'unfinished format intent requires inspect-only recovery')
 check(not mounts,'blank device is mounted')
 signatures=json.loads(run(['wipefs','--json','--output','TYPE,UUID,OFFSET',device])).get('signatures',[])
 check(not signatures,'unknown disk signatures; never format')
 probe=subprocess.run(['blkid','-p',device],capture_output=True,text=True,timeout=60)
 check(probe.returncode==2 and not probe.stdout.strip(),'disk probe is not unambiguously empty')
 # Reject hidden contents under the mountpoint before masking them.
 check(not any(mount.iterdir()),'mountpoint contains existing files')
 subprocess.run(['mkfs.ext4','-F','-E','nodiscard','-U',p['filesystem_uuid'],device],check=True,timeout=300,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL)
 check(run(['blkid','-s','UUID','-o','value',device])==p['filesystem_uuid'],'formatted UUID differs')
 check(run(['blkid','-s','TYPE','-o','value',device])=='ext4','formatted type differs')
if not p['allow_format']:
 check(existing['target']==str(mount) and existing.get('uuid')==p['filesystem_uuid'],'original filesystem exists but is not mounted; inspect only')
 cache=mount/'.palimpsest-station.json';directory=mount/'measurements'
 metadata={k:p[k] for k in ('data_volume_id','boot_volume_id','filesystem_uuid','data_mountpoint')}
 check(not cache.is_symlink() and cache.stat().st_uid==0 and stat.S_IMODE(cache.stat().st_mode)==0o600 and json.loads(cache.read_text())==metadata,'original station cache metadata differs; inspect only')
 u=pwd.getpwnam(p['user'])
 check(not directory.is_symlink() and directory.is_dir() and directory.stat().st_uid==u.pw_uid and stat.S_IMODE(directory.stat().st_mode)==0o700 and directory.stat().st_dev==os.stat(device).st_rdev,'original measurement directory differs; inspect only')
 print(json.dumps({'boot_id':boot,'data_volume_id':p['data_volume_id'],'boot_volume_id':p['boot_volume_id'],'filesystem_uuid':p['filesystem_uuid'],'data_mountpoint':str(mount),'data_directory':str(mount/'measurements'),'data_size_gib':500,'device':device,'filesystem_binding':existing,'data_serial':d.get('serial')}))
 sys.exit(0)
current=json.loads(run(['findmnt','--json','--target',str(mount),'--output','SOURCE,FSTYPE,TARGET,UUID,MAJ:MIN,OPTIONS']))['filesystems'][0]
if current['target']!=str(mount):
 check(not any(mount.iterdir()),'mountpoint contains existing files')
 subprocess.run(['mount','--types','ext4','--options','nodev,nosuid','UUID='+p['filesystem_uuid'],str(mount)],check=True,timeout=60)
current=json.loads(run(['findmnt','--json','--target',str(mount),'--output','SOURCE,FSTYPE,TARGET,UUID,MAJ:MIN,OPTIONS']))['filesystems'][0]
check(current['target']==str(mount) and current['fstype']=='ext4' and current['uuid']==p['filesystem_uuid'] and current['maj:min']==d['maj:min'],'mounted filesystem identity differs')
check('rw' in current['options'].split(','),'station disk is not writable')
fstab=Path('/etc/fstab')
check(not fstab.is_symlink(),'fstab must not be a symlink')
text=fstab.read_text()
entry='UUID='+p['filesystem_uuid']+' '+str(mount)+' ext4 defaults,nodev,nosuid,nofail 0 2'
conflicts=[line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#') and (str(mount) in line.split()[:2] or 'UUID='+p['filesystem_uuid'] in line.split()[:2])]
check(not conflicts or conflicts==[entry],'fstab entry belongs to another mount')
if not conflicts:
 with fstab.open('a') as f:
  f.write(('' if not text or text.endswith('\n') else '\n')+entry+'\n');f.flush();os.fsync(f.fileno())
os.chown(mount,0,0);os.chmod(mount,0o700)
u=pwd.getpwnam(p['user'])
# Descriptor-based directory walks need read access at this root; the private
# measurement child and root-owned metadata keep their restrictive permissions.
os.chmod(mount,0o755)
directory=mount/'measurements'
cache=mount/'.palimpsest-station.json'
check(not cache.is_symlink(),'station cache metadata is a symlink')
metadata={k:p[k] for k in ('data_volume_id','boot_volume_id','filesystem_uuid','data_mountpoint')}
if cache.exists():
 check(cache.stat().st_uid==0 and stat.S_IMODE(cache.stat().st_mode)==0o600 and json.loads(cache.read_text())==metadata,'station cache ownership differs')
else:
 check(not directory.exists(),'unknown existing measurement directory')
 fd=os.open(cache,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'w') as f:
  json.dump(metadata,f);f.flush();os.fsync(f.fileno())
check(not directory.is_symlink(),'measurement directory is a symlink')
directory.mkdir(mode=0o700,exist_ok=True)
check(directory.stat().st_dev==os.stat(device).st_rdev,'measurement directory is on another device')
os.chown(directory,u.pw_uid,u.pw_gid);os.chmod(directory,0o700)
check(Path('/proc/sys/kernel/random/boot_id').read_text().strip()==boot,'guest rebooted during preparation')
print(json.dumps({'boot_id':boot,'data_volume_id':p['data_volume_id'],'boot_volume_id':p['boot_volume_id'],'filesystem_uuid':p['filesystem_uuid'],'data_mountpoint':str(mount),'data_directory':str(directory),'data_size_gib':500,'device':device,'filesystem_binding':current,'data_serial':d.get('serial')}))
"""

_GUEST_REPAIR_ACCESS = r"""import json,os,pwd,stat,subprocess,sys
from pathlib import Path
p=json.loads(sys.argv[1])
def run(argv):
 return subprocess.check_output(argv,text=True,timeout=60).strip()
def check(condition,message):
 if not condition: raise RuntimeError(message)
def flatten(nodes):
 for n in nodes:
  yield n
  yield from flatten(n.get('children') or [])
check(os.geteuid()==0,'root permission repair required')
boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
check(boot==p['boot_id'],'original guest boot differs')
root=json.loads(run(['findmnt','--json','--target','/','--output','SOURCE,MAJ:MIN']))['filesystems'][0]
check(root['source'].startswith('/dev/'),'root block source unavailable')
root_nodes=list(flatten(json.loads(run(['lsblk','--json','--inverse','--paths','--bytes','--output','NAME,SERIAL,SIZE,TYPE,MAJ:MIN',root['source']]))['blockdevices']))
serials=lambda s: {s,s[:20],s.replace('-',''),s.replace('-','')[:20]}
check(any(n.get('serial') in serials(p['boot_volume_id']) for n in root_nodes),'root differs from original boot volume')
nodes=list(flatten(json.loads(run(['lsblk','--json','--paths','--bytes','--output','NAME,SERIAL,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINTS,MAJ:MIN']))['blockdevices']))
candidates=[n for n in nodes if n.get('serial') in serials(p['data_volume_id'])]
check(len(candidates)==1,'data serial is missing or ambiguous')
d=candidates[0];device=d['name']
check(d['type']=='disk' and int(d['size'])==500*1024**3 and not d.get('children'),'data device must be a whole 500GiB disk')
check(d['maj:min'] not in {n['maj:min'] for n in root_nodes},'data is boot/root device')
check(d.get('fstype')=='ext4' and d.get('uuid')==p['filesystem_uuid'],'original data filesystem differs')
check(not any(n.get('uuid')==p['filesystem_uuid'] and n['maj:min']!=d['maj:min'] for n in nodes),'filesystem UUID is not unique')
device_stat=os.stat(device)
check(stat.S_ISBLK(device_stat.st_mode),'data path is not a block device')
mount=Path(p['data_mountpoint'])
check(str(mount)=='/mnt/palimpsest-benchmark','unexpected mountpoint')
check(all(not x.is_symlink() for x in [mount,*mount.parents]),'unsafe mountpoint symlink')
check([m for m in (d.get('mountpoints') or []) if m]==[str(mount)],'original data disk mount differs')
current=json.loads(run(['findmnt','--json','--target',str(mount),'--output','SOURCE,FSTYPE,TARGET,UUID,MAJ:MIN,OPTIONS']))['filesystems'][0]
check(current['target']==str(mount) and current.get('fstype')=='ext4' and current.get('uuid')==p['filesystem_uuid'] and current['maj:min']==d['maj:min'],'original mounted filesystem differs')
check('rw' in current['options'].split(','),'station disk is not writable')
fd=os.open(mount,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 before=os.fstat(fd)
 check(stat.S_ISDIR(before.st_mode) and before.st_uid==0 and before.st_gid==0 and before.st_dev==device_stat.st_rdev,'mount root ownership or device differs')
 mode=stat.S_IMODE(before.st_mode)
 check(mode in (0o711,0o755),'unexpected mount root permissions')
 entries=set(os.listdir(fd))
 check({'.palimpsest-station.json','measurements'}<=entries and entries<={'.palimpsest-station.json','measurements','lost+found'},'foreign mount root entry')
 cache_fd=os.open('.palimpsest-station.json',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
 with os.fdopen(cache_fd,'r') as cache:
  s=os.fstat(cache.fileno())
  check(stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_gid==0 and s.st_nlink==1 and s.st_dev==before.st_dev and stat.S_IMODE(s.st_mode)==0o600,'original station metadata ownership differs')
  check(json.load(cache)=={k:p[k] for k in ('data_volume_id','boot_volume_id','filesystem_uuid','data_mountpoint')},'original station metadata differs')
 u=pwd.getpwnam(p['user'])
 child_fd=os.open('measurements',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
 try:
  s=os.fstat(child_fd)
  check(stat.S_ISDIR(s.st_mode) and s.st_uid==u.pw_uid and s.st_gid==u.pw_gid and s.st_dev==before.st_dev and stat.S_IMODE(s.st_mode)==0o700,'original measurement directory differs')
 finally: os.close(child_fd)
 if 'lost+found' in entries:
  s=os.stat('lost+found',dir_fd=fd,follow_symlinks=False)
  check(stat.S_ISDIR(s.st_mode) and s.st_uid==0 and s.st_gid==0 and s.st_dev==before.st_dev and stat.S_IMODE(s.st_mode)==0o700,'foreign recovery directory')
 check(Path('/proc/sys/kernel/random/boot_id').read_text().strip()==boot,'guest rebooted during repair')
 check(os.stat(mount,follow_symlinks=False).st_ino==before.st_ino and os.stat(mount,follow_symlinks=False).st_dev==before.st_dev,'mount root changed during repair')
 if mode==0o711:
  os.fchmod(fd,0o755)
  os.fsync(fd)
 after=os.fstat(fd)
 check(stat.S_IMODE(after.st_mode)==0o755 and after.st_uid==0 and after.st_gid==0,'mount root permission repair differs')
finally: os.close(fd)
print(json.dumps({'boot_id':boot,'data_volume_id':p['data_volume_id'],'boot_volume_id':p['boot_volume_id'],'filesystem_uuid':p['filesystem_uuid'],'data_mountpoint':str(mount),'data_directory':str(mount/'measurements'),'data_size_gib':500,'device':device,'filesystem_binding':current,'data_serial':d.get('serial'),'previous_mode':oct(mode),'mode':'0o755','changed':mode==0o711,'metadata_mode':'0o600','measurement_mode':'0o700'}))
"""

_GUEST_UNMOUNT = r"""import json,os,subprocess,sys
p=json.loads(sys.argv[1])
m=json.loads(subprocess.check_output(['findmnt','--json','--target',p['data_mountpoint'],'--output','SOURCE,FSTYPE,TARGET,UUID'],text=True,timeout=30))['filesystems'][0]
if m['target']==p['data_mountpoint']:
 if m.get('uuid')!=p['filesystem_uuid'] or m.get('fstype')!='ext4': raise RuntimeError('cleanup mount ownership differs')
 if p['inspect_only']: raise RuntimeError('unmount intent uncertain and original filesystem remains mounted; no replay')
 os.sync()
 subprocess.run(['umount',p['data_mountpoint']],check=True,timeout=60)
print(json.dumps({'unmounted':True}))
"""


class StationCloud:
    """Create once, reuse by original receipts, clean up only on explicit call.

    ``cloud`` is the existing ScenarioCloud adapter. ``resource()`` returns its
    original SDK receipt with data_volume_id(s), filesystem_uuid,
    data_mountpoint/data_directory and data_size_gib, suitable for measurements.
    """

    def __init__(self, config, output, *, connection=None):
        # Expired creation authority does not prevent inspection or cleanup.
        validate_station(config, require_current_approval=False)
        self.config = copy.deepcopy(config)
        self.output = Path(output).expanduser().absolute()
        self.output.mkdir(mode=0o700, parents=False, exist_ok=True)
        _private(self.output)
        self.path = self.output / "station.json"
        with self._locked():
            config_path = self.output / "station-config.json"
            if config_path.exists() or config_path.is_symlink():
                if _read(config_path) != self.config:
                    _fail("original station configuration changed")
            else:
                _new_json(config_path, self.config)
        self.cloud = ScenarioCloud(config, self.output, self._event, connection=connection, purpose="station")
        self.connection = self.cloud.connection
        flavor = self.connection.compute.get_flavor(config["placement"]["flavor_id"])
        if any(_field(flavor, k) != v for k, v in (("vcpus", 4), ("ram", 8192), ("disk", 0))) or any(
            _field(flavor, k, 0) not in (None, 0, "") for k in ("ephemeral", "swap")
        ):
            _fail("actual approved flavor must be exactly 4vCPU/8192MiB with no local root disk")

    @contextmanager
    def _locked(self):
        _private(self.output)
        fd = os.open(self.output / "station.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _event(self, name, data):
        atomic_write_json(
            self.output / "station-last-event.json", {"event": name, "data": data, "unix_seconds": time.time()}
        )

    def _save(self, value):
        atomic_write_json(self.path, value)

    def _receipt(self):
        value = _read(self.path)
        if value.get("schema") != SCHEMA or value.get("config_sha256") != _digest(self.config):
            _fail("original station receipt/configuration differs")
        if value.get("project_id") != self.config["project_id"] or value.get("job_id") != self.config["job_id"]:
            _fail("original station identity differs")
        return value

    def _original(self, *, allow_unallocated=False):
        original = _read(self.cloud.operations / (_ROLE + ".json"))
        return self.cloud._own(original, allow_unallocated=True) if allow_unallocated else self.cloud._own(original)

    def _create_fences(self):
        return (
            self.cloud.operations / (_ROLE + ".json"),
            self.output / "nova-receipts" / (self.config["job_id"] + "-" + _ROLE + ".json"),
        )

    def _metadata(self, value):
        return {
            "palimpsest_station": self.config["job_id"],
            "palimpsest_station_intent": value["data_intent_id"],
            "palimpsest_project": self.config["project_id"],
            "palimpsest_server": value["server_id"],
            "palimpsest_filesystem_uuid": value["filesystem_uuid"],
            "palimpsest_config_sha256": value["config_sha256"],
        }

    def _volume(self, value, *, attached=None):
        self.cloud._scope()
        volume = self.connection.block_storage.get_volume(value["data_volume_id"])
        if (
            _field(volume, "id") != value["data_volume_id"]
            or _field(volume, "id") == value["boot_volume_id"]
            or _field(volume, "project_id") != self.config["project_id"]
            or _field(volume, "size") != 500
            or _field(volume, "volume_type") != "ceph_hdd"
            or bool(_field(volume, "is_multiattach", _field(volume, "multiattach", False)))
            or any((_field(volume, "metadata", {}) or {}).get(k) != v for k, v in self._metadata(value).items())
        ):
            _fail("original data volume project, metadata, size, type or exclusive ownership differs")
        bindings = _field(volume, "attachments", []) or []
        if any(_field(a, "server_id") != value["server_id"] for a in bindings) or len(bindings) > 1:
            _fail("original data volume has a foreign or nonexclusive attachment")
        if attached is True and (len(bindings) != 1 or _field(volume, "status") != "in-use"):
            _fail("original data volume is not exclusively in use by station")
        if attached is False and (bindings or _field(volume, "status") != "available"):
            _fail("original data volume is not detached and available")
        return volume

    def _retention(self, value):
        response = self.connection.compute.get(
            "/servers/" + value["server_id"] + "/os-volume_attachments", microversion="2.79"
        )
        response.raise_for_status()
        attachments = response.json().get("volumeAttachments")
        if (
            not isinstance(attachments, list)
            or len(attachments) != 2
            or {a.get("volumeId") for a in attachments} != {value["boot_volume_id"], value["data_volume_id"]}
            or any(a.get("delete_on_termination") is not False for a in attachments)
        ):
            _fail("Nova 2.79 exact boot/data retention could not be attested")
        return attachments

    def _wait_volume(self, value, *, attached):
        deadline = time.monotonic() + self.cloud._remaining(300)
        while True:
            volume = self._volume(value)
            bindings = _field(volume, "attachments", []) or []
            if _field(volume, "status") == ("in-use" if attached else "available") and len(bindings) == int(attached):
                return self._volume(value, attached=attached)
            if str(_field(volume, "status", "")).startswith("error") or time.monotonic() >= deadline:
                _fail("data lifecycle did not reach the expected state; retained without replay")
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def _prepare(self, value, *, allow_format):
        original = self._original()
        self.cloud.inspect(original)
        self._volume(value, attached=True)
        self._retention(value)
        parameters = {k: value[k] for k in ("data_volume_id", "boot_volume_id", "filesystem_uuid", "data_mountpoint")}
        parameters.update(user=self.config["ssh"]["user"], allow_format=allow_format)
        response = self.cloud.run_guest(
            original,
            ["sudo", "-n", "python3", "-c", _GUEST_DISK, json.dumps(parameters)],
            label="station-disk-prepare" if allow_format else "station-disk-inspect-" + uuid.uuid4().hex,
            timeout=600,
        )
        if response.get("returncode") != 0 or response.get("timed_out") or response.get("truncated"):
            _fail("guest disk preparation uncertain; inspect only, never repeat formatting")
        try:
            binding = json.loads(response["stdout"])
        except (ValueError, KeyError):
            _fail("guest disk binding was not returned")
        for key in ("data_volume_id", "boot_volume_id", "filesystem_uuid", "data_mountpoint", "data_size_gib"):
            if binding.get(key) != value[key]:
                _fail("guest disk binding differs from original owned data")
        if binding.get("data_directory") != value["data_directory"]:
            _fail("guest measurement directory differs")
        self._volume(value, attached=True)
        self.cloud.inspect(original)
        self._retention(value)
        return binding

    def create(self):
        """Provision once; ready reuse is read-only, any unfinished intent blocks replay."""
        with station_operation(self.output), self._locked():
            if self.path.exists() or self.path.is_symlink():
                value = self._receipt()
                if value["phase"] == "ready":
                    return self._resource(value)
                _fail("station intent exists; use status, never replay uncertain creation")
            validate_station(self.config)
            self.cloud._scope()
            self.cloud._capabilities()
            if any(
                not callable(getattr(self.connection.block_storage, m, None))
                for m in ("create_volume", "get_volume", "delete_volume", "find_volume")
            ):
                _fail("Cinder lacks dedicated data lifecycle capability")
            if any(not callable(getattr(self.connection.compute, m, None)) for m in ("post", "delete")):
                _fail("Nova lacks microversioned data attach/detach capability")
            data_intent_id = str(uuid.uuid4())
            value = {
                "schema": SCHEMA,
                "config_sha256": _digest(self.config),
                "job_id": self.config["job_id"],
                "project_id": self.config["project_id"],
                "phase": "create-station-intent",
                "data_intent_id": data_intent_id,
                "filesystem_uuid": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, "palimpsest-station:" + self.config["project_id"] + ":" + data_intent_id
                    )
                ),
                "data_size_gib": 500,
                "data_mountpoint": _MOUNTPOINT,
                "data_directory": _MOUNTPOINT + "/measurements",
                "created_unix_seconds": time.time(),
                "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scenario_cloud_source_sha256": hashlib.sha256(
                    Path(__file__).with_name("benchmark_scenario_cloud.py").read_bytes()
                ).hexdigest(),
                "guest_prepare_sha256": hashlib.sha256(_GUEST_DISK.encode()).hexdigest(),
            }
            _new_json(self.path, value)
            try:
                resource = self.cloud.create(_ROLE, user_data={"packages": ["e2fsprogs"]})
            except Exception:
                # ScenarioCloud persists the role operation before invoking
                # NovaController, which persists its own intent before POST.
                # Certify only a synchronous failure before BOTH fences; a
                # later missing receipt alone is never proof of no request.
                if not any(path.exists() or path.is_symlink() for path in self._create_fences()):
                    value["phase"] = "aborted-before-request"
                    self._save(value)
                raise
            observed = self.cloud.inspect(resource)
            if len(observed["volumes"]) != 1 or observed["volumes"][0]["size"] != 20:
                _fail("station boot allocation is not exactly its original 20GiB disk")
            actual = observed["flavor"].get("actual") or {}
            if (
                actual.get("vcpus") != 4
                or actual.get("ram_mib", actual.get("ram")) != 8192
                or observed.get("status") != "ACTIVE"
            ):
                _fail("actual station allocation is not ACTIVE 4vCPU/8192MiB; no data allocation started")
            value.update(server_id=resource["server_id"], boot_volume_id=resource["boot_volume_id"])
            self._save(value)
            validate_station(self.config)
            value["phase"] = "create-data-intent"
            self._save(value)
            try:
                self.cloud._remaining(1)
                self.cloud._scope()
                validate_station(self.config)
            except Exception:
                # No Cinder request occurred. Keep the original VM explicitly
                # cleanable, rather than inventing an ambiguous data request.
                value["phase"] = "create-station-intent"
                self._save(value)
                raise
            volume = self.connection.block_storage.create_volume(
                name=self.config["job_id"] + "-data",
                size=500,
                volume_type="ceph_hdd",
                metadata=self._metadata(value),
                is_multiattach=False,
            )
            value.update(data_volume_id=_id(_field(volume, "id"), "original data volume"), phase="data-created")
            self._save(value)
            self._wait_volume(value, attached=False)
            resource.update(
                data_volume_id=value["data_volume_id"],
                data_volume_ids=[value["data_volume_id"]],
                filesystem_uuid=value["filesystem_uuid"],
                data_size_gib=500,
                data_mountpoint=_MOUNTPOINT,
                data_directory=value["data_directory"],
            )
            self.cloud._save(resource)
            value["phase"] = "attach-data-intent"
            self._save(value)
            self.cloud._scope()
            self.cloud._remaining(1)
            self._volume(value, attached=False)
            response = self.connection.compute.post(
                "/servers/" + value["server_id"] + "/os-volume_attachments",
                microversion="2.79",
                json={"volumeAttachment": {"volumeId": value["data_volume_id"], "delete_on_termination": False}},
            )
            response.raise_for_status()
            self._wait_volume(value, attached=True)
            self.cloud.inspect(resource)
            self._retention(value)
            value["phase"] = "format-data-intent"
            self._save(value)
            self.cloud._remaining(1)
            binding = self._prepare(value, allow_format=True)
            value.update(phase="ready", guest_binding=binding)
            self._save(value)
            resource = self._original()
            resource["station_binding"] = binding
            self.cloud._save(resource)
            return self._resource(value)

    def _resource(self, value):
        if value.get("phase") != "ready":
            _fail("station is not ready; uncertain data lifecycle must remain inspection-only")
        original = self._original()
        for key in (
            "server_id",
            "boot_volume_id",
            "data_volume_id",
            "filesystem_uuid",
            "data_mountpoint",
            "data_directory",
            "data_size_gib",
        ):
            if original.get(key) != value.get(key):
                _fail("station binding differs from original SDK receipt")
        if original.get("data_volume_ids") != [value["data_volume_id"]]:
            _fail("original station data-volume identity differs")
        self._volume(value, attached=True)
        observed = self.cloud.inspect(original)
        boot = [v for v in observed["volumes"] if v["id"] == value["boot_volume_id"]]
        requested = observed["flavor"]["requested"]
        actual = observed["flavor"].get("actual") or {}
        ram = actual.get("ram_mib", actual.get("ram"))
        if (
            len(boot) != 1
            or boot[0]["size"] != 20
            or any(requested.get(k) != v for k, v in (("vcpus", 4), ("ram", 8192), ("disk", 0)))
            or actual.get("vcpus") != 4
            or ram != 8192
            or observed.get("status") != "ACTIVE"
        ):
            _fail("original station hardware or boot capacity differs")
        self._retention(value)
        return original

    def resource(self):
        """Return the exact original measurement binding after provider read-only checks."""
        with self._locked():
            return self._resource(self._receipt())

    def repair_access(self) -> dict:
        """Explicitly repair only the original mount root's 0711 permission defect.

        Every call has its own durable intent and result. An already-0755 root
        is inspected without chmod; no allocation, mount or format is replayed.
        Guest stdout and provider exception text never enter failure receipts.
        """
        with station_operation(self.output), self._locked():
            value = self._receipt()
            operation_id = uuid.uuid4().hex
            label = "station-access-repair-" + operation_id
            result_path = self.output / ("access-repair-" + operation_id + ".json")
            identity = {
                k: value[k]
                for k in (
                    "server_id",
                    "project_id",
                    "boot_volume_id",
                    "data_volume_id",
                    "filesystem_uuid",
                    "data_mountpoint",
                    "data_directory",
                    "data_size_gib",
                )
            }
            identity.update(
                user_id=self.config["approval"]["user_id"],
                boot_id=value.get("guest_binding", {}).get("boot_id"),
                flavor={"vcpus": 4, "ram_mib": 8192},
            )
            result = {
                "schema": "palimpsest.station-access-repair.v1",
                "operation_id": operation_id,
                "command_label": label,
                "status": "intent",
                "station": identity,
                "started_unix_seconds": time.time(),
                "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "guest_source_sha256": hashlib.sha256(_GUEST_REPAIR_ACCESS.encode()).hexdigest(),
                "result_path": str(result_path),
            }
            _new_json(self.output / ("access-repair-" + operation_id + ".intent.json"), result)
            stage = "original station validation"
            try:
                original = self._resource(value)
                if not identity["boot_id"] or original.get("boot_id") != identity["boot_id"]:
                    _fail("original station boot binding differs")
                parameters = {
                    k: identity[k]
                    for k in ("boot_id", "data_volume_id", "boot_volume_id", "filesystem_uuid", "data_mountpoint")
                }
                parameters["user"] = self.config["ssh"]["user"]
                stage = "guest permission repair"
                response = self.cloud.run_guest(
                    original,
                    ["sudo", "-n", "python3", "-c", _GUEST_REPAIR_ACCESS, json.dumps(parameters)],
                    label=label,
                    timeout=180,
                )
                if response.get("returncode") != 0 or response.get("timed_out") or response.get("truncated"):
                    _fail("guest permission repair did not return a complete binding")
                binding = json.loads(response["stdout"])
                if (
                    any(
                        binding.get(k) != identity[k]
                        for k in (
                            "boot_id",
                            "data_volume_id",
                            "boot_volume_id",
                            "filesystem_uuid",
                            "data_mountpoint",
                            "data_directory",
                            "data_size_gib",
                        )
                    )
                    or binding.get("mode") != "0o755"
                    or binding.get("previous_mode") not in {"0o711", "0o755"}
                ):
                    _fail("guest permission repair binding differs")
                if (
                    binding.get("changed") is not (binding["previous_mode"] == "0o711")
                    or binding.get("metadata_mode") != "0o600"
                    or binding.get("measurement_mode") != "0o700"
                ):
                    _fail("guest permission repair permissions differ")
                stage = "post-repair original station validation"
                self._resource(value)
                result.update(status="success", guest_binding=binding, changed=binding["changed"])
            except Exception:
                result.update(
                    status="failed", error=stage + " failed; inspect original bindings before another explicit repair"
                )
                result["finished_unix_seconds"] = time.time()
                _new_json(result_path, result)
                raise ArtifactValidationError("benchmark station: " + result["error"]) from None
            result["finished_unix_seconds"] = time.time()
            _new_json(result_path, result)
            return result

    def status(self, *, inspect_guest=False):
        """Read-only observation; optional guest inspection never mounts or formats."""
        with self._locked():
            if not self.path.exists() and not self.path.is_symlink():
                return {"schema": SCHEMA, "phase": "not-created", "retained": False}
            value = self._receipt()
            result = {**value, "retained": value["phase"] != "absent", "automatic_cleanup": False}
            if value["phase"] == "ready":
                result["resource"] = self._resource(value)
            elif value.get("data_volume_id") and value["phase"] not in {
                "delete-data-intent",
                "data-absent",
                "cleanup-guest-intent",
                "absent",
            }:
                volume = self._volume(value)
                result["data_observed"] = {k: _field(volume, k) for k in ("id", "size", "status", "attachments")}
            if value["phase"] == "format-data-intent":
                result["recovery"] = "inspect existing UUID only; no automatic format or allocation replay"
            if inspect_guest and value["phase"] in {"ready", "format-data-intent"}:
                result["guest_observed"] = self._prepare(value, allow_format=False)
            return result

    def cleanup(self):
        """Explicit ordinary detach/delete of original data, then original VM/boot.

        Failed detach/delete intents are settled only through exact GETs. They
        are never reissued. An unknown create ID blocks cleanup rather than
        deleting the VM and abandoning an unaccounted-for disk.
        """
        with station_operation(self.output), self._locked():
            value = self._receipt()
            self.cloud._scope()
            if value["phase"] == "absent":
                if value.get("no_vm_request") and any(
                    path.exists() or path.is_symlink() for path in self._create_fences()
                ):
                    _fail("pre-request absence conflicts with a late creation receipt; retain for inspection")
                if value.get("server_id"):
                    self.cloud.verify_absent(value["server_id"], value.get("boot_volume_id"))
                if (
                    value.get("data_volume_id")
                    and self.connection.block_storage.find_volume(value["data_volume_id"], ignore_missing=True)
                    is not None
                ):
                    _fail("original data volume reappeared after cleanup")
                return value
            if value["phase"] == "aborted-before-request":
                if any(path.exists() or path.is_symlink() for path in self._create_fences()):
                    _fail("pre-request absence conflicts with a late creation receipt; retain for inspection")
                # No provider allocation exists to delete. This station receipt
                # remains terminal: create() never replays it or refunds any
                # cumulative approval budget consumed before the failure.
                value.update(
                    phase="absent",
                    no_vm_request=True,
                    no_data_request=True,
                    server_absent=True,
                    boot_volume_absent=True,
                    data_volume_absent=True,
                )
                self._save(value)
                return value
            if not value.get("data_volume_id"):
                if value["phase"] == "create-station-intent":
                    deleted = self.cloud.delete(self._original(allow_unallocated=True))
                    value.update(
                        phase="absent",
                        server_id=deleted.get("server_id"),
                        boot_volume_id=deleted.get("boot_volume_id"),
                        data_volume_id=None,
                        data_volume_absent=True,
                        no_data_request=True,
                        server_absent=deleted.get("server_absent", True),
                        boot_volume_absent=deleted.get("boot_volume_absent"),
                    )
                    self._save(value)
                    return value
                _fail("station/data allocation uncertain; retain original VM and inspect receipts")
            deadline = time.monotonic() + self.cloud._remaining(300)
            if value["phase"] not in {
                "detach-data-intent",
                "delete-data-intent",
                "data-absent",
                "cleanup-guest-intent",
            }:
                volume = self._volume(value)
                bindings = _field(volume, "attachments", []) or []
                if bindings:
                    original = self._original()
                    self.cloud.inspect(original)
                    self._retention(value)
                    inspect_only = value["phase"] == "unmount-data-intent"
                    value["phase"] = "unmount-data-intent"
                    self._save(value)
                    unmounted = self.cloud.run_guest(
                        original,
                        [
                            "sudo",
                            "-n",
                            "python3",
                            "-c",
                            _GUEST_UNMOUNT,
                            json.dumps(
                                {
                                    "data_mountpoint": value["data_mountpoint"],
                                    "filesystem_uuid": value["filesystem_uuid"],
                                    "inspect_only": inspect_only,
                                }
                            ),
                        ],
                        label="station-disk-unmount-inspect" if inspect_only else "station-disk-unmount",
                        timeout=120,
                    )
                    if unmounted.get("returncode") != 0 or unmounted.get("timed_out"):
                        _fail("ordinary data unmount was not observed; retained without replay")
                    value["phase"] = "detach-data-intent"
                    self._save(value)
                    response = self.connection.compute.delete(
                        "/servers/" + value["server_id"] + "/os-volume_attachments/" + value["data_volume_id"],
                        microversion="2.79",
                    )
                    response.raise_for_status()
                    self._wait_volume(value, attached=False)
                else:
                    self._volume(value, attached=False)
            elif value["phase"] == "detach-data-intent":
                self._volume(value, attached=False)
            if value["phase"] not in {"delete-data-intent", "data-absent", "cleanup-guest-intent"}:
                self._volume(value, attached=False)
                value["phase"] = "delete-data-intent"
                self._save(value)
                self.connection.block_storage.delete_volume(value["data_volume_id"], ignore_missing=False)
                self.cloud._absent(self.connection.block_storage.find_volume, value["data_volume_id"], deadline)
            elif value["phase"] == "delete-data-intent":
                if self.connection.block_storage.find_volume(value["data_volume_id"], ignore_missing=True) is not None:
                    _fail("data delete intent exists; original disk still present; no destructive replay")
            value["phase"] = "data-absent"
            self._save(value)
            original = self._original()
            original["data_volume_ids"] = []
            original["data_volume_absent"] = True
            self.cloud._save(original)
            value["phase"] = "cleanup-guest-intent"
            self._save(value)
            deleted = self.cloud.delete(original)
            value.update(
                phase="absent",
                data_volume_absent=True,
                server_absent=deleted["server_absent"],
                boot_volume_absent=deleted["boot_volume_absent"],
            )
            self._save(value)
            return value
