from __future__ import annotations

import hashlib
import json
import sys

import pytest

from palimpsest_local.benchmark_worker import initialize, status, submit, work
from palimpsest_local.benchmark_workflow import METHODS, run_workflow
from palimpsest_local.errors import ArtifactValidationError


def _config(tmp_path):
    return {
        "schema": "palimpsest.benchmark-workflow.v1",
        "job_id": "measure-one",
        "mode": "local",
        "environment": {"schema": "palimpsest.benchmark-environment.v1"},
        "cases": [
            {
                "method": method,
                "argv": [sys.executable, "-c", "raise SystemExit(3)"],
                "proof_argv": [sys.executable, "-c", "raise SystemExit(9)"],
                "timeout_seconds": 5,
                "cache_state": "cold",
                "preparation_scope": "operator-defined",
            }
            for method in METHODS
        ],
    }


def test_failed_environment_prevents_all_install_build_and_allocation_commands(tmp_path):
    config = _config(tmp_path)
    target = tmp_path / "must-not-exist"
    for case in config["cases"]:
        case["argv"] = [sys.executable, "-c", f"from pathlib import Path; Path({str(target)!r}).touch()"]
    config["before_hooks"] = [{"argv": config["cases"][0]["argv"], "timeout_seconds": 5}]
    config["after_hooks"] = [{"argv": config["cases"][0]["argv"], "timeout_seconds": 5}]
    result = run_workflow(config, tmp_path / "evidence", environment_runner=lambda *_: {"status": "failed"})
    assert result["status"] == "failed"
    assert result["cases"] == []
    assert not target.exists()
    with pytest.raises(FileExistsError):
        run_workflow(config, tmp_path / "evidence", environment_runner=lambda *_: {"status": "success"})


@pytest.mark.parametrize("mode", ["local", "openstack"])
@pytest.mark.parametrize("from_file", [False, True])
def test_workflow_selects_environment_runner_from_authoritative_mode(tmp_path, monkeypatch, mode, from_file):
    from palimpsest_local import benchmark_openstack, benchmark_workflow, environment_benchmark

    config = _config(tmp_path)
    config["mode"] = mode
    environment_config = {"schema": "palimpsest.benchmark-environment.v1", "probe": "mode-selection"}
    if from_file:
        config_file = tmp_path / "environment.json"
        config_file.write_text(json.dumps(environment_config))
        config["environment"] = {"config_file": str(config_file)}
    else:
        config["environment"] = environment_config
    calls = []

    def selected(actual_config, output):
        calls.append((actual_config, output))
        return {"status": "failed"}

    def forbidden(*args, **kwargs):
        pytest.fail("workflow invoked the other mode's environment runner or started a command")

    monkeypatch.setattr(environment_benchmark, "run_environment", selected if mode == "local" else forbidden)
    monkeypatch.setattr(
        benchmark_openstack, "run_openstack_environment", selected if mode == "openstack" else forbidden
    )
    monkeypatch.setattr(benchmark_workflow, "_run", forbidden)
    result = run_workflow(config, tmp_path / "evidence")
    assert calls == [(environment_config, tmp_path / "evidence" / "environment")]
    assert result["mode"] == mode
    assert result["status"] == "failed"
    assert result["cases"] == []


def test_failed_case_is_not_a_timing_and_next_command_is_not_started(tmp_path):
    config = _config(tmp_path)
    next_target = tmp_path / "next"
    config["cases"][1]["argv"] = [sys.executable, "-c", f"from pathlib import Path; Path({str(next_target)!r}).touch()"]
    result = run_workflow(config, tmp_path / "evidence", environment_runner=lambda *_: {"status": "success"})
    assert result["status"] == "failed"
    assert len(result["cases"]) == 1
    assert result["cases"][0]["status"] == "failed"
    assert "elapsed_seconds" not in result["cases"][0]
    assert not next_target.exists()
    assert json.loads((tmp_path / "evidence/direct-install.json").read_text())["returncode"] == 3


def test_wrong_job_proof_cannot_promote_a_successful_command_to_runtime_success(tmp_path):
    config = _config(tmp_path)
    config["cases"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(0)"]
    proof = {"job_id": "another-job", "method": "direct-install", "status": "success"}
    config["cases"][0]["proof_argv"] = [sys.executable, "-c", "print(" + repr(json.dumps(proof)) + ")"]
    result = run_workflow(config, tmp_path / "evidence", environment_runner=lambda *_: {"status": "success"})
    assert result["status"] == "failed"
    assert "elapsed_seconds" not in result["cases"][0]


def test_crash_started_receipt_prevents_queue_replay(tmp_path):
    root = tmp_path / "queue-root"
    initialize(root)
    config = _config(tmp_path)
    submit(root, config)
    receipt = {"job_id": config["job_id"], "phase": "started-no-replay"}
    (root / "state/measure-one.json").write_text(json.dumps(receipt))
    assert work(root, once=True) == 2
    assert status(root, "measure-one") == receipt
    assert list((root / "results").iterdir()) == []
    with pytest.raises(ArtifactValidationError, match="already started"):
        submit(root, config)


def test_workflow_executes_real_outputs_before_accepting_each_method_proof(tmp_path):
    config = _config(tmp_path)
    expected = {}
    for case in config["cases"]:
        method = case["method"]
        target = tmp_path / (method + ".bin")
        payload = (method + "\n").encode() * 17
        digest = hashlib.sha256(payload).hexdigest()
        expected[method] = digest
        case["argv"] = [
            sys.executable,
            "-c",
            f"from pathlib import Path;Path({str(target)!r}).write_bytes({payload!r})",
        ]
        program = (
            "import hashlib,json;from pathlib import Path;"
            f"digest=hashlib.sha256(Path({str(target)!r}).read_bytes()).hexdigest();"
            f"assert digest=={digest!r};"
            f"print(json.dumps({{'status':'success','method':{method!r},'job_id':'measure-one','digest':digest}}))"
        )
        case["proof_argv"] = [sys.executable, "-c", program]
    result = run_workflow(config, tmp_path / "evidence", environment_runner=lambda *_: {"status": "success"})
    assert result["status"] == "success"
    assert {case["method"]: case["proof"]["digest"] for case in result["cases"]} == expected
    assert {case["status"] for case in result["cases"]} == {"success"}
    assert json.loads((tmp_path / "evidence/result.json").read_text())["status"] == "success"
