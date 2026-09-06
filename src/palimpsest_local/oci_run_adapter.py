"""First local OCI/KVM vertical adapter, separate from cloud-image RunSpec.

Unexpected launch failures retain exact owned resources for inspection. They
never authorize guessing that a domain is inactive or forging terminal proof.
"""

from __future__ import annotations

import os
import signal
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from . import platforms
from .digest import digest_file, digest_hex
from .errors import StateError
from .oci_boot_access import grant_oci_boot_access
from .oci_boot_exports import load_oci_boot_exports, publish_oci_boot_exports
from .oci_host import OCIHostConfig, first_party_boot, preflight_oci_host, verify_runtime_parent
from .oci_lower_access import grant_oci_lower_access
from .oci_lower_exports import publish_oci_lower_exports
from .oci_monitor_client import MonitorClient
from .oci_monitor_coordinator import spawn_monitor_coordinator
from .oci_monitor_ipc import MonitorExecIdentity, _read_preactivation_journal
from .oci_monitor_launch import prepare_monitor_launch_authority
from .oci_packer import discover_squashfs_toolchain
from .oci_process_session import OCIMonitorProcessSession
from .oci_root_access import grant_oci_root_access
from .oci_root_kvm import build_oci_root_domain_plan, commit_oci_root_domain_plan
from .oci_root_prepare import prepare_oci_root_run
from .oci_root_runtime import (
    connect_oci_root_libvirt,
    define_committed_oci_root_domain,
    prepare_oci_root_monitor_binding,
)
from .oci_run_cleanup import load_oci_run_binding, remove_oci_run
from .oci_run_request import LocalOCIRunRequest, materialize_local_oci_run
from .oci_runtime_access import grant_oci_runtime_access
from .oci_shared_traversal import join_oci_shared_traversal
from .oci_stage1_access import grant_oci_stage1_access
from .oci_store import OCIStore
from .state import init_resolved_roots, locked_existing_run, reserve_new_run


@dataclass(frozen=True, slots=True)
class OCILaunchResult:
    record: object
    endpoint: object
    session: OCIMonitorProcessSession | None
    terminal: object = None


def run_local_oci(roots, request: LocalOCIRunRequest, host_config: OCIHostConfig | None = None) -> OCILaunchResult:
    # A handler must never acquire run locks or interrupt a half-written receipt.
    # Queue startup cancellation, then stop through the authenticated monitor.
    with _startup_signals() as interrupted:
        result = _launch_local_oci(roots, request, host_config, interrupted)
    # Include signals queued while constructing the foreground session and
    # restoring handlers, not only those observed by wait_ready.
    if interrupted:
        try:
            stop_oci_run(roots, request.name, expected_record=result.record)
        finally:
            if result.session is not None:
                result.session.close()
        raise StateError(f"OCI startup interrupted and stopped; exact run {request.name!r} is retained for rm")
    return result


