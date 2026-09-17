"""Compatibility and architecture contract for the cloud runtime split."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import palimpsest_local.cloud_runtime as cloud_runtime
import palimpsest_local.runtime as runtime

_EXPECTED_PUBLIC_API = (
    "commit",
    "commit_run",
    "create_and_validate_overlay",
    "exec_command",
    "inspect_run",
    "logs",
    "ps",
    "receive_serial_builder_output",
    "reconcile",
    "rm",
    "run",
    "shell_command",
    "start",
    "start_serial_builder",
    "stop",
)
_LEGACY_RUNTIME_IMPORTERS: set[str] = set()
_EXPLICIT_BACKEND_OWNER_EXCEPTIONS = {
    "build.py": (
        "platforms.select_backend(",
        "cloud_runtime.start_serial_builder(",
        "cloud_runtime.receive_serial_builder_output(",
        "cloud_runtime.rm(",
    ),
    "project_adapter.py": ("lima.inspect_instance_status(name)",),
}
_FORBIDDEN_CLI_MODULES = frozenset({"runtime", "cloud_runtime", "lima"})
_ROUTING_ATTRIBUTES = frozenset({"backend", "runtime_kind", "dispatch_key"})


def _source_root() -> Path:
    return Path(__file__).parents[2] / "src" / "palimpsest_local"


def _imports_legacy_runtime(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0 and node.module == "runtime":
                return True
            if node.level > 0 and node.module is None and any(alias.name == "runtime" for alias in node.names):
                return True
            if node.level == 0 and node.module == "palimpsest_local.runtime":
                return True
            if (
                node.level == 0
                and node.module == "palimpsest_local"
                and any(alias.name == "runtime" for alias in node.names)
            ):
                return True
        elif isinstance(node, ast.Import) and any(alias.name == "palimpsest_local.runtime" for alias in node.names):
            return True
    return False


def _attribute_chain(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parent = _attribute_chain(node.value)
        return None if parent is None else (*parent, node.attr)
    return None


def _imported_module_tail(node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.rsplit(".", 1)[-1] for alias in node.names}
    module_tail = "" if node.module is None else node.module.rsplit(".", 1)[-1]
    if module_tail in _FORBIDDEN_CLI_MODULES:
        return {module_tail}
    return {alias.name for alias in node.names if alias.name in _FORBIDDEN_CLI_MODULES}


def _expression_uses_routing(node: ast.AST, aliases: set[str]) -> bool:
    for candidate in ast.walk(node):
        if isinstance(candidate, ast.Name) and candidate.id in aliases:
            return True
        chain = _attribute_chain(candidate)
        if chain is not None and any(part in _ROUTING_ATTRIBUTES for part in chain[1:]):
            return True
    return False


def _is_routing_alias_value(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    chain = _attribute_chain(node)
    return chain is not None and (chain[0] in aliases or any(part in _ROUTING_ATTRIBUTES for part in chain[1:]))


def _is_public_oci_backend_validation(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1 or not isinstance(node.body[0], ast.Raise):
        return False
    test = node.test
    raised = node.body[0].exc
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.NotIn):
        return False
    if (
        not isinstance(raised, ast.Call)
        or not isinstance(raised.func, ast.Name)
        or raised.func.id != "PalimpsestError"
        or len(raised.args) != 1
        or raised.keywords
        or not isinstance(raised.args[0], ast.Constant)
        or raised.args[0].value != "OCI-root run supports only the KVM backend"
        or node.body[0].cause is not None
    ):
        return False
    chain = _attribute_chain(test.left)
    comparator = test.comparators[0]
    return (
        chain == ("args", "backend")
        and isinstance(comparator, ast.Set)
        and {item.value for item in comparator.elts if isinstance(item, ast.Constant)} == {"auto", "kvm"}
        and len(comparator.elts) == 2
    )


def _assigned_names(node: ast.AST) -> set[str]:
    return {candidate.id for candidate in ast.walk(node) if isinstance(candidate, ast.Name)}


def _cli_architecture_violations(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    violations: list[str] = []
    routing_aliases: set[str] = set()
    run_path_aliases = {"run_paths"}
    allowed_backend_validations = {id(node.test) for node in ast.walk(tree) if _is_public_oci_backend_validation(node)}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            run_path_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "run_paths")

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                if value is not None and _is_routing_alias_value(value, routing_aliases):
                    discovered = set().union(*(_assigned_names(target) for target in targets))
                    if not discovered <= routing_aliases:
                        routing_aliases.update(discovered)
                        changed = True

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            forbidden = _imported_module_tail(node) & _FORBIDDEN_CLI_MODULES
            if forbidden:
                violations.append(f"forbidden-import:{','.join(sorted(forbidden))}:{node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "limactl" in node.value:
            violations.append(f"limactl-literal:{node.lineno}")
        elif isinstance(node, ast.Call):
            chain = _attribute_chain(node.func)
            if chain is not None and chain[-1] in run_path_aliases:
                violations.append(f"run-path-heuristic:{node.lineno}")
        elif (
            isinstance(node, ast.Compare)
            and _expression_uses_routing(node, routing_aliases)
            and id(node) not in allowed_backend_validations
        ):
            violations.append(f"routing-comparison:{node.lineno}")
        elif (
            isinstance(node, (ast.If, ast.IfExp, ast.While))
            and _expression_uses_routing(node.test, routing_aliases)
            and id(node.test) not in allowed_backend_validations
        ):
            violations.append(f"routing-control:{node.lineno}")
        elif isinstance(node, ast.Match):
            if _expression_uses_routing(node.subject, routing_aliases):
                violations.append(f"routing-match:{node.lineno}")
            for case in node.cases:
                if case.guard is not None and _expression_uses_routing(case.guard, routing_aliases):
                    violations.append(f"routing-match-guard:{case.guard.lineno}")
    return tuple(sorted(set(violations)))


def test_runtime_facade_resolves_the_legacy_public_api() -> None:
    assert runtime.__all__ == _EXPECTED_PUBLIC_API
    for name in _EXPECTED_PUBLIC_API:
        assert getattr(runtime, name) is getattr(cloud_runtime, name)
    assert runtime.commit_run is runtime.commit


def test_runtime_facade_contains_no_lifecycle_implementation_or_private_helpers() -> None:
    source = (_source_root() / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert len(imports) == 2
    future_import = next(node for node in imports if node.module == "__future__")
    facade_import = next(node for node in imports if node.module == "cloud_runtime")
    assert (future_import.level, future_import.module) == (0, "__future__")
    assert {alias.name for alias in future_import.names} == {"annotations"}
    assert (facade_import.level, facade_import.module) == (1, "cloud_runtime")
    assert {alias.asname or alias.name for alias in facade_import.names} == set(_EXPECTED_PUBLIC_API)
    assert not any(
        name.startswith("_") for name in runtime.__dict__ if name not in runtime.__all__ and not name.startswith("__")
    )


@pytest.mark.parametrize(
    "source",
    [
        "from .runtime import run",
        "from . import runtime",
        "from ..runtime import run",
        "from .. import runtime",
        "import palimpsest_local.runtime",
        "from palimpsest_local.runtime import run",
        "from palimpsest_local import runtime",
    ],
)
def test_legacy_runtime_import_detector_covers_every_supported_spelling(source: str) -> None:
    assert _imports_legacy_runtime(ast.parse(source))


def test_only_existing_compatibility_consumers_import_the_legacy_facade() -> None:
    source_root = _source_root()
    importers = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if path.name != "runtime.py" and _imports_legacy_runtime(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert importers == _LEGACY_RUNTIME_IMPORTERS


def test_build_and_project_adapter_use_the_split_owners() -> None:
    build_source = (_source_root() / "build.py").read_text(encoding="utf-8")
    adapter_source = (_source_root() / "project_adapter.py").read_text(encoding="utf-8")
    ui_source = (_source_root() / "ui.py").read_text(encoding="utf-8")

    assert "from . import cloud_runtime" in build_source
    assert "cloud_runtime.start_serial_builder(" in build_source
    assert "cloud_runtime.receive_serial_builder_output(" in build_source
    assert "cloud_runtime.rm(" in build_source
    assert "kvm.get_domain_run_id(domain)" in adapter_source
    assert "runtime._get_domain_run_id" not in adapter_source
    for operation in ("inspect_run", "start", "stop", "rm", "logs"):
        assert f"runtime_dispatch.{operation}(" in adapter_source
    assert "runtime_dispatch.run(" in adapter_source
    for operation in ("start", "stop", "rm", "logs"):
        assert f"runtime_dispatch.{operation}(" in ui_source
    assert "lima.is_lima_run(" not in adapter_source
    assert "state.read_run_state(" not in ui_source


def test_first_party_cli_and_project_create_only_through_dispatcher() -> None:
    source_root = _source_root()
    for filename in ("cli.py", "project_adapter.py"):
        tree = ast.parse((source_root / filename).read_text(encoding="utf-8"))
        forbidden: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "run"
                and isinstance(function.value, ast.Name)
                and function.value.id in {"cloud_runtime", "lima", "runtime"}
            ):
                forbidden.append(f"{function.value.id}.run")
            elif isinstance(function, ast.Name) and function.id == "run":
                forbidden.append("run")
        assert forbidden == [], (filename, forbidden)

    cli_source = (source_root / "cli.py").read_text(encoding="utf-8")
    adapter_source = (source_root / "project_adapter.py").read_text(encoding="utf-8")
    assert "runtime_dispatch.resolve_run_request(" in cli_source
    assert "runtime_dispatch.preflight_run_request(" in cli_source
    assert "runtime_dispatch.run(" in cli_source
    assert "runtime_dispatch.resolve_run_request(" in adapter_source
    assert "runtime_dispatch.preflight_run_request(" in adapter_source
    assert "runtime_dispatch.bind_run_request_volumes(" in adapter_source


def test_first_party_process_operations_use_dispatcher_sessions() -> None:
    source_root = _source_root()
    cli_source = (source_root / "cli.py").read_text(encoding="utf-8")
    assert "runtime_dispatch.exec(" in cli_source
    assert "runtime_dispatch.shell(" in cli_source
    assert "lima.exec_command(" not in cli_source
    assert "lima.shell_command(" not in cli_source
    assert "from .runtime import exec_command" not in cli_source
    assert "from .runtime import shell_command" not in cli_source

    dispatch_source = (source_root / "runtime_dispatch.py").read_text(encoding="utf-8")
    assert "adapter.exec_session(" in dispatch_source
    assert "adapter.shell_session(" in dispatch_source


def test_cli_has_no_backend_lifecycle_escape_hatches() -> None:
    source = (_source_root() / "cli.py").read_text(encoding="utf-8")
    assert _cli_architecture_violations(source) == ()
    assert "runtime_dispatch.commit(" in source


@pytest.mark.parametrize(
    "source",
    [
        'if request.dispatch_key.backend.value == "lima-vz":\n    pass',
        'route = result.record.dispatch_key\nbackend = route.backend\nif backend != "kvm":\n    pass',
        'value = 1 if args.backend == "kvm" else 2',
        'kind = result.runtime_kind\nwhile kind == "oci-root":\n    break',
        'match request.dispatch_key.backend:\n    case "kvm":\n        pass',
        'match value:\n    case _ if result.backend == "kvm":\n        pass',
        "from .lima import available as host_available\nhost_available()",
        "import palimpsest_local.cloud_runtime as adapter\nadapter.run(spec)",
        "from .state import run_paths as paths\npaths(roots, name)",
        'print("limactl shell demo")',
        'if args.backend not in {"auto", "kvm"}:\n    pass',
        'if args.backend not in {"auto", "kvm"}:\n    raise PalimpsestError("different")',
        'if args.backend not in {"auto", "kvm"}:\n    raise PalimpsestError("OCI-root run supports only the KVM backend", detail="x")',
        'if args.backend not in {"auto", "kvm"}:\n    raise PalimpsestError("OCI-root run supports only the KVM backend") from cause',
    ],
)
def test_cli_architecture_gate_rejects_nested_alias_and_control_flow_bypasses(source: str) -> None:
    assert _cli_architecture_violations(source), source


def test_narrow_non_cli_backend_owner_exceptions_remain_explicit() -> None:
    for filename, allowlist in _EXPLICIT_BACKEND_OWNER_EXCEPTIONS.items():
        source = (_source_root() / filename).read_text(encoding="utf-8")
        for allowed in allowlist:
            assert allowed in source, (filename, allowed)


def test_first_party_callers_cannot_use_the_legacy_cloud_bulk_reconcile_bypass() -> None:
    for path in _source_root().rglob("*.py"):
        if path.name in {"cloud_runtime.py", "runtime.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        assert "cloud_runtime.reconcile(" not in source, path
        assert "runtime.reconcile(" not in source, path

    dispatch_source = (_source_root() / "runtime_dispatch.py").read_text(encoding="utf-8")
    assert "lima.reconcile_run(" in dispatch_source
