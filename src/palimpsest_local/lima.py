"""Native Apple Silicon Ubuntu VM lifecycle through the Lima VZ backend."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import platform
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import state
from .digest import require_file_digest
from .errors import ArtifactValidationError, BuildError, LifecycleError, StateError
from .refs import BuildSpec, LayerRef, RunSpec, StackRef
from .runtime_types import (
    DispatchKey,
    ExecRequest,
    ExistingRunRecord,
    ProcessSession,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RuntimeOperation,
)
from .state import RunPaths, StatePaths

_BACKEND = "lima-vz"
_CONFIG_NAME = "lima.yaml"
_TIMEOUT_SECONDS = 600
_LIMA_NETWORK_RE = re.compile(r"^lima:[a-z0-9][a-z0-9-]{0,62}$")
_LIMA_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?\b")
_RUN_ID_ENV = "PALIMPSEST_RUN_ID"


def available() -> bool:
    return platform.system() == "Darwin" and platform.machine() in {"arm64", "aarch64"}


def is_lima_run(rpaths: RunPaths) -> bool:
    try:
        return state.read_run_state(rpaths).get("backend") == _BACKEND
    except Exception:
        return False


def _run_command(argv: list[str], *, timeout_seconds: float = _TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout_seconds)
    except FileNotFoundError as exc:
        raise LifecycleError("Lima is required on macOS; install it with: brew install lima") from exc
    except subprocess.TimeoutExpired as exc:
        raise LifecycleError(f"Lima command timed out: {' '.join(argv[:2])}") from exc


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise LifecycleError(f"Lima {action} failed: {detail}")


def _require_supported_version() -> None:
    result = _run_command(["limactl", "--version"], timeout_seconds=30)
    _require_success(result, "version check")
    output = "\n".join(value for value in (result.stdout, result.stderr) if isinstance(value, str) and value)
    match = _LIMA_VERSION_RE.search(output)
    if match is None:
        raise LifecycleError("cannot determine Lima version; version 2.1 or newer in the 2.x series is required")
    version = tuple(int(part) for part in match.groups())
    if version < (2, 1, 0) or version >= (3, 0, 0):
        raise LifecycleError(
            f"unsupported Lima version {'.'.join(match.groups())}; version 2.1 or newer in the 2.x series is required"
        )


def _decode_json_objects(output: str, context: str) -> list[dict[str, Any]]:
    raw = output.strip()
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        values: list[object] = []
        for line in raw.splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise LifecycleError(f"Lima returned invalid {context} JSON") from exc
    else:
        values = decoded if isinstance(decoded, list) else [decoded]
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            raise LifecycleError(f"Lima returned an invalid {context} list")
        result.append(value)
    return result


def validate_network(name: str) -> None:
    """Fail closed unless Lima VZ can provide the requested network."""

    if not available():
        raise LifecycleError("Lima VZ networks require macOS on Apple Silicon")
    _require_supported_version()
    if name in {"default", "vzNAT"}:
        return
    if _LIMA_NETWORK_RE.fullmatch(name) is None:
        raise ArtifactValidationError(
            "Lima networks must be 'default', 'vzNAT', or 'lima:NAME' with a lowercase hyphen-only NAME"
        )
    result = _run_command(["limactl", "network", "list", "--json"], timeout_seconds=30)
    _require_success(result, "network list")
    values = _decode_json_objects(result.stdout, "network")
    names: list[str] = []
    for value in values:
        candidate = value.get("name")
        if not isinstance(candidate, str) or not candidate:
            raise LifecycleError("Lima network list contains an invalid name")
        names.append(candidate)
    if len(set(names)) != len(names):
        raise LifecycleError("Lima network list contains duplicate names")
    backend_name = name.split(":", 1)[1]
    if backend_name not in names:
        raise LifecycleError(f"Lima network does not exist: {backend_name}")


def _lima_config(spec: RunSpec, *, run_id: str | None = None) -> str:
    image_uri = spec.stack.base.local_path.resolve().as_uri()
    lines = [
        'minimumLimaVersion: "2.0.0"',
        "vmType: vz",
        "arch: aarch64",
        f"cpus: {spec.vcpus}",
        f'memory: "{spec.memory_mib}MiB"',
        'disk: "30GiB"',
        "containerd:",
        "  system: false",
        "  user: false",
        "mounts: []",
        "networks:",
    ]
    if spec.network in {"default", "vzNAT"}:
        lines.append("  - vzNAT: true")
    elif _LIMA_NETWORK_RE.fullmatch(spec.network):
        lines.extend([f"  - lima: {json.dumps(spec.network.split(':', 1)[1])}"])
    else:
        raise ArtifactValidationError("Lima networks must be 'default', 'vzNAT', or an existing 'lima:NAME' network")
    lines.extend(
        [
            "hostResolver:",
            "  enabled: true",
            "ssh:",
            "  localPort: 0",
            "  forwardAgent: false",
            "images:",
            f"  - location: {json.dumps(image_uri)}",
            "    arch: aarch64",
        ]
    )
    if spec.volumes:
        lines.append("additionalDisks:")
        for volume in spec.volumes:
            if volume.backend_name is None:
                raise ArtifactValidationError("Lima runs require a Lima disk name for every project volume")
            lines.extend(
                [
                    f"  - name: {json.dumps(volume.backend_name)}",
                    f"    format: {'true' if volume.format else 'false'}",
                    f"    fsType: {json.dumps(volume.filesystem)}",
                ]
            )
    if spec.ports:
        lines.append("portForwards:")
        for port in spec.ports:
            lines.extend(
                [
                    f"  - guestPort: {port.guest_port}",
                    f"    hostPort: {port.host_port}",
                    f"    hostIP: {json.dumps(port.host_ip)}",
                    f"    proto: {json.dumps(port.protocol)}",
                    "    static: true",
                ]
            )
    if any(name == _RUN_ID_ENV for name, _value in spec.environment):
        raise ArtifactValidationError(f"environment name {_RUN_ID_ENV!r} is reserved for runtime ownership")
    environment = spec.environment + (((_RUN_ID_ENV, run_id),) if run_id is not None else ())
    if environment:
        lines.append("env:")
        lines.extend(f"  {name}: {json.dumps(value)}" for name, value in environment)

    provision_entries: list[str] = []
    provision_script: list[str] = ["#!/bin/sh", "set -eu"]
    for volume in spec.volumes:
        assert volume.backend_name is not None
        source = f"/mnt/lima-{volume.backend_name}"
        options = "-o ro --bind" if volume.read_only else "--bind"
        provision_script.extend(
            [
                f"install -d -m 0755 {shlex.quote(volume.mount_path)}",
                f"mountpoint -q {shlex.quote(volume.mount_path)} || mount {options} "
                f"{shlex.quote(source)} {shlex.quote(volume.mount_path)}",
            ]
        )
    if len(provision_script) > 2:
        provision_entries.extend(["  - mode: system", "    script: |"])
        provision_entries.extend(f"      {line}" for line in provision_script)

    if spec.cloud_init is not None:
        packages = tuple(getattr(spec.cloud_init, "packages", ()))
        write_files = tuple(getattr(spec.cloud_init, "write_files", ()))
        commands = tuple(getattr(spec.cloud_init, "runcmd", ()))
        for item in write_files:
            path = getattr(item, "path", None)
            content = getattr(item, "content", None)
            permissions = getattr(item, "permissions", None)
            if not all(isinstance(value, str) for value in (path, content, permissions)):
                raise ArtifactValidationError("Lima cloud-init write_files entries are invalid")
            provision_entries.extend(
                [
                    "  - mode: data",
                    f"    path: {json.dumps(path)}",
                    "    owner: root:root",
                    f"    permissions: {permissions.removeprefix('0')}",
                    "    overwrite: true",
                    "    content: |",
                    *(f"      {line}" if line else "" for line in content.splitlines()),
                ]
            )
        if packages or commands:
            identity = hashlib.sha256(
                json.dumps(
                    {"packages": packages, "runcmd": commands},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            marker = f"/var/lib/palimpsest/provision/{identity}.done"
            once_script = [
                "#!/bin/bash",
                "set -euo pipefail",
                f"marker={shlex.quote(marker)}",
                'if [ ! -e "$marker" ]; then',
            ]
            if packages:
                once_script.extend(
                    [
                        "  export DEBIAN_FRONTEND=noninteractive",
                        "  apt-get update",
                        "  apt-get install -y -- " + " ".join(shlex.quote(package) for package in packages),
                    ]
                )
            once_script.extend(f"  {shlex.join(command)}" for command in commands)
            once_script.extend(["  install -d -m 0755 /var/lib/palimpsest/provision", '  touch "$marker"', "fi"])
            provision_entries.extend(["  - mode: system", "    script: |"])
            provision_entries.extend(f"      {line}" for line in once_script)
    if provision_entries:
        lines.append("provision:")
        lines.extend(provision_entries)
    return "\n".join(lines) + "\n"


def validate_run_spec(spec: RunSpec) -> None:
    """Render and validate a Lima run definition without touching backend state."""

    _lima_config(spec, run_id="00000000-0000-0000-0000-000000000000")


def _list_instances() -> list[dict[str, Any]]:
    result = _run_command(["limactl", "list", "--format", "json"])
    _require_success(result, "list")
    instances = _decode_json_objects(result.stdout, "instance")
    names = [instance.get("name") for instance in instances]
    if any(not isinstance(name, str) or not name for name in names):
        raise LifecycleError("Lima instance list contains an invalid name")
    if len(set(names)) != len(names):
        raise LifecycleError("Lima instance list contains duplicate names")
    return instances


def _instance_info_or_none(name: str) -> dict[str, Any] | None:
    for instance in _list_instances():
        if instance.get("name") == name:
            return instance
    return None


def _instance_info(name: str) -> dict[str, Any]:
    instance = _instance_info_or_none(name)
    if instance is not None:
        return instance
    raise LifecycleError(f"Lima instance '{name}' was not found after startup")


def _instance_run_id(instance: dict[str, Any]) -> str | None:
    config = instance.get("config")
    environment = config.get("env") if isinstance(config, dict) else None
    value = environment.get(_RUN_ID_ENV) if isinstance(environment, dict) else None
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError("Lima instance contains a malformed Palimpsest run marker")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise StateError("Lima instance contains an invalid Palimpsest run marker") from exc
    if canonical != value:
        raise StateError("Lima instance Palimpsest run marker is not a canonical UUID")
    return value


def _require_owned_instance(instance: dict[str, Any], expected_run_id: str, name: str) -> None:
    actual = _instance_run_id(instance)
    if actual != expected_run_id:
        raise StateError(
            f"Lima instance {name!r} is foreign: expected run marker {expected_run_id!r}, found {actual!r}"
        )


def _require_disk_format_sealed(
    instance: dict[str, Any],
    initializing: list[tuple[int, object]],
    name: str,
) -> None:
    """Prove Lima's persisted instance config can no longer format a disk."""

    config = instance.get("config")
    disks = config.get("additionalDisks") if isinstance(config, dict) else None
    if not isinstance(disks, list):
        raise StateError(f"Lima instance {name!r} did not expose its persisted additionalDisks configuration")
    for index, volume in initializing:
        if index >= len(disks) or not isinstance(disks[index], dict):
            raise StateError(f"Lima instance {name!r} is missing persisted disk index {index}")
        expected_name = getattr(volume, "backend_name", None)
        disk = disks[index]
        if disk.get("name") != expected_name or disk.get("format") is not False:
            raise StateError(f"Lima instance {name!r} did not persist format:false for disk {expected_name!r}")


