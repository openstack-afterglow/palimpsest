from __future__ import annotations

import copy
import errno
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import pytest

from palimpsest_local import cloud_cli, cloud_env
from palimpsest_local.cloud_env import CONSUMER_SCRIPT, SCHEMA, CloudEnv, CloudEnvError


def image():
    return {
        "layers": [{"digest": "sha256:" + "a" * 64, "size": 10}],
        "runtime": {"user": "0", "env": [], "working_dir": "/", "entrypoint": [], "cmd": []},
    }


@pytest.fixture
def environment(tmp_path, monkeypatch):
    env = CloudEnv("test", root=tmp_path)
    conn = Obj(
        current_project_id="project",
        current_user_id="user",
        compute=Mock(),
        network=Mock(),
        shared_file_system=Mock(),
        block_storage=Mock(),
        image=Mock(),
    )
    env.state = {
        "schema": SCHEMA,
        "name": "test",
        "cloud": "test-cloud",
        "project_id": "project",
        "user_id": "user",
        "marker": "a" * 32,
        "resources": {},
        "images": {"app": image()},
        "external_network_id": "external",
        "network_id": "network",
        "image_id": "image",
        "share_gib": 50,
        "share_type": "cephfs",
        "dns": [],
    }
    env.save()
    env._connection = conn
    monkeypatch.setattr(cloud_cli, "_connect", Mock(return_value=conn))
    return env, conn


def recorded(env, kind, role, **values):
    return env.record(kind, role, {"status": "confirmed", **values})


def owned(env, **values):
    return Obj(
        project_id="project",
        metadata={
            "palimpsest_cloud_env": "test",
            "palimpsest_env_id": env.state["marker"],
            **values.pop("metadata", {}),
        },
        **values,
    )


def sdk_errors():
    return pytest.importorskip("openstack.exceptions")


def http_response(status):
    requests = pytest.importorskip("requests")
    response = requests.Response()
    response.status_code = status
    response.url = "https://openstack.invalid/resource"
    response.request = requests.Request("GET", response.url).prepare()
    response._content = b""
    return response


def test_wrong_project_reauthentication_prevents_all_effects(environment):
    env, conn = environment
    conn.current_project_id = "wrong"
    recorded(env, "shares", "layers", id="share")
    with pytest.raises(CloudEnvError, match="stored environment project"):
        env.down()
    assert not conn.compute.mock_calls and not conn.network.mock_calls
    assert not conn.shared_file_system.mock_calls and not conn.block_storage.mock_calls
    assert env.resource("shares", "layers")["id"] == "share"


def test_changed_credential_user_prevents_keypair_scoped_cleanup(environment):
    env, conn = environment
    conn.current_user_id = "different-user"
    with pytest.raises(CloudEnvError, match="keypair-owning"):
        env.down()
    assert not conn.compute.mock_calls and not conn.network.mock_calls


def test_operation_reloads_state_instead_of_overwriting_newer_resource(environment):
    env, _ = environment
    stale = CloudEnv("test", root=env.directory.parent)
    recorded(env, "shares", "layers", id="new-share")
    assert stale.status()["resources"]["shares"]["layers"]["id"] == "new-share"


def test_operation_holds_exclusive_flock_while_reauthenticating(environment, monkeypatch):
    import fcntl

    env, conn = environment

    def authenticate(cloud):
        fd = os.open(env.directory / "operation.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        return conn

    monkeypatch.setattr(cloud_cli, "_connect", authenticate)
    env.down()


def up_args(tmp_path):
    archive = tmp_path / "buildkit.tar.gz"
    archive.write_bytes(b"archive")
    return dict(
        cloud="test-cloud",
        network="network",
        external_network="external",
        image="image",
        worker_flavor="flavor",
        share_gib=50,
        share_type="cephfs",
        ssh_cidrs=["192.0.2.1/32"],
        security_group=None,
        worker_boot_gib=80,
        dns=["1.1.1.1"],
        buildkit_archive=archive,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("dns", ["1.1.1.1'; touch /tmp/no"]),
        ("dns", ["fe80::1%$(touch /tmp/no)"]),
        ("ssh_cidrs", ["192.0.2.1/24"]),
        ("worker_boot_gib", 0),
    ],
)
def test_malformed_up_input_rejected_before_provider_calls(tmp_path, monkeypatch, field, value):
    env = CloudEnv("new", root=tmp_path)
    connect = Mock()
    monkeypatch.setattr(cloud_cli, "_connect", connect)
    args = up_args(tmp_path)
    args[field] = value
    with pytest.raises(CloudEnvError):
        env.up(**args)
    connect.assert_not_called()
    assert not env.state_path.exists()


def test_repeated_up_rejects_changed_immutable_arguments(environment, tmp_path):
    env, conn = environment
    args = up_args(tmp_path)
    env.state["settings"] = {key: value for key, value in args.items() if key != "buildkit_archive"}
    env.save()
    args["share_gib"] = 51
    with pytest.raises(CloudEnvError, match="immutable"):
        env.up(**args)
    assert not conn.network.mock_calls and not conn.compute.mock_calls


@pytest.mark.parametrize("failure", ["lookup", "inactive-image"])
def test_failed_initial_resolution_never_publishes_partial_state(tmp_path, monkeypatch, failure):
    env = CloudEnv("new", root=tmp_path)
    args = up_args(tmp_path)
    monkeypatch.setattr(cloud_env, "BUILDKIT_SHA256", hashlib.sha256(b"archive").hexdigest())
    conn = Obj(
        current_project_id="project",
        current_user_id="user",
        network=Mock(),
        image=Mock(),
        compute=Mock(),
        shared_file_system=Mock(),
    )
    conn.network.find_network.side_effect = [
        Obj(id="network", project_id="project"),
        Obj(id="external", is_router_external=True),
    ]
    if failure == "lookup":
        conn.image.find_image.side_effect = RuntimeError("lookup unavailable")
    else:
        conn.image.find_image.return_value = Obj(id="image", status="queued")
    monkeypatch.setattr(cloud_cli, "_connect", lambda cloud: conn)
    with pytest.raises((CloudEnvError, RuntimeError)):
        env.up(**args)
    assert not env.state_path.exists()
    assert not conn.compute.mock_calls and not conn.shared_file_system.mock_calls


def test_uncertain_share_create_persists_intent_and_never_replays(environment):
    env, conn = environment

    def uncertain(**kwargs):
        state = json.loads(env.state_path.read_text())
        assert state["resources"]["shares"]["layers"]["status"] == "creating"
        raise TimeoutError("POST outcome unknown")

    conn.shared_file_system.create_share.side_effect = uncertain
    with pytest.raises(TimeoutError):
        env._ensure_share()
    with pytest.raises(CloudEnvError, match="HOLD"):
        env._ensure_share()
    with pytest.raises(CloudEnvError, match="HOLD"):
        env.down()
    assert conn.shared_file_system.create_share.call_count == 1
    conn.shared_file_system.delete_share.assert_not_called()


def test_keypair_collision_is_held_and_never_deleted(environment):
    env, conn = environment
    env.key_path.write_text("private")
    env.key_path.with_suffix(".pub").write_text("ssh-ed25519 public")
    conn.compute.create_keypair.side_effect = RuntimeError("name collision")
    with pytest.raises(RuntimeError):
        env._ensure_keypair()
    with pytest.raises(CloudEnvError, match="HOLD"):
        env._ensure_keypair()
    with pytest.raises(CloudEnvError, match="HOLD"):
        env.down()
    assert conn.compute.create_keypair.call_count == 1
    conn.compute.delete_keypair.assert_not_called()


def test_uncertain_security_rule_is_not_replayed_on_group_retry(environment):
    env, conn = environment
    recorded(env, "security_groups", "env", id="sg", description="marker", owned=True)
    conn.network.get_security_group.return_value = Obj(id="sg", project_id="project", description="marker")
    conn.network.create_security_group_rule.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        env._ensure_security_group(["192.0.2.1/32"], None)
    with pytest.raises(CloudEnvError, match="HOLD"):
        env._ensure_security_group(["192.0.2.1/32"], None)
    assert conn.network.create_security_group_rule.call_count == 1


def test_503_is_not_absence_and_share_record_survives(environment):
    errors = sdk_errors()
    env, conn = environment
    recorded(env, "shares", "layers", id="share")
    conn.shared_file_system.get_share.side_effect = errors.HttpException("unavailable", response=http_response(503))
    with pytest.raises(errors.HttpException):
        env.down()
    assert env.resource("shares", "layers")["id"] == "share"
    conn.shared_file_system.delete_share.assert_not_called()


def test_only_sdk_404_is_absence(environment):
    errors = sdk_errors()
    env, _ = environment
    getter = Mock(side_effect=errors.NotFoundException("not a 404", response=http_response(503)))
    with pytest.raises(errors.NotFoundException):
        env._get(getter, "id")
    getter.side_effect = errors.NotFoundException("gone", response=http_response(404))
    assert env._get(getter, "id") is None
    getter.side_effect = None
    getter.return_value = None
    with pytest.raises(CloudEnvError, match="without an exact HTTP 404"):
        env._get(getter, "id")


def server_setup(env, conn, monkeypatch, role="worker"):
    server = owned(env, id="server", attached_volumes=[{"id": "boot"}], metadata={"palimpsest_role": role})
    recorded(env, "servers", role, id="server", flavor="flavor", boot_gib=80, provisioned_at=1)
    conn.compute.get_server.return_value = server
    conn.compute.wait_for_server.return_value = server
    conn.block_storage.get_volume.return_value = Obj(
        project_id="project",
        attachments=[{"server_id": "server"}],
        size=80,
        volume_image_metadata={"image_id": "image"},
    )
    conn.network.ports.return_value = [Obj(id="port")]
    conn.network.get_port.return_value = Obj(id="port", project_id="project", device_id="server", network_id="network")
    monkeypatch.setattr(env, "_pin_host_key", Mock())
    monkeypatch.setattr(env, "_wait_ssh", Mock(return_value={"status": "done", "ready": True}))
    return server


