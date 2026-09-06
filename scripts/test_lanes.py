#!/usr/bin/env python3
"""Explicit developer test lanes, not a proof of dependency completeness.

Use list --check in CI, plan --changed BASE while editing, and run portable
--shard I/N for independent deterministic node shards. Special lanes require
explicit selection; their existing prerequisite checks and environment remain
untouched. Hub uses the invoking interpreter: invoke this script with its venv.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _units(names: str) -> tuple[str, ...]:
    return tuple(f"tests/unit/test_{name}.py" for name in names.split())


# Every whole-file test belongs to exactly one lane. Do not replace this with
# globs: a new test file must be classified intentionally in code review.
PORTABLE_FILES = {
    "core-cli": _units("""
        afterglow_tracking cli_contract cli_project cli_registry completion
        inventory log_stream metrics refs runtime_dispatch runtime_facade
        sandbox_policy state test_lanes ui
    """),
    "host-runtime": _units("""
        cloud_runtime cloud_runtime_arch cloudinit_guest kvm_contract lima
        platforms process_session project project_adapter project_runtime project_volumes oci_host
        oci_run_request oci_run_adapter oci_public_cli oci_resource_status
    """),
    "build-registry": _units("build buildkit hub_contract registry"),
    "oci-store": _units("""
        oci_changeset oci_convert_security oci_converter_first_pass oci_image
        oci_layout oci_metrics oci_provenance oci_source oci_store oci_store_handoff
        oci_worker_protocol
    """),
    "oci-guest": _units("""
        oci_control_protocol oci_control_protocol_v2 oci_fs_fixtures
        oci_guest_filesystems oci_guest_stage1 oci_initramfs oci_process
        oci_stage1_kvm_proof oci_stage1_qualification oci_stage1_transport oci_guest_exec
    """),
    "oci-monitor": _units("""
        oci_lifecycle_transport oci_monitor oci_monitor_control oci_monitor_handoff
        oci_monitor_ipc oci_monitor_ipc_journal oci_monitor_launch oci_monitor_coordinator
        oci_monitor_client oci_process_session oci_exec_control oci_exec_protocol oci_exec_ipc oci_exec_session oci_exec_client oci_exec_public_routing
        oci_monitor_recovery oci_monitor_retention oci_supervisor oci_run_cleanup
        oci_root_proof
    """),
    "oci-access": _units("""
        oci_acl oci_boot_access oci_boot_access_launch oci_boot_exports
        oci_lower_exports oci_lower_access oci_lower_access_launch
        oci_root_access oci_root_access_lifecycle oci_root_volume oci_runtime_access
        oci_runtime_access_launch oci_runtime_io oci_runtime_io_integration
        oci_shared_root_claim oci_shared_root_traversal oci_shared_traversal
        oci_shared_traversal_init oci_stage1_access oci_stage1_access_launch oci_stage1_access_shared
    """),
    "qualification": _units("oci_monitor_qualification_adapter oci_build_run_acceptance"),
}
SPECIAL_FILES = {
    "native-live": (
        "tests/kvm/test_oci_public_cli_live.py",
        "tests/kvm/test_oci_exec_live.py",
        "tests/kvm/test_oci_exec_cli_live.py",
    ),
    "guest-kvm": ("tests/kvm/test_oci_guest_stage1_live.py",),
    "guest-binary": ("tests/integration/test_oci_guest_stage1_binary.py",),
    "filesystem": (),
    "gate1": ("tests/integration/test_buildkit_named_oci_context.py",),
    "gate2": ("tests/e2e/test_local_oci_build_run.py",),
    "hub": tuple(f"hub/tests/test_{name}.py" for name in ("auth", "hub_api", "image_exports", "migrate")),
}
SPECIAL_NOTES = {
    "native-live": "Native libvirt requires PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT=1; public CLI proof additionally requires PALIMPSEST_OCI_PUBLIC_CLI_LIVE=1. Exec engine/public CLI proofs require PALIMPSEST_OCI_EXEC_LIVE=1 / PALIMPSEST_OCI_EXEC_CLI_LIVE=1 respectively and their image. All require explicit host BOOT config; missing opt-ins skip their proof, not qualify it.",
    "guest-kvm": "Explicit KVM guest proof; requires PALIMPSEST_REQUIRE_STAGE1_KVM=1 and proof fixtures.",
    "guest-binary": "Runs guest ELF under Docker when its existing prerequisites permit it.",
    "filesystem": "Privileged Linux filesystem proof; PALIMPSEST_REQUIRE_OCI_FS=1 makes prerequisites mandatory.",
    "gate1": "BuildKit build gate; set PALIMPSEST_BUILDKIT_E2E=1 and configure its builder explicitly.",
    "gate2": "Build-to-VM gate; PALIMPSEST_OCI_ROOT_E2E=1, transferred artifacts and runtime prerequisites required.",
    "hub": "Separate dependency environment; invoke with the Hub venv Python. Not part of portable/full.",
}
REQUIRED_ENV = {
    "native-live": {"PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT": "1"},
    "guest-kvm": {"PALIMPSEST_REQUIRE_STAGE1_KVM": "1"},
    "filesystem": {"PALIMPSEST_REQUIRE_OCI_FS": "1"},
    "gate1": {"PALIMPSEST_BUILDKIT_E2E": "1"},
    "gate2": {"PALIMPSEST_OCI_ROOT_E2E": "1", "PALIMPSEST_OCI_ROOT_E2E_ARTIFACT_DIR": None},
}

# Mixed helper/proof modules need an exact function-level partition. A new
# function here also requires classification; module markers alone are too broad.
LIVE_FILE = "tests/kvm/test_oci_root_libvirt_live.py"
FS_FILE = "tests/oci_fs/test_layer_filesystem.py"
MIXED = {
    LIVE_FILE: {
        "native-live": ("test_live_oci_root",),
        "qualification": tuple(
            """
            test_product_access_failure_before_spawn_retains_pending_authority
            test_product_private_boundary_rejects_any_extra_named_grant
            test_product_acl_observer_does_not_normalize_or_write_unrecorded_target
            test_product_access_filter_removes_only_exact_eight_owned_targets
            test_product_access_filter_requires_exact_boot_pair
            test_product_access_filter_requires_exact_lower_exports
            test_qemu_dac_baselabel_parser_accepts_one_exact_kvm_identity
            test_qemu_dac_baselabel_parser_rejects_ambiguous_or_noncanonical_input
            test_qualification_acl_target_requires_owned_stable_tmp_descendant
            test_qualification_acl_target_rejects_escape_symlink_and_wrong_kind
            test_qualification_acl_target_openat_walk_rejects_intermediate_swap
            test_qualified_kernel_copy_is_owner_only_and_leaves_source_unchanged
            test_qualified_kernel_copy_removes_exact_partial_on_failure
            test_qualified_kernel_copy_does_not_remove_replaced_destination_on_failure
            test_qualified_kernel_copy_rejects_same_uid_replacement_on_success_path
            test_qualified_kernel_copy_preserves_partial_when_initial_fd_identity_is_unavailable
            test_qualification_console_is_owner_only_and_tail_is_bounded_binary_safe
            test_qualification_console_tail_rejects_symlink
            test_qualification_console_failure_note_preserves_primary_error
            test_qualified_lower_stage_is_owner_only_and_revalidates_both_files
            test_qualified_lower_stage_removes_exact_partial_on_failure
            test_qualified_lower_stage_rejects_source_swap_and_removes_its_partial
            test_qualified_lower_stage_never_removes_replaced_destination
            test_qualified_lower_stage_rejects_changed_existing_copy
            test_acl_parser_accepts_gnu_getfacl_trailing_blank_line_and_compares_structure
            test_runtime_io_adapter_requires_exact_grant_and_keeps_identity_checks
            test_acl_parser_rejects_noncanonical_structure
            test_qualification_dac_broker_applies_in_order_and_restores_in_reverse
            test_qualification_dac_broker_real_fd_bound_apply_and_restore
            test_qualification_dac_broker_refuses_replaced_held_path
            test_qualification_dac_broker_rejects_preexisting_extended_acl
            test_qualification_dac_broker_failure_restores_or_retains_exact_state
            test_qualification_acl_specifications_bind_exact_xml_paths_and_permissions
            test_qualification_acl_specifications_reject_malformed_console
            test_qualification_source_dac_policy_has_no_ambiguous_override
            test_qualification_global_dac_policy_is_exact
            test_qualification_acl_specifications_require_exact_dac_no_relabel
            test_activation_domain_proxy_applies_once_and_caller_restores_only_after_absence
            test_activation_connection_proxy_wraps_only_name_lookup_create_surface
            test_activation_domain_proxy_never_restores_acl_on_create_failure
            test_remove_inactive_domain_proves_absence_before_acl_restore
            test_remove_inactive_domain_retains_acl_when_domain_reactivated
            test_empty_held_qualification_directory_removes_only_held_tree_entries
            test_empty_held_directory_preserves_child_quarantine_on_rename_boundary_swap
            test_empty_held_directory_preserves_late_child_quarantine_replacement
            test_remove_qualification_root_removes_only_expected_inode
            test_remove_qualification_root_refuses_same_uid_swap
            test_remove_qualification_root_refuses_late_quarantine_swap
            test_exact_cleanup_refuses_active_domain_identity_drift
            test_exact_cleanup_expected_inactive_refuses_reactivation
            test_exact_cleanup_revalidates_active_domain_before_destroy
            test_console_marker_count_matches_exact_lines_with_terminal_newlines
            test_completed_fixture_socket_cleanup_is_exact_and_preserves_replacement
            test_completed_monitor_retirement_pins_pid_and_refuses_identity_drift
            test_live_journal_poll_retries_actual_open_replace_race
            test_live_journal_poll_does_not_accept_invalid_journal
            test_live_apparmor_proof_requires_exact_enforcing_runtime_label
            test_live_apparmor_proof_accepts_only_explicit_boolean_or_integer_enforcement
            test_reuse_fixture_injects_pinned_elf_only_into_unique_upper_path
            test_reuse_fixture_replay_failure_prevents_offline_injection
        """.split()
        ),
    },
    FS_FILE: {
        "filesystem": tuple(
            """
            test_layer_filesystem_semantics
            test_hard_worker_materializes_then_reuses_a_local_oci_layer
            test_hard_worker_materializes_all_ordered_image_occurrences_cold_then_warm
            test_erofs_reference_retains_timestamp_failure
        """.split()
        ),
        "oci-store": tuple(
            """
            test_non_linux_rejection_before_subprocess test_base36_conversion
            test_lowerdir_page_budget_128_layers test_tar_translation_whiteouts_and_xattrs
            test_tar_translation_same_layer_whiteout_overridden_by_real_target
            test_tar_translation_file_to_directory_transition_synthesizes_parents
            test_tar_translation_forward_hardlink_emitted_after_target test_tar_translation_leaf_fixture_structure
            test_translated_fixture_bytes_remain_exact test_verify_merged_tree_pure_unit_tests
            test_build_layer_filesystem_cleanup_on_failure
            test_build_layer_filesystem_atomically_publishes_from_private_staging
            test_build_layer_filesystem_refuses_output_symlink_inserted_during_pack
        """.split()
        ),
    },
}

PORTABLE = tuple(PORTABLE_FILES)
LANES = (*PORTABLE, *SPECIAL_FILES)

# Explicit conservative dependency groups. This is a reviewable heuristic, not
# an import graph or a guarantee. Shared infrastructure/unknown changes fall
# back to every portable lane. Suggested external proofs still need opt-in.
DEPENDENCIES = (
    ("oci_resource_status", ("host-runtime", "core-cli"), ()),
    ("oci_worker_limits", ("host-runtime", "oci-store"), ("native-live",)),
    (
        "oci_materializer oci_materializer_worker oci_packer oci_worker_protocol",
        (
            "core-cli",
            "host-runtime",
            "oci-store",
            "oci-guest",
            "oci-monitor",
            "oci-access",
            "qualification",
            "build-registry",
        ),
        ("filesystem", "guest-binary", "guest-kvm", "native-live", "gate1", "gate2"),
    ),
    (
        "oci_exec_control oci_exec_session",
        ("oci-monitor", "oci-guest", "oci-access", "qualification", "core-cli", "host-runtime"),
        ("native-live", "gate2"),
    ),
    (
        "oci_host oci_run_request oci_run_adapter oci_run_cleanup",
        ("host-runtime", "core-cli", "oci-store", "oci-guest", "oci-monitor", "oci-access", "qualification"),
        ("native-live", "gate2"),
    ),
    ("build buildkit hub registry refs", ("build-registry", "core-cli", "host-runtime", "oci-store"), ("gate1", "hub")),
    (
        "cloud_runtime cloudinit guest lima project project_adapter project_runtime project_volumes platforms kvm",
        ("host-runtime", "core-cli", "oci-store", "oci-guest", "oci-monitor", "oci-access", "qualification"),
        ("native-live", "guest-kvm", "gate2"),
    ),
    (
        "oci_changeset oci_convert oci_converter oci_image oci_layout oci_metrics oci_provenance oci_source oci_tar_emitter",
        ("oci-store", "oci-guest", "oci-monitor", "oci-access", "qualification", "build-registry"),
        ("filesystem", "guest-binary", "guest-kvm", "native-live", "gate1", "gate2"),
    ),
    (
        "oci_boot_access oci_boot_exports oci_lower_exports oci_lower_access oci_stage1_access oci_monitor oci_monitor_control oci_monitor_ipc oci_monitor_launch oci_monitor_coordinator oci_monitor_client oci_process_session",
        ("oci-access", "oci-monitor", "qualification"),
        ("native-live", "gate2"),
    ),
    (
        "oci_acl oci_root_access oci_root_volume oci_runtime_access oci_runtime_io oci_shared_traversal",
        ("oci-access", "oci-monitor", "oci-store", "qualification", "host-runtime", "oci-guest"),
        ("native-live", "gate2"),
    ),
    (
        "oci_monitor_handoff oci_monitor_recovery oci_monitor_retention oci_root_kvm oci_root_prepare oci_root_proof oci_root_runtime oci_supervisor oci_lifecycle_transport",
        ("oci-monitor", "oci-access", "oci-store", "oci-guest", "qualification", "host-runtime", "core-cli"),
        ("native-live", "gate2"),
    ),
    (
        "_oci_stage1_kvm_proof oci_control_protocol oci_control_protocol_v2 oci_guest_filesystems oci_guest_stage1 oci_initramfs oci_process oci_stage1 oci_stage1_transport",
        ("oci-guest", "oci-monitor", "oci-access", "oci-store", "qualification", "host-runtime"),
        ("guest-binary", "guest-kvm", "native-live", "gate2"),
    ),
    ("completion inventory log_stream metrics ui", ("core-cli", "host-runtime", "build-registry"), ()),
)


class LaneError(ValueError):
    pass


def selectors(lane: str) -> tuple[str, ...]:
    files = {**PORTABLE_FILES, **SPECIAL_FILES}[lane]
    nodes = tuple(f"{path}::{name}" for path, groups in MIXED.items() for name in groups.get(lane, ()))
    return tuple(sorted((*files, *nodes)))


def validate_manifest(root: Path = ROOT) -> None:
    owners: dict[str, str] = {}
    for lane in LANES:
        for path in {**PORTABLE_FILES, **SPECIAL_FILES}[lane]:
            if path in owners or path in MIXED:
                raise LaneError(f"overlapping test file: {path}")
            owners[path] = lane
    expected = set(owners) | set(MIXED)
    actual = {
        path.relative_to(root).as_posix()
        for directory in (root / "tests", root / "hub/tests")
        for pattern in ("test_*.py", "*_test.py")
        for path in directory.rglob(pattern)
        if path.is_file()
    }
    if actual != expected:
        raise LaneError(
            f"test classification drift: unclassified={sorted(actual - expected)} missing={sorted(expected - actual)}"
        )
    for path, groups in MIXED.items():
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        actual_names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        ]
        expected_names = [name for lane, names in groups.items() for name in names if lane in LANES]
        if (
            len(expected_names) != len(set(expected_names))
            or len(actual_names) != len(set(actual_names))
            or set(actual_names) != set(expected_names)
            or any(lane not in LANES for lane in groups)
            or any(isinstance(node, ast.ClassDef) and node.name.startswith("Test") for node in tree.body)
        ):
            raise LaneError(f"mixed-module test classification drift: {path}")


def _git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=False)
    if result.returncode:
        raise LaneError("git change discovery failed; use a valid commit/ref or select lanes explicitly")
    return result.stdout


def changed_files(base: str, root: Path = ROOT) -> tuple[str, ...]:
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
    ancestor = _git(root, "merge-base", commit, "HEAD").decode().strip()
    tracked = _git(root, "diff", "--name-only", "--no-renames", "-z", ancestor, "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return tuple(
        sorted({value.decode("utf-8", "surrogateescape") for value in (tracked + untracked).split(b"\0") if value})
    )


@dataclass(frozen=True)
class Selection:
    lanes: tuple[str, ...]
    suggested: tuple[str, ...]
    reasons: tuple[str, ...]


def select_changed(paths: tuple[str, ...]) -> Selection:
    selected: set[str] = set()
    suggested: set[str] = set()
    reasons = []
    source_map = {name: (lanes, special) for names, lanes, special in DEPENDENCIES for name in names.split()}
    for path in paths:
        direct = {lane for lane in LANES if any(item.split("::", 1)[0] == path for item in selectors(lane))}
        if direct:
            selected.update(direct & set(PORTABLE))
            suggested.update(direct - set(PORTABLE))
            reasons.append(f"{path}: direct test lane")
        elif path in {"scripts/test_lanes.py", ".github/workflows/test.yml"}:
            selected.update(("core-cli", "qualification"))
            reasons.append(
                f"{path}: runner/workflow contracts; all-portable CI collection and shard validation also recommended"
            )
        elif path.startswith("docs/") or Path(path).suffix.lower() in {".md", ".rst"}:
            reasons.append(f"{path}: documentation only")
        elif path.startswith("hub/"):
            suggested.add("hub")
            reasons.append(f"{path}: separate Hub environment required")
        elif path == f"src/palimpsest_local/{Path(path).stem}.py" and Path(path).stem in source_map:
            lanes, special = source_map[Path(path).stem]
            selected.update(lanes)
            suggested.update(special)
            reasons.append(f"{path}: explicit dependency mapping (heuristic)")
        else:
            selected.update(PORTABLE)
            suggested.update(set(SPECIAL_FILES) - {"hub"})
            reasons.append(f"{path}: shared core or unknown dependency; all portable lanes")
    if not paths:
        reasons.append("No changed files; no tests selected.")
    elif not selected:
        reasons.append("No portable tests selected; special lanes are suggestions, not executed.")
    return Selection(
        tuple(lane for lane in PORTABLE if lane in selected),
        tuple(lane for lane in SPECIAL_FILES if lane in suggested),
        tuple(reasons),
    )


def expand_lanes(names: list[str]) -> tuple[str, ...]:
    if "full" in names:
        if names != ["full"]:
            raise LaneError("full is the legacy root suite; select it alone")
        return ("full",)
    result = set()
    for name in names:
        if name == "portable":
            result.update(PORTABLE)
        elif name in LANES:
            result.add(name)
        else:
            raise LaneError(f"unknown lane: {name}")
    return tuple(lane for lane in LANES if lane in result)


def parse_shard(value: str) -> tuple[int, int]:
    try:
        index, total = map(int, value.split("/"))
    except (ValueError, TypeError):
        raise LaneError("shard must be I/N, 1 <= I <= N <= 256") from None
    if not 1 <= index <= total <= 256 or value != f"{index}/{total}":
        raise LaneError("shard must be I/N, 1 <= I <= N <= 256")
    return index, total


def node_shard(key: str, total: int) -> int:
    """Hash a stable collection key, never pytest's potentially random display ID."""
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % total + 1


