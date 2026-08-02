"""Native Apple Silicon Ubuntu VM lifecycle through the Lima VZ backend."""

from __future__ import annotations

import ipaddress
import json
import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from . import state
from .digest import require_file_digest
from .errors import ArtifactValidationError, BuildError, LifecycleError, StateError
from .refs import BuildSpec, LayerRef, RunSpec, StackRef
from .state import RunPaths, StatePaths

_BACKEND = "lima-vz"
_CONFIG_NAME = "lima.yaml"
_TIMEOUT_SECONDS = 600


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


def _lima_config(spec: RunSpec) -> str:
    image_uri = spec.stack.base.local_path.resolve().as_uri()
    return (
        'minimumLimaVersion: "2.0.0"\n'
        "vmType: vz\n"
        "arch: aarch64\n"
        f"cpus: {spec.vcpus}\n"
        f'memory: "{spec.memory_mib}MiB"\n'
        'disk: "30GiB"\n'
        "containerd:\n  system: false\n  user: false\n"
        "mounts: []\n"
        "networks:\n  - vzNAT: true\n"
        "hostResolver:\n  enabled: true\n"
        "ssh:\n  localPort: 0\n  forwardAgent: false\n"
        "images:\n"
        f"  - location: {image_uri}\n"
        "    arch: aarch64\n"
    )


def _instance_info(name: str) -> dict[str, Any]:
    result = _run_command(["limactl", "list", "--format", "json"])
    _require_success(result, "list")
    try:
        decoded = json.loads(result.stdout)
        instances = decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        instances = []
        for line in result.stdout.splitlines():
            try:
                instances.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not instances:
            raise LifecycleError("Lima returned invalid instance JSON") from None
    if not all(isinstance(instance, dict) for instance in instances):
        raise LifecycleError("Lima returned an invalid instance list")
    for instance in instances:
        if isinstance(instance, dict) and instance.get("name") == name:
            return instance
    raise LifecycleError(f"Lima instance '{name}' was not found after startup")


def _write_state(rpaths: RunPaths, status: str, data: dict[str, Any]) -> dict[str, Any]:
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
    rpaths = state.run_paths(roots, spec.name)
    if rpaths.owner.exists() or rpaths.root.exists():
        try:
            if state.read_run_state(rpaths).get("status") == "removed":
                raise StateError(
                    f"run name '{spec.name}' is held by a removed run; free it with: palimpsest rm {spec.name} --volumes"
                )
        except StateError:
            raise
        except Exception:
            pass
        raise StateError(f"run name '{spec.name}' already exists")
    rpaths.root.mkdir(parents=True, mode=0o700)
    config_path = rpaths.root / _CONFIG_NAME

    with state.locked(rpaths):
        try:
            owner = state.write_owner_record(rpaths)
            config_path.write_text(_lima_config(spec), encoding="utf-8")
            config_path.chmod(0o600)
            record: dict[str, Any] = {
                "name": spec.name,
                "run_id": owner.run_id,
                "created_at": state.utc_now_iso(),
                "updated_at": state.utc_now_iso(),
                "backend": _BACKEND,
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
                "domain_uuid": None,
                "guest_ip": None,
                "cleanup_flags": {},
            }
            _write_state(rpaths, "creating", record)
            created = _run_command(
                ["limactl", "create", "--tty=false", "--name", spec.name, str(config_path)],
                timeout_seconds=timeout_seconds,
            )
            _require_success(created, "create")
            _write_state(rpaths, "defined", {**record, "updated_at": state.utc_now_iso()})
            started = _run_command(
                ["limactl", "start", "--timeout", f"{int(timeout_seconds)}s", spec.name],
                timeout_seconds=timeout_seconds + 30,
            )
            _require_success(started, "start")
            instance = _instance_info(spec.name)
            if instance.get("status") != "Running":
                raise LifecycleError(f"Lima instance '{spec.name}' did not reach Running state")
            port = instance.get("sshLocalPort")
            config = instance.get("sshConfigFile")
            if not isinstance(port, int) or not 1 <= port <= 65535 or not isinstance(config, str) or not config:
                raise LifecycleError("Lima did not report usable SSH connection data")
            rpaths.console.write_text("", encoding="utf-8")
            _attach_layers(spec.name, spec.stack.layers, console_log=rpaths.console)
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
            )
        except Exception as exc:
            try:
                _write_state(rpaths, "failed", {"name": spec.name, "error": str(exc), "backend": _BACKEND})
            except Exception:
                pass
            _run_command(["limactl", "delete", "--force", spec.name], timeout_seconds=60)
            raise


def shell_command(name: str, *, roots: StatePaths | None = None) -> list[str]:
    roots = roots or state.init_roots()
    if not is_lima_run(state.run_paths(roots, name)):
        raise StateError(f"run '{name}' is not a Lima-managed macOS VM")
    return ["limactl", "shell", name]


