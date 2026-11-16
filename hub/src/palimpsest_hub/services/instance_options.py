"""Read-only provider choices for an instance request, read with the caller's own token.

Nothing here admits a request: `instances.provider_inputs` checks every selected object again
before any allocation. The lists only save a dashboard user from copying identifiers by hand.
"""

from __future__ import annotations

import re
from itertools import islice

from fastapi import HTTPException

from palimpsest_hub.services.instances import request_project, value

MAX_CHOICES = 200


def _bounded(items):
    """At most MAX_CHOICES rows and whether the provider had more."""
    rows = list(islice(items, MAX_CHOICES + 1))
    return rows[:MAX_CHOICES], len(rows) > MAX_CHOICES


def _text(item, default=""):
    return item if isinstance(item, str) else default


def _integer(item):
    return item if type(item) is int and item >= 0 else 0


def _image(image, qualified):
    """A projection when `provider_inputs` could accept the image as a boot base, else None."""
    if str(value(image, "status", "")).lower() != "active":
        return None
    props = {**(value(image, "extra_properties", {}) or {}), **(value(image, "properties", {}) or {})}
    if value(image, "palimpsest_artifact_type", props.get("palimpsest_artifact_type")) == "external-layer-store":
        return None
    identity = _text(value(image, "id"))
    qualification = qualified.get(identity)
    if qualification is not None:
        algorithm = value(image, "hash_algo") or value(image, "os_hash_algo")
        content = value(image, "hash_value") or value(image, "os_hash_value")
        if (algorithm, content) != (qualification["os_hash_algo"], qualification["os_hash_value"]):
            return None  # The operator qualified other bytes; a create request would be refused.
    arch = value(image, "architecture") or props.get("architecture")
    if arch is None and qualification:
        arch = qualification["architecture"]
    os_type = value(image, "os_type") or props.get("os_type")
    if os_type is None and qualification:
        os_type = qualification["os_type"]
    if (
        not identity
        or arch not in ("x86_64", "amd64")
        or os_type not in (None, "linux")
        or value(image, "disk_format") not in ("qcow2", "raw")
        or value(image, "container_format", "bare") != "bare"
    ):
        return None
    base_digest = props.get("palimpsest_digest") or value(image, "palimpsest_digest")
    return {
        "id": identity,
        "name": _text(value(image, "name")),
        "architecture": "x86_64",
        "disk_format": value(image, "disk_format"),
        "size_bytes": _integer(value(image, "size", 0)),
        "min_disk_gib": _integer(value(image, "min_disk", 0)),
        "min_ram_mib": _integer(value(image, "min_ram", 0)),
        "visibility": _text(value(image, "visibility"), "unknown"),
        # Set only on a Glance image that records which Hub cloud image it was made from.
        "base_image_digest": base_digest if isinstance(base_digest, str) else None,
    }


def _flavor(flavor):
    identity = _text(value(flavor, "id"))
    if not identity or value(flavor, "is_disabled") is True:
        return None
    # A PCI/GPU flavor needs an operator-qualified GPU profile, which this list does not offer.
    # A provider that does not return extra specs here leaves that check to the create request.
    extra = value(flavor, "extra_specs", None) or {}
    if extra.get("pci_passthrough:alias") or any(
        re.fullmatch(r"resources[A-Za-z0-9_-]*:.+", key) and "GPU" in key.upper() for key in extra
    ):
        return None
    return {
        "id": identity,
        "name": _text(value(flavor, "name")),
        "vcpus": _integer(value(flavor, "vcpus", 0)),
        "ram_mib": _integer(value(flavor, "ram", 0)),
        "disk_gib": _integer(value(flavor, "disk", 0)),
    }


def choices(conn, actor, settings):
    """Blocking provider reads; call through `run_blocking`."""
    project = request_project(actor, conn)
    try:
        images, more_images = _bounded(conn.image.images(status="active"))
        # Nova's public filter also returns the flavors granted to the caller's project.
        flavors, more_flavors = _bounded(conn.compute.flavors(details=True, is_public=True))
        networks, more_networks = _bounded(conn.network.networks(project_id=project))
        keypairs, more_keypairs = _bounded(conn.compute.keypairs())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Provider choices are unavailable") from None
    qualified = settings.palimpsest_hub_instance_base_images
    return {
        "project_id": project,
        "images": sorted(
            (item for item in (_image(image, qualified) for image in images) if item is not None),
            key=lambda item: (item["name"], item["id"]),
        ),
        "flavors": sorted(
            (item for item in (_flavor(flavor) for flavor in flavors) if item is not None),
            key=lambda item: (item["ram_mib"], item["vcpus"], item["name"], item["id"]),
        ),
        # The provider filter is not trusted: a network of another project is never a choice.
        "networks": sorted(
            (
                {"id": _text(value(network, "id")), "name": _text(value(network, "name"))}
                for network in networks
                if value(network, "project_id") == project and _text(value(network, "id"))
            ),
            key=lambda item: (item["name"], item["id"]),
        ),
        "key_names": sorted({_text(value(keypair, "name")) for keypair in keypairs} - {""}),
        "truncated": {
            "images": more_images,
            "flavors": more_flavors,
            "networks": more_networks,
            "key_names": more_keypairs,
        },
        "limits": {
            "max_boot_gib": settings.palimpsest_hub_instance_max_boot_gib,
            "max_writable_gib": settings.palimpsest_hub_instance_max_writable_gib,
        },
    }
