from __future__ import annotations

import base64
import builtins
import copy
import json
import stat
from types import SimpleNamespace

import pytest

from palimpsest_local import benchmark_station_cloud as module
from palimpsest_local.benchmark_scenario_cloud import _new_json, _read
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.state import atomic_write_json


@pytest.fixture
def config(tmp_path, monkeypatch):
    key, public, hosts = (tmp_path / n for n in ("identity", "identity.pub", "known-hosts"))
    key.write_text("test-private-placeholder")
    key.chmod(0o600)
    public.write_text("ssh-ed25519 " + base64.b64encode(b"public-test-value").decode())
    hosts.write_text("")
    from palimpsest_local import benchmark_station_measurements

    monkeypatch.setattr(benchmark_station_measurements, "validate_measurement", lambda _: None)
    return {
        "schema": module.SCHEMA,
        "job_id": "station-one",
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
        },
        "approval": {
            "id": "approval-one",
            "project_id": "project-one",
            "user_id": "user-one",
            "allowed_scenarios": ["environment"],
            "allowed_flavor_ids": ["flavor-one"],
            "max_vcpus": 4,
            "max_ram_mib": 8192,
            "max_creates": 1,
            "max_live": 1,
            "max_boot_gib": 20,
            "ledger_root": str(tmp_path / "ledger"),
            "expires_at": "2099-01-01T00:00:00Z",
        },
        "resources": {"max_creates": 1, "max_live": 1, "max_boot_gib": 20},
        "ssh": {
            "user": "researcher",
            "identity_file": str(key),
            "public_key_file": str(public),
            "known_hosts": str(hosts),
            "direct_private": True,
        },
        "data_disk": {"size_gib": 500, "volume_type": "ceph_hdd", "mountpoint": module._MOUNTPOINT},
        "measurement": {},
    }


class Provider:
    def __init__(self):
        self.calls = []
        self.volumes = {}
        self.attachments = [{"volumeId": "boot-one", "delete_on_termination": False}]
        self.flavor = {"id": "flavor-one", "vcpus": 4, "ram": 8192, "disk": 0}
        self.uncertain_create = self.uncertain_attach = self.uncertain_delete = False
        self.block_storage = SimpleNamespace(
            create_volume=self.create_volume,
            get_volume=self.get_volume,
            find_volume=lambda i, **_: self.volumes.get(i),
            delete_volume=self.delete_volume,
        )
        self.compute = SimpleNamespace(
            get_flavor=lambda _: copy.deepcopy(self.flavor), post=self.attach, delete=self.detach, get=self.retention
        )

    def response(self):
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"volumeAttachments": copy.deepcopy(self.attachments)}
        )

    def create_volume(self, **kwargs):
        self.calls.append(("create-data", kwargs))
        if self.uncertain_create:
            raise TimeoutError("unknown POST response")
        assert kwargs["size"] == 500 and kwargs["volume_type"] == "ceph_hdd" and kwargs["is_multiattach"] is False
        self.volumes["data-one"] = dict(
            id="data-one",
            project_id="project-one",
            size=500,
            volume_type="ceph_hdd",
            status="available",
            attachments=[],
            metadata=kwargs["metadata"],
            is_multiattach=False,
        )
        return {"id": "data-one"}

    def get_volume(self, identity):
        return copy.deepcopy(self.volumes[identity])

    def attach(self, path, **kwargs):
        self.calls.append(("attach", path, kwargs))
        assert kwargs["microversion"] == "2.79"
        assert kwargs["json"] == {"volumeAttachment": {"volumeId": "data-one", "delete_on_termination": False}}
        if self.uncertain_attach:
            raise TimeoutError("unknown attachment")
        self.attachments.append({"volumeId": "data-one", "delete_on_termination": False})
        self.volumes["data-one"].update(status="in-use", attachments=[{"server_id": "server-one"}])
        return self.response()

    def retention(self, path, **kwargs):
        assert kwargs["microversion"] == "2.79"
        return self.response()

    def detach(self, path, **kwargs):
        self.calls.append(("detach", path, kwargs))
        assert path.endswith("/os-volume_attachments/data-one") and kwargs["microversion"] == "2.79"
        self.attachments = self.attachments[:1]
        self.volumes["data-one"].update(status="available", attachments=[])
        return self.response()

    def delete_volume(self, identity, **kwargs):
        self.calls.append(("delete-data", identity, kwargs))
        assert kwargs == {"ignore_missing": False}
        if self.uncertain_delete:
            raise TimeoutError("unknown delete result")
        assert not self.volumes[identity]["attachments"]
        del self.volumes[identity]


