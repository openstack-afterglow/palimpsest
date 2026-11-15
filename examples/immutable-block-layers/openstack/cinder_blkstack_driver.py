"""Cinder backend sketch: a volume is the writable delta of a block stack.

REFERENCE ONLY. This file was not imported by, or run against, any Cinder. It shows where the
design plugs into unmodified Nova and Cinder 2026.1 and which calls carry which fact; names of
Cinder internals follow cinder/volume/drivers/rbd.py on stable/2026.1 and must be re-checked
against the deployed build before any use.

How it fits together
- A stack is published as a Glance image whose property ``blkstack_stack`` holds the stack
  description (ordered layer digests, virtual size). The image carries no usable payload.
  The property must be a protected Glance property that only the publishing role can set:
  whatever it names is assembled by a root daemon on the compute host.
- ``openstack volume create --image <stack image> --type blkstack --size <virtual size>``
  reaches ``clone_image``: the driver creates an empty, thin RBD image (the delta) and records
  the stack in its image metadata. Nothing is copied. The RBD image is made larger than the
  volume: it holds a filesystem with the sparse upper file, and that file must be able to
  grow to the whole virtual disk.
- ``initialize_connection`` answers with Cinder's existing ``local`` connection type and the
  path ``/dev/blkstack/<volume id>``. Stock Nova turns that into ``<disk type='block'>``
  (nova/virt/libvirt/driver.py VOLUME_DRIVERS['local']); it does not create the device.
- On the compute node the libvirt ``prepare`` hook asks the node agent to assemble that device
  from the delta image's recorded stack before QEMU starts (compute/libvirt-hook-qemu).

Everything that would change the delta behind the node's back is refused for stack volumes:
extend, snapshot, clone, retype, upload to image. Backup and restore are not refused here and
must be disabled for the volume type, because a restore writes tenant-chosen bytes into an
image whose filesystem the compute host mounts. Usage metering reads ``rbd du`` outside
Cinder, because Cinder reports provisioned sizes only.
"""

import json
import re

from cinder import exception
from cinder.volume.drivers import rbd as rbd_driver