def test_recorded_unbound_floating_ip_reconciles_exact_port_without_new_post(environment, monkeypatch):
    env, conn = environment
    server_setup(env, conn, monkeypatch)
    description = "palimpsest:" + env.state["marker"] + ":worker"
    recorded(
        env,
        "floating_ips",
        "worker",
        id="fip",
        description=description,
        port_id="port",
        server_id="server",
        address="192.0.2.5",
    )
    unbound = Obj(
        project_id="project",
        description=description,
        floating_network_id="external",
        floating_ip_address="192.0.2.5",
        port_id=None,
    )
    bound = copy.copy(unbound)
    bound.port_id = "port"
    conn.network.get_ip.side_effect = [unbound, bound]
    result = env._create_server("worker", flavor="flavor", boot_gib=80, packages=[])
    assert result["address"] == "192.0.2.5"
    conn.network.create_ip.assert_not_called()
    conn.network.update_ip.assert_called_once_with("fip", port_id="port")


def test_foreign_floating_ip_binding_is_not_reassigned(environment, monkeypatch):
    env, conn = environment
    server_setup(env, conn, monkeypatch)
    recorded(
        env,
        "floating_ips",
        "worker",
        id="fip",
        description="marker",
        port_id="port",
        server_id="server",
        address="192.0.2.5",
    )
    conn.network.get_ip.return_value = Obj(
        project_id="project",
        description="marker",
        floating_network_id="external",
        floating_ip_address="192.0.2.5",
        port_id="foreign",
    )
    with pytest.raises(CloudEnvError, match="association"):
        env._create_server("worker", flavor="flavor", boot_gib=80, packages=[])
    conn.network.update_ip.assert_not_called()
    conn.network.create_ip.assert_not_called()


def test_boot_volume_ids_survive_failure_then_server_404_on_retry(environment, monkeypatch):
    errors = sdk_errors()
    env, conn = environment
    server = server_setup(env, conn, monkeypatch)
    conn.compute.get_server.side_effect = [
        server,
        errors.NotFoundException("gone", response=http_response(404)),
        errors.NotFoundException("gone", response=http_response(404)),
    ]

    def pending(getter, identity, **kwargs):
        if identity == "boot":
            raise CloudEnvError("boot still present")

    monkeypatch.setattr(env, "_wait_absent", pending)
    for _ in range(2):
        with pytest.raises(CloudEnvError, match="boot still present"):
            env._delete_server("worker")
        persisted = json.loads(env.state_path.read_text())["resources"]["servers"]["worker"]
        assert persisted["boot_volumes"] == ["boot"] and persisted["status"] == "deleting"
    assert conn.compute.delete_server.call_count == 1
    monkeypatch.setattr(env, "_wait_absent", Mock())
    assert env._delete_server("worker")["boot_volumes_absent"] == ["boot"]
    assert env.resource("servers", "worker") is None


def test_server_read_failure_prevents_delete_and_retains_record(environment):
    errors = sdk_errors()
    env, conn = environment
    recorded(env, "servers", "worker", id="server", boot_volumes=["boot"])
    conn.compute.get_server.side_effect = errors.HttpException("unavailable", response=http_response(503))
    with pytest.raises(errors.HttpException):
        env._delete_server("worker")
    conn.compute.delete_server.assert_not_called()
    assert env.resource("servers", "worker")["boot_volumes"] == ["boot"]


def test_revocation_waits_for_actual_rule_absence(environment, monkeypatch):
    sdk_errors()
    env, conn = environment
    recorded(env, "access_rules", "consumer", id="rule", share_id="share", access_to="identity", level="ro")
    conn.shared_file_system.get_share.return_value = owned(env)
    rule = Obj(id="rule", access_to="identity", access_level="ro", access_type="cephx")
    conn.shared_file_system.access_rules.side_effect = [[rule], [rule], []]
    sleeps = []
    monkeypatch.setattr(cloud_env.time, "sleep", lambda seconds: sleeps.append(seconds))
    env._revoke("consumer")
    assert sleeps == [3]
    assert conn.shared_file_system.access_rules.call_count == 3
    assert env.resource("access_rules", "consumer") is None


def run_setup(env, monkeypatch, *, returncode=0):
    monkeypatch.setattr(env, "grant", Mock())
    monkeypatch.setattr(
        env, "_create_server", Mock(return_value={"id": "server", "flavor": "flavor", "address": "192.0.2.5"})
    )
    monkeypatch.setattr(env, "_send_mount", Mock())
    monkeypatch.setattr(
        env,
        "ssh",
        Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                json.dumps({"command": {"returncode": returncode, "stdout": "output", "stderr": ""}}).encode(),
                b"",
            )
        ),
    )
    monkeypatch.setattr(env, "_delete_server", Mock(return_value={"server": "absent"}))
    monkeypatch.setattr(env, "_revoke", Mock())


def run_args():
    return dict(tag="app", command=["/bin/true"], flavor="flavor", boot_gib=20, keep=False, verify_blobs=True)


def test_consumer_nonzero_preserves_full_report_and_fails_cli(environment, monkeypatch, capsys):
    env, _ = environment
    run_setup(env, monkeypatch, returncode=7)
    report = env.run(**run_args())
    assert report["exit_code"] == 1 and report["status"] == "command_failed"
    assert json.loads(env.state_path.read_text())["runs"][report["role"]] == report
    monkeypatch.setattr(cloud_env, "dispatch", lambda args: report)
    assert cloud_cli.dispatch(Obj(cloud_operation="layer-run")) == 1
    assert json.loads(capsys.readouterr().out) == report


def test_cleanup_failure_preserves_successful_guest_output_and_fails(environment, monkeypatch):
    env, _ = environment
    run_setup(env, monkeypatch)
    env._delete_server.side_effect = RuntimeError("cleanup service failed")
    report = env.run(**run_args())
    assert report["exit_code"] == 1 and report["status"] == "cleanup_failed"
    assert report["guest"]["command"]["stdout"] == "output"
    assert report["cleanup_error"] == "RuntimeError"
    assert json.loads(env.state_path.read_text())["runs"][report["role"]] == report
    env._revoke.assert_not_called()


def test_exception_during_assembly_still_preserves_report_and_cleanup(environment, monkeypatch):
    env, _ = environment
    run_setup(env, monkeypatch)
    env.ssh.side_effect = TimeoutError("SSH timeout")
    report = env.run(**run_args())
    assert report["error"] == "TimeoutError" and report["exit_code"] == 1
    assert report["cleanup"]["ro_access"] == "revoked"
    assert report["role"] in json.loads(env.state_path.read_text())["runs"]


def test_uncertain_nova_create_does_not_trigger_automatic_cleanup(environment, monkeypatch):
    env, _ = environment
    run_setup(env, monkeypatch)

    def uncertain(role, **kwargs):
        env._intent("servers", role)
        raise TimeoutError()

    env._create_server.side_effect = uncertain
    report = env.run(**run_args())
    assert "HOLD" in report["cleanup_error"] and report["exit_code"] == 1
    env._delete_server.assert_not_called()
    env._revoke.assert_not_called()


@pytest.mark.parametrize("change", ["digest", "size", "user", "env", "working_dir"])
def test_malformed_image_is_rejected_before_consumer_effects(environment, monkeypatch, change):
    env, _ = environment
    bad = env.state["images"]["app"]
    if change == "digest":
        bad["layers"][0]["digest"] = "sha256:../../etc/passwd"
    elif change == "size":
        bad["layers"][0]["size"] = True
    else:
        bad["runtime"][change] = {"user": "1000", "env": ["bad"], "working_dir": "relative"}[change]
    env.save()
    grant = Mock()
    monkeypatch.setattr(env, "grant", grant)
    report = env.run(**run_args())
    assert report["exit_code"] == 1 and report["status"] == "failed"
    grant.assert_not_called()


@pytest.mark.parametrize(
    "code,status,ready",
    [
        (0, {"status": "done", "errors": []}, True),
        (2, {"status": "done", "recoverable_errors": {"module": ["error"]}}, False),
        (1, {"status": "error"}, False),
        (0, {"status": "running"}, False),
    ],
)
def test_cloud_init_readiness_distinguishes_error_and_success(environment, monkeypatch, code, status, ready):
    env, _ = environment
    monkeypatch.setattr(
        cloud_env.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], code, json.dumps(status).encode(), b"")),
    )
    result = env._wait_ssh("192.0.2.5", time.monotonic() + 10)
    assert result["ready"] is ready and result["returncode"] == code


def guest_script(monkeypatch, *, denied_errno=errno.EACCES, workdir="/", command_timeout=False):
    config = {**image(), "verify": False, "command": ["/bin/true"]}
    config["runtime"]["working_dir"] = workdir
    stdout = io.StringIO()
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "chroot" and command_timeout:
            raise subprocess.TimeoutExpired(argv, 1500, output=b"partial", stderr=b"timeout")
        output = "ro,secret=DO-NOT-EXPOSE,name=identity" if argv[0] == "findmnt" else ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", io.StringIO(json.dumps(config)))
        patch.setattr(sys, "stdout", stdout)
        patch.setattr(os, "open", Mock(side_effect=OSError(denied_errno, "probe denied")))
        patch.setattr(os, "makedirs", Mock())
        patch.setattr(os, "stat", Mock(return_value=Obj(st_size=10)))
        patch.setattr(subprocess, "run", execute)
        try:
            exec(CONSUMER_SCRIPT, {})
        except SystemExit as error:
            return error.code, stdout.getvalue(), calls
    return 0, stdout.getvalue(), calls


