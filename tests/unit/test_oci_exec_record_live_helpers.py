from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HELPER = Path(__file__).parents[1] / "kvm" / "virsh_output.py"
_SPEC = importlib.util.spec_from_file_location("oci_exec_record_virsh_output", _HELPER)
assert _SPEC is not None and _SPEC.loader is not None
helpers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helpers)


@pytest.mark.parametrize("terminator", [b"\n", b"\n\n"])
def test_single_uuid_accepts_one_optional_extra_terminal_lf(terminator):
    identifier = "49bd618f-1a3e-4cd8-b436-58c194efd791"
    assert helpers.virsh_single_uuid(identifier.encode("ascii") + terminator, expected=identifier) == identifier


@pytest.mark.parametrize("terminator", [b"\n", b"\n\n"])
def test_single_name_accepts_one_optional_extra_terminal_lf(terminator):
    name = "exec-record-cli-5376e48a"
    assert helpers.virsh_single_inventory_value(name.encode() + terminator, encoding="utf-8", expected=name) == name


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (b"not-a-uuid\n", "not-a-uuid"),
        (b"49BD618F-1A3E-4CD8-B436-58C194EFD791\n", "49BD618F-1A3E-4CD8-B436-58C194EFD791"),
        (b"49bd618f-1a3e-4cd8-b436-58c194efd791\n", "00000000-0000-4000-8000-000000000000"),
    ],
)
def test_single_uuid_rejects_malformed_noncanonical_or_wrong_values(output, expected):
    with pytest.raises((AssertionError, ValueError)):
        helpers.virsh_single_uuid(output, expected=expected)


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"\n",
        b"expected\nother\n",
        b"expected\nother\n\n",
        b"expected \n",
        b" expected\n",
        b"expected\t\n",
        b"expected\n\n\n",
        b"expected",
        b"wrong\n",
        b"\xff\n",
        b"x" * (1024 * 1024 + 1),
    ],
)
def test_single_name_rejects_missing_extra_nonexact_or_unbounded_values(output):
    with pytest.raises((AssertionError, UnicodeDecodeError)):
        helpers.virsh_single_inventory_value(output, encoding="utf-8", expected="expected")
