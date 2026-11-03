"""Offline validators and injected syscalls only; never boot, mount or load a GPU."""

import errno
import hashlib
import json
import os
import shlex
import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from palimpsest_local import nova_ceph_boot as boot

INSTANCE = "12345678-1234-4234-8234-123456789abc"
OTHER_INSTANCE = "12345678-1234-4234-8234-123456789abd"
PROJECT = "1" * 32
GPU_UUID = "GPU-12345678-1234-4234-8234-123456789abc"


def digest(char):
    return "sha256:" + char * 64


def descriptor(char, media="application/vnd.oci.image.manifest.v1+json"):
    return {"digest": digest(char), "size": 128, "mediaType": media}


@pytest.fixture
def plan():
    compressed = [
        descriptor("c", "application/vnd.oci.image.layer.v1.tar+gzip"),
        descriptor("d", "application/vnd.oci.image.layer.v1.tar+gzip"),
    ]
    config_descriptor = descriptor("b", "application/vnd.oci.image.config.v1+json")
    manifest = {"schemaVersion": 2, "config": config_descriptor, "layers": compressed}
    config = {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [digest("e"), digest("f")]},
        "config": {
            "Entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"],
            "Cmd": ["bash"],
            "Env": ["PATH=/usr/bin:/bin", "VALUE=original"],
            "WorkingDir": "/work",
            "User": "1000:1001",
        },
    }
    return {
        "run_id": "native-proof-01",
        "server_id": INSTANCE,
        "project_id": PROJECT,
        "carrier_digest": digest("9"),
        "image": {
            "source": {
                "reference": "docker.io/nvidia/cuda@" + digest("a"),
                "root_digest": digest("a"),
                "manifest_digest": digest("a"),
                "config_digest": digest("b"),
                "root_descriptor": descriptor("a"),
                "manifest_descriptor": descriptor("a"),
                "config_descriptor": config_descriptor,
                "root_document": manifest,
                "manifest": manifest,
                "config": config,
            },
            "runtime": {
                "entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"],
                "cmd": ["bash"],
                "env": ["PATH=/usr/bin:/bin", "VALUE=original"],
                "working_dir": "/work",
                "user": "1000:1001",
            },
            "layers": [
                {"digest": digest("1"), "size": 128, "ordinal": 0, "compressed": compressed[0], "diff_id": digest("e")},
                {"digest": digest("2"), "size": 256, "ordinal": 1, "compressed": compressed[1], "diff_id": digest("f")},
            ],
        },
        "network": {
            "address": "192.168.0.17",
            "cidr": "192.168.0.0/24",
            "gateway": "192.168.0.1",
            "dns": ["8.8.8.8", "8.8.4.4"],
        },
        "upper": {"label": "PAL-UPPER"},
        "gpu": {
            "enabled": False,
            "kernel_release": "6.8.0-147-generic",
            "driver_version": "580.178.04",
            "expected_pci_alias": "nvidia",
        },
        "command": ["/usr/bin/true"],
    }


@pytest.fixture
def seed(plan):
    return {
        "run_id": plan["run_id"],
        "nonce": "a" * 32,
        "worker_address": "172.30.101.63",
        "worker_known_hosts": "/bootstrap/known_hosts",
        "ssh_private_key": "/bootstrap/tunnel.key",
        "client_cert": "/bootstrap/client.pem",
        "client_key": "/bootstrap/client.key",
        "ca_file": "/bootstrap/ca.pem",
        "tls_server_name": "worker-bootstrap",
        "bootstrap_url": "https://127.0.0.1:9443",
        "deadline": 2000000000,
        "artifacts_manifest": "/bootstrap/artifacts.json",
        "expected_plan_digest": boot.plan_policy_digest(plan),
        "carrier_digest": plan["carrier_digest"],
        "project_id": PROJECT,
        "expected_image_digest": digest("a"),
    }


def test_seed_is_bound_and_contains_no_ceph_key(seed):
    assert boot.validate_seed(seed, now=1000) is seed
    assert all("ceph" not in key for key in seed)
    for forbidden in ("ceph_access", "secret", "ceph_key", "worker_password"):
        with pytest.raises(boot.BootError, match="seed_fields"):
            boot.validate_seed({**seed, forbidden: "not-allowed"}, now=1000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "../other"),
        ("nonce", "A" * 32),
        ("worker_address", "evil.invalid"),
        ("ssh_private_key", "/etc/ssh/id_rsa"),
        ("client_key", "/bootstrap/../key"),
        ("ca_file", "/bootstrap/subdir/ca.pem"),
        ("bootstrap_url", "https://elsewhere.invalid"),
        ("tls_server_name", "worker\nheader"),
        ("deadline", 1),
        ("deadline", True),
        ("project_id", "human-project"),
        ("carrier_digest", "9" * 64),
        ("expected_image_digest", None),
    ],
)
def test_seed_rejects_unbound_or_unsafe_inputs(seed, field, value):
    with pytest.raises(boot.BootError):
        boot.validate_seed({**seed, field: value}, now=1000)


def test_plan_accepts_exact_metadata_and_seed_policy(plan, seed):
    assert boot.validate_plan(plan, seed, INSTANCE, "192.168.0.17") is plan


def test_policy_digest_excludes_only_fresh_server_and_address(plan):
    changed = deepcopy(plan)
    changed["server_id"] = OTHER_INSTANCE
    changed["network"]["address"] = "192.168.0.18"
    assert boot.plan_policy_digest(changed) == boot.plan_policy_digest(plan)
    for field, value in (("gateway", "192.168.0.2"), ("dns", ["1.1.1.1"]), ("cidr", "192.168.0.0/25")):
        changed = deepcopy(plan)
        changed["network"][field] = value
        assert boot.plan_policy_digest(changed) != boot.plan_policy_digest(plan)
    changed = deepcopy(plan)
    changed["command"] = ["/bin/sh"]
    assert boot.plan_policy_digest(changed) != boot.plan_policy_digest(plan)