@pytest.mark.parametrize("denied_errno", [errno.ENOSPC, errno.EIO, errno.EEXIST, errno.ENOENT])
def test_read_only_probe_rejects_non_permission_errors(monkeypatch, denied_errno):
    code, output, calls = guest_script(monkeypatch, denied_errno=denied_errno)
    assert code and "unexpectedly" in code and not output and not calls


@pytest.mark.parametrize("denied_errno", [errno.EACCES, errno.EROFS])
def test_guest_uses_literal_positional_workdir_and_redacts_auth_options(monkeypatch, denied_errno):
    workdir = '/$(touch /tmp/never)`echo never`/"unicode-한글"'
    code, output, calls = guest_script(monkeypatch, denied_errno=denied_errno, workdir=workdir)
    assert code == 0
    report = json.loads(output)
    assert "DO-NOT-EXPOSE" not in output
    argv = next(call for call in calls if call[0] == "chroot")
    assert argv[-2:] == [workdir, "/bin/true"]
    assert report["command"]["returncode"] == 0


def test_guest_command_timeout_preserves_partial_command_report(monkeypatch):
    code, output, _ = guest_script(monkeypatch, command_timeout=True)
    report = json.loads(output)
    assert code == 0 and report["command_timeout"] is True
    assert report["command"] == {"argv": ["/bin/true"], "returncode": 124, "stdout": "partial", "stderr": "timeout"}


def test_run_on_missing_environment_leaves_no_invalid_state(tmp_path):
    env = CloudEnv("missing", root=tmp_path)
    with pytest.raises(CloudEnvError, match="not initialized"):
        env.run(**run_args())
    assert not env.state_path.exists()
    assert CloudEnv("missing", root=tmp_path).state == {}


def test_share_absence_and_image_retirement_are_one_durable_transition(environment, monkeypatch):
    errors = sdk_errors()
    env, conn = environment
    recorded(env, "shares", "layers", id="share")
    conn.shared_file_system.get_share.side_effect = errors.NotFoundException("gone", response=http_response(404))
    original_save = env.save

    def assert_atomic_save():
        if env.resource("shares", "layers") is None:
            assert env.state["images"] == {}
        original_save()

    monkeypatch.setattr(env, "save", assert_atomic_save)
    env.down()
    persisted = json.loads(env.state_path.read_text())
    assert persisted["images"] == {} and persisted["retired_images"][0]["app"] == image()


def test_uncertain_access_create_never_replays(environment):
    env, conn = environment
    recorded(env, "shares", "layers", id="share")
    conn.shared_file_system.get_share.return_value = owned(env)
    conn.shared_file_system.create_access_rule.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        env.grant("consumer", "ro")
    assert json.loads(env.state_path.read_text())["resources"]["access_rules"]["consumer"]["id"] is None
    with pytest.raises(CloudEnvError, match="HOLD"):
        env.grant("consumer", "ro")
    assert conn.shared_file_system.create_access_rule.call_count == 1


def test_real_nova_create_intent_precedes_post_and_retry_never_replays(environment):
    env, conn = environment
    recorded(env, "keypairs", "env", id="key", name="key")
    recorded(env, "security_groups", "env", id="sg")

    def uncertain(**kwargs):
        record = json.loads(env.state_path.read_text())["resources"]["servers"]["worker"]
        assert record["status"] == "creating" and record["id"] is None
        raise TimeoutError()

    conn.compute.create_server.side_effect = uncertain
    with pytest.raises(TimeoutError):
        env._create_server("worker", flavor="flavor", boot_gib=80, packages=[])
    with pytest.raises(CloudEnvError, match="HOLD"):
        env._create_server("worker", flavor="flavor", boot_gib=80, packages=[])
    assert conn.compute.create_server.call_count == 1
    conn.compute.delete_server.assert_not_called()


def test_uncertain_floating_ip_post_is_not_replayed(environment, monkeypatch):
    env, conn = environment
    server_setup(env, conn, monkeypatch)
    conn.network.create_ip.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        env._create_server("worker", flavor="flavor", boot_gib=80, packages=[])
    with pytest.raises(CloudEnvError, match="HOLD"):
        env._create_server("worker", flavor="flavor", boot_gib=80, packages=[])
    assert conn.network.create_ip.call_count == 1
    assert env.resource("floating_ips", "worker")["status"] == "creating"


def test_boot_volume_with_different_image_is_not_deleted_via_nova(environment, monkeypatch):
    sdk_errors()
    env, conn = environment
    server_setup(env, conn, monkeypatch)
    conn.block_storage.get_volume.return_value.volume_image_metadata = {"image_id": "foreign"}
    with pytest.raises(CloudEnvError, match="image/size"):
        env._delete_server("worker")
    conn.compute.delete_server.assert_not_called()


def test_cloud_init_failure_is_persisted_without_provisioning(environment, monkeypatch):
    env, conn = environment
    server_setup(env, conn, monkeypatch)
    recorded(
        env,
        "floating_ips",
        "worker",
        id="fip",
        description="marker",
        port_id="port",
        server_id="server",
        address="192.0.2.5",
    )
    conn.network.get_ip.return_value = Obj(
        project_id="project",
        description="marker",
        floating_network_id="external",
        floating_ip_address="192.0.2.5",
        port_id="port",
    )
    env._wait_ssh.return_value = {"status": "error", "returncode": 1, "ready": False}
    ssh = Mock()
    monkeypatch.setattr(env, "ssh", ssh)
    with pytest.raises(CloudEnvError, match="status persisted"):
        env._create_server("worker", flavor="flavor", boot_gib=80, packages=[])
    assert env.resource("servers", "worker")["cloud_init"]["status"] == "error"
    ssh.assert_not_called()


def test_successful_ro_probe_creation_followed_by_unlink_denial_is_not_read_only(monkeypatch):
    output = io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", io.StringIO(json.dumps({**image(), "verify": False, "command": ["true"]})))
        patch.setattr(sys, "stdout", output)
        patch.setattr(os, "open", Mock(return_value=123))
        patch.setattr(os, "close", Mock())
        patch.setattr(os, "unlink", Mock(side_effect=PermissionError(errno.EACCES, "unlink denied")))
        with pytest.raises(PermissionError):
            exec(CONSUMER_SCRIPT, {})
    assert not output.getvalue()


@pytest.mark.parametrize(
    "expected,observed",
    [
        ("192.0.2.1:06789,192.0.2.2:/volumes/share/", "192.0.2.2,192.0.2.1:6789:/volumes/share"),
        ("[2001:0db8:0:0::1]:6789:/volumes/share", "[2001:db8::1]:6789:/volumes/share"),
    ],
)
def test_literal_ceph_source_normalizes_ip_text_order_and_numeric_ports(expected, observed):
    assert cloud_env._ceph_source_identity(expected) == cloud_env._ceph_source_identity(observed)


@pytest.mark.parametrize(
    "source",
    [
        "mon.example:/volumes/share",
        "192.0.2.1:/../foreign",
        "192.0.2.1:/path;touch-payload",
        "user@fsid.fs=/volumes/share",
    ],
)
def test_unsupported_ceph_exports_fail_closed_before_mount(environment, source):
    env, _ = environment
    with pytest.raises(CloudEnvError, match="unsupported CephFS export"):
        env._validate_export(source)


@pytest.mark.parametrize(
    "source,name,accepted",
    [
        ("192.0.2.2,192.0.2.1:6789:/volumes/share", "identity", True),
        ("192.0.2.2,192.0.2.1:/volumes/share", "identity", False),
        ("192.0.2.2,192.0.2.1:6789:/volumes/foreign", "identity", False),
        ("192.0.2.2,192.0.2.1:6789:/volumes/share", "foreign", False),
    ],
)
def test_generated_mount_checker_binds_source_path_port_and_cephx_identity(
    environment, monkeypatch, source, name, accepted
):
    import shlex

    env, _ = environment
    export = "192.0.2.1:6789,192.0.2.2:/volumes/share"
    recorded(env, "shares", "layers", id="share", export=export)
    recorded(env, "access_rules", "worker", id="rule", access_to="identity")
    argv = shlex.split(env._mount_script("worker"))
    index = argv.index("python3")
    check = argv[index + 2]
    mounts = {
        "filesystems": [{"fstype": "ceph", "source": source, "options": "rw,name=" + name + ",secret=DO-NOT-EXPOSE"}]
    }
    output = io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "argv", ["check", cloud_env.STORE_MOUNT, export, "identity"])
        patch.setattr(sys, "stdout", output)
        patch.setattr(subprocess, "check_output", Mock(return_value=json.dumps(mounts).encode()))
        if accepted:
            exec(check, {})
        else:
            with pytest.raises((AssertionError, ValueError)):
                exec(check, {})
    assert ("ceph-export-identity-verified" in output.getvalue()) is accepted
    assert "DO-NOT-EXPOSE" not in output.getvalue()


def intake_worker(environment, monkeypatch):
    env, conn = environment
    env.state.update(worker_ready=True, worker_package_sha256="verified-package")
    recorded(env, "servers", "worker", id="worker", address="192.0.2.5")
    conn.compute.get_server.return_value = owned(env, id="worker", metadata={"palimpsest_role": "worker"})
    monkeypatch.setattr(cloud_env, "package_archive", lambda: (b"package", "verified-package"))
    monkeypatch.setattr(env, "_send_mount", Mock())
    monkeypatch.setattr(env, "upload", Mock(side_effect=AssertionError("registry payload must not come from client")))
    return env


def test_registry_pull_requires_pin_before_remote_effects(environment, monkeypatch):
    env = intake_worker(environment, monkeypatch)
    remote = Mock()
    monkeypatch.setattr(env, "ssh", remote)
    with pytest.raises(CloudEnvError, match="digest pin"):
        env.pull(reference="docker.io/nvidia/cuda:latest", tag="cuda", timeout=60)
    remote.assert_not_called()
    env._send_mount.assert_not_called()


