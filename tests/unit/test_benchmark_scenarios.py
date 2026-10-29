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
                "status": "ready",
                "server_id": "original-server",
                "boot_volume_id": "original-boot",
                "layer_digests": ["sha256:" + "a" * 64],
                "result": {"exit_code": 0, "stdout": json.dumps({"palimpsest_workload": proof})},
            }
            self.removed = False
            return dict(self.row)

        def get_instance(self, identity):
            assert identity == "consumer"
            return dict(self.row)

        def delete_instance(self, identity):
            assert identity == "consumer"
            self.provider_deletes += 1
            self.delete_journals.append((output / "r1-consumer1-delete.json").exists())
            self.row["status"] = "deleting"
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

        def verify_absent(self, server_id, boot_volume_id):
            model.absence_checks.append((server_id, boot_volume_id))
            assert (server_id, boot_volume_id) == ("original-server", "original-boot")
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