@pytest.mark.parametrize("instance,address", [(OTHER_INSTANCE, "192.168.0.17"), (INSTANCE, "192.168.0.18")])
def test_plan_never_accepts_metadata_or_dhcp_mismatch(plan, seed, instance, address):
    with pytest.raises(boot.BootError):
        boot.validate_plan(plan, seed, instance, address)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(project_id="2" * 32),
        lambda p: p.update(carrier_digest=digest("8")),
        lambda p: p["upper"].update(label="OTHER"),
        lambda p: p.update(command=[]),
        lambda p: p["image"]["runtime"].update(user="root"),
        lambda p: p["image"]["source"]["root_descriptor"].update(digest=digest("0")),
        lambda p: p["image"]["source"]["config_descriptor"].update(digest=digest("0")),
        lambda p: p["image"]["source"]["config"]["rootfs"].update(diff_ids=[digest("f"), digest("e")]),
        lambda p: p["image"]["source"]["config"].update(architecture="arm64"),
        lambda p: p["image"]["layers"].reverse(),
    ],
)
def test_plan_and_original_image_projection_are_fail_closed(plan, seed, mutation):
    mutation(plan)
    # Updating the policy digest does not excuse independent source projection,
    # project, root-image or fixed upper-label validation.
    seed["expected_plan_digest"] = boot.plan_policy_digest(plan)
    with pytest.raises(boot.BootError):
        boot.validate_plan(plan, seed, INSTANCE, "192.168.0.17")


def test_expected_image_pin_is_upstream_root_not_selected_manifest(plan, seed):
    selected = deepcopy(plan["image"]["source"]["manifest_descriptor"])
    selected["platform"] = {"os": "linux", "architecture": "amd64"}
    plan["image"]["source"]["root_digest"] = digest("7")
    plan["image"]["source"]["root_descriptor"] = descriptor("7", "application/vnd.oci.image.index.v1+json")
    plan["image"]["source"]["root_document"] = {"manifests": [selected]}
    plan["image"]["source"]["reference"] = "docker.io/nvidia/cuda@" + digest("7")
    seed["expected_plan_digest"] = boot.plan_policy_digest(plan)
    seed["expected_image_digest"] = digest("7")
    boot.validate_plan(plan, seed, INSTANCE, "192.168.0.17")
    seed["expected_image_digest"] = digest("a")
    with pytest.raises(boot.BootError):
        boot.validate_plan(plan, seed, INSTANCE, "192.168.0.17")


def test_layer_paths_are_canonical_and_overlay_order_reversed(plan):
    layers = plan["image"]["layers"]
    assert boot.ordered_lower_paths(layers) == ["blobs/sha256/" + "1" * 64, "blobs/sha256/" + "2" * 64]
    assert boot.overlay_lowers(["/run/lower/0", "/run/lower/1"]) == "/run/lower/1:/run/lower/0"
    layers[0]["path"] = "../../etc/shadow"
    with pytest.raises(boot.BootError, match="layer_path"):
        boot.ordered_lower_paths(layers)


def test_duplicate_occurrences_retain_separate_mounts(plan):
    layers = plan["image"]["layers"]
    layers[1]["digest"] = layers[0]["digest"]
    assert len(boot.ordered_lower_paths(layers)) == 2
    with pytest.raises(boot.BootError):
        boot.overlay_lowers(["/run/lower/0", "/run/lower/0"])


def test_original_entrypoint_env_cwd_user_and_explicit_command(plan):
    runtime = plan["image"]["runtime"]
    before = deepcopy(runtime)
    argv, env, cwd, user = boot.runtime_vector(runtime, ["/probe", "two words"])
    assert argv == ["/opt/nvidia/nvidia_entrypoint.sh", "/probe", "two words"]
    assert env == {"PATH": "/usr/bin:/bin", "VALUE": "original"}
    assert (cwd, user) == ("/work", "1000:1001")
    assert runtime == before
    assert boot.runtime_vector(runtime, [])[0][-1] == "bash"
    del runtime["cmd"]
    assert boot.runtime_vector(runtime, [])[0] == runtime["entrypoint"]
    runtime["cmd"] = None
    assert boot.runtime_vector(runtime, ["/probe"])[0][-1] == "/probe"


@pytest.mark.parametrize(
    "field,value",
    [
        ("entrypoint", "sh -c"),
        ("env", ["NOT_AN_ASSIGNMENT"]),
        ("env", ["BAD-NAME=value"]),
        ("working_dir", "relative"),
        ("user", "missing\nroot"),
        ("cmd", ["nul\x00arg"]),
    ],
)
def test_runtime_rejects_shell_coercion_and_malformed_config(plan, field, value):
    runtime = {**plan["image"]["runtime"], field: value}
    with pytest.raises(boot.BootError):
        boot.runtime_vector(runtime, ["/probe"])


def test_image_user_resolution_never_silently_becomes_root():
    passwd = "root:x:0:0::/:/bin/sh\nworker:x:1000:1001::/work:/bin/sh\n"
    group = "worker:x:1001:\nrender:x:109:worker\n"
    assert boot.resolve_user("worker", passwd, group) == (1000, 1001, [109])
    assert boot.resolve_user("worker:render", passwd, group) == (1000, 109, [])
    assert boot.resolve_user("1000:1001", passwd, group) == (1000, 1001, [])
    assert boot.resolve_user("2000", passwd, group) == (2000, 0, [])
    assert boot.resolve_user("", passwd, group) == (0, 0, [])
    for user in ("missing", "worker:missing", ":worker", "worker:", "4294967295"):
        with pytest.raises(boot.BootError):
            boot.resolve_user(user, passwd, group)


@pytest.mark.parametrize(
    "number,accepted",
    [
        (errno.EACCES, True),
        (errno.EROFS, True),
        (errno.ENOENT, False),
        (errno.EIO, False),
        (errno.ENOSPC, False),
        (errno.EPERM, False),
        (errno.EEXIST, False),
    ],
)
def test_provider_ro_requires_only_exact_storage_denials(number, accepted):
    assert boot.readonly_denial(OSError(number, "private secret")) is accepted
    assert boot.readonly_denial(None) is False


