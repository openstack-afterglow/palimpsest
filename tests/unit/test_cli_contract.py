"""Compatibility and safety contract for the standalone argparse CLI."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from types import MappingProxyType

import pytest

from palimpsest_local import cli, runtime_dispatch, state
from palimpsest_local.errors import PalimpsestError, StateError
from palimpsest_local.oci_layout import ContentStore
from palimpsest_local.refs import ImageRef, RunSpec, StackRef
from palimpsest_local.runtime_types import (
    CapabilityCheck,
    CapabilityErrorCategory,
    CommitResult,
    DispatchKey,
    ExistingRunRecord,
    InspectLifecycle,
    InspectPort,
    InspectRecord,
    OCIRootInspectDetail,
    ProcessCapabilities,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessSignal,
    ProcessStatusEvent,
    ProcessStream,
    RunAggregationResult,
    RunAttachmentMode,
    RunResult,
    RunSummary,
    RuntimeBackend,
    RuntimeKind,
    RuntimePreflightError,
    _issue_lifecycle_adapter_outcome,
)


def test_external_v2_cloud_init_cli_emits_writable_runtime(tmp_path, capsys):
    config = tmp_path / "runtime-v2.json"
    config.write_text(
        json.dumps(
            {
                "schema": "palimpsest.external-layer-runtime.v2",
                "arch": "x86_64",
                "filesystem_uuid": "12345678-1234-5678-9abc-123456789abc",
                "stacks": [{"name": "native", "layers": ["sha256:" + "a" * 64]}],
            }
        )
    )
    assert cli.main(["layer", "cloud-init", "--config", str(config)]) == 0
    result = capsys.readouterr().out
    assert result.startswith("#cloud-config\n")
    assert "rw,nosuid,nodev,lowerdir=" in result
    assert "upperdir=" in result and "workdir=" in result
    assert "/var/lib/palimpsest/consumer" in result


def test_layer_cloud_init_never_replaces_an_intervening_destination(tmp_path, monkeypatch):
    config = tmp_path / "external.json"
    config.write_text(
        json.dumps(
            {
                "schema": "palimpsest.external-layer-runtime.v2",
                "arch": "x86_64",
                "filesystem_uuid": "12345678-1234-5678-9abc-123456789abc",
                "stacks": [{"name": "native", "layers": ["sha256:" + "a" * 64]}],
            }
        )
    )
    output = tmp_path / "user-data.yaml"
    mkstemp = cli.tempfile.mkstemp

    def create_intervening_output(*args, **kwargs):
        output.write_text("another actor's cloud-init policy\n")
        return mkstemp(*args, **kwargs)

    monkeypatch.setattr(cli.tempfile, "mkstemp", create_intervening_output)
    assert cli.main(["layer", "cloud-init", "--config", str(config), "--output", str(output)]) == 1
    assert output.read_text() == "another actor's cloud-init policy\n"


@pytest.fixture(autouse=True)
def _isolated_xdg_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI state/config writes out of the developer's real XDG roots."""
    journal_root = tmp_path / "host-journal"
    journal_root.mkdir(mode=0o700)
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(journal_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    # Automatic acquisition must not depend on the developer's Docker daemon,
    # credential directory, context/host, or preferred registry.
    from palimpsest_local import run_source

    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker-config"))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("PALIMPSEST_REGISTRY", raising=False)
    monkeypatch.setattr(run_source, "find_docker_daemon_source", lambda *a, **k: None)
    monkeypatch.setattr(
        run_source,
        "run_docker_command",
        lambda *a, **k: pytest.fail("real Docker inspection reached in CLI contract test"),
    )
    monkeypatch.setattr(
        run_source,
        "export_docker_image_to_oci_archive",
        lambda *a, **k: pytest.fail("real Docker export reached in CLI contract test"),
    )
    monkeypatch.setattr(
        run_source,
        "pull_registry_oci_archive",
        lambda *a, **k: pytest.fail("real registry acquisition reached in CLI contract test"),
    )
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "test-present", True),
    )


def _write_cli_run_ledger(
    *,
    backend: str,
    runtime_kind: str = "cloud-image",
) -> tuple[state.StatePaths, state.RunPaths]:
    roots = state.init_roots()
    rpaths = state.run_paths(roots, "demo-vm")
    rpaths.root.mkdir()
    run_id = str(uuid.uuid4())
    rpaths.owner.write_text(
        json.dumps({"schema_version": 1, "run_id": run_id, "name": "demo-vm"}) + "\n",
        encoding="utf-8",
    )
    rpaths.owner.chmod(0o600)
    rpaths.state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "runtime_kind": runtime_kind,
                "backend": backend,
                "name": "demo-vm",
                "run_id": run_id,
                "status": "stopped",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rpaths.state.chmod(0o600)
    return roots, rpaths


def _fake_run_result(name: str, backend: RuntimeBackend, guest_ip: str | None = None) -> RunResult:
    record = ExistingRunRecord(
        name=name,
        run_id="862ffb44-6795-4618-b2d8-c0750439fac3",
        state_schema_version=2,
        dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, backend),
    )
    return RunResult(record, "running", True, RunAttachmentMode.DETACHED, guest_ip)


def _oci_inspect_record() -> InspectRecord:
    record = ExistingRunRecord(
        name="demo-vm",
        run_id="862ffb44-6795-4618-b2d8-c0750439fac3",
        state_schema_version=2,
        dispatch_key=DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )
    return InspectRecord(
        schema_version=1,
        record=record,
        lifecycle=InspectLifecycle("running", 4, "2026-09-16T00:00:00Z", "2026-09-16T00:01:00Z"),
        detail=OCIRootInspectDetail(
            memory_mib=4096,
            vcpus=4,
            network="nat",
            ports=(
                InspectPort("127.0.0.1", 18080, 8080, "tcp"),
                InspectPort("0.0.0.0", 18443, 8443, "udp"),
            ),
            guest_ip="10.0.2.15",
        ),
    )


def _snapshot_cli_state(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    snapshot: dict[str, tuple[int, int, bytes | None]] = {}
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        snapshot[relative] = (
            metadata.st_mode,
            metadata.st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
    return snapshot


_CLI_EXISTING_RUN_OPERATIONS: tuple[tuple[str, str, list[str], dict[str, object]], ...] = (
    ("start", "start", ["start", "demo-vm"], {}),
    ("stop", "stop", ["stop", "demo-vm"], {}),
    ("rm", "rm", ["rm", "demo-vm", "--volumes"], {"volumes": True}),
    ("inspect", "inspect_run", ["inspect", "demo-vm"], {}),
)


def test_digest_file_streams_hash(tmp_path: Path):
    payload = b"palimpsest layer bytes" * 100
    path = tmp_path / "layer.squashfs"
    path.write_bytes(payload)
    assert cli.digest_file(path) == f"sha256:{hashlib.sha256(payload).hexdigest()}"


def test_non_run_image_resolution_pulls_without_runtime_capability_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    store = ContentStore(roots.store)
    payload = b"ubuntu-arm64-cloud-image"
    digest_value = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    calls: list[object] = []

    class FakeHub:
        def __init__(self, url: str, token: str):
            calls.append((url, token))

        def get_layer(self, requested: str):
            assert requested == digest_value
            return {"kind": "cloud-image", "disk_format": "raw", "arch": "aarch64", "os_variant": "ubuntu"}

        def pull_blob(self, requested: str, target: Path):
            assert requested == digest_value
            target.write_bytes(payload)
            return target

    monkeypatch.setattr(cli, "HubClient", FakeHub)
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")
    monkeypatch.setattr(
        cli.runtime_dispatch,
        "preflight_run_capabilities",
        lambda *_args, **_kwargs: pytest.fail("non-run image resolution entered runtime preflight"),
    )

    image = cli._resolve_image_ref(store, digest_value, "https://hub.example.test")

    assert image.arch == "aarch64"
    assert image.local_path.read_bytes() == payload
    assert calls == [("https://hub.example.test", "test-token")]


def test_image_resolution_repairs_unsealed_legacy_blob_via_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    store = ContentStore(roots.store)
    payload = b"legacy-direct-download"
    digest_value = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    blob = store.blob_path(digest_value)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)
    assert blob.stat().st_mode & 0o777 == 0o644
    destinations: list[Path] = []

    class FakeHub:
        def __init__(self, _url: str, _token: str):
            pass

        def get_layer(self, requested: str):
            assert requested == digest_value
            return {"kind": "cloud-image", "disk_format": "raw", "arch": "aarch64"}

        def pull_blob(self, requested: str, target: Path):
            assert requested == digest_value
            destinations.append(target)
            target.write_bytes(payload)
            return target

    monkeypatch.setattr(cli, "HubClient", FakeHub)
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")

    image = cli._resolve_image_ref(store, digest_value, "https://hub.example.test")

    assert image.local_path == blob
    assert store.verify_blob(digest_value) == len(payload)
    assert blob.stat().st_mode & 0o777 == 0o444
    assert len(destinations) == 1 and destinations[0] != blob


def test_pack_command_matches_fixed_mksquashfs_argv(tmp_path: Path):
    source = tmp_path / "rootfs"
    source.mkdir()
    assert cli.build_mksquashfs_command(source, tmp_path / "out.squashfs") == [
        "mksquashfs",
        str(source),
        str(tmp_path / "out.squashfs"),
        "-comp",
        "zstd",
        "-Xcompression-level",
        "3",
        "-noappend",
        "-no-exports",
    ]
    with pytest.raises(PalimpsestError):
        cli.build_mksquashfs_command(tmp_path / "missing", tmp_path / "out.squashfs")


def test_default_hub_keeps_authentication_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("PALIMPSEST_URL", raising=False)
    monkeypatch.delenv("PALIMPSEST_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert cli.resolve_url(None) == "https://palimpsest.dmslab.re.kr"
    with pytest.raises(PalimpsestError, match="PALIMPSEST_TOKEN"):
        cli.resolve_token()


def test_explicit_then_environment_then_config_url_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = tmp_path / "config" / "palimpsest" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[hub]\nurl = 'https://configured.example/'\n", encoding="utf-8")
    assert cli.resolve_url(None) == "https://configured.example"
    monkeypatch.setenv("PALIMPSEST_URL", "https://environment.example/")
    assert cli.resolve_url(None) == "https://environment.example"
    assert cli.resolve_url("https://explicit.example/") == "https://explicit.example"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--help"],
        ["build", "--help"],
        ["run", "--help"],
        ["store", "--help"],
        ["store", "ls", "--help"],
        ["image", "--help"],
        ["image", "ls", "--help"],
        ["images", "--help"],
        ["registry", "--help"],
        ["registry", "add", "--help"],
        ["completion", "--help"],
    ],
)
def test_cli_help_is_useful_without_journal_state_or_hub(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected(*_args, **_kwargs):
        pytest.fail("help accessed journal, state, or Hub")

    monkeypatch.setattr(cli.host_journal, "begin", unexpected)
    for name in ("init_roots", "resolve_roots", "HubClient", "resolve_url", "resolve_token"):
        monkeypatch.setattr(cli, name, unexpected)

    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("usage: palimpsest")
    assert "-h, --help" in captured.out
    if not argv:
        assert "Examples:" in captured.out
        assert "palimpsest image import" in captured.out
        assert 'palimpsest run "$digest" --name dev' in captured.out
    assert not captured.err
    assert not (tmp_path / "xdg-config").exists()
    assert not (tmp_path / "xdg-state").exists()


@pytest.mark.parametrize("shell", [None, "bash", "zsh", "fish"])
def test_hidden_completion_emits_real_protocol_without_journal_or_state(
    shell: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def unexpected(*_args, **_kwargs):
        pytest.fail("completion accessed journal or state")

    monkeypatch.setattr(cli.host_journal, "begin", unexpected)
    monkeypatch.setattr(cli, "init_roots", unexpected)
    monkeypatch.setattr(cli, "resolve_roots", unexpected)
    monkeypatch.setattr(cli, "HubClient", unexpected)
    protocol = [] if shell is None else ["--shell", shell]
    assert cli.main(["__complete", *protocol, "--", "build", "--runtime-"]) == 0
    lines = capsys.readouterr().out.splitlines()
    if shell is not None:
        assert lines.pop(0) == "__palimpsest_directive__\t4"
    assert {line.partition("\t")[0] for line in lines} == {
        "--runtime-base",
        "--runtime-block-size",
        "--runtime-push",
        "--runtime-tag",
    }
    assert all(("\t" in line) == (shell in ("fish", "zsh")) for line in lines)
    assert not (tmp_path / "xdg-config").exists()
    assert not (tmp_path / "xdg-state").exists()


@pytest.mark.parametrize("argv", [["__complete", "--shell"], ["__complete", "--shell", "invalid", "--"]])
def test_hidden_completion_rejects_invalid_protocol_before_journal(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_args: pytest.fail("journal accessed"))
    assert cli.main(argv) == 2
    assert "__complete requires --shell" in capsys.readouterr().err


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_script_then_real_install_avoids_journal_state(
    shell: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ZDOTDIR", raising=False)

    def unexpected(*_args, **_kwargs):
        pytest.fail("completion accessed journal or state")

    monkeypatch.setattr(cli.host_journal, "begin", unexpected)
    monkeypatch.setattr(cli, "init_roots", unexpected)
    monkeypatch.setattr(cli, "resolve_roots", unexpected)
    monkeypatch.setattr(cli, "HubClient", unexpected)
    assert cli.main(["completion", shell]) == 0
    script = capsys.readouterr().out
    target = {
        "bash": tmp_path / ".bashrc",
        "zsh": tmp_path / ".zshrc",
        "fish": tmp_path / "xdg-config/fish/completions/palimpsest.fish",
    }[shell]
    assert not target.exists()
    assert "__complete" in script
    assert cli.main(["completion", shell, "--install"]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert target.is_file()
    assert script.strip() in target.read_text()
    assert not (tmp_path / "xdg-state").exists()


def test_completion_install_preserves_conflicting_user_file_and_reports_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "xdg-config/fish/completions/palimpsest.fish"
    target.parent.mkdir(parents=True, mode=0o700)
    original = b"# existing user completion\n"
    target.write_bytes(original)
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_args: pytest.fail("journal accessed"))
    monkeypatch.setattr(cli, "init_roots", lambda *_args: pytest.fail("state accessed"))
    assert cli.main(["completion", "fish", "--install"]) == 1
    assert target.read_bytes() == original
    assert str(target) in capsys.readouterr().err


def test_parser_accepts_additive_buildkit_dockerfile_surface():
    parsed = cli.build_parser().parse_args(
        [
            "build",
            ".",
            "--frontend",
            "dockerfile",
            "-f",
            "Dockerfile",
            "-t",
            "demo:test",
            "--platform",
            "linux/arm64",
            "--build-arg",
            "MODE=release",
            "--local-image",
            "base=/tmp/base@sha256:" + "a" * 64,
            "--cache-scope",
            "demo",
            "--runtime-base",
            "sha256:" + "b" * 64,
            "--runtime-tag",
            "demo-runtime",
            "--runtime-block-size",
            "262144",
            "--offline",
        ]
    )

    assert parsed.context == Path(".")
    assert parsed.frontend == "dockerfile"
    assert parsed.tag == ["demo:test"]
    assert parsed.offline is True
    assert parsed.network == "none"
    assert parsed.runtime_block_size == 262144


@pytest.mark.parametrize("automatic", [False, True])
def test_oci_materialize_dispatches_local_archive_and_emits_path_free_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], automatic: bool
) -> None:
    real_state_home = tmp_path / "real-state-home"
    real_state_home.mkdir()
    state_home_alias = tmp_path / "state-home-alias"
    state_home_alias.symlink_to(real_state_home, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home_alias))
    real_source = tmp_path / "real-source"
    real_source.mkdir()
    source_alias = tmp_path / "source-alias"
    source_alias.symlink_to(real_source, target_is_directory=True)
    archive = real_source / "image.oci.tar"
    archive.write_bytes(b"archive")
    packer = tmp_path / "mksquashfs"
    packer.write_bytes(b"packer")
    manifest = "sha256:" + "a" * 64
    snapshot = object()
    calls: dict[str, object] = {}

    class FakeSource:
        def __init__(self, *, archive: Path, root_digest: str):
            calls["archive"] = archive
            calls["root_digest"] = root_digest

        def snapshot(self, reference, source_cas):
            calls["reference"] = reference
            calls["source_cas"] = source_cas
            return snapshot

    class FakeReceipt:
        digest = "sha256:" + "b" * 64

        def to_dict(self):
            return {"schema": "palimpsest.oci-image-materialization.v2", "results": []}

    def fake_materialize(selected, **kwargs):
        calls["snapshot"] = selected
        calls["materialize"] = kwargs
        return FakeReceipt()

    monkeypatch.setattr(cli, "LocalArchiveSource", FakeSource)
    monkeypatch.setattr(cli, "OCIStore", lambda roots: ("store", roots))
    monkeypatch.setattr(cli, "discover_squashfs_toolchain", lambda *args, **kwargs: (args, kwargs))
    monkeypatch.setattr(cli, "materialize_image_hard", fake_materialize)

    result = cli.main(
        [
            "oci",
            "materialize",
            str(source_alias / archive.name),
            *([] if automatic else ["--manifest", manifest]),
            "--packer",
            str(packer),
            "--timeout",
            "12",
        ]
    )

    assert result == 0
    assert calls["archive"] == archive.resolve()
    assert calls["root_digest"] == (None if automatic else manifest)
    if automatic:
        assert calls["reference"] is None
    assert calls["snapshot"] is snapshot
    materialize = calls["materialize"]
    assert isinstance(materialize, dict)
    assert materialize["timeout_seconds"] == 12.0
    assert materialize["roots"].state == (real_state_home / "palimpsest").resolve()
    assert materialize["source_cas_root"] == (real_state_home / "palimpsest/runtime-packs/oci-source-v1").resolve()
    payload = json.loads(capsys.readouterr().out)
    assert payload["receipt_digest"] == FakeReceipt.digest
    assert str(tmp_path) not in json.dumps(payload)


