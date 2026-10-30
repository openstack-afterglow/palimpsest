"""Pure filesystem and policy boundaries of the real image executor."""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import pytest

from palimpsest_local import hub_image_builder as builder
from palimpsest_local.registry_intake import RegistryIntakeError


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def archive(tmp_path, entries):
    path = tmp_path / "context.tar"
    with tarfile.open(path, "w") as stream:
        for name, kind, data in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = data.decode() if data else "outside"
            stream.addfile(member, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return path


def test_context_extracts_real_files_and_preserves_copy_permissions(tmp_path):
    source = archive(
        tmp_path,
        [
            (".", tarfile.DIRTYPE, b""),
            ("app", tarfile.DIRTYPE, b""),
            ("app/data", tarfile.REGTYPE, b"actual COPY content"),
        ],
    )
    destination = tmp_path / "out"
    builder.extract_context(source, destination, maximum_bytes=100, maximum_members=10)
    assert (destination / "app/data").read_bytes() == b"actual COPY content"
    assert (destination / "app/data").stat().st_mode & 0o777 == 0o644
    assert (destination / "app").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/escape", tarfile.REGTYPE),
        ("app/link", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
        ("fifo", tarfile.FIFOTYPE),
        ("a\\b", tarfile.REGTYPE),
    ],
)
def test_context_rejects_traversal_links_and_special_files(tmp_path, name, kind):
    source = archive(tmp_path, [(name, kind, b"x")])
    with pytest.raises(ValueError, match="unsafe"):
        builder.extract_context(source, tmp_path / "out", maximum_bytes=100, maximum_members=10)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "entries,maximum,members",
    [
        ([("a", tarfile.REGTYPE, b"1234")], 3, 10),
        ([("a", tarfile.REGTYPE, b"1"), ("b", tarfile.REGTYPE, b"2")], 100, 1),
        ([("a", tarfile.REGTYPE, b"1"), ("a", tarfile.REGTYPE, b"2")], 100, 10),
    ],
)
def test_context_enforces_expanded_bytes_members_and_duplicate_paths(tmp_path, entries, maximum, members):
    with pytest.raises(ValueError):
        builder.extract_context(
            archive(tmp_path, entries), tmp_path / "out", maximum_bytes=maximum, maximum_members=members
        )


@pytest.mark.parametrize(
    "source,canonical",
    [
        ("alpine:3", "docker.io/library/alpine:3"),
        ("team/image:tag", "docker.io/team/image:tag"),
        ("ghcr.io/team/image:tag", "ghcr.io/team/image:tag"),
    ],
)
def test_registry_shorthand_uses_canonical_resolver(source, canonical):
    assert builder.normalize_reference(source) == canonical


@pytest.mark.parametrize(
    "reference",
    [
        "localhost/a:tag",
        "127.0.0.1/a:tag",
        "docker.io:443/a:tag",
        "https://docker.io/library/alpine",
        "user:password@docker.io/image:tag",
        "docker.io/../a",
        "ghcr.io/a\n",
    ],
)
def test_registry_rejects_unadmitted_hosts_ports_and_uri_credentials(reference):
    with pytest.raises((ValueError, RegistryIntakeError)):
        builder.normalize_reference(reference)


def materialization(tmp_path):
    config = json.dumps(
        {
            "config": {
                "Entrypoint": ["/bin/sh"],
                "Cmd": ["-c", "echo ok"],
                "Env": ["A=one"],
                "WorkingDir": "/app",
                "User": "1000",
            }
        }
    ).encode()
    config_file = tmp_path / sha(config).split(":")[1]
    config_file.write_bytes(config)
    layers = []
    results = []
    for ordinal, data in enumerate((b"hsqsfirst", b"hsqssecond")):
        path = tmp_path / f"source-{ordinal}"
        path.write_bytes(data)
        layers.append(str(path))
        results.append({"receipt": {"ordinal": ordinal, "image_digest": sha(data), "image_size": len(data)}})
    return {
        "receipt": {
            "root_descriptor": {"digest": sha(b"root")},
            "manifest_digest": sha(b"manifest"),
            "results": results,
        },
        "blob_paths": layers,
        "root": str(tmp_path),
        "image": {"config_descriptor": {"digest": sha(config), "size": len(config)}},
        "source_blob_paths": [str(config_file)],
    }


