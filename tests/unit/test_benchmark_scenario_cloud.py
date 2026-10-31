from __future__ import annotations

import base64
import copy
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from palimpsest_local import benchmark_scenario_cloud as module
from palimpsest_local.errors import ArtifactValidationError, BuildError


class Budget:
    def __init__(self):
        self.reserved = []
        self.bound = {}
        self.released = []
        self.aux_reserved = []
        self.aux_bound = {}
        self.aux_released = []

    def reserve(self, role, boot_gib):
        identity = f"reservation-{len(self.reserved)}"
        self.reserved.append((identity, role, boot_gib))
        return identity

    def bind(self, identity, server):
        self.bound[identity] = server

    def release(self, identity):
        guard = getattr(self, "release_guard", None)
        if guard:
            guard()
        self.released.append(identity)

    def reserve_aux(self, kind, role):
        if self.aux_reserved:
            raise ArtifactValidationError("auxiliary cumulative approval exhausted")
        identity = "aux-one"
        self.aux_reserved.append((identity, kind, role))
        return identity

    def bind_aux(self, identity, provider_id):
        if identity in self.aux_bound:
            assert self.aux_bound[identity] == provider_id
        self.aux_bound[identity] = provider_id

    def release_aux(self, identity):
        guard = getattr(self, "aux_release_guard", None)
        if guard:
            guard()
        self.aux_released.append(identity)


class Cloud:
    def __init__(self):
        self.current_project_id = "project-one"
        self.current_user_id = "user-one"
        self.requests = []
        self.deleted = []
        self.server_absent = False
        self.volume_absent = False
        self.server = {
            "id": "server-one",
            "name": "job-one-baseline",
            "project_id": "project-one",
            "status": "ACTIVE",
            "flavor": {"id": "flavor-one"},
            "attached_volumes": [{"id": "volume-one"}],
            "metadata": {},
        }
        self.volume = {
            "id": "volume-one",
            "project_id": "project-one",
            "size": 20,
            "status": "in-use",
            "volume_type": "fast",
            "attachments": [{"server_id": "server-one"}],
        }
        self.flavor = {"id": "flavor-one", "name": "cpu-small", "ram": 4096, "vcpus": 2, "disk": 0, "extra_specs": {}}
        self.port = {
            "id": "port-one",
            "device_id": "server-one",
            "project_id": "project-one",
            "network_id": "network-one",
            "fixed_ips": [{"ip_address": "10.20.0.5"}],
        }
        self.identity = SimpleNamespace(
            get_project=lambda _: {"id": "project-one", "name": "research", "domain_id": "domain-one"},
            get_domain=lambda _: {"id": "domain-one", "name": "Default"},
        )
        self.image = SimpleNamespace(get_image=lambda _: {"id": "image-one", "status": "active"})
        self.compute = SimpleNamespace(
            get_server=lambda _: copy.deepcopy(self.server),
            create_server=self.create_server,
            get_flavor=lambda _: copy.deepcopy(self.flavor),
            fetch_flavor_extra_specs=lambda f: f,
            get_server_console_output=self.console,
            get=self.retention,
            delete_server=self.delete_server,
            find_server=self.find_server,
        )
        self.network = SimpleNamespace(
            ports=lambda **_: [copy.deepcopy(self.port)],
            get_network=lambda _: {"id": "network-one", "project_id": "project-one"},
            get_security_group=lambda _: {"project_id": "project-one"},
        )
        self.block_storage = SimpleNamespace(
            get_volume=self.get_volume,
            delete_volume=self.delete_volume,
            find_volume=self.find_volume,
        )

    def create_server(self, **kwargs):
        self.requests.append(kwargs)
        self.server["metadata"] = kwargs["metadata"]
        self.server["name"] = kwargs["name"]
        return {"id": "server-one"}

    def console(self, server_id, length):
        key = base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + b"x" * 32).decode()
        return {
            "output": "PALIMPSEST_SCENARIO_HOSTKEY "
            + self.server["metadata"]["palimpsest_job"]
            + " ssh-ed25519 "
            + key
            + "\n"
        }

    def retention(self, path, **kwargs):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "volumeAttachments": [
                    {"volumeId": a["id"], "delete_on_termination": False} for a in self.server["attached_volumes"]
                ]
            },
        )

    def delete_server(self, server_id, **kwargs):
        assert server_id == self.server["id"] and not self.server_absent
        assert not kwargs.get("force") and kwargs.get("ignore_missing") is False
        self.deleted.append(("server", server_id, kwargs))
        self.server_absent = True
        self.volume.update(status="available", attachments=[])

    def delete_volume(self, volume_id, **kwargs):
        assert volume_id == self.volume["id"] and not self.volume_absent
        assert self.server_absent and not self.volume["attachments"] and self.volume["status"] == "available"
        assert not kwargs.get("force") and kwargs.get("ignore_missing") is False
        self.deleted.append(("volume", volume_id, kwargs))
        self.volume_absent = True

    def find_server(self, identity, **kwargs):
        return None if self.server_absent else self.server

    def get_volume(self, identity):
        assert identity == self.volume["id"]
        if self.volume_absent:
            error = RuntimeError("provider volume absent")
            error.status_code = 404
            raise error
        return copy.deepcopy(self.volume)

    def find_volume(self, identity, **kwargs):
        return None if self.volume_absent else self.volume


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    key = tmp_path / "identity"
    key.write_text("private-placeholder")
    key.chmod(0o600)
    public = tmp_path / "identity.pub"
    public.write_text("ssh-ed25519 " + base64.b64encode(b"public-test-value").decode())
    hosts = tmp_path / "known-hosts"
    hosts.write_text("")
    config = {
        "job_id": "job-one",
        "scenario": "environment",
        "cloud": "lab",
        "project_id": "project-one",
        "project_name": "research",
        "project_domain_name": "Default",
        "placement": {
            "image_id": "image-one",
            "flavor_id": "flavor-one",
            "network_id": "network-one",
            "security_group_ids": ["sg-one"],
            "boot_gib": 20,
            "writable_gib": 2,
        },
        "approval": {"user_id": "user-one", "allowed_flavor_ids": ["flavor-one"], "max_vcpus": 2, "max_ram_mib": 8192},
        "ssh": {
            "user": "researcher",
            "identity_file": str(key),
            "public_key_file": str(public),
            "known_hosts": str(hosts),
            "direct_private": True,
        },
        "environment": {
            "schema": "palimpsest.benchmark-environment.v1",
            "disk": {"directory": "/var/tmp/scenario-disk", "size_mib": 16},
            "network": {"external": {}, "internal": {}},
        },
    }
    root = tmp_path / "output"
    root.mkdir(mode=0o700)
    cloud, budget, events = Cloud(), Budget(), []
    monkeypatch.setattr(module, "_supports_retention", lambda *_: True)
    instance = module.ScenarioCloud(
        config, root, lambda name, data: events.append((name, data)), connection=cloud, budget=budget
    )
    return instance, cloud, budget, events


def _create(adapter, monkeypatch):
    instance, cloud, budget, events = adapter

    def ready(resource, argv, **kwargs):
        return {
            "returncode": 0,
            "stdout": "12345678-1234-1234-1234-123456789abc\n" if argv[0] == "python3" else "done",
            "stderr": "",
        }

    with monkeypatch.context() as patch:
        patch.setattr(instance, "run_guest", ready)
        return instance.create("baseline")


