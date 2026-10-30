"""Fixed two-node k3s whole-VM runtime and nonsecret immutable admission state."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from palimpsest_hub.config import InstanceClusterProfile, Settings
from palimpsest_hub.instance_models import PalimpsestHubInstance
from palimpsest_hub.models import exact_identity

ClusterErrorCode = Literal["cluster_prerequisite_failed", "cluster_bootstrap_failed", "cluster_proof_failed"]
CLUSTER_ERROR_CODES = {"cluster_prerequisite_failed", "cluster_bootstrap_failed", "cluster_proof_failed"}


def qualified_cluster(spec, project, settings):
    identity = spec.get("cluster_profile")
    if identity is None:
        return None
    configured = settings.palimpsest_hub_instance_cluster_profiles.get(identity)
    if configured is None:
        raise HTTPException(422, "Cluster profile is not operator-qualified")
    if not settings.palimpsest_hub_build_worker_key.get_secret_value():
        raise HTTPException(503, "Cluster bootstrap key is unavailable")
    try:
        profile = InstanceClusterProfile.model_validate(
            configured.model_dump() if isinstance(configured, InstanceClusterProfile) else configured
        )
        qualification = settings.palimpsest_hub_instance_base_images[profile.image_id]
        Settings.qualified_base_images({profile.image_id: qualification})
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "Cluster qualification is invalid") from None
    if project not in profile.project_ids:
        raise HTTPException(403, "Cluster profile is not qualified for the original caller project")
    if (
        spec["mode"] != "oci-chroot"
        or spec["image_id"] != profile.image_id
        or spec["flavor_id"] not in profile.flavor_ids
        or spec.get("gpu_profile") is not None
    ):
        raise HTTPException(422, "Cluster profile requires its exact OCI guest image and permitted CPU flavor")
    return {**profile.model_dump(mode="json"), "base_image": dict(qualification)}


def server_address(conn, source, network_id):
    from palimpsest_hub.services.instances import exact_server, value

    server = exact_server(conn, source)
    if server is None or str(value(server, "status", "")).upper() != "ACTIVE":
        raise HTTPException(409, "Cluster server must be provider ACTIVE")
    addresses = set()
    for interface in conn.compute.server_interfaces(value(server, "id")):
        if value(interface, "net_id") != network_id:
            continue
        port = conn.network.get_port(value(interface, "port_id"))
        if (
            port is None
            or value(port, "project_id") != source.project_id
            or value(port, "network_id") != network_id
            or value(port, "device_id") != value(server, "id")
        ):
            raise HTTPException(409, "Cluster server interface ownership changed")
        for fixed in value(port, "fixed_ips", []) or []:
            try:
                ip = ipaddress.ip_address(value(fixed, "ip_address"))
            except ValueError:
                continue
            if (
                ip.version == 4
                and ip.is_private
                and not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified)
            ):
                addresses.add(str(ip))
    if len(addresses) != 1:
        raise HTTPException(409, "Cluster server requires one exact selected-network IPv4 address")
    return addresses.pop()


def require_cluster_source(source, spec, actor, settings, qualification):
    """Bind a live source capability to its immutable operator qualification."""
    from palimpsest_hub.services.instances import aware

    cluster = source.spec_json.get("cluster") or {}
    if (
        source.project_id != actor["project_id"]
        or source.created_by != actor["user_id"]
        or source.status not in ("booting", "ready")
        or not source.capability_hash
        or source.capability_expires_at is None
        or aware(source.capability_expires_at) <= datetime.now(UTC)
        or cluster.get("role") != "server"
        or cluster.get("profile") != spec["cluster_profile"]
        or source.spec_json["network_id"] != spec["network_id"]
        or qualification is None
        or cluster.get("qualification") != qualification
        or qualified_cluster(spec, actor["project_id"], settings) != qualification
        or qualified_cluster(source.spec_json, source.project_id, settings) != qualification
    ):
        raise HTTPException(409, "Cluster source must be the same original user's live qualified server")


async def cluster_snapshot(session, spec, actor, conn, settings):
    qualification = qualified_cluster(spec, actor["project_id"], settings)
    if qualification is None:
        return None
    source_id = spec.get("cluster_server_id")
    address = None
    if source_id:
        from palimpsest_hub.services.instances import owned
        from palimpsest_hub.services.worker_leases import run_blocking

        source = await owned(session, source_id, actor["project_id"])
        require_cluster_source(source, spec, actor, settings, qualification)
        for key in ("layer_digests", "runtime", "provenance", "image_id", "mode"):
            if source.spec_json[key] != spec[key]:
                raise HTTPException(422, "Cluster members require identical ordered layers and runtime provenance")
        rows = (
            await session.scalars(
                select(PalimpsestHubInstance).where(exact_identity(PalimpsestHubInstance.project_id, source.project_id))
            )
        ).all()
        # Retain consumed membership even after deletion: no replacement/replay of this cluster.
        if any((row.spec_json.get("cluster") or {}).get("server_instance_id") == source.id for row in rows):
            raise HTTPException(409, "Cluster server already has its one immutable agent")
        address = await run_blocking(server_address, conn, source, spec["network_id"])
        # A locking read sees committed revocation even under MariaDB REPEATABLE READ.
        await session.refresh(source, with_for_update=True)
        require_cluster_source(source, spec, actor, settings, qualification)
    return {
        "role": "agent" if source_id else "server",
        "profile": spec["cluster_profile"],
        "server_instance_id": source_id,
        "server_address": address,
        "node_name": None,
        "qualification": qualification,
    }


def cluster_join_token(row, settings):
    """Derive only; callers separately authorize issuance or redact sealed failures."""
    key = settings.palimpsest_hub_build_worker_key.get_secret_value()
    if not key:
        raise HTTPException(503, "Cluster bootstrap key is unavailable")
    server_id = row.spec_json["cluster"]["server_instance_id"] or row.id
    return hmac.new(
        key.encode(),
        b"palimpsest:k3s-join:v1\0" + UUID(server_id).bytes + b"\0" + row.project_id.encode(),
        hashlib.sha256,
    ).hexdigest()


async def cluster_capability(session, row, settings):
    from palimpsest_hub.services.instances import aware

    cluster = row.spec_json.get("cluster")
    if not cluster or qualified_cluster(row.spec_json, row.project_id, settings) != cluster["qualification"]:
        raise HTTPException(404, "Cluster capability not found or expired")
    server_id = cluster["server_instance_id"] or row.id
    server = await session.get(PalimpsestHubInstance, server_id)
    if (
        server is None
        or server.created_by != row.created_by
        or server.project_id != row.project_id
        or server.status in ("deleted", "deleting", "stopping", "stopped")
        or not server.capability_hash
        or aware(server.capability_expires_at) <= datetime.now(UTC)
        or qualified_cluster(server.spec_json, server.project_id, settings) != cluster["qualification"]
    ):
        raise HTTPException(404, "Cluster capability not found or expired")
    candidates = (
        await session.scalars(
            select(PalimpsestHubInstance).where(exact_identity(PalimpsestHubInstance.project_id, row.project_id))
        )
    ).all()
    members = [server]
    for candidate in candidates:
        state = candidate.spec_json.get("cluster") or {}
        if (
            state.get("server_instance_id") == server_id
            and candidate.created_by == row.created_by
            and candidate.status not in ("deleted", "deleting", "stopping", "stopped")
        ):
            if qualified_cluster(candidate.spec_json, candidate.project_id, settings) != cluster["qualification"]:
                raise HTTPException(404, "Cluster capability not found or expired")
            members.append(candidate)
    if len(members) > 2 or row.id not in {member.id for member in members}:
        raise HTTPException(404, "Cluster capability not found or expired")
    token = cluster_join_token(row, settings)
    address = next(
        (
            member.spec_json["cluster"]["server_address"]
            for member in members
            if member.spec_json["cluster"]["role"] == "agent"
        ),
        None,
    )
    return {
        "token": token,
        "server_address": address,
        "role": cluster["role"],
        "node_name": cluster["node_name"],
        "members": [
            {
                "instance_id": member.id,
                "node_name": member.spec_json["cluster"]["node_name"],
                "role": member.spec_json["cluster"]["role"],
            }
            for member in members
        ],
    }


def validate_cluster_result(payload, capability):
    """A running agent is not the server's independently attributable proof."""
    if payload["status"] != "ready":
        return
    if capability["role"] == "agent":
        if payload["exit_code"] is not None or payload["stdout"] or payload["stderr"]:
            raise HTTPException(422, "Cluster agent readiness must not impersonate server proof")
        return
    import json

    try:
        proof = json.loads(payload["stdout"])["palimpsest_cluster"]
        members = {item["node_name"] for item in capability["members"]}
        nodes, pods = proof["nodes"], proof["pods"]
        valid = (
            payload["exit_code"] == 0
            and not payload["stderr"]
            and len(members) == 2
            and proof["schema"] == "palimpsest.cluster-result.v1"
            and proof["success"] is True
            and proof["k3s_version"] == "v1.37.1+k3s1"
            and set(proof["checks"]) == {"nodes", "numerical"}
            and all(check is True for check in proof["checks"].values())
            and len(nodes) == len(pods) == 2
            and {node["name"] for node in nodes} == members
            and {pod["node"] for pod in pods} == members
            and len({pod["name"] for pod in pods}) == 2
            and len({node["internal_ip"] for node in nodes}) == 2
            and all(
                ipaddress.IPv4Address(node["internal_ip"]).is_private
                and node["kubelet_version"] == proof["k3s_version"]
                and node["container_runtime"].startswith("containerd://")
                for node in nodes
            )
            and any(
                node["name"]
                == next(member["node_name"] for member in capability["members"] if member["role"] == "server")
                and node["internal_ip"] == capability["server_address"]
                for node in nodes
            )
            and all(
                pod["ready"] is True
                and type(pod["restarts"]) is int
                and pod["restarts"] == 0
                and type(pod["numerical_sum"]) is int
                and pod["numerical_sum"] == 4950
                and pod["image"]
                == "docker.io/rancher/mirrored-pause:3.10.2@sha256:412c4a7219cb8a299a37337f3d87810c5340095322e15594a1637785adad0f17"
                for pod in pods
            )
        )
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration):
        valid = False
    if not valid:
        raise HTTPException(422, "Cluster server requires the exact two-member numerical proof")


