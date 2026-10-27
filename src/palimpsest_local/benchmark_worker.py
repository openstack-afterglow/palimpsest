"""Single-executor queue for an always-on, operator-owned benchmark controller.

The desktop develops code; this worker owns request execution and monitoring.
An exclusive started receipt prevents replay after a crash or uncertain create.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .benchmark_workflow import load_config, validate_workflow
from .errors import ArtifactValidationError
from .state import atomic_write_json


def _request(config: dict) -> tuple[str, dict]:
    if config.get("schema") == "palimpsest.benchmark-workflow.v1":
        validate_workflow(config)
        return "workflow", config
    if (
        config.get("schema") != "palimpsest.benchmark-request.v1"
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", config.get("job_id", "")) is None
    ):
        raise ArtifactValidationError("invalid standalone benchmark queue request")
    mode, payload = config.get("mode"), config.get("config")
    schemas = {"local": "palimpsest.benchmark-environment.v1", "openstack": "palimpsest.benchmark-openstack.v1"}
    if mode not in schemas or not isinstance(payload, dict) or payload.get("schema") != schemas[mode]:
        raise ArtifactValidationError("standalone benchmark request mode/config differs")
    return mode, payload


def _root(root: Path) -> Path:
    root = root.expanduser().absolute()
    for path in (root, root / "queue", root / "state", root / "results"):
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise ArtifactValidationError("benchmark queue directories must be private, owned and not symlinks")
    return root


def initialize(root: Path) -> None:
    root = root.expanduser().absolute()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name in ("queue", "state", "results"):
        (root / name).mkdir(mode=0o700)


def submit(root: Path, config: dict) -> dict:
    root = _root(root)
    _request(config)
    identity = config["job_id"]
    if (root / "state" / (identity + ".json")).exists():
        raise ArtifactValidationError("benchmark job already started; inspect instead of replaying")
    path = root / "queue" / (identity + ".json")
    temporary = root / "queue" / (".pending-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            rendered = json.dumps(config, allow_nan=False)
            if len(rendered.encode()) > 1024 * 1024:
                raise ArtifactValidationError("benchmark queue configuration exceeds its bound")
            stream.write(rendered + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(root / "queue", os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink()
    return {"job_id": identity, "phase": "queued", "queue": str(path)}


def status(root: Path, job_id: str) -> dict:
    root = _root(root)
    if not job_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in job_id):
        raise ArtifactValidationError("invalid benchmark job ID")
    path = root / "state" / (job_id + ".json")
    if path.exists():
        return load_config(path)
    return {"job_id": job_id, "phase": "queued" if (root / "queue" / (job_id + ".json")).is_file() else "not-found"}


def work(root: Path, *, once: bool = False) -> int:
    root = _root(root)
    lockfd = os.open(root / ".worker.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            uncertain = any(
                load_config(p).get("phase") in {"started-no-replay", "interrupted-no-replay", "failed-no-replay"}
                for p in (root / "state").glob("*.json")
            )
            atomic_write_json(
                root / "heartbeat.json",
                {
                    "observed_unix_seconds": time.time(),
                    "phase": "blocked-inspect-uncertain-job" if uncertain else "ready",
                },
            )
            if uncertain:
                if once:
                    return 2
                time.sleep(10)
                continue
            for path in sorted((root / "queue").glob("*.json")):
                statepath = root / "state" / path.name
                if statepath.exists():
                    continue
                st = path.lstat()
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_uid != os.geteuid()
                    or st.st_mode & 0o077
                    or st.st_size > 1024 * 1024
                ):
                    raise ArtifactValidationError("unsafe benchmark queue entry")
                config = load_config(path)
                mode, payload = _request(config)
                if path.stem != config["job_id"]:
                    raise ArtifactValidationError("benchmark queue identity differs")
                evidence = root / "results" / path.stem
                record = {
                    "job_id": path.stem,
                    "phase": "started-no-replay",
                    "started_unix_seconds": time.time(),
                    "evidence": str(evidence),
                }
                fd = os.open(statepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                input_path = root / "state" / (path.stem + ".input")
                input_fd = os.open(input_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(input_fd, "w") as stream:
                    json.dump(payload, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                with (
                    (root / "state" / (path.stem + ".stdout")).open("xb") as out,
                    (root / "state" / (path.stem + ".stderr")).open("xb") as err,
                ):
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "palimpsest_local.benchmark_cli",
                            mode,
                            "--config",
                            str(input_path),
                            "--output",
                            str(evidence),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=err,
                        start_new_session=True,
                    )
                    record["pid"] = child.pid
                    atomic_write_json(statepath, record)
                    try:
                        while True:
                            try:
                                code = child.wait(timeout=10)
                                break
                            except subprocess.TimeoutExpired:
                                atomic_write_json(
                                    root / "heartbeat.json",
                                    {
                                        "observed_unix_seconds": time.time(),
                                        "phase": "running",
                                        "job_id": path.stem,
                                        "pid": child.pid,
                                    },
                                )
                    except BaseException:
                        try:
                            os.killpg(child.pid, signal.SIGTERM)
                            child.wait(timeout=5)
                        except (ProcessLookupError, subprocess.TimeoutExpired):
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            child.wait()
                        record.update(phase="interrupted-no-replay", finished_unix_seconds=time.time())
                        atomic_write_json(statepath, record)
                        raise
                record.update(
                    phase="success" if code == 0 else "failed-no-replay",
                    returncode=code,
                    finished_unix_seconds=time.time(),
                )
                atomic_write_json(statepath, record)
                if code != 0:
                    if once:
                        return 1
                    break
            if once:
                return 0
            time.sleep(10)
