"""Unit tests for palimpsest_local.runtime lifecycle, state ledgers, and KVM controls."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import palimpsest_local.kvm as kvm
import palimpsest_local.state as state
from palimpsest_local.errors import (
    ArtifactValidationError,
    LifecycleError,
    StateError,
)
from palimpsest_local.refs import ImageRef, LayerRef, RunSpec, StackRef
from palimpsest_local.runtime import (
    commit,
    create_and_validate_overlay,
    exec_command,
    inspect_run,
    logs,
    ps,
    receive_serial_builder_output,
    reconcile,
    rm,
    run,
    shell_command,
    start_serial_builder,
    stop,
)


class FakeDomain:
    def __init__(self, name: str, xml_content: str, run_id: str | None = None, uuid_str: str | None = None):
        self.name = name
        self.xml_content = xml_content
        self._active = False
        self._uuid = uuid_str or "00000000-0000-0000-0000-000000000001"
        self._run_id = run_id
        self.destroyed = False
        self.undefined = False
        self.shutdown_called = False

    def isActive(self) -> bool:
        return self._active

    def create(self) -> int:
        self._active = True
        return 0

    def shutdown(self) -> int:
        self.shutdown_called = True
        self._active = False
        return 0

    def destroy(self) -> int:
        self._active = False
        self.destroyed = True
        return 0

    def undefine(self) -> int:
        self.undefined = True
        return 0

    def UUIDString(self) -> str:
        return self._uuid

    def XMLDesc(self) -> str:
        return self.xml_content

    def interfaceAddresses(self, source: int = 1) -> dict[str, dict[str, list[dict[str, str]]]]:
        return {"vda": {"addrs": [{"addr": "192.168.122.100", "type": "0"}]}}


class FakeLibvirtConn:
    def __init__(self):
        self.domains: dict[str, FakeDomain] = {}

    def defineXML(self, xml_content: str) -> FakeDomain:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_content)
        name = root.findtext("name") or "unnamed"
        run_elem = root.find(f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run")
        run_id = run_elem.get("id") if run_elem is not None else None
        domain = FakeDomain(name, xml_content, run_id=run_id)
        self.domains[name] = domain
        return domain

    def lookupByName(self, name: str) -> FakeDomain:
        if name in self.domains and not self.domains[name].undefined:
            return self.domains[name]
        raise Exception(f"Domain not found: {name}")


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _serve_serial_frame(path: Path, header: dict[str, object], body: bytes = b"") -> threading.Thread:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.sendall(json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + body)
        finally:
            listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    return thread


def _short_socket_path(name: str = "builder.sock") -> tuple[tempfile.TemporaryDirectory[str], Path]:
    directory = tempfile.TemporaryDirectory(dir="/tmp")
    return directory, Path(directory.name) / name


def test_receive_serial_builder_output_verifies_framed_squashfs(tmp_path: Path):
    payload = b"hsqs" + b"\0" * 128
    digest = hashlib.sha256(payload).hexdigest()
    socket_directory, socket_path = _short_socket_path()
    receiver = _serve_serial_frame(
        socket_path, {"version": 1, "status": "ok", "size": len(payload), "sha256": digest}, payload
    )

    actual = receive_serial_builder_output(socket_path, tmp_path / "output.squashfs", connect_timeout_seconds=2)

    receiver.join(timeout=2)
    assert actual == f"sha256:{digest}"
    socket_directory.cleanup()
    assert (tmp_path / "output.squashfs").read_bytes() == payload


def test_receive_serial_builder_output_rejects_error_frame_and_deletes_partial(tmp_path: Path):
    socket_directory, socket_path = _short_socket_path()
    receiver = _serve_serial_frame(socket_path, {"version": 1, "status": "error", "stage": "run", "line": 7})

    with pytest.raises(LifecycleError, match="run at Palimpsestfile line 7"):
        receive_serial_builder_output(socket_path, tmp_path / "output.squashfs", connect_timeout_seconds=2)

    receiver.join(timeout=2)
    socket_directory.cleanup()
    assert not (tmp_path / "output.squashfs").exists()


def test_receive_serial_builder_output_rejects_truncated_or_mismatched_data(tmp_path: Path):
    socket_directory, socket_path = _short_socket_path("truncated.sock")
    receiver = _serve_serial_frame(socket_path, {"version": 1, "status": "ok", "size": 8, "sha256": "a" * 64}, b"hsqs")
    with pytest.raises(LifecycleError, match="truncated"):
        receive_serial_builder_output(socket_path, tmp_path / "truncated.squashfs", connect_timeout_seconds=2)
    receiver.join(timeout=2)
    socket_directory.cleanup()
    assert not (tmp_path / "truncated.squashfs").exists()
    socket_directory, socket_path = _short_socket_path("mismatch.sock")
    receiver = _serve_serial_frame(socket_path, {"version": 1, "status": "ok", "size": 4, "sha256": "a" * 64}, b"hsqs")
    with pytest.raises(LifecycleError, match="digest mismatch"):
        receive_serial_builder_output(socket_path, tmp_path / "mismatch.squashfs", connect_timeout_seconds=2)
    receiver.join(timeout=2)
    socket_directory.cleanup()
    assert not (tmp_path / "mismatch.squashfs").exists()


def test_start_serial_builder_writes_ledger_and_serial_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"base")
    spec = RunSpec(
        name="serial-builder",
        stack=StackRef(ImageRef(_sha256_file(base), "qcow2", "x86_64", None, base), ()),
        network="none",
    )
    conn = FakeLibvirtConn()
    monkeypatch.setattr(
        "palimpsest_local.runtime.create_and_validate_overlay", lambda _base, output: output.write_bytes(b"overlay")
    )
    monkeypatch.setattr("palimpsest_local.kvm.run_seed_iso", lambda seed, _user, _meta: seed.touch())

    readiness: list[bool] = []
    monkeypatch.setattr(
        "palimpsest_local.runtime._wait_for_readiness",
        lambda *_args, require_ip, **_kwargs: readiness.append(require_ip) or None,
    )
    result = start_serial_builder(spec, user_data="#cloud-config\n", roots=roots, conn=conn)

    assert result["status"] == "running"
    assert readiness == [False]
    rpaths = state.run_paths(roots, spec.name)
    assert state.read_run_state(rpaths)["status"] == "running"
    xml = conn.domains[spec.name].xml_content
    assert 'name="org.qemu.guest_agent.0"' not in xml
    assert 'source mode="bind"' in xml
    assert str(rpaths.root / "builder.sock") in xml
    assert 'name="org.afterglow.palimpsest.builder.v1"' in xml
    assert "<interface" not in xml


def test_no_libvirt_import_on_module_import():
    assert "libvirt" not in sys.modules, "importing runtime must not import libvirt"


def test_overlay_creation_and_validation():
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        base_file = tmppath / "base.qcow2"
        base_file.write_bytes(b"qcow2_base_data")
        base_digest = _sha256_file(base_file)

        base_ref = ImageRef(
            digest=base_digest,
            disk_format="qcow2",
            arch="x86_64",
            os_variant="ubuntu24.04",
            local_path=base_file,
        )
        overlay_file = tmppath / "overlay.qcow2"

        def fake_subprocess_run(argv, *args, **kwargs):
            if argv[0] == "qemu-img":
                if argv[1] == "create":
                    overlay_file.write_bytes(b"overlay_data")
                    return MagicMock(stdout="", stderr="", returncode=0)
                elif argv[1] == "info":
                    img_path = Path(argv[-1]).resolve()
                    if img_path == overlay_file.resolve():
                        info = {
                            "format": "qcow2",
                            "backing-filename": str(base_file.resolve()),
                            "backing-filename-format": "qcow2",
                        }
                    else:
                        info = {"format": "qcow2"}
                    return MagicMock(stdout=json.dumps(info), stderr="", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        with patch("palimpsest_local.runtime.subprocess.run", side_effect=fake_subprocess_run):
            create_and_validate_overlay(base_ref, overlay_file)
            assert overlay_file.exists()


def test_overlay_refuses_raw_squashfs():
    with tempfile.TemporaryDirectory() as tmp:
        tmppath = Path(tmp)
        base_file = tmppath / "base.raw"
        base_file.write_bytes(b"hsqs_squashfs_header")
        base_digest = _sha256_file(base_file)

        base_ref = ImageRef(
            digest=base_digest,
            disk_format="raw",
            arch="x86_64",
            os_variant=None,
            local_path=base_file,
        )
        overlay_file = tmppath / "overlay.qcow2"

        with pytest.raises(ArtifactValidationError, match="SquashFS"):
            create_and_validate_overlay(base_ref, overlay_file)


def test_run_status_transitions_and_completion():
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            "XDG_STATE_HOME": str(Path(tmp) / "state"),
        }
        roots = state.init_roots(env)

        base_file = roots.store / "base.qcow2"
        base_file.parent.mkdir(parents=True, exist_ok=True)
        base_file.write_bytes(b"base_data")
        base_digest = _sha256_file(base_file)

        layer_file = roots.store / "layer.squashfs"
        layer_file.write_bytes(b"layer_data")
        layer_digest = _sha256_file(layer_file)

        base_ref = ImageRef(base_digest, "qcow2", "x86_64", None, base_file)
        layer_ref = LayerRef(layer_digest, "application/vnd.afterglow.palimpsest.layer.squashfs.v1", layer_file)
        stack = StackRef(base_ref, (layer_ref,))
        spec = RunSpec(name="test-run", stack=stack)

        conn = FakeLibvirtConn()

        def fake_subprocess_run(argv, *args, **kwargs):
            if argv[0] == "qemu-img":
                if argv[1] == "create":
                    Path(argv[-1]).write_bytes(b"overlay")
                    return MagicMock(stdout="", stderr="", returncode=0)
                elif argv[1] == "info":
                    img_path = Path(argv[-1]).resolve()
                    if "overlay" in img_path.name:
                        info = {
                            "format": "qcow2",
                            "backing-filename": str(base_file.resolve()),
                            "backing-filename-format": "qcow2",
                        }
                    else:
                        info = {"format": "qcow2"}
                    return MagicMock(stdout=json.dumps(info), stderr="", returncode=0)
            elif argv[0] == "ssh-keygen":
                key_p = Path(argv[-1])
                key_p.write_text("private_key")
                key_p.with_name(key_p.name + ".pub").write_text("ssh-ed25519 AAAAFakePubKey user@host")
                return MagicMock(stdout="", stderr="", returncode=0)
            elif argv[0] == "cloud-localds":
                Path(argv[1]).touch()
                rpaths.console.write_text("PALIMPSEST_READY=1\n")
                return MagicMock(stdout="", stderr="", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        rpaths = state.run_paths(roots, "test-run")

        with patch("palimpsest_local.runtime.subprocess.run", side_effect=fake_subprocess_run):
            res = run(spec, roots=roots, conn=conn)

            assert res["status"] == "running"
            assert res["guest_ip"] == "192.168.122.100"

            r_owner = state.read_owner_record(rpaths)
            assert r_owner.name == "test-run"

            # Check ps output
            ps_runs = ps(roots=roots, conn=conn)
            assert len(ps_runs) == 1
            assert ps_runs[0]["name"] == "test-run"
            assert ps_runs[0]["status"] == "running"

            # Stop run
            stopped_res = stop("test-run", roots=roots, conn=conn)
            assert stopped_res["status"] == "stopped"

            # Stop again (idempotent)
            idempotent_res = stop("test-run", roots=roots, conn=conn)
            assert idempotent_res["status"] == "stopped"

            # Remove run (plain rm retains volume)
            rm_res = rm("test-run", roots=roots, conn=conn, volumes=False)
            assert rm_res["status"] == "removed"
            assert rpaths.root.exists()

            # Attempting run with retained name fails
            with pytest.raises(StateError, match="held by a removed run"):
                run(spec, roots=roots, conn=conn)

            # rm with volumes=True cleans directory
            rm_vol_res = rm("test-run", roots=roots, conn=conn, volumes=True)
            assert rm_vol_res["status"] == "removed"
            assert not rpaths.root.exists()


def test_start_rollback_on_failure():
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            "XDG_STATE_HOME": str(Path(tmp) / "state"),
        }
        roots = state.init_roots(env)

        base_file = roots.store / "base.qcow2"
        base_file.parent.mkdir(parents=True, exist_ok=True)
        base_file.write_bytes(b"base_data")
        base_digest = _sha256_file(base_file)

        base_ref = ImageRef(base_digest, "qcow2", "x86_64", None, base_file)
        stack = StackRef(base_ref, ())
        spec = RunSpec(name="rollback-run", stack=stack)

        conn = FakeLibvirtConn()

        def fake_subprocess_run(argv, *args, **kwargs):
            if argv[0] == "qemu-img":
                if argv[1] == "create":
                    Path(argv[-1]).write_bytes(b"overlay")
                    return MagicMock(stdout="", stderr="", returncode=0)
                elif argv[1] == "info":
                    img_p = Path(argv[-1])
                    if "overlay" in img_p.name:
                        return MagicMock(
                            stdout=json.dumps(
                                {
                                    "format": "qcow2",
                                    "backing-filename": str(base_file.resolve()),
                                    "backing-filename-format": "qcow2",
                                }
                            ),
                            stderr="",
                            returncode=0,
                        )
                    return MagicMock(stdout=json.dumps({"format": "qcow2"}), stderr="", returncode=0)
            elif argv[0] == "ssh-keygen":
                key_p = Path(argv[-1])
                key_p.write_text("private_key")
                key_p.with_name(key_p.name + ".pub").write_text("ssh-ed25519 AAAAFakePubKey user@host")
                return MagicMock(stdout="", stderr="", returncode=0)
            elif argv[0] == "cloud-localds":
                Path(argv[1]).touch()
                return MagicMock(stdout="", stderr="", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        def failing_wait(rpaths_in, domain, timeout_seconds, require_ip=True):
            raise LifecycleError("readiness failed")

        with (
            patch("palimpsest_local.runtime.subprocess.run", side_effect=fake_subprocess_run),
            patch("palimpsest_local.runtime._wait_for_readiness", side_effect=failing_wait),
        ):
            with pytest.raises(LifecycleError, match="readiness failed"):
                run(spec, roots=roots, conn=conn)

            rpaths = state.run_paths(roots, "rollback-run")
            st = state.read_run_state(rpaths)
            assert st["status"] == "failed"
            assert "rollback-run" not in conn.domains or conn.domains["rollback-run"].undefined


def test_foreign_marker_refusal():
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            "XDG_STATE_HOME": str(Path(tmp) / "state"),
        }
        roots = state.init_roots(env)
        rpaths = state.run_paths(roots, "foreign-run")
        rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)

        state.write_owner_record(rpaths)
        state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.50"})

        conn = FakeLibvirtConn()
        foreign_xml = f"""<domain type='kvm'>
          <name>foreign-run</name>
          <metadata>
            <palimpsest:run id="different-uuid-1234" schema="1" version="0.1.0" xmlns:palimpsest="{kvm.DOMAIN_MARKER_NAMESPACE}"/>
          </metadata>
        </domain>"""
        domain = conn.defineXML(foreign_xml)
        domain.create()

        with pytest.raises(LifecycleError, match="foreign"):
            stop("foreign-run", roots=roots, conn=conn)
        assert domain.isActive()

        with pytest.raises(LifecycleError, match="foreign"):
            rm("foreign-run", roots=roots, conn=conn)
        assert domain.isActive()

        runs, warnings = reconcile(roots=roots, conn=conn)
        assert any("not owned by run ID" in w for w in warnings)


def test_run_refuses_existing_libvirt_domain():
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            "XDG_STATE_HOME": str(Path(tmp) / "state"),
        }
        roots = state.init_roots(env)

        base_file = roots.store / "base.qcow2"
        base_file.parent.mkdir(parents=True, exist_ok=True)
        base_file.write_bytes(b"base_data")
        base_digest = _sha256_file(base_file)

        base_ref = ImageRef(base_digest, "qcow2", "x86_64", None, base_file)
        stack = StackRef(base_ref, ())
        spec = RunSpec(name="existing-dom", stack=stack)

        conn = FakeLibvirtConn()
        foreign_xml = "<domain type='kvm'><name>existing-dom</name></domain>"
        conn.defineXML(foreign_xml)

        with pytest.raises(LifecycleError, match="already exists in libvirt"):
            run(spec, roots=roots, conn=conn)

        assert conn.domains["existing-dom"].xml_content == foreign_xml


def test_missing_domain_reconciliation():
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            "XDG_STATE_HOME": str(Path(tmp) / "state"),
        }
        roots = state.init_roots(env)
        rpaths = state.run_paths(roots, "missing-run")
        rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)

        state.write_owner_record(rpaths)
        state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.60"})

        conn = FakeLibvirtConn()

        runs, warnings = reconcile(roots=roots, conn=conn)
        assert any("domain missing from libvirt" in w for w in warnings)
        st = state.read_run_state(rpaths)
        assert st["status"] == "stopped"


def test_lima_run_reconciliation_skips_libvirt_state_changes():
    with tempfile.TemporaryDirectory() as tmp:
        roots = state.init_roots(
            {
                "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
                "XDG_STATE_HOME": str(Path(tmp) / "state"),
            }
        )
        rpaths = state.run_paths(roots, "lima-run")
        rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.write_owner_record(rpaths)
        state.write_run_state(rpaths, status="running", data={"backend": "lima-vz", "guest_ip": "192.168.5.15"})

        runs, warnings = reconcile(roots=roots, conn=FakeLibvirtConn())

        assert warnings == []
        assert runs[0]["status"] == "running"
        assert state.read_run_state(rpaths)["status"] == "running"


def test_network_omission():
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            "XDG_STATE_HOME": str(Path(tmp) / "state"),
        }
        roots = state.init_roots(env)

        base_file = roots.store / "base.qcow2"
        base_file.parent.mkdir(parents=True, exist_ok=True)
        base_file.write_bytes(b"base_data")
        base_digest = _sha256_file(base_file)

        base_ref = ImageRef(base_digest, "qcow2", "x86_64", None, base_file)
        stack = StackRef(base_ref, ())
        spec = RunSpec(name="no-net-run", stack=stack, network="none")

        conn = FakeLibvirtConn()
        rpaths = state.run_paths(roots, "no-net-run")

        def fake_subprocess_run(argv, *args, **kwargs):
            if argv[0] == "qemu-img":
                if argv[1] == "create":
                    Path(argv[-1]).write_bytes(b"overlay")
                    return MagicMock(stdout="", stderr="", returncode=0)
                elif argv[1] == "info":
                    img_p = Path(argv[-1])
                    if "overlay" in img_p.name:
                        return MagicMock(
                            stdout=json.dumps(
                                {
                                    "format": "qcow2",
                                    "backing-filename": str(base_file.resolve()),
                                    "backing-filename-format": "qcow2",
                                }
                            ),
                            stderr="",
                            returncode=0,
                        )
                    return MagicMock(stdout=json.dumps({"format": "qcow2"}), stderr="", returncode=0)
            elif argv[0] == "ssh-keygen":
                key_p = Path(argv[-1])
                key_p.write_text("private_key")
                key_p.with_name(key_p.name + ".pub").write_text("ssh-ed25519 AAAAFakePubKey user@host")
                return MagicMock(stdout="", stderr="", returncode=0)
            elif argv[0] == "cloud-localds":
                Path(argv[1]).touch()
                rpaths.console.write_text("PALIMPSEST_READY=1\n")
                return MagicMock(stdout="", stderr="", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        with patch("palimpsest_local.runtime.subprocess.run", side_effect=fake_subprocess_run):
            res = run(spec, roots=roots, conn=conn)
            assert res["status"] == "running"
            assert res["guest_ip"] is None
            domain = conn.lookupByName("no-net-run")
            assert "<interface" not in domain.XMLDesc()


def test_logs_and_commands():
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            "XDG_STATE_HOME": str(Path(tmp) / "state"),
        }
        roots = state.init_roots(env)
        rpaths = state.run_paths(roots, "cmd-run")
        rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)

        state.write_owner_record(rpaths)
        state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.70"})
        rpaths.console.write_text("line 1\nline 2\n")

        log_lines = list(logs("cmd-run", roots=roots, follow=False))
        assert log_lines == ["line 1\n", "line 2\n"]

        # Test follow mode exits on stopped
        state.write_run_state(rpaths, status="stopped", data={"guest_ip": "192.168.122.70"})
        follow_lines = list(logs("cmd-run", roots=roots, follow=True, poll_interval=0.01))
        assert follow_lines == ["line 1\n", "line 2\n"]

        state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.70"})

        insp = inspect_run("cmd-run", roots=roots)
        assert insp["owner"]["name"] == "cmd-run"
        assert insp["state"]["status"] == "running"

        sh_cmd = shell_command("cmd-run", roots=roots)
        assert any("192.168.122.70" in arg for arg in sh_cmd)
        assert "ssh" in sh_cmd[0]

        ex_cmd = exec_command("cmd-run", ["ls", "-la"], roots=roots)
        assert any("192.168.122.70" in arg for arg in ex_cmd)
        assert any("palimpsest-exec" in arg or "ls" in arg for arg in ex_cmd)


def parse_runner_cmd(cmd: list[str]) -> list[str]:
    if not cmd or cmd[0] == "scp":
        return cmd
    try:
        payload = cmd[-1]
        pad = "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode((payload + pad).encode("ascii")).decode("utf-8"))
    except Exception:
        return cmd


def get_cmd_name(argv: list[str]) -> str:
    if not argv:
        return ""
    for candidate in ("fuser", "findmnt", "stat", "mksquashfs", "chroot"):
        if candidate in argv:
            return candidate
    if argv[0] == "sudo":
        for tok in argv[1:]:
            if not tok.startswith("-"):
                return tok
    return argv[0]


def test_commit_success(tmp_path: Path):
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    roots = state.init_roots(env)
    rpaths = state.run_paths(roots, "demo-run")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.ssh.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.identity.write_text("fake key")
    rpaths.known_hosts.write_text("fake host")

    base_digest = "sha256:" + "a" * 64
    layer_digest = "sha256:" + "b" * 64
    owner_rec = state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths,
        status="running",
        data={
            "guest_ip": "192.168.122.50",
            "base_digest": base_digest,
            "layers": [{"digest": layer_digest}],
        },
    )
    conn = FakeLibvirtConn()
    dom = conn.defineXML(
        f'<domain><name>demo-run</name><metadata><palimpsest:run xmlns:palimpsest="https://afterglow.dev/palimpsest-local/domain/v1" id="{owner_rec.run_id}" schema="1" version="0.1.0"/></metadata></domain>'
    )
    dom._active = True

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "scp":
            dst = Path(cmd[-1])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"hsqs" + b"\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        argv = parse_runner_cmd(cmd)
        cmd_name = get_cmd_name(argv)
        if cmd_name == "fuser":
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd_name == "findmnt":
            return subprocess.CompletedProcess(cmd, 0, "ext4\n", "")
        if cmd_name == "stat":
            return subprocess.CompletedProcess(cmd, 0, "2049\n", "")
        if cmd_name == "mksquashfs":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    res = commit("demo-run", "committed-tag", roots=roots, conn=conn, runner=fake_runner)
    assert res["tag"] == "committed-tag"
    assert res["parent_digest"] == layer_digest
    assert res["base_image_digest"] == base_digest
    assert res["source"] == "commit"

    tag_rec = state.read_tag_record(roots, "committed-tag")
    assert tag_rec.digest == res["digest"]
    assert tag_rec.source == "commit"


def test_commit_refuses_non_running_or_unowned_state(tmp_path: Path):
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    roots = state.init_roots(env)
    rpaths = state.run_paths(roots, "stopped-run")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="stopped", data={"guest_ip": "192.168.122.50"})

    with pytest.raises(LifecycleError, match="is not running"):
        commit("stopped-run", "my-tag", roots=roots)

    with pytest.raises(StateError):
        commit("non-existent-run", "my-tag", roots=roots)


def test_commit_refuses_busy_merged_tree(tmp_path: Path):
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    roots = state.init_roots(env)
    rpaths = state.run_paths(roots, "busy-run")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.ssh.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.identity.write_text("key")
    rpaths.known_hosts.write_text("host")
    owner_rec = state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths, status="running", data={"guest_ip": "192.168.122.50", "base_digest": "sha256:" + "a" * 64}
    )

    conn = FakeLibvirtConn()
    dom = conn.defineXML(
        f'<domain><name>busy-run</name><metadata><palimpsest:run xmlns:palimpsest="https://afterglow.dev/palimpsest-local/domain/v1" id="{owner_rec.run_id}" schema="1" version="0.1.0"/></metadata></domain>'
    )
    dom._active = True

    def fake_runner_busy(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        argv = parse_runner_cmd(cmd)
        cmd_name = get_cmd_name(argv)
        if cmd_name == "fuser":
            return subprocess.CompletedProcess(cmd, 0, "1234\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with pytest.raises(LifecycleError, match="processes are using /opt/layers/merged"):
        commit("busy-run", "busy-tag", roots=roots, conn=conn, runner=fake_runner_busy)


def test_commit_tag_conflict(tmp_path: Path):
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    roots = state.init_roots(env)
    rpaths = state.run_paths(roots, "conflict-run")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.ssh.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.identity.write_text("key")
    rpaths.known_hosts.write_text("host")
    base_digest = "sha256:" + "a" * 64
    owner_rec = state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.50", "base_digest": base_digest})

    conn = FakeLibvirtConn()
    dom = conn.defineXML(
        f'<domain><name>conflict-run</name><metadata><palimpsest:run xmlns:palimpsest="https://afterglow.dev/palimpsest-local/domain/v1" id="{owner_rec.run_id}" schema="1" version="0.1.0"/></metadata></domain>'
    )
    dom._active = True

    existing_rec = state.TagRecord(
        schema_version=1,
        tag="conflict-commit",
        digest="sha256:" + "f" * 64,
        media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
        size_bytes=100,
        parent_digest=None,
        base_image_digest=base_digest,
        source="commit",
        created_at=state.utc_now_iso(),
    )
    state.write_tag_record(roots, existing_rec)

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "scp":
            dst = Path(cmd[-1])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"hsqs" + b"\x00" * 100)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        argv = parse_runner_cmd(cmd)
        cmd_name = get_cmd_name(argv)
        if cmd_name == "fuser":
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd_name == "findmnt":
            return subprocess.CompletedProcess(cmd, 0, "ext4\n", "")
        if cmd_name == "stat":
            return subprocess.CompletedProcess(cmd, 0, "2049\n", "")
        if cmd_name == "mksquashfs":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with pytest.raises(LifecycleError, match="conflicting with committed digest"):
        commit("conflict-run", "conflict-commit", roots=roots, conn=conn, runner=fake_runner)
