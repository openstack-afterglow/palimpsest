#!/usr/bin/env bash
# End-to-end proof run of bin/blkstack on ONE disposable Linux host (it plays builder, developer
# machine and compute node). Needs root, /dev/kvm, overlaybd, qemu, cloud-image-utils, oras, a
# `registry` binary, and a Ceph cluster reachable with the default `rbd` configuration (a
# single-node MicroCeph is enough).
#
# DESTRUCTIVE on that host: it deletes EVERY image in the two pools it uses, rewrites
# /etc/overlaybd/overlaybd.json, empties overlaybd's registry cache and restarts overlaybd-tcmu.
# Never point it at a host or a cluster that holds anything else.
#
#   sudo poc/run-local-poc.sh /path/to/jammy-server-cloudimg-<arch>.img [OUT_DIR]
set -euo pipefail

IMAGE=${1:?cloud image}
OUT=${2:-/var/lib/blkstack-poc/out}
HERE=$(cd "$(dirname "$0")/.." && pwd)
B=$HERE/bin/blkstack
BASE=/var/lib/blkstack-poc
REG=127.0.0.1:5000
REPO=$REG/blk/ubuntu2204
LAYER_POOL=${POC_LAYER_POOL:-lib-layers}
DELTA_POOL=${POC_DELTA_POOL:-user-delta}
KEY=$BASE/ssh/id_ed25519
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=4 -i "$KEY")

mkdir -p "$OUT" "$BASE/ssh"; : >"$OUT/steps.jsonl"
say() { printf '\n== %s\n' "$*"; }
fail() { echo "FAILED: $*" >&2; exit 1; }
record() { jq -cn --arg k "$1" --arg v "$2" '{($k): ($v | tonumber? // .)}' >>"$OUT/steps.jsonl"; }
timed() { # KEY COMMAND...
  local key=$1 start end; shift
  start=$(date +%s.%N); "$@"; end=$(date +%s.%N)
  record "${key}_seconds" "$(awk -v a="$start" -v b="$end" 'BEGIN { printf "%.2f", b - a }')"
}
as() { BLKSTACK_ROOT=$BASE/$1 "$B" "${@:2}"; }
guest() { ssh "${SSH_OPTS[@]}" -p "$1" ubuntu@127.0.0.1 "${@:2}"; }
wait_ssh() { local i; for i in $(seq 150); do guest "$1" true 2>/dev/null && return 0; sleep 2; done; fail "guest on port $1 never answered"; }
wait_exit() { local pid i; pid=$(cat "$1"); for i in $(seq 90); do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done; fail "VM did not stop"; }
poweroff_guest() { guest "$1" 'sudo poweroff' 2>/dev/null || true; wait_exit "$2"; }
device_hash() { sha256sum <"$1" | cut -d' ' -f1; }
no_mappings() { [ -z "$(rbd device list --format json | jq -r '.[].name')" ] || fail "an RBD mapping was left behind: $*"; }
node_attach() { as node node-attach "$1" --instance "$2" --layer-pool "$LAYER_POOL" --delta-pool "$DELTA_POOL"; }
used_exact() { as node usage --image "$DELTA_POOL/delta-$1" | jq .used_bytes_exact; }

say "0. clean state from an earlier run (every blkstack device on this host)"
for pid in "$BASE"/*/sessions/*/qemu.pid; do [ ! -f "$pid" ] || kill "$(cat "$pid")" 2>/dev/null || true; done
sleep 1
awk -v d="$BASE/" 'index($5, d) == 1 { print $5 }' /proc/self/mountinfo | sort -r | while read -r mp; do umount "$mp" || umount -l "$mp"; done
for round in 1 2; do   # partitions first, then the disks under them
  for m in $(dmsetup ls 2>/dev/null | awk '/^blk-/ { print $1 }' | sort -r); do dmsetup remove --retry "$m" 2>/dev/null || [ "$round" = 1 ]; done
