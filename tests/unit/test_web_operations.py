"""Real owned subprocesses exercise job completion, bounds and shutdown.

The executable fixture replaces only the external CLI boundary. It does not
read/echo forwarded arguments or claim a BuildKit/VM execution proof.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

from palimpsest_local import web_operations
from palimpsest_local.errors import StateError
from palimpsest_local.state import StatePaths


@pytest.fixture
def manager():
    current = web_operations.OperationManager()
    yield current
    current.close_all()


def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> StatePaths:
    helper = tmp_path / "owned-client"
    helper.write_text(f"#!{sys.executable}\n{source}\n", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setattr(web_operations.sys, "executable", str(helper))
    return StatePaths(tmp_path / "config" / "palimpsest", tmp_path / "state")


def finished(manager, operation_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        item = next(item for item in manager.list() if item["id"] == operation_id)
        if item["status"] != "running":
            return item
        time.sleep(0.01)
    pytest.fail("owned operation did not finish")


def test_output_tail_is_bounded_and_preserves_true_failed_completion(manager, tmp_path, monkeypatch):
    roots = client(
        tmp_path, monkeypatch, "import os\nos.write(1, b'A' * 300000 + b'\\nLAST-OUTPUT\\n')\nraise SystemExit(7)"
    )
    opened = manager.start(roots, "build", [])
    item = finished(manager, opened["id"])
    assert item["status"] == "failed"
    assert item["returncode"] == 7
    assert item["output_truncated"] is True
    assert len(item["output"].encode()) == manager.MAX_OUTPUT_BYTES
    assert item["output"].endswith("\nLAST-OUTPUT\n")
    assert not manager.has_active()


def test_shutdown_reaps_active_clients_and_rejects_late_work(manager, tmp_path, monkeypatch):
    cancellation = tmp_path / "startup-cancelled"
    source = (
        "import os, time, signal\nfrom pathlib import Path\n"
        f"signal.signal(signal.SIGTERM, lambda *_: Path({str(cancellation)!r}).write_text('cancelled'))\n"
        "print(os.getpid(), flush=True)\ntime.sleep(60)"
    )
    roots = client(tmp_path, monkeypatch, source)
    first = manager.start(roots, "run", [], resource_name="one")
    second = manager.start(roots, "run", [], resource_name="two")
    with pytest.raises(StateError):
        manager.start(roots, "build", [])
    deadline = time.monotonic() + 8
    while any(not item["output"].strip() for item in manager.list()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    pids = [int(item["output"]) for item in manager.list()]
    manager.close_all()
    assert not cancellation.exists(), "shutdown entered the run client's startup-cancellation handler"
    for operation_id in (first["id"], second["id"]):
        item = finished(manager, operation_id)
        assert item["status"] == "failed" and item["returncode"] == -signal.SIGUSR1
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    with pytest.raises(StateError):
        manager.start(roots, "build", [])


def test_completed_retention_replaces_oldest_completed_job(manager, tmp_path, monkeypatch):
    roots = client(tmp_path, monkeypatch, "raise SystemExit(0)")
    manager.MAX_RETAINED = 2
    first = manager.start(roots, "build", [])
    assert finished(manager, first["id"])["status"] == "succeeded"
    second = manager.start(roots, "build", [])
    assert finished(manager, second["id"])["status"] == "succeeded"
    third = manager.start(roots, "build", [])
    assert finished(manager, third["id"])["status"] == "succeeded"
    assert [item["id"] for item in manager.list()] == [third["id"], second["id"]]


def test_build_shutdown_closes_term_ignoring_descendant_listener(manager, tmp_path, monkeypatch):
    child = (
        "import os, signal, socket, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()\n"
        "print(os.getpid(), listener.getsockname()[1], flush=True)\ntime.sleep(60)"
    )
    roots = client(
        tmp_path,
        monkeypatch,
        (f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-u', '-c', {child!r}])\ntime.sleep(60)"),
    )
    opened = manager.start(roots, "build", [])
    deadline = time.monotonic() + 8
    while not manager.list()[0]["output"].strip():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    child_pid, port = map(int, manager.list()[0]["output"].split())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        manager.close_all()
        assert finished(manager, opened["id"])["status"] == "failed"
        deadline = time.monotonic() + 3
        while True:
            with socket.socket() as connection:
                if connection.connect_ex(("127.0.0.1", port)) != 0:
                    break
            assert time.monotonic() < deadline, "owned build descendant still serves after shutdown"
            time.sleep(0.01)
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_run_preparation_shutdown_reaps_separate_session_and_scratch(manager, tmp_path, monkeypatch):
    ready = tmp_path / "worker-ready.json"
    stopped_vm = tmp_path / "vm-stop-requested"
    worker = (
        "import json, os, signal, socket, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()\n"
        f"Path({str(ready)!r}).write_text(json.dumps({{'pid': os.getpid(), 'port': listener.getsockname()[1], 'scratch': os.environ['HOME']}}))\n"
        "time.sleep(60)"
    )
    roots = client(
        tmp_path,
        monkeypatch,
        (
            "import signal, sys, tempfile\nfrom pathlib import Path\n"
            "from palimpsest_local.web_job_client import cancellable_run_preparation\n"
            "from palimpsest_local.registry_intake import _run_bounded_copy\n"
            f"signal.signal(signal.SIGTERM, lambda *_: Path({str(stopped_vm)!r}).write_text('STOP'))\n"
            "with cancellable_run_preparation():\n"
            f"    with tempfile.TemporaryDirectory(dir={str(tmp_path)!r}, prefix='preparation-') as scratch:\n"
            f"        _run_bounded_copy((sys.executable, '-u', '-c', {worker!r}), Path(scratch) / 'image.oci.tar', Path(scratch), timeout_seconds=60, maximum_bytes=1048576)\n"
        ),
    )
    opened = manager.start(roots, "run", [], resource_name="not-a-vm")
    deadline = time.monotonic() + 8
    while not ready.exists():
        assert time.monotonic() < deadline, "owned preparation worker did not start"
        time.sleep(0.01)
    import json

    receipt = json.loads(ready.read_text())
    try:
        with socket.create_connection(("127.0.0.1", receipt["port"]), timeout=1):
            pass
        manager.close_all()
        outcome = finished(manager, opened["id"])
        assert outcome["status"] == "failed" and outcome["returncode"] == 128 + signal.SIGUSR1
        assert not stopped_vm.exists(), "preparation cancellation issued a VM STOP"
        assert not Path(receipt["scratch"]).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(receipt["pid"], 0)
        with socket.socket() as connection:
            assert connection.connect_ex(("127.0.0.1", receipt["port"])) != 0
    finally:
        try:
            os.killpg(receipt["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("kind", ["download", "upload"])
def test_transfer_deadline_reaps_a_term_ignoring_drip_client(manager, tmp_path, monkeypatch, kind):
    roots = client(
        tmp_path,
        monkeypatch,
        (
            "import os, signal, socket, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()\n"
            "print(os.getpid(), listener.getsockname()[1], flush=True)\n"
            "while True: os.write(1, b'tick\\n'); time.sleep(0.01)"
        ),
    )
    manager.MAX_TRANSFER_SECONDS = 3
    opened = manager.start(roots, kind, [], module="palimpsest_local.remote_artifacts")
    deadline = time.monotonic() + 8
    while not manager.list()[0]["output"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    pid, port = map(int, manager.list()[0]["output"].splitlines()[0].split())
    result = finished(manager, opened["id"])
    assert result["status"] == "failed" and result["returncode"] == -signal.SIGKILL
    assert not manager.has_active()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with socket.socket() as connection:
        assert connection.connect_ex(("127.0.0.1", port)) != 0
