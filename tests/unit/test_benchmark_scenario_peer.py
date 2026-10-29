"""Offline peer lifecycle tests with stateful provider models, never live SDK calls."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import benchmark_scenario_peer as peer
from palimpsest_local.benchmark_scenarios import ResourceBudget, _executor_lock
from palimpsest_local.errors import ArtifactValidationError


class Missing(Exception):
    status_code = 404


class Network:
    def __init__(self, output):
        self.output = output
        self.groups = {}
        self.rules = {}
        self.project_ports = []
        self.effects = []
        self.fail_create = False
        self.lose_rule_response = False
        self.deny_delete = False
        self.permission_denied = False

    def create_security_group(self, **attributes):
        receipt = peer._read(self.output / "peer-security-group.json")
        assert receipt["phase"] == "create-intent" and receipt["reservation_id"]
        self.effects.append(("create-group", attributes))
        group = dict(id="sg-fresh", security_group_rules=[], **attributes)
        self.groups[group["id"]] = group
        for version in ("IPv4", "IPv6"):
            rule = dict(
                id="default-" + version,
                security_group_id=group["id"],
                project_id=attributes["project_id"],
                direction="egress",
                ether_type=version,
                protocol=None,
                port_range_min=None,
                port_range_max=None,
                remote_ip_prefix=None,
                remote_group_id=None,
            )
            group["security_group_rules"].append(rule)
            self.rules[rule["id"]] = rule
        if self.fail_create:
            raise TimeoutError("authorization: Bearer do-not-persist")
        return copy.deepcopy(group)

    def get_security_group(self, identity):
        if self.permission_denied:
            raise PermissionError("credential=do-not-persist")
        if identity not in self.groups:
            raise Missing
        return copy.deepcopy(self.groups[identity])

    def security_groups(self, **filters):
        return [copy.deepcopy(g) for g in self.groups.values()]

    def get_security_group_rule(self, identity):
        if identity not in self.rules:
            raise Missing
        return copy.deepcopy(self.rules[identity])

    def delete_security_group_rule(self, identity, *, ignore_missing):
        assert ignore_missing is False
        receipt = peer._read(self.output / "peer-security-group.json")
        originals = receipt["defaults"]
        assert len(originals) == 2 and next(r for r in originals if r["id"] == identity)["phase"] == "delete-intent"
        self.effects.append(("delete-default", identity))
        rule = self.rules.pop(identity)
        self.groups[rule["security_group_id"]]["security_group_rules"].remove(rule)

    def create_security_group_rule(self, **attributes):
        receipt = peer._read(self.output / "peer-security-group.json")
        assert receipt["rules"][-1]["phase"] == "create-intent"
        rule = dict(id="rule-" + str(len(self.rules)), **attributes)
        for field in peer._RULE_FIELDS:
            rule.setdefault(field, None)
        self.rules[rule["id"]] = rule
        self.groups[attributes["security_group_id"]]["security_group_rules"].append(rule)
        self.effects.append(("create-rule", attributes))
        if self.lose_rule_response:
            self.lose_rule_response = False
            raise TimeoutError("password=do-not-persist")
        return copy.deepcopy(rule)

    def ports(self, **filters):
        # Intentionally ignores every provider filter, like the observed Neutron trap.
        return copy.deepcopy(self.project_ports)

    def delete_security_group(self, identity, *, ignore_missing):
        assert ignore_missing is False
        receipt = peer._read(self.output / "peer-security-group.json")
        assert receipt["phase"] == "delete-intent"
        assert not any(identity in p.get("security_group_ids", []) for p in self.project_ports)
        self.effects.append(("delete-group", identity))
        if self.deny_delete:
            raise PermissionError("password=do-not-persist")
        self.groups.pop(identity)


class Cloud:
    def __init__(self, config, output, event, connection=None, budget=None, *, purpose, cleanup_only=False):
        assert purpose == "peer" and config["placement"]["security_group_ids"] == []
        self.config, self.output = copy.deepcopy(config), Path(output)
        self.project_id, self.connection, self.budget = config["project_id"], connection, budget
        self.event = event
        self.connection.cloud_modes.append(cleanup_only)
        self._scope()

    def _scope(self):
        if (
            self.connection.current_project_id != self.project_id
            or self.connection.current_user_id != self.config["approval"]["user_id"]
        ):
            raise ArtifactValidationError("scope mismatch")

    def _remaining(self, requested):
        self._scope()
        return requested

    def create(self, role):
        assert self.config["placement"]["security_group_ids"] == ["sg-fresh"]
        self.connection.creates += 1
        reservation = self.budget.reserve(role, 20)
        self.budget.bind(reservation, "server-fresh")
        fip = self.budget.reserve_aux("floating_ips", "peer-fip")
        self.budget.bind_aux(fip, "fip-fresh")
        known = self.output / "peer-pinned-hosts"
        known.write_text(self.config["job_id"] + "-peer ssh-ed25519 AAAA\n")
        known.chmod(0o600)
        resource = {
            "source": "sdk",
            "role": role,
            "job_id": self.config["job_id"] + "-peer",
            "server_id": "server-fresh",
            "private_ip": "10.40.0.2",
            "public_ip": "203.0.113.20",
            "floating_ip_id": "fip-fresh",
            "reservation_id": reservation,
            "floating_ip_reservation_id": fip,
            "ssh": dict(
                self.config["ssh"],
                host="203.0.113.20",
                known_hosts=str(known),
                host_key_alias=self.config["job_id"] + "-peer",
            ),
        }
        self.connection.live.append(resource)
        self.connection.network.project_ports.append(
            {
                "id": "port-peer",
                "project_id": self.project_id,
                "device_id": resource["server_id"],
                "security_group_ids": ["sg-fresh"],
            }
        )
        return copy.deepcopy(resource)

    def run_guest(self, resource, argv, *, label, timeout):
        assert argv[:4] == ["sudo", "-n", "python3", "-c"] and argv[-1] == resource["private_ip"]
        assert label == "peer-iperf-service" and timeout == 120
        if self.connection.interrupt:
            raise KeyboardInterrupt
        return {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "ready": self.connection.ready,
                    "private_ip": resource["private_ip"],
                    "port": 5201,
                    "unit": "palimpsest-benchmark-peer.service",
                    "pid": 271,
                    "socket_owned_by_process": self.connection.ready,
                }
            ),
        }

    def resources(self):
        return copy.deepcopy(self.connection.live)

    def delete(self, resource):
        self._scope()
        self.connection.deleted.append(resource["server_id"])
        self.connection.live = [r for r in self.connection.live if r["server_id"] != resource["server_id"]]
        self.connection.network.project_ports = [
            p for p in self.connection.network.project_ports if p.get("device_id") != resource["server_id"]
        ]
        self.budget.release_aux(resource["floating_ip_reservation_id"])
        self.budget.release(resource["reservation_id"])


class InterruptedAllocationCloud(Cloud):
    """Nova persisted its response, but the adapter never copied the server ID."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.controller = SimpleNamespace(inspect=self._inspect_original)

    def _inspect_original(self, job_id):
        self._scope()
        self.connection.inspections.append(job_id)
        return peer._read(self.output / (job_id + "-nova.json"))

    def create(self, role):
        assert self.config["placement"]["security_group_ids"] == ["sg-fresh"]
        self.connection.creates += 1
        resource = {
            "source": "sdk",
            "role": role,
            "job_id": self.config["job_id"] + "-peer",
            "reservation_id": self.budget.reserve(role, 20),
        }
        self.connection.live.append(resource)
        self.connection.network.project_ports.append(
            {
                "id": "port-peer",
                "project_id": self.project_id,
                "device_id": "server-fresh",
                "security_group_ids": ["sg-fresh"],
            }
        )
        original = {
            "job_id": resource["job_id"],
            "project_id": self.project_id,
            "server_id": "server-fresh",
            "phase": "created",
        }
        original.update(self.connection.original_receipt_mutation)
        peer._new_json(self.output / (resource["job_id"] + "-nova.json"), original)
        raise KeyboardInterrupt

    def delete(self, resource):
        # The adapter, not the peer membership guard, persists the recovered ID.
        assert not resource.get("server_id")
        original = self.controller.inspect(resource["job_id"])
        server_id = original["server_id"]
        self.connection.deleted.append(server_id)
        self.connection.live = [r for r in self.connection.live if r["job_id"] != resource["job_id"]]
        self.connection.network.project_ports = [
            p for p in self.connection.network.project_ports if p.get("device_id") != server_id
        ]
        self.budget.bind(resource["reservation_id"], server_id)
        self.budget.release(resource["reservation_id"])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    identity, public, known = (tmp_path / name for name in ("session-key", "session-key.pub", "known-hosts"))
    identity.write_text("PRIVATE-KEY-CONTENT-NEVER-COPY")
    public.write_text("ssh-ed25519 " + base64.b64encode(b"qualified-public-key").decode())
    known.write_text("")
    for path in (identity, public, known):
        path.chmod(0o600)
    approval = {
        "id": "new-grant",
        "project_id": "project",
        "user_id": "original-user",
        "allowed_scenarios": ["peer", "environment", "torch-cpu"],
        "allowed_flavor_ids": ["cpu-2-8"],
        "max_vcpus": 2,
        "max_ram_mib": 8192,
        "max_creates": 3,
        "max_live": 2,
        "max_boot_gib": 80,
        "max_security_groups": 1,
        "max_floating_ips": 1,
        "expires_at": "2099-01-01T00:00:00Z",
        "ledger_root": str(tmp_path / "ledger"),
    }
    config = {
        "schema": peer.SCHEMA,
        "job_id": "fresh-peer",
        "scenario": "peer",
        "cloud": "qualified",
        "project_id": "project",
        "project_name": "dmslab",
        "project_domain_name": "SYSTEM",
        "approval": approval,
        "resources": {
            "max_creates": 1,
            "max_live": 1,
            "max_boot_gib": 20,
            "max_security_groups": 1,
            "max_floating_ips": 1,
        },
        "placement": {
            "image_id": "ubuntu",
            "flavor_id": "cpu-2-8",
            "network_id": "private",
            "floating_network_id": "external",
            "security_group_ids": [],
            "boot_gib": 20,
        },
        "deadline_seconds": 3600,
        "peer": {"port": 5201},
        "ssh": {
            "user": "ubuntu",
            "identity_file": str(identity),
            "public_key_file": str(public),
            "known_hosts": str(known),
        },
        "timeouts": {"delete_seconds": 1},
    }
    output = tmp_path / "peer-output"
    network = Network(output)
    connection = SimpleNamespace(
        network=network,
        current_project_id="project",
        current_user_id="original-user",
        live=[],
        deleted=[],
        ready=True,
        interrupt=False,
        creates=0,
        cloud_modes=[],
    )
    monkeypatch.setattr(peer, "ScenarioCloud", Cloud)
    return config, output, connection