done
for naa in /sys/kernel/config/target/loopback/naa.6001*; do [ -d "$naa" ] || continue
  rm -f "$naa"/tpgt_1/lun/lun_0/blk_*; rmdir "$naa/tpgt_1/lun/lun_0" "$naa/tpgt_1" "$naa" 2>/dev/null || true
done
for core in /sys/kernel/config/target/core/user_1/blk_*; do [ ! -d "$core" ] || rmdir "$core"; done
for dev in $(rbd device list --format json 2>/dev/null | jq -r '.[].device'); do rbd device unmap "$dev"; done
rm -rf /dev/blkstack
for root in builder dev node lazy; do rm -rf --one-file-system "${BASE:?}/$root"; done
for pool in "$LAYER_POOL" "$DELTA_POOL"; do
  ceph osd pool ls | grep -qx "$pool" || continue
  for img in $(rbd ls "$pool"); do
    for snap in $(rbd snap ls "$pool/$img" --format json | jq -r '.[].name'); do rbd snap unprotect "$pool/$img@$snap" 2>/dev/null || true; done
    rbd snap purge "$pool/$img" >/dev/null 2>&1 || true; rbd rm "$pool/$img" >/dev/null
  done
done
"$HERE/ceph/setup-pools.sh" "$LAYER_POOL" "$DELTA_POOL"
PUBLISHER="rbd --id blkstack-publisher"; NODE="rbd --id blkstack-node"
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -f "$KEY"
pkill -x registry 2>/dev/null || true; rm -rf "$BASE/registry"; mkdir -p "$BASE/registry"
cat >"$BASE/registry.yml" <<EOF
version: 0.1
storage: {filesystem: {rootdirectory: $BASE/registry}, delete: {enabled: true}}
http: {addr: $REG}
EOF
nohup registry serve "$BASE/registry.yml" >"$OUT/registry.log" 2>&1 &
for _ in $(seq 50); do curl -fs "http://$REG/v2/" >/dev/null && break; sleep 0.2; done
# Keep layers lazy for the whole run: no background download of whole blobs.
jq '.download.enable = false' /etc/overlaybd/overlaybd.json >"$BASE/overlaybd.json" && install -m 0644 "$BASE/overlaybd.json" /etc/overlaybd/overlaybd.json
empty_lazy_cache() { systemctl stop overlaybd-tcmu; rm -rf /opt/overlaybd/registry_cache; systemctl start overlaybd-tcmu; }
empty_lazy_cache
cat >"$BASE/user-data" <<EOF
#cloud-config
ssh_authorized_keys: ["$(cat "$KEY.pub")"]
EOF

say "1. builder: base layer and one library layer"
as builder init
timed build_base as builder build-base --name ubuntu2204:base --image "$IMAGE" --size-gb 20
timed build_layer as builder build-layer --from ubuntu2204:base --name ubuntu2204:tools --recipe "$HERE/recipes/poc-tools.sh" --title "library stand-in"
as builder inspect ubuntu2204:tools | jq -c '.layers[] | {title, size}' | tee "$OUT/layers-built.jsonl"

say "2. builder: publish to the registry and to Ceph"
timed push_registry as builder push ubuntu2204:tools "$REPO:tools" --plain-http
BLKSTACK_RBD=$PUBLISHER timed publish_rbd as builder rbd-publish ubuntu2204:tools --pool "$LAYER_POOL"
rbd ls -l "$LAYER_POOL" | tee "$OUT/rbd-layers.txt"