def test_materialized_result_preserves_order_full_provenance_and_actual_runtime(tmp_path):
    materialized = materialization(tmp_path)
    output = tmp_path / "job"
    output.mkdir()
    (output / "outputs.pack").write_bytes(bytes(1024))
    result = builder.materialized_result(materialized, output, 1024)
    assert [layer["digest"] for layer in result["layers"]] == [sha(b"hsqsfirst"), sha(b"hsqssecond")]
    assert (output / "outputs.pack").read_bytes()[:19] == b"hsqsfirsthsqssecond"
    assert result["runtime"] == {
        "entrypoint": ["/bin/sh"],
        "cmd": ["-c", "echo ok"],
        "env": ["A=one"],
        "working_dir": "/app",
        "user": "1000",
    }
    assert result["source"]["root_digest"] == sha(b"root")


def test_materialized_result_refuses_receipt_byte_contradiction(tmp_path):
    materialized = materialization(tmp_path)
    materialized["receipt"]["results"][0]["receipt"]["image_digest"] = sha(b"not these bytes")
    with pytest.raises(ValueError, match="receipt"):
        (tmp_path / "outputs.pack").write_bytes(bytes(1024))
        builder.materialized_result(materialized, tmp_path, 1024)


def test_materialized_result_enforces_total_not_only_per_layer_limit(tmp_path):
    with pytest.raises(ValueError, match="exceeds"):
        (tmp_path / "outputs.pack").write_bytes(bytes(10))
        builder.materialized_result(materialization(tmp_path), tmp_path, 10)


def test_source_valid_null_process_config_preserves_import_without_inventing_command(tmp_path):
    materialized = materialization(tmp_path)
    payload = json.dumps({"config": None}).encode()
    config = tmp_path / sha(payload).split(":")[1]
    config.write_bytes(payload)
    materialized["image"]["config_descriptor"] = {"digest": sha(payload), "size": len(payload)}
    materialized["source_blob_paths"] = [str(config)]
    output = tmp_path / "null-config-job"
    output.mkdir()
    (output / "outputs.pack").write_bytes(bytes(1024))
    result = builder.materialized_result(materialized, output, 1024)
    assert result["source"]["config_digest"] == sha(payload)
    assert result["runtime"]["entrypoint"] == [] and result["runtime"]["cmd"] == []
    assert (output / "outputs.pack").read_bytes()[:9] == b"hsqsfirst"


@pytest.mark.parametrize(
    "name,value", [("memory.max", "max"), ("pids.max", "max"), ("cpu.max", "max 100000"), ("cpu.max", "0 100000")]
)
def test_resource_preflight_rejects_unbounded_worker_without_mutating_limits(tmp_path, name, value):
    limits = {"memory.max": "2147483648", "pids.max": "512", "cpu.max": "200000 100000"}
    limits[name] = value
    for filename, content in limits.items():
        (tmp_path / filename).write_text(content)
    with pytest.raises(RuntimeError, match="finite"):
        builder._worker_limits(tmp_path)
    assert {path.name: path.read_text() for path in tmp_path.iterdir()} == limits


def test_resource_preflight_accepts_existing_outer_bounds_without_enabling_controllers(tmp_path):
    for name, value in {"memory.max": "2147483648", "pids.max": "512", "cpu.max": "200000 100000"}.items():
        (tmp_path / name).write_text(value)
    builder._worker_limits(tmp_path)
    assert not (tmp_path / "cgroup.subtree_control").exists()


def test_internal_relative_symlinks_are_deferred_and_preserve_source_tree(tmp_path):
    source = archive(tmp_path, [("app/link", tarfile.SYMTYPE, b"../data"), ("data", tarfile.REGTYPE, b"actual source")])
    destination = tmp_path / "out"
    builder.extract_context(source, destination, maximum_bytes=100, maximum_members=10)
    assert (destination / "app/link").is_symlink()
    assert (destination / "app/link").readlink().as_posix() == "../data"
    assert (destination / "app/link").read_bytes() == b"actual source"