def _require_existing_disk_config_safe(
    instance: dict[str, Any],
    record: Mapping[str, object],
    name: str,
) -> None:
    """Reject any live config drift that could format an existing named disk."""

    raw_volumes = record.get("volumes", [])
    if not isinstance(raw_volumes, list):
        raise StateError(f"Lima run {name!r} has malformed applied volume state")
    expected_names: list[str] = []
    for raw_volume in raw_volumes:
        if not isinstance(raw_volume, Mapping):
            raise StateError(f"Lima run {name!r} has malformed applied volume state")
        backend_name = raw_volume.get("backend_name")
        if not isinstance(backend_name, str) or not backend_name:
            raise StateError(f"Lima run {name!r} has malformed applied volume state")
        expected_names.append(backend_name)
    if len(set(expected_names)) != len(expected_names):
        raise StateError(f"Lima run {name!r} has duplicate applied volume state")

    config = instance.get("config")
    raw_disks = config.get("additionalDisks") if isinstance(config, dict) else None
    if raw_disks is None and not expected_names:
        return
    if not isinstance(raw_disks, list) or len(raw_disks) != len(expected_names):
        raise StateError(f"Lima instance {name!r} additionalDisks drifted from applied volume state")
    for index, expected_name in enumerate(expected_names):
        disk = raw_disks[index]
        if not isinstance(disk, dict) or disk.get("name") != expected_name or disk.get("format") is not False:
            raise StateError(f"Lima instance {name!r} disk {expected_name!r} is not safely sealed with format:false")


