"""Selection is explicit, conservative, disjoint and never an implicit proof run."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/test_lanes.py"
_SPEC = importlib.util.spec_from_file_location("palimpsest_test_lanes", _SCRIPT)
lanes = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = lanes
_SPEC.loader.exec_module(lanes)


def test_current_manifest_is_complete_disjoint_and_has_no_special_in_portable():
    lanes.validate_manifest()
    portable = {selector for lane in lanes.PORTABLE for selector in lanes.selectors(lane)}
    special = {selector for lane in lanes.SPECIAL_FILES for selector in lanes.selectors(lane)}
    assert portable.isdisjoint(special)
    assert sum(len(lanes.selectors(lane)) for lane in lanes.LANES) == len(portable | special)
    assert lanes.LIVE_FILE not in portable
    assert lanes.LIVE_FILE + "::test_live_oci_root" not in portable
    assert lanes.LIVE_FILE + "::test_live_oci_root" in special
    assert all(not item.startswith("hub/") for item in portable)
    assert "hub" not in lanes.expand_lanes(["full"])


@pytest.fixture
def tiny_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(lanes, "PORTABLE_FILES", {"small": ("tests/test_small.py",)})
    monkeypatch.setattr(lanes, "SPECIAL_FILES", {"proof": ()})
    monkeypatch.setattr(lanes, "PORTABLE", ("small",))
    monkeypatch.setattr(lanes, "LANES", ("small", "proof"))
    monkeypatch.setattr(lanes, "MIXED", {"tests/test_mixed.py": {"small": ("test_unit",), "proof": ("test_proof",)}})
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_small.py").write_text("def test_small(): pass\n")
    (tmp_path / "tests/test_mixed.py").write_text("def test_unit(): pass\ndef test_proof(): pass\n")
    lanes.validate_manifest(tmp_path)
    return tmp_path


@pytest.mark.parametrize("damage", ["new-file", "missing-file", "new-mixed", "missing-mixed", "duplicate", "class"])
def test_manifest_refuses_unclassified_or_overlapping_coverage(tiny_manifest, monkeypatch, damage):
    root = tiny_manifest
    if damage == "new-file":
        (root / "tests/test_new.py").write_text("def test_new(): pass\n")
    elif damage == "missing-file":
        (root / "tests/test_small.py").unlink()
    elif damage == "new-mixed":
        with (root / "tests/test_mixed.py").open("a") as stream:
            stream.write("def test_added(): pass\n")
    elif damage == "missing-mixed":
        (root / "tests/test_mixed.py").write_text("def test_unit(): pass\n")
    elif damage == "duplicate":
        monkeypatch.setattr(lanes, "SPECIAL_FILES", {"proof": ("tests/test_small.py",)})
    else:
        with (root / "tests/test_mixed.py").open("a") as stream:
            stream.write("class TestNew:\n    def test_new(self): pass\n")
    with pytest.raises(lanes.LaneError):
        lanes.validate_manifest(root)


def test_changed_direct_tests_select_exact_lanes_and_do_not_enable_proofs():
    selection = lanes.select_changed(("tests/unit/test_oci_monitor_ipc.py",))
    assert selection.lanes == ("oci-monitor",)
    assert selection.suggested == ()
    mixed = lanes.select_changed((lanes.LIVE_FILE,))
    assert mixed.lanes == ("qualification",)
    assert mixed.suggested == ("native-live",)
    proof = lanes.select_changed(("tests/e2e/test_local_oci_build_run.py",))
    assert proof.lanes == () and proof.suggested == ("gate2",)


@pytest.mark.parametrize(
    "path",
    [
        "src/palimpsest_local/state.py",
        "src/palimpsest_local/oci_store.py",
        "src/palimpsest_local/new_module.py",
        "src/palimpsest_local/new_namespace/oci_monitor_ipc.py",
        "conftest.py",
        "pyproject.toml",
        ".github/workflows/ci.yml",
    ],
)
def test_core_and_unknown_changes_fall_back_to_all_portable(path):
    result = lanes.select_changed((path,))
    assert result.lanes == lanes.PORTABLE
    assert "native-live" in result.suggested
    assert not set(result.lanes) & set(lanes.SPECIAL_FILES)


@pytest.mark.parametrize("module", ["oci_boot_access", "oci_monitor_ipc", "oci_root_runtime", "oci_shared_traversal"])
def test_security_boundary_dependencies_include_consumers_and_suggest_native(module):
    result = lanes.select_changed((f"src/palimpsest_local/{module}.py",))
    assert {"oci-monitor", "oci-access", "qualification"} <= set(result.lanes)
    if module in {"oci_boot_access", "oci_monitor_ipc"}:
        assert set(result.lanes) == {"oci-monitor", "oci-access", "qualification"}
    assert {"native-live", "gate2"} <= set(result.suggested)


@pytest.mark.parametrize(
    ("module", "expected", "suggested"),
    [
        ("oci_resource_status", {"host-runtime", "core-cli"}, ()),
        ("oci_worker_limits", {"host-runtime", "oci-store"}, ("native-live",)),
    ],
)
def test_resource_status_sources_select_only_exact_consumers(module, expected, suggested):
    result = lanes.select_changed((f"src/palimpsest_local/{module}.py",))
    assert set(result.lanes) == expected
    assert result.suggested == suggested


@pytest.mark.parametrize(
    "module",
    ["oci_materializer", "oci_materializer_worker", "oci_packer", "oci_worker_protocol"],
)
def test_materializer_worker_sources_preserve_broad_consumers_and_suggest_external_proofs(module):
    result = lanes.select_changed((f"src/palimpsest_local/{module}.py",))

    assert set(result.lanes) == {
        "build-registry",
        "core-cli",
        "host-runtime",
        "oci-access",
        "oci-guest",
        "oci-monitor",
        "oci-store",
        "qualification",
    }
    assert result.suggested == ("native-live", "guest-kvm", "guest-binary", "filesystem", "gate1", "gate2")


def test_documentation_and_empty_changes_explain_no_tests():
    for paths in ((), ("README.md", "docs/acceptance.md")):
        selection = lanes.select_changed(paths)
        assert selection.lanes == selection.suggested == ()
        assert selection.reasons


def test_tool_and_workflow_changes_select_bounded_contracts():
    result = lanes.select_changed(("scripts/test_lanes.py", ".github/workflows/test.yml"))
    assert set(result.lanes) == {"core-cli", "qualification"}
    assert any("all-portable CI" in reason for reason in result.reasons)


def test_full_preserves_explicit_legacy_root_suite_and_original_skip_behavior(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        lanes.subprocess, "run", lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0)
    )
    monkeypatch.delenv("PALIMPSEST_OCI_ROOT_E2E", raising=False)
    assert lanes.main(["run", "full"]) == 0
    assert calls == [(sys.executable, "-m", "pytest", "-q", "tests")]
    assert "may SKIP" in capsys.readouterr().out
    with pytest.raises(lanes.LaneError):
        lanes.expand_lanes(["full", "portable"])


def test_hub_is_separate_environment_not_implicit_portable():
    result = lanes.select_changed(("hub/src/palimpsest_hub/main.py",))
    assert result.lanes == () and result.suggested == ("hub",)
    (command,) = lanes.commands(("hub",))
    assert command[:4] == (sys.executable, "-m", "pytest", "-q")
    assert command[command.index("-c") + 1] == "hub/pyproject.toml"


def _git(root, *argv):
    return subprocess.run(["git", *argv], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


def test_changed_discovery_includes_committed_staged_unstaged_deleted_and_untracked(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    for name in ("tracked.py", "deleted.py", "staged.py", "committed.py"):
        (tmp_path / name).write_text("before\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "committed.py").write_text("committed\n")
    _git(tmp_path, "add", "committed.py")
    _git(tmp_path, "commit", "-qm", "committed change")
    (tmp_path / "staged.py").write_text("staged\n")
    _git(tmp_path, "add", "staged.py")
    (tmp_path / "tracked.py").write_text("unstaged\n")
    (tmp_path / "deleted.py").unlink()
    unusual = "spaces\n$(not-a-command).md"
    (tmp_path / unusual).write_text("untracked\n")
    assert set(lanes.changed_files(base, tmp_path)) == {
        "tracked.py",
        "deleted.py",
        "staged.py",
        "committed.py",
        unusual,
    }
    with pytest.raises(lanes.LaneError):
        lanes.changed_files("--not-a-revision;echo bad", tmp_path)


@pytest.mark.parametrize("value", ["0/2", "3/2", "1/0", "1/257", "01/2", "1", "1/2/3", "x/y"])
def test_shard_invalid_values_fail_closed(value):
    with pytest.raises(lanes.LaneError):
        lanes.parse_shard(value)


def test_node_shards_are_deterministic_disjoint_and_complete():
    nodes = [f"tests/unit/test_oci_store.py::test_large[case-{i}]" for i in range(1000)]
    groups = [{node for node in nodes if lanes.node_shard(node, 6) == i} for i in range(1, 7)]
    assert set.union(*groups) == set(nodes)
    assert sum(map(len, groups)) == len(nodes)
    assert all(groups)
    assert all(groups[i].isdisjoint(groups[j]) for i in range(6) for j in range(i))
    assert lanes.node_shard(nodes[0], 6) == lanes.node_shard(nodes[0], 6)
    assert max(map(len, groups)) < 220  # Sanity, not a runtime balance claim.


def test_portable_command_shards_nodes_without_special_selectors_or_environment_mutation(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT", "1")
    before = dict(os.environ)
    (command,) = lanes.commands(lanes.expand_lanes(["portable"]), "2/6")
    assert command[:4] == (sys.executable, "-m", "pytest", "-q")
    assert command[command.index("--test-lane-shard") + 1] == "2/6"
    assert lanes.LIVE_FILE + "::test_live_oci_root" not in command
    assert "tests/kvm/test_oci_guest_stage1_live.py" not in command
    assert "tests/integration/test_oci_guest_stage1_binary.py" not in command
    assert dict(os.environ) == before
    with pytest.raises(lanes.LaneError):
        lanes.commands(("native-live",), "1/2")


@pytest.mark.parametrize(
    "lane,required", [(lane, key) for lane, values in lanes.REQUIRED_ENV.items() for key in values]
)
def test_special_lanes_require_explicit_flags(lane, required):
    environment = {key: value or "/fixture" for key, value in lanes.REQUIRED_ENV[lane].items()}
    lanes.require_environment((lane,), environment)
    environment.pop(required)
    with pytest.raises(lanes.LaneError, match=required):
        lanes.require_environment((lane,), environment)


def test_cli_dry_run_displays_command_but_never_executes_or_enables_flags(monkeypatch, capsys):
    monkeypatch.setattr(lanes.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("dry run executed command"))
    before = dict(os.environ)
    assert lanes.main(["run", "native-live", "--dry-run"]) == 0
    assert "test_live_oci_root" in capsys.readouterr().out
    assert dict(os.environ) == before


def test_cli_run_requires_selection_and_special_flags_before_subprocess(monkeypatch, capsys):
    monkeypatch.delenv("PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT", raising=False)
    monkeypatch.setattr(lanes.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("missing gate executed command"))
    assert lanes.main(["run"]) == 2
    assert lanes.main(["run", "native-live"]) == 2
    assert "requires explicit" in capsys.readouterr().err


def test_runner_preserves_current_python_and_nonzero_exit(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(lanes.subprocess, "run", run)
    assert lanes.main(["run", "core-cli"]) == 3
    assert calls[0][0][0] == sys.executable
    assert calls[0][1] == {"cwd": lanes.ROOT, "check": False}


@pytest.mark.parametrize("kind", ["empty-shard", "collection-error", "all-skipped", "valid"])
def test_real_pytest_plugin_fails_closed_and_reports_shard_counts(tmp_path, kind):
    path = tmp_path / "test_plugin_probe.py"
    if kind == "collection-error":
        path.write_text("raise RuntimeError('collection failed')\n")
    elif kind == "all-skipped":
        path.write_text("import pytest\n@pytest.mark.skip(reason='unavailable')\ndef test_one(): pass\n")
    else:
        path.write_text("def test_one(): pass\n")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "scripts.test_lanes", str(path)]
    if kind == "empty-shard":
        # Find the exact node ID through real collection; then choose another shard.
        collected = subprocess.run(
            [*command, "--collect-only"], cwd=lanes.ROOT, text=True, capture_output=True, check=True
        )
        key = next(
            line.removeprefix("test-lane-key ")
            for line in collected.stdout.splitlines()
            if line.startswith("test-lane-key ")
        )
        chosen = 2 if int(key[:16], 16) % 2 + 1 == 1 else 1
        command.extend(("--test-lane-shard", f"{chosen}/2"))
    elif kind == "all-skipped":
        command.append("--test-lane-require-executed")
    else:
        command.extend(("--test-lane-shard", "1/1"))
    result = subprocess.run(command, cwd=lanes.ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == (0 if kind == "valid" else 5 if kind in {"empty-shard", "all-skipped"} else 2), (
        result.stdout + result.stderr
    )
    if kind in {"empty-shard", "valid"}:
        assert "Lane shard" in result.stdout and "not a pass count" in result.stdout


def test_collection_key_uses_param_positions_not_random_display_values():
    one = SimpleNamespace(
        parent=SimpleNamespace(nodeid="tests/test_random.py"),
        originalname="test_random",
        name="test_random[uuid-A]",
        callspec=SimpleNamespace(indices={"value": 0}),
    )
    two = SimpleNamespace(
        parent=one.parent,
        originalname=one.originalname,
        name="test_random[uuid-B]",
        callspec=SimpleNamespace(indices={"value": 0}),
    )
    assert lanes.collection_key(one) == lanes.collection_key(two)
    two.callspec.indices["value"] = 1
    assert lanes.collection_key(one) != lanes.collection_key(two)


def test_separate_process_random_uuid_parameters_have_exact_complete_shard_union(tmp_path):
    path = tmp_path / "test_random_probe.py"
    path.write_text(
        "import uuid, pytest\n@pytest.mark.parametrize('value', [str(uuid.uuid4()) for _ in range(24)])\ndef test_random(value): pass\n"
    )
    base = [sys.executable, "-m", "pytest", "-q", "-p", "scripts.test_lanes", str(path), "--collect-only"]

    def collect(extra):
        result = subprocess.run([*base, *extra], cwd=lanes.ROOT, text=True, capture_output=True, check=True)
        return {
            line.removeprefix("test-lane-key ")
            for line in result.stdout.splitlines()
            if line.startswith("test-lane-key ")
        }

    unsharded = collect([])
    groups = [collect(["--test-lane-shard", f"{index}/4"]) for index in range(1, 5)]
    assert len(unsharded) == 24
    assert set.union(*groups) == unsharded
    assert sum(map(len, groups)) == len(unsharded)
    assert all(groups)


def test_duplicate_logical_keys_refuse_collection():
    item = SimpleNamespace(
        parent=SimpleNamespace(nodeid="tests/test_same.py"), originalname="test_same", name="test_same"
    )
    config = SimpleNamespace(getoption=lambda _: "1/2")
    with pytest.raises(lanes.LaneError, match="duplicate stable"):
        lanes.pytest_collection_modifyitems(config, [item, item])
