from __future__ import annotations

import copy
import json
from datetime import UTC

import pytest

from palimpsest_local.benchmark_scenarios import (
    ResourceBudget,
    plan_scenario,
    render_scenario_report,
    run_scenario,
    scenario_status,
)
from palimpsest_local.errors import ArtifactValidationError


def config(tmp_path):
    key = tmp_path / "identity"
    key.write_text("test-only-key-reference")
    known = tmp_path / "known_hosts"
    known.write_text("")
    public = tmp_path / "identity.pub"
    public.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEHREV7idFv5DSvFnWV3OI/jslhbwJHj7rnMhOXPe3y4 test")
    return {
        "schema": "palimpsest.benchmark-scenario.v1",
        "job_id": "environment-proof",
        "scenario": "torch-cpu",
        "method": "openstack-container",
        "cloud": "fixture",
        "project_id": "project-one",
        "project_name": "SYSTEM",
        "project_domain_name": "dmslab",
        "placement": {
            "image_id": "base-image",
            "flavor_id": "small",
            "network_id": "private-network",
            "security_group_ids": ["proof-sg"],
            "boot_gib": 20,
            "writable_gib": 8,
        },
        "ssh": {
            "user": "ubuntu",
            "identity_file": str(key),
            "known_hosts": str(known),
            "public_key_file": str(public),
            "direct_private": True,
        },
        "images": {"torch-cpu": "docker.io/pytorch/pytorch@sha256:" + "a" * 64},
        "resources": {"max_creates": 2, "max_live": 1, "max_boot_gib": 40},
        "approval": {
            "id": "bounded-authority",
            "project_id": "project-one",
            "user_id": "original-human",
            "allowed_flavor_ids": ["small"],
            "max_vcpus": 2,
            "max_ram_mib": 8192,
            "allowed_scenarios": ["environment", "torch-cpu"],
            "max_creates": 2,
            "max_live": 1,
            "max_boot_gib": 40,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "ledger_root": str(tmp_path / "approval-ledger"),
        },
        "environment_context": {
            "measurement_root": str(tmp_path / "station-measurements"),
            "station_id": "station",
            "project_id": "project-one",
            "data_volume_id": "data-volume",
            "max_age_seconds": 3600,
        },
    }


def test_cumulative_budget_is_shared_between_runs_and_never_refunded(tmp_path):
    first = config(tmp_path)
    a = ResourceBudget(first, tmp_path)
    reservation = a.reserve("baseline", 20)
    a.bind(reservation, "vm-a")
    second = copy.deepcopy(first)
    second["job_id"] = "second-run"
    b = ResourceBudget(second, tmp_path)
    with pytest.raises(ArtifactValidationError, match="budget exhausted"):
        b.reserve("baseline", 20)
    a.release(reservation)
    another = b.reserve("baseline", 20)
    b.bind(another, "vm-b")
    b.release(another)
    with pytest.raises(ArtifactValidationError, match="budget exhausted"):
        a.reserve("another", 20)
    with pytest.raises(ArtifactValidationError, match="role consumed"):
        a.reserve("baseline", 20)


