"""Linux/amd64 execution tests for the packaged freestanding stage-1 ELF."""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_guest_stage1 import parse_guest_kernel_cmdline, verify_guest_stage1_transport
from palimpsest_local.oci_process import OCIProcessSpec, OCIUserSpec
from palimpsest_local.oci_provenance import canonical_json_bytes
from palimpsest_local.oci_stage1 import OCIStage1Plan
from palimpsest_local.oci_stage1_transport import build_stage1_transport

_HEADER = struct.Struct("<16sIIQ32s")
_TOOLCHAIN = "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _toolchain_present() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", _TOOLCHAIN],
                check=False,
                capture_output=True,
            ).returncode
            == 0
        )
    except OSError:
        return False


pytestmark = [
    pytest.mark.guest_stage1_binary,
    pytest.mark.skipif(not _docker_ready(), reason="Docker daemon is unavailable"),
]


def _plan(*, environment_name: str = "LANG") -> OCIStage1Plan:
    return OCIStage1Plan(
        run_id="f6f546e2-e734-4920-9eff-1762b348a249",
        run_name="7binary",
        boot_plan_digest="sha256:" + "a" * 64,
        domain_core_digest="sha256:" + "b" * 64,
        root={
            "filesystem": "ext4",
            "generation": 10**80,
            "mount_options": ["rw", "nodev", "nosuid"],
            "serial": "1" * 20,
            "volume_id": "1fd7a60e-fdb2-4877-91d3-148bbca3884f",
        },
        layers=(
            {
                "filesystem": "squashfs",
                "mount_options": ["ro", "nodev", "nosuid"],
                "ordinal": 0,
                "serial": "2" * 20,
            },
        ),
        process=OCIProcessSpec(
            ("/usr/bin/한글", "line\nbreak", "\b", "\t", "\f", "\r"),
            ((environment_name, "한국어"),),
            "/srv/자료",
            OCIUserSpec("4294967294", "4294967294"),
            15,
        ),
    )


def _transport_serial(digest: str) -> str:
    return hashlib.sha256(f"palimpsest-oci-root-stage1-transport-v1\0{digest}".encode()).hexdigest()[:20]


def _cmdline(plan: OCIStage1Plan, artifact_digest: str) -> str:
    serial = _transport_serial(artifact_digest)
    lowers = ",".join(f"virtio-{layer['serial']}" for layer in plan.layers)
    return (
        f"console=ttyS0 rdinit=/init palimpsest.resource={plan.boot_plan_digest} "
        f"palimpsest.core={plan.domain_core_digest} palimpsest.stage1={artifact_digest} "
        f"palimpsest.stage1dev=virtio-{serial} palimpsest.root=virtio-{plan.root['serial']} "
        f"palimpsest.lowers={lowers}\n"
    )


def _envelope(payload: bytes) -> bytes:
    header = _HEADER.pack(b"PALIMPSEST-S1\0\0\0", 1, 64, len(payload), hashlib.sha256(payload).digest())
    size = (64 + len(payload) + 4095) // 4096 * 4096
    return header + payload + b"\0" * (size - 64 - len(payload))


def _write_fixture(root: Path, plan: OCIStage1Plan, artifact: bytes) -> None:
    digest = f"sha256:{hashlib.sha256(artifact).hexdigest()}"
    serial = _transport_serial(digest)
    sys_device = root / "sys/class/block/vdb"
    sys_device.mkdir(parents=True)
    (root / "proc").mkdir()
    (root / "dev").mkdir()
    (sys_device / "serial").write_text(serial + "\n", encoding="ascii")
    (sys_device / "ro").write_text("1\n", encoding="ascii")
    (sys_device / "driver").write_text("virtio_blk\n", encoding="ascii")
    (sys_device / "dev").write_text("0:0\n", encoding="ascii")
    (root / "proc/cmdline").write_text(_cmdline(plan, digest), encoding="ascii")
    (root / "dev/vdb").write_bytes(artifact)
    (root / "dev/vdb").chmod(0o444)
    for directory in (root, *tuple(path for path in root.rglob("*") if path.is_dir())):
        directory.chmod(0o755)


@pytest.fixture(scope="module")
def scratch_image(tmp_path_factory: pytest.TempPathFactory) -> str:
    context = tmp_path_factory.mktemp("guest-stage1-image")
    repository = Path(__file__).resolve().parents[2]
    shutil.copyfile(repository / "src/palimpsest_local/assets/oci-stage1-init.x86_64", context / "init")
    (context / "Dockerfile").write_text(
        'FROM scratch\nCOPY --chmod=0755 init /init\nENTRYPOINT ["/init"]\n',
        encoding="ascii",
    )
    completed = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "--network", "none", "-q", str(context)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    image = completed.stdout.strip()
    yield image
    subprocess.run(["docker", "image", "rm", "--force", image], check=False, capture_output=True)