STACK_PROPERTY = "blkstack_stack"
STACK_METADATA_KEY = "blkstack.stack"
DEVICE_DIRECTORY = "/dev/blkstack"
GIB = 1024**3
MAX_LAYERS = 255
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def delta_image_bytes(virtual_size_bytes):
    """Same rule as `blkstack node-attach`: room for a full overwrite plus the filesystem's own needs."""
    gib = virtual_size_bytes // GIB
    return (gib + gib // 16 + 1) * GIB


def valid_stack(stack):
    """The same checks `blkstack` applies before it uses a description (validate_stack)."""
    if not isinstance(stack, dict) or stack.get("schema") != "palimpsest.blockstack.v1":
        return False
    size, layers = stack.get("virtualSizeBytes"), stack.get("layers")
    if not isinstance(size, int) or size <= 0 or size % GIB:
        return False
    if not isinstance(layers, list) or not 1 <= len(layers) <= MAX_LAYERS:
        return False
    parent = ""
    for layer in layers:
        if not isinstance(layer, dict):
            return False
        digest, uuid, layer_size = layer.get("digest"), layer.get("uuid"), layer.get("size")
        if not (isinstance(digest, str) and DIGEST.fullmatch(digest)):
            return False
        if not (isinstance(uuid, str) and UUID.fullmatch(uuid)) or layer.get("parentUuid") != parent:
            return False
        if not isinstance(layer_size, int) or layer_size <= 0:
            return False
        parent = uuid
    return True


def stack_from_image(image_meta):
    """The stack description carried by a Glance image, or None for an ordinary image."""
    raw = (image_meta.get("properties") or {}).get(STACK_PROPERTY)
    if not raw:
        return None
    try:
        stack = json.loads(raw)
    except ValueError:
        stack = None
    if not valid_stack(stack):
        raise exception.ImageUnacceptable(image_id=image_meta.get("id"), reason="invalid block stack description")
    return stack


class BlockStackDriver(rbd_driver.RBDDriver):
    """RBD driver whose image-backed volumes are block-stack deltas."""

    def clone_image(self, context, volume, image_location, image_meta, image_service):
        stack = stack_from_image(image_meta)
        if stack is None:
            return super().clone_image(context, volume, image_location, image_meta, image_service)
        if int(volume.size) * GIB != stack["virtualSizeBytes"]:
            # The guest sees the stack's virtual size whatever the volume says, so they must agree.
            raise exception.ImageUnacceptable(
                image_id=image_meta.get("id"), reason="volume size must equal the stack's virtual size"
            )
        if volume.multiattach:
            raise exception.ImageUnacceptable(image_id=image_meta.get("id"), reason="multi-attach is not supported")
        update = self.create_volume(volume)  # an empty thin image named volume-<id>
        with rbd_driver.RBDVolumeProxy(self, volume.name) as image:
            image.resize(delta_image_bytes(stack["virtualSizeBytes"]))
            # The node refuses a delta without the exclusive-lock feature (object-map and
            # fast-diff make `rbd du` cheap). Which features create_volume set depends on the
            # cluster's defaults; enabling missing ones is not written here.
            image.metadata_set(STACK_METADATA_KEY, json.dumps(stack, sort_keys=True, separators=(",", ":")))
        return update, True

    def copy_image_to_volume(self, context, volume, image_service, image_id, disable_sparse=False):
        # Reached when clone_image was not used. A stack image has no payload, so copying it
        # would produce a volume that boots nothing. Other backends do not run this code at all:
        # the stack image must also carry cinder_img_volume_type=<the blkstack type>.
        if stack_from_image(image_service.show(context, image_id)) is not None:
            raise exception.ImageUnacceptable(
                image_id=image_id, reason="a block stack image needs the blkstack volume type"
            )
        return super().copy_image_to_volume(context, volume, image_service, image_id, disable_sparse)

    def copy_image_to_encrypted_volume(self, context, volume, image_service, image_id):
        # Cinder's create flow uses this entry point, not copy_image_to_volume, for encrypted types.
        if stack_from_image(image_service.show(context, image_id)) is not None:
            raise exception.ImageUnacceptable(image_id=image_id, reason="block stack images cannot be encrypted")
        return super().copy_image_to_encrypted_volume(context, volume, image_service, image_id)

    def _is_stack_volume(self, volume):
        with rbd_driver.RBDVolumeProxy(self, volume.name, read_only=True) as image:
            try:
                return bool(image.metadata_get(STACK_METADATA_KEY))
            except KeyError:
                return False

    def initialize_connection(self, volume, connector):
        if not self._is_stack_volume(volume):
            return super().initialize_connection(volume, connector)
        return {
            "driver_volume_type": "local",
            "data": {"device_path": f"{DEVICE_DIRECTORY}/{volume.id}", "volume_id": volume.id, "discard": True},
        }

    def _refuse_for_stack(self, volume, operation):
        if self._is_stack_volume(volume):
            raise exception.VolumeDriverException(message=f"{operation} is not supported for block-stack volumes")

    def extend_volume(self, volume, new_size):
        self._refuse_for_stack(volume, "extend")  # the guest's disk size is fixed by the stack
        return super().extend_volume(volume, new_size)

    def create_snapshot(self, snapshot):
        self._refuse_for_stack(snapshot.volume, "snapshot")
        return super().create_snapshot(snapshot)

    def create_cloned_volume(self, volume, src_vref):
        self._refuse_for_stack(src_vref, "clone")
        return super().create_cloned_volume(volume, src_vref)

    def retype(self, context, volume, new_type, diff, host):
        self._refuse_for_stack(volume, "retype")
        return super().retype(context, volume, new_type, diff, host)

    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        self._refuse_for_stack(volume, "upload to image")
        return super().copy_volume_to_image(context, volume, image_service, image_meta)