def test_actual_root_and_pid1_must_match_overlay_identity():
    boot.assert_root_identity((42, 99), (42, 99), (42, 99), "overlay")
    for root, proc1, fs in (
        ((41, 99), (42, 99), "overlay"),
        ((42, 99), (41, 99), "overlay"),
        ((42, 99), (42, 99), "ext4"),
    ):
        with pytest.raises(boot.BootError, match="actual_root_identity"):
            boot.assert_root_identity(root, proc1, (42, 99), fs)


def test_hash_verification_rejects_payload_changes_and_symlinks(tmp_path):
    directory = tmp_path.resolve()
    payload = directory / "blob"
    payload.write_bytes(b"canonical lower")
    hashed = hashlib.sha256(payload.read_bytes()).hexdigest()
    assert boot.verify_file(str(payload), hashed, 15)["sha256"] == hashed
    with pytest.raises(boot.BootError, match="file_metadata"):
        boot.verify_file(str(payload), hashed, 16)
    payload.write_bytes(b"different lower")
    with pytest.raises(boot.BootError, match="file_hash"):
        boot.verify_file(str(payload), hashed, 15)
    alias = directory / "alias"
    alias.symlink_to(payload)
    with pytest.raises(OSError):
        boot.verify_file(str(alias), hashed, 15)
    physical = directory / "physical"
    physical.mkdir()
    (physical / "blob").write_bytes(b"canonical lower")
    (directory / "linked-directory").symlink_to(physical, target_is_directory=True)
    with pytest.raises(OSError):
        boot.verify_file(str(directory / "linked-directory" / "blob"), hashed, 15)


def test_json_is_bounded_duplicate_free_and_finite():
    for content in (b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b"{broken", b"x" * (boot.MAX_JSON + 1)):
        with pytest.raises(boot.BootError):
            boot.strict_json(content)
    assert boot.strict_json(b'{"x":1}') == {"x": 1}
    large = b'{"value":"' + b"x" * (boot.MAX_JSON + 1) + b'"}'
    assert len(boot.strict_json(large, maximum=32 * boot.MAX_JSON)["value"]) > boot.MAX_JSON


@pytest.fixture
def artifacts():
    return {
        "schema": boot.ARTIFACT_SCHEMA,
        "kernel_release": "6.8.0-147-generic",
        "driver_version": "580.178.04",
        "files": {
            "bin/busybox": {"sha256": "a" * 64, "size": 128, "mode": 0o755},
            "driver/usr/bin/nvidia-smi": {"sha256": "b" * 64, "size": 256, "mode": 0o755},
        },
    }


def test_artifact_inventory_is_explicit_and_pinned(artifacts):
    assert boot.validate_artifacts_manifest(artifacts) is artifacts
    for path in ("", "../outside", "/absolute", "driver/../../etc/shadow"):
        changed = deepcopy(artifacts)
        changed["files"][path] = changed["files"].pop("bin/busybox")
        with pytest.raises(boot.BootError):
            boot.validate_artifacts_manifest(changed)
    changed = deepcopy(artifacts)
    changed["files"]["bin/busybox"]["mode"] = 0o777
    with pytest.raises(boot.BootError):
        boot.validate_artifacts_manifest(changed)


def test_bootstrap_envelope_and_ceph_credentials_bound_to_nonce(seed, plan):
    access = {
        "export": "172.30.0.11:6789,172.30.0.12:6789:/volumes/native/readonly",
        "name": "native-client",
        "secret": "A" * 40 + "==",
    }
    reply = {
        "schema": boot.BOOTSTRAP_SCHEMA,
        "run_id": seed["run_id"],
        "nonce": seed["nonce"],
        "expires_unix": 1999999999,
        "plan": plan,
        "ceph_access": access,
    }
    assert boot.validate_bootstrap(reply, seed, now=1000) == (plan, access)
    for change in (
        {"nonce": "b" * 32},
        {"run_id": "other"},
        {"expires_unix": 1},
        {"expires_unix": 2000000001},
        {"extra": "field"},
    ):
        with pytest.raises(boot.BootError):
            boot.validate_bootstrap({**reply, **change}, seed, now=1000)
    for field, value in (
        ("name", "evil,secret=argument"),
        ("secret", "secret\npassword"),
        ("export", "172.30.0.11:/../../etc"),
        ("export", "host.invalid:/volumes/native"),
    ):
        with pytest.raises(boot.BootError):
            boot.validate_bootstrap({**reply, "ceph_access": {**access, field: value}}, seed, now=1000)


@pytest.mark.parametrize("damage", [None, "export", "fs_name", "missing-fs", "invalid-fs"])
def test_namespace_capability_matches_the_seed_bound_storage_plan(seed, plan, damage):
    storage = {
        "export": "192.0.2.20:6789:/volumes/fixture/cas/user-abc",
        "fs_name": "cephfs",
        "data_pool": "cephfs_data",
        "namespace": "pal-user-abc",
    }
    plan["storage"] = storage
    seed["expected_plan_digest"] = boot.plan_policy_digest(plan)
    boot.validate_plan(plan, seed, INSTANCE, "192.168.0.17")
    access = {
        "export": storage["export"],
        "fs_name": storage["fs_name"],
        "name": "pal-native-fixture",
        "secret": "A" * 40 + "==",
    }
    if damage == "export":
        access["export"] = "192.0.2.20:6789:/volumes/fixture/cas/public"
    elif damage == "fs_name":
        access["fs_name"] = "anotherfs"
    elif damage == "missing-fs":
        del access["fs_name"]
    elif damage == "invalid-fs":
        access["fs_name"] = "cephfs,secret=bad"
    reply = {
        "schema": boot.BOOTSTRAP_SCHEMA,
        "run_id": seed["run_id"],
        "nonce": seed["nonce"],
        "expires_unix": 1999999999,
        "plan": plan,
        "ceph_access": access,
    }
    if damage:
        with pytest.raises(boot.BootError):
            boot.validate_bootstrap(reply, seed, now=1000)
    else:
        selected, credential = boot.validate_bootstrap(reply, seed, now=1000)
        assert selected["storage"]["namespace"] == "pal-user-abc"
        assert credential["export"] == storage["export"]
        assert credential["fs_name"] == storage["fs_name"]


