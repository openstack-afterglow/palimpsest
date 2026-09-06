"""Local run intake keeps cloud routing and authenticated image policy distinct."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_oci_source as source_tests

from palimpsest_local import oci_materializer
from palimpsest_local import oci_run_request as intake
from palimpsest_local.errors import ArtifactValidationError, PalimpsestError, UnsupportedPlatformError
from palimpsest_local.oci_process import OCIProcessSpec
from palimpsest_local.oci_provenance import OCI_IMAGE_CONFIG_MEDIA_TYPE, OCI_IMAGE_MANIFEST_MEDIA_TYPE
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind
from palimpsest_local.state import StatePaths, init_resolved_roots


def _layout(root, *, bootable=True):
    layout, _, _, layers = source_tests._direct_layout(root, repeated=True)
    config = layout.add(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [f"sha256:{index + 1:064x}" for index in range(len(layers))]},
            "config": {
                "Entrypoint": ["/bin/demo", "$(literal)"] if bootable else [],
                "Cmd": ["arg with spaces"] if bootable else [],
                "Env": ["PATH=/bin", "MESSAGE=$HOME;literal"],
                "WorkingDir": "/work",
                "User": "1000:1001",
                "StopSignal": "TERM",
            },
        },
        OCI_IMAGE_CONFIG_MEDIA_TYPE,
    )
    manifest = layout.add(
        {
            "schemaVersion": 2,
            "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            "config": config.to_dict(),
            "layers": [layer.to_dict() for layer in layers],
        },
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    layout.top(manifest)
    return layout, manifest


def _roots(tmp_path):
    return init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))


def test_request_defaults_are_foreground_local_linux_kvm_without_cloud_spec(tmp_path):
    request = intake.LocalOCIRunRequest("demo", tmp_path / "source")
    assert request.dispatch_key == DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)
    assert not request.detached
    assert request.network is None
    assert request.root_size_bytes == 4 * 1024**3
    assert (request.memory_mib, request.vcpus) == (512, 1)
    assert not hasattr(request, "spec")
    assert str(tmp_path) not in repr(request)
    with pytest.raises(FrozenInstanceError):
        request.detached = True


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Bad Name"},
        {"name": "a" * 64},
        {"name": 5},
        {"source": Path("relative")},
        {"source": Path("/tmp/../bad")},
        {"source": "/tmp/image"},
        {"manifest_digest": "a" * 64},
        {"manifest_digest": "sha256:" + "A" * 64},
        {"manifest_digest": 4},
        {"detached": 1},
        {"memory_mib": True},
        {"memory_mib": 255},
        {"memory_mib": 1_048_577},
        {"vcpus": False},
        {"vcpus": 0},
        {"vcpus": 257},
        {"network": "default"},
        {"network": "none"},
        {"platform": "linux/arm64"},
        {"platform": "windows/amd64"},
        {"backend": "lima-vz"},
        {"root_size_bytes": True},
        {"root_size_bytes": 1024},
        {"root_size_bytes": 64 * 1024**2 + 1},
    ],
)
def test_request_rejects_unsupported_or_mutable_policy(tmp_path, changes):
    values = {"name": "demo", "source": tmp_path / "source", **changes}
    with pytest.raises(PalimpsestError):
        intake.LocalOCIRunRequest(**values)


def test_resolver_canonicalizes_existing_symlink_without_selecting_image(tmp_path):
    source = tmp_path / "image.tar"
    source.touch()
    link = tmp_path / "current"
    link.symlink_to(source)
    request = intake.resolve_local_oci_run_request(link, name="demo", detached=True)
    assert request.source == source.resolve()
    assert request.detached
    assert request.manifest_digest is None


def test_resolver_refuses_missing_or_non_path_source(tmp_path):
    for source in (tmp_path / "missing", "ubuntu:latest"):
        with pytest.raises(ArtifactValidationError):
            intake.resolve_local_oci_run_request(source, name="demo")


@pytest.mark.parametrize("archive", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_intake_snapshots_local_input_and_preserves_process_and_layer_occurrences(
    tmp_path, monkeypatch, archive, explicit
):
    layout, manifest = _layout(tmp_path / "layout")
    path = layout.root
    if archive:
        path = tmp_path / "image.tar"
        source_tests._write_oci_archive(layout.root, path)
    request = intake.resolve_local_oci_run_request(
        path, name="demo", manifest_digest=manifest.digest if explicit else None
    )
    roots = _roots(tmp_path)
    calls = []
    monkeypatch.setattr(intake, "sys", SimpleNamespace(platform="linux"))

    def convert(image, ordinal, **kwargs):
        calls.append((image, ordinal, kwargs))
        return source_tests._derived_result(image, ordinal)

    monkeypatch.setattr(oci_materializer, "materialize_layer_hard", convert)
    toolchain = object()
    prepared = intake.materialize_local_oci_run(
        request, roots=roots, packer_path=Path("/usr/bin/mksquashfs"), toolchain=toolchain, timeout_seconds=7
    )
    assert prepared.request is request
    assert prepared.receipt.root_descriptor == manifest
    assert [item[1] for item in calls] == [0, 1]
    assert prepared.receipt.layer_descriptors[0] == prepared.receipt.layer_descriptors[1]
    assert all(call[2]["toolchain"] is toolchain and 0 < call[2]["timeout_seconds"] <= 7 for call in calls)
    process = prepared.receipt.process
    assert process.argv == ("/bin/demo", "$(literal)", "arg with spaces")
    assert process.environment == (("PATH", "/bin"), ("MESSAGE", "$HOME;literal"))
    assert process.cwd == "/work"
    assert (process.user.user, process.user.group, process.stop_signal) == ("1000", "1001", 15)
    assert list(roots.runs.iterdir()) == []
    with pytest.raises(ArtifactValidationError, match="root pin"):
        intake.PreparedLocalOCIRun(replace(request, manifest_digest="sha256:" + "a" * 64), prepared.receipt)
    with pytest.raises(ArtifactValidationError, match="no Entrypoint"):
        intake.PreparedLocalOCIRun(request, replace(prepared.receipt, process=OCIProcessSpec.empty()))


@pytest.mark.parametrize("failure", ["ambiguous", "unbootable", "mismatch"])
def test_intake_rejects_bad_selection_or_process_before_conversion(tmp_path, monkeypatch, failure):
    layout, manifest = _layout(tmp_path / "layout", bootable=failure != "unbootable")
    if failure == "ambiguous":
        layout.top(manifest, duplicates=2)
    request = intake.resolve_local_oci_run_request(
        layout.root, name="demo", manifest_digest="sha256:" + "f" * 64 if failure == "mismatch" else None
    )
    monkeypatch.setattr(intake, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(intake, "materialize_image_hard", lambda *args, **kwargs: pytest.fail("converter entered"))
    with pytest.raises(PalimpsestError):
        intake.materialize_local_oci_run(
            request, roots=_roots(tmp_path), packer_path=Path("/packer"), toolchain=object()
        )


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), "3"])
def test_invalid_deadline_precedes_snapshot_mutation(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(intake, "sys", SimpleNamespace(platform="linux"))
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    with pytest.raises(ArtifactValidationError, match="timeout"):
        intake.materialize_local_oci_run(
            intake.LocalOCIRunRequest("demo", tmp_path / "missing"),
            roots=roots,
            packer_path=Path("/packer"),
            toolchain=object(),
            timeout_seconds=timeout,
        )
    assert not roots.state.exists()


def test_unsupported_host_precedes_any_source_or_state_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "sys", SimpleNamespace(platform="darwin"))
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    with pytest.raises(UnsupportedPlatformError, match="Linux"):
        intake.materialize_local_oci_run(
            intake.LocalOCIRunRequest("demo", tmp_path / "missing"),
            roots=roots,
            packer_path=Path("/packer"),
            toolchain=object(),
        )
    assert not roots.state.exists()
