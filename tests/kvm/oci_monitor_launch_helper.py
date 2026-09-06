"""Test-only clean launcher and qualified child adapter; never a runtime entrypoint."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace


def _exception_evidence(error: BaseException) -> str:
    """Expose suppressed exception context for qualification, without locals."""
    parts = []
    seen = set()
    for index in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        parts.append(f"Exception context {index}:\n")
        parts.extend(traceback.format_exception(type(error), error, error.__traceback__, chain=False))
        error = error.__cause__ or error.__context__
    return "".join(parts)


def _metadata_differences(entry, opened, visible, granted=None):
    # Explicit numeric metadata fields only; never include paths or the frame.
    fields = {
        "device": "st_dev",
        "inode": "st_ino",
        "uid": "st_uid",
        "gid": "st_gid",
        "nlink": "st_nlink",
        "mode": "st_mode",
        "size": "st_size",
        "mtime_ns": "st_mtime_ns",
        "ctime_ns": "st_ctime_ns",
    }
    result = {}
    for label, snapshot in (("opened", opened), ("visible", visible)):
        differences = {
            name: [entry[name], getattr(snapshot, field)]
            for name, field in fields.items()
            if name in entry and entry[name] != getattr(snapshot, field)
        }
        if differences:
            result[label] = differences
    if granted is not None:
        result["grant"] = {
            label: {
                name: [getattr(granted, field), getattr(snapshot, field)]
                for name, field in fields.items()
                if getattr(granted, field) != getattr(snapshot, field)
            }
            for label, snapshot in (("opened", opened), ("visible", visible))
        }
    return result


def _guard_lifecycle_connect(original_connect, path: Path):
    def connect(channel, address):
        if isinstance(address, (str, bytes, os.PathLike)) and os.fsdecode(address) == str(path):
            raise AssertionError("direct lifecycle socket connection is forbidden")
        return original_connect(channel, address)

    return connect


def _validate_qualified_boot_relabel(key, entry, opened, visible, target, granted, policy):
    """Prove one copied BOOT artifact across qualified libvirt DAC relabeling."""
    if key not in {"kernel", "initramfs"}:
        raise ValueError("qualified DAC relabel is limited to boot artifacts")
    activity = policy["prove_activity"]()
    if type(activity) is not int or activity not in {0, 1}:
        raise ValueError("qualified boot domain activity is invalid")
    owner = (policy["uid"], policy["gid"]) if activity else (entry["uid"], entry["gid"])
    immutable = {
        "device": "st_dev",
        "inode": "st_ino",
        "nlink": "st_nlink",
        "size": "st_size",
        "mtime_ns": "st_mtime_ns",
    }

    def verify(info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode != granted.st_mode
            or info.st_nlink != 1
            or (info.st_uid, info.st_gid) != owner
            or any(getattr(info, field) != entry[name] for name, field in immutable.items())
        ):
            raise ValueError("qualified boot immutable identity or selected DAC owner changed")

    before = os.fstat(target.descriptor)
    for info in (opened, visible, before, target.path.lstat()):
        verify(info)
    digest = hashlib.sha256()
    offset = 0
    while offset < entry["size"]:
        chunk = os.pread(target.descriptor, min(1024 * 1024, entry["size"] - offset), offset)
        if not chunk:
            raise ValueError("qualified boot artifact ended during verification")
        digest.update(chunk)
        offset += len(chunk)
    if "sha256:" + digest.hexdigest() != policy["digests"][key]:
        raise ValueError("qualified boot artifact digest changed")
    after = os.fstat(target.descriptor)
    final = target.path.lstat()
    for info in (after, final):
        verify(info)
        if any(
            getattr(info, field) != getattr(before, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        ):
            raise ValueError("qualified boot artifact changed during digest verification")
    if policy["prove_activity"]() != activity:
        raise ValueError("qualified boot domain changed during verification")
    return before


def _qualification_metadata_adapter(original_metadata, context, acl_structure, acl_mode):
    """Permit exactly the broker's recorded grant, never a general mode bypass."""

    def metadata(key, entry, opened, visible, owner_uid):
        # The control socket and ownership journal are never QEMU resources.
        if key == "monitor" or (
            context.get("product_io", False)
            and key
            in {
                "run",
                "runtime_io",
                "runtime_console",
                "root_disk",
                "root_volumes",
                "stage1_transport",
                "kernel",
                "initramfs",
            }
        ):
            return original_metadata(key, entry, opened, visible, owner_uid)
        broker = context.get("broker")
        target = (
            None
            if broker is None
            else next(
                (
                    item
                    for item in broker.targets
                    if (item.opened.st_dev, item.opened.st_ino) == (opened.st_dev, opened.st_ino)
                ),
                None,
            )
        )
        if target is None or not broker.applied:
            return original_metadata(key, entry, opened, visible, owner_uid)
        # FD/path identity checks also remain in the production validator.
        original = target.original_acl
        mask = acl_mode(original.group) | acl_mode(target.permission)
        mask_text = f"{'r' if mask & 4 else '-'}{'w' if mask & 2 else '-'}{'x' if mask & 1 else '-'}"
        boot_policy = context.get("boot_relabel") if key in {"kernel", "initramfs"} else None
        users = ((broker.uid, target.permission),)
        if boot_policy is not None:
            users = tuple(sorted((*users, (boot_policy["reader_uid"], "r--"))))
        expected = acl_structure(original.user, users, original.group, mask_text, original.other)
        granted = context["granted"][(opened.st_dev, opened.st_ino)]
        if boot_policy is None:
            current = broker._verify_held(target)
        else:
            current = _validate_qualified_boot_relabel(key, entry, opened, visible, target, granted, boot_policy)
        if broker._getfacl(target) != expected or any(
            value.st_mode != granted.st_mode
            or (
                boot_policy is None
                and key != "runtime_console"
                and not stat.S_ISDIR(value.st_mode)
                and value.st_ctime_ns != granted.st_ctime_ns
            )
            for value in (opened, visible, current)
        ):
            raise ValueError("qualified monitor ACL changed")
        fields = (
            "st_dev",
            "st_ino",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )

        def normalized(value):
            data = {field: getattr(value, field) for field in fields}
            data["st_mode"] = entry["mode"]
            data["st_ctime_ns"] = entry["ctime_ns"]
            if boot_policy is not None:
                data["st_uid"] = entry["uid"]
                data["st_gid"] = entry["gid"]
            return SimpleNamespace(**data)

        return original_metadata(key, entry, normalized(opened), normalized(visible), owner_uid)

    def diagnosed_metadata(key, entry, opened, visible, owner_uid):
        try:
            return metadata(key, entry, opened, visible, owner_uid)
        except Exception as error:
            granted = context.get("granted", {}).get((opened.st_dev, opened.st_ino))
            evidence = {"key": key, "differences": _metadata_differences(entry, opened, visible, granted)}
            raise ValueError("qualified monitor metadata rejected: " + json.dumps(evidence, sort_keys=True)) from error

    return diagnosed_metadata