def test_full_peer_lifecycle_pins_transport_and_removes_only_original_resources(setup):
    config, output, connection = setup
    result = peer.create_peer(config, output, connection=connection)
    assert result["status"] == "ready"
    data = json.loads((output / "connection.json").read_text())
    assert data["private_ip"] == "10.40.0.2" and data["ssh_jump"] == "ubuntu@203.0.113.20"
    ssh = Path(data["ssh_config_file"]).read_text()
    assert 'IdentityFile "' + config["ssh"]["identity_file"] + '"' in ssh
    assert "StrictHostKeyChecking yes" in ssh and "ForwardAgent no" in ssh and "HostKeyAlias fresh-peer-peer" in ssh
    assert "PRIVATE-KEY-CONTENT" not in "".join(p.read_text() for p in output.rglob("*") if p.is_file())
    rules = connection.network.groups["sg-fresh"]["security_group_rules"]
    assert {r["ether_type"] for r in rules} == {"IPv4"}
    assert not any(r["direction"] == "ingress" and r["port_range_min"] == 5201 and r["remote_ip_prefix"] for r in rules)
    assert peer.cleanup_peer(output, connection=connection)["status"] == "deleted"
    assert not connection.live and not connection.network.groups
    ledger = json.loads((Path(config["approval"]["ledger_root"]) / "new-grant.json").read_text())
    assert all(r["released"] for r in ledger["reservations"].values())
    assert all(r["released"] for r in ledger["auxiliary"].values())