def _instance_runtime_status(instance: dict[str, Any]) -> str:
    value = instance.get("status")
    statuses = {
        "Running": "running",
        "Stopped": "stopped",
        "Broken": "failed",
        "Starting": "starting",
        "Stopping": "stopping",
    }
    if not isinstance(value, str) or value not in statuses:
        raise LifecycleError(f"Lima instance returned unsupported status: {value!r}")
    return statuses[value]


def inspect_instance_status(name: str) -> str | None:
    """Return live status for a Lima instance without asserting ownership."""

    instance = _instance_info_or_none(name)
    return None if instance is None else _instance_runtime_status(instance)


def _mutable_snapshot_state(snapshot: state.RunLedgerSnapshot) -> dict[str, Any]:
    def thaw(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [thaw(item) for item in value]
        return value

    return thaw(snapshot.state)


def _reconcile_result(snapshot: state.RunLedgerSnapshot) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "owner": {
            "schema_version": 1,
            "run_id": snapshot.record.run_id,
            "name": snapshot.record.name,
        },
        "state": _mutable_snapshot_state(snapshot),
        "warnings": [],
    }


def _require_lima_snapshot(
    snapshot: state.RunLedgerSnapshot,
    expected: ExistingRunRecord,
) -> dict[str, Any]:
    if snapshot.record != expected:
        raise StateError("run ledger changed during Lima reconciliation")
    record = _mutable_snapshot_state(snapshot)
    if record.get("backend") != _BACKEND:
        raise StateError("run is not a Lima-managed macOS VM")
    return record


