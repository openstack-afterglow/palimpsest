"""The public qualification command reads real OCI bytes without a VM adapter."""

import hashlib
import json
import socket
import subprocess
import threading

import pytest
from test_native_root_source import os_layout
from test_oci_source import _write_oci_archive

from palimpsest_local import cli, platforms, runtime_dispatch, state
from palimpsest_local.native_root_source import revalidate_native_root_source
from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS


@pytest.fixture(autouse=True)
def isolated_source_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PALIMPSEST_STATE_HOME", raising=False)
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path / "journal"))

    def forbidden(*args, **kwargs):
        pytest.fail("source qualification invoked a host probe, runtime adapter, process or network")

    monkeypatch.setattr(platforms, "detect_host", forbidden)
    monkeypatch.setattr(runtime_dispatch, "run", forbidden)
    monkeypatch.setattr(runtime_dispatch, "run_local_oci", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def command(source, *extra):
    return ["oci", "qualify", str(source), "--platform", "linux/arm64", "--backend", "lima-vz", *extra]


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("pinned", [False, True])
def test_qualify_reads_real_source_and_prints_exact_descriptive_identities(tmp_path, capsys, archived, pinned):
    layout, root, manifest, config, layer, diff_id = os_layout(tmp_path / "layout", indexed=True)
    source = layout.root
    if archived:
        source = tmp_path / "original.oci.tar"
        _write_oci_archive(layout.root, source)
    assert cli.main(command(source, *(["--manifest", root.digest] if pinned else []))) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["source"]["root"]["descriptor"] == root.to_dict()
    assert output["source"]["manifest"]["descriptor"] == manifest.to_dict()
    assert output["source"]["config"]["descriptor"] == config.to_dict()
    assert output["layer_occurrences"] == [{"ordinal": 0, "compressed": layer.to_dict(), "diff_id": diff_id}]
    assert output["boot_profile"]["boot_method"] == "direct-linux"
    assert output["boot_profile"]["backend"] == "lima-vz"
    assert output["platform"] == "linux/arm64"
    assert (
        output["os_release"]["content_digest"]
        == "sha256:" + hashlib.sha256(b'ID=ubuntu\nVERSION_ID="24.04"\n').hexdigest()
    )
    assert output["execution_admitted"] is False
    roots = state.resolve_roots()
    # The CLI and library share the same source CAS, rather than creating a
    # second intake store or a qualification-only filesystem projection.
    cas = SourceCAS(roots.oci_source_cas)
    from palimpsest_local.native_root_source import qualify_native_root_source

    reopened = qualify_native_root_source(
        LocalLayoutSource(layout.root, root.digest), cas, platform="linux/arm64", backend="lima-vz"
    )
    assert reopened.binding_digest == output["qualification_binding_digest"]
    assert revalidate_native_root_source(reopened, cas) == reopened
    assert not roots.runs.exists()
    assert set(roots.state.iterdir()) == {roots.runtime_packs}
    assert set(roots.runtime_packs.iterdir()) == {roots.oci_source_cas}
    assert not roots.config.parent.exists()


@pytest.mark.parametrize(
    "os_bytes",
    [
        b"ID=debian\nVERSION_ID=12\n",
        b"ID=ubuntu\nVERSION_ID=22.04\n",
        b"ID=ubuntu\nVERSION_ID=24.04\nVERSION_ID=24.04\n",
        b"ID_LIKE=ubuntu\nVERSION_ID=24.04\n",
    ],
)
def test_qualify_rejects_actual_unsupported_or_invalid_os_without_success_output(tmp_path, capsys, os_bytes):
    layout, *_ = os_layout(tmp_path / "layout", os_bytes=os_bytes)
    assert cli.main(command(layout.root)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""


@pytest.mark.parametrize("failure", ["wrong-pin", "wrong-arch", "corrupt-layer", "wrong-diffid"])
def test_qualify_rejects_original_source_identity_failures(tmp_path, capsys, failure):
    layout, _root, _manifest, _config, layer, _diff_id = os_layout(
        tmp_path / "layout",
        architecture="amd64" if failure == "wrong-arch" else "arm64",
        bad_diff_id=failure == "wrong-diffid",
    )
    if failure == "corrupt-layer":
        (layout.blobs / layer.digest.split(":", 1)[1]).write_bytes(b"x" * layer.size)
    extra = ["--manifest", "sha256:" + "f" * 64] if failure == "wrong-pin" else []
    assert cli.main(command(layout.root, *extra)) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "extra",
    [
        ["--platform", "linux/amd64"],
        ["--platform", "linux/aarch64"],
        ["--backend", "auto"],
        ["--backend", "kvm"],
        ["--force"],
        ["--allow-risk"],
        ["--timeout", "nan"],
        ["--timeout", "inf"],
        ["--timeout", "0"],
        ["--timeout", "3601"],
        ["--maximum-bytes", "0"],
        ["--maximum-bytes", "8589934593"],
    ],
)
def test_qualify_has_no_force_or_risk_bypass_and_validates_bounds_before_state(tmp_path, capsys, extra):
    layout, *_ = os_layout(tmp_path / "layout")
    assert cli.main(command(layout.root, *extra)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("missing", ["--platform", "--backend"])
def test_qualify_requires_both_explicit_target_dimensions(tmp_path, capsys, missing):
    layout, *_ = os_layout(tmp_path / "layout")
    args = command(layout.root)
    position = args.index(missing)
    del args[position : position + 2]
    assert cli.main(args) == 2
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "state").exists()


def test_qualify_does_not_resolve_source_alias_before_secure_intake(tmp_path, capsys):
    layout, *_ = os_layout(tmp_path / "layout")
    alias = tmp_path / "alias"
    alias.symlink_to(layout.root, target_is_directory=True)
    assert cli.main(command(alias)) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("relative", [False, True])
def test_qualify_rejects_symlink_parent_traversal_without_normalizing_it(tmp_path, monkeypatch, capsys, relative):
    os_layout(tmp_path / "layout")
    external_child = tmp_path / "elsewhere" / "child"
    external_child.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(external_child, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    source = (alias.relative_to(tmp_path) if relative else alias) / ".." / "layout"
    assert cli.main(command(source)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""


def test_qualify_enforces_real_source_byte_budget(tmp_path, capsys):
    layout, *_ = os_layout(tmp_path / "layout")
    assert cli.main(command(layout.root, "--maximum-bytes", "100")) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("authority", ["xdg", "config", "environment"])
def test_qualify_preserves_populated_runtime_and_does_not_wait_for_its_name_lock(
    tmp_path, monkeypatch, capsys, accepted, authority
):
    roots = state.init_roots()
    default_state = roots.state
    if authority == "config":
        state.write_state_root(roots, tmp_path / "configured-state")
        roots = state.init_roots()
    elif authority == "environment":
        monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "environment-state"))
        roots = state.init_roots()
    run = state.run_paths(roots, "retained-runtime")
    state.write_owner_record(run)
    state.write_run_state(run, status="stopped", data={"backend": "lima-vz"})
    runtime_bytes = {path: path.read_bytes() for path in (run.owner, run.state)}
    quarantine = roots.run_deletions / ("unrelated-deleted-run-" + "a" * 32)
    quarantine.mkdir(mode=0o700)
    artifact = quarantine / "runtime-artifact"
    artifact.write_bytes(b"committed deletion awaiting ordinary runtime cleanup")
    quarantine_identity = (quarantine.stat().st_dev, quarantine.stat().st_ino)
    layout, *_ = os_layout(
        tmp_path / "layout",
        os_bytes=b"ID=ubuntu\nVERSION_ID=24.04\n" if accepted else b"ID=debian\nVERSION_ID=12\n",
    )
    outcomes = []

    def qualify():
        outcomes.append(cli.main(command(layout.root)))

    with state._new_run_name_lock(roots, "unrelated-deleted-run"):
        worker = threading.Thread(target=qualify)
        worker.start()
        worker.join(timeout=5)
        completed_while_locked = not worker.is_alive()
    worker.join(timeout=5)
    assert completed_while_locked
    assert not worker.is_alive()
    assert outcomes == [0 if accepted else 1]
    captured = capsys.readouterr()
    if accepted:
        assert json.loads(captured.out)["execution_admitted"] is False
    else:
        assert captured.out == ""
    assert (quarantine.stat().st_dev, quarantine.stat().st_ino) == quarantine_identity
    assert artifact.read_bytes() == b"committed deletion awaiting ordinary runtime cleanup"
    assert {path: path.read_bytes() for path in runtime_bytes} == runtime_bytes
    if authority != "xdg":
        assert not (default_state / "runtime-packs" / "oci-source-v1" / "blobs").exists()


@pytest.mark.parametrize("component", ["config", "state", "runtime_packs", "oci_source_cas"])
@pytest.mark.parametrize("unsafe_kind", ["symlink", "file"])
def test_qualify_rejects_unsafe_state_paths_without_touching_alias_targets(tmp_path, capsys, component, unsafe_kind):
    roots = state.resolve_roots()
    selected = getattr(roots, component)
    selected.parent.mkdir(parents=True, mode=0o700)
    external = tmp_path / "outside-state-authority"
    external.mkdir(mode=0o755)
    marker = external / "keep"
    marker.write_bytes(b"outside authority")
    if unsafe_kind == "symlink":
        selected.symlink_to(external, target_is_directory=True)
    else:
        selected.write_bytes(b"not a state directory")
    selected_metadata = selected.lstat()
    layout, *_ = os_layout(tmp_path / "layout")

    assert cli.main(command(layout.root)) == 1

    assert capsys.readouterr().out == ""
    current = selected.lstat()
    assert (current.st_dev, current.st_ino, current.st_mode) == (
        selected_metadata.st_dev,
        selected_metadata.st_ino,
        selected_metadata.st_mode,
    )
    assert set(external.iterdir()) == {marker}
    assert marker.read_bytes() == b"outside authority"
    assert external.stat().st_mode & 0o777 == 0o755
