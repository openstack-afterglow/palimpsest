"""Isolated CLI contracts for named volume routing and logical run mounts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from palimpsest_local import cli, named_volumes
from palimpsest_local.errors import StateError
from palimpsest_local.run_source import ResolvedRunSource


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "selected-state"))
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(journal))
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker-config"))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("PALIMPSEST_REGISTRY", raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *_a, **_k: pytest.fail("CLI test reached a real host command"))
    monkeypatch.setattr(
        cli, "resolve_run_source", lambda *_a, **_k: pytest.fail("CLI test reached real source acquisition")
    )


@pytest.mark.parametrize(
    "value",
    [
        "data",
        "data:",
        ":/data",
        "data:relative",
        "data:/data:rw",
        "data:/data:",
        "data:/data:ro:extra",
        "/tmp/data:/data",
        "./data:/data",
        "../data:/data",
        "Bad:/data",
        "data:/",
        "data:/dev",
        "data:/proc/files",
        "data:/sys/fs",
        "data:/a/../b",
        "data:/a/./b",
        "data://data",
        "data:/data/",
        "data:/a//b",
    ],
)
def test_run_rejects_malformed_volume_before_any_side_effect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_a: pytest.fail("invalid volume opened journal"))
    monkeypatch.setattr(cli, "resolve_roots", lambda: pytest.fail("invalid volume resolved state"))
    assert cli.main(["run", "unused", "--name", "demo", "--volume", value]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--volume" in captured.err


@pytest.mark.parametrize(
    ("mounts", "message"),
    [
        (["data:/data", "data:/other:ro"], "repeat a volume name"),
        (["data:/data", "other:/data:ro"], "repeat a guest target"),
    ],
)
def test_run_rejects_duplicate_volume_names_and_targets_before_acquisition(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mounts: list[str], message: str
) -> None:
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_a: pytest.fail("duplicate volume opened journal"))
    monkeypatch.setattr(cli, "resolve_roots", lambda: pytest.fail("duplicate volume resolved state"))
    argv = ["run", "unused", "--name", "demo"]
    for mount in mounts:
        argv += ["--volume", mount]
    assert cli.main(argv) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("value", "size_bytes"),
    [("16", 16 * 1024**2), ("512MiB", 512 * 1024**2), ("4GiB", 4 * 1024**3), ("2gib", 2 * 1024**3)],
)
def test_volume_create_reuses_project_size_conventions(value: str, size_bytes: int) -> None:
    args = cli.build_parser().parse_args(["volume", "create", "data", "--size", value, "--backend", "kvm"])
    assert args.operation == "volume"
    assert args.volume_operation == "create"
    assert args.name == "data"
    assert args.size == size_bytes


@pytest.mark.parametrize("value", ["0", "15MiB", "1.5GiB", "1GB", "bogus", "16385GiB", "9" * 100])
def test_volume_create_rejects_invalid_size_before_state_access(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_a: pytest.fail("invalid size opened journal"))
    monkeypatch.setattr(cli, "resolve_roots", lambda: pytest.fail("invalid size resolved state"))
    assert cli.main(["volume", "create", "data", "--size", value, "--backend", "kvm"]) == 2
    assert "--size" in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["create", "inspect", "rm"])
@pytest.mark.parametrize("name", ["/tmp/data", "../data", "foo/bar", "Bad", ""])
def test_volume_operations_reject_host_paths_and_invalid_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], operation: str, name: str
) -> None:
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_a: pytest.fail("invalid name opened journal"))
    monkeypatch.setattr(cli, "resolve_roots", lambda: pytest.fail("invalid name resolved state"))
    argv = ["volume", operation, name, "--backend", "kvm"]
    if operation == "create":
        argv += ["--size", "1GiB"]
    assert cli.main(argv) == 1
    assert "volume name" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["volume"],
        ["volume", "create", "data", "--backend", "kvm"],
        ["volume", "create", "data", "--size", "1GiB"],
        ["volume", "inspect", "data"],
        ["volume", "rm", "data"],
        ["volume", "rm", "--backend", "kvm"],
        ["volume", "inspect", "data", "--backend", "auto"],
        ["volume", "inspect", "data", "--backend", "lima-qemu-oci"],
        ["volume", "inspect", "data", "--backend", "kvm", "--size", "1GiB"],
        ["volume", "rm", "data", "--backend", "kvm", "--yes"],
    ],
)
def test_volume_requires_explicit_identity_and_does_not_ignore_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_a: pytest.fail("invalid command opened journal"))
    monkeypatch.setattr(cli, "resolve_roots", lambda: pytest.fail("invalid command resolved state"))
    assert cli.main(argv) == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize("backend", ["kvm", "lima-vz", "lima-krunkit", "libvirt-hvf"])
def test_volume_inspect_filters_exact_name_and_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: str
) -> None:
    roots = cli.resolve_roots()
    expected = {"name": "data", "backend": backend, "status": "available", "kind": "named"}
    rows = [
        {"name": "data-backup", "backend": backend},
        {"name": "data", "backend": "lima-vz" if backend == "kvm" else "kvm"},
        expected,
    ]
    calls = []

    def inventory(candidate):
        calls.append(candidate)
        return rows

    monkeypatch.setattr(named_volumes, "list_named_volumes", inventory)
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("inspect initialized state"))
    monkeypatch.setattr(cli.inventory, "list_volumes", lambda *_a: pytest.fail("inspect used broad host inventory"))
    assert cli.main(["volume", "inspect", "data", "--backend", backend]) == 0
    assert calls == [roots]
    assert json.loads(capsys.readouterr().out) == expected
    assert not roots.state.exists()


@pytest.mark.parametrize(
    "rows", [[], [{"name": "data", "backend": "lima-vz"}, {"name": "data-copy", "backend": "kvm"}]]
)
def test_volume_inspect_missing_exact_identity_reports_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], rows: list[dict[str, str]]
) -> None:
    monkeypatch.setattr(named_volumes, "list_named_volumes", lambda _roots: rows)
    assert cli.main(["volume", "inspect", "data", "--backend", "kvm"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "'data'" in captured.err and "'kvm'" in captured.err and "does not exist" in captured.err


@pytest.mark.parametrize(
    ("options", "allowed"),
    [
        ([], True),
        (["--root-retention", "retain"], True),
        (["--root-retention", "delete"], False),
        (["--root-volume", "saved-root"], False),
    ],
)
def test_cloud_root_policy_accepts_only_existing_retention_semantics(
    tmp_path,
    monkeypatch,
    capsys,
    options,
    allowed,
):
    source = tmp_path / "cloud.qcow2"
    monkeypatch.setattr(cli, "resolve_run_source", lambda *_a, **_k: ResolvedRunSource("cloud-image", source))
    entered = []

    def resolve_stack(*_args, **_kwargs):
        entered.append(True)
        raise StateError("cloud stack acquisition boundary")

    monkeypatch.setattr(cli, "_resolve_runtime_stack", resolve_stack)
    assert cli.main(["run", "cloud:latest", "--name", "demo", *options]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert bool(entered) is allowed
    assert ("cloud stack acquisition boundary" in captured.err) is allowed
    if not allowed:
        assert "supported only for OCI runs" in captured.err