def test_tls_uses_explicit_worker_identity_and_verified_client_certificate(seed, monkeypatch):
    calls = []

    class Context:
        def load_cert_chain(self, cert, key):
            calls.append((cert, key))

    context = Context()

    def create_default_context(purpose, *, cafile):
        calls.append((purpose, cafile))
        return context

    monkeypatch.setattr(boot.ssl, "create_default_context", create_default_context)
    client = boot.TLSClient(seed)
    assert client.context is context
    assert calls[0][1] == seed["ca_file"]
    assert calls[1] == (seed["client_cert"], seed["client_key"])
    assert context.minimum_version == boot.ssl.TLSVersion.TLSv1_2
    # post() must use context.wrap_socket(server_hostname=seed TLS identity),
    # not HTTPConnection or an unverified HTTPS context.
    assert seed["tls_server_name"] != "127.0.0.1"


def test_secrets_never_appear_in_failure_results(seed):
    secret = "AQ_PRIVATE_CEPH_SECRET_AND_CLIENT_KEY"
    for error in (OSError(errno.EACCES, secret), RuntimeError(secret), boot.BootError(secret)):
        result = boot.failure_result(seed["run_id"], error)
        assert secret not in json.dumps(result)
        assert result["success"] is False
        assert result["failure"] == "native_boot_failed"
        assert result["trust"] == "trusted-workload-observation-not-attestation"


@pytest.mark.parametrize(
    "error,expected_type,expected_errno",
    [
        (FileNotFoundError(errno.ENOENT, "private-key-and-ceph-secret"), "FileNotFoundError", errno.ENOENT),
        (boot.ssl.SSLError(1, "private-key-and-ceph-secret"), "SSLError", 1),
        (type("private_key_and_ceph_secret", (RuntimeError,), {})("private-key-and-ceph-secret"), "Exception", None),
    ],
)
def test_main_retains_safe_failure_operation_without_secret_messages(
    seed, monkeypatch, capsys, error, expected_type, expected_errno
):
    retained = []

    class DiagnosticRuntime:
        failure_stage = "prepare_tls"

        def __init__(self, seed, **kwargs):
            self.seed = seed

        def execute(self):
            raise error

        def post_result(self, result):
            retained.append(result)

    class RetainedPID1(BaseException):
        pass

    def retain():
        raise RetainedPID1()

    monkeypatch.setattr(boot, "_runtime_identity", lambda: ("linux", 0, 1))
    monkeypatch.setattr(boot, "read_regular", lambda *args, **kwargs: json.dumps(seed).encode())
    monkeypatch.setattr(boot, "Runtime", DiagnosticRuntime)
    monkeypatch.setattr(boot, "retain_pid1", retain)
    with pytest.raises(RetainedPID1):
        boot.main(["--seed", "/bootstrap/seed.json"])
    output = capsys.readouterr().err
    diagnostic = json.loads(output.split("palimpsest native boot diagnostic: ", 1)[1])
    assert diagnostic == {"stage": "prepare_tls", "error_type": expected_type, "errno": expected_errno}
    assert "private-key-and-ceph-secret" not in output
    assert "private_key_and_ceph_secret" not in output
    assert retained[0]["failure"] == "native_boot_failed"
    assert retained[0]["disposition"] == "HOLD"
    assert retained[0]["success"] is False


def test_diagnostic_rejects_arbitrary_operation_and_errno():
    error = OSError(9000, "private-path-or-credential")
    assert boot._failure_diagnostic("private-path-or-credential", error) == {
        "stage": "unknown",
        "error_type": "OSError",
        "errno": None,
    }