def reconcile_run(
    name: str,
    *,
    roots: StatePaths | None = None,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    """Reconcile one exact owner-bound Lima run against live ``limactl`` state."""

    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    expected = _expected_record or state.read_run_dispatch_record(roots, name)
    if expected.name != name:
        raise StateError("run dispatch identity does not match requested name")
    if (
        expected.dispatch_key.runtime_kind is not RuntimeKind.CLOUD_IMAGE
        or expected.dispatch_key.backend is not RuntimeBackend.LIMA_VZ
    ):
        raise StateError("run is not managed by the Lima runtime")

    def observe(record: dict[str, Any]) -> str:
        if record.get("backend") != _BACKEND:
            raise StateError("run is not a Lima-managed macOS VM")
        instance = _instance_info_or_none(name)
        if instance is None:
            reconciled = "removed"
        else:
            live = _instance_runtime_status(instance)
            _require_owned_instance(instance, expected.run_id, name)
            if record.get("status") in {"removed", "failed"}:
                raise StateError(f"foreign or ambiguous Lima instance uses owned run name: {name}")
            reconciled = live
        return reconciled

    initial = state.read_run_ledger_snapshot(roots, name)
    if initial.record != expected:
        raise StateError("run ledger changed during Lima reconciliation")
    initial_record = _mutable_snapshot_state(initial)
    reconciled = observe(initial_record)
    after_observe = state.read_run_ledger_snapshot(roots, name)
    if after_observe.record != expected:
        raise StateError("run ledger changed during Lima reconciliation")
    if reconciled == initial_record.get("status"):
        return _reconcile_result(initial)
    if expected.state_schema_version == 1:
        result = _reconcile_result(initial)
        result["state"] = {**initial_record, "status": reconciled, "updated_at": state.utc_now_iso()}
        return result

    with state.locked_existing_run(roots, name, expected=expected) as mutation:
        record = mutation.mutable_state()
        reconciled = observe(record)
        if reconciled != record.get("status"):
            mutation.write_state(
                reconciled,
                {**record, "updated_at": state.utc_now_iso()},
            )
        return _reconcile_result(mutation.snapshot)


def inspect_run(
    name: str,
    *,
    roots: StatePaths | None = None,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    """Compatibility entry point for exact single-run Lima reconciliation."""

    return reconcile_run(name, roots=roots, _expected_record=_expected_record)


def _write_state(
    rpaths: RunPaths,
    status: str,
    data: dict[str, Any],
    *,
    reservation: state.NewRunReservation | None = None,
) -> dict[str, Any]:
    if reservation is not None:
        return reservation.write_state(status, data)
    return state.write_run_state(rpaths, status=status, data={**data, "status": status})


def _guest_ipv4(name: str) -> str:
    result = _run_command(
        ["limactl", "shell", name, "ip", "-4", "-o", "addr", "show", "scope", "global"], timeout_seconds=60
    )
    _require_success(result, "query guest network")
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            try:
                address = ipaddress.ip_interface(fields[3]).ip
            except ValueError:
                continue
            if not address.is_loopback:
                return str(address)
    raise LifecycleError("Lima guest did not report an IPv4 address")


def run(spec: RunSpec, *, roots: StatePaths | None = None, timeout_seconds: float = _TIMEOUT_SECONDS) -> dict[str, Any]:
    """Create and start a verified ARM64 Ubuntu cloud image through Lima VZ."""
    if not available():
        raise LifecycleError("native Lima VZ runs require macOS on Apple Silicon")
    if spec.stack.base.arch != "aarch64":
        raise ArtifactValidationError("native macOS Lima runs require an aarch64 Ubuntu cloud image")
    if spec.network == "none":
        raise ArtifactValidationError("native macOS Lima prototype requires a managed network for SSH access")
    try:
        require_file_digest(spec.stack.base.local_path, spec.stack.base.digest)
        for layer in spec.stack.layers:
            require_file_digest(layer.local_path, layer.digest)
    except Exception as exc:
        raise ArtifactValidationError(f"runtime input digest mismatch: {exc}") from exc

    roots = roots or state.init_roots()
    try:
        _instance_info(spec.name)
    except LifecycleError as exc:
        if "was not found after startup" not in str(exc):
            raise
    else:
        raise StateError(f"a Lima instance named '{spec.name}' already exists and is not owned by this run")
    dispatch_key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ)
    with state.reserve_new_run(roots, spec.name, dispatch_key) as reservation:
        rpaths = reservation.paths
        config_path = rpaths.root / _CONFIG_NAME
        try:
            owner = reservation.record
            record: dict[str, Any] = {
                "created_at": state.utc_now_iso(),
                "updated_at": state.utc_now_iso(),
                "lima_instance": spec.name,
                "base": {
                    "digest": spec.stack.base.digest,
                    "local_path": str(spec.stack.base.local_path),
                    "disk_format": spec.stack.base.disk_format,
                    "arch": spec.stack.base.arch,
                },
                "layers": [
                    {"digest": layer.digest, "local_path": str(layer.local_path)} for layer in spec.stack.layers
                ],
                "layer_attachment": {
                    "delivery": "scp-copy",
                    "device": "loop",
                    "mount": "squashfs-ro",
                },
                "network": spec.network,
                "volumes": [
                    {
                        "name": volume.name,
                        "backend_name": volume.backend_name,
                        "mount_path": volume.mount_path,
                        "filesystem": volume.filesystem,
                        "read_only": volume.read_only,
                    }
                    for volume in spec.volumes
                ],
                "ports": [
                    {
                        "host_ip": port.host_ip,
                        "host_port": port.host_port,
                        "guest_port": port.guest_port,
                        "protocol": port.protocol,
                    }
                    for port in spec.ports
                ],
                "environment_names": [name for name, _value in spec.environment],
                "cloud_init": spec.cloud_init is not None,
                "domain_uuid": None,
                "guest_ip": None,
                "cleanup_flags": {},
            }
            _write_state(rpaths, "creating", record, reservation=reservation)
            reservation.write_file(_CONFIG_NAME, _lima_config(spec, run_id=owner.run_id).encode("utf-8"))
            created = _run_command(
                ["limactl", "create", "--tty=false", "--name", spec.name, str(config_path)],
                timeout_seconds=timeout_seconds,
            )
            _require_success(created, "create")
            _write_state(
                rpaths,
                "defined",
                {**record, "updated_at": state.utc_now_iso()},
                reservation=reservation,
            )
            reservation.verify_binding()
            started = _run_command(
                ["limactl", "start", "--timeout", f"{int(timeout_seconds)}s", spec.name],
                timeout_seconds=timeout_seconds + 30,
            )
            _require_success(started, "start")
            instance = _instance_info(spec.name)
            _require_owned_instance(instance, owner.run_id, spec.name)
            if instance.get("status") != "Running":
                raise LifecycleError(f"Lima instance '{spec.name}' did not reach Running state")
            port = instance.get("sshLocalPort")
            config = instance.get("sshConfigFile")
            if not isinstance(port, int) or not 1 <= port <= 65535 or not isinstance(config, str) or not config:
                raise LifecycleError("Lima did not report usable SSH connection data")
            initializing = [(index, volume) for index, volume in enumerate(spec.volumes) if volume.format]
            if initializing:
                # ``format: true`` is permitted for exactly the first boot of a
                # newly-created managed disk.  Persistently leaving it in the
                # instance configuration could reformat after filesystem-label
                # drift, so switch every such entry off while the VM is stopped
                # and require a clean second boot before exposing success.
                reservation.verify_binding()
                _require_success(
                    _run_command(["limactl", "stop", "--force", spec.name], timeout_seconds=60),
                    "stop after first-use disk formatting",
                )
                for index, volume in initializing:
                    assert volume.backend_name is not None
                    expression = (
                        f"select(.additionalDisks[{index}].name == {json.dumps(volume.backend_name)}) | "
                        f".additionalDisks[{index}].format = false"
                    )
                    reservation.verify_binding()
                    _require_success(
                        _run_command(
                            ["limactl", "edit", spec.name, "--tty=false", "--set", expression],
                            timeout_seconds=60,
                        ),
                        f"disable repeated formatting for disk {volume.backend_name}",
                    )
                sealed_instance = _instance_info(spec.name)
                _require_owned_instance(sealed_instance, owner.run_id, spec.name)
                _require_disk_format_sealed(sealed_instance, initializing, spec.name)
                safe_spec = replace(
                    spec,
                    volumes=tuple(replace(volume, format=False) for volume in spec.volumes),
                )
                reservation.write_file(
                    _CONFIG_NAME,
                    _lima_config(safe_spec, run_id=owner.run_id).encode("utf-8"),
                )
                restarted = _run_command(
                    ["limactl", "start", "--timeout", f"{int(timeout_seconds)}s", spec.name],
                    timeout_seconds=timeout_seconds + 30,
                )
                _require_success(restarted, "restart after first-use disk formatting")
                instance = _instance_info(spec.name)
                _require_owned_instance(instance, owner.run_id, spec.name)
                if instance.get("status") != "Running":
                    raise LifecycleError(f"Lima instance '{spec.name}' did not restart after disabling disk formatting")
                port = instance.get("sshLocalPort")
                config = instance.get("sshConfigFile")
                if not isinstance(port, int) or not 1 <= port <= 65535 or not isinstance(config, str) or not config:
                    raise LifecycleError("Lima did not report usable SSH connection data after disk-safe restart")
            reservation.write_file("console.log", b"")
            # Guest command appenders use the visible path while the
            # cooperative per-name create lock remains held.
            reservation.verify_binding()
            _attach_layers(spec.name, spec.stack.layers, console_log=rpaths.console)
            reservation.verify_binding()
            guest_ip = _guest_ipv4(spec.name)
            return _write_state(
                rpaths,
                "running",
                {
                    **record,
                    "guest_ip": guest_ip,
                    "ssh_host": "127.0.0.1",
                    "ssh_local_port": port,
                    "ssh_config_file": config,
                    "updated_at": state.utc_now_iso(),
                },
                reservation=reservation,
            )
        except BaseException as exc:
            try:
                reservation.write_failure({"error": str(exc), "updated_at": state.utc_now_iso()})
            except BaseException:
                pass
            try:
                cleanup_instance = _instance_info_or_none(spec.name)
                if cleanup_instance is not None:
                    _require_owned_instance(cleanup_instance, owner.run_id, spec.name)
                    _run_command(["limactl", "delete", "--force", spec.name], timeout_seconds=60)
            except BaseException:
                pass
            raise


def shell_command(name: str, *, roots: StatePaths | None = None) -> list[str]:
    roots = roots or state.init_roots()
    inspect_run(name, roots=roots)
    return ["limactl", "shell", name]


def exec_command(name: str, argv: list[str], *, roots: StatePaths | None = None) -> list[str]:
    if not argv:
        raise ArtifactValidationError("exec command must be nonempty")
    roots = roots or state.init_roots()
    inspect_run(name, roots=roots)
    return ["limactl", "shell", name, *argv]


def shell_session(
    name: str,
    *,
    roots: StatePaths | None = None,
    _expected_record: ExistingRunRecord | None = None,
) -> ProcessSession:
    """Open an exact owner-checked Lima shell process."""

    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    expected = _expected_record or state.read_run_dispatch_record(roots, name)
    if expected.dispatch_key != DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ):
        raise StateError("run is not managed by the Lima runtime")
    state.require_bound_run_dispatch_record(roots, expected)
    raise RuntimeCapabilityError(RuntimeOperation.SHELL, expected.dispatch_key)


