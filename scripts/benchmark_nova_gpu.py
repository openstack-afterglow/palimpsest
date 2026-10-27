#!/usr/bin/env python3
"""Measure explicit, prepared Nova GPU cases serially and retain boot/data volumes.

The operator must approve the exact project, resource IDs and experiment marker.
Each case supplies real cloud-init and an actual GPU readiness command; this tool
never substitutes a CPU result or fabricates a successful missing measurement.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from palimpsest_local.cloud_cli import _connect  # noqa: E402
from palimpsest_local.errors import PalimpsestError  # noqa: E402
from palimpsest_local.nova_build import NovaController  # noqa: E402
from palimpsest_local.nova_metrics import GUEST_SAMPLER_SOURCE, summarize_runs, summarize_samples  # noqa: E402
from palimpsest_local.state import atomic_write_json  # noqa: E402


def _run(argv, *, timeout=60, check=True, **kwargs):
    return subprocess.run(argv, timeout=timeout, check=check, capture_output=True, text=True, **kwargs)


def _json(path: Path):
    return json.loads(path.read_text())


def _save(path: Path, value):
    atomic_write_json(path, value)


def _private_root(path: Path):
    if path.exists():
        raise PalimpsestError("measurement evidence root already exists; never replay an uncertain experiment")
    path.mkdir(mode=0o700, parents=False)
    return path


def _ssh_prefix(plan, run_root: Path, target: str):
    argv = [
        "ssh",
        "-i",
        os.fspath(Path(plan["ssh"]["identity_file"]).expanduser()),
        "-o",
        "UserKnownHostsFile=" + str(run_root / "known-hosts"),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if plan["ssh"].get("jump"):
        argv += ["-J", plan["ssh"]["jump"]]
    return [*argv, plan["ssh"]["user"] + "@" + target]


def _userdata(case: dict, run_id: str, run_root: Path):
    import yaml

    payload = yaml.safe_load(Path(case["user_data"]).expanduser().read_text())
    if not isinstance(payload, dict):
        raise PalimpsestError("case cloud-init must be a complete operator-authored mapping")
    host = run_root / "host"
    _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host)])
    payload["ssh_keys"] = {
        "ed25519_private": host.read_text(),
        "ed25519_public": host.with_suffix(".pub").read_text().strip(),
    }
    sampler = "/usr/libexec/palimpsest-io-sampler"
    destination = "/var/lib/palimpsest/metrics/" + run_id + ".jsonl"
    source = (
        "from pathlib import Path; import subprocess; "
        f"p=Path({sampler!r}); p.parent.mkdir(parents=True,exist_ok=True); p.write_text({GUEST_SAMPLER_SOURCE!r}); "
        f"subprocess.Popen(['/usr/bin/python3',str(p),{destination!r},'14400'],stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)"
    )
    payload["bootcmd"] = [*payload.get("bootcmd", []), ["/usr/bin/python3", "-c", source]]
    rendered = "#cloud-config\n" + yaml.safe_dump(payload, sort_keys=False)
    if len(rendered.encode()) > 65535:
        raise PalimpsestError("measurement cloud-init exceeds Nova user-data limit")
    private = run_root / ".private-user-data.yaml"
    private.write_text(rendered)
    private.chmod(0o600)
    return rendered, destination


def _check_clock_consistency(record, provider, label, tolerance=15.0):
    """Wall and monotonic elapsed must agree. macOS pauses the monotonic clock during sleep, so a
    host sleep makes every request-relative timing wrong while the guest keeps running."""
    monotonic_elapsed = time.monotonic() - provider["request_monotonic_seconds"]
    wall_elapsed = time.time() - provider["request_unix_seconds"]
    record.setdefault("clock_checks", []).append(
        {"at": label, "monotonic_elapsed": monotonic_elapsed, "wall_elapsed": wall_elapsed}
    )
    if abs(wall_elapsed - monotonic_elapsed) > tolerance:
        for key in ("request_to_first_nvidia_smi_seconds", "request_to_framework_gpu_ready_seconds"):
            record.pop(key, None)
        record["clock_inconsistent"] = True
        raise PalimpsestError(
            f"host wall and monotonic clocks disagree by {wall_elapsed - monotonic_elapsed:.1f}s at {label}; "
            "request-relative timings are invalid (host sleep suspected); the guest proof is kept separately"
        )


def _run_ssh_retrying_transport(argv, *, timeout=30, attempts=4):
    """Retry only SSH transport failure (rc 255). A real remote command failure is never retried."""
    last = None
    for attempt in range(attempts):
        try:
            return _run(argv, timeout=timeout)
        except subprocess.CalledProcessError as error:
            if error.returncode != 255 or attempt == attempts - 1:
                raise
            last = error
        except subprocess.TimeoutExpired:
            if attempt == attempts - 1:
                raise
        time.sleep(2 * (attempt + 1))
    raise last


def _wait_guest(ssh, command, deadline, *, evidence_prefix=None):
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            response = _run([*ssh, shlex.join(command)], check=False, timeout=min(30, remaining))
        except subprocess.TimeoutExpired as error:
            # A not-yet-listening guest may exhaust a probe's transport budget.
            # Keep the experiment's readiness deadline, not the first probe's.
            if evidence_prefix is not None:
                for name, value in (("stdout", error.stdout), ("stderr", error.stderr)):
                    text = value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                    evidence_prefix.with_suffix("." + name).write_text(text)
                _save(evidence_prefix.with_suffix(".status.json"), {"returncode": None, "timed_out": True})
        else:
            if evidence_prefix is not None:
                evidence_prefix.with_suffix(".stdout").write_text(response.stdout)
                evidence_prefix.with_suffix(".stderr").write_text(response.stderr)
                _save(evidence_prefix.with_suffix(".status.json"), {"returncode": response.returncode})
            if response.returncode == 0 and time.monotonic() < deadline:
                return response
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise PalimpsestError("guest readiness deadline expired; no successful timing is reported")


def _delete_owned_server(controller, run_id: str, evidence: dict):
    """Only the recorded new VM is deleted. All Cinder boot/data volumes remain."""
    c = controller.connection
    record = controller.inspect(run_id)
    identity = record["server_id"]
    response = c.compute.get("/servers/" + identity + "/os-volume_attachments", microversion="2.79")
    response.raise_for_status()
    attachments = response.json()["volumeAttachments"]
    if any(item.get("delete_on_termination") is not False for item in attachments):
        raise PalimpsestError("measurement disk retention is not attested; refusing VM deletion")
    evidence["retained_volume_ids"] = [item["volumeId"] for item in attachments]
    controller.delete(run_id)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        response = c.compute.get("/servers/" + identity)
        if response.status_code == 404:
            evidence["cleanup"] = "server-absent-404-all-volumes-retained"
            return
        response.raise_for_status()
        time.sleep(2)
    raise PalimpsestError("owned VM deletion remains uncertain; do not allocate another GPU VM")


def _check_boot_quota(quota, boot_gib):
    if type(boot_gib) is not int or not 10 <= boot_gib <= 16384:
        raise PalimpsestError("measurement requires an explicit valid boot_gib")
    for name, demand in (("gigabytes", boot_gib), ("volumes", 1)):
        limit = getattr(quota, name)
        used = (quota.usage or {}).get(name)
        if limit != -1 and used is not None and used + demand > limit:
            raise PalimpsestError(f"Cinder {name} quota cannot retain the next boot; no old bytes are deleted")


def _measure(plan, case, ordinal, connection, receipts, evidence_root):
    run_id = f"{plan['marker']}-{case['name']}-{ordinal}"
    if len(run_id) > 63 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in run_id):
        raise PalimpsestError("case run ID must be a lowercase bounded identifier")
    run_root = _private_root(evidence_root / run_id)
    controller = NovaController(connection, plan["project_id"], receipts)
    record = {
        "run_id": run_id,
        "framework": case["framework"],
        "method": case["method"],
        "ordinal": ordinal,
        "status": "intent",
        "cpu_fallback": None,
        "retention": "all-boot-and-data-volumes",
    }
    _save(run_root / "result.json", record)
    created = False
    installer = None
    installer_logs = []
    ssh = None
    samples_path = None
    try:
        rendered, samples_path = _userdata(case, run_id, run_root)
        launch = {**plan["launch"], **case.get("launch", {}), "job_id": run_id, "name": "pal-" + run_id}
        quota = connection.block_storage.get_quota_set(plan["project_id"], usage=True)
        _check_boot_quota(quota, launch.get("boot_gib"))
        provider = controller.create(**launch, user_data=rendered)
        created = True
        record.update(
            server_id=provider["server_id"],
            phase="provider-boot",
            request_unix_seconds=provider["request_unix_seconds"],
            flavor={"requested": provider.get("flavor_at_request"), "actual": None},
        )
        _save(run_root / "result.json", record)
        active = controller.wait_active(run_id, timeout_seconds=900)
        record["flavor"]["actual"] = active.get("flavor_at_allocation")
        _save(run_root / "result.json", record)
        addresses = [
            item["addr"]
            for values in active["addresses"].values()
            for item in values
            if item.get("OS-EXT-IPS:type") == "fixed" and item.get("version") == 4
        ]
        if len(addresses) != 1:
            raise PalimpsestError("owned measurement guest lacks one unambiguous private IPv4 address")
        target = addresses[0]
        if plan.get("management_floating_ip_id"):
            fip_started = time.monotonic()
            floating = connection.network.get_ip(plan["management_floating_ip_id"])
            if floating.project_id != plan["project_id"] or floating.port_id is not None:
                raise PalimpsestError("measurement floating IP must be project-owned and unbound")
            ports = [
                port
                for port in connection.network.ports(device_id=provider["server_id"])
                if port.device_id == provider["server_id"] and port.project_id == plan["project_id"]
            ]
            if len(ports) != 1:
                raise PalimpsestError("measurement requires one exact-owned management port")
            connection.network.update_ip(floating, port_id=ports[0].id)
            floating = connection.network.get_ip(floating.id)
            if floating.project_id != plan["project_id"] or floating.port_id != ports[0].id:
                raise PalimpsestError("measurement floating IP binding was not confirmed")
            target = floating.floating_ip_address
            record["management_floating_ip_id"] = floating.id
            record["fip_bound_monotonic_seconds"] = time.monotonic()
            record["fip_binding_seconds"] = record["fip_bound_monotonic_seconds"] - fip_started
        known = run_root / "known-hosts"
        known.write_text(target + " " + (run_root / "host.pub").read_text().strip() + "\n")
        known.chmod(0o600)
        ssh = _ssh_prefix(plan, run_root, target)
        deadline = time.monotonic() + case.get("readiness_timeout_seconds", 3600)
        if case["method"] == "ansible":
            _wait_guest(ssh, ["/usr/bin/id", "-u"], deadline)
            transport = "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=" + shlex.quote(str(known))
            if plan["ssh"].get("jump"):
                transport += " -o ProxyJump=" + shlex.quote(plan["ssh"]["jump"])
            ansible = [
                plan.get("ansible_playbook_executable", "ansible-playbook"),
                "-i",
                target + ",",
                "-u",
                plan["ssh"]["user"],
                "--private-key",
                os.fspath(Path(plan["ssh"]["identity_file"]).expanduser()),
                "--ssh-common-args",
                transport,
                str(Path(case["ansible_playbook"]).expanduser()),
            ]
            installer_logs = [(run_root / "ansible.stdout").open("x"), (run_root / "ansible.stderr").open("x")]
            installer = subprocess.Popen(
                ansible,
                stdin=subprocess.DEVNULL,
                stdout=installer_logs[0],
                stderr=installer_logs[1],
                start_new_session=True,
            )
        # Observe ordinary-user NVIDIA availability while Ansible/cloud-init is
        # still installing the framework. Do not charge later package time to
        # the first nvidia-smi endpoint, or hide framework time in that endpoint.
        smi = _wait_guest(ssh, ["nvidia-smi"], deadline, evidence_prefix=run_root / "nvidia-smi-probe")
        first_smi = time.monotonic()
        record["request_to_first_nvidia_smi_seconds"] = first_smi - provider["request_monotonic_seconds"]
        (run_root / "nvidia-smi.txt").write_text(smi.stdout)
        (run_root / "nvidia-smi.stderr").write_text(smi.stderr)
        first_stamp = _run_ssh_retrying_transport(
            [*ssh, "cat /proc/sys/kernel/random/boot_id; cut -d' ' -f1 /proc/uptime"], timeout=30
        ).stdout.splitlines()
        record["first_nvidia_smi_guest_observation_seconds_after_boot"] = float(first_stamp[1])
        record["first_nvidia_smi_boot_id"] = first_stamp[0]
        _check_clock_consistency(record, provider, "first nvidia-smi")
        if installer is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or installer.wait(timeout=remaining) != 0:
                raise PalimpsestError("actual Ansible installation failed or exceeded its deadline")
        ready = _wait_guest(ssh, case["readiness_argv"], deadline, evidence_prefix=run_root / "framework-proof")
        (run_root / "framework-proof.stdout").write_text(ready.stdout)
        proof = json.loads(ready.stdout)
        if (
            proof.get("cpu_fallback") is not False
            or proof.get("framework") != case["framework"]
            or not proof.get("gpu_uuid")
        ):
            raise PalimpsestError("framework proof did not attest the requested real GPU workload")
        finished = time.monotonic()
        record["request_to_framework_gpu_ready_seconds"] = finished - provider["request_monotonic_seconds"]
        _check_clock_consistency(record, provider, "framework gpu ready")
        stamp = _run_ssh_retrying_transport(
            [*ssh, "cat /proc/sys/kernel/random/boot_id; cut -d' ' -f1 /proc/uptime"], timeout=30
        ).stdout.splitlines()
        uid = int(_run_ssh_retrying_transport([*ssh, "id -u"], timeout=30).stdout.strip())
        gpu_uuid = _run_ssh_retrying_transport(
            [*ssh, "nvidia-smi --query-gpu=uuid --format=csv,noheader"], timeout=30
        ).stdout.strip()
        if (
            uid == 0
            or proof.get("uid") != uid
            or proof.get("boot_id") != stamp[0]
            or stamp[0] != first_stamp[0]
            or proof["gpu_uuid"] != gpu_uuid
        ):
            raise PalimpsestError("GPU proof is stale, root-only or belongs to another boot/device")
        record.update(
            status="success",
            phase="ordinary-user-gpu-ready",
            cpu_fallback=False,
            gpu_uuid=proof["gpu_uuid"],
            boot_id=stamp[0],
            framework_proof=proof,
            guest_ready_seconds_after_boot=float(stamp[1]),
            request_to_first_nvidia_smi_seconds=first_smi - provider["request_monotonic_seconds"],
        )
        # Preserve both endpoints: first driver/tool availability and later real
        # framework GPU readiness, with raw samples for each observation window.
        _run_ssh_retrying_transport([*ssh, "sudo -n touch /run/palimpsest-metrics.stop"], timeout=30)
        samples = _run_ssh_retrying_transport([*ssh, "sudo -n cat " + shlex.quote(samples_path)], timeout=60).stdout
        (run_root / "guest-io.jsonl").write_text(samples)
        parsed = [json.loads(line) for line in samples.splitlines() if line]
        record["io_to_first_nvidia_smi"] = summarize_samples(parsed, end_monotonic=float(first_stamp[1]))
        record["io_to_framework_gpu_ready"] = summarize_samples(parsed, end_monotonic=float(stamp[1]))
    except Exception as error:
        record.update(
            status="failed",
            error_class=type(error).__name__,
            error=str(error) if isinstance(error, PalimpsestError) else "bounded runtime error",
        )
        record["failure_request_elapsed_seconds"] = (
            time.monotonic() - provider["request_monotonic_seconds"] if created else None
        )
        if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
            record["subprocess_returncode"] = getattr(error, "returncode", None)
            for name, value in (("stdout", error.stdout), ("stderr", error.stderr)):
                if value is not None:
                    data = value if isinstance(value, bytes) else value.encode()
                    (run_root / ("failed-subprocess." + name)).write_bytes(data)
    finally:
        if installer is not None and installer.poll() is None:
            os.killpg(installer.pid, signal.SIGTERM)
            try:
                installer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(installer.pid, signal.SIGKILL)
                installer.wait()
        for log in installer_logs:
            log.close()
        if created and record["status"] != "success":
            # A failed framework/installer still has valuable actual I/O and
            # console evidence. Capture it before deleting only the owned VM.
            try:
                controller.inspect(run_id)
                console = connection.compute.get_server_console_output(record["server_id"], length=4000)
                _save(run_root / "provider-console-private.json", {"console": console})
            except Exception as error:
                record["console_unavailable"] = type(error).__name__
            if ssh is not None and samples_path is not None:
                try:
                    sampled = _run(
                        [*ssh, "sudo -n touch /run/palimpsest-metrics.stop; sudo -n cat " + shlex.quote(samples_path)],
                        timeout=60,
                    )
                    (run_root / "guest-io.jsonl").write_text(sampled.stdout)
                    rows = [json.loads(line) for line in sampled.stdout.splitlines() if line]
                    record["io_failed_attempt"] = summarize_samples(rows)
                except Exception as error:
                    record["guest_io_unavailable"] = type(error).__name__
        _save(run_root / "result.json", record)
        if created:
            try:
                _delete_owned_server(controller, run_id, record)
            except Exception as error:
                record.update(cleanup="blocked-do-not-launch-next", cleanup_error_class=type(error).__name__)
            _save(run_root / "result.json", record)
        elif (receipts / (run_id + ".json")).exists():
            record.update(cleanup="blocked-do-not-launch-next", uncertain_create=True)
            _save(run_root / "result.json", record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    plan = _json(args.config.expanduser())
    if (
        plan.get("repetitions") != 5
        or plan.get("max_parallel_instances") != 1
        or plan.get("retain_boot_volumes") is not True
    ):
        parser.error("this qualification requires exactly five repetitions, serial VMs and retained boot volumes")
    cases = plan["cases"]
    expected = {
        (framework, method)
        for framework in ("pytorch", "tensorflow")
        for method in ("cloud-init", "nova-layer", "registry-layer", "ansible")
    }
    if {(case["framework"], case["method"]) for case in cases} != expected or len(cases) != 8:
        parser.error("all eight framework/method cases are required; no narrowed matrix")
    for case in cases:
        if not Path(case["user_data"]).expanduser().is_file() or not case.get("readiness_argv"):
            parser.error("every case needs real prepared cloud-init and an actual GPU assertion command")
        if case["method"] == "ansible" and not Path(case["ansible_playbook"]).expanduser().is_file():
            parser.error("Ansible comparison requires its real pinned installation playbook")
    evidence_root = _private_root(Path(plan["evidence_root"]).expanduser().absolute())
    receipts = evidence_root / "nova-jobs"
    receipts.mkdir(mode=0o700)
    connection = _connect(plan["cloud"])
    controller = NovaController(connection, plan["project_id"], receipts)
    from palimpsest_local.benchmark_openstack import run_openstack_environment

    if not isinstance(plan.get("environment_benchmark"), dict):
        parser.error("fresh API-bound disk/network environment_benchmark configuration is required before GPU timing")
    if any(plan["environment_benchmark"].get(key) != plan.get(key) for key in ("cloud", "project_id")):
        parser.error("environment benchmark must use the same explicit cloud and project as the GPU campaign")
    environment = run_openstack_environment(
        plan["environment_benchmark"], evidence_root / "environment", connection=connection
    )
    if environment.get("status") != "success":
        parser.error("disk/network benchmark failed; no GPU comparison is started")
    release = plan.get("environment_release_argv")
    if not isinstance(release, list) or not release or any(not isinstance(s, str) or not s for s in release):
        parser.error("explicit environment_release_argv is required to release CPU benchmark peers before GPU timing")
    _run(release, timeout=900)
    if any(s.project_id == plan["project_id"] for s in connection.compute.servers()):
        parser.error("benchmark peers or another VM remain; no GPU consumer allocated")
    if controller.project_id != plan["project_id"]:
        parser.error("project mismatch")
    results = []
    stop = False
    # Interleave repetition rounds to avoid comparing one method only at a
    # different time of day. No concurrent boot/installer contaminates samples.
    for ordinal in range(1, 6):
        for case in cases:
            result = _measure(plan, case, ordinal, connection, receipts, evidence_root)
            results.append(result)
            _save(evidence_root / "attempts.json", {"attempts": results})
            print(
                json.dumps(
                    {
                        key: result.get(key)
                        for key in ("run_id", "status", "cleanup", "request_to_first_nvidia_smi_seconds")
                    }
                ),
                flush=True,
            )
            if result.get("cleanup") == "blocked-do-not-launch-next" or "quota" in result.get("error", "").lower():
                stop = True
                break
        if stop:
            break
    summary = {
        case["name"]: summarize_runs(
            [r for r in results if r["framework"] == case["framework"] and r["method"] == case["method"]]
        )
        for case in cases
    }
    _save(
        evidence_root / "summary.json",
        {
            "cases": summary,
            "retention": "all-boot-and-data-volumes",
            "complete": all(s["complete"] for s in summary.values()),
        },
    )
    return 0 if all(s["complete"] for s in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