class Adapter:
    def __init__(self, config, output, event, *, connection, purpose):
        assert purpose == "station"
        self.config, self.connection = config, connection
        self.operations = output / "cloud-operations"
        self.operations.mkdir(mode=0o700)
        self.fail_prepare = False

    def _scope(self):
        assert self.config["project_id"] == "project-one"

    def _capabilities(self):
        self._scope()

    def _remaining(self, seconds):
        return seconds

    def _own(self, resource, **kwargs):
        return _read(self.operations / "station.json")

    def _save(self, resource):
        atomic_write_json(self.operations / "station.json", resource)

    def create(self, role, **kwargs):
        self.connection.calls.append(("create-vm", role))
        assert role == "station" and kwargs == {"user_data": {"packages": ["e2fsprogs"]}}
        value = {
            "role": role,
            "source": "sdk",
            "purpose": "station",
            "project_id": "project-one",
            "server_id": "server-one",
            "boot_volume_id": "boot-one",
            "job_id": "station-one-station",
            "boot_id": "initial-boot",
            "phase": "ready",
            "ssh": {"host": "10.0.0.1"},
        }
        _new_json(self.operations / "station.json", value)
        return value

    def inspect(self, resource):
        value = self._own(resource)
        volumes = [{"id": "boot-one", "size": 20}]
        if any(a["volumeId"] == "data-one" for a in self.connection.attachments):
            assert value["data_volume_ids"] == ["data-one"]
            volumes.append({"id": "data-one", "size": 500})
        return {
            **value,
            "status": "ACTIVE",
            "volumes": volumes,
            "flavor": {"requested": self.connection.flavor, "actual": self.connection.flavor},
        }

    def run_guest(self, resource, argv, **kwargs):
        labels = getattr(self, "_used_guest_labels", set())
        if kwargs["label"] in labels:
            raise ArtifactValidationError("guest command label already used; no effect replay")
        labels.add(kwargs["label"])
        self._used_guest_labels = labels
        p = json.loads(argv[-1])
        if "inspect_only" in p:
            self.connection.calls.append(("unmount", p))
            return {"returncode": 0, "stdout": '{"unmounted":true}'}
        if argv[-2] == module._GUEST_REPAIR_ACCESS:
            self.connection.calls.append(("repair-access", p, kwargs["label"]))
            mode = getattr(self, "mount_mode", "0o711")
            self.mount_mode = "0o755"
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        **{
                            k: p[k]
                            for k in (
                                "boot_id",
                                "data_volume_id",
                                "boot_volume_id",
                                "filesystem_uuid",
                                "data_mountpoint",
                            )
                        },
                        "data_directory": p["data_mountpoint"] + "/measurements",
                        "data_size_gib": 500,
                        "previous_mode": mode,
                        "mode": "0o755",
                        "changed": mode == "0o711",
                        "metadata_mode": "0o600",
                        "measurement_mode": "0o700",
                    }
                ),
            }
        self.connection.calls.append(("prepare", p))
        if self.fail_prepare:
            raise TimeoutError("interrupted format result")
        return {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    **{k: p[k] for k in ("data_volume_id", "boot_volume_id", "filesystem_uuid", "data_mountpoint")},
                    "data_size_gib": 500,
                    "data_directory": p["data_mountpoint"] + "/measurements",
                    "boot_id": "initial-boot",
                }
            ),
        }

    def _absent(self, finder, identity, deadline):
        assert finder(identity, ignore_missing=True) is None

    def delete(self, resource):
        assert not self.connection.volumes
        assert self._own(resource).get("data_volume_ids", []) == []
        self.connection.calls.append(("delete-vm-boot", resource["server_id"]))
        return {**resource, "server_absent": True, "boot_volume_absent": True}

    def verify_absent(self, server_id, boot_volume_id):
        assert (server_id, boot_volume_id) == ("server-one", "boot-one")


@pytest.fixture
def station(config, tmp_path, monkeypatch):
    provider = Provider()
    monkeypatch.setattr(module, "ScenarioCloud", Adapter)
    return module.StationCloud(config, tmp_path / "station", connection=provider), provider


def test_create_retains_original_station_and_disk_with_durable_preformat_uuid(station):
    instance, provider = station
    resource = instance.create()
    receipt = _read(instance.path)
    assert receipt["phase"] == "ready"
    assert resource["data_volume_ids"] == [resource["data_volume_id"]] == ["data-one"]
    assert resource["data_size_gib"] == 500 and resource["boot_volume_id"] == "boot-one"
    assert (
        provider.volumes["data-one"]["metadata"]["palimpsest_filesystem_uuid"]
        == resource["filesystem_uuid"]
        == receipt["filesystem_uuid"]
    )
    assert resource["data_directory"].endswith("/measurements")
    original_calls = copy.deepcopy(provider.calls)
    assert instance.create() == resource
    assert instance.resource() == resource
    assert instance.status()["retained"] is True
    assert provider.calls == original_calls
    assert not any(c[0].startswith("delete") or c[0] == "detach" for c in provider.calls)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("placement", "boot_gib", 40),
        ("approval", "max_vcpus", 2),
        ("approval", "max_ram_mib", 4096),
        ("approval", "allowed_flavor_ids", ["flavor-one", "other"]),
        ("resources", "max_creates", 2),
        ("data_disk", "size_gib", 499),
        ("data_disk", "volume_type", "fast"),
        ("placement", "floating_network_id", "external"),
        ("approval", "max_security_groups", 1),
    ],
)
def test_exact_station_authority_envelope(config, section, key, value):
    config[section][key] = value
    with pytest.raises(ArtifactValidationError):
        module.validate_station(config)