def test_cleanup_recovers_nova_persisted_id_before_owned_port_guard_without_create_replay(setup, monkeypatch):
    config, output, connection = setup
    connection.inspections, connection.original_receipt_mutation = [], {}
    monkeypatch.setattr(peer, "ScenarioCloud", InterruptedAllocationCloud)
    with pytest.raises(KeyboardInterrupt):
        peer.create_peer(config, output, connection=connection)
    result = peer.status_peer(output)
    assert result["status"] == "failed" and result["cleanup"]["original_resources_absent"] is True
    assert connection.deleted == ["server-fresh"] and connection.creates == 1
    assert connection.inspections == ["fresh-peer-peer", "fresh-peer-peer"]
    assert not connection.live and not connection.network.groups
    assert peer.cleanup_peer(output, connection=connection)["status"] == "deleted"
    assert connection.creates == 1 and connection.deleted == ["server-fresh"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"uncertain_create": True},
        {"server_id": None},
        {"project_id": "other-project"},
        {"job_id": "other-job"},
    ],
)
def test_missing_adapter_id_requires_certain_exact_original_nova_receipt(setup, monkeypatch, mutation):
    config, output, connection = setup
    connection.inspections, connection.original_receipt_mutation = [], mutation
    monkeypatch.setattr(peer, "ScenarioCloud", InterruptedAllocationCloud)
    with pytest.raises(KeyboardInterrupt):
        peer.create_peer(config, output, connection=connection)
    assert peer.status_peer(output)["status"] == "held"
    before = copy.deepcopy(connection.network.effects)
    assert peer.cleanup_peer(output, connection=connection)["status"] == "held"
    assert connection.live and not connection.deleted and connection.creates == 1
    assert connection.network.effects == before