def _qualification_stage1_adapter(original_verify, context, acl_structure):
    """Observe only the legacy broker's exact immutable stage1 grant.

    This is not a product access policy. Managed members and all pre-grant
    calls retain the production validator, including its strict0400 baseline.
    """
    from palimpsest_local import oci_stage1_access as access
    from palimpsest_local.oci_stage1_transport import OCIStage1Plan, verify_stage1_transport

    def verify(roots, member, run_fd, fd, *, binding, metadata_only=False, expected_stamp=None):
        def original():
            return original_verify(
                roots,
                member,
                run_fd,
                fd,
                binding=binding,
                metadata_only=metadata_only,
                expected_stamp=expected_stamp,
            )

        broker = context.get("broker")
        if context.get("product_io", False) or member is not None or broker is None or not broker.applied:
            return original()
        state = access._read_pinned_json_object(run_fd, "state.json")
        if access.OCI_STAGE1_ACCESS_STATE_KEY in state:
            return original()
        run_path = roots.runs / binding.record.name
        path = run_path / "stage1-plan.raw"
        targets = [target for target in broker.targets if target.path == path]
        if not targets:
            return original()
        if len(targets) != 1 or broker.restored or broker.ambiguous or context.get("binding") != binding:
            raise ValueError("qualified stage1 broker authority changed")
        target = targets[0]
        plan = access._plan(state, binding)
        transport = access._transport(plan)
        baseline = target.opened
        granted = context.get("granted", {}).get((baseline.st_dev, baseline.st_ino))
        if (
            target.permission != "r--"
            or target.original_acl != acl_structure("r--", (), "---", None, "---")
            or not stat.S_ISREG(baseline.st_mode)
            or stat.S_IMODE(baseline.st_mode) != 0o400
            or baseline.st_uid != binding.owner_uid
            or baseline.st_nlink != 1
            or baseline.st_size != transport.artifact_size_bytes
            or transport.artifact_digest != binding.stage1_artifact_digest
            or granted is None
        ):
            raise ValueError("qualified stage1 baseline or transport changed")
        stamp = access._immutable_stamp(granted)
        if metadata_only and expected_stamp != stamp:
            raise ValueError("qualified stage1 validation stamp changed")
        run_identity = os.fstat(run_fd)

        def tail():
            if (
                context.get("binding") != binding
                or not broker.applied
                or broker.restored
                or broker.ambiguous
                or context.get("broker") is not broker
                or [item for item in broker.targets if item.path == path] != [target]
            ):
                raise ValueError("qualified stage1 broker authority changed")
            visible_run = run_path.lstat()
            if (
                not stat.S_ISDIR(visible_run.st_mode)
                or visible_run.st_uid != binding.owner_uid
                or (visible_run.st_dev, visible_run.st_ino) != (run_identity.st_dev, run_identity.st_ino)
            ):
                raise ValueError("qualified stage1 run identity changed")
            infos = [
                os.fstat(target.descriptor),
                path.lstat(),
                os.stat(path.name, dir_fd=run_fd, follow_symlinks=False),
            ]
            if fd is not None:
                infos.append(os.fstat(fd))
            for info in infos:
                if (
                    access._immutable_stamp(info) != stamp
                    or not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o440
                    or (
                        info.st_dev,
                        info.st_ino,
                        info.st_uid,
                        info.st_gid,
                        info.st_nlink,
                        info.st_size,
                        info.st_mtime_ns,
                    )
                    != (
                        baseline.st_dev,
                        baseline.st_ino,
                        baseline.st_uid,
                        baseline.st_gid,
                        1,
                        baseline.st_size,
                        baseline.st_mtime_ns,
                    )
                ):
                    raise ValueError("qualified stage1 immutable metadata changed")
            current = access._read_pinned_json_object(run_fd, "state.json")
            if access.OCI_STAGE1_ACCESS_STATE_KEY in current or access._plan(current, binding) != plan:
                raise ValueError("qualified stage1 ledger authority changed")

        tail()
        if not metadata_only and broker._getfacl(target) != acl_structure(
            "r--", ((broker.uid, "r--"),), "---", "r--", "---"
        ):
            raise ValueError("qualified stage1 ACL changed")
        verify_stage1_transport(
            os.pread(target.descriptor, transport.artifact_size_bytes + 1, 0),
            transport,
            expected_stage1_plan=OCIStage1Plan.from_domain_plan(plan),
        )
        tail()
        return stamp

    return verify