@pytest.mark.parametrize("target", [b"/etc/passwd", b"../../outside", b"../..", b"..\\outside"])
def test_context_symlink_cannot_escape_context(tmp_path, target):
    with pytest.raises(ValueError, match="link"):
        builder.extract_context(
            archive(tmp_path, [("app/link", tarfile.SYMTYPE, target)]),
            tmp_path / "out",
            maximum_bytes=100,
            maximum_members=10,
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_context_never_writes_through_symlink_ancestor_in_either_order(tmp_path, reverse):
    entries = [("link", tarfile.SYMTYPE, b"internal"), ("link/write", tarfile.REGTYPE, b"must not write")]
    if reverse:
        entries.reverse()
    with pytest.raises(ValueError, match="beneath symlink"):
        builder.extract_context(archive(tmp_path, entries), tmp_path / "out", maximum_bytes=100, maximum_members=10)
    assert not (tmp_path / "out/internal/write").exists()


def test_context_symlink_cycles_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        builder.extract_context(
            archive(tmp_path, [("a", tarfile.SYMTYPE, b"b"), ("b", tarfile.SYMTYPE, b"a")]),
            tmp_path / "out",
            maximum_bytes=100,
            maximum_members=10,
        )


@pytest.mark.skipif(sys.platform != "linux", reason="requires the actual Linux cgroup v2 kernel")
@pytest.mark.parametrize("stage", ["acquisition", "materialization"])
def test_cleanup_kills_setsid_tool_after_executor_is_killed(tmp_path, stage):
    """Opt-in parent-run proof: session changes cannot escape kernel ownership."""
    configured = os.environ.get("PALIMPSEST_TEST_IMAGE_CGROUP_ROOT")
    if configured is None:
        pytest.skip("run inside the bounded privileged worker with PALIMPSEST_TEST_IMAGE_CGROUP_ROOT=/sys/fs/cgroup")
    root = Path(configured)
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    code = """
import os, subprocess, sys, time
from pathlib import Path
from palimpsest_local import hub_image_builder as builder
from palimpsest_local.hub_builder import _mark_builder, _save
job, root = map(Path, sys.argv[1:3])
stage = sys.argv[3]
def tool():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True)
    _save(job / 'escaped-child.json', {'pid': child.pid, 'pgid': os.getpgid(child.pid)})
    time.sleep(300)
class CacheProbe:
    def __init__(self, *args, **kwargs): pass
    def pull(self, *args, **kwargs):
        if stage == 'acquisition': tool()
        return {'root_digest': 'sha256:' + 'a' * 64}
    def materialize(self, *args, **kwargs): tool()
builder.LayerCache = CacheProbe
_mark_builder(job, os.getppid())
builder.run_image({'platform': 'linux/amd64', 'run_network': 'none', 'cgroup_root': str(root),
    'timeout_seconds': 300, 'max_output_bytes': 1024 * 1024, 'kind': 'registry',
    'registry_hosts': ['docker.io'], 'request': {'reference': 'docker.io/library/alpine:3'}}, job)
"""
    executor = subprocess.Popen([sys.executable, "-c", code, str(job), str(root), stage], start_new_session=True)
    marker = job / "escaped-child.json"
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            if executor.poll() is not None or time.monotonic() >= deadline:
                pytest.fail(
                    "real executor failed before spawning independent-session acquisition/materializer surrogate"
                )
            time.sleep(0.05)
        child = json.loads(marker.read_text())
        ledger = json.loads((job / "image-resources.json").read_text())
        group = root / ledger["name"]
        assert child["pgid"] == child["pid"] and child["pgid"] != executor.pid
        assert "populated 1" in (group / "cgroup.events").read_text()
        os.killpg(executor.pid, signal.SIGKILL)
        executor.wait(timeout=5)
        # Killing the executor's session demonstrably leaves the tool alive.
        assert Path(f"/proc/{child['pid']}").exists()
        assert "populated 1" in (group / "cgroup.events").read_text()
        builder.cleanup_image(job)
        assert not group.exists()
        stat_path = Path(f"/proc/{child['pid']}/stat")
        if stat_path.exists():
            assert stat_path.read_text().rsplit(") ", 1)[1].split()[0] in ("Z", "X")
        assert not (job / "image-resources.json").exists()
    finally:
        if executor.poll() is None:
            os.killpg(executor.pid, signal.SIGKILL)
            executor.wait(timeout=5)
        if (job / "image-resources.json").exists():
            builder.cleanup_image(job)


def test_allocation_uses_real_reserved_blocks_and_refuses_existing_file(tmp_path, monkeypatch):
    # Exercise real file descriptors on platforms without posix_fallocate too.
    def allocate(fd, offset, size):
        assert offset == 0
        os.write(fd, os.urandom(size))

    monkeypatch.setattr(os, "posix_fallocate", allocate, raising=False)
    path = tmp_path / "reserve"
    builder._allocate(path, 32768)
    assert path.stat().st_size == 32768
    assert path.stat().st_blocks * 512 >= 32768
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        builder._allocate(path, 32768)
    assert path.read_bytes() == before


def test_sparse_backing_is_not_accepted_as_reserved_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "posix_fallocate", lambda fd, offset, size: os.ftruncate(fd, size), raising=False)
    with pytest.raises(RuntimeError, match="fully allocated"):
        builder._allocate(tmp_path / "sparse", 1024 * 1024)