def test_recovered_original_id_does_not_authorize_unmatched_group_port(setup, monkeypatch):
    config, output, connection = setup
    connection.inspections, connection.original_receipt_mutation = [], {}
    connection.network.project_ports.append(
        {
            "id": "unknown-port",
            "project_id": "project",
            "device_id": "other-server",
            "security_group_ids": ["sg-fresh"],
        }
    )
    monkeypatch.setattr(peer, "ScenarioCloud", InterruptedAllocationCloud)
    with pytest.raises(KeyboardInterrupt):
        peer.create_peer(config, output, connection=connection)
    assert peer.status_peer(output)["status"] == "held"
    before = copy.deepcopy(connection.network.effects)
    assert peer.cleanup_peer(output, connection=connection)["status"] == "held"
    assert connection.live and not connection.deleted and connection.creates == 1
    assert connection.network.effects == before


def test_cleanup_needs_no_local_ssh_files_and_uses_cleanup_only_adapter(setup):
    config, output, connection = setup
    assert peer.create_peer(config, output, connection=connection)["status"] == "ready"
    for key in ("identity_file", "public_key_file", "known_hosts"):
        Path(config["ssh"][key]).unlink()
    with pytest.raises(ArtifactValidationError):
        peer.validate_peer(config)
    before = [effect for effect in connection.network.effects if effect[0].startswith("create-")]
    assert peer.cleanup_peer(output, connection=connection)["status"] == "deleted"
    assert connection.cloud_modes == [False, True] and connection.creates == 1
    assert [effect for effect in connection.network.effects if effect[0].startswith("create-")] == before


