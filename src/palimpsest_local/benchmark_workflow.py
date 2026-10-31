"""Measured operator-authored workflows with mandatory environment qualification.

No detached magic, credential forwarding or inferred success: commands and proofs
are explicit policy. A fresh evidence directory is an exclusive no-replay fence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import signal
import subprocess
import time
from pathlib import Path

from .errors import ArtifactValidationError
from .state import atomic_write_json

METHODS = ("direct-install", "palimpsest-build", "palimpsest-import", "layer-allocate", "ansible")


def load_config(path: Path) -> dict:
    from .oci_image import strict_json_object

    return strict_json_object(path.expanduser().read_bytes(), "benchmark configuration")


def _argv(value, label: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(s, str) or not s or "\x00" in s for s in value):
        raise ArtifactValidationError(f"benchmark {label} requires an explicit argv")
    return value


def _duration(value, maximum: float = 14400) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= maximum:
        raise ArtifactValidationError("benchmark timeout is outside its bounded range")
    return float(value)


def _run(argv: list[str], root: Path, label: str, timeout: float) -> dict:
    # stdout/stderr are private raw evidence, never included verbatim in reports.
    started, wall = time.monotonic(), time.time()
    with (root / (label + ".stdout")).open("xb") as out, (root / (label + ".stderr")).open("xb") as err:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
    elapsed = time.monotonic() - started
    wall_elapsed = time.time() - wall
    result = {"returncode": code, "elapsed_seconds": elapsed, "wall_elapsed_seconds": wall_elapsed}
    atomic_write_json(root / (label + ".json"), result)
    if abs(elapsed - wall_elapsed) > 15:
        raise ArtifactValidationError("benchmark controller clock disagreement; timing invalid")
    if code:
        raise ArtifactValidationError("benchmark command failed; see private evidence")
    return result


def validate_workflow(config: dict) -> None:
    if config.get("schema") != "palimpsest.benchmark-workflow.v1":
        raise ArtifactValidationError("unsupported benchmark workflow schema")
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", config.get("job_id", "")) is None:
        raise ArtifactValidationError("benchmark job_id must be an explicit bounded identifier")
    if config.get("mode") not in ("local", "openstack"):
        raise ArtifactValidationError("benchmark mode must be local or openstack")
    cases = config.get("cases", [])
    if (
        not isinstance(cases, list)
        or len(cases) != len(METHODS)
        or any(not isinstance(c, dict) for c in cases)
        or {c.get("method") for c in cases} != set(METHODS)
    ):
        raise ArtifactValidationError("benchmark requires all five distinct methods")
    if "environment_context" in config:
        from .benchmark_scenarios import _validate_environment_context

        _validate_environment_context(config["environment_context"])
        if "environment" in config:
            raise ArtifactValidationError(
                "select hourly station context or explicit standalone calibration, never both"
            )
    elif not isinstance(config.get("environment"), dict):
        raise ArtifactValidationError("workflow requires explicit hourly station context or standalone calibration")
    if config.get("controller_hostname") is not None and config["controller_hostname"] != platform.node():
        raise ArtifactValidationError("benchmark request belongs to a different execution controller")
    for case in cases:
        _argv(case.get("argv"), "case")
        _argv(case.get("proof_argv"), "runtime proof")
        _duration(case.get("timeout_seconds"))
        for key in ("cache_state", "preparation_scope"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ArtifactValidationError("every benchmark case requires explicit cache/preparation scope")
    for group in ("setup_hooks", "before_hooks", "after_hooks"):
        hooks = config.get(group, [])
        if not isinstance(hooks, list) or len(hooks) > 16 or any(not isinstance(hook, dict) for hook in hooks):
            raise ArtifactValidationError("benchmark hook lists are bounded explicit objects")
        for hook in hooks:
            _argv(hook.get("argv"), "hook")
            _duration(hook.get("timeout_seconds"))


def run_workflow(config: dict, output: Path, *, environment_runner=None) -> dict:
    validate_workflow(config)
    if environment_runner is None and "environment_context" not in config:
        if config["mode"] == "openstack":
            from .benchmark_openstack import run_openstack_environment

            environment_runner = run_openstack_environment
        else:
            from .environment_benchmark import run_environment

            environment_runner = run_environment

    output = output.expanduser().absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(output, 0o700)
    record = {
        "schema": "palimpsest.benchmark-workflow-result.v1",
        "job_id": config["job_id"],
        "mode": config["mode"],
        "status": "running",
        "controller": platform.node(),
        "started_unix_seconds": time.time(),
        "config_digest": "sha256:" + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "cases": [],
        "issues": [],
    }
    atomic_write_json(output / "result.json", record)
    try:
        for i, hook in enumerate(config.get("setup_hooks", [])):
            _run(hook["argv"], output, f"setup-{i}", hook["timeout_seconds"])
        if "environment_context" in config:
            from .benchmark_scenarios import _environment_context

            record["environment"], sample = _environment_context(
                {
                    "environment_context": config["environment_context"],
                    "project_id": config["environment_context"]["project_id"],
                }
            )
            record["environment_reference"] = sample["sample_reference"]
            if sample["status"] != "success":
                record["issues"].append(
                    {
                        "category": "station-context-partial",
                        "message": "Hourly Internet context is partial; missing vendor means are unavailable, not a workload failure",
                    }
                )
        else:
            environment_config = config["environment"]
            if "config_file" in environment_config:
                environment_config = load_config(Path(environment_config["config_file"]))
            record["environment"] = environment_runner(environment_config, output / "environment")
            if record["environment"].get("status") != "success":
                raise ArtifactValidationError(
                    "explicit standalone disk/network preflight failed; workflow commands were not started"
                )
        for i, hook in enumerate(config.get("before_hooks", [])):
            _run(hook["argv"], output, f"before-{i}", hook["timeout_seconds"])
        for case in config["cases"]:
            result = {key: case[key] for key in ("method", "cache_state", "preparation_scope")}
            result["status"] = "running"
            record["cases"].append(result)
            atomic_write_json(output / "result.json", record)
            label = case["method"]
            try:
                measured = _run(case["argv"], output, label, case["timeout_seconds"])
                _run(case["proof_argv"], output, label + "-proof", min(300, case["timeout_seconds"]))
                # A proof command must return a matching machine-readable assertion,
                # not merely exit0, print a nonempty string, or echo the command.
                proof_path = output / (label + "-proof.stdout")
                if proof_path.stat().st_size > 1024 * 1024:
                    raise ArtifactValidationError("benchmark proof exceeds its bound")
                proof = json.loads(proof_path.read_text())
                if (
                    not isinstance(proof, dict)
                    or proof.get("status") != "success"
                    or proof.get("method") != label
                    or proof.get("job_id") != config["job_id"]
                ):
                    raise ArtifactValidationError("runtime proof does not attest this method and job")
                if config["mode"] == "openstack":
                    requested = proof.get("flavor", {}).get("requested", {})
                    actual = proof.get("flavor", {}).get("actual", {})
                    if not requested.get("id") or not actual:
                        raise ArtifactValidationError("OpenStack proof lacks creation/allocation flavor evidence")
                result.update(status="success", elapsed_seconds=measured["elapsed_seconds"], proof=proof)
                if "flavor" in proof:
                    result["flavor"] = proof["flavor"]
            except (Exception, KeyboardInterrupt) as error:
                result.update(status="failed", issue=type(error).__name__ + ": benchmark case or proof failed")
                record["issues"].append(
                    {
                        "method": label,
                        "category": type(error).__name__,
                        "message": "case/proof failed; private raw evidence retained",
                    }
                )
                # An uncertain allocation or incomplete cleanup must never trigger
                # the next case. No automatic hook or arbitrary command replay.
                raise
            finally:
                atomic_write_json(output / "result.json", record)
        for i, hook in enumerate(config.get("after_hooks", [])):
            _run(hook["argv"], output, f"after-{i}", hook["timeout_seconds"])
        record["status"] = "success"
    except (Exception, KeyboardInterrupt) as error:
        record["status"] = "failed"
        record["issues"].append(
            {"category": type(error).__name__, "message": "workflow stopped; inspect evidence before any restart"}
        )
    finally:
        atomic_write_json(output / "result.json", record)
    from .benchmark_reporting import render_report

    rendered = render_report([record])
    for key, name in (("markdown", "report.md"), ("html", "overview.html"), ("issues_markdown", "issues.md")):
        (output / name).write_text(rendered[key])
    return record
