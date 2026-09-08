"""Portable naming and routing contracts for the opt-in public exec proof."""

from __future__ import annotations

import importlib.util
import re
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

from palimpsest_local.oci_run_request import LocalOCIRunRequest

_PROOF_PATH = Path(__file__).resolve().parents[1] / "kvm" / "test_oci_exec_cli_live.py"
_PACKAGE_NAME = "exec_cli_live_contract_fixture"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_PROOF_PATH.parent)]
sys.modules[_PACKAGE_NAME] = _PACKAGE
_SPEC = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.proof", _PROOF_PATH)
assert _SPEC is not None and _SPEC.loader is not None
proof = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = proof
_SPEC.loader.exec_module(proof)


def test_fresh_target_uses_one_canonical_suffix_for_parent_and_run_name(monkeypatch):
    fixed = uuid.UUID("01234567-89ab-cdef-0123-456789abcdef")
    monkeypatch.setattr(proof.uuid, "uuid4", lambda: fixed)

    target = proof._fresh_exec_cli_target()

    assert target.parent == Path("/tmp/p-execcli-01234567")
    assert target.name == "exec-cli-01234567"
    assert re.fullmatch(r"exec-cli-[0-9a-f]{8}", target.name)
    assert target.parent.name.removeprefix("p-execcli-") == target.name.removeprefix("exec-cli-")
    assert LocalOCIRunRequest(name=target.name, source=Path("/tmp/image.oci.tar")).name == target.name


def test_every_public_operation_and_domain_lookup_routes_the_fresh_name(monkeypatch):
    fixed = uuid.UUID("89abcdef-0123-4567-89ab-cdef01234567")
    monkeypatch.setattr(proof.uuid, "uuid4", lambda: fixed)
    target = proof._fresh_exec_cli_target()
    environment = {"PATH": "/usr/bin"}
    archive = Path("/tmp/image.oci.tar")
    cli_calls = []
    virsh_calls = []
    result = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def fake_cli(actual_environment, *arguments, timeout=90):
        cli_calls.append((actual_environment, arguments, timeout))
        return result

    def fake_run(arguments, **options):
        virsh_calls.append((arguments, options))
        return result

    monkeypatch.setattr(proof, "_cli", fake_cli)
    monkeypatch.setattr(proof.subprocess, "run", fake_run)

    assert target.launch(environment, archive) is result
    assert target.exec_status(environment) is result
    assert target.root_proof(environment) is result
    assert target.execute(environment, "/bin/sh", "-c", "cat /proc/1/root/marker") is result
    assert target.stop(environment) is result
    assert target.remove(environment) is result
    assert target.domain_info(environment, "/usr/bin/virsh") is result

    assert cli_calls == [
        (environment, ("run", archive, "--name", target.name, "-d"), 180),
        (environment, ("oci", "exec-status", target.name), 90),
        (environment, ("oci", "root-proof", target.name), 90),
        (environment, ("exec", target.name, "--", "/bin/sh", "-c", "cat /proc/1/root/marker"), 60),
        (environment, ("stop", target.name), 60),
        (environment, ("rm", target.name), 60),
    ]
    assert virsh_calls == [
        (
            ["/usr/bin/virsh", "-c", "qemu:///system", "dominfo", target.name],
            {
                "env": environment,
                "capture_output": True,
                "check": False,
                "timeout": 15,
            },
        )
    ]
    assert target.run_state == Path("/tmp/p-execcli-89abcdef/state/runs/exec-cli-89abcdef")
    assert all("exec-cli" not in arguments for _, arguments, _ in cli_calls)
    assert "exec-cli" not in virsh_calls[0][0]
