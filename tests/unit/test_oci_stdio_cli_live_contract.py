"""Portable contracts for the opt-in stdio FD diagnostic."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.kvm.test_oci_stdio_cli_live import (
    _FIELDS,
    _MAX_LINE,
    _alias_marker_count,
    _records,
    parse_probe_record,
)


def _record(role: str = "service") -> bytes:
    values = {field: "0" for field in _FIELDS}
    values["role"] = role
    for field in ("capinh", "capprm", "capeff", "capbnd", "capamb"):
        values[field] = "0000000000000000"
    return ("PALIMPSEST_STDIO_FD_V2 " + " ".join(f"{field}={values[field]}" for field in _FIELDS) + "\n").encode()


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
        b"PALIMPSEST_STDIO_FD_V2 " + b"x" * _MAX_LINE + b"\n",
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


def test_probe_source_pins_exact_aliases_and_nginx_compatible_bounded_writes() -> None:
    source = (Path(__file__).parents[1] / "kvm" / "assets" / "stdio-fd-probe.c").read_text()
    assert 'exact_link("/dev/stdout","/proc/self/fd/1")' in source
    assert 'exact_link("/dev/stderr","/proc/self/fd/2")' in source
    assert "(metadata->mode&07777)==0777" in source
    assert "metadata->uid==0&&metadata->gid==0&&metadata->nlink==1" in source
    assert "O_WRONLY|O_APPEND|O_CREAT|O_NONBLOCK|O_NOCTTY,0666" in source
    assert "O_TRUNC" not in source
    assert "if(same1)write1=write_result" in source
    assert "if(same2)write2=write_result" in source
    assert '"/dev/stdin"' in source and '"/dev/fd"' in source


def test_alias_marker_count_normalizes_only_complete_console_crlf_lines() -> None:
    marker = b"PALIMPSEST_STDIO_ALIAS_V2 service stdout"
    assert _alias_marker_count(marker + b"\n", role="service", stream="stdout", console=True) == 1
    assert _alias_marker_count(marker + b"\r\n", role="service", stream="stdout", console=True) == 1
    assert _alias_marker_count(marker, role="service", stream="stdout", console=True) == 0
    assert _alias_marker_count(marker + b"\r", role="service", stream="stdout", console=True) == 0


def test_alias_marker_count_exposes_duplicate_missing_and_cross_stream_output() -> None:
    stdout = b"PALIMPSEST_STDIO_ALIAS_V2 exec stdout\n"
    stderr = b"PALIMPSEST_STDIO_ALIAS_V2 exec stderr\n"
    assert _alias_marker_count(b"", role="exec", stream="stdout") == 0
    assert _alias_marker_count(stdout + stdout, role="exec", stream="stdout") == 2
    assert _alias_marker_count(stderr, role="exec", stream="stdout") == 0
    assert _alias_marker_count(stdout, role="exec", stream="stderr") == 0
