# Shared read-only base and layers with a per-user writable volume on OpenStack

Investigation and design options, 2026-11-15. Nothing here is implemented, deployed or measured.
No cloud resource was created, changed or deleted for this document. An independent fact-check against the
cited sources was applied (33 findings); it is not an Astra/Sol signoff.

## Question

In the cloud, can the base OS be shared read-only, the library and command layers be shared read-only,
and only the writable layer be a volume of the user, so that the same layer is stored once however many
instances use it — and can that be composed from Nova and Cinder?

Short answer: OpenStack documents the building blocks, and some settings observed on this cloud are
consistent with the copy-on-write clone path. Whether the Hub's own boot volumes are clones is not
established, and sharing layers through Cinder has not been run end to end here. It is a candidate to test,
not a result. Section 9 lists what must be decided first.

Evidence tags: **[SRC]** repository source at the branch that carries this document (based on dev `7557727`);
**[OPS]** read-only observation of the deployment on 2026-11-15 (configuration keys from an allowlist, package
versions, one aggregate count; no API call, no secret read); **[DOC]** upstream documentation, with the
release read; **[UPSTREAM-SRC]** upstream source on the `stable/2026.1` branch, which is neither documentation
nor observed behaviour and may differ from the deployed builds; **[INFERENCE]**; **[UNVERIFIED]** not observed
on this cloud.

## 1. What the Hub cloud path does today

| Part | Where the bytes are | Shared between instances |
| --- | --- | --- |
| Base OS | A Cinder boot volume created from the selected Glance image, then Nova boots from that volume (`delete_on_termination: True`). [SRC `hub/src/palimpsest_hub/services/instances.py:971-977,1139-1156`] | At the storage level only, and only if the volume is a copy-on-write clone (sections 2 and 3). |
| Layers | The guest downloads every SquashFS layer over HTTPS from the Hub into its own boot volume, checks size and SHA-256, and loop-mounts it read-only. [SRC `hub/src/palimpsest_hub/services/instance_bootstrap.py:304-337`] | No. One copy per instance. |
| Writable layer | `writable.ext4`, a file on the boot volume reserved with `posix_fallocate` and loop-mounted; overlay upper and work directories live in it. [SRC `instance_bootstrap.py:104,341-389`] | No, and it is deleted with the instance. There is no user-volume concept. |

In application mode the bottom overlay lower is the instance's own boot root, bind-mounted read-only.
[SRC `instance_bootstrap.py:378-386`] That root is a live, writable filesystem of that instance, not an
immutable input. Section 6 comes back to this.

The Hub accepts a `raw` or a `qcow2` base image in container format `bare`. [SRC `instances.py:285-286`]
An instance row records one `boot_volume_id` and has no list of volumes.
[SRC `hub/src/palimpsest_hub/instance_models.py:28`] Quota preflight asks for one volume of the boot size.
[SRC `instances.py:537-541,873-879`, `hub/src/palimpsest_hub/services/preflight.py:33-35`]

Two other paths already avoid per-instance layer copies, with Manila CephFS, not Cinder:

- The native-root path mounts a CephFS share, proves that the storage refuses writes, remounts read-only,
  verifies each layer file by digest and size and loop-mounts it.
  [SRC `src/palimpsest_local/nova_ceph_boot.py:1196-1285`] `palimpsest cloud env` mounts a share whose access
  rule is `ro`, always checks sizes and checks SHA-256 unless `--no-verify` is given.
  [SRC `src/palimpsest_local/cloud_env.py:962-967,1419,1630-1638,1780`] The stated target architecture
  (owner correction of 2026-10-29) is: layer payloads on CephFS exposed through Manila, metadata in the Hub
  database, consumers mount read-only. [SRC `docs/benchmarks.md:596-603`]
- A read-only multi-attach Cinder layer store exists as a manifest and guest runtime for the direct Nova job
  path; provisioning it is external. [SRC `src/palimpsest_local/external_layers.py:58-98`,
  `src/palimpsest_local/nova_build.py:471-508`, `docs/vm-workflow.md:90-111`] The target statement says the
  scenario integration "does not add" the older writable-Cinder-store, seal, read-only re-attach prototype.
  [SRC `docs/benchmarks.md:602-603`]

## 2. What this cloud provides

Read-only observation, 2026-11-15. [OPS]

