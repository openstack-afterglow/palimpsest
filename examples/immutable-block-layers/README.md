# Immutable block layers — reference code

Reference implementation for the design proposal in
[`docs/design/immutable-block-layers/`](../../docs/design/immutable-block-layers/README.ko.md). It is not part of
the Palimpsest product, is not wired into the CLI or the Hub, and has no CI lane.

| Path | What it is |
| --- | --- |
| `bin/blkstack` | CLI: build layers, sessions, commit, registry push/pull, RBD publish, compute-node attach, local boot |
| `pipeline/`, `recipes/` | Base → CUDA → cuDNN → PyTorch stack build and publication |
| `ceph/setup-pools.sh` | Pools and Ceph identities |
| `compute/` | Node agent (systemd socket), libvirt `qemu` hook, overlaybd configuration, udev rule example |
| `openstack/cinder_blkstack_driver.py` | Cinder backend sketch, never executed |
| `poc/` | Proof scripts. **Destructive**: disposable host and disposable Ceph cluster only |

Needs root on a Linux host with the `target_core_user`, `tcm_loop` and `dm-ebs` kernel modules and the
`overlaybd` package (1.0.18 was used). What was run, on what, and what was not is listed in the design README.