def test_registry_pull_unknown_response_is_durable_hold_not_replayed(environment, monkeypatch):
    env = intake_worker(environment, monkeypatch)
    remote = Mock(side_effect=subprocess.TimeoutExpired("ssh", 60))
    monkeypatch.setattr(env, "ssh", remote)
    reference = "docker.io/nvidia/cuda@sha256:" + "b" * 64
    with pytest.raises(subprocess.TimeoutExpired):
        env.pull(reference=reference, tag="cuda", timeout=60)
    persisted = json.loads(env.state_path.read_text())
    assert persisted["image_intakes"]["cuda"]["status"] == "held"
    assert "cuda" not in persisted["images"]
    with pytest.raises(CloudEnvError, match="no replay"):
        env.pull(reference=reference, tag="cuda", timeout=60)
    assert remote.call_count == 1


@pytest.mark.parametrize("mismatch", ["job_id", "name", "root_digest", "reference"])
def test_registry_receipt_must_match_original_intake(environment, monkeypatch, mismatch):
    env = intake_worker(environment, monkeypatch)
    reference = "docker.io/nvidia/cuda@sha256:" + "b" * 64

    def response(*args, **kwargs):
        receipt = {
            **image(),
            "status": "success",
            "name": "cuda",
            "job_id": env.state["image_intakes"]["cuda"]["id"],
            "source": {"root_digest": "sha256:" + "b" * 64, "reference": reference},
        }
        target = receipt["source"] if mismatch in {"root_digest", "reference"} else receipt
        target[mismatch] = "wrong"
        return subprocess.CompletedProcess([], 0, json.dumps(receipt).encode(), b"")

    monkeypatch.setattr(env, "ssh", response)
    with pytest.raises(CloudEnvError, match="receipt differs"):
        env.pull(reference=reference, tag="cuda", timeout=60)
    persisted = json.loads(env.state_path.read_text())
    assert "cuda" not in persisted["images"]
    assert persisted["image_intakes"]["cuda"]["status"] == "held"


def registry_receipt_fixture():
    manifest_descriptor = {
        "digest": "sha256:" + "b" * 64,
        "size": 123,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
    }
    config_descriptor = {
        "digest": "sha256:" + "d" * 64,
        "size": 234,
        "mediaType": "application/vnd.oci.image.config.v1+json",
    }
    compressed = {
        "digest": "sha256:" + "e" * 64,
        "size": 345,
        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
    }
    runtime = {"user": "1000:1000", "env": ["EXAMPLE=original"], "working_dir": "/work", "entrypoint": ["/entrypoint"]}
    return {
        "status": "success",
        "name": "cuda",
        "runtime": runtime,
        "layers": [
            {
                "digest": "sha256:" + "a" * 64,
                "size": 10,
                "ordinal": 0,
                "compressed": compressed,
                "diff_id": "sha256:" + "c" * 64,
                "conversion_key": "sha256:" + "f" * 64,
                "receipt": "receipts/sha256/" + "1" * 64 + ".json",
            }
        ],
        "source": {
            "root_digest": manifest_descriptor["digest"],
            "manifest_digest": manifest_descriptor["digest"],
            "config_digest": config_descriptor["digest"],
            "root_descriptor": manifest_descriptor,
            "manifest_descriptor": manifest_descriptor,
            "config_descriptor": config_descriptor,
            "manifest": {"config": config_descriptor, "layers": [compressed]},
            "config": {
                "os": "linux",
                "architecture": "amd64",
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "c" * 64]},
                "config": {
                    "User": "1000:1000",
                    "Env": ["EXAMPLE=original"],
                    "WorkingDir": "/work",
                    "Entrypoint": ["/entrypoint"],
                },
            },
        },
    }


def test_registry_intake_preserves_source_occurrences_and_original_runtime(environment, monkeypatch):
    env = intake_worker(environment, monkeypatch)
    reference = "docker.io/nvidia/cuda@sha256:" + "b" * 64
    fixture = registry_receipt_fixture()

    def response(*args, **kwargs):
        receipt = copy.deepcopy(fixture)
        receipt["job_id"] = env.state["image_intakes"]["cuda"]["id"]
        receipt["source"]["reference"] = reference
        return subprocess.CompletedProcess([], 0, json.dumps(receipt).encode(), b"")

    monkeypatch.setattr(env, "ssh", response)
    env.pull(reference=reference, tag="cuda", timeout=60)
    persisted = json.loads(env.state_path.read_text())
    assert persisted["images"]["cuda"]["runtime"] == fixture["runtime"]
    assert "cmd" not in persisted["images"]["cuda"]["runtime"]
    assert persisted["images"]["cuda"]["layers"] == fixture["layers"]
    assert persisted["images"]["cuda"]["source"]["config"] == fixture["source"]["config"]
    assert persisted["image_intakes"]["cuda"]["status"] == "success"
    env.upload.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ordinal", 1),
        ("ordinal", False),
        ("diff_id", "sha256:" + "2" * 64),
        ("compressed", {}),
        ("conversion_key", "bad"),
        ("receipt", "../foreign.json"),
    ],
)
def test_registry_projection_rejects_changed_occurrence(field, value):
    receipt = registry_receipt_fixture()
    receipt["layers"][0][field] = value
    with pytest.raises(CloudEnvError, match="provenance"):
        cloud_env._validate_registry_image(receipt)


def test_registry_projection_rejects_synthesized_default_command():
    receipt = registry_receipt_fixture()
    receipt["runtime"]["cmd"] = ["bash"]
    with pytest.raises(CloudEnvError, match="original image config"):
        cloud_env._validate_registry_image(receipt)


def test_env_up_cannot_overwrite_recorded_worker_source(environment, monkeypatch):
    env, conn = environment
    env.state["worker_package_sha256"] = "previous-source"
    env.save()
    monkeypatch.setattr(cloud_env, "package_archive", lambda: (b"new-source", "new-source-digest"))
    remote = Mock(side_effect=AssertionError("must refuse before remote effects"))
    monkeypatch.setattr(env, "ssh", remote)
    with pytest.raises(CloudEnvError):
        env.up(
            cloud="test-cloud",
            network="network",
            external_network="external",
            image="image",
            worker_flavor="flavor",
            share_gib=50,
            share_type="cephfs",
            ssh_cidrs=[],
            security_group="00000000-0000-0000-0000-000000000001",
            worker_boot_gib=80,
            dns=[],
            buildkit_archive=env.directory / "not-read.tar",
        )
    assert json.loads(env.state_path.read_text())["worker_package_sha256"] == "previous-source"
    remote.assert_not_called()
    assert not conn.compute.mock_calls and not conn.network.mock_calls and not conn.shared_file_system.mock_calls


def test_initial_installer_cannot_replace_a_recorded_package(environment, monkeypatch):
    env, _ = environment
    env.state["worker_package_sha256"] = "previous-source"
    monkeypatch.setattr(cloud_env, "package_archive", lambda: (b"new-source", "new-source-digest"))
    upload = Mock(side_effect=AssertionError("must not transfer replacement source"))
    monkeypatch.setattr(env, "upload", upload)
    with pytest.raises(CloudEnvError):
        env._sync_worker_package("192.0.2.5")
    upload.assert_not_called()
    assert env.state["worker_package_sha256"] == "previous-source"