| Fact | Value |
| --- | --- |
| Release | Kolla images `2026.1-ubuntu-noble`: Cinder 28.0.1, Nova 33.0.3, Manila 22.0.2; libvirt 10.0.0, QEMU 8.2.2; Ceph client 20.2.0 in the Cinder container |
| Cinder | one backend, `cinder.volume.drivers.rbd.RBDDriver`, pool `volumes`, default type `ceph_hdd` |
| Glance | `enabled_backends = http, cinder, rbd`, default `rbd`, pool `images`, `show_image_direct_url = True` |
| Nova compute | `images_type = rbd`, pool `vms`; `max_disk_devices_to_attach` not set; `file_backed_memory` not set |
| Manila | CephFS native and CephFS-through-NFS backends, `driver_handles_share_servers = false` |
| Existing volumes | of 190 RBD images inspected in pool `volumes`, 105 have a parent in pool `images`, 1 has a parent in `volumes`, 84 have none |

The last row is an aggregate count from `rbd info` and names no volume. It shows that the image-clone path
was taken for 105 of 190 images. It does not show when or why, which prerequisites held, or that Hub-created
boot volumes are among them; the 84 without a parent may be blank, converted from `qcow2`, encrypted,
flattened or created from another project's image.

Not inspected: volume types and whether one has `multiattach` or encryption; effective Cinder and Glance
policies (`get_image_location`, new policy defaults); Cinder's `[glance]` service credentials for the new
location API; `image_volume_cache_enabled`, `rbd_max_clone_depth`, `rbd_flatten_volume_from_snapshot`,
deferred deletion; the Ceph cluster release and `rbd_default_clone_format`; which Glance store holds the
Hub's images and their formats; Ceph capabilities of the service users; image properties such as
`hw_disk_bus`; project quotas.

## 3. What OpenStack and Ceph state

