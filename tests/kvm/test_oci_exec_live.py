"""Real guest exec engine proof before opening the public EXEC capability.

Launch/STOP/rm are public CLI operations; exec uses the production process
session directly while dispatch remains gated. No fixture ACLs, guest output
substitution, path remapping, or private launch pipeline is permitted.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import threading
import uuid
from pathlib import Path

import pytest

from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_exec_session import OCIExecProcessSession, exec_session
from palimpsest_local.oci_run_cleanup import _read_run_journal, load_oci_run_binding
from palimpsest_local.runtime_types import ExecRequest, ProcessOutputEvent, ProcessStatusEvent, ProcessStream

from .test_oci_public_cli_live import _cli, _success

pytestmark = [
    pytest.mark.kvm,
    pytest.mark.skipif(
        os.environ.get("PALIMPSEST_OCI_EXEC_LIVE") != "1",
        reason="set PALIMPSEST_OCI_EXEC_LIVE=1 on the qualified native OCI host",
    ),
]


def _session(roots, name, argv, *, timeout_ms=30000):
    expected = state.read_run_ledger_snapshot(roots, name).record
    if timeout_ms == 30000:
        return exec_session(name, ExecRequest(tuple(argv)), roots=roots, _expected_record=expected)
    binding = load_oci_run_binding(roots, name)
    assert binding.record == expected
    with state.locked_existing_run(roots, name, expected=expected) as mutation:
        endpoint = _read_run_journal(mutation, binding).endpoint
    return OCIExecProcessSession(roots, binding, endpoint, tuple(argv), timeout_ms=timeout_ms)


def _collect(session):
    stdout, stderr, result = bytearray(), bytearray(), None
    try:
        for event in session.events():
            if isinstance(event, ProcessOutputEvent):
                assert result is None
                (stderr if event.stream is ProcessStream.STDERR else stdout).extend(event.data)
            else:
                assert isinstance(event, ProcessStatusEvent) and result is None
                result = event.result
        assert result == session.wait() and result is not None
        return bytes(stdout), bytes(stderr), result
    finally:
        session.close()


def test_real_guest_exec_streams_limits_descendants_and_concurrent_stop():
    assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
    artifact_value = os.environ.get("PALIMPSEST_OCI_EXEC_LIVE_IMAGE")
    assert artifact_value, "PALIMPSEST_OCI_EXEC_LIVE_IMAGE must name a bootable BusyBox OCI archive"
    archive = Path(artifact_value).resolve(strict=True)
    assert archive.is_file() and archive.name.endswith((".oci.tar", ".oci"))
    before_archive = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = json.loads(archive.with_name("acceptance.json").read_text())
    assert receipt["schema"] == "palimpsest.oci-root-build-run-acceptance.v1"
    assert receipt["archive_sha256"] == "sha256:" + before_archive
    marker = receipt["marker"]
    assert type(marker) is str and re.fullmatch("palimpsest-local-build-[0-9a-f]{32}", marker)
    parent = Path("/tmp") / ("p-exec-" + uuid.uuid4().hex[:8])
    assert not parent.exists()
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONNOUSERSITE"] = "1"
    print(f"native OCI exec evidence: {parent}", flush=True)
    _success(_cli(environment, "oci", "init-runtime", parent))
    owned = parent.lstat()
    environment["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    environment["XDG_CONFIG_HOME"] = str(parent / "config")
    roots = state.resolve_roots(environment)
    name = "native-exec"
    completed = False
    results = []
    try:
        launched = _cli(environment, "run", archive, "--name", name, "-d", timeout=180)
        (parent / "launch.stdout").write_bytes(launched.stdout)
        (parent / "launch.stderr").write_bytes(launched.stderr)
        _success(launched)
        assert launched.stdout == (name + "\n").encode()

        def execute(script, *, timeout_ms=30000):
            output, error, result = _collect(_session(roots, name, ("/bin/sh", "-c", script), timeout_ms=timeout_ms))
            results.append(
                {
                    "script": script,
                    "stdout_hex": output.hex(),
                    "stderr_hex": error.hex(),
                    "returncode": result.returncode,
                }
            )
            (parent / "exec-results.json").write_text(json.dumps(results, indent=2) + "\n")
            assert state.read_run_ledger_snapshot(roots, name).state["status"] == "running"
            return output, error, result

        output, error, result = execute("printf 'native-stdout'; printf 'native-stderr' >&2; exit 17")
        assert (output, error, result.returncode) == (b"native-stdout", b"native-stderr", 17)
        output, error, result = execute("printf 'second-exec'; exit 0")
        assert (output, error, result.returncode) == (b"second-exec", b"", 0)
        output, error, result = execute(
            f'test "$(cat /palimpsest-e2e-root-marker)" = {marker} && '
            f'test "$(cat /proc/self/root/palimpsest-e2e-root-marker)" = {marker} && '
            "printf 'native-image-root'"
        )
        assert (output, error, result.returncode) == (b"native-image-root", b"", 0)
        output, error, result = execute(
            "(printf 'descendant-drained'; sleep 30) & child=$!; "
            "printf '%s' \"$child\" >/tmp/palimpsest-exec-descendant-pid; sleep 0.1; exit 0"
        )
        assert (output, error, result.returncode) == (b"descendant-drained", b"", 0)
        output, error, result = execute(
            'test ! -e "/proc/$(cat /tmp/palimpsest-exec-descendant-pid)" && printf descendant-reaped'
        )
        assert (output, error, result.returncode) == (b"descendant-reaped", b"", 0)

        with pytest.raises(StateError, match="timeout"):
            execute("sleep 5", timeout_ms=100)
        output, error, result = execute("printf 'after-timeout'")
        assert (output, error, result.returncode) == (b"after-timeout", b"", 0)
        with pytest.raises(StateError, match="output-limit"):
            execute("while :; do printf '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ\n'; done")
        output, error, result = execute("printf 'after-output-limit'")
        assert (output, error, result.returncode) == (b"after-output-limit", b"", 0)

        # An actual additional process must have started before the independent
        # public STOP client races its lifecycle worker. No main-console marker.
        started = threading.Event()
        observed = {"stdout": bytearray(), "failure": None}
        pending = _session(roots, name, ("/bin/sh", "-c", "printf 'exec-is-running'; sleep 30"))

        def consume():
            try:
                for event in pending.events():
                    if isinstance(event, ProcessOutputEvent) and event.stream is ProcessStream.STDOUT:
                        observed["stdout"].extend(event.data)
                        if b"exec-is-running" in observed["stdout"]:
                            started.set()
                    elif isinstance(event, ProcessStatusEvent):
                        observed["failure"] = AssertionError("STOP must cancel the outstanding exec")
            except BaseException as exc:
                observed["failure"] = exc
            finally:
                pending.close()

        consumer = threading.Thread(target=consume, name="native-exec-proof")
        consumer.start()
        try:
            assert started.wait(10), "additional guest process did not produce its start output"
            stopped = _cli(environment, "stop", name, timeout=60)
            (parent / "stop.stdout").write_bytes(stopped.stdout)
            (parent / "stop.stderr").write_bytes(stopped.stderr)
            _success(stopped)
        finally:
            consumer.join(45)
        assert not consumer.is_alive(), "bounded exec consumer remained after STOP"
        assert isinstance(observed["failure"], StateError) and "cancelled" in str(observed["failure"]), observed[
            "failure"
        ]
        _success(_cli(environment, "rm", name, timeout=60))
        assert not (roots.runs / name).exists()
        assert archive.is_file() and hashlib.sha256(archive.read_bytes()).hexdigest() == before_archive
        completed = True
    finally:
        if completed:
            visible = parent.lstat()
            assert stat.S_ISDIR(visible.st_mode) and visible.st_uid == os.geteuid()
            assert (visible.st_dev, visible.st_ino) == (owned.st_dev, owned.st_ino)
            shutil.rmtree(parent)
        else:
            print(f"native OCI exec failure evidence preserved: {parent}", flush=True)