@pytest.fixture
def cloud_mutation_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"status": 503, "posts": [], "gets": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value, location=None):
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Retry-After", "0")
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            state["gets"].append(self.path)
            if self.path.endswith("/metadata"):
                self.reply(307, {}, endpoint + "/destination")
            elif self.path.endswith("/destination"):
                self.reply(200, {"status": "read-complete"})
            else:
                self.reply(
                    200,
                    {
                        "versions": [
                            {
                                "id": "v2.17",
                                "status": "CURRENT",
                                "links": [{"rel": "self", "href": endpoint + "/"}],
                            }
                        ]
                    },
                )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["posts"].append({"path": self.path, "body": body})
            if len(state["posts"]) == 1:
                self.reply(
                    state["status"],
                    {"error": {"message": "synthetic uncertain result"}},
                    endpoint + "/images/redirected",
                )
            else:
                self.reply(
                    201, {"id": "22222222-2222-4222-8222-222222222222", "name": body["name"], "status": "queued"}
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    endpoint = "http://127.0.0.1:" + str(server.server_port) + "/v2"
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield endpoint, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def synthetic_cloud_connection(monkeypatch, endpoint):
    sdk = pytest.importorskip("openstack")
    from openstack.config.loader import OpenStackConfig

    loader = OpenStackConfig(load_yaml_config=False, load_envvars=False)

    def configured_region(**options):
        options.pop("cloud")
        return loader.get_one_cloud(
            **options,
            auth_type="none",
            auth={"endpoint": endpoint},
            image_endpoint_override=endpoint,
            image_api_version="2",
        )

    monkeypatch.setattr(sdk.config, "get_cloud_region", configured_region)
    return cloud_cli._connect("synthetic")


@pytest.mark.parametrize("status", [401, 307, 503])
def test_cloud_factory_never_replays_mutating_post_after_auth_redirect_or_status(
    monkeypatch, cloud_mutation_server, status
):
    NoAuth = pytest.importorskip("keystoneauth1.noauth").NoAuth

    endpoint, state = cloud_mutation_server
    state["status"] = status
    connection = synthetic_cloud_connection(monkeypatch, endpoint)

    class ReauthNoAuth(NoAuth):
        def invalidate(self):
            return True

    connection.session.auth = ReauthNoAuth(endpoint=endpoint)
    try:
        # Both SDK service defaults and an explicit unsafe override must lose to
        # the one-shot session policy. Only a read is eligible for those retries.
        connection.image.status_code_retries = 5
        connection.image.connect_retries = 2

        def send():
            return connection.image.post(
                "images",
                json={"name": "synthetic-intent"},
                status_code_retries=5,
                connect_retries=2,
                allow_reauth=True,
                redirect=30,
                raise_exc=False,
            )

        if status == 307:
            with pytest.raises(cloud_cli.PalimpsestError, match="mutation redirect refused"):
                send()
        else:
            assert send().status_code == status
        assert state["posts"] == [{"path": "/v2/images", "body": {"name": "synthetic-intent"}}]
    finally:
        connection.close()


def test_cloud_factory_metadata_post503_cannot_create_an_untracked_second_image(monkeypatch, cloud_mutation_server):
    sdk_errors = pytest.importorskip("openstack.exceptions")
    endpoint, state = cloud_mutation_server
    connection = synthetic_cloud_connection(monkeypatch, endpoint)
    try:
        with pytest.raises(sdk_errors.HttpException) as failure:
            connection.image.create_image(
                name="synthetic-intent",
                allow_duplicates=True,
                disk_format="qcow2",
                container_format="bare",
                visibility="private",
            )
        assert failure.value.status_code == 503
        assert len(state["posts"]) == 1
        assert state["posts"][0]["body"]["name"] == "synthetic-intent"
    finally:
        connection.close()


def test_cloud_factory_preserves_read_only_metadata_redirects(monkeypatch, cloud_mutation_server):
    endpoint, state = cloud_mutation_server
    connection = synthetic_cloud_connection(monkeypatch, endpoint)
    try:
        response = connection.session.get(endpoint + "/metadata", authenticated=False)
        assert response.status_code == 200 and response.json() == {"status": "read-complete"}
        assert state["gets"][-2:] == ["/v2/metadata", "/v2/destination"]
        assert not state["posts"]
    finally:
        connection.close()


def test_nonroot_build_is_retained_but_consumer_refuses_before_allocation(environment, monkeypatch, tmp_path):
    env, conn = environment
    context = tmp_path / "context"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text('FROM scratch\nUSER 1000:1000\nCMD ["/app/server"]\n')
    record = {
        "schema": "palimpsest.cloud-layer-image.v1",
        "status": "success",
        "name": "nonroot-app",
        "source": {
            "root_digest": "sha256:" + "b" * 64,
            "manifest_digest": "sha256:" + "c" * 64,
            "config_digest": "sha256:" + "d" * 64,
        },
        "layers": [{"digest": "sha256:" + "a" * 64, "size": 4096}],
        "runtime": {
            "user": "1000:1000",
            "env": ["PATH=/usr/bin:/bin"],
            "working_dir": "/app",
            "entrypoint": [],
            "cmd": ["/app/server"],
        },
        "built_at_unix": 1793600000.0,
        "evidence_directory": "/var/lib/palimpsest-cloud/jobs/build-123",
        "publication": [True],
        "timings_seconds": {"buildkit": 0.1, "squashfs": 0.1, "publish": 0.1},
    }
    env.state["worker_package_sha256"] = "package-digest"
    env.state["worker_ready"] = True
    recorded(env, "servers", "worker", id="worker", address="192.0.2.4")
    monkeypatch.setattr(cloud_env, "package_archive", lambda: (b"package", "package-digest"))
    monkeypatch.setattr(env, "_send_mount", Mock())
    monkeypatch.setattr(env, "upload", Mock())
    monkeypatch.setattr(
        env, "ssh", Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(record).encode(), b""))
    )
    built = env.build(dockerfile=dockerfile, context=context, tag="nonroot-app", timeout=0.5)
    assert built["runtime"]["user"] == "1000:1000"
    persisted = json.loads(env.state_path.read_text())["images"]["nonroot-app"]
    assert persisted == {key: record[key] for key in ("name", "source", "layers", "runtime", "built_at_unix")}
    grant = Mock()
    allocate = Mock()
    monkeypatch.setattr(env, "grant", grant)
    monkeypatch.setattr(env, "_create_server", allocate)
    report = env.run(**{**run_args(), "tag": "nonroot-app"})
    assert report["status"] == "failed" and report["exit_code"] == 1
    assert "nonroot image User" in report["error"]
    grant.assert_not_called()
    allocate.assert_not_called()
    assert not conn.compute.mock_calls and not conn.shared_file_system.mock_calls


@pytest.mark.parametrize("assembly_code,command_code", [(0, 0), (0, 7), (23, 0)])
def test_large_consumer_output_retains_bounded_utf8_tails_and_cleanup(
    environment, monkeypatch, assembly_code, command_code
):
    env, _ = environment
    run_setup(env, monkeypatch)
    stdout = "discarded-prefix-" + "한" * 50000 + "-stdout-tail"
    stderr = "discarded-prefix-" + "글" * 50000 + "-stderr-tail"
    guest = {"command": {"returncode": command_code, "stdout": stdout, "stderr": stderr}}
    wire_stdout = json.dumps(guest, ensure_ascii=False).encode()
    wire_stderr = stderr.encode()
    env.ssh.return_value = subprocess.CompletedProcess([], assembly_code, wire_stdout, wire_stderr)
    report = env.run(**run_args())
    persisted = json.loads(env.state_path.read_text())["runs"][report["role"]]
    assert persisted == report
    assert report["guest_process"]["returncode"] == assembly_code
    for stream, original in (("stdout", wire_stdout), ("stderr", wire_stderr)):
        retained = report["guest_process"][stream]
        assert len(retained.encode()) <= 64 * 1024
        assert original.decode().endswith(retained) and "\ufffd" not in retained
        assert report["guest_process"]["output_retention"][stream] == {
            "original_bytes": len(original),
            "retained_bytes": len(retained.encode()),
            "truncated": True,
        }
    if assembly_code == 0:
        assert report["guest"]["command"]["returncode"] == command_code
        for stream, original in (("stdout", stdout), ("stderr", stderr)):
            retained = report["guest"]["command"][stream]
            assert len(retained.encode()) <= 64 * 1024
            assert original.endswith(retained) and "\ufffd" not in retained
            assert retained.endswith(f"-{stream}-tail")
            assert report["guest"]["command"]["output_retention"][stream]["truncated"] is True
        assert report["status"] == ("success" if command_code == 0 else "command_failed")
        assert report["exit_code"] == (0 if command_code == 0 else 1)
    else:
        assert report["guest_process_returncode"] == assembly_code
        assert report["status"] == "failed" and report["exit_code"] == 1
        assert "guest" not in report
    assert report["cleanup"] == {"server": "absent", "ro_access": "revoked"}
    env._delete_server.assert_called_once_with(report["role"])
    env._revoke.assert_called_once_with(report["role"])


DELETE_KINDS = [
    "servers",
    "floating_ips",
    "access_rules",
    "shares",
    "security_group_rules",
    "security_groups",
    "keypairs",
]


def cleanup_case(env, conn, monkeypatch, kind):
    role = "worker" if kind in {"servers", "floating_ips"} else "env"
    if kind == "servers":
        fresh = server_setup(env, conn, monkeypatch)
        getter, delete = conn.compute.get_server, conn.compute.delete_server
    elif kind == "floating_ips":
        recorded(
            env,
            kind,
            role,
            id="fip",
            description="marker",
            port_id="port",
            server_id="server",
            address="192.0.2.5",
        )
        fresh = Obj(
            project_id="project",
            description="marker",
            floating_network_id="external",
            floating_ip_address="192.0.2.5",
            port_id="port",
        )
        conn.network.get_port.return_value = Obj(project_id="project", device_id="server", network_id="network")
        getter, delete = conn.network.get_ip, conn.network.delete_ip
    elif kind == "access_rules":
        recorded(env, kind, role, id="rule", share_id="share", access_to="identity", level="ro")
        fresh = Obj(id="rule", access_to="identity", access_level="ro", access_type="cephx")
        conn.shared_file_system.get_share.return_value = owned(env)
        conn.shared_file_system.access_rules.return_value = [fresh]
        env._secret_path("identity").write_text("private")
        return role, conn.shared_file_system.access_rules, conn.shared_file_system.delete_access_rule, fresh
    elif kind == "shares":
        role = "layers"
        recorded(env, kind, role, id="share")
        fresh = owned(env)
        getter, delete = conn.shared_file_system.get_share, conn.shared_file_system.delete_share
    elif kind in {"security_group_rules", "security_groups"}:
        recorded(env, "security_groups", "env", id="sg", description="marker", owned=True)
        group = Obj(project_id="project", description="marker")
        conn.network.get_security_group.return_value = group
        conn.network.ports.return_value = []
        if kind == "security_groups":
            fresh = group
            getter, delete = conn.network.get_security_group, conn.network.delete_security_group
        else:
            role = "ssh-0"
            recorded(env, kind, role, id="rule", group_id="sg", cidr="192.0.2.1/32")
            fresh = Obj(project_id="project", security_group_id="sg", remote_ip_prefix="192.0.2.1/32")
            getter, delete = conn.network.get_security_group_rule, conn.network.delete_security_group_rule
    else:
        recorded(env, kind, role, id="key", public_key="ssh-ed25519 public", user_id="user")
        fresh = Obj(public_key="ssh-ed25519 public", user_id="user")
        getter, delete = conn.compute.get_keypair, conn.compute.delete_keypair
    getter.return_value = fresh
    return role, getter, delete, fresh


