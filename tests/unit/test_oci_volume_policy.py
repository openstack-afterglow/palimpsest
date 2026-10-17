"""OCI consumers refuse unsupported disks before reservation or native mutation."""

from dataclasses import replace

import pytest

from palimpsest_local import experimental_oci_vm as carrier
from palimpsest_local import oci_run_adapter as native
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_run_request import LocalOCIRunRequest, resolve_local_oci_run_request
from palimpsest_local.runtime_types import RuntimeBackend, RuntimeKind, RunVolumeIntent
from palimpsest_local.state import StatePaths


def _request(tmp_path, *, experimental=False):
    return LocalOCIRunRequest(
        name="volume-policy",
        source=tmp_path / "image",
        backend="lima-qemu-oci" if experimental else "kvm",
        force_architecture=experimental,
    )


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("experimental", [False, True])
def test_request_refuses_unsupported_disk_instead_of_dropping_option(tmp_path, experimental, read_only):
    request = _request(tmp_path, experimental=experimental)
    match = "carrier container disks are not attached" if experimental else "native OCI-root is unsupported"
    with pytest.raises(ArtifactValidationError, match=match):
        replace(request, volume_intents=(RunVolumeIntent("data", "/data", read_only=read_only),))


@pytest.mark.parametrize("experimental", [False, True])
def test_resolver_threads_volume_intent_to_rejection(tmp_path, monkeypatch, experimental):
    source = tmp_path / "image"
    source.mkdir()
    if experimental:
        monkeypatch.setattr(carrier.platforms, "select_backend", lambda *args, **kwargs: "lima-qemu-oci")
    with pytest.raises(ArtifactValidationError, match="data volume attachment"):
        resolve_local_oci_run_request(
            source,
            name="volume-policy",
            backend="lima-qemu-oci" if experimental else "kvm",
            force_architecture=experimental,
            volume_intents=(RunVolumeIntent("data", "/data"),),
        )
    assert list(source.iterdir()) == []


@pytest.mark.parametrize("read_only", [False, True])
def test_native_adapter_revalidates_before_preflight_or_state_creation(tmp_path, monkeypatch, read_only):
    request = _request(tmp_path)
    # Exercise the consumer admission fence independently of constructor checks.
    object.__setattr__(request, "volume_intents", (RunVolumeIntent("data", "/data", read_only=read_only),))
    roots = StatePaths(tmp_path / "config", tmp_path / "state")
    sentinel = tmp_path / "existing-ext4.raw"
    sentinel.write_bytes(b"existing data must not be formatted or adopted")

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported OCI attachment reached host preflight")

    monkeypatch.setattr(native, "preflight_oci_host", forbidden)
    with pytest.raises(ArtifactValidationError, match="native OCI-root is unsupported"):
        native.run_local_oci(roots, request)
    assert not roots.state.exists()
    assert not roots.config.exists()
    assert sentinel.read_bytes() == b"existing data must not be formatted or adopted"


@pytest.mark.parametrize("read_only", [False, True])
def test_carrier_consumer_revalidates_before_backend_selection(tmp_path, monkeypatch, read_only):
    request = _request(tmp_path, experimental=True)
    object.__setattr__(request, "volume_intents", (RunVolumeIntent("data", "/data", read_only=read_only),))
    roots = StatePaths(tmp_path / "config", tmp_path / "state")

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported OCI attachment reached backend selection")

    monkeypatch.setattr(carrier.platforms, "select_backend", forbidden)
    with pytest.raises(ArtifactValidationError, match="carrier container disks are not attached"):
        carrier.run(roots, request)
    assert not roots.state.exists()
    assert not roots.config.exists()


@pytest.mark.parametrize("invalid", [[], (object(),), None])
def test_oci_request_requires_immutable_typed_volume_intents(tmp_path, invalid):
    with pytest.raises(ArtifactValidationError, match="immutable typed volume intents"):
        replace(_request(tmp_path), volume_intents=invalid)


def test_zero_volume_native_resolution_keeps_existing_dispatch_and_root_policy(tmp_path):
    source = tmp_path / "image"
    source.mkdir()
    request = resolve_local_oci_run_request(source, name="volume-policy")
    assert request.volume_intents == ()
    assert request.dispatch_key.runtime_kind is RuntimeKind.OCI_ROOT
    assert request.dispatch_key.backend is RuntimeBackend.KVM
    assert request.root_retention == "delete"
    assert request.root_volume_id is None


def test_zero_volume_carrier_request_retains_explicit_experimental_dispatch(tmp_path):
    request = _request(tmp_path, experimental=True)
    assert request.volume_intents == ()
    assert request.dispatch_key.backend == "lima-qemu-oci"
    assert request.force_architecture is True