def exec_command(name: str, argv: list[str], *, roots: StatePaths | None = None) -> list[str]:
    if not argv:
        raise ArtifactValidationError("exec command must be nonempty")
    roots = roots or state.init_roots()
    if not is_lima_run(state.run_paths(roots, name)):
        raise StateError(f"run '{name}' is not a Lima-managed macOS VM")
    return ["limactl", "shell", name, *argv]


def stop(name: str, *, roots: StatePaths | None = None) -> dict[str, Any]:
    roots = roots or state.init_roots()
    rpaths = state.run_paths(roots, name)
    record = state.read_run_state(rpaths)
    if record.get("backend") != _BACKEND:
        raise StateError(f"run '{name}' is not a Lima-managed macOS VM")
    if record.get("status") == "stopped":
        return record
    _require_success(_run_command(["limactl", "stop", "--force", name], timeout_seconds=60), "stop")
    return _write_state(rpaths, "stopped", {**record, "updated_at": state.utc_now_iso()})


def rm(name: str, *, volumes: bool = False, roots: StatePaths | None = None) -> dict[str, Any]:
    roots = roots or state.init_roots()
    rpaths = state.run_paths(roots, name)
    record = state.read_run_state(rpaths)
    if record.get("backend") != _BACKEND:
        raise StateError(f"run '{name}' is not a Lima-managed macOS VM")
    try:
        _instance_info(name)
    except LifecycleError as exc:
        if "was not found after startup" not in str(exc):
            raise
    else:
        _require_success(_run_command(["limactl", "delete", "--force", name], timeout_seconds=60), "delete")
    if not volumes:
        return _write_state(rpaths, "removed", {**record, "updated_at": state.utc_now_iso()})
    shutil.rmtree(rpaths.root)
    return {**record, "status": "removed"}


def _append_console(console_log: Path, result: subprocess.CompletedProcess[str]) -> None:
    with console_log.open("a", encoding="utf-8") as output:
        if result.stdout:
            output.write(result.stdout)
        if result.stderr:
            output.write(result.stderr)


def _guest_command(
    name: str,
    argv: list[str],
    *,
    console_log: Path,
    action: str,
    timeout_seconds: float = _TIMEOUT_SECONDS,
) -> None:
    result = _run_command(["limactl", "shell", name, *argv], timeout_seconds=timeout_seconds)
    _append_console(console_log, result)
    _require_success(result, action)


def _copy_to_guest(name: str, sources: list[Path], target_dir: str, *, console_log: Path) -> None:
    result = _run_command(
        ["limactl", "copy", "--backend=scp", *(str(source) for source in sources), f"{name}:{target_dir}"],
        timeout_seconds=_TIMEOUT_SECONDS,
    )
    _append_console(console_log, result)
    _require_success(result, "copy files to guest")


def _copy_from_guest(name: str, source: str, target: Path, *, console_log: Path) -> None:
    result = _run_command(
        ["limactl", "copy", "--backend=scp", f"{name}:{source}", str(target)],
        timeout_seconds=_TIMEOUT_SECONDS,
    )
    _append_console(console_log, result)
    _require_success(result, "retrieve guest file")


def _attach_layers(name: str, layers: tuple[LayerRef, ...], *, console_log: Path) -> None:
    _guest_command(
        name,
        ["sudo", "install", "-d", "-m", "0700", "/mnt/palimpsest", "/opt/layers/upper", "/opt/layers/work"],
        console_log=console_log,
        action="prepare layer mounts",
    )
    _guest_command(
        name,
        ["sudo", "install", "-d", "-m", "0755", "/opt/layers", "/opt/layers/merged"],
        console_log=console_log,
        action="prepare merged layer mount",
    )
    if not layers:
        return
    guest_dir = "/tmp/palimpsest-layers/"
    _guest_command(name, ["mkdir", "-p", guest_dir], console_log=console_log, action="prepare layer input")
    _copy_to_guest(name, [layer.local_path for layer in layers], guest_dir, console_log=console_log)
    mounts: list[str] = []
    for index, layer in enumerate(layers):
        mount_path = f"/mnt/palimpsest/lower{index}"
        mounts.append(mount_path)
        _guest_command(
            name,
            ["sudo", "install", "-d", "-m", "0700", mount_path],
            console_log=console_log,
            action="prepare layer mount",
        )
        _guest_command(
            name,
            ["sudo", "mount", "-t", "squashfs", "-o", "loop,ro", f"{guest_dir}{layer.local_path.name}", mount_path],
            console_log=console_log,
            action="mount layer",
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
    )

    _guest_command(
        name,
        ["sudo", "chmod", "0755", "/opt/layers/merged"],
        console_log=console_log,
        action="make merged layers readable",
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
