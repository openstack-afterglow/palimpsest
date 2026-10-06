"""Behavioral contract tests for scripts/run_openstack_system.py (no cloud access)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("run_openstack_system", ROOT / "scripts" / "run_openstack_system.py")
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

DOCKERFILE = b"FROM debian AS native-vm\nFROM native-vm AS native-cloud-vm\nRUN true\n"
EXPORTER = b"print('export')\n"
README = b"afterglow\n"


def _private(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    os.chmod(path, 0o600)
    return path


def _bundle(tmp_path: Path, *, extra: dict[str, bytes] | None = None, ref: str | None = None, unlisted: bool = False):
    members = {"README.md": README, "Dockerfile": DOCKERFILE, "scripts/export_native_cloud.py": EXPORTER}
    members.update(extra or {})
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    bundle = _private(tmp_path / "SOURCE.tar.gz", raw.getvalue())
    listed = {name: payload for name, payload in members.items() if not (unlisted and name in (extra or {}))}
    manifest = {
        "schema_version": 1,
        "afterglow_ref": ref or runner.APPROVED_AFTERGLOW_REF,
        "bundle_sha256": hashlib.sha256(raw.getvalue()).hexdigest(),
        "bundle_bytes": len(raw.getvalue()),
        "overlays": ["Dockerfile", "scripts/export_native_cloud.py"],
        "excluded": [],
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "origin": "native-overlay" if name in {"Dockerfile", "scripts/export_native_cloud.py"} else "commit",
            }
            for name, payload in sorted(listed.items())
        ],
    }
    _private(tmp_path / "SOURCE.tar.gz.manifest.json", json.dumps(manifest).encode())
    return bundle, manifest


def test_source_bundle_verifies_manifest_list_and_digests(tmp_path: Path) -> None:
    bundle, manifest = _bundle(tmp_path)
    verified = runner.verify_source_bundle(bundle, "ff007ed")
    assert verified.sha256 == manifest["bundle_sha256"]
    assert verified.manifest["afterglow_ref"] == runner.APPROVED_AFTERGLOW_REF


@pytest.mark.parametrize("ref", ["abc1234", "ff007e", "ff007edz"])
def test_source_bundle_refuses_unapproved_afterglow_ref_argument(tmp_path: Path, ref: str) -> None:
    bundle, _ = _bundle(tmp_path)
    with pytest.raises(runner.SystemProofError):
        runner.verify_source_bundle(bundle, ref)


def test_source_bundle_refuses_self_consistent_unknown_manifest_ref(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path, ref="0" * 40)
    with pytest.raises(runner.SystemProofError, match="approved full baseline"):
        runner.verify_source_bundle(bundle, "ff007ed")


def test_source_bundle_refuses_missing_manifest(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    (tmp_path / "SOURCE.tar.gz.manifest.json").unlink()
    with pytest.raises(runner.SystemProofError, match="manifest"):
        runner.verify_source_bundle(bundle, "ff007ed")


def test_source_bundle_refuses_excluded_and_unlisted_members(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path, extra={"backend/.env.local": b"SECRET=1\n"})
    with pytest.raises(runner.SystemProofError):
        runner.verify_source_bundle(bundle, "ff007ed")
    other = tmp_path / "other"
    other.mkdir()
    bundle, _ = _bundle(other, extra={"extra.txt": b"x"}, unlisted=True)
    with pytest.raises(runner.SystemProofError, match="unlisted"):
        runner.verify_source_bundle(bundle, "ff007ed")


def test_source_bundle_refuses_digest_mismatch(tmp_path: Path) -> None:
    bundle, manifest = _bundle(tmp_path)
    manifest["files"][0]["sha256"] = "0" * 64
    _private(tmp_path / "SOURCE.tar.gz.manifest.json", json.dumps(manifest).encode())
    with pytest.raises(runner.SystemProofError, match="digest mismatch"):
        runner.verify_source_bundle(bundle, "ff007ed")


def test_cleanup_only_tolerates_removed_bundle_but_not_missing_manifest(tmp_path: Path) -> None:
    bundle, manifest = _bundle(tmp_path)
    bundle.unlink()
    assert runner.verify_source_bundle(bundle, "ff007ed", cleanup_only=True).sha256 == manifest["bundle_sha256"]
    with pytest.raises(runner.SystemProofError):
        runner.verify_source_bundle(bundle, "ff007ed")


def _source(tmp_path: Path):
    bundle, _ = _bundle(tmp_path)
    return runner.verify_source_bundle(bundle, "ff007ed")


def test_receipt_binds_run_name_and_refuses_foreign_resume(tmp_path: Path) -> None:
    source = _source(tmp_path)
    path = tmp_path / "system-run.json"
    receipt = runner.Receipt.create(path, source)
    assert receipt.run_name == f"pal-system-{uuid.UUID(receipt.owner_id).hex[:8]}"
    assert os.stat(path).st_mode & 0o777 == 0o600
    loaded = runner.Receipt.load(path, source_ref=runner.APPROVED_AFTERGLOW_REF, bundle_sha256=source.sha256)
    assert set(loaded.data["resources"]) == set(runner.RESOURCE_KINDS)
    with pytest.raises(runner.SystemProofError, match="refusing to resume"):
        runner.Receipt.load(path, source_ref=runner.APPROVED_AFTERGLOW_REF, bundle_sha256="1" * 64)
    data = json.loads(path.read_text())
    data["run_name"] = "pal-system-00000000"
    _private(path, json.dumps(data).encode())
    with pytest.raises(runner.SystemProofError, match="run_name"):
        runner.Receipt.load(path, source_ref=runner.APPROVED_AFTERGLOW_REF, bundle_sha256=source.sha256)


def test_receipt_enforces_caps_names_and_inflight(tmp_path: Path) -> None:
    receipt = runner.Receipt.create(tmp_path / "system-run.json", _source(tmp_path))
    for role in ("builder-boot", "consumer-boot", "data"):
        receipt.begin("volumes", role)
        assert receipt.data["inflight"]["name"] == f"{receipt.run_name}-{role}"
        receipt.finish(
            "volumes",
            {
                "id": role,
                "name": receipt.name("volumes", role),
                "role": role,
                "size_gib": runner.VOLUME_SIZES_GIB[role],
            },
        )
    with pytest.raises(runner.SystemProofError, match="cap"):
        receipt.check_cap("volumes", "data")
    receipt.begin("servers", "builder")
    with pytest.raises(runner.SystemProofError, match="unresolved"):
        receipt.begin("servers", "consumer")
    data = json.loads(receipt.path.read_text())
    data["resources"]["servers"] = [{"id": "x", "name": "foreign-vm", "role": "builder"}]
    _private(receipt.path, json.dumps(data).encode())
    with pytest.raises(runner.SystemProofError, match="owner-bound name"):
        runner.Receipt.load(
            receipt.path,
            source_ref=runner.APPROVED_AFTERGLOW_REF,
            bundle_sha256=receipt.data["source"]["bundle_sha256"],
        )


def test_ownership_refuses_foreign_and_protected_objects(tmp_path: Path) -> None:
    receipt = runner.Receipt.create(tmp_path / "system-run.json", _source(tmp_path))
    receipt.user_id = "3" * 32
    fip_entry = {"id": "fip-1", "name": receipt.name("floating_ips", "builder"), "role": "builder"}
    fip = {
        "id": "fip-1",
        "floating_ip_address": "172.30.102.169",
        "description": receipt.description("builder"),
        "floating_network_id": runner.APPROVED_EXTERNAL_NETWORK_ID,
        "project_id": runner.APPROVED_PROJECT_ID,
    }
    assert runner.owned("floating_ips", fip_entry, fip, receipt) == "protected production floating IP"
    fip["floating_ip_address"] = "172.30.1.10"
    assert runner.owned("floating_ips", fip_entry, fip, receipt) is None
    port_entry = {"id": "p1", "name": receipt.name("ports", "builder"), "role": "builder"}
    port = {
        "id": "p1",
        "name": port_entry["name"],
        "description": "someone else",
        "project_id": runner.APPROVED_PROJECT_ID,
    }
    assert runner.owned("ports", port_entry, port, receipt) == "description owner mismatch"
    server_entry = {"id": "s1", "name": receipt.name("servers", "builder"), "role": "builder"}
    server = {"id": "s1", "name": server_entry["name"], "metadata": {}, "project_id": runner.APPROVED_PROJECT_ID}
    assert runner.owned("servers", server_entry, server, receipt) == "metadata owner mismatch"
    server["metadata"] = receipt.metadata("builder")
    assert runner.owned("servers", server_entry, server, receipt) is None
    server["project_id"] = "e4f1487023f047b59901f28a91876e7b"
    assert runner.owned("servers", server_entry, server, receipt) == "project mismatch"
    keypair_entry = {"id": receipt.name("keypairs", "key"), "name": receipt.name("keypairs", "key"), "role": "key"}
    keypair = {"name": keypair_entry["name"], "user_id": "4" * 32}
    assert runner.owned("keypairs", keypair_entry, keypair, receipt) == "keypair user mismatch"


def test_security_group_binding_is_filtered_client_side() -> None:
    ports = [
        {"id": "foreign", "security_group_ids": ["other"], "project_id": "a" * 32},
        {"id": "bound", "security_group_ids": ["sg-1", "other"], "project_id": runner.APPROVED_PROJECT_ID},
    ]
    assert runner.ports_bound_to_security_group(ports, "sg-1") == ["bound"]


def _host_key() -> str:
    raw = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
    return base64.b64encode(raw).decode()


def test_console_host_key_only_from_cloud_init_block_and_keyscan_must_match() -> None:
    key = _host_key()
    console = (
        f"ci-info: ssh-ed25519 {base64.b64encode(b'x' * 51).decode()} user-key\n"
        f"-----BEGIN SSH HOST KEY KEYS-----\nssh-ed25519 {key} root@builder\n-----END SSH HOST KEY KEYS-----\n"
    )
    assert runner.console_ed25519_host_key(console) == key
    assert runner.console_ed25519_host_key("no keys yet") is None
    other = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(32)).decode()
    conflicting = (
        console + f"-----BEGIN SSH HOST KEY KEYS-----\nssh-ed25519 {other} x\n-----END SSH HOST KEY KEYS-----\n"
    )
    with pytest.raises(runner.SystemProofError, match="conflicting"):
        runner.console_ed25519_host_key(conflicting)
    assert runner.verify_keyscan("172.30.1.10", f"172.30.1.10 ssh-ed25519 {key}\n", key) == (
        f"172.30.1.10 ssh-ed25519 {key}\n"
    )
    with pytest.raises(runner.SystemProofError, match="does not match"):
        runner.verify_keyscan("172.30.1.10", f"172.30.1.10 ssh-ed25519 {other}\n", key)


def test_sigv4_matches_aws_reference_vector() -> None:
    empty = hashlib.sha256(b"").hexdigest()
    header = runner.sigv4_authorization(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        canonical_uri="/test.txt",
        query={},
        headers={"range": "bytes=0-9", "x-amz-content-sha256": empty, "x-amz-date": "20130524T000000Z"},
        payload_sha256=empty,
        amz_date="20130524T000000Z",
        region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    assert header.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in header


@pytest.mark.parametrize(
    "url",
    [
        "http://keystone.example.org/v3",
        "https://10.0.0.5/v3",
        "https://keystone-internal.example.org/v3",
        "https://keystone.svc.cluster.local/v3",
        "https://user@keystone.example.org/v3",
    ],
)
def test_endpoint_validation_refuses_plaintext_internal_and_ip_endpoints(url: str) -> None:
    with pytest.raises(runner.SystemProofError):
        runner.validate_public_https_url(url, "endpoint")


def _profile(**overrides: object) -> dict[str, object]:
    profile: dict[str, object] = {
        "schema_version": 1,
        "auth_url": "https://keystone.example.org/v3",
        "auth_type": "v3applicationcredential",
        "application_credential_id": "a" * 32,
        "application_credential_secret": "very-secret-application-credential",
        "project_id": runner.APPROVED_PROJECT_ID,
        "project_name": "SYSTEM",
        "user_id": "3" * 32,
        "region_name": "RegionOne",
        "interface": "public",
        "verify": True,
        "s3_endpoint": "https://s3.example.org",
        "ceph_monitors": "172.30.2.101:6789,172.30.2.102:6789",
        "manila_share_type": "cephfs",
        "network_id": runner.APPROVED_NETWORK_ID,
        "volume_type": "ceph_hdd",
        "s3_access_key": "s3-access-key-value",
        "s3_secret_key": "s3-secret-key-value",
    }
    profile.update(overrides)
    return profile


def test_profile_accepts_member_profile_and_redacts_secrets(tmp_path: Path) -> None:
    path = _private(tmp_path / "profile.json", json.dumps(_profile()).encode())
    profile = runner.load_profile(path)
    assert profile.project_id == runner.APPROVED_PROJECT_ID
    assert "very-secret" not in repr(profile)
    assert "[REDACTED]" in runner.sanitize("auth failed for very-secret-application-credential")
    assert runner.sanitize("mount -o name=x,secret=AQBabc123==") == "mount -o name=x,secret=[REDACTED]"


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_id": "e4f1487023f047b59901f28a91876e7b"},
        {"verify": False},
        {"interface": "internal"},
        {"auth_type": "password"},
        {"password": "admin-password"},
        {"auth_url": "http://keystone.example.org/v3"},
        {"login_username": "admin", "login_password": "pw-value", "login_domain_name": "Default"},
        {"login_username": "tester"},
    ],
)
def test_profile_refuses_admin_wrong_project_and_tls_weakening(tmp_path: Path, overrides: dict[str, object]) -> None:
    path = _private(tmp_path / "profile.json", json.dumps(_profile(**overrides)).encode())
    with pytest.raises(runner.SystemProofError):
        runner.load_profile(path)


def test_profile_must_be_owner_only(tmp_path: Path) -> None:
    path = _private(tmp_path / "profile.json", json.dumps(_profile()).encode())
    os.chmod(path, 0o644)
    with pytest.raises(runner.SystemProofError, match="0600"):
        runner.load_profile(path)


def test_cli_refuses_secret_and_override_flags(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--os-password", "x"]) == 1
    assert runner.main(["--insecure"]) == 1
    assert "never accepted" in capsys.readouterr().err


def test_consumer_failures_require_member_only_native_service() -> None:
    probe = {
        "pid1": {"comm": "systemd", "exe": "/usr/lib/systemd/systemd"},
        "container": "none",
        "vm": "kvm",
        "dockerenv": False,
        "engine_binaries": [],
        "engine_processes": [],
        "unit": {"ActiveState": "active", "SubState": "running"},
        "supervisor": {"user": "appuser", "script": True},
        "children": [{"role": role, "user": "appuser"} for role in runner.EXPECTED_CHILD_ROLES],
        "health": {"api_8000": {"ok": True}, "frontend_3080": {"ok": True}},
        "listeners": {"8000": True, "3080": True, "6379_loopback_only": True, "3306": False},
        "backend_env": {"DEFAULT_NETWORK_ENABLED": "false"},
        "settings": {
            "default_network_enabled": False,
            "database_auto_create_tables": False,
            "os_interface": "public",
            "os_insecure": False,
            "ssl_verify": True,
            "os_auth_url_https": True,
            "admin_username_configured": False,
            "admin_password_configured": False,
        },
        "schema": {"tables": 12},
        "state": {"account_ready": True, "mode": "0o700", "owner": "appuser"},
    }
    assert runner.consumer_failures(probe) == []
    probe["backend_env"] = {"DEFAULT_NETWORK_ENABLED": None}
    probe["engine_binaries"] = ["docker"]
    probe["settings"]["admin_password_configured"] = True
    failures = runner.consumer_failures(probe)
    assert any("DEFAULT_NETWORK_ENABLED" in item for item in failures)
    assert any("container engine" in item for item in failures)
    assert any("admin_password_configured" in item for item in failures)
    probe["vm"] = "none"
    assert any("direct KVM" in item for item in runner.consumer_failures(probe))


def _approval(tmp_path: Path, source, **overrides: object) -> Path:
    approval = {
        "schema": runner.SOURCE_APPROVAL_SCHEMA,
        "afterglow_ref": runner.APPROVED_AFTERGLOW_REF,
        "bundle_sha256": source.sha256,
        "bundle_bytes": source.size,
        "manifest_sha256": source.manifest_sha256,
        "approved_at": "2026-10-06T00:00:00Z",
    }
    approval.update(overrides)
    return _private(tmp_path / "source-approval.json", json.dumps(approval).encode())


def test_source_requires_independent_parent_approval_receipt(tmp_path: Path) -> None:
    source = _source(tmp_path)
    approved = runner.verify_source_approval(_approval(tmp_path, source), source)
    assert len(approved.approval_sha256) == 64
    for overrides in (
        {"manifest_sha256": "0" * 64},
        {"bundle_sha256": "1" * 64},
        {"afterglow_ref": "0" * 40},
        {"schema": "self-approved"},
    ):
        with pytest.raises(runner.SystemProofError):
            runner.verify_source_approval(_approval(tmp_path, source, **overrides), source)
    with pytest.raises(runner.SystemProofError, match="separate receipt"):
        runner.verify_source_approval(Path(f"{source.path}.manifest.json"), source)


def test_single_active_binding_refuses_second_experiment_and_concurrent_process(tmp_path: Path) -> None:
    source = _source(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    binding = runner.SystemBinding(tmp_path / "binding")
    try:
        with pytest.raises(runner.SystemProofError, match="exclusive lock"):
            runner.SystemBinding(tmp_path / "binding")
        binding.bind(evidence=first, owner_id="owner-a", source=source)
        with pytest.raises(runner.SystemProofError, match="another SYSTEM experiment"):
            binding.check(evidence=second, owner_id=None, source=source)
        with pytest.raises(runner.SystemProofError, match="different owner_id"):
            binding.bind(evidence=first, owner_id="owner-b", source=source)
        with pytest.raises(runner.SystemProofError, match="another experiment"):
            binding.release(evidence=second, owner_id="owner-a")
        binding.release(evidence=first, owner_id="owner-a")
        binding.bind(evidence=second, owner_id="owner-b", source=source)
        assert binding.active()["owner_id"] == "owner-b"
    finally:
        binding.close()


def test_receipt_refuses_resume_with_different_manifest(tmp_path: Path) -> None:
    source = _source(tmp_path)
    receipt = runner.Receipt.create(tmp_path / "system-run.json", source)
    with pytest.raises(runner.SystemProofError, match="manifest"):
        runner.Receipt.load(
            receipt.path,
            source_ref=runner.APPROVED_AFTERGLOW_REF,
            bundle_sha256=source.sha256,
            manifest_sha256="2" * 64,
        )


def test_member_login_account_and_principal_are_mandatory(tmp_path: Path) -> None:
    path = _private(tmp_path / "profile.json", json.dumps(_profile()).encode())
    profile = runner.load_profile(path)
    with pytest.raises(runner.SystemProofError, match="login"):
        runner.require_login_account(profile)
    good = {
        "project_id": runner.APPROVED_PROJECT_ID,
        "user_id": profile.user_id,
        "roles": ["member", "reader"],
        "is_system_admin": False,
    }
    runner._require_member_principal(runner._principal_summary(good, profile), "login token")
    for overrides in (
        {"user_id": "9" * 32},
        {"project_id": "e4f1487023f047b59901f28a91876e7b"},
        {"roles": ["member", "admin"]},
        {"roles": []},
        {"is_system_admin": True},
    ):
        with pytest.raises(runner.SystemProofError):
            runner._require_member_principal(runner._principal_summary({**good, **overrides}, profile), "login")


@pytest.mark.parametrize("value", ["default", "sg-123", "", "0bf5daef-0589-498a-9028-eecc3deb3cc1/extra"])
def test_cli_refuses_non_uuid_user_security_group(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        runner.main(
            [
                "--profile",
                "unused-profile.json",
                "--evidence",
                "unused-evidence",
                "--source-bundle",
                "unused-source.tar.gz",
                "--source-approval",
                "unused-approval.json",
                "--afterglow-ref",
                "ff007ed",
                "--phase",
                "prepare",
                "--user-port-security-group",
                value,
            ]
        )
        == 1
    )
    assert "exact security group UUID" in capsys.readouterr().err


@pytest.mark.parametrize("group", [None, {"project_id": "9" * 32}, {"name": "default"}])
def test_user_security_group_requires_existing_project_identity(monkeypatch: pytest.MonkeyPatch, group) -> None:
    instance = object.__new__(runner.Runner)
    cloud = SimpleNamespace(
        conn=SimpleNamespace(network=SimpleNamespace(get_security_group=lambda _id: group)),
        get_or_none=lambda _action, function, *args: function(*args),
    )
    monkeypatch.setattr(instance, "connect", lambda: cloud)
    monkeypatch.setattr(runner, "USER_PORT_SECURITY_GROUPS", ("user-group",))
    with pytest.raises(runner.SystemProofError, match="missing, foreign or the owned group"):
        instance.expected_port_groups("owned-group")


def test_owned_security_group_cannot_be_approved_as_external(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = object.__new__(runner.Runner)
    cloud = SimpleNamespace(
        conn=SimpleNamespace(
            network=SimpleNamespace(get_security_group=lambda _id: {"project_id": runner.APPROVED_PROJECT_ID})
        ),
        get_or_none=lambda _action, function, *args: function(*args),
    )
    monkeypatch.setattr(instance, "connect", lambda: cloud)
    monkeypatch.setattr(runner, "USER_PORT_SECURITY_GROUPS", ("owned-group",))
    with pytest.raises(runner.SystemProofError, match="the owned group"):
        instance.expected_port_groups("owned-group")


def test_ingress_refuses_unapproved_extra_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = object.__new__(runner.Runner)
    receipt = runner.Receipt.create(tmp_path / "system-run.json", _source(tmp_path))
    receipt.begin("security_groups", "ssh")
    receipt.finish(
        "security_groups", {"id": "owned-group", "name": receipt.name("security_groups", "ssh"), "role": "ssh"}
    )
    receipt.begin("ports", "builder")
    receipt.finish("ports", {"id": "port-1", "name": receipt.name("ports", "builder"), "role": "builder"})
    instance.receipt = receipt
    port = {
        "id": "port-1",
        "name": receipt.name("ports", "builder"),
        "description": receipt.description("builder"),
        "project_id": runner.APPROVED_PROJECT_ID,
        "security_group_ids": ["owned-group", "extra-group"],
        "is_port_security_enabled": True,
    }
    cloud = SimpleNamespace(
        conn=SimpleNamespace(
            network=SimpleNamespace(
                get_port=lambda _id: port,
                get_security_group=lambda _id: {"project_id": runner.APPROVED_PROJECT_ID},
            )
        ),
        get_or_none=lambda _action, function, *args: function(*args),
    )
    monkeypatch.setattr(instance, "connect", lambda: cloud)
    monkeypatch.setattr(instance, "verify_security_group", lambda: None)
    monkeypatch.setattr(runner, "USER_PORT_SECURITY_GROUPS", ())
    with pytest.raises(runner.SystemProofError, match="differ from the owned/user-approved set"):
        instance.verify_ingress("builder")
    monkeypatch.setattr(runner, "USER_PORT_SECURITY_GROUPS", ("extra-group",))
    instance.verify_ingress("builder")
    for value in (False, None, "true", 1):
        port["is_port_security_enabled"] = value
        with pytest.raises(runner.SystemProofError, match="port security disabled"):
            instance.verify_ingress("builder")


@pytest.mark.parametrize("share_path", ["/v2", "/v2/", "/v2/" + runner.APPROVED_PROJECT_ID, "/v2/" + "9" * 32])
def test_manila_catalog_is_bound_to_authenticated_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    share_path: str,
) -> None:
    profile = runner.load_profile(_private(tmp_path / "profile.json", json.dumps(_profile()).encode()))
    access = SimpleNamespace(
        project_id=runner.APPROVED_PROJECT_ID, user_id=profile.user_id, role_names=["member", "reader"]
    )
    auth = SimpleNamespace(get_access=lambda _session: access)

    def endpoint(**kwargs):
        service = kwargs["service_type"]
        path = share_path if service == "sharev2" else "/v3/" + runner.APPROVED_PROJECT_ID
        return "https://" + service + ".example.org" + path

    session = SimpleNamespace(get_endpoint=endpoint)
    for name, module in {
        "keystoneauth1": SimpleNamespace(session=SimpleNamespace(Session=lambda **_kwargs: session)),
        "keystoneauth1.identity": SimpleNamespace(),
        "keystoneauth1.identity.v3": SimpleNamespace(ApplicationCredential=lambda **_kwargs: auth),
        "openstack": SimpleNamespace(),
        "openstack.connection": SimpleNamespace(Connection=lambda **_kwargs: SimpleNamespace()),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    if "9" * 32 in share_path:
        with pytest.raises(runner.SystemProofError, match="bound to a different project"):
            runner.Cloud(profile)
    else:
        cloud = runner.Cloud(profile)
        assert cloud.endpoints["sharev2"] == "https://sharev2.example.org/v2/" + runner.APPROVED_PROJECT_ID


def _cleanup_runner(tmp_path, monkeypatch):
    instance = object.__new__(runner.Runner)
    instance.receipt = runner.Receipt.create(tmp_path / "system-run.json", _source(tmp_path))
    instance.receipt.user_id = "3" * 32
    deleted = []
    api = SimpleNamespace(
        **{
            name: (lambda *args, **kwargs: None)
            for name in (
                "get_server",
                "delete_server",
                "get_volume",
                "delete_volume",
                "get_ip",
                "delete_ip",
                "get_port",
                "delete_port",
                "get_security_group",
                "delete_security_group",
                "get_keypair",
                "delete_keypair",
            )
        }
    )
    cloud = SimpleNamespace(
        conn=SimpleNamespace(compute=api, block_storage=api, network=api),
        get_or_none=lambda action, function, *args: function(*args),
        call=lambda action, function, *args, **kwargs: deleted.append(action),
    )
    monkeypatch.setattr(instance, "connect", lambda **kwargs: cloud)
    instance.cloud = cloud
    writes, releases = {}, []
    instance.evidence = SimpleNamespace(
        root=tmp_path, ceph_access_path=tmp_path / "ceph-key", write=lambda name, data: writes.update({name: data})
    )
    instance.binding = SimpleNamespace(release=lambda **kwargs: releases.append(kwargs))
    instance.deadline = SimpleNamespace(
        sleep=lambda *args: pytest.fail("unexpected cleanup wait"), remaining=lambda *args: 100
    )
    return instance, cloud, deleted, writes, releases


@pytest.mark.parametrize("status", [403, 500])
def test_bucket_discovery_and_cleanup_never_treat_errors_as_absence(tmp_path, monkeypatch, status):
    instance, _, _, _, releases = _cleanup_runner(tmp_path, monkeypatch)
    receipt = instance.receipt
    receipt.begin("buckets", "proof")
    instance.s3 = SimpleNamespace(request=lambda *args, **kwargs: (status, {}, b""))
    with pytest.raises(runner.SystemProofError, match=f"HTTP {status}"):
        instance.reconcile_inflight()
    assert receipt.data["inflight"] is not None
    assert receipt.data["cleanup"] is None
    assert releases == []
    receipt.finish(
        "buckets", {"id": receipt.name("buckets", "proof"), "name": receipt.name("buckets", "proof"), "role": "proof"}
    )
    with pytest.raises(runner.SystemProofError, match=f"HTTP {status}"):
        instance.phase_cleanup()
    assert receipt.data["cleanup"] is None
    assert releases == []


def test_replaced_same_name_keypair_is_never_adopted_or_deleted(tmp_path, monkeypatch):
    instance, cloud, deleted, writes, releases = _cleanup_runner(tmp_path, monkeypatch)
    receipt = instance.receipt
    public = "ssh-ed25519 " + _host_key()
    receipt.data["ssh_public_key_sha256"] = runner.ssh_public_key_digest(public)
    receipt.begin("keypairs", "key")
    entry = {"id": receipt.name("keypairs", "key"), "name": receipt.name("keypairs", "key"), "role": "key"}
    other = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(32)).decode()
    keypair = {"name": entry["name"], "user_id": receipt.user_id, "public_key": "ssh-ed25519 " + other}
    cloud.conn.compute.get_keypair = lambda name: keypair
    with pytest.raises(runner.SystemProofError, match="public key mismatch"):
        instance.reconcile_inflight()
    assert receipt.data["inflight"] is not None
    receipt.finish("keypairs", entry)
    with pytest.raises(runner.SystemProofError, match="cleanup refused"):
        instance.phase_cleanup()
    assert deleted == []
    assert writes["cleanup.json"]["verified"] is False
    assert receipt.data["cleanup"] is None
    assert releases == []
    keypair["public_key"] = public + " ignored-comment"
    assert runner.owned("keypairs", entry, keypair, receipt) is None


def test_keypair_digest_is_durable_before_ambiguous_create(tmp_path, monkeypatch):
    instance, cloud, _, _, _ = _cleanup_runner(tmp_path, monkeypatch)
    public = "ssh-ed25519 " + _host_key()
    cloud.conn.compute.create_keypair = lambda **kwargs: None

    def lose_response(action, function, **kwargs):
        data = json.loads(instance.receipt.path.read_text())
        assert data["ssh_public_key_sha256"] == runner.ssh_public_key_digest(public)
        assert data["inflight"]["kind"] == "keypairs"
        raise runner.SystemProofError("response lost")

    cloud.call = lose_response
    with pytest.raises(runner.SystemProofError, match="response lost"):
        instance.ensure_keypair(public)
    assert instance.receipt.data["inflight"] is not None


@pytest.mark.parametrize("access_type", ["cephx", "ip"])
def test_cleanup_denies_late_owned_cephx_rule_but_refuses_other_types(tmp_path, monkeypatch, access_type):
    instance, cloud, _, writes, releases = _cleanup_runner(tmp_path, monkeypatch)
    receipt = instance.receipt
    receipt.begin("shares", "artifacts")
    entry = receipt.finish(
        "shares", {"id": "share-1", "name": receipt.name("shares", "artifacts"), "role": "artifacts", "size_gib": 10}
    )
    share = {
        "id": entry["id"],
        "name": entry["name"],
        "metadata": receipt.metadata("artifacts"),
        "project_id": runner.APPROVED_PROJECT_ID,
    }
    rules = [{"id": "late-rule", "share_id": "share-1", "access_to": receipt.run_name, "access_type": access_type}]
    monkeypatch.setattr(instance, "_share", lambda _id: share)
    monkeypatch.setattr(instance, "_share_access_rules", lambda _id: rules[:])
    mutations = []

    def manila(method, path, **kwargs):
        nonlocal share
        mutations.append((method, path, kwargs))
        if method == "POST":
            assert kwargs["json_body"] == {"deny_access": {"access_id": "late-rule"}}
            rules.clear()
        if method == "DELETE":
            share = None

    cloud.manila = manila
    if access_type == "ip":
        with pytest.raises(runner.SystemProofError, match="cleanup refused"):
            instance.phase_cleanup()
        assert mutations == releases == []
        assert writes["cleanup.json"]["verified"] is False
    else:
        instance.phase_cleanup()
        assert [method for method, _, _ in mutations] == ["POST", "DELETE"]
        assert receipt.data["cleanup"]["verified"] is True
        assert len(releases) == 1


def test_share_access_intent_records_timestamp_before_ambiguous_request(tmp_path, monkeypatch):
    instance, cloud, _, _, _ = _cleanup_runner(tmp_path, monkeypatch)
    receipt = instance.receipt
    receipt.begin("shares", "artifacts")
    entry = receipt.finish(
        "shares", {"id": "share-1", "name": receipt.name("shares", "artifacts"), "role": "artifacts", "size_gib": 10}
    )
    share = {
        "id": entry["id"],
        "name": entry["name"],
        "metadata": receipt.metadata("artifacts"),
        "project_id": runner.APPROVED_PROJECT_ID,
        "size": 10,
        "share_proto": runner.SHARE_PROTO,
    }
    monkeypatch.setattr(instance, "create", lambda *args: entry)
    monkeypatch.setattr(instance, "wait_status", lambda *args, **kwargs: share)

    def lose_response(*args, **kwargs):
        marker = json.loads(receipt.path.read_text())["inflight"]
        assert marker["kind"] == "share_access"
        assert marker["name"] == receipt.run_name
        assert marker["started_at"]
        raise runner.SystemProofError("access response lost")

    cloud.manila = lose_response
    with pytest.raises(runner.SystemProofError, match="access response lost"):
        instance.ensure_share()


def test_consumer_fresh_install_receipt_survives_phase_resume_after_reboot(tmp_path, monkeypatch):
    instance, _, _, _, _ = _cleanup_runner(tmp_path, monkeypatch)
    first = {"boot_id": "first-boot", "boot_time": 1000, "state": {"installed_mtime": 1001}}
    assert instance.record_consumer_installation(first) is None
    saved = json.loads(instance.receipt.path.read_text())["consumer_first_boot"]
    assert saved == {"boot_id": "first-boot", "boot_time": 1000, "installed_mtime": 1001}
    resumed = {"boot_id": "second-boot", "boot_time": 2000, "state": {"installed_mtime": 1001}}
    assert instance.record_consumer_installation(resumed) is None
    assert instance.receipt.data["consumer_first_boot"] == saved
    resumed["state"]["installed_mtime"] = 2001
    assert "reinitialized" in instance.record_consumer_installation(resumed)


def test_consumer_stale_install_cannot_create_a_freshness_receipt(tmp_path, monkeypatch):
    instance, _, _, _, _ = _cleanup_runner(tmp_path, monkeypatch)
    assert "predates" in instance.record_consumer_installation(
        {"boot_id": "first", "boot_time": 1000, "state": {"installed_mtime": 500}}
    )
    assert "consumer_first_boot" not in instance.receipt.data


@pytest.mark.parametrize("ambiguous", ["inflight", "cleared_inflight"])
def test_cleanup_preserves_refused_bucket_binding_but_deletes_verified_server(tmp_path, monkeypatch, ambiguous):
    instance, cloud, deleted, writes, releases = _cleanup_runner(tmp_path, monkeypatch)
    receipt = instance.receipt
    receipt.begin("servers", "consumer")
    entry = receipt.finish(
        "servers", {"id": "owned-server", "name": receipt.name("servers", "consumer"), "role": "consumer"}
    )
    servers = {
        entry["id"]: {
            "id": entry["id"],
            "name": entry["name"],
            "project_id": runner.APPROVED_PROJECT_ID,
            "metadata": receipt.metadata("consumer"),
        }
    }
    cloud.conn.compute.get_server = lambda identifier: servers.get(identifier)
    cloud.conn.compute.delete_server = lambda identifier, **kwargs: servers.pop(identifier)
    cloud.call = lambda action, function, *args, **kwargs: (deleted.append(action), function(*args, **kwargs))
    receipt.begin("buckets", "proof")
    if ambiguous == "cleared_inflight":
        receipt.data["cleared_inflight"] = [dict(receipt.data["inflight"])]
        receipt.clear_inflight()
    instance.s3 = SimpleNamespace(request=lambda *args, **kwargs: (200, {}, b""), bucket_tags=lambda name: {})
    with pytest.raises(runner.SystemProofError, match="cleanup refused"):
        instance.phase_cleanup()
    assert deleted == ["deleting servers"]
    assert not servers
    assert writes["cleanup.json"]["verified"] is False
    assert writes["cleanup.json"]["refusals"]
    assert writes["cleanup.json"]["outcome"]["servers"][0]["result"] == "deleted"
    assert receipt.data["cleanup"] is None and releases == []
    if ambiguous == "inflight":
        assert receipt.data["inflight"] is not None


def test_successful_bucket_create_is_durable_before_owner_tag_request(tmp_path, monkeypatch):
    instance, _, _, _, _ = _cleanup_runner(tmp_path, monkeypatch)
    receipt = instance.receipt

    def lose_tag_response(name, tags):
        saved = json.loads(receipt.path.read_text())
        assert saved["inflight"] is None
        assert saved["resources"]["buckets"][0]["id"] == name
        assert tags["palimpsest-owner"] == receipt.owner_id
        raise runner.SystemProofError("tag request interrupted")

    instance.s3 = SimpleNamespace(
        request=lambda *args, **kwargs: (404, {}, b""),
        expect=lambda *args, **kwargs: None,
        put_bucket_tags=lose_tag_response,
    )
    with pytest.raises(runner.SystemProofError, match="tag request interrupted"):
        instance.rgw_round_trip()
    assert receipt.require("buckets", "proof")["objects"] == []
