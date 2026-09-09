"""Portable contracts for the opt-in stdio FD diagnostic."""

from __future__ import annotations

import pytest

from tests.kvm.test_oci_stdio_cli_live import _FIELDS, _MAX_LINE, _records, parse_probe_record


def _record(role: str = "service") -> bytes:
    values = {field: "0" for field in _FIELDS}
    values["role"] = role
    for field in ("capinh", "capprm", "capeff", "capbnd", "capamb"):
        values[field] = "0000000000000000"
    return ("PALIMPSEST_STDIO_FD_V1 " + " ".join(f"{field}={values[field]}" for field in _FIELDS) + "\n").encode()


def test_probe_record_parser_accepts_only_the_fixed_ordered_schema() -> None:
    parsed = parse_probe_record(_record(), expected_role="service")
    assert tuple(parsed) == tuple(_FIELDS)
    assert parsed["role"] == "service" and parsed["capbnd"] == parsed["fd1ino"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        _record()[:-1],
        _record().replace(b" role=service", b" role=wrong"),
        _record().replace(b" role=service", b" role=service role=service"),
        _record().replace(b" uid=0", b" uid=-1"),
        _record().replace(b" uid=0", b" uid=01"),
        _record().replace(b" uid=0", b" uid=18446744073709551616"),
        _record().replace(b" capinh=0000000000000000", b" capinh=0"),
        _record().replace(b" uid=0", b" uid=[]"),
        b"PALIMPSEST_STDIO_FD_V1 " + b"x" * _MAX_LINE + b"\n",
    ],
)
def test_probe_record_parser_rejects_missing_duplicate_oversize_type_and_range(payload: bytes) -> None:
    with pytest.raises(ValueError):
        parse_probe_record(payload, expected_role="service")


def test_probe_record_parser_rejects_the_wrong_expected_role() -> None:
    with pytest.raises(ValueError, match="unexpected diagnostic role"):
        parse_probe_record(_record("exec"), expected_role="service")


def test_completed_exec_extraction_requires_the_original_single_lf() -> None:
    assert _records(_record("exec"), role="exec")[0]["role"] == "exec"
    for invalid in (_record("exec")[:-1], _record("exec")[:-1] + b"\r", _record("exec")[:-1] + b"\r\r\n"):
        with pytest.raises(ValueError):
            _records(invalid, role="exec")


def test_console_extraction_accepts_crlf_and_ignores_only_an_incomplete_tail() -> None:
    complete = _record().replace(b"\n", b"\r\n")
    records = _records(b"unrelated console line\r\n" + complete + _record()[:-7], role="service", partial_tail=True)
    assert len(records) == 1 and records[0]["role"] == "service"
