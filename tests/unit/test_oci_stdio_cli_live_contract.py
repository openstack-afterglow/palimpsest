"""Portable contracts for the opt-in stdio FD diagnostic."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.kvm.test_oci_stdio_cli_live import (
    _FIELDS,
    _MAX_LINE,
    _alias_marker_count,
    _assert_security,
    _records,
    parse_probe_record,
)


def _record(role: str = "service") -> bytes:
    values = {field: "0" for field in _FIELDS}
    values["role"] = role
    for field in ("capinh", "capprm", "capeff", "capbnd", "capamb"):
        values[field] = "0000000000000000"
    return ("PALIMPSEST_STDIO_FD_V3 " + " ".join(f"{field}={values[field]}" for field in _FIELDS) + "\n").encode()


def _secure_record(uid: int = 0) -> dict[str, int | str]:
    values = parse_probe_record(_record(), expected_role="service")
    values.update(
        uid=uid,
        gid=uid,
        securebits=239,
        nnp=1,
        seccomp=2,
        stdout_alias=0o120000,
        stderr_alias=0o120000,
        fd_alias=0o120000,
        stdout_meta=1,
        stdout_target=1,
        stdout_same=1,
        stderr_meta=1,
        stderr_target=1,
        stderr_same=1,
        fd_meta=1,
        fd_target=1,
        inherited_fds=1,
        dev_entries=9,
        fd1path_same=1,
        fd2path_same=1,
        pipe_read_same=1,
        pipe_write_same=1,
        closed_fd=2,
        stdin_alias=2,
        pid1fdempty=1,
        pid1fdinfoempty=1,
        pid1root=13,
        fd1type=0o010000,
        fd2type=0o010000,
        fd1dev=1,
        fd1ino=1,
        fd2dev=1,
        fd2ino=2,
    )
    return values


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
        _record().replace(b"PALIMPSEST_STDIO_FD_V3 ", b"PALIMPSEST_STDIO_FD_V2 "),
        b"PALIMPSEST_STDIO_FD_V3 " + b"x" * _MAX_LINE + b"\n",
    ],
)
def test_probe_record_parser_rejects_missing_duplicate_oversize_type_and_range(payload: bytes) -> None:
    with pytest.raises(ValueError):
        parse_probe_record(payload, expected_role="service")


def test_probe_record_parser_rejects_the_wrong_expected_role() -> None:
    with pytest.raises(ValueError, match="unexpected diagnostic role"):
        parse_probe_record(_record("exec"), expected_role="service")


def test_security_validator_accepts_the_complete_v3_baseline_for_both_uids() -> None:
    _assert_security(_secure_record(0), 0)
    _assert_security(_secure_record(101), 101)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("fd_alias", 2),
        ("fd_meta", 0),
        ("fd_target", 0),
        ("inherited_fds", 0),
        ("dev_entries", 8),
        ("fd1path_same", 0),
        ("fd2path_same", 0),
        ("fd1path_write", 13),
        ("fd2path_write", 13),
        ("pipe_read_same", 0),
        ("pipe_write_same", 0),
        ("pipe_read", 13),
        ("pipe_write", 13),
        ("closed_fd", 0),
        ("pid1fdempty", 0),
        ("pid1fdinfoempty", 0),
    ],
)
def test_security_validator_rejects_each_new_fd_boundary_mutation(field: str, invalid: int) -> None:
    record = _secure_record()
    record[field] = invalid
    with pytest.raises(AssertionError):
        _assert_security(record, 0)


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
    assert 'exact_link("/dev/fd","/proc/self/fd")' in source
    assert '"/dev/stdin"' in source and '"/dev/fd"' in source
    assert "inherited_fd_inventory()" in source and "seen==((1ul<<0)|(1ul<<1)|(1ul<<2)|(1ul<<directory))" in source
    assert "exact_dev_inventory()" in source and "seen==0x1ff" in source
    assert "SYS_pipe2" in source and "pipe_read_same" in source and "pipe_write_same" in source
    assert "closed_fd" in source and "descriptor<0?-descriptor:0" in source
    assert "SYS_fstatfs" in source and "filesystem.type!=TMPFS_MAGIC" in source
    assert "filesystem.flags&ST_RDONLY" in source


def test_alias_marker_count_normalizes_only_complete_console_crlf_lines() -> None:
    marker = b"PALIMPSEST_STDIO_ALIAS_V3 service stdout"
    assert _alias_marker_count(marker + b"\n", role="service", stream="stdout", console=True) == 1
    assert _alias_marker_count(marker + b"\r\n", role="service", stream="stdout", console=True) == 1
    assert _alias_marker_count(marker, role="service", stream="stdout", console=True) == 0
    assert _alias_marker_count(marker + b"\r", role="service", stream="stdout", console=True) == 0


def test_alias_marker_count_exposes_duplicate_missing_and_cross_stream_output() -> None:
    stdout = b"PALIMPSEST_STDIO_ALIAS_V3 exec stdout\n"
    stderr = b"PALIMPSEST_STDIO_ALIAS_V3 exec stderr\n"
    assert _alias_marker_count(b"", role="exec", stream="stdout") == 0
    assert _alias_marker_count(stdout + stdout, role="exec", stream="stdout") == 2
    assert _alias_marker_count(stderr, role="exec", stream="stdout") == 0
    assert _alias_marker_count(stdout, role="exec", stream="stderr") == 0