def test_factory_rejects_wrong_actual_hardware_before_create(config, tmp_path, monkeypatch):
    provider = Provider()
    provider.flavor["vcpus"] = 2
    monkeypatch.setattr(module, "ScenarioCloud", Adapter)
    with pytest.raises(ArtifactValidationError, match="exactly 4vCPU"):
        module.StationCloud(config, tmp_path / "station", connection=provider)
    assert not provider.calls


@pytest.mark.parametrize("expiry_boundary", ["guest-readiness", "data-request"])
def test_expired_allocation_after_vm_readiness_never_creates_data_and_vm_is_cleanable(
    station, monkeypatch, expiry_boundary
):
    instance, provider = station
    expiry = module.datetime.fromisoformat(instance.config["approval"]["expires_at"].replace("Z", "+00:00")).timestamp()
    clock = SimpleNamespace(now=expiry - 10)
    monkeypatch.setattr(module.time, "time", lambda: clock.now)
    if expiry_boundary == "guest-readiness":
        create = instance.cloud.create

        def ready_after_expiry(*args, **kwargs):
            resource = create(*args, **kwargs)
            clock.now = expiry
            return resource

        monkeypatch.setattr(instance.cloud, "create", ready_after_expiry)
    else:
        scope = instance.cloud._scope

        def expire_at_data_request():
            scope()
            if instance.path.exists() and _read(instance.path)["phase"] == "create-data-intent":
                clock.now = expiry

        monkeypatch.setattr(instance.cloud, "_scope", expire_at_data_request)
    with pytest.raises(ArtifactValidationError, match="unexpired allocation expiry"):
        instance.create()
    receipt = _read(instance.path)
    assert receipt["phase"] == "create-station-intent"
    assert receipt["server_id"] == "server-one" and receipt["boot_volume_id"] == "boot-one"
    assert not receipt.get("data_volume_id")
    assert [call[0] for call in provider.calls] == ["create-vm"]
    assert instance.status()["retained"] is True
    with pytest.raises(ArtifactValidationError, match="never replay"):
        instance.create()
    assert instance.cleanup()["phase"] == "absent"
    assert [call[0] for call in provider.calls] == ["create-vm", "delete-vm-boot"]


@pytest.fixture
def admission_station(config, tmp_path, monkeypatch):
    # Exercise real ScenarioCloud placement ordering and durable budget state;
    # only unrelated SDK capability enumeration is bypassed for this provider.
    from palimpsest_local import benchmark_scenario_cloud

    provider = Provider()
    provider.current_project_id = "project-one"
    provider.current_user_id = "user-one"
    provider.identity = SimpleNamespace(
        get_project=lambda _: {"id": "project-one", "name": "research", "domain_id": "domain-one"},
        get_domain=lambda _: {"name": "Default"},
    )
    provider.image = SimpleNamespace(get_image=lambda _: {"id": "image-one", "status": "queued"})
    monkeypatch.setattr(benchmark_scenario_cloud.ScenarioCloud, "_capabilities", lambda self: None)
    return module.StationCloud(config, tmp_path / "station", connection=provider), provider


def test_negative_image_admission_cleanup_settles_safe_absence_without_provider_mutation(admission_station):
    instance, provider = admission_station
    with pytest.raises(ArtifactValidationError, match="base image is not active"):
        instance.create()
    assert not list(instance.cloud.operations.glob("*.json"))
    assert not list(instance.cloud.receipts.glob("*.json"))
    assert not instance.cloud.budget.path.exists()
    assert instance.status()["retained"] is True
    result = instance.cleanup()
    assert result["phase"] == "absent"
    assert result["no_vm_request"] is result["no_data_request"] is True
    assert result["server_absent"] is result["boot_volume_absent"] is result["data_volume_absent"] is True
    assert instance.cleanup() == result
    assert instance.status()["retained"] is False
    with pytest.raises(ArtifactValidationError, match="never replay"):
        instance.create()
    assert not provider.calls
    assert not instance.cloud.budget.path.exists()


@pytest.mark.parametrize("receipt_kind", ["role", "nova"])
@pytest.mark.parametrize("already_cleaned", [False, True])
def test_pre_request_certificate_never_ignores_a_late_creation_receipt(
    admission_station, receipt_kind, already_cleaned
):
    instance, provider = admission_station
    with pytest.raises(ArtifactValidationError, match="base image is not active"):
        instance.create()
    if already_cleaned:
        instance.cleanup()
    path = (
        instance.cloud.operations / "station.json"
        if receipt_kind == "role"
        else instance.cloud.receipts / "station-one-station.json"
    )
    _new_json(path, {"phase": "create-intent"})
    before = instance.path.read_bytes()
    with pytest.raises(ArtifactValidationError, match="late creation receipt"):
        instance.cleanup()
    assert instance.path.read_bytes() == before
    assert not provider.calls


