from __future__ import annotations

import struct

import pytest

from palimpsest_local import vz_runtime_assets as assets
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.vz_runtime_assets import _image, _soft_dependencies

_RELEASE = "6.8.0-147-generic"


def _kernel_image(*banners: bytes) -> bytes:
    payload = bytearray(4096)
    struct.pack_into(
        "<IIQQQQQQII",
        payload,
        0,
        0x14000010,
        0,
        0x80000,
        len(payload),
        0,
        0,
        0,
        0,
        0x644D5241,
        0,
    )
    strings = b"\0".join(banners) + b"\0"
    payload[64 : 64 + len(strings)] = strings
    return bytes(payload)


def test_original_ubuntu_weak_and_final_banners_bind_the_same_release() -> None:
    weak = b"Linux version 6.8.0-147-generic (Ubuntu gcc) #147-Ubuntu SMP PREEMPT_DYNAMIC\n"
    final = (
        b"Linux version 6.8.0-147-generic (Ubuntu gcc) #147-Ubuntu SMP PREEMPT_DYNAMIC Thu Sep 24 11:39:52 UTC 2026\n"
    )
    image = _kernel_image(weak, final, b"Linux version %s (%s)\n")

    assert _image(image, _RELEASE).split(maxsplit=1)[0] == _RELEASE


@pytest.mark.parametrize(
    "conflicting",
    [
        b"Linux version 6.8.0-148-generic (Ubuntu gcc) #148-Ubuntu\n",
        b"Linux version 6.8.0-147-generic-extra (Ubuntu gcc) #147-Ubuntu\n",
        b"Linux version 6.8.0-147-generic (unqualified vendor)\n",
    ],
)
def test_matching_banner_does_not_hide_a_conflicting_kernel_identity(conflicting: bytes) -> None:
    matching = b"Linux version 6.8.0-147-generic (Ubuntu gcc) #147-Ubuntu\n"

    with pytest.raises(ArtifactValidationError):
        _image(_kernel_image(matching, conflicting), _RELEASE)


def test_unphased_depmod_tokens_are_ignored_without_losing_pre_post_order() -> None:
    dependencies = _soft_dependencies(
        [
            "softdep cifs gcm",
            "softdep cifs nls",
            "softdep vmw-vsock-virtio-transport ignored pre: vsock post: inet-diag",
            "softdep vmw-vsock-virtio-transport pre: virtio-ring",
        ]
    )

    assert dependencies == {
        "cifs": {"pre:": [], "post:": []},
        "vmw_vsock_virtio_transport": {
            "pre:": ["vsock", "virtio_ring"],
            "post:": ["inet_diag"],
        },
    }


def test_phased_soft_dependency_cannot_escape_the_module_namespace() -> None:
    with pytest.raises(ArtifactValidationError):
        _soft_dependencies(["softdep virtiofs pre: ../foreign"])


def _static_executable(markers: bytes, *, size: int = 4096) -> bytes:
    payload = bytearray(size)
    struct.pack_into(
        "<16sHHIQQQIHHHHHH",
        payload,
        0,
        b"\x7fELF\x02\x01\x01" + b"\0" * 9,
        2,
        183,
        1,
        0x400080,
        64,
        0,
        0,
        64,
        56,
        1,
        0,
        0,
        0,
    )
    struct.pack_into("<IIQQQQQQ", payload, 64, 1, 5, 0, 0x400000, 0x400000, size, size, 4096)
    payload[128 : 128 + len(markers)] = markers
    return bytes(payload)


def _component_payloads(dropbear_size: int = 4096) -> dict[str, bytes]:
    applets = (
        "modprobe udhcpc ip sh cat uname env printf test true false id readlink mkdir chmod "
        "sleep date touch kill ps wc cut tr sort tee rm ln mv cp stat find timeout "
        "grep sed awk head tail ls"
    ).split()
    return {
        "platform": _static_executable(assets.PLATFORM_ABI.encode()),
        "busybox": _static_executable(
            b"BusyBox v1.36.1\0" + b"\0\0".join(applet.encode() for applet in applets) + b"\0"
        ),
        "dropbear": _static_executable(b"dropbear\x00dropbearkey\x002026.94\x00", size=dropbear_size),
        "guestagent": _static_executable(b"github.com/lima-vm/lima\x002.2.1\x00"),
    }


def test_same_version_and_exact_size_cannot_qualify_an_unknown_dropbear_build() -> None:
    payload = _component_payloads(506576)["dropbear"]

    with pytest.raises(ArtifactValidationError):
        assets._executable(payload, "dropbear", "2.2.1")


@pytest.mark.parametrize("role", ["platform", "busybox", "guestagent"])
def test_non_dropbear_structurally_valid_executables_remain_accepted(role: str) -> None:
    receipt = assets._executable(_component_payloads()[role], role, "2.2.1")

    assert receipt["machine"] == 183
    assert receipt["linkage"] == "static"


@pytest.mark.parametrize("role", ["platform", "busybox", "guestagent"])
def test_non_dropbear_version_identity_remains_required(role: str) -> None:
    with pytest.raises(ArtifactValidationError):
        assets._executable(_static_executable(b"unselected version"), role, "2.2.1")


def test_busybox_required_applets_remain_required() -> None:
    with pytest.raises(ArtifactValidationError):
        assets._executable(_static_executable(b"BusyBox v1.36.1"), "busybox", "2.2.1")