def _install_qualification(root: Path, *, product_io: bool = False) -> None:
    # The qualified server supplies libvirt-python outside the test venv.
    # Production intentionally does not inherit or infer this search path.
    sys.path.append("/usr/lib/python3/dist-packages")
    import palimpsest_local.oci_monitor_launch as authority_module
    from palimpsest_local import oci_root_kvm, oci_root_runtime, oci_runtime_io, oci_stage1_access
    from palimpsest_local.state import read_run_ledger_snapshot

    fixture = runpy.run_path(str(Path(__file__).with_name("test_oci_root_libvirt_live.py")))
    original_lower = oci_root_kvm._verified_lower_path
    original_connect = oci_root_runtime.connect_oci_root_libvirt
    original_run = authority_module.MonitorLaunchAuthority.run
    original_metadata = authority_module._validate_entry_metadata
    original_io_metadata = oci_runtime_io._validate_runtime_io_metadata
    context: dict = {"product_io": product_io}

    def lower(roots, digest, size):
        return fixture["_stage_qualified_lower"](original_lower(roots, digest, size), digest, size, root / "l")

    metadata = _qualification_metadata_adapter(
        original_metadata, context, fixture["_ACLStructure"], fixture["_acl_mode"]
    )

    def connect(uri):
        assert threading.current_thread() is not threading.main_thread()
        conn = original_connect(uri)
        roots, store, boot, profile = context["resources"]
        binding = context["binding"]
        resolved = oci_root_kvm.resolve_committed_oci_root_domain_plan(
            roots,
            read_run_ledger_snapshot(roots, binding.record.name),
            store,
            boot,
            profile,
            expected_status="defined",
        )
        uid, gid = fixture["_parse_qemu_dac_baselabel"](conn.getCapabilities())
        specifications = fixture["_qualification_acl_specifications"](root, resolved.xml)
        if product_io:
            import xml.etree.ElementTree as ET

            root_source = ET.fromstring(resolved.xml).find("./devices/disk/target[@dev='vda']/../source")
            assert root_source is not None
            specifications = fixture["_without_product_access_grants"](
                specifications,
                roots,
                roots.runs / binding.record.name,
                Path(root_source.attrib["file"]),
                boot_paths=(boot.kernel.path, boot.initramfs.path),
            )
        broker = fixture["_QualificationDACBroker"](root, uid, specifications)
        context["broker"] = broker
        proxy = fixture["_ActivationConnectionProxy"](conn, binding.domain_uuid, broker)
        original_apply = broker.apply

        def apply():
            original_apply()
            # Keep the qualification owner genuinely able to reopen the copied
            # BOOT files after libvirt changes their DAC owner to QEMU. This
            # narrowly named read grant is not an O_PATH/read-rights bypass.
            for target in broker.targets:
                if target.path not in {boot.kernel.path, boot.initramfs.path}:
                    continue
                assert not product_io, "managed BOOT access must not use the fixture broker"
                broker._verify_held(target)
                broker._command(["setfacl", "-m", f"u:{os.geteuid()}:r--", "--", target.fd_path], target)
                original = target.original_acl
                expected = fixture["_ACLStructure"](
                    original.user,
                    tuple(sorted(((broker.uid, "r--"), (os.geteuid(), "r--")))),
                    original.group,
                    "r--",
                    original.other,
                )
                actual = broker._verify_held(target)
                if (
                    target.permission != "r--"
                    or broker._getfacl(target) != expected
                    or stat.S_IMODE(actual.st_mode) != 0o440
                ):
                    raise ValueError("qualified copied boot reader grant is invalid")
            context["granted"] = {
                (target.opened.st_dev, target.opened.st_ino): os.fstat(target.descriptor) for target in broker.targets
            }

        broker.apply = apply
        domain = proxy.lookupByName(binding.record.name)
        original_create = domain.create

        def prove_activity():
            captured_id = context.get("created_domain_id")
            if type(captured_id) is not int or captured_id <= 0:
                raise ValueError("qualified boot has no captured create identity")
            exact = oci_root_runtime._exact_domain(conn, resolved, binding.domain_uuid)
            active = exact.isActive()
            if type(active) is not int or active not in {0, 1}:
                raise ValueError("qualified boot activity is ambiguous")
            oci_root_runtime._exact_launch_instance(
                conn,
                resolved,
                binding.domain_uuid,
                captured_id,
                binding.expected_definition_projection_digest,
                active=active == 1,
            )
            return active

        if not product_io:
            context["boot_relabel"] = {
                "uid": uid,
                "gid": gid,
                "reader_uid": os.geteuid(),
                "prove_activity": prove_activity,
                "digests": {"kernel": boot.kernel.digest, "initramfs": boot.initramfs.digest},
            }

        def create():
            result = original_create()
            captured_id = domain.ID()
            if type(captured_id) is not int or captured_id <= 0:
                raise ValueError("qualified create did not yield an exact active ID")
            context["created_domain_id"] = captured_id
            if prove_activity() != 1:
                raise ValueError("qualified create did not remain active")
            deadline = time.monotonic() + 30
            # Hold an actual active VM at the create boundary while the launcher
            # has exited, so the test can verify the independent IPC main loop.
            while not (root / "continue-monitor").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("qualified monitor continuation was not delivered")
                time.sleep(0.01)
            return result

        domain.create = create
        return proxy

    def run(self, directory_fd, binding, lease, *, stop_control=None):
        original_socket_connect = socket.socket.connect
        try:
            context["resources"] = self._rebuild()
            context["binding"] = binding
            socket.socket.connect = _guard_lifecycle_connect(
                original_socket_connect, context["resources"][0].runs / binding.record.name / "io" / "lifecycle.sock"
            )
            return original_run(self, directory_fd, binding, lease, stop_control=stop_control)
        except BaseException as error:
            # Qualification evidence only: traceback lines, no captured locals.
            try:
                fd = os.open(
                    root / "monitor-child-error.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "w") as output:
                    output.write(_exception_evidence(error))
            except OSError:
                pass
            raise
        finally:
            socket.socket.connect = original_socket_connect

    oci_root_kvm._verified_lower_path = lower
    oci_root_runtime.connect_oci_root_libvirt = connect
    authority_module._validate_entry_metadata = metadata
    oci_stage1_access.verify_stage1_launch = _qualification_stage1_adapter(
        oci_stage1_access.verify_stage1_launch, context, fixture["_ACLStructure"]
    )
    if not product_io:
        oci_runtime_io._validate_runtime_io_metadata = fixture["_qualification_runtime_io_adapter"](
            original_io_metadata, lambda: context.get("broker")
        )
    authority_module.MonitorLaunchAuthority.run = run


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from palimpsest_local import oci_monitor_ipc as ipc

    if sys.argv[1] in {"child", "child-product-io"}:
        _install_qualification(Path(sys.argv[2]), product_io=sys.argv[1] == "child-product-io")
        return ipc._entrypoint([__file__, *sys.argv[-3:]])

    from palimpsest_local.oci_monitor_launch import prepare_monitor_launch_authority
    from palimpsest_local.oci_root_kvm import verify_host_boot_artifacts
    from palimpsest_local.oci_store import OCIStore
    from palimpsest_local.platforms import resolve_domain_profile
    from palimpsest_local.state import StatePaths

    payload = json.load(sys.stdin)
    root = Path(payload["root"])
    roots = StatePaths(root / "c", root / "s")
    binding = ipc.MonitorPreActivationBinding.from_dict(payload["binding"])
    boot = verify_host_boot_artifacts(
        Path(payload["kernel"]),
        Path(payload["initramfs"]),
        expected_kernel_digest=payload["kernel_digest"],
        expected_initramfs_digest=payload["initramfs_digest"],
    )
    directory_fd = os.open(
        roots.runs / binding.record.name / "monitor-private", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        with prepare_monitor_launch_authority(
            roots,
            OCIStore(roots),
            boot,
            resolve_domain_profile("kvm", "x86_64"),
            binding,
            timeout_seconds=60,
            terminal_timeout_seconds=60,
        ) as authority:

            def popen(argv, **kwargs):
                return subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "child-product-io" if payload.get("product_io", False) else "child",
                        str(root),
                        *argv[1:],
                    ],
                    **kwargs,
                )

            import uuid

            handle = ipc.spawn_monitor_exec(
                directory_fd,
                ipc.MonitorExecIdentity(binding, str(uuid.uuid4())),
                timeout=15,
                launch_authority=authority,
                popen_factory=popen,
            )
            print(json.dumps({"endpoint": handle.endpoint.to_dict(), "launcher_pid": os.getpid()}), flush=True)
            handle.close()
    finally:
        os.close(directory_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