@pytest.mark.parametrize("kind", DELETE_KINDS)
@pytest.mark.parametrize("observation", ["present", "none", "503", "timeout"])
def test_failed_delete_restart_observes_without_replay_then_requires_absence(
    environment, monkeypatch, kind, observation
):
    errors = sdk_errors()
    env, conn = environment
    role, getter, delete, _ = cleanup_case(env, conn, monkeypatch, kind)

    def uncertain_delete(*args, **kwargs):
        durable = json.loads(env.state_path.read_text())["resources"][kind][role]
        assert durable["status"] == "deleting"
        if kind == "servers":
            assert durable["boot_volumes"] == ["boot"]
        raise TimeoutError("DELETE outcome unknown")

    delete.side_effect = uncertain_delete
    with pytest.raises(TimeoutError, match="DELETE outcome unknown"):
        env.down()
    restarted = CloudEnv("test", root=env.directory.parent)
    if observation == "present":
        monkeypatch.setattr(cloud_env.time, "monotonic", Mock(side_effect=range(0, 100000, 601)))
    elif observation == "none":
        if kind == "access_rules":
            conn.shared_file_system.get_share.return_value = None
        else:
            getter.return_value = None
    elif observation == "503":
        getter.side_effect = errors.HttpException("unavailable", response=http_response(503))
    else:
        getter.side_effect = TimeoutError("GET outcome unknown")
    with pytest.raises((CloudEnvError, TimeoutError, errors.HttpException)):
        restarted.down()
    assert delete.call_count == 1
    retained = json.loads(env.state_path.read_text())["resources"][kind][role]
    assert retained["status"] == "deleting"
    if kind == "servers":
        assert retained["boot_volumes"] == ["boot"]
    if kind == "access_rules":
        assert env._secret_path("identity").read_text() == "private"
    if kind == "shares":
        assert "app" in json.loads(env.state_path.read_text())["images"]
    getter.side_effect = None
    if kind == "access_rules":
        conn.shared_file_system.get_share.return_value = owned(restarted)
        getter.return_value = []
    else:
        getter.side_effect = errors.NotFoundException("gone", response=http_response(404))
    if kind == "servers":
        conn.block_storage.get_volume.side_effect = errors.NotFoundException("gone", response=http_response(404))
    if kind == "security_group_rules":
        conn.network.get_security_group.side_effect = errors.NotFoundException("gone", response=http_response(404))
    restarted.down()
    assert delete.call_count == 1
    assert restarted.resource(kind, role) is None
    if kind == "access_rules":
        assert not env._secret_path("identity").exists()


@pytest.mark.parametrize("kind", DELETE_KINDS)
def test_deleting_resource_still_requires_fresh_ownership_binding(environment, monkeypatch, kind):
    sdk_errors()
    env, conn = environment
    role, _, delete, fresh = cleanup_case(env, conn, monkeypatch, kind)
    env._deleting(kind, role)
    if kind in {"servers", "shares"}:
        fresh.metadata["palimpsest_env_id"] = "foreign"
    elif kind == "floating_ips":
        conn.network.get_port.return_value.device_id = "foreign"
    elif kind == "access_rules":
        fresh.access_to = "foreign"
    elif kind == "security_group_rules":
        fresh.security_group_id = "foreign"
    elif kind == "security_groups":
        fresh.description = "foreign"
    else:
        fresh.user_id = "foreign"
    with pytest.raises(CloudEnvError, match="HOLD"):
        env.down()
    delete.assert_not_called()
    assert json.loads(env.state_path.read_text())["resources"][kind][role]["status"] == "deleting"


def test_started_group_deletion_never_revisits_rule_effects_and_requires_child_absence(environment, monkeypatch):
    errors = sdk_errors()
    env, conn = environment
    role, getter, delete, _ = cleanup_case(env, conn, monkeypatch, "security_group_rules")
    env._deleting("security_groups", "env")
    monkeypatch.setattr(cloud_env.time, "monotonic", Mock(side_effect=range(0, 100000, 601)))
    with pytest.raises(CloudEnvError, match="still present"):
        env.down()
    conn.network.ports.assert_not_called()
    delete.assert_not_called()
    conn.network.delete_security_group.assert_not_called()
    conn.network.get_security_group.side_effect = errors.NotFoundException("gone", response=http_response(404))
    with pytest.raises(CloudEnvError, match="still present"):
        env.down()
    assert env.resource("security_groups", "env")["status"] == "deleting"
    assert env.resource("security_group_rules", role) is not None
    getter.side_effect = errors.NotFoundException("gone", response=http_response(404))
    assert env.down()["security_group"] == "absent"
    assert env.resource("security_groups", "env") is None
    assert env.resource("security_group_rules", role) is None
    delete.assert_not_called()
    conn.network.delete_security_group.assert_not_called()


@pytest.mark.parametrize("attached", [[], [{"id": "boot"}], [{"id": "foreign"}]])
def test_deleting_server_uses_durable_boot_binding_after_nova_clears_mappings(environment, monkeypatch, attached):
    errors = sdk_errors()
    env, conn = environment
    server = server_setup(env, conn, monkeypatch)
    recorded(env, "servers", "worker", id="server", boot_gib=80, boot_volumes=["boot"], status="deleting")
    server.attached_volumes = attached
    conn.compute.get_server.side_effect = [
        server,
        errors.NotFoundException("gone", response=http_response(404)),
    ]
    conn.block_storage.get_volume.side_effect = errors.NotFoundException("gone", response=http_response(404))
    restarted = CloudEnv("test", root=env.directory.parent)
    if attached == [{"id": "foreign"}]:
        with pytest.raises(CloudEnvError, match="boot volume IDs changed"):
            restarted.down()
        assert restarted.resource("servers", "worker")["boot_volumes"] == ["boot"]
        assert restarted.resource("servers", "worker")["status"] == "deleting"
        conn.block_storage.get_volume.assert_not_called()
    else:
        result = restarted.down()
        assert result["server:worker"]["boot_volumes_absent"] == ["boot"]
        assert restarted.resource("servers", "worker") is None
        conn.block_storage.get_volume.assert_called_once_with("boot")
    conn.compute.delete_server.assert_not_called()


def test_boolean_build_timeout_is_refused_before_upload_or_worker_execution(environment, monkeypatch, tmp_path):
    env, _ = environment
    ssh = Mock()
    monkeypatch.setattr(env, "ssh", ssh)
    with pytest.raises(CloudEnvError, match="build timeout"):
        env.build(dockerfile=tmp_path / "absent", context=tmp_path, tag="app", timeout=True)
    ssh.assert_not_called()


@pytest.fixture
def namespace_access(environment, tmp_path, monkeypatch):
    from palimpsest_local import ceph_path_access as access

    env, _ = environment
    env.native_ceph_mutation_boundary = Mock(return_value=None)
    real_run = access.CephPathAccessManager._run
    private = tmp_path.resolve() / "issuer-private"
    private.mkdir(mode=0o700)
    conf, keyring = private / "ceph.conf", private / "issuer.keyring"
    conf.write_text("[global]\nmon_host = 192.0.2.10:6789\n")
    keyring.write_text("[client.sdk-issuer]\nkey = AAAAAAAAAAAAAAAAAAAAAA==\n")
    conf.chmod(0o600)
    keyring.chmod(0o600)
    share, scope = "12345678-1234-1234-1234-123456789abc", "user-" + "a" * 32
    binding = {
        "kind": "manila-namespace-v1",
        "share_id": share,
        "base_export": "192.0.2.10:6789:/share",
        "export": "192.0.2.10:6789:/share/cas/" + scope,
        "root": "blobs/sha256",
        "scope_key": scope,
        "fs_name": "cephfs",
        "data_pool": "cephfs.data",
        "namespace": access.namespace_for(share, scope),
    }
    configuration = {
        "fs_name": "cephfs",
        "data_pool": "cephfs.data",
        "issuer_client": "client.sdk-issuer",
        "conf_file": str(conf),
        "keyring_file": str(keyring),
        "publication_root": str(tmp_path / "export"),
    }
    provider, calls = {}, []
    secret = "AQBBBBBBBBBBBBBBBBBBBB=="

    def command(manager, *args):
        calls.append(args)
        verb, entity = args[1], args[2]
        if verb == "get":
            if entity not in provider:
                return 2, b"", f"Error ENOENT: failed to find {entity} in keyring\n".encode()
            return 0, json.dumps([{"entity": entity, "key": secret, "caps": provider[entity]}]).encode(), b""
        role = entity.removeprefix("client.pal-" + env.state["marker"] + "-")
        record = env.resource("access_rules", role)
        if verb == "add":
            assert record["status"] == "creating" and record["id"] == entity
            assert json.loads(env.state_path.read_text())["resources"]["access_rules"][role]["status"] == "creating"
            provider[entity] = dict(zip(args[3::2], args[4::2], strict=True))
            return 0, secret.encode(), b""
        if verb == "del":
            assert record["status"] == "deleting"
            assert json.loads(env.state_path.read_text())["resources"]["access_rules"][role]["status"] == "deleting"
            provider.pop(entity)
            return 0, b"", b""
        raise AssertionError("unexpected command")

    monkeypatch.setattr(access.CephPathAccessManager, "_run", command)
    manager = access.CephPathAccessManager(env, configuration, binding)
    return Obj(
        module=access,
        manager=manager,
        env=env,
        configuration=configuration,
        binding=binding,
        provider=provider,
        calls=calls,
        command=command,
        secret=secret,
        real_run=real_run,
    )


def test_ceph_consumer_caps_restrict_raw_rados_to_one_exact_namespace(namespace_access):
    fixture = namespace_access
    caps = fixture.module.consumer_caps(fixture.binding)
    assert caps == {
        "mon": "allow r fsname=cephfs",
        "mds": "allow r fsname=cephfs path=/share/cas/" + fixture.binding["scope_key"],
        "osd": "allow r pool=cephfs.data namespace=" + fixture.binding["namespace"],
    }
    assert not any("tag cephfs" in value or "*" in value or "allow rw" in value for value in caps.values())
    assert "mgr" not in caps
    for changed in (
        {"namespace": ""},
        {"namespace": "foreign"},
        {"scope_key": "../foreign"},
        {"data_pool": "data, allow *"},
        {"export": "192.0.2.10:6789:/share"},
        {"export": "192.0.2.11:6789:/share/cas/" + fixture.binding["scope_key"]},
    ):
        with pytest.raises(CloudEnvError):
            fixture.module.consumer_caps({**fixture.binding, **changed})