@pytest.mark.parametrize("marker", ["", "@cert-authority ", "@revoked "])
def test_console_pin_replaces_all_inherited_alias_keys_and_preserves_jump_pins(adapter, monkeypatch, marker):
    instance, cloud, _, _ = adapter
    alias = "job-one-baseline"
    old = "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + b"y" * 32).decode()
    jump = "jump-one ssh-ed25519 " + base64.b64encode(b"jump-key").decode() + " # jump pin\n"
    shared = marker + "jump-two," + alias + ",jump-three " + old + " # shared pin\n"
    inherited = "# inherited jump policy\n\n" + jump + marker + alias + " " + old + "\n" + shared
    inherited += alias.upper() + " " + old + "\n" + alias + " " + old + "\n"
    inherited += marker + "[" + alias + "]:2222 " + old + "\n"
    Path(instance.config["ssh"]["known_hosts"]).write_text(inherited)
    resource = _create(adapter, monkeypatch)
    pinned = Path(resource["ssh"]["known_hosts"]).read_text()
    fresh = cloud.console(resource["server_id"], 2000)["output"].split(alias + " ", 1)[1]
    assert pinned == (
        "# inherited jump policy\n\n"
        + jump
        + marker
        + "jump-two,jump-three "
        + old
        + " # shared pin\n\n"
        + alias
        + " "
        + fresh
    )
    assert Path(instance.config["ssh"]["known_hosts"]).read_text() == inherited
    args = instance._transport(resource["ssh"])
    assert "StrictHostKeyChecking=yes" in args
    assert "HostKeyAlias=" + alias in args


@pytest.mark.parametrize("marker", ["", "@cert-authority ", "@revoked "])
def test_console_pin_rejects_hashed_inherited_alias_before_writing_pin(adapter, monkeypatch, marker):
    instance, _, _, _ = adapter
    alias, salt = "job-one-baseline", b"s" * 20
    digest = hmac.digest(salt, alias.encode(), "sha1")
    hashed = "|1|" + base64.b64encode(salt).decode() + "|" + base64.b64encode(digest).decode()
    Path(instance.config["ssh"]["known_hosts"]).write_text(marker + hashed + " ssh-ed25519 b2xk\n")
    with pytest.raises(ArtifactValidationError, match="hashed known-host entry matches"):
        _create(adapter, monkeypatch)
    assert not (instance.operations / "baseline.known-hosts").exists()


def test_console_pin_preserves_unrelated_hashed_jump_host(adapter, monkeypatch):
    instance, _, _, _ = adapter
    salt = b"s" * 20
    digest = hmac.digest(salt, b"jump-one", "sha1")
    hashed = "|1|" + base64.b64encode(salt).decode() + "|" + base64.b64encode(digest).decode()
    inherited = "@revoked " + hashed + " ssh-ed25519 b2xk # unrelated\n"
    Path(instance.config["ssh"]["known_hosts"]).write_text(inherited)
    resource = _create(adapter, monkeypatch)
    assert Path(resource["ssh"]["known_hosts"]).read_text().startswith(inherited + "\n")


@pytest.mark.parametrize("hosts", ["job-one-*", "jump-one,job-?ne-baseline", "@cert-authority *"])
def test_console_pin_rejects_matching_inherited_host_patterns(adapter, monkeypatch, hosts):
    instance, _, _, _ = adapter
    Path(instance.config["ssh"]["known_hosts"]).write_text(hosts + " ssh-ed25519 b2xk\n")
    with pytest.raises(ArtifactValidationError, match="known-host pattern matches"):
        _create(adapter, monkeypatch)
    assert not (instance.operations / "baseline.known-hosts").exists()


@pytest.mark.parametrize(
    "returncode,timed_out,message",
    [
        (255, False, "readiness not observed; SSH transport failed"),
        (124, True, "readiness not observed; SSH transport failed"),
        (1, False, "cloud-init did not succeed"),
    ],
)
def test_create_distinguishes_ssh_failure_from_cloud_init_failure(adapter, monkeypatch, returncode, timed_out, message):
    instance, cloud, budget, _ = adapter
    monkeypatch.setattr(
        instance,
        "run_guest",
        lambda *args, **kwargs: {"returncode": returncode, "timed_out": timed_out, "stdout": "", "stderr": ""},
    )
    with pytest.raises(ArtifactValidationError, match=message):
        instance.create("baseline")
    assert instance.resources()[0]["phase"] == "active-not-ready"
    assert not cloud.deleted and not budget.released