def test_buildkit_offline_dispatch_reads_no_registry_profile_or_hub_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    context = tmp_path / "context"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PALIMPSEST_URL", raising=False)
    monkeypatch.delenv("PALIMPSEST_TOKEN", raising=False)
    # A corrupt profile file proves strict offline never resolves registry profiles.
    profiles = tmp_path / "config" / "palimpsest" / "registries.toml"
    profiles.parent.mkdir(parents=True)
    profiles.write_text("not = [valid", encoding="utf-8")
    profiles.chmod(0o600)
    captured = []

    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("offline dispatch constructed a remote client")

    def fake_build(spec, roots, *, hub_client=None, cache_package=None, runtime_hub_client=None, on_oci_export=None):
        captured.append((spec, hub_client, cache_package, runtime_hub_client, on_oci_export))
        return {
            "build_id": "bk-000000000000",
            "runtime_block_digest": None,
            "output_oci_manifest_digest": "sha256:" + "c" * 64,
            "output_oci_archive_digest": "sha256:" + "d" * 64,
        }

    monkeypatch.setattr(cli, "HubClient", forbidden_client)
    monkeypatch.setattr(cli, "NativePackageClient", forbidden_client)
    monkeypatch.setattr(cli, "build_with_buildkit", fake_build)

    assert cli.main(["build", str(context), "-f", str(dockerfile), "-t", "demo", "--offline"]) == 0
    assert len(captured) == 1
    spec, hub_client, cache_package, runtime_hub_client, on_oci_export = captured[0]
    assert (spec.offline, spec.network, spec.push_cache, spec.push_image, spec.push) == (
        True,
        "none",
        False,
        False,
        False,
    )
    assert (spec.tag, spec.registry_profile, spec.registry_config_digest) == ("demo", None, None)
    assert (spec.external_cache_from, spec.external_cache_to) == ((), ())
    assert (hub_client, cache_package, runtime_hub_client, on_oci_export) == (None, None, None, None)
    assert capsys.readouterr().out.strip() == "sha256:" + "c" * 64


@pytest.mark.parametrize("source_kind", ["directory", "archive", "retained"])
@pytest.mark.parametrize("outcome", ["success", "solve-error", "pack-error"])
def test_buildkit_cli_local_inputs_survive_solve_and_runtime_pack_then_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_kind: str,
    outcome: str,
) -> None:
    import io
    import subprocess
    import tarfile
    from functools import partial

    from palimpsest_local import buildkit
    from palimpsest_local.package_reference import LocalPackageReference, write_package_reference

    roots = cli.init_roots()
    layout = tmp_path / "base-layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")

    def blob(media_type: str, payload: bytes) -> dict[str, object]:
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        (blobs / digest.split(":", 1)[1]).write_bytes(payload)
        return {"mediaType": media_type, "digest": digest, "size": len(payload)}

    layer_stream = io.BytesIO()
    with tarfile.open(fileobj=layer_stream, mode="w"):
        pass
    layer_bytes = layer_stream.getvalue()
    layer = blob("application/vnd.oci.image.layer.v1.tar", layer_bytes)
    config = blob(
        "application/vnd.oci.image.config.v1+json",
        json.dumps(
            {
                "architecture": "amd64",
                "os": "linux",
                "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]},
                "config": {},
            }
        ).encode(),
    )
    manifest = blob(
        "application/vnd.oci.image.manifest.v1+json",
        json.dumps({"schemaVersion": 2, "config": config, "layers": [layer]}).encode(),
    )
    root = manifest["digest"]
    (layout / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [manifest]}), encoding="utf-8")
    archive = tmp_path / "base.oci.tar"
    with tarfile.open(archive, "w") as stream:
        for path in sorted(layout.rglob("*")):
            if path.is_file():
                stream.add(path, arcname=str(path.relative_to(layout)), recursive=False)
    archive_bytes = archive.read_bytes()
    reference = f"cloud.example/project/base@{root}"
    write_package_reference(
        roots,
        LocalPackageReference(
            reference=reference,
            authority="cloud.example",
            api_base="https://cloud.example/v1",
            namespace="project",
            package="base",
            archive=str(archive),
            archive_digest="sha256:" + hashlib.sha256(archive_bytes).hexdigest(),
            archive_size_bytes=len(archive_bytes),
            root_digest=root,
            package_type="oci-image",
        ),
    )
    source = {"directory": f"{layout}@{root}", "archive": f"{archive}@{root}", "retained": reference}[source_kind]
    context = tmp_path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM base\n", encoding="utf-8")
    base_bytes = b"runtime-base"
    base_digest = "sha256:" + hashlib.sha256(base_bytes).hexdigest()
    store = ContentStore(roots.store)
    store.write_stream([base_bytes], expected_digest=base_digest)
    store.write_metadata(base_digest, {"kind": "cloud-image", "disk_format": "qcow2", "arch": "x86_64"})
    profiles = roots.config / "registries.toml"
    profiles.write_text("not = [valid", encoding="utf-8")
    profiles.chmod(0o600)
    consumed_layouts = []
    packed_manifests = {}

    def runner(argv, **kwargs):
        command = list(map(str, argv))
        replies = {
            ("docker", "buildx", "inspect"): (
                "Name: palimpsest\nDriver: docker-container\nNodes:\n"
                "Name: palimpsest0\nEndpoint: default\nBuildKit: v0.23.2\n"
            ),
            ("docker", "buildx", "version"): "github.com/docker/buildx v0.28.0 test\n",
            ("docker", "context", "show"): "default\n",
        }
        if tuple(command) in replies:
            return subprocess.CompletedProcess(command, 0, stdout=replies[tuple(command)], stderr="")
        if command[:3] == ["docker", "context", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout="unix:///var/run/docker.sock\n", stderr="")
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, stdout="buildkit-container\n", stderr="")
        if command[:3] == ["docker", "inspect", "--format"]:
            stdout = '{"none":{}}\n' if command[3] == "{{json .NetworkSettings.Networks}}" else "none\n"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        if command[:3] == ["docker", "buildx", "build"]:
            named = command[command.index("--build-context") + 1]
            assert named.startswith("base=oci-layout://")
            frozen, digest = named.removeprefix("base=oci-layout://").rsplit("@", 1)
            assert digest == root
            frozen = Path(frozen)
            assert (frozen / "blobs" / "sha256" / str(layer["digest"]).split(":", 1)[1]).read_bytes() == layer_bytes
            consumed_layouts.append(frozen)
            if outcome == "solve-error":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="deliberate solve failure")
            cache = command[command.index("--cache-to") + 1]
            cache_path = Path(dict(field.split("=", 1) for field in cache.split(","))["dest"])
            cache_path.mkdir(parents=True)
            (cache_path / "index.json").write_text('{"schemaVersion":2}', encoding="utf-8")
            metadata = Path(command[command.index("--metadata-file") + 1])
            metadata.write_text(json.dumps({"containerimage.digest": root}), encoding="utf-8")
            for index, value in enumerate(command):
                if value == "--output":
                    fields = dict(field.split("=", 1) for field in command[index + 1].split(","))
                    Path(fields["dest"]).write_bytes(archive_bytes if fields["type"] == "oci" else layer_bytes)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command == ["mksquashfs", "-version"]:
            return subprocess.CompletedProcess(command, 0, stdout="mksquashfs version 4.7.5 (test)\n", stderr="")
        if command[0] == "mksquashfs":
            assert consumed_layouts[0].is_dir(), "OCI snapshot released before runtime packing"
            if outcome == "pack-error":
                return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"deliberate pack failure")
            with tarfile.open(fileobj=kwargs["stdin"], mode="r:") as stream:
                packed_manifests[Path(command[2])] = stream.extractfile(".palimpsest/runtime-pack.json").read()
            Path(command[2]).write_bytes(b"hsqs-packed-runtime")
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        if command[0] == "unsquashfs":
            return subprocess.CompletedProcess(command, 0, stdout=packed_manifests[Path(command[2])], stderr=b"")
        pytest.fail(f"unexpected external command: {command}")

    # Inject only the process boundary; execute the real resolver, spec validation,
    # Buildx command construction, cache handling, and runtime packing path.
    monkeypatch.setattr(cli, "build_with_buildkit", partial(buildkit.build_with_buildkit, runner=runner))
    result = cli.main(
        [
            "build",
            str(context),
            "-t",
            "demo",
            "--offline",
            "--local-image",
            f"base={source}",
            "--runtime-base",
            base_digest,
            "--runtime-tag",
            "demo-runtime",
        ]
    )
    captured = capsys.readouterr()
    assert len(consumed_layouts) == 1
    if source_kind == "directory":
        assert consumed_layouts[0] == layout
        assert layout.is_dir()
    else:
        assert consumed_layouts[0] != layout
        assert not consumed_layouts[0].exists()
    assert archive.read_bytes() == archive_bytes
    if outcome == "success":
        assert result == 0
        assert captured.out.strip() == "sha256:" + hashlib.sha256(b"hsqs-packed-runtime").hexdigest()
    else:
        assert result == 1
        assert f"deliberate {'solve' if outcome == 'solve-error' else 'pack'} failure" in captured.err