def test_ceph_private_preflight_never_calls_auth_or_mutates_state(namespace_access):
    fixture = namespace_access
    original = fixture.env.state_path.read_bytes()
    fixture.manager.preflight()
    assert fixture.calls == [] and fixture.provider == {}
    assert fixture.env.state_path.read_bytes() == original


@pytest.mark.parametrize("issuer", ["client.admin", "client.manila", "client.bootstrap-mds", "client.*", "admin", ""])
def test_ceph_preflight_rejects_admin_default_and_wildcard_issuers(namespace_access, issuer):
    fixture = namespace_access
    with pytest.raises(CloudEnvError, match="issuer"):
        fixture.module.CephPathAccessManager(
            fixture.env, {**fixture.configuration, "issuer_client": issuer}, fixture.binding
        )
    assert fixture.calls == []


def test_ceph_private_issuer_keyring_and_conf_never_live_in_public_export(namespace_access):
    fixture = namespace_access
    private = os.path.dirname(fixture.configuration["keyring_file"])
    with pytest.raises(CloudEnvError, match="outside the publication root"):
        fixture.module.CephPathAccessManager(
            fixture.env, {**fixture.configuration, "publication_root": private}, fixture.binding
        )
    for key in ("conf_file", "keyring_file"):
        path = fixture.configuration[key]
        os.chmod(path, 0o644)
        with pytest.raises(CloudEnvError, match="private"):
            fixture.manager.preflight()
        os.chmod(path, 0o600)
    assert fixture.calls == []


def test_ceph_issuance_freshly_checks_exact_caps_and_never_persists_keys(namespace_access, capsys):
    fixture, role = namespace_access, "run-one"
    record = fixture.manager.issue(role)
    entity = fixture.manager._entity(role)
    assert record["status"] == "confirmed" and record["type"] == "ceph-path"
    assert record["binding"] == fixture.binding and record["run_role"] == role
    result = fixture.manager.access(role)
    assert result == {
        "export": fixture.binding["export"],
        "access_to": entity[7:],
        "access_key": fixture.secret,
        "fs_name": "cephfs",
    }
    assert [call[:2] for call in fixture.calls] == [("auth", "get"), ("auth", "add"), ("auth", "get"), ("auth", "get")]
    assert fixture.secret not in fixture.env.state_path.read_text()
    assert not list(fixture.env.directory.glob("cephx-*.key"))
    captured = capsys.readouterr()
    assert fixture.secret not in captured.out + captured.err
    fixture.provider[entity] = {**fixture.provider[entity], "osd": "allow r tag cephfs data=cephfs"}
    with pytest.raises(CloudEnvError, match="caps"):
        fixture.manager.access(role)


@pytest.mark.parametrize("matching", [True, False])
def test_ceph_existing_unrecorded_clients_are_never_adopted(namespace_access, matching):
    fixture = namespace_access
    entity = fixture.manager._entity("run-one")
    fixture.provider[entity] = fixture.module.consumer_caps(fixture.binding) if matching else {"osd": "allow *"}
    with pytest.raises(CloudEnvError, match="never adopt|caps"):
        fixture.manager.issue("run-one")
    assert fixture.env.resource("access_rules", "run-one") is None
    assert [call[1] for call in fixture.calls] == ["get"]


@pytest.mark.parametrize("error", [b"Error EACCES: access denied\n", b"Error ENOENT: unavailable\n", b"", b"secret"])
def test_ceph_exit_two_without_exact_server_enoent_is_not_absence(namespace_access, monkeypatch, error):
    fixture = namespace_access
    monkeypatch.setattr(fixture.manager, "_run", lambda *args: (2, b"", error))
    with pytest.raises(CloudEnvError, match="absence is not confirmed"):
        fixture.manager.issue("run-one")
    assert fixture.env.resource("access_rules", "run-one") is None


def test_ceph_unknown_create_is_durable_and_never_replayed(namespace_access, monkeypatch):
    fixture, mutations = namespace_access, []

    def uncertain(*args):
        if args[:2] == ("auth", "add"):
            mutations.append(args)
            assert fixture.env.resource("access_rules", "run-one")["status"] == "creating"
            raise fixture.module.CephCommandError("private timeout; HOLD", command_pending=False)
        return fixture.command(fixture.manager, *args)

    monkeypatch.setattr(fixture.manager, "_run", uncertain)
    with pytest.raises(CloudEnvError, match="HOLD"):
        fixture.manager.issue("run-one")
    assert fixture.env.resource("access_rules", "run-one")["status"] == "creating"
    with pytest.raises(CloudEnvError, match="never replay"):
        fixture.manager.issue("run-one")
    assert len(mutations) == 1
    with pytest.raises(CloudEnvError, match="not freshly issuable"):
        fixture.manager.access("run-one")


def test_ceph_unknown_delete_is_durable_no_replay_and_requires_fresh_absence(namespace_access, monkeypatch):
    fixture, role, mutations = namespace_access, "run-one", []
    fixture.manager.issue(role)
    entity = fixture.manager._entity(role)

    def uncertain(*args):
        if args[:2] == ("auth", "del"):
            mutations.append(args)
            assert fixture.env.resource("access_rules", role)["status"] == "deleting"
            raise fixture.module.CephCommandError("private timeout; HOLD", command_pending=False)
        return fixture.command(fixture.manager, *args)

    monkeypatch.setattr(fixture.manager, "_run", uncertain)
    with pytest.raises(CloudEnvError, match="HOLD"):
        fixture.manager.revoke(role)
    assert fixture.env.resource("access_rules", role)["status"] == "deleting"
    with pytest.raises(CloudEnvError, match="never replay"):
        fixture.manager.revoke(role)
    assert len(mutations) == 1
    fixture.provider.pop(entity)
    fixture.manager.revoke(role)
    assert fixture.env.resource("access_rules", role) is None and len(mutations) == 1


def test_ceph_delete_fresh_get_and_historical_other_scope_absence(namespace_access):
    fixture = namespace_access
    fixture.manager.issue("run-one")
    fixture.manager.revoke("run-one")
    record = fixture.env.state["history"][-1]["resource"]
    assert record["status"] == "deleting"
    assert [call[1] for call in fixture.calls][-3:] == ["get", "del", "get"]
    other_scope = "project-" + "b" * 32
    other_binding = {
        **fixture.binding,
        "scope_key": other_scope,
        "export": fixture.binding["base_export"] + "/cas/" + other_scope,
        "namespace": fixture.module.namespace_for(fixture.binding["share_id"], other_scope),
    }
    later = fixture.module.CephPathAccessManager(fixture.env, fixture.configuration, other_binding)
    later.check_absent(record)
    with pytest.raises(CloudEnvError, match="sealed publication"):
        later.check_absent({**record, "id": "client.foreign"})