def _run(image: str, fixture: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--init",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--mount",
            f"type=bind,src={fixture.resolve()},dst=/fixture,readonly",
            image,
            "--fixture-v1",
            "/fixture",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(not _toolchain_present(), reason="pinned linux/amd64 GCC image is not local")
def test_pinned_offline_build_reproduces_packaged_elf_twice(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    expected = repository / "src/palimpsest_local/assets/oci-stage1-init.x86_64"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for output in (first, second):
        subprocess.run(
            [str(repository / "scripts/build_oci_guest_init.sh"), str(output)],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert first.read_bytes() == second.read_bytes() == expected.read_bytes()


def test_freestanding_binary_matches_python_reference_for_canonical_unicode_fixture(
    scratch_image: str,
    tmp_path: Path,
) -> None:
    plan = _plan()
    built = build_stage1_transport(plan)
    bindings = parse_guest_kernel_cmdline(_cmdline(plan, built.receipt.artifact_digest))
    assert verify_guest_stage1_transport(built.artifact, bindings).plan == plan
    _write_fixture(tmp_path, plan, built.artifact)

    completed = _run(scratch_image, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "palimpsest guest stage1 fixture: verified\n"


def test_freestanding_binary_matches_reference_for_large_valid_environment_name(
    scratch_image: str,
    tmp_path: Path,
) -> None:
    plan = _plan(environment_name="A" * (40 * 1024))
    built = build_stage1_transport(plan)
    bindings = parse_guest_kernel_cmdline(_cmdline(plan, built.receipt.artifact_digest))
    assert verify_guest_stage1_transport(built.artifact, bindings).plan == plan
    _write_fixture(tmp_path, plan, built.artifact)

    completed = _run(scratch_image, tmp_path)

    assert completed.returncode == 0, completed.stderr


def test_freestanding_binary_rejects_noncanonical_trailing_lower_separator(
    scratch_image: str,
    tmp_path: Path,
) -> None:
    plan = _plan()
    built = build_stage1_transport(plan)
    _write_fixture(tmp_path, plan, built.artifact)
    cmdline_path = tmp_path / "proc/cmdline"
    changed = cmdline_path.read_text(encoding="ascii").replace("\n", ",\n")
    cmdline_path.write_text(changed, encoding="ascii")
    with pytest.raises(ArtifactValidationError, match="device"):
        parse_guest_kernel_cmdline(changed)

    completed = _run(scratch_image, tmp_path)

    assert completed.returncode == 65


def test_non_pid1_without_fixture_arguments_exits_usage(scratch_image: str) -> None:
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--init",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            scratch_image,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 64


@pytest.mark.parametrize(
    ("short", "long"),
    [
        (b"\\b", b"\\u0008"),
        (b"\\t", b"\\u0009"),
        (b"\\n", b"\\u000a"),
        (b"\\f", b"\\u000c"),
        (b"\\r", b"\\u000d"),
    ],
)
def test_freestanding_binary_rejects_noncanonical_short_escape_spelling_like_python(
    scratch_image: str,
    tmp_path: Path,
    short: bytes,
    long: bytes,
) -> None:
    plan = _plan()
    payload = canonical_json_bytes(plan.to_dict())
    assert short in payload
    noncanonical = payload.replace(short, long, 1)
    artifact = _envelope(noncanonical)
    _write_fixture(tmp_path, plan, artifact)
    bindings = parse_guest_kernel_cmdline(_cmdline(plan, f"sha256:{hashlib.sha256(artifact).hexdigest()}"))
    with pytest.raises(ArtifactValidationError, match="JSON"):
        verify_guest_stage1_transport(artifact, bindings)

    completed = _run(scratch_image, tmp_path)

    assert completed.returncode == 68
    assert completed.stderr == "palimpsest guest stage1 fixture: rejected\n"


def test_freestanding_binary_rejects_writable_discovery_and_artifact_metadata(
    scratch_image: str,
    tmp_path: Path,
) -> None:
    plan = _plan()
    built = build_stage1_transport(plan)
    _write_fixture(tmp_path, plan, built.artifact)
    (tmp_path / "sys/class/block/vdb/ro").write_text("0\n", encoding="ascii")
    assert _run(scratch_image, tmp_path).returncode == 66

    (tmp_path / "sys/class/block/vdb/ro").write_text("1\n", encoding="ascii")
    (tmp_path / "dev/vdb").chmod(0o666)
    assert _run(scratch_image, tmp_path).returncode == 67