def collection_key(item) -> str:
    # Existing tests use uuid4() in parameter IDs. Callspec indices describe
    # logical parameter positions without the unstable value/display text.
    indices = getattr(getattr(item, "callspec", None), "indices", {})
    if any(type(name) is not str or type(index) is not int or index < 0 for name, index in indices.items()):
        raise LaneError("unsupported parametrization indices for stable test sharding")
    return json.dumps(
        [item.parent.nodeid, getattr(item, "originalname", None) or item.name, sorted(indices.items())],
        separators=(",", ":"),
    )


def pytest_addoption(parser):
    parser.addoption("--test-lane-shard", default=None, help="Internal deterministic test lane I/N shard")
    parser.addoption(
        "--test-lane-require-executed", action="store_true", help="Fail instead of accepting all-skipped proof lane"
    )


def pytest_collection_modifyitems(config, items):
    value = config.getoption("--test-lane-shard")
    index, total = parse_shard(value) if value is not None else (1, 1)
    selected, excluded = [], []
    seen = set()
    selected_keys = []
    for item in items:
        key = collection_key(item)
        if key in seen:
            raise LaneError("duplicate stable test key; explicit classification is required before sharding")
        seen.add(key)
        if node_shard(key, total) == index:
            selected.append(item)
            selected_keys.append(hashlib.sha256(key.encode()).hexdigest())
        else:
            excluded.append(item)
    config.hook.pytest_deselected(items=excluded)
    items[:] = selected
    config._test_lane_counts = (value or "unsharded", len(selected), len(excluded))
    config._test_lane_keys = selected_keys
    # Empty collection retains pytest's nonzero NO_TESTS_COLLECTED exit status.


