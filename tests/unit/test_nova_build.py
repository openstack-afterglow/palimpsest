from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.nova_build import NovaController, build_nova_layer_user_data, validate_build_spec


@pytest.fixture
def spec():
    return {
        "schema": "palimpsest.nova-build.v1",
        "job_id": "one",
        "project_id": "project-one",
        "arch": "x86_64",
        "base_image_id": "base-one",
        "storage": {
            "volume_id": "12345678-1234-4567-8abc-123456789abc",
            "filesystem_uuid": "12345678-1234-4567-8abc-123456789abd",
            "size_gib": 100,
            "initialize": True,
        },
        "commands": [["/usr/bin/apt-get", "install", "-y", "package=1.0"]],
        "checks": [["/usr/bin/nvidia-smi"]],
    }


@pytest.mark.parametrize(
    "field,value",
    [("arch", "unknown"), ("mode", "nested-libvirt"), ("commands", []), ("checks", []), ("job_id", "../other")],
)
def test_unsafe_build_requests_fail_before_emission(spec, field, value):
    spec[field] = value
    with pytest.raises(ArtifactValidationError):
        build_nova_layer_user_data(spec)


def test_filesystem_and_parent_identity_are_not_inferred(spec):
    for value in ("/dev/vdb", "1234", "12345678-1234-4567-8ABC-123456789ABD"):
        changed = copy.deepcopy(spec)
        changed["storage"]["filesystem_uuid"] = value
        with pytest.raises(ArtifactValidationError):
            validate_build_spec(changed)
    spec["parent_layers"] = [{"digest": "sha256:" + "a" * 64, "size": 4096}] * 2
    with pytest.raises(ArtifactValidationError, match="unique"):
        validate_build_spec(spec)


class Provider:
    def __init__(self):
        self.servers = {}
        self.current_project_id = "project-one"
        self.flavor = {"id": "gpu", "extra_specs": {}}
        self.image = SimpleNamespace(get_image=lambda identity: {"id": identity, "status": "active"})
        self.network = SimpleNamespace(
            get_network=lambda identity: {"id": identity, "project_id": "project-one"},
            get_security_group=lambda identity: {"id": identity, "project_id": "project-one"},
        )
        self.compute = self
        self.block_storage = SimpleNamespace()

    def get_flavor(self, identity):
        return self.flavor

    def fetch_flavor_extra_specs(self, flavor):
        return {**flavor, "extra_specs": {"pci_passthrough:alias": "GPU:1,AUDIO:1"}}

    def create_server(self, **body):
        server = {
            "id": "server-one",
            "name": body["name"],
            "metadata": body["metadata"],
            "project_id": "project-one",
            "status": "ACTIVE",
            "addresses": {},
            "attached_volumes": [],
        }
        self.servers[server["id"]] = server
        return server

    def get_server(self, identity):
        return self.servers[identity]

    def delete_server(self, identity, *, ignore_missing):
        del self.servers[identity]

    def get(self, path, *, microversion):
        attachments = getattr(self, "volume_attachments", [])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"volumeAttachments": attachments})


def _request():
    return dict(
        job_id="job-one",
        name="worker-one",
        image_id="base",
        flavor_id="gpu",
        network_id="network",
        security_group_ids=["security"],
        user_data="#cloud-config\nusers: []\n",
    )


def _controller(tmp_path):
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    provider = Provider()
    return NovaController(provider, "project-one", root), provider