def test_missing_receipts_after_interruption_are_not_a_no_request_certificate(admission_station, monkeypatch):
    instance, provider = admission_station

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("controller stopped without proving the request boundary")

    monkeypatch.setattr(instance.cloud, "create", interrupted)
    with pytest.raises(KeyboardInterrupt):
        instance.create()
    assert _read(instance.path)["phase"] == "create-station-intent"
    before = instance.path.read_bytes()
    with pytest.raises(FileNotFoundError):
        instance.cleanup()
    assert instance.path.read_bytes() == before
    assert not provider.calls


def test_pre_request_controller_rejection_cleanup_releases_only_live_slot_not_cumulative_budget(
    admission_station, monkeypatch
):
    instance, provider = admission_station
    instance.cloud._image_at_request = {}
    monkeypatch.setattr(instance.cloud, "_placement", lambda: None)
    monkeypatch.setattr(instance.cloud, "_userdata", lambda *args: "{}")

    def rejected(**kwargs):
        raise ArtifactValidationError("controller rejected before its durable request intent")

    monkeypatch.setattr(instance.cloud.controller, "create", rejected)
    with pytest.raises(ArtifactValidationError, match="controller rejected"):
        instance.create()
    assert _read(instance.path)["phase"] == "create-station-intent"
    assert instance.cleanup()["phase"] == "absent"
    rows = list(_read(instance.cloud.budget.path)["reservations"].values())
    assert len(rows) == 1 and rows[0]["released"] is True
    with pytest.raises(ArtifactValidationError, match="budget exhausted"):
        instance.cloud.budget.reserve("another-role", 20)
    assert not provider.calls


def test_uncertain_vm_create_with_durable_nova_intent_never_certifies_safe_absence(admission_station, monkeypatch):
    instance, provider = admission_station
    instance.cloud._image_at_request = {}
    monkeypatch.setattr(instance.cloud, "_placement", lambda: None)
    monkeypatch.setattr(instance.cloud, "_userdata", lambda *args: "{}")

    def uncertain_create(**kwargs):
        _new_json(instance.cloud.receipts / (kwargs["job_id"] + ".json"), {"phase": "create-intent"})
        raise TimeoutError("provider outcome is unknown")

    monkeypatch.setattr(instance.cloud.controller, "create", uncertain_create)
    monkeypatch.setattr(instance.cloud.controller, "inspect", lambda _: {"uncertain_create": True})
    with pytest.raises(TimeoutError):
        instance.create()
    assert _read(instance.path)["phase"] == "create-station-intent"
    ledger_before = instance.cloud.budget.path.read_bytes()
    with pytest.raises(ArtifactValidationError, match="original create remains uncertain"):
        instance.cleanup()
    (instance.cloud.operations / "station.json").unlink()
    with pytest.raises(FileNotFoundError):
        instance.cleanup()
    assert instance.cloud.budget.path.read_bytes() == ledger_before
    with pytest.raises(ArtifactValidationError, match="never replay"):
        instance.create()
    assert not provider.calls


def test_uncertain_data_post_is_never_replayed_or_implicitly_cleaned(station):
    instance, provider = station
    provider.uncertain_create = True
    with pytest.raises(TimeoutError):
        instance.create()
    assert _read(instance.path)["phase"] == "create-data-intent"
    assert instance.status()["phase"] == "create-data-intent"
    with pytest.raises(ArtifactValidationError, match="never replay"):
        instance.create()
    with pytest.raises(ArtifactValidationError, match="allocation uncertain"):
        instance.cleanup()
    assert [c[0] for c in provider.calls] == ["create-vm", "create-data"]


def test_uncertain_attach_records_allowed_original_id_before_request(station):
    instance, provider = station
    provider.uncertain_attach = True
    with pytest.raises(TimeoutError):
        instance.create()
    assert _read(instance.path)["phase"] == "attach-data-intent"
    assert instance.cloud._own({})["data_volume_ids"] == ["data-one"]
    with pytest.raises(ArtifactValidationError):
        instance.create()
    assert sum(c[0] == "attach" for c in provider.calls) == 1


def test_uncertain_format_is_inspection_only_and_never_reformats(station):
    instance, provider = station
    instance.cloud.fail_prepare = True
    with pytest.raises(TimeoutError):
        instance.create()
    value = _read(instance.path)
    assert value["phase"] == "format-data-intent" and value["filesystem_uuid"]
    assert instance.status()["recovery"].startswith("inspect existing UUID only")
    with pytest.raises(ArtifactValidationError):
        instance.create()
    assert sum(c[0] == "prepare" for c in provider.calls) == 1


def test_optional_guest_status_inspection_never_allows_format(station):
    instance, provider = station
    instance.create()
    assert instance.status(inspect_guest=True)["guest_observed"]["data_size_gib"] == 500
    assert instance.status(inspect_guest=True)["guest_observed"]["data_size_gib"] == 500
    assert provider.calls[-1][0] == "prepare"
    assert provider.calls[-1][1]["allow_format"] is False