def pytest_terminal_summary(terminalreporter):
    counts = getattr(terminalreporter.config, "_test_lane_counts", None)
    if counts is not None:
        shard, selected, excluded = counts
        terminalreporter.write_line(
            f"Lane shard {shard}: selected {selected} nodes, deselected {excluded} nodes (not a pass count)"
        )
    if terminalreporter.config.getoption("collectonly"):
        for key in getattr(terminalreporter.config, "_test_lane_keys", ()):
            terminalreporter.write_line(f"test-lane-key {key}")


def pytest_sessionfinish(session):
    if (
        session.exitstatus != 0
        or session.config.getoption("collectonly")
        or not session.config.getoption("--test-lane-require-executed")
    ):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None or not reporter.stats.get("passed"):
        session.exitstatus = 5
        if reporter is not None:
            reporter.write_line("Explicit special lane executed no passing tests; all-skipped is not qualification.")


def require_environment(lanes: tuple[str, ...], environment: dict[str, str]) -> None:
    for lane in lanes:
        for key, expected in REQUIRED_ENV.get(lane, {}).items():
            value = environment.get(key)
            if (expected is None and not value) or (expected is not None and value != expected):
                raise LaneError(
                    f"{lane} requires explicit {key}" + (f"={expected}" if expected is not None else " (nonempty)")
                )


