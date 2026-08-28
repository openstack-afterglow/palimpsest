"""Bridge strict project models to the existing KVM and Lima runtimes."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import kvm, lima, runtime_dispatch, state
from .digest import digest_file, require_file_digest
from .errors import ArtifactValidationError, LifecycleError, StateError
from .project import Project, ServiceSpec, resolve_cloud_init, resolve_service_environment
from .project_runtime import (
    DownTarget,
    ManagedVolume,
    PreparedService,
    ProjectCallbacks,
    ProjectPrepareError,
    PublishedPort,
    read_project_state,
    service_config_digest,
)
from .project_volumes import (
    delete_kvm_volume,
    delete_lima_volume,
    ensure_kvm_volume,
    ensure_lima_volume,
    kvm_volume_path,
    lima_backend_name,
    preflight_delete_lima_volume,
    preflight_kvm_volume_support,
    verify_kvm_volume,
    verify_lima_volume,
)
from .refs import PortForward, RunSpec, StackRef, VolumeAttachment
from .runtime_types import ExpectedRunIdentity, ResolvedRunRequest, RunVolumeIntent

_MIB = 1024 * 1024


@dataclass(frozen=True)
class ResolvedProjectService:
    request: ResolvedRunRequest = field(repr=False)

    @property
    def stack(self) -> StackRef:
        return self.request.spec.stack

    @property
    def backend(self) -> str:
        return self.request.dispatch_key.backend.value

    @property
    def network(self) -> str:
        return self.request.spec.network

    @property
    def environment(self) -> tuple[tuple[str, str], ...]:
        return self.request.spec.environment

    @property
    def cloud_init(self) -> object | None:
        return self.request.spec.cloud_init


@dataclass(frozen=True)
class _ResolvedExecutionInputs:
    digest: str
    stack: StackRef = field(repr=False)
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    cloud_init: object | None = field(repr=False)


StackResolver = Callable[[ServiceSpec], StackRef]


def _network_for_service(project: Project, service: ServiceSpec, backend: str) -> str:
    if len(service.networks) != 1:
        raise ArtifactValidationError("project services currently support exactly one network")
    logical_name = service.networks[0]
    network = project.networks[logical_name]
    if logical_name == "default" and not network.external and network.driver == "nat":
        return "default"
    if not network.external:
        if logical_name == "default":
            raise ArtifactValidationError(
                f"managed default network requires driver 'nat'; driver {network.driver!r} is not implemented"
            )
        raise ArtifactValidationError(
            f"managed project network {logical_name!r} is not implemented yet; use default or external: true"
        )
    external_name = network.external_name or logical_name
    if backend == "lima-vz":
        if external_name == "vzNAT":
            return "vzNAT"
        return f"lima:{external_name}"
    return external_name


def _port_is_available(port: PublishedPort, replacing_run: str | None) -> bool:
    if replacing_run is not None:
        return True
    address = ipaddress.ip_address(port.host_ip)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    kind = socket.SOCK_STREAM if port.protocol == "tcp" else socket.SOCK_DGRAM
    candidate = socket.socket(family, kind)
    try:
        candidate.bind((port.host_ip, port.host_port))
    except OSError:
        return False
    finally:
        candidate.close()
    return True


def _inspect_run(name: str, roots: state.StatePaths) -> object | None:
    if not state.run_entry_present_or_ambiguous(roots, name):
        if lima.available():
            foreign_status = lima.inspect_instance_status(name)
            if foreign_status is not None:
                return {"status": foreign_status}
        return None
    # Any partial or malformed local ledger is an ownership ambiguity, never
    # evidence that the run name is free.
    return runtime_dispatch.inspect_run(name, roots=roots)


def _volume_use_counts(project: Project) -> Counter[str]:
    return Counter(mount.source for service in project.services.values() for mount in service.volumes)


def _applied_reservations(
    record: Mapping[str, object],
    *,
    service: str,
    run_name: str,
    backend: str,
) -> tuple[tuple[str, ...], tuple[PublishedPort, ...]]:
    raw_volumes = record.get("volumes", [])
    if not isinstance(raw_volumes, list):
        raise StateError(f"managed service {service!r} has malformed applied volume metadata")
    expected_volume_keys = (
        {"name", "host_path", "serial", "target_dev", "mount_path", "filesystem", "read_only"}
        if backend in {"kvm", "libvirt-hvf"}
        else {"name", "backend_name", "mount_path", "filesystem", "read_only"}
    )
    attached: list[str] = []
    seen_volumes: set[str] = set()
    for raw in raw_volumes:
        if not isinstance(raw, Mapping) or set(raw) != expected_volume_keys:
            raise StateError(f"managed service {service!r} has malformed applied volume metadata")
        name = raw.get("name")
        read_only = raw.get("read_only")
        if not isinstance(name, str) or not name or name in seen_volumes or not isinstance(read_only, bool):
            raise StateError(f"managed service {service!r} has invalid applied volume metadata")
        seen_volumes.add(name)
        attached.append(name)

    raw_ports = record.get("ports", [])
    if not isinstance(raw_ports, list):
        raise StateError(f"managed service {service!r} has malformed applied port metadata")
    ports: list[PublishedPort] = []
    seen_ports: set[tuple[str, int, str]] = set()
    for raw in raw_ports:
        if not isinstance(raw, Mapping) or set(raw) != {"host_ip", "host_port", "guest_port", "protocol"}:
            raise StateError(f"managed service {service!r} has malformed applied port metadata")
        host_ip = raw.get("host_ip")
        host_port = raw.get("host_port")
        guest_port = raw.get("guest_port")
        protocol = raw.get("protocol")
        if not isinstance(host_ip, str):
            raise StateError(f"managed service {service!r} has invalid applied port metadata")
        try:
            canonical_ip = str(ipaddress.ip_address(host_ip))
        except ValueError as exc:
            raise StateError(f"managed service {service!r} has invalid applied port metadata") from exc
        if (
            canonical_ip != host_ip
            or isinstance(host_port, bool)
            or not isinstance(host_port, int)
            or not 1 <= host_port <= 65535
            or isinstance(guest_port, bool)
            or not isinstance(guest_port, int)
            or not 1 <= guest_port <= 65535
            or protocol not in {"tcp", "udp"}
        ):
            raise StateError(f"managed service {service!r} has invalid applied port metadata")
        key = (host_ip, host_port, protocol)
        if key in seen_ports:
            raise StateError(f"managed service {service!r} has duplicate applied port metadata")
        seen_ports.add(key)
        ports.append(PublishedPort(service, run_name, host_ip, host_port, guest_port, protocol))
    return tuple(attached), tuple(ports)


def _ports_conflict(one: PublishedPort, two: PublishedPort) -> bool:
    if one.host_port != two.host_port or one.protocol != two.protocol:
        return False
    first = ipaddress.ip_address(one.host_ip)
    second = ipaddress.ip_address(two.host_ip)
    if first.version != second.version:
        return False
    return first == second or first.is_unspecified or second.is_unspecified


def _validate_kvm_volume_references(path: Path, allowed_run_ids: Mapping[str, str]) -> None:
    """Reject any libvirt attachment not bound to an exact teardown identity."""

    connection = kvm.connect("qemu:///system")
    try:
        try:
            domains = connection.listAllDomains(0)
        except Exception as exc:
            raise StateError("cannot enumerate libvirt domains before KVM volume deletion") from exc
        if not isinstance(domains, (list, tuple)):
            raise StateError("libvirt returned an invalid domain list during KVM volume deletion preflight")
        expected_path = path.resolve(strict=False)
        for domain in domains:
            try:
                xml = domain.XMLDesc(0)
                root = ET.fromstring(xml)
            except Exception as exc:
                raise StateError("cannot inspect libvirt domain disks before KVM volume deletion") from exc
            references = False
            for source in root.findall("./devices/disk/source"):
                source_path = source.get("file")
                if isinstance(source_path, str) and Path(source_path).resolve(strict=False) == expected_path:
                    references = True
                    break
            if not references:
                continue
            name = root.findtext("name")
            expected_run_id = allowed_run_ids.get(name) if isinstance(name, str) else None
            actual_run_id = kvm.get_domain_run_id(domain)
            if expected_run_id is None or actual_run_id != expected_run_id:
                raise StateError(
                    f"KVM volume {path.name!r} is referenced by foreign or unexpected libvirt domain {name!r}"
                )
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _cloud_init_payload(cloud_init: object | None) -> object:
    if cloud_init is None:
        return None
    return {
        "packages": list(getattr(cloud_init, "packages", ())),
        "write_files": [
            {
                "path": getattr(item, "path", None),
                "content": getattr(item, "content", None),
                "permissions": getattr(item, "permissions", None),
            }
            for item in getattr(cloud_init, "write_files", ())
        ],
        "runcmd": [list(command) for command in getattr(cloud_init, "runcmd", ())],
    }


def _env_file_digests(service: ServiceSpec) -> tuple[tuple[str, str], ...]:
    try:
        return tuple((project_file.reference, digest_file(project_file.path)) for project_file in service.env_files)
    except OSError as exc:
        raise StateError(f"cannot fingerprint env_file for service {service.name!r}") from exc


def _cloud_init_source_digest(service: ServiceSpec) -> tuple[str, str] | None:
    source = service.cloud_init.source if service.cloud_init is not None else None
    if source is None:
        return None
    try:
        actual = digest_file(source.path)
    except OSError as exc:
        raise StateError(f"cannot fingerprint cloud_init file for service {service.name!r}") from exc
    if source.content_sha256 is not None and actual != source.content_sha256:
        raise StateError(f"service {service.name!r} cloud_init file changed after project validation")
    return source.reference, actual


def build_project_callbacks(
    project: Project,
    roots: state.StatePaths,
    stack_resolver: StackResolver,
    *,
    environment: Mapping[str, str] | None = None,
) -> ProjectCallbacks:
    """Create lifecycle callbacks without retaining resolved secrets in project state."""

    active_environment = dict(os.environ if environment is None else environment)
    volume_uses = _volume_use_counts(project)
    shared = sorted(name for name, count in volume_uses.items() if count > 1)
    if shared:
        raise ArtifactValidationError("writable block volumes can attach to only one service: " + ", ".join(shared))

    execution_inputs: dict[str, _ResolvedExecutionInputs] = {}
    attachment_cache: dict[tuple[str, str], VolumeAttachment] = {}
    preexisting_volume_keys: set[tuple[str, str]] = set()
    preflighted_requests: dict[str, ResolvedRunRequest] = {}
    prepare_complete = False

    def resolved_inputs(_project: Project, service: ServiceSpec) -> _ResolvedExecutionInputs:
        cached = execution_inputs.get(service.name)
        if cached is not None:
            return cached
        stack = stack_resolver(service)
        before = _env_file_digests(service)
        cloud_source_before = _cloud_init_source_digest(service)
        service_environment = resolve_service_environment(service, active_environment)
        cloud_environment = {**active_environment, **service_environment}
        cloud = resolve_cloud_init(service.cloud_init, cloud_environment) if service.cloud_init is not None else None
        after = _env_file_digests(service)
        cloud_source_after = _cloud_init_source_digest(service)
        if before != after:
            raise StateError(f"service {service.name!r} env_file changed while project inputs were resolved")
        if cloud_source_before != cloud_source_after:
            raise StateError(f"service {service.name!r} cloud_init file changed while project inputs were resolved")
        environment_items = tuple(sorted(service_environment.items()))
        fingerprint_payload = {
            "structural_digest": service_config_digest(_project, service.name),
            "stack": {
                "base": {
                    "digest": stack.base.digest,
                    "disk_format": stack.base.disk_format,
                    "arch": stack.base.arch,
                },
                "layers": [layer.digest for layer in stack.layers],
            },
            "env_files": [{"reference": reference, "digest": digest} for reference, digest in after],
            "environment": [[name, value] for name, value in environment_items],
            "cloud_init": _cloud_init_payload(cloud),
            "cloud_init_source": (
                {"reference": cloud_source_after[0], "digest": cloud_source_after[1]}
                if cloud_source_after is not None
                else None
            ),
        }
        encoded = json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        result = _ResolvedExecutionInputs(
            digest="sha256:" + hashlib.sha256(encoded).hexdigest(),
            stack=stack,
            environment=environment_items,
            cloud_init=cloud,
        )
        execution_inputs[service.name] = result
        return result

    def desired_digest(_project: Project, service: ServiceSpec) -> str:
        return resolved_inputs(_project, service).digest

    def resolve(_project: Project, service: ServiceSpec, run_name: str) -> ResolvedProjectService:
        inputs = resolved_inputs(_project, service)
        ports = tuple(
            PortForward(port.host_ip, port.host_port, port.guest_port, port.protocol) for port in service.ports
        )
        provisional_spec = RunSpec(
            name=run_name,
            stack=inputs.stack,
            memory_mib=service.memory_mib,
            vcpus=service.vcpus,
            ports=ports,
            environment=inputs.environment,
            cloud_init=inputs.cloud_init,
        )
        provisional = runtime_dispatch.resolve_run_request(provisional_spec)
        network = _network_for_service(project, service, provisional.dispatch_key.backend.value)
        logical_spec = RunSpec(
            name=run_name,
            stack=inputs.stack,
            memory_mib=service.memory_mib,
            vcpus=service.vcpus,
            network=network,
            ports=ports,
            environment=inputs.environment,
            cloud_init=inputs.cloud_init,
        )
        request = runtime_dispatch.resolve_run_request(
            logical_spec,
            requested_backend=provisional.dispatch_key.backend.value,
            require_volume_binding=bool(service.volumes),
            volume_intents=tuple(
                RunVolumeIntent(
                    project.volumes[mount.source].name,
                    mount.target,
                    "ext4",
                    mount.read_only,
                )
                for mount in service.volumes
            ),
        )
        if request.dispatch_key != provisional.dispatch_key:
            raise StateError("project runtime backend changed while the request was resolved")
        return ResolvedProjectService(request)

    def _allowed_lima_instance(item: PreparedService) -> str | None:
        if item.plan.action == "start":
            return item.plan.run_name
        if item.plan.action == "recreate" and item.plan.previous_status in {
            "defined",
            "running",
            "stopped",
            "failed",
        }:
            return item.plan.run_name
        return None

    def _verify_stack(item: PreparedService, resolved: ResolvedProjectService) -> None:
        try:
            require_file_digest(resolved.stack.base.local_path, resolved.stack.base.digest)
            for layer in resolved.stack.layers:
                require_file_digest(layer.local_path, layer.digest)
        except Exception as exc:
            raise ArtifactValidationError(
                f"service {item.service.name!r} runtime input digest verification failed: {exc}"
            ) from exc

    def _reject_lima_owned_paths(item: PreparedService, resolved: ResolvedProjectService) -> None:
        if resolved.backend != "lima-vz":
            return
        protected = "/mnt/lima-"
        for mount in item.service.volumes:
            if mount.read_only:
                raise ArtifactValidationError(
                    f"service {item.service.name!r} requests a read-only Lima block volume, which is not "
                    "enforceable at the underlying source filesystem in v1"
                )
            if mount.target.startswith(protected):
                raise ArtifactValidationError(
                    f"service {item.service.name!r} volume target {mount.target!r} overlaps Lima-owned disk paths"
                )
        for write_file in getattr(resolved.cloud_init, "write_files", ()):
            path = getattr(write_file, "path", "")
            if isinstance(path, str) and path.startswith(protected):
                raise ArtifactValidationError(
                    f"service {item.service.name!r} cloud-init path {path!r} overlaps Lima-owned disk paths"
                )

    def _reject_guest_path_overlaps(item: PreparedService, resolved: ResolvedProjectService) -> None:
        targets = sorted(mount.target.rstrip("/") for mount in item.service.volumes)
        for index, target in enumerate(targets):
            for other in targets[index + 1 :]:
                if other == target or other.startswith(target + "/"):
                    raise ArtifactValidationError(
                        f"service {item.service.name!r} volume targets {target!r} and {other!r} overlap"
                    )
        for write_file in getattr(resolved.cloud_init, "write_files", ()):
            path = getattr(write_file, "path", None)
            if not isinstance(path, str):
                continue
            normalized = path.rstrip("/")
            for target in targets:
                if normalized == target or normalized.startswith(target + "/") or target.startswith(normalized + "/"):
                    raise ArtifactValidationError(
                        f"service {item.service.name!r} cloud-init path {path!r} overlaps volume target {target!r}"
                    )

    def _validate_resolved_run_spec(item: PreparedService, resolved: ResolvedProjectService) -> None:
        ports = tuple(
            PortForward(port.host_ip, port.host_port, port.guest_port, port.protocol) for port in item.service.ports
        )
        lima_volumes: tuple[VolumeAttachment, ...] = ()
        if resolved.backend == "lima-vz":
            lima_volumes = tuple(
                VolumeAttachment(
                    mount.source,
                    mount.target,
                    backend_name=lima_backend_name(project.name, mount.source),
                    read_only=mount.read_only,
                    format=False,
                )
                for mount in item.service.volumes
                if not project.volumes[mount.source].external
            )
        spec = RunSpec(
            name=item.plan.run_name,
            stack=resolved.stack,
            memory_mib=item.service.memory_mib,
            vcpus=item.service.vcpus,
            network=resolved.network,
            ports=ports,
            volumes=lima_volumes,
            environment=resolved.environment,
            cloud_init=resolved.cloud_init,
        )
        if resolved.backend == "lima-vz":
            lima.validate_run_spec(spec)
        elif len(resolved.stack.layers) + len(item.service.volumes) > kvm.MAX_LAYER_DISKS:
            raise ArtifactValidationError(f"combined layer and volume count exceeds limit {kvm.MAX_LAYER_DISKS}")

    def _preflight_preserved_service(
        item: PreparedService,
        ledger_volumes: Mapping[str, ManagedVolume],
        checked_networks: set[tuple[str, str]],
    ) -> None:
        rpaths = state.run_paths(roots, item.plan.run_name)
        record = state.read_run_state(rpaths)
        backend = record.get("backend", "kvm")
        if backend not in {"kvm", "lima-vz", "libvirt-hvf"}:
            raise StateError(f"owned run has unsupported backend {backend!r}")
        network = record.get("network")
        if not isinstance(network, str) or not network:
            raise StateError("owned run is missing its preserved network binding")
        network_key = (backend, network)
        if network_key not in checked_networks:
            if backend in {"kvm", "libvirt-hvf"}:
                kvm.validate_network(network)
            else:
                lima.validate_network(network)
            checked_networks.add(network_key)

        base = record.get("base")
        layers = record.get("layers")
        if not isinstance(base, Mapping) or not isinstance(layers, list):
            raise StateError("owned run is missing preserved stack metadata")
        base_path = base.get("local_path")
        base_digest = base.get("digest")
        if not isinstance(base_path, str) or not isinstance(base_digest, str):
            raise StateError("owned run has malformed preserved base metadata")
        require_file_digest(Path(base_path), base_digest)
        for layer in layers:
            if not isinstance(layer, Mapping):
                raise StateError("owned run has malformed preserved layer metadata")
            layer_path = layer.get("local_path")
            layer_digest = layer.get("digest")
            if not isinstance(layer_path, str) or not isinstance(layer_digest, str):
                raise StateError("owned run has malformed preserved layer metadata")
            require_file_digest(Path(layer_path), layer_digest)

        raw_volumes = record.get("volumes", [])
        if not isinstance(raw_volumes, list):
            raise StateError("owned run has malformed preserved volume metadata")
        seen: set[str] = set()
        for raw_volume in raw_volumes:
            if not isinstance(raw_volume, Mapping):
                raise StateError("owned run has malformed preserved volume metadata")
            name = raw_volume.get("name")
            if not isinstance(name, str) or name in seen:
                raise StateError("owned run has invalid or duplicate preserved volume metadata")
            seen.add(name)
            managed = ledger_volumes.get(name)
            if managed is None or managed.backend != backend:
                raise StateError(f"preserved volume {name!r} has no matching project ownership record")
            if backend in {"kvm", "libvirt-hvf"}:
                verified = verify_kvm_volume(roots, project.name, name, managed.size_bytes)
                if raw_volume.get("host_path") != str(verified.path):
                    raise StateError(f"preserved KVM volume path changed for {name!r}")
            else:
                if raw_volume.get("read_only") is True:
                    raise StateError(f"preserved Lima read-only volume {name!r} is not safely enforceable")
                verified_lima = verify_lima_volume(
                    roots,
                    project.name,
                    name,
                    managed.size_bytes,
                    allowed_instance=item.plan.run_name,
                )
                if verified_lima is None or raw_volume.get("backend_name") != verified_lima.backend_name:
                    raise StateError(f"preserved Lima volume binding changed for {name!r}")

    def _validate_global_reservations(
        prepared: tuple[PreparedService, ...],
        ledger_volumes: Mapping[str, ManagedVolume],
        ledger_services: Mapping[str, object],
    ) -> None:
        volume_claims: dict[str, str] = {}
        port_claims: list[PublishedPort] = []

        def reserve_volume(name: str, service_name: str) -> None:
            owner = volume_claims.get(name)
            if owner is not None and owner != service_name:
                raise ArtifactValidationError(
                    f"managed block volume {name!r} is already reserved by applied service {owner!r}; "
                    f"service {service_name!r} cannot claim it"
                )
            volume_claims[name] = service_name

        def reserve_port(port: PublishedPort) -> None:
            for existing in port_claims:
                if existing.service != port.service and _ports_conflict(existing, port):
                    raise ArtifactValidationError(
                        f"host port {port.host_ip}:{port.host_port}/{port.protocol} requested by service "
                        f"{port.service!r} conflicts with applied service {existing.service!r}"
                    )
            port_claims.append(port)

        for service_name, raw_managed in ledger_services.items():
            run_name = getattr(raw_managed, "run_name", None)
            expected_run_id = getattr(raw_managed, "run_id", None)
            expected_backend = getattr(raw_managed, "backend", None)
            if not all(isinstance(value, str) for value in (run_name, expected_run_id, expected_backend)):
                raise StateError(f"project ledger contains an invalid service reservation for {service_name!r}")
            rpaths = state.run_paths(roots, run_name)
            owner = state.read_owner_record(rpaths)
            record = state.read_run_state(rpaths)
            backend = record.get("backend", "kvm")
            if owner.run_id != expected_run_id or backend != expected_backend:
                raise StateError(f"managed service {service_name!r} identity changed while reserving applied resources")
            applied_volumes, applied_ports = _applied_reservations(
                record,
                service=service_name,
                run_name=run_name,
                backend=expected_backend,
            )
            for volume_name in applied_volumes:
                managed_volume = ledger_volumes.get(volume_name)
                if managed_volume is None or managed_volume.backend != expected_backend:
                    raise StateError(
                        f"applied volume {volume_name!r} for service {service_name!r} has no matching project ledger"
                    )
                reserve_volume(volume_name, service_name)
            for port in applied_ports:
                reserve_port(port)

        for item in prepared:
            if item.plan.preserve_config:
                continue
            for mount in item.service.volumes:
                volume = project.volumes[mount.source]
                if not volume.external:
                    reserve_volume(volume.name, item.service.name)
            for port in item.service.ports:
                reserve_port(
                    PublishedPort(
                        item.service.name,
                        item.plan.run_name,
                        port.host_ip,
                        port.host_port,
                        port.guest_port,
                        port.protocol,
                    )
                )

    def preflight(prepared: tuple[PreparedService, ...]) -> None:
        nonlocal prepare_complete
        attachment_cache.clear()
        preexisting_volume_keys.clear()
        preflighted_requests.clear()
        prepare_complete = False
        ledger = read_project_state(project, roots)
        ledger_volumes = {} if ledger is None else ledger.volumes
        ledger_services = {} if ledger is None else ledger.services
        _validate_global_reservations(prepared, ledger_volumes, ledger_services)
        checked_networks: set[tuple[str, str]] = set()
        for item in prepared:
            if item.plan.preserve_config:
                _preflight_preserved_service(item, ledger_volumes, checked_networks)
                continue
            resolved = item.resolved
            if not isinstance(resolved, ResolvedProjectService):
                raise LifecycleError("project resolver returned an invalid service payload")
            _verify_stack(item, resolved)
            _reject_lima_owned_paths(item, resolved)
            _reject_guest_path_overlaps(item, resolved)
            _validate_resolved_run_spec(item, resolved)
            if resolved.backend in {"kvm", "libvirt-hvf"} and item.service.ports:
                raise ArtifactValidationError(
                    f"service {item.service.name!r} requests ports, but libvirt network interfaces do not provide "
                    "per-domain inbound forwarding; use Lima or an explicitly routed external network"
                )
            if resolved.backend in {"kvm", "libvirt-hvf"} and item.plan.action == "create":
                kvm.validate_domain_name_available(item.plan.run_name)
            if resolved.backend in {"kvm", "libvirt-hvf"}:
                external = [mount.source for mount in item.service.volumes if project.volumes[mount.source].external]
                if external:
                    raise ArtifactValidationError(
                        "external KVM block-volume lookup is not implemented: " + ", ".join(sorted(external))
                    )
            if resolved.backend == "lima-vz" and any(
                project.volumes[mount.source].external for mount in item.service.volumes
            ):
                raise ArtifactValidationError(
                    "external Lima volumes are disabled in v1 because their filesystem/label contract cannot be "
                    "proven without risking implicit formatting"
                )
            network_key = (resolved.backend, resolved.network)
            if network_key not in checked_networks:
                if resolved.backend in {"kvm", "libvirt-hvf"}:
                    kvm.validate_network(resolved.network)
                elif resolved.backend == "lima-vz":
                    lima.validate_network(resolved.network)
                else:
                    raise ArtifactValidationError(f"unsupported project backend: {resolved.backend!r}")
                checked_networks.add(network_key)
            for mount in item.service.volumes:
                volume = project.volumes[mount.source]
                if volume.external:
                    continue
                size_bytes = volume.size_mib * _MIB
                managed = ledger_volumes.get(volume.name)
                if managed is not None and (managed.backend != resolved.backend or managed.size_bytes != size_bytes):
                    raise ArtifactValidationError(
                        f"managed volume {volume.name!r} is ledger-bound to "
                        f"{managed.backend}/{managed.size_bytes} bytes, not "
                        f"{resolved.backend}/{size_bytes} bytes"
                    )
                if resolved.backend in {"kvm", "libvirt-hvf"}:
                    path = kvm_volume_path(roots, project.name, volume.name)
                    if path.exists() or path.is_symlink():
                        verified = verify_kvm_volume(roots, project.name, volume.name, size_bytes)
                        preexisting_volume_keys.add((item.service.name, volume.name))
                        attachment_cache[(item.service.name, volume.name)] = VolumeAttachment(
                            volume.name,
                            mount.target,
                            host_path=verified.path,
                            read_only=mount.read_only,
                        )
                    else:
                        if managed is not None or item.plan.action == "start":
                            raise ArtifactValidationError(
                                f"managed KVM volume {volume.name!r} is missing; refusing to recreate it empty"
                            )
                        preflight_kvm_volume_support()
                else:
                    try:
                        verified_lima = verify_lima_volume(
                            roots,
                            project.name,
                            volume.name,
                            size_bytes,
                            allow_missing=managed is None and item.plan.action != "start",
                            allowed_instance=_allowed_lima_instance(item),
                        )
                    except StateError as exc:
                        raise ArtifactValidationError(
                            f"managed Lima volume {volume.name!r} failed ownership validation: {exc}"
                        ) from exc
                    if verified_lima is not None:
                        preexisting_volume_keys.add((item.service.name, volume.name))
                        attachment_cache[(item.service.name, volume.name)] = VolumeAttachment(
                            volume.name,
                            mount.target,
                            backend_name=verified_lima.backend_name,
                            read_only=False,
                            format=False,
                        )

        # Preserve existing project error precedence: backend/tool probing is
        # the final preflight step, after every pure reservation, spec, path,
        # network, domain, and volume validation, and immediately before the
        # caller enters volume preparation. These cached requests are not
        # stale-proof capability tokens; T14 will add that later contract.
        completed_preflights: dict[str, ResolvedRunRequest] = {}
        for item in prepared:
            if item.plan.action not in {"create", "recreate"} or item.plan.preserve_config:
                continue
            resolved = item.resolved
            if not isinstance(resolved, ResolvedProjectService):
                raise LifecycleError("project resolver returned an invalid service payload")
            completed_preflights[item.plan.service] = runtime_dispatch.preflight_run_request(resolved.request)
        preflighted_requests.update(completed_preflights)

    def prepare(prepared: tuple[PreparedService, ...]) -> tuple[ManagedVolume, ...]:
        nonlocal prepare_complete
        completed: dict[str, ManagedVolume] = {}
        try:
            for item in prepared:
                resolved = item.resolved
                if not isinstance(resolved, ResolvedProjectService):
                    raise LifecycleError("project resolver returned an invalid service payload")
                for mount in item.service.volumes:
                    volume = project.volumes[mount.source]
                    if volume.external:
                        continue
                    size_bytes = volume.size_mib * _MIB
                    volume_key = (item.service.name, volume.name)
                    if resolved.backend == "lima-vz":
                        if volume_key in preexisting_volume_keys:
                            existing = verify_lima_volume(
                                roots,
                                project.name,
                                volume.name,
                                size_bytes,
                                allowed_instance=_allowed_lima_instance(item),
                            )
                            if existing is None:  # pragma: no cover - verifier raises first
                                raise StateError(f"managed Lima volume {volume.name!r} disappeared after preflight")
                            backend_name = existing.backend_name
                            initialize = False
                        else:
                            created = ensure_lima_volume(
                                roots,
                                project.name,
                                volume.name,
                                size_bytes,
                            )
                            backend_name = created.backend_name
                            initialize = created.created
                        attachment = VolumeAttachment(
                            volume.name,
                            mount.target,
                            backend_name=backend_name,
                            read_only=False,
                            format=initialize,
                        )
                    elif resolved.backend in {"kvm", "libvirt-hvf"}:
                        if volume_key in preexisting_volume_keys:
                            existing_kvm = verify_kvm_volume(
                                roots,
                                project.name,
                                volume.name,
                                size_bytes,
                            )
                            host_path = existing_kvm.path
                        else:
                            created_kvm = ensure_kvm_volume(
                                roots,
                                project.name,
                                volume.name,
                                size_bytes,
                            )
                            host_path = created_kvm.path
                        attachment = VolumeAttachment(
                            volume.name,
                            mount.target,
                            host_path=host_path,
                            read_only=mount.read_only,
                        )
                    else:  # pragma: no cover - preflight rejects this first
                        raise ArtifactValidationError(f"unsupported project backend: {resolved.backend!r}")
                    completed[volume.name] = ManagedVolume(volume.name, resolved.backend, size_bytes)
                    attachment_cache[(item.service.name, volume.name)] = attachment
        except Exception as exc:
            raise ProjectPrepareError(str(exc), tuple(completed.values())) from exc
        prepare_complete = True
        return tuple(completed[name] for name in sorted(completed))

    def attachments(item: PreparedService, resolved: ResolvedProjectService) -> tuple[VolumeAttachment, ...]:
        if not prepare_complete:
            raise StateError("project volumes were not prepared before service start")
        result: list[VolumeAttachment] = []
        for mount in item.service.volumes:
            volume = project.volumes[mount.source]
            cached = attachment_cache.get((item.service.name, volume.name))
            if cached is None:
                raise StateError(f"project volume {volume.name!r} has no prepared attachment")
            result.append(cached)
        return tuple(result)

    def start_service(
        item: PreparedService,
        *,
        expected_identity: ExpectedRunIdentity | None = None,
    ) -> object:
        if item.plan.action == "start":
            if expected_identity is None:
                return runtime_dispatch.start(item.plan.run_name, roots=roots)
            return runtime_dispatch.start(
                item.plan.run_name,
                roots=roots,
                expected_identity=expected_identity,
            )
        resolved = item.resolved
        if not isinstance(resolved, ResolvedProjectService):
            raise LifecycleError("project resolver returned an invalid service payload")
        ports = tuple(
            PortForward(port.host_ip, port.host_port, port.guest_port, port.protocol) for port in item.service.ports
        )
        spec = RunSpec(
            name=item.plan.run_name,
            stack=resolved.stack,
            memory_mib=item.service.memory_mib,
            vcpus=item.service.vcpus,
            network=resolved.network,
            ports=ports,
            volumes=attachments(item, resolved),
            environment=resolved.environment,
            cloud_init=resolved.cloud_init,
        )
        logical_request = preflighted_requests.get(item.plan.service)
        if logical_request is not resolved.request:
            raise StateError("project run request was not preflighted before volume preparation")
        request = runtime_dispatch.bind_run_request_volumes(
            logical_request,
            spec,
            dispatch_key=resolved.request.dispatch_key,
        )
        if resolved.backend == "lima-vz":
            try:
                return runtime_dispatch.run(request, roots=roots)
            except Exception as exc:
                if spec.volumes and not any(volume.format for volume in spec.volumes):
                    detail = str(exc).strip() or type(exc).__name__
                    raise LifecycleError(
                        "Lima start failed while all existing disks remained protected by format:false: "
                        f"{detail}. Automatic reformatting is disabled; inspect the original failure and volume "
                        "contents before explicitly deleting any named volume."
                    ) from exc
                raise
        return runtime_dispatch.run(request, roots=roots)

    def stop_service(
        name: str,
        *,
        expected_identity: ExpectedRunIdentity | None = None,
    ) -> object:
        if expected_identity is None:
            return runtime_dispatch.stop(name, roots=roots)
        return runtime_dispatch.stop(name, roots=roots, expected_identity=expected_identity)

    def remove_service(
        name: str,
        *,
        expected_identity: ExpectedRunIdentity | None = None,
    ) -> object:
        if not state.run_entry_present_or_ambiguous(roots, name):
            return {"name": name, "status": "removed"}
        if expected_identity is None:
            return runtime_dispatch.rm(name, roots=roots, volumes=True)
        return runtime_dispatch.rm(
            name,
            roots=roots,
            volumes=True,
            expected_identity=expected_identity,
        )

    def preflight_down(
        targets: tuple[DownTarget, ...],
        volumes: tuple[ManagedVolume, ...],
    ) -> None:
        allowed_instances = tuple(target.run_name for target in targets)
        ledger = read_project_state(project, roots)
        if ledger is None and volumes:
            raise StateError("project ownership ledger disappeared before volume deletion preflight")
        allowed_kvm_run_ids = {
            managed.run_name: managed.run_id
            for managed in (() if ledger is None else ledger.services.values())
            if managed.run_name in allowed_instances and managed.backend in {"kvm", "libvirt-hvf"}
        }
        for volume in volumes:
            if volume.backend in {"kvm", "libvirt-hvf"}:
                path = kvm_volume_path(roots, project.name, volume.name)
                if path.exists() or path.is_symlink():
                    verify_kvm_volume(roots, project.name, volume.name, volume.size_bytes)
                    _validate_kvm_volume_references(path, allowed_kvm_run_ids)
            elif volume.backend == "lima-vz":
                preflight_delete_lima_volume(
                    roots,
                    project.name,
                    volume.name,
                    volume.size_bytes,
                    allowed_instances=allowed_instances,
                )
            else:
                raise StateError(f"managed volume {volume.name!r} has unsupported backend {volume.backend!r}")

    def remove_volume(_project_name: str, volume_name: str, backend: str, size_bytes: int) -> object:
        if backend == "lima-vz":
            return delete_lima_volume(roots, project.name, volume_name, size_bytes)
        if backend in {"kvm", "libvirt-hvf"}:
            path = kvm_volume_path(roots, project.name, volume_name)
            if path.exists() or path.is_symlink():
                _validate_kvm_volume_references(path, {})

            def validate_quarantine(old_path: Path, quarantine_path: Path) -> None:
                _validate_kvm_volume_references(old_path, {})
                _validate_kvm_volume_references(quarantine_path, {})

            return delete_kvm_volume(
                roots,
                project.name,
                volume_name,
                size_bytes,
                quarantine_validator=validate_quarantine,
            )
        raise StateError(f"managed volume {volume_name!r} has unsupported backend {backend!r}")

    def service_logs(
        name: str,
        follow: bool,
        *,
        expected_identity: ExpectedRunIdentity | None = None,
    ):
        if expected_identity is None:
            return runtime_dispatch.logs(name, roots=roots, follow=follow)
        return runtime_dispatch.logs(
            name,
            roots=roots,
            follow=follow,
            expected_identity=expected_identity,
        )

    return ProjectCallbacks(
        inspect=lambda name: _inspect_run(name, roots),
        resolve=resolve,
        start=start_service,
        stop=stop_service,
        remove=remove_service,
        preflight=preflight,
        prepare=prepare,
        preflight_down=preflight_down,
        port_available=_port_is_available,
        remove_volume=remove_volume,
        logs=service_logs,
        desired_digest=desired_digest,
    )


def external_lima_volume_name(project: Project, volume_name: str) -> str:
    """Expose the managed-name mapping for config/debug output without creating a disk."""

    volume = project.volumes[volume_name]
    return volume.external_name or (volume.name if volume.external else lima_backend_name(project.name, volume.name))
