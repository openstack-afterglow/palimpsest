"""Unit tests for the cloud VM lifecycle, state ledgers, and KVM controls."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import palimpsest_local.cloud_runtime as runtime
import palimpsest_local.kvm as kvm
import palimpsest_local.platforms as platforms
import palimpsest_local.state as state
from palimpsest_local.cloud_runtime import (
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
    start,
    start_serial_builder,
    stop,
)
from palimpsest_local.errors import (
    ArtifactValidationError,
    LifecycleError,
    StateError,
)
from palimpsest_local.refs import ImageRef, LayerRef, PortForward, RunSpec, StackRef
from palimpsest_local.runtime_types import ExecRequest


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
        raise KeyError(f"Domain not found: {name}")


def _legacy_cloud_lifecycle(
    roots: state.StatePaths,
    name: str,
    status: str,
) -> tuple[state.RunPaths, state.OwnerRecord, FakeLibvirtConn, FakeDomain]:
    rpaths = state.run_paths(roots, name)
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.ssh.mkdir(mode=0o700)
    rpaths.console.write_text("old boot\n", encoding="utf-8")
    rpaths.console.chmod(0o600)
    owner = state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths,
        status=status,
        data={"backend": "kvm", "network": "none", "guest_ip": None, "layers": [], "volumes": []},
    )
    marker = (
        f'<palimpsest:run xmlns:palimpsest="{kvm.DOMAIN_MARKER_NAMESPACE}" id="{owner.run_id}" '
        f'schema="1" version="{kvm.DOMAIN_MARKER_VERSION}"/>'
    )
    domain = FakeDomain(name, f"<domain><name>{name}</name><metadata>{marker}</metadata></domain>")
    conn = FakeLibvirtConn()
    conn.domains[name] = domain
    return rpaths, owner, conn, domain


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
        "palimpsest_local.cloud_runtime.create_and_validate_overlay",
        lambda _base, output: output.write_bytes(b"overlay"),
    )
    monkeypatch.setattr("palimpsest_local.kvm.run_seed_iso", lambda seed, _user, _meta: seed.touch())

    readiness: list[bool] = []
    monkeypatch.setattr(
        "palimpsest_local.cloud_runtime._wait_for_readiness",
        lambda *_args, require_ip, **_kwargs: readiness.append(require_ip) or None,
    )
    result = start_serial_builder(spec, user_data="#cloud-config\n", roots=roots, conn=conn)

    assert result["status"] == "running"
    assert readiness == [False]
    rpaths = state.run_paths(roots, spec.name)
    owner = state.read_owner_record(rpaths)
    assert state.read_run_state(rpaths) == result
    assert {key: result[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "kvm",
        "name": spec.name,
        "run_id": owner.run_id,
    }
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

        with patch("palimpsest_local.cloud_runtime.subprocess.run", side_effect=fake_subprocess_run):
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
                Path(argv[1]).with_name("console.log").write_text("PALIMPSEST_READY=1\n")
                return MagicMock(stdout="", stderr="", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        rpaths = state.run_paths(roots, "test-run")

        with patch("palimpsest_local.cloud_runtime.subprocess.run", side_effect=fake_subprocess_run):
            res = run(spec, roots=roots, conn=conn)

            assert res["status"] == "running"
            assert res["guest_ip"] == "192.168.122.100"
            assert res["layer_attachment"] == {
                "delivery": "direct-block",
                "device": "virtio-blk",
                "mount": "squashfs-ro",
            }

            r_owner = state.read_owner_record(rpaths)
            assert r_owner.name == "test-run"
            assert res["name"] == spec.name
            assert res["run_id"] == r_owner.run_id
            assert res["backend"] == "kvm"
            assert res["schema_version"] == 2
            assert res["runtime_kind"] == "cloud-image"

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

            def restarted_wait(rpaths_in, domain, timeout_seconds, require_ip=True):
                assert rpaths_in.console.read_text(encoding="utf-8") == ""
                rpaths_in.console.write_text("PALIMPSEST_READY=1\n", encoding="utf-8")
                return "192.168.122.100"

            with patch("palimpsest_local.cloud_runtime._wait_for_readiness", side_effect=restarted_wait):
                restarted_res = start("test-run", roots=roots, conn=conn)
            assert restarted_res["status"] == "running"
            assert restarted_res["guest_ip"] == "192.168.122.100"
            stop("test-run", roots=roots, conn=conn)

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
            patch("palimpsest_local.cloud_runtime.subprocess.run", side_effect=fake_subprocess_run),
            patch("palimpsest_local.cloud_runtime._wait_for_readiness", side_effect=failing_wait),
        ):
            with pytest.raises(LifecycleError, match="readiness failed"):
                run(spec, roots=roots, conn=conn)

            rpaths = state.run_paths(roots, "rollback-run")
            st = state.read_run_state(rpaths)
            owner = state.read_owner_record(rpaths)
            assert {
                key: st[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")
            } == {
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": "kvm",
                "name": spec.name,
                "run_id": owner.run_id,
                "status": "failed",
            }
            assert "rollback-run" not in conn.domains or conn.domains["rollback-run"].undefined


def test_legacy_cloud_start_promotes_once_after_backend_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, owner, conn, domain = _legacy_cloud_lifecycle(roots, "legacy-start", "stopped")
    before = rpaths.state.read_bytes()
    monkeypatch.setattr(runtime, "_wait_for_readiness", lambda *_a, **_k: None)

    result = start("legacy-start", roots=roots, conn=conn)

    assert domain.isActive() is True
    assert rpaths.state.read_bytes() != before
    assert {key: result[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "kvm",
        "name": "legacy-start",
        "run_id": owner.run_id,
        "status": "running",
    }


def test_legacy_cloud_start_backend_failure_preserves_exact_state_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, _owner, conn, _domain = _legacy_cloud_lifecycle(roots, "legacy-start-failure", "stopped")
    before = rpaths.state.read_bytes()
    monkeypatch.setattr(
        runtime, "_wait_for_readiness", lambda *_a, **_k: (_ for _ in ()).throw(LifecycleError("boot failed"))
    )

    with pytest.raises(LifecycleError, match="boot failed"):
        start("legacy-start-failure", roots=roots, conn=conn)

    assert rpaths.state.read_bytes() == before


def test_legacy_cloud_stop_success_promotes_but_failure_preserves_bytes(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    success_paths, success_owner, success_conn, success_domain = _legacy_cloud_lifecycle(
        roots, "legacy-stop", "running"
    )
    success_domain._active = True

    stopped = stop("legacy-stop", roots=roots, conn=success_conn, timeout_seconds=0)

    assert stopped["schema_version"] == 2
    assert stopped["run_id"] == success_owner.run_id
    assert stopped["status"] == "stopped"

    failure_paths, _owner, failure_conn, failure_domain = _legacy_cloud_lifecycle(
        roots, "legacy-stop-failure", "running"
    )
    failure_domain._active = True
    failure_domain.shutdown = lambda: None  # type: ignore[method-assign]
    failure_domain.destroy = lambda: (_ for _ in ()).throw(LifecycleError("destroy failed"))  # type: ignore[method-assign]
    before = failure_paths.state.read_bytes()

    with pytest.raises(LifecycleError, match="destroy failed"):
        stop("legacy-stop-failure", roots=roots, conn=failure_conn, timeout_seconds=0)

    assert failure_paths.state.read_bytes() == before


@pytest.mark.parametrize("domain_present", [True, False])
def test_legacy_cloud_stop_live_success_promotes_only_at_terminal_write(
    tmp_path: Path,
    domain_present: bool,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    suffix = "present" if domain_present else "missing"
    rpaths, _owner, conn, domain = _legacy_cloud_lifecycle(roots, f"legacy-stop-noop-{suffix}", "running")
    domain._active = False
    if not domain_present:
        conn.domains.clear()
    stopped = stop(rpaths.name, roots=roots, conn=conn)

    assert stopped["status"] == "stopped"
    assert stopped["schema_version"] == 2
    assert stopped["lifecycle_revision"] == 1
    assert state.read_run_state(rpaths) == stopped


@pytest.mark.parametrize("source_status", ["creating", "failed"])
def test_cloud_stop_preserves_legacy_source_compatibility(
    tmp_path: Path,
    source_status: str,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, _owner, conn, _domain = _legacy_cloud_lifecycle(
        roots,
        f"legacy-stop-{source_status}",
        source_status,
    )

    stopped = stop(rpaths.name, roots=roots, conn=conn, timeout_seconds=0)

    assert stopped["status"] == "stopped"
    assert stopped["schema_version"] == 2
    assert stopped["lifecycle_revision"] == 1


def test_legacy_cloud_plain_rm_promotes_removed_and_volumes_rm_rejects_replacement(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    plain_paths, plain_owner, plain_conn, _plain_domain = _legacy_cloud_lifecycle(roots, "legacy-rm", "stopped")
    plain_conn.domains.clear()

    removed = rm("legacy-rm", roots=roots, conn=plain_conn)

    assert removed["schema_version"] == 2
    assert removed["run_id"] == plain_owner.run_id
    assert removed["status"] == "removed"
    assert plain_paths.root.exists()

    swap_paths, _owner, swap_conn, swap_domain = _legacy_cloud_lifecycle(roots, "legacy-rm-swap", "stopped")
    displaced = roots.runs / "legacy-rm-swap-original"
    original_state = swap_paths.state.read_bytes()

    def swap_on_undefine() -> int:
        os.rename(swap_paths.root, displaced)
        swap_paths.root.mkdir()
        (swap_paths.root / "marker").write_bytes(b"replacement")
        swap_domain.undefined = True
        return 0

    swap_domain.undefine = swap_on_undefine  # type: ignore[method-assign]

    with pytest.raises(StateError, match="changed during lifecycle"):
        rm("legacy-rm-swap", roots=roots, conn=swap_conn, volumes=True)

    assert (swap_paths.root / "marker").read_bytes() == b"replacement"
    assert (displaced / "state.json").read_bytes() == original_state


def test_legacy_cloud_plain_rm_cleans_owned_domain_without_rewriting_removed_state(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths, _owner, conn, domain = _legacy_cloud_lifecycle(roots, "legacy-rm-noop", "removed")
    domain._active = True
    before = rpaths.state.read_bytes()

    removed = rm("legacy-rm-noop", roots=roots, conn=conn)

    assert removed["status"] == "removed"
    assert removed.get("schema_version") is None
    assert domain.destroyed is True
    assert domain.undefined is True
    assert rpaths.state.read_bytes() == before


@pytest.mark.parametrize(
    ("backend", "arch"),
    [(platforms.BACKEND_KVM, "x86_64"), (platforms.BACKEND_HVF, "aarch64")],
)
def test_new_cloud_backend_failure_holds_exact_v2_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    arch: str,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    base = tmp_path / f"base-{backend}.qcow2"
    base.write_bytes(b"base")
    spec = RunSpec(
        name=f"failed-{arch.replace('_', '-')}",
        stack=StackRef(ImageRef(_sha256_file(base), "qcow2", arch, None, base), ()),
    )
    profile = (
        platforms.resolve_domain_profile(platforms.BACKEND_KVM, arch)
        if backend == platforms.BACKEND_KVM
        else _hvf_test_profile(tmp_path)
    )
    overlay_calls = 0

    def fail_overlay(_base: ImageRef, _output: Path) -> None:
        nonlocal overlay_calls
        overlay_calls += 1
        rpaths = state.run_paths(roots, spec.name)
        assert _output.parent != rpaths.root
        assert _output.parent.parent == roots.state
        owner = state.read_owner_record(rpaths)
        creating = state.read_run_state(rpaths)
        assert {
            key: creating[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")
        } == {
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": backend,
            "name": spec.name,
            "run_id": owner.run_id,
            "status": "creating",
        }
        raise LifecycleError("backend exploded")

    monkeypatch.setattr(runtime, "create_and_validate_overlay", fail_overlay)

    with pytest.raises(LifecycleError, match="backend exploded"):
        run(spec, roots=roots, conn=FakeLibvirtConn(), profile=profile)

    rpaths = state.run_paths(roots, spec.name)
    owner = state.read_owner_record(rpaths)
    failed = state.read_run_state(rpaths)
    assert {key: failed[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": backend,
        "name": spec.name,
        "run_id": owner.run_id,
        "status": "failed",
    }
    with pytest.raises(StateError, match="already exists"):
        run(spec, roots=roots, conn=FakeLibvirtConn(), profile=profile)
    assert overlay_calls == 1
    assert list(roots.state.glob(".run-create-*")) == []


def test_serial_builder_failure_holds_exact_v2_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    base = tmp_path / "serial-base.qcow2"
    base.write_bytes(b"base")
    spec = RunSpec(
        name="failed-serial",
        stack=StackRef(ImageRef(_sha256_file(base), "qcow2", "x86_64", None, base), ()),
        network="none",
    )

    def fail_overlay(_base: ImageRef, _output: Path) -> None:
        rpaths = state.run_paths(roots, spec.name)
        assert _output.parent != rpaths.root
        assert _output.parent.parent == roots.state
        owner = state.read_owner_record(rpaths)
        creating = state.read_run_state(rpaths)
        assert {
            key: creating[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")
        } == {
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "name": spec.name,
            "run_id": owner.run_id,
            "status": "creating",
        }
        raise LifecycleError("serial backend exploded")

    monkeypatch.setattr(runtime, "create_and_validate_overlay", fail_overlay)

    with pytest.raises(LifecycleError, match="serial backend exploded"):
        start_serial_builder(spec, user_data="#cloud-config\n", roots=roots, conn=FakeLibvirtConn())

    rpaths = state.run_paths(roots, spec.name)
    owner = state.read_owner_record(rpaths)
    failed = state.read_run_state(rpaths)
    assert {key: failed[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": "kvm",
        "name": spec.name,
        "run_id": owner.run_id,
        "status": "failed",
    }
    assert list(roots.state.glob(".run-create-*")) == []


def test_cloud_pre_reservation_failure_creates_no_run_entry(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"base")
    spec = RunSpec(
        name="invalid-before-reserve",
        stack=StackRef(ImageRef(_sha256_file(base), "qcow2", "x86_64", None, base), ()),
        ports=(PortForward("127.0.0.1", 18080, 80),),
    )

    with pytest.raises(ArtifactValidationError, match="port forwarding is unavailable"):
        run(spec, roots=roots, conn=FakeLibvirtConn())

    assert not (roots.runs / spec.name).exists()


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

        with pytest.raises(StateError, match="shadowed by a foreign libvirt domain"):
            reconcile(roots=roots, conn=conn)
        with pytest.raises(StateError, match="shadowed by a foreign libvirt domain"):
            inspect_run("foreign-run", roots=roots, conn=conn)
        assert domain.isActive()


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
        assert st["status"] == "running"
        assert runs[0]["status"] == "stopped"


def test_bulk_reconcile_persists_v2_drift_through_locked_single_run_path(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "v2-bulk-reconcile")
    rpaths.root.mkdir()
    owner = state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths,
        status="running",
        data={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "name": rpaths.name,
            "run_id": owner.run_id,
        },
    )

    runs, warnings = reconcile(roots=roots, conn=FakeLibvirtConn())

    assert warnings == ["run 'v2-bulk-reconcile': domain missing from libvirt"]
    assert runs[0]["status"] == "stopped"
    assert state.read_run_state(rpaths)["status"] == "stopped"


def test_bulk_reconcile_preserves_observed_result_when_v2_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "v2-bulk-write-failure")
    rpaths.root.mkdir()
    owner = state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths,
        status="running",
        data={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "name": rpaths.name,
            "run_id": owner.run_id,
        },
    )
    monkeypatch.setattr(
        state.ExistingRunMutation,
        "write_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    runs, warnings = reconcile(roots=roots, conn=FakeLibvirtConn())

    assert warnings == ["run 'v2-bulk-write-failure': domain missing from libvirt"]
    assert runs[0]["status"] == "stopped"
    assert state.read_run_state(rpaths)["status"] == "running"


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
                Path(argv[1]).with_name("console.log").write_text("PALIMPSEST_READY=1\n")
                return MagicMock(stdout="", stderr="", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        with patch("palimpsest_local.cloud_runtime.subprocess.run", side_effect=fake_subprocess_run):
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


def test_cloud_process_adapters_spawn_sessions_and_never_return_host_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "process-run")
    rpaths.root.mkdir(parents=True, mode=0o700)
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.70"})
    rpaths.identity.write_bytes(b"identity-a")
    rpaths.known_hosts.write_bytes(b"known-host-a")

    class Session:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    sessions = [Session(), Session()]
    calls: list[tuple[list[str], bool, bool, Path, Path]] = []

    def fake_spawn(argv, *, tty, stdin):
        identity_index = argv.index("-i") + 1
        known_hosts_arg = next(item for item in argv if item.startswith("UserKnownHostsFile="))
        identity = Path(argv[identity_index])
        known_hosts = Path(known_hosts_arg.split("=", 1)[1])
        assert identity.read_bytes() == b"identity-a"
        assert known_hosts.read_bytes() == b"known-host-a"
        calls.append((argv, tty, stdin, identity, known_hosts))
        return sessions[len(calls) - 1]

    monkeypatch.setattr(runtime, "spawn_process_session", fake_spawn)

    exec_session = runtime.exec_session("process-run", ExecRequest(("printf", "%s", "literal")), roots=roots)
    shell_session = runtime.shell_session("process-run", roots=roots)
    assert calls[0][1:3] == (False, False)
    assert any("palimpsest-exec" in item for item in calls[0][0])
    assert calls[1][1:3] == (True, True)
    assert calls[1][0][0] == "ssh"
    staged_paths = [path for call in calls for path in call[3:]]
    assert all(path.is_file() for path in staged_paths)

    shutil.rmtree(rpaths.root)
    rpaths.root.mkdir(mode=0o700)
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"guest_ip": "192.168.122.99"})
    rpaths.identity.write_bytes(b"identity-b")
    rpaths.known_hosts.write_bytes(b"known-host-b")
    assert [path.read_bytes() for path in calls[0][3:]] == [b"identity-a", b"known-host-a"]

    exec_session.close()
    assert all(not path.exists() for path in calls[0][3:])
    assert all(path.exists() for path in calls[1][3:])
    shell_session.close()
    assert all(not path.exists() for path in calls[1][3:])


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


def test_run_writes_backend_memory_vcpus_and_ssh_ledger_fields(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})

    base_file = roots.store / "base.qcow2"
    base_file.parent.mkdir(parents=True, exist_ok=True)
    base_file.write_bytes(b"base_data")
    base_digest = _sha256_file(base_file)

    base_ref = ImageRef(base_digest, "qcow2", "x86_64", None, base_file)
    stack = StackRef(base_ref, ())
    spec = RunSpec(name="ledger-fields-run", stack=stack, memory_mib=2048, vcpus=4)

    conn = FakeLibvirtConn()

    def fake_subprocess_run(argv, *args, **kwargs):
        if argv[0] == "qemu-img":
            if argv[1] == "create":
                Path(argv[-1]).write_bytes(b"overlay")
                return MagicMock(stdout="", stderr="", returncode=0)
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
            Path(argv[1]).with_name("console.log").write_text("PALIMPSEST_READY=1\n")
            return MagicMock(stdout="", stderr="", returncode=0)
        return MagicMock(stdout="", stderr="", returncode=0)

    with patch("palimpsest_local.cloud_runtime.subprocess.run", side_effect=fake_subprocess_run):
        res = run(spec, roots=roots, conn=conn)

    assert res["backend"] == platforms.BACKEND_KVM
    assert res["memory_mib"] == 2048
    assert res["vcpus"] == 4
    assert res["guest_ip"] == "192.168.122.100"
    assert res["ssh"] == {"host": "192.168.122.100", "port": 22}


def _hvf_test_profile(tmp_path: Path, *, with_firmware: bool = True) -> platforms.DomainProfile:
    firmware = None
    if with_firmware:
        firmware_dir = tmp_path / "firmware"
        firmware_dir.mkdir(exist_ok=True)
        loader = firmware_dir / "edk2-aarch64-code.fd"
        loader.write_bytes(b"loader")
        nvram_template = firmware_dir / "edk2-arm-vars.fd"
        nvram_template.write_bytes(b"nvram-template")
        firmware = platforms.Firmware(loader=loader, nvram_template=nvram_template)
    return platforms.DomainProfile(
        backend=platforms.BACKEND_HVF,
        domain_type="hvf",
        arch="aarch64",
        machine="virt",
        emulator=Path("/opt/homebrew/bin/qemu-system-aarch64"),
        uri="qemu:///session",
        firmware=firmware,
        autoselect_firmware=False,
        network_mode="user-hostfwd",
        seed_tool="cloud-localds",
        seed_bus="scsi",
    )


def test_run_user_hostfwd_allocates_port_and_writes_known_hosts_without_ip_discovery(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})

    base_file = roots.store / "base.qcow2"
    base_file.parent.mkdir(parents=True, exist_ok=True)
    base_file.write_bytes(b"base_data")
    base_digest = _sha256_file(base_file)

    base_ref = ImageRef(base_digest, "qcow2", "aarch64", None, base_file)
    stack = StackRef(base_ref, ())
    spec = RunSpec(name="hvf-run", stack=stack, network="default")

    hvf_profile = _hvf_test_profile(tmp_path)
    conn = FakeLibvirtConn()
    rpaths = state.run_paths(roots, "hvf-run")

    def fake_subprocess_run(argv, *args, **kwargs):
        if argv[0] == "qemu-img":
            if argv[1] == "create":
                Path(argv[-1]).write_bytes(b"overlay")
                return MagicMock(stdout="", stderr="", returncode=0)
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
            return MagicMock(stdout="", stderr="", returncode=0)
        return MagicMock(stdout="", stderr="", returncode=0)

    readiness: list[bool] = []

    def fake_wait(rpaths_in, domain, timeout_seconds, require_ip=True):
        readiness.append(require_ip)
        return None

    with (
        patch("palimpsest_local.cloud_runtime.subprocess.run", side_effect=fake_subprocess_run),
        patch("palimpsest_local.cloud_runtime._wait_for_readiness", side_effect=fake_wait),
    ):
        res = run(spec, roots=roots, conn=conn, profile=hvf_profile)

    assert readiness == [False]
    owner = state.read_owner_record(rpaths)
    assert {key: res[key] for key in ("schema_version", "runtime_kind", "backend", "name", "run_id", "status")} == {
        "schema_version": 2,
        "runtime_kind": "cloud-image",
        "backend": platforms.BACKEND_HVF,
        "name": spec.name,
        "run_id": owner.run_id,
        "status": "running",
    }
    assert res["guest_ip"] is None
    assert res["ssh"]["host"] == "127.0.0.1"
    assert 1 <= res["ssh"]["port"] <= 65535

    domain_xml = conn.domains["hvf-run"].xml_content
    assert "<interface" not in domain_xml
    assert "qemu:commandline" in domain_xml
    assert f"hostfwd=tcp:127.0.0.1:{res['ssh']['port']}-:22" in domain_xml

    known_hosts_text = rpaths.known_hosts.read_text(encoding="utf-8")
    assert f"[127.0.0.1]:{res['ssh']['port']}" in known_hosts_text

    nvram_path = rpaths.root / "nvram.fd"
    assert nvram_path.read_bytes() == b"nvram-template"
    assert (nvram_path.stat().st_mode & 0o777) == 0o600


def test_start_user_hostfwd_reallocates_port_and_rewrites_ledger_and_known_hosts(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "hvf-restart")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    rpaths.ssh.mkdir(parents=True, exist_ok=True, mode=0o700)
    (rpaths.root / "ssh_host_ed25519_key.pub").write_text("ssh-ed25519 AAAAFakeHostKey host")

    owner_rec = state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths,
        status="stopped",
        data={
            "backend": platforms.BACKEND_HVF,
            "base": {"arch": "aarch64"},
            "network": "default",
            "guest_ip": None,
            "ssh": {"host": "127.0.0.1", "port": 55123},
        },
    )

    conn = FakeLibvirtConn()
    dom_xml = (
        f"<domain><name>hvf-restart</name><metadata>"
        f'<palimpsest:run xmlns:palimpsest="{kvm.DOMAIN_MARKER_NAMESPACE}" id="{owner_rec.run_id}" '
        f'schema="1" version="0.1.0"/></metadata>'
        f'<qemu:commandline xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0">'
        f'<qemu:arg value="-netdev"/><qemu:arg value="user,id=palimpsest0,hostfwd=tcp:127.0.0.1:55123-:22"/>'
        f"</qemu:commandline></domain>"
    )
    conn.defineXML(dom_xml)
    rpaths.known_hosts.write_text("[127.0.0.1]:55123 ssh-ed25519 AAAAFakeHostKey host\n", encoding="utf-8")

    hvf_profile = _hvf_test_profile(tmp_path, with_firmware=False)

    def fake_wait(rpaths_in, domain, timeout_seconds, require_ip=True):
        assert require_ip is False
        rpaths_in.console.write_text("PALIMPSEST_READY=1\n", encoding="utf-8")
        return None

    with patch("palimpsest_local.cloud_runtime._wait_for_readiness", side_effect=fake_wait):
        res = start("hvf-restart", roots=roots, conn=conn, profile=hvf_profile)

    new_port = res["ssh"]["port"]
    assert res["status"] == "running"
    assert res["guest_ip"] is None
    assert res["ssh"]["host"] == "127.0.0.1"
    assert 1 <= new_port <= 65535
    assert new_port != 55123

    updated_xml = conn.lookupByName("hvf-restart").XMLDesc()
    assert f"hostfwd=tcp:127.0.0.1:{new_port}-:22" in updated_xml
    assert "55123" not in updated_xml

    known_hosts_text = rpaths.known_hosts.read_text(encoding="utf-8")
    assert f"[127.0.0.1]:{new_port}" in known_hosts_text
    assert "55123" not in known_hosts_text


def test_shell_and_exec_commands_use_recorded_ssh_port(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "port-run")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths, status="running", data={"guest_ip": None, "ssh": {"host": "127.0.0.1", "port": 54321}}
    )

    sh_cmd = shell_command("port-run", roots=roots)
    assert any("127.0.0.1" in arg for arg in sh_cmd)
    assert sh_cmd[sh_cmd.index("-p") + 1] == "54321"

    ex_cmd = exec_command("port-run", ["ls"], roots=roots)
    assert any("127.0.0.1" in arg for arg in ex_cmd)
    assert ex_cmd[ex_cmd.index("-p") + 1] == "54321"


def test_ssh_endpoint_falls_back_to_legacy_guest_ip_and_raises_without_either():
    assert runtime._ssh_endpoint("r", {"guest_ip": "10.0.0.5"}) == ("10.0.0.5", 22)
    assert runtime._ssh_endpoint("r", {"ssh": {"host": "10.0.0.9", "port": 2222}}) == ("10.0.0.9", 2222)
    with pytest.raises(LifecycleError, match="no reachable SSH endpoint"):
        runtime._ssh_endpoint("r", {})


def test_shell_command_raises_when_no_ssh_endpoint_recorded(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "no-endpoint-run")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"guest_ip": None})

    with pytest.raises(LifecycleError, match="no reachable SSH endpoint"):
        shell_command("no-endpoint-run", roots=roots)


def test_reconcile_treats_libvirt_hvf_backend_as_libvirt_backed(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "hvf-reconcile")
    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths,
        status="running",
        data={"backend": platforms.BACKEND_HVF, "guest_ip": None, "ssh": {"host": "127.0.0.1", "port": 12345}},
    )

    runs, warnings = reconcile(roots=roots, conn=FakeLibvirtConn())

    assert any("domain missing from libvirt" in w for w in warnings)
    assert state.read_run_state(rpaths)["status"] == "running"
    assert runs[0]["status"] == "stopped"


def test_ps_reports_backend_field_and_defaults_legacy_ledgers_to_kvm(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})

    hvf_rpaths = state.run_paths(roots, "hvf-ps-run")
    hvf_rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(hvf_rpaths)
    state.write_run_state(hvf_rpaths, status="stopped", data={"backend": platforms.BACKEND_HVF, "guest_ip": None})

    legacy_rpaths = state.run_paths(roots, "legacy-ps-run")
    legacy_rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(legacy_rpaths)
    state.write_run_state(legacy_rpaths, status="stopped", data={"guest_ip": None})

    result = {r["name"]: r for r in ps(roots=roots, conn=FakeLibvirtConn())}
    assert result["hvf-ps-run"]["backend"] == platforms.BACKEND_HVF
    assert result["legacy-ps-run"]["backend"] == platforms.BACKEND_KVM


def test_resolve_new_run_profile_preserves_legacy_conn_and_kvm_uri_callers():
    profile, uri = runtime._resolve_new_run_profile("x86_64", kvm_uri=None, profile=None, conn=object())
    assert profile.backend == platforms.BACKEND_KVM
    assert profile.arch == "x86_64"
    assert uri == profile.uri

    profile2, uri2 = runtime._resolve_new_run_profile("aarch64", kvm_uri="qemu:///system", profile=None, conn=None)
    assert profile2.backend == platforms.BACKEND_KVM
    assert profile2.arch == "aarch64"
    assert uri2 == "qemu:///system"


def test_resolve_new_run_profile_bare_call_uses_host_auto_selection_and_preflight(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    monkeypatch.setattr(
        platforms, "select_backend", lambda arch, **_kwargs: calls.append("select") or platforms.BACKEND_KVM
    )
    monkeypatch.setattr(platforms, "preflight", lambda backend, **_kwargs: calls.append("preflight"))
    profile, uri = runtime._resolve_new_run_profile("x86_64", kvm_uri=None, profile=None, conn=None)
    assert calls == ["select", "preflight"]
    assert profile.backend == platforms.BACKEND_KVM
    assert uri == profile.uri


def test_resolve_new_run_profile_bare_call_propagates_preflight_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platforms, "select_backend", lambda arch, **_kwargs: platforms.BACKEND_KVM)

    def boom(backend, **_kwargs):
        raise LifecycleError("no /dev/kvm")

    monkeypatch.setattr(platforms, "preflight", boom)
    with pytest.raises(LifecycleError, match="no /dev/kvm"):
        runtime._resolve_new_run_profile("x86_64", kvm_uri=None, profile=None, conn=None)


def test_resolve_ledger_profile_defaults_missing_backend_to_kvm():
    profile = runtime._resolve_ledger_profile({"base": {"arch": "x86_64"}})
    assert profile.backend == platforms.BACKEND_KVM
    assert profile.uri == "qemu:///system"
    assert profile.arch == "x86_64"

    profile2 = runtime._resolve_ledger_profile({"backend": platforms.BACKEND_KVM, "base": {"arch": "aarch64"}})
    assert profile2.arch == "aarch64"
    assert profile2.machine == "virt"


def test_reconcile_scoped_by_profile_ignores_other_backend(tmp_path: Path):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})

    kvm_rpaths = state.run_paths(roots, "kvm-run")
    kvm_rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(kvm_rpaths)
    state.write_run_state(kvm_rpaths, status="running", data={"backend": platforms.BACKEND_KVM, "guest_ip": None})

    hvf_rpaths = state.run_paths(roots, "hvf-run")
    hvf_rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(hvf_rpaths)
    state.write_run_state(hvf_rpaths, status="running", data={"backend": platforms.BACKEND_HVF, "guest_ip": None})

    class TrackingConn(FakeLibvirtConn):
        def __init__(self):
            super().__init__()
            self.looked_up = []

        def lookupByName(self, name: str):
            self.looked_up.append(name)
            return super().lookupByName(name)

    conn = TrackingConn()
    hvf_profile = platforms.DomainProfile(
        backend=platforms.BACKEND_HVF,
        domain_type="hvf",
        arch="aarch64",
        machine="virt",
        emulator=Path("/usr/bin/qemu-system-aarch64"),
        uri="qemu:///session",
        firmware=platforms.Firmware(
            loader=Path("/usr/share/qemu/edk2-aarch64-code.fd"),
            nvram_template=Path("/usr/share/qemu/edk2-arm-vars.fd"),
        ),
        autoselect_firmware=False,
        network_mode="user-hostfwd",
        seed_tool="hdiutil",
        seed_bus="scsi",
    )
    hvf_runs, _ = reconcile(roots=roots, conn=conn, profile=hvf_profile)

    assert conn.looked_up == ["hvf-run"]
    assert [r["name"] for r in hvf_runs] == ["hvf-run"]

    conn.looked_up.clear()
    kvm_profile = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64")
    kvm_runs, _ = reconcile(roots=roots, conn=conn, profile=kvm_profile)

    assert conn.looked_up == ["kvm-run"]
    assert [r["name"] for r in kvm_runs] == ["kvm-run"]


def test_reconcile_profile_scoped_connect_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    run_rpaths = state.run_paths(roots, "test-run")
    run_rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.write_owner_record(run_rpaths)
    state.write_run_state(run_rpaths, status="running", data={"backend": platforms.BACKEND_HVF, "guest_ip": None})

    hvf_profile = platforms.DomainProfile(
        backend=platforms.BACKEND_HVF,
        domain_type="hvf",
        arch="aarch64",
        machine="virt",
        emulator=Path("/usr/bin/qemu-system-aarch64"),
        uri="qemu:///session",
        firmware=platforms.Firmware(
            loader=Path("/usr/share/qemu/edk2-aarch64-code.fd"),
            nvram_template=Path("/usr/share/qemu/edk2-arm-vars.fd"),
        ),
        autoselect_firmware=False,
        network_mode="user-hostfwd",
        seed_tool="hdiutil",
        seed_bus="scsi",
    )

    def failing_connect(uri: str):
        raise runtime.kvm.KvmError("connection refused")

    monkeypatch.setattr(runtime.kvm, "connect", failing_connect)

    with pytest.raises(LifecycleError) as exc_info:
        reconcile(roots=roots, profile=hvf_profile)

    err_msg = str(exc_info.value)
    assert "libvirt-hvf" in err_msg
    assert "qemu:///session" in err_msg

    # Verify unprofiled call swallows connection error
    runs, warnings = reconcile(roots=roots)
    assert len(runs) == 1
    assert runs[0]["name"] == "test-run"


def test_single_run_reconcile_uses_exact_backend_uri_without_profile_or_firmware_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    for name, backend in (("kvm-exact", "kvm"), ("hvf-exact", "libvirt-hvf")):
        rpaths = state.run_paths(roots, name)
        rpaths.root.mkdir()
        owner = state.write_owner_record(rpaths)
        state.atomic_write_json(
            rpaths.state,
            {
                "schema_version": 2,
                "runtime_kind": "cloud-image",
                "backend": backend,
                "name": name,
                "run_id": owner.run_id,
                "status": "stopped",
            },
        )

    uris: list[str] = []
    monkeypatch.setattr(
        platforms,
        "resolve_domain_profile",
        lambda *_a, **_k: pytest.fail("single-run reconciliation performed create-time profile discovery"),
    )
    monkeypatch.setattr(kvm, "connect", lambda uri: uris.append(uri) or FakeLibvirtConn())

    for name in ("kvm-exact", "hvf-exact"):
        expected = state.read_run_dispatch_record(roots, name)
        result = runtime.reconcile_run(name, roots=roots, _expected_record=expected)
        assert result["state"]["status"] == "stopped"

    assert uris == ["qemu:///system", "qemu:///session"]


def test_single_run_connection_failure_does_not_create_lock_or_mutate_ledgers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")
    roots.runs.mkdir(parents=True)
    rpaths = state.run_paths(roots, "offline-run")
    rpaths.root.mkdir()
    owner = state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="stopped", data={"backend": "kvm"})
    expected = state.read_run_dispatch_record(roots, "offline-run")
    before = (rpaths.owner.read_bytes(), rpaths.state.read_bytes())
    monkeypatch.setattr(kvm, "connect", lambda _uri: (_ for _ in ()).throw(kvm.KvmError("offline")))

    with pytest.raises(LifecycleError, match="offline"):
        runtime.reconcile_run("offline-run", roots=roots, _expected_record=expected)

    assert not roots.locks.exists()
    assert (rpaths.owner.read_bytes(), rpaths.state.read_bytes()) == before

    result = runtime.inspect_run("offline-run", roots=roots)
    assert result["owner"]["run_id"] == owner.run_id
    assert result["state"]["status"] == "stopped"
    assert not roots.locks.exists()
    assert (rpaths.owner.read_bytes(), rpaths.state.read_bytes()) == before


def test_single_run_reconcile_updates_only_the_bound_target_and_preserves_sibling_bytes(tmp_path: Path) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    target = state.run_paths(roots, "target-run")
    target.root.mkdir()
    target_owner = state.write_owner_record(target)
    state.write_run_state(target, status="defined", data={"backend": "kvm"})
    sibling = state.run_paths(roots, "sibling-run")
    sibling.root.mkdir()
    state.write_owner_record(sibling)
    state.write_run_state(sibling, status="running", data={"backend": "libvirt-hvf"})
    sibling_before = (sibling.owner.read_bytes(), sibling.state.read_bytes(), sibling.state.stat().st_mtime_ns)

    marker = (
        f'<palimpsest:run xmlns:palimpsest="{kvm.DOMAIN_MARKER_NAMESPACE}" id="{target_owner.run_id}" '
        f'schema="1" version="{kvm.DOMAIN_MARKER_VERSION}"/>'
    )
    domain = FakeDomain("target-run", f"<domain><metadata>{marker}</metadata></domain>")
    domain._active = True
    conn = FakeLibvirtConn()
    conn.domains["target-run"] = domain
    expected = state.read_run_dispatch_record(roots, "target-run")
    target_before = target.state.read_bytes()

    result = runtime.reconcile_run("target-run", roots=roots, conn=conn, _expected_record=expected)

    assert result["state"]["status"] == "running"
    assert target.state.read_bytes() == target_before
    assert (sibling.owner.read_bytes(), sibling.state.read_bytes(), sibling.state.stat().st_mtime_ns) == sibling_before


def test_single_run_reconcile_does_not_swallow_state_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    rpaths = state.run_paths(roots, "write-failure")
    rpaths.root.mkdir()
    owner = state.write_owner_record(rpaths)
    state.write_run_state(
        rpaths,
        status="running",
        data={
            "schema_version": 2,
            "runtime_kind": "cloud-image",
            "backend": "kvm",
            "name": "write-failure",
            "run_id": owner.run_id,
        },
    )
    expected = state.read_run_dispatch_record(roots, "write-failure")
    monkeypatch.setattr(
        state.ExistingRunMutation,
        "write_state",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        runtime.reconcile_run("write-failure", roots=roots, conn=FakeLibvirtConn(), _expected_record=expected)