@pytest.mark.parametrize("stream,limit_name", [("stdout", "_OUTPUT_LIMIT"), ("stderr", "_ERROR_LIMIT")])
def test_ceph_command_output_is_bounded_private_and_term_reaped(
    namespace_access, monkeypatch, capsys, stream, limit_name
):
    fixture, access = namespace_access, namespace_access.module

    class Pipe:
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

        def close(self):
            pass

    process = Obj(stdout=Pipe(100), stderr=Pipe(101), pid=123456, wait=Mock(return_value=0))

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, pipe, event, name):
            pass

        def get_map(self):
            return {stream: True}

        def select(self, timeout):
            return [(Obj(fd=100, data=stream), 1)]

    monkeypatch.setattr(access.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(access.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(access.os, "set_blocking", lambda *args: None)
    monkeypatch.setattr(access.os, "read", lambda *args: fixture.secret.encode() * 8)
    term = Mock()
    monkeypatch.setattr(access.os, "killpg", term)
    monkeypatch.setattr(access, limit_name, 32)
    with pytest.raises(CloudEnvError, match="HOLD") as raised:
        access._command(["/usr/bin/ceph", "auth", "get", "client.synthetic"])
    assert fixture.secret not in str(raised.value)
    captured = capsys.readouterr()
    assert fixture.secret not in captured.out + captured.err
    term.assert_called_once_with(process.pid, access.signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=access._TERM_SECONDS)


@pytest.mark.parametrize("operation", ["issue", "access", "revoke"])
def test_ceph_same_caps_rotated_key_is_not_reissued_or_deleted(namespace_access, monkeypatch, operation):
    fixture, role = namespace_access, "run-one"
    record = fixture.manager.issue(role)
    assert record["key_digest"] == hashlib.sha256(fixture.secret.encode()).hexdigest()
    previous = fixture.manager._run

    def rotated(*args):
        code, out, error = previous(*args)
        if args[:2] == ("auth", "get") and code == 0:
            response = json.loads(out)
            response[0]["key"] = "AQCCCCCCCCCCCCCCCCCCCC=="
            return code, json.dumps(response).encode(), error
        return code, out, error

    monkeypatch.setattr(fixture.manager, "_run", rotated)
    calls_before = len(fixture.calls)
    with pytest.raises(CloudEnvError, match="original fingerprint"):
        getattr(fixture.manager, operation)(role)
    assert [call[1] for call in fixture.calls[calls_before:]] == ["get"]
    assert fixture.env.resource("access_rules", role)["status"] == "confirmed"


def test_ceph_uncertain_creation_can_read_settle_once_without_auth_replay(namespace_access, monkeypatch):
    fixture, role, mutations = namespace_access, "run-one", []

    def uncertain(*args):
        result = fixture.command(fixture.manager, *args)
        if args[:2] == ("auth", "add"):
            mutations.append(args)
            raise fixture.module.CephCommandError("acknowledgement lost; HOLD", command_pending=False)
        return result

    monkeypatch.setattr(fixture.manager, "_run", uncertain)
    with pytest.raises(CloudEnvError, match="HOLD"):
        fixture.manager.issue(role)
    assert fixture.env.resource("access_rules", role)["status"] == "creating"
    record = fixture.manager.issue(role)
    assert len(mutations) == 1 and record["status"] == "confirmed"
    assert record["key_digest"] == hashlib.sha256(fixture.secret.encode()).hexdigest()


def test_ceph_pool_and_filesystem_bounds_match_operator_configuration(namespace_access):
    fixture = namespace_access
    assert fixture.module.consumer_caps({**fixture.binding, "data_pool": "p" * 128})["osd"].startswith(
        "allow r pool=" + "p" * 128 + " "
    )
    for changed in ({"data_pool": "p" * 129}, {"fs_name": "f" * 65}):
        with pytest.raises(CloudEnvError, match="filesystem or data pool"):
            fixture.module.consumer_caps({**fixture.binding, **changed})


def test_ceph_command_deadline_sends_term_and_reaps_without_kill(namespace_access, monkeypatch):
    access = namespace_access.module

    class Pipe:
        def fileno(self):
            return 100

        def close(self):
            pass

    process = Obj(stdout=Pipe(), stderr=Pipe(), pid=123456, wait=Mock(return_value=0))

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, *args):
            pass

        def get_map(self):
            return {100: True}

    monkeypatch.setattr(access.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(access.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(access.os, "set_blocking", lambda *args: None)
    monotonic = Mock(side_effect=[0, access._COMMAND_SECONDS + 1])
    monkeypatch.setattr(access.time, "monotonic", monotonic)
    term = Mock()
    monkeypatch.setattr(access.os, "killpg", term)
    with pytest.raises(CloudEnvError, match="no mutation replay"):
        access._command(["/usr/bin/ceph", "auth", "get", "client.synthetic"])
    term.assert_called_once_with(process.pid, access.signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=access._TERM_SECONDS)


@pytest.mark.parametrize("operation", ["add", "del"])
def test_ceph_unreaped_mutation_persists_custody_before_spawn_and_blocks_all_settlement(
    namespace_access, monkeypatch, operation
):
    fixture, role = namespace_access, "run-one"
    if operation == "del":
        fixture.manager.issue(role)

    def unreaped(*args):
        if args[:2] == ("auth", operation):
            record = fixture.env.resource("access_rules", role)
            assert record["command_pending"] is True
            assert (
                json.loads(fixture.env.state_path.read_text())["resources"]["access_rules"][role]["command_pending"]
                is True
            )
            raise fixture.module.CephCommandError("TERM grace expired; HOLD", command_pending=True)
        return fixture.command(fixture.manager, *args)

    monkeypatch.setattr(fixture.manager, "_run", unreaped)
    with pytest.raises(CloudEnvError, match="HOLD"):
        (fixture.manager.issue if operation == "add" else fixture.manager.revoke)(role)
    record = fixture.env.resource("access_rules", role)
    assert record["command_pending"] is True
    fixture.provider.clear()  # A transient ENOENT is not safe process settlement.
    blocked_command = Mock(side_effect=AssertionError("no provider action after lost process custody"))
    monkeypatch.setattr(fixture.manager, "_run", blocked_command)
    for operation_call in (fixture.manager.issue, fixture.manager.access, fixture.manager.revoke):
        with pytest.raises(CloudEnvError, match="process custody"):
            operation_call(role)
    assert fixture.env.resource("access_rules", role) == record
    blocked_command.assert_not_called()


def test_ceph_unconfirmed_create_absence_cannot_release_custody(namespace_access, monkeypatch):
    fixture, role = namespace_access, "run-one"

    def reaped_error(*args):
        if args[:2] == ("auth", "add"):
            raise fixture.module.CephCommandError("reaped with lost outcome; HOLD", command_pending=False)
        return fixture.command(fixture.manager, *args)

    monkeypatch.setattr(fixture.manager, "_run", reaped_error)
    with pytest.raises(CloudEnvError, match="HOLD"):
        fixture.manager.issue(role)
    with pytest.raises(CloudEnvError, match="absence does not settle"):
        fixture.manager.revoke(role)
    assert fixture.env.resource("access_rules", role)["status"] == "creating"


@pytest.mark.parametrize("operation", ["get", "add", "del"])
@pytest.mark.parametrize(
    "code,error", [(13, b"Error EACCES: access denied\n"), (1, b"Error EPERM: Operation not permitted\n")]
)
def test_ceph_authorization_denial_is_distinct_from_absence_and_other_errors(
    namespace_access, monkeypatch, operation, code, error
):
    fixture, role = namespace_access, "run-one"
    if operation == "del":
        fixture.manager.issue(role)

    def denied(*args):
        if args[:2] == ("auth", operation):
            return code, b"", error
        return fixture.command(fixture.manager, *args)

    monkeypatch.setattr(fixture.manager, "_run", denied)
    with pytest.raises(CloudEnvError, match=f"auth {operation} authorization denied \\(errno={code}\\)"):
        (fixture.manager.revoke if operation == "del" else fixture.manager.issue)(role)


@pytest.mark.parametrize("reason", ["expired", "approval-changed", "missing-callback"])
def test_ceph_final_spawn_boundary_rechecks_after_fresh_get_and_preserves_no_call_intent(
    namespace_access, monkeypatch, reason
):
    fixture, role = namespace_access, "run-one"
    crossed = []

    def boundary(operation, callback_role):
        assert (operation, callback_role) == ("add", role)
        assert crossed == ["fresh-get-completed"]
        record = fixture.env.resource("access_rules", role)
        assert record["status"] == "creating" and record["command_pending"] is True
        assert (
            json.loads(fixture.env.state_path.read_text())["resources"]["access_rules"][role]["command_pending"] is True
        )
        raise CloudEnvError("synthetic expired or changed original approval")

    if reason == "missing-callback":
        del fixture.env.native_ceph_mutation_boundary
    else:
        fixture.env.native_ceph_mutation_boundary = boundary

    def run(*args):
        if args[:2] == ("auth", "get"):
            result = fixture.command(fixture.manager, *args)
            crossed.append("fresh-get-completed")
            return result
        return fixture.real_run(fixture.manager, *args)

    monkeypatch.setattr(fixture.manager, "_run", run)
    popen = Mock(side_effect=AssertionError("expired or changed approval cannot spawn auth add"))
    monkeypatch.setattr(fixture.module.subprocess, "Popen", popen)
    with pytest.raises(fixture.module.CephCommandError, match="boundary rejected before spawn") as raised:
        fixture.manager.issue(role)
    assert raised.value.command_pending is False and raised.value.started is False
    record = fixture.env.resource("access_rules", role)
    assert record["status"] == "creating" and record["command_pending"] is False and record["no_call"] is True
    assert crossed == ["fresh-get-completed"] and fixture.provider == {}
    popen.assert_not_called()


def test_ceph_auth_get_does_not_require_mutation_callback(namespace_access, monkeypatch):
    fixture = namespace_access
    del fixture.env.native_ceph_mutation_boundary
    seen = []

    def command(argv, *, pass_fds=(), before_spawn=None):
        assert before_spawn is None
        assert len(pass_fds) == 2
        seen.append(argv[-3:])
        return 2, b"", b"Error ENOENT: synthetic"

    monkeypatch.setattr(fixture.module, "_command", command)
    fixture.real_run(fixture.manager, "auth", "get", fixture.manager._entity("run-one"))
    assert seen == [["auth", "get", fixture.manager._entity("run-one")]]


def test_certified_no_spawn_ceph_create_can_release_live_custody_without_refunding_budget(
    namespace_access, monkeypatch
):
    fixture, role = namespace_access, "run-one"
    fixture.env.state["native"] = {"grants": {"approved": {"used": {"access_rules": 1}}}}
    fixture.env.save()

    def expired(operation, callback_role):
        assert (operation, callback_role) == ("add", role)
        raise CloudEnvError("original approval expired before command spawn")

    fixture.env.native_ceph_mutation_boundary = expired

    def command(*args):
        if args[:2] == ("auth", "get"):
            return fixture.command(fixture.manager, *args)
        return fixture.real_run(fixture.manager, *args)

    monkeypatch.setattr(fixture.manager, "_run", command)
    never_spawn = Mock(side_effect=AssertionError("no auth mutation may spawn"))
    monkeypatch.setattr(fixture.module.subprocess, "Popen", never_spawn)
    with pytest.raises(fixture.module.CephCommandError):
        fixture.manager.issue(role)
    fixture.manager.revoke(role)
    assert fixture.env.resource("access_rules", role) is None
    assert fixture.env.state["native"]["grants"]["approved"]["used"]["access_rules"] == 1
    assert fixture.provider == {}
    assert fixture.env.state["history"][-1]["resource"]["no_call"] is True
    never_spawn.assert_not_called()


def test_worker_source_archive_ignores_non_distributed_finder_metadata(tmp_path, monkeypatch):
    import tarfile

    package = tmp_path / "package"
    package.mkdir()
    module = package / "cloud_env.py"
    module.write_bytes(b"SOURCE = 'reviewed package bytes'\n")
    monkeypatch.setattr(cloud_env, "__file__", str(module))
    baseline, baseline_digest = cloud_env.package_archive()
    (package / ".DS_Store").write_bytes(b"Finder metadata must not alter worker source authority")
    actual, actual_digest = cloud_env.package_archive()
    assert actual == baseline and actual_digest == baseline_digest
    with tarfile.open(fileobj=io.BytesIO(actual)) as archive:
        assert archive.getnames() == ["palimpsest_local/cloud_env.py"]
        assert archive.extractfile("palimpsest_local/cloud_env.py").read() == module.read_bytes()