def test_buildkit_runtime_base_arch_mismatch_fails_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    roots = cli.init_roots()
    context = tmp_path / "context"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    base_bytes = b"arm64-runtime-base"
    base_digest = f"sha256:{hashlib.sha256(base_bytes).hexdigest()}"
    store = ContentStore(roots.store)
    store.blobs_dir.mkdir(parents=True)
    store.write_stream([base_bytes], expected_digest=base_digest)
    store.write_metadata(base_digest, {"kind": "cloud-image", "disk_format": "qcow2", "arch": "aarch64"})

    monkeypatch.setattr(
        cli,
        "build_with_buildkit",
        lambda *_args, **_kwargs: pytest.fail("BuildKit started before runtime architecture validation"),
    )

    assert (
        cli.main(
            [
                "build",
                str(context),
                "-t",
                "demo",
                "--offline",
                "--platform",
                "linux/amd64",
                "--runtime-base",
                base_digest,
                "--runtime-tag",
                "demo-runtime",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "linux/amd64 targets x86_64" in error
    assert "is aarch64" in error


@pytest.mark.parametrize(
    "argv",
    [
        ["build", ".", "-t", "demo", "--offline", "--network", "default"],
        ["build", ".", "-t", "demo", "--offline", "--push"],
        ["build", ".", "-t", "demo", "--offline", "--runtime-push"],
        ["build", ".", "-t", "demo", "--offline", "--pull"],
        ["build", ".", "-t", "demo", "--offline", "--cache-from", "type=local,src=.cache"],
        ["build", ".", "-t", "demo", "--offline", "--cache-to", "type=local,dest=.cache"],
        ["build", ".", "-t", "demo", "--offline", "--registry", "corp"],
        ["build", ".", "-t", "demo", "--offline", "--cache-registry", "cloud"],
        ["build", ".", "-t", "demo", "--offline", "--cache-package", "project/test"],
        ["build", ".", "-t", "demo", "--runtime-tag", "runtime"],
    ],
)
def test_buildkit_cli_rejects_incomplete_or_networked_offline_forms(argv: list[str]):
    assert cli.main(argv) == 2


@pytest.mark.parametrize(
    "buildkit_option",
    [
        ["--offline"],
        ["--platform", "linux/amd64"],
        ["--target", "runtime"],
        ["--build-arg", "MODE=release"],
        ["--local-image", "base=/tmp/base@sha256:" + "a" * 64],
        ["--cache-scope", "default"],
        ["--cache-mode", "local"],
        ["--registry", "corp"],
        ["--cache-registry", "cloud"],
        ["--cache-package", "project/test"],
        ["--cache-from", "type=registry,ref=registry.example.com/cache/from"],
        ["--cache-to", "type=registry,ref=registry.example.com/cache/to,mode=max"],
        ["--no-cache"],
        ["--pull"],
        ["--load"],
        ["--progress", "tty"],
        ["--output", "/tmp/image.oci.tar"],
        ["--rootfs-output", "/tmp/rootfs"],
        ["--runtime-tag", "runtime"],
        ["--runtime-base", "sha256:" + "b" * 64],
        ["--runtime-block-size", "131072"],
        ["--push"],
        ["--runtime-push"],
    ],
)
def test_palimpsestfile_frontend_rejects_every_buildkit_only_option(
    buildkit_option: list[str], capsys: pytest.CaptureFixture[str]
):
    argv = [
        "build",
        "--frontend",
        "palimpsestfile",
        "--base",
        "sha256:" + "c" * 64,
        "--tag",
        "legacy",
        *buildkit_option,
    ]

    assert cli.main(argv) == 2
    assert buildkit_option[0] in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [
            "image",
            "ls",
            "--ubuntu-base",
            "24.04",
            "--arch",
            "aarch64",
            "--os-variant",
            "ubuntu24.04",
            "--disk-format",
            "raw",
            "--limit",
            "200",
        ],
        ["image", "pull", "sha256:" + "a" * 64, "--output", "out"],
        ["image", "verify", "base.raw", "--digest", "sha256:" + "a" * 64],
        [
            "image",
            "push",
            "base.raw",
            "--name",
            "base",
            "--disk-format",
            "raw",
            "--arch",
            "aarch64",
            "--os-variant",
            "ubuntu24.04",
            "--ubuntu-base",
            "24.04",
            "--publish",
        ],
        ["layer", "ls", "--name", "layer", "--kind", "squashfs", "--parent", "sha256:" + "a" * 64, "--limit", "1"],
        ["layer", "pull", "sha256:" + "a" * 64, "--output", "out"],
        ["layer", "pack", "rootfs", "--tag", "layer"],
        [
            "layer",
            "push",
            "layer",
            "--name",
            "layer",
            "--parent",
            "sha256:" + "a" * 64,
            "--base-image",
            "sha256:" + "b" * 64,
            "--ubuntu-base",
            "24.04",
            "--publish",
        ],
        ["bundle", "pull", "sha256:" + "a" * 64, "--output", "out", "--include-base"],
        ["bundle", "verify", "layout"],
        [
            "build",
            "--base",
            "sha256:" + "a" * 64,
            "--tag",
            "layer",
            "-f",
            "Recipe",
            "--layer",
            "sha256:" + "b" * 64,
            "--network",
            "default",
        ],
        [
            "run",
            "stack",
            "--name",
            "demo",
            "--layer",
            "sha256:" + "a" * 64,
            "--memory",
            "512",
            "--vcpus",
            "3",
            "--network",
            "isolated",
        ],
        ["ps"],
        ["inspect", "demo"],
        ["logs", "demo", "--follow"],
        ["shell", "demo"],
        ["exec", "demo", "--", "printf", "%s", "hello"],
        ["start", "demo"],
        ["stop", "demo"],
        ["rm", "demo", "--volumes"],
        ["commit", "demo", "--tag", "layer"],
        ["web"],
        ["web", "--port", "8080", "--no-browser", "--allow-control"],
        ["store", "show"],
        ["store", "show", "--format", "json"],
        ["store", "ls"],
        ["store", "ls", "--kind", "image", "--format", "table"],
        ["store", "ls", "--kind", "layer", "--format", "json"],
        ["store", "rm", "sha256:" + "a" * 64],
        ["store", "rm", "sha256:" + "a" * 64, "--force"],
        ["store", "move", "--to", "/tmp/dest"],
        ["store", "move", "--to", "/tmp/dest", "--keep-source"],
        ["store", "set", "--to", "/tmp/dest"],
    ],
)
def test_parser_accepts_every_v1_command_form(argv: list[str]):
    cli.build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["--token", "secret", "ps"],
        ["image", "ls", "--publish"],
        ["login", "registry.example.com", "--password", "secret"],
    ],
)
def test_parser_rejects_prohibited_legacy_aliases_and_flags(argv: list[str], capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as rejected:
        cli.build_parser().parse_args(argv)
    assert rejected.value.code == 2
    assert capsys.readouterr().err


def test_exec_requires_nonempty_remainder(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["exec", "demo", "--"]) == 2
    assert "command" in capsys.readouterr().err


def test_top_level_exec_consumes_dispatcher_session_without_spawning_in_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object()
    calls: list[tuple[str, list[str], int | None]] = []

    def fake_exec(name, argv, *, roots, timeout_ms=None):
        del roots
        calls.append((name, argv, timeout_ms))
        return session

    monkeypatch.setattr(cli.runtime_dispatch, "exec", fake_exec)
    monkeypatch.setattr(
        cli,
        "_run_process_session",
        lambda candidate, *, interactive: 23 if candidate is session and not interactive else pytest.fail(),
    )

    assert cli.main(["exec", "demo", "--", "printf", "%s", "literal value"]) == 23
    assert calls == [("demo", ["printf", "%s", "literal value"], None)]


def test_cli_run_attached_result_bridges_generic_session_and_returns_its_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "base.qcow2"
    image.write_bytes(b"cloud")
    stack = StackRef(
        ImageRef("sha256:" + hashlib.sha256(b"cloud").hexdigest(), "qcow2", "x86_64", None, image),
        (),
    )
    terminal = ProcessExit(7, 7, None, ProcessExitCategory.EXITED)

    class Session:
        capabilities = ProcessCapabilities(False, False, False, True)

        def events(self):
            yield ProcessOutputEvent(ProcessStream.STDOUT, b"attached\n")
            yield ProcessStatusEvent(terminal)

        def write_stdin(self, _data: bytes) -> None:
            return None

        def close_stdin(self) -> None:
            return None

        def resize(self, _rows: int, _columns: int) -> None:
            return None

        def signal(self, _requested: ProcessSignal) -> None:
            return None

        def wait(self) -> ProcessExit:
            return terminal

        def close(self) -> None:
            pytest.fail("successful attached bridge closed its session")

    session = Session()
    record = ExistingRunRecord(
        "attached-run",
        "862ffb44-6795-4618-b2d8-c0750439fac3",
        2,
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )
    request = object()
    monkeypatch.setattr(cli, "_resolve_runtime_stack", lambda *_args, **_kwargs: stack)
    monkeypatch.setattr(cli.runtime_dispatch, "resolve_run_request", lambda *_args, **_kwargs: request)
    monkeypatch.setattr(cli.runtime_dispatch, "preflight_run_request", lambda candidate: candidate)
    monkeypatch.setattr(
        cli.runtime_dispatch,
        "run",
        lambda candidate, *, preflight, roots: (
            RunResult(record, "exited", True, RunAttachmentMode.ATTACHED, session=session)
            if candidate is request and preflight is request
            else pytest.fail("attached request binding changed")
        ),
    )

    assert cli.main(["run", str(image), "--name", "attached-run"]) == 7
    assert capsys.readouterr().out == "attached\n"


def test_cli_run_nonready_result_fails_without_printing_a_launch_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "base.qcow2"
    image.write_bytes(b"cloud")
    stack = StackRef(
        ImageRef("sha256:" + hashlib.sha256(b"cloud").hexdigest(), "qcow2", "x86_64", None, image),
        (),
    )
    record = ExistingRunRecord(
        "failed-run",
        "862ffb44-6795-4618-b2d8-c0750439fac3",
        2,
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )
    request = object()
    monkeypatch.setattr(cli, "_resolve_runtime_stack", lambda *_args, **_kwargs: stack)
    monkeypatch.setattr(cli.runtime_dispatch, "resolve_run_request", lambda *_args, **_kwargs: request)
    monkeypatch.setattr(cli.runtime_dispatch, "preflight_run_request", lambda candidate: candidate)
    monkeypatch.setattr(
        cli.runtime_dispatch,
        "run",
        lambda candidate, *, preflight, roots: (
            RunResult(record, "failed", False, RunAttachmentMode.DETACHED)
            if candidate is request and preflight is request
            else pytest.fail("non-ready request binding changed")
        ),
    )

    assert cli.main(["run", str(image), "--name", "failed-run"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runtime did not become ready (status: failed)" in captured.err


def test_shell_requires_local_terminal_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.runtime_dispatch, "shell", lambda *_args, **_kwargs: pytest.fail("shell dispatched"))

    assert cli.main(["shell", "demo"]) == 1
    assert "requires a local terminal" in capsys.readouterr().err


def test_cli_process_bridge_preserves_split_bytes_and_exact_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = ProcessExit(19, 19, None, ProcessExitCategory.EXITED)

    class Session:
        capabilities = ProcessCapabilities(False, False, False, True)

        def events(self):
            yield ProcessOutputEvent(ProcessStream.STDOUT, b"out\n")
            yield ProcessOutputEvent(ProcessStream.STDERR, b"err\n")
            yield ProcessStatusEvent(result)

        def write_stdin(self, _data: bytes) -> None:
            return None

        def close_stdin(self) -> None:
            return None

        def resize(self, _rows: int, _columns: int) -> None:
            return None

        def signal(self, _requested: ProcessSignal) -> None:
            return None

        def wait(self):
            return result

        def close(self) -> None:
            pytest.fail("successful bridge closed its session")

    assert cli._run_process_session(Session(), interactive=False) == 19
    captured = capsys.readouterr()
    assert captured.out == "out\n"
    assert captured.err == "err\n"


@pytest.mark.parametrize(
    ("events", "waited", "message"),
    [
        ((), ProcessExit(0, 0, None, ProcessExitCategory.EXITED), "no terminal"),
        (
            (
                ProcessStatusEvent(ProcessExit(0, 0, None, ProcessExitCategory.EXITED)),
                ProcessStatusEvent(ProcessExit(0, 0, None, ProcessExitCategory.EXITED)),
            ),
            ProcessExit(0, 0, None, ProcessExitCategory.EXITED),
            "duplicate terminal",
        ),
        (
            (
                ProcessStatusEvent(ProcessExit(0, 0, None, ProcessExitCategory.EXITED)),
                ProcessOutputEvent(ProcessStream.STDOUT, b"late"),
            ),
            ProcessExit(0, 0, None, ProcessExitCategory.EXITED),
            "output after terminal",
        ),
        (
            (ProcessStatusEvent(ProcessExit(0, 0, None, ProcessExitCategory.EXITED)),),
            ProcessExit(7, 7, None, ProcessExitCategory.EXITED),
            "does not match",
        ),
    ],
)
def test_cli_process_bridge_rejects_invalid_terminal_status_contract(
    events: tuple[object, ...],
    waited: ProcessExit,
    message: str,
) -> None:
    closed = False

    class Session:
        capabilities = ProcessCapabilities(False, False, False, False)

        def events(self):
            yield from events

        def write_stdin(self, _data: bytes) -> None:
            return None

        def close_stdin(self) -> None:
            return None

        def resize(self, _rows: int, _columns: int) -> None:
            return None

        def signal(self, _requested: ProcessSignal) -> None:
            pytest.fail("signal forwarding was not advertised")

        def wait(self):
            return waited

        def close(self) -> None:
            nonlocal closed
            closed = True

    with pytest.raises(PalimpsestError, match=message):
        cli._run_process_session(Session(), interactive=False)
    assert closed


def test_cli_process_bridge_installs_no_handlers_without_signal_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProcessExit(0, 0, None, ProcessExitCategory.EXITED)

    class Session:
        capabilities = ProcessCapabilities(False, False, False, False)

        def events(self):
            yield ProcessStatusEvent(result)

        def write_stdin(self, _data: bytes) -> None:
            return None

        def close_stdin(self) -> None:
            return None

        def resize(self, _rows: int, _columns: int) -> None:
            return None

        def signal(self, _requested: ProcessSignal) -> None:
            pytest.fail("signal forwarding was not advertised")

        def wait(self):
            return result

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli.signal_module, "signal", lambda *_args: pytest.fail("handler installed"))

    assert cli._run_process_session(Session(), interactive=False) == 0


@pytest.mark.parametrize(
    ("capabilities", "stream", "message"),
    [
        (ProcessCapabilities(True, True, False, False), ProcessStream.STDOUT, "TTY session returned a split"),
        (ProcessCapabilities(False, False, False, False), ProcessStream.PTY, "non-TTY session returned a PTY"),
    ],
)
def test_cli_process_bridge_rejects_stream_capability_mismatch(
    capabilities: ProcessCapabilities,
    stream: ProcessStream,
    message: str,
) -> None:
    result = ProcessExit(0, 0, None, ProcessExitCategory.EXITED)
    closed = False

    class Session:
        def __init__(self) -> None:
            self.capabilities = capabilities

        def events(self):
            yield ProcessOutputEvent(stream, b"must-not-render")
            yield ProcessStatusEvent(result)

        def write_stdin(self, _data: bytes) -> None:
            return None

        def close_stdin(self) -> None:
            return None

        def resize(self, _rows: int, _columns: int) -> None:
            return None

        def signal(self, _requested: ProcessSignal) -> None:
            return None

        def wait(self):
            return result

        def close(self) -> None:
            nonlocal closed
            closed = True

    with pytest.raises(PalimpsestError, match=message):
        cli._run_process_session(Session(), interactive=False)
    assert closed


def test_cli_process_bridge_restores_handlers_when_terminal_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProcessExit(0, 0, None, ProcessExitCategory.EXITED)
    signal_calls: list[tuple[int, object]] = []

    class Session:
        capabilities = ProcessCapabilities(True, True, False, True)

        def events(self):
            yield ProcessStatusEvent(result)

        def write_stdin(self, _data: bytes) -> None:
            return None

        def close_stdin(self) -> None:
            return None

        def resize(self, _rows: int, _columns: int) -> None:
            return None

        def signal(self, _requested: ProcessSignal) -> None:
            return None

        def wait(self):
            return result

        def close(self) -> None:
            return None

    class FakeInput:
        @staticmethod
        def fileno() -> int:
            return 81

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            del target, name, daemon

        def start(self) -> None:
            return None

        def join(self, *, timeout: float) -> None:
            assert timeout == 0.5

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(cli.sys, "stdin", FakeInput())
    monkeypatch.setattr(cli.termios, "tcgetattr", lambda _fd: ["saved"])
    monkeypatch.setattr(cli.tty, "setraw", lambda _fd: None)
    monkeypatch.setattr(
        cli.termios,
        "tcsetattr",
        lambda *_args: (_ for _ in ()).throw(OSError("restore failed")),
    )
    monkeypatch.setattr(cli.threading, "Thread", FakeThread)
    monkeypatch.setattr(cli.signal_module, "getsignal", lambda signum: f"prior-{signum}")
    monkeypatch.setattr(cli.signal_module, "signal", lambda signum, handler: signal_calls.append((signum, handler)))

    with pytest.raises(PalimpsestError, match="cannot restore local terminal"):
        cli._run_process_session(Session(), interactive=True)

    assert signal_calls[-2:] == [
        (cli.signal_module.SIGINT, f"prior-{cli.signal_module.SIGINT}"),
        (cli.signal_module.SIGTERM, f"prior-{cli.signal_module.SIGTERM}"),
    ]


def test_cli_process_bridge_closes_session_to_terminate_input_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProcessExit(0, 0, None, ProcessExitCategory.EXITED)

    class Session:
        capabilities = ProcessCapabilities(True, True, False, False)

        def __init__(self) -> None:
            self.closed = False

        def events(self):
            yield ProcessStatusEvent(result)

        def write_stdin(self, _data: bytes) -> None:
            return None

        def close_stdin(self) -> None:
            return None

        def resize(self, _rows: int, _columns: int) -> None:
            return None

        def signal(self, _requested: ProcessSignal) -> None:
            return None

        def wait(self):
            return result

        def close(self) -> None:
            self.closed = True

    session = Session()

    class FakeInput:
        @staticmethod
        def fileno() -> int:
            return 82

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            del target, name, daemon
            self.joins = 0

        def start(self) -> None:
            return None

        def join(self, *, timeout: float) -> None:
            assert timeout == 0.5
            self.joins += 1

        def is_alive(self) -> bool:
            return not session.closed

    monkeypatch.setattr(cli.sys, "stdin", FakeInput())
    monkeypatch.setattr(cli.termios, "tcgetattr", lambda _fd: ["saved"])
    monkeypatch.setattr(cli.termios, "tcsetattr", lambda *_args: None)
    monkeypatch.setattr(cli.tty, "setraw", lambda _fd: None)
    monkeypatch.setattr(cli.threading, "Thread", FakeThread)

    assert cli._run_process_session(session, interactive=True) == 0
    assert session.closed


def test_cli_dispatch_image_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    img_file = tmp_path / "test.qcow2"
    img_file.write_bytes(b"qcow2 header content")
    d = "sha256:" + hashlib.sha256(img_file.read_bytes()).hexdigest()
    ret = cli.main(["image", "verify", str(img_file), "--digest", d])
    assert ret == 0
    assert "ok" in capsys.readouterr().out


def test_cli_dispatch_ps(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    roots, rpaths = _write_cli_run_ledger(backend="kvm")
    record = state.read_run_state(rpaths)
    state.atomic_write_json(
        rpaths.state,
        {
            **record,
            "status": "running",
            "base": {"digest": "sha256:" + "a" * 64, "arch": "x86_64"},
            "layers": [
                {"digest": "sha256:" + "b" * 64, "target_dev": "vdb"},
                {"digest": "sha256:" + "c" * 64, "target_dev": "vdc"},
            ],
            "guest_ip": "192.168.122.50",
            "created_at": "2026-07-30T00:00:00Z",
        },
    )
    assert roots.runs.exists()
    ret = cli.main(["ps"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "demo-vm" in out
    assert "running" in out
    assert "192.168.122.50" in out


def _aligned_table_cells(output: str) -> list[list[str]]:
    """Check column starts, not a spacing snapshot or formatter implementation."""
    parsed = [list(re.finditer(r"\S.*?(?= {2,}|$)", line)) for line in output.splitlines()]
    assert len(parsed) > 1
    starts = [cell.start() for cell in parsed[0]]
    for row in parsed[1:]:
        assert [cell.start() for cell in row] == starts
    return [[cell.group() for cell in row] for row in parsed]


def test_cli_ps_aligns_long_and_short_names_without_changing_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    long_name = "afterglow-ag-f1823ba8-app-1"
    payload = {
        "warnings": ["test observation warning"],
        "vms": [
            {
                "name": name,
                "status": "running",
                "backend": "lima-vz",
                "managed": managed,
                "base_digest": "sha256:" + "a" * 64,
                "layers": [],
                "guest_ip": "192.168.5.15",
                "created_at": "2026-10-11T00:00:00Z",
                "ports": [{"host_ip": "127.0.0.1", "host_port": 18080, "guest_port": 8080, "protocol": "tcp"}],
            }
            for name, managed in ((long_name, True), ("db", False))
        ]
        + [{"name": "removed-name-longer-than-every-live-name", "status": "removed"}],
    }
    monkeypatch.setattr(cli.inventory, "list_vms", lambda _roots: payload)

    assert cli.main(["ps"]) == 0

    captured = capsys.readouterr()
    cells = _aligned_table_cells(captured.out)
    assert len(cells) == 3
    assert [row[0] for row in cells[1:]] == [long_name, "db"]
    assert captured.out.splitlines()[0].index("STATUS") == len(long_name) + 2
    assert [row[3] for row in cells[1:]] == ["managed", "observed"]
    for row in cells[1:]:
        assert row[4:7] == ["a" * 12, "0", "192.168.5.15"]
        assert row[-1] == "127.0.0.1:18080->8080/tcp"
    assert "test observation warning" in captured.err


@pytest.mark.parametrize("operation", ["volumes", "networks"])
def test_cli_resource_tables_align_names_preserve_values_and_json(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    long_name = "afterglow-ag-f1823ba8-app-1-writable-disk"
    fields = (
        {"size_bytes": 0, "file_size_bytes": 4096, "allocated_bytes": None}
        if operation == "volumes"
        else {"mode": "shared", "subnet": "192.168.105.0/24", "gateway": None}
    )
    payload = {
        "warnings": [],
        operation: [
            {
                "name": name,
                "backend": "lima-vz",
                "status": "attached",
                "attachments": [{"name": "afterglow-ag-f1823ba8-app-1"}],
                **fields,
            }
            for name in (long_name, "db")
        ],
    }
    monkeypatch.setattr(cli.inventory, "list_" + operation, lambda _roots: payload)

    assert cli.main([operation]) == 0

    cells = _aligned_table_cells(capsys.readouterr().out)
    assert [row[0] for row in cells[1:]] == [long_name, "db"]
    for row in cells[1:]:
        assert row[3:6] == (["0", "4096", "-"] if operation == "volumes" else ["shared", "192.168.105.0/24", "-"])
        assert row[-1] == "afterglow-ag-f1823ba8-app-1"

    assert cli.main([operation, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_cli_compose_ps_aligns_long_names_and_preserves_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    from palimpsest_local.project_runtime import ProjectServiceStatus

    (tmp_path / "palimpsest.yml").write_text(
        'version: "1"\nname: afterglow\nservices:\n'
        f"  app:\n    image: sha256:{'a' * 64}\n"
        f"  db:\n    image: sha256:{'a' * 64}\n",
        encoding="utf-8",
    )
    rows = (
        ProjectServiceStatus(
            "application-service-longer-than-the-old-column", "afterglow-ag-f1823ba8-app-1", "running"
        ),
        ProjectServiceStatus("db", "db", "stopped"),
    )
    monkeypatch.setattr(cli, "_compose_callbacks", lambda *_args: SimpleNamespace(inspect=lambda _name: None))
    monkeypatch.setattr(cli, "project_ps", lambda *_args, **_kwargs: rows)
    argv = ["compose", "--project-directory", str(tmp_path), "ps"]

    assert cli.main(argv) == 0

    cells = _aligned_table_cells(capsys.readouterr().out)
    assert [row[1] for row in cells[1:]] == [row.run_name for row in rows]
    assert [row[2] for row in cells[1:]] == ["running", "stopped"]

    assert cli.main([*argv, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"service": row.service, "name": row.run_name, "status": row.status} for row in rows
    ]


def test_cli_ps_missing_state_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "missing-config"
    state_home = tmp_path / "missing-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert cli.main(["ps"]) == 0

    assert "NAME" in capsys.readouterr().out
    assert not config_home.exists()
    assert not state_home.exists()


def test_cli_ps_reports_corrupt_ledger_without_reflecting_its_contents(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _roots, rpaths = _write_cli_run_ledger(backend="kvm")
    rpaths.state.write_text('{"status":"SENSITIVE_VALUE"}\n', encoding="utf-8")

    assert cli.main(["ps"]) == 0

    captured = capsys.readouterr()
    assert "demo-vm" not in captured.out
    assert "SENSITIVE_VALUE" not in captured.out + captured.err


def test_cli_dispatch_image_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("PALIMPSEST_URL", "http://localhost:8080")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")
    monkeypatch.setattr(
        "palimpsest_local.cli.HubClient.list_images",
        lambda self, **kwargs: [
            {"digest": "sha256:" + "a" * 64, "name": "ubuntu-24.04", "disk_format": "qcow2", "arch": "x86_64"}
        ],
    )
    ret = cli.main(["image", "ls"])
    assert ret == 0
    assert "ubuntu-24.04" in capsys.readouterr().out


def test_cli_dispatch_layer_ls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("PALIMPSEST_URL", "http://localhost:8080")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")
    monkeypatch.setattr(
        "palimpsest_local.cli.HubClient.list_layers",
        lambda self, **kwargs: [{"digest": "sha256:" + "b" * 64, "name": "python-layer", "kind": "squashfs"}],
    )
    ret = cli.main(["layer", "ls"])
    assert ret == 0
    assert "python-layer" in capsys.readouterr().out


def test_cli_dispatch_stop_and_rm(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    stopped = []
    removed = []
    monkeypatch.setattr(runtime_dispatch, "stop", lambda name, roots=None: stopped.append(name))
    monkeypatch.setattr(
        runtime_dispatch,
        "rm",
        lambda name, roots=None, volumes=False: removed.append((name, volumes)),
    )

    assert cli.main(["stop", "demo-vm"]) == 0
    assert stopped == ["demo-vm"]
    assert "stopped demo-vm" in capsys.readouterr().out

    assert cli.main(["rm", "demo-vm", "--volumes"]) == 0
    assert removed == [("demo-vm", True)]
    assert "removed demo-vm" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("backend", "adapter_name", "expected_backend"),
    [
        ("kvm", "cloud_runtime", RuntimeBackend.KVM),
        ("libvirt-hvf", "cloud_runtime", RuntimeBackend.LIBVIRT_HVF),
        ("lima-vz", "lima", RuntimeBackend.LIMA_VZ),
        ("lima-krunkit", "lima", RuntimeBackend.LIMA_KRUNKIT),
    ],
)
@pytest.mark.parametrize(("operation", "target_name", "argv", "expected_kwargs"), _CLI_EXISTING_RUN_OPERATIONS)
def test_cli_existing_run_operations_route_only_through_durable_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: str,
    adapter_name: str,
    expected_backend: RuntimeBackend,
    operation: str,
    target_name: str,
    argv: list[str],
    expected_kwargs: dict[str, object],
) -> None:
    roots, rpaths = _write_cli_run_ledger(backend=backend)
    calls: list[tuple[str, dict[str, object]]] = []

    def selected(name: str, **kwargs: object) -> object:
        calls.append((name, kwargs))
        if target_name == "logs":
            return iter(("first\n", "second\n"))
        if target_name == "inspect_run":
            return {"owner": {"name": name}, "state": {"status": "stopped"}}
        expected = kwargs["_expected_record"]
        assert isinstance(expected, ExistingRunRecord)
        operation_roots = kwargs["roots"]
        assert isinstance(operation_roots, state.StatePaths)
        expected_snapshot = kwargs["_expected_snapshot"]
        assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
        with state.locked_existing_run(
            operation_roots,
            name,
            expected=expected,
            expected_snapshot=expected_snapshot,
        ) as mutation:
            terminal = "running" if target_name == "start" else "removed" if target_name == "rm" else "stopped"
            initial = mutation.initial_snapshot
            written = (
                mutation.mutable_state()
                if initial.state["status"] == terminal and target_name != "rm"
                else mutation.write_state(terminal, mutation.mutable_state())
            )
            outcome = _issue_lifecycle_adapter_outcome(
                mutation.record,
                initial.state["status"],
                state.lifecycle_revision(initial),
                terminal,
                state.lifecycle_revision(written),
            )
            if target_name == "rm":
                mutation.delete_run_tree()
            return outcome

    selected_adapter = getattr(runtime_dispatch, adapter_name)
    other_adapter = runtime_dispatch.lima if adapter_name == "cloud_runtime" else runtime_dispatch.cloud_runtime
    monkeypatch.setattr(selected_adapter, target_name, selected)
    monkeypatch.setattr(
        other_adapter,
        target_name,
        lambda *_args, **_kwargs: pytest.fail("dispatcher selected the wrong runtime adapter"),
    )
    assert cli.main(argv) == 0

    output = capsys.readouterr().out
    if operation == "inspect":
        assert calls == []
        run_id = json.loads(rpaths.owner.read_text(encoding="utf-8"))["run_id"]
        assert json.loads(output) == {
            "schema_version": 1,
            "state_schema_version": 2,
            "owner": {"schema_version": 1, "name": "demo-vm", "run_id": run_id},
            "identity": {"runtime_kind": "cloud-image", "backend": backend},
            "lifecycle": {
                "status": "stopped",
                "lifecycle_revision": 0,
                "created_at": None,
                "updated_at": None,
            },
            "detail": {
                "type": "cloud-image",
                "base": {"digest": None, "arch": None, "disk_format": None},
                "layers": [],
                "memory_mib": None,
                "vcpus": None,
                "network": None,
                "ports": [],
                "volumes": [],
                "ssh": {"host": None, "port": 22},
                "guest_ip": None,
            },
            "warnings": [],
        }
        return
    assert len(calls) == 1
    called_name, called_kwargs = calls[0]
    assert called_name == "demo-vm"
    record = called_kwargs.pop("_expected_record")
    expected_snapshot = called_kwargs.pop("_expected_snapshot", None)
    assert isinstance(record, ExistingRunRecord)
    assert record.dispatch_key.backend is expected_backend
    if operation in {"start", "stop", "rm"}:
        assert isinstance(expected_snapshot, state.RunLedgerSnapshot)
    assert called_kwargs == {"roots": roots, **expected_kwargs}
    if operation == "logs":
        assert output == "first\nsecond\n"
    else:
        past_tense = {"start": "started", "stop": "stopped", "rm": "removed"}[operation]
        assert output == f"{past_tense} demo-vm\n"


def test_cli_inspect_json_does_not_serialize_internal_ledger_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _roots, rpaths = _write_cli_run_ledger(backend="kvm")
    payload = json.loads(rpaths.state.read_text(encoding="utf-8"))
    payload.update(
        {
            "base": {
                "digest": "sha256:" + "a" * 64,
                "arch": "x86_64",
                "disk_format": "qcow2",
                "local_path": "/private/SENSITIVE_VALUE/base.qcow2",
            },
            "domain_uuid": "SENSITIVE_VALUE",
            "environment": {"API_KEY": "SENSITIVE_VALUE"},
            "cleanup_flags": {"internal": "SENSITIVE_VALUE"},
            "error": "SENSITIVE_VALUE",
            "ssh_config_file": "/private/SENSITIVE_VALUE/ssh.config",
        }
    )
    rpaths.state.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert cli.main(["inspect", "demo-vm"]) == 0

    rendered = capsys.readouterr().out
    assert "SENSITIVE_VALUE" not in rendered
    assert "local_path" not in rendered
    assert "domain_uuid" not in rendered
    assert "environment" not in rendered
    assert "cleanup_flags" not in rendered
    assert "error" not in rendered
    assert "ssh_config_file" not in rendered


def test_cli_inspect_serializes_oci_root_published_ports(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_cli_run_ledger(backend="kvm", runtime_kind="oci-root")
    inspected = _oci_inspect_record()
    monkeypatch.setattr(runtime_dispatch, "inspect_run", lambda *_args, **_kwargs: inspected)

    assert cli.main(["inspect", "demo-vm"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["identity"] == {"runtime_kind": "oci-root", "backend": "kvm"}
    assert payload["detail"] == {
        "type": "oci-root",
        "memory_mib": 4096,
        "vcpus": 4,
        "network": "nat",
        "ports": [
            {"host_ip": "127.0.0.1", "host_port": 18080, "guest_port": 8080, "protocol": "tcp"},
            {"host_ip": "0.0.0.0", "host_port": 18443, "guest_port": 8443, "protocol": "udp"},
        ],
        "guest_ip": "10.0.2.15",
    }


def test_cli_ps_prints_each_published_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state.init_roots()
    inspected = _oci_inspect_record()
    details = MappingProxyType(
        {
            "base_digest": "",
            "base_arch": "",
            "layers": (),
            "memory_mib": 4096,
            "vcpus": 4,
            "network": "nat",
            "ports": tuple(
                MappingProxyType(
                    {
                        "host_ip": port.host_ip,
                        "host_port": port.host_port,
                        "guest_port": port.guest_port,
                        "protocol": port.protocol,
                    }
                )
                for port in inspected.detail.ports
            ),
            "volumes": (),
            "ssh": MappingProxyType({"host": None, "port": 22}),
            "guest_ip": "10.0.2.15",
            "created_at": "2026-09-16T00:00:00Z",
            "updated_at": "2026-09-16T00:01:00Z",
        }
    )
    aggregation = RunAggregationResult((RunSummary(inspected.record, "running", details, stale=True),), ())
    monkeypatch.setattr(runtime_dispatch, "reconcile", lambda **_kwargs: aggregation)

    assert cli.main(["ps"]) == 0

    output = capsys.readouterr().out
    assert "PORTS" in output
    assert "127.0.0.1:18080->8080/tcp" in output
    assert "0.0.0.0:18443->8443/udp" in output


@pytest.mark.parametrize(
    ("operation", "target_name", "argv", "_expected_kwargs"),
    [item for item in _CLI_EXISTING_RUN_OPERATIONS if item[0] not in {"stop", "rm", "inspect"}],
)
def test_cli_oci_root_existing_operations_fail_typed_before_backend_subprocess_or_file_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    target_name: str,
    argv: list[str],
    _expected_kwargs: dict[str, object],
) -> None:
    roots, _rpaths = _write_cli_run_ledger(backend="kvm", runtime_kind="oci-root")
    before = _snapshot_cli_state(roots.state)
    effects: list[str] = []

    def forbidden(effect: str) -> None:
        effects.append(effect)
        pytest.fail(f"side effect reached: {effect}")

    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime,
        target_name,
        lambda *_args, **_kwargs: forbidden("cloud-backend"),
    )
    monkeypatch.setattr(
        runtime_dispatch.lima,
        target_name,
        lambda *_args, **_kwargs: forbidden("lima-backend"),
    )
    monkeypatch.setattr(cli.subprocess, "run", lambda *_args, **_kwargs: forbidden("subprocess"))
    assert cli.main(argv) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"runtime operation '{operation}' is unavailable for oci-root/kvm\n"
    assert effects == []
    assert _snapshot_cli_state(roots.state) == before


@pytest.mark.parametrize(("_operation", "_target_name", "argv", "_expected_kwargs"), _CLI_EXISTING_RUN_OPERATIONS)
def test_cli_missing_existing_run_fails_closed_without_creating_state_roots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    _operation: str,
    _target_name: str,
    argv: list[str],
    _expected_kwargs: dict[str, object],
) -> None:
    missing_argv = ["missing" if item == "demo-vm" else item for item in argv]

    assert cli.main(missing_argv) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "cannot securely read run ledger\n"
    assert not (tmp_path / "xdg-config").exists()
    assert not (tmp_path / "xdg-state").exists()


@pytest.mark.parametrize(("_operation", "_target_name", "argv", "_expected_kwargs"), _CLI_EXISTING_RUN_OPERATIONS)
def test_cli_corrupt_existing_run_fails_closed_without_rewrite_or_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _operation: str,
    _target_name: str,
    argv: list[str],
    _expected_kwargs: dict[str, object],
) -> None:
    roots, rpaths = _write_cli_run_ledger(backend="kvm")
    rpaths.state.write_text('{"schema_version":"secret-invalid-schema"}\n', encoding="utf-8")
    before = _snapshot_cli_state(roots.state)
    assert cli.main(argv) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid run state schema\n"
    assert "secret-invalid-schema" not in captured.err
    assert _snapshot_cli_state(roots.state) == before


def test_cli_dispatch_build_routes_verified_spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    base_bytes = b"base"
    base_digest = f"sha256:{hashlib.sha256(base_bytes).hexdigest()}"
    store = ContentStore(tmp_path / "state" / "palimpsest" / "store")
    store.root.parent.mkdir(parents=True)
    store.write_stream([base_bytes], expected_digest=base_digest)
    store.write_metadata(
        base_digest,
        {"kind": "cloud-image", "disk_format": "qcow2", "arch": "x86_64", "os_variant": None},
    )
    recipe = tmp_path / "Palimpsestfile"
    recipe.write_text(f"FROM {base_digest}\nRUN true\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        "palimpsest_local.cli.build_layer",
        lambda spec, *, roots: (captured.append((spec, roots)), {"output_digest": "sha256:" + "a" * 64})[1],
    )

    assert cli.main(["build", "--base", base_digest, "--tag", "result", "--network", "default", "-f", str(recipe)]) == 0
    assert captured[0][0].base.digest == base_digest
    assert captured[0][0].network == "default"
    assert "sha256:" in capsys.readouterr().out


def test_cli_dispatch_commit_routes_runtime(monkeypatch: pytest.MonkeyPatch, capsys):
    roots, rpaths = _write_cli_run_ledger(backend="kvm")
    monkeypatch.setattr(cli, "init_roots", lambda: roots)
    record = state.read_run_dispatch_record(roots, rpaths.root.name)
    monkeypatch.setattr(
        cli.runtime_dispatch,
        "commit",
        lambda name, tag, *, roots: CommitResult(
            record,
            tag,
            "sha256:" + "b" * 64,
            4,
            None,
            "sha256:" + "a" * 64,
        ),
    )
    assert cli.main(["commit", "demo-vm", "--tag", "delta"]) == 0
    assert "sha256:" in capsys.readouterr().out


def test_cli_dispatch_build_integrity_guard(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    recipe = tmp_path / "Palimpsestfile"
    recipe.write_text(f"FROM sha256:{'a' * 64}\nRUN echo hi\n")
    ret = cli.main(["build", "--base", "sha256:" + "b" * 64, "--tag", "my-layer", "-f", str(recipe)])
    assert ret == 1
    err = capsys.readouterr().err
    assert "base digest mismatch" in err


@pytest.mark.parametrize("stored_arch, requested_arch", [(None, None), ("aarch64", "x86_64"), ("x86_64", "aarch64")])
def test_image_push_refuses_unknown_or_conflicting_architecture_before_transport(
    tmp_path, monkeypatch, stored_arch, requested_arch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    image = tmp_path / "image.raw"
    image.write_bytes(b"declared target fixture")
    roots = cli.init_roots()
    store = ContentStore(roots.store)
    digest = cli.digest_file(image)
    before = None
    if stored_arch is not None:
        store.ingest_file(image, expected_digest=digest)
        store.write_metadata(digest, {"kind": "cloud-image", "disk_format": "raw", "arch": stored_arch})
        before = store.read_metadata(digest)
    monkeypatch.setattr(cli, "HubClient", lambda *a, **kw: pytest.fail("publication preceded architecture validation"))
    arguments = ["image", "push", str(image), "--name", "target-fixture", "--disk-format", "raw"]
    if requested_arch is not None:
        arguments.extend(["--arch", requested_arch])
    assert cli.main(arguments) == 1
    if before is not None:
        assert store.read_metadata(digest) == before


def test_cli_dispatch_run_passes_cli_layers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    import hashlib

    base_content = b"base qcow2 content"
    layer_content = b"layer squashfs content"
    base_hex = hashlib.sha256(base_content).hexdigest()
    layer_hex = hashlib.sha256(layer_content).hexdigest()
    base_digest = f"sha256:{base_hex}"
    layer_digest = f"sha256:{layer_hex}"

    store = ContentStore(tmp_path / "state" / "palimpsest" / "store")
    store.root.parent.mkdir(parents=True)
    store.write_stream([base_content], expected_digest=base_digest)
    store.write_stream([layer_content], expected_digest=layer_digest)
    store.write_metadata(
        base_digest,
        {
            "kind": "cloud-image",
            "disk_format": "qcow2",
            "arch": "x86_64",
            "os_variant": None,
        },
    )
    store.write_metadata(
        layer_digest,
        {
            "kind": "squashfs",
            "media_type": "application/vnd.afterglow.palimpsest.layer.squashfs.v1",
            "parent_digest": None,
            "base_image_digest": base_digest,
        },
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(runtime_dispatch.platforms, "select_backend", lambda arch, requested="auto": "kvm")
    preflight = object()
    pinned_roots = cli.resolve_roots()
    redirected_state = tmp_path / "redirected-after-preflight"

    def preflight_and_replace_config(_request: object) -> object:
        pinned_roots.config.mkdir(parents=True, exist_ok=True)
        (pinned_roots.config / "config.toml").write_text(
            f'[storage]\nstate_root = "{redirected_state}"\n',
            encoding="utf-8",
        )
        return preflight

    monkeypatch.setattr(cli.runtime_dispatch, "preflight_run_request", preflight_and_replace_config)
    recorded_specs: list[RunSpec] = []
    passed_roots: list[state.StatePaths] = []
    monkeypatch.setattr(
        cli.runtime_dispatch,
        "run",
        lambda request, preflight=None, roots=None: (
            (
                recorded_specs.append(request.spec),
                passed_roots.append(roots),
                _fake_run_result(request.spec.name, RuntimeBackend.KVM, "10.0.0.5"),
            )[2]
            if preflight is not None
            else pytest.fail("CLI omitted preflight report")
        ),
    )

    ret = cli.main(["run", base_digest, "--name", "test-vm", "--layer", layer_digest])
    assert ret == 0
    assert len(recorded_specs) == 1
    spec = recorded_specs[0]
    assert spec.name == "test-vm"
    assert len(spec.stack.layers) == 1
    assert spec.stack.layers[0].digest == layer_digest
    assert passed_roots == [pinned_roots]
    assert not redirected_state.exists()
    assert capsys.readouterr().out.strip() == "10.0.0.5"


def test_remote_run_capability_failure_precedes_pull_or_store_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    digest_value = "sha256:" + "d" * 64
    store = ContentStore(tmp_path / "not-created-store")
    pulls: list[Path] = []
    monkeypatch.setenv("PALIMPSEST_URL", "https://hub.invalid")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")
    monkeypatch.setattr(
        cli.HubClient,
        "get_layer",
        lambda _self, _digest: {
            "kind": "cloud-image",
            "disk_format": "qcow2",
            "arch": "x86_64",
        },
    )
    monkeypatch.setattr(
        cli.HubClient,
        "pull_blob",
        lambda _self, _digest, destination: pulls.append(destination) or pytest.fail("pull reached"),
    )
    requested: list[tuple[str, str, str]] = []

    def reject(arch: str, *, requested_backend: str, network: str) -> None:
        requested.append((arch, requested_backend, network))
        raise RuntimePreflightError(CapabilityErrorCategory.MISSING, "host capability unavailable")

    monkeypatch.setattr(cli.runtime_dispatch, "preflight_run_capabilities", reject)

    with pytest.raises(RuntimePreflightError, match="host capability unavailable"):
        cli._resolve_image_ref(
            store,
            digest_value,
            None,
            requested_backend="kvm",
            preflight_for_run=True,
            run_network="private",
        )

    assert requested == [("x86_64", "kvm", "private")]
    assert pulls == []
    assert not store.root.exists()


def test_remote_hvf_routed_network_rejection_precedes_pull_mkdir_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    digest_value = "sha256:" + "e" * 64
    store = ContentStore(tmp_path / "must-not-exist")
    effects: list[str] = []
    monkeypatch.setenv("PALIMPSEST_URL", "https://hub.invalid")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")
    monkeypatch.setattr(
        cli.HubClient,
        "get_layer",
        lambda _self, _digest: {
            "kind": "cloud-image",
            "disk_format": "qcow2",
            "arch": "aarch64",
        },
    )
    monkeypatch.setattr(
        cli.HubClient,
        "pull_blob",
        lambda *_args, **_kwargs: effects.append("pull") or pytest.fail("pull reached"),
    )
    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "select_backend",
        lambda *_args, **_kwargs: "libvirt-hvf",
    )
    monkeypatch.setattr(
        store,
        "write_metadata",
        lambda *_args, **_kwargs: effects.append("metadata") or pytest.fail("metadata write reached"),
    )

    with pytest.raises(StateError, match="only none or default"):
        cli._resolve_image_ref(
            store,
            digest_value,
            None,
            requested_backend="libvirt-hvf",
            preflight_for_run=True,
            run_network="routed",
        )

    assert effects == []
    assert not store.root.exists()


def test_cli_dispatch_image_pull_fresh_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    payload = b"downloaded blob content"
    d = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PALIMPSEST_URL", "http://localhost:8080")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "test-token")

    monkeypatch.setattr(
        "palimpsest_local.cli.HubClient.get_layer",
        lambda self, digest: {"digest": d, "kind": "cloud-image", "disk_format": "qcow2", "arch": "x86_64"},
    )

    pulled_destinations = []

    def mock_pull(self, digest, destination, resume=True):
        pulled_destinations.append(destination)
        destination.write_bytes(payload)
        return destination

    monkeypatch.setattr("palimpsest_local.cli.HubClient.pull_blob", mock_pull)

    ret = cli.main(["image", "pull", d])
    assert ret == 0
    assert len(pulled_destinations) == 1
    store = ContentStore(tmp_path / "state" / "palimpsest" / "store")
    assert store.verify_blob(d) == len(payload)
    assert pulled_destinations[0] != store.blob_path(d)


def test_cli_image_import_ingests_verified_cloud_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    source = tmp_path / "ubuntu-arm64.img"
    payload = b"ubuntu-arm64-cloud-image"
    source.write_bytes(payload)
    expected_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    monkeypatch.setattr(cli, "init_roots", lambda: roots)

    assert cli.main(["image", "import", str(source), "--disk-format", "qcow2", "--arch", "aarch64"]) == 0

    store = ContentStore(roots.store)
    assert store.read_metadata(expected_digest)["arch"] == "aarch64"


def test_layer_push_preserves_runtime_pack_chain_and_arch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    store = ContentStore(roots.store)
    payload = b"hsqs-runtime-pack"
    source = tmp_path / "runtime.squashfs"
    source.write_bytes(payload)
    runtime_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    pack_key = "sha256:" + "9" * 64
    base_digest = "sha256:" + "8" * 64
    store.ingest_file(source, expected_digest=runtime_digest)
    store.write_metadata(
        runtime_digest,
        {
            "kind": "squashfs",
            "media_type": cli.MEDIA_TYPE_LAYER_SQUASHFS,
            "parent_digest": None,
            "base_image_digest": base_digest,
            "runtime_pack_manifest_digest": pack_key,
            "arch": "aarch64",
        },
    )
    cli.write_tag_record(
        roots,
        cli.TagRecord(
            schema_version=1,
            tag="runtime-arm64",
            digest=runtime_digest,
            media_type=cli.MEDIA_TYPE_LAYER_SQUASHFS,
            size_bytes=len(payload),
            parent_digest=None,
            base_image_digest=base_digest,
            source="buildkit-runtime-pack",
            created_at="2026-08-19T00:00:00Z",
        ),
    )
    monkeypatch.setattr(cli, "init_roots", lambda: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    monkeypatch.setenv("PALIMPSEST_URL", "http://hub.invalid")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "token")
    pushed: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli.HubClient,
        "push_blob",
        lambda _self, _path, metadata: (pushed.append(metadata), {"blob_digest": runtime_digest})[1],
    )

    assert cli.main(["layer", "push", "runtime-arm64"]) == 0
    assert pushed[0]["chain_id"] == pack_key
    assert pushed[0]["arch"] == "aarch64"
    assert pushed[0]["base_image_digest"] == base_digest


def test_cli_rejects_commit_for_lima_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    del tmp_path
    roots, _rpaths = _write_cli_run_ledger(backend="lima-vz")
    monkeypatch.setattr(cli, "init_roots", lambda: roots)

    assert cli.main(["commit", "demo-vm", "--tag", "layer"]) == 1
    assert "runtime operation 'commit' is unavailable for cloud-image/lima-vz" in capsys.readouterr().err


def test_cli_run_parser_defers_backend_network_validation_to_dispatcher():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["run", "sha256:" + "a" * 64, "--name", "vm1", "--backend", "libvirt-hvf", "--network", "routed"]
    )
    cli._validate_args(args, parser)
    assert args.network == "routed"


@pytest.mark.parametrize("backend", [RuntimeBackend.LIBVIRT_HVF, RuntimeBackend.LIMA_KRUNKIT])
def test_cli_run_dispatch_experimental_warning_uses_result_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: RuntimeBackend
):
    import hashlib

    content = b"dummy image data hvf"
    hex_digest = hashlib.sha256(content).hexdigest()
    base_digest = f"sha256:{hex_digest}"
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    store = ContentStore(roots.store)
    store.write_stream([content], expected_digest=base_digest)
    store.write_metadata(
        base_digest,
        {"kind": "cloud-image", "disk_format": "qcow2", "arch": "aarch64", "os_variant": None},
    )
    monkeypatch.setattr(cli, "init_roots", lambda: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    selected = []
    preflighted = []
    run_calls = []

    monkeypatch.setattr(
        runtime_dispatch.platforms,
        "select_backend",
        lambda arch, requested="auto": (selected.append((arch, requested)), backend.value)[1],
    )

    token = object()

    def preflight(request) -> object:
        assert capsys.readouterr().err == ""
        preflighted.append(request.dispatch_key.backend.value)
        return token

    def run(request, *, preflight, roots=None):
        assert capsys.readouterr().err == ""
        assert preflight is token
        run_calls.append(request)
        return _fake_run_result(request.spec.name, backend, None)

    monkeypatch.setattr(cli.runtime_dispatch, "preflight_run_request", preflight)
    monkeypatch.setattr(
        cli.runtime_dispatch,
        "run",
        run,
    )

    ret = cli.main(["run", base_digest, "--name", "experimental-vm", "--backend", backend.value])
    assert ret == 0
    assert selected == [("aarch64", backend.value)]
    assert preflighted == [backend.value]
    assert len(run_calls) == 1
    assert run_calls[0].dispatch_key.backend is backend
    captured = capsys.readouterr()
    assert captured.err.strip() == f"warning: {backend.value} is experimental"
    assert captured.out.strip() == ("limactl shell experimental-vm" if backend.is_lima else "None")


@pytest.mark.parametrize("backend", [RuntimeBackend.LIMA_VZ, RuntimeBackend.LIMA_KRUNKIT])
def test_cli_run_dispatch_lima_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    backend: RuntimeBackend,
):
    import hashlib

    content = b"dummy image data lima"
    hex_digest = hashlib.sha256(content).hexdigest()
    base_digest = f"sha256:{hex_digest}"
    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    store = ContentStore(roots.store)
    store.write_stream([content], expected_digest=base_digest)
    store.write_metadata(
        base_digest,
        {"kind": "cloud-image", "disk_format": "qcow2", "arch": "aarch64", "os_variant": None},
    )
    monkeypatch.setattr(cli, "init_roots", lambda: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    run_requests = []

    monkeypatch.setattr(runtime_dispatch.platforms, "select_backend", lambda arch, requested="auto": backend.value)
    token = object()
    monkeypatch.setattr(cli.runtime_dispatch, "preflight_run_request", lambda _request: token)
    monkeypatch.setattr(
        cli.runtime_dispatch,
        "run",
        lambda request, preflight=None, roots=None: (
            (
                run_requests.append(request),
                _fake_run_result(request.spec.name, backend, "192.0.2.10"),
            )[1]
            if preflight is token
            else pytest.fail("CLI omitted preflight report")
        ),
    )

    ret = cli.main(["run", base_digest, "--name", "lima-vm", "--backend", backend.value])
    assert ret == 0
    assert len(run_requests) == 1
    assert run_requests[0].dispatch_key.backend is backend
    assert capsys.readouterr().out.strip() == "limactl shell lima-vm"


def test_cli_web_port_validation(capsys: pytest.CaptureFixture[str]):
    parser = cli.build_parser()
    args = parser.parse_args(["web", "--port", "500"])
    with pytest.raises(SystemExit) as exc_info:
        cli._validate_args(args, parser)
    assert exc_info.value.code == 2
    assert "--port must be 0 or between 1024 and 65535" in capsys.readouterr().err

    args_0 = parser.parse_args(["web", "--port", "0"])
    cli._validate_args(args_0, parser)

    args_8080 = parser.parse_args(["web", "--port", "8080"])
    cli._validate_args(args_8080, parser)


def test_cli_dispatch_store_show(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(
        "palimpsest_local.cli.inventory.storage_report",
        lambda roots: {
            "state_root": "/tmp/state",
            "source": "default",
            "directories": {"store": 100, "runs": 0},
            "total_state_bytes": 100,
            "free_bytes": 500,
            "total_bytes": 1000,
        },
    )
    ret_table = cli.main(["store", "show"])
    assert ret_table == 0
    out_table = capsys.readouterr().out
    assert "/tmp/state" in out_table
    assert "default" in out_table

    ret_json = cli.main(["store", "show", "--format", "json"])
    assert ret_json == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["state_root"] == "/tmp/state"


def test_cli_dispatch_store_rm(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(
        "palimpsest_local.cli.inventory.remove_artifact",
        lambda roots, digest, force=False: {
            "digest": digest,
            "removed_tags": ["tag1"],
            "freed_bytes": 1024,
        },
    )
    ret = cli.main(["store", "rm", "sha256:" + "a" * 64, "--force"])
    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["digest"] == "sha256:" + "a" * 64
    assert data["removed_tags"] == ["tag1"]


def test_cli_dispatch_store_move(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    called_dest: list[Path] = []
    monkeypatch.setattr(
        "palimpsest_local.cli.inventory.move_state_root",
        lambda roots, destination, keep_source=False: (
            called_dest.append(Path(destination))
            or {"status": "ok", "state_root": str(destination), "source": "config"}
        ),
    )
    ret = cli.main(["store", "move", "--to", "/tmp/new-root", "--keep-source"])
    assert ret == 0
    assert called_dest == [Path("/tmp/new-root")]
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["status"] == "ok"


def test_cli_dispatch_store_set(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    called_dest: list[Path] = []
    monkeypatch.setattr(
        "palimpsest_local.cli.inventory.set_state_root",
        lambda roots, destination: (
            called_dest.append(Path(destination))
            or {"status": "ok", "state_root": str(destination), "source": "config"}
        ),
    )
    ret = cli.main(["store", "set", "--to", "/tmp/new-root"])
    assert ret == 0
    assert called_dest == [Path("/tmp/new-root")]
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["status"] == "ok"


def test_cli_run_without_name_or_kind_auto_names_and_resolves_local_buildkit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    from types import SimpleNamespace

    from palimpsest_local import run_source

    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "init_roots", lambda *a, **k: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda *a, **k: roots)

    # Create dummy OCI archive and BuildKit record
    archive_dir = tmp_path / "build-output"
    archive_dir.mkdir(parents=True)
    archive_file = archive_dir / "app.oci.tar"
    archive_content = b"fake-oci-archive-tar-bytes"
    archive_file.write_bytes(archive_content)
    archive_digest = f"sha256:{hashlib.sha256(archive_content).hexdigest()}"
    manifest_digest = f"sha256:{hashlib.sha256(b'fake-manifest').hexdigest()}"

    build_dir = roots.builds / "bk-123456abcdef"
    build_dir.mkdir(parents=True)
    build_record = {
        "build_id": "bk-123456abcdef",
        "engine": "buildkit",
        "status": "success",
        "output_tags": ["myorg/demo-service:v2"],
        "output_path": str(archive_file),
        "output_oci_archive_digest": archive_digest,
        "output_oci_manifest_digest": manifest_digest,
    }
    (build_dir / "record.json").write_text(json.dumps(build_record), encoding="utf-8")

    dispatched: list[object] = []
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **kw: (
            dispatched.append(request)
            or SimpleNamespace(record=SimpleNamespace(name=request.name), terminal=None, session=None)
        ),
    )
    monkeypatch.setattr(cli, "_resolve_runtime_stack", lambda *a, **k: pytest.fail("cloud stack entered"))

    # Run without --name and without --runtime-kind
    ret = cli.main(["run", "myorg/demo-service:v2", "-d"])
    assert ret == 0
    assert len(dispatched) == 1
    req = dispatched[0]
    # Auto-generated name matches pattern and has prefix
    assert req.name.startswith("demo-service-")
    assert run_source._RUN_NAME_RE.fullmatch(req.name) is not None
    assert req.source == archive_file.resolve()
    assert req.manifest_digest == manifest_digest
    assert req.detached is True
    assert capsys.readouterr().out.strip() == req.name


def test_cli_run_warm_local_source_does_not_query_registries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from types import SimpleNamespace

    from palimpsest_local import run_source

    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "init_roots", lambda *a, **k: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda *a, **k: roots)

    blobs_dir = roots.state / "sources" / "blobs"
    blobs_dir.mkdir(parents=True, mode=0o700)
    archive_file = blobs_dir / "warm.oci.tar"
    archive_content = b"fake-warm-oci-tar"
    archive_file.write_bytes(archive_content)
    archive_digest = f"sha256:{hashlib.sha256(archive_content).hexdigest()}"
    manifest_digest = f"sha256:{hashlib.sha256(b'fake-warm-manifest').hexdigest()}"

    rec = run_source.RetainedSourceRecord(
        reference="warm-org/cached-app:1.0",
        canonical_reference="docker.io/warm-org/cached-app:1.0",
        archive_path=str(archive_file),
        archive_digest=archive_digest,
        archive_size_bytes=len(archive_content),
        manifest_digest=manifest_digest,
        root_digest=manifest_digest,
        source_type="oci-registry",
        platform="linux/amd64",
        created_at="2026-10-10T00:00:00Z",
        tags=("warm-org/cached-app:1.0",),
    )
    run_source.save_retained_source_record(roots, rec)

    # Guard that registries and docker are NEVER queried
    monkeypatch.setattr(
        run_source,
        "pull_registry_oci_archive",
        lambda *a, **k: pytest.fail("remote registry queried on warm local source"),
    )
    monkeypatch.setattr(
        run_source,
        "export_docker_image_to_oci_archive",
        lambda *a, **k: pytest.fail("docker export queried on warm local source"),
    )

    dispatched: list[object] = []
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **kw: (
            dispatched.append(request)
            or SimpleNamespace(record=SimpleNamespace(name=request.name), terminal=None, session=None)
        ),
    )

    ret = cli.main(["run", "warm-org/cached-app:1.0", "--name", "warm-vm", "-d"])
    assert ret == 0
    assert len(dispatched) == 1
    assert dispatched[0].source == archive_file.resolve()
    assert dispatched[0].manifest_digest == manifest_digest


def test_cli_run_web_oci_digest_selection_with_explicit_kind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from types import SimpleNamespace

    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "init_roots", lambda *a, **k: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda *a, **k: roots)

    archive_file = tmp_path / "export.oci.tar"
    archive_content = b"fake-exported-archive-bytes"
    archive_file.write_bytes(archive_content)
    archive_digest = f"sha256:{hashlib.sha256(archive_content).hexdigest()}"
    manifest_digest = f"sha256:{hashlib.sha256(b'web-selected-manifest').hexdigest()}"

    build_dir = roots.builds / "bk-0123456789ab"
    build_dir.mkdir(parents=True)
    build_record = {
        "build_id": "bk-0123456789ab",
        "engine": "buildkit",
        "status": "success",
        "output_tags": ["web-built:latest"],
        "output_path": str(archive_file),
        "output_oci_archive_digest": archive_digest,
        "output_oci_manifest_digest": manifest_digest,
    }
    (build_dir / "record.json").write_text(json.dumps(build_record), encoding="utf-8")

    dispatched: list[object] = []
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **kw: (
            dispatched.append(request)
            or SimpleNamespace(record=SimpleNamespace(name=request.name), terminal=None, session=None)
        ),
    )

    # Web passes image=manifest_digest with runtime_kind=oci-root
    ret = cli.main(["run", manifest_digest, "--runtime-kind", "oci-root", "--name", "web-vm", "-d"])
    assert ret == 0
    assert len(dispatched) == 1
    assert dispatched[0].source == archive_file.resolve()
    assert dispatched[0].manifest_digest == manifest_digest


def test_cli_run_short_ref_searches_deterministic_oci_profiles_on_true_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from types import SimpleNamespace

    from palimpsest_local import registry_intake, run_source
    from palimpsest_local.registry import RegistryConfig, RegistryProfile, save_registry_config

    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "init_roots", lambda *a, **k: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda *a, **k: roots)

    config = RegistryConfig(
        schema_version=1,
        default="alpha",
        registries={
            "docker": RegistryProfile(alias="docker", endpoint="docker.io", namespace="library"),
            "alpha": RegistryProfile(alias="alpha", endpoint="reg-alpha.example.com"),
            "beta": RegistryProfile(alias="beta", endpoint="reg-beta.example.com"),
            "gamma": RegistryProfile(alias="gamma", endpoint="reg-gamma.example.com"),
            "palnative": RegistryProfile(
                alias="palnative",
                endpoint="native.example.com",
                protocol="palimpsest",
                api_base="https://native.example.com",
            ),
        },
    )
    save_registry_config(roots, config)

    queried_profiles: list[str] = []
    pulled_archive = tmp_path / "pulled.oci.tar"
    pulled_content = b"fake-pulled-archive"
    pulled_archive.write_bytes(pulled_content)

    def fake_pull(ref, out, _roots, *, profile=None, **kwargs):
        alias = profile.alias if profile else "none"
        queried_profiles.append(alias)
        if alias == "alpha":
            # True miss in default profile: 404
            raise registry_intake.RegistryImageNotFoundError("manifest unknown in alpha")
        if alias == "beta":
            # Hit in second profile
            out.write_bytes(pulled_content)
            return registry_intake.RegistryPullReceipt(
                reference=ref,
                output=out,
                platform="linux/amd64",
                root_digest="sha256:" + "1" * 64,
                manifest_digest="sha256:" + "2" * 64,
                source_cas_id="cas-1",
                source_binding_digest="sha256:" + "3" * 64,
            )
        pytest.fail(f"unexpected query to profile: {alias}")

    monkeypatch.setattr(run_source, "pull_registry_oci_archive", fake_pull)
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **kw: SimpleNamespace(record=SimpleNamespace(name=request.name), terminal=None, session=None),
    )

    ret = cli.main(["run", "service:3.0", "--name", "det-vm", "-d"])
    assert ret == 0
    # Alpha was tried first (default), then Beta (next OCI profile alphabetically), Gamma was not reached
    # Native profile palnative was completely excluded (not treated as Docker /v2)
    assert queried_profiles == ["alpha", "beta"]


def test_cli_run_explicit_registry_respects_authority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from types import SimpleNamespace

    from palimpsest_local import registry_intake, run_source
    from palimpsest_local.registry import RegistryConfig, RegistryProfile, save_registry_config

    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "init_roots", lambda *a, **k: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda *a, **k: roots)

    config = RegistryConfig(
        schema_version=1,
        default="docker",
        registries={
            "docker": RegistryProfile(alias="docker", endpoint="docker.io", namespace="library"),
            "quay": RegistryProfile(alias="quay", endpoint="quay.io"),
        },
    )
    save_registry_config(roots, config)

    queried: list[str] = []

    def fake_pull(ref, out, _roots, *, profile=None, **kwargs):
        queried.append(profile.endpoint if profile else "none")
        out.write_bytes(b"dummy")
        return registry_intake.RegistryPullReceipt(
            reference=ref,
            output=out,
            platform="linux/amd64",
            root_digest="sha256:" + "a" * 64,
            manifest_digest="sha256:" + "b" * 64,
            source_cas_id="c-1",
            source_binding_digest="sha256:" + "c" * 64,
        )

    monkeypatch.setattr(run_source, "pull_registry_oci_archive", fake_pull)
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **kw: SimpleNamespace(record=SimpleNamespace(name=request.name), terminal=None, session=None),
    )

    # Explicit reference with authority quay.io queries ONLY quay.io, never default docker.io
    ret = cli.main(["run", "quay.io/core/app:v1", "--name", "auth-vm", "-d"])
    assert ret == 0
    assert queried == ["quay.io"]

    # Selecting mismatched --registry fails
    assert cli.main(["run", "quay.io/core/app:v1", "--registry", "docker", "--name", "bad-vm", "-d"]) == 1


