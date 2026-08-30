"""PR4 first-pass layer intake and source-lease acceptance tests."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import pickle
import tarfile
import threading
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import palimpsest_local.oci_converter as oci_converter
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_changeset import EntryKind
from palimpsest_local.oci_converter import (
    DEFAULT_LAYER_CONVERSION_LIMITS,
    LAYER_INTAKE_POLICY_ID,
    LayerIntakeError,
    RegularPayloadRef,
    stage_layer,
)
from palimpsest_local.oci_image import OCIImageRef
from palimpsest_local.oci_provenance import (
    DOCKER_IMAGE_CONFIG_MEDIA_TYPE,
    DOCKER_IMAGE_MANIFEST_MEDIA_TYPE,
    DOCKER_LAYER_GZIP_MEDIA_TYPE,
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    Descriptor,
)
from palimpsest_local.oci_source import LeasedSourceLayer, LocalLayoutSource, SourceCAS, SourceLeaseError


def _tar(*members: tarfile.TarInfo, payloads: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    payloads = payloads or {}
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for member in members:
            payload = payloads.get(member.name)
            archive.addfile(member, io.BytesIO(payload) if payload is not None else None)
    return output.getvalue()


def _file(name: str, payload: bytes = b"payload") -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = 0o644
    return member, payload


def _descriptor(payload: bytes, media_type: str) -> Descriptor:
    return Descriptor(
        media_type=media_type,
        digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        size=len(payload),
    )


def _replace_first_header_field(payload: bytes, start: int, end: int, value: bytes) -> bytes:
    return _replace_header_field(payload, 0, start, end, value)


def _replace_header_field(payload: bytes, header_offset: int, start: int, end: int, value: bytes) -> bytes:
    changed = bytearray(payload)
    changed[header_offset + start : header_offset + end] = value.ljust(end - start, b"\0")
    changed[header_offset + 148 : header_offset + 156] = b" " * 8
    checksum = sum(changed[header_offset : header_offset + 512])
    changed[header_offset + 148 : header_offset + 156] = f"{checksum:06o}".encode() + b"\0 "
    return bytes(changed)


def _snapshot(
    tmp_path: Path,
    uncompressed: bytes,
    media_type: str,
    *,
    expected_diff_id: str | None = None,
    compressed_override: bytes | None = None,
):
    compressed = (
        gzip.compress(uncompressed, mtime=0)
        if media_type in {OCI_LAYER_GZIP_MEDIA_TYPE, DOCKER_LAYER_GZIP_MEDIA_TYPE}
        else uncompressed
    )
    if compressed_override is not None:
        compressed = compressed_override
    layer = _descriptor(compressed, media_type)
    diff_id = expected_diff_id or f"sha256:{hashlib.sha256(uncompressed).hexdigest()}"
    root = tmp_path / f"layout-{len(list(tmp_path.iterdir()))}"
    blobs = root / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")

    def add_json(value: object, kind: str) -> Descriptor:
        payload = json.dumps(value, separators=(",", ":")).encode()
        descriptor = _descriptor(payload, kind)
        (blobs / descriptor.digest.split(":", 1)[1]).write_bytes(payload)
        return descriptor

    (blobs / layer.digest.split(":", 1)[1]).write_bytes(compressed)
    docker_profile = media_type == DOCKER_LAYER_GZIP_MEDIA_TYPE
    config_media_type = DOCKER_IMAGE_CONFIG_MEDIA_TYPE if docker_profile else OCI_IMAGE_CONFIG_MEDIA_TYPE
    manifest_media_type = DOCKER_IMAGE_MANIFEST_MEDIA_TYPE if docker_profile else OCI_IMAGE_MANIFEST_MEDIA_TYPE
    config = add_json(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        },
        config_media_type,
    )
    manifest = add_json(
        {
            "schemaVersion": 2,
            "mediaType": manifest_media_type,
            "config": config.to_dict(),
            "layers": [layer.to_dict()],
        },
        manifest_media_type,
    )
    (root / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": [manifest.to_dict()]}, separators=(",", ":")),
        encoding="utf-8",
    )
    cas = SourceCAS(tmp_path / f"cas-{len(list(tmp_path.iterdir()))}")
    reference = OCIImageRef(
        registry="registry.example.com",
        repository="team/app",
        requested_reference="registry.example.com/team/app:latest",
    )
    image = LocalLayoutSource.parse(f"oci-layout://{root}@{manifest.digest}").snapshot(reference, cas)
    return cas, image, compressed


@pytest.mark.parametrize(
    "media_type",
    [OCI_LAYER_MEDIA_TYPE, OCI_LAYER_GZIP_MEDIA_TYPE, DOCKER_LAYER_GZIP_MEDIA_TYPE],
)
def test_plain_oci_gzip_and_docker_gzip_produce_the_same_validated_tar(
    tmp_path: Path,
    media_type: str,
) -> None:
    member, payload = _file("etc/message", b"hello")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, compressed = _snapshot(tmp_path, uncompressed, media_type)

    with cas.lease_layer(image, 0) as lease, stage_layer(lease) as staged:
        assert staged.receipt.policy_id == LAYER_INTAKE_POLICY_ID
        assert staged.receipt.policy_fingerprint == DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint
        assert staged.receipt.compressed_size == len(compressed)
        assert staged.receipt.uncompressed_size == len(uncompressed)
        assert staged.receipt.diff_id == f"sha256:{hashlib.sha256(uncompressed).hexdigest()}"
        assert [(item.path, item.kind, item.size) for item in staged.members] == [("etc/message", "file", 5)]
        assert b"".join(staged.chunks(511)) == uncompressed


def test_source_layer_lease_requires_verified_eof_and_is_single_use(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with pytest.raises(SourceLeaseError, match="not consumed"):
        with cas.lease_layer(image, 0) as lease:
            next(lease.chunks(1))

    with cas.lease_layer(image, 0) as lease:
        assert b"".join(lease.chunks(257)) == uncompressed
        with pytest.raises(SourceLeaseError, match="single-use"):
            next(lease.chunks())


def test_source_layer_lease_is_immutable_and_cannot_be_copied(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with cas.lease_layer(image, 0) as lease:
        with pytest.raises(AttributeError):
            lease.diff_id = f"sha256:{hashlib.sha256(uncompressed).hexdigest()}"  # type: ignore[misc]
        with pytest.raises(SourceLeaseError, match="cannot be copied"):
            copy(lease)
        with pytest.raises(SourceLeaseError, match="cannot be copied"):
            deepcopy(lease)
        b"".join(lease.chunks())


def test_foreign_thread_context_exit_still_revokes_leaf_fd(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    context = cas.lease_layer(image, 0)
    lease = context.__enter__()
    failures: list[BaseException] = []

    def exit_context() -> None:
        try:
            context.__exit__(None, None, None)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=exit_context)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], SourceLeaseError)
    with pytest.raises(SourceLeaseError, match="closed"):
        next(lease.chunks())


def test_layer_intake_rejects_raw_paths_and_wrong_diffid(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    with pytest.raises(LayerIntakeError, match="oci-source-lease"):
        with stage_layer(tmp_path):  # type: ignore[arg-type]
            pass

    cas, image, _ = _snapshot(
        tmp_path,
        uncompressed,
        OCI_LAYER_MEDIA_TYPE,
        expected_diff_id="sha256:" + "0" * 64,
    )
    with pytest.raises(LayerIntakeError, match="oci-diffid"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


@pytest.mark.parametrize("mutation", ["truncated", "crc", "trailing", "codec"])
def test_corrupt_or_mismatched_gzip_fails_closed(tmp_path: Path, mutation: str) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    compressed = bytearray(gzip.compress(uncompressed, mtime=0))
    if mutation == "truncated":
        changed = bytes(compressed[:-3])
    elif mutation == "crc":
        compressed[-8] ^= 0xFF
        changed = bytes(compressed)
    elif mutation == "trailing":
        changed = bytes(compressed) + b"junk"
    else:
        changed = uncompressed
    cas, image, _ = _snapshot(
        tmp_path,
        uncompressed,
        OCI_LAYER_GZIP_MEDIA_TYPE,
        compressed_override=changed,
    )
    with pytest.raises(LayerIntakeError, match="oci-gzip"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


@pytest.mark.parametrize(
    "member",
    [
        tarfile.TarInfo(".palimpsest"),
        tarfile.TarInfo("./.palimpsest/state"),
        tarfile.TarInfo(".wh..palimpsest"),
    ],
)
def test_reserved_tree_is_rejected_for_members_and_whiteout_targets(tmp_path: Path, member: tarfile.TarInfo) -> None:
    member.size = 0
    uncompressed = _tar(member)
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with pytest.raises(LayerIntakeError, match="oci-reserved-path"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


def test_reserved_tree_is_rejected_for_pax_path_and_hardlink_target(tmp_path: Path) -> None:
    pax = tarfile.TarInfo("allowed")
    pax.pax_headers = {"path": ".palimpsest/pax"}
    hardlink = tarfile.TarInfo("link")
    hardlink.type = tarfile.LNKTYPE
    hardlink.linkname = ".palimpsest/target"
    for uncompressed in (_tar(pax), _tar(hardlink)):
        cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
        with pytest.raises(LayerIntakeError, match="oci-reserved-path"):
            with cas.lease_layer(image, 0) as lease, stage_layer(lease):
                pass


def test_pax_numeric_metadata_is_applied_without_parser_differential(tmp_path: Path) -> None:
    member, payload = _file("value", b"x")
    member.pax_headers = {"uid": "123", "gid": "456", "mtime": "7.000"}
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with cas.lease_layer(image, 0) as lease, stage_layer(lease) as staged:
        actual = staged.members[0]
        assert (actual.uid, actual.gid, actual.mtime) == (123, 456, 7)


@pytest.mark.parametrize("key,value", [("uid", "-1"), ("uid", "1e3"), ("mtime", "1.5")])
def test_invalid_or_contradictory_pax_numeric_metadata_is_rejected(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    member, payload = _file("value", b"x")
    member.pax_headers = {key: value}
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with pytest.raises(LayerIntakeError, match="oci-pax-metadata"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


def test_pax_size_override_controls_physical_member_framing(tmp_path: Path) -> None:
    member, payload = _file("value", b"x")
    member.pax_headers = {"size": "1"}
    generated = _tar(member, payloads={member.name: payload})
    pax_size = int(generated[124:136].strip(b"\0 ") or b"0", 8)
    member_header_offset = 512 + ((pax_size + 511) // 512) * 512
    uncompressed = _replace_header_field(generated, member_header_offset, 124, 136, b"0")
    with tarfile.open(fileobj=io.BytesIO(uncompressed), mode="r:") as archive:
        parsed = archive.getmember("value")
        assert parsed.size == 1
        assert archive.extractfile(parsed).read() == b"x"

    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with cas.lease_layer(image, 0) as lease, stage_layer(lease) as staged:
        assert staged.members[0].size == 1
        reference = staged.changeset.by_path()["value"].payload
        with staged.lease_regular_payload(reference) as payload_lease:
            assert b"".join(payload_lease.chunks()) == b"x"


def test_pr2_base_fixture_binary_capability_stages_through_the_production_parser(tmp_path: Path) -> None:
    uncompressed = (Path(__file__).parent.parent / "fixtures" / "oci-root" / "base_layer.tar").read_bytes()
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)

    with cas.lease_layer(image, 0) as lease, stage_layer(lease) as staged:
        capability = next(member for member in staged.members if member.path == "cap_file.txt")
        value = dict(capability.xattrs)["security.capability"]
        assert value.encode("latin-1") == b"\x01\0\0\x02\x01" + b"\0" * 15
        reference = staged.changeset.by_path()["cap_file.txt"].payload
        with staged.lease_regular_payload(reference) as payload_lease:
            assert b"".join(payload_lease.chunks()) == b"file with capability"


def test_large_pax_integer_is_range_checked_before_python_int_conversion(tmp_path: Path) -> None:
    member, payload = _file("value", b"x")
    member.pax_headers = {"uid": "9" * 10_000}
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with pytest.raises(LayerIntakeError, match="exceeds its supported range"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


def test_negative_base_header_numeric_metadata_is_rejected(tmp_path: Path) -> None:
    member, payload = _file("value", b"x")
    uncompressed = _replace_first_header_field(
        _tar(member, payloads={member.name: payload}),
        108,
        116,
        b"-1",
    )
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with pytest.raises(LayerIntakeError, match="negative uid"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


def test_similarly_named_nonreserved_tree_is_allowed(tmp_path: Path) -> None:
    member, payload = _file(".palimpsest-data/value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with cas.lease_layer(image, 0) as lease, stage_layer(lease) as staged:
        assert staged.members[0].path == ".palimpsest-data/value"


def test_whiteout_must_be_an_empty_regular_file_without_control_metadata(tmp_path: Path) -> None:
    marker = tarfile.TarInfo("etc/.wh.value")
    marker.type = tarfile.DIRTYPE
    uncompressed = _tar(marker)
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with pytest.raises(LayerIntakeError, match="oci-whiteout"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


@pytest.mark.parametrize("name", [".wh.", ".wh..", ".wh..wh.foo"])
def test_malformed_whiteout_target_is_rejected(tmp_path: Path, name: str) -> None:
    marker = tarfile.TarInfo(name)
    uncompressed = _tar(marker)
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with pytest.raises(LayerIntakeError, match="oci-whiteout"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


def test_staged_changeset_binds_duplicate_path_to_the_winning_payload_occurrence(tmp_path: Path) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for payload in (b"first", b"second"):
            member, _ = _file("value", payload)
            archive.addfile(member, io.BytesIO(payload))
    uncompressed = output.getvalue()
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)

    with cas.lease_layer(image, 0) as source, stage_layer(source) as staged:
        entry = staged.changeset.by_path()["value"]
        assert entry.kind is EntryKind.FILE
        assert isinstance(entry.payload, RegularPayloadRef)
        assert entry.source_ordinal == 1
        assert entry.payload.member_index == 1
        assert entry.payload.size == 6
        assert entry.payload.digest == f"sha256:{hashlib.sha256(b'second').hexdigest()}"
        assert not hasattr(entry.payload, "offset")
        with staged.lease_regular_payload(entry.payload) as payload_lease:
            assert b"".join(payload_lease.chunks(2)) == b"second"


def test_payload_leases_support_reverse_order_without_extra_spools(tmp_path: Path) -> None:
    first, first_payload = _file("first", b"111")
    second, second_payload = _file("second", b"2222")
    uncompressed = _tar(
        first,
        second,
        payloads={first.name: first_payload, second.name: second_payload},
    )
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)

    with cas.lease_layer(image, 0) as source, stage_layer(source) as staged:
        entries = staged.changeset.by_path()
        with (
            staged.lease_regular_payload(entries["first"].payload) as first_lease,
            staged.lease_regular_payload(entries["second"].payload) as second_lease,
        ):
            assert b"".join(second_lease.chunks()) == second_payload
            assert b"".join(first_lease.chunks()) == first_payload


def test_payload_reference_and_lease_are_nontransferable_capabilities(tmp_path: Path) -> None:
    member, payload = _file("value", b"payload")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)

    with cas.lease_layer(image, 0) as source, stage_layer(source) as staged:
        reference = staged.changeset.by_path()["value"].payload
        assert isinstance(reference, RegularPayloadRef)
        with pytest.raises(LayerIntakeError, match="cannot be copied"):
            copy(reference)
        with pytest.raises(LayerIntakeError, match="cannot be copied"):
            deepcopy(reference)
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(reference)
        forged = RegularPayloadRef(reference.member_index, reference.size, reference.digest)
        with pytest.raises(LayerIntakeError, match="does not belong"):
            with staged.lease_regular_payload(forged):
                pass
        with staged.lease_regular_payload(reference) as payload_lease:
            with pytest.raises(LayerIntakeError, match="cannot be copied"):
                copy(payload_lease)
            with pytest.raises(TypeError, match="cannot be serialized"):
                pickle.dumps(payload_lease)
            assert b"".join(payload_lease.chunks()) == payload


def test_clean_payload_context_requires_complete_consumption(tmp_path: Path) -> None:
    member, payload = _file("value", b"payload")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)

    with cas.lease_layer(image, 0) as source, stage_layer(source) as staged:
        reference = staged.changeset.by_path()["value"].payload
        with pytest.raises(LayerIntakeError, match="oci-payload-incomplete"):
            with staged.lease_regular_payload(reference) as payload_lease:
                next(payload_lease.chunks(1))


def test_payload_lease_rehashes_bytes_at_verified_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member, payload = _file("value", b"payload")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)

    with cas.lease_layer(image, 0) as source, stage_layer(source) as staged:
        reference = staged.changeset.by_path()["value"].payload
        original_pread = os.pread

        def corrupt_pread(fd: int, size: int, offset: int) -> bytes:
            actual = original_pread(fd, size, offset)
            return bytes((actual[0] ^ 1,)) + actual[1:] if actual else actual

        monkeypatch.setattr(oci_converter.os, "pread", corrupt_pread)
        with pytest.raises(LayerIntakeError, match="oci-payload-digest"):
            with staged.lease_regular_payload(reference) as payload_lease:
                b"".join(payload_lease.chunks())


@pytest.mark.parametrize("fault", ["oserror", "short-read"])
def test_payload_read_fault_releases_its_token_and_allows_a_fresh_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    member, payload = _file("value", b"payload")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)

    with cas.lease_layer(image, 0) as source, stage_layer(source) as staged:
        reference = staged.changeset.by_path()["value"].payload
        original_pread = os.pread

        def faulty_pread(fd: int, size: int, offset: int) -> bytes:
            if fault == "oserror":
                raise OSError("injected payload fault")
            return original_pread(fd, size, offset)[:-1]

        monkeypatch.setattr(oci_converter.os, "pread", faulty_pread)
        with pytest.raises(LayerIntakeError, match="oci-payload-io|oci-payload-short-read"):
            with staged.lease_regular_payload(reference) as payload_lease:
                b"".join(payload_lease.chunks())

        monkeypatch.setattr(oci_converter.os, "pread", original_pread)
        with staged.lease_regular_payload(reference) as retry:
            assert b"".join(retry.chunks()) == payload


def test_outer_stage_close_revokes_an_open_payload_lease(tmp_path: Path) -> None:
    member, payload = _file("value", b"payload")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with cas.lease_layer(image, 0) as source:
        stage_context = stage_layer(source)
        staged = stage_context.__enter__()
        reference = staged.changeset.by_path()["value"].payload
        payload_context = staged.lease_regular_payload(reference)
        payload_lease = payload_context.__enter__()
        stream = payload_lease.chunks(1)
        assert next(stream) == b"p"
        stage_context.__exit__(None, None, None)
        try:
            with pytest.raises(LayerIntakeError, match="oci-stage-closed|oci-payload-revoked") as failure:
                next(stream)
        finally:
            payload_context.__exit__(type(failure.value), failure.value, failure.value.__traceback__)


def test_uncompressed_and_member_limits_fail_at_exact_boundary(tmp_path: Path) -> None:
    first, first_payload = _file("first", b"a")
    second, second_payload = _file("second", b"b")
    uncompressed = _tar(first, second, payloads={"first": first_payload, "second": second_payload})

    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    uncompressed_limit = replace(DEFAULT_LAYER_CONVERSION_LIMITS, max_uncompressed_bytes=len(uncompressed) - 1)
    with pytest.raises(LayerIntakeError, match="oci-uncompressed-limit"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease, limits=uncompressed_limit):
            pass

    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    member_limit = replace(DEFAULT_LAYER_CONVERSION_LIMITS, max_members=1)
    with pytest.raises(LayerIntakeError, match="oci-member-limit"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease, limits=member_limit):
            pass


def test_compression_ratio_and_timeout_are_deterministic(tmp_path: Path) -> None:
    member, payload = _file("zeros", b"\0" * 4096)
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, compressed = _snapshot(tmp_path, uncompressed, OCI_LAYER_GZIP_MEDIA_TYPE)
    ratio = max(1, (len(uncompressed) - 1) // len(compressed))
    limits = replace(DEFAULT_LAYER_CONVERSION_LIMITS, max_compression_ratio=ratio)
    with pytest.raises(LayerIntakeError, match="oci-expansion-limit"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease, limits=limits):
            pass

    ticks = iter((0.0, 0.0, 301.0))
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with pytest.raises(LayerIntakeError, match="oci-layer-timeout"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease, clock=lambda: next(ticks)):
            pass


def test_gzip_decoder_emits_only_bounded_python_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    member, payload = _file("zeros", b"\0" * (3 * 1024**2))
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_GZIP_MEDIA_TYPE)
    original = oci_converter._write_uncompressed
    observed: list[int] = []

    def recording_write(*args, **kwargs):
        observed.append(len(args[1]))
        return original(*args, **kwargs)

    monkeypatch.setattr(oci_converter, "_write_uncompressed", recording_write)
    with cas.lease_layer(image, 0) as lease, stage_layer(lease):
        pass
    assert observed
    assert max(observed) <= 1024 * 1024


@pytest.mark.parametrize("source_chunk_size", [1, 511, 512, 64 * 1024])
def test_gzip_header_body_and_trailer_are_chunk_boundary_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_chunk_size: int,
) -> None:
    payload = b"".join(hashlib.sha256(index.to_bytes(4, "big")).digest() for index in range(4096))
    member, payload = _file("random", payload)
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_GZIP_MEDIA_TYPE)
    original_chunks = LeasedSourceLayer.chunks

    def split_chunks(self, _chunk_size=1024 * 1024):
        yield from original_chunks(self, source_chunk_size)

    monkeypatch.setattr(LeasedSourceLayer, "chunks", split_chunks)
    with cas.lease_layer(image, 0) as lease, stage_layer(lease) as staged:
        assert staged.receipt.diff_id == f"sha256:{hashlib.sha256(uncompressed).hexdigest()}"


def test_concatenated_gzip_is_an_explicit_phase1_unsupported_subset(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    concatenated = gzip.compress(uncompressed, mtime=0) + gzip.compress(b"", mtime=0)
    cas, image, _ = _snapshot(
        tmp_path,
        uncompressed,
        OCI_LAYER_GZIP_MEDIA_TYPE,
        compressed_override=concatenated,
    )
    with pytest.raises(LayerIntakeError, match="oci-gzip-trailing"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass


def test_source_cas_corruption_is_rejected_without_repair(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, compressed = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    target = tmp_path / next(path.name for path in tmp_path.iterdir() if path.name.startswith("cas-"))
    target = target / "blobs" / "sha256" / image.layers[0].descriptor.digest.split(":", 1)[1]
    target.chmod(0o600)
    target.write_bytes(b"x" * len(compressed))
    target.chmod(0o400)

    with pytest.raises(SourceLeaseError, match="descriptor verification"):
        with cas.lease_layer(image, 0) as lease:
            b"".join(lease.chunks())
    assert target.read_bytes() == b"x" * len(compressed)


def test_production_policy_constants_match_pr4_contract() -> None:
    limits = DEFAULT_LAYER_CONVERSION_LIMITS
    assert limits.max_members == 250_000
    assert limits.max_uncompressed_bytes == 32 * 1024**3
    assert limits.max_path_bytes == 4096
    assert limits.max_compression_ratio == 2048
    assert limits.timeout_seconds == 300.0
    assert LAYER_INTAKE_POLICY_ID.endswith(".v1")
    assert limits.fingerprint.startswith("sha256:")
    assert replace(limits, max_members=limits.max_members - 1).fingerprint != limits.fingerprint


def test_body_exception_is_not_masked_by_incomplete_source_lease(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)

    with pytest.raises(RuntimeError, match="consumer failed"):
        with cas.lease_layer(image, 0):
            raise RuntimeError("consumer failed")


def test_late_failure_closes_the_private_spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(
        tmp_path,
        uncompressed,
        OCI_LAYER_MEDIA_TYPE,
        expected_diff_id="sha256:" + "0" * 64,
    )
    original = oci_converter.tempfile.TemporaryFile
    created = []

    def tracking_temporary_file(*args, **kwargs):
        result = original(*args, **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr(oci_converter.tempfile, "TemporaryFile", tracking_temporary_file)
    with pytest.raises(LayerIntakeError, match="oci-diffid"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass
    assert len(created) == 1
    assert created[0].closed


def test_spool_close_failure_does_not_mask_consumer_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    original = oci_converter.tempfile.TemporaryFile

    class CloseFault:
        def __init__(self):
            self._file = original(mode="w+b")

        def __getattr__(self, name):
            return getattr(self._file, name)

        def close(self) -> None:
            self._file.close()
            raise OSError("injected close failure")

    monkeypatch.setattr(oci_converter.tempfile, "TemporaryFile", lambda mode: CloseFault())
    with pytest.raises(RuntimeError, match="consumer failed"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            raise RuntimeError("consumer failed")


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("write", "oci-spool-io"),
        ("short-write", "oci-spool-io"),
        ("flush", "oci-spool-io"),
        ("fsync", "oci-spool-io"),
        ("pread", "oci-tar-io"),
    ],
)
def test_spool_fault_matrix_is_typed_and_closes_every_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    expected: str,
) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    original_temporary = oci_converter.tempfile.TemporaryFile
    created = []

    class FaultSpool:
        def __init__(self):
            self._file = original_temporary(mode="w+b")

        def __getattr__(self, name):
            return getattr(self._file, name)

        def write(self, value: bytes) -> int:
            if fault == "write":
                raise OSError("injected write failure")
            if fault == "short-write":
                return max(0, len(value) - 1)
            return self._file.write(value)

        def flush(self) -> None:
            if fault == "flush":
                raise OSError("injected flush failure")
            self._file.flush()

    def make_spool(mode: str):
        assert mode == "w+b"
        result = FaultSpool()
        created.append(result)
        return result

    monkeypatch.setattr(oci_converter.tempfile, "TemporaryFile", make_spool)
    if fault == "fsync":
        monkeypatch.setattr(oci_converter.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync")))
    if fault == "pread":
        monkeypatch.setattr(
            oci_converter.os,
            "pread",
            lambda _fd, _size, _offset: (_ for _ in ()).throw(OSError("pread")),
        )
    with pytest.raises(LayerIntakeError, match=expected):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass
    assert len(created) == 1
    assert created[0]._file.closed


def test_source_read_interruption_closes_private_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    original_chunks = LeasedSourceLayer.chunks
    original_temporary = oci_converter.tempfile.TemporaryFile
    created = []

    def interrupted_chunks(self, chunk_size=1024 * 1024):
        stream = original_chunks(self, chunk_size)
        yield next(stream)
        raise SourceLeaseError("injected source read failure")

    def tracking_temporary_file(mode: str):
        result = original_temporary(mode=mode)
        created.append(result)
        return result

    monkeypatch.setattr(LeasedSourceLayer, "chunks", interrupted_chunks)
    monkeypatch.setattr(oci_converter.tempfile, "TemporaryFile", tracking_temporary_file)
    with pytest.raises(SourceLeaseError, match="injected source read failure"):
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass
    assert len(created) == 1
    assert created[0].closed


def test_spool_creation_and_staged_read_errors_are_typed_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    original_temporary = oci_converter.tempfile.TemporaryFile
    monkeypatch.setattr(
        oci_converter.tempfile,
        "TemporaryFile",
        lambda mode: (_ for _ in ()).throw(OSError(f"cannot create {tmp_path}/{mode}")),
    )
    with pytest.raises(LayerIntakeError, match="oci-spool-io") as failure:
        with cas.lease_layer(image, 0) as lease, stage_layer(lease):
            pass
    assert os.fspath(tmp_path) not in str(failure.value)

    class ReadFault:
        def __init__(self):
            self._file = original_temporary(mode="w+b")

        def __getattr__(self, name):
            return getattr(self._file, name)

        def read(self, _size: int) -> bytes:
            raise OSError(f"cannot read {tmp_path}")

    monkeypatch.setattr(oci_converter.tempfile, "TemporaryFile", lambda mode: ReadFault())
    with cas.lease_layer(image, 0) as lease, stage_layer(lease) as staged:
        with pytest.raises(LayerIntakeError, match="oci-stage-io") as read_failure:
            next(staged.chunks())
    assert os.fspath(tmp_path) not in str(read_failure.value)


def test_receipts_and_capabilities_do_not_disclose_local_paths(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    cas, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    with cas.lease_layer(image, 0) as lease, stage_layer(lease) as staged:
        rendered = repr(lease) + repr(staged) + repr(staged.receipt)
        assert os.fspath(tmp_path) not in rendered
        assert "fileno" not in rendered
        assert not hasattr(staged, "fileno")


def test_foreign_snapshot_cannot_be_leased_from_another_cas(tmp_path: Path) -> None:
    member, payload = _file("value")
    uncompressed = _tar(member, payloads={member.name: payload})
    _, image, _ = _snapshot(tmp_path, uncompressed, OCI_LAYER_MEDIA_TYPE)
    other = SourceCAS(tmp_path / "foreign-cas")
    with pytest.raises(ArtifactValidationError, match="different source CAS"):
        with other.lease_layer(image, 0):
            pass
