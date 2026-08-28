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
_LEGACY_RUNTIME_IMPORTERS = {
    "cli.py",
}


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


def test_first_party_callers_cannot_use_the_legacy_cloud_bulk_reconcile_bypass() -> None:
    for path in _source_root().rglob("*.py"):
        if path.name in {"cloud_runtime.py", "runtime.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        assert "cloud_runtime.reconcile(" not in source, path
        assert "runtime.reconcile(" not in source, path

    dispatch_source = (_source_root() / "runtime_dispatch.py").read_text(encoding="utf-8")
    assert "lima.reconcile_run(" in dispatch_source