def test_simultaneous_pairs_atomically_hold_shared_grant_capacity_across_outputs(tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    first = config(tmp_path)
    first["resources"].update(max_creates=4, max_live=2, max_boot_gib=80)
    first["approval"].update(max_creates=4, max_live=2, max_boot_gib=80)
    second = copy.deepcopy(first)
    second["job_id"] = "other-cluster-output"
    budgets = [ResourceBudget(first, tmp_path / "first-output"), ResourceBudget(second, tmp_path / "second-output")]
    barrier = Barrier(2)

    def reserve(index):
        barrier.wait()
        try:
            return budgets[index].reserve_pair(["server", "agent"], 20, simultaneous=True)
        except ArtifactValidationError as error:
            assert "live budget exhausted" in str(error)
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        pairs = list(executor.map(reserve, range(2)))
    assert sum(pair is not None for pair in pairs) == 1
    winner = next(index for index, pair in enumerate(pairs) if pair is not None)
    loser = 1 - winner
    with pytest.raises(ArtifactValidationError, match="budget exhausted"):
        budgets[loser].reserve("unrelated-consumer", 20)
    for reservation in pairs[winner].values():
        budgets[winner].activate(reservation, time.monotonic() + 60)
    with pytest.raises(ArtifactValidationError, match="live budget exhausted"):
        budgets[loser].reserve_pair(["server", "agent"], 20, simultaneous=True)
    for reservation in pairs[winner].values():
        budgets[winner].release(reservation)
    remaining = budgets[loser].reserve_pair(["server", "agent"], 20, simultaneous=True)
    for reservation in remaining.values():
        budgets[loser].activate(reservation, time.monotonic() + 60)
        budgets[loser].release(reservation)
    assert len(_scenario_reservations(first)) == 4
    with pytest.raises(ArtifactValidationError, match="cumulative/boot budget exhausted"):
        budgets[winner].reserve_pair(["new-server", "new-agent"], 20, simultaneous=True)


def test_sequential_pair_holds_only_one_live_slot_and_never_refunds_creates(tmp_path):
    import time

    value = config(tmp_path)
    budget = ResourceBudget(value, tmp_path)
    pair = budget.reserve_pair(["cold", "warm"], 20)
    budget.activate(pair["cold"], time.monotonic() + 60)
    with pytest.raises(ArtifactValidationError, match="live budget exhausted"):
        budget.activate(pair["warm"], time.monotonic() + 60)
    budget.release(pair["cold"])
    budget.activate(pair["warm"], time.monotonic() + 60)
    budget.release(pair["warm"])
    with pytest.raises(ArtifactValidationError, match="budget exhausted"):
        budget.reserve("extra", 20)


def test_changed_authority_and_foreign_reservation_are_not_adopted(tmp_path):
    original = config(tmp_path)
    a = ResourceBudget(original, tmp_path)
    reservation = a.reserve("baseline", 20)
    altered = copy.deepcopy(original)
    altered["approval"]["max_creates"] = 4
    with pytest.raises(ArtifactValidationError, match="policy changed"):
        ResourceBudget(altered, tmp_path).reserve("new", 20)
    foreign = copy.deepcopy(original)
    foreign["job_id"] = "different-job"
    with pytest.raises(ArtifactValidationError, match="another run"):
        ResourceBudget(foreign, tmp_path).release(reservation)


def test_reuse_plan_counts_only_consumers_not_a_per_experiment_baseline(tmp_path):
    value = config(tmp_path)
    value.update(method="api-reuse", hub={"url": "https://hub.example.test"})
    value["resources"].update(max_creates=1, max_boot_gib=20)
    with pytest.raises(ArtifactValidationError, match="cannot complete"):
        plan_scenario(value)
    value["resources"].update(max_creates=2, max_boot_gib=40)
    plan = plan_scenario(value)
    assert plan["cumulative_creates"] == 2
    assert plan["max_live"] == 1
    assert plan["cumulative_boot_gib"] == 40


def test_unavailable_station_checkpoint_blocks_allocation_without_running_probes(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_station_measurements as measurements

    monkeypatch.setattr(measurements, "latest_measurement", lambda *args: {"cache": {"usable": False}})

    class Cloud:
        def __init__(self, *args, **kwargs):
            pass

        def create(self, role):
            pytest.fail("no VM allocation before a usable hourly checkpoint")

        def environment(self, *args):
            pytest.fail("workload runs cannot generate Internet measurements")

    output = tmp_path / "evidence"
    result = run_scenario(config(tmp_path), output, cloud_factory=Cloud)
    assert result["status"] == "failed"
    assert result["resources"] == []
    assert "station checkpoint" in result["issues"][-1]["message"]
    with pytest.raises(FileExistsError):
        run_scenario(config(tmp_path), output, cloud_factory=Cloud)


def test_authentication_failure_still_seals_a_readable_failure_record(tmp_path):
    class Unavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("private authorization unavailable")

    output = tmp_path / "evidence"
    result = run_scenario(config(tmp_path), output, cloud_factory=Unavailable)
    assert result["status"] == "failed"
    assert json.loads((output / "result.json").read_text())["phase"] == "held-no-replay"
    assert "RuntimeError" in (output / "issues.md").read_text()
    assert "private authorization unavailable" not in (output / "overview.html").read_text()


def test_report_excludes_failed_timings_and_escapes_untrusted_labels(tmp_path):
    record = {
        "schema": "palimpsest.benchmark-scenario-result.v1",
        "scenario": "<script>alert(1)</script>",
        "method": "api-import",
        "status": "failed",
        "measurements": [
            {
                "status": "failed",
                "repetition": 1,
                "workload_seconds": 123.456,
                "environment": {"schema": "palimpsest.environment-result.v1", "status": "failed"},
            }
        ],
        "issues": [{"category": "failure", "message": "token=private-value"}],
    }
    render_scenario_report(record, tmp_path)
    html = (tmp_path / "overview.html").read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "123.456" not in html
    assert "private-value" not in (tmp_path / "issues.md").read_text()


@pytest.mark.parametrize(
    "origin",
    [
        "http://hub.example",
        "https://user:password@hub.example",
        "https://hub.example/path",
        "https://hub.example?token=x",
    ],
)
def test_authenticated_hub_factory_rejects_unsafe_origins_before_getting_token(origin):
    from types import SimpleNamespace

    from palimpsest_local.benchmark_scenarios import _hub

    def forbidden():
        pytest.fail("original token requested for an invalid authenticated transport")

    connection = SimpleNamespace(session=SimpleNamespace(get_token=forbidden))
    with pytest.raises(ArtifactValidationError, match="HTTPS origin"):
        _hub({"hub": {"url": origin}, "project_id": "project"}, connection)


def test_expired_queued_scenario_becomes_durable_hold_without_cloud_effects(tmp_path):
    from palimpsest_local.benchmark_worker import initialize, status, submit, work

    value = config(tmp_path)
    root = tmp_path / "queue"
    initialize(root)
    submit(root, value)
    queued = root / "queue" / (value["job_id"] + ".json")
    value["approval"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    queued.write_text(json.dumps(value))
    assert work(root, once=True) == 1
    held = status(root, value["job_id"])
    assert held["phase"] == "failed-no-replay" and held["cloud_effects"] == 0
    assert work(root, once=True) == 2
    assert not list((root / "results").iterdir())


def test_executor_kernel_lock_distinguishes_live_owner_from_abandoned_running_record(tmp_path):
    from palimpsest_local.benchmark_scenarios import _executor_lock

    with _executor_lock(tmp_path):
        with pytest.raises(ArtifactValidationError, match="still owns"):
            with _executor_lock(tmp_path):
                pytest.fail("concurrent cleanup acquired a live executor")
    with _executor_lock(tmp_path):
        pass


@pytest.mark.parametrize("boot,writable", [(20, 20), (4097, 8), (2048, 1025)])
def test_api_consumer_invalid_placement_is_rejected_before_baseline_allocation(tmp_path, boot, writable):
    value = config(tmp_path)
    value.update(
        scenario="torch-cpu",
        method="api-import",
        images={"torch-cpu": "docker.io/pytorch/pytorch@sha256:" + "a" * 64},
        hub={"url": "https://hub.example"},
    )
    value["placement"].update(boot_gib=boot, writable_gib=writable)
    for section in ("approval", "resources"):
        value[section]["max_boot_gib"] = 16384
    with pytest.raises(ArtifactValidationError, match="API placement"):
        plan_scenario(value)


def test_fixed_script_uses_its_reviewed_checkout_even_with_stale_installed_package(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    value = config(tmp_path)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    stale = tmp_path / "old-install" / "palimpsest_local"
    stale.mkdir(parents=True)
    (stale / "__init__.py").write_text("raise RuntimeError('stale installed package must not execute')\n")
    root = Path(__file__).resolve().parents[2]
    response = subprocess.run(
        [sys.executable, str(root / "scripts/benchmarks/torch_cpu.py"), "plan", "--config", str(path)],
        env={**os.environ, "PYTHONPATH": str(stale.parent)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert response.returncode == 0, response.stderr
    result = json.loads(response.stdout)
    assert result["cloud_effects"] == 0 and result["cumulative_creates"] == 1
    assert result["cumulative_boot_gib"] == 20


def test_failed_api_consumer_keeps_its_Hub_identity_visible_without_server_id(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_scenarios as scenarios

    value = config(tmp_path)
    value.update(
        scenario="torch-cpu",
        method="api-import",
        images={"torch-cpu": "docker.io/pytorch/pytorch@sha256:" + "a" * 64},
        hub={"url": "https://hub.example"},
    )
    monkeypatch.setattr(scenarios, "_environment_context", lambda _: _cached_context())

    class Cloud:
        def __init__(self, config, output, event, budget, observation_only):
            assert observation_only is True
            self.connection = object()

        def create(self, role):
            pytest.fail("API lane cannot allocate an SDK baseline VM")

    class Hub:
        def import_registry_image(self, request):
            return {"id": "image-job", "status": "queued"}

        def get_image_job(self, identity):
            return {"id": "image-job", "status": "complete", "output_digests": ["sha256:" + "a" * 64]}

        def create_instance(self, request):
            self.request = request
            return {"id": "retained-Hub-instance", "server_id": None, "status": "uncertain"}

        def get_instance(self, identity):
            return {"id": identity, "server_id": None, "status": "error", "error_code": "provider_instance_error"}

    hub = Hub()
    monkeypatch.setattr(scenarios, "_hub", lambda *a: hub)
    output = tmp_path / "evidence"
    result = run_scenario(value, output, cloud_factory=Cloud)
    assert result["status"] == "failed"
    retained = result["issues"][-1]["retained_hub_instances"]
    assert retained[0]["instance_id"] == "retained-Hub-instance"
    assert retained[0]["request_id"] == hub.request["request_id"]
    visible = scenario_status(output)["resources"]
    assert any(row["instance_id"] == "retained-Hub-instance" and row["server_id"] is None for row in visible)


def test_auxiliary_budget_is_cumulative_and_rejects_foreign_reservations(tmp_path):
    value = config(tmp_path)
    value["approval"].update(max_floating_ips=1, max_security_groups=1)
    budget = ResourceBudget(value, tmp_path)
    reserved = budget.reserve_aux("floating_ips", "peer-fip")
    budget.bind_aux(reserved, "new-FIP")
    budget.release_aux(reserved)
    other = copy.deepcopy(value)
    other["job_id"] = "another-run"
    with pytest.raises(ArtifactValidationError, match="another run|ownership|identity"):
        ResourceBudget(other, tmp_path).bind_aux(reserved, "new-FIP")
    with pytest.raises(ArtifactValidationError, match="cumulative budget"):
        ResourceBudget(other, tmp_path).reserve_aux("floating_ips", "second-fip")


def test_private_stdin_auth_never_exposes_provider_credential_errors(monkeypatch):
    import io
    import logging
    import sys
    from types import SimpleNamespace

    from palimpsest_local.benchmark_scenarios import connection_from_stdin

    monkeypatch.setattr(logging, "disable", lambda *_: None)
    payload = {
        "auth_type": "v3password",
        "auth_url": "https://identity.example/v3",
        "project_id": "original-project",
        "username": "original-user",
        "user_domain_name": "Accounts",
        "password": "never-persist-this",
        "region_name": "RegionOne",
    }
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))

    def unavailable(**kwargs):
        raise RuntimeError("provider leaked password=never-persist-this")

    monkeypatch.setitem(sys.modules, "openstack", SimpleNamespace(connect=unavailable))
    with pytest.raises(ArtifactValidationError, match="authentication failed") as caught:
        connection_from_stdin()
    assert "never-persist-this" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_private_stdin_auth_rejects_provider_scope_substitution_and_closes_session(monkeypatch):
    import io
    import logging
    import sys
    from types import SimpleNamespace

    from palimpsest_local.benchmark_scenarios import connection_from_stdin

    monkeypatch.setattr(logging, "disable", lambda *_: None)
    payload = {
        "auth_url": "https://identity.example/v3",
        "project_id": "original-project",
        "token": "private-test-token",
        "region_name": "RegionOne",
    }
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    closed = []
    connection = SimpleNamespace(
        current_project_id="another-project",
        session=SimpleNamespace(verify=True),
        authorize=lambda: None,
        close=lambda: closed.append(True),
    )
    monkeypatch.setitem(sys.modules, "openstack", SimpleNamespace(connect=lambda **kw: connection))
    with pytest.raises(ArtifactValidationError, match="authentication failed"):
        connection_from_stdin()
    assert closed == [True]


def test_checkpoint_from_another_station_cannot_start_an_experiment(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_station_measurements as measurements

    monkeypatch.setattr(
        measurements,
        "latest_measurement",
        lambda *args: {"cache": {"usable": True}, "station": {"server_id": "another-station"}},
    )

    class Cloud:
        def __init__(self, *args, **kwargs):
            pass

        def create(self, role):
            pytest.fail("foreign station checkpoint must not admit a VM")

    result = run_scenario(config(tmp_path), tmp_path / "run", cloud_factory=Cloud)
    assert result["status"] == "failed"
    assert result["resources"] == []
    assert result["measurements"] == []


def test_private_auth_ignores_ambient_insecure_cloud_configuration(tmp_path, monkeypatch):
    import io
    import logging
    import sys
    from types import SimpleNamespace

    sdk = pytest.importorskip("openstack.connection")
    from palimpsest_local.benchmark_scenarios import connection_from_stdin

    unsafe = tmp_path / "ambient-clouds.yaml"
    unsafe.write_text(
        "clouds:\n  ambient:\n    insecure: true\n    compute_endpoint_override: http://untrusted.invalid\n"
    )
    monkeypatch.setenv("OS_CLIENT_CONFIG_FILE", str(unsafe))
    monkeypatch.setenv("OS_CLOUD", "ambient")
    monkeypatch.setenv("OS_INSECURE", "true")
    monkeypatch.setenv("OS_COMPUTE_ENDPOINT_OVERRIDE", "http://untrusted.invalid")
    monkeypatch.setattr(logging, "disable", lambda *_: None)
    monkeypatch.setattr(sdk.Connection, "authorize", lambda self: None)
    monkeypatch.setattr(sdk.Connection, "current_project_id", property(lambda self: "original-project"))
    payload = {
        "auth_url": "https://identity.example/v3",
        "project_id": "original-project",
        "token": "private-test-token",
        "region_name": "RegionOne",
    }
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    connection = connection_from_stdin()
    try:
        assert connection.session.verify is True
        assert not connection.config.config.get("insecure")
        assert not connection.config.config.get("compute_endpoint_override")
    finally:
        connection.close()


def test_source_change_during_allocation_prevents_workload_execution(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_scenarios as scenarios

    original = scenarios._source_identity()
    changed = copy.deepcopy(original)
    changed["files"]["benchmark_scenario_cloud.py"] = "f" * 64
    monkeypatch.setattr(scenarios, "_environment_context", lambda _: _cached_context())

    class Cloud:
        def __init__(self, *args, **kwargs):
            pass

        def create(self, role):
            monkeypatch.setattr(scenarios, "_source_identity", lambda: changed)
            return {"server_id": "owned-new-server"}

        def run_guest(self, *args, **kwargs):
            pytest.fail("changed code must not execute a workload")

    result = run_scenario(config(tmp_path), tmp_path / "run", cloud_factory=Cloud)
    assert result["status"] == "failed"
    assert result["issues"][-1]["retained_sdk_servers"] == ["owned-new-server"]
    assert scenario_status(tmp_path / "run")["measurements_completed"] == 0


def _cached_context():
    environment = {
        "schema": "palimpsest.environment-result.v1",
        "status": "success",
        "mode": "openstack",
        "server_id": "station",
        "project_id": "project-one",
        "disk": {"status": "success", "profiles": {}},
        "network": {"status": "success", "internal": {}, "external": {"providers": [], "combined": {}}},
        "host": {},
    }
    sample = {
        "schema": "palimpsest.station-measurement.v1",
        "status": "success",
        "station": {"server_id": "station", "project_id": "project-one", "data_gib": 500},
        "environment": environment,
        "attempt_id": "hourly-cohort",
        "finished_unix_seconds": 1,
        "sample_reference": {"attempt_id": "hourly-cohort"},
    }
    return environment, sample


@pytest.mark.parametrize("bad", [None, "volume", "boot", "stale"])
def test_separate_disk_checkpoint_must_match_the_original_hourly_station(tmp_path, monkeypatch, bad):
    from palimpsest_local import benchmark_scenarios as scenarios
    from palimpsest_local import benchmark_station_disk as disk_module
    from palimpsest_local import benchmark_station_measurements as measurements

    value = config(tmp_path)
    value["environment_context"]["disk_checkpoint_root"] = str(tmp_path / "disk-checkpoints")
    binding = {
        "server_id": "station",
        "project_id": "project-one",
        "boot_volume_id": "boot-volume",
        "boot_id": "same-boot",
        "data_volume_id": "data-volume",
        "filesystem_uuid": "same-filesystem",
    }
    network = {
        "cache": {"usable": True},
        "status": "partial",
        "station": binding,
        "environment": {"disk": {"status": "failed"}},
        "probe_source_sha256": "a" * 64,
        "sample_reference": {"attempt_id": "original-hour"},
    }
    disk = {
        "schema": "palimpsest.station-disk.v1",
        "cache": {"usable": True},
        "station": dict(binding),
        "disk": {"status": "success"},
        "probe_source_sha256": "a" * 64,
        "raw_logs_complete": True,
        "started_unix_seconds": 100,
        "finished_unix_seconds": 200,
    }
    if bad == "volume":
        disk["station"]["data_volume_id"] = "foreign-volume"
    elif bad == "boot":
        disk["station"]["boot_id"] = "rebooted"
    elif bad == "stale":
        disk["cache"]["usable"] = False
    monkeypatch.setattr(measurements, "latest_measurement", lambda *args: network)
    monkeypatch.setattr(disk_module, "latest_disk_sample", lambda *args: disk)
    if bad:
        with pytest.raises(ArtifactValidationError, match="separate disk checkpoint"):
            scenarios._environment_context(value)
    else:
        environment, joined = scenarios._environment_context(value)
        assert environment["disk"]["status"] == "success"
        assert environment["disk_reference"]["started_unix_seconds"] == 100
        assert joined["status"] == "partial"
        assert network["environment"]["disk"]["status"] == "failed"
        assert environment["measurement_reference"]["attempt_id"] == "original-hour"


def _api_scenario_model(tmp_path, monkeypatch, method="api-import"):
    from palimpsest_local import benchmark_scenario_cloud
    from palimpsest_local import benchmark_scenarios as scenarios

    value = config(tmp_path)
    value.update(method=method, hub={"url": "https://hub.example"})
    value["workload"] = {"iterations": 1, "matrix_size": 16}
    output = tmp_path / "run"
    proof = {
        "schema": "palimpsest.workload-result.v1",
        "scenario": "torch-cpu",
        "success": True,
        "device": "cpu",
        "iterations": 1,
        "matrix_size": 16,
        "errors": 0,
        "checked_elements": 256,
        "completed_elements": 256,
        "framework": "torch",
        "framework_version": "fixture",
        "elapsed_seconds": 1,
        "iteration_seconds": [1],
        "checksum": 8192,
        "expected_checksum": 8192,
        "max_abs_error": 0,
        "operations": 8192,
        "operations_per_second": 8192,
        "shape": [16, 16],
        "tensor_device": "cpu",
        "dtype": "torch.float32",
    }

    class Model:
        def __init__(self):
            self.images = []
            self.provider_deletes = 0
            self.delete_journals = []
            self.row = None
            self.removed = False
            self.lose_response = False
            self.fail_verification = False
            self.absence_checks = []
            self.advances = []
            self.lose_advance = False
            self.advance_effect = True
            self.pending_volume_delete = False
            self.volume_deletes = 0
            self.lose_volume_delete = False
            self.volume_delete_effect = True

        def import_registry_image(self, request):
            self.images.append(request)
            return {"id": "image-job", "status": "queued"}

        build_dockerfile = import_registry_image

        def get_image_job(self, identity):
            return {"id": identity, "status": "complete"}

        def create_instance(self, request):
            self.row = {
                "id": "consumer",
                "request_id": request["request_id"],
                "status": "allocating",
                "server_id": None,
                "provider_phase": "volume_ready",
                "server_create_attempted": False,
                "boot_volume_id": "original-boot",
                "layer_digests": ["sha256:" + "a" * 64],
                "result": {"exit_code": 0, "stdout": json.dumps({"palimpsest_workload": proof})},
            }
            self.removed = False
            return dict(self.row)

        def advance_instance(self, identity, *, operation, request_id, boot_volume_id):
            assert identity == self.row["id"]
            assert request_id == self.row["request_id"] and boot_volume_id == self.row["boot_volume_id"]
            if operation == "delete-volume":
                assert self.row["provider_phase"] == "delete_volume_ready"
                assert (output / "r1-consumer1-delete-volume-advance.json").exists()
                self.volume_deletes += 1
                if self.volume_delete_effect:
                    self.removed = True
                    self.row.update(status="deleted", provider_phase="deleted")
                if self.lose_volume_delete:
                    raise RuntimeError("volume delete response lost")
                return dict(self.row)
            assert operation == "create-server"
            assert self.row["server_create_attempted"] is False
            assert (output / ("r1-consumer" + str(len(self.advances) + 1) + "-create-server-advance.json")).exists()
            self.advances.append((identity, operation, request_id, boot_volume_id))
            if self.advance_effect:
                self.row.update(
                    status="ready",
                    server_id="original-server",
                    provider_phase="server_attempted",
                    server_create_attempted=True,
                )
            if self.lose_advance:
                raise RuntimeError("advance response lost")
            return dict(self.row)

        def get_instance(self, identity):
            assert identity == "consumer"
            if self.row.get("provider_phase") == "delete_volume_pending":
                self.row["provider_phase"] = "delete_volume_ready"
            return dict(self.row)

        def delete_instance(self, identity):
            assert identity == "consumer"
            self.provider_deletes += 1
            self.delete_journals.append((output / "r1-consumer1-delete.json").exists())
            self.row["status"] = "deleting"
            if self.pending_volume_delete:
                self.row["provider_phase"] = "delete_volume_pending"
                return dict(self.row)
            if self.lose_response:
                raise RuntimeError("accepted provider deletion response lost")
            self.row["status"] = "deleted"
            self.removed = True

    model = Model()

    class Cloud:
        def __init__(self, *args, **kwargs):
            self.connection = object()

        def resources(self):
            return []

        def verify_external(self, server_id, *, expected_marker):
            assert server_id == "original-server"
            assert expected_marker == {"palimpsest_instance_id": "consumer"}
            if model.fail_verification:
                raise ArtifactValidationError("verification interrupted")
            return {"server_id": server_id}

        def verify_prepared_volume(self, instance_id, boot_volume_id):
            assert instance_id == "consumer" and boot_volume_id == "original-boot"
            assert model.row["server_id"] is None and model.row["server_create_attempted"] is False

        def verify_absent(self, server_id, boot_volume_id, *, server_not_created=False):
            model.absence_checks.append((server_id, boot_volume_id))
            assert (server_id, boot_volume_id) == (None if server_not_created else "original-server", "original-boot")
            if not model.removed:
                raise ArtifactValidationError("original provider resources still present")

    def interrupted_sleep(_):
        raise ArtifactValidationError("observation interrupted")

    monkeypatch.setattr(scenarios, "_hub", lambda *args: model)
    monkeypatch.setattr(scenarios, "_environment_context", lambda _: _cached_context())
    monkeypatch.setattr(scenarios.time, "sleep", interrupted_sleep)
    monkeypatch.setattr(benchmark_scenario_cloud, "ScenarioCloud", Cloud)
    return value, output, model, Cloud


def _scenario_reservations(value):
    from pathlib import Path

    ledger = Path(value["approval"]["ledger_root"]) / (value["approval"]["id"] + ".json")
    return list(json.loads(ledger.read_text())["reservations"].values())


@pytest.mark.parametrize("first_delete", ["normal", "cleanup"])
def test_accepted_consumer_delete_lost_response_is_never_replayed(tmp_path, monkeypatch, first_delete):
    from palimpsest_local.benchmark_scenarios import cleanup_scenario

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    model.lose_response = True
    model.fail_verification = first_delete == "cleanup"
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    if first_delete == "cleanup":
        model.fail_verification = False
        with pytest.raises(RuntimeError, match="response lost"):
            cleanup_scenario(output)
    assert model.provider_deletes == 1
    assert model.delete_journals == [True]
    original = (output / "r1-consumer1-delete.json").read_bytes()
    with pytest.raises(ArtifactValidationError, match="observation interrupted"):
        cleanup_scenario(output)
    assert model.provider_deletes == 1
    assert not _scenario_reservations(value)[0]["released"]
    model.row["status"] = "deleted"
    with pytest.raises(ArtifactValidationError, match="still present"):
        cleanup_scenario(output)
    assert not _scenario_reservations(value)[0]["released"]
    model.removed = True
    assert cleanup_scenario(output)["status"] == "success"
    assert model.provider_deletes == 1
    assert _scenario_reservations(value)[0]["released"]
    assert (output / "r1-consumer1-delete.json").read_bytes() == original


def test_already_deleting_consumer_is_observed_without_provider_delete(tmp_path, monkeypatch):
    from palimpsest_local.benchmark_scenarios import cleanup_scenario

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    model.fail_verification = True
    assert run_scenario(value, output, cloud_factory=cloud)["status"] == "failed"
    model.row["status"] = "deleting"
    with pytest.raises(ArtifactValidationError, match="observation interrupted"):
        cleanup_scenario(output)
    assert model.provider_deletes == 0
    assert not _scenario_reservations(value)[0]["released"]
    original = (output / "r1-consumer1-delete.json").read_bytes()
    model.row["status"] = "ready"
    with pytest.raises(ArtifactValidationError, match="observation interrupted"):
        cleanup_scenario(output)
    assert model.provider_deletes == 0
    model.row["status"] = "deleted"
    model.removed = True
    assert cleanup_scenario(output)["status"] == "success"
    assert model.provider_deletes == 0
    assert _scenario_reservations(value)[0]["released"]
    assert (output / "r1-consumer1-delete.json").read_bytes() == original


@pytest.mark.parametrize("change", ["server_id", "boot_volume_id", "request_id", "id", "reservation", "spec"])
def test_consumer_delete_recovery_rejects_changed_original_identity(tmp_path, monkeypatch, change):
    from palimpsest_local.benchmark_scenarios import cleanup_scenario

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    model.lose_response = True
    assert run_scenario(value, output, cloud_factory=cloud)["status"] == "failed"
    original = (output / "r1-consumer1-delete.json").read_bytes()
    if change in {"reservation", "spec"}:
        suffix = "reservation" if change == "reservation" else "intent"
        path = output / ("r1-consumer1-" + suffix + ".json")
        row = json.loads(path.read_text())
        row["role" if change == "reservation" else "request_id"] = "changed"
        path.write_text(json.dumps(row))
    else:
        model.row[change] = None if change in {"server_id", "boot_volume_id"} else "changed"
    with pytest.raises(ArtifactValidationError, match="identity changed|role changed"):
        cleanup_scenario(output)
    assert model.provider_deletes == 1
    assert not _scenario_reservations(value)[0]["released"]
    assert (output / "r1-consumer1-delete.json").read_bytes() == original


@pytest.mark.parametrize("missing", ["server_id", "boot_volume_id", "request_id"])
def test_consumer_cleanup_unknown_identity_cannot_start_deletion(tmp_path, monkeypatch, missing):
    from palimpsest_local.benchmark_scenarios import cleanup_scenario

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    model.fail_verification = True
    assert run_scenario(value, output, cloud_factory=cloud)["status"] == "failed"
    model.fail_verification = False
    model.row[missing] = None
    with pytest.raises(ArtifactValidationError, match="identity uncertain|identity changed"):
        cleanup_scenario(output)
    assert model.provider_deletes == 0
    assert not _scenario_reservations(value)[0]["released"]
    assert not (output / "r1-consumer1-delete.json").exists()


@pytest.mark.parametrize("method", ["api-import", "api-build"])
@pytest.mark.parametrize("expired", ["approval", "deadline"])
def test_next_image_submission_checks_expiry_before_intent(tmp_path, monkeypatch, method, expired):
    from datetime import datetime

    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch, method)
    value["repetitions"] = 2
    value["deadline_seconds"] = 60
    value["approval"]["expires_at"] = datetime.fromtimestamp(1100, UTC).isoformat()
    clock = [1000.0]
    monkeypatch.setattr(scenarios.time, "time", lambda: clock[0])
    monkeypatch.setattr(scenarios.time, "monotonic", lambda: clock[0] - 1000)
    contexts = []

    def context(_):
        contexts.append(True)
        if len(contexts) == 2:
            clock[0] += 101 if expired == "approval" else 61
        return _cached_context()

    monkeypatch.setattr(scenarios, "_environment_context", context)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert expired in result["issues"][-1]["message"]
    assert len(model.images) == 1
    assert not (output / "r2-image-intent.json").exists()
    assert not (output / "r2-image-accepted.json").exists()
    assert len(_scenario_reservations(value)) == 1
    assert scenarios.cleanup_scenario(output)["status"] == "success"


@pytest.mark.parametrize("method", ["api-import", "api-build"])
def test_image_submission_rechecks_expiry_after_durable_intent(tmp_path, monkeypatch, method):
    from datetime import datetime

    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch, method)
    value["approval"]["expires_at"] = datetime.fromtimestamp(1100, UTC).isoformat()
    clock = [1000.0]
    monkeypatch.setattr(scenarios.time, "time", lambda: clock[0])
    monkeypatch.setattr(scenarios.time, "monotonic", lambda: clock[0] - 1000)
    write = scenarios.atomic_write_json

    def expire_after_intent(path, row):
        write(path, row)
        if path.name == "r1-image-intent.json":
            clock[0] = 1101

    monkeypatch.setattr(scenarios, "atomic_write_json", expire_after_intent)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert "approval expired" in result["issues"][-1]["message"]
    assert (output / "r1-image-intent.json").exists()
    assert not (output / "r1-image-accepted.json").exists()
    assert model.images == []
    assert scenarios.cleanup_scenario(output)["status"] == "success"


def _cold_warm_model(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch, "api-cold-warm")
    model.creates = []
    model.fail_first = False
    model.changed_layers = False
    model.reused_resources = False
    clock = [1000.0]
    monkeypatch.setattr(scenarios.time, "time", lambda: clock[0])
    monkeypatch.setattr(scenarios.time, "monotonic", lambda: clock[0] - 1000)
    create = model.create_instance

    def image(request):
        model.images.append(request)
        clock[0] += 40
        return {"id": "image-job", "status": "queued"}

    def consumer(request):
        model.creates.append(request)
        index = len(model.creates)
        clock[0] += 10 if index == 1 else 5
        create(request)
        suffix = "1" if model.reused_resources else str(index)
        model.row.update(id="consumer-" + suffix, boot_volume_id="boot-" + suffix)
        if index == 1 and model.fail_first:
            model.row["result"]["exit_code"] = 1
        if index == 2 and model.changed_layers:
            model.row["layer_digests"] = ["sha256:" + "b" * 64]
        return dict(model.row)

    def advance(identity, *, operation, request_id, boot_volume_id):
        assert identity == model.row["id"] and operation == "create-server"
        assert request_id == model.row["request_id"] and boot_volume_id == model.row["boot_volume_id"]
        role = "r1-consumer" + str(len(model.creates))
        intent = json.loads((output / (role + "-create-server-advance.json")).read_text())
        assert intent["request_id"] == request_id and intent["boot_volume_id"] == boot_volume_id
        assert model.row["server_id"] is None and model.row["server_create_attempted"] is False
        model.advances.append((identity, operation, request_id, boot_volume_id))
        model.row.update(
            status="ready",
            server_id="server-" + identity.rsplit("-", 1)[1],
            provider_phase="server_attempted",
            server_create_attempted=True,
        )
        return dict(model.row)

    def delete(identity):
        assert identity == model.row["id"]
        model.row["status"] = "deleted"
        model.removed = True

    model.import_registry_image = image
    model.build_dockerfile = image
    model.get_image_job = lambda identity: {
        "id": identity,
        "status": "complete",
        "output_digests": ["sha256:" + "a" * 64],
    }
    model.create_instance = consumer
    model.advance_instance = advance
    model.get_instance = lambda identity: dict(model.row)
    model.delete_instance = delete
    monkeypatch.setattr(cloud, "verify_external", lambda self, identity, **kwargs: {"server_id": identity})
    monkeypatch.setattr(cloud, "verify_absent", lambda self, *args, **kwargs: None)
    return value, output, model, cloud, clock


def test_cold_warm_has_one_image_job_two_fresh_consumers_and_continuous_clocks(tmp_path, monkeypatch):
    value, output, model, cloud, _ = _cold_warm_model(tmp_path, monkeypatch)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "success"
    assert len(model.images) == 1 and len(model.creates) == 2
    assert model.creates[0]["image_job_id"] == "image-job"
    assert model.creates[1]["source_instance_id"] == "consumer-1"
    assert "image_job_id" not in model.creates[1]
    assert model.creates[0]["request_id"] != model.creates[1]["request_id"]
    assert len(model.advances) == 2
    assert [item[2] for item in model.advances] == [item["request_id"] for item in model.creates]
    measurement = result["measurements"][0]
    cold, warm = measurement["consumers"]
    assert [cold["boundary"], warm["boundary"]] == ["cold", "warm"]
    assert [cold["elapsed_seconds"], warm["elapsed_seconds"]] == [50, 5]
    assert measurement["acquisition_build"]["elapsed_seconds"] == 40
    assert cold["timing"]["started_monotonic_seconds"] == measurement["acquisition_build"]["started_monotonic_seconds"]
    assert cold["consumer_timing"]["elapsed_seconds"] == 10
    assert warm["timing"] == warm["consumer_timing"]
    assert cold["layer_digests"] == warm["layer_digests"]
    assert cold["server_id"] != warm["server_id"]
    assert len(_scenario_reservations(value)) == 2
    assert all(row["released"] for row in _scenario_reservations(value))


@pytest.mark.parametrize("failure", ["fail_first", "changed_layers", "reused_resources"])
def test_cold_warm_failure_never_emits_success_pair(tmp_path, monkeypatch, failure):
    value, output, model, cloud, _ = _cold_warm_model(tmp_path, monkeypatch)
    setattr(model, failure, True)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert result["measurements"][0]["status"] == "failed"
    assert len(model.images) == 1
    if failure == "fail_first":
        assert len(model.creates) == 1
        assert result["measurements"][0]["consumers"] == []
    assert "| cold |" not in (output / "report.md").read_text()
    assert "| warm |" not in (output / "report.md").read_text()


def test_pair_cumulatively_reserves_both_slots_before_image_effect(tmp_path, monkeypatch):
    value, output, model, cloud, _ = _cold_warm_model(tmp_path, monkeypatch)
    spent = ResourceBudget(value, output)
    previous = spent.reserve("previous-proof", 20)
    spent.release(previous)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert model.images == [] and model.creates == []
    assert len(_scenario_reservations(value)) == 1


def test_pair_expired_before_create_is_no_call_not_uncertain(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud, clock = _cold_warm_model(tmp_path, monkeypatch)
    write = scenarios.atomic_write_json

    def deadline_after_intent(path, row):
        write(path, row)
        if path.name == "r1-consumer1-intent.json":
            clock[0] += 8000

    monkeypatch.setattr(scenarios, "atomic_write_json", deadline_after_intent)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed" and model.creates == []
    assert result["resources"][-1]["effect"] == "no-call"
    assert not (output / "r1-consumer1-submission.json").exists()
    assert result["issues"][-1]["retained_hub_instances"] == []


def test_pair_lost_create_response_is_uncertain_and_never_replayed(tmp_path, monkeypatch):
    value, output, model, cloud, _ = _cold_warm_model(tmp_path, monkeypatch)

    def lost_response(request):
        model.creates.append(request)
        raise RuntimeError("lost accepted response")

    model.create_instance = lost_response
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed" and len(model.creates) == 1
    assert result["resources"][-1]["effect"] == "uncertain-no-replay"
    assert (output / "r1-consumer1-submission.json").exists()
    assert not (output / "r1-consumer1-accepted.json").exists()
    assert result["issues"][-1]["retained_hub_instances"][0]["request_id"] == model.creates[0]["request_id"]


@pytest.mark.parametrize(
    "start,finish", [((1, 1), (1, 1)), ((2, 2), (1, 1)), ((1, 1), (2, 30)), ((1, 1), (float("nan"), 2))]
)
def test_pair_boundaries_fail_closed_on_invalid_clocks(start, finish):
    from palimpsest_local.benchmark_scenarios import _boundary

    with pytest.raises(ArtifactValidationError, match="clock disagreement"):
        _boundary(start, finish)


@pytest.mark.parametrize("scenario", ["cuda", "torch-cuda", "tensorflow-cuda"])
def test_gpu_pair_forwards_profile_and_tf_cold_includes_external_context(tmp_path, monkeypatch, scenario):
    from palimpsest_local import benchmark_scenario_workloads as workloads
    from palimpsest_local import benchmark_tensorflow_context

    value, output, model, cloud, clock = _cold_warm_model(tmp_path, monkeypatch)
    value.update(scenario=scenario, gpu_profile="qualified-gpu")
    value["images"] = {
        scenario: workloads.TENSORFLOW_GPU_BASE
        if scenario == "tensorflow-cuda"
        else "docker.io/nvidia/cuda:12.6.3-devel-ubuntu24.04@sha256:" + "a" * 64
    }
    value["approval"].update(allow_gpu=True, allowed_scenarios=[scenario], max_context_bytes=1024**3)
    value["hub"].update(image_run_network="none", max_context_bytes=1024**3)
    profile = {
        "image_id": value["placement"]["image_id"],
        "flavor_id": value["placement"]["flavor_id"],
        "project_id": value["project_id"],
        "device_count": 1,
        "driver_version": "580.95.05",
    }
    create = model.create_instance

    def gpu_consumer(request):
        create(request)
        model.row.update(gpu_profile="qualified-gpu", gpu=profile)
        return dict(model.row)

    model.create_instance = gpu_consumer
    monkeypatch.setattr(
        workloads,
        "validate_result",
        lambda *_: {
            "elapsed_seconds": 1,
            "device": "cuda:0",
            "device_count": 1,
            "iterations": 1,
            "framework_version": "2.21.0" if scenario == "tensorflow-cuda" else "fixture",
            "nvidia_smi": [{"driver_version": "580.95.05"}],
        },
    )
    context_calls = []

    def acquire(directory, **kwargs):
        context_calls.append("acquisition")
        clock[0] += 20
        archive = __import__("pathlib").Path(directory) / "context.tar"
        archive.write_bytes(b"fixed-context")
        return archive, {"context_digest": "sha256:" + "c" * 64, "context_bytes": 13}

    def upload(source, **kwargs):
        context_calls.append("upload")
        clock[0] += 3
        return {"digest": "sha256:" + "c" * 64, "size_bytes": 13}

    monkeypatch.setattr(benchmark_tensorflow_context, "acquire_context", acquire)
    model.upload_build_context = upload
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "success"
    assert len(model.images) == 1 and len(model.creates) == 2
    assert all(request["gpu_profile"] == "qualified-gpu" for request in model.creates)
    measurement = result["measurements"][0]
    cold, warm = measurement["consumers"]
    assert warm["elapsed_seconds"] == 5
    assert measurement["acquisition_build"]["elapsed_seconds"] == 40
    if scenario == "tensorflow-cuda":
        assert context_calls == ["acquisition", "upload"]
        assert cold["elapsed_seconds"] == 73
        assert measurement["dependency_acquisition"]["elapsed_seconds"] == 20
        assert measurement["context_upload"]["elapsed_seconds"] == 3
        assert model.images[0]["context_digest"] == "sha256:" + "c" * 64
        assert "dockerfile" in model.images[0]
        assert measurement["image_recipe"]["untouched_tensorflow_image"] is False
    else:
        assert context_calls == [] and cold["elapsed_seconds"] == 50
        assert "reference" in model.images[0]


def test_pair_plan_reserves_six_consumers_for_three_single_repetition_profiles(tmp_path):
    from palimpsest_local.benchmark_scenarios import plan_scenario

    value = config(tmp_path)
    value["method"] = "api-cold-warm"
    assert (
        sum(
            plan_scenario(dict(value, scenario=name), validate=False)["cumulative_creates"]
            for name in ("cuda", "torch-cuda", "tensorflow-cuda")
        )
        == 6
    )


@pytest.mark.parametrize("stage", ["image", "consumer1"])
@pytest.mark.parametrize("expired", ["approval", "deadline"])
def test_post_submission_fsync_expiry_is_durable_no_call(tmp_path, monkeypatch, stage, expired):
    from datetime import datetime

    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud, clock = _cold_warm_model(tmp_path, monkeypatch)
    value["approval"]["expires_at"] = datetime.fromtimestamp(1100 if expired == "approval" else 100000, UTC).isoformat()
    write = scenarios.atomic_write_json
    submission_name = "r1-" + stage + "-submission.json"

    def expire_during_submission(path, row):
        write(path, row)
        if path.name == submission_name and row.get("effect") == "uncertain-no-replay":
            clock[0] = 1101 if expired == "approval" else 9001

    monkeypatch.setattr(scenarios, "atomic_write_json", expire_during_submission)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert model.creates == []
    assert len(model.images) == (0 if stage == "image" else 1)
    assert json.loads((output / submission_name).read_text())["effect"] == "no-call"
    assert result["issues"][-1]["retained_hub_instances"] == []
    if stage == "consumer1":
        assert result["resources"][-1]["effect"] == "no-call"
        assert scenarios.cleanup_scenario(output)["status"] == "success"
        assert next(row for row in _scenario_reservations(value) if row["role"] == "r1-consumer1")["released"]


@pytest.mark.parametrize("method", ["api-import", "api-build", "api-reuse"])
@pytest.mark.parametrize("fence", ["deadline", "source"])
def test_nonpaired_no_call_consumer_releases_unused_live_slot_without_adoption(tmp_path, monkeypatch, method, fence):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud, clock = _cold_warm_model(tmp_path, monkeypatch)
    value["method"] = method
    write = scenarios.atomic_write_json
    source_identity = scenarios._source_identity()
    changed = [False]

    def identity():
        result = copy.deepcopy(source_identity)
        if changed[0]:
            result["files"]["benchmark_scenarios.py"] = "changed-after-intent"
        return result

    def change_after_intent(path, row):
        write(path, row)
        if path.name == "r1-consumer1-intent.json":
            if fence == "deadline":
                clock[0] = 9001
            else:
                changed[0] = True

    monkeypatch.setattr(scenarios, "_source_identity", identity)
    monkeypatch.setattr(scenarios, "atomic_write_json", change_after_intent)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed" and model.creates == [] and len(model.images) == 1
    assert result["resources"][-1]["effect"] == "no-call"
    assert not (output / "r1-consumer1-submission.json").exists()
    assert not _scenario_reservations(value)[0]["released"]
    assert scenarios.cleanup_scenario(output)["status"] == "success"
    assert _scenario_reservations(value)[0]["released"]
    assert len(_scenario_reservations(value)) == 1


def _api_cluster_model(tmp_path, monkeypatch):
    import uuid

    from palimpsest_local import benchmark_cluster_context as context

    value, output, model, cloud, clock = _cold_warm_model(tmp_path, monkeypatch)
    value.update(
        scenario="k3s",
        method="api-cluster",
        cluster_profile="qualified-k3s",
        k3s_version=context.K3S_VERSION,
        repetitions=2,
        cluster={"servers": 1, "agents": 1, "probe_replicas": 2, "timeout_seconds": 60},
    )
    value["images"] = {"k3s": context.K3S_IMAGE, "k3s-probe": context.PAUSE_IMAGE}
    value["resources"].update(max_live=2, max_creates=4, max_boot_gib=80)
    value["approval"].update(
        max_live=2,
        max_creates=4,
        max_boot_gib=80,
        allow_cluster=True,
        allowed_scenarios=["k3s"],
        max_context_bytes=1024**3,
    )
    value["hub"].update(max_context_bytes=1024**3, image_run_network="none")
    model.rows, model.deletions, model.acquisitions, model.uploads = {}, [], [], []
    model.bad_proof, model.changed_layers, model.lost_create, model.lost_delete = False, False, False, False
    model.maximum_live = 0
    model.sdk_creates = []

    def acquire(directory, **kwargs):
        from pathlib import Path

        model.acquisitions.append(directory)
        kwargs["check"](kwargs["deadline"])
        clock[0] += 20
        path = Path(directory) / "context.tar"
        path.write_bytes(b"fixture-tar")
        return path, {
            "context_digest": "sha256:" + "c" * 64,
            "context_bytes": 11,
            "download_bytes": context.AIRGAP_BYTES,
            "guest_acquisition": False,
        }

    def upload(path, **kwargs):
        kwargs["before_send"]()
        assert (output / "r1-context-upload-intent.json").exists()
        model.uploads.append(path.name)
        clock[0] += 5
        return {"digest": "sha256:" + "c" * 64, "size_bytes": 11}

    def proof(server, agent):
        pair = [server, agent]
        return {
            "palimpsest_cluster": {
                "schema": "palimpsest.cluster-result.v1",
                "success": True,
                "k3s_version": context.K3S_VERSION,
                "checks": {"nodes": True, "numerical": True},
                "nodes": [
                    {
                        "name": row["cluster"]["node_name"],
                        "internal_ip": row["private_ip"],
                        "kubelet_version": context.K3S_VERSION,
                        "container_runtime": "containerd://2.2.2",
                    }
                    for row in pair
                ],
                "pods": [
                    {
                        "node": row["cluster"]["node_name"],
                        "name": "probe-" + str(index),
                        "image": context.PAUSE_IMAGE,
                        "ready": True,
                        "restarts": 0,
                        "numerical_sum": 1 if model.bad_proof else 4950,
                    }
                    for index, row in enumerate(pair)
                ],
            }
        }

    def create(spec):
        model.creates.append(dict(spec))
        index = len(model.creates)
        role = "agent" if spec.get("cluster_server_id") else "server"
        if role == "agent":
            server = model.rows[spec["cluster_server_id"]]
            assert server["status"] == "bootstrapping"
            assert spec["source_instance_id"] == server["id"]
        else:
            assert all(row["status"] == "deleted" for row in model.rows.values())
        assert spec["command"] == [] and spec["cluster_profile"] == value["cluster_profile"]
        identity = str(uuid.UUID(int=index))
        row = {
            "id": identity,
            "request_id": spec["request_id"],
            "server_id": None,
            "boot_volume_id": "boot-" + str(index),
            "status": "allocating",
            "provider_status": None,
            "provider_phase": "volume_ready",
            "server_create_attempted": False,
            "private_ip": "10.0.0." + str(index + 1),
            "layer_digests": ["sha256:" + "a" * 64],
            "result": {"exit_code": None, "stdout": ""},
            "cluster": {
                "profile": value["cluster_profile"],
                "role": role,
                "server_instance_id": spec.get("cluster_server_id"),
                "server_address": server["private_ip"] if role == "agent" else None,
                "node_name": "pal-" + uuid.UUID(identity).hex,
            },
        }
        if model.changed_layers and index == 2:
            row["layer_digests"] = ["sha256:" + "b" * 64]
        model.rows[identity] = row
        model.maximum_live = max(model.maximum_live, sum(item["status"] != "deleted" for item in model.rows.values()))
        # The initial admission is Cinder-only, even when its root is ready.
        clock[0] += 10
        if model.lost_create:
            raise RuntimeError("accepted create response lost")
        return copy.deepcopy(row)

    def advance(identity, *, operation, request_id, boot_volume_id):
        row = model.rows[identity]
        assert operation == "create-server" and row["server_create_attempted"] is False
        assert row["request_id"] == request_id and row["boot_volume_id"] == boot_volume_id
        index = int(uuid.UUID(identity))
        role = "r" + str((index + 1) // 2) + "-consumer" + str(2 if index % 2 == 0 else 1)
        assert (output / (role + "-create-server-advance.json")).exists()
        model.advances.append((identity, operation, request_id, boot_volume_id))
        row.update(
            server_id="server-" + str(index),
            provider_status="ACTIVE",
            provider_phase="server_attempted",
            server_create_attempted=True,
            status="ready" if row["cluster"]["role"] == "agent" else "bootstrapping",
        )
        if row["cluster"]["role"] == "agent":
            server = model.rows[row["cluster"]["server_instance_id"]]
            server.update(status="ready", result={"exit_code": 0, "stdout": json.dumps(proof(server, row))})
        return copy.deepcopy(row)

    def delete(identity):
        row = model.rows[identity]
        model.deletions.append(identity)
        if row["cluster"]["role"] == "server":
            assert all(
                item["status"] == "deleted"
                for item in model.rows.values()
                if item["cluster"]["server_instance_id"] == identity
            )
        if model.lost_delete:
            row["status"] = "deleting"
            raise RuntimeError("accepted delete response lost")
        row.update(status="deleted", provider_phase="deleted")
        clock[0] += 1

    def observe(self, identity, **kwargs):
        resource = identity if isinstance(identity, dict) else {"server_id": identity}
        identity = resource["server_id"]
        row = next(item for item in model.rows.values() if item["server_id"] == identity)
        if "volume_ids" in resource:
            assert resource["volume_ids"] == [row["boot_volume_id"]]
        return {"server_id": identity, "status": "ACTIVE", "addresses": [row["private_ip"]]}

    def prepared(self, instance_id, boot_volume_id):
        row = model.rows[instance_id]
        assert row["boot_volume_id"] == boot_volume_id and row["server_id"] is None
        assert row["server_create_attempted"] is False

    def forbidden_create(self, *args, **kwargs):
        model.sdk_creates.append((args, kwargs))
        raise AssertionError("api-cluster must never create through SDK")

    model.create_instance, model.delete_instance = create, delete
    model.advance_instance = advance
    model.get_instance = lambda identity: copy.deepcopy(model.rows[identity])
    model.upload_build_context = upload
    monkeypatch.setattr(context, "acquire_context", acquire)
    monkeypatch.setattr(cloud, "verify_external", observe)
    monkeypatch.setattr(cloud, "verify_prepared_volume", prepared)
    monkeypatch.setattr(cloud, "create", forbidden_create, raising=False)
    return value, output, model, cloud, clock


def test_api_cluster_one_cold_job_two_simultaneous_pairs_exact_prepared_reuse(tmp_path, monkeypatch):
    value, output, model, cloud, _ = _api_cluster_model(tmp_path, monkeypatch)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "success"
    assert model.sdk_creates == []
    assert len(model.images) == len(model.uploads) == len(model.acquisitions) == 1
    assert len(model.creates) == 4 and model.maximum_live == 2
    assert len(model.advances) == 4
    assert [row["boundary"] for row in result["measurements"]] == ["cold", "prepared-reuse"]
    assert result["measurements"][0]["timing"]["elapsed_seconds"] == 85
    assert result["measurements"][1]["timing"]["elapsed_seconds"] == 20
    assert result["measurements"][0]["dependency_acquisition"]["elapsed_seconds"] == 20
    assert result["measurements"][0]["context_upload"]["elapsed_seconds"] == 5
    assert result["measurements"][0]["acquisition_build"]["elapsed_seconds"] == 40
    assert "acquisition_build" not in result["measurements"][1]
    assert model.creates[0]["image_job_id"] == model.creates[2]["image_job_id"] == "image-job"
    assert all("source_instance_id" not in model.creates[index] for index in (0, 2))
    assert len({row["request_id"] for row in model.creates}) == 4
    assert len({row["server_id"] for row in model.rows.values()}) == 4
    assert [model.rows[identity]["cluster"]["role"] for identity in model.deletions] == ["agent", "server"] * 2
    assert len(_scenario_reservations(value)) == 4 and all(row["released"] for row in _scenario_reservations(value))
    assert (output / "r2-image-reuse.json").exists()
    assert not (output / "r2-context-intent.json").exists()


def test_api_cluster_agent_callback_ready_waits_read_only_for_provider_active(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud, clock = _api_cluster_model(tmp_path, monkeypatch)
    value["repetitions"] = 1
    advance, get, observe = model.advance_instance, model.get_instance, cloud.verify_external
    agent_reads, provider_reads = [], []

    def early_callback(identity, **binding):
        row = advance(identity, **binding)
        if row["cluster"]["role"] == "agent":
            model.rows[row["id"]]["provider_status"] = "BUILD"
            row["provider_status"] = "BUILD"
        return row

    def observe_callback(identity):
        row = model.rows[identity]
        if row["cluster"]["role"] == "agent" and row["server_id"] and row["status"] != "deleted":
            if agent_reads:
                row["provider_status"] = "ACTIVE"
            agent_reads.append((row["status"], row["provider_status"]))
        return get(identity)

    def provider(self, resource, **kwargs):
        server_id = resource["server_id"] if isinstance(resource, dict) else resource
        row = next(item for item in model.rows.values() if item["server_id"] == server_id)
        provider_reads.append((row["cluster"]["role"], row["provider_status"]))
        return dict(observe(self, resource, **kwargs), status=row["provider_status"])

    model.advance_instance, model.get_instance = early_callback, observe_callback
    monkeypatch.setattr(cloud, "verify_external", provider)
    monkeypatch.setattr(scenarios.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "success"
    assert agent_reads[:2] == [("ready", "BUILD"), ("ready", "ACTIVE")]
    assert all(status == "ACTIVE" for _, status in provider_reads)
    assert result["measurements"][0]["cluster_timing"]["elapsed_seconds"] == 22
    assert len(model.creates) == 2 and model.sdk_creates == []
    assert len(model.images) == len(model.acquisitions) == len(model.uploads) == 1


@pytest.mark.parametrize("stage", ["server-create", "server-read", "agent-read", "provider-proof", "sealed-proof"])
def test_api_cluster_pair_timeout_covers_create_reads_and_sealed_proof(tmp_path, monkeypatch, stage):
    from palimpsest_local import benchmark_scenario_cloud
    from palimpsest_local import benchmark_scenario_workloads as workloads
    from palimpsest_local.benchmark_scenarios import cleanup_scenario

    value, output, model, cloud, clock = _api_cluster_model(tmp_path, monkeypatch)
    value["repetitions"] = 1
    value["resources"].update(max_creates=2, max_boot_gib=40)
    value["approval"].update(max_creates=2, max_boot_gib=40)
    monkeypatch.setattr(benchmark_scenario_cloud, "ScenarioCloud", cloud)
    create, get, observe, validate = (
        model.create_instance,
        model.get_instance,
        cloud.verify_external,
        workloads.validate_cluster_result,
    )
    provider_reads = []

    def slow_create(spec):
        row = create(spec)
        if stage == "server-create" and not spec.get("cluster_server_id"):
            clock[0] += 60  # The original server call takes 70 seconds against a 60-second pair limit.
        return row

    def slow_read(identity):
        row = get(identity)
        if stage == row["cluster"]["role"] + "-read":
            clock[0] += 60
        return row

    def slow_provider(self, resource, **kwargs):
        provider_reads.append(resource)
        if stage == "provider-proof" and len(provider_reads) == 3:
            clock[0] += 60
        return observe(self, resource, **kwargs)

    def slow_proof(*args):
        proof = validate(*args)
        if stage == "sealed-proof":
            clock[0] += 60
        return proof

    model.create_instance, model.get_instance = slow_create, slow_read
    monkeypatch.setattr(cloud, "verify_external", slow_provider)
    monkeypatch.setattr(workloads, "validate_cluster_result", slow_proof)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert "deadline" in result["issues"][-1]["message"]
    assert len(model.creates) == (1 if stage in {"server-create", "server-read"} else 2)
    assert model.sdk_creates == [] and model.deletions == []
    assert len(model.images) == len(model.acquisitions) == len(model.uploads) == 1
    assert all(row["status"] != "success" for row in result["measurements"])
    assert all(not row["released"] for row in _scenario_reservations(value))
    assert "two-node cluster proof |" not in (output / "report.md").read_text()
    creates = len(model.creates)
    assert cleanup_scenario(output)["status"] == "success"
    assert len(model.creates) == creates
    assert all(row["released"] for row in _scenario_reservations(value))
    fresh = copy.deepcopy(value)
    fresh["job_id"] = "fresh-after-timeout-cleanup"
    with pytest.raises(ArtifactValidationError, match="cumulative/boot budget exhausted"):
        ResourceBudget(fresh, tmp_path / "fresh-output").reserve_pair(["server", "agent"], 20, simultaneous=True)


@pytest.mark.parametrize("extra_seconds, status, creates", [(20, "success", 4), (60, "failed", 3)])
def test_api_cluster_prepared_pair_gets_fresh_bounded_deployment_window(
    tmp_path, monkeypatch, extra_seconds, status, creates
):
    from palimpsest_local import benchmark_scenario_cloud
    from palimpsest_local.benchmark_scenarios import cleanup_scenario

    value, output, model, cloud, clock = _api_cluster_model(tmp_path, monkeypatch)
    monkeypatch.setattr(benchmark_scenario_cloud, "ScenarioCloud", cloud)
    create = model.create_instance

    def prepared_server(spec):
        row = create(spec)
        if len(model.creates) == 3:
            clock[0] += extra_seconds
        return row

    model.create_instance = prepared_server
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == status and len(model.creates) == creates
    assert len(model.images) == len(model.acquisitions) == len(model.uploads) == 1
    assert result["measurements"][0]["status"] == "success"
    assert result["measurements"][0]["timing"]["elapsed_seconds"] == 85
    if status == "success":
        assert result["measurements"][1]["cluster_timing"]["elapsed_seconds"] == 40
    else:
        assert result["measurements"][1]["status"] != "success"
        assert "deadline" in result["issues"][-1]["message"]
        assert cleanup_scenario(output)["status"] == "success"
        assert len(model.creates) == creates
        assert all(row["released"] for row in _scenario_reservations(value))
        fresh = copy.deepcopy(value)
        fresh["job_id"] = "fresh-after-prepared-cleanup"
        with pytest.raises(ArtifactValidationError, match="cumulative/boot budget exhausted"):
            ResourceBudget(fresh, tmp_path / "fresh-output").reserve_pair(["server", "agent"], 20, simultaneous=True)


@pytest.mark.parametrize("options", [None, {"servers": 1, "agents": 1, "probe_replicas": 2}])
def test_api_cluster_executes_with_validated_default_timeout(tmp_path, monkeypatch, options):
    value, output, model, cloud, _ = _api_cluster_model(tmp_path, monkeypatch)
    if options is None:
        value.pop("cluster")
    else:
        value["cluster"] = options
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "success"
    assert len(model.creates) == 4 and len(model.images) == 1
    assert all(row["status"] == "deleted" for row in model.rows.values())


@pytest.mark.parametrize("method", ["api-cluster", "api-cold-warm"])
def test_pair_image_failure_cleanup_releases_uncalled_holds_without_refunding_grant(tmp_path, monkeypatch, method):
    from palimpsest_local import benchmark_scenario_cloud
    from palimpsest_local.benchmark_scenarios import cleanup_scenario

    fixture = _api_cluster_model if method == "api-cluster" else _cold_warm_model
    value, output, model, cloud, _ = fixture(tmp_path, monkeypatch)
    value["repetitions"] = 1
    value["resources"].update(max_creates=2, max_boot_gib=40)
    value["approval"].update(max_creates=2, max_boot_gib=40)
    monkeypatch.setattr(benchmark_scenario_cloud, "ScenarioCloud", cloud)
    model.get_image_job = lambda identity: {"id": identity, "status": "failed", "error_code": "build-failed"}
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed" and model.creates == []
    assert not list(output.glob("r*-consumer*-intent.json"))
    assert all(not row["released"] for row in _scenario_reservations(value))
    assert cleanup_scenario(output)["status"] == "success"
    assert model.creates == []
    assert len(_scenario_reservations(value)) == 2
    assert all(row["released"] for row in _scenario_reservations(value))
    events = [event for path in output.glob("cleanup-*/events.json") for event in json.loads(path.read_text())]
    assert [event["event"] for event in events] == ["consumer-no-call", "consumer-no-call"]
    fresh = copy.deepcopy(value)
    fresh["job_id"] = "fresh-after-image-cleanup"
    with pytest.raises(ArtifactValidationError, match="cumulative/boot budget exhausted"):
        ResourceBudget(fresh, tmp_path / "fresh-output").reserve_pair(["server", "agent"], 20)


@pytest.mark.parametrize("evidence", ["submission", "accepted"])
def test_reservation_only_cleanup_retains_capacity_when_create_evidence_exists(tmp_path, monkeypatch, evidence):
    from palimpsest_local import benchmark_scenario_cloud
    from palimpsest_local.benchmark_scenarios import cleanup_scenario

    value, output, model, cloud, _ = _api_cluster_model(tmp_path, monkeypatch)
    monkeypatch.setattr(benchmark_scenario_cloud, "ScenarioCloud", cloud)
    model.get_image_job = lambda identity: {"id": identity, "status": "failed", "error_code": "build-failed"}
    assert run_scenario(value, output, cloud_factory=cloud)["status"] == "failed"
    (output / ("r1-consumer1-" + evidence + ".json")).write_text(json.dumps({"effect": "uncertain-no-replay"}))
    with pytest.raises(ArtifactValidationError, match="create evidence without original intent; retain reservation"):
        cleanup_scenario(output)
    rows = {row["role"]: row for row in _scenario_reservations(value)}
    assert not rows["r1-consumer1"]["released"]
    assert not rows["r1-consumer2"]["released"]
    assert model.creates == []


@pytest.mark.parametrize("failure", ["bad_proof", "changed_layers", "lost_create", "lost_delete"])
def test_api_cluster_failure_never_qualifies_comparison_or_replays_effects(tmp_path, monkeypatch, failure):
    value, output, model, cloud, _ = _api_cluster_model(tmp_path, monkeypatch)
    setattr(model, failure, True)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert model.sdk_creates == []
    assert all(row["status"] != "success" for row in result["measurements"])
    assert len(model.images) == 1 and len(model.creates) <= 2
    assert len(_scenario_reservations(value)) == 2
    assert all(not row["released"] for row in _scenario_reservations(value))
    assert "two-node cluster proof |" not in (output / "report.md").read_text()
    if failure == "lost_create":
        assert result["resources"][-1]["effect"] == "uncertain-no-replay"
    if failure == "lost_delete":
        assert len(model.deletions) == 1
        assert model.rows[model.deletions[0]]["cluster"]["role"] == "agent"


@pytest.mark.parametrize("stage", ["context-upload", "image", "consumer1", "consumer2"])
def test_api_cluster_rechecks_authority_after_durable_intents_without_effect(tmp_path, monkeypatch, stage):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud, clock = _api_cluster_model(tmp_path, monkeypatch)
    write = scenarios.atomic_write_json

    def expire(path, row):
        write(path, row)
        if path.name == "r1-" + stage + "-intent.json":
            clock[0] += 10000

    monkeypatch.setattr(scenarios, "atomic_write_json", expire)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert len(model.uploads) == (0 if stage == "context-upload" else 1)
    assert len(model.images) == (0 if stage in {"context-upload", "image"} else 1)
    assert len(model.creates) == (1 if stage == "consumer2" else 0)
    assert not model.deletions


def test_api_cluster_budget_prevents_external_context_when_two_live_slots_unavailable(tmp_path, monkeypatch):
    value, output, model, cloud, _ = _api_cluster_model(tmp_path, monkeypatch)
    ResourceBudget(value, output).reserve("preexisting-proof", 20)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert model.acquisitions == model.uploads == model.images == model.creates == []


def test_api_cluster_cleanup_observes_uncertain_agent_delete_without_server_replay(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_scenario_cloud
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud, _ = _api_cluster_model(tmp_path, monkeypatch)
    value["repetitions"] = 1
    model.lost_delete = True
    monkeypatch.setattr(benchmark_scenario_cloud, "ScenarioCloud", cloud)
    assert run_scenario(value, output, cloud_factory=cloud)["status"] == "failed"
    assert len(model.deletions) == 1
    with pytest.raises(ArtifactValidationError, match="observation interrupted"):
        scenarios.cleanup_scenario(output)
    assert len(model.deletions) == 1
    agent = model.rows[model.deletions[0]]
    agent["status"] = "deleted"
    model.lost_delete = False
    assert scenarios.cleanup_scenario(output)["status"] == "success"
    assert [model.rows[identity]["cluster"]["role"] for identity in model.deletions] == ["agent", "server"]
    assert all(row["released"] for row in _scenario_reservations(value))


@pytest.mark.parametrize("stage", ["image", "consumer1", "consumer2"])
def test_api_cluster_submission_fsync_expiry_is_recorded_as_no_call(tmp_path, monkeypatch, stage):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud, clock = _api_cluster_model(tmp_path, monkeypatch)
    write = scenarios.atomic_write_json

    def expire(path, row):
        write(path, row)
        if path.name == "r1-" + stage + "-submission.json" and row.get("effect") == "uncertain-no-replay":
            clock[0] += 10000

    monkeypatch.setattr(scenarios, "atomic_write_json", expire)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert len(model.images) == (0 if stage == "image" else 1)
    assert len(model.creates) == (1 if stage == "consumer2" else 0)
    submission = json.loads((output / ("r1-" + stage + "-submission.json")).read_text())
    assert submission["effect"] == "no-call"


def test_api_cluster_upload_rechecks_authority_after_descriptor_hash_before_http(tmp_path, monkeypatch):
    value, output, model, cloud, clock = _api_cluster_model(tmp_path, monkeypatch)

    def upload(path, **kwargs):
        clock[0] += 10000
        kwargs["before_send"]()
        pytest.fail("expired context descriptor must never reach upload")

    model.upload_build_context = upload
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed" and model.images == model.creates == []
    assert (output / "r1-context-upload-intent.json").exists()
    assert not (output / "r1-context-accepted.json").exists()


def test_api_cluster_reuse_job_order_changes_block_second_pair_without_rebuild(tmp_path, monkeypatch):
    value, output, model, cloud, _ = _api_cluster_model(tmp_path, monkeypatch)
    original = model.get_image_job
    reads = []

    def changed(identity):
        reads.append(identity)
        row = original(identity)
        if len(reads) > 1:
            row["output_digests"] = ["sha256:" + "b" * 64, "sha256:" + "a" * 64]
        return row

    model.get_image_job = changed
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    assert len(model.images) == len(model.acquisitions) == 1 and len(model.creates) == 2
    assert result["measurements"][0]["status"] == "success"
    assert result["measurements"][1]["status"] == "failed"
    assert not (output / "r2-consumer1-intent.json").exists()


def test_api_cluster_waits_for_provider_active_before_observing_unbound_ports(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud, clock = _api_cluster_model(tmp_path, monkeypatch)
    value["repetitions"] = 1
    get, verify = model.get_instance, cloud.verify_external
    waiting, sleeps = [False], []

    def building(identity):
        row = model.rows[identity]
        if row["server_id"] and not waiting[0]:
            waiting[0] = True
            row["provider_status"] = "BUILD"
        return get(identity)

    def active_after_poll(seconds):
        sleeps.append(seconds)
        for row in model.rows.values():
            row["provider_status"] = "ACTIVE"
        clock[0] += seconds

    def bound_only(self, resource, **kwargs):
        identity = resource["server_id"] if isinstance(resource, dict) else resource
        row = next(item for item in model.rows.values() if item["server_id"] == identity)
        assert row["provider_status"] == "ACTIVE", "BUILD has no bound provider ports"
        return verify(self, resource, **kwargs)

    model.get_instance = building
    monkeypatch.setattr(scenarios.time, "sleep", active_after_poll)
    monkeypatch.setattr(cloud, "verify_external", bound_only)
    assert run_scenario(value, output, cloud_factory=cloud)["status"] == "success"
    assert sleeps == [2]
    assert model.sdk_creates == []


@pytest.mark.parametrize("effect", [True, False])
def test_normal_consumer_unknown_advance_response_is_get_only_and_never_rereserved(tmp_path, monkeypatch, effect):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    model.lose_advance = True
    model.advance_effect = effect
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == ("success" if effect else "failed")
    assert len(model.advances) == 1
    reservations = _scenario_reservations(value)
    assert len(reservations) == 1 and reservations[0]["released"] is effect
    if not effect:
        admitted = json.loads((output / "r1-consumer1-accepted.json").read_text())
        spec = json.loads((output / "r1-consumer1-intent.json").read_text())
        observe = scenarios._consumer_observer(
            value,
            output,
            "r1-consumer1",
            spec,
            admitted,
            cloud(),
            ResourceBudget(value, output),
            result["resources"][0]["reservation_id"],
            1e20,
        )
        assert observe()["server_id"] is None
        assert len(model.advances) == 1


@pytest.mark.parametrize("stage", ["before-intent", "after-intent"])
@pytest.mark.parametrize("expired", ["approval", "deadline"])
def test_prepared_consumer_expiry_fences_nova_without_another_reservation(tmp_path, monkeypatch, stage, expired):
    from datetime import datetime

    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    value["approval"]["expires_at"] = datetime.fromtimestamp(1100, UTC).isoformat()
    value["deadline_seconds"] = 60
    clock = [1000.0]
    monkeypatch.setattr(scenarios.time, "time", lambda: clock[0])
    monkeypatch.setattr(scenarios.time, "monotonic", lambda: clock[0] - 1000)
    original_get = model.get_instance
    original_write = scenarios.atomic_write_json

    def expire():
        clock[0] = 1101 if expired == "approval" else 1061

    def observe(identity):
        row = original_get(identity)
        if stage == "before-intent":
            expire()
        return row

    def write(path, row):
        original_write(path, row)
        if stage == "after-intent" and path.name.endswith("-create-server-advance.json"):
            expire()

    model.get_instance = observe
    monkeypatch.setattr(scenarios, "atomic_write_json", write)
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed" and model.advances == []
    assert (output / "r1-consumer1-create-server-advance.json").exists() is (stage == "after-intent")
    assert len(_scenario_reservations(value)) == 1 and not _scenario_reservations(value)[0]["released"]


@pytest.mark.parametrize("lost_reply", [True, False])
def test_generic_cleanup_volume_only_never_creates_and_advances_exact_delete_once(tmp_path, monkeypatch, lost_reply):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    # Slow Cinder has not certified volume_ready during the normal run.
    create = model.create_instance

    def preparing(spec):
        create(spec)
        model.row["provider_phase"] = "volume_preparing"
        return dict(model.row)

    model.create_instance = preparing
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed" and model.advances == []
    model.pending_volume_delete = True
    model.lose_volume_delete = lost_reply
    model.row["provider_phase"] = "volume_ready"
    # Cleanup remains authorized after creation authority expires.
    monkeypatch.setattr(scenarios.time, "time", lambda: 1e12)
    assert scenarios.cleanup_scenario(output)["cloud_creates"] == 0
    assert model.provider_deletes == model.volume_deletes == 1 and model.advances == []
    assert model.absence_checks == [(None, "original-boot")]
    assert _scenario_reservations(value)[0]["released"]
    scenarios.cleanup_scenario(output)
    assert model.provider_deletes == model.volume_deletes == 1


@pytest.mark.parametrize("certificate", [None, True])
def test_generic_cleanup_legacy_or_attempted_null_server_is_hold(tmp_path, monkeypatch, certificate):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    model.advance_effect = False
    run_scenario(value, output, cloud_factory=cloud)
    model.row["server_create_attempted"] = certificate
    with pytest.raises(ArtifactValidationError, match="uncertain"):
        scenarios.cleanup_scenario(output)
    assert model.provider_deletes == model.volume_deletes == 0
    assert not _scenario_reservations(value)[0]["released"]


def test_cli_scenario_run_uses_prepared_root_effect_then_status_is_readonly(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from palimpsest_local import benchmark_cli

    value, output, model, _ = _api_scenario_model(tmp_path, monkeypatch)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(benchmark_cli.signal, "signal", lambda *args: None)
    args = SimpleNamespace(benchmark_mode="scenario", scenario_action="run", config=path, output=output)
    assert benchmark_cli.dispatch(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"
    assert len(model.advances) == model.provider_deletes == 1
    args.scenario_action = "status"
    model.get_instance = lambda *args: pytest.fail("recorded CLI status must not access provider/API lifecycle")
    assert benchmark_cli.dispatch(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"
    assert len(model.advances) == model.provider_deletes == 1


def test_cleanup_unknown_volume_delete_without_effect_holds_without_retry(tmp_path, monkeypatch):
    from palimpsest_local import benchmark_scenarios as scenarios

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    model.advance_effect = False
    run_scenario(value, output, cloud_factory=cloud)
    model.pending_volume_delete = True
    model.volume_delete_effect = False
    model.lose_volume_delete = True
    for _ in range(2):
        with pytest.raises(ArtifactValidationError, match="observation interrupted"):
            scenarios.cleanup_scenario(output)
        assert model.provider_deletes == model.volume_deletes == 1
        assert len(model.advances) == 1
        assert not _scenario_reservations(value)[0]["released"]
    model.row.update(status="deleted", provider_phase="deleted")
    model.removed = True
    assert scenarios.cleanup_scenario(output)["status"] == "success"
    assert model.provider_deletes == model.volume_deletes == 1
    assert _scenario_reservations(value)[0]["released"]


@pytest.mark.parametrize("status", [403, 409, 422, 500])
def test_advance_response_receipts_distinguish_rejection_from_unknown(tmp_path, monkeypatch, status):
    from palimpsest_local import benchmark_scenarios as scenarios
    from palimpsest_local.errors import HubError

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    attempts = []

    def reject(identity, **binding):
        attempts.append((identity, binding))
        error = HubError("provider response")
        error.http_status = status
        raise error

    model.advance_instance = reject
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed"
    receipt = json.loads((output / "r1-consumer1-create-server-advance-result.json").read_text())
    assert receipt == {"outcome": "rejected" if status < 500 else "unknown", "http_status": status}
    assert len(attempts) == 1 and not _scenario_reservations(value)[0]["released"]
    if status < 500:
        assert "advance rejected" in result["issues"][-1]["message"]
    else:
        assert "observation interrupted" in result["issues"][-1]["message"]
    admitted = json.loads((output / "r1-consumer1-accepted.json").read_text())
    spec = json.loads((output / "r1-consumer1-intent.json").read_text())
    observe = scenarios._consumer_observer(
        value,
        output,
        "r1-consumer1",
        spec,
        admitted,
        cloud(),
        ResourceBudget(value, output),
        result["resources"][0]["reservation_id"],
        1e20,
    )
    if status < 500:
        with pytest.raises(ArtifactValidationError, match="explicit disposition"):
            observe()
    else:
        observe()
    assert len(attempts) == 1


def test_first_visible_server_is_bound_before_workload_verification(tmp_path, monkeypatch):
    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    model.fail_verification = True
    result = run_scenario(value, output, cloud_factory=cloud)
    assert result["status"] == "failed" and model.provider_deletes == 0
    reservation = _scenario_reservations(value)[0]
    assert reservation["server_id"] == "original-server" and not reservation["released"]
    receipt = json.loads((output / "r1-consumer1-create-server-advance-result.json").read_text())
    assert receipt["outcome"] == "accepted" and receipt["response"]["server_id"] == "original-server"


@pytest.mark.parametrize("attempted", [False, None, True])
@pytest.mark.parametrize("root_state", ["present", "absent", "none", "error"])
def test_terminal_prepared_root_cleanup_requires_positive_no_nova_certificate(
    tmp_path, monkeypatch, attempted, root_state
):
    from types import SimpleNamespace

    from palimpsest_local import benchmark_scenarios as scenarios
    from palimpsest_local.benchmark_scenario_cloud import ScenarioCloud

    value, output, model, cloud = _api_scenario_model(tmp_path, monkeypatch)
    create = model.create_instance
    provider_reads = []

    def failed_root(spec):
        create(spec)
        model.row.update(
            status="error",
            provider_phase="uncertain",
            server_create_attempted=attempted,
            error_code="provider_instance_error",
        )
        return dict(model.row)

    def get_volume(identity):
        assert identity == "original-boot"
        provider_reads.append(identity)
        if root_state == "none":
            return None
        if root_state == "error":
            error = RuntimeError("Cinder GET unavailable")
            error.status_code = 503
            raise error
        if model.removed or root_state == "absent":
            error = RuntimeError("actual exact Cinder GET404")
            error.status_code = 404
            raise error
        return {
            "id": identity,
            "project_id": value["project_id"],
            "attachments": [],
            "metadata": {"palimpsest_instance_id": "consumer"},
        }

    # Exercise the real exact-volume observation/404 implementation rather than
    # a cleanup stub that simply echoes successful deletion.
    observer = object.__new__(ScenarioCloud)
    observer.config = value
    observer.project_id = value["project_id"]
    observer.connection = SimpleNamespace(block_storage=SimpleNamespace(get_volume=get_volume))
    observer._scope = lambda: None
    monkeypatch.setattr(cloud, "verify_prepared_volume", lambda self, *args: observer.verify_prepared_volume(*args))
    monkeypatch.setattr(cloud, "verify_absent", lambda self, *args, **kwargs: observer.verify_absent(*args, **kwargs))
    model.create_instance = failed_root
    model.pending_volume_delete = True
    if root_state == "absent":

        def settle_missing(identity):
            assert identity == model.row["id"]
            model.provider_deletes += 1  # Official API DELETE intent, no provider deletion.
            model.row.update(status="deleted", provider_phase="deleted")

        model.delete_instance = settle_missing
    assert run_scenario(value, output, cloud_factory=cloud)["status"] == "failed"
    assert model.advances == []
    if attempted is not False:
        with pytest.raises(ArtifactValidationError, match="identity uncertain"):
            scenarios.cleanup_scenario(output)
        assert model.provider_deletes == model.volume_deletes == 0 and provider_reads == []
        assert not _scenario_reservations(value)[0]["released"]
        return
    if root_state in {"none", "error"}:
        with pytest.raises(ArtifactValidationError if root_state == "none" else RuntimeError):
            scenarios.cleanup_scenario(output)
        assert model.provider_deletes == model.volume_deletes == 0
        assert not _scenario_reservations(value)[0]["released"]
        return
    assert scenarios.cleanup_scenario(output)["status"] == "success"
    assert model.provider_deletes == 1 and model.volume_deletes == (0 if root_state == "absent" else 1)
    assert model.advances == []
    assert provider_reads == ["original-boot", "original-boot"]
    observation = json.loads((output / "r1-consumer1-delete-root-observation.json").read_text())
    assert observation.get("volume_already_absent", False) is (root_state == "absent")
    absence = json.loads((output / "r1-consumer1-absence.json").read_text())
    assert absence["server_absent"] == "not_created" and absence["boot_volume_absent"] is True
    assert _scenario_reservations(value)[0]["released"]
