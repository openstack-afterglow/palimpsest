"""Legacy native fixtures cannot turn stage1 qualification into product access."""

import json
import os
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_oci_runtime_access as runtime_tests

from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local import oci_stage1_access as access
from palimpsest_local.errors import ArtifactValidationError, StateError
from palimpsest_local.oci_acl import ACLStructure

_HELPER = runpy.run_path(str(Path(__file__).parents[1] / "kvm" / "oci_monitor_launch_helper.py"))


@pytest.fixture
def case(tmp_path, monkeypatch):
    c = runtime_tests.case.__wrapped__(tmp_path, monkeypatch)
    c.path = c.state.parent / "stage1-plan.raw"
    c.run_fd = os.open(c.state.parent, os.O_RDONLY | os.O_DIRECTORY)
    c.fd = os.open(c.path, os.O_RDONLY | os.O_NOFOLLOW)
    c.target = SimpleNamespace(
        path=c.path,
        descriptor=c.fd,
        opened=os.fstat(c.fd),
        permission="r--",
        original_acl=ACLStructure("r--", (), "---", None, "---"),
    )
    c.acl = ACLStructure("r--", ((12345, "r--"),), "---", "r--", "---")
    c.broker = SimpleNamespace(
        uid=12345,
        applied=False,
        restored=False,
        ambiguous=False,
        targets=[c.target],
        _getfacl=lambda _: c.acl,
    )
    c.context = {"broker": c.broker, "binding": c.binding, "product_io": False}
    c.adapter = _HELPER["_qualification_stage1_adapter"](
        access.verify_stage1_launch,
        c.context,
        ACLStructure,
    )
    try:
        yield c
    finally:
        os.close(c.fd)
        os.close(c.run_fd)


def _apply(c):
    c.path.chmod(0o440)
    c.broker.applied = True
    info = os.fstat(c.fd)
    c.context["granted"] = {(info.st_dev, info.st_ino): info}


def _verify(c, **kwargs):
    return c.adapter(c.roots, None, c.run_fd, None, binding=c.binding, **kwargs)


def _state(c, update):
    state = json.loads(c.state.read_text())
    update(state)
    c.state.write_text(json.dumps(state))


def test_pregrant_delegates_and_exact_legacy_grant_has_callback_free_final_tail(case):
    c = case
    assert _verify(c) is None
    _apply(c)
    with pytest.raises(StateError):
        access.verify_stage1_launch(c.roots, None, c.run_fd, None, binding=c.binding)
    stamp = _verify(c)
    c.broker._getfacl = lambda _: pytest.fail("final immutable tail called external ACL backend")
    assert _verify(c, metadata_only=True, expected_stamp=stamp) == stamp


def test_actual_unmanaged_launch_accepts_only_fixture_override(case, monkeypatch):
    c = case
    (c.state.parent / "monitor-private").mkdir(mode=0o700)
    with launch.prepare_monitor_launch_authority(c.roots, c.store, c.boot, c.profile, c.binding) as authority:
        _apply(c)
        with pytest.raises(StateError):
            authority.validate()
        monkeypatch.setattr(access, "verify_stage1_launch", c.adapter)
        authority.validate()


@pytest.mark.parametrize("reason", ["product", "member", "key", "null", "broker", "unapplied", "target"])
def test_all_nonlegacy_contexts_delegate_unchanged(case, reason):
    c = case
    _apply(c)
    member = None
    if reason == "product":
        c.context["product_io"] = True
    elif reason == "member":
        member = {"managed": True}
    elif reason in {"key", "null"}:
        _state(c, lambda state: state.update(oci_stage1_access={} if reason == "key" else None))
    elif reason == "broker":
        c.context.pop("broker")
    elif reason == "unapplied":
        c.broker.applied = False
    else:
        c.broker.targets = []
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return "strict-original"

    adapter = _HELPER["_qualification_stage1_adapter"](original, c.context, ACLStructure)
    assert adapter(c.roots, member, c.run_fd, None, binding=c.binding) == "strict-original"
    assert calls[0][0] == (c.roots, member, c.run_fd, None)
    assert calls[0][1] == {"binding": c.binding, "metadata_only": False, "expected_stamp": None}


@pytest.mark.parametrize(
    "change", ["acl", "baseline", "permission", "attempt", "duplicate", "restored", "ambiguous", "snapshot"]
)
def test_exact_broker_authority_required(case, change):
    c = case
    _apply(c)
    if change == "acl":
        c.acl = ACLStructure("r--", ((12347, "r--"),), "---", "r--", "---")
    elif change == "baseline":
        c.target.original_acl = ACLStructure("rw-", (), "---", None, "---")
    elif change == "permission":
        c.target.permission = "rw-"
    elif change == "attempt":
        c.context["binding"] = replace(c.binding, boot_attempt_id="00000000-0000-4000-8000-000000000001")
    elif change == "duplicate":
        c.broker.targets.append(c.target)
    elif change in {"restored", "ambiguous"}:
        setattr(c.broker, change, True)
    else:
        c.context["granted"] = {}
    with pytest.raises(ValueError):
        _verify(c)


@pytest.mark.parametrize("change", ["bytes", "replacement", "hardlink", "mode", "ledger", "plan", "same_mode_acl"])
def test_final_tail_refuses_late_immutable_or_authority_drift(case, change):
    c = case
    _apply(c)
    stamp = _verify(c)
    if change == "bytes":
        c.path.chmod(0o600)
        payload = c.path.read_bytes()
        c.path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        c.path.chmod(0o440)
    elif change == "replacement":
        c.path.rename(c.path.with_suffix(".old"))
        c.path.write_bytes(c.path.with_suffix(".old").read_bytes())
        c.path.chmod(0o440)
    elif change == "hardlink":
        os.link(c.path, c.path.with_suffix(".alias"))
    elif change == "mode":
        c.path.chmod(0o660)
    elif change == "ledger":
        _state(c, lambda state: state.update(oci_stage1_access=None))
    elif change == "plan":
        _state(c, lambda state: state["oci_root_domain"].update(digest="sha256:" + "0" * 64))
    else:
        c.acl = ACLStructure("r--", ((12347, "r--"),), "---", "r--", "---")
        c.path.chmod(0o440)
    with pytest.raises((ValueError, StateError, ArtifactValidationError)):
        _verify(c, metadata_only=True, expected_stamp=stamp)


def test_acl_callback_cannot_mutate_ledger_after_initial_check(case):
    c = case
    _apply(c)

    def read(_):
        _state(c, lambda state: state.update(oci_stage1_access=None))
        return c.acl

    c.broker._getfacl = read
    with pytest.raises(ValueError):
        _verify(c)


def test_full_transport_hash_checked_even_if_fixture_snapshot_is_refreshed(case):
    c = case
    c.path.chmod(0o600)
    payload = c.path.read_bytes()
    c.path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    c.path.chmod(0o400)
    c.target.opened = c.path.stat()
    _apply(c)
    with pytest.raises(ArtifactValidationError):
        _verify(c)


def test_final_tail_cannot_start_without_initial_stamp(case):
    _apply(case)
    with pytest.raises(ValueError):
        _verify(case, metadata_only=True)