def exec_session(
    name: str,
    request: ExecRequest,
    *,
    roots: StatePaths | None = None,
    _expected_record: ExistingRunRecord | None = None,
) -> ProcessSession:
    """Open an exact owner-checked non-interactive Lima exec process."""

    if not isinstance(request, ExecRequest):
        raise TypeError("Lima exec requires an ExecRequest")
    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    expected = _expected_record or state.read_run_dispatch_record(roots, name)
    if expected.dispatch_key != DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ):
        raise StateError("run is not managed by the Lima runtime")
    state.require_bound_run_dispatch_record(roots, expected)
    raise RuntimeCapabilityError(RuntimeOperation.EXEC, expected.dispatch_key)


def logs(
    name: str,
    *,
    roots: StatePaths | None = None,
    follow: bool = False,
    _expected_record: ExistingRunRecord | None = None,
) -> Iterator[str]:
    """Read the owned Lima VM's current-boot system journal.

    A Palimpsest service is a VM rather than one container process, so its useful
    runtime log is the guest journal.  Stopped runs fall back to the locally
    retained provisioning/attachment console.
    """

    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    if _expected_record is not None:
        state.require_bound_run_dispatch_record(roots, _expected_record)
    rpaths = state.run_paths(roots, name)
    inspected = inspect_run(name, roots=roots)
    record = inspected["state"]
    if record.get("status") != "running":
        if follow:
            raise LifecycleError(f"cannot follow logs for non-running Lima run '{name}'")
        if rpaths.console.is_file():
            yield from rpaths.console.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        return

    argv = ["limactl", "shell", name, "journalctl", "-b", "--no-pager", "-o", "cat"]
    if not follow:
        result = _run_command(argv)
        _require_success(result, "read guest journal")
        yield from result.stdout.splitlines(keepends=True)
        return

    argv.append("--follow")
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise LifecycleError("Lima is required on macOS; install it with: brew install lima") from exc
    assert process.stdout is not None
    try:
        yield from process.stdout
    finally:
        if process.poll() is None:
            process.terminate()
        return_code = process.wait()
    if return_code != 0:
        stderr = process.stderr.read().strip() if process.stderr is not None else ""
        raise LifecycleError(f"Lima follow guest journal failed: {stderr or f'exit status {return_code}'}")