# Included only for explicitly privileged cluster profiles; CPU/GPU payloads do not grow.
CLUSTER_RUNTIME = r"""
import ctypes, ipaddress, signal, stat
cluster_process = None
cluster_secret = None

def cluster_private(path, content=None):
    target = merged
    parts = pathlib.PurePosixPath(path).parts[1:]
    for name in parts[:-1]:
        target /= name
        target.mkdir(mode=0o700, exist_ok=True)
        info = target.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('cluster_prerequisite_failed')
    target /= parts[-1]
    if content is None:
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('cluster_prerequisite_failed')
        return target
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise RuntimeError('cluster_prerequisite_failed')
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, 'w') as output:
            fd = None
            output.write(content)
    finally:
        if fd is not None:
            os.close(fd)
    return target

def cluster_enter():
    os.chroot(merged)
    os.chdir('/')
    os.setgroups([])
    os.setgid(0)
    os.setuid(0)

def cluster_fetch():
    request = urllib.request.Request(spec['origin'] + '/v1/instance-bootstrap/' + spec['instance_id'] + '/cluster', headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(8193)
    if len(raw) > 8192:
        raise RuntimeError('cluster_bootstrap_failed')
    data = json.loads(raw)
    if (data['role'] != spec['cluster']['role'] or data['node_name'] != spec['cluster']['node_name']
            or not re.fullmatch('[0-9a-f]{64}', data['token'])):
        raise RuntimeError('cluster_bootstrap_failed')
    return data

def cluster_children():
    with open('/proc/self/task/' + str(os.getpid()) + '/children', 'rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise RuntimeError('cluster_proof_failed')
    return [int(item) for item in raw.split()]

def cluster_reap_children():
    until = time.monotonic() + 5
    while time.monotonic() < until:
        children = cluster_children()
        if not children:
            return
        # Only unreaped direct children can be signalled. This single-threaded
        # parent never releases their numeric identities before these kills.
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in children:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        time.sleep(.01)  # Independent-session orphans are adopted by our subreaper.
    raise RuntimeError('cluster_proof_failed')

def cluster_command(args, data=None):
    import selectors
    if data is not None and len(data) > 65536:
        raise RuntimeError('cluster_proof_failed')
    until = time.monotonic() + min(20, max(0, deadline-time.time()))
    if time.monotonic() >= until:
        raise RuntimeError('cluster_proof_failed')
    command = ['/bin/kubectl', *args[1:]] if args and args[0] == 'kubectl' else ['/bin/k3s', *args]
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE if data else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            env={'PATH':'/bin:/usr/bin:/sbin:/usr/sbin','HOME':'/root'}, preexec_fn=cluster_enter)
    except Exception:
        raise RuntimeError('cluster_proof_failed') from None
    selected = selectors.DefaultSelector()
    output = bytearray()
    offset = 0
    try:
        os.set_blocking(child.stdout.fileno(), False)
        selected.register(child.stdout, selectors.EVENT_READ)
        if data:
            os.set_blocking(child.stdin.fileno(), False)
            selected.register(child.stdin, selectors.EVENT_WRITE)
        while selected.get_map():
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('cluster_proof_failed')
            for key, _ in selected.select(min(.1, remaining)):
                if key.fileobj is child.stdin:
                    offset += os.write(child.stdin.fileno(), data[offset:offset+4096])
                    if offset == len(data):
                        selected.unregister(child.stdin)
                        child.stdin.close()
                else:
                    chunk = os.read(child.stdout.fileno(), min(4096, 65537-len(output)))
                    if not chunk:
                        selected.unregister(child.stdout)
                    else:
                        output.extend(chunk)
                        if len(output) > 65536:
                            raise RuntimeError('cluster_proof_failed')
        # Observe exit without reaping: retain the leader PID until group cleanup.
        while True:
            status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is not None:
                if status.si_code != os.CLD_EXITED or status.si_status:
                    raise RuntimeError('cluster_proof_failed')
                break
            if time.monotonic() >= until:
                raise RuntimeError('cluster_proof_failed')
            time.sleep(.01)
        return output.decode('utf-8', 'strict')
    except Exception:
        raise RuntimeError('cluster_proof_failed') from None
    finally:
        try:
            # The leader is still unreaped, so this group identity cannot be reused.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            # This command-local subreaper owns no long-lived k3s children.
            cluster_reap_children()
        except Exception:
            raise RuntimeError('cluster_proof_failed') from None
        finally:
            selected.close()
            child.stdout.close()
            if child.stdin is not None:
                child.stdin.close()

def cluster_exec(args, data=None):
    import selectors
    # A separate subreaper is essential: the runtime's adopted children may
    # belong to live k3s/containerd, not to this fixed kubectl invocation.
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        raise RuntimeError('cluster_proof_failed') from None
    if pid == 0:
        os.close(read_fd)
        try:
            os.setsid()
            if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
                raise RuntimeError('cluster_proof_failed')
            signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(SystemExit(signum)))
            result = cluster_command(args, data).encode('utf-8')
            while result:
                result = result[os.write(write_fd, result):]
            os._exit(0)
        except BaseException:
            try:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                cluster_reap_children()
            except BaseException:
                os._exit(2)  # Parent must abort the runtime, not retry a command.
            os._exit(1)
    os.close(write_fd)
    selected = selectors.DefaultSelector()
    output = bytearray()
    # The command itself gets at most 20s/the instance deadline. Allow only
    # its bounded teardown afterwards, never an unbounded pipe/child wait.
    until = time.monotonic() + min(20, max(0, deadline-time.time())) + 6
    try:
        os.set_blocking(read_fd, False)
        selected.register(read_fd, selectors.EVENT_READ)
        while selected.get_map():
            remaining = until-time.monotonic()
            if remaining <= 0:
                raise RuntimeError('cluster_proof_failed')
            for key, _ in selected.select(min(.1, remaining)):
                chunk = os.read(read_fd, min(4096, 65537-len(output)))
                if not chunk:
                    selected.unregister(read_fd)
                else:
                    output.extend(chunk)
                    if len(output) > 65536:
                        raise RuntimeError('cluster_proof_failed')
        while time.monotonic() < until:
            status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is not None:
                if status.si_code != os.CLD_EXITED or status.si_status:
                    raise RuntimeError('cluster_proof_failed')
                return output.decode('utf-8', 'strict')
            time.sleep(.01)
        raise RuntimeError('cluster_proof_failed')
    except Exception:
        raise RuntimeError('cluster_proof_failed') from None
    finally:
        selected.close()
        os.close(read_fd)
        # Never reap the supervisor until after signalling its still-owned ID.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        until = time.monotonic() + 6
        while time.monotonic() < until:
            reaped, status = os.waitpid(pid, os.WNOHANG)
            if reaped:
                if os.waitstatus_to_exitcode(status) == 2:
                    raise SystemExit('cluster_proof_failed')
                break
            time.sleep(.01)
        else:
            os.kill(pid, signal.SIGKILL)
            # Runtime teardown will drain adopted descendants; do not continue
            # serving commands when this ownership boundary cannot quiesce.
            raise SystemExit('cluster_proof_failed')

def cluster_kube(args, data=None):
    return cluster_exec(['kubectl', '--kubeconfig=/etc/rancher/k3s/k3s.yaml', '--request-timeout=10s', *args], data)

def cluster_alive():
    if time.time() >= deadline or cluster_process.poll() is not None:
        raise RuntimeError('cluster_bootstrap_failed')

def cluster_probe_manifest(expected, pause):
    manifest = {'apiVersion':'v1','kind':'List','items':[]}
    for index, name in enumerate(sorted(expected)):
        manifest['items'].append({'apiVersion':'v1','kind':'Pod','metadata':{'name':'pal-proof-'+str(index),
            'labels':{'palimpsest-proof':'numerical'}},'spec':{'nodeSelector':{'kubernetes.io/hostname':name},
            'restartPolicy':'Never','affinity':{'podAntiAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':[
                {'labelSelector':{'matchLabels':{'palimpsest-proof':'numerical'}},'topologyKey':'kubernetes.io/hostname'}]}},
            'containers':[{'name':'probe','image':pause,'imagePullPolicy':'Never','command':['/busybox','sh','-ec',
                's=0; i=0; while [ "$i" -lt 100 ]; do s=$((s+i)); i=$((i+1)); done; echo "$s"'],
                'volumeMounts':[{'name':'probe','mountPath':'/busybox','readOnly':True}]}],
            'volumes':[{'name':'probe','hostPath':{'path':'/opt/palimpsest/probe-busybox','type':'File'}}]}})
    return manifest

def cluster_execute():
    global cluster_process, cluster_secret
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('cluster_prerequisite_failed')
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda signum, frame: (_ for _ in ()).throw(SystemExit(signum)))
    cluster_private('/bin/k3s')
    cluster_private('/opt/palimpsest/probe-busybox')
    archive = cluster_private('/var/lib/rancher/k3s/agent/images/k3s-airgap-images-amd64.tar')
    digest = hashlib.sha256()
    count = 0
    with archive.open('rb') as stream:
        while True:
            chunk = stream.read(1024*1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
            if count > 195939840 or time.time() >= deadline:
                raise RuntimeError('cluster_prerequisite_failed')
    if count != 195939840 or digest.hexdigest() != '202d334501e61561354a2d45f4fe36d3a4104a3c95a9d2c9c47587c704864014':
        raise RuntimeError('cluster_prerequisite_failed')
    version = cluster_private('/opt/palimpsest/k3s-version').read_text().strip()
    pause = cluster_private('/opt/palimpsest/pause-image').read_text().strip()
    if version != 'v1.37.1+k3s1' or pause != 'docker.io/rancher/mirrored-pause:3.10.2@sha256:412c4a7219cb8a299a37337f3d87810c5340095322e15594a1637785adad0f17':
        raise RuntimeError('cluster_prerequisite_failed')
    data = cluster_fetch()
    cluster_secret = cluster_private('/run/palimpsest-cluster/token', data['token'])
    config = {'node-name':data['node_name'], 'token-file':'/run/palimpsest-cluster/token',
              'snapshotter':'native', 'disable-default-registry-endpoint':True,
              'kubelet-arg':['cgroup-driver=cgroupfs'], 'pause-image':pause}
    if cluster_exec(['--version']).splitlines()[0].split()[2] != version:
        raise RuntimeError('cluster_prerequisite_failed')
    # Empty endpoint configuration plus the per-pod Never policy prevents external registry fallback.
    cluster_private('/etc/rancher/k3s/registries.yaml', json.dumps({'mirrors':{'*':{'endpoint':['http://127.0.0.1:1']}}}))
    if data['role'] == 'server':
        config.update({'disable':['traefik','servicelb','metrics-server','local-storage'],
                       'write-kubeconfig-mode':'0600', 'flannel-backend':'vxlan'})
    else:
        if data['server_address'] != spec['cluster']['server_address']:
            raise RuntimeError('cluster_bootstrap_failed')
        config['server'] = 'https://' + str(ipaddress.IPv4Address(data['server_address'])) + ':6443'
    cluster_private('/run/palimpsest-cluster/config.yaml', json.dumps(config))
    cluster_process = subprocess.Popen(['/bin/k3s', data['role'], '--config', '/run/palimpsest-cluster/config.yaml'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={'PATH':'/bin:/usr/bin:/sbin:/usr/sbin','HOME':'/root'}, preexec_fn=cluster_enter, start_new_session=True)
    if data['role'] == 'agent':
        while not (merged / 'var/lib/rancher/k3s/agent/kubelet.kubeconfig').is_file():
            cluster_alive()
            time.sleep(1)
        report('ready')
    else:
        expected = None
        nodes = None
        while time.time() < deadline:
            cluster_alive()
            data = cluster_fetch()
            names = {member['node_name'] for member in data['members']}
            if len(data['members']) == 2 and len(names) == 2:
                try:
                    observed = json.loads(cluster_kube(['get','nodes','-o','json']))['items']
                    if {item['metadata']['name'] for item in observed} == names and all(
                        any(condition['type'] == 'Ready' and condition['status'] == 'True'
                            for condition in item['status'].get('conditions', [])) for item in observed):
                        expected, nodes = names, observed
                        break
                except (RuntimeError, subprocess.TimeoutExpired):
                    pass
            time.sleep(2)
        if nodes is None:
            raise RuntimeError('cluster_proof_failed')
        sealed_nodes = []
        for item in nodes:
            info = item['status']['nodeInfo']
            addresses = [entry['address'] for entry in item['status']['addresses'] if entry['type'] == 'InternalIP']
            if (len(addresses) != 1 or not ipaddress.IPv4Address(addresses[0]).is_private
                    or info['kubeletVersion'] != version or not info['containerRuntimeVersion'].startswith('containerd://')):
                raise RuntimeError('cluster_proof_failed')
            sealed_nodes.append({'name':item['metadata']['name'], 'internal_ip':addresses[0],
                'kubelet_version':info['kubeletVersion'], 'container_runtime':info['containerRuntimeVersion']})
        if len({item['internal_ip'] for item in sealed_nodes}) != 2:
            raise RuntimeError('cluster_proof_failed')
        server_name = next(member['node_name'] for member in data['members'] if member['role'] == 'server')
        if not any(node['name'] == server_name and node['internal_ip'] == data['server_address'] for node in sealed_nodes):
            raise RuntimeError('cluster_proof_failed')
        manifest = cluster_probe_manifest(expected, pause)
        cluster_kube(['create','-f','-'], json.dumps(manifest).encode())
        sealed_pods = None
        while time.time() < deadline:
            cluster_alive()
            pods = json.loads(cluster_kube(['get','pods','-o','json']))['items']
            if len(pods) != 2 or {pod['metadata']['name'] for pod in pods} != {'pal-proof-0','pal-proof-1'}:
                raise RuntimeError('cluster_proof_failed')
            if all(pod['status'].get('phase') == 'Succeeded' for pod in pods):
                sealed_pods = []
                for pod in pods:
                    statuses = pod['status'].get('containerStatuses', [])
                    if (len(statuses) != 1 or statuses[0]['restartCount'] != 0
                            or statuses[0].get('state',{}).get('terminated',{}).get('exitCode') != 0
                            or cluster_kube(['logs',pod['metadata']['name']]).strip() != '4950'):
                        raise RuntimeError('cluster_proof_failed')
                    sealed_pods.append({'node':pod['spec']['nodeName'],'name':pod['metadata']['name'],
                        'image':pause,'ready':True,'restarts':0,'numerical_sum':4950})
                break
            if any(pod['status'].get('phase') == 'Failed' for pod in pods):
                raise RuntimeError('cluster_proof_failed')
            time.sleep(1)
        if sealed_pods is None or {pod['node'] for pod in sealed_pods} != expected:
            raise RuntimeError('cluster_proof_failed')
        # No unexpected container restart anywhere in this newly created cluster.
        for pod in json.loads(cluster_kube(['get','pods','--all-namespaces','-o','json']))['items']:
            if pod['metadata']['namespace'] not in ('default', 'kube-system'):
                raise RuntimeError('cluster_proof_failed')
            statuses = pod['status'].get('containerStatuses', []) + pod['status'].get('initContainerStatuses', [])
            if any(status.get('restartCount') != 0 for status in statuses):
                raise RuntimeError('cluster_proof_failed')
        cluster_alive()
        output = json.dumps({'palimpsest_cluster':{'schema':'palimpsest.cluster-result.v1','success':True,
            'nodes':sealed_nodes,'pods':sealed_pods,'checks':{'nodes':True,'numerical':True},'k3s_version':version}}, separators=(',',':'))
        if len(output.encode()) > 8192:
            raise RuntimeError('cluster_proof_failed')
        (ROOT / 'stdout').write_text(output)
        report('ready')
    while cluster_process.poll() is None:
        time.sleep(1)

def cluster_cleanup():
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, signal.SIG_IGN)
    if cluster_secret is not None:
        cluster_secret.unlink(missing_ok=True)
    # Never collect grandchild numeric PIDs: an exiting grandchild may be reaped
    # and reused before we signal it. Kill unreaped direct children, then drain
    # newly adopted children until the subreaper is demonstrably quiescent.
    cluster_reap_children()
    # Container runtime mounts are below our private merged root in this mount namespace.
    if (ROOT / 'merged').exists():
        result = subprocess.run(['umount','--recursive',str(ROOT / 'merged')], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode:
            raise RuntimeError('cluster_proof_failed')
"""