@pytest.mark.parametrize("mutation", ["scenario", "old-sg", "envelope", "boot", "gpu", "secret", "expired"])
def test_approval_and_secret_boundaries_precede_any_effect(setup, mutation):
    config, output, connection = setup
    if mutation == "scenario":
        config["approval"]["allowed_scenarios"] = ["environment"]
    if mutation == "old-sg":
        config["placement"]["security_group_ids"] = ["existing"]
    if mutation == "envelope":
        config["approval"]["max_creates"] = 4
    if mutation == "boot":
        config["placement"]["boot_gib"] = 40
    if mutation == "gpu":
        config["approval"]["allow_gpu"] = True
    if mutation == "secret":
        config["access_token"] = "do-not-persist"
    if mutation == "expired":
        config["approval"]["expires_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ArtifactValidationError):
        peer.create_peer(config, output, connection=connection)
    assert not output.exists() and not connection.network.effects


def test_actor_mismatch_cannot_allocate_or_copy_sdk_error(setup):
    config, output, connection = setup
    connection.current_user_id = "another-user"
    result = peer.create_peer(config, output, connection=connection)
    assert result["status"] == "held" and not connection.network.effects
    assert "scope mismatch" not in (output / "result.json").read_text()
    assert result["error_category"] == "ArtifactValidationError"
    assert result["failed_stage"] == "peer-cloud-preflight"


def test_group_lost_create_response_holds_budget_and_never_adopts_unrecorded_defaults(setup):
    config, output, connection = setup
    connection.network.fail_create = True
    assert peer.create_peer(config, output, connection=connection)["status"] == "held"
    original_effects = copy.deepcopy(connection.network.effects)
    assert peer.cleanup_peer(output, connection=connection)["status"] == "held"
    assert connection.network.effects == original_effects
    assert "do-not-persist" not in "".join(p.read_text() for p in output.rglob("*.json"))
    with pytest.raises(FileExistsError):
        peer.create_peer(config, output, connection=connection)


def test_lost_rule_response_can_only_reconcile_exact_original_intent_for_cleanup(setup):
    config, output, connection = setup
    connection.network.lose_rule_response = True
    result = peer.create_peer(config, output, connection=connection)
    assert result["status"] == "failed" and result["cleanup"]["original_resources_absent"] is True
    assert not connection.network.groups and not connection.live
    assert "do-not-persist" not in json.dumps(result)


def test_target_membership_blocks_peer_deletion_despite_broken_provider_filter(setup):
    config, output, connection = setup
    peer.create_peer(config, output, connection=connection)
    target = {
        "id": "target-port",
        "device_id": "target-server",
        "project_id": "project",
        "security_group_ids": ["sg-fresh"],
    }
    connection.network.project_ports += [
        target,
        {"id": "unrelated", "project_id": "elsewhere", "security_group_ids": ["other"]},
    ]
    assert peer.cleanup_peer(output, connection=connection)["status"] == "held"
    assert connection.live and not connection.deleted
    connection.network.project_ports.remove(target)
    assert peer.cleanup_peer(output, connection=connection)["status"] == "deleted"


def test_permission_failure_is_not_absence_or_delete_replay(setup):
    config, output, connection = setup
    peer.create_peer(config, output, connection=connection)
    connection.network.deny_delete = True
    assert peer.cleanup_peer(output, connection=connection)["status"] == "held"
    before = copy.deepcopy(connection.network.effects)
    connection.network.permission_denied = True
    assert peer.cleanup_peer(output, connection=connection)["status"] == "held"
    assert connection.network.effects == before and "sg-fresh" in connection.network.groups
    connection.network.permission_denied = False
    connection.network.deny_delete = False
    assert peer.cleanup_peer(output, connection=connection)["status"] == "held"
    assert connection.network.effects == before
    # Only externally observed original disappearance settles the existing intent.
    connection.network.groups.pop("sg-fresh")
    assert peer.cleanup_peer(output, connection=connection)["status"] == "deleted"


def test_ready_exit_without_socket_proof_is_failure_and_cleans_owned_vm(setup):
    config, output, connection = setup
    connection.ready = False
    result = peer.create_peer(config, output, connection=connection)
    assert result["status"] == "failed" and not (output / "connection.json").exists()
    assert connection.deleted == ["server-fresh"] and not connection.network.groups


def test_interrupt_cleans_owned_peer_then_preserves_original_interrupt(setup):
    config, output, connection = setup
    connection.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        peer.create_peer(config, output, connection=connection)
    assert not connection.live and not connection.network.groups
    assert peer.status_peer(output)["status"] == "failed"


def test_status_and_cleanup_reject_symlink_or_modified_original_config(setup, tmp_path):
    config, output, connection = setup
    peer.create_peer(config, output, connection=connection)
    original = output / "config.json"
    original.unlink()
    other = tmp_path / "other.json"
    other.write_text(json.dumps(config))
    other.chmod(0o600)
    original.symlink_to(other)
    with pytest.raises(OSError):
        peer.status_peer(output)
    original.unlink()
    config["project_id"] = "replacement"
    peer._new_json(original, config)
    with pytest.raises(ArtifactValidationError, match="original configuration"):
        peer.cleanup_peer(output, connection=connection)
    assert connection.live


def test_kernel_executor_lock_prevents_competing_cleanup(setup):
    config, output, connection = setup
    peer.create_peer(config, output, connection=connection)
    with _executor_lock(output):
        with pytest.raises(ArtifactValidationError, match="executor"):
            peer.cleanup_peer(output, connection=connection)
    assert connection.live


def test_aux_and_vm_ledger_are_shared_with_both_target_jobs(setup, tmp_path):
    config, output, connection = setup
    peer.create_peer(config, output, connection=connection)
    environment = copy.deepcopy(config)
    environment.update(job_id="environment-job", scenario="environment")
    environment["resources"] = dict(config["resources"], max_creates=3, max_live=2, max_boot_gib=80)
    first = ResourceBudget(environment, tmp_path / "environment-output")
    reservation = first.reserve("environment", 20)
    with pytest.raises(ArtifactValidationError, match="auxiliary"):
        first.reserve_aux("security_groups", "other-sg")
    torch = copy.deepcopy(environment)
    torch.update(job_id="torch-job", scenario="torch-cpu")
    second = ResourceBudget(torch, tmp_path / "torch-output")
    with pytest.raises(ArtifactValidationError, match="budget exhausted"):
        second.reserve("torch", 40)
    first.bind(reservation, "environment-server")
    first.release(reservation)
    final = second.reserve("torch", 40)
    second.bind(final, "torch-server")
    second.release(final)
    with pytest.raises(ArtifactValidationError, match="budget exhausted"):
        first.reserve("extra", 1)


def test_nested_wire_rule_aliases_preserve_exact_ownership_and_reject_conflicts():
    wire = {"ethertype": "IPv4", "tenant_id": "project", "protocol": "tcp"}
    assert peer._rule(wire)["ether_type"] == "IPv4"
    assert peer._aliased(wire, "project_id", "tenant_id") == "project"
    with pytest.raises(ArtifactValidationError, match="conflicting"):
        peer._aliased(dict(wire, project_id="other-project"), "project_id", "tenant_id")
    with pytest.raises(ArtifactValidationError, match="conflicting"):
        peer._rule(dict(wire, ether_type="IPv6"))


def test_changed_rule_owner_prevents_group_delete_and_holds_reservation(setup):
    config, output, connection = setup
    peer.create_peer(config, output, connection=connection)
    connection.network.groups["sg-fresh"]["security_group_rules"][0]["project_id"] = "other-project"
    result = peer.cleanup_peer(output, connection=connection)
    assert result["status"] == "held" and "sg-fresh" in connection.network.groups
    assert not any(effect[0] == "delete-group" for effect in connection.network.effects)


def test_absence_requires_actual_404_not_none_or_permission_error():
    with pytest.raises(ArtifactValidationError, match="only 404"):
        peer._get(lambda identity: None, "sg-original")

    def denied(identity):
        raise PermissionError("not a 404")

    with pytest.raises(PermissionError):
        peer._get(denied, "sg-original")

    def absent(identity):
        raise Missing

    assert peer._get(absent, "sg-original") is None


def test_cli_auth_stays_private_and_owned_connection_closes_on_failure(setup, monkeypatch, capsys):
    import importlib.util
    import sys

    from palimpsest_local import benchmark_scenarios

    _, output, _ = setup
    launcher = Path(peer.__file__).resolve().parents[2] / "scripts" / "benchmarks" / "peer.py"
    spec = importlib.util.spec_from_file_location("peer_launcher_test", launcher)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    closed = []
    connection = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(benchmark_scenarios, "connection_from_stdin", lambda: connection)

    def denied(path, *, connection):
        raise PermissionError("authorization: Bearer PRIVATE-TOKEN")

    monkeypatch.setattr(module, "cleanup_peer", denied)
    monkeypatch.setattr(sys, "argv", [str(launcher), "cleanup", "--output", str(output), "--auth-stdin"])
    assert module.main() == 1 and closed == [True]
    text = capsys.readouterr().out
    assert json.loads(text)["status"] == "held" and "PRIVATE-TOKEN" not in text


def test_safe_error_fields_keep_type_stage_and_integer_http_status_only():
    class Denied(Exception):
        status_code = 403
        http_status = "credential-bearing-noninteger"

    result = peer._error_fields(Denied("authorization: Bearer PRIVATE-TOKEN"), "peer-service-start")
    assert result == {"error_category": "Denied", "failed_stage": "peer-service-start", "status_code": 403}


@pytest.mark.parametrize("declaration", ["not-peer", {"peer": True}, ["peer", 1]])
def test_approval_requires_a_list_of_exact_scenario_strings_before_effects(setup, declaration):
    config, output, connection = setup
    config["approval"]["allowed_scenarios"] = declaration
    with pytest.raises(ArtifactValidationError, match="allowed_scenarios"):
        peer.create_peer(config, output, connection=connection)
    assert not output.exists() and not connection.network.effects


@pytest.mark.parametrize("kind", ["packaged", "absent", "override", "changed-payload"])
def test_package_service_setup_only_stops_verified_stock_unit(kind):
    import stat

    class Systemd:
        PIPE = object()

        def __init__(self):
            self.mutations = []

        def check_output(self, argv, **kwargs):
            if argv[:2] == ["dpkg-query", "--listfiles"]:
                return "/usr/lib/systemd/system/iperf3.service\n"
            assert argv[:3] == ["systemctl", "show", "iperf3.service"]
            if "--property=LoadState" in argv:
                if kind == "absent":
                    return "LoadState=not-found\nFragmentPath=\nDropInPaths=\n"
                fragment = (
                    "/etc/systemd/system/iperf3.service"
                    if kind == "override"
                    else "/usr/lib/systemd/system/iperf3.service"
                )
                return "LoadState=loaded\nFragmentPath=" + fragment + "\nDropInPaths=\n"
            assert self.mutations == [["systemctl", "disable", "--now", "iperf3.service"]]
            return "ActiveState=inactive\nMainPID=0\n"

        def run(self, argv, **kwargs):
            if argv == ["dpkg", "--verify", "iperf3"]:
                return SimpleNamespace(stdout="changed" if kind == "changed-payload" else "")
            self.mutations.append(argv)
            return SimpleNamespace(returncode=0)

    model = Systemd()
    filesystem = SimpleNamespace(
        path=SimpleNamespace(realpath=lambda path: path),
        stat=lambda path, **kwargs: SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644),
    )
    namespace = {"subprocess": model, "os": filesystem, "stat": stat}
    if kind in {"override", "changed-payload"}:
        with pytest.raises(SystemExit):
            exec(peer._PACKAGE_SERVICE_SETUP, namespace)
        assert not model.mutations
    else:
        exec(peer._PACKAGE_SERVICE_SETUP, namespace)
        assert model.mutations == ([["systemctl", "disable", "--now", "iperf3.service"]] if kind == "packaged" else [])
