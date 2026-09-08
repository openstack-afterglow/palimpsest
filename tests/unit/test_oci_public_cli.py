"""Public OCI routing opens only the implemented local run/STOP/removal path."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_runtime_dispatch as dispatch_tests

from palimpsest_local import (
    cli,
    oci_host,
    oci_root_proof,
    oci_root_volume_inventory,
    platforms,
    runtime_dispatch,
    state,
)
from palimpsest_local.errors import StateError
from palimpsest_local.oci_process import OCIUserSpec
from palimpsest_local.oci_run_request import LocalOCIRunRequest
from palimpsest_local.runtime_types import (
    CapabilityCheck,
    DispatchKey,
    ExpectedRunIdentity,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessStatusEvent,
    ProcessStream,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RuntimeOperation,
)


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PALIMPSEST_STATE_HOME", raising=False)
    monkeypatch.setattr(
        platforms,
        "_check_capability",
        lambda requirement, **kwargs: CapabilityCheck(requirement.capability_id, "test-present", True),
    )


class Session(dispatch_tests._FakeProcessSession):
    def events(self):
        yield ProcessOutputEvent(ProcessStream.STDOUT, b"literal console output\n")
        yield ProcessStatusEvent(self.wait())

    def wait(self):
        return ProcessExit(23, 23, None, ProcessExitCategory.EXITED)


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("source_kind", ["archive", "layout"])
def test_public_local_run_foreground_or_detached(tmp_path, monkeypatch, capsys, detached, source_kind):
    source = tmp_path / ("image.oci.tar" if source_kind == "archive" else "layout")
    if source_kind == "archive":
        source.touch()
    else:
        source.mkdir()
    seen = []

    def launch(request, *, roots):
        seen.append(request)
        return SimpleNamespace(
            record=SimpleNamespace(name=request.name), terminal=None, session=None if request.detached else Session()
        )

    monkeypatch.setattr(runtime_dispatch, "run_local_oci", launch)
    monkeypatch.setattr(cli, "_resolve_runtime_stack", lambda *a, **k: pytest.fail("cloud resolution entered"))
    args = ["run", str(source), "--name", "demo", "--network", "none"]
    if source_kind == "layout":
        args += ["--runtime-kind", "oci-root"]
    if detached:
        args += ["-d"]
    assert cli.main(args) == (0 if detached else 23)
    request = seen[0]
    assert isinstance(request, LocalOCIRunRequest)
    assert request.detached is detached and request.network is None
    assert request.root_retention == "delete" and request.root_volume_id is None
    assert request.user_override is None
    assert request.source == source.resolve()
    assert capsys.readouterr().out == ("demo\n" if detached else "literal console output\n")


def test_root_manifest_pin_is_canonicalized_for_local_intake(tmp_path, monkeypatch):
    source = tmp_path / "image.oci.tar"
    source.touch()
    seen = []
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **kw: (
            seen.append(request) or SimpleNamespace(record=SimpleNamespace(name="demo"), terminal=None, session=None)
        ),
    )
    assert cli.main(["run", str(source), "--name", "demo", "--manifest", "A" * 64, "-d"]) == 0
    assert seen[0].manifest_digest == "sha256:" + "a" * 64


def test_public_local_run_accepts_redis_user_as_separate_compatibility_mode(tmp_path, monkeypatch):
    source = tmp_path / "redis.oci.tar"
    source.touch()
    seen = []
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **_kwargs: (
            seen.append(request) or SimpleNamespace(record=SimpleNamespace(name="redis"), terminal=None, session=None)
        ),
    )

    assert cli.main(["run", str(source), "--name", "redis", "--user", "redis", "-d"]) == 0
    assert seen[0].user_override == OCIUserSpec("redis", None)


@pytest.mark.parametrize("value", ["1000", "1000:1001", "redis:staff"])
def test_public_local_run_accepts_canonical_user_and_group_forms(tmp_path, monkeypatch, value):
    source = tmp_path / "image.oci.tar"
    source.touch()
    seen = []
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **_kwargs: (
            seen.append(request) or SimpleNamespace(record=SimpleNamespace(name="demo"), terminal=None, session=None)
        ),
    )

    assert cli.main(["run", str(source), "--name", "demo", "--user", value, "-d"]) == 0
    assert seen[0].user_override == OCIUserSpec.from_override_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ":staff",
        "redis:",
        "redis:staff:extra",
        "01",
        "1000:01",
        "bad user",
        "9" * 5000,
        f"redis:{'9' * 5000}",
    ],
)
def test_public_local_run_rejects_invalid_user_before_adapter(tmp_path, monkeypatch, value):
    source = tmp_path / "image.oci.tar"
    source.touch()
    monkeypatch.setattr(runtime_dispatch, "run_local_oci", lambda *_a, **_k: pytest.fail("adapter entered"))

    assert cli.main(["run", str(source), "--name", "demo", "--user", value, "-d"]) == 1


def test_public_run_threads_explicit_retained_root_policy(tmp_path, monkeypatch):
    source = tmp_path / "image.oci.tar"
    source.touch()
    volume_id = "49bd618f-1a3e-4cd8-b436-58c194efd791"
    seen = []
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda request, **_kwargs: (
            seen.append(request) or SimpleNamespace(record=SimpleNamespace(name="demo"), terminal=None, session=None)
        ),
    )
    assert (
        cli.main(["run", str(source), "--name", "demo", "--root-retention", "retain", "--root-volume", volume_id, "-d"])
        == 0
    )
    assert seen[0].root_retention == "retain" and seen[0].root_volume_id == volume_id


def test_public_run_rejects_root_volume_without_retain_before_adapter(tmp_path, monkeypatch):
    source = tmp_path / "image.oci.tar"
    source.touch()
    monkeypatch.setattr(runtime_dispatch, "run_local_oci", lambda *_a, **_k: pytest.fail("adapter entered"))
    assert (
        cli.main(["run", str(source), "--name", "demo", "--root-volume", "49bd618f-1a3e-4cd8-b436-58c194efd791"]) == 1
    )


def test_successful_retained_rm_discloses_only_exact_root_uuid(monkeypatch, capsys):
    from palimpsest_local.oci_run_cleanup import OCIRunRemovalResult

    result = OCIRunRemovalResult(
        "demo",
        "00000000-0000-4000-8000-000000000000",
        "49bd618f-1a3e-4cd8-b436-58c194efd791",
        "retain",
    )
    monkeypatch.setattr(runtime_dispatch, "rm", lambda *_a, **_k: result)
    assert cli.main(["rm", "demo"]) == 0
    assert capsys.readouterr().out == "removed demo\nretained root\t49bd618f-1a3e-4cd8-b436-58c194efd791\n"


@pytest.mark.parametrize(
    "flags", [["--layer", "sha256:" + "a" * 64], ["--network", "default"], ["--backend", "lima-vz"]]
)
def test_unsupported_local_policy_refused_before_adapter(tmp_path, monkeypatch, flags):
    source = tmp_path / "image.oci.tar"
    source.touch()
    monkeypatch.setattr(runtime_dispatch, "run_local_oci", lambda *a, **k: pytest.fail("adapter entered"))
    assert cli.main(["run", str(source), "--name", "demo", *flags]) == 1


def test_detached_early_exit_is_not_reported_as_running(tmp_path, monkeypatch, capsys):
    source = tmp_path / "image.oci.tar"
    source.touch()
    monkeypatch.setattr(
        runtime_dispatch,
        "run_local_oci",
        lambda *a, **k: SimpleNamespace(
            record=SimpleNamespace(name="demo"),
            terminal=ProcessExit(0, 0, None, ProcessExitCategory.EXITED),
            session=None,
        ),
    )
    assert cli.main(["run", str(source), "--name", "demo", "-d"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "exited before detached" in captured.err


@pytest.mark.parametrize(
    "flags",
    [
        ["-d"],
        ["--manifest", "a" * 64],
        ["--root-retention", "retain"],
        ["--root-volume", "49bd618f-1a3e-4cd8-b436-58c194efd791"],
        ["--user", "redis"],
    ],
)
def test_cloud_run_does_not_adopt_new_oci_flags(monkeypatch, flags):
    monkeypatch.setattr(cli, "_resolve_runtime_stack", lambda *a, **k: pytest.fail("cloud resolution entered"))
    assert cli.main(["run", "cloud:latest", "--name", "demo", *flags]) == 1


def test_cloud_user_rejection_precedes_store_resolution_and_state_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ContentStore", lambda *_a, **_k: pytest.fail("content store entered"))
    monkeypatch.setattr(cli, "_resolve_runtime_stack", lambda *_a, **_k: pytest.fail("cloud resolution entered"))

    assert cli.main(["run", "cloud:latest", "--name", "demo", "--user", "redis"]) == 1
    assert not (tmp_path / "state").exists() and not (tmp_path / "config").exists()


@pytest.mark.parametrize("kind", ["layout", "explicit-cloud", "reference"])
def test_existing_cloud_source_selection_and_default_network_are_preserved(tmp_path, monkeypatch, kind):
    source = tmp_path / ("image.oci.tar" if kind == "explicit-cloud" else "layout")
    if kind == "layout":
        source.mkdir()
        (source / "oci-layout").touch()
    elif kind == "explicit-cloud":
        source.touch()
    seen = []

    def cloud(*args, **kwargs):
        seen.append(kwargs["run_network"])
        raise StateError("selected cloud resolver")

    monkeypatch.setattr(cli, "_resolve_runtime_stack", cloud)
    monkeypatch.setattr(runtime_dispatch, "run_local_oci", lambda *a, **k: pytest.fail("OCI adapter entered"))
    args = ["run", "cloud:latest" if kind == "reference" else str(source), "--name", "demo"]
    if kind == "explicit-cloud":
        args += ["--runtime-kind", "cloud-image"]
    assert cli.main(args) == 1
    assert seen == ["default"]


def test_init_runtime_does_not_initialize_default_state(tmp_path, monkeypatch, capsys):
    parent = tmp_path / "new parent"
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("default roots initialized"))
    monkeypatch.setattr(cli, "resolve_roots", lambda: pytest.fail("default roots resolved"))
    calls = []
    monkeypatch.setattr(oci_host, "create_runtime_parent", lambda path: calls.append(path) or path)
    assert cli.main(["oci", "init-runtime", str(parent)]) == 0
    assert calls == [parent.resolve()]
    assert capsys.readouterr().out == f"PALIMPSEST_STATE_HOME='{parent.resolve()}/state'\n"
    assert not parent.exists()


def test_root_proof_is_read_only_and_prints_only_the_public_report(monkeypatch, capsys):
    report = {"schema": "palimpsest.oci-root-proof.v1", "root_identity": {"device": 7, "inode": 9}}
    seen = []
    monkeypatch.setattr(oci_root_proof, "root_proof", lambda roots, name: seen.append((roots, name)) or report)
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("root-proof initialized state"))
    assert cli.main(["oci", "root-proof", "demo"]) == 0
    assert seen and seen[0][1] == "demo"
    assert __import__("json").loads(capsys.readouterr().out) == report


@pytest.mark.parametrize("operation", ["root-volumes", "root-volume"])
def test_root_volume_inventory_is_early_read_only_json_without_host_or_state_effects(
    tmp_path, monkeypatch, capsys, operation
):
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("state initialized"))
    monkeypatch.setattr(oci_host, "preflight_oci_host", lambda *_a, **_k: pytest.fail("host probed"))
    expected = {"schema": "fixed"}
    monkeypatch.setattr(
        oci_root_volume_inventory, "root_volumes", lambda selected: expected if selected == roots else None
    )
    monkeypatch.setattr(
        oci_root_volume_inventory,
        "root_volume",
        lambda selected, volume_id: (
            expected if selected == roots and volume_id == "49bd618f-1a3e-4cd8-b436-58c194efd791" else None
        ),
    )
    args = ["oci", operation]
    if operation == "root-volume":
        args.append("49bd618f-1a3e-4cd8-b436-58c194efd791")
    assert cli.main(args) == 0
    assert __import__("json").loads(capsys.readouterr().out) == expected
    assert not roots.state.exists() and not roots.config.exists()


def test_root_volume_inventory_real_projection_and_failures_have_no_partial_stdout(tmp_path, monkeypatch, capsys):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("state initialized"))
    monkeypatch.setattr(oci_host, "preflight_oci_host", lambda *_a, **_k: pytest.fail("host probed"))

    assert cli.main(["oci", "root-volumes"]) == 0
    assert __import__("json").loads(capsys.readouterr().out)["volumes"] == []

    unknown = "49bd618f-1a3e-4cd8-b436-58c194efd791"
    assert cli.main(["oci", "root-volume", unknown]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "unknown" in captured.err

    (roots.oci_root_volumes / "unexpected").touch()
    assert cli.main(["oci", "root-volumes"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "OCI root-volume metadata is unavailable or inconsistent"


def test_root_volume_inventory_missing_runtime_is_fixed_and_boot_independent(tmp_path, monkeypatch, capsys):
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("state initialized"))
    monkeypatch.setattr(oci_host, "preflight_oci_host", lambda *_a, **_k: pytest.fail("host probed"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *_a, **_k: pytest.fail("subprocess entered"))

    assert cli.main(["oci", "root-volumes"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "OCI root-volume metadata is unavailable or inconsistent"
    assert str(tmp_path) not in captured.err
    assert not roots.state.exists() and not roots.config.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["oci", "root-volumes"],
        ["oci", "root-volume", "49bd618f-1a3e-4cd8-b436-58c194efd791"],
    ],
)
@pytest.mark.parametrize("malformed", ["[storage\n", "x=" + "[" * 2000 + "0" + "]" * 2000])
def test_root_volume_inventory_malformed_config_is_fixed_and_path_free(tmp_path, monkeypatch, capsys, args, malformed):
    config_home = tmp_path / "private-config"
    config = config_home / "palimpsest"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(malformed)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("PALIMPSEST_STATE_HOME", raising=False)
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("state initialized"))
    monkeypatch.setattr(oci_host, "preflight_oci_host", lambda *_a, **_k: pytest.fail("host probed"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *_a, **_k: pytest.fail("subprocess entered"))

    assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "OCI root-volume metadata is unavailable or inconsistent"
    assert str(tmp_path) not in captured.err


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_root_volume_inventory_root_resolution_preserves_cancellation(monkeypatch, interruption):
    def interrupt():
        raise interruption(7) if interruption is SystemExit else interruption()

    monkeypatch.setattr(cli, "resolve_roots", interrupt)
    if interruption is SystemExit:
        assert cli.main(["oci", "root-volumes"]) == 7
    else:
        with pytest.raises(KeyboardInterrupt):
            cli.main(["oci", "root-volumes"])


def _record(tmp_path):
    roots = dispatch_tests._roots(tmp_path)
    dispatch_tests._write_ledger(
        roots, record={"schema_version": 2, "runtime_kind": "oci-root", "backend": "kvm", "status": "running"}
    )
    return roots, runtime_dispatch.resolve_existing_run("demo", roots=roots)


@pytest.mark.parametrize("operation", ["stop", "rm"])
def test_oci_lifecycle_routes_exact_identity_without_cloud_adapter(tmp_path, monkeypatch, operation):
    roots, record = _record(tmp_path)
    calls = []
    result = object()

    def mutate(selected_roots, name, *, expected_record):
        calls.append((selected_roots, name, expected_record))
        return result

    monkeypatch.setattr(runtime_dispatch, "_oci_adapter", lambda: SimpleNamespace(**{operation + "_oci_run": mutate}))
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, operation, lambda *a, **k: pytest.fail("cloud adapter entered"))
    assert getattr(runtime_dispatch, operation)("demo", roots=roots) is result
    assert calls == [(roots, "demo", record)]


@pytest.mark.parametrize("operation", ["stop", "rm"])
def test_oci_lifecycle_rejects_name_reuse_before_adapter(tmp_path, monkeypatch, operation):
    roots, record = _record(tmp_path)
    expected = ExpectedRunIdentity(record.name, "00000000-0000-4000-8000-000000000000", record.dispatch_key)
    monkeypatch.setattr(runtime_dispatch, "_oci_adapter", lambda: pytest.fail("adapter selected"))
    with pytest.raises(StateError, match="identity changed"):
        getattr(runtime_dispatch, operation)("demo", roots=roots, expected_identity=expected)


def test_oci_rm_volumes_refused_before_host_probe(tmp_path, monkeypatch):
    roots, _ = _record(tmp_path)
    monkeypatch.setattr(platforms, "_check_capability", lambda *a, **k: pytest.fail("host probe entered"))
    with pytest.raises(StateError, match="--volumes is unavailable"):
        runtime_dispatch.rm("demo", roots=roots, volumes=True)


@pytest.mark.parametrize("operation", ["stop", "rm"])
def test_incomplete_oci_run_evidence_is_preserved_without_backend_mutation(tmp_path, monkeypatch, operation):
    roots, record = _record(tmp_path)
    run_paths = state.run_paths(roots, record.name)
    before = (run_paths.owner.read_bytes(), run_paths.state.read_bytes())
    monkeypatch.setattr(runtime_dispatch.cloud_runtime, operation, lambda *a, **k: pytest.fail("cloud adapter entered"))
    with pytest.raises(StateError):
        getattr(runtime_dispatch, operation)(record.name, roots=roots)
    assert (run_paths.owner.read_bytes(), run_paths.state.read_bytes()) == before


def test_typed_create_dispatch_revalidates_adapter_identity(tmp_path, monkeypatch):
    roots, record = _record(tmp_path)
    request = LocalOCIRunRequest("demo", tmp_path / "image")
    result = SimpleNamespace(record=record)
    calls = []
    monkeypatch.setattr(
        runtime_dispatch,
        "_oci_adapter",
        lambda: SimpleNamespace(run_local_oci=lambda roots, selected: calls.append(selected) or result),
    )
    assert runtime_dispatch.run_local_oci(request, roots=roots) is result
    assert calls == [request]
    result.record = replace(record, name="different")
    with pytest.raises(StateError, match="invalid run identity"):
        runtime_dispatch.run_local_oci(request, roots=roots)


def test_oci_capability_matrix_opens_only_the_implemented_public_operations():
    key = DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)
    for operation in RuntimeOperation:
        if operation in {
            RuntimeOperation.RUN,
            RuntimeOperation.STOP,
            RuntimeOperation.RM,
            RuntimeOperation.PS,
            RuntimeOperation.EXEC,
        }:
            profile = platforms.capability_profile(key, operation, network=None)
            identifiers = {item.capability_id for item in profile.requirements}
            assert not {"tool.ssh", "tool.cloud-localds", "tool.ssh-keygen"} & identifiers
        else:
            with pytest.raises(RuntimeCapabilityError):
                platforms.capability_profile(key, operation)