say "3. developer machine: lazy pull (manifest only), boot, change, commit, push the delta"
as dev init
timed pull_lazy as dev pull "$REPO:tools" --name ubuntu2204:tools --plain-http
[ -z "$(ls "$BASE/dev/blobs/sha256")" ] || fail "lazy pull stored a layer blob"
as dev session-create ubuntu2204:tools devbox
DEV=$(as dev attach devbox); echo "developer device: $DEV"
timed boot_dev as dev boot devbox --user-data "$BASE/user-data" --ssh-port 2222
timed ssh_ready_dev wait_ssh 2222
seen=$(guest 2222 'cloud-init status --wait >/dev/null; jq --version; cat /opt/blkstack/layer-poc-tools; df -h / | tail -1'); echo "$seen"
grep -q 'layer=poc-tools' <<<"$seen" || fail "the library layer is not visible in the developer VM"
record lazy_cache_bytes_after_boot "$(du -s -B1 /opt/overlaybd/registry_cache | cut -f1)"
record layers_total_bytes "$(as dev inspect ubuntu2204:tools | jq '[.layers[].size] | add')"
[ -z "$(ls "$BASE/dev/blobs/sha256")" ] || fail "a layer blob was downloaded during the lazy boot"
guest 2222 'sudo apt-get update -q >/dev/null && sudo apt-get install -y -q tree >/dev/null 2>&1 && tree --version && echo "made on the developer machine" | sudo tee /opt/blkstack/dev-note >/dev/null'
# Before a session is shared: drop what belongs to this one machine (host keys, machine-id,
# cloud-init state), drop package lists, and let the filesystem discard freed blocks.
# (One command: once the host keys are gone, sshd accepts no new connection.)
guest 2222 'sudo rm -rf /var/lib/apt/lists/*; sudo cloud-init clean --logs --machine-id >/dev/null; sudo rm -f /etc/ssh/ssh_host_*; sync; sudo fstrim -av >/dev/null; sudo poweroff' || true
wait_exit "$BASE/dev/sessions/devbox/qemu.pid"
as dev detach devbox
as dev commit devbox --name ubuntu2204:dev1-untrimmed --title "developer change, as the guest left it"
record delta_bytes_before_host_trim "$(as dev inspect ubuntu2204:dev1-untrimmed | jq '.layers[-1].size')"
as dev trim devbox
timed commit_delta as dev commit devbox --name ubuntu2204:dev1 --title "developer change"
record delta_bytes "$(as dev inspect ubuntu2204:dev1 | jq '.layers[-1].size')"
as dev inspect ubuntu2204:dev1 | jq -c '.layers[-1] | {title, size}' | tee "$OUT/delta-layer.json"
timed push_delta as dev push ubuntu2204:dev1 "$REPO:dev1" --plain-http 2>&1 | tee "$OUT/push-delta.log"
[ "$(grep -c 'already in the registry' "$OUT/push-delta.log")" = 2 ] || fail "the delta push re-uploaded an existing layer"

say "4. a write smaller than 4 KiB over lower-layer data survives a reopen of the writable layer"
as dev session-create ubuntu2204:dev1 small-write >/dev/null
D=$(as dev attach small-write); SECTOR=2097152   # 1 GiB into the disk: inside the root filesystem's data
dd if="$D" bs=512 skip=$SECTOR count=16 iflag=direct status=none >"$OUT/small-before.bin"
[ "$(tr -d '\0' <"$OUT/small-before.bin" | wc -c)" -gt 4000 ] || fail "the test region holds no lower-layer data"
head -c 512 /dev/urandom >"$OUT/small-sector.bin"
{ head -c 1536 "$OUT/small-before.bin"; cat "$OUT/small-sector.bin"; tail -c 6144 "$OUT/small-before.bin"; } >"$OUT/small-expect.bin"
dd if="$OUT/small-sector.bin" of="$D" bs=512 seek=$((SECTOR + 3)) count=1 oflag=direct conv=fsync status=none
as dev detach small-write; D=$(as dev attach small-write)
dd if="$D" bs=512 skip=$SECTOR count=16 iflag=direct status=none | cmp - "$OUT/small-expect.bin" \
  || fail "a 512-byte write damaged the rest of its 4 KiB block after a reopen"
as dev detach small-write; as dev session-rm small-write
rm -f "$OUT"/small-*.bin; record small_write_survives_reopen true

