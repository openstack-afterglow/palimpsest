"""Project block-volume lifecycle contracts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from palimpsest_local import project_volumes, state
from palimpsest_local.errors import LifecycleError, StateError

MIB = 1024 * 1024
VOLUME_SIZE = 16 * MIB
EXT4_MAGIC_OFFSET = 1024 + 56
EXT4_INCOMPAT_OFFSET = 1024 + 96


@pytest.fixture
def roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )


def _completed(argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _write_ext4_magic(path: Path, label: str = "pali-test") -> None:
    with path.open("r+b") as stream:
        stream.seek(EXT4_MAGIC_OFFSET)
        stream.write(b"\x53\xef")
        stream.seek(EXT4_INCOMPAT_OFFSET)
        stream.write((0x40).to_bytes(4, byteorder="little"))
        stream.seek(1024 + 120)
        stream.write(label.encode("ascii").ljust(16, b"\0"))
        stream.flush()
        os.fsync(stream.fileno())


class FakeKvmTools:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_mkfs = False
        self.info_format = "raw"
        self.backing_file: str | None = None

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv == ["qemu-img", "--version"]:
            return _completed(argv, stdout="qemu-img version 9.2\n")
        if argv == ["mkfs.ext4", "-V"]:
            return _completed(argv, stdout="mke2fs 1.47\n")
        if argv[0] == "mkfs.ext4":
            if self.fail_mkfs:
                return _completed(argv, returncode=1, stderr="format failed")
            _write_ext4_magic(Path(argv[-1]), argv[argv.index("-L") + 1])
            return _completed(argv)
        if argv[:3] == ["qemu-img", "info", "--output=json"]:
            path = Path(argv[-1])
            value: dict[str, object] = {
                "format": self.info_format,
                "virtual-size": path.stat().st_size,
            }
            if self.backing_file is not None:
                value["backing-filename"] = self.backing_file
            return _completed(argv, stdout=json.dumps(value))
        raise AssertionError(f"unexpected command: {argv}")


class FakeLima:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.disks: dict[str, dict[str, object]] = {}
        self.version = "limactl version 2.1.4\n"
        self.fail_delete = False
        self.keep_after_delete = False
        self.omit_created_from_list = False

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv == ["limactl", "--version"]:
            return _completed(argv, stdout=self.version)
        if argv == ["limactl", "disk", "list", "--json"]:
            values = [] if self.omit_created_from_list else list(self.disks.values())
            return _completed(argv, stdout="\n".join(json.dumps(value) for value in values))
        if argv[:3] == ["limactl", "disk", "create"]:
            name = argv[3]
            if name in self.disks:
                return _completed(argv, returncode=1, stderr="already exists")
            size_text = argv[argv.index("--size") + 1]
            assert size_text.endswith("MiB")
            size_bytes = int(size_text.removesuffix("MiB")) * MIB
            disk_format = argv[argv.index("--format") + 1]
            self.disks[name] = {
                "name": name,
                "size": size_bytes,
                "format": disk_format,
                "dir": f"/tmp/lima/_disks/{name}",
                "instance": "",
            }
            return _completed(argv)
        if argv[:3] == ["limactl", "disk", "delete"]:
            if self.fail_delete:
                return _completed(argv, returncode=1, stderr="disk is attached")
            if not self.keep_after_delete:
                self.disks.pop(argv[3], None)
            return _completed(argv)
        raise AssertionError(f"unexpected command: {argv}")


def test_default_runner_always_uses_argv_and_shell_false(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed.update(kwargs)
        return _completed(argv)

    monkeypatch.setattr(subprocess, "run", fake_run)

    project_volumes._default_runner(["qemu-img", "--version"])

    assert observed["argv"] == ["qemu-img", "--version"]
    assert observed["shell"] is False
    assert observed["check"] is False


@pytest.mark.parametrize("project,name", [("../escape", "data"), ("demo", "../data"), ("UPPER", "data")])
def test_kvm_path_rejects_invalid_names(
    roots: state.StatePaths,
    project: str,
    name: str,
) -> None:
    with pytest.raises(StateError, match="invalid"):
        project_volumes.kvm_volume_path(roots, project, name)


@pytest.mark.parametrize("size", [True, 1, 16 * MIB + 1, project_volumes.MAX_VOLUME_BYTES + MIB])
def test_kvm_create_rejects_invalid_sizes(roots: state.StatePaths, size: object) -> None:
    with pytest.raises(StateError, match="volume size"):
        project_volumes.ensure_kvm_volume(roots, "demo", "data", size)  # type: ignore[arg-type]


def test_kvm_path_rejects_project_directory_symlink(roots: state.StatePaths) -> None:
    other = roots.volumes / "other"
    other.mkdir(mode=0o700)
    (roots.volumes / "demo").symlink_to(other, target_is_directory=True)

    with pytest.raises(StateError, match="must not be a symlink"):
        project_volumes.kvm_volume_path(roots, "demo", "data")


def test_kvm_volume_is_sparse_formatted_verified_and_atomically_published(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()

    volume = project_volumes.ensure_kvm_volume(
        roots,
        "demo",
        "data",
        VOLUME_SIZE,
        runner=tools,
    )

    assert volume.created is True
    assert volume.path == roots.volumes / "demo" / "data.raw"
    assert volume.path.stat().st_size == VOLUME_SIZE
    assert state.permission_bits(volume.path) == 0o600
    assert volume.path.read_bytes()[EXT4_MAGIC_OFFSET : EXT4_MAGIC_OFFSET + 2] == b"\x53\xef"
    assert tools.calls[:2] == [["qemu-img", "--version"], ["mkfs.ext4", "-V"]]
    format_call = next(call for call in tools.calls if call[0] == "mkfs.ext4" and call != ["mkfs.ext4", "-V"])
    assert format_call[:6] == ["mkfs.ext4", "-F", "-q", "-L", format_call[4], format_call[5]]
    assert Path(format_call[-1]).name.startswith(".data-")
    assert not list(volume.path.parent.glob("*.tmp"))


def test_kvm_existing_volume_is_verified_without_preflight_or_reformat(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    path = project_volumes.kvm_volume_path(roots, "demo", "data")
    path.parent.mkdir(mode=0o700)
    with path.open("wb") as stream:
        stream.truncate(VOLUME_SIZE)
    path.chmod(0o600)
    _write_ext4_magic(path, project_volumes.kvm_volume_label("demo", "data"))

    volume = project_volumes.ensure_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)

    assert volume.created is False
    assert tools.calls == [["qemu-img", "info", "--output=json", str(path)]]


def test_kvm_never_reformats_conflicting_existing_file(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    path = project_volumes.kvm_volume_path(roots, "demo", "data")
    path.parent.mkdir(mode=0o700)
    path.write_bytes(b"not-ext4")
    path.chmod(0o600)

    with pytest.raises(StateError, match="size conflict"):
        project_volumes.ensure_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)

    assert not any(call[0] == "mkfs.ext4" for call in tools.calls)
    assert path.read_bytes() == b"not-ext4"


def test_kvm_format_failure_leaves_no_target_or_temporary_file(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    tools.fail_mkfs = True

    with pytest.raises(LifecycleError, match="format KVM volume"):
        project_volumes.ensure_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)

    directory = roots.volumes / "demo"
    assert not (directory / "data.raw").exists()
    assert list(directory.iterdir()) == []


def test_kvm_verification_rejects_backing_file_and_preserves_volume(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    path = project_volumes.kvm_volume_path(roots, "demo", "data")
    path.parent.mkdir(mode=0o700)
    with path.open("wb") as stream:
        stream.truncate(VOLUME_SIZE)
    path.chmod(0o600)
    _write_ext4_magic(path, project_volumes.kvm_volume_label("demo", "data"))
    tools.backing_file = "/external/base.raw"

    with pytest.raises(StateError, match="backing file"):
        project_volumes.delete_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)

    assert path.exists()


def test_kvm_verification_and_delete_reject_foreign_label_and_unsafe_permissions(
    roots: state.StatePaths,
) -> None:
    tools = FakeKvmTools()
    path = project_volumes.kvm_volume_path(roots, "demo", "data")
    path.parent.mkdir(mode=0o700)
    with path.open("wb") as stream:
        stream.truncate(VOLUME_SIZE)
    path.chmod(0o600)
    _write_ext4_magic(path, "foreign-label")

    with pytest.raises(StateError, match="expected label"):
        project_volumes.verify_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)
    with pytest.raises(StateError, match="expected label"):
        project_volumes.delete_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)
    assert path.exists()

    _write_ext4_magic(path, project_volumes.kvm_volume_label("demo", "data"))
    path.chmod(0o644)
    with pytest.raises(StateError, match="permissions"):
        project_volumes.verify_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)
    path.chmod(0o600)
    path.parent.chmod(0o755)
    with pytest.raises(StateError, match="0700"):
        project_volumes.verify_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)


def test_kvm_delete_is_verified_and_idempotent(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    volume = project_volumes.ensure_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)
    tools.calls.clear()

    assert project_volumes.delete_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools) is True
    assert not volume.path.exists()
    assert project_volumes.delete_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools) is False


def test_kvm_delete_quarantine_validator_failure_restores_original_path(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    volume = project_volumes.ensure_kvm_volume(roots, "demo", "data", VOLUME_SIZE, runner=tools)
    observed: list[tuple[Path, Path]] = []

    def reject(old_path: Path, quarantine_path: Path) -> None:
        observed.append((old_path, quarantine_path))
        assert not old_path.exists()
        assert quarantine_path.exists()
        raise StateError("backend reference appeared")

    with pytest.raises(StateError, match="backend reference appeared"):
        project_volumes.delete_kvm_volume(
            roots,
            "demo",
            "data",
            VOLUME_SIZE,
            runner=tools,
            quarantine_validator=reject,
        )

    assert observed
    assert volume.path.exists()
    assert not observed[0][1].exists()


def test_lima_backend_name_is_scoped_deterministic_and_bounded() -> None:
    first = project_volumes.lima_backend_name("project", "data")

    assert first == project_volumes.lima_backend_name("project", "data")
    assert first != project_volumes.lima_backend_name("project-data", "data")
    assert first != project_volumes.lima_backend_name("project", "data-data")
    assert len(project_volumes.lima_backend_name("p" * 63, "v" * 63)) == 11
    assert first.isalnum() and first.islower()


def test_lima_list_parses_v21_json_lines() -> None:
    output = (
        '{"name":"one","size":16777216,"format":"raw","dir":"/tmp/one","instance":""}\n'
        '{"name":"two","size":33554432,"format":"qcow2","dir":"/tmp/two","instance":"vm"}\n'
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return _completed(argv, stdout=output)

    disks = project_volumes.list_lima_disks(runner=runner)

    assert disks == (
        project_volumes.LimaDiskInfo("one", VOLUME_SIZE, "raw", "/tmp/one", None),
        project_volumes.LimaDiskInfo("two", 2 * VOLUME_SIZE, "qcow2", "/tmp/two", "vm"),
    )


def test_lima_list_rejects_duplicate_or_malformed_entries() -> None:
    duplicate = '{"name":"same","size":16777216,"format":"raw"}\n' * 2
    with pytest.raises(LifecycleError, match="duplicate"):
        project_volumes.list_lima_disks(runner=lambda argv: _completed(argv, stdout=duplicate))
    with pytest.raises(LifecycleError, match="invalid JSON"):
        project_volumes.list_lima_disks(runner=lambda argv: _completed(argv, stdout="{broken\n"))


def test_lima_create_writes_receipt_and_is_idempotent(roots: state.StatePaths) -> None:
    lima = FakeLima()

    created = project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)

    assert created.created is True
    assert created.backend_name == project_volumes.lima_backend_name("demo", "data")
    create_call = next(call for call in lima.calls if call[:3] == ["limactl", "disk", "create"])
    assert create_call == [
        "limactl",
        "disk",
        "create",
        created.backend_name,
        "--size",
        "16MiB",
        "--format",
        "raw",
        "--tty=false",
    ]
    owner = roots.volumes / "demo" / "data.lima-owner.json"
    assert owner.is_file()
    assert state.permission_bits(owner) == 0o600

    lima.calls.clear()
    existing = project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    assert existing.created is False
    assert lima.calls == [["limactl", "disk", "list", "--json"]]


def test_lima_existing_disk_without_receipt_is_never_adopted_or_deleted(roots: state.StatePaths) -> None:
    lima = FakeLima()
    backend_name = project_volumes.lima_backend_name("demo", "data")
    lima.disks[backend_name] = {"name": backend_name, "size": VOLUME_SIZE, "format": "raw"}

    with pytest.raises(StateError, match="refusing to adopt"):
        project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    with pytest.raises(StateError, match="refusing to delete"):
        project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)

    assert backend_name in lima.disks
    assert not any(call[:3] == ["limactl", "disk", "delete"] for call in lima.calls)


def test_lima_conflicting_size_fails_closed(roots: state.StatePaths) -> None:
    lima = FakeLima()
    owned = project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    lima.disks[owned.backend_name]["size"] = 2 * VOLUME_SIZE

    with pytest.raises(StateError, match="size conflict"):
        project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    with pytest.raises(StateError, match="size conflict"):
        project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)

    assert owned.backend_name in lima.disks


def test_lima_receipt_permissions_and_attachment_ownership_are_fail_closed(roots: state.StatePaths) -> None:
    lima = FakeLima()
    owned = project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    receipt = roots.volumes / "demo" / "data.lima-owner.json"
    receipt.chmod(0o644)
    with pytest.raises(StateError, match="0600"):
        project_volumes.verify_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    receipt.chmod(0o600)
    receipt.parent.chmod(0o755)
    with pytest.raises(StateError, match="0700"):
        project_volumes.verify_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    receipt.parent.chmod(0o700)

    lima.disks[owned.backend_name]["instance"] = "demo-api-1"
    assert (
        project_volumes.verify_lima_volume(
            roots,
            "demo",
            "data",
            VOLUME_SIZE,
            allowed_instance="demo-api-1",
            runner=lima,
        )
        is not None
    )
    with pytest.raises(StateError, match="foreign instance"):
        project_volumes.verify_lima_volume(
            roots,
            "demo",
            "data",
            VOLUME_SIZE,
            allowed_instance="other-run",
            runner=lima,
        )
    with pytest.raises(StateError, match="foreign instance"):
        project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)


def test_lima_delete_requires_receipt_verifies_absence_and_avoids_force(roots: state.StatePaths) -> None:
    lima = FakeLima()
    owned = project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    owner = roots.volumes / "demo" / "data.lima-owner.json"
    lima.calls.clear()

    assert project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima) is True
    delete_call = next(call for call in lima.calls if call[:3] == ["limactl", "disk", "delete"])
    assert delete_call == ["limactl", "disk", "delete", owned.backend_name, "--tty=false"]
    assert "--force" not in delete_call
    assert owned.backend_name not in lima.disks
    assert not owner.exists()
    assert project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima) is False


def test_lima_failed_delete_retains_owner_receipt(roots: state.StatePaths) -> None:
    lima = FakeLima()
    owned = project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    lima.fail_delete = True

    with pytest.raises(LifecycleError, match="disk is attached"):
        project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)

    assert owned.backend_name in lima.disks
    assert (roots.volumes / "demo" / "data.lima-owner.json").exists()


def test_lima_successful_delete_that_still_lists_disk_revokes_ownership(roots: state.StatePaths) -> None:
    lima = FakeLima()
    owned = project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    lima.keep_after_delete = True

    with pytest.raises(LifecycleError, match="treated as external"):
        project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)

    assert owned.backend_name in lima.disks
    assert not (roots.volumes / "demo" / "data.lima-owner.json").exists()
    with pytest.raises(StateError, match="refusing to delete"):
        project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)


def test_lima_missing_owned_disk_only_cleans_local_receipt(roots: state.StatePaths) -> None:
    lima = FakeLima()
    owned = project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)
    lima.disks.pop(owned.backend_name)

    assert project_volumes.delete_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima) is False
    assert not (roots.volumes / "demo" / "data.lima-owner.json").exists()
    assert not any(call[:3] == ["limactl", "disk", "delete"] for call in lima.calls)


def test_lima_create_verification_failure_retains_unowned_disk_for_manual_recovery(
    roots: state.StatePaths,
) -> None:
    lima = FakeLima()
    lima.omit_created_from_list = True

    with pytest.raises(LifecycleError, match="retained for manual recovery"):
        project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)

    backend_name = project_volumes.lima_backend_name("demo", "data")
    assert backend_name in lima.disks
    assert not (roots.volumes / "demo" / "data.lima-owner.json").exists()
    assert not any(call[:3] == ["limactl", "disk", "delete"] for call in lima.calls)


@pytest.mark.parametrize("version", ["limactl version 2.0.9\n", "limactl version 3.0.0\n", "unknown\n"])
def test_lima_mutation_requires_supported_v21_series(
    roots: state.StatePaths,
    version: str,
) -> None:
    lima = FakeLima()
    lima.version = version

    with pytest.raises(LifecycleError, match="Lima version|unsupported Lima"):
        project_volumes.ensure_lima_volume(roots, "demo", "data", VOLUME_SIZE, runner=lima)

    assert not lima.disks
    assert not any(call[:3] == ["limactl", "disk", "create"] for call in lima.calls)
