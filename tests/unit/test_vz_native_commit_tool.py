from __future__ import annotations

import os
import stat

import pytest

from palimpsest_local import vz_native_runtime
from palimpsest_local.oci_commit import CommitError

VAR = "PALIMPSEST_TEST_TOOL"


def tool(tmp_path, mode=0o755):
    path = tmp_path / "debugfs"
    path.write_bytes(b"#!/bin/sh\n")
    os.chmod(path, mode)
    return path


def test_a_missing_required_tool_is_refused_and_an_optional_one_is_absent(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    with pytest.raises(CommitError) as raised:
        vz_native_runtime._commit_tool(VAR, required=True)
    assert raised.value.code == "commit-tool"
    assert vz_native_runtime._commit_tool(VAR, required=False) is None


def test_an_owned_executable_that_others_cannot_write_is_accepted(tmp_path, monkeypatch):
    path = tool(tmp_path)
    monkeypatch.setenv(VAR, str(path))
    assert vz_native_runtime._commit_tool(VAR, required=True) == path


@pytest.mark.parametrize(
    "mode",
    [0o775, 0o757, 0o644],
    ids=["group-writable", "world-writable", "not-executable"],
)
def test_a_writable_or_non_executable_tool_is_refused(tmp_path, monkeypatch, mode):
    monkeypatch.setenv(VAR, str(tool(tmp_path, mode)))
    with pytest.raises(CommitError) as raised:
        vz_native_runtime._commit_tool(VAR, required=True)
    assert raised.value.code == "commit-tool"


def test_relative_missing_and_directory_paths_are_refused(tmp_path, monkeypatch):
    for value in ("debugfs", str(tmp_path / "absent"), str(tmp_path)):
        monkeypatch.setenv(VAR, value)
        with pytest.raises(CommitError):
            vz_native_runtime._commit_tool(VAR, required=True)
    assert stat.S_ISDIR(os.stat(tmp_path).st_mode)


def test_platform_bound_identity_files_are_always_treated_as_generated():
    assert set(vz_native_runtime._COMMIT_GENERATED_PLACEHOLDERS) == {
        "etc/machine-id",
        "var/lib/dbus/machine-id",
        "etc/hostname",
        "etc/resolv.conf",
    }
