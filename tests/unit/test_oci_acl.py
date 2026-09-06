"""Exact-FD ACL commands, canonical narrow policy shapes, and DAC selection."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import oci_acl as acl

CAPABILITIES = """<capabilities><host><secmodel><model>dac</model><doi>0</doi>
<baselabel type="kvm">+107:+108</baselabel><baselabel type="qemu">+107:+108</baselabel>
</secmodel></host></capabilities>"""


def test_traversal_acl_is_exact_search_only_and_roundtrips():
    value = acl.traversal_acl(acl.baseline_acl(directory=True), 107)
    assert value.named_users == ((107, "--x"),) and value.mask == "--x"
    assert value.user == "rwx" and value.group == value.other == "---"
    assert acl.parse_acl(value.setfile_text()) == value
    assert acl.ACLStructure.from_dict(value.to_dict()) == value
    with pytest.raises(acl.OCIACLError):
        acl.traversal_acl(acl.baseline_acl(directory=False), 107)


@pytest.mark.parametrize(
    "owner,permission,mask",
    [("rw-", "--x", "--x"), ("rwx", "--x", "-wx"), ("rwx", "--x", "r-x"), ("rwx", "r-x", "r-x")],
)
def test_traversal_parser_rejects_file_execute_or_broader_masks(owner, permission, mask):
    with pytest.raises(acl.OCIACLError):
        acl.parse_acl(f"user::{owner}\nuser:107:{permission}\ngroup::---\nmask::{mask}\nother::---\n")


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("blank_tail", [False, True])
def test_acl_exact_roundtrip(directory, extended, blank_tail):
    value = acl.baseline_acl(directory=directory)
    if extended:
        value = acl.grant_acl(value, 107)
    assert acl.parse_acl(value.setfile_text() + ("\n" if blank_tail else "")) == value
    assert acl.ACLStructure.from_dict(value.to_dict()) == value
    assert value.group == value.other == "---"
    assert value.mask == ("-wx" if directory else "rw-") if extended else value.mask is None


@pytest.mark.parametrize(
    "payload",
    [
        "",
        None,
        b"user::rw-\ngroup::---\nother::---\n",
        "user::rw-\ngroup::---\nother::---",
        "user::rw-\r\ngroup::---\r\nother::---\r\n",
        "user::rw-\ngroup::---\nother::---\n\n\n",
        "user::rw-\n\ngroup::---\nother::---\n",
        "# file: secret\nuser::rw-\ngroup::---\nother::---\n",
        "user::rw-\ngroup::---\nother::---\ndefault:user::rwx\n",
        "user::rw-\ngroup:107:rw-\ngroup::---\nother::---\n",
        "user::rw-\nuser:107:rw-\nuser:107:rw-\ngroup::---\nmask::rw-\nother::---\n",
        "user::rw-\nuser:107:rw-\nuser:108:rw-\ngroup::---\nmask::rw-\nother::---\n",
        "user::rw-\nuser:0107:rw-\ngroup::---\nmask::rw-\nother::---\n",
        "user::rw-\nuser:+107:rw-\ngroup::---\nmask::rw-\nother::---\n",
        "user::rw-\nuser:qemu:rw-\ngroup::---\nmask::rw-\nother::---\n",
        "user::rw-\nuser:4294967295:rw-\ngroup::---\nmask::rw-\nother::---\n",
        "user::rw-\nuser:107:rw-\ngroup::---\nother::---\n",
        "user::rw-\nuser:107:rw-\ngroup::---\nmask::r--\nother::---\n",
        "user::rw-\nuser:107:rwx\ngroup::---\nmask::rwx\nother::---\n",
        "user::rwx\nuser:107:rw-\ngroup::---\nmask::rw-\nother::---\n",
        "user::rw-\nuser:107:rw-\t#effective:r--\ngroup::---\nmask::rw-\nother::---\n",
        "user::rw-\ngroup::r--\nother::---\n",
        "user::rw-\ngroup::---\nother::r--\n",
        "user::r--\ngroup::---\nother::---\n",
        "user::rw-\nmask::rw-\ngroup::---\nother::---\n",
        "user::rw-\ngroup::---\nmask::---\nother::---\n",
        "user::rw-\ngroup::---\nother::--- \n",
        "x" * 4096 + "\n",
    ],
)
def test_acl_rejects_noncanonical_or_unapproved_structure(payload):
    with pytest.raises(acl.OCIACLError):
        acl.parse_acl(payload)


@pytest.mark.parametrize("uid", [True, -1, 4294967295, "107", None])
def test_grant_rejects_noncanonical_uid(uid):
    with pytest.raises(acl.OCIACLError):
        acl.grant_acl(acl.baseline_acl(directory=True), uid)


def test_root_uid_and_current_uid_selection_remains_explicit_caller_policy():
    for uid in (0, os.geteuid()):
        value = acl.grant_acl(acl.baseline_acl(directory=True), uid)
        assert acl.parse_acl(value.setfile_text()) == value


def test_grant_never_extends_preexisting_acl():
    value = acl.grant_acl(acl.baseline_acl(directory=False), 107)
    with pytest.raises(acl.OCIACLError):
        acl.grant_acl(value, 108)


@pytest.mark.parametrize(
    "field,value", [("user", []), ("named_users", [(107, "-wx")]), ("mask", True), ("other", "r--"), ("group", "rwx")]
)
def test_dataclass_rejects_invalid_values(field, value):
    with pytest.raises(acl.OCIACLError):
        replace(acl.baseline_acl(directory=True), **{field: value})


def test_wire_rejects_extra_fields_and_nonlist_entries():
    value = acl.baseline_acl(directory=True).to_dict()
    for changed in ({**value, "default": []}, {**value, "named_users": "107"}, {**value, "named_users": [107]}):
        with pytest.raises(acl.OCIACLError):
            acl.ACLStructure.from_dict(changed)


@pytest.fixture
def descriptor(tmp_path):
    path = tmp_path / "console"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        yield fd
    finally:
        os.close(fd)


class FakeRunner:
    def __init__(self):
        self.value = acl.baseline_acl(directory=False)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        fd = kwargs["pass_fds"][0]
        assert argv[-1] == f"/proc/self/fd/{fd}"
        os.fstat(fd)
        if argv[0] == "/usr/bin/getfacl":
            return subprocess.CompletedProcess(argv, 0, self.value.setfile_text() + "\n", "")
        self.value = acl.parse_acl(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_commands_are_fixed_fd_only_and_whole_acl_readback_is_exact(descriptor):
    runner = FakeRunner()
    backend = acl.LinuxFdACLBackend(runner=runner)
    before = os.fstat(descriptor)
    baseline = backend.read_acl(descriptor)
    granted = acl.grant_acl(baseline, 107)
    assert backend.write_acl(descriptor, granted) == granted
    assert backend.write_acl(descriptor, baseline) == baseline
    assert os.fstat(descriptor) == before
    assert [command[0][0] for command in runner.calls] == [
        "/usr/bin/getfacl",
        "/usr/bin/setfacl",
        "/usr/bin/getfacl",
        "/usr/bin/setfacl",
        "/usr/bin/getfacl",
    ]
    for argv, kwargs in runner.calls:
        assert argv[1:-1] == (["-cpn", "--"] if "getfacl" in argv[0] else ["--no-mask", "--set-file=-", "--"])
        assert kwargs == {
            "input": None
            if "getfacl" in argv[0]
            else (granted if argv is runner.calls[1][0] else baseline).setfile_text(),
            "text": True,
            "capture_output": True,
            "check": False,
            "pass_fds": (descriptor,),
            "timeout": 10,
            "env": {"LC_ALL": "C"},
        }


@pytest.mark.parametrize(
    "result",
    [
        None,
        SimpleNamespace(returncode=0, stdout="", stderr=""),
        subprocess.CompletedProcess([], True, "", ""),
        subprocess.CompletedProcess([], 1, "", ""),
        subprocess.CompletedProcess([], 0, "", "/private/secret"),
        subprocess.CompletedProcess([], 0, b"text", ""),
        subprocess.CompletedProcess([], 0, "x" * 4097, ""),
        subprocess.CompletedProcess([], 0, "\ud800", ""),
        subprocess.CompletedProcess([], 0, "unexpected", ""),
    ],
)
def test_invalid_command_result_is_pathfree_and_never_rolled_back(descriptor, result):
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return result

    with pytest.raises(acl.OCIACLError) as error:
        acl.LinuxFdACLBackend(runner=runner).write_acl(descriptor, acl.baseline_acl(directory=False))
    assert len(calls) == 1
    assert "/private/secret" not in str(error.value)
    os.fstat(descriptor)


@pytest.mark.parametrize(
    "error",
    [OSError("secret"), subprocess.TimeoutExpired("secret", 10), UnicodeError("secret"), RuntimeError("secret")],
)
def test_command_exception_never_triggers_implicit_restore(descriptor, error):
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        raise error

    with pytest.raises(acl.OCIACLError) as raised:
        acl.LinuxFdACLBackend(runner=runner).write_acl(descriptor, acl.baseline_acl(directory=False))
    assert len(calls) == 1 and "secret" not in str(raised.value)
    os.fstat(descriptor)


def test_readback_mismatch_leaves_ambiguous_write_to_caller(descriptor):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        output = acl.baseline_acl(directory=False).setfile_text() if "getfacl" in argv[0] else ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    with pytest.raises(acl.OCIACLError, match="readback"):
        acl.LinuxFdACLBackend(runner=runner).write_acl(
            descriptor, acl.grant_acl(acl.baseline_acl(directory=False), 107)
        )
    assert len(calls) == 2


@pytest.mark.parametrize("fd", [True, -1, 0, 1, 2, "3", None, 2**64])
def test_invalid_descriptor_never_executes(fd):
    runner = FakeRunner()
    with pytest.raises(acl.OCIACLError):
        acl.LinuxFdACLBackend(runner=runner).read_acl(fd)
    assert not runner.calls


def test_default_backend_fails_closed_on_non_linux(descriptor, monkeypatch):
    monkeypatch.setattr(acl.sys, "platform", "darwin")
    with pytest.raises(acl.OCIACLError):
        acl.LinuxFdACLBackend().read_acl(descriptor)


def test_dac_parser_selects_one_kvm_identity():
    assert acl.parse_qemu_dac_baselabel(CAPABILITIES) == (107, 108)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "<!DOCTYPE capabilities><capabilities />",
        CAPABILITIES.replace("<capabilities>", "<capabilities x='1'>"),
        CAPABILITIES.replace("</capabilities>", "<host /></capabilities>"),
        CAPABILITIES.replace("</host>", "<secmodel><model>dac</model></secmodel></host>"),
        CAPABILITIES.replace("<model>dac</model>", "<model>dac</model><model>dac</model>"),
        CAPABILITIES.replace("<model>dac</model>", "<model> dac </model>"),
        CAPABILITIES.replace("<doi>0</doi>", "<doi>1</doi>"),
        CAPABILITIES.replace('type="kvm"', 'type="kvm" extra="1"'),
        CAPABILITIES.replace("</secmodel>", '<baselabel type="kvm">+109:+110</baselabel></secmodel>'),
        CAPABILITIES.replace("+107:+108", "107:108"),
        CAPABILITIES.replace("+107:+108", "+0107:+108"),
        CAPABILITIES.replace("+107:+108", "+4294967295:+108"),
        CAPABILITIES.replace('type="kvm"', 'type="xen"'),
        CAPABILITIES.replace("</secmodel>", "<unknown /></secmodel>"),
    ],
)
def test_dac_parser_rejects_missing_ambiguous_or_noncanonical_identity(payload):
    with pytest.raises(acl.OCIACLError):
        acl.parse_qemu_dac_baselabel(payload)


@pytest.mark.skipif(
    sys.platform != "linux" or not all(Path(path).is_file() for path in ("/usr/bin/getfacl", "/usr/bin/setfacl")),
    reason="requires Linux and system GNU ACL utilities",
)
def test_native_acl_roundtrip_on_owned_descriptor(descriptor):
    backend = acl.LinuxFdACLBackend()
    baseline = backend.read_acl(descriptor)
    assert baseline == acl.baseline_acl(directory=False)
    granted = acl.grant_acl(baseline, 65534)
    try:
        assert backend.write_acl(descriptor, granted) == granted
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o660
    finally:
        assert backend.write_acl(descriptor, baseline) == baseline
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o600


@pytest.mark.skipif(
    sys.platform != "linux" or not all(Path(path).is_file() for path in ("/usr/bin/getfacl", "/usr/bin/setfacl")),
    reason="requires Linux and system GNU ACL utilities",
)
def test_native_directory_grant_sets_only_exact_write_traverse_mask(tmp_path):
    path = tmp_path / "io"
    path.mkdir(mode=0o700)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    backend = acl.LinuxFdACLBackend()
    baseline = acl.baseline_acl(directory=True)
    try:
        assert backend.read_acl(descriptor) == baseline
        try:
            granted = acl.grant_acl(baseline, 65534)
            assert backend.write_acl(descriptor, granted) == granted
            assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o730
        finally:
            assert backend.write_acl(descriptor, baseline) == baseline
            assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o700
    finally:
        os.close(descriptor)
