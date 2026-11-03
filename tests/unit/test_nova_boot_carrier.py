"""Pure construction/ownership contracts; no loop, mount, build, or cloud calls."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import stat
import struct
from pathlib import Path

import pytest

from palimpsest_local import nova_boot_carrier as carrier
from palimpsest_local import nova_ceph_boot as runtime


def _decode_newc(data: bytes) -> list[tuple[str, list[int], bytes]]:
    """Independent minimal decoder, deliberately not the production writer."""
    rows = []
    offset = 0
    while True:
        assert data[offset : offset + 6] == b"070701"
        fields = [int(data[offset + 6 + index * 8 : offset + 14 + index * 8], 16) for index in range(13)]
        name_start = offset + 110
        name_end = name_start + fields[11]
        assert data[name_end - 1] == 0
        name = data[name_start : name_end - 1].decode("ascii")
        body_start = (name_end + 3) & ~3
        body = data[body_start : body_start + fields[6]]
        rows.append((name, fields, body))
        offset = (body_start + fields[6] + 3) & ~3
        if name == "TRAILER!!!":
            assert set(data[offset:]) <= {0}
            return rows


def test_streaming_newc_is_canonical_and_console_has_correct_device(tmp_path):
    source = tmp_path / "payload"
    source.write_bytes(b"abcde")
    entries = [
        carrier.CpioEntry("init", stat.S_IFREG | 0o755, source),
        carrier.CpioEntry("dev/console", stat.S_IFCHR | 0o600, rdevmajor=5, rdevminor=1),
        carrier.CpioEntry("dev", stat.S_IFDIR | 0o755),
    ]
    output = io.BytesIO()
    size = carrier.write_newc(entries, output)
    rows = _decode_newc(output.getvalue())
    assert [row[0] for row in rows] == ["dev", "dev/console", "init", "TRAILER!!!"]
    assert rows[1][1][9:11] == [5, 1]
    assert rows[1][1][1] == stat.S_IFCHR | 0o600
    assert rows[2][2] == b"abcde"
    assert all(row[1][2:4] == [0, 0] and row[1][5] == 0 for row in rows)
    assert len(output.getvalue()) == size and size % 512 == 0
    other = io.BytesIO()
    carrier.write_newc(list(reversed(entries)), other)
    assert other.getvalue() == output.getvalue()


@pytest.mark.parametrize(
    "path", ["", "/init", "../init", "a/../b", "a//b", "a/./b", "a\\b", "a\0b", "a\nb", "é", "TRAILER!!!", "a" * 1025]
)
def test_archive_paths_are_bounded(path):
    with pytest.raises(carrier.CarrierError, match="path"):
        carrier.archive_path(path)


def test_cpio_refuses_duplicate_and_missing_parent():
    with pytest.raises(carrier.CarrierError, match="duplicate"):
        carrier.write_newc([carrier.CpioEntry("a", stat.S_IFREG), carrier.CpioEntry("a", stat.S_IFREG)], io.BytesIO())
    with pytest.raises(carrier.CarrierError, match="parent"):
        carrier.write_newc([carrier.CpioEntry("a/b", stat.S_IFREG)], io.BytesIO())


def test_cpio_refuses_foreign_devices_and_sockets():
    with pytest.raises(carrier.CarrierError, match="console"):
        carrier.write_newc([carrier.CpioEntry("evil", stat.S_IFCHR, rdevmajor=1, rdevminor=3)], io.BytesIO())
    with pytest.raises(carrier.CarrierError, match="type"):
        carrier.write_newc([carrier.CpioEntry("socket", stat.S_IFSOCK)], io.BytesIO())


def test_source_symlink_is_not_a_regular_archive_input(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"private")
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises(carrier.CarrierError, match="regular"):
        carrier.write_newc([carrier.CpioEntry("init", stat.S_IFREG, alias)], io.BytesIO())


def test_gzip_initramfs_has_no_timestamp_or_filename_entropy(tmp_path):
    root = tmp_path / "tree"
    (root / "dev").mkdir(parents=True)
    (root / "init").write_bytes(b"#!/bin/sh\n")
    first, second = tmp_path / "a.gz", tmp_path / "b.gz"
    carrier.write_initramfs(root, first)
    carrier.write_initramfs(root, second)
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes()[4:8] == b"\0" * 4
    assert first.read_bytes()[3] & 8 == 0  # no original filename
    rows = _decode_newc(gzip.decompress(first.read_bytes()))
    assert any(name == "dev/console" and fields[9:11] == [5, 1] for name, fields, _ in rows)


def test_canonical_policy_uses_carried_runtime_and_excludes_only_fresh_fields():
    plan = {
        "run_id": "proof",
        "server_id": "old",
        "carrier_digest": "sha256:" + "1" * 64,
        "network": {"address": "192.0.2.2", "cidr": "192.0.2.0/24", "gateway": "192.0.2.1", "dns": ["192.0.2.1"]},
        "command": ["true"],
    }
    actual = carrier.load_runtime_policy(Path(runtime.__file__), plan)
    stable = {key: value for key, value in plan.items() if key != "server_id"}
    stable["network"] = {key: value for key, value in plan["network"].items() if key != "address"}
    assert actual == "sha256:" + hashlib.sha256(carrier.canonical_json(stable)).hexdigest()
    changed = json.loads(json.dumps(plan))
    changed["server_id"] = "new"
    changed["network"]["address"] = "192.0.2.3"
    assert carrier.load_runtime_policy(Path(runtime.__file__), changed) == actual
    changed["network"]["gateway"] = "192.0.2.4"
    assert carrier.load_runtime_policy(Path(runtime.__file__), changed) != actual
    changed = json.loads(json.dumps(plan))
    changed["command"] = ["false"]
    assert carrier.load_runtime_policy(Path(runtime.__file__), changed) != actual


def test_canonical_json_rejects_nonfinite_numbers():
    with pytest.raises(ValueError):
        carrier.canonical_json({"bad": float("nan")})


def test_missing_runtime_policy_helper_is_fatal(tmp_path):
    source = tmp_path / "runtime.py"
    source.write_text("VALUE = 1\n")
    with pytest.raises(carrier.CarrierError, match="lacks plan_policy_digest"):
        carrier.load_runtime_policy(source, {"network": {}})


def _elf_source(tmp_path):
    source = tmp_path / "executable"
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, 62)
    source.write_bytes(header)
    return source


def test_shared_library_helper_failure_is_not_silenced(tmp_path):
    source = _elf_source(tmp_path)

    def failure(argv):
        raise carrier.CarrierError("ldd helper failure")

    with pytest.raises(carrier.CarrierError, match="helper failure"):
        carrier.shared_dependencies(source, failure)
    with pytest.raises(carrier.CarrierError, match="unresolved"):
        carrier.shared_dependencies(source, lambda _: b"libmissing.so => not found\n")
    with pytest.raises(carrier.CarrierError, match="unrecognized"):
        carrier.shared_dependencies(source, lambda _: b"unexpected helper output\n")


def test_foreign_elf_abi_is_rejected_before_helper(tmp_path):
    source = _elf_source(tmp_path)
    data = bytearray(source.read_bytes())
    struct.pack_into("<H", data, 18, 183)
    source.write_bytes(data)
    with pytest.raises(carrier.CarrierError, match="amd64"):
        carrier.shared_dependencies(source, lambda _: pytest.fail("must not inspect foreign ELF"))


def test_missing_tool_is_fatal(monkeypatch):
    monkeypatch.setattr(carrier.shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(carrier.CarrierError, match="required helper unavailable"):
        carrier._tool("ssh")


def test_foreign_loop_refuses_detach_and_preserves_hold(tmp_path):
    disk = tmp_path / "carrier.raw"
    disk.write_bytes(b"new disk")
    calls = []
    owner = carrier.OwnedDisk(
        disk, tmp_path / "resources.json", run=lambda argv: calls.append(argv), inspect=lambda _: (999, 999)
    )
    owner.device = "/dev/loop57"
    with pytest.raises(carrier.CarrierError, match="foreign"):
        owner.close()
    assert calls == []
    journal = json.loads((tmp_path / "resources.json").read_bytes())
    assert journal["state"] == "HOLD" and journal["loop"] == "/dev/loop57"
    assert disk.read_bytes() == b"new disk"


def test_attach_intent_is_durable_before_effect_and_exact_detach(tmp_path):
    disk = tmp_path / "carrier.raw"
    disk.write_bytes(b"new disk")
    journal = tmp_path / "resources.json"
    backing = (disk.stat().st_dev, disk.stat().st_ino)
    observed = None
    commands = []

    def inspect(device):
        assert device == "/dev/loop57"
        return observed

    def run(argv):
        nonlocal observed
        commands.append(argv)
        if argv == ["losetup", "--find"]:
            return b"/dev/loop57\n"
        state = json.loads(journal.read_bytes())
        if argv[:2] == ["losetup", "--partscan"]:
            assert state["state"] == "ATTACH_INTENT"
            assert state["backing"] == list(backing)
            observed = backing
        elif argv[:2] == ["losetup", "--detach"]:
            observed = None
        else:
            pytest.fail(f"unexpected mutation: {argv}")
        return b""

    owner = carrier.OwnedDisk(disk, journal, run=run, inspect=inspect)
    assert owner.attach() == "/dev/loop57"
    owner.close()
    assert json.loads(journal.read_bytes())["state"] == "RELEASED"
    assert commands[-1] == ["losetup", "--detach", "/dev/loop57"]


def test_busy_selected_loop_never_attaches_or_detaches(tmp_path):
    disk = tmp_path / "carrier.raw"
    disk.write_bytes(b"new disk")
    calls = []

    def run(argv):
        calls.append(argv)
        return b"/dev/loop57\n"

    owner = carrier.OwnedDisk(disk, tmp_path / "resources.json", run=run, inspect=lambda _: (123, 456))
    with pytest.raises(carrier.CarrierError, match="not free"):
        owner.attach()
    assert calls == [["losetup", "--find"]]
    assert owner.device is None


def test_manifest_excludes_secrets_and_binds_outside_initramfs_driver(tmp_path):
    root, driver, extra = tmp_path / "root", tmp_path / "driver", tmp_path / "carrier"
    (root / "bootstrap").mkdir(parents=True)
    driver.mkdir()
    (extra / "probe").mkdir(parents=True)
    (root / "bootstrap/runtime.py").write_bytes(b"runtime")
    (root / "bootstrap/seed.json").write_bytes(b"credentials")
    (driver / "libcuda.so").write_bytes(b"driver")
    (extra / "probe/cuda-probe").write_bytes(b"probe")
    manifest = carrier._manifest(root, driver, {"bootstrap/seed.json"}, extra)
    assert set(manifest["files"]) == {"bootstrap/runtime.py", "driver/libcuda.so", "carrier/probe/cuda-probe"}
    assert manifest["files"]["driver/libcuda.so"]["sha256"] == hashlib.sha256(b"driver").hexdigest()
    digest = hashlib.sha256(carrier.canonical_json(manifest)).hexdigest()
    (root / "bootstrap/seed.json").write_bytes(b"changed secret")
    assert (
        hashlib.sha256(
            carrier.canonical_json(carrier._manifest(root, driver, {"bootstrap/seed.json"}, extra))
        ).hexdigest()
        == digest
    )


def test_bundle_symlink_escape_is_rejected(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"not from bundle")
    (bundle / "escape").symlink_to(outside)
    with pytest.raises(carrier.CarrierError, match="escapes"):
        carrier._resolved_file(bundle / "escape", bundle)


def test_wrong_kernel_pin_is_rejected_before_any_helper(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "boot").mkdir(parents=True)
    (bundle / "boot" / f"vmlinuz-{carrier.KERNEL_RELEASE}").write_bytes(b"wrong kernel")
    with pytest.raises(carrier.CarrierError, match="kernel SHA256"):
        carrier._prepare_assets(
            bundle,
            tmp_path / "runtime.py",
            tmp_path / "root",
            tmp_path / "driver",
            lambda _: pytest.fail("must not invoke helper for wrong kernel"),
        )


def test_wrong_module_abi_is_rejected_without_copy_or_loading(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    modules = bundle / "lib/modules" / carrier.KERNEL_RELEASE / "kernel/nvidia-580"
    modules.mkdir(parents=True)
    (modules / "nvidia.ko").write_bytes(b"module fixture")
    monkeypatch.setattr(carrier, "file_identity", lambda _: {"sha256": carrier.KERNEL_SHA256})
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[2] == "version":
            return carrier.DRIVER_VERSION.encode()
        if argv[2] == "signer":
            return b"Canonical Ltd.\n"
        if argv[2] == "vermagic":
            return b"6.8.0-57-generic SMP mod_unload\n"
        pytest.fail(f"unexpected helper: {argv}")

    with pytest.raises(carrier.CarrierError, match="module ABI"):
        carrier._prepare_assets(bundle, tmp_path / "runtime.py", tmp_path / "root", tmp_path / "driver", run)
    assert all(argv[0] == "modinfo" for argv in calls)
    assert not (tmp_path / "root").exists()


def test_host_pin_requires_supported_exact_worker_key(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    (root / "bootstrap").mkdir(parents=True)
    seed = {"worker_address": "192.0.2.1", "worker_known_hosts": "/bootstrap/known_hosts"}
    path = root / "bootstrap/known_hosts"
    # Public-key wire representation: type string followed by the key body.
    encoded = carrier.base64.b64encode(struct.pack(">I", 11) + b"ssh-ed25519" + b"x" * 36).decode()
    monkeypatch.setattr(carrier, "_tool", lambda _: Path("/usr/bin/ssh"))
    path.write_text(f"192.0.2.1 ssh-ed25519 {encoded}\n")
    carrier._check_host_key(seed, root, lambda _: b"ssh-ed25519\n")
    with pytest.raises(carrier.CarrierError, match="unsupported"):
        carrier._check_host_key(seed, root, lambda _: b"ssh-rsa\n")
    path.write_text(f"192.0.2.2 ssh-ed25519 {encoded}\n")
    with pytest.raises(carrier.CarrierError, match="exact worker"):
        carrier._check_host_key(seed, root, lambda _: b"ssh-ed25519\n")


def test_debian_stdlib_customization_and_development_symlinks_are_excluded(tmp_path):
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    outside = tmp_path / "sitecustomize.py"
    outside.write_text("raise RuntimeError('ambient customization')\n")
    (stdlib / "sitecustomize.py").symlink_to(outside)
    configuration = stdlib / "config-3.12-x86_64-linux-gnu"
    configuration.mkdir()
    (configuration / "libpython3.12.so").symlink_to(outside)
    (stdlib / "json.py").write_text("VALUE = 1\n")
    destination = tmp_path / "copied"
    carrier._copy_tree(stdlib, destination, boundary=stdlib, skip=carrier._stdlib_excluded)
    assert (destination / "json.py").is_file()
    assert not (destination / "sitecustomize.py").exists()
    assert not (destination / configuration.name).exists()


def test_bundle_dependency_cannot_follow_symlink_to_host_library(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    host_library = tmp_path / "host-libcuda.so"
    host_library.write_bytes(b"host driver must not leak")
    alias = bundle / "libcuda.so"
    alias.symlink_to(host_library)
    with pytest.raises(carrier.CarrierError, match="escapes"):
        carrier._copy_executable(
            alias,
            tmp_path / "driver",
            "usr/lib/libcuda.so",
            lambda _: pytest.fail("must fail before ldd"),
            bundle=bundle,
        )


def test_carrier_partition_budget_fails_before_device_effects(tmp_path):
    payload = tmp_path / "driver"
    payload.mkdir()
    (payload / "libcuda.so").write_bytes(b"x" * 4097)
    expected = 256 * 1024**2 + 4096 + 8192
    assert carrier.ensure_carrier_capacity((payload,), partition_bytes=expected) == expected
    with pytest.raises(carrier.CarrierError, match="no disk formatted"):
        carrier.ensure_carrier_capacity((payload,), partition_bytes=expected - 1)


def test_driver_injection_never_includes_worker_libc(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    source = bundle / "usr/bin/nvidia-smi"
    nvml = bundle / "usr/lib/x86_64-linux-gnu/libnvidia-ml.so.580.178.04"
    host_libc = tmp_path / "host/usr/lib/x86_64-linux-gnu/libc.so.6"
    for path in (source, nvml, host_libc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())
    root, driver = tmp_path / "initramfs", tmp_path / "driver"

    def dependencies(path, run, *, bundle=None):
        return [nvml, host_libc] if path == source else []

    monkeypatch.setattr(carrier, "shared_dependencies", dependencies)
    carrier._copy_executable(source, driver, "usr/bin/nvidia-smi", lambda _: b"", bundle=bundle, system_root=root)
    assert (driver / nvml.relative_to(bundle)).is_file()
    assert (driver / "usr/bin/nvidia-smi").is_file()
    assert list(driver.rglob("libc.so.6")) == []
    assert len(list(root.rglob("libc.so.6"))) == 1


def test_packaged_nvidia_bits_and_signature_sidecars_are_never_modules_or_staged(tmp_path):
    modules = tmp_path / "bundle/lib/modules" / carrier.KERNEL_RELEASE
    actual = {
        "kernel/nvidia-580/nvidia.ko": b"assembled signed NVIDIA module",
        "kernel/nvidia-580/nvidia-uvm.ko": b"assembled signed UVM module",
        "kernel/drivers/block/virtio_blk.ko.zst": b"compressed zstd module",
        "kernel/fs/ext4/ext4.ko.xz": b"compressed xz module",
        "kernel/fs/overlayfs/overlay.ko.gz": b"compressed gzip module",
    }
    assembler = {
        "kernel/nvidia-580/bits/nvidia.ko.sig": b"Canonical detached signature",
        "kernel/nvidia-580/bits/nvidia.o": b"precompiled assembler object",
        "kernel/nvidia-580/bits/nvidia.ko": b"build-only intermediate module",
        "kernel/nvidia-580/nvidia.ko.sig": b"sidecar outside bits",
        "kernel/nvidia-580srv/bits/nvidia.ko.sig": b"server detached signature",
        "kernel/nvidia-595/bits/nvidia.ko.sig": b"other driver detached signature",
        "kernel/nvidia-595/bits/nvidia.o": b"other driver object",
    }
    metadata = {"modules.builtin": b"kernel/fs/ext4/ext4.ko\n", "modules.builtin.modinfo": b"builtin metadata"}
    for name, content in {**actual, **assembler, **metadata}.items():
        source = modules / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    selected = carrier.module_files(modules)
    assert {path.relative_to(modules).as_posix() for path in selected} == set(actual)
    assert all(not path.name.endswith(".sig") and "bits" not in path.parts for path in selected)
    target = tmp_path / "initramfs/lib/modules" / carrier.KERNEL_RELEASE
    carrier._copy_tree(modules, target, boundary=modules, skip=carrier._module_asset_excluded)
    assert {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()} == set(actual) | set(
        metadata
    )
    for name, content in actual.items():
        assert (target / name).read_bytes() == content  # appended signatures unchanged
    for name, content in assembler.items():
        assert (modules / name).read_bytes() == content  # source assembly evidence retained
        assert not (target / name).exists()


def test_module_bits_exclusion_does_not_depend_on_ancestors_above_bundle(tmp_path):
    modules = tmp_path / "nvidia-prep/lib/modules" / carrier.KERNEL_RELEASE
    module = modules / "kernel/drivers/other/bits/example.ko"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"ordinary module, not NVIDIA assembler input")
    assert carrier.module_files(modules) == [module]
    target = tmp_path / "copied"
    carrier._copy_tree(modules, target, boundary=modules, skip=carrier._module_asset_excluded)
    assert (target / module.relative_to(modules)).read_bytes() == module.read_bytes()


@pytest.mark.parametrize("family", ["nvidia-580srv", "nvidia-595"])
def test_competing_final_nvidia_module_is_rejected_before_boot(tmp_path, family):
    modules = tmp_path / "modules"
    competing = modules / "kernel" / family / "nvidia.ko"
    competing.parent.mkdir(parents=True)
    competing.write_bytes(b"same kernel ABI does not establish selected driver identity")
    with pytest.raises(carrier.CarrierError, match="competing NVIDIA module family"):
        carrier.module_files(modules)


def published_carrier_inputs():
    from palimpsest_local.image_manifest import native_image_projection, normalize_manifest
    from palimpsest_local.nova_native_cloud import _native_plan, _native_seed

    image = native_image_projection(
        normalize_manifest(
            {
                "schema": "palimpsest.image-manifest.v1",
                "mode": "oci-root",
                "platform": "linux/amd64",
                "os_family": "ubuntu",
                "os_version": "22.04",
                "source": {"kind": "local"},
                "runtime": {"entrypoint": [], "cmd": [], "env": ["EXACT=original"], "working_dir": "/", "user": "0"},
                "metadata": {"description": "full canonical metadata"},
                "layers": [{"digest": "sha256:" + "a" * 64, "size_bytes": 128}],
            }
        )
    )
    role = "native-" + "1" * 32
    plan = _native_plan(
        image,
        ["/bin/true"],
        "cpu",
        "b" * 32,
        role,
        {"address": "10.0.0.2", "cidr": "10.0.0.0/24", "gateway": "10.0.0.1", "dns": ["8.8.8.8"]},
    )
    seed = _native_seed(role, "1" * 32, "b" * 32, image["source"]["manifest_digest"], "10.0.0.1", 2**53 - 1)
    return plan, seed


def test_carrier_published_manifest_uses_full_canonical_digest_without_registry_root():
    plan, seed = published_carrier_inputs()
    source = plan["image"]["source"]
    assert "root_digest" not in source
    assert carrier._source_image_digest(source) == seed["expected_image_digest"]
    assert carrier.load_runtime_policy(Path(runtime.__file__), plan, seed=seed) == runtime.plan_policy_digest(plan)
    source["manifest"]["metadata"]["description"] = "changed"
    with pytest.raises(carrier.CarrierError, match="manifest digest mismatch"):
        carrier._source_image_digest(source)


@pytest.mark.parametrize("change", ["runtime", "layer-id", "seed"])
def test_carrier_validates_published_execution_and_parent_chain_not_just_digest(change):
    plan, seed = published_carrier_inputs()
    image = plan["image"]
    if change == "runtime":
        image["runtime"] = {**image["runtime"], "env": ["EXACT=changed"]}
    elif change == "layer-id":
        image["source"]["manifest"]["layers"][0]["id"] = "sha256:" + "c" * 64
        identity = "sha256:" + hashlib.sha256(carrier.canonical_json(image["source"]["manifest"])).hexdigest()
        image["source"]["manifest_digest"] = seed["expected_image_digest"] = identity
    else:
        seed["expected_image_digest"] = "sha256:" + "c" * 64
    with pytest.raises(RuntimeError, match="published_"):
        carrier.load_runtime_policy(Path(runtime.__file__), plan, seed=seed)


def test_carrier_registry_pin_remains_original_root_not_selected_manifest():
    root, manifest = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    assert carrier._source_image_digest({"root_digest": root, "manifest_digest": manifest}) == root