def test_original_boot_capacity_and_flavor_remain_bound(station, monkeypatch):
    instance, _ = station
    instance.create()
    inspect = instance.cloud.inspect

    def changed(resource):
        value = inspect(resource)
        value["volumes"][0]["size"] = 500
        return value

    monkeypatch.setattr(instance.cloud, "inspect", changed)
    with pytest.raises(ArtifactValidationError, match="hardware or boot capacity"):
        instance.resource()


@pytest.mark.parametrize("actual", [{"vcpus": 2, "ram": 8192}, {"vcpus": 4, "ram": 4096}, {}])
def test_actual_flavor_must_match_measurement_admission(station, monkeypatch, actual):
    instance, _ = station
    instance.create()
    inspect = instance.cloud.inspect

    def changed(resource):
        value = inspect(resource)
        value["flavor"]["actual"] = actual
        return value

    monkeypatch.setattr(instance.cloud, "inspect", changed)
    with pytest.raises(ArtifactValidationError, match="hardware or boot capacity"):
        instance.resource()


def test_actual_flavor_failure_prevents_data_allocation(station, monkeypatch):
    instance, provider = station
    inspect = instance.cloud.inspect

    def changed(resource):
        value = inspect(resource)
        value["flavor"]["actual"] = {"vcpus": 2, "ram": 8192}
        return value

    monkeypatch.setattr(instance.cloud, "inspect", changed)
    with pytest.raises(ArtifactValidationError, match="no data allocation started"):
        instance.create()
    assert [c[0] for c in provider.calls] == ["create-vm"]


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "foreign"},
        {"size": 20},
        {"attachments": [{"server_id": "foreign"}]},
        {"attachments": [{"server_id": "server-one"}, {"server_id": "server-one"}]},
        {"is_multiattach": True},
        {"metadata": {}},
    ],
)
def test_foreign_or_changed_original_data_blocks_observation_and_cleanup(station, change):
    instance, provider = station
    instance.create()
    provider.volumes["data-one"].update(change)
    before = copy.deepcopy(provider.calls)
    with pytest.raises(ArtifactValidationError):
        instance.resource()
    with pytest.raises(ArtifactValidationError):
        instance.cleanup()
    assert provider.calls == before


def test_nova_retention_false_required_for_both_original_disks(station):
    instance, provider = station
    instance.create()
    provider.attachments[1]["delete_on_termination"] = True
    with pytest.raises(ArtifactValidationError, match="retention"):
        instance.resource()
    with pytest.raises(ArtifactValidationError, match="retention"):
        instance.cleanup()
    assert not any(c[0] == "detach" for c in provider.calls)


def test_explicit_cleanup_detaches_deletes_data_then_vm_boot_only(station):
    instance, provider = station
    instance.create()
    assert instance.cleanup()["phase"] == "absent"
    assert [c[0] for c in provider.calls][-3:] == ["detach", "delete-data", "delete-vm-boot"]
    assert instance.cloud._own({})["data_volume_ids"] == []
    count = len(provider.calls)
    assert instance.cleanup()["phase"] == "absent"
    assert len(provider.calls) == count


def test_uncertain_data_delete_is_not_replayed_and_preserves_boot_vm(station):
    instance, provider = station
    instance.create()
    provider.uncertain_delete = True
    with pytest.raises(TimeoutError):
        instance.cleanup()
    assert _read(instance.path)["phase"] == "delete-data-intent"
    with pytest.raises(ArtifactValidationError, match="no destructive replay"):
        instance.cleanup()
    assert sum(c[0] == "delete-data" for c in provider.calls) == 1
    assert not any(c[0] == "delete-vm-boot" for c in provider.calls)


