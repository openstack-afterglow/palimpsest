import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_PROOF_PATH = ROOT / "tests/kvm/test_oci_console_ofd_live.py"
_PACKAGE_NAME = "console_ofd_live_contract_fixture"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_PROOF_PATH.parent)]
sys.modules[_PACKAGE_NAME] = _PACKAGE
_SPEC = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.proof", _PROOF_PATH)
assert _SPEC is not None and _SPEC.loader is not None
proof = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = proof
_SPEC.loader.exec_module(proof)
_completed_probe_lines = proof._completed_probe_lines
_finalize_probe = proof._finalize_probe


def test_console_ofd_probe_is_test_only_and_contract_is_bounded() -> None:
    probe = (ROOT / "tests/kvm/assets/console-ofd-probe.c").read_text()
    test = (ROOT / "tests/kvm/test_oci_console_ofd_live.py").read_text()
    for value in (
        '"/proc/self/fd/1"',
        "O_NOFOLLOW",
        "-ELOOP",
        "F_GETFL",
        "F_GETFD",
        'mounted("proc","/proc","proc",14',
        'mounted("sysfs","/sys","sysfs",14',
        'mounted("devtmpfs","/dev","devtmpfs",10',
    ):
        assert value in probe
    assert "time.monotonic() + 20" in test
    assert '"128"' in test and '"-smp"' in test and '"none"' in test
    assert "guest/stage1/init.c" not in test


def test_console_ofd_line_parser_is_exact_bounded_and_ignores_partial_tail() -> None:
    assert _completed_probe_lines(b"one\r\ntwo\npartial") == (b"one", b"two")
    with pytest.raises(ValueError, match="byte limit"):
        _completed_probe_lines(b"x" * (1024 * 1024 + 1))


def test_probe_prepare_flags_match_production_prepare_live() -> None:
    source = (ROOT / "guest/stage1/init.c").read_text()
    body = source[source.index("static int prepare_live(void)") : source.index("#define MOUNTINFO_MAX")]
    assert 'mount_ok("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC' in body
    assert 'mount_ok("sysfs", "/sys", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC' in body
    assert 'mount_ok("devtmpfs", "/dev", "devtmpfs", MS_NOSUID | MS_NOEXEC' in body


def test_finalize_reaps_and_writes_evidence_despite_close_and_kill_races(tmp_path, monkeypatch) -> None:
    calls = []

    class Selector:
        def close(self):
            calls.append("selector")
            raise OSError("selector")

    class Stream:
        def close(self):
            calls.append("stream")

    class Process:
        pid = 123
        stdout = Stream()

        def poll(self):
            calls.append("poll")
            return None

        def wait(self, timeout):
            calls.append(("wait", timeout))
            return 0

    def killpg(pid, sig):
        calls.append(("kill", pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(proof.os, "killpg", killpg)
    error = _finalize_probe(Process(), Selector(), tmp_path, bytearray(b"proof"))
    assert str(error) == "selector"
    assert calls == ["selector", "poll", ("kill", 123, 15), ("wait", 3), "stream"]
    assert (tmp_path / "console.bin").read_bytes() == b"proof"


def test_initramfs_has_the_prepare_live_directory_skeleton() -> None:
    test = (ROOT / "tests/kvm/test_oci_console_ofd_live.py").read_text()
    for path in ("dev", "proc", "sys"):
        assert f'NewcEntry("{path}", stat.S_IFDIR | 0o755, b"")' in test