**Copy-on-write clone of a Glance image into a Cinder volume.** Ceph's OpenStack guide (Squid edition read;
the cluster release was not observed): "you can use Cinder to boot a VM using a copy-on-write clone of an
image", and "please use the `raw` image format within Glance".
[DOC https://docs.ceph.com/en/squid/rbd/rbd-openstack/] In the RBD driver a clone needs all of: a volume that
is not encrypted, an image location Cinder can obtain (the service-to-service location API introduced in
2025.2, or the legacy `direct_url`), an `rbd://` location in the same Ceph cluster, format `raw`, and a
readable source snapshot. [UPSTREAM-SRC `cinder/volume/drivers/rbd.py`, `cinder/volume/flows/manager/create_volume.py`,
`cinder/image/glance.py`; DOC https://docs.openstack.org/cinder/2026.1/admin/volume-backed-image.html]
Otherwise the image is downloaded and converted into an unshared volume, unless the image-volume cache is
enabled, in which case later volumes clone the cached entry.
[DOC https://docs.openstack.org/cinder/2026.1/admin/image-volume-cache.html]

**Clone lifetime.** `rbd_max_clone_depth` (default 5) applies to volumes cloned from volumes, not to a
volume cloned straight from an image, which is one level deep. `rbd_flatten_volume_from_snapshot` (default
False) flattens volumes made from snapshots; a flattened volume is a full copy.
[DOC https://docs.openstack.org/cinder/2026.1/configuration/block-storage/drivers/ceph-rbd-volume-driver.html;
UPSTREAM-SRC `rbd.py`] The Glance RBD store refuses to delete an image whose snapshot still has clones.
[UPSTREAM-SRC `glance_store/_drivers/rbd.py`]

**Several existing volumes at server create.** Block device mapping v2 accepts `source_type=volume`,
`destination_type=volume` entries with a negative or absent `boot_index` as data disks.
[DOC https://docs.openstack.org/nova/2026.1/user/block-device-mapping.html] Device names and order in the
guest are not guaranteed. Nova sets the disk serial to the volume ID [UPSTREAM-SRC `nova/virt/block_device.py`,
`nova/virt/libvirt/volume/volume.py`]; a virtio-blk serial is at most 20 bytes (Linux
`include/uapi/linux/virtio_blk.h`, `VIRTIO_BLK_ID_BYTES`), so a guest would see a 20-character prefix of the
volume ID [INFERENCE]. A device `tag` is accepted at microversion 2.32 and from 2.42 on and is exposed
through the metadata service. [DOC https://docs.openstack.org/api-ref/compute/ (unversioned)]
`[compute]max_disk_devices_to_attach` defaults to -1, the real limit depends on the disk bus, and the limit
"is enforced during server create, rebuild, evacuate, unshelve, live migrate, and attach volume" while a cold
migration does not check the destination. No numeric virtio limit is documented.
[DOC https://docs.openstack.org/nova/2026.1/configuration/config.html]

**Read-only volumes.** A volume can be marked read-only (`openstack volume set --read-only`,
`os-update_readonly_flag`). Reading the source: the flag can be changed only while the volume is `available`;
a new attachment takes mode `ro` from it unless an explicit attach mode is present; Nova then emits a libvirt
`<readonly/>` disk. [INFERENCE from UPSTREAM-SRC `cinder/volume/api.py`, `cinder/volume/manager.py`,
`nova/virt/libvirt/volume/volume.py`; untested on 28.0.1 / 33.0.3] Three limits: the source marks this route
deprecated in favour of a per-attachment mode that Nova's attach API does not expose; the flag is enforced by
the hypervisor and does not change Ceph authorization [INFERENCE]; and the default policy lets any member of
the owning project clear the flag while the volume is `available` [UPSTREAM-SRC
`cinder/policies/volume_actions.py`], so it guards against accidents and is not a control the user cannot undo.

**Multi-attach.** RBD supports it; it needs a volume type with `multiattach="<is> True"` (creating a type is
admin-only by default). "By default, secondary volume attachments are made in read/write mode", encryption
is not supported, and "it is the responsibility of the user to ensure that a multiattach or clustered file
system is used". [DOC https://docs.openstack.org/cinder/2026.1/admin/volume-multiattach.html] The driver
turns off `exclusive-lock`, `object-map`, `fast-diff` and `journaling` on a multi-attach image and rejects
replication with it. [UPSTREAM-SRC `rbd.py`] No documented recipe gives a read-only multi-attach through
Nova; whether the read-only flag makes every attachment read-only is [UNVERIFIED].

**Across projects.** Cinder's default attachment policy is project-scoped and volume transfer hands ownership
over; neither shares a volume. [DOC https://docs.openstack.org/cinder/2026.1/configuration/block-storage/policy.html]
Glance images can be read across projects (`public`, `community`, `shared` with members); making an image
public is admin-only by default. [DOC https://docs.openstack.org/api-ref/image/v2/ (unversioned)] So a Glance
image is a documented carrier for block data across projects. Whether a volume created from another
project's image is a clone or a copy is [UNVERIFIED]: with the stable/2026.1 default Glance policy a non-owner
may receive no image location, and the clone would then depend on Cinder's service-to-service location
setup, which was not inspected. [INFERENCE from UPSTREAM-SRC `glance/policies/image.py`]

**Manila through Nova (virtiofs).** Available since 2025.1 (compute API 2.97). It needs hugepages or
file-backed memory, a share can be attached only while the instance is `SHUTOFF` and not at create, and
evacuate, live migration, rebuild, resize, resume, shelve and volume snapshot are blocked while a share is
attached. [DOC https://docs.openstack.org/nova/2026.1/admin/manage-shares.html] The implemented 2025.1
specification says the host mounts the share read-write and "Read-only will not be supported".
[DOC https://specs.openstack.org/openstack/nova-specs/specs/2025.1/implemented/libvirt-virtiofs-attach-manila-shares.html]

**Manila CephFS native.** Access is by cephx identity with `ro` or `rw`, enforced by Ceph, with each share in
its own RADOS namespace; the guest needs a Ceph client and network connectivity to the Ceph public network.
[DOC https://docs.openstack.org/manila/2026.1/configuration/shared-file-systems/drivers/cephfs_driver.html]

**Guest side.** overlayfs: "Multiple lower layers can now be given using the colon", and "changes to the
underlying filesystems while part of a mounted overlay filesystem are not allowed".
[DOC https://docs.kernel.org/6.12/filesystems/overlayfs.html] Linux 6.12 source caps a mount at 500 lowers
(`fs/overlayfs/params.h`, `OVL_MAX_STACK`); the current Hub guest also refuses a `lowerdir` option of 4096
bytes or more. [SRC `instance_bootstrap.py:386-388`] SquashFS is a read-only filesystem on a block device.
dm-verity verifies a block device on demand against a trusted root digest.
[DOC https://docs.kernel.org/6.12/admin-guide/device-mapper/verity.html]

## 4. Options

### Base OS

The boot volume is a clone only when every prerequisite in section 3 holds (`raw` is necessary, not
sufficient); that is [UNVERIFIED] for the Hub's images. Where it is a clone, it stores only the blocks the
instance changes. Today the instance writes its downloaded layers into that volume and reserves
`writable.ext4` there, so the clone diverges by at least the layer bytes and whatever the instance writes.
[INFERENCE] With layers and the writable layer elsewhere, the boot volume would stay close to the image.

### Layers

| Option | Copies of a layer | Across projects | Read-only enforced by | Per instance | State in this repository |
| --- | --- | --- | --- | --- | --- |
| L0 HTTPS download | one per instance | n/a | guest mount option | nothing extra | the standard Hub path |
| L1 Manila CephFS, guest mounts | one per share namespace | the share owner grants an `ro` rule and hands out the key [INFERENCE] | Ceph capabilities (storage) | a cephx identity, network path to Ceph, Ceph client in the guest | implemented for native-root and `cloud env`; the stated target |
| L2 Cinder clone per layer | one in Glance, plus the Hub's own copy | [UNVERIFIED] (section 3) | expected from source to be the hypervisor [UNVERIFIED]; a project member can clear the flag; a write could only reach the instance's own clone | one volume per layer, each at least 1 GiB of quota | not implemented |
| L3 Cinder multi-attach per project | one per project | no | hypervisor, not selectable per attachment through Nova [UNVERIFIED] | none, but a shared volume to reference-count | manifest and guest runtime exist for the direct Nova path; provisioning external |
| L4 Manila through Nova virtiofs | one per share | unknown | none at the host (mounted read-write) | shared-memory flavor, attach while stopped | not implemented |

Rows L2–L4 describe documented or source-derived mechanisms, not observed behaviour on this cloud.

### Writable layer

| Option | Where | Survives instance deletion | Notes |
| --- | --- | --- | --- |
| W0 file on the boot volume | boot volume | no | today |
| W1 a Cinder volume of the user | separate volume, `delete_on_termination: False` | yes | needs the binding rule and the immutable base lower of section 6 |

## 5. A composition from Nova and Cinder only

Candidate, to be tested before anything is built on it:

1. **Publish once.** The Hub registers each layer's SquashFS bytes as a `raw` Glance image carrying the
   layer digest. Which visibility can be used, and who may set it, is a deployment policy that was not
   inspected: `public` is admin-only by default and `shared` needs a member entry per consumer project.
2. **Per instance.** The boot volume is created as today. For each layer, in order, a volume is created from
   the layer's image (a clone without a data copy only if the prerequisites hold [UNVERIFIED]), marked
   read-only while `available`, and passed to Nova as a non-boot block device with
   `delete_on_termination: True` and a device tag.
3. **User volume.** The writable layer is a separate volume of the caller's project, new or existing,
   attached read-write with `delete_on_termination: False`.
4. **In the guest.** The bootstrap finds each device by the metadata-service tag (microversion 2.42 or
   later) or by the 20-character serial prefix, never by `/dev/vdX` order, checks that it is read-only,
   verifies the SHA-256 of the first `size` bytes against the digest the Hub sent, mounts it as SquashFS
   without a loop file, and assembles the overlay with upper and work directories on the user volume.

If the clones are real, are never flattened and no cached intermediate is created, this would store each
layer once in Glance (plus the Hub's own copy), the base once, and per instance only what the instance
writes. [INFERENCE] What it would cost:

- **Quota is logical.** Cinder counts volumes and their nominal gigabytes per project; the upstream default
  is 10 volumes. [UPSTREAM-SRC `cinder/quota.py`] Nine layers plus a boot and a user volume are eleven
  volumes for one instance. This cloud's project quotas were not inspected. Preflight and the instance row,
  which know one volume, would have to change.
- **Devices per instance.** One disk device per layer. The ceiling on this cloud is [UNVERIFIED]; long
  chains may need another bus or a packed variant (one volume per composition, which shares per composition
  instead of per layer). [INFERENCE]
- **More provider calls at create and delete**, and more states to clean up after a failure.
- **Verification reads every byte once per boot** unless dm-verity is adopted, which needs a hash tree at
  publish time and a trusted root digest (a format decision).
- **Read-only is a guard, not a boundary** (section 3). Because each instance has its own clone, a write
  that got through would stay in that clone. [INFERENCE]
- **Layer images appear in Glance.** They must not be offered as boot images, and an image with clones
  cannot be deleted (section 3).
- **No deduplication below a layer.** Two layers with the same files store them twice.

Compared with L1, this composition gives the guest no storage credential and needs no route from tenant
networks to the Ceph public network [INFERENCE], at the price of the quota, device-count and bookkeeping
costs above and of the unverified points. Which matters more here is an owner decision.

## 6. Rules that must hold whichever option is chosen

- A layer or base is an immutable input named by digest. A new parent is a new lineage; no existing
  instance, clone or user volume is changed by it.
- A writable layer is valid only over the exact ordered lowers it was written on. For a user volume that
  outlives an instance this has two consequences. The volume must record the composition it belongs to
  (base and ordered layer digests), and attaching it under another composition must be refused unless the
  user asks for it through the explicit base-migration acknowledgement, which is not implemented. And every
  lower must really be immutable: today's application mode uses the instance's own live boot root as the
  bottom lower, so a user volume written over one instance's root would later sit over a different root.
  W1 therefore needs the base as an immutable read-only lower as well (as the native-root path assembles
  it), or it must not be offered. Nothing migrates a user volume automatically, and rolling back to the old
  layers is not assumed to be safe once the volume was written under new ones.
- Whiteout, opaque-directory, hardlink and metadata semantics are those of the ordered SquashFS lowers and
  do not change with the transport.
- Sharing the bytes says nothing about compatibility. Digest and chain checks, filesystem structure, ABI,
  kernel and drivers, and application behaviour remain separate stages.
- A new path verifies the digest of a layer in the guest before use, whatever carried the bytes. (The
  existing `cloud env --no-verify` is an exception that this document does not extend.)

## 7. Not established

- That the Hub's base images are `raw`, that every clone prerequisite holds, and that its boot volumes are
  clones.
- That a volume created from another project's image is a clone.
- What a guest on this cloud sees as the disk serial, for virtio-blk and for virtio-scsi.
- That the read-only flag reaches the guest disk for volumes passed at server create, and that the
  hypervisor, not only the guest kernel, refuses the write.
- Create and delete latency with several volumes, and the device ceiling.
- That a read-only multi-attach volume works end to end through Nova (L3).
- Any saving in bytes, time or cost. No measurement was made.

## 8. Proposed bounded experiment (needs approval)

A functional check through ordinary tenant APIs in the designated test project. It is a developer-controlled
experiment, not product acceptance, and it changes no Hub. Each finished instance and its exclusive boot
disk are deleted and their absence observed before the next case
(`openspec/specs/contributor-workflow/spec.md`). The read-only `rbd info` in step 2 is Kolla administration
and needs its own authorization. Bound: two instances, one after the other, never at the same time.

1. Two small private `raw` Glance images, each a SquashFS file; one volume from each; one empty 1 GiB volume;
   a boot volume from a `raw` base image owned by the same project.
2. Confirm with `rbd info` whether the boot volume and the two layer volumes have their parent in pool
   `images`.
3. Mark the two layer volumes read-only; create the first instance with the three data volumes in its block
   device mapping, each with a tag.
4. In the guest: identify each device by tag and by serial prefix, record what the serial looks like,
   confirm the kernel reports the two as read-only, then clear the kernel flag as root, write, and expect an
   I/O error. Verify digests, mount, assemble the overlay with the third volume as upper, write a file.
5. Delete the first instance; confirm its boot and layer volumes are gone and the third volume remains.
   Confirm with `rbd info` that the layer images were not changed.
6. Create the second instance with fresh clones of the same images and the same third volume; confirm the
   file is there. Delete it and observe absence.
7. Delete the volumes, then the images (an image with clones cannot be deleted first), and re-check that
   nothing the test created remains.
8. Record timings as observations of this one run, not as a benchmark.

Not covered by this experiment: clones across projects, virtio-scsi, the device ceiling, multi-attach,
bytes saved, and the Hub's own images. A failure at any step is a result. Steps 2 and 4 decide whether
section 5 is worth designing in detail.

## 9. Decisions needed before any implementation

1. Whether layer sharing in the standard Hub path should follow the Manila target (L1), the Cinder clone
   composition (L2), or be decided after the experiment. L2 would revise the storage part of the 2026-10-29
   target.
2. Whether the writable layer may become a user-owned volume that outlives the instance (W1): this adds a
   resource the Hub must own the lifecycle of, changes quota use, needs the composition binding, and needs
   an immutable base lower instead of the live boot root.
3. Hub API and database changes (volumes per instance, layer image registration, user volume records) and
   the matching cleanup and quota rules.
4. Approval of the experiment in section 8 and its resource bound.

A block-level alternative, in which the hypervisor assembles immutable block layers and a per-user delta
into one root disk, is designed separately and is not compared here.