say "5. compute node: verified pull, RBD publish of the new layer only"
as node init
timed pull_verified as node pull "$REPO:dev1" --name ubuntu2204:dev1 --plain-http --blobs
BLKSTACK_RBD=$PUBLISHER timed publish_rbd_delta as node rbd-publish ubuntu2204:dev1 --pool "$LAYER_POOL" 2>&1 | tee "$OUT/publish-delta.log"
[ "$(grep -c 'already published' "$OUT/publish-delta.log")" = 2 ] || fail "RBD publish rewrote an existing layer"
export BLKSTACK_RBD=$NODE   # from here on the compute-node identity: read-only on the layer pool
BASE_IMAGE=$LAYER_POOL/blk-$(as node inspect ubuntu2204:dev1 | jq -r '.layers[0].digest' | cut -d: -f2)
: >"$OUT/node-write-denied.txt"
for attempt in "snap create $BASE_IMAGE@tamper" "image-meta set $BASE_IMAGE blkstack.digest x" "snap unprotect $BASE_IMAGE@sealed" \
               "rm $BASE_IMAGE" "create --size 1M $LAYER_POOL/tamper"; do
  # shellcheck disable=SC2086
  if $NODE $attempt >>"$OUT/node-write-denied.txt" 2>&1; then fail "the compute-node identity could: rbd $attempt"; fi
done
grep -c 'Operation not permitted' "$OUT/node-write-denied.txt"

say "6. the same stack read through three backends is the same virtual disk"
# (a) local files: the node's verified blobs. (b) RBD: read-only snapshot mappings.
# (c) registry: lazy, with an emptied cache, so every byte comes over HTTP range requests.
as node session-create ubuntu2204:dev1 eq-file >/dev/null
D=$(as node attach eq-file); record file_stack_sha256 "$(device_hash "$D")"; as node detach eq-file; as node session-rm eq-file
timed node_attach node_attach ubuntu2204:dev1 vm1
record rbd_stack_sha256 "$(device_hash /dev/blkstack/vm1)"
as node node-detach --instance vm1; no_mappings "after hashing"
empty_lazy_cache
as lazy init; as lazy pull "$REPO:dev1" --name ubuntu2204:dev1 --plain-http
as lazy session-create ubuntu2204:dev1 eq-lazy >/dev/null
D=$(as lazy attach eq-lazy); record lazy_stack_sha256 "$(device_hash "$D")"; as lazy detach eq-lazy
[ -z "$(ls "$BASE/lazy/blobs/sha256")" ] || fail "the lazy stack has local blobs"
record lazy_cache_bytes_after_full_read "$(du -s -B1 /opt/overlaybd/registry_cache | cut -f1)"
jq -s 'add | [.rbd_stack_sha256, .file_stack_sha256, .lazy_stack_sha256] | unique | length == 1' "$OUT/steps.jsonl" | grep -qx true \
  || fail "the three backends disagree"

say "7. compute node: boot from RBD layers with the delta on the instance's own RBD image"
node_attach - vm1 >/dev/null   # `-`: the stack comes from the delta image's own record, as the agent does it
timed boot_node as node boot node-vm1 --user-data "$BASE/user-data" --ssh-port 2223
timed ssh_ready_node wait_ssh 2223
seen=$(guest 2223 'cloud-init status --wait >/dev/null; cat /opt/blkstack/dev-note; tree --version; jq --version'); echo "$seen"
grep -q 'made on the developer machine' <<<"$seen" || fail "the developer's change is not visible on the compute node"
guest 2223 'dd if=/dev/urandom of=/home/ubuntu/work.bin bs=1M count=64 conv=fsync status=none; sync; sudo fstrim -av >/dev/null; sync'
record delta_used_exact_after_guest_sync "$(used_exact vm1)"   # no host-side sync before this
poweroff_guest 2223 "$BASE/node/sessions/node-vm1/qemu.pid"
as node usage --image "$DELTA_POOL/delta-vm1" | tee "$OUT/usage-vm1.json"
rbd device list | tee "$OUT/rbd-mapped.txt"
as node node-detach --instance vm1; no_mappings "after detach"