@pytest.mark.parametrize("volume_type", ["ceph_hdd", "a8075c40-bb0e-4d23-bb63-c1174397e0f5"])
def test_explicit_boot_type_is_bound_before_create_and_preserved_in_owned_receipt(tmp_path, volume_type):
    controller, provider = _controller(tmp_path)
    create = provider.create_server
    observed = []

    def check_original_intent(**body):
        intent = json.loads((controller.receipts / "job-one.json").read_text())
        assert intent["phase"] == "create-intent"
        assert intent["descriptor"]["boot_volume_type"] == volume_type
        assert body["block_device_mapping_v2"][0]["volume_type"] == volume_type
        assert body["block_device_mapping_v2"][0]["delete_on_termination"] is False
        encoded = json.dumps(intent["descriptor"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
        assert intent["spec_digest"] == body["metadata"]["palimpsest_spec"] == expected
        without_type = {key: value for key, value in intent["descriptor"].items() if key != "boot_volume_type"}
        assert (
            expected
            != "sha256:"
            + hashlib.sha256(
                json.dumps(without_type, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
        )
        observed.append(expected)
        return create(**body)

    provider.create_server = check_original_intent
    created = controller.create(**_request(), boot_volume_type=volume_type)
    inspected = controller.inspect("job-one")
    assert observed == [created["spec_digest"]]
    assert inspected["descriptor"]["boot_volume_type"] == volume_type
    assert inspected["spec_digest"] == created["spec_digest"]


@pytest.mark.parametrize("volume_type", ["", " ceph_hdd", "../other", "ceph\0hdd", False, {}, "x" * 65])
def test_invalid_boot_type_is_refused_before_create_intent(tmp_path, volume_type):
    controller, provider = _controller(tmp_path)
    with pytest.raises(ArtifactValidationError, match="boot volume type"):
        controller.create(**_request(), boot_volume_type=volume_type)
    assert provider.servers == {}
    assert not list(controller.receipts.iterdir())


def test_older_nova_flavor_get_without_extra_specs_still_accepts_real_gpu_and_preserves_lifecycle(tmp_path):
    controller, provider = _controller(tmp_path)
    controller.create(**_request())
    active = controller.wait_active("job-one", timeout_seconds=1)
    assert active["phase"] == "active-not-ready"
    assert len(provider.servers) == 1
    with pytest.raises(ArtifactValidationError, match="intent already exists"):
        controller.create(**_request())
    assert len(provider.servers) == 1
    provider.servers["server-one"]["metadata"]["palimpsest_spec"] = "changed"
    with pytest.raises(ArtifactValidationError, match="identity"):
        controller.delete("job-one")
    assert "server-one" in provider.servers


def test_lost_create_response_leaves_durable_uncertain_intent_not_a_second_vm(tmp_path):
    controller, provider = _controller(tmp_path)
    original = provider.create_server

    def response_lost(**body):
        original(**body)
        raise TimeoutError("response lost after accepted create")

    provider.create_server = response_lost
    with pytest.raises(TimeoutError):
        controller.create(**_request())
    assert controller.inspect("job-one")["uncertain_create"] is True
    with pytest.raises(ArtifactValidationError, match="intent already exists"):
        controller.create(**_request())
    assert len(provider.servers) == 1


def test_project_and_active_only_result_cannot_claim_build_success(tmp_path):
    controller, provider = _controller(tmp_path)
    provider.current_project_id = "foreign"
    with pytest.raises(ArtifactValidationError, match="project"):
        controller.create(**_request())
    assert provider.servers == {}
    provider.current_project_id = "project-one"
    controller.create(**_request())
    with pytest.raises(ArtifactValidationError, match="successful"):
        controller.record_guest_result("job-one", {"status": "ACTIVE"})
    assert json.loads((controller.receipts / "job-one.json").read_text())["phase"] == "created"


def test_delete_refuses_changed_boot_volume_retention(tmp_path):
    controller, provider = _controller(tmp_path)
    controller.create(**_request())
    provider.volume_attachments = [{"volumeId": "boot", "delete_on_termination": True}]
    with pytest.raises(ArtifactValidationError, match="retention"):
        controller.delete("job-one")
    assert "server-one" in provider.servers
    provider.volume_attachments[0]["delete_on_termination"] = False
    controller.delete("job-one")
    assert "server-one" not in provider.servers


@pytest.mark.parametrize("contents", [None, "{invalid JSON"])
def test_public_nova_build_reports_config_failure_without_traceback(tmp_path, capsys, contents):
    from palimpsest_local.cli import main

    path = tmp_path / "nova-build.json"
    if contents is not None:
        path.write_text(contents)
    assert main(["build", "--executor", "nova", "--nova-config", str(path), "-t", "layer"]) == 1
    captured = capsys.readouterr()
    assert "config" in captured.err.lower() or "json" in captured.err.lower()
    assert "Traceback" not in captured.err


def test_creation_receipt_preserves_original_flavor_after_provider_definition_changes(tmp_path):
    controller, provider = _controller(tmp_path)
    provider.flavor.update(name="gpu-original", vcpus=8, ram=16384, disk=0)
    controller.create(**_request())
    provider.flavor.update(name="gpu-renamed", ram=32768)
    provider.servers["server-one"]["flavor"] = {"original_name": "gpu-original", "vcpus": 8, "ram": 16384}
    evidence = controller.wait_active("job-one", timeout_seconds=1)
    assert evidence["flavor_at_request"]["name"] == "gpu-original"
    assert evidence["flavor_at_request"]["ram"] == 16384
    assert evidence["flavor_at_allocation"]["ram"] == 16384
    provider.servers["server-one"]["flavor"] = None
    assert controller.inspect("job-one")["flavor_at_allocation"] is None


@pytest.fixture
def nova_cli_build(tmp_path, monkeypatch, spec):
    from palimpsest_local import cloud_cli

    identity, known_hosts = tmp_path / "identity", tmp_path / "known-hosts"
    identity.write_text("fixture")
    known_hosts.write_text("fixture")
    config = {
        "spec": spec,
        "project_id": spec["project_id"],
        "assets_root": "/srv/palimpsest/assets",
        "ssh": {
            "user": "builder",
            "identity_file": str(identity),
            "known_hosts": str(known_hosts),
            "python": "/opt/worker/bin/python",
        },
        "launch": {"job_id": spec["job_id"], "image_id": spec["base_image_id"]},
    }
    events = []
    controller = SimpleNamespace(
        create=lambda **kwargs: events.append(("create", kwargs)),
        wait_active=lambda job: {
            "addresses": {"net": [{"addr": "10.0.0.1", "OS-EXT-IPS:type": "fixed", "version": 4}]}
        },
        record_guest_result=lambda job, result: events.append(("guest-result", job, result)),
        inspect=lambda job: {"job_id": job, "phase": "guest-verified"},
    )
    monkeypatch.setattr(cloud_cli, "_controller", lambda actual: controller)
    result = {
        "schema": "palimpsest.nova-build-result.v1",
        "status": "success",
        "job_id": spec["job_id"],
        "project_id": spec["project_id"],
        "boot_id": "fixture-boot",
        "checks_succeeded": 1,
        "recipe_digest": cloud_cli.spec_digest(validate_build_spec(spec)),
    }
    return cloud_cli, config, result, events


def test_nova_build_terminal_unit_failure_stops_promptly_and_retains_resources(nova_cli_build, monkeypatch):
    module, config, result, events = nova_cli_build
    from palimpsest_local.errors import PalimpsestError

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert "sudo -n cat /srv/palimpsest/builds/one/result.json" in argv[-1]
        assert "systemctl is-failed --quiet palimpsest-nova-build.service" in argv[-1]
        assert "HostKeyAlias=one" in argv
        return SimpleNamespace(returncode=42, stdout="")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: 100, sleep=lambda _: pytest.fail("terminal failure slept"))
    )
    with pytest.raises(PalimpsestError, match="service failed.*resources and journal retained"):
        module.run_nova_build(config, name="asset")
    assert len(calls) == 1
    assert [event[0] for event in events] == ["create"]


def test_nova_build_probe_timeout_continues_with_original_deadline(nova_cli_build, monkeypatch):
    module, config, result, events = nova_cli_build
    calls, sleeps = [], []
    clock = iter([100, 100, 14495, 14499])

    def run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        if len(calls) == 1:
            raise module.subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if len(calls) == 2:
            return SimpleNamespace(returncode=0, stdout=json.dumps(result))
        assert "register-build" in argv[-1]
        return SimpleNamespace(returncode=0, stdout='{"asset_id":"verified"}')

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=sleeps.append))
    published = module.run_nova_build(config, name="asset")
    assert published["asset"] == {"asset_id": "verified"}
    assert [timeout for argv, timeout in calls] == [30, 1, 3600]
    assert calls[0][0] == calls[1][0]
    assert sleeps == [5]
    assert [event[0] for event in events] == ["create", "guest-result"]


def test_nova_build_uncertain_registration_timeout_is_typed_and_never_replayed(nova_cli_build, monkeypatch):
    module, config, result, events = nova_cli_build
    from palimpsest_local.errors import PalimpsestError

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout=json.dumps(result))
        assert "register-build" in argv[-1]
        assert kwargs["timeout"] == 3600
        raise module.subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: 100, sleep=lambda _: pytest.fail("unexpected retry"))
    )
    with pytest.raises(PalimpsestError, match="registration timed out.*uncertain.*volumes retained") as failed:
        module.run_nova_build(config, name="asset")
    assert isinstance(failed.value.__cause__, module.subprocess.TimeoutExpired)
    assert len(calls) == 2
    assert [event[0] for event in events] == ["create", "guest-result"]
