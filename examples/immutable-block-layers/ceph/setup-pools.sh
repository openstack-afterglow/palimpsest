#!/usr/bin/env bash
# Pools and Ceph identities for block stacks. Run once by a Ceph administrator.
#
#   lib-layers   one RBD image per layer blob, sealed by a protected snapshot; written only by
#                the publisher, read by every compute node
#   DELTA_POOL   one writable RBD image per instance (with Cinder this is its volume pool)
#
# The compute-node identity can read the layer pool and cannot write it: read-only is enforced
# by Ceph capabilities, not only by the `--read-only` mapping option.
set -euo pipefail

LAYER_POOL=${1:-lib-layers}
DELTA_POOL=${2:-user-delta}
PG_NUM=${PG_NUM:-32}

for pool in "$LAYER_POOL" "$DELTA_POOL"; do
  ceph osd pool ls | grep -qx "$pool" || ceph osd pool create "$pool" "$PG_NUM"
  rbd pool init "$pool"
done

# Layer blobs are already compressed (ZFile); compressing them again in BlueStore buys nothing.
ceph osd pool set "$LAYER_POOL" compression_mode none

umask 077
# get-or-create fails when the identity exists with other capabilities; setting them
# afterwards makes a re-run converge instead.
identity() { # NAME OSD_CAPS
  ceph auth get-or-create "client.$1" mon 'profile rbd' osd "$2" >/dev/null 2>&1 || true
  ceph auth caps "client.$1" mon 'profile rbd' osd "$2" >/dev/null
  ceph auth get "client.$1" -o "/etc/ceph/ceph.client.$1.keyring" >/dev/null
}
identity blkstack-publisher "profile rbd pool=$LAYER_POOL"
# rbd-read-only still lets a client register clone children on a parent's header; it cannot
# write image data, create or remove images or snapshots in the layer pool.
identity blkstack-node "profile rbd-read-only pool=$LAYER_POOL, profile rbd pool=$DELTA_POOL"
echo "pools: $LAYER_POOL (layers), $DELTA_POOL (deltas); identities: client.blkstack-publisher, client.blkstack-node"