def commands(
    lanes: tuple[str, ...], shard: str | None = None, *, collect_only: bool = False
) -> tuple[tuple[str, ...], ...]:
    if shard is not None:
        parse_shard(shard)
        if any(lane not in PORTABLE for lane in lanes):
            raise LaneError("sharding is supported only for portable lanes")
    if lanes == ("full",):
        return ((sys.executable, "-m", "pytest", "-q", *(("--collect-only",) if collect_only else ()), "tests"),)
    groups = [tuple(lane for lane in lanes if lane in PORTABLE)]
    groups.extend((lane,) for lane in lanes if lane in SPECIAL_FILES)
    result = []
    for group in groups:
        if not group:
            continue
        argv = [sys.executable, "-m", "pytest", "-q"]
        if collect_only:
            argv.append("--collect-only")
        if group == ("hub",):
            argv.extend(("-c", "hub/pyproject.toml"))
        argv.extend(("-p", "scripts.test_lanes"))
        if group[0] in SPECIAL_FILES:
            argv.append("--test-lane-require-executed")
        if shard is not None:
            argv.extend(("--test-lane-shard", shard))
        argv.extend(sorted({item for lane in group for item in selectors(lane)}))
        result.append(tuple(argv))
    return tuple(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "plan", "run"))
    parser.add_argument("lanes", nargs="*")
    parser.add_argument("--changed", metavar="BASE")
    parser.add_argument("--shard", metavar="I/N")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--collect-only", action="store_true", help="collect selected nodes without running tests")
    parser.add_argument("--check", action="store_true", help="validate every file and mixed-module test classification")
    args = parser.parse_args(argv)
    try:
        validate_manifest()
        if args.action == "list":
            if args.lanes or args.changed or args.shard or args.collect_only:
                raise LaneError("list accepts no lane/change/shard selection")
            print("Manifest valid: every test file classified; mixed functions are exact and disjoint.")
            for lane in LANES:
                print(f"{lane}: {len(selectors(lane))} selectors; " + SPECIAL_NOTES.get(lane, "portable"))
            print(
                "portable = all portable lanes; full = legacy pytest tests (original skips, not proof qualification), excluding Hub."
            )
            return 0
        if args.changed is not None and args.lanes:
            raise LaneError("choose either explicit lanes or --changed BASE")
        if args.changed is None and not args.lanes:
            raise LaneError("select lanes or --changed BASE; full-suite execution is never implicit")
        if args.changed is not None:
            selection = select_changed(changed_files(args.changed))
            lanes = selection.lanes
            for reason in selection.reasons:
                print(reason)
            for lane in selection.suggested:
                print(f"Opt-in suggestion only: {lane}. {SPECIAL_NOTES[lane]}")
        else:
            lanes = expand_lanes(args.lanes)
        print("Selected: " + (", ".join(lanes) or "none"))
        if lanes == ("full",):
            print(
                "Full uses legacy pytest tests and may SKIP opt-in proofs; it does not certify skipped qualifications."
            )
        if args.collect_only:
            print("Collection only: selected/deselected node counts are NOT passed tests.")
        for lane in lanes:
            if lane in SPECIAL_NOTES:
                print(SPECIAL_NOTES[lane] + " Existing environment and prerequisite checks are unchanged.")
        if args.action == "run" and not args.dry_run and not args.collect_only:
            require_environment(lanes, os.environ)
        for command in commands(lanes, args.shard, collect_only=args.collect_only):
            print(shlex.join(command), flush=True)
            if args.action == "run" and not args.dry_run:
                result = subprocess.run(command, cwd=ROOT, check=False)
                if result.returncode:
                    return result.returncode
        return 0
    except (LaneError, OSError, SyntaxError) as error:
        print(f"test lanes: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