def guest_format_policy(
    *,
    filesystem="",
    filesystem_uuid="",
    size=500,
    root_matches=True,
    data_is_root=False,
    mounts=None,
    signatures=None,
    allow_format=True,
    occupied=False,
):
    """Exercise production guest selection/format guards with inert OS adapters."""
    state = {"filesystem": filesystem, "uuid": filesystem_uuid, "commands": []}
    params = {
        "data_volume_id": "11111111-2222-3333-4444-555555555555",
        "boot_volume_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "filesystem_uuid": "12345678-1234-1234-1234-123456789abc",
        "data_mountpoint": module._MOUNTPOINT,
        "allow_format": allow_format,
        "user": "researcher",
    }

    class GuestPath:
        def __init__(self, value):
            self.value = str(value)

        def __str__(self):
            return self.value

        @property
        def parents(self):
            return []

        def __truediv__(self, name):
            return GuestPath(self.value + "/" + name)

        def is_symlink(self):
            return False

        def is_dir(self):
            return True

        def mkdir(self, **kwargs):
            return None

        def iterdir(self):
            return iter([])

        def read_text(self):
            return "current-boot"

    def check_output(argv, **kwargs):
        if argv[0] == "findmnt":
            target = argv[argv.index("--target") + 1]
            m = {"source": "/dev/vda1", "target": "/", "maj:min": "252:1"}
            if target != "/" and occupied:
                m.update(target=module._MOUNTPOINT, uuid="foreign", **{"maj:min": "8:1"})
            return json.dumps({"filesystems": [m]})
        if argv[0] == "lsblk" and "--inverse" in argv:
            return json.dumps(
                {
                    "blockdevices": [
                        {
                            "name": "/dev/vda",
                            "serial": params["boot_volume_id"] if root_matches else "foreign",
                            "maj:min": "252:0",
                        }
                    ]
                }
            )
        if argv[0] == "lsblk" and "--nodeps" in argv:
            return state["filesystem"] if argv[-2] == "FSTYPE" else state["uuid"]
        if argv[0] == "lsblk":
            return json.dumps(
                {
                    "blockdevices": [
                        {
                            "name": "/dev/vdb",
                            "type": "disk",
                            "size": size * 1024**3,
                            "serial": params["data_volume_id"][:20],
                            "mountpoints": mounts or [],
                            "uuid": state["uuid"],
                            "maj:min": "252:0" if data_is_root else "252:16",
                        }
                    ]
                }
            )
        if argv[0] == "wipefs":
            return json.dumps({"signatures": signatures or []})
        if argv[0] == "blkid":
            return state["uuid"] if "UUID" in argv else state["filesystem"]
        raise AssertionError(argv)

    def run(argv, **kwargs):
        state["commands"].append(argv)
        if argv[0] == "blkid":
            return SimpleNamespace(returncode=2, stdout="")
        assert argv[:5] == ["mkfs.ext4", "-F", "-E", "nodiscard", "-U"]
        assert kwargs["stdin"] == -3 and kwargs["timeout"] == 300
        state.update(filesystem="ext4", uuid=argv[5])
        return SimpleNamespace(returncode=0)

    imports = {
        "os": SimpleNamespace(geteuid=lambda: 0, stat=lambda _: SimpleNamespace(st_mode=stat.S_IFBLK)),
        "pwd": SimpleNamespace(),
        "subprocess": SimpleNamespace(check_output=check_output, run=run, DEVNULL=-3),
        "sys": SimpleNamespace(
            argv=["guest", json.dumps(params)], exit=lambda code: (_ for _ in ()).throw(SystemExit(code))
        ),
        "pathlib": SimpleNamespace(Path=GuestPath),
    }
    original_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        return imports[name] if name in imports else original_import(name, *args, **kwargs)

    namespace = {"__builtins__": {**vars(builtins), "__import__": import_module}}
    exec(module._GUEST_DISK.split("\ncurrent=json.loads", 1)[0], namespace)
    return state


def test_guest_formats_only_blank_exact_owned_device_with_predetermined_uuid():
    state = guest_format_policy()
    commands = [c for c in state["commands"] if c[0] == "mkfs.ext4"]
    assert commands == [
        ["mkfs.ext4", "-F", "-E", "nodiscard", "-U", "12345678-1234-1234-1234-123456789abc", "/dev/vdb"]
    ]


def test_guest_reuses_exact_existing_filesystem_without_format():
    state = guest_format_policy(filesystem="ext4", filesystem_uuid="12345678-1234-1234-1234-123456789abc")
    assert not state["commands"]


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"filesystem": "ext4", "filesystem_uuid": "foreign"}, "unknown existing filesystem"),
        ({"filesystem": "xfs", "filesystem_uuid": "foreign"}, "unknown existing filesystem"),
        ({"signatures": [{"type": "gpt"}]}, "unknown disk signatures"),
        ({"size": 20}, "whole 500GiB"),
        ({"root_matches": False}, "original boot volume"),
        ({"data_is_root": True}, "boot/root device"),
        ({"mounts": ["/other"]}, "mounted elsewhere"),
        ({"allow_format": False}, "inspect-only"),
        ({"occupied": True}, "occupied by another filesystem"),
    ],
)
def test_guest_never_formats_unknown_occupied_root_or_wrong_device(arguments, message):
    with pytest.raises(RuntimeError, match=message):
        guest_format_policy(**arguments)


def test_explicit_access_repair_retains_unique_intents_and_noop_repeat(station):
    instance, provider = station
    instance.create()
    original = instance.path.read_bytes()
    before = copy.deepcopy(provider.calls)
    first = instance.repair_access()
    second = instance.repair_access()
    assert first["status"] == second["status"] == "success"
    assert first["changed"] is True and second["changed"] is False
    assert first["operation_id"] != second["operation_id"]
    assert first["command_label"] != second["command_label"]
    assert instance.path.read_bytes() == original
    assert [c[0] for c in provider.calls[len(before) :]] == ["repair-access", "repair-access"]
    for result in (first, second):
        assert result["station"]["boot_id"] == "initial-boot"
        assert result["station"]["flavor"] == {"vcpus": 4, "ram_mib": 8192}
        assert (
            _read(instance.output / ("access-repair-" + result["operation_id"] + ".intent.json"))["status"] == "intent"
        )
        assert _read(result["result_path"]) == result


