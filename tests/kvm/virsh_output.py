"""Bounded parsers for allowlisted virsh inventory output used by live proofs."""

from __future__ import annotations

import uuid

_MAX_VIRSH_INVENTORY_BYTES = 1024 * 1024
_MAX_VIRSH_INVENTORY_RECORDS = 65536


def virsh_inventory_lines(output: bytes, *, encoding: str) -> list[str]:
    assert len(output) <= _MAX_VIRSH_INVENTORY_BYTES
    if output in (b"", b"\n"):
        return []
    assert output.endswith(b"\n")
    text = output.decode(encoding)
    records = text[:-1]
    if records.endswith("\n"):
        records = records[:-1]
        assert records
    lines = records.split("\n")
    assert len(lines) <= _MAX_VIRSH_INVENTORY_RECORDS
    assert all(line and all(character.isprintable() for character in line) for line in lines)
    return lines


def virsh_uuid_inventory(output: bytes) -> list[str]:
    identifiers = virsh_inventory_lines(output, encoding="ascii")
    for identifier in identifiers:
        parsed = uuid.UUID(identifier)
        assert str(parsed) == identifier
    assert len(set(identifiers)) == len(identifiers)
    return identifiers


def virsh_single_inventory_value(output: bytes, *, encoding: str, expected: str) -> str:
    values = virsh_inventory_lines(output, encoding=encoding)
    assert values == [expected]
    return values[0]


def virsh_single_uuid(output: bytes, *, expected: str) -> str:
    identifier = virsh_single_inventory_value(output, encoding="ascii", expected=expected)
    parsed = uuid.UUID(identifier)
    assert str(parsed) == identifier
    return identifier