def test_requested_flavor_and_original_allocation_are_visible(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter
    assert resource["flavor"]["requested"]["ram"] == 4096
    assert resource["flavor"]["actual"] == {"id": "flavor-one"}
    assert resource["boot_volume_id"] == "volume-one"
    assert resource["private_ip"] == "10.20.0.5"
    assert budget.bound[resource["reservation_id"]] == resource["server_id"]
    request = cloud.requests[0]
    assert request["block_device_mapping_v2"] == [
        {
            "source_type": "image",
            "destination_type": "volume",
            "uuid": "image-one",
            "boot_index": 0,
            "volume_size": 20,
            "delete_on_termination": False,
        }
    ]
    userdata = yaml.safe_load(base64.b64decode(request["user_data"]).decode().removeprefix("#cloud-config\n"))
    assert userdata["users"][0]["ssh_authorized_keys"] == [Path(instance.config["ssh"]["public_key_file"]).read_text()]
    assert "fio" in userdata["packages"] and "iperf3" in userdata["packages"]
    assert "private-placeholder" not in json.dumps(userdata)
    assert resource["ssh"]["host_key_alias"] == resource["job_id"]
    assert resource["job_id"] in Path(resource["ssh"]["known_hosts"]).read_text()


def test_accepted_create_exception_stays_reserved_and_cannot_replay(adapter, monkeypatch):
    instance, cloud, budget, _ = adapter

    def uncertain(**kwargs):
        cloud.requests.append(kwargs)
        raise TimeoutError("accepted but response lost")

    cloud.compute.create_server = uncertain
    with pytest.raises(TimeoutError):
        instance.create("baseline")
    restarted = module.ScenarioCloud(instance.config, instance.output, lambda *_: None, connection=cloud, budget=budget)
    with pytest.raises(ArtifactValidationError, match="intent exists"):
        restarted.create("baseline")
    assert budget.reserved and not budget.released
    assert not cloud.deleted


@pytest.mark.parametrize(
    "field,value", [("project_id", "other-project"), ("name", "research-other"), ("domain", "Other")]
)
def test_project_identity_mismatch_prevents_allocation(adapter, field, value):
    instance, cloud, budget, _ = adapter
    if field == "project_id":
        cloud.current_project_id = value
    elif field == "name":
        cloud.identity.get_project = lambda _: {"id": "project-one", "name": value, "domain_id": "domain-one"}
    else:
        cloud.identity.get_domain = lambda _: {"name": value}
    with pytest.raises(ArtifactValidationError, match="project"):
        instance.create("baseline")
    assert not budget.reserved and not cloud.requests


def test_capability_failure_precedes_reservation(adapter, monkeypatch):
    instance, cloud, budget, _ = adapter
    monkeypatch.setattr(module, "_supports_retention", lambda *_: False)
    with pytest.raises(ArtifactValidationError, match="microversion"):
        instance.create("baseline")
    assert not cloud.requests and not budget.reserved


def test_cloud_init_rejects_credential_injection_before_allocation(adapter):
    instance, cloud, budget, _ = adapter
    with pytest.raises(ArtifactValidationError, match="credentials"):
        instance.create(
            "baseline", user_data={"write_files": [{"path": "/tmp/auth", "content": "OS_PASSWORD=not-a-real-secret"}]}
        )
    assert not cloud.requests and not budget.reserved


def test_current_server_marker_cannot_replace_original_receipt(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, cloud, _, _ = adapter
    cloud.server["metadata"]["palimpsest_spec"] = "sha256:replacement"
    with pytest.raises(ArtifactValidationError, match="original identity"):
        instance.inspect(resource)
    assert not cloud.deleted


def test_changed_attachment_refuses_destructive_cleanup(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter
    cloud.volume["attachments"] = [{"server_id": "someone-else"}]
    with pytest.raises(ArtifactValidationError, match="exclusively"):
        instance.delete(resource)
    assert not cloud.deleted and not budget.released


def test_ordinary_cleanup_proves_both_resources_absent_before_releasing(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter

    def safe_release():
        assert cloud.server_absent and cloud.volume_absent

    budget.release_guard = safe_release
    result = instance.delete(resource)
    assert result["server_absent"] and result["boot_volume_absent"]
    assert cloud.server_absent and cloud.volume_absent
    assert budget.released == [resource["reservation_id"]]
    assert instance.delete(resource)["phase"] == "absent"
    assert cloud.server_absent and cloud.volume_absent
    assert instance.resources() == []


@pytest.mark.parametrize("purpose", ["benchmark", "peer"])
def test_cleanup_only_survives_missing_ssh_and_retired_flavor_without_create_capabilities(
    adapter, monkeypatch, purpose
):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter
    config = copy.deepcopy(instance.config)
    for key in ("identity_file", "public_key_file", "known_hosts"):
        Path(config["ssh"][key]).unlink()
    config.pop("ssh")
    config.pop("environment")

    def retired(*args, **kwargs):
        raise AssertionError("cleanup must not require current provider flavor admission")

    cloud.compute.get_flavor = retired
    cloud.compute.create_server = None
    cloud.compute.get_server_console_output = None
    cloud.compute.fetch_flavor_extra_specs = None
    cloud.image = None
    cloud.network = None
    monkeypatch.setattr(module, "_supports_retention", retired)
    monkeypatch.setattr(module.shutil, "which", retired)
    cleanup = module.ScenarioCloud(
        config,
        instance.output,
        lambda *_: None,
        connection=cloud,
        budget=budget,
        purpose=purpose,
        cleanup_only=True,
    )
    for effect in (
        lambda: cleanup.create("another"),
        lambda: cleanup.run_guest(resource, ["true"], label="forbidden"),
        lambda: cleanup.environment(resource, instance.output),
    ):
        with pytest.raises(ArtifactValidationError, match="cleanup-only"):
            effect()
    assert len(cloud.requests) == 1 and len(budget.reserved) == 1
    assert cleanup.delete(resource)["phase"] == "absent"
    assert cloud.server_absent and cloud.volume_absent
    assert budget.released == [resource["reservation_id"]]


@pytest.mark.parametrize("when", ["construct", "delete"])
@pytest.mark.parametrize("identity", ["actor", "project", "name", "domain"])
def test_cleanup_only_preserves_exact_identity_before_deletion(adapter, monkeypatch, when, identity):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter

    def cleanup_adapter():
        return module.ScenarioCloud(
            instance.config,
            instance.output,
            lambda *_: None,
            connection=cloud,
            budget=budget,
            cleanup_only=True,
        )

    cleanup = cleanup_adapter() if when == "delete" else None
    if identity == "actor":
        cloud.current_user_id = "foreign-user"
    elif identity == "project":
        cloud.current_project_id = "foreign-project"
    elif identity == "name":
        cloud.identity.get_project = lambda _: {"id": "project-one", "name": "other", "domain_id": "domain-one"}
    else:
        cloud.identity.get_domain = lambda _: {"name": "Other"}
    with pytest.raises(ArtifactValidationError, match="approved actor|project"):
        cleanup.delete(resource) if cleanup is not None else cleanup_adapter()
    assert not cloud.deleted and not budget.released


@pytest.mark.parametrize("mutation", ["receipt", "marker", "volume"])
def test_cleanup_only_retains_original_receipt_and_provider_ownership_checks(adapter, monkeypatch, mutation):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter
    cleanup = module.ScenarioCloud(
        instance.config,
        instance.output,
        lambda *_: None,
        connection=cloud,
        budget=budget,
        cleanup_only=True,
    )
    if mutation == "receipt":
        resource["server_id"] = "foreign-server"
    elif mutation == "marker":
        cloud.server["metadata"]["palimpsest_spec"] = "sha256:replacement"
    else:
        cloud.volume["project_id"] = "foreign-project"
    with pytest.raises(ArtifactValidationError):
        cleanup.delete(resource)
    assert not cloud.deleted and not budget.released


@pytest.mark.parametrize("observation_only", [False, True])
def test_noncleanup_constructor_still_requires_current_flavor_admission(adapter, observation_only):
    instance, cloud, budget, _ = adapter
    cloud.flavor["ram"] = 16384
    with pytest.raises(ArtifactValidationError, match="provider flavor exceeds"):
        module.ScenarioCloud(
            instance.config,
            instance.output,
            lambda *_: None,
            connection=cloud,
            budget=budget,
            observation_only=observation_only,
        )


@pytest.mark.parametrize("cleanup_only,observation_only", [("yes", False), (True, True)])
def test_cleanup_mode_requires_unambiguous_boolean_admission(adapter, cleanup_only, observation_only):
    instance, cloud, budget, _ = adapter
    with pytest.raises(ArtifactValidationError, match="boolean|mutually exclusive"):
        module.ScenarioCloud(
            instance.config,
            instance.output,
            lambda *_: None,
            connection=cloud,
            budget=budget,
            observation_only=observation_only,
            cleanup_only=cleanup_only,
        )


def test_lost_delete_response_is_not_replayed_or_budget_released(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter
    unresolved = {"delete_sent": False}

    def lost(identity, **kwargs):
        assert not unresolved["delete_sent"], "uncertain deletion was replayed"
        unresolved["delete_sent"] = True
        cloud.deleted.append(("uncertain-server", identity))
        raise TimeoutError("response lost")

    cloud.compute.delete_server = lost
    with pytest.raises(TimeoutError):
        instance.delete(resource)
    with pytest.raises(ArtifactValidationError, match="replay"):
        instance.delete(resource)
    assert unresolved["delete_sent"] and not cloud.server_absent
    assert not budget.released


def test_api_consumer_observation_needs_no_ssh_and_grants_no_delete_authority(adapter):
    instance, cloud, budget, _ = adapter
    cloud.server["metadata"] = {"palimpsest_instance": "consumer-one"}
    instance.config["ssh"].pop("direct_private")
    observed = instance.verify_external("server-one", expected_marker={"palimpsest_instance": "consumer-one"})
    assert observed["source"] == "api" and observed["volumes"][0]["id"] == "volume-one"
    assert not cloud.requests and not budget.reserved
    with pytest.raises(ArtifactValidationError, match="role"):
        instance.delete(observed)
    with pytest.raises(ArtifactValidationError, match="execution authority"):
        instance.run_guest(observed, ["true"], label="not-owned")
    assert not cloud.deleted


@pytest.mark.parametrize("mutation", ["marker", "project", "flavor", "volume", "port"])
def test_api_consumer_rejects_mismatched_visible_identity(adapter, mutation):
    instance, cloud, _, _ = adapter
    cloud.server["metadata"] = {"palimpsest_instance": "consumer-one"}
    if mutation == "marker":
        cloud.server["metadata"] = {"palimpsest_instance": "other"}
    elif mutation == "project":
        cloud.server["project_id"] = "other"
    elif mutation == "flavor":
        cloud.server["flavor"] = {"id": "other"}
    elif mutation == "volume":
        cloud.volume["project_id"] = "other"
    else:
        cloud.port["project_id"] = "other"
    with pytest.raises(ArtifactValidationError):
        instance.verify_external("server-one", expected_marker={"palimpsest_instance": "consumer-one"})


class Process:
    """Pipe-backed subprocess double: no process or SSH is ever started."""

    def __init__(self, stdout, stderr, private_input=False, returncode=0):
        self.returncode = None
        self.code = returncode
        self.pid = 123456
        self.stdout = self._pipe(stdout)
        self.stderr = self._pipe(stderr)
        self.input_reader = None
        if private_input:
            read_fd, write_fd = os.pipe()
            self.input_reader = os.fdopen(read_fd, "rb")
            self.stdin = os.fdopen(write_fd, "wb")
        else:
            self.stdin = None

    @staticmethod
    def _pipe(value):
        reader, writer = os.pipe()
        os.write(writer, value)
        os.close(writer)
        return os.fdopen(reader, "rb")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.code
        if self.input_reader:
            self.input_reader.close()
        return self.code


def test_guest_stdout_exit_and_redacted_logs_are_consumer_visible(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, _, _, events = adapter
    captured = {}
    sentinels = {
        key: "PRIVATE-SENTINEL-" + key for key in ("OS_PASSWORD", "OS_AUTH_TOKEN", "SSH_AUTH_SOCK", "OS_CLOUD")
    }
    for key, value in sentinels.items():
        monkeypatch.setenv(key, value)

    def launch(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return Process(b"result=42\npassword=do-not-save\n", b"warning\n", returncode=7)

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    response = instance.run_guest(resource, ["python3", "-c", "print(42)"], label="workload", timeout=12)
    assert response["returncode"] == 7 and "result=42" in response["stdout"]
    assert "do-not-save" not in Path(response["stdout_path"]).read_text()
    assert response["stderr"] == "warning\n"
    for sentinel in sentinels.values():
        assert sentinel not in json.dumps(captured["env"])
        assert sentinel not in json.dumps(captured["argv"])
        assert sentinel not in json.dumps(events)
        assert sentinel not in Path(response["stdout_path"]).read_text()
        assert sentinel not in Path(response["stderr_path"]).read_text()
    assert not any("password" in json.dumps(event) for event in events)
    with pytest.raises(ArtifactValidationError, match="label already used"):
        instance.run_guest(resource, ["true"], label="workload")


def test_private_stdin_is_absent_from_arguments_and_all_evidence(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, _, _, events = adapter
    secret = b"K10-PRIVATE-JOIN-TOKEN"
    captured = {}

    def launch(argv, **kwargs):
        captured["argv"] = argv
        return Process(secret + b"\n", secret + b" failure\n", private_input=True, returncode=1)

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    response = instance.run_guest(
        resource, ["python3", "-c", "import sys; sys.stdin.buffer.read()"], label="join-token", input_bytes=secret
    )
    assert response["returncode"] == 1
    assert "WITHHELD" in response["stdout"] and "WITHHELD" in response["stderr"]
    assert secret.decode() not in json.dumps(captured)
    assert secret.decode() not in json.dumps(events)
    for path in (instance.operations / "baseline.join-token").iterdir():
        assert secret not in path.read_bytes()


def test_environment_requires_fresh_original_creation_receipt(adapter):
    instance, _, _, _ = adapter
    with pytest.raises(ArtifactValidationError, match="role"):
        instance.environment({"source": "api", "server_id": "server-one"}, instance.output / "measurement")


def test_environment_preserves_guest_probe_result_and_binding(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, _, _, _ = adapter
    proof = {
        "schema": "palimpsest.environment-result.v1",
        "status": "success",
        "boot_id": resource["boot_id"],
        "boot_id_after": resource["boot_id"],
        "disk": {"status": "success"},
        "network": {"status": "success", "external": {"status": "success"}, "internal": {"status": "success"}},
    }
    monkeypatch.setattr(instance, "run_guest", lambda *a, **kw: {"returncode": 0, "stdout": json.dumps(proof)})
    result = instance.environment(resource, instance.output / "measurement")
    assert result["server_id"] == "server-one" and result["volumes"][0]["id"] == "volume-one"
    assert result["flavor"]["requested"]["ram"] == 4096
    assert json.loads((instance.output / "measurement" / "result.json").read_text())["boot_id"] == resource["boot_id"]


def test_environment_rejects_guest_reboot(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, _, _, _ = adapter
    proof = {
        "schema": "palimpsest.environment-result.v1",
        "boot_id": resource["boot_id"],
        "boot_id_after": "another-boot",
    }
    monkeypatch.setattr(instance, "run_guest", lambda *a, **kw: {"returncode": 0, "stdout": json.dumps(proof)})
    with pytest.raises(ArtifactValidationError, match="boot/schema"):
        instance.environment(resource, instance.output / "measurement")


def test_local_recovery_lists_uncertain_intents_without_cloud_adoption(adapter):
    instance, cloud, _, _ = adapter

    def uncertain(**kwargs):
        raise TimeoutError("lost create")

    cloud.compute.create_server = uncertain
    with pytest.raises(TimeoutError):
        instance.create("baseline")
    resources = instance.resources()
    assert resources[0]["phase"] == "create-intent"
    assert "server_id" not in resources[0]
    assert not cloud.deleted


def test_readonly_absence_verification_does_not_delete_resources(adapter):
    instance, cloud, budget, _ = adapter
    cloud.server_absent = cloud.volume_absent = True
    proof = instance.verify_absent("server-one", "volume-one")
    assert proof["server_absent"] and proof["boot_volume_absent"]
    assert not cloud.deleted and not budget.released


def test_readonly_absence_refuses_name_resolution_to_different_identity(adapter):
    instance, cloud, budget, _ = adapter
    cloud.compute.find_server = lambda *args, **kwargs: {"id": "someone-else"}
    with pytest.raises(ArtifactValidationError, match="different resource"):
        instance.verify_absent("server-one", "volume-one")
    assert not cloud.deleted and not budget.released


def test_volume_only_absence_requires_explicit_certificate_and_actual_get404(adapter):
    instance, cloud, budget, _ = adapter
    with pytest.raises(ArtifactValidationError, match="unknown Nova"):
        instance.verify_absent(None, "volume-one")
    cloud.block_storage.get_volume = lambda identity: None
    with pytest.raises(ArtifactValidationError, match="actual provider 404"):
        instance.verify_absent(None, "volume-one", server_not_created=True)
    cloud.block_storage.get_volume = cloud.get_volume
    cloud.volume_absent = True
    result = instance.verify_absent(None, "volume-one", server_not_created=True)
    assert result["server_absent"] == "not_created" and result["boot_volume_absent"] is True
    assert not cloud.deleted and not budget.released


@pytest.mark.parametrize("change", ["project", "marker", "attachment"])
def test_prepared_root_observation_preserves_exact_ownership_guards(adapter, change):
    instance, cloud, _, _ = adapter
    cloud.volume.update(metadata={"palimpsest_instance_id": "consumer-one"}, attachments=[])
    assert instance.verify_prepared_volume("consumer-one", "volume-one")["boot_volume_id"] == "volume-one"
    if change == "project":
        cloud.volume["project_id"] = "other"
    elif change == "marker":
        cloud.volume["metadata"]["palimpsest_instance_id"] = "other"
    else:
        cloud.volume["attachments"] = [{"server_id": "other"}]
    with pytest.raises(ArtifactValidationError, match="ownership"):
        instance.verify_prepared_volume("consumer-one", "volume-one")


def test_resized_flavor_is_not_original_allocation_evidence(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, cloud, _, _ = adapter
    cloud.server["flavor"] = {"id": "a-larger-flavor"}
    with pytest.raises(ArtifactValidationError, match="original allocation"):
        instance.inspect(resource)


def test_expired_scenario_deadline_prevents_new_allocation(adapter):
    instance, cloud, budget, _ = adapter
    instance.deadline = 0
    with pytest.raises(ArtifactValidationError, match="deadline"):
        instance.create("baseline")
    assert not cloud.requests and not budget.reserved


def test_changed_authenticated_actor_cannot_allocate_under_original_approval(adapter):
    instance, cloud, budget, _ = adapter
    cloud.current_user_id = "different-user"
    with pytest.raises(ArtifactValidationError, match="approved actor"):
        instance.create("baseline")
    assert not cloud.requests and not budget.reserved


@pytest.mark.parametrize("field,value", [("vcpus", 4), ("ram", 16384)])
def test_actual_flavor_limits_are_checked_before_allocation(adapter, field, value):
    instance, cloud, budget, _ = adapter
    cloud.flavor[field] = value
    with pytest.raises(ArtifactValidationError, match="approved"):
        instance.create("baseline")
    assert not cloud.requests and not budget.reserved


def test_cleanup_settles_lost_server_delete_response_without_repeating_it(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter

    def accepted_but_lost(identity, **kwargs):
        cloud.delete_server(identity, **kwargs)
        raise TimeoutError("accepted deletion response lost")

    cloud.compute.delete_server = accepted_but_lost
    with pytest.raises(TimeoutError):
        instance.delete(resource)
    result = instance.delete(instance.resources()[0])
    assert result["phase"] == "absent"
    assert cloud.server_absent and cloud.volume_absent
    assert budget.released == [resource["reservation_id"]]


def test_cleanup_settles_lost_volume_delete_response_without_repeating_it(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter

    def accepted_but_lost(identity, **kwargs):
        cloud.delete_volume(identity, **kwargs)
        raise TimeoutError("accepted volume deletion response lost")

    cloud.block_storage.delete_volume = accepted_but_lost
    with pytest.raises(TimeoutError):
        instance.delete(resource)
    assert not budget.released
    result = instance.delete(instance.resources()[0])
    assert result["phase"] == "absent"
    assert cloud.server_absent and cloud.volume_absent
    assert budget.released == [resource["reservation_id"]]


def test_observer_needs_no_guest_credentials_or_environment_and_cannot_mutate(adapter):
    instance, cloud, budget, _ = adapter
    config = copy.deepcopy(instance.config)
    config.pop("ssh")
    config.pop("environment")
    output = instance.output / "observer"
    output.mkdir(mode=0o700)
    observer = module.ScenarioCloud(config, output, lambda *_: None, connection=cloud, observation_only=True)
    cloud.server["metadata"] = {"palimpsest_instance_id": "consumer-one"}
    assert (
        observer.verify_external("server-one", expected_marker={"palimpsest_instance_id": "consumer-one"})["project_id"]
        == "project-one"
    )
    for operation in (
        lambda: observer.create("baseline"),
        lambda: observer.run_guest({}, ["true"], label="forbidden"),
        lambda: observer.environment({}, output / "environment"),
        lambda: observer.delete({}),
    ):
        with pytest.raises(ArtifactValidationError, match="observation-only"):
            operation()
    assert not cloud.requests and not cloud.deleted and not budget.reserved


def test_retention_uses_discovered_server_range_not_default_microversion():
    pytest.importorskip("keystoneauth1.discover")
    compute = SimpleNamespace(
        default_microversion="2.60",
        get_endpoint_data=lambda: SimpleNamespace(min_microversion="2.1", max_microversion="2.96"),
    )
    assert module._supports_retention(compute)
    compute.get_endpoint_data = lambda: SimpleNamespace(min_microversion="2.1", max_microversion="2.78")
    assert not module._supports_retention(compute)
    compute.get_endpoint_data = lambda: None
    assert not module._supports_retention(compute)


def test_authenticated_token_scope_does_not_require_privileged_domain_inventory(adapter):
    instance, cloud, _, _ = adapter
    cloud.session = SimpleNamespace(
        auth=SimpleNamespace(
            get_access=lambda _: SimpleNamespace(
                project_id="project-one", project_name="research", project_domain_name="Default"
            )
        )
    )
    cloud.identity = SimpleNamespace()
    cloud.server["metadata"] = {"palimpsest_instance_id": "consumer-one"}
    result = instance.verify_external("server-one", expected_marker={"palimpsest_instance_id": "consumer-one"})
    assert result["server_id"] == "server-one"


def test_guest_deadline_is_failed_exit_and_durable_no_replay(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, _, _, _ = adapter
    clock = {"launched": False}
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0 if clock["launched"] else 0.0)

    def launch(*args, **kwargs):
        clock["launched"] = True
        return Process(b"", b"", returncode=-9)

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module.os, "killpg", lambda *a: None)
    result = instance.run_guest(resource, ["sleep", "60"], label="bounded", timeout=1)
    assert result["timed_out"] and result["returncode"] == 124
    assert json.loads((instance.operations / "baseline.bounded" / "status.json").read_text())["timed_out"]
    with pytest.raises(ArtifactValidationError, match="label already used"):
        instance.run_guest(resource, ["sleep", "60"], label="bounded", timeout=1)


def test_guest_output_overflow_is_bounded_failed_not_successful(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    instance, _, _, _ = adapter
    process = Process(b"ready", b"", returncode=-9)
    original_read = os.read
    stdout_fd = process.stdout.fileno()

    def overflowing(fd, count):
        if fd == stdout_fd:
            return b"x" * (module._MAX_OUTPUT + 1)
        return original_read(fd, count)

    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(module.os, "read", overflowing)
    monkeypatch.setattr(module.os, "killpg", lambda *a: None)
    result = instance.run_guest(resource, ["noisy-workload"], label="overflow")
    assert result["truncated"] and result["returncode"] == 125
    assert len(result["stdout"].encode()) <= module._MAX_OUTPUT
    assert Path(result["stdout_path"]).stat().st_size <= module._MAX_OUTPUT


@pytest.mark.parametrize("failure", ["image", "network", "security-group", "gpu"])
def test_readonly_placement_rejection_does_not_consume_creation_budget(adapter, failure):
    instance, cloud, budget, _ = adapter
    if failure == "image":
        cloud.image.get_image = lambda _: {"status": "queued"}
    elif failure == "network":
        cloud.network.get_network = lambda _: {"project_id": "other-project"}
    elif failure == "security-group":
        cloud.network.get_security_group = lambda _: {"project_id": "other-project"}
    else:
        instance.config["scenario"] = "torch-cuda"
    with pytest.raises(ArtifactValidationError):
        instance.create("baseline")
    assert not budget.reserved and not cloud.requests
    assert instance.resources() == []


def test_provider_pre_request_rejection_releases_only_known_unallocated_live_slot(adapter, monkeypatch):
    instance, cloud, budget, _ = adapter

    def reject_before_intent(**kwargs):
        raise ArtifactValidationError("provider admission changed before intent")

    monkeypatch.setattr(instance.controller, "create", reject_before_intent)
    with pytest.raises(ArtifactValidationError, match="admission changed"):
        instance.create("baseline")
    assert budget.reserved and budget.released and not cloud.requests
    assert instance.resources() == []
    with pytest.raises(ArtifactValidationError, match="intent exists"):
        instance.create("baseline")


def test_failed_gpu_scheduling_without_boot_attachment_cleans_owned_error_server(adapter):
    instance, cloud, budget, _ = adapter
    instance.config["scenario"] = "torch-cuda"
    cloud.flavor["extra_specs"] = {"pci_passthrough:alias": "gpu:1"}
    cloud.server.update(status="ERROR", attached_volumes=[])
    cloud.volume_absent = True
    with pytest.raises(BuildError, match="ERROR"):
        instance.create("baseline")

    def forbidden_ports(**kwargs):
        raise AssertionError("cleanup must not require failed guest interfaces")

    cloud.network.ports = forbidden_ports
    result = instance.delete(instance.resources()[0])
    assert cloud.server_absent and result["server_absent"]
    assert result["no_boot_attachment_observed"] and result["boot_volume_id"] is None
    assert result["boot_volume_absent"] is None
    assert budget.released and instance.resources() == []


def test_failed_scheduling_with_boot_attachment_cleans_exact_original_disk(adapter):
    instance, cloud, budget, _ = adapter
    cloud.server["status"] = "ERROR"
    with pytest.raises(BuildError, match="ERROR"):
        instance.create("baseline")

    def forbidden_ports(**kwargs):
        raise AssertionError("cleanup must not require failed guest interfaces")

    cloud.network.ports = forbidden_ports
    result = instance.delete(instance.resources()[0])
    assert result["boot_volume_id"] == "volume-one"
    assert cloud.server_absent and cloud.volume_absent and budget.released


def test_api_embedded_flavor_requires_actual_snapshot_not_only_reused_name(adapter):
    instance, cloud, _, _ = adapter
    cloud.server["metadata"] = {"palimpsest_instance_id": "consumer-one"}
    cloud.server["flavor"] = {"original_name": "cpu-small", "vcpus": 2, "ram": 4096, "disk": 0}
    assert (
        instance.verify_external("server-one", expected_marker={"palimpsest_instance_id": "consumer-one"})["flavor"][
            "observed"
        ]["ram"]
        == 4096
    )
    cloud.server["flavor"]["ram"] = 8192
    with pytest.raises(ArtifactValidationError, match="embedded flavor snapshot"):
        instance.verify_external("server-one", expected_marker={"palimpsest_instance_id": "consumer-one"})


def test_api_image_boot_without_cinder_is_observed_without_invented_disk(adapter):
    instance, cloud, _, _ = adapter
    cloud.server.update(metadata={"palimpsest_instance_id": "consumer-one"}, attached_volumes=[])
    observed = instance.verify_external("server-one", expected_marker={"palimpsest_instance_id": "consumer-one"})
    assert observed["volumes"] == []
    with pytest.raises(ArtifactValidationError, match="attachments differ"):
        instance.verify_external(
            {"server_id": "server-one", "volume_ids": ["expected-volume"]},
            expected_marker={"palimpsest_instance_id": "consumer-one"},
        )


@pytest.mark.parametrize("group_absent", [True, False])
def test_signal_denial_is_benign_only_after_owned_reap_and_group_absence(monkeypatch, group_absent):
    process = Process(b"", b"", returncode=125)

    def signal_group(pid, selected_signal):
        if selected_signal == 0 and group_absent:
            raise ProcessLookupError()
        if selected_signal != 0:
            raise PermissionError("signal denied")

    monkeypatch.setattr(module.os, "killpg", signal_group)
    try:
        if group_absent:
            module._kill_owned(process)
            assert process.returncode == 125
        else:
            with pytest.raises(PermissionError, match="signal denied"):
                module._kill_owned(process)
    finally:
        process.stdout.close()
        process.stderr.close()


def test_signal_denial_of_live_owned_child_is_not_suppressed(monkeypatch):
    def deny(*args):
        raise PermissionError("live child signal denied")

    def still_live(**kwargs):
        raise module.subprocess.TimeoutExpired("private transport", 1)

    process = SimpleNamespace(pid=123456, wait=still_live)
    monkeypatch.setattr(module.os, "killpg", deny)
    with pytest.raises(PermissionError, match="live child"):
        module._kill_owned(process)


@pytest.mark.parametrize("corrupt", [False, True])
def test_environment_collects_complete_truncated_raw_log_before_guest_cleanup(adapter, monkeypatch, corrupt):
    import base64
    import hashlib

    resource = _create(adapter, monkeypatch)
    instance, cloud, budget, _ = adapter
    payload = b'{"fio_result":"measured"}\n' * 20000
    prefix = payload[:32768]
    proof = {
        "schema": "palimpsest.environment-result.v1",
        "status": "success",
        "boot_id": resource["boot_id"],
        "boot_id_after": resource["boot_id"],
        "disk": {"status": "success"},
        "remote_evidence_directory": "/tmp/palimpsest-scenario-environment-fixture/evidence",
        "guest_logs": {
            "fio.stdout": {
                "text": prefix.decode(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "truncated": True,
            }
        },
    }

    def guest(resource, argv, *, label, timeout):
        if label == "environment":
            return {"returncode": 0, "stdout": json.dumps(proof)}
        offset, count = int(argv[-2]), int(argv[-1])
        data = payload[offset : offset + count]
        if corrupt:
            data = b"X" * len(data)
        return {"returncode": 0, "stdout": json.dumps({"offset": offset, "data": base64.b64encode(data).decode()})}

    monkeypatch.setattr(instance, "run_guest", guest)
    output = instance.output / "measurement"
    if corrupt:
        with pytest.raises(ArtifactValidationError, match="byte identity"):
            instance.environment(resource, output)
        assert not cloud.deleted and not budget.released
    else:
        result = instance.environment(resource, output)
        assert (output / "raw/fio.stdout").read_bytes() == payload
        assert result["guest_logs"]["fio.stdout"]["collected"] is True


def test_existing_guest_docker_engine_is_preserved_instead_of_replaced():
    import sys
    from unittest.mock import patch

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("subprocess.run", side_effect=AssertionError("existing qualified engine must not be reinstalled")),
        patch.object(sys, "argv", ["setup", "gpu"]),
    ):
        exec(compile(module._DOCKER_SETUP, "<fixed-docker-setup>", "exec"), {})


def test_missing_gpu_docker_runtime_is_not_automatically_installed():
    import sys
    from unittest.mock import patch

    with (
        patch("shutil.which", return_value=None),
        patch(
            "subprocess.run", side_effect=AssertionError("GPU setup must not install an unqualified engine or driver")
        ),
        patch.object(sys, "argv", ["setup", "gpu"]),
    ):
        with pytest.raises(SystemExit, match="GPU prerequisites absent"):
            exec(compile(module._DOCKER_SETUP, "<fixed-docker-setup>", "exec"), {})


class FloatingCloud(Cloud):
    def __init__(self):
        super().__init__()
        self.floating_requests = []
        self.floating = None
        self.floating_absent = False
        self.network.get_network = lambda identity: (
            {"id": "external-one", "is_router_external": True}
            if identity == "external-one"
            else {"id": "network-one", "project_id": "project-one"}
        )
        self.network.create_ip = self.create_ip
        self.network.get_ip = self.get_ip
        self.network.delete_ip = self.delete_ip
        self.network.ips = lambda **_: (
            [] if self.floating is None or self.floating_absent else [copy.deepcopy(self.floating)]
        )

    def create_ip(self, **kwargs):
        assert self.server["status"] == "ACTIVE"
        assert kwargs["port_id"] == self.port["id"]
        self.floating_requests.append(kwargs)
        self.floating = {"id": "fip-one", "floating_ip_address": "198.51.100.10", **kwargs}
        return copy.deepcopy(self.floating)

    def get_ip(self, identity):
        class NotFound(Exception):
            status_code = 404

        assert identity == "fip-one"
        if self.floating_absent:
            raise NotFound("absent")
        return copy.deepcopy(self.floating)

    def delete_ip(self, identity, **kwargs):
        assert identity == "fip-one" and not self.floating_absent
        assert kwargs == {"ignore_missing": False}
        assert not self.server_absent
        self.deleted.append(("floating", identity, kwargs))
        self.floating_absent = True


def _peer_adapter(adapter):
    instance, _, budget, events = adapter
    config = copy.deepcopy(instance.config)
    config.pop("environment")
    config["scenario"] = "peer"
    config["placement"]["floating_network_id"] = "external-one"
    config["ssh"].pop("direct_private")
    cloud = FloatingCloud()
    peer = module.ScenarioCloud(
        config,
        instance.output,
        lambda name, data: events.append((name, data)),
        connection=cloud,
        budget=budget,
        purpose="peer",
    )
    return peer, cloud, budget, events


def test_peer_allocation_binds_fresh_ip_before_console_pin_and_preserves_private_interface(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    instance, cloud, budget, _ = peer
    pin = instance._pin

    def require_binding(resource, host):
        assert cloud.floating_requests and budget.aux_bound == {"aux-one": "fip-one"}
        assert host == "198.51.100.10"
        return pin(resource, host)

    monkeypatch.setattr(instance, "_pin", require_binding)
    resource = _create(peer, monkeypatch)
    assert resource["private_ip"] == "10.20.0.5" and resource["port_id"] == "port-one"
    assert resource["public_ip"] == resource["ssh"]["host"] == "198.51.100.10"
    assert resource["floating_ip_phase"] == "bound"
    assert resource["interfaces"][0]["device_id"] == resource["server_id"]
    assert budget.aux_reserved == [("aux-one", "floating_ips", "baseline-fip")]
    data = yaml.safe_load(base64.b64decode(cloud.requests[0]["user_data"]).decode().removeprefix("#cloud-config\n"))
    assert "/var/tmp/palimpsest-benchmark-peer" in data["runcmd"][2][2]
    with pytest.raises(ArtifactValidationError, match="peer guests"):
        instance.environment(resource, instance.output / "measurements")


def test_normal_benchmark_is_private_without_automatic_floating_ip(adapter, monkeypatch):
    resource = _create(adapter, monkeypatch)
    assert "floating_ip_id" not in resource and "public_ip" not in resource
    assert resource["ssh"]["host"] == resource["private_ip"]
    assert not adapter[2].aux_reserved


@pytest.mark.parametrize("observation_only", [False, True])
def test_nonpeer_floating_network_is_rejected_before_authentication(adapter, observation_only):
    instance, cloud, budget, _ = adapter
    config = copy.deepcopy(instance.config)
    config["placement"]["floating_network_id"] = "external-one"
    cloud.identity.get_project = lambda _: pytest.fail("must reject before authentication")
    with pytest.raises(ArtifactValidationError, match="only permitted for a fresh peer"):
        module.ScenarioCloud(
            config,
            instance.output,
            lambda *_: None,
            connection=cloud,
            budget=budget,
            observation_only=observation_only,
        )
    assert not cloud.requests and not budget.reserved


def test_peer_empty_security_groups_only_admitted_until_fresh_group_is_set(adapter, monkeypatch):
    instance, cloud, budget, _ = _peer_adapter(adapter)
    config = copy.deepcopy(instance.config)
    config["placement"]["security_group_ids"] = []
    peer = module.ScenarioCloud(
        config,
        instance.output,
        lambda *_: None,
        connection=cloud,
        budget=budget,
        purpose="peer",
    )
    with pytest.raises(ArtifactValidationError, match="must be set"):
        peer.create("baseline")
    assert not budget.reserved and not cloud.requests
    peer.config["placement"]["security_group_ids"] = ["sg-one"]
    assert _create((peer, cloud, budget, []), monkeypatch)["floating_ip_id"] == "fip-one"
    config["placement"].pop("floating_network_id")
    with pytest.raises(ArtifactValidationError, match="security groups"):
        module.validate_cloud_config(config)


@pytest.mark.parametrize("mutation", ["user", "project", "flavor"])
def test_peer_actor_project_and_flavor_caps_remain_strict(adapter, mutation):
    instance, cloud, budget, _ = _peer_adapter(adapter)
    if mutation == "user":
        cloud.current_user_id = "foreign-user"
    elif mutation == "project":
        cloud.current_project_id = "foreign-project"
    else:
        cloud.flavor["ram"] = 16384
    with pytest.raises(ArtifactValidationError):
        module.ScenarioCloud(
            instance.config,
            instance.output,
            lambda *_: None,
            connection=cloud,
            budget=budget,
            purpose="peer",
        )
    assert not cloud.requests and not cloud.floating_requests and not budget.aux_reserved


def test_lost_floating_ip_creation_response_reconciles_only_original_exact_marker(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    instance, cloud, budget, _ = peer

    def lost(**kwargs):
        cloud.create_ip(**kwargs)
        raise TimeoutError("accepted response lost")

    cloud.network.create_ip = lost
    with pytest.raises(TimeoutError):
        _create(peer, monkeypatch)
    resource = instance.resources()[0]
    assert resource["floating_ip_phase"] == "create-intent" and "floating_ip_id" not in resource
    restarted = module.ScenarioCloud(
        instance.config,
        instance.output,
        lambda *_: None,
        connection=cloud,
        budget=budget,
        purpose="peer",
    )
    with pytest.raises(ArtifactValidationError, match="intent exists"):
        restarted.create("baseline")
    reconciled = restarted.inspect(resource)
    assert reconciled["floating_ip_id"] == "fip-one" and reconciled["floating_ip_phase"] == "bound"
    assert len(cloud.floating_requests) == 1 and budget.aux_bound == {"aux-one": "fip-one"}
    result = restarted.delete(reconciled)
    assert result["floating_ip_absent"] and result["server_absent"] and result["boot_volume_absent"]
    assert [entry[0] for entry in cloud.deleted] == ["floating", "server", "volume"]


@pytest.mark.parametrize("matches", [0, 2])
def test_uncertain_floating_ip_intent_cannot_allocate_again_or_delete_unmatched_resources(
    adapter, monkeypatch, matches
):
    peer = _peer_adapter(adapter)
    instance, cloud, budget, _ = peer

    def lost(**kwargs):
        cloud.create_ip(**kwargs)
        raise TimeoutError("response lost")

    cloud.network.create_ip = lost
    with pytest.raises(TimeoutError):
        _create(peer, monkeypatch)
    cloud.network.ips = lambda **_: [copy.deepcopy(cloud.floating)] * matches
    resource = instance.resources()[0]
    for operation in (instance.inspect, instance.delete):
        with pytest.raises(ArtifactValidationError, match="uncertain"):
            operation(resource)
    assert len(cloud.floating_requests) == 1 and not cloud.deleted
    assert not budget.aux_released and not budget.released


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "foreign-project"),
        ("port_id", "foreign-port"),
        ("fixed_ip_address", "10.20.0.99"),
        ("floating_network_id", "foreign-network"),
        ("description", "borrowed"),
        ("floating_ip_address", "198.51.100.99"),
    ],
)
def test_changed_floating_binding_blocks_pinning_execution_and_cleanup(adapter, monkeypatch, field, value):
    peer = _peer_adapter(adapter)
    resource = _create(peer, monkeypatch)
    instance, cloud, budget, _ = peer
    cloud.floating[field] = value
    for operation in (
        lambda: instance.inspect(resource),
        lambda: instance._pin(resource, resource["public_ip"]),
        lambda: instance.run_guest(resource, ["true"], label="changed-binding"),
        lambda: instance.delete(resource),
    ):
        with pytest.raises(ArtifactValidationError, match="floating IP ownership"):
            operation()
    assert not cloud.deleted and not budget.aux_released and not budget.released


def test_floating_marker_match_with_foreign_port_is_never_adopted(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    instance, cloud, budget, _ = peer

    def lost(**kwargs):
        cloud.create_ip(**kwargs)
        cloud.floating["port_id"] = "foreign-port"
        raise TimeoutError("response lost")

    cloud.network.create_ip = lost
    with pytest.raises(TimeoutError):
        _create(peer, monkeypatch)
    with pytest.raises(ArtifactValidationError, match="exact port binding"):
        instance.delete(instance.resources()[0])
    assert not budget.aux_bound and not budget.aux_released and not cloud.deleted


@pytest.mark.parametrize("accepted", [False, True])
def test_interrupted_floating_delete_is_forward_only_and_budget_requires_404(adapter, monkeypatch, accepted):
    peer = _peer_adapter(adapter)
    resource = _create(peer, monkeypatch)
    instance, cloud, budget, _ = peer
    attempts = []

    def lost(identity, **kwargs):
        attempts.append(identity)
        if accepted:
            cloud.delete_ip(identity, **kwargs)
        raise TimeoutError("response lost")

    cloud.network.delete_ip = lost

    def safe_aux_release():
        assert cloud.floating_absent, "release requires the original floating IP GET 404"

    budget.aux_release_guard = safe_aux_release
    with pytest.raises(TimeoutError):
        instance.delete(resource)
    assert not cloud.server_absent and not budget.aux_released
    receipt = instance.resources()[0]
    assert receipt["floating_ip_phase"] == "delete-intent"
    if accepted:
        result = instance.delete(receipt)
        assert result["phase"] == "absent" and result["floating_ip_budget_released"]
        assert budget.aux_released == ["aux-one"] and budget.released == [resource["reservation_id"]]
        assert instance.delete(result)["phase"] == "absent"
        assert instance.resources() == []
    else:
        with pytest.raises(ArtifactValidationError, match="no destructive replay"):
            instance.delete(receipt)
        assert not budget.aux_released and not budget.released and not cloud.server_absent
    assert attempts == ["fip-one"]


def test_floating_delete_intent_is_durable_before_provider_effect(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    resource = _create(peer, monkeypatch)
    instance, cloud, budget, _ = peer

    def interrupted(identity, **kwargs):
        receipt = module._read(instance.operations / "baseline.json")
        assert receipt["floating_ip_phase"] == "delete-intent"
        raise KeyboardInterrupt()

    cloud.network.delete_ip = interrupted
    with pytest.raises(KeyboardInterrupt):
        instance.delete(resource)
    with pytest.raises(ArtifactValidationError, match="no destructive replay"):
        instance.delete(instance.resources()[0])
    assert not cloud.deleted and not budget.aux_released


def test_floating_allocation_budget_denial_precedes_provider_request(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    instance, cloud, budget, _ = peer
    budget.aux_reserved.append(("already-consumed", "floating_ips", "prior-peer-fip"))
    with pytest.raises(ArtifactValidationError, match="approval exhausted"):
        _create(peer, monkeypatch)
    assert not cloud.floating_requests and not budget.aux_bound
    resource = instance.resources()[0]
    assert resource["server_id"] == "server-one" and "floating_ip_phase" not in resource
    assert instance.delete(resource)["phase"] == "absent"
    assert not budget.aux_released


def test_peer_public_transport_cannot_be_substituted_for_another_host(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    resource = _create(peer, monkeypatch)
    instance, cloud, budget, _ = peer
    with pytest.raises(ArtifactValidationError, match="public address differs"):
        instance._pin(resource, resource["private_ip"])
    resource["ssh"]["host"] = resource["private_ip"]
    with pytest.raises(ArtifactValidationError, match="transport is not bound"):
        instance.run_guest(resource, ["true"], label="private-substitution")
    assert not (instance.operations / "baseline.private-substitution").exists()


def test_original_peer_port_replacement_is_not_floating_ip_authority(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    resource = _create(peer, monkeypatch)
    instance, cloud, budget, _ = peer
    cloud.port["id"] = "replacement-port"
    for operation in (instance.inspect, instance.delete):
        with pytest.raises(ArtifactValidationError, match="interface binding changed"):
            operation(resource)
    assert not cloud.deleted and not budget.aux_released


def test_floating_create_intent_and_aux_reservation_precede_provider_effect(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    instance, cloud, budget, _ = peer

    def capture(**kwargs):
        receipt = module._read(instance.operations / "baseline.json")
        assert receipt["floating_ip_phase"] == "create-intent"
        assert receipt["floating_ip_reservation_id"] == budget.aux_reserved[0][0]
        assert receipt["port_id"] == kwargs["port_id"]
        assert receipt["private_ip"] == kwargs["fixed_ip_address"]
        assert receipt["floating_ip_description"] == kwargs["description"]
        assert receipt["project_id"] == kwargs["project_id"]
        assert "floating_ip_id" not in receipt
        return cloud.create_ip(**kwargs)

    cloud.network.create_ip = capture
    assert _create(peer, monkeypatch)["floating_ip_phase"] == "bound"


def test_floating_absence_requires_actual_404_not_empty_get_response(adapter, monkeypatch):
    peer = _peer_adapter(adapter)
    resource = _create(peer, monkeypatch)
    instance, cloud, budget, _ = peer

    def lost(identity, **kwargs):
        raise TimeoutError("unresolved delete response")

    cloud.network.delete_ip = lost
    with pytest.raises(TimeoutError):
        instance.delete(resource)
    cloud.network.get_ip = lambda _: None
    with pytest.raises(ArtifactValidationError, match="GET returned no exact resource identity"):
        instance.delete(instance.resources()[0])
    assert not budget.aux_released and not budget.released and not cloud.server_absent


def test_station_admits_no_legacy_environment_and_prepares_fixed_mountpoint(adapter):
    instance, _, _, _ = adapter
    config = copy.deepcopy(instance.config)
    config.pop("environment")
    config["data_disk"] = {"mountpoint": "/mnt/palimpsest-benchmark"}
    module.validate_cloud_config(config, purpose="station")
    instance.config, instance.purpose = config, "station"
    userdata = yaml.safe_load(instance._userdata("job-one-station", None).split("\n", 1)[1])
    prepare = next(
        command[2] for command in userdata["runcmd"] if command[:2] == ["python3", "-c"] and "p=Path(" in command[2]
    )
    assert "/mnt/palimpsest-benchmark" in prepare
    assert "mkfs" not in instance._userdata("job-one-station", None)
    assert any(command[:3] == ["systemctl", "enable", "--now"] for command in userdata["runcmd"])


def test_explicit_station_context_allows_probe_free_workload_cloud_init(adapter):
    instance, _, _, _ = adapter
    config = copy.deepcopy(instance.config)
    config.pop("environment")
    config["environment_context"] = {"checkpoint_id": "one"}
    module.validate_cloud_config(config)
    instance.config = config
    userdata = instance._userdata("job-one-workload", None)
    assert "/var/tmp/palimpsest-benchmark" in userdata
    assert "iperf3 -" not in userdata and "mkfs" not in userdata


@pytest.mark.parametrize("purpose", ["station", "benchmark"])
def test_only_station_original_receipt_may_include_exact_data_id(adapter, monkeypatch, purpose):
    instance, cloud, _, _ = adapter
    instance.purpose = purpose
    instance.config["data_disk"] = {"mountpoint": "/mnt/palimpsest-benchmark"}
    resource = _create(adapter, monkeypatch)
    data = {**copy.deepcopy(cloud.volume), "id": "data-one", "size": 500}
    resource["data_volume_ids"] = ["data-one"]
    instance._save(resource)
    cloud.server["attached_volumes"].append({"id": "data-one"})
    cloud.block_storage.get_volume = lambda identity: copy.deepcopy(data if identity == "data-one" else cloud.volume)
    if purpose == "station":
        assert {v["id"] for v in instance.inspect(resource)["volumes"]} == {"volume-one", "data-one"}
    else:
        with pytest.raises(ArtifactValidationError, match="only original station receipts"):
            instance.inspect(resource)


def test_station_rejects_extra_unrecorded_disk_and_forged_caller_data_ids(adapter, monkeypatch):
    instance, cloud, _, _ = adapter
    instance.purpose = "station"
    instance.config["data_disk"] = {"mountpoint": "/mnt/palimpsest-benchmark"}
    resource = _create(adapter, monkeypatch)
    data = {**copy.deepcopy(cloud.volume), "id": "foreign-data", "size": 500}
    cloud.server["attached_volumes"].append({"id": "foreign-data"})
    cloud.block_storage.get_volume = lambda identity: copy.deepcopy(
        data if identity == "foreign-data" else cloud.volume
    )
    resource["data_volume_ids"] = ["foreign-data"]
    with pytest.raises(ArtifactValidationError, match="original boot/data attachment changed"):
        instance.inspect(resource)


def test_station_recorded_data_still_requires_exact_project_exclusive_binding(adapter, monkeypatch):
    instance, cloud, _, _ = adapter
    instance.purpose = "station"
    instance.config["data_disk"] = {"mountpoint": "/mnt/palimpsest-benchmark"}
    resource = _create(adapter, monkeypatch)
    resource["data_volume_ids"] = ["data-one"]
    instance._save(resource)
    data = {**copy.deepcopy(cloud.volume), "id": "data-one", "size": 500, "attachments": [{"server_id": "foreign"}]}
    cloud.server["attached_volumes"].append({"id": "data-one"})
    cloud.block_storage.get_volume = lambda identity: copy.deepcopy(data if identity == "data-one" else cloud.volume)
    with pytest.raises(ArtifactValidationError, match="exclusively bound"):
        instance.inspect(resource)