@pytest.mark.parametrize(
    "change",
    [{"size": 20}, {"project_id": "foreign"}, {"attachments": [{"server_id": "foreign"}]}, {"is_multiattach": True}],
)
def test_access_repair_rejects_foreign_provider_before_guest(station, change):
    instance, provider = station
    instance.create()
    provider.volumes["data-one"].update(change)
    before = copy.deepcopy(provider.calls)
    with pytest.raises(ArtifactValidationError, match="original station validation failed"):
        instance.repair_access()
    assert provider.calls == before
    results = list(instance.output.glob("access-repair-*.json"))
    assert len(results) == 2
    assert sorted(_read(p)["status"] for p in results) == ["failed", "intent"]


def test_access_repair_sanitizes_sdk_or_guest_failure_and_preserves_intent(station, monkeypatch):
    instance, _ = station
    instance.create()

    def interrupted(*args, **kwargs):
        assert list(instance.output.glob("access-repair-*.intent.json"))
        raise TimeoutError("authorization=private-provider-value")

    monkeypatch.setattr(instance.cloud, "run_guest", interrupted)
    with pytest.raises(ArtifactValidationError, match="guest permission repair failed") as error:
        instance.repair_access()
    assert "private-provider-value" not in str(error.value)
    results = list(instance.output.glob("access-repair-*.json"))
    assert len(results) == 2
    assert all("private-provider-value" not in p.read_text() for p in results)


def test_measurement_descriptor_walk_requires_readable_mount_root(tmp_path, monkeypatch):
    import os

    from palimpsest_local import environment_benchmark

    mount = tmp_path / "mount"
    child = mount / "measurements"
    child.mkdir(parents=True, mode=0o700)
    mount.chmod(0o711)
    real_open = os.open

    def unprivileged_open(path, flags, *args, **kwargs):
        # Emulate a nonowner on the root-owned mount, even when pytest is root.
        if str(path) == mount.name and flags & os.O_DIRECTORY and not (mount.stat().st_mode & stat.S_IROTH):
            raise PermissionError("O_RDONLY descriptor walk needs directory read permission")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(environment_benchmark.os, "open", unprivileged_open)
    with pytest.raises(PermissionError, match="directory read permission"):
        environment_benchmark._open_directory(child)
    mount.chmod(0o755)
    os.close(environment_benchmark._open_directory(child))
    assert stat.S_IMODE(child.stat().st_mode) == 0o700
    assert "os.chmod(mount,0o755)" in module._GUEST_DISK