def test_cli_run_unsupported_host_fails_before_acquisition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    from palimpsest_local import run_source

    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "init_roots", lambda *a, **k: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda *a, **k: roots)

    # Host check fails on kvm-device
    def fail_kvm(requirement, **_kw):
        if requirement.capability_id == "host.kvm-device":
            return CapabilityCheck(
                requirement.capability_id, "missing", False, CapabilityErrorCategory.MISSING, "no kvm"
            )
        return CapabilityCheck(requirement.capability_id, "test-present", True)

    monkeypatch.setattr(runtime_dispatch.platforms, "_check_capability", fail_kvm)
    monkeypatch.setattr(
        run_source, "pull_registry_oci_archive", lambda *a, **k: pytest.fail("pull reached on unsupported host")
    )
    monkeypatch.setattr(
        run_source,
        "export_docker_image_to_oci_archive",
        lambda *a, **k: pytest.fail("export reached on unsupported host"),
    )

    ret = cli.main(["run", "remote-registry.example.com/app:v1", "--name", "fail-vm", "-d"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "no kvm" in err or "preflight" in err or "cannot run OCI-root" in err


def test_cli_run_auth_tls_network_errors_are_not_masked_as_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    from palimpsest_local import registry_intake, run_source
    from palimpsest_local.registry import RegistryConfig, RegistryProfile, save_registry_config

    roots = cli.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "init_roots", lambda *a, **k: roots)
    monkeypatch.setattr(cli, "resolve_roots", lambda *a, **k: roots)

    config = RegistryConfig(
        schema_version=1,
        default="p1",
        registries={
            "docker": RegistryProfile(alias="docker", endpoint="docker.io", namespace="library"),
            "p1": RegistryProfile(alias="p1", endpoint="reg1.example.com"),
            "p2": RegistryProfile(alias="p2", endpoint="reg2.example.com"),
        },
    )
    save_registry_config(roots, config)

    attempts: list[str] = []

    def fake_pull(ref, out, _roots, *, profile=None, **kwargs):
        alias = profile.alias if profile else "none"
        attempts.append(alias)
        # Auth error on p1
        raise registry_intake.RegistryAuthenticationError("401 Unauthorized: token expired")

    monkeypatch.setattr(run_source, "pull_registry_oci_archive", fake_pull)

    ret = cli.main(["run", "private-app:1.0", "--name", "auth-vm", "-d"])
    assert ret == 1
    # p2 was NOT queried because p1 failed with auth, which must not masquerade as missing
    assert attempts == ["p1"]
    err = capsys.readouterr().err
    assert "401 Unauthorized" in err


def test_cli_run_docker_credentials_do_not_cross_native_boundary():
    from palimpsest_local.registry import credential_free_subprocess_environment

    env = {
        "PATH": "/usr/bin",
        "DOCKER_CONFIG": "/path/to/docker",
        "PALIMPSEST_TOKEN": "secret-hub-token",
        "PALIMPSEST_PACKAGE_KEY": "secret-native-key",
    }
    sanitized = credential_free_subprocess_environment(env)
    assert sanitized["PATH"] == "/usr/bin"
    assert sanitized["DOCKER_CONFIG"] == "/path/to/docker"
    assert "PALIMPSEST_TOKEN" not in sanitized
    assert "PALIMPSEST_PACKAGE_KEY" not in sanitized
    assert "PALIMPSEST_TOKEN" in env
    assert "PALIMPSEST_PACKAGE_KEY" in env


def test_cli_automatic_acquisition_fixture_is_isolated(tmp_path: Path):
    import os

    from palimpsest_local import run_source

    assert os.environ["DOCKER_CONFIG"] == str(tmp_path / "docker-config")
    assert "DOCKER_HOST" not in os.environ
    assert "DOCKER_CONTEXT" not in os.environ
    assert "PALIMPSEST_REGISTRY" not in os.environ
    assert run_source.find_docker_daemon_source(None, ["service"]) is None
    with pytest.raises(pytest.fail.Exception, match="real Docker inspection"):
        run_source.run_docker_command(["docker", "info"])
    with pytest.raises(pytest.fail.Exception, match="real Docker export"):
        run_source.export_docker_image_to_oci_archive("service", tmp_path / "unused", None)
    with pytest.raises(pytest.fail.Exception, match="real registry acquisition"):
        run_source.pull_registry_oci_archive("service", tmp_path / "unused", None)


def test_store_cli_filters_architecture_and_reports_unknown_without_starting(tmp_path, monkeypatch, capsys):
    from palimpsest_local import platforms

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    roots = cli.init_roots()
    store = ContentStore(roots.store)
    digests = {}
    for arch in ("aarch64", "x86_64", None):
        digest = "sha256:" + store.write_stream([str(arch).encode()]).name
        metadata = {"kind": "cloud-image", "disk_format": "raw", "arch": arch} if arch else {"kind": "squashfs"}
        store.write_metadata(digest, metadata)
        digests[arch] = digest
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    assert cli.main(["store", "ls", "--compatible", "--backend", "lima-krunkit", "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["digest"] for row in rows] == [digests["aarch64"]]
    assert cli.main(["store", "check", digests["x86_64"]]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "incompatible"
    assert cli.main(["store", "check", digests[None]]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"


def test_hub_compatible_layers_scan_past_incomplete_first_page(tmp_path, monkeypatch, capsys):
    import threading
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from palimpsest_local import platforms

    arch = "aarch64"
    base, orphan, leaf = ("sha256:" + digit * 64 for digit in ("a", "b", "c"))
    rows = [
        {"blob_digest": orphan, "kind": "squashfs", "arch": arch, "parent_digest": None},
        {"blob_digest": leaf, "kind": "squashfs", "arch": arch, "parent_digest": None, "base_image_digest": base},
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            target = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(target.query)
            if target.path == "/v1/layers":
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                payload = rows[offset : offset + limit]
            elif target.path == "/v1/layers/" + base:
                payload = {"blob_digest": base, "kind": "cloud-image", "arch": arch, "disk_format": "raw"}
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PALIMPSEST_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PALIMPSEST_TOKEN", "synthetic-loopback-token")
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", arch))
    try:
        assert cli.main(["layer", "ls", "--compatible", "--limit", "1", "--format", "json"]) == 0
        selected = json.loads(capsys.readouterr().out)
        assert [row["blob_digest"] for row in selected] == [leaf]
        assert selected[0]["compatibility"]["compatible"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