def test_injected_mount_failure_stops_before_root_transition(monkeypatch):
    calls = []

    class Calls:
        def mount(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise boot.BootError("injected_mount_failure")

        def chroot(self, path):
            pytest.fail("must not chroot after mount failure")

        def chdir(self, path):
            pytest.fail("must not chdir after mount failure")

    monkeypatch.setattr(boot, "_process_id", lambda: 1)
    monkeypatch.setattr(boot, "_mount_entry", lambda *args, **kwargs: {"fstype": "overlay"})
    monkeypatch.setattr(boot, "_mkdir", lambda path: None)
    with pytest.raises(boot.BootError, match="injected_mount_failure"):
        boot.switch_actual_root(Calls(), "/newroot", (42, 99))
    assert calls == [(("/dev", "/newroot/dev"), {"flags": boot.MS_MOVE})]


def test_actual_transition_moves_mounts_and_root_before_pid1_check(monkeypatch):
    calls = []

    class Calls:
        def mount(self, *args, **kwargs):
            calls.append(("mount", args, kwargs))

        def chroot(self, path):
            calls.append(("chroot", path))

        def chdir(self, path):
            calls.append(("chdir", path))

        def identity(self, path):
            return 42, 99

    monkeypatch.setattr(boot, "_process_id", lambda: 1)
    monkeypatch.setattr(boot, "_mount_entry", lambda *args, **kwargs: {"fstype": "overlay"})
    monkeypatch.setattr(boot, "_mkdir", lambda path: None)
    boot.switch_actual_root(Calls(), "/newroot", (42, 99))
    assert [item[1][0] for item in calls[:4]] == ["/dev", "/sys", "/proc", "/run"]
    assert calls[4:] == [
        ("chdir", "/newroot"),
        ("mount", ("/newroot", "/"), {"flags": boot.MS_MOVE}),
        ("chroot", "."),
        ("chdir", "/"),
    ]


def test_libc_mount_error_is_never_ignored(monkeypatch):
    class Mount:
        def __call__(self, *args):
            return -1

    with pytest.raises(boot.BootError, match="mount_failed"):
        boot.Syscalls(library=SimpleNamespace(mount=Mount())).mount("overlay", "/newroot", "overlay")


@pytest.mark.parametrize("platform,uid,pid", [("darwin", 0, 1), ("linux", 501, 1), ("linux", 0, 42)])
def test_real_main_refuses_non_linux_non_root_and_non_pid1(monkeypatch, platform, uid, pid, capsys):
    monkeypatch.setattr(boot, "_runtime_identity", lambda: (platform, uid, pid))
    monkeypatch.setattr(boot, "read_regular", lambda *args, **kwargs: pytest.fail("must refuse before reading seed"))
    assert boot.main(["--seed", "/bootstrap/seed.json"]) == 1
    assert "Linux root initramfs PID1 required" in capsys.readouterr().err


def test_cpu_mode_does_not_touch_driver_or_device_syscalls(seed, plan):
    runtime = boot.Runtime(seed, syscalls=object())
    runtime.plan = plan
    assert runtime.gpu() == {"enabled": False}


def test_dhcp_callback_preserves_gateway_reachability(tmp_path):
    # Model the observed BusyBox 1.36 route selector and Linux's requirement
    # that a gateway remain reachable through a connected route. Execute the
    # actual callback shell and assert route/resolver outcomes, not captured argv.
    state = tmp_path / "network.json"
    state.write_text("{}")
    model = tmp_path / "network.py"
    model.write_text(
        """import ipaddress,json,sys
from pathlib import Path
p=Path(sys.argv[1]); state=json.loads(p.read_text()); args=sys.argv[2:]
if args[0]=='ifconfig':
    network=ipaddress.IPv4Interface(args[2]+'/'+args[4]).network
    state[str(network)]=None
else:
    args=args[1:]; family=None
    if args[0]=='-4': family=4; args=args[1:]
    if args[:2]==['addr','flush']:
        state={}
    elif args[:2]==['route','flush']:
        prefix=args[2]
        # With AF_UNSPEC, BusyBox's 'default' filter is disabled. A numeric
        # IPv4 prefix (or explicit IPv4 family) enables the exact filter.
        if prefix!='default': family=4
        target=ipaddress.ip_network('0.0.0.0/0' if prefix=='default' else prefix)
        state={k:v for k,v in state.items() if family==4 and ipaddress.ip_network(k)!=target}
    elif args[0]=='route' and args[1] in ('add','replace'):
        prefix='0.0.0.0/0' if args[2]=='default' else args[2]
        if args[1]=='add' and prefix in state:
            sys.exit(2)
        gateway=ipaddress.IPv4Address(args[args.index('via')+1])
        if not any(v is None and gateway in ipaddress.ip_network(k) for k,v in state.items()):
            sys.exit(2)
        state[prefix]=str(gateway)
    else:
        raise AssertionError(args)
p.write_text(json.dumps(state))
"""
    )
    helper = tmp_path / "busybox"
    helper.write_text(f'#!/bin/sh\nexec {shlex.join([sys.executable, str(model), str(state)])} "$@"\n')
    helper.chmod(0o700)
    resolver = tmp_path / "resolv.conf"
    script = boot.dhcp_script().replace("/bin/busybox", shlex.quote(str(helper)))
    script = script.replace("/run/resolv.conf", shlex.quote(str(resolver)))
    result = subprocess.run(
        ["/bin/sh", "-s", "bound"],
        input=script,
        text=True,
        capture_output=True,
        timeout=10,
        env={
            **os.environ,
            "interface": "test0",
            "ip": "192.0.2.2",
            "subnet": "255.255.255.0",
            "router": "192.0.2.1",
            "dns": "192.0.2.53 192.0.2.54",
            "staticroutes": "169.254.169.254/32 192.0.2.1 0.0.0.0/0 192.0.2.1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text()) == {
        "192.0.2.0/24": None,
        "0.0.0.0/0": "192.0.2.1",
        "169.254.169.254/32": "192.0.2.1",
    }
    assert resolver.read_text() == "nameserver 192.0.2.53\nnameserver 192.0.2.54\n"


def test_cuda_result_requires_gpu_uuid_and_actual_expected_computation():
    observation = {
        "success": True,
        "device_index": 0,
        "device_uuid": GPU_UUID,
        "device_name": "NVIDIA Tesla",
        "results": [2, 6, 12, 20, 30, 42, 56, 72, 90],
        "checksum": 330,
        "runtime_version": 12020,
        "cublas_results": [90, 114, 138, 54, 69, 84, 18, 24, 30],
        "cublas_checksum": 621,
    }
    stdout = b"Original NVIDIA entrypoint banner\n" + boot.canonical(observation) + b"\n"
    assert boot.validate_cuda_result(stdout, GPU_UUID, 0) == observation
    for result, gpu_uuid, status in (
        (observation, "GPU-other", 0),
        (observation, GPU_UUID, 1),
        ({**observation, "success": False}, GPU_UUID, 0),
        ({**observation, "results": [0] * 9}, GPU_UUID, 0),
        ({**observation, "checksum": 0}, GPU_UUID, 0),
        ({**observation, "runtime_version": 12010}, GPU_UUID, 0),
        ({**observation, "cublas_results": [0] * 9}, GPU_UUID, 0),
        ({**observation, "cublas_checksum": 0}, GPU_UUID, 0),
    ):
        with pytest.raises(boot.BootError):
            boot.validate_cuda_result(boot.canonical(result), gpu_uuid, status)
    with pytest.raises(boot.BootError):
        boot.validate_cuda_result(b"CPU fallback succeeded", GPU_UUID, 0)


def test_pci_normalization_reconciles_sysfs_and_nvidia_smi_domains():
    assert boot.normalize_pci("00000000:00:05.0") == boot.normalize_pci("0000:00:05.0")
    assert boot.normalize_pci("0000:AB:CD.1") == "0000:ab:cd.1"
    with pytest.raises(boot.BootError):
        boot.normalize_pci("not-a-device")


def process(pid, *, parent=1, group=None, session=None, started=None, kernel=False):
    return {
        "state": "S",
        "ppid": parent,
        "pgrp": group if group is not None else pid,
        "session": session if session is not None else pid,
        "start": started if started is not None else pid * 10,
        "kernel": kernel,
    }


def test_process_census_tracks_escaped_descendants_but_not_unknown_orphans():
    snapshot = {
        1: process(1),
        2: process(2, kernel=True),
        200: process(200),
        10: process(10),
        11: process(11, parent=10, group=11, session=11),
    }
    owned, unknown = boot.classify_processes(snapshot, {10: None}, {200: 2000}, 10)
    assert owned == {10: 100, 11: 110} and not unknown
    snapshot[12] = process(12)
    owned, unknown = boot.classify_processes(snapshot, owned, {200: 2000}, 10)
    assert unknown == {12} and 12 not in owned
    snapshot[11]["start"] = 111
    with pytest.raises(boot.HoldError, match="workload_pid_reused"):
        boot.classify_processes(snapshot, owned, {200: 2000}, 10)


def test_auxiliary_ssh_identity_must_remain_exact():
    with pytest.raises(boot.HoldError, match="auxiliary_process_changed"):
        boot.classify_processes({200: process(200, started=2001)}, {}, {200: 2000}, 10)


def test_proc_stat_parser_handles_parenthesized_names():
    fields = ["S", "1", "10", "10", "0", "0", "0"] + ["0"] * 12 + ["12345"]
    record = boot.process_record("10 (worker (nested) name) " + " ".join(fields))
    assert (record["ppid"], record["pgrp"], record["session"], record["start"]) == (1, 10, 10, 12345)


def test_helper_timeout_retains_without_force_or_unbounded_wait(monkeypatch):
    signals = []

    class Helper:
        pid = 4242

        def communicate(self, *, timeout):
            raise boot.subprocess.TimeoutExpired("nonsecret-helper", timeout)

    helper = Helper()
    monkeypatch.setattr(boot.subprocess, "Popen", lambda *args, **kwargs: helper)
    monkeypatch.setattr(boot.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(boot, "_RETAINED_PROCESSES", [])
    with pytest.raises(boot.HoldError, match="helper_term_unsettled"):
        boot._run(["nonsecret-helper"], timeout=1)
    assert signals == [(4242, boot.signal.SIGTERM)]
    assert boot._RETAINED_PROCESSES == [helper]


def test_cpu_supervisor_never_captures_or_limits_console_output(monkeypatch):
    before = {1: process(1), 200: process(200)}
    running = {**before, 123: process(123)}
    snapshots = iter([before, running, before, before, before])
    waits = iter([(123, 0), (0, 0), (0, 0), (0, 0)])

    def absent_group(pid, sig):
        assert sig == 0
        raise ProcessLookupError()

    # Complete local facades: no host fork, signal registration, proc scan,
    # wait, pipe or Linux identity impersonation is possible in this test.
    fake_os = SimpleNamespace(
        fork=lambda: 123,
        WNOHANG=1,
        waitpid=lambda *args: next(waits),
        waitstatus_to_exitcode=lambda status: 0,
        killpg=absent_group,
        pipe=lambda: pytest.fail("CPU stdout must stay on the console"),
    )
    fake_signal = SimpleNamespace(SIGTERM=15, SIGINT=2, SIGHUP=1, SIGQUIT=3, signal=lambda *args: None)
    monkeypatch.setattr(boot, "os", fake_os)
    monkeypatch.setattr(boot, "signal", fake_signal)
    monkeypatch.setattr(boot, "time", SimpleNamespace(time=lambda: 100, monotonic=lambda: 0, sleep=lambda _: None))
    monkeypatch.setattr(boot, "_process_id", lambda: 1)
    monkeypatch.setattr(boot, "process_snapshot", lambda: next(snapshots))
    assert boot.supervise(["/proof"], {}, "/", 0, 0, [], 1000, auxiliary_pids={200}) == (0, b"")


def test_storage_backings_are_outside_every_moved_mount():
    for suffix in ("ceph", "lower", "private/upper", "private/work", "ceph-key"):
        path = boot.INTERNAL + "/" + suffix
        assert all(path != moved and not path.startswith(moved + "/") for moved in boot.MOVED_MOUNTS)


def test_symlink_resolver_mountpoint_is_rejected(seed, tmp_path):
    directory = tmp_path.resolve()
    target = directory / "resolv.conf"
    target.symlink_to(directory / "outside")

    class Calls:
        def mount(self, *args, **kwargs):
            pytest.fail("must refuse before mounting over symlink")

    runtime = boot.Runtime(seed, syscalls=Calls())
    with pytest.raises(boot.BootError, match="bind_mountpoint"):
        runtime.readonly_bind("/run/resolv.conf", str(target), directory=False)


def test_result_is_not_posted_when_observed_proc1_root_changes(seed, plan, monkeypatch):
    class Calls:
        def identity(self, path):
            return (42, 99) if path == "/" else (42, 100)

    class ImagePath:
        def __init__(self, path):
            pass

        def exists(self):
            return False

    runtime = boot.Runtime(seed, syscalls=Calls())
    runtime.plan = plan
    runtime.tunnel = SimpleNamespace(pid=200)
    runtime.prepare = lambda: None
    runtime.network = lambda: "192.168.0.17"
    runtime.bootstrap = lambda address: {}
    runtime.ceph = lambda access: None
    runtime.assemble = lambda: (42, 99)
    runtime.gpu = lambda: {"enabled": False}
    runtime.preserve = lambda: None
    posted = []
    runtime.post_result = posted.append
    monkeypatch.setattr(boot, "Path", ImagePath)
    monkeypatch.setattr(boot, "switch_actual_root", lambda *args: None)
    monkeypatch.setattr(boot, "_mount_entry", lambda *args, **kwargs: {"fstype": "overlay", "id": "1"})
    monkeypatch.setattr(boot, "supervise", lambda *args, **kwargs: (0, b""))
    with pytest.raises(boot.BootError, match="actual_root_identity"):
        runtime.execute()
    assert posted == []


def test_real_main_admission_is_exercised_without_identity_mock(monkeypatch):
    if boot._runtime_identity() == ("linux", 0, 1):
        pytest.skip("real initramfs PID1 must not enter runtime in unit tests")
    monkeypatch.setattr(
        boot, "read_regular", lambda *args, **kwargs: pytest.fail("real host must refuse before seed read")
    )
    assert boot.main(["--seed", "/bootstrap/seed.json"]) == 1


def test_counted_nova_gpu_alias_is_policy_valid(plan, seed):
    plan["gpu"].update(enabled=True, expected_pci_alias="RTX-2000Ada:1,AD107-audio:1")
    seed["expected_plan_digest"] = boot.plan_policy_digest(plan)
    boot.validate_plan(plan, seed, INSTANCE, "192.168.0.17")


def test_fixed_failure_codes_are_preserved_without_exception_messages(seed):
    with pytest.raises(boot.BootError) as caught:
        boot.fail("provider_readonly_not_proven")
    result = boot.failure_result(seed["run_id"], caught.value)
    assert result["failure"] == "provider_readonly_not_proven"
    assert result["disposition"] == "HOLD"


def test_authenticated_enrollment_narrows_runtime_and_tls_deadlines(seed, plan, monkeypatch):
    access = {"export": "172.30.0.11:6789:/volumes/native/readonly", "name": "native-client", "secret": "A" * 40 + "=="}
    reply = {
        "schema": boot.BOOTSTRAP_SCHEMA,
        "run_id": seed["run_id"],
        "nonce": seed["nonce"],
        "expires_unix": 5000,
        "plan": plan,
        "ceph_access": access,
    }
    writes = []

    class WritablePath:
        def __init__(self, path):
            self.path = path

        def write_text(self, content):
            writes.append((self.path, content))

    runtime = boot.Runtime(seed, syscalls=object())
    runtime.client = SimpleNamespace(seed=dict(seed), post=lambda *args: reply)
    runtime.tunnel = SimpleNamespace(poll=lambda: None)
    runtime.artifacts = {"kernel_release": plan["gpu"]["kernel_release"]}
    runtime.network_observed = {key: plan["network"][key] for key in ("cidr", "gateway", "dns")}
    monkeypatch.setattr(boot, "time", SimpleNamespace(time=lambda: 1000, monotonic=lambda: 1000))
    monkeypatch.setattr(boot, "metadata_instance", lambda: INSTANCE)
    monkeypatch.setattr(boot, "Path", WritablePath)
    assert runtime.bootstrap("192.168.0.17") is access
    assert runtime.seed["deadline"] == runtime.client.seed["deadline"] == 5000
    assert seed["deadline"] == 2000000000
    assert writes == [("/run/resolv.conf", "nameserver 8.8.8.8\nnameserver 8.8.4.4\n")]


@pytest.mark.parametrize("bad_binding", [None, "cidr", "gateway", "plan_dns"])
def test_bootstrap_uses_pinned_dns_without_trusting_dhcp_policy(seed, plan, monkeypatch, tmp_path, bad_binding):
    access = {"export": "172.30.0.11:6789:/volumes/native/readonly", "name": "native-client", "secret": "A" * 40 + "=="}
    reply_plan = deepcopy(plan)
    if bad_binding == "plan_dns":
        reply_plan["network"]["dns"] = ["1.1.1.1"]
    reply = {
        "schema": boot.BOOTSTRAP_SCHEMA,
        "run_id": seed["run_id"],
        "nonce": seed["nonce"],
        "expires_unix": 5000,
        "plan": reply_plan,
        "ceph_access": access,
    }
    runtime = boot.Runtime(seed, syscalls=object())
    runtime.client = SimpleNamespace(seed=dict(seed), post=lambda *args: reply)
    runtime.tunnel = SimpleNamespace(poll=lambda: None)
    runtime.artifacts = {"kernel_release": plan["gpu"]["kernel_release"]}
    runtime.network_observed = {
        "cidr": plan["network"]["cidr"],
        "gateway": plan["network"]["gateway"],
        "dns": ["192.168.0.2", "192.168.0.3"],
    }
    if bad_binding == "cidr":
        runtime.network_observed["cidr"] = "192.168.1.0/24"
    elif bad_binding == "gateway":
        runtime.network_observed["gateway"] = "192.168.0.254"
    resolver = tmp_path / "resolv.conf"
    monkeypatch.setattr(boot, "Path", lambda path: resolver)
    monkeypatch.setattr(boot, "time", SimpleNamespace(time=lambda: 1000, monotonic=lambda: 1000))
    monkeypatch.setattr(boot, "metadata_instance", lambda: INSTANCE)
    if bad_binding is not None:
        category = "plan_policy" if bad_binding == "plan_dns" else "dhcp_plan_binding"
        with pytest.raises(boot.BootError, match=category):
            runtime.bootstrap("192.168.0.17")
        assert not resolver.exists()
    else:
        runtime.bootstrap("192.168.0.17")
        assert resolver.read_text() == "nameserver 8.8.8.8\nnameserver 8.8.4.4\n"
        assert runtime.checks["plan_policy"] is True


class BootstrapClock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def time(self):
        return 1000 + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.elapsed += delay


def test_pending_bootstrap_retries_identical_nonce_then_accepts_valid_reply(seed, plan, monkeypatch):
    clock = BootstrapClock()
    monkeypatch.setattr(boot, "time", clock)
    requests = []
    access = {"export": "172.30.0.11:6789:/volumes/native/readonly", "name": "native-client", "secret": "A" * 40 + "=="}
    reply = {
        "schema": boot.BOOTSTRAP_SCHEMA,
        "run_id": seed["run_id"],
        "nonce": seed["nonce"],
        "expires_unix": 5000,
        "plan": plan,
        "ceph_access": access,
    }

    class Client:
        def post(self, path, payload):
            requests.append((path, dict(payload)))
            if len(requests) < 3:
                raise boot.BootstrapPending(404)
            return reply

    client = Client()
    result = boot.wait_for_bootstrap(client, seed, SimpleNamespace(poll=lambda: None))
    assert boot.validate_bootstrap(result, seed) == (plan, access)
    assert requests == [("/v1/bootstrap/" + seed["run_id"], {"nonce": seed["nonce"]})] * 3
    assert clock.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("outer_offset,expected_elapsed", [(3, 3), (10000, 300)])
def test_pending_bootstrap_stops_at_outer_or_monotonic_deadline(seed, monkeypatch, outer_offset, expected_elapsed):
    clock = BootstrapClock()
    monkeypatch.setattr(boot, "time", clock)
    calls = []

    def pending(path, payload):
        calls.append((path, dict(payload)))
        raise boot.BootstrapPending(404)

    with pytest.raises(boot.BootError, match="bootstrap_wait_expired"):
        boot.wait_for_bootstrap(
            SimpleNamespace(post=pending), {**seed, "deadline": 1000 + outer_offset}, SimpleNamespace(poll=lambda: None)
        )
    assert clock.elapsed == expected_elapsed
    assert all(call == calls[0] for call in calls)
    assert all(0 < delay <= 5 for delay in clock.sleeps)


@pytest.mark.parametrize("status", [403, 409, 503])
def test_unauthorized_or_failed_service_response_is_never_retried(seed, monkeypatch, status):
    requests = []

    class Raw:
        def close(self):
            pass

    class TLS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def getpeercert(self, *, binary_form):
            return b"same-signed-worker-certificate"

        def sendall(self, payload):
            requests.append(payload)

    class Response:
        def __init__(self, stream):
            self.status = status

        def begin(self):
            pass

    def wrap(raw, *, server_hostname):
        assert server_hostname == seed["tls_server_name"]
        return TLS()

    client = object.__new__(boot.TLSClient)
    client.seed = dict(seed)
    client.context = SimpleNamespace(wrap_socket=wrap)
    client.peer_sha256 = None
    clock = BootstrapClock()
    monkeypatch.setattr(boot, "time", clock)
    monkeypatch.setattr(boot, "socket", SimpleNamespace(create_connection=lambda *args, **kwargs: Raw()))
    monkeypatch.setattr(boot, "http", SimpleNamespace(client=SimpleNamespace(HTTPResponse=Response)))
    with pytest.raises(boot.BootError, match="service_response"):
        boot.wait_for_bootstrap(client, seed, SimpleNamespace(poll=lambda: None))
    assert len(requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "error", [TimeoutError(), ConnectionResetError(), ConnectionRefusedError(), boot.ssl.SSLCertVerificationError()]
)
def test_bootstrap_transport_and_tls_errors_are_fatal(seed, monkeypatch, error):
    clock = BootstrapClock()
    calls = []

    def failed(path, payload):
        calls.append((path, payload))
        raise error

    monkeypatch.setattr(boot, "time", clock)
    with pytest.raises(type(error)):
        boot.wait_for_bootstrap(SimpleNamespace(post=failed), seed, SimpleNamespace(poll=lambda: None))
    assert len(calls) == 1 and clock.sleeps == []


def test_provider_denial_cannot_be_claimed_from_a_client_ro_mount(tmp_path, monkeypatch, seed):
    monkeypatch.setattr(boot, "INTERNAL", str(tmp_path / "internal"))
    monkeypatch.setattr(boot, "_run", lambda *args, **kwargs: "")
    access = {"export": "192.0.2.1:6789:/share", "name": "consumer", "secret": "test-only-secret"}
    monkeypatch.setattr(
        boot,
        "_mount_entry",
        lambda *args, **kwargs: {"source": access["export"], "super": ["name=consumer"], "options": ["ro"]},
    )
    runtime = boot.Runtime(seed)
    with pytest.raises(boot.BootError, match="provider_probe_mount_not_rw"):
        runtime.ceph(access)
    assert "provider_readonly_errno" not in runtime.checks
    assert access["secret"] == ""
    assert not (tmp_path / "internal" / "ceph-key").exists()


def published_projection():
    from palimpsest_local.image_manifest import manifest_digest, native_image_projection, normalize_manifest

    manifest = normalize_manifest(
        {
            "schema": "palimpsest.image-manifest.v1",
            "mode": "oci-root",
            "platform": "linux/amd64",
            "os_family": "ubuntu",
            "os_version": "22.04",
            "source": {"kind": "local"},
            "runtime": {
                "entrypoint": ["/bin/sh"],
                "cmd": ["-c", "true"],
                "env": ["EXACT=value"],
                "working_dir": "/",
                "user": "1000:1001",
            },
            "layers": [{"digest": digest("1"), "size_bytes": 128}, {"digest": digest("2"), "size_bytes": 256}],
        }
    )
    return native_image_projection(manifest), manifest_digest(manifest)


def test_published_native_projection_accepts_real_local_provenance_without_fabricated_oci():
    image, identity = published_projection()
    boot.validate_image_projection(image, identity)
    assert image["source"]["manifest"]["source"] == {"kind": "local"}
    assert "config_descriptor" not in image["source"]
    assert image["runtime"]["user"] == "1000:1001"


@pytest.mark.parametrize("change", ["runtime", "size", "order", "parent", "manifest", "identity"])
def test_published_native_projection_rejects_every_unbound_projection(change):
    image, identity = published_projection()
    if change == "runtime":
        image["runtime"] = {**image["runtime"], "env": ["EXACT=changed"]}
    elif change == "size":
        image["layers"][0]["size"] += 1
    elif change == "order":
        image["layers"].reverse()
    elif change == "parent":
        image["source"]["manifest"]["layers"][1]["parent_id"] = None
    elif change == "manifest":
        image["source"]["manifest"]["source"]["kind"] = "registry"
    else:
        identity = digest("f")
    with pytest.raises(boot.BootError):
        boot.validate_image_projection(image, identity)


def test_published_guest_validator_runs_without_any_package_import(tmp_path):
    import inspect

    image, identity = published_projection()
    runtime = tmp_path / "nova_ceph_boot.py"
    runtime.write_text(inspect.getsource(boot))
    program = (
        "import json,runpy,sys; policy=runpy.run_path(sys.argv[1]); "
        "image,identity=json.load(sys.stdin); policy['validate_image_projection'](image,identity)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(runtime)],
        input=json.dumps([image, identity]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("change", ["logical-id", "compatibility"])
def test_published_guest_recomputes_parent_aware_ids_even_for_rehashed_manifest(change):
    image, _ = published_projection()
    manifest = image["source"]["manifest"]
    if change == "logical-id":
        manifest["layers"][-1]["id"] = digest("e")
    else:
        manifest["os_version"] = "24.04"
    identity = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )
    image["source"]["manifest_digest"] = identity
    with pytest.raises(boot.BootError, match="published_layer_identity"):
        boot.validate_image_projection(image, identity)