def test_setup_allocation_failure_releases_journal_capacity_before_tools(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(builder, "_command", lambda *args, **kwargs: b"ext4\n")

    def allocate(path, size):
        calls.append(path.name)
        if path.name == "scratch.ext4":
            raise OSError("no space")
        path.write_bytes(b"reserved")

    monkeypatch.setattr(builder, "_allocate", allocate)
    with (
        pytest.raises(OSError, match="no space"),
        builder._scratch(tmp_path, 256 * 1024**2, 1024, time.monotonic() + 10),
    ):
        pytest.fail("unbounded fallback")
    assert calls == ["journal.reserve", "outputs.pack", "scratch.ext4"]
    assert not (tmp_path / "journal.reserve").exists()
    builder._save(tmp_path / "worker.json", {"phase": "failed"})
    assert json.loads((tmp_path / "worker.json").read_text()) == {"phase": "failed"}


def test_temporary_root_covers_python_and_safe_external_tools_and_restores_state(tmp_path):
    before = os.environ.get("TMPDIR"), tempfile.tempdir, builder.SAFE_ENV.get("TMPDIR")
    with pytest.raises(RuntimeError, match="interrupted"):
        with builder._temporary_root(tmp_path):
            with tempfile.TemporaryDirectory(prefix="registry-") as directory:
                assert Path(directory).parent == tmp_path / "tmp"
                Path(directory, "download").write_bytes(b"private registry bytes")
                output = subprocess.check_output(
                    [sys.executable, "-c", "import tempfile; print(tempfile.mkdtemp(prefix='tool-'))"],
                    env=builder.SAFE_ENV,
                )
                assert Path(output.decode().strip()).parent == tmp_path / "tmp"
            assert not Path(directory).exists()
            raise RuntimeError("interrupted")
    assert (os.environ.get("TMPDIR"), tempfile.tempdir, builder.SAFE_ENV.get("TMPDIR")) == before


@pytest.mark.skipif(
    os.environ.get("PALIMPSEST_IMAGE_SCRATCH_LINUX_SMOKE") != "1",
    reason="explicit single-container privileged Linux scratch proof only",
)
def test_native_scratch_bounds_temp_kill_journal_and_exact_loop_cleanup(tmp_path):
    """No registry/network/tools beyond ext4+loop; writes at most 264MiB."""
    import shutil

    from palimpsest_local import hub_builder

    assert sys.platform == "linux" and os.geteuid() == 0
    job = tmp_path / "scratch-proof"
    job.mkdir(mode=0o700)
    script = r"""
import errno, json, os, sys, tempfile, time
from pathlib import Path
from palimpsest_local import hub_image_builder as b
job = Path(sys.argv[1])
b._mark_builder(job, os.getppid())
b._private_mounts()
with b._scratch(job, 256 * 1024**2, 64 * 1024**2, time.monotonic() + 90) as (root, maximum, executor):
    assert (job / 'scratch.ext4').stat().st_blocks * 512 >= 192 * 1024**2
    assert (job / 'outputs.pack').stat().st_blocks * 512 >= maximum
    with b._temporary_root(root):
        temporary = Path(tempfile.mkdtemp(prefix='registry-killed-'))
        (temporary / 'partial-download').write_bytes(b'owned bytes')
        count = 0
        try:
            for count in range(300):
                with (root / ('snapshot-' + str(count))).open('wb') as stream:
                    stream.write(bytes(1024 * 1024))
        except OSError as exc:
            assert exc.errno == errno.ENOSPC, exc
        else:
            raise AssertionError('scratch grew beyond aggregate limit')
        assert 2 < count < 256
        b._save(job / 'worker.json', {'phase': 'cleanup_pending', 'files': count})
        fd = os.open(job / 'outputs.pack', os.O_WRONLY)
        os.write(fd, b'hsqsreserved-output')
        os.fsync(fd)
        os.close(fd)
        b._save(job / 'ready.json', {'loop': executor.loops[0]['device'], 'temporary': str(temporary)})
        while True:
            time.sleep(1)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(job)], start_new_session=True)
    try:
        deadline = time.monotonic() + 120
        while not (job / "ready.json").exists():
            assert process.poll() is None, "scratch setup/fill failed"
            assert time.monotonic() < deadline, "scratch proof timed out"
            time.sleep(0.05)
        ready = json.loads((job / "ready.json").read_text())
        assert json.loads((job / "worker.json").read_text())["phase"] == "cleanup_pending"
        with (job / "outputs.pack").open("rb") as stream:
            assert stream.read(19) == b"hsqsreserved-output"
        loop = ready["loop"]
        assert hub_builder._loop_identity(loop) is not None
        process.kill()
        process.wait(timeout=10)
        builder.cleanup_image(job)
        assert hub_builder._loop_identity(loop) is None
        assert not (job / "resources.json").exists()
        assert not Path(ready["temporary"]).exists()
        assert not any((job / "scratch").iterdir())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        builder.cleanup_image(job)
        shutil.rmtree(job)


def test_scratch_rejects_copy_on_write_or_overlay_storage_before_allocating(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "_command", lambda *args, **kwargs: b"overlay\n")
    with (
        pytest.raises(RuntimeError, match="non-CoW"),
        builder._scratch(tmp_path, 256 * 1024**2, 1024, time.monotonic() + 10),
    ):
        pytest.fail("unbounded fallback")
    assert not list(tmp_path.iterdir())


def test_cleanup_reclaims_interrupted_setup_reserve_without_following_links(tmp_path):
    protected = tmp_path / "keep"
    protected.write_bytes(b"not owned capacity")
    (tmp_path / "journal.reserve").symlink_to(protected)
    with pytest.raises(RuntimeError, match="unsafe journal"):
        builder._release_journal_reserve(tmp_path)
    assert protected.read_bytes() == b"not owned capacity"
    (tmp_path / "journal.reserve").unlink()
    (tmp_path / "journal.reserve").write_bytes(b"partial allocation")
    builder._release_journal_reserve(tmp_path)
    builder._save(tmp_path / "worker.json", {"phase": "reconciled"})
    assert not (tmp_path / "journal.reserve").exists()


@pytest.mark.parametrize("missing", [False, True])
def test_cleanup_fences_cgroup_before_exact_loop_cleanup_even_after_group_removal(tmp_path, monkeypatch, missing):
    job = tmp_path / "job"
    job.mkdir()
    root = tmp_path / "cgroup"
    root.mkdir()
    name = "palimpsest-image-" + "a" * 32
    group = root / name
    inode = None
    events = []
    if not missing:
        group.mkdir()
        inode = group.stat().st_ino
        (group / "cgroup.kill").write_text("")
        (group / "cgroup.events").write_text("populated 1\n")
    builder._save(
        job / "image-resources.json",
        {
            "root": str(root),
            "root_inode": root.stat().st_ino,
            "name": name,
            "inode": inode,
        },
    )
    # Map only the kernel policy root to our private simulated cgroup files.
    monkeypatch.setattr(builder, "Path", lambda value: root if str(value) == "/sys/fs/cgroup" else Path(value))

    def wait(_):
        assert (group / "cgroup.kill").read_text() == "1"
        events.append("empty")
        (group / "cgroup.events").write_text("populated 0\n")

    monkeypatch.setattr(builder.time, "sleep", wait)
    original_rmdir = Path.rmdir

    def rmdir(path):
        if path == group:
            (path / "cgroup.kill").unlink()
            (path / "cgroup.events").unlink()
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", rmdir)

    def cleanup(directory):
        assert directory == job
        assert not group.exists()
        assert missing or events == ["empty"]
        events.append("loops")

    monkeypatch.setattr(builder, "cleanup_build", cleanup)
    builder.cleanup_image(job)
    assert events[-1] == "loops"
    assert not (job / "image-resources.json").exists()


def test_private_tool_drain_keeps_final_failure_tail_outside_scratch(tmp_path):
    import shutil

    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    scratch = job / "scratch"
    scratch.mkdir(mode=0o700)
    marker = b"final-buildkit-failure-evidence"
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import os; os.write(2,b'prefix'+b'x'*300000+" + repr(marker) + "); raise SystemExit(1)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    log = job / "buildctl.log"
    drain = builder._private_log(child, log)
    try:
        assert child.wait(timeout=10) == 1
        drain.join(timeout=5)
        assert not drain.is_alive()
        shutil.rmtree(scratch)
        tail = log.read_bytes()
        assert len(tail) == builder.LOG_TAIL_BYTES
        assert tail.endswith(marker) and b"prefix" not in tail
        assert log.stat().st_mode & 0o777 == 0o600
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_private_tool_drain_refuses_symlinks_before_starting_thread(tmp_path):
    from types import SimpleNamespace

    protected = tmp_path / "protected"
    protected.write_bytes(b"not a log")
    log = tmp_path / "buildkitd.log"
    log.symlink_to(protected)
    with pytest.raises(OSError):
        builder._private_log(SimpleNamespace(stderr=io.BytesIO(b"overwrite")), log)
    assert protected.read_bytes() == b"not a log"


@pytest.mark.parametrize(
    "error,category,number,exit_code",
    [
        (OSError(28, "SECRET recipe /private/path"), "OSError", 28, 1),
        (RuntimeError("SECRET recipe /private/path"), "RuntimeError", None, 1),
        (TimeoutError("SECRET recipe /private/path"), "TimeoutError", None, 124),
    ],
)
def test_executor_main_reports_safe_real_exception_category_and_errno(
    tmp_path, monkeypatch, capsys, error, category, number, exit_code
):
    def fail(manifest, directory):
        raise error

    monkeypatch.setattr(sys, "argv", ["hub_image_builder", str(tmp_path / "input.json"), "123"])
    monkeypatch.setattr(builder, "_mark_builder", lambda *args: None)
    monkeypatch.setattr(builder, "_load_manifest", lambda *args: {})
    monkeypatch.setattr(builder, "run_image", fail)
    with pytest.raises(SystemExit) as stopped:
        builder.main()
    assert stopped.value.code == exit_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"stage": "execution", "category": category, "errno": number}
    assert "SECRET" not in captured.err and "/private/path" not in captured.err


def test_safe_failure_stage_identifies_actual_allocation_without_paths(tmp_path, monkeypatch):
    def no_space(*args):
        raise OSError(28, "SECRET allocation /private/path")

    monkeypatch.setattr(builder.os, "posix_fallocate", no_space, raising=False)
    try:
        builder._allocate(tmp_path / "reserve", 4096)
    except OSError as exc:
        diagnostic = json.loads(builder._failure_summary(exc, "execution"))
    else:
        pytest.fail("allocation unexpectedly succeeded")
    assert diagnostic == {"stage": "scratch_allocation", "category": "OSError", "errno": 28}


def test_buildkit_binds_short_private_socket_with_production_length_job_root(tmp_path, monkeypatch):
    # Exercise the real AF_UNIX bind and client connection, without root,
    # BuildKit binaries, cgroups, networking, or any cloud resources.
    job = tmp_path / ("worker-" + "x" * 100) / ("job-" + "1" * 36)
    scratch = job / "scratch"
    scratch.mkdir(mode=0o700, parents=True)
    assert len(os.fsencode(scratch / "buildkit.sock")) > 104
    actual_popen = subprocess.Popen
    original_path = builder.Path
    addresses = []

    with tempfile.TemporaryDirectory(prefix="pbs-", dir="/tmp") as runtime:
        short_socket = Path(runtime) / "buildkit.sock"

        def private_path(value):
            if value == "/run/palimpsest-buildkit.sock":
                # Production /run is private tmpfs; model only that mount here.
                return short_socket
            return original_path(value)

        def launch(argv, **kwargs):
            address = argv[argv.index("--addr") + 1]
            path = address.removeprefix("unix://")
            addresses.append(path)
            if argv[0] == "buildkitd":
                assert "preexec_fn" in kwargs
                del kwargs["preexec_fn"]
                code = (
                    "import socket,sys,time; s=socket.socket(socket.AF_UNIX); "
                    "s.bind(sys.argv[1]); s.listen(); time.sleep(60)"
                )
                return actual_popen([sys.executable, "-I", "-c", code, path], **kwargs)
            assert argv[0] == "buildctl"
            output = argv[argv.index("--output") + 1].split("dest=", 1)[1].split(",", 1)[0]
            code = (
                "import socket,sys,pathlib; s=socket.socket(socket.AF_UNIX); "
                "s.connect(sys.argv[1]); pathlib.Path(sys.argv[2]).write_bytes(b'oci-output')"
            )
            return actual_popen([sys.executable, "-I", "-c", code, path, output], **kwargs)

        monkeypatch.setattr(builder, "Path", private_path)
        monkeypatch.setattr(builder.subprocess, "Popen", launch)
        manifest = {
            "context": None,
            "request": {"dockerfile": "FROM scratch\n"},
            "registry_hosts": ["docker.io"],
            "_execution_cgroup": "/sys/fs/cgroup/palimpsest-test",
            "run_network": "none",
            "max_output_bytes": 4096,
        }
        output = builder._buildkit(manifest, scratch, time.monotonic() + 10, job)
        assert output.read_bytes() == b"oci-output"
        assert addresses == [str(short_socket), str(short_socket)]
        assert len(os.fsencode(short_socket)) <= 104
        assert not (scratch / "buildkit.sock").exists()
        assert (job / "buildkitd.log").stat().st_mode & 0o777 == 0o600
        assert (job / "buildctl.log").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("explicit", [False, True])
def test_safe_failure_summary_preserves_wrapped_cache_errno_without_payload(explicit):
    from palimpsest_local.layer_cache import LayerCacheError

    def raise_implicitly():
        raise LayerCacheError("private wrapped payload")

    try:
        try:
            raise OSError(28, "private credential recipe /private/path")
        except OSError as cause:
            if explicit:
                raise LayerCacheError("private wrapped payload") from cause
            raise_implicitly()
    except LayerCacheError as exc:
        summary = builder._failure_summary(exc, "execution")
    assert json.loads(summary) == {"stage": "execution", "category": "OSError", "errno": 28}
    assert "private" not in summary and "credential" not in summary


def test_safe_failure_summary_bounds_cyclic_cause_chain():
    first = RuntimeError("private first")
    second = OSError(28, "private second")
    first.__cause__ = second
    second.__cause__ = first
    assert json.loads(builder._failure_summary(first, "execution")) == {
        "stage": "execution",
        "category": "OSError",
        "errno": 28,
    }