say "8. the delta survives detach and attach, and is refused on another stack"
node_attach - vm1 >/dev/null
as node boot node-vm1 --ssh-port 2223; wait_ssh 2223
guest 2223 'stat -c %s /home/ubuntu/work.bin' | tee "$OUT/persisted.txt"
[ "$(cat "$OUT/persisted.txt")" = 67108864 ] || fail "the file written before the detach is gone"
poweroff_guest 2223 "$BASE/node/sessions/node-vm1/qemu.pid"; as node node-detach --instance vm1
as node pull "$REPO:tools" --name ubuntu2204:tools --plain-http --blobs
if node_attach ubuntu2204:tools vm1 2>"$OUT/refused.txt"; then fail "a delta was attached under another stack"; fi
cat "$OUT/refused.txt"; grep -q 'only valid on the exact stack' "$OUT/refused.txt" || fail "refused for another reason"
no_mappings "after the refusal"

say "9. a failed attach leaves nothing behind"
# A delta whose record names a layer that was never published: the attach maps the layers
# that exist, fails on the missing one, and has to undo what it did.
BROKEN=$(as node inspect ubuntu2204:dev1 | jq -cS 'del(.name, .source) | .layers[-1].digest = "sha256:" + ("0" * 64)')
$NODE create --size 22G --image-feature layering,exclusive-lock,object-map,fast-diff "$DELTA_POOL/delta-broken"
$NODE image-meta set "$DELTA_POOL/delta-broken" blkstack.stack "$BROKEN"
if node_attach - broken 2>"$OUT/failed-attach.txt"; then fail "an attach with an unpublished layer succeeded"; fi
cat "$OUT/failed-attach.txt"
no_mappings "after the failed attach"
[ ! -e "$BASE/node/sessions/node-broken" ] && [ ! -e /dev/blkstack/broken ] || fail "the failed attach left state behind"

say "10. two instances on one node share the layer mappings"
node_attach ubuntu2204:dev1 vm2 >/dev/null; node_attach ubuntu2204:dev1 vm3 >/dev/null
rbd device list --format json | jq -c '[.[] | {pool, name, snap}]' | tee "$OUT/rbd-mapped-two.json"
[ "$(rbd device list --format json | jq '[.[] | select(.pool == "'"$LAYER_POOL"'")] | length')" = 3 ] || fail "layers were mapped more than once"
as node node-detach --instance vm2
[ "$(rbd device list --format json | jq '[.[] | select(.pool == "'"$LAYER_POOL"'")] | length')" = 3 ] || fail "detaching one instance unmapped layers the other still uses"
dd if=/dev/blkstack/vm3 of=/dev/null bs=1M count=64 iflag=direct status=none || fail "the remaining instance lost its layers"
as node node-detach --instance vm3; no_mappings "after both instances"
rbd du -p "$LAYER_POOL" 2>/dev/null | tee "$OUT/rbd-du-layers.txt"; rbd du -p "$DELTA_POOL" 2>/dev/null | tee "$OUT/rbd-du-deltas.txt"

say "11. a flush on the assembled device reaches the delta image before any host-wide sync"
node_attach ubuntu2204:dev1 vm4 >/dev/null
DEVICE=$(readlink -f /dev/blkstack/vm4)
[ "$(cat "/sys/block/$(basename "$DEVICE")/queue/write_cache")" = "write back" ] || fail "flushes are not enabled on $DEVICE"
grep -qx 2147483 /sys/kernel/config/target/core/user_1/blk_*/attrib/cmd_time_out || fail "the TCMU command timeout was not raised"
sync; before=$(used_exact vm4)
dd if=/dev/urandom of="$DEVICE" bs=1M count=64 seek=8192 oflag=direct conv=fsync status=none   # what QEMU does for a guest flush
after=$(used_exact vm4)
record flush_test_delta_bytes_before "$before"; record flush_test_delta_bytes_after_flush "$after"
[ $((after - before)) -ge $((60 * 1024 * 1024)) ] || fail "flushed data did not reach the delta image ($before -> $after)"
as node node-detach --instance vm4

say "12. clean up"
for i in vm1 vm2 vm3 vm4 broken; do rbd rm "$DELTA_POOL/delta-$i" >/dev/null 2>&1 || true; done
pkill -x registry || true
jq -s 'add' "$OUT/steps.jsonl" | tee "$OUT/report.json"
echo "PROOF RUN PASSED"