def stop(
    name: str,
    *,
    roots: StatePaths | None = None,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    with state.locked_existing_run(roots, name, expected=_expected_record) as mutation:
        if (
            mutation.record.dispatch_key.runtime_kind is not RuntimeKind.CLOUD_IMAGE
            or mutation.record.dispatch_key.backend is not RuntimeBackend.LIMA_VZ
        ):
            raise StateError("run is not managed by the Lima runtime")
        record = mutation.mutable_state()
        instance = _instance_info_or_none(name)
        if instance is None:
            if not mutation.is_legacy and record.get("status") != "removed":
                mutation.write_state("removed", {**record, "updated_at": state.utc_now_iso()})
            raise StateError(f"owned Lima instance is missing: {name}")
        _require_owned_instance(instance, mutation.record.run_id, name)
        live_status = _instance_runtime_status(instance)
        if record.get("status") in {"failed", "removed"}:
            raise StateError(f"foreign or ambiguous Lima instance uses owned run name: {name}")
        if live_status == "stopped":
            if record.get("status") == "stopped":
                return record
            if mutation.is_legacy:
                return {**record, "status": "stopped", "updated_at": state.utc_now_iso()}
            return mutation.write_state("stopped", {**record, "updated_at": state.utc_now_iso()})
        if not mutation.is_legacy and live_status != record.get("status"):
            record = mutation.write_state(live_status, {**record, "updated_at": state.utc_now_iso()})
        mutation.verify_binding()
        _require_success(_run_command(["limactl", "stop", "--force", name], timeout_seconds=60), "stop")
        final_instance = _instance_info_or_none(name)
        if final_instance is None:
            raise StateError(f"owned Lima instance is missing after stop: {name}")
        _require_owned_instance(final_instance, mutation.record.run_id, name)
        if _instance_runtime_status(final_instance) != "stopped":
            raise LifecycleError(f"Lima instance '{name}' did not stop")
        return mutation.write_state("stopped", {**record, "updated_at": state.utc_now_iso()})


def start(
    name: str,
    *,
    roots: StatePaths | None = None,
    timeout_seconds: float = _TIMEOUT_SECONDS,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    """Start an owned stopped Lima VM and restore its runtime SquashFS mounts."""

    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    with state.locked_existing_run(roots, name, expected=_expected_record) as mutation:
        if (
            mutation.record.dispatch_key.runtime_kind is not RuntimeKind.CLOUD_IMAGE
            or mutation.record.dispatch_key.backend is not RuntimeBackend.LIMA_VZ
        ):
            raise StateError("run is not managed by the Lima runtime")
        rpaths = mutation.paths
        record = mutation.mutable_state()
        instance = _instance_info_or_none(name)
        if instance is None:
            if not mutation.is_legacy and record.get("status") != "removed":
                mutation.write_state("removed", {**record, "updated_at": state.utc_now_iso()})
            raise StateError(f"owned Lima instance is missing: {name}")
        _require_owned_instance(instance, mutation.record.run_id, name)
        live_status = _instance_runtime_status(instance)
        if record.get("status") in {"failed", "removed"}:
            raise StateError(f"foreign or ambiguous Lima instance uses owned run name: {name}")
        _require_existing_disk_config_safe(instance, record, name)
        if live_status == "running":
            if record.get("status") == "running":
                return record
            if mutation.is_legacy:
                return {**record, "status": "running", "updated_at": state.utc_now_iso()}
            return mutation.write_state("running", {**record, "updated_at": state.utc_now_iso()})
        if live_status not in {"stopped", "running"}:
            if not mutation.is_legacy and live_status != record.get("status"):
                mutation.write_state(live_status, {**record, "updated_at": state.utc_now_iso()})
            raise LifecycleError(f"Lima run '{name}' cannot be started from status {live_status!r}")
        if record.get("status") not in {"stopped", "running"}:
            raise LifecycleError(f"Lima run '{name}' cannot be started from status {record.get('status')!r}")
        legacy = mutation.is_legacy
        start_attempted = False
        if not legacy:
            record = mutation.write_state("starting", {**record, "updated_at": state.utc_now_iso()})
        try:
            mutation.verify_binding()
            start_attempted = True
            result = _run_command(
                ["limactl", "start", "--timeout", f"{int(timeout_seconds)}s", name],
                timeout_seconds=timeout_seconds + 30,
            )
            _require_success(result, "start")
            instance = _instance_info(name)
            _require_owned_instance(instance, mutation.record.run_id, name)
            if instance.get("status") != "Running":
                raise LifecycleError(f"Lima instance '{name}' did not reach Running state")
            layers = tuple(
                LayerRef(
                    digest=item["digest"],
                    media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
                    local_path=Path(item["local_path"]),
                )
                for item in record.get("layers", [])
            )
            _attach_layers(name, layers, console_log=rpaths.console, mutation=mutation)
            mutation.verify_binding()
            guest_ip = _guest_ipv4(name)
            final_instance = _instance_info(name)
            _require_owned_instance(final_instance, mutation.record.run_id, name)
            if final_instance.get("status") != "Running":
                raise LifecycleError(f"Lima instance '{name}' did not remain Running")
            return mutation.write_state(
                "running",
                {**record, "guest_ip": guest_ip, "updated_at": state.utc_now_iso()},
            )
        except BaseException as exc:
            if start_attempted:
                try:
                    cleanup_instance = _instance_info_or_none(name)
                    if cleanup_instance is not None:
                        _require_owned_instance(cleanup_instance, mutation.record.run_id, name)
                        if _instance_runtime_status(cleanup_instance) != "stopped":
                            _require_success(
                                _run_command(["limactl", "stop", "--force", name], timeout_seconds=60),
                                "rollback start",
                            )
                except BaseException:
                    pass
            if not legacy:
                try:
                    record = mutation.mutable_state()
                    mutation.write_state(
                        "failed",
                        {**record, "error": str(exc), "updated_at": state.utc_now_iso()},
                    )
                except BaseException:
                    pass
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, (LifecycleError, StateError, ArtifactValidationError)):
                raise
            raise LifecycleError(f"Lima start failed: {exc}") from exc


def rm(
    name: str,
    *,
    volumes: bool = False,
    roots: StatePaths | None = None,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    with state.locked_existing_run(roots, name, expected=_expected_record) as mutation:
        if (
            mutation.record.dispatch_key.runtime_kind is not RuntimeKind.CLOUD_IMAGE
            or mutation.record.dispatch_key.backend is not RuntimeBackend.LIMA_VZ
        ):
            raise StateError("run is not managed by the Lima runtime")
        record = mutation.mutable_state()
        instance = _instance_info_or_none(name)
        if instance is not None:
            _require_owned_instance(instance, mutation.record.run_id, name)
            mutation.verify_binding()
            _require_success(_run_command(["limactl", "delete", "--force", name], timeout_seconds=60), "delete")
            remaining = _instance_info_or_none(name)
            if remaining is not None:
                raise LifecycleError(f"Lima instance '{name}' was not deleted")
        if not volumes:
            if instance is None and record.get("status") == "removed":
                return record
            return mutation.write_state("removed", {**record, "updated_at": state.utc_now_iso()})
        result = {**record, "status": "removed", "updated_at": state.utc_now_iso()}
        mutation.delete_run_tree()
        return result


def _append_console(
    console_log: Path,
    result: subprocess.CompletedProcess[str],
    *,
    mutation: state.ExistingRunMutation | None = None,
) -> None:
    content = (result.stdout or "") + (result.stderr or "")
    if mutation is not None:
        mutation.append_file("console.log", content.encode("utf-8"))
        return
    with console_log.open("a", encoding="utf-8") as output:
        output.write(content)


def _guest_command(
    name: str,
    argv: list[str],
    *,
    console_log: Path,
    action: str,
    timeout_seconds: float = _TIMEOUT_SECONDS,
    mutation: state.ExistingRunMutation | None = None,
) -> None:
    if mutation is not None:
        mutation.verify_binding()
    result = _run_command(["limactl", "shell", name, *argv], timeout_seconds=timeout_seconds)
    _append_console(console_log, result, mutation=mutation)
    _require_success(result, action)


def _copy_to_guest(
    name: str,
    sources: list[Path],
    target_dir: str,
    *,
    console_log: Path,
    mutation: state.ExistingRunMutation | None = None,
) -> None:
    if mutation is not None:
        mutation.verify_binding()
    result = _run_command(
        ["limactl", "copy", "--backend=scp", *(str(source) for source in sources), f"{name}:{target_dir}"],
        timeout_seconds=_TIMEOUT_SECONDS,
    )
    _append_console(console_log, result, mutation=mutation)
    _require_success(result, "copy files to guest")


def _copy_from_guest(
    name: str,
    source: str,
    target: Path,
    *,
    console_log: Path,
    mutation: state.ExistingRunMutation | None = None,
) -> None:
    if mutation is not None:
        mutation.verify_binding()
    result = _run_command(
        ["limactl", "copy", "--backend=scp", f"{name}:{source}", str(target)],
        timeout_seconds=_TIMEOUT_SECONDS,
    )
    _append_console(console_log, result, mutation=mutation)
    _require_success(result, "retrieve guest file")


def _attach_layers(
    name: str,
    layers: tuple[LayerRef, ...],
    *,
    console_log: Path,
    mutation: state.ExistingRunMutation | None = None,
) -> None:
    _guest_command(
        name,
        ["sudo", "install", "-d", "-m", "0700", "/mnt/palimpsest", "/opt/layers/upper", "/opt/layers/work"],
        console_log=console_log,
        action="prepare layer mounts",
        mutation=mutation,
    )
    _guest_command(
        name,
        ["sudo", "install", "-d", "-m", "0755", "/opt/layers", "/opt/layers/merged"],
        console_log=console_log,
        action="prepare merged layer mount",
        mutation=mutation,
    )
    if not layers:
        return
    guest_dir = "/tmp/palimpsest-layers/"
    _guest_command(
        name,
        ["mkdir", "-p", guest_dir],
        console_log=console_log,
        action="prepare layer input",
        mutation=mutation,
    )
    _copy_to_guest(
        name,
        [layer.local_path for layer in layers],
        guest_dir,
        console_log=console_log,
        mutation=mutation,
    )
    mounts: list[str] = []
    for index, layer in enumerate(layers):
        mount_path = f"/mnt/palimpsest/lower{index}"
        mounts.append(mount_path)
        _guest_command(
            name,
            ["sudo", "install", "-d", "-m", "0700", mount_path],
            console_log=console_log,
            action="prepare layer mount",
            mutation=mutation,
        )
        _guest_command(
            name,
            ["sudo", "mount", "-t", "squashfs", "-o", "loop,ro", f"{guest_dir}{layer.local_path.name}", mount_path],
            console_log=console_log,
            action="mount layer",
            mutation=mutation,
        )
    _guest_command(
        name,
        [
            "sudo",
            "mount",
            "-t",
            "overlay",
            "overlay",
            "-o",
            f"lowerdir={':'.join(reversed(mounts))},upperdir=/opt/layers/upper,workdir=/opt/layers/work",
            "/opt/layers/merged",
        ],
        console_log=console_log,
        action="activate layers",
        mutation=mutation,
    )

    _guest_command(
        name,
        ["sudo", "chmod", "0755", "/opt/layers/merged"],
        console_log=console_log,
        action="make merged layers readable",
        mutation=mutation,
    )


def _read_build_result(result_path: Path) -> str:
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("Lima builder did not produce a valid result record") from exc
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise BuildError("Lima builder reported a failed build")
    digest = result.get("sha256")
    size = result.get("size")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise BuildError("Lima builder result is missing a valid SHA-256 digest")
    if not isinstance(size, int) or size < 4:
        raise BuildError("Lima builder result is missing a valid output size")
    return f"sha256:{digest}"


def build_layer(spec: BuildSpec, *, roots: StatePaths | None = None) -> dict[str, Any]:
    """Build the standard verified SquashFS artifact contract in a disposable VZ guest."""
    if not available():
        raise BuildError("native Lima builds require macOS on Apple Silicon")
    if spec.base.arch != "aarch64":
        raise ArtifactValidationError("native macOS Lima builds require an aarch64 Ubuntu cloud image")

    from .build import _finalize_build_output, create_build_record, parse_palimpsestfile, verify_build_integrity
    from .digest import require_digest
    from .state import read_tag_record, validate_tag

    roots = roots or state.init_roots()
    validate_tag(spec.output_name)
    recipe = parse_palimpsestfile(spec.recipe)
    parent_digests = [layer.digest for layer in spec.parent_layers]
    verify_build_integrity(recipe, cli_base=spec.base.digest, cli_layers=parent_digests)
    try:
        require_file_digest(spec.base.local_path, spec.base.digest)
        for layer in spec.parent_layers:
            require_file_digest(layer.local_path, layer.digest)
    except Exception as exc:
        raise ArtifactValidationError(f"build input digest mismatch: {exc}") from exc

    existing_tag = (
        read_tag_record(roots, spec.output_name) if state.tag_path(roots, spec.output_name).is_file() else None
    )
    build_id = f"b-{uuid.uuid4().hex[:12]}"
    build_dir = roots.builds / build_id
    build_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    record_data = create_build_record(
        build_id=build_id,
        base_digest=spec.base.digest,
        parent_digests=parent_digests,
        recipe_sha256=recipe.recipe_sha256,
        network=spec.network,
        output_tag=spec.output_name,
        status="running",
    )
    state.atomic_write_json(build_dir / "record.json", record_data)
    console_log = build_dir / "console.log"
    console_log.write_text("", encoding="utf-8")
    stage_dir = build_dir / "guest-input"
    stage_dir.mkdir(mode=0o700)
    worker_path = stage_dir / "palimpsest-builder"
    job_path = stage_dir / "build-job.json"
    worker_result = stage_dir / "result.json"
    tmp_host_out = build_dir / "output.squashfs"
    builder_name = f"builder-{build_id}"
    builder_started = False
    mounted_paths: list[str] = []

    try:
        from . import cloudinit

        worker_path.write_text(cloudinit._BUILD_WORKER_SOURCE, encoding="utf-8")
        worker_path.chmod(0o700)
        job_path.write_text(
            json.dumps(
                {
                    "network": spec.network,
                    "parent_mounts": [f"/mnt/palimpsest/lower{index}" for index in range(len(spec.parent_layers))],
                    "runs": [
                        {"line": run.line, "command": run.command, "env": run.env, "workdir": run.workdir}
                        for run in recipe.runs
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        job_path.chmod(0o600)
        layer_inputs: list[Path] = []
        for index, layer in enumerate(spec.parent_layers):
            layer_input = stage_dir / f"layer-{index}.squashfs"
            shutil.copyfile(layer.local_path, layer_input)
            layer_inputs.append(layer_input)

        run(
            RunSpec(
                name=builder_name,
                stack=StackRef(base=spec.base, layers=()),
                memory_mib=4096,
                vcpus=2,
                network="default",
            ),
            roots=roots,
        )
        builder_started = True
        guest_dir = "/tmp/palimpsest-build/"
        _guest_command(
            builder_name, ["mkdir", "-p", guest_dir], console_log=console_log, action="prepare builder input"
        )
        _copy_to_guest(builder_name, [worker_path, job_path, *layer_inputs], guest_dir, console_log=console_log)
        _guest_command(
            builder_name,
            ["sudo", "install", "-d", "-m", "0700", "/etc/palimpsest", "/mnt/palimpsest", "/usr/local/libexec"],
            console_log=console_log,
            action="prepare builder directories",
        )
        _guest_command(
            builder_name,
            [
                "sudo",
                "install",
                "-m",
                "0755",
                f"{guest_dir}palimpsest-builder",
                "/usr/local/libexec/palimpsest-builder",
            ],
            console_log=console_log,
            action="install fixed builder",
        )
        _guest_command(
            builder_name,
            ["sudo", "install", "-m", "0600", f"{guest_dir}build-job.json", "/etc/palimpsest/build-job.json"],
            console_log=console_log,
            action="install build job",
        )
        for index in range(len(layer_inputs)):
            mount_path = f"/mnt/palimpsest/lower{index}"
            mounted_paths.append(mount_path)
            _guest_command(
                builder_name,
                ["sudo", "install", "-d", "-m", "0700", mount_path],
                console_log=console_log,
                action="prepare parent layer mount",
            )
            _guest_command(
                builder_name,
                [
                    "sudo",
                    "mount",
                    "-t",
                    "squashfs",
                    "-o",
                    "loop,ro",
                    f"{guest_dir}layer-{index}.squashfs",
                    mount_path,
                ],
                console_log=console_log,
                action="mount parent layer",
            )
        _guest_command(
            builder_name,
            [
                "sudo",
                "env",
                f"PALIMPSEST_BUILD_RESULT={guest_dir}result.json",
                "/usr/local/libexec/palimpsest-builder",
            ],
            console_log=console_log,
            action="execute fixed builder",
            timeout_seconds=_TIMEOUT_SECONDS,
        )
        _guest_command(
            builder_name,
            ["sudo", "install", "-m", "0644", "/tmp/palimpsest-output.squashfs", f"{guest_dir}output.squashfs"],
            console_log=console_log,
            action="stage builder output",
        )
        _copy_from_guest(builder_name, f"{guest_dir}result.json", worker_result, console_log=console_log)
        _copy_from_guest(builder_name, f"{guest_dir}output.squashfs", tmp_host_out, console_log=console_log)
        output_digest = require_digest(_read_build_result(worker_result))
        return _finalize_build_output(
            tmp_host_out,
            spec=spec,
            roots=roots,
            existing_tag=existing_tag,
            build_dir=build_dir,
            record_data=record_data,
            recipe=recipe,
            parent_digests=parent_digests,
            output_digest=output_digest,
        )
    except Exception as exc:
        failed_record = create_build_record(
            build_id=build_id,
            base_digest=spec.base.digest,
            parent_digests=parent_digests,
            recipe_sha256=recipe.recipe_sha256,
            network=spec.network,
            output_tag=spec.output_name,
            created_at=record_data["created_at"],
            finished_at=state.utc_now_iso(),
            status="failed",
        )
        state.atomic_write_json(build_dir / "record.json", failed_record)
        if isinstance(exc, (BuildError, ArtifactValidationError, StateError)):
            raise
        raise BuildError(f"Lima build execution failed: {exc}") from exc
    finally:
        for mount_path in reversed(mounted_paths):
            try:
                result = _run_command(
                    ["limactl", "shell", builder_name, "sudo", "umount", mount_path], timeout_seconds=60
                )
                _append_console(console_log, result)
            except Exception:
                pass
        if builder_started:
            rm(builder_name, roots=roots, volumes=True)
        shutil.rmtree(stage_dir, ignore_errors=True)
        tmp_host_out.unlink(missing_ok=True)