def guest_access_policy(
    *,
    mode=0o711,
    filesystem_uuid=None,
    filesystem="ext4",
    serial=None,
    size=500,
    owner=0,
    child_mode=0o700,
    child_owner=1000,
    metadata_mode=0o600,
    metadata_matches=True,
    foreign_entry=False,
    symlink=False,
    data_is_root=False,
    boot_matches=True,
    mounted=True,
):
    """Run the actual repair predicate against inert block/filesystem adapters."""
    import io
    import os

    params = {
        "data_volume_id": "11111111-2222-3333-4444-555555555555",
        "boot_volume_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "filesystem_uuid": "12345678-1234-1234-1234-123456789abc",
        "data_mountpoint": module._MOUNTPOINT,
        "boot_id": "current-boot",
        "user": "researcher",
    }
    state = {"mode": mode, "changes": [], "commands": [], "output": []}
    device_number = 25216

    class GuestPath:
        def __init__(self, value):
            self.value = str(value)

        def __str__(self):
            return self.value

        @property
        def parents(self):
            return []

        def __truediv__(self, name):
            return GuestPath(self.value + "/" + name)

        def is_symlink(self):
            return symlink

        def read_text(self):
            return "current-boot" if boot_matches else "changed-boot"

    def metadata(kind, uid, permissions, *, dev=device_number):
        return SimpleNamespace(
            st_mode=kind | permissions,
            st_uid=uid,
            st_gid=uid,
            st_dev=dev,
            st_rdev=device_number,
            st_ino=123,
            st_nlink=1,
        )

    def fstat(fd):
        if fd == 1:
            return metadata(stat.S_IFDIR, owner, state["mode"])
        if fd == 2:
            return metadata(stat.S_IFREG, 0, metadata_mode)
        assert fd == 3
        return metadata(stat.S_IFDIR, child_owner, child_mode)

    def open_path(path, flags, **kwargs):
        assert flags & os.O_NOFOLLOW
        if str(path) == module._MOUNTPOINT:
            return 1
        assert kwargs["dir_fd"] == 1
        return {".palimpsest-station.json": 2, "measurements": 3}[path]

    class Cache(io.StringIO):
        def fileno(self):
            return 2

    def fdopen(fd, mode):
        assert (fd, mode) == (2, "r")
        value = {k: params[k] for k in ("data_volume_id", "boot_volume_id", "filesystem_uuid", "data_mountpoint")}
        if not metadata_matches:
            value["data_volume_id"] = "foreign"
        return Cache(json.dumps(value))

    def check_output(argv, **kwargs):
        state["commands"].append(argv)
        if argv[0] == "findmnt":
            target = argv[argv.index("--target") + 1]
            value = {"source": "/dev/vda1", "target": "/", "maj:min": "252:1"}
            if target != "/" and mounted:
                value.update(
                    target=module._MOUNTPOINT,
                    fstype=filesystem,
                    uuid=filesystem_uuid or params["filesystem_uuid"],
                    options="rw,nodev,nosuid",
                    **{"maj:min": "252:16"},
                )
            return json.dumps({"filesystems": [value]})
        assert argv[0] == "lsblk"
        if "--inverse" in argv:
            return json.dumps(
                {"blockdevices": [{"name": "/dev/vda", "serial": params["boot_volume_id"], "maj:min": "252:0"}]}
            )
        return json.dumps(
            {
                "blockdevices": [
                    {
                        "name": "/dev/vdb",
                        "type": "disk",
                        "size": size * 1024**3,
                        "serial": serial or params["data_volume_id"][:20],
                        "fstype": filesystem,
                        "uuid": filesystem_uuid or params["filesystem_uuid"],
                        "maj:min": "252:0" if data_is_root else "252:16",
                        "mountpoints": [module._MOUNTPOINT] if mounted else [],
                    }
                ]
            }
        )

    def chmod(fd, value):
        assert fd == 1 and value == 0o755
        state["changes"].append((fd, value))
        state["mode"] = value

    def guest_stat(path, **kwargs):
        if str(path) == "/dev/vdb":
            return metadata(stat.S_IFBLK, 0, 0o600)
        if str(path) == module._MOUNTPOINT:
            return fstat(1)
        assert path == "lost+found" and kwargs == {"dir_fd": 1, "follow_symlinks": False}
        return metadata(stat.S_IFDIR, 0, 0o700)

    imports = {
        "os": SimpleNamespace(
            geteuid=lambda: 0,
            stat=guest_stat,
            fstat=fstat,
            open=open_path,
            fdopen=fdopen,
            close=lambda fd: None,
            listdir=lambda fd: (
                [".palimpsest-station.json", "measurements", "lost+found"] + (["foreign"] if foreign_entry else [])
            ),
            fchmod=chmod,
            fsync=lambda fd: None,
            O_RDONLY=os.O_RDONLY,
            O_DIRECTORY=os.O_DIRECTORY,
            O_NOFOLLOW=os.O_NOFOLLOW,
        ),
        "pwd": SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_uid=1000, pw_gid=1000)),
        "subprocess": SimpleNamespace(check_output=check_output),
        "sys": SimpleNamespace(argv=["guest", json.dumps(params)]),
        "pathlib": SimpleNamespace(Path=GuestPath),
    }
    original_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        return imports[name] if name in imports else original_import(name, *args, **kwargs)

    namespace = {
        "__builtins__": {
            **vars(builtins),
            "__import__": import_module,
            "print": lambda text: state["output"].append(json.loads(text)),
        }
    }
    try:
        exec(module._GUEST_REPAIR_ACCESS, namespace)
    except Exception:
        assert not state["changes"], "a failing ownership predicate must never chmod"
        raise
    return state


@pytest.mark.parametrize("mode,changed", [(0o711, True), (0o755, False)])
def test_guest_access_repair_changes_only_exact_mount_root(mode, changed):
    state = guest_access_policy(mode=mode)
    assert state["mode"] == 0o755
    assert state["changes"] == ([(1, 0o755)] if changed else [])
    assert state["output"][0]["changed"] is changed
    assert state["output"][0]["metadata_mode"] == "0o600"
    assert state["output"][0]["measurement_mode"] == "0o700"
    assert {c[0] for c in state["commands"]} == {"findmnt", "lsblk"}


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"filesystem_uuid": "foreign"}, "original data filesystem"),
        ({"filesystem": "xfs"}, "original data filesystem"),
        ({"serial": "foreign"}, "data serial"),
        ({"size": 20}, "whole 500GiB"),
        ({"data_is_root": True}, "boot/root device"),
        ({"boot_matches": False}, "original guest boot"),
        ({"mounted": False}, "disk mount differs"),
        ({"mode": 0o777}, "root permissions"),
        ({"owner": 1000}, "root ownership"),
        ({"metadata_mode": 0o644}, "metadata ownership"),
        ({"metadata_matches": False}, "metadata differs"),
        ({"child_mode": 0o755}, "measurement directory"),
        ({"child_owner": 2000}, "measurement directory"),
        ({"foreign_entry": True}, "foreign mount root entry"),
        ({"symlink": True}, "mountpoint symlink"),
    ],
)
def test_guest_access_repair_rejects_foreign_filesystem_or_metadata_without_effect(arguments, message):
    with pytest.raises(RuntimeError, match=message):
        guest_access_policy(**arguments)