@contextmanager
def _startup_signals():
    interrupted = []
    previous = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.getsignal(number)
                signal.signal(number, lambda number, frame: interrupted.append(number) if not interrupted else None)
        yield interrupted
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _launch_local_oci(roots, request, host_config, interrupted):
    if type(request) is not LocalOCIRunRequest:
        raise StateError("OCI launch requires a typed local request")
    request.__post_init__()
    config = OCIHostConfig.from_environment() if host_config is None else host_config
    # All readonly host checks and toolchain identity precede state mutation.
    preflight_oci_host(config, roots, request.name)
    toolchain = discover_squashfs_toolchain(
        config.packer, expected_packer_sha256=digest_hex(digest_file(config.packer))
    )
    profile = platforms.resolve_domain_profile("kvm", "x86_64")
    if profile.uri != "qemu:///system":
        raise StateError("OCI public launch requires the qualified system libvirt profile")
    init_resolved_roots(roots)
    selected = materialize_local_oci_run(request, roots=roots, packer_path=config.packer, toolchain=toolchain)
    store = OCIStore(roots)
    conn = connect_oci_root_libvirt(profile.uri)
    try:
        with first_party_boot(config, roots) as source_boot:
            with reserve_new_run(roots, request.name, request.dispatch_key) as reservation:
                prepared = prepare_oci_root_run(
                    reservation,
                    selected.receipt,
                    store,
                    root_volume_size_bytes=request.root_size_bytes,
                    retention_policy="delete",
                )
            publish_oci_boot_exports(roots, prepared, source_boot, conn=conn)
            boot = load_oci_boot_exports(roots, request.name)
            publish_oci_lower_exports(roots, prepared, store, conn=conn)
            resolved = build_oci_root_domain_plan(
                roots,
                prepared,
                store,
                boot,
                profile,
                memory_mib=request.memory_mib,
                vcpus=request.vcpus,
                network=request.network,
            )
            commit_oci_root_domain_plan(roots, resolved, store)
            define_committed_oci_root_domain(roots, request.name, store, boot, profile, conn=conn)
            with locked_existing_run(roots, request.name) as mutation:
                os.mkdir("monitor-private", 0o700, dir_fd=mutation._run_fd)
                os.fsync(mutation._run_fd)
            binding = prepare_oci_root_monitor_binding(
                roots,
                request.name,
                store,
                boot,
                profile,
                conn=conn,
                boot_attempt_id=str(uuid.uuid4()),
            )
            for grant in (
                grant_oci_runtime_access,
                join_oci_shared_traversal,
                grant_oci_root_access,
                grant_oci_stage1_access,
                grant_oci_boot_access,
                grant_oci_lower_access,
            ):
                grant(roots, binding, conn=conn)
            # No fixture grants/remapping: outside state, ancestors must still
            # satisfy the explicit readonly host admission policy.
            verify_runtime_parent(roots.state.parent)
            if interrupted:
                raise StateError(f"OCI startup interrupted before activation; exact run {request.name!r} is retained")
            with prepare_monitor_launch_authority(
                roots,
                store,
                boot,
                profile,
                binding,
                timeout_seconds=60,
                terminal_timeout_seconds=None,
            ) as authority:
                endpoint = spawn_monitor_coordinator(
                    MonitorExecIdentity(binding, str(uuid.uuid4())), authority, timeout=15
                )
    finally:
        conn.close()
    with MonitorClient(roots, binding, endpoint) as client:
        try:
            observation = client.wait_ready(timeout=75)
            if interrupted:
                client.stop_and_wait(timeout=35)
                raise StateError(f"OCI startup interrupted and stopped; exact run {request.name!r} is retained for rm")
        except StateError as exc:
            raise StateError(
                f"OCI run {request.name!r} retained; inspect/stop/rm using the same state root: {exc}"
            ) from exc
    if request.detached and observation.terminal is not None:
        raise StateError("OCI workload exited before detached READY return; exact run is retained for stop/rm")
    session = None if request.detached else OCIMonitorProcessSession(roots, binding, endpoint)
    return OCILaunchResult(binding.record, endpoint, session, observation.terminal)


def _existing(roots, name, expected_record):
    binding = load_oci_run_binding(roots, name)
    if expected_record is not None and binding.record != expected_record:
        raise StateError("OCI existing run identity changed")
    return binding


def stop_oci_run(roots, name, *, expected_record=None):
    binding = _existing(roots, name, expected_record)
    with locked_existing_run(roots, name, expected=binding.record, lock_timeout=5) as mutation:
        fd = os.open(
            "monitor-private", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=mutation._run_fd
        )
        try:
            endpoint = _read_preactivation_journal(fd, binding)[0].endpoint
        finally:
            os.close(fd)
    with MonitorClient(roots, binding, endpoint) as client:
        return client.stop_and_wait(timeout=35)


def rm_oci_run(roots, name, *, expected_record=None):
    binding = _existing(roots, name, expected_record)
    conn = connect_oci_root_libvirt(binding.libvirt_uri)
    try:
        return remove_oci_run(roots, binding, OCIStore(roots), conn=conn, timeout=10)
    finally:
        conn.close()
